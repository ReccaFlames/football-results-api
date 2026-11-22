"""Agent-facing documentation generated from the OpenAPI spec and the competition data.

Backs `/llms.txt` (llmstxt.org v2), `/agents` (HTML), `/agents.md`, `/llms-full.txt`, and the
`/problems/<slug>` pages. The Agent Skill is a static file (`SKILL_FILE`) served unchanged. Everything here is pure: callers pass the OpenAPI document, the
competitions, and the public base URL. Pages are built as `Block`s so that the Markdown and the
HTML renderings always carry the same content.
"""

import json
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Any, Literal

from app.models import PROBLEM_TYPES, CompetitionSummary
from app.problems import PROBLEM_MEDIA_TYPE
from app.ratelimit import is_trusted_proxy

MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
HTML_MEDIA_TYPE = "text/html; charset=utf-8"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")
REQUEST_DERIVED_VARY = "Host, X-Forwarded-Host, X-Forwarded-Proto"

# Meaning and fix for every registered problem type; a test keeps this in sync with PROBLEM_TYPES.
PROBLEM_GUIDANCE: dict[str, tuple[str, str]] = {
    "invalid-cursor": (
        "The `cursor` is malformed, or it belongs to a request with other filters or order.",
        (
            "Pass `pagination.next_cursor` unchanged with the same filters, or omit `cursor` to "
            "start again."
        ),
    ),
    "unauthorized": (
        "The operation needs an API key, and `X-API-Key` is missing or wrong.",
        "Send a key issued by the operator in the `X-API-Key` header. Read operations need no key.",
    ),
    "unknown-competition": (
        "No competition has this slug.",
        "Use a slug from `valid_values` or from `list_competitions`.",
    ),
    "unknown-season": (
        "The competition has no such season.",
        (
            "Use a season from `valid_values`; `list_competitions` lists `available_seasons`. "
            "Seasons are `2026-2027` (split) or `2026` (calendar), depending on `season_format`."
        ),
    ),
    "unknown-job": (
        "No import job has this ID.",
        "Use the `status_url` returned by `request_refresh` or `fetch_historical_season`.",
    ),
    "not-found": (
        "No route matches the request path.",
        "Check the path against `/openapi.json` or `/llms.txt`.",
    ),
    "method-not-allowed": (
        "The path exists, but not for this HTTP method.",
        "Use the method documented in `/openapi.json`.",
    ),
    "job-conflict": (
        "An import job for this competition-season is already active with another source URL.",
        "Poll the active job with `get_job` until it finishes, then retry.",
    ),
    "invalid-parameter": (
        (
            "A parameter has an unsupported value: an unknown `fields` entry or `status`, a date "
            "that is not ISO 8601, a cup table without `stage`, or a competition without a current "
            "season."
        ),
        "Correct `parameter` using `valid_values` or `examples`.",
    ),
    "validation-error": (
        (
            "The request failed schema validation: a wrong type, a value out of range, a pattern "
            "mismatch, or a missing required field."
        ),
        "Fix every entry of `errors`; `loc` names the parameter and `msg` says what is wrong.",
    ),
    "invalid-request": (
        (
            "The request is well-formed but not allowed for this competition or season, for "
            "example a refresh of a season that is not current, or an unsupported `source_url`."
        ),
        (
            "Follow `hint`: use `request_refresh` for the current season and "
            "`fetch_historical_season` for other seasons."
        ),
    ),
    "rate-limited": (
        "This client sent too many requests in the current window.",
        (
            "Wait `Retry-After` seconds. Revalidate with `If-None-Match` and use "
            "`freshness[].next_update_at` instead of polling."
        ),
    ),
    "internal-error": (
        "An unexpected server error occurred.",
        "Retry later; report it to the operator if it persists.",
    ),
    "writes-disabled": (
        (
            "The operator has configured no API keys, so operations that need one (writes, jobs, "
            "admin, and `/metrics`) are switched off."
        ),
        (
            "A client cannot fix this; the operator must set `FOOTBALL_RESULTS_API_KEYS`. Read "
            "operations still work."
        ),
    ),
    "service-unavailable": (
        "A dependency such as the database is unavailable.",
        "Retry later with backoff.",
    ),
}

