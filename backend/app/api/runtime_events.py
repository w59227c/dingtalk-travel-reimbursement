from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.errors import ApiError
from app.core.runtime_events import safe_code
from app.schemas.common import success
from app.services.runtime_events import RuntimeEventStore
from app.services.sessions import CurrentSession, require_admin

router = APIRouter(tags=["runtime-events"])


@router.get("/admin/runtime-events")
async def list_runtime_events(
    request: Request,
    response: Response,
    _current: Annotated[CurrentSession, Depends(require_admin)],
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    module: Literal["approvals", "materials", "submission", "auth", "system"] | None = None,
    result: Literal["success", "empty", "failed", "incomplete", "info"] | None = None,
    request_id: Annotated[str | None, Query(alias="requestId", max_length=128)] = None,
    before: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    if (from_date is None) != (to_date is None) or (
        from_date and to_date and not 0 <= (to_date - from_date).days < 7
    ):
        raise ApiError("RUNTIME_EVENTS_WINDOW_INVALID", "请同时填写起止日期，范围不超过 7 天", 422)
    if request_id is not None and not safe_code(request_id):
        raise ApiError("RUNTIME_EVENTS_REQUEST_ID_INVALID", "排查编号格式不正确", 422)
    if from_date and to_date:
        zone = ZoneInfo("Asia/Shanghai")
        try:
            start = (
                datetime.combine(from_date, time.min, tzinfo=zone)
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
            end = (
                datetime.combine(to_date, time.max, tzinfo=zone)
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except OverflowError:
            raise ApiError("RUNTIME_EVENTS_WINDOW_INVALID", "查询日期超出支持范围", 422) from None
    else:
        end = datetime.now(UTC).replace(tzinfo=None)
        start = end - timedelta(days=7)
    store: RuntimeEventStore = request.app.state.runtime_events
    await asyncio.to_thread(store.flush, 0.25)
    data = await asyncio.to_thread(
        store.search,
        start=start,
        end=end,
        module=module,
        result=result,
        request_id=request_id,
        before=before,
        limit=limit,
    )
    response.headers["Cache-Control"] = "no-store"
    return success(data)
