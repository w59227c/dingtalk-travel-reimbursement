from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import mock_login
from test_itinerary_ocr import _digital_pdf
from test_material_classification import _auto_upload, _pages, _recognize
from test_reimbursement_files import _image_bytes, _insert_draft

from app.models.reimbursement import ReimbursementDraft
from app.ocr.engine import FakeOcrEngine
from app.ocr.itinerary_worker import recognize_itinerary_worker
from app.ocr.materials import classify_material
from app.ocr.payment_proofs import payment_proof_details
from app.ocr.types import OcrLine
from app.schemas.reimbursements import ReimbursementDraftInput

CARD_PAYMENT = (
    "付款凭证 电子回单\n转账金额\n￥50.00\n币种\n人民币\n收款账户\n1234 **** 6116\n"
    "付款账户\n1234 **** 5956\n附言\n制卡费\n业务日期\n2026-07-09 21:34:01"
)
EXPECTED = {
    "amount": "50.00",
    "date": "2026-07-09",
    "description": "制卡费",
    "categoryId": None,
}
BANK_PURCHASE = (
    "交易明细详情\n￥68.50\n卡号\n1234 *** 5678\n交易时间\n2026/09/03 12:30\n"
    "交易国家/地区\n中国\n交易描述\n测试支付-消费-测试商户\n结算金额\n￥68.50\n"
    "记账时间\n2026/09/04"
)


def test_posted_bank_purchase_is_payment_proof_with_optional_manual_entry_hints(
    settings_factory, tmp_path
):
    class CountingOcr(FakeOcrEngine):
        calls = 0

        def recognize(self, _path):
            self.calls += 1
            return [OcrLine(text, 0.95) for text in BANK_PURCHASE.splitlines()]

    path = tmp_path / "unrelated-name.jpg"
    path.write_bytes(_image_bytes())
    settings = settings_factory()
    engine = CountingOcr()
    result = recognize_itinerary_worker(
        str(path),
        "jpg",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        engine,
        True,
    )
    assert result["ok"] and result["materialKind"] == "payment_proof"
    assert result["paymentDetails"] == {
        "amount": "68.50",
        "date": "2026-09-03",
        "description": "测试支付-消费-测试商户",
        "categoryId": None,
    }
    assert "expense" not in result
    assert engine.calls == 1


@pytest.mark.parametrize(
    "text",
    [
        BANK_PURCHASE.replace("记账时间\n2026/09/04", "待入账"),
        BANK_PURCHASE.replace("记账时间\n2026/09/04", "记账时间\n--"),
        BANK_PURCHASE.replace("消费", "退款"),
        BANK_PURCHASE.replace("消费", "收入"),
        BANK_PURCHASE.replace("￥68.50", "-68.50元"),
        BANK_PURCHASE.replace("卡号\n1234 *** 5678\n", ""),
        BANK_PURCHASE.replace("交易时间\n2026/09/03 12:30\n", ""),
        BANK_PURCHASE.replace("结算金额\n￥68.50\n", ""),
        BANK_PURCHASE.replace("交易明细详情\n", ""),
        BANK_PURCHASE + "\n结算金额\n￥80.00",
        BANK_PURCHASE + "\n交易失败",
        BANK_PURCHASE + "\n已撤销",
        BANK_PURCHASE + "\n预授权",
        BANK_PURCHASE.replace("2026/09/04", "2026/09/02"),
        BANK_PURCHASE.replace("￥68.50", "￥0.00"),
    ],
)
def test_bank_transaction_requires_posted_purchase_evidence(text):
    assert classify_material(_pages(text), page_count=1)[0] == "unknown"
    assert payment_proof_details(_pages(text), reference_year=2026)["amount"] is None


@pytest.mark.parametrize("state", ["待入账", "退款成功", "交易失败", "预授权", "贷方", "已撤销"])
def test_success_banner_does_not_override_conflicting_bank_state(state):
    text = BANK_PURCHASE + "\n交易成功\n" + state
    assert classify_material(_pages(text), page_count=1)[0] == "unknown"
    assert payment_proof_details(_pages(text), reference_year=2026)["amount"] is None


def test_unsupported_bank_document_cannot_bypass_state_guard_by_adding_a_page():
    pages = _pages(BANK_PURCHASE + "\n交易成功\n待入账") + _pages("其他说明")
    assert classify_material(pages, page_count=2)[0] == "unknown"


def test_transfer_hints_are_labeled_and_do_not_use_account_numbers():
    assert payment_proof_details(_pages(CARD_PAYMENT), reference_year=2026) == EXPECTED


