# Running the public demo instance

This guide deploys a **demo instance**: a public, read-only deployment that serves only
competitions whose sources permit public redistribution
([ADR 0007](adr/0007-read-only-demo-instance-serves-only-redistributable-sources.md)). It runs
on an Oracle Cloud Always Free VM, with Caddy terminating TLS and Cloudflare's free plan in front
for caching and flood protection. Everything here is free; running a public instance is the
operator's decision and responsibility.

```text
client ──HTTPS──▶ Cloudflare (cache, IP rate limit) ──HTTPS──▶ Caddy :443 ──▶ API 127.0.0.1:8000
                                                                             scheduler, PostgreSQL
```

What the repository provides:

| File | Purpose |
| --- | --- |
| `compose.demo.yaml` | Compose override: demo mode, `config/demo-competitions.yaml`, 30 anonymous reads per minute, JSON logs, no admin keys |
| `config/demo-competitions.yaml` | The 3. Liga only (OpenLigaDB, ODbL) |
| `FOOTBALL_RESULTS_DEMO_MODE` | Refuses to start (API and scheduler) and fails `config check` with an admin key or a provider that is not publicly redistributable |

In demo mode, writes, jobs, admin operations, and `/metrics` return `503` `writes-disabled`, and
`/llms.txt` and `/agents` show a demo notice: read-only, no SLA, the anonymous read quota, and
the OpenLigaDB attribution. `/mcp` works anonymously, without OAuth. `client` keys from the keys
file stay allowed (for example, a higher quota for a partner).

## 1. Oracle Cloud VM

Oracle's Always Free tier includes Arm-based `VM.Standard.A1.Flex` capacity (2 OCPUs and 12 GB
of memory in total), two AMD `VM.Standard.E2.1.Micro` VMs with 1 GB of memory each, and 200 GB of
block storage, all in the tenancy's home region
([Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)).

1. Create an Ubuntu instance with the `VM.Standard.A1.Flex` shape (1 OCPU and 6 GB are plenty;
   1 GB on a Micro VM is tight for Docker builds plus PostgreSQL). On "out of host capacity",
   try another availability domain or retry later (same page).
