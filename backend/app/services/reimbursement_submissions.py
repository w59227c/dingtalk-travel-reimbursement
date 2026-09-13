from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileStatus,
    ReimbursementDraftStatus,
    ReimbursementSubmission,
    ReimbursementSubmissionStatus,
    ReimbursementUpload,
    ReimbursementUploadLocalStatus,
    ReimbursementUploadRole,
    ReimbursementUploadStatus,
    new_uuid,
    utc_now,
)
from app.services.reimbursement_drafts import DraftActor
from app.services.reimbursement_quota import SubmissionLease

_SHA256 = re.compile(r"[0-9a-f]{64}")
_CLAIMABLE_STATUSES = frozenset(
    {
        ReimbursementSubmissionStatus.QUEUED.value,
        ReimbursementSubmissionStatus.VALIDATING.value,
        ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
        ReimbursementSubmissionStatus.UPLOADING.value,
        ReimbursementSubmissionStatus.RECONCILING.value,
        ReimbursementSubmissionStatus.VERIFYING.value,
        ReimbursementSubmissionStatus.FAILED_RETRYABLE.value,
        ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
    }
)
_RETRYABLE_STATUSES = frozenset(
    {
        ReimbursementSubmissionStatus.VALIDATING.value,
        ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
        ReimbursementSubmissionStatus.UPLOADING.value,
        ReimbursementSubmissionStatus.VERIFYING.value,
        ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
    }
)
_NORMAL_TRANSITIONS = {
    ReimbursementSubmissionStatus.QUEUED.value: {
        ReimbursementSubmissionStatus.VALIDATING.value,
    },
    ReimbursementSubmissionStatus.VALIDATING.value: {
        ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
    },
    ReimbursementSubmissionStatus.GENERATING_EXCEL.value: {
        ReimbursementSubmissionStatus.UPLOADING.value,
    },
}
_TERMINAL_STATUSES = frozenset(
    {
        ReimbursementSubmissionStatus.SUBMITTED.value,
        ReimbursementSubmissionStatus.FAILED_FINAL.value,
        ReimbursementSubmissionStatus.MANUAL_REVIEW.value,
    }
)
_FAILED_FINAL_LOCAL_DISCARD_STATUSES = frozenset(
    {
        ReimbursementUploadStatus.PENDING.value,
        ReimbursementUploadStatus.PUTTING.value,
        ReimbursementUploadStatus.PUT_DONE.value,
    }
)


class ReimbursementSubmissionConflict(RuntimeError):
    """A submission or upload changed since the caller's CAS snapshot."""


