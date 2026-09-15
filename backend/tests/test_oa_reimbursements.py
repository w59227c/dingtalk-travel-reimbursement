from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import oa_reimbursements
from app.core.errors import ApiError, install_error_handlers
from app.database.session import get_db
from app.integrations.dingtalk.workflow import (
    FormOption,
    WorkflowFormValue,
    WorkflowInstanceIdPage,
    WorkflowProcessInstance,
)
from app.services.sessions import get_current_session


def _profile():
    return SimpleNamespace(
        profile_key="domestic",
        display_name="境内出差",
        process_code="PROC-DOMESTIC",
        schema=SimpleNamespace(fingerprint="d" * 64),
        start_date_component_id="travel-start",
        end_date_component_id="travel-end",
        travel_type_option=FormOption(
            value="市外项目出差（短期）",
            label="市外项目出差（短期）",
            key="travel-domestic",
        ),
    )


def _catalog():
    return SimpleNamespace(
        config_version=12,
        reimbursement=SimpleNamespace(
            process_code="PROC-REIMBURSEMENT",
            mappings={"company": "company", "budgetCode": "budget"},
            schema=SimpleNamespace(
                components=(
                    SimpleNamespace(
                        component_id="company",
                        options=(
                            FormOption(
                                value="北京",
                                label="北京公司",
                                key="company-beijing",
                            ),
                        ),
                    ),
                    SimpleNamespace(
                        component_id="budget",
                        options=(
                            FormOption(
                                value="26007",
                                label="MES 项目",
                                key="budget-26007",
                            ),
                        ),
                    ),
                )
            ),
        ),
        travel_profiles=(_profile(),),
    )


def _instance(instance_id: str) -> WorkflowProcessInstance:
    return WorkflowProcessInstance(
        instance_id=instance_id,
        title="本人 TARGET 境内出差申请",
        business_id="TARGET-2026-001",
        originator_user_id="employee-1",
        originator_department_id="100",
        status="COMPLETED",
        result="agree",
        created_at="2026-08-01T08:00:00+08:00",
        finished_at="2026-08-02T08:00:00+08:00",
        form_values=(
            WorkflowFormValue(
                component_id="travel-start",
                name="开始日期",
                component_type="DDDateField",
                value="2026-08-10",
                ext_value=None,
                biz_alias=None,
            ),
            WorkflowFormValue(
                component_id="travel-end",
                name="结束日期",
                component_type="DDDateField",
                value="2026-08-12",
                ext_value=None,
                biz_alias=None,
            ),
        ),
    )


class FakeWorkflow:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.detail_calls: list[str] = []
        self.database = None

    async def list_process_instance_ids(self, **kwargs):
        assert self.database is not None
        assert self.database.rollback_calls == 1
        self.list_calls.append(kwargs)
        return WorkflowInstanceIdPage(
            ("instance-1",) if len(self.list_calls) == 1 else (),
            None,
        )

    async def get_process_instance(self, instance_id: str):
        self.detail_calls.append(instance_id)
        return _instance(instance_id)


def _client(monkeypatch, *, catalog=None, workflow=None) -> TestClient:
    application = FastAPI()
    install_error_handlers(application)
    application.include_router(oa_reimbursements.router, prefix="/api")
    selected_workflow = workflow or FakeWorkflow()
    application.state.dingtalk_workflow = selected_workflow

    class FakeDatabase:
        rollback_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1

    database = FakeDatabase()
    selected_workflow.database = database

    def database_dependency():
        yield database

    application.dependency_overrides[get_db] = database_dependency
    application.dependency_overrides[get_current_session] = lambda: SimpleNamespace(
        record=SimpleNamespace(dingtalk_user_id="employee-1")
    )

    def ready_catalog(received_database):
        assert received_database is database
        return catalog or _catalog()

    monkeypatch.setattr(
        oa_reimbursements,
        "require_submission_ready_catalog",
        ready_catalog,
    )
    return TestClient(application)


def test_main_application_registers_reimbursement_routes(client_factory) -> None:
    client = client_factory(auth_mock_enabled=True)

    assert client.get("/api/oa/reimbursements/options").status_code == 401
    assert client.get("/api/oa/travel-approvals").status_code == 401


