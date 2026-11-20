from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.security import token_hash, tokens_match
from app.database.session import get_db
from app.models.reimbursement import ReimbursementDraft
from app.models.session import UserSession, utc_now
from app.services.dingtalk import DepartmentIdentity
from app.services.reimbursement_delegation_policy import (
    delegation_forbidden,
    get_delegation_policy,
    normalize_directory_id,
    require_delegation_permission_for_session,
)
from app.services.sessions import (
    CurrentSession,
    get_current_session,
    require_csrf,
    selectable_departments,
    session_directory_department_ids,
)

REIMBURSEMENT_CONTEXT_HEADER = "X-Reimbursement-Context"
MAX_REIMBURSEMENT_CONTEXT_LENGTH = 1024


def _context_error() -> ApiError:
    return ApiError(
        "REIMBURSEMENT_CONTEXT_INVALID",
        "代报身份已失效，请重新选择报销人",
        403,
    )


def _signed_value(session_id_hash: str, encoded: str) -> str:
    return f"reimbursement-context:{session_id_hash}:{encoded}"


def _issue_token(current: CurrentSession, secret: str) -> str:
    claims = (current.reimbursement_user_id, current.reimbursement_department_id)
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode()
    signature = token_hash(_signed_value(current.record.session_id_hash, encoded), secret)
    return f"{encoded}.{signature}"


def _verify_token(
    token: str, *, session_id_hash: str, secret: str
) -> tuple[str, str | None]:
    if len(token) > MAX_REIMBURSEMENT_CONTEXT_LENGTH:
        raise _context_error()
    try:
        encoded, signature = token.split(".")
        if not tokens_match(_signed_value(session_id_hash, encoded), signature, secret):
            raise ValueError
        user_id, department_id = json.loads(
            base64.b64decode(encoded, altchars=b"-_", validate=True).decode()
        )
        if not isinstance(user_id, str) or not (
            department_id is None or isinstance(department_id, str)
        ):
            raise ValueError
        return (
            normalize_directory_id(user_id),
            normalize_directory_id(department_id) if department_id is not None else None,
        )
    except (ValueError, TypeError):
        raise _context_error() from None


def can_delegate_reimbursements(database: Session, record: UserSession) -> bool:
    policy = get_delegation_policy(database, record.corp_id)
    return policy.permits(record.dingtalk_user_id, session_directory_department_ids(record))


async def validate_reimbursement_operator(
    request: Request, database: Session, current: CurrentSession, *, fresh: bool = False
) -> tuple[str, ...]:
    record = current.record
    policy = get_delegation_policy(database, record.corp_id)
    user_id = record.dingtalk_user_id
    if user_id in policy.employee_user_ids:
        return ()
    if not policy.department_ids:
        raise delegation_forbidden()
    session_id = record.session_id_hash
    database.rollback()
    try:
        operator = await request.app.state.dingtalk_service.get_employee_identity(
            user_id, fresh=fresh
        )
    except ApiError as exc:
        if exc.code == "REIMBURSEMENT_EMPLOYEE_NOT_FOUND":
            raise delegation_forbidden() from None
        raise
    record = _require_live_session(database, session_id)
    if record.dingtalk_union_id != operator.union_id:
        raise delegation_forbidden()
    department_ids = tuple(department.id for department in operator.departments)
    require_delegation_permission_for_session(database, record, department_ids)
    return department_ids


def require_live_reimbursement_actor(
    database: Session, current: CurrentSession, session_id_hash: str
) -> None:
    if current.beneficiary is None:
        return
    record = _require_live_session(database, session_id_hash)
    require_delegation_permission_for_session(
        database, record, current.reimbursement_operator_department_ids
    )


def _require_live_session(database: Session, session_id_hash: str) -> UserSession:
    record = database.get(UserSession, session_id_hash, populate_existing=True)
    if (
        record is None or record.expires_at <= utc_now()
        or not record.dingtalk_union_id
    ):
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    return record


async def _select_context(
    request: Request,
    database: Session,
    current: CurrentSession,
    user_id: str,
    department_id: str | None,
    *,
    restore_workspace: bool,
) -> CurrentSession:
    record = current.record
    session_id_hash = record.session_id_hash
    corp_id = record.corp_id
    operator_user_id = record.dingtalk_user_id
    if user_id == operator_user_id:
        raise _context_error()
    operator_department_ids = await validate_reimbursement_operator(
        request, database, current, fresh=restore_workspace
    )
    database.rollback()
    try:
        employee = await request.app.state.dingtalk_service.get_employee_identity(
            user_id, fresh=restore_workspace
        )
    except ApiError as exc:
        if not restore_workspace and exc.code == "REIMBURSEMENT_EMPLOYEE_NOT_FOUND":
            raise _context_error() from None
        raise
    if employee.user_id != user_id:
        raise _context_error()
    departments = selectable_departments(employee.departments)
    if department_id is None and restore_workspace:
        department_id = database.scalar(
            select(ReimbursementDraft.department_id).where(
                ReimbursementDraft.corp_id == corp_id,
                ReimbursementDraft.owner_user_id == user_id,
                ReimbursementDraft.operator_user_id == operator_user_id,
                ReimbursementDraft.department_id.in_(tuple(item.id for item in departments)),
                ReimbursementDraft.workspace_retired.is_(False),
            ).order_by(ReimbursementDraft.updated_at.desc(), ReimbursementDraft.id.desc()).limit(1)
        )
    selected = next((item for item in departments if item.id == department_id), None)
    if restore_workspace and department_id is not None and selected is None:
        raise _context_error()
    # Signed legacy contexts retain their beneficiary after a directory move.
    # Current delegation permission and employee identity are still rechecked.
    if selected is None and len(departments) == 1:
        selected = departments[0]
    scoped = replace(
        current, beneficiary=employee, beneficiary_department=selected,
        reimbursement_operator_department_ids=operator_department_ids,
    )
    require_live_reimbursement_actor(database, scoped, session_id_hash)
    return with_reimbursement_department(scoped, selected, request)


def with_reimbursement_department(
    current: CurrentSession, department: DepartmentIdentity | None, request: Request
) -> CurrentSession:
    selected = replace(current, beneficiary_department=department)
    return replace(
        selected,
        reimbursement_context=_issue_token(selected, request.app.state.settings.session_secret),
    )


async def begin_reimbursement_context(
    request: Request,
    database: Session,
    current: CurrentSession,
    user_id: str | None,
    department_id: str | None,
) -> CurrentSession:
    if user_id is None or user_id == current.record.dingtalk_user_id:
        return current
    return await _select_context(
        request, database, current, user_id, department_id, restore_workspace=True
    )


async def restore_reimbursement_context(
    request: Request, database: Session, current: CurrentSession, token: str | None
) -> CurrentSession:
    if token is None:
        return current
    user_id, department_id = _verify_token(
        token,
        session_id_hash=current.record.session_id_hash,
        secret=request.app.state.settings.session_secret,
    )
    return await _select_context(
        request, database, current, user_id, department_id, restore_workspace=False
    )


async def get_reimbursement_session(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> CurrentSession:
    return await restore_reimbursement_context(
        request, database, current, request.headers.get(REIMBURSEMENT_CONTEXT_HEADER)
    )


async def require_reimbursement_csrf(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> CurrentSession:
    return await restore_reimbursement_context(
        request, database, current, request.headers.get(REIMBURSEMENT_CONTEXT_HEADER)
    )
