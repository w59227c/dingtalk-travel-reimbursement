from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ocr.engine import FakeOcrEngine
from app.ocr.itinerary import ItineraryPage, parse_itinerary_pages
from app.ocr.types import OcrLine
from app.services.ocr_service import OcrService
from app.services.process_jobs import KillableProcessRunner
from app.services.temp_files import StoredFile


def test_single_itinerary_keeps_total_and_trip_as_separate_evidence():
    text = (
        "出行行程单\n"
        "行程金额合计：12.30元\n"
        "乘车日期：2026-09-03\n"
        "起点：测试起点\n终点：测试终点\n"
        "订单号：ORDER-00123"
    )
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert parsed.complete
    assert parsed.summary.amount == Decimal("12.30")
    assert parsed.summary.start_date == parsed.summary.end_date == date(2026, 9, 3)
    assert len(parsed.trips) == 1
    assert parsed.trips[0].amount == Decimal("12.30")
    assert parsed.trips[0].order_numbers == ("ORDER-00123",)
    assert parsed.trips[0].origin == "测试起点"
    assert parsed.trips[0].destination == "测试终点"


def test_multi_page_itinerary_preserves_each_trip_and_checks_document_total():
    texts = (
        "行程单 人民币\n序号  日期  起点  终点  金额\n1  2026-09-03  测试甲  测试乙  12.30",
        "行程单 人民币\n序号  日期  起点  终点  金额\n"
        "2  2026-09-04  测试丙  测试丁  14.50\n总金额：26.80元",
    )
    pages = [
        ItineraryPage(
            index, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text"
        )
        for index, text in enumerate(texts, 1)
    ]
    parsed = parse_itinerary_pages(pages, page_count=2, reference_year=2026)
    assert parsed.complete
    assert parsed.summary.amount == Decimal("26.80")
    assert [(trip.page, trip.date, trip.amount) for trip in parsed.trips] == [
        (1, date(2026, 9, 3), Decimal("12.30")),
        (2, date(2026, 9, 4), Decimal("14.50")),
    ]
    assert parsed.trips[1].origin == "测试丙"
    assert parsed.summary.end_date == date(2026, 9, 4)


def test_layout_columns_survive_merged_leading_cells_and_wrapped_origin():
    text = (
        "行程单 人民币 合计 9.20元\n"
        "序号  服务      车型            上车时间              城市        "
        "起点          终点       金额\n"
        " 1 网约服务   快车          2026-09-03 10:20    测试市         "
        "测试起点        测试终点     9.20\n"
        "                                                       东门"
    )
    result = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert result.complete
    assert len(result.trips) == 1
    assert result.trips[0].date == date(2026, 9, 3)
    assert result.trips[0].amount == Decimal("9.20")


def _digital_pdf(path: Path, texts: tuple[str, ...]) -> StoredFile:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        commands = ["BT /F1 12 Tf 40 800 Td"]
        for line in text.splitlines():
            commands.append(
                "("
                + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                + ") Tj 0 -20 Td"
            )
        commands.append("ET")
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)
    return StoredFile("test-file", path, "pdf", "application/pdf", path.stat().st_size, path.name)


@pytest.mark.asyncio
async def test_service_reads_all_digital_pdf_pages_without_loading_models(
    settings_factory, tmp_path
):
    stored = _digital_pdf(
        tmp_path / "itinerary.pdf",
        (
            "Itinerary CNY\nTrip date: 2026-09-03\nOrder No: TEST-0001",
            "Itinerary CNY\nTrip date: 2026-09-04\nTotal amount: 26.80",
        ),
    )
    runner = KillableProcessRunner()
    try:
        service = OcrService(settings_factory(ocr_enabled=True), None, runner)
        result = await service.recognize_itinerary_file(stored, reference_year=2026)
    finally:
        await runner.close()
    assert result["kind"] == "itinerary"
    assert result["source"] == "pdf_text"
    assert result["pageCount"] == result["processedPageCount"] == 2
    assert result["complete"]
    assert result["summary"]["amount"] == "26.80"
    assert result["summary"]["startDate"] == "2026-09-03"
    assert result["summary"]["endDate"] == "2026-09-04"
    assert result["summary"]["orderNumbers"] == ["TEST-0001"]


