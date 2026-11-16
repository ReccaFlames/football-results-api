from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SeasonSource(BaseModel):
    season: str
    source_url: str
    provider: str = "90minut"


class Competition(BaseModel):
    slug: str
    name: str
    country: str | None = None
    format: str
    current_season: str | None
    seasons: dict[str, SeasonSource]


class CompetitionSummary(BaseModel):
    slug: str
    name: str
    country: str | None = None
    format: str
    current_season: str | None
    available_seasons: list[str]
    fetched_seasons: list[str]
    automatic_refresh_enabled: bool = False
    scheduler_state: str | None = None
    next_check_at: datetime | None = None


class MatchResult(BaseModel):
    competition: str
    season: str
    stage: str = "Unknown stage"
    round: int | None = Field(default=None, ge=1)
    home_team: str
    away_team: str
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
    season: str | None = Field(default=None, pattern=r"^\d{4}-\d{4}$")


class HistoricalFetchRequest(BaseModel):
    competition: str
    season: str = Field(pattern=r"^\d{4}-\d{4}$")
    source_url: str | None = None
    force_refresh: bool = False
