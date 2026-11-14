---
name: add-competition
description: Add a new football competition or season to the Football Results API, or roll a competition over to a new current season. Use when asked to support another league/cup, add a season, or change which season is current.
---

# Add a competition or season

The registry currently lives in code (`app/competitions.py`). It is seeded into PostgreSQL on API
startup and on every scheduler cycle, so the code is authoritative. (Roadmap: move to a config
file — if `config/competitions.yaml` exists, edit that instead and ignore the code steps below.)

## 1. Pick the provider and find the source

| Provider | Source format | Helper |
| --- | --- | --- |
| `90minut` | `http://www.90minut.pl/liga/1/liga<ID>.html` (ID is per season) | `_source(season, league_id)` |
| `football-data` | `https://api.football-data.org/v4/competitions/PL/matches?season=<start year>` | `_football_data_source(season)` |

football-data is currently hard-wired to `PL` in both `_football_data_source` and
`FOOTBALL_DATA_PATH`/`source_from_url`. A non-PL competition requires generalising those first
(add a competition code parameter, update validation and tests). If the source is neither, use the
`add-data-provider` skill.

## 2. Edit `app/competitions.py`

Add or update the entry in `COMPETITIONS`:

~~~python
"slug": Competition(
    slug="slug",                # lowercase, hyphenated, unique; used in URLs
    name="Display Name",
    country="Poland",           # None for international competitions
    format="league",            # "league" (rounds) or "cup" (stages; longer match duration)
    current_season="2026-2027", # must be a key in seasons
    seasons={
        "2025-2026": _source("2025-2026", 14072),
        "2026-2027": _source("2026-2027", 14675),
    },
),
~~~

Rules:
- `current_season` must exist in `seasons`; only the current season is auto-refreshed.
- A competition should use a single provider; historical fetches reject providers not configured
  for that competition.
- Rolling over = add the new season entry and change `current_season`. Changing a season's
  source URL resets its scheduler state on next seed.

## 3. Verify

~~~bash
uv run pytest -q && uv run ruff check .
~~~

Then, with the stack running, check the competition appears and refreshes:

~~~bash
curl -s localhost:8000/api/v1/competitions | jq '.[] | {slug, current_season, scheduler_state}'
curl -s -X POST localhost:8000/api/v1/results/refresh -H 'content-type: application/json' \
  -d '{"competition":"<slug>"}' | jq '.count'
~~~

For a new 90minut page layout, add a parser test with a trimmed HTML fixture in
`tests/test_scraper.py`. Update the "Supported competitions" table in `README.md`.
