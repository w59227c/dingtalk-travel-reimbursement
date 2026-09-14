from __future__ import annotations

from datetime import timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.database.session import get_db
from app.models.reimbursement import ReimbursementAttachmentKind, ReimbursementDraftFileRole
from app.models.session import UserSession, utc_now
from app.schemas.common import success
from app.schemas.reimbursements import DraftRevisionRequest
from app.services.excel_generator import XLSX_MEDIA_TYPE, content_disposition
from app.services.excel_preview_tickets import (
    InvalidExcelPreviewTicket,
    issue_excel_preview_ticket,
    verify_excel_preview_ticket,
)
from app.services.file_coordination import (
    SessionFileCoordinator,
    SessionFilesRetired,
    UploadBusy,
)
from app.services.file_preview_tickets import (
    InvalidFilePreviewTicket,
    issue_file_preview_ticket,
    verify_file_preview_ticket,
)
from app.services.multipart_uploads import parse_upload_files
from app.services.ocr_service import OcrService
from app.services.process_jobs import KillableProcessRunner
from app.services.reimbursement_drafts import (
    draft_actor,
    require_owned_draft,
)
from app.services.reimbursement_files import (
    begin_draft_file_delete,
    begin_draft_files_clear,
    complete_draft_file_delete,
    complete_draft_files_clear,
    generate_draft_excel_preview,
    list_draft_files,
    map_reimbursement_storage_error,
    persist_draft_upload,
    read_draft_file_content,
    recognize_draft_file,
    require_previewable_draft_file,
    serialize_draft_file,
    update_draft_file,
    validate_expense_source_conversion,
)
from app.services.reimbursement_quota import (
    ReimbursementQuotaCoordinator,
    ReimbursementQuotaError,
)
from app.services.reimbursement_staging import (
    ReimbursementStaging,
    ReimbursementStagingError,
)
from app.services.sessions import (
    CurrentSession,
    deserialize_departments,
    get_current_session,
    require_csrf,
    require_fresh_active_file_session,
)
from app.services.temp_files import close_upload_file, new_upload_budget

router = APIRouter(tags=["reimbursement-files"])
_EXCEL_PREVIEW_TICKET_LIFETIME = timedelta(seconds=60)
_FILE_PREVIEW_TICKET_LIFETIME = timedelta(seconds=60)
_NATIVE_FILE_TYPES = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
}


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class UpdateDraftFileRequest(StrictRequest):
    expected_revision: int = Field(alias="expectedRevision", ge=1, strict=True)
    role: ReimbursementDraftFileRole | None = None
    attachment_kind: ReimbursementAttachmentKind | None = Field(
        default=None, alias="attachmentKind"
    )
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def require_change(self) -> UpdateDraftFileRequest:
        if self.role is None and self.name is None and self.attachment_kind is None:
            raise ValueError("at least one file field must be supplied")
        return self


class DraftFileOcrRequest(DraftRevisionRequest):
    trip_year: int | None = Field(default=None, alias="tripYear", ge=2000, le=2100)
    allow_upload_overlap: bool = Field(default=False, alias="allowUploadOverlap", strict=True)


def _require_multipart(request: Request) -> None:
    content_type = request.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise ApiError("MALFORMED_MULTIPART", "上传请求必须使用 multipart/form-data", 400)


