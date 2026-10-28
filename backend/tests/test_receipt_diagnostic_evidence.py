from types import SimpleNamespace

from test_itinerary_ocr import _digital_pdf

from app.ocr.diagnostics import TEXT_LIMIT, RecognitionDiagnostics
from app.ocr.engine import FakeOcrEngine
from app.ocr.itinerary_worker import recognize_itinerary_worker
from app.ocr.types import OcrLine


def test_pdf_supplement_keeps_original_extractions_separate_from_derived_fields(
    settings_factory, tmp_path, monkeypatch
):
    native = (
        "电子发票\n开票日期：2026-09-03\n乘车日期：2026-09-02\n"
        "旅客运输服务\n交通工具类型：出租车\n价税合计：12.30\n销售方\n名称：合成商户"
    )
    scanned = "行程路线：测试起点-测试终点"
    stored = _digital_pdf(tmp_path / "synthetic.pdf", ("synthetic fixture",))
    monkeypatch.setattr(
        "app.ocr.itinerary_worker.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_kwargs: native)]
        ),
    )
    settings = settings_factory()
    args = (
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        FakeOcrEngine({"*": [OcrLine(scanned, 1)]}),
        True,
    )
    normal = recognize_itinerary_worker(*args)
    diagnosed = recognize_itinerary_worker(*args, diagnostics=True)
    assert "diagnostic" not in normal
    assert normal["expense"] == diagnosed["expense"]
    trace = diagnosed["diagnostic"]
    assert trace["sources"] == ["paddle", "pdf_text"]
    assert trace["extractions"] == [
        {
            "page": 1,
            "source": "pdf_text",
            "text": native,
            "truncated": False,
            "usedForParsing": True,
        },
        {
            "page": 1,
            "source": "paddle",
            "text": scanned,
            "truncated": False,
            "usedForParsing": True,
        },
    ]
    derived = "销售方名称：合成商户"
    assert derived not in trace["recognizedText"]
    assert derived in [field["text"] for field in trace["derivedFields"]]
    assert "12.30" not in trace["extractions"][1]["text"]


def test_capture_bounds_each_display_and_tracks_unused_native_text():
    capture = RecognitionDiagnostics()
    capture.record_extraction(1, "pdf_text", "n" * (TEXT_LIMIT - 1), used_for_parsing=False)
    capture.record_extraction(1, "paddle", "ocr", used_for_parsing=True)
    capture.record_derived(1, ["a"], ["inserted", "a", "d" * (TEXT_LIMIT + 1)])
    capture.record_derived(1, ["a"], ["inserted", "a"])
    trace = capture.as_dict(complete=True)
    assert len(trace["recognizedText"]) <= TEXT_LIMIT
    assert trace["recognizedTextTruncated"]
    assert sum(len(item["text"]) for item in trace["extractions"]) <= TEXT_LIMIT
    assert trace["extractions"][0]["usedForParsing"] is False
    assert trace["derivedFieldsTruncated"]
    assert len(trace["derivedFields"]) == 2
    assert sum(len(item["text"]) for item in trace["derivedFields"]) <= TEXT_LIMIT
