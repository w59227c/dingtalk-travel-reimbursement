from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import oa_reimbursements, reimbursement_submissions
from app.integrations.dingtalk.workflow import FormOption
from app.services import dingtalk, reimbursement_approval_scope, reimbursement_drafts

ADMIN_GRANTS = "/api/admin/reimbursement-delegation"
EMPLOYEES = "/api/me/reimbursement-employees"
CONTEXT = "/api/me/reimbursement-context"
TRAVEL = "/api/oa/travel-approvals"
CSRF_HEADER = "X-CSRF-Token"
CONTEXT_HEADER = "X-Reimbursement-Context"
EMPLOYEE = "beneficiary"
OPERATOR = "hr-primary"
SECOND_OPERATOR = "hr-secondary"
ADMIN = "bootstrap-admin"


@dataclass
class DelegationApi:
    connect: Callable[..., tuple[TestClient, dict]]
    departments: dict[str, int | tuple[int, ...]]
    approvals: dict[str, dict]
    listed_origins: list[str]
    employee_names: dict[str, str]
    employee_search_results: dict[str, list[str]]
    department_lookups: list[int]
    unavailable_departments: set[int]


@pytest.fixture
def delegation_api(client_factory):
    departments: dict[str, int | tuple[int, ...]] = {
        ADMIN: 31, OPERATOR: 11, SECOND_OPERATOR: 12, EMPLOYEE: 25,
    }
    approvals: dict[str, dict] = {}
    listed_origins: list[str] = []
    employee_names: dict[str, str] = {}
    employee_search_results: dict[str, list[str]] = {}
    department_lookups: list[int] = []
    unavailable_departments: set[int] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 7200})
        if path.endswith("/user/getuserinfo"):
            user_id = body["code"]
            assert user_id in departments
            return httpx.Response(200, json={"errcode": 0, "result": {"userid": user_id}})
        if path.endswith("/contact/users/search"):
            if body["queryWord"] in employee_search_results:
                matches = employee_search_results[body["queryWord"]]
                return httpx.Response(200, json={"list": matches, "totalCount": len(matches)})
            return httpx.Response(
                403, json={"code": "Forbidden.AccessDenied.AccessTokenPermissionDenied"}
            )
        if path.endswith("/user/get"):
            user_id = body["userid"]
            if user_id not in departments:
                return httpx.Response(200, json={"errcode": 60121})
            current_departments = departments[user_id]
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {
                        "userid": user_id,
                        "name": employee_names.get(user_id, f"合成员工 {user_id}"),
                        "unionid": f"fixture-union-{user_id}",
                        "dept_id_list": (
                            [current_departments] if isinstance(current_departments, int)
                            else list(current_departments)
                        ),
                    },
                },
            )
        if path.endswith("/department/get"):
            department_id = body["dept_id"]
            department_lookups.append(department_id)
            if department_id in unavailable_departments:
                return httpx.Response(200, json={"errcode": 60003})
            return httpx.Response(
                200, json={"errcode": 0, "result": {"name": f"合成部门 {department_id}"}}
            )
        if path.endswith("/department/listsub"):
            rows = [{"dept_id": 11, "name": "合成部门 11"}] if body["dept_id"] == 1 else []
            return httpx.Response(200, json={"errcode": 0, "result": rows})
        if path.endswith("/processes/instanceIds/query"):
            assert body["processCode"] == "PROC-TRAVEL-SYNTHETIC"
            assert body["statuses"] == ["COMPLETED"]
            (origin_id,) = body["userIds"]
            listed_origins.append(origin_id)
            ids = [key for key, value in approvals.items() if value["listedFor"] == origin_id]
            return httpx.Response(200, json={"success": True, "result": {"list": ids}})
        if path.endswith("/processInstances"):
            approval = approvals[request.url.params["processInstanceId"]]
            detail = {key: value for key, value in approval.items() if key != "listedFor"}
            return httpx.Response(200, json={"success": True, "result": detail})
        raise AssertionError(f"Unexpected DingTalk call: {path}")

    base = client_factory(transport=httpx.MockTransport(handler))
    with ExitStack() as clients:

        def connect(user_id: str | None = None):
            # Each client has a distinct cookie jar while using the same company and DB.
            client = TestClient(base.app)
            clients.callback(client.close)
            if user_id is None:
                return client, {}
            response = client.post("/api/auth/dingtalk", json={"authCode": user_id})
            assert response.status_code == 200, response.text
            return client, response.json()["data"]

        yield DelegationApi(
            connect, departments, approvals, listed_origins, employee_names,
            employee_search_results, department_lookups, unavailable_departments,
        )


