"""All admin surfaces reject employee requests, regardless of UI visibility."""

import json
import re

from conftest import mock_login

from app.models.setting import Setting
from app.services.application_settings import ADMIN_USER_IDS_KEY


def admin_routes(client):
    for path, operations in client.app.openapi()["paths"].items():
        if not path.startswith("/api/admin/"):
            continue
        path = re.sub(r"\{([^}]+)\}", lambda match: "2042" if match[1] == "year" else "1", path)
        for method in operations:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                yield method.upper(), path


def test_every_administrator_endpoint_denies_anonymous_and_employee_even_with_valid_csrf(
    client_factory,
):
    client = client_factory(auth_mock_enabled=True, auth_mock_user_id="ordinary-employee")
    endpoints = list(admin_routes(client))
    assert len(endpoints) >= 15
    for method, path in endpoints:
        assert client.request(method, path, json={}).status_code == 401, (method, path)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    for method, path in endpoints:
        assert client.request(method, path, json={}, headers=headers).status_code == 403, (
            method,
            path,
        )
    # Employees read calculation settings, never the administrator list or writes.
    assert client.get("/api/settings").status_code == 200
    assert "adminUserIds" not in client.get("/api/settings").json()["data"]
    assert client.put("/api/settings", json={}, headers=headers).status_code == 405


def test_added_admin_gains_all_admin_access_in_existing_session_and_revocation_is_immediate(
    client_factory,
):
    client = client_factory(auth_mock_enabled=True, auth_mock_user_id="added-admin")
    csrf = mock_login(client)["csrfToken"]
    assert not client.get("/api/me").json()["data"]["isAdmin"]
    with client.app.state.database_session_factory() as database:
        database.merge(Setting(key=ADMIN_USER_IDS_KEY, value=json.dumps(["added-admin"])))
        database.commit()
    assert client.get("/api/me").json()["data"]["isAdmin"]
    headers = {"X-CSRF-Token": csrf}
    for method, path in admin_routes(client):
        response = client.request(method, path, json={}, headers=headers)
        assert response.status_code not in {401, 403}, (method, path, response.text)
        if method not in {"GET", "HEAD", "OPTIONS"}:
            assert client.request(method, path, json={}).status_code == 403, (method, path)
    settings = client.get("/api/admin/settings").json()["data"]
    body = {
        key: settings[key]
        for key in ("subsidyRates", "calculationMode", "appTitle", "adminUserIds")
    }
    body["appTitle"] = "合成管理员设置"
    assert client.put("/api/admin/settings", json=body, headers=headers).status_code == 200
    assert (
        client.post(
            "/api/admin/receipt-keywords",
            json={"keyword": "合成办公用品", "categoryId": "office"},
            headers=headers,
        ).status_code
        == 201
    )
    body["adminUserIds"] = []
    assert client.put("/api/admin/settings", json=body, headers=headers).status_code == 200
    assert not client.get("/api/me").json()["data"]["isAdmin"]
    assert client.put("/api/admin/settings", json=body, headers=headers).status_code == 403
    assert client.get("/api/admin/attendance-calendars").status_code == 403


def test_every_configured_admin_can_edit_settings_without_being_the_first_id(client_factory):
    for user_id in ("first-admin", "second-admin"):
        client = client_factory(
            auth_mock_enabled=True,
            auth_mock_user_id=user_id,
            admin_user_ids="first-admin,second-admin",
        )
        headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
        settings = client.get("/api/admin/settings").json()["data"]
        body = {
            key: settings[key]
            for key in ("subsidyRates", "calculationMode", "appTitle", "adminUserIds")
        }
        assert client.put("/api/admin/settings", json=body, headers=headers).status_code == 200
