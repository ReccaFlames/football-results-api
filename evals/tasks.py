"""Eval tasks: a question for the agent, ground truth read from the API, and a scorer.

Ground truth is computed when the eval runs, so the tasks work on any instance and season. An
agent passes a task when the JSON on its final `ANSWER:` line matches the expected answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

Answer = dict[str, Any]
Score = tuple[bool, str]
FRESHNESS_TOLERANCE = timedelta(minutes=2)


@dataclass(frozen=True)
class Expected:
    """Ground truth for one task: `truth` must pass `score` (checked by --dry-run)."""

    prompt: str
    shape: str
    truth: Answer
    score: Callable[[Answer], Score]


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    build: Callable[[World], Expected]


class World:
    """Facts about the instance, read once per eval run."""

    def __init__(self, api: httpx.Client) -> None:
        self.api = api
        competitions = self.get("/api/v1/competitions")
        league = [c for c in competitions if c["format"] == "league" and c["current_season"]]
        if not league:
            raise RuntimeError("the instance has no league with a current season")
        self.competitions = competitions
        self.competition = league[0]
        self.slug = self.competition["slug"]
        self.name = self.competition["name"]
        self.season = self.competition["current_season"]
        self.table = self.get("/api/v1/standings", competition=self.slug, season=self.season)[
            "data"
        ]
        if len(self.table) < 3:
            raise RuntimeError(f"{self.slug} {self.season} has no table yet; refresh it first")
        self.teams = self.pages("/api/v1/teams", competition=self.slug, season=self.season)
        # The second-placed team: a leader is too easy to guess from general knowledge.
        self.team = self.table[1]

    def get(self, path: str, **params: Any) -> Any:
        response = self.api.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def pages(self, path: str, **params: Any) -> list[Answer]:
        items: list[Answer] = []
        cursor = None
        while True:
            body = self.get(path, limit=100, **params, **({"cursor": cursor} if cursor else {}))
            items.extend(body["data"])
            cursor = body["pagination"]["next_cursor"]
            if not cursor:
                return items

    def matches(self, **params: Any) -> list[Answer]:
        return self.pages("/api/v1/matches", competition=self.slug, season=self.season, **params)

    def same_team(self, given: Any, slug: str) -> bool:
        """Whether an answer names the team with this slug (name, slug, or alias)."""
        if not isinstance(given, str):
            return False
        team = next((t for t in self.teams if t["slug"] == slug), None)
        names = [slug] + ([team["name"], *team["aliases"]] if team else [])
        return _norm(given) in {_norm(name) for name in names}

    def team_check(self, answer: Answer, key: str, slug: str, name: str) -> Score:
        if self.same_team(answer.get(key), slug):
            return True, "ok"
        return False, f"{key}: got {answer.get(key)!r}, expected {name!r}"


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _equal(answer: Answer, truth: Answer, *keys: str) -> Score:
    for key in keys:
        if answer.get(key) != truth[key]:
            return False, f"{key}: got {answer.get(key)!r}, expected {truth[key]!r}"
    return True, "ok"


def _both(first: Score, second: Callable[[], Score]) -> Score:
    return second() if first[0] else first


def _unavailable(answer: Answer) -> Score:
    if answer.get("available") is False:
        return True, "ok"
    return False, f"expected available=false, got {answer.get('available')!r}"


def _typo(name: str) -> str:
    """Drop one inner letter of the longest word: 'Dresden' -> 'Dresen'."""
    words = name.split()
    longest = max(range(len(words)), key=lambda i: len(words[i]))
    word = words[longest]
    if len(word) >= 5:
        middle = len(word) // 2
        words[longest] = word[:middle] + word[middle + 1 :]
    return " ".join(words)


def _opponent(match: Answer, slug: str) -> tuple[str, str, bool]:
    home = match["home_team_slug"] == slug
    if home:
        return match["away_team_slug"], match["away_team"], home
    return match["home_team_slug"], match["home_team"], home


def competitions(world: World) -> Expected:
    slugs = sorted(c["slug"] for c in world.competitions)

    def score(answer: Answer) -> Score:
        given = answer.get("slugs")
        if isinstance(given, list) and sorted(map(str, given)) == slugs:
            return True, "ok"
        return False, f"slugs: got {given!r}, expected {slugs!r}"

    return Expected(
        "Which competitions does the football results service cover? Give their slugs.",
        '{"slugs": ["<competition slug>", ...]}',
        {"slugs": slugs},
        score,
    )


def leader(world: World) -> Expected:
    top = world.table[0]
    truth = {"team": top["team_name"], "points": top["points"]}
    return Expected(
        f"Who is top of the {world.name} table right now, and with how many points?",
        '{"team": "<team name>", "points": <int>}',
        truth,
        lambda answer: _both(
            world.team_check(answer, "team", top["team_slug"], top["team_name"]),
            lambda: _equal(answer, truth, "points"),
        ),
    )


def last_result(world: World) -> Expected:
    slug = world.team["team_slug"]
    finished = world.matches(team=slug, status="finished", order="desc")
    if not finished:
        raise RuntimeError(f"{slug} has no finished match")
    match = finished[0]
    opponent_slug, opponent, home = _opponent(match, slug)
    truth = {
        "opponent": opponent,
        "goals_for": match["home_goals"] if home else match["away_goals"],
        "goals_against": match["away_goals"] if home else match["home_goals"],
    }
    return Expected(
        f"What was {world.team['team_name']}'s most recent result in the {world.name}?",
        '{"opponent": "<team name>", "goals_for": <int>, "goals_against": <int>}',
        truth,
        lambda answer: _both(
            world.team_check(answer, "opponent", opponent_slug, opponent),
            lambda: _equal(answer, truth, "goals_for", "goals_against"),
        ),
    )


def next_fixture(world: World) -> Expected:
    slug = world.team["team_slug"]
    upcoming = [
        m for m in world.matches(team=slug, status="scheduled", order="asc") if m["kickoff_at"]
    ]
    prompt = (
        f"When and against whom do {world.team['team_name']} play their next {world.name} match?"
    )
    shape = '{"opponent": "<team name or null>", "date": "<YYYY-MM-DD or null>"}'
    if not upcoming:
        none = {"opponent": None, "date": None}
        return Expected(prompt, shape, none, lambda a: _equal(a, none, "opponent", "date"))
    opponent_slug, opponent, _ = _opponent(upcoming[0], slug)
    kickoff = datetime.fromisoformat(upcoming[0]["kickoff_at"])
    local = kickoff.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
    dates = {kickoff.date().isoformat(), local}

    def date_check(answer: Answer) -> Score:
        if answer.get("date") in dates:
            return True, "ok"
        return False, f"date: got {answer.get('date')!r}, expected one of {sorted(dates)}"

    return Expected(
        prompt,
        shape,
        {"opponent": opponent, "date": local},
        lambda answer: _both(
            world.team_check(answer, "opponent", opponent_slug, opponent),
            lambda: date_check(answer),
        ),
    )


def form(world: World) -> Expected:
    truth = {"form": world.team["form"]}

    def score(answer: Answer) -> Score:
        given = answer.get("form")
        if isinstance(given, str) and given.replace(" ", "").upper() == truth["form"]:
            return True, "ok"
        return False, f"form: got {given!r}, expected {truth['form']!r}"

    return Expected(
        f"What is {world.team['team_name']}'s form in the {world.name}: their last five "
        "results as W, D, or L, newest first?",
        '{"form": "<for example WDLWW>"}',
        truth,
        score,
    )


def misspelled_team(world: World) -> Expected:
    row = world.table[2]
    truth = {"team": row["team_name"], "points": row["points"]}
    return Expected(
        f"How many points does {_typo(row['team_name'])} have in the {world.name}? "
        "Give the team's correct name too.",
        '{"team": "<correct team name>", "points": <int>}',
        truth,
        lambda answer: _both(
            world.team_check(answer, "team", row["team_slug"], row["team_name"]),
            lambda: _equal(answer, truth, "points"),
        ),
    )


def unknown_competition(world: World) -> Expected:
    names = {_norm(c["name"]) for c in world.competitions}
    league = next(n for n in ("Serie A", "Eredivisie", "Liga Portugal") if _norm(n) not in names)
    return Expected(
        f"Who leads the {league} table right now?",
        '{"available": <false if this service has no data for it>, "team": "<name or null>"}',
        {"available": False, "team": None},
        _unavailable,
    )


def missing_season(world: World) -> Expected:
    return Expected(
        f"Who won the {world.name} in the 1990-1991 season?",
        '{"available": <false if this service has no data for it>, "team": "<name or null>"}',
        {"available": False, "team": None},
        _unavailable,
    )


def season_by_year(world: World) -> Expected:
    start = world.season.split("-")[0]
    truth = {"season": world.season, "teams": len(world.table)}
    return Expected(
        f"How many teams are in the {world.name} table for the season that started in {start}?",
        '{"season": "<season as the service names it>", "teams": <int>}',
        truth,
        lambda answer: _equal(answer, truth, "season", "teams"),
    )


def freshness(world: World) -> Expected:
    def data_as_of() -> str | None:
        body = world.get(
            "/api/v1/standings", competition=world.slug, season=world.season, fields="position"
        )
        return body["freshness"][0]["data_as_of"]

    before = data_as_of()

    def score(answer: Answer) -> Score:
        try:
            given = datetime.fromisoformat(str(answer.get("data_as_of")))
        except ValueError:
            return False, f"data_as_of: not a timestamp: {answer.get('data_as_of')!r}"
        if given.tzinfo is None:
            return False, f"data_as_of: no time zone: {answer.get('data_as_of')!r}"
        # A scheduled refresh during the run moves the timestamp, so the newer one counts too.
        for value in {before, data_as_of()}:
            if value and abs(given - datetime.fromisoformat(value)) <= FRESHNESS_TOLERANCE:
                return True, "ok"
        return False, f"data_as_of: got {given.isoformat()}, expected {before}"

    return Expected(
        f"How up to date is the {world.name} data: when was it last updated?",
        '{"data_as_of": "<ISO 8601 timestamp with time zone>"}',
        {"data_as_of": before},
        score,
    )


def finished_count(world: World) -> Expected:
    truth = {"finished": len(world.matches(status="finished", fields="id"))}
    return Expected(
        f"How many {world.name} matches of the {world.season} season have been played so far?",
        '{"finished": <int>}',
        truth,
        lambda answer: _equal(answer, truth, "finished"),
    )


TASKS = [
    Task("competitions", "top task", competitions),
    Task("leader", "top task", leader),
    Task("last-result", "top task", last_result),
    Task("next-fixture", "top task", next_fixture),
    Task("form", "top task", form),
    Task("misspelled-team", "error recovery", misspelled_team),
    Task("unknown-competition", "error recovery", unknown_competition),
    Task("missing-season", "error recovery", missing_season),
    Task("season-by-year", "error recovery", season_by_year),
    Task("freshness", "freshness", freshness),
    Task("finished-count", "pagination", finished_count),
]

ANSWER_LINE = re.compile(r"^\s*ANSWER:\s*(\{.*\})\s*$", re.MULTILINE)


def prompt_for(expected: Expected) -> str:
    return (
        f"{expected.prompt}\n\n"
        "Use the football results tools available to you; do not answer from memory. "
        "Finish your reply with one line that starts with `ANSWER:` followed by JSON of "
        f"exactly this shape: {expected.shape}"
    )


def parse_answer(output: str) -> Answer | None:
    """The JSON object on the last `ANSWER:` line, or None."""
    found = ANSWER_LINE.findall(output.replace("`", ""))
    if not found:
        return None
    try:
        answer = json.loads(found[-1])
    except json.JSONDecodeError:
        return None
    return answer if isinstance(answer, dict) else None