@router.get("/reimbursements/drafts/{draft_id}/files")
def get_files(
    draft_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    revision, files = list_draft_files(
        database,
        draft_id=draft_id,
        actor=draft_actor(current),
    )
    return success(
        {
            "draftId": draft_id,
            "revision": revision,
            "items": [
                serialize_draft_file(
                    item,
                    ocr_timeout_seconds=(
                        request.app.state.settings.ocr_operation_timeout_seconds
                    ),
                )
                for item in files
            ],
        }
    )


@router.post(
    "/reimbursements/drafts/{draft_id}/files",
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    draft_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
    role: Annotated[
        ReimbursementDraftFileRole,
        Query(),
    ] = ReimbursementDraftFileRole.EXPENSE_SOURCE,
    attachment_kind: Annotated[
        ReimbursementAttachmentKind, Query(alias="attachmentKind")
    ] = ReimbursementAttachmentKind.OTHER,
    auto_classify: Annotated[bool, Query(alias="autoClassify")] = False,
) -> dict[str, object]:
    _require_multipart(request)
    actor = draft_actor(current)
    # Reject inaccessible drafts before consuming the multipart stream, then
    # release the auth read transaction before parsing and worker I/O.
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor, mutable=True)
    if draft.revision != expected_revision:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
            "报销内容版本已更新，请刷新后重试",
            409,
        )
    session_id_hash = current.record.session_id_hash
    database.rollback()

    settings: Settings = request.app.state.settings
    session_factory: sessionmaker[Session] = request.app.state.database_session_factory
    quota: ReimbursementQuotaCoordinator = request.app.state.reimbursement_quota
    staging: ReimbursementStaging = request.app.state.reimbursement_staging
    process_runner: KillableProcessRunner = request.app.state.file_validation_runner
    coordinator: SessionFileCoordinator = request.app.state.file_coordinator
    uploads = []
    try:
        async with coordinator.async_upload_lease(session_id_hash):
            require_fresh_active_file_session(session_factory, settings, session_id_hash)
            budget = new_upload_budget(settings, session_id_hash, 1)
            uploads = await parse_upload_files(
                request.headers,
                request.stream(),
                settings,
                budget,
            )
            result = await persist_draft_upload(
                upload=uploads[0],
                actor=actor,
                draft_id=draft_id,
                expected_revision=expected_revision,
                processing_role=role,
                attachment_kind=attachment_kind,
                auto_classify=auto_classify,
                settings=settings,
                session_factory=session_factory,
                quota=quota,
                staging=staging,
                process_runner=process_runner,
            )
    except SessionFilesRetired as exc:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401) from exc
    except UploadBusy as exc:
        raise ApiError("UPLOAD_BUSY", "上传服务繁忙，请稍后重试", 429) from exc
    except (ReimbursementQuotaError, ReimbursementStagingError) as exc:
        raise map_reimbursement_storage_error(exc) from exc
    finally:
        for upload in uploads:
            close_upload_file(upload)

    return success(
        {
            "draftId": draft_id,
            "revision": result.revision,
            "file": serialize_draft_file(result.file),
        }
    )


