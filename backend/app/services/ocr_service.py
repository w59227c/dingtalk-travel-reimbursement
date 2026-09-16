from __future__ import annotations

import logging
from datetime import date

from app.core.config import Settings
from app.core.errors import ApiError
from app.domain.categories import CATEGORY_BY_ID, ExpenseCategory
from app.domain.money import money_string
from app.ocr.extractors import normalized_lines
from app.ocr.itinerary_worker import recognize_itinerary_worker
from app.ocr.parsers import ReceiptParserRegistry
from app.ocr.qr import invoice_qr_from_payload
from app.ocr.receipt_evidence import merge_pdf_ocr_lines, needs_pdf_ocr_fallback
from app.ocr.types import (
    InvoiceQrEvidence,
    LocalOcrEngine,
    OcrLine,
    ParseContext,
    ParsedExpense,
    ParsedItinerary,
    ReceiptKeywordRule,
)
from app.ocr.workers import (
    extract_pdf_text_worker,
    recognize_document_worker,
    verify_ocr_runtime_worker,
)
from app.services.process_jobs import (
    KillableProcessRunner,
    ProcessJobBusy,
    ProcessJobResourceLimit,
    ProcessJobTimeout,
)
from app.services.temp_files import StoredFile

logger = logging.getLogger(__name__)


def parsed_itinerary_payload(file_id: str, parsed: ParsedItinerary) -> dict[str, object]:
    summary = parsed.summary
    return {
        "fileId": file_id,
        "version": 1,
        "kind": "itinerary",
        "status": "recognized",
        "source": parsed.source,
        "pageCount": parsed.page_count,
        "processedPageCount": parsed.processed_page_count,
        "complete": parsed.complete,
        "summary": {
            "currency": summary.currency,
            "amount": money_string(summary.amount) if summary.amount is not None else None,
            "startDate": summary.start_date.isoformat() if summary.start_date else None,
            "endDate": summary.end_date.isoformat() if summary.end_date else None,
            "invoiceNumbers": list(summary.invoice_numbers),
            "orderNumbers": list(summary.order_numbers),
        },
        "trips": [
            {
                "page": trip.page,
                "row": trip.row,
                "date": trip.date.isoformat() if trip.date else None,
                "amount": money_string(trip.amount) if trip.amount is not None else None,
                "origin": trip.origin,
                "destination": trip.destination,
                "invoiceNumbers": list(trip.invoice_numbers),
                "orderNumbers": list(trip.order_numbers),
            }
            for trip in parsed.trips
        ],
        "warnings": list(parsed.warnings),
        "error": None,
    }


def failed_itinerary_payload(file_id: str, code: str, message: str) -> dict[str, object]:
    result = parsed_itinerary_payload(file_id, ParsedItinerary("unknown", 0, 0, False))
    result.update(
        status="failed",
        warnings=["MANUAL_REVIEW_REQUIRED"],
        error={"code": code, "message": message},
    )
    return result


def parsed_expense_payload(file_id: str, parsed: ParsedExpense) -> dict[str, object]:
    category = CATEGORY_BY_ID[parsed.category]
    return {
        "fileId": file_id,
        "type": parsed.receipt_type,
        "categoryId": parsed.category.value,
        "categoryName": category.name,
        "date": parsed.date.isoformat() if parsed.date else None,
        "description": parsed.description,
        "amount": money_string(parsed.amount) if parsed.amount is not None else None,
        "receiptCount": 1,
        "requiresItinerary": parsed.requires_itinerary,
        "transportType": parsed.transport_type,
        "railType": parsed.rail_type,
        "invoiceNumbers": list(parsed.invoice_numbers),
        "orderNumbers": list(parsed.order_numbers),
        "originalCurrency": parsed.original_currency,
        "originalAmount": (
            money_string(parsed.original_amount) if parsed.original_amount is not None else None
        ),
        "source": "ocr",
        "confidence": f"{max(0.0, min(1.0, parsed.confidence)):.2f}",
        "warnings": list(parsed.warnings),
        "status": "recognized",
        "error": None,
    }


