import ast
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from football_results_web import api as web_api
from football_results_web import main as web
from starlette.testclient import TestClient

import app.main as main_module
from app.ratelimit import FixedWindowRateLimiter
from tests.test_api import FakeRepository, use_demo_mode

ROOT = Path(__file__).resolve().parents[1]
API_URL = "http://api.test"
COMPETITIONS = [
    {"slug": "3-liga", "name": "3. Liga", "country": "Germany", "current_season": "2026-2027"},
    {"slug": "cup", "name": "<b>Cup</b>", "country": None, "current_season": None},
]


LLMS_TXT = "# Football Results API\n\n> Football data.\n"
DEMO_LLMS_TXT = "# Football Results API\n\n**Demo instance:** This is a public demo.\n"
CREATED = {
    "id": 7,
    "status": "pending",
    "claim_secret": "frc_s3cr3t-claim",
    "status_url": "/api/v1/access-requests/7",
    "created_at": "2026-11-26T12:00:00Z",
}
FORM = {
    "name": "Ada",
    "contact_email": "ada@example.com",
    "intended_use": "Weekly tables on a fan site",
    "client_type": "agent",
    "message": "",
    "website": "",
}

Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 26, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def fake_api(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    def create(api_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=api_url, transport=httpx.MockTransport(record))

    monkeypatch.setattr(web_api, "create_http_client", create)
    return sent


def routes(
    competitions: Handler | None = None,
    llms: Handler | None = None,
    access_requests: Handler | None = None,
) -> Handler:
    """A fake API: one handler per path, with working defaults."""
    handlers = {
        "/api/v1/competitions": competitions or (lambda _: httpx.Response(200, json=COMPETITIONS)),
        "/llms.txt": llms or (lambda _: httpx.Response(200, text=LLMS_TXT)),
        "/api/v1/access-requests": access_requests or (lambda _: httpx.Response(201, json=CREATED)),
    }
    return lambda request: handlers[request.url.path](request)


def in_order(*responses: httpx.Response) -> Handler:
    queue = list(responses)
    return lambda _: queue.pop(0)


def paths(sent: list[httpx.Request], path: str) -> list[httpx.Request]:
    return [request for request in sent if request.url.path == path]


def problem(status: int, slug: str, detail: str, **extensions: object) -> Handler:
    body = {"type": f"/problems/{slug}", "title": slug, "status": status, "detail": detail}
    headers = (
        {"Retry-After": str(extensions.pop("retry_after"))} if "retry_after" in extensions else {}
    )
    return lambda _: httpx.Response(
        status,
        json=body | extensions,
        headers={"Content-Type": "application/problem+json", **headers},
    )


def landing(clock: Clock | None = None) -> TestClient:
    return TestClient(web.create_app(API_URL, clock))


def test_page_lists_competitions_and_links_without_scripts_or_absolute_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = fake_api(monkeypatch, routes())

    response = landing().get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    page = response.text
    assert "<title>Whistleprint</title>" in page
    assert "<td>3. Liga</td><td>Germany</td><td>2026-2027</td>" in page
    assert "<td>&lt;b&gt;Cup&lt;/b&gt;</td><td>–</td><td>–</td>" in page
    links = re.findall(r'(?:href|src)="([^"]*)"', page)
    for target in ("docs", "openapi.json", "llms.txt", "agents#cli", "agents#mcp", "privacy"):
        assert target in links
    assert not [link for link in links if re.match(r"[a-z]+:|//|/", link)]
    assert "<script" not in page
    assert sorted(str(request.url) for request in sent) == [
        f"{API_URL}/api/v1/competitions",
        f"{API_URL}/llms.txt",
    ]


def test_competitions_and_demo_mode_are_cached_for_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = fake_api(monkeypatch, routes())
    clock = Clock()
    client = landing(clock)

    client.get("/")
    clock.now += timedelta(minutes=5) - timedelta(seconds=1)
    client.get("/")
    assert len(paths(sent, "/api/v1/competitions")) == len(paths(sent, "/llms.txt")) == 1
    clock.now += timedelta(seconds=1)
    client.get("/")
    assert len(paths(sent, "/api/v1/competitions")) == len(paths(sent, "/llms.txt")) == 2


def test_unavailable_until_first_fetch_then_retried_after_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = fake_api(
        monkeypatch,
        routes(competitions=in_order(httpx.Response(503), httpx.Response(200, json=COMPETITIONS))),
    )
    clock = Clock()
    client = landing(clock)

    page = client.get("/")
    assert page.status_code == 200
    assert "temporarily unavailable" in page.text and 'href="llms.txt"' in page.text
    clock.now += timedelta(seconds=29)
    assert "temporarily unavailable" in client.get("/").text
    clock.now += timedelta(seconds=1)
    assert "<td>3. Liga</td>" in client.get("/").text
    assert len(paths(sent, "/api/v1/competitions")) == 2


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json=[{"name": "No slug"}]),
    ],
)
def test_last_good_list_is_kept_when_a_refresh_fails(
    monkeypatch: pytest.MonkeyPatch, failure: httpx.Response
) -> None:
    fake_api(
        monkeypatch,
        routes(competitions=in_order(httpx.Response(200, json=COMPETITIONS), failure)),
    )
    clock = Clock()
    client = landing(clock)

    client.get("/")
    clock.now += timedelta(minutes=5)
    page = client.get("/").text

    assert "<td>3. Liga</td>" in page and "temporarily unavailable" not in page


