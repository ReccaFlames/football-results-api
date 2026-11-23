import re
import shlex
from ipaddress import ip_network

import httpx
import pytest
from fastapi.testclient import TestClient
from football_results_cli import client as cli_http
from football_results_cli import main as cli
from football_results_cli import problems
from pydantic import ValidationError
from typer.testing import CliRunner

from app import agent_docs
from app.competitions import competition_summary
from app.config import Settings, load_config
from app.main import app
from app.models import PROBLEM_TYPES, Match, StandingRow

BASE_URL = "https://football.example/api-root"
COMPETITIONS = [competition_summary(item) for item in load_config().to_competitions().values()]


def guide(**overrides) -> agent_docs.AgentGuide:
    arguments = {
        "base_url": BASE_URL,
        "competitions_live": True,
        "rate_limits": agent_docs.RateLimits(True, 120, 10),
        "cache_max_age_seconds": 60,
    } | overrides
    return agent_docs.build_guide(app.openapi(), COMPETITIONS, **arguments)


def outside_code(text: str) -> list[str]:
    lines, in_code = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code:
            lines.append(line)
    return lines


def test_problem_guidance_covers_every_problem_type() -> None:
    assert set(agent_docs.PROBLEM_GUIDANCE) == set(PROBLEM_TYPES)


def test_llms_txt_follows_the_llmstxt_format() -> None:
    lines = [line for line in outside_code(agent_docs.llms_txt(guide())) if line.strip()]

    assert lines[0] == "# Football Results API"
    assert lines[1].startswith("> ")
    assert not any(line.startswith("# ") for line in lines[1:])
    first_section = next(index for index, line in enumerate(lines) if line.startswith("## "))
    assert not any(line.startswith("#") for line in lines[2:first_section])
    for line in lines[first_section:]:
        assert line.startswith("## ") or re.fullmatch(
            rf"- \[[^\]]+\]\({re.escape(BASE_URL)}/[^)\s]*\)(: .+)?", line
        ), line


def test_guides_mention_every_operation_and_no_unknown_ones() -> None:
    operation_ids = {
        item.operation_id for item in agent_docs.operations_from_openapi(app.openapi())
    }
    rendered = guide()
    reference = agent_docs.agent_guide_markdown(rendered)
    mentioned = set()
    for text in (agent_docs.llms_txt(rendered), reference, agent_docs.llms_full_txt(rendered)):
        mentioned |= set(re.findall(r"`((?:list|get|request|fetch)_[a-z_]+)`", text))

    assert all(f"`{operation_id}`" in reference for operation_id in operation_ids)
    assert mentioned <= operation_ids
    assert all(f"/problems/{slug}" in reference for slug in PROBLEM_TYPES)


def test_task_examples_use_documented_paths_parameters_and_fields() -> None:
    spec = app.openapi()
    tasks = guide().tasks
    item_fields = {
        "list_matches": set(Match.model_fields),
        "get_standings": set(StandingRow.model_fields),
    }

    assert len(tasks) == 5
    for task in tasks:
        documented = {
            item["name"] for item in spec["paths"][task.operation.path]["get"].get("parameters", [])
        }
        query = dict(task.query)
        assert set(query) <= documented
        assert query.get("competition", "ekstraklasa") == "ekstraklasa"
        assert set(query.get("status", "finished").split(",")) <= {"finished", "scheduled"}
        if "fields" in query:
            assert set(query["fields"].split(",")) <= item_fields[task.operation.operation_id]
        assert task.curl(BASE_URL).startswith(f'curl -s "{BASE_URL}{task.operation.path}')


def test_tasks_reject_parameters_that_the_spec_does_not_document() -> None:
    operations = [
        operation
        if operation.operation_id != "list_teams"
        else agent_docs.Operation(**{**operation.__dict__, "parameters": ()})
        for operation in agent_docs.operations_from_openapi(app.openapi())
    ]

    with pytest.raises(ValueError, match="list_teams has no parameter"):
        agent_docs.top_tasks(operations, COMPETITIONS)


