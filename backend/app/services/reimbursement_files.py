from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile

from app.core.config import Settings
from app.core.errors import ApiError
from app.domain.expenses import ExpenseTotals, calculate_expense_totals
from app.domain.material_classification import (
    MATERIAL_CLASSIFICATION_KEY,
    PENDING_CLASSIFICATION_STATUSES,
    material_classification,
)
from app.domain.subsidy import SubsidyCalculation
from app.models.reimbursement import (
    ReimbursementAttachmentKind,
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftFileRole,
    ReimbursementDraftFileStatus,
    ReimbursementDraftStatus,
    ReimbursementOcrStatus,
    ReimbursementSubmission,
    ReimbursementUpload,
    utc_now,
)
from app.models.session import UserSession
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services.excel_generator import (
    ResolvedProject,
    WorkbookResult,
    generate_expense_workbook,
)
from app.services.multipart_uploads import prepare_spool_directory
from app.services.oa_reimbursement_payload import (
    draft_input_from_snapshot,
    parse_locked_submission_snapshot,
    snapshot_excel_input,
    verify_excel_template,
)
from app.services.oa_template_profiles import require_submission_ready_catalog
from app.services.ocr_service import (
    OcrService,
    failed_expense_payload,
    failed_itinerary_payload,
    parsed_expense_payload,
)
from app.services.process_jobs import KillableProcessRunner
from app.services.receipt_keywords import load_receipt_keyword_rules
from app.services.reimbursement_drafts import (
    DraftActor,
    apply_ocr_evidence,
    bump_owned_draft_revision,
    complete_expense_items,
    detach_draft_file_from_input,
    detach_draft_files_from_input,
    require_complete_draft_input,
    require_owned_draft,
    validate_and_calculate_input,
)
from app.services.reimbursement_quota import (
    DraftFileOwner,
    QuotaReservation,
    ReimbursementQuotaCoordinator,
    ReimbursementQuotaExceeded,
    ReimbursementReservationConflict,
)
from app.services.reimbursement_staging import (
    ReimbursementStaging,
    StagedObject,
    StagingIntegrityError,
    StagingLayoutError,
    StagingLimitExceeded,
    StagingObjectExists,
    StagingObjectNotFound,
)
from app.services.subsidy_calculation import calculate_trip_subsidies, request_trips
from app.services.temp_files import (
    StoredFile,
    validate_new_file,
    validate_upload_name,
    validate_upload_type,
)

logger = logging.getLogger(__name__)

_OCR_RUNNING_MARKER_KEY = "operationId"
_OCR_STALE_GRACE_SECONDS = 30
_PIPELINE_INPUT_HASH_KEY = "_pipelineInputSha256"


@dataclass(frozen=True, slots=True)
class DraftFileSnapshot:
    id: str
    draft_id: str
    original_name: str
    extension: str
    media_type: str
    processing_role: str
    sort_order: int
    file_status: str
    storage_key: str
    size_bytes: int
    sha256: str
    ocr_status: str
    ocr_result_json: str | None
    attachment_kind: str = "other"


@dataclass(frozen=True, slots=True)
class DraftFileMutationResult:
    revision: int
    file: DraftFileSnapshot


@dataclass(frozen=True, slots=True)
class DraftFileDeletion:
    file_id: str
    draft_id: str
    storage_key: str
    size_bytes: int
    sha256: str
    revision: int
    completed: bool = False


@dataclass(frozen=True, slots=True)
class WorkbookPreviewSnapshot:
    canonical_input_json: str
    input: ReimbursementDraftInput
    employee_name: str
    department_name: str
    project: ResolvedProject
    subsidy: SubsidyCalculation | None
    subsidies: tuple[SubsidyCalculation, ...]
    totals: ExpenseTotals


def serialize_draft_file(file: DraftFileSnapshot) -> dict[str, object]:
    ocr_result: object | None = None
    payment_details: object | None = None
    hotel_bill_details: object | None = None
    if (
        file.ocr_status
        in {
            ReimbursementOcrStatus.COMPLETE.value,
            ReimbursementOcrStatus.FAILED.value,
        }
        and file.ocr_result_json is not None
    ):
        try:
            ocr_result = json.loads(file.ocr_result_json)
            if isinstance(ocr_result, dict):
                ocr_result.pop(MATERIAL_CLASSIFICATION_KEY, None)
                ocr_result.pop(_PIPELINE_INPUT_HASH_KEY, None)
                details = ocr_result.pop("paymentDetails", None)
                hotel_details = ocr_result.pop("hotelBillDetails", None)
                if (
                    file.processing_role == ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
                    and file.attachment_kind == ReimbursementAttachmentKind.HOTEL_BILL.value
                    and isinstance(hotel_details, dict)
                ):
                    hotel_bill_details = hotel_details
                if (
                    file.processing_role == ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
                    and file.attachment_kind == ReimbursementAttachmentKind.PAYMENT_PROOF.value
                    and isinstance(details, dict)
                ):
                    payment_details = details
                if not ocr_result:
                    ocr_result = None
        except (TypeError, ValueError):
            logger.error(
                "Stored reimbursement OCR result is invalid",
                extra={"draft_file_id": file.id},
            )
    return {
        "id": file.id,
        "name": file.original_name,
        "role": file.processing_role,
        "attachmentKind": file.attachment_kind,
        "hotelBillDetails": hotel_bill_details,
        "sortOrder": file.sort_order,
        "status": file.file_status,
        "mediaType": file.media_type,
        "sizeBytes": file.size_bytes,
        "ocrStatus": file.ocr_status,
        "ocrResult": ocr_result,
        "paymentDetails": payment_details,
        "materialClassification": material_classification(file.ocr_result_json),
    }


def list_draft_files(
    database: Session,
    *,
    draft_id: str,
    actor: DraftActor,
) -> tuple[int, list[DraftFileSnapshot]]:
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor)
    visible_file_status = ReimbursementDraftFile.file_status.in_(
        {
            ReimbursementDraftFileStatus.ACTIVE.value,
            ReimbursementDraftFileStatus.DELETING.value,
        }
    )
    if draft.status == ReimbursementDraftStatus.LOCKED.value:
        locked_input = ReimbursementDraftInput.model_validate_json(draft.input_json)
        referenced_file_ids = {
            file_id
            for item in locked_input.items
            for file_id in (
                item.source_file_id,
                *item.itinerary_file_ids,
                *item.payment_proof_file_ids,
                *item.hotel_bill_file_ids,
            )
            if file_id is not None
        }
        if referenced_file_ids:
            visible_file_status = or_(
                visible_file_status,
                (ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.PURGED.value)
                & ReimbursementDraftFile.id.in_(referenced_file_ids),
            )
    files = database.scalars(
        select(ReimbursementDraftFile)
        .where(
            ReimbursementDraftFile.draft_id == draft.id,
            visible_file_status,
        )
        .order_by(ReimbursementDraftFile.sort_order, ReimbursementDraftFile.id)
    ).all()
    return draft.revision, [_snapshot(item) for item in files]


