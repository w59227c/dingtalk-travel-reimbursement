from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.ocr import engine as engine_module
from app.ocr.engine import FakeOcrEngine, OcrRuntimeError, PaddleLocalOcrEngine
from app.ocr.types import OcrLine
from app.ocr.workers import verify_ocr_runtime_worker
from app.services.ocr_service import OcrService
from app.services.temp_files import StoredFile


async def test_production_startup_runs_real_ocr_runtime_smoke(
    settings_factory,
    tmp_path: Path,
) -> None:
    class RecordingRunner:
        function = None

        async def run(self, function, *_args, **_kwargs):
            self.function = function
            return {"ok": True}

    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    settings.app_env = "production"
    runner = RecordingRunner()

    await OcrService(settings, None, runner).ensure_ready()  # type: ignore[arg-type]

    assert runner.function is verify_ocr_runtime_worker


async def test_pdf_text_missing_amount_falls_back_once_to_paddle(
    settings_factory,
    tmp_path: Path,
) -> None:
    class PdfFallbackRunner:
        def __init__(self) -> None:
            self.preferences: list[bool] = []

        async def run(self, _function, *_args, timeout_seconds: float):
            del timeout_seconds
            prefer_pdf_text = bool(_args[-1])
            self.preferences.append(prefer_pdf_text)
            common = [
                ("铁路电子客票 G123", 0.98),
                ("乘车日期 2026年7月7日", 0.97),
                ("合肥南站-北京南站", 0.96),
            ]
            if prefer_pdf_text:
                return {"ok": True, "source": "pdf_text", "lines": common}
            return {
                "ok": True,
                "source": "paddle",
                "lines": [*common, ("票价 ￥473.50", 0.99)],
            }

    receipt = tmp_path / "train.pdf"
    receipt.write_bytes(b"%PDF-test-fixture")
    stored = StoredFile(
        temp_id="train-1",
        path=receipt,
        extension="pdf",
        media_type="application/pdf",
        size=receipt.stat().st_size,
        original_name="高铁1.pdf",
    )
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    runner = PdfFallbackRunner()
    parsed = await OcrService(settings, None, runner).recognize_file(  # type: ignore[arg-type]
        stored,
        reference_year=2026,
    )

    assert runner.preferences == [True, False]
    assert parsed.receipt_type == "train"
    assert str(parsed.amount) == "473.50"
    assert parsed.date.isoformat() == "2026-07-07"
    assert parsed.description == "合肥南站-北京南站"
    assert "MISSING_AMOUNT" not in parsed.warnings


@pytest.mark.asyncio
async def test_ambiguous_passenger_pdf_text_falls_back_once_to_paddle(
    settings_factory,
    tmp_path: Path,
) -> None:
    class PassengerTransportFallbackRunner:
        def __init__(self) -> None:
            self.preferences: list[bool] = []

        async def run(self, _function, *_args, timeout_seconds: float):
            del timeout_seconds
            prefer_pdf_text = bool(_args[-1])
            self.preferences.append(prefer_pdf_text)
            common = [
                ("电子发票 旅客运输服务", 0.98),
                ("开票日期 2026年07月08日", 0.97),
                ("价税合计 ￥9.20", 0.99),
            ]
            if prefer_pdf_text:
                return {"ok": True, "source": "pdf_text", "lines": common}
            return {
                "ok": True,
                "source": "paddle",
                "lines": [*common, ("交通工具类型 出租车", 0.96)],
            }

    receipt = tmp_path / "taxi.pdf"
    receipt.write_bytes(b"%PDF-test-fixture")
    stored = StoredFile(
        temp_id="taxi-1",
        path=receipt,
        extension="pdf",
        media_type="application/pdf",
        size=receipt.stat().st_size,
        original_name="打车发票.pdf",
    )
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    runner = PassengerTransportFallbackRunner()
    parsed = await OcrService(settings, None, runner).recognize_file(  # type: ignore[arg-type]
        stored,
        reference_year=2026,
    )

    assert runner.preferences == [True, False]
    assert parsed.category.value == "local_transport"
    assert "MANUAL_REVIEW_REQUIRED" not in parsed.warnings


