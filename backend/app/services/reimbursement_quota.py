from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias

from sqlalchemy import Engine, Select, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import ApiError
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
    new_uuid,
    utc_now,
)
from app.services.reimbursement_drafts import DraftActor, bump_owned_draft_revision
from app.services.reimbursement_staging import (
    ReimbursementStaging,
    ReimbursementStagingError,
    StagedObject,
    StagingArea,
    StagingReservation,
)

_GENERATED_EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_GENERATED_ROLES = {
    ReimbursementUploadRole.GENERATED_EXCEL.value,
    ReimbursementUploadRole.GENERATED_PDF.value,
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACTIVE_DRAFT_STATUSES = frozenset(
    {ReimbursementDraftStatus.DRAFT.value, ReimbursementDraftStatus.REVIEW_READY.value}
)
_LOGGER = logging.getLogger(__name__)


class ReimbursementQuotaError(RuntimeError):
    """Base class for deterministic staging-quota failures."""


class ReimbursementQuotaExceeded(ReimbursementQuotaError):
    def __init__(self, *, maximum_bytes: int, reserved_bytes: int, requested_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.reserved_bytes = reserved_bytes
        self.requested_bytes = requested_bytes
        super().__init__("reimbursement staging quota is exhausted")


class ReimbursementReservationConflict(ReimbursementQuotaError):
    pass


class ReservationKind(StrEnum):
    DRAFT_FILE = "DRAFT_FILE"
    GENERATED_UPLOAD = "GENERATED_UPLOAD"


@dataclass(frozen=True, slots=True)
class DraftFileOwner:
    corp_id: str
    user_id: str
    draft_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class SubmissionLease:
    corp_id: str
    user_id: str
    submission_id: str
    lease_token: str
    expected_status_version: int


ReservationAuthority: TypeAlias = DraftFileOwner | SubmissionLease


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    kind: ReservationKind
    record_id: str
    staging: StagingReservation


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    reserved_bytes: int
    maximum_bytes: int

    @property
    def available_bytes(self) -> int:
        return max(0, self.maximum_bytes - self.reserved_bytes)


@dataclass(frozen=True, slots=True)
class _DraftFileCleanup:
    file_status: str
    storage_key: str
    part_storage_key: str | None
    reserved_bytes: int
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class _ExpiredDraftCleanup:
    draft_id: str
    actor: DraftActor
    revision: int


@dataclass(frozen=True, slots=True)
class _DeletingDraftFileCleanup:
    file_id: str
    draft_id: str
    actor: DraftActor
    draft_revision: int
    draft_status: str
    draft_expires_at: datetime
    storage_key: str
    reserved_bytes: int
    size_bytes: int
    sha256: str


class ReimbursementQuotaCoordinator:
    """Serialize quota admission and reservation lifecycle in SQLite.

    Every mutating method owns its database transaction. Callers therefore
    cannot accidentally perform the usage check and reservation write in two
    different transactions.
    """

    def __init__(
        self,
        engine: Engine,
        staging: ReimbursementStaging,
        *,
        max_bytes: int,
    ) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("reimbursement quota currently requires SQLite")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes < staging.max_object_bytes:
            raise ValueError("max_bytes must cover one maximum-size staging object")
        self._engine = engine
        self._staging = staging
        self._max_bytes = max_bytes

    def usage(self) -> QuotaUsage:
        with self._engine.connect() as connection:
            reserved_bytes = int(connection.execute(_usage_statement()).scalar_one())
        return QuotaUsage(reserved_bytes=reserved_bytes, maximum_bytes=self._max_bytes)

    def reserve_draft_file(
        self,
        owner: DraftFileOwner,
        *,
        sort_order: int,
        processing_role: ReimbursementDraftFileRole,
        original_name: str,
        extension: str,
        media_type: str,
        reserved_bytes: int,
        expires_at: datetime,
        attachment_kind: str = "other",
        ocr_result_json: str | None = None,
    ) -> QuotaReservation:
        _require_positive_size(reserved_bytes)
        _require_sort_order(sort_order)
        if not isinstance(processing_role, ReimbursementDraftFileRole):
            raise ValueError("processing_role is invalid")
        now = utc_now()
        expiry = _naive_utc(expires_at)
        if expiry <= now:
            raise ValueError("reservation expiry must be in the future")
        record_id = new_uuid()
        staging_reservation = self._staging.new_reservation(
            StagingArea.DRAFTS,
            owner.draft_id,
            extension,
            reserved_bytes=reserved_bytes,
        )

        try:
            with self._write_session() as database:
                draft = database.scalar(_draft_owner_query(owner))
                if draft is None or draft.status not in _ACTIVE_DRAFT_STATUSES:
                    raise ReimbursementReservationConflict(
                        "draft ownership, revision, or state changed"
                    )
                self._admit(database, reserved_bytes)
                claimed_revision = bump_owned_draft_revision(
                    database,
                    draft_id=draft.id,
                    actor=DraftActor(
                        corp_id=owner.corp_id,
                        user_id=owner.user_id,
                        department_id=draft.department_id,
                        department_name=draft.department_name,
                    ),
                    expected_revision=owner.expected_revision,
                    now=now,
                )
                if claimed_revision != _draft_operation_revision(owner):
                    raise ReimbursementReservationConflict(
                        "draft reservation claimed an unexpected revision"
                    )
                database.add(
                    ReimbursementDraftFile(
                        id=record_id,
                        draft_id=draft.id,
                        sort_order=sort_order,
                        processing_role=processing_role.value,
                        attachment_kind=attachment_kind,
                        file_status=ReimbursementDraftFileStatus.RESERVED.value,
                        storage_key=staging_reservation.storage_key,
                        part_storage_key=staging_reservation.part_storage_key,
                        reserved_bytes=reserved_bytes,
                        reservation_expires_at=expiry,
                        original_name=_required_text(original_name, maximum=255),
                        extension=extension.lower().lstrip("."),
                        media_type=_required_text(media_type, maximum=128),
                        size_bytes=None,
                        sha256=None,
                        ocr_result_json=ocr_result_json,
                    )
                )
        except IntegrityError as exc:
            raise ReimbursementReservationConflict("draft file reservation conflicts") from exc

        return QuotaReservation(
            kind=ReservationKind.DRAFT_FILE,
            record_id=record_id,
            staging=staging_reservation,
        )

    def reserve_generated_upload(
        self,
        lease: SubmissionLease,
        *,
        sort_order: int,
        file_name: str,
        reserved_bytes: int,
        expires_at: datetime,
        role: ReimbursementUploadRole = ReimbursementUploadRole.GENERATED_EXCEL,
    ) -> QuotaReservation:
        _require_positive_size(reserved_bytes)
        _require_sort_order(sort_order)
        normalized_name = _required_text(file_name, maximum=255)
        if not isinstance(role, ReimbursementUploadRole) or role not in _GENERATED_ROLES:
            raise ValueError("generated file role is invalid")
        extension = "pdf" if role == ReimbursementUploadRole.GENERATED_PDF else "xlsx"
        if not normalized_name.casefold().endswith(f".{extension}"):
            raise ValueError(f"generated file name must end in .{extension}")
        now = utc_now()
        expiry = _naive_utc(expires_at)
        if expiry <= now:
            raise ValueError("reservation expiry must be in the future")
        record_id = new_uuid()
        staging_reservation = self._staging.new_reservation(
            StagingArea.GENERATED,
            lease.submission_id,
            extension,
            reserved_bytes=reserved_bytes,
        )

        try:
            with self._write_session() as database:
                submission = database.scalar(_submission_lease_query(lease, now=now))
                if submission is None:
                    raise ReimbursementReservationConflict(
                        "submission ownership, lease, version, or state changed"
                    )
                self._admit(database, reserved_bytes)
                database.add(
                    ReimbursementUpload(
                        id=record_id,
                        submission_id=submission.id,
                        draft_id=submission.draft_id,
                        source_draft_file_id=None,
                        role=role.value,
                        sort_order=sort_order,
                        local_storage_key=staging_reservation.storage_key,
                        local_part_storage_key=staging_reservation.part_storage_key,
                        local_status=ReimbursementUploadLocalStatus.RESERVED.value,
                        reserved_bytes=reserved_bytes,
                        reservation_expires_at=expiry,
                        file_name=normalized_name,
                        file_type=extension,
                        media_type=(
                            "application/pdf" if extension == "pdf" else _GENERATED_EXCEL_MEDIA_TYPE
                        ),
                        size_bytes=None,
                        sha256=None,
                        upload_status=ReimbursementUploadStatus.PENDING.value,
                        status_version=1,
                        attempt_count=0,
                    )
                )
        except IntegrityError as exc:
            raise ReimbursementReservationConflict(
                "generated workbook reservation conflicts"
            ) from exc

        return QuotaReservation(
            kind=ReservationKind.GENERATED_UPLOAD,
            record_id=record_id,
            staging=staging_reservation,
        )

    def mark_writing(
        self,
        authority: ReservationAuthority,
        reservation: QuotaReservation,
    ) -> None:
        now = utc_now()
        with self._write_session() as database:
            record = self._owned_record(database, authority, reservation, now=now)
            if _local_status(record) != ReimbursementDraftFileStatus.RESERVED.value:
                raise ReimbursementReservationConflict("reservation is not awaiting a writer")
            if record.reservation_expires_at is None or record.reservation_expires_at <= now:
                raise ReimbursementReservationConflict("reservation has expired")
            _set_local_status(record, ReimbursementDraftFileStatus.WRITING.value)

    def finalize(
        self,
        authority: ReservationAuthority,
        reservation: QuotaReservation,
        staged: StagedObject,
    ) -> None:
        _validate_staged_object(reservation, staged)

        with self._write_session() as database:
            record = self._owned_record(database, authority, reservation, now=utc_now())
            if _local_status(record) != ReimbursementDraftFileStatus.WRITING.value:
                raise ReimbursementReservationConflict("reservation is not being written")
            record.size_bytes = staged.size_bytes
            record.sha256 = staged.sha256
            record.reservation_expires_at = None
            if isinstance(record, ReimbursementDraftFile):
                record.file_status = ReimbursementDraftFileStatus.ACTIVE.value
                record.part_storage_key = None
            else:
                record.local_status = ReimbursementUploadLocalStatus.READY.value
                record.local_part_storage_key = None

    def finalize_draft_file(
        self,
        owner: DraftFileOwner,
        reservation: QuotaReservation,
        staged: StagedObject,
        *,
        actor: DraftActor,
    ) -> int:
        """Finalize a durable draft file and claim its draft revision atomically."""

        _validate_staged_object(reservation, staged)
        if reservation.kind is not ReservationKind.DRAFT_FILE:
            raise ReimbursementReservationConflict("reservation is not a draft file")
        if actor.corp_id != owner.corp_id or actor.user_id != owner.user_id:
            raise ReimbursementReservationConflict("draft file actor does not own reservation")
        with self._write_session() as database:
            record = self._owned_record(database, owner, reservation, now=utc_now())
            if not isinstance(record, ReimbursementDraftFile):
                raise ReimbursementReservationConflict("reservation is not a draft file")
            if record.file_status != ReimbursementDraftFileStatus.WRITING.value:
                raise ReimbursementReservationConflict("reservation is not being written")
            record.size_bytes = staged.size_bytes
            record.sha256 = staged.sha256
            record.reservation_expires_at = None
            record.file_status = ReimbursementDraftFileStatus.ACTIVE.value
            record.part_storage_key = None
        return _draft_operation_revision(owner)

    def abandon_draft_file(
        self,
        owner: DraftFileOwner,
        reservation: QuotaReservation,
    ) -> None:
        """Discard only this unfinished draft reservation, even after revision drift.

        The exact generated storage and part keys remain mandatory. An ACTIVE
        file is never touched, which makes this safe after an uncertain local
        finalize result.
        """

        if reservation.kind is not ReservationKind.DRAFT_FILE:
            raise ReimbursementReservationConflict("reservation is not a draft file")
        expected = reservation.staging
        with self._write_session() as database:
            record = database.scalar(
                select(ReimbursementDraftFile)
                .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
                .where(
                    ReimbursementDraftFile.id == reservation.record_id,
                    ReimbursementDraftFile.draft_id == owner.draft_id,
                    ReimbursementDraftFile.storage_key == expected.storage_key,
                    ReimbursementDraftFile.part_storage_key == expected.part_storage_key,
                    ReimbursementDraftFile.reserved_bytes == expected.reserved_bytes,
                    ReimbursementDraftFile.file_status.in_(
                        {
                            ReimbursementDraftFileStatus.RESERVED.value,
                            ReimbursementDraftFileStatus.WRITING.value,
                        }
                    ),
                    ReimbursementDraft.corp_id == owner.corp_id,
                    ReimbursementDraft.owner_user_id == owner.user_id,
                )
            )
            if record is None:
                # A successful finalize removes the part key and is deliberately
                # indistinguishable here from any other non-abandonable state.
                raise ReimbursementReservationConflict(
                    "unfinished draft reservation no longer exists"
                )
            self._staging.discard_reservation(expected)
            record.file_status = ReimbursementDraftFileStatus.PURGED.value
            record.part_storage_key = None
            record.reservation_expires_at = None
            record.purged_at = utc_now()

    def release(
        self,
        authority: ReservationAuthority,
        reservation: QuotaReservation,
    ) -> None:
        """Delete the exact local object before releasing its database quota."""

        now = utc_now()
        with self._write_session() as database:
            record = self._owned_release_record(database, authority, reservation, now=now)
            local_status = _local_status(record)
            if local_status in {
                ReimbursementDraftFileStatus.RESERVED.value,
                ReimbursementDraftFileStatus.WRITING.value,
            }:
                if isinstance(record, ReimbursementUpload) and (
                    record.upload_status != ReimbursementUploadStatus.PENDING.value
                ):
                    raise ReimbursementReservationConflict(
                        "remote upload state still requires its local reservation"
                    )
                self._staging.discard_reservation(reservation.staging)
            elif isinstance(record, ReimbursementDraftFile) and (
                local_status == ReimbursementDraftFileStatus.ACTIVE.value
            ):
                referenced = database.scalar(
                    select(ReimbursementUpload.id)
                    .where(ReimbursementUpload.source_draft_file_id == record.id)
                    .limit(1)
                )
                if referenced is not None or record.size_bytes is None or record.sha256 is None:
                    raise ReimbursementReservationConflict(
                        "draft file is still required by a submission"
                    )
                self._staging.delete(
                    record.storage_key,
                    expected_size=record.size_bytes,
                    expected_sha256=record.sha256,
                    missing_ok=True,
                )
            elif isinstance(record, ReimbursementUpload) and (
                local_status == ReimbursementUploadLocalStatus.READY.value
            ):
                if record.upload_status not in {
                    ReimbursementUploadStatus.LINKED.value,
                    ReimbursementUploadStatus.CLEANED.value,
                    ReimbursementUploadStatus.DISCARDED.value,
                }:
                    raise ReimbursementReservationConflict(
                        "remote upload state still requires its local object"
                    )
                if record.size_bytes is None or record.sha256 is None:
                    raise ReimbursementReservationConflict("local object metadata is incomplete")
                if record.upload_status == ReimbursementUploadStatus.LINKED.value:
                    confirmed_instance_id = database.scalar(
                        select(ReimbursementSubmission.process_instance_id).where(
                            ReimbursementSubmission.id == record.submission_id,
                            ReimbursementSubmission.draft_id == record.draft_id,
                            ReimbursementSubmission.process_instance_id.is_not(None),
                        )
                    )
                    if confirmed_instance_id is None or not record.space_id or not record.file_id:
                        raise ReimbursementReservationConflict(
                            "linked upload has not been confirmed against an OA instance"
                        )
                self._staging.delete(
                    record.local_storage_key,
                    expected_size=record.size_bytes,
                    expected_sha256=record.sha256,
                    missing_ok=True,
                )
            else:
                raise ReimbursementReservationConflict("local reservation is not releasable")

            if isinstance(record, ReimbursementDraftFile):
                record.file_status = ReimbursementDraftFileStatus.PURGED.value
                record.part_storage_key = None
                record.reservation_expires_at = None
                record.purged_at = now
            else:
                if record.upload_status == ReimbursementUploadStatus.PENDING.value:
                    record.upload_status = ReimbursementUploadStatus.DISCARDED.value
                record.local_status = ReimbursementUploadLocalStatus.DELETED.value
                record.local_part_storage_key = None
                record.reservation_expires_at = None
                record.local_deleted_at = now
                record.status_version += 1

    def delete_owned_draft(
        self,
        *,
        actor: DraftActor,
        draft_id: str,
        expected_revision: int,
    ) -> str:
        """Persist a delete intent, clean local files, then delete the draft."""

        if not isinstance(actor, DraftActor):
            raise ValueError("draft actor is required")
        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        normalized_draft_id = _required_text(draft_id, maximum=36)
        now = utc_now()

        with self._write_session() as database:
            draft = database.scalar(
                select(ReimbursementDraft).where(
                    ReimbursementDraft.id == normalized_draft_id,
                    ReimbursementDraft.corp_id == actor.corp_id,
                    ReimbursementDraft.owner_user_id == actor.user_id,
                )
            )
            if draft is None:
                raise _draft_not_found_error()
            _require_draft_department(draft, actor)
            if draft.status == ReimbursementDraftStatus.LOCKED.value or draft.locked_at is not None:
                raise _draft_locked_error()
            if (
                database.scalar(
                    select(ReimbursementSubmission.id)
                    .where(ReimbursementSubmission.draft_id == draft.id)
                    .limit(1)
                )
                is not None
            ):
                raise _draft_in_use_error()

            if draft.status == ReimbursementDraftStatus.EXPIRED.value:
                operation_revision = draft.revision
                if expected_revision not in {
                    operation_revision,
                    operation_revision - 1,
                }:
                    raise _draft_revision_conflict_error()
            else:
                if (
                    draft.status not in _ACTIVE_DRAFT_STATUSES
                    or draft.revision != expected_revision
                ):
                    raise _draft_revision_conflict_error()
                operation_revision = expected_revision + 1
                draft.status = ReimbursementDraftStatus.EXPIRED.value
                draft.revision = operation_revision
                draft.updated_at = now

            cleanup: list[_DraftFileCleanup] = []
            files = database.scalars(
                select(ReimbursementDraftFile).where(
                    ReimbursementDraftFile.draft_id == draft.id,
                    ReimbursementDraftFile.file_status != ReimbursementDraftFileStatus.PURGED.value,
                )
            ).all()
            for record in files:
                if record.file_status == ReimbursementDraftFileStatus.ACTIVE.value:
                    record.file_status = ReimbursementDraftFileStatus.DELETING.value
                cleanup.append(
                    _DraftFileCleanup(
                        file_status=record.file_status,
                        storage_key=record.storage_key,
                        part_storage_key=record.part_storage_key,
                        reserved_bytes=record.reserved_bytes,
                        size_bytes=record.size_bytes,
                        sha256=record.sha256,
                    )
                )

        try:
            for item in cleanup:
                _delete_draft_file_cleanup(self._staging, item)
        except ReimbursementStagingError as exc:
            raise ApiError(
                "REIMBURSEMENT_STORAGE_ERROR",
                "票据文件删除失败，请重试",
                500,
            ) from exc

        with self._write_session() as database:
            draft = database.scalar(
                select(ReimbursementDraft).where(
                    ReimbursementDraft.id == normalized_draft_id,
                    ReimbursementDraft.corp_id == actor.corp_id,
                    ReimbursementDraft.owner_user_id == actor.user_id,
                    ReimbursementDraft.department_id == actor.department_id,
                    ReimbursementDraft.department_name == actor.department_name,
                    ReimbursementDraft.status == ReimbursementDraftStatus.EXPIRED.value,
                    ReimbursementDraft.revision == operation_revision,
                )
            )
            if draft is None:
                # A concurrent retry may already have completed after this call
                # authenticated the same durable delete intent.
                return normalized_draft_id
            if (
                database.scalar(
                    select(ReimbursementSubmission.id)
                    .where(ReimbursementSubmission.draft_id == draft.id)
                    .limit(1)
                )
                is not None
            ):
                raise _draft_in_use_error()
            database.delete(draft)
        return normalized_draft_id

    def reclaim_expired(self, *, now: datetime | None = None) -> int:
        """Delete expired drafts and release abandoned local write attempts.

        Draft deletion reuses the durable two-phase delete intent and is isolated
        per draft so one local cleanup failure can be retried without blocking
        others. Locked/submitted drafts, active submission leases, and uploads
        that reached a remote mutation state are deliberately excluded.
        """

        schema = inspect(self._engine)
        if not all(
            schema.has_table(table_name)
            for table_name in (
                ReimbursementDraft.__tablename__,
                ReimbursementDraftFile.__tablename__,
                ReimbursementSubmission.__tablename__,
                ReimbursementUpload.__tablename__,
            )
        ):
            return 0

        cutoff = _naive_utc(now) if now is not None else utc_now()
        reclaimed = 0
        with Session(self._engine) as database:
            expired_drafts = [
                _ExpiredDraftCleanup(
                    draft_id=draft.id,
                    actor=DraftActor(
                        corp_id=draft.corp_id,
                        user_id=draft.owner_user_id,
                        department_id=draft.department_id,
                        department_name=draft.department_name,
                    ),
                    revision=draft.revision,
                )
                for draft in database.scalars(
                    select(ReimbursementDraft).where(
                        ReimbursementDraft.status.in_(
                            {
                                *_ACTIVE_DRAFT_STATUSES,
                                ReimbursementDraftStatus.EXPIRED.value,
                            }
                        ),
                        ReimbursementDraft.locked_at.is_(None),
                        (
                            (ReimbursementDraft.expires_at <= cutoff)
                            | (ReimbursementDraft.status == ReimbursementDraftStatus.EXPIRED.value)
                        ),
                        ~select(ReimbursementSubmission.id)
                        .where(ReimbursementSubmission.draft_id == ReimbursementDraft.id)
                        .exists(),
                    )
                ).all()
            ]
        for draft in expired_drafts:
            try:
                self.delete_owned_draft(
                    actor=draft.actor,
                    draft_id=draft.draft_id,
                    expected_revision=draft.revision,
                )
            except (ApiError, ReimbursementQuotaError) as exc:
                _LOGGER.error(
                    "Failed to reclaim expired reimbursement draft",
                    extra={"exception_type": type(exc).__name__},
                )
            else:
                reclaimed += 1

        with self._write_session() as database:
            draft_records = database.scalars(
                select(ReimbursementDraftFile)
                .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
                .where(
                    ReimbursementDraftFile.file_status.in_(
                        {
                            ReimbursementDraftFileStatus.RESERVED.value,
                            ReimbursementDraftFileStatus.WRITING.value,
                        }
                    ),
                    ReimbursementDraftFile.reservation_expires_at.is_not(None),
                    ReimbursementDraftFile.reservation_expires_at <= cutoff,
                    ReimbursementDraft.status.in_(_ACTIVE_DRAFT_STATUSES),
                    ReimbursementDraft.locked_at.is_(None),
                    ReimbursementDraft.expires_at > cutoff,
                    ~select(ReimbursementSubmission.id)
                    .where(ReimbursementSubmission.draft_id == ReimbursementDraftFile.draft_id)
                    .exists(),
                    ~select(ReimbursementUpload.id)
                    .where(ReimbursementUpload.source_draft_file_id == ReimbursementDraftFile.id)
                    .exists(),
                )
            ).all()
            for record in draft_records:
                reservation = _draft_staging_reservation(record)
                try:
                    self._staging.discard_reservation(reservation)
                except (OSError, ReimbursementStagingError) as exc:
                    _LOGGER.error(
                        "Failed to reclaim expired draft file reservation",
                        extra={"exception_type": type(exc).__name__},
                    )
                    continue
                record.file_status = ReimbursementDraftFileStatus.PURGED.value
                record.part_storage_key = None
                record.reservation_expires_at = None
                record.purged_at = cutoff
                reclaimed += 1

            upload_records = database.scalars(
                select(ReimbursementUpload)
                .join(
                    ReimbursementSubmission,
                    ReimbursementSubmission.id == ReimbursementUpload.submission_id,
                )
                .where(
                    ReimbursementUpload.role.in_(_GENERATED_ROLES),
                    ReimbursementUpload.local_status.in_(
                        {
                            ReimbursementUploadLocalStatus.RESERVED.value,
                            ReimbursementUploadLocalStatus.WRITING.value,
                        }
                    ),
                    ReimbursementUpload.upload_status == ReimbursementUploadStatus.PENDING.value,
                    ReimbursementUpload.reservation_expires_at.is_not(None),
                    ReimbursementUpload.reservation_expires_at <= cutoff,
                    ReimbursementSubmission.process_instance_id.is_(None),
                    (
                        ReimbursementSubmission.lease_expires_at.is_(None)
                        | (ReimbursementSubmission.lease_expires_at <= cutoff)
                    ),
                )
            ).all()
            for record in upload_records:
                reservation = _upload_staging_reservation(record)
                try:
                    self._staging.discard_reservation(reservation)
                except (OSError, ReimbursementStagingError) as exc:
                    _LOGGER.error(
                        "Failed to reclaim expired generated upload reservation",
                        extra={"exception_type": type(exc).__name__},
                    )
                    continue
                record.upload_status = ReimbursementUploadStatus.DISCARDED.value
                record.local_status = ReimbursementUploadLocalStatus.DELETED.value
                record.local_part_storage_key = None
                record.reservation_expires_at = None
                record.local_deleted_at = cutoff
                record.status_version += 1
                reclaimed += 1

        deleting_files = self._deleting_draft_file_cleanups(cutoff=cutoff)
        for item in deleting_files:
            # Verification reads the complete object. Keep it outside the SQLite
            # write transaction so a large stale file cannot block every writer.
            try:
                self._staging.delete(
                    item.storage_key,
                    expected_size=item.size_bytes,
                    expected_sha256=item.sha256,
                    missing_ok=True,
                )
            except (OSError, ReimbursementStagingError) as exc:
                _LOGGER.error(
                    "Failed to reclaim deleting draft file",
                    extra={"exception_type": type(exc).__name__},
                )
                continue
            if self._finalize_deleting_draft_file(item, cutoff=cutoff):
                reclaimed += 1
        return reclaimed

    def _deleting_draft_file_cleanups(
        self,
        *,
        cutoff: datetime,
    ) -> list[_DeletingDraftFileCleanup]:
        """Snapshot durable delete intents without taking the SQLite write lock."""

        with Session(self._engine) as database:
            rows = database.execute(
                select(ReimbursementDraftFile, ReimbursementDraft)
                .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
                .where(
                    ReimbursementDraftFile.file_status
                    == ReimbursementDraftFileStatus.DELETING.value,
                    ReimbursementDraftFile.size_bytes.is_not(None),
                    ReimbursementDraftFile.sha256.is_not(None),
                    ReimbursementDraft.status.in_(_ACTIVE_DRAFT_STATUSES),
                    ReimbursementDraft.locked_at.is_(None),
                    ReimbursementDraft.expires_at > cutoff,
                    ~select(ReimbursementSubmission.id)
                    .where(ReimbursementSubmission.draft_id == ReimbursementDraftFile.draft_id)
                    .exists(),
                    ~select(ReimbursementUpload.id)
                    .where(ReimbursementUpload.source_draft_file_id == ReimbursementDraftFile.id)
                    .exists(),
                )
            ).all()
            return [
                _DeletingDraftFileCleanup(
                    file_id=file.id,
                    draft_id=file.draft_id,
                    actor=DraftActor(
                        corp_id=draft.corp_id,
                        user_id=draft.owner_user_id,
                        department_id=draft.department_id,
                        department_name=draft.department_name,
                    ),
                    draft_revision=draft.revision,
                    draft_status=draft.status,
                    draft_expires_at=draft.expires_at,
                    storage_key=file.storage_key,
                    reserved_bytes=file.reserved_bytes,
                    size_bytes=file.size_bytes,
                    sha256=file.sha256,
                )
                for file, draft in rows
                if file.size_bytes is not None and file.sha256 is not None
            ]

    def _finalize_deleting_draft_file(
        self,
        item: _DeletingDraftFileCleanup,
        *,
        cutoff: datetime,
    ) -> bool:
        """CAS one physically removed delete intent to PURGED in a short transaction."""

        with self._write_session() as database:
            record = database.scalar(
                select(ReimbursementDraftFile)
                .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
                .where(
                    ReimbursementDraftFile.id == item.file_id,
                    ReimbursementDraftFile.draft_id == item.draft_id,
                    ReimbursementDraftFile.file_status
                    == ReimbursementDraftFileStatus.DELETING.value,
                    ReimbursementDraftFile.storage_key == item.storage_key,
                    ReimbursementDraftFile.reserved_bytes == item.reserved_bytes,
                    ReimbursementDraftFile.size_bytes == item.size_bytes,
                    ReimbursementDraftFile.sha256 == item.sha256,
                    ReimbursementDraft.corp_id == item.actor.corp_id,
                    ReimbursementDraft.owner_user_id == item.actor.user_id,
                    ReimbursementDraft.department_id == item.actor.department_id,
                    ReimbursementDraft.department_name == item.actor.department_name,
                    ReimbursementDraft.revision == item.draft_revision,
                    ReimbursementDraft.status == item.draft_status,
                    ReimbursementDraft.locked_at.is_(None),
                    ReimbursementDraft.expires_at == item.draft_expires_at,
                    ReimbursementDraft.expires_at > cutoff,
                    ~select(ReimbursementSubmission.id)
                    .where(ReimbursementSubmission.draft_id == ReimbursementDraftFile.draft_id)
                    .exists(),
                    ~select(ReimbursementUpload.id)
                    .where(ReimbursementUpload.source_draft_file_id == ReimbursementDraftFile.id)
                    .exists(),
                )
            )
            if record is None:
                return False
            record.file_status = ReimbursementDraftFileStatus.PURGED.value
            record.part_storage_key = None
            record.reservation_expires_at = None
            record.purged_at = cutoff
            return True

    def _admit(self, database: Session, requested_bytes: int) -> None:
        reserved_bytes = int(database.execute(_usage_statement()).scalar_one())
        if requested_bytes > self._max_bytes - reserved_bytes:
            raise ReimbursementQuotaExceeded(
                maximum_bytes=self._max_bytes,
                reserved_bytes=reserved_bytes,
                requested_bytes=requested_bytes,
            )

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        with self._engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            database = Session(bind=connection, autoflush=False, expire_on_commit=False)
            try:
                yield database
                database.flush()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                database.close()

    def _owned_record(
        self,
        database: Session,
        authority: ReservationAuthority,
        reservation: QuotaReservation,
        *,
        now: datetime,
    ) -> ReimbursementDraftFile | ReimbursementUpload:
        if not isinstance(reservation, QuotaReservation):
            raise ValueError("quota reservation is required")
        expected = reservation.staging
        if reservation.kind is ReservationKind.DRAFT_FILE and isinstance(authority, DraftFileOwner):
            record = database.scalar(
                select(ReimbursementDraftFile)
                .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
                .where(
                    ReimbursementDraftFile.id == reservation.record_id,
                    ReimbursementDraftFile.draft_id == authority.draft_id,
                    ReimbursementDraftFile.storage_key == expected.storage_key,
                    ReimbursementDraftFile.part_storage_key == expected.part_storage_key,
                    ReimbursementDraftFile.reserved_bytes == expected.reserved_bytes,
                    ReimbursementDraft.corp_id == authority.corp_id,
                    ReimbursementDraft.owner_user_id == authority.user_id,
                    ReimbursementDraft.revision == _draft_operation_revision(authority),
                    ReimbursementDraft.status.in_(_ACTIVE_DRAFT_STATUSES),
                    ReimbursementDraft.locked_at.is_(None),
                    ReimbursementDraft.expires_at > now,
                )
            )
        elif reservation.kind is ReservationKind.GENERATED_UPLOAD and isinstance(
            authority, SubmissionLease
        ):
            record = database.scalar(
                select(ReimbursementUpload)
                .join(
                    ReimbursementSubmission,
                    ReimbursementSubmission.id == ReimbursementUpload.submission_id,
                )
                .where(
                    ReimbursementUpload.id == reservation.record_id,
                    ReimbursementUpload.role.in_(_GENERATED_ROLES),
                    ReimbursementUpload.local_storage_key == expected.storage_key,
                    ReimbursementUpload.local_part_storage_key == expected.part_storage_key,
                    ReimbursementUpload.reserved_bytes == expected.reserved_bytes,
                    *_submission_lease_conditions(authority, now=now),
                    ReimbursementSubmission.status
                    == ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
                )
            )
        else:
            record = None
        if record is None:
            raise ReimbursementReservationConflict(
                "reservation ownership, lease, version, or state changed"
            )
        return record

    def _owned_release_record(
        self,
        database: Session,
        authority: ReservationAuthority,
        reservation: QuotaReservation,
        *,
        now: datetime,
    ) -> ReimbursementDraftFile | ReimbursementUpload:
        if not isinstance(reservation, QuotaReservation):
            raise ValueError("quota reservation is required")
        expected = reservation.staging
        if reservation.kind is ReservationKind.DRAFT_FILE and isinstance(authority, DraftFileOwner):
            record = database.scalar(
                select(ReimbursementDraftFile)
                .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
                .where(
                    ReimbursementDraftFile.id == reservation.record_id,
                    ReimbursementDraftFile.draft_id == authority.draft_id,
                    ReimbursementDraftFile.storage_key == expected.storage_key,
                    ReimbursementDraftFile.reserved_bytes == expected.reserved_bytes,
                    ReimbursementDraft.corp_id == authority.corp_id,
                    ReimbursementDraft.owner_user_id == authority.user_id,
                    ReimbursementDraft.revision == _draft_operation_revision(authority),
                    ReimbursementDraft.status.in_(_ACTIVE_DRAFT_STATUSES),
                )
            )
        elif reservation.kind is ReservationKind.GENERATED_UPLOAD and isinstance(
            authority, SubmissionLease
        ):
            record = database.scalar(
                select(ReimbursementUpload)
                .join(
                    ReimbursementSubmission,
                    ReimbursementSubmission.id == ReimbursementUpload.submission_id,
                )
                .where(
                    ReimbursementUpload.id == reservation.record_id,
                    ReimbursementUpload.role.in_(_GENERATED_ROLES),
                    ReimbursementUpload.local_storage_key == expected.storage_key,
                    ReimbursementUpload.reserved_bytes == expected.reserved_bytes,
                    *_submission_lease_conditions(authority, now=now),
                )
            )
        else:
            record = None
        if record is None:
            raise ReimbursementReservationConflict(
                "reservation ownership, lease, version, or state changed"
            )
        if _active_part_key(record) is not None and (
            _active_part_key(record) != expected.part_storage_key
        ):
            raise ReimbursementReservationConflict("reservation attempt changed")
        return record


def _delete_draft_file_cleanup(
    staging: ReimbursementStaging,
    item: _DraftFileCleanup,
) -> None:
    if item.file_status in {
        ReimbursementDraftFileStatus.RESERVED.value,
        ReimbursementDraftFileStatus.WRITING.value,
    }:
        if item.part_storage_key is None:
            raise ReimbursementReservationConflict("draft reservation attempt is incomplete")
        staging.discard_reservation(
            StagingReservation(
                storage_key=item.storage_key,
                part_storage_key=item.part_storage_key,
                reserved_bytes=item.reserved_bytes,
            )
        )
        return
    if item.file_status == ReimbursementDraftFileStatus.DELETING.value:
        if item.size_bytes is None or item.sha256 is None:
            raise ReimbursementReservationConflict("draft file metadata is incomplete")
        staging.delete(
            item.storage_key,
            expected_size=item.size_bytes,
            expected_sha256=item.sha256,
            missing_ok=True,
        )
        return
    if item.file_status == ReimbursementDraftFileStatus.FAILED.value:
        # A failed row has no trustworthy digest and therefore owns no safely
        # deletable final object.
        return
    raise ReimbursementReservationConflict("draft file is not deletable")


def _require_draft_department(draft: ReimbursementDraft, actor: DraftActor) -> None:
    if draft.department_id != actor.department_id or draft.department_name != actor.department_name:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_DEPARTMENT_MISMATCH",
            "草稿所属部门与当前选择不同，请切换部门后重试",
            409,
        )


def _draft_not_found_error() -> ApiError:
    return ApiError("REIMBURSEMENT_DRAFT_NOT_FOUND", "草稿不存在", 404)


def _draft_revision_conflict_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
        "草稿已在其他页面更新，请刷新后重试",
        409,
    )


