from __future__ import annotations

import asyncio
import logging

import pytest
from browser_smoke_server import create_smoke_app
from conftest import mock_login
from fastapi.testclient import TestClient


def test_browser_smoke_uses_disposable_configuration_and_blocks_submission(tmp_path, monkeypatch):
    real_database = tmp_path / "must-not-create.db"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{real_database}")
    monkeypatch.setenv("DINGTALK_OA_WORKER_ENABLED", "true")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("REIMBURSEMENT_STAGING_DIR", str(tmp_path / "must-not-use"))
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>synthetic smoke</html>")
    app = create_smoke_app(tmp_path, dist)
    assert logging.getLogger("app.main").disabled is False
    settings = app.state.settings
    assert settings.app_env == "test"
    assert settings.dingtalk_oa_worker_enabled is False
    assert settings.ocr_fake_enabled is True
    assert settings.reimbursement_staging_dir == tmp_path / "staging"
    assert settings.database_url == f"sqlite:///{tmp_path / 'smoke.db'}"
    assert settings.session_cookie_secure is False
    assert not real_database.exists()
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 200
        assert client.get("/m").text == "<html>synthetic smoke</html>"
        assert client.get("/synthetic.pdf").status_code == 404
        assert client.post("/api/reimbursements/drafts/unused/submit").status_code == 403
        mock_login(client)
        seen = {}
        cursor = None
        for _ in range(5):
            response = client.get(
                "/api/oa/general-approvals", params={"cursor": cursor} if cursor else {}
            )
            assert response.status_code == 200
            page = response.json()["data"]
            seen.update((item["processInstanceId"], item) for item in page["items"])
            cursor = page.get("nextCursor")
            if not cursor:
                assert page["complete"] is True
                break
        else:
            raise AssertionError("Synthetic approval query did not finish")
        assert len(seen) == 122
        assert "synthetic-other-user" not in seen
        assert {item["status"] for item in seen.values()} == {"RUNNING", "COMPLETED", "TERMINATED"}
        assert any(item["result"] == "refuse" for item in seen.values())
        assert client.get("/__browser_smoke__").json()["fixture"] == "isolated-browser-smoke"
        with pytest.raises(AssertionError, match="must never create"):
            asyncio.run(app.state.dingtalk_workflow.create_process_instance(None))
