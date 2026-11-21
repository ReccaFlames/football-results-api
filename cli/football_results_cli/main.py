"""`football-results`: a command-line client for the Football Results API.

Command names, option names, and exit codes are public: agents and scripts depend on them. Each
command maps to one API operation (`OPERATIONS`), and its API options carry the OpenAPI parameter
names (`--date-from` is `date_from`); a test checks them against the OpenAPI spec.
"""

import sys
import time
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

import typer
from typer.core import TyperGroup

from football_results_cli import __version__, output
from football_results_cli import client as http
from football_results_cli.problems import EXIT_USAGE, CliFailure, cli_problem, write_problem

PROG_NAME = "football-results"

# Command -> API operation ID.
OPERATIONS: dict[str, str] = {
    "health": "get_health",
    "competitions": "list_competitions",
    "matches": "list_matches",
    "standings": "get_standings",
    "teams": "list_teams",
    "refresh": "request_refresh",
    "fetch-season": "fetch_historical_season",
    "status": "get_job",
    "scrape-runs": "list_scrape_runs",
    "scheduler": "get_scheduler_status",
}

# Options that only steer the CLI and are never sent to the API, per command.
_WAIT_OPTIONS = frozenset({"json_output", "wait", "wait_timeout", "poll_interval"})
CLI_ONLY_OPTIONS: dict[str, frozenset[str]] = {
    "health": frozenset({"json_output"}),
    "competitions": frozenset({"json_output", "fields"}),
    "matches": frozenset({"json_output", "all_pages", "max_items", "fail_if_stale"}),
    "standings": frozenset({"json_output", "fail_if_stale"}),
    "teams": frozenset({"json_output", "fields", "all_pages", "max_items"}),
    "refresh": _WAIT_OPTIONS,
    "fetch-season": _WAIT_OPTIONS,
    "status": _WAIT_OPTIONS,
    "scrape-runs": frozenset({"json_output", "all_pages", "max_items"}),
    "scheduler": frozenset({"json_output"}),
}

# Item fields for commands whose `--fields` is applied by the CLI (the API has no `fields=`).
COMPETITION_FIELDS = (
    "slug",
    "name",
    "country",
    "format",
    "season_format",
    "season_start_month",
    "current_season",
    "available_seasons",
    "fetched_seasons",
    "automatic_refresh_enabled",
    "scheduler_state",
    "next_check_at",
)
TEAM_FIELDS = ("slug", "name", "aliases")
JOB_DONE_STATUSES = frozenset({"completed"})
JOB_FAILED_STATUSES = frozenset({"failed", "cancelled"})

_sleep = time.sleep
_monotonic = time.monotonic


