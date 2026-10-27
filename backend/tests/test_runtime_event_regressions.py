from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import contextmanager

import httpx
import pytest
from test_approval_diagnostics import Workflow
from test_oa_reimbursement_worker import FakeState, FakeStorage, _job, _lease, _processor, _upload
from test_runtime_events import PATH, admin
from test_travel_approvals import _catalog, _profile

from app.core.logging import JsonFormatter
from app.core.runtime_events import (
    RuntimeEventHandler,
    bind_runtime_recorder,
    reset_runtime_recorder,
)
from app.integrations.dingtalk.client import DingTalkOpenAPIClient, DingTalkOpenAPIError
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient
from app.models.reimbursement import ReimbursementSubmissionStatus, ReimbursementUploadStatus


@contextmanager
def capture_gateway_events(store):
    logger = logging.getLogger("app.integrations.dingtalk.client")
    handler = RuntimeEventHandler()
    output = io.StringIO()
    formatted = logging.StreamHandler(output)
    formatted.setFormatter(JsonFormatter())
    previous_level, previous_propagate = logger.level, logger.propagate
    token = bind_runtime_recorder(store)
    logger.addHandler(handler)
    logger.addHandler(formatted)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield output
    finally:
        reset_runtime_recorder(token)
        logger.removeHandler(handler)
        logger.removeHandler(formatted)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("module", ["approvals", "submission"])
def test_outer_exception_handler_preserves_exactly_one_safe_failure_with_request_id(
    client_factory, synchronous, module
):
    client = admin(client_factory)
    request_id = "synthetic-outer-failure"
    path = (
        "/api/oa/_synthetic-failure"
        if module == "approvals"
        else "/api/reimbursements/submissions/_synthetic-failure"
    )

    def crash():
        raise ValueError("synthetic-private-exception-body")

    async def async_crash():
        crash()

    client.app.add_api_route(path, crash if synchronous else async_crash, methods=["GET"])
    with pytest.raises(ValueError, match="synthetic-private-exception-body"):
        client.get(path, headers={"X-Request-ID": request_id})
    data = client.get(PATH, params={"requestId": request_id}).json()["data"]
    failures = [row for row in data["items"] if row["event"] == "internal_failure"]
    assert len(failures) == 1
    assert failures[0]["module"] == module
    assert failures[0]["details"]["exceptionType"] == "ValueError"
    assert failures[0]["details"]["errorCode"] == "INTERNAL_ERROR"
    request = next(row for row in data["items"] if row["event"] == "request")
    assert request["details"]["httpStatus"] == 500
    assert request["details"]["errorCode"] == "INTERNAL_ERROR"
    assert "synthetic-private" not in json.dumps(data)
    assert data["collector"]["droppedSinceStart"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "kind"), [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "transport")]
)
async def test_gateway_keeps_transport_kind_and_sanitization_without_replaying_create(
    client_factory, settings_factory, failure_type, kind
):
    client = admin(client_factory)
    calls = []

    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "synthetic-private-token"})
        calls.append(request)
        raise failure_type("synthetic-private-network-body", request=request)

    gateway = DingTalkOpenAPIClient(settings_factory(), transport=httpx.MockTransport(handler))
    try:
        with capture_gateway_events(client.app.state.runtime_events) as output:
            with pytest.raises(DingTalkOpenAPIError) as failure:
                await gateway.request_openapi_json(
                    "POST",
                    "/v1.0/workflow/processInstances",
                    retry_transient=False,
                    retry_invalid_token=False,
                    json={"originatorUserId": "synthetic-private-user"},
                )
        assert len(calls) == 1
        assert failure.value.code == "DINGTALK_OPENAPI_FAILED"
        assert failure.value.status_code == 502
        assert failure.value.failure_kind == kind
        data = client.get(PATH, params={"module": "submission", "result": "failed"}).json()["data"]
        event = next(row for row in data["items"] if row["event"] == "upstream_failure")
        assert event["details"]["failureKind"] == kind
        assert json.loads(output.getvalue())["failureKind"] == kind
        assert "synthetic-private" not in output.getvalue()
        assert "synthetic-private" not in json.dumps(data)
    finally:
        await gateway.close()


