from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models.session import UserSession
from app.models.setting import Setting

MAX_DELEGATION_GRANTS = 100
_ID = re.compile(r"[A-Za-z0-9_.:@+-]{1,128}")


def normalize_directory_id(value: str) -> str:
    result = value.strip()
    if _ID.fullmatch(result) is None:
        raise ValueError("钉钉 ID 格式无效")
    return result


def normalize_grant_ids(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted({normalize_directory_id(item) for item in values}))
    if len(result) > MAX_DELEGATION_GRANTS:
        raise ValueError("代报授权项过多")
    return result


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    employee_user_ids: tuple[str, ...] = ()
    department_ids: tuple[str, ...] = ()

    def serialize(self) -> str:
        return json.dumps(
            {"employeeUserIds": self.employee_user_ids, "departmentIds": self.department_ids},
            separators=(",", ":"),
        )

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.serialize().encode()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "employeeUserIds": list(self.employee_user_ids),
            "departmentIds": list(self.department_ids),
            "revision": self.revision,
        }

    def permits(self, user_id: str, department_ids: Iterable[str]) -> bool:
        return user_id in self.employee_user_ids or not set(self.department_ids).isdisjoint(
            department_ids
        )


def _policy_key(corp_id: str) -> str:
    return f"reimbursement_delegation:{hashlib.sha256(corp_id.encode()).hexdigest()}"


def get_delegation_policy(database: Session, corp_id: str) -> DelegationPolicy:
    row = database.get(Setting, _policy_key(corp_id), populate_existing=True)
    if row is None:
        return DelegationPolicy()
    try:
        value = json.loads(row.value)
        employees = value["employeeUserIds"]
        departments = value["departmentIds"]
        if (
            not isinstance(employees, list)
            or not isinstance(departments, list)
            or not all(isinstance(item, str) for item in (*employees, *departments))
        ):
            raise ValueError
        return DelegationPolicy(normalize_grant_ids(employees), normalize_grant_ids(departments))
    except (KeyError, TypeError, ValueError):
        raise ApiError(
            "REIMBURSEMENT_DELEGATION_INVALID",
            "代报授权配置无效，请联系管理员",
            503,
        ) from None


def update_delegation_policy(
    database: Session,
    *,
    corp_id: str,
    proposed: DelegationPolicy,
    expected_revision: str,
) -> DelegationPolicy:
    database.rollback()
    database.connection().exec_driver_sql("BEGIN IMMEDIATE")
    try:
        current = get_delegation_policy(database, corp_id)
        if current == proposed:
            database.rollback()
            return current
        if current.revision != expected_revision:
            raise ApiError(
                "SETTINGS_REVISION_CONFLICT",
                "代报授权已更新，请重新加载核对后再保存",
                409,
            )
        database.merge(Setting(key=_policy_key(corp_id), value=proposed.serialize()))
        database.commit()
    except Exception:
        database.rollback()
        raise
    return proposed


def delegation_forbidden() -> ApiError:
    return ApiError("REIMBURSEMENT_DELEGATION_FORBIDDEN", "当前登录人没有代报权限", 403)


def require_delegation_permission_for_session(
    database: Session, session: UserSession, department_ids: tuple[str, ...]
) -> None:
    policy = get_delegation_policy(database, session.corp_id)
    if not policy.permits(session.dingtalk_user_id, department_ids):
        raise delegation_forbidden()
