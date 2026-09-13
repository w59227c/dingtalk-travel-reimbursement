from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import mock_login
from pypdf import PdfWriter
from test_itinerary_ocr import _digital_pdf
from test_reimbursement_files import _draft_input, _image_bytes, _insert_draft, _named_image_bytes

from app.core.errors import ApiError
from app.domain.material_classification import material_classification
from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile
from app.ocr.engine import FakeOcrEngine, OcrRuntimeError
from app.ocr.itinerary import ItineraryPage
from app.ocr.itinerary_worker import recognize_itinerary_worker
from app.ocr.materials import classify_material
from app.ocr.parsers import ReceiptParserRegistry
from app.ocr.types import InvoiceQrEvidence, OcrLine, ParseContext
from app.schemas.reimbursements import ReimbursementDraftInput
from app.services.reimbursement_drafts import DraftActor, validate_draft_file_references
from app.services.reimbursement_files import recognize_draft_file

ITINERARY = (
    "出行行程单\n行程金额合计：12.30元\n乘车日期：2026-09-03\n"
    "起点：测试起点\n终点：测试终点\n订单号：ORDER-00123"
)
INVOICE = "电子发票\n开票日期：2026-09-03\n价税合计：12.30\n旅客运输服务"
PAYMENT = "付款成功\n支付金额：512.30元\n收款方：测试商户\n2026-09-03"


def _pages(text: str) -> list[ItineraryPage]:
    return [
        ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")
    ]


@pytest.mark.parametrize(
    "text,kind",
    [
        (INVOICE, "expense"),
        (ITINERARY, "itinerary"),
        (ITINERARY + "\n关联发票号码：12345678901234567890", "itinerary"),
        ("航空运输电子客票行程单\n起点：北京\n终点：上海\n合计：1230元", "expense"),
        ("铁路电子客票\n车次 G123\n票价 510元", "expense"),
        ("G123 2026-09-03 二等座 北京南 → 上海虹桥 ￥510.00", "expense"),
        ("Guest Invoice\nTotal: 97600000 VND", "expense"),
        (PAYMENT, "payment_proof"),
        ("Payment receipt\nAmount: 123.00 USD\nPayee: Example", "payment_proof"),
        ("金额：999\n2026-09-03\n起点：测试起点\n终点：测试终点", "unknown"),
        ("行程单\n请在出差后提交相关材料", "unknown"),
        (INVOICE + "\n备注：需要附行程单", "expense"),
        (ITINERARY + "\n" + INVOICE, "unknown"),
        (PAYMENT + "\n" + INVOICE, "unknown"),
    ],
)
def test_routing_uses_document_structure_not_keywords_alone(text, kind):
    assert classify_material(_pages(text), page_count=1)[0] == kind


def test_multi_page_invoice_and_mixed_documents_are_not_silently_first_page_expenses():
    assert classify_material(_pages(INVOICE), page_count=2)[0] == "unknown"


def test_photographed_meter_receipt_with_obscured_title_uses_one_ocr_pass(
    settings_factory, tmp_path
):
    # The stamp obscures 机打 in the title; tilted OCR places the total before its label.
    text = (
        "通用发票\n发票代码：123456789012\n发票号码：12345678\n车号\n皖A\n工号\n"
        "2026-07-05\n日期\n22:07-22:42\n时间\n单价\n3.00元\n里程\n45.1km\n"
        "00:00.07\n等候\n除客户名称外手写无效\n128.60元\n金额\n卡原额\n余额发票专用章"
    )

    class CountingOcr(FakeOcrEngine):
        calls = 0

        def recognize(self, _path):
            self.calls += 1
            return [OcrLine(value, 0.95) for value in text.splitlines()]

    image = tmp_path / "unrelated-name.jpg"
    image.write_bytes(_image_bytes())
    engine = CountingOcr()
    settings = settings_factory()
    result = recognize_itinerary_worker(
        str(image),
        "jpg",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        engine,
        True,
    )
    assert result["materialKind"] == "expense"
    parsed = result["expense"]
    assert parsed.receipt_type == "taxi_receipt"
    assert parsed.amount == Decimal("128.60")
    assert parsed.date == date(2026, 7, 5)
    assert parsed.transport_type == "taxi" and not parsed.requires_itinerary
    assert engine.calls == 1


