from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.seasons import DEFAULT_SPLIT_START_MONTH, SEASON_PATTERN, SeasonFormat


class SeasonSource(BaseModel):
    season: str
    source_url: str
    provider: str = "90minut"
    # The fallback source of this season, if any (see GLOSSARY.md and ADR 0002).
    fallback_provider: str | None = None
    fallback_url: str | None = None

    def fallback(self) -> "SeasonSource | None":
        if self.fallback_provider is None or self.fallback_url is None:
            return None
        return SeasonSource(
            season=self.season, source_url=self.fallback_url, provider=self.fallback_provider
        )


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

    id: int = Field(description="Stable match ID; kept across refreshes")
    competition: str
    season: str
    stage: str
    round: int | None = None
    status: MatchStatus = Field(
        description="finished, scheduled, or inactive (postponed, suspended, or cancelled)"
    )
    kickoff_at: datetime | None = None
    home_team: str
    away_team: str
    home_team_slug: str
    away_team_slug: str
    home_goals: int | None = Field(default=None, description="Full-time goals; null until finished")
    away_goals: int | None = Field(default=None, description="Full-time goals; null until finished")
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
    """Source of the last successful import (the fallback source when it filled in); the
    configured primary source when the season was never fetched."""

    provider: str
    url: str


class Freshness(BaseModel):
    competition: str
    season: str
    data_as_of: datetime | None = Field(
        description="Last successful import; null if the season was never fetched"
    )
    next_update_at: datetime | None = Field(
        description="Next scheduled check; null when the season is not refreshed automatically"
    )
    automatic_refresh: bool
    scheduler_state: str | None
    source: FreshnessSource


class Pagination(BaseModel):
    limit: int
    next_cursor: str | None = Field(
        description="Pass as cursor with the same filters for the next page; null on the last page"
    )


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
    form: str = Field(description="Up to five latest results, newest first (W, D, L)")


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
    matches_skipped: int = Field(
        default=0,
        description="Fallback matches skipped because a team is not yet a team of the season",
    )
    error_message: str | None


SCRAPE_RUN_STATUSES = ("queued", "running", "completed", "failed", "cancelled")

# Access requests (ADR 0010): pending -> approved -> claimed (or expired after the claim
# window), or pending -> rejected.
AccessRequestState = Literal["pending", "approved", "rejected", "claimed", "expired"]
ACCESS_REQUEST_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "claimed", "expired")
ClientType = Literal["human", "agent"]


class ScrapeRun(ScrapeJobStatus):
    source_url: str
    provider: str | None = Field(
        description="Provider of `source_url`; differs from the season's when it is a fallback run"
    )
    fallback: bool = Field(description="Whether the job refreshed from the fallback source")
    worker_id: str | None = Field(description="Scheduler worker that ran the job, if any")


class ScrapeRunsResponse(BaseModel):
    data: list[ScrapeRun]
    pagination: Pagination


class SchedulerWorker(BaseModel):
    worker_id: str
    started_at: datetime
    heartbeat_at: datetime
    alive: bool = Field(description="Whether the heartbeat is within the stall window")


class SeasonSchedule(BaseModel):
    competition: str
    season: str
    scheduler_state: str
    data_as_of: datetime | None = Field(description="Last successful import; null if never")
    next_update_at: datetime | None
    last_checked_at: datetime | None
    overdue_seconds: float = Field(description="Seconds past `next_update_at`; 0 when not due")
    stale: bool = Field(
        description="Never fetched, or the update is more than the grace period overdue"
    )
    consecutive_failures: int
    primary_consecutive_failures: int = Field(
        description="Primary source failures in a row; only a primary success resets it"
    )
    fallback_active: bool = Field(
        description="Whether the last successful import came from the fallback source"
    )
    active_job_id: int | None = Field(description="Queued or running job of this season")


class SchedulerStatus(BaseModel):
    status: Literal["ok", "stalled", "not_started"] = Field(
        description="`stalled` when no worker heartbeat is within the stall window"
    )
    checked_at: datetime
    latest_heartbeat_at: datetime | None
    stall_after_seconds: float
    stale_grace_seconds: float
    workers: list[SchedulerWorker] = Field(description="Most recently seen workers first")
    seasons: list[SeasonSchedule] = Field(
        description="Current seasons with automatic refresh, by competition"
    )


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competition: str = Field(description="Competition slug", examples=["ekstraklasa"])
    season: str | None = Field(
        default=None,
        pattern=SEASON_PATTERN,
        description="Must be the current season; omit to use it",
        examples=["2026-2027"],
    )


class HistoricalFetchRequest(BaseModel):
    competition: str = Field(description="Competition slug", examples=["ekstraklasa"])
    season: str = Field(
        pattern=SEASON_PATTERN,
        description="A non-current season in the competition's season format",
        examples=["2025-2026"],
    )
    source_url: str | None = Field(
        default=None,
        description=(
            "Source for a season that is not configured; must be an allowed URL of a provider "
            "already configured for the competition"
        ),
        examples=["http://www.90minut.pl/liga/1/liga13482.html"],
    )
    force_refresh: bool = Field(
        default=False, description="Queue a new fetch even when the season is already stored"
    )