def read_draft_file_content(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    staging: ReimbursementStaging,
) -> tuple[DraftFileSnapshot, bytes]:
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor)
    if draft.expires_at <= utc_now() or draft.status == "EXPIRED":
        raise ApiError("REIMBURSEMENT_DRAFT_EXPIRED", "报销资料已过期，请重新上传", 409)
    file = _snapshot(_require_active_file(database, draft_id=draft.id, file_id=file_id))
    if file.media_type not in {"application/pdf", "image/png", "image/jpeg"}:
        raise ApiError("UNSUPPORTED_FILE_TYPE", "此文件类型不能预览", 415)
    try:
        content = staging.read_bytes(
            file.storage_key, expected_size=file.size_bytes, expected_sha256=file.sha256
        )
    except (StagingIntegrityError, StagingObjectNotFound, StagingLayoutError, OSError):
        raise ApiError(
            "REIMBURSEMENT_DRAFT_FILE_CHANGED", "附件已丢失或内容发生变化，请重新上传", 409
        ) from None
    return file, content


async def persist_draft_upload(
    *,
    upload: UploadFile,
    actor: DraftActor,
    draft_id: str,
    expected_revision: int,
    processing_role: ReimbursementDraftFileRole,
    settings: Settings,
    session_factory: sessionmaker[Session],
    quota: ReimbursementQuotaCoordinator,
    staging: ReimbursementStaging,
    process_runner: KillableProcessRunner,
    attachment_kind: ReimbursementAttachmentKind = ReimbursementAttachmentKind.OTHER,
    auto_classify: bool = False,
) -> DraftFileMutationResult:
    if auto_classify and (
        processing_role is not ReimbursementDraftFileRole.ATTACHMENT_ONLY
        or attachment_kind != ReimbursementAttachmentKind.OTHER
    ):
        raise ApiError("VALIDATION_ERROR", "自动分类材料请使用未指定用途的附件上传", 422)
    if processing_role is ReimbursementDraftFileRole.EXPENSE_SOURCE and attachment_kind != "other":
        raise ApiError("VALIDATION_ERROR", "费用来源文件不能指定证明材料用途", 422)
    size_bytes, first_bytes, worker_path = _inspect_upload_spool(upload, settings=settings)
    upload_type = validate_upload_type(upload.filename, first_bytes)
    page_count = await validate_new_file(
        worker_path,
        upload_type.extension,
        settings,
        process_runner,
        supporting_pdf=processing_role is ReimbursementDraftFileRole.ATTACHMENT_ONLY,
    )

    with session_factory() as database:
        draft = require_owned_draft(
            database,
            draft_id=draft_id,
            actor=actor,
            mutable=True,
        )
        _require_revision(draft, expected_revision)
        retained_file_count = database.scalar(
            select(func.count())
            .select_from(ReimbursementDraftFile)
            .where(
                ReimbursementDraftFile.draft_id == draft.id,
                ReimbursementDraftFile.file_status != ReimbursementDraftFileStatus.PURGED.value,
            )
        )
        if int(retained_file_count or 0) >= settings.session_max_files:
            raise ApiError(
                "REIMBURSEMENT_FILE_LIMIT",
                f"每次报销最多保留 {settings.session_max_files} 个文件",
                413,
            )
        retained_bytes = database.scalar(
            select(func.coalesce(func.sum(ReimbursementDraftFile.reserved_bytes), 0)).where(
                ReimbursementDraftFile.draft_id == draft.id,
                ReimbursementDraftFile.file_status != ReimbursementDraftFileStatus.PURGED.value,
            )
        )
        if int(retained_bytes or 0) + size_bytes > settings.session_max_bytes:
            raise ApiError(
                "REIMBURSEMENT_DRAFT_STORAGE_LIMIT",
                "当前报销的文件总量超过限制",
                413,
            )
        maximum_sort_order = database.scalar(
            select(func.max(ReimbursementDraftFile.sort_order)).where(
                ReimbursementDraftFile.draft_id == draft.id
            )
        )
        sort_order = int(maximum_sort_order) + 1 if maximum_sort_order is not None else 0
        pipeline_input_hash = _pipeline_input_hash(draft.input_json)

    owner = DraftFileOwner(
        corp_id=actor.corp_id,
        user_id=actor.user_id,
        draft_id=draft_id,
        expected_revision=expected_revision,
    )
    reservation: QuotaReservation | None = None
    finalized = False
    try:
        reservation = quota.reserve_draft_file(
            owner,
            sort_order=sort_order,
            processing_role=processing_role,
            attachment_kind=attachment_kind.value,
            ocr_result_json=json.dumps(
                {
                    _PIPELINE_INPUT_HASH_KEY: pipeline_input_hash,
                    MATERIAL_CLASSIFICATION_KEY: {
                        "status": "pending",
                        "kind": "unknown",
                        "reason": None,
                        "pageCount": page_count,
                    },
                }
            )
            if auto_classify
            else None,
            original_name=upload_type.original_name,
            extension=upload_type.extension,
            media_type=upload_type.media_type,
            reserved_bytes=size_bytes,
            expires_at=utc_now() + timedelta(minutes=settings.upload_ttl_minutes),
        )
        quota.mark_writing(owner, reservation)
        upload.file.seek(0)
        staged = await _write_staging_cancellation_safe(
            staging,
            reservation,
            upload.file,
            expected_size=size_bytes,
        )
        new_revision = quota.finalize_draft_file(
            owner,
            reservation,
            staged,
            actor=actor,
        )
        finalized = True
    except BaseException:
        if reservation is not None and not finalized:
            _abandon_upload_without_masking_error(quota, owner, reservation)
        raise

    with session_factory() as database:
        file = _require_active_file(database, draft_id=draft_id, file_id=reservation.record_id)
        return DraftFileMutationResult(revision=new_revision, file=_snapshot(file))