def failed_expense_payload(file_id: str, code: str, message: str) -> dict[str, object]:
    return {
        "fileId": file_id,
        "type": "other",
        "categoryId": "other",
        "categoryName": CATEGORY_BY_ID[ExpenseCategory.OTHER].name,
        "date": None,
        "description": None,
        "amount": None,
        "receiptCount": 1,
        "requiresItinerary": False,
        "transportType": None,
        "railType": None,
        "invoiceNumbers": [],
        "orderNumbers": [],
        "originalCurrency": None,
        "originalAmount": None,
        "source": "ocr",
        "confidence": "0.00",
        "warnings": ["MANUAL_REVIEW_REQUIRED"],
        "status": "failed",
        "error": {"code": code, "message": message},
    }


class OcrService:
    def __init__(
        self,
        settings: Settings,
        engine: LocalOcrEngine | None,
        process_runner: KillableProcessRunner,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._process_runner = process_runner

    async def ensure_ready(self) -> None:
        if not self._settings.ocr_enabled:
            return
        if self._engine is not None and self._engine.is_fake:
            self._engine.ensure_ready()
            return
        if self._settings.app_env != "production":
            return
        detection = self._settings.ocr_detection_model_dir
        recognition = self._settings.ocr_recognition_model_dir
        if detection is None or recognition is None:
            raise RuntimeError("本地 OCR 模型未就绪")
        try:
            result = await self._process_runner.run(
                verify_ocr_runtime_worker,
                {
                    "ocr_detection_model_dir": str(detection),
                    "ocr_recognition_model_dir": str(recognition),
                    "ocr_engine": self._settings.ocr_engine,
                    "ocr_cpu_threads": self._settings.ocr_cpu_threads,
                },
                self._settings.ocr_worker_limits,
                timeout_seconds=self._settings.ocr_timeout_seconds,
            )
        except (ProcessJobBusy, ProcessJobTimeout, ProcessJobResourceLimit) as exc:
            raise RuntimeError("本地 OCR 运行时自检失败") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("本地 OCR 运行时自检失败")

    async def close(self) -> None:
        return None

    async def recognize_material_file(
        self,
        stored: StoredFile,
        *,
        reference_year: int | None,
        keyword_rules: tuple[ReceiptKeywordRule, ...] | None = None,
    ) -> dict[str, object]:
        """Classify and parse from the same bounded extraction, never from a filename."""
        if not self._settings.ocr_enabled:
            raise ApiError("OCR_DISABLED", "自动识别尚未配置，请确认材料用途", 503)
        detection = self._settings.ocr_detection_model_dir
        recognition = self._settings.ocr_recognition_model_dir
        try:
            result = await self._process_runner.run(
                recognize_itinerary_worker,
                str(stored.path),
                stored.extension,
                {
                    "ocr_detection_model_dir": str(detection) if detection else None,
                    "ocr_recognition_model_dir": str(recognition) if recognition else None,
                    "ocr_engine": self._settings.ocr_engine,
                    "ocr_cpu_threads": self._settings.ocr_cpu_threads,
                },
                self._settings.pdf_limits,
                self._settings.ocr_worker_limits,
                reference_year,
                self._engine if self._engine is not None and self._engine.is_fake else None,
                True,
                keyword_rules,
                timeout_seconds=self._settings.ocr_timeout_seconds,
            )
        except ProcessJobBusy as exc:
            raise ApiError("OCR_BUSY", "本地识别繁忙，请稍后重试", 429) from exc
        except ProcessJobTimeout as exc:
            raise ApiError("OCR_TIMEOUT", "材料识别超时，请重试或确认用途", 504) from exc
        except ProcessJobResourceLimit as exc:
            raise ApiError("OCR_FAILED", "材料识别超过处理限制，请确认用途", 422) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise ApiError("OCR_FAILED", "材料识别失败，请重试或确认用途", 422)
        kind = result.get("materialKind", "unknown")
        payload: dict[str, object] = {}
        if kind == "expense" and isinstance(result.get("expense"), ParsedExpense):
            payload = parsed_expense_payload(stored.temp_id, result["expense"])
        elif kind == "itinerary" and isinstance(result.get("parsed"), ParsedItinerary):
            payload = parsed_itinerary_payload(stored.temp_id, result["parsed"])
        elif kind == "payment_proof" and isinstance(result.get("paymentDetails"), dict):
            payload["paymentDetails"] = result["paymentDetails"]
        elif kind == "hotel_bill" and isinstance(result.get("hotelBillDetails"), dict):
            payload["hotelBillDetails"] = result["hotelBillDetails"]
        elif kind != "payment_proof":
            kind = "unknown"
        payload["_materialClassification"] = {
            "status": "needs_confirmation" if kind == "unknown" else "classified",
            "kind": kind,
            "reason": result.get("reason"),
            "pageCount": result.get("pageCount"),
        }
        return payload

    async def recognize_itinerary_file(
        self,
        stored: StoredFile,
        *,
        reference_year: int | None,
    ) -> dict[str, object]:
        if not self._settings.ocr_enabled:
            raise ApiError("OCR_DISABLED", "自动识别尚未配置，请人工关联行程单", 503)
        detection = self._settings.ocr_detection_model_dir
        recognition = self._settings.ocr_recognition_model_dir
        try:
            result = await self._process_runner.run(
                recognize_itinerary_worker,
                str(stored.path),
                stored.extension,
                {
                    "ocr_detection_model_dir": str(detection) if detection else None,
                    "ocr_recognition_model_dir": str(recognition) if recognition else None,
                    "ocr_engine": self._settings.ocr_engine,
                    "ocr_cpu_threads": self._settings.ocr_cpu_threads,
                },
                self._settings.pdf_limits,
                self._settings.ocr_worker_limits,
                reference_year,
                self._engine if self._engine is not None and self._engine.is_fake else None,
                timeout_seconds=self._settings.ocr_timeout_seconds,
            )
        except ProcessJobBusy as exc:
            raise ApiError("OCR_BUSY", "本地识别繁忙，请稍后重试", 429) from exc
        except ProcessJobTimeout as exc:
            raise ApiError("OCR_TIMEOUT", "行程单识别超时，请重试或人工关联", 504) from exc
        except ProcessJobResourceLimit as exc:
            raise ApiError("OCR_FAILED", "行程单识别失败，请人工关联", 422) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict):
                raise ApiError(
                    str(result.get("code", "OCR_FAILED")),
                    str(result.get("message", "行程单识别失败，请人工关联")),
                    int(result.get("status", 422)),
                )
            raise ApiError("OCR_FAILED", "行程单识别失败，请人工关联", 422)
        parsed = result.get("parsed")
        if not isinstance(parsed, ParsedItinerary):
            raise ApiError("OCR_FAILED", "行程单识别失败，请人工关联", 422)
        return parsed_itinerary_payload(stored.temp_id, parsed)

    async def _fake_lines(self, stored: StoredFile) -> list[OcrLine]:
        """Deterministic test/development seam; never used in production."""

        if self._engine is None or not self._engine.is_fake:
            raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422)
        if stored.extension == "pdf":
            try:
                result = await self._process_runner.run(
                    extract_pdf_text_worker,
                    str(stored.path),
                    self._settings.pdf_limits,
                    self._settings.file_worker_limits,
                    timeout_seconds=self._settings.pdf_preflight_timeout_seconds,
                )
            except ProcessJobBusy as exc:
                raise ApiError("OCR_BUSY", "本地识别繁忙，请稍后重试", 429) from exc
            except ProcessJobTimeout as exc:
                raise ApiError("OCR_TIMEOUT", "票据识别超时，请手工填写", 504) from exc
            except ProcessJobResourceLimit as exc:
                raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422) from exc
            if not isinstance(result, dict) or not result.get("ok"):
                raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422)
            raw_lines = result.get("lines")
            if isinstance(raw_lines, list) and raw_lines:
                return normalized_lines(
                    [OcrLine(text=str(line), confidence=float(score)) for line, score in raw_lines]
                )
        return normalized_lines(self._engine.recognize(str(stored.path)))

    async def _ocr_lines(
        self,
        stored: StoredFile,
        *,
        prefer_pdf_text: bool = True,
    ) -> tuple[list[OcrLine], str, InvoiceQrEvidence | None]:
        if not self._settings.ocr_enabled:
            raise ApiError("OCR_DISABLED", "自动识别尚未配置，可手工填写票据信息", 503)
        if self._engine is not None and self._engine.is_fake:
            return await self._fake_lines(stored), "fake", None
        detection = self._settings.ocr_detection_model_dir
        recognition = self._settings.ocr_recognition_model_dir
        if detection is None or recognition is None:
            raise ApiError("OCR_FAILED", "自动识别模型未配置，请手工填写", 503)
        try:
            result = await self._process_runner.run(
                recognize_document_worker,
                str(stored.path),
                stored.extension,
                {
                    "ocr_detection_model_dir": str(detection),
                    "ocr_recognition_model_dir": str(recognition),
                    "ocr_engine": self._settings.ocr_engine,
                    "ocr_cpu_threads": self._settings.ocr_cpu_threads,
                },
                self._settings.pdf_limits,
                self._settings.ocr_worker_limits,
                prefer_pdf_text,
                timeout_seconds=self._settings.ocr_timeout_seconds,
            )
        except ProcessJobBusy as exc:
            raise ApiError("OCR_BUSY", "本地识别正在处理另一张票据，请稍后重试", 429) from exc
        except ProcessJobTimeout as exc:
            raise ApiError("OCR_TIMEOUT", "票据识别超时，请重试或手工填写", 504) from exc
        except ProcessJobResourceLimit as exc:
            raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            code = (
                str(result.get("code", "OCR_FAILED")) if isinstance(result, dict) else "OCR_FAILED"
            )
            message = (
                str(result.get("message", "票据识别失败，请手工填写"))
                if isinstance(result, dict)
                else "票据识别失败，请手工填写"
            )
            status_code = int(result.get("status", 422)) if isinstance(result, dict) else 422
            if code in {"WORKER_LIMIT_SETUP_FAILED", "WORKER_RESOURCE_LIMIT"}:
                code, message, status_code = "OCR_FAILED", "票据识别失败，请手工填写", 422
            logger.warning("Local OCR failed", extra={"error_code": code})
            raise ApiError(code, message, status_code)
        raw_lines = result.get("lines")
        if not isinstance(raw_lines, list):
            raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422)
        source = result.get("source", "paddle")
        if source not in {"pdf_text", "paddle"}:
            raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422)
        try:
            lines = normalized_lines(
                [OcrLine(text=str(text), confidence=float(score)) for text, score in raw_lines]
            )
            return lines, str(source), invoice_qr_from_payload(result.get("invoiceQr"))
        except (TypeError, ValueError) as exc:
            raise ApiError("OCR_FAILED", "票据识别失败，请手工填写", 422) from exc

    @staticmethod
    def _needs_pdf_ocr_fallback(
        parsed: ParsedExpense,
        lines: list[OcrLine],
    ) -> bool:
        return needs_pdf_ocr_fallback(parsed, lines)

    @staticmethod
    def _merge_pdf_ocr_lines(
        pdf_text_lines: list[OcrLine],
        image_ocr_lines: list[OcrLine],
    ) -> list[OcrLine]:
        return merge_pdf_ocr_lines(pdf_text_lines, image_ocr_lines)

    async def recognize_file(
        self,
        stored: StoredFile,
        *,
        reference_year: int | None,
        keyword_rules: tuple[ReceiptKeywordRule, ...] | None = None,
    ) -> ParsedExpense:
        lines, source, invoice_qr = await self._ocr_lines(stored)
        if not lines:
            raise ApiError("OCR_NO_TEXT", "未识别到可用文字，请手工填写", 422)
        context = ParseContext(
            reference_year=reference_year or date.today().year,
            invoice_qr=invoice_qr,
        )
        registry = ReceiptParserRegistry(keyword_rules=keyword_rules)
        parsed = registry.parse(lines, context)
        if source != "pdf_text" or not self._needs_pdf_ocr_fallback(parsed, lines):
            return parsed

        # A PDF text layer can omit visually rendered totals, routes, or transport
        # types. Retry once with real OCR only when those omissions affect output.
        try:
            fallback_lines, _fallback_source, fallback_qr = await self._ocr_lines(
                stored,
                prefer_pdf_text=False,
            )
        except ApiError as exc:
            logger.warning("PDF text fallback OCR failed", extra={"error_code": exc.code})
            return parsed
        if not fallback_lines:
            return parsed
        if context.invoice_qr is None and fallback_qr is not None:
            context = ParseContext(reference_year=context.reference_year, invoice_qr=fallback_qr)
        combined_lines = self._merge_pdf_ocr_lines(lines, fallback_lines)
        return registry.parse(combined_lines, context)
