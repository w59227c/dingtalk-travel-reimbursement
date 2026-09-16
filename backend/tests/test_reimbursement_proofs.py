from __future__ import annotations

import json

import pytest
from conftest import mock_login
from test_reimbursement_drafts import _input
from test_reimbursement_files import _image_bytes, _insert_draft
from test_reimbursement_intake import _proof_setup

from app.core.errors import ApiError
from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile
from app.ocr.engine import FakeOcrEngine
from app.ocr.types import OcrLine
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services.reimbursement_drafts import apply_ocr_evidence, validate_draft_file_references


@pytest.mark.parametrize("disabled", [True, False])
def test_manual_itinerary_matching_intent_survives_put_get_without_changing_totals(
    client_factory, monkeypatch, disabled
):
    client, headers, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    before = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["totals"]
    value = _input()
    value["items"][0].update(sourceFileId=source, itineraryAutoMatchDisabled=disabled)
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        headers=headers,
        json={"expectedRevision": 1, "input": value},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["input"]["items"][0]["itineraryAutoMatchDisabled"] is disabled
    loaded = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert loaded["input"]["items"][0]["itineraryAutoMatchDisabled"] is disabled
    assert loaded["totals"] == before


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_itinerary_matching_intent_requires_a_strict_boolean(client_factory, monkeypatch, value):
    client, headers, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    draft_input = _input()
    draft_input["items"][0].update(sourceFileId=source, itineraryAutoMatchDisabled=value)
    rejected = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        headers=headers,
        json={"expectedRevision": 1, "input": draft_input},
    )
    assert rejected.status_code == 422, rejected.text
    assert client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["revision"] == 1


def test_saving_source_receipt_normalizes_count_but_manual_aggregate_keeps_count(
    client_factory, monkeypatch
):
    client, headers, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(sourceFileId=source, receiptCount=9)
    value["items"].append({**_input()["items"][0], "receiptCount": 3})
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        headers=headers,
        json={"expectedRevision": 1, "input": value},
    )
    assert saved.status_code == 200, saved.text
    assert [item["receiptCount"] for item in saved.json()["data"]["input"]["items"]] == [1, 3]
    restored = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert [item["receiptCount"] for item in restored["input"]["items"]] == [1, 3]
    assert restored["totals"]["receiptCount"] == 4


def test_payment_threshold_does_not_divide_amount_by_manual_receipt_count(
    client_factory, monkeypatch
):
    client, _, draft_id, _, _ = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(amount="501.00", receiptCount=2)
    with client.app.state.database_session_factory() as database:
        with pytest.raises(ApiError, match="付款凭证"):
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=ReimbursementDraftInput.model_validate(value),
                require_submission_proofs=True,
            )


def test_new_draft_cannot_reference_payment_file_from_an_existing_draft(
    client_factory, monkeypatch
):
    client, headers, _, _, support = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0]["paymentProofFileIds"] = [support]
    created = client.post(
        "/api/reimbursements/drafts",
        headers=headers,
        json={"expectedRevision": 0, "input": value},
    )
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "REIMBURSEMENT_DRAFT_FILE_REFERENCE_INVALID"


@pytest.mark.parametrize(("amount", "required"), [("500.00", False), ("500.01", True)])
def test_payment_proof_uses_strict_confirmed_cny_threshold(
    client_factory, monkeypatch, amount, required
):
    client, _, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(amount=amount, sourceFileId=source)
    draft_input = ReimbursementDraftInput.model_validate(value)
    with client.app.state.database_session_factory() as database:
        if required:
            with pytest.raises(ApiError, match="付款凭证"):
                validate_draft_file_references(
                    database,
                    draft_id=draft_id,
                    draft_input=draft_input,
                    require_submission_proofs=True,
                )
        else:
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=draft_input,
                require_submission_proofs=True,
            )


