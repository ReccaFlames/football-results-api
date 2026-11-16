# AGENTS.md

Guidance for AI coding agents working **on** this repository. For how to *use* the API, see
`README.md`. For the planned direction and the ordered backlog, see `docs/ROADMAP.md`.

## Project in one paragraph

Football Results API (repo: `ekstraklasa-results-api`, package: `football-results-api`) imports
fixtures and results from external sources (90minut.pl HTML, football-data.org JSON), stores them in
PostgreSQL, and serves them through FastAPI. A separate scheduler process refreshes current seasons
adaptively based on kickoff times. Goal: a generic, easy-to-configure, AI-agent-first league data
API.

## Layout

| Path | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app and all HTTP endpoints; module-level `store` singleton |
| `app/scheduler.py` | Scheduler loop (`python -m app.scheduler`): seed → heartbeat → claim due seasons → scrape → persist |
| `app/scheduling.py` | Pure functions: `calculate_next_check`, `failure_backoff` |
| `app/scraper.py` | Provider adapters (`NinetyMinutScraper`, `FootballDataScraper`), parsers, `ScraperRegistry` |
| `app/competitions.py` | Competition summary helpers and `source_from_url` validation |
| `app/config.py` | Environment settings, validated competition configuration, and `python -m app.config check` |
| `config/competitions.yaml` | Competition, provider, and schedule defaults |
| `app/repository.py` | `PostgresResultRepository`: seeding, queries, atomic season replace, scheduler claims |
| `app/models.py` | Pydantic API/domain models (`Competition`, `SeasonSource`, `ScrapedMatch`, `MatchResult`, ...) |
| `app/db_models.py` | SQLAlchemy ORM tables |
| `app/config.py` | `Settings` from env vars with prefix `FOOTBALL_RESULTS_` |
| `migrations/` | Alembic migrations (schema changes go here only; no `create_all`) |
| `tests/` | pytest; API tests use `FakeRepository` and need no database |
| `tests/integration/` | Repository and migration tests against real PostgreSQL (skipped without `FOOTBALL_RESULTS_TEST_DATABASE_URL`) |

## Commands

~~~bash
uv sync --extra dev                 # install
uv run python -m app.config check   # validate the competition configuration
uv run pytest -q                    # unit tests; integration tests skip without a test DB
uv run ruff check .                 # lint (line length 100)
uv run ruff format --check .        # formatting (enforced in CI)
uv run alembic upgrade head --sql   # validate migrations offline

# PostgreSQL integration tests (repository + migrations), disposable DB on port 55432
docker compose --profile test up -d --wait test-db
FOOTBALL_RESULTS_TEST_DATABASE_URL=postgresql+psycopg_async://football:football@localhost:55432/football_test uv run pytest -q
docker compose --profile test down
docker compose up -d db             # local PostgreSQL
uv run alembic upgrade head && uv run uvicorn app.main:app --reload
uv run python -m app.scheduler      # scheduler, second terminal
~~~

Before declaring a change done, run `ruff check`, `ruff format --check`, and `pytest` **with** the
integration database when touching `app/repository.py`, `app/db_models.py`, or `migrations/`.
CI (`.github/workflows/ci.yml`) runs all of these on every push and pull request.

## Conventions and gotchas

- Python 3.12+, fully async (SQLAlchemy async + httpx). Type hints everywhere; Pydantic v2 models.
- Seasons are strings `YYYY-YYYY` (validated by regex in request models).
- `config/competitions.yaml` is seeded on API and scheduler startup; the scheduler reseeds only when
  the file changes. Configured competitions overwrite names, sources, provider, and current season.
  Removed competitions are disabled, not deleted, so their stored results remain in the database.
- A refresh deletes and re-inserts all matches of one competition-season in one transaction.
  Database match IDs are therefore not stable; `source_key` is a hash of season/stage/teams/kickoff.
- `MatchResult` (API) exposes only finished matches; `ScrapedMatch` (internal) also has scheduled ones.
- Statistics that a provider cannot supply must stay `null` — never default to `0`.
- 90minut pages are ISO-8859-2 and use Polish month names with Europe/Warsaw time.
- football-data.org is throttled (`FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS`, default 6s) and
  needs `FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN`. Never commit tokens or `.env`.
- Tests must not hit the network. Parser tests use inline HTML/JSON fixtures.
- The integration test database name must contain `test`; the fixtures truncate all tables and
  the migration test downgrades to `base`.
- The ORM models must match the migrations exactly (`alembic check` runs in the integration
  tests). Example: `competition.slug` is a unique constraint plus a separate non-unique index.
- Read the application and default User-Agent versions from the `football-results-api` package
  metadata; `pyproject.toml` is the authoritative version source.
- Build Docker images from `uv.lock` with `uv sync --frozen`.
- `RefreshRequest.competition` is required. Competition summaries may have a null `current_season`.
- Update `README.md` when behaviour, endpoints, or configuration change.

## Roadmap workflow

`docs/ROADMAP.md` contains the ordered **Implementation checklist**. When starting work, pick the
first unchecked item (unless the user says otherwise). When a sub-task is completed, tick it
(`[x]`) in the same change; tick the parent item once all sub-tasks are done. Do not skip ahead
without the user's agreement, because later items depend on earlier ones.

## Skills in this repo

Task-specific playbooks live in `.github/skills/`:

- `add-competition` – add a competition or season, or roll over the current season.
- `add-data-provider` – add a new source adapter (website or API).
- `database-migration` – change the schema safely with Alembic.
