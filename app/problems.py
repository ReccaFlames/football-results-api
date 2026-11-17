"""RFC 9457 problem details (`application/problem+json`) for every error response.

`type` is a stable relative URI `/problems/<slug>` (see `PROBLEM_TYPES`); `detail` is always a
human-readable string. Machine-readable hints are extension members (`parameter`, `invalid`,
`valid_values`, `hint`, `examples`, `errors`).
"""

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models import PROBLEM_TYPES

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemError(Exception):
    """Raise from a route to return a problem response of the registered ``problem_type``."""

    def __init__(
        self,
        problem_type: str,
        detail: str,
        *,
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        if problem_type not in PROBLEM_TYPES:
            raise ValueError(f"Unregistered problem type: {problem_type}")
        super().__init__(detail)
        self.problem_type = problem_type
        self.status_code = PROBLEM_TYPES[problem_type][0]
        self.detail = detail
        self.headers = dict(headers or {})
        self.extensions = extensions


def problem_body(
    problem_type: str, detail: str, instance: str, **extensions: Any
) -> dict[str, Any]:
    status, title = PROBLEM_TYPES[problem_type]
    body: dict[str, Any] = {
        "type": f"/problems/{problem_type}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
    }
    body.update({key: value for key, value in extensions.items() if value is not None})
    return body


def problem_response(
    request: Request,
    problem_type: str,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    body = problem_body(problem_type, detail, request.url.path, **extensions)
    return JSONResponse(
        body,
        status_code=body["status"],
        media_type=PROBLEM_MEDIA_TYPE,
        headers={**(headers or {}), "Cache-Control": "no-store"},
    )


def _parameter(loc: Sequence[str | int]) -> str | None:
    names = [str(item) for item in loc[1:]]
    return ".".join(names) if names else None


async def _problem_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ProblemError)
    return problem_response(
        request,
        error.problem_type,
        error.detail,
        headers=error.headers,
        **error.extensions,
    )


async def _validation_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    issues = [
        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]
    messages = [
        f"{_parameter(item['loc']) or 'body'}: {item['msg']}" if item["loc"] else item["msg"]
        for item in issues
    ]
    return problem_response(
        request,
        "validation-error",
        "; ".join(messages),
        parameter=_parameter(issues[0]["loc"]) if issues else None,
        errors=issues,
    )


async def _http_error(request: Request, error: Exception) -> JSONResponse:
    """Errors raised by the framework itself (unmatched route or method)."""
    assert isinstance(error, StarletteHTTPException)
    problem_type = {404: "not-found", 405: "method-not-allowed"}.get(error.status_code)
    detail = error.detail if isinstance(error.detail, str) else HTTPStatus(error.status_code).phrase
    if problem_type is not None:
        return problem_response(request, problem_type, detail, headers=error.headers)
    phrase = HTTPStatus(error.status_code).phrase
    return JSONResponse(
        {
            "type": "about:blank",
            "title": phrase,
            "status": error.status_code,
            "detail": detail,
            "instance": request.url.path,
        },
        status_code=error.status_code,
        media_type=PROBLEM_MEDIA_TYPE,
        headers={**(error.headers or {}), "Cache-Control": "no-store"},
    )


async def _internal_error(request: Request, error: Exception) -> JSONResponse:
    # Starlette's ServerErrorMiddleware re-raises the error afterwards, so the server logs it.
    return problem_response(request, "internal-error", "An unexpected error occurred")


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _internal_error)
