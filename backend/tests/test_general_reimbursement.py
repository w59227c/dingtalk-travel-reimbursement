from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import mock_login
from openpyxl import load_workbook
from test_oa_reimbursement_integration import (
    MAPPINGS,
    OA_INSTANCE_ID,
    TRAVEL_INSTANCE_ID,
    TRAVEL_PROCESS_CODE,
    LocalStorageBoundary,
    LocalWorkflowBoundary,
    _component,
    _schema,
    _schemas,
    _worker,
)
from test_reimbursement_files import _upload

from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.integrations.dingtalk.workflow import (
    FormOption,
    RelatedTemplatePolicy,
    WorkflowFormValue,
    WorkflowInstanceIdPage,
)
from app.models.reimbursement import ReimbursementSubmission
from app.services import reimbursement_files
from app.services.general_reimbursement import confirm_general_catalog, refresh_general_catalog
from app.services.oa_reimbursement_payload import parse_snapshot, serialize_snapshot
from app.services.oa_template_profiles import TravelProfileConfirmation, confirm_template_catalog

GENERAL_CODE = "PROC-SYNTHETIC-EXPENSE"
PURCHASE_CODE = "PROC-SYNTHETIC-PURCHASE"
PURCHASE_INSTANCE = "synthetic-purchase-instance"
SUBJECTS = (
    "办公费",
    "交通费",
    "快递费",
    "招待费",
    "通讯费",
    "福利费",
    "咨询费",
    "广告费",
    "租赁费",
    "物业费",
    "水电费",
    "劳务费",
    "会议费",
    "住宿费",
    "其他",
)


class GeneralWorkflow(LocalWorkflowBoundary):
    def __init__(self) -> None:
        reimbursement, travel, self.travel_type = _schemas()
        super().__init__(reimbursement, travel)
        companies = tuple(
            FormOption(value=value, label=label, key=None)
            for value, label in (
                ("synthetic-company", "示例公司"),
                ("synthetic-company-north", "示例华北分公司"),
                ("synthetic-company-east", "示例华东分公司"),
                ("synthetic-company-service", "示例技术服务公司"),
            )
        )
        budgets = tuple(
            FormOption(value=value, label=label, key=None)
            for value, label in (
                ("synthetic-budget", "DEMO-002 日常费用"),
                ("synthetic-budget-office", "DEMO-003 办公采购"),
                ("synthetic-budget-project", "DEMO-004 项目交付"),
                ("synthetic-budget-operations", "DEMO-005 运营支持"),
                ("synthetic-budget-meetings", "DEMO-006 会议与培训"),
                ("synthetic-budget-services", "DEMO-007 技术服务"),
            )
        )
        subjects = tuple(
            FormOption(value=f"synthetic-subject-{i}", label=label, key=None)
            for i, label in enumerate(SUBJECTS)
        )
        self.general = _schema(
            GENERAL_CODE,
            (
                _component("company", "DDSelectField", "报销主体", options=companies),
                _component("budget", "DDSelectField", "预算代码", options=budgets),
                _component("subjects", "DDMultiSelectField", "费用科目", options=subjects),
                _component("description", "TextareaField", "明细说明"),
                _component("amount", "MoneyField", "金额（元）"),
                replace(_component("related", "RelateField", "关联审批单"), required=False),
                _component("attachments", "DDAttachment", "附件"),
            ),
        )
        self.schemas[GENERAL_CODE] = self.general
        self.approval_time = datetime.now(UTC)

    async def list_visible_template_codes(self, user_id):
        assert user_id == "mock-user"
        return (*self.schemas, PURCHASE_CODE), True

    async def list_process_instance_ids(self, *, process_code, next_token, **kwargs):
        if process_code == TRAVEL_PROCESS_CODE:
            page = await super().list_process_instance_ids(
                process_code=process_code,
                next_token=next_token,
                **kwargs,
            )
        elif process_code == PURCHASE_CODE:
            assert kwargs["user_ids"] == ("mock-user",)
            assert kwargs["statuses"] == ("COMPLETED",)
            page = WorkflowInstanceIdPage((PURCHASE_INSTANCE, "synthetic-other-user"), None)
        else:
            return WorkflowInstanceIdPage((), None)
        matching = []
        for instance_id in page.instance_ids:
            instance = await self.get_process_instance(instance_id)
            created_ms = int(datetime.fromisoformat(instance.created_at).timestamp() * 1000)
            if kwargs["start_time"] <= created_ms <= kwargs["end_time"]:
                matching.append(instance_id)
        return WorkflowInstanceIdPage(tuple(matching), page.next_token)

    async def get_process_instance(self, instance_id):
        if instance_id in {PURCHASE_INSTANCE, "synthetic-other-user"}:
            source = await super().get_process_instance(TRAVEL_INSTANCE_ID)
            return replace(
                source,
                instance_id=instance_id,
                title="合成采购审批",
                business_id="SYNTHETIC-PURCHASE",
                status="COMPLETED",
                result="agree",
                created_at=self.approval_time.isoformat(),
                originator_user_id=(
                    "mock-user" if instance_id == PURCHASE_INSTANCE else "synthetic-other-user"
                ),
            )
        source = await super().get_process_instance(instance_id)
        if instance_id == TRAVEL_INSTANCE_ID:
            # Put the demo travel request in the older chunk of the default 180-day range.
            created = self.approval_time - timedelta(days=140)
            return replace(
                source,
                created_at=created.isoformat(),
                finished_at=(created + timedelta(hours=1)).isoformat(),
            )
        return source


