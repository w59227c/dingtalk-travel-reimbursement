from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.models.reimbursement import ReimbursementSubmission, ReimbursementSubmissionStatus
from app.schemas.common import success
from app.schemas.reimbursement_submissions import SubmitReimbursementRequest
from app.services.oa_reimbursement_payload import (
    SNAPSHOT_VERSION,
    build_snapshot,
    collect_snapshot_source,
    serialize_snapshot,
    snapshot_sha256,
)
from app.services.oa_template_profiles import load_fresh_submission_catalog
from app.services.reimbursement_drafts import draft_actor
from app.services.reimbursement_submissions import (
    admin_confirm_manual_review_not_created,
    admin_resume_manual_review_with_instance,
    create_or_get_submission,
    find_owned_submission_for_draft,
    request_submission_recheck,
    require_owned_submission,
)
from app.services.sessions import (
    CurrentSession,
    get_current_session,
    require_admin_csrf,
    require_csrf,
)

router = APIRouter(tags=["reimbursement-submissions"])

_TERMINAL_STATUSES = frozenset(
    {
        ReimbursementSubmissionStatus.SUBMITTED.value,
        ReimbursementSubmissionStatus.FAILED_FINAL.value,
        ReimbursementSubmissionStatus.MANUAL_REVIEW.value,
    }
)


class AdminSubmissionRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["ATTACH_INSTANCE", "CONFIRM_NOT_CREATED"]
    process_instance_id: str | None = Field(
        default=None,
        alias="processInstanceId",
        min_length=1,
        max_length=128,
    )
    confirm_uncertain_uploads_absent: bool = Field(
        default=False,
        alias="confirmUncertainUploadsAbsent",
    )
    verification_note: str = Field(
        alias="verificationNote",
        min_length=1,
        max_length=500,
    )

    @field_validator("verification_note")
    @classmethod
    def validate_verification_note(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character in normalized for character in "\r\n\x00"):
            raise ValueError("verificationNote must be one line of text")
        return normalized

    @model_validator(mode="after")
    def validate_action_fields(self) -> AdminSubmissionRecoveryRequest:
        if self.action == "ATTACH_INSTANCE" and self.process_instance_id is None:
            raise ValueError("ATTACH_INSTANCE requires processInstanceId")
        if self.action == "CONFIRM_NOT_CREATED" and self.process_instance_id is not None:
            raise ValueError("CONFIRM_NOT_CREATED does not accept processInstanceId")
        return self


