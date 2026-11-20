from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from conftest import mock_login
from sqlalchemy import select
from test_general_reimbursement import GeneralWorkflow, configure_profiles
from test_reimbursement_files import _upload
from test_reimbursement_recovery import failed_form, recover
from test_reimbursement_workspaces import setup

from app.models.reimbursement import ReimbursementDraft, ReimbursementSubmission
from app.models.session import UserSession
from app.services.reimbursement_submissions import claim_due_submission, fail_submission_final

pytest_plugins = ["test_oa_reimbursement_integration"]


def failed_general(client, *, before_submit=None, terminal=True):
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    asyncio.run(configure_profiles(client.app, GeneralWorkflow()))
    created = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={"expectedRevision": 0, "kind": "general", "input": {"items": []}},
    )
    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    identifier = draft["id"]
    root = "/api/reimbursements/drafts/" + identifier
    uploaded = _upload(
        client,
        headers["X-CSRF-Token"],
        identifier,
        revision=draft["revision"],
        role="ATTACHMENT_ONLY",
    )
    assert uploaded.status_code == 201, uploaded.text
    uploaded = uploaded.json()["data"]
    saved = client.put(
        root,
        headers=headers,
        json={
            "expectedRevision": uploaded["revision"],
            "input": {
                "companyValue": "synthetic-company",
                "budgetCodeValue": "synthetic-budget",
                "items": [
                    {
                        "category": "office",
                        "subjectValue": "synthetic-subject-0",
                        "date": "2026-09-01",
                        "displayDate": "9月1日",
                        "description": "合成办公费",
                        "amount": "30.00",
                    }
                ],
            },
        },
    )
    assert saved.status_code == 200, saved.text
    ready = client.post(
        root + "/review",
        headers=headers,
        json={"expectedRevision": saved.json()["data"]["revision"]},
    )
    assert ready.status_code == 200, ready.text
    if before_submit:
        with client.app.state.database_session_factory() as database:
            change_department(database, before_submit)
    submitted = client.post(
        "/api/oa/reimbursements/" + identifier + "/submit",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"expectedRevision": ready.json()["data"]["revision"]},
    )
    assert submitted.status_code == 202, submitted.text
    if not terminal:
        with client.app.state.database_session_factory() as database:
            revision = database.get(ReimbursementDraft, identifier).revision
        return identifier, revision, [uploaded["file"]["id"]], headers
    with client.app.state.database_session_factory() as database:
        claim = claim_due_submission(database, worker_id="general-local-review", lease_seconds=180)
        assert claim is not None
        fail_submission_final(
            database,
            lease=claim.lease,
            worker_id="general-local-review",
            error_code="LOCAL_VALIDATION",
            error_message="合成失败，未创建OA",
        )
        database.commit()
        revision = database.get(ReimbursementDraft, identifier).revision
    return identifier, revision, [uploaded["file"]["id"]], headers


def change_department(database, change):
    session = database.scalar(select(UserSession))
    identifier = session.current_department_id if change == "rename" else "200"
    session.departments_json = json.dumps(
        [] if change == "none" else [{"id": identifier, "name": "当前合成部门"}]
    )
    session.current_department_id = None if change in {"none", "unselected"} else identifier
    session.current_department_name = None if change in {"none", "unselected"} else "当前合成部门"
    database.commit()


@pytest.mark.parametrize("change", ["rename", "move", "unselected", "none"])
def test_new_task_freezes_saved_department_and_remains_queryable(
    enabled_manual_worker_client, change
):
    client = enabled_manual_worker_client
    identifier, revision, _, headers = failed_general(client, before_submit=change, terminal=False)
    response = client.get(f"/api/oa/reimbursements/drafts/{identifier}/submission")
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    assert task["status"] == "QUEUED"
    with client.app.state.database_session_factory() as database:
        source = database.get(ReimbursementDraft, identifier)
        submission = database.get(ReimbursementSubmission, task["submissionId"])
        identity = json.loads(submission.form_snapshot_json)["identity"]
        assert identity["departmentId"] == source.department_id == "100"
        assert identity["departmentName"] == source.department_name
    denied = recover(client, identifier, revision, headers)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "REIMBURSEMENT_RECOVERY_NOT_ALLOWED"


