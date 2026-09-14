from __future__ import annotations

import asyncio
import io
import json
import logging
from time import monotonic

import httpx
import pytest

from app.core.logging import JsonFormatter
from app.core.request_id import bind_request_id, reset_request_id
from app.integrations.dingtalk.client import (
    DingTalkOpenAPIClient,
    DingTalkOpenAPIError,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "expected_operation"),
    [
        (
            "POST",
            "/v1.0/workflow/processes/instanceIds/query",
            "workflow_instance_list",
        ),
        ("GET", "/v1.0/workflow/processInstances", "workflow_instance_read"),
    ],
)
async def test_openapi_permission_failure_logs_only_sanitized_diagnostics(
    settings_factory,
    method: str,
    path: str,
    expected_operation: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "private-token"})
        return httpx.Response(
            403,
            json={
                "code": "Forbidden.AccessDenied.PermissionDenied",
                "message": "private upstream diagnostic for employee-1",
                "requestId": "private-upstream-request-id",
            },
        )

    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(JsonFormatter())
    client_logger = logging.getLogger("app.integrations.dingtalk.client")
    client_logger.addHandler(log_handler)
    request_id_token = bind_request_id("local-request-123")
    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError):
            await client.request_openapi_json(method, path)
    finally:
        await client.close()
        reset_request_id(request_id_token)
        client_logger.removeHandler(log_handler)
        log_handler.close()

    log_body = json.loads(log_output.getvalue())
    assert log_body == {
        "timestamp": log_body["timestamp"],
        "level": "WARNING",
        "logger": "app.integrations.dingtalk.client",
        "message": "DingTalk OpenAPI request failed",
        "requestId": "local-request-123",
        "errorCode": "DINGTALK_PERMISSION_MISSING",
        "upstream": "dingtalk",
        "upstreamApi": "openapi",
        "upstreamOperation": expected_operation,
        "upstreamHttpStatus": 403,
        "upstreamErrorCode": "Forbidden.AccessDenied.PermissionDenied",
    }
    assert "private-token" not in log_output.getvalue()
    assert "private upstream diagnostic" not in log_output.getvalue()
    assert "employee-1" not in log_output.getvalue()
    assert "private-upstream-request-id" not in log_output.getvalue()


@pytest.mark.asyncio
async def test_openapi_permission_code_in_success_response_is_logged(
    settings_factory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "private-token"})
        return httpx.Response(
            200,
            json={
                "code": "Forbidden.AccessDenied.AccessTokenPermissionDenied",
                "message": "private upstream diagnostic",
            },
        )

    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(JsonFormatter())
    client_logger = logging.getLogger("app.integrations.dingtalk.client")
    client_logger.addHandler(log_handler)
    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError):
            await client.request_openapi_json(
                "POST",
                "/v1.0/workflow/processes/instanceIds/query",
            )
    finally:
        await client.close()
        client_logger.removeHandler(log_handler)
        log_handler.close()

    log_body = json.loads(log_output.getvalue())
    assert log_body["upstreamHttpStatus"] == 200
    assert log_body["upstreamErrorCode"] == "Forbidden.AccessDenied.AccessTokenPermissionDenied"
    assert log_body["upstreamOperation"] == "workflow_instance_list"
    assert "private upstream diagnostic" not in log_output.getvalue()


@pytest.mark.asyncio
async def test_recovered_invalid_token_does_not_log_an_upstream_failure(
    settings_factory,
) -> None:
    calls = {"token": 0, "query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"private-token-{calls['token']}"},
            )
        calls["query"] += 1
        if calls["query"] == 1:
            return httpx.Response(401, json={"code": "InvalidAuthentication"})
        return httpx.Response(200, json={"success": True})

    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(JsonFormatter())
    client_logger = logging.getLogger("app.integrations.dingtalk.client")
    client_logger.addHandler(log_handler)
    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.request_openapi_json(
            "POST",
            "/v1.0/workflow/processes/instanceIds/query",
        )
    finally:
        await client.close()
        client_logger.removeHandler(log_handler)
        log_handler.close()

    assert result == {"success": True}
    assert calls == {"token": 2, "query": 2}
    assert log_output.getvalue() == ""


