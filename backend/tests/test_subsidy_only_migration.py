import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.database.base import Base
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementSubmission,
    ReimbursementUpload,
    utc_now,
)
from app.services.readiness import _database_ready


def seed_verifying(
    database,
    *,
    version=10,
    mode="excel_only",
    roles=("GENERATED_EXCEL",),
    items=(),
    files=(),
    subsidy="100.00",
    excel_order=None,
):
    now = utc_now()
    draft = ReimbursementDraft(
        corp_id="synthetic-corp",
        owner_user_id="synthetic-user",
        status="LOCKED",
        revision=5,
        department_id="100",
        department_name="合成部门",
        template_process_code="PROC-SYNTHETIC",
        template_config_version=1,
        schema_fingerprint="a" * 64,
        input_json="{}",
        related_instance_ids_json="[]",
        expires_at=now + timedelta(days=7),
        locked_at=now,
    )
    database.add(draft)
    database.flush()
    raw = {
        "snapshotVersion": version,
        "originalFiles": list(files),
        "input": {"items": list(items)},
        "totals": {"subsidyTotal": subsidy},
    }
    if mode is not None:
        raw["attachmentMode"] = mode
    submission = ReimbursementSubmission(
        draft_id=draft.id,
        corp_id=draft.corp_id,
        originator_user_id=draft.owner_user_id,
        originator_union_id="synthetic-union",
        originator_name="合成用户",
        department_id="100",
        department_name="合成部门",
        template_process_code="PROC-SYNTHETIC",
        template_config_version=1,
        schema_fingerprint="a" * 64,
        snapshot_version=version,
        form_snapshot_json=json.dumps(raw),
        related_instance_ids_json="[]",
        snapshot_sha256="b" * 64,
        idempotency_key_hash="c" * 64,
        status="VERIFYING",
        process_instance_id=f"oa-{draft.id}",
        oa_create_started_at=now,
        oa_request_hash="d" * 64,
    )
    database.add(submission)
    database.flush()
    for index, role in enumerate(roles):
        extension = "xlsx" if role == "GENERATED_EXCEL" else "pdf"
        database.add(
            ReimbursementUpload(
                submission_id=submission.id,
                draft_id=draft.id,
                source_draft_file_id=None,
                role=role,
                sort_order=excel_order
                if role == "GENERATED_EXCEL" and excel_order is not None
                else index,
                local_storage_key=f"generated/{submission.id}/{index}.{extension}",
                local_status="READY",
                reserved_bytes=100,
                file_name=f"synthetic.{extension}",
                file_type=extension,
                media_type="application/pdf"
                if extension == "pdf"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=100,
                sha256="e" * 64,
                upload_status="LINKED",
                space_id="synthetic-space",
                file_id=f"file-{submission.id}-{index}",
                linked_at=now,
            )
        )
    database.commit()
    return submission


def mark_submitted(database, submission):
    submission.status = "SUBMITTED"
    submission.business_id = "synthetic-business"
    submission.approval_url = "https://example.invalid/synthetic"
    submission.submitted_at = utc_now()
    database.commit()


@pytest.mark.parametrize("database_kind", ["metadata", "empty", "upgrade"])
@pytest.mark.parametrize(
    "case,accepted",
    [
        ({}, True),
        ({"subsidy": "0.00"}, False),
        ({"items": ({"amount": "1.00"},)}, False),
        ({"files": ({"draftFileId": "synthetic-file"},)}, False),
        ({"mode": None}, False),
        ({"mode": "unexpected"}, False),
        ({"excel_order": 1}, False),
        ({"roles": ("GENERATED_PDF", "GENERATED_EXCEL")}, False),
        ({"version": 9, "mode": None}, False),
        ({"version": 9, "mode": None, "roles": ("GENERATED_PDF", "GENERATED_EXCEL")}, True),
        ({"mode": "bundle_and_excel", "roles": ("GENERATED_PDF", "GENERATED_EXCEL")}, False),
        (
            {
                "mode": "bundle_and_excel",
                "files": ({"draftFileId": "synthetic-file"},),
                "roles": ("GENERATED_PDF", "GENERATED_EXCEL"),
            },
            True,
        ),
    ],
)
def test_fresh_and_migrated_databases_enforce_exact_attachment_manifests(
    settings_factory,
    monkeypatch,
    tmp_path,
    database_kind,
    case,
    accepted,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'manifest.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    engine = create_database_engine(settings.database_url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    try:
        if database_kind == "metadata":
            Base.metadata.create_all(engine)
        else:
            if database_kind == "upgrade":
                command.upgrade(config, "20261020_0020")
                assert not _database_ready(engine)
            command.upgrade(config, "head")
            assert _database_ready(engine)
        with create_session_factory(engine)() as database:
            submission = seed_verifying(database, **case)
            if accepted:
                mark_submitted(database, submission)
            else:
                with pytest.raises(IntegrityError):
                    mark_submitted(database, submission)
                database.rollback()
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_upgrade_preserves_old_snapshot_and_refuses_downgrade_with_v10_audit(
    settings_factory,
    monkeypatch,
    tmp_path,
):
    settings = settings_factory(database_url=f"sqlite:///{tmp_path / 'upgrade.db'}")
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    engine = create_database_engine(settings.database_url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    try:
        command.upgrade(config, "20261020_0020")
        with create_session_factory(engine)() as database:
            old = seed_verifying(
                database, version=9, mode=None, roles=("GENERATED_PDF", "GENERATED_EXCEL")
            )
            mark_submitted(database, old)
            old_json, old_id = old.form_snapshot_json, old.id
        command.upgrade(config, "head")
        with create_session_factory(engine)() as database:
            assert database.get(ReimbursementSubmission, old_id).form_snapshot_json == old_json
            assert database.get(ReimbursementSubmission, old_id).status == "SUBMITTED"
        command.downgrade(config, "20261020_0020")
        assert not _database_ready(engine)
        command.upgrade(config, "head")
        with create_session_factory(engine)() as database:
            new = seed_verifying(database)
            mark_submitted(database, new)
        with pytest.raises(RuntimeError, match="version 10"):
            command.downgrade(config, "20261020_0020")
        assert _database_ready(engine)
        with create_session_factory(engine)() as database:
            assert len(database.scalars(select(ReimbursementSubmission)).all()) == 2
    finally:
        engine.dispose()
        get_settings.cache_clear()