async def configure_profiles(app, workflow: GeneralWorkflow) -> None:
    app.state.dingtalk_workflow = workflow
    reimbursement, travel, _ = _schemas()
    with app.state.database_session_factory() as database:
        await confirm_template_catalog(
            database,
            workflow,
            reimbursement_process_code=reimbursement.process_code,
            expected_config_version=None,
            reimbursement_schema_fingerprint=reimbursement.fingerprint,
            reimbursement_mappings=MAPPINGS,
            travel_profiles=[
                TravelProfileConfirmation(
                    profile_key="domestic",
                    display_name="境内出差",
                    process_code=travel.process_code,
                    schema_fingerprint=travel.fingerprint,
                    mappings={"startDate": "travel-start-id", "endDate": "travel-end-id"},
                    travel_type_option=workflow.travel_type,
                )
            ],
            administrator_user_id="mock-user",
        )
        confirm_general_catalog(
            database,
            schema=workflow.general,
            expected_version=0,
            administrator="mock-user",
        )


def test_general_budget_refresh_preserves_the_draft_and_requires_reselection_only_when_removed(
    client_factory,
    monkeypatch,
):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    workflow = GeneralWorkflow()
    asyncio.run(configure_profiles(client.app, workflow))
    root = "/api/reimbursements/drafts"
    initial = client.post(
        root,
        headers=headers,
        json={"kind": "general", "expectedRevision": 0, "input": {"items": []}},
    )
    assert initial.status_code == 201, initial.text
    draft = initial.json()["data"]
    path = f"{root}/{draft['id']}"
    options_path = "/api/oa/reimbursements/options?kind=general"
    budget_value = "synthetic-budget"
    initial_budgets = next(
        c.options for c in workflow.general.components if c.component_id == "budget"
    )

    def set_budgets(budgets):
        workflow.schemas[GENERAL_CODE] = _schema(
            GENERAL_CODE,
            tuple(
                replace(c, options=budgets) if c.component_id == "budget" else c
                for c in workflow.general.components
            ),
        )

    def save(revision, budget=budget_value, description="示例办公用品"):
        return client.put(
            path,
            headers=headers,
            json={
                "expectedRevision": revision,
                "input": {
                    "companyValue": "synthetic-company",
                    "budgetCodeValue": budget,
                    "items": [
                        {
                            "category": "office",
                            "subjectValue": "synthetic-subject-0",
                            "date": "2026-11-10",
                            "displayDate": "11月10日",
                            "amount": "12.50",
                            "description": description,
                        }
                    ],
                },
            },
        )

    saved = save(draft["revision"])
    assert saved.status_code == 200, saved.text
    draft = saved.json()["data"]
    new_budget = FormOption(value="synthetic-new-budget", label="新增预算", key=None)
    new_label = "DEMO-002 日常运营费用"
    set_budgets((replace(initial_budgets[0], label=new_label), *initial_budgets[1:], new_budget))

    async def concurrent_refresh():
        both_reading = asyncio.Event()
        read_count = 0
        get_schema = workflow.get_form_schema

        async def synchronized_read(process_code):
            nonlocal read_count
            read_count += 1
            if read_count == 2:
                both_reading.set()
            await asyncio.wait_for(both_reading.wait(), timeout=2)
            return await get_schema(process_code)

        with monkeypatch.context() as mock:
            mock.setattr(workflow, "get_form_schema", synchronized_read)
            factory = client.app.state.database_session_factory
            return await asyncio.gather(
                *(refresh_general_catalog(factory, workflow) for _ in range(2))
            )

    refreshed = asyncio.run(concurrent_refresh())
    assert all(catalog.config_version == draft["templateConfigVersion"] for catalog in refreshed)
    options = client.get(options_path).json()["data"]
    assert options["budgetCodeOptions"][0]["label"] == new_label
    assert options["budgetCodeOptions"][-1]["value"] == new_budget.value
    resaved = save(draft["revision"])
    assert resaved.status_code == 200, resaved.text
    draft = resaved.json()["data"]
    assert draft["input"]["budgetCodeValue"] == budget_value
    assert draft["input"]["project"]["text"] == new_label

    set_budgets((*initial_budgets[1:], new_budget))
    assert client.get(options_path).status_code == 200
    edited = save(draft["revision"], description="停用后的合成草稿修改")
    assert edited.status_code == 200, edited.text
    draft = edited.json()["data"]
    assert draft["input"]["budgetCodeValue"] == budget_value
    assert draft["input"]["project"]["text"] == new_label
    assert draft["input"]["items"][0]["description"] == "停用后的合成草稿修改"
    assert save(draft["revision"], budget="not-an-option").status_code == 422
    preview = client.post(
        f"{path}/excel-preview", headers=headers, json={"expectedRevision": draft["revision"]}
    )
    assert preview.status_code == 422, preview.text
    assert preview.json()["error"]["code"] == "REIMBURSEMENT_BUDGET_OPTION_INVALID"
    reselected = save(draft["revision"], budget=new_budget.value)
    assert reselected.status_code == 200, reselected.text
    assert reselected.json()["data"]["input"]["project"]["text"] == new_budget.label
    draft = reselected.json()["data"]
    schema = workflow.schemas[GENERAL_CODE]
    workflow.schemas[GENERAL_CODE] = _schema(
        GENERAL_CODE,
        tuple(
            replace(c, options=c.options[1:]) if c.component_id == "subjects" else c
            for c in schema.components
        ),
    )
    assert client.get(options_path).status_code == 200
    preserved = save(draft["revision"], budget=new_budget.value, description="停用科目仍保留草稿")
    assert preserved.status_code == 200, preserved.text
    draft = preserved.json()["data"]
    invalid = client.post(
        f"{path}/review", headers=headers, json={"expectedRevision": draft["revision"]}
    )
    assert (
        invalid.status_code == 409
        and "第 1 笔费用的科目已停用" in invalid.json()["error"]["message"]
    )
    line = draft["input"]["items"][0]
    duplicate = client.put(
        path,
        headers=headers,
        json={
            "expectedRevision": draft["revision"],
            "input": {
                **draft["input"],
                "items": [line, {**line, "description": "不能新增已停用科目"}],
            },
        },
    )
    assert duplicate.status_code == 409


