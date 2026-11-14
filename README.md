# Football Results API

FastAPI service that imports football fixtures and results from public sources (90minut.pl and
football-data.org), stores them in PostgreSQL, keeps current seasons up to date with an adaptive
scheduler, and serves them over a REST API.

The repository started as an Ekstraklasa-only scraper and is evolving into a **generic, configurable
league-data API**. This README describes both:

- **[Current state](#current-state)** – what is implemented and works today (version 0.12.1).
- **[Target state](#target-state-and-roadmap)** – where the project is heading and what is still
  missing.

## At a glance

| Capability | Current state | Target state |
| --- | --- | --- |
| Competitions | 4 hard-coded in `app/competitions.py`: Ekstraklasa, I liga, Europa League, Premier League | Any league/cup, declared in a config file or through an admin API |
| Data providers | `90minut` (HTML scraping) and `football-data` (JSON API, Premier League only) | Pluggable provider adapters with a documented contract; any football-data.org competition; more sources |
| Adding a competition/season | Python code change + redeploy | Edit configuration (no code), hot-reloaded |
| Season rollover | Manual: edit `current_season` and source URLs in code | Automatic discovery of the new season's source |
| Scheduling | Adaptive per-season scheduler driven by fixture times (built-in rules) | Same engine, with policy thresholds configurable per competition |
| Configuration | Environment variables (`FOOTBALL_RESULTS_*`) for infrastructure only | Env vars for infrastructure + one validated config file for leagues, sources, and schedules |
| Observability | `/health` (DB + scheduler heartbeat), `scrape_run` table, logs | Scheduler/scrape status endpoints, metrics, alerting on stale data |
| API | Read results, manual refresh, historical fetch | Plus fixtures, standings, teams, and authenticated admin endpoints |

## Current state

### Architecture

~~~text
HTTP clients ──▶ FastAPI (app.main) ──┐
                                      ├──▶ ScraperRegistry ──▶ 90minut.pl / football-data.org
Scheduler (app.scheduler) ────────────┤
                                      └──▶ PostgresResultRepository ──▶ PostgreSQL
~~~

- FastAPI provides the HTTP API (`app/main.py`).
- Provider-specific adapters (`app/scraper.py`) parse 90minut pages and football-data.org JSON.
  They implement a small `CompetitionScraper` protocol and are selected by `ScraperRegistry` using
  the season's `provider` value.
- SQLAlchemy's async PostgreSQL dialect handles persistence (`app/repository.py`).
- Alembic owns database migrations.
- The competition registry lives in `app/competitions.py`. It is seeded into PostgreSQL when the API
  starts **and at the start of every scheduler cycle**, so the code is effectively authoritative for
  competitions, seasons, source URLs, and which season is current. Seasons added through the
  historical fetch endpoint are kept in the database alongside the seeded ones.
- A refresh replaces one competition-season atomically.
- A separate adaptive scheduler process refreshes only current seasons and sleeps between fixtures
  (`app/scheduler.py`, rules in `app/scheduling.py`).
- Season sources persist their provider so additional websites can be added without changing the
  scheduler.

Database tables: competition, competition_season, match_result, scrape_run, scheduler_heartbeat,
and alembic_version.

### Supported competitions

| Slug | Name | Provider | Seeded seasons | Current season |
| --- | --- | --- | --- | --- |
| `ekstraklasa` | PKO BP Ekstraklasa | 90minut | 2025-2026, 2026-2027 | 2026-2027 |
| `pierwsza-liga` | Betclic I liga | 90minut | 2025-2026, 2026-2027 | 2026-2027 |
| `europa-league` | UEFA Europa League (cup) | 90minut | 2025-2026, 2026-2027 | 2026-2027 |
| `premier-league` | Premier League | football-data | 2024-2025 – 2026-2027 | 2026-2027 |

### Known limitations

- Adding a competition, a season, or changing the current season requires editing
  `app/competitions.py` and redeploying.
- The football-data adapter and URL validation are restricted to the Premier League (`PL`)
  endpoint; other football-data.org competitions are not accepted yet.
- 90minut source URLs must match `http(s)://(www.)90minut.pl/liga/1/ligaNNNNN.html`.
- Scheduler thresholds (match duration, polling intervals, backoff) are constants in code.
- Only finished matches are exposed; scheduled fixtures are stored but not served.
- There is no authentication; refresh and fetch endpoints are open to any client that can reach
  the API.
- 90minut match-detail pages are cached in process memory only.

## Local development

Requirements: Python 3.12+, uv, Docker, and Docker Compose.

~~~bash
cp .env.example .env
~~~

Change POSTGRES_PASSWORD in .env. Keep the password in
FOOTBALL_RESULTS_DATABASE_URL synchronized when running the API outside Docker.

Premier League data requires a free football-data.org token. Register at
https://www.football-data.org/client/register and put the generated token in `.env`:

~~~dotenv
FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN=your-token-here
~~~

Do not commit the token. The API can start without it and the 90minut competitions continue to
work, but Premier League refreshes will report a clear configuration error until it is configured.

### Run PostgreSQL in Docker and FastAPI locally

This is the recommended development workflow because code reload is immediate:

~~~bash
docker compose up -d db
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
~~~

In a second terminal, start the scheduler:

~~~bash
uv run python -m app.scheduler
~~~

If PostgreSQL runs directly on macOS instead of in Docker, use the same commands after setting
FOOTBALL_RESULTS_DATABASE_URL in .env; do not start the db Compose service.

The API is available at http://localhost:8000 and Swagger UI at http://localhost:8000/docs.
PostgreSQL is exposed only on 127.0.0.1:5432.

Stop services without deleting data:

~~~bash
docker compose down
~~~

Delete the development database only when intentionally starting over:

~~~bash
docker compose down --volumes
~~~

### Run the complete stack

~~~bash
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
~~~

The API container waits for PostgreSQL, runs Alembic migrations, then starts Uvicorn. The separate
scheduler container continuously claims due current seasons. Initial competitions and sources are
seeded during application startup.

## Adaptive refresh policy

The scraper stores scheduled fixtures internally, while GET /api/v1/results continues to expose
only finished matches. For each current season the worker calculates next_check_at:

- far from a fixture: once a day, or once a week when it is more than 30 days away;
- within 24 hours: at most daily, with a final check 30 minutes before kickoff;
- during a match: at its expected end (105 minutes for leagues, 135 minutes for cups);
- while waiting for a posted result: every 5 minutes initially, then 15 minutes, then hourly;
- with no published schedule: once daily;
- after source errors: 5, 15, 30, then 60 minutes.

Manual current-season refreshes use the same scheduling calculation. Historical seasons are never
scheduled automatically; they remain available through the historical fetch endpoint. PostgreSQL
row locking makes claims safe if more than one worker is accidentally running.

## Database migrations

~~~bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
uv run alembic current
~~~

Always inspect generated migrations. The application does not call create_all; schema changes must
go through Alembic.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Database connectivity and scheduler heartbeat (`ok`, `stale`, or `not_started`) |
| GET | `/api/v1/competitions` | Competitions with available/fetched seasons and scheduler state |
| GET | `/api/v1/results` | Finished matches; filters: `competition`, `season`, `round`, `stage`, `team` |
| POST | `/api/v1/results/refresh` | Re-import the current season now; optional `first_round`/`last_round` filter the response |
| POST | `/api/v1/results/history/fetch` | Import a non-current season, optionally from a supplied `source_url` |

Interactive documentation is available at `/docs`.

### Examples

List database-backed competitions and seasons:

~~~bash
curl 'http://localhost:8000/api/v1/competitions'
~~~

Refresh the current season:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/refresh \
  -H 'content-type: application/json' \
  -d '{"competition":"ekstraklasa"}'
~~~

The whole season is always re-imported. For league competitions you can limit the returned results
to a round range with `"first_round": 1, "last_round": 5` (both are required together).

Fetch a configured historical season:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'content-type: application/json' \
  -d '{"competition":"ekstraklasa","season":"2025-2026"}'
~~~

Fetch and persist an unregistered historical season:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'content-type: application/json' \
  -d '{
    "competition":"ekstraklasa",
    "season":"2024-2025",
    "source_url":"http://www.90minut.pl/liga/1/liga13482.html"
  }'
~~~

The competition-season row and matches are committed together. The season then appears in
GET /api/v1/competitions. Add force_refresh=true to replace a cached historical snapshot.
The supplied URL must use a provider already configured for that competition.

Premier League historical sources use season-specific football-data.org URLs, for example:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'content-type: application/json' \
  -d '{
    "competition":"premier-league",
    "season":"2024-2025",
    "source_url":"https://api.football-data.org/v4/competitions/PL/matches?season=2024"
  }'
