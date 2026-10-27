from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date

import pytest
from test_travel_approvals import _catalog, _instance, _profile

from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.integrations.dingtalk.workflow import WorkflowFormValue, WorkflowInstanceIdPage
from app.services import approval_diagnostics
from app.services.travel_approvals import TravelApprovalQueryWindow

NUMBER = "202608010800000000001"
INSTANCE = "synthetic-instance"
PATH = "/api/admin/oa/travel-approvals/diagnose"


class Workflow:
    def __init__(self, instance=None, *, listed=True, lookup=True, failure=None):
        self.instance = instance or _instance(INSTANCE, business_id=NUMBER)
        self.normal_ids = (INSTANCE,) if listed else ()
        self.lookup_ids = (INSTANCE,) if lookup else ()
        self.failure = failure
        self.list_calls = []
        self.lookup_calls = []
        self.detail_calls = []
        self.delay = 0
        self.active = 0

    async def list_process_instance_ids(self, **kwargs):
        self.list_calls.append(kwargs)
        return WorkflowInstanceIdPage(self.normal_ids, None)

    async def list_process_instance_ids_for_diagnostics(self, **kwargs):
        self.lookup_calls.append(kwargs)
        assert kwargs["end_time"] - kwargs["start_time"] == 59_999
        return WorkflowInstanceIdPage(self.lookup_ids, None)

    async def get_process_instance(self, instance_id):
        self.detail_calls.append(instance_id)
        self.active += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.failure:
                raise self.failure
            return self.instance
        finally:
            self.active -= 1


@pytest.fixture
def catalog(monkeypatch):
    configured = _catalog(_profile("domestic", "PROC-A"))

    async def load(_factory, _workflow):
        return configured

    monkeypatch.setattr(approval_diagnostics, "load_readonly_submission_catalog", load)
    return configured


async def diagnose(workflow, *, user_id="employee-1", identifier=NUMBER, kind="auto", keyword=""):
    return await approval_diagnostics.diagnose_travel_approval(
        object(),
        workflow,
        user_id=user_id,
        identifier=identifier,
        identifier_kind=kind,
        query_window=TravelApprovalQueryWindow.from_dates(date(2026, 8, 1), date(2026, 8, 31)),
        keyword=keyword,
    )


def check(report, key):
    return next(item for item in report["checks"] if item["key"] == key)


@pytest.mark.asyncio
async def test_same_day_reuses_normal_policy_and_never_performs_unscoped_lookup(catalog):
    profile = catalog.travel_profiles[0]
    profile.start_date_component_id = profile.end_date_component_id = "period"
    instance = _instance(INSTANCE, business_id=NUMBER, start_date=None, end_date=None)
    instance = replace(
        instance,
        form_values=(
            *instance.form_values,
            WorkflowFormValue(
                "period",
                "日期",
                "DDDateRangeField",
                '["2026-08-10","2026-08-10","1"]',
                None,
                None,
            ),
        ),
    )
    workflow = Workflow(instance)
    report = await diagnose(workflow)
    assert report["outcome"] == "visible"
    assert report["record"]["sameDay"] is True
    assert check(report, "dates")["status"] == "passed"
    assert report["counts"]["returned"] == 1
    assert report["counts"]["excluded"] == 0
    assert workflow.list_calls[0]["user_ids"] == ("employee-1",)
    assert workflow.list_calls[0]["statuses"] == ("COMPLETED",)
    assert workflow.lookup_calls == []
    assert workflow.detail_calls == [INSTANCE]


@pytest.mark.asyncio
async def test_invalid_budget_is_visible_but_unselectable_instead_of_being_filtered(catalog):
    instance = _instance(INSTANCE, business_id=NUMBER)
    instance = replace(
        instance,
        form_values=tuple(
            replace(value, value="合成失效预算") if value.component_id == "source-budget" else value
            for value in instance.form_values
        ),
    )
    report = await diagnose(Workflow(instance))
    assert report["outcome"] == "unselectable"
    assert check(report, "listed")["status"] == "passed"
    assert check(report, "accounting")["status"] == "failed"
    assert "预算代码" in check(report, "accounting")["message"]
    assert report["counts"]["returned"] == 1
    assert report["counts"]["unselectable"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected_check", "reason"),
    [
        ({"user_id": "different-owner"}, "owner", "ORIGINATOR_MISMATCH"),
        ({"status": "RUNNING"}, "status", "NOT_COMPLETED"),
        ({"result": "refuse"}, "result", "NOT_AGREED"),
        ({"start_date": None}, "dates", "DATE_FIELDS_INVALID"),
    ],
)
async def test_rejections_have_the_same_reason_as_normal_candidate_filtering(
    catalog,
    overrides,
    expected_check,
    reason,
):
    report = await diagnose(Workflow(_instance(INSTANCE, business_id=NUMBER, **overrides)))
    assert report["outcome"] == "excluded"
    assert check(report, expected_check)["status"] == "failed"
    assert report["counts"]["exclusions"] == [
        {"code": reason, "reason": approval_diagnostics._REASONS[reason], "count": 1},
    ]
    if expected_check == "owner":
        assert report["record"]["ownerMatches"] is False
        assert report["record"]["startDate"] is None
        assert "different-owner" not in json.dumps(report)


