"""Stable, non-sensitive HTTP error responses for the hosted application."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class ApiError(Exception):
    """An expected API failure whose code and message are safe for clients.

    Raw provider responses and caught exception text must never be passed as
    ``message``. Keep those details in redacted server-side telemetry instead.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError("API error status must be between 400 and 599")
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("API error code must be a stable snake_case identifier")
        if not message or len(message) > 300:
            raise ValueError("API error message must contain between 1 and 300 characters")
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


def request_id_for(request: Request) -> str:
    """Return the server-generated request identifier for ``request``."""

    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        request_id = str(uuid4())
        request.state.request_id = request_id
    return request_id


def error_response(request: Request, error: ApiError) -> JSONResponse:
    """Render the public error envelope without reflecting request/provider data."""

    request_id = request_id_for(request)
    headers = {
        "Cache-Control": "no-store",
        "X-Request-ID": request_id,
        **error.headers,
    }
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": request_id,
            }
        },
        headers=headers,
    )


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """FastAPI handler for deliberately raised :class:`ApiError` values."""

    return error_response(request, exc)


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a stable validation error without echoing submitted values."""

    del exc
    return error_response(
        request,
        ApiError(422, "request_invalid", "The request contains invalid or missing fields."),
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize framework HTTP errors while avoiding unsafe ``detail`` reflection."""

    defaults = {
        400: ("request_invalid", "The request could not be processed."),
        401: ("authentication_required", "A valid sign-in session is required."),
        403: ("forbidden", "You do not have permission to perform this action."),
        404: ("not_found", "The requested resource was not found."),
        405: ("method_not_allowed", "This request method is not allowed."),
        409: ("conflict", "The request conflicts with existing data."),
        413: ("request_too_large", "The request is too large."),
        429: ("rate_limited", "Too many requests. Please try again later."),
    }
    code, message = defaults.get(
        exc.status_code,
        ("http_error", "The request could not be completed."),
    )
    return error_response(
        request,
        ApiError(exc.status_code, code, message, headers=exc.headers),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort response; intentionally does not stringify ``exc``."""

    del exc
    return error_response(
        request,
        ApiError(500, "internal_error", "The service could not complete the request."),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Install request IDs and the complete safe error contract on ``app``."""

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next: Any):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        return response

    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