def test_meter_fields_without_invoice_evidence_still_require_material_confirmation():
    assert (
        classify_material(_pages("车号 A12345\n工号 123\n里程 45.1km\n单价 3元"), page_count=1)[0]
        == "unknown"
    )


@pytest.mark.parametrize("font_failure", [False, True])
def test_native_pdf_classifies_and_parses_in_one_extraction(
    settings_factory, tmp_path, monkeypatch, font_failure
):
    if font_failure:
        from pypdf import PageObject
        from pypdf.errors import PdfReadError

        def failed_text(*args, **kwargs):
            raise PdfReadError("More than one /FontFile found")

        monkeypatch.setattr(PageObject, "extract_text", failed_text)

    class NoOcr(FakeOcrEngine):
        def recognize(self, _path):
            raise AssertionError("native text should not start OCR")

    settings = settings_factory()
    stored = _digital_pdf(
        tmp_path / "irrelevant-name.pdf",
        ("Guest Invoice\nDate: 2026-09-03\nTotal amount: 97600000 VND",),
    )
    result = recognize_itinerary_worker(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        NoOcr(),
        True,
    )
    assert result["ok"] and result["materialKind"] == "expense"
    assert result["expense"].original_currency == "VND"
    assert result["expense"].amount is None


@pytest.mark.parametrize(
    "native,scanned,expected_amount,expected_description",
    [
        (
            "Guest Invoice\nDate: 2026-09-03\nTotal amount",
            "Guest Invoice\nTotal amount: 97600000 VND",
            Decimal("97600000"),
            None,
        ),
        (
            "电子发票\n开票日期：2026-09-03\n乘车日期：2026-09-02\n"
            "旅客运输服务\n交通工具类型：出租车\n价税合计：12.30",
            "行程路线：测试起点-测试终点",
            Decimal("12.30"),
            "测试起点-测试终点",
        ),
    ],
)
def test_auto_pdf_recovers_missing_fields_once_and_keeps_native_evidence(
    settings_factory,
    tmp_path,
    monkeypatch,
    native,
    scanned,
    expected_amount,
    expected_description,
):
    settings = settings_factory()
    stored = _digital_pdf(tmp_path / "partial.pdf", ("valid fixture",))
    monkeypatch.setattr(
        "app.ocr.itinerary_worker.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_kwargs: native)]
        ),
    )

    class CountingOcr(FakeOcrEngine):
        calls = 0

        def recognize(self, _path):
            self.calls += 1
            return [OcrLine(line, 1) for line in scanned.splitlines()]

    engine = CountingOcr()
    result = recognize_itinerary_worker(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        engine,
        True,
    )
    assert result["materialKind"] == "expense"
    parsed = result["expense"]
    assert (parsed.original_amount or parsed.amount) == expected_amount
    assert parsed.date is not None
    if expected_description:
        assert parsed.description == expected_description
    assert engine.calls == 1


def test_auto_expense_retains_local_qr_evidence(settings_factory, tmp_path, monkeypatch):
    settings = settings_factory()
    stored = _digital_pdf(tmp_path / "invoice.pdf", ("valid fixture",))
    native = "电子发票\n开票日期：2026-09-03\n餐饮服务\n金额未能读取"
    monkeypatch.setattr(
        "app.ocr.itinerary_worker.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_kwargs: native)]
        ),
    )
    monkeypatch.setattr(
        "app.ocr.itinerary_worker.decode_invoice_qr",
        lambda *_args, **_kwargs: InvoiceQrEvidence(Decimal("123.45"), date(2026, 9, 3)),
        raising=False,
    )
    result = recognize_itinerary_worker(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        FakeOcrEngine({"*": [OcrLine(native, 1)]}),
        True,
    )
    assert result["materialKind"] == "expense"
    assert result["expense"].amount == Decimal("123.45")
    assert "QR_AMOUNT_REQUIRES_REVIEW" in result["expense"].warnings