@pytest.mark.asyncio
async def test_passenger_pdf_fallback_merges_occurrence_route_with_text_amount(
    settings_factory,
    tmp_path: Path,
) -> None:
    class PassengerFieldFallbackRunner:
        def __init__(self) -> None:
            self.preferences: list[bool] = []

        async def run(self, _function, *_args, timeout_seconds: float):
            del timeout_seconds
            prefer_pdf_text = bool(_args[-1])
            self.preferences.append(prefer_pdf_text)
            if prefer_pdf_text:
                return {
                    "ok": True,
                    "source": "pdf_text",
                    "lines": [
                        ("电子发票 旅客运输服务", 0.98),
                        ("*交通运输服务*客运服务费", 0.98),
                        ("开票日期：", 0.99),
                        ("2026年07月08日", 0.99),
                        ("价税合计（小写） ￥9.20", 0.99),
                    ],
                }
            return {
                "ok": True,
                "source": "paddle",
                "lines": [
                    ("出行人", 0.99),
                    ("有效身份证件号", 0.99),
                    ("出行日期", 0.99),
                    ("出发地", 0.99),
                    ("到达地", 0.99),
                    ("等级", 0.99),
                    ("交通工具类型", 0.99),
                    ("开票日期：2026年07月08日", 0.99),
                    ("2026-07-06", 0.99),
                    ("示例存储技术有限公司(东", 0.99),
                    ("泊寓·新桥产业园店", 0.99),
                    ("其他", 0.99),
                    ("出租车", 0.99),
                    ("门)", 0.93),
                ],
            }

    receipt = tmp_path / "taxi-fields.pdf"
    receipt.write_bytes(b"%PDF-test-fixture")
    stored = StoredFile(
        temp_id="taxi-fields-1",
        path=receipt,
        extension="pdf",
        media_type="application/pdf",
        size=receipt.stat().st_size,
        original_name="打车发票.pdf",
    )
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    runner = PassengerFieldFallbackRunner()
    parsed = await OcrService(settings, None, runner).recognize_file(  # type: ignore[arg-type]
        stored,
        reference_year=2026,
    )

    assert runner.preferences == [True, False]
    assert parsed.date.isoformat() == "2026-07-06"
    assert parsed.amount is not None and str(parsed.amount) == "9.20"
    assert parsed.description == "示例存储技术有限公司(东门)-泊寓·新桥产业园店"
    assert "INVOICE_DATE_USED_AS_OCCURRENCE" not in parsed.warnings


@pytest.mark.asyncio
async def test_unclassified_non_transport_pdf_text_does_not_use_paddle(
    settings_factory,
    tmp_path: Path,
) -> None:
    class PlainInvoiceRunner:
        def __init__(self) -> None:
            self.preferences: list[bool] = []

        async def run(self, _function, *_args, timeout_seconds: float):
            del timeout_seconds
            self.preferences.append(bool(_args[-1]))
            return {
                "ok": True,
                "source": "pdf_text",
                "lines": [
                    ("电子发票", 0.98),
                    ("开票日期 2026年07月08日", 0.97),
                    ("价税合计 ￥9.20", 0.99),
                ],
            }

    receipt = tmp_path / "plain.pdf"
    receipt.write_bytes(b"%PDF-test-fixture")
    stored = StoredFile(
        temp_id="plain-1",
        path=receipt,
        extension="pdf",
        media_type="application/pdf",
        size=receipt.stat().st_size,
        original_name="普通发票.pdf",
    )
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    runner = PlainInvoiceRunner()
    parsed = await OcrService(settings, None, runner).recognize_file(  # type: ignore[arg-type]
        stored,
        reference_year=2026,
    )

    assert runner.preferences == [True]
    assert parsed.category.value == "other"


def test_production_rejects_fake_and_missing_models(
    settings_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_engine_created = False

    def unexpected_database_engine(_database_url: str):
        nonlocal database_engine_created
        database_engine_created = True
        raise AssertionError("production fake OCR must fail before resources are created")

    monkeypatch.setattr("app.main.create_database_engine", unexpected_database_engine)
    with pytest.raises(ValueError, match="fake OCR"):
        from app.main import create_app

        create_app(
            settings_factory(
                app_env="production",
                auth_mock_enabled=False,
                ocr_enabled=False,
                session_cookie_secure=True,
            ),
            ocr_engine=FakeOcrEngine(),
        )
    assert database_engine_created is False

    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "missing-det",
        ocr_recognition_model_dir=tmp_path / "missing-rec",
    )
    with pytest.raises(OcrRuntimeError, match="模型目录不可用"):
        PaddleLocalOcrEngine(settings).ensure_ready()


def test_paddle_static_cpu_runtime_disables_mkldnn(
    monkeypatch,
    settings_factory,
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}

    class FakePaddleOcr:
        def __init__(self, **kwargs: object) -> None:
            received.update(kwargs)

    fake_module = ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOcr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    monkeypatch.setattr(engine_module, "model_directory_ready", lambda *_args: True)
    monkeypatch.setattr(
        engine_module.metadata,
        "version",
        lambda package: {"paddleocr": "3.7.0", "paddlepaddle": "3.3.1"}[package],
    )

    PaddleLocalOcrEngine(
        settings_factory(
            ocr_detection_model_dir=tmp_path / "det",
            ocr_recognition_model_dir=tmp_path / "rec",
        )
    ).ensure_ready()

    assert received["enable_mkldnn"] is False


