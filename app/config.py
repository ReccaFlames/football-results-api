import argparse
import hashlib
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Literal

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


settings = Settings()


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["adaptive"] = "adaptive"
    pre_match_check_minutes: int = Field(default=30, gt=0)
    match_duration_minutes: dict[str, int] = Field(
        default_factory=lambda: {"league": 105, "cup": 135}
    )
    result_poll_minutes: list[int] = Field(default_factory=lambda: [5, 15, 60], min_length=1)
    idle_interval: str = Field(default="1d", pattern=r"^[1-9]\d*[smhd]$")
    failure_backoff_minutes: list[int] = Field(
        default_factory=lambda: [5, 15, 30, 60], min_length=1
    )

    @field_validator("match_duration_minutes")
    @classmethod
    def positive_match_durations(cls, values: dict[str, int]) -> dict[str, int]:
        if any(value <= 0 for value in values.values()):
            raise ValueError("match durations must be positive")
        return values

    @field_validator("result_poll_minutes", "failure_backoff_minutes")
    @classmethod
    def positive_intervals(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("schedule intervals must be positive")
        return values


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
    schedule: ScheduleConfig | None = None

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
        return self

    def to_competitions(self) -> dict[str, Competition]:
        return {competition.slug: competition.to_competition() for competition in self.competitions}


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
