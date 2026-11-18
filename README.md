# Football Results API

FastAPI service that imports football fixtures and results from public sources (90minut.pl and
football-data.org), stores them in PostgreSQL, keeps current seasons up to date with an adaptive
scheduler, and serves them over a REST API.

The repository started as an Ekstraklasa-only scraper and is evolving into a **generic, configurable
league-data API**. This README describes both:

- **[Current state](#current-state)** – what is implemented and works today (version 0.15.0).
- **[Target state](#target-state-and-roadmap)** – where the project is heading and what is still
  missing.

## At a glance

| Capability | Current state | Target state |
| --- | --- | --- |
| Competitions | 4 declared in `config/competitions.yaml`: Ekstraklasa, I liga, Europa League, Premier League | Any league/cup, declared in a config file or through an admin API |
| Data providers | `90minut` (HTML scraping) and `football-data` (JSON API, any competition code) behind a [documented provider contract](docs/PROVIDERS.md) | More sources and fallback providers |
| Adding a competition/season | Edit `config/competitions.yaml`; the scheduler applies changes | Edit configuration (no code), hot-reloaded |
| Seasons | Split (`2026-2027`) or calendar-year (`2026`) per competition, with a configurable start month | Same |
| Season rollover | Automatic: providers with discovery (football-data) add new seasons; the scheduler switches `current_season` once the new season has started and the old one is finished. 90minut sources are added to YAML by hand | Same, with more discovering providers |
| Scheduling | Adaptive per-season scheduler driven by fixture times; thresholds from `defaults.schedule` with per-competition overrides | Plus cron-style overrides and global provider quotas |
| Configuration | Environment variables plus validated YAML for leagues, sources, providers, and schedule defaults | Env vars for infrastructure + one validated config file for leagues, sources, and schedules |
| Observability | `/health` (DB + scheduler heartbeat), `scrape_run` table, logs; CI with PostgreSQL integration tests | Scheduler/scrape status endpoints, metrics, alerting on stale data |
| API | Matches of every status with filters, cursor pagination, `fields=` and per-season freshness; computed standings; teams with aliases; manual refresh and historical fetch; RFC 9457 errors, rate limits, and `ETag` caching; generated `/llms.txt` and `/agents` guide; `football-results` CLI, Agent Skill, and curated MCP server at `/mcp` | Plus authenticated admin endpoints |

## Current state

### Architecture

~~~text
HTTP clients ──▶ FastAPI (app.main) ───────────────▶ PostgresResultRepository ──▶ PostgreSQL
Scheduler (app.scheduler) ──┬──▶ ScraperRegistry ──▶ sources
                            └─────────────────────▶ PostgresResultRepository
~~~

- FastAPI provides the HTTP API (`app/main.py`).
- Provider-specific adapters (`app/scraper.py`) parse 90minut pages and football-data.org JSON.
  They implement the `CompetitionScraper` provider contract (`scrape`, `discover_seasons`, and
  declared capabilities; see [docs/PROVIDERS.md](docs/PROVIDERS.md)) and are selected by
  `ScraperRegistry` using the season's `provider` value.
- Each competition declares a season format: `split` seasons (`2026-2027`) start in a configurable
  month (July by default); `calendar` seasons (`2026`) follow the calendar year. `app/seasons.py`
  maps dates to seasons, for example to date 90minut fixtures that omit the year.
- SQLAlchemy's async PostgreSQL dialect handles persistence (`app/repository.py`).
- Alembic owns database migrations.
- `config/competitions.yaml` is validated and seeded into PostgreSQL when the API or scheduler starts.
  The scheduler reseeds only when the file contents change. Configured competitions are authoritative
  for names and sources. Seasons added by discovery or the historical fetch endpoint remain in the
  database alongside them. `current_season` is exact with `season_rollover: manual`, and a minimum
  with `auto` (see [Season discovery and rollover](#season-discovery-and-rollover)). Removed
  competitions are disabled and retained in the database, including their matches.
- A refresh reconciles one competition-season atomically, retaining match IDs by provider match ID
  or a team/stage key that ignores kickoff-time changes.
- Teams have canonical slugs and normalized aliases; match rows reference team records while
  preserving provider display names.
- A separate adaptive scheduler process refreshes only current seasons and sleeps between fixtures
  (`app/scheduler.py`, rules in `app/scheduling.py`).
- Season sources persist their provider so additional websites can be added without changing the
  scheduler.

Database tables: competition, competition_season, team, team_alias, match_result, scrape_run,
scheduler_heartbeat, and alembic_version.

### Supported competitions

| Slug | Name | Provider | Seeded seasons | Current season |
| --- | --- | --- | --- | --- |
| `ekstraklasa` | PKO BP Ekstraklasa | 90minut | 2025-2026, 2026-2027 | 2026-2027 |
| `pierwsza-liga` | Betclic I liga | 90minut | 2025-2026, 2026-2027 | 2026-2027 |
| `europa-league` | UEFA Europa League (cup) | 90minut | 2025-2026, 2026-2027 | 2026-2027 |
| `premier-league` | Premier League | football-data | 2024-2025 – 2026-2027 | 2026-2027 |

### Known limitations

- Source URLs are validated against the currently supported 90minut and football-data.org endpoints.
- football-data.org sources must use `/v4/competitions/<CODE>/matches?season=<start year>`, and
  all sources of one competition must share the same `<CODE>`.
- Season discovery is implemented for football-data.org only; new 90minut seasons must be added to
  the YAML by hand (rollover to them is then automatic).
- 90minut source URLs must match `http(s)://(www.)90minut.pl/liga/1/ligaNNNNN.html`.
- Standings use points (3/1/0), then goal difference, goals for, wins, and team name. Head-to-head
  records and point deductions are not applied, so leagues that rank on head-to-head first (such
  as Ekstraklasa) can differ from the official table when teams are level on points.
- Cup standings need a `stage` (for example a group) and only count matches whose stage contains
  it; knockout rounds produce a table but it has little meaning.
- `inactive` covers postponed, suspended, and cancelled matches; the API does not distinguish them.
- Team identity is by normalized display name: two spellings of one club are separate teams until
  an alias row links them.
- Cursors are keyset positions on (kickoff time, match ID). A match whose kickoff changes while a
  client pages through results can be skipped or repeated; match IDs themselves never change.
- Refresh and history-fetch requests require an API key; write endpoints return `503` until at
  least one key is configured.
- 90minut league and match-detail requests are throttled per host (2 seconds by default).
  Detail-page failures leave that match's optional detail fields null and are logged without
  failing the season refresh. Successfully parsed details use a bounded, process-local LRU cache.

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

Protect refresh and history-fetch requests by setting one or more comma-separated API keys:

~~~dotenv
FOOTBALL_RESULTS_API_KEYS=replace-with-a-long-random-key
~~~

Send a configured key in the `X-API-Key` header. Keep API keys out of source control and logs.
With no keys configured, the write endpoints are disabled and return `503`.

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

The scraper stores every fixture; GET /api/v1/matches serves all statuses, while GET
/api/v1/results continues to expose only finished matches. For each current season the worker calculates next_check_at. The thresholds
come from `defaults.schedule` in `config/competitions.yaml` and can be overridden per competition
(see [Schedule settings](#schedule-settings)); the shipped defaults are:

- far from a fixture: once a day (`idle_interval`), or once a week (`off_season_interval`) when it
  is more than 30 days away (`off_season_after`);
- within 24 hours: at most daily, with a final check 30 minutes before kickoff
  (`pre_match_check_minutes`);
- during a match: at its expected end (`match_duration_minutes`: 105 for leagues, 135 for cups);
- while waiting for a posted result: every 5 minutes for the first 30 minutes, then every 15
  minutes up to 4 hours, then hourly (`result_poll_minutes`, `result_poll_until_minutes`);
- with no published schedule: once daily (`idle_interval`);
- after source errors: 5, 15, 30, then 60 minutes (`failure_backoff_minutes`).

Manual current-season refreshes are queued and use the same scheduling calculation when completed.
Historical seasons are never scheduled automatically; they remain available through the queued
historical fetch endpoint. PostgreSQL row locking makes claims safe if more than one worker is
accidentally running.

### Season discovery and rollover

Every `season_check_interval` (default daily) the scheduler claims each enabled competition for a
season check:

1. **Discovery.** If the provider supports it (football-data.org), `discover_seasons` lists the
   provider's seasons. Only seasons newer than the newest known season are stored; existing
   seasons, including their source URLs, are never changed. Failed discovery is retried after the
   longest failure backoff.
2. **Rollover** (`season_rollover: auto`, the default). The scheduler switches `current_season` to
   the newest known season that has started by date (according to `season_format` and
   `season_start_month`), but only once the current season is finished: it has no upcoming,
   in-progress, or pending-result fixtures (`waiting_for_schedule` or `delayed`). The previous
   season stops being refreshed and stays readable. A competition without a `current_season` is
   never rolled over. If the new season has started but no source is known (for example a 90minut
   competition), the scheduler logs a hint to add it to the YAML.

Interaction with `config/competitions.yaml`, which stays authoritative:

- Discovered seasons are kept when the file is reseeded; listing the same season in the file later
  makes the file's source URL win.
- With `season_rollover: auto`, `current_season` in the file is a minimum: reseeding keeps a newer
  season the scheduler rolled over to, while a newer `current_season` in the file takes effect.
- With `season_rollover: manual`, the file's `current_season` is applied exactly (use this to pin
  or roll back a season) and the scheduler never switches seasons; discovery still adds seasons.

## Database migrations

~~~bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
uv run alembic current
~~~

Always inspect generated migrations. The application does not call create_all; schema changes must
go through Alembic.

## API

| Method | Path | Operation ID | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | `get_health` | Database connectivity and scheduler heartbeat (`ok`, `stale`, or `not_started`) |
| GET | `/api/v1/competitions` | `list_competitions` | Competitions with `season_format`, `season_start_month`, available/fetched seasons, and scheduler state; `current_season` is `null` when none is designated |
| GET | `/api/v1/matches` | `list_matches` | Finished, scheduled, and inactive matches with cursor pagination, `fields=`, and per-season freshness; filters: `competition`, `season`, `status`, `date_from`, `date_to`, `team`, `round`, `stage`, `order` |
| GET | `/api/v1/standings` | `get_standings` | League table computed from stored finished matches for one competition-season (`stage` required for cups) |
| GET | `/api/v1/teams` | `list_teams` | Teams with canonical slugs and aliases; filters: `competition`, `season`, `q`; cursor pagination |
| GET | `/api/v1/results` | `list_results` | Deprecated: finished matches with stable `id` and canonical team slugs; filters: `competition`, `season`, `round`, `stage`, `team` (kept unchanged for backward compatibility; use `/matches`) |
| POST | `/api/v1/results/refresh` | `request_refresh` | Queue a current-season refresh (`202 Accepted`; requires `X-API-Key`) |
| POST | `/api/v1/results/history/fetch` | `fetch_historical_season` | Queue a non-current-season fetch (`202 Accepted`; requires `X-API-Key`); cached results return immediately |
| GET | `/api/v1/jobs/{job_id}` | `get_job` | Get queued, running, completed, or failed scrape-job status (`X-API-Key` required) |

Interactive documentation is available at `/docs`; the OpenAPI document is at `/openapi.json`.
Agents should start at `/llms.txt` (see [Agent documentation](#agent-documentation)).

### Agent documentation

Pages for AI agents and people, generated on each request from the OpenAPI document (operation
IDs, summaries, "when to use" descriptions, parameters, auth, documented problem types) and the
competitions in the database. They are not operations, so they are absent from `/openapi.json`.

| Path | Content type | Content |
| --- | --- | --- |
| `/llms.txt` | `text/markdown; charset=utf-8` | [llms.txt v2](https://llmstxt.org/) index: purpose, base URL, auth, competitions, top 5 tasks with curl and CLI commands, rules, links |
| `/agents` | `text/html; charset=utf-8` | Agent guide for people: every operation with parameters, auth, and errors, plus conventions |
| `/agents.md` | `text/markdown; charset=utf-8` | The same guide as Markdown (the llms.txt `.md` alternate) |
| `/llms-full.txt` | `text/markdown; charset=utf-8` | The guide plus every problem type in one file (a common, non-standard convention) |
| `/problems`, `/problems/<slug>` | `text/markdown; charset=utf-8` | Meaning and fix of each problem `type`, so `type` URIs resolve |
| `/skills/football-results/SKILL.md` | `text/markdown; charset=utf-8` | The [Agent Skill](#agent-skill), served unchanged from the repository |

- The top 5 tasks are: discover competitions, resolve a team slug, a team's latest results,
  upcoming fixtures, and the league table. Examples use the first configured league.
- Links and curl examples use `FOOTBALL_RESULTS_PUBLIC_BASE_URL` (for example
  `https://football.example.com`, optionally with a path prefix). When it is unset, the base URL
  comes from the request's `Host`; `X-Forwarded-Proto` and `X-Forwarded-Host` count only from
  `FOOTBALL_RESULTS_TRUSTED_PROXIES`, and the response adds `Vary: Host, X-Forwarded-Host,
  X-Forwarded-Proto`. Set the variable in production.
- Pages count against the `read` rate limit and carry `ETag`, `Cache-Control: public,
  max-age=<FOOTBALL_RESULTS_CACHE_MAX_AGE_SECONDS>`, and `Link` headers (`rel="describedby"` to
  `/llms.txt`; `/agents` also `rel="alternate"` to `/agents.md`).
- When the database is unavailable the pages still return `200`, listing competitions from
  `config/competitions.yaml` with a note and `Cache-Control: no-store`.
- The pages link the [`football-results` CLI](#command-line-client-football-results) and the
  [Agent Skill](#agent-skill) and the [MCP server](#mcp-server) (a `Tools` section in llms.txt;
  "Command-line client", "Agent Skill", and "MCP server" sections at `/agents#cli`,
  `/agents#skill`, and `/agents#mcp`) and show each top task as curl and as a CLI command.

~~~bash
curl -s http://localhost:8000/llms.txt
~~~

### OpenAPI contract

The OpenAPI document is the source for the agent-facing surfaces (llms.txt, CLI, skill, MCP):

- Every operation has a stable snake_case `operationId` (table above), one tag, a summary, and a
  description that says when to use it. Tags group tasks: `Competitions` (competitions, teams),
  `Matches`, `Standings`, `Data refresh` (refresh, history fetch, jobs; API key), and `System`.
- Query parameters have descriptions and examples. `/api/v1/results` is marked `deprecated`.
- Error responses are documented per status as `application/problem+json` with examples, all
  using the `Problem` schema (see [Errors](#errors-rate-limits-and-caching)). Every rate-limited
  operation documents `429`; cacheable reads document `304` and the `ETag`, `Cache-Control`, and
  `RateLimit` response headers.

### Errors, rate limits, and caching

**Errors** are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details with content
type `application/problem+json` and `Cache-Control: no-store`:

~~~json
{
  "type": "/problems/unknown-season",
  "title": "Unknown season",
  "status": 404,
  "detail": "Unknown season 1999-2000 for ekstraklasa",
  "instance": "/api/v1/matches",
  "parameter": "season",
  "invalid": ["1999-2000"],
  "valid_values": ["2026-2027", "2025-2026"]
}
~~~

- `type` is a stable relative URI; branch on it rather than on `detail`. Types: `invalid-cursor`
  (400), `unauthorized` (401), `unknown-competition`, `unknown-season`, `unknown-job`,
  `not-found` (404), `method-not-allowed` (405), `job-conflict` (409), `invalid-parameter`
  (`fields`, `status`, dates, cup `stage`), `validation-error` (type, range, pattern, or missing
  values), `invalid-request` (write-endpoint rules) (all 422), `rate-limited` (429),
  `internal-error` (500), `writes-disabled` and `service-unavailable` (503).
- `detail` is always a human-readable string; `instance` is the request path.
- Optional extension members: `parameter` (what to fix), `invalid`, `valid_values`, `hint`,
  `examples`, and `errors` (`[{loc, msg, type}]` for `validation-error`).
- Compatibility (0.13.0): errors whose `detail` used to be a string keep the same text in
  `detail`. Structured details moved to top-level members: `detail.valid_values` is now
  `valid_values`, and FastAPI's `detail[]` list is now `errors`.

**Rate limits** are counted per client in fixed 60-second windows: `GET` requests use the `read`
policy (120 per minute by default), other methods the `write` policy (10 per minute). The client is
the API key when a valid `X-API-Key` is sent, otherwise the IP address. `/health`, `/docs`,
`/redoc`, and `/openapi.json` are exempt. Responses carry the
[draft-ietf-httpapi-ratelimit-headers-11](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/)
fields; a `429` adds `Retry-After`:

~~~text
RateLimit-Policy: "read";q=120;w=60
RateLimit: "read";r=87;t=42
~~~

Counters are kept in memory by each API process; with several workers or replicas each enforces
its own quota. Behind a reverse proxy, set `FOOTBALL_RESULTS_TRUSTED_PROXIES` so that the
`X-Forwarded-For` client address is used; otherwise all clients share the proxy's quota.

**Caching:** `list_competitions`, `list_matches`, `get_standings`, `list_teams`, and `list_results`
return a weak `ETag` (a hash of the body) and `Cache-Control: public, max-age=N`. `N` is the time
until the earliest `freshness.next_update_at`, capped at `FOOTBALL_RESULTS_CACHE_MAX_AGE_SECONDS`
(60), or `no-cache` when an update is due. Send the ETag back in `If-None-Match` to get
`304 Not Modified` without a body. Health, job status, and write responses use `no-store`.

~~~bash
curl -i 'http://localhost:8000/api/v1/standings?competition=ekstraklasa' -H 'If-None-Match: W/"<etag>"'
~~~

### Examples

List database-backed competitions and seasons:

~~~bash
curl 'http://localhost:8000/api/v1/competitions'
~~~

Queue a refresh of the current season:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/refresh \
  -H 'X-API-Key: your-api-key' \
  -H 'content-type: application/json' \
  -d '{"competition":"ekstraklasa"}'
~~~

The response is `202 Accepted` with a `Location` header and a `status_url`. Poll that URL, using
the same API key, until the job completes. The scheduler must be running to process queued jobs.
Each refresh reconciles the full season while retaining IDs for known matches; query
`/api/v1/results` with `round` to limit displayed results.

Queue a fetch of a configured historical season:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'X-API-Key: your-api-key' \
  -H 'content-type: application/json' \
  -d '{"competition":"ekstraklasa","season":"2025-2026"}'
~~~

Fetch and persist an unregistered historical season:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'X-API-Key: your-api-key' \
  -H 'content-type: application/json' \
  -d '{
    "competition":"ekstraklasa",
    "season":"2024-2025",
    "source_url":"http://www.90minut.pl/liga/1/liga13482.html"
  }'
~~~

New historical fetches return `202 Accepted` and a job status URL. Cached seasons return their
results immediately unless `force_refresh` is true. The competition-season row and matches are
committed together. The season then appears in GET /api/v1/competitions. The season must use the
competition's `season_format` (`2024-2025` or `2024`); football-data.org URLs must use the
competition's configured code. The supplied URL must use
a provider already configured for that competition.

Premier League historical sources use season-specific football-data.org URLs, for example:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'X-API-Key: your-api-key' \
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

### Matches, standings, and teams (API v1.1)

List endpoints share one envelope:

~~~json
{
  "data": [{"id": 412, "competition": "ekstraklasa", "season": "2026-2027", "status": "finished", "...": "..."}],
  "pagination": {"limit": 50, "next_cursor": "eyJ2IjoxLC..."},
  "freshness": [
    {
      "competition": "ekstraklasa",
      "season": "2026-2027",
      "data_as_of": "2026-08-01T20:05:12Z",
      "next_update_at": "2026-08-02T20:05:12Z",
      "automatic_refresh": true,
      "scheduler_state": "idle",
      "source": {"provider": "90minut", "url": "http://www.90minut.pl/liga/1/liga14675.html"}
    }
  ]
}
~~~

- **Match fields:** `id`, `competition`, `season`, `stage`, `round`, `status` (`finished`,
  `scheduled`, or `inactive` for postponed/suspended/cancelled), `kickoff_at`, `home_team`,
  `away_team` (source display names), `home_team_slug`, `away_team_slug`, the goal and statistics
  fields listed under [Match statistics](#match-statistics), `first_team_to_score`,
  `first_goal_minute`, and `source_url`. Every key is always present; unknown values are `null`
  (scheduled and inactive matches have null goals). `home_score`/`away_score` exist only in
  `/results`.
- **Filters:** `status` is comma-separated (default: all). `date_from` and `date_to` take an ISO
  date or datetime and are inclusive; a date covers the whole UTC day, and naive datetimes are UTC.
  `team` is a canonical slug (exactly that team) or otherwise a case-insensitive substring of any
  team name or slug, so `wisla` also finds `Wisła`. `stage` is a case-insensitive substring.
  `order` is `asc` (default) or `desc` by kickoff; matches without a kickoff sort last.
- **Pagination:** `limit` defaults to 50 (maximum 200). Pass `pagination.next_cursor` back as
  `cursor` with the same filters (the page size may change); `next_cursor` is `null` on the last
  page. Cursors are opaque; one used with other filters or order returns `400`.
- **`fields=`:** a comma-separated list of item fields for compact responses, for example
  `fields=kickoff_at,home_team_slug,away_team_slug,home_goals,away_goals`. `id` (matches) and
  `team_slug` (standings) are always included. Unknown names return `422` with `invalid` and
  `valid_values`.
- **Freshness** is reported per competition-season, never as a maximum across competitions:
  `data_as_of` is the last successful import (`null` if never fetched); `next_update_at` is the
  next scheduled check for the current season (`null` when the season is not refreshed
  automatically). `/matches` lists every season within its `competition`/`season` filter;
  `/standings` lists the one season it used.
- **Standings** (`competition` required; `season` defaults to the current one) return
  `{competition, season, stage, data, freshness}` with rows of `position`, `team_slug`,
  `team_name`, `played`, `won`, `drawn`, `lost`, `goals_for`, `goals_against`, `goal_difference`,
  `points`, and `form` (up to five latest results, newest first, e.g. `WDLWW`). Only finished
  matches count. Ties are broken by goal difference, goals for, wins, then team name. Cup
  competitions require `stage`; without it the `422` response lists the season's stages.
- **Teams** return `{slug, name, aliases}` ordered by slug, limited to teams that play in enabled
  competitions (or the given competition-season).
- Unknown competitions return `404`, as do unknown seasons of a given competition; both list the
  accepted slugs or seasons in `valid_values`.

~~~bash
curl 'http://localhost:8000/api/v1/matches?competition=ekstraklasa&status=scheduled&date_from=2026-08-01&date_to=2026-08-31'
curl 'http://localhost:8000/api/v1/matches?team=legia&status=finished&order=desc&limit=5&fields=kickoff_at,home_team,away_team,home_goals,away_goals'
curl 'http://localhost:8000/api/v1/matches?competition=premier-league&round=2&cursor=<next_cursor>'
curl 'http://localhost:8000/api/v1/standings?competition=ekstraklasa'
curl 'http://localhost:8000/api/v1/standings?competition=europa-league&stage=Faza%20ligowa&fields=points,form'
curl 'http://localhost:8000/api/v1/teams?competition=premier-league&season=2026-2027'
curl 'http://localhost:8000/api/v1/teams?q=wisla'
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

## Command-line client (`football-results`)

`football-results` is a command-line client for this API, aimed at shells, scripts, and coding
agents. It lives in `cli/` as a separate package, `football-results-cli`, and depends only on
`httpx` and `typer` (with `rich`); it does not install the server. It needs Python 3.12 or later,
and its version matches the API's.

### Install

Nothing is published to PyPI or a public Git URL yet, so install from a checkout:

~~~bash
uv tool install ./cli                     # puts football-results on PATH
pipx install ./cli                        # the same with pipx
uvx --from ./cli football-results --help  # run once without installing
uv run football-results --help            # inside the repository (dev extra)
~~~

Use `./cli`, not `.`: `uvx --from .` would install the whole API with its dependencies.

### Configuration

| Variable | Option | Default | Purpose |
| --- | --- | --- | --- |
| `FOOTBALL_RESULTS_URL` | `--url` | `http://localhost:8000` | API base URL, optionally with a path prefix |
| `FOOTBALL_RESULTS_API_KEY` | `--api-key` | none | Sent as `X-API-Key`, only by `refresh`, `fetch-season`, and `status` |
| `FOOTBALL_RESULTS_TIMEOUT` | `--timeout` | `30` | HTTP timeout in seconds |

Global options go before the command (`football-results --url URL matches`). The API key is never
printed. Redirects are not followed, so the key is only sent to the configured URL. The server
ignores these three variables; its settings use other names.

### Commands

| Command | Operation | API options |
| --- | --- | --- |
| `health` | `get_health` | none |
| `competitions` | `list_competitions` | none |
| `matches` | `list_matches` | `-c/--competition`, `-s/--season`, `--status`, `--date-from`, `--date-to`, `--team`, `--round`, `--stage`, `--order`, `--limit`, `--cursor`, `--fields` |
| `standings` | `get_standings` | `-c/--competition` (required), `-s/--season`, `--stage`, `--fields` |
| `teams` | `list_teams` | `-c/--competition`, `-s/--season`, `-q/--q`, `--limit`, `--cursor` |
| `refresh` | `request_refresh` | `-c/--competition` (required), `-s/--season` |
| `fetch-season` | `fetch_historical_season` | `-c/--competition`, `-s/--season` (both required), `--source-url`, `--force-refresh` |
| `status JOB_ID` | `get_job` | the job ID |

Options use the API parameter names (`--date-from` is `date_from`). CLI-only options:

- `--json` prints JSON even on a terminal.
- `--fields` selects item fields. The API applies it for `matches` and `standings`; the CLI applies
  it for `competitions` and `teams`.
- `--all` (`matches`, `teams`) follows `next_cursor` and prints one combined response with up to
  `--max-items` items (default 1000).
- `--fail-if-stale` (`matches`, `standings`) prints the data, then exits 4 if a competition-season
  was never fetched or its `next_update_at` is more than 15 minutes in the past.
- `--wait` (`refresh`, `fetch-season`, `status`) polls the job every `--poll-interval` seconds
  (default 5). It stops after `--wait-timeout` seconds (default 300).

Every command has `--help` with examples:

~~~bash
export FOOTBALL_RESULTS_URL=http://localhost:8000
football-results competitions
football-results teams -c ekstraklasa -q legia
football-results matches -c ekstraklasa --team legia-warszawa --status finished --order desc --limit 5
football-results standings -c ekstraklasa --json | jq '.data[:3]'
FOOTBALL_RESULTS_API_KEY=... football-results refresh -c ekstraklasa --wait
~~~

### Output

- **Piped (or with `--json`):** stdout is the API response body, unchanged, as one line of JSON. On
  a terminal `--json` is indented. List commands keep `pagination` and `freshness`.
- **On a terminal:** stdout is a table. `null` is shown as `—`, never as `0`. Freshness per
  competition-season (with a `[stale]` marker) and the next cursor go to stderr.

### Exit codes and errors

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage error: invalid option or parameter (HTTP 400, 422, or a local check) |
| 3 | Not found: unknown competition, season, or job (HTTP 404) |
| 4 | Upstream or stale: HTTP 409, 429 after retries, or 5xx; network failure or timeout; failed or unfinished job with `--wait`; stale data with `--fail-if-stale` |
| 5 | Auth: missing or invalid API key (HTTP 401/403) or writes disabled |

Errors are written to stderr as one line of RFC 9457 problem JSON. Problems from the server are
passed through unchanged. Problems detected by the CLI have `type`
`urn:football-results:cli:<slug>`, where the slug is one of `usage-error` (2), `connection-failed`,
`timeout`, `invalid-response`, `stale-data`, `job-failed`, or `wait-timeout` (all 4). These slugs
are not server problem types. Usage errors include `parameter` and `hint` when known. Read
commands retry `429` and `503` at most twice, when `Retry-After` is at most 30 seconds. Writes
are never retried.

Exit codes, command and option names, and the CLI problem slugs are public. New ones may be added,
but existing ones are never renamed or renumbered.

## Agent Skill

[`skills/football-results/SKILL.md`](skills/football-results/SKILL.md) is an
[Agent Skill](https://agentskills.io/specification) for people and coding agents that *use* the
API. It teaches the `football-results` CLI: resolving competition and team slugs, recipes for the
common questions, `null` as unknown, freshness and `--fail-if-stale`, when (not) to queue refresh
jobs, and branching on exit codes and problem types, with curl and `/llms.txt` as a fallback. The
contributor skills in `.github/skills/` are a different thing: they are playbooks for working on
this repository.

The skill is one static file; it reads the API URL from `FOOTBALL_RESULTS_URL` and works offline
once installed. The API serves it unchanged at `/skills/football-results/SKILL.md` (read rate
limit, `ETag`, `Link: rel="describedby"` to `/llms.txt`), and the Docker image includes it.
Install it with one of:

~~~bash
# GitHub Copilot CLI: from a server over HTTPS (add --project for the repository's .github/skills)
copilot skill add https://football.example.com/skills/football-results/SKILL.md
# GitHub Copilot CLI: from a checkout of this repository
copilot skill add ./skills/football-results/SKILL.md
# Any agent: copy the file (Claude Code: ~/.claude/skills/football-results/)
mkdir -p ~/.copilot/skills/football-results
curl -fsSL http://localhost:8000/skills/football-results/SKILL.md \
  -o ~/.copilot/skills/football-results/SKILL.md
export FOOTBALL_RESULTS_URL=http://localhost:8000
~~~

`copilot skill add <url>` fetches a single `SKILL.md` over HTTPS only, so use `curl` for a plain
HTTP server such as `localhost`. The directory name must be `football-results`, the skill's
`name`. GitHub Copilot reads personal skills from `~/.copilot/skills/` or `~/.agents/skills/` and
project skills from `.github/skills/`, `.agents/skills/`, or `.claude/skills/`; Claude Code reads
`~/.claude/skills/` and `.claude/skills/`. Install the CLI as well (see
[Install](#install)). `/agents#skill` shows the same commands with the server's base URL.
`tests/test_skill.py` keeps the skill in step with the CLI: every command in its code blocks must
run, and its options, exit codes, problem types, and links must exist.

## MCP server

`/mcp` is a curated [Model Context Protocol](https://modelcontextprotocol.io) server for chat
clients without a shell (ChatGPT, claude.ai, IDE chats). It speaks Streamable HTTP, protocol
revision 2026-07-28 (stateless, no session ID) and older revisions, with JSON responses. It is
built with the official `mcp` Python SDK and served by the API process; it is not an OpenAPI
operation, so it is absent from `/openapi.json`.

| Tool | API operation | Purpose |
| --- | --- | --- |
| `list_competitions` | `list_competitions` | Competition slugs, current and stored seasons, scheduler state |
| `find_matches` | `list_matches` | Fixtures and results with every `/api/v1/matches` filter, cursors, and `fields` |
| `get_standings` | `get_standings` | League table computed from finished matches |
| `get_team_form` | `list_teams` + `list_matches` | A team's last 1–20 (default 5) finished matches with W/D/L, points, and a form string |
| `request_refresh` | `request_refresh` | Queue a re-import of a stale current season (needs an API key) |

- Each tool calls the HTTP API in-process, so validation, `null` statistics, freshness, match IDs,
  and cursors are identical to the API. Structured results carry an `outputSchema`; the text
  content is the same JSON.
- Errors are tool results with `isError: true` whose text is the API's RFC 9457 problem (for
  example `unknown-competition` with `valid_values`). An ambiguous `get_team_form` team returns
  `invalid-parameter` with the candidate slugs. An unknown tool name is a JSON-RPC
  `-32602` error.
- The four read tools need no key. `request_refresh` sends the `X-API-Key` header of the MCP
  connection; configure it in the client only if you want refreshes.
- Each `/mcp` request counts once against the `read` rate limit; `request_refresh` also counts
  against the caller's `write` limit. Responses are `Cache-Control: no-store`.
- `Host` and `Origin` are checked against DNS rebinding: `localhost`, `127.0.0.1`, `[::1]`, the
  host of `FOOTBALL_RESULTS_PUBLIC_BASE_URL`, and the hosts and origins in
  `FOOTBALL_RESULTS_MCP_ALLOWED_HOSTS` and `FOOTBALL_RESULTS_MCP_ALLOWED_ORIGINS` are allowed
  (`421` for another host, `403` for another origin; requests without `Origin` pass).
- Tool names are public; tool descriptions are kept short (`tests/test_mcp.py` caps them).

~~~bash
# GitHub Copilot CLI (add --header "X-API-Key: <key>" for request_refresh)
copilot mcp add --transport http football-results https://football.example.com/mcp
# Claude Code (check `claude mcp add --help` for your version)
claude mcp add --transport http football-results https://football.example.com/mcp
~~~

Clients that read a JSON configuration (for example Copilot CLI `~/.copilot/mcp-config.json` or
a project `.mcp.json`):

~~~json
{"mcpServers": {"football-results": {"type": "http", "url": "https://football.example.com/mcp"}}}
~~~

`/agents#mcp` shows the same setup with the server's base URL.
[`server.json`](server.json) describes the server for the
[MCP Registry](https://registry.modelcontextprotocol.io) (a remote with a `{host}` variable and an
optional secret `X-API-Key` header) and is validated against the vendored schema in
`tests/schemas/`. It is not published yet: replace `OWNER` in its `name` with the GitHub account
that publishes it, then run `mcp-publisher login github` and `mcp-publisher publish`.

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
HTTP/HTTPS ports through the Ubuntu firewall. Also set `FOOTBALL_RESULTS_TRUSTED_PROXIES` to the address the
proxy connects from as seen by the API container (for a proxy on the host, the Docker network
gateway, for example `172.16.0.0/12`), and make the proxy set `X-Forwarded-For`. Otherwise every
client shares one rate-limit quota. Set `FOOTBALL_RESULTS_PUBLIC_BASE_URL` to the public HTTPS URL
so that `/llms.txt` and `/agents` show the right base URL and `/mcp` accepts its host.

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
Competitions, provider defaults, and schedule defaults are declared in `config/competitions.yaml`.
The API seeds that file at startup; the scheduler also checks it for content changes and updates the
database when it changes. Removed competitions are disabled rather than deleted, so their existing
matches remain stored. In Docker Compose the file is mounted read-only from the repository, so
editing it takes effect without rebuilding the image. In other deployments, mount the file at the
path configured by `FOOTBALL_RESULTS_CONFIG_FILE`.

Validate a config file before starting the services:

~~~bash
uv run python -m app.config check
~~~

The check can target an alternate file with `--config-file`. Invalid YAML, unsupported source URLs,
undeclared providers, duplicate slugs, inconsistent season/provider declarations, seasons that do
not match the competition's `season_format`, and mixed football-data.org competition codes fail
validation.

Per-competition season settings:

| Key | Values | Default |
| --- | --- | --- |
| `season_format` | `split` (`2026-2027`, consecutive years) or `calendar` (`2026`) | `split` |
| `season_start_month` | `2`–`12` for split seasons; omit for calendar seasons (always January) | `7` |

A calendar-year football-data.org competition, for example the Brazilian Série A:

~~~yaml
  - slug: brasileirao
    name: Campeonato Brasileiro Série A
    country: Brazil
    format: league
    provider: football-data
    season_format: calendar
    current_season: 2026
    seasons:
      2026:
        source_url: https://api.football-data.org/v4/competitions/BSA/matches?season=2026
~~~

football-data.org source URLs accept any competition code (`PL`, `BL1`, `SA`, `PD`, `CL`, ...);
`season` is the season's start year. Which codes your token can read depends on your
football-data.org plan.

#### Schedule settings

`defaults.schedule` sets the scheduler thresholds for every competition. A competition may add a
`schedule` block that overrides only the keys it lists (`match_duration_minutes` entries are merged
per format). Durations use `s`, `m`, `h`, or `d` suffixes.

| Key | Default | Meaning |
| --- | --- | --- |
| `pre_match_check_minutes` | `30` | Final check before kickoff |
| `match_duration_minutes` | `{league: 105, cup: 135}` | Expected kickoff-to-result time per competition `format`; each competition's format must be listed |
| `result_poll_minutes` | `[5, 15, 60]` | Poll intervals while a result is overdue |
| `result_poll_until_minutes` | `[30, 240]` | How late a result may be for each interval except the last (one entry fewer, increasing); afterwards the state is `delayed` |
| `idle_interval` | `1d` | Check interval without a published schedule, and the longest wait before a fixture |
| `off_season_interval` | `7d` | Check interval when the next fixture is further away than `off_season_after` |
| `off_season_after` | `30d` | See above |
| `failure_backoff_minutes` | `[5, 15, 30, 60]` | Retry delays after consecutive source errors |
| `season_rollover` | `auto` | `auto` or `manual`; see [Season discovery and rollover](#season-discovery-and-rollover) |
| `season_check_interval` | `1d` | How often seasons are discovered and rollover is evaluated |

~~~yaml
  - slug: europa-league
    format: cup
    # ...
    schedule:
      result_poll_minutes: [2, 10, 60]
      season_rollover: manual
~~~

Application variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| FOOTBALL_RESULTS_DATABASE_URL | `postgresql+psycopg_async://football:football@localhost:5432/football` | PostgreSQL connection |
| FOOTBALL_RESULTS_CONFIG_FILE | `config/competitions.yaml` locally; `/app/config/competitions.yaml` in Docker | Validated competition configuration file |
| FOOTBALL_RESULTS_REQUEST_TIMEOUT_SECONDS | `20` | HTTP timeout for source requests |
| FOOTBALL_RESULTS_USER_AGENT | `FootballResultsAPI/<package version> (contact: ...)` | Sent to sources; default version comes from package metadata; include a contact address |
| FOOTBALL_RESULTS_SCHEDULER_LOOP_SECONDS | `30` | Pause between scheduler cycles |
| FOOTBALL_RESULTS_SCHEDULER_CLAIM_LIMIT | `5` | Max queued jobs and due seasons claimed per cycle (each limit applies separately) |
| FOOTBALL_RESULTS_SCHEDULER_WORKER_TIMEOUT_SECONDS | `300` | A scheduler worker without a heartbeat for this long is treated as dead: its `running` scrape jobs are marked `failed` so their seasons are claimed again |
| FOOTBALL_RESULTS_API_KEYS | empty | Comma-separated API keys; required for write endpoints and job status |
| FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN | empty | football-data.org token (needed for football-data competitions such as the Premier League) |
| FOOTBALL_RESULTS_FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS | `6` (from YAML) | Optional environment override for the football-data.org throttle (free tier: 10 req/min) |
| FOOTBALL_RESULTS_NINETY_MINUT_MIN_REQUEST_INTERVAL_SECONDS | `2` (from YAML) | Optional environment override for the minimum delay between requests to each 90minut host; set to `0` to disable |
| FOOTBALL_RESULTS_NINETY_MINUT_DETAIL_CACHE_MAX_ENTRIES | `512` | Maximum successful 90minut detail responses kept in the process-local LRU cache; set to `0` to disable |
| FOOTBALL_RESULTS_RATE_LIMIT_ENABLED | `true` | Per-client API rate limiting; set to `false` to disable |
| FOOTBALL_RESULTS_RATE_LIMIT_READ_PER_MINUTE | `120` | `GET` requests per client per minute |
| FOOTBALL_RESULTS_RATE_LIMIT_WRITE_PER_MINUTE | `10` | `POST` (and other non-read) requests per client per minute |
| FOOTBALL_RESULTS_TRUSTED_PROXIES | empty | Comma-separated proxy IPs or CIDRs whose `X-Forwarded-For` is trusted for the client address |
| FOOTBALL_RESULTS_CACHE_MAX_AGE_SECONDS | `60` | Maximum `Cache-Control` `max-age` of read responses; `0` sends `no-cache` |
| FOOTBALL_RESULTS_PUBLIC_BASE_URL | empty | Absolute public URL (optionally with a path prefix) used in `/llms.txt` and `/agents` links and examples; empty derives it from the request. Its host and origin are allowed by `/mcp` |
| FOOTBALL_RESULTS_MCP_ALLOWED_HOSTS | empty | Extra comma-separated `Host` values accepted by `/mcp` (`host` or `host:port`; `host:*` for any port) |
| FOOTBALL_RESULTS_MCP_ALLOWED_ORIGINS | empty | Extra comma-separated `Origin` values accepted by `/mcp` (for example `https://chat.example.com`) |

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
uv run ruff format --check .
uv run alembic upgrade head --sql
~~~

Unit and API tests use a repository test double and do not require PostgreSQL. CLI tests
(`tests/test_cli.py`) run the CLI against the FastAPI app in-process or a mock transport, without
network access.

### PostgreSQL integration tests

`tests/integration/` tests the repository and migrations against a real PostgreSQL database:
seeding, scheduler claims (including `SKIP LOCKED`), atomic season replacement, failure backoff,
season discovery and rollover,
and a migration round trip followed by `alembic check`. That last check fails when
`app/db_models.py` changes without a matching migration. The tests are skipped unless
`FOOTBALL_RESULTS_TEST_DATABASE_URL` is set. They truncate and drop tables, so the database name
must contain `test`. Use the disposable Compose service; it stores data in memory only:

~~~bash
docker compose --profile test up -d --wait test-db
FOOTBALL_RESULTS_TEST_DATABASE_URL=postgresql+psycopg_async://football:football@localhost:55432/football_test \
  uv run pytest
docker compose --profile test down
~~~

Set `TEST_POSTGRES_PORT` to use a port other than 55432.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request on Python 3.12. It installs
dependencies from `uv.lock` (`--locked`), runs ruff lint and format checks, renders migrations
offline, and runs all tests against a PostgreSQL 17 service container. In CI, missing integration
database configuration is an error rather than a skip.

Before deployment, run the Compose stack and exercise at least one real refresh against
PostgreSQL.

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

Competitions, provider defaults, and schedule defaults live in `config/competitions.yaml` and are
loaded and validated with Pydantic:

~~~yaml
defaults:
  schedule:
    policy: adaptive
    pre_match_check_minutes: 30
    match_duration_minutes:
      league: 105
      cup: 135
    result_poll_minutes: [5, 15, 60]
    result_poll_until_minutes: [30, 240]
    idle_interval: 1d
    off_season_interval: 7d
    off_season_after: 30d
    failure_backoff_minutes: [5, 15, 30, 60]
    season_rollover: auto
    season_check_interval: 1d

providers:
  90minut:
    min_request_interval_seconds: 2
  football-data:
    min_request_interval_seconds: 6

competitions:
  - slug: ekstraklasa
    name: PKO BP Ekstraklasa
    country: Poland
    format: league
    provider: 90minut
    current_season: 2026-2027
    seasons:
      "2026-2027":
        source_url: http://www.90minut.pl/liga/1/liga14675.html
      "2025-2026":
        source_url: http://www.90minut.pl/liga/1/liga14072.html

  - slug: premier-league
    name: Premier League
    country: England
    format: league
    provider: football-data
    current_season: 2026-2027
    seasons:
      "2026-2027":
        source_url: https://api.football-data.org/v4/competitions/PL/matches?season=2026
~~~

Configuration behaviour:

- The file path is set by `FOOTBALL_RESULTS_CONFIG_FILE`; `config/competitions.yaml` ships in the
  image and is mounted by Docker Compose for live edits.
- Invalid configuration fails fast at service startup; `python -m app.config check` validates it
  without starting services.
- The scheduler detects file content changes and reseeds the database; competitions removed from
  the file are disabled rather than deleted.

### Pluggable data providers

- The provider interface (`scrape`, `discover_seasons`, declared capabilities) is documented in
  [docs/PROVIDERS.md](docs/PROVIDERS.md); the scheduler uses discovery for automatic rollover.
- Register providers by name (entry points or a simple registry module) so a new source is one new
  module plus configuration.
- Candidate additional sources: official league sites, other open football APIs, and a
  generic CSV/JSON URL provider for custom feeds.
- Allow a fallback provider per competition when the primary source fails repeatedly.
- Shared HTTP layer: per-host rate limiting, retries with backoff, conditional requests
  (`ETag`/`If-Modified-Since`), and a persistent response cache instead of in-memory only.

### Scheduling

The current adaptive scheduler is the foundation; planned improvements:

- ~~Make every threshold configurable per competition.~~ (done; see `schedule` above)
- Optional cron-style overrides (e.g. `cron: "0 6 * * *"`) for sources that publish on a fixed
  timetable.
- ~~Automatic season discovery and rollover.~~ (done)
- Respect provider quotas globally across all competitions and workers.
- Optional one-off backfill jobs for historical seasons, queued and processed by the scheduler
  instead of blocking an HTTP request.

### API

- ~~Fixtures endpoint for scheduled matches.~~ (done: `/api/v1/matches`)
- ~~League tables/standings computed from stored results.~~ (done: `/api/v1/standings`)
- ~~Team resources and pagination~~ (done: `/api/v1/teams`, cursors); ~~`ETag` caching headers~~ (done).
- Head-to-head tie-breakers and point deductions for standings.
- Admin endpoints (protected by an API key) to list scheduler state, view recent `scrape_run`
  entries, trigger refreshes, and enable/disable competitions.
- Keep `/api/v1` backward compatible; breaking changes go to `/api/v2`.

### Operations and observability

- `/metrics` (Prometheus): refresh counts, failures, latency, data age per competition.
- Alerts when a current season has not been refreshed successfully within its expected window.
- Structured JSON logging.
- Move remaining hard-coded competition configuration into explicit, documented configuration.

### Suggested implementation order

1. Move the registry to a validated config file (no behaviour change).
2. ~~Generalise the football-data.org adapter to any competition code.~~ (done)
3. ~~Make scheduler thresholds configurable per competition.~~ (done)
4. ~~Add fixtures and standings endpoints.~~ (done)
5. Add admin/status endpoints with API-key authentication and metrics.
6. ~~Automatic season discovery and rollover.~~ (done)
7. Additional providers and fallback sources.