def test_paddle_result_contract_rejects_mismatch_without_runtime(
    settings_factory,
) -> None:
    class Result:
        json = {"res": {"rec_texts": ["文字"], "rec_scores": []}}

    class Pipeline:
        def predict(self, _path: str) -> list[Result]:
            return [Result()]

    engine = PaddleLocalOcrEngine(settings_factory())
    engine._pipeline = Pipeline()
    with pytest.raises(OcrRuntimeError, match="数量不一致"):
        engine.recognize("not-opened-by-fake-pipeline.png")


def test_paddle_recovers_passenger_route_from_separate_header_guided_cell_crops(
    settings_factory,
) -> None:
    texts = [
        "出行人",
        "有效身份证件号",
        "出行日期",
        "出发地",
        "到达地",
        "等级",
        "交通工具类型",
        "测试用户",
        "2026-07-01",
        "甲方园区(北乙方酒店(南",
        "无",
        "出租车",
        "门)",
        "门)",
    ]
    boxes = [
        [50, 100, 110, 120],
        [170, 100, 300, 120],
        [390, 100, 470, 120],
        [570, 100, 630, 120],
        [790, 100, 850, 120],
        [950, 100, 1010, 120],
        [1040, 100, 1150, 120],
        [50, 122, 110, 144],
        [380, 122, 470, 144],
        [500, 122, 930, 144],
        [950, 122, 990, 144],
        [1060, 122, 1120, 144],
        [570, 146, 620, 166],
        [800, 146, 850, 166],
    ]

    class PageImage:
        shape = (1000, 1200, 3)

    class Result(dict):
        json = {
            "res": {
                "rec_texts": texts,
                "rec_scores": [0.99] * len(texts),
                "rec_boxes": boxes,
            }
        }

        def __init__(self) -> None:
            super().__init__({"doc_preprocessor_res": {"output_img": PageImage()}})

    class Pipeline:
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, value: object) -> list[Result]:
            assert value == "passenger-invoice.png"
            self.calls += 1
            return [Result()]

    pipeline = Pipeline()
    engine = PaddleLocalOcrEngine(settings_factory())
    engine._pipeline = pipeline
    crop_regions: list[tuple[int, int, int, int]] = []

    def recognize_region(
        _page_image: object,
        region: tuple[int, int, int, int],
    ) -> list[OcrLine]:
        crop_regions.append(region)
        if len(crop_regions) == 1:
            return [OcrLine("甲方园区(北", 0.98), OcrLine("门)", 0.97)]
        return [OcrLine("乙方酒店(南", 0.98), OcrLine("门)", 0.97)]

    engine._recognize_region = recognize_region  # type: ignore[attr-defined,method-assign]

    lines = engine.recognize("passenger-invoice.png")

    assert pipeline.calls == 1
    assert len(crop_regions) == 2
    assert OcrLine("行程路线：甲方园区(北门)-乙方酒店(南门)", 0.97) in lines


async def test_complete_foreign_pdf_text_does_not_repeat_ocr_for_manual_confirmation(
    settings_factory,
    tmp_path: Path,
) -> None:
    class ForeignPdfRunner:
        calls = 0

        async def run(self, _function, *_args, **_kwargs):
            self.calls += 1
            return {
                "ok": True,
                "source": "pdf_text",
                "lines": [
                    ("Hotel invoice", 0.98),
                    ("18/4/2026", 0.98),
                    ("18/6/2026", 0.98),
                    ("Total $108.00", 0.98),
                ],
            }

    runner = ForeignPdfRunner()
    settings = settings_factory(
        ocr_enabled=True,
        ocr_detection_model_dir=tmp_path / "det",
        ocr_recognition_model_dir=tmp_path / "rec",
    )
    stored = StoredFile(
        temp_id="hotel-1",
        path=tmp_path / "hotel.pdf",
        extension="pdf",
        media_type="application/pdf",
        size=10,
        original_name="hotel.pdf",
    )
    parsed = await OcrService(settings, None, runner).recognize_file(  # type: ignore[arg-type]
        stored,
        reference_year=2026,
    )
    assert runner.calls == 1
    assert str(parsed.original_amount) == "108.00"
    assert parsed.amount is None
    assert parsed.original_currency is None
    assert parsed.date is None
    assert "CURRENCY_REQUIRES_REVIEW" in parsed.warnings
    assert "MULTIPLE_RECEIPT_DATES" in parsed.warnings
