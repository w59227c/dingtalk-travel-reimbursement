from __future__ import annotations

import io
import math
from typing import Any

from app.ocr.workers import apply_worker_limits

PDF_PREVIEW_MAX_PIXELS = 2_500_000
PDF_PREVIEW_MAX_DIMENSION = 2_200
PDF_PREVIEW_MAX_SCALE = 2.0


def render_pdf_preview_page_worker(
    content: bytes,
    page_number: int,
    raw_worker_limits: dict[str, int],
) -> dict[str, Any]:
    """Render one verified PDF page as a bounded RGB PNG in an isolated process."""

    apply_worker_limits(raw_worker_limits)
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(content)
        try:
            page_count = len(document)
            if page_number < 1 or page_number > page_count:
                return {
                    "ok": False,
                    "code": "PDF_PREVIEW_PAGE_OUT_OF_RANGE",
                    "message": "请求的 PDF 页面不存在",
                    "status": 416,
                }
            page = document[page_number - 1]
            try:
                width_points, height_points = page.get_size()
                if width_points <= 0 or height_points <= 0:
                    raise ValueError("PDF page dimensions must be positive")
                scale = min(
                    PDF_PREVIEW_MAX_SCALE,
                    PDF_PREVIEW_MAX_DIMENSION / max(width_points, height_points),
                    math.sqrt(PDF_PREVIEW_MAX_PIXELS / (width_points * height_points)),
                )
                if not math.isfinite(scale) or scale <= 0:
                    raise ValueError("PDF preview scale is invalid")
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()

        try:
            if image.width * image.height > PDF_PREVIEW_MAX_PIXELS:
                image.thumbnail(
                    (PDF_PREVIEW_MAX_DIMENSION, PDF_PREVIEW_MAX_DIMENSION),
                )
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False)
            rendered = output.getvalue()
        finally:
            image.close()
        return {
            "ok": True,
            "content": rendered,
            "pageCount": page_count,
        }
    except (ImportError, OSError, RuntimeError, ValueError):
        return {
            "ok": False,
            "code": "PDF_PREVIEW_FAILED",
            "message": "PDF 页面暂时无法生成，请重试",
            "status": 422,
        }
