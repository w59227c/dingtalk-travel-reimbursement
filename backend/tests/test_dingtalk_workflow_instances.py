from __future__ import annotations

import json

import httpx
import pytest

from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIClient
from app.integrations.dingtalk.workflow import (
    CreateProcessInstanceCommand,
    CreateWorkflowFormValue,
    DingTalkProcessInstanceCreateOutcomeUnknown,
    DingTalkProcessInstanceCreateRejected,
    DingTalkWorkflowClient,
)

LIST_PATH = "/v1.0/workflow/processes/instanceIds/query"
INSTANCE_PATH = "/v1.0/workflow/processInstances"
SIGNED_INT64_MAX = 9_223_372_036_854_775_807
MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000


def workflow_client(settings_factory, handler):
    client = DingTalkOpenAPIClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
    )
    return client, DingTalkWorkflowClient(client)


def token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "private-access-token", "expires_in": 7200},
    )


def create_command() -> CreateProcessInstanceCommand:
    return CreateProcessInstanceCommand(
        process_code="PROC-REIMBURSEMENT",
        originator_user_id="employee-1",
        department_id=100,
        microapp_agent_id=1_234_567_890,
        form_values=(CreateWorkflowFormValue(name="字段", value="值"),),
    )


def process_instance_payload(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "title": "测试员工提交的境内出差申请",
        "businessId": "202609040001",
        "originatorUserId": "employee-1",
        "originatorDeptId": "100",
        "status": "COMPLETED",
        "result": "agree",
        "createTime": "2026-09-01T01:02Z",
        "finishTime": "2026-09-02T03:04Z",
        "formComponentValues": [
            {
                "id": "start-date-id",
                "name": "开始时间",
                "componentType": "DDDateField",
                "value": "2026-09-01",
                "extValue": "2026年9月1日",
                "bizAlias": "tripStart",
            },
            {
                "id": "budget-id",
                "name": "预算代码",
                "componentType": "DDSelectField",
                "value": "DEMO-001 项目",
            },
        ],
    }
    result.update(overrides)
    return {"success": True, "result": result}


@pytest.mark.asyncio
async def test_list_process_instance_ids_uses_read_query_contract_and_normalizes_page(
    settings_factory,
) -> None:
    calls = {"token": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return token_response()
        calls["list"] += 1
        assert request.method == "POST"
        assert request.url.path == LIST_PATH
        assert request.headers["x-acs-dingtalk-access-token"] == "private-access-token"
        assert json.loads(request.content) == {
            "processCode": "PROC-TRAVEL",
            "startTime": 1_777_500_000_000,
            "endTime": 1_777_586_399_999,
            "nextToken": 0,
            "maxResults": 20,
            "userIds": ["employee-1"],
            "statuses": ["COMPLETED"],
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"list": ["instance-1", "instance-2"], "nextToken": "20"},
            },
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        page = await workflow.list_process_instance_ids(
            process_code="PROC-TRAVEL",
            start_time=1_777_500_000_000,
            end_time=1_777_586_399_999,
            next_token=0,
            max_results=20,
            user_ids=("employee-1",),
            statuses=("COMPLETED",),
        )
    finally:
        await client.close()

    assert page.instance_ids == ("instance-1", "instance-2")
    assert page.next_token == 20
    assert calls == {"token": 1, "list": 1}


@pytest.mark.asyncio
async def test_list_process_instance_ids_missing_next_token_means_last_page(
    settings_factory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"success": True, "result": {"list": []}},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        page = await workflow.list_process_instance_ids(
            process_code="PROC-TRAVEL",
            start_time=1,
            end_time=2,
            next_token=0,
            max_results=20,
            user_ids=("employee-1",),
            statuses=("COMPLETED",),
        )
    finally:
        await client.close()

    assert page.instance_ids == ()
    assert page.next_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", ["rate_limit", "server", "timeout"])
