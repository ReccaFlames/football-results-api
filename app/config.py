import argparse
import hashlib
import sys
from datetime import timedelta
from importlib.metadata import version
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.competitions import provider_competition_code, source_from_url
from app.models import Competition, SeasonSource
from app.scheduling import (
    DURATION_PATTERN,
    SchedulePolicy,
    SeasonRollover,
    parse_duration,
)
from app.seasons import SeasonFormat, default_start_month, validate_season


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FOOTBALL_RESULTS_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg_async://football:football@localhost:5432/football"
    config_file: Path = Path(__file__).resolve().parents[1] / "config/competitions.yaml"
    request_timeout_seconds: float = 20.0
    user_agent: str = Field(
        default_factory=lambda: (
            f"FootballResultsAPI/{version('football-results-api')} "
            "(contact: set-your-email@example.com)"
        )
    )
    scheduler_loop_seconds: float = 30.0
    scheduler_claim_limit: int = 5
    api_keys: str = ""
    football_data_api_token: str | None = None
    football_data_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    ninety_minut_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    ninety_minut_detail_cache_max_entries: int = Field(default=512, ge=0)
    rate_limit_enabled: bool = True
    rate_limit_read_per_minute: int = Field(default=120, ge=1)
    rate_limit_write_per_minute: int = Field(default=10, ge=1)
    trusted_proxies: str = ""
    cache_max_age_seconds: int = Field(default=60, ge=0)
    public_base_url: str | None = None
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, value: str) -> str:
        parse_networks(value)
        return value

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "must be an absolute http(s) URL without credentials, query, or fragment"
            )
        return value.strip().rstrip("/")