def _draft_locked_error() -> ApiError:
    return ApiError("REIMBURSEMENT_DRAFT_LOCKED", "草稿已锁定，不能删除", 409)


def _draft_in_use_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_IN_USE",
        "草稿已进入提交流程，不能删除",
        409,
    )


def _usage_statement():
    return text(
        """
        SELECT COALESCE(SUM(reserved_bytes), 0)
        FROM (
            SELECT storage_key, MAX(reserved_bytes) AS reserved_bytes
            FROM (
                SELECT storage_key, reserved_bytes
                FROM reimbursement_draft_files
                WHERE file_status != 'PURGED'
                UNION ALL
                SELECT local_storage_key AS storage_key, reserved_bytes
                FROM reimbursement_uploads
                WHERE local_status != 'DELETED'
            ) AS live_reservations
            GROUP BY storage_key
        ) AS physical_objects
        """
    )


def _draft_owner_query(owner: DraftFileOwner) -> Select[tuple[ReimbursementDraft]]:
    if not isinstance(owner, DraftFileOwner):
        raise ValueError("draft file owner is required")
    if owner.expected_revision < 1:
        raise ValueError("expected_revision must be positive")
    return select(ReimbursementDraft).where(
        ReimbursementDraft.id == owner.draft_id,
        ReimbursementDraft.corp_id == owner.corp_id,
        ReimbursementDraft.owner_user_id == owner.user_id,
        ReimbursementDraft.revision == owner.expected_revision,
    )