~~~

Query results:

~~~bash
curl 'http://localhost:8000/api/v1/results?competition=ekstraklasa&season=2025-2026'
curl 'http://localhost:8000/api/v1/results?competition=pierwsza-liga&round=3'
curl 'http://localhost:8000/api/v1/results?competition=europa-league&stage=eliminacyjna'
curl 'http://localhost:8000/api/v1/results?competition=premier-league&round=2'
curl 'http://localhost:8000/api/v1/results?team=Wisła'
~~~

### Match statistics

Finished results include nullable statistics. Fields stay null when the configured provider does
not supply enough information; the API never invents a zero for unavailable data.

| Fields | 90minut | football-data.org |
| --- | --- | --- |
| home_goals, away_goals | yes | yes |
| first-half goals | derived from match-page goal events | yes, when supplied |
| second-half goals | derived from match-page goal events | derived from full-time minus half-time |
| home_cards, away_cards | match-page card markers | booking-event count |
| first_team_to_score, first_goal_minute | match-page goal events | unfolded goal events |
| corners and fouls | unavailable | unavailable |

`home_score` and `away_score` remain in the response for backward compatibility and have the same
values as the full-time goal fields. Stoppage-time first goals use strings such as `45+2` so the
additional minute is not lost.
If a 90minut goal event has no readable minute, refresh continues; minute-dependent fields remain
null for that match rather than failing the complete competition refresh.

