from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from http.cookiejar import DefaultCookiePolicy
from time import monotonic
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIClient, DingTalkOpenAPIError

_APPROVAL_SPACE_PATH = "/v1.0/workflow/processInstances/spaces/infos/query"
_SAFE_ID = re.compile(r"[A-Za-z0-9._:~-]{1,256}")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_FILE_TYPE = re.compile(r"[a-z0-9]{1,16}")
_RETRYABLE_UPLOAD_STATUSES = frozenset({408, 425, 429})
_DENTRY_NOT_FOUND_CODES = frozenset({"dentrynotexist"})
_MISSING_DENTRY_STATUSES = frozenset({"DELETED"})
_FORBIDDEN_SIGNED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "set-cookie",
        "transfer-encoding",
        "x-acs-dingtalk-access-token",
    }
)
_ACCEPTED_FILE_TYPES = frozenset({"jpeg", "jpg", "pdf", "png", "xlsx"})


class DingTalkStorageCommitOutcomeUnknown(ApiError):
    """A commit may have created a dentry and must not be replayed or cleaned.

    Cancellation after the commit request starts has the same unknown outcome. The
    worker must persist COMMITTING before entering that boundary and recover an
    abandoned COMMITTING attempt as COMMIT_UNCERTAIN rather than replaying it.
    """

    __slots__ = ("http_status", "possible_file_id", "possible_space_id")

    def __init__(
        self,
        *,
        http_status: int | None,
        possible_space_id: str | None = None,
        possible_file_id: str | None = None,
    ) -> None:
        self.http_status = _safe_http_status(http_status)
        self.possible_space_id = possible_space_id
        self.possible_file_id = possible_file_id
        super().__init__(
            "DINGTALK_STORAGE_COMMIT_UNKNOWN",
            "附件提交结果暂时无法确认，请勿重复提交",
            502,
        )


class DingTalkStorageRecycleOutcomeUnknown(ApiError):
    """A recycle request may have applied, but its final state cannot be read."""

    __slots__ = ("http_status",)

    def __init__(self, *, http_status: int | None) -> None:
        self.http_status = _safe_http_status(http_status)
        super().__init__(
            "DINGTALK_STORAGE_CLEANUP_UNKNOWN",
            "附件清理结果暂时无法确认",
            502,
        )


class DingTalkStorageUploadError(ApiError):
    """Sanitized failure from the external signed-upload boundary."""

    __slots__ = ("http_status",)

    def __init__(self, *, http_status: int | None) -> None:
        self.http_status = _safe_http_status(http_status)
        super().__init__(
            "DINGTALK_STORAGE_UPLOAD_FAILED",
            "附件上传失败，请稍后重试",
            502,
        )


class DingTalkStorageUploadStateError(RuntimeError):
    """A process-local upload handle was reused or passed to another client."""


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalFile:
    file_name: str
    file_type: str
    content: bytes = field(repr=False)
    size: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise _input_error("附件内容不能为空")
        file_name = _validated_file_name(self.file_name)
        file_type = _normalized_file_type(self.file_type)
        if not _matching_extension(file_name, file_type):
            raise _input_error("附件名称与文件类型不一致")
        object.__setattr__(self, "file_name", file_name)
        object.__setattr__(self, "file_type", file_type)
        object.__setattr__(self, "size", len(self.content))
        object.__setattr__(self, "sha256", hashlib.sha256(self.content).hexdigest())


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalSpace:
    space_id: str


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalAttachment:
    space_id: str
    file_id: str
    file_name: str
    file_size: int
    file_type: str

    def as_oa_value(self) -> dict[str, str | int]:
        return {
            "spaceId": self.space_id,
            "fileId": self.file_id,
            "fileName": self.file_name,
            "fileSize": self.file_size,
            "fileType": self.file_type,
        }


class AttachmentProbe(StrEnum):
    EXACT = "EXACT"
    MISSING = "MISSING"
    MISMATCH = "MISMATCH"


