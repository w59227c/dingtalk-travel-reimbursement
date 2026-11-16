from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.database.session import get_db
from app.integrations.dingtalk.client import DingTalkOpenAPIError
from app.schemas.common import success
from app.services.dingtalk import DingTalkService
from app.services.reimbursement_delegation_policy import normalize_directory_id
from app.services.sessions import CurrentSession, get_current_session, require_admin

router = APIRouter(prefix="/admin/directory", tags=["admin-directory"])


async def _directory_response(
    request: Request, response: Response, database: Session, session_id: str,
    items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    current = get_current_session(request, database)
    if current.record.session_id_hash != session_id:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    await require_admin(request, database, current)
    response.headers["Cache-Control"] = "no-store"
    return success({"items": items})


@router.get("/departments")
async def list_departments(
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_admin)],
    parent_id: Annotated[int, Query(alias="parentId", ge=1, le=2**63 - 1)] = 1,
) -> dict[str, object]:
    session_id = current.record.session_id_hash
    database.rollback()
    directory: DingTalkService = request.app.state.dingtalk_service
    departments = await directory.list_child_departments(parent_id)
    return await _directory_response(
        request, response, database, session_id,
        [{"id": item.id, "name": item.name} for item in departments],
    )


@router.get("/employees")
async def search_employees(
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_admin)],
    query: Annotated[str, Query(alias="q", min_length=1, max_length=128)],
) -> dict[str, object]:
    session_id = current.record.session_id_hash
    database.rollback()
    directory: DingTalkService = request.app.state.dingtalk_service
    entries = await directory.search_employee_entries(query.strip()) if query.strip() else []
    return await _directory_response(
        request, response, database, session_id,
        [
            {"id": item["userId"], "name": item["name"], "departmentNames": item["departmentNames"]}
            for item in entries
        ],
    )


@router.get("/labels")
async def lookup_labels(
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_admin)],
    kind: Literal["department", "employee"],
    ids: Annotated[list[str], Query(min_length=1, max_length=20)],
) -> dict[str, object]:
    session_id = current.record.session_id_hash
    database.rollback()
    directory: DingTalkService = request.app.state.dingtalk_service
    sem = asyncio.Semaphore(5)

    async def label(raw_id: str) -> dict[str, str] | None:
        async with sem:
            try:
                item_id = normalize_directory_id(raw_id)
                if kind == "department":
                    item = await directory.get_department_identity(item_id)
                    return {"id": item.id, "name": item.name}
                employee = await directory.employee_directory_entry(item_id)
                return {"id": item_id, "name": employee.name} if employee else None
            except (ApiError, DingTalkOpenAPIError, ValueError):
                return None

    items = await asyncio.gather(*(label(value) for value in dict.fromkeys(ids)))
    return await _directory_response(
        request, response, database, session_id, [item for item in items if item is not None],
    )
