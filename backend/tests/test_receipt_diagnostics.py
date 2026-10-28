from __future__ import annotations

from io import BytesIO

import pytest
from conftest import mock_login
from pypdf import PdfWriter
from sqlalchemy import select
from test_reimbursement_files import _image_bytes

from app.core.errors import ApiError
from app.domain.categories import ExpenseCategory
from app.models.receipt_keyword import ReceiptKeywordMapping
from app.models.reimbursement import ReimbursementDraft, ReimbursementDraftFile
from app.models.session import UserSession
from app.ocr.engine import FakeOcrEngine
from app.ocr.parsers import ReceiptParserRegistry
from app.ocr.types import OcrLine, ParseContext, ReceiptKeywordRule
from app.services.process_jobs import ProcessJobBusy

PATH = "/api/admin/receipts/diagnose"
TEXT = "电子发票\n开票日期：2026-09-03\n价税合计：12.30\n合成文具"


def setup(client_factory):
    client = client_factory(
        auth_mock_enabled=True,
        auth_mock_user_id="diagnostic-admin",
        admin_user_ids="diagnostic-admin",
        ocr_enabled=True,
        ocr_engine=FakeOcrEngine({"*": [OcrLine(line, 1) for line in TEXT.splitlines()]}),
    )
    return client, str(mock_login(client)["csrfToken"])


def post(client, token, **kwargs):
    return client.post(
        PATH + "?tripYear=2026",
        headers={"X-CSRF-Token": token},
        files={"files[]": ("synthetic.png", _image_bytes(), "image/png")},
        **kwargs,
    )


def no_uploads(client):
    assert not list(client.app.state.settings.temp_dir.rglob("*.png"))
    assert not list(client.app.state.settings.temp_dir.rglob("*.pdf"))


def test_requires_session_live_admin_and_csrf(client_factory):
    client, token = setup(client_factory)
    assert client.post(PATH).status_code == 403
    client.app.state.settings.admin_user_ids = ""
    assert post(client, token).status_code == 403
    client.cookies.clear()
    assert client.post(PATH).status_code == 401
    no_uploads(client)


def test_uses_current_rules_and_shared_worker_without_persisting_raw_text(client_factory, capsys):
    client, token = setup(client_factory)
    with client.app.state.database_session_factory() as database:
        database.add(
            ReceiptKeywordMapping(
                keyword="合成文具", normalized_keyword="合成文具", category_id="office"
            )
        )
        database.commit()
    response = post(client, token)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    report = response.json()["data"]
    assert report["requestId"] and report["checkedAt"]
    recognized = report["recognition"]
    assert "fileId" not in recognized
    assert recognized["amount"] == "12.30"
    assert recognized["categoryId"] == "office"
    trace = recognized["diagnostic"]
    assert trace["recognizedText"] == TEXT
    assert trace["complete"] is True
    assert trace["parser"] == "GenericInvoiceParser"
    assert trace["policy"] == "keyword_match"
    assert trace["matchedRules"] == [
        {
            "keyword": "合成文具",
            "category": "office",
            "categoryName": "办公费",
            "transportType": None,
        }
    ]
    with client.app.state.database_session_factory() as database:
        assert database.scalar(select(ReimbursementDraft)) is None
        assert database.scalar(select(ReimbursementDraftFile)) is None
    no_uploads(client)
    output = capsys.readouterr()
    assert "合成文具" not in output.out + output.err


@pytest.mark.parametrize("outcome", ["busy", "failure", "logout", "revoked"])
def test_failure_and_lost_authorization_discard_text_and_clean_upload(
    client_factory, monkeypatch, outcome
):
    client, token = setup(client_factory)
    original = client.app.state.ocr_service.recognize_material_file

    async def changed(*args, **kwargs):
        if outcome == "busy":
            raise ApiError("OCR_BUSY", "繁忙", 429)
        if outcome == "failure":
            raise ApiError("OCR_TIMEOUT", "超时", 504)
        result = await original(*args, **kwargs)
        if outcome == "revoked":
            client.app.state.settings.admin_user_ids = ""
        else:
            with client.app.state.database_session_factory() as database:
                database.delete(database.scalar(select(UserSession)))
                database.commit()
        return result

    monkeypatch.setattr(client.app.state.ocr_service, "recognize_material_file", changed)
    response = post(client, token)
    assert (
        response.status_code
        == {"busy": 429, "failure": 504, "logout": 401, "revoked": 403}[outcome]
    )
    assert "diagnostic" not in response.json().get("data", {})
    assert TEXT not in response.text
    no_uploads(client)


def test_diagnostic_admission_uses_the_existing_global_ocr_slot(client_factory, monkeypatch):
    client, token = setup(client_factory)
    calls = []

    async def full_queue(function, *args, **kwargs):
        calls.append((function.__name__, args[-1]))
        raise ProcessJobBusy()

    monkeypatch.setattr(client.app.state.process_runner, "run", full_queue)
    response = post(client, token)
    assert response.status_code == 429
    assert calls == [("recognize_itinerary_worker", True)]
    no_uploads(client)


