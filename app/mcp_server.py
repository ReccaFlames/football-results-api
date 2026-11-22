"""Curated MCP server: Streamable HTTP at `/mcp` (MCP 2026-07-28, stateless, JSON responses).

Each tool calls the existing HTTP routes in-process, so validation, problem types, freshness,
IDs, and cursors are exactly those of the API. API errors become tool results with
`isError: true` whose text is the RFC 9457 problem JSON, unchanged. Tool names are public:
add, never rename. With OAuth configured (ADR 0006), `McpEndpoint` also checks bearer tokens;
tools then act as the resolved identity, never with the token.
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit

import httpx
from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from mcp_types import INVALID_PARAMS
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import Match as RouteMatch
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app.access import Identity, resolve_subject
from app.agent_docs import MCP_PATH, MCP_SERVER_NAME, MCP_TOOLS
from app.config import Settings
from app.contract import MAX_LIMIT
from app.models import (
    CompetitionSummary,
    Freshness,
    JobAccepted,
    Match,
    Pagination,
    StandingRow,
    Team,
)
from app.oauth import InvalidToken, IssuerUnavailable, JwtVerifier, OAuthConfig, bearer_token
from app.problems import problem_body, problem_response

logger = logging.getLogger(__name__)

# Set by the rate-limit middleware on the `/mcp` request: the rate-limit client of the caller.
CLIENT_SCOPE_KEY = "football_results.client"
# Set on in-process API calls made by tools; the value is the outer client (or "").
INTERNAL_SCOPE_KEY = "football_results.mcp_internal"
# The caller's `Identity` (or None): set on the `/mcp` request and copied to the tools' API calls,
# which authorize with it instead of a key or token.
IDENTITY_SCOPE_KEY = "football_results.identity"
# The `McpAuth` result of the `/mcp` request, computed once.
AUTH_SCOPE_KEY = "football_results.mcp_auth"
FORM_DEFAULT = 5
FORM_MAX = 20
# Budget for all tool descriptions together (about 600 tokens); a test enforces it.
DESCRIPTION_BUDGET = 2400

INSTRUCTIONS = (
    "Football fixtures, results, league tables, and team form from this Football Results API. "
    "Start with list_competitions to get competition slugs and seasons. Unknown values are "
    "null, never 0. Results carry `freshness` per competition-season. Errors are RFC 9457 "
    "problem details (JSON in the text content) with `type`, `hint`, and `valid_values`."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
REFRESH = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


# --- Output schemas ----------------------------------------------------------------------------


def _partial(model: type[BaseModel], required: str) -> Any:
    """A TypedDict of ``model`` where only ``required`` is required (for `fields=` subsets)."""
    fields: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        annotation: Any = field.annotation
        if field.description:
            annotation = Annotated[annotation, Field(description=field.description)]
        fields[name] = annotation if name == required else NotRequired[annotation]
    item = TypedDict(f"{model.__name__}Item", fields)  # type: ignore[misc]
    item.__doc__ = f"A {model.__name__}; with `fields`, only the selected fields and `{required}`."
    return item


MatchItem = _partial(Match, "id")
StandingRowItem = _partial(StandingRow, "team_slug")


class CompetitionsOutput(BaseModel):
    competitions: list[CompetitionSummary]


class MatchesOutput(BaseModel):
    data: list[MatchItem]  # type: ignore[valid-type]
    pagination: Pagination
    freshness: list[Freshness]


class StandingsOutput(BaseModel):
    competition: str
    season: str
    stage: str | None
    data: list[StandingRowItem]  # type: ignore[valid-type]
    freshness: list[Freshness]


class FormMatch(BaseModel):
    id: int = Field(description="Stable match ID")
    competition: str
    season: str
    kickoff_at: datetime | None
    venue: Literal["home", "away"]
    opponent: str
    opponent_slug: str
    goals_for: int | None
    goals_against: int | None
    result: Literal["W", "D", "L"] | None = Field(description="null when the score is unknown")


class FormSummary(BaseModel):
    """Totals over the listed matches with a known score."""

    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    points: int = Field(description="3 per win, 1 per draw")
    form: str = Field(description="Results newest first (W, D, L)")


class TeamFormOutput(BaseModel):
    team: Team
    matches: list[FormMatch] = Field(description="Latest finished matches, newest first")
    summary: FormSummary
    freshness: list[Freshness]


# --- Transport security --------------------------------------------------------------------------

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection: localhost, the public base URL, and the configured extras."""
    hosts = [host for name in _LOCAL_HOSTS for host in (name, f"{name}:*")]
    origins = [
        origin
        for name in _LOCAL_HOSTS
        for scheme in ("http", "https")
        for origin in (f"{scheme}://{name}", f"{scheme}://{name}:*")
    ]
    if settings.public_base_url:
        parts = urlsplit(settings.public_base_url)
        hosts += [parts.netloc, f"{parts.netloc}:*"]
        origins.append(f"{parts.scheme}://{parts.netloc}")
    hosts += _csv(settings.mcp_allowed_hosts)
    origins += _csv(settings.mcp_allowed_origins)
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


