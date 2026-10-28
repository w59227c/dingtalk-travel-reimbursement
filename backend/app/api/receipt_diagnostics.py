from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.core.errors import ApiError
from app.core.request_id import request_id_for
from app.schemas.common import success
from app.services.application_settings import get_additional_admin_ids
from app.services.file_coordination import FileOperationBusy, SessionFilesRetired, UploadBusy
from app.services.multipart_uploads import parse_upload_files
from app.services.receipt_keywords import load_receipt_keyword_rules
from app.services.sessions import (
    CurrentSession,
    require_admin_csrf,
    require_fresh_active_file_session,
)
from app.services.temp_files import (
    close_upload_file,
    delete_session_file,
    new_upload_budget,
    store_upload,
)

router = APIRouter(tags=["receipt-diagnostics"])


@router.post("/admin/receipts/diagnose")
async def diagnose_receipt(
    request: Request,
    response: Response,
    current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    state = request.app.state
    settings = state.settings
    session_id = current.record.session_id_hash

    def revalidate() -> None:
        require_fresh_active_file_session(state.database_session_factory, settings, session_id)
        with state.database_session_factory() as database:
            if current.record.dingtalk_user_id not in (
                settings.admin_ids | get_additional_admin_ids(database)
            ):
                raise ApiError("FORBIDDEN", "仅管理员可以执行此操作", 403)

    with state.database_session_factory() as database:
        rules = load_receipt_keyword_rules(database)
    uploads = []
    stored = None
    try:
        async with state.file_coordinator.async_upload_lease(session_id):
            revalidate()
            budget = new_upload_budget(settings, session_id, 1)
            uploads = await parse_upload_files(request.headers, request.stream(), settings, budget)
            stored = await store_upload(
                uploads[0], settings, session_id, budget, state.file_validation_runner
            )
        async with state.file_coordinator.async_session_lease(session_id):
            revalidate()
            payload = await state.ocr_service.recognize_material_file(
                stored,
                reference_year=None,
                keyword_rules=rules,
                diagnostics=True,
            )
            revalidate()
            if not isinstance(payload.get("diagnostic"), dict):
                raise ApiError(
                    "RECEIPT_DIAGNOSIS_INCOMPLETE", "本次诊断未取得完整识别依据，请重试", 503
                )
            payload.pop("fileId", None)
            response.headers["Cache-Control"] = "no-store"
            return success(
                {
                    "requestId": request_id_for(request),
                    "checkedAt": datetime.now(UTC).isoformat(),
                    "keywordRuleCount": len(rules),
                    "recognition": payload,
                }
            )
    except SessionFilesRetired:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401) from None
    except (FileOperationBusy, UploadBusy):
        raise ApiError("RECEIPT_DIAGNOSIS_BUSY", "当前会话正在处理文件，请稍后重试", 429) from None
    finally:
        for upload in uploads:
            close_upload_file(upload)
        if stored is not None:
            try:
                delete_session_file(settings, session_id, stored.temp_id)
            except ApiError as error:
                if error.code != "TEMP_FILE_NOT_FOUND":
                    raise