2. Open port 443 twice: an ingress rule in the subnet's security list, and the instance's
   iptables, as in Oracle's
   [Apache on Ubuntu tutorial](https://docs.oracle.com/en-us/iaas/developer-tutorials/tutorials/apache-on-ubuntu/01oci-ubuntu-apache-summary.htm)
   (with port 443 instead of 80):

   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

   To stop clients from bypassing Cloudflare's cache and rate limit, make the security list
   allow 443 only from [Cloudflare's IP ranges](https://www.cloudflare.com/ips/). Port 80 does
   not need to be open: with **Always Use HTTPS** (section 4) Cloudflare redirects HTTP at the
   edge and connects to the origin on 443 only.
3. Install Docker Engine and the Compose plugin from Docker's apt repository
   ([Docker on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)).

**Idle reclamation:** Oracle may reclaim an Always Free instance that is idle for 7 days: 95th
percentile CPU below 20 %, network below 20 %, and, on A1 shapes, memory below 20 %
([Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm#compute)).
A quiet demo can meet those conditions, so keep backups off the VM (section 5).

## 2. The application

```bash
git clone <this repository> football-results && cd football-results
cp .env.example .env
```

Set these in `.env` (leave everything else at its default):

```bash
POSTGRES_PASSWORD=<long random password>
# Only Caddy on the same host may reach the API. Docker-published ports bypass the host
# firewall (https://docs.docker.com/engine/network/packet-filtering-firewalls/).
API_BIND_ADDRESS=127.0.0.1
FOOTBALL_RESULTS_PUBLIC_BASE_URL=https://demo.example.com
FOOTBALL_RESULTS_USER_AGENT="FootballResultsAPI demo (contact: you@example.com)"
# The Docker bridge (Caddy's connections arrive from it) plus Cloudflare's ranges; see below.
FOOTBALL_RESULTS_TRUSTED_PROXIES=172.16.0.0/12,<Cloudflare ranges>
# Leave admin keys empty; compose.demo.yaml forces FOOTBALL_RESULTS_API_KEYS empty anyway.
FOOTBALL_RESULTS_API_KEYS=
```

`FOOTBALL_RESULTS_PUBLIC_BASE_URL` makes the links in `/llms.txt`, `/agents`, and the MCP
metadata absolute and independent of the request `Host`, so Cloudflare can cache them.
Generate the trusted-proxy list from Cloudflare's published ranges
([Cloudflare IP addresses](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)):

```bash
ranges=$( { curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6; } \
  | grep . | paste -sd, -)
echo "FOOTBALL_RESULTS_TRUSTED_PROXIES=172.16.0.0/12,$ranges"
```

The API takes the client address from `X-Forwarded-For` only when the connecting peer is
trusted, and then uses the rightmost address that is not a trusted proxy. With Caddy (the Docker
bridge) and Cloudflare both trusted, that is the visitor's address, so the per-IP quota applies
to visitors, not to Cloudflare. Cloudflare changes its ranges rarely and announces additions
first (same page); repeat this step when it does.

Validate and start:

```bash
docker compose -f compose.yaml -f compose.demo.yaml run --rm --no-deps --entrypoint \
  "python -m app.config check" api
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
curl -s http://127.0.0.1:8000/health
```

`config check` must print `Demo mode: read-only (no admin keys), providers limited to
openligadb`. Change the anonymous quota with `DEMO_READ_PER_MINUTE` in `.env` (default 30). Do not
start the `observability` profile: Prometheus needs an admin key, which the demo does not have.

The scheduler refreshes the current season automatically. Older configured seasons need an admin
request, which the demo cannot accept; see [Importing a past season](#importing-a-past-season).

## 3. Caddy

Install Caddy from its apt repository ([Install](https://caddyserver.com/docs/install)). Use a
[Cloudflare origin CA certificate](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)
for the origin: Cloudflare creates it for proxied hostnames, its **Full (strict)** mode accepts
it ([Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/)),
and it needs no ACME challenge through the proxy. Create it under **SSL/TLS > Origin Server**,
save the certificate and key as `/etc/caddy/origin.pem` and `/etc/caddy/origin-key.pem`
(readable by the `caddy` user only), and note its expiry: Cloudflare sends no expiry
notifications for origin certificates (same page).

`/etc/caddy/Caddyfile`:

```caddyfile
{
	servers {
		# Cloudflare's ranges, space-separated (the list from section 2).
		trusted_proxies static 173.245.48.0/20 103.21.244.0/22 ...
		trusted_proxies_strict
	}
}

demo.example.com {
	tls /etc/caddy/origin.pem /etc/caddy/origin-key.pem
	reverse_proxy 127.0.0.1:8000
}
```

By default `reverse_proxy` ignores incoming `X-Forwarded-*` values and sets them itself; with
`trusted_proxies`, requests from Cloudflare keep Cloudflare's `X-Forwarded-For` and Caddy appends
the Cloudflare address ([reverse_proxy defaults](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#defaults)).
`trusted_proxies_strict` parses the header right to left, as Caddy recommends behind Cloudflare
([global options](https://caddyserver.com/docs/caddyfile/options#trusted-proxies)). Reload with
`sudo systemctl reload caddy`.

## 4. Cloudflare (free plan)

1. Add the domain to Cloudflare and create an `A` record (and `AAAA` if the VM has IPv6) for
   the demo hostname pointing to the VM's public IP, **Proxied**.
2. **SSL/TLS > Overview**: encryption mode **Full (strict)**. **SSL/TLS > Edge Certificates**:
   turn on [Always Use HTTPS](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/always-use-https/).
3. **Cache rule** for public GETs. Cloudflare does not cache JSON or HTML by default
   ([default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/));
   the free plan allows 10 cache rules ([Cache Rules](https://developers.cloudflare.com/cache/how-to/cache-rules/)).
   Create one rule:
   - **If incoming requests match:** hostname equals the demo hostname.
   - **Cache eligibility:** Eligible for cache.
   - **Edge TTL:** *Use cache-control header if present, bypass cache if not*; **Browser TTL:**
     *Respect origin* ([cache rule settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/)).

   This honours the API's `Cache-Control`: reads send `public, max-age=<seconds>` until the
   next scheduled update (at most `FOOTBALL_RESULTS_CACHE_MAX_AGE_SECONDS`), while errors
   (including `429`), `/metrics`, and uncached operations send `no-store`, which Cloudflare does
   not cache; `/mcp` uses `POST`, which Cloudflare never caches (same page). A cached response
   carries the `RateLimit` headers of the request that filled the cache, and cache hits do not
   count against the API's per-IP quota.
4. **Rate limiting rule** (Security rules > Create rule > Rate limiting rules;
   [dashboard steps](https://developers.cloudflare.com/waf/rate-limiting-rules/create-zone-dashboard/)).
   The free plan allows one rule that matches on the URI path, counts per IP, and uses a
   10-second period and a 10-second block
   ([availability](https://developers.cloudflare.com/waf/rate-limiting-rules/#availability)):
   - **If incoming requests match:** URI Path starts with `/`.
   - **Characteristics:** IP. **When rate exceeds:** 50 requests per 10 seconds.
   - **Action:** Block, **Duration:** 10 seconds.

   This stops floods at the edge, including of cached pages; the API's own quota (30 reads per
   minute per visitor) stays the precise limit for requests that reach the origin.

Check from outside:

```bash
curl -s https://demo.example.com/llms.txt | head -20          # demo notice near the top
curl -sI "https://demo.example.com/api/v1/matches?competition=3-liga" | grep -i "cf-cache-status\|cache-control"
curl -s -X POST https://demo.example.com/api/v1/results/refresh \
  -H 'Content-Type: application/json' -d '{"competition":"3-liga"}'   # 503 writes-disabled
```

## 5. Backups

The data can be imported again from OpenLigaDB, so backups mainly save time and history. Dump
PostgreSQL daily with [`pg_dump`](https://www.postgresql.org/docs/17/app-pgdump.html) and copy the
dumps off the VM, because Oracle may reclaim an idle instance:

```bash
# crontab -e (as the user that runs Docker)
15 3 * * * cd ~/football-results && docker compose exec -T db pg_dump -U football -Fc football > ~/backups/football-$(date +\%F).dump && find ~/backups -name 'football-*.dump' -mtime +14 -delete
```

Restore into a fresh stack with `docker compose exec -T db pg_restore -U football -d football
--clean --if-exists < football-<date>.dump`. Oracle also includes five free block volume backups
([boot volume backups](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/bootvolumebackups.htm)),
useful before upgrading the OS. A database derived from OpenLigaDB is subject to the ODbL's
share-alike terms if you publish it
([ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)).

## 6. Updates

```bash
cd ~/football-results && git pull
docker compose -f compose.yaml -f compose.demo.yaml run --rm --no-deps --entrypoint \
  "python -m app.config check" api
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
```

The API container runs `alembic upgrade head` before it starts. After an update that changes
responses, purge Cloudflare's cache (**Caching > Configuration > Purge Everything**), or wait for
`max-age` to expire.

## Importing a past season

The scheduler refreshes only current seasons; past configured seasons (2025-2026 in
`config/demo-competitions.yaml`) need `fetch_historical_season`, which needs an admin key. Import
them once in a short, local-only window without demo mode, then return to the demo:

```bash
key=$(openssl rand -hex 32)
FOOTBALL_RESULTS_API_KEYS=$key FOOTBALL_RESULTS_CONFIG_FILE=/app/config/demo-competitions.yaml \
  docker compose up -d --build api scheduler          # base file only: no demo mode, 127.0.0.1
curl -s -X POST http://127.0.0.1:8000/api/v1/results/history/fetch -H "X-API-Key: $key" \
  -H 'Content-Type: application/json' -d '{"competition":"3-liga","season":"2025-2026"}'
# Follow the returned status_url with the same key until the job has finished, then:
unset key
docker compose -f compose.yaml -f compose.demo.yaml up -d
```

Use the demo competitions file in that window, so no other competition is seeded.

## Adding a provider

The allow list of publicly redistributable providers is code, not configuration:
`REDISTRIBUTABLE_PROVIDERS` in `app/config.py`. Today it holds only `openligadb`. To add a
provider, for example 90minut.pl for the Ekstraklasa:

1. **Get written permission** from the provider for public, free redistribution through an API,
   including the attribution wording and any conditions (rate, caching, commercial use).
2. **Record it**: a new ADR (or an update to ADR 0007) that links or quotes the permission, and
   the provider's row in `docs/PROVIDERS.md`.
3. **Change the code**: add the provider to `REDISTRIBUTABLE_PROVIDERS`; if it requires
   attribution, add its text to `PROVIDER_ATTRIBUTIONS` in `app/agent_docs.py` so the demo
   notice and `/llms.txt` show it.
4. **Configure it**: add the competition to `config/demo-competitions.yaml` together with its
   `providers` entry. Its fallback provider must be allowed too; for the Ekstraklasa that means
   leaving out the TheSportsDB fallback (`fallback_provider` and the `fallback_url`s) unless
   TheSportsDB grants permission as well.
5. **Check and release**: `FOOTBALL_RESULTS_DEMO_MODE=true uv run python -m app.config check
   --config-file config/demo-competitions.yaml`, the tests, a version bump, then update the
   demo (section 6).