@pytest.mark.parametrize(
    ("category", "rail_type", "evidence", "required"),
    [
        ("rail_fare", "high_speed", {"categoryId": "rail_fare", "railType": "high_speed"}, False),
        ("rail_fare", "regular", {"categoryId": "rail_fare", "railType": "regular"}, True),
        ("rail_fare", "emu", {"categoryId": "rail_fare", "railType": "emu"}, True),
        ("rail_fare", "unknown", {"categoryId": "rail_fare", "railType": "unknown"}, True),
        ("rail_fare", "high_speed", {"categoryId": "rail_fare", "railType": "unknown"}, False),
        ("rail_fare", "high_speed", {"categoryId": "other"}, False),
        ("rail_fare", "high_speed", {"categoryId": "other", "status": "failed"}, False),
        ("rail_fare", "high_speed", {"categoryId": "lodging"}, True),
        ("rail_fare", "high_speed", {"categoryId": "local_transport"}, True),
        ("other", "high_speed", {"categoryId": "rail_fare", "railType": "high_speed"}, True),
        ("rail_fare", "high_speed", {"categoryId": "rail_fare", "railType": "regular"}, True),
    ],
)
def test_rail_exemption_uses_category_and_authoritative_subtype(
    client_factory, monkeypatch, category, rail_type, evidence, required
):
    client, _, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(
        amount="600.00",
        sourceFileId=source,
        category=category,
        railType=rail_type,
    )
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraftFile, source).ocr_result_json = json.dumps(evidence)
        database.commit()
        normalized = apply_ocr_evidence(
            database,
            draft_id=draft_id,
            draft_input=ReimbursementDraftInput.model_validate(value),
        )
        if category != "rail_fare":
            assert normalized.items[0].rail_type == "unknown"
        if required:
            with pytest.raises(ApiError, match="付款凭证"):
                validate_draft_file_references(
                    database,
                    draft_id=draft_id,
                    draft_input=normalized,
                    require_submission_proofs=True,
                )
        else:
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=normalized,
                require_submission_proofs=True,
            )


@pytest.mark.parametrize(("amount", "required"), [("499.00", False), ("501.00", True)])
def test_foreign_receipt_threshold_uses_confirmed_cny_not_original_ocr_total(
    client_factory, monkeypatch, amount, required
):
    client, _, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(
        amount=amount,
        sourceFileId=source,
        originalCurrency="VND",
        originalAmount="97600000.00",
        cnyAmountConfirmed=True,
    )
    with client.app.state.database_session_factory() as database:
        draft_input = ReimbursementDraftInput.model_validate(value)
        if required:
            with pytest.raises(ApiError, match="付款凭证"):
                validate_draft_file_references(
                    database,
                    draft_id=draft_id,
                    draft_input=draft_input,
                    require_submission_proofs=True,
                )
        else:
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=draft_input,
                require_submission_proofs=True,
            )


@pytest.mark.parametrize("kind", ["payment_proof", "itinerary", "other"])
def test_payment_reference_requires_correct_supporting_purpose(client_factory, monkeypatch, kind):
    client, _, draft_id, source, support = _proof_setup(client_factory, monkeypatch)
    value = _input()
    value["items"][0].update(amount="600.00", sourceFileId=source, paymentProofFileIds=[support])
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraftFile, support).attachment_kind = kind
        database.commit()
        draft_input = ReimbursementDraftInput.model_validate(value)
        if kind == "payment_proof":
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=draft_input,
                require_submission_proofs=True,
            )
        else:
            with pytest.raises(ApiError, match="票据文件无效"):
                validate_draft_file_references(
                    database,
                    draft_id=draft_id,
                    draft_input=draft_input,
                    require_submission_proofs=True,
                )


