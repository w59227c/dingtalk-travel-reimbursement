from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import Depends, Header, Request, status
from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import random_token, token_hash, tokens_match
from app.database.session import get_db
from app.models.session import UserSession, utc_now
from app.services.application_settings import get_additional_admin_ids
from app.services.dingtalk import DepartmentIdentity, DingTalkIdentity


@dataclass(frozen=True, slots=True)
class CurrentSession:
    record: UserSession
    departments: tuple[DepartmentIdentity, ...]


class SessionCleanupGate:
    """Limit opportunistic expiry cleanup without a scheduler or worker thread."""

    def __init__(self, interval_seconds: int) -> None:
        self._interval_seconds = interval_seconds
        self._last_completed_at = 0.0
        self._lock = Lock()

    def mark_completed(self) -> None:
        with self._lock:
            self._last_completed_at = monotonic()

    def run_if_due(self, action: Callable[[], None]) -> bool:
        now = monotonic()
        if now - self._last_completed_at < self._interval_seconds:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            now = monotonic()
            if now - self._last_completed_at < self._interval_seconds:
                return False
            action()
            self._last_completed_at = monotonic()
            return True
        finally:
            self._lock.release()


def _configured_settings(request: Request) -> Settings:
    return request.app.state.settings


def selectable_departments(
    departments: tuple[DepartmentIdentity, ...],
) -> tuple[DepartmentIdentity, ...]:
    """Return ordered, unambiguous departments available to reimbursement users."""

    result: list[DepartmentIdentity] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for department in departments:
        department_id = department.id.strip()
        department_name = department.name.strip()
        if (
            not department_id
            or not department_name
            or department_name.startswith("其他")
            or department_id in seen_ids
            or department_name in seen_names
        ):
            continue
        seen_ids.add(department_id)
        seen_names.add(department_name)
        result.append(DepartmentIdentity(department_id, department_name))
    return tuple(result)