For finished 90minut fixtures which link to an individual `mecz.php` page, refresh also downloads
that detail page. Detail responses are cached for the lifetime of the API or scheduler process, so
subsequent refreshes do not repeatedly request old match pages. A forced historical refresh can be
used to enrich results which were stored before detail parsing was added. Corners and fouls remain
null because the configured 90minut pages do not publish those figures.

## Ubuntu deployment

The Compose deployment runs PostgreSQL and the API on the same Ubuntu machine.

1. Install Docker Engine and the Docker Compose plugin.
2. Copy the project to the server.
3. Create production configuration:

~~~bash
cp .env.example .env
chmod 600 .env
~~~

4. Set a long random POSTGRES_PASSWORD, a contact USER_AGENT, and the desired API_BIND_ADDRESS.
5. Start the services:

~~~bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 api scheduler
~~~

Both containers use restart: unless-stopped. PostgreSQL data is stored in the postgres_data named
volume. Port 5432 remains bound to localhost.

When using Caddy or nginx, set API_BIND_ADDRESS=127.0.0.1 and expose only the reverse proxy's
HTTP/HTTPS ports through the Ubuntu firewall.

### Backups

~~~bash
chmod +x scripts/backup-postgres.sh
./scripts/backup-postgres.sh
~~~

Backups go to ./backups and files older than seven days are removed. Example cron entry:

~~~cron
15 3 * * * cd /opt/football-results-api && ./scripts/backup-postgres.sh >> /var/log/football-results-backup.log 2>&1
~~~

Copy backups to another disk or machine. A backup stored only on the old computer does not protect
against disk failure.

Example restore:

~~~bash
docker compose exec -T db pg_restore \
  --clean --if-exists \
  --username football \
  --dbname football \
  < backups/football-YYYYMMDDTHHMMSSZ.dump
~~~

## Configuration

Infrastructure settings come from environment variables or `.env` (see `app/config.py`).
Competitions and their sources are **not** configurable this way yet; they live in
`app/competitions.py`.

Application variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| FOOTBALL_RESULTS_DATABASE_URL | `postgresql+psycopg_async://football:football@localhost:5432/football` | PostgreSQL connection |
| FOOTBALL_RESULTS_REQUEST_TIMEOUT_SECONDS | `20` | HTTP timeout for source requests |
| FOOTBALL_RESULTS_USER_AGENT | `FootballResultsAPI/0.11 (contact: ...)` | Sent to sources; include a contact address |
| FOOTBALL_RESULTS_SCHEDULER_LOOP_SECONDS | `30` | Pause between scheduler cycles |
| FOOTBALL_RESULTS_SCHEDULER_CLAIM_LIMIT | `5` | Max due seasons refreshed per cycle |
| FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN | empty | football-data.org token (needed for Premier League) |
| FOOTBALL_RESULTS_FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS | `6` | Throttle for football-data.org (free tier: 10 req/min) |

Deployment variables (Docker Compose):

- POSTGRES_DB
- POSTGRES_USER
- POSTGRES_PASSWORD
- POSTGRES_PORT
- API_BIND_ADDRESS
- API_PORT

Never commit .env.

## Tests and validation

~~~bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run alembic upgrade head --sql
~~~

API tests use a repository test double and do not require PostgreSQL. Before deployment, run the
Compose stack and exercise at least one real refresh against PostgreSQL.

## Target state and roadmap

