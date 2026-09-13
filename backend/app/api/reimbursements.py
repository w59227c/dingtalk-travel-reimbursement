from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

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
    create_reimbursement_draft,
    draft_actor,
    get_reimbursement_draft,
    list_reimbursement_drafts,
    mark_reimbursement_draft_review_ready,
    replace_related_approvals,
    update_reimbursement_draft,
)
from app.services.reimbursement_quota import ReimbursementQuotaCoordinator
from app.services.sessions import CurrentSession, get_current_session, require_csrf

router = APIRouter(tags=["reimbursement-drafts"])


@router.post("/reimbursements/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(
    body: CreateReimbursementDraftRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    return success(
        create_reimbursement_draft(
            database,
            actor=draft_actor(current),
            draft_input=body.input,
            ttl_days=request.app.state.settings.reimbursement_draft_ttl_days,
            max_items=request.app.state.settings.expense_max_items,
        )
    )


@router.get("/reimbursements/drafts")
def list_drafts(
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    return success(
        list_reimbursement_drafts(
            database,
            actor=draft_actor(current),
            offset=offset,
            limit=limit,
        )
    )


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
