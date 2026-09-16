from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable

import httpx
import pytest
from conftest import mock_login

from app.api import oa_templates
from app.core.errors import ApiError
from app.integrations.dingtalk.client import DingTalkOpenAPIClient
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient, normalize_form_schema
from app.models.oa_template_profile import OaTemplateProfile
from app.services import oa_template_profiles
from app.services.oa_template_profiles import load_fresh_submission_template

PROCESS_CODE = "PROC-TEST-REIMBURSEMENT"
TRAVEL_PROCESS_CODES = ["PROC-TRAVEL-DOMESTIC", "PROC-TRAVEL-INTERNATIONAL"]
TRAVEL_TABLE_COMPONENT_ID = "TableField-J8TW2TVT"

MAPPINGS = {
    "company": "company-id",
    "budgetCode": "budget-id",
    "travelType": "travel-type-id",
    "startDate": "start-id",
    "endDate": "end-id",
    "durationDays": "duration-id",
    "description": "description-id",
    "reimbursementAmount": "amount-id",
    "relatedApprovals": "related-id",
    "attachments": "attachment-id",
}


def component(
    component_type: str,
    component_id: str,
    label: str,
    *,
    options: list[object] | None = None,
    required: bool = True,
    **props: object,
) -> dict[str, object]:
    return {
        "componentName": component_type,
        "props": {
            "id": component_id,
            "label": label,
            "required": required,
            **({"options": options} if options is not None else {}),
            **props,
        },
    }


def schema_payload(
    *,
    modified_at: str = "2026-09-03T10:00:00+08:00",
    company_options: list[object] | None = None,
) -> dict[str, object]:
    if company_options is None:
        company_options = [
            json.dumps({"value": "北京", "key": "option_0"}, ensure_ascii=False),
            json.dumps({"value": "无锡", "key": "option_1"}, ensure_ascii=False),
        ]
    items = [
        component("DDSelectField", "company-id", "所属公司", options=company_options),
        component(
            "DDSelectField",
            "budget-id",
            "预算代码",
            options=[json.dumps({"value": "10000", "key": "budget_0"})],
        ),
        component(
            "DDSelectField",
            "travel-type-id",
            "出差类别",
            options=["商务出差"],
        ),
        component("DDDateField", "start-id", "开始时间", format="yyyy-MM-dd"),
        component("DDDateField", "end-id", "结束时间", format="yyyy-MM-dd"),
        component("NumberField", "duration-id", "时长（天）", unit="天"),
        component("TextareaField", "description-id", "明细说明"),
        component("NumberField", "amount-id", "报销金额", unit="元"),
        component("RelateField", "related-id", "关联审批单"),
        component("DDAttachment", "attachment-id", "附件"),
    ]
    return {
        "result": {
            "formCode": PROCESS_CODE,
            "formUuid": "form-uuid-1",
            "name": "差旅费报销申请",
            "status": "PUBLISHED",
            "gmtModified": modified_at,
            "schemaContent": {"title": "差旅费报销申请", "items": items},
        }
    }


def travel_schema_payload(
    process_code: str,
    *,
    modified_at: str = "2026-09-03T11:00:00+08:00",
    start_component_type: str = "DDDateField",
    start_format: str = "yyyy-MM-dd",
) -> dict[str, object]:
    return {
        "result": {
            "formCode": process_code,
            "formUuid": f"form-uuid-{process_code.lower()}",
            "name": f"{process_code} 出差申请",
            "status": "PUBLISHED",
            "gmtModified": modified_at,
            "schemaContent": {
                "title": f"{process_code} 出差申请",
                "items": [
                    component(
                        start_component_type,
                        "travel-start-id",
                        "开始日期",
                        format=start_format,
                    ),
                    component(
                        "DDDateField",
                        "travel-end-id",
                        "结束日期",
                        format="yyyy-MM-dd",
                    ),
                    component("TextField", "travel-reason-id", "出差事由"),
                ],
            },
        }
    }


def travel_table_schema_payload(
    process_code: str,
    *,
    component_type: str,
) -> dict[str, object]:
    return {
        "result": {
            "formCode": process_code,
            "formUuid": f"form-uuid-{process_code.lower()}",
            "name": f"{process_code} 出差申请",
            "status": "PUBLISHED",
            "gmtModified": "2026-09-03T11:00:00+08:00",
            "schemaContent": {
                "title": f"{process_code} 出差申请",
                "items": [
                    {
                        "componentName": "DDBizSuite",
                        "props": {
                            "id": "DDBizSuite-J4NUHWH9",
                            "label": "商旅出差",
                            "bizAlias": "tripSuite",
                        },
                        "children": [
                            component(
                                component_type,
                                TRAVEL_TABLE_COMPONENT_ID,
                                "出差明细",
                            )
                        ],
                    }
                ],
            },
        }
    }


def travel_fingerprint(process_code: str) -> str:
    return normalize_form_schema(
        process_code,
        travel_schema_payload(process_code),
    ).fingerprint


def transport_for_schema(
    response_factory: Callable[[int, httpx.Request], httpx.Response],
    *,
    travel_response_factory: Callable[[str, httpx.Request], httpx.Response] | None = None,
) -> tuple[httpx.MockTransport, dict[str, int]]:
    calls = {"token": 0, "schema": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            body = json.loads(request.content.decode())
            assert body == {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "client_credentials",
            }
            return httpx.Response(
                200,
                json={"access_token": f"access-{calls['token']}", "expires_in": 7200},
            )

        assert request.method == "GET"
        assert request.url.path == "/v1.0/workflow/forms/schemas/processCodes"
        process_code = request.url.params["processCode"]
        assert "access_token" not in request.url.params
        assert request.headers["x-acs-dingtalk-access-token"].startswith("access-")
        if process_code in TRAVEL_PROCESS_CODES:
            if travel_response_factory is not None:
                return travel_response_factory(process_code, request)
            return httpx.Response(200, json=travel_schema_payload(process_code))
        assert process_code == PROCESS_CODE
        calls["schema"] += 1
        return response_factory(calls["schema"], request)

    return httpx.MockTransport(handler), calls


class GatedSchemaTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        payloads: list[dict[str, object]],
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.payloads = payloads
        self.started = started
        self.release = release
        self.schema_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={"access_token": "slow-access-token", "expires_in": 7200},
            )
        process_code = request.url.params["processCode"]
        if process_code in TRAVEL_PROCESS_CODES:
            return httpx.Response(200, json=travel_schema_payload(process_code))
        self.schema_calls += 1
        self.started.set()
        await self.release.wait()
        payload_index = min(self.schema_calls - 1, len(self.payloads) - 1)
        return httpx.Response(200, json=self.payloads[payload_index])


