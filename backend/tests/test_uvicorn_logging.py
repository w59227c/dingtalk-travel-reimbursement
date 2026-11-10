import ast
import os
import subprocess
import sys
from pathlib import Path


def test_uvicorn_sql_failure_does_not_log_employee_data():
    backend = Path(__file__).resolve().parents[1]
    output = subprocess.check_output(
        [sys.executable, str(backend / "tests/helpers/uvicorn_logging_probe.py")],
        cwd=backend,
        env={**os.environ, "APP_ENV": "test", "OCR_ENABLED": "false", "PYTHONPATH": str(backend)},
        text=True,
    )
    result = ast.literal_eval(output.strip())
    assert result["http_status"] == 500
    assert result["safe_application_error_logged"]
    assert not result["employee_canary_leaked_in_logs"]
    assert not result["sqlalchemy_insert_parameters_logged"]
    assert not result["uvicorn_traceback_logged"]
