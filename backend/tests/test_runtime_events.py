from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from test_approval_diagnostics import Workflow
from test_travel_approvals import _catalog, _instance, _profile

from app.core.runtime_events import (
    RuntimeEvent,
    RuntimeEventHandler,
    bind_runtime_recorder,
    reset_runtime_recorder,
)
from app.models.runtime_event import RuntimeEventRecord
from app.services.runtime_events import RuntimeEventStore

PATH = "/api/admin/runtime-events"


def admin(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="synthetic-admin",
        admin_user_ids="synthetic-admin",
    )
    client.post("/api/auth/mock")
    assert client.app.state.runtime_events.flush()
    return client


def event(*, request_id="synthetic-request", when=None, **details):
    return RuntimeEvent(
        when or datetime.now(UTC).replace(tzinfo=None),
        "approvals",
        "approval_query",
        "empty",
        request_id,
        details,
    )


def test_admin_auth_filters_pagination_no_store_and_no_recursive_collection(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="synthetic-admin",
        admin_user_ids="synthetic-admin",
    )
    assert client.get(PATH).status_code == 401
    client.post("/api/auth/mock")
    store = client.app.state.runtime_events
    store.record(event(request_id="synthetic-one", returned=0))
    store.record(event(request_id="synthetic-two", returned=0))
    assert store.flush()
    response = client.get(PATH, params={"module": "approvals", "result": "empty", "limit": 1})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()["data"]
    assert data["items"][0]["requestId"] == "synthetic-two"
    next_page = client.get(
        PATH,
        params={"module": "approvals", "result": "empty", "before": data["nextCursor"], "limit": 1},
    ).json()["data"]
    assert next_page["items"][0]["requestId"] == "synthetic-one"
    assert next_page["nextCursor"] is None
    only = client.get(PATH, params={"requestId": "synthetic-one"}).json()["data"]
    assert len(only["items"]) == 1
    assert only["coverage"]["records"] == data["coverage"]["records"]
    client.app.state.settings.admin_user_ids = ""
    assert client.get(PATH).status_code == 403


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-08-01"},
        {"from": "2026-08-01", "to": "2026-08-08"},
        {"from": "2026-08-02", "to": "2026-08-01"},
        {"requestId": "not a valid id"},
        {"module": "invalid"},
        {"result": "invalid"},
        {"before": 0},
        {"limit": 101},
        {"from": "0001-01-01", "to": "0001-01-01"},
    ],
)
def test_invalid_search_inputs_are_rejected(client_factory, params):
    client = admin(client_factory)
    assert client.get(PATH, params=params).status_code == 422


def test_writer_and_reader_keep_only_safe_fields_and_survive_restart(client_factory):
    client = admin(client_factory)
    store = client.app.state.runtime_events
    store.record(
        event(
            returned=0,
            userId="synthetic-private-user",
            title="synthetic-private-project",
            approvalId="synthetic-private-approval",
            token="synthetic-private-token",
            keyword="synthetic-private-keyword",
        )
    )
    assert store.flush()
    reopened = RuntimeEventStore(client.app.state.database_session_factory)
    data = reopened.search(
        start=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7),
        end=datetime.now(UTC).replace(tzinfo=None),
        request_id="synthetic-request",
    )
    assert data["items"][0]["details"] == {"returned": 0}
    with client.app.state.database_session_factory() as database:
        raw = database.scalar(
            select(RuntimeEventRecord.details_json).where(
                RuntimeEventRecord.request_id == "synthetic-request"
            )
        )
        assert "synthetic-private" not in raw
        # Even unexpected fields in old/corrupt data are not echoed to the browser.
        database.execute(
            text(
                "UPDATE runtime_events SET details_json = :value "
                "WHERE request_id = 'synthetic-request'"
            ),
            {"value": '{"returned":0,"token":"synthetic-private-token"}'},
        )
        database.commit()
    assert "synthetic-private" not in client.get(PATH).text


