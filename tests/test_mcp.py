import json
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import access
from app.agent_docs import MCP_TOOLS
from app.main import app
from app.mcp_server import DESCRIPTION_BUDGET, FORM_MAX
from app.models import Freshness, FreshnessSource
from app.ratelimit import FixedWindowRateLimiter
from tests.test_api import FakeRepository, fixture

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> FakeRepository:
    monkeypatch.setattr(main_module.settings, "api_keys", "test-api-key")
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())
    repository = FakeRepository()
    repository.matches = [
        fixture(1, "Wisła Płock", "Legia", (2, 0), days=0),
        fixture(2, "Lech", "Wisła Kraków", (1, 1), days=0),
        fixture(3, "Legia", "Lech", (0, 1), days=7, round_number=2),
        fixture(4, "Wisła Kraków", "Wisła Płock", days=14, round_number=3),
        fixture(5, "Lech", "Legia", (3, 3), days=14, round_number=3),
    ]
    fetched = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    repository.freshness = [
        Freshness(
            competition="ekstraklasa",
            season="2026-2027",
            data_as_of=fetched,
            next_update_at=fetched + timedelta(days=1),
            automatic_refresh=True,
            scheduler_state="idle",
            source=FreshnessSource(provider="90minut", url="http://www.90minut.pl/liga/1/l.html"),
        )
    ]
    monkeypatch.setattr(main_module, "store", repository)
    return repository


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def post(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    name = (params or {}).get("name")
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {**(params or {}), "_meta": META},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": VERSION,
            "mcp-method": method,
            **({"mcp-name": name} if name else {}),
            **(headers or {}),
        },
    )


def tools(client: TestClient) -> dict[str, dict[str, Any]]:
    response = post(client, "tools/list")
    assert response.status_code == 200
    return {tool["name"]: tool for tool in response.json()["result"]["tools"]}