@pytest.mark.asyncio
async def test_scanned_pdf_fallback_is_bounded_to_five_pages(settings_factory, tmp_path):
    stored = _digital_pdf(tmp_path / "scanned.pdf", ("",) * 6)
    engine = FakeOcrEngine(
        {
            "*": [
                OcrLine(line, 0.99)
                for line in (
                    "Itinerary CNY",
                    "Trip date: 2026-09-03",
                    "Total amount: 12.30",
                    "Origin: Test A",
                    "Destination: Test B",
                )
            ]
        }
    )
    runner = KillableProcessRunner()
    try:
        result = await OcrService(
            settings_factory(ocr_enabled=True), engine, runner
        ).recognize_itinerary_file(stored, reference_year=2026)
    finally:
        await runner.close()
    assert result["pageCount"] == 6
    assert result["processedPageCount"] == 5
    assert result["source"] == "paddle"
    assert not result["complete"]
    assert "ITINERARY_OCR_PAGE_LIMIT" in result["warnings"]
    assert "ITINERARY_PARTIAL" in result["warnings"]


def test_mixed_pdf_reuses_one_engine_and_keeps_native_pages_after_scan_budget(
    settings_factory, tmp_path, run_itinerary_in_process
):
    class CountingEngine(FakeOcrEngine):
        calls = 0

        def recognize(self, path):
            self.calls += 1
            return [OcrLine("Itinerary CNY 2026-09-03", 1)]

    settings = settings_factory()
    stored = _digital_pdf(
        tmp_path / "mixed.pdf", ("",) * 6 + ("Itinerary CNY Total amount: 12.30",)
    )
    engine = CountingEngine()
    result = run_itinerary_in_process(
        str(stored.path), "pdf", {}, settings.pdf_limits, settings.ocr_worker_limits, 2026, engine
    )
    parsed = result["parsed"]
    assert engine.calls == 5
    assert parsed.page_count == 7
    assert parsed.processed_page_count == 6
    assert parsed.source == "mixed"
    assert parsed.summary.amount == Decimal("12.30")
    assert not parsed.complete


def test_every_page_is_safety_checked_before_any_fallback(
    settings_factory, tmp_path, run_itinerary_in_process
):
    settings = settings_factory()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_blank_page(width=100000, height=100000)
    path = tmp_path / "unsafe-second-page.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    result = run_itinerary_in_process(
        str(path), "pdf", {}, settings.pdf_limits, settings.ocr_worker_limits, 2026, FakeOcrEngine()
    )
    assert result["ok"] is False
    assert result["code"] == "PDF_PAGE_TOO_LARGE"


@pytest.mark.parametrize(
    "text,warning",
    [
        (
            "行程单 人民币\n乘车日期：2026-09-03\n合计：12.30元\n总金额：14.50元",
            "ITINERARY_TOTAL_CONFLICT",
        ),
        ("行程单\n乘车日期：2026-09-03\n合计：12.30", "ITINERARY_CURRENCY_UNKNOWN"),
        ("行程单 人民币\n申请时间：2026-09-03\n合计：12.30元", "MISSING_DATE"),
        ("行程单 人民币\n乘车日期：2026-09-03\n合计1笔", "MISSING_AMOUNT"),
        ("行程单 人民币 USD\n乘车日期：2026-09-03\n合计12.30元", "ITINERARY_CURRENCY_CONFLICT"),
        ("行程单 人民币\n行程日期：2026-09-05至2026-09-03\n合计12.30元", "ITINERARY_DATE_CONFLICT"),
    ],
)
def test_ambiguous_evidence_never_becomes_complete(text, warning):
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert not parsed.complete
    assert warning in parsed.warnings


def test_foreign_itinerary_keeps_currency_and_does_not_assume_yen_is_cny():
    text = "Itinerary JPY\nTrip date: 2026-09-03\nTotal amount: ¥1230"
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert parsed.complete
    assert parsed.summary.currency == "JPY"
    assert parsed.summary.amount == Decimal("1230.00")


def test_unknown_year_is_not_filled_with_current_year():
    text = "行程单 人民币\n乘车日期：09-03\n合计12.30元"
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert not parsed.complete
    assert parsed.summary.start_date is None


