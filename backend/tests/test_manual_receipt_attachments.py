import json
from dataclasses import replace

import pytest
from conftest import mock_login
from pydantic import ValidationError
from test_oa_reimbursement_payload import _source
from test_reimbursement_drafts import _create, _input, _install_catalog
from test_reimbursement_files import _upload

from app.core.errors import ApiError
from app.domain.expenses import calculate_expense_totals
from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile, utc_now
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services.oa_reimbursement_payload import build_snapshot, parse_snapshot, serialize_snapshot
from app.services.reimbursement_drafts import validate_draft_file_references


def _setup(client_factory, monkeypatch):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True, ocr_enabled=False)
    csrf = str(mock_login(client)["csrfToken"])
    headers = {"X-CSRF-Token": csrf}
    draft_id = _create(client, headers).json()["data"]["id"]
    uploaded = _upload(client, csrf, draft_id, revision=1, role="ATTACHMENT_ONLY")
    assert uploaded.status_code == 201, uploaded.text
    file = uploaded.json()["data"]["file"]
    assert file["ocrStatus"] == "NOT_REQUESTED" and file["ocrResult"] is None
    return client, headers, draft_id, file["id"]


def test_manual_aggregate_upload_restore_revision_and_delete_keep_manual_count(
    client_factory, monkeypatch
):
    client, headers, draft_id, file_id = _setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(
        category="local_transport", transportType="taxi", receiptCount=12,
        amount="240.00", receiptAttachmentFileIds=[file_id],
    )
    response = client.put(
        f"/api/reimbursements/drafts/{draft_id}", headers=headers,
        json={"expectedRevision": 2, "input": value},
    )
    assert response.status_code == 200, response.text
    restored = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert restored["input"]["items"][0]["receiptAttachmentFileIds"] == [file_id]
    assert restored["input"]["items"][0]["receiptCount"] == 12
    assert restored["input"]["items"][0].get("sourceFileId") is None
    assert restored["totals"]["receiptCount"] == 12
    conflict = client.put(
        f"/api/reimbursements/drafts/{draft_id}", headers=headers,
        json={"expectedRevision": 2, "input": value},
    )
    assert conflict.status_code == 409
    deleted = client.delete(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}", headers=headers,
        params={"expectedRevision": 3},
    )
    assert deleted.status_code == 200, deleted.text
    item = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["input"]["items"][0]
    assert not item.get("receiptAttachmentFileIds") and item["receiptCount"] == 12
    assert item["amount"] == "240.00"


@pytest.mark.parametrize("invalid", ["missing", "foreign_draft", "wrong_kind", "shared", "source"])
def test_manual_receipt_references_are_owned_and_exclusive(client_factory, monkeypatch, invalid):
    client, headers, draft_id, file_id = _setup(client_factory, monkeypatch)
    value = _input()
    item = value["items"][0]
    item["receiptAttachmentFileIds"] = [file_id]
    if invalid == "missing":
        item["receiptAttachmentFileIds"] = ["missing-file"]
    elif invalid == "foreign_draft":
        draft_id = _create(client, headers).json()["data"]["id"]
    elif invalid == "wrong_kind":
        with client.app.state.database_session_factory() as database:
            database.get(ReimbursementDraftFile, file_id).attachment_kind = "payment_proof"
            database.commit()
    elif invalid == "shared":
        value["items"].append(dict(item))
    else:
        item["sourceFileId"] = file_id
    response = client.put(
        f"/api/reimbursements/drafts/{draft_id}", headers=headers,
        json={"expectedRevision": 1 if invalid == "foreign_draft" else 2, "input": value},
    )
    assert response.status_code == 422, response.text


def test_manual_receipt_photo_does_not_replace_required_payment_proof(client_factory, monkeypatch):
    client, _, draft_id, file_id = _setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(amount="600.00", receiptCount=1, receiptAttachmentFileIds=[file_id])
    with client.app.state.database_session_factory() as database:
        with pytest.raises(ApiError, match="付款凭证"):
            validate_draft_file_references(
                database, draft_id=draft_id,
                draft_input=ReimbursementDraftInput.model_validate(value),
                require_submission_proofs=True,
            )


def test_manual_aggregate_photo_exempts_total_amount_without_requiring_itinerary(
    client_factory, monkeypatch
):
    client, headers, draft_id, file_id = _setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(
        category="local_transport", transportType="taxi", requiresItinerary=True,
        amount="12000.00", receiptCount=20, receiptAttachmentFileIds=[file_id],
    )
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}", headers=headers,
        json={"expectedRevision": 2, "input": value},
    )
    assert saved.status_code == 200, saved.text
    restored = saved.json()["data"]["input"]
    assert restored["items"][0]["requiresItinerary"] is False
    with client.app.state.database_session_factory() as database:
        validate_draft_file_references(
            database, draft_id=draft_id,
            draft_input=ReimbursementDraftInput.model_validate(restored),
            require_submission_proofs=True,
        )


def test_locked_manual_receipt_metadata_survives_file_body_cleanup(client_factory, monkeypatch):
    client, headers, draft_id, file_id = _setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0]["receiptAttachmentFileIds"] = [file_id]
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}", headers=headers,
        json={"expectedRevision": 2, "input": value},
    )
    assert saved.status_code == 200
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        draft.status = "LOCKED"
        draft.locked_at = utc_now()
        file = database.get(ReimbursementDraftFile, file_id)
        file.file_status = "PURGED"
        file.purged_at = utc_now()
        database.commit()
    files = client.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"]["items"]
    assert [(file["id"], file["status"]) for file in files] == [(file_id, "PURGED")]


