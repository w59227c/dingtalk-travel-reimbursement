from __future__ import annotations

from pathlib import Path
from shutil import copyfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text

from app.database.session import create_database_engine
from app.main import create_app
from app.services import readiness
from app.services.reimbursement_staging import ReimbursementStaging


def test_database_readiness_accepts_actual_migration_head_and_rejects_old_schema(
    settings_factory, tmp_path, monkeypatch
):
    from alembic import command
    from alembic.config import Config

    from app.core.config import get_settings

    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'ready-migrated.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    try:
        command.upgrade(config, "20260904_0011")
        assert readiness._database_ready(engine) is False
        command.upgrade(config, "head")
        assert readiness._database_ready(engine) is True
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_related_approval_department_migration_preserves_old_rows_and_guards_evidence(
    settings_factory,
    tmp_path,
    monkeypatch,
) -> None:
    from alembic import command
    from alembic.config import Config

    from app.core.config import get_settings

    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'related-dept.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    engine = create_database_engine(settings.database_url)
    now = "2026-09-15 08:00:00"
    try:
        command.upgrade(config, "20260914_0018")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reimbursement_drafts "
                    "(id, corp_id, owner_user_id, status, revision, department_id, "
                    "department_name, template_process_code, template_config_version, "
                    "schema_fingerprint, input_json, related_instance_ids_json, "
                    "expires_at, locked_at, created_at, updated_at) VALUES "
                    "('draft-1', 'corp', 'user', 'DRAFT', 1, 'current', '当前部门', "
                    "'PROC', 1, :fingerprint, '{}', '[\"travel-1\"]', :now, NULL, :now, :now)"
                ),
                {"fingerprint": "a" * 64, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO reimbursement_draft_related_approvals "
                    "(id, draft_id, corp_id, owner_user_id, sort_order, process_instance_id, "
                    "travel_profile_key, process_code, catalog_config_version, "
                    "travel_schema_fingerprint, listed_from_ms, listed_to_ms, "
                    "travel_start_date, travel_end_date, source_travel_type_value, title, "
                    "business_id, instance_created_at, verified_at, created_at, updated_at) "
                    "VALUES ('related-1', 'draft-1', 'corp', 'user', 0, 'travel-1', "
                    "'domestic', 'TRAVEL', 1, :fingerprint, 1, 2, '2026-09-01', "
                    "'2026-09-02', NULL, '出差申请', 'BIZ-1', :now, :now, :now, :now)"
                ),
                {"fingerprint": "b" * 64, "now": now},
            )

        command.upgrade(config, "head")
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT process_instance_id, originator_department_id "
                    "FROM reimbursement_draft_related_approvals WHERE id = 'related-1'"
                )
            ).one()
            assert row == ("travel-1", None)
            connection.execute(
                text(
                    "UPDATE reimbursement_draft_related_approvals "
                    "SET originator_department_id = 'historical' WHERE id = 'related-1'"
                )
            )

        with pytest.raises(RuntimeError, match="department evidence"):
            command.downgrade(config, "20260914_0018")
    finally:
        engine.dispose()
        get_settings.cache_clear()


class _SchemaWithout:
    def __init__(
        self,
        delegate,
        *,
        table_name: str | None = None,
        column: tuple[str, str] | None = None,
    ) -> None:
        self._delegate = delegate
        self._table_name = table_name
        self._column = column

    def get_table_names(self):
        return [table for table in self._delegate.get_table_names() if table != self._table_name]

    def get_columns(self, table_name: str):
        columns = self._delegate.get_columns(table_name)
        if self._column is None or self._column[0] != table_name:
            return columns
        return [column for column in columns if column["name"] != self._column[1]]


def test_ready_reports_required_components_without_paths(client_factory) -> None:
    client = client_factory(ocr_enabled=False)

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {
            "status": "ready",
            "checks": {
                "database": "ok",
                "excelTemplate": "ok",
                "tempStorage": "ok",
                "reimbursementStaging": "ok",
                "ocr": "disabled",
                "dingtalkConfiguration": "configured",
            },
        },
    }
    assert "path" not in response.text.lower()


def test_ready_fails_closed_when_dingtalk_agent_id_is_not_configured(
    client_factory,
) -> None:
    client = client_factory(dingtalk_agent_id=None)

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["dingtalkConfiguration"] == "not_ready"
    assert "client-secret" not in response.text
    assert "1234567890" not in response.text


def test_ready_allows_explicit_development_mock_without_real_dingtalk_config(
    client_factory,
) -> None:
    client = client_factory(
        auth_mock_enabled=True,
        dingtalk_client_id="",
        dingtalk_client_secret="",
        dingtalk_corp_id="",
        dingtalk_agent_id=None,
    )

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["data"]["checks"]["dingtalkConfiguration"] == "development_mock"