def _parse_layout(text: str, layout: str | None = None):
    return parse_itinerary_pages(
        [
            ItineraryPage(
                1, tuple(OcrLine(line, 1) for line in text.splitlines()), layout or text, "pdf_text"
            )
        ],
        page_count=1,
        reference_year=2026,
    )


@pytest.mark.parametrize(
    "amount_header", ["可开票金额", "可开票金额[元]", "实付金额", "金额（元）"]
)
def test_itinerary_amount_column_aliases_keep_the_actual_route(amount_header):
    text = (
        "打车行程单 人民币\n共1笔行程，合计18.39元\n"
        f"序号  用车时间  起点  终点  {amount_header}\n"
        "1  2026-07-02 22:07:47  测试起点  测试终点  ￥18.39"
    )
    result = _parse_layout(text)
    assert result.complete
    assert result.trips[0].amount == Decimal("18.39")
    assert (result.trips[0].origin, result.trips[0].destination) == ("测试起点", "测试终点")


def test_month_day_uses_explicit_trip_range_not_application_date_or_current_year():
    text = (
        "打车行程单 人民币\n申请日期：2026-07-08 · 行程起止日期：2025-07-03至2025-07-03\n"
        "合计7.10元\n"
        "序号  上车时间              城市      起点                终点         金额[元]  备注\n"
        "1     07-03 08:58 周五      测试市  起点酒店完整名称        测试终点      7.10"
    )
    result = _parse_layout(text)
    assert result.complete
    assert result.trips[0].date == date(2025, 7, 3)
    assert result.trips[0].amount == Decimal("7.10")
    assert result.trips[0].origin == "起点酒店完整名称"
    assert result.trips[0].destination == "测试终点"


@pytest.mark.parametrize("range_text", ["", "行程起止日期：2025-12-31至2026-01-01"])
def test_partial_trip_date_stays_incomplete_without_an_unambiguous_document_year(range_text):
    text = (
        f"打车行程单 人民币\n{range_text}\n合计7.10元\n"
        "序号  上车时间  起点  终点  金额\n"
        "1  07-03 08:58  测试起点  测试终点  7.10"
    )
    assert not _parse_layout(text).complete
    assert not _parse_layout(text).trips


def test_combined_route_header_and_wrapped_text_above_row_preserve_both_endpoints():
    def row(*entries):
        line = ""
        for position, value in entries:
            line += " " * max(2 if line else 0, position - len(line)) + value
        return line

    text = "\n".join(
        [
            "享道出行一行程单",
            "行程时间：2026-07-01至2026-07-01",
            "行程总计：共1笔行程，总计可开票金额16.61元",
            row((323, "可开票金额")),
            row(
                (63, "序号"),
                (87, "订单类型"),
                (134, "上车时间"),
                (178, "所在城市"),
                (250, "起点/终点"),
                (332, "(元)"),
            ),
            row((206, "测试机场酒店/测试技术")),
            row(
                (68, "1"),
                (92, "舒享"),
                (123, "2026-07-0108:34:53"),
                (180, "测试市"),
                (242, "有限公司-东大门"),
                (333, "16.61"),
            ),
        ]
    )
    result = _parse_layout(text)
    assert result.complete
    assert result.summary.amount == Decimal("16.61")
    assert result.trips[0].date == date(2026, 7, 1)
    assert result.trips[0].origin == "测试机场酒店"
    assert result.trips[0].destination == "测试技术有限公司-东大门"


def test_column_headers_are_never_used_as_single_trip_addresses():
    result = _parse_layout(
        "打车行程单 人民币\n行程日期：2026-07-02\n合计19.98元\n"
        "序号\n上车时间\n起点\n终点\n里程[公里]可开票金额[元]"
    )
    assert not result.complete
    assert not result.trips
    assert "ITINERARY_ROWS_INCOMPLETE" in result.warnings


def test_table_total_without_readable_rows_is_not_complete():
    result = _parse_layout(
        "打车行程单 人民币\n行程日期：2026-07-02\n合计19.98元\n"
        "序号  上车时间  起点  终点  金额\n无法读取的表格"
    )
    assert result.summary.amount == Decimal("19.98")
    assert not result.complete


