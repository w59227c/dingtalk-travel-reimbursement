from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.schemas.common import success
from app.schemas.reimbursements import (
    CreateReimbursementDraftRequest,
    DraftRevisionRequest,
    ReplaceRelatedApprovalsRequest,
    UpdateReimbursementDraftRequest,
)
from app.services.reimbursement_drafts import (
    DraftActor,
    create_reimbursement_draft,
    draft_actor,
    get_reimbursement_draft,
    list_reimbursement_drafts,
    list_reimbursement_submission_history,
    mark_reimbursement_draft_review_ready,
    replace_related_approvals,
    update_reimbursement_draft,
)
from app.services.reimbursement_files import map_reimbursement_storage_error
from app.services.reimbursement_quota import ReimbursementQuotaCoordinator, ReimbursementQuotaError
from app.services.reimbursement_recovery import (
    recover_failed_reimbursement,
    workspace_reimbursements,
)
from app.services.reimbursement_staging import ReimbursementStagingError
from app.services.sessions import CurrentSession, get_current_session, require_csrf

router = APIRouter(tags=["reimbursement-drafts"])


def _checked_actor(
    current: CurrentSession,
    expected_id: str | None,
    expected_name: str | None,
) -> DraftActor:
    actor = draft_actor(current)
    if (expected_id is not None and actor.department_id != expected_id) or (
        expected_name is not None and actor.department_name != expected_name
    ):
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_CHANGED",
            "当前报销部门已变化，请刷新页面后重试",
            409,
        )
    return actor


@router.post("/reimbursements/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(
    body: CreateReimbursementDraftRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    actor = _checked_actor(current, body.expected_department_id, body.expected_department_name)
    created = create_reimbursement_draft(
        database,
        actor=actor,
        draft_input=body.input,
        ttl_days=request.app.state.settings.reimbursement_draft_ttl_days,
        max_items=request.app.state.settings.expense_max_items,
        reuse_existing=body.reuse_existing,
        replaces_draft_id=body.replaces_draft_id,
        replaces_draft_revision=body.replaces_draft_revision,
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
    current: Annotated[CurrentSession, Depends(get_current_session)],
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    expected_id: Annotated[str | None, Query(alias="expectedDepartmentId", max_length=128)] = None,
    expected_name: Annotated[
        str | None, Query(alias="expectedDepartmentName", max_length=255)
    ] = None,
) -> dict[str, object]:
    return success(
        list_reimbursement_drafts(
            database,
            actor=_checked_actor(current, expected_id, expected_name),
            offset=offset,
            limit=limit,
        )
    )


@router.get("/reimbursements/submission-history")
def list_submission_history(
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
    expected_id: Annotated[str | None, Query(alias="expectedDepartmentId", max_length=128)] = None,
    expected_name: Annotated[
        str | None, Query(alias="expectedDepartmentName", max_length=255)
    ] = None,
) -> dict[str, object]:
    return success(
        list_reimbursement_submission_history(
            database,
            actor=_checked_actor(current, expected_id, expected_name),
        )
    )


@router.get("/reimbursements/workspace")
def get_workspace(
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
    expected_id: Annotated[str | None, Query(alias="expectedDepartmentId", max_length=128)] = None,
    expected_name: Annotated[
        str | None, Query(alias="expectedDepartmentName", max_length=255)
    ] = None,
) -> dict[str, object]:
    return success(
        workspace_reimbursements(
            database,
            actor=_checked_actor(current, expected_id, expected_name),
        )
    )


@router.post("/reimbursements/drafts/{draft_id}/recover")
def recover_draft(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    current: Annotated[CurrentSession, Depends(require_csrf)],
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
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    return success(
        get_reimbursement_draft(
            database,
            actor=draft_actor(current),
            draft_id=draft_id,
        )
    )


@router.delete("/reimbursements/drafts/{draft_id}")
def delete_draft(
    draft_id: str,
    request: Request,
    current: Annotated[CurrentSession, Depends(require_csrf)],
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
    current: Annotated[CurrentSession, Depends(require_csrf)],
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
    current: Annotated[CurrentSession, Depends(require_csrf)],
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
        )
    )


@router.post("/reimbursements/drafts/{draft_id}/review")
def review_draft(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
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
