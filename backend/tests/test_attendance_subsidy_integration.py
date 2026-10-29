"""Calendar guards through real draft/Excel/submit APIs and a synthetic OA boundary."""

import asyncio
from dataclasses import replace
from datetime import date

import pytest
import test_oa_reimbursement_integration as integration
from conftest import mock_login
from sqlalchemy import select
from test_attendance_subsidy import install_calendar

from app.integrations.dingtalk.workflow import FormOption
from app.models.reimbursement import ReimbursementDraft, ReimbursementSubmission
from app.schemas.expenses import TripPurpose
from app.services.attendance_calendar import delete_calendar, list_calendars


@pytest.fixture
def city_flow(client_factory, monkeypatch):
    async def manual_worker(_self, stop):
        await stop.wait()

    monkeypatch.setattr(integration.DurableOAReimbursementWorker, "run", manual_worker)
    client = client_factory(auth_mock_enabled=True, dingtalk_oa_worker_enabled=True)
    headers = {
        "X-CSRF-Token": mock_login(client)["csrfToken"],
        "Idempotency-Key": "86868686-8686-4868-8868-868686868686",
    }
    install_calendar(client)
    reimbursement, travel, _ = integration._schemas()
    city = FormOption(
        "境内市内项目出差（长/短期）", "境内市内项目出差（长/短期）", "synthetic-city"
    )
    reimbursement = integration._schema(
        reimbursement.process_code,
        tuple(
            replace(component, options=(city,))
            if component.component_id == "travel-type-id"
            else component
            for component in reimbursement.components
        ),
    )
    original = integration._draft_input

    # Only change the synthetic input fixture; all real validation remains active.
    def city_input(**kwargs):
        value = original(**kwargs)
        return value.model_copy(
            update={
                "trip": value.trip.model_copy(
                    update={
                        "trip_type": TripPurpose.SAME_CITY_PROJECT,
                        "attendance_dates": [date(2026, 9, 1), date(2026, 9, 3)],
                    }
                )
            }
        )

    monkeypatch.setattr(integration, "_draft_input", city_input)
    workflow = integration.LocalWorkflowBoundary(reimbursement, travel)
    draft_id, _ = integration._persist_ready_draft(
        client,
        workflow,
        city,
        subsidy_only=True,
        with_materials=False,
    )
    return client, headers, draft_id, workflow


def remove_calendar(client):
    with client.app.state.database_session_factory() as database:
        calendar = next(item for item in list_calendars(database) if item["year"] == 2026)
        delete_calendar(database, 2026, calendar["revision"])


def test_missing_calendar_blocks_excel_review_and_submit_without_locking_or_losing_input(city_flow):
    client, headers, draft_id, workflow = city_flow
    remove_calendar(client)
    for path in (
        f"/api/reimbursements/drafts/{draft_id}/excel-preview",
        f"/api/reimbursements/drafts/{draft_id}/review",
        f"/api/oa/reimbursements/{draft_id}/submit",
    ):
        response = client.post(path, json={"expectedRevision": 4}, headers=headers)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "ATTENDANCE_CALENDAR_REQUIRED"
    restored = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert restored["input"]["trip"]["attendanceDates"] == ["2026-09-01", "2026-09-03"]
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        assert draft.locked_at is None and draft.revision == 4
        assert database.scalar(select(ReimbursementSubmission.id)) is None
    assert workflow.create_calls == 0
    install_calendar(client)
    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/excel-preview",
        json={"expectedRevision": 4},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_locked_city_snapshot_preview_idempotency_and_worker_ignore_later_calendar_removal(
    city_flow,
):
    client, headers, draft_id, workflow = city_flow
    path = f"/api/oa/reimbursements/{draft_id}/submit"
    submitted = client.post(path, json={"expectedRevision": 4}, headers=headers)
    assert submitted.status_code == 202, submitted.text
    submission_id = submitted.json()["data"]["submissionId"]
    remove_calendar(client)
    repeated = client.post(path, json={"expectedRevision": 4}, headers=headers)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["data"]["submissionId"] == submission_id
    locked = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert locked["totals"]["subsidyTotal"] == "100.00"
    preview = client.post(
        f"/api/reimbursements/drafts/{draft_id}/excel-preview",
        json={"expectedRevision": locked["revision"]},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    worker = integration._worker(client, workflow, integration.LocalStorageBoundary())
    assert asyncio.run(worker.run_once())
    status = client.get(f"/api/oa/reimbursements/submissions/{submission_id}").json()["data"]
    assert status["status"] == "SUBMITTED", status
    assert workflow.create_calls == 1