def test_general_template_confirmation_and_excel_share_draft_binding(client_factory, monkeypatch):
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    workflow = GeneralWorkflow()
    asyncio.run(configure_profiles(client.app, workflow))
    root = "/api/reimbursements/drafts"
    draft = client.post(
        root,
        headers=headers,
        json={
            "kind": "general",
            "expectedRevision": 0,
            "input": {"items": []},
        },
    ).json()["data"]
    uploaded = _upload(
        client,
        login["csrfToken"],
        draft["id"],
        revision=draft["revision"],
        name="synthetic-proof.png",
        role="ATTACHMENT_ONLY",
        attachment_kind="other",
    )
    assert uploaded.status_code == 201, uploaded.text
    path = f"{root}/{draft['id']}"
    saved = client.put(
        path,
        headers=headers,
        json={
            "expectedRevision": uploaded.json()["data"]["revision"],
            "input": {
                "companyValue": "synthetic-company",
                "budgetCodeValue": "synthetic-budget",
                "items": [
                    {
                        "category": "office",
                        "subjectValue": "synthetic-subject-0",
                        "date": "2026-11-10",
                        "displayDate": "11月10日",
                        "amount": "12.50",
                        "description": "合成费用",
                    }
                ],
            },
        },
    )
    assert saved.status_code == 200, saved.text
    revision = {"expectedRevision": saved.json()["data"]["revision"]}
    assert client.post(f"{path}/excel-preview", headers=headers, json=revision).status_code == 200

    def confirm(schema, version):
        with client.app.state.database_session_factory() as database:
            return confirm_general_catalog(
                database, schema=schema, expected_version=version, administrator="mock-user"
            ).config_version

    assert confirm(workflow.general, 1) == 1
    renamed = _schema(
        GENERAL_CODE,
        tuple(
            replace(
                component,
                options=(replace(component.options[0], label="预算改名"), *component.options[1:]),
            )
            if component.component_id == "budget"
            else component
            for component in workflow.general.components
        ),
    )
    workflow.schemas[GENERAL_CODE] = renamed
    assert confirm(renamed, 1) == 1
    assert client.post(f"{path}/excel-preview", headers=headers, json=revision).status_code == 200

    workbook_generator = reimbursement_files.generate_expense_workbook
    stopped_budget = _schema(
        GENERAL_CODE,
        tuple(
            replace(component, options=component.options[1:])
            if component.component_id == "budget"
            else component
            for component in renamed.components
        ),
    )

    def stop_budget_during_generation(**kwargs):
        workflow.schemas[GENERAL_CODE] = stopped_budget
        assert confirm(stopped_budget, 1) == 1
        return workbook_generator(**kwargs)

    with monkeypatch.context() as context:
        context.setattr(
            reimbursement_files, "generate_expense_workbook", stop_budget_during_generation
        )
        rejected_preview = client.post(f"{path}/excel-preview", headers=headers, json=revision)
    rejected_review = client.post(f"{path}/review", headers=headers, json=revision)
    rejected_ticket = client.post(f"{path}/excel-preview-ticket", headers=headers, json=revision)
    for response in (rejected_preview, rejected_review, rejected_ticket):
        assert response.status_code == 422, response.headers.get("Content-Type")
        assert response.json()["error"]["code"] == "REIMBURSEMENT_BUDGET_OPTION_INVALID"
    workflow.schemas[GENERAL_CODE] = renamed
    assert confirm(renamed, 1) == 1

    rebound = _schema("PROC-SYNTHETIC-REBIND", renamed.components)
    workflow.schemas[rebound.process_code] = rebound
    assert confirm(rebound, 1) == 2
    for endpoint in ("excel-preview", "review"):
        response = client.post(f"{path}/{endpoint}", headers=headers, json=revision)
        assert response.status_code == 409, response.text[:200]
        assert response.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED"
    related = client.put(
        f"{path}/general-approvals",
        headers=headers,
        json={**revision, "instanceIds": [PURCHASE_INSTANCE]},
    )
    assert related.status_code == 409, related.text
    assert related.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED"
    assert client.get(path).json()["data"]["revision"] == revision["expectedRevision"]