class AttachmentRecycleOutcome(StrEnum):
    CLEANED = "CLEANED"
    RETRY_LATER = "RETRY_LATER"


@dataclass(frozen=True, slots=True, repr=False)
class _UploadTicket:
    upload_key: str = field(repr=False)
    resource_url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    expires_at: float


class _UploadTicketExpired(RuntimeError):
    pass


class _PreparedUploadStage(StrEnum):
    PREPARED = "PREPARED"
    PUTTING = "PUTTING"
    PUT_COMPLETE = "PUT_COMPLETE"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    COMMIT_REJECTED = "COMMIT_REJECTED"
    COMMIT_UNKNOWN = "COMMIT_UNKNOWN"
    FAILED = "FAILED"


@dataclass(slots=True, repr=False)
class _PreparedUploadLifecycle:
    ticket: _UploadTicket | None = field(repr=False)
    stage: _PreparedUploadStage = field(
        default=_PreparedUploadStage.PREPARED,
        repr=False,
    )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PreparedApprovalUpload:
    """Opaque, process-local handle for one exact approval attachment upload.

    The signed URL, headers, and upload key deliberately live only in this
    short-lived handle. The handle binds one client, space, user, and immutable
    :class:`ApprovalFile`; callers persist their own non-secret checkpoint, not
    this object.
    """

    _owner: object = field(repr=False)
    _space_id: str = field(repr=False)
    _union_id: str = field(repr=False)
    _file: ApprovalFile = field(repr=False)
    _lifecycle: _PreparedUploadLifecycle = field(repr=False)


class _RejectAllCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