@pytest.mark.asyncio
async def test_keyword_exclusion_is_separate_from_upstream_list_membership(catalog):
    report = await diagnose(Workflow(), keyword="no-such-title")
    assert report["outcome"] == "excluded"
    assert check(report, "keyword")["status"] == "failed"
    assert check(report, "listed")["status"] == "passed"
    assert report["counts"]["returned"] == 0
    assert report["counts"]["exclusions"][0]["code"] == "KEYWORD_MISMATCH"


@pytest.mark.asyncio
async def test_details_that_pass_rules_but_are_absent_upstream_are_explained(catalog):
    workflow = Workflow(listed=False)
    report = await diagnose(workflow)
    assert report["outcome"] == "excluded"
    assert "员工查询没有返回" in report["summary"]
    assert check(report, "owner")["status"] == "passed"
    assert check(report, "dates")["status"] == "passed"
    assert check(report, "accounting")["status"] == "passed"
    assert check(report, "listed")["status"] == "failed"
    assert workflow.lookup_calls


@pytest.mark.asyncio
async def test_business_number_fallback_includes_unfinished_approvals(catalog):
    instance = _instance(INSTANCE, business_id=NUMBER, status="RUNNING")
    report = await diagnose(Workflow(instance, listed=False))
    assert report["outcome"] == "excluded"
    assert report["record"]["status"] == "RUNNING"
    assert check(report, "status")["status"] == "failed"


@pytest.mark.asyncio
async def test_creation_window_not_trip_dates_explains_missing_old_approval(catalog):
    number = "202607030908000000001"
    instance = replace(
        _instance(INSTANCE, business_id=number), created_at="2026-07-03T09:08:00+08:00"
    )
    report = await diagnose(Workflow(instance, listed=False), identifier=number)
    assert report["outcome"] == "excluded"
    assert "发起时间" in report["summary"]
    assert check(report, "window")["status"] == "failed"
    assert check(report, "dates")["status"] == "passed"


@pytest.mark.asyncio
async def test_unlocated_number_does_not_claim_approval_nonexistence(catalog):
    report = await diagnose(Workflow(listed=False, lookup=False))
    assert report["outcome"] == "not_found"
    assert "不能据此断言" in report["summary"]
    assert report["record"] is None
    assert check(report, "located")["status"] == "unknown"


@pytest.mark.asyncio
async def test_short_lookup_stops_at_page_bound_and_never_claims_target_absence(catalog):
    class BusyMinute(Workflow):
        async def list_process_instance_ids_for_diagnostics(self, **kwargs):
            self.lookup_calls.append(kwargs)
            token = kwargs["next_token"]
            return WorkflowInstanceIdPage(
                tuple(f"synthetic-{index}" for index in range(token, token + 20)),
                token + 20,
            )

    workflow = BusyMinute(listed=False)
    report = await diagnose(workflow, identifier="202608010800000000099")
    assert report["outcome"] == "incomplete"
    assert report["error"]["code"] == "DIAGNOSIS_LOOKUP_LIMIT_EXCEEDED"
    assert report["record"] is None
    assert len(workflow.lookup_calls) == 3
    assert len(workflow.detail_calls) == 60


@pytest.mark.asyncio
async def test_instance_id_with_unconfirmed_source_does_not_claim_source_is_unconfigured(catalog):
    report = await diagnose(Workflow(listed=False, lookup=False), identifier=INSTANCE)
    assert report["identifierKind"] == "instance_id"
    assert check(report, "source")["status"] == "unknown"
    assert check(report, "dates")["status"] == "unknown"
    assert report["record"] is not None


@pytest.mark.asyncio
async def test_result_limit_preserves_unknown_returned_count(catalog):
    workflow = Workflow()
    workflow.normal_ids = tuple(f"synthetic-{index}" for index in range(201))
    report = await diagnose(workflow)
    assert report["outcome"] == "incomplete"
    assert report["error"]["code"] == "TRAVEL_APPROVAL_RESULT_LIMIT_EXCEEDED"
    assert report["counts"]["returned"] is None
    assert workflow.detail_calls == []
    assert workflow.lookup_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (
            DingTalkOpenAPIError._permission_denied(http_status=403, upstream_code="noPermission"),
            "blocked",
        ),
        (
            DingTalkOpenAPIError._rate_limited(
                http_status=429, upstream_code="QpsLimitForAppkeyAndApi"
            ),
            "incomplete",
        ),
    ],
)
async def test_upstream_errors_are_classified_without_becoming_empty_success(
    catalog, error, outcome
):
    report = await diagnose(Workflow(failure=error))
    assert report["outcome"] == outcome
    assert report["error"]["upstreamHttpStatus"] in {403, 429}
    assert report["counts"]["returned"] is None
    assert report["record"] is None


