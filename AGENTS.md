# AGENTS.md

Guidance for AI coding agents working **on** this repository. For how to *use* the API, see
`README.md`. For the planned direction and the ordered backlog, see `docs/ROADMAP.md`.

## Project in one paragraph

Football Results API (repo: `ReccaFlames/football-results-api`, package: `football-results-api`) imports
fixtures and results from external sources (90minut.pl HTML, football-data.org JSON,
ESPN scoreboard JSON, TheSportsDB JSON, OpenLigaDB JSON), stores them in
PostgreSQL, and serves them through FastAPI. A separate scheduler process refreshes current seasons
adaptively based on kickoff times. Goal: a generic, easy-to-configure, AI-agent-first league data
API.

## Layout

| Path | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app and all HTTP endpoints; module-level `store` singleton |
| `app/api_docs.py` | OpenAPI metadata: tags, shared query parameters, documented error responses |
| `app/contract.py` | API v1.1 request rules: opaque cursors, `fields=`, status and date parsing, limits |
| `app/problems.py` | RFC 9457 problem details: `ProblemError`, exception handlers for every error |
| `app/ratelimit.py` | In-process fixed-window rate limiter, `RateLimit` headers, trusted-proxy client IP |
| `app/access.py` | Named API keys (ADR 0005, 0008): keys-file models with required expiry, SHA-256 hashing, constant-time key resolution to an `Identity` (name, role, quotas, expiry), `KeysFileWatcher` (reload on change), `issue_key`/`extend_key`/`revoke_key` |
| `app/agent_docs.py` | Pure generator of `/llms.txt`, `/agents`, `/agents.md`, `/llms-full.txt`, `/problems/<slug>`, `/robots.txt`, `/privacy` from the OpenAPI spec and competitions; `SKILL_PATH`/`SKILL_FILE` for the served skill; `MCP_TOOLS` (tool → operation) |
| `app/oauth.py` | Optional OAuth for `/mcp` (ADR 0006): `OAuthConfig` (RFC 9728 metadata, `WWW-Authenticate` challenge), `JwksCache`, `JwtVerifier`; `create_http_client` is the test seam |
| `app/mcp_server.py` | Curated MCP server (`mcp` SDK `MCPServer`) at `/mcp`: 5 tools calling the API in-process, output models, Host/Origin checks, `McpRoute` |
| `app/caching.py` | Weak body-hash `ETag`, `If-None-Match`, and `Cache-Control` rules for read endpoints |
| `app/observability.py` | Stale-season and stalled-scheduler rules (`STALE_GRACE`, `stall_window`), Prometheus rendering for `/metrics`, HTTP request metrics |
| `app/logs.py` | `configure_logging` (`FOOTBALL_RESULTS_LOG_FORMAT`: `text` or `json`) for the API and scheduler; access-log IP anonymisation |
| `app/standings.py` | Pure league-table calculation and tie-breakers from finished matches |
| `app/scheduler.py` | Scheduler loop (`python -m app.scheduler`): seed → heartbeat → season checks (discovery/rollover) → claim due seasons → scrape → persist |
| `app/scheduling.py` | Pure rules driven by `SchedulePolicy`: `calculate_next_check`, `failure_backoff`, `rollover_target` |
| `app/scraper.py` | Provider adapters (`NinetyMinutScraper`, `FootballDataScraper`), parsers, `ScraperRegistry` |
| `app/seasons.py` | Season formats (`2026-2027`, `2026`) and season/date mapping |
| `app/competitions.py` | Competition summary helpers and `source_from_url` validation |
| `app/config.py` | Environment settings, validated competition configuration, and `python -m app.config check` |
| `config/competitions.yaml` | Competition, provider, and schedule defaults |
| `config/demo-competitions.yaml` | Demo instance competitions: only redistributable providers (the 3. Liga) |
| `compose.demo.yaml` | Compose override for the public read-only demo (ADR 0007); guide in `docs/DEMO.md` |
| `docs/PRIVATE.md` | Private instance guide (ADR 0008): invited users, expiring keys, `keys` commands |
| `app/repository.py` | `PostgresResultRepository`: seeding, queries, atomic season replace, scheduler claims |
| `app/models.py` | Pydantic API/domain models (`Competition`, `SeasonSource`, `ScrapedMatch`, `MatchResult`, ...) |
| `app/db_models.py` | SQLAlchemy ORM tables |
| `app/config.py` | `Settings` from env vars with prefix `FOOTBALL_RESULTS_` |
| `migrations/` | Alembic migrations (schema changes go here only; no `create_all`) |
| `docs/PROVIDERS.md` | Provider contract: `scrape`, `discover_seasons`, capabilities, URL rules |
| `cli/` | Separate uv workspace package `football-results-cli` (httpx + typer only): the `football-results` CLI |
| `cli/football_results_cli/main.py` | Typer commands, `OPERATIONS` (command → operation ID), `CLI_ONLY_OPTIONS`, JSON usage errors |
| `cli/football_results_cli/client.py` | `Config`, `create_http_client` (test seam), `ApiClient` with GET retries and HTTP → problem mapping |
| `cli/football_results_cli/output.py` | JSON vs Rich tables (TTY detection), freshness footer, staleness rule |
| `cli/football_results_cli/problems.py` | Exit codes, CLI problem slugs (`urn:football-results:cli:<slug>`), `exit_code_for` |
| `skills/football-results/SKILL.md` | Consumer Agent Skill for API users (teaches the CLI), served unchanged at `/skills/football-results/SKILL.md` |
| `server.json` | MCP Registry metadata (`io.github.ReccaFlames/football-results`; demo remote first, `{host}` template second), validated against `tests/schemas/server.schema.json` |
| `scripts/set_demo_host.py` | Replaces the `FOOTBALL_DEMO_HOST` placeholder (server.json) and `DEMO_URL` (CLI) once; `--check` fails while one remains (ADR 0009) |
| `scripts/publish-wizard.sh` | Interactive wizard for the one-time manual publish steps (GitHub repo, PyPI pending publisher, demo host, tag) |
| `.github/dependabot.yml` | Weekly grouped dependency updates (uv, Actions, Docker, Compose) with a 7-day cooldown |
| `.github/workflows/release.yml` | Dormant `v*` tag release: reuses CI, version and placeholder checks, PyPI trusted publishing, MCP Registry via GitHub OIDC |
| `evals/` | Agent evals with `copilot -p` (CLI + skill and MCP) against a local demo stack; manual chat-client checklist. Never in pytest or CI |
| `keycloak/` | Realm import for the `oauth-example` Compose profile (no users, no secrets) |
| `observability/` | Prometheus scrape config and Grafana provisioning (data source, dashboard, alert rules, policy; the Telegram contact point is in `compose.yaml`) for the `observability` Compose profile |
| `GLOSSARY.md` | Domain terms (stale season, stalled scheduler, grace period) |
| `tests/` | pytest; API tests use `FakeRepository` and need no database; `test_cli.py` runs the CLI against the app in-process |
| `tests/integration/` | Repository and migration tests against real PostgreSQL (skipped without `FOOTBALL_RESULTS_TEST_DATABASE_URL`) |

