from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.errors import ApiError
from app.database.base import Base
from app.integrations.dingtalk.client import DingTalkOpenAPIClient
from app.integrations.dingtalk.storage import (
    ApprovalAttachment,
    ApprovalFile,
    ApprovalSpace,
    AttachmentProbe,
    AttachmentRecycleOutcome,
    DingTalkStorageClient,
    DingTalkStorageCommitOutcomeUnknown,
    DingTalkStorageRecycleOutcomeUnknown,
    DingTalkStorageUploadError,
    DingTalkStorageUploadStateError,
)
from app.main import create_app

SIGNED_UPLOAD_URL = "https://zjk-dualstack.trans.dingtalk.com/upload/object-1"


async def no_sleep(_seconds: float) -> None:
    return None


def token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "private-enterprise-token", "expires_in": 7200},
    )


def upload_info_payload(
    *,
    upload_key: str = "private-upload-key",
    resource_url: str = SIGNED_UPLOAD_URL,
    headers: object = None,
    expiration_seconds: object = 900,
) -> dict[str, object]:
    if headers is None:
        headers = {
            "Authorization": "private-storage-signature",
            "x-oss-date": "Thu, 04 Sep 2026 00:00:00 GMT",
        }
    return {
        "protocol": "HEADER_SIGNATURE",
        "storageDriver": "DINGTALK",
        "uploadKey": upload_key,
        "headerSignatureInfo": {
            "expirationSeconds": expiration_seconds,
            "resourceUrls": [resource_url],
            "headers": headers,
        },
    }


def dentry_payload(
    *,
    space_id: str = "space-1",
    file_id: str = "file-1",
    name: str = "差旅费报销单 (1).xlsx",
    size: int = 17,
    extension: str = "xlsx",
    status: str = "NORMAL",
    dentry_type: str = "FILE",
) -> dict[str, object]:
    return {
        "dentry": {
            "spaceId": space_id,
            "id": file_id,
            "name": name,
            "size": size,
            "extension": extension,
            "status": status,
            "type": dentry_type,
        }
    }


def expected_attachment() -> ApprovalAttachment:
    return ApprovalAttachment(
        space_id="space-1",
        file_id="file-1",
        file_name="票据.pdf",
        file_size=17,
        file_type="pdf",
    )


def storage_clients(
    settings_factory,
    openapi_handler,
    upload_handler,
    *,
    clock: Callable[[], float] = lambda: 100.0,
    **overrides,
):
    settings = settings_factory(**overrides)
    openapi = DingTalkOpenAPIClient(
        settings,
        transport=httpx.MockTransport(openapi_handler),
    )
    storage = DingTalkStorageClient(
        openapi,
        settings,
        upload_transport=httpx.MockTransport(upload_handler),
        sleep=no_sleep,
        clock=clock,
    )
    return openapi, storage


async def complete_upload(
    storage: DingTalkStorageClient,
    *,
    space: ApprovalSpace,
    union_id: str,
    file: ApprovalFile,
) -> ApprovalAttachment:
    prepared = await storage.prepare_upload(
        space=space,
        union_id=union_id,
        file=file,
    )
    await storage.put_prepared_upload(prepared=prepared)
    return await storage.commit_prepared_upload(prepared=prepared)


class TrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url.host}")

    async def aclose(self) -> None:
        self.closed = True


def test_application_owns_separate_openapi_and_signed_upload_clients(
    settings_factory,
) -> None:
    openapi_transport = TrackingTransport()
    upload_transport = TrackingTransport()
    application = create_app(
        settings_factory(),
        dingtalk_transport=openapi_transport,
        dingtalk_upload_transport=upload_transport,
    )
    Base.metadata.create_all(application.state.database_engine)

    with TestClient(application):
        assert isinstance(application.state.dingtalk_storage, DingTalkStorageClient)
        assert not openapi_transport.closed
        assert not upload_transport.closed

    assert openapi_transport.closed
    assert upload_transport.closed


