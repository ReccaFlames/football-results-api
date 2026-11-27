# Deployment plan

Checklist for the first deployment to a real server. Readiness was reviewed on 2026-11-26
(version 0.25.0): lint, formatting, unit tests, PostgreSQL integration tests (including
`alembic check`), `config check`, and the Docker build all pass. There are **no code blockers**;
what remains is setup and operator steps. The step-by-step guides are [DEMO.md](DEMO.md) (public)
and [PRIVATE.md](PRIVATE.md) (invited people).

This checklist covers the Compose deployment (an Oracle VM with Caddy). The planned home server
runs a private instance on k3s instead: see [SELF-HOSTED.md](SELF-HOSTED.md)
([ADR 0011](adr/0011-home-server-runs-single-node-k3s-with-flux.md)). The blockers below apply to
both.

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

## Services and routes

`docker compose up -d --build` starts four services. Only the API and the landing page are
published to the reverse proxy, and both are bound to `127.0.0.1` by default:

| Service | Image | Published on | Role |
| --- | --- | --- | --- |
| `db` | PostgreSQL | `127.0.0.1:${POSTGRES_PORT:-5432}` | Data |
| `api` | `Dockerfile` | `API_BIND_ADDRESS:API_PORT` (default `127.0.0.1:8000`) | The API, agent docs, `/mcp` |
| `scheduler` | `Dockerfile` | Not published | Refreshes current seasons |
| `web` | `web/Dockerfile` | `WEB_BIND_ADDRESS:WEB_PORT` (default `127.0.0.1:8001`) | The Whistleprint landing page; calls the API at `http://api:8000` |

Caddy on the host is the only public entry. It serves both apps on one origin and routes by
path:

| Path | Upstream |
| --- | --- |
| `/`, `/request-access`, `/static/*`, `/favicon.ico` | Landing page, `127.0.0.1:8001` |
| Everything else (`/api/*`, `/llms.txt`, `/agents`, `/docs`, `/privacy`, `/mcp`, ...) | API, `127.0.0.1:8000` |

The API has no route on the landing paths. The landing page links relatively (`docs`,
`llms.txt`, `agents`, `privacy`) when `FOOTBALL_RESULTS_WEB_PUBLIC_API_URL` is empty, so those
links work only on this shared origin. The Caddyfiles
are in [DEMO.md](DEMO.md#3-caddy) and [PRIVATE.md](PRIVATE.md#2-https-with-caddy).

### Client addresses and trusted proxies

The API counts rate limits per client IP. It reads `X-Forwarded-For` only when the connecting
peer is in `FOOTBALL_RESULTS_TRUSTED_PROXIES`, and then uses the rightmost address that is not
trusted:

```text
visitor ─▶ (Cloudflare, demo only) ─▶ Caddy on the host ─┬─▶ API              peer: Compose gateway
                                                         └─▶ landing ─▶ API   peer: landing container
```

- Caddy sets `X-Forwarded-For` to the visitor's address. Behind Cloudflare it keeps Cloudflare's
  header and appends the Cloudflare address instead (`trusted_proxies` in the demo Caddyfile).
- Caddy reaches the published ports from the host, so the containers see its connections coming
  from the Compose network's gateway (for example `172.18.0.1`). The landing app appends that
  peer to `X-Forwarded-For` (it runs Uvicorn with `--no-proxy-headers`, so it sees the real peer)
  and calls the API from its own Compose address (for example `172.18.0.3`).
- `172.16.0.0/12` covers both, because Docker allocates its default bridge networks from
  `172.17.0.0/16` to `172.31.0.0/16`. Check the subnet after the first start with
  `docker network inspect football-results_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'`
  (the network is named after the clone directory); if Docker picked another range, list that
  instead. The demo adds Cloudflare's ranges ([DEMO.md](DEMO.md#2-the-application)).
- Without it, the API sees every visitor as the gateway or the landing container: all visitors
  share one read quota, and all landing-page form submissions share the landing app's
  `create_access_request` limit of 3 accepted requests per hour.

## Go-live checklist

1. [ ] Both blockers are resolved.
2. [ ] Create the VM: Oracle `VM.Standard.A1.Flex`, not the 1 GB Micro. Open port 443 in the
       security list and in iptables. A private instance also needs port 80 for Caddy's ACME
       challenge.
3. [ ] Create `.env` with `chmod 600`, and set:
       - `POSTGRES_PASSWORD=$(openssl rand -hex 32)`, before the first start
       - `API_BIND_ADDRESS=127.0.0.1` and `WEB_BIND_ADDRESS=127.0.0.1`
       - `WEB_PUBLIC_API_URL=` (empty, so the landing page links relatively; `compose.demo.yaml`
         forces this for the demo)
       - `FOOTBALL_RESULTS_PUBLIC_BASE_URL`
       - `FOOTBALL_RESULTS_USER_AGENT` with a contact address
       - `FOOTBALL_RESULTS_TRUSTED_PROXIES`: `172.16.0.0/12` for Caddy and the landing app,
         plus Cloudflare's ranges on the demo ([Client addresses](#client-addresses-and-trusted-proxies))
       - `FOOTBALL_RESULTS_OPERATOR_CONTACT`
4. [ ] Demo only: set journald retention equal to `FOOTBALL_RESULTS_LOG_RETENTION_DAYS`
       (DEMO.md, section 2).
5. [ ] Run `config check` in the container, then `up -d --build`, then check the API and the
       landing page on the host:
       `curl -s http://127.0.0.1:8000/health` and
       `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/` (`200`).
6. [ ] Set up Caddy with the [path routes](#services-and-routes) to `127.0.0.1:8001` and
       `127.0.0.1:8000`, and check the Docker network's subnet against
       `FOOTBALL_RESULTS_TRUSTED_PROXIES`. For the demo, use a Cloudflare origin certificate and
       put its expiry date in a calendar; Cloudflare sends no reminder.
7. [ ] Demo only: in Cloudflare, set Full (strict), Always Use HTTPS, one cache rule, one rate
       limiting rule, and the bot settings (DEMO.md, section 4).
8. [ ] Demo only: import the 2025-2026 season once in the local admin window (DEMO.md,
       "Importing a past season").
9. [ ] Private only: issue keys with `python -m app.config keys --file secrets/api-keys.yaml issue`,
       and an admin key for Prometheus if you use the `observability` profile.
10. [ ] Run the external checks: for the demo, DEMO.md section 4 (the landing page's "No key
        needed", the `llms.txt` notice, `robots.txt`, `/privacy`, cache headers, and `503` on
        writes); for a private instance, PRIVATE.md section 2 (the landing page with its form,
        `401` without a key).
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
