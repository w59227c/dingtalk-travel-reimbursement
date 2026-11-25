from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import (
    WorkflowApprovalSummary,
    WorkflowInstanceIdPage,
    WorkflowVisibleTemplate,
)
from app.services.general_approvals import find_general_approvals


class SearchWorkflow:
    """Synthetic upstream with enough records to require a signed continuation."""

    def __init__(self):
        self.templates = (WorkflowVisibleTemplate("PROC-A", "合成采购审批"),)
        self.list_queries = []
        self.active_details = 0
        self.peak_details = 0
        self.details_started = asyncio.Event()
        self.detail_gate = None

    async def list_enterprise_templates(self):
        return self.templates, True

    async def list_process_instance_ids(self, **query):
        self.list_queries.append(query)
        end = min(query["next_token"] + query["max_results"], 121)
        return WorkflowInstanceIdPage(
            tuple(f"synthetic-{index}" for index in range(query["next_token"], end)),
            end if end < 121 else None,
        )

    async def get_process_instance_summary(self, instance_id):
        self.active_details += 1
        self.peak_details = max(self.peak_details, self.active_details)
        if self.active_details == 4:
            self.details_started.set()
        try:
            if self.detail_gate is not None:
                await self.detail_gate.wait()
            else:
                await asyncio.sleep(0)
            return WorkflowApprovalSummary(
                instance_id=instance_id,
                title="合成采购审批",
                business_id=f"business-{instance_id}",
                originator_user_id="synthetic-employee",
                status="RUNNING",
                result=None,
                created_at="2026-11-25T10:00:00+08:00",
            )
        finally:
            self.active_details -= 1


def search_options():
    return {
        "user_id": "synthetic-employee",
        "date_range": (date(2026, 11, 25), date(2026, 11, 25)),
        "keyword": "",
        "session_id": "synthetic-session",
        "secret": "synthetic-secret",
    }


@pytest.mark.asyncio
async def test_search_continues_without_database_or_template_configuration():
    workflow = SearchWorkflow()
    first = await find_general_approvals(workflow, **search_options())
    assert first["complete"] is False
    assert len(first["items"]) == 120
    cursor = first["nextCursor"]
    second = await find_general_approvals(workflow, **search_options(), cursor=cursor)
    ids = [row["processInstanceId"] for row in first["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 121
    assert second["complete"] is True
    assert "nextCursor" not in second
    assert second["incompleteReasons"] == []
    assert second["templates"] == [{"processCode": "PROC-A", "name": "合成采购审批"}]
    assert workflow.list_queries[-1]["next_token"] == 120
    assert workflow.peak_details == 4
    assert all(query["user_ids"] == ("synthetic-employee",) for query in workflow.list_queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    ["session", "employee", "dates", "keyword", "category", "directory", "secret", "signature"],
)
async def test_continuation_cannot_cross_its_original_scope(changed):
    workflow = SearchWorkflow()
    page = await find_general_approvals(workflow, **search_options())
    cursor = page["nextCursor"]
    options = search_options()
    if changed == "session":
        options["session_id"] = "another-session"
    elif changed == "employee":
        options["user_id"] = "another-employee"
    elif changed == "dates":
        options["date_range"] = (date(2026, 11, 24), date(2026, 11, 25))
    elif changed == "keyword":
        options["keyword"] = "采购"
    elif changed == "category":
        options["process_code"] = "PROC-A"
    elif changed == "directory":
        workflow.templates += (WorkflowVisibleTemplate("PROC-B", "合成外出审批"),)
    elif changed == "secret":
        options["secret"] = "another-secret"
    else:
        cursor += "tampered"
    previous_queries = len(workflow.list_queries)
    with pytest.raises(ApiError) as failure:
        await find_general_approvals(workflow, **options, cursor=cursor)
    assert failure.value.code == "GENERAL_APPROVAL_SEARCH_CURSOR_INVALID"
    assert len(workflow.list_queries) == previous_queries


@pytest.mark.asyncio
async def test_canceling_search_cancels_all_pending_detail_reads():
    workflow = SearchWorkflow()
    workflow.detail_gate = asyncio.Event()
    task = asyncio.create_task(find_general_approvals(workflow, **search_options()))
    try:
        await asyncio.wait_for(workflow.details_started.wait(), timeout=2)
        assert workflow.active_details == 4
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert workflow.active_details == 0
    assert workflow.peak_details == 4


@pytest.mark.asyncio
async def test_continuation_issued_before_module_extraction_remains_valid():
    # Issued by the previous implementation with search_options() and synthetic fixtures.
    cursor = (
        "WzAsIDAsIDEyMCwgZmFsc2UsIFtdXQ."
        "71589cf8f5270ae2a645cf0afca5f4a3f5300cc0e23a567871e1b17c54a019dd"
    )
    workflow = SearchWorkflow()
    page = await find_general_approvals(workflow, **search_options(), cursor=cursor)
    assert page["complete"] is True
    assert [item["processInstanceId"] for item in page["items"]] == ["synthetic-120"]
    assert workflow.list_queries[0]["next_token"] == 120