class DingTalkStorageClient:
    """Approval-space Storage facade with an isolated signed PUT boundary."""

    def __init__(
        self,
        client: DingTalkOpenAPIClient,
        settings: Settings,
        *,
        upload_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._upload_handle_owner = object()
        timeout_seconds = settings.dingtalk_storage_upload_timeout_seconds
        self._upload_http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=min(10.0, timeout_seconds),
                pool=min(10.0, timeout_seconds),
            ),
            transport=upload_transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._upload_http.cookies.jar.set_policy(_RejectAllCookies())

    async def close(self) -> None:
        await self._upload_http.aclose()

    async def get_approval_space(self, user_id: str) -> ApprovalSpace:
        user_id = _required_text(user_id, "员工身份无效")
        agent_id = self._settings.dingtalk_agent_id
        if agent_id is None:
            raise ApiError(
                "DINGTALK_AGENT_ID_MISSING",
                "钉钉应用 AgentId 尚未配置",
                503,
            )
        payload = await self._client.request_openapi_json(
            "POST",
            _APPROVAL_SPACE_PATH,
            json={"userId": user_id, "agentId": agent_id},
            retry_transient=True,
        )
        if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
            raise _protocol_error()
        space_id = _required_identifier(payload["result"].get("spaceId"))
        return ApprovalSpace(space_id=space_id)

    async def prepare_upload(
        self,
        *,
        space: ApprovalSpace,
        union_id: str,
        file: ApprovalFile,
    ) -> PreparedApprovalUpload:
        """Acquire a signed ticket for one exact file without sending its bytes."""

        if not isinstance(file, ApprovalFile):
            raise _input_error("附件内容无效")
        space_id = _required_identifier(space.space_id)
        union_id = _required_text(union_id, "员工存储身份无效")
        if file.size > self._settings.upload_max_file_bytes:
            raise _input_error("附件超过服务器允许的大小")
        ticket = await self._query_upload_info(
            space_id=space_id,
            union_id=union_id,
            file=file,
        )
        return PreparedApprovalUpload(
            _owner=self._upload_handle_owner,
            _space_id=space_id,
            _union_id=union_id,
            _file=file,
            _lifecycle=_PreparedUploadLifecycle(ticket=ticket),
        )

    async def put_prepared_upload(
        self,
        *,
        prepared: PreparedApprovalUpload,
    ) -> PreparedApprovalUpload:
        """Send bytes for a prepared upload, but never commit the remote dentry."""

        self._require_upload_stage(prepared, _PreparedUploadStage.PREPARED)
        prepared._lifecycle.stage = _PreparedUploadStage.PUTTING
        for ticket_attempt in range(2):
            ticket = prepared._lifecycle.ticket
            assert ticket is not None
            try:
                await self._put_signed(ticket, prepared._file.content)
            except _UploadTicketExpired:
                if ticket_attempt == 0:
                    try:
                        prepared._lifecycle.ticket = await self._query_upload_info(
                            space_id=prepared._space_id,
                            union_id=prepared._union_id,
                            file=prepared._file,
                        )
                    except BaseException:
                        prepared._lifecycle.stage = _PreparedUploadStage.FAILED
                        prepared._lifecycle.ticket = None
                        raise
                    continue
                prepared._lifecycle.stage = _PreparedUploadStage.FAILED
                prepared._lifecycle.ticket = None
                raise DingTalkStorageUploadError(http_status=None) from None
            except BaseException:
                prepared._lifecycle.stage = _PreparedUploadStage.FAILED
                prepared._lifecycle.ticket = None
                raise
            prepared._lifecycle.stage = _PreparedUploadStage.PUT_COMPLETE
            return prepared
        prepared._lifecycle.stage = _PreparedUploadStage.FAILED
        raise DingTalkStorageUploadError(http_status=None)

    async def commit_prepared_upload(
        self,
        *,
        prepared: PreparedApprovalUpload,
    ) -> ApprovalAttachment:
        """Commit exactly once after the caller has persisted ``COMMITTING``.

        Once this method starts, cancellation or any non-definitive failure means
        the outcome is unknown. The handle becomes terminal before the first await,
        so neither concurrent nor later calls can replay the commit.
        """

        self._require_upload_stage(prepared, _PreparedUploadStage.PUT_COMPLETE)
        prepared._lifecycle.stage = _PreparedUploadStage.COMMITTING
        ticket = prepared._lifecycle.ticket
        assert ticket is not None
        prepared._lifecycle.ticket = None
        try:
            attachment = await self._commit_file(
                space_id=prepared._space_id,
                union_id=prepared._union_id,
                file=prepared._file,
                upload_key=ticket.upload_key,
            )
        except asyncio.CancelledError:
            prepared._lifecycle.stage = _PreparedUploadStage.COMMIT_UNKNOWN
            raise
        except DingTalkStorageCommitOutcomeUnknown:
            prepared._lifecycle.stage = _PreparedUploadStage.COMMIT_UNKNOWN
            raise
        except ApiError as exc:
            if exc.code == "DINGTALK_STORAGE_COMMIT_REJECTED":
                prepared._lifecycle.stage = _PreparedUploadStage.COMMIT_REJECTED
                raise
            prepared._lifecycle.stage = _PreparedUploadStage.COMMIT_UNKNOWN
            raise DingTalkStorageCommitOutcomeUnknown(http_status=None) from None
        except Exception:
            prepared._lifecycle.stage = _PreparedUploadStage.COMMIT_UNKNOWN
            raise DingTalkStorageCommitOutcomeUnknown(http_status=None) from None
        prepared._lifecycle.stage = _PreparedUploadStage.COMMITTED
        return attachment

    def _require_upload_stage(
        self,
        prepared: PreparedApprovalUpload,
        expected: _PreparedUploadStage,
    ) -> None:
        if (
            not isinstance(prepared, PreparedApprovalUpload)
            or prepared._owner is not self._upload_handle_owner
            or prepared._lifecycle.stage is not expected
            or prepared._lifecycle.ticket is None
        ):
            raise _upload_stage_error()

    async def probe_attachment(
        self,
        *,
        union_id: str,
        expected: ApprovalAttachment,
    ) -> AttachmentProbe:
        union_id = _required_text(union_id, "员工存储身份无效")
        space_id = _required_identifier(expected.space_id)
        file_id = _required_identifier(expected.file_id)
        error: DingTalkOpenAPIError | None = None
        payload: dict[str, object] | None = None
        try:
            payload = await self._client.request_openapi_json(
                "POST",
                f"/v1.0/storage/spaces/{_path_segment(space_id)}"
                f"/dentries/{_path_segment(file_id)}/query",
                params={"unionId": union_id},
                json={},
                retry_transient=True,
            )
        except DingTalkOpenAPIError as exc:
            error = exc
        if error is not None:
            if _is_explicit_dentry_not_found(error):
                return AttachmentProbe.MISSING
            raise error
        assert payload is not None
        return _probe_payload(payload, expected)

    async def recycle_attachment(
        self,
        *,
        union_id: str,
        attachment: ApprovalAttachment,
    ) -> AttachmentRecycleOutcome:
        """Recycle one exact remote file after external state authorization.

        This is a raw mutation primitive, not an authorization boundary. Only the
        persisted reimbursement state service may call it after a CAS/lease proves
        the upload is an orphan, is not LINKED, and has no unknown OA outcome.
        """

        initial_probe = await self.probe_attachment(
            union_id=union_id,
            expected=attachment,
        )
        if initial_probe is AttachmentProbe.MISSING:
            return AttachmentRecycleOutcome.CLEANED
        if initial_probe is AttachmentProbe.MISMATCH:
            raise _mismatch_error()

        delete_error: DingTalkOpenAPIError | None = None
        try:
            await self._client.request_openapi_json(
                "DELETE",
                f"/v1.0/storage/spaces/{_path_segment(attachment.space_id)}"
                f"/dentries/{_path_segment(attachment.file_id)}",
                params={"unionId": union_id, "toRecycleBin": "true"},
                retry_invalid_token=False,
                retry_transient=False,
            )
        except DingTalkOpenAPIError as exc:
            delete_error = exc

        if delete_error is not None and _is_definitive_cleanup_rejection(delete_error):
            raise delete_error

        verification_error: ApiError | None = None
        final_probe: AttachmentProbe | None = None
        try:
            final_probe = await self.probe_attachment(
                union_id=union_id,
                expected=attachment,
            )
        except ApiError as exc:
            verification_error = exc
        if verification_error is not None:
            raise DingTalkStorageRecycleOutcomeUnknown(
                http_status=getattr(verification_error, "http_status", None)
            )
        if final_probe is AttachmentProbe.MISSING:
            return AttachmentRecycleOutcome.CLEANED
        if final_probe is AttachmentProbe.MISMATCH:
            raise _mismatch_error()
        return AttachmentRecycleOutcome.RETRY_LATER

    async def _query_upload_info(
        self,
        *,
        space_id: str,
        union_id: str,
        file: ApprovalFile,
    ) -> _UploadTicket:
        payload = await self._client.request_openapi_json(
            "POST",
            f"/v1.0/storage/spaces/{_path_segment(space_id)}/files/uploadInfos/query",
            params={"unionId": union_id},
            json={
                "protocol": "HEADER_SIGNATURE",
                "multipart": False,
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {
                        "name": file.file_name,
                        "size": file.size,
                        "parentId": "0",
                    },
                    "preferIntranet": False,
                },
            },
            retry_transient=True,
        )
        return _normalize_upload_ticket(
            payload,
            allowed_host_suffixes=self._settings.storage_upload_host_suffixes,
            now=self._clock(),
        )

    async def _put_signed(self, ticket: _UploadTicket, content: bytes) -> None:
        failure_status: int | None = None
        for attempt in range(3):
            if self._clock() >= ticket.expires_at - 2:
                raise _UploadTicketExpired
            response: httpx.Response | None = None
            local_serialization_failed = False
            try:
                response = await self._upload_http.request(
                    "PUT",
                    ticket.resource_url,
                    headers=dict(ticket.headers),
                    content=content,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                failure_status = None
            except (UnicodeError, ValueError):
                local_serialization_failed = True

            if local_serialization_failed:
                raise DingTalkStorageUploadError(http_status=None)

            if response is not None:
                if 200 <= response.status_code < 300:
                    return
                failure_status = response.status_code
                if not _retryable_upload_status(response.status_code):
                    raise DingTalkStorageUploadError(http_status=response.status_code)

            if attempt + 1 < 3:
                if self._clock() >= ticket.expires_at - 2:
                    raise _UploadTicketExpired
                await self._sleep(0.2 * (2**attempt))
                continue
            raise DingTalkStorageUploadError(http_status=failure_status)
        raise DingTalkStorageUploadError(http_status=failure_status)

    async def _commit_file(
        self,
        *,
        space_id: str,
        union_id: str,
        file: ApprovalFile,
        upload_key: str,
    ) -> ApprovalAttachment:
        error: DingTalkOpenAPIError | None = None
        payload: dict[str, object] | None = None
        try:
            # asyncio.CancelledError intentionally remains a cancellation signal.
            # It is nevertheless an unknown commit outcome: the persisted worker
            # must checkpoint COMMITTING before this await and must never replay a
            # stranded COMMITTING attempt after cancellation or process death.
            payload = await self._client.request_openapi_json(
                "POST",
                f"/v1.0/storage/spaces/{_path_segment(space_id)}/files/commit",
                params={"unionId": union_id},
                json={
                    "name": file.file_name,
                    "uploadKey": upload_key,
                    "parentId": "0",
                    "option": {
                        "size": file.size,
                        "conflictStrategy": "AUTO_RENAME",
                    },
                },
                retry_invalid_token=False,
                retry_transient=False,
            )
        except DingTalkOpenAPIError as exc:
            error = exc

        if error is not None:
            if _is_definitive_commit_rejection(error):
                raise ApiError(
                    "DINGTALK_STORAGE_COMMIT_REJECTED",
                    "钉钉拒绝提交附件，请检查应用权限或文件信息",
                    502,
                )
            raise DingTalkStorageCommitOutcomeUnknown(http_status=error.http_status)
        assert payload is not None
        return _normalize_committed_attachment(payload, expected_space_id=space_id, file=file)


def _normalize_upload_ticket(
    payload: dict[str, object],
    *,
    allowed_host_suffixes: tuple[str, ...],
    now: float,
) -> _UploadTicket:
    if payload.get("protocol") != "HEADER_SIGNATURE":
        raise _protocol_error()
    if payload.get("storageDriver") != "DINGTALK":
        raise _protocol_error()
    upload_key = _required_secret_text(payload.get("uploadKey"))
    signature = payload.get("headerSignatureInfo")
    if not isinstance(signature, dict):
        raise _protocol_error()
    expiration_seconds = signature.get("expirationSeconds")
    if (
        isinstance(expiration_seconds, bool)
        or not isinstance(expiration_seconds, int)
        or not 1 <= expiration_seconds <= 3600
    ):
        raise _protocol_error()
    resource_urls = signature.get("resourceUrls")
    if not isinstance(resource_urls, list) or not 1 <= len(resource_urls) <= 10:
        raise _protocol_error()
    resource_url = resource_urls[0]
    if not isinstance(resource_url, str):
        raise _protocol_error()
    resource_url = _validated_signed_url(resource_url, allowed_host_suffixes)
    headers = _validated_signature_headers(signature.get("headers"))
    return _UploadTicket(
        upload_key=upload_key,
        resource_url=resource_url,
        headers=headers,
        expires_at=now + expiration_seconds,
    )


def _normalize_committed_attachment(
    payload: dict[str, object],
    *,
    expected_space_id: str,
    file: ApprovalFile,
) -> ApprovalAttachment:
    dentry = payload.get("dentry")
    if not isinstance(dentry, dict):
        raise DingTalkStorageCommitOutcomeUnknown(http_status=200)

    possible_space_id = _possible_identifier(dentry.get("spaceId"))
    possible_file_id = _possible_identifier(dentry.get("id"))
    try:
        space_id = _required_identifier(dentry.get("spaceId"))
        file_id = _required_identifier(dentry.get("id"))
        file_name = _validated_file_name(dentry.get("name"))
        file_size = _required_size(dentry.get("size"))
        file_type = _normalized_file_type(dentry.get("extension"))
        dentry_type = _required_text(dentry.get("type"), "")
        status = _required_text(dentry.get("status"), "")
    except ApiError:
        raise DingTalkStorageCommitOutcomeUnknown(
            http_status=200,
            possible_space_id=possible_space_id,
            possible_file_id=possible_file_id,
        ) from None
    if (
        space_id != expected_space_id
        or file_size != file.size
        or dentry_type.upper() != "FILE"
        or status.upper() != "NORMAL"
        or not _matching_extension(file_name, file_type)
        or not _equivalent_file_type(file_type, file.file_type)
    ):
        raise DingTalkStorageCommitOutcomeUnknown(
            http_status=200,
            possible_space_id=space_id,
            possible_file_id=file_id,
        )
    return ApprovalAttachment(
        space_id=space_id,
        file_id=file_id,
        file_name=file_name,
        file_size=file_size,
        file_type=file_type,
    )


def _probe_payload(
    payload: dict[str, object],
    expected: ApprovalAttachment,
) -> AttachmentProbe:
    dentry = payload.get("dentry")
    if not isinstance(dentry, dict):
        raise _protocol_error()
    try:
        space_id = _required_identifier(dentry.get("spaceId"))
        file_id = _required_identifier(dentry.get("id"))
        status = _required_text(dentry.get("status"), "")
    except ApiError:
        raise _protocol_error() from None
    if space_id != expected.space_id or file_id != expected.file_id:
        return AttachmentProbe.MISMATCH
    if status.upper() in _MISSING_DENTRY_STATUSES:
        return AttachmentProbe.MISSING
    try:
        file_name = _validated_file_name(dentry.get("name"))
        file_size = _required_size(dentry.get("size"))
        file_type = _normalized_file_type(dentry.get("extension"))
        dentry_type = _required_text(dentry.get("type"), "")
    except ApiError:
        raise _protocol_error() from None
    if (
        status.upper() == "NORMAL"
        and dentry_type.upper() == "FILE"
        and file_name == expected.file_name
        and file_size == expected.file_size
        and _equivalent_file_type(file_type, expected.file_type)
    ):
        return AttachmentProbe.EXACT
    return AttachmentProbe.MISMATCH


def _validated_signed_url(url: str, allowed_host_suffixes: tuple[str, ...]) -> str:
    if not url or len(url) > 8192 or any(char in url for char in "\r\n\x00"):
        raise _protocol_error()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise _protocol_error() from None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not host
        or host.endswith(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or not parsed.path.startswith("/")
    ):
        raise _protocol_error()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _protocol_error()
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_host_suffixes):
        raise _protocol_error()
    return url