def test_unreachable_api_shows_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake_api(monkeypatch, refuse)

    assert "temporarily unavailable" in landing().get("/").text


def test_reads_competitions_from_the_real_api(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository()
    monkeypatch.setattr(main_module, "store", repository)
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())
    transport = httpx.ASGITransport(app=main_module.app)

    def create(api_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=api_url, transport=transport)

    monkeypatch.setattr(web_api, "create_http_client", create)

    page = landing().get("/").text

    for competition in repository.competitions.values():
        assert f"<td>{competition.name}</td>" in page
        assert f"<td>{competition.current_season}</td>" in page


def test_private_or_public_instance_shows_the_access_request_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_api(monkeypatch, routes())

    page = landing().get("/").text

    assert '<form method="post" action="request-access">' in page
    for name in ("name", "contact_email", "intended_use", "message"):
        assert f'<label for="{name}">' in page and f'id="{name}" name="{name}"' in page
    assert 'maxlength="1000"' in page and 'maxlength="2000"' in page
    assert "<legend>Who will use the key?</legend>" in page
    assert 'name="client_type" value="human"' in page and 'value="agent"' in page
    assert 'name="competitions" value="3-liga"' in page
    assert 'name="competitions" value="cup"> &lt;b&gt;Cup&lt;/b&gt;' in page
    assert 'name="website" type="text" tabindex="-1" autocomplete="off"' in page
    assert "<script" not in page


def test_demo_instance_shows_no_key_needed_instead_of_the_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_api(monkeypatch, routes(llms=lambda _: httpx.Response(200, text=DEMO_LLMS_TXT)))

    page = landing().get("/").text

    assert "No key needed" in page and "<form" not in page


