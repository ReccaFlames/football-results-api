# Deployment plan

Checklist for the first deployment to a real server. Readiness was reviewed on 2026-11-26
(version 0.25.0): lint, formatting, unit tests, PostgreSQL integration tests (including
`alembic check`), `config check`, and the Docker build all pass. There are **no code blockers**;
what remains is setup and operator steps. The step-by-step guides are [DEMO.md](DEMO.md) (public)
and [PRIVATE.md](PRIVATE.md) (invited people).

## Blockers

### 1. Put the repository on GitHub

The guides deploy with `git clone https://github.com/ReccaFlames/football-results-api.git`, and
CI (`.github/workflows/ci.yml`) runs only on GitHub.

- [ ] Create the repository (step 1 of `scripts/publish-wizard.sh`, or
      `gh repo create ReccaFlames/football-results-api --source . --push`) with the right account.
- [ ] Push `main` and confirm the first CI run is green.
- [ ] If the repository stays private, clone on the server over SSH with a read-only deploy key.

### 2. Choose the instance type

The instance type decides which providers you may serve. `config/competitions.yaml` uses 90minut.pl,
ESPN, football-data.org, and TheSportsDB; only OpenLigaDB is publicly redistributable
([ADR 0007](adr/0007-read-only-demo-instance-serves-only-redistributable-sources.md)).

- [ ] **Public:** demo instance, `compose.yaml` + `compose.demo.yaml` (3. Liga only), following
      [DEMO.md](DEMO.md).
- [ ] **Invited people:** private instance, following [PRIVATE.md](PRIVATE.md), with only
      providers whose terms cover those people (PRIVATE.md, section 6).
- Never serve the full `config/competitions.yaml` anonymously on a public URL.

## Fixed during the review

- [x] `scripts/backup-postgres.sh` no longer leaves empty dumps or prunes good ones after a
      failure. It checks each dump with `pg_restore --list` and keeps `BACKUP_RETENTION_DAYS`
      days (default 7).
- [x] Private instances refuse a default or short database password, as demo mode already did.
- [x] `compose.yaml` and `.env.example` publish the API on `127.0.0.1` by default.
- [x] [DEMO.md](DEMO.md) section 6 adds monitoring: an external uptime check on `/health` that
      alerts unless the body contains `"scheduler":"ok"`.

## Go-live checklist

1. [ ] Both blockers are resolved.
2. [ ] Create the VM: Oracle `VM.Standard.A1.Flex`, not the 1 GB Micro. Open port 443 in the
       security list and in iptables. A private instance also needs port 80 for Caddy's ACME
       challenge.
3. [ ] Create `.env` with `chmod 600`, and set:
       - `POSTGRES_PASSWORD=$(openssl rand -hex 32)`, before the first start
       - `API_BIND_ADDRESS=127.0.0.1`
       - `FOOTBALL_RESULTS_PUBLIC_BASE_URL`
       - `FOOTBALL_RESULTS_USER_AGENT` with a contact address
       - `FOOTBALL_RESULTS_TRUSTED_PROXIES`
       - `FOOTBALL_RESULTS_OPERATOR_CONTACT`
4. [ ] Demo only: set journald retention equal to `FOOTBALL_RESULTS_LOG_RETENTION_DAYS`
       (DEMO.md, section 2).
5. [ ] Run `config check` in the container, then `up -d --build`, then
       `curl -s http://127.0.0.1:8000/health`.
6. [ ] Set up Caddy. For the demo, use a Cloudflare origin certificate and put its expiry date in
       a calendar; Cloudflare sends no reminder.
7. [ ] Demo only: in Cloudflare, set Full (strict), Always Use HTTPS, one cache rule, one rate
       limiting rule, and the bot settings (DEMO.md, section 4).
8. [ ] Demo only: import the 2025-2026 season once in the local admin window (DEMO.md,
       "Importing a past season").
9. [ ] Private only: issue keys with `python -m app.config keys --file secrets/api-keys.yaml issue`,
       and an admin key for Prometheus if you use the `observability` profile.
10. [ ] Run the external checks in DEMO.md section 4: the `llms.txt` notice, `robots.txt`,
        `/privacy`, cache headers, and `503` on writes.
11. [ ] Add `scripts/backup-postgres.sh` to cron and copy the dumps off the VM; Oracle may reclaim
        an idle Always Free VM.
12. [ ] Test a restore once into a scratch stack before you rely on the backups.
13. [ ] Set up the external uptime monitor on `/health` (DEMO.md, section 6).
14. [ ] Run the agent evals in `evals/` once against the deployed instance (by hand, never in CI).

## Later (not needed to run a server)

- Publishing ([ADR 0009](adr/0009-published-cli-defaults-to-the-demo-instance.md)): run
  `scripts/set_demo_host.py <host>` to replace `FOOTBALL_DEMO_HOST` and `DEMO_URL`, set up the
  PyPI pending publisher, and tag `v1.0.0` for the release workflow. That closes roadmap item 15
  (MCP Registry).
- Roadmap items 17d (more providers) and 20 (OAuth for chat clients on private instances).
- A paid TheSportsDB key, so the Polish fallback stays reliable on busy matchdays.
- Pre-build the image in CI (GHCR) instead of running `--build` on the VM.
