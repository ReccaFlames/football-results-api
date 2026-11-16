import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from app.competitions import football_data_source_url, provider_competition_code
from app.models import (
    Competition,
    MatchResult,
    ScrapedMatch,
    SeasonSource,
)
from app.seasons import season_year_for_month

logger = logging.getLogger(__name__)

ROUND_RE = re.compile(r"^Kolejka\s+(\d+)\b", re.IGNORECASE)
SCORE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
DETAIL_SCORE_RE = re.compile(r"^(\d{1,2})\s*-\s*(\d{1,2})$")
GOAL_MINUTE_RE = re.compile(r"(\d{1,3}(?:\+\d{1,2})?)\s*(?:\(s\))?$")
DATE_RE = re.compile(
    r"(\d{1,2})\s+([a-ząćęłńóśźż]+),\s*(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)
POLISH_MONTHS = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "września": 9,
    "października": 10,
    "listopada": 11,
    "grudnia": 12,
}

MATCH_STATISTICS = frozenset(
    {
        "home_goals",
        "away_goals",
        "home_goals_first_half",
        "away_goals_first_half",
        "home_goals_second_half",
        "away_goals_second_half",
        "home_corners",
        "away_corners",
        "home_cards",
        "away_cards",
        "home_fouls",
        "away_fouls",
        "first_team_to_score",
        "first_goal_minute",
    }
)
DERIVED_GOAL_STATISTICS = frozenset(
    {
        "home_goals",
        "away_goals",
        "home_goals_first_half",
        "away_goals_first_half",
        "home_goals_second_half",
        "away_goals_second_half",
        "first_team_to_score",
        "first_goal_minute",
    }
)


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can supply; see docs/PROVIDERS.md.

    ``statistics`` lists the optional ScrapedMatch statistic fields the provider may fill.
    Every other statistic must stay ``None``.
    """

    statistics: frozenset[str]
    season_discovery: bool
    scheduled_fixtures: bool = True
    requires_api_token: bool = False

    def __post_init__(self) -> None:
        unknown = self.statistics - MATCH_STATISTICS
        if unknown:
            raise ValueError(f"unknown statistics: {sorted(unknown)}")


class CompetitionScraper(Protocol):
    """Provider contract. The full contract is documented in docs/PROVIDERS.md."""

    name: str
    capabilities: ProviderCapabilities

    async def scrape(
        self, competition: Competition, source: SeasonSource
    ) -> list[ScrapedMatch]: ...

    async def discover_seasons(self, competition: Competition) -> list[SeasonSource]: ...


def _clean(value: str) -> str:
    return " ".join(value.split())


def _played_at(text: str, season: str, season_start_month: int) -> datetime | None:
    match = DATE_RE.search(_clean(text))
    if not match:
        return None
    day, month_name, hour, minute = match.groups()
    month = POLISH_MONTHS.get(month_name.casefold())
    if month is None:
        return None
    return datetime(
        season_year_for_month(season, month, season_start_month),
        month,
        int(day),
        int(hour),
        int(minute),
        tzinfo=ZoneInfo("Europe/Warsaw"),
    )


def _stage_before(
    element: object,
    competition: Competition,
) -> tuple[str, int | None] | None:
    for header in element.find_all_previous("u"):
        stage = _clean(header.get_text())
        round_match = ROUND_RE.match(stage)
        cup_stage = any(
            marker in stage.casefold() for marker in ("runda", "faza ligowa", "play-off", "finał")
        )
        if round_match:
            return stage, int(round_match.group(1))
        if competition.format == "cup" and cup_stage:
            return stage, None
    return None


def parse_ninety_minut_matches(
    html: str,
    competition: Competition,
    source: SeasonSource,
) -> list[ScrapedMatch]:
    soup = BeautifulSoup(html, "html.parser")
    matches: list[ScrapedMatch] = []
    seen: set[tuple[object, ...]] = set()

    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        played_at = next(
            (
                value
                for cell in cells
                if (
                    value := _played_at(
                        cell.get_text(), source.season, competition.season_start_month
                    )
                )
                is not None
            ),
            None,
        )
        if played_at is None:
            continue
        match_cell_index = next(
            (
                index
                for index, cell in enumerate(cells)
                if SCORE_RE.fullmatch(_clean(cell.get_text())) or _clean(cell.get_text()) == "-"
            ),
            None,
        )
        if match_cell_index is None or match_cell_index == 0 or match_cell_index + 1 >= len(cells):
            continue
        stage_details = _stage_before(row, competition)
        if stage_details is None:
            continue
        stage, round_number = stage_details
        home_team = _clean(cells[match_cell_index - 1].get_text())
        away_team = _clean(cells[match_cell_index + 1].get_text())
        if not home_team or not away_team:
            continue

        match_cell = cells[match_cell_index]
        score_match = SCORE_RE.fullmatch(_clean(match_cell.get_text()))
        status = "finished" if score_match else "scheduled"
        home_score, away_score = map(int, score_match.groups()) if score_match else (None, None)
        score_link = match_cell.find(
            "a",
            href=re.compile(r"(?:^|/)mecz\.php\?id_mecz=\d+"),
        )
        if status == "finished" and competition.format == "league" and score_link is None:
            continue
        match_url = (
            str(httpx.URL(source.source_url).join(score_link["href"]))
            if score_link
            else source.source_url
        )
        provider_id = re.search(r"[?&]id_mecz=(\d+)", match_url)
        key = (stage, home_team, away_team, played_at)
        if key in seen:
            continue
        seen.add(key)
        matches.append(
            ScrapedMatch(
                competition=competition.slug,
                season=source.season,
                stage=stage,
                round=round_number,
                home_team=home_team,
                away_team=away_team,
                status=status,
                home_score=home_score,
                away_score=away_score,
                home_goals=home_score,
                away_goals=away_score,
                played_at=played_at,
                source_url=match_url,
                provider_match_id=None if provider_id is None else provider_id.group(1),
            )
        )
    return matches


def parse_results(
    html: str,
    competition: Competition,
    source: SeasonSource,
) -> list[MatchResult]:
    return [
        result
        for match in parse_ninety_minut_matches(html, competition, source)
        if (result := match.as_result()) is not None
    ]


def parse_ninety_minut_details(
    html: str,
    match: ScrapedMatch,
) -> dict[str, int | str | None]:
    """Extract statistics which 90minut exposes on an individual match page."""
    soup = BeautifulSoup(html, "html.parser")
    title_score = soup.find(
        "nobr",
        string=lambda value: bool(value and DETAIL_SCORE_RE.fullmatch(_clean(value))),
    )
    match_table = title_score.find_parent("table") if title_score is not None else None
    if match_table is None:
        raise ValueError(f"90minut detail page has no match table: {match.source_url}")

    goals: list[tuple[int, int, str | None]] = []
    previous_home = previous_away = 0
    for row in match_table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 3:
            continue
        score = DETAIL_SCORE_RE.fullmatch(_clean(cells[1].get_text()))
        has_goal_icon = any(
            image.get("src", "").endswith(("goal.gif", "own.gif")) for image in row.find_all("img")
        )
        if score is None or not has_goal_icon:
            continue
        home_score, away_score = map(int, score.groups())
        if home_score == previous_home + 1 and away_score == previous_away:
            side, scorer_cell = "home", cells[0]
        elif away_score == previous_away + 1 and home_score == previous_home:
            side, scorer_cell = "away", cells[2]
        else:
            raise ValueError(f"Invalid goal progression on {match.source_url}")
        minute_match = GOAL_MINUTE_RE.search(_clean(scorer_cell.get_text()))
        goals.append(
            (
                home_score,
                away_score,
                minute_match.group(1) if minute_match is not None else None,
            )
        )
        previous_home, previous_away = home_score, away_score

    expected = (match.home_score or 0, match.away_score or 0)
    if (previous_home, previous_away) != expected and (expected != (0, 0) or goals):
        raise ValueError(f"Goal events do not match the final score on {match.source_url}")

    home_cards = away_cards = 0
    for row in match_table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 3:
            continue
        for index, side in ((0, "home"), (2, "away")):
            count = sum(
                image.get("src", "").endswith(("yel.gif", "red.gif"))
                for image in cells[index].find_all("img")
            )
            if side == "home":
                home_cards += count
            else:
                away_cards += count

    # Score progression is cumulative, so count which side changed instead of
    # treating every event with a non-zero cumulative score as that side's goal.
    previous_home = previous_away = 0
    home_first_half = away_first_half = 0
    first_side: str | None = None
    has_all_goal_minutes = all(minute is not None for _, _, minute in goals)
    for home_score, away_score, minute in goals:
        side = "home" if home_score > previous_home else "away"
        if minute is not None and int(minute.split("+")[0]) <= 45:
            if side == "home":
                home_first_half += 1
            else:
                away_first_half += 1
        if first_side is None:
            first_side = side
        previous_home, previous_away = home_score, away_score

    return {
        "home_goals_first_half": home_first_half if has_all_goal_minutes else None,
        "away_goals_first_half": away_first_half if has_all_goal_minutes else None,
        "home_goals_second_half": expected[0] - home_first_half if has_all_goal_minutes else None,
        "away_goals_second_half": expected[1] - away_first_half if has_all_goal_minutes else None,
        "home_cards": home_cards,
        "away_cards": away_cards,
        "first_team_to_score": (
            match.home_team if first_side == "home" else match.away_team if first_side else None
        ),
        "first_goal_minute": goals[0][2] if goals else None,
    }


class NinetyMinutScraper:
    name = "90minut"
    capabilities = ProviderCapabilities(
        statistics=DERIVED_GOAL_STATISTICS | {"home_cards", "away_cards"},
        season_discovery=False,
    )

    def __init__(
        self,
        timeout: float,
        user_agent: str,
        min_interval_seconds: float = 2.0,
        detail_cache_max_entries: int = 512,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self.detail_cache_max_entries = detail_cache_max_entries
        self._detail_cache: OrderedDict[str, dict[str, int | str | None]] = OrderedDict()
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request_at: dict[str, float] = {}

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        host = httpx.URL(url).netloc
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            previous_request_at = self._last_request_at.get(host)
            if previous_request_at is not None:
                delay = self.min_interval_seconds - (time.monotonic() - previous_request_at)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request_at[host] = time.monotonic()
            return await client.get(url)

    def _cache_details(self, url: str, details: dict[str, int | str | None]) -> None:
        if self.detail_cache_max_entries == 0:
            return
        self._detail_cache[url] = details
        self._detail_cache.move_to_end(url)
        if len(self._detail_cache) > self.detail_cache_max_entries:
            self._detail_cache.popitem(last=False)

    async def discover_seasons(self, competition: Competition) -> list[SeasonSource]:
        # 90minut assigns an unrelated page ID to every season; sources are configured by hand.
        return []

    async def scrape(
        self,
        competition: Competition,
        source: SeasonSource,
    ) -> list[ScrapedMatch]:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=False,
        ) as client:
            response = await self._get(client, source.source_url)
            response.raise_for_status()
            matches = parse_ninety_minut_matches(
                response.content.decode("iso-8859-2"),
                competition,
                source,
            )
            enriched: list[ScrapedMatch] = []
            for match in matches:
                if match.status != "finished" or "mecz.php?id_mecz=" not in match.source_url:
                    enriched.append(match)
                    continue
                details = self._detail_cache.get(match.source_url)
                if details is None:
                    try:
                        detail_response = await self._get(client, match.source_url)
                        detail_response.raise_for_status()
                        details = parse_ninety_minut_details(
                            detail_response.content.decode("iso-8859-2"), match
                        )
                    except (httpx.HTTPError, UnicodeDecodeError, ValueError) as error:
                        logger.warning(
                            "failed to fetch 90minut details for %s; leaving detail fields null: %s",
                            match.source_url,
                            error,
                        )
                        enriched.append(match)
                        continue
                    self._cache_details(match.source_url, details)
                else:
                    self._detail_cache.move_to_end(match.source_url)
                enriched.append(match.model_copy(update=details))
        return enriched


def parse_matches(
    html: str,
    competition: Competition,
    source: SeasonSource,
) -> list[ScrapedMatch]:
    """Backward-compatible name for the original 90minut parser."""
    return parse_ninety_minut_matches(html, competition, source)


def parse_football_data_matches(
    payload: dict[str, object],
    competition: Competition,
    source: SeasonSource,
) -> list[ScrapedMatch]:
    matches: list[ScrapedMatch] = []
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        raise TypeError("football-data.org response does not contain a matches list")
    for raw_match in raw_matches:
        if not isinstance(raw_match, dict):
            continue
        raw_matchday = raw_match.get("matchday")
        matchday = raw_matchday if isinstance(raw_matchday, int) and raw_matchday >= 1 else None
        stage = _football_data_stage(raw_match, matchday)
        utc_date = raw_match.get("utcDate")
        home = raw_match.get("homeTeam")
        away = raw_match.get("awayTeam")
        if (
            stage is None
            or not isinstance(utc_date, str)
            or not isinstance(home, dict)
            or not isinstance(away, dict)
            or not isinstance(home.get("name"), str)
            or not isinstance(away.get("name"), str)
        ):
            continue
        try:
            played_at = datetime.fromisoformat(utc_date)
        except ValueError:
            continue
        provider_status = raw_match.get("status")
        score = raw_match.get("score")
        full_time = score.get("fullTime") if isinstance(score, dict) else None
        raw_home_score = full_time.get("home") if isinstance(full_time, dict) else None
        raw_away_score = full_time.get("away") if isinstance(full_time, dict) else None
        finished = (
            provider_status in {"FINISHED", "AWARDED"}
            and isinstance(raw_home_score, int)
            and isinstance(raw_away_score, int)
        )
        inactive = provider_status in {"SUSPENDED", "POSTPONED", "CANCELLED"}
        status = "finished" if finished else "inactive" if inactive else "scheduled"
        half_time = score.get("halfTime") if isinstance(score, dict) else None
        home_first_half = half_time.get("home") if isinstance(half_time, dict) else None
        away_first_half = half_time.get("away") if isinstance(half_time, dict) else None
        has_half_time = isinstance(home_first_half, int) and isinstance(away_first_half, int)
        home_second_half = raw_home_score - home_first_half if finished and has_half_time else None
        away_second_half = raw_away_score - away_first_half if finished and has_half_time else None
        bookings = raw_match.get("bookings")
        home_cards = _team_event_count(bookings, home) if finished else None
        away_cards = _team_event_count(bookings, away) if finished else None
        first_team_to_score, first_goal_minute = (
            _first_goal(raw_match.get("goals")) if finished else (None, None)
        )
        match_id = raw_match.get("id")
        match_url = (
            f"https://api.football-data.org/v4/matches/{match_id}"
            if isinstance(match_id, int)
            else source.source_url
        )
        matches.append(
            ScrapedMatch(
                competition=competition.slug,
                season=source.season,
                stage=stage,
                round=matchday,
                home_team=home["name"],
                away_team=away["name"],
                status=status,
                home_score=raw_home_score if finished else None,
                away_score=raw_away_score if finished else None,
                home_goals=raw_home_score if finished else None,
                away_goals=raw_away_score if finished else None,
                home_goals_first_half=home_first_half if finished and has_half_time else None,
                away_goals_first_half=away_first_half if finished and has_half_time else None,
                home_goals_second_half=home_second_half,
                away_goals_second_half=away_second_half,
                home_cards=home_cards,
                away_cards=away_cards,
                first_team_to_score=first_team_to_score,
                first_goal_minute=first_goal_minute,
                played_at=played_at,
                source_url=match_url,
                provider_match_id=str(match_id) if isinstance(match_id, int) else None,
            )
        )
    return matches


def _football_data_label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("_")) if value.isupper() else value


def _football_data_stage(raw_match: dict[object, object], matchday: int | None) -> str | None:
    """Name a fixture's stage; league fixtures keep the historical ``Matchweek N`` form."""
    raw_stage = raw_match.get("stage")
    if raw_stage in (None, "REGULAR_SEASON"):
        return None if matchday is None else f"Matchweek {matchday}"
    if not isinstance(raw_stage, str) or not raw_stage:
        return None
    parts = [_football_data_label(raw_stage)]
    group = raw_match.get("group")
    if isinstance(group, str) and group:
        parts.append(_football_data_label(group))
    if matchday is not None:
        parts.append(f"Matchday {matchday}")
    return ", ".join(parts)


