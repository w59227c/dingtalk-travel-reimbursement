from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.material_classification import (
    MATERIAL_CLASSIFICATION_KEY,
    PENDING_CLASSIFICATION_STATUSES,
    failed_material_classification,
    material_classification,
)
from app.models.reimbursement import (
    ReimbursementAttachmentKind,
    ReimbursementDraftFile,
    ReimbursementDraftFileStatus,
    ReimbursementOcrStatus,
    utc_now,
)
from app.services.ocr_service import failed_expense_payload, failed_itinerary_payload

OCR_STALE_GRACE_SECONDS = 30


def ocr_is_actively_running(
    file: ReimbursementDraftFile,
    *,
    ocr_timeout_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Return whether a RUNNING marker can still belong to a live OCR worker."""
    if file.ocr_status != ReimbursementOcrStatus.RUNNING.value:
        return False
    running_cutoff = (now or utc_now()) - timedelta(
        seconds=ocr_timeout_seconds + OCR_STALE_GRACE_SECONDS
    )
    return file.updated_at > running_cutoff


def recover_stale_running_ocr(
    database: Session,
    *,
    draft_id: str,
    ocr_timeout_seconds: int,
    now: datetime | None = None,
) -> int:
    """Stage failed results for OCR workers that cannot still be alive.

    Callers must authorize access to the draft before invoking this recovery.
    The caller owns the transaction and decides whether the recovery belongs to
    the surrounding write. Read-only endpoints must not call this function.
    """
    running_cutoff = (now or utc_now()) - timedelta(
        seconds=ocr_timeout_seconds + OCR_STALE_GRACE_SECONDS
    )
    stale_files = database.scalars(
        select(ReimbursementDraftFile).where(
            ReimbursementDraftFile.draft_id == draft_id,
            ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
            ReimbursementDraftFile.ocr_status == ReimbursementOcrStatus.RUNNING.value,
            ReimbursementDraftFile.updated_at <= running_cutoff,
        )
    ).all()
    recovered = 0
    for file in stale_files:
        marker = file.ocr_result_json
        payload = failed_ocr_payload(
            file,
            marker=marker,
            code="OCR_INTERRUPTED",
            message="材料识别因服务中断未完成，请重新识别或手动处理",
        )
        result = database.execute(
            update(ReimbursementDraftFile)
            .where(
                ReimbursementDraftFile.id == file.id,
                ReimbursementDraftFile.draft_id == draft_id,
                ReimbursementDraftFile.file_status == ReimbursementDraftFileStatus.ACTIVE.value,
                ReimbursementDraftFile.ocr_status == ReimbursementOcrStatus.RUNNING.value,
                ReimbursementDraftFile.ocr_result_json == marker,
                ReimbursementDraftFile.updated_at <= running_cutoff,
            )
            .values(
                ocr_status=ReimbursementOcrStatus.FAILED.value,
                ocr_result_json=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        recovered += int(result.rowcount or 0)
    if recovered:
        database.expire_all()
    return recovered


def failed_ocr_payload(
    file: ReimbursementDraftFile,
    *,
    marker: str | None,
    code: str,
    message: str,
) -> dict[str, object]:
    """Build one consistent terminal payload for interrupted durable OCR."""

    classification = material_classification(marker)
    if classification and classification.get("status") in PENDING_CLASSIFICATION_STATUSES:
        return {
            MATERIAL_CLASSIFICATION_KEY: failed_material_classification(
                classification,
                code=code,
                message=message,
            )
        }
    kind = file.attachment_kind
    if classification is not None and classification.get("kind") in {
        "itinerary",
        "payment_proof",
        "hotel_bill",
    }:
        kind = str(classification["kind"])
    if kind == ReimbursementAttachmentKind.ITINERARY.value:
        payload = failed_itinerary_payload(file.id, code, message)
    elif kind == ReimbursementAttachmentKind.HOTEL_BILL.value:
        payload = {
            "status": "failed",
            "kind": "hotel_bill",
            "warnings": ["HOTEL_BILL_INCOMPLETE", "HOTEL_BILL_REVIEW_REQUIRED"],
            "error": {"code": code, "message": message},
            "hotelBillDetails": {
                "warnings": ["HOTEL_BILL_INCOMPLETE", "HOTEL_BILL_REVIEW_REQUIRED"]
            },
        }
    else:
        payload = failed_expense_payload(file.id, code, message)
    if classification:
        payload[MATERIAL_CLASSIFICATION_KEY] = classification
    return payload