def admin_client(client_factory, transport: httpx.MockTransport):
    client = client_factory(
        transport=transport,
        auth_mock_enabled=True,
        auth_mock_user_id="admin-1",
        admin_user_ids="admin-1",
    )
    csrf = mock_login(client)["csrfToken"]
    return client, {"X-CSRF-Token": csrf}


def inspect(client, headers: dict[str, str]):
    return client.post(
        "/api/admin/oa/templates/catalog/inspect",
        json={
            "reimbursementProcessCode": PROCESS_CODE,
            "travelProfiles": [
                {
                    "profileKey": f"travel-{index}",
                    "displayName": f"出差模板 {index}",
                    "processCode": process_code,
                }
                for index, process_code in enumerate(TRAVEL_PROCESS_CODES)
            ],
        },
        headers=headers,
    )


def confirm(
    client,
    headers: dict[str, str],
    fingerprint: str,
    mappings=None,
    *,
    allowed_travel_process_codes=None,
    expected_config_version: int | None = None,
    travel_schema_fingerprints: dict[str, str] | None = None,
    travel_mappings: dict[str, dict[str, str]] | None = None,
    travel_type_options: dict[str, dict[str, str | None]] | None = None,
):
    return client.put(
        "/api/admin/oa/templates/catalog",
        json={
            "expectedConfigVersion": expected_config_version,
            "reimbursement": {
                "processCode": PROCESS_CODE,
                "schemaFingerprint": fingerprint,
                "mappings": mappings or MAPPINGS,
            },
            "travelProfiles": [
                {
                    "profileKey": f"travel-{index}",
                    "displayName": f"出差模板 {index}",
                    "processCode": process_code,
                    "schemaFingerprint": (travel_schema_fingerprints or {}).get(
                        process_code,
                        travel_fingerprint(process_code),
                    ),
                    "mappings": (travel_mappings or {}).get(
                        process_code,
                        {
                            "startDate": "travel-start-id",
                            "endDate": "travel-end-id",
                        },
                    ),
                    "travelTypeOption": (travel_type_options or {}).get(
                        process_code,
                        {
                            "value": "商务出差",
                            "label": "商务出差",
                            "key": None,
                        },
                    ),
                }
                for index, process_code in enumerate(
                    allowed_travel_process_codes
                    if allowed_travel_process_codes is not None
                    else TRAVEL_PROCESS_CODES
                )
            ],
        },
        headers=headers,
    )


def test_travel_accounting_mappings_survive_catalog_api_confirmation(client_factory) -> None:
    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        payload = travel_schema_payload(process_code)
        payload["result"]["schemaContent"]["items"].extend(
            [
                component("TextField", f"{process_code}-company", "所属公司"),
                component("TextField", f"{process_code}-budget", "预算代码"),
            ]
        )
        return httpx.Response(200, json=payload)

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)
    response = inspect(client, headers)
    assert response.status_code == 200, response.text
    inspection = response.json()["data"]
    mappings = {
        code: {
            "startDate": "travel-start-id",
            "endDate": "travel-end-id",
            "company": f"{code}-company",
            "budgetCode": f"{code}-budget",
        }
        for code in TRAVEL_PROCESS_CODES
    }
    confirmed = confirm(
        client,
        headers,
        inspection["reimbursement"]["schema"]["schemaFingerprint"],
        travel_schema_fingerprints={
            item["processCode"]: item["schema"]["schemaFingerprint"]
            for item in inspection["travelProfiles"]
        },
        travel_mappings=mappings,
    )
    assert confirmed.status_code == 200, confirmed.text
    current = client.get("/api/admin/oa/templates/catalog")
    assert current.status_code == 200, current.text
    for profile in current.json()["data"]["catalog"]["travelProfiles"]:
        assert profile["mappings"] == mappings[profile["processCode"]]


def test_inspect_uses_official_signature_and_normalizes_safe_schema(client_factory) -> None:
    payload = schema_payload()
    payload["result"]["schemaContent"]["items"][0]["props"]["uploadUrl"] = (
        "https://secret-upload.invalid/signed"
    )
    transport, calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)

    response = inspect(client, headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["compatibilityStatus"] == "UNCONFIGURED"
    assert data["isSubmissionReady"] is False
    assert data["reimbursement"]["schema"]["formUuid"] == "form-uuid-1"
    assert data["reimbursement"]["schema"]["modifiedAt"] == "2026-09-03T10:00:00+08:00"
    logical_keys = {item["key"] for item in data["reimbursement"]["logicalFields"]}
    assert {"startDate", "endDate"} <= logical_keys
    assert not {"startTime", "endTime"} & logical_keys
    company = data["reimbursement"]["schema"]["components"][0]
    assert company["options"] == [
        {"value": "北京", "label": "北京", "key": "option_0"},
        {"value": "无锡", "label": "无锡", "key": "option_1"},
    ]
    related = next(
        item
        for item in data["reimbursement"]["schema"]["components"]
        if item["componentId"] == "related-id"
    )
    assert related["relatedTemplatePolicy"] == {"mode": "UNKNOWN", "processCodes": []}
    assert "relatedApprovals" in related["compatibleLogicalFields"]
    assert "secret-upload" not in response.text
    assert "client-secret" not in response.text
    assert "access-1" not in response.text
    assert calls == {"token": 1, "schema": 1}


@pytest.mark.parametrize("component_type", ("TableField", "DDTableField"))
def test_travel_dates_can_share_one_top_level_table_through_confirm_and_reload(
    client_factory,
    component_type: str,
) -> None:
    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=travel_table_schema_payload(
                process_code,
                component_type=component_type,
            ),
        )

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)

    inspected = inspect(client, headers)

    assert inspected.status_code == 200, inspected.text
    inspection = inspected.json()["data"]
    fingerprints: dict[str, str] = {}
    table_mappings: dict[str, dict[str, str]] = {}
    for profile in inspection["travelProfiles"]:
        table = next(
            component
            for component in profile["schema"]["components"]
            if component["componentId"] == TRAVEL_TABLE_COMPONENT_ID
        )
        assert table["componentType"] == component_type
        assert table["nested"] is True
        assert table["inSubtable"] is False
        assert table["unsupportedContainerAncestor"] is True
        assert table["parentComponentId"] == "DDBizSuite-J4NUHWH9"
        assert table["compatibleLogicalFields"] == ["startDate", "endDate"]
        fingerprints[profile["processCode"]] = profile["schema"]["schemaFingerprint"]
        table_mappings[profile["processCode"]] = {
            "startDate": TRAVEL_TABLE_COMPONENT_ID,
            "endDate": TRAVEL_TABLE_COMPONENT_ID,
        }

    confirmed = confirm(
        client,
        headers,
        inspection["reimbursement"]["schema"]["schemaFingerprint"],
        travel_schema_fingerprints=fingerprints,
        travel_mappings=table_mappings,
    )
    current = client.get("/api/admin/oa/templates/catalog")

    assert confirmed.status_code == 200, confirmed.text
    assert current.status_code == 200, current.text
    assert current.json()["data"]["isSubmissionReady"] is True
    assert all(
        profile["mappings"]
        == {
            "startDate": TRAVEL_TABLE_COMPONENT_ID,
            "endDate": TRAVEL_TABLE_COMPONENT_ID,
        }
        for profile in current.json()["data"]["catalog"]["travelProfiles"]
    )

    async def reload_contract():
        return await load_fresh_submission_template(
            client.app.state.database_session_factory,
            client.app.state.dingtalk_workflow,
        )

    reloaded = asyncio.run(reload_contract())
    assert all(
        profile.start_date_component_id == TRAVEL_TABLE_COMPONENT_ID
        and profile.end_date_component_id == TRAVEL_TABLE_COMPONENT_ID
        for profile in reloaded.travel_profiles
    )


