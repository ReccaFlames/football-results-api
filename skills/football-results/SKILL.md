---
name: football-results
description: Football fixtures, results, league tables, teams, and data freshness from a Football Results API through the `football-results` CLI. Use when asked about scores, upcoming matches, standings, or a team's form in the competitions that API serves, or whether that data is current.
compatibility: Needs the football-results CLI (Python 3.12+) or curl, and network access to a Football Results API set in FOOTBALL_RESULTS_URL.
---

# Football Results

Answer football questions from the API's stored data with the `football-results` CLI. Every fact in
an answer comes from command output; report gaps (`null`, stale seasons) as gaps.

## Setup

```sh
football-results --version
```

If the command is missing, install it with `uv tool install football-results-cli` (or
`pipx install football-results-cli`; in a checkout of the API repository, `uv tool install ./cli`),
or use [curl](#without-the-cli).

- `FOOTBALL_RESULTS_URL` is the API base URL. When it is unset, the CLI uses its built-in default
  (listed under `--url` in the CLI's help). If that fails with `connection-failed`, ask the user for
  the URL. With `FOOTBALL_RESULTS_API_KEY` set, also set `FOOTBALL_RESULTS_URL` to the instance
  that issued the key.
- Piped stdout is the API's JSON response body, so agents get JSON by default; `--json` forces it.
  Notes and errors go to stderr.

## First step: resolve slugs

Commands take slugs, never display names. Resolve both before querying:

```sh
football-results competitions --fields slug,name,format,season_format,current_season,fetched_seasons
football-results teams -c ekstraklasa -q legia --fields slug,name,aliases
```

- `competitions` is the list of what this API serves. A competition missing from it is not
  available; say so.
- Seasons are strings in the competition's `season_format`: `2026-2027` (split) or `2026`
  (calendar). Omit `-s` for the `current_season`; past seasons are queryable only when listed in
  `fetched_seasons`.
- `teams -q` matches a name fragment, slug, or alias. Pass the chosen `slug` to `--team`; when
  several teams match, pick by `name` and `aliases` or ask the user.

## Recipes

The examples use `ekstraklasa` and `legia-warszawa`; substitute the slugs you resolved.

A team's latest results:

```sh
football-results matches -c ekstraklasa --team legia-warszawa --status finished --order desc --limit 5 --fields kickoff_at,home_team,away_team,home_goals,away_goals
```

Upcoming fixtures:

```sh
football-results matches -c ekstraklasa --status scheduled --date-from "$(date -u +%F)" --limit 10 --fields kickoff_at,round,home_team,away_team
```

League table (cup competitions also need `--stage`, a substring of a match's `stage`, such as
`--stage "Group A"`):

```sh
football-results standings -c ekstraklasa --fields position,team_name,played,points,form
```

One round, a date range, or a past season:

```sh
football-results matches -c ekstraklasa --round 3 --fields kickoff_at,home_team,away_team,home_goals,away_goals
football-results matches -c ekstraklasa --status finished --date-from 2026-08-01 --date-to 2026-08-31
football-results standings -c ekstraklasa -s 2025-2026 --fields position,team_name,points
```

Head-to-head: filter by one team, then select the other team's slug:

```sh
football-results matches -c ekstraklasa --team legia-warszawa --status finished --all --fields kickoff_at,home_team_slug,away_team_slug,home_goals,away_goals | jq --arg other lech-poznan '.data[] | select(.home_team_slug == $other or .away_team_slug == $other)'
```

## Reading the output

- `matches` and `teams` return `{data, pagination, freshness}` (`teams` has no `freshness`);
  `standings` returns `{competition, season, stage, data, freshness}`; `competitions` returns a
  list.
- `--fields` keeps output compact. The key field is always included: `id` for matches,
  `team_slug` for standings, `slug` for competitions and teams.
- A non-null `pagination.next_cursor` means more pages. `--all` follows it and prints one combined
  response (capped by `--max-items`); or repeat the command with `--cursor` and the same filters.
- Match `status` is `finished`, `scheduled`, or `inactive` (postponed or cancelled).
- Times are ISO 8601 with an offset; convert them to the user's time zone when presenting.
  Naive `--date-from` and `--date-to` values are UTC.

## Null means unknown

`null` means the provider did not supply the value; it is never a stand-in for 0. A `null`
`home_corners` is "not recorded", not "no corners", and scheduled matches have `null` goals.
Present nulls as "not available", leave them out of sums and averages, and say how many matches
lacked the value.

## Freshness

`freshness` has one entry per competition-season: `data_as_of` (last successful import; `null`
means never fetched), `next_update_at` (next scheduled check), and `source`.

- Quote `data_as_of` when an answer depends on recent matches ("as of 18 Nov, 20:05 UTC").
- Add `--fail-if-stale` to `matches` or `standings` when currency matters. The data still prints;
  exit 4 with `urn:football-results:cli:stale-data` means a season was never fetched or its update
  is overdue, so answer with that caveat.
- Fresh data appears after `next_update_at`; repeating a query sooner returns the same data.

## Refresh and history fetch

The scheduler refreshes current seasons on its own, timed around kickoffs, so reads are almost
always enough. `refresh`, `fetch-season`, and `status` need an admin key in
`FOOTBALL_RESULTS_API_KEY`, issued by the API operator. When the variable is set, the CLI sends the
key on every request, so reads use that key's quota too. Ask the user before queuing a job, and use one only for missing or stale data:

```sh
football-results refresh -c ekstraklasa --wait
football-results fetch-season -c ekstraklasa -s 2025-2026 --wait
football-results status 42
```

- `refresh` is for an overdue current season; `fetch-season` imports a past season that is absent
  from `fetched_seasons`, which you then read with `-s`.
- Exit 5 means no key, a wrong key, a client key without the admin role (`forbidden`), or writes
  disabled on this instance: answer from the stored data and its `data_as_of`.

## Errors

Errors are one line of RFC 9457 problem JSON on stderr: `type`, `title`, `detail`, plus
`parameter`, `valid_values`, and `hint` when relevant. Branch on the exit code, then on `type`;
correct the request from `valid_values` or `hint` and retry once.

| Exit code | Meaning | Next step |
| --- | --- | --- |
| 0 | Success | Answer from stdout. |
| 2 | Usage error: invalid option or value | Fix `parameter` using `valid_values` or `hint`. |
| 3 | Not found: competition, season, or job | Re-check slugs with `competitions` and seasons with `fetched_seasons`. |
| 4 | Upstream or stale: unreachable API, timeout, server error, job conflict, rate limit, failed job, stale data | `connection-failed`: check `FOOTBALL_RESULTS_URL`; `stale-data`: answer with a caveat; otherwise retry later. |
| 5 | Auth: missing or invalid API key, a client key on an admin operation, or writes disabled | Skip the write. A read with `unauthorized` needs a key: `$FOOTBALL_RESULTS_URL/llms.txt` (Auth) explains requesting one with `create-access-request` and `get-access-request`. |

Server problem types are `/problems/<slug>` (for example `/problems/unknown-competition` or
`/problems/invalid-parameter`); the CLI's own are `urn:football-results:cli:<slug>`. The API
explains each one at `$FOOTBALL_RESULTS_URL/problems` and the CLI's at
`$FOOTBALL_RESULTS_URL/agents#cli`.

## Without the CLI

Every read command is an HTTP GET whose options are query parameters (`--date-from` is
`date_from`):

```sh
curl -s "$FOOTBALL_RESULTS_URL/api/v1/matches?competition=ekstraklasa&team=legia-warszawa&status=finished&order=desc&limit=5"
curl -s "$FOOTBALL_RESULTS_URL/llms.txt"
```

`$FOOTBALL_RESULTS_URL/llms.txt` shows the top tasks as curl commands, and
`$FOOTBALL_RESULTS_URL/agents.md` documents every operation.
