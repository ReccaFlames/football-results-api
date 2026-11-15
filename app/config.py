from importlib.metadata import version

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FOOTBALL_RESULTS_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg_async://football:football@localhost:5432/football"
    request_timeout_seconds: float = 20.0
    user_agent: str = (
        f"FootballResultsAPI/{version('football-results-api')} "
        "(contact: set-your-email@example.com)"
    )
    scheduler_loop_seconds: float = 30.0
    scheduler_claim_limit: int = 5
    api_keys: str = ""
    football_data_api_token: str | None = None
    football_data_min_request_interval_seconds: float = 6.0
    ninety_minut_min_request_interval_seconds: float = Field(default=2.0, ge=0)
    ninety_minut_detail_cache_max_entries: int = Field(default=512, ge=0)


settings = Settings()