# --- Tools ---------------------------------------------------------------------------------------

Competition = Annotated[str, Field(description="Competition slug from list_competitions")]
OptionalCompetition = Annotated[
    str | None, Field(description="Competition slug from list_competitions; omit for all")
]
Season = Annotated[
    str | None,
    Field(description="Season such as 2026-2027 or 2026; defaults to the current season"),
]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ok(structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=_json(structured))], structured_content=structured
    )


def _problem(body: Mapping[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=_json(body))], is_error=True)


class ApiError(Exception):
    def __init__(self, problem: Mapping[str, Any]) -> None:
        super().__init__(problem.get("detail"))
        self.problem = problem


def _outer_scope(ctx: Context) -> Mapping[str, Any]:
    request = getattr(ctx.request_context, "request", None)
    return getattr(request, "scope", None) or {}


class Api:
    """Calls the API in-process; requests are marked so the rate limiter knows the caller."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def call(
        self,
        ctx: Context,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        outer = _outer_scope(ctx)
        client = outer.get(CLIENT_SCOPE_KEY)
        identity = outer.get(IDENTITY_SCOPE_KEY)

        async def marked(scope: Scope, receive: Receive, send: Send) -> None:
            scope[INTERNAL_SCOPE_KEY] = client
            scope[IDENTITY_SCOPE_KEY] = identity
            await self.app(scope, receive, send)

        query = {key: value for key, value in (params or {}).items() if value is not None}
        transport = httpx.ASGITransport(app=marked)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp.internal") as http:
            response = await http.request(method, path, params=query, json=body)
        payload = response.json()
        if response.status_code >= 400:
            raise ApiError(payload)
        return payload


async def _tool(call: Callable[[], Awaitable[dict[str, Any]]]) -> CallToolResult:
    try:
        return _ok(await call())
    except ApiError as error:
        return _problem(error.problem)


def _form_match(item: Mapping[str, Any], slug: str) -> dict[str, Any]:
    home = item["home_team_slug"] == slug
    goals_for, goals_against = (
        (item["home_goals"], item["away_goals"])
        if home
        else (item["away_goals"], item["home_goals"])
    )
    result = None
    if goals_for is not None and goals_against is not None:
        result = "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"
    return {
        "id": item["id"],
        "competition": item["competition"],
        "season": item["season"],
        "kickoff_at": item["kickoff_at"],
        "venue": "home" if home else "away",
        "opponent": item["away_team"] if home else item["home_team"],
        "opponent_slug": item["away_team_slug"] if home else item["home_team_slug"],
        "goals_for": goals_for,
        "goals_against": goals_against,
        "result": result,
    }


def form_summary(matches: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in matches if item["result"] is not None]
    results = [item["result"] for item in scored]
    won, drawn, lost = results.count("W"), results.count("D"), results.count("L")
    return {
        "played": len(scored),
        "won": won,
        "drawn": drawn,
        "lost": lost,
        "goals_for": sum(item["goals_for"] for item in scored),
        "goals_against": sum(item["goals_against"] for item in scored),
        "points": 3 * won + drawn,
        "form": "".join(results),
    }


def _team_problem(team: str, detail: str, **extensions: Any) -> ApiError:
    return ApiError(
        problem_body(
            "invalid-parameter", detail, MCP_PATH, parameter="team", invalid=[team], **extensions
        )
    )


def resolve_team(team: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """The one team meant by ``team``: its slug, else a unique exact name or alias, else the
    only candidate; otherwise an `invalid-parameter` problem listing the candidate slugs."""
    needle = team.strip().casefold()
    for matches in (
        [item for item in candidates if item["slug"] == needle],
        [
            item
            for item in candidates
            if needle in {name.casefold() for name in (item["name"], *item["aliases"])}
        ],
        candidates,
    ):
        if len(matches) == 1:
            return matches[0]
    if not candidates:
        raise _team_problem(
            team,
            f"No team matches {team!r}",
            hint="Use a team slug from list_teams, or a shorter part of the name.",
        )
    raise _team_problem(
        team,
        f"{team!r} matches {len(candidates)} teams",
        valid_values=[item["slug"] for item in candidates],
        hint="Pass one of valid_values as team.",
    )


def create_server(app: ASGIApp) -> MCPServer:
    api = Api(app)
    server = MCPServer(
        name=MCP_SERVER_NAME,
        title="Football Results",
        version=version("football-results-api"),
        instructions=INSTRUCTIONS,
        cache_hints={"tools/list": CacheHint(ttl_ms=3_600_000, scope="public")},
    )

    @server.tool(title="List competitions", annotations=READ_ONLY)
    async def list_competitions(ctx: Context) -> Annotated[CallToolResult, CompetitionsOutput]:
        """Use first: competition slugs, the current season, stored seasons, season format, and
        scheduler state for every competition."""

        async def call() -> dict[str, Any]:
            return {"competitions": await api.call(ctx, "GET", "/api/v1/competitions")}

        return await _tool(call)

    @server.tool(title="Find matches", annotations=READ_ONLY)
    async def find_matches(
        ctx: Context,
        competition: OptionalCompetition = None,
        season: Annotated[str | None, Field(description="Season such as 2026-2027 or 2026")] = None,
        status: Annotated[
            str | None,
            Field(description="Comma-separated: finished, scheduled, inactive (default: all)"),
        ] = None,
        date_from: Annotated[
            str | None, Field(description="Earliest kickoff, inclusive: ISO date or datetime")
        ] = None,
        date_to: Annotated[
            str | None, Field(description="Latest kickoff, inclusive: ISO date or datetime")
        ] = None,
        team: Annotated[
            str | None, Field(description="Team slug, else a name or slug substring")
        ] = None,
        round: Annotated[int | None, Field(description="League round number")] = None,
        stage: Annotated[str | None, Field(description="Stage substring")] = None,
        order: Annotated[
            Literal["asc", "desc"], Field(description="Kickoff order; desc for the latest")
        ] = "asc",
        limit: Annotated[
            int,
            Field(
                description=f"Page size, 1-{MAX_LIMIT}",
                json_schema_extra={"minimum": 1, "maximum": MAX_LIMIT},
            ),
        ] = 50,
        cursor: Annotated[
            str | None, Field(description="pagination.next_cursor; repeat the same filters")
        ] = None,
        fields: Annotated[
            str | None, Field(description="Comma-separated Match fields; id is always included")
        ] = None,
    ) -> Annotated[CallToolResult, MatchesOutput]:
        """Results, fixtures, and postponed games. Filter by competition, season, status, dates,
        team, round, or stage; order=desc with a small limit gives the latest. Same contract as
        GET /api/v1/matches: {data, pagination, freshness}, paged with next_cursor."""

        params = {
            "competition": competition,
            "season": season,
            "status": status,
            "date_from": date_from,
            "date_to": date_to,
            "team": team,
            "round": round,
            "stage": stage,
            "order": order,
            "limit": limit,
            "cursor": cursor,
            "fields": fields,
        }
        return await _tool(lambda: api.call(ctx, "GET", "/api/v1/matches", params=params))

    @server.tool(title="Get standings", annotations=READ_ONLY)
    async def get_standings(
        ctx: Context,
        competition: Competition,
        season: Season = None,
        stage: Annotated[
            str | None, Field(description="Stage substring; required for cup competitions")
        ] = None,
        fields: Annotated[
            str | None,
            Field(description="Comma-separated StandingRow fields; team_slug always included"),
        ] = None,
    ) -> Annotated[CallToolResult, StandingsOutput]:
        """League table computed from finished matches: points, goals, and form (newest
        first)."""

        params = {"competition": competition, "season": season, "stage": stage, "fields": fields}
        return await _tool(lambda: api.call(ctx, "GET", "/api/v1/standings", params=params))

    @server.tool(title="Get team form", annotations=READ_ONLY)
    async def get_team_form(
        ctx: Context,
        team: Annotated[str, Field(description="Team slug or name, for example legia-warszawa")],
        competition: OptionalCompetition = None,
        season: Annotated[str | None, Field(description="Season such as 2026-2027 or 2026")] = None,
        last: Annotated[
            int,
            Field(
                description=f"Number of matches, 1-{FORM_MAX}",
                json_schema_extra={"minimum": 1, "maximum": FORM_MAX},
            ),
        ] = FORM_DEFAULT,
    ) -> Annotated[CallToolResult, TeamFormOutput]:
        """A team's latest finished matches, newest first, with W/D/L, goals, points, and a form
        string. An ambiguous name returns invalid-parameter with candidate slugs."""

        async def call() -> dict[str, Any]:
            if not 1 <= last <= FORM_MAX:
                raise ApiError(
                    problem_body(
                        "invalid-parameter",
                        f"last must be between 1 and {FORM_MAX}",
                        MCP_PATH,
                        parameter="last",
                        invalid=[str(last)],
                    )
                )
            scope = {"competition": competition, "season": season}
            teams = await api.call(
                ctx, "GET", "/api/v1/teams", params={**scope, "q": team, "limit": MAX_LIMIT}
            )
            found = resolve_team(team, teams["data"])
            page = await api.call(
                ctx,
                "GET",
                "/api/v1/matches",
                params={
                    **scope,
                    "team": found["slug"],
                    "status": "finished",
                    "order": "desc",
                    "limit": last,
                },
            )
            matches = [_form_match(item, found["slug"]) for item in page["data"]]
            return {
                "team": found,
                "matches": matches,
                "summary": form_summary(matches),
                "freshness": page["freshness"],
            }

        return await _tool(call)

    @server.tool(title="Request a refresh", annotations=REFRESH)
    async def request_refresh(
        ctx: Context,
        competition: Competition,
        season: Annotated[
            str | None, Field(description="Must be the current season; omit to use it")
        ] = None,
    ) -> Annotated[CallToolResult, JobAccepted]:
        """Queue a re-import of the current season when its data is stale. Needs an admin
        X-API-Key header or admin OAuth sign-in. Returns a job; the scheduler runs it within a
        minute."""

        body = {"competition": competition, "season": season}
        return await _tool(
            lambda: api.call(
                ctx,
                "POST",
                "/api/v1/results/refresh",
                body={key: value for key, value in body.items() if value is not None},
            )
        )

    async def reject_unknown_tools(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            if name not in MCP_TOOLS:
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")
        return await call_next(ctx)

    server.middleware.append(reject_unknown_tools)
    return server


# --- HTTP endpoint -------------------------------------------------------------------------------


@dataclass(frozen=True)
class McpAuth:
    """Who calls `/mcp`, or why the request is rejected before it reaches the MCP server."""

    identity: Identity | None
    rejection: Literal["invalid_token", "missing", "unavailable"] | None = None
    detail: str = ""


class McpEndpoint:
    """The `/mcp` ASGI endpoint: Host/Origin checks from the current settings, the optional
    bearer check, then the SDK.

    ``identify_key`` resolves an `X-API-Key`; ``identify_subject`` maps a valid token subject.
    """

    def __init__(
        self,
        server: MCPServer,
        settings: Settings,
        identify_key: Callable[[str | None], Identity | None] = lambda _: None,
        identify_subject: Callable[[str], Identity] = lambda subject: resolve_subject(subject, ()),
    ) -> None:
        self.server = server
        self.settings = settings
        self.identify_key = identify_key
        self.identify_subject = identify_subject
        self._verifiers: dict[OAuthConfig, JwtVerifier] = {}

    def verifier(self, config: OAuthConfig) -> JwtVerifier:
        if config not in self._verifiers:
            self._verifiers = {config: JwtVerifier(config)}
        return self._verifiers[config]

    async def authenticate(self, scope: Scope) -> McpAuth:
        """The caller of a `/mcp` request; computed once and kept in the scope."""
        if AUTH_SCOPE_KEY not in scope:
            scope[AUTH_SCOPE_KEY] = await self._authenticate(Headers(scope=scope))
        return scope[AUTH_SCOPE_KEY]

    async def _authenticate(self, headers: Headers) -> McpAuth:
        key_identity = self.identify_key(headers.get("x-api-key"))
        config = OAuthConfig.from_settings(self.settings)
        token = bearer_token(headers.get("authorization")) if config else None
        if config is None or token is None:
            if config is not None and config.mode == "required" and key_identity is None:
                return McpAuth(None, "missing", "This MCP server requires sign-in")
            return McpAuth(key_identity)
        if not token:
            return McpAuth(None, "invalid_token", "The bearer token is empty")
        try:
            access = await self.verifier(config).check(token)
        except InvalidToken as error:
            return McpAuth(None, "invalid_token", str(error))
        except IssuerUnavailable as error:
            logger.warning("OAuth token not checked: %s", error)
            return McpAuth(None, "unavailable", "The authorization server cannot be reached")
        # A valid API key on the same request takes precedence over the token's subject.
        if key_identity is not None or access.subject is None:
            return McpAuth(key_identity)
        return McpAuth(self.identify_subject(access.subject))

    async def _reject(self, auth: McpAuth, scope: Scope, receive: Receive, send: Send) -> None:
        config = OAuthConfig.from_settings(self.settings)
        assert config is not None
        request = Request(scope, receive)
        if auth.rejection == "unavailable":
            response = problem_response(
                request,
                "service-unavailable",
                auth.detail,
                headers={"Retry-After": "30"},
                hint="Retry later, or use an X-API-Key header.",
            )
        else:
            error = "invalid_token" if auth.rejection == "invalid_token" else None
            response = problem_response(
                request,
                "unauthorized",
                auth.detail,
                headers={"WWW-Authenticate": config.challenge(error, auth.detail)},
                hint=(
                    "Sign in again with the authorization server in resource_metadata, or send "
                    "an API key in X-API-Key."
                ),
            )
        await response(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        # A session manager runs once, so every application lifespan gets a new one.
        self.server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        async with self.server.session_manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        guard = TransportSecurityMiddleware(transport_security(self.settings))
        rejection = await guard.validate_request(Request(scope, receive))
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        auth = await self.authenticate(scope)
        if auth.rejection is not None:
            await self._reject(auth, scope, receive, send)
            return
        scope[IDENTITY_SCOPE_KEY] = auth.identity
        await self.server.session_manager.handle_request(scope, receive, send)


class McpRoute(Route):
    """A route for every method that, like FastAPI routes, records itself in `scope["route"]`."""

    def __init__(self, endpoint: McpEndpoint) -> None:
        super().__init__(MCP_PATH, endpoint)

    def matches(self, scope: Scope) -> tuple[RouteMatch, Scope]:
        match, child_scope = super().matches(scope)
        if match != RouteMatch.NONE:
            child_scope = {**child_scope, "route": self}
        return match, child_scope