@pytest.mark.asyncio
async def test_uploads_file_to_approval_space_and_returns_verified_oa_metadata(
    settings_factory,
) -> None:
    content = b"xlsx-test-content"
    file = ApprovalFile(
        file_name="差旅费报销单.xlsx",
        file_type="xlsx",
        content=content,
    )
    calls: list[str] = []

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls.append("token")
            return token_response()
        assert request.headers["x-acs-dingtalk-access-token"] == "private-enterprise-token"
        if request.url.path.endswith("/spaces/infos/query"):
            calls.append("space")
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "userId": "employee-1",
                "agentId": 1234567890,
            }
            return httpx.Response(
                200,
                json={"success": True, "result": {"spaceId": "space-1"}},
            )
        if request.url.path.endswith("/files/uploadInfos/query"):
            calls.append("upload-info")
            assert request.method == "POST"
            assert dict(request.url.params) == {"unionId": "union-1"}
            assert json.loads(request.content) == {
                "protocol": "HEADER_SIGNATURE",
                "multipart": False,
                "option": {
                    "storageDriver": "DINGTALK",
                    "preCheckParam": {
                        "name": "差旅费报销单.xlsx",
                        "size": 17,
                        "parentId": "0",
                    },
                    "preferIntranet": False,
                },
            }
            return httpx.Response(200, json=upload_info_payload())
        if request.url.path.endswith("/files/commit"):
            calls.append("commit")
            assert request.method == "POST"
            assert dict(request.url.params) == {"unionId": "union-1"}
            assert json.loads(request.content) == {
                "name": "差旅费报销单.xlsx",
                "uploadKey": "private-upload-key",
                "parentId": "0",
                "option": {"size": 17, "conflictStrategy": "AUTO_RENAME"},
            }
            return httpx.Response(200, json=dentry_payload())
        if request.url.path.endswith("/dentries/file-1/query"):
            calls.append("probe")
            assert request.method == "POST"
            assert dict(request.url.params) == {"unionId": "union-1"}
            assert json.loads(request.content) == {}
            return httpx.Response(200, json=dentry_payload())
        raise AssertionError(f"unexpected OpenAPI request: {request.url}")

    def upload_handler(request: httpx.Request) -> httpx.Response:
        calls.append("signed-put")
        assert request.method == "PUT"
        assert str(request.url) == SIGNED_UPLOAD_URL
        assert request.content == content
        assert request.headers["authorization"] == "private-storage-signature"
        assert request.headers["x-oss-date"] == "Thu, 04 Sep 2026 00:00:00 GMT"
        assert request.headers["content-length"] == "17"
        assert "x-acs-dingtalk-access-token" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200)

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        upload_handler,
    )
    try:
        space = await storage.get_approval_space("employee-1")
        prepared = await storage.prepare_upload(
            space=space,
            union_id="union-1",
            file=file,
        )
        assert calls == ["token", "space", "upload-info"]
        returned = await storage.put_prepared_upload(prepared=prepared)
        assert returned is prepared
        assert calls == ["token", "space", "upload-info", "signed-put"]
        attachment = await storage.commit_prepared_upload(prepared=prepared)
        probe = await storage.probe_attachment(
            union_id="union-1",
            expected=attachment,
        )
    finally:
        await storage.close()
        await openapi.close()

    assert attachment.as_oa_value() == {
        "spaceId": "space-1",
        "fileId": "file-1",
        "fileName": "差旅费报销单 (1).xlsx",
        "fileSize": 17,
        "fileType": "xlsx",
    }
    assert probe is AttachmentProbe.EXACT
    assert calls == ["token", "space", "upload-info", "signed-put", "commit", "probe"]


