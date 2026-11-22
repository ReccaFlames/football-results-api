# Provider contract

A provider is an adapter that turns one external source (a website or a JSON API) into
`ScrapedMatch` records. Providers live in `app/scraper.py`, implement the `CompetitionScraper`
protocol, and are registered by name in `create_scraper_registry`. The scheduler and API select a
provider by the `provider` value stored on each season source; they need no changes for a new one.

## Interface

~~~python
class CompetitionScraper(Protocol):
    name: str  # registry key, also used in config/competitions.yaml
    capabilities: ProviderCapabilities

    async def scrape(
        self, competition: Competition, source: SeasonSource
    ) -> list[ScrapedMatch]: ...
    async def discover_seasons(self, competition: Competition) -> list[SeasonSource]: ...
~~~

### `name`

A short, stable, lowercase identifier (`90minut`, `football-data`, `espn`, `thesportsdb`). It is persisted in
`competition_season.provider`, so renaming it requires a data migration.

### `capabilities`

`ProviderCapabilities` declares what the provider can supply:

| Field | Meaning |
| --- | --- |
| `statistics` | Optional `ScrapedMatch` statistic fields the provider may fill (subset of `MATCH_STATISTICS`). Every other statistic must stay `None`. |
| `season_discovery` | `True` when `discover_seasons` can list seasons; `False` when sources are configured by hand. |
| `scheduled_fixtures` | `True` when unplayed fixtures with kickoff times are returned (the adaptive scheduler relies on them). |
| `requires_api_token` | `True` when the provider cannot work without a configured token. |
| `windowed` | `True` when `scrape` returns only the matches around today, not a whole season. A windowed provider may only be a `fallback_provider`; `config check` rejects it as `provider` (ADR 0003). |

Current providers:

| Provider | Statistics | Season discovery | Token | Windowed |
| --- | --- | --- | --- | --- |
| `90minut` | goals, half-time split, cards, first scorer and minute | no | no | no |
| `football-data` | goals, half-time split, cards, first scorer and minute | yes | yes | no |
| `espn` | goals, half-time split, cards, first scorer and minute, corners, fouls | yes | no | no |
| `thesportsdb` | goals | yes (fallback URLs only) | no (free key `123` by default) | yes |

football-data.org declares cards and first scorer, but its free plan omits goal and booking events,
so those fields stay `null` without a paid plan. Corners and fouls come only from `espn`.

`espn` reads `site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard` with one request
per calendar year (`?dates=YYYY&limit=1000`) and keeps events whose `season.year` is the season's
start year; a full page is treated as truncated and fails the scrape. ESPN has no matchweek, so
`round` is `None` and `stage` is the competition name. Requests are throttled by
`providers.espn.min_request_interval_seconds` (default 2 s). ESPN answers `403` to User-Agents
containing an email address, so the adapter sends the contact address in the `From` header instead.