def _validated_signature_headers(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 64:
        raise _protocol_error()
    result: list[tuple[str, str]] = []
    normalized_names: set[str] = set()
    total_bytes = 0
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not _HEADER_NAME.fullmatch(raw_name):
            raise _protocol_error()
        normalized_name = raw_name.lower()
        if (
            normalized_name in normalized_names
            or normalized_name in _FORBIDDEN_SIGNED_HEADERS
            or not isinstance(raw_value, str)
        ):
            raise _protocol_error()
        if not raw_value or len(raw_value) > 8192 or not _is_safe_header_value(raw_value):
            raise _protocol_error()
        total_bytes += len(raw_name.encode("ascii")) + len(raw_value.encode("ascii"))
        if total_bytes > 32768:
            raise _protocol_error()
        normalized_names.add(normalized_name)
        result.append((raw_name, raw_value))
    return tuple(result)


def _validated_file_name(value: object) -> str:
    if not isinstance(value, str):
        raise _input_error("附件名称无效")
    name = unicodedata.normalize("NFC", value).strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(unicodedata.category(character) == "Cc" for character in name)
        or len(name) > 255
        or len(name.encode("utf-8")) > 255
    ):
        raise _input_error("附件名称无效")
    return name


def _normalized_file_type(value: object) -> str:
    if not isinstance(value, str):
        raise _input_error("附件文件类型无效")
    file_type = value.strip().lower().lstrip(".")
    if not _FILE_TYPE.fullmatch(file_type) or file_type not in _ACCEPTED_FILE_TYPES:
        raise _input_error("附件文件类型无效")
    return file_type


