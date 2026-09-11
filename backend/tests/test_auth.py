from __future__ import annotations

import json
import logging
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.core.security import token_hash
from app.models.session import UserSession, utc_now


@pytest.mark.parametrize("worker_enabled", (False, True))
def test_public_config_exposes_authoritative_upload_limits(
    client_factory, worker_enabled: bool
) -> None:
    client = client_factory(
        dingtalk_agent_id=1234567890,
        dingtalk_client_secret="secret-must-stay-server-side",
        dingtalk_oa_worker_enabled=worker_enabled,
        upload_max_file_bytes=12 * 1024 * 1024,
        session_max_files=7,
        session_max_bytes=55 * 1024 * 1024,
        expense_max_items=123,
    )

    response = client.get("/api/config/public")

    assert response.status_code == 200
    assert response.json()["data"]["appTitle"] == "智能差旅费报销申请"
    assert response.json()["data"]["oaSubmissionEnabled"] is worker_enabled
    assert response.json()["data"]["uploadLimits"] == {
        "maxFiles": 7,
        "maxFileBytes": 12 * 1024 * 1024,
        "maxSessionBytes": 55 * 1024 * 1024,
    }
    assert response.json()["data"]["expenseLimits"] == {"maxItems": 123}
    assert "1234567890" not in response.text
    assert "secret-must-stay-server-side" not in response.text


def success_transport(
    department_ids: list[int] | None = None,
    department_names: dict[int, str] | None = None,
):
    department_ids = department_ids or [10]
    department_names = department_names or {}
    calls: list[tuple[str, str, dict[str, object], dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        query = dict(request.url.params)
        calls.append((request.method, str(request.url.copy_with(query=None)), body, query))
        if request.url.host == "api.dingtalk.com":
            assert str(request.url) == ("https://api.dingtalk.com/v1.0/oauth2/corp-fixed/token")
            assert body == {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "client_credentials",
            }
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 7200})
        assert query == {"access_token": "access-1"}
        if request.url.path.endswith("/user/getuserinfo"):
            assert body == {"code": "one-time-code"}
            return httpx.Response(200, json={"errcode": 0, "result": {"userid": "user-1"}})
        if request.url.path.endswith("/user/get"):
            assert body == {"userid": "user-1", "language": "zh_CN"}
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {
                        "name": "测试员工",
                        "unionid": "union-id-must-stay-server-side",
                        "dept_id_list": department_ids,
                    },
                },
            )
        department_id = body["dept_id"]
        assert body == {"dept_id": department_id, "language": "zh_CN"}
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {
                    "name": department_names.get(
                        department_id, f"部门-{department_id}"
                    )
                },
            },
        )

    return httpx.MockTransport(handler), calls


def test_single_department_login_session_cookie_and_csrf_rotation(client_factory) -> None:
    transport, calls = success_transport()
    client = client_factory(transport=transport)

    login = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})

    assert login.status_code == 200
    data = login.json()["data"]
    assert data["user"] == {"userId": "user-1", "name": "测试员工"}
    assert data["selectedDepartment"] == {"id": "10", "name": "部门-10"}
    assert data["departments"] == [{"id": "10", "name": "部门-10"}]
    assert data["isAdmin"] is False
    old_csrf = data["csrfToken"]
    cookie = login.headers["set-cookie"]
    assert "expense_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie

    application = client.app
    raw_cookie = client.cookies["expense_session"]
    with application.state.database_session_factory() as database:
        session = database.get(
            UserSession,
            token_hash(raw_cookie, application.state.settings.session_secret),
        )
        assert session is not None
        assert session.session_id_hash != raw_cookie
        assert session.corp_id == "corp-fixed"
        assert session.dingtalk_union_id == "union-id-must-stay-server-side"
        assert "测试员工" not in raw_cookie
    assert "union-id-must-stay-server-side" not in login.text

    me = client.get("/api/me")
    assert me.status_code == 200
    assert "union-id-must-stay-server-side" not in me.text
    new_csrf = me.json()["data"]["csrfToken"]
    assert new_csrf != old_csrf
    stale = client.post(
        "/api/me/department",
        json={"departmentId": "10"},
        headers={"X-CSRF-Token": old_csrf},
    )
    assert stale.status_code == 403
    current = client.post(
        "/api/me/department",
        json={"departmentId": "10"},
        headers={"X-CSRF-Token": new_csrf},
    )
    assert current.status_code == 200
    assert len([call for call in calls if "oauth2" in call[1]]) == 1