@pytest.mark.asyncio
async def test_timeout_cancels_details_and_does_not_turn_partial_results_into_not_found(
    catalog, monkeypatch
):
    monkeypatch.setattr(approval_diagnostics, "_TIMEOUT_SECONDS", 0.001)
    workflow = Workflow()
    workflow.delay = 1
    report = await diagnose(workflow)
    assert report["outcome"] == "incomplete"
    assert report["error"]["code"] == "DIAGNOSIS_TIMEOUT"
    assert report["counts"]["returned"] is None
    assert workflow.active == 0


@pytest.mark.asyncio
async def test_unexpected_errors_never_return_private_exception_messages(catalog, caplog):
    report = await diagnose(Workflow(failure=RuntimeError("synthetic-private-upstream-body")))
    assert report["outcome"] == "incomplete"
    assert "synthetic-private-upstream-body" not in json.dumps(report)
    assert "synthetic-private-upstream-body" not in caplog.text


@pytest.mark.asyncio
async def test_unconfigured_catalog_explains_blocker_without_making_queries(monkeypatch):
    async def unconfigured(*_args):
        raise ApiError("OA_TEMPLATE_NOT_CONFIGURED", "请先配置模板", 409)

    monkeypatch.setattr(approval_diagnostics, "load_readonly_submission_catalog", unconfigured)
    workflow = Workflow()
    report = await diagnose(workflow)
    assert report["outcome"] == "blocked"
    assert check(report, "catalog")["status"] == "failed"
    assert workflow.list_calls == []


def test_api_requires_session_live_admin_and_csrf(client_factory):
    client = client_factory(
        auth_mock_enabled=True, auth_mock_user_id="admin-1", admin_user_ids="admin-1"
    )
    payload = {"userId": "employee-1", "approvalIdentifier": INSTANCE}
    assert client.post(PATH, json=payload).status_code == 401
    token = client.post("/api/auth/mock").json()["data"]["csrfToken"]
    assert client.post(PATH, json=payload).status_code == 403
    client.app.state.settings.admin_user_ids = ""
    assert client.post(PATH, json=payload, headers={"X-CSRF-Token": token}).status_code == 403


def test_api_returns_noncacheable_private_report_without_logging_inputs(
    client_factory, catalog, caplog
):
    client = client_factory(
        auth_mock_enabled=True, auth_mock_user_id="admin-1", admin_user_ids="admin-1"
    )
    token = client.post("/api/auth/mock").json()["data"]["csrfToken"]
    client.app.state.dingtalk_workflow = Workflow()
    response = client.post(
        PATH,
        json={
            "userId": "employee-1",
            "approvalIdentifier": INSTANCE,
            "from": "2026-08-01",
            "to": "2026-08-31",
        },
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    report = response.json()["data"]
    assert report["outcome"] == "visible"
    assert report["requestId"] == response.headers["X-Request-ID"]
    assert INSTANCE not in response.text and NUMBER not in response.text
    assert "employee-1" not in caplog.text and INSTANCE not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"userId": " "},
        {"approvalIdentifier": "a\nb"},
        {"userId": "a" * 129},
        {"from": "2026-08-01"},
        {"from": "2026-01-01", "to": "2026-12-31"},
        {"processCode": "injected-template"},
        {"identifierKind": "invalid"},
    ],
)
def test_api_rejects_invalid_inputs_before_upstream_queries(client_factory, catalog, changes):
    client = client_factory(
        auth_mock_enabled=True, auth_mock_user_id="admin-1", admin_user_ids="admin-1"
    )
    token = client.post("/api/auth/mock").json()["data"]["csrfToken"]
    workflow = Workflow()
    client.app.state.dingtalk_workflow = workflow
    response = client.post(
        PATH,
        json={"userId": "employee-1", "approvalIdentifier": INSTANCE} | changes,
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 422
    assert workflow.list_calls == []


def test_api_rejects_a_second_diagnosis_while_one_is_running(client_factory):
    client = client_factory(
        auth_mock_enabled=True, auth_mock_user_id="admin-1", admin_user_ids="admin-1"
    )
    token = client.post("/api/auth/mock").json()["data"]["csrfToken"]
    lock = client.app.state.approval_diagnostics_lock
    client.portal.call(lock.acquire)
    try:
        response = client.post(
            PATH,
            json={"userId": "employee-1", "approvalIdentifier": INSTANCE},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DIAGNOSIS_BUSY"
    finally:
        client.portal.call(lock.release)
