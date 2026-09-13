import io
import json
import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.core.request_id import current_request_id
from app.main import create_app
from app.services.reimbursement_quota import ReimbursementQuotaCoordinator
from app.services.reimbursement_staging import StagingLayoutError


def make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        temp_dir=tmp_path / "receipts",
        reimbursement_staging_dir=tmp_path / "reimbursement-staging",
    )


def test_health_returns_success_envelope_and_request_id(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)

    application = create_app(settings)
    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"status": "ok"}}
    assert len(response.headers["X-Request-ID"]) == 32
    assert (tmp_path / "receipts").is_dir()
    assert application.state.reimbursement_staging.root == tmp_path / "reimbursement-staging"
    assert (tmp_path / "reimbursement-staging" / "drafts").is_dir()
    assert (tmp_path / "reimbursement-staging" / "generated").is_dir()


def test_reimbursement_staging_is_reclaimed_on_startup_and_periodically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    periodic_call = threading.Event()

    def record_reclaim(_coordinator: ReimbursementQuotaCoordinator) -> int:
        nonlocal calls
        calls += 1
        if calls >= 2:
            periodic_call.set()
        return 0

    monkeypatch.setattr(ReimbursementQuotaCoordinator, "reclaim_expired", record_reclaim)
    settings = make_test_settings(tmp_path)
    settings.temp_cleanup_interval_seconds = 1

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        assert periodic_call.wait(timeout=3)

    assert calls >= 2


def test_interrupted_local_writes_are_reclaimed_once_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def record_reclaim(_coordinator: ReimbursementQuotaCoordinator) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(
        ReimbursementQuotaCoordinator,
        "reclaim_interrupted_local_writes",
        record_reclaim,
        raising=False,
    )

    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        assert client.get("/api/health").status_code == 200

    assert calls == 1


def test_startup_failure_closes_external_clients_and_database(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'startup-failure.db'}",
        temp_dir=tmp_path / "receipts",
        reimbursement_staging_dir=tmp_path / "missing-parent" / "staging",
        ocr_enabled=False,
    )
    application = create_app(settings)
    disposed: list[bool] = []
    event.listen(
        application.state.database_engine,
        "engine_disposed",
        lambda _engine: disposed.append(True),
    )

    with pytest.raises(StagingLayoutError):
        with TestClient(application):
            pass

    assert disposed == [True]
    assert application.state.dingtalk_client._http.is_closed is True
    assert application.state.dingtalk_storage._upload_http.is_closed is True
    assert application.state.process_runner._closing is True


def test_safe_inbound_request_id_is_preserved(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.get("/api/health", headers={"X-Request-ID": "edge.trace-123:abc"})

    assert response.headers["X-Request-ID"] == "edge.trace-123:abc"


def test_unsafe_inbound_request_id_is_replaced(tmp_path: Path) -> None:
    with TestClient(create_app(make_test_settings(tmp_path))) as client:
        response = client.get("/api/health", headers={"X-Request-ID": "unsafe value\t"})

    assert response.headers["X-Request-ID"] != "unsafe value\t"
    assert len(response.headers["X-Request-ID"]) == 32


def test_unknown_api_route_uses_error_envelope(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/not-a-route")

    assert response.status_code == 404
    assert response.json()["success"] is False
    assert response.json()["error"] == {
        "code": "NOT_FOUND",
        "message": "请求的资源不存在",
    }
    assert response.json()["requestId"] == response.headers["X-Request-ID"]


def test_structured_request_log_omits_query_string(tmp_path: Path) -> None:
    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(JsonFormatter())
    request_logger = logging.getLogger("app.main")
    request_logger.addHandler(log_handler)

    try:
        with TestClient(create_app(make_test_settings(tmp_path))) as client:
            response = client.get("/api/health?authCode=sensitive-value")
    finally:
        request_logger.removeHandler(log_handler)
        log_handler.close()

    log_body = json.loads(log_output.getvalue())
    assert response.status_code == 200
    assert log_body["method"] == "GET"
    assert log_body["path"] == "/api/health"
    assert "authCode" not in log_output.getvalue()
    assert "sensitive-value" not in log_output.getvalue()


def test_unexpected_error_keeps_request_id_and_resets_context(tmp_path: Path) -> None:
    application = create_app(make_test_settings(tmp_path))
    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(JsonFormatter())
    error_logger = logging.getLogger("app.core.errors")
    error_logger.addHandler(log_handler)

    def raise_unexpected_error() -> None:
        raise RuntimeError("sensitive implementation detail")

    application.add_api_route("/api/_test/raise", raise_unexpected_error, methods=["GET"])

    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/_test/raise",
                headers={"X-Request-ID": "edge.failure-456"},
            )
    finally:
        error_logger.removeHandler(log_handler)
        log_handler.close()

    response_body = response.json()
    log_body = json.loads(log_output.getvalue())
    assert response.status_code == 500
    assert response_body["success"] is False
    assert response_body["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "服务暂时不可用",
    }
    assert response_body["requestId"] == "edge.failure-456"
    assert response_body["requestId"] == response.headers["X-Request-ID"]
    assert log_body == {
        "timestamp": log_body["timestamp"],
        "level": "ERROR",
        "logger": "app.core.errors",
        "message": "Unexpected request error",
        "requestId": response_body["requestId"],
        "exceptionType": "RuntimeError",
    }
    assert "sensitive implementation detail" not in response.text
    assert "sensitive implementation detail" not in log_output.getvalue()
    assert "Traceback" not in log_output.getvalue()
    assert current_request_id() == ""