class JsonErrorGroup(TyperGroup):
    """Reports usage errors and API failures as problem details on stderr with stable exit codes."""

    def main(  # type: ignore[override]
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        try:
            result = super().main(
                args, prog_name or PROG_NAME, complete_var, standalone_mode=False, **extra
            )
            code = result if isinstance(result, int) else 0
        except CliFailure as failure:
            write_problem(failure.problem)
            code = failure.exit_code
        except typer.TyperException as error:
            code = error.exit_code
            if code == EXIT_USAGE:
                write_problem(_usage_problem(error))
            else:
                write_problem({"title": "Error", "detail": error.format_message()})
        except typer.Abort:
            code = 1
        if standalone_mode:
            sys.exit(code)
        return code


def _usage_problem(error: typer.TyperException) -> dict[str, Any]:
    context = getattr(error, "ctx", None)
    command_path = context.command_path if context is not None else PROG_NAME
    param = getattr(error, "param", None)
    parameter = None
    if param is not None:
        parameter = next((opt for opt in getattr(param, "opts", ()) if opt.startswith("--")), None)
        parameter = parameter or param.name
    return cli_problem(
        "usage-error",
        error.format_message(),
        parameter=parameter,
        hint=f"Run '{command_path} --help' for options and examples.",
    ).problem


app = typer.Typer(
    cls=JsonErrorGroup,
    name=PROG_NAME,
    help=(
        "Query football fixtures, results, standings, and teams from a Football Results API "
        "server. Prints JSON when piped (the API response body) and tables on a terminal; "
        "errors are RFC 9457 problem details on stderr.\n\n"
        "Exit codes: 0 ok, 2 usage, 3 not found, 4 upstream/stale, 5 auth."
    ),
    epilog=(
        "Examples:\n\n\b\n"
        "  export FOOTBALL_RESULTS_URL=http://localhost:8000\n"
        "  football-results competitions\n"
        "  football-results matches -c ekstraklasa --team legia-warszawa --status finished "
        "--order desc --limit 5\n"
        "  football-results standings -c ekstraklasa --json | jq '.data[:3]'"
    ),
    rich_markup_mode=None,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version(value: bool) -> None:
    if value:
        sys.stdout.write(f"{PROG_NAME} {__version__}\n")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            envvar="FOOTBALL_RESULTS_URL",
            show_envvar=True,
            help=f"API base URL, optionally with a path prefix [default: {http.DEFAULT_URL}]",
            show_default=False,
        ),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            envvar="FOOTBALL_RESULTS_API_KEY",
            show_envvar=True,
            help=(
                "API key for refresh, fetch-season, status, scrape-runs, and scheduler; sent only "
                "to the API URL, never printed. Prefer the environment variable over the option."
            ),
            show_default=False,
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            envvar="FOOTBALL_RESULTS_TIMEOUT",
            show_envvar=True,
            min=0.1,
            help="HTTP timeout in seconds",
        ),
    ] = http.DEFAULT_TIMEOUT_SECONDS,
    _show_version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Print the version and exit"
        ),
    ] = False,
) -> None:
    ctx.obj = http.Config(url=http.normalize_url(url), api_key=api_key or None, timeout=timeout)


def _api(ctx: typer.Context) -> http.ApiClient:
    client = http.create_http_client(ctx.obj)
    ctx.call_on_close(client.close)
    return http.ApiClient(ctx.obj, client, sleep=_sleep)


# --- Shared options ------------------------------------------------------------------------

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json", help="Print the JSON response body even on a terminal (the default when piped)"
    ),
]
CompetitionFilter = Annotated[
    str | None,
    typer.Option(
        "--competition",
        "-c",
        help="Competition slug from 'competitions'; omit for all",
        show_default=False,
    ),
]
CompetitionRequired = Annotated[
    str, typer.Option("--competition", "-c", help="Competition slug from 'competitions'")
]
SeasonOption = Annotated[
    str | None,
    typer.Option(
        "--season",
        "-s",
        help="Season in the competition's format: 2026-2027 (split) or 2026 (calendar)",
        show_default=False,
    ),
]
LimitOption = Annotated[
    int | None,
    typer.Option(
        "--limit",
        min=1,
        max=200,
        help="Page size, 1-200 [default: 50; 200 with --all]",
        show_default=False,
    ),
]
CursorOption = Annotated[
    str | None,
    typer.Option(
        "--cursor",
        help="pagination.next_cursor from the previous page; repeat the same filters",
        show_default=False,
    ),
]
AllOption = Annotated[
    bool, typer.Option("--all", help="Follow next_cursor and print every page as one response")
]
MaxItemsOption = Annotated[
    int, typer.Option("--max-items", min=1, help="With --all, stop after this many items")
]
FailIfStaleOption = Annotated[
    bool,
    typer.Option(
        "--fail-if-stale",
        help=(
            "Exit 4 (after printing the data) if a season was never fetched or its scheduled "
            "update is more than 15 minutes overdue"
        ),
    ),
]
WaitOption = Annotated[
    bool, typer.Option("--wait", help="Poll the job until it completes or fails")
]
WaitTimeoutOption = Annotated[
    float, typer.Option("--wait-timeout", min=0, help="With --wait, give up after these seconds")
]
PollIntervalOption = Annotated[
    float, typer.Option("--poll-interval", min=0.1, help="With --wait, seconds between polls")
]


def _fields_option(help_text: str) -> Any:
    return typer.Option("--fields", help=help_text, show_default=False)


# --- Helpers -------------------------------------------------------------------------------


def _split_fields(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()] or None


