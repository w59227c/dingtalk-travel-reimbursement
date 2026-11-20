from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftRelatedApproval,
    ReimbursementKind,
    utc_now,
)
from app.schemas.common import success
from app.schemas.reimbursements import (
    CorrectDraftDepartmentRequest,
    CreateReimbursementDraftRequest,
    DraftRevisionRequest,
    OpenWorkspaceRequest,
    ReplaceGeneralApprovalsRequest,
    ReplaceRelatedApprovalsRequest,
    UpdateReimbursementDraftRequest,
)
from app.services.dingtalk import DepartmentIdentity
from app.services.oa_template_profiles import require_submission_ready_catalog
from app.services.reimbursement_context import (
    REIMBURSEMENT_CONTEXT_HEADER,
    get_reimbursement_session,
    require_reimbursement_csrf,
    restore_reimbursement_context,
    with_reimbursement_department,
)
from app.services.reimbursement_drafts import (
    CatalogBinding,
    _store_related_approvals,
    _workspace_drafts,
    create_reimbursement_draft,
    draft_actor,
    get_reimbursement_draft,
    history_draft_actor,
    list_reimbursement_drafts,
    list_reimbursement_submission_history,
    mark_reimbursement_draft_review_ready,
    read_general_approval_summaries,
    replace_general_approvals,
    replace_related_approvals,
    require_owned_draft,
    update_reimbursement_draft,
)
from app.services.reimbursement_files import map_reimbursement_storage_error
from app.services.reimbursement_quota import ReimbursementQuotaCoordinator, ReimbursementQuotaError
from app.services.reimbursement_recovery import (
    recover_failed_reimbursement,
    workspace_reimbursements,
)
from app.services.reimbursement_staging import ReimbursementStagingError
from app.services.reimbursement_workspaces import (
    actor_for_draft,
    correct_general_department,
    open_workspace,
    workspace_actor,
)
from app.services.sessions import CurrentSession, get_current_session
from app.services.travel_approvals import (
    TravelApprovalQueryWindow,
    TravelApprovalSelection,
    reverify_travel_approval_selection,
)

router = APIRouter(tags=["reimbursement-drafts"])