## Commands

~~~bash
uv sync --extra dev                 # install
uv run python -m app.config check   # validate the competition configuration
uv run pytest -q                    # unit tests; integration tests skip without a test DB
uv run ruff check .                 # lint (line length 100)
uv run ruff format --check .        # formatting (enforced in CI)
uv run alembic upgrade head --sql   # validate migrations offline
uv run football-results --help      # CLI from the workspace (FOOTBALL_RESULTS_URL, default :8000)
uvx --from ./cli football-results --help   # CLI install smoke test (not `--from .`)

# PostgreSQL integration tests (repository + migrations), disposable DB on port 55432
docker compose --profile test up -d --wait test-db
FOOTBALL_RESULTS_TEST_DATABASE_URL=postgresql+psycopg_async://football:football@localhost:55432/football_test uv run pytest -q
docker compose --profile test down
docker compose up -d db             # local PostgreSQL
uv run alembic upgrade head && uv run uvicorn app.main:app --reload
uv run python -m app.scheduler      # scheduler, second terminal
~~~

Before declaring a change done, run `ruff check`, `ruff format --check`, and `pytest` **with** the
integration database when touching `app/repository.py`, `app/db_models.py`, or `migrations/`.
CI (`.github/workflows/ci.yml`) runs all of these on every push and pull request.

## Conventions and gotchas

- Python 3.12+, fully async (SQLAlchemy async + httpx). Type hints everywhere; Pydantic v2 models.
- Seasons are strings: `YYYY-YYYY` (split, consecutive years) or `YYYY` (calendar), chosen per
  competition by `season_format`; split seasons start in `season_start_month` (default July). Use
  `app/seasons.py` for validation and date mapping; never assume a July start.
- `config/competitions.yaml` is seeded on API and scheduler startup; the scheduler reseeds only when
  the file changes. Configured competitions overwrite names, sources, provider, and current season.
  Removed competitions are disabled, not deleted, so their stored results remain in the database.