@router.post(
    "/oa/reimbursements/{draft_id}/submit",
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_reimbursement(
    draft_id: str,
    body: SubmitReimbursementRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, object]:
    """Lock one reviewed revision; all remote work is performed by the worker."""

    normalized_key = _normalized_idempotency_key(idempotency_key)
    actor = draft_actor(current)
    existing = find_owned_submission_for_draft(
        database,
        actor=actor,
        draft_id=draft_id,
    )
    if existing is not None:
        return success(_submission_data(existing))

    settings = request.app.state.settings
    if not settings.dingtalk_oa_worker_enabled:
        raise ApiError(
            "OA_SUBMISSION_DISABLED",
            "钉钉 OA 提交服务尚未启用，请联系管理员",
            503,
        )
    agent_id = settings.dingtalk_agent_id
    if agent_id is None:
        raise ApiError(
            "DINGTALK_AGENT_ID_MISSING",
            "钉钉应用 AgentId 尚未配置",
            503,
        )
    # Verify the live OA schemas before producing an immutable snapshot or
    # locking the draft. The helper persists DRIFTED so every session stops
    # using an outdated catalog until an administrator reconfirms it.
    database.rollback()
    await load_fresh_submission_catalog(
        request.app.state.database_session_factory,
        request.app.state.dingtalk_workflow,
    )
    source = collect_snapshot_source(
        database,
        staging=request.app.state.reimbursement_staging,
        actor=actor,
        originator_union_id=current.record.dingtalk_union_id,
        originator_name=current.record.name,
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        microapp_agent_id=agent_id,
        excel_template_path=settings.excel_template_path,
        max_items=settings.expense_max_items,
        ocr_timeout_seconds=settings.ocr_operation_timeout_seconds,
    )
    snapshot = build_snapshot(source)
    snapshot_json = serialize_snapshot(snapshot)
    result = create_or_get_submission(
        database,
        actor=actor,
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        originator_union_id=current.record.dingtalk_union_id,
        originator_name=current.record.name,
        idempotency_key=normalized_key,
        snapshot_version=SNAPSHOT_VERSION,
        form_snapshot_json=snapshot_json,
        snapshot_sha256=snapshot_sha256(snapshot),
    )
    submission = require_owned_submission(
        database,
        actor=actor,
        submission_id=result.submission_id,
    )
    return success(_submission_data(submission))


@router.get("/oa/reimbursements/submissions/{submission_id}")
def get_reimbursement_submission(
    submission_id: str,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    submission = require_owned_submission(
        database,
        actor=draft_actor(current),
        submission_id=submission_id,
    )
    return success(_submission_data(submission))


@router.post("/admin/oa/reimbursements/submissions/{submission_id}/recover")
def recover_reimbursement_submission_as_admin(
    submission_id: str,
    body: AdminSubmissionRecoveryRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    """Resolve an otherwise terminal manual-review checkpoint without recreating OA."""

    if not request.app.state.settings.dingtalk_oa_worker_enabled:
        raise ApiError("OA_SUBMISSION_DISABLED", "审批处理服务尚未启用", 503)
    if body.action == "ATTACH_INSTANCE":
        submission = admin_resume_manual_review_with_instance(
            database,
            corp_id=current.record.corp_id,
            submission_id=submission_id,
            process_instance_id=body.process_instance_id or "",
            admin_user_id=current.record.dingtalk_user_id,
            verification_note=body.verification_note,
        )
    else:
        submission = admin_confirm_manual_review_not_created(
            database,
            corp_id=current.record.corp_id,
            submission_id=submission_id,
            admin_user_id=current.record.dingtalk_user_id,
            verification_note=body.verification_note,
            confirm_uncertain_uploads_absent=body.confirm_uncertain_uploads_absent,
        )
    return success(_submission_data(submission))


@router.post("/oa/reimbursements/submissions/{submission_id}/recheck")
def recheck_reimbursement_submission(
    submission_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    actor = draft_actor(current)
    existing = require_owned_submission(database, actor=actor, submission_id=submission_id)
    if existing.status == ReimbursementSubmissionStatus.SUBMITTED.value:
        return success(_submission_data(existing))
    if not request.app.state.settings.dingtalk_oa_worker_enabled:
        raise ApiError("OA_SUBMISSION_DISABLED", "审批核对服务尚未启用，请联系管理员", 503)
    return success(
        _submission_data(
            request_submission_recheck(
                database,
                actor=actor,
                submission_id=submission_id,
            )
        )
    )


@router.get("/oa/reimbursements/drafts/{draft_id}/submission")
def get_reimbursement_submission_for_draft(
    draft_id: str,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    """Recover an existing submission without mutating or locking its draft."""

    submission = find_owned_submission_for_draft(
        database,
        actor=draft_actor(current),
        draft_id=draft_id,
    )
    if submission is None:
        raise ApiError("REIMBURSEMENT_SUBMISSION_NOT_FOUND", "提交记录不存在", 404)
    return success(_submission_data(submission))


def _submission_data(submission: ReimbursementSubmission) -> dict[str, object]:
    error = None
    if submission.last_error_code is not None:
        error = {
            "code": submission.last_error_code,
            "message": submission.last_error_message or "提交处理遇到问题",
        }
    return {
        "submissionId": submission.id,
        "draftId": submission.draft_id,
        "status": submission.status,
        "statusVersion": submission.status_version,
        "attemptCount": submission.attempt_count,
        "processInstanceId": submission.process_instance_id,
        "businessId": submission.business_id,
        "approvalUrl": submission.approval_url,
        "error": error,
        "pollAfterMs": 0 if submission.status in _TERMINAL_STATUSES else 1500,
        "createdAt": _timestamp(submission.created_at),
        "updatedAt": _timestamp(submission.updated_at),
        "submittedAt": _timestamp(submission.submitted_at),
    }


def _normalized_idempotency_key(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ApiError(
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key 必须是 UUID",
            422,
        ) from None
    normalized = str(parsed)
    if value.lower() != normalized:
        raise ApiError(
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key 必须是规范 UUID",
            422,
        )
    return normalized


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")
