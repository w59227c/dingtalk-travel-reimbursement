from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select
from test_dingtalk_workflow_instances import token_response, workflow_client
from test_oa_templates import admin_client, confirm, inspect, schema_payload, transport_for_schema

from app.core.errors import ApiError
from app.models.oa_template_profile import OaTemplateProfile
from app.services import oa_template_profiles
from app.services.oa_template_profiles import load_readonly_submission_catalog


@pytest.mark.asyncio
async def test_diagnostic_discovery_is_explicit_bounded_and_has_no_employee_filter(
    settings_factory,
):
    calls = []

    def handler(request):
        if request.url.path.endswith("/token"):
            return token_response()
        calls.append(request)
        body = json.loads(request.content)
        assert "userIds" not in body
        assert body["statuses"] == ["COMPLETED", "RUNNING", "TERMINATED"]
        assert body["endTime"] - body["startTime"] == 59_999
        return httpx.Response(200, json={"success": True, "result": {"list": ["synthetic-id"]}})

    client, workflow = workflow_client(settings_factory, handler)
    try:
        page = await workflow.list_process_instance_ids_for_diagnostics(
            process_code="PROC-SYNTHETIC",
            start_time=1000,
            end_time=60_999,
            next_token=0,
            max_results=20,
        )
        assert page.instance_ids == ("synthetic-id",)
        with pytest.raises(ValueError):
            await workflow.list_process_instance_ids_for_diagnostics(
                process_code="PROC-SYNTHETIC",
                start_time=1000,
                end_time=601_000,
                next_token=0,
                max_results=20,
            )
        with pytest.raises(ValueError):
            await workflow.list_process_instance_ids(
                process_code="PROC-SYNTHETIC",
                start_time=1000,
                end_time=60_999,
                next_token=0,
                max_results=20,
                user_ids=(),
                statuses=("COMPLETED",),
            )
        assert len(calls) == 1
    finally:
        await client.close()


def _catalog_snapshot(client):
    with client.app.state.database_session_factory() as database:
        profile = database.execute(select(OaTemplateProfile)).scalar_one()
        return tuple(getattr(profile, column.key) for column in OaTemplateProfile.__table__.columns)


def test_readonly_freshness_uses_new_options_without_writing_snapshot_or_drift(client_factory):
    payload = schema_payload()
    transport, _ = transport_for_schema(lambda _number, _request: httpx.Response(200, json=payload))
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    before = _catalog_snapshot(client)

    payload["result"]["gmtModified"] = "2026-09-04T10:00:00+08:00"
    payload["result"]["schemaContent"]["items"][0]["props"]["options"].append(
        json.dumps({"value": "合成新增公司", "key": "new-option"})
    )
    refreshed = client.portal.call(
        load_readonly_submission_catalog,
        client.app.state.database_session_factory,
        client.app.state.dingtalk_workflow,
    )
    assert refreshed.reimbursement.schema.fingerprint != fingerprint
    assert any(
        option.value == "合成新增公司"
        for component in refreshed.reimbursement.schema.components
        for option in component.options
    )
    assert _catalog_snapshot(client) == before

    payload["result"]["schemaContent"]["items"][0]["props"]["label"] = "合成不兼容字段"
    with pytest.raises(ApiError) as error:
        client.portal.call(
            load_readonly_submission_catalog,
            client.app.state.database_session_factory,
            client.app.state.dingtalk_workflow,
        )
    assert error.value.code == "OA_TEMPLATE_CONFIRMATION_REQUIRED"
    assert _catalog_snapshot(client) == before


def test_readonly_observation_rejects_configuration_changed_during_remote_read(
    client_factory,
    monkeypatch,
):
    transport, _ = transport_for_schema(
        lambda _number, _request: httpx.Response(200, json=schema_payload())
    )
    client, headers = admin_client(client_factory, transport)
    fingerprint = inspect(client, headers).json()["data"]["reimbursement"]["schema"][
        "schemaFingerprint"
    ]
    assert confirm(client, headers, fingerprint).status_code == 200
    observe = oa_template_profiles._observe_submission_catalog

    async def concurrent_change(contract, workflow):
        result = await observe(contract, workflow)
        with client.app.state.database_session_factory() as database:
            profile = database.execute(select(OaTemplateProfile)).scalar_one()
            profile.config_version += 1
            database.commit()
        return result

    monkeypatch.setattr(oa_template_profiles, "_observe_submission_catalog", concurrent_change)
    with pytest.raises(ApiError) as error:
        client.portal.call(
            load_readonly_submission_catalog,
            client.app.state.database_session_factory,
            client.app.state.dingtalk_workflow,
        )
    assert error.value.code == "OA_TEMPLATE_CONFIGURATION_CHANGED"
