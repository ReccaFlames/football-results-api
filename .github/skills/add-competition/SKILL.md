---
name: add-competition
description: Add a new football competition or season to the Football Results API, or roll a competition over to a new current season. Use when asked to support another league/cup, add a season, or change which season is current.
---

# Add a competition or season

Competitions live in `config/competitions.yaml`. The file is validated with Pydantic, seeded into
PostgreSQL on API startup, and reseeded by the scheduler whenever its contents change. Competitions
removed from the file are disabled, not deleted.

## 1. Pick the provider and find the source

| Provider | Source format |
| --- | --- |
| `90minut` | `http://www.90minut.pl/liga/1/liga<ID>.html` (ID is per season) |
| `football-data` | `https://api.football-data.org/v4/competitions/<CODE>/matches?season=<start year>` |
| `espn` | `https://site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard?season=<start year>` |

football-data.org accepts any competition code (`PL`, `BL1`, `SA`, `PD`, `CL`, `BSA`, ...) and ESPN
any league (`eng.1`, `ger.1`, ...); all seasons of one competition must use the same code or
league. ESPN has corners, fouls, cards, and goal events without a token but no rounds;
football-data.org's free plan has rounds but no cards or goal events. When a provider's team names
differ from names already stored, add `team_aliases` (provider name → stored name). If the source is neither provider, use the
`add-data-provider` skill. The provider contract is in `docs/PROVIDERS.md`.

## 2. Edit `config/competitions.yaml`

~~~yaml
  - slug: slug                  # lowercase, hyphenated, unique; used in URLs
    name: Display Name
    country: Poland             # omit for international competitions
    format: league              # "league" (rounds) or "cup" (stages; longer match duration)
    provider: 90minut           # must be declared under providers:
    season_format: split        # "split" (2026-2027, default) or "calendar" (2026)
    season_start_month: 7       # split seasons only; 2-12, default 7. Omit for calendar seasons
    current_season: 2026-2027   # must be a key in seasons
    seasons:
      "2025-2026":
        source_url: http://www.90minut.pl/liga/1/liga14072.html
      "2026-2027":
        source_url: http://www.90minut.pl/liga/1/liga14675.html
~~~

Rules:
- Season keys must match `season_format`; split seasons span consecutive years.
- Set `season_start_month` to the month the season begins. It decides which year 90minut dates
  (which omit the year) fall in.
- `current_season` must exist in `seasons`; only the current season is auto-refreshed.
- A competition uses a single provider; historical fetches reject other providers and other
  football-data.org codes or ESPN leagues.
- Rollover is automatic by default (`schedule.season_rollover: auto`): football-data.org and ESPN seasons
  are discovered, and the scheduler switches to a known newer season once it has started and the
  current season has no pending fixtures. For 90minut, add the next season's entry in advance and
  leave `current_season` alone. `current_season` is then a minimum: raising it rolls over
  immediately, lowering it has no effect. To pin or roll back, set `schedule: {season_rollover:
  manual}` on the competition and change `current_season`.
- Changing a season's source URL resets its scheduler state on next seed.
- Thresholds (match duration, polling, backoff) can be overridden per competition with a `schedule`
  block listing only the changed keys; a new `format` needs a `match_duration_minutes` entry.

## 3. Verify

~~~bash
uv run python -m app.config check
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
~~~

With the stack running, check that the competition appears:

~~~bash
curl -s localhost:8000/api/v1/competitions | jq '.[] | {slug, season_format, current_season, scheduler_state}'
~~~

For a new 90minut page layout, add a parser test with a trimmed HTML fixture in
`tests/test_scraper.py`. Update the "Supported competitions" table in `README.md`.
