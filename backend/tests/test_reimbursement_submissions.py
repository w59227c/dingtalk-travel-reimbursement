from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.base import Base
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftStatus,
    ReimbursementSubmission,
    ReimbursementSubmissionStatus,
    ReimbursementUpload,
    ReimbursementUploadLocalStatus,
    ReimbursementUploadRole,
    ReimbursementUploadStatus,
    utc_now,
)
from app.services.reimbursement_drafts import DraftActor
from app.services.reimbursement_submissions import (
    ReimbursementSubmissionConflict,
    advance_submission,
    begin_orphan_cleanup,
    begin_upload_commit,
    begin_upload_put,
    checkpoint_oa_create,
    claim_due_submission,
    complete_orphan_cleanup,
    create_submission,
    fail_submission_retryable,
    finalize_local_upload_release,
    find_owned_submission_for_draft,
    list_due_linked_local_release_candidates,
    mark_oa_create_uncertain,
    mark_submission_submitted,
    mark_upload_commit_uncertain,
    mark_upload_linked,
    record_oa_instance,
    record_upload_committed,
    record_upload_put_done,
    recover_expired_submission_leases,
    remove_discarded_generated_upload,
    renew_submission_lease,
    require_owned_submission,
    reset_rejected_upload_commit,
)

_HASH_A = "a" * 64
_ACTOR = DraftActor(
    corp_id="corp-test",
    user_id="employee-1",
    department_id="department-1",
    department_name="测试部门",
)


