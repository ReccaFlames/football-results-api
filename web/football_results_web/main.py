"""The Whistleprint landing page: a thin, stateless front for the Football Results API."""

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markupsafe import Markup
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from football_results_web import api

PACKAGE = Path(__file__).parent
STATIC = PACKAGE / "static"
TEMPLATES = PACKAGE / "templates"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; style-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


# Field limits of the API's `create_access_request` body (a contract test compares them).
MAX_LENGTHS = {"name": 200, "contact_email": 320, "intended_use": 1000, "message": 2000}
CLIENT_TYPES = ("human", "agent")
# A field people never see; bots that fill every input are dropped without an API call.
HONEYPOT = "website"
NO_STORE = {"Cache-Control": "no-store"}

logger = logging.getLogger(__name__)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


@dataclass(frozen=True)
class AccessForm:
    """The access request form as entered; re-rendered unchanged (escaped) after a problem."""

    name: str = ""
    contact_email: str = ""
    intended_use: str = ""
    client_type: str = ""
    competitions: tuple[str, ...] = ()
    message: str = ""

    @classmethod
    def from_form(cls, form: Mapping[str, Any], competitions: list[Any]) -> "AccessForm":
        def text(key: str) -> str:
            value = form.get(key)
            return value if isinstance(value, str) else ""

        return cls(
            name=text("name"),
            contact_email=text("contact_email"),
            intended_use=text("intended_use"),
            client_type=text("client_type"),
            competitions=tuple(item for item in competitions if isinstance(item, str) and item),
            message=text("message"),
        )

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "contact_email": self.contact_email,
            "intended_use": self.intended_use,
            "client_type": self.client_type,
        }
        if self.competitions:
            body["competitions"] = list(self.competitions)
        if self.message.strip():
            body["message"] = self.message
        return body


@dataclass
class FormErrors:
    """Readable messages: per form field, and for the whole form."""

    fields: dict[str, str] = field(default_factory=dict)
    general: list[str] = field(default_factory=list)
    demo: bool = False


UNREACHABLE = (
    "The API cannot be reached right now, so your request was not sent. Your entries are kept "
    "below; please try again in a few minutes."
)


def _issue_message(name: str, issue: api.ValidationIssue) -> str:
    if issue.type in ("missing", "string_too_short"):
        return "This field is required."
    if issue.type == "string_too_long" and name in MAX_LENGTHS:
        return f"Use at most {MAX_LENGTHS[name]} characters."
    if name == "contact_email" and issue.type == "string_pattern_mismatch":
        return "Enter an email address, for example ada@example.com."
    if name == "client_type":
        return "Choose whether a person or an AI agent will use the key."
    return issue.msg or "This value is not accepted."


def form_errors(problem: api.Problem) -> FormErrors:
    """Messages for an API problem, chosen by its type slug, never by the status alone."""
    errors = FormErrors()
    match problem.slug:
        case "validation-error":
            for issue in problem.errors:
                name = str(issue.loc[1]) if len(issue.loc) > 1 and issue.loc[0] == "body" else ""
                if name in (*MAX_LENGTHS, "client_type", "competitions"):
                    errors.fields.setdefault(name, _issue_message(name, issue))
                else:
                    errors.general.append(issue.msg or problem.detail)
            if not problem.errors:
                errors.general.append(problem.detail)
        case "invalid-parameter" if problem.parameter == "competitions":
            errors.fields["competitions"] = (
                f"{problem.detail}. Choose from the list, or reload the page for the current one."
            )
        case "access-request-pending":
            errors.fields["contact_email"] = (
                "This email address already has a pending access request. Check it with the "
                "request ID and claim secret you received; you can send a new request once the "
                "operator has decided on it."
            )
        case "rate-limited":
            wait = (
                f"in about {math.ceil(problem.retry_after_seconds / 60)} minutes"
                if problem.retry_after_seconds
                else "later"
            )
            errors.general.append(
                f"Too many access requests were sent from your network. Please try again {wait}."
            )
        case "writes-disabled":
            errors.demo = True
        case _:
            errors.general.append(f"The API did not accept the request: {problem.detail}")
    if errors.fields and not errors.general:
        errors.general.append("Please correct the marked fields.")
    return errors


def create_app(api_url: str | None = None, clock: api.Clock | None = None) -> Starlette:
    base_url = api_url or api.api_url_from_env()
    competitions = api.CompetitionCache(base_url, clock)
    demo_mode = api.DemoModeCache(base_url, clock)
    templates = Jinja2Templates(directory=TEMPLATES)
    # Inline, so that the stylesheet can recolour the wordmark for dark mode.
    logo = Markup((STATIC / "whistleprint-logo.svg").read_text(encoding="utf-8"))

    def render(request: Request, template: str, status_code: int = 200, **context: Any) -> Response:
        return templates.TemplateResponse(
            request,
            template,
            {
                "logo": logo,
                "max_lengths": MAX_LENGTHS,
                "honeypot": HONEYPOT,
                "form": AccessForm(),
                "errors": FormErrors(),
                **context,
            },
            status_code=status_code,
        )

    def private(response: Response) -> Response:
        # Claim secrets and entered personal data must not be stored by any cache.
        response.headers.update(NO_STORE)
        return response

    async def index(request: Request) -> Response:
        return render(
            request,
            "index.html",
            competitions=await competitions.get(),
            demo=bool(await demo_mode.get()),
        )

    async def show_form(_: Request) -> Response:
        return RedirectResponse("./#request-access", status_code=303)

    async def request_access(request: Request) -> Response:
        data = await request.form(max_files=0, max_fields=100)
        form = AccessForm.from_form(data, data.getlist("competitions"))
        if data.get(HONEYPOT):
            return private(render(request, "access_requested.html", receipt=None))
        forwarded = api.forwarded_for(
            request.headers.getlist("x-forwarded-for"),
            request.client.host if request.client else None,
        )
        try:
            result = await api.create_access_request(base_url, form.payload(), forwarded)
        except api.ApiUnavailable as error:
            logger.warning("Could not send an access request to the API: %s", error)
            errors, status_code = FormErrors(general=[UNREACHABLE]), 503
        else:
            if isinstance(result, api.AccessRequestReceipt):
                return private(render(request, "access_requested.html", receipt=result))
            errors, status_code = form_errors(result), result.status
        return private(
            render(
                request,
                "request_access.html",
                status_code,
                competitions=await competitions.get(),
                demo=errors.demo,
                form=form,
                errors=errors,
            )
        )

    async def favicon(_: Request) -> Response:
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    return Starlette(
        routes=[
            Route("/", index),
            Route("/request-access", show_form, methods=["GET"]),
            Route("/request-access", request_access, methods=["POST"]),
            Route("/favicon.ico", favicon),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        middleware=[Middleware(SecurityHeaders)],
    )


app = create_app()
