from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.errors import ApiError
from app.core.request_id import request_id_for
from app.core.runtime_events import emit_runtime_event
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.schemas.common import success
from app.services.oa_template_profiles import load_fresh_submission_catalog
from app.services.sessions import CurrentSession, get_current_session
from app.services.travel_approvals import (
    TravelApprovalQueryObservation,
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
    observation = TravelApprovalQueryObservation()
    outcome = "incomplete"
    error_code = None
    failure_kind = None
    query_window = None
    config_version = None
    try:
        query_window = requested_query_window(from_date, to_date)
        catalog = await load_fresh_submission_catalog(
            request.app.state.database_session_factory,
            request.app.state.dingtalk_workflow,
        )
        config_version = catalog.config_version
        workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
        candidates = await list_current_user_travel_approvals(
            workflow,
            catalog,
            current_user_id=current.record.dingtalk_user_id,
            query_window=query_window,
            query=query,
            observation=observation,
        )
        outcome = "success" if candidates else "empty"
        return success(
            {
                "templateConfigVersion": catalog.config_version,
                "queryWindow": query_window.as_dict(),
                "items": [candidate.as_dict() for candidate in candidates],
                "requestId": request_id_for(request),
            }
        )
    except ApiError as error:
        error_code = error.code
        failure_kind = getattr(error, "failure_kind", None)
        outcome = (
            "incomplete"
            if failure_kind == "timeout"
            or error.code in {"TRAVEL_APPROVAL_RESULT_LIMIT_EXCEEDED", "DINGTALK_RATE_LIMITED"}
            else "failed"
        )
        raise
    finally:
        emit_runtime_event(
            "approvals",
            "approval_query",
            outcome,
            **observation.as_dict(),
            configVersion=config_version,
            keywordPresent=bool(query.strip()),
            errorCode=error_code,
            failureKind=failure_kind,
            queryWindow=query_window.as_dict() if query_window else None,
        )