async def test_list_process_instance_ids_retries_transient_read_post(
    settings_factory,
    failure_type: str,
) -> None:
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["list"] += 1
        if calls["list"] < 3:
            if failure_type == "timeout":
                raise httpx.ReadTimeout("private timeout", request=request)
            if failure_type == "rate_limit":
                return httpx.Response(429, json={"code": "Throttling.RateLimit"})
            return httpx.Response(503, json={"code": "ServiceUnavailable"})
        return httpx.Response(
            200,
            json={"success": True, "result": {"list": ["instance-1"]}},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        page = await workflow.list_process_instance_ids(
            process_code="PROC-TRAVEL",
            start_time=1,
            end_time=2,
            next_token=0,
            max_results=20,
            user_ids=("employee-1",),
            statuses=("COMPLETED",),
        )
    finally:
        await client.close()

    assert page.instance_ids == ("instance-1",)
    assert calls["list"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [400, 200])
async def test_list_process_instance_ids_maps_only_exact_invalid_process_code(
    settings_factory,
    http_status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            http_status,
            json={"success": False, "code": "invalidProcessCode"},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.list_process_instance_ids(
                process_code="PROC-TRAVEL",
                start_time=1,
                end_time=2,
                next_token=0,
                max_results=20,
                user_ids=("employee-1",),
                statuses=("COMPLETED",),
            )
    finally:
        await client.close()

    assert caught.value.code == "OA_TEMPLATE_PROCESS_CODE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "expected_code"),
    [
        (400, "DINGTALK_OPENAPI_FAILED"),
        (200, "DINGTALK_WORKFLOW_LIST_INVALID"),
    ],
)
async def test_list_process_instance_ids_does_not_overmatch_invalid_parameter_codes(
    settings_factory,
    http_status: int,
    expected_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            http_status,
            json={"success": False, "code": "invalidParameter.processCode"},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.list_process_instance_ids(
                process_code="PROC-TRAVEL",
                start_time=1,
                end_time=2,
                next_token=0,
                max_results=20,
                user_ids=("employee-1",),
                statuses=("COMPLETED",),
            )
    finally:
        await client.close()

    assert caught.value.code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "result": {"list": []}},
        {"success": True, "result": None},
        {"success": True, "result": {"list": "instance-1"}},
        {"success": True, "result": {"list": ["instance-1", "instance-1"]}},
        {"success": True, "result": {"list": [""]}},
        {"success": True, "result": {"list": [], "nextToken": 20}},
        {"success": True, "result": {"list": [], "nextToken": "0"}},
        {"success": True, "result": {"list": [], "nextToken": "not-a-number"}},
        {
            "success": True,
            "result": {"list": [], "nextToken": str(SIGNED_INT64_MAX + 1)},
        },
    ],
)
async def test_list_process_instance_ids_rejects_malformed_upstream_payload(
    settings_factory,
    payload: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(200, json=payload)

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.list_process_instance_ids(
                process_code="PROC-TRAVEL",
                start_time=1,
                end_time=2,
                next_token=0,
                max_results=20,
                user_ids=("employee-1",),
                statuses=("COMPLETED",),
            )
    finally:
        await client.close()

    assert caught.value.code == "DINGTALK_WORKFLOW_LIST_INVALID"
    assert caught.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_next_token", ["20", "19"])
async def test_list_process_instance_ids_rejects_nonadvancing_cursor(
    settings_factory,
    returned_next_token: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"list": ["instance-1"], "nextToken": returned_next_token},
            },
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.list_process_instance_ids(
                process_code="PROC-TRAVEL",
                start_time=1,
                end_time=2,
                next_token=20,
                max_results=20,
                user_ids=("employee-1",),
                statuses=("COMPLETED",),
            )
    finally:
        await client.close()

    assert caught.value.code == "DINGTALK_WORKFLOW_LIST_INVALID"