@pytest.mark.asyncio
async def test_checkpoint_failure_after_put_never_sends_commit(settings_factory) -> None:
    calls = {"signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(200, json=dentry_payload(name="票据.pdf", extension="pdf"))

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(200)

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        prepared = await storage.prepare_upload(
            space=ApprovalSpace("space-1"),
            union_id="private-union-id",
            file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
        )
        assert all(
            secret not in repr(prepared)
            for secret in (
                "private-union-id",
                "private-file-data",
                "private-upload-key",
                "private-storage-signature",
                SIGNED_UPLOAD_URL,
            )
        )
        assert await storage.put_prepared_upload(prepared=prepared) is prepared
        assert calls == {"signed_put": 1, "commit": 0}

        async def persist_committing_checkpoint() -> None:
            raise RuntimeError("database unavailable")

        with pytest.raises(RuntimeError, match="database unavailable"):
            await persist_committing_checkpoint()
    finally:
        await storage.close()
        await openapi.close()

    assert calls == {"signed_put": 1, "commit": 0}


@pytest.mark.asyncio
async def test_upload_handle_is_client_bound_ordered_and_commit_is_single_use(
    settings_factory,
) -> None:
    calls = {"signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(200, json=dentry_payload(name="票据.pdf", extension="pdf"))

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(200)

    settings = settings_factory()
    openapi = DingTalkOpenAPIClient(
        settings,
        transport=httpx.MockTransport(openapi_handler),
    )
    storage = DingTalkStorageClient(
        openapi,
        settings,
        upload_transport=httpx.MockTransport(upload_handler),
        sleep=no_sleep,
    )
    other_storage = DingTalkStorageClient(
        openapi,
        settings,
        upload_transport=httpx.MockTransport(upload_handler),
        sleep=no_sleep,
    )
    try:
        prepared = await storage.prepare_upload(
            space=ApprovalSpace("space-1"),
            union_id="union-1",
            file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
        )
        with pytest.raises(DingTalkStorageUploadStateError):
            await storage.commit_prepared_upload(prepared=prepared)
        with pytest.raises(DingTalkStorageUploadStateError):
            await other_storage.put_prepared_upload(prepared=prepared)
        assert calls == {"signed_put": 0, "commit": 0}

        await storage.put_prepared_upload(prepared=prepared)
        with pytest.raises(DingTalkStorageUploadStateError):
            await storage.put_prepared_upload(prepared=prepared)
        attachment = await storage.commit_prepared_upload(prepared=prepared)
        with pytest.raises(DingTalkStorageUploadStateError):
            await storage.commit_prepared_upload(prepared=prepared)
    finally:
        await other_storage.close()
        await storage.close()
        await openapi.close()

    assert attachment.file_id == "file-1"
    assert calls == {"signed_put": 1, "commit": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        upload_info_payload(resource_url="http://zjk-dualstack.trans.dingtalk.com/object"),
        upload_info_payload(resource_url="https://trans.dingtalk.com.evil.example/object"),
        upload_info_payload(resource_url="https://user@trans.dingtalk.com/object"),
        upload_info_payload(resource_url="https://trans.dingtalk.com:444/object"),
        upload_info_payload(resource_url="https://127.0.0.1/object"),
        upload_info_payload(resource_url=f"{SIGNED_UPLOAD_URL}#fragment"),
        upload_info_payload(headers={"Cookie": "private-cookie"}),
        upload_info_payload(headers={"x-acs-dingtalk-access-token": "private-token"}),
        upload_info_payload(headers={"Content-Length": "17"}),
        upload_info_payload(headers={"Authorization": "private-signature-é"}),
        upload_info_payload(headers={"Authorization": "private-signature-\x01"}),
        upload_info_payload(
            headers={
                "Authorization": "private-signature-1",
                "authorization": "private-signature-2",
            }
        ),
        upload_info_payload(headers={"Authorization": "private-signature\r\nInjected: yes"}),
        upload_info_payload(expiration_seconds=0),
        upload_info_payload(expiration_seconds=3601),
    ],
)
async def test_rejects_untrusted_signed_upload_contract_before_sending_file(
    settings_factory,
    payload: dict[str, object],
) -> None:
    calls = {"upload_info": 0, "signed_put": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["upload_info"] += 1
        return httpx.Response(200, json=payload)

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(200)

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        with pytest.raises(ApiError) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-bytes"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert calls == {"upload_info": 1, "signed_put": 0}
    assert caught.value.code == "DINGTALK_STORAGE_INVALID_RESPONSE"
    for secret in ("private-cookie", "private-token", "private-signature", "private-file"):
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_signed_put_retries_transient_failures_without_requerying_upload_info(
    settings_factory,
) -> None:
    calls = {"upload_info": 0, "signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            calls["upload_info"] += 1
            return httpx.Response(200, json=upload_info_payload())
        if request.url.path.endswith("/files/commit"):
            calls["commit"] += 1
            return httpx.Response(
                200,
                json=dentry_payload(name="票据.pdf", size=17, extension="pdf"),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    def upload_handler(request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        assert "cookie" not in request.headers
        if calls["signed_put"] == 1:
            raise httpx.ReadTimeout("private timeout", request=request)
        if calls["signed_put"] == 2:
            return httpx.Response(
                503,
                text="private storage failure",
                headers={"Set-Cookie": "private-cookie=must-not-be-retained; Path=/"},
            )
        return httpx.Response(200)

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        attachment = await complete_upload(
            storage,
            space=ApprovalSpace("space-1"),
            union_id="union-1",
            file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
        )
    finally:
        await storage.close()
        await openapi.close()

    assert attachment.file_name == "票据.pdf"
    assert calls == {"upload_info": 1, "signed_put": 3, "commit": 1}


@pytest.mark.asyncio
async def test_signed_put_exhaustion_returns_only_sanitized_failure(settings_factory) -> None:
    calls = {"signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(200, json={})

    def upload_handler(request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        raise httpx.ReadTimeout("private upload timeout", request=request)

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        with pytest.raises(DingTalkStorageUploadError) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.http_status is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)
    assert calls == {"signed_put": 3, "commit": 0}


@pytest.mark.asyncio
async def test_signed_put_normalizes_local_serialization_failure(settings_factory) -> None:
    calls = {"signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(200, json={})

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        raise ValueError("private local serialization failure")

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        with pytest.raises(DingTalkStorageUploadError) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.http_status is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)
    assert calls == {"signed_put": 1, "commit": 0}


@pytest.mark.asyncio
async def test_signed_put_does_not_follow_redirect_or_commit(settings_factory) -> None:
    calls = {"upload_info": 0, "signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            calls["upload_info"] += 1
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(200, json={})

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(307, headers={"Location": "https://evil.example/private"})

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        with pytest.raises(DingTalkStorageUploadError) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.http_status == 307
    assert calls == {"upload_info": 1, "signed_put": 1, "commit": 0}


@pytest.mark.asyncio
async def test_expired_ticket_is_requeried_once_then_fails_without_commit(
    settings_factory,
) -> None:
    calls = {"clock": 0, "upload_info": 0, "signed_put": 0, "commit": 0}
    clock_values = iter((100.0, 102.0, 200.0, 202.0))

    def clock() -> float:
        calls["clock"] += 1
        return next(clock_values)

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            calls["upload_info"] += 1
            return httpx.Response(
                200,
                json=upload_info_payload(expiration_seconds=1),
            )
        calls["commit"] += 1
        return httpx.Response(200, json={})

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(200)

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        upload_handler,
        clock=clock,
    )
    try:
        with pytest.raises(DingTalkStorageUploadError):
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert calls == {"clock": 4, "upload_info": 2, "signed_put": 0, "commit": 0}


@pytest.mark.asyncio
async def test_refreshed_ticket_is_bound_to_put_and_used_by_commit(settings_factory) -> None:
    calls = {"upload_info": 0, "signed_put": 0, "commit": 0}
    clock_values = iter((100.0, 102.0, 200.0, 201.0))

    def clock() -> float:
        return next(clock_values)

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            calls["upload_info"] += 1
            if calls["upload_info"] == 1:
                return httpx.Response(
                    200,
                    json=upload_info_payload(
                        upload_key="expired-private-upload-key",
                        expiration_seconds=1,
                    ),
                )
            return httpx.Response(
                200,
                json=upload_info_payload(upload_key="fresh-private-upload-key"),
            )
        calls["commit"] += 1
        body = json.loads(request.content)
        assert body["uploadKey"] == "fresh-private-upload-key"
        return httpx.Response(200, json=dentry_payload(name="票据.pdf", extension="pdf"))

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(200)

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        upload_handler,
        clock=clock,
    )
    try:
        attachment = await complete_upload(
            storage,
            space=ApprovalSpace("space-1"),
            union_id="union-1",
            file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
        )
    finally:
        await storage.close()
        await openapi.close()

    assert attachment.file_id == "file-1"
    assert calls == {"upload_info": 2, "signed_put": 1, "commit": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "expected_http_status"),
    [("timeout", None), ("server", 503), ("invalid-json", 200)],
)
async def test_commit_uncertainty_is_not_retried_or_exposed_as_upload_failure(
    settings_factory,
    failure_type: str,
    expected_http_status: int | None,
) -> None:
    calls = {"upload_info": 0, "signed_put": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            calls["upload_info"] += 1
            return httpx.Response(200, json=upload_info_payload())
        if request.url.path.endswith("/files/commit"):
            calls["commit"] += 1
            if failure_type == "timeout":
                raise httpx.ReadTimeout("private commit timeout", request=request)
            if failure_type == "server":
                return httpx.Response(503, text="private commit failure")
            return httpx.Response(200, text="private non-json response")
        raise AssertionError(f"unexpected request: {request.url}")

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["signed_put"] += 1
        return httpx.Response(200)

    openapi, storage = storage_clients(settings_factory, openapi_handler, upload_handler)
    try:
        prepared = await storage.prepare_upload(
            space=ApprovalSpace("space-1"),
            union_id="union-1",
            file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
        )
        await storage.put_prepared_upload(prepared=prepared)
        with pytest.raises(DingTalkStorageCommitOutcomeUnknown) as caught:
            await storage.commit_prepared_upload(prepared=prepared)
        with pytest.raises(DingTalkStorageUploadStateError):
            await storage.commit_prepared_upload(prepared=prepared)
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.http_status == expected_http_status
    assert caught.value.code == "DINGTALK_STORAGE_COMMIT_UNKNOWN"
    assert calls == {"upload_info": 1, "signed_put": 1, "commit": 1}
    for secret in ("private commit", "private non-json", "private-file"):
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_cancelled_commit_is_terminal_and_is_never_replayed(settings_factory) -> None:
    calls = {"commit": 0}

    async def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        raise asyncio.CancelledError

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(200),
    )
    try:
        prepared = await storage.prepare_upload(
            space=ApprovalSpace("space-1"),
            union_id="union-1",
            file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
        )
        await storage.put_prepared_upload(prepared=prepared)
        with pytest.raises(asyncio.CancelledError):
            await storage.commit_prepared_upload(prepared=prepared)
        with pytest.raises(DingTalkStorageUploadStateError):
            await storage.commit_prepared_upload(prepared=prepared)
    finally:
        await storage.close()
        await openapi.close()

    assert calls["commit"] == 1


@pytest.mark.asyncio
async def test_malformed_commit_metadata_is_unknown_and_preserves_safe_probe_ids(
    settings_factory,
) -> None:
    calls = {"commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(
            200,
            json=dentry_payload(
                file_id="file-possible-1",
                name="different.pdf",
                size=18,
                extension="pdf",
            ),
        )

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(200),
    )
    try:
        with pytest.raises(DingTalkStorageCommitOutcomeUnknown) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.possible_space_id == "space-1"
    assert caught.value.possible_file_id == "file-possible-1"
    assert calls["commit"] == 1


@pytest.mark.asyncio
async def test_definitive_commit_permission_rejection_is_not_retried(
    settings_factory,
) -> None:
    calls = {"commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(
            403,
            json={"code": "Forbidden.Private", "message": "private diagnostic"},
        )

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(200),
    )
    try:
        with pytest.raises(ApiError) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.code == "DINGTALK_STORAGE_COMMIT_REJECTED"
    assert calls["commit"] == 1
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "upstream_code"),
    [
        (400, "invalidParameter"),
        (404, "dentryNotExist"),
        (409, "Conflict"),
        (422, "UnprocessableEntity"),
        (500, "systemError"),
    ],
)
async def test_unknown_commit_rejections_are_uncertain_and_never_retried(
    settings_factory,
    status_code: int,
    upstream_code: str,
) -> None:
    calls = {"commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(
            status_code,
            json={"code": upstream_code, "message": "private diagnostic"},
        )

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(200),
    )
    try:
        with pytest.raises(DingTalkStorageCommitOutcomeUnknown) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.code == "DINGTALK_STORAGE_COMMIT_UNKNOWN"
    assert caught.value.http_status == status_code
    assert calls["commit"] == 1
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)