def test_joined_destination_and_mileage_stays_incomplete_instead_of_becoming_an_address():
    result = _parse_layout(
        "打车行程单 人民币\n合计19.98元\n"
        "序号  上车时间  起点  终点  里程[公里]  可开票金额[元]\n"
        "1  2026-07-02  测试起点  测试终点7.4  7.4  19.98"
    )
    assert not result.complete
    assert not result.trips


def test_yen_symbol_on_baidu_itinerary_does_not_force_currency_or_create_an_expense():
    result = _parse_layout(
        "百度地图打车行程单\n共1笔行程\n"
        "序号  用车时间  服务方  起点  终点  实付金额\n"
        "1  2026-06-30  哈啰出行  测试起点  测试终点  ￥14.97"
    )
    assert result.summary.amount == Decimal("14.97")
    assert result.summary.currency is None
    assert not result.complete
    assert result.warnings == ("ITINERARY_CURRENCY_UNKNOWN",)


@pytest.mark.parametrize("currency", ["", "JPY", "USD", "港元"])
def test_recognized_baidu_domestic_template_resolves_yuan_but_never_overrides_foreign_currency(
    currency,
):
    result = _parse_layout(
        f"百度地图打车行程单\nBAIDU MAP ITINERARY\n{currency}\n共1笔行程\n"
        "序号  用车时间  服务方  车型  城市  起点  终点  实付金额\n"
        "1  2026-06-30  哈啰出行  快车  合肥  测试起点  测试终点  ￥14.97"
    )
    assert result.complete
    assert result.summary.currency == ("HKD" if currency == "港元" else currency or "CNY")
    assert result.summary.amount == Decimal("14.97")


def test_partial_table_and_total_mismatch_are_not_auto_matchable():
    text = (
        "行程单 人民币\n日期  起点  终点  金额\n2026-09-03  测试甲  测试乙  12.30\n"
        "2026-09-04  测试甲  测试乙  不清楚\n合计26.80元"
    )
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert not parsed.complete
    assert "ITINERARY_ROWS_INCOMPLETE" in parsed.warnings
    assert "ITINERARY_TOTAL_CONFLICT" in parsed.warnings


def test_continuation_without_repeated_table_header_is_not_silently_omitted():
    texts = [
        "行程单 人民币\n日期  起点  终点  金额\n2026-09-03  测试甲  测试乙  12.30",
        "行程单续页 人民币\n2026-09-04  测试丙  测试丁  14.50",
    ]
    parsed = parse_itinerary_pages(
        [
            ItineraryPage(
                index, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text"
            )
            for index, text in enumerate(texts, 1)
        ],
        page_count=2,
        reference_year=2026,
    )
    assert not parsed.complete
    assert "ITINERARY_ROWS_INCOMPLETE" in parsed.warnings
    assert parsed.summary.amount is None


def test_text_header_only_still_uses_local_ocr_fallback(
    settings_factory, tmp_path, run_itinerary_in_process
):
    settings = settings_factory()
    stored = _digital_pdf(
        tmp_path / "mixed-header.pdf", ("Itinerary details generated by Test Platform",)
    )
    fake = FakeOcrEngine(
        {
            "*": [
                OcrLine(line, 0.99)
                for line in (
                    "Itinerary CNY",
                    "Trip date: 2026-09-03",
                    "Total amount: 12.30",
                    "Origin: Test A",
                    "Destination: Test B",
                )
            ]
        }
    )
    parsed = run_itinerary_in_process(
        str(stored.path), "pdf", {}, settings.pdf_limits, settings.ocr_worker_limits, 2026, fake
    )["parsed"]
    assert parsed.source == "paddle"
    assert parsed.complete
    assert parsed.summary.amount == Decimal("12.30")


@pytest.mark.parametrize(
    "currency,expected", [("美元", "USD"), ("日元 ￥", "JPY"), ("港元", "HKD")]
)
def test_foreign_currency_names_are_not_yuan(currency, expected):
    text = f"行程单 {currency}\n乘车日期：2026-09-03\n合计12.30"
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert parsed.summary.currency == expected