def _context(client: TestClient, session: dict, beneficiary: str | None, **values):
    return client.post(
        CONTEXT,
        headers={CSRF_HEADER: session["csrfToken"]},
        json={"userId": beneficiary, **values},
    )


def _write_grants(
    client: TestClient,
    session: dict,
    *,
    employees: tuple[str, ...] = (),
    departments: tuple[str, ...] = (),
    revision: str | None = None,
):
    if revision is None:
        revision = client.get(ADMIN_GRANTS).json()["data"]["revision"]
    return client.put(
        ADMIN_GRANTS,
        headers={CSRF_HEADER: session["csrfToken"]},
        json={
            "employeeUserIds": list(employees),
            "departmentIds": list(departments),
            "expectedRevision": revision,
        },
    )


def _assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_delegation_defaults_to_denied_even_for_a_system_administrator(delegation_api):
    admin, admin_session = delegation_api.connect(ADMIN)
    operator, operator_session = delegation_api.connect(OPERATOR)
    anonymous, _ = delegation_api.connect()
    policy = admin.get(ADMIN_GRANTS).json()["data"]
    assert policy["employeeUserIds"] == []
    assert policy["departmentIds"] == []
    assert admin_session["isAdmin"]
    for client, session in ((admin, admin_session), (operator, operator_session)):
        assert not session.get("canDelegateReimbursements")
        assert session.get("operator") is None
        _assert_error(
            _context(client, session, EMPLOYEE), 403, "REIMBURSEMENT_DELEGATION_FORBIDDEN"
        )
    _assert_error(anonymous.get(EMPLOYEES, params={"q": EMPLOYEE}), 401, "UNAUTHORIZED")
    _assert_error(
        operator.get(EMPLOYEES, params={"q": EMPLOYEE}), 403, "REIMBURSEMENT_DELEGATION_FORBIDDEN"
    )
    assert operator.get(ADMIN_GRANTS).status_code == 403
    assert (
        _write_grants(
            operator, operator_session, employees=(OPERATOR,), revision=policy["revision"]
        ).status_code
        == 403
    )
    _assert_error(
        admin.put(
            ADMIN_GRANTS,
            json={
                "employeeUserIds": [OPERATOR],
                "departmentIds": [],
                "expectedRevision": policy["revision"],
            },
        ),
        403,
        "CSRF_INVALID",
    )