def test_unknown_demo_mode_shows_the_form(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api(monkeypatch, routes(llms=lambda _: httpx.Response(503)))

    assert "<form" in landing().get("/").text


def test_access_request_shows_the_id_and_claim_secret_once_and_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = fake_api(monkeypatch, routes())

    response = landing().post("/request-access", data=FORM | {"competitions": ["3-liga", "cup"]})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert "<dd><code>7</code></dd>" in page
    assert page.count(CREATED["claim_secret"]) == 1
    assert "football-results get-access-request 7" in page
    assert "<code>GET /api/v1/access-requests/7</code>" in page
    assert 'href="agents#cli"' in page and 'href="llms.txt"' in page
    [created] = paths(sent, "/api/v1/access-requests")
    assert created.method == "POST"
    assert json.loads(created.content) == {
        "name": "Ada",
        "contact_email": "ada@example.com",
        "intended_use": "Weekly tables on a fan site",
        "client_type": "agent",
        "competitions": ["3-liga", "cup"],
    }


def test_optional_message_is_sent_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = fake_api(monkeypatch, routes())

    landing().post("/request-access", data=FORM | {"message": "Thanks!"})

    [created] = paths(sent, "/api/v1/access-requests")
    assert json.loads(created.content)["message"] == "Thanks!"
    assert "competitions" not in json.loads(created.content)


def test_filled_honeypot_is_dropped_without_an_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = fake_api(monkeypatch, routes())

    response = landing().post("/request-access", data=FORM | {"website": "https://spam.test"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Access request sent" in response.text
    assert sent == []


@pytest.mark.parametrize(
    ("incoming", "forwarded"),
    [
        ({}, "testclient"),
        ({"X-Forwarded-For": "203.0.113.9"}, "203.0.113.9, testclient"),
        ({"X-Forwarded-For": "198.51.100.1, 203.0.113.9"}, "198.51.100.1, 203.0.113.9, testclient"),
    ],
)
def test_visitor_address_is_appended_to_x_forwarded_for(
    monkeypatch: pytest.MonkeyPatch, incoming: dict[str, str], forwarded: str
) -> None:
    sent = fake_api(monkeypatch, routes())

    landing().post("/request-access", data=FORM, headers=incoming)

    [created] = paths(sent, "/api/v1/access-requests")
    assert created.headers["x-forwarded-for"] == forwarded


VALIDATION_ERRORS = [
    {
        "loc": ["body", "contact_email"],
        "msg": "String should match pattern",
        "type": "string_pattern_mismatch",
    },
    {
        "loc": ["body", "intended_use"],
        "msg": "String should have at most 1000 characters",
        "type": "string_too_long",
    },
    {
        "loc": ["body", "client_type"],
        "msg": "Input should be 'human' or 'agent'",
        "type": "literal_error",
    },
]


@pytest.mark.parametrize(
    ("api_problem", "status", "field", "message"),
    [
        (
            problem(422, "validation-error", "body.contact_email: ...", errors=VALIDATION_ERRORS),
            422,
            "contact_email",
            "Enter an email address, for example ada@example.com.",
        ),
        (
            problem(422, "validation-error", "...", errors=VALIDATION_ERRORS),
            422,
            "intended_use",
            "Use at most 1000 characters.",
        ),
        (
            problem(422, "validation-error", "...", errors=VALIDATION_ERRORS),
            422,
            "client_type",
            "Choose whether a person or an AI agent will use the key.",
        ),
        (
            problem(
                422,
                "invalid-parameter",
                "Unknown competitions: cup",
                parameter="competitions",
                invalid=["cup"],
            ),
            422,
            "competitions",
            "Unknown competitions: cup. Choose from the list",
        ),
        (
            problem(
                409,
                "access-request-pending",
                "An access request for this contact email is already pending",
                parameter="contact_email",
            ),
            409,
            "contact_email",
            "This email address already has a pending access request.",
        ),
        (
            problem(429, "rate-limited", "Rate limit exceeded", retry_after=1750),
            429,
            None,
            "Please try again in about 30 minutes.",
        ),
        (
            problem(503, "service-unavailable", "Database unavailable"),
            503,
            None,
            "The API did not accept the request: Database unavailable",
        ),
    ],
)
def test_api_problems_are_shown_with_the_entered_values(
    monkeypatch: pytest.MonkeyPatch,
    api_problem: Handler,
    status: int,
    field: str | None,
    message: str,
) -> None:
    fake_api(monkeypatch, routes(access_requests=api_problem))
    entered = FORM | {
        "name": '<script>alert("x")</script>',
        "intended_use": "Tables & <b>stats</b>",
        "competitions": ["3-liga"],
    }

    response = landing().post("/request-access", data=entered)

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert message in page
    if field is None:
        assert 'role="alert" id="form-problem"' in page
    else:
        error = re.search(rf'<span class="field-error" id="{field}-error">([^<]*)</span>', page)
        assert error and message in error.group(1)
        assert re.search(rf'aria-describedby="[^"]*\b{field}-error\b', page)
    assert "<script>" not in page
    assert 'value="&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;"' in page
    assert ">Tables &amp; &lt;b&gt;stats&lt;/b&gt;</textarea>" in page
    assert 'value="ada@example.com"' in page
    assert 'value="agent" checked' in page
    assert 'value="3-liga" checked' in page and 'value="cup" checked' not in page


def test_writes_disabled_shows_no_key_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api(
        monkeypatch,
        routes(access_requests=problem(503, "writes-disabled", "Access requests are disabled")),
    )

    response = landing().post("/request-access", data=FORM)

    assert "No key needed" in response.text and "<form" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "failure",
    [
        "refused",
        httpx.Response(502, text="<html>Bad gateway</html>"),
        httpx.Response(201, json={"id": 7}),
    ],
)
def test_unreachable_api_keeps_the_entries(
    monkeypatch: pytest.MonkeyPatch, failure: str | httpx.Response
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, str):
            raise httpx.ConnectError(failure, request=request)
        return failure

    fake_api(monkeypatch, routes(access_requests=respond))

    response = landing().post("/request-access", data=FORM)

    assert response.status_code == 503
    assert "The API cannot be reached right now" in response.text
    assert 'value="ada@example.com"' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_get_request_access_redirects_to_the_form() -> None:
    response = landing().get("/request-access", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "./#request-access"


def real_api(monkeypatch: pytest.MonkeyPatch, **settings: object) -> FakeRepository:
    repository = FakeRepository()
    monkeypatch.setattr(main_module, "store", repository)
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())
    for name, value in settings.items():
        monkeypatch.setattr(main_module.settings, name, value)
    transport = httpx.ASGITransport(app=main_module.app)
    monkeypatch.setattr(
        web_api,
        "create_http_client",
        lambda api_url: httpx.AsyncClient(base_url=api_url, transport=transport),
    )
    return repository


def test_access_request_reaches_the_real_api_and_counts_visitors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Caddy (10.0.0.2) sends visitors to the landing app, which reaches the API from 127.0.0.1
    # (the ASGI transport); both are trusted proxies, as in the deployment.
    repository = real_api(monkeypatch, trusted_proxies="127.0.0.1/32,10.0.0.2/32")
    client = TestClient(web.create_app(API_URL), client=("10.0.0.2", 50000))

    def send(visitor: str, **fields: object) -> httpx.Response:
        return client.post(
            "/request-access", data=FORM | fields, headers={"X-Forwarded-For": visitor}
        )

    page = send("192.0.2.1", competitions=["ekstraklasa"]).text
    [stored] = repository.access_requests.values()
    assert f"<dd><code>{stored.id}</code></dd>" in page
    assert list(stored.competitions) == ["ekstraklasa"]

    for number in range(3):
        assert send("203.0.113.9", contact_email=f"user{number}@example.com").status_code == 200
    limited = send("203.0.113.9", contact_email="late@example.com")
    assert limited.status_code == 429 and "Please try again in about" in limited.text
    assert send("198.51.100.1", contact_email="late@example.com").status_code == 200

    pending = send("198.51.100.2", contact_email="late@example.com")
    assert pending.status_code == 409 and "already has a pending access request" in pending.text
    invalid = send("198.51.100.3", contact_email="nope", client_type="robot")
    assert invalid.status_code == 422
    assert 'id="contact_email-error">Enter an email address' in invalid.text
    assert 'id="client_type-error">Choose whether' in invalid.text


def test_demo_mode_is_read_from_the_real_api(monkeypatch: pytest.MonkeyPatch) -> None:
    real_api(monkeypatch)
    assert "<form" in landing().get("/").text

    use_demo_mode(monkeypatch)
    assert "No key needed" in landing().get("/").text
    response = landing().post("/request-access", data=FORM)
    assert "No key needed" in response.text and "<form" not in response.text


def test_form_limits_match_the_api_schema() -> None:
    schema = main_module.app.openapi()["components"]["schemas"]["AccessRequestCreate"]

    for name, limit in web.MAX_LENGTHS.items():
        field = schema["properties"][name]
        [text] = [item for item in field.get("anyOf", [field]) if item["type"] == "string"]
        assert text["maxLength"] == limit, name
    assert set(schema["required"]) == {"name", "contact_email", "intended_use", "client_type"}
    assert set(schema["properties"]) == {*web.MAX_LENGTHS, "client_type", "competitions"}


def test_static_assets_and_favicon_are_served() -> None:
    client = landing()

    assert client.get("/favicon.ico").headers["content-type"] == "image/x-icon"
    assert client.get("/static/whistleprint-mark.svg").status_code == 200
    assert client.get("/static/style.css").headers["content-type"].startswith("text/css")


def test_http_client_identifies_itself_and_does_not_follow_redirects() -> None:
    client = web_api.create_http_client(API_URL)

    assert client.headers["User-Agent"] == f"football-results-web/{version('football-results-web')}"
    assert client.follow_redirects is False


@pytest.mark.parametrize("value", ["ftp://api", "http://", "http://user:pw@api", "http://api?x=1"])
def test_api_url_must_be_a_plain_http_base_url(value: str) -> None:
    with pytest.raises(ValueError, match="FOOTBALL_RESULTS_WEB_API_URL"):
        web_api.api_url_from_env({"FOOTBALL_RESULTS_WEB_API_URL": value})


def test_api_url_defaults_to_a_local_api_without_trailing_slash() -> None:
    assert web_api.api_url_from_env({}) == "http://localhost:8000"
    assert web_api.api_url_from_env({"FOOTBALL_RESULTS_WEB_API_URL": "http://api:8000/"}) == (
        "http://api:8000"
    )


def _imported_packages(directory: Path) -> set[str]:
    names = set()
    for path in directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split(".")[0])
    return names


def test_landing_app_and_api_do_not_import_each_other() -> None:
    assert "app" not in _imported_packages(ROOT / "web")
    assert "football_results_web" not in _imported_packages(ROOT / "app")


def test_landing_app_and_api_versions_match() -> None:
    assert version("football-results-web") == version("football-results-api")
