#!/usr/bin/env python3
"""Bound real OCR peak RSS using only the repository's synthetic fixture."""

from __future__ import annotations

import argparse
import gc
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.config import Settings
from app.ocr.engine import PaddleLocalOcrEngine


def peak_mib() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage / (1024**2 if sys.platform == "darwin" else 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description="合成图片交替尺寸 OCR 内存回归检查")
    parser.add_argument("--detection", type=Path, required=True)
    parser.add_argument("--recognition", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--memory-profile", choices=("standard", "low"), default="low")
    parser.add_argument("--max-peak-mib", type=float, default=700)
    arguments = parser.parse_args()
    engine = PaddleLocalOcrEngine(
        Settings(
            _env_file=None,
            app_env="test",
            ocr_enabled=True,
            ocr_fake_enabled=False,
            ocr_memory_profile=arguments.memory_profile,
            ocr_detection_model_dir=arguments.detection,
            ocr_recognition_model_dir=arguments.recognition,
        )
    )
    try:
        with Image.open(arguments.fixtures / "itinerary.png") as fixture:
            base = fixture.convert("RGB")
        with base, Image.new("RGB", (1800, 2800), "white") as dense:
            with base.resize((1400, 854)) as tile:
                for y in (40, 960, 1880):
                    dense.paste(tile, (200, y))
            expected: dict[tuple[int, int], list[str]] = {}
            for iteration, image in enumerate((base, dense, base, dense), 1):
                started = time.perf_counter()
                lines, layout = engine.recognize_itinerary(np.asarray(image))
                texts = [line.text for line in lines]
                if not texts or texts != expected.setdefault(image.size, texts):
                    raise RuntimeError("合成样本连续识别结果不一致")
                print(
                    json.dumps(
                        {
                            "iteration": iteration,
                            "profile": arguments.memory_profile,
                            "pixels": image.size,
                            "line_count": len(lines),
                            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                            "peak_mib": round(peak_mib(), 1),
                        }
                    ),
                    flush=True,
                )
                del lines, layout, texts
                gc.collect()
                if peak_mib() > arguments.max_peak_mib:
                    raise RuntimeError("OCR 进程峰值 RSS 超过合成回归预算")
    except Exception as exc:
        # Never include raw OCR output, paths, or exception details in reports.
        print(f"OCR 内存检查失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
