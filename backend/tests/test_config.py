from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_settings_use_local_ocr_and_sqlite(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        temp_dir=tmp_path / "receipts",
    )

    assert settings.ocr_mode == "local"
    assert settings.ocr_enabled is True
    assert settings.admin_ids == frozenset()


def test_admin_ids_are_trimmed() -> None:
    settings = Settings(admin_user_ids="user-1, user-2,,")

    assert settings.admin_ids == frozenset({"user-1", "user-2"})


def test_production_requires_a_bootstrap_admin(settings_factory) -> None:
    with pytest.raises(ValidationError, match="ADMIN_USER_IDS"):
        settings_factory(
            app_env="production",
            session_cookie_secure=True,
            admin_user_ids=" , ",
        )


def test_mock_departments_are_parsed_without_accepting_invalid_entries() -> None:
    settings = Settings(auth_mock_departments="10:部门一,invalid,20: 部门二")

    assert settings.mock_departments == (("10", "部门一"), ("20", "部门二"))


def test_non_sqlite_database_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://example.invalid/expense")


def test_v1_requires_single_ocr_worker_and_safe_temp_ttl() -> None:
    with pytest.raises(ValidationError, match="OCR_CONCURRENCY"):
        Settings(ocr_concurrency=2)
    with pytest.raises(ValidationError, match="UPLOAD_TTL_MINUTES"):
        Settings(upload_ttl_minutes=3, ocr_timeout_seconds=120)
    assert Settings(upload_ttl_minutes=4, ocr_timeout_seconds=120).upload_ttl_minutes == 4


def test_ocr_queue_is_bounded_and_wired_for_deployment() -> None:
    settings = Settings(ocr_queue_max_waiters=8, ocr_queue_wait_seconds=150)
    assert settings.ocr_operation_timeout_seconds == 270
    for invalid in (0, 101):
        with pytest.raises(ValidationError, match="OCR_QUEUE_MAX_WAITERS"):
            Settings(ocr_queue_max_waiters=invalid)
    for invalid in (0, 601):
        with pytest.raises(ValidationError, match="OCR_QUEUE_WAIT_SECONDS"):
            Settings(ocr_queue_wait_seconds=invalid)

    for env_name in (".env.example", ".env.production.example"):
        contents = (REPOSITORY_ROOT / env_name).read_text()
        assert "OCR_QUEUE_MAX_WAITERS=8" in contents
        assert "OCR_QUEUE_WAIT_SECONDS=150" in contents
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert "OCR_QUEUE_MAX_WAITERS: ${OCR_QUEUE_MAX_WAITERS:-8}" in compose
    assert "OCR_QUEUE_WAIT_SECONDS: ${OCR_QUEUE_WAIT_SECONDS:-150}" in compose


def test_global_temp_quota_must_reserve_tmpfs_headroom() -> None:
    with pytest.raises(ValidationError, match="TEMP_STORAGE_MAX_BYTES"):
        Settings(temp_storage_max_bytes=1024 * 1024 * 1024)


def test_reimbursement_staging_quota_covers_one_complete_submission() -> None:
    with pytest.raises(ValidationError, match="REIMBURSEMENT_STAGING_MAX_BYTES"):
        Settings(
            session_max_bytes=100 * 1024 * 1024,
            upload_max_file_bytes=20 * 1024 * 1024,
            reimbursement_staging_max_bytes=119 * 1024 * 1024,
        )

    with pytest.raises(ValidationError, match="REIMBURSEMENT_STAGING_DIR"):
        Settings(reimbursement_staging_dir=Path("/tmp/.."))


def test_reimbursement_draft_ttl_is_bounded_and_wired_for_deployment() -> None:
    assert Settings(reimbursement_draft_ttl_days=1).reimbursement_draft_ttl_days == 1
    assert Settings(reimbursement_draft_ttl_days=365).reimbursement_draft_ttl_days == 365
    for invalid in (0, 366):
        with pytest.raises(ValidationError, match="REIMBURSEMENT_DRAFT_TTL_DAYS"):
            Settings(reimbursement_draft_ttl_days=invalid)

    for env_name in (".env.example", ".env.production.example", ".env.dingtalk-dev.example"):
        assert "REIMBURSEMENT_DRAFT_TTL_DAYS=30" in (REPOSITORY_ROOT / env_name).read_text()
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert "REIMBURSEMENT_DRAFT_TTL_DAYS: ${REIMBURSEMENT_DRAFT_TTL_DAYS:-30}" in compose


