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

A short, stable, lowercase identifier (`90minut`, `football-data`). It is persisted in
`competition_season.provider`, so renaming it requires a data migration.

### `capabilities`

`ProviderCapabilities` declares what the provider can supply:

| Field | Meaning |
| --- | --- |
| `statistics` | Optional `ScrapedMatch` statistic fields the provider may fill (subset of `MATCH_STATISTICS`). Every other statistic must stay `None`. |
| `season_discovery` | `True` when `discover_seasons` can list seasons; `False` when sources are configured by hand. |
| `scheduled_fixtures` | `True` when unplayed fixtures with kickoff times are returned (the adaptive scheduler relies on them). |
| `requires_api_token` | `True` when the provider cannot work without a configured token. |

Current providers:

| Provider | Statistics | Season discovery | Token |
| --- | --- | --- | --- |
| `90minut` | goals, half-time split, cards, first scorer and minute | no | no |
| `football-data` | goals, half-time split, cards, first scorer and minute | yes | yes |

Corners and fouls are declared by neither provider and therefore always `null`.

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

All football-data.org sources of one competition must use the same `<CODE>`; the configuration
check and the historical fetch endpoint reject mixed codes.

## Adding a provider

Follow the `add-data-provider` skill in `.github/skills/`. Tests must use inline fixtures and never
hit the network; include a test that parsed statistics stay within the declared capabilities.