def _client_fields(value: str | None, valid: Sequence[str]) -> list[str] | None:
    fields = _split_fields(value)
    unknown = [item for item in fields or () if item not in valid]
    if unknown:
        raise cli_problem(
            "usage-error",
            f"Unknown field(s): {', '.join(unknown)}",
            parameter="--fields",
            invalid=unknown,
            valid_values=list(valid),
        )
    return fields


def _project(items: list[dict[str, Any]], key: str, fields: list[str] | None) -> list[Any]:
    if not fields:
        return items
    names = [key, *(field for field in fields if field != key)]
    return [{name: item.get(name) for name in names} for item in items]


def _fetch_list(
    api: http.ApiClient,
    path: str,
    params: dict[str, Any],
    *,
    all_pages: bool,
    max_items: int,
    auth: bool = False,
) -> dict[str, Any]:
    if not all_pages:
        return api.get(path, params, auth=auth).body
    limit = params.get("limit") or 200
    data: list[Any] = []
    while True:
        page = api.get(path, {**params, "limit": min(limit, max_items - len(data))}, auth=auth).body
        data.extend(page["data"])
        cursor = page["pagination"]["next_cursor"]
        if not cursor or len(data) >= max_items:
            break
        params = {**params, "cursor": cursor}
    return {**page, "data": data, "pagination": {"limit": limit, "next_cursor": cursor}}


def _check_stale(body: Mapping[str, Any], fail_if_stale: bool) -> None:
    if not fail_if_stale:
        return
    stale = output.stale_entries(body.get("freshness") or [])
    if stale:
        keys = ("competition", "season", "data_as_of", "next_update_at")
        raise cli_problem(
            "stale-data",
            f"{len(stale)} competition-season(s) have stale data",
            stale=[{key: entry.get(key) for key in keys} for entry in stale],
            hint=(
                "The data was printed but may be outdated. The scheduler refreshes current "
                "seasons automatically; with an API key, 'football-results refresh' queues one."
            ),
        )


def _wait_for_job(
    api: http.ApiClient, job_id: int, *, timeout: float, interval: float
) -> dict[str, Any]:
    deadline = _monotonic() + timeout
    while True:
        job = api.get(f"/api/v1/jobs/{job_id}", auth=True).body
        status = job.get("status")
        if status in JOB_DONE_STATUSES:
            return job
        if status in JOB_FAILED_STATUSES:
            output.print_json(job)
            raise cli_problem(
                "job-failed",
                f"Job {job_id} {status}: {job.get('error_message') or 'no error message'}",
                job_id=job_id,
                hint="The job status is on stdout; check the source and retry later.",
            )
        remaining = deadline - _monotonic()
        if remaining <= 0:
            output.print_json(job)
            raise cli_problem(
                "wait-timeout",
                f"Job {job_id} is still {status} after {timeout:g} seconds",
                job_id=job_id,
                hint=f"Poll again later with 'football-results status {job_id} --wait'.",
            )
        _sleep(min(interval, remaining))


def _print_record(record: Mapping[str, Any], *, json_output: bool, title: str) -> None:
    if output.use_json(json_output):
        output.print_json(record)
    else:
        output.print_record(record, title=title)


