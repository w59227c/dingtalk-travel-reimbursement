"""Explicit, bounded operational evidence; never copies log messages or request data."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from app.core.request_id import current_request_id

MODULES = {"approvals", "materials", "submission", "auth", "system"}
RESULTS = {"success", "empty", "failed", "incomplete", "info"}
EVENTS = {
    "request",
    "approval_query",
    "upstream_failure",
    "ocr_failure",
    "internal_failure",
    "collection_gap",
}
REJECTION_CODES = {
    "INSTANCE_ID_MISMATCH",
    "ORIGINATOR_MISMATCH",
    "NOT_COMPLETED",
    "NOT_AGREED",
    "DATE_FIELDS_INVALID",
    "KEYWORD_MISMATCH",
}
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_COUNTS = {
    "templates",
    "listRequests",
    "listed",
    "detailsRead",
    "evaluated",
    "excluded",
    "returned",
    "unselectable",
    "dropped",
}
_STAGES = {"catalog", "list", "details", "complete"}
OPERATIONS = {
    "travel_approvals",
    "approval_diagnosis",
    "template_options",
    "materials",
    "submission",
    "login",
    "logout",
    "session",
    "settings",
    "other",
}


@dataclass(frozen=True)
class RuntimeEvent:
    occurred_at: datetime
    module: str
    event: str
    result: str
    request_id: str | None
    details: dict[str, object]


class RuntimeRecorder(Protocol):
    def record(self, event: RuntimeEvent) -> None: ...


_recorder: ContextVar[RuntimeRecorder | None] = ContextVar("runtime_recorder", default=None)
_event_module: ContextVar[str | None] = ContextVar("runtime_event_module", default=None)


@contextmanager
def runtime_event_module(module: str) -> Iterator[None]:
    if module not in MODULES:
        raise ValueError("unknown runtime module")
    token = _event_module.set(module)
    try:
        yield
    finally:
        _event_module.reset(token)


def bind_runtime_recorder(recorder: RuntimeRecorder) -> Token:
    return _recorder.set(recorder)


def reset_runtime_recorder(token: Token) -> None:
    _recorder.reset(token)


def safe_code(value: object) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def safe_details(details: dict[str, object]) -> dict[str, object]:
    """A closed field set, used both when writing and when serving stored records."""
    result: dict[str, object] = {}
    for key in _COUNTS | {"configVersion", "httpStatus", "upstreamHttpStatus"}:
        value = details.get(key)
        if type(value) is int and 0 <= value <= 1_000_000_000:
            result[key] = value
        elif key in {"returned", "unselectable"} and key in details and value is None:
            result[key] = None
    duration = details.get("durationMs")
    if type(duration) in {int, float} and 0 <= duration <= 86_400_000:
        result["durationMs"] = round(duration, 2)
    for key in ("errorCode", "upstreamErrorCode", "upstreamOperation", "exceptionType"):
        if value := safe_code(details.get(key)):
            result[key] = value
    if isinstance(details.get("stage"), str) and details["stage"] in _STAGES:
        result["stage"] = details["stage"]
    if isinstance(details.get("operation"), str) and details["operation"] in OPERATIONS:
        result["operation"] = details["operation"]
    if isinstance(details.get("failureKind"), str) and details["failureKind"] in {
        "timeout",
        "transport",
    }:
        result["failureKind"] = details["failureKind"]
    if type(details.get("keywordPresent")) is bool:
        result["keywordPresent"] = details["keywordPresent"]
    window = details.get("queryWindow")
    if isinstance(window, dict):
        try:
            start, end = (date.fromisoformat(window[key]).isoformat() for key in ("from", "to"))
            result["queryWindow"] = {"from": start, "to": end}
        except (KeyError, TypeError, ValueError):
            pass
    exclusions = details.get("exclusions")
    if isinstance(exclusions, list):
        result["exclusions"] = [
            {"code": item["code"], "count": item["count"]}
            for item in exclusions[: len(REJECTION_CODES)]
            if isinstance(item, dict)
            and isinstance(item.get("code"), str)
            and item.get("code") in REJECTION_CODES
            and type(item.get("count")) is int
            and 0 <= item["count"] <= 1_000_000_000
        ]
    return result


def emit_runtime_event(module: str, event: str, result: str, **details: object) -> None:
    record_runtime_event(_recorder.get(), current_request_id(), module, event, result, **details)


def record_runtime_event(
    recorder: RuntimeRecorder | None,
    request_id: str | None,
    module: str,
    event: str,
    result: str,
    **details: object,
) -> None:
    if recorder is None or module not in MODULES or event not in EVENTS or result not in RESULTS:
        return
    recorder.record(
        RuntimeEvent(
            datetime.now(UTC).replace(tzinfo=None),
            module,
            event,
            result,
            safe_code(request_id),
            safe_details(details),
        )
    )


class RuntimeEventHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Inspect fixed logger/message pairs only. getMessage(), args, exception text,
        # URL paths and arbitrary extra fields never enter the event store.
        if not isinstance(record.msg, str):
            return
        if (
            record.name == "app.integrations.dingtalk.client"
            and record.msg == "DingTalk OpenAPI request failed"
        ):
            operation = safe_code(getattr(record, "upstream_operation", "")) or "unknown"
            module = (
                "materials"
                if operation.startswith("storage_")
                else "submission"
                if operation in {"workflow_instance_create", "workflow_approval_space_read"}
                else "approvals"
            )
            emit_runtime_event(
                _event_module.get() or module,
                "upstream_failure",
                "failed",
                errorCode=getattr(record, "error_code", None),
                upstreamOperation=operation,
                upstreamHttpStatus=getattr(record, "upstream_http_status", None),
                upstreamErrorCode=getattr(record, "upstream_error_code", None),
                failureKind=getattr(record, "failure_kind", None),
            )
        elif record.name == "app.services.ocr_service" and record.msg in {
            "Local OCR failed",
            "PDF text fallback OCR failed",
        }:
            emit_runtime_event(
                "materials", "ocr_failure", "failed", errorCode=getattr(record, "error_code", None)
            )
        elif record.name == "app.core.errors" and record.msg == "Unexpected request error":
            # The outer catch-all records explicitly after request context cleanup.
            return
        elif record.name.startswith("app.") and record.levelno >= logging.ERROR:
            emit_runtime_event(
                "submission" if record.name == "app.services.oa_reimbursement" else "system",
                "internal_failure",
                "failed",
                exceptionType=getattr(record, "exception_type", None),
                errorCode=getattr(record, "error_code", None),
            )


def http_event_context(route: str) -> tuple[str, str] | None:
    # Route is the registered route pattern, never request.url or its parameters.
    if route and not route.startswith("/api/"):
        route = "/api" + route
    if not route or route.startswith(("/api/admin/runtime-events", "/api/health", "/api/ready")):
        return None
    if route == "/api/oa/travel-approvals":
        module, operation = "approvals", "travel_approvals"
    elif "travel-approvals/diagnose" in route:
        module, operation = "approvals", "approval_diagnosis"
    elif route.startswith("/api/oa/"):
        module, operation = "approvals", "template_options"
    elif "submission" in route:
        module, operation = "submission", "submission"
    elif "/files" in route:
        module, operation = "materials", "materials"
    elif route.startswith("/api/auth/"):
        module, operation = "auth", "logout" if route.endswith("logout") else "login"
    elif route.startswith("/api/me"):
        module, operation = "auth", "session"
    else:
        module, operation = "system", "settings" if "/settings" in route else "other"
    return module, operation


def record_http_request(
    route: str, status: int, duration_ms: float, error_code: str | None
) -> None:
    context = http_event_context(route)
    if context is None:
        return
    module, operation = context
    emit_runtime_event(
        module,
        "request",
        "failed" if status >= 400 else "success",
        operation=operation,
        httpStatus=status,
        durationMs=duration_ms,
        errorCode=error_code,
    )