def call(client: TestClient, name: str, headers: dict[str, str] | None = None, **arguments: Any):
    response = post(client, "tools/call", {"name": name, "arguments": arguments}, headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    content = json.loads(result["content"][0]["text"])
    if result["isError"]:
        assert "structuredContent" not in result
        return None, content
    assert content == result["structuredContent"]
    schema = tools(client)[name]["outputSchema"]
    jsonschema.validate(result["structuredContent"], schema)
    return result["structuredContent"], None


def test_tools_list_is_curated_described_and_annotated(client: TestClient) -> None:
    listed = tools(client)

    assert list(listed) == list(MCP_TOOLS)
    assert sum(len(tool["description"]) for tool in listed.values()) <= DESCRIPTION_BUDGET
    for tool in listed.values():
        assert tool["title"] and tool["outputSchema"]["type"] == "object"
        for parameter in tool["inputSchema"].get("properties", {}).values():
            assert parameter.get("description")
    assert all(listed[name]["annotations"]["readOnlyHint"] for name in list(MCP_TOOLS)[:4])
    refresh = listed["request_refresh"]["annotations"]
    assert refresh["readOnlyHint"] is False and refresh["destructiveHint"] is False
    assert listed["find_matches"]["inputSchema"]["properties"]["limit"]["maximum"] == 200
    assert listed["get_team_form"]["inputSchema"]["properties"]["last"]["maximum"] == FORM_MAX


def test_read_tools_return_the_http_responses(client: TestClient) -> None:
    competitions, _ = call(client, "list_competitions")
    assert competitions == {"competitions": client.get("/api/v1/competitions").json()}

    matches, _ = call(client, "find_matches", competition="ekstraklasa", limit=2)
    http = client.get("/api/v1/matches?competition=ekstraklasa&limit=2").json()
    assert matches == http
    assert matches["freshness"][0]["competition"] == "ekstraklasa"

    page, _ = call(
        client,
        "find_matches",
        cursor=http["pagination"]["next_cursor"],
        competition="ekstraklasa",
        limit=2,
    )
    assert [item["id"] for item in page["data"]] == [3, 4]

    subset, _ = call(client, "find_matches", fields="home_goals", limit=1)
    assert subset["data"] == [{"id": 1, "home_goals": 2}]

    standings, _ = call(client, "get_standings", competition="ekstraklasa", fields="points")
    assert standings == client.get("/api/v1/standings?competition=ekstraklasa&fields=points").json()


@pytest.mark.parametrize(
    ("name", "arguments", "slug", "parameter"),
    [
        ("find_matches", {"competition": "nope"}, "unknown-competition", "competition"),
        (
            "get_standings",
            {"competition": "ekstraklasa", "season": "1999-2000"},
            "unknown-season",
            "season",
        ),
        ("find_matches", {"cursor": "garbage"}, "invalid-cursor", "cursor"),
        ("find_matches", {"status": "postponed"}, "invalid-parameter", "status"),
        ("get_team_form", {"team": "legia", "last": FORM_MAX + 1}, "invalid-parameter", "last"),
    ],
)
def test_errors_are_tool_results_with_the_api_problem(
    client: TestClient, name: str, arguments: dict[str, Any], slug: str, parameter: str
) -> None:
    _, problem = call(client, name, **arguments)

    assert problem["type"] == f"/problems/{slug}"
    assert problem.get("parameter") == parameter
    assert isinstance(problem["detail"], str)


def test_unknown_tool_is_a_protocol_error(client: TestClient) -> None:
    response = post(client, "tools/call", {"name": "get_results", "arguments": {}})

    assert response.json()["error"]["code"] == -32602
    assert "result" not in response.json()


def test_team_form_summarises_finished_matches(client: TestClient) -> None:
    form, _ = call(client, "get_team_form", team="Legia", last=3)

    assert form["team"]["slug"] == "legia"
    assert [match["id"] for match in form["matches"]] == [5, 3, 1]
    assert [match["result"] for match in form["matches"]] == ["D", "L", "L"]
    assert form["matches"][0] == {
        "id": 5,
        "competition": "ekstraklasa",
        "season": "2026-2027",
        "kickoff_at": form["matches"][0]["kickoff_at"],
        "venue": "away",
        "opponent": "Lech",
        "opponent_slug": "lech",
        "goals_for": 3,
        "goals_against": 3,
        "result": "D",
    }
    assert form["summary"] == {
        "played": 3,
        "won": 0,
        "drawn": 1,
        "lost": 2,
        "goals_for": 3,
        "goals_against": 6,
        "points": 1,
        "form": "DLL",
    }
    assert form["freshness"][0]["competition"] == "ekstraklasa"


def test_team_form_skips_matches_without_goals(isolated: FakeRepository, client: TestClient):
    isolated.matches[0] = isolated.matches[0].model_copy(update={"home_goals": None})

    form, _ = call(client, "get_team_form", team="wisla-plock")

    assert form["matches"][0]["goals_for"] is None and form["matches"][0]["result"] is None
    assert form["summary"]["played"] == 0 and form["summary"]["form"] == ""


def test_ambiguous_team_lists_candidate_slugs(client: TestClient) -> None:
    _, problem = call(client, "get_team_form", team="Wisła")

    assert problem["type"] == "/problems/invalid-parameter"
    assert problem["parameter"] == "team"
    assert problem["valid_values"] == ["wisla-krakow", "wisla-plock"]

    _, missing = call(client, "get_team_form", team="Nobody")
    assert missing["parameter"] == "team"


def test_refresh_needs_the_api_key_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, missing = call(client, "request_refresh", competition="ekstraklasa")
    _, wrong = call(client, "request_refresh", {"X-API-Key": "wrong"}, competition="ekstraklasa")
    job, _ = call(
        client, "request_refresh", {"X-API-Key": "test-api-key"}, competition="ekstraklasa"
    )

    assert missing["type"] == wrong["type"] == "/problems/unauthorized"
    assert job["job_id"] and job["status"] == "queued"

    monkeypatch.setattr(main_module.settings, "api_keys", "")
    _, disabled = call(
        client, "request_refresh", {"X-API-Key": "test-api-key"}, competition="ekstraklasa"
    )
    assert disabled["status"] == 503


def test_transport_is_stateless_and_uncached(client: TestClient) -> None:
    response = post(client, "tools/list")

    assert "mcp-session-id" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert "/mcp" not in app.openapi()["paths"]
    get = client.get("/mcp", headers={"mcp-protocol-version": VERSION})
    assert get.status_code == 405


def test_host_and_origin_are_validated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert post(client, "tools/list", headers={"host": "evil.example"}).status_code == 421
    assert post(client, "tools/list", headers={"origin": "https://evil.example"}).status_code == 403
    assert (
        post(client, "tools/list", headers={"origin": "http://localhost:3000"}).status_code == 200
    )

    monkeypatch.setattr(main_module.settings, "mcp_allowed_hosts", "football.example.com")
    monkeypatch.setattr(main_module.settings, "public_base_url", "https://api.example.org")
    assert post(client, "tools/list", headers={"host": "football.example.com"}).status_code == 200
    public = post(
        client,
        "tools/list",
        headers={"host": "api.example.org", "origin": "https://api.example.org"},
    )
    assert public.status_code == 200


def test_a_call_counts_once_as_a_read_and_refresh_as_a_write(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.settings, "rate_limit_read_per_minute", 2)
    monkeypatch.setattr(main_module.settings, "rate_limit_write_per_minute", 1)
    form = post(client, "tools/call", {"name": "get_team_form", "arguments": {"team": "legia"}})
    assert form.headers["ratelimit"].startswith('"read";r=1;')

    key = {"X-API-Key": "test-api-key"}
    arguments = {"competition": "ekstraklasa"}
    first = post(client, "tools/call", {"name": "request_refresh", "arguments": arguments}, key)
    second = post(client, "tools/call", {"name": "request_refresh", "arguments": arguments}, key)
    assert first.json()["result"]["isError"] is False
    second_problem = json.loads(second.json()["result"]["content"][0]["text"])
    assert second_problem["type"] == "/problems/rate-limited"

    assert post(client, "tools/list").status_code == 200
    limited = post(client, "tools/list")
    assert limited.status_code == 429
    assert limited.json()["type"] == "/problems/rate-limited"


def test_agent_docs_link_the_mcp_server(client: TestClient) -> None:
    assert "/agents#mcp" in client.get("/llms.txt").text
    guide = client.get("/agents.md").text
    assert "## MCP server" in guide
    assert "copilot mcp add --transport http football-results http://localhost/mcp" in guide
    for name in MCP_TOOLS:
        assert f"`{name}`" in guide


def test_server_json_is_valid_and_versioned() -> None:
    server = json.loads((ROOT / "server.json").read_text())
    schema = json.loads((ROOT / "tests/schemas/server.schema.json").read_text())

    jsonschema.Draft7Validator(schema).validate(server)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert server["version"] == project["version"]
    assert server["remotes"][0]["url"].endswith("/mcp")


def test_a_client_key_selects_the_quota_and_cannot_refresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = bytes.fromhex(access.hash_key("client-key").removeprefix(access.HASH_PREFIX))
    entry = access.FileKey("partner-app", digest, "client", 5, None)
    monkeypatch.setattr(main_module, "file_keys", (entry,))
    key = {"X-API-Key": "client-key"}

    listed = post(client, "tools/list", headers=key)
    assert listed.headers["ratelimit-policy"] == '"read";q=5;w=60'
    _, forbidden = call(client, "request_refresh", key, competition="ekstraklasa")
    assert forbidden["type"] == "/problems/forbidden"
    assert forbidden["status"] == 403