@pytest.mark.parametrize("kind", ["travel", "general"])
@pytest.mark.parametrize("change", ["rename", "move", "unselected", "none"])
def test_task_and_recovery_keep_department_as_an_attribute(
    enabled_manual_worker_client, kind, change
):
    client = enabled_manual_worker_client
    identifier, revision, source_files, headers = (
        failed_general if kind == "general" else failed_form
    )(client)
    root = f"/api/reimbursements/drafts/{identifier}"
    original = client.get(root).json()["data"]
    task = client.get(f"/api/oa/reimbursements/drafts/{identifier}/submission").json()["data"]
    with client.app.state.database_session_factory() as database:
        frozen = database.get(ReimbursementSubmission, task["submissionId"])
        snapshot = frozen.form_snapshot_json, frozen.snapshot_sha256
        change_department(database, change)
    assert client.get(root).status_code == 200
    assert client.get(root + "/files").status_code == 200
    workspace = client.get("/api/reimbursements/workspace", params={"kind": kind})
    assert workspace.status_code == 200, workspace.text
    assert [d["id"] for d in workspace.json()["data"]["items"]] == [identifier]
    for path in [
        f"/api/oa/reimbursements/drafts/{identifier}/submission",
        f"/api/oa/reimbursements/submissions/{task['submissionId']}",
    ]:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["submissionId"] == task["submissionId"]
        assert response.json()["data"]["status"] == "FAILED_FINAL"
    repeated = client.post(
        f"/api/oa/reimbursements/{identifier}/submit",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"expectedRevision": revision},
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["data"]["submissionId"] == task["submissionId"]
    recovered = recover(client, identifier, revision, headers)
    assert recovered.status_code == 200, recovered.text
    result = recovered.json()["data"]
    assert result["department"] == original["department"]
    assert result.get("kind", "travel") == kind
    assert result["recoverySourceDraftId"] == identifier
    assert not result["recoveryPending"]
    copied = client.get(f"/api/reimbursements/drafts/{result['id']}/files").json()["data"]
    assert len(copied["items"]) == len(source_files)
    assert recover(client, identifier, revision, headers).json()["data"]["id"] == result["id"]
    with client.app.state.database_session_factory() as database:
        frozen = database.get(ReimbursementSubmission, task["submissionId"])
        assert (frozen.form_snapshot_json, frozen.snapshot_sha256) == snapshot
        assert len(database.scalars(select(ReimbursementSubmission)).all()) == 1


@pytest.mark.parametrize("general", [False, True])
@pytest.mark.parametrize("change", ["rename", "move", "unselected", "none"])
def test_department_does_not_block_content_or_material_operations(client_factory, general, change):
    client, headers, draft = setup(client_factory, general=general)
    root = f"/api/reimbursements/drafts/{draft['id']}"
    with client.app.state.database_session_factory() as database:
        change_department(database, change)
    saved = client.put(
        root, headers=headers, json={"expectedRevision": draft["revision"], "input": draft["input"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["department"] == draft["department"]
    stale = client.put(
        root, headers=headers, json={"expectedRevision": draft["revision"], "input": draft["input"]}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_REVISION_CONFLICT"
    uploaded = _upload(
        client,
        headers["X-CSRF-Token"],
        draft["id"],
        revision=saved.json()["data"]["revision"],
        role="ATTACHMENT_ONLY",
    )
    assert uploaded.status_code == 201, uploaded.text
    data = uploaded.json()["data"]
    path = root + f"/files/{data['file']['id']}"
    assert client.get(path + "/content").status_code == 200
    deleted = client.delete(path, headers=headers, params={"expectedRevision": data["revision"]})
    assert deleted.status_code == 200, deleted.text
    assert client.get(root).json()["data"]["department"] == draft["department"]


@pytest.mark.parametrize("general", [False, True])
@pytest.mark.parametrize("change", ["none", "unselected"])
@pytest.mark.parametrize("with_subsidy", [False, True])
def test_calculation_is_independent_of_session_department(
    client_factory, general, change, with_subsidy
):
    client, headers, draft = setup(client_factory, general=general)
    body = {
        "items": [
            {
                "id": "synthetic-total-item",
                "source": "manual",
                "category": "office",
                "date": "2026-09-01",
                "displayDate": "9月1日",
                "description": "合成办公费",
                "amount": "30.00",
                "receiptCount": 1,
            }
        ]
    }
    if with_subsidy:
        body["trip"] = {
            "tripType": "short_term_project",
            "startDate": "2026-09-01",
            "startTime": "09:00",
            "endDate": "2026-09-03",
            "endTime": "18:00",
        }
    before = client.post("/api/calculate/totals", headers=headers, json=body)
    assert before.status_code == 200, before.text
    with client.app.state.database_session_factory() as database:
        change_department(database, change)
    response = client.post("/api/calculate/totals", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == before.json()["data"]
    assert (
        client.get(f"/api/reimbursements/drafts/{draft['id']}").json()["data"]["department"]
        == draft["department"]
    )
    assert client.post("/api/calculate/totals", json=body).status_code == 403
