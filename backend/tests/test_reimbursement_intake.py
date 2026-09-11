from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from conftest import mock_login
from openpyxl import load_workbook
from pypdf import PdfWriter
from test_reimbursement_drafts import _add_active_file, _create, _input, _install_catalog
from test_reimbursement_files import _image_bytes, _insert_draft, _upload

from app.core.errors import ApiError
from app.excel.template_contract import EXCEL_TEMPLATE
from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile, utc_now
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services import reimbursement_files
from app.services.reimbursement_drafts import (
    apply_ocr_evidence,
    require_complete_draft_input,
    validate_draft_file_references,
)


def test_partial_input_autosaves_and_incomplete_values_never_enter_totals(
    client_factory, monkeypatch
):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    body = {
        "companyValue": "",
        "budgetCodeValue": "",
        "items": [
            {
                "category": "other",
                "date": None,
                "amount": None,
                "description": "",
                "displayDate": "",
                "receiptCount": 1,
            }
        ],
        "editingState": {
            "includeSubsidy": True,
            "trip": {
                "tripType": "business",
                "startDate": "2026-09-01",
                "endDate": "",
                "startTime": "09:00",
                "endTime": "",
            },
        },
    }
    response = _create(client, headers, body)
    assert response.status_code == 201, response.text
    saved = response.json()["data"]
    assert saved["input"]["project"] is None
    assert saved["input"]["items"][0]["amount"] is None
    assert saved["totals"]["totalAmount"] == "0.00"
    loaded = client.get(f"/api/reimbursements/drafts/{saved['id']}").json()["data"]
    assert loaded["input"] == saved["input"]
    with pytest.raises(ApiError, match="补齐"):
        require_complete_draft_input(ReimbursementDraftInput.model_validate(saved["input"]))


def test_budget_option_ignores_client_project_and_full_label_reaches_excel(
    client_factory, monkeypatch
):
    from dataclasses import replace

    from test_reimbursement_drafts import (
        FakeTravelWorkflow,
        _catalog_with_travel,
        _selection,
        _travel_instance,
    )

    from app.integrations.dingtalk.workflow import FormOption
    from app.services import reimbursement_drafts

    catalog = _catalog_with_travel()
    label = "26007 " + "国际项目预算明细" * 50
    catalog.reimbursement.schema.components[1].options = (
        FormOption(value="26007", label=label, key=None),
    )
    monkeypatch.setattr(reimbursement_drafts, "require_submission_ready_catalog", lambda _: catalog)
    monkeypatch.setattr(reimbursement_files, "require_submission_ready_catalog", lambda _: catalog)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    response = _create(client, headers, {**_input(), "project": {"mode": "selected", "id": 999999}})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["input"]["project"] is None

    class BudgetWorkflow(FakeTravelWorkflow):
        async def get_process_instance(self, instance_id):
            instance = _travel_instance(instance_id)
            return replace(
                instance,
                form_values=tuple(
                    replace(value, value=label) if value.component_id == "source-budget" else value
                    for value in instance.form_values
                ),
            )

    client.app.state.dingtalk_workflow = BudgetWorkflow()
    linked = client.put(
        f"/api/reimbursements/drafts/{data['id']}/related-approvals",
        headers=headers,
        json={"expectedRevision": 1, "selections": [_selection()]},
    )
    assert linked.status_code == 200, linked.text
    data = linked.json()["data"]
    assert data["input"]["project"] == {"mode": "manual", "text": label}
    preview = client.post(
        f"/api/reimbursements/drafts/{data['id']}/excel-preview",
        headers=headers,
        json={"expectedRevision": 2},
    )
    assert preview.status_code == 200, preview.text
    workbook = load_workbook(io.BytesIO(preview.content))
    try:
        assert workbook.active[EXCEL_TEMPLATE.project_cell].value == label
        assert workbook.active.row_dimensions[2].height > 35
    finally:
        workbook.close()