@pytest.mark.parametrize("kind", ["mismatch", "multiple", "multipage", "year"])
def test_rejects_invalid_diagnostic_uploads_before_ocr(client_factory, monkeypatch, kind):
    client, token = setup(client_factory)
    called = False

    async def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid upload must not reach OCR")

    monkeypatch.setattr(client.app.state.ocr_service, "recognize_material_file", forbidden)
    files = [("files[]", ("test.png", _image_bytes(), "image/png"))]
    if kind == "mismatch":
        files = [("files[]", ("test.png", b"not an image", "image/png"))]
    elif kind == "multiple":
        files *= 2
    elif kind == "multipage":
        writer = PdfWriter()
        for _ in range(2):
            writer.add_blank_page(width=100, height=100)
        buffer = BytesIO()
        writer.write(buffer)
        files = [("files[]", ("test.pdf", buffer.getvalue(), "application/pdf"))]
    response = client.post(
        PATH + ("?tripYear=1800" if kind == "year" else ""),
        headers={"X-CSRF-Token": token},
        files=files,
    )
    assert response.status_code in {400, 413, 422}
    assert not called
    no_uploads(client)


@pytest.mark.parametrize(
    "content,expected_policy",
    [
        ("电子发票\n开票日期2026-09-03\n价税合计12.30\n合成文具", "keyword_match"),
        ("电子发票\n开票日期2026-09-03\n价税合计12.30\n合成文具 合成酒店", "keyword_conflict"),
        ("无结构\n合成文具", "keyword_fallback"),
        ("电子发票\n开票日期2026-09-03\n价税合计12.30\n合成未知", "no_keyword_match"),
        ("铁路电子客票\n车次 G123\n票价510元\n2026-09-03\n合成文具", "specialized_parser"),
    ],
)
def test_trace_reports_actual_decisions_without_changing_parser_result(content, expected_policy):
    registry = ReceiptParserRegistry(
        keyword_rules=(
            ReceiptKeywordRule("合成文具", ExpenseCategory.OFFICE),
            ReceiptKeywordRule("合成酒店", ExpenseCategory.LODGING),
        )
    )
    lines = [OcrLine(line, 1) for line in content.splitlines()]
    trace = {}
    parsed = registry.parse(lines, ParseContext(2026), trace=trace)
    assert parsed == registry.parse(lines, ParseContext(2026))
    assert trace["policy"] == expected_policy
    assert trace["finalCategory"] == parsed.category.value


def test_regular_recognition_never_returns_diagnostic_text(settings_factory, tmp_path):
    from app.ocr.itinerary_worker import recognize_itinerary_worker

    settings = settings_factory()
    image = tmp_path / "receipt.png"
    image.write_bytes(_image_bytes())
    args = (
        str(image),
        "png",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        FakeOcrEngine({"*": [OcrLine(line, 1) for line in TEXT.splitlines()]}),
        True,
    )
    normal = recognize_itinerary_worker(*args)
    diagnostic = recognize_itinerary_worker(*args, diagnostics=True)
    assert "diagnostic" not in normal
    assert normal["expense"] == diagnostic["expense"]
    assert diagnostic["diagnostic"]["recognizedText"] == TEXT


def test_keyword_trace_explains_why_a_buyer_keyword_is_not_matched():
    content = (
        "电子发票\n购买方名称：合成文具公司\n销售方名称：合成商户\n"
        "项目名称：普通服务\n价税合计12.30"
    )
    registry = ReceiptParserRegistry(
        keyword_rules=(ReceiptKeywordRule("合成文具", ExpenseCategory.OFFICE),)
    )
    trace = {}
    parsed = registry.parse(
        [OcrLine(line, 1) for line in content.splitlines()], ParseContext(2026), trace=trace
    )
    assert parsed.category is ExpenseCategory.OTHER
    assert trace["matchedRules"] == []
    assert "合成文具" not in trace["classificationText"]
    assert trace["policy"] == "no_keyword_match"


def test_concurrent_logout_maps_retired_diagnostic_session_to_401(client_factory, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    client, token = setup(client_factory)
    coordinator = client.app.state.file_coordinator
    with client.app.state.database_session_factory() as database:
        session_id = database.scalar(select(UserSession)).session_id_hash
    diagnosis_queued = threading.Event()
    logout_queued = threading.Event()
    reserve = coordinator._reserve_state

    def observed_reserve(identifier, *, retire=False):
        state = reserve(identifier, retire=retire)
        (logout_queued if retire else diagnosis_queued).set()
        return state

    with ThreadPoolExecutor(max_workers=2) as executor:
        with coordinator.session_lease(session_id):
            monkeypatch.setattr(coordinator, "_reserve_state", observed_reserve)
            diagnostic = executor.submit(post, client, token)
            assert diagnosis_queued.wait(5)
            logout = executor.submit(
                client.post, "/api/auth/logout", headers={"X-CSRF-Token": token}
            )
            assert logout_queued.wait(5)
        response = diagnostic.result(10)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
        assert logout.result(10).status_code == 200
    no_uploads(client)


@pytest.mark.parametrize(
    "declared_length,code", [("999999999", "REQUEST_TOO_LARGE"), ("invalid", "MALFORMED_REQUEST")]
)
def test_diagnostic_upload_uses_shared_request_length_boundary(
    client_factory, declared_length, code
):
    client, token = setup(client_factory)
    response = client.post(
        PATH,
        headers={"X-CSRF-Token": token, "Content-Length": declared_length},
        content=b"synthetic",
    )
    assert response.status_code == (413 if code == "REQUEST_TOO_LARGE" else 400)
    assert response.json()["error"]["code"] == code
    no_uploads(client)
