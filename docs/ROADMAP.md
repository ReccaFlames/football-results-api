# Roadmap and review notes

This is the working backlog for turning the Football Results API into a **generic, easy-to-configure,
AI-agent-first** league data API. The items are in the recommended implementation order. Last
reviewed: 2026-11-14, at version 0.12.1.

## Review summary (2026-11-14)

### What is good

- The code is small and readable. Responsibilities are clearly separated: API, scheduler,
  scheduling rules, providers and repository.
- The adaptive scheduler is a strong design:
  - It decides when to refresh based on kickoff times.
  - Workers claim due seasons with `FOR UPDATE SKIP LOCKED`.
  - Failed refreshes back off and are recorded.
- Parsers are pure functions with offline fixture tests. Statistics a source does not provide are
  left `null` instead of being guessed.
- `source_from_url` only accepts known source URLs, which blocks SSRF.
- Each season refresh is written to the database in a single transaction.
- At review time, all 31 tests passed and `ruff check` was clean.

### What should change

1. **The registry is hard-coded and re-seeded every scheduler cycle (30 s).**
   - Adding or rolling over a league needs a code change and a deploy.
   - Any manual change in the database is reverted within seconds.
   - Each cycle runs a write transaction for nothing.
2. **The write endpoints have no authentication.**
   - Anyone who can reach the API can trigger `POST /results/refresh` or `/history/fetch`.
   - Each call scrapes upstream synchronously inside the HTTP request. With 90minut that means
     fetching every match detail page one after another, with no throttling.
   - This is an abuse and politeness risk for the source sites.
3. **One bad 90minut detail page fails the whole season refresh.** A goal sequence that does not
   add up to the final score raises `ValueError`.
4. **Match identifiers are not stable.**
   - A refresh deletes all matches and inserts them again.
   - The API exposes no match `id`.
   - `source_key` includes the kickoff time, so a postponed match becomes a different match.
   - Agents and clients have nothing stable to refer to.
5. **The read API has gaps:**
   - `/results` has no pagination or limit.
   - `stale` only means "never fetched".
   - Without filters, `refreshed_at` is the latest time across all competitions.
   - Scheduled fixtures are stored but cannot be retrieved.
6. **Team names are raw strings.**
   - There is no team entity and no aliases.
   - Searches use `ILIKE` with unescaped `%` and `_`.
   - The same club can appear under different names in different sources.
7. **The season format is fixed at `YYYY-YYYY`, and the code assumes the season starts in July.**
   This rules out calendar-year leagues (MLS, Scandinavia, Brazil).
8. **Test gaps:**
   - No tests run against PostgreSQL. The most complex logic (seeding, claiming due seasons,
     replacing matches) is untested.
   - There is no CI. (Both fixed in step 1.)
9. **Housekeeping:**
   - The version is set in two places, and the User-Agent still says 0.11.
   - `.idea/` is committed.
   - The Dockerfile uses `pip install .` and ignores `uv.lock`.
   - Some defaults are still Ekstraklasa-specific (`RefreshRequest.competition`,
     `MatchResult.competition`).
   - `list_competitions` raises if a competition has no current season.
   - The 90minut detail cache is an unbounded in-memory dict.
   - One file fails `ruff format --check`. (Fixed in step 1.)

## Agent-first: current practice (researched 2026-11)

- **One contract.** The OpenAPI spec is the single source of truth. Everything else (docs, CLI,
  MCP, llms.txt) is derived from it, so it needs good descriptions, examples, `operationId`s and
  "when to use" text.
- **Errors.** Use RFC 9457 `application/problem+json` with actionable hints and lists of valid
  values, so an agent can correct itself.
- **Response shape.**
  - Keep responses compact, with field selection and pagination.
  - Use stable IDs and human-readable slugs.
  - Add freshness metadata (`data_as_of`, `next_update_at`).
  - Send `RateLimit` and `Retry-After` headers.
- **`/llms.txt` and an agent page.** A Markdown usage guide at a well-known path. It is cheap and
  agents fetch it.
- **CLI.** Coding agents are fluent in the shell, and a CLI costs no context until it is used.
  - Non-interactive.
  - JSON output (the default when stdout is not a TTY).
  - Stable exit codes.
  - JSON errors on stderr.
  - `--help` with examples.
- **Agent Skills (SKILL.md, agentskills.io).**
  - Supported by Copilot, Claude Code, Codex and others.
  - Only the name and description are loaded up front; the body loads on demand.
  - A consumer skill should teach the CLI.
  - Copilot can install a skill from a URL: `copilot skill add https://.../SKILL.md`.