# The `football-results` CLI (`cli/` in the repository). It cannot be imported here (the API does
# not depend on it), so tests check these names against the CLI itself. All are public.
CLI_NAME = "football-results"
CLI_COMMANDS: dict[str, str] = {
    "get_health": "health",
    "list_competitions": "competitions",
    "list_matches": "matches",
    "get_standings": "standings",
    "list_teams": "teams",
    "request_refresh": "refresh",
    "fetch_historical_season": "fetch-season",
    "get_job": "status",
    "list_scrape_runs": "scrape-runs",
    "get_scheduler_status": "scheduler",
}
CLI_EXIT_CODES: tuple[tuple[int, str], ...] = (
    (0, "Success"),
    (2, "Usage error: invalid option or parameter (HTTP 400, 422, or a local check)"),
    (3, "Not found: unknown competition, season, or job (HTTP 404)"),
    (
        4,
        (
            "Upstream or stale: server error, job conflict, rate limit after retries, network "
            "failure, timeout, failed job, or stale data with `--fail-if-stale`"
        ),
    ),
    (5, "Auth: missing or invalid API key (HTTP 401/403), or writes disabled"),
)
CLI_PROBLEM_PREFIX = "urn:football-results:cli:"
# Problems raised by the CLI itself (never by the server): slug -> (exit code, meaning).
CLI_PROBLEM_GUIDANCE: dict[str, tuple[int, str]] = {
    "usage-error": (2, "An unknown command or option, or an invalid option value."),
    "connection-failed": (4, "The API is unreachable; check `FOOTBALL_RESULTS_URL`."),
    "timeout": (4, "The API did not answer within `--timeout` seconds."),
    "invalid-response": (4, "A redirect, or a non-JSON or non-problem response (wrong URL?)."),
    "stale-data": (4, "With `--fail-if-stale`: a season was never fetched or is overdue."),
    "job-failed": (4, "With `--wait`: the job failed or was cancelled."),
    "wait-timeout": (4, "With `--wait`: the job did not finish within `--wait-timeout`."),
}
CLI_INSTALL = (
    "uv tool install ./cli        # from a checkout of the repository (or: pipx install ./cli)\n"
    "uvx --from ./cli football-results --help   # run once without installing"
)

# The consumer Agent Skill (agentskills.io format) teaching the CLI. It is a static file served
# byte for byte at `SKILL_PATH`; tests check its commands, exit codes, and links.
SKILL_NAME = "football-results"
SKILL_PATH = f"/skills/{SKILL_NAME}/SKILL.md"
SKILL_FILE = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / "SKILL.md"

# The curated MCP server (`app/mcp_server.py`): tool name -> (API operation, summary). Tool names
# are public: add, never rename. A test checks them against the server's `tools/list`.
MCP_SERVER_NAME = "football-results"
MCP_PATH = "/mcp"
MCP_TOOLS: dict[str, tuple[str, str]] = {
    "list_competitions": ("list_competitions", "Competition slugs, seasons, and state"),
    "find_matches": ("list_matches", "Results and fixtures with every `list_matches` filter"),
    "get_standings": ("get_standings", "League table with points, goals, and form"),
    "get_team_form": (
        "list_teams + list_matches",
        "A team's latest finished matches with W/D/L, goals, and points",
    ),
    "request_refresh": (
        "request_refresh",
        "Queue a current-season refresh (needs the `X-API-Key` header)",
    ),
}


# --- Document blocks ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    anchor: str | None = None


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class Quote:
    text: str


@dataclass(frozen=True)
class Code:
    text: str
    language: str = "sh"


@dataclass(frozen=True)
class Bullets:
    items: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


Block = Heading | Paragraph | Quote | Code | Bullets | Table


def render_markdown(blocks: Iterable[Block]) -> str:
    return "\n\n".join(_markdown_block(block) for block in blocks) + "\n"


def _markdown_block(block: Block) -> str:
    match block:
        case Heading(level, text, _):
            return f"{'#' * level} {text}"
        case Paragraph(text):
            return text
        case Quote(text):
            return f"> {text}"
        case Code(text, language):
            return f"```{language}\n{text}\n```"
        case Bullets(items):
            return "\n".join(f"- {item}" for item in items)
        case Table(headers, rows):
            lines = [_markdown_row(headers), _markdown_row(tuple("---" for _ in headers))]
            lines.extend(_markdown_row(row) for row in rows)
            return "\n".join(lines)


def _markdown_row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |"


_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def inline_html(text: str) -> str:
    """Render the inline Markdown subset used in blocks: code spans, links, and bold."""
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN.finditer(text):
        parts.append(_prose_html(text[position : match.start()]))
        parts.append(f"<code>{escape(match.group(1))}</code>")
        position = match.end()
    parts.append(_prose_html(text[position:]))
    return "".join(parts)


def _prose_html(text: str) -> str:
    html = escape(text)
    html = _LINK.sub(lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>', html)
    return _BOLD.sub(r"<strong>\1</strong>", html)


def render_html(blocks: Iterable[Block], *, title: str, links: Mapping[str, str]) -> str:
    """A standalone HTML page; ``links`` maps `<link>` relations to absolute URLs."""
    head_links = "".join(
        f'<link rel="{relation}"{_link_type(relation)} href="{escape(url)}">'
        for relation, url in links.items()
    )
    body = "\n".join(_html_block(block) for block in blocks)
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n{head_links}\n<style>{_STYLE}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def _link_type(relation: str) -> str:
    return ' type="text/markdown"' if relation == "alternate" else ""


_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:64rem;margin:2rem auto;padding:0 1rem;"
    "line-height:1.5}pre{background:#f4f4f4;padding:.75rem;overflow-x:auto}"
    "code{font-family:ui-monospace,monospace}table{border-collapse:collapse;margin:1rem 0}"
    "th,td{border:1px solid #ccc;padding:.25rem .5rem;text-align:left;vertical-align:top}"
    "blockquote{margin-left:0;padding-left:1rem;border-left:4px solid #ccc}"
)