def test_failed_supplemental_ocr_keeps_native_invoice_candidate(
    settings_factory,
    tmp_path,
    monkeypatch,
):
    settings = settings_factory()
    stored = _digital_pdf(tmp_path / "invoice.pdf", ("valid fixture",))
    native = "电子发票\n开票日期：2026-09-03\n旅客运输服务\n交通工具类型：出租车\n价税合计：12.30"
    monkeypatch.setattr(
        "app.ocr.itinerary_worker.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_kwargs: native)]
        ),
    )

    class FailedOcr(FakeOcrEngine):
        def recognize(self, _path):
            raise OcrRuntimeError("测试补读失败")

    result = recognize_itinerary_worker(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        FailedOcr(),
        True,
    )
    assert result["materialKind"] == "expense"
    assert result["expense"].amount == Decimal("12.30")
    assert result["expense"].date == date(2026, 9, 3)


def test_auto_scan_cap_and_all_page_safety_are_retained(settings_factory, tmp_path):
    settings = settings_factory()
    engine = FakeOcrEngine({"*": [OcrLine(line, 1) for line in ITINERARY.splitlines()]})
    stored = _digital_pdf(tmp_path / "scan.pdf", ("",) * 6)
    result = recognize_itinerary_worker(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        engine,
        True,
    )
    assert result["materialKind"] == "unknown"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_blank_page(width=100000, height=100000)
    with stored.path.open("wb") as handle:
        writer.write(handle)
    result = recognize_itinerary_worker(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        engine,
        True,
    )
    assert result["ok"] is False and result["code"] == "PDF_PAGE_TOO_LARGE"


def _auto_upload(client, csrf, draft_id, *, content=None, name="material.png", revision=1):
    if content is None:
        content = _named_image_bytes(name)
    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files",
        params={"expectedRevision": revision, "role": "ATTACHMENT_ONLY", "autoClassify": True},
        headers={"X-CSRF-Token": csrf},
        files=[("files[]", (name, content, "application/octet-stream"))],
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _recognize(client, csrf, draft_id, file_id, revision):
    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": revision, "tripYear": 2026},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize(
    "text,role,kind,result_kind",
    [
        (INVOICE, "EXPENSE_SOURCE", "other", "expense"),
        (ITINERARY, "ATTACHMENT_ONLY", "itinerary", "itinerary"),
        (PAYMENT, "ATTACHMENT_ONLY", "payment_proof", "payment_proof"),
        ("unknown amount 1000", "ATTACHMENT_ONLY", "other", "unknown"),
    ],
)
def test_auto_upload_persists_purpose_and_only_invoices_return_expense(
    client_factory,
    text,
    role,
    kind,
    result_kind,
):
    client = client_factory(
        auth_mock_enabled=True,
        ocr_enabled=True,
        ocr_engine=FakeOcrEngine({"*": [OcrLine(text, 1)]}),
    )
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _auto_upload(client, csrf, draft_id)
    file_id = uploaded["file"]["id"]
    assert uploaded["file"]["materialClassification"]["status"] == "pending"
    with client.app.state.database_session_factory() as database:
        with pytest.raises(ApiError, match="确认所有待确认材料"):
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=ReimbursementDraftInput.model_validate(_draft_input()),
                require_submission_proofs=True,
            )
    recognized = _recognize(client, csrf, draft_id, file_id, uploaded["revision"])
    file = recognized["file"]
    assert (file["role"], file["attachmentKind"]) == (role, kind)
    assert file["materialClassification"]["kind"] == result_kind
    if result_kind in {"unknown", "payment_proof"}:
        assert file["ocrResult"] is None
    else:
        assert file["ocrResult"]["status"] == "recognized"
    listed = client.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"]
    assert listed["items"][0] == file


def test_failed_auto_classification_keeps_the_actionable_ocr_error(
    client_factory,
    monkeypatch,
):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _auto_upload(client, csrf, draft_id, name="高铁1.png")

    async def timed_out(*_args, **_kwargs):
        raise ApiError("OCR_TIMEOUT", "材料识别超时，请重试或确认用途", 504)

    monkeypatch.setattr(client.app.state.ocr_service, "recognize_material_file", timed_out)
    recognized = _recognize(
        client,
        csrf,
        draft_id,
        uploaded["file"]["id"],
        uploaded["revision"],
    )

    file = recognized["file"]
    assert file["ocrStatus"] == "FAILED"
    assert file["ocrResult"] is None
    assert file["materialClassification"] == {
        "status": "needs_confirmation",
        "kind": "unknown",
        "reason": "材料识别超时，请重试或确认用途",
        "pageCount": 1,
        "error": {
            "code": "OCR_TIMEOUT",
            "message": "材料识别超时，请重试或确认用途",
        },
    }