`thesportsdb` reads TheSportsDB's documented v1 API
(`https://www.thesportsdb.com/api/v1/json/<key>/...`) with one `eventsday.php?d=<date>&l=<league>`
request per day, from `providers.thesportsdb.window_days_back` (default 7) days before today to
`window_days_ahead` (default 14) days after. Status `FT`/`AET`/`PEN`/`AWD`/`WO` is `finished`;
postponed, cancelled, abandoned, and suspended matches are `inactive`; live and not-started
matches are `scheduled`. Requests are throttled by `providers.thesportsdb.min_request_interval_seconds`
(default 3 s, within the free plan's 30 requests per minute). `discover_seasons` uses
`search_all_seasons.php?id=<league>`. The free key `123` is the default; with it `eventsday.php`
returns **at most 3 events per day** and `search_all_seasons.php` **at most 5 seasons**, so a busy
matchday can miss matches. A paid key in `FOOTBALL_RESULTS_THESPORTSDB_API_KEY` removes these
limits, and `config check` and the services warn about each `thesportsdb` fallback on the free key.
The key is part of every request path, so it is never stored in a source URL, logged, or included
in error messages.

Statistic rules shared by providers: a card count is yellow plus red cards, so a second yellow
counts as 2; an own goal belongs to the team whose score it raised; a goalless match has no first
goal. Provider team names can differ from stored names; map them with per-competition
`team_aliases` in `config/competitions.yaml`, which the registry applies to every scraped match.

### `scrape(competition, source)`

Return **all** fixtures of one competition-season, finished and unplayed.

- `status`: `"finished"` (scores required), `"scheduled"`, or `"inactive"` for postponed,
  suspended, or cancelled matches. Never return an inactive match as `"scheduled"`, or the
  scheduler will treat it as overdue and poll aggressively.
- `season`: `source.season`, unchanged.
- `played_at`: timezone-aware kickoff time. When the source omits the year, derive it with
  `app.seasons.season_year_for_month(season, month, competition.season_start_month)`; never assume
  a July start.
- `round` for league rounds; a descriptive `stage` for cup rounds and groups.
  `/api/v1/standings` selects cup matches by a `stage` substring, so group-stage fixtures must
  include the group name (for example `Group A`).
- `home_goals`/`away_goals` equal the full-time score. Unknown statistics stay `None`, never `0`.
- `first_goal_minute` is a string; stoppage time uses `"45+2"`.
- `source_url`: the most specific URL available (match page if possible).
- `provider_match_id`: the provider's stable match ID when it has one; it keeps API match IDs
  stable across refreshes.
- Raise on unusable responses (HTTP errors, missing payload sections). The caller records a failed
  `scrape_run` and backs off. A failure that affects only one match's optional details should log
  a warning and leave those fields `None` instead.

### `discover_seasons(competition)`

Return the seasons the provider currently offers for `competition`, newest first, as
`SeasonSource` objects with this provider's name and a URL that `source_from_url` accepts.

- Season identifiers must follow `competition.season_format` (`2026-2027` or `2026`).
- Derive the provider's own competition identifier from the competition's configured sources
  (for football-data.org, the `<CODE>` in `/v4/competitions/<CODE>/matches`). Raise `ValueError`
  if it cannot be determined unambiguously.
- Providers without discovery declare `season_discovery=False` and return an empty list.
- Discovery only reports sources; switching `current_season` (rollover) is the scheduler's job.
  The scheduler calls `discover_seasons` every `season_check_interval` and stores only seasons newer
  than the newest known one; it never changes existing seasons. Seasons with an invalid format or
  a URL that `source_from_url` rejects are ignored with a warning. See "Season discovery and
  rollover" in the README.

## Seasons

Each competition declares its season format in `config/competitions.yaml`:

| Setting | Values | Default |
| --- | --- | --- |
| `season_format` | `split` (`2026-2027`, consecutive years) or `calendar` (`2026`) | `split` |
| `season_start_month` | `2`–`12` for split seasons; calendar seasons always start in January | `7` (split) |

`app/seasons.py` maps between seasons and dates: `season_year_for_month` gives the calendar year of
a month within a season, and `season_for_date` gives the season containing a date.

## Source URL validation

`source_from_url` in `app/competitions.py` is a strict allow-list. It guards the historical fetch
endpoint against SSRF, so never accept arbitrary hosts or paths. Accepted today:

| Provider | URL |
| --- | --- |
| `90minut` | `http(s)://(www.)90minut.pl/liga/1/liga<ID>.html` |
| `football-data` | `https://api.football-data.org/v4/competitions/<CODE>/matches?season=<start year>` (`<CODE>` is 2–10 uppercase letters or digits, e.g. `PL`, `BL1`, `CL`, `BSA`) |
| `espn` | `https://site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard?season=<start year>` (`<league>` such as `eng.1`, `ger.1`, `uefa.champions`) |
| `thesportsdb` | `https://www.thesportsdb.com/league/<id>?season=<YYYY-YYYY>` (key-free; `<id>` such as `4422` for Ekstraklasa, `4661` for I liga) |

All football-data.org sources (or ESPN sources) of one competition must use the same `<CODE>` (or `<league>`); the configuration
check and the historical fetch endpoint reject mixed codes.

## Fallback sources

A competition may name a `fallback_provider` with a `fallback_url` per season (ADR 0002). Any
provider can be a fallback; nothing in the contract changes. The scheduler calls the same
`scrape(competition, source)` with the fallback source after `fallback_after_failures` primary
failures, and the season check calls the fallback's `discover_seasons` to fill missing fallback
URLs. A fallback import only updates status, kickoff, and scores and adds missing matches.

A fallback refresh skips a match whose home or away team, after `team_aliases`, is not already a
team of that season (ADR 0002 amendment). It logs a warning per skipped match and counts it in
`scrape_run.matches_skipped`, which the admin endpoints and the CLI `scrape-runs` table show. Give
every fallback team name that differs from the primary's a `team_aliases` entry, or its matches
are skipped. A windowed fallback that returns no matches (for example in a winter break) is a
success with no changes, and the next check is calculated from the season's stored matches.

A provider that needs credentials should expose `available` (false without them, as
`FootballDataScraper` does) so the scheduler skips it instead of failing.

## Adding a provider

Follow the `add-data-provider` skill in `.github/skills/`. Tests must use inline fixtures and never
hit the network; include a test that parsed statistics stay within the declared capabilities.
