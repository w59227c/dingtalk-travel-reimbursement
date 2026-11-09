"""Resume work independently and copy only definitively uncreated OA submissions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.models.reimbursement import (
    RECOVERY_COPY_LEASE,
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementDraftRelatedApproval,
    ReimbursementSubmission,
    new_uuid,
    utc_now,
)
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services.oa_reimbursement_payload import (
    SnapshotOriginalFile,
    parse_locked_submission_snapshot,
)
from app.services.oa_template_profiles import require_submission_ready_catalog
from app.services.reimbursement_drafts import (
    DraftActor,
    _canonical_input_data,
    _department_drafts,
    _draft_summary,
    draft_data,
    require_owned_draft,
)
from app.services.reimbursement_quota import ReimbursementQuotaCoordinator
from app.services.reimbursement_staging import (
    ReimbursementStaging,
    StagingArea,
    StagingIntegrityError,
    StagingObjectExists,
    StagingObjectNotFound,
    StagingRecoveryBusy,
    StagingReservation,
)

_LEASE = RECOVERY_COPY_LEASE


@dataclass(frozen=True, slots=True)
class _RecoveryCopy:
    file_id: str
    destination: StagingReservation
    original: SnapshotOriginalFile


def _require_current_recovery(
    database: Session, target_id: str, actor: DraftActor, generation: int
) -> ReimbursementDraft:
    target = require_owned_draft(database, draft_id=target_id, actor=actor)
    if target.expires_at <= utc_now():
        raise ApiError("REIMBURSEMENT_DRAFT_EXPIRED", "保留的材料已过期，请新开报销", 409)
    if target.revision != generation or not target.recovery_pending or target.workspace_retired:
        raise ApiError("REIMBURSEMENT_RECOVERY_BUSY", "恢复状态已更新，请刷新后继续", 409)
    return target


def _require_copy_file(database: Session, copy: _RecoveryCopy) -> ReimbursementDraftFile:
    file = database.get(ReimbursementDraftFile, copy.file_id)
    if (
        file is None
        or file.file_status not in {"RESERVED", "WRITING"}
        or file.storage_key != copy.destination.storage_key
        or file.part_storage_key != copy.destination.part_storage_key
    ):
        raise ApiError("REIMBURSEMENT_RECOVERY_BUSY", "材料恢复状态已更新，请刷新后继续", 409)
    return file


def _copied_ocr_result(ocr_result_json: str | None, file_id: str) -> str | None:
    if ocr_result_json is None:
        return None
    try:
        result = json.loads(ocr_result_json)
    except (TypeError, ValueError):
        return ocr_result_json
    # Classification-only metadata has no OCR identity to rebind.
    if not isinstance(result, dict) or "fileId" not in result:
        return ocr_result_json
    result["fileId"] = file_id
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def workspace_reimbursements(
    database: Session,
    *,
    actor: DraftActor,
) -> dict[str, object]:
    """Return the one current reimbursement, independently of task progress."""
    draft = database.scalar(
        _department_drafts(actor)
        .where(
            ReimbursementDraft.workspace_retired.is_(False),
        )
        .order_by(ReimbursementDraft.created_at.desc(), ReimbursementDraft.id.desc())
        .limit(1)
    )
    return {
        "items": [_draft_summary(draft, now=utc_now())] if draft else [],
        "total": int(draft is not None),
    }


def recover_failed_reimbursement(
    *,
    actor: DraftActor,
    source_id: str,
    expected_revision: int,
    settings: Settings,
    session_factory: sessionmaker[Session],
    staging: ReimbursementStaging,
    quota: ReimbursementQuotaCoordinator,
) -> dict[str, object]:
    """Persist reservations first; resume the same copy after retries or lost responses."""
    now = utc_now()
    with session_factory() as database:
        database.connection().exec_driver_sql("BEGIN IMMEDIATE")
        source = require_owned_draft(database, draft_id=source_id, actor=actor)
        if source.revision != expected_revision:
            raise ApiError(
                "REIMBURSEMENT_DRAFT_REVISION_CONFLICT", "报销状态已更新，请刷新后重试", 409
            )
        task = database.scalar(
            select(ReimbursementSubmission).where(
                ReimbursementSubmission.draft_id == source.id,
            )
        )
        if (
            source.status != "LOCKED"
            or task is None
            or task.status != "FAILED_FINAL"
            or task.process_instance_id is not None
            or (
                task.oa_create_started_at is not None
                and not (task.orphan_confirmed_at and task.orphan_confirmation_code)
            )
        ):
            raise ApiError(
                "REIMBURSEMENT_RECOVERY_NOT_ALLOWED", "审批结果尚未确认未创建，不能重新处理", 409
            )
        snapshot = parse_locked_submission_snapshot(source, task)
        target = database.scalar(
            select(ReimbursementDraft).where(
                ReimbursementDraft.recovery_source_draft_id == source.id,
            )
        )
        if target is not None:
            if target.workspace_retired:
                raise ApiError(
                    "REIMBURSEMENT_DRAFT_REPLACED", "已放弃这笔报销，请返回当前报销", 409
                )
            require_owned_draft(database, draft_id=target.id, actor=actor)
            if target.expires_at <= now and target.status != "LOCKED":
                raise ApiError("REIMBURSEMENT_DRAFT_EXPIRED", "保留的材料已过期，请新开报销", 409)
            if not target.recovery_pending or target.updated_at > now - _LEASE:
                return draft_data(database, target)
            target.revision += 1
            target.updated_at = now
        else:
            current_id = database.scalar(
                _department_drafts(actor)
                .where(
                    ReimbursementDraft.workspace_retired.is_(False),
                )
                .order_by(ReimbursementDraft.created_at.desc(), ReimbursementDraft.id.desc())
                .limit(1)
            )
            if source.workspace_retired or (current_id and current_id.id != source.id):
                raise ApiError(
                    "REIMBURSEMENT_DRAFT_REPLACED", "已放弃这笔报销，请返回当前报销", 409
                )
            catalog = require_submission_ready_catalog(database)
            files = {
                file.id: file
                for file in database.scalars(
                    select(ReimbursementDraftFile).where(
                        ReimbursementDraftFile.draft_id == source.id,
                    )
                ).all()
            }
            if len(snapshot.original_files) > settings.session_max_files:
                raise ApiError("REIMBURSEMENT_FILE_LIMIT", "原材料数量超过当前部署限制", 413)
            total_size = sum(file.size_bytes for file in snapshot.original_files)
            if total_size > settings.session_max_bytes:
                raise ApiError(
                    "REIMBURSEMENT_DRAFT_STORAGE_LIMIT", "原材料总量超过当前部署限制", 413
                )
            quota.admit_recovery_copy(database, total_size)
            target = ReimbursementDraft(
                corp_id=actor.corp_id,
                owner_user_id=actor.user_id,
                department_id=actor.department_id,
                department_name=actor.department_name,
                template_process_code=catalog.reimbursement.process_code,
                template_config_version=catalog.config_version,
                schema_fingerprint=catalog.reimbursement.schema.fingerprint,
                recovery_source_draft_id=source.id,
                recovery_pending=True,
                expires_at=now + timedelta(days=settings.reimbursement_draft_ttl_days),
                created_at=now,
                updated_at=now,
            )
            database.execute(
                update(ReimbursementDraft)
                .where(
                    ReimbursementDraft.corp_id == actor.corp_id,
                    ReimbursementDraft.owner_user_id == actor.user_id,
                    ReimbursementDraft.department_id == actor.department_id,
                    ReimbursementDraft.department_name == actor.department_name,
                )
                .values(workspace_retired=True, updated_at=ReimbursementDraft.updated_at)
            )
            database.add(target)
            database.flush()
            mapping = {}
            for original in snapshot.original_files:
                old = files.get(original.draft_file_id)
                reservation = staging.new_reservation(
                    StagingArea.DRAFTS,
                    target.id,
                    original.file_type,
                    reserved_bytes=original.size_bytes,
                )
                new_id = new_uuid()
                mapping[original.draft_file_id] = new_id
                database.add(
                    ReimbursementDraftFile(
                        id=new_id,
                        draft_id=target.id,
                        sort_order=original.sort_order,
                        processing_role=original.processing_role,
                        attachment_kind=original.attachment_kind,
                        storage_key=reservation.storage_key,
                        part_storage_key=reservation.part_storage_key,
                        reserved_bytes=reservation.reserved_bytes,
                        reservation_expires_at=now + timedelta(minutes=15),
                        original_name=original.file_name,
                        extension=original.file_type,
                        media_type=original.media_type,
                        file_status="RESERVED",
                        ocr_status=old.ocr_status if old else original.ocr_status,
                        ocr_result_json=_copied_ocr_result(old.ocr_result_json, new_id)
                        if old
                        else None,
                    )
                )
            value = json.loads(source.input_json)
            value["accountingSourceVerified"] = False
            for item in value["items"]:
                if item.get("sourceFileId"):
                    item["sourceFileId"] = mapping[item["sourceFileId"]]
                for key in (
                    "itineraryFileIds",
                    "paymentProofFileIds",
                    "hotelBillFileIds",
                    "receiptAttachmentFileIds",
                ):
                    if key in item:
                        item[key] = [mapping[file_id] for file_id in item[key]]
            value["dismissedOcrFileIds"] = [
                mapping[file_id] for file_id in value["dismissedOcrFileIds"]
            ]
            target.input_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            target.related_instance_ids_json = source.related_instance_ids_json
            for related in database.scalars(
                select(ReimbursementDraftRelatedApproval).where(
                    ReimbursementDraftRelatedApproval.draft_id == source.id,
                )
            ).all():
                copied = {
                    column.name: getattr(related, column.name)
                    for column in related.__table__.columns
                    if column.name not in {"id", "draft_id"}
                }
                database.add(ReimbursementDraftRelatedApproval(draft_id=target.id, **copied))
        database.flush()
        originals = {file.sort_order: file for file in snapshot.original_files}
        copies = []
        for file in database.scalars(
            select(ReimbursementDraftFile)
            .where(ReimbursementDraftFile.draft_id == target.id)
            .order_by(ReimbursementDraftFile.sort_order)
        ).all():
            if file.file_status == "ACTIVE":
                continue
            # Each lease owns a distinct partial name. Unlinking the previous
            # name can interrupt its writer, but can never publish our new part.
            if file.part_storage_key:
                staging.delete_reserved_part(
                    StagingReservation(
                        file.storage_key, file.part_storage_key, file.reserved_bytes
                    ),
                    missing_ok=True,
                )
            reservation = staging.reserve_existing_key(
                file.storage_key, reserved_bytes=file.reserved_bytes
            )
            file.part_storage_key = reservation.part_storage_key
            copies.append(_RecoveryCopy(file.id, reservation, originals[file.sort_order]))
        target_id, generation = target.id, target.revision
        database.commit()

    # Bytes are verified/copied outside the SQLite writer lock. Every destination
    # and reservation is durable, so process failure leaves an explicit resumable task.
    try:
        with staging.recovery_guard(target_id), session_factory() as database:
            for copy in copies:
                _require_current_recovery(database, target_id, actor, generation)
                original, reservation = copy.original, copy.destination
                needs_copy = False
                try:
                    # An interrupted copy can already have published its complete final object.
                    with staging.open_verified(
                        reservation.storage_key,
                        expected_size=original.size_bytes,
                        expected_sha256=original.sha256,
                    ):
                        pass
                except StagingObjectNotFound:
                    needs_copy = True
                except StagingIntegrityError:
                    # Only an unfinalized, still-owned destination may be
                    # discarded. Never remove an ACTIVE file or a newer lease.
                    database.rollback()
                    database.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    _require_current_recovery(database, target_id, actor, generation)
                    _require_copy_file(database, copy)
                    staging.discard_reservation(reservation)
                    database.commit()
                    needs_copy = True
                if needs_copy:
                    try:
                        with staging.open_verified(
                            original.storage_key,
                            expected_size=original.size_bytes,
                            expected_sha256=original.sha256,
                        ):
                            pass
                    except (StagingObjectNotFound, StagingIntegrityError):
                        raise ApiError(
                            "REIMBURSEMENT_RECOVERY_FILES_UNAVAILABLE",
                            "原材料已不可用，无法完整恢复。原报销内容仍可查看，请新开后补传材料",
                            409,
                        ) from None
                    try:
                        staging.copy(
                            original.storage_key,
                            reservation,
                            expected_size=original.size_bytes,
                            expected_sha256=original.sha256,
                        )
                    except StagingObjectExists:
                        # Another generation can have published the same source.
                        # Accept it only after verifying the actual final bytes.
                        pass
                    with staging.open_verified(
                        reservation.storage_key,
                        expected_size=original.size_bytes,
                        expected_sha256=original.sha256,
                    ):
                        pass
                database.rollback()
                database.connection().exec_driver_sql("BEGIN IMMEDIATE")
                target = _require_current_recovery(database, target_id, actor, generation)
                file = _require_copy_file(database, copy)
                file.file_status = "ACTIVE"
                file.part_storage_key = None
                file.reservation_expires_at = None
                file.size_bytes, file.sha256 = original.size_bytes, original.sha256
                file.purged_at = None
                target.updated_at = utc_now()
                database.commit()
        # No writer may remain when editing (and file deletion) is enabled.
        # Release our shared guard first, then fence every older writer through
        # the database commit that publishes recovery completion.
        try:
            with staging.recovery_guard(target_id, exclusive=True), session_factory() as database:
                database.connection().exec_driver_sql("BEGIN IMMEDIATE")
                target = _require_current_recovery(database, target_id, actor, generation)
                value = json.loads(target.input_json)
                value = ReimbursementDraftInput.model_validate(value)
                target.input_json = json.dumps(
                    _canonical_input_data(value),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                target.recovery_pending = False
                target.revision += 1
                target.updated_at = utc_now()
                database.commit()
                return draft_data(database, target)
        except StagingRecoveryBusy:
            with session_factory() as database:
                database.connection().exec_driver_sql("BEGIN IMMEDIATE")
                target = _require_current_recovery(database, target_id, actor, generation)
                target.updated_at = utc_now() - _LEASE - timedelta(seconds=1)
                database.commit()
                return draft_data(database, target)
    except BaseException as error:
        with session_factory() as database:
            database.execute(
                update(ReimbursementDraft)
                .where(
                    ReimbursementDraft.id == target_id,
                    ReimbursementDraft.revision == generation,
                    ReimbursementDraft.recovery_pending.is_(True),
                )
                .values(updated_at=utc_now() - _LEASE - timedelta(seconds=1))
            )
            database.commit()
        if isinstance(error, StagingRecoveryBusy):
            raise ApiError("REIMBURSEMENT_RECOVERY_BUSY", "材料正在清理，请稍后刷新", 409) from None
        raise