def test_travel_dates_cannot_share_one_direct_date_component(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
        travel_mappings={
            TRAVEL_PROCESS_CODES[0]: {
                "startDate": "travel-start-id",
                "endDate": "travel-start-id",
            }
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"
    assert "同一个 OA 控件" in response.json()["error"]["message"]


def test_travel_dates_cannot_mix_a_table_with_a_direct_date_component(
    client_factory,
) -> None:
    mixed = travel_table_schema_payload(
        TRAVEL_PROCESS_CODES[0],
        component_type="TableField",
    )
    mixed["result"]["schemaContent"]["items"].append(
        component(
            "DDDateField",
            "travel-end-id",
            "结束日期",
            format="yyyy-MM-dd",
        )
    )

    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        payload = (
            mixed
            if process_code == TRAVEL_PROCESS_CODES[0]
            else travel_schema_payload(process_code)
        )
        return httpx.Response(200, json=payload)

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]
    fingerprints = {
        profile["processCode"]: profile["schema"]["schemaFingerprint"]
        for profile in inspected["travelProfiles"]
    }

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
        travel_schema_fingerprints=fingerprints,
        travel_mappings={
            TRAVEL_PROCESS_CODES[0]: {
                "startDate": TRAVEL_TABLE_COMPONENT_ID,
                "endDate": "travel-end-id",
            }
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"
    assert "同一个表格控件" in response.json()["error"]["message"]


def test_schema_read_refreshes_one_rejected_access_token(client_factory) -> None:
    def response_factory(number: int, request: httpx.Request) -> httpx.Response:
        if number == 1:
            assert request.headers["x-acs-dingtalk-access-token"] == "access-1"
            return httpx.Response(401, json={"code": "InvalidAuthentication"})
        assert request.headers["x-acs-dingtalk-access-token"] == "access-2"
        return httpx.Response(200, json=schema_payload())

    transport, calls = transport_for_schema(response_factory)
    client, headers = admin_client(client_factory, transport)

    response = inspect(client, headers)

    assert response.status_code == 200
    assert calls == {"token": 2, "schema": 2}


def test_schema_permission_error_is_safe(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(
            403,
            json={
                "code": "Forbidden.AccessDenied.AccessTokenPermissionDenied",
                "message": "sensitive upstream permission detail",
            },
        )
    )
    client, headers = admin_client(client_factory, transport)

    response = inspect(client, headers)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_PERMISSION_MISSING"
    assert "sensitive" not in response.text


@pytest.mark.parametrize(
    ("upstream_status", "upstream_code"),
    [
        (400, "formNotExist"),
        (400, "aflowProcessCodeIsError"),
        (400, "InvalidParameter"),
        (400, "invalidParameter.processCode"),
        (200, "formNotExist"),
    ],
)
def test_invalid_process_code_has_a_stable_actionable_error(
    client_factory,
    upstream_status,
    upstream_code,
) -> None:
    transport, calls = transport_for_schema(
        lambda _number, _request: httpx.Response(
            upstream_status,
            json={
                "code": upstream_code,
                "message": "sensitive upstream template detail",
            },
        )
    )
    client, headers = admin_client(client_factory, transport)

    response = inspect(client, headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_PROCESS_CODE_INVALID"
    assert "processCode" in response.json()["error"]["message"]
    assert "sensitive" not in response.text
    assert calls == {"token": 1, "schema": 1}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("result"),
        lambda payload: payload["result"].update({"status": "DISABLED"}),
        lambda payload: payload["result"].update({"formCode": "PROC-OTHER"}),
        lambda payload: payload["result"]["schemaContent"].update({"items": "invalid"}),
        lambda payload: payload["result"]["schemaContent"]["items"].append(
            component("TextField", "company-id", "重复 ID")
        ),
    ],
)
def test_malformed_or_mismatched_schema_fails_closed(client_factory, mutate) -> None:
    payload = schema_payload()
    mutate(payload)
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)

    response = inspect(client, headers)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_FORM_SCHEMA_INVALID"