@pytest.mark.asyncio
async def test_get_process_instance_uses_query_and_normalizes_documented_fields(
    settings_factory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        assert request.method == "GET"
        assert request.url.path == INSTANCE_PATH
        assert dict(request.url.params) == {"processInstanceId": "instance-1"}
        return httpx.Response(200, json=process_instance_payload())

    client, workflow = workflow_client(settings_factory, handler)
    try:
        instance = await workflow.get_process_instance("instance-1")
    finally:
        await client.close()

    assert instance.instance_id == "instance-1"
    assert instance.title == "测试员工提交的境内出差申请"
    assert instance.business_id == "202609040001"
    assert instance.originator_user_id == "employee-1"
    assert instance.originator_department_id == "100"
    assert instance.status == "COMPLETED"
    assert instance.result == "agree"
    assert instance.created_at == "2026-09-01T01:02Z"
    assert instance.finished_at == "2026-09-02T03:04Z"
    assert instance.form_values[0].component_id == "start-date-id"
    assert instance.form_values[0].value == "2026-09-01"
    assert instance.form_values[0].ext_value == "2026年9月1日"
    assert not hasattr(instance, "process_code")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "result": {}},
        {"success": True, "result": None},
        process_instance_payload(title=""),
        process_instance_payload(originatorUserId=None),
        process_instance_payload(formComponentValues="bad"),
        process_instance_payload(
            formComponentValues=[
                {"id": "same-id", "name": "字段1", "value": "1"},
                {"id": "same-id", "name": "字段2", "value": "2"},
            ]
        ),
        process_instance_payload(formComponentValues=[{"name": "字段", "value": 3}]),
    ],
)
async def test_get_process_instance_rejects_malformed_upstream_payload(
    settings_factory,
    payload: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(200, json=payload)

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.get_process_instance("instance-1")
    finally:
        await client.close()

    assert caught.value.code == "DINGTALK_WORKFLOW_INSTANCE_INVALID"
    assert caught.value.status_code == 502


@pytest.mark.asyncio
async def test_create_process_instance_sends_only_template_driven_request_fields(
    settings_factory,
) -> None:
    calls = {"create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["create"] += 1
        assert request.method == "POST"
        assert request.url.path == INSTANCE_PATH
        body = json.loads(request.content)
        assert body == {
            "processCode": "PROC-REIMBURSEMENT",
            "originatorUserId": "employee-1",
            "deptId": 100,
            "microappAgentId": 1_234_567_890,
            "formComponentValues": [
                {
                    "id": "company-id",
                    "componentType": "DDSelectField",
                    "name": "所属公司",
                    "value": "北京",
                },
                {
                    "name": "关联审批单",
                    "value": '["travel-instance-1"]',
                },
            ],
        }
        assert not {"approvers", "ccList", "ccPosition", "targetSelectActioners"} & set(body)
        return httpx.Response(200, json={"instanceId": "created-instance-1"})

    client, workflow = workflow_client(settings_factory, handler)
    command = CreateProcessInstanceCommand(
        process_code="PROC-REIMBURSEMENT",
        originator_user_id="employee-1",
        department_id=100,
        microapp_agent_id=1_234_567_890,
        form_values=(
            CreateWorkflowFormValue(
                component_id="company-id",
                component_type="DDSelectField",
                name="所属公司",
                value="北京",
            ),
            CreateWorkflowFormValue(
                name="关联审批单",
                value='["travel-instance-1"]',
            ),
        ),
    )
    try:
        created = await workflow.create_process_instance(command)
    finally:
        await client.close()

    assert created.instance_id == "created-instance-1"
    assert calls["create"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "http_status"),
    [
        ("rate_limit", 429),
        ("server", 503),
        ("timeout", None),
        ("transport", None),
        ("invalid_success", 200),
    ],
)
async def test_create_process_instance_never_retries_unknown_outcome(
    settings_factory,
    failure_type: str,
    http_status: int | None,
) -> None:
    calls = {"create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["create"] += 1
        if failure_type == "timeout":
            raise httpx.ReadTimeout("private timeout", request=request)
        if failure_type == "transport":
            raise httpx.ConnectError("private transport error", request=request)
        if failure_type == "rate_limit":
            return httpx.Response(
                429,
                json={"code": "Throttling.RateLimit", "message": "private detail"},
            )
        if failure_type == "server":
            return httpx.Response(
                503,
                json={"code": "ServiceUnavailable", "message": "private detail"},
            )
        return httpx.Response(200, json={"unexpected": "private detail"})

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(DingTalkProcessInstanceCreateOutcomeUnknown) as caught:
            await workflow.create_process_instance(
                CreateProcessInstanceCommand(
                    process_code="PROC-REIMBURSEMENT",
                    originator_user_id="employee-1",
                    department_id=100,
                    microapp_agent_id=1_234_567_890,
                    form_values=(CreateWorkflowFormValue(name="字段", value="值"),),
                )
            )
    finally:
        await client.close()

    assert calls["create"] == 1
    assert caught.value.code == "OA_CREATE_OUTCOME_UNKNOWN"
    assert caught.value.http_status == http_status
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream_code",
    [
        "targetSelectApproverScopeError",
        "targetSelectApproverMissing",
        "invalidParameter",
        "processInstanceInvalidParameter",
        "needAuth",
        "invalidAgentId",
        "processCodeError",
        "processSetupNoPermission",
        "formConverterError",
        "illegalComponent",
    ],
)
async def test_create_process_instance_maps_explicit_precondition_rejection_codes(
    settings_factory,
    upstream_code: str,
) -> None:
    calls = {"create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["create"] += 1
        return httpx.Response(
            400,
            json={"code": upstream_code, "message": "private rejection detail"},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(DingTalkProcessInstanceCreateRejected) as caught:
            await workflow.create_process_instance(create_command())
    finally:
        await client.close()

    assert calls["create"] == 1
    assert caught.value.code == "OA_CREATE_REJECTED"
    assert caught.value.http_status == 400
    assert caught.value.upstream_code == upstream_code
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream_code",
    [
        "processGroupGetFailed",
        "processGetFailed",
        "processInstanceStartFailed",
        "sysErrror",
        "internalError",
        "unrecognizedCreateFailure",
        "invalidParameter.detail",
        None,
    ],
)
async def test_create_process_instance_treats_other_4xx_as_unknown_outcome(
    settings_factory,
    upstream_code: str | None,
) -> None:
    calls = {"create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["create"] += 1
        payload = {"message": "private failure detail"}
        if upstream_code is not None:
            payload["code"] = upstream_code
        return httpx.Response(400, json=payload)

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(DingTalkProcessInstanceCreateOutcomeUnknown) as caught:
            await workflow.create_process_instance(create_command())
    finally:
        await client.close()

    assert calls["create"] == 1
    assert caught.value.code == "OA_CREATE_OUTCOME_UNKNOWN"
    assert caught.value.http_status == 400
    assert caught.value.upstream_code == upstream_code
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)


@pytest.mark.asyncio
async def test_create_process_instance_maps_explicit_200_rejection_payload(
    settings_factory,
) -> None:
    calls = {"create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["create"] += 1
        return httpx.Response(
            200,
            json={"success": False, "code": "InvalidParameter"},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(DingTalkProcessInstanceCreateRejected) as caught:
            await workflow.create_process_instance(create_command())
    finally:
        await client.close()

    assert calls["create"] == 1
    assert caught.value.code == "OA_CREATE_REJECTED"
    assert caught.value.http_status == 200
    assert caught.value.upstream_code == "InvalidParameter"


@pytest.mark.asyncio
async def test_create_process_instance_preserves_401_and_never_replays_mutation(
    settings_factory,
) -> None:
    calls = {"token": 0, "create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return token_response()
        calls["create"] += 1
        return httpx.Response(401, json={"code": "InvalidAuthentication"})

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.create_process_instance(create_command())
    finally:
        await client.close()

    assert calls == {"token": 1, "create": 1}
    assert caught.value.code == "DINGTALK_OPENAPI_FAILED"
    assert getattr(caught.value, "http_status", None) == 401


@pytest.mark.asyncio
async def test_create_process_instance_preserves_actionable_permission_error(
    settings_factory,
) -> None:
    calls = {"token": 0, "create": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return token_response()
        calls["create"] += 1
        return httpx.Response(
            403,
            json={"code": "Forbidden.AccessDenied.PermissionDenied"},
        )

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ApiError) as caught:
            await workflow.create_process_instance(create_command())
    finally:
        await client.close()

    assert calls == {"token": 1, "create": 1}
    assert caught.value.code == "DINGTALK_PERMISSION_MISSING"


@pytest.mark.asyncio
async def test_workflow_instance_methods_validate_commands_before_network(
    settings_factory,
) -> None:
    calls = {"network": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["network"] += 1
        return token_response()

    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ValueError):
            await workflow.list_process_instance_ids(
                process_code="PROC-TRAVEL",
                start_time=2,
                end_time=1,
                next_token=0,
                max_results=20,
                user_ids=("employee-1",),
                statuses=("COMPLETED",),
            )
        with pytest.raises(ValueError):
            await workflow.get_process_instance(" ")
        with pytest.raises(ValueError):
            await workflow.create_process_instance(
                CreateProcessInstanceCommand(
                    process_code="PROC-REIMBURSEMENT",
                    originator_user_id="employee-1",
                    department_id=0,
                    microapp_agent_id=1_234_567_890,
                    form_values=(CreateWorkflowFormValue(name="字段", value="值"),),
                )
            )
    finally:
        await client.close()

    assert calls["network"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"start_time": 2, "end_time": 1},
        {"start_time": 0, "end_time": 120 * MILLISECONDS_PER_DAY + 1},
        {"start_time": SIGNED_INT64_MAX + 1, "end_time": SIGNED_INT64_MAX + 1},
        {"start_time": 0, "end_time": SIGNED_INT64_MAX + 1},
        {"next_token": SIGNED_INT64_MAX + 1},
    ],
)
async def test_list_process_instance_ids_rejects_invalid_bounds_before_network(
    settings_factory,
    overrides: dict[str, int],
) -> None:
    calls = {"network": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["network"] += 1
        return token_response()

    arguments = {
        "process_code": "PROC-TRAVEL",
        "start_time": 1,
        "end_time": 2,
        "next_token": 0,
        "max_results": 20,
        "user_ids": ("employee-1",),
        "statuses": ("COMPLETED",),
    }
    arguments.update(overrides)
    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ValueError):
            await workflow.list_process_instance_ids(**arguments)
    finally:
        await client.close()

    assert calls["network"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["department_id", "microapp_agent_id"])
async def test_create_process_instance_rejects_signed_int64_overflow_before_network(
    settings_factory,
    field_name: str,
) -> None:
    calls = {"network": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["network"] += 1
        return token_response()

    command_values = {
        "process_code": "PROC-REIMBURSEMENT",
        "originator_user_id": "employee-1",
        "department_id": 100,
        "microapp_agent_id": 1_234_567_890,
        "form_values": (CreateWorkflowFormValue(name="字段", value="值"),),
    }
    command_values[field_name] = SIGNED_INT64_MAX + 1
    client, workflow = workflow_client(settings_factory, handler)
    try:
        with pytest.raises(ValueError):
            await workflow.create_process_instance(CreateProcessInstanceCommand(**command_values))
    finally:
        await client.close()

    assert calls["network"] == 0
