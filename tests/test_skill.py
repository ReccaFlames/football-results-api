"""The consumer Agent Skill (`skills/football-results/SKILL.md`) must match the CLI and the API."""

import re
import shlex
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from football_results_cli import client as cli_http
from football_results_cli import main as cli
from football_results_cli import problems
from typer.testing import CliRunner

from app import agent_docs
from app.caching import CACHEABLE_PAGES
from app.config import load_config
from app.main import app
from app.models import PROBLEM_TYPES

SKILL = agent_docs.SKILL_FILE.read_text(encoding="utf-8")
FRONTMATTER, BODY = re.fullmatch(r"---\n(.*?)\n---\n(.*)", SKILL, re.DOTALL).groups()
CODE_LINES = [
    line
    for block in re.findall(r"^```[a-z]*\n(.*?)^```", BODY, re.DOTALL | re.MULTILINE)
    for line in block.splitlines()
]
COMMANDS = [line for line in CODE_LINES if line.startswith(f"{agent_docs.CLI_NAME} ")]


def test_frontmatter_follows_the_agent_skills_specification() -> None:
    meta = yaml.safe_load(FRONTMATTER)

    assert set(meta) <= {"name", "description", "license", "compatibility", "metadata"}
    assert meta["name"] == agent_docs.SKILL_NAME == agent_docs.SKILL_FILE.parent.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"]) and len(meta["name"]) <= 64
    assert 1 <= len(meta["description"]) <= 1024
    assert "Use when" in meta["description"]
    assert 1 <= len(meta.get("compatibility", "x")) <= 500
    assert len(SKILL.splitlines()) < 500


def test_skill_commands_run_against_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": 42, "status": "queued"})
        if path.startswith("/api/v1/jobs/"):
            return httpx.Response(200, json={"job_id": 42, "status": "completed"})
        if path.endswith("/competitions"):
            return httpx.Response(200, json=[])
        page = {"data": [], "pagination": {"limit": 1, "next_cursor": None}, "freshness": []}
        return httpx.Response(200, json=page)

    monkeypatch.setattr(
        cli_http,
        "create_http_client",
        lambda config: httpx.Client(transport=httpx.MockTransport(respond), base_url=config.url),
    )
    env = {"FOOTBALL_RESULTS_API_KEY": "test-key"}
    assert len(COMMANDS) >= 10
    for line in COMMANDS:
        words = shlex.split(line.split(" | ")[0])
        result = CliRunner().invoke(cli.app, ["--url", "http://api.test", *words[1:]], env=env)

        assert result.exit_code == 0, (line, result.stderr)
    assert sent and {request.url.host for request in sent} == {"api.test"}


def test_skill_mentions_only_real_options_and_example_slugs() -> None:
    options = {
        name
        for command in [cli.command, *cli.command.commands.values()]
        for param in command.params
        if param.param_type_name == "option"
        for name in param.opts
    }
    mentioned = set(re.findall(r"(?<![\w-])(--?[a-z][a-z-]*)", "\n".join([BODY, *COMMANDS])))
    other_tools = {"-fsSL", "-u", "--arg"}  # curl, date, and jq in the examples

    assert mentioned - other_tools <= options, mentioned - other_tools - options
    values: dict[str, set[str]] = {"-c": set(), "--team": set()}
    for line in COMMANDS:
        words = shlex.split(line.split(" | ")[0])
        for flag, value in pairwise(words):
            values.get(flag, set()).add(value)
    operations = agent_docs.operations_from_openapi(app.openapi())
    matches = next(item for item in operations if item.operation_id == "list_matches")
    assert values["-c"] and values["-c"] <= set(load_config().to_competitions())
    assert values["--team"] == {matches.parameter("team").example}


def test_exit_codes_and_problem_types_match_the_cli_and_the_api() -> None:
    rows = re.findall(r"^\| (\d) \| ([^|:]+?)[:|]", BODY, re.MULTILINE)
    cli_codes = {code: meaning.split(":")[0] for code, meaning in agent_docs.CLI_EXIT_CODES}

    assert {int(code): meaning.strip() for code, meaning in rows} == cli_codes
    assert set(cli_codes) == {
        problems.EXIT_OK,
        problems.EXIT_USAGE,
        problems.EXIT_NOT_FOUND,
        problems.EXIT_UPSTREAM,
        problems.EXIT_AUTH,
    }
    cli_slugs = set(re.findall(r"urn:football-results:cli:([a-z-]+)", BODY))
    cli_slugs |= set(re.findall(r"`([a-z]+-[a-z-]+)`:", BODY))
    assert cli_slugs and cli_slugs <= set(problems.CLI_PROBLEM_TYPES)
    server_slugs = set(re.findall(r"/problems/([a-z-]+)", BODY))
    assert server_slugs and server_slugs <= set(PROBLEM_TYPES)


def test_links_resolve_to_real_routes_and_anchors() -> None:
    routes = {getattr(route, "path", None) for route in app.routes}
    links = re.findall(r"\$FOOTBALL_RESULTS_URL(/[^\s`\"?#)]*)(#[a-z-]+)?", BODY)
    agents_page = TestClient(app).get("/agents").text

    assert {path for path, _ in links} >= {"/llms.txt", "/agents.md", "/problems"}
    for path, anchor in links:
        assert path in routes, path
        if anchor:
            assert f'id="{anchor[1:]}"' in agents_page, anchor
    headings = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^#+ (.+)$", BODY, re.MULTILINE)
    }
    assert set(re.findall(r"\]\(#([a-z-]+)\)", BODY)) <= headings


def test_skill_is_served_unchanged_with_etag_and_page_headers() -> None:
    client = TestClient(app)
    response = client.get(agent_docs.SKILL_PATH)

    assert response.status_code == 200
    assert response.content == agent_docs.SKILL_FILE.read_bytes()
    assert response.headers["content-type"] == agent_docs.MARKDOWN_MEDIA_TYPE
    assert response.headers["link"] == '<http://testserver/llms.txt>; rel="describedby"'
    assert response.headers["cache-control"].startswith("public")
    assert "ratelimit" in response.headers
    etag = response.headers["etag"]
    cached = client.get(agent_docs.SKILL_PATH, headers={"If-None-Match": etag})
    assert cached.status_code == 304 and cached.content == b""
    assert agent_docs.SKILL_PATH in CACHEABLE_PAGES
    assert agent_docs.SKILL_PATH not in app.openapi()["paths"]
    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
    assert "COPY skills ./skills" in dockerfile.read_text()
