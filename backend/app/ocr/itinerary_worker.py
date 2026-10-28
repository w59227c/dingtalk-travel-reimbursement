from __future__ import annotations

import logging
import re
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import ApiError
from app.ocr.diagnostics import RecognitionDiagnostics
from app.ocr.engine import OcrRuntimeError
from app.ocr.extractors import normalized_lines
from app.ocr.itinerary import ItineraryPage, parse_itinerary_pages
from app.ocr.itinerary_pdf import positioned_itinerary_layout
from app.ocr.pdf_inspection import PdfLimits, inspect_single_page_pdf
from app.ocr.qr import decode_invoice_qr
from app.ocr.receipt_evidence import merge_pdf_ocr_lines, needs_pdf_ocr_fallback
from app.ocr.types import LocalOcrEngine, OcrLine, ParseContext, ReceiptKeywordRule
from app.ocr.workers import (
    WorkerOcrSettings,
    _append_passenger_fields,
    apply_worker_limits,
    cached_ocr_engine,
)

MAX_ITINERARY_PAGES = 30
MAX_SCANNED_PAGES = 5
MAX_TEXT_CHARACTERS = 100_000


def _native_page_has_evidence(text: str) -> bool:
    return len("".join(text.split())) >= 20 and bool(
        re.search(
            r"20\d{2}[年./-]\d{1,2}[月./-]\d{1,2}|(?:合计|金额|total|amount)\s*[:：]?\s*[¥￥]?\d",
            text,
            re.I,
        )
    )


