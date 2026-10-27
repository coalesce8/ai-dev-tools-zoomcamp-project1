"""Error types and handlers that put every error in the spec's envelope.

Every error body is `{"error": {"code": "...", "message": "..."}}`.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_HTTP_CODES = {
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
}


class ApiError(Exception):
    """An error a route raises to return a given status, code and message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class NotFoundError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(404, "not_found", message)


class ConflictError(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "conflict", message)


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Invalid request"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    reason = str(first.get("msg", "Invalid value"))
    # Only the location and the reason: the submitted value is never echoed.
    return f"{location}: {reason}" if location else reason


async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return error_response(400, "validation_error", _validation_message(exc))


async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return error_response(exc.status_code, code, detail, headers=exc.headers)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return error_response(500, "internal_error", "Internal server error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
