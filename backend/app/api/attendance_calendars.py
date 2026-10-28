from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.schemas.common import success
from app.services.attendance_calendar import (
    MAX_BODY_BYTES,
    NoticeApply,
    NoticeInput,
    apply_notice,
    delete_calendar,
    list_calendars,
    preview_notice,
)
from app.services.sessions import CurrentSession, require_admin, require_admin_csrf

router = APIRouter(tags=["attendance-calendars"])


async def _body(request: Request, model: type[NoticeInput]):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError("ATTENDANCE_NOTICE_INVALID", "请粘贴完整年度通知或年度 JSON", 422)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BODY_BYTES:
            raise ApiError(
                "ATTENDANCE_NOTICE_TOO_LARGE", "内容过长，请仅粘贴年度通知或年度 JSON", 413
            )
        content.extend(chunk)
    try:
        return model.model_validate_json(content)
    except ValidationError:
        # Never reflect a pasted document or validation input in errors/logs.
        raise ApiError(
            "ATTENDANCE_NOTICE_INVALID", "内容输入无效，最多 20000 字；请重新解析后保存", 422
        ) from None


@router.get("/admin/attendance-calendars")
def get_calendars(
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin)],
):
    return success({"items": list_calendars(database)})


@router.post("/admin/attendance-calendars/parse")
async def parse_calendar(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
):
    body = await _body(request, NoticeInput)
    return success(preview_notice(database, body.text, body.correction))


@router.post("/admin/attendance-calendars/apply")
async def apply_calendar(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
):
    body = await _body(request, NoticeApply)
    return success(apply_notice(database, body))


@router.delete("/admin/attendance-calendars/{year}")
def remove_calendar(
    year: int,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
    revision: Annotated[str, Query(min_length=1, max_length=36)],
):
    delete_calendar(database, year, revision)
    return success({"items": list_calendars(database)})