@pytest.mark.parametrize(
    "text",
    [
        "付款成功\n收款账户 999999\n交易单号 50000000\n余额 ￥10000.00",
        "付款成功\n支付金额 -50.00元\n收款方 测试商户",
        "付款成功\n支付金额 0.00元\n收款方 测试商户",
        "付款成功\n支付金额 50.00 USD\n收款方 测试商户",
        "付款成功\n支付金额 50.00\n币种 VND\n收款方 测试商户",
        "付款成功\n支付金额 50.00元\n支付金额 80.00元\n收款方 测试商户",
        "付款成功\n支付金额\n交易单号\n50000000\n收款方 测试商户",
        "付款成功\n支付金额 50.00\n收款方 测试商户",
        "付款失败\n支付金额 ￥50.00\n收款方 测试商户",
        "退款成功\n支付金额 ￥50.00\n收款方 测试商户",
        "付款成功\n支付金额 ￥50.00\n支付金额 ￥80.00\n收款方 测试商户",
        "付款成功\n支付金额 ￥1000\n币种：泰铢\n收款方 商户",
        "付款成功\n支付金额 ￥100.00元\n退款成功\n退款金额 ￥100.00元",
    ],
)
def test_uncertain_amounts_stay_empty_for_manual_confirmation(text):
    result = payment_proof_details(_pages(text), reference_year=2026)
    assert result["amount"] is None
    assert result["categoryId"] is None


def test_conflicting_dates_descriptions_or_multiple_pages_are_not_prefilled():
    text = CARD_PAYMENT + "\n业务日期 2026-07-10\n备注 其他费用"
    result = payment_proof_details(_pages(text), reference_year=2026)
    assert result["date"] is None and result["description"] is None
    assert payment_proof_details(_pages(text) * 2, reference_year=2026) == {
        "amount": None,
        "date": None,
        "description": None,
        "categoryId": None,
    }


def test_electronic_payment_receipt_uses_native_text_and_never_returns_expense(
    settings_factory, tmp_path, monkeypatch
):
    path = tmp_path / "payment.pdf"
    _digital_pdf(path, ("valid fixture",))
    monkeypatch.setattr(
        "app.ocr.itinerary_worker.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_kwargs: CARD_PAYMENT)]
        ),
    )

    class NeverOcr(FakeOcrEngine):
        def recognize(self, _path):
            raise AssertionError("An electronic payment receipt must not invoke image OCR")

    settings = settings_factory()
    result = recognize_itinerary_worker(
        str(path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        NeverOcr(),
        True,
    )
    assert result["ok"]
    assert result["materialKind"] == "payment_proof"
    assert result["paymentDetails"] == EXPECTED
    assert "expense" not in result


def test_photographed_payment_receipt_uses_only_one_ocr_pass(settings_factory, tmp_path):
    class CountingOcr(FakeOcrEngine):
        calls = 0

        def recognize(self, _path):
            self.calls += 1
            return [OcrLine(text, 0.95) for text in CARD_PAYMENT.splitlines()]

    path = tmp_path / "payment.jpg"
    path.write_bytes(_image_bytes())
    settings = settings_factory()
    engine = CountingOcr()
    result = recognize_itinerary_worker(
        str(path),
        "jpg",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        engine,
        True,
    )
    assert result["paymentDetails"] == EXPECTED
    assert result["materialKind"] == "payment_proof"
    assert "expense" not in result
    assert engine.calls == 1


def test_payment_details_survive_reload_without_creating_or_hydrating_expense(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        ocr_enabled=True,
        ocr_engine=FakeOcrEngine({"*": [OcrLine(text, 1) for text in CARD_PAYMENT.splitlines()]}),
    )
    csrf = str(mock_login(client)["csrfToken"])
    draft_id = _insert_draft(client)
    with client.app.state.database_session_factory() as database:
        before = database.get(ReimbursementDraft, draft_id).input_json
    uploaded = _auto_upload(client, csrf, draft_id)
    file_id = uploaded["file"]["id"]
    recognized = _recognize(client, csrf, draft_id, file_id, uploaded["revision"])
    file = recognized["file"]
    assert file["role"] == "ATTACHMENT_ONLY"
    assert file["attachmentKind"] == "payment_proof"
    assert file["ocrResult"] is None
    assert file["paymentDetails"] == EXPECTED
    listed = client.get(f"/api/reimbursements/drafts/{draft_id}/files").json()["data"]
    assert listed["items"][0] == file
    with client.app.state.database_session_factory() as database:
        after = database.get(ReimbursementDraft, draft_id).input_json
        assert ReimbursementDraftInput.model_validate_json(
            after
        ) == ReimbursementDraftInput.model_validate_json(before)
