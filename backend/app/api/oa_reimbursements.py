from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.schemas.common import success
from app.services.oa_template_profiles import load_fresh_submission_catalog
from app.services.sessions import CurrentSession, get_current_session
from app.services.travel_approvals import (
    list_current_user_travel_approvals,
    requested_query_window,
    runtime_options,
)

router = APIRouter(tags=["oa-reimbursements"])


@router.get("/oa/reimbursements/options")
async def get_reimbursement_options(
    request: Request,
    _current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    catalog = await load_fresh_submission_catalog(
        request.app.state.database_session_factory,
        request.app.state.dingtalk_workflow,
    )
    return success(runtime_options(catalog))


@router.get("/oa/travel-approvals")
async def get_current_user_travel_approvals(
    request: Request,
    current: Annotated[CurrentSession, Depends(get_current_session)],
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    query: Annotated[str, Query(alias="q", max_length=100)] = "",
) -> dict[str, object]:
    query_window = requested_query_window(from_date, to_date)
    catalog = await load_fresh_submission_catalog(
        request.app.state.database_session_factory,
        request.app.state.dingtalk_workflow,
    )
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    current_user_id = current.record.dingtalk_user_id
    candidates = await list_current_user_travel_approvals(
        workflow,
        catalog,
        current_user_id=current_user_id,
        query_window=query_window,
        query=query,
    )
    return success(
        {
            "templateConfigVersion": catalog.config_version,
            "queryWindow": query_window.as_dict(),
            "items": [candidate.as_dict() for candidate in candidates],
        }
    )
