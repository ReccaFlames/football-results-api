# TheSportsDB is a windowed fallback source for the 90minut competitions

Ekstraklasa and I liga come only from 90minut.pl, so they have no fallback source
([ADR 0002](0002-fallback-source-refreshes.md)); ESPN has no Polish leagues. TheSportsDB's
documented v1 API covers both (leagues 4422 and 4661) and is current, but with the free key its
season endpoint returns only 15 events. We use its documented per-day endpoint
(`eventsday.php?d=<date>&l=<league>`) over a window around today (7 days back, 14 days ahead by
default) and make TheSportsDB a **windowed source**: it may only be a fallback source, never a
primary source. This works because a fallback refresh already only updates and adds matches and
never deletes them.

## Considered options

- **`eventsround.php`, one request per round** (a whole season): rejected because the endpoint is
  missing from TheSportsDB's current API reference and could disappear without notice; new
  providers must use documented or explicitly permitted access.
- **A paid TheSportsDB key** (full `eventsseason.php`): rejected to keep self-hosting free.
- **football-data.org, openfootball, API-Football, ekstraklasa.org**: football-data.org covers no
  Polish league on any plan; openfootball's Polish data stops at 2024–25; API-Football's free plan
  does not cover current seasons; ekstraklasa.org's `robots.txt` disallows `/api/`.
- **Undocumented sites (SofaScore, FotMob, Flashscore)**: rejected because their terms forbid
  automated access.

## Consequences

- The free public key `123` is the default, so the fallback works without configuration;
  `FOOTBALL_RESULTS_THESPORTSDB_API_KEY` overrides it. Source URLs are stored without a key
  (`https://www.thesportsdb.com/league/<id>?season=<YYYY-YYYY>`).
- An empty window (for example during the winter break) is a successful fallback refresh with no
  changes, not a primary failure or an empty-season error.
- A fallback refresh calculates the next check from the season's stored matches, not only from the
  window, so the scheduler does not idle when the window ends before the next round.
- Matches that finish outside the window during an outage stay unchanged until the primary
  recovers or a later window covers them.
- With the free key, `eventsday.php` returns at most 3 events per day and `search_all_seasons.php`
  at most 5 seasons. We accept these limits for the default: a busy matchday can miss matches, and
  those matches stay unchanged until the primary recovers or a later window covers them. We add no
  workarounds (other endpoints, extra requests, or undocumented access). `config check` and the
  services warn about every `thesportsdb` fallback on the free key, without printing the key.
- Operators who need complete matchdays should set a paid key in
  `FOOTBALL_RESULTS_THESPORTSDB_API_KEY`, which removes both limits.
- TheSportsDB uses English team names (`Polonia Warsaw`); every team whose name differs from
  90minut's needs a `team_aliases` entry (an alias that maps a name to itself is rejected), and
  matches with unmapped teams are skipped (ADR 0002). For 2026-27 only `Legia Warsaw` and
  `Polonia Warsaw` differ.
