from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import request_id_for
from app.core.runtime_events import http_event_context, record_runtime_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApiError(Exception):
    code: str
    message: str
    status_code: int = status.HTTP_400_BAD_REQUEST


def error_body(code: str, message: str, request_id: str) -> dict[str, object]:
    return {
        "success": False,
        "error": {"code": code, "message": message},
        "requestId": request_id,
    }


def error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    request.state.runtime_error_code = code
    request.state.runtime_status_code = status_code
    request_id = request_id_for(request)
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, request_id),
        headers={"X-Request-ID": request_id},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == status.HTTP_404_NOT_FOUND else "HTTP_ERROR"
        message = "请求的资源不存在" if exc.status_code == status.HTTP_404_NOT_FOUND else "请求失败"
        return error_response(request, code, message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            request,
            "VALIDATION_ERROR",
            "请求参数不正确",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_for(request)
        route = request.scope.get("route")
        context = http_event_context(getattr(route, "path", ""))
        if context is not None:
            module, operation = context
            record_runtime_event(
                getattr(request.app.state, "runtime_events", None),
                request_id,
                module,
                "internal_failure",
                "failed",
                operation=operation,
                errorCode="INTERNAL_ERROR",
                exceptionType=type(exc).__name__,
            )
        logger.error(
            "Unexpected request error",
            extra={
                "exception_type": type(exc).__name__,
                "request_id": request_id,
                "error_code": "INTERNAL_ERROR",
            },
        )
        return error_response(
            request,
            "INTERNAL_ERROR",
            "服务暂时不可用",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