CONTACT_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AccessRequestCreate(BaseModel):
    """A request for an API key (ADR 0010); the operator approves or rejects it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(
        min_length=1, max_length=200, description="Your name or organisation", examples=["Ada"]
    )
    contact_email: str = Field(
        max_length=320,
        pattern=CONTACT_EMAIL_PATTERN,
        description="Where the operator can reach you; never shared or sent to notifications",
        examples=["ada@example.com"],
    )
    intended_use: str = Field(
        min_length=1,
        max_length=1000,
        description="What you will use the API for",
        examples=["Weekly Ekstraklasa tables on a fan site"],
    )
    client_type: ClientType = Field(
        description="`human` for a person, `agent` for an AI agent or automated client",
        examples=["agent"],
    )
    competitions: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None,
        max_length=50,
        description="Competition slugs from `list_competitions` you need",
        examples=[["ekstraklasa"]],
    )
    message: str | None = Field(
        default=None, max_length=2000, description="Anything else for the operator"
    )


class AccessRequestCreated(BaseModel):
    id: int = Field(description="Access request ID")
    status: AccessRequestState = Field(description="Always `pending` for a new request")
    claim_secret: str = Field(
        description=(
            "Shown only in this response and never stored in plain text: send it as the "
            "`Claim-Secret` header to `get_access_request`. A lost secret cannot be recovered"
        )
    )
    status_url: str = Field(description="Poll with `get_access_request` (also in `Location`)")
    created_at: datetime


class AccessRequestStatus(BaseModel):
    id: int
    status: AccessRequestState = Field(
        description=(
            "`pending` until the operator decides; `approved` keys are returned once and the "
            "request becomes `claimed`; `expired` when not claimed in time; or `rejected`"
        )
    )
    client_type: ClientType
    created_at: datetime
    decided_at: datetime | None = Field(description="When the operator approved or rejected it")
    claim_expires_at: datetime | None = Field(
        description="While `approved`: claim the key before this time"
    )
    key_name: str | None = Field(description="Name of the issued key, once approved")
    api_key: str | None = Field(
        description=(
            "The issued API key, only in the one response that claims it; store it at once. "
            "Send it as `X-API-Key`"
        )
    )


class HealthStatus(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
    scheduler: Literal["ok", "stale", "not_started"]


# Error bodies: RFC 9457 problem details (app/problems.py builds them).

PROBLEM_TYPES: dict[str, tuple[int, str]] = {
    "invalid-cursor": (400, "Invalid cursor"),
    "unauthorized": (401, "Unauthorized"),
    "forbidden": (403, "Forbidden"),
    "unknown-competition": (404, "Unknown competition"),
    "unknown-season": (404, "Unknown season"),
    "unknown-job": (404, "Unknown job"),
    "unknown-access-request": (404, "Unknown access request"),
    "not-found": (404, "Not found"),
    "method-not-allowed": (405, "Method not allowed"),
    "job-conflict": (409, "Job conflict"),
    "access-request-pending": (409, "Access request pending"),
    "invalid-parameter": (422, "Invalid parameter"),
    "validation-error": (422, "Request validation failed"),
    "invalid-request": (422, "Invalid request"),
    "rate-limited": (429, "Too many requests"),
    "internal-error": (500, "Internal server error"),
    "writes-disabled": (503, "Write endpoints disabled"),
    "service-unavailable": (503, "Service unavailable"),
}
PROBLEM_TYPE_URIS = tuple(f"/problems/{slug}" for slug in PROBLEM_TYPES)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loc: list[str | int] = Field(description='Location, for example `["query", "limit"]`')
    msg: str
    type: str


class Problem(BaseModel):
    """RFC 9457 problem details; extension members are omitted when not relevant."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[*PROBLEM_TYPE_URIS, "about:blank"] = Field(  # type: ignore[valid-type]
        description="Stable problem type URI (relative); branch on this, not on `detail`"
    )
    title: str = Field(description="Short summary of the problem type")
    status: int = Field(description="HTTP status code")
    detail: str = Field(description="Explanation of this occurrence")
    instance: str = Field(description="Request path")
    parameter: str | None = Field(default=None, description="The request parameter to correct")
    invalid: list[str] | None = Field(default=None, description="The rejected values")
    valid_values: list[str] | None = Field(default=None, description="Values that are accepted")
    hint: str | None = Field(default=None, description="How to correct the request")
    examples: list[str] | None = Field(default=None, description="Accepted value examples")
    errors: list[ValidationIssue] | None = Field(
        default=None, description="Every request validation failure (`validation-error` only)"
    )