async def validate_expense_source_conversion(
    *,
    database: Session,
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    expected_revision: int,
    settings: Settings,
    staging: ReimbursementStaging,
    process_runner: KillableProcessRunner,
) -> None:
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor, mutable=True)
    _require_revision(draft, expected_revision)
    file = _require_active_file(database, draft_id=draft.id, file_id=file_id)
    if file.ocr_status == ReimbursementOcrStatus.RUNNING.value:
        raise ApiError("REIMBURSEMENT_FILE_BUSY", "材料正在识别，请稍后再修改", 409)
    source = _snapshot(file)
    database.rollback()
    path = await _materialize_cancellation_safe(source, settings, staging)
    try:
        await validate_new_file(path, source.extension, settings, process_runner)
    finally:
        path.unlink(missing_ok=True)


def update_draft_file(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    expected_revision: int,
    processing_role: ReimbursementDraftFileRole | None,
    original_name: str | None,
    attachment_kind: ReimbursementAttachmentKind | None = None,
) -> DraftFileMutationResult:
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
        mutable=True,
    )
    _require_revision(draft, expected_revision)
    file = _require_active_file(database, draft_id=draft.id, file_id=file_id)
    if file.ocr_status == ReimbursementOcrStatus.RUNNING.value:
        raise ApiError(
            "REIMBURSEMENT_FILE_BUSY",
            "票据正在识别，请稍后再修改",
            409,
        )
    if processing_role is None and original_name is None and attachment_kind is None:
        raise ApiError("VALIDATION_ERROR", "请指定要修改的文件信息", 422)
    if original_name is not None:
        file.original_name = validate_upload_name(
            original_name,
            expected_extension=file.extension,
        )
    detached_input_json: str | None = None
    next_role = processing_role.value if processing_role is not None else file.processing_role
    next_kind = attachment_kind.value if attachment_kind is not None else file.attachment_kind
    classification = material_classification(file.ocr_result_json)
    confirms_purpose = processing_role is not None or attachment_kind is not None
    if next_role == ReimbursementDraftFileRole.EXPENSE_SOURCE.value:
        if attachment_kind is not None and attachment_kind != "other":
            raise ApiError("VALIDATION_ERROR", "费用来源文件不能指定证明材料用途", 422)
        next_kind = "other"
        if classification and (classification.get("pageCount") or 1) != 1:
            raise ApiError("MULTI_PAGE_PDF_UNSUPPORTED", "请将多页材料拆分为单张发票后上传", 400)
    if next_role != file.processing_role or next_kind != file.attachment_kind:
        detached_input_json = detach_draft_file_from_input(
            database,
            draft=draft,
            file_id=file.id,
        ).canonical_json
        file.processing_role = next_role
        file.attachment_kind = next_kind
        file.ocr_status = ReimbursementOcrStatus.NOT_REQUESTED.value
        file.ocr_result_json = None
    if confirms_purpose:
        payload = json.loads(file.ocr_result_json) if file.ocr_result_json else {}
        payload[MATERIAL_CLASSIFICATION_KEY] = {
            "status": "confirmed",
            "kind": "expense" if next_role == "EXPENSE_SOURCE" else next_kind,
            "reason": None,
            "pageCount": classification.get("pageCount") if classification else None,
        }
        file.ocr_result_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    new_revision = bump_owned_draft_revision(
        database,
        draft_id=draft.id,
        actor=actor,
        expected_revision=expected_revision,
    )
    if detached_input_json is not None:
        database.execute(
            update(ReimbursementDraft)
            .where(
                ReimbursementDraft.id == draft.id,
                ReimbursementDraft.revision == new_revision,
            )
            .values(input_json=detached_input_json)
            .execution_options(synchronize_session=False)
        )
    database.commit()
    database.refresh(file)
    return DraftFileMutationResult(revision=new_revision, file=_snapshot(file))


def begin_draft_files_clear(
    database: Session, *, actor: DraftActor, draft_id: str, expected_revision: int
) -> tuple[int, list[DraftFileDeletion]]:
    """Persist one atomic delete intent for the current revision's materials."""
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor, mutable=True)
    _require_revision(draft, expected_revision)
    files = list(
        database.scalars(
            select(ReimbursementDraftFile).where(
                ReimbursementDraftFile.draft_id == draft_id,
                ReimbursementDraftFile.file_status != ReimbursementDraftFileStatus.PURGED.value,
            )
        )
    )
    if any(
        file.file_status in {"RESERVED", "WRITING"} or file.ocr_status == "RUNNING"
        for file in files
    ):
        raise ApiError("REIMBURSEMENT_FILE_BUSY", "文件仍在上传或识别，请完成后清空", 409)
    targets = [file for file in files if file.file_status in {"ACTIVE", "DELETING"}]
    ids = {file.id for file in targets}
    if not ids:
        return draft.revision, []
    if (
        database.scalar(
            select(ReimbursementUpload.id)
            .where(ReimbursementUpload.source_draft_file_id.in_(ids))
            .limit(1)
        )
        is not None
    ):
        raise ApiError("REIMBURSEMENT_FILE_IN_USE", "文件已被提交流程使用，不能删除", 409)
    if any(file.size_bytes is None or file.sha256 is None for file in targets):
        raise _file_not_found_error()
    revision = bump_owned_draft_revision(
        database,
        draft_id=draft_id,
        actor=actor,
        expected_revision=expected_revision,
    )
    canonical = detach_draft_files_from_input(database, draft=draft, file_ids=ids).canonical_json
    database.execute(
        update(ReimbursementDraft)
        .where(
            ReimbursementDraft.id == draft_id,
            ReimbursementDraft.revision == revision,
        )
        .values(input_json=canonical)
        .execution_options(synchronize_session=False)
    )
    deletions = []
    for file in targets:
        file.file_status = ReimbursementDraftFileStatus.DELETING.value
        deletions.append(
            DraftFileDeletion(
                file_id=file.id,
                draft_id=draft_id,
                storage_key=file.storage_key,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                revision=revision,
                completed=False,
            )
        )
    database.commit()
    return revision, deletions