@pytest.mark.parametrize(
    ("employees", "departments"), [((OPERATOR,), ()), ((), ("11",))], ids=["employee", "department"]
)
def test_grant_uses_actual_operator_and_can_be_revoked(delegation_api, employees, departments):
    admin, admin_session = delegation_api.connect(ADMIN)
    operator, operator_session = delegation_api.connect(OPERATOR)
    rejected, rejected_session = delegation_api.connect(SECOND_OPERATOR)
    original = admin.get(ADMIN_GRANTS).json()["data"]
    saved = _write_grants(admin, admin_session, employees=employees, departments=departments)
    assert saved.status_code == 200, saved.text
    assert (
        _write_grants(
            admin,
            admin_session,
            employees=employees,
            departments=departments,
            revision=original["revision"],
        ).json()["data"]
        == saved.json()["data"]
    )
    _assert_error(
        _write_grants(
            admin, admin_session, employees=(SECOND_OPERATOR,), revision=original["revision"]
        ),
        409,
        "SETTINGS_REVISION_CONFLICT",
    )
    assert operator.get("/api/me").json()["data"]["canDelegateReimbursements"]
    assert operator.get(EMPLOYEES, params={"q": ADMIN}).json()["data"]["items"] == [
        {"userId": ADMIN, "name": f"合成员工 {ADMIN}", "departmentNames": ["合成部门 31"]}
    ]
    assert operator.get(EMPLOYEES, params={"q": OPERATOR}).json()["data"]["items"] == []
    _assert_error(
        _context(rejected, rejected_session, OPERATOR), 403, "REIMBURSEMENT_DELEGATION_FORBIDDEN"
    )

    selected = _context(operator, operator_session, ADMIN)
    assert selected.status_code == 200, selected.text
    delegate = selected.json()["data"]
    assert delegate["operator"] == operator_session["user"]
    assert delegate["user"]["userId"] == ADMIN
    assert delegate["selectedDepartment"]["id"] == "31"
    assert delegate["csrfToken"] == operator_session["csrfToken"]
    assert not delegate["isAdmin"]
    assert "set-cookie" not in selected.headers
    context = {CONTEXT_HEADER: delegate["reimbursementContext"]}
    assert operator.get(ADMIN_GRANTS, headers=context).status_code == 403
    assert operator.get("/api/me", headers=context).json()["data"] == delegate
    assert operator.get("/api/me").json()["data"]["user"]["userId"] == OPERATOR
    assert _context(operator, operator_session, None).json()["data"].get("operator") is None

    if departments:
        delegation_api.departments[OPERATOR] = delegation_api.departments[SECOND_OPERATOR]
        _assert_error(
            _context(operator, operator_session, ADMIN), 403, "REIMBURSEMENT_DELEGATION_FORBIDDEN"
        )

    revoked = _write_grants(admin, admin_session)
    assert revoked.status_code == 200, revoked.text
    assert not operator.get("/api/me").json()["data"].get("canDelegateReimbursements")
    _assert_error(
        operator.get("/api/me", headers=context), 403, "REIMBURSEMENT_DELEGATION_FORBIDDEN"
    )


def test_context_requires_csrf_and_is_bound_to_the_exact_session(delegation_api):
    admin, admin_session = delegation_api.connect(ADMIN)
    operator, operator_session = delegation_api.connect(OPERATOR)
    same_employee, other_session = delegation_api.connect(OPERATOR)
    anonymous, _ = delegation_api.connect()
    assert _write_grants(admin, admin_session, employees=(OPERATOR,)).status_code == 200
    _assert_error(operator.post(CONTEXT, json={"userId": EMPLOYEE}), 403, "CSRF_INVALID")
    _assert_error(_context(operator, other_session, EMPLOYEE), 403, "CSRF_INVALID")
    _assert_error(
        _context(operator, operator_session, EMPLOYEE, departmentId="11"),
        403,
        "REIMBURSEMENT_CONTEXT_INVALID",
    )
    data = _context(operator, operator_session, EMPLOYEE).json()["data"]
    token = data["reimbursementContext"]
    headers = {CONTEXT_HEADER: token}
    _assert_error(anonymous.get("/api/me", headers=headers), 401, "UNAUTHORIZED")
    _assert_error(
        same_employee.get("/api/me", headers=headers), 403, "REIMBURSEMENT_CONTEXT_INVALID"
    )
    tampered = token[:-1] + ("1" if token.endswith("0") else "0")
    _assert_error(
        operator.get("/api/me", headers={CONTEXT_HEADER: tampered}),
        403,
        "REIMBURSEMENT_CONTEXT_INVALID",
    )
    assert operator.get("/api/me", headers=headers).json()["data"]["user"]["userId"] == EMPLOYEE


def test_employee_user_id_selection_works_without_name_search_permission(delegation_api):
    admin, admin_session = delegation_api.connect(ADMIN)
    operator, _ = delegation_api.connect(OPERATOR)
    assert _write_grants(admin, admin_session, employees=(OPERATOR,)).status_code == 200
    _assert_error(
        operator.get(EMPLOYEES, params={"q": "合成员工"}),
        502,
        "REIMBURSEMENT_EMPLOYEE_SEARCH_UNAVAILABLE",
    )
    selected = operator.get(EMPLOYEES, params={"q": EMPLOYEE})
    assert selected.status_code == 200, selected.text
    assert selected.json()["data"]["items"] == [
        {"userId": EMPLOYEE, "name": f"合成员工 {EMPLOYEE}", "departmentNames": ["合成部门 25"]}
    ]