# --- Commands ------------------------------------------------------------------------------


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results health\n"
        "  football-results --url https://football.example.com health --json"
    ),
)
def health(ctx: typer.Context, json_output: JsonOption = False) -> None:
    """Check the API, its database, and the scheduler heartbeat (get_health)."""
    body = _api(ctx).get("/health").body
    _print_record(body, json_output=json_output, title="Health")


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results competitions\n"
        "  football-results competitions --fields slug,current_season,fetched_seasons --json"
    ),
)
def competitions(
    ctx: typer.Context,
    fields: Annotated[
        str | None,
        _fields_option(
            "Comma-separated competition fields (applied by the CLI); slug is always included"
        ),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """List competitions, their current season, and stored seasons (list_competitions).

    Start here to find competition slugs and season formats.
    """
    selected = _client_fields(fields, COMPETITION_FIELDS)
    body = _project(_api(ctx).get("/api/v1/competitions").body, "slug", selected)
    if output.use_json(json_output):
        output.print_json(body)
        return
    columns = output.selected_columns(output.COMPETITION_COLUMNS, selected, "slug")
    output.print_table(body, columns, title="Competitions")


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results matches -c ekstraklasa --team legia-warszawa --status finished "
        "--order desc --limit 5\n"
        '  football-results matches -c ekstraklasa --status scheduled --date-from "$(date -u +%F)"'
        " --fields kickoff_at,home_team,away_team\n"
        "  football-results matches -c ekstraklasa -s 2025-2026 --round 3 --all --json"
    ),
)
def matches(
    ctx: typer.Context,
    competition: CompetitionFilter = None,
    season: SeasonOption = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Comma-separated statuses: finished, scheduled, inactive [default: all]",
            show_default=False,
        ),
    ] = None,
    date_from: Annotated[
        str | None,
        typer.Option(
            "--date-from",
            help="Earliest kickoff, inclusive: ISO date or datetime (naive values are UTC)",
            show_default=False,
        ),
    ] = None,
    date_to: Annotated[
        str | None,
        typer.Option(
            "--date-to",
            help="Latest kickoff, inclusive: ISO date (the whole UTC day) or datetime",
            show_default=False,
        ),
    ] = None,
    team: Annotated[
        str | None,
        typer.Option(
            "--team",
            help="Team slug from 'teams' (exact), otherwise a name substring",
            show_default=False,
        ),
    ] = None,
    round: Annotated[
        int | None, typer.Option("--round", min=1, help="League round number", show_default=False)
    ] = None,
    stage: Annotated[
        str | None,
        typer.Option(
            "--stage", help="Stage name substring (cup rounds, groups)", show_default=False
        ),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"] | None,
        typer.Option("--order", help="Kickoff order [default: asc]", show_default=False),
    ] = None,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    fields: Annotated[
        str | None,
        _fields_option("Comma-separated match fields (sent to the API); id is always included"),
    ] = None,
    all_pages: AllOption = False,
    max_items: MaxItemsOption = 1000,
    fail_if_stale: FailIfStaleOption = False,
    json_output: JsonOption = False,
) -> None:
    """List finished, scheduled, and inactive matches with filters (list_matches).

    Unknown scores and statistics are null, never 0. Freshness per competition-season is in the
    JSON 'freshness' list (and below the table on a terminal).
    """
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
    body = _fetch_list(
        _api(ctx), "/api/v1/matches", params, all_pages=all_pages, max_items=max_items
    )
    if output.use_json(json_output):
        output.print_json(body)
    else:
        columns = output.selected_columns(output.MATCH_COLUMNS, _split_fields(fields), "id")
        if not fields and len({item.get("competition") for item in body["data"]}) <= 1:
            columns = [column for column in columns if column.header != "Competition"]
        output.print_table(body["data"], columns, title="Matches")
        output.print_footer(body)
    _check_stale(body, fail_if_stale)


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results standings -c ekstraklasa\n"
        "  football-results standings -c ekstraklasa -s 2025-2026 "
        "--fields position,team_name,points --json\n"
        '  football-results standings -c europa-league --stage "Group A"'
    ),
)
def standings(
    ctx: typer.Context,
    competition: CompetitionRequired,
    season: Annotated[
        str | None,
        typer.Option(
            "--season",
            "-s",
            help="Season in the competition's format [default: the current season]",
            show_default=False,
        ),
    ] = None,
    stage: Annotated[
        str | None,
        typer.Option(
            "--stage", help="Stage substring; required for cup competitions", show_default=False
        ),
    ] = None,
    fields: Annotated[
        str | None,
        _fields_option(
            "Comma-separated table row fields (sent to the API); team_slug is always included"
        ),
    ] = None,
    fail_if_stale: FailIfStaleOption = False,
    json_output: JsonOption = False,
) -> None:
    """Show the league table computed from finished matches (get_standings)."""
    params = {"competition": competition, "season": season, "stage": stage, "fields": fields}
    body = _api(ctx).get("/api/v1/standings", params).body
    if output.use_json(json_output):
        output.print_json(body)
    else:
        title = " ".join(
            str(part) for part in (body.get("competition"), body.get("season")) if part
        )
        if body.get("stage"):
            title += f" · {body['stage']}"
        columns = output.selected_columns(
            output.STANDING_COLUMNS, _split_fields(fields), "team_slug"
        )
        output.print_table(body["data"], columns, title=title or "Standings")
        output.print_footer(body)
    _check_stale(body, fail_if_stale)


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results teams -c ekstraklasa -q legia\n"
        "  football-results teams -c ekstraklasa --all --fields slug --json"
    ),
)
def teams(
    ctx: typer.Context,
    competition: CompetitionFilter = None,
    season: SeasonOption = None,
    q: Annotated[
        str | None,
        typer.Option(
            "-q",
            "--q",
            help="Team slug (exact), otherwise a case-insensitive name or slug substring",
            show_default=False,
        ),
    ] = None,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    fields: Annotated[
        str | None,
        _fields_option("Comma-separated team fields (applied by the CLI); slug is always included"),
    ] = None,
    all_pages: AllOption = False,
    max_items: MaxItemsOption = 1000,
    json_output: JsonOption = False,
) -> None:
    """Find teams and their canonical slugs for the --team filter (list_teams)."""
    selected = _client_fields(fields, TEAM_FIELDS)
    params = {
        "competition": competition,
        "season": season,
        "q": q,
        "limit": limit,
        "cursor": cursor,
    }
    body = _fetch_list(_api(ctx), "/api/v1/teams", params, all_pages=all_pages, max_items=max_items)
    body = {**body, "data": _project(body["data"], "slug", selected)}
    if output.use_json(json_output):
        output.print_json(body)
        return
    columns = output.selected_columns(output.TEAM_COLUMNS, selected, "slug")
    output.print_table(body["data"], columns, title="Teams")
    output.print_footer(body)


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  FOOTBALL_RESULTS_API_KEY=... football-results refresh -c ekstraklasa\n"
        "  football-results refresh -c ekstraklasa --wait --wait-timeout 600"
    ),
)
def refresh(
    ctx: typer.Context,
    competition: CompetitionRequired,
    season: Annotated[
        str | None,
        typer.Option(
            "--season",
            "-s",
            help="Must be the current season [default: the current season]",
            show_default=False,
        ),
    ] = None,
    wait: WaitOption = False,
    wait_timeout: WaitTimeoutOption = 300.0,
    poll_interval: PollIntervalOption = 5.0,
    json_output: JsonOption = False,
) -> None:
    """Queue a refresh of the current season; needs an API key (request_refresh).

    Use only when data is missing or stale: the scheduler refreshes current seasons
    automatically. Prints the queued job, or with --wait the finished job.
    """
    api = _api(ctx)
    body = api.post("/api/v1/results/refresh", {"competition": competition, "season": season}).body
    if wait:
        body = _wait_for_job(api, body["job_id"], timeout=wait_timeout, interval=poll_interval)
    _print_record(body, json_output=json_output, title="Job")