def test_ready_rejects_database_without_union_id_column(client_factory) -> None:
    client = client_factory()
    with client.app.state.database_engine.begin() as connection:
        connection.execute(text("ALTER TABLE sessions DROP COLUMN dingtalk_union_id"))

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["database"] == "not_ready"


@pytest.mark.parametrize(
    "missing_table",
    [
        "reimbursement_drafts",
        "reimbursement_draft_files",
        "reimbursement_draft_related_approvals",
        "reimbursement_submissions",
        "reimbursement_uploads",
    ],
)
def test_ready_rejects_a_missing_reimbursement_table(
    client_factory,
    monkeypatch,
    missing_table: str,
) -> None:
    client = client_factory()
    monkeypatch.setattr(
        readiness,
        "inspect",
        lambda connection: _SchemaWithout(
            sqlalchemy_inspect(connection),
            table_name=missing_table,
        ),
    )

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["database"] == "not_ready"


@pytest.mark.parametrize(
    ("table_name", "missing_column"),
    [
        ("oa_template_profiles", "travel_profiles_json"),
        ("reimbursement_drafts", "owner_user_id"),
        ("reimbursement_draft_files", "file_status"),
        ("reimbursement_draft_related_approvals", "originator_department_id"),
        ("reimbursement_submissions", "process_instance_id"),
        ("reimbursement_uploads", "file_id"),
    ],
)
def test_ready_rejects_a_missing_critical_reimbursement_column(
    client_factory,
    monkeypatch,
    table_name: str,
    missing_column: str,
) -> None:
    client = client_factory()
    monkeypatch.setattr(
        readiness,
        "inspect",
        lambda connection: _SchemaWithout(
            sqlalchemy_inspect(connection),
            column=(table_name, missing_column),
        ),
    )

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["database"] == "not_ready"


def test_ready_fails_closed_for_invalid_template(settings_factory, tmp_path: Path) -> None:
    invalid_template = tmp_path / "invalid.xlsx"
    invalid_template.write_bytes(b"not an xlsx")
    settings = settings_factory(excel_template_path=invalid_template)
    application = create_app(settings)

    with TestClient(application) as client:
        response = client.get("/api/ready", headers={"X-Request-ID": "ready-check-1"})

    body = response.json()
    assert response.status_code == 503
    assert body["success"] is False
    assert body["error"] == {
        "code": "SERVICE_NOT_READY",
        "message": "服务尚未就绪",
    }
    assert body["data"]["status"] == "not_ready"
    assert body["data"]["checks"]["excelTemplate"] == "not_ready"
    assert body["requestId"] == "ready-check-1"
    assert str(invalid_template) not in response.text


def test_ocr_enabled_requires_local_models_and_locked_packages(settings_factory) -> None:
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=Path("/missing/detection"),
        ocr_recognition_model_dir=Path("/missing/recognition"),
    )
    application = create_app(settings)

    with TestClient(application) as client:
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["ocr"] == "not_ready"
    assert "/missing" not in response.text


def test_app_process_does_not_construct_real_ocr_engine(settings_factory, tmp_path: Path) -> None:
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "detection",
        ocr_recognition_model_dir=tmp_path / "recognition",
    )

    application = create_app(settings)

    # Real Paddle initialization belongs exclusively to the killable OCR worker.
    assert application.state.ocr_service._engine is None


def test_ready_rejects_model_files_with_wrong_sha256(settings_factory, tmp_path: Path) -> None:
    detection = tmp_path / "det"
    recognition = tmp_path / "rec"
    for directory in (detection, recognition):
        directory.mkdir()
        for name in ("inference.json", "inference.pdiparams", "inference.yml"):
            (directory / name).write_bytes(b"tampered")
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=detection,
        ocr_recognition_model_dir=recognition,
    )
    application = create_app(settings)

    with TestClient(application) as client:
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["ocr"] == "not_ready"


def test_temp_storage_check_rejects_symlink(settings_factory, tmp_path: Path) -> None:
    real_directory = tmp_path / "real-temp"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked-temp"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    settings = settings_factory(temp_dir=linked_directory)

    engine = create_database_engine(settings.database_url)
    try:
        report = readiness.check_readiness(settings, engine)
    finally:
        engine.dispose()

    assert report.ready is False
    assert report.checks["tempStorage"] == "not_ready"