@pytest.mark.parametrize(("kind", "result"), [("timeout", "incomplete"), ("transport", "failed")])
def test_employee_query_preserves_failure_kind_without_claiming_zero_results(
    client_factory, monkeypatch, kind, result
):
    from app.api import oa_reimbursements

    client = admin(client_factory)

    async def ready(*_args):
        return _catalog(_profile("synthetic", "PROC-SYNTHETIC"))

    monkeypatch.setattr(oa_reimbursements, "load_fresh_submission_catalog", ready)
    client.app.state.dingtalk_workflow = Workflow(
        failure=DingTalkOpenAPIError(http_status=None, failure_kind=kind)
    )
    response = client.get(
        "/api/oa/travel-approvals", params={"from": "2026-08-01", "to": "2026-08-31"}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_OPENAPI_FAILED"
    data = client.get(PATH, params={"requestId": response.json()["requestId"]}).json()["data"]
    event = next(row for row in data["items"] if row["event"] == "approval_query")
    assert event["result"] == result
    assert event["details"]["failureKind"] == kind
    assert event["details"]["returned"] is None


@pytest.mark.asyncio
async def test_actual_oa_readback_failure_is_searchable_as_submission_and_scope_is_restored(
    client_factory, settings_factory
):
    client = admin(client_factory)
    paths = []

    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        paths.append((request.method, request.url.path))
        return httpx.Response(
            403, json={"code": "noPermission", "message": "synthetic-private-upstream-body"}
        )

    gateway = DingTalkOpenAPIClient(settings_factory(), transport=httpx.MockTransport(handler))
    workflow = DingTalkWorkflowClient(gateway)
    state = FakeState(
        _job(
            ReimbursementSubmissionStatus.VERIFYING,
            upload=_upload(ReimbursementUploadStatus.COMMITTED),
            process_instance_id="synthetic-readback-instance",
            checkpointed=True,
        )
    )
    try:
        with capture_gateway_events(client.app.state.runtime_events):
            await _processor(state, workflow, FakeStorage()).process(
                _lease(ReimbursementSubmissionStatus.VERIFYING)
            )
            with pytest.raises(DingTalkOpenAPIError):
                await workflow.get_process_instance("synthetic-employee-instance")
        assert state.job.status is ReimbursementSubmissionStatus.MANUAL_REVIEW
        assert paths == [("GET", "/v1.0/workflow/processInstances")] * 2
        for module in ("submission", "approvals"):
            data = client.get(PATH, params={"module": module, "result": "failed"}).json()["data"]
            failures = [row for row in data["items"] if row["event"] == "upstream_failure"]
            assert len(failures) == 1
            assert failures[0]["details"]["upstreamOperation"] == "workflow_instance_read"
            assert "synthetic-readback-instance" not in json.dumps(data)
            assert "synthetic-private" not in json.dumps(data)
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "exception"])
async def test_submission_scope_is_task_local_and_restored_after_interruption(
    client_factory, settings_factory, interruption
):
    client = admin(client_factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        if request.url.params.get("processInstanceId") == "synthetic-delayed-oa":
            entered.set()
            await release.wait()
            raise RuntimeError("synthetic-private-interruption")
        return httpx.Response(403, json={"code": "noPermission"})

    gateway = DingTalkOpenAPIClient(settings_factory(), transport=httpx.MockTransport(handler))
    workflow = DingTalkWorkflowClient(gateway)
    state = FakeState(
        _job(
            ReimbursementSubmissionStatus.VERIFYING,
            upload=_upload(ReimbursementUploadStatus.COMMITTED),
            process_instance_id="synthetic-delayed-oa",
            checkpointed=True,
        )
    )

    async def run_and_resume():
        with pytest.raises(asyncio.CancelledError if interruption == "cancel" else RuntimeError):
            await _processor(state, workflow, FakeStorage()).process(
                _lease(ReimbursementSubmissionStatus.VERIFYING)
            )
        with pytest.raises(DingTalkOpenAPIError):
            await workflow.get_process_instance("synthetic-after-interruption")

    task = None
    try:
        with capture_gateway_events(client.app.state.runtime_events):
            task = asyncio.create_task(run_and_resume())
            await asyncio.wait_for(entered.wait(), 2)
            # This employee request overlaps the blocked OA task and must retain
            # its own module even while the OA task's context remains active.
            with pytest.raises(DingTalkOpenAPIError):
                await workflow.get_process_instance("synthetic-concurrent-employee")
            if interruption == "cancel":
                task.cancel()
            else:
                release.set()
            await asyncio.wait_for(task, 2)
        assert state.job.status is ReimbursementSubmissionStatus.VERIFYING
        data = client.get(PATH, params={"module": "approvals", "result": "failed"}).json()["data"]
        assert sum(row["event"] == "upstream_failure" for row in data["items"]) == 2
        data = client.get(PATH, params={"module": "submission", "result": "failed"}).json()["data"]
        assert not any(row["event"] == "upstream_failure" for row in data["items"])
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
