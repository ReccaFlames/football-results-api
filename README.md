# Football Results API

FastAPI service that imports football fixtures and results from public sources (90minut.pl,
football-data.org, ESPN, TheSportsDB, and OpenLigaDB), stores them in PostgreSQL, keeps current seasons up to date with an adaptive
scheduler, and serves them over a REST API.

The repository started as an Ekstraklasa-only scraper and is evolving into a **generic, configurable
league-data API**. This README describes both:

- **[Current state](#current-state)** – what is implemented and works today (version 0.26.0).
- **[Target state](#target-state-and-roadmap)** – where the project is heading and what is still
  missing.

## At a glance

| Capability | Current state | Target state |
| --- | --- | --- |
| Competitions | 5 declared in `config/competitions.yaml`: Ekstraklasa, I liga, Europa League, Premier League, 3. Liga | Any league/cup, declared in a config file or through an admin API |
| Data providers | `90minut` (HTML scraping), `football-data` (JSON API, any competition code), `espn` (public scoreboard JSON, Premier League; Europa League fallback), `thesportsdb` (documented JSON API, windowed fallback for Ekstraklasa and I liga), and `openligadb` (documented JSON API, ODbL data, 3. Liga) behind a [documented provider contract](docs/PROVIDERS.md) | More sources and fallback providers |
| Adding a competition/season | Edit `config/competitions.yaml`; the scheduler applies changes | Edit configuration (no code), hot-reloaded |
| Seasons | Split (`2026-2027`) or calendar-year (`2026`) per competition, with a configurable start month | Same |
| Season rollover | Automatic: providers with discovery (football-data, espn, openligadb) add new seasons; the scheduler switches `current_season` once the new season has started and the old one is finished. 90minut sources are added to YAML by hand | Same, with more discovering providers |
| Scheduling | Adaptive per-season scheduler driven by fixture times; thresholds from `defaults.schedule` with per-competition overrides | Plus cron-style overrides and global provider quotas |
| Configuration | Environment variables plus validated YAML for leagues, sources, providers, and schedule defaults | Env vars for infrastructure + one validated config file for leagues, sources, and schedules |
| Observability | `/health`; authenticated admin endpoints for import jobs and scheduler state; Prometheus `/metrics`; optional Prometheus + Grafana stack with Telegram alerts on stale seasons; JSON logs | Same, plus per-provider quotas and fallback alerts |
| API | Matches of every status with filters, cursor pagination, `fields=` and per-season freshness; computed standings; teams with aliases; manual refresh and historical fetch; RFC 9457 errors, rate limits, and `ETag` caching; generated `/llms.txt` and `/agents` guide; `football-results` CLI, Agent Skill, and curated MCP server at `/mcp`; authenticated admin endpoints; access requests for private instances and a landing page | Plus admin writes (enable/disable competitions) |

## Current state

### Architecture

~~~text
HTTP clients ──▶ FastAPI (app.main) ───────────────▶ PostgresResultRepository ──▶ PostgreSQL
Browsers ──▶ landing page (web/) ──HTTP──▶ FastAPI
Scheduler (app.scheduler) ──┬──▶ ScraperRegistry ──▶ sources
                            └─────────────────────▶ PostgresResultRepository
~~~

- FastAPI provides the HTTP API (`app/main.py`).
- The Whistleprint landing page (`web/`) is a separate, stateless app on the same origin that
  calls the API over HTTP only; its access request form goes to the API, which stores the
  requests ([Access requests](#access-requests)).
- Provider-specific adapters (`app/scraper.py`) parse 90minut pages, football-data.org JSON, ESPN
  scoreboard JSON, TheSportsDB JSON, and OpenLigaDB JSON.
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
scheduler_heartbeat, access_request (key requests for a private instance, ADR 0010), and
alembic_version.

### Supported competitions

| Slug | Name | Provider | Seeded seasons | Current season |
| --- | --- | --- | --- | --- |
| `ekstraklasa` | PKO BP Ekstraklasa | 90minut (fallback: thesportsdb) | 2025-2026, 2026-2027 | 2026-2027 |
| `pierwsza-liga` | Betclic I liga | 90minut (fallback: thesportsdb) | 2025-2026, 2026-2027 | 2026-2027 |
| `europa-league` | UEFA Europa League (cup) | 90minut (fallback: espn) | 2025-2026, 2026-2027 | 2026-2027 |
| `premier-league` | Premier League | espn (fallback: football-data) | 2024-2025 – 2026-2027 | 2026-2027 |
| `3-liga` | 3. Liga (Germany) | openligadb | 2025-2026, 2026-2027 | 2026-2027 |

3. Liga data comes from [OpenLigaDB](https://www.openligadb.de) and is licensed under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/). If you publish it,
keep the attribution to OpenLigaDB; a database derived from it is subject to the ODbL's
share-alike terms. `/llms.txt`, `/agents`, the competition's `data_attributions` in
`list_competitions`, and the landing page footer add this attribution whenever a configured
competition uses `openligadb` as its primary or fallback provider
([ADR 0004](docs/adr/0004-openligadb-primary-source-for-3-liga.md)).

### Known limitations

- Source URLs are validated against the currently supported 90minut, football-data.org, ESPN,
  TheSportsDB, and OpenLigaDB endpoints.
- football-data.org sources must use `/v4/competitions/<CODE>/matches?season=<start year>`, and
  all sources of one competition must share the same `<CODE>`.
- ESPN sources must use
  `https://site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard?season=<start year>`.
  ESPN publishes no matchweek, so ESPN matches have `round: null` unless a round was already stored
  for the same match by an earlier provider. The ESPN endpoint is public but undocumented and
  unofficial; it can change without notice, and its terms of use apply to how you use the data.
- TheSportsDB is a windowed source: it serves only as a fallback and fetches only the days around
  today. With the free key `123` it returns at most 3 events per day and 5 seasons, so a busy
  matchday can miss matches; see [Fallback sources](#fallback-sources).
- OpenLigaDB sources must use `https://api.openligadb.de/getmatchdata/<shortcut>/<start year>`
  (`bl3` is the 3. Liga), with one shortcut per competition. OpenLigaDB is community-edited and has
  no postponed status: an unfinished match more than `postponed_after_hours` (default 24) past
  kickoff is `inactive`, and a result entered later is picked up at the next scheduled check.
  Cards, corners, and fouls are always null.
- Season discovery is implemented for football-data.org, ESPN, and OpenLigaDB only (TheSportsDB discovery only
  fills missing fallback URLs); new 90minut seasons must be added to the YAML by hand (rollover to
  them is then automatic).
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

Competitions that use football-data.org require a free football-data.org token. The shipped
configuration has none (the Premier League uses ESPN, which needs no token), so this is only needed
when you add one. Register at https://www.football-data.org/client/register and put the generated
token in `.env`:

~~~dotenv
FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN=your-token-here
~~~

Do not commit the token. The API can start without it and the 90minut competitions continue to
work, but football-data.org refreshes will report a clear configuration error until it is configured.

Protect refresh and history-fetch requests by setting one or more comma-separated API keys:

~~~dotenv
FOOTBALL_RESULTS_API_KEYS=replace-with-a-long-random-key
~~~

Send a configured key in the `X-API-Key` header. Keep API keys out of source control and logs.
With no admin key configured, the write endpoints are disabled and return `503`.

#### Named API keys, roles, and quotas

For more than one caller, list named keys in a YAML keys file (ADR 0005) and point
`FOOTBALL_RESULTS_API_KEYS_FILE` at it. The file stores only SHA-256 hashes; plain keys are
rejected. Every key expires (ADR 0008). Manage the file with the `keys` commands:

~~~bash
uv run python -m app.config keys --file secrets/api-keys.yaml issue --name partner-app  # client, 30 days
uv run python -m app.config keys --file secrets/api-keys.yaml issue --name ops --role admin --days 90
uv run python -m app.config keys --file secrets/api-keys.yaml extend --name partner-app --days 60
uv run python -m app.config keys --file secrets/api-keys.yaml revoke --name partner-app
uv run python -m app.config keys --file secrets/api-keys.yaml list
~~~

`issue` writes the entry and prints the new `frk_...` key once on stdout (hand it to the caller
over a private channel; it is stored nowhere). `--file` defaults to
`FOOTBALL_RESULTS_API_KEYS_FILE`. `hash-key --name <name> [--role admin] [--days N]` still prints
a key and an entry to paste by hand. An entry:

~~~yaml
keys:
  - name: partner-app            # [a-z0-9-], unique; not `env` or `anonymous`
    hash: sha256:3b5c...         # 64 hex digits
    role: client                 # client (reads) or admin (jobs, admin endpoints, /metrics)
    read_per_minute: 600         # optional; default FOOTBALL_RESULTS_RATE_LIMIT_READ_PER_MINUTE
    issued_at: '2026-11-01T09:00:00Z'
    expires_at: '2026-12-01T09:00:00Z'  # required; at most 365 days after issued_at
  - name: ops
    hash: sha256:9f2a...
    role: admin
    write_per_minute: 30         # optional, admin keys only
    issued_at: '2026-11-01T09:00:00Z'
    expires_at: '2027-02-01T09:00:00Z'
  - name: alice                  # OAuth on /mcp only: a token subject instead of a hash
    subject: f5195edd-3660-457a-934a-dd3dd7d0d802
    role: admin
~~~

- A `client` key gets its own read quota; on refresh, history fetch, job status, admin endpoints,
  and `/metrics` it gets `403 forbidden`. A missing or unknown key gets `401 unauthorized`.
- Keys in `FOOTBALL_RESULTS_API_KEYS` stay valid: they are admin keys named `env` with the default
  quotas. Both sources can be combined; `503 writes-disabled` applies only when neither has an
  admin key.
- Expiry: `issue --days` defaults to 30; `extend` sets the expiry to `--days` from now but never
  more than 365 days after `issued_at` (then issue a new key, for example `partner-app-2027`).
  An expired key gets `401 unauthorized`. Keyed responses carry `API-Key-Expires` (UTC); the
  CLI warns when it is within 14 days. An entry without `issued_at`/`expires_at` stops startup;
  fix it with `keys extend --name <name>`.
- The API reloads the file within a few seconds of a change, without a restart. An invalid file
  is logged and ignored (the last valid keys stay in use). `python -m app.config check`
  validates it too (`--api-keys-file`, default the setting) without printing any hash, and
  `check` and `keys list` flag keys that expire within 14 days.
- Quotas are per minute and kept in memory (see [rate limits](#errors-rate-limits-and-caching)).
- An entry has either `hash` or `subject`, never both. A `subject` entry maps the `sub` claim of an
  OAuth access token on `/mcp` to a name, role, and quotas (see
  [OAuth for the MCP server](#oauth-for-the-mcp-server)); subjects are unique and not secret,
  and have no `issued_at`/`expires_at` (the token issuer controls expiry).
- To require a key for reads too, run a [private instance](#private-instance).

#### Access requests

People and agents ask for a key with `create_access_request` and poll `get_access_request` with
the one-time claim secret ([ADR 0010](docs/adr/0010-access-requests-are-owned-by-the-api-and-never-auto-approved.md)),
people usually through the landing page form ([Run the landing page locally](#run-the-landing-page-locally)).
Nothing is approved automatically; the operator decides on the server (guide:
[docs/PRIVATE.md](docs/PRIVATE.md#4-access-requests)):

~~~bash
uv run python -m app.config access-requests list [--status pending]   # ID, status, client, name, email, created, use
uv run python -m app.config access-requests approve 7 --file secrets/api-keys.yaml
uv run python -m app.config access-requests approve 7 --name ada-site --days 90 --read-per-minute 120
uv run python -m app.config access-requests reject 8
~~~

- `approve` issues a `client` key (365 days, default quotas, name `req-<id>-<slug of the name>`;
  `--name`, `--days`, and `--read-per-minute` override) into the keys file (`--file`, default
  `FOOTBALL_RESULTS_API_KEYS_FILE`) and stores it for the claim. It prints the key name, expiry,
  and claim deadline, never the key: the requester claims it exactly once, within 14 days.
  If the database approval fails, the new keys-file entry is removed again.
- Only pending requests can be approved or rejected; an unknown ID or a decided request exits
  with code 2. The commands read `FOOTBALL_RESULTS_DATABASE_URL` and refuse to run in demo mode.
- `list` is the operator's view and shows the contact email; nothing else prints or logs it.
- Retention: the scheduler runs an hourly purge. It erases the key and personal data of approved
  requests not claimed within 14 days (a claim erases them at once) and deletes pending and
  rejected requests 30 days after they were created; it logs only counts. An unclaimed key stays
  in the keys file until you revoke it. `/privacy` states this retention (read from
  `CLAIM_WINDOW`/`UNAPPROVED_RETENTION`), the stored fields, and, when configured, Telegram.

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

### Run the landing page locally

The Whistleprint landing page (`web/`, package `football-results-web`, Starlette + Jinja2) is a
separate, stateless app that talks to the API over HTTP only. It shows the competitions (from
`list_competitions`, cached for 5 minutes), links to the developer and agent documentation, and
an access request form (on a demo instance, "no key needed" instead; it reads this from the demo
notice in `/llms.txt`). Its footer attributes each data source once, from the competitions'
`data_attributions` (the only external links on the page), and links to `/privacy`, which names
the operator contact (`FOOTBALL_RESULTS_OPERATOR_CONTACT`). It follows the browser's light or dark
preference, has no JavaScript, and loads nothing from other origins: the pitch artwork is inline
SVG and the Inter typeface (SIL Open Font License, `static/inter-OFL.txt`) is self-hosted, subset
to Latin and Latin Extended. With the API running on port 8000:

~~~bash
uv run uvicorn football_results_web.main:app --reload --port 8001
~~~

`FOOTBALL_RESULTS_WEB_API_URL` sets the API base URL it calls (default `http://localhost:8000`).
The page is meant to be served on the same origin as the API (a reverse proxy that routes `/`,
`/request-access`, `/static/*`, and `/favicon.ico` to it and everything else to the API; see
[Ubuntu deployment](#ubuntu-deployment)), so by default its links to the API's pages (`docs`,
`llms.txt`, `agents`, `privacy`) are relative. Without such a proxy, set
`FOOTBALL_RESULTS_WEB_PUBLIC_API_URL` to the API's address as browsers reach it, and those links
point there instead:

~~~bash
FOOTBALL_RESULTS_WEB_PUBLIC_API_URL=http://localhost:8000 \
  uv run uvicorn football_results_web.main:app --reload --port 8001
~~~

`compose.yaml` sets it to `http://localhost:${API_PORT}` unless `WEB_PUBLIC_API_URL` is set in
`.env`; deployments behind the reverse proxy set `WEB_PUBLIC_API_URL=` (empty), and
`compose.demo.yaml` forces it empty.

The form posts to the landing app's `/request-access`, which calls `create_access_request` and
shows the request ID and claim secret once (`Cache-Control: no-store`). API problems (validation,
a pending request for the same email, the rate limit, an unreachable API) are shown next to the
form with the entered values kept. A hidden honeypot field drops bot submissions without calling
the API. The app appends the visitor's address to `X-Forwarded-For`, so the API's per-IP limit
counts visitors only when the landing app (and any proxy in front of it) is listed in
`FOOTBALL_RESULTS_TRUSTED_PROXIES`; otherwise all form requests share the landing app's limit.

### Run the complete stack

~~~bash
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8001/   # landing page
~~~

The API container waits for PostgreSQL, runs Alembic migrations, then starts Uvicorn. The separate
scheduler container continuously claims due current seasons. Initial competitions and sources are
seeded during application startup. The `web` service runs the landing page from its own image
(`web/Dockerfile`, which installs only `football-results-web` from `uv.lock`; the API image
installs neither `web/` nor `cli/`). It calls the API at `http://api:8000` over the Compose network
and is published on `WEB_BIND_ADDRESS:WEB_PORT` (default `127.0.0.1:8001`), like the API.

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

### Fallback sources

A competition can name a `fallback_provider` and give seasons a `fallback_url` (see
[ADR 0002](docs/adr/0002-fallback-source-refreshes.md)). The Premier League uses ESPN as its
primary source and football-data.org as its fallback; the Europa League uses 90minut.pl as its
primary and ESPN as its fallback for 2026-2027, with `team_aliases` mapping ESPN's team names to
90minut's. Ekstraklasa and I liga use 90minut.pl as their primary and TheSportsDB as their
fallback for 2026-2027 ([ADR 0003](docs/adr/0003-thesportsdb-windowed-fallback-for-polish-competitions.md)),
with `team_aliases` for the TheSportsDB names that differ from 90minut's. A **primary failure** is an error from the
primary source, or an empty result for a season that already has matches (an empty result never
deletes stored matches). After `fallback_after_failures` primary failures in a row (default 3),
each scheduled check tries the primary first and, if it fails again, refreshes from the fallback in
the same run. The primary is always tried first, so the first primary success switches back.

A fallback refresh only updates fixture fields (status, kickoff, full-time and half-time scores) and
adds missing matches. It never deletes matches and never overwrites provider match IDs, rounds, or
statistics (cards, fouls, corners, first goal) stored from the primary. It skips a match whose home
or away team, after `team_aliases`, is not yet a team of the season (for example a newly promoted
team without an alias): it logs a warning and counts the match in the import job's
`matches_skipped`, so a missing alias never creates a duplicate team. Fallbacks apply only to
scheduled refreshes of current seasons; queued refreshes and historical fetches use the primary.
`freshness.source` shows the provider and URL of the last successful import, so it names the
fallback while one is in use. Without `FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN` a football-data
fallback is unavailable: `config check` and the services log a warning, and only the primary is
used.

TheSportsDB is a **windowed source**: it fetches one documented `eventsday.php` request per day
from `window_days_back` (default 7) days before today to `window_days_ahead` (default 14) days
after, so it can only be a fallback. An empty window (for example in the winter break) is a
successful refresh with no changes, and the next check is calculated from the season's stored
matches. It works without configuration on TheSportsDB's free key `123`, which returns **at most 3
events per day** and **at most 5 seasons** for discovery. A busy matchday can therefore miss
matches; those matches stay unchanged until the primary source recovers or a later window covers
them. `config check` and the services warn about every TheSportsDB fallback that uses the free
key. A paid key in `FOOTBALL_RESULTS_THESPORTSDB_API_KEY` removes these limits; the key is never
logged or stored, and source URLs stay key-free
(`https://www.thesportsdb.com/league/<id>?season=<YYYY-YYYY>`).

Manual current-season refreshes are queued and use the same scheduling calculation when completed.
Historical seasons are never scheduled automatically; they remain available through the queued
historical fetch endpoint. PostgreSQL row locking makes claims safe if more than one worker is
accidentally running.

### Season discovery and rollover

Every `season_check_interval` (default daily) the scheduler claims each enabled competition for a
season check:

1. **Discovery.** If the provider supports it (football-data.org, ESPN, OpenLigaDB), `discover_seasons` lists the
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

If the competition has a `fallback_provider` (and, for football-data.org, a token), the season
check also runs the fallback's discovery and fills `fallback_url` for known seasons that have none.

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
| GET | `/api/v1/competitions` | `list_competitions` | Competitions with `season_format`, `season_start_month`, available/fetched seasons, and scheduler state; `current_season` is `null` when none is designated; `data_attributions` lists the sources (primary or fallback) whose licence requires attribution (`source`, `source_url`, `license`, `license_url`, `notice`), empty if none does |
| GET | `/api/v1/matches` | `list_matches` | Finished, scheduled, and inactive matches with cursor pagination, `fields=`, and per-season freshness; filters: `competition`, `season`, `status`, `date_from`, `date_to`, `team`, `round`, `stage`, `order` |
| GET | `/api/v1/standings` | `get_standings` | League table computed from stored finished matches for one competition-season (`stage` required for cups) |
| GET | `/api/v1/teams` | `list_teams` | Teams with canonical slugs and aliases; filters: `competition`, `season`, `q`; cursor pagination |
| GET | `/api/v1/results` | `list_results` | Deprecated: finished matches with stable `id` and canonical team slugs; filters: `competition`, `season`, `round`, `stage`, `team` (kept unchanged for backward compatibility; use `/matches`) |
| POST | `/api/v1/results/refresh` | `request_refresh` | Queue a current-season refresh (`202 Accepted`; requires an admin `X-API-Key`) |
| POST | `/api/v1/results/history/fetch` | `fetch_historical_season` | Queue a non-current-season fetch (`202 Accepted`; requires an admin `X-API-Key`); cached results return immediately |
| GET | `/api/v1/jobs/{job_id}` | `get_job` | Get queued, running, completed, or failed scrape-job status (admin `X-API-Key` required) |
| GET | `/api/v1/admin/scrape-runs` | `list_scrape_runs` | Import jobs, newest first, with source URL, worker, error, and `matches_skipped` (fallback matches skipped for unknown teams); filters: `competition`, `season`, `status`; cursor pagination (admin `X-API-Key` required) |
| GET | `/api/v1/admin/scheduler` | `get_scheduler_status` | Scheduler workers and heartbeats plus every scheduled season's next update, overdue time, staleness, and failure count (admin `X-API-Key` required) |
| POST | `/api/v1/access-requests` | `create_access_request` | Ask the operator for an API key ([ADR 0010](docs/adr/0010-access-requests-are-owned-by-the-api-and-never-auto-approved.md)): `name`, `contact_email`, `intended_use`, `client_type` (`human`/`agent`), optional `competitions` and `message`. `201` with a one-time `claim_secret`; one pending request per email (`409 access-request-pending`); 3 valid requests per hour per client IP; `503 writes-disabled` on the demo. No key needed, even on a private instance. The operator gets a Telegram message (ID, name, client type, intended use, competitions; never the email or message) when `FOOTBALL_RESULTS_TELEGRAM_*` is set |
| GET | `/api/v1/access-requests/{access_request_id}` | `get_access_request` | Status of an access request; needs the `Claim-Secret` header (wrong secret or ID: `404 unknown-access-request`). Once approved, the first call returns the key in `api_key` and the status becomes `claimed`. No key needed |
| GET | `/api/v1/admin/access-requests` | `list_access_requests` | Access requests, newest first, with name, contact email, intended use, competitions, and message (erased once the key is claimed or the claim expires); filter: `status`; cursor pagination (admin `X-API-Key` required) |
| POST | `/api/v1/admin/access-requests/{access_request_id}/reject` | `reject_access_request` | Reject a pending access request; returns it with status `rejected` (unknown ID: `404 unknown-access-request`; already decided: `409 access-request-decided`; admin `X-API-Key` required) |

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
| `/robots.txt` | `text/plain; charset=utf-8` | Allows every crawler and agent and points to `/llms.txt`; on a [private instance](#private-instance) it disallows everything. Not rate-limited |
| `/privacy`, `/privacy.md` | HTML / Markdown | What the instance processes (client IPs for rate limits, access logs, API key names, the landing page), why, for how long (`FOOTBALL_RESULTS_LOG_RETENTION_DAYS`), and the operator contact. Except on a demo, also access requests: stored fields, purpose, retention, and Telegram notifications (only when configured). Linked from `/llms.txt` and `/agents` |

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
- With `FOOTBALL_RESULTS_DEMO_MODE=true` (a [public demo instance](#public-demo-instance)) the pages
  add a demo notice: read-only, no SLA, the anonymous read quota from
  `FOOTBALL_RESULTS_RATE_LIMIT_READ_PER_MINUTE`, and the data licences and attribution (OpenLigaDB,
  ODbL share-alike). Without demo mode there is no notice.

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
  (400), `unauthorized` (401), `forbidden` (403, a `client` key on an admin operation),
  `unknown-competition`, `unknown-season`, `unknown-job`, `unknown-access-request`,
  `not-found` (404), `method-not-allowed` (405), `job-conflict`, `access-request-pending`, and
  `access-request-decided` (409), `invalid-parameter` (`fields`, `status`, dates, cup `stage`, access-request
  `competitions`), `validation-error` (type, range, pattern, or missing
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
the API key when a valid `X-API-Key` is sent, otherwise the IP address. A named key from the keys
file can carry its own `read_per_minute` (and, for admin keys, `write_per_minute`); `q` in
`RateLimit-Policy` shows the caller's own quota, and exceeding it returns `429 rate-limited` as
usual. Any valid key may be sent on reads; an unknown key counts against the IP address. `/health`, `/docs`,
`/redoc`, `/openapi.json`, and `/metrics` (with a valid key) are exempt. Responses carry the
[draft-ietf-httpapi-ratelimit-headers-11](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/)
fields; a `429` adds `Retry-After`:

~~~text
RateLimit-Policy: "read";q=120;w=60
RateLimit: "read";r=87;t=42
~~~

Counters are kept in memory by each API process, and a client's counter is dropped once its
window ends; with several workers or replicas each enforces its own quota. Behind a reverse proxy, set `FOOTBALL_RESULTS_TRUSTED_PROXIES` so that the
`X-Forwarded-For` client address is used; otherwise all clients share the proxy's quota.

**Caching:** `list_competitions`, `list_matches`, `get_standings`, `list_teams`, and `list_results`
return a weak `ETag` (a hash of the body) and `Cache-Control: public, max-age=N`. `N` is the time
until the earliest `freshness.next_update_at`, capped at `FOOTBALL_RESULTS_CACHE_MAX_AGE_SECONDS`
(60), or `no-cache` when an update is due. Send the ETag back in `If-None-Match` to get
`304 Not Modified` without a body. On a [private instance](#private-instance) these reads send
`Cache-Control: private, ...` instead of `public`. Health, job status, admin, `/metrics`, and write responses use
`no-store`.

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
competition's `season_format` (`2024-2025` or `2024`); football-data.org, ESPN, and OpenLigaDB URLs must use
the competition's configured code, league, or shortcut. The supplied URL must use
a provider already configured for that competition.

Premier League historical sources use season-specific ESPN URLs (`season` is the start year), for
example:

~~~bash
curl -X POST http://localhost:8000/api/v1/results/history/fetch \
  -H 'X-API-Key: your-api-key' \
  -H 'content-type: application/json' \
  -d '{
    "competition":"premier-league",
    "season":"2024-2025",
    "source_url":"https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2024",
    "force_refresh":true
  }'
~~~

The Premier League switched from football-data.org to ESPN in 0.17.0 (see
[ADR 0001](docs/adr/0001-espn-primary-source-for-premier-league.md)). The current season is
refreshed by the scheduler automatically. Seasons stored earlier from football-data.org keep their
data until you re-fetch them once with `force_refresh` as above (2024-2025 and 2025-2026). Match
IDs and rounds are kept: a refresh matches existing rows by provider ID, then by round and teams,
then by a home/away pair that is unique in the season.

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

| Fields | 90minut | football-data.org | ESPN | OpenLigaDB |
| --- | --- | --- | --- | --- |
| home_goals, away_goals | yes | yes | yes | yes |
| first-half goals | derived from match-page goal events | yes, when supplied | derived from goal events | half-time result, when supplied |
| second-half goals | derived from match-page goal events | derived from full-time minus half-time | derived from goal events | full-time minus half-time result |
| home_cards, away_cards | match-page card markers | booking-event count (paid plan only) | yellow and red card events | unavailable |
| first_team_to_score, first_goal_minute | match-page goal events | goal events (paid plan only) | goal events | goal list, when it adds up to the score |
| corners and fouls | unavailable | unavailable | `wonCorners`, `foulsCommitted` | unavailable |

A card count is yellow plus red cards; a second yellow counts as one yellow and one red (2). An own
goal counts for the team whose score it raised, including as the first goal. A goalless match has
null first-goal fields. ESPN goal-derived fields stay null when the goal events do not add up to
the final score; cards are then null too, because missing events cannot be told apart from none.
OpenLigaDB first-goal fields stay null when its goal list does not add up to the full-time score;
an own goal's team comes from the running score it raised.

football-data.org's free plan omits goal and booking events, so these fields stay null for
football-data competitions unless your token has a paid plan with them.

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

The CLI is prepared for PyPI as `football-results-cli` (published by the
[release workflow](#releases)):

~~~bash
uv tool install football-results-cli                     # puts football-results on PATH
pipx install football-results-cli                        # the same with pipx
uvx --from football-results-cli football-results --help  # run once without installing
~~~

Until the first release, install from a checkout:

~~~bash
uv tool install ./cli                     # or: pipx install ./cli
uvx --from ./cli football-results --help  # run once without installing
uv run football-results --help            # inside the repository (dev extra)
~~~

Use `./cli`, not `.`: `uvx --from .` would install the whole API with its dependencies.

### Configuration

| Variable | Option | Default | Purpose |
| --- | --- | --- | --- |
| `FOOTBALL_RESULTS_URL` | `--url` | the public demo once its host is set, until then `http://localhost:8000` | API base URL, optionally with a path prefix |
| `FOOTBALL_RESULTS_API_KEY` | `--api-key` | none | Sent as `X-API-Key` on every request (it selects the key's quota; a private instance needs it for every command except `health`, `competitions`, `create-access-request`, and `get-access-request`); `refresh`, `fetch-season`, `status`, `scrape-runs`, `scheduler`, `list-access-requests`, and `reject-access-request` need an admin key. The CLI warns on stderr when the API's `API-Key-Expires` header is within 14 days |
| `FOOTBALL_RESULTS_TIMEOUT` | `--timeout` | `30` | HTTP timeout in seconds |
| `FOOTBALL_RESULTS_CLAIM_SECRET` | `--claim-secret` (`get-access-request` only) | none | Claim secret from `create-access-request`, sent as the `Claim-Secret` header; prefer the variable to keep it out of shell history |

Global options go before the command (`football-results --url URL matches`). The default URL
follows [ADR 0009](docs/adr/0009-published-cli-defaults-to-the-demo-instance.md): a released CLI
talks to the public demo out of the box (`--help` shows the default). An API key is never sent to
that default: with a key, set `FOOTBALL_RESULTS_URL` or `--url` (otherwise a `usage-error`, exit
2). When the CLI uses the local default and cannot connect, the error explains how to set the URL.
The API key is never
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
| `scrape-runs` | `list_scrape_runs` | `-c/--competition`, `-s/--season`, `--status`, `--limit`, `--cursor` |
| `scheduler` | `get_scheduler_status` | none |
| `create-access-request` | `create_access_request` | `--name`, `--contact-email`, `--intended-use`, `--client-type` (all required), `--competitions` (comma-separated), `--message` |
| `get-access-request ACCESS_REQUEST_ID` | `get_access_request` | the request ID, `--claim-secret` (required) |
| `list-access-requests` | `list_access_requests` | `--status`, `--limit`, `--cursor` |
| `reject-access-request ACCESS_REQUEST_ID` | `reject_access_request` | the request ID |

Options use the API parameter names (`--date-from` is `date_from`). CLI-only options:

- `--json` prints JSON even on a terminal.
- `--fields` selects item fields. The API applies it for `matches` and `standings`; the CLI applies
  it for `competitions` and `teams`.
- `--all` (`matches`, `teams`, `scrape-runs`, `list-access-requests`) follows `next_cursor` and prints one combined response with up to
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
FOOTBALL_RESULTS_API_KEY=... football-results scrape-runs --status failed --limit 10
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
jobs, and branching on exit codes and problem types (including where to request a key), with curl
and `/llms.txt` as a fallback. The contributor skills in `.github/skills/` are a different thing:
they are playbooks for working on this repository.

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
  connection and needs an admin key (a `client` key gets `forbidden` as an `isError` result);
  configure the header only if you want refreshes or your key's own quota.
- Each `/mcp` request counts once against the `read` rate limit of the connection's key (or of the
  client IP without one); `request_refresh` also counts against the caller's `write` limit. Responses are `Cache-Control: no-store`.
- `Host` and `Origin` are checked against DNS rebinding: `localhost`, `127.0.0.1`, `[::1]`, the
  host of `FOOTBALL_RESULTS_PUBLIC_BASE_URL`, and the hosts and origins in
  `FOOTBALL_RESULTS_MCP_ALLOWED_HOSTS` and `FOOTBALL_RESULTS_MCP_ALLOWED_ORIGINS` are allowed
  (`421` for another host, `403` for another origin; requests without `Origin` pass).
- Tool names are public; tool descriptions are kept short (`tests/test_mcp.py` caps them).
- With OAuth configured, `/mcp` also accepts `Authorization: Bearer` access tokens (see below).

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
[MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.ReccaFlames/football-results`
with two remotes: the public demo first (`FOOTBALL_DEMO_HOST` until
`scripts/set_demo_host.py` sets it), then a `{host}` template for your own deployment with an
optional secret `X-API-Key` header. It is validated against the vendored schema in
`tests/schemas/` and published by the [release workflow](#releases), not by hand.

### OAuth for the MCP server

OAuth on `/mcp` is optional and off by default (ADR 0006). The API is only an OAuth *resource
server*: it validates JWT access tokens from an authorization server you run (Keycloak, Entra ID,
Auth0, ...) and never issues tokens. The REST API keeps `X-API-Key`.

- Set `FOOTBALL_RESULTS_OAUTH_ISSUER` (requires `FOOTBALL_RESULTS_PUBLIC_BASE_URL`). The API then
  serves RFC 9728 metadata at `/.well-known/oauth-protected-resource/mcp` (cacheable, not rate
  limited, not in `/openapi.json`), which MCP clients use to find the issuer.
- Tokens must be signed with an asymmetric algorithm (RS*, PS*, ES*, EdDSA) by a key from the
  issuer's JWKS (from `FOOTBALL_RESULTS_OAUTH_JWKS_URL`, else OIDC or RFC 8414 discovery; cached
  for an hour and refetched for an unknown `kid`), and carry `iss` = the issuer, `aud` = the
  public `/mcp` URL (or `FOOTBALL_RESULTS_OAUTH_AUDIENCE`), `exp`, and `sub`; `nbf` is checked
  when present (30 s leeway).
- An invalid or expired token gets `401 unauthorized` with
  `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="..."`, even in optional
  mode. An unreachable issuer gives `503 service-unavailable`.
- `FOOTBALL_RESULTS_OAUTH_MODE=optional` (default): anonymous and `X-API-Key` requests work as
  before. `required`: a request with neither a valid token nor a valid key gets `401` with
  `resource_metadata`, which starts sign-in in the client.
- The token's `sub` gets the name, role, and quotas of a keys-file `subject` entry; an unknown
  subject is a `client` named `oauth` (default keyed quotas, metric label `oauth`). An admin
  subject may use `request_refresh`. A valid `X-API-Key` on the same request wins over the token.
  Scopes (`FOOTBALL_RESULTS_OAUTH_SCOPES`) are advertised to clients, not enforced.
- Client support (checked November 2026, see `docs/research/2026-11-hosted-access.md` §7):
  GitHub Copilot CLI, VS Code, and Claude Code discover the metadata, register by client ID
  metadata document, dynamic registration, or a pre-registered client ID, and send `resource`.
  In optional mode a client may stay signed out because anonymous access works; start sign-in
  yourself (`/mcp auth` in Copilot CLI, `claude mcp login football-results`) or use `required`.

#### Local walkthrough with Keycloak

The `oauth-example` Compose profile runs Keycloak 26 in development mode on
`http://localhost:8080` and imports `keycloak/football-realm.json`: realm `football`, a public
client `football-mcp` (PKCE, loopback redirects), and a `mcp:tools` scope whose audience mapper
adds `http://localhost:8000/mcp` to access tokens. The realm ships no users and no secrets. It is
an example, not a production setup (plain HTTP, `start-dev`).

1. In `.env`, set a Keycloak admin password and the OAuth settings (the issuer is the
   browser-visible URL; the API container fetches keys through the Compose network):

   ~~~bash
   KEYCLOAK_ADMIN_PASSWORD=<choose one>
   FOOTBALL_RESULTS_PUBLIC_BASE_URL=http://localhost:8000
   FOOTBALL_RESULTS_OAUTH_ISSUER=http://localhost:8080/realms/football
   FOOTBALL_RESULTS_OAUTH_JWKS_URL=http://keycloak:8080/realms/football/protocol/openid-connect/certs
   FOOTBALL_RESULTS_OAUTH_SCOPES=mcp:tools
   FOOTBALL_RESULTS_OAUTH_MODE=required   # optional: also allow anonymous access
   ~~~

2. Start Keycloak and the stack: `docker compose --profile oauth-example up -d`.
3. Create a user: sign in to `http://localhost:8080/admin` as `admin`, open realm `football`,
   **Users → Create user**, then set a password under **Credentials**.
4. Check the metadata: `curl -s http://localhost:8000/.well-known/oauth-protected-resource/mcp`.
5. Add the server to a client with the pre-registered client ID and sign in in the browser:

   ~~~bash
   claude mcp add --transport http --client-id football-mcp football-results http://localhost:8000/mcp
   claude mcp login football-results
   ~~~

   In VS Code, add `"oauth": {"clientId": "football-mcp"}` to the server in `mcp.json`; in Copilot
   CLI, set `oauthClientId` to `football-mcp` in `~/.copilot/mcp-config.json` and run `/mcp auth`.
6. To let that user use `request_refresh`, copy the user's ID (Keycloak **Users → Details**, the
   token's `sub`) into a keys-file `subject` entry with `role: admin` and restart the API.

For another issuer or public URL, change the audience mapper's `included.custom.audience` to your
public `/mcp` URL (or set `FOOTBALL_RESULTS_OAUTH_AUDIENCE` to the audience your issuer puts in
tokens). Keycloak ignores the `resource` parameter unless its experimental `resource-indicators`
feature is enabled, which is why the realm uses a scope mapper.

## Ubuntu deployment

The Compose deployment runs PostgreSQL, the API, the scheduler, and the landing page on the same
Ubuntu machine ([services and routes](docs/DEPLOYMENT.md#services-and-routes)).
Before the first deployment, work through the checklist in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

1. Install Docker Engine and the Docker Compose plugin.
2. Copy the project to the server.
3. Create production configuration:

~~~bash
cp .env.example .env
chmod 600 .env
~~~

4. Set a long random POSTGRES_PASSWORD (`openssl rand -hex 32`, before the first start), a
   contact USER_AGENT, and API_BIND_ADDRESS (default `127.0.0.1`).
   For named API keys, create `secrets/api-keys.yaml` (git-ignored; mounted read-only at
   `/app/secrets`; the `keys` commands create it with mode `0644`, readable by the container
   user, UID 10001, since it holds only hashes), and set
   `FOOTBALL_RESULTS_API_KEYS_FILE=/app/secrets/api-keys.yaml`.
5. Start the services:

~~~bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 api scheduler web
~~~

All containers use restart: unless-stopped. PostgreSQL data is stored in the postgres_data named
volume. Port 5432 remains bound to localhost.

API_BIND_ADDRESS and WEB_BIND_ADDRESS (the landing page) default to 127.0.0.1: Docker-published
ports bypass the Ubuntu firewall, so set
`0.0.0.0` only on a trusted network. Put Caddy or nginx in front, and expose only the reverse proxy's
HTTP/HTTPS ports through the Ubuntu firewall. The proxy serves both apps on one origin: `/`,
`/request-access`, `/static/*`, and `/favicon.ico` go to the landing page (`127.0.0.1:8001`),
everything else to the API (`127.0.0.1:8000`), which has no `/` route; the Caddyfiles are in
[docs/DEMO.md](docs/DEMO.md#3-caddy) and [docs/PRIVATE.md](docs/PRIVATE.md#2-https-with-caddy).
Also set `FOOTBALL_RESULTS_TRUSTED_PROXIES` to the addresses the API container sees the proxy and
the landing app connect from (for a proxy on the host, the Docker network gateway; the landing
container is on the same network: `172.16.0.0/12` covers both), and make the proxy set
`X-Forwarded-For`. Otherwise every client shares one rate-limit quota, and all landing-page
form submissions share one access-request limit
([details](docs/DEPLOYMENT.md#client-addresses-and-trusted-proxies)). Set `FOOTBALL_RESULTS_PUBLIC_BASE_URL` to the public HTTPS URL
so that `/llms.txt` and `/agents` show the right base URL and `/mcp` accepts its host.

### Private instance

A private instance serves only invited people
([ADR 0008](docs/adr/0008-private-instances-require-expiring-named-keys.md)); the guide is
[docs/PRIVATE.md](docs/PRIVATE.md). Set `FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false`, and then:

- Every API operation except `get_health`, `list_competitions`, `create_access_request`, and
  `get_access_request` needs a valid, unexpired key (`401 unauthorized` with
  `WWW-Authenticate: ApiKey`), and so does `/mcp`. People and agents ask for a key with
  `create_access_request` ([ADR 0010](docs/adr/0010-access-requests-are-owned-by-the-api-and-never-auto-approved.md)); the operator approves or rejects with
  [`python -m app.config access-requests`](#access-requests). Agent docs, `/problems/*`, the skill,
  `/openapi.json`, `/docs`, and OAuth metadata stay public, and the agent docs say a key is needed.
- Every `401` counts against the IP read quota. Cacheable reads send `Cache-Control: private`.
- The API and `config check` refuse to start with `FOOTBALL_RESULTS_API_KEYS`, without
  `FOOTBALL_RESULTS_API_KEYS_FILE`, without an `https` `FOOTBALL_RESULTS_PUBLIC_BASE_URL`, with
  a database password that is a shipped default or shorter than 16 characters (both checks skip a
  localhost base URL), or in demo mode.

### Public demo instance

A demo instance is a public, read-only deployment that serves only providers whose data may be
redistributed publicly ([ADR 0007](docs/adr/0007-read-only-demo-instance-serves-only-redistributable-sources.md)).
[docs/DEMO.md](docs/DEMO.md) is the deployment guide: an Oracle Cloud Always Free VM, Caddy for
TLS, and Cloudflare's free plan for caching and an IP rate limit.

~~~bash
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
~~~

`compose.demo.yaml` sets `FOOTBALL_RESULTS_DEMO_MODE=true`, serves
`config/demo-competitions.yaml` (the 3. Liga from OpenLigaDB), allows 30 anonymous reads per
minute (`DEMO_READ_PER_MINUTE`), writes JSON logs, and empties `FOOTBALL_RESULTS_API_KEYS`. In
demo mode:

- The API, the scheduler, and `python -m app.config check` refuse admin keys
  (`FOOTBALL_RESULTS_API_KEYS`, or `admin` entries in the keys file; `client` keys are allowed)
  and any competition whose provider or fallback provider is not publicly redistributable. The
  allow list is code (`REDISTRIBUTABLE_PROVIDERS` in `app/config.py`, today only `openligadb`);
  adding a provider needs written permission and a code change (see the guide).
- Without an admin key, writes, jobs, admin operations, and `/metrics` return `503`
  `writes-disabled`. `/mcp` stays anonymous; the observability profile cannot scrape `/metrics`.
- `/llms.txt` and `/agents` show the demo notice (see [Agent documentation](#agent-documentation)).
- `FOOTBALL_RESULTS_OPERATOR_CONTACT` is required (shown on `/privacy`), client IPs in access logs
  are shortened (`/24` for IPv4, `/48` for IPv6), and container logs go to journald, which deletes
  them after `FOOTBALL_RESULTS_LOG_RETENTION_DAYS` (set its `MaxRetentionSec` to match; see the
  guide). `/robots.txt` lets crawlers and AI agents in; the guide's Cloudflare settings keep it so.
- The origin is hardened: `compose.demo.yaml` publishes the API and the landing page on
  `127.0.0.1` only, whatever `API_BIND_ADDRESS` and `WEB_BIND_ADDRESS` say, and has no default
  `POSTGRES_PASSWORD`; demo mode also refuses a
  database password that is a shipped default or shorter than 16 characters (use
  `openssl rand -hex 32`: the password is part of the database URL).

~~~bash
FOOTBALL_RESULTS_DEMO_MODE=true FOOTBALL_RESULTS_OPERATOR_CONTACT=you@example.com \
FOOTBALL_RESULTS_DATABASE_URL=postgresql+psycopg_async://football:$(openssl rand -hex 16)@localhost/football \
  uv run python -m app.config check --config-file config/demo-competitions.yaml
~~~

### Observability

Everything here is self-hosted and free: Prometheus (Apache-2.0) and Grafana OSS (AGPL-3.0) run in
the optional `observability` Compose profile, and alerts go to a Telegram bot. Together they use
about 200 MB of RAM. Terms such as *stale season* and *stalled scheduler* are defined in
[GLOSSARY.md](GLOSSARY.md).

**Signals**

- `GET /metrics` (Prometheus text format, not in OpenAPI, not rate-limited with a valid key) needs an admin key
  (from `FOOTBALL_RESULTS_API_KEYS` or the keys file), sent as `Authorization: Bearer <key>` or
  `X-API-Key`; a `client` key gets `403 forbidden`. Without an admin key it returns
  `503 writes-disabled`. Scheduler and season values are read from
  PostgreSQL at scrape time, so they are correct even when the scheduler is down:
  - `football_results_scheduler_stalled`, `football_results_scheduler_heartbeat_age_seconds`
  - per `{competition, season}` of every current, automatically refreshed season:
    `football_results_season_stale`, `_season_overdue_seconds`, `_season_consecutive_failures`,
    `_season_data_age_seconds`, `_season_next_update_timestamp_seconds`,
    `_season_primary_consecutive_failures`, and `_season_fallback_active` (1 while the last
    successful import came from the fallback source)
  - `football_results_scrape_runs{competition, status}` (stored import jobs)
  - `football_results_http_requests_total{operation, method, status}` and
    `football_results_http_request_duration_seconds{operation}` (labelled by operation ID, so paths
    with IDs do not create new series), `football_results_build_info{version}`, and `process_*`
  - `football_results_keyed_requests_total{key, policy}` and
    `football_results_rate_limited_total{key, policy}`: rate-limited requests and `429`s per key
    name (`env` for `FOOTBALL_RESULTS_API_KEYS`, `anonymous` without a valid key) and policy
  - `football_results_api_key_expires_in_seconds{key}`: time until each keys-file key expires
    (negative once expired)
  - `football_results_access_requests_total{outcome}`: created access requests by operator
    notification outcome (`notified`, `not_configured`, `failed`; a failed Telegram send never
    fails the request)
- Admin endpoints (admin `X-API-Key`): `list_scrape_runs`, `get_scheduler_status`,
  `list_access_requests`, and `reject_access_request`, also available as `football-results
  scrape-runs`, `scheduler`, `list-access-requests`, and `reject-access-request`. They are not MCP
  tools. Import
  jobs include their `provider`, a `fallback` flag, and `matches_skipped` (matches a fallback
  refresh skipped because a team is not yet a team of the season; the `scrape-runs` table shows
  it); a fallback refresh records the failed primary run and the fallback run separately. Scheduler seasons include
  `primary_consecutive_failures` and `fallback_active`.
- `FOOTBALL_RESULTS_LOG_FORMAT=json` (the Compose default) writes one JSON object per line. Compose
  rotates container logs (`json-file`, 3 × 10 MB per container); read them with
  `docker compose logs api scheduler`.

A season is **stale** when it was never fetched or its scheduled update is more than 15 minutes
overdue (the same rule as the CLI's `--fail-if-stale`). The scheduler is **stalled** when no
heartbeat is newer than four scheduler loops (at least two minutes). `/health` keeps reporting a
stalled scheduler as `stale` for backward compatibility; the admin endpoint and metrics say
`stalled`.

**Set up monitoring**

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and note its token. Send the bot
   a message, then read your chat ID from
   `https://api.telegram.org/bot<token>/getUpdates` (`message.chat.id`).
2. In `.env`, add a dedicated admin key to `FOOTBALL_RESULTS_API_KEYS` (or the keys file; on a
   [private instance](#private-instance) only the keys file, so rotate it yearly) and set
   the same value as `PROMETHEUS_API_KEY`. Set `GRAFANA_ADMIN_PASSWORD`, `TELEGRAM_BOT_TOKEN`, and
   `TELEGRAM_CHAT_ID`.
3. Start the stack:

~~~bash
docker compose --profile observability up -d
~~~

4. Open Grafana through an SSH tunnel (it listens on 127.0.0.1 only; Prometheus is not published):

~~~bash
ssh -L 3000:localhost:3000 user@server   # then browse http://localhost:3000
~~~

Grafana is provisioned from `observability/grafana/` (the Prometheus data source, the *Football
Results API* dashboard, and these alert rules, evaluated every minute) and from the Telegram
contact point that `compose.yaml` renders from `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`:

| Alert | Fires when |
| --- | --- |
| Stale season | `football_results_season_stale` is 1 for a season (for 1 minute) |
| Stalled scheduler | `football_results_scheduler_stalled` is 1 for 2 minutes |
| Failing season | `football_results_season_consecutive_failures` is 3 or more (retries back off, so the season is not yet overdue) |
| Season on fallback source | `football_results_season_fallback_active` is 1 for 24 hours (warning; the primary source keeps failing) |
| API down | Prometheus cannot scrape `/metrics` for 2 minutes (API down or the Prometheus key rejected) |
| API key expiring | `football_results_api_key_expires_in_seconds` is under 7 days for a key (keeps firing until the key is extended or revoked) |

Without Telegram settings the stack still starts and alerts show in Grafana, but notifications fail.
Prometheus keeps 15 days of data (`PROMETHEUS_RETENTION`). Provisioned rules and the dashboard are
read-only in Grafana; change the files and restart Grafana.

### Backups

~~~bash
chmod +x scripts/backup-postgres.sh
./scripts/backup-postgres.sh
~~~

Backups go to ./backups (mode `0600`). The script checks each dump with `pg_restore --list`; a
failed or unreadable dump is deleted and the script exits non-zero, so a failure never leaves an
empty file. Only after a good dump are files older than `BACKUP_RETENTION_DAYS` (default 7)
removed. Example cron entry:

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
not match the competition's `season_format`, mixed football-data.org competition codes, ESPN leagues,
or OpenLigaDB shortcuts, provider-only settings on another provider (`window_days_*` outside
`thesportsdb`, `postponed_after_hours` outside `openligadb`), and invalid `team_aliases` fail
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

A competition may map provider team names to the names already stored, so switching providers
does not create duplicate teams. Keys are the provider's names; values are the stored names:

~~~yaml
  - slug: premier-league
    provider: espn
    team_aliases:
      Fulham: Fulham FC
      Crystal Palace: Crystal Palace FC
~~~

ESPN sources use `https://site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard?season=<start year>`
(`eng.1` is the Premier League). One season is fetched with one request per calendar year it spans.

A fallback source is declared with `fallback_provider` on the competition and `fallback_url` per
season. The fallback provider must be declared, differ from `provider`, and every `fallback_url`
must be a valid source of that provider (with one competition code):

~~~yaml
  - slug: premier-league
    provider: espn
    fallback_provider: football-data
    seasons:
      2026-2027:
        source_url: https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2026
        fallback_url: https://api.football-data.org/v4/competitions/PL/matches?season=2026
~~~

TheSportsDB fallback URLs use the key-free form
`https://www.thesportsdb.com/league/<league id>?season=<YYYY-YYYY>` (4422 is Ekstraklasa, 4661 is
I liga). `thesportsdb` is windowed, so `config check` rejects it as a `provider`.

OpenLigaDB sources use `https://api.openligadb.de/getmatchdata/<shortcut>/<start year>` (`bl3` is
the 3. Liga); one request returns the whole season and needs no key. Its provider block accepts
`postponed_after_hours` (default `24`): an unfinished match that long past kickoff is `inactive`,
because OpenLigaDB has no postponed status.

~~~yaml
providers:
  openligadb:
    min_request_interval_seconds: 2
    postponed_after_hours: 24
~~~

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
| `fallback_after_failures` | `3` | Primary failures in a row before scheduled checks use the fallback source; see [Fallback sources](#fallback-sources) |

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
| FOOTBALL_RESULTS_USER_AGENT | `FootballResultsAPI/<package version> (contact: ...)` | Sent to sources; default version comes from package metadata; include a contact address (ESPN rejects emails in the User-Agent, so for ESPN the address is sent in the `From` header) |
| FOOTBALL_RESULTS_SCHEDULER_LOOP_SECONDS | `30` | Pause between scheduler cycles |
| FOOTBALL_RESULTS_SCHEDULER_CLAIM_LIMIT | `5` | Max queued jobs and due seasons claimed per cycle (each limit applies separately) |
| FOOTBALL_RESULTS_SCHEDULER_WORKER_TIMEOUT_SECONDS | `300` | A scheduler worker without a heartbeat for this long is treated as dead: its `running` scrape jobs are marked `failed` so their seasons are claimed again |
| FOOTBALL_RESULTS_API_KEYS | empty | Comma-separated admin API keys (named `env`); an admin key is required for write endpoints, job status, admin endpoints, and `/metrics` |
| FOOTBALL_RESULTS_API_KEYS_FILE | empty | YAML file of named, hashed, expiring keys with roles and per-minute quotas; reloaded on change (see [Named API keys](#named-api-keys-roles-and-quotas)) |
| FOOTBALL_RESULTS_ANONYMOUS_ACCESS | `true` | `false` makes a [private instance](#private-instance): every API operation except `get_health`, `list_competitions`, and the access-request operations, and `/mcp`, need a key from the keys file |
| FOOTBALL_RESULTS_DEMO_MODE | `false` | Public demo instance: startup and `config check` reject admin keys and providers that are not publicly redistributable; agent docs show a demo notice (see [Public demo instance](#public-demo-instance)) |
| FOOTBALL_RESULTS_LOG_FORMAT | `text` | `text` or `json` (one JSON object per line on stderr, including uvicorn access logs); Docker Compose sets `json` |
| FOOTBALL_RESULTS_OPERATOR_CONTACT | empty | Who runs the instance (an email or URL), shown on `/privacy`; required in demo mode |
| FOOTBALL_RESULTS_LOG_RETENTION_DAYS | `14` | How long logs are kept, as stated on `/privacy`; keep the log driver's retention equal |
| FOOTBALL_RESULTS_ANONYMIZE_LOG_IPS | `false` | Shorten client IPs in access logs (`/24` IPv4, `/48` IPv6); always on in demo mode |
| FOOTBALL_RESULTS_TELEGRAM_BOT_TOKEN | empty | Telegram bot token for a message to the operator on each new access request (ID, name, client type, intended use, competitions; never the contact email or message). Set both or neither (`config check` fails otherwise); without them a new request is only logged (ID and client type) and counted. Never logged. Docker Compose falls back to `TELEGRAM_BOT_TOKEN` |
| FOOTBALL_RESULTS_TELEGRAM_CHAT_ID | empty | Chat for those messages: a numeric chat ID or an `@channel` name. Docker Compose falls back to `TELEGRAM_CHAT_ID` (the Grafana alert chat); set it when others can read that chat |
| FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN | empty | football-data.org token (needed only for football-data competitions) |
| FOOTBALL_RESULTS_FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS | `6` (from YAML) | Optional environment override for the football-data.org throttle (free tier: 10 req/min) |
| FOOTBALL_RESULTS_ESPN_MIN_REQUEST_INTERVAL_SECONDS | `2` (from YAML) | Optional environment override for the minimum delay between ESPN requests |
| FOOTBALL_RESULTS_THESPORTSDB_API_KEY | `123` (free key) | TheSportsDB API key. The free key returns at most 3 events per day and 5 seasons; a paid key removes these limits. Never logged or stored |
| FOOTBALL_RESULTS_THESPORTSDB_MIN_REQUEST_INTERVAL_SECONDS | `3` (from YAML) | Optional environment override for the minimum delay between TheSportsDB requests (free plan: 30 req/min) |
| FOOTBALL_RESULTS_OPENLIGADB_MIN_REQUEST_INTERVAL_SECONDS | `2` (from YAML) | Optional environment override for the minimum delay between OpenLigaDB requests |
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
| FOOTBALL_RESULTS_OAUTH_ISSUER | empty | OAuth authorization server whose access tokens `/mcp` accepts; empty turns OAuth off. Requires `FOOTBALL_RESULTS_PUBLIC_BASE_URL` (see [OAuth for the MCP server](#oauth-for-the-mcp-server)) |
| FOOTBALL_RESULTS_OAUTH_MODE | `optional` | `optional` keeps anonymous and `X-API-Key` access on `/mcp`; `required` answers other requests with `401` and `resource_metadata` |
| FOOTBALL_RESULTS_OAUTH_AUDIENCE | public `/mcp` URL | Required `aud` of access tokens, when the issuer cannot put the `/mcp` URL there |
| FOOTBALL_RESULTS_OAUTH_SCOPES | empty | Comma-separated scopes advertised in the metadata and the `401` challenge (not enforced) |
| FOOTBALL_RESULTS_OAUTH_JWKS_URL | discovered | JWKS URL when the issuer URL is not reachable from the API (for example `http://keycloak:8080/...` in Compose) |

The landing app reads only `FOOTBALL_RESULTS_WEB_API_URL` (see
[Run the landing page locally](#run-the-landing-page-locally)); the CLI's variables are listed
under [Command-line client](#command-line-client-football-results).

Deployment variables (Docker Compose):

- POSTGRES_DB
- POSTGRES_USER
- POSTGRES_PASSWORD
- POSTGRES_PORT
- API_BIND_ADDRESS (`127.0.0.1`)
- API_PORT
- WEB_BIND_ADDRESS (`127.0.0.1`), WEB_PORT (`8001`): the landing page (`web` service)
- Observability profile: PROMETHEUS_API_KEY, PROMETHEUS_RETENTION (`15d`), GRAFANA_ADMIN_USER,
  GRAFANA_ADMIN_PASSWORD, GRAFANA_BIND_ADDRESS (`127.0.0.1`), GRAFANA_PORT (`3000`),
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (see [Observability](#observability); also the default
  for the API's access-request notifications)
- OAuth example profile: KEYCLOAK_ADMIN_USER (`admin`), KEYCLOAK_ADMIN_PASSWORD, KEYCLOAK_PORT
  (`8080`; bound to `127.0.0.1`) (see [OAuth for the MCP server](#oauth-for-the-mcp-server))

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
network access; the landing page tests (`tests/test_web.py`) do the same with a fake API.

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

### Agent evals

[`evals/`](evals/README.md) asks `copilot -p` 11 questions (top tasks, error recovery, freshness,
pagination) through the CLI + Agent Skill and through the MCP server, against a local demo-mode
stack, with ground truth read from the API at run time. A task passes when 2 of 3 runs answer
correctly with the tools under test. The evals cost AI credits, so they are not part of pytest
or CI; run them before a release. [`evals/CHAT_CLIENTS.md`](evals/CHAT_CLIENTS.md) is the manual
checklist for claude.ai and ChatGPT.

### Releases

Nothing is published yet. `.github/workflows/release.yml` is dormant until a `v*` tag is pushed
to `github.com/ReccaFlames/football-results-api`: it reruns CI, checks that the tag equals the
versions in `pyproject.toml`, `cli/pyproject.toml`, and `server.json`, fails while a demo-host
placeholder remains (`scripts/set_demo_host.py --check`), builds `football-results-cli`,
publishes it to PyPI with trusted publishing (environment `pypi`, no token), and publishes
`server.json` to the MCP Registry with GitHub OIDC. The one-time manual steps (repository,
secret scan, PyPI pending publisher, demo host, version 1.0.0, tag) are scripted in
`scripts/publish-wizard.sh`:

~~~bash
bash scripts/publish-wizard.sh   # interactive; run from a clean checkout as the ReccaFlames account
~~~

`.github/dependabot.yml` proposes weekly, grouped updates for uv (API and CLI), GitHub Actions,
the Dockerfile, and the Compose images, each at least 7 days after its release; security updates
open immediately. Every update runs CI and is merged by hand.

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
  espn:
    min_request_interval_seconds: 2

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
    provider: espn
    current_season: 2026-2027
    team_aliases:
      Fulham: Fulham FC
    seasons:
      "2026-2027":
        source_url: https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2026
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

- ~~`/metrics` (Prometheus), admin status endpoints, stale-data alerts, structured JSON logging~~
  (done: see [Observability](#observability)).
- Move remaining hard-coded competition configuration into explicit, documented configuration.

### Suggested implementation order

1. Move the registry to a validated config file (no behaviour change).
2. ~~Generalise the football-data.org adapter to any competition code.~~ (done)
3. ~~Make scheduler thresholds configurable per competition.~~ (done)
4. ~~Add fixtures and standings endpoints.~~ (done)
5. ~~Add admin/status endpoints with API-key authentication and metrics.~~ (done)
6. ~~Automatic season discovery and rollover.~~ (done)
7. Additional providers and fallback sources.

## License

[MIT](LICENSE). The data served by an instance has its own licences, listed in its `/llms.txt`
(for example OpenLigaDB data is ODbL).