@pytest.mark.parametrize("union_id", [None, "", "   "])
def test_login_rejects_member_response_without_nonempty_union_id(
    client_factory,
    union_id: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.dingtalk.com":
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 7200})
        if request.url.path.endswith("/user/getuserinfo"):
            return httpx.Response(200, json={"errcode": 0, "result": {"userid": "user-1"}})
        if request.url.path.endswith("/user/get"):
            member = {"name": "测试员工", "dept_id_list": [10]}
            if union_id is not None:
                member["unionid"] = union_id
            return httpx.Response(200, json={"errcode": 0, "result": member})
        pytest.fail("department lookup must not run for an incomplete member identity")

    client = client_factory(transport=httpx.MockTransport(handler))

    response = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_AUTH_FAILED"
    assert "union" not in response.text.lower()
    assert "expense_session" not in client.cookies


def test_multiple_departments_require_authoritative_selection(client_factory) -> None:
    transport, _calls = success_transport([10, 20])
    client = client_factory(transport=transport)
    login = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})
    data = login.json()["data"]

    assert data["selectedDepartment"] is None
    assert [item["id"] for item in data["departments"]] == ["10", "20"]
    rejected = client.post(
        "/api/me/department",
        json={"departmentId": "999"},
        headers={"X-CSRF-Token": data["csrfToken"]},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "INVALID_DEPARTMENT"
    accepted = client.post(
        "/api/me/department",
        json={"departmentId": "20"},
        headers={"X-CSRF-Token": data["csrfToken"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["selectedDepartment"] == {
        "id": "20",
        "name": "部门-20",
    }


def test_login_filters_other_departments_and_deduplicates_remaining_names(
    client_factory,
) -> None:
    transport, _calls = success_transport(
        [10, 20, 30, 40, 50, 60],
        {
            10: "其他1",
            20: "技术管理中心",
            30: "技术管理中心",
            40: "其他2",
            50: "产品开发部",
            60: "其他xxx",
        },
    )
    client = client_factory(transport=transport)

    login = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})
    data = login.json()["data"]

    assert data["selectedDepartment"] is None
    assert data["departments"] == [
        {"id": "20", "name": "技术管理中心"},
        {"id": "50", "name": "产品开发部"},
    ]


def test_login_rejects_identity_when_all_departments_start_with_other(
    client_factory,
) -> None:
    transport, _calls = success_transport(
        [10, 20, 30],
        {10: "其他1", 20: "其他2", 30: "其他xxx"},
    )
    client = client_factory(transport=transport)

    login = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})

    assert login.status_code == 502
    assert login.json()["error"]["code"] == "DINGTALK_PERMISSION_MISSING"
    assert "expense_session" not in client.cookies


def test_existing_session_departments_are_filtered_and_deduplicated(
    client_factory,
) -> None:
    client = client_factory(auth_mock_enabled=True)
    login = client.post("/api/auth/mock")
    session_hash = token_hash(
        client.cookies["expense_session"],
        client.app.state.settings.session_secret,
    )
    with client.app.state.database_session_factory() as database:
        record = database.get(UserSession, session_hash)
        assert record is not None
        record.departments_json = json.dumps(
            [
                {"id": "10", "name": "其他1"},
                {"id": "20", "name": "技术管理中心"},
                {"id": "30", "name": "技术管理中心"},
                {"id": "40", "name": "其他xxx"},
                {"id": "50", "name": "产品开发部"},
            ]
        )
        record.current_department_id = "30"
        record.current_department_name = "技术管理中心"
        database.commit()

    me = client.get("/api/me")

    assert me.status_code == 200
    assert me.json()["data"]["departments"] == [
        {"id": "20", "name": "技术管理中心"},
        {"id": "50", "name": "产品开发部"},
    ]
    assert me.json()["data"]["selectedDepartment"] is None
    assert login.json()["data"]["selectedDepartment"] is not None


def test_expired_and_forged_sessions_return_401(client_factory) -> None:
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_departments="10:测试部门",
    )
    client.post("/api/auth/mock")
    raw_cookie = client.cookies["expense_session"]
    with client.app.state.database_session_factory() as database:
        record = database.get(
            UserSession,
            token_hash(raw_cookie, client.app.state.settings.session_secret),
        )
        assert record is not None
        record.expires_at = utc_now() - timedelta(seconds=1)
        database.commit()

    assert client.get("/api/me").status_code == 401
    client.cookies.set("expense_session", "forged")
    assert client.get("/api/me").status_code == 401


