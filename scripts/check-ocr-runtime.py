#!/usr/bin/env python3
"""Verify the locked ONNX Runtime and run the real OCR service on synthetic fixtures."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
from datetime import date
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from app.core.config import Settings
from app.domain.categories import ExpenseCategory
from app.ocr.model_artifacts import model_directory_ready
from app.services.ocr_service import OcrService
from app.services.process_jobs import KillableProcessRunner
from app.services.temp_files import StoredFile
from app.services.warm_ocr_process import WarmOcrProcess

SUPPORTED_DEVELOPMENT_PLATFORM = ("Darwin", "arm64")
SUPPORTED_PRODUCTION_PLATFORM = ("Linux", "x86_64")
SUPPORTED_PRODUCTION_ALIAS = ("Linux", "amd64")


async def verify_samples(settings: Settings, fixtures: Path) -> None:
    warm = WarmOcrProcess()
    runner = KillableProcessRunner(warm)
    service = OcrService(settings, None, runner)
    try:
        taxi = fixtures / "taxi.png"
        expense = await service.recognize_file(
            StoredFile("taxi", taxi, "png", "image/png", taxi.stat().st_size, taxi.name),
            reference_year=2026,
        )
        if (
            expense.category != ExpenseCategory.LOCAL_TRANSPORT
            or expense.transport_type != "taxi"
            or expense.amount != Decimal("38.50")
            or expense.date != date(2026, 9, 4)
        ):
            raise RuntimeError("出租车合成票据识别结果不匹配")

        with TemporaryDirectory(prefix="expense-ocr-runtime-") as directory:
            scan = Path(directory) / "itinerary.pdf"
            with Image.open(fixtures / "itinerary.png") as image:
                image.save(scan, format="PDF", resolution=144)
            itinerary = await service.recognize_itinerary_file(
                StoredFile(
                    "itinerary", scan, "pdf", "application/pdf", scan.stat().st_size, scan.name
                ),
                reference_year=2026,
            )
        trips = itinerary["trips"]
        if (
            itinerary["status"] != "recognized"
            or itinerary["source"] != "paddle"
            or not itinerary["complete"]
            or not isinstance(trips, list)
            or len(trips) != 1
            or any(
                trips[0][field] != value
                for field, value in {
                    "date": "2026-09-03",
                    "amount": "12.30",
                    "origin": "测试甲站",
                    "destination": "测试乙站",
                }.items()
            )
        ):
            raise RuntimeError("扫描行程单识别结果不匹配")
    finally:
        await runner.close()
        await warm.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过正式 OCR 服务检查合成图片和扫描 PDF")
    parser.add_argument("--detection", type=Path, required=True)
    parser.add_argument("--recognition", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    arguments = parser.parse_args(argv)

    current_platform = (platform.system(), platform.machine())
    if current_platform not in {
        SUPPORTED_DEVELOPMENT_PLATFORM,
        SUPPORTED_PRODUCTION_PLATFORM,
        SUPPORTED_PRODUCTION_ALIAS,
    }:
        print(
            "OCR 依赖检查仅支持 macOS arm64 开发机或 Linux x86_64 生产机",
            file=sys.stderr,
        )
        return 2

    if not model_directory_ready(arguments.detection, "detection") or not model_directory_ready(
        arguments.recognition, "recognition"
    ):
        print("本地 OCR 模型与固定 SHA-256 manifest 不一致", file=sys.stderr)
        return 1

    if os.environ.get("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK") != "1":
        print("必须禁用 PaddleX 模型源联网检查", file=sys.stderr)
        return 2

    expected_versions = {
        "opencv-contrib-python": "4.10.0.84",
        "paddleocr": "3.7.0",
        "onnxruntime": "1.23.2",
        "pypdfium2": "5.13.0",
    }
    for package, expected in expected_versions.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            print(f"缺少已锁定依赖：{package}=={expected}", file=sys.stderr)
            return 1
        if actual != expected:
            print(
                f"依赖版本不匹配：{package} 应为 {expected}，实际为 {actual}",
                file=sys.stderr,
            )
            return 1

    try:
        asyncio.run(
            verify_samples(
                Settings(
                    _env_file=None,
                    app_env="test",
                    ocr_enabled=True,
                    ocr_fake_enabled=False,
                    ocr_detection_model_dir=arguments.detection,
                    ocr_recognition_model_dir=arguments.recognition,
                ),
                arguments.fixtures,
            )
        )
    except Exception as exc:
        print(f"ONNX Runtime 本地 OCR 检查失败：{type(exc).__name__}", file=sys.stderr)
        return 1

    print("ONNX Runtime CPU 已通过合成出租车票据与扫描行程单的字段检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