def _draft_operation_revision(owner: DraftFileOwner) -> int:
    return owner.expected_revision + 1


def _submission_lease_query(
    lease: SubmissionLease,
    *,
    now: datetime,
) -> Select[tuple[ReimbursementSubmission]]:
    return select(ReimbursementSubmission).where(
        *_submission_lease_conditions(lease, now=now),
        ReimbursementSubmission.status == ReimbursementSubmissionStatus.GENERATING_EXCEL.value,
    )


def _submission_lease_conditions(
    lease: SubmissionLease,
    *,
    now: datetime,
) -> tuple[ColumnElement[bool], ...]:
    if not isinstance(lease, SubmissionLease):
        raise ValueError("submission lease is required")
    if lease.expected_status_version < 1:
        raise ValueError("expected_status_version must be positive")
    token = _required_text(lease.lease_token, maximum=64)
    return (
        ReimbursementSubmission.id == lease.submission_id,
        ReimbursementSubmission.corp_id == lease.corp_id,
        ReimbursementSubmission.originator_user_id == lease.user_id,
        ReimbursementSubmission.status_version == lease.expected_status_version,
        ReimbursementSubmission.lease_token == token,
        ReimbursementSubmission.lease_expires_at.is_not(None),
        ReimbursementSubmission.lease_expires_at > now,
    )