def test_session_without_union_id_requires_fresh_login(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    login = client.post("/api/auth/mock")
    assert login.status_code == 200
    raw_cookie = client.cookies["expense_session"]
    session_hash = token_hash(raw_cookie, client.app.state.settings.session_secret)
    with client.app.state.database_session_factory() as database:
        record = database.get(UserSession, session_hash)
        assert record is not None
        record.dingtalk_union_id = None
        database.commit()

    response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert "union" not in response.text.lower()
    with client.app.state.database_session_factory() as database:
        assert database.get(UserSession, session_hash) is None


def test_logout_requires_csrf_and_invalidates_server_session(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)
    login = client.post("/api/auth/mock").json()["data"]
    assert client.post("/api/auth/logout").status_code == 403
    logout = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": login["csrfToken"]},
    )
    assert logout.status_code == 200
    assert client.get("/api/me").status_code == 401


def test_mock_auth_is_explicit_and_cannot_accept_identity_or_admin_override(client_factory) -> None:
    disabled = client_factory()
    assert disabled.post("/api/auth/mock").status_code == 404

    enabled = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="fixed-user",
        auth_mock_user_name="固定测试用户",
        auth_mock_departments="10:测试部门",
        admin_user_ids="admin-only",
    )
    response = enabled.post(
        "/api/auth/mock",
        json={"userId": "admin-only", "isAdmin": True},
    )
    assert response.status_code == 200
    assert response.json()["data"]["user"]["userId"] == "fixed-user"
    assert response.json()["data"]["isAdmin"] is False
    assert "mock-union-id" not in response.text
    raw_cookie = enabled.cookies["expense_session"]
    with enabled.app.state.database_session_factory() as database:
        record = database.get(
            UserSession,
            token_hash(raw_cookie, enabled.app.state.settings.session_secret),
        )
        assert record is not None
        assert record.dingtalk_union_id == "mock-union-id:fixed-user"


def test_production_rejects_mock_and_placeholders_but_allows_http(settings_factory) -> None:
    with pytest.raises(ValidationError, match="AUTH_MOCK_ENABLED"):
        settings_factory(app_env="production", auth_mock_enabled=True)
    settings = settings_factory(app_env="production", session_cookie_secure=False)
    assert settings.session_cookie_secure is False
    with pytest.raises(ValidationError, match="DINGTALK_CLIENT_SECRET"):
        settings_factory(
            app_env="production",
            session_cookie_secure=False,
            dingtalk_client_secret="placeholder",
        )


def test_upstream_http_and_oapi_errors_are_safe(client_factory) -> None:
    def http_failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "secret upstream detail"})

    failed = client_factory(transport=httpx.MockTransport(http_failure)).post(
        "/api/auth/dingtalk", json={"authCode": "one-time-code"}
    )
    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "DINGTALK_AUTH_FAILED"
    assert "secret" not in failed.text

    def oapi_failure(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.dingtalk.com":
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 12345, "errmsg": "sensitive detail"})

    oapi = client_factory(transport=httpx.MockTransport(oapi_failure)).post(
        "/api/auth/dingtalk", json={"authCode": "one-time-code"}
    )
    assert oapi.status_code == 502
    assert oapi.json()["error"]["code"] == "DINGTALK_AUTH_FAILED"
    assert "sensitive" not in oapi.text


def test_invalid_access_token_is_evicted_and_retried_once(client_factory) -> None:
    token_calls = 0
    user_info_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, user_info_calls
        if request.url.host == "api.dingtalk.com":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{token_calls}", "expires_in": 7200},
            )
        body = json.loads(request.content.decode())
        token = request.url.params["access_token"]
        if request.url.path.endswith("/user/getuserinfo"):
            user_info_calls += 1
            assert body == {"code": "one-time-code"}
            if user_info_calls == 1:
                assert token == "access-1"
                return httpx.Response(200, json={"errcode": 40014})
            assert token == "access-2"
            return httpx.Response(200, json={"errcode": 0, "result": {"userid": "user-1"}})
        if request.url.path.endswith("/user/get"):
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {
                        "name": "测试员工",
                        "unionid": "union-retry-1",
                        "dept_id_list": [10],
                    },
                },
            )
        return httpx.Response(200, json={"errcode": 0, "result": {"name": "测试部门"}})

    client = client_factory(transport=httpx.MockTransport(handler))
    response = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})
    assert response.status_code == 200
    assert token_calls == 2
    assert user_info_calls == 2