def _proof_setup(client_factory, monkeypatch):
    _install_catalog(monkeypatch)
    client = client_factory(auth_mock_enabled=True)
    headers = {"X-CSRF-Token": mock_login(client)["csrfToken"]}
    draft_id = _create(client, headers).json()["data"]["id"]
    source = _add_active_file(client, draft_id)
    support = _add_active_file(
        client,
        draft_id,
        processing_role="ATTACHMENT_ONLY",
        ocr_status="NOT_REQUESTED",
        record_disposition=False,
    )
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraftFile, support).attachment_kind = "itinerary"
        database.commit()
    return client, headers, draft_id, source, support


def test_authoritative_ride_hailing_evidence_cannot_be_cleared_by_client(
    client_factory, monkeypatch
):
    client, headers, draft_id, source, support = _proof_setup(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, source)
        file.ocr_result_json = json.dumps(
            {"requiresItinerary": True, "transportType": "ride_hailing"}
        )
        database.commit()
        value = ReimbursementDraftInput.model_validate(
            {
                **_input(),
                "items": [
                    {
                        **_input()["items"][0],
                        "sourceFileId": source,
                        "requiresItinerary": False,
                        "transportType": "taxi",
                    }
                ],
            }
        )
        canonical = apply_ocr_evidence(database, draft_id=draft_id, draft_input=value)
        assert canonical.items[0].requires_itinerary is True
        with pytest.raises(ApiError, match="缺少对应行程单"):
            validate_draft_file_references(
                database, draft_id=draft_id, draft_input=value, require_submission_proofs=True
            )
        canonical.items[0].itinerary_file_ids = [support]
        validate_draft_file_references(
            database,
            draft_id=draft_id,
            draft_input=canonical,
            require_terminal_disposition=True,
            require_submission_proofs=True,
        )


@pytest.mark.parametrize("bad_link", ["unknown", "invoice", "other_record", "deleted"])
@pytest.mark.parametrize(
    "proof_field", ["itineraryFileIds", "paymentProofFileIds", "hotelBillFileIds"]
)
def test_proof_must_be_active_support_from_same_application(
    client_factory,
    monkeypatch,
    bad_link,
    proof_field,
):
    client, headers, draft_id, source, support = _proof_setup(client_factory, monkeypatch)
    link = support
    if bad_link == "unknown":
        link = "unknown"
    elif bad_link == "invoice":
        link = source
    elif bad_link == "other_record":
        other_id = _create(client, headers).json()["data"]["id"]
        link = _add_active_file(
            client,
            other_id,
            processing_role="ATTACHMENT_ONLY",
            ocr_status="NOT_REQUESTED",
            record_disposition=False,
        )
    with client.app.state.database_session_factory() as database:
        linked_file = database.get(ReimbursementDraftFile, link)
        if linked_file is not None:
            linked_file.attachment_kind = {
                "paymentProofFileIds": "payment_proof",
                "hotelBillFileIds": "hotel_bill",
                "itineraryFileIds": "itinerary",
            }[proof_field]
        if bad_link == "deleted":
            database.get(ReimbursementDraftFile, support).file_status = "PURGED"
            database.get(ReimbursementDraftFile, support).purged_at = utc_now()
        database.commit()
        value = ReimbursementDraftInput.model_validate(
            {
                **_input(),
                "items": [
                    {
                        **_input()["items"][0],
                        "sourceFileId": source,
                        proof_field: [link],
                        "transportType": "ride_hailing",
                    }
                ],
            }
        )
        with pytest.raises(ApiError, match="票据文件无效"):
            validate_draft_file_references(
                database, draft_id=draft_id, draft_input=value, require_submission_proofs=True
            )


def test_foreign_ocr_cannot_be_submitted_as_unconfirmed_cny(client_factory, monkeypatch):
    client, _, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraftFile, source).ocr_result_json = json.dumps(
            {
                "originalCurrency": "VND",
                "originalAmount": "97600000.00",
                "amount": None,
            }
        )
        database.commit()
        value = ReimbursementDraftInput.model_validate(
            {
                **_input(),
                "items": [
                    {
                        **_input()["items"][0],
                        "sourceFileId": source,
                    }
                ],
            }
        )
        with pytest.raises(ApiError, match="人民币报销金额"):
            validate_draft_file_references(
                database, draft_id=draft_id, draft_input=value, require_submission_proofs=True
            )
        value.items[0].cny_amount_confirmed = True
        normalized = apply_ocr_evidence(database, draft_id=draft_id, draft_input=value)
        assert normalized.items[0].original_currency == "VND"
        assert str(normalized.items[0].original_amount) == "97600000.00"
        validate_draft_file_references(
            database, draft_id=draft_id, draft_input=normalized, require_submission_proofs=True
        )


