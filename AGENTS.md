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
| `app/main.py` | FastAPI app and all HTTP endpoints; module-level `store` and `scraper` singletons |
| `app/scheduler.py` | Scheduler loop (`python -m app.scheduler`): seed → heartbeat → claim due seasons → scrape → persist |
| `app/scheduling.py` | Pure functions: `calculate_next_check`, `failure_backoff` |
| `app/scraper.py` | Provider adapters (`NinetyMinutScraper`, `FootballDataScraper`), parsers, `ScraperRegistry` |
| `app/competitions.py` | Hard-coded competition registry (seed data) and `source_from_url` validation |
| `app/repository.py` | `PostgresResultRepository`: seeding, queries, atomic season replace, scheduler claims |
| `app/models.py` | Pydantic API/domain models (`Competition`, `SeasonSource`, `ScrapedMatch`, `MatchResult`, ...) |
| `app/db_models.py` | SQLAlchemy ORM tables |
| `app/config.py` | `Settings` from env vars with prefix `FOOTBALL_RESULTS_` |
| `migrations/` | Alembic migrations (schema changes go here only; no `create_all`) |
| `tests/` | pytest; API tests use `FakeRepository` and need no database |

## Commands

~~~bash
uv sync --extra dev                 # install
uv run pytest -q                    # tests (no DB needed)
uv run ruff check .                 # lint (line length 100)
uv run ruff format --check .        # formatting (not yet enforced; 1 file currently differs)
uv run alembic upgrade head --sql   # validate migrations offline
docker compose up -d db             # local PostgreSQL
uv run alembic upgrade head && uv run uvicorn app.main:app --reload
uv run python -m app.scheduler      # scheduler, second terminal
~~~

Run `pytest` and `ruff check` before declaring any change done.

## Conventions and gotchas

- Python 3.12+, fully async (SQLAlchemy async + httpx). Type hints everywhere; Pydantic v2 models.
- Seasons are strings `YYYY-YYYY` (validated by regex in request models).
- `app/competitions.py` is re-seeded **on API startup and every scheduler cycle**; it overwrites
  names, source URLs, provider and `is_current` in the DB. Editing DB rows by hand will be reverted.
- A refresh deletes and re-inserts all matches of one competition-season in one transaction.
  Database match IDs are therefore not stable; `source_key` is a hash of season/stage/teams/kickoff.
- `MatchResult` (API) exposes only finished matches; `ScrapedMatch` (internal) also has scheduled ones.
- Statistics that a provider cannot supply must stay `null` — never default to `0`.
- 90minut pages are ISO-8859-2 and use Polish month names with Europe/Warsaw time.
- football-data.org is throttled (`FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS`, default 6s) and
  needs `FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN`. Never commit tokens or `.env`.
- Tests must not hit the network. Parser tests use inline HTML/JSON fixtures.
- Keep the version in sync in `pyproject.toml` and `app/main.py` (`FastAPI(version=...)`).
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