- **MCP.**
  - Needed for chat clients without a shell (ChatGPT, claude.ai, IDE chats).
  - Current spec: 2026-07-28, which is stateless.
  - Build with the official `mcp` SDK (`MCPServer`, formerly FastMCP), served by FastAPI.
  - Curate 4–6 task-oriented tools. Do not auto-convert every endpoint: tool descriptions use up
    context on every turn, and curated servers perform better.
- **Skip for now.** A2A agent cards, NLWeb and Arazzo matter little for a small data API.

## Implementation checklist

Work top to bottom. The reasoning behind the order: every agent-facing surface (CLI, skill, MCP,
llms.txt) copies the API contract. Fix the contract and configuration first, or every surface will
need to be rewritten.

How to use this list:
- Tick a sub-task (`[x]`) when it is merged; tick the parent item when all of its sub-tasks are done.
- Each item should normally be one branch and one PR.
- Every item is done only when `uv run pytest -q` and `uv run ruff check .` pass and `README.md`
  reflects the change.
- If scope changes, edit this list in the same PR.

**Next up: item 21 (landing page and access requests).** It blocks the server push; everything
else for the push is ready. Work on it before any other unchecked item, then remove this note.

### Phase 0: Foundations

- [x] **1. CI and PostgreSQL integration tests**
  - [x] GitHub Actions workflow: `uv sync`, `pytest`, `ruff check`, `ruff format --check`,
        `alembic upgrade head --sql`
  - [x] PostgreSQL service container (or testcontainers) for integration tests
  - [x] Repository tests: seeding, `claim_due_seasons` (incl. `SKIP LOCKED`), `replace_competition`,
        `record_scrape_failure`
  - [x] Migration round-trip test (upgrade → downgrade → upgrade)
- [x] **2. Housekeeping**
  - [x] Single version source (read from package metadata in `app/main.py` and the User-Agent)
  - [x] Untrack `.idea/` and add it to `.gitignore`
  - [x] Build the Docker image from `uv.lock` (`uv sync --frozen`)
  - [x] Run `ruff format` on the whole repo
  - [x] Remove the Ekstraklasa defaults (`RefreshRequest.competition`, `MatchResult.competition`)
  - [x] `list_competitions` handles a competition with no current season
- [x] **3. Safe write endpoints**
  - [x] API-key authentication (`X-API-Key`, `FOOTBALL_RESULTS_API_KEYS`) for refresh and
        history fetch
  - [x] Refresh queues the season for the scheduler and returns `202` with a status URL
  - [x] History fetch runs as a queued job
  - [x] Job/status endpoint backed by `scrape_run`
- [x] **4. Polite and resilient scraping**
  - [x] Per-host throttling for 90minut (configurable minimum interval)
  - [x] A detail-page failure affects only that match (fields stay `null`, warning logged)
  - [x] Bounded or persistent detail-page cache

### Phase 1: Generic and configurable (the README target state)

- [x] **5. Configuration file**
  - [x] `config/competitions.yaml` schema (competitions, providers, schedule defaults)
  - [x] Pydantic validation and a `python -m app.config check` command
  - [x] `FOOTBALL_RESULTS_CONFIG_FILE` setting with a default file shipped in the image
  - [x] Seed only on startup or when the file changes, not every scheduler cycle
  - [x] Competitions removed from the file are disabled, not deleted
  - [x] Remove the hard-coded registry from `app/competitions.py`
- [x] **6. Stable data model**
  - [x] Upsert matches by provider match ID, or a key that ignores kickoff time
  - [x] Expose a stable match `id` in the API
  - [x] `team` table with canonical slugs and aliases; matches reference team IDs
  - [x] Escape `%` and `_` in team/stage search
- [x] **7. Generic seasons and providers**
  - [x] Allow calendar-year seasons (`2026`) alongside `2026-2027`
  - [x] Season-to-date mapping per competition (no hard-coded July start)
  - [x] football-data.org adapter and URL validation for any competition code
  - [x] Written provider contract (capabilities, `discover_seasons`)
- [x] **8. Scheduler settings per competition**
  - [x] Scheduler thresholds read from the configuration file
  - [x] Automatic season discovery and rollover

### Phase 2: Agent-first API surface

- [x] **9. API contract v1.1**
  - [x] `GET /api/v1/matches` (status, date range, team, round/stage filters)
  - [x] `GET /api/v1/standings`
  - [x] `GET /api/v1/teams`
  - [x] Cursor pagination with a default `limit`
  - [x] `fields=` parameter for compact responses
  - [x] Freshness metadata: `data_as_of`, `next_update_at`, `source`
  - [x] `/results` kept for backward compatibility