def test_manual_receipt_is_frozen_in_v8_and_legacy_snapshots_still_roundtrip():
    source = _source()
    original = replace(
        source.original_files[0], processing_role="ATTACHMENT_ONLY", ocr_status="NOT_REQUESTED"
    )
    item = source.draft_input.items[0].model_copy(update={
        "source_file_id": None, "receipt_count": 12,
        "receipt_attachment_file_ids": [original.draft_file_id],
    })
    totals = {**source.totals_data, "receiptCount": source.totals_data["receiptCount"] + 11}
    aggregate = replace(
        source, original_files=(original, *source.original_files[1:]), totals_data=totals,
        draft_input=source.draft_input.model_copy(
            update={"items": [item, *source.draft_input.items[1:]]}
        ),
    )
    snapshot = build_snapshot(aggregate)
    encoded = serialize_snapshot(snapshot)
    assert json.loads(encoded)["input"]["items"][0]["receiptAttachmentFileIds"] == [
        original.draft_file_id
    ]
    assert parse_snapshot(encoded) == snapshot
    assert snapshot.input.items[0].receipt_count == 12
    with pytest.raises(ValidationError, match="receipt attachments"):
        serialize_snapshot(snapshot.model_copy(update={
            "snapshot_version": 7, "attachment_mode": None,
        }))
    legacy = build_snapshot(source).model_copy(update={
        "snapshot_version": 7, "attachment_mode": None,
    })
    assert parse_snapshot(serialize_snapshot(legacy)) == legacy


def test_v8_aggregate_payment_exemption_does_not_change_frozen_legacy_rules():
    source = _source()
    value = source.draft_input.model_dump(mode="json", by_alias=True)
    value.update(trip=None, trips=[])
    value["items"][0].update(sourceFileId=None, receiptCount=12, amount="600.00")
    draft_input = ReimbursementDraftInput.model_validate(value)
    original = replace(
        source.original_files[0], processing_role="ATTACHMENT_ONLY", ocr_status="NOT_REQUESTED"
    )
    snapshot = build_snapshot(replace(
        source, draft_input=draft_input,
        original_files=(original, *source.original_files[1:]),
        totals_data=calculate_expense_totals(draft_input.items, None).as_api_dict(),
    ))
    assert parse_snapshot(serialize_snapshot(snapshot)) == snapshot
    for legacy_version in (6, 7):
        with pytest.raises(ValidationError, match="payment proof"):
            serialize_snapshot(snapshot.model_copy(update={
                "snapshot_version": legacy_version, "attachment_mode": None,
            }))


def test_manual_aggregate_review_submit_freezes_photo_order_and_retries_idempotently(
    client_factory, monkeypatch
):
    from sqlalchemy import select
    from test_oa_reimbursement_integration import (
        LocalWorkflowBoundary,
        _persist_ready_draft,
        _schemas,
    )

    from app.models.reimbursement import ReimbursementSubmission
    from app.services.oa_reimbursement import DurableOAReimbursementWorker

    async def wait_for_stop(_self, stop):
        await stop.wait()

    monkeypatch.setattr(DurableOAReimbursementWorker, "run", wait_for_stop)
    client = client_factory(auth_mock_enabled=True, dingtalk_oa_worker_enabled=True)
    headers = {"X-CSRF-Token": str(mock_login(client)["csrfToken"])}
    reimbursement, travel, travel_type = _schemas()
    draft_id, files = _persist_ready_draft(
        client, LocalWorkflowBoundary(reimbursement, travel), travel_type,
    )
    value = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["input"]
    value["items"].insert(0, {
        **value["items"][0], "sourceFileId": None, "transportType": "taxi",
        "receiptCount": 12, "amount": "1240.00", "receiptAttachmentFileIds": [files[1]],
    })
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}", headers=headers,
        json={"expectedRevision": 4, "input": value},
    )
    assert saved.status_code == 200, saved.text
    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review", headers=headers,
        json={"expectedRevision": 5},
    )
    assert reviewed.status_code == 200, reviewed.text
    submit_headers = {**headers, "Idempotency-Key": "11111111-1111-4111-8111-111111111111"}
    for _ in range(2):
        accepted = client.post(
            f"/api/oa/reimbursements/{draft_id}/submit", headers=submit_headers,
            json={"expectedRevision": 6},
        )
        assert accepted.status_code == 202, accepted.text
    with client.app.state.database_session_factory() as database:
        submissions = database.scalars(select(ReimbursementSubmission)).all()
        assert len(submissions) == 1
        snapshot = parse_snapshot(
            submissions[0].form_snapshot_json, expected_sha256=submissions[0].snapshot_sha256,
        )
        assert snapshot.snapshot_version == 10
        assert snapshot.input.items[0].receipt_attachment_file_ids == (files[1],)
        assert snapshot.input.items[0].receipt_count == 12
        assert not snapshot.input.items[0].payment_proof_file_ids
        assert not snapshot.input.items[0].itinerary_file_ids
        assert [file.draft_file_id for file in snapshot.original_files] == [files[1], files[0]]
        assert database.get(ReimbursementDraft, draft_id).status == "LOCKED"