async def complete_draft_files_clear(
    *,
    deletions: list[DraftFileDeletion],
    actor: DraftActor,
    session_factory: sessionmaker[Session],
    staging: ReimbursementStaging,
) -> None:
    # Bound disk work, but await every started deletion even when one fails.
    # Durable DELETING intents remain resumable by retry and the existing sweeper.
    semaphore = asyncio.Semaphore(2)

    async def remove(deletion: DraftFileDeletion) -> None:
        async with semaphore:
            await complete_draft_file_delete(
                deletion=deletion,
                actor=actor,
                session_factory=session_factory,
                staging=staging,
            )

    async def finish() -> None:
        results = await asyncio.gather(
            *(remove(item) for item in deletions), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    task = asyncio.create_task(finish())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def begin_draft_file_delete(
    database: Session,
    *,
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    expected_revision: int,
) -> DraftFileDeletion:
    draft = require_owned_draft(
        database,
        draft_id=draft_id,
        actor=actor,
    )
    _require_revision(draft, expected_revision)
    file = database.scalar(
        select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.id == file_id,
            ReimbursementDraftFile.draft_id == draft.id,
            ReimbursementDraftFile.file_status.in_(
                {
                    ReimbursementDraftFileStatus.ACTIVE.value,
                    ReimbursementDraftFileStatus.DELETING.value,
                    ReimbursementDraftFileStatus.PURGED.value,
                }
            ),
        )
    )
    if file is None or file.size_bytes is None or file.sha256 is None:
        raise _file_not_found_error()
    if file.file_status == ReimbursementDraftFileStatus.ACTIVE.value:
        require_owned_draft(
            database,
            draft_id=draft_id,
            actor=actor,
            mutable=True,
        )
        if file.ocr_status == ReimbursementOcrStatus.RUNNING.value:
            raise ApiError(
                "REIMBURSEMENT_FILE_BUSY",
                "票据正在识别，请稍后再删除",
                409,
            )
        referenced = database.scalar(
            select(ReimbursementUpload.id)
            .where(ReimbursementUpload.source_draft_file_id == file.id)
            .limit(1)
        )
        if referenced is not None:
            raise ApiError(
                "REIMBURSEMENT_FILE_IN_USE",
                "文件已被提交流程使用，不能删除",
                409,
            )
        new_revision = bump_owned_draft_revision(
            database,
            draft_id=draft.id,
            actor=actor,
            expected_revision=expected_revision,
        )
        detached_input_json = detach_draft_file_from_input(
            database,
            draft=draft,
            file_id=file.id,
        ).canonical_json
        database.execute(
            update(ReimbursementDraft)
            .where(
                ReimbursementDraft.id == draft.id,
                ReimbursementDraft.revision == new_revision,
            )
            .values(input_json=detached_input_json)
            .execution_options(synchronize_session=False)
        )
        file.file_status = ReimbursementDraftFileStatus.DELETING.value
        deletion = DraftFileDeletion(
            file_id=file.id,
            draft_id=file.draft_id,
            storage_key=file.storage_key,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            revision=new_revision,
            completed=False,
        )
        database.commit()
        return deletion
    else:
        # A retry resumes a durable delete intent without requiring the draft to
        # remain mutable or bumping its revision twice. Snapshot before rollback:
        # rollback expires ORM attributes and reading them afterwards would open
        # a new synchronous transaction across the caller's await.
        new_revision = draft.revision
        deletion = DraftFileDeletion(
            file_id=file.id,
            draft_id=file.draft_id,
            storage_key=file.storage_key,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            revision=new_revision,
            completed=file.file_status == ReimbursementDraftFileStatus.PURGED.value,
        )
        database.rollback()
        return deletion