def request_submission_recheck(
    database: Session, *, actor: DraftActor, submission_id: str
) -> ReimbursementSubmission:
    """Resume only readback of an already-known OA; never enqueue creation."""
    from app.services.oa_reimbursement_payload import parse_create_command

    current = require_owned_submission(database, actor=actor, submission_id=submission_id)
    if current.status in {"SUBMITTED", "VERIFYING"}:
        return current
    if current.status != "MANUAL_REVIEW" or not current.process_instance_id:
        raise ApiError("OA_RECHECK_NOT_ALLOWED", "没有可重新核对的已创建审批，请联系管理员", 409)
    if (
        not current.oa_request_json
        or not current.oa_request_hash
        or not current.oa_create_started_at
    ):
        raise ApiError("OA_CREATE_CHECKPOINT_MISSING", "审批提交快照缺失，请联系管理员", 409)
    parse_create_command(current.oa_request_json, expected_sha256=current.oa_request_hash)
    now = utc_now()
    changed = database.execute(
        update(ReimbursementSubmission)
        .where(
            ReimbursementSubmission.id == current.id,
            ReimbursementSubmission.status == "MANUAL_REVIEW",
            ReimbursementSubmission.status_version == current.status_version,
            ReimbursementSubmission.process_instance_id == current.process_instance_id,
            ReimbursementSubmission.lease_token.is_(None),
        )
        .values(
            status="VERIFYING",
            resume_status=None,
            status_version=current.status_version + 1,
            next_attempt_at=now,
            last_error_code=None,
            last_error_message=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        database.rollback()
        raise ApiError("OA_RECHECK_CONFLICT", "提交状态已变化，请刷新后重试", 409)
    database.commit()
    database.expire_all()
    return require_owned_submission(database, actor=actor, submission_id=submission_id)


def admin_resume_manual_review_with_instance(
    database: Session,
    *,
    corp_id: str,
    submission_id: str,
    process_instance_id: str,
) -> ReimbursementSubmission:
    """Attach an OA id verified by an administrator and resume readback only."""

    from app.services.oa_reimbursement_payload import parse_create_command

    normalized_corp = _required_text(corp_id, maximum=128)
    normalized_submission = _required_text(submission_id, maximum=36)
    normalized_instance = _required_text(process_instance_id, maximum=128)
    current = database.scalar(
        select(ReimbursementSubmission).where(
            ReimbursementSubmission.id == normalized_submission,
            ReimbursementSubmission.corp_id == normalized_corp,
        )
    )
    if current is None:
        raise ApiError("REIMBURSEMENT_SUBMISSION_NOT_FOUND", "提交记录不存在", 404)
    if current.status in {
        ReimbursementSubmissionStatus.VERIFYING.value,
        ReimbursementSubmissionStatus.SUBMITTED.value,
    } and current.process_instance_id == normalized_instance:
        return current
    if (
        current.status != ReimbursementSubmissionStatus.MANUAL_REVIEW.value
        or current.process_instance_id is not None
        or current.lease_token is not None
    ):
        raise ApiError("OA_ADMIN_RECOVERY_NOT_ALLOWED", "当前提交状态不能绑定审批编号", 409)
    if (
        not current.oa_request_json
        or not current.oa_request_hash
        or not current.oa_create_started_at
    ):
        raise ApiError("OA_CREATE_CHECKPOINT_MISSING", "审批提交快照缺失，不能自动核对", 409)
    parse_create_command(current.oa_request_json, expected_sha256=current.oa_request_hash)
    changed_at = utc_now()
    try:
        changed = database.execute(
            update(ReimbursementSubmission)
            .where(
                ReimbursementSubmission.id == current.id,
                ReimbursementSubmission.corp_id == normalized_corp,
                ReimbursementSubmission.status
                == ReimbursementSubmissionStatus.MANUAL_REVIEW.value,
                ReimbursementSubmission.status_version == current.status_version,
                ReimbursementSubmission.process_instance_id.is_(None),
                ReimbursementSubmission.lease_token.is_(None),
            )
            .values(
                status=ReimbursementSubmissionStatus.VERIFYING.value,
                process_instance_id=normalized_instance,
                resume_status=None,
                status_version=current.status_version + 1,
                next_attempt_at=changed_at,
                last_error_code=None,
                last_error_message=None,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise ReimbursementSubmissionConflict("manual-review submission changed")
        database.commit()
        database.expire_all()
    except IntegrityError:
        database.rollback()
        raise ApiError("OA_INSTANCE_ALREADY_BOUND", "该审批编号已关联其他提交记录", 409) from None
    except ReimbursementSubmissionConflict:
        database.rollback()
        raise ApiError("OA_ADMIN_RECOVERY_CONFLICT", "提交状态已变化，请刷新后重试", 409) from None
    return database.get(ReimbursementSubmission, normalized_submission)


def admin_confirm_manual_review_not_created(
    database: Session,
    *,
    corp_id: str,
    submission_id: str,
    admin_user_id: str,
    confirm_uncertain_uploads_absent: bool = False,
) -> ReimbursementSubmission:
    """Resume cleanup after an administrator confirms no OA was created.

    A Storage commit whose response was lost is never guessed automatically.
    The administrator must separately confirm that those remote files are also
    absent before their checkpoints can safely return to the pre-commit state.
    """

    normalized_corp = _required_text(corp_id, maximum=128)
    normalized_submission = _required_text(submission_id, maximum=36)
    normalized_admin = _required_text(admin_user_id, maximum=128)
    current = database.scalar(
        select(ReimbursementSubmission).where(
            ReimbursementSubmission.id == normalized_submission,
            ReimbursementSubmission.corp_id == normalized_corp,
        )
    )
    if current is None:
        raise ApiError("REIMBURSEMENT_SUBMISSION_NOT_FOUND", "提交记录不存在", 404)
    if (
        current.status == ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value
        and current.process_instance_id is None
        and current.orphan_confirmation_code == "ADMIN_CONFIRMED_NOT_CREATED"
    ):
        return current
    if (
        current.status != ReimbursementSubmissionStatus.MANUAL_REVIEW.value
        or current.process_instance_id is not None
        or current.lease_token is not None
    ):
        raise ApiError("OA_ADMIN_RECOVERY_NOT_ALLOWED", "当前提交状态不能确认未创建", 409)

    uncertain_statuses = {
        ReimbursementUploadStatus.COMMITTING.value,
        ReimbursementUploadStatus.COMMIT_UNCERTAIN.value,
    }
    uncertain_upload = database.scalar(
        select(ReimbursementUpload.id)
        .where(
            ReimbursementUpload.submission_id == current.id,
            ReimbursementUpload.upload_status.in_(uncertain_statuses),
        )
        .limit(1)
    )
    if uncertain_upload is not None and not confirm_uncertain_uploads_absent:
        database.rollback()
        raise ApiError(
            "OA_REMOTE_FILE_CONFIRMATION_REQUIRED",
            "存在提交结果未知的附件，请先确认钉钉文件中不存在后再继续",
            409,
        )

    changed_at = utc_now()
    try:
        changed = database.execute(
            update(ReimbursementSubmission)
            .where(
                ReimbursementSubmission.id == current.id,
                ReimbursementSubmission.corp_id == normalized_corp,
                ReimbursementSubmission.status
                == ReimbursementSubmissionStatus.MANUAL_REVIEW.value,
                ReimbursementSubmission.status_version == current.status_version,
                ReimbursementSubmission.process_instance_id.is_(None),
                ReimbursementSubmission.lease_token.is_(None),
            )
            .values(
                status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
                resume_status=None,
                status_version=current.status_version + 1,
                next_attempt_at=changed_at,
                orphan_confirmed_at=changed_at,
                orphan_confirmation_code="ADMIN_CONFIRMED_NOT_CREATED",
                orphan_confirmed_by_user_id=normalized_admin,
                last_error_code=None,
                last_error_message=None,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise ReimbursementSubmissionConflict("manual-review submission changed")
        if uncertain_upload is not None:
            database.execute(
                update(ReimbursementUpload)
                .where(
                    ReimbursementUpload.submission_id == current.id,
                    ReimbursementUpload.upload_status.in_(uncertain_statuses),
                )
                .values(
                    upload_status=ReimbursementUploadStatus.PENDING.value,
                    status_version=ReimbursementUpload.status_version + 1,
                    put_started_at=None,
                    commit_started_at=None,
                    space_id=None,
                    file_id=None,
                    last_error_code="ADMIN_CONFIRMED_REMOTE_FILE_ABSENT",
                    updated_at=changed_at,
                )
                .execution_options(synchronize_session=False)
            )
        database.commit()
        database.expire_all()
    except ReimbursementSubmissionConflict:
        database.rollback()
        raise ApiError("OA_ADMIN_RECOVERY_CONFLICT", "提交状态已变化，请刷新后重试", 409) from None
    return database.get(ReimbursementSubmission, normalized_submission)


@dataclass(frozen=True, slots=True)
class SubmissionCreateResult:
    submission_id: str
    status: str
    status_version: int
    created: bool
    idempotency_matched: bool


@dataclass(frozen=True, slots=True)
class SubmissionClaim:
    lease: SubmissionLease
    status: str
    snapshot_version: int
    form_snapshot_json: str
    oa_request_json: str | None


@dataclass(frozen=True, slots=True)
class SubmissionRecoveryResult:
    oa_create_reconciliations: int = 0
    uncertain_uploads: int = 0
    manual_reviews: int = 0


@dataclass(frozen=True, slots=True)
class UploadCheckpoint:
    upload_id: str
    upload_status: str
    status_version: int


@dataclass(frozen=True, slots=True)
class LinkedLocalReleaseCandidate:
    corp_id: str
    user_id: str
    submission_id: str
    upload_id: str
    upload_status_version: int
    role: str
    local_storage_key: str
    size_bytes: int
    sha256: str
    source_draft_file_id: str | None


@dataclass(frozen=True, slots=True)
class BundledSourceReleaseCandidate:
    submission_id: str
    file_id: str
    storage_key: str
    size_bytes: int
    sha256: str


def _require_bundle_manifest(
    _submission: ReimbursementSubmission, uploads: list[ReimbursementUpload]
) -> None:
    if [
        (item.role, item.sort_order) for item in sorted(uploads, key=lambda item: item.sort_order)
    ] != [
        (ReimbursementUploadRole.GENERATED_PDF.value, 0),
        (ReimbursementUploadRole.GENERATED_EXCEL.value, 1),
    ]:
        raise ReimbursementSubmissionConflict("submission requires exactly the PDF and Excel")


def create_submission(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    originator_union_id: str,
    originator_name: str,
    idempotency_key: str,
    snapshot_version: int,
    form_snapshot_json: str,
    snapshot_sha256: str,
    now: datetime | None = None,
) -> SubmissionCreateResult:
    """Lock one review-ready draft and persist its immutable submit snapshot.

    The caller must build ``form_snapshot_json`` from the same Session directly
    before this call. This function performs the draft CAS and submission insert
    in one transaction, then commits it. Source files remain draft records; only
    the generated PDF and Excel become remote uploads.
    """

    _require_actor(actor)
    normalized_draft_id = _required_text(draft_id, maximum=36)
    idempotency_hash = _idempotency_hash(idempotency_key)

    existing = _owned_submission_for_draft(
        database,
        actor=actor,
        draft_id=normalized_draft_id,
    )
    if existing is not None:
        return _create_result(existing, created=False, idempotency_hash=idempotency_hash)

    # A draft is a one-shot submission boundary.  Only validate creation-only
    # inputs after the existing lookup so a refresh can recover the durable
    # submission even when its stale page no longer has a usable snapshot or
    # expected draft revision.
    _require_positive_integer(expected_revision, name="expected_revision")
    _require_positive_integer(snapshot_version, name="snapshot_version")
    normalized_union_id = _required_text(originator_union_id, maximum=128)
    normalized_name = _required_text(originator_name, maximum=128)
    canonical_snapshot = _validated_json_snapshot(
        form_snapshot_json,
        snapshot_sha256,
        field_name="submission snapshot",
    )
    created_at = _naive_utc(now) if now is not None else utc_now()

    draft = database.scalar(
        select(ReimbursementDraft).where(
            ReimbursementDraft.id == normalized_draft_id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
        )
    )
    if draft is None:
        raise _draft_not_found_error()
    _require_submission_department(draft, actor)
    _require_review_ready_draft(
        draft,
        expected_revision=expected_revision,
        now=created_at,
    )
    source_files = database.scalars(
        select(ReimbursementDraftFile)
        .where(
            ReimbursementDraftFile.draft_id == draft.id,
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
        )
        .order_by(ReimbursementDraftFile.sort_order, ReimbursementDraftFile.id)
    ).all()
    if not source_files:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_NOT_READY",
            "请先上传至少一个有效附件",
            409,
        )
    for source in source_files:
        if source.size_bytes is None or source.sha256 is None:
            raise ApiError(
                "REIMBURSEMENT_DRAFT_CORRUPTED",
                "草稿附件数据损坏，请联系管理员",
                500,
            )

    submission_id = new_uuid()
    try:
        claimed = database.execute(
            update(ReimbursementDraft)
            .where(
                ReimbursementDraft.id == draft.id,
                ReimbursementDraft.corp_id == actor.corp_id,
                ReimbursementDraft.owner_user_id == actor.user_id,
                ReimbursementDraft.department_id == actor.department_id,
                ReimbursementDraft.department_name == actor.department_name,
                ReimbursementDraft.revision == expected_revision,
                ReimbursementDraft.status == ReimbursementDraftStatus.REVIEW_READY.value,
                ReimbursementDraft.locked_at.is_(None),
                ReimbursementDraft.expires_at > created_at,
                ~exists(
                    select(ReimbursementSubmission.id).where(
                        ReimbursementSubmission.draft_id == ReimbursementDraft.id
                    )
                ),
            )
            .values(
                status=ReimbursementDraftStatus.LOCKED.value,
                revision=expected_revision + 1,
                locked_at=created_at,
                updated_at=created_at,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            database.rollback()
            existing = _owned_submission_for_draft(
                database,
                actor=actor,
                draft_id=normalized_draft_id,
            )
            if existing is not None:
                return _create_result(
                    existing,
                    created=False,
                    idempotency_hash=idempotency_hash,
                )
            _raise_current_draft_conflict(
                database,
                actor=actor,
                draft_id=normalized_draft_id,
                expected_revision=expected_revision,
                now=created_at,
            )

        submission = ReimbursementSubmission(
            id=submission_id,
            draft_id=draft.id,
            corp_id=actor.corp_id,
            originator_user_id=actor.user_id,
            originator_union_id=normalized_union_id,
            originator_name=normalized_name,
            department_id=draft.department_id,
            department_name=draft.department_name,
            template_process_code=draft.template_process_code,
            template_config_version=draft.template_config_version,
            schema_fingerprint=draft.schema_fingerprint,
            snapshot_version=snapshot_version,
            form_snapshot_json=canonical_snapshot,
            related_instance_ids_json=draft.related_instance_ids_json,
            snapshot_sha256=snapshot_sha256,
            idempotency_key_hash=idempotency_hash,
            status=ReimbursementSubmissionStatus.QUEUED.value,
            resume_status=None,
            status_version=1,
            attempt_count=0,
            reconciliation_attempt_count=0,
            next_attempt_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )
        database.add(submission)
        database.flush()
        database.commit()
        database.expire_all()
    except IntegrityError as exc:
        database.rollback()
        existing = _owned_submission_for_draft(
            database,
            actor=actor,
            draft_id=normalized_draft_id,
        )
        if existing is None:
            raise ReimbursementSubmissionConflict("submission creation lost its draft CAS") from exc
        return _create_result(existing, created=False, idempotency_hash=idempotency_hash)
    except Exception:
        database.rollback()
        raise

    return SubmissionCreateResult(
        submission_id=submission_id,
        status=ReimbursementSubmissionStatus.QUEUED.value,
        status_version=1,
        created=True,
        idempotency_matched=True,
    )


def create_or_get_submission(*args, **kwargs) -> SubmissionCreateResult:
    """Compatibility alias for orchestration code that names the idempotent behavior."""

    return create_submission(*args, **kwargs)


def require_owned_submission(
    database: Session,
    *,
    actor: DraftActor,
    submission_id: str,
) -> ReimbursementSubmission:
    _require_actor(actor)
    normalized_id = _required_text(submission_id, maximum=36)
    submission = database.scalar(
        select(ReimbursementSubmission).where(
            ReimbursementSubmission.id == normalized_id,
            ReimbursementSubmission.corp_id == actor.corp_id,
            ReimbursementSubmission.originator_user_id == actor.user_id,
        )
    )
    if submission is None:
        raise ApiError("REIMBURSEMENT_SUBMISSION_NOT_FOUND", "提交记录不存在", 404)
    if (
        submission.department_id != actor.department_id
        or submission.department_name != actor.department_name
    ):
        raise ApiError(
            "REIMBURSEMENT_SUBMISSION_DEPARTMENT_MISMATCH",
            "提交记录所属部门与当前选择不同，请切换部门后重试",
            409,
        )
    return submission


def find_owned_submission_for_draft(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
) -> ReimbursementSubmission | None:
    """Find the one-shot submission before rebuilding a now-locked snapshot."""

    _require_actor(actor)
    normalized_draft_id = _required_text(draft_id, maximum=36)
    return _owned_submission_for_draft(
        database,
        actor=actor,
        draft_id=normalized_draft_id,
    )


def claim_due_submission(
    database: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> SubmissionClaim | None:
    """Optimistically claim one durable job due for work.

    An attempt is counted when a worker acquires a lease. A retryable record is
    restored to its persisted ``resume_status`` in the same CAS update.
    """

    normalized_worker = _required_text(worker_id, maximum=128)
    _require_positive_integer(lease_seconds, name="lease_seconds")
    claimed_at = _naive_utc(now) if now is not None else utc_now()
    recover_expired_submission_leases(database, now=claimed_at)

    for _ in range(32):
        candidate = database.scalar(
            select(ReimbursementSubmission)
            .where(
                ReimbursementSubmission.status.in_(_CLAIMABLE_STATUSES),
                or_(
                    ReimbursementSubmission.next_attempt_at.is_(None),
                    ReimbursementSubmission.next_attempt_at <= claimed_at,
                ),
                or_(
                    ReimbursementSubmission.lease_expires_at.is_(None),
                    ReimbursementSubmission.lease_expires_at <= claimed_at,
                ),
            )
            .order_by(
                ReimbursementSubmission.next_attempt_at,
                ReimbursementSubmission.created_at,
                ReimbursementSubmission.id,
            )
            .limit(1)
        )
        if candidate is None:
            database.rollback()
            return None
        old_status = candidate.status
        effective_status = (
            candidate.resume_status
            if old_status == ReimbursementSubmissionStatus.FAILED_RETRYABLE.value
            else old_status
        )
        if effective_status not in _CLAIMABLE_STATUSES or effective_status is None:
            raise ReimbursementSubmissionConflict("submission has an invalid resume status")
        token = new_uuid()
        new_version = candidate.status_version + 1
        values: dict[str, object] = {
            "status": effective_status,
            "resume_status": None,
            "status_version": new_version,
            "attempt_count": candidate.attempt_count + 1,
            "next_attempt_at": None,
            "lease_owner": normalized_worker,
            "lease_token": token,
            "lease_expires_at": claimed_at + timedelta(seconds=lease_seconds),
            "updated_at": claimed_at,
        }
        if effective_status == ReimbursementSubmissionStatus.RECONCILING.value:
            values["reconciliation_attempt_count"] = candidate.reconciliation_attempt_count + 1
        result = database.execute(
            update(ReimbursementSubmission)
            .where(
                ReimbursementSubmission.id == candidate.id,
                ReimbursementSubmission.corp_id == candidate.corp_id,
                ReimbursementSubmission.originator_user_id == candidate.originator_user_id,
                ReimbursementSubmission.status == old_status,
                ReimbursementSubmission.status_version == candidate.status_version,
                or_(
                    ReimbursementSubmission.next_attempt_at.is_(None),
                    ReimbursementSubmission.next_attempt_at <= claimed_at,
                ),
                or_(
                    ReimbursementSubmission.lease_expires_at.is_(None),
                    ReimbursementSubmission.lease_expires_at <= claimed_at,
                ),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            database.rollback()
            database.expire_all()
            continue
        database.commit()
        database.expire_all()
        lease = SubmissionLease(
            corp_id=candidate.corp_id,
            user_id=candidate.originator_user_id,
            submission_id=candidate.id,
            lease_token=token,
            expected_status_version=new_version,
        )
        return SubmissionClaim(
            lease=lease,
            status=effective_status,
            snapshot_version=candidate.snapshot_version,
            form_snapshot_json=candidate.form_snapshot_json,
            oa_request_json=candidate.oa_request_json,
        )
    raise ReimbursementSubmissionConflict("could not claim a due submission after contention")


def renew_submission_lease(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> SubmissionLease:
    normalized_worker = _required_text(worker_id, maximum=128)
    _require_positive_integer(lease_seconds, name="lease_seconds")
    renewed_at = _naive_utc(now) if now is not None else utc_now()
    new_version = lease.expected_status_version + 1
    result = database.execute(
        update(ReimbursementSubmission)
        .where(*_lease_conditions(lease, worker_id=normalized_worker, now=renewed_at))
        .values(
            status_version=new_version,
            lease_expires_at=renewed_at + timedelta(seconds=lease_seconds),
            updated_at=renewed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        database.rollback()
        raise ReimbursementSubmissionConflict("submission lease is stale or expired")
    database.commit()
    database.expire_all()
    return _updated_lease(lease, expected_status_version=new_version)


def release_submission_lease(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    next_attempt_at: datetime | None = None,
    now: datetime | None = None,
) -> int:
    normalized_worker = _required_text(worker_id, maximum=128)
    released_at = _naive_utc(now) if now is not None else utc_now()
    next_attempt = _naive_utc(next_attempt_at) if next_attempt_at is not None else None
    new_version = lease.expected_status_version + 1
    result = database.execute(
        update(ReimbursementSubmission)
        .where(*_lease_conditions(lease, worker_id=normalized_worker, now=released_at))
        .values(
            status_version=new_version,
            next_attempt_at=next_attempt,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            updated_at=released_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        database.rollback()
        raise ReimbursementSubmissionConflict("submission lease is stale or expired")
    database.commit()
    database.expire_all()
    return new_version


def advance_submission(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    to_status: ReimbursementSubmissionStatus,
    now: datetime | None = None,
) -> SubmissionLease:
    if not isinstance(to_status, ReimbursementSubmissionStatus):
        raise ValueError("to_status is invalid")
    changed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
    )
    if to_status.value not in _NORMAL_TRANSITIONS.get(current.status, set()):
        raise ValueError(f"unsafe submission transition {current.status} -> {to_status.value}")
    return _leased_submission_update(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "status": to_status.value,
            "resume_status": None,
            "next_attempt_at": None,
            "last_error_code": None,
            "last_error_message": None,
        },
    )


def checkpoint_oa_create(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    oa_request_json: str,
    oa_request_hash: str,
    reconciliation_deadline_at: datetime,
    now: datetime | None = None,
) -> SubmissionLease:
    """Persist the exact OA command before invoking the non-idempotent create API."""

    started_at = _naive_utc(now) if now is not None else utc_now()
    deadline = _naive_utc(reconciliation_deadline_at)
    if deadline <= started_at:
        raise ValueError("reconciliation_deadline_at must be in the future")
    canonical_request = _validated_json_snapshot(
        oa_request_json,
        oa_request_hash,
        field_name="OA request",
    )
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=started_at,
    )
    if current.status != ReimbursementSubmissionStatus.UPLOADING.value:
        raise ValueError("OA create can only start after uploads complete")
    uploads = database.scalars(
        select(ReimbursementUpload).where(
            ReimbursementUpload.submission_id == current.id,
            ReimbursementUpload.draft_id == current.draft_id,
        )
    ).all()
    if not uploads or not any(
        item.role == ReimbursementUploadRole.GENERATED_EXCEL.value for item in uploads
    ):
        raise ReimbursementSubmissionConflict(
            "OA create requires an original/generated upload manifest"
        )
    _require_bundle_manifest(current, uploads)
    if any(item.upload_status != ReimbursementUploadStatus.COMMITTED.value for item in uploads):
        raise ReimbursementSubmissionConflict("OA create requires every upload to be committed")
    return _leased_submission_update(
        database,
        lease=lease,
        worker_id=worker_id,
        now=started_at,
        values={
            "status": ReimbursementSubmissionStatus.OA_CREATING.value,
            "oa_create_started_at": started_at,
            "oa_request_json": canonical_request,
            "oa_request_hash": oa_request_hash,
            "reconciliation_deadline_at": deadline,
            "next_attempt_at": None,
            "last_error_code": None,
            "last_error_message": None,
        },
    )


def mark_oa_create_uncertain(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> SubmissionLease:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
    )
    if current.status != ReimbursementSubmissionStatus.OA_CREATING.value:
        raise ValueError("only OA_CREATING can enter reconciliation")
    return _leased_submission_update(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "status": ReimbursementSubmissionStatus.RECONCILING.value,
            "next_attempt_at": None,
            "last_error_code": _required_text(error_code, maximum=128),
            "last_error_message": _required_text(error_message, maximum=500),
        },
    )


def record_oa_instance(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    process_instance_id: str,
    now: datetime | None = None,
) -> SubmissionLease:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
    )
    if current.status not in {
        ReimbursementSubmissionStatus.OA_CREATING.value,
        ReimbursementSubmissionStatus.RECONCILING.value,
    }:
        raise ValueError("OA instance can only be recorded after create or reconciliation")
    return _leased_submission_update(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "status": ReimbursementSubmissionStatus.VERIFYING.value,
            "process_instance_id": _required_text(process_instance_id, maximum=128),
            "next_attempt_at": None,
            "last_error_code": None,
            "last_error_message": None,
        },
    )


def mark_submission_submitted(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    business_id: str,
    approval_url: str | None = None,
    now: datetime | None = None,
) -> int:
    submitted_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=submitted_at,
    )
    if current.status != ReimbursementSubmissionStatus.VERIFYING.value:
        raise ValueError("only a verified OA can be submitted")
    uploads = database.scalars(
        select(ReimbursementUpload).where(
            ReimbursementUpload.submission_id == current.id,
            ReimbursementUpload.draft_id == current.draft_id,
        )
    ).all()
    if not uploads or any(
        item.upload_status != ReimbursementUploadStatus.LINKED.value for item in uploads
    ):
        raise ReimbursementSubmissionConflict(
            "submission cannot finish before every upload is linked"
        )
    _require_bundle_manifest(current, uploads)
    values: dict[str, object] = {
        "business_id": _required_text(business_id, maximum=128),
        "approval_url": _required_text(approval_url, maximum=2048),
        "submitted_at": submitted_at,
        "last_error_code": None,
        "last_error_message": None,
    }
    return _finish_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=submitted_at,
        status=ReimbursementSubmissionStatus.SUBMITTED,
        values=values,
    )


def fail_submission_retryable(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    next_attempt_at: datetime,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> int:
    failed_at = _naive_utc(now) if now is not None else utc_now()
    retry_at = _naive_utc(next_attempt_at)
    if retry_at <= failed_at:
        raise ValueError("next_attempt_at must be in the future")
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=failed_at,
    )
    if current.status not in _RETRYABLE_STATUSES:
        raise ValueError(f"{current.status} is not a safely retryable submission phase")
    return _finish_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=failed_at,
        status=ReimbursementSubmissionStatus.FAILED_RETRYABLE,
        values={
            "resume_status": current.status,
            "next_attempt_at": retry_at,
            "last_error_code": _required_text(error_code, maximum=128),
            "last_error_message": _required_text(error_message, maximum=500),
        },
    )


def fail_submission_final(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> int:
    failed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=failed_at,
    )
    if current.status in {
        ReimbursementSubmissionStatus.OA_CREATING.value,
        ReimbursementSubmissionStatus.RECONCILING.value,
        ReimbursementSubmissionStatus.VERIFYING.value,
    }:
        raise ValueError("an OA create result cannot be collapsed into final failure")
    unsafe_remote = database.scalar(
        select(ReimbursementUpload.id)
        .where(
            ReimbursementUpload.submission_id == current.id,
            ReimbursementUpload.upload_status.not_in(
                {
                    ReimbursementUploadStatus.PENDING.value,
                    ReimbursementUploadStatus.PUTTING.value,
                    ReimbursementUploadStatus.PUT_DONE.value,
                    ReimbursementUploadStatus.CLEANED.value,
                    ReimbursementUploadStatus.DISCARDED.value,
                }
            ),
        )
        .limit(1)
    )
    if unsafe_remote is not None:
        raise ReimbursementSubmissionConflict(
            "committed or uncertain remote files require cleanup or manual review"
        )
    return _finish_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=failed_at,
        status=ReimbursementSubmissionStatus.FAILED_FINAL,
        values={
            "last_error_code": _required_text(error_code, maximum=128),
            "last_error_message": _required_text(error_message, maximum=500),
        },
    )


def mark_submission_manual_review(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> int:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
    )
    if current.status in _TERMINAL_STATUSES:
        raise ValueError("terminal submission cannot enter manual review")
    return _finish_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        status=ReimbursementSubmissionStatus.MANUAL_REVIEW,
        values={
            "last_error_code": _required_text(error_code, maximum=128),
            "last_error_message": _required_text(error_message, maximum=500),
        },
    )


def begin_orphan_cleanup(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    confirmation_code: str,
    confirmed_by_user_id: str | None = None,
    now: datetime | None = None,
) -> SubmissionLease:
    confirmed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=confirmed_at,
    )
    if current.status not in {
        ReimbursementSubmissionStatus.UPLOADING.value,
        ReimbursementSubmissionStatus.OA_CREATING.value,
        ReimbursementSubmissionStatus.RECONCILING.value,
    }:
        raise ValueError("orphan cleanup requires a confirmed pre-instance state")
    uncertain_upload = database.scalar(
        select(ReimbursementUpload.id)
        .where(
            ReimbursementUpload.submission_id == current.id,
            ReimbursementUpload.upload_status.in_(
                {
                    ReimbursementUploadStatus.COMMITTING.value,
                    ReimbursementUploadStatus.COMMIT_UNCERTAIN.value,
                }
            ),
        )
        .limit(1)
    )
    if uncertain_upload is not None:
        raise ReimbursementSubmissionConflict(
            "an uncertain file commit cannot enter automatic cleanup"
        )
    values: dict[str, object] = {
        "status": ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
        "orphan_confirmed_at": confirmed_at,
        "orphan_confirmation_code": _required_text(confirmation_code, maximum=128),
        "last_error_code": None,
        "last_error_message": None,
    }
    if confirmed_by_user_id is not None:
        values["orphan_confirmed_by_user_id"] = _required_text(
            confirmed_by_user_id,
            maximum=128,
        )
    return _leased_submission_update(
        database,
        lease=lease,
        worker_id=worker_id,
        now=confirmed_at,
        values=values,
    )


def complete_orphan_cleanup(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> int:
    completed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=completed_at,
    )
    if current.status != ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value:
        raise ValueError("only ORPHAN_CLEANUP can finish cleanup")
    outstanding = database.scalar(
        select(ReimbursementUpload.id)
        .where(
            ReimbursementUpload.submission_id == current.id,
            ReimbursementUpload.upload_status.not_in(
                _FAILED_FINAL_LOCAL_DISCARD_STATUSES
                | {
                    ReimbursementUploadStatus.CLEANED.value,
                    ReimbursementUploadStatus.DISCARDED.value,
                }
            ),
        )
        .limit(1)
    )
    if outstanding is not None:
        raise ReimbursementSubmissionConflict("orphan cleanup still has live remote files")
    return _finish_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=completed_at,
        status=ReimbursementSubmissionStatus.FAILED_FINAL,
        values={
            "last_error_code": _required_text(error_code, maximum=128),
            "last_error_message": _required_text(error_message, maximum=500),
        },
    )


def begin_upload_put(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> UploadCheckpoint:
    """Start or safely restart the idempotent binary PUT step."""

    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.UPLOADING,
        now=changed_at,
    )
    if upload.upload_status not in {
        ReimbursementUploadStatus.PENDING.value,
        ReimbursementUploadStatus.PUTTING.value,
        ReimbursementUploadStatus.PUT_DONE.value,
    }:
        raise ValueError("upload PUT can only start from PENDING or restart PUTTING/PUT_DONE")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "upload_status": ReimbursementUploadStatus.PUTTING.value,
            "put_started_at": changed_at,
            "attempt_count": upload.attempt_count + 1,
            "last_error_code": None,
        },
    )