@router.get("/reimbursements/drafts/{draft_id}/files/{file_id}/content")
def preview_file(
    draft_id: str,
    file_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> Response:
    try:
        file, content = read_draft_file_content(
            database,
            actor=draft_actor(current),
            draft_id=draft_id,
            file_id=file_id,
            staging=request.app.state.reimbursement_staging,
        )
    except ReimbursementStagingError as exc:
        raise map_reimbursement_storage_error(exc) from exc
    return Response(
        content=content,
        media_type=file.media_type,
        headers={
            "Content-Disposition": (
                f"inline; filename=receipt.{file.extension.lstrip('.')}; "
                f"filename*=UTF-8''{quote(file.original_name, safe='')}"
            ),
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.post("/reimbursements/drafts/{draft_id}/files/{file_id}/preview-ticket")
def create_file_preview_ticket(
    draft_id: str,
    file_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    file = require_previewable_draft_file(
        database,
        actor=draft_actor(current),
        draft_id=draft_id,
        file_id=file_id,
    )
    token = issue_file_preview_ticket(
        draft_id=draft_id,
        file_id=file_id,
        session_id_hash=current.record.session_id_hash,
        secret=request.app.state.settings.session_secret,
        lifetime=_FILE_PREVIEW_TICKET_LIFETIME,
    )
    return success(
        {
            "downloadUrl": (
                f"/api/reimbursements/drafts/{quote(draft_id, safe='')}/files/"
                f"{quote(file_id, safe='')}/preview/native"
            ),
            "downloadToken": token,
            "fileType": _NATIVE_FILE_TYPES[file.media_type],
        }
    )


@router.get("/reimbursements/drafts/{draft_id}/files/{file_id}/preview/native")
def download_native_file_preview(
    draft_id: str,
    file_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    download_token: Annotated[
        str | None,
        Header(alias="X-Reimbursement-Download-Token"),
    ] = None,
) -> Response:
    settings: Settings = request.app.state.settings
    try:
        ticket = verify_file_preview_ticket(
            download_token or "",
            secret=settings.session_secret,
        )
    except InvalidFilePreviewTicket as exc:
        raise ApiError(
            "FILE_PREVIEW_TICKET_INVALID",
            "文件预览凭证已失效，请返回钉钉重新打开",
            401,
        ) from exc
    if ticket.draft_id != draft_id or ticket.file_id != file_id:
        raise ApiError(
            "FILE_PREVIEW_TICKET_INVALID",
            "文件预览凭证已失效，请返回钉钉重新打开",
            401,
        )
    record = database.get(UserSession, ticket.session_id_hash)
    if (
        record is None
        or record.expires_at <= utc_now()
        or record.corp_id != settings.dingtalk_corp_id
        or not str(record.dingtalk_union_id or "").strip()
    ):
        raise ApiError(
            "FILE_PREVIEW_TICKET_INVALID",
            "登录状态已失效，请从公司钉钉工作台重新进入",
            401,
        )
    current = CurrentSession(
        record=record,
        departments=deserialize_departments(record.departments_json),
    )
    try:
        file, content = read_draft_file_content(
            database,
            actor=draft_actor(current),
            draft_id=draft_id,
            file_id=file_id,
            staging=request.app.state.reimbursement_staging,
        )
    except ReimbursementStagingError as exc:
        raise map_reimbursement_storage_error(exc) from exc
    return Response(
        content=content,
        media_type=file.media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=receipt.{file.extension.lstrip('.')}; "
                f"filename*=UTF-8''{quote(file.original_name, safe='')}"
            ),
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/reimbursements/drafts/{draft_id}/files/{file_id}")
async def patch_file(
    draft_id: str,
    file_id: str,
    body: UpdateDraftFileRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    if body.role is ReimbursementDraftFileRole.EXPENSE_SOURCE:
        await validate_expense_source_conversion(
            database=database,
            actor=draft_actor(current),
            draft_id=draft_id,
            file_id=file_id,
            expected_revision=body.expected_revision,
            settings=request.app.state.settings,
            staging=request.app.state.reimbursement_staging,
            process_runner=request.app.state.file_validation_runner,
        )
    result = update_draft_file(
        database,
        actor=draft_actor(current),
        draft_id=draft_id,
        file_id=file_id,
        expected_revision=body.expected_revision,
        processing_role=body.role,
        attachment_kind=body.attachment_kind,
        original_name=body.name,
        settings=request.app.state.settings,
    )
    return success(
        {
            "draftId": draft_id,
            "revision": result.revision,
            "file": serialize_draft_file(result.file),
        }
    )


@router.delete("/reimbursements/drafts/{draft_id}/files/{file_id}")
async def delete_file(
    draft_id: str,
    file_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
) -> dict[str, object]:
    actor = draft_actor(current)
    deletion = begin_draft_file_delete(
        database,
        actor=actor,
        draft_id=draft_id,
        file_id=file_id,
        expected_revision=expected_revision,
        settings=request.app.state.settings,
    )
    try:
        revision = await complete_draft_file_delete(
            deletion=deletion,
            actor=actor,
            session_factory=request.app.state.database_session_factory,
            staging=request.app.state.reimbursement_staging,
        )
    except ReimbursementStagingError as exc:
        raise map_reimbursement_storage_error(exc) from exc
    return success(
        {
            "draftId": draft_id,
            "revision": revision,
            "deletedFileId": file_id,
        }
    )


@router.post("/reimbursements/drafts/{draft_id}/files/clear")
async def clear_files(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    actor = draft_actor(current)
    revision, deletions = begin_draft_files_clear(
        database,
        actor=actor,
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        settings=request.app.state.settings,
    )
    try:
        await complete_draft_files_clear(
            deletions=deletions,
            actor=actor,
            session_factory=request.app.state.database_session_factory,
            staging=request.app.state.reimbursement_staging,
        )
    except ReimbursementStagingError as exc:
        raise map_reimbursement_storage_error(exc) from exc
    return success(
        {
            "draftId": draft_id,
            "revision": revision,
            "deletedFileIds": [item.file_id for item in deletions],
        }
    )


@router.post("/reimbursements/drafts/{draft_id}/files/{file_id}/ocr")
async def recognize_file(
    draft_id: str,
    file_id: str,
    body: DraftFileOcrRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    actor = draft_actor(current)
    # The durable OCR service opens short transactions around state changes;
    # do not retain the authentication read transaction during OCR.
    database.rollback()
    ocr_service: OcrService = request.app.state.ocr_service
    result = await recognize_draft_file(
        actor=actor,
        draft_id=draft_id,
        file_id=file_id,
        expected_revision=body.expected_revision,
        reference_year=body.trip_year,
        settings=request.app.state.settings,
        session_factory=request.app.state.database_session_factory,
        staging=request.app.state.reimbursement_staging,
        ocr_service=ocr_service,
        allow_upload_overlap=body.allow_upload_overlap,
        session_id_hash=current.record.session_id_hash if body.allow_upload_overlap else None,
    )
    return success(
        {
            "draftId": draft_id,
            "revision": result.revision,
            "file": serialize_draft_file(result.file),
        }
    )


@router.post("/reimbursements/drafts/{draft_id}/excel-preview")
async def preview_excel(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> StreamingResponse:
    return await _excel_preview_response(
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        request=request,
        database=database,
        current=current,
    )


@router.get("/reimbursements/drafts/{draft_id}/excel-preview")
async def open_excel_preview(
    draft_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
) -> StreamingResponse:
    return await _excel_preview_response(
        draft_id=draft_id,
        expected_revision=expected_revision,
        request=request,
        database=database,
        current=current,
    )


@router.post("/reimbursements/drafts/{draft_id}/excel-preview-ticket")
def create_excel_preview_ticket(
    draft_id: str,
    body: DraftRevisionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> dict[str, object]:
    actor = draft_actor(current)
    draft = require_owned_draft(database, draft_id=draft_id, actor=actor)
    if draft.revision != body.expected_revision:
        raise ApiError(
            "REIMBURSEMENT_DRAFT_REVISION_CONFLICT",
            "报销内容版本已更新，请刷新后重试",
            409,
        )
    token = issue_excel_preview_ticket(
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        session_id_hash=current.record.session_id_hash,
        secret=request.app.state.settings.session_secret,
        lifetime=_EXCEL_PREVIEW_TICKET_LIFETIME,
    )
    return success(
        {
            "downloadUrl": (
                f"/api/reimbursements/drafts/{quote(draft_id, safe='')}/excel-preview/native"
            ),
            "downloadToken": token,
            "fileType": "xlsx",
        }
    )


@router.get("/reimbursements/drafts/{draft_id}/excel-preview/native")
async def download_native_excel_preview(
    draft_id: str,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    download_token: Annotated[
        str | None,
        Header(alias="X-Reimbursement-Download-Token"),
    ] = None,
) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    try:
        ticket = verify_excel_preview_ticket(
            download_token or "",
            secret=settings.session_secret,
        )
    except InvalidExcelPreviewTicket as exc:
        raise ApiError(
            "EXCEL_PREVIEW_TICKET_INVALID",
            "Excel 预览凭证已失效，请返回钉钉重新打开",
            401,
        ) from exc
    if ticket.draft_id != draft_id:
        raise ApiError(
            "EXCEL_PREVIEW_TICKET_INVALID",
            "Excel 预览凭证已失效，请返回钉钉重新打开",
            401,
        )
    record = database.get(UserSession, ticket.session_id_hash)
    if (
        record is None
        or record.expires_at <= utc_now()
        or record.corp_id != settings.dingtalk_corp_id
        or not str(record.dingtalk_union_id or "").strip()
    ):
        raise ApiError(
            "EXCEL_PREVIEW_TICKET_INVALID",
            "登录状态已失效，请从公司钉钉工作台重新进入",
            401,
        )
    current = CurrentSession(
        record=record,
        departments=deserialize_departments(record.departments_json),
    )
    return await _excel_preview_response(
        draft_id=draft_id,
        expected_revision=ticket.expected_revision,
        request=request,
        database=database,
        current=current,
    )


async def _excel_preview_response(
    *,
    draft_id: str,
    expected_revision: int,
    request: Request,
    database: Session,
    current: CurrentSession,
) -> StreamingResponse:
    actor = draft_actor(current)
    employee_name = current.record.name
    database.rollback()
    result = await generate_draft_excel_preview(
        actor=actor,
        employee_name=employee_name,
        draft_id=draft_id,
        expected_revision=expected_revision,
        settings=request.app.state.settings,
        session_factory=request.app.state.database_session_factory,
    )
    return StreamingResponse(
        iter((result.content,)),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": content_disposition(result.filename),
            "Cache-Control": "no-store",
        },
    )
