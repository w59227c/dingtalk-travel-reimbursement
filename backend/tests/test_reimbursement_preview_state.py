from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import mock_login
from test_oa_reimbursement_integration import LocalWorkflowBoundary, _persist_ready_draft, _schemas

from app.models.reimbursement import ReimbursementDraft, utc_now

pytest_plugins = ["test_oa_reimbursement_integration"]


def ready_form(client):
    reimbursement, travel, travel_type = _schemas()
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    identifier, _ = _persist_ready_draft(
        client, LocalWorkflowBoundary(reimbursement, travel), travel_type
    )
    return identifier, headers


def change_preview_state(client, identifier, state):
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, identifier)
        if state == "pending":
            draft.recovery_pending = True
        elif state in {"retired", "locked-history"}:
            draft.workspace_retired = True
        elif state == "expired-status":
            draft.status = "EXPIRED"
        elif state == "expired":
            draft.expires_at = utc_now() - timedelta(seconds=1)
        database.commit()


@pytest.mark.parametrize(
    ("state", "error_code"),
    [
        ("editable", None),
        ("locked-history", None),
        ("pending", "REIMBURSEMENT_RECOVERY_PENDING"),
        ("retired", "REIMBURSEMENT_DRAFT_REPLACED"),
        ("expired", "REIMBURSEMENT_DRAFT_EXPIRED"),
        ("expired-status", "REIMBURSEMENT_DRAFT_EXPIRED"),
    ],
)
def test_all_excel_preview_paths_enforce_current_state(
    enabled_manual_worker_client, state, error_code
):
    client = enabled_manual_worker_client
    identifier, headers = ready_form(client)
    url = f"/api/reimbursements/drafts/{identifier}/excel-preview"
    assert client.post(url, json={"expectedRevision": 4}, headers=headers).status_code == 200
    if state == "locked-history":
        submitted = client.post(
            f"/api/oa/reimbursements/{identifier}/submit",
            json={"expectedRevision": 4},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert submitted.status_code == 202, submitted.text
    with client.app.state.database_session_factory() as database:
        revision = database.get(ReimbursementDraft, identifier).revision
    payload = {"expectedRevision": revision}
    issued_before = client.post(f"{url}-ticket", json=payload, headers=headers)
    assert issued_before.status_code == 200, issued_before.text
    change_preview_state(client, identifier, state)
    desktop = client.post(url, json=payload, headers=headers)
    opened = client.get(url, params=payload)
    issued_after = client.post(f"{url}-ticket", json=payload, headers=headers)
    ticket = issued_before.json()["data"]
    client.cookies.clear()
    native = client.get(
        ticket["downloadUrl"],
        headers={"X-Reimbursement-Download-Token": ticket["downloadToken"]},
    )
    for response in (desktop, opened, issued_after, native):
        assert response.status_code == (409 if error_code else 200), response.text
        if error_code:
            assert response.json()["error"]["code"] == error_code


@pytest.mark.parametrize("state", ["pending", "retired", "expired"])
def test_preview_rechecks_access_after_generation_without_a_revision_change(
    enabled_manual_worker_client, monkeypatch, state
):
    from app.services import reimbursement_files

    client = enabled_manual_worker_client
    identifier, headers = ready_form(client)
    generate = reimbursement_files.generate_expense_workbook

    def change_during_generation(**kwargs):
        change_preview_state(client, identifier, state)
        return generate(**kwargs)

    monkeypatch.setattr(reimbursement_files, "generate_expense_workbook", change_during_generation)
    response = client.post(
        f"/api/reimbursements/drafts/{identifier}/excel-preview",
        json={"expectedRevision": 4},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    with client.app.state.database_session_factory() as database:
        assert database.get(ReimbursementDraft, identifier).revision == 4