def recognize_itinerary_worker(
    path: str,
    extension: str,
    raw_ocr_settings: dict[str, Any],
    raw_pdf_limits: dict[str, int],
    raw_worker_limits: dict[str, int],
    reference_year: int | None,
    fake_engine: LocalOcrEngine | None = None,
    classify: bool = False,
    keyword_rules: tuple[ReceiptKeywordRule, ...] | None = None,
    diagnostics: bool = False,
) -> dict[str, object]:
    """One bounded process; raw text leaves only for explicit admin diagnostics."""

    try:
        apply_worker_limits(raw_worker_limits)
        limits = PdfLimits(**raw_pdf_limits)
        reader = None
        if extension == "pdf":
            inspection = inspect_single_page_pdf(
                Path(path), limits, extract_text=False, max_pages=MAX_ITINERARY_PAGES
            )
            reader = PdfReader(path, strict=True)
        page_count = len(reader.pages) if reader is not None else 1
        pages: list[ItineraryPage] = []
        problems: list[str] = []
        scanned_count = 0
        characters = 0
        engine = fake_engine
        receipt_context: ParseContext | None = None
        diagnostic_capture = RecognitionDiagnostics() if diagnostics and classify else None

        def expense_context() -> ParseContext:
            nonlocal receipt_context
            if receipt_context is None:
                receipt_context = ParseContext(
                    reference_year=reference_year or date.today().year,
                    invoice_qr=decode_invoice_qr(
                        Path(path),
                        extension,
                        render_dpi=limits.render_dpi,
                    )
                    if page_count == 1
                    else None,
                )
            return receipt_context

        def scan_page(index: int) -> tuple[tuple[OcrLine, ...], str]:
            nonlocal engine
            if engine is None:
                detection = raw_ocr_settings.get("ocr_detection_model_dir")
                recognition = raw_ocr_settings.get("ocr_recognition_model_dir")
                if not detection or not recognition:
                    raise OcrRuntimeError("本地 OCR 模型未配置")
                engine = cached_ocr_engine(
                    WorkerOcrSettings(
                        Path(detection),
                        Path(recognition),
                        str(raw_ocr_settings["ocr_engine"]),
                        int(raw_ocr_settings["ocr_cpu_threads"]),
                    )
                )
            if engine.is_fake:
                found = tuple(engine.recognize(path))
                return found, "\n".join(line.text for line in found)
            if reader is None:
                found, scanned_layout = engine.recognize_itinerary(path)
                return tuple(found), scanned_layout
            import numpy as np
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(path)
            try:
                page = document[index]
                try:
                    bitmap = page.render(scale=limits.render_dpi / 72)
                    try:
                        pixels = np.asarray(bitmap.to_pil().convert("RGB"))
                        found, scanned_layout = engine.recognize_itinerary(pixels)
                        return tuple(found), scanned_layout
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()

        for index in range(page_count):
            text, layout = "", ""
            scanned_text: str | None = None
            native_receipt_lines: list[OcrLine] = []
            if reader is not None:
                pdf_logger = logging.getLogger("pypdf")
                previous = pdf_logger.level
                try:
                    pdf_logger.setLevel(logging.ERROR)
                    try:
                        text = reader.pages[index].extract_text() or ""
                        layout = reader.pages[index].extract_text(extraction_mode="layout") or ""
                    except (PdfReadError, OSError, ValueError, TypeError, KeyError, OverflowError):
                        text, layout = "", ""
                        # Some electronic tickets declare the same font stream
                        # twice. PDFium can read them without modifying the PDF.
                        import pypdfium2 as pdfium

                        with ExitStack() as resources:
                            document = pdfium.PdfDocument(path)
                            resources.callback(document.close)
                            page = document[index]
                            resources.callback(page.close)
                            textpage = page.get_textpage()
                            resources.callback(textpage.close)
                            if textpage.count_chars() <= MAX_TEXT_CHARACTERS:
                                text = textpage.get_text_range()
                                layout = text
                finally:
                    pdf_logger.setLevel(previous)
            source = "pdf_text"
            native_enough = _native_page_has_evidence(text)
            # Continuation tables often omit the title, year and total. Keep
            # their native text when the preceding document supplies the range.
            continuation = False
            if pages and all(label in text for label in ("上车时间", "起点", "终点", "金额")):
                candidate_page = ItineraryPage(
                    index + 1,
                    tuple(OcrLine(t.strip(), 1) for t in text.splitlines() if t.strip()),
                    layout,
                    "pdf_text",
                )
                continuation_result = parse_itinerary_pages(
                    [*pages, candidate_page],
                    page_count=index + 1,
                    reference_year=reference_year,
                )
                continuation = (
                    any(trip.page == index + 1 for trip in continuation_result.trips)
                    and "ITINERARY_NOT_RECOGNIZED" not in continuation_result.warnings
                    and "ITINERARY_ROWS_INCOMPLETE" not in continuation_result.warnings
                )
                native_enough = native_enough or continuation
            candidate_kind = None
            if native_enough and reader is not None and (classify or inspection.embedded_images):
                candidate = ItineraryPage(
                    index + 1,
                    tuple(OcrLine(line.strip(), 1) for line in text.splitlines() if line.strip()),
                    layout,
                    "pdf_text",
                )
                # Native headings/totals over a scanned table do not mean that
                # its rows have been read. Scan within the same five-page cap.
                if classify:
                    from app.ocr.materials import classify_material
                    from app.ocr.parsers import ReceiptParserRegistry

                    candidate_kind, _ = classify_material(
                        [*pages, candidate] if continuation else [candidate], page_count=page_count
                    )
                    if candidate_kind == "expense":
                        candidate_lines = [(line.text, line.confidence) for line in candidate.lines]
                        before = (
                            [value for value, _score in candidate_lines]
                            if diagnostic_capture
                            else []
                        )
                        _append_passenger_fields(
                            candidate_lines,
                            text,
                            layout,
                            pdf_path=path if page_count == 1 else None,
                        )
                        if diagnostic_capture is not None:
                            diagnostic_capture.record_derived(
                                index + 1, before, (value for value, _score in candidate_lines)
                            )
                        native_receipt_lines = normalized_lines(
                            [OcrLine(value, score) for value, score in candidate_lines]
                        )
                        parsed_candidate = ReceiptParserRegistry(keyword_rules=keyword_rules).parse(
                            native_receipt_lines,
                            expense_context(),
                        )
                        native_enough = not needs_pdf_ocr_fallback(
                            parsed_candidate,
                            native_receipt_lines,
                        )
                    elif candidate_kind == "unknown":
                        native_enough = False
                    elif candidate_kind == "itinerary":
                        native_enough = bool(
                            parse_itinerary_pages(
                                [*pages, candidate] if continuation else [candidate],
                                page_count=index + 1 if continuation else 1,
                                reference_year=reference_year,
                            ).trips
                        )
                else:
                    native_enough = bool(
                        parse_itinerary_pages(
                            [*pages, candidate] if continuation else [candidate],
                            page_count=index + 1 if continuation else 1,
                            reference_year=reference_year,
                        ).trips
                    )
            # A layout string can merge adjacent destination/mileage cells even
            # when the PDF retains their exact positions. Try the bounded native
            # table recovery before paying for a model pass.
            if (
                reader is not None
                and (not classify or candidate_kind == "itinerary")
                and _native_page_has_evidence(text)
                and re.search(r"行程单|\bitinerary\b", text, re.I)
                and re.search(r"里程|\bmileage\b", text, re.I)
            ):
                candidate_lines = tuple(
                    OcrLine(line.strip(), 1) for line in text.splitlines() if line.strip()
                )
                native_parsed = parse_itinerary_pages(
                    [ItineraryPage(index + 1, candidate_lines, layout, "pdf_text")],
                    page_count=1,
                    reference_year=reference_year,
                )
                if not native_parsed.trips or "ITINERARY_ROWS_INCOMPLETE" in native_parsed.warnings:
                    positioned = positioned_itinerary_layout(
                        path,
                        index,
                        max_characters=MAX_TEXT_CHARACTERS - characters,
                    )
                    if positioned:
                        recovered = parse_itinerary_pages(
                            [ItineraryPage(index + 1, candidate_lines, positioned, "pdf_text")],
                            page_count=1,
                            reference_year=reference_year,
                        )
                        if recovered.complete and recovered.trips:
                            layout, native_enough = positioned, True
            if native_enough:
                lines = tuple(
                    OcrLine(line.strip(), 1) for line in text.splitlines() if line.strip()
                )
            else:
                if scanned_count >= MAX_SCANNED_PAGES:
                    problems.append("ITINERARY_OCR_PAGE_LIMIT")
                    continue
                scanned_count += 1
                source = "paddle"
                try:
                    lines, layout = scan_page(index)
                    if diagnostic_capture is not None:
                        scanned_text = "\n".join(line.text for line in lines)
                except (OcrRuntimeError, ApiError, OSError, ValueError):
                    if not native_receipt_lines:
                        raise
                    # Keep the native candidate when the supplemental pass
                    # fails, matching the regular receipt OCR endpoint.
                    lines = tuple(native_receipt_lines)
                    source = "pdf_text"
                if native_receipt_lines:
                    lines = tuple(merge_pdf_ocr_lines(native_receipt_lines, list(lines)))
            characters += sum(len(line.text) for line in lines) + len(layout)
            if characters > MAX_TEXT_CHARACTERS:
                problems.append("ITINERARY_TEXT_LIMIT")
                break
            pages.append(ItineraryPage(index + 1, lines, layout, source))
            if diagnostic_capture is not None:
                if reader is not None:
                    diagnostic_capture.record_extraction(
                        index + 1,
                        "pdf_text",
                        text,
                        used_for_parsing=native_enough or bool(native_receipt_lines),
                    )
                if scanned_text is not None:
                    diagnostic_capture.record_extraction(
                        index + 1,
                        "paddle",
                        scanned_text,
                        used_for_parsing=True,
                    )
        if classify:
            from app.ocr.materials import classify_material
            from app.ocr.parsers import ReceiptParserRegistry

            kind, reason = classify_material(pages, page_count=page_count)
            if problems or len(pages) != page_count:
                kind, reason = "unknown", "材料未完整识别，请确认用途或拆分后重传"
            result: dict[str, object] = {
                "ok": True,
                "materialKind": kind,
                "reason": reason,
                "pageCount": page_count,
            }
            trace: dict[str, object] | None = {} if diagnostic_capture is not None else None
            if kind == "expense":
                raw_lines = [(line.text, line.confidence) for line in pages[0].lines]
                before = [value for value, _score in raw_lines] if diagnostic_capture else []
                _append_passenger_fields(
                    raw_lines,
                    "\n".join(line.text for line in pages[0].lines),
                    pages[0].layout_text,
                    pdf_path=path if extension == "pdf" and page_count == 1 else None,
                )
                if diagnostic_capture is not None:
                    diagnostic_capture.record_derived(
                        1, before, (value for value, _score in raw_lines)
                    )
                result["expense"] = ReceiptParserRegistry(keyword_rules=keyword_rules).parse(
                    normalized_lines([OcrLine(text, score) for text, score in raw_lines]),
                    expense_context(),
                    trace=trace,
                )
            elif kind == "itinerary":
                result["parsed"] = parse_itinerary_pages(
                    pages,
                    page_count=page_count,
                    reference_year=reference_year,
                    warnings=problems,
                )
            elif kind == "payment_proof":
                from app.ocr.payment_proofs import payment_proof_details

                result["paymentDetails"] = payment_proof_details(
                    pages, reference_year=reference_year or date.today().year
                )
            elif kind == "hotel_bill":
                from app.ocr.hotel_bills import hotel_bill_details

                result["hotelBillDetails"] = hotel_bill_details(pages)
            if trace is not None and diagnostic_capture is not None:
                trace.update(
                    diagnostic_capture.as_dict(
                        complete=not problems and len(pages) == page_count,
                    )
                )
                result["diagnostic"] = trace
            return result
        return {
            "ok": True,
            "parsed": parse_itinerary_pages(
                pages,
                page_count=page_count,
                reference_year=reference_year,
                warnings=problems,
            ),
        }
    except ApiError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message, "status": exc.status_code}
    except (MemoryError, RecursionError):
        return {
            "ok": False,
            "code": "OCR_FAILED",
            "message": "行程单超过本地处理限制",
            "status": 422,
        }
    except (OcrRuntimeError, PdfReadError, OSError, ValueError, TypeError, KeyError, OverflowError):
        return {
            "ok": False,
            "code": "OCR_FAILED",
            "message": "行程单识别失败，请人工关联",
            "status": 422,
        }
