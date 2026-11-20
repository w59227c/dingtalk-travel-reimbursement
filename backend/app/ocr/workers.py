from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.errors import ApiError
from app.ocr.engine import OcrRuntimeError, PaddleLocalOcrEngine
from app.ocr.extractors import (
    PASSENGER_OCCURRENCE_DATE_PREFIX,
    PASSENGER_ROUTE_PREFIX,
    extract_passenger_fields_from_layout,
    extract_passenger_transport_from_layout,
)
from app.ocr.image_validation import validate_image_worker as validate_image_worker
from app.ocr.invoice_parties import (
    SELLER_NAME_PREFIX,
    invoice_seller_from_pdf,
    invoice_seller_name,
)
from app.ocr.pdf_inspection import PdfLimits, inspect_single_page_pdf
from app.ocr.qr import decode_invoice_qr, invoice_qr_to_payload
from app.ocr.worker_limits import apply_worker_limits
from app.ocr.worker_limits import worker_resource_failure as _resource_failure

_DATE_SIGNAL = re.compile(r"20\d{2}[年./-]\d{1,2}[月./-]\d{1,2}|\d{1,2}月\d{1,2}日")
_MONEY_SIGNAL = re.compile(r"[¥￥]\s*\d|价税合计|票价|金额")
_PASSENGER_ROUTE_HEADERS = ("出发地", "到达地", "交通工具类型")
_ENGINES: dict[tuple[str, str, str, int], PaddleLocalOcrEngine] = {}


def _passenger_fields(plain_text: str, layout_text: str) -> tuple[date | None, str | None]:
    if not all(header in plain_text for header in _PASSENGER_ROUTE_HEADERS):
        return None, None
    return extract_passenger_fields_from_layout(layout_text)


def _append_passenger_fields(
    lines: list[tuple[str, float]],
    plain_text: str,
    layout_text: str,
    *,
    pdf_path: str | None = None,
) -> None:
    seller = invoice_seller_name(layout_text)
    if seller is None and pdf_path is not None:
        seller = invoice_seller_from_pdf(pdf_path)
    if seller:
        lines.append((f"{SELLER_NAME_PREFIX}{seller}", 1.0))
    occurrence_date, route = _passenger_fields(plain_text, layout_text)
    if occurrence_date:
        lines.append((f"{PASSENGER_OCCURRENCE_DATE_PREFIX}{occurrence_date.isoformat()}", 1.0))
    if route:
        lines.append((f"{PASSENGER_ROUTE_PREFIX}{route}", 1.0))
    if all(header in plain_text for header in _PASSENGER_ROUTE_HEADERS):
        transport = extract_passenger_transport_from_layout(layout_text)
        if transport:
            lines.insert(0, (f"交通工具类型：{transport}", 1.0))


@dataclass(frozen=True, slots=True)
class WorkerOcrSettings:
    ocr_detection_model_dir: Path
    ocr_recognition_model_dir: Path
    ocr_engine: str
    ocr_cpu_threads: int


def _pdf_text_is_sufficient(text: str) -> bool:
    normalized = " ".join(text.split())
    if len(normalized) < 20:
        return False
    has_document_signal = any(
        keyword in normalized for keyword in ("发票", "铁路", "客票", "住宿", "价税合计", "票价")
    )
    return (
        has_document_signal
        and bool(_DATE_SIGNAL.search(normalized))
        and bool(_MONEY_SIGNAL.search(normalized))
    )


def _safe_failure(exc: ApiError | OcrRuntimeError) -> dict[str, Any]:
    if isinstance(exc, ApiError):
        return {"ok": False, "code": exc.code, "message": exc.message, "status": exc.status_code}
    return {"ok": False, "code": "OCR_FAILED", "message": "票据识别失败，请手工填写", "status": 422}


def inspect_pdf_worker(
    path: str,
    raw_limits: dict[str, int],
    raw_worker_limits: dict[str, int],
    max_pages: int = 1,
) -> dict[str, Any]:
    """Process entry point for upload-time PDF metadata validation."""

    try:
        apply_worker_limits(raw_worker_limits)
        inspection = inspect_single_page_pdf(
            Path(path), PdfLimits(**raw_limits), extract_text=False, max_pages=max_pages
        )
        return {
            "ok": True,
            "renderWidth": inspection.render_width,
            "renderHeight": inspection.render_height,
            "embeddedImages": inspection.embedded_images,
            "embeddedPixels": inspection.embedded_pixels,
            "pageCount": inspection.page_count,
        }
    except MemoryError:
        return _resource_failure()
    except ApiError as exc:
        return _safe_failure(exc)