@pytest.mark.asyncio
async def test_commit_unauthorized_response_is_not_replayed_with_a_new_token(
    settings_factory,
) -> None:
    calls = {"token": 0, "commit": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"private-token-{calls['token']}"},
            )
        if request.url.path.endswith("/files/uploadInfos/query"):
            return httpx.Response(200, json=upload_info_payload())
        calls["commit"] += 1
        return httpx.Response(401, json={"code": "InvalidToken"})

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(200),
    )
    try:
        with pytest.raises(ApiError) as caught:
            await complete_upload(
                storage,
                space=ApprovalSpace("space-1"),
                union_id="union-1",
                file=ApprovalFile("票据.pdf", "pdf", b"private-file-data"),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.code == "DINGTALK_STORAGE_COMMIT_REJECTED"
    assert calls == {"token": 1, "commit": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_kind", "expected_probe"),
    [
        ("not-found", AttachmentProbe.MISSING),
        ("deleted", AttachmentProbe.MISSING),
        ("exact", AttachmentProbe.EXACT),
        ("wrong-name", AttachmentProbe.MISMATCH),
        ("wrong-id", AttachmentProbe.MISMATCH),
    ],
)
async def test_probe_distinguishes_missing_exact_and_mismatched_dentry(
    settings_factory,
    response_kind: str,
    expected_probe: AttachmentProbe,
) -> None:
    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        assert request.method == "POST"
        assert request.url.path == "/v1.0/storage/spaces/space-1/dentries/file-1/query"
        assert dict(request.url.params) == {"unionId": "union-1"}
        assert json.loads(request.content) == {}
        if response_kind == "not-found":
            return httpx.Response(404, json={"code": "dentryNotExist"})
        if response_kind == "deleted":
            return httpx.Response(
                200,
                json=dentry_payload(name="票据.pdf", extension="pdf", status="DELETED"),
            )
        if response_kind == "wrong-name":
            return httpx.Response(
                200,
                json=dentry_payload(name="其他票据.pdf"),
            )
        if response_kind == "wrong-id":
            return httpx.Response(
                200,
                json=dentry_payload(file_id="file-2", name="票据.pdf"),
            )
        return httpx.Response(
            200,
            json=dentry_payload(name="票据.pdf", extension="pdf"),
        )

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        probe = await storage.probe_attachment(
            union_id="union-1",
            expected=expected_attachment(),
        )
    finally:
        await storage.close()
        await openapi.close()

    assert probe is expected_probe


@pytest.mark.asyncio
async def test_probe_does_not_treat_generic_gateway_404_as_missing(settings_factory) -> None:
    calls = 0

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/token"):
            return token_response()
        calls += 1
        return httpx.Response(404, json={"code": "NotFound"})

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        with pytest.raises(ApiError) as caught:
            await storage.probe_attachment(
                union_id="union-1",
                expected=expected_attachment(),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.code == "DINGTALK_OPENAPI_FAILED"
    assert getattr(caught.value, "http_status", None) == 404
    assert calls == 1


@pytest.mark.asyncio
async def test_probe_does_not_treat_permission_failure_as_missing(settings_factory) -> None:
    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            403,
            json={"code": "Forbidden.AccessDenied.PermissionDenied"},
        )

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        with pytest.raises(ApiError) as caught:
            await storage.probe_attachment(
                union_id="union-1",
                expected=expected_attachment(),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.code == "DINGTALK_PERMISSION_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_kind", ["success", "timeout", "not-found", "qps-limit"])
async def test_recycle_attachment_verifies_missing_after_one_delete_attempt(
    settings_factory,
    delete_kind: str,
) -> None:
    calls = {"probe": 0, "delete": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.method == "POST":
            calls["probe"] += 1
            if calls["probe"] == 1:
                return httpx.Response(
                    200,
                    json=dentry_payload(name="票据.pdf", extension="pdf"),
                )
            return httpx.Response(404, json={"code": "dentryNotExist"})
        assert request.method == "DELETE"
        assert request.url.path == "/v1.0/storage/spaces/space-1/dentries/file-1"
        assert dict(request.url.params) == {
            "unionId": "union-1",
            "toRecycleBin": "true",
        }
        calls["delete"] += 1
        if delete_kind == "timeout":
            raise httpx.ReadTimeout("private delete timeout", request=request)
        if delete_kind == "not-found":
            return httpx.Response(404, json={"code": "NotFound"})
        if delete_kind == "qps-limit":
            return httpx.Response(
                403,
                json={"code": "Forbidden.AccessDenied.QpsLimitForAppkeyAndApi"},
            )
        return httpx.Response(200, json={"taskId": "task-1"})

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        outcome = await storage.recycle_attachment(
            union_id="union-1",
            attachment=expected_attachment(),
        )
    finally:
        await storage.close()
        await openapi.close()

    assert outcome is AttachmentRecycleOutcome.CLEANED
    assert calls == {"probe": 2, "delete": 1}


@pytest.mark.asyncio
async def test_async_recycle_attachment_returns_retry_later_while_file_remains(
    settings_factory,
) -> None:
    calls = {"probe": 0, "delete": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.method == "POST":
            calls["probe"] += 1
            return httpx.Response(
                200,
                json=dentry_payload(name="票据.pdf", extension="pdf"),
            )
        calls["delete"] += 1
        return httpx.Response(202, json={"taskId": "task-1"})

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        outcome = await storage.recycle_attachment(
            union_id="union-1",
            attachment=expected_attachment(),
        )
    finally:
        await storage.close()
        await openapi.close()

    assert outcome is AttachmentRecycleOutcome.RETRY_LATER
    assert calls == {"probe": 2, "delete": 1}


@pytest.mark.asyncio
async def test_recycle_attachment_refuses_mismatched_file_without_delete(
    settings_factory,
) -> None:
    calls = {"probe": 0, "delete": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.method == "POST":
            calls["probe"] += 1
            return httpx.Response(200, json=dentry_payload(name="其他票据.pdf"))
        calls["delete"] += 1
        return httpx.Response(200, json={})

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        with pytest.raises(ApiError) as caught:
            await storage.recycle_attachment(
                union_id="union-1",
                attachment=expected_attachment(),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.code == "DINGTALK_STORAGE_FILE_MISMATCH"
    assert calls == {"probe": 1, "delete": 0}


@pytest.mark.asyncio
async def test_recycle_attachment_reports_unknown_when_final_probe_cannot_complete(
    settings_factory,
) -> None:
    calls = {"probe": 0, "delete": 0}

    def openapi_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        if request.method == "POST":
            calls["probe"] += 1
            if calls["probe"] == 1:
                return httpx.Response(
                    200,
                    json=dentry_payload(name="票据.pdf", extension="pdf"),
                )
            return httpx.Response(503, text="private final probe failure")
        calls["delete"] += 1
        return httpx.Response(202, json={"taskId": "task-1"})

    openapi, storage = storage_clients(
        settings_factory,
        openapi_handler,
        lambda _request: httpx.Response(500),
    )
    try:
        with pytest.raises(DingTalkStorageRecycleOutcomeUnknown) as caught:
            await storage.recycle_attachment(
                union_id="union-1",
                attachment=expected_attachment(),
            )
    finally:
        await storage.close()
        await openapi.close()

    assert caught.value.http_status == 503
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)
    assert calls == {"probe": 4, "delete": 1}