- [x] **10. OpenAPI polish**
  - [x] Explicit `operation_id` on every endpoint
  - [x] Tags and summaries that say when to use each endpoint
  - [x] Parameter descriptions and examples
  - [x] Documented error responses
- [x] **11. Errors, rate limits and caching**
  - [x] RFC 9457 `application/problem+json` with `hint` and `valid_values`
  - [x] Rate limiting with `RateLimit` and `Retry-After` headers
  - [x] `ETag` / `Cache-Control` on read endpoints
- [x] **12. `/llms.txt` and `/agents` page**
  - [x] Generated from the OpenAPI spec and competition config (`app/agent_docs.py`; also
        `/agents.md`, `/llms-full.txt`, and `/problems/<slug>` pages)
  - [x] Purpose, base URL (`FOOTBALL_RESULTS_PUBLIC_BASE_URL`), auth, top 5 tasks with curl
        examples. CLI examples are deferred: step 13 adds them.
  - [x] Links to `/openapi.json`. The CLI and the skill are shown as "planned" until steps 13–14
        pass `ToolLink`s to `build_guide` and add the CLI examples.
- [x] **13. CLI (`football-results`)**
  - [x] Typer app over the HTTP API, installable with `uvx`/`pipx` (separate `cli/` workspace
        package `football-results-cli`, httpx + typer only; install from a checkout until published)
  - [x] Commands: `competitions`, `matches`, `standings`, `teams`, `refresh`, `status` (plus
        `health` and `fetch-season`, so every non-deprecated operation has a command)
  - [x] JSON when piped, a table on a TTY, `--json` and `--fields` (also `--all`, `--fail-if-stale`,
        and `--wait`)
  - [x] Stable exit codes (0 ok, 2 usage, 3 not found, 4 upstream/stale, 5 auth)
  - [x] Errors as JSON on stderr (server problems unchanged; CLI problems as
        `urn:football-results:cli:<slug>`)
  - [x] Configured through `FOOTBALL_RESULTS_URL` and `FOOTBALL_RESULTS_API_KEY`
  - [x] `--help` with examples on every command
  - [x] CLI examples and a CLI `ToolLink` in `/llms.txt` and `/agents` (deferred from step 12)
- [x] **14. Consumer Agent Skill**
  - [x] `skills/football-results/SKILL.md` teaching the CLI (when to use, recipes, nulls, freshness)
  - [x] Served at `/skills/football-results/SKILL.md`
  - [x] Install instructions (`copilot skill add <url>`, HTTPS only; curl into a skills directory
        otherwise) on `/agents#skill` and in the README (a skill `ToolLink` replaces the step 12
        "planned" note)