@pytest.mark.parametrize(("max_records", "max_bytes", "expected"), [(2, 10000, 2), (100, 1100, 2)])
def test_retention_and_both_capacity_bounds(client_factory, max_records, max_bytes, expected):
    client = admin(client_factory)
    store = RuntimeEventStore(
        client.app.state.database_session_factory, max_records=max_records, max_bytes=max_bytes
    )
    store._write(
        [event(when=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8))]
        + [event(request_id=f"synthetic-{index}") for index in range(5)]
    )
    with store.factory() as database:
        assert database.scalar(select(func.count()).select_from(RuntimeEventRecord)) == expected
        assert database.scalar(select(func.sum(RuntimeEventRecord.byte_size))) <= max_bytes
        assert database.scalar(select(func.min(RuntimeEventRecord.occurred_at))) > datetime.now(
            UTC
        ).replace(tzinfo=None) - timedelta(days=7)


def test_collection_failure_is_visible_and_recovery_persists_a_gap(client_factory, monkeypatch):
    client = admin(client_factory)
    store = client.app.state.runtime_events
    write = store._write

    def fail(_events):
        raise RuntimeError("synthetic-private-error")

    monkeypatch.setattr(store, "_write", fail)
    store.record(event())
    assert store.flush()
    data = client.get(PATH).json()["data"]
    assert data["collector"]["writeError"] is True
    assert client.get("/api/me").status_code == 200
    assert data["collector"]["droppedSinceStart"] >= 1
    assert "synthetic-private-error" not in json.dumps(data)
    monkeypatch.setattr(store, "_write", write)
    store.record(event())
    assert store.flush()
    data = client.get(PATH).json()["data"]
    assert data["collector"]["writeError"] is False
    assert any(row["event"] == "collection_gap" for row in data["items"])


def test_full_queue_never_blocks_and_reports_drops(client_factory):
    client = admin(client_factory)
    store = RuntimeEventStore(client.app.state.database_session_factory, queue_size=1)
    store.record(event())
    store.record(event())
    assert store.status()["droppedSinceStart"] == 1
    store.start()
    try:
        assert store.flush()
    finally:
        store.close()


def test_unreadable_store_returns_failure_instead_of_empty_success(client_factory, monkeypatch):
    client = admin(client_factory)

    def fail():
        raise RuntimeError("synthetic-private-database")

    monkeypatch.setattr(client.app.state.runtime_events, "factory", fail)
    response = client.get(PATH)
    assert response.status_code == 503
    assert "synthetic-private" not in response.text


def test_log_handler_uses_fixed_events_and_drops_message_args_and_unknown_extras():
    captured = []
    recorder = type("Recorder", (), {"record": lambda _self, item: captured.append(item)})()
    token = bind_runtime_recorder(recorder)
    try:
        record = logging.LogRecord(
            "app.services.ocr_service",
            logging.WARNING,
            "",
            0,
            "Local OCR failed",
            ("synthetic-private-text",),
            None,
        )
        record.error_code = "OCR_TIMEOUT"
        record.token = "synthetic-private-token"
        RuntimeEventHandler().emit(record)
    finally:
        reset_runtime_recorder(token)
    assert len(captured) == 1
    assert captured[0].module == "materials"
    assert captured[0].details == {"errorCode": "OCR_TIMEOUT"}
    assert "synthetic-private" not in repr(captured)


@pytest.mark.parametrize(
    ("instance", "expected_result", "rejection"),
    [
        (None, "empty", None),
        (
            _instance("synthetic-instance", user_id="synthetic-other"),
            "empty",
            "ORIGINATOR_MISMATCH",
        ),
        (_instance("synthetic-instance"), "success", None),
    ],
)
def test_actual_employee_query_records_empty_filtered_and_success_with_same_request_id(
    client_factory, monkeypatch, instance, expected_result, rejection
):
    from app.api import oa_reimbursements

    client = admin(client_factory)
    catalog = _catalog(_profile("synthetic", "PROC-SYNTHETIC"))

    async def ready(*_args):
        return catalog

    monkeypatch.setattr(oa_reimbursements, "load_fresh_submission_catalog", ready)
    workflow = Workflow(instance, listed=instance is not None)
    client.app.state.dingtalk_workflow = workflow
    # Use the authenticated employee identity, not a client-supplied override.
    if instance and not rejection:
        workflow.instance = replace(instance, originator_user_id="synthetic-admin")
    response = client.get(
        "/api/oa/travel-approvals", params={"q": "", "from": "2026-08-01", "to": "2026-08-31"}
    )
    assert response.status_code == 200
    request_id = response.json()["data"]["requestId"]
    assert request_id == response.headers["X-Request-ID"]
    data = client.get(PATH, params={"requestId": request_id}).json()["data"]
    query = next(row for row in data["items"] if row["event"] == "approval_query")
    assert query["result"] == expected_result
    assert query["details"]["listed"] == int(instance is not None)
    assert query["details"]["detailsRead"] == int(instance is not None)
    assert query["details"]["returned"] == int(expected_result == "success")
    if rejection:
        assert query["details"]["exclusions"] == [{"code": rejection, "count": 1}]
    assert "synthetic-admin" not in json.dumps(data)
    assert "synthetic-instance" not in json.dumps(data)