def test_reimbursement_staging_check_rejects_symlink(
    settings_factory,
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real-staging"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-staging"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    settings = settings_factory(reimbursement_staging_dir=linked_directory)

    engine = create_database_engine(settings.database_url)
    try:
        report = readiness.check_readiness(settings, engine)
    finally:
        engine.dispose()

    assert report.ready is False
    assert report.checks["reimbursementStaging"] == "not_ready"


def test_reimbursement_staging_check_rejects_read_only_directory(
    settings_factory,
    tmp_path: Path,
) -> None:
    staging = tmp_path / "read-only-staging"
    staging.mkdir(mode=0o700)
    staging.chmod(0o500)
    settings = settings_factory(reimbursement_staging_dir=staging)

    engine = create_database_engine(settings.database_url)
    try:
        report = readiness.check_readiness(settings, engine)
    finally:
        staging.chmod(0o700)
        engine.dispose()

    assert report.ready is False
    assert report.checks["reimbursementStaging"] == "not_ready"


@pytest.mark.parametrize(
    ("available_delta", "expected_status"),
    [(-1, "not_ready"), (0, "ok")],
)
def test_reimbursement_staging_check_enforces_minimum_free_space_boundary(
    settings_factory,
    tmp_path: Path,
    monkeypatch,
    available_delta: int,
    expected_status: str,
) -> None:
    staging = tmp_path / "small-staging"
    settings = settings_factory(reimbursement_staging_dir=staging)
    ReimbursementStaging(
        staging,
        max_object_bytes=settings.upload_max_file_bytes,
    ).prepare()

    minimum_bytes = settings.reimbursement_staging_minimum_free_bytes

    class LimitedFreeSpace:
        f_bavail = minimum_bytes + available_delta
        f_frsize = 1

    monkeypatch.setattr(readiness.os, "fstatvfs", lambda _descriptor: LimitedFreeSpace())
    engine = create_database_engine(settings.database_url)
    try:
        report = readiness.check_readiness(settings, engine)
    finally:
        engine.dispose()

    assert report.checks["reimbursementStaging"] == expected_status


def test_reimbursement_staging_readiness_probe_leaves_no_file(
    settings_factory,
    tmp_path: Path,
) -> None:
    staging = tmp_path / "probed-staging"
    settings = settings_factory(reimbursement_staging_dir=staging)
    ReimbursementStaging(
        staging,
        max_object_bytes=settings.upload_max_file_bytes,
    ).prepare()

    engine = create_database_engine(settings.database_url)
    try:
        report = readiness.check_readiness(settings, engine)
    finally:
        engine.dispose()

    assert report.checks["reimbursementStaging"] == "ok"
    assert {path.name for path in staging.iterdir()} == {"drafts", "generated"}
    assert all(not any(area.iterdir()) for area in staging.iterdir())


def test_ready_fails_closed_without_leaking_unwritable_staging_path(client_factory) -> None:
    client = client_factory()
    staging = client.app.state.reimbursement_staging.root
    staging.chmod(0o500)
    try:
        response = client.get("/api/ready")
    finally:
        staging.chmod(0o700)

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["reimbursementStaging"] == "not_ready"
    assert str(staging) not in response.text


def test_ready_rejects_unsafe_reimbursement_staging_area(
    client_factory,
    tmp_path: Path,
) -> None:
    client = client_factory()
    staging = client.app.state.reimbursement_staging.root
    drafts = staging / "drafts"
    outside = tmp_path / "outside-staging-area"
    outside.mkdir(mode=0o700)
    drafts.rmdir()
    drafts.symlink_to(outside, target_is_directory=True)
    try:
        response = client.get("/api/ready")
    finally:
        drafts.unlink()
        drafts.mkdir(mode=0o700)

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["reimbursementStaging"] == "not_ready"
    assert str(outside) not in response.text


def test_database_check_rejects_missing_migration_revision(client_factory) -> None:
    client = client_factory()
    engine = client.app.state.database_engine
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260901_0001')")
        )

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["database"] == "not_ready"


def test_template_contract_validation_is_cached_by_bounded_fingerprint(
    settings_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = Path(__file__).parents[1] / "app" / "templates" / "expense_template.xlsx"
    template = tmp_path / "cached-template.xlsx"
    copyfile(source, template)
    settings = settings_factory(excel_template_path=template)
    engine = create_database_engine(settings.database_url)
    calls = 0
    original = readiness.load_validated_template

    def counted(path: Path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(readiness, "load_validated_template", counted)
    readiness._validated_template_fingerprint.cache_clear()
    try:
        readiness.check_readiness(settings, engine)
        readiness.check_readiness(settings, engine)
    finally:
        engine.dispose()
        readiness._validated_template_fingerprint.cache_clear()

    assert calls == 1