def test_manual_confirmation_same_other_value_clears_pending_and_prevents_reclassification(
    client_factory,
):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _auto_upload(client, csrf, draft_id)
    file_id = uploaded["file"]["id"]
    patched = client.patch(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2, "role": "ATTACHMENT_ONLY", "attachmentKind": "other"},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["file"]["materialClassification"]["status"] == "confirmed"
    with client.app.state.database_session_factory() as database:
        validate_draft_file_references(
            database,
            draft_id=draft_id,
            draft_input=ReimbursementDraftInput.model_validate(_draft_input()),
            require_submission_proofs=True,
        )
    retry = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 3},
    )
    assert retry.status_code == 409


def test_multi_page_material_cannot_be_manually_promoted_to_expense(client_factory, tmp_path):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    stored = _digital_pdf(tmp_path / "multi.pdf", ("", ""))
    uploaded = _auto_upload(
        client, csrf, draft_id, content=stored.path.read_bytes(), name="multi.pdf"
    )
    file_id = uploaded["file"]["id"]
    assert uploaded["file"]["materialClassification"]["pageCount"] == 2
    response = client.patch(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2, "role": "EXPENSE_SOURCE"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MULTI_PAGE_PDF_UNSUPPORTED"


@pytest.mark.asyncio
async def test_cancelled_auto_classification_remains_blocked_and_can_retry(client_factory):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _auto_upload(client, csrf, draft_id)
    file_id = uploaded["file"]["id"]
    started = asyncio.Event()

    class BlockingOcr:
        async def recognize_material_file(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        recognize_draft_file(
            actor=DraftActor("corp-fixed", "mock-user", "100", "测试部门"),
            draft_id=draft_id,
            file_id=file_id,
            expected_revision=2,
            reference_year=2026,
            settings=client.app.state.settings,
            session_factory=client.app.state.database_session_factory,
            staging=client.app.state.reimbursement_staging,
            ocr_service=BlockingOcr(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert material_classification(file.ocr_result_json)["status"] == "pending"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert file.ocr_status == "FAILED"
        assert material_classification(file.ocr_result_json)["status"] == "needs_confirmation"
        with pytest.raises(ApiError):
            validate_draft_file_references(
                database,
                draft_id=draft_id,
                draft_input=ReimbursementDraftInput.model_validate(_draft_input()),
                require_submission_proofs=True,
            )
        assert json.loads(database.get(ReimbursementDraft, draft_id).input_json)["items"]
    assert not list((client.app.state.settings.temp_dir / ".spool").glob("ocr-*"))


def _classified_file(client, csrf, draft_id, kind):
    uploaded = _auto_upload(client, csrf, draft_id)
    file_id = uploaded["file"]["id"]
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        file.processing_role = "EXPENSE_SOURCE" if kind == "expense" else "ATTACHMENT_ONLY"
        file.attachment_kind = "other" if kind == "expense" else kind
        file.ocr_status = "COMPLETE"
        file.ocr_result_json = json.dumps(
            {
                "_materialClassification": {
                    "status": "classified",
                    "kind": kind,
                    "reason": None,
                    "pageCount": 1,
                }
            }
        )
        draft = database.get(ReimbursementDraft, draft_id)
        value = json.loads(draft.input_json)
        value["items"][0]["description"] = "员工已手工修改的说明"
        if kind == "expense":
            value["items"][0]["sourceFileId"] = file_id
        elif kind == "itinerary":
            value["items"][0]["itineraryFileIds"] = [file_id]
        draft.input_json = json.dumps(value, ensure_ascii=False)
        unchanged_input = draft.input_json
        database.commit()
    return file_id, unchanged_input


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expense", "itinerary"])
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled", "interrupted"])
async def test_classified_retry_keeps_purpose_and_employee_input(client_factory, kind, outcome):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    file_id, unchanged_input = _classified_file(client, csrf, draft_id, kind)
    started = asyncio.Event()
    calls = []

    class DirectedOcr:
        async def recognize_material_file(self, *_args, **_kwargs):
            raise AssertionError("A classified material must not be reclassified")

        async def _recognize(self, requested):
            calls.append(requested)
            if outcome == "failed":
                raise ApiError("OCR_FAILED", "测试识别失败", 422)
            if outcome in {"cancelled", "interrupted"}:
                started.set()
                await asyncio.Event().wait()
            if requested == "expense":
                return ReceiptParserRegistry().parse(
                    [OcrLine(line, 1) for line in INVOICE.splitlines()],
                    ParseContext(reference_year=2026),
                )
            return {"fileId": file_id, "kind": "itinerary", "status": "recognized"}

        async def recognize_file(self, *_args, **_kwargs):
            return await self._recognize("expense")

        async def recognize_itinerary_file(self, *_args, **_kwargs):
            return await self._recognize("itinerary")

    task = asyncio.create_task(
        recognize_draft_file(
            actor=DraftActor("corp-fixed", "mock-user", "100", "测试部门"),
            draft_id=draft_id,
            file_id=file_id,
            expected_revision=2,
            reference_year=2026,
            settings=client.app.state.settings,
            session_factory=client.app.state.database_session_factory,
            staging=client.app.state.reimbursement_staging,
            ocr_service=DirectedOcr(),
        )
    )
    if outcome in {"cancelled", "interrupted"}:
        await asyncio.wait_for(started.wait(), timeout=5)
        if outcome == "interrupted":
            # Another edit forces the cancellation handler's conflict-recovery path.
            with client.app.state.database_session_factory() as database:
                draft = database.get(ReimbursementDraft, draft_id)
                draft.revision += 1
                database.commit()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert calls == [kind]
    with client.app.state.database_session_factory() as database:
        file = database.get(ReimbursementDraftFile, file_id)
        assert material_classification(file.ocr_result_json)["status"] == "classified"
        assert material_classification(file.ocr_result_json)["kind"] == kind
        assert file.processing_role == (
            "EXPENSE_SOURCE" if kind == "expense" else "ATTACHMENT_ONLY"
        )
        payload = json.loads(file.ocr_result_json)
        assert payload["status"] == ("recognized" if outcome == "success" else "failed")
        if kind == "itinerary":
            assert payload["kind"] == "itinerary"
        assert database.get(ReimbursementDraft, draft_id).input_json == unchanged_input


def test_classified_payment_proof_does_not_offer_ocr(client_factory):
    client = client_factory(auth_mock_enabled=True)
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    file_id, _ = _classified_file(client, csrf, draft_id, "payment_proof")
    response = client.post(
        f"/api/reimbursements/drafts/{draft_id}/files/{file_id}/ocr",
        headers={"X-CSRF-Token": csrf},
        json={"expectedRevision": 2},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REIMBURSEMENT_FILE_OCR_NOT_ALLOWED"


def test_unknown_retry_may_classify_as_expense(client_factory, monkeypatch):
    from app.services.ocr_service import parsed_expense_payload

    client = client_factory(
        auth_mock_enabled=True,
        ocr_enabled=True,
        ocr_engine=FakeOcrEngine({"*": [OcrLine("unreadable", 1)]}),
    )
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    uploaded = _auto_upload(client, csrf, draft_id)
    file_id = uploaded["file"]["id"]
    first = _recognize(client, csrf, draft_id, file_id, 2)
    assert first["file"]["materialClassification"]["status"] == "needs_confirmation"

    async def retry_classification(stored, **_kwargs):
        parsed = ReceiptParserRegistry().parse(
            [OcrLine(line, 1) for line in INVOICE.splitlines()],
            ParseContext(reference_year=2026),
        )
        payload = parsed_expense_payload(stored.temp_id, parsed)
        payload["_materialClassification"] = {
            "status": "classified",
            "kind": "expense",
            "reason": None,
            "pageCount": 1,
        }
        return payload

    monkeypatch.setattr(
        client.app.state.ocr_service, "recognize_material_file", retry_classification
    )
    second = _recognize(client, csrf, draft_id, file_id, first["revision"])
    assert second["file"]["role"] == "EXPENSE_SOURCE"
    assert second["file"]["ocrResult"]["status"] == "recognized"
    assert second["file"]["materialClassification"]["status"] == "classified"
