from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.seasons import DEFAULT_SPLIT_START_MONTH, SEASON_PATTERN, SeasonFormat


class SeasonSource(BaseModel):
    season: str
    source_url: str
    provider: str = "90minut"


class Competition(BaseModel):
    slug: str
    name: str
    country: str | None = None
    format: str
    season_format: SeasonFormat = "split"
    season_start_month: int = Field(default=DEFAULT_SPLIT_START_MONTH, ge=1, le=12)
    current_season: str | None
    seasons: dict[str, SeasonSource]


class CompetitionSummary(BaseModel):
    slug: str
    name: str
    country: str | None = None
    format: str
    season_format: SeasonFormat = "split"
    season_start_month: int = DEFAULT_SPLIT_START_MONTH
    current_season: str | None
    available_seasons: list[str]
    fetched_seasons: list[str]
    automatic_refresh_enabled: bool = False
    scheduler_state: str | None = None
    next_check_at: datetime | None = None


class MatchResult(BaseModel):
    id: int | None = None
    competition: str
    season: str
    stage: str = "Unknown stage"
    round: int | None = Field(default=None, ge=1)
    home_team: str
    away_team: str
    home_team_slug: str | None = None
    away_team_slug: str | None = None
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    home_goals: int | None = Field(default=None, ge=0)
    away_goals: int | None = Field(default=None, ge=0)
    home_goals_first_half: int | None = Field(default=None, ge=0)
    away_goals_first_half: int | None = Field(default=None, ge=0)
    home_goals_second_half: int | None = Field(default=None, ge=0)
    away_goals_second_half: int | None = Field(default=None, ge=0)
    home_corners: int | None = Field(default=None, ge=0)
    away_corners: int | None = Field(default=None, ge=0)
    home_cards: int | None = Field(default=None, ge=0)
    away_cards: int | None = Field(default=None, ge=0)
    home_fouls: int | None = Field(default=None, ge=0)
    away_fouls: int | None = Field(default=None, ge=0)
    first_team_to_score: str | None = None
    first_goal_minute: str | None = None
    played_at: datetime | None = None
    source_url: str


class ScrapedMatch(BaseModel):
    competition: str
    season: str
    stage: str
    round: int | None = Field(default=None, ge=1)
    home_team: str
    away_team: str
    status: str
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    home_goals: int | None = Field(default=None, ge=0)
    away_goals: int | None = Field(default=None, ge=0)
    home_goals_first_half: int | None = Field(default=None, ge=0)
    away_goals_first_half: int | None = Field(default=None, ge=0)
    home_goals_second_half: int | None = Field(default=None, ge=0)
    away_goals_second_half: int | None = Field(default=None, ge=0)
    home_corners: int | None = Field(default=None, ge=0)
    away_corners: int | None = Field(default=None, ge=0)
    home_cards: int | None = Field(default=None, ge=0)
    away_cards: int | None = Field(default=None, ge=0)
    home_fouls: int | None = Field(default=None, ge=0)
    away_fouls: int | None = Field(default=None, ge=0)
    first_team_to_score: str | None = None
    first_goal_minute: str | None = None
    played_at: datetime
    source_url: str
    provider_match_id: str | None = None

    def as_result(self) -> MatchResult | None:
        if self.status != "finished" or self.home_score is None or self.away_score is None:
            return None
        return MatchResult(
            competition=self.competition,
            season=self.season,
            stage=self.stage,
            round=self.round,
            home_team=self.home_team,
            away_team=self.away_team,
            home_score=self.home_score,
            away_score=self.away_score,
            home_goals=self.home_goals,
            away_goals=self.away_goals,
            home_goals_first_half=self.home_goals_first_half,
            away_goals_first_half=self.away_goals_first_half,
            home_goals_second_half=self.home_goals_second_half,
            away_goals_second_half=self.away_goals_second_half,
            home_corners=self.home_corners,
            away_corners=self.away_corners,
            home_cards=self.home_cards,
            away_cards=self.away_cards,
            home_fouls=self.home_fouls,
            away_fouls=self.away_fouls,
            first_team_to_score=self.first_team_to_score,
            first_goal_minute=self.first_goal_minute,
            played_at=self.played_at,
            source_url=self.source_url,
        )


MatchStatus = Literal["finished", "scheduled", "inactive"]
MATCH_STATUSES: tuple[str, ...] = ("finished", "scheduled", "inactive")


class Match(BaseModel):
    """A fixture of any status (API v1.1). Unknown values are null, never 0."""

    id: int
    competition: str
    season: str
    stage: str
    round: int | None = None
    status: MatchStatus
    kickoff_at: datetime | None = None
    home_team: str
    away_team: str
    home_team_slug: str
    away_team_slug: str
    home_goals: int | None = None
    away_goals: int | None = None
    home_goals_first_half: int | None = None
    away_goals_first_half: int | None = None
    home_goals_second_half: int | None = None
    away_goals_second_half: int | None = None
    home_cards: int | None = None
    away_cards: int | None = None
    home_corners: int | None = None
    away_corners: int | None = None
    home_fouls: int | None = None
    away_fouls: int | None = None
    first_team_to_score: str | None = None
    first_goal_minute: str | None = None
    source_url: str


class MatchQuery(BaseModel):
    """Repository filters for ``find_matches``; ``after`` is the keyset (kickoff_at, id)."""

    model_config = ConfigDict(frozen=True)

    competition: str | None = None
    season: str | None = None
    statuses: tuple[str, ...] = ()
    kickoff_from: datetime | None = None
    kickoff_until: datetime | None = None  # exclusive
    team: str | None = None
    round: int | None = None
    stage: str | None = None
    descending: bool = False
    after: tuple[datetime | None, int] | None = None


class FreshnessSource(BaseModel):
    provider: str
    url: str


class Freshness(BaseModel):
    competition: str
    season: str
    data_as_of: datetime | None
    next_update_at: datetime | None
    automatic_refresh: bool
    scheduler_state: str | None
    source: FreshnessSource


class Pagination(BaseModel):
    limit: int
    next_cursor: str | None


class MatchesResponse(BaseModel):
    data: list[Match]
    pagination: Pagination
    freshness: list[Freshness]


class StandingRow(BaseModel):
    position: int
    team_slug: str
    team_name: str
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_difference: int
    points: int
    form: str


class StandingsResponse(BaseModel):
    competition: str
    season: str
    stage: str | None
    data: list[StandingRow]
    freshness: list[Freshness]


class Team(BaseModel):
    slug: str
    name: str
    aliases: list[str]


class TeamsResponse(BaseModel):
    data: list[Team]
    pagination: Pagination


class ResultsResponse(BaseModel):
    results: list[MatchResult]
    count: int
    refreshed_at: datetime | None
    stale: bool
    fetched: bool | None = None


class JobAccepted(BaseModel):
    job_id: int
    status: str
    status_url: str


class ScrapeJobStatus(BaseModel):
    job_id: int
    competition: str
    season: str
    status: str
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    matches_found: int | None
    error_message: str | None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competition: str = Field(examples=["ekstraklasa"])
    season: str | None = Field(default=None, pattern=SEASON_PATTERN)


class HistoricalFetchRequest(BaseModel):
    competition: str
    season: str = Field(pattern=SEASON_PATTERN)
    source_url: str | None = None
    force_refresh: bool = False
