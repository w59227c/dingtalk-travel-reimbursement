from __future__ import annotations

import json
import subprocess
import sys

from app.services.temp_files import validate_image_worker


def test_spawned_image_check_does_not_load_web_pdf_or_ocr_modules():
    # Spawn imports the entry point's defining module in every fresh process.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib,json,sys; "
            "importlib.import_module(sys.argv[1]); "
            "print(json.dumps(sorted(set(sys.modules) & "
            "{'fastapi','pypdf','app.ocr.engine','app.ocr.extractors','paddleocr','paddlex'})))",
            validate_image_worker.__module__,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout) == []