def test_failed_query_keeps_partial_counts_without_claiming_zero_results(
    client_factory, monkeypatch
):
    from app.api import oa_reimbursements
    from app.core.errors import ApiError

    client = admin(client_factory)

    async def ready(*_args):
        return _catalog(_profile("synthetic", "PROC-SYNTHETIC"))

    monkeypatch.setattr(oa_reimbursements, "load_fresh_submission_catalog", ready)
    client.app.state.dingtalk_workflow = Workflow(
        failure=ApiError("DINGTALK_RATE_LIMITED", "合成限流", 429)
    )
    response = client.get(
        "/api/oa/travel-approvals", params={"from": "2026-08-01", "to": "2026-08-31"}
    )
    assert response.status_code == 429
    data = client.get(PATH, params={"requestId": response.json()["requestId"]}).json()["data"]
    query = next(row for row in data["items"] if row["event"] == "approval_query")
    assert query["result"] == "incomplete"
    assert query["details"]["returned"] is None
    assert query["details"]["stage"] == "details"
    assert any(row["details"].get("errorCode") == "DINGTALK_RATE_LIMITED" for row in data["items"])


@pytest.mark.asyncio
async def test_observation_counts_date_keyword_and_option_outcomes_without_changing_policy():
    from datetime import date

    from test_travel_approvals import FakeWorkflow

    from app.integrations.dingtalk.workflow import WorkflowInstanceIdPage
    from app.services.travel_approvals import (
        TravelApprovalQueryObservation,
        TravelApprovalQueryWindow,
        list_current_user_travel_approvals,
    )

    instances = [
        replace(_instance("synthetic-same-day", end_date="2026-08-10"), title="match"),
        replace(_instance("synthetic-invalid-date", start_date=None), title="match"),
        replace(_instance("synthetic-refused", result="refuse"), title="match"),
        replace(_instance("synthetic-keyword"), title="other title"),
        replace(_instance("synthetic-unselectable"), title="match"),
    ]
    last = instances[-1]
    instances[-1] = replace(
        last,
        form_values=tuple(
            replace(value, value="合成失效预算") if value.component_id == "source-budget" else value
            for value in last.form_values
        ),
    )
    workflow = FakeWorkflow(
        {
            ("PROC-SYNTHETIC", 0): WorkflowInstanceIdPage(
                tuple(item.instance_id for item in instances), None
            )
        },
        {item.instance_id: item for item in instances},
    )
    observation = TravelApprovalQueryObservation()
    candidates = await list_current_user_travel_approvals(
        workflow,
        _catalog(_profile("synthetic", "PROC-SYNTHETIC")),
        current_user_id="employee-1",
        query_window=TravelApprovalQueryWindow.from_dates(date(2026, 8, 1), date(2026, 8, 31)),
        query="match",
        observation=observation,
    )
    assert len(candidates) == 2
    assert observation.stage == "complete"
    assert observation.listed == observation.details_read == observation.evaluated == 5
    assert observation.returned == 2 and observation.unselectable == 1
    assert observation.exclusions == {
        "DATE_FIELDS_INVALID": 1,
        "NOT_AGREED": 1,
        "KEYWORD_MISMATCH": 1,
    }
