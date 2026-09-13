# Football Results API

FastAPI service that scrapes fixtures and completed football matches from 90minut.pl and stores
competitions, seasons, results, schedules, and scrape metadata in PostgreSQL.

## Architecture

- FastAPI provides the HTTP API.
- Provider-specific adapters parse 90minut pages and football-data.org JSON.
- SQLAlchemy's async PostgreSQL dialect handles persistence.
- Alembic owns database migrations.
- The initial competition registry is seed data only.
- PostgreSQL is the runtime source of truth for competitions and seasons.
- A refresh replaces one competition-season atomically.
- A separate adaptive scheduler process refreshes only current seasons and sleeps between fixtures.
- Season sources persist their provider so additional websites can be added without changing the scheduler.

Database tables: competition, competition_season, match_result, scrape_run, scheduler_heartbeat,
and alembic_version.

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

## API examples

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

Application variables:

- FOOTBALL_RESULTS_DATABASE_URL
- FOOTBALL_RESULTS_REQUEST_TIMEOUT_SECONDS
- FOOTBALL_RESULTS_USER_AGENT
- FOOTBALL_RESULTS_SCHEDULER_LOOP_SECONDS
- FOOTBALL_RESULTS_SCHEDULER_CLAIM_LIMIT
- FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN
- FOOTBALL_RESULTS_FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS

Deployment variables:

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