def _require_positive_size(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("reserved_bytes must be a positive integer")


def _require_sort_order(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("sort_order must be a non-negative integer")


def _required_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("text value is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(c in normalized for c in "\r\n\x00"):
        raise ValueError("text value is invalid")
    return normalized


def _naive_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("timestamp is invalid")
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _local_status(record: ReimbursementDraftFile | ReimbursementUpload) -> str:
    if isinstance(record, ReimbursementDraftFile):
        return record.file_status
    return record.local_status


def _set_local_status(record: ReimbursementDraftFile | ReimbursementUpload, value: str) -> None:
    if isinstance(record, ReimbursementDraftFile):
        record.file_status = value
    else:
        record.local_status = value


def _active_part_key(record: ReimbursementDraftFile | ReimbursementUpload) -> str | None:
    if isinstance(record, ReimbursementDraftFile):
        return record.part_storage_key
    return record.local_part_storage_key


def _draft_staging_reservation(record: ReimbursementDraftFile) -> StagingReservation:
    if record.part_storage_key is None:
        raise ReimbursementReservationConflict("draft reservation attempt is incomplete")
    return StagingReservation(
        storage_key=record.storage_key,
        part_storage_key=record.part_storage_key,
        reserved_bytes=record.reserved_bytes,
    )


def _upload_staging_reservation(record: ReimbursementUpload) -> StagingReservation:
    if record.local_part_storage_key is None:
        raise ReimbursementReservationConflict("upload reservation attempt is incomplete")
    return StagingReservation(
        storage_key=record.local_storage_key,
        part_storage_key=record.local_part_storage_key,
        reserved_bytes=record.reserved_bytes,
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_staged_object(
    reservation: QuotaReservation,
    staged: StagedObject,
) -> None:
    if not isinstance(staged, StagedObject):
        raise ValueError("staged object is required")
    if staged.storage_key != reservation.staging.storage_key:
        raise ReimbursementReservationConflict("staged object does not match reservation")
    if (
        isinstance(staged.size_bytes, bool)
        or not isinstance(staged.size_bytes, int)
        or staged.size_bytes < 1
        or staged.size_bytes > reservation.staging.reserved_bytes
    ):
        raise ReimbursementReservationConflict("staged object exceeds reservation")
    if not _valid_sha256(staged.sha256):
        raise ReimbursementReservationConflict("staged object digest is invalid")