@pytest.fixture
def database(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'submission-state.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        yield session
    engine.dispose()


def _new_ready_draft(
    database: Session,
    *,
    actor: DraftActor = _ACTOR,
) -> tuple[ReimbursementDraft, ReimbursementDraftFile]:
    draft = ReimbursementDraft(
        corp_id=actor.corp_id,
        owner_user_id=actor.user_id,
        status=ReimbursementDraftStatus.REVIEW_READY.value,
        revision=4,
        department_id=actor.department_id,
        department_name=actor.department_name,
        template_process_code="PROC-REIMBURSEMENT",
        template_config_version=3,
        schema_fingerprint=_HASH_A,
        input_json='{"companyValue":"北京"}',
        related_instance_ids_json='["travel-1"]',
        expires_at=utc_now() + timedelta(days=7),
    )
    database.add(draft)
    database.flush()
    source = ReimbursementDraftFile(
        draft_id=draft.id,
        sort_order=7,
        processing_role=ReimbursementDraftFileRole.ATTACHMENT_ONLY.value,
        file_status=ReimbursementDraftFileStatus.ACTIVE.value,
        storage_key=f"drafts/{draft.id}/invoice.pdf",
        part_storage_key=None,
        reserved_bytes=2048,
        reservation_expires_at=None,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        size_bytes=1024,
        sha256=_HASH_A,
    )
    database.add(source)
    database.commit()
    return draft, source


def _create(
    database: Session,
    draft: ReimbursementDraft,
    *,
    actor: DraftActor = _ACTOR,
    key: str = "submission-request-0001",
    now=None,
):
    snapshot = '{"snapshotVersion":6,"total":"100.00"}'
    return create_submission(
        database,
        actor=actor,
        draft_id=draft.id,
        expected_revision=4,
        originator_union_id="union-1",
        originator_name="测试员工",
        idempotency_key=key,
        snapshot_version=6,
        schema_fingerprint=_HASH_A,
        form_snapshot_json=snapshot,
        snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
        now=now,
    )


def _claim_to_uploading(database: Session, draft: ReimbursementDraft, *, now):
    result = _create(database, draft, now=now)
    claim = claim_due_submission(
        database,
        worker_id="worker-1",
        lease_seconds=120,
        now=now,
    )
    assert claim is not None
    lease = advance_submission(
        database,
        lease=claim.lease,
        worker_id="worker-1",
        to_status=ReimbursementSubmissionStatus.VALIDATING,
        now=now,
    )
    lease = advance_submission(
        database,
        lease=lease,
        worker_id="worker-1",
        to_status=ReimbursementSubmissionStatus.GENERATING_EXCEL,
        now=now,
    )
    submission = database.get(ReimbursementSubmission, result.submission_id)
    _add_generated_upload(database, submission)
    lease = advance_submission(
        database,
        lease=lease,
        worker_id="worker-1",
        to_status=ReimbursementSubmissionStatus.UPLOADING,
        now=now,
    )
    return result, lease


def _add_generated_upload(
    database: Session,
    submission: ReimbursementSubmission,
    *,
    status: ReimbursementUploadStatus = ReimbursementUploadStatus.PENDING,
) -> ReimbursementUpload:
    existing = database.scalars(
        select(ReimbursementUpload)
        .where(ReimbursementUpload.submission_id == submission.id)
        .order_by(ReimbursementUpload.sort_order)
    ).all()
    if existing:
        bundle, generated = existing
        for index, upload in enumerate((bundle, generated)):
            upload.upload_status = status.value
            if status in {
                ReimbursementUploadStatus.COMMITTED,
                ReimbursementUploadStatus.LINKED,
            }:
                upload.space_id = "space-generated"
                upload.file_id = f"file-generated-{index}"
            if status is ReimbursementUploadStatus.LINKED:
                upload.linked_at = utc_now()
        database.commit()
        return generated

    bundle = ReimbursementUpload(
        submission_id=submission.id,
        draft_id=submission.draft_id,
        source_draft_file_id=None,
        role=ReimbursementUploadRole.GENERATED_PDF.value,
        sort_order=0,
        local_storage_key=f"generated/{submission.id}/receipts.pdf",
        local_part_storage_key=None,
        local_status=ReimbursementUploadLocalStatus.READY.value,
        reserved_bytes=4096,
        reservation_expires_at=None,
        file_name="票据汇总.pdf",
        file_type="pdf",
        media_type="application/pdf",
        size_bytes=2048,
        sha256="a" * 64,
        upload_status=status.value,
    )
    generated = ReimbursementUpload(
        submission_id=submission.id,
        draft_id=submission.draft_id,
        source_draft_file_id=None,
        role=ReimbursementUploadRole.GENERATED_EXCEL.value,
        sort_order=1,
        local_storage_key=f"generated/{submission.id}/expense.xlsx",
        local_part_storage_key=None,
        local_status=ReimbursementUploadLocalStatus.READY.value,
        reserved_bytes=4096,
        reservation_expires_at=None,
        file_name="差旅费报销单.xlsx",
        file_type="xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        sha256="b" * 64,
        upload_status=status.value,
    )
    if status in {
        ReimbursementUploadStatus.COMMITTED,
        ReimbursementUploadStatus.LINKED,
    }:
        for index, upload in enumerate((bundle, generated)):
            upload.space_id = "space-generated"
            upload.file_id = f"file-generated-{index}"
    if status is ReimbursementUploadStatus.LINKED:
        bundle.linked_at = generated.linked_at = utc_now()
    database.add_all((bundle, generated))
    database.commit()
    return generated


def test_create_submission_locks_draft_without_copying_source_files(database: Session) -> None:
    draft, _source = _new_ready_draft(database)

    result = _create(database, draft)

    assert result.created is True
    assert result.idempotency_matched is True
    persisted_draft = database.get(ReimbursementDraft, draft.id)
    submission = database.get(ReimbursementSubmission, result.submission_id)
    assert persisted_draft.status == ReimbursementDraftStatus.LOCKED.value
    assert persisted_draft.revision == 5
    assert persisted_draft.locked_at is not None
    assert submission.snapshot_version == 6
    assert submission.form_snapshot_json == '{"snapshotVersion":6,"total":"100.00"}'
    assert submission.next_attempt_at is not None
    assert (
        database.scalar(
            select(ReimbursementUpload).where(
                ReimbursementUpload.submission_id == result.submission_id
            )
        )
        is None
    )


def test_existing_submission_is_reused_even_when_browser_has_a_new_key(
    database: Session,
) -> None:
    draft, _ = _new_ready_draft(database)
    first = _create(database, draft, key="first-browser-key")

    same = _create(database, draft, key="first-browser-key")
    replacement_key = _create(database, draft, key="new-browser-key")
    stale_page = create_submission(
        database,
        actor=_ACTOR,
        draft_id=draft.id,
        expected_revision=0,
        originator_union_id="",
        originator_name="",
        idempotency_key="refreshed-browser-key",
        snapshot_version=0,
        schema_fingerprint="invalid",
        form_snapshot_json="snapshot no longer available",
        snapshot_sha256="invalid",
    )

    assert same.submission_id == first.submission_id
    assert same.created is False
    assert same.idempotency_matched is True
    assert replacement_key.submission_id == first.submission_id
    assert replacement_key.created is False
    assert replacement_key.idempotency_matched is False
    assert stale_page.submission_id == first.submission_id
    assert stale_page.created is False
    assert stale_page.idempotency_matched is False
    assert database.query(ReimbursementSubmission).count() == 1
    assert (
        find_owned_submission_for_draft(
            database,
            actor=_ACTOR,
            draft_id=draft.id,
        ).id
        == first.submission_id
    )


def test_submission_creation_and_lookup_are_owner_and_corp_isolated(database: Session) -> None:
    draft, _ = _new_ready_draft(database)
    outsider = DraftActor(
        corp_id="other-corp",
        user_id=_ACTOR.user_id,
        department_id=_ACTOR.department_id,
        department_name=_ACTOR.department_name,
    )

    with pytest.raises(ApiError) as create_error:
        _create(database, draft, actor=outsider)
    assert create_error.value.code == "REIMBURSEMENT_DRAFT_NOT_FOUND"

    result = _create(database, draft)
    with pytest.raises(ApiError) as read_error:
        require_owned_submission(
            database,
            actor=outsider,
            submission_id=result.submission_id,
        )
    assert read_error.value.code == "REIMBURSEMENT_SUBMISSION_NOT_FOUND"
    assert (
        find_owned_submission_for_draft(
            database,
            actor=outsider,
            draft_id=draft.id,
        )
        is None
    )


def test_claim_lease_uses_status_version_and_counts_attempts(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result = _create(database, draft, now=now)

    claim = claim_due_submission(
        database,
        worker_id="worker-1",
        lease_seconds=30,
        now=now,
    )

    assert claim is not None
    assert claim.lease.submission_id == result.submission_id
    assert claim.status == ReimbursementSubmissionStatus.QUEUED.value
    assert claim.lease.expected_status_version == 2
    submission = database.get(ReimbursementSubmission, result.submission_id)
    assert submission.attempt_count == 1
    assert submission.lease_owner == "worker-1"
    assert (
        claim_due_submission(
            database,
            worker_id="worker-2",
            lease_seconds=30,
            now=now,
        )
        is None
    )

    renewed = renew_submission_lease(
        database,
        lease=claim.lease,
        worker_id="worker-1",
        lease_seconds=60,
        now=now + timedelta(seconds=1),
    )
    assert renewed.expected_status_version == 3
    with pytest.raises(ReimbursementSubmissionConflict):
        renew_submission_lease(
            database,
            lease=claim.lease,
            worker_id="worker-1",
            lease_seconds=60,
            now=now + timedelta(seconds=2),
        )


def test_retryable_failure_restores_exact_resume_status_when_due(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    _create(database, draft, now=now)
    claim = claim_due_submission(
        database,
        worker_id="worker-1",
        lease_seconds=60,
        now=now,
    )
    assert claim is not None
    validating = advance_submission(
        database,
        lease=claim.lease,
        worker_id="worker-1",
        to_status=ReimbursementSubmissionStatus.VALIDATING,
        now=now,
    )
    retry_at = now + timedelta(minutes=2)

    fail_submission_retryable(
        database,
        lease=validating,
        worker_id="worker-1",
        next_attempt_at=retry_at,
        error_code="TEMPORARY",
        error_message="稍后重试",
        now=now,
    )

    assert (
        claim_due_submission(
            database,
            worker_id="worker-2",
            lease_seconds=60,
            now=retry_at - timedelta(seconds=1),
        )
        is None
    )
    resumed = claim_due_submission(
        database,
        worker_id="worker-2",
        lease_seconds=60,
        now=retry_at,
    )
    assert resumed is not None
    assert resumed.status == ReimbursementSubmissionStatus.VALIDATING.value
    persisted = database.get(ReimbursementSubmission, resumed.lease.submission_id)
    assert persisted.resume_status is None
    assert persisted.attempt_count == 2


def test_upload_and_oa_checkpoints_are_persisted_before_remote_boundaries(
    database: Session,
) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    submission = database.get(ReimbursementSubmission, result.submission_id)
    generated = _add_generated_upload(database, submission)
    uploads = database.scalars(
        select(ReimbursementUpload)
        .where(ReimbursementUpload.submission_id == submission.id)
        .order_by(ReimbursementUpload.sort_order)
    ).all()

    versions: dict[str, int] = {}
    for upload in uploads:
        putting = begin_upload_put(
            database,
            lease=lease,
            worker_id="worker-1",
            upload_id=upload.id,
            expected_status_version=upload.status_version,
            now=now,
        )
        put_done = record_upload_put_done(
            database,
            lease=lease,
            worker_id="worker-1",
            upload_id=upload.id,
            expected_status_version=putting.status_version,
            now=now,
        )
        committing = begin_upload_commit(
            database,
            lease=lease,
            worker_id="worker-1",
            upload_id=upload.id,
            expected_status_version=put_done.status_version,
            now=now,
        )
        persisted = database.get(ReimbursementUpload, upload.id)
        assert persisted.upload_status == ReimbursementUploadStatus.COMMITTING.value
        assert persisted.commit_started_at == now
        committed = record_upload_committed(
            database,
            lease=lease,
            worker_id="worker-1",
            upload_id=upload.id,
            expected_status_version=committing.status_version,
            space_id=f"space-{upload.id}",
            file_id=f"file-{upload.id}",
            file_name=f"remote-{upload.file_name}",
            file_size=upload.size_bytes,
            file_type=upload.file_type,
            now=now,
        )
        versions[upload.id] = committed.status_version
        database.refresh(upload)
        assert upload.file_name.startswith("remote-")

    request = '{"processCode":"PROC-REIMBURSEMENT"}'
    request_hash = hashlib.sha256(request.encode()).hexdigest()
    oa_lease = checkpoint_oa_create(
        database,
        lease=lease,
        worker_id="worker-1",
        oa_request_json=request,
        oa_request_hash=request_hash,
        reconciliation_deadline_at=now + timedelta(minutes=30),
        now=now,
    )

    persisted_submission = database.get(ReimbursementSubmission, submission.id)
    assert persisted_submission.status == ReimbursementSubmissionStatus.OA_CREATING.value
    assert persisted_submission.oa_request_json == request
    assert persisted_submission.oa_request_hash == request_hash
    assert database.get(ReimbursementUpload, generated.id).status_version == versions[generated.id]
    assert oa_lease.expected_status_version == lease.expected_status_version + 1


def test_put_done_can_restart_with_a_fresh_process_local_ticket(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    upload = database.scalar(
        select(ReimbursementUpload).where(ReimbursementUpload.submission_id == result.submission_id)
    )
    putting = begin_upload_put(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=upload.status_version,
        now=now,
    )
    put_done = record_upload_put_done(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=putting.status_version,
        now=now,
    )
    retry_at = now + timedelta(seconds=10)
    fail_submission_retryable(
        database,
        lease=lease,
        worker_id="worker-1",
        next_attempt_at=retry_at,
        error_code="WORKER_RESTARTED",
        error_message="process-local ticket was lost",
        now=now,
    )
    resumed = claim_due_submission(
        database,
        worker_id="worker-2",
        lease_seconds=60,
        now=retry_at,
    )
    assert resumed is not None
    assert resumed.status == ReimbursementSubmissionStatus.UPLOADING.value

    restarted = begin_upload_put(
        database,
        lease=resumed.lease,
        worker_id="worker-2",
        upload_id=upload.id,
        expected_status_version=put_done.status_version,
        now=retry_at,
    )

    persisted = database.get(ReimbursementUpload, upload.id)
    assert restarted.upload_status == ReimbursementUploadStatus.PUTTING.value
    assert persisted.put_started_at == retry_at
    assert persisted.attempt_count == 2


def test_definitive_commit_rejection_resets_only_that_checkpoint(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    upload = database.scalar(
        select(ReimbursementUpload).where(ReimbursementUpload.submission_id == result.submission_id)
    )
    upload.upload_status = ReimbursementUploadStatus.PUT_DONE.value
    upload.put_started_at = now - timedelta(seconds=5)
    database.commit()
    committing = begin_upload_commit(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=upload.status_version,
        now=now,
    )

    with pytest.raises(ValueError, match="size differs"):
        record_upload_committed(
            database,
            lease=lease,
            worker_id="worker-1",
            upload_id=upload.id,
            expected_status_version=committing.status_version,
            space_id="space-remote",
            file_id="file-remote",
            file_name="invoice(1).pdf",
            file_size=upload.size_bytes + 1,
            file_type="pdf",
            now=now,
        )
    database.refresh(upload)
    assert upload.upload_status == ReimbursementUploadStatus.COMMITTING.value
    assert upload.file_name != "invoice(1).pdf"

    reset = reset_rejected_upload_commit(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=committing.status_version,
        error_code="DINGTALK_STORAGE_COMMIT_REJECTED",
        now=now,
    )

    persisted = database.get(ReimbursementUpload, upload.id)
    assert reset.upload_status == ReimbursementUploadStatus.PENDING.value
    assert persisted.put_started_at is None
    assert persisted.commit_started_at is None
    assert persisted.space_id is None
    assert persisted.file_id is None
    assert persisted.last_error_code == "DINGTALK_STORAGE_COMMIT_REJECTED"


def test_expired_oa_create_lease_moves_to_reconciliation_never_create_retry(
    database: Session,
) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    submission = database.get(ReimbursementSubmission, result.submission_id)
    generated = _add_generated_upload(
        database,
        submission,
        status=ReimbursementUploadStatus.COMMITTED,
    )
    request = '{"processCode":"PROC-REIMBURSEMENT"}'
    oa_lease = checkpoint_oa_create(
        database,
        lease=lease,
        worker_id="worker-1",
        oa_request_json=request,
        oa_request_hash=hashlib.sha256(request.encode()).hexdigest(),
        reconciliation_deadline_at=now + timedelta(minutes=30),
        now=now,
    )
    assert generated.file_id is not None

    recovered = recover_expired_submission_leases(
        database,
        now=now + timedelta(seconds=121),
    )

    assert recovered.oa_create_reconciliations == 1
    persisted = database.get(ReimbursementSubmission, submission.id)
    assert persisted.status == ReimbursementSubmissionStatus.RECONCILING.value
    assert persisted.lease_token is None
    assert persisted.status_version == oa_lease.expected_status_version + 1
    reclaimed = claim_due_submission(
        database,
        worker_id="worker-2",
        lease_seconds=60,
        now=now + timedelta(seconds=121),
    )
    assert reclaimed is not None
    assert reclaimed.status == ReimbursementSubmissionStatus.RECONCILING.value
    assert reclaimed.oa_request_json == request


def test_expired_commit_checkpoint_stops_in_manual_review(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    upload = database.scalar(
        select(ReimbursementUpload).where(ReimbursementUpload.submission_id == result.submission_id)
    )
    upload.upload_status = ReimbursementUploadStatus.PUT_DONE.value
    database.commit()
    committing = begin_upload_commit(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=upload.status_version,
        now=now,
    )

    recovered = recover_expired_submission_leases(
        database,
        now=now + timedelta(seconds=121),
    )

    assert recovered.uncertain_uploads == 1
    assert recovered.manual_reviews == 1
    assert (
        database.get(ReimbursementUpload, upload.id).upload_status
        == ReimbursementUploadStatus.COMMIT_UNCERTAIN.value
    )
    assert database.get(ReimbursementUpload, upload.id).status_version == (
        committing.status_version + 1
    )
    submission = database.get(ReimbursementSubmission, result.submission_id)
    assert submission.status == ReimbursementSubmissionStatus.MANUAL_REVIEW.value
    assert submission.lease_token is None


def test_explicit_commit_uncertainty_atomically_stops_parent(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    upload = database.scalar(
        select(ReimbursementUpload).where(ReimbursementUpload.submission_id == result.submission_id)
    )
    upload.upload_status = ReimbursementUploadStatus.PUT_DONE.value
    database.commit()
    committing = begin_upload_commit(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=upload.status_version,
        now=now,
    )

    mark_upload_commit_uncertain(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=upload.id,
        expected_status_version=committing.status_version,
        error_code="COMMIT_TIMEOUT",
        error_message="附件提交响应超时",
        now=now,
    )

    assert (
        database.get(ReimbursementUpload, upload.id).upload_status
        == ReimbursementUploadStatus.COMMIT_UNCERTAIN.value
    )
    assert (
        database.get(ReimbursementSubmission, result.submission_id).status
        == ReimbursementSubmissionStatus.MANUAL_REVIEW.value
    )


def test_oa_result_can_be_reconciled_and_submitted_only_after_linking(database: Session) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    submission = database.get(ReimbursementSubmission, result.submission_id)
    generated = _add_generated_upload(
        database,
        submission,
        status=ReimbursementUploadStatus.COMMITTED,
    )
    original = database.scalar(
        select(ReimbursementUpload).where(
            ReimbursementUpload.submission_id == submission.id,
            ReimbursementUpload.role == ReimbursementUploadRole.GENERATED_PDF.value,
        )
    )
    request = '{"processCode":"PROC-REIMBURSEMENT"}'
    lease = checkpoint_oa_create(
        database,
        lease=lease,
        worker_id="worker-1",
        oa_request_json=request,
        oa_request_hash=hashlib.sha256(request.encode()).hexdigest(),
        reconciliation_deadline_at=now + timedelta(minutes=30),
        now=now,
    )
    lease = mark_oa_create_uncertain(
        database,
        lease=lease,
        worker_id="worker-1",
        error_code="CREATE_TIMEOUT",
        error_message="创建响应超时",
        now=now,
    )
    lease = record_oa_instance(
        database,
        lease=lease,
        worker_id="worker-1",
        process_instance_id="oa-instance-1",
        now=now,
    )

    with pytest.raises(ReimbursementSubmissionConflict):
        mark_submission_submitted(
            database,
            lease=lease,
            worker_id="worker-1",
            business_id="business-1",
            approval_url="dingtalk://oa-instance-1",
            now=now,
        )
    for upload in (original, generated):
        persisted = database.get(ReimbursementUpload, upload.id)
        mark_upload_linked(
            database,
            lease=lease,
            worker_id="worker-1",
            upload_id=upload.id,
            expected_status_version=persisted.status_version,
            now=now,
        )
    mark_submission_submitted(
        database,
        lease=lease,
        worker_id="worker-1",
        business_id="business-1",
        approval_url="dingtalk://oa-instance-1",
        now=now,
    )

    persisted = database.get(ReimbursementSubmission, submission.id)
    assert persisted.status == ReimbursementSubmissionStatus.SUBMITTED.value
    assert persisted.lease_token is None
    assert persisted.approval_url == "dingtalk://oa-instance-1"


def test_discarded_generated_excel_can_be_removed_under_generation_lease(
    database: Session,
) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result = _create(database, draft, now=now)
    claim = claim_due_submission(
        database,
        worker_id="worker-1",
        lease_seconds=60,
        now=now,
    )
    assert claim is not None
    lease = advance_submission(
        database,
        lease=claim.lease,
        worker_id="worker-1",
        to_status=ReimbursementSubmissionStatus.VALIDATING,
        now=now,
    )
    lease = advance_submission(
        database,
        lease=lease,
        worker_id="worker-1",
        to_status=ReimbursementSubmissionStatus.GENERATING_EXCEL,
        now=now,
    )
    submission = database.get(ReimbursementSubmission, result.submission_id)
    discarded = _add_generated_upload(database, submission)
    discarded.upload_status = ReimbursementUploadStatus.DISCARDED.value
    discarded.local_status = ReimbursementUploadLocalStatus.DELETED.value
    discarded.local_deleted_at = now
    database.commit()

    assert remove_discarded_generated_upload(
        database,
        lease=lease,
        worker_id="worker-1",
        upload_id=discarded.id,
        expected_status_version=discarded.status_version,
        now=now,
    )
    assert database.get(ReimbursementUpload, discarded.id) is None


def test_explicit_oa_rejection_can_enter_and_complete_orphan_cleanup(
    database: Session,
) -> None:
    now = utc_now()
    draft, _ = _new_ready_draft(database)
    result, lease = _claim_to_uploading(database, draft, now=now)
    submission = database.get(ReimbursementSubmission, result.submission_id)
    generated = _add_generated_upload(
        database,
        submission,
        status=ReimbursementUploadStatus.COMMITTED,
    )
    original = database.scalar(
        select(ReimbursementUpload).where(
            ReimbursementUpload.submission_id == submission.id,
            ReimbursementUpload.role == ReimbursementUploadRole.GENERATED_PDF.value,
        )
    )
    request = '{"processCode":"PROC-REIMBURSEMENT"}'
    lease = checkpoint_oa_create(
        database,
        lease=lease,
        worker_id="worker-1",
        oa_request_json=request,
        oa_request_hash=hashlib.sha256(request.encode()).hexdigest(),
        reconciliation_deadline_at=now + timedelta(minutes=30),
        now=now,
    )
    cleanup_lease = begin_orphan_cleanup(
        database,
        lease=lease,
        worker_id="worker-1",
        confirmation_code="EXPLICIT_4XX_REJECTION",
        now=now,
    )
    for upload in (original, generated):
        persisted = database.get(ReimbursementUpload, upload.id)
        persisted.upload_status = ReimbursementUploadStatus.CLEANED.value
        persisted.cleanup_started_at = now
        persisted.cleaned_at = now
    database.commit()

    complete_orphan_cleanup(
        database,
        lease=cleanup_lease,
        worker_id="worker-1",
        error_code="DINGTALK_REJECTED",
        error_message="钉钉明确拒绝创建审批",
        now=now,
    )

    assert database.get(ReimbursementSubmission, submission.id).status == (
        ReimbursementSubmissionStatus.FAILED_FINAL.value
    )
    cleanup_candidates = list_due_linked_local_release_candidates(database)
    assert {item.upload_id for item in cleanup_candidates} == {original.id, generated.id}
    for candidate in cleanup_candidates:
        finalize_local_upload_release(
            database,
            corp_id=candidate.corp_id,
            user_id=candidate.user_id,
            submission_id=candidate.submission_id,
            upload_id=candidate.upload_id,
            expected_status_version=candidate.upload_status_version,
            now=now,
        )