async def complete_draft_file_delete(
    *,
    deletion: DraftFileDeletion,
    actor: DraftActor,
    session_factory: sessionmaker[Session],
    staging: ReimbursementStaging,
) -> int:
    if deletion.completed:
        return deletion.revision
    task = asyncio.create_task(
        asyncio.to_thread(
            _delete_file_and_release_quota,
            deletion,
            actor,
            session_factory,
            staging,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Once DELETING is committed, finish physical deletion and the matching
        # quota release even if the HTTP caller disappears.
        try:
            await task
        except Exception as exc:  # pragma: no cover - defensive recovery logging
            logger.error(
                "Cancelled reimbursement file deletion needs retry",
                extra={
                    "draft_file_id": deletion.file_id,
                    "exception_type": type(exc).__name__,
                },
            )
        raise


async def recognize_draft_file(
    *,
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    expected_revision: int,
    reference_year: int | None,
    settings: Settings,
    session_factory: sessionmaker[Session],
    staging: ReimbursementStaging,
    ocr_service: OcrService,
    allow_upload_overlap: bool = False,
    session_id_hash: str | None = None,
) -> DraftFileMutationResult:
    operation_id = str(uuid4())
    pipeline_input_json: str | None = None
    with session_factory() as database:
        draft = require_owned_draft(
            database,
            draft_id=draft_id,
            actor=actor,
            mutable=True,
        )
        if not allow_upload_overlap:
            _require_revision(draft, expected_revision)
        file = _require_active_file(database, draft_id=draft.id, file_id=file_id)
        classification = material_classification(file.ocr_result_json)
        auto_classify = (
            classification is not None
            and classification.get("status") in PENDING_CLASSIFICATION_STATUSES
        )
        if allow_upload_overlap:
            pipeline_input_json = _admit_pipeline_ocr(
                database,
                draft=draft,
                file=file,
                actor=actor,
                expected_revision=expected_revision,
                session_id_hash=session_id_hash,
            )
        marker_data: dict[str, object] = {_OCR_RUNNING_MARKER_KEY: operation_id}
        if classification:
            marker_data[MATERIAL_CLASSIFICATION_KEY] = classification
        marker = json.dumps(marker_data, separators=(",", ":"), sort_keys=True)
        is_itinerary = (
            file.processing_role == ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
            and file.attachment_kind == ReimbursementAttachmentKind.ITINERARY.value
        )
        is_hotel_bill = (
            file.processing_role == ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
            and file.attachment_kind == ReimbursementAttachmentKind.HOTEL_BILL.value
        )
        if (
            file.processing_role != ReimbursementDraftFileRole.EXPENSE_SOURCE.value
            and not is_itinerary
            and not is_hotel_bill
            and not auto_classify
        ):
            raise ApiError(
                "REIMBURSEMENT_FILE_OCR_NOT_ALLOWED",
                "仅票据来源或行程单材料可进行识别",
                409,
            )
        running_cutoff = utc_now() - timedelta(
            seconds=settings.ocr_timeout_seconds + _OCR_STALE_GRACE_SECONDS
        )
        if (
            file.ocr_status == ReimbursementOcrStatus.RUNNING.value
            and file.updated_at > running_cutoff
        ):
            raise ApiError(
                "REIMBURSEMENT_FILE_OCR_RUNNING",
                "票据正在识别，请稍后查看",
                409,
            )
        # A RUNNING marker older than the worker timeout belongs to a dead
        # process and can be atomically replaced by this attempt.
        source = _snapshot(file)
        if pipeline_input_json is None:
            operation_revision = bump_owned_draft_revision(
                database,
                draft_id=draft.id,
                actor=actor,
                expected_revision=expected_revision,
            )
        else:
            operation_revision = draft.revision
        # A file-scoped operation still needs an atomic marker claim: two
        # requests must never both consume the same NOT_REQUESTED file.
        claimed = database.execute(
            update(ReimbursementDraftFile)
            .where(
                ReimbursementDraftFile.id == file.id,
                ReimbursementDraftFile.draft_id == draft.id,
                ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                ReimbursementDraftFile.ocr_status == file.ocr_status,
                ReimbursementDraftFile.ocr_result_json == file.ocr_result_json,
            )
            .values(ocr_status=ReimbursementOcrStatus.RUNNING.value, ocr_result_json=marker)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ApiError("REIMBURSEMENT_FILE_OCR_RUNNING", "票据正在识别，请稍后查看", 409)
        keyword_rules = load_receipt_keyword_rules(database)
        database.commit()

    worker_path: Path | None = None
    final_status = ReimbursementOcrStatus.COMPLETE
    failure_payload = failed_itinerary_payload if is_itinerary else failed_expense_payload
    if is_hotel_bill:
        # Stay OCR is advisory even if the model is unavailable or times out.
        # Keep failed expense-shaped candidates out of this attachment's data.
        def failure_payload(_file_id: str, _code: str, _message: str) -> dict[str, object]:
            return {
                "hotelBillDetails": {
                    "warnings": ["HOTEL_BILL_INCOMPLETE", "HOTEL_BILL_REVIEW_REQUIRED"]
                }
            }

    try:
        worker_path = await _materialize_cancellation_safe(source, settings, staging)
        stored = StoredFile(
            temp_id=file_id,
            path=worker_path,
            extension=source.extension,
            media_type=source.media_type,
            size=source.size_bytes,
            original_name=source.original_name,
        )
        if auto_classify or is_hotel_bill:
            payload = await ocr_service.recognize_material_file(
                stored,
                reference_year=reference_year,
                keyword_rules=keyword_rules,
            )
            if is_hotel_bill and not auto_classify:
                # Explicit purpose is authoritative; OCR is advisory and must
                # neither replace the purpose nor create an expense result.
                payload = {
                    "hotelBillDetails": payload.get(
                        "hotelBillDetails",
                        {"warnings": ["HOTEL_BILL_INCOMPLETE", "HOTEL_BILL_REVIEW_REQUIRED"]},
                    )
                }
        elif is_itinerary:
            payload = await ocr_service.recognize_itinerary_file(
                stored,
                reference_year=reference_year,
            )
        else:
            parsed = await ocr_service.recognize_file(
                stored,
                reference_year=reference_year,
                keyword_rules=keyword_rules,
            )
            payload = parsed_expense_payload(file_id, parsed)
    except asyncio.CancelledError:
        payload = failure_payload(
            file_id,
            "OCR_CANCELLED",
            "票据识别已中断，请重试",
        )
        await asyncio.shield(
            asyncio.to_thread(
                _settle_cancelled_ocr,
                session_factory=session_factory,
                actor=actor,
                draft_id=draft_id,
                file_id=file_id,
                operation_revision=operation_revision,
                marker=marker,
                payload=payload,
                pipeline_input_json=pipeline_input_json,
                session_id_hash=session_id_hash,
            )
        )
        raise
    except ApiError as exc:
        final_status = ReimbursementOcrStatus.FAILED
        payload = failure_payload(file_id, exc.code, exc.message)
    except Exception as exc:  # Defensive: never log OCR text or a staging path.
        logger.error(
            "Unexpected durable reimbursement OCR error",
            extra={"draft_file_id": file_id, "exception_type": type(exc).__name__},
        )
        final_status = ReimbursementOcrStatus.FAILED
        payload = failure_payload(
            file_id,
            "OCR_FAILED",
            "票据识别失败，请手工填写",
        )
    finally:
        if worker_path is not None:
            worker_path.unlink(missing_ok=True)

    try:
        return _finish_ocr(
            session_factory=session_factory,
            actor=actor,
            draft_id=draft_id,
            file_id=file_id,
            operation_revision=operation_revision,
            marker=marker,
            final_status=final_status,
            payload=payload,
            pipeline_input_json=pipeline_input_json,
            session_id_hash=session_id_hash,
        )
    except ApiError:
        _mark_ocr_interrupted(
            session_factory=session_factory,
            actor=actor,
            draft_id=draft_id,
            file_id=file_id,
            marker=marker,
        )
        raise


async def generate_draft_excel_preview(
    *,
    actor: DraftActor,
    employee_name: str,
    draft_id: str,
    expected_revision: int,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> WorkbookResult:
    with session_factory() as database:
        snapshot = _workbook_preview_snapshot(
            database,
            actor=actor,
            employee_name=employee_name,
            draft_id=draft_id,
            expected_revision=expected_revision,
            settings=settings,
        )

    result = await asyncio.to_thread(
        generate_expense_workbook,
        template_path=settings.excel_template_path,
        employee_name=snapshot.employee_name,
        department_name=snapshot.department_name,
        project=snapshot.project,
        trip=snapshot.input.trip,
        items=complete_expense_items(snapshot.input),
        subsidy=snapshot.subsidy,
        totals=snapshot.totals,
        trips=snapshot.input.trips,
        subsidies=snapshot.subsidies,
    )

    # Do not serve a workbook calculated from a draft that changed during the
    # CPU-bound generation step.
    with session_factory() as database:
        current = require_owned_draft(database, draft_id=draft_id, actor=actor)
        _require_revision(current, expected_revision)
        if current.input_json != snapshot.canonical_input_json:
            raise _revision_conflict_error()
    return result


def _workbook_preview_snapshot(
    database: Session,
    *,
    actor: DraftActor,
    employee_name: str,
    draft_id: str,
    expected_revision: int,
    settings: Settings,
) -> WorkbookPreviewSnapshot:
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor)
    _require_revision(draft, expected_revision)
    if draft.status == ReimbursementDraftStatus.LOCKED.value:
        submission = database.scalar(
            select(ReimbursementSubmission).where(ReimbursementSubmission.draft_id == draft.id)
        )
        if submission is None:
            raise ApiError(
                "REIMBURSEMENT_DRAFT_CORRUPTED",
                "锁定报销缺少提交快照，请联系管理员",
                500,
            )
        submission_snapshot = parse_locked_submission_snapshot(draft, submission)
        verify_excel_template(submission_snapshot, settings.excel_template_path)
        excel_input = snapshot_excel_input(submission_snapshot)
        draft_input = draft_input_from_snapshot(submission_snapshot)
        return WorkbookPreviewSnapshot(
            canonical_input_json=draft.input_json,
            input=draft_input,
            employee_name=excel_input.employee_name,
            department_name=excel_input.department_name,
            project=excel_input.project,
            subsidy=excel_input.subsidy,
            subsidies=excel_input.subsidies,
            totals=excel_input.totals,
        )
    try:
        draft_input = ReimbursementDraftInput.model_validate_json(draft.input_json)
    except ValidationError as exc:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_INVALID",
            "报销内容无法生成 Excel，请重新确认填写内容",
            409,
        ) from exc
    if len(draft_input.items) > settings.expense_max_items:
        raise ApiError(
            "TOO_MANY_EXPENSE_LINES",
            f"当前部署每张报销单最多处理 {settings.expense_max_items} 条费用明细",
            422,
        )
    # A locked draft keeps the exact receipt counts and evidence it was reviewed with.
    if draft.locked_at is None:
        draft_input = apply_ocr_evidence(database, draft_id=draft.id, draft_input=draft_input)
    require_complete_draft_input(draft_input)
    catalog = require_submission_ready_catalog(database)
    calculation = validate_and_calculate_input(
        database,
        catalog=catalog,
        draft_input=draft_input,
        max_items=settings.expense_max_items,
    )
    draft_input = ReimbursementDraftInput.model_validate(calculation.input_data)
    project = ResolvedProject(
        display_text=draft_input.project.text, filename_component=draft_input.project.text
    )
    subsidies = calculate_trip_subsidies(
        database,
        request_trips(trip=draft_input.trip, trips=draft_input.trips),
    )
    subsidy = subsidies[0] if draft_input.trip is not None and len(subsidies) == 1 else None
    totals = calculate_expense_totals(complete_expense_items(draft_input), subsidies)
    return WorkbookPreviewSnapshot(
        canonical_input_json=draft.input_json,
        input=draft_input,
        employee_name=employee_name,
        department_name=draft.department_name,
        project=project,
        subsidy=subsidy,
        subsidies=tuple(subsidies),
        totals=totals,
    )


def _inspect_upload_spool(
    upload: UploadFile,
    *,
    settings: Settings,
) -> tuple[int, bytes, Path]:
    upload.file.seek(0, os.SEEK_END)
    size_bytes = upload.file.tell()
    if size_bytes < 1:
        raise ApiError("EMPTY_FILE", "不能上传空文件", 400)
    if size_bytes > settings.upload_max_file_bytes:
        raise ApiError("FILE_TOO_LARGE", "单个文件超过大小限制", 413)
    upload.file.seek(0)
    first_bytes = upload.file.read(16)
    upload.file.seek(0)
    upload.file.flush()
    # The bounded parser owns this path and unlinks it when the UploadFile is
    # closed. Calling fileno rolls an in-memory upload into that private spool.
    upload.file.fileno()
    spool_name = getattr(upload.file, "name", None)
    if not isinstance(spool_name, str) or not spool_name:
        raise ApiError("TEMP_STORAGE_INVALID", "上传缓存空间无效", 500)
    return size_bytes, first_bytes, Path(spool_name)


async def _write_staging_cancellation_safe(
    staging: ReimbursementStaging,
    reservation: QuotaReservation,
    source: BinaryIO,
    *,
    expected_size: int,
) -> StagedObject:
    task = asyncio.create_task(
        asyncio.to_thread(
            staging.write_stream,
            reservation.staging,
            source,
            expected_size=expected_size,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Never race reservation cleanup against the thread that may still be
        # linking the final object.
        try:
            await task
        except Exception:
            pass
        raise


def _abandon_upload_without_masking_error(
    quota: ReimbursementQuotaCoordinator,
    owner: DraftFileOwner,
    reservation: QuotaReservation,
) -> None:
    try:
        quota.abandon_draft_file(owner, reservation)
    except Exception as exc:
        logger.error(
            "Failed to discard an unfinished reimbursement upload",
            extra={
                "draft_file_id": reservation.record_id,
                "exception_type": type(exc).__name__,
            },
        )


def _delete_file_and_release_quota(
    deletion: DraftFileDeletion,
    actor: DraftActor,
    session_factory: sessionmaker[Session],
    staging: ReimbursementStaging,
) -> int:
    staging.delete(
        deletion.storage_key,
        expected_size=deletion.size_bytes,
        expected_sha256=deletion.sha256,
        missing_ok=True,
    )
    with session_factory() as database:
        file = database.scalar(
            select(ReimbursementDraftFile)
            .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
            .where(
                ReimbursementDraftFile.id == deletion.file_id,
                ReimbursementDraftFile.draft_id == deletion.draft_id,
                ReimbursementDraftFile.storage_key == deletion.storage_key,
                ReimbursementDraftFile.size_bytes == deletion.size_bytes,
                ReimbursementDraftFile.sha256 == deletion.sha256,
                ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.DELETING.value,
                ReimbursementDraft.corp_id == actor.corp_id,
                ReimbursementDraft.owner_user_id == actor.user_id,
                ReimbursementDraft.department_id == actor.department_id,
                ReimbursementDraft.department_name == actor.department_name,
                ReimbursementDraft.revision == deletion.revision,
            )
        )
        if file is None:
            raise _revision_conflict_error()
        file.file_status = ReimbursementDraftFileStatus.PURGED.value
        file.part_storage_key = None
        file.reservation_expires_at = None
        file.purged_at = utc_now()
        database.commit()
    return deletion.revision


def _materialize_verified_file(
    file: DraftFileSnapshot,
    settings: Settings,
    staging: ReimbursementStaging,
) -> Path:
    spool_directory = prepare_spool_directory(settings)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="ocr-",
        suffix=f".{file.extension}",
        dir=spool_directory,
    )
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        output = os.fdopen(descriptor, "wb")
        descriptor = -1
        with (
            output,
            staging.open_verified(
                file.storage_key,
                expected_size=file.size_bytes,
                expected_sha256=file.sha256,
            ) as source,
        ):
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        return path
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


async def _materialize_cancellation_safe(
    file: DraftFileSnapshot,
    settings: Settings,
    staging: ReimbursementStaging,
) -> Path:
    task = asyncio.create_task(
        asyncio.to_thread(_materialize_verified_file, file, settings, staging)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The copy runs in a thread and cannot be force-cancelled. Wait for it
        # and remove any completed copy before unwinding the request.
        try:
            path = await task
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _finish_ocr(
    *,
    session_factory: sessionmaker[Session],
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    operation_revision: int,
    marker: str,
    final_status: ReimbursementOcrStatus,
    payload: dict[str, object],
    pipeline_input_json: str | None = None,
    session_id_hash: str | None = None,
) -> DraftFileMutationResult:
    with session_factory() as database:
        draft = require_owned_draft(
            database,
            draft_id=draft_id,
            actor=actor,
            mutable=True,
        )
        if pipeline_input_json is None:
            _require_revision(draft, operation_revision)
        else:
            _guard_pipeline_draft(
                database,
                draft=draft,
                actor=actor,
                input_json=pipeline_input_json,
                session_id_hash=session_id_hash,
            )
        file = database.scalar(
            select(ReimbursementDraftFile).where(
                ReimbursementDraftFile.id == file_id,
                ReimbursementDraftFile.draft_id == draft.id,
                ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                ReimbursementDraftFile.ocr_status == ReimbursementOcrStatus.RUNNING.value,
                ReimbursementDraftFile.ocr_result_json == marker,
            )
        )
        if file is None:
            raise ApiError(
                "REIMBURSEMENT_FILE_OPERATION_CONFLICT",
                "票据文件在识别期间已变更",
                409,
            )
        classification = material_classification(marker)
        auto_attempt = (
            classification is not None
            and classification.get("status") in PENDING_CLASSIFICATION_STATUSES
        )
        if classification and MATERIAL_CLASSIFICATION_KEY not in payload:
            if auto_attempt:
                classification = {
                    **classification,
                    "status": "needs_confirmation",
                    "kind": "unknown",
                    "reason": "材料识别未完成，请重试或确认用途",
                }
                # A failed auto-classification is not a failed expense line.
                payload = {}
            payload[MATERIAL_CLASSIFICATION_KEY] = classification
        final_classification = payload.get(MATERIAL_CLASSIFICATION_KEY)
        if isinstance(final_classification, dict) and auto_attempt:
            kind = final_classification.get("kind")
            next_role = "EXPENSE_SOURCE" if kind == "expense" else "ATTACHMENT_ONLY"
            next_kind = kind if kind in {"itinerary", "payment_proof", "hotel_bill"} else "other"
            if file.processing_role != next_role or file.attachment_kind != next_kind:
                if pipeline_input_json is None:
                    draft.input_json = detach_draft_file_from_input(
                        database,
                        draft=draft,
                        file_id=file.id,
                    ).canonical_json
                elif _input_references_file(draft.input_json, file.id):
                    raise _pipeline_not_allowed_error()
                file.processing_role, file.attachment_kind = next_role, next_kind
        file.ocr_status = final_status.value
        file.ocr_result_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        database.commit()
        database.refresh(file)
        return DraftFileMutationResult(revision=draft.revision, file=_snapshot(file))


def _set_ocr_failed_without_revision(
    session_factory: sessionmaker[Session],
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    marker: str,
    payload: dict[str, object],
) -> None:
    with session_factory() as database:
        file = database.scalar(
            select(ReimbursementDraftFile)
            .join(ReimbursementDraft, ReimbursementDraft.id == ReimbursementDraftFile.draft_id)
            .where(
                ReimbursementDraftFile.id == file_id,
                ReimbursementDraftFile.draft_id == draft_id,
                ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                ReimbursementDraftFile.ocr_status == ReimbursementOcrStatus.RUNNING.value,
                ReimbursementDraftFile.ocr_result_json == marker,
                ReimbursementDraft.corp_id == actor.corp_id,
                ReimbursementDraft.owner_user_id == actor.user_id,
                ReimbursementDraft.department_id == actor.department_id,
                ReimbursementDraft.department_name == actor.department_name,
            )
        )
        if file is None:
            return
        classification = material_classification(marker)
        if classification:
            if classification.get("status") in PENDING_CLASSIFICATION_STATUSES:
                classification = {
                    **classification,
                    "status": "needs_confirmation",
                    "kind": "unknown",
                    "reason": "材料识别已中断，请重试或确认用途",
                }
                payload = {}
            payload[MATERIAL_CLASSIFICATION_KEY] = classification
        # The SELECT above only prepares the safe failure payload. A newer
        # retry can replace the marker before this write, so cleanup must CAS
        # the old marker too rather than flushing an ORM update by primary key.
        database.execute(
            update(ReimbursementDraftFile)
            .where(
                ReimbursementDraftFile.id == file_id,
                ReimbursementDraftFile.draft_id == draft_id,
                ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                ReimbursementDraftFile.ocr_status == ReimbursementOcrStatus.RUNNING.value,
                ReimbursementDraftFile.ocr_result_json == marker,
                ReimbursementDraftFile.draft_id.in_(
                    select(ReimbursementDraft.id).where(
                        ReimbursementDraft.corp_id == actor.corp_id,
                        ReimbursementDraft.owner_user_id == actor.user_id,
                        ReimbursementDraft.department_id == actor.department_id,
                        ReimbursementDraft.department_name == actor.department_name,
                    )
                ),
            )
            .values(
                ocr_status=ReimbursementOcrStatus.FAILED.value,
                ocr_result_json=json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
            )
            .execution_options(synchronize_session=False)
        )
        database.commit()


def _mark_ocr_interrupted(
    *,
    session_factory: sessionmaker[Session],
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    marker: str,
) -> None:
    classification = material_classification(marker)
    failure_payload = (
        failed_itinerary_payload
        if classification and classification.get("kind") == "itinerary"
        else failed_expense_payload
    )
    payload = failure_payload(
        file_id,
        "OCR_RESULT_CONFLICT",
        "报销内容在识别期间已变更，请重试",
    )
    _set_ocr_failed_without_revision(
        session_factory,
        actor,
        draft_id,
        file_id,
        marker,
        payload,
    )


def _settle_cancelled_ocr(
    *,
    session_factory: sessionmaker[Session],
    actor: DraftActor,
    draft_id: str,
    file_id: str,
    operation_revision: int,
    marker: str,
    payload: dict[str, object],
    pipeline_input_json: str | None = None,
    session_id_hash: str | None = None,
) -> None:
    try:
        _finish_ocr(
            session_factory=session_factory,
            actor=actor,
            draft_id=draft_id,
            file_id=file_id,
            operation_revision=operation_revision,
            marker=marker,
            final_status=ReimbursementOcrStatus.FAILED,
            payload=payload,
            pipeline_input_json=pipeline_input_json,
            session_id_hash=session_id_hash,
        )
    except ApiError:
        _mark_ocr_interrupted(
            session_factory=session_factory,
            actor=actor,
            draft_id=draft_id,
            file_id=file_id,
            marker=marker,
        )


def _require_active_file(
    database: Session,
    *,
    draft_id: str,
    file_id: str,
) -> ReimbursementDraftFile:
    file = database.scalar(
        select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.id == file_id,
            ReimbursementDraftFile.draft_id == draft_id,
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
        )
    )
    if file is None:
        raise _file_not_found_error()
    return file


def _pipeline_input_hash(input_json: str) -> str:
    return sha256(input_json.encode("utf-8")).hexdigest()


def _input_references_file(input_json: str, file_id: str) -> bool:
    value = ReimbursementDraftInput.model_validate_json(input_json)
    return file_id in value.dismissed_ocr_file_ids or any(
        item.source_file_id == file_id
        or file_id in item.itinerary_file_ids
        or file_id in item.payment_proof_file_ids
        or file_id in item.hotel_bill_file_ids
        for item in value.items
    )


def _pipeline_not_allowed_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_FILE_PIPELINE_NOT_ALLOWED",
        "仅本批新上传且未关联费用的材料可并行初次识别，请刷新后使用重新识别",
        409,
    )


def _admit_pipeline_ocr(
    database: Session,
    *,
    draft: ReimbursementDraft,
    file: ReimbursementDraftFile,
    actor: DraftActor,
    expected_revision: int,
    session_id_hash: str | None,
) -> str:
    payload = json.loads(file.ocr_result_json) if file.ocr_result_json else {}
    classification = material_classification(file.ocr_result_json)
    if (
        file.ocr_status != ReimbursementOcrStatus.NOT_REQUESTED.value
        or file.processing_role != ReimbursementDraftFileRole.ATTACHMENT_ONLY.value
        or file.attachment_kind != ReimbursementAttachmentKind.OTHER.value
        or not classification
        or classification.get("status") != "pending"
        or not isinstance(payload, dict)
        or _input_references_file(draft.input_json, file.id)
    ):
        raise _pipeline_not_allowed_error()
    # Only this narrow initial-file path accepts an older client revision:
    # later uploads may advance it, but neither OCR admission nor completion
    # may change or consume an edited form. The upload's persisted hash proves
    # that the exact input is still unchanged; ordinary OCR keeps strict CAS.
    if expected_revision > draft.revision or payload.get(
        _PIPELINE_INPUT_HASH_KEY
    ) != _pipeline_input_hash(draft.input_json):
        raise _revision_conflict_error()
    _guard_pipeline_draft(
        database,
        draft=draft,
        actor=actor,
        input_json=draft.input_json,
        session_id_hash=session_id_hash,
    )
    return draft.input_json


def _guard_pipeline_draft(
    database: Session,
    *,
    draft: ReimbursementDraft,
    actor: DraftActor,
    input_json: str,
    session_id_hash: str | None,
) -> None:
    # A conditional no-op write serializes this short metadata transaction
    # against upload reservation, edits and submit, without spending a revision
    # or holding a transaction during OCR. File marker CAS is checked separately.
    now = utc_now()
    guarded = database.execute(
        update(ReimbursementDraft)
        .where(
            ReimbursementDraft.id == draft.id,
            ReimbursementDraft.corp_id == actor.corp_id,
            ReimbursementDraft.owner_user_id == actor.user_id,
            ReimbursementDraft.department_id == actor.department_id,
            ReimbursementDraft.department_name == actor.department_name,
            ReimbursementDraft.input_json == input_json,
            ReimbursementDraft.status.in_(
                (ReimbursementDraftStatus.DRAFT.value, ReimbursementDraftStatus.REVIEW_READY.value)
            ),
            ReimbursementDraft.locked_at.is_(None),
            ReimbursementDraft.expires_at > now,
        )
        .values(revision=ReimbursementDraft.revision)
        .execution_options(synchronize_session=False)
    )
    if guarded.rowcount != 1:
        raise _revision_conflict_error()
    # The guarded file-only operation deliberately allows a concurrently
    # reserved upload to advance the global revision. Read the locked row's
    # latest value instead of returning the pre-UPDATE ORM snapshot.
    database.refresh(draft)
    if session_id_hash is not None:
        active_session = database.scalar(
            select(UserSession.session_id_hash).where(
                UserSession.session_id_hash == session_id_hash,
                UserSession.corp_id == actor.corp_id,
                UserSession.dingtalk_user_id == actor.user_id,
                UserSession.current_department_id == actor.department_id,
                UserSession.current_department_name == actor.department_name,
                UserSession.expires_at > now,
            )
        )
        if active_session is None:
            raise ApiError("UNAUTHORIZED", "登录状态或部门已变更，请重新进入", 401)


def _snapshot(file: ReimbursementDraftFile) -> DraftFileSnapshot:
    if file.size_bytes is None or file.sha256 is None:
        raise ApiError(
            "REIMBURSEMENT_FILE_INVALID",
            "票据文件状态异常，请重新上传",
            409,
        )
    return DraftFileSnapshot(
        id=file.id,
        draft_id=file.draft_id,
        original_name=file.original_name,
        extension=file.extension,
        media_type=file.media_type,
        processing_role=file.processing_role,
        sort_order=file.sort_order,
        file_status=file.file_status,
        storage_key=file.storage_key,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        ocr_status=file.ocr_status,
        ocr_result_json=file.ocr_result_json,
        attachment_kind=file.attachment_kind,
    )


def _require_revision(draft: ReimbursementDraft, expected_revision: int) -> None:
    if draft.revision != expected_revision:
        raise _revision_conflict_error()


def _revision_conflict_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
        "报销内容已在其他操作中更新，请刷新后重试",
        409,
    )


def _file_not_found_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_FILE_NOT_FOUND",
        "票据文件不存在",
        404,
    )


def map_reimbursement_storage_error(exc: Exception) -> ApiError:
    if isinstance(exc, ReimbursementQuotaExceeded):
        return ApiError(
            "REIMBURSEMENT_STORAGE_FULL",
            "报销文件空间暂无足够容量，请删除无用文件后重试",
            503,
        )
    if isinstance(exc, ReimbursementReservationConflict):
        return _revision_conflict_error()
    if isinstance(exc, StagingLimitExceeded):
        return ApiError("FILE_TOO_LARGE", "单个文件超过大小限制", 413)
    if isinstance(
        exc,
        (
            StagingIntegrityError,
            StagingLayoutError,
            StagingObjectExists,
            StagingObjectNotFound,
        ),
    ):
        return ApiError(
            "REIMBURSEMENT_STORAGE_ERROR",
            "票据文件保存失败，请重试",
            500,
        )
    return ApiError("INTERNAL_ERROR", "服务暂时不可用", 500)