def record_upload_put_done(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> UploadCheckpoint:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.UPLOADING,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.PUTTING.value:
        raise ValueError("upload PUT completion requires PUTTING")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={"upload_status": ReimbursementUploadStatus.PUT_DONE.value},
    )


def begin_upload_commit(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> UploadCheckpoint:
    """Persist COMMITTING before calling the non-idempotent Storage commit API."""

    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.UPLOADING,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.PUT_DONE.value:
        raise ValueError("file commit can only start after PUT_DONE")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "upload_status": ReimbursementUploadStatus.COMMITTING.value,
            "commit_started_at": changed_at,
        },
    )


def reset_rejected_upload_commit(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    error_code: str,
    now: datetime | None = None,
) -> UploadCheckpoint:
    """Reset only a definitively rejected commit so a new ticket may be used."""

    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.UPLOADING,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.COMMITTING.value:
        raise ValueError("only a definitively rejected COMMITTING upload can reset")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "upload_status": ReimbursementUploadStatus.PENDING.value,
            "put_started_at": None,
            "commit_started_at": None,
            "space_id": None,
            "file_id": None,
            "last_error_code": _required_text(error_code, maximum=128),
        },
    )


def record_upload_committed(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    space_id: str,
    file_id: str,
    file_name: str,
    file_size: int,
    file_type: str,
    now: datetime | None = None,
) -> UploadCheckpoint:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.UPLOADING,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.COMMITTING.value:
        raise ValueError("file commit result requires COMMITTING")
    if (
        isinstance(file_size, bool)
        or not isinstance(file_size, int)
        or file_size != upload.size_bytes
    ):
        raise ValueError("committed file size differs from the locked local bytes")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "upload_status": ReimbursementUploadStatus.COMMITTED.value,
            "space_id": _required_text(space_id, maximum=128),
            "file_id": _required_text(file_id, maximum=128),
            "file_name": _required_text(file_name, maximum=255),
            "file_type": _required_text(file_type, maximum=32),
            "last_error_code": None,
        },
    )