- Scheduler thresholds come from `defaults.schedule` plus a per-competition `schedule` override
  (`CompetitionFile.schedule_for`); never hard-code them. `SchedulePolicy()` must equal the shipped
  defaults (a test checks this).
- With `season_rollover: auto` (default) the configured `current_season` is a minimum: reseeding
  keeps a newer season the scheduler rolled over to. Discovered seasons are never overwritten or
  removed by reseeding; `manual` makes `current_season` exact. Pass `auto_rollover_slugs()` to
  every `seed_competitions` call (API startup and scheduler).
- A refresh reconciles all matches of one competition-season in one transaction and keeps match IDs
  (matched by `provider_match_id`, else a season/round/team key that ignores kickoff time).
  Cursors and clients rely on these IDs; never renumber or re-insert existing matches.
- `MatchResult` (`/results`, frozen for backward compatibility) exposes only finished matches.
  `Match` (`/matches`, API v1.1) has every status (`finished`, `scheduled`, `inactive`) and uses
  `kickoff_at`; `ScrapedMatch` is the internal provider record.
- API v1.1 list endpoints return `{data, pagination, freshness}`. Cursors are opaque keysets on
  `(kickoff_at, id)` (teams: slug) bound to a hash of the filters; `fields=` validates against the
  item model. Freshness is per competition-season, never a maximum across competitions. Later
  surfaces (OpenAPI, llms.txt, CLI, skill, MCP) copy this contract, so change it deliberately.
- Every route declares a snake_case `operation_id`, one tag from `app/api_docs.py`, a `summary`,
  a "when to use" `description`, parameter descriptions, and `responses=` for each error status it
  can return (as `application/problem+json` via `docs.error()`, examples in `app/api_docs.py`).
  Rate-limited operations declare `429`; cacheable reads spread `docs.cached_read(path)`.
  Operation IDs are public names for the CLI, skill, and MCP; never rename one.
  `tests/test_api.py` checks that IDs are unique and that real error bodies are documented
  problems. Routes that return `JSONResponse` still declare `response_model` so the schema is
  documented.
- Errors: raise `ProblemError("<slug>", detail, parameter=..., valid_values=..., hint=...)`, never
  `HTTPException`. Slugs (the `type` URI `/problems/<slug>`) are registered in `PROBLEM_TYPES` in
  `app/models.py` and are public; add new ones, never rename, and give each new slug a meaning
  and fix in `PROBLEM_GUIDANCE` (`app/agent_docs.py`; a test checks). `detail` is always a string.
  Contract parsers raise `ContractError(<slug>, {...})`, converted by `_contract()`.
- The `rate_limit_and_cache` middleware in `app/main.py` applies rate limits (exempt:
  `EXEMPT_PATHS`), adds `ETag`/`304` to operations in `CACHEABLE_OPERATIONS`, and sets
  `Cache-Control: no-store` on errors and on every other API operation. Routes with freshness set
  their own `Cache-Control` from `next_update_at`. Tests get a fresh limiter per test.
- Agent docs (`/llms.txt`, `/agents`, ...) are generated from `app.openapi()`, so new operations,
  parameters, and documented problems appear automatically. The doc routes use
  `include_in_schema=False` and no operation ID; they get `ETag` through `CACHEABLE_PAGES`. Top-5
  task examples are validated against the spec (`top_tasks` raises on unknown parameters) and,
  as CLI commands, against the CLI (`tests/test_agent_docs.py`). `_agent_guide` passes
  `cli_tool()` and `skill_tool()`, which add the Tools links and the `#cli` and `#skill` sections.
- The MCP server (`/mcp`, `app/mcp_server.py`) is stateless Streamable HTTP and not an OpenAPI
  operation. Tools call the HTTP routes in-process (httpx `ASGITransport`), never the repository,
  so errors are the API's problems (`isError: true`) and results equal the HTTP bodies. Tool names
  (`MCP_TOOLS` in `app/agent_docs.py`) are public: add, never rename. Keep tool descriptions within
  `DESCRIPTION_BUDGET` (a test checks) and every parameter described. Inner reads are not
  rate-limited again; inner writes count against the outer client's write quota. `server.json`
  `version` must equal `pyproject.toml` (a test checks). MCP tests need
  `with TestClient(app, base_url="http://localhost")` so the lifespan starts the session manager.
