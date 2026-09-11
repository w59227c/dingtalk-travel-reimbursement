from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.base import Base
from app.database.session import create_database_engine, create_session_factory
from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftRelatedApproval,
    ReimbursementDraftStatus,
    ReimbursementSubmission,
    ReimbursementSubmissionStatus,
    ReimbursementUpload,
    ReimbursementUploadLocalStatus,
    ReimbursementUploadRole,
    ReimbursementUploadStatus,
    utc_now,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64

_IMMUTABLE_TRIGGER_NAMES = {
    "trg_reimbursement_submissions_snapshot_immutable",
    "trg_reimbursement_submissions_oa_request_immutable",
    "trg_reimbursement_submissions_process_instance_immutable",
    "trg_reimbursement_submissions_submitted_terminal",
    "trg_reimbursement_uploads_linked_at_immutable",
    "trg_reimbursement_uploads_cleaned_at_immutable",
    "trg_reimbursement_uploads_local_deleted_at_immutable",
    "trg_reimbursement_uploads_remote_identity_immutable",
}

_SUBMITTED_MANIFEST_TRIGGER_NAMES = {
    "trg_reimbursement_submissions_submitted_manifest_insert",
    "trg_reimbursement_submissions_submitted_manifest_update",
    "trg_reimbursement_uploads_submitted_parent_insert",
    "trg_reimbursement_uploads_submitted_parent_delete",
    "trg_reimbursement_uploads_submitted_manifest_update",
}