- [ ] **15. Curated MCP server**
  - [x] FastMCP mounted at `/mcp` (the `mcp` SDK's `MCPServer`, stateless Streamable HTTP route)
  - [x] Read-only tools: `list_competitions`, `find_matches`, `get_standings`, `get_team_form`
  - [x] Optional authenticated `request_refresh`
  - [x] Structured output schemas
  - [ ] Published to the MCP Registry (prepared and published by item 19)

### Phase 3: Operations and growth

- [x] **16. Observability**
  - [x] `/metrics` (Prometheus)
  - [x] Admin status endpoints (recent `scrape_run` rows, scheduler state)
  - [x] Alerts when data goes stale (Prometheus + Grafana OSS profile, Telegram contact point)
  - [x] Structured JSON logs
- [ ] **17. More providers and fallback sources** (use the `add-data-provider` skill)
  - [x] 17a. ESPN provider as the Premier League primary source
        ([ADR 0001](adr/0001-espn-primary-source-for-premier-league.md))
    - [x] `espn` provider: full season via calendar-year scoreboard requests filtered by season;
          goals, half-time split, cards, fouls, corners, first goal; season discovery
    - [x] Status mapping: full time → `finished`; postponed, cancelled, abandoned, suspended →
          `inactive`; live → `scheduled`
    - [x] Cards are `0` without card events only when the goal events add up to the score
    - [x] Source URL allow-list
          `https://site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard?season=<start year>`
    - [x] Throttle with `FOOTBALL_RESULTS_ESPN_MIN_REQUEST_INTERVAL_SECONDS` (default 2s)
    - [x] Per-competition `team_aliases` in `competitions.yaml`, applied before persisting
    - [x] Match IDs survive a provider switch (fallback key: season + home + away when unique);
          a refresh without a round keeps the stored round and stage
    - [x] Premier League switched to `espn` for 2024–25, 2025–26, and 2026–27; documented one-off
          historical re-fetch of the past seasons
    - [x] football-data: count `YELLOW_RED` as two cards; `PROVIDERS.md` notes goals and bookings
          need a paid plan
    - [x] Docs (README with ESPN terms caveat, `PROVIDERS.md`); version 0.17.0
  - [x] 17b. Fallback sources (football-data.org as the Premier League fallback source;
        [ADR 0002](adr/0002-fallback-source-refreshes.md))
    - [x] Config: competition `fallback_provider`, season `fallback_url`, validated by
          `config check`; missing token is a warning, not an error
    - [x] `fallback_after_failures` (default 3) in `defaults.schedule` and `SchedulePolicy`;
          primary failure = error or empty result for a season with stored matches
    - [x] Scheduler: primary first, then fallback in the same run after the threshold; scheduled
          current seasons only; first primary success switches back
    - [x] Fallback merge updates fixture fields and adds matches only; keeps provider IDs, rounds,
          statistics; never deletes
    - [x] Migration 0011: season fallback source, primary-failure counter, last-success source;
          `scrape_run.provider` and `fallback`
    - [x] `freshness.source` = last successful import; admin fields; metrics
          `season_fallback_active`, `season_primary_consecutive_failures`; 24 h Grafana warning
    - [x] Season check discovers missing fallback URLs
    - [x] Docs (README, `PROVIDERS.md`, ADR 0002, glossary); version 0.18.0
  - [x] 17c. Fallback source for the 90minut competitions
        ([ADR 0003](adr/0003-thesportsdb-windowed-fallback-for-polish-competitions.md))
    - [x] 17c.0 ESPN (`uefa.europa`) as the Europa League fallback: `fallback_provider`,
          `fallback_url` for 2026-2027, `team_aliases` for the 2026-27 league-phase teams only
    - [x] `thesportsdb` provider: documented `eventsday.php?d=<date>&l=<league>` over a window
          (`providers.thesportsdb.window_days_back`/`window_days_ahead`, default 7/14); status
          mapping (finished, scheduled, postponed/cancelled → `inactive`, live → `scheduled`);
          throttled within the 30 requests/minute free limit
    - [x] Windowed source capability; `config check` rejects a windowed provider as `provider`
    - [x] Free key `123` by default, optional `FOOTBALL_RESULTS_THESPORTSDB_API_KEY`; key-free
          source URL allow-list `https://www.thesportsdb.com/league/<id>?season=<YYYY-YYYY>`
    - [x] `discover_seasons` via `search_all_seasons.php?id=<league>` (fills missing fallback URLs)
    - [x] Fallback refreshes (all providers, ADR 0002 amendment): skip matches with teams unknown
          in the season and log a warning; an empty windowed result is a success with no changes;
          next check from stored matches after the merge
    - [x] Migration: `scrape_run.matches_skipped`, shown in the admin endpoints
    - [x] Ekstraklasa (4422) and I liga (4661): `fallback_provider: thesportsdb`, `fallback_url`
          for 2026-2027, `team_aliases` for all teams
    - [x] Docs (README, `PROVIDERS.md`, ADR 0003, glossary); version 0.19.0
  - [ ] 17d. More primary providers, each tied to a named competition that no current provider
        covers
    - [x] 17d.1 3. Liga (Germany) via OpenLigaDB
          ([ADR 0004](adr/0004-openligadb-primary-source-for-3-liga.md))
      - [x] `openligadb` provider: one documented `getmatchdata/<shortcut>/<start year>` request
            per season (whole season, no key); `matchDateTimeUTC` kickoff; `round` from
            `group.groupOrderID`; `provider_match_id` = `matchID`; scores by `resultTypeID`
            (2 = full time, 1 = half time), never by array order
      - [x] Status: `matchIsFinished` with a full-time result → `finished`; an unfinished match
            more than `providers.openligadb.postponed_after_hours` (default 24) past kickoff →
            `inactive` (OpenLigaDB has no postponed status, see ADR 0004); otherwise
            `scheduled`; a finished match without a full-time result stays unfinished with a
            warning
      - [x] Statistics: goals, half-time split, first goal team and minute; the first goal only
            when the goal list adds up to the full-time score (else `null`); own goals count for
            the team whose score they raised (from the score change, not `scoringTeamId`);
            cards, corners, and fouls always `null`
      - [x] `discover_seasons` via `getavailableleagues`, filtered by the competition's shortcut,
            newest first; `split` seasons map `2026-2027` ↔ `2026`
      - [x] Source URL allow-list `https://api.openligadb.de/getmatchdata/<shortcut>/<start year>`
            (`<shortcut>` lowercase letters and digits, e.g. `bl3`); all seasons of one
            competition use the same shortcut (`config check` and historical fetch reject mixed
            ones)
      - [x] Throttle with `providers.openligadb.min_request_interval_seconds` (default 2) and
            `FOOTBALL_RESULTS_OPENLIGADB_MIN_REQUEST_INTERVAL_SECONDS`; `postponed_after_hours`
            accepted only for `openligadb` (`config check`)
      - [x] Tests (`tests/test_openligadb_scraper.py`, inline fixtures): finished match with
            goals, scheduled match, postponed heuristic, goal list that does not add up, own goal,
            missing half-time result stays `null`, malformed payload raises, discovery, URL
            accept/reject, statistics within capabilities
      - [x] `config/competitions.yaml`: `3-liga` (3. Liga, Germany, league, `openligadb`, split,
            start month 7), seasons 2025-2026 and 2026-2027 (current), no fallback
      - [x] ODbL attribution for OpenLigaDB: README, `PROVIDERS.md`, and a generated note in
            `/llms.txt` and `/agents` when a configured competition uses `openligadb`; API
            contract unchanged
      - [x] Docs (README supported competitions, statistics coverage, configuration;
            `PROVIDERS.md` capability and URL tables; ADR 0004); version 0.20.0
            (`pyproject.toml`, `cli/pyproject.toml`, `server.json`, `uv lock`)
- [x] **18. Optional hosted access**
      ([research](research/2026-11-hosted-access.md); everything stays optional and free to
      self-host)
  - [x] 18a. Named API keys, roles, and per-key quotas
        ([ADR 0005](adr/0005-named-api-keys-with-roles-and-quotas.md))
    - [x] Keys file `FOOTBALL_RESULTS_API_KEYS_FILE` (YAML, optional): `keys[]` with `name`
          (`[a-z0-9-]`, unique, not `env` or `anonymous`), `hash` (`sha256:<64 hex>`; plain keys
          rejected), `role` (`client` | `admin`), optional `read_per_minute` and
          `write_per_minute` (admin only); validated at startup and by `python -m app.config check`
    - [x] `python -m app.config hash-key --name <name> [--role client|admin]` prints a new random
          key (32 bytes) and its YAML entry
    - [x] Key resolution: SHA-256 of the supplied key compared in constant time with every hash;
          `FOOTBALL_RESULTS_API_KEYS` keys are `admin` keys named `env`; both sources combine
    - [x] Roles: every currently protected operation (refresh, history fetch, jobs, admin,
          `/metrics`) needs `admin`; a valid `client` key gets the new problem `forbidden` (403)
          in `PROBLEM_TYPES`, `PROBLEM_GUIDANCE`, and each route's `responses=`; a missing or
          wrong key stays `unauthorized`; `writes-disabled` only when no admin key exists
    - [x] Per-key quotas: keyed clients are counted per key name with their own read and write
          quotas (defaults: `FOOTBALL_RESULTS_RATE_LIMIT_*_PER_MINUTE`); anonymous clients stay
          per IP; `RateLimit-Policy` `q` shows the client's quota (draft -11 syntax unchanged);
          `rate-limited` (429) unchanged
    - [x] MCP: a key on `/mcp` sets the quota of the `/mcp` read count; `request_refresh` with a
          `client` key returns `forbidden` as an `isError` tool result
    - [x] Metrics: `football_results_keyed_requests_total{key,policy}` and
          `football_results_rate_limited_total{key,policy}` (`key` = name, `env`, or `anonymous`;
          never the key itself)
    - [x] CLI sends `FOOTBALL_RESULTS_API_KEY` on every request when set (AGENTS.md rule updated);
          `forbidden` maps to exit code 5; consumer skill wording updated
    - [x] Agent docs: `/llms.txt` and `/agents` describe roles, keyed quotas, and `forbidden`
    - [x] Compose passes `FOOTBALL_RESULTS_API_KEYS_FILE` and mounts `./secrets` read-only
          (`secrets/` git-ignored); `.env.example` updated
    - [x] Docs (README errors, rate limits, configuration, MCP, deployment; ADR 0005; glossary);
          version 0.21.0 (`pyproject.toml`, `cli/pyproject.toml`, `server.json`, `uv lock`)
  - [x] 18b. Optional OAuth for `/mcp`
        ([ADR 0006](adr/0006-optional-oauth-resource-server-for-mcp.md))
    - [x] Spike: check OAuth on remote HTTP servers in Copilot CLI, VS Code, and Claude Code
          (CIMD, DCR, or pre-registered client) against Keycloak; add the results to the
          research note; stop and re-plan if no client works
    - [x] Settings: `FOOTBALL_RESULTS_OAUTH_ISSUER` (enables OAuth), `..._OAUTH_AUDIENCE`
          (default `<public base URL>/mcp`), `..._OAUTH_MODE` (`optional` | `required`, default
          `optional`), `..._OAUTH_SCOPES`; an issuer requires `FOOTBALL_RESULTS_PUBLIC_BASE_URL`
    - [x] `GET /.well-known/oauth-protected-resource/mcp` (RFC 9728: `resource`,
          `authorization_servers`, `bearer_methods_supported`, `scopes_supported`); not an OpenAPI
          operation, rate-limit exempt, cacheable
    - [x] Bearer check in `McpEndpoint` (PyJWT, JWKS cached and refetched on an unknown `kid`,
          asymmetric algorithms only; `iss`, `aud`, `exp`, `nbf`): an invalid token → 401 with
          `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="..."`; `required`
          mode: anonymous → 401 with `resource_metadata`
    - [x] Identity: a keys-file entry has either `hash` or `subject`; a mapped subject gets its
          name, role, and quotas; an unknown valid subject is a `client` with default keyed
          quotas (metric `key` = `oauth`); X-API-Key keeps working on `/mcp`
    - [x] Inner tool calls carry the resolved identity in the ASGI scope, never the token, so an
          admin subject can `request_refresh`
    - [x] Optional `oauth-example` Compose profile (Keycloak, realm import without committed
          secrets) and a README walkthrough
    - [x] `/agents#mcp` and `/llms.txt` mention OAuth when an issuer is set; `server.json`
          unchanged (clients discover OAuth at runtime)
    - [x] Tests with locally generated keys and JWKS (no network): valid, expired, wrong
          audience, wrong issuer, unknown `kid`, `required` mode, mapped admin refresh
    - [x] Docs (README MCP and configuration; ADR 0006); version 0.22.0 (`pyproject.toml`,
          `cli/pyproject.toml`, `server.json`, `uv lock`)
  - [x] 18c. Public demo instance
        ([ADR 0007](adr/0007-read-only-demo-instance-serves-only-redistributable-sources.md))
    - [x] `FOOTBALL_RESULTS_DEMO_MODE`: startup and `config check` reject admin keys and
          competitions whose primary or fallback provider is not publicly redistributable (allow
          list: `openligadb`)
    - [x] `config/demo-competitions.yaml`: the 3. Liga only
    - [x] Demo notice in `/llms.txt` and `/agents`: read-only, no SLA, data licences and
          attribution, quotas
    - [x] `compose.demo.yaml` override: demo competitions, demo mode, low anonymous quotas (for
          example 30 reads per minute), JSON logs
    - [x] `docs/DEMO.md`: Oracle Cloud Always Free VM, Caddy, Cloudflare free in front (cache rule
          for public GETs, IP rate-limit rule, Cloudflare ranges in
          `FOOTBALL_RESULTS_TRUSTED_PROXIES`), and how to add a provider after written
          permission (90minut.pl)
    - [x] Tests: demo-mode validation, notice rendering
    - [x] Docs (README, ADR 0007, glossary); version 0.23.0 (`pyproject.toml`,
          `cli/pyproject.toml`, `server.json`, `uv lock`)
  - [x] 18d. Private instance with expiring named keys
        ([ADR 0008](adr/0008-private-instances-require-expiring-named-keys.md))
    - [x] `FOOTBALL_RESULTS_ANONYMOUS_ACCESS` (default `true`). When `false`, every data
          operation, `/mcp` (as OAuth `required`), and `/metrics` need a valid key or token.
          `/health`, agent docs, `/problems/*`, `/skills/...`, `/openapi.json`, `/docs`, and OAuth
          metadata stay public.
    - [x] Private-instance startup and `config check`: reject `FOOTBALL_RESULTS_API_KEYS` and a
          non-`https` `FOOTBALL_RESULTS_PUBLIC_BASE_URL` (localhost excepted)
    - [x] Keys file: required `issued_at` and `expires_at` on `hash` entries (not on `subject`);
          `expires_at` at most 365 days after `issued_at`; expired keys get `unauthorized`;
          entries without expiry stop startup with the fix in the message
    - [x] Reload the keys file on change (mtime check every few seconds); keep the last valid keys
          and log an error for an invalid file; log key names added, removed, or changed
    - [x] `python -m app.config keys issue|extend|revoke|list`: `issue --name --role --days`
          (default 30) writes the entry and prints a `frk_` key once; `extend` refuses beyond the
          cap; `list` and `config check` flag keys expiring within 14 days. `hash-key` stays.
    - [x] Every `401` counts against the caller's IP read quota
    - [x] `Cache-Control: private` on cacheable reads when anonymous access is off (`ETag`/`304`
          kept)
    - [x] Expiry signals: metric `football_results_api_key_expires_in_seconds{key}`, a Grafana
          alert 7 days before expiry, and an expiry response header for keyed clients (also in the
          CLI's stderr notes)
    - [x] Agent docs say a key is required and how to ask the operator when anonymous access is
          off
    - [x] `docs/PRIVATE.md`: one worker, Caddy HTTPS, no CDN cache rule, keys file permissions,
          issuing and sending keys, rotation (`alice-2027`), revocation, provider permissions
    - [x] Tests: access switch, public pages, expiry and cap, reload, `keys` commands, `private`
          caching, `401` counting
    - [x] Docs (README, ADR 0008, glossary, AGENTS.md); version 0.24.0 (`pyproject.toml`,
          `cli/pyproject.toml`, `server.json`, `uv lock`)

### Phase 4: Public launch

Decided on 2026-11-25. Put a demo instance and private instances on the internet ready for AI
agents. Coding agents (CLI + skill) and chat clients (MCP on the demo, anonymously) are both
first-class. Publishing needs a GitHub account switch, so the repository only **prepares** it, and
the maintainer runs `scripts/publish-wizard.sh`.

- [x] **19. Launch readiness**
  - [x] 19a. Crawlers and agents: `/robots.txt` (allow everything and point to `/llms.txt`
        when anonymous access is on; disallow everything on a private instance); `docs/DEMO.md`
        Cloudflare settings that keep AI-bot blocking, Bot Fight Mode, and managed `robots.txt`
        off
  - [x] 19b. Privacy: `/privacy` page (what is processed, why, and for how long; operator
        contact), linked from `/llms.txt`, `/agents`, and the README; client IPs anonymised in
        access logs in demo mode; Docker log rotation in `compose.demo.yaml`
  - [x] 19c. CLI default URL ([ADR 0009](adr/0009-published-cli-defaults-to-the-demo-instance.md)):
        `DEMO_URL` placeholder; a failed connection to the unset default explains
        `FOOTBALL_RESULTS_URL`; a key needs an explicit URL when the default is the demo
  - [x] 19d. Publishing prep (nothing is published here): MIT `LICENSE`; repository renamed to
        `ReccaFlames/football-results-api` in docs and metadata; PyPI metadata for
        `football-results-cli`; `server.json` named `io.github.ReccaFlames/football-results`
        with the demo remote first and the `{host}` template second;
        `scripts/set_demo_host.py`; dormant tag-triggered release workflow (checks, PyPI trusted
        publishing, `mcp-publisher` with GitHub OIDC; fails on placeholders);
        `scripts/publish-wizard.sh` for the manual steps
  - [x] 19e. Agent evals in `evals/` (never in pytest or CI): about 10 tasks (top 5, error
        recovery, freshness, pagination) through the CLI + skill and through MCP, run with
        `copilot -p` against a local demo-mode stack, ground truth read from the API at run
        time, 3 runs each, a task passes at 2 of 3; manual checklist for claude.ai and ChatGPT
  - [x] 19f. Docs (README, AGENTS.md, DEMO.md); version 0.25.0. Tag 1.0.0 at the real publish
        (it declares the public contract stable)
- [ ] **21. Whistleprint landing page and access requests** (decided 2026-11-26, do before the
      server push; [ADR 0010](adr/0010-access-requests-are-owned-by-the-api-and-never-auto-approved.md),
      glossary: *Landing page*, *Access request*)
  - [x] 21a. API: `access_request` table (Alembic, `database-migration` skill); public operations
        `create_access_request` (`POST /api/v1/access-requests`) and `get_access_request`
        (`GET /api/v1/access-requests/{id}`, needs the one-time claim secret, constant-time check;
        returns status and, once approved, the key exactly once); admin operations
        `list_access_requests` and `reject_access_request` (not MCP tools). Fields: required
        `name`, `contact_email`, `intended_use` (≤1000), `client_type` (`human`|`agent`);
        optional `competitions` (validated slugs), `message` (≤2000); honeypot field.
        `create_access_request` and `list_competitions` join `PUBLIC_OPERATIONS`; on the demo
        `create_access_request` returns `writes-disabled`. Limit 3 per hour per IP; a pending
        request with the same email → new problem slug `access-request-pending` (with
        `PROBLEM_GUIDANCE`)
    - [x] Storage: migration `0013_access_requests` (one pending request per lower-cased email,
          plaintext key only while `approved`, personal data erased on claim or expiry) and
          repository methods (create, get, list, approve, reject, claim, purge) with
          integration tests
    - [x] Public operations `create_access_request` and `get_access_request` (+ CLI commands,
          required by the CLI/OpenAPI parity test). The claim secret travels in the
          `Claim-Secret` header (never the query string, which access logs record); the path
          parameter is `access_request_id`. The honeypot moved to the landing app (21e): an
          API field would only confuse agents
    - [x] Admin operations `list_access_requests` (`GET /api/v1/admin/access-requests`) and
          `reject_access_request` (`POST /api/v1/admin/access-requests/{access_request_id}/reject`,
          `409 access-request-decided` unless pending) (+ CLI commands)
  - [ ] 21b. Telegram notification on each new request (ID, name, client type, intended use,
        competitions; never the email): optional `FOOTBALL_RESULTS_TELEGRAM_BOT_TOKEN` and
        `FOOTBALL_RESULTS_TELEGRAM_CHAT_ID`; without them only a log line and a metric
  - [ ] 21c. Server command `python -m app.config access-requests approve <id>` (role `client`
        only, default quotas, 365-day expiry, name `req-<id>-<slug of name>`, flags override)
        using the existing keys-file helpers; plus `list` and `reject`. The key must be claimed
        within 14 days; after claim or claim expiry the plaintext key and the request's personal
        data are deleted. Unapproved (pending/rejected) requests are deleted after 30 days
  - [ ] 21d. CLI commands `create-access-request` and `get-access-request` (and, because every
        operation needs a command, `list-access-requests` and `reject-access-request`); one line
        in the consumer skill pointing to `/llms.txt` for requesting access (no instance facts);
        no MCP tool
  - [ ] 21e. Landing app: new uv workspace package `web/` (`football-results-web`, Starlette +
        Jinja2 + httpx; must not import `app`, talks to the API over HTTP like the CLI). Brand
        "Whistleprint" in the UI only (technical names stay `football-results`). Move `images/`
        (logo, mark, favicon) to `web/static/`. Server-rendered, no JS required, semantic HTML,
        single column (~40rem), system fonts, `prefers-color-scheme` light/dark, no external
        assets. Sections: logo + one-line pitch; competitions (name, country, current season) from
        `list_competitions`, cached 5 min (if never fetched: "temporarily unavailable" + link to
        `/llms.txt`); developers (`/docs`, `/openapi.json`, CLI install); agents (`/llms.txt`,
        `/agents`, `/mcp`, skill); private: access request form (plain HTML POST to
        `/request-access`, confirmation page shows the request ID and claim secret once, API
        problems shown readably; a hidden honeypot field the app drops silently) / demo: "no key needed"; footer: `/privacy`, provider
        attributions (OpenLigaDB ODbL), operator contact. Relative links only. Forwards
        `X-Forwarded-For`; listed in `FOOTBALL_RESULTS_TRUSTED_PROXIES`. Stays public on a private
        instance
  - [ ] 21f. Deployment: own Docker image and Compose service; Caddy routes `/`,
        `/request-access`, `/static/*`, `/favicon.ico` to the landing app and everything else to
        the API (the API keeps no `/` route); `docs/DEPLOYMENT.md`, `docs/PRIVATE.md`,
        `docs/DEMO.md`
  - [ ] 21g. `/privacy`: access request data (name, email, use, message), Telegram as a
        processor, 30-day / claim-window retention (`privacy_blocks` in `app/agent_docs.py`)
  - [ ] 21h. Tests (API, CLI, landing app with a fake API; no network), docs (README, AGENTS.md
        layout table, ADR 0010), bump version in `pyproject.toml`, `cli/pyproject.toml`,
        `server.json`, `uv lock`
- [ ] **20. OAuth for chat clients on private instances**: claude.ai and ChatGPT connectors cannot
      send `X-API-Key`, so a private instance needs a production authorization server (ADR 0006)
      before they can connect. Until then, chat clients are supported on the demo only.
