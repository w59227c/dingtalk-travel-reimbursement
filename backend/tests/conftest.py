from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.database.base import Base
from app.main import create_app
from app.ocr.types import LocalOcrEngine


@pytest.fixture
def settings_factory(tmp_path: Path):
    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "app_env": "test",
            "database_url": f"sqlite:///{tmp_path / 'phase2.db'}",
            "temp_dir": tmp_path / "receipts",
            "reimbursement_staging_dir": tmp_path / "reimbursement-staging",
            "excel_template_path": Path(__file__).parents[1]
            / "app"
            / "templates"
            / "expense_template.xlsx",
            "dingtalk_client_id": "client-id",
            "dingtalk_client_secret": "client-secret",
            "dingtalk_corp_id": "corp-fixed",
            "dingtalk_agent_id": 1234567890,
            "dingtalk_oa_worker_enabled": False,
            "admin_user_ids": "bootstrap-admin",
            "session_secret": "test-session-secret-with-more-than-32-chars",
            "session_cookie_secure": False,
            # Unit tests opt out unless they exercise the OCR boundary
            # explicitly; application and deployment defaults remain enabled.
            "ocr_enabled": False,
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def client_factory(settings_factory):
    clients: list[TestClient] = []

    def factory(
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        upload_transport: httpx.AsyncBaseTransport | None = None,
        ocr_engine: LocalOcrEngine | None = None,
        **settings_overrides: object,
    ) -> TestClient:
        application = create_app(
            settings_factory(**settings_overrides),
            dingtalk_transport=transport,
            dingtalk_upload_transport=upload_transport,
            ocr_engine=ocr_engine,
        )
        Base.metadata.create_all(application.state.database_engine)
        with application.state.database_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('20260915_0019')")
            )
        client = TestClient(application)
        client.__enter__()
        clients.append(client)
        return client

    yield factory

    for client in reversed(clients):
        client.__exit__(None, None, None)


def mock_login(client: TestClient) -> dict[str, object]:
    response = client.post("/api/auth/mock")
    assert response.status_code == 200, response.text
    return response.json()["data"]