@pytest.fixture
def database(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'reimbursements.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        yield session
    engine.dispose()


def new_draft(*, owner: str = "employee-1", corp: str = "corp-test") -> ReimbursementDraft:
    return ReimbursementDraft(
        corp_id=corp,
        owner_user_id=owner,
        status=ReimbursementDraftStatus.REVIEW_READY.value,
        revision=3,
        department_id="department-1",
        department_name="测试部门",
        template_process_code="PROC-REIMBURSEMENT",
        template_config_version=2,
        schema_fingerprint=_HASH_A,
        input_json='{"company":"北京"}',
        related_instance_ids_json='["travel-1"]',
        expires_at=utc_now() + timedelta(days=30),
    )


def new_submission(
    draft: ReimbursementDraft,
    *,
    idempotency_hash: str = _HASH_C,
) -> ReimbursementSubmission:
    return ReimbursementSubmission(
        draft_id=draft.id,
        corp_id=draft.corp_id,
        originator_user_id=draft.owner_user_id,
        originator_union_id="union-1",
        originator_name="测试员工",
        department_id=draft.department_id,
        department_name=draft.department_name,
        template_process_code=draft.template_process_code,
        template_config_version=draft.template_config_version,
        schema_fingerprint=draft.schema_fingerprint,
        form_snapshot_json=draft.input_json,
        related_instance_ids_json=draft.related_instance_ids_json,
        snapshot_sha256=_HASH_B,
        idempotency_key_hash=idempotency_hash,
        status=ReimbursementSubmissionStatus.QUEUED.value,
    )


def new_active_file(
    draft: ReimbursementDraft,
    *,
    sort_order: int = 0,
    role: ReimbursementDraftFileRole = ReimbursementDraftFileRole.EXPENSE_SOURCE,
) -> ReimbursementDraftFile:
    return ReimbursementDraftFile(
        draft_id=draft.id,
        sort_order=sort_order,
        processing_role=role.value,
        file_status=ReimbursementDraftFileStatus.ACTIVE.value,
        storage_key=f"drafts/{draft.id}/{sort_order:08d}-file.pdf",
        part_storage_key=None,
        reserved_bytes=123,
        reservation_expires_at=None,
        original_name=f"附件-{sort_order}.pdf",
        extension="pdf",
        media_type="application/pdf",
        size_bytes=123,
        sha256=_HASH_A,
    )


def new_related_approval(
    draft: ReimbursementDraft,
    *,
    sort_order: int = 0,
    process_instance_id: str = "travel-instance-1",
) -> ReimbursementDraftRelatedApproval:
    now = utc_now()
    return ReimbursementDraftRelatedApproval(
        draft_id=draft.id,
        corp_id=draft.corp_id,
        owner_user_id=draft.owner_user_id,
        sort_order=sort_order,
        process_instance_id=process_instance_id,
        travel_profile_key="domestic-travel",
        process_code="PROC-DOMESTIC-TRAVEL",
        catalog_config_version=2,
        travel_schema_fingerprint=_HASH_B,
        listed_from_ms=1_788_192_000_000,
        listed_to_ms=1_799_078_400_000,
        travel_start_date=date(2026, 8, 20),
        travel_end_date=date(2026, 8, 22),
        title="测试员工的境内出差申请",
        business_id="BUSINESS-TRAVEL-1",
        instance_created_at=now - timedelta(days=15),
        verified_at=now,
    )


def new_ready_upload(
    submission: ReimbursementSubmission,
    *,
    sort_order: int,
    role: ReimbursementUploadRole,
    source: ReimbursementDraftFile | None = None,
    storage_suffix: str | None = None,
) -> ReimbursementUpload:
    suffix = storage_suffix or str(sort_order)
    is_pdf = role is ReimbursementUploadRole.GENERATED_PDF
    return ReimbursementUpload(
        submission_id=submission.id,
        draft_id=submission.draft_id,
        source_draft_file_id=source.id if source is not None else None,
        role=role.value,
        sort_order=sort_order,
        local_storage_key=f"generated/{submission.id}/{suffix}.{'pdf' if is_pdf else 'xlsx'}",
        local_part_storage_key=None,
        local_status=ReimbursementUploadLocalStatus.READY.value,
        reserved_bytes=123,
        reservation_expires_at=None,
        file_name="票据汇总.pdf" if is_pdf else "差旅费报销单.xlsx",
        file_type="pdf" if is_pdf else "xlsx",
        media_type=(
            "application/pdf"
            if is_pdf
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size_bytes=123,
        sha256=_HASH_A,
        upload_status=ReimbursementUploadStatus.PENDING.value,
    )


def add_linked_generated_manifest(
    database: Session,
    submission: ReimbursementSubmission,
    *,
    now: datetime,
) -> tuple[ReimbursementUpload, ReimbursementUpload]:
    pdf = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_PDF,
    )
    excel = new_ready_upload(
        submission,
        sort_order=1,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    for label, upload in (("pdf", pdf), ("excel", excel)):
        upload.upload_status = ReimbursementUploadStatus.LINKED.value
        upload.space_id = f"space-{label}"
        upload.file_id = f"file-{label}"
        upload.linked_at = now
    database.add_all([pdf, excel])
    return pdf, excel


def test_create_all_installs_current_immutable_guards(
    database: Session,
) -> None:
    trigger_names = set(
        database.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars()
    )

    assert _IMMUTABLE_TRIGGER_NAMES <= trigger_names
    assert _SUBMITTED_MANIFEST_TRIGGER_NAMES <= trigger_names


def test_submission_terminal_fields_match_current_model_contract() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in ReimbursementSubmission.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }

    assert checks["ck_reimbursement_submissions_submitted_at"] == (
        "status != 'SUBMITTED' OR "
        "(submitted_at IS NOT NULL AND business_id IS NOT NULL AND approval_url IS NOT NULL)"
    )
    assert checks["ck_reimbursement_submissions_instance_required"] == (
        "status NOT IN ('VERIFYING', 'SUBMITTED') OR process_instance_id IS NOT NULL"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_version", 2),
        ("form_snapshot_json", '{"changed":true}'),
        ("related_instance_ids_json", '["another-travel"]'),
        ("snapshot_sha256", _HASH_C),
        ("idempotency_key_hash", _HASH_A),
    ],
)
def test_submission_snapshot_is_immutable_after_insert(
    database: Session,
    field: str,
    value: object,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.commit()

    setattr(submission, field, value)
    with pytest.raises(IntegrityError):
        database.commit()


def test_oa_create_command_is_immutable_after_checkpoint(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.OA_CREATING.value
    submission.oa_create_started_at = utc_now()
    submission.oa_request_json = '{"request":1}'
    submission.oa_request_hash = _HASH_A
    database.add(submission)
    database.commit()

    submission.oa_request_json = '{"request":2}'
    with pytest.raises(IntegrityError):
        database.commit()


def test_persists_verified_related_approval_snapshot(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    approval = new_related_approval(draft)
    database.add(approval)
    database.commit()
    approval_id = approval.id
    draft_id = draft.id
    database.expunge_all()

    persisted = database.get(ReimbursementDraftRelatedApproval, approval_id)

    assert persisted is not None
    assert persisted.draft_id == draft_id
    assert persisted.travel_start_date == date(2026, 8, 20)
    assert persisted.travel_end_date == date(2026, 8, 22)
    assert persisted.travel_schema_fingerprint == _HASH_B
    assert persisted.created_at.tzinfo is None
    assert persisted.updated_at.tzinfo is None


def test_related_approval_must_match_draft_owner(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    approval = new_related_approval(draft)
    approval.owner_user_id = "another-employee"
    database.add(approval)

    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"sort_order": -1},
        {"catalog_config_version": 0},
        {"travel_schema_fingerprint": "short"},
        {"listed_from_ms": -1},
        {"listed_from_ms": 2_000, "listed_to_ms": 1_000},
        {"travel_start_date": date(2026, 8, 23)},
    ],
)
def test_related_approval_rejects_invalid_catalog_snapshot(
    database: Session,
    changes: dict[str, object],
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    approval = new_related_approval(draft)
    for field, value in changes.items():
        setattr(approval, field, value)
    database.add(approval)

    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"process_instance_id": "travel-instance-1", "sort_order": 1},
        {"process_instance_id": "travel-instance-2", "sort_order": 0},
    ],
)
def test_related_approval_is_unique_by_instance_and_sort_order_within_draft(
    database: Session,
    changes: dict[str, object],
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    database.add(new_related_approval(draft))
    database.flush()
    duplicate = new_related_approval(draft)
    for field, value in changes.items():
        setattr(duplicate, field, value)
    database.add(duplicate)

    with pytest.raises(IntegrityError):
        database.commit()


def test_persists_attachment_only_and_generated_excel_manifest(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    attachment = new_active_file(
        draft,
        role=ReimbursementDraftFileRole.ATTACHMENT_ONLY,
    )
    database.add(attachment)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    original = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.ORIGINAL,
        source=attachment,
        storage_suffix="original",
    )
    original.local_storage_key = attachment.storage_key
    original.file_name = attachment.original_name
    original.file_type = attachment.extension
    original.media_type = attachment.media_type
    excel = new_ready_upload(
        submission,
        sort_order=1,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    database.add_all([original, excel])
    database.commit()

    database.expire_all()
    assert database.get(ReimbursementDraftFile, attachment.id).processing_role == "ATTACHMENT_ONLY"
    assert database.get(ReimbursementUpload, original.id).source_draft_file_id == attachment.id
    assert database.get(ReimbursementUpload, excel.id).source_draft_file_id is None


def test_one_submission_per_draft_but_same_key_on_another_draft_is_allowed(
    database: Session,
) -> None:
    first_draft = new_draft()
    second_draft = new_draft()
    database.add_all([first_draft, second_draft])
    database.flush()
    database.add_all([new_submission(first_draft), new_submission(second_draft)])
    database.commit()

    database.add(new_submission(first_draft, idempotency_hash=_HASH_A))
    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    ("corp_id", "originator_user_id"),
    [
        ("other-corp", "employee-1"),
        ("corp-test", "other-employee"),
    ],
)
def test_submission_must_match_draft_owner(
    database: Session,
    corp_id: str,
    originator_user_id: str,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.corp_id = corp_id
    submission.originator_user_id = originator_user_id
    database.add(submission)

    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize("mismatch", ["submission", "source"])
def test_upload_submission_and_source_file_must_share_the_same_draft(
    database: Session,
    mismatch: str,
) -> None:
    first_draft = new_draft(owner="employee-1")
    second_draft = new_draft(owner="employee-2")
    database.add_all([first_draft, second_draft])
    database.flush()
    first_source = new_active_file(first_draft)
    second_source = new_active_file(second_draft)
    first_submission = new_submission(first_draft)
    database.add_all([first_source, second_source, first_submission])
    database.flush()
    upload = new_ready_upload(
        first_submission,
        sort_order=0,
        role=ReimbursementUploadRole.ORIGINAL,
        source=first_source,
    )
    if mismatch == "submission":
        upload.draft_id = second_draft.id
        upload.source_draft_file_id = second_source.id
    else:
        upload.source_draft_file_id = second_source.id
    database.add(upload)

    with pytest.raises(IntegrityError):
        database.commit()


def test_each_submission_has_at_most_one_generated_excel(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    database.add(
        new_ready_upload(
            submission,
            sort_order=0,
            role=ReimbursementUploadRole.GENERATED_EXCEL,
            storage_suffix="first",
        )
    )
    database.commit()

    database.add(
        new_ready_upload(
            submission,
            sort_order=1,
            role=ReimbursementUploadRole.GENERATED_EXCEL,
            storage_suffix="second",
        )
    )
    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    ("role", "has_source", "status", "space_id", "file_id", "linked_at", "cleaned_at"),
    [
        ("ORIGINAL", False, "PENDING", None, None, None, None),
        ("GENERATED_EXCEL", True, "PENDING", None, None, None, None),
        ("GENERATED_EXCEL", False, "COMMITTED", None, None, None, None),
        ("GENERATED_EXCEL", False, "LINKED", "space", "file", None, None),
        ("GENERATED_EXCEL", False, "CLEANED", "space", "file", None, None),
        ("GENERATED_EXCEL", False, "PENDING", "space", None, None, None),
    ],
)
def test_upload_lifecycle_invalid_rows_are_rejected(
    database: Session,
    role: str,
    has_source: bool,
    status: str,
    space_id: str | None,
    file_id: str | None,
    linked_at,
    cleaned_at,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    source = new_active_file(draft)
    submission = new_submission(draft)
    database.add_all([source, submission])
    database.flush()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    upload.role = role
    upload.source_draft_file_id = source.id if has_source else None
    upload.upload_status = status
    upload.space_id = space_id
    upload.file_id = file_id
    upload.linked_at = linked_at
    upload.cleaned_at = cleaned_at
    database.add(upload)

    with pytest.raises(IntegrityError):
        database.commit()


def test_active_reservations_require_persistable_part_and_expiry(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    invalid = ReimbursementDraftFile(
        draft_id=draft.id,
        sort_order=0,
        processing_role=ReimbursementDraftFileRole.EXPENSE_SOURCE.value,
        file_status=ReimbursementDraftFileStatus.RESERVED.value,
        storage_key=f"drafts/{draft.id}/reserved.pdf",
        part_storage_key=None,
        reserved_bytes=100,
        reservation_expires_at=None,
        original_name="发票.pdf",
        extension="pdf",
        media_type="application/pdf",
        size_bytes=None,
        sha256=None,
    )
    database.add(invalid)

    with pytest.raises(IntegrityError):
        database.commit()


def test_draft_file_size_cannot_exceed_reserved_bytes(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    source = new_active_file(draft)
    source.reserved_bytes = source.size_bytes - 1
    database.add(source)

    with pytest.raises(IntegrityError):
        database.commit()


def test_upload_size_cannot_exceed_reserved_bytes(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    upload.reserved_bytes = upload.size_bytes - 1
    database.add(upload)

    with pytest.raises(IntegrityError):
        database.commit()


def test_submission_verification_and_terminal_states_require_result_fields(
    database: Session,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.SUBMITTED.value
    database.add(submission)

    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    "resume_status",
    [
        ReimbursementSubmissionStatus.VALIDATING.value,
        ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
        ReimbursementSubmissionStatus.UPLOADING.value,
        ReimbursementSubmissionStatus.VERIFYING.value,
        ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
    ],
)
def test_failed_retryable_submission_accepts_each_resumable_phase(
    database: Session,
    resume_status: str,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.FAILED_RETRYABLE.value
    submission.resume_status = resume_status
    database.add(submission)

    database.commit()


@pytest.mark.parametrize(
    ("status", "resume_status"),
    [
        (
            ReimbursementSubmissionStatus.FAILED_RETRYABLE.value,
            None,
        ),
        (
            ReimbursementSubmissionStatus.QUEUED.value,
            ReimbursementSubmissionStatus.VALIDATING.value,
        ),
    ],
)
def test_submission_resume_status_must_match_failed_retryable_state(
    database: Session,
    status: str,
    resume_status: str | None,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = status
    submission.resume_status = resume_status
    database.add(submission)

    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    ("status", "resume_status"),
    [
        (ReimbursementSubmissionStatus.OA_CREATING.value, None),
        (ReimbursementSubmissionStatus.RECONCILING.value, None),
        (ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value, None),
        (
            ReimbursementSubmissionStatus.FAILED_RETRYABLE.value,
            ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value,
        ),
    ],
)
def test_process_instance_is_rejected_in_cleanup_or_pre_result_states(
    database: Session,
    status: str,
    resume_status: str | None,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = status
    submission.resume_status = resume_status
    submission.process_instance_id = "instance-unsafe"
    submission.oa_create_started_at = utc_now()
    submission.oa_request_hash = _HASH_A
    if status == ReimbursementSubmissionStatus.RECONCILING.value:
        submission.reconciliation_deadline_at = utc_now() + timedelta(minutes=5)
    if status == ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value:
        submission.orphan_confirmed_at = utc_now()
        submission.orphan_confirmation_code = "CONFIRMED"
    database.add(submission)

    with pytest.raises(IntegrityError):
        database.commit()


def test_submitted_reconciling_and_orphan_checkpoints_can_be_persisted(
    database: Session,
) -> None:
    now = utc_now()
    submitted_draft = new_draft(owner="submitted-owner")
    reconciling_draft = new_draft(owner="reconciling-owner")
    orphan_draft = new_draft(owner="orphan-owner")
    database.add_all([submitted_draft, reconciling_draft, orphan_draft])
    database.flush()

    submitted = new_submission(submitted_draft, idempotency_hash="1" * 64)
    submitted.status = ReimbursementSubmissionStatus.VERIFYING.value
    submitted.oa_create_started_at = now
    submitted.oa_request_hash = "2" * 64
    submitted.process_instance_id = "instance-1"

    reconciling = new_submission(reconciling_draft, idempotency_hash="3" * 64)
    reconciling.status = ReimbursementSubmissionStatus.RECONCILING.value
    reconciling.oa_create_started_at = now
    reconciling.oa_request_hash = "4" * 64
    reconciling.reconciliation_deadline_at = now + timedelta(minutes=30)

    orphan = new_submission(orphan_draft, idempotency_hash="5" * 64)
    orphan.status = ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value
    orphan.orphan_confirmed_at = now
    orphan.orphan_confirmation_code = "EXPLICIT_CREATE_REJECTION"

    database.add_all([submitted, reconciling, orphan])
    database.flush()
    add_linked_generated_manifest(database, submitted, now=now)
    database.flush()
    submitted.status = ReimbursementSubmissionStatus.SUBMITTED.value
    submitted.business_id = "business-1"
    submitted.approval_url = "dingtalk://approval/instance-1"
    submitted.submitted_at = now
    database.commit()

    assert database.get(ReimbursementSubmission, submitted.id).process_instance_id == "instance-1"
    assert database.get(ReimbursementSubmission, reconciling.id).status == "RECONCILING"
    assert database.get(ReimbursementSubmission, orphan.id).status == "ORPHAN_CLEANUP"


@pytest.mark.parametrize("transition", ["insert", "update"])
def test_submitted_submission_requires_the_current_generated_manifest(
    database: Session,
    transition: str,
) -> None:
    now = utc_now()
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.VERIFYING.value
    submission.oa_create_started_at = now
    submission.oa_request_hash = _HASH_A
    submission.process_instance_id = "instance-no-excel"
    database.add(submission)
    if transition == "update":
        database.commit()
    submission.status = ReimbursementSubmissionStatus.SUBMITTED.value
    submission.business_id = "business-no-excel"
    submission.approval_url = "dingtalk://approval/instance-no-excel"
    submission.submitted_at = now

    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize(
    "upload_status",
    [
        ReimbursementUploadStatus.PENDING,
        ReimbursementUploadStatus.COMMITTED,
    ],
)
def test_submitted_submission_rejects_any_upload_that_is_not_linked(
    database: Session,
    upload_status: ReimbursementUploadStatus,
) -> None:
    now = utc_now()
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.VERIFYING.value
    submission.oa_create_started_at = now
    submission.oa_request_hash = _HASH_A
    submission.process_instance_id = f"instance-{upload_status.value.lower()}"
    database.add(submission)
    database.flush()
    _, generated = add_linked_generated_manifest(database, submission, now=now)
    generated.upload_status = upload_status.value
    generated.linked_at = None
    generated.space_id = None
    generated.file_id = None
    if upload_status is ReimbursementUploadStatus.COMMITTED:
        generated.space_id = "space-committed"
        generated.file_id = "file-committed"
    database.commit()

    submission.status = ReimbursementSubmissionStatus.SUBMITTED.value
    submission.business_id = f"business-{upload_status.value.lower()}"
    submission.approval_url = "dingtalk://approval/not-linked"
    submission.submitted_at = now
    with pytest.raises(IntegrityError):
        database.commit()


def test_all_uploads_must_be_linked_before_submit_and_no_upload_can_be_added_afterward(
    database: Session,
) -> None:
    now = utc_now()
    draft = new_draft()
    database.add(draft)
    database.flush()
    source = new_active_file(draft)
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.VERIFYING.value
    submission.oa_create_started_at = now
    submission.oa_request_hash = _HASH_A
    submission.process_instance_id = "instance-complete"
    database.add_all([source, submission])
    database.flush()
    _, generated = add_linked_generated_manifest(database, submission, now=now)
    database.flush()

    submission.status = ReimbursementSubmissionStatus.SUBMITTED.value
    submission.business_id = "business-complete"
    submission.approval_url = "dingtalk://approval/instance-complete"
    submission.submitted_at = now
    database.commit()

    late_upload = new_ready_upload(
        submission,
        sort_order=2,
        role=ReimbursementUploadRole.ORIGINAL,
        source=source,
    )
    late_upload.local_storage_key = source.storage_key
    late_upload.file_name = source.original_name
    late_upload.file_type = source.extension
    late_upload.media_type = source.media_type
    database.add(late_upload)
    with pytest.raises(IntegrityError):
        database.commit()
    database.rollback()

    linked_excel = database.get(ReimbursementUpload, generated.id)
    assert linked_excel is not None
    linked_excel.role = ReimbursementUploadRole.ORIGINAL.value
    linked_excel.source_draft_file_id = source.id
    with pytest.raises(IntegrityError):
        database.commit()
    database.rollback()

    linked_excel = database.get(ReimbursementUpload, generated.id)
    assert linked_excel is not None
    local_deleted_at = utc_now()
    linked_excel.local_status = ReimbursementUploadLocalStatus.DELETED.value
    linked_excel.local_deleted_at = local_deleted_at
    database.commit()
    assert linked_excel.local_deleted_at == local_deleted_at

    database.delete(linked_excel)
    with pytest.raises(IntegrityError):
        database.commit()
    database.rollback()

    persisted = database.get(ReimbursementUpload, generated.id)
    assert persisted is not None
    assert persisted.upload_status == ReimbursementUploadStatus.LINKED.value
    assert persisted.role == ReimbursementUploadRole.GENERATED_EXCEL.value
    assert persisted.source_draft_file_id is None
    assert persisted.local_status == ReimbursementUploadLocalStatus.DELETED.value


def test_foreign_keys_prevent_deleting_a_submitted_draft(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    database.add(new_submission(draft))
    database.commit()

    database.delete(draft)
    with pytest.raises(IntegrityError):
        database.commit()


def test_local_file_deletion_requires_a_safe_terminal_remote_state(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    upload.local_status = ReimbursementUploadLocalStatus.DELETED.value
    upload.local_deleted_at = utc_now()
    database.add(upload)

    with pytest.raises(IntegrityError):
        database.commit()


def test_linked_upload_can_record_safe_local_deletion(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.VERIFYING.value
    submission.oa_create_started_at = utc_now()
    submission.oa_request_hash = _HASH_A
    submission.process_instance_id = "instance-1"
    database.add(submission)
    database.flush()
    now = utc_now()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    upload.upload_status = ReimbursementUploadStatus.LINKED.value
    upload.space_id = "space-1"
    upload.file_id = "file-1"
    upload.linked_at = now
    upload.local_status = ReimbursementUploadLocalStatus.DELETED.value
    upload.local_deleted_at = now
    database.add(upload)
    database.commit()

    assert database.get(ReimbursementUpload, upload.id).local_deleted_at == now


@pytest.mark.parametrize("transition", ["insert", "update"])
def test_linked_upload_requires_a_confirmed_parent_instance(
    database: Session,
    transition: str,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    now = utc_now()
    if transition == "insert":
        upload.upload_status = ReimbursementUploadStatus.LINKED.value
        upload.space_id = "space-unsafe"
        upload.file_id = "file-unsafe"
        upload.linked_at = now
        database.add(upload)
    else:
        database.add(upload)
        database.commit()
        upload.upload_status = ReimbursementUploadStatus.LINKED.value
        upload.space_id = "space-unsafe"
        upload.file_id = "file-unsafe"
        upload.linked_at = now

    with pytest.raises(IntegrityError, match="linked upload requires a confirmed OA instance"):
        database.commit()


def test_cleaned_upload_blocks_parent_from_recording_an_oa_instance(
    database: Session,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    submission.status = ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value
    submission.orphan_confirmed_at = utc_now()
    submission.orphan_confirmation_code = "CREATE_REJECTED"
    database.add(submission)
    database.flush()
    cleaned = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    cleaned.upload_status = ReimbursementUploadStatus.CLEANED.value
    cleaned.space_id = "space-cleaned"
    cleaned.file_id = "file-cleaned"
    cleaned.cleanup_started_at = utc_now()
    cleaned.cleaned_at = utc_now()
    database.add(cleaned)
    database.commit()

    submission.status = ReimbursementSubmissionStatus.VERIFYING.value
    submission.oa_create_started_at = utc_now()
    submission.oa_request_hash = _HASH_A
    submission.process_instance_id = "instance-too-late"
    with pytest.raises(IntegrityError, match="OA instance and remote cleanup cannot coexist"):
        database.commit()


def test_irreversible_upload_cannot_be_reparented_to_bypass_cleanup_guard(
    database: Session,
) -> None:
    first_draft = new_draft(owner="employee-1")
    second_draft = new_draft(owner="employee-2")
    database.add_all([first_draft, second_draft])
    database.flush()
    first = new_submission(first_draft, idempotency_hash="1" * 64)
    first.status = ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value
    first.orphan_confirmed_at = utc_now()
    first.orphan_confirmation_code = "CREATE_REJECTED"
    second = new_submission(second_draft, idempotency_hash="2" * 64)
    second.status = ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value
    second.orphan_confirmed_at = utc_now()
    second.orphan_confirmation_code = "CREATE_REJECTED"
    database.add_all([first, second])
    database.flush()
    cleaned = new_ready_upload(
        first,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    cleaned.upload_status = ReimbursementUploadStatus.CLEANED.value
    cleaned.space_id = "space-cleaned"
    cleaned.file_id = "file-cleaned"
    cleaned.cleanup_started_at = utc_now()
    cleaned.cleaned_at = utc_now()
    database.add(cleaned)
    database.commit()

    cleaned.submission_id = second.id
    cleaned.draft_id = second.draft_id
    with pytest.raises(IntegrityError, match="upload parent is immutable"):
        database.commit()


def test_committed_upload_can_be_persisted_and_reused(database: Session) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    upload.upload_status = ReimbursementUploadStatus.COMMITTED.value
    upload.space_id = "space-1"
    upload.file_id = "file-1"
    database.add(upload)
    database.commit()

    assert database.get(ReimbursementUpload, upload.id).upload_status == "COMMITTED"


def test_unwritten_discarded_upload_is_valid_but_cannot_be_reactivated(
    database: Session,
) -> None:
    draft = new_draft()
    database.add(draft)
    database.flush()
    submission = new_submission(draft)
    database.add(submission)
    database.flush()
    upload = new_ready_upload(
        submission,
        sort_order=0,
        role=ReimbursementUploadRole.GENERATED_EXCEL,
    )
    discarded_at = utc_now()
    upload.local_status = ReimbursementUploadLocalStatus.DELETED.value
    upload.local_deleted_at = discarded_at
    upload.upload_status = ReimbursementUploadStatus.DISCARDED.value
    upload.size_bytes = None
    upload.sha256 = None
    database.add(upload)
    database.commit()

    submission.status = ReimbursementSubmissionStatus.ORPHAN_CLEANUP.value
    submission.orphan_confirmed_at = utc_now()
    submission.orphan_confirmation_code = "CREATE_REJECTED"
    database.commit()

    upload.upload_status = ReimbursementUploadStatus.CLEANED.value
    upload.size_bytes = 123
    upload.sha256 = _HASH_A
    upload.space_id = "space-late"
    upload.file_id = "file-late"
    upload.cleanup_started_at = utc_now()
    upload.cleaned_at = utc_now()
    with pytest.raises(IntegrityError):
        database.commit()
    database.rollback()

    persisted = database.get(ReimbursementUpload, upload.id)
    assert persisted is not None
    assert persisted.upload_status == ReimbursementUploadStatus.DISCARDED.value
    assert persisted.local_status == ReimbursementUploadLocalStatus.DELETED.value
    assert persisted.size_bytes is None
    assert persisted.sha256 is None
    assert persisted.space_id is None
    assert persisted.file_id is None
