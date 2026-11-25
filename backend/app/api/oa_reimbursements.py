from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.models.reimbursement import ReimbursementKind
from app.schemas.common import success
from app.services.attendance_calendar import attendance_calendar
from app.services.general_reimbursement import (
    find_general_approvals,
    general_approval_date_range,
    general_options,
    get_general_catalog,
    refresh_general_catalog,
    subject_category,
)
from app.services.oa_template_profiles import (
    load_fresh_submission_catalog,
    require_submission_ready_catalog,
)
from app.services.reimbursement_context import (
    get_reimbursement_session,
    require_live_reimbursement_actor,
)
from app.services.sessions import CurrentSession
from app.services.travel_approvals import (
    list_current_user_travel_approvals,
    requested_query_window,
    runtime_options,
)

router = APIRouter(tags=["oa-reimbursements"])


@router.get("/oa/reimbursements/options")
async def get_reimbursement_options(
    request: Request,
    _current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
    kind: ReimbursementKind = ReimbursementKind.TRAVEL,
) -> dict[str, object]:
    if kind == "general" and _current.beneficiary is not None:
        raise ApiError("OA_GENERAL_SELF_ONLY", "普通费用暂只支持本人报销", 403)
    refresh_catalog = (
        refresh_general_catalog if kind == "general" else load_fresh_submission_catalog
    )
    catalog = await refresh_catalog(
        request.app.state.database_session_factory, request.app.state.dingtalk_workflow
    )
    if catalog.is_general:
        return success(
            {
                "templateConfigVersion": catalog.config_version,
                "schemaFingerprint": catalog.reimbursement.schema.fingerprint,
                "reimbursementProcessCode": catalog.reimbursement.process_code,
                "companyOptions": [item.as_dict() for item in general_options(catalog, "company")],
                "budgetCodeOptions": [
                    item.as_dict() for item in general_options(catalog, "budgetCode")
                ],
                "subjectOptions": [
                    {**item.as_dict(), "categoryId": subject_category(item.label)}
                    for item in general_options(catalog, "subjects")
                ],
                "travelTypeOptions": [],
                "travelProfiles": [],
            }
        )
    with request.app.state.database_session_factory() as database:
        calendar = attendance_calendar(database)
    return success({**runtime_options(catalog), "attendanceCalendar": calendar})


@router.get("/oa/general-approvals")
async def get_current_user_general_approvals(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
    days: Annotated[int, Query(ge=1, le=365)] = 183,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    query: Annotated[str, Query(alias="q", max_length=100)] = "",
    process_code: Annotated[str, Query(alias="processCode", max_length=256)] = "",
    cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
) -> dict[str, object]:
    if current.beneficiary is not None:
        raise ApiError("OA_GENERAL_SELF_ONLY", "普通费用暂只支持本人报销", 403)
    date_range = general_approval_date_range(from_date, to_date, days=days)
    session_id = current.record.session_id_hash
    user_id = current.reimbursement_user_id
    known_codes = []
    known_names = {}
    for kind in ("general", "travel"):
        try:
            catalog = (
                get_general_catalog if kind == "general" else require_submission_ready_catalog
            )(database)
        except ApiError:
            continue
        known_codes.extend(
            (catalog.reimbursement.process_code, *catalog.allowed_travel_process_codes)
        )
        known_names[catalog.reimbursement.process_code] = catalog.reimbursement.schema.template_name
        known_names.update(
            {item.process_code: item.schema.template_name for item in catalog.travel_profiles}
        )
    database.rollback()
    result = await find_general_approvals(
        request.app.state.dingtalk_workflow,
        user_id=user_id,
        date_range=date_range,
        keyword=query.strip(),
        session_id=session_id,
        secret=request.app.state.settings.session_secret,
        cursor=cursor,
        known_codes=tuple(known_codes),
        known_names=known_names,
        process_code=process_code.strip(),
    )
    require_live_reimbursement_actor(database, current, session_id)
    return success(result)


@router.get("/oa/travel-approvals")
async def get_current_user_travel_approvals(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    query: Annotated[str, Query(alias="q", max_length=100)] = "",
) -> dict[str, object]:
    query_window = requested_query_window(from_date, to_date)
    session_id_hash = current.record.session_id_hash
    current_user_id = current.reimbursement_user_id
    operator_id = current.reimbursement_operator_user_id
    database.rollback()
    catalog = await load_fresh_submission_catalog(
        request.app.state.database_session_factory,
        request.app.state.dingtalk_workflow,
    )
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    candidates = await list_current_user_travel_approvals(
        workflow,
        catalog,
        current_user_id=current_user_id,
        query_window=query_window,
        query=query,
        required_cc_user_id=operator_id,
    )
    require_live_reimbursement_actor(database, current, session_id_hash)
    calendar = attendance_calendar(database)
    return success(
        {
            "templateConfigVersion": catalog.config_version,
            "reimbursementOptions": {**runtime_options(catalog), "attendanceCalendar": calendar},
            "queryWindow": query_window.as_dict(),
            "items": [candidate.as_dict() for candidate in candidates],
        }
    )