@router.post("/reimbursements/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(
    body: CreateReimbursementDraftRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    created = create_reimbursement_draft(
        database,
        actor=draft_actor(current),
        departments=current.reimbursement_departments,
        expected_department_id=body.expected_department_id,
        expected_department_name=body.expected_department_name,
        draft_input=body.input,
        ttl_days=request.app.state.settings.reimbursement_draft_ttl_days,
        max_items=request.app.state.settings.expense_max_items,
        reuse_existing=body.reuse_existing,
        replaces_draft_id=body.replaces_draft_id,
        replaces_draft_revision=body.replaces_draft_revision,
        kind=body.kind,
    )
    if body.replaces_draft_id:
        background_tasks.add_task(
            request.app.state.cleanup_retired_reimbursements,
            body.replaces_draft_id,
        )
    return success(created)


@router.get("/reimbursements/drafts")
def list_drafts(
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    kind: ReimbursementKind = ReimbursementKind.TRAVEL,
    expected_id: Annotated[str | None, Query(alias="expectedDepartmentId", max_length=128)] = None,
    expected_name: Annotated[
        str | None, Query(alias="expectedDepartmentName", max_length=255)
    ] = None,
) -> dict[str, object]:
    return success(
        list_reimbursement_drafts(
            database,
            actor=draft_actor(current),
            offset=offset,
            limit=limit,
            kind=kind,
        )
    )


@router.get("/reimbursements/submission-history")
def list_submission_history(
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
    kind: ReimbursementKind | None = None,
) -> dict[str, object]:
    return success(
        list_reimbursement_submission_history(
            database,
            corp_id=current.record.corp_id,
            user_id=current.record.dingtalk_user_id,
            kind=kind,
        )
    )


@router.get("/reimbursements/workspace")
def get_workspace(
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
    kind: ReimbursementKind = ReimbursementKind.TRAVEL,
    expected_id: Annotated[str | None, Query(alias="expectedDepartmentId", max_length=128)] = None,
    expected_name: Annotated[
        str | None, Query(alias="expectedDepartmentName", max_length=255)
    ] = None,
) -> dict[str, object]:
    return success(
        workspace_reimbursements(
            database,
            actor=workspace_actor(current),
            kind=kind,
        )
    )


@router.post("/reimbursements/workspace/open")
def open_current_workspace(
    body: OpenWorkspaceRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    chosen, retired_ids = open_workspace(
        database,
        actor=workspace_actor(current),
        kind=body.kind,
        draft_id=body.draft_id,
        candidates={candidate.id: candidate.revision for candidate in body.candidates},
    )
    department = next(
        (
            d
            for d in current.reimbursement_departments
            if d.id == chosen.department_id
        ),
        None,
    )
    # Restoring a record never requires membership in its saved department.
    if department is None:
        department = next(iter(current.reimbursement_departments),
                          DepartmentIdentity(chosen.department_id, chosen.department_name))
    if current.beneficiary is None:
        current.record.current_department_id = department.id
        current.record.current_department_name = department.name
    else:
        current = with_reimbursement_department(current, department, request)
    database.commit()
    for retired_id in retired_ids:
        background_tasks.add_task(request.app.state.cleanup_retired_reimbursements, retired_id)
    return success(
        {
            "selectedDepartment": {"id": department.id, "name": department.name},
            **(
                {"reimbursementContext": current.reimbursement_context}
                if current.reimbursement_context
                else {}
            ),
        }
    )


@router.put("/reimbursements/drafts/{draft_id}/department")
async def correct_draft_department(
    draft_id: str,
    body: CorrectDraftDepartmentRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    actor = workspace_actor(current)
    # Locate only this beneficiary/operator's draft, preserving the old department
    # as the CAS source even when another tab changed the Session department.
    owned = database.scalar(_workspace_drafts(actor).where(ReimbursementDraft.id == draft_id))
    if owned is None:
        owned = database.scalar(
            _workspace_drafts(actor, kind="general").where(ReimbursementDraft.id == draft_id)
        )
    if owned is None:
        raise ApiError("REIMBURSEMENT_DRAFT_NOT_FOUND", "报销不存在或无权访问", 404)
    if owned.kind == "general" and current.beneficiary is not None:
        raise ApiError("OA_GENERAL_SELF_ONLY", "普通费用暂只支持本人报销", 403)
    actor = actor_for_draft(actor, owned)
    require_owned_draft(database, actor=actor, draft_id=draft_id, mutable=True)
    if owned.revision != body.expected_revision:
        raise ApiError("REIMBURSEMENT_DRAFT_REVISION_CONFLICT", "报销状态已变化，请刷新后重试", 409)
    verified = None
    binding = None
    if owned.kind == "travel":
        catalog = require_submission_ready_catalog(database)
        binding = CatalogBinding.from_catalog(catalog)
        if body.selection is not None:
            selection = body.selection
            selections = (
                TravelApprovalSelection(
                    profile_key=selection.profile_key,
                    process_instance_id=selection.process_instance_id,
                    query_window=TravelApprovalQueryWindow.from_dates(
                        selection.query_window.from_date,
                        selection.query_window.to_date,
                    ),
                ),
            )
        else:
            # Manual fallback changes the department of the saved approval group;
            # reverify the complete group rather than dropping all but one row.
            related = database.scalars(
                select(ReimbursementDraftRelatedApproval)
                .where(
                    ReimbursementDraftRelatedApproval.draft_id == draft_id,
                )
                .order_by(ReimbursementDraftRelatedApproval.sort_order)
            ).all()
            selections = tuple(
                TravelApprovalSelection(
                    profile_key=approval.travel_profile_key,
                    process_instance_id=approval.process_instance_id,
                    query_window=TravelApprovalQueryWindow.from_dates(
                        datetime.fromtimestamp(
                            approval.listed_from_ms / 1000, ZoneInfo("Asia/Shanghai")
                        ).date(),
                        datetime.fromtimestamp(
                            approval.listed_to_ms / 1000, ZoneInfo("Asia/Shanghai")
                        ).date(),
                    ),
                )
                for approval in related
            )
            if not selections:
                raise ApiError(
                    "REIMBURSEMENT_DEPARTMENT_APPROVAL_REQUIRED", "请先选择出差审批", 422
                )
        database.rollback()
        verified = await reverify_travel_approval_selection(
            request.app.state.dingtalk_workflow,
            catalog,
            current_user_id=actor.user_id,
            required_cc_user_id=actor.operator_user_id,
            selections=selections,
        )
    elif body.selection is not None:
        raise ApiError("REIMBURSEMENT_DEPARTMENT_INVALID", "普通费用请直接选择部门", 422)
    fresh = await restore_reimbursement_context(
        request,
        database,
        get_current_session(request, database),
        request.headers.get(REIMBURSEMENT_CONTEXT_HEADER),
    )
    if (
        fresh.record.session_id_hash != current.record.session_id_hash
        or fresh.reimbursement_user_id != actor.user_id
        or fresh.reimbursement_operator_user_id != actor.operator_user_id
    ):
        raise ApiError("UNAUTHORIZED", "登录或办理身份已变化，请重新进入", 401)
    current = fresh
    actor = replace(
        actor, available_department_ids=tuple(d.id for d in current.reimbursement_departments)
    )
    target_id = (
        verified.department_id
        if verified and verified.department_id in actor.available_department_ids
        else body.department_id
    )
    if target_id is None and len(current.reimbursement_departments) == 1:
        target_id = current.reimbursement_departments[0].id
    if (
        verified
        and verified.department_id in actor.available_department_ids
        and body.department_id not in (None, target_id)
    ):
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_SELECTION_NOT_ALLOWED", "请使用审批对应的当前部门", 422
        )
    department = next((d for d in current.reimbursement_departments if d.id == target_id), None)
    if department is None:
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_CHOICE_REQUIRED", "请从当前有效部门中选择办理部门", 422
        )
    target = replace(actor, department_id=department.id, department_name=department.name)
    session = current.record if current.beneficiary is None else None
    if verified is not None:
        result = _store_related_approvals(
            database,
            actor=actor,
            target_actor=target,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            expected_binding=binding,
            verified=verified,
            changed_at=utc_now(),
            bind_department_for=session,
        )
    else:
        result = correct_general_department(
            database,
            actor=actor,
            target=target,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            session=session,
        )
    if current.beneficiary is not None:
        current = with_reimbursement_department(current, department, request)
    return success(
        {
            "draft": result,
            "selectedDepartment": {"id": department.id, "name": department.name},
            **(
                {"reimbursementContext": current.reimbursement_context}
                if current.reimbursement_context
                else {}
            ),
        }
    )


@router.post("/reimbursements/drafts/{draft_id}/recover")
def recover_draft(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    try:
        result = recover_failed_reimbursement(
            actor=draft_actor(current),
            source_id=draft_id,
            expected_revision=body.expected_revision,
            settings=request.app.state.settings,
            session_factory=request.app.state.database_session_factory,
            staging=request.app.state.reimbursement_staging,
            quota=request.app.state.reimbursement_quota,
        )
        return success(result)
    except (ReimbursementQuotaError, ReimbursementStagingError) as error:
        raise map_reimbursement_storage_error(error) from None


@router.get("/reimbursements/drafts/{draft_id}")
def get_draft(
    draft_id: str,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
    history: bool = False,
) -> dict[str, object]:
    return success(
        get_reimbursement_draft(
            database,
            actor=history_draft_actor(database, current, draft_id)
            if history
            else draft_actor(current),
            draft_id=draft_id,
        )
    )


@router.delete("/reimbursements/drafts/{draft_id}")
def delete_draft(
    draft_id: str,
    request: Request,
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
) -> dict[str, object]:
    quota: ReimbursementQuotaCoordinator = request.app.state.reimbursement_quota
    deleted_draft_id = quota.delete_owned_draft(
        actor=draft_actor(current),
        draft_id=draft_id,
        expected_revision=expected_revision,
    )
    return success({"deletedDraftId": deleted_draft_id})


@router.put("/reimbursements/drafts/{draft_id}")
def update_draft(
    draft_id: str,
    body: UpdateReimbursementDraftRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    return success(
        update_reimbursement_draft(
            database,
            actor=draft_actor(current),
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            draft_input=body.input,
            max_items=request.app.state.settings.expense_max_items,
        )
    )


@router.put("/reimbursements/drafts/{draft_id}/related-approvals")
async def update_related_approvals(
    draft_id: str,
    body: ReplaceRelatedApprovalsRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    return success(
        await replace_related_approvals(
            database,
            workflow,
            actor=draft_actor(current),
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            selections=body.selections,
            departments=current.reimbursement_departments,
        )
    )


@router.get("/reimbursements/drafts/{draft_id}/general-approvals")
async def get_saved_general_approvals(
    draft_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_reimbursement_session)],
) -> dict[str, object]:
    return success(
        await read_general_approval_summaries(
            database,
            request.app.state.dingtalk_workflow,
            actor=draft_actor(current),
            draft_id=draft_id,
        )
    )


@router.put("/reimbursements/drafts/{draft_id}/general-approvals")
async def update_general_approvals(
    draft_id: str,
    body: ReplaceGeneralApprovalsRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    return success(
        await replace_general_approvals(
            database,
            request.app.state.dingtalk_workflow,
            actor=draft_actor(current),
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            instance_ids=body.instance_ids,
        )
    )


@router.post("/reimbursements/drafts/{draft_id}/review")
def review_draft(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_reimbursement_csrf)],
) -> dict[str, object]:
    return success(
        mark_reimbursement_draft_review_ready(
            database,
            actor=draft_actor(current),
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            max_items=request.app.state.settings.expense_max_items,
            settings=request.app.state.settings,
        )
    )