@pytest.mark.parametrize(
    ("temp_suffix", "staging_suffix"),
    [
        ("runtime", "runtime"),
        ("runtime", "runtime/reimbursements"),
        ("runtime/receipts", "runtime"),
    ],
)
def test_production_requires_staging_outside_temporary_storage(
    settings_factory,
    tmp_path: Path,
    temp_suffix: str,
    staging_suffix: str,
) -> None:
    with pytest.raises(ValidationError, match="REIMBURSEMENT_STAGING_DIR"):
        settings_factory(
            app_env="production",
            session_cookie_secure=True,
            temp_dir=tmp_path / temp_suffix,
            reimbursement_staging_dir=tmp_path / staging_suffix,
        )


def test_file_and_ocr_workers_have_distinct_memory_limits() -> None:
    settings = Settings()
    assert settings.file_worker_limits["memory_bytes"] == 512 * 1024 * 1024
    assert settings.ocr_worker_limits["memory_bytes"] == 5 * 1024 * 1024 * 1024


def test_production_ocr_requires_linux_limits(
    settings_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.config.sys.platform", "darwin")
    monkeypatch.setattr("app.core.config.platform.machine", lambda: "arm64")
    common = {
        "app_env": "production",
        "session_cookie_secure": True,
        "ocr_enabled": True,
        "ocr_detection_model_dir": tmp_path / "det",
        "ocr_recognition_model_dir": tmp_path / "rec",
    }
    with pytest.raises(ValidationError, match="Linux x86_64/amd64 worker limits"):
        settings_factory(**common)
    monkeypatch.setattr("app.core.config.sys.platform", "linux")
    with pytest.raises(ValidationError, match="Linux x86_64/amd64 worker limits"):
        settings_factory(**common)
    monkeypatch.setattr("app.core.config.platform.machine", lambda: "AMD64")
    assert settings_factory(**common).ocr_enabled


def test_production_requires_positive_numeric_dingtalk_agent_id(settings_factory) -> None:
    with pytest.raises(ValidationError, match="DINGTALK_AGENT_ID"):
        settings_factory(
            app_env="production",
            session_cookie_secure=True,
            dingtalk_agent_id=None,
        )
    with pytest.raises(ValidationError, match="DINGTALK_AGENT_ID"):
        settings_factory(dingtalk_agent_id=0)
    invalid_agent_id = "agent-id-must-not-reach-errors"
    with pytest.raises(ValidationError, match="DINGTALK_AGENT_ID") as caught:
        settings_factory(dingtalk_agent_id=invalid_agent_id)
    assert invalid_agent_id not in str(caught.value)
    assert invalid_agent_id not in repr(caught.value)

    assert settings_factory(dingtalk_agent_id="1234567890").dingtalk_agent_id == 1234567890


def test_agent_id_is_wired_through_deployment_and_development_entrypoints() -> None:
    for env_name in (".env.example", ".env.production.example", ".env.dingtalk-dev.example"):
        assert "DINGTALK_AGENT_ID=" in (REPOSITORY_ROOT / env_name).read_text()

    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert "DINGTALK_AGENT_ID: ${DINGTALK_AGENT_ID:-}" in compose

    dingtalk_script = (REPOSITORY_ROOT / "scripts" / "dev-dingtalk-backend.sh").read_text()
    required_variables = dingtalk_script.split("for variable_name in ", maxsplit=1)[1].split(
        "; do", maxsplit=1
    )[0]
    assert "DINGTALK_AGENT_ID" in required_variables
    assert "--reload" not in dingtalk_script

    frontend_script = (REPOSITORY_ROOT / "scripts" / "dev-dingtalk-frontend.sh").read_text()
    unset_line = next(line for line in frontend_script.splitlines() if line.startswith("unset "))
    assert "DINGTALK_AGENT_ID" in unset_line


def test_formal_company_local_entrypoint_is_isolated_from_test_data() -> None:
    makefile = (REPOSITORY_ROOT / "Makefile").read_text()
    assert "dev-dingtalk-prod:" in makefile
    assert "sh scripts/dev-dingtalk-backend.sh prod" in makefile
    assert "sh scripts/dev-dingtalk-frontend.sh prod" in makefile
    backend = (REPOSITORY_ROOT / "scripts/dev-dingtalk-backend.sh").read_text()
    assert 'CONFIG_FILE="$PROJECT_DIR/.env"' in backend
    assert "DATABASE_URL=sqlite:///./data/dev-dingtalk-prod.db" in backend
    assert "reimbursement-staging-prod" in backend
    assert "SESSION_COOKIE_NAME=expense_dingtalk_prod_session" in backend
    assert "export APP_ENV=development" in backend
    frontend = (REPOSITORY_ROOT / "scripts/dev-dingtalk-frontend.sh").read_text()
    assert 'CONFIG_FILE="$PROJECT_DIR/.env"' in frontend
    assert "export DINGTALK_DEV_FRONTEND_PORT=5173" in frontend


def test_storage_upload_boundary_settings_are_normalized_and_bounded() -> None:
    settings = Settings(
        dingtalk_storage_upload_timeout_seconds=5,
        dingtalk_storage_upload_host_suffixes=(
            " .TRANS.DINGTALK.COM,upload.example.com,trans.dingtalk.com "
        ),
    )
    assert settings.dingtalk_storage_upload_timeout_seconds == 5
    assert settings.storage_upload_host_suffixes == (
        "trans.dingtalk.com",
        "upload.example.com",
    )
    assert Settings(dingtalk_storage_upload_timeout_seconds=900)

    for timeout in (4.99, 901):
        with pytest.raises(ValidationError, match="UPLOAD_TIMEOUT_SECONDS"):
            Settings(dingtalk_storage_upload_timeout_seconds=timeout)
    for hosts in ("", "localhost", "127.0.0.1", "*.dingtalk.com", "a" * 4097):
        with pytest.raises(ValidationError, match="UPLOAD_HOST_SUFFIXES"):
            Settings(dingtalk_storage_upload_host_suffixes=hosts)


def test_storage_upload_settings_are_wired_through_deployment_entrypoints() -> None:
    required = (
        "DINGTALK_STORAGE_UPLOAD_TIMEOUT_SECONDS=120",
        "DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES=trans.dingtalk.com",
    )
    for env_name in (".env.example", ".env.production.example", ".env.dingtalk-dev.example"):
        content = (REPOSITORY_ROOT / env_name).read_text()
        for expected in required:
            assert expected in content

    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert (
        "DINGTALK_STORAGE_UPLOAD_TIMEOUT_SECONDS: ${DINGTALK_STORAGE_UPLOAD_TIMEOUT_SECONDS:-120}"
    ) in compose
    assert (
        "DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES: "
        "${DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES:-trans.dingtalk.com}"
    ) in compose


def test_dingtalk_oa_worker_settings_are_bounded() -> None:
    settings = Settings(
        dingtalk_oa_worker_poll_interval_seconds=0.1,
        dingtalk_oa_worker_lease_seconds=150,
        dingtalk_oa_worker_retry_base_seconds=1,
        dingtalk_oa_worker_retry_max_seconds=1,
        dingtalk_oa_worker_reconciliation_seconds=30,
    )
    assert settings.dingtalk_oa_worker_enabled is False
    assert (
        Settings(
            dingtalk_client_id="credentialed-client",
            dingtalk_client_secret="credentialed-secret",
            dingtalk_corp_id="ding-credentialed-corp",
            dingtalk_agent_id=123456,
        ).dingtalk_oa_worker_enabled
        is False
    )

    with pytest.raises(ValidationError, match="POLL_INTERVAL_SECONDS"):
        Settings(dingtalk_oa_worker_poll_interval_seconds=0)
    with pytest.raises(ValidationError, match="LEASE_SECONDS"):
        Settings(dingtalk_oa_worker_enabled=True, dingtalk_oa_worker_lease_seconds=149)
    with pytest.raises(ValidationError, match="RETRY_MAX_SECONDS"):
        Settings(
            dingtalk_oa_worker_retry_base_seconds=10,
            dingtalk_oa_worker_retry_max_seconds=9,
        )
    with pytest.raises(ValidationError, match="RECONCILIATION_SECONDS"):
        Settings(dingtalk_oa_worker_reconciliation_seconds=29)


def test_dingtalk_approval_detail_url_template_is_optional_and_strict() -> None:
    generic = Settings(
        dingtalk_corp_id="ding corp/id",
        dingtalk_approval_detail_url_template="",
    )
    assert generic.dingtalk_approval_detail_url_template == ""
    assert generic.dingtalk_approval_url("instance-1") == (
        "dingtalk://dingtalkclient/action/openapp?"
        "app_id=-4&container_type=work_platform&corpid=ding+corp%2Fid&ddtab=true"
    )
    template = "dingtalk://approval/detail?id={processInstanceId}"
    configured = Settings(dingtalk_approval_detail_url_template=f" {template} ")
    assert configured.dingtalk_approval_detail_url_template == template
    assert configured.dingtalk_approval_url("instance /1") == (
        "dingtalk://approval/detail?id=instance%20%2F1"
    )

    for invalid in (
        "https://example.com/no-placeholder",
        "https://example.com/{processInstanceId}/{processInstanceId}",
        "http://example.com/{processInstanceId}",
        "https://user:password@example.com/{processInstanceId}",
    ):
        with pytest.raises(ValidationError, match="DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE"):
            Settings(dingtalk_approval_detail_url_template=invalid)


def test_dingtalk_oa_worker_settings_are_wired_for_deployment() -> None:
    required = (
        "DINGTALK_OA_WORKER_ENABLED=false",
        "DINGTALK_OA_WORKER_POLL_INTERVAL_SECONDS=1",
        "DINGTALK_OA_WORKER_LEASE_SECONDS=180",
        "DINGTALK_OA_WORKER_RETRY_BASE_SECONDS=5",
        "DINGTALK_OA_WORKER_RETRY_MAX_SECONDS=300",
        "DINGTALK_OA_WORKER_RECONCILIATION_SECONDS=900",
        "DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE=",
    )
    for env_name in (".env.example", ".env.production.example", ".env.dingtalk-dev.example"):
        content = (REPOSITORY_ROOT / env_name).read_text()
        for expected in required:
            assert expected in content
        assert (
            "Keep false until migrations, templates, permissions, and one acceptance run "
            "are verified."
        ) in content

    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert "DINGTALK_OA_WORKER_ENABLED: ${DINGTALK_OA_WORKER_ENABLED:-false}" in compose
    assert (
        "Keep false until migrations, templates, permissions, and one acceptance run are verified."
    ) in compose
    for variable in (
        "DINGTALK_OA_WORKER_ENABLED",
        "DINGTALK_OA_WORKER_POLL_INTERVAL_SECONDS",
        "DINGTALK_OA_WORKER_LEASE_SECONDS",
        "DINGTALK_OA_WORKER_RETRY_BASE_SECONDS",
        "DINGTALK_OA_WORKER_RETRY_MAX_SECONDS",
        "DINGTALK_OA_WORKER_RECONCILIATION_SECONDS",
        "DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE",
    ):
        assert f"{variable}: ${{{variable}:-" in compose


def test_reimbursement_staging_is_wired_to_a_persistent_compose_volume() -> None:
    for env_name in (".env.example", ".env.production.example", ".env.dingtalk-dev.example"):
        content = (REPOSITORY_ROOT / env_name).read_text()
        assert "REIMBURSEMENT_STAGING_DIR=" in content
        assert "REIMBURSEMENT_STAGING_MAX_BYTES=4294967296" in content
    for env_name in (".env.example", ".env.production.example"):
        assert "REIMBURSEMENT_STAGING_DIR=/app/staging" in (REPOSITORY_ROOT / env_name).read_text()

    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text()
    assert "REIMBURSEMENT_STAGING_DIR: /app/staging" in compose
    assert "REIMBURSEMENT_STAGING_DIR: ${" not in compose
    assert (
        "REIMBURSEMENT_STAGING_MAX_BYTES: ${REIMBURSEMENT_STAGING_MAX_BYTES:-4294967296}" in compose
    )
    assert "- reimbursement_staging:/app/staging" in compose
    assert "\n  reimbursement_staging:\n" in compose
    assert (
        "/app/staging:"
        not in compose.split("tmpfs:", maxsplit=1)[1].split("healthcheck:", maxsplit=1)[0]
    )