def mark_upload_commit_uncertain(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> int:
    """Stop all automation when a Storage commit response is unknowable."""

    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.UPLOADING,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.COMMITTING.value:
        raise ValueError("only COMMITTING can become COMMIT_UNCERTAIN")
    normalized_code = _required_text(error_code, maximum=128)
    normalized_message = _required_text(error_message, maximum=500)
    new_submission_version = lease.expected_status_version + 1
    try:
        parent_result = database.execute(
            update(ReimbursementSubmission)
            .where(*_lease_conditions(lease, worker_id=worker_id, now=changed_at))
            .values(
                status=ReimbursementSubmissionStatus.MANUAL_REVIEW.value,
                resume_status=None,
                status_version=new_submission_version,
                next_attempt_at=None,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=normalized_code,
                last_error_message=normalized_message,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if parent_result.rowcount != 1:
            raise ReimbursementSubmissionConflict("submission lease is stale or expired")
        upload_result = database.execute(
            update(ReimbursementUpload)
            .where(
                ReimbursementUpload.id == upload.id,
                ReimbursementUpload.submission_id == lease.submission_id,
                ReimbursementUpload.status_version == expected_status_version,
                ReimbursementUpload.upload_status == ReimbursementUploadStatus.COMMITTING.value,
            )
            .values(
                upload_status=ReimbursementUploadStatus.COMMIT_UNCERTAIN.value,
                status_version=expected_status_version + 1,
                last_error_code=normalized_code,
                updated_at=changed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if upload_result.rowcount != 1:
            raise ReimbursementSubmissionConflict("upload checkpoint changed")
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    return new_submission_version


def mark_upload_linked(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> UploadCheckpoint:
    linked_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.VERIFYING,
        now=linked_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.COMMITTED.value:
        raise ValueError("only a committed file can be linked")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=linked_at,
        values={
            "upload_status": ReimbursementUploadStatus.LINKED.value,
            "linked_at": linked_at,
        },
    )


def begin_upload_cleanup(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> UploadCheckpoint:
    """Persist CLEANUP_PENDING before the non-idempotent remote recycle/delete."""

    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.COMMITTED.value:
        raise ValueError("only a committed orphan file can enter cleanup")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "upload_status": ReimbursementUploadStatus.CLEANUP_PENDING.value,
            "cleanup_started_at": changed_at,
        },
    )


def record_upload_cleaned(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> UploadCheckpoint:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.ORPHAN_CLEANUP,
        now=changed_at,
    )
    if upload.upload_status != ReimbursementUploadStatus.CLEANUP_PENDING.value:
        raise ValueError("remote cleanup completion requires CLEANUP_PENDING")
    return _upload_update(
        database,
        upload=upload,
        lease=lease,
        worker_id=worker_id,
        now=changed_at,
        values={
            "upload_status": ReimbursementUploadStatus.CLEANED.value,
            "cleaned_at": changed_at,
        },
    )


def remove_discarded_generated_upload(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> bool:
    """Remove a terminal failed generated-file reservation before its retry."""

    changed_at = _naive_utc(now) if now is not None else utc_now()
    upload = _require_leased_upload(
        database,
        lease=lease,
        worker_id=worker_id,
        upload_id=upload_id,
        expected_status_version=expected_status_version,
        submission_status=ReimbursementSubmissionStatus.GENERATING_EXCEL,
        now=changed_at,
    )
    if (
        upload.role
        not in {
            ReimbursementUploadRole.GENERATED_EXCEL.value,
            ReimbursementUploadRole.GENERATED_PDF.value,
        }
        or upload.upload_status != ReimbursementUploadStatus.DISCARDED.value
        or upload.local_status != ReimbursementUploadLocalStatus.DELETED.value
        or upload.local_deleted_at is None
        or upload.space_id is not None
        or upload.file_id is not None
    ):
        raise ReimbursementSubmissionConflict(
            "only a fully discarded local generated upload can be replaced"
        )
    try:
        result = database.execute(
            delete(ReimbursementUpload).where(
                ReimbursementUpload.id == upload.id,
                ReimbursementUpload.submission_id == lease.submission_id,
                ReimbursementUpload.status_version == expected_status_version,
                ReimbursementUpload.role == upload.role,
                ReimbursementUpload.upload_status == ReimbursementUploadStatus.DISCARDED.value,
                ReimbursementUpload.local_status == ReimbursementUploadLocalStatus.DELETED.value,
                ReimbursementUpload.local_deleted_at.is_not(None),
                ReimbursementUpload.space_id.is_(None),
                ReimbursementUpload.file_id.is_(None),
                exists(
                    select(ReimbursementSubmission.id).where(
                        *_lease_conditions(lease, worker_id=worker_id, now=changed_at),
                        ReimbursementSubmission.status
                        == ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
                    )
                ),
            )
        )
        if result.rowcount != 1:
            raise ReimbursementSubmissionConflict(
                "discarded generated upload changed before replacement"
            )
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    return True


def list_linked_local_release_candidates(
    database: Session,
    *,
    corp_id: str,
    user_id: str,
    submission_id: str,
) -> tuple[LinkedLocalReleaseCandidate, ...]:
    """Snapshot safe local-delete work without holding a database transaction."""

    normalized_corp = _required_text(corp_id, maximum=128)
    normalized_user = _required_text(user_id, maximum=128)
    normalized_submission = _required_text(submission_id, maximum=36)
    records = database.scalars(
        select(ReimbursementUpload)
        .join(
            ReimbursementSubmission,
            ReimbursementSubmission.id == ReimbursementUpload.submission_id,
        )
        .where(
            ReimbursementSubmission.id == normalized_submission,
            ReimbursementSubmission.corp_id == normalized_corp,
            ReimbursementSubmission.originator_user_id == normalized_user,
            ReimbursementSubmission.process_instance_id.is_not(None),
            ReimbursementUpload.upload_status == ReimbursementUploadStatus.LINKED.value,
            ReimbursementUpload.local_status == ReimbursementUploadLocalStatus.READY.value,
            ReimbursementUpload.size_bytes.is_not(None),
            ReimbursementUpload.sha256.is_not(None),
        )
        .order_by(ReimbursementUpload.sort_order, ReimbursementUpload.id)
    ).all()
    candidates = tuple(
        LinkedLocalReleaseCandidate(
            corp_id=normalized_corp,
            user_id=normalized_user,
            submission_id=record.submission_id,
            upload_id=record.id,
            upload_status_version=record.status_version,
            role=record.role,
            local_storage_key=record.local_storage_key,
            size_bytes=int(record.size_bytes),
            sha256=str(record.sha256),
            source_draft_file_id=record.source_draft_file_id,
        )
        for record in records
    )
    database.rollback()
    return candidates


def list_due_linked_local_release_candidates(
    database: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[LinkedLocalReleaseCandidate, ...]:
    """Return bounded restart-safe local cleanup work across submissions.

    A FAILED_FINAL row is eligible only when the OA-create checkpoint was never
    written. Together with a missing remote identity and a pre-commit upload
    status, that is durable evidence that no approval attachment was created.
    """

    _require_positive_integer(limit, name="limit")
    if limit > 1000:
        raise ValueError("limit must not exceed 1000")
    release_at = _naive_utc(now) if now is not None else utc_now()
    rows = database.execute(
        select(ReimbursementUpload, ReimbursementSubmission)
        .join(
            ReimbursementSubmission,
            ReimbursementSubmission.id == ReimbursementUpload.submission_id,
        )
        .join(
            ReimbursementDraft,
            ReimbursementDraft.id == ReimbursementSubmission.draft_id,
        )
        .where(
            or_(
                and_(
                    ReimbursementSubmission.process_instance_id.is_not(None),
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.LINKED.value,
                ),
                and_(
                    ReimbursementSubmission.process_instance_id.is_(None),
                    ReimbursementSubmission.status.in_(
                        {
                            ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
                            ReimbursementSubmissionStatus.FAILED_FINAL.value,
                        }
                    ),
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.CLEANED.value,
                ),
                and_(
                    ReimbursementSubmission.process_instance_id.is_(None),
                    ReimbursementSubmission.oa_create_started_at.is_(None),
                    ReimbursementSubmission.status
                    == ReimbursementSubmissionStatus.FAILED_FINAL.value,
                    ReimbursementDraft.status == ReimbursementDraftStatus.LOCKED.value,
                    ReimbursementDraft.locked_at.is_not(None),
                    ReimbursementDraft.expires_at <= release_at,
                    ReimbursementUpload.upload_status.in_(_FAILED_FINAL_LOCAL_DISCARD_STATUSES),
                    ReimbursementUpload.space_id.is_(None),
                    ReimbursementUpload.file_id.is_(None),
                    ReimbursementUpload.commit_started_at.is_(None),
                ),
            ),
            ReimbursementUpload.local_status == ReimbursementUploadLocalStatus.READY.value,
            ReimbursementUpload.size_bytes.is_not(None),
            ReimbursementUpload.sha256.is_not(None),
        )
        .order_by(ReimbursementUpload.linked_at, ReimbursementUpload.id)
        .limit(limit)
    ).all()
    candidates = tuple(
        LinkedLocalReleaseCandidate(
            corp_id=submission.corp_id,
            user_id=submission.originator_user_id,
            submission_id=upload.submission_id,
            upload_id=upload.id,
            upload_status_version=upload.status_version,
            role=upload.role,
            local_storage_key=upload.local_storage_key,
            size_bytes=int(upload.size_bytes),
            sha256=str(upload.sha256),
            source_draft_file_id=upload.source_draft_file_id,
        )
        for upload, submission in rows
    )
    database.rollback()
    return candidates


def finalize_local_upload_release(
    database: Session,
    *,
    corp_id: str,
    user_id: str,
    submission_id: str,
    upload_id: str,
    expected_status_version: int,
    now: datetime | None = None,
) -> int:
    """Record a safe local physical deletion and its manifest disposition.

    ORIGINAL manifests share their object with a draft file. Both accounting
    rows are therefore released in the same transaction after the caller has
    deleted the object outside the SQLite write lock. Expired pre-commit files
    from a FAILED_FINAL submission become DISCARDED; successful OA-linked and
    remotely cleaned manifests retain their remote disposition.
    """

    _require_positive_integer(expected_status_version, name="expected_status_version")
    deleted_at = _naive_utc(now) if now is not None else utc_now()
    normalized_corp = _required_text(corp_id, maximum=128)
    normalized_user = _required_text(user_id, maximum=128)
    normalized_submission = _required_text(submission_id, maximum=36)
    normalized_upload = _required_text(upload_id, maximum=36)
    upload = database.scalar(
        select(ReimbursementUpload)
        .join(
            ReimbursementSubmission,
            ReimbursementSubmission.id == ReimbursementUpload.submission_id,
        )
        .join(
            ReimbursementDraft,
            ReimbursementDraft.id == ReimbursementSubmission.draft_id,
        )
        .where(
            ReimbursementUpload.id == normalized_upload,
            ReimbursementUpload.submission_id == normalized_submission,
            ReimbursementUpload.status_version == expected_status_version,
            ReimbursementUpload.local_status == ReimbursementUploadLocalStatus.READY.value,
            ReimbursementSubmission.corp_id == normalized_corp,
            ReimbursementSubmission.originator_user_id == normalized_user,
            or_(
                and_(
                    ReimbursementSubmission.process_instance_id.is_not(None),
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.LINKED.value,
                ),
                and_(
                    ReimbursementSubmission.process_instance_id.is_(None),
                    ReimbursementSubmission.status.in_(
                        {
                            ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
                            ReimbursementSubmissionStatus.FAILED_FINAL.value,
                        }
                    ),
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.CLEANED.value,
                ),
                and_(
                    ReimbursementSubmission.process_instance_id.is_(None),
                    ReimbursementSubmission.oa_create_started_at.is_(None),
                    ReimbursementSubmission.status
                    == ReimbursementSubmissionStatus.FAILED_FINAL.value,
                    ReimbursementDraft.status == ReimbursementDraftStatus.LOCKED.value,
                    ReimbursementDraft.locked_at.is_not(None),
                    ReimbursementDraft.expires_at <= deleted_at,
                    ReimbursementUpload.upload_status.in_(_FAILED_FINAL_LOCAL_DISCARD_STATUSES),
                    ReimbursementUpload.space_id.is_(None),
                    ReimbursementUpload.file_id.is_(None),
                    ReimbursementUpload.commit_started_at.is_(None),
                ),
            ),
        )
    )
    if upload is None:
        database.rollback()
        # A duplicate post-delete finalization is intentionally idempotent.
        completed = database.scalar(
            select(ReimbursementUpload)
            .join(
                ReimbursementSubmission,
                ReimbursementSubmission.id == ReimbursementUpload.submission_id,
            )
            .join(
                ReimbursementDraft,
                ReimbursementDraft.id == ReimbursementSubmission.draft_id,
            )
            .where(
                ReimbursementUpload.id == normalized_upload,
                ReimbursementUpload.submission_id == normalized_submission,
                ReimbursementUpload.status_version == expected_status_version + 1,
                ReimbursementUpload.local_status == ReimbursementUploadLocalStatus.DELETED.value,
                ReimbursementSubmission.corp_id == normalized_corp,
                ReimbursementSubmission.originator_user_id == normalized_user,
                or_(
                    and_(
                        ReimbursementSubmission.process_instance_id.is_not(None),
                        ReimbursementUpload.upload_status == ReimbursementUploadStatus.LINKED.value,
                    ),
                    and_(
                        ReimbursementSubmission.process_instance_id.is_(None),
                        ReimbursementSubmission.status.in_(
                            {
                                ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
                                ReimbursementSubmissionStatus.FAILED_FINAL.value,
                            }
                        ),
                        ReimbursementUpload.upload_status
                        == ReimbursementUploadStatus.CLEANED.value,
                    ),
                    and_(
                        ReimbursementSubmission.process_instance_id.is_(None),
                        ReimbursementSubmission.oa_create_started_at.is_(None),
                        ReimbursementSubmission.status
                        == ReimbursementSubmissionStatus.FAILED_FINAL.value,
                        ReimbursementDraft.status == ReimbursementDraftStatus.LOCKED.value,
                        ReimbursementDraft.locked_at.is_not(None),
                        ReimbursementDraft.expires_at <= deleted_at,
                        ReimbursementUpload.commit_started_at.is_(None),
                        ReimbursementUpload.upload_status
                        == ReimbursementUploadStatus.DISCARDED.value,
                        ReimbursementUpload.space_id.is_(None),
                        ReimbursementUpload.file_id.is_(None),
                    ),
                ),
            )
        )
        if completed is not None:
            return completed.status_version
        raise ReimbursementSubmissionConflict("local release candidate changed")
    try:
        if upload.source_draft_file_id is not None:
            source_result = database.execute(
                update(ReimbursementDraftFile)
                .where(
                    ReimbursementDraftFile.id == upload.source_draft_file_id,
                    ReimbursementDraftFile.draft_id == upload.draft_id,
                    ReimbursementDraftFile.storage_key == upload.local_storage_key,
                    ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                    ReimbursementDraftFile.size_bytes == upload.size_bytes,
                    ReimbursementDraftFile.sha256 == upload.sha256,
                )
                .values(
                    file_status=ReimbursementDraftFileStatus.PURGED.value,
                    part_storage_key=None,
                    reservation_expires_at=None,
                    purged_at=deleted_at,
                    updated_at=deleted_at,
                )
                .execution_options(synchronize_session=False)
            )
            if source_result.rowcount != 1:
                raise ReimbursementSubmissionConflict(
                    "source draft file changed before local release"
                )
        upload_values: dict[str, object] = {
            "local_status": ReimbursementUploadLocalStatus.DELETED.value,
            "local_part_storage_key": None,
            "reservation_expires_at": None,
            "local_deleted_at": deleted_at,
            "status_version": expected_status_version + 1,
            "updated_at": deleted_at,
        }
        update_conditions: list[object] = []
        if upload.upload_status in _FAILED_FINAL_LOCAL_DISCARD_STATUSES:
            upload_values["upload_status"] = ReimbursementUploadStatus.DISCARDED.value
            update_conditions.extend(
                (
                    ReimbursementUpload.space_id.is_(None),
                    ReimbursementUpload.file_id.is_(None),
                    ReimbursementUpload.commit_started_at.is_(None),
                    exists(
                        select(ReimbursementSubmission.id)
                        .join(
                            ReimbursementDraft,
                            ReimbursementDraft.id == ReimbursementSubmission.draft_id,
                        )
                        .where(
                            ReimbursementSubmission.id == upload.submission_id,
                            ReimbursementSubmission.process_instance_id.is_(None),
                            ReimbursementSubmission.oa_create_started_at.is_(None),
                            ReimbursementSubmission.status
                            == ReimbursementSubmissionStatus.FAILED_FINAL.value,
                            ReimbursementDraft.status == ReimbursementDraftStatus.LOCKED.value,
                            ReimbursementDraft.locked_at.is_not(None),
                            ReimbursementDraft.expires_at <= deleted_at,
                        )
                    ),
                )
            )
        result = database.execute(
            update(ReimbursementUpload)
            .where(
                ReimbursementUpload.id == upload.id,
                ReimbursementUpload.submission_id == upload.submission_id,
                ReimbursementUpload.status_version == expected_status_version,
                ReimbursementUpload.upload_status == upload.upload_status,
                ReimbursementUpload.local_status == ReimbursementUploadLocalStatus.READY.value,
                *update_conditions,
            )
            .values(**upload_values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ReimbursementSubmissionConflict("upload changed during local release")
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    return expected_status_version + 1


def _bundled_source_release_query():
    # A source is not a remote upload. Delete it only after the complete immutable
    # two-file manifest has been read back, linked and marked SUBMITTED.
    return (
        select(ReimbursementDraftFile, ReimbursementSubmission)
        .join(
            ReimbursementSubmission,
            ReimbursementSubmission.draft_id == ReimbursementDraftFile.draft_id,
        )
        .where(
            ReimbursementSubmission.status == ReimbursementSubmissionStatus.SUBMITTED.value,
            ReimbursementSubmission.process_instance_id.is_not(None),
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
            ReimbursementDraftFile.size_bytes.is_not(None),
            ReimbursementDraftFile.sha256.is_not(None),
            ~exists(
                select(ReimbursementUpload.id).where(
                    ReimbursementUpload.submission_id == ReimbursementSubmission.id,
                    ReimbursementUpload.upload_status != ReimbursementUploadStatus.LINKED.value,
                )
            ),
            exists(
                select(ReimbursementUpload.id).where(
                    ReimbursementUpload.submission_id == ReimbursementSubmission.id,
                    ReimbursementUpload.role == ReimbursementUploadRole.GENERATED_PDF.value,
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.LINKED.value,
                )
            ),
            exists(
                select(ReimbursementUpload.id).where(
                    ReimbursementUpload.submission_id == ReimbursementSubmission.id,
                    ReimbursementUpload.role == ReimbursementUploadRole.GENERATED_EXCEL.value,
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.LINKED.value,
                )
            ),
        )
    )


def list_due_bundled_source_release_candidates(
    database: Session, *, limit: int = 100
) -> tuple[BundledSourceReleaseCandidate, ...]:
    _require_positive_integer(limit, name="limit")
    if limit > 1000:
        raise ValueError("limit must not exceed 1000")
    rows = database.execute(
        _bundled_source_release_query().order_by(ReimbursementDraftFile.id).limit(limit)
    ).all()
    candidates = []
    for source, submission in rows:
        # Only immutable snapshot source identities are covered by the bundle.
        snapshot_files = json.loads(submission.form_snapshot_json).get("originalFiles", [])
        if not any(
            entry.get("draftFileId") == source.id
            and entry.get("storageKey") == source.storage_key
            and entry.get("sizeBytes") == source.size_bytes
            and entry.get("sha256") == source.sha256
            for entry in snapshot_files
        ):
            continue
        candidates.append(
            BundledSourceReleaseCandidate(
                submission.id,
                source.id,
                source.storage_key,
                int(source.size_bytes),
                str(source.sha256),
            )
        )
    database.rollback()
    return tuple(candidates)


def finalize_bundled_source_release(
    database: Session, *, candidate: BundledSourceReleaseCandidate, now: datetime | None = None
) -> None:
    changed_at = _naive_utc(now) if now is not None else utc_now()
    row = database.execute(
        _bundled_source_release_query().where(
            ReimbursementSubmission.id == candidate.submission_id,
            ReimbursementDraftFile.id == candidate.file_id,
            ReimbursementDraftFile.storage_key == candidate.storage_key,
            ReimbursementDraftFile.size_bytes == candidate.size_bytes,
            ReimbursementDraftFile.sha256 == candidate.sha256,
        )
    ).first()
    if row is None:
        database.rollback()
        raise ReimbursementSubmissionConflict("bundled source release candidate changed")
    source, _submission = row
    source.file_status = ReimbursementDraftFileStatus.PURGED.value
    source.part_storage_key = None
    source.reservation_expires_at = None
    source.purged_at = changed_at
    source.updated_at = changed_at
    database.commit()


def recover_expired_submission_leases(
    database: Session,
    *,
    now: datetime | None = None,
) -> SubmissionRecoveryResult:
    """Fence abandoned non-idempotent calls before any job can be reclaimed.

    A lost OA-create response is reconciled; it is never retried as create. A
    lost Storage-commit response is not machine-recoverable with the documented
    API, so both the upload and its parent submission move to manual review.
    """

    cutoff = _naive_utc(now) if now is not None else utc_now()
    reconciliations = 0
    uncertain_uploads = 0
    manual_reviews = 0

    candidates = database.scalars(
        select(ReimbursementSubmission)
        .where(
            ReimbursementSubmission.status.in_(
                {
                    ReimbursementSubmissionStatus.UPLOADING.value,
                    ReimbursementSubmissionStatus.OA_CREATING.value,
                }
            ),
            or_(
                ReimbursementSubmission.lease_expires_at.is_(None),
                ReimbursementSubmission.lease_expires_at <= cutoff,
            ),
        )
        .order_by(ReimbursementSubmission.updated_at, ReimbursementSubmission.id)
    ).all()
    for candidate in candidates:
        if candidate.status == ReimbursementSubmissionStatus.UPLOADING.value:
            committing = database.scalars(
                select(ReimbursementUpload).where(
                    ReimbursementUpload.submission_id == candidate.id,
                    ReimbursementUpload.draft_id == candidate.draft_id,
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.COMMITTING.value,
                )
            ).all()
            if not committing:
                continue
            result = database.execute(
                update(ReimbursementSubmission)
                .where(
                    ReimbursementSubmission.id == candidate.id,
                    ReimbursementSubmission.corp_id == candidate.corp_id,
                    ReimbursementSubmission.originator_user_id == candidate.originator_user_id,
                    ReimbursementSubmission.status == ReimbursementSubmissionStatus.UPLOADING.value,
                    ReimbursementSubmission.status_version == candidate.status_version,
                    or_(
                        ReimbursementSubmission.lease_expires_at.is_(None),
                        ReimbursementSubmission.lease_expires_at <= cutoff,
                    ),
                )
                .values(
                    status=ReimbursementSubmissionStatus.MANUAL_REVIEW.value,
                    resume_status=None,
                    status_version=candidate.status_version + 1,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code="DINGTALK_STORAGE_COMMIT_UNCERTAIN",
                    last_error_message="附件提交结果不确定，需要人工核对",
                    updated_at=cutoff,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                database.rollback()
                database.expire_all()
                continue
            upload_result = database.execute(
                update(ReimbursementUpload)
                .where(
                    ReimbursementUpload.submission_id == candidate.id,
                    ReimbursementUpload.draft_id == candidate.draft_id,
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.COMMITTING.value,
                )
                .values(
                    upload_status=ReimbursementUploadStatus.COMMIT_UNCERTAIN.value,
                    status_version=ReimbursementUpload.status_version + 1,
                    last_error_code="DINGTALK_STORAGE_COMMIT_UNCERTAIN",
                    updated_at=cutoff,
                )
                .execution_options(synchronize_session=False)
            )
            database.commit()
            uncertain_uploads += int(upload_result.rowcount or 0)
            manual_reviews += 1
            database.expire_all()
            continue

        deadline = candidate.reconciliation_deadline_at or cutoff
        result = database.execute(
            update(ReimbursementSubmission)
            .where(
                ReimbursementSubmission.id == candidate.id,
                ReimbursementSubmission.corp_id == candidate.corp_id,
                ReimbursementSubmission.originator_user_id == candidate.originator_user_id,
                ReimbursementSubmission.status == ReimbursementSubmissionStatus.OA_CREATING.value,
                ReimbursementSubmission.status_version == candidate.status_version,
                or_(
                    ReimbursementSubmission.lease_expires_at.is_(None),
                    ReimbursementSubmission.lease_expires_at <= cutoff,
                ),
            )
            .values(
                status=ReimbursementSubmissionStatus.RECONCILING.value,
                resume_status=None,
                status_version=candidate.status_version + 1,
                next_attempt_at=cutoff,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                reconciliation_deadline_at=deadline,
                last_error_code="DINGTALK_OA_CREATE_RESULT_UNKNOWN",
                last_error_message="审批创建结果不确定，正在核对",
                updated_at=cutoff,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            database.rollback()
            database.expire_all()
            continue
        database.commit()
        database.expire_all()
        reconciliations += 1

    return SubmissionRecoveryResult(
        oa_create_reconciliations=reconciliations,
        uncertain_uploads=uncertain_uploads,
        manual_reviews=manual_reviews,
    )


def require_leased_submission(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    now: datetime | None = None,
) -> ReimbursementSubmission:
    observed_at = _naive_utc(now) if now is not None else utc_now()
    return _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=observed_at,
    )


def list_leased_uploads(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    now: datetime | None = None,
) -> tuple[ReimbursementUpload, ...]:
    observed_at = _naive_utc(now) if now is not None else utc_now()
    current = _require_leased_submission(
        database,
        lease=lease,
        worker_id=worker_id,
        now=observed_at,
    )
    return tuple(
        database.scalars(
            select(ReimbursementUpload)
            .where(
                ReimbursementUpload.submission_id == current.id,
                ReimbursementUpload.draft_id == current.draft_id,
            )
            .order_by(ReimbursementUpload.sort_order, ReimbursementUpload.id)
        ).all()
    )


def _require_leased_submission(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    now: datetime,
) -> ReimbursementSubmission:
    normalized_worker = _required_text(worker_id, maximum=128)
    submission = database.scalar(
        select(ReimbursementSubmission).where(
            *_lease_conditions(lease, worker_id=normalized_worker, now=now)
        )
    )
    if submission is None:
        database.rollback()
        raise ReimbursementSubmissionConflict("submission lease is stale or expired")
    return submission


def _require_leased_upload(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    upload_id: str,
    expected_status_version: int,
    submission_status: ReimbursementSubmissionStatus,
    now: datetime,
) -> ReimbursementUpload:
    _require_positive_integer(expected_status_version, name="expected_status_version")
    normalized_upload_id = _required_text(upload_id, maximum=36)
    normalized_worker = _required_text(worker_id, maximum=128)
    upload = database.scalar(
        select(ReimbursementUpload)
        .join(
            ReimbursementSubmission,
            ReimbursementSubmission.id == ReimbursementUpload.submission_id,
        )
        .where(
            ReimbursementUpload.id == normalized_upload_id,
            ReimbursementUpload.submission_id == lease.submission_id,
            ReimbursementUpload.status_version == expected_status_version,
            ReimbursementSubmission.status == submission_status.value,
            *_lease_conditions(lease, worker_id=normalized_worker, now=now),
        )
    )
    if upload is None:
        database.rollback()
        raise ReimbursementSubmissionConflict(
            "upload ownership, status version, or submission lease changed"
        )
    return upload


def _leased_submission_update(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    now: datetime,
    values: dict[str, object],
) -> SubmissionLease:
    normalized_worker = _required_text(worker_id, maximum=128)
    new_version = lease.expected_status_version + 1
    if any(
        forbidden in values
        for forbidden in {
            "id",
            "corp_id",
            "originator_user_id",
            "status_version",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
            "updated_at",
        }
    ):
        raise ValueError("unsafe leased submission update")
    result = database.execute(
        update(ReimbursementSubmission)
        .where(*_lease_conditions(lease, worker_id=normalized_worker, now=now))
        .values(**values, status_version=new_version, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        database.rollback()
        raise ReimbursementSubmissionConflict("submission lease is stale or expired")
    try:
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    return _updated_lease(lease, expected_status_version=new_version)


def _finish_submission(
    database: Session,
    *,
    lease: SubmissionLease,
    worker_id: str,
    now: datetime,
    status: ReimbursementSubmissionStatus,
    values: dict[str, object],
) -> int:
    normalized_worker = _required_text(worker_id, maximum=128)
    new_version = lease.expected_status_version + 1
    payload: dict[str, object] = {
        "status": status.value,
        "resume_status": None,
        "status_version": new_version,
        "next_attempt_at": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "updated_at": now,
    }
    payload.update(values)
    result = database.execute(
        update(ReimbursementSubmission)
        .where(*_lease_conditions(lease, worker_id=normalized_worker, now=now))
        .values(**payload)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        database.rollback()
        raise ReimbursementSubmissionConflict("submission lease is stale or expired")
    try:
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    return new_version


def _upload_update(
    database: Session,
    *,
    upload: ReimbursementUpload,
    lease: SubmissionLease,
    worker_id: str,
    now: datetime,
    values: dict[str, object],
) -> UploadCheckpoint:
    normalized_worker = _required_text(worker_id, maximum=128)
    old_version = upload.status_version
    new_version = old_version + 1
    if any(
        forbidden in values
        for forbidden in {
            "id",
            "submission_id",
            "draft_id",
            "status_version",
            "updated_at",
        }
    ):
        raise ValueError("unsafe upload checkpoint update")
    result = database.execute(
        update(ReimbursementUpload)
        .where(
            ReimbursementUpload.id == upload.id,
            ReimbursementUpload.submission_id == lease.submission_id,
            ReimbursementUpload.status_version == old_version,
            exists(
                select(ReimbursementSubmission.id).where(
                    *_lease_conditions(lease, worker_id=normalized_worker, now=now)
                )
            ),
        )
        .values(**values, status_version=new_version, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        database.rollback()
        raise ReimbursementSubmissionConflict("upload checkpoint changed")
    try:
        database.commit()
        database.expire_all()
    except Exception:
        database.rollback()
        raise
    return UploadCheckpoint(
        upload_id=upload.id,
        upload_status=str(values.get("upload_status", upload.upload_status)),
        status_version=new_version,
    )


def _lease_conditions(
    lease: SubmissionLease,
    *,
    worker_id: str,
    now: datetime,
) -> tuple[object, ...]:
    if not isinstance(lease, SubmissionLease):
        raise ValueError("submission lease is required")
    _require_positive_integer(
        lease.expected_status_version,
        name="expected_status_version",
    )
    return (
        ReimbursementSubmission.id == _required_text(lease.submission_id, maximum=36),
        ReimbursementSubmission.corp_id == _required_text(lease.corp_id, maximum=128),
        ReimbursementSubmission.originator_user_id == _required_text(lease.user_id, maximum=128),
        ReimbursementSubmission.status_version == lease.expected_status_version,
        ReimbursementSubmission.lease_owner == _required_text(worker_id, maximum=128),
        ReimbursementSubmission.lease_token == _required_text(lease.lease_token, maximum=64),
        ReimbursementSubmission.lease_expires_at.is_not(None),
        ReimbursementSubmission.lease_expires_at > now,
    )


def _updated_lease(
    lease: SubmissionLease,
    *,
    expected_status_version: int,
) -> SubmissionLease:
    return SubmissionLease(
        corp_id=lease.corp_id,
        user_id=lease.user_id,
        submission_id=lease.submission_id,
        lease_token=lease.lease_token,
        expected_status_version=expected_status_version,
    )


def _owned_submission_for_draft(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
) -> ReimbursementSubmission | None:
    return database.scalar(
        select(ReimbursementSubmission).where(
            ReimbursementSubmission.draft_id == draft_id,
            ReimbursementSubmission.corp_id == actor.corp_id,
            ReimbursementSubmission.originator_user_id == actor.user_id,
            ReimbursementSubmission.department_id == actor.department_id,
            ReimbursementSubmission.department_name == actor.department_name,
        )
    )


def _create_result(
    submission: ReimbursementSubmission,
    *,
    created: bool,
    idempotency_hash: str,
) -> SubmissionCreateResult:
    return SubmissionCreateResult(
        submission_id=submission.id,
        status=submission.status,
        status_version=submission.status_version,
        created=created,
        idempotency_matched=submission.idempotency_key_hash == idempotency_hash,
    )


def _require_review_ready_draft(
    draft: ReimbursementDraft,
    *,
    expected_revision: int,
    now: datetime,
) -> None:
    if draft.expires_at <= now or draft.status == ReimbursementDraftStatus.EXPIRED.value:
        raise ApiError("REIMBURSEMENT_DRAFT_EXPIRED", "草稿已过期，请重新创建", 409)
    if draft.revision != expected_revision:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
            "报销内容版本已更新，请刷新后重试",
            409,
        )
    if draft.status != ReimbursementDraftStatus.REVIEW_READY.value or draft.locked_at is not None:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_NOT_READY",
            "请先完成检查并确认报销内容",
            409,
        )


def _raise_current_draft_conflict(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    now: datetime,
) -> None:
    current = database.scalar(
        select(ReimbursementDraft).where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
        )
    )
    if current is None:
        raise _draft_not_found_error()
    _require_submission_department(current, actor)
    _require_review_ready_draft(
        current,
        expected_revision=expected_revision,
        now=now,
    )
    raise ReimbursementSubmissionConflict("draft submission CAS changed")


def _require_submission_department(draft: ReimbursementDraft, actor: DraftActor) -> None:
    if draft.department_id != actor.department_id or draft.department_name != actor.department_name:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_DEPARTMENT_MISMATCH",
            "草稿所属部门与当前选择不同，请切换部门后重试",
            409,
        )


def _require_actor(actor: DraftActor) -> None:
    if not isinstance(actor, DraftActor):
        raise ValueError("draft actor is required")
    _required_text(actor.corp_id, maximum=128)
    _required_text(actor.user_id, maximum=128)
    _required_text(actor.department_id, maximum=128)
    _required_text(actor.department_name, maximum=255)


def _idempotency_hash(value: str) -> str:
    normalized = _required_text(value, maximum=255)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validated_json_snapshot(value: str, expected_sha256: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty JSON")
    _require_sha256(expected_sha256)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be valid JSON") from None
    if not isinstance(decoded, dict):
        raise ValueError(f"{field_name} root must be an object")
    actual = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"{field_name} SHA-256 does not match")
    return value


def _require_sha256(value: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")


def _required_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("text value is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(c in normalized for c in "\r\n\x00"):
        raise ValueError("text value is invalid")
    return normalized


def _require_positive_integer(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _naive_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("timestamp is invalid")
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _draft_not_found_error() -> ApiError:
    return ApiError("REIMBURSEMENT_DRAFT_NOT_FOUND", "草稿不存在", 404)