def extract_pdf_text_worker(
    path: str,
    raw_pdf_limits: dict[str, int],
    raw_worker_limits: dict[str, int],
) -> dict[str, Any]:
    """Development fake-engine seam that still parses PDFs in the bounded worker."""

    try:
        apply_worker_limits(raw_worker_limits)
        inspection = inspect_single_page_pdf(
            Path(path),
            PdfLimits(**raw_pdf_limits),
            extract_text=True,
        )
        lines = [(line, 1.0) for line in inspection.text.splitlines() if line.strip()]
        _append_passenger_fields(lines, inspection.text, inspection.layout_text, pdf_path=path)
        return {
            "ok": True,
            "lines": lines,
        }
    except MemoryError:
        return _resource_failure()
    except ApiError as exc:
        return _safe_failure(exc)


def cached_ocr_engine(settings: WorkerOcrSettings) -> PaddleLocalOcrEngine:
    key = (
        str(settings.ocr_detection_model_dir),
        str(settings.ocr_recognition_model_dir),
        settings.ocr_engine,
        settings.ocr_cpu_threads,
    )
    engine = _ENGINES.get(key)
    if engine is None:
        _ENGINES.clear()
        engine = PaddleLocalOcrEngine(settings)
        _ENGINES[key] = engine
    return engine


def recognize_document_worker(
    path: str,
    extension: str,
    raw_ocr_settings: dict[str, Any],
    raw_pdf_limits: dict[str, int],
    raw_worker_limits: dict[str, int],
    prefer_pdf_text: bool = True,
) -> dict[str, Any]:
    """Process entry point for one independent receipt file."""

    try:
        apply_worker_limits(raw_worker_limits)
        pdf_limits = PdfLimits(**raw_pdf_limits)
        invoice_qr = invoice_qr_to_payload(
            decode_invoice_qr(
                Path(path),
                extension,
                render_dpi=pdf_limits.render_dpi,
            )
        )
        if extension == "pdf" and prefer_pdf_text:
            inspection = inspect_single_page_pdf(
                Path(path),
                pdf_limits,
                extract_text=True,
            )
            if _pdf_text_is_sufficient(inspection.text):
                lines = [(line, 1.0) for line in inspection.text.splitlines() if line.strip()]
                _append_passenger_fields(
                    lines, inspection.text, inspection.layout_text, pdf_path=path
                )
                return {
                    "ok": True,
                    "lines": lines,
                    "source": "pdf_text",
                    "invoiceQr": invoice_qr,
                }

        settings = WorkerOcrSettings(
            ocr_detection_model_dir=Path(raw_ocr_settings["ocr_detection_model_dir"]),
            ocr_recognition_model_dir=Path(raw_ocr_settings["ocr_recognition_model_dir"]),
            ocr_engine=str(raw_ocr_settings["ocr_engine"]),
            ocr_cpu_threads=int(raw_ocr_settings["ocr_cpu_threads"]),
        )
        engine = cached_ocr_engine(settings)
        lines = engine.recognize(path)
        return {
            "ok": True,
            "lines": [(line.text, line.confidence) for line in lines],
            "source": "paddle",
            "invoiceQr": invoice_qr,
        }
    except MemoryError:
        return _resource_failure()
    except (ApiError, OcrRuntimeError) as exc:
        return _safe_failure(exc)


def verify_ocr_runtime_worker(
    raw_ocr_settings: dict[str, Any],
    raw_worker_limits: dict[str, int],
) -> dict[str, Any]:
    """Initialize the locked runtime and execute one blank-image inference."""

    path: Path | None = None
    try:
        apply_worker_limits(raw_worker_limits)
        settings = WorkerOcrSettings(
            ocr_detection_model_dir=Path(raw_ocr_settings["ocr_detection_model_dir"]),
            ocr_recognition_model_dir=Path(raw_ocr_settings["ocr_recognition_model_dir"]),
            ocr_engine=str(raw_ocr_settings["ocr_engine"]),
            ocr_cpu_threads=int(raw_ocr_settings["ocr_cpu_threads"]),
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
            path = Path(temporary.name)
        Image.new("RGB", (64, 64), "white").save(path, format="PNG")
        PaddleLocalOcrEngine(settings).recognize(str(path))
        return {"ok": True}
    except MemoryError:
        return _resource_failure()
    except (ApiError, OcrRuntimeError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ApiError | OcrRuntimeError):
            return _safe_failure(exc)
        return {
            "ok": False,
            "code": "OCR_FAILED",
            "message": "本地 OCR 运行时自检失败",
            "status": 503,
        }
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
