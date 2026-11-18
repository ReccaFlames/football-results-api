import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from football_results_cli import client as http
from football_results_cli import main as cli
from football_results_cli import output, problems
from typer.testing import CliRunner

import app.main as main_module
from app.config import Settings
from app.main import app as api_app
from app.models import CompetitionSummary, Freshness, FreshnessSource, Team
from app.ratelimit import FixedWindowRateLimiter
from tests.test_api import FakeRepository, fixture

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FOOTBALL_RESULTS_URL", "FOOTBALL_RESULTS_API_KEY", "FOOTBALL_RESULTS_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main_module.settings, "api_keys", "test-api-key")
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setattr(output, "stdout_is_tty", lambda: False)
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> FakeRepository:
    repository = FakeRepository()
    repository.matches = [
        fixture(1, "Wisła Płock", "Legia", (2, 0), days=0),
        fixture(2, "Lech", "Wisła Kraków", (1, 1), days=0),
        fixture(3, "Legia", "Lech", (0, 1), days=7, round_number=2),
        fixture(4, "Wisła Kraków", "Wisła Płock", days=14, round_number=3),
        fixture(5, "Lech", "Wisła Płock", days=14, round_number=3, status="inactive"),
    ]
    monkeypatch.setattr(main_module, "store", repository)
    return repository


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch, repository: FakeRepository) -> list[httpx.Request]:
    """Route the CLI to the real FastAPI app over `FakeRepository`, recording each request."""
    sent: list[httpx.Request] = []

    def create(config: http.Config) -> httpx.Client:
        client = TestClient(api_app, base_url=config.url)
        client.event_hooks = {"request": [sent.append], "response": []}
        return client

    monkeypatch.setattr(http, "create_http_client", create)
    return sent