def _matching_extension(file_name: str, file_type: str) -> bool:
    separator = file_name.rfind(".")
    if separator <= 0:
        return False
    return _equivalent_file_type(file_name[separator + 1 :].lower(), file_type)


def _equivalent_file_type(first: str, second: str) -> bool:
    return ({first, second} <= {"jpeg", "jpg"}) or first == second


def _required_identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _protocol_error()
    identifier = str(value).strip()
    if not _SAFE_ID.fullmatch(identifier):
        raise _protocol_error()
    return identifier


def _possible_identifier(value: object) -> str | None:
    try:
        return _required_identifier(value)
    except ApiError:
        return None


def _required_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _protocol_error()
    return value


def _required_secret_text(value: object) -> str:
    if not isinstance(value, str):
        raise _protocol_error()
    text = value.strip()
    if not text or len(text) > 4096 or any(character in text for character in "\r\n\x00"):
        raise _protocol_error()
    return text


def _required_text(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise _input_error(message) if message else _protocol_error()
    text = value.strip()
    if not text or len(text) > 512 or any(character in text for character in "\r\n\x00"):
        raise _input_error(message) if message else _protocol_error()
    return text


def _path_segment(value: str) -> str:
    return quote(_required_identifier(value), safe="")


def _retryable_upload_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_UPLOAD_STATUSES or status_code >= 500


def _is_safe_header_value(value: str) -> bool:
    """Return whether httpx can serialize the signed value without ambiguity."""

    return all(character == "\t" or 0x20 <= ord(character) <= 0x7E for character in value)


def _is_explicit_dentry_not_found(error: DingTalkOpenAPIError) -> bool:
    code = error.upstream_code
    return bool(
        error.http_status in {404, 410} and code and code.lower() in _DENTRY_NOT_FOUND_CODES
    )


def _is_definitive_commit_rejection(error: DingTalkOpenAPIError) -> bool:
    """Return only failures that prove DingTalk rejected before committing."""

    return error.http_status == 401 or error.code == "DINGTALK_PERMISSION_MISSING"


def _is_definitive_cleanup_rejection(error: DingTalkOpenAPIError) -> bool:
    if error.code == "DINGTALK_PERMISSION_MISSING":
        return True
    if error.code == "DINGTALK_RATE_LIMITED":
        return False
    status_code = error.http_status
    return bool(
        status_code is not None
        and 400 <= status_code < 500
        and status_code not in _RETRYABLE_UPLOAD_STATUSES
        and status_code not in {404, 410}
    )


def _safe_http_status(value: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        return None
    return value


def _input_error(message: str) -> ApiError:
    return ApiError("INVALID_APPROVAL_ATTACHMENT", message, 400)


def _upload_stage_error() -> DingTalkStorageUploadStateError:
    return DingTalkStorageUploadStateError(
        "prepared approval upload is invalid or has already advanced"
    )


def _protocol_error() -> ApiError:
    return ApiError(
        "DINGTALK_STORAGE_INVALID_RESPONSE",
        "钉钉附件服务返回了无法识别的数据",
        502,
    )


def _mismatch_error() -> ApiError:
    return ApiError(
        "DINGTALK_STORAGE_FILE_MISMATCH",
        "钉钉附件信息与提交记录不一致，需要人工核对",
        409,
    )