@pytest.mark.parametrize(
    "ocr_evidence",
    [
        {"type": "foreign_receipt", "originalCurrency": None},
        {"warnings": ["FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT"], "originalCurrency": None},
    ],
)
def test_foreign_currency_unknown_still_requires_cny_confirmation(
    client_factory, monkeypatch, ocr_evidence
):
    client, _, draft_id, source, _ = _proof_setup(client_factory, monkeypatch)
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraftFile, source).ocr_result_json = json.dumps(ocr_evidence)
        database.commit()
        value = ReimbursementDraftInput.model_validate(
            {
                **_input(),
                "items": [{**_input()["items"][0], "sourceFileId": source}],
            }
        )
        with pytest.raises(ApiError, match="人民币报销金额"):
            validate_draft_file_references(
                database, draft_id=draft_id, draft_input=value, require_submission_proofs=True
            )
        normalized = apply_ocr_evidence(database, draft_id=draft_id, draft_input=value)
        assert normalized.items[0].original_currency is None
        assert normalized.items[0].requires_cny_confirmation is True
        with pytest.raises(ApiError, match="人民币报销金额"):
            require_complete_draft_input(normalized)
        normalized.items[0].cny_amount_confirmed = True
        require_complete_draft_input(normalized)
        validate_draft_file_references(
            database, draft_id=draft_id, draft_input=normalized, require_submission_proofs=True
        )


def test_original_preview_is_authenticated_private_and_expires(client_factory):
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1).json()["data"]["file"]
    url = f"/api/reimbursements/drafts/{draft_id}/files/{uploaded['id']}/content"
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == _image_bytes()
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["content-disposition"].startswith("inline;")
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        draft.owner_user_id = "another-employee"
        database.commit()
    assert client.get(url).status_code == 404
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        draft.owner_user_id = "mock-user"
        draft.expires_at = utc_now() - timedelta(seconds=1)
        database.commit()
    assert client.get(url).status_code == 409
    client.cookies.clear()
    assert client.get(url).status_code == 401


def test_supporting_pdf_can_span_pages_but_invoice_stays_single_page(client_factory):
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=595, height=842)
    writer.write(output)
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    draft_id = _insert_draft(client)
    invoice = _upload(
        client, csrf, draft_id, revision=1, name="发票.pdf", content=output.getvalue()
    )
    assert invoice.status_code == 400
    assert invoice.json()["error"]["code"] == "MULTI_PAGE_PDF_UNSUPPORTED"
    support = _upload(
        client,
        csrf,
        draft_id,
        revision=1,
        name="行程单.pdf",
        content=output.getvalue(),
        role="ATTACHMENT_ONLY",
    )
    assert support.status_code == 201, support.text


def test_preview_rejects_changed_file_bytes_and_wrong_department(client_factory):
    client = client_factory(auth_mock_enabled=True)
    csrf = mock_login(client)["csrfToken"]
    draft_id = _insert_draft(client)
    uploaded = _upload(client, csrf, draft_id, revision=1).json()["data"]["file"]
    url = f"/api/reimbursements/drafts/{draft_id}/files/{uploaded['id']}/content"
    with client.app.state.database_session_factory() as database:
        draft = database.get(ReimbursementDraft, draft_id)
        draft.department_id = "other-department"
        database.commit()
    assert client.get(url).status_code == 409
    with client.app.state.database_session_factory() as database:
        database.get(ReimbursementDraft, draft_id).department_id = "100"
        file = database.get(ReimbursementDraftFile, uploaded["id"])
        storage_path = client.app.state.reimbursement_staging.root / file.storage_key
        database.commit()
    storage_path.write_bytes(b"changed bytes")
    assert client.get(url).status_code == 409