def test_template_administration_requires_current_admin_and_csrf(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    anonymous = client_factory(transport=transport, auth_mock_enabled=True)
    assert anonymous.get("/api/admin/oa/templates/catalog").status_code == 401
    assert (
        anonymous.post(
            "/api/admin/oa/templates/catalog/inspect",
            json={
                "reimbursementProcessCode": PROCESS_CODE,
                "travelProfiles": [
                    {
                        "profileKey": "travel-0",
                        "displayName": "境内出差",
                        "processCode": TRAVEL_PROCESS_CODES[0],
                    }
                ],
            },
        ).status_code
        == 401
    )

    login = mock_login(anonymous)
    assert anonymous.get("/api/admin/oa/templates/catalog").status_code == 403
    assert inspect(anonymous, {"X-CSRF-Token": login["csrfToken"]}).status_code == 403

    admin, headers = admin_client(client_factory, transport)
    assert inspect(admin, {}).status_code == 403
    inspected = inspect(admin, headers)
    fingerprint = inspected.json()["data"]["reimbursement"]["schema"]["schemaFingerprint"]
    assert confirm(admin, {}, fingerprint).status_code == 403


def test_remote_catalog_reads_start_without_the_auth_database_transaction(
    client_factory,
    monkeypatch,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    observed: list[str] = []

    async def fake_inspect(database, _workflow, **_kwargs):
        assert database.in_transaction() is False
        observed.append("inspect")
        return {}

    async def fake_confirm(database, _workflow, **kwargs):
        assert database.in_transaction() is False
        assert kwargs["administrator_user_id"] == "admin-1"
        observed.append("confirm")
        return {}

    monkeypatch.setattr(oa_templates, "inspect_template_catalog", fake_inspect)
    inspected = inspect(client, headers)
    monkeypatch.setattr(oa_templates, "confirm_template_catalog", fake_confirm)
    confirmed = confirm(client, headers, "a" * 64)

    assert inspected.status_code == 200
    assert confirmed.status_code == 200
    assert observed == ["inspect", "confirm"]


def test_inspection_never_returns_a_new_catalog_version_with_stale_mappings(
    client_factory,
    monkeypatch,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    monkeypatch.setattr(
        oa_template_profiles,
        "_replace_compatible_catalog",
        lambda *_args, **_kwargs: False,
    )

    response = inspect(client, headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OA_TEMPLATE_CONFIGURATION_CHANGED"


@pytest.mark.parametrize(
    ("mapping_change", "message"),
    [
        ({"company": "missing-id"}, "控件不存在"),
        ({"budgetCode": "company-id"}, "不能对应多个"),
        ({"company": "start-id"}, "不支持控件类型"),
    ],
)
def test_mapping_rejects_unknown_duplicate_and_wrong_type(
    client_factory, mapping_change, message
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]

    response = confirm(client, headers, fingerprint, MAPPINGS | mapping_change)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"
    assert message in response.json()["error"]["message"]


def test_mapping_rejects_unmapped_required_component_and_empty_select(client_factory) -> None:
    extra_required = schema_payload()
    extra_required["result"]["schemaContent"]["items"].append(
        component("TextField", "extra-required", "额外必填字段")
    )
    payloads = [schema_payload(company_options=[]), extra_required]

    for payload, expected in [(payloads[0], "没有可用选项"), (payloads[1], "未映射的必填控件")]:
        transport, _calls = transport_for_schema(
            lambda _number, _request, current=payload: httpx.Response(200, json=current)
        )
        client, headers = admin_client(client_factory, transport)
        fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
            "schemaFingerprint"
        ]
        response = confirm(client, headers, fingerprint)
        assert response.status_code == 400
        assert expected in response.json()["error"]["message"]


def test_mapping_rejects_unmapped_required_component_in_visible_container(
    client_factory,
) -> None:
    payload = schema_payload()
    payload["result"]["schemaContent"]["items"].append(
        {
            "componentName": "FieldGroup",
            "props": {},
            "children": [component("TextField", "nested-required", "容器内必填字段")],
        }
    )
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]

    response = confirm(client, headers, fingerprint)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"
    assert "容器内必填字段" in response.json()["error"]["message"]


def test_schema_rejects_malformed_json_or_duplicate_select_options(client_factory) -> None:
    payloads = [
        schema_payload(company_options=['{"value":"北京"']),
        schema_payload(
            company_options=[
                json.dumps({"value": "北京", "key": "option_0"}, ensure_ascii=False),
                json.dumps({"value": "北京", "key": "option_1"}, ensure_ascii=False),
            ]
        ),
    ]

    for payload in payloads:
        transport, _calls = transport_for_schema(
            lambda _number, _request, current=payload: httpx.Response(200, json=current)
        )
        client, headers = admin_client(client_factory, transport)
        response = inspect(client, headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "DINGTALK_FORM_SCHEMA_INVALID"


def test_mapping_rejects_an_unsupported_date_format(client_factory) -> None:
    payload = schema_payload()
    start = payload["result"]["schemaContent"]["items"][3]
    start["props"]["format"] = "yyyy-MM-dd HH:mm"
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]
    start_component = next(
        item
        for item in inspected["reimbursement"]["schema"]["components"]
        if item["componentId"] == "start-id"
    )
    assert "startDate" not in start_component["compatibleLogicalFields"]
    fingerprint = inspected["reimbursement"]["schema"]["schemaFingerprint"]

    response = confirm(client, headers, fingerprint)

    assert response.status_code == 400
    assert "yyyy-MM-dd" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("wrapper_state", "ancestor_flag"),
    [({"hidden": True}, "ancestorHidden"), ({"disabled": True}, "ancestorDisabled")],
)
def test_idless_container_state_is_propagated_and_blocks_descendant_mapping(
    client_factory, wrapper_state, ancestor_flag
) -> None:
    payload = schema_payload()
    items = payload["result"]["schemaContent"]["items"]
    company = items[0]
    items[0] = {
        "componentName": "FieldGroup",
        "props": wrapper_state,
        "children": [company],
    }
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)

    inspected = inspect(client, headers).json()["data"]
    normalized = next(
        item
        for item in inspected["reimbursement"]["schema"]["components"]
        if item["componentId"] == "company-id"
    )
    assert normalized["parentComponentId"] is None
    assert normalized["nested"] is True
    assert normalized[ancestor_flag] is True
    assert normalized["compatibleLogicalFields"] == []

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"


def test_ordinary_field_group_allows_mapping_visible_leaf_component(client_factory) -> None:
    payload = schema_payload()
    items = payload["result"]["schemaContent"]["items"]
    company = items[0]
    items[0] = {
        "componentName": "FieldGroup",
        "props": {},
        "children": [company],
    }
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)

    inspected = inspect(client, headers).json()["data"]
    normalized = next(
        item
        for item in inspected["reimbursement"]["schema"]["components"]
        if item["componentId"] == "company-id"
    )
    assert normalized["nested"] is True
    assert normalized["unsupportedContainerAncestor"] is False
    assert "company" in normalized["compatibleLogicalFields"]

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
    )

    assert response.status_code == 200


def test_idless_table_container_marks_and_blocks_subtable_descendant(client_factory) -> None:
    payload = schema_payload()
    items = payload["result"]["schemaContent"]["items"]
    description = items[6]
    items[6] = {
        "componentName": "TableField",
        "props": {},
        "children": [description],
    }
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)

    inspected = inspect(client, headers).json()["data"]
    normalized = next(
        item
        for item in inspected["reimbursement"]["schema"]["components"]
        if item["componentId"] == "description-id"
    )
    assert normalized["parentComponentId"] is None
    assert normalized["nested"] is True
    assert normalized["inSubtable"] is True
    assert normalized["unsupportedContainerAncestor"] is True
    assert normalized["compatibleLogicalFields"] == []

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
    )
    assert response.status_code == 400
    assert "子控件" in response.json()["error"]["message"]