def test_general_approval_removal_works_when_both_saved_approvals_are_unavailable(client_factory):
    class UnavailableApprovals(GeneralWorkflow):
        unavailable = False

        async def get_process_instance(self, instance_id):
            if self.unavailable:
                raise ApiError("SYNTHETIC_APPROVAL_NOT_FOUND", "审批已不可用", 404)
            if instance_id == "second-approval":
                return replace(
                    await super().get_process_instance(PURCHASE_INSTANCE), instance_id=instance_id
                )
            return await super().get_process_instance(instance_id)

    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    workflow = UnavailableApprovals()
    asyncio.run(configure_profiles(client.app, workflow))
    created = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={"kind": "general", "expectedRevision": 0, "input": {"items": []}},
    )
    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    url = f"/api/reimbursements/drafts/{draft['id']}/general-approvals"
    linked = client.put(
        url,
        headers=headers,
        json={
            "expectedRevision": draft["revision"],
            "instanceIds": [PURCHASE_INSTANCE, "second-approval"],
        },
    )
    assert linked.status_code == 200, linked.text
    workflow.unavailable = True
    unchanged = linked.json()["data"]
    remaining = client.put(
        url,
        headers=headers,
        json={"expectedRevision": unchanged["revision"], "instanceIds": ["second-approval"]},
    )
    assert remaining.status_code == 200, remaining.text
    saved = remaining.json()["data"]
    assert saved["input"]["generalApprovalIds"] == ["second-approval"]
    assert (
        saved["input"]["generalApprovalSummaries"]
        == unchanged["input"]["generalApprovalSummaries"][1:]
    )
    assert (
        client.put(
            url,
            headers=headers,
            json={"expectedRevision": unchanged["revision"], "instanceIds": []},
        ).status_code
        == 409
    )
    cleared = client.put(
        url,
        headers=headers,
        json={"expectedRevision": saved["revision"], "instanceIds": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert not cleared.json()["data"]["input"].get("generalApprovalIds")


def test_general_template_rejects_a_restricted_relation_policy(client_factory):
    client = client_factory(auth_mock_enabled=True)
    workflow = GeneralWorkflow()
    restricted = _schema(
        GENERAL_CODE,
        tuple(
            replace(
                component,
                related_template_policy=RelatedTemplatePolicy("RESTRICTED", (PURCHASE_CODE,)),
            )
            if component.component_id == "related"
            else component
            for component in workflow.general.components
        ),
    )
    with client.app.state.database_session_factory() as database:
        with pytest.raises(ApiError, match="关联审批单.*所有审批模板"):
            confirm_general_catalog(
                database, schema=restricted, expected_version=0, administrator="mock-user"
            )


def test_general_approval_search_only_returns_approved_owned_instances(client_factory):
    states = {
        "synthetic-approved": ("COMPLETED", "agree", "mock-user"),
        "synthetic-running": ("RUNNING", "agree", "mock-user"),
        "synthetic-terminated": ("TERMINATED", "agree", "mock-user"),
        "synthetic-refused": ("COMPLETED", "refuse", "mock-user"),
        "synthetic-unknown-result": ("COMPLETED", None, "mock-user"),
        "synthetic-other-owner": ("COMPLETED", "agree", "synthetic-other-user"),
    }

    class MixedWorkflow(GeneralWorkflow):
        requested_statuses = []

        async def list_visible_template_codes(self, user_id):
            return (PURCHASE_CODE,), True

        async def list_process_instance_ids(self, **query):
            self.requested_statuses.append(query["statuses"])
            return WorkflowInstanceIdPage(tuple(states), None)

        async def get_process_instance(self, instance_id):
            source = await super().get_process_instance(PURCHASE_INSTANCE)
            status, result, owner = states[instance_id]
            return replace(
                source,
                instance_id=instance_id,
                status=status,
                result=result,
                originator_user_id=owner,
            )

    client = client_factory(auth_mock_enabled=True)
    mock_login(client)
    workflow = MixedWorkflow()
    client.app.state.dingtalk_workflow = workflow
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    response = client.get("/api/oa/general-approvals", params={"from": today, "to": today})
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert [item["processInstanceId"] for item in page["items"]] == ["synthetic-approved"]
    assert page["complete"] is True
    assert workflow.requested_statuses == [("COMPLETED",)]


@pytest.mark.parametrize(
    "status,result",
    [("RUNNING", None), ("TERMINATED", "agree"), ("COMPLETED", "refuse"), ("COMPLETED", None)],
)
def test_general_approval_selection_rejects_unapproved_instances(client_factory, status, result):
    class UnapprovedWorkflow(GeneralWorkflow):
        async def get_process_instance(self, instance_id):
            source = await super().get_process_instance(instance_id)
            return (
                replace(source, status=status, result=result)
                if instance_id == PURCHASE_INSTANCE
                else source
            )

    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    workflow = UnapprovedWorkflow()
    asyncio.run(configure_profiles(client.app, workflow))
    created = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={"kind": "general", "expectedRevision": 0, "input": {"items": []}},
    )
    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    response = client.put(
        f"/api/reimbursements/drafts/{draft['id']}/general-approvals",
        headers=headers,
        json={"expectedRevision": draft["revision"], "instanceIds": [PURCHASE_INSTANCE]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "OA_GENERAL_APPROVAL_NOT_APPROVED"
    current = client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]
    assert current["revision"] == draft["revision"]
    assert current["input"].get("generalApprovalIds", []) == []


def test_general_rechecks_approved_associations_before_preview_and_submit(client_factory):
    class ChangedWorkflow(GeneralWorkflow):
        revoked = False

        async def get_process_instance(self, instance_id):
            source = await super().get_process_instance(instance_id)
            return (
                replace(source, status="TERMINATED")
                if self.revoked and instance_id == PURCHASE_INSTANCE
                else source
            )

    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    workflow = ChangedWorkflow()
    asyncio.run(configure_profiles(client.app, workflow))
    client.app.state.settings.dingtalk_oa_worker_enabled = True
    created = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={"kind": "general", "expectedRevision": 0, "input": {"items": []}},
    )
    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    uploaded = _upload(
        client,
        login["csrfToken"],
        draft["id"],
        revision=draft["revision"],
        name="synthetic-proof.png",
        role="ATTACHMENT_ONLY",
        attachment_kind="other",
    )
    assert uploaded.status_code == 201, uploaded.text
    path = f"/api/reimbursements/drafts/{draft['id']}"
    saved = client.put(
        path,
        headers=headers,
        json={
            "expectedRevision": uploaded.json()["data"]["revision"],
            "input": {
                "companyValue": "synthetic-company",
                "budgetCodeValue": "synthetic-budget",
                "items": [
                    {
                        "category": "office",
                        "subjectValue": "synthetic-subject-0",
                        "date": "2026-11-10",
                        "amount": "12.50",
                        "description": "合成费用",
                        "displayDate": "11月10日",
                    }
                ],
            },
        },
    )
    assert saved.status_code == 200, saved.text
    associated = client.put(
        f"{path}/general-approvals",
        headers=headers,
        json={
            "expectedRevision": saved.json()["data"]["revision"],
            "instanceIds": [PURCHASE_INSTANCE],
        },
    )
    assert associated.status_code == 200, associated.text
    draft = associated.json()["data"]
    revision = {"expectedRevision": draft["revision"]}
    initial_preview = client.post(f"{path}/excel-preview", headers=headers, json=revision)
    assert initial_preview.status_code == 200, initial_preview.text
    reviewed = client.post(f"{path}/review", headers=headers, json=revision)
    assert reviewed.status_code == 200, reviewed.text
    draft = reviewed.json()["data"]
    revision = {"expectedRevision": draft["revision"]}
    workflow.revoked = True
    for endpoint in ("excel-preview", "excel-preview-ticket"):
        rejected = client.post(f"{path}/{endpoint}", headers=headers, json=revision)
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "OA_GENERAL_APPROVAL_NOT_APPROVED"
    rejected = client.post(
        f"/api/oa/reimbursements/{draft['id']}/submit",
        headers={**headers, "Idempotency-Key": "44000000-0000-4000-8000-000000000000"},
        json=revision,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "OA_GENERAL_APPROVAL_NOT_APPROVED"
    current = client.get(path).json()["data"]
    assert current["revision"] == draft["revision"]
    assert current["status"] == "REVIEW_READY"
    removed = client.put(
        f"{path}/general-approvals", headers=headers, json={**revision, "instanceIds": []}
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["input"].get("generalApprovalIds", []) == []
    assert client.get(f"{path}/files").json()["data"]["items"]


@pytest.mark.parametrize("failure", [None, "directory", "list", "detail"])
def test_general_approval_search_reports_incomplete_stage(client_factory, failure):
    class PartialWorkflow(GeneralWorkflow):
        async def list_visible_template_codes(self, user_id):
            return (PURCHASE_CODE,), failure != "directory"

        async def list_process_instance_ids(self, **query):
            if failure == "list":
                raise DingTalkOpenAPIError(http_status=503)
            return WorkflowInstanceIdPage(
                (PURCHASE_INSTANCE, "synthetic-unavailable")
                if failure == "detail"
                else (PURCHASE_INSTANCE,),
                None,
            )

        async def get_process_instance(self, instance_id):
            if instance_id == "synthetic-unavailable":
                raise DingTalkOpenAPIError(http_status=503)
            source = await super().get_process_instance(instance_id)
            return replace(source, status="COMPLETED", result="agree")

    client = client_factory(auth_mock_enabled=True)
    mock_login(client)
    client.app.state.dingtalk_workflow = PartialWorkflow()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    response = client.get("/api/oa/general-approvals", params={"from": today, "to": today})
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page.get("incompleteReasons", []) == ([failure] if failure else [])
    assert page["complete"] is (failure is None)
    assert [row["processInstanceId"] for row in page["items"]] == (
        [] if failure == "list" else [PURCHASE_INSTANCE]
    )


@pytest.mark.parametrize("directory_partial", [False, True])
def test_general_approval_search_resumes_within_the_same_template_and_day(
    client_factory, directory_partial
):
    class BusyWorkflow(GeneralWorkflow):
        fail_directory_once = False
        directory_reads = 0

        async def list_visible_template_codes(self, user_id):
            if self.fail_directory_once:
                self.fail_directory_once = False
                raise DingTalkOpenAPIError(http_status=429)
            self.directory_reads += 1
            return (PURCHASE_CODE,), not (directory_partial and self.directory_reads == 1)

        async def list_process_instance_ids(self, *, process_code, next_token, **_query):
            if process_code != PURCHASE_CODE:
                return WorkflowInstanceIdPage((), None)
            end = min(next_token + 20, 321)
            return WorkflowInstanceIdPage(
                tuple(f"synthetic-busy-{i}" for i in range(next_token, end)),
                end if end < 321 else None,
            )

        async def get_process_instance(self, instance_id):
            item = await super().get_process_instance(PURCHASE_INSTANCE)
            return replace(
                item,
                instance_id=instance_id,
                title="唯一目标" if instance_id.endswith("-320") else "其他审批",
            )

    client = client_factory(auth_mock_enabled=True)
    mock_login(client)
    workflow = BusyWorkflow()
    client.app.state.dingtalk_workflow = workflow
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    url = "/api/oa/general-approvals"
    params = {"from": today, "to": today, "q": "唯一目标"}
    seen = set()
    for _ in range(10):
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        if page["items"]:
            assert page["items"][0]["processInstanceId"] == "synthetic-busy-320"
            assert page["complete"] is not directory_partial
            assert page["incompleteReasons"] == (["directory"] if directory_partial else [])
            break
        cursor = page.get("nextCursor")
        assert cursor and cursor not in seen
        assert len(cursor) <= 256
        if not seen:
            assert (
                client.get(url, params={**params, "q": "别的标题", "cursor": cursor}).status_code
                == 422
            )
            assert (
                client.get(url, params={**params, "cursor": cursor + "invalid"}).status_code == 422
            )
            workflow.fail_directory_once = True
            retryable = client.get(url, params={**params, "cursor": cursor})
            assert retryable.status_code == 503, retryable.text
            assert (
                retryable.json()["error"]["code"] == "GENERAL_APPROVAL_SEARCH_DIRECTORY_UNAVAILABLE"
            )
        seen.add(cursor)
        params["cursor"] = cursor
    else:
        raise AssertionError("exact-match approval was never reached")


@pytest.mark.parametrize(
    "subject,category,amount,extra",
    [
        (0, "office", "500.01", {}),
        (13, "lodging", "50.00", {}),
        (1, "local_transport", "50.00", {"transportType": "ride_hailing"}),
    ],
)
def test_general_can_preview_missing_supporting_proofs_but_cannot_review(
    client_factory, subject, category, amount, extra
):
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    asyncio.run(configure_profiles(client.app, GeneralWorkflow()))
    root = "/api/reimbursements/drafts"
    created = client.post(
        root,
        headers=headers,
        json={"kind": "general", "expectedRevision": 0, "input": {"items": []}},
    )
    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    uploaded = _upload(
        client,
        headers["X-CSRF-Token"],
        draft["id"],
        revision=draft["revision"],
        role="ATTACHMENT_ONLY",
    )
    assert uploaded.status_code == 201, uploaded.text
    path = f"{root}/{draft['id']}"
    saved = client.put(
        path,
        headers=headers,
        json={
            "expectedRevision": uploaded.json()["data"]["revision"],
            "input": {
                "companyValue": "synthetic-company",
                "budgetCodeValue": "synthetic-budget",
                "items": [
                    {
                        "category": category,
                        "subjectValue": f"synthetic-subject-{subject}",
                        "date": "2026-11-10",
                        "displayDate": "11月10日",
                        "description": "合成预览核对",
                        "amount": amount,
                        "receiptCount": 1,
                        **extra,
                    }
                ],
            },
        },
    )
    assert saved.status_code == 200, saved.text
    revision = {"expectedRevision": saved.json()["data"]["revision"]}
    for response in (
        client.post(path + "/excel-preview", headers=headers, json=revision),
        client.get(path + "/excel-preview", params=revision),
    ):
        assert response.status_code == 200, response.text[:250]
        workbook = load_workbook(io.BytesIO(response.content))
        assert "合成预览核对" in str([cell.value for row in workbook.active for cell in row])
    ticket = client.post(path + "/excel-preview-ticket", headers=headers, json=revision)
    assert ticket.status_code == 200, ticket.text
    native = client.get(
        ticket.json()["data"]["downloadUrl"],
        headers={"X-Reimbursement-Download-Token": ticket.json()["data"]["downloadToken"]},
    )
    assert native.status_code == 200, native.text[:250]
    rejected = client.post(path + "/review", headers=headers, json=revision)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"
    current = client.get(path).json()["data"]
    assert current["status"] == "DRAFT"
    assert current["revision"] == revision["expectedRevision"]
    with client.app.state.database_session_factory() as database:
        assert database.query(ReimbursementSubmission).count() == 0


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "template-rebound",
        "budget-stopped",
        "subject-reclassified-at-lock",
        "subject-reclassified-in-queue",
        "subject-reclassified-after-upload",
        "no-related-approval",
        "empty-related-field",
        "unexpected-approval",
    ],
)
def test_general_reimbursement_separates_workspaces_and_submits_exact_frozen_subjects(
    client_factory,
    monkeypatch,
    scenario,
):
    client = client_factory(auth_mock_enabled=True)
    login = mock_login(client)
    headers = {"X-CSRF-Token": login["csrfToken"]}
    workflow = GeneralWorkflow()
    asyncio.run(configure_profiles(client.app, workflow))
    client.app.state.settings.dingtalk_oa_worker_enabled = True

    def data(response, status=200):
        assert response.status_code == status, response.text
        return response.json()["data"]

    root = "/api/reimbursements/drafts"
    initial = {"expectedRevision": 0, "input": {"items": []}}
    travel = data(client.post(root, headers=headers, json=initial), 201)
    general = data(
        client.post(
            root,
            headers=headers,
            json={
                **initial,
                "kind": "general",
            },
        ),
        201,
    )
    assert general["id"] != travel["id"]
    assert general["kind"] == "general" and travel.get("kind", "travel") == "travel"
    draft = f"{root}/{general['id']}"
    options = data(client.get("/api/oa/reimbursements/options?kind=general"))
    assert len(options["companyOptions"]) == 4
    assert len(options["budgetCodeOptions"]) == 6

    approvals = data(client.get("/api/oa/general-approvals"))
    assert approvals["complete"]
    assert PURCHASE_INSTANCE in {item["processInstanceId"] for item in approvals["items"]}
    assert "synthetic-other-user" not in {item["processInstanceId"] for item in approvals["items"]}
    travel_approvals = data(client.get("/api/oa/travel-approvals"))
    assert [item["processInstanceId"] for item in travel_approvals["items"]] == [TRAVEL_INSTANCE_ID]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    explicit = data(
        client.get(
            "/api/oa/general-approvals",
            params={
                "from": today.isoformat(),
                "to": today.isoformat(),
                "q": "合成采购",
            },
        )
    )
    assert [item["processInstanceId"] for item in explicit["items"]] == [PURCHASE_INSTANCE]
    older = data(
        client.get(
            "/api/oa/general-approvals",
            params={
                "from": yesterday,
                "to": yesterday,
                "q": "合成采购",
            },
        )
    )
    assert older["items"] == []
    assert client.get("/api/oa/general-approvals", params={"from": yesterday}).status_code == 422
    assert (
        client.put(
            f"{draft}/general-approvals",
            headers=headers,
            json={
                "expectedRevision": general["revision"],
                "instanceIds": ["synthetic-other-user"],
            },
        ).status_code
        == 422
    )
    general = data(
        client.put(
            f"{draft}/general-approvals",
            headers=headers,
            json={
                "expectedRevision": general["revision"],
                "instanceIds": [PURCHASE_INSTANCE],
            },
        )
    )
    saved_approvals = general["input"]["generalApprovalSummaries"]
    assert saved_approvals[0]["title"] == "合成采购审批"
    assert data(client.get(f"{draft}/general-approvals")) == saved_approvals
    uploaded = data(
        _upload(
            client,
            login["csrfToken"],
            general["id"],
            revision=general["revision"],
            name="synthetic-waybill.png",
            role="ATTACHMENT_ONLY",
            attachment_kind="waybill",
        ),
        201,
    )
    waybill_id = uploaded["file"]["id"]
    assert data(client.get(f"{root}/{travel['id']}/files"))["items"] == []

    def item(subject, description, amount, **fields):
        return {
            "category": "other",
            "subjectValue": f"synthetic-subject-{subject}",
            "date": "2026-11-10",
            "displayDate": "11月10日",
            "description": description,
            "amount": amount,
            **fields,
        }

    payload = {
        "companyValue": "synthetic-company-east",
        "budgetCodeValue": "synthetic-budget-project",
        "items": [
            item(2, "寄送资料", "30", waybillFileIds=[waybill_id]),
            item(0, "纸张", "45"),
            item(0, "文具", "18"),
        ],
    }
    saved = data(
        client.put(
            draft,
            headers=headers,
            json={
                "expectedRevision": uploaded["revision"],
                "input": payload,
            },
        )
    )
    assert saved["input"]["generalApprovalIds"] == [PURCHASE_INSTANCE]
    assert saved["input"]["generalApprovalSummaries"] == saved_approvals
    assert [row["category"] for row in saved["input"]["items"]] == ["express", "office", "office"]

    for line, message in (
        (item(13, "示例住宿", "50"), "住宿明细"),
        (item(0, "示例设备", "500.01"), "付款凭证"),
        (item(1, "示例网约车", "50", transportType="ride_hailing"), "行程单"),
    ):
        incomplete = data(
            client.put(
                draft,
                headers=headers,
                json={
                    "expectedRevision": saved["revision"],
                    "input": {**payload, "items": [line]},
                },
            )
        )
        rejected = client.post(
            f"{draft}/review",
            headers=headers,
            json={
                "expectedRevision": incomplete["revision"],
            },
        )
        assert rejected.status_code == 409 and message in rejected.json()["error"]["message"]
        saved = data(
            client.put(
                draft,
                headers=headers,
                json={
                    "expectedRevision": incomplete["revision"],
                    "input": payload,
                },
            )
        )

    preview = client.post(
        f"{draft}/excel-preview",
        headers=headers,
        json={
            "expectedRevision": saved["revision"],
        },
    )
    assert preview.status_code == 200, preview.text
    workbook = load_workbook(io.BytesIO(preview.content))
    validation = workbook.worksheets[0].data_validations.dataValidation[0]
    assert validation.formula1 == '"' + ",".join(SUBJECTS) + '"'
    assert "快递费" in {cell for row in workbook.worksheets[0].values for cell in row}

    without_approval = scenario in {
        "no-related-approval",
        "empty-related-field",
        "unexpected-approval",
    }
    if without_approval:
        saved = data(
            client.put(
                f"{draft}/general-approvals",
                headers=headers,
                json={"expectedRevision": saved["revision"], "instanceIds": []},
            )
        )
    reviewed = data(
        client.post(
            f"{draft}/review",
            headers=headers,
            json={
                "expectedRevision": saved["revision"],
            },
        )
    )
    labels = {"办公费": "住宿费", "住宿费": "办公费"}
    reclassified = _schema(
        GENERAL_CODE,
        tuple(
            replace(
                component,
                options=tuple(
                    replace(option, label=labels.get(option.label, option.label))
                    for option in component.options
                ),
            )
            if component.component_id == "subjects"
            else component
            for component in workflow.general.components
        ),
    )
    lock_failures = {
        "template-rebound": (409, "REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED"),
        "budget-stopped": (422, "REIMBURSEMENT_BUDGET_OPTION_INVALID"),
        "subject-reclassified-at-lock": (409, "REIMBURSEMENT_SUBJECT_CATEGORY_CHANGED"),
    }
    if scenario in lock_failures:
        if scenario == "template-rebound":
            new_schema = _schema(
                "PROC-SYNTHETIC-REBOUND-DURING-SUBMIT", workflow.general.components
            )
            expected_version = 2
        elif scenario == "budget-stopped":
            new_schema = _schema(
                GENERAL_CODE,
                tuple(
                    replace(
                        component,
                        options=tuple(
                            option
                            for option in component.options
                            if option.value != "synthetic-budget-project"
                        ),
                    )
                    if component.component_id == "budget"
                    else component
                    for component in workflow.general.components
                ),
            )
            expected_version = 1
        else:
            new_schema = reclassified
            expected_version = 1
        read_approval = workflow.get_process_instance

        async def rebind_and_read(instance_id):
            workflow.schemas[new_schema.process_code] = new_schema
            with client.app.state.database_session_factory() as database:
                confirmed = confirm_general_catalog(
                    database, schema=new_schema, expected_version=1, administrator="mock-user"
                )
                assert confirmed.config_version == expected_version
            return await read_approval(instance_id)

        monkeypatch.setattr(workflow, "get_process_instance", rebind_and_read)
    result = client.post(
        f"/api/oa/reimbursements/{general['id']}/submit",
        headers={**headers, "Idempotency-Key": "42000000-0000-4000-8000-000000000000"},
        json={"expectedRevision": reviewed["revision"]},
    )
    if scenario in lock_failures:
        status, code = lock_failures[scenario]
        assert result.status_code == status, result.text
        assert result.json()["error"]["code"] == code
        stored = data(client.get(draft))
        assert stored["revision"] == reviewed["revision"]
        assert stored["status"] == "REVIEW_READY"
        with client.app.state.database_session_factory() as database:
            assert database.query(ReimbursementSubmission).count() == 0
        if scenario == "subject-reclassified-at-lock":
            retry = client.post(
                f"{draft}/review", headers=headers, json={"expectedRevision": stored["revision"]}
            )
            assert retry.status_code == 409
            assert "住宿明细" in retry.json()["error"]["message"]
        assert workflow.create_calls == 0
        return
    submitted = data(result, 202)
    if scenario in {"empty-related-field", "unexpected-approval"}:
        read_instance = workflow.get_process_instance

        async def read_with_optional_relation(instance_id):
            instance = await read_instance(instance_id)
            if instance_id != OA_INSTANCE_ID:
                return instance
            return replace(
                instance,
                form_values=(
                    *instance.form_values,
                    WorkflowFormValue(
                        component_id="related",
                        name="关联审批单",
                        component_type="RelateField",
                        value="[]",
                        ext_value='{"list":[{"procInstId":"unexpected-approval"}]}'
                        if scenario == "unexpected-approval"
                        else '{"list":[]}',
                        biz_alias=None,
                    ),
                ),
            )

        monkeypatch.setattr(workflow, "get_process_instance", read_with_optional_relation)
    storage = LocalStorageBoundary()
    if scenario == "subject-reclassified-in-queue":
        workflow.schemas[GENERAL_CODE] = reclassified
    elif scenario == "subject-reclassified-after-upload":
        commit_upload = storage.commit_prepared_upload

        async def commit_and_reclassify(*, prepared):
            attachment = await commit_upload(prepared=prepared)
            workflow.schemas[GENERAL_CODE] = reclassified
            return attachment

        monkeypatch.setattr(storage, "commit_prepared_upload", commit_and_reclassify)
    assert asyncio.run(_worker(client, workflow, storage).run_once())
    polled = data(client.get(f"/api/oa/reimbursements/submissions/{submitted['submissionId']}"))
    if scenario in {"subject-reclassified-in-queue", "subject-reclassified-after-upload"}:
        assert polled["status"] == "FAILED_FINAL", polled
        assert polled["error"]["code"] == "REIMBURSEMENT_SUBJECT_CATEGORY_CHANGED"
        assert workflow.create_calls == 0
        if scenario == "subject-reclassified-after-upload":
            assert len(storage.recycled) == len(storage.committed) == 2
        else:
            assert not storage.committed
        return
    if scenario == "unexpected-approval":
        assert polled["status"] == "MANUAL_REVIEW", polled
        assert polled["error"]["code"] == "OA_READBACK_MISMATCH"
        assert workflow.create_calls == 1
        return
    assert polled["status"] == "SUBMITTED", polled
    assert workflow.create_calls == 1
    command = workflow.created_command
    assert command.process_code == GENERAL_CODE
    values = {field.component_id: field.value for field in command.form_values}
    assert values["company"] == "synthetic-company-east"
    assert values["budget"] == "synthetic-budget-project"
    assert json.loads(values["subjects"]) == ["synthetic-subject-0", "synthetic-subject-2"]
    if without_approval:
        assert "related" not in values
    else:
        assert json.loads(values["related"]) == [PURCHASE_INSTANCE]
    assert values["description"].splitlines() == [
        "2026-11-10 办公费 纸张：45.00",
        "2026-11-10 办公费 文具：18.00",
        "2026-11-10 快递费 寄送资料：30.00",
    ]
    with client.app.state.database_session_factory() as database:
        frozen = database.get(ReimbursementSubmission, submitted["submissionId"])
        snapshot = parse_snapshot(frozen.form_snapshot_json, expected_sha256=frozen.snapshot_sha256)
    assert "generalReason" not in json.loads(frozen.form_snapshot_json)["input"]
    legacy_reason = "合成历史说明"
    legacy = snapshot.model_copy(
        update={
            "input": snapshot.input.model_copy(update={"general_reason": legacy_reason}),
            "form_values": tuple(
                field.model_copy(update={"value": f"{legacy_reason}\n{field.value}"})
                if field.logical_key == "description"
                else field
                for field in snapshot.form_values
            ),
        }
    )
    assert parse_snapshot(serialize_snapshot(legacy)) == legacy
    assert [entry.file_type for entry in storage.committed] == ["pdf", "xlsx"]
    assert data(client.get(f"{root}/{travel['id']}"))["revision"] == travel["revision"]