@app.command(
    "fetch-season",
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results fetch-season -c ekstraklasa -s 2025-2026 --wait\n"
        "  football-results fetch-season -c ekstraklasa -s 2024-2025 "
        "--source-url 'http://www.90minut.pl/liga/1/liga<ID>.html'"
    ),
)
def fetch_season(
    ctx: typer.Context,
    competition: CompetitionRequired,
    season: Annotated[
        str,
        typer.Option(
            "--season", "-s", help="A non-current season in the competition's season format"
        ),
    ],
    source_url: Annotated[
        str | None,
        typer.Option(
            "--source-url",
            help="Source for a season that is not configured (an allowed provider URL)",
            show_default=False,
        ),
    ] = None,
    force_refresh: Annotated[
        bool,
        typer.Option("--force-refresh", help="Queue a new fetch even if the season is stored"),
    ] = False,
    wait: WaitOption = False,
    wait_timeout: WaitTimeoutOption = 300.0,
    poll_interval: PollIntervalOption = 5.0,
    json_output: JsonOption = False,
) -> None:
    """Import a past season; needs an API key (fetch_historical_season).

    A season that is already stored is printed directly; otherwise a job is queued (use --wait
    to poll it, then 'matches -s SEASON' to read the data).
    """
    api = _api(ctx)
    payload = {
        "competition": competition,
        "season": season,
        "source_url": source_url,
        "force_refresh": force_refresh or None,
    }
    response = api.post("/api/v1/results/history/fetch", payload)
    body = response.body
    if response.status == 202 and wait:
        body = _wait_for_job(api, body["job_id"], timeout=wait_timeout, interval=poll_interval)
    if response.status == 200 and "results" in body:
        if output.use_json(json_output):
            output.print_json(body)
        else:
            title = f"{competition} {season}"
            output.print_table(body["results"], output.RESULT_COLUMNS, title=title)
            refreshed = output.format_value(body.get("refreshed_at"), "refreshed_at")
            output.note(f"Already stored; refreshed at {refreshed}")
        return
    _print_record(body, json_output=json_output, title="Job")