- OAuth (ADR 0006) covers `/mcp` only; the API is a resource server, never an authorization
  server, and does not use the SDK's `auth=`/`token_verifier=`. `McpEndpoint` resolves the caller
  once per request (`AUTH_SCOPE_KEY`, also used for the rate-limit client) and passes the identity
  to inner calls in `IDENTITY_SCOPE_KEY`; never forward the token. A valid `X-API-Key` wins over a
  token. Tests generate keys locally and patch `app.oauth.create_http_client`.
- Two kinds of skills: `.github/skills/` holds *contributor* skills for working on this repo;
  `skills/football-results/SKILL.md` is the *consumer* skill for people and agents using the API.
  The consumer skill is a static, single file (`copilot skill add <url>` installs one file) that
  reads the API URL from `FOOTBALL_RESULTS_URL`. It must match the CLI: `tests/test_skill.py` runs
  every `football-results` line in its code blocks, and checks options, exit codes, problem slugs,
  `$FOOTBALL_RESULTS_URL/...` links, and the agentskills.io frontmatter. Keep instance facts
  (competition lists, rate limits, thresholds, defaults) out of it; link to `/llms.txt` instead.
  The Docker image copies `skills/`.
- The CLI (`cli/`) is a separate package: the API must not import it, and it must not import
  `app`. `CLI_NAME`, `CLI_COMMANDS`, `CLI_EXIT_CODES`, and `CLI_PROBLEM_GUIDANCE` in
  `app/agent_docs.py` mirror the CLI; tests check both sides. Command names, option names, exit
  codes (0 ok, 2 usage, 3 not found, 4 upstream/stale, 5 auth), and CLI problem slugs are public:
  add, never rename or renumber. A new API operation or parameter needs a CLI command/option with
  the same name (`--date-from` = `date_from`); a test compares commands with the OpenAPI spec.
