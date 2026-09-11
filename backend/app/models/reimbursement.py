from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    """Return UTC without tzinfo, matching the project's SQLite convention."""

    return datetime.now(UTC).replace(tzinfo=None)


def new_uuid() -> str:
    return str(uuid4())


def _sql_values(enum_type: type[StrEnum]) -> str:
    return ", ".join(f"'{item.value}'" for item in enum_type)


class ReimbursementDraftStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEW_READY = "REVIEW_READY"
    LOCKED = "LOCKED"
    EXPIRED = "EXPIRED"


class ReimbursementDraftFileRole(StrEnum):
    EXPENSE_SOURCE = "EXPENSE_SOURCE"
    ATTACHMENT_ONLY = "ATTACHMENT_ONLY"


class ReimbursementAttachmentKind(StrEnum):
    ITINERARY = "itinerary"
    PAYMENT_PROOF = "payment_proof"
    HOTEL_BILL = "hotel_bill"
    OTHER = "other"


class ReimbursementDraftFileStatus(StrEnum):
    RESERVED = "RESERVED"
    WRITING = "WRITING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    DELETING = "DELETING"
    PURGED = "PURGED"


class ReimbursementOcrStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class ReimbursementSubmissionStatus(StrEnum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    GENERATING_EXCEL = "GENERATING_EXCEL"
    UPLOADING = "UPLOADING"
    OA_CREATING = "OA_CREATING"
    RECONCILING = "RECONCILING"
    VERIFYING = "VERIFYING"
    SUBMITTED = "SUBMITTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    ORPHAN_CLEANUP = "ORPHAN_CLEANUP"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ReimbursementUploadRole(StrEnum):
    ORIGINAL = "ORIGINAL"
    GENERATED_EXCEL = "GENERATED_EXCEL"
    GENERATED_PDF = "GENERATED_PDF"


class ReimbursementUploadStatus(StrEnum):
    PENDING = "PENDING"
    PUTTING = "PUTTING"
    PUT_DONE = "PUT_DONE"
    COMMITTING = "COMMITTING"
    COMMIT_UNCERTAIN = "COMMIT_UNCERTAIN"
    COMMITTED = "COMMITTED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLEANED = "CLEANED"
    LINKED = "LINKED"
    DISCARDED = "DISCARDED"


class ReimbursementUploadLocalStatus(StrEnum):
    RESERVED = "RESERVED"
    WRITING = "WRITING"
    READY = "READY"
    DELETED = "DELETED"
    FAILED = "FAILED"


_RESUMABLE_SUBMISSION_STATUSES = (
    ReimbursementSubmissionStatus.VALIDATING,
    ReimbursementSubmissionStatus.GENERATING_EXCEL,
    ReimbursementSubmissionStatus.UPLOADING,
    ReimbursementSubmissionStatus.VERIFYING,
    ReimbursementSubmissionStatus.ORPHAN_CLEANUP,
)


class ReimbursementDraft(Base):
    __tablename__ = "reimbursement_drafts"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_values(ReimbursementDraftStatus)})",
            name="ck_reimbursement_drafts_status",
        ),
        CheckConstraint("revision > 0", name="ck_reimbursement_drafts_revision_positive"),
        CheckConstraint(
            "length(schema_fingerprint) = 64",
            name="ck_reimbursement_drafts_schema_fingerprint_length",
        ),
        CheckConstraint(
            "template_config_version > 0",
            name="ck_reimbursement_drafts_template_config_version_positive",
        ),
        CheckConstraint(
            "status != 'LOCKED' OR locked_at IS NOT NULL",
            name="ck_reimbursement_drafts_locked_at",
        ),
        UniqueConstraint(
            "id",
            "corp_id",
            "owner_user_id",
            name="uq_reimbursement_drafts_identity",
        ),
        Index(
            "ix_reimbursement_drafts_owner_updated",
            "corp_id",
            "owner_user_id",
            "updated_at",
        ),
        Index(
            "ix_reimbursement_drafts_status_expires",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    corp_id: Mapped[str] = mapped_column(String(128))
    owner_user_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32),
        default=ReimbursementDraftStatus.DRAFT.value,
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    department_id: Mapped[str] = mapped_column(String(128))
    department_name: Mapped[str] = mapped_column(String(255))
    template_process_code: Mapped[str] = mapped_column(String(128))
    template_config_version: Mapped[int] = mapped_column(Integer)
    schema_fingerprint: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    related_instance_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    expires_at: Mapped[datetime] = mapped_column(DateTime())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class ReimbursementDraftRelatedApproval(Base):
    __tablename__ = "reimbursement_draft_related_approvals"
    __table_args__ = (
        UniqueConstraint(
            "draft_id",
            "process_instance_id",
            name="uq_reimbursement_draft_related_approvals_draft_instance",
        ),
        UniqueConstraint(
            "draft_id",
            "sort_order",
            name="uq_reimbursement_draft_related_approvals_draft_sort_order",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_reimbursement_draft_related_approvals_sort_order",
        ),
        CheckConstraint(
            "catalog_config_version > 0",
            name="ck_reimbursement_draft_related_approvals_catalog_version_positive",
        ),
        CheckConstraint(
            "length(travel_schema_fingerprint) = 64",
            name="ck_reimbursement_draft_related_approvals_fingerprint_length",
        ),
        CheckConstraint(
            "listed_from_ms >= 0 AND listed_to_ms >= listed_from_ms",
            name="ck_reimbursement_draft_related_approvals_listing_window",
        ),
        CheckConstraint(
            "travel_end_date >= travel_start_date",
            name="ck_reimbursement_draft_related_approvals_travel_dates",
        ),
        ForeignKeyConstraint(
            ["draft_id", "corp_id", "owner_user_id"],
            [
                "reimbursement_drafts.id",
                "reimbursement_drafts.corp_id",
                "reimbursement_drafts.owner_user_id",
            ],
            ondelete="CASCADE",
            name="fk_reimbursement_draft_related_approvals_draft_owner",
        ),
        Index(
            "ix_reimbursement_draft_related_approvals_owner_instance",
            "corp_id",
            "owner_user_id",
            "process_instance_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    draft_id: Mapped[str] = mapped_column(String(36))
    corp_id: Mapped[str] = mapped_column(String(128))
    owner_user_id: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer)
    process_instance_id: Mapped[str] = mapped_column(String(128))
    travel_profile_key: Mapped[str] = mapped_column(String(64))
    process_code: Mapped[str] = mapped_column(String(128))
    catalog_config_version: Mapped[int] = mapped_column(Integer)
    travel_schema_fingerprint: Mapped[str] = mapped_column(String(64))
    listed_from_ms: Mapped[int] = mapped_column(BigInteger)
    listed_to_ms: Mapped[int] = mapped_column(BigInteger)
    travel_start_date: Mapped[date] = mapped_column(Date)
    travel_end_date: Mapped[date] = mapped_column(Date)
    source_travel_type_value: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    business_id: Mapped[str] = mapped_column(String(128))
    instance_created_at: Mapped[datetime] = mapped_column(DateTime())
    verified_at: Mapped[datetime] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class ReimbursementDraftFile(Base):
    __tablename__ = "reimbursement_draft_files"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_reimbursement_draft_files_storage_key"),
        UniqueConstraint(
            "part_storage_key",
            name="uq_reimbursement_draft_files_part_storage_key",
        ),
        UniqueConstraint(
            "id",
            "draft_id",
            name="uq_reimbursement_draft_files_identity",
        ),
        UniqueConstraint(
            "draft_id",
            "sort_order",
            name="uq_reimbursement_draft_files_draft_sort_order",
        ),
        CheckConstraint(
            f"processing_role IN ({_sql_values(ReimbursementDraftFileRole)})",
            name="ck_reimbursement_draft_files_processing_role",
        ),
        CheckConstraint(
            f"attachment_kind IN ({_sql_values(ReimbursementAttachmentKind)})",
            name="ck_reimbursement_draft_files_attachment_kind",
        ),
        CheckConstraint(
            f"file_status IN ({_sql_values(ReimbursementDraftFileStatus)})",
            name="ck_reimbursement_draft_files_status",
        ),
        CheckConstraint(
            f"ocr_status IN ({_sql_values(ReimbursementOcrStatus)})",
            name="ck_reimbursement_draft_files_ocr_status",
        ),
        CheckConstraint("sort_order >= 0", name="ck_reimbursement_draft_files_sort_order"),
        CheckConstraint(
            "reserved_bytes > 0",
            name="ck_reimbursement_draft_files_reserved_bytes_positive",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes > 0",
            name="ck_reimbursement_draft_files_size_positive",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes <= reserved_bytes",
            name="ck_reimbursement_draft_files_size_within_reservation",
        ),
        CheckConstraint(
            "sha256 IS NULL OR length(sha256) = 64",
            name="ck_reimbursement_draft_files_sha256_length",
        ),
        CheckConstraint(
            "(size_bytes IS NULL) = (sha256 IS NULL)",
            name="ck_reimbursement_draft_files_metadata_pair",
        ),
        CheckConstraint(
            "file_status NOT IN ('RESERVED', 'WRITING') OR "
            "(part_storage_key IS NOT NULL AND reservation_expires_at IS NOT NULL)",
            name="ck_reimbursement_draft_files_active_reservation",
        ),
        CheckConstraint(
            "file_status != 'ACTIVE' OR "
            "(part_storage_key IS NULL AND size_bytes IS NOT NULL AND sha256 IS NOT NULL)",
            name="ck_reimbursement_draft_files_active_metadata",
        ),
        CheckConstraint(
            "part_storage_key IS NULL OR file_status IN ('RESERVED', 'WRITING')",
            name="ck_reimbursement_draft_files_part_owned_by_active_reservation",
        ),
        CheckConstraint(
            "reservation_expires_at IS NULL OR file_status IN ('RESERVED', 'WRITING')",
            name="ck_reimbursement_draft_files_expiry_for_active_reservation",
        ),
        CheckConstraint(
            "file_status != 'PURGED' OR purged_at IS NOT NULL",
            name="ck_reimbursement_draft_files_purged_at",
        ),
        Index(
            "ix_reimbursement_draft_files_draft_status",
            "draft_id",
            "file_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("reimbursement_drafts.id", ondelete="CASCADE"),
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer)
    processing_role: Mapped[str] = mapped_column(String(32))
    attachment_kind: Mapped[str] = mapped_column(
        String(32), default="other", server_default="other"
    )
    file_status: Mapped[str] = mapped_column(
        String(32),
        default=ReimbursementDraftFileStatus.RESERVED.value,
    )
    storage_key: Mapped[str] = mapped_column(String(255))
    part_storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reserved_bytes: Mapped[int] = mapped_column(Integer)
    reservation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    original_name: Mapped[str] = mapped_column(String(255))
    extension: Mapped[str] = mapped_column(String(16))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocr_status: Mapped[str] = mapped_column(
        String(32),
        default=ReimbursementOcrStatus.NOT_REQUESTED.value,
    )
    ocr_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class ReimbursementSubmission(Base):
    __tablename__ = "reimbursement_submissions"
    __table_args__ = (
        UniqueConstraint("draft_id", name="uq_reimbursement_submissions_draft_id"),
        UniqueConstraint(
            "corp_id",
            "originator_user_id",
            "draft_id",
            "idempotency_key_hash",
            name="uq_reimbursement_submissions_owner_draft_idempotency",
        ),
        UniqueConstraint(
            "id",
            "draft_id",
            name="uq_reimbursement_submissions_identity",
        ),
        UniqueConstraint(
            "process_instance_id",
            name="uq_reimbursement_submissions_process_instance_id",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(ReimbursementSubmissionStatus)})",
            name="ck_reimbursement_submissions_status",
        ),
        CheckConstraint(
            "resume_status IS NULL OR resume_status IN "
            f"({', '.join(repr(item.value) for item in _RESUMABLE_SUBMISSION_STATUSES)})",
            name="ck_reimbursement_submissions_resume_status",
        ),
        CheckConstraint(
            "(status = 'FAILED_RETRYABLE' AND resume_status IS NOT NULL) OR "
            "(status != 'FAILED_RETRYABLE' AND resume_status IS NULL)",
            name="ck_reimbursement_submissions_resume_status_pair",
        ),
        CheckConstraint(
            "status_version > 0",
            name="ck_reimbursement_submissions_status_version_positive",
        ),
        CheckConstraint(
            "template_config_version > 0",
            name="ck_reimbursement_submissions_template_config_version_positive",
        ),
        CheckConstraint(
            "snapshot_version > 0",
            name="ck_reimbursement_submissions_snapshot_version_positive",
        ),
        CheckConstraint(
            "length(schema_fingerprint) = 64",
            name="ck_reimbursement_submissions_schema_fingerprint_length",
        ),
        CheckConstraint(
            "length(snapshot_sha256) = 64",
            name="ck_reimbursement_submissions_snapshot_sha256_length",
        ),
        CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_reimbursement_submissions_idempotency_hash_length",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND reconciliation_attempt_count >= 0",
            name="ck_reimbursement_submissions_attempt_counts",
        ),
        CheckConstraint(
            "status NOT IN ('VERIFYING', 'SUBMITTED') OR process_instance_id IS NOT NULL",
            name="ck_reimbursement_submissions_instance_required",
        ),
        CheckConstraint(
            "process_instance_id IS NULL OR "
            "status IN ('VERIFYING', 'SUBMITTED', 'MANUAL_REVIEW') OR "
            "(status = 'FAILED_RETRYABLE' AND resume_status = 'VERIFYING')",
            name="ck_reimbursement_submissions_instance_safe_status",
        ),
        CheckConstraint(
            "status NOT IN ('OA_CREATING', 'RECONCILING', 'VERIFYING', 'SUBMITTED') OR "
            "(oa_create_started_at IS NOT NULL AND oa_request_hash IS NOT NULL)",
            name="ck_reimbursement_submissions_create_checkpoint",
        ),
        CheckConstraint(
            "status != 'RECONCILING' OR reconciliation_deadline_at IS NOT NULL",
            name="ck_reimbursement_submissions_reconciliation_deadline",
        ),
        CheckConstraint(
            "status != 'ORPHAN_CLEANUP' OR "
            "(orphan_confirmed_at IS NOT NULL AND orphan_confirmation_code IS NOT NULL)",
            name="ck_reimbursement_submissions_orphan_confirmation",
        ),
        CheckConstraint(
            "status != 'SUBMITTED' OR "
            "(submitted_at IS NOT NULL AND business_id IS NOT NULL AND approval_url IS NOT NULL)",
            name="ck_reimbursement_submissions_submitted_at",
        ),
        Index(
            "ix_reimbursement_submissions_owner_updated",
            "corp_id",
            "originator_user_id",
            "updated_at",
        ),
        Index(
            "ix_reimbursement_submissions_due",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        ForeignKeyConstraint(
            ["draft_id", "corp_id", "originator_user_id"],
            [
                "reimbursement_drafts.id",
                "reimbursement_drafts.corp_id",
                "reimbursement_drafts.owner_user_id",
            ],
            ondelete="RESTRICT",
            name="fk_reimbursement_submissions_draft_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    draft_id: Mapped[str] = mapped_column(String(36))
    corp_id: Mapped[str] = mapped_column(String(128))
    originator_user_id: Mapped[str] = mapped_column(String(128))
    originator_union_id: Mapped[str] = mapped_column(String(128))
    originator_name: Mapped[str] = mapped_column(String(128))
    department_id: Mapped[str] = mapped_column(String(128))
    department_name: Mapped[str] = mapped_column(String(255))
    template_process_code: Mapped[str] = mapped_column(String(128))
    template_config_version: Mapped[int] = mapped_column(Integer)
    schema_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot_version: Mapped[int] = mapped_column(Integer, default=6)
    form_snapshot_json: Mapped[str] = mapped_column(Text)
    related_instance_ids_json: Mapped[str] = mapped_column(Text)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    idempotency_key_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32),
        default=ReimbursementSubmissionStatus.QUEUED.value,
    )
    resume_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_version: Mapped[int] = mapped_column(Integer, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    reconciliation_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    oa_create_started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    oa_request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    oa_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    orphan_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    orphan_confirmation_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    orphan_confirmed_by_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    process_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    business_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class ReimbursementUpload(Base):
    __tablename__ = "reimbursement_uploads"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "sort_order",
            name="uq_reimbursement_uploads_submission_sort_order",
        ),
        UniqueConstraint(
            "submission_id",
            "source_draft_file_id",
            name="uq_reimbursement_uploads_submission_source_file",
        ),
        UniqueConstraint(
            "space_id",
            "file_id",
            name="uq_reimbursement_uploads_remote_file",
        ),
        Index(
            "uq_reimbursement_uploads_generated_excel",
            "submission_id",
            unique=True,
            sqlite_where=text("role = 'GENERATED_EXCEL'"),
            postgresql_where=text("role = 'GENERATED_EXCEL'"),
        ),
        Index(
            "uq_reimbursement_uploads_generated_pdf",
            "submission_id",
            unique=True,
            sqlite_where=text("role = 'GENERATED_PDF'"),
            postgresql_where=text("role = 'GENERATED_PDF'"),
        ),
        UniqueConstraint(
            "local_storage_key",
            name="uq_reimbursement_uploads_local_storage_key",
        ),
        UniqueConstraint(
            "local_part_storage_key",
            name="uq_reimbursement_uploads_local_part_storage_key",
        ),
        CheckConstraint(
            f"role IN ({_sql_values(ReimbursementUploadRole)})",
            name="ck_reimbursement_uploads_role",
        ),
        CheckConstraint(
            f"upload_status IN ({_sql_values(ReimbursementUploadStatus)})",
            name="ck_reimbursement_uploads_status",
        ),
        CheckConstraint(
            f"local_status IN ({_sql_values(ReimbursementUploadLocalStatus)})",
            name="ck_reimbursement_uploads_local_status",
        ),
        CheckConstraint(
            "(role = 'ORIGINAL' AND source_draft_file_id IS NOT NULL) OR "
            "(role IN ('GENERATED_EXCEL', 'GENERATED_PDF') "
            "AND source_draft_file_id IS NULL)",
            name="ck_reimbursement_uploads_source_role",
        ),
        CheckConstraint("sort_order >= 0", name="ck_reimbursement_uploads_sort_order"),
        CheckConstraint(
            "reserved_bytes > 0",
            name="ck_reimbursement_uploads_reserved_bytes_positive",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes > 0",
            name="ck_reimbursement_uploads_size_positive",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes <= reserved_bytes",
            name="ck_reimbursement_uploads_size_within_reservation",
        ),
        CheckConstraint(
            "sha256 IS NULL OR length(sha256) = 64",
            name="ck_reimbursement_uploads_sha256_length",
        ),
        CheckConstraint(
            "(size_bytes IS NULL) = (sha256 IS NULL)",
            name="ck_reimbursement_uploads_metadata_pair",
        ),
        CheckConstraint(
            "local_status NOT IN ('RESERVED', 'WRITING') OR "
            "(local_part_storage_key IS NOT NULL AND reservation_expires_at IS NOT NULL)",
            name="ck_reimbursement_uploads_active_reservation",
        ),
        CheckConstraint(
            "local_status != 'READY' OR "
            "(local_part_storage_key IS NULL AND size_bytes IS NOT NULL AND sha256 IS NOT NULL)",
            name="ck_reimbursement_uploads_local_ready_metadata",
        ),
        CheckConstraint(
            "local_part_storage_key IS NULL OR local_status IN ('RESERVED', 'WRITING')",
            name="ck_reimbursement_uploads_part_owned_by_active_reservation",
        ),
        CheckConstraint(
            "reservation_expires_at IS NULL OR local_status IN ('RESERVED', 'WRITING')",
            name="ck_reimbursement_uploads_expiry_for_active_reservation",
        ),
        CheckConstraint(
            "upload_status = 'PENDING' OR local_status IN ('READY', 'DELETED')",
            name="ck_reimbursement_uploads_remote_requires_local_complete",
        ),
        CheckConstraint(
            "upload_status IN ('PENDING', 'DISCARDED') OR "
            "(size_bytes IS NOT NULL AND sha256 IS NOT NULL)",
            name="ck_reimbursement_uploads_remote_ready_metadata",
        ),
        CheckConstraint(
            "upload_status != 'DISCARDED' OR "
            "(local_status = 'DELETED' AND local_deleted_at IS NOT NULL "
            "AND space_id IS NULL AND file_id IS NULL)",
            name="ck_reimbursement_uploads_discarded_terminal_shape",
        ),
        CheckConstraint(
            "status_version > 0 AND attempt_count >= 0",
            name="ck_reimbursement_uploads_versions",
        ),
        CheckConstraint(
            "upload_status NOT IN ('COMMITTED', 'CLEANUP_PENDING', 'CLEANED', 'LINKED') "
            "OR (space_id IS NOT NULL AND file_id IS NOT NULL)",
            name="ck_reimbursement_uploads_remote_identity",
        ),
        CheckConstraint(
            "(space_id IS NULL) = (file_id IS NULL)",
            name="ck_reimbursement_uploads_remote_identity_pair",
        ),
        CheckConstraint(
            "upload_status != 'LINKED' OR linked_at IS NOT NULL",
            name="ck_reimbursement_uploads_linked_at",
        ),
        CheckConstraint(
            "linked_at IS NULL OR upload_status = 'LINKED'",
            name="ck_reimbursement_uploads_linked_terminal",
        ),
        CheckConstraint(
            "upload_status != 'CLEANED' OR cleaned_at IS NOT NULL",
            name="ck_reimbursement_uploads_cleaned_at",
        ),
        CheckConstraint(
            "cleaned_at IS NULL OR upload_status = 'CLEANED'",
            name="ck_reimbursement_uploads_cleaned_terminal",
        ),
        CheckConstraint(
            "upload_status NOT IN ('CLEANUP_PENDING', 'CLEANED') OR cleanup_started_at IS NOT NULL",
            name="ck_reimbursement_uploads_cleanup_started_at",
        ),
        CheckConstraint(
            "local_deleted_at IS NULL OR "
            "(local_status = 'DELETED' AND upload_status IN ('LINKED', 'CLEANED', 'DISCARDED'))",
            name="ck_reimbursement_uploads_local_delete_safe_status",
        ),
        CheckConstraint(
            "local_status != 'DELETED' OR local_deleted_at IS NOT NULL",
            name="ck_reimbursement_uploads_local_deleted_at",
        ),
        Index(
            "ix_reimbursement_uploads_submission_status",
            "submission_id",
            "upload_status",
        ),
        ForeignKeyConstraint(
            ["submission_id", "draft_id"],
            ["reimbursement_submissions.id", "reimbursement_submissions.draft_id"],
            ondelete="CASCADE",
            name="fk_reimbursement_uploads_submission_draft",
        ),
        ForeignKeyConstraint(
            ["source_draft_file_id", "draft_id"],
            ["reimbursement_draft_files.id", "reimbursement_draft_files.draft_id"],
            ondelete="RESTRICT",
            name="fk_reimbursement_uploads_source_draft",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    submission_id: Mapped[str] = mapped_column(String(36), index=True)
    draft_id: Mapped[str] = mapped_column(String(36))
    source_draft_file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(Integer)
    local_storage_key: Mapped[str] = mapped_column(String(255))
    local_part_storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    local_status: Mapped[str] = mapped_column(
        String(32),
        default=ReimbursementUploadLocalStatus.READY.value,
    )
    reserved_bytes: Mapped[int] = mapped_column(Integer)
    reservation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(32))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upload_status: Mapped[str] = mapped_column(
        String(32),
        default=ReimbursementUploadStatus.PENDING.value,
    )
    status_version: Mapped[int] = mapped_column(Integer, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    space_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    put_started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    commit_started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    cleanup_started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    local_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


# These triggers are also declared verbatim in migration 0009. Attaching them to
# the final table keeps test/dev schemas created via ``Base.metadata.create_all``
# subject to the same cross-row safety boundary as migrated databases.
_SQLITE_REIMBURSEMENT_SAFETY_TRIGGERS = (
    """
    CREATE TRIGGER trg_reimbursement_submissions_snapshot_immutable
    BEFORE UPDATE ON reimbursement_submissions
    WHEN NEW.draft_id IS NOT OLD.draft_id
         OR NEW.corp_id IS NOT OLD.corp_id
         OR NEW.originator_user_id IS NOT OLD.originator_user_id
         OR NEW.originator_union_id IS NOT OLD.originator_union_id
         OR NEW.originator_name IS NOT OLD.originator_name
         OR NEW.department_id IS NOT OLD.department_id
         OR NEW.department_name IS NOT OLD.department_name
         OR NEW.template_process_code IS NOT OLD.template_process_code
         OR NEW.template_config_version IS NOT OLD.template_config_version
         OR NEW.schema_fingerprint IS NOT OLD.schema_fingerprint
         OR NEW.snapshot_version IS NOT OLD.snapshot_version
         OR NEW.form_snapshot_json IS NOT OLD.form_snapshot_json
         OR NEW.related_instance_ids_json IS NOT OLD.related_instance_ids_json
         OR NEW.snapshot_sha256 IS NOT OLD.snapshot_sha256
         OR NEW.idempotency_key_hash IS NOT OLD.idempotency_key_hash
    BEGIN
        SELECT RAISE(ABORT, 'reimbursement submission snapshot is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_oa_request_immutable
    BEFORE UPDATE OF oa_create_started_at, oa_request_json, oa_request_hash
    ON reimbursement_submissions
    WHEN OLD.oa_create_started_at IS NOT NULL
         AND (
             NEW.oa_create_started_at IS NOT OLD.oa_create_started_at
             OR NEW.oa_request_json IS NOT OLD.oa_request_json
             OR NEW.oa_request_hash IS NOT OLD.oa_request_hash
         )
    BEGIN
        SELECT RAISE(ABORT, 'OA create checkpoint is immutable once set');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_process_instance_immutable
    BEFORE UPDATE OF process_instance_id ON reimbursement_submissions
    WHEN OLD.process_instance_id IS NOT NULL
         AND NEW.process_instance_id IS NOT OLD.process_instance_id
    BEGIN
        SELECT RAISE(ABORT, 'process_instance_id is immutable once set');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_submitted_terminal
    BEFORE UPDATE OF status ON reimbursement_submissions
    WHEN OLD.status = 'SUBMITTED' AND NEW.status IS NOT OLD.status
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement is terminal');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_discarded_terminal
    BEFORE UPDATE OF upload_status ON reimbursement_uploads
    WHEN OLD.upload_status = 'DISCARDED' AND NEW.upload_status IS NOT 'DISCARDED'
    BEGIN
        SELECT RAISE(ABORT, 'discarded upload is terminal');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_linked_at_immutable
    BEFORE UPDATE OF linked_at ON reimbursement_uploads
    WHEN OLD.linked_at IS NOT NULL AND NEW.linked_at IS NOT OLD.linked_at
    BEGIN
        SELECT RAISE(ABORT, 'linked_at is immutable once set');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_cleaned_at_immutable
    BEFORE UPDATE OF cleaned_at ON reimbursement_uploads
    WHEN OLD.cleaned_at IS NOT NULL AND NEW.cleaned_at IS NOT OLD.cleaned_at
    BEGIN
        SELECT RAISE(ABORT, 'cleaned_at is immutable once set');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_local_deleted_at_immutable
    BEFORE UPDATE OF local_deleted_at ON reimbursement_uploads
    WHEN OLD.local_deleted_at IS NOT NULL
         AND NEW.local_deleted_at IS NOT OLD.local_deleted_at
    BEGIN
        SELECT RAISE(ABORT, 'local_deleted_at is immutable once set');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_remote_identity_immutable
    BEFORE UPDATE OF space_id, file_id ON reimbursement_uploads
    WHEN OLD.file_id IS NOT NULL
         AND (NEW.file_id IS NOT OLD.file_id OR NEW.space_id IS NOT OLD.space_id)
    BEGIN
        SELECT RAISE(ABORT, 'remote file identity is immutable once set');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_parent_immutable
    BEFORE UPDATE OF submission_id, draft_id ON reimbursement_uploads
    WHEN NEW.submission_id IS NOT OLD.submission_id OR NEW.draft_id IS NOT OLD.draft_id
    BEGIN
        SELECT RAISE(ABORT, 'upload parent is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_submitted_manifest_insert
    BEFORE INSERT ON reimbursement_submissions
    WHEN NEW.status = 'SUBMITTED'
         AND (
             NOT EXISTS (
                 SELECT 1
                 FROM reimbursement_uploads AS upload
                 WHERE upload.submission_id = NEW.id
                   AND upload.draft_id = NEW.draft_id
                   AND upload.role = 'GENERATED_EXCEL'
                   AND upload.upload_status = 'LINKED'
             )
             OR EXISTS (
                 SELECT 1
                 FROM reimbursement_uploads AS upload
                 WHERE upload.submission_id = NEW.id
                   AND upload.draft_id = NEW.draft_id
                   AND upload.upload_status IS NOT 'LINKED'
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement requires a fully linked manifest');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_submitted_manifest_update
    BEFORE UPDATE ON reimbursement_submissions
    WHEN NEW.status = 'SUBMITTED'
         AND (
             NOT EXISTS (
                 SELECT 1
                 FROM reimbursement_uploads AS upload
                 WHERE upload.submission_id = NEW.id
                   AND upload.draft_id = NEW.draft_id
                   AND upload.role = 'GENERATED_EXCEL'
                   AND upload.upload_status = 'LINKED'
             )
             OR EXISTS (
                 SELECT 1
                 FROM reimbursement_uploads AS upload
                 WHERE upload.submission_id = NEW.id
                   AND upload.draft_id = NEW.draft_id
                   AND upload.upload_status IS NOT 'LINKED'
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement requires a fully linked manifest');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_submitted_parent_insert
    BEFORE INSERT ON reimbursement_uploads
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_submissions AS submission
             WHERE submission.id = NEW.submission_id
               AND submission.draft_id = NEW.draft_id
               AND submission.status = 'SUBMITTED'
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement cannot accept new uploads');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_submitted_parent_delete
    BEFORE DELETE ON reimbursement_uploads
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_submissions AS submission
             WHERE submission.id = OLD.submission_id
               AND submission.draft_id = OLD.draft_id
               AND submission.status = 'SUBMITTED'
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement cannot lose uploads');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_submitted_manifest_update
    BEFORE UPDATE ON reimbursement_uploads
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_submissions AS submission
             WHERE submission.id = OLD.submission_id
               AND submission.draft_id = OLD.draft_id
               AND submission.status = 'SUBMITTED'
         )
         AND (
             NEW.submission_id IS NOT OLD.submission_id
             OR NEW.draft_id IS NOT OLD.draft_id
             OR NEW.role IS NOT OLD.role
             OR NEW.source_draft_file_id IS NOT OLD.source_draft_file_id
             OR NEW.sort_order IS NOT OLD.sort_order
             OR NEW.local_storage_key IS NOT OLD.local_storage_key
             OR NEW.reserved_bytes IS NOT OLD.reserved_bytes
             OR NEW.file_name IS NOT OLD.file_name
             OR NEW.file_type IS NOT OLD.file_type
             OR NEW.media_type IS NOT OLD.media_type
             OR NEW.size_bytes IS NOT OLD.size_bytes
             OR NEW.sha256 IS NOT OLD.sha256
             OR NEW.upload_status IS NOT OLD.upload_status
             OR NEW.space_id IS NOT OLD.space_id
             OR NEW.file_id IS NOT OLD.file_id
             OR NEW.linked_at IS NOT OLD.linked_at
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement upload manifest is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_linked_parent_insert
    BEFORE INSERT ON reimbursement_uploads
    WHEN NEW.upload_status = 'LINKED'
         AND NOT EXISTS (
             SELECT 1
             FROM reimbursement_submissions AS submission
             WHERE submission.id = NEW.submission_id
               AND submission.draft_id = NEW.draft_id
               AND submission.process_instance_id IS NOT NULL
               AND (
                   submission.status IN ('VERIFYING', 'SUBMITTED', 'MANUAL_REVIEW')
                   OR (
                       submission.status = 'FAILED_RETRYABLE'
                       AND submission.resume_status = 'VERIFYING'
                   )
               )
         )
    BEGIN
        SELECT RAISE(ABORT, 'linked upload requires a confirmed OA instance');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_linked_parent_update
    BEFORE UPDATE ON reimbursement_uploads
    WHEN NEW.upload_status = 'LINKED'
         AND NOT EXISTS (
             SELECT 1
             FROM reimbursement_submissions AS submission
             WHERE submission.id = NEW.submission_id
               AND submission.draft_id = NEW.draft_id
               AND submission.process_instance_id IS NOT NULL
               AND (
                   submission.status IN ('VERIFYING', 'SUBMITTED', 'MANUAL_REVIEW')
                   OR (
                       submission.status = 'FAILED_RETRYABLE'
                       AND submission.resume_status = 'VERIFYING'
                   )
               )
         )
    BEGIN
        SELECT RAISE(ABORT, 'linked upload requires a confirmed OA instance');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_cleanup_parent_insert
    BEFORE INSERT ON reimbursement_uploads
    WHEN NEW.upload_status IN ('CLEANUP_PENDING', 'CLEANED')
         AND (
             NOT EXISTS (
                 SELECT 1
                 FROM reimbursement_submissions AS submission
                 WHERE submission.id = NEW.submission_id
                   AND submission.draft_id = NEW.draft_id
                   AND submission.process_instance_id IS NULL
                   AND (
                       submission.status = 'ORPHAN_CLEANUP'
                       OR (
                           NEW.upload_status = 'CLEANED'
                           AND submission.status = 'FAILED_FINAL'
                       )
                   )
             )
             OR EXISTS (
                 SELECT 1
                 FROM reimbursement_uploads AS upload
                 WHERE upload.submission_id = NEW.submission_id
                   AND upload.upload_status = 'LINKED'
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'remote cleanup requires an uncreated orphan submission');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_uploads_cleanup_parent_update
    BEFORE UPDATE ON reimbursement_uploads
    WHEN NEW.upload_status IN ('CLEANUP_PENDING', 'CLEANED')
         AND (
             NOT EXISTS (
                 SELECT 1
                 FROM reimbursement_submissions AS submission
                 WHERE submission.id = NEW.submission_id
                   AND submission.draft_id = NEW.draft_id
                   AND submission.process_instance_id IS NULL
                   AND (
                       submission.status = 'ORPHAN_CLEANUP'
                       OR (
                           NEW.upload_status = 'CLEANED'
                           AND submission.status = 'FAILED_FINAL'
                       )
                   )
             )
             OR EXISTS (
                 SELECT 1
                 FROM reimbursement_uploads AS upload
                 WHERE upload.submission_id = NEW.submission_id
                   AND upload.upload_status = 'LINKED'
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'remote cleanup requires an uncreated orphan submission');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_cleanup_guard_insert
    BEFORE INSERT ON reimbursement_submissions
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_uploads AS upload
             WHERE upload.submission_id = NEW.id
               AND upload.upload_status IN ('CLEANUP_PENDING', 'CLEANED')
         )
         AND (
             NEW.status IS NOT 'ORPHAN_CLEANUP'
             OR NEW.process_instance_id IS NOT NULL
         )
    BEGIN
        SELECT RAISE(ABORT, 'OA instance and remote cleanup cannot coexist');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_cleanup_guard_update
    BEFORE UPDATE ON reimbursement_submissions
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_uploads AS upload
             WHERE upload.submission_id = OLD.id
               AND upload.upload_status IN ('CLEANUP_PENDING', 'CLEANED')
         )
         AND (
             NEW.id IS NOT OLD.id
             OR NEW.process_instance_id IS NOT NULL
             OR (
                 NEW.status IS NOT 'ORPHAN_CLEANUP'
                 AND (
                     NEW.status IS NOT 'FAILED_FINAL'
                     OR EXISTS (
                         SELECT 1
                         FROM reimbursement_uploads AS unfinished
                         WHERE unfinished.submission_id = OLD.id
                           AND unfinished.upload_status NOT IN (
                               'PENDING', 'PUTTING', 'PUT_DONE', 'CLEANED', 'DISCARDED'
                           )
                     )
                 )
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'OA instance and remote cleanup cannot coexist');
    END
    """,
    """
    CREATE TRIGGER trg_reimbursement_submissions_linked_guard_update
    BEFORE UPDATE ON reimbursement_submissions
    WHEN EXISTS (
             SELECT 1
             FROM reimbursement_uploads AS upload
             WHERE upload.submission_id = OLD.id
               AND upload.upload_status = 'LINKED'
         )
         AND (
             NEW.id IS NOT OLD.id
             OR NEW.process_instance_id IS NULL
             OR NOT (
                 NEW.status IN ('VERIFYING', 'SUBMITTED', 'MANUAL_REVIEW')
                 OR (
                     NEW.status = 'FAILED_RETRYABLE'
                     AND NEW.resume_status = 'VERIFYING'
                 )
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'linked upload requires a confirmed OA instance');
    END
    """,
)

_SQLITE_BUNDLE_MANIFEST_TRIGGERS = tuple(
    f"""
    CREATE TRIGGER trg_reimbursement_submissions_bundle_manifest_{operation.lower()}
    BEFORE {operation} ON reimbursement_submissions
    WHEN NEW.snapshot_version >= 2 AND NEW.status = 'SUBMITTED'
         AND (
             (SELECT count(*) FROM reimbursement_uploads WHERE submission_id = NEW.id) != 2
             OR NOT EXISTS (
                 SELECT 1 FROM reimbursement_uploads
                 WHERE submission_id = NEW.id AND role = 'GENERATED_PDF'
                   AND sort_order = 0 AND upload_status = 'LINKED'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM reimbursement_uploads
                 WHERE submission_id = NEW.id AND role = 'GENERATED_EXCEL'
                   AND sort_order = 1 AND upload_status = 'LINKED'
             )
         )
    BEGIN
        SELECT RAISE(ABORT, 'submitted reimbursement requires PDF and Excel');
    END
    """
    for operation in ("INSERT", "UPDATE")
)

for _trigger_ddl in _SQLITE_REIMBURSEMENT_SAFETY_TRIGGERS + _SQLITE_BUNDLE_MANIFEST_TRIGGERS:
    event.listen(
        ReimbursementUpload.__table__,
        "after_create",
        DDL(_trigger_ddl).execute_if(dialect="sqlite"),
    )
