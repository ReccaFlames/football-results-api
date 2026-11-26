# football-results

Command-line client for the Football Results API. It covers competitions, matches, standings,
teams, refresh jobs, and historical season fetches. When stdout is piped (or with `--json`) it
prints the API's JSON response body; on a terminal it prints tables. Errors go to stderr as one
line of RFC 9457 problem JSON, with stable exit codes.

~~~bash
uv tool install football-results-cli   # or: pipx install football-results-cli
football-results competitions          # without FOOTBALL_RESULTS_URL: the built-in default (see --help)
export FOOTBALL_RESULTS_URL=https://football.example.com   # your own or a private instance
export FOOTBALL_RESULTS_API_KEY=...   # optional; needs FOOTBALL_RESULTS_URL; admin key for refresh, fetch-season, status
football-results competitions
football-results matches -c ekstraklasa --team legia-warszawa --status finished --order desc --limit 5
football-results standings -c ekstraklasa --fields position,team_name,points --json
~~~

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage error (HTTP 400/422 or a local check) |
| 3 | Not found (HTTP 404) |
| 4 | Upstream or stale: server, network, timeout, rate limit, failed job, `--fail-if-stale` |
| 5 | Auth: missing or invalid API key, or writes disabled |

Run `football-results COMMAND --help` for options and examples. The
[repository README](https://github.com/ReccaFlames/football-results-api#command-line-client) has
the full reference, and a running server documents the CLI at `/agents#cli`.

For coding agents, the
[Agent Skill](https://github.com/ReccaFlames/football-results-api/blob/main/skills/football-results/SKILL.md)
(also served by every instance at `/skills/football-results/SKILL.md`) teaches this CLI; see
`/agents#skill` on an instance for how to install it.

MIT licensed. Data licences depend on the instance you query; its `/llms.txt` lists them.
