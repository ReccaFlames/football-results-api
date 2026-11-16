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
  - Build with FastMCP, mounted in FastAPI.
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
- [ ] **7. Generic seasons and providers**
  - [ ] Allow calendar-year seasons (`2026`) alongside `2026-2027`
  - [ ] Season-to-date mapping per competition (no hard-coded July start)
  - [ ] football-data.org adapter and URL validation for any competition code
  - [ ] Written provider contract (capabilities, `discover_seasons`)
- [ ] **8. Scheduler settings per competition**
  - [ ] Scheduler thresholds read from the configuration file
  - [ ] Automatic season discovery and rollover

### Phase 2: Agent-first API surface

- [ ] **9. API contract v1.1**
  - [ ] `GET /api/v1/matches` (status, date range, team, round/stage filters)
  - [ ] `GET /api/v1/standings`
  - [ ] `GET /api/v1/teams`
  - [ ] Cursor pagination with a default `limit`
  - [ ] `fields=` parameter for compact responses
  - [ ] Freshness metadata: `data_as_of`, `next_update_at`, `source`
  - [ ] `/results` kept for backward compatibility
- [ ] **10. OpenAPI polish**
  - [ ] Explicit `operation_id` on every endpoint
  - [ ] Tags and summaries that say when to use each endpoint
  - [ ] Parameter descriptions and examples
  - [ ] Documented error responses
- [ ] **11. Errors, rate limits and caching**
  - [ ] RFC 9457 `application/problem+json` with `hint` and `valid_values`
  - [ ] Rate limiting with `RateLimit` and `Retry-After` headers
  - [ ] `ETag` / `Cache-Control` on read endpoints
- [ ] **12. `/llms.txt` and `/agents` page**
  - [ ] Generated from the OpenAPI spec and competition config
  - [ ] Purpose, base URL, auth, top 5 tasks with curl and CLI examples
  - [ ] Links to `/openapi.json`, the CLI and the skill
- [ ] **13. CLI (`football-results`)**
  - [ ] Typer app over the HTTP API, installable with `uvx`/`pipx`
  - [ ] Commands: `competitions`, `matches`, `standings`, `teams`, `refresh`, `status`
  - [ ] JSON when piped, a table on a TTY, `--json` and `--fields`
  - [ ] Stable exit codes (0 ok, 2 usage, 3 not found, 4 upstream/stale, 5 auth)
  - [ ] Errors as JSON on stderr
  - [ ] Configured through `FOOTBALL_RESULTS_URL` and `FOOTBALL_RESULTS_API_KEY`
  - [ ] `--help` with examples on every command
- [ ] **14. Consumer Agent Skill**
  - [ ] `skills/football-results/SKILL.md` teaching the CLI (when to use, recipes, nulls, freshness)
  - [ ] Served at `/skills/football-results/SKILL.md`
  - [ ] Install instructions (`copilot skill add <url>`) on `/agents` and in the README
- [ ] **15. Curated MCP server**
  - [ ] FastMCP mounted at `/mcp`
  - [ ] Read-only tools: `list_competitions`, `find_matches`, `get_standings`, `get_team_form`
  - [ ] Optional authenticated `request_refresh`
  - [ ] Structured output schemas
  - [ ] Published to the MCP Registry

### Phase 3: Operations and growth

- [ ] **16. Observability**
  - [ ] `/metrics` (Prometheus)
  - [ ] Admin status endpoints (recent `scrape_run` rows, scheduler state)
  - [ ] Alerts when data goes stale
  - [ ] Structured JSON logs
- [ ] **17. More providers and fallback sources** (use the `add-data-provider` skill)
- [ ] **18. Optional hosted access**
  - [ ] OAuth for remote MCP
  - [ ] Per-key quotas
  - [ ] Public demo instance
