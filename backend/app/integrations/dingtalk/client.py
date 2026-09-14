from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import ApiError

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_SAFE_UPSTREAM_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_TOKEN_INVALID_CODES = frozenset({40014, 42001})
_OPENAPI_PERMISSION_CODES = frozenset(
    {
        "Forbidden.AccessDenied.AccessTokenPermissionDenied",
        "Forbidden.AccessDenied.PermissionDenied",
        "Forbidden.Private",
        "noPermission",
    }
)
_OPENAPI_RATE_LIMIT_CODES = frozenset(
    {
        "forbidden.accessdenied.qpslimitforappkeyandapi",
        "throttling.ratelimit",
    }
)
_OPENAPI_OPERATIONS = {
    ("GET", "/v1.0/workflow/forms/schemas/processCodes"): "workflow_form_schema_read",
    (
        "POST",
        "/v1.0/workflow/processes/instanceIds/query",
    ): "workflow_instance_list",
    ("GET", "/v1.0/workflow/processInstances"): "workflow_instance_read",
    ("POST", "/v1.0/workflow/processInstances"): "workflow_instance_create",
    (
        "POST",
        "/v1.0/workflow/processInstances/spaces/infos/query",
    ): "workflow_approval_space_read",
}
_STORAGE_UPLOAD_INFO_PATH = re.compile(r"/v1\.0/storage/spaces/[^/]+/files/uploadInfos/query")
_STORAGE_COMMIT_PATH = re.compile(r"/v1\.0/storage/spaces/[^/]+/files/commit")
_STORAGE_DENTRY_QUERY_PATH = re.compile(r"/v1\.0/storage/spaces/[^/]+/dentries/[^/]+/query")
_STORAGE_DENTRY_PATH = re.compile(r"/v1\.0/storage/spaces/[^/]+/dentries/[^/]+")

logger = logging.getLogger(__name__)


class DingTalkOpenAPIError(ApiError):
    """Sanitized DingTalk failure with machine-readable upstream classification."""

    __slots__ = ("http_status", "upstream_code")

    def __init__(
        self,
        *,
        http_status: int | None,
        upstream_code: object = None,
    ) -> None:
        self.http_status = (
            http_status
            if isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
            else None
        )
        self.upstream_code = _sanitized_upstream_code(upstream_code)
        super().__init__(
            "DINGTALK_OPENAPI_FAILED",
            "钉钉服务暂时不可用，请稍后重试",
            502,
        )

    @classmethod
    def _permission_denied(
        cls,
        *,
        http_status: int | None,
        upstream_code: object = None,
    ) -> DingTalkOpenAPIError:
        error = cls(http_status=http_status, upstream_code=upstream_code)
        error.code = "DINGTALK_PERMISSION_MISSING"
        error.message = "钉钉应用权限不足，请联系管理员"
        return error

    @classmethod
    def _rate_limited(
        cls,
        *,
        http_status: int | None,
        upstream_code: object = None,
    ) -> DingTalkOpenAPIError:
        error = cls(http_status=http_status, upstream_code=upstream_code)
        error.code = "DINGTALK_RATE_LIMITED"
        error.message = "钉钉接口请求繁忙，请稍后重试"
        error.status_code = 503
        return error


class _RequestPacer:
    """Space request starts across every caller sharing one application client."""

    def __init__(self, minimum_interval_seconds: float) -> None:
        self._minimum_interval_seconds = minimum_interval_seconds
        self._lock = asyncio.Lock()
        self._next_start_at = 0.0

    async def wait(self) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                delay = self._next_start_at - now
                if delay <= 0:
                    self._next_start_at = now + self._minimum_interval_seconds
                    return
            await asyncio.sleep(delay)

    async def defer(self, delay_seconds: float) -> None:
        async with self._lock:
            self._next_start_at = max(
                self._next_start_at,
                monotonic() + delay_seconds,
            )


