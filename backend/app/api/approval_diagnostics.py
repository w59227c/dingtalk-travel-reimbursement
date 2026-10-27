from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ApiError
from app.core.request_id import request_id_for
from app.schemas.common import success
from app.services.approval_diagnostics import diagnose_travel_approval
from app.services.sessions import CurrentSession, require_admin_csrf
from app.services.travel_approvals import requested_query_window

router = APIRouter(tags=["approval-diagnostics"])


class ApprovalDiagnosticInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    user_id: str = Field(alias="userId", min_length=1, max_length=128)
    identifier: str = Field(alias="approvalIdentifier", min_length=1, max_length=512)
    identifier_kind: Literal["auto", "business_id", "instance_id"] = Field(
        default="auto", alias="identifierKind"
    )
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")
    keyword: str = Field(default="", max_length=100)

    @field_validator("user_id", "identifier")
    @classmethod
    def clean_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 33 or ord(character) == 127 for character in value):
            raise ValueError("identifier must not contain whitespace or control characters")
        return value


@router.post("/admin/oa/travel-approvals/diagnose")
async def diagnose_approval(
    body: ApprovalDiagnosticInput,
    request: Request,
    response: Response,
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    window = requested_query_window(body.from_date, body.to_date)
    lock: asyncio.Lock = request.app.state.approval_diagnostics_lock
    if lock.locked():
        raise ApiError("DIAGNOSIS_BUSY", "已有审批诊断正在运行，请稍后重试", 409)
    async with lock:
        report = await diagnose_travel_approval(
            request.app.state.database_session_factory,
            request.app.state.dingtalk_workflow,
            user_id=body.user_id,
            identifier=body.identifier,
            identifier_kind=body.identifier_kind,
            query_window=window,
            keyword=body.keyword,
        )
    response.headers["Cache-Control"] = "no-store"
    return success(report | {"requestId": request_id_for(request)})