def test_unknown_business_suite_container_blocks_descendant_mapping(client_factory) -> None:
    payload = schema_payload()
    items = payload["result"]["schemaContent"]["items"]
    company = items[0]
    items[0] = {
        "componentName": "TravelBusinessSuite",
        "props": {},
        "children": [company],
    }
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)

    inspected = inspect(client, headers).json()["data"]
    normalized = next(
        item
        for item in inspected["reimbursement"]["schema"]["components"]
        if item["componentId"] == "company-id"
    )
    assert normalized["inSubtable"] is False
    assert normalized["unsupportedContainerAncestor"] is True
    assert normalized["compatibleLogicalFields"] == []

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
    )

    assert response.status_code == 400
    assert "复杂业务组件" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("allowed_process_codes", "status_code", "message"),
    [
        ([TRAVEL_PROCESS_CODES[0], TRAVEL_PROCESS_CODES[0]], 400, "不能重复"),
        ([PROCESS_CODE], 400, "不能同时作为"),
        (["invalid process code"], 422, "请求参数不正确"),
    ],
)
def test_relationship_requires_valid_local_allowlist(
    client_factory, allowed_process_codes, status_code, message
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]

    response = confirm(
        client,
        headers,
        fingerprint,
        allowed_travel_process_codes=allowed_process_codes,
    )

    assert response.status_code == status_code
    assert message in response.text


@pytest.mark.parametrize(
    "travel_profiles",
    [
        [
            {
                "profileKey": "travel-0",
                "displayName": "出差模板 0",
                "processCode": TRAVEL_PROCESS_CODES[0],
            }
        ],
        [
            {
                "profileKey": "renamed-travel",
                "displayName": "出差模板 0",
                "processCode": TRAVEL_PROCESS_CODES[0],
            },
            {
                "profileKey": "travel-1",
                "displayName": "出差模板 1",
                "processCode": TRAVEL_PROCESS_CODES[1],
            },
        ],
        [
            {
                "profileKey": "travel-0",
                "displayName": "出差模板 0",
                "processCode": TRAVEL_PROCESS_CODES[1],
            },
            {
                "profileKey": "travel-1",
                "displayName": "出差模板 1",
                "processCode": TRAVEL_PROCESS_CODES[0],
            },
        ],
    ],
)
def test_inspection_reports_a_changed_catalog(
    client_factory,
    travel_profiles,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    initial = inspect(client, headers).json()["data"]
    assert (
        confirm(
            client,
            headers,
            initial["reimbursement"]["schema"]["schemaFingerprint"],
        ).status_code
        == 200
    )

    unchanged = inspect(client, headers)
    changed = client.post(
        "/api/admin/oa/templates/catalog/inspect",
        json={
            "reimbursementProcessCode": PROCESS_CODE,
            "travelProfiles": travel_profiles,
        },
        headers=headers,
    )

    assert unchanged.status_code == 200
    assert unchanged.json()["data"]["compatibilityStatus"] == "COMPATIBLE"
    assert changed.status_code == 200
    assert changed.json()["data"]["compatibilityStatus"] == "CATALOG_CHANGED"


@pytest.mark.parametrize(
    ("start_component_type", "start_format", "message"),
    [
        ("TextField", "yyyy-MM-dd", "不支持控件类型"),
        ("DDDateField", "yyyy-MM-dd HH:mm", "yyyy-MM-dd"),
    ],
)
def test_travel_profile_maps_only_supported_date_controls(
    client_factory,
    start_component_type,
    start_format,
    message,
) -> None:
    altered = travel_schema_payload(
        TRAVEL_PROCESS_CODES[0],
        start_component_type=start_component_type,
        start_format=start_format,
    )

    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        payload = (
            altered
            if process_code == TRAVEL_PROCESS_CODES[0]
            else travel_schema_payload(process_code)
        )
        return httpx.Response(200, json=payload)

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]
    travel_fingerprints = {
        item["processCode"]: item["schema"]["schemaFingerprint"]
        for item in inspected["travelProfiles"]
    }

    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
        travel_schema_fingerprints=travel_fingerprints,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"
    assert message in response.json()["error"]["message"]


