import argparse
import hashlib
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app import access
from app.competitions import provider_competition_code, source_from_url
from app.models import Competition, SeasonSource
from app.scheduling import (
    DURATION_PATTERN,
    SchedulePolicy,
    SeasonRollover,
    parse_duration,
)
from app.scraper import (
    OPENLIGADB_POSTPONED_AFTER_HOURS,
    PROVIDER_CAPABILITIES,
    THESPORTSDB_FREE_API_KEY,
    THESPORTSDB_WINDOW_DAYS_AHEAD,
    THESPORTSDB_WINDOW_DAYS_BACK,
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
    # Running scrape jobs of a worker without a heartbeat for this long are failed as abandoned.
    scheduler_worker_timeout_seconds: float = Field(default=300.0, gt=0)
    api_keys: str = ""
    # YAML file of named, hashed API keys with roles and quotas (ADR 0005).
    api_keys_file: Path | None = None
    football_data_api_token: str | None = None
    football_data_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    ninety_minut_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    ninety_minut_detail_cache_max_entries: int = Field(default=512, ge=0)
    espn_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    # TheSportsDB's free public key; a premium key overrides it. Never logged or stored.
    thesportsdb_api_key: SecretStr = SecretStr(THESPORTSDB_FREE_API_KEY)
    thesportsdb_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    openligadb_min_request_interval_seconds: float | None = Field(default=None, ge=0)
    rate_limit_enabled: bool = True
    rate_limit_read_per_minute: int = Field(default=120, ge=1)
    rate_limit_write_per_minute: int = Field(default=10, ge=1)
    trusted_proxies: str = ""
    cache_max_age_seconds: int = Field(default=60, ge=0)
    public_base_url: str | None = None
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    # Optional OAuth for `/mcp` (ADR 0006): an issuer turns it on.
    oauth_issuer: str | None = None
    oauth_audience: str | None = None
    oauth_mode: Literal["optional", "required"] = "optional"
    oauth_scopes: str = ""
    # JWKS location when the issuer's discovery URL is unreachable from the API (Compose).
    oauth_jwks_url: str | None = None
    log_format: Literal["text", "json"] = "text"
    # Public read-only demo instance (ADR 0007): no admin keys, redistributable providers only.
    demo_mode: bool = False
    # False makes a private instance (ADR 0008): every data operation needs a key.
    anonymous_access: bool = True
    # Shown on `/privacy`; required in demo mode (a public instance must name its operator).
    operator_contact: str | None = None
    # How long the operator keeps access logs; shown on `/privacy`.
    log_retention_days: int = Field(default=14, ge=1)
    # Shorten client IPs in access logs (always on in demo mode).
    anonymize_log_ips: bool = False

    @field_validator("oauth_issuer", "oauth_jwks_url")
    @classmethod
    def _validate_oauth_urls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.fragment:
            raise ValueError("must be an absolute http(s) URL without a fragment")
        return value

    @model_validator(mode="after")
    def _oauth_needs_public_base_url(self) -> "Settings":
        if self.oauth_issuer and not self.public_base_url:
            raise ValueError(
                "FOOTBALL_RESULTS_OAUTH_ISSUER requires FOOTBALL_RESULTS_PUBLIC_BASE_URL"
            )
        return self

    @property
    def log_ips_anonymized(self) -> bool:
        return self.anonymize_log_ips or self.demo_mode

    @property
    def mcp_resource(self) -> str | None:
        """The public `/mcp` URL: the OAuth protected resource, or None without a base URL."""
        return f"{self.public_base_url}/mcp" if self.public_base_url else None

    @property
    def oauth_token_audience(self) -> str | None:
        return self.oauth_audience or self.mcp_resource

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
    fallback_after_failures: int = Field(default=3, ge=1)

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
            fallback_after_failures=self.fallback_after_failures,
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
    fallback_after_failures: int | None = Field(default=None, ge=1)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_request_interval_seconds: float = Field(ge=0)
    # Only for windowed providers (ADR 0003): days before and after today that are fetched.
    window_days_back: int | None = Field(default=None, ge=0)
    window_days_ahead: int | None = Field(default=None, ge=0)
    # Only for openligadb (ADR 0004): an unfinished match this long past kickoff is inactive.
    postponed_after_hours: float | None = Field(default=None, gt=0)

    def postponed_after(self) -> float:
        return (
            OPENLIGADB_POSTPONED_AFTER_HOURS
            if self.postponed_after_hours is None
            else self.postponed_after_hours
        )

    def window(self) -> tuple[int, int]:
        return (
            THESPORTSDB_WINDOW_DAYS_BACK
            if self.window_days_back is None
            else self.window_days_back,
            THESPORTSDB_WINDOW_DAYS_AHEAD
            if self.window_days_ahead is None
            else self.window_days_ahead,
        )


class SeasonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str
    fallback_url: str | None = None


class CompetitionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    country: str | None = None
    format: str = Field(min_length=1, max_length=30)
    provider: str
    # Fallback source provider; each season may set a matching fallback_url (ADR 0002).
    fallback_provider: str | None = None
    season_format: SeasonFormat = "split"
    season_start_month: int | None = Field(default=None, ge=1, le=12)
    current_season: str | None = None
    seasons: dict[str, SeasonConfig] = Field(default_factory=dict)
    schedule: ScheduleOverride | None = None
    # Provider team name -> canonical team name, applied before matches are stored.
    team_aliases: dict[str, str] = Field(default_factory=dict)

    @field_validator("team_aliases")
    @classmethod
    def non_empty_team_aliases(cls, value: dict[str, str]) -> dict[str, str]:
        for alias, name in value.items():
            if not alias.strip() or not name.strip():
                raise ValueError("team_aliases names must not be empty")
            if alias == name:
                raise ValueError(f"team alias {alias!r} maps to itself")
        return value

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
        if self.fallback_provider is not None and self.fallback_provider == self.provider:
            raise ValueError("fallback_provider must differ from provider")
        for season, source in self.seasons.items():
            validate_season(season, self.season_format)
            if source.fallback_url is not None and self.fallback_provider is None:
                raise ValueError(f"season {season} has a fallback_url but no fallback_provider")
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
                    fallback_provider=None
                    if source.fallback_url is None
                    else self.fallback_provider,
                    fallback_url=source.fallback_url,
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
        for name, provider_config in self.providers.items():
            capabilities = PROVIDER_CAPABILITIES.get(name)
            has_window = (
                provider_config.window_days_back is not None
                or provider_config.window_days_ahead is not None
            )
            if has_window and (capabilities is None or not capabilities.windowed):
                raise ValueError(f"provider {name!r} is not windowed; remove its window_days_*")
            if provider_config.postponed_after_hours is not None and name != "openligadb":
                raise ValueError(
                    f"postponed_after_hours is only supported for 'openligadb', not {name!r}"
                )
        for competition in self.competitions:
            capabilities = PROVIDER_CAPABILITIES.get(competition.provider)
            if capabilities is not None and capabilities.windowed:
                raise ValueError(
                    f"competition {competition.slug!r} uses windowed provider "
                    f"{competition.provider!r} as provider; a windowed source supplies only the "
                    "matches around today, so it may only be a fallback_provider"
                )
            if competition.provider not in self.providers:
                raise ValueError(
                    f"competition {competition.slug!r} references undeclared "
                    f"provider {competition.provider!r}"
                )
            if (
                competition.fallback_provider is not None
                and competition.fallback_provider not in self.providers
            ):
                raise ValueError(
                    f"competition {competition.slug!r} references undeclared "
                    f"fallback_provider {competition.fallback_provider!r}"
                )
            codes: set[str] = set()
            fallback_codes: set[str] = set()
            for season, source_config in competition.seasons.items():
                source = source_from_url(season, source_config.source_url)
                if source.provider != competition.provider:
                    raise ValueError(
                        f"competition {competition.slug!r} season {season} source uses "
                        f"provider {source.provider!r}, not {competition.provider!r}"
                    )
                if (code := provider_competition_code(source)) is not None:
                    codes.add(code)
                if source_config.fallback_url is None:
                    continue
                fallback = source_from_url(season, source_config.fallback_url)
                if fallback.provider != competition.fallback_provider:
                    raise ValueError(
                        f"competition {competition.slug!r} season {season} fallback_url uses "
                        f"provider {fallback.provider!r}, not {competition.fallback_provider!r}"
                    )
                if (code := provider_competition_code(fallback)) is not None:
                    fallback_codes.add(code)
            for label, found in (("provider", codes), ("fallback provider", fallback_codes)):
                if len(found) > 1:
                    raise ValueError(
                        f"competition {competition.slug!r} mixes {label} competition codes "
                        f"{sorted(found)}"
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

    def team_aliases(self) -> dict[str, dict[str, str]]:
        """Configured team aliases per competition slug."""
        return {
            competition.slug: competition.team_aliases
            for competition in self.competitions
            if competition.team_aliases
        }

    def competitions_by_provider(self) -> dict[str, list[str]]:
        """Competition slugs per provider, as primary or fallback provider."""
        result: dict[str, list[str]] = {}
        for competition in self.competitions:
            for provider in {competition.provider, competition.fallback_provider} - {None}:
                result.setdefault(str(provider), []).append(competition.slug)
        return result

    def fallback_providers(self) -> dict[str, str]:
        """Configured fallback provider per competition slug."""
        return {
            competition.slug: competition.fallback_provider
            for competition in self.competitions
            if competition.fallback_provider is not None
        }

    def fallback_warnings(
        self, football_data_api_token: str | None, thesportsdb_api_key: str | None
    ) -> list[str]:
        """Fallback sources that are unavailable or limited with the current settings.

        The messages never contain the token or key values.
        """
        has_football_data_token = bool(football_data_api_token and football_data_api_token.strip())
        free_thesportsdb_key = not thesportsdb_api_key or thesportsdb_api_key.strip() in (
            "",
            THESPORTSDB_FREE_API_KEY,
        )
        warnings: list[str] = []
        for slug, provider in self.fallback_providers().items():
            if provider == "football-data" and not has_football_data_token:
                warnings.append(
                    f"competition {slug!r} has fallback provider 'football-data', but "
                    "FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN is not set; its fallback source "
                    "is unavailable"
                )
            elif provider == "thesportsdb" and free_thesportsdb_key:
                warnings.append(
                    f"competition {slug!r} has fallback provider 'thesportsdb' with the free "
                    "TheSportsDB API key, which returns at most 3 events per day and 5 seasons; "
                    "a busy matchday can miss matches, and those matches stay unchanged until "
                    "the primary source recovers or a later window covers them. Set "
                    "FOOTBALL_RESULTS_THESPORTSDB_API_KEY to a paid key to remove these limits"
                )
        return warnings

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


# Providers whose terms permit public redistribution of their data (ADR 0007). Adding one needs a
# documented written permission or licence and a code change, never configuration.
REDISTRIBUTABLE_PROVIDERS = frozenset({"openligadb"})


def demo_mode_errors(
    config: CompetitionFile,
    environment_keys: Sequence[str],
    file_keys: Sequence[access.FileKey],
) -> list[str]:
    """Why this setup cannot run as a demo instance; empty when it can.

    Messages name competitions, providers, and key names, never a key or hash.
    """
    allowed = ", ".join(sorted(REDISTRIBUTABLE_PROVIDERS))
    errors: list[str] = []
    if environment_keys:
        errors.append(
            "FOOTBALL_RESULTS_API_KEYS is set; a demo instance has no admin keys, so leave it empty"
        )
    errors.extend(
        f"API keys file entry {item.name!r} has role admin; a demo instance allows only client keys"
        for item in file_keys
        if item.role == "admin"
    )
    for competition in config.competitions:
        for label, provider in (
            ("provider", competition.provider),
            ("fallback_provider", competition.fallback_provider),
        ):
            if provider is not None and provider not in REDISTRIBUTABLE_PROVIDERS:
                errors.append(
                    f"competition {competition.slug!r} uses {label} {provider!r}, which is not "
                    f"publicly redistributable (allowed: {allowed})"
                )
    return errors


DEFAULT_DATABASE_PASSWORDS = frozenset({"football", "change-me"})
MIN_DATABASE_PASSWORD_LENGTH = 16


def weak_database_password(database_url: str) -> bool:
    """Whether the URL's password is missing, a shipped default, or too short."""
    try:
        password = make_url(database_url).password or ""
    except ArgumentError:
        return True
    return password in DEFAULT_DATABASE_PASSWORDS or len(password) < MIN_DATABASE_PASSWORD_LENGTH


WEAK_DATABASE_PASSWORD_ERROR = (
    "the database password is missing, a shipped default, or shorter than "
    f"{MIN_DATABASE_PASSWORD_LENGTH} characters; set POSTGRES_PASSWORD to "
    "`openssl rand -hex 32` before the database is first created"
)


def check_demo_mode(config: CompetitionFile, file_keys: Sequence[access.FileKey]) -> None:
    """Raise when ``FOOTBALL_RESULTS_DEMO_MODE`` is on and the setup breaks ADR 0007."""
    if not settings.demo_mode:
        return
    errors = demo_mode_errors(config, access.env_keys(settings.api_keys), file_keys)
    if not (settings.operator_contact or "").strip():
        errors.append(
            "FOOTBALL_RESULTS_OPERATOR_CONTACT is not set; a public instance must name its "
            "operator on /privacy"
        )
    if weak_database_password(settings.database_url):
        errors.append(WEAK_DATABASE_PASSWORD_ERROR)
    if errors:
        raise ValueError(
            "FOOTBALL_RESULTS_DEMO_MODE is on, but:\n" + "\n".join(f"  - {item}" for item in errors)
        )


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def private_instance_errors(current: Settings) -> list[str]:
    """Why these settings cannot run as a private instance (ADR 0008); empty when they can."""
    if current.anonymous_access:
        return []
    errors: list[str] = []
    if current.demo_mode:
        errors.append("FOOTBALL_RESULTS_DEMO_MODE is on; a demo instance is public by design")
    if access.env_keys(current.api_keys):
        errors.append(
            "FOOTBALL_RESULTS_API_KEYS is set; a private instance accepts only hashed keys from "
            "FOOTBALL_RESULTS_API_KEYS_FILE (`python -m app.config keys issue`)"
        )
    if current.api_keys_file is None:
        errors.append(
            "FOOTBALL_RESULTS_API_KEYS_FILE is not set; without it no caller can get a key"
        )
    base_url = current.public_base_url
    if base_url is None:
        errors.append("FOOTBALL_RESULTS_PUBLIC_BASE_URL is not set; set it to the https URL")
    elif urlsplit(base_url).hostname not in LOCAL_HOSTS:
        if urlsplit(base_url).scheme != "https":
            errors.append(
                f"FOOTBALL_RESULTS_PUBLIC_BASE_URL {base_url} is not https; keys must not travel "
                "in plain text"
            )
        if weak_database_password(current.database_url):
            errors.append(WEAK_DATABASE_PASSWORD_ERROR)
    return errors


def check_private_instance() -> None:
    """Raise when ``FOOTBALL_RESULTS_ANONYMOUS_ACCESS`` is off and the setup breaks ADR 0008."""
    errors = private_instance_errors(settings)
    if errors:
        raise ValueError(
            "FOOTBALL_RESULTS_ANONYMOUS_ACCESS is off (private instance), but:\n"
            + "\n".join(f"  - {item}" for item in errors)
        )


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
    parser = argparse.ArgumentParser(
        description="Validate the football competition config, or manage API keys."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser(
        "check", help="validate the configured YAML file and the API keys file"
    )
    check_parser.add_argument("--config-file", type=Path, default=settings.config_file)
    check_parser.add_argument("--api-keys-file", type=Path, default=settings.api_keys_file)
    hash_parser = subparsers.add_parser(
        "hash-key", help="print a new random API key and its keys-file entry"
    )
    hash_parser.add_argument("--name", required=True, help="key name ([a-z0-9-])")
    hash_parser.add_argument("--role", choices=access.ROLES, default="client")
    hash_parser.add_argument("--days", type=int, default=access.DEFAULT_KEY_DAYS)
    _add_keys_parser(subparsers)
    args = parser.parse_args(argv)
    if args.command == "hash-key":
        return _hash_key(parser, args.name, args.role, args.days)
    if args.command == "keys":
        return _keys_command(parser, args)
    try:
        config = load_config(args.config_file)
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    keys: tuple[access.FileKey, ...] = ()
    if args.api_keys_file is not None:
        try:
            keys = access.load_keys(args.api_keys_file)
        except ValueError as error:
            parser.error(str(error))
    try:
        check_demo_mode(config, keys)
        check_private_instance()
    except ValueError as error:
        parser.error(str(error))
    warnings = [
        *config.fallback_warnings(
            settings.football_data_api_token, settings.thesportsdb_api_key.get_secret_value()
        ),
        *access.expiry_warnings(keys, datetime.now(UTC)),
    ]
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Configuration valid: {args.config_file} ({len(config.competitions)} competitions)")
    if args.api_keys_file is not None:
        admins = sum(1 for key in keys if key.role == "admin")
        subjects = sum(1 for key in keys if key.subject is not None)
        counts = f"{len(keys) - subjects} keys, {admins} admin"
        if subjects:
            counts += f", {subjects} OAuth subjects"
        print(f"API keys file valid: {args.api_keys_file} ({counts})")
    if settings.oauth_issuer:
        print(
            f"OAuth for /mcp: issuer {settings.oauth_issuer}, audience "
            f"{settings.oauth_token_audience}, mode {settings.oauth_mode}"
        )
    if settings.demo_mode:
        print(
            "Demo mode: read-only (no admin keys), providers limited to "
            + ", ".join(sorted(REDISTRIBUTABLE_PROVIDERS))
        )
    if not settings.anonymous_access:
        print("Private instance: every data operation needs an API key")
    return 0


def _hash_key(parser: argparse.ArgumentParser, name: str, role: str, days: int) -> int:
    if not 1 <= days <= access.MAX_KEY_LIFETIME.days:
        parser.error(f"--days must be between 1 and {access.MAX_KEY_LIFETIME.days}")
    key = access.generate_key()
    entry = access.key_entry_yaml(name, role, key, datetime.now(UTC), days)  # type: ignore[arg-type]
    try:
        access.KeysFile.model_validate(yaml.safe_load("keys:\n" + entry))
    except ValidationError as error:
        parser.error(f"invalid key name {name!r}: {error.errors()[0]['msg']}")
    print(f"API key (give it to the client; it is not stored): {key}", file=sys.stderr)
    print("Add this entry under `keys:` in the API keys file:", file=sys.stderr)
    print(entry, end="")
    return 0


def _add_keys_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    keys_parser = subparsers.add_parser(
        "keys", help="issue, extend, revoke, or list the keys in the API keys file (ADR 0008)"
    )
    keys_parser.add_argument(
        "--file",
        type=Path,
        default=settings.api_keys_file,
        help="API keys file (default: FOOTBALL_RESULTS_API_KEYS_FILE)",
    )
    actions = keys_parser.add_subparsers(dest="action", required=True)
    days_help = f"days until it expires (1-{access.MAX_KEY_LIFETIME.days})"
    issue = actions.add_parser("issue", help="add a key and print it once")
    issue.add_argument("--name", required=True, help="key name ([a-z0-9-])")
    issue.add_argument("--role", choices=access.ROLES, default="client")
    issue.add_argument("--days", type=int, default=access.DEFAULT_KEY_DAYS, help=days_help)
    issue.add_argument("--read-per-minute", type=int)
    issue.add_argument("--write-per-minute", type=int, help="admin keys only")
    extend = actions.add_parser(
        "extend",
        help="set a key's expiry to --days from now, at most a year after it was issued",
    )
    extend.add_argument("--name", required=True)
    extend.add_argument("--days", type=int, default=access.DEFAULT_KEY_DAYS, help=days_help)
    revoke = actions.add_parser("revoke", help="remove an entry")
    revoke.add_argument("--name", required=True)
    actions.add_parser("list", help="names, roles, and expiry (never keys or hashes)")


def _keys_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    path: Path | None = args.file
    if path is None:
        parser.error("set --file or FOOTBALL_RESULTS_API_KEYS_FILE")
    now = datetime.now(UTC)
    try:
        if args.action == "issue":
            key, entry = access.issue_key(
                path,
                args.name,
                args.role,
                args.days,
                now,
                read_per_minute=args.read_per_minute,
                write_per_minute=args.write_per_minute,
            )
            assert entry.expires_at is not None
            print(
                f"Issued {entry.role} key {entry.name!r}, valid until "
                f"{access.format_time(entry.expires_at)}. It is shown once and not stored; send "
                "it over a private channel:",
                file=sys.stderr,
            )
            print(key)
        elif args.action == "extend":
            entry = access.extend_key(path, args.name, args.days, now)
            assert entry.expires_at is not None
            print(f"Key {entry.name!r} now expires at {access.format_time(entry.expires_at)}")
        elif args.action == "revoke":
            access.revoke_key(path, args.name)
            print(f"Removed {args.name!r}; the API stops accepting it within seconds")
        else:
            _print_key_list(access.list_entries(path), now)
    except ValueError as error:
        parser.error(str(error))
    return 0


def _print_key_list(entries: Sequence[access.KeyEntry], now: datetime) -> None:
    rows = [("NAME", "ROLE", "KIND", "ISSUED", "EXPIRES", "STATUS")]
    for item in entries:
        if item.subject is not None:
            status = "oauth subject"
        elif item.expires_at is None or item.issued_at is None:
            status = "no expiry: run keys extend"
        elif now >= item.expires_at:
            status = "expired"
        elif item.expires_at - now <= access.EXPIRY_WARNING:
            status = f"expires in {(item.expires_at - now).days} days"
        else:
            status = "ok"
        rows.append(
            (
                item.name,
                item.role,
                "subject" if item.subject is not None else "key",
                access.format_time(item.issued_at) if item.issued_at else "-",
                access.format_time(item.expires_at) if item.expires_at else "-",
                status,
            )
        )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


if __name__ == "__main__":
    sys.exit(cli())
