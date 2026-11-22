"""The HTTP side of the refusal registry: one exception type, one handler set.

Every refusal a route raises is a :class:`Refusal` that names its code
explicitly, from ``core/refusal_codes.py``. :func:`install` registers the
app-level handlers that write that code beside the unchanged ``detail``:

- any ``HTTPException`` (a :class:`Refusal`, or one the framework raises itself,
  such as the 404 for an unknown path and the 405 for a wrong method);
- FastAPI's own 422 request-validation error;
- an unhandled exception, as a JSON 500.

Only ``code`` is added. Status, ``detail`` and headers stay exactly as FastAPI's
default handlers produced them.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException

from athena.core.refusal_codes import code_for_status

#: FastAPI's own 422 body for a request that does not match the route's schema.
VALIDATION_ERROR_CODE = "validation_error"
#: The 500 an unhandled exception answers with. ``detail`` is the text
#: Starlette's plain-text 500 always carried.
INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_DETAIL = "Internal Server Error"


class Refusal(HTTPException):
    """An ``HTTPException`` that names its registered refusal code.

    ``code`` is keyword-only and required, so a call site cannot forget it;
    ``scripts/check_refusal_codes.py`` also refuses a code the registry lacks."""

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: dict[str, str] | None = None,
        *,
        code: str,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def code_of(exc: StarletteHTTPException) -> str:
    """The code an HTTP exception answers with: its own, or its status's."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else code_for_status(exc.status_code)


def _http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    headers = getattr(exc, "headers", None)
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    return JSONResponse(
        {"detail": exc.detail, "code": code_of(exc)},
        status_code=exc.status_code,
        headers=headers,
    )


def _request_validation(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "code": VALIDATION_ERROR_CODE,
        },
    )


def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's ServerErrorMiddleware calls this, then re-raises the exception
    # so it is still logged and still fails a test client; only the body changes.
    return JSONResponse(
        status_code=500,
        content={"detail": INTERNAL_ERROR_DETAIL, "code": INTERNAL_ERROR_CODE},
    )


def install(app: FastAPI) -> None:
    """Register the handlers that put a ``code`` on every JSON refusal."""
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(Exception, _unhandled)