@pytest.mark.parametrize("label_failure", ["unavailable", "timeout"])
def test_employee_name_search_returns_current_departments_for_both_pickers(
    delegation_api, monkeypatch, label_failure,
):
    admin, admin_session = delegation_api.connect(ADMIN)
    operator, _ = delegation_api.connect(OPERATOR)
    assert _write_grants(admin, admin_session, employees=(OPERATOR,)).status_code == 200
    same_name = "合成同名员工"
    other = "another-beneficiary"
    delegation_api.departments[EMPLOYEE] = (26, 25, 26, 99)
    delegation_api.departments[other] = (26, 30)
    delegation_api.employee_names.update({EMPLOYEE: same_name, other: same_name})
    delegation_api.employee_search_results[same_name] = [EMPLOYEE, other, EMPLOYEE]
    cancelled_lookups = []
    if label_failure == "unavailable":
        delegation_api.unavailable_departments.add(99)
    else:
        service = operator.app.state.dingtalk_service
        original_lookup = service.get_department_identity

        async def lookup_department(department_id):
            if department_id != "99":
                return await original_lookup(department_id)
            delegation_api.department_lookups.append(99)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled_lookups.append(department_id)

        monkeypatch.setattr(dingtalk, "_EMPLOYEE_DEPARTMENT_LOOKUP_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(service, "get_department_identity", lookup_department)
    admin_search = "/api/admin/directory/employees"
    assert operator.get(admin_search, params={"q": same_name}).status_code == 403

    for client, path, id_key in ((operator, EMPLOYEES, "userId"), (admin, admin_search, "id")):
        delegation_api.department_lookups.clear()
        response = client.get(path, params={"q": same_name})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["data"]["items"] == [
            {
                id_key: EMPLOYEE, "name": same_name,
                "departmentNames": ["合成部门 26", "合成部门 25"],
            },
            {id_key: other, "name": same_name, "departmentNames": ["合成部门 26", "合成部门 30"]},
        ]
        assert sorted(delegation_api.department_lookups) == [25, 26, 30, 99]
    if label_failure == "timeout":
        assert cancelled_lookups == ["99", "99"]

    delegation_api.departments[EMPLOYEE] = 35
    current = operator.get(EMPLOYEES, params={"q": EMPLOYEE})
    assert current.status_code == 200, current.text
    assert current.json()["data"]["items"] == [
        {"userId": EMPLOYEE, "name": same_name, "departmentNames": ["合成部门 35"]}
    ]


def test_directory_selection_and_admin_department_authorization(delegation_api):
    admin, admin_session = delegation_api.connect(ADMIN)
    member, member_session = delegation_api.connect(OPERATOR)
    other, _ = delegation_api.connect(SECOND_OPERATOR)
    directory = "/api/admin/directory"
    settings_path = "/api/admin/settings"
    assert member.get(f"{directory}/departments").status_code == 403
    assert admin.get(f"{directory}/departments").json()["data"]["items"] == [
        {"id": "11", "name": "合成部门 11"}
    ]
    assert admin.get(
        f"{directory}/departments", params={"parentId": "11"},
    ).json()["data"]["items"] == []
    assert admin.get(
        f"{directory}/employees", params={"q": OPERATOR},
    ).json()["data"]["items"] == [
        {"id": OPERATOR, "name": f"合成员工 {OPERATOR}", "departmentNames": ["合成部门 11"]}
    ]
    assert admin.get(
        f"{directory}/labels",
        params=[("kind", "employee"), ("ids", OPERATOR), ("ids", "missing")],
    ).json()["data"]["items"] == [{"id": OPERATOR, "name": f"合成员工 {OPERATOR}"}]
    assert admin.get(
        f"{directory}/labels", params={"kind": "department", "ids": "11"},
    ).headers["cache-control"] == "no-store"

    settings = admin.get(settings_path).json()["data"]
    body = {
        key: settings[key]
        for key in ("subsidyRates", "calculationMode", "appTitle", "adminUserIds")
    }
    body.update(expectedRevision=settings["revision"], adminDepartmentIds=["11"])
    saved = admin.put(settings_path, json=body, headers={CSRF_HEADER: admin_session["csrfToken"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["adminDepartmentIds"] == ["11"]
    assert member.get("/api/me").json()["data"]["isAdmin"]
    assert member.get(f"{directory}/departments").status_code == 200
    assert other.get(f"{directory}/departments").status_code == 403
    assert member.put(settings_path, json=body).status_code == 403

    delegation_api.departments[OPERATOR] = 12
    _assert_error(
        member.put(settings_path, json=body, headers={CSRF_HEADER: member_session["csrfToken"]}),
        403, "FORBIDDEN",
    )
    assert not member.get("/api/me").json()["data"]["isAdmin"]

    body.update(expectedRevision=saved.json()["data"]["revision"], adminDepartmentIds=[])
    assert admin.put(
        settings_path, json=body, headers={CSRF_HEADER: admin_session["csrfToken"]},
    ).json()["data"]["adminDepartmentIds"] == []
    assert member.get(settings_path).status_code == 403
    assert admin.get(settings_path).status_code == 200


def test_department_grants_follow_live_membership_in_an_existing_session(delegation_api):
    admin, admin_session = delegation_api.connect(ADMIN)
    member, member_session = delegation_api.connect(SECOND_OPERATOR)
    settings_path = "/api/admin/settings"
    settings = admin.get(settings_path).json()["data"]
    body = {
        key: settings[key]
        for key in ("subsidyRates", "calculationMode", "appTitle", "adminUserIds")
    }
    body.update(expectedRevision=settings["revision"], adminDepartmentIds=["11"])
    assert admin.put(
        settings_path, json=body, headers={CSRF_HEADER: admin_session["csrfToken"]},
    ).status_code == 200
    assert _write_grants(admin, admin_session, departments=("11",)).status_code == 200

    delegation_api.departments[SECOND_OPERATOR] = 11
    own = member.get("/api/me").json()["data"]
    assert own["isAdmin"]
    assert own["canDelegateReimbursements"]
    assert own["csrfToken"] == member_session["csrfToken"]
    assert own["selectedDepartment"] == member_session["selectedDepartment"]
    assert member.get(settings_path).status_code == 200

    response = _context(member, member_session, EMPLOYEE)
    assert response.status_code == 200, response.text
    delegated = response.json()["data"]
    assert delegated["operator"]["userId"] == SECOND_OPERATOR
    assert delegated["user"]["userId"] == EMPLOYEE
    assert member.get(
        "/api/reimbursements/workspace",
        headers={CONTEXT_HEADER: delegated["reimbursementContext"]},
    ).status_code == 200


def _travel_catalog():
    travel = SimpleNamespace(
        profile_key="domestic",
        display_name="境内出差",
        process_code="PROC-TRAVEL-SYNTHETIC",
        schema=SimpleNamespace(fingerprint="d" * 64),
        start_date_component_id="travel-start",
        end_date_component_id="travel-end",
        company_component_id="company",
        budget_code_component_id="budget",
        travel_type_option=FormOption("市外项目出差（短期）", "市外项目出差（短期）", "trip"),
    )
    reimbursement = SimpleNamespace(
        process_code="PROC-REIMBURSEMENT",
        mappings={"company": "company", "budgetCode": "budget"},
        schema=SimpleNamespace(
            fingerprint="a" * 64,
            components=(
                SimpleNamespace(
                    component_id="company", options=(FormOption("公司", "公司", "company"),)
                ),
                SimpleNamespace(
                    component_id="budget", options=(FormOption("预算", "预算", "budget"),)
                ),
            ),
        ),
    )
    return SimpleNamespace(
        config_version=12, reimbursement=reimbursement, travel_profiles=(travel,)
    )


def _approval(
    instance_id: str,
    *,
    recipient: str,
    originator: str = EMPLOYEE,
    status: str = "COMPLETED",
    result: str = "agree",
) -> dict:
    return {
        "listedFor": EMPLOYEE,
        "title": f"合成出差 {instance_id}",
        "businessId": f"business-{instance_id}",
        "originatorUserId": originator,
        "originatorDeptId": "25",
        "status": status,
        "result": result,
        "createTime": "2026-09-01T00:00Z",
        "operationRecords": [{"type": "PROCESS_CC", "ccUserIds": [recipient]}],
        "formComponentValues": [
            {"id": "travel-start", "name": "开始日期", "value": "2026-09-01"},
            {"id": "travel-end", "name": "结束日期", "value": "2026-09-02"},
            {"id": "company", "name": "所属公司", "value": "公司"},
            {"id": "budget", "name": "预算代码", "value": "预算"},
        ],
    }


def test_delegated_list_and_save_require_executed_cc_to_current_operator(
    delegation_api, monkeypatch
):
    catalog = _travel_catalog()

    async def verified_catalog(_session_factory, _workflow):
        return catalog

    monkeypatch.setattr(oa_reimbursements, "load_fresh_submission_catalog", verified_catalog)
    monkeypatch.setattr(
        reimbursement_submissions, "load_fresh_submission_catalog", verified_catalog
    )
    monkeypatch.setattr(reimbursement_drafts, "require_submission_ready_catalog", lambda _: catalog)
    monkeypatch.setattr(
        reimbursement_approval_scope, "require_submission_ready_catalog", lambda _: catalog
    )
    delegation_api.approvals.update(
        {
            "yes": _approval("yes", recipient=OPERATOR),
            "other-hr": _approval("other-hr", recipient=SECOND_OPERATOR),
            "rejected": _approval("rejected", recipient=OPERATOR, result="refuse"),
            "running": _approval("running", recipient=OPERATOR, status="RUNNING"),
            "wrong-originator": _approval("wrong-originator", recipient=OPERATOR, originator=ADMIN),
            "unverified": {
                **_approval("unverified", recipient=OPERATOR),
                "operationRecords": None,
                "ccUserIds": [OPERATOR],
            },
        }
    )
    admin, admin_session = delegation_api.connect(ADMIN)
    operator, operator_session = delegation_api.connect(OPERATOR)
    assert _write_grants(admin, admin_session, employees=(OPERATOR,)).status_code == 200
    context = _context(operator, operator_session, EMPLOYEE).json()["data"]
    headers = {CSRF_HEADER: context["csrfToken"], CONTEXT_HEADER: context["reimbursementContext"]}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    response = operator.get(
        TRAVEL,
        headers=headers,
        params={
            "userId": ADMIN,
            "from": (today - timedelta(days=7)).isoformat(),
            "to": today.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    listing = response.json()["data"]
    assert [item["processInstanceId"] for item in listing["items"]] == ["yes"]
    assert OPERATOR not in response.text
    assert SECOND_OPERATOR not in response.text

    response = operator.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={"expectedRevision": 0, "reuseExisting": True, "input": {"items": []}},
    )
    assert response.status_code == 201, response.text
    draft = response.json()["data"]

    def save(instance_id):
        return operator.put(
            f"/api/reimbursements/drafts/{draft['id']}/related-approvals",
            headers=headers,
            json={
                "expectedRevision": draft["revision"],
                "selections": [
                    {
                        "profileKey": "domestic",
                        "processInstanceId": instance_id,
                        "queryWindow": listing["queryWindow"],
                    }
                ],
            },
        )

    _assert_error(save("other-hr"), 409, "TRAVEL_APPROVAL_CC_REQUIRED")
    _assert_error(save("unverified"), 502, "TRAVEL_APPROVAL_CC_UNVERIFIED")
    saved = save("yes")
    assert saved.status_code == 200, saved.text
    assert [item["processInstanceId"] for item in saved.json()["data"]["relatedApprovals"]] == [
        "yes"
    ]
    revision = saved.json()["data"]["revision"]
    delegation_api.approvals["yes"] = _approval("yes", recipient=SECOND_OPERATOR)
    _assert_error(
        operator.post(
            f"/api/reimbursements/drafts/{draft['id']}/excel-preview",
            headers=headers,
            json={"expectedRevision": revision},
        ),
        409,
        "TRAVEL_APPROVAL_CC_REQUIRED",
    )
    monkeypatch.setattr(operator.app.state.settings, "dingtalk_oa_worker_enabled", True)
    _assert_error(
        operator.post(
            f"/api/oa/reimbursements/{draft['id']}/submit",
            headers={**headers, "Idempotency-Key": "f5006cea-1dd6-41c9-8113-73cc70c34f7b"},
            json={"expectedRevision": revision},
        ),
        409,
        "TRAVEL_APPROVAL_CC_REQUIRED",
    )
    resumed = operator.get(f"/api/reimbursements/drafts/{draft['id']}", headers=headers)
    assert resumed.json()["data"]["status"] == "DRAFT"
    assert resumed.json()["data"]["revision"] == revision
    assert set(delegation_api.listed_origins) == {EMPLOYEE}