def parse_football_data_seasons(
    payload: dict[str, object],
    competition: Competition,
    code: str,
) -> list[SeasonSource]:
    """Map a football-data.org ``/v4/competitions/<CODE>`` response to season sources."""
    raw_seasons = payload.get("seasons")
    if not isinstance(raw_seasons, list):
        raise TypeError("football-data.org response does not contain a seasons list")
    sources: dict[str, SeasonSource] = {}
    for raw_season in raw_seasons:
        start_date = raw_season.get("startDate") if isinstance(raw_season, dict) else None
        if not isinstance(start_date, str):
            continue
        try:
            start_year = date.fromisoformat(start_date).year
        except ValueError:
            continue
        season = (
            str(start_year)
            if competition.season_format == "calendar"
            else f"{start_year}-{start_year + 1}"
        )
        sources[season] = SeasonSource(
            season=season,
            source_url=football_data_source_url(code, season),
            provider="football-data",
        )
    return [sources[season] for season in sorted(sources, reverse=True)]


def _team_event_count(events: object, team: dict[object, object]) -> int | None:
    if not isinstance(events, list):
        return None
    team_id = team.get("id")
    team_name = team.get("name")
    count = 0
    for event in events:
        event_team = event.get("team") if isinstance(event, dict) else None
        if not isinstance(event_team, dict):
            continue
        id_matches = team_id is not None and event_team.get("id") == team_id
        name_matches = team_name is not None and event_team.get("name") == team_name
        if id_matches or name_matches:
            count += 1
    return count