def serialize_departments(departments: tuple[DepartmentIdentity, ...]) -> str:
    return json.dumps(
        [
            {"id": item.id, "name": item.name}
            for item in selectable_departments(departments)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def deserialize_departments(raw: str) -> tuple[DepartmentIdentity, ...]:
    try:
        values = json.loads(raw)
        return selectable_departments(
            tuple(
                DepartmentIdentity(id=str(item["id"]), name=str(item["name"]))
                for item in values
                if isinstance(item, dict) and item.get("id") and item.get("name")
            )
        )
    except (TypeError, ValueError, KeyError):
        return ()


def purge_expired_sessions(database: Session) -> None:
    bind = database.get_bind()
    if not inspect(bind).has_table(UserSession.__tablename__):
        return
    database.execute(delete(UserSession).where(UserSession.expires_at <= utc_now()))
    database.commit()


def require_fresh_active_file_session(
    session_factory: sessionmaker[Session],
    settings: Settings,
    session_id_hash: str,
) -> None:
    """Revalidate file access from SQLite after acquiring the file lease.

    The request dependency may still hold an ORM identity loaded before a
    concurrent logout. A short-lived Session plus a scalar SQL query ensures
    that file access observes the committed session row as it exists now.
    """

    with session_factory() as database:
        active_union_id = database.scalar(
            select(UserSession.dingtalk_union_id).where(
                UserSession.session_id_hash == session_id_hash,
                UserSession.corp_id == settings.dingtalk_corp_id,
                UserSession.expires_at > utc_now(),
            )
        )
    if not str(active_union_id or "").strip():
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)


def create_session(
    database: Session,
    settings: Settings,
    identity: DingTalkIdentity,
    existing_cookie: str | None,
) -> tuple[UserSession, str, str]:
    departments = selectable_departments(identity.departments)
    if not departments:
        raise ApiError(
            "DINGTALK_PERMISSION_MISSING",
            "未找到可用于报销的所属部门，请联系管理员",
            502,
        )
    if existing_cookie:
        existing = database.get(
            UserSession,
            token_hash(existing_cookie, settings.session_secret),
        )
        if existing is not None:
            database.delete(existing)

    session_token = random_token()
    csrf_token = _csrf_token_for_session(settings, session_token)
    now = utc_now()
    selected = departments[0] if len(departments) == 1 else None
    record = UserSession(
        session_id_hash=token_hash(session_token, settings.session_secret),
        dingtalk_user_id=identity.user_id,
        dingtalk_union_id=identity.union_id,
        name=identity.name,
        corp_id=settings.dingtalk_corp_id,
        departments_json=serialize_departments(departments),
        current_department_id=selected.id if selected else None,
        current_department_name=selected.name if selected else None,
        csrf_token_hash=token_hash(csrf_token, settings.session_secret),
        is_admin=identity.user_id in (settings.admin_ids | get_additional_admin_ids(database)),
        created_at=now,
        expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
        last_seen_at=now,
    )
    database.add(record)
    database.commit()
    database.refresh(record)
    return record, session_token, csrf_token


def _csrf_token_for_session(settings: Settings, session_token: str) -> str:
    """Derive one stable CSRF token per login session.

    Rotating the token from ``GET /me`` invalidated other DingTalk tabs that
    shared the same HttpOnly session cookie. The session token already rotates
    on login, so deriving the CSRF value from it keeps separate login sessions
    isolated without introducing a cross-tab race.
    """

    return token_hash(f"csrf:{session_token}", settings.session_secret)


def get_session_csrf(
    database: Session,
    settings: Settings,
    record: UserSession,
    session_token: str,
) -> str:
    csrf_token = _csrf_token_for_session(settings, session_token)
    expected_hash = token_hash(csrf_token, settings.session_secret)
    if not tokens_match(csrf_token, record.csrf_token_hash, settings.session_secret):
        # Transparently migrate sessions created before stable CSRF tokens were
        # introduced. This write happens once, not on every /me request.
        record.csrf_token_hash = expected_hash
        database.commit()
    return csrf_token


def session_payload(current: CurrentSession, csrf_token: str) -> dict[str, object]:
    record = current.record
    return {
        "user": {
            "userId": record.dingtalk_user_id,
            "name": record.name,
        },
        "departments": [{"id": item.id, "name": item.name} for item in current.departments],
        "selectedDepartment": (
            {
                "id": record.current_department_id,
                "name": record.current_department_name,
            }
            if record.current_department_id and record.current_department_name
            else None
        ),
        "isAdmin": record.is_admin,
        "csrfToken": csrf_token,
    }


def get_current_session(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
) -> CurrentSession:
    settings = _configured_settings(request)
    cleanup_gate: SessionCleanupGate = request.app.state.session_cleanup_gate
    cleanup_gate.run_if_due(lambda: purge_expired_sessions(database))
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise ApiError("UNAUTHORIZED", "请从公司钉钉工作台进入本应用", 401)
    record = database.get(UserSession, token_hash(cookie, settings.session_secret))
    now = utc_now()
    if record is None:
        raise ApiError("UNAUTHORIZED", "登录状态已失效，请重新进入", 401)
    if record.expires_at <= now:
        database.delete(record)
        database.commit()
        raise ApiError("UNAUTHORIZED", "登录状态已过期，请重新进入", 401)
    if record.corp_id != settings.dingtalk_corp_id:
        database.delete(record)
        database.commit()
        raise ApiError("INVALID_CORP_CONTEXT", "当前登录不属于本公司应用", 401)
    if not str(record.dingtalk_union_id or "").strip():
        database.delete(record)
        database.commit()
        raise ApiError("UNAUTHORIZED", "登录身份数据已更新，请重新进入", 401)
    changed = False
    current_admin = record.dingtalk_user_id in (
        settings.admin_ids | get_additional_admin_ids(database)
    )
    if record.is_admin != current_admin:
        # The database column is only a cache for inspection. Authorization is
        # always re-derived from current configuration on every session load.
        record.is_admin = current_admin
        changed = True
    if (now - record.last_seen_at).total_seconds() >= 300:
        record.last_seen_at = now
        changed = True
    departments = deserialize_departments(record.departments_json)
    if not departments:
        raise ApiError("UNAUTHORIZED", "登录身份数据无效，请重新进入", 401)
    selected_is_valid = any(
        item.id == record.current_department_id
        and item.name == record.current_department_name
        for item in departments
    )
    if (record.current_department_id or record.current_department_name) and not selected_is_valid:
        record.current_department_id = None
        record.current_department_name = None
        changed = True
    if changed:
        database.commit()
    return CurrentSession(record=record, departments=departments)


def require_csrf(
    request: Request,
    current: Annotated[CurrentSession, Depends(get_current_session)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> CurrentSession:
    settings = _configured_settings(request)
    if not csrf_token or not tokens_match(
        csrf_token,
        current.record.csrf_token_hash,
        settings.session_secret,
    ):
        raise ApiError("CSRF_INVALID", "页面安全令牌已失效，请刷新后重试", 403)
    return current


def require_selected_department(current: CurrentSession) -> str:
    """Return the trusted department name or reject an incomplete reimbursement context."""

    department_name = current.record.current_department_name
    if not current.record.current_department_id or not department_name:
        raise ApiError("DEPARTMENT_REQUIRED", "请先选择本次报销部门", 409)
    return department_name


def require_admin(
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> CurrentSession:
    if not current.record.is_admin:
        raise ApiError("FORBIDDEN", "仅管理员可以执行此操作", status.HTTP_403_FORBIDDEN)
    return current


def require_admin_csrf(
    current: Annotated[CurrentSession, Depends(require_csrf)],
) -> CurrentSession:
    if not current.record.is_admin:
        raise ApiError("FORBIDDEN", "仅管理员可以执行此操作", status.HTTP_403_FORBIDDEN)
    return current
