import re
from ipaddress import ip_network

import pytest
from pydantic import ValidationError

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
    assert agent_docs.PLANNED_TOOLS_NOTE in lines


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


def test_tool_links_replace_the_planned_note_once_they_exist() -> None:
    tool = agent_docs.ToolLink("CLI", f"{BASE_URL}/cli", "the football-results command")
    text = agent_docs.llms_txt(guide(tools=[tool]))

    assert agent_docs.PLANNED_TOOLS_NOTE not in text
    assert f"## Tools\n\n- [CLI]({BASE_URL}/cli): the football-results command" in text