def _first_goal(goals: object) -> tuple[str | None, str | None]:
    if not isinstance(goals, list):
        return None, None
    valid = [
        goal
        for goal in goals
        if isinstance(goal, dict)
        and isinstance(goal.get("minute"), int)
        and isinstance(goal.get("team"), dict)
        and isinstance(goal["team"].get("name"), str)
    ]
    if not valid:
        return None, None
    goal = min(
        valid,
        key=lambda item: (
            item["minute"],
            item.get("injuryTime") if isinstance(item.get("injuryTime"), int) else 0,
        ),
    )
    injury_time = goal.get("injuryTime")
    minute = str(goal["minute"])
    if isinstance(injury_time, int) and injury_time > 0:
        minute = f"{minute}+{injury_time}"
    return goal["team"]["name"], minute


class FootballDataScraper:
    name = "football-data"
    capabilities = ProviderCapabilities(
        statistics=DERIVED_GOAL_STATISTICS | {"home_cards", "away_cards"},
        season_discovery=True,
        requires_api_token=True,
    )

    def __init__(
        self,
        timeout: float,
        user_agent: str,
        api_token: str | None,
        min_interval_seconds: float,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.api_token = api_token
        self.min_interval_seconds = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def _get_json(self, url: str) -> dict[str, object]:
        if not self.api_token or not self.api_token.strip():
            raise ValueError(
                "FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN is required for football-data.org"
            )
        async with self._lock:
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "User-Agent": self.user_agent,
                    "X-Auth-Token": self.api_token,
                    "X-Unfold-Goals": "true",
                    "X-Unfold-Bookings": "true",
                },
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
            self._last_request_at = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("football-data.org response is not a JSON object")
        return payload

    async def scrape(self, competition: Competition, source: SeasonSource) -> list[ScrapedMatch]:
        payload = await self._get_json(source.source_url)
        return parse_football_data_matches(payload, competition, source)

    async def discover_seasons(self, competition: Competition) -> list[SeasonSource]:
        codes = {
            code
            for source in competition.seasons.values()
            if (code := provider_competition_code(source)) is not None
        }
        if len(codes) != 1:
            raise ValueError(
                f"cannot determine the football-data.org competition code for {competition.slug}"
            )
        code = codes.pop()
        payload = await self._get_json(f"https://api.football-data.org/v4/competitions/{code}")
        return parse_football_data_seasons(payload, competition, code)


class ScraperRegistry:
    def __init__(self, scrapers: dict[str, CompetitionScraper]) -> None:
        self.scrapers = scrapers

    def provider(self, name: str) -> CompetitionScraper:
        scraper = self.scrapers.get(name)
        if scraper is None:
            raise ValueError(f"Unsupported source provider: {name}")
        return scraper

    async def scrape(self, competition: Competition, source: SeasonSource) -> list[ScrapedMatch]:
        return await self.provider(source.provider).scrape(competition, source)


def create_scraper_registry(
    timeout: float,
    user_agent: str,
    football_data_api_token: str | None,
    football_data_min_interval_seconds: float,
    ninety_minut_min_interval_seconds: float = 2.0,
    ninety_minut_detail_cache_max_entries: int = 512,
) -> ScraperRegistry:
    providers: list[CompetitionScraper] = [
        NinetyMinutScraper(
            timeout,
            user_agent,
            ninety_minut_min_interval_seconds,
            ninety_minut_detail_cache_max_entries,
        ),
        FootballDataScraper(
            timeout,
            user_agent,
            football_data_api_token,
            football_data_min_interval_seconds,
        ),
    ]
    return ScraperRegistry({provider.name: provider for provider in providers})
