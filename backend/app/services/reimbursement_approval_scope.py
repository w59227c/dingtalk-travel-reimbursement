from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.models.reimbursement import ReimbursementDraftRelatedApproval, ReimbursementDraftStatus
from app.services.oa_template_profiles import require_submission_ready_catalog
from app.services.reimbursement_drafts import DraftActor, require_previewable_draft
from app.services.travel_approvals import (
    TravelApprovalQueryWindow,
    TravelApprovalSelection,
    reverify_travel_approval_selection,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


async def verify_delegated_saved_approvals(
    database: Session,
    workflow: DingTalkWorkflowClient,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
) -> None:
    operator_id = actor.operator_user_id
    if operator_id is None:
        return
    draft = require_previewable_draft(
        database, draft_id=draft_id, actor=actor, expected_revision=expected_revision
    )
    if draft.status == ReimbursementDraftStatus.LOCKED.value:
        return
    catalog = require_submission_ready_catalog(database)
    rows = database.scalars(
        select(ReimbursementDraftRelatedApproval)
        .where(ReimbursementDraftRelatedApproval.draft_id == draft_id)
        .order_by(ReimbursementDraftRelatedApproval.sort_order)
    ).all()
    selections = []
    for row in rows:
        start_date = datetime.fromtimestamp(row.listed_from_ms / 1000, _SHANGHAI).date()
        end_date = datetime.fromtimestamp(row.listed_to_ms / 1000, _SHANGHAI).date()
        window = TravelApprovalQueryWindow.from_dates(start_date, end_date)
        if (
            window.start_time_ms != row.listed_from_ms
            or window.end_time_ms != row.listed_to_ms
        ):
            raise ApiError(
                "TRAVEL_APPROVAL_QUERY_WINDOW_INVALID",
                "请重新选择关联出差审批",
                409,
            )
        selections.append(
            TravelApprovalSelection(
                profile_key=row.travel_profile_key,
                process_instance_id=row.process_instance_id,
                query_window=window,
            )
        )
    database.rollback()
    await reverify_travel_approval_selection(
        workflow,
        catalog,
        current_user_id=actor.user_id,
        required_cc_user_id=operator_id,
        selections=tuple(selections),
    )
    require_previewable_draft(
        database, draft_id=draft_id, actor=actor, expected_revision=expected_revision
    )
