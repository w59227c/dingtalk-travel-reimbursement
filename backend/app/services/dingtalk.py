from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from app.core.config import Settings
from app.core.errors import ApiError

if TYPE_CHECKING:
    from app.integrations.dingtalk.client import DingTalkOpenAPIClient

PERMISSION_ERROR_CODES = {88, 43007, 60011, 60020}


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

        user = await self._oapi(
            "/topapi/v2/user/get",
            {"userid": user_id, "language": "zh_CN"},
            retry_invalid_token=True,
            retry_transient=True,
        )
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
        raise self._safe_error()

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
