from __future__ import annotations

import json
import logging
import re
import time
from importlib import metadata
from pathlib import Path
from threading import Lock
from typing import Any

from app.ocr.extractors import PASSENGER_ROUTE_PREFIX
from app.ocr.invoice_parties import SELLER_NAME_PREFIX, invoice_seller_name
from app.ocr.model_artifacts import model_directory_ready
from app.ocr.types import OcrEngineConfig, OcrLine

_PASSENGER_ROUTE_HEADERS = ("出发地", "到达地")
_ROUTE_CELL_FORBIDDEN_TEXT = (
    "价税合计",
    "开票日期",
    "发票号码",
    "项目名称",
    "交通工具类型",
)


class OcrRuntimeError(RuntimeError):
    """Safe OCR runtime failure; the message must not contain receipt text or paths."""


class PaddleLocalOcrEngine:
    is_fake = False

    def __init__(self, settings: OcrEngineConfig) -> None:
        self._settings = settings
        self._pipeline: Any | None = None
        self._lock = Lock()

    def _validate_models(self) -> tuple[Path, Path]:
        detection = self._settings.ocr_detection_model_dir
        recognition = self._settings.ocr_recognition_model_dir
        if detection is None or recognition is None:
            raise OcrRuntimeError("本地 OCR 模型目录未配置")
        for model_kind, directory in (("detection", detection), ("recognition", recognition)):
            if not model_directory_ready(directory, model_kind):
                raise OcrRuntimeError("本地 OCR 模型目录不可用")
        return detection, recognition

    def ensure_ready(self) -> None:
        if self._pipeline is not None:
            return
        with self._lock:
            if self._pipeline is not None:
                return
            started = time.perf_counter()
            detection, recognition = self._validate_models()
            try:
                if metadata.version("paddleocr") != "3.7.0":
                    raise OcrRuntimeError("PaddleOCR 版本与部署锁不一致")
                if metadata.version("paddlepaddle") != "3.3.1":
                    raise OcrRuntimeError("PaddlePaddle 版本与部署锁不一致")
                from paddleocr import PaddleOCR
            except metadata.PackageNotFoundError as exc:
                raise OcrRuntimeError("本地 OCR 运行依赖未安装") from exc
            except ImportError as exc:
                raise OcrRuntimeError("本地 OCR 运行依赖无法加载") from exc
            try:
                self._pipeline = PaddleOCR(
                    device="cpu",
                    engine=self._settings.ocr_engine,
                    cpu_threads=self._settings.ocr_cpu_threads,
                    # PaddleOCR/PaddleX enables oneDNN on x86 CPUs by default.
                    # PP-OCRv6 static models currently hit an unsupported PIR
                    # attribute conversion in that execution path, while the
                    # regular Paddle CPU path supports the same models.
                    enable_mkldnn=False,
                    text_detection_model_name="PP-OCRv6_small_det",
                    text_detection_model_dir=str(detection),
                    text_recognition_model_name="PP-OCRv6_small_rec",
                    text_recognition_model_dir=str(recognition),
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                )
            except Exception as exc:
                raise OcrRuntimeError("本地 OCR 初始化失败") from exc
            logging.getLogger(__name__).info(
                "OCR model initialized",
                extra={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    @staticmethod
    def _payload(result: Any) -> dict[str, Any]:
        raw = getattr(result, "json", None)
        if callable(raw):
            raw = raw()
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OcrRuntimeError("OCR 输出格式无效") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("res"), dict):
            raise OcrRuntimeError("OCR 输出格式无效")
        return raw["res"]

    @staticmethod
    def _lines(payload: dict[str, Any]) -> list[OcrLine]:
        texts = payload.get("rec_texts")
        scores = payload.get("rec_scores")
        if not isinstance(texts, list) or not isinstance(scores, list):
            raise OcrRuntimeError("OCR 输出缺少识别字段")
        if len(texts) != len(scores):
            raise OcrRuntimeError("OCR 输出字段数量不一致")
        normalized: list[OcrLine] = []
        for text, score in zip(texts, scores, strict=True):
            if not isinstance(text, str) or isinstance(score, bool):
                raise OcrRuntimeError("OCR 输出字段类型无效")
            try:
                confidence = float(score)
            except (TypeError, ValueError) as exc:
                raise OcrRuntimeError("OCR 置信度无效") from exc
            if not 0.0 <= confidence <= 1.0:
                raise OcrRuntimeError("OCR 置信度超出范围")
            if text.strip():
                normalized.append(OcrLine(text=text, confidence=confidence))
        return normalized

    @staticmethod
    def _box(value: object) -> tuple[int, int, int, int] | None:
        if not isinstance(value, list | tuple) or len(value) != 4:
            return None
        try:
            values = tuple(int(round(float(coordinate))) for coordinate in value)
        except (TypeError, ValueError, OverflowError):
            return None
        if values[0] >= values[2] or values[1] >= values[3]:
            return None
        return values

    @classmethod
    def _passenger_route_regions(
        cls,
        payload: dict[str, Any],
        page_image: object,
    ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
        """Locate route cells when a detector merged both columns into one line."""

        texts = payload.get("rec_texts")
        raw_boxes = payload.get("rec_boxes")
        shape = getattr(page_image, "shape", None)
        if (
            not isinstance(texts, list)
            or not isinstance(raw_boxes, list)
            or len(texts) != len(raw_boxes)
            or not isinstance(shape, tuple | list)
            or len(shape) < 2
        ):
            return None
        try:
            image_height, image_width = int(shape[0]), int(shape[1])
        except (TypeError, ValueError, OverflowError):
            return None
        if image_width <= 0 or image_height <= 0:
            return None

        recognized: list[tuple[str, tuple[int, int, int, int]]] = []
        for text, raw_box in zip(texts, raw_boxes, strict=True):
            box = cls._box(raw_box)
            if isinstance(text, str) and box is not None:
                recognized.append(("".join(text.split()), box))

        headers: dict[str, tuple[int, int, int, int]] = {}
        for header in _PASSENGER_ROUTE_HEADERS:
            matches = [box for text, box in recognized if text == header]
            if len(matches) != 1:
                return None
            headers[header] = matches[0]

        origin_header = headers["出发地"]
        destination_header = headers["到达地"]
        origin_center = (origin_header[0] + origin_header[2]) / 2
        destination_center = (destination_header[0] + destination_header[2]) / 2
        cell_width = destination_center - origin_center
        if cell_width < 40:
            return None

        split_x = int(round((origin_center + destination_center) / 2))
        origin_left = max(0, int(round(origin_center - cell_width / 2)))
        destination_right = min(
            image_width,
            int(round(destination_center + cell_width / 2)),
        )
        row_top = max(origin_header[3], destination_header[3]) - 2
        header_height = max(
            origin_header[3] - origin_header[1],
            destination_header[3] - destination_header[1],
        )
        row_bottom = min(image_height, row_top + header_height * 4)
        if origin_left >= split_x or split_x >= destination_right or row_top >= row_bottom:
            return None

        crossing_margin = max(2, int(round(cell_width * 0.03)))
        has_cross_column_line = any(
            box[0] < split_x - crossing_margin
            and box[2] > split_x + crossing_margin
            and box[1] < row_bottom
            and box[3] > row_top
            for text, box in recognized
            if text not in _PASSENGER_ROUTE_HEADERS
        )
        if not has_cross_column_line:
            return None
        return (
            (origin_left, row_top, split_x, row_bottom),
            (split_x, row_top, destination_right, row_bottom),
        )

    @staticmethod
    def _page_image(result: Any) -> object | None:
        getter = getattr(result, "get", None)
        if not callable(getter):
            return None
        preprocessing = getter("doc_preprocessor_res")
        if not isinstance(preprocessing, dict):
            return None
        return preprocessing.get("output_img")

    def _recognize_region(
        self,
        page_image: object,
        region: tuple[int, int, int, int],
    ) -> list[OcrLine]:
        assert self._pipeline is not None
        try:
            import cv2

            left, top, right, bottom = region
            crop = page_image[top:bottom, left:right]  # type: ignore[index]
            if getattr(crop, "size", 0) == 0:
                return []
            enlarged = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            lines: list[OcrLine] = []
            for result in self._pipeline.predict(enlarged):
                lines.extend(self._lines(self._payload(result)))
            return lines
        except MemoryError:
            raise
        except Exception as exc:
            raise OcrRuntimeError("票据路线单元格识别失败") from exc

    @staticmethod
    def _route_cell(lines: list[OcrLine]) -> tuple[str, float] | None:
        text = "".join("".join(line.text.replace("\x00", " ").split()) for line in lines)
        text = text.replace("|", "").replace("｜", "").strip()
        if (
            not 2 <= len(text) <= 100
            or not re.search(r"[\u4e00-\u9fff]", text)
            or any(forbidden in text for forbidden in _ROUTE_CELL_FORBIDDEN_TEXT)
        ):
            return None
        confidence = min(line.confidence for line in lines if line.text.strip())
        return text, confidence

    def _recover_passenger_route(
        self,
        result: Any,
        payload: dict[str, Any],
    ) -> OcrLine | None:
        page_image = self._page_image(result)
        if page_image is None:
            return None
        regions = self._passenger_route_regions(payload, page_image)
        if regions is None:
            return None
        origin = self._route_cell(self._recognize_region(page_image, regions[0]))
        destination = self._route_cell(self._recognize_region(page_image, regions[1]))
        if origin is None or destination is None or origin[0] == destination[0]:
            return None
        return OcrLine(
            text=f"{PASSENGER_ROUTE_PREFIX}{origin[0]}-{destination[0]}",
            confidence=min(origin[1], destination[1]),
        )

    def recognize(self, path: str) -> list[OcrLine]:
        self.ensure_ready()
        started = time.perf_counter()
        assert self._pipeline is not None
        try:
            results = self._pipeline.predict(path)
            normalized: list[OcrLine] = []
            for result in results:
                payload = self._payload(result)
                normalized.extend(self._lines(payload))
                seller = invoice_seller_name(self._itinerary_layout(payload))
                if seller:
                    normalized.append(OcrLine(f"{SELLER_NAME_PREFIX}{seller}", 1.0))
                try:
                    route = self._recover_passenger_route(result, payload)
                except OcrRuntimeError:
                    # Cell OCR is a best-effort repair. Preserve the primary OCR
                    # result so the user can still edit the route manually.
                    route = None
                if route is not None:
                    normalized.append(route)
            logging.getLogger(__name__).info(
                "OCR image inference completed",
                extra={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return normalized
        except OcrRuntimeError:
            raise
        except Exception as exc:
            raise OcrRuntimeError("本地 OCR 识别失败") from exc

    @classmethod
    def _itinerary_layout(cls, payload: dict[str, Any]) -> str:
        texts, boxes = payload.get("rec_texts"), payload.get("rec_boxes")
        if not isinstance(texts, list) or not isinstance(boxes, list) or len(texts) != len(boxes):
            return "\n".join(line.text for line in cls._lines(payload))
        cells = [(text, cls._box(box)) for text, box in zip(texts, boxes, strict=True)]
        positioned = [
            (text, box) for text, box in cells if isinstance(text, str) and box is not None
        ]
        if len(positioned) != len(cells) or not positioned:
            return "\n".join(line.text for line in cls._lines(payload))
        scale = max(4.0, max(box[2] for _text, box in positioned) / 500)
        rows: list[list[tuple[str, tuple[int, int, int, int]]]] = []
        for text, box in sorted(positioned, key=lambda cell: (cell[1][1], cell[1][0])):
            center = (box[1] + box[3]) / 2
            if rows:
                previous = rows[-1][0][1]
                previous_center = (previous[1] + previous[3]) / 2
                tolerance = min(box[3] - box[1], previous[3] - previous[1]) / 2
            if rows and abs(center - previous_center) < tolerance:
                rows[-1].append((text, box))
            else:
                rows.append([(text, box)])
        output = []
        for row in rows:
            line = ""
            for text, box in sorted(row, key=lambda cell: cell[1][0]):
                start = round(box[0] / scale)
                line += " " * max(2 if line else 0, start - len(line)) + text.strip()
            output.append(line)
        return "\n".join(output)

    def recognize_itinerary(self, image: object) -> tuple[list[OcrLine], str]:
        """Keep table geometry from the same prediction, without a second OCR pass."""
        self.ensure_ready()
        assert self._pipeline is not None
        started = time.perf_counter()
        try:
            lines: list[OcrLine] = []
            layouts: list[str] = []
            for result in self._pipeline.predict(image):
                payload = self._payload(result)
                lines.extend(self._lines(payload))
                layouts.append(self._itinerary_layout(payload))
            logging.getLogger(__name__).info(
                "OCR itinerary inference completed",
                extra={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return lines, "\n".join(layouts)
        except OcrRuntimeError:
            raise
        except MemoryError:
            raise
        except Exception as exc:
            raise OcrRuntimeError("本地行程单识别失败") from exc


class FakeOcrEngine:
    """Explicit test/development seam; never enabled from a production setting."""

    is_fake = True

    def __init__(self, lines_by_name: dict[str, list[OcrLine]] | None = None) -> None:
        self._lines_by_name = lines_by_name or {}

    def ensure_ready(self) -> None:
        return None

    def recognize(self, path: str) -> list[OcrLine]:
        return list(self._lines_by_name.get(Path(path).name, self._lines_by_name.get("*", [])))