def test_operations_are_extracted_with_auth_parameters_and_problem_types() -> None:
    operations = {
        item.operation_id: item for item in agent_docs.operations_from_openapi(app.openapi())
    }

    refresh = operations["request_refresh"]
    assert refresh.requires_api_key and (refresh.method, refresh.path) == (
        "POST",
        "/api/v1/results/refresh",
    )
    assert refresh.parameter("competition").location == "body"
    assert {"unauthorized", "writes-disabled", "rate-limited"} <= set(refresh.problem_types)
    assert not operations["list_matches"].requires_api_key
    assert operations["list_matches"].parameter("order").values == ("asc", "desc")
    assert operations["list_results"].deprecated


def test_request_base_url_trusts_forwarded_headers_only_from_trusted_proxies() -> None:
    def base_url(peer: str, host: str | None = "api.internal:8000", root_path: str = "") -> str:
        return agent_docs.request_base_url(
            scheme="http",
            host=host,
            root_path=root_path,
            peer=peer,
            forwarded_proto="https",
            forwarded_host="football.example, proxy.internal",
            trusted_proxies=(ip_network("10.0.0.0/8"),),
        )

    assert base_url("203.0.113.9") == "http://api.internal:8000"
    assert base_url("10.0.0.2", root_path="/football/") == "https://football.example/football"
    assert base_url("203.0.113.9", host='evil.example/"><script>') == "http://localhost"
    assert base_url("203.0.113.9", host=None) == "http://localhost"


def test_public_base_url_setting_is_validated() -> None:
    assert Settings(public_base_url="https://football.example/v/").public_base_url == (
        "https://football.example/v"
    )
    for invalid in ("football.example", "ftp://football.example", "https://x.example/?a=1"):
        with pytest.raises(ValidationError):
            Settings(public_base_url=invalid)


def test_html_rendering_escapes_text_and_keeps_inline_markup() -> None:
    html = agent_docs.inline_html("**Use** `<b>` & [docs](https://x.example/a?b=1&c=2) <script>")

    assert html == (
        "<strong>Use</strong> <code>&lt;b&gt;</code> &amp; "
        '<a href="https://x.example/a?b=1&amp;c=2">docs</a> &lt;script&gt;'
    )


def test_tool_links_are_listed_in_a_tools_section() -> None:
    tools = [agent_docs.cli_tool(BASE_URL), agent_docs.skill_tool(BASE_URL)]
    text = agent_docs.llms_txt(guide(tools=tools))
    page = agent_docs.agent_guide_markdown(guide(tools=tools))
    html = agent_docs.agent_guide_html(guide(tools=tools))

    assert (
        f"## Tools\n\n- [football-results CLI]({BASE_URL}/agents#cli): command-line client"
    ) in text
    skill_link = f"- [football-results Agent Skill]({BASE_URL}/skills/football-results/SKILL.md): "
    assert skill_link in text and skill_link in page
    assert "## Agent Skill" in page and 'id="skill"' in html
    assert f"copilot skill add {BASE_URL}/skills/football-results/SKILL.md" in page
    assert f"export FOOTBALL_RESULTS_URL={BASE_URL}" in page
    assert "## Tools" not in agent_docs.llms_txt(guide())
    assert "planned" not in (text + page).lower()


def test_cli_tool_adds_cli_examples_and_a_cli_section() -> None:
    rendered = guide(tools=[agent_docs.cli_tool(BASE_URL)])
    text = agent_docs.llms_txt(rendered)
    page = agent_docs.render_markdown(agent_docs.agent_guide_blocks(rendered))
    html = agent_docs.render_html(agent_docs.agent_guide_blocks(rendered), title="t", links={})

    for task in rendered.tasks:
        assert f"```sh\n{task.cli()}\n```" in text
        assert task.cli() in page
    assert "football-results matches --competition ekstraklasa --team" in text
    assert '--date-from "$(date -u +%F)"' in text
    assert 'id="cli"' in html
    assert "## Command-line client" in page and "`urn:football-results:cli:stale-data`" in page
    assert "CLI: `football-results standings`" in page
    assert "## Agent Skill" not in page
    assert "football-results" not in agent_docs.llms_txt(guide())