@pytest.mark.asyncio
async def test_idempotent_get_retries_rate_limit_with_sanitized_error(
    settings_factory,
) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        return httpx.Response(
            429,
            json={
                "code": "Throttling.RateLimit",
                "message": "private upstream diagnostic",
            },
        )

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json("GET", "/v1.0/example")
    finally:
        await client.close()

    error = caught.value
    assert calls == {"token": 1, "openapi": 3}
    assert error.status_code == 503
    assert error.code == "DINGTALK_RATE_LIMITED"
    assert error.message == "钉钉接口请求繁忙，请稍后重试"
    assert error.http_status == 429
    assert error.upstream_code == "Throttling.RateLimit"
    assert "private" not in str(error)
    assert "private" not in repr(error)


@pytest.mark.asyncio
async def test_idempotent_get_retries_qps_limit_returned_as_403(
    settings_factory,
) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        if calls["openapi"] < 3:
            return httpx.Response(
                403,
                json={
                    "code": "Forbidden.AccessDenied.QpsLimitForAppkeyAndApi",
                    "message": "private upstream diagnostic",
                },
            )
        return httpx.Response(200, json={"success": True})

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.request_openapi_json(
            "GET",
            "/v1.0/workflow/processInstances",
        )
    finally:
        await client.close()

    assert result == {"success": True}
    assert calls == {"token": 1, "openapi": 3}


@pytest.mark.asyncio
async def test_exhausted_qps_limit_is_not_reported_as_permission_failure(
    settings_factory,
) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        return httpx.Response(
            403,
            json={"code": "Forbidden.AccessDenied.QpsLimitForAppkeyAndApi"},
        )

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json(
                "GET",
                "/v1.0/workflow/processInstances",
            )
    finally:
        await client.close()

    assert calls == {"token": 1, "openapi": 3}
    assert caught.value.code == "DINGTALK_RATE_LIMITED"
    assert caught.value.status_code == 503
    assert caught.value.message == "钉钉接口请求繁忙，请稍后重试"
    assert caught.value.http_status == 403