@app.command(
    epilog=(
        "Examples:\n\n\b\n  football-results status 42\n  football-results status 42 --wait --json"
    ),
)
def status(
    ctx: typer.Context,
    job_id: Annotated[int, typer.Argument(min=1, help="Job ID from refresh or fetch-season")],
    wait: WaitOption = False,
    wait_timeout: WaitTimeoutOption = 300.0,
    poll_interval: PollIntervalOption = 5.0,
    json_output: JsonOption = False,
) -> None:
    """Show a refresh or fetch job: queued, running, completed, or failed (get_job).

    Needs an API key. With --wait, exits 4 if the job fails or does not finish in time.
    """
    api = _api(ctx)
    if wait:
        body = _wait_for_job(api, job_id, timeout=wait_timeout, interval=poll_interval)
    else:
        body = api.get(f"/api/v1/jobs/{job_id}", auth=True).body
    _print_record(body, json_output=json_output, title="Job")


@app.command(
    "scrape-runs",
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results scrape-runs --status failed --limit 10\n"
        "  football-results scrape-runs -c ekstraklasa --all --max-items 500 --json"
    ),
)
def scrape_runs(
    ctx: typer.Context,
    competition: CompetitionFilter = None,
    season: SeasonOption = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help=(
                "Comma-separated job statuses: queued, running, completed, failed, cancelled "
                "[default: all]"
            ),
            show_default=False,
        ),
    ] = None,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    all_pages: AllOption = False,
    max_items: MaxItemsOption = 1000,
    json_output: JsonOption = False,
) -> None:
    """List recent import jobs, newest first; needs an API key (list_scrape_runs).

    Use to find out why data is stale: failed jobs carry the error message.
    """
    params = {
        "competition": competition,
        "season": season,
        "status": status,
        "limit": limit,
        "cursor": cursor,
    }
    body = _fetch_list(
        _api(ctx),
        "/api/v1/admin/scrape-runs",
        params,
        all_pages=all_pages,
        max_items=max_items,
        auth=True,
    )
    if output.use_json(json_output):
        output.print_json(body)
        return
    output.print_table(body["data"], output.SCRAPE_RUN_COLUMNS, title="Import jobs")
    output.print_footer(body)


@app.command(
    epilog=(
        "Examples:\n\n\b\n"
        "  football-results scheduler\n"
        "  football-results scheduler --json | jq '.seasons[] | select(.stale)'"
    ),
)
def scheduler(ctx: typer.Context, json_output: JsonOption = False) -> None:
    """Show the scheduler and the refresh state of current seasons; needs an API key
    (get_scheduler_status).

    The status is 'stalled' when no worker heartbeat is recent; a season is stale when it was
    never fetched or its update is more than the grace period overdue.
    """
    body = _api(ctx).get("/api/v1/admin/scheduler", auth=True).body
    if output.use_json(json_output):
        output.print_json(body)
        return
    summary = {key: value for key, value in body.items() if key not in ("workers", "seasons")}
    output.print_record(summary, title="Scheduler")
    output.print_table(body.get("seasons") or [], output.SEASON_SCHEDULE_COLUMNS, title="Seasons")
    output.print_table(body.get("workers") or [], output.WORKER_COLUMNS, title="Workers")


command = typer.main.get_command(app)


def main() -> None:
    command.main(prog_name=PROG_NAME)