@pytest.mark.parametrize("operation", ["purpose", "role", "delete"])
def test_changing_payment_file_clears_only_its_proof_references(
    client_factory, monkeypatch, operation
):
    client, headers, draft_id, source, support = _proof_setup(client_factory, monkeypatch)
    uploaded = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={
            "expectedRevision": 1,
            "role": "ATTACHMENT_ONLY",
            "attachmentKind": "payment_proof",
        },
        headers=headers,
        files=[("files[]", ("payment.png", _image_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    support = uploaded.json()["data"]["file"]["id"]
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraftFile, support).attachment_kind = "payment_proof"
        draft = database.get(ReimbursementDraft, draft_id)
        value = json.loads(draft.input_json)
        value["items"][0].update(sourceFileId=source, paymentProofFileIds=[support])
        draft.input_json = json.dumps(value)
        revision = draft.revision
        database.commit()
    url = f"/api/reimbursements/drafts/{draft_id}/files/{support}"
    if operation == "delete":
        response = client.delete(url, headers=headers, params={"expectedRevision": revision})
    else:
        change = (
            {"attachmentKind": "other"} if operation == "purpose" else {"role": "EXPENSE_SOURCE"}
        )
        response = client.patch(url, headers=headers, json={"expectedRevision": revision, **change})
    assert response.status_code == 200, response.text
    loaded = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]
    assert loaded["input"]["items"][0]["paymentProofFileIds"] == []
    assert loaded["input"]["items"][0]["sourceFileId"] == source


@pytest.mark.parametrize("kind", ["itinerary", "payment_proof", "other"])
def test_upload_persists_explicit_attachment_purpose(client_factory, kind):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={"expectedRevision": 1, "role": "ATTACHMENT_ONLY", "attachmentKind": kind},
        headers={"X-CSRF-Token": csrf},
        files=[("files[]", ("proof.png", _image_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["data"]["file"]["attachmentKind"] == kind
    listed = client.get(f"/api/reimbursements/drafts/{draft_id}/files")
    assert listed.json()["data"]["items"][0]["attachmentKind"] == kind


def test_itinerary_ocr_is_persisted_without_adding_an_expense(client_factory):
    engine = FakeOcrEngine(
        {
            "*": [
                OcrLine("出行行程单 人民币", 1),
                OcrLine("行程金额合计：12.30元", 1),
                OcrLine("乘车日期：2026-09-03", 1),
                OcrLine("起点：测试起点", 1),
                OcrLine("终点：测试终点", 1),
                OcrLine("订单号：ORDER-00123", 1),
            ]
        }
    )
    client = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=engine)
    headers = {"X-CSRF-Token": str(mock_login(client)["csrfToken"])}
    draft_id = _insert_draft(client)
    before = client.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["input"]
    uploaded = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={"expectedRevision": 1, "role": "ATTACHMENT_ONLY", "attachmentKind": "itinerary"},
        headers=headers,
        files=[("files[]", ("itinerary.png", _image_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["data"]["file"]["id"]
    recognized = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers=headers,
        json={"expectedRevision": 2, "tripYear": 2026},
    )
    assert recognized.status_code == 200, recognized.text
    file = recognized.json()["data"]["file"]
    assert file["ocrStatus"] == "COMPLETE"
    assert file["ocrResult"]["kind"] == "itinerary"
    assert file["ocrResult"]["summary"]["amount"] == "12.30"
    assert file["ocrResult"]["trips"][0]["date"] == "2026-09-03"
    assert file["ocrResult"]["trips"][0]["orderNumbers"] == ["ORDER-00123"]
    restarted = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=engine)
    mock_login(restarted)
    restored = restarted.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"][
        "items"
    ][0]
    assert restored["attachmentKind"] == "itinerary"
    assert restored["ocrResult"] == file["ocrResult"]
    assert restarted.get(f"/api/reimbursements/drafts/{draft_id}").json()["data"]["input"] == before


@pytest.mark.parametrize("kind", ["payment_proof", "other"])
def test_non_itinerary_support_ocr_is_rejected_without_mutating_file(client_factory, kind):
    client = client_factory(auth_mock_enabled=True, ocr_enabled=True, ocr_engine=FakeOcrEngine())
    headers = {"X-CSRF-Token": str(mock_login(client)["csrfToken"])}
    draft_id = _insert_draft(client)
    uploaded = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={"expectedRevision": 1, "role": "ATTACHMENT_ONLY", "attachmentKind": kind},
        headers=headers,
        files=[("files[]", ("proof.png", _image_bytes(), "image/png"))],
    )
    file_id = uploaded.json()["data"]["file"]["id"]
    denied = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers=headers,
        json={"expectedRevision": 2},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "REIMBURSEMENT_FILE_OCR_NOT_ALLOWED"
    listed = client.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"]
    assert listed["revision"] == 2
    assert listed["items"][0]["ocrStatus"] == "NOT_REQUESTED"
    assert listed["items"][0]["ocrResult"] is None


def test_itinerary_ocr_failure_keeps_itinerary_kind_and_can_be_read_back(client_factory):
    client = client_factory(auth_mock_enabled=True, ocr_enabled=False)
    headers = {"X-CSRF-Token": str(mock_login(client)["csrfToken"])}
    draft_id = _insert_draft(client)
    uploaded = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={"expectedRevision": 1, "role": "ATTACHMENT_ONLY", "attachmentKind": "itinerary"},
        headers=headers,
        files=[("files[]", ("itinerary.png", _image_bytes(), "image/png"))],
    )
    file_id = uploaded.json()["data"]["file"]["id"]
    failed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers=headers,
        json={"expectedRevision": 2},
    )
    assert failed.status_code == 200, failed.text
    payload = failed.json()["data"]["file"]
    assert payload["ocrStatus"] == "FAILED"
    assert payload["ocrResult"]["kind"] == "itinerary"
    assert payload["ocrResult"]["error"]["code"] == "OCR_DISABLED"
    assert "OCR" not in payload["ocrResult"]["error"]["message"]
    assert "自动识别" in payload["ocrResult"]["error"]["message"]
    assert payload["ocrResult"]["summary"]["amount"] is None
    assert payload["ocrResult"]["trips"] == []
    restored = client.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"]["items"][0]
    assert restored["ocrResult"] == payload["ocrResult"]