def test_options_returns_only_confirmed_exact_catalog_options(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        response = client.get("/api/oa/reimbursements/options")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "templateConfigVersion": 12,
        "reimbursementProcessCode": "PROC-REIMBURSEMENT",
        "companyOptions": [{"value": "北京", "label": "北京公司", "key": "company-beijing"}],
        "budgetCodeOptions": [{"value": "26007", "label": "MES 项目", "key": "budget-26007"}],
        "travelProfiles": [
            {
                "profileKey": "domestic",
                "displayName": "境内出差",
                "processCode": "PROC-DOMESTIC",
                "schemaFingerprint": "d" * 64,
                "travelTypeOption": {
                    "value": "市外项目出差（短期）",
                    "label": "市外项目出差（短期）",
                    "key": "travel-domestic",
                },
                "subsidyTripType": "project",
            }
        ],
    }


def test_travel_list_ignores_claimed_identity_and_process_code(monkeypatch) -> None:
    workflow = FakeWorkflow()
    with _client(monkeypatch, workflow=workflow) as client:
        response = client.get(
            "/api/oa/travel-approvals",
            params={
                "from": "2026-05-01",
                "to": "2026-08-28",
                "q": "target",
                "userId": "attacker",
                "processCode": "PROC-EVIL",
            },
        )

    assert response.status_code == 200
    assert len(workflow.list_calls) == 1
    assert workflow.list_calls[0]["process_code"] == "PROC-DOMESTIC"
    assert workflow.list_calls[0]["user_ids"] == ("employee-1",)
    assert workflow.list_calls[0]["statuses"] == ("COMPLETED",)
    assert set(workflow.list_calls[0]) == {
        "process_code",
        "start_time",
        "end_time",
        "next_token",
        "max_results",
        "user_ids",
        "statuses",
    }
    assert response.json()["data"]["items"] == [
        {
            "processInstanceId": "instance-1",
            "profileKey": "domestic",
            "profileDisplayName": "境内出差",
            "sourceProcessCode": "PROC-DOMESTIC",
            "travelTypeOption": {
                "value": "市外项目出差（短期）",
                "label": "市外项目出差（短期）",
                "key": "travel-domestic",
            },
            "subsidyTripType": "project",
            "title": "本人 TARGET 境内出差申请",
            "businessId": "TARGET-2026-001",
            "originatorDepartmentId": "100",
            "startDate": "2026-08-10",
            "endDate": "2026-08-12",
            "createdAt": "2026-08-01T08:00:00+08:00",
            "finishedAt": "2026-08-02T08:00:00+08:00",
            "companyOption": None,
            "budgetCodeOption": None,
            "unavailableReason": "出差模板尚未配置所属公司来源，请联系管理员",
        }
    ]


def test_travel_list_defaults_to_latest_180_calendar_dates(monkeypatch) -> None:
    workflow = FakeWorkflow()
    with _client(monkeypatch, workflow=workflow) as client:
        response = client.get("/api/oa/travel-approvals")

    assert response.status_code == 200
    query_window = response.json()["data"]["queryWindow"]
    assert (
        date.fromisoformat(query_window["to"]) - date.fromisoformat(query_window["from"])
    ).days == 179


def test_invalid_window_is_rejected_before_workflow(monkeypatch) -> None:
    workflow = FakeWorkflow()
    with _client(monkeypatch, workflow=workflow) as client:
        missing_endpoint = client.get(
            "/api/oa/travel-approvals",
            params={"from": "2026-08-01"},
        )
        too_large = client.get(
            "/api/oa/travel-approvals",
            params={"from": "2026-03-01", "to": "2026-09-01"},
        )

    assert missing_endpoint.status_code == 422
    assert too_large.status_code == 422
    assert workflow.list_calls == []


def test_catalog_readiness_failure_prevents_workflow_calls(monkeypatch) -> None:
    workflow = FakeWorkflow()
    client = _client(monkeypatch, workflow=workflow)

    def not_ready(_database):
        raise ApiError(
            "OA_TEMPLATE_CONFIRMATION_REQUIRED",
            "审批模板配置需要重新确认",
            409,
        )

    monkeypatch.setattr(
        oa_reimbursements,
        "require_submission_ready_catalog",
        not_ready,
    )
    with client:
        response = client.get("/api/oa/travel-approvals")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OA_TEMPLATE_CONFIRMATION_REQUIRED"
    assert workflow.list_calls == []
    assert workflow.detail_calls == []