class DingTalkOpenAPIClient:
    """Shared organization-app token and HTTP boundary for DingTalk APIs."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            transport=transport,
        )
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._workflow_instance_read_pacer = _RequestPacer(
            settings.dingtalk_workflow_instance_read_min_interval_seconds
        )

    async def close(self) -> None:
        await self._http.aclose()

    def evict_token(self) -> None:
        self._access_token = None
        self._access_token_expires_at = 0.0

    async def request_openapi_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_invalid_token: bool = True,
        retry_transient: bool | None = None,
    ) -> dict[str, Any]:
        _validate_path(path)
        operation = _openapi_operation(method, path)
        pacer = (
            self._workflow_instance_read_pacer if operation == "workflow_instance_read" else None
        )
        for token_attempt in range(2):
            token = await self._get_access_token()
            error: DingTalkOpenAPIError | None = None
            try:
                payload = await self._request_json(
                    method,
                    f"https://api.dingtalk.com{path}",
                    params=params,
                    json=json,
                    headers={"x-acs-dingtalk-access-token": token},
                    retry_transient=_should_retry(method, retry_transient),
                    before_attempt=pacer.wait if pacer is not None else None,
                    on_rate_limit=pacer.defer if pacer is not None else None,
                )
            except DingTalkOpenAPIError as exc:
                error = exc

            if error is not None:
                if error.http_status == 401:
                    self.evict_token()
                    if retry_invalid_token and token_attempt == 0:
                        continue
                if _is_rate_limit_error(error):
                    error = DingTalkOpenAPIError._rate_limited(
                        http_status=error.http_status,
                        upstream_code=error.upstream_code,
                    )
                elif error.http_status == 403 and _is_permission_code(error.upstream_code):
                    error = DingTalkOpenAPIError._permission_denied(
                        http_status=error.http_status,
                        upstream_code=error.upstream_code,
                    )
                _log_openapi_failure(method, path, error)
                raise error

            upstream_code = _sanitized_upstream_code(_payload_code(payload))
            if _is_permission_code(upstream_code):
                error = DingTalkOpenAPIError._permission_denied(
                    http_status=200,
                    upstream_code=upstream_code,
                )
                _log_openapi_failure(method, path, error)
                raise error
            return payload
        error = DingTalkOpenAPIError(http_status=401)
        _log_openapi_failure(method, path, error)
        raise error

    async def request_oapi_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_invalid_token: bool = True,
        retry_transient: bool | None = None,
    ) -> dict[str, Any]:
        """Call a legacy OAPI endpoint through the same token and HTTP policy."""

        _validate_path(path)
        for token_attempt in range(2):
            token = await self._get_access_token()
            request_params = dict(params or {})
            request_params["access_token"] = token
            error: DingTalkOpenAPIError | None = None
            try:
                payload = await self._request_json(
                    method,
                    f"https://oapi.dingtalk.com{path}",
                    params=request_params,
                    json=json,
                    retry_transient=_should_retry(method, retry_transient),
                    retry_oapi_error=True,
                )
            except DingTalkOpenAPIError as exc:
                error = exc

            if error is not None:
                if error.http_status == 401:
                    self.evict_token()
                    if retry_invalid_token and token_attempt == 0:
                        continue
                raise error

            error_code = _integer_error_code(payload)
            if retry_invalid_token and error_code in _TOKEN_INVALID_CODES and token_attempt == 0:
                self.evict_token()
                continue
            return payload
        raise DingTalkOpenAPIError(http_status=401)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry_transient: bool,
        retry_oapi_error: bool = False,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
        on_rate_limit: Callable[[float], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        attempts = 3 if retry_transient else 1
        error: DingTalkOpenAPIError | None = None
        for attempt in range(attempts):
            if before_attempt is not None:
                await before_attempt()
            response: httpx.Response | None = None
            try:
                response = await self._http.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                error = DingTalkOpenAPIError(http_status=None)

            if response is not None:
                if not 200 <= response.status_code < 300:
                    response_error = _response_error(response)
                    if (
                        response.status_code == 429
                        or response.status_code >= 500
                        or _is_rate_limit_code(response_error.upstream_code)
                    ):
                        error = response_error
                    else:
                        raise response_error
                else:
                    payload = _response_json(response)
                    upstream_code = _sanitized_upstream_code(_payload_code(payload))
                    if _is_rate_limit_code(upstream_code):
                        error = DingTalkOpenAPIError(
                            http_status=response.status_code,
                            upstream_code=upstream_code,
                        )
                    elif retry_oapi_error and _integer_error_code(payload) == -1:
                        error = DingTalkOpenAPIError(
                            http_status=response.status_code,
                            upstream_code=-1,
                        )
                    else:
                        return payload

            is_rate_limit = _is_rate_limit_error(error)
            retry_base_seconds = 0.5 if is_rate_limit else 0.2
            retry_delay_seconds = retry_base_seconds * (2**attempt)
            if is_rate_limit and on_rate_limit is not None:
                await on_rate_limit(retry_delay_seconds)
            if attempt + 1 < attempts:
                await asyncio.sleep(retry_delay_seconds)
                continue
            if error is not None:
                raise error

        raise DingTalkOpenAPIError(http_status=None)

    async def _get_access_token(self) -> str:
        if self._access_token and monotonic() < self._access_token_expires_at:
            return self._access_token

        async with self._token_lock:
            if self._access_token and monotonic() < self._access_token_expires_at:
                return self._access_token
            payload = await self._request_json(
                "POST",
                f"https://api.dingtalk.com/v1.0/oauth2/{self._settings.dingtalk_corp_id}/token",
                json={
                    "client_id": self._settings.dingtalk_client_id,
                    "client_secret": self._settings.dingtalk_client_secret,
                    "grant_type": "client_credentials",
                },
                retry_transient=True,
            )
            access_token = str(
                payload.get("access_token") or payload.get("accessToken") or ""
            ).strip()
            if not access_token:
                raise DingTalkOpenAPIError(
                    http_status=200,
                    upstream_code=_payload_code(payload),
                )
            expires_in = payload.get("expires_in", payload.get("expireIn", 7200))
            try:
                cache_seconds = max(1, int(expires_in) - 300)
            except (TypeError, ValueError):
                cache_seconds = 6900
            self._access_token = access_token
            self._access_token_expires_at = monotonic() + cache_seconds
            return access_token


def _response_json(response: httpx.Response) -> dict[str, Any]:
    invalid_json = False
    try:
        payload = response.json()
    except ValueError:
        invalid_json = True
        payload = None
    if invalid_json or not isinstance(payload, dict):
        raise DingTalkOpenAPIError(http_status=response.status_code)
    return payload


def _response_error(response: httpx.Response) -> DingTalkOpenAPIError:
    return DingTalkOpenAPIError(
        http_status=response.status_code,
        upstream_code=_response_code(response),
    )


def _response_code(response: httpx.Response) -> object:
    try:
        payload = response.json()
    except ValueError:
        return None
    return _payload_code(payload) if isinstance(payload, dict) else None


def _payload_code(payload: dict[str, Any]) -> object:
    return payload.get("code", payload.get("errcode"))


def _integer_error_code(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("errcode", 0))
    except (TypeError, ValueError):
        return -2


def _should_retry(method: str, retry_transient: bool | None) -> bool:
    if retry_transient is not None:
        return retry_transient
    return method.upper() in _IDEMPOTENT_METHODS


def _validate_path(path: str) -> None:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("DingTalk API paths must be absolute paths")


def _is_permission_code(code: str | None) -> bool:
    return bool(code and (code in _OPENAPI_PERMISSION_CODES or "PermissionDenied" in code))


def _is_rate_limit_code(code: str | None) -> bool:
    return bool(code and code.casefold() in _OPENAPI_RATE_LIMIT_CODES)


def _is_rate_limit_error(error: DingTalkOpenAPIError | None) -> bool:
    return bool(
        error is not None and (error.http_status == 429 or _is_rate_limit_code(error.upstream_code))
    )


def _log_openapi_failure(
    method: str,
    path: str,
    error: DingTalkOpenAPIError,
) -> None:
    logger.warning(
        "DingTalk OpenAPI request failed",
        extra={
            "error_code": error.code,
            "upstream": "dingtalk",
            "upstream_api": "openapi",
            "upstream_operation": _openapi_operation(method, path),
            "upstream_http_status": error.http_status,
            "upstream_error_code": error.upstream_code,
        },
    )


def _openapi_operation(method: str, path: str) -> str:
    normalized_method = method.upper()
    exact = _OPENAPI_OPERATIONS.get((normalized_method, path))
    if exact is not None:
        return exact
    if normalized_method == "POST" and _STORAGE_UPLOAD_INFO_PATH.fullmatch(path):
        return "storage_upload_info_read"
    if normalized_method == "POST" and _STORAGE_COMMIT_PATH.fullmatch(path):
        return "storage_file_commit"
    if normalized_method == "POST" and _STORAGE_DENTRY_QUERY_PATH.fullmatch(path):
        return "storage_file_probe"
    if normalized_method == "DELETE" and _STORAGE_DENTRY_PATH.fullmatch(path):
        return "storage_file_recycle"
    return "openapi_request"


def _sanitized_upstream_code(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    code = str(value).strip()
    return code if _SAFE_UPSTREAM_CODE.fullmatch(code) else None