@pytest.mark.parametrize("stay_details", [False, True])
def test_review_submit_and_snapshot_order_enforce_new_payment_rule(
    client_factory, monkeypatch, stay_details
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
    draft_id, sources = _persist_ready_draft(
        client,
        LocalWorkflowBoundary(reimbursement, travel),
        travel_type,
    )
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        value = json.loads(draft.input_json)
        value["items"][0].update(amount="500.01", itineraryFileIds=[sources[1]])
        database.get(ReimbursementDraftFile, sources[1]).attachment_kind = "itinerary"
        if stay_details:
            value["items"][0].update(
                category="lodging", itineraryFileIds=[], hotelBillFileIds=[sources[1]]
            )
            database.get(ReimbursementDraftFile, sources[1]).attachment_kind = "hotel_bill"
        draft.input_json = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        database.commit()
    rejected = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        headers=headers,
        json={"expectedRevision": 4},
    )
    assert rejected.status_code == 409, rejected.text
    assert "付款凭证" in rejected.json()["error"]["message"]
    rejected_submit = client.post(
        f"/api/oa/reimbursements/{draft_id}/submit",
        headers={**headers, "Idempotency-Key": "11111111-1111-4111-8111-111111111111"},
        json={"expectedRevision": 4},
    )
    assert rejected_submit.status_code == 409, rejected_submit.text
    assert "付款凭证" in rejected_submit.json()["error"]["message"]
    with client.app.state.database_session_factory() as database:
        assert database.scalar(select(ReimbursementSubmission)) is None
        assert database.get(ReimbursementDraft, draft_id).locked_at is None

    uploaded = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        headers=headers,
        params={
            "expectedRevision": 4,
            "role": "ATTACHMENT_ONLY",
            "attachmentKind": "payment_proof",
        },
        files=[("files[]", ("payment.png", _image_bytes(), "image/png"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    payment_id = uploaded.json()["data"]["file"]["id"]
    value["items"][0]["paymentProofFileIds"] = [payment_id]
    saved = client.put(
        f"/api/reimbursements/drafts/{draft_id}",
        headers=headers,
        json={"expectedRevision": 5, "input": value},
    )
    assert saved.status_code == 200, saved.text
    reviewed = client.post(
        f"/api/reimbursements/drafts/{draft_id}/review",
        headers=headers,
        json={"expectedRevision": 6},
    )
    assert reviewed.status_code == 200, reviewed.text
    accepted = client.post(
        f"/api/oa/reimbursements/{draft_id}/submit",
        headers={**headers, "Idempotency-Key": "11111111-1111-4111-8111-111111111111"},
        json={"expectedRevision": 7},
    )
    assert accepted.status_code == 202, accepted.text
    with client.app.state.database_session_factory() as database:
        snapshot = json.loads(database.scalar(select(ReimbursementSubmission)).form_snapshot_json)
        assert snapshot["snapshotVersion"] == 6
        assert [file["draftFileId"] for file in snapshot["originalFiles"]] == [
            sources[0],
            sources[1],
            payment_id,
        ]
        assert [file["attachmentKind"] for file in snapshot["originalFiles"]] == [
            "other",
            "hotel_bill" if stay_details else "itinerary",
            "payment_proof",
        ]