def _html_block(block: Block) -> str:
    match block:
        case Heading(level, text, anchor):
            identifier = f' id="{escape(anchor)}"' if anchor else ""
            return f"<h{level}{identifier}>{inline_html(text)}</h{level}>"
        case Paragraph(text):
            return f"<p>{inline_html(text)}</p>"
        case Quote(text):
            return f"<blockquote><p>{inline_html(text)}</p></blockquote>"
        case Code(text, language):
            return f'<pre><code class="language-{language}">{escape(text)}</code></pre>'
        case Bullets(items):
            return "<ul>" + "".join(f"<li>{inline_html(item)}</li>" for item in items) + "</ul>"
        case Table(headers, rows):
            head = "".join(f"<th>{inline_html(cell)}</th>" for cell in headers)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline_html(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# --- OpenAPI extraction --------------------------------------------------------------------


@dataclass(frozen=True)
class Parameter:
    name: str
    location: str
    required: bool
    description: str
    example: str | None = None
    default: str | None = None
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    tag: str
    summary: str
    description: str
    deprecated: bool
    requires_api_key: bool
    parameters: tuple[Parameter, ...]
    problem_types: tuple[str, ...]

    def parameter(self, name: str) -> Parameter | None:
        return next((item for item in self.parameters if item.name == name), None)


def operations_from_openapi(spec: Mapping[str, Any]) -> tuple[Operation, ...]:
    schemas = spec.get("components", {}).get("schemas", {})
    operations = []
    for path, item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            parameters = [_parameter(value) for value in operation.get("parameters", [])]
            parameters.extend(_body_parameters(operation.get("requestBody"), schemas))
            operations.append(
                Operation(
                    operation_id=operation["operationId"],
                    method=method.upper(),
                    path=path,
                    tag=(operation.get("tags") or [""])[0],
                    summary=operation.get("summary", ""),
                    description=operation.get("description", ""),
                    deprecated=bool(operation.get("deprecated")),
                    requires_api_key=bool(operation.get("security")),
                    parameters=tuple(parameters),
                    problem_types=_problem_types(operation.get("responses", {})),
                )
            )
    return tuple(operations)


def _parameter(value: Mapping[str, Any]) -> Parameter:
    schema = value.get("schema", {})
    return Parameter(
        name=value["name"],
        location=value["in"],
        required=bool(value.get("required")),
        description=value.get("description") or schema.get("description", ""),
        example=_example(value, schema),
        default=_text(schema.get("default")),
        values=_enum(schema),
    )


def _body_parameters(body: Mapping[str, Any] | None, schemas: Mapping[str, Any]) -> list[Parameter]:
    if not body:
        return []
    schema = body.get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
    required = set(schema.get("required", []))
    return [
        Parameter(
            name=name,
            location="body",
            required=name in required,
            description=field.get("description", ""),
            example=_example(field, field),
            default=_text(field.get("default")),
            values=_enum(field),
        )
        for name, field in schema.get("properties", {}).items()
    ]


def _example(value: Mapping[str, Any], schema: Mapping[str, Any]) -> str | None:
    for source in (value, schema):
        if "example" in source:
            return _text(source["example"])
        if source.get("examples"):
            examples = source["examples"]
            if isinstance(examples, list):
                return _text(examples[0])
    return None


def _enum(schema: Mapping[str, Any]) -> tuple[str, ...]:
    for candidate in (schema, *schema.get("anyOf", [])):
        if "enum" in candidate:
            return tuple(str(item) for item in candidate["enum"])
    return ()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _problem_types(responses: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    for status in sorted(responses):
        content = responses[status].get("content", {}).get(PROBLEM_MEDIA_TYPE, {})
        for example in content.get("examples", {}).values():
            slug = example.get("value", {}).get("type", "").removeprefix("/problems/")
            if slug in PROBLEM_TYPES and slug not in found:
                found.append(slug)
    return tuple(found)


# --- Guide ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    title: str
    purpose: str
    operation: Operation
    query: tuple[tuple[str, str], ...] = ()

    def url(self, base_url: str) -> str:
        query = "&".join(f"{name}={value}" for name, value in self.query)
        return f"{base_url}{self.operation.path}" + (f"?{query}" if query else "")

    def curl(self, base_url: str) -> str:
        return f'curl -s "{self.url(base_url)}"'

    def cli(self) -> str:
        """The same request as a `football-results` command (options are the parameter names)."""
        words = [CLI_NAME, CLI_COMMANDS[self.operation.operation_id]]
        for name, value in self.query:
            quoted = f'"{value}"' if "$(" in value else shlex.quote(value)
            words.append(f"--{name.replace('_', '-')} {quoted}")
        return " ".join(words)


@dataclass(frozen=True)
class ToolLink:
    """A client tool (CLI, Agent Skill, or MCP server) to advertise once it exists."""

    kind: Literal["cli", "skill", "mcp"]
    name: str
    url: str
    description: str


def cli_tool(base_url: str) -> ToolLink:
    return ToolLink(
        "cli",
        f"{CLI_NAME} CLI",
        f"{base_url.rstrip('/')}/agents#cli",
        (
            "command-line client: JSON when piped, tables on a terminal, stable exit codes, "
            "and errors as JSON on stderr; installation, commands, and exit codes"
        ),
    )


def skill_tool(base_url: str) -> ToolLink:
    return ToolLink(
        "skill",
        f"{SKILL_NAME} Agent Skill",
        f"{base_url.rstrip('/')}{SKILL_PATH}",
        (
            "SKILL.md teaching coding agents the CLI: when to use it, recipes, nulls, freshness, "
            "and exit codes; install it with `copilot skill add <this URL>`"
        ),
    )


def mcp_tool(base_url: str) -> ToolLink:
    return ToolLink(
        "mcp",
        f"{MCP_SERVER_NAME} MCP server",
        f"{base_url.rstrip('/')}/agents#mcp",
        (
            f"Streamable HTTP endpoint `{base_url.rstrip('/')}{MCP_PATH}` with "
            f"{len(MCP_TOOLS)} curated tools for MCP clients without a shell; tools, "
            "authentication, and client setup"
        ),
    )


@dataclass(frozen=True)
class RateLimits:
    enabled: bool
    read_per_minute: int
    write_per_minute: int


@dataclass(frozen=True)
class AgentGuide:
    title: str
    version: str
    summary: str
    description: str
    base_url: str
    tags: tuple[tuple[str, str], ...]
    operations: tuple[Operation, ...]
    competitions: tuple[CompetitionSummary, ...]
    competitions_live: bool
    tasks: tuple[Task, ...]
    rate_limits: RateLimits
    cache_max_age_seconds: int
    tools: tuple[ToolLink, ...] = ()
    attributions: tuple[str, ...] = ()

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def operation(self, operation_id: str) -> Operation:
        return next(item for item in self.operations if item.operation_id == operation_id)

    def has_tool(self, kind: str) -> bool:
        return any(item.kind == kind for item in self.tools)


def build_guide(
    spec: Mapping[str, Any],
    competitions: Sequence[CompetitionSummary],
    *,
    base_url: str,
    competitions_live: bool,
    rate_limits: RateLimits,
    cache_max_age_seconds: int,
    tools: Sequence[ToolLink] = (),
    provider_competitions: Mapping[str, Sequence[str]] | None = None,
) -> AgentGuide:
    info = spec.get("info", {})
    description = info.get("description", "").strip()
    operations = operations_from_openapi(spec)
    return AgentGuide(
        title=info.get("title", "API"),
        version=info.get("version", ""),
        summary=" ".join(description.split("\n\n", 1)[0].split()),
        description=description,
        base_url=base_url.rstrip("/"),
        tags=tuple((tag["name"], tag.get("description", "")) for tag in spec.get("tags", [])),
        operations=operations,
        competitions=tuple(competitions),
        competitions_live=competitions_live,
        tasks=top_tasks(operations, competitions),
        rate_limits=rate_limits,
        cache_max_age_seconds=cache_max_age_seconds,
        tools=tuple(tools),
        attributions=data_attributions(provider_competitions or {}),
    )


# Data licences that require attribution, by provider name (ADR 0004).
PROVIDER_ATTRIBUTIONS = {
    "openligadb": (
        "data from [OpenLigaDB](https://www.openligadb.de), licensed under the "
        "[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/). Keep "
        "this attribution when you publish the data; a database derived from it is subject to "
        "the ODbL's share-alike terms."
    ),
}


def data_attributions(provider_competitions: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """Attribution notes for the configured providers whose data licence requires one."""
    notes = []
    for provider, text in PROVIDER_ATTRIBUTIONS.items():
        slugs = sorted(set(provider_competitions.get(provider, ())))
        if slugs:
            notes.append(", ".join(f"`{slug}`" for slug in slugs) + f": {text}")
    return tuple(notes)


def _attribution_blocks(guide: AgentGuide) -> list[Block]:
    if not guide.attributions:
        return []
    return [Paragraph("Data attribution:"), Bullets(guide.attributions)]


def top_tasks(
    operations: Sequence[Operation], competitions: Sequence[CompetitionSummary]
) -> tuple[Task, ...]:
    """The five most common tasks; raises ``ValueError`` if one uses an undocumented parameter."""
    by_id = {item.operation_id: item for item in operations}
    matches = by_id["list_matches"]
    competition = _example_competition(competitions, matches)
    team = (matches.parameter("team") or Parameter("team", "query", False, "")).example
    team = team or "legia-warszawa"
    tasks = (
        Task(
            "Discover competitions and seasons",
            "Find competition slugs, each current season, and which seasons are stored.",
            by_id["list_competitions"],
        ),
        Task(
            "Resolve a team slug",
            "Turn a team name into the canonical `slug` used by the `team` filter.",
            by_id["list_teams"],
            (("competition", competition), ("q", team.split("-")[0])),
        ),
        Task(
            "Get a team's latest results",
            "Newest finished matches first; use a `slug` from the previous task as `team`.",
            matches,
            (
                ("competition", competition),
                ("team", team),
                ("status", "finished"),
                ("order", "desc"),
                ("limit", "5"),
                ("fields", "kickoff_at,home_team,away_team,home_goals,away_goals"),
            ),
        ),
        Task(
            "List upcoming fixtures",
            "Scheduled matches from today (UTC), soonest first.",
            matches,
            (
                ("competition", competition),
                ("status", "scheduled"),
                ("date_from", "$(date -u +%F)"),
                ("limit", "10"),
                ("fields", "kickoff_at,round,home_team,away_team"),
            ),
        ),
        Task(
            "Get the league table",
            "Positions, points, and form for the current season (pass `season` for another).",
            by_id["get_standings"],
            (("competition", competition), ("fields", "position,team_name,played,points,form")),
        ),
    )
    for task in tasks:
        for name, _ in task.query:
            if task.operation.parameter(name) is None:
                raise ValueError(f"{task.operation.operation_id} has no parameter {name!r}")
    return tasks


def _example_competition(competitions: Sequence[CompetitionSummary], matches: Operation) -> str:
    leagues = [item for item in competitions if item.format == "league" and item.current_season]
    if leagues or competitions:
        return (leagues or list(competitions))[0].slug
    parameter = matches.parameter("competition")
    return (parameter.example if parameter else None) or "ekstraklasa"


# --- Base URL ------------------------------------------------------------------------------

_HOST = re.compile(r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")


def request_base_url(
    *,
    scheme: str,
    host: str | None,
    root_path: str,
    peer: str | None,
    forwarded_proto: str | None,
    forwarded_host: str | None,
    trusted_proxies: Sequence[IPv4Network | IPv6Network],
) -> str:
    """The public base URL derived from a request (used when no base URL is configured).

    ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` count only when the peer is a trusted proxy;
    a host that is not a plain host name or IP address falls back to ``localhost``.
    """
    if peer is not None and trusted_proxies and is_trusted_proxy(peer, trusted_proxies):
        proto = _first(forwarded_proto)
        scheme = proto.lower() if proto and proto.lower() in {"http", "https"} else scheme
        host = _first(forwarded_host) or host
    if host is None or _HOST.fullmatch(host) is None:
        host = "localhost"
    return f"{scheme}://{host}{root_path.rstrip('/')}"


def _first(header: str | None) -> str | None:
    if not header:
        return None
    return header.split(",", 1)[0].strip() or None


def link_header(base_url: str, *, alternate: str | None = None) -> str:
    links = []
    if alternate is not None:
        links.append(f'<{base_url}{alternate}>; rel="alternate"; type="text/markdown"')
    links.append(f'<{base_url}/llms.txt>; rel="describedby"')
    return ", ".join(links)


# --- Pages ---------------------------------------------------------------------------------


def llms_txt(guide: AgentGuide) -> str:
    """`/llms.txt` per llmstxt.org v2: H1, summary blockquote, details, then H2 link lists."""
    blocks: list[Block] = [
        Heading(1, guide.title),
        Quote(guide.summary),
        Paragraph(
            f"Base URL: `{guide.base_url}` (API version {guide.version}). JSON over HTTP; "
            f"the contract is the [OpenAPI spec]({guide.url('/openapi.json')}), and "
            "operation IDs such as `list_matches` are stable names."
        ),
        Paragraph(_auth_text(guide)),
        Paragraph("Competitions on this instance:"),
        Bullets(_competition_lines(guide)),
        *_attribution_blocks(guide),
        Paragraph(_tasks_intro(guide)),
    ]
    for number, task in enumerate(guide.tasks, start=1):
        blocks.append(
            Paragraph(
                f"Task {number}: **{task.title}** (`{task.operation.operation_id}`). {task.purpose}"
            )
        )
        blocks.extend(_task_examples(guide, task))
    blocks.append(Paragraph("Rules:"))
    blocks.append(Bullets(_rules(guide)))
    blocks.extend(
        [
            Heading(2, "API reference"),
            Bullets(
                (
                    (
                        f"[Agent guide]({guide.url('/agents.md')}): every operation with its "
                        "parameters, auth, and errors, plus the conventions"
                    ),
                    (
                        f"[Full reference]({guide.url('/llms-full.txt')}): the agent guide and "
                        "every problem type in one file"
                    ),
                    (
                        f"[OpenAPI spec]({guide.url('/openapi.json')}): the machine-readable "
                        "contract (OpenAPI 3.1)"
                    ),
                )
            ),
            Heading(2, "Optional"),
            Bullets(
                (
                    (
                        f"[Problem types]({guide.url('/problems')}): the meaning and fix of every "
                        "error `type`"
                    ),
                    f"[Agent guide (HTML)]({guide.url('/agents')}): the agent guide for people",
                    f"[Swagger UI]({guide.url('/docs')}): interactive API documentation",
                )
            ),
        ]
    )
    blocks[-2:-2] = _tools_blocks(guide)
    return render_markdown(blocks)


def agent_guide_blocks(guide: AgentGuide) -> list[Block]:
    """The `/agents` and `/agents.md` page."""
    blocks: list[Block] = [
        Heading(1, f"{guide.title}: agent guide"),
        Quote(guide.summary),
        Paragraph(
            f"Version {guide.version}. Base URL: `{guide.base_url}`. "
            f"[OpenAPI spec]({guide.url('/openapi.json')}) · [llms.txt]({guide.url('/llms.txt')})"
            f" · [Full reference]({guide.url('/llms-full.txt')})"
        ),
        Heading(2, "Quick start"),
        Paragraph(_tasks_intro(guide)),
    ]
    for task in guide.tasks:
        blocks.extend(
            [
                Heading(3, task.title),
                Paragraph(f"{task.purpose} Operation: `{task.operation.operation_id}`."),
                *_task_examples(guide, task),
            ]
        )
    blocks.extend([Heading(2, "Competitions")])
    if not guide.competitions_live:
        blocks.append(
            Paragraph(
                "**The database is unavailable**, so this list comes from the configuration "
                "file; current seasons may lag, and stored seasons are unknown."
            )
        )
    blocks.append(
        Table(
            ("Slug", "Name", "Country", "Format", "Season format", "Current season", "Stored"),
            tuple(
                (
                    f"`{item.slug}`",
                    item.name,
                    item.country or "",
                    item.format,
                    item.season_format,
                    item.current_season or "none",
                    ", ".join(item.fetched_seasons) or "none",
                )
                for item in guide.competitions
            ),
        )
    )
    blocks.extend(_attribution_blocks(guide))
    blocks.extend(
        [
            Heading(2, "Authentication"),
            Paragraph(_auth_text(guide)),
            Heading(2, "Conventions"),
            Bullets(_rules(guide) + _conventions(guide)),
            Heading(2, "Operations"),
        ]
    )
    for tag, tag_description in guide.tags:
        operations = [item for item in guide.operations if item.tag == tag]
        if not operations:
            continue
        blocks.extend([Heading(3, tag), Paragraph(tag_description)])
        for operation in operations:
            blocks.extend(_operation_blocks(guide, operation))
    blocks.extend(
        [
            Heading(2, "Problem types"),
            Paragraph(
                "Errors are RFC 9457 problem details (`application/problem+json`) with `type`, "
                "`title`, `status`, `detail`, and `instance`, plus `parameter`, `invalid`, "
                "`valid_values`, `hint`, `examples`, or `errors` when relevant."
            ),
            Table(
                ("Type", "Status", "Meaning"),
                tuple(
                    (
                        f"[/problems/{slug}]({guide.url(f'/problems/{slug}')})",
                        str(status),
                        PROBLEM_GUIDANCE[slug][0],
                    )
                    for slug, (status, _) in PROBLEM_TYPES.items()
                ),
            ),
        ]
    )
    blocks.extend(_cli_blocks(guide))
    blocks.extend(_skill_blocks(guide))
    blocks.extend(_mcp_blocks(guide))
    blocks.extend(_tools_blocks(guide))
    return blocks


def agent_guide_markdown(guide: AgentGuide) -> str:
    return render_markdown(agent_guide_blocks(guide))


def agent_guide_html(guide: AgentGuide) -> str:
    return render_html(
        agent_guide_blocks(guide),
        title=f"{guide.title}: agent guide",
        links={"alternate": guide.url("/agents.md"), "describedby": guide.url("/llms.txt")},
    )


def llms_full_txt(guide: AgentGuide) -> str:
    blocks = agent_guide_blocks(guide)
    blocks.append(Heading(2, "Problem type details"))
    for slug in PROBLEM_TYPES:
        blocks.extend(_problem_detail_blocks(guide, slug, level=3))
    return render_markdown(blocks)


def problem_index_markdown(guide: AgentGuide) -> str:
    return render_markdown(
        [
            Heading(1, "Problem types"),
            Quote(
                f"Every error of {guide.title} is `application/problem+json` (RFC 9457); its "
                "`type` is one of these URIs. Branch on `type`, never on `detail`."
            ),
            Bullets(
                tuple(
                    f"[/problems/{slug}]({guide.url(f'/problems/{slug}')}) ({status}): {title}"
                    for slug, (status, title) in PROBLEM_TYPES.items()
                )
            ),
        ]
    )


def problem_markdown(guide: AgentGuide, slug: str) -> str:
    return render_markdown(_problem_detail_blocks(guide, slug, level=1))


def _problem_detail_blocks(guide: AgentGuide, slug: str, *, level: int) -> list[Block]:
    status, title = PROBLEM_TYPES[slug]
    meaning, fix = PROBLEM_GUIDANCE[slug]
    operations = [item.operation_id for item in guide.operations if slug in item.problem_types]
    returned_by = (
        "Documented for " + ", ".join(f"`{item}`" for item in operations) + "."
        if operations
        else "Not tied to one operation; any request can receive it."
    )
    return [
        Heading(level, f"{title} ({slug})"),
        Quote(f"`type`: `/problems/{slug}` · HTTP status {status}"),
        Paragraph(meaning),
        Paragraph(f"**How to fix:** {fix}"),
        Paragraph(returned_by),
    ]


def _operation_blocks(guide: AgentGuide, operation: Operation) -> list[Block]:
    flags = ["requires `X-API-Key`" if operation.requires_api_key else "no auth"]
    if operation.deprecated:
        flags.append("**deprecated**")
    command = CLI_COMMANDS.get(operation.operation_id)
    if command and guide.has_tool("cli"):
        flags.append(f"CLI: `{CLI_NAME} {command}`")
    blocks: list[Block] = [
        Heading(4, f"`{operation.operation_id}`: {operation.summary}"),
        Paragraph(f"`{operation.method} {operation.path}` · " + " · ".join(flags)),
        Paragraph(operation.description),
    ]
    if operation.parameters:
        blocks.append(
            Table(
                ("Parameter", "In", "Required", "Description", "Example"),
                tuple(
                    (
                        f"`{item.name}`",
                        item.location,
                        "yes" if item.required else "no",
                        _parameter_description(item),
                        f"`{item.example}`" if item.example is not None else "",
                    )
                    for item in operation.parameters
                ),
            )
        )
    if operation.problem_types:
        errors = ", ".join(
            f"[{slug}]({guide.url(f'/problems/{slug}')}) ({PROBLEM_TYPES[slug][0]})"
            for slug in operation.problem_types
        )
        blocks.append(Paragraph(f"Errors: {errors}."))
    return blocks


def _parameter_description(parameter: Parameter) -> str:
    sentences = [parameter.description.strip().rstrip(".")] if parameter.description else []
    if parameter.values:
        sentences.append("One of: " + ", ".join(f"`{value}`" for value in parameter.values))
    if parameter.default is not None:
        sentences.append(f"Default: `{parameter.default}`")
    return "".join(f"{sentence}. " for sentence in sentences if sentence).strip()


def _auth_text(guide: AgentGuide) -> str:
    protected = [item.operation_id for item in guide.operations if item.requires_api_key]
    return (
        "**Auth:** other operations need no authentication. "
        + ", ".join(f"`{item}`" for item in protected)
        + " need an `X-API-Key` header with a key issued by the operator; without one they "
        "return `401` (or `503` when the instance has no keys configured). Request imports only "
        "when data is missing or stale: the scheduler refreshes current seasons automatically. "
        "Admin operations are for the operator."
    )


def _competition_lines(guide: AgentGuide) -> tuple[str, ...]:
    lines = [
        f"`{item.slug}`: {item.name} ({item.format}, {item.season_format} seasons, current "
        f"season {item.current_season or 'none'})"
        for item in guide.competitions
    ]
    if not guide.competitions_live:
        lines.append("Live data is unavailable; this list comes from the configuration file.")
    return tuple(lines) or ("None configured.",)


def _rules(guide: AgentGuide) -> tuple[str, ...]:
    limits = guide.rate_limits
    rate = (
        f"Rate limits: {limits.read_per_minute} reads and {limits.write_per_minute} writes per "
        "minute per client (`RateLimit` and `RateLimit-Policy` headers); on `429` wait "
        "`Retry-After` seconds."
        if limits.enabled
        else "Rate limiting is disabled on this instance."
    )
    return (
        (
            "Errors are `application/problem+json`: branch on `type`, then correct the request with "
            "`parameter`, `valid_values`, `hint`, and `examples`."
        ),
        "Unknown values are `null`, never `0`; a provider may not supply every statistic.",
        (
            "List responses are `{data, pagination, freshness}`; follow `pagination.next_cursor` "
            "with the same filters until it is `null`."
        ),
        "Use `fields=` for compact items (`id` is always included).",
        (
            "`freshness` reports `data_as_of` and `next_update_at` per competition-season; do not "
            "poll before `next_update_at`."
        ),
        rate,
    )


def _conventions(guide: AgentGuide) -> tuple[str, ...]:
    return (
        (
            "Seasons are strings: `2026-2027` for split seasons, `2026` for calendar-year seasons "
            "(see `season_format` in `list_competitions`)."
        ),
        (
            "Times are ISO 8601 with an offset; `date_from` and `date_to` take dates or datetimes, "
            "and naive values are UTC."
        ),
        (
            "Match `status` is `finished`, `scheduled`, or `inactive` (postponed or cancelled); "
            "`list_matches` covers all of them, while the deprecated `list_results` returns only "
            "finished matches."
        ),
        "Match `id`s and team slugs are stable across refreshes; store them, not names.",
        (
            "Read responses carry a weak `ETag`; send it in `If-None-Match` to get `304 Not "
            f"Modified`. `Cache-Control` allows caching for at most {guide.cache_max_age_seconds} "
            "seconds, or until the next scheduled update."
        ),
    )


def _tools_blocks(guide: AgentGuide) -> list[Block]:
    if not guide.tools:
        return []
    return [
        Heading(2, "Tools"),
        Bullets(tuple(f"[{item.name}]({item.url}): {item.description}" for item in guide.tools)),
    ]


def _tasks_intro(guide: AgentGuide) -> str:
    if not guide.has_tool("cli"):
        return "Top 5 tasks:"
    return (
        f"Top 5 tasks, each as curl and as the equivalent `{CLI_NAME}` command (set "
        f"`FOOTBALL_RESULTS_URL={guide.base_url}` for the CLI):"
    )


def _task_examples(guide: AgentGuide, task: Task) -> list[Block]:
    blocks: list[Block] = [Code(task.curl(guide.base_url))]
    if guide.has_tool("cli"):
        blocks.append(Code(task.cli()))
    return blocks


def _cli_blocks(guide: AgentGuide) -> list[Block]:
    if not guide.has_tool("cli"):
        return []
    commands = {command: operation for operation, command in CLI_COMMANDS.items()}
    return [
        Heading(2, "Command-line client", anchor="cli"),
        Paragraph(
            f"`{CLI_NAME}` wraps this API for shells and coding agents. It prints the JSON "
            "response body when stdout is not a terminal (or with `--json`) and a table "
            "otherwise; errors are RFC 9457 problem details as one JSON line on stderr. Every "
            "command has `--help` with examples. It needs Python 3.12 or later."
        ),
        Code(CLI_INSTALL),
        Paragraph(
            "Configure it with environment variables (or `--url`, `--api-key`, `--timeout`). The "
            "API key is sent only for `refresh`, `fetch-season`, `status`, `scrape-runs`, and "
            "`scheduler`."
        ),
        Code(
            f"export FOOTBALL_RESULTS_URL={guide.base_url}\n"
            "export FOOTBALL_RESULTS_API_KEY=...   # only for refresh, fetch-season, status, admin"
        ),
        Table(
            ("Command", "Operation", "Summary"),
            tuple(
                (
                    f"`{CLI_NAME} {command}`",
                    f"`{operation}`",
                    guide.operation(operation).summary,
                )
                for command, operation in commands.items()
                if any(item.operation_id == operation for item in guide.operations)
            ),
        ),
        Paragraph(
            "Options use the API parameter names (`--date-from` is `date_from`). `--fields` "
            "selects item fields; `--all` follows `next_cursor` up to `--max-items` (1000); "
            "`--fail-if-stale` exits 4 when a season was never fetched or its update is more "
            "than 15 minutes overdue; `--wait` polls a job until it finishes."
        ),
        Table(
            ("Exit code", "Meaning"),
            tuple((str(code), meaning) for code, meaning in CLI_EXIT_CODES),
        ),
        Paragraph(
            "Server errors are passed through unchanged. Problems detected by the CLI itself use "
            f"`type` `{CLI_PROBLEM_PREFIX}<slug>`:"
        ),
        Table(
            ("Type", "Exit code", "Meaning"),
            tuple(
                (f"`{CLI_PROBLEM_PREFIX}{slug}`", str(code), meaning)
                for slug, (code, meaning) in CLI_PROBLEM_GUIDANCE.items()
            ),
        ),
    ]


def _skill_blocks(guide: AgentGuide) -> list[Block]:
    skill = next((item for item in guide.tools if item.kind == "skill"), None)
    if skill is None:
        return []
    home = f"~/.copilot/skills/{SKILL_NAME}"
    return [
        Heading(2, "Agent Skill", anchor="skill"),
        Paragraph(
            f"The [`{SKILL_NAME}` skill]({skill.url}) is one `SKILL.md` file in the "
            "[Agent Skills](https://agentskills.io/specification) format. It teaches coding "
            f"agents to answer football questions with `{CLI_NAME}`: resolving slugs, recipes, "
            "`null` values, freshness, refresh jobs, and exit codes. Install the CLI too (or the "
            "skill falls back to curl), and set `FOOTBALL_RESULTS_URL`; the skill reads the API "
            "URL from it and works offline once installed."
        ),
        Code(
            "# GitHub Copilot CLI (HTTPS URLs only; --project installs into .github/skills)\n"
            f"copilot skill add {skill.url}\n"
            "# Any agent: download it into a skills directory\n"
            f"mkdir -p {home} && curl -fsSL {skill.url} -o {home}/SKILL.md\n"
            f"export FOOTBALL_RESULTS_URL={guide.base_url}"
        ),
        Paragraph(
            f"The directory must be named `{SKILL_NAME}`, like the skill. GitHub Copilot reads "
            "personal skills from `~/.copilot/skills/` or `~/.agents/skills/` and project skills "
            "from `.github/skills/`, `.agents/skills/`, or `.claude/skills/`; Claude Code reads "
            "`~/.claude/skills/` and `.claude/skills/`. From a checkout of the repository, "
            f"`copilot skill add ./skills/{SKILL_NAME}/SKILL.md` installs the same file."
        ),
    ]


def _mcp_blocks(guide: AgentGuide) -> list[Block]:
    if not guide.has_tool("mcp"):
        return []
    url = guide.url(MCP_PATH)
    return [
        Heading(2, "MCP server", anchor="mcp"),
        Paragraph(
            f"`{url}` is a [Model Context Protocol](https://modelcontextprotocol.io) server "
            "(Streamable HTTP, protocol 2026-07-28 and older revisions; stateless, JSON "
            "responses) for clients without a shell. Each tool calls this API, so validation, "
            "`null` values, freshness, IDs, and cursors are the same. `structuredContent` "
            "matches each tool's `outputSchema`; errors are tool results with `isError: true` "
            "whose text is the problem JSON (`type`, `hint`, `valid_values`)."
        ),
        Table(
            ("Tool", "API operation", "Summary"),
            tuple(
                (f"`{name}`", f"`{operation}`", summary)
                for name, (operation, summary) in MCP_TOOLS.items()
            ),
        ),
        Paragraph(
            "Each MCP request counts once against the read rate limit; `request_refresh` also "
            "counts against the write limit and needs an API key in the `X-API-Key` header of "
            "the MCP connection. Leave the header out for read-only use."
        ),
        Code(
            "# GitHub Copilot CLI\n"
            f"copilot mcp add --transport http {MCP_SERVER_NAME} {url}\n"
            "# with request_refresh\n"
            f'copilot mcp add --transport http --header "X-API-Key: $KEY" {MCP_SERVER_NAME} '
            f"{url}\n"
            "# Claude Code (check `claude mcp add --help` for your version)\n"
            f"claude mcp add --transport http {MCP_SERVER_NAME} {url}"
        ),
        Paragraph(
            "VS Code and other clients that read `.mcp.json` or `~/.copilot/mcp-config.json`:"
        ),
        Code(
            json.dumps({"mcpServers": {MCP_SERVER_NAME: {"type": "http", "url": url}}}, indent=2),
            "json",
        ),
    ]