> Everything in this section is **planned, not implemented**. It describes the intended direction so
> contributors can work toward a shared design.

### Goals

1. **Generic** – support any football league or cup, not only Ekstraklasa, without code changes.
2. **Up to date** – import new data as soon as it is likely to be published, without hammering
   sources.
3. **Easy to configure** – one readable, validated configuration file plus a few environment
   variables; sensible defaults everywhere.

### Declarative competition configuration

Replace the hard-coded registry in `app/competitions.py` with a configuration file, for example
`config/competitions.yaml`, loaded and validated with Pydantic at startup:

~~~yaml
defaults:
  schedule:
    policy: adaptive
    pre_match_check_minutes: 30
    match_duration_minutes: { league: 105, cup: 135 }
    result_poll_minutes: [5, 15, 60]
    idle_interval: 1d
    failure_backoff_minutes: [5, 15, 30, 60]

providers:
  90minut:
    min_request_interval_seconds: 2
  football-data:
    api_token: ${FOOTBALL_DATA_API_TOKEN}
    min_request_interval_seconds: 6

competitions:
  - slug: ekstraklasa
    name: PKO BP Ekstraklasa
    country: Poland
    format: league
    provider: 90minut
    current_season: 2026-2027
    seasons:
      2026-2027: { league_id: 14675 }
      2025-2026: { league_id: 14072 }

  - slug: premier-league
    name: Premier League
    country: England
    format: league
    provider: football-data
    provider_options: { competition_code: PL }
    current_season: 2026-2027
    seasons: auto            # derive URLs from the season, no per-season entry needed
    schedule:
      result_poll_minutes: [10, 30, 120]
~~~

Planned behaviour:

- The file path is set by `FOOTBALL_RESULTS_CONFIG_FILE`; a default file ships with the image.
- Invalid configuration fails fast with a clear message; `python -m app.config check` validates it
  without starting services.
- The file is reloaded on change (or on `SIGHUP`) by both API and scheduler; the database stays the
  runtime store, but competitions removed from the file are disabled rather than deleted.
- Provider-specific season parameters (`league_id`, `competition_code`) replace hand-written URLs.

### Pluggable data providers

- Formalise the existing `CompetitionScraper` protocol as a provider interface:
  `fetch_matches(competition, season)`, optional `fetch_match_details(match)`,
  `discover_seasons(competition)`, and a declared capability list (e.g. which statistics it can
  supply).
- Register providers by name (entry points or a simple registry module) so a new source is one new
  module plus configuration.
- Generalise the football-data.org adapter to any competition code (`BL1`, `SA`, `PD`, `CL`, ...).
- Candidate additional sources: official league sites, other open football APIs, and a
  generic CSV/JSON URL provider for custom feeds.
- Allow a fallback provider per competition when the primary source fails repeatedly.
- Shared HTTP layer: per-host rate limiting, retries with backoff, conditional requests
  (`ETag`/`If-Modified-Since`), and a persistent response cache instead of in-memory only.

### Scheduling

The current adaptive scheduler is the foundation; planned improvements:

- Make every threshold configurable per competition (see `schedule` above), with optional
  cron-style overrides (e.g. `cron: "0 6 * * *"`) for sources that publish on a fixed timetable.
- Automatic season rollover: detect when a provider publishes the next season, switch
  `current_season`, and archive the previous one.
- Respect provider quotas globally across all competitions and workers.
- Optional one-off backfill jobs for historical seasons, queued and processed by the scheduler
  instead of blocking an HTTP request.

### API

- Fixtures endpoint for scheduled matches (data is already stored).
- League tables/standings computed from stored results.
- Team and season resources, pagination, and `ETag` caching headers.
- Admin endpoints (protected by an API key) to list scheduler state, view recent `scrape_run`
  entries, trigger refreshes, and enable/disable competitions.
- Keep `/api/v1` backward compatible; breaking changes go to `/api/v2`.

### Operations and observability

- `/metrics` (Prometheus): refresh counts, failures, latency, data age per competition.
- Alerts when a current season has not been refreshed successfully within its expected window.
- Structured JSON logging.
- Rename remaining Ekstraklasa-specific defaults (e.g. the default `competition` in refresh
  requests) once multi-league configuration is in place.

### Suggested implementation order

1. Move the registry to a validated config file (no behaviour change).
2. Generalise the football-data.org adapter to any competition code.
3. Make scheduler thresholds configurable per competition.
4. Add fixtures and standings endpoints.
5. Add admin/status endpoints with API-key authentication and metrics.
6. Automatic season discovery and rollover.
7. Additional providers and fallback sources.