@pytest.mark.asyncio
async def test_workflow_instance_reads_are_paced_across_concurrent_callers(
    settings_factory,
) -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "private-token"})
        starts.append(monotonic())
        return httpx.Response(200, json={"success": True})

    client = DingTalkOpenAPIClient(
        settings_factory(
            dingtalk_workflow_instance_read_min_interval_seconds=0.03,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        await asyncio.gather(
            *(
                client.request_openapi_json(
                    "GET",
                    "/v1.0/workflow/processInstances",
                    params={"processInstanceId": f"synthetic-{index}"},
                )
                for index in range(3)
            )
        )
    finally:
        await client.close()

    assert len(starts) == 3
    assert all(
        current - previous >= 0.02 for previous, current in zip(starts, starts[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_qps_limit_defers_other_workflow_instance_read_callers(
    settings_factory,
) -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "private-token"})
        starts.append(monotonic())
        if len(starts) == 1:
            return httpx.Response(
                403,
                json={"code": "Forbidden.AccessDenied.QpsLimitForAppkeyAndApi"},
            )
        return httpx.Response(200, json={"success": True})

    client = DingTalkOpenAPIClient(
        settings_factory(
            dingtalk_workflow_instance_read_min_interval_seconds=0.03,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        results = await asyncio.gather(
            *(
                client.request_openapi_json(
                    "GET",
                    "/v1.0/workflow/processInstances",
                    params={"processInstanceId": f"synthetic-{index}"},
                )
                for index in range(2)
            )
        )
    finally:
        await client.close()

    assert results == [{"success": True}, {"success": True}]
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.45


@pytest.mark.asyncio
async def test_idempotent_get_retries_server_errors(settings_factory) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        return httpx.Response(
            503,
            json={"code": "ServiceUnavailable", "message": "private failure detail"},
        )

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json("GET", "/v1.0/example")
    finally:
        await client.close()

    assert calls == {"token": 1, "openapi": 3}
    assert caught.value.http_status == 503
    assert caught.value.upstream_code == "ServiceUnavailable"
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", ["timeout", "transport"])
async def test_idempotent_get_retries_transport_failures_without_retaining_details(
    settings_factory,
    failure_type: str,
) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        if failure_type == "timeout":
            raise httpx.ReadTimeout("private timeout detail", request=request)
        raise httpx.ConnectError("private transport detail", request=request)

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json("GET", "/v1.0/example")
    finally:
        await client.close()

    assert calls == {"token": 1, "openapi": 3}
    assert caught.value.http_status is None
    assert caught.value.upstream_code is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_success_status_with_non_json_body_is_a_sanitized_protocol_error(
    settings_factory,
) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        return httpx.Response(200, text="private non-json upstream body")

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json("GET", "/v1.0/example")
    finally:
        await client.close()

    assert calls == {"token": 1, "openapi": 1}
    assert caught.value.http_status == 200
    assert caught.value.upstream_code is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "expected_status", "expected_code"),
    [
        ("rate_limit", 429, "Throttling.RateLimit"),
        ("server", 503, "ServiceUnavailable"),
        ("timeout", None, None),
        ("transport", None, None),
    ],
)
async def test_non_idempotent_post_does_not_retry_transient_failure(
    settings_factory,
    failure_type: str,
    expected_status: int | None,
    expected_code: str | None,
) -> None:
    calls = {"token": 0, "openapi": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "private-token"})
        calls["openapi"] += 1
        if failure_type == "timeout":
            raise httpx.ReadTimeout("private timeout detail", request=request)
        if failure_type == "transport":
            raise httpx.ConnectError("private transport detail", request=request)
        if failure_type == "rate_limit":
            return httpx.Response(429, json={"code": "Throttling.RateLimit"})
        return httpx.Response(503, json={"code": "ServiceUnavailable"})

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json(
                "POST",
                "/v1.0/workflow/processInstances",
                json={"private": "request body"},
            )
    finally:
        await client.close()

    assert calls == {"token": 1, "openapi": 1}
    assert caught.value.http_status == expected_status
    assert caught.value.upstream_code == expected_code


@pytest.mark.asyncio
async def test_non_replayed_401_evicts_cached_token_for_the_next_request(
    settings_factory,
) -> None:
    calls = {"token": 0, "mutation": 0, "query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"private-token-{calls['token']}"},
            )
        if request.url.path.endswith("/mutation"):
            calls["mutation"] += 1
            assert request.headers["x-acs-dingtalk-access-token"] == "private-token-1"
            return httpx.Response(401, json={"code": "InvalidAuthentication"})
        calls["query"] += 1
        assert request.url.path.endswith("/query")
        assert request.headers["x-acs-dingtalk-access-token"] == "private-token-2"
        return httpx.Response(200, json={"success": True})

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_openapi_json(
                "POST",
                "/v1.0/mutation",
                retry_invalid_token=False,
                retry_transient=False,
            )
        result = await client.request_openapi_json("GET", "/v1.0/query")
    finally:
        await client.close()

    assert caught.value.http_status == 401
    assert result == {"success": True}
    assert calls == {"token": 2, "mutation": 1, "query": 1}


@pytest.mark.asyncio
async def test_non_replayed_oapi_401_evicts_cached_token_for_the_next_request(
    settings_factory,
) -> None:
    calls = {"token": 0, "mutation": 0, "query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"private-token-{calls['token']}"},
            )
        if request.url.path.endswith("/mutation"):
            calls["mutation"] += 1
            assert request.url.params["access_token"] == "private-token-1"
            return httpx.Response(401, json={"errcode": 40014})
        calls["query"] += 1
        assert request.url.path.endswith("/query")
        assert request.url.params["access_token"] == "private-token-2"
        return httpx.Response(200, json={"errcode": 0})

    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DingTalkOpenAPIError) as caught:
            await client.request_oapi_json(
                "POST",
                "/mutation",
                retry_invalid_token=False,
                retry_transient=False,
            )
        result = await client.request_oapi_json("GET", "/query")
    finally:
        await client.close()

    assert caught.value.http_status == 401
    assert result == {"errcode": 0}
    assert calls == {"token": 2, "mutation": 1, "query": 1}