- CLI output: stdout is only data (the API body unchanged as JSON, or a table on a TTY); notes,
  freshness, and errors go to stderr, errors as one-line problem JSON. Server problems are passed
  through unchanged. `FOOTBALL_RESULTS_URL`, `FOOTBALL_RESULTS_API_KEY`, and
  `FOOTBALL_RESULTS_TIMEOUT` belong to the CLI; never add `url`, `api_key`, or `timeout` fields to
  the server `Settings` (a test checks). The CLI sends the API key on every request (it selects
  the caller's quota, ADR 0005), only to the configured URL, and never prints it.
- Both packages share one version; bump `pyproject.toml` and `cli/pyproject.toml` together (a test
  checks). Both are MIT (`LICENSE`, copied to `cli/LICENSE` for the CLI's PyPI package).
- Publishing (ADR 0009): nothing is published from a working copy. The release workflow publishes
  the CLI to PyPI and `server.json` to the MCP Registry from a `v*` tag; the maintainer runs
  `scripts/publish-wizard.sh`. Keep the `FOOTBALL_DEMO_HOST` and `DEMO_URL = None` placeholders
  until `scripts/set_demo_host.py` replaces them; never send an API key to the CLI's demo default.
- `/robots.txt` allows everyone (and points to `/llms.txt`) unless the instance is private.
  `/privacy` states what the instance processes; when you add logging or storage of personal
  data (IPs, key names), update `privacy_blocks` in `app/agent_docs.py`.
- Evals (`evals/`) are run by hand before a release, never in pytest or CI; a new top task or
  MCP tool deserves an eval task. The Docker image installs only the API (`uv sync --frozen --no-dev`), not `cli/`.
- Observability: `/metrics` (not an OpenAPI operation, rate-limit exempt) and the `Admin`
  operations need an API key; `/metrics` also accepts `Authorization: Bearer`. Scheduler and
  season metrics are computed from PostgreSQL at scrape time with the rules in
  `app/observability.py`; keep `STALE_GRACE` equal to the CLI's (a test checks). Label HTTP
  metrics by operation ID, never by raw path. Admin operations are not MCP tools. Alert rules and
  the dashboard in `observability/grafana/` use these metric names, so rename none. The Telegram
  contact point is a Compose `configs` entry: Grafana turns numeric-looking `$VAR` values into
  numbers, which breaks chat IDs. Alert templates use `{{ $labels.x }}` unescaped.
- Statistics that a provider cannot supply must stay `null` — never default to `0`.
- 90minut pages are ISO-8859-2 and use Polish month names with Europe/Warsaw time.
- football-data.org is throttled (`FOOTBALL_DATA_MIN_REQUEST_INTERVAL_SECONDS`, default 6s) and
  needs `FOOTBALL_RESULTS_FOOTBALL_DATA_API_TOKEN`; its free plan omits goal and booking events.
  Never commit tokens or `.env`.
- ESPN (`espn`, Premier League primary source, ADR 0001) is throttled
  (`FOOTBALL_RESULTS_ESPN_MIN_REQUEST_INTERVAL_SECONDS`, default 2s) and has no rounds. Provider
  team names are mapped to stored names by per-competition `team_aliases`.
- OpenLigaDB (`openligadb`, 3. Liga primary source, ADR 0004) is key-free, throttled
  (`FOOTBALL_RESULTS_OPENLIGADB_MIN_REQUEST_INTERVAL_SECONDS`, default 2s), and has no postponed
  status (`providers.openligadb.postponed_after_hours`, default 24). Its data is ODbL-licensed:
  `/llms.txt` and `/agents` attribute it via `PROVIDER_ATTRIBUTIONS` in `app/agent_docs.py`.
- Tests must not hit the network. Parser tests use inline HTML/JSON fixtures.
- The integration test database name must contain `test`; the fixtures truncate all tables and
  the migration test downgrades to `base`.
- The ORM models must match the migrations exactly (`alembic check` runs in the integration
  tests). Example: `competition.slug` is a unique constraint plus a separate non-unique index.
- Read the application and default User-Agent versions from the `football-results-api` package
  metadata; `pyproject.toml` is the authoritative version source.
- Build Docker images from `uv.lock` with `uv sync --frozen`.
- API keys (ADR 0005): `FOOTBALL_RESULTS_API_KEYS` keys are admin keys named `env`; the optional
  keys file holds only `sha256:` hashes with a name, a role (`client` or `admin`), and per-minute
  quotas. Protected operations and `/metrics` need an admin key (`403 forbidden` for a client
  key). Key names are metric labels; never log keys or hashes.
- Private instance (`FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false`, ADR 0008): every operation with an
  operation ID except `access.PUBLIC_OPERATIONS` (`get_health`, `list_competitions`, and the
  public access-request operations, ADR 0010) needs a key, checked in the middleware
  *after* rate-limit counting so every `401` uses the IP quota; doc pages (no operation ID) stay
  public. `check_private_instance` (startup and `config check`) rejects env keys, a missing keys
  file, demo mode, and (unless the public base URL is localhost) a non-https base URL or a
  `weak_database_password`. The base `compose.yaml` binds the API to `127.0.0.1` by default.
  Hash entries need `issued_at`/`expires_at` (at most 365 days apart; `subject` entries have none); expired keys resolve to `None` (plain
  `401`, no new slug). The keys file reloads via `KeysFileWatcher.poll` in `_current_keys()`;
  tests patch `main.file_keys` (the watcher is `None` outside the lifespan). Keyed responses get
  `API-Key-Expires`; cacheable reads get `Cache-Control: private`. Edit the file only through
  `python -m app.config keys ...` helpers (atomic replace, validated).
- Demo mode (`FOOTBALL_RESULTS_DEMO_MODE`, ADR 0007): `check_demo_mode` in `app/config.py` runs
  at API startup (before seeding), in the scheduler before every (re)seed, and in `config check`.
  It rejects admin keys, providers not in `REDISTRIBUTABLE_PROVIDERS` (code, not config;
  adding one needs written permission), and a weak database password
  (`weak_database_password`). Messages name the key, never the secret, hash, or password.
  `compose.demo.yaml` binds the API to `127.0.0.1` (`ports: !override`) and requires
  `POSTGRES_PASSWORD` (`:?`); tests read it with `ComposeLoader`. Writes
  use the existing `writes-disabled` problem. The demo notice in the agent docs reads quotas
  from settings; keep instance facts out of the consumer skill.
- `RefreshRequest.competition` is required. Competition summaries may have a null `current_season`.
- Update `README.md` when behaviour, endpoints, or configuration change.

## Roadmap workflow

`docs/ROADMAP.md` contains the ordered **Implementation checklist**. When starting work, pick the
item named in its **Next up** note if there is one, else the first unchecked item (unless the user
says otherwise). When a sub-task is completed, tick it
(`[x]`) in the same change; tick the parent item once all sub-tasks are done. Do not skip ahead
without the user's agreement, because later items depend on earlier ones.

## Skills in this repo

Task-specific contributor playbooks live in `.github/skills/` (not the consumer skill in
`skills/`):

- `add-competition` – add a competition or season, or roll over the current season.
- `add-data-provider` – add a new source adapter (website or API).
- `database-migration` – change the schema safely with Alembic.
