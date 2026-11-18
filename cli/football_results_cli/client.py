"""HTTP access to the Football Results API: configuration, retries, and error mapping."""

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from football_results_cli import __version__
from football_results_cli.problems import CliFailure, cli_problem, exit_code_for

DEFAULT_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
USER_AGENT = f"football-results-cli/{__version__}"
MAX_RETRIES = 2
MAX_RETRY_AFTER_SECONDS = 30.0
RETRY_STATUSES = (429, 503)


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS


def normalize_url(value: str | None) -> str:
    """The API base URL without a trailing slash; raises a usage error if it is not usable."""
    url = (value or "").strip() or DEFAULT_URL
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise cli_problem(
            "usage-error",
            f"Invalid API URL {url!r}",
            parameter="url",
            hint=(
                "Set FOOTBALL_RESULTS_URL or --url to the API base URL, for example "
                f"{DEFAULT_URL} or https://football.example.com/prefix"
            ),
        )
    return url.rstrip("/")


def create_http_client(config: Config) -> httpx.Client:
    # Redirects are not followed, so the API key is only ever sent to the configured base URL.
    return httpx.Client(
        base_url=config.url,
        timeout=httpx.Timeout(config.timeout, connect=min(config.timeout, 10.0)),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
    )


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any


class ApiClient:
    def __init__(
        self,
        config: Config,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.http = http
        self.sleep = sleep

    def get(
        self, path: str, params: Mapping[str, Any] | None = None, *, auth: bool = False
    ) -> ApiResponse:
        query = {key: value for key, value in (params or {}).items() if value is not None}
        for attempt in range(MAX_RETRIES + 1):
            response = self._send("GET", path, params=query, auth=auth)
            delay = retry_delay(response)
            if attempt == MAX_RETRIES or delay is None:
                break
            self.sleep(delay)
        return self._result(response)

    def post(self, path: str, body: Mapping[str, Any], *, auth: bool = True) -> ApiResponse:
        payload = {key: value for key, value in body.items() if value is not None}
        return self._result(self._send("POST", path, json=payload, auth=auth))

    def _send(self, method: str, path: str, *, auth: bool, **kwargs: Any) -> httpx.Response:
        headers = {"X-API-Key": self.config.api_key} if auth and self.config.api_key else {}
        url = f"{self.config.url}{path}"
        try:
            return self.http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as error:
            raise cli_problem(
                "timeout",
                f"No response from {url} within {self.config.timeout:g} seconds",
                hint="Retry later, or raise --timeout / FOOTBALL_RESULTS_TIMEOUT.",
            ) from error
        except httpx.HTTPError as error:
            raise cli_problem(
                "connection-failed",
                f"Cannot reach {url}: {error}",
                hint="Check that the API is running and FOOTBALL_RESULTS_URL / --url is right.",
            ) from error

    def _result(self, response: httpx.Response) -> ApiResponse:
        status = response.status_code
        if 300 <= status < 400:
            raise cli_problem(
                "invalid-response",
                f"The API answered {status} with a redirect, which the CLI does not follow",
                http_status=status,
                location=response.headers.get("Location"),
                hint="Set FOOTBALL_RESULTS_URL / --url to the final base URL (often https://).",
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        if status >= 400:
            if isinstance(body, dict) and isinstance(body.get("type"), str) and "status" in body:
                raise CliFailure(body, exit_code_for(status, body["type"]))
            failure = cli_problem(
                "invalid-response",
                f"The API answered {status} without a problem details body",
                http_status=status,
                hint="Check that FOOTBALL_RESULTS_URL / --url points at the Football Results API.",
            )
            raise CliFailure(failure.problem, exit_code_for(status))
        if body is None:
            raise cli_problem(
                "invalid-response",
                f"The API answered {status} without a JSON body",
                http_status=status,
                hint="Check that FOOTBALL_RESULTS_URL / --url points at the Football Results API.",
            )
        return ApiResponse(status, body)


def retry_delay(response: httpx.Response, now: datetime | None = None) -> float | None:
    """Seconds to wait before retrying, or ``None`` when the response should not be retried."""
    if response.status_code not in RETRY_STATUSES:
        return None
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        delay = (moment - (now or datetime.now(UTC))).total_seconds()
    if not math.isfinite(delay) or delay > MAX_RETRY_AFTER_SECONDS:
        return None
    return max(delay, 0.0)
