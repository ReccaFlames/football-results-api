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
   - There is no CI.
9. **Housekeeping:**
   - The version is set in two places, and the User-Agent still says 0.11.
   - `.idea/` is committed.
   - The Dockerfile uses `pip install .` and ignores `uv.lock`.
   - Some defaults are still Ekstraklasa-specific (`RefreshRequest.competition`,
     `MatchResult.competition`).
   - `list_competitions` raises if a competition has no current season.
   - The 90minut detail cache is an unbounded in-memory dict.
   - One file fails `ruff format --check`.

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

## Ordered backlog

The reasoning behind the order: every agent-facing surface (CLI, skill, MCP, llms.txt) copies the
API contract. Fix the contract and configuration first, or every surface will need to be rewritten.

### Phase 0: Foundations

1. **CI and Postgres integration tests.** Add a GitHub Actions workflow that runs pytest, ruff and
   `alembic upgrade --sql`. Add repository tests against a real PostgreSQL (service container or
   testcontainers) that cover seeding, claiming due seasons and replacing matches.
2. **Housekeeping.**
   - Keep the version in one place.
   - Untrack `.idea/`.
   - Build the Docker image from `uv.lock`.
   - Run `ruff format`.
   - Remove the Ekstraklasa defaults.
   - Handle the no-current-season case.
3. **Safe write endpoints.**
   - Require an API key (`X-API-Key`) for refresh and history fetch.
   - Make refresh asynchronous: queue the season for the scheduler and return `202` with a job or
     status URL. Do not scrape inside the request.
4. **Polite and resilient scraping.**
   - Throttle requests per host for 90minut.
   - Make detail-page failures affect only that match (leave its fields `null` and log a warning).
   - Bound the detail cache, or persist it.

### Phase 1: Generic and configurable (the README target state)

5. **Configuration file.** Add `config/competitions.yaml` with Pydantic validation and a
   `check` command.
   - Seed only on startup or when the file changes, not every cycle.
   - Competitions removed from the file are disabled, not deleted.
6. **Stable data model.**
   - Upsert matches by provider match ID (or a key that ignores kickoff time) instead of
     deleting and re-inserting.
   - Expose a stable match `id`.
   - Add a `team` table with canonical slugs and aliases.
7. **Generic seasons and providers.**
   - Allow calendar-year seasons (`2026`) alongside `2026-2027`.
   - Generalise football-data.org to any competition code.
   - Write down the provider contract.
8. **Per-competition scheduler settings.** Make the thresholds configurable. Add automatic season
   discovery and rollover.

### Phase 2: Agent-first API surface

9. **API contract v1.1.**
   - `GET /matches` (finished and scheduled, filtered by status, date range, team ID).
   - `GET /standings`.
   - `GET /teams`.
   - Cursor pagination with a default `limit`.
   - A `fields=` parameter.
   - Freshness metadata: `data_as_of`, `next_update_at`, `source`.
   - Keep `/results` for backward compatibility.
10. **OpenAPI polish.**
    - Explicit `operation_id`s, tags and summaries that say when to use each endpoint.
    - Parameter descriptions with examples, and documented error responses.
    - This is the input for every later step.
11. **RFC 9457 errors** with `type`, `title`, `detail`, and extension members such as
    `valid_values` (competitions, seasons) and `hint`. Add `RateLimit` and `Retry-After` headers
    and ETag caching.
12. **`/llms.txt` and `/agents` static page.** Generate both from the OpenAPI spec and the
    competition config: purpose, base URL, auth, the 5 most common tasks with curl and CLI
    examples, and links to `/openapi.json`, the CLI and the skill.
13. **CLI** (`football-results`, Typer, installable with `uvx`/`pipx`, using the HTTP API):
    - Commands: `competitions`, `matches`, `standings`, `teams`, `refresh`, `status`.
    - JSON when piped, a table on a TTY, `--json` and `--fields`.
    - Exit codes: 0 ok, 2 usage error, 3 not found, 4 upstream or stale, 5 auth.
    - Errors as JSON on stderr.
    - Configured through `FOOTBALL_RESULTS_URL` and `FOOTBALL_RESULTS_API_KEY`.
14. **Consumer Agent Skill.**
    - A `skills/football-results/SKILL.md` that teaches the CLI: when to use it, common recipes,
      and how to interpret nulls and freshness.
    - Keep it in the repo and serve it at `/skills/football-results/SKILL.md`, so it can be
      installed with `copilot skill add <url>`.
15. **Curated MCP server.**
    - FastMCP, mounted at `/mcp`.
    - Read-only tools: `list_competitions`, `find_matches`, `get_standings`, `get_team_form`.
    - Optional authenticated `request_refresh`.
    - Structured output schemas.
    - Publish it to the MCP Registry once it is stable.

### Phase 3: Operations and growth

16. **Observability.** `/metrics` (Prometheus), admin status endpoints (recent `scrape_run` rows,
    scheduler state), and alerts when data goes stale. Use structured logs.
17. **More providers and fallback sources**, using the `add-data-provider` skill.
18. **Optional hosted access.** OAuth for remote MCP, per-key quotas and a public demo instance.