@pytest.mark.parametrize("failure_kind", ["transport", "server"])
def test_one_time_auth_code_exchange_does_not_retry_ambiguous_failures(
    client_factory, failure_kind: str
) -> None:
    token_calls = 0
    user_info_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, user_info_calls
        if request.url.host == "api.dingtalk.com":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "access-once", "expires_in": 7200},
            )
        user_info_calls += 1
        assert request.url.path.endswith("/user/getuserinfo")
        if failure_kind == "transport":
            raise httpx.ConnectError("connection lost", request=request)
        return httpx.Response(503, json={"message": "ambiguous upstream failure"})

    client = client_factory(transport=httpx.MockTransport(handler))
    response = client.post("/api/auth/dingtalk", json={"authCode": "single-use-code"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_AUTH_FAILED"
    assert token_calls == 1
    assert user_info_calls == 1


def test_dingtalk_permission_error_43007_is_mapped(client_factory) -> None:
    oapi_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal oapi_calls
        if request.url.host == "api.dingtalk.com":
            return httpx.Response(
                200,
                json={"access_token": "permission-token", "expires_in": 7200},
            )
        oapi_calls += 1
        return httpx.Response(200, json={"errcode": 43007, "errmsg": "no scope"})

    client = client_factory(transport=httpx.MockTransport(handler))
    response = client.post("/api/auth/dingtalk", json={"authCode": "permission-code"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_PERMISSION_MISSING"
    assert oapi_calls == 1


def test_dingtalk_credentials_and_codes_never_reach_http_client_logs(
    client_factory, caplog
) -> None:
    access_token = "access-token-must-not-log"
    auth_code = "auth-code-must-not-log"
    client_secret = "client-secret-must-not-log"
    union_id = "union-id-must-not-log"
    agent_id = 9876543210

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        if request.url.host == "api.dingtalk.com":
            assert body["client_secret"] == client_secret
            return httpx.Response(
                200,
                json={"access_token": access_token, "expires_in": 7200},
            )
        assert request.url.params["access_token"] == access_token
        if request.url.path.endswith("/user/getuserinfo"):
            assert body == {"code": auth_code}
            return httpx.Response(
                200,
                json={"errcode": 0, "result": {"userid": "safe-log-user"}},
            )
        if request.url.path.endswith("/user/get"):
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {
                        "name": "日志测试用户",
                        "unionid": union_id,
                        "dept_id_list": [10],
                    },
                },
            )
        return httpx.Response(
            200,
            json={"errcode": 0, "result": {"name": "日志测试部门"}},
        )

    client = client_factory(
        transport=httpx.MockTransport(handler),
        dingtalk_client_secret=client_secret,
        dingtalk_agent_id=agent_id,
    )
    caplog.set_level(logging.INFO)

    response = client.post("/api/auth/dingtalk", json={"authCode": auth_code})

    assert response.status_code == 200
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert access_token not in caplog.text
    assert auth_code not in caplog.text
    assert client_secret not in caplog.text
    assert union_id not in caplog.text
    assert str(agent_id) not in caplog.text
    assert access_token not in response.text
    assert auth_code not in response.text
    assert client_secret not in response.text
    assert union_id not in response.text
    assert str(agent_id) not in response.text


def test_admin_access_is_rederived_from_current_configuration(client_factory) -> None:
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="admin-live-config",
        admin_user_ids="admin-live-config",
    )
    login = client.post("/api/auth/mock")
    assert login.json()["data"]["isAdmin"] is True
    raw_cookie = client.cookies["expense_session"]

    client.app.state.settings.admin_user_ids = ""
    assert client.get("/api/admin/settings").status_code == 403
    with client.app.state.database_session_factory() as database:
        record = database.get(
            UserSession,
            token_hash(raw_cookie, client.app.state.settings.session_secret),
        )
        assert record is not None and record.is_admin is False

    client.app.state.settings.admin_user_ids = "admin-live-config"
    assert client.get("/api/admin/settings").status_code == 200
    with client.app.state.database_session_factory() as database:
        record = database.get(
            UserSession,
            token_hash(raw_cookie, client.app.state.settings.session_secret),
        )
        assert record is not None and record.is_admin is True


def test_https_configuration_sets_secure_cookie(client_factory) -> None:
    transport, _calls = success_transport()
    client = client_factory(
        transport=transport,
        app_env="production",
        session_cookie_secure=True,
        session_secret="production-session-secret-at-least-32-characters",
    )
    response = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_production_http_configuration_omits_secure_cookie(client_factory) -> None:
    transport, _calls = success_transport()
    client = client_factory(
        transport=transport,
        app_env="production",
        session_cookie_secure=False,
        session_secret="production-session-secret-at-least-32-characters",
    )
    response = client.post("/api/auth/dingtalk", json={"authCode": "one-time-code"})
    assert response.status_code == 200
    assert "Secure" not in response.headers["set-cookie"]


def test_login_rejects_identity_spoof_fields(client_factory) -> None:
    transport, _calls = success_transport()
    client = client_factory(transport=transport)
    response = client.post(
        "/api/auth/dingtalk",
        json={
            "authCode": "one-time-code",
            "name": "伪造姓名",
            "departmentId": "999",
        },
    )
    assert response.status_code == 422