def test_ocr_layout_keeps_table_column_geometry_without_second_model_pass(settings_factory):
    from types import SimpleNamespace

    from app.ocr.engine import PaddleLocalOcrEngine

    class Pipeline:
        calls = 0

        def predict(self, value):
            self.calls += 1
            texts = [
                "行程单 人民币",
                "日期",
                "起点",
                "终点",
                "金额",
                "2026-09-03",
                "测试甲",
                "测试乙",
                "12.30",
            ]
            boxes = [
                [0, 0, 140, 20],
                [0, 40, 80, 60],
                [200, 40, 250, 60],
                [400, 40, 450, 60],
                [600, 40, 650, 60],
                [0, 80, 100, 100],
                [200, 80, 260, 100],
                [400, 80, 460, 100],
                [600, 80, 670, 100],
            ]
            return [
                SimpleNamespace(
                    json={
                        "res": {
                            "rec_texts": texts,
                            "rec_scores": [0.99] * len(texts),
                            "rec_boxes": boxes,
                        }
                    }
                )
            ]

    engine = PaddleLocalOcrEngine(settings_factory())
    pipeline = Pipeline()
    engine._pipeline = pipeline
    lines, layout = engine.recognize_itinerary("test.png")
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(lines), layout, "paddle")], page_count=1, reference_year=2026
    )
    assert pipeline.calls == 1
    assert len(parsed.trips) == 1
    assert parsed.complete
    assert parsed.trips[0].amount == Decimal("12.30")


def test_split_application_date_does_not_replace_trip_date():
    text = "行程单 人民币\n申请时间\n2026-09-10\n乘车日期\n2026-09-03\n合计12.30元"
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "paddle")],
        page_count=1,
        reference_year=2026,
    )
    assert parsed.summary.start_date == parsed.summary.end_date == date(2026, 9, 3)


def test_invalid_date_row_cannot_disappear_from_table_total():
    text = (
        "行程单 人民币\n日期  起点  终点  金额\n2026-09-03  测试甲  测试乙  12.30\n"
        "2026-02-30  测试丙  测试丁  14.50"
    )
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert not parsed.complete
    assert "ITINERARY_ROWS_INCOMPLETE" in parsed.warnings
    assert parsed.summary.amount is None


def test_native_pdf_text_budget_returns_partial_without_raw_payload(
    settings_factory, tmp_path, monkeypatch, run_itinerary_in_process
):
    import app.ocr.itinerary_worker as worker

    settings = settings_factory()
    stored = _digital_pdf(
        tmp_path / "text-budget.pdf", ("Itinerary CNY 2026-09-03 Total: 12.30",) * 2
    )
    monkeypatch.setattr(worker, "MAX_TEXT_CHARACTERS", 80)
    result = run_itinerary_in_process(
        str(stored.path), "pdf", {}, settings.pdf_limits, settings.ocr_worker_limits, 2026
    )
    assert set(result) == {"ok", "parsed"}
    assert result["parsed"].processed_page_count == 1
    assert not result["parsed"].complete
    assert "ITINERARY_TEXT_LIMIT" in result["parsed"].warnings


def test_pdf_page_limit_is_enforced_before_scanning(
    settings_factory, tmp_path, run_itinerary_in_process
):
    settings = settings_factory()
    stored = _digital_pdf(tmp_path / "too-many-pages.pdf", ("",) * 31)
    result = run_itinerary_in_process(
        str(stored.path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        FakeOcrEngine(),
    )
    assert result["ok"] is False
    assert result["code"] == "PDF_PAGE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_whole_itinerary_uses_one_existing_timeout_and_preserves_cancellation(
    settings_factory, tmp_path
):
    import asyncio

    from app.core.errors import ApiError
    from app.services.process_jobs import ProcessJobTimeout

    class Runner:
        calls = 0
        failure = ProcessJobTimeout

        async def run(self, function, *args, timeout_seconds):
            self.calls += 1
            assert timeout_seconds == 120
            raise self.failure()

    stored = _digital_pdf(tmp_path / "deadline.pdf", ("",) * 6)
    runner = Runner()
    service = OcrService(settings_factory(ocr_enabled=True), None, runner)
    with pytest.raises(ApiError) as error:
        await service.recognize_itinerary_file(stored, reference_year=2026)
    assert error.value.code == "OCR_TIMEOUT"
    assert runner.calls == 1
    runner.failure = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await service.recognize_itinerary_file(stored, reference_year=2026)


