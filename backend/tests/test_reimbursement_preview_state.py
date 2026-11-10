import json
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import mock_login
from test_oa_reimbursement_integration import LocalWorkflowBoundary, _persist_ready_draft, _schemas

from app.models.reimbursement import (
    ReimbursementDraft,
    ReimbursementDraftFile,
    ReimbursementSubmission,
    utc_now,
)
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services.oa_reimbursement_payload import parse_snapshot
from app.services.reimbursement_drafts import validate_draft_file_references

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


@pytest.mark.parametrize(
    ("kind", "field", "label"),
    [
        ("itinerary", "itineraryFileIds", "行程单"),
        ("hotel_bill", "hotelBillFileIds", "住宿明细"),
        ("payment_proof", "paymentProofFileIds", "付款凭证"),
    ],
)
@pytest.mark.parametrize("resolution", ["associate", "other", "delete"])
def test_unassociated_proofs_block_all_export_paths_but_allow_saving_and_resolution(
    enabled_manual_worker_client, kind, field, label, resolution,
):
    client = enabled_manual_worker_client
    identifier, headers = ready_form(client)
    base = f"/api/reimbursements/drafts/{identifier}"
    ticket = client.post(
        f"{base}/excel-preview-ticket", json={"expectedRevision": 4}, headers=headers,
    ).json()["data"]
    with client.app.state.database_session_factory() as database:
        file = database.query(ReimbursementDraftFile).filter_by(
            draft_id=identifier, processing_role="ATTACHMENT_ONLY",
        ).one()
        file.attachment_kind = kind
        file_id = file.id
        database.commit()
        # The worker validates an already frozen manifest under its original policy.
        draft = database.get(ReimbursementDraft, identifier)
        validate_draft_file_references(
            database, draft_id=identifier,
            draft_input=ReimbursementDraftInput.model_validate_json(draft.input_json),
            require_terminal_disposition=True, require_submission_proofs=True,
            require_linked_support_files=False,
        )
    payload = {"expectedRevision": 4}
    responses = [
        client.post(f"{base}/excel-preview", json=payload, headers=headers),
        client.get(f"{base}/excel-preview", params=payload),
        client.post(f"{base}/excel-preview-ticket", json=payload, headers=headers),
        client.get(ticket["downloadUrl"], headers={
            "X-Reimbursement-Download-Token": ticket["downloadToken"],
        }),
        client.post(f"{base}/review", json=payload, headers=headers),
        client.post(f"/api/oa/reimbursements/{identifier}/submit", json=payload,
                    headers={**headers, "Idempotency-Key": str(uuid4())}),
    ]
    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_NOT_READY"
        assert label in response.json()["error"]["message"]
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, identifier)
        assert draft.revision == 4
        assert draft.status == "REVIEW_READY"
        assert database.query(ReimbursementSubmission).count() == 0
        saved_input = json.loads(draft.input_json)
    # Autosave is intentionally allowed while association is incomplete.
    saved = client.put(base, json={**payload, "input": saved_input}, headers=headers)
    assert saved.status_code == 200, saved.text
    revision = saved.json()["data"]["revision"]
    if resolution == "associate":
        saved_input["items"][0][field] = [file_id]
        if kind == "itinerary":
            saved_input["items"][0].update(transportType="ride_hailing", requiresItinerary=True)
        elif kind == "hotel_bill":
            saved_input["items"][0]["category"] = "lodging"
        linked = client.put(
            base, json={"expectedRevision": revision, "input": saved_input}, headers=headers,
        )
        assert linked.status_code == 200, linked.text
        revision = linked.json()["data"]["revision"]
    else:
        if resolution == "other":
            resolved = client.patch(
                f"{base}/files/{file_id}", headers=headers,
                json={"expectedRevision": revision, "attachmentKind": "other"},
            )
        else:
            resolved = client.delete(
                f"{base}/files/{file_id}", params={"expectedRevision": revision},
                headers=headers,
            )
        assert resolved.status_code == 200, resolved.text
        revision = resolved.json()["data"]["revision"]
    reloaded = client.get(base).json()["data"]
    if resolution == "associate":
        assert reloaded["input"]["items"][0][field] == [file_id]
    preview = client.post(
        f"{base}/excel-preview", json={"expectedRevision": revision}, headers=headers,
    )
    assert preview.status_code == 200, preview.text
    reviewed = client.post(
        f"{base}/review", json={"expectedRevision": revision}, headers=headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    submitted = client.post(
        f"/api/oa/reimbursements/{identifier}/submit",
        json={"expectedRevision": reviewed.json()["data"]["revision"]},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert submitted.status_code == 202, submitted.text
    with client.app.state.database_session_factory() as database:
        submission = database.query(ReimbursementSubmission).one()
        snapshot = parse_snapshot(
            submission.form_snapshot_json, expected_sha256=submission.snapshot_sha256,
        )
        assert (file_id in {file.draft_file_id for file in snapshot.original_files}) == (
            resolution != "delete"
        )
