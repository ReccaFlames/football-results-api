---
name: add-data-provider
description: Add a new external data source (website scraper or JSON API adapter) to the Football Results API. Use when asked to import data from a new site/API, or to generalise an existing provider (e.g. football-data.org beyond the Premier League).
---

# Add a data provider

Providers are selected by the `provider` string stored on each season source. The scheduler and
API do not need changes for a new provider.

## Contract

Implement the `CompetitionScraper` protocol in `app/scraper.py`:

~~~python
async def scrape(self, competition: Competition, source: SeasonSource) -> list[ScrapedMatch]
~~~

Return **all** fixtures of the season, finished and scheduled:

- `status`: `"finished"` (scores required) or `"scheduled"`. Postponed/cancelled matches must not
  be returned as overdue scheduled fixtures, or the scheduler will poll aggressively.
- `played_at`: timezone-aware datetime (kickoff). Required.
- `round` for leagues; descriptive `stage` for cups.
- `home_goals`/`away_goals` equal full-time score; other statistics `None` when unknown — never `0`.
- `first_goal_minute` as a string, stoppage time as `"45+2"`.
- `source_url`: most specific URL (match page if available).
- Raise an exception on unusable responses; the caller records a failed `scrape_run` and backs off.

## Steps

1. Write a pure parse function (`parse_<provider>_matches(payload, competition, source)`) separate
   from HTTP code so it can be unit tested offline.
2. Write the scraper class: one `httpx.AsyncClient` per scrape, `User-Agent` from settings,
   `settings.request_timeout_seconds`, and a minimum request interval/lock if the source has rate
   limits (see `FootballDataScraper`). Be polite to HTML sites.
3. Register it in `create_scraper_registry` under a new provider key; add any settings (token,
   interval) to `app/config.py`, `.env.example`, `compose.yaml` (both `api` and `scheduler`), and
   the README configuration table.
4. Extend `source_from_url` in `app/competitions.py` with a strict allow-list for the new host/path
   (it guards against SSRF via the historical fetch endpoint). Never accept arbitrary URLs.
5. Add a seed helper and competition entries (see the `add-competition` skill).
6. Tests in a new `tests/test_<provider>_scraper.py` with inline fixtures: finished match, scheduled
   match, missing data stays `None`, malformed payload raises, URL validation accepts/rejects.
   Never hit the network in tests.
7. Update the README statistics-coverage table with what the provider supplies.

~~~bash
uv run pytest -q && uv run ruff check .
~~~