def mock_api(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    monkeypatch.setattr(
        http,
        "create_http_client",
        lambda config: httpx.Client(transport=httpx.MockTransport(record), base_url=config.url),
    )
    return sent


def invoke(*args: str, env: dict[str, str] | None = None):
    return runner.invoke(cli.app, list(args), env=env)


def problem(result) -> dict[str, Any]:
    lines = result.stderr.strip().splitlines()
    return json.loads(lines[-1])


def freshness(next_update_at: datetime | None, data_as_of: datetime | None) -> Freshness:
    return Freshness(
        competition="ekstraklasa",
        season="2026-2027",
        data_as_of=data_as_of,
        next_update_at=next_update_at,
        automatic_refresh=True,
        scheduler_state="idle",
        source=FreshnessSource(provider="90minut", url="http://www.90minut.pl/liga/1/l.html"),
    )


# --- Output --------------------------------------------------------------------------------


def test_piped_output_is_the_compact_response_body(requests, repository) -> None:
    repository.freshness = [freshness(None, datetime.now(UTC))]
    result = invoke("matches", "-c", "ekstraklasa", "--status", "finished,scheduled")

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    body = json.loads(result.stdout)
    assert [item["id"] for item in body["data"]] == [1, 2, 3, 4]
    assert body["data"][3]["home_goals"] is None
    assert body["pagination"] == {"limit": 50, "next_cursor": None}
    assert body["freshness"][0]["competition"] == "ekstraklasa"
    assert dict(requests[0].url.params) == {
        "competition": "ekstraklasa",
        "status": "finished,scheduled",
    }


def test_terminal_output_is_a_table_with_nulls_as_dashes_and_a_freshness_footer(
    requests, repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output, "stdout_is_tty", lambda: True)
    now = datetime.now(UTC)
    repository.freshness = [freshness(now - timedelta(hours=1), now - timedelta(days=1))]
    result = invoke("matches", "-c", "ekstraklasa", "--limit", "4", env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stderr
    row = next(line for line in result.stdout.splitlines() if line.startswith("│  4 │"))
    assert [cell.strip() for cell in row.split("│")[4:7]] == ["Wisła Kraków", "—", "Wisła Płock"]
    assert "2026-08-01 18:00Z" in result.stdout
    assert "ekstraklasa 2026-2027: data as of" in result.stderr
    assert "[stale]" in result.stderr
    assert "--cursor " in result.stderr

    forced = invoke("matches", "--json", "--limit", "1")
    assert json.loads(forced.stdout)["data"][0]["id"] == 1
    assert forced.stdout.startswith("{\n  ")


def test_fail_if_stale_prints_the_data_then_exits_4(requests, repository) -> None:
    repository.freshness = [freshness(datetime.now(UTC) + timedelta(hours=1), None)]
    result = invoke("matches", "-c", "ekstraklasa", "--fail-if-stale")

    assert result.exit_code == 4
    assert len(json.loads(result.stdout)["data"]) == 5
    assert problem(result)["type"] == "urn:football-results:cli:stale-data"
    assert problem(result)["stale"][0]["season"] == "2026-2027"

    fetched = datetime.now(UTC) - timedelta(minutes=5)
    repository.freshness = [freshness(datetime.now(UTC) - timedelta(minutes=10), fetched)]
    assert invoke("matches", "-c", "ekstraklasa", "--fail-if-stale").exit_code == 0


def test_fields_go_to_the_api_for_matches_and_are_applied_locally_otherwise(requests) -> None:
    matches = json.loads(invoke("matches", "--fields", "home_team", "--limit", "1").stdout)
    assert matches["data"] == [{"id": 1, "home_team": "Wisła Płock"}]
    assert requests[-1].url.params["fields"] == "home_team"

    rejected = invoke("matches", "--fields", "home_score")
    assert rejected.exit_code == 2
    assert problem(rejected)["type"].endswith("/problems/invalid-parameter")

    competitions = json.loads(invoke("competitions", "--fields", "current_season").stdout)
    assert set(competitions[0]) == {"slug", "current_season"}
    teams = json.loads(invoke("teams", "-q", "legia", "--fields", "name").stdout)
    assert teams["data"] == [{"slug": "legia", "name": "Legia"}]
    assert "fields" not in requests[-1].url.params

    unknown = invoke("competitions", "--fields", "slug,bogus")
    assert unknown.exit_code == 2
    assert problem(unknown) | {"valid_values": None} == {
        "type": "urn:football-results:cli:usage-error",
        "title": "Invalid command-line usage",
        "detail": "Unknown field(s): bogus",
        "parameter": "--fields",
        "invalid": ["bogus"],
        "valid_values": None,
    }


def test_all_follows_next_cursor_up_to_max_items(requests) -> None:
    every = json.loads(invoke("matches", "--limit", "2", "--all").stdout)
    assert [item["id"] for item in every["data"]] == [1, 2, 3, 4, 5]
    assert every["pagination"] == {"limit": 2, "next_cursor": None}
    assert len(requests) == 3

    requests.clear()
    capped = json.loads(invoke("matches", "--limit", "2", "--all", "--max-items", "3").stdout)
    assert [item["id"] for item in capped["data"]] == [1, 2, 3]
    assert capped["pagination"]["next_cursor"]
    assert [request.url.params["limit"] for request in requests] == ["2", "1"]

    teams = json.loads(invoke("teams", "--all").stdout)
    assert [item["slug"] for item in teams["data"]] == [
        "lech",
        "legia",
        "wisla-krakow",
        "wisla-plock",
    ]


# --- Errors and exit codes -----------------------------------------------------------------


def test_server_problems_are_passed_through_with_their_exit_code(requests) -> None:
    expected = TestClient(api_app).get("/api/v1/standings?competition=nope").json()
    result = invoke("standings", "-c", "nope")

    assert result.exit_code == 3
    assert result.stdout == ""
    assert problem(result) == expected
    assert result.stderr.count("\n") == 1

    assert invoke("matches", "--date-from", "yesterday").exit_code == 2
    assert invoke("status", "999", env={"FOOTBALL_RESULTS_API_KEY": "test-api-key"}).exit_code == 3


@pytest.mark.parametrize(
    ("status", "problem_type", "code"),
    [
        (400, "x/problems/invalid-parameter", 2),
        (422, "x/problems/validation-error", 2),
        (401, "x/problems/unauthorized", 5),
        (403, None, 5),
        (503, "x/problems/writes-disabled", 5),
        (404, None, 3),
        (409, None, 4),
        (429, None, 4),
        (500, None, 4),
        (503, None, 4),
    ],
)
def test_exit_codes_for_http_errors(status: int, problem_type: str | None, code: int) -> None:
    assert problems.exit_code_for(status, problem_type) == code


def test_api_key_is_sent_only_for_jobs_and_never_printed(requests, repository) -> None:
    missing = invoke("refresh", "-c", "ekstraklasa")
    assert missing.exit_code == 5
    assert problem(missing)["type"].endswith("/problems/unauthorized")

    env = {"FOOTBALL_RESULTS_API_KEY": "test-api-key"}
    queued = invoke("refresh", "-c", "ekstraklasa", env=env)
    assert queued.exit_code == 0, queued.stderr
    assert json.loads(queued.stdout)["status"] == "queued"
    assert invoke("status", "1", env=env).exit_code == 0
    assert invoke("matches", env=env).exit_code == 0

    keys = [(request.method, request.headers.get("x-api-key")) for request in requests]
    assert keys == [
        ("POST", None),
        ("POST", "test-api-key"),
        ("GET", "test-api-key"),
        ("GET", None),
    ]
    assert json.loads(requests[1].content) == {"competition": "ekstraklasa"}
    for result in (missing, queued):
        assert "test-api-key" not in result.stdout + result.stderr


def test_disabled_writes_exit_5(requests, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "api_keys", "")
    result = invoke("--api-key", "anything", "refresh", "-c", "ekstraklasa")
    assert result.exit_code == 5
    assert problem(result)["type"].endswith("/problems/writes-disabled")


def test_status_wait_polls_until_the_job_finishes(
    requests, repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"FOOTBALL_RESULTS_API_KEY": "test-api-key"}
    assert invoke("refresh", "-c", "ekstraklasa", env=env).exit_code == 0

    def finish(status: str) -> Callable[[float], None]:
        def sleep(_seconds: float) -> None:
            repository.jobs[1] = repository.jobs[1].model_copy(
                update={"status": status, "error_message": "boom" if status == "failed" else None}
            )

        return sleep

    monkeypatch.setattr(cli, "_sleep", finish("completed"))
    done = invoke("status", "1", "--wait", "--poll-interval", "1", env=env)
    assert done.exit_code == 0, done.stderr
    assert json.loads(done.stdout)["status"] == "completed"

    repository.jobs[1] = repository.jobs[1].model_copy(update={"status": "running"})
    monkeypatch.setattr(cli, "_sleep", finish("failed"))
    failed = invoke("status", "1", "--wait", env=env)
    assert failed.exit_code == 4
    assert json.loads(failed.stdout)["status"] == "failed"
    assert problem(failed)["type"] == "urn:football-results:cli:job-failed"
    assert "boom" in problem(failed)["detail"]

    repository.jobs[1] = repository.jobs[1].model_copy(update={"status": "running"})
    late = invoke("status", "1", "--wait", "--wait-timeout", "0", env=env)
    assert late.exit_code == 4
    assert problem(late)["type"] == "urn:football-results:cli:wait-timeout"


@pytest.mark.parametrize(
    ("handler", "slug"),
    [
        (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")), "connection-failed"),
        (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "timeout"),
        (lambda request: httpx.Response(502, text="<html>Bad gateway</html>"), "invalid-response"),
        (lambda request: httpx.Response(200, text="not json"), "invalid-response"),
        (
            lambda request: httpx.Response(301, headers={"Location": "https://api.example/"}),
            "invalid-response",
        ),
    ],
)
def test_network_and_protocol_failures_exit_4(
    monkeypatch: pytest.MonkeyPatch, handler, slug: str
) -> None:
    mock_api(monkeypatch, handler)
    result = invoke("health")

    assert result.exit_code == 4
    assert result.stdout == ""
    assert problem(result)["type"] == f"urn:football-results:cli:{slug}"


def test_gets_retry_429_and_503_with_a_short_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    limited = {"type": "x/problems/rate-limited", "title": "Too many", "status": 429}
    answers = [
        httpx.Response(429, json=limited, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"status": "ok"}),
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(cli, "_sleep", sleeps.append)
    sent = mock_api(monkeypatch, lambda request: answers.pop(0))

    assert invoke("health").exit_code == 0
    assert (len(sent), sleeps) == (2, [2.0])

    sent = mock_api(
        monkeypatch,
        lambda request: httpx.Response(429, json=limited, headers={"Retry-After": "120"}),
    )
    result = invoke("health")
    assert (result.exit_code, len(sent)) == (4, 1)
    assert problem(result) == limited

    sent = mock_api(
        monkeypatch, lambda request: httpx.Response(429, json=limited, headers={"Retry-After": "1"})
    )
    assert invoke("refresh", "-c", "ekstraklasa").exit_code == 4
    assert len(sent) == 1
    assert invoke("health").exit_code == 4
    assert len(sent) == 1 + 1 + http.MAX_RETRIES


def test_retry_after_accepts_an_http_date() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    response = httpx.Response(503, headers={"Retry-After": "Thu, 01 Jan 2026 12:00:10 GMT"})
    assert http.retry_delay(response, now) == 10.0
    assert http.retry_delay(httpx.Response(503, headers={"Retry-After": "soon"}), now) is None
    assert http.retry_delay(httpx.Response(500, headers={"Retry-After": "1"}), now) is None


def test_usage_errors_are_problem_details_with_exit_2(requests) -> None:
    for args, parameter in (
        (("matches", "--limit", "0"), "--limit"),
        (("matches", "--order", "sideways"), "--order"),
        (("standings",), "--competition"),
        (("matches", "--bogus"), None),
        (("dance",), None),
        ((), None),
    ):
        result = invoke(*args)
        assert result.exit_code == 2, args
        assert result.stdout == ""
        body = problem(result)
        assert body["type"] == "urn:football-results:cli:usage-error"
        assert body.get("parameter") == parameter
        assert "--help" in body["hint"]
    assert requests == []


# --- Configuration -------------------------------------------------------------------------


def test_url_and_timeout_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    configs: list[http.Config] = []
    sent: list[httpx.Request] = []

    def create(config: http.Config) -> httpx.Client:
        configs.append(config)
        transport = httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json=[])
        )
        return httpx.Client(transport=transport, base_url=config.url)

    monkeypatch.setattr(http, "create_http_client", create)
    env = {"FOOTBALL_RESULTS_URL": "https://api.example/prefix/", "FOOTBALL_RESULTS_TIMEOUT": "5"}
    assert invoke("competitions", env=env).exit_code == 0
    assert str(sent[0].url) == "https://api.example/prefix/api/v1/competitions"
    assert configs[0] == http.Config("https://api.example/prefix", None, 5.0)

    assert invoke("--url", "http://other.example:9000", "competitions", env=env).exit_code == 0
    assert str(sent[1].url) == "http://other.example:9000/api/v1/competitions"

    assert invoke("competitions").exit_code == 0
    assert configs[-1].url == http.DEFAULT_URL

    for bad in ("ftp://api.example", "http://user:pw@api.example", "https://api.example/?a=1"):
        result = invoke("--url", bad, "competitions")
        assert result.exit_code == 2
        assert problem(result)["parameter"] == "url"


def test_http_client_identifies_itself_and_does_not_follow_redirects() -> None:
    client = http.create_http_client(http.Config("https://api.example", "secret", 30.0))

    assert client.headers["User-Agent"] == f"football-results-cli/{version('football-results-cli')}"
    assert "X-API-Key" not in client.headers
    assert client.follow_redirects is False
    assert (client.timeout.read, client.timeout.connect) == (30.0, 10.0)


def test_cli_environment_variables_are_not_server_settings() -> None:
    assert not {"url", "api_key", "timeout"} & set(Settings.model_fields)


def test_cli_and_api_versions_match() -> None:
    assert version("football-results-cli") == version("football-results-api")
    result = invoke("--version")
    assert result.stdout == f"football-results {version('football-results-cli')}\n"


# --- Contract with the API -----------------------------------------------------------------


def test_every_command_has_help_with_examples() -> None:
    root = invoke("--help")
    assert root.exit_code == 0 and "Examples:" in root.stdout
    for name in cli.OPERATIONS:
        result = invoke(name, "--help")
        assert result.exit_code == 0, name
        assert "Examples:" in result.stdout, name
        assert f"football-results {name}" in result.stdout.split("Examples:")[1], name


def _api_parameters(spec: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    names = {item["name"] for item in operation.get("parameters", ())}
    body = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if body:
        schema = body["schema"]
        if "$ref" in schema:
            schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
        names |= set(schema["properties"])
    return names


def test_commands_cover_every_operation_with_its_parameter_names() -> None:
    spec = api_app.openapi()
    operations = {
        operation["operationId"]: operation
        for path in spec["paths"].values()
        for operation in path.values()
        if not operation.get("deprecated")
    }
    assert set(cli.OPERATIONS.values()) == set(operations)
    for name, operation_id in cli.OPERATIONS.items():
        command = cli.command.commands[name]
        options = {param.name for param in command.params} - cli.CLI_ONLY_OPTIONS[name]
        assert options == _api_parameters(spec, operations[operation_id]), name
        for param in command.params:
            flags = [opt for opt in param.opts if opt.startswith("--")]
            if flags and param.name not in ("json_output", "all_pages"):
                assert flags[0] == f"--{param.name.replace('_', '-')}", flags


def test_client_side_fields_match_the_api_models() -> None:
    assert cli.COMPETITION_FIELDS == tuple(CompetitionSummary.model_fields)
    assert cli.TEAM_FIELDS == tuple(Team.model_fields)
