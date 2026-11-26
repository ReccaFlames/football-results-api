"""HTTP access to the Football Results API. The landing app never imports the API package."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from football_results_web import __version__

API_URL_VARIABLE = "FOOTBALL_RESULTS_WEB_API_URL"
DEFAULT_API_URL = "http://localhost:8000"
USER_AGENT = f"football-results-web/{__version__}"
TIMEOUT = httpx.Timeout(5.0)
COMPETITIONS_TTL = timedelta(minutes=5)
# A failed fetch is retried sooner than the TTL, but not on every page view.
RETRY_AFTER_FAILURE = timedelta(seconds=30)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def api_url_from_env(environ: Mapping[str, str] = os.environ) -> str:
    """The API base URL without a trailing slash; raises `ValueError` if it is not usable."""
    url = (environ.get(API_URL_VARIABLE) or "").strip() or DEFAULT_API_URL
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError(f"{API_URL_VARIABLE} must be an http(s) base URL, got {url!r}")
    return url.rstrip("/")


def create_http_client(api_url: str) -> httpx.AsyncClient:
    """The client for API calls (the test seam)."""
    return httpx.AsyncClient(
        base_url=api_url,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=False,
    )


class MalformedResponse(ValueError):
    """An API body that does not have the documented shape."""


@dataclass(frozen=True)
class CompetitionItem:
    slug: str
    name: str
    country: str | None
    current_season: str | None


def parse_competitions(body: Any) -> list[CompetitionItem]:
    """The fields the page uses from a `list_competitions` body."""
    if not isinstance(body, list):
        raise MalformedResponse("list_competitions did not return a list")
    items = []
    for entry in body:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("slug"), str)
            or not isinstance(entry.get("name"), str)
        ):
            raise MalformedResponse("list_competitions returned an entry without a slug or name")
        country, season = entry.get("country"), entry.get("current_season")
        items.append(
            CompetitionItem(
                slug=entry["slug"],
                name=entry["name"],
                country=country if isinstance(country, str) else None,
                current_season=season if isinstance(season, str) else None,
            )
        )
    return items


async def fetch_competitions(client: httpx.AsyncClient) -> list[CompetitionItem]:
    response = await client.get("/api/v1/competitions")
    response.raise_for_status()
    return parse_competitions(response.json())


# The demo notice the API puts at the top of `/llms.txt` (ADR 0007); no API response has a
# structured demo flag, and a contract test against the real API pins this line.
DEMO_NOTICE = "**Demo instance:**"


def is_demo(llms_txt: str) -> bool:
    return any(line.startswith(DEMO_NOTICE) for line in llms_txt.splitlines())


async def fetch_demo_mode(client: httpx.AsyncClient) -> bool:
    response = await client.get("/llms.txt")
    response.raise_for_status()
    return is_demo(response.text)


class TimedCache[T]:
    """A value read from the API, refreshed at most every `COMPETITIONS_TTL`.

    After a failed refresh the last good value stays; `None` means none was ever fetched.
    """

    def __init__(
        self,
        api_url: str,
        fetch: Callable[[httpx.AsyncClient], Awaitable[T]],
        what: str,
        clock: Clock | None = None,
    ) -> None:
        self._api_url = api_url
        self._fetch = fetch
        self._what = what
        self._clock = clock or (lambda: datetime.now(UTC))
        self._value: T | None = None
        self._next_fetch_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> T | None:
        if not self._due():
            return self._value
        async with self._lock:
            if self._due():
                await self._refresh()
        return self._value

    def _due(self) -> bool:
        return self._next_fetch_at is None or self._clock() >= self._next_fetch_at

    async def _refresh(self) -> None:
        try:
            async with create_http_client(self._api_url) as client:
                self._value = await self._fetch(client)
        # ValueError covers MalformedResponse and invalid JSON.
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Could not fetch %s from the API: %s", self._what, error)
            self._next_fetch_at = self._clock() + RETRY_AFTER_FAILURE
        else:
            self._next_fetch_at = self._clock() + COMPETITIONS_TTL


class CompetitionCache(TimedCache[list[CompetitionItem]]):
    """Competitions from `list_competitions`."""

    def __init__(self, api_url: str, clock: Clock | None = None) -> None:
        super().__init__(api_url, fetch_competitions, "competitions", clock)


class DemoModeCache(TimedCache[bool]):
    """Whether the API is a demo instance, read from `/llms.txt`."""

    def __init__(self, api_url: str, clock: Clock | None = None) -> None:
        super().__init__(api_url, fetch_demo_mode, "llms.txt", clock)


ACCESS_REQUEST_PATH = "/api/v1/access-requests"


class ApiUnavailable(Exception):
    """The API could not be reached or did not answer with a documented body."""


@dataclass(frozen=True)
class AccessRequestReceipt:
    id: int
    claim_secret: str
    status_url: str


@dataclass(frozen=True)
class ValidationIssue:
    loc: tuple[str | int, ...]
    msg: str
    type: str


@dataclass(frozen=True)
class Problem:
    """An RFC 9457 problem from the API; `slug` is the last segment of its `type`."""

    slug: str
    status: int
    detail: str
    parameter: str | None = None
    errors: tuple[ValidationIssue, ...] = ()
    retry_after_seconds: int | None = None


def forwarded_for(existing: Sequence[str], client: str | None) -> str | None:
    """`X-Forwarded-For` for the API: the incoming hops with the visitor's address appended.

    The API takes the rightmost address that is not a trusted proxy, so a forged incoming
    value never replaces the address this app saw.
    """
    hops = [hop.strip() for header in existing for hop in header.split(",") if hop.strip()]
    if client:
        hops.append(client)
    return ", ".join(hops) or None


def parse_problem(response: httpx.Response) -> Problem:
    try:
        body = response.json()
    except ValueError as error:
        raise ApiUnavailable(f"API answered {response.status_code} without JSON") from error
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("type"), str)
        or not isinstance(body.get("detail"), str)
    ):
        raise ApiUnavailable(f"API answered {response.status_code} without a problem body")
    issues = []
    for item in body.get("errors") or ():
        if isinstance(item, dict) and isinstance(item.get("loc"), list):
            issues.append(
                ValidationIssue(
                    tuple(part for part in item["loc"] if isinstance(part, str | int)),
                    str(item.get("msg", "")),
                    str(item.get("type", "")),
                )
            )
    parameter = body.get("parameter")
    retry_after = response.headers.get("Retry-After", "")
    return Problem(
        slug=body["type"].rsplit("/", 1)[-1],
        status=response.status_code,
        detail=body["detail"],
        parameter=parameter if isinstance(parameter, str) else None,
        errors=tuple(issues),
        retry_after_seconds=int(retry_after) if retry_after.isdigit() else None,
    )


async def create_access_request(
    api_url: str, payload: Mapping[str, Any], forwarded: str | None
) -> AccessRequestReceipt | Problem:
    """`create_access_request`; raises `ApiUnavailable` when there is no usable answer."""
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    try:
        async with create_http_client(api_url) as client:
            response = await client.post(ACCESS_REQUEST_PATH, json=payload, headers=headers)
    except httpx.HTTPError as error:
        raise ApiUnavailable(str(error)) from error
    if response.status_code != 201:
        return parse_problem(response)
    try:
        body = response.json()
        return AccessRequestReceipt(
            id=int(body["id"]),
            claim_secret=str(body["claim_secret"]),
            status_url=str(body["status_url"]),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise ApiUnavailable("create_access_request returned an unexpected body") from error