def test_labelled_table_identifier_columns_are_retained_per_trip():
    text = (
        "行程单 人民币\n日期  起点  终点  金额  订单号  发票号码\n"
        "2026-09-03  测试甲  测试乙  12.30  order-0012  000012345678\n"
        "2026-09-04  测试丙  测试丁  14.50  order-0034  000012345679"
    )
    parsed = parse_itinerary_pages(
        [ItineraryPage(1, tuple(OcrLine(line, 1) for line in text.splitlines()), text, "pdf_text")],
        page_count=1,
        reference_year=2026,
    )
    assert parsed.complete
    assert parsed.trips[0].order_numbers == ("ORDER-0012",)
    assert parsed.trips[1].invoice_numbers == ("000012345679",)
    assert parsed.summary.order_numbers == ("ORDER-0012", "ORDER-0034")


def test_continuation_rows_inherit_explicit_document_date_range():
    from app.ocr.itinerary import ItineraryPage, parse_itinerary_pages
    from app.ocr.types import OcrLine

    header = "序号  上车时间  起点  终点  金额"
    pages = [
        ItineraryPage(
            1,
            (
                OcrLine("滴滴出行-行程单", 1),
                OcrLine("申请日期：2026-09-07 · 行程起止日期：2026-08-25 至 2026-09-04", 1),
                OcrLine("合计199.00元", 1),
            ),
            header + "\n1  08-25 08:15  酒店  公司  130.40",
            "pdf_text",
        ),
        ItineraryPage(
            2, (OcrLine(header, 1),), header + "\n11  09-04 08:28  公司  酒店  68.60", "pdf_text"
        ),
    ]
    result = parse_itinerary_pages(pages, page_count=2, reference_year=2026)
    assert len(result.trips) == 2
    assert result.complete
    assert str(result.summary.amount) == "199.00"
    assert str(result.trips[1].date) == "2026-09-04"


@pytest.mark.parametrize(
    "range_text",
    [
        "申请日期：2026-09-07",
        "行程起止日期：2025-12-25 至 2026-01-04",
        "行程起止日期：2026-09-04 至 2026-08-25",
    ],
)
def test_continuation_never_guesses_year_from_unusable_range(range_text):
    header = "序号  上车时间  起点  终点  金额"
    pages = [
        ItineraryPage(
            1, (OcrLine("行程单 合计199.00元", 1), OcrLine(range_text, 1)), "", "pdf_text"
        ),
        ItineraryPage(
            2, (OcrLine(header, 1),), header + "\n11  09-04 08:28  公司  酒店  199.00", "pdf_text"
        ),
    ]
    result = parse_itinerary_pages(pages, page_count=2, reference_year=2026)
    assert not result.trips
    assert not result.complete


def test_worker_keeps_titleless_continuation_native(
    settings_factory, tmp_path, monkeypatch, run_itinerary_in_process
):
    from types import SimpleNamespace

    import app.ocr.itinerary_worker as worker

    path = tmp_path / "continuation.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=640, height=842)
    writer.add_blank_page(width=640, height=842)
    with path.open("wb") as output:
        writer.write(output)
    header = "序号  上车时间  起点  终点  金额"
    texts = [
        "滴滴出行-行程单\n申请日期：2026-09-07 行程起止日期：2026-08-25 至 2026-09-04\n"
        "合计199.00元\n" + header + "\n1  08-25 08:15  酒店  公司  130.40",
        header + "\n11  09-04 08:28  公司  酒店  68.60",
    ]
    monkeypatch.setattr(
        worker,
        "PdfReader",
        lambda *a, **kw: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda *a, text=t, **kw: text) for t in texts]
        ),
    )

    class NoScan(FakeOcrEngine):
        def recognize(self, path):
            raise AssertionError("A native continuation must not invoke image OCR")

    settings = settings_factory()
    result = run_itinerary_in_process(
        str(path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        NoScan(),
        classify=True,
    )
    assert result["parsed"].complete
    assert len(result["parsed"].trips) == 2
    assert result["parsed"].source == "pdf_text"