def parse_networks(value: str) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse comma-separated IP addresses or CIDR networks (``FOOTBALL_RESULTS_TRUSTED_PROXIES``)."""
    return tuple(
        ip_network(item.strip(), strict=False) for item in value.split(",") if item.strip()
    )


settings = Settings()


Duration = Annotated[str, Field(pattern=DURATION_PATTERN)]


def _positive(values: list[int], name: str) -> list[int]:
    if any(value <= 0 for value in values):
        raise ValueError(f"{name} must be positive")
    return values


class ScheduleConfig(BaseModel):
    """Scheduler thresholds; ``defaults.schedule`` in the configuration file."""

    model_config = ConfigDict(extra="forbid")

    policy: Literal["adaptive"] = "adaptive"
    pre_match_check_minutes: int = Field(default=30, gt=0)
    match_duration_minutes: dict[str, int] = Field(
        default_factory=lambda: {"league": 105, "cup": 135}, min_length=1
    )
    result_poll_minutes: list[int] = Field(default_factory=lambda: [5, 15, 60], min_length=1)
    result_poll_until_minutes: list[int] = Field(default_factory=lambda: [30, 240])
    idle_interval: Duration = "1d"
    off_season_interval: Duration = "7d"
    off_season_after: Duration = "30d"
    failure_backoff_minutes: list[int] = Field(
        default_factory=lambda: [5, 15, 30, 60], min_length=1
    )
    season_rollover: SeasonRollover = "auto"
    season_check_interval: Duration = "1d"

    @field_validator("match_duration_minutes")
    @classmethod
    def positive_match_durations(cls, values: dict[str, int]) -> dict[str, int]:
        _positive(list(values.values()), "match durations")
        return values

    @field_validator("result_poll_minutes", "failure_backoff_minutes")
    @classmethod
    def positive_intervals(cls, values: list[int]) -> list[int]:
        return _positive(values, "schedule intervals")

    @model_validator(mode="after")
    def poll_limits_match_intervals(self) -> "ScheduleConfig":
        limits = self.result_poll_until_minutes
        _positive(limits, "result_poll_until_minutes")
        if len(limits) != len(self.result_poll_minutes) - 1:
            raise ValueError(
                "result_poll_until_minutes needs exactly one entry fewer than result_poll_minutes"
            )
        if limits != sorted(set(limits)):
            raise ValueError("result_poll_until_minutes must be strictly increasing")
        return self

    def merged(self, override: "ScheduleOverride | None") -> "ScheduleConfig":
        """Return these settings with a per-competition override applied on top."""
        if override is None:
            return self
        values = self.model_dump()
        changes = override.model_dump(exclude_unset=True)
        if "match_duration_minutes" in changes:
            changes["match_duration_minutes"] = {
                **values["match_duration_minutes"],
                **changes["match_duration_minutes"],
            }
        return ScheduleConfig.model_validate({**values, **changes})

    def to_policy(self) -> SchedulePolicy:
        def minutes(values: list[int]) -> tuple[timedelta, ...]:
            return tuple(timedelta(minutes=value) for value in values)

        return SchedulePolicy(
            pre_match_check=timedelta(minutes=self.pre_match_check_minutes),
            match_duration={
                name: timedelta(minutes=value)
                for name, value in self.match_duration_minutes.items()
            },
            result_poll=minutes(self.result_poll_minutes),
            result_poll_until=minutes(self.result_poll_until_minutes),
            idle_interval=parse_duration(self.idle_interval),
            off_season_interval=parse_duration(self.off_season_interval),
            off_season_after=parse_duration(self.off_season_after),
            failure_backoff=minutes(self.failure_backoff_minutes),
            season_rollover=self.season_rollover,
            season_check_interval=parse_duration(self.season_check_interval),
        )


class ScheduleOverride(BaseModel):
    """Per-competition ``schedule``: only the listed keys replace ``defaults.schedule``."""

    model_config = ConfigDict(extra="forbid")

    pre_match_check_minutes: int | None = Field(default=None, gt=0)
    match_duration_minutes: dict[str, int] | None = None
    result_poll_minutes: list[int] | None = Field(default=None, min_length=1)
    result_poll_until_minutes: list[int] | None = None
    idle_interval: Duration | None = None
    off_season_interval: Duration | None = None
    off_season_after: Duration | None = None
    failure_backoff_minutes: list[int] | None = Field(default=None, min_length=1)
    season_rollover: SeasonRollover | None = None
    season_check_interval: Duration | None = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_request_interval_seconds: float = Field(ge=0)


class SeasonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str


class CompetitionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    country: str | None = None
    format: str = Field(min_length=1, max_length=30)
    provider: str
    season_format: SeasonFormat = "split"
    season_start_month: int | None = Field(default=None, ge=1, le=12)
    current_season: str | None = None
    seasons: dict[str, SeasonConfig] = Field(default_factory=dict)
    schedule: ScheduleOverride | None = None

    @field_validator("current_season", mode="before")
    @classmethod
    def calendar_season_as_string(cls, value: object) -> object:
        # YAML reads an unquoted calendar season such as 2026 as an integer.
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("seasons", mode="before")
    @classmethod
    def calendar_season_keys_as_strings(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            str(key) if isinstance(key, int) and not isinstance(key, bool) else key: item
            for key, item in value.items()
        }

    @model_validator(mode="after")
    def validate_sources(self) -> "CompetitionConfig":
        if self.season_start_month is None:
            self.season_start_month = default_start_month(self.season_format)
        elif self.season_format == "calendar" and self.season_start_month != 1:
            raise ValueError("calendar seasons start in January; omit season_start_month")
        elif self.season_format == "split" and self.season_start_month == 1:
            raise ValueError("a split season cannot start in January; use season_format: calendar")
        if self.current_season is not None and self.current_season not in self.seasons:
            raise ValueError("current_season must be a key in seasons")
        for season in self.seasons:
            validate_season(season, self.season_format)
        return self

    def to_competition(self) -> Competition:
        return Competition(
            slug=self.slug,
            name=self.name,
            country=self.country,
            format=self.format,
            season_format=self.season_format,
            season_start_month=self.season_start_month,
            current_season=self.current_season,
            seasons={
                season: SeasonSource(
                    season=season,
                    source_url=source.source_url,
                    provider=self.provider,
                )
                for season, source in self.seasons.items()
            },
        )


class DefaultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: ScheduleConfig


class CompetitionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: DefaultsConfig
    providers: dict[str, ProviderConfig]
    competitions: list[CompetitionConfig]

    @model_validator(mode="after")
    def validate_entries(self) -> "CompetitionFile":
        slugs = [competition.slug for competition in self.competitions]
        if len(slugs) != len(set(slugs)):
            raise ValueError("competition slugs must be unique")
        for competition in self.competitions:
            if competition.provider not in self.providers:
                raise ValueError(
                    f"competition {competition.slug!r} references undeclared "
                    f"provider {competition.provider!r}"
                )
            codes: set[str] = set()
            for season, source_config in competition.seasons.items():
                source = source_from_url(season, source_config.source_url)
                if source.provider != competition.provider:
                    raise ValueError(
                        f"competition {competition.slug!r} season {season} source uses "
                        f"provider {source.provider!r}, not {competition.provider!r}"
                    )
                if (code := provider_competition_code(source)) is not None:
                    codes.add(code)
            if len(codes) > 1:
                raise ValueError(
                    f"competition {competition.slug!r} mixes provider competition codes "
                    f"{sorted(codes)}"
                )
            try:
                schedule = self.defaults.schedule.merged(competition.schedule)
            except ValidationError as error:
                raise ValueError(
                    f"competition {competition.slug!r} has an invalid schedule override:\n{error}"
                ) from error
            if competition.format not in schedule.match_duration_minutes:
                raise ValueError(
                    f"competition {competition.slug!r} has format {competition.format!r} but "
                    "schedule.match_duration_minutes does not define it"
                )
        return self

    def to_competitions(self) -> dict[str, Competition]:
        return {competition.slug: competition.to_competition() for competition in self.competitions}

    def schedule_for(self, slug: str) -> SchedulePolicy:
        """Scheduler policy of one competition: ``defaults.schedule`` plus its override."""
        competition = next((item for item in self.competitions if item.slug == slug), None)
        override = None if competition is None else competition.schedule
        return self.defaults.schedule.merged(override).to_policy()

    def schedules(self) -> dict[str, SchedulePolicy]:
        return {
            competition.slug: self.schedule_for(competition.slug)
            for competition in self.competitions
        }

    def auto_rollover_slugs(self) -> frozenset[str]:
        """Competitions whose configured ``current_season`` is a floor, not an exact value."""
        return frozenset(
            slug for slug, policy in self.schedules().items() if policy.season_rollover == "auto"
        )


def _validate_config(path: Path, contents: bytes) -> CompetitionFile:
    try:
        data = yaml.safe_load(contents)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"cannot parse YAML in {path}: {error}") from error
    if not isinstance(data, dict):
        raise TypeError(f"configuration in {path} must be a YAML mapping")
    try:
        return CompetitionFile.model_validate(data)
    except ValidationError as error:
        raise ValueError(f"invalid configuration in {path}:\n{error}") from error


def load_config(path: Path | None = None) -> CompetitionFile:
    config_path = path or settings.config_file
    try:
        contents = config_path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read configuration file {config_path}: {error}") from error
    return _validate_config(config_path, contents)


class ConfigFileWatcher:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._digest: str | None = None
        self._config: CompetitionFile | None = None
        self._pending_digest: str | None = None
        self._pending_config: CompetitionFile | None = None

    def load_if_changed(self) -> tuple[CompetitionFile, bool]:
        try:
            contents = self.path.read_bytes()
        except OSError as error:
            raise ValueError(f"cannot read configuration file {self.path}: {error}") from error
        digest = hashlib.sha256(contents).hexdigest()
        if digest == self._digest:
            assert self._config is not None
            return self._config, False
        if digest == self._pending_digest:
            assert self._pending_config is not None
            return self._pending_config, True
        config = _validate_config(self.path, contents)
        self._pending_digest, self._pending_config = digest, config
        return config, True

    def mark_applied(self) -> None:
        if self._pending_digest is None or self._pending_config is None:
            raise RuntimeError("no changed configuration is pending")
        self._digest, self._config = self._pending_digest, self._pending_config
        self._pending_digest, self._pending_config = None, None


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the football competition config.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="validate the configured YAML file")
    check_parser.add_argument("--config-file", type=Path, default=settings.config_file)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_file)
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    print(f"Configuration valid: {args.config_file} ({len(config.competitions)} competitions)")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