def test_served_guides_advertise_the_cli_and_the_skill() -> None:
    client = TestClient(app)
    llms = client.get("/llms.txt").text
    agents = client.get("/agents").text

    assert "[football-results CLI](http://testserver/agents#cli)" in llms
    assert (
        "[football-results Agent Skill](http://testserver/skills/football-results/SKILL.md)" in llms
    )
    assert 'id="cli"' in agents and 'id="skill"' in agents


def test_cli_names_match_the_cli_package() -> None:
    assert agent_docs.CLI_NAME == cli.PROG_NAME
    assert agent_docs.CLI_COMMANDS == {
        operation: command for command, operation in cli.OPERATIONS.items()
    }
    assert agent_docs.CLI_PROBLEM_PREFIX == problems.CLI_PROBLEM_PREFIX
    assert {slug: code for slug, (code, _) in agent_docs.CLI_PROBLEM_GUIDANCE.items()} == {
        slug: code for slug, (_, code) in problems.CLI_PROBLEM_TYPES.items()
    }
    assert [code for code, _ in agent_docs.CLI_EXIT_CODES] == [
        problems.EXIT_OK,
        problems.EXIT_USAGE,
        problems.EXIT_NOT_FOUND,
        problems.EXIT_UPSTREAM,
        problems.EXIT_AUTH,
    ]


def test_cli_task_examples_send_the_same_request_as_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        page = {"data": [], "pagination": {"limit": 1, "next_cursor": None}, "freshness": []}
        return httpx.Response(200, json=[] if request.url.path.endswith("competitions") else page)

    monkeypatch.setattr(
        cli_http,
        "create_http_client",
        lambda config: httpx.Client(transport=httpx.MockTransport(respond), base_url=config.url),
    )
    for task in guide().tasks:
        words = shlex.split(task.cli())
        assert words[0] == "football-results"
        result = CliRunner().invoke(cli.app, ["--url", BASE_URL, *words[1:]])

        assert result.exit_code == 0, (task.cli(), result.stderr)
        assert str(sent[-1].url.copy_with(query=None)) == f"{BASE_URL}{task.operation.path}"
        assert dict(sent[-1].url.params) == dict(task.query)


def test_demo_notice_keeps_the_llms_txt_format_and_lists_each_attribution_once() -> None:
    providers = {"openligadb": ["3-liga"]}
    demo = guide(demo=True, provider_competitions=providers)
    text = agent_docs.llms_txt(demo)
    lines = [line for line in outside_code(text) if line.strip()]

    assert lines[2].startswith("**Demo instance:** This is a public, read-only demo instance")
    assert "120 reads per minute per IP" in text
    assert text.count("Open Database License") == 1
    assert "Data attribution:" not in text
    first_section = next(index for index, line in enumerate(lines) if line.startswith("## "))
    assert not any(line.startswith("#") for line in lines[2:first_section])
    assert "## Demo instance" in agent_docs.agent_guide_markdown(demo)

    disabled = guide(demo=True, rate_limits=agent_docs.RateLimits(False, 120, 10))
    assert "Rate limiting is disabled on this instance." in agent_docs.llms_txt(disabled)
    assert "Data attribution:" in agent_docs.llms_txt(guide(provider_competitions=providers))


def test_private_instance_guides_send_the_api_key() -> None:
    public, private = agent_docs.llms_txt(guide()), agent_docs.llms_txt(guide(key_required=True))
    header = '-H "X-API-Key: $FOOTBALL_RESULTS_API_KEY"'
    assert header not in public
    assert header in private and "private instance" in private
    assert "API-Key-Expires" in private