def test_travel_profile_requires_an_exact_reimbursement_travel_type_option(
    client_factory,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]

    response = confirm(
        client,
        headers,
        fingerprint,
        travel_type_options={
            TRAVEL_PROCESS_CODES[0]: {
                "value": "商务出差",
                "label": "商务出差",
                "key": "different-key",
            }
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OA_TEMPLATE_MAPPING_INVALID"
    assert "选项已失效" in response.json()["error"]["message"]


def test_catalog_confirmation_is_atomic_when_a_travel_schema_read_fails(
    client_factory,
) -> None:
    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        if process_code == TRAVEL_PROCESS_CODES[1]:
            return httpx.Response(
                403,
                json={"code": "Forbidden.AccessDenied.AccessTokenPermissionDenied"},
            )
        return httpx.Response(200, json=travel_schema_payload(process_code))

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)

    response = confirm(
        client,
        headers,
        normalize_form_schema(PROCESS_CODE, schema_payload()).fingerprint,
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "DINGTALK_PERMISSION_MISSING"
    with client.app.state.database_session_factory() as database:
        assert database.get(OaTemplateProfile, "reimbursement") is None


def test_failed_catalog_reconfirmation_preserves_the_previous_whole_catalog(
    client_factory,
) -> None:
    fail_second_travel = False

    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        if fail_second_travel and process_code == TRAVEL_PROCESS_CODES[1]:
            return httpx.Response(
                403,
                json={"code": "Forbidden.AccessDenied.AccessTokenPermissionDenied"},
            )
        return httpx.Response(200, json=travel_schema_payload(process_code))

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]
    assert (
        confirm(
            client,
            headers,
            inspected["reimbursement"]["schema"]["schemaFingerprint"],
        ).status_code
        == 200
    )
    with client.app.state.database_session_factory() as database:
        before = database.get(OaTemplateProfile, "reimbursement")
        assert before is not None
        before_travel_profiles = before.travel_profiles_json

    fail_second_travel = True
    response = confirm(
        client,
        headers,
        inspected["reimbursement"]["schema"]["schemaFingerprint"],
        expected_config_version=1,
    )

    assert response.status_code == 502
    with client.app.state.database_session_factory() as database:
        after = database.get(OaTemplateProfile, "reimbursement")
        assert after is not None
        assert after.config_version == 1
        assert after.travel_profiles_json == before_travel_profiles


def test_declared_relationship_policy_restricts_the_local_allowlist(client_factory) -> None:
    payload = schema_payload()
    related = payload["result"]["schemaContent"]["items"][8]
    related["props"]["availableTemplates"] = [
        {"name": "境内出差", "processCode": TRAVEL_PROCESS_CODES[0]}
    ]
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]
    related_component = next(
        item
        for item in inspected["reimbursement"]["schema"]["components"]
        if item["componentId"] == "related-id"
    )
    assert related_component["relatedTemplatePolicy"] == {
        "mode": "RESTRICTED",
        "processCodes": [TRAVEL_PROCESS_CODES[0]],
    }
    fingerprint = inspected["reimbursement"]["schema"]["schemaFingerprint"]

    rejected = confirm(client, headers, fingerprint)
    accepted = confirm(
        client,
        headers,
        fingerprint,
        allowed_travel_process_codes=[TRAVEL_PROCESS_CODES[0]],
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "OA_TEMPLATE_RELATIONSHIP_INVALID"
    assert accepted.status_code == 200
    assert accepted.json()["data"]["isSubmissionReady"] is True


def test_submission_readiness_is_false_when_travel_allowlist_is_empty(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.allowed_travel_process_codes_json = "[]"
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")

    assert current.status_code == 200
    assert current.json()["data"]["isSubmissionReady"] is False
    assert current.json()["data"]["requiresConfirmation"] is True
    assert current.json()["data"]["catalog"]["allowedTravelProcessCodes"] == []


def test_upgraded_profile_with_no_travel_profiles_is_not_submission_ready(
    client_factory,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.travel_profiles_json = "[]"
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")

    assert current.status_code == 200
    assert current.json()["data"]["isSubmissionReady"] is False
    assert current.json()["data"]["requiresConfirmation"] is True
    assert current.json()["data"]["catalog"]["travelProfiles"] == []


def test_submission_readiness_fails_closed_for_self_referential_travel_allowlist(
    client_factory,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.allowed_travel_process_codes_json = json.dumps([PROCESS_CODE])
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")

    assert current.status_code == 200
    assert current.json()["data"]["isSubmissionReady"] is False
    assert current.json()["data"]["catalog"]["allowedTravelProcessCodes"] == [PROCESS_CODE]


@pytest.mark.parametrize(
    "stored_allowlist",
    ["not-json", json.dumps([TRAVEL_PROCESS_CODES[0], TRAVEL_PROCESS_CODES[0]])],
)
def test_submission_readiness_fails_closed_for_corrupted_travel_allowlist(
    client_factory,
    stored_allowlist,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.allowed_travel_process_codes_json = stored_allowlist
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")

    assert current.status_code == 200
    assert current.json()["data"]["isSubmissionReady"] is False
    assert current.json()["data"]["configVersion"] == 1
    assert current.json()["data"]["catalog"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.pop("mappings"),
        lambda item: item.update({"unexpected": True}),
        lambda item: item.update({"profileKey": 123}),
        lambda item: item["travelTypeOption"].update({"unexpected": True}),
    ],
)
def test_submission_readiness_rejects_noncanonical_travel_profile_json(
    client_factory,
    mutate,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        stored = json.loads(profile.travel_profiles_json)
        mutate(stored[0])
        profile.travel_profiles_json = json.dumps(stored)
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")
    submission_options = client.get("/api/oa/reimbursements/options")

    assert current.status_code == 200
    assert current.json()["data"]["configVersion"] == 1
    assert current.json()["data"]["catalog"] is None
    assert submission_options.status_code == 409
    assert submission_options.json()["error"]["code"] == ("OA_TEMPLATE_CONFIRMATION_REQUIRED")


def test_submission_readiness_revalidates_the_full_persisted_mapping(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.mapping_json = json.dumps(MAPPINGS | {"company": "missing-component"})
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")

    assert current.status_code == 200
    assert current.json()["data"]["isSubmissionReady"] is False
    assert current.json()["data"]["catalog"]["reimbursement"]["mappings"] == (
        MAPPINGS | {"company": "missing-component"}
    )


def test_admin_can_reinspect_and_recover_a_malformed_persisted_mapping(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.mapping_json = "not-json"
        database.commit()

    current = client.get("/api/admin/oa/templates/catalog")
    reinspected = inspect(client, headers)

    assert current.status_code == 200
    assert current.json()["data"]["isSubmissionReady"] is False
    assert current.json()["data"]["configVersion"] == 1
    assert current.json()["data"]["catalog"] is None
    assert reinspected.status_code == 200
    assert reinspected.json()["data"]["isSubmissionReady"] is False
    assert reinspected.json()["data"]["reimbursement"]["mappings"] is None

    repaired = confirm(
        client,
        headers,
        reinspected.json()["data"]["reimbursement"]["schema"]["schemaFingerprint"],
        expected_config_version=current.json()["data"]["configVersion"],
    )

    assert repaired.status_code == 200
    assert repaired.json()["data"]["configVersion"] == 2


def test_fresh_submission_contract_allows_only_configured_travel_templates(
    client_factory,
) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200

    async def load_contract():
        return await load_fresh_submission_template(
            client.app.state.database_session_factory,
            client.app.state.dingtalk_workflow,
        )

    contract = asyncio.run(load_contract())

    assert contract.allowed_travel_process_codes == tuple(TRAVEL_PROCESS_CODES)
    assert contract.travel_profile("travel-0").process_code == TRAVEL_PROCESS_CODES[0]
    with pytest.raises(ApiError) as rejected:
        contract.travel_profile("travel-not-configured")
    assert rejected.value.code == "TRAVEL_APPROVAL_TEMPLATE_NOT_ALLOWED"


def test_fresh_submission_contract_auto_syncs_modified_timestamp(client_factory) -> None:
    original = schema_payload()
    changed = schema_payload(modified_at="2026-09-04T09:30:00+08:00")
    transport, _calls = transport_for_schema(
        lambda number, _request: httpx.Response(
            200,
            json=original if number <= 2 else changed,
        )
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200

    async def load_contract():
        return await load_fresh_submission_template(
            client.app.state.database_session_factory,
            client.app.state.dingtalk_workflow,
        )

    contract = asyncio.run(load_contract())

    assert contract.config_version == 1
    assert contract.reimbursement.schema.modified_at == "2026-09-04T09:30:00+08:00"
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        assert profile.compatibility_status == "COMPATIBLE"
        assert profile.schema_fingerprint != profile.confirmed_schema_fingerprint


def test_fresh_submission_contract_retries_after_concurrent_admin_reconfirmation(
    client_factory,
) -> None:
    version_one = schema_payload()
    description = version_one["result"]["schemaContent"]["items"][6]
    description["props"]["required"] = False
    version_one["result"]["schemaContent"]["items"].append(
        component(
            "TextareaField",
            "description-v2-id",
            "明细说明新版",
            required=False,
        )
    )
    version_two = copy.deepcopy(version_one)
    version_two["result"]["gmtModified"] = "2026-09-04T09:30:00+08:00"
    current_admin_payload = {"value": version_one}
    initial_transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=current_admin_payload["value"])
    )
    client, headers = admin_client(client_factory, initial_transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    version_two_mapping = MAPPINGS | {"description": "description-v2-id"}

    async def exercise_race():
        started = asyncio.Event()
        release = asyncio.Event()
        slow_transport = GatedSchemaTransport([version_one, version_two], started, release)
        slow_client = DingTalkOpenAPIClient(client.app.state.settings, transport=slow_transport)
        try:

            async def load_contract():
                return await load_fresh_submission_template(
                    client.app.state.database_session_factory,
                    DingTalkWorkflowClient(slow_client),
                )

            slow_load = asyncio.create_task(load_contract())
            await started.wait()
            current_admin_payload["value"] = version_two
            version_two_fingerprint = inspect(client, headers).json()["data"]["reimbursement"][
                "schema"
            ]["schemaFingerprint"]
            saved_response = confirm(
                client,
                headers,
                version_two_fingerprint,
                version_two_mapping,
                expected_config_version=1,
            )
            assert saved_response.status_code == 200
            release.set()
            contract = await slow_load
            return (
                saved_response.json()["data"],
                contract,
                slow_transport.schema_calls,
                version_two_fingerprint,
            )
        finally:
            await slow_client.close()

    saved, contract, schema_calls, version_two_fingerprint = asyncio.run(exercise_race())

    assert saved["configVersion"] == 2
    assert contract.reimbursement.mappings == version_two_mapping
    assert contract.reimbursement.schema.fingerprint == version_two_fingerprint
    assert schema_calls == 2
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        assert profile.config_version == 2
        assert profile.compatibility_status == "COMPATIBLE"
        assert profile.schema_fingerprint == version_two_fingerprint
        assert profile.confirmed_schema_fingerprint == version_two_fingerprint
        assert json.loads(profile.schema_json)["modifiedAt"] == "2026-09-04T09:30:00+08:00"
        assert json.loads(profile.mapping_json) == version_two_mapping


def test_confirmed_profile_is_persisted_and_returned_without_refetch(client_factory) -> None:
    transport, calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]

    saved = confirm(client, headers, fingerprint)
    current = client.get("/api/admin/oa/templates/catalog")

    assert saved.status_code == 200
    assert current.status_code == 200
    data = current.json()["data"]
    assert data["configured"] is True
    assert data["compatibilityStatus"] == "COMPATIBLE"
    assert data["isSubmissionReady"] is True
    catalog = data["catalog"]
    assert catalog["reimbursement"]["processCode"] == PROCESS_CODE
    assert catalog["configVersion"] == 1
    assert catalog["reimbursement"]["mappings"] == MAPPINGS
    assert catalog["allowedTravelProcessCodes"] == TRAVEL_PROCESS_CODES
    assert calls == {"token": 1, "schema": 2}
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        assert profile.process_code == PROCESS_CODE
        assert profile.config_version == 1
        assert profile.confirmed_schema_fingerprint == fingerprint
        assert profile.confirmed_by_user_id == "admin-1"
        assert json.loads(profile.allowed_travel_process_codes_json) == TRAVEL_PROCESS_CODES
        assert "client-secret" not in profile.schema_json


def test_stale_unconfigured_confirmation_cannot_overwrite_first_admin(client_factory) -> None:
    payload = schema_payload()
    payload["result"]["schemaContent"]["items"][6]["props"]["required"] = False
    payload["result"]["schemaContent"]["items"].append(
        component("TextareaField", "description-v2-id", "明细说明新版", required=False)
    )
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)
    first_inspection = inspect(client, headers).json()["data"]
    second_inspection = inspect(client, headers).json()["data"]
    assert first_inspection["configuredConfigVersion"] is None
    assert second_inspection["configuredConfigVersion"] is None

    first = confirm(
        client,
        headers,
        first_inspection["reimbursement"]["schema"]["schemaFingerprint"],
        MAPPINGS,
        expected_config_version=None,
    )
    stale = confirm(
        client,
        headers,
        second_inspection["reimbursement"]["schema"]["schemaFingerprint"],
        MAPPINGS | {"description": "description-v2-id"},
        expected_config_version=None,
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "OA_TEMPLATE_CONFIGURATION_CHANGED"
    current = client.get("/api/admin/oa/templates/catalog").json()["data"]["catalog"]
    assert current["configVersion"] == 1
    assert current["reimbursement"]["mappings"] == MAPPINGS


def test_stale_existing_confirmation_cannot_overwrite_newer_admin_mapping(
    client_factory,
) -> None:
    payload = schema_payload()
    payload["result"]["schemaContent"]["items"][6]["props"]["required"] = False
    payload["result"]["schemaContent"]["items"].append(
        component("TextareaField", "description-v2-id", "明细说明新版", required=False)
    )
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=payload)
    )
    client, headers = admin_client(client_factory, transport)
    initial = inspect(client, headers).json()["data"]
    fingerprint = initial["reimbursement"]["schema"]["schemaFingerprint"]
    assert confirm(client, headers, fingerprint).status_code == 200
    first_admin = inspect(client, headers).json()["data"]
    stale_admin = inspect(client, headers).json()["data"]
    assert first_admin["configuredConfigVersion"] == 1
    assert stale_admin["configuredConfigVersion"] == 1
    version_two_mapping = MAPPINGS | {"description": "description-v2-id"}

    newer = confirm(
        client,
        headers,
        fingerprint,
        version_two_mapping,
        expected_config_version=first_admin["configuredConfigVersion"],
    )
    stale = confirm(
        client,
        headers,
        fingerprint,
        MAPPINGS,
        expected_config_version=stale_admin["configuredConfigVersion"],
    )

    assert newer.status_code == 200
    assert newer.json()["data"]["configVersion"] == 2
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "OA_TEMPLATE_CONFIGURATION_CHANGED"
    current = client.get("/api/admin/oa/templates/catalog").json()["data"]["catalog"]
    assert current["configVersion"] == 2
    assert current["reimbursement"]["mappings"] == version_two_mapping


def test_option_catalog_drift_auto_syncs_without_new_config_version(client_factory) -> None:
    original = schema_payload()
    changed = schema_payload(
        modified_at="2026-09-04T09:30:00+08:00",
        company_options=[json.dumps({"value": "苏州", "key": "option_2"})],
    )

    def response_factory(number: int, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=original if number <= 2 else changed)

    transport, _calls = transport_for_schema(response_factory)
    client, headers = admin_client(client_factory, transport)
    original_fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, original_fingerprint).status_code == 200
    # A deployment can inherit the old implementation's blanket drift marker.
    # The first live compatibility check must recover it without another admin save.
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        profile.compatibility_status = "DRIFTED"
        database.commit()

    options = client.get("/api/oa/reimbursements/options")
    drift = inspect(client, headers)
    current = client.get("/api/admin/oa/templates/catalog")

    assert options.status_code == 200
    assert options.json()["data"]["companyOptions"] == [
        {"value": "苏州", "label": "苏州", "key": "option_2"}
    ]
    assert drift.status_code == 200
    drift_data = drift.json()["data"]
    assert drift_data["compatibilityStatus"] == "COMPATIBLE"
    assert drift_data["requiresConfirmation"] is False
    assert drift_data["isSubmissionReady"] is True
    changed_fingerprint = drift_data["reimbursement"]["schema"]["schemaFingerprint"]
    assert changed_fingerprint != original_fingerprint
    assert current.json()["data"]["compatibilityStatus"] == "COMPATIBLE"
    assert current.json()["data"]["isSubmissionReady"] is True
    assert current.json()["data"]["catalog"]["confirmedSchemaFingerprint"] == (original_fingerprint)
    assert current.json()["data"]["catalog"]["configVersion"] == 1

    assert changed_fingerprint != original_fingerprint
    assert current.json()["data"]["catalog"]["schemaFingerprint"] == changed_fingerprint


def test_stale_compatible_refresh_cannot_clear_a_newer_drift_marker(client_factory) -> None:
    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    with client.app.state.database_session_factory() as database:
        profile = database.get(OaTemplateProfile, "reimbursement")
        assert profile is not None
        expected_state = oa_template_profiles._catalog_cas_state(profile)
        stale_refresh = oa_template_profiles._validated_persisted_catalog(profile)
        profile.compatibility_status = "DRIFTED"
        database.commit()

    with client.app.state.database_session_factory() as database:
        assert (
            oa_template_profiles._replace_compatible_catalog(
                database,
                expected_state=expected_state,
                refreshed=stale_refresh,
            )
            is False
        )
        current = database.get(OaTemplateProfile, "reimbursement")
        assert current is not None
        assert current.compatibility_status == "DRIFTED"


def test_mapped_field_structure_drift_still_requires_admin_confirmation(
    client_factory,
) -> None:
    original = schema_payload()
    changed = schema_payload(modified_at="2026-09-04T09:30:00+08:00")
    changed["result"]["schemaContent"]["items"][0]["props"]["hidden"] = True

    def response_factory(number: int, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=original if number <= 2 else changed)

    transport, _calls = transport_for_schema(response_factory)
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200

    blocked = client.get("/api/oa/reimbursements/options")
    current = client.get("/api/admin/oa/templates/catalog").json()["data"]

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "OA_TEMPLATE_CONFIRMATION_REQUIRED"
    assert current["compatibilityStatus"] == "DRIFTED"
    assert current["isSubmissionReady"] is False
    assert current["configVersion"] == 1


def test_mapped_field_label_drift_requires_admin_confirmation(client_factory) -> None:
    original = schema_payload()
    changed = schema_payload(modified_at="2026-09-04T09:30:00+08:00")
    changed["result"]["schemaContent"]["items"][0]["props"]["label"] = "当前所属公司"

    def response_factory(number: int, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=original if number <= 2 else changed)

    transport, _calls = transport_for_schema(response_factory)
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200

    blocked = client.get("/api/oa/reimbursements/options")
    current = client.get("/api/admin/oa/templates/catalog").json()["data"]

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "OA_TEMPLATE_CONFIRMATION_REQUIRED"
    assert current["compatibilityStatus"] == "DRIFTED"
    assert current["isSubmissionReady"] is False


def test_travel_modified_timestamp_auto_syncs_without_blocking_catalog(client_factory) -> None:
    travel_calls: dict[str, int] = {}

    def travel_response(process_code: str, _request: httpx.Request) -> httpx.Response:
        travel_calls[process_code] = travel_calls.get(process_code, 0) + 1
        modified_at = (
            "2026-09-04T12:00:00+08:00"
            if process_code == TRAVEL_PROCESS_CODES[0] and travel_calls[process_code] >= 3
            else "2026-09-03T11:00:00+08:00"
        )
        return httpx.Response(
            200,
            json=travel_schema_payload(process_code, modified_at=modified_at),
        )

    transport, _calls = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload()),
        travel_response_factory=travel_response,
    )
    client, headers = admin_client(client_factory, transport)
    inspected = inspect(client, headers).json()["data"]
    assert (
        confirm(
            client,
            headers,
            inspected["reimbursement"]["schema"]["schemaFingerprint"],
        ).status_code
        == 200
    )

    async def load_contract():
        return await load_fresh_submission_template(
            client.app.state.database_session_factory,
            client.app.state.dingtalk_workflow,
        )

    contract = asyncio.run(load_contract())

    drift = inspect(client, headers)
    current = client.get("/api/admin/oa/templates/catalog")
    blocked = client.get("/api/oa/reimbursements/options")

    assert drift.status_code == 200
    assert contract.config_version == 1
    assert drift.json()["data"]["compatibilityStatus"] == "COMPATIBLE"
    assert drift.json()["data"]["isSubmissionReady"] is True
    assert current.json()["data"]["compatibilityStatus"] == "COMPATIBLE"
    assert current.json()["data"]["isSubmissionReady"] is True
    assert current.json()["data"]["catalog"]["configVersion"] == 1
    assert blocked.status_code == 200


def test_fingerprint_is_stable_across_json_key_and_component_order(client_factory) -> None:
    first = schema_payload()
    reordered = copy.deepcopy(first)
    reordered_items = reordered["result"]["schemaContent"]["items"]
    reordered_items.reverse()
    transport, _calls = transport_for_schema(
        lambda number, _request: httpx.Response(200, json=first if number == 1 else reordered)
    )
    client, headers = admin_client(client_factory, transport)

    first_hash = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    second_hash = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]

    assert first_hash == second_hash
