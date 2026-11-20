"""One current reimbursement per beneficiary, operator and kind, across departments."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models.reimbursement import ReimbursementDraft, utc_now
from app.models.session import UserSession
from app.services.reimbursement_drafts import (
    DraftActor,
    _revision_conflict_error,
    _workspace_drafts,
    bump_owned_draft_revision,
    draft_actor,
    draft_data,
    require_owned_draft,
    require_resolved_workspace,
)
from app.services.sessions import CurrentSession


def workspace_actor(current: CurrentSession) -> DraftActor:
    return draft_actor(current)


def open_workspace(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    kind: str,
    candidates: dict[str, int],
) -> tuple[ReimbursementDraft, list[str]]:
    # The full observed set is a CAS token: a concurrent edit/new record requires
    # another explicit choice. A lost response may safely reopen the survivor.
    database.rollback()
    database.connection().exec_driver_sql("BEGIN IMMEDIATE")
    actor.require_current_delegation(database)
    drafts = database.scalars(_workspace_drafts(actor, kind=kind)).all()
    chosen = next((d for d in drafts if d.id == draft_id), None)
    observed = {d.id: d.revision for d in drafts}
    if chosen is None or (len(drafts) > 1 and observed != candidates):
        raise _revision_conflict_error()
    if len(drafts) == 1 and candidates.get(draft_id) != chosen.revision:
        raise _revision_conflict_error()
    retired_ids = []
    for draft in drafts:
        if draft.id != draft_id:
            draft.workspace_retired = True
            retired_ids.append(draft.id)
    return chosen, retired_ids


def correct_general_department(
    database: Session,
    *,
    actor: DraftActor,
    target: DraftActor,
    draft_id: str,
    expected_revision: int,
    session: UserSession | None,
) -> dict[str, object]:
    database.rollback()
    database.connection().exec_driver_sql("BEGIN IMMEDIATE")
    draft = require_owned_draft(database, actor=actor, draft_id=draft_id, mutable=True)
    if draft.kind != "general":
        raise ApiError(
            "REIMBURSEMENT_DEPARTMENT_APPROVAL_REQUIRED", "请通过出差审批更正差旅报销部门", 422
        )
    require_resolved_workspace(database, actor, kind=draft.kind)
    bump_owned_draft_revision(
        database, actor=actor, draft_id=draft_id, expected_revision=expected_revision, now=utc_now()
    )
    database.refresh(draft)
    draft.department_id = target.department_id
    draft.department_name = target.department_name
    if session is not None:
        session.current_department_id = target.department_id
        session.current_department_name = target.department_name
    database.commit()
    return draft_data(database, draft)


def actor_for_draft(actor: DraftActor, draft: ReimbursementDraft) -> DraftActor:
    return replace(actor, department_id=draft.department_id, department_name=draft.department_name)
