from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from app.core.request_id import current_request_id


class JsonFormatter(logging.Formatter):
    """Small structured formatter that only emits an explicit safe field set."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", "") or current_request_id()
        if request_id:
            payload["requestId"] = request_id
        for key in ("method", "path", "status_code", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if hasattr(record, "exception_type"):
            payload["exceptionType"] = record.exception_type
        for attribute, field_name in (
            ("error_code", "errorCode"),
            ("upstream", "upstream"),
            ("upstream_api", "upstreamApi"),
            ("upstream_operation", "upstreamOperation"),
            ("upstream_http_status", "upstreamHttpStatus"),
            ("upstream_error_code", "upstreamErrorCode"),
        ):
            if hasattr(record, attribute):
                payload[field_name] = getattr(record, attribute)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # httpx logs full request URLs at INFO. DingTalk's legacy OAPI carries the
    # application access token in the query string, so those records must never
    # reach the application handler even when our own log level is DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
