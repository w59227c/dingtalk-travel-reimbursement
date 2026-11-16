from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx

from app.core.config import Settings
from app.core.errors import ApiError

if TYPE_CHECKING:
    from app.integrations.dingtalk.client import DingTalkOpenAPIClient

PERMISSION_ERROR_CODES = {88, 43007, 60011, 60020}
_EMPLOYEE_NOT_FOUND = {60111, 60121}
_EMPLOYEE_SEARCH_SIZE = 8
_IDENTITY_CACHE_SECONDS = 30
_IDENTITY_CACHE_SIZE = 128


@dataclass(frozen=True, slots=True)
class DepartmentIdentity:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class DingTalkIdentity:
    user_id: str
    union_id: str
    name: str
    departments: tuple[DepartmentIdentity, ...]


class DingTalkService:
    """Identity facade over the shared DingTalk organization-app client."""

    def __init__(
        self,
        client: DingTalkOpenAPIClient | Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        from app.integrations.dingtalk.client import DingTalkOpenAPIClient

        if isinstance(client, Settings):
            client = DingTalkOpenAPIClient(client, transport=transport)
        elif transport is not None:
            raise ValueError("transport belongs to DingTalkOpenAPIClient")
        self._client = client
        self._employee_identities: OrderedDict[str, tuple[float, DingTalkIdentity]] = OrderedDict()

    async def close(self) -> None:
        await self._client.close()

    def evict_token(self) -> None:
        self._client.evict_token()

    async def get_identity(self, auth_code: str) -> DingTalkIdentity:
        from app.integrations.dingtalk.client import DingTalkOpenAPIError

        try:
            return await self._get_identity(auth_code)
        except DingTalkOpenAPIError:
            raise self._safe_error() from None
        except ApiError as exc:
            if exc.code == "REIMBURSEMENT_EMPLOYEE_NOT_FOUND":
                raise self._safe_error() from None
            raise

    async def get_department_identity(self, department_id: str) -> DepartmentIdentity:
        """Resolve the canonical DingTalk department selected by an OA instance."""

        from app.integrations.dingtalk.client import DingTalkOpenAPIError

        normalized_id = str(department_id).strip()
        if not normalized_id:
            raise ApiError("TRAVEL_APPROVAL_DEPARTMENT_INVALID", "出差审批缺少所在部门", 422)
        raw_id: int | str = int(normalized_id) if normalized_id.isdecimal() else normalized_id
        try:
            department = await self._oapi(
                "/topapi/v2/department/get",
                {"dept_id": raw_id, "language": "zh_CN"},
                retry_invalid_token=True,
                retry_transient=True,
            )
        except DingTalkOpenAPIError:
            raise ApiError(
                "TRAVEL_APPROVAL_DEPARTMENT_LOOKUP_FAILED",
                "无法读取出差审批的所在部门，请稍后重试",
                502,
            ) from None
        name = str(department.get("name") or "").strip()
        if not name:
            raise ApiError(
                "TRAVEL_APPROVAL_DEPARTMENT_INVALID",
                "出差审批的所在部门已失效，请联系管理员",
                422,
            )
        return DepartmentIdentity(normalized_id, name)

    async def _get_identity(self, auth_code: str) -> DingTalkIdentity:
        user_info = await self._oapi(
            "/topapi/v2/user/getuserinfo",
            {"code": auth_code},
            retry_invalid_token=True,
            retry_transient=False,
        )
        user_id = str(user_info.get("userid") or "").strip()
        if not user_id:
            raise self._safe_error()
        return await self._identity_from_user(user_id, await self._get_user(user_id))

    async def _get_user(self, user_id: str) -> dict[str, Any]:
        return await self._oapi(
            "/topapi/v2/user/get",
            {"userid": user_id, "language": "zh_CN"},
            retry_invalid_token=True,
            retry_transient=True,
        )

    async def employee_directory_entry(self, user_id: str) -> dict[str, str] | None:
        try:
            user = await self._get_user(user_id)
        except ApiError as exc:
            if exc.code == "REIMBURSEMENT_EMPLOYEE_NOT_FOUND":
                return None
            raise
        name = user.get("name")
        if user.get("userid") == user_id and isinstance(name, str) and name.strip():
            return {"userId": user_id, "name": name.strip()}
        return None

    async def list_child_departments(self, parent_id: int) -> tuple[DepartmentIdentity, ...]:
        from app.integrations.dingtalk.client import DingTalkOpenAPIError

        try:
            payload = await self._oapi(
                "/topapi/v2/department/listsub",
                {"dept_id": parent_id, "language": "zh_CN"},
                retry_invalid_token=True,
                retry_transient=True,
            )
        except DingTalkOpenAPIError:
            raise ApiError("DIRECTORY_LOOKUP_FAILED", "暂时无法读取钉钉部门，请重试", 502) from None
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise ApiError("DIRECTORY_LOOKUP_FAILED", "钉钉部门列表格式异常，请重试", 502)
        departments: dict[str, DepartmentIdentity] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            department_id = str(row.get("dept_id", ""))
            name = row.get("name")
            if (
                department_id.isascii() and department_id.isdecimal()
                and department_id != str(parent_id)
                and isinstance(name, str) and name.strip()
            ):
                departments[department_id] = DepartmentIdentity(department_id, name.strip())
        return tuple(departments.values())

    async def get_employee_identity(self, user_id: str, *, fresh: bool = False) -> DingTalkIdentity:
        from app.integrations.dingtalk.client import DingTalkOpenAPIError

        cached = self._employee_identities.get(user_id)
        if not fresh and cached is not None and cached[0] > monotonic():
            self._employee_identities.move_to_end(user_id)
            return cached[1]
        try:
            user = await self._get_user(user_id)
            if user.get("userid") != user_id:
                raise self._employee_not_found()
            identity = await self._identity_from_user(user_id, user)
        except DingTalkOpenAPIError as exc:
            raise self._employee_lookup_failed() from exc
        self._employee_identities[user_id] = (monotonic() + _IDENTITY_CACHE_SECONDS, identity)
        self._employee_identities.move_to_end(user_id)
        if len(self._employee_identities) > _IDENTITY_CACHE_SIZE:
            self._employee_identities.popitem(last=False)
        return identity

    async def search_employee_entries(self, query: str) -> list[dict[str, str]]:
        from app.integrations.dingtalk.client import DingTalkOpenAPIError
        from app.services.reimbursement_delegation_policy import normalize_directory_id

        try:
            try:
                exact_id = normalize_directory_id(query)
            except ValueError:
                exact_id = None
            if exact_id:
                exact = await self.employee_directory_entry(exact_id)
                if exact:
                    return [exact]
            payload = await self._client.request_openapi_json(
                "POST",
                "/v1.0/contact/users/search",
                json={"queryWord": query, "offset": 0, "size": _EMPLOYEE_SEARCH_SIZE},
                retry_transient=True,
            )
            ids = payload.get("list")
            if ids is None and payload.get("totalCount") == 0:
                return []
            if not isinstance(ids, list):
                raise self._employee_lookup_failed()
            user_ids = tuple(
                dict.fromkeys(
                    normalize_directory_id(item)
                    for item in ids[:_EMPLOYEE_SEARCH_SIZE]
                    if isinstance(item, str)
                )
            )
            entries = await asyncio.gather(
                *(self.employee_directory_entry(user_id) for user_id in user_ids)
            )
            return [item for item in entries if item is not None]
        except DingTalkOpenAPIError as exc:
            if exc.code == "DINGTALK_PERMISSION_MISSING":
                raise ApiError(
                    "REIMBURSEMENT_EMPLOYEE_SEARCH_UNAVAILABLE",
                    "钉钉应用未授权姓名搜索，请输入准确的员工 userId 或联系管理员",
                    502,
                ) from None
            raise self._employee_lookup_failed() from exc
        except ValueError:
            raise self._employee_lookup_failed() from None

    async def _identity_from_user(
        self, user_id: str, user: dict[str, Any]
    ) -> DingTalkIdentity:
        name = str(user.get("name") or "").strip()
        union_id = str(user.get("unionid") or "").strip()
        raw_department_ids = user.get("dept_id_list")
        if not name or not union_id or not isinstance(raw_department_ids, list):
            raise self._safe_error()

        departments: list[DepartmentIdentity] = []
        seen: set[str] = set()
        for raw_id in raw_department_ids:
            department_id = str(raw_id).strip()
            if not department_id or department_id in seen:
                continue
            seen.add(department_id)
            department = await self._oapi(
                "/topapi/v2/department/get",
                {"dept_id": raw_id, "language": "zh_CN"},
                retry_invalid_token=True,
                retry_transient=True,
            )
            department_name = str(department.get("name") or "").strip()
            if department_name:
                departments.append(DepartmentIdentity(department_id, department_name))

        if not departments:
            raise ApiError(
                "DINGTALK_PERMISSION_MISSING",
                "无法读取所属部门，请联系管理员检查钉钉应用权限",
                502,
            )
        return DingTalkIdentity(
            user_id=user_id,
            union_id=union_id,
            name=name,
            departments=tuple(departments),
        )

    async def request_openapi_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_transient: bool | None = None,
    ) -> dict[str, Any]:
        return await self._client.request_openapi_json(
            method,
            path,
            params=params,
            json=json,
            retry_transient=retry_transient,
        )

    async def _oapi(
        self,
        path: str,
        body: dict[str, Any],
        *,
        retry_invalid_token: bool,
        retry_transient: bool,
    ) -> dict[str, Any]:
        payload = await self._client.request_oapi_json(
            "POST",
            path,
            json=body,
            retry_invalid_token=retry_invalid_token,
            retry_transient=retry_transient,
        )
        errcode = self._error_code(payload)
        if errcode == 0:
            result = payload.get("result")
            return result if isinstance(result, dict) else payload
        if errcode in PERMISSION_ERROR_CODES:
            raise ApiError(
                "DINGTALK_PERMISSION_MISSING",
                "钉钉应用权限不足，请联系管理员",
                502,
            )
        if path == "/topapi/v2/user/get" and errcode in _EMPLOYEE_NOT_FOUND:
            raise self._employee_not_found()
        raise self._safe_error()

    @staticmethod
    def _employee_not_found() -> ApiError:
        return ApiError("REIMBURSEMENT_EMPLOYEE_NOT_FOUND", "未找到可办理报销的员工", 404)

    @staticmethod
    def _employee_lookup_failed() -> ApiError:
        return ApiError("REIMBURSEMENT_EMPLOYEE_LOOKUP_FAILED", "暂时无法读取钉钉员工，请重试", 502)

    @staticmethod
    def _error_code(payload: dict[str, Any]) -> int:
        try:
            return int(payload.get("errcode", 0))
        except (TypeError, ValueError):
            return -2

    @staticmethod
    def _safe_error() -> ApiError:
        return ApiError(
            "DINGTALK_AUTH_FAILED",
            "钉钉身份验证失败，请从公司钉钉工作台重新进入",
            502,
        )
