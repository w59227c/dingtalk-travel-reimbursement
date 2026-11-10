import asyncio
import contextlib
import io
import logging
import logging.config
import tempfile
from pathlib import Path

from sqlalchemy import text
from uvicorn.config import LOGGING_CONFIG
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

from app.core.config import Settings
from app.database.base import Base
from app.main import create_app


class Transport:
    def close(self):
        pass


async def main():
    capture = io.StringIO()
    with (
        tempfile.TemporaryDirectory(prefix="expense-log-review-") as root,
        contextlib.redirect_stderr(capture),
    ):
        logging.config.dictConfig(LOGGING_CONFIG)
        settings = Settings(
            _env_file=None,
            app_env="test",
            attendance_calendar_sync_enabled=False,
            database_url=f"sqlite:///{root}/test.db",
            temp_dir=Path(root) / "tmp",
            reimbursement_staging_dir=Path(root) / "staging",
            ocr_enabled=False,
            auth_mock_enabled=True,
            auth_mock_user_name="SYNTHETIC_EMPLOYEE_CANARY",
            auth_mock_user_id="synthetic-user",
            dingtalk_corp_id="synthetic-corp",
        )
        app = create_app(settings)
        Base.metadata.create_all(app.state.database_engine)
        # A deterministic synthetic database write failure, standing in for
        # disk-full/read-only/concurrent-write failures on a session INSERT.
        with app.state.database_engine.begin() as database:
            database.execute(
                text(
                    "CREATE TRIGGER reject_insert BEFORE INSERT ON sessions "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic write fault'); END"
                )
            )
        cycle = object.__new__(RequestResponseCycle)
        cycle.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/mock",
            "raw_path": b"/api/auth/mock",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 2),
        }
        cycle.logger = logging.getLogger("uvicorn.error")
        cycle.response_started = False
        cycle.transport = Transport()
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.start":
                cycle.response_started = True

        cycle.receive, cycle.send = receive, send
        await cycle.run_asgi(app)
        app.state.database_engine.dispose()
    logs = capture.getvalue()
    print(
        {
            "http_status": next(
                m["status"] for m in messages if m["type"] == "http.response.start"
            ),
            "safe_application_error_logged": "Unexpected request error" in logs,
            "uvicorn_traceback_logged": "Traceback (most recent call last)" in logs,
            "employee_canary_leaked_in_logs": "SYNTHETIC_EMPLOYEE_CANARY" in logs,
            "sqlalchemy_insert_parameters_logged": "[parameters:" in logs,
        }
    )


asyncio.run(main())
