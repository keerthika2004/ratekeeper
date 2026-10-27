"""Turns exceptions into RFC 9457 "problem details" JSON:

    {"type": "about:blank", "title": "Not Found", "status": 404, "detail": "policy 'x' not found"}

so every error has the same shape, including FastAPI's own validation errors.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from ratekeeper.core.rules import InvalidPolicyError
from ratekeeper.errors import CostExceedsCapacityError, PolicyNotFoundError, VersionMismatchError

log = logging.getLogger(__name__)


class InvalidIfMatchError(ValueError):
    def __init__(self, header: str) -> None:
        super().__init__(f'If-Match must be a policy version such as "3", got: {header}')


def problem(status: HTTPStatus, detail: str, **extra: Any) -> JSONResponse:
    body = {"type": "about:blank", "title": status.phrase, "status": status.value, "detail": detail, **extra}
    return JSONResponse(body, status_code=status.value, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    def simple(status: HTTPStatus) -> Any:
        async def handler(request: Request, error: Exception) -> JSONResponse:
            return problem(status, str(error))

        return handler

    app.add_exception_handler(PolicyNotFoundError, simple(HTTPStatus.NOT_FOUND))
    app.add_exception_handler(InvalidIfMatchError, simple(HTTPStatus.BAD_REQUEST))
    app.add_exception_handler(VersionMismatchError, simple(HTTPStatus.PRECONDITION_FAILED))
    app.add_exception_handler(CostExceedsCapacityError, simple(HTTPStatus.UNPROCESSABLE_ENTITY))

    async def invalid_policy(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, InvalidPolicyError)
        return problem(HTTPStatus.BAD_REQUEST, "invalid policy", errors=error.errors)

    async def invalid_request(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, RequestValidationError)
        # 422 is FastAPI's convention for "the JSON doesn't match the schema".
        return problem(
            HTTPStatus.UNPROCESSABLE_ENTITY, "request validation failed", errors=jsonable_encoder(error.errors())
        )

    async def storage_unavailable(request: Request, error: Exception) -> JSONResponse:
        log.error("Storage failure", exc_info=error)
        return problem(HTTPStatus.SERVICE_UNAVAILABLE, "storage temporarily unavailable")

    app.add_exception_handler(InvalidPolicyError, invalid_policy)
    app.add_exception_handler(RequestValidationError, invalid_request)
    # Postgres unreachable (or Redis outside the check path, e.g. health checks).
    app.add_exception_handler(DBAPIError, storage_unavailable)
    app.add_exception_handler(OSError, storage_unavailable)
