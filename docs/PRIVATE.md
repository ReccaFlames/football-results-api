# Running a private instance

A **private instance** serves only invited people: every API operation needs a named key that
expires ([ADR 0008](adr/0008-private-instances-require-expiring-named-keys.md)). It needs no
identity provider. The maintainer issues keys with one command and sends them over a private
channel. Use it when your sources allow sharing data with named people but not with the public;
for a public deployment, see the [demo instance](DEMO.md).

```text
invited client ──HTTPS + X-API-Key──┐               ┌─ /, /request-access, ───▶ landing page 127.0.0.1:8001
visitor ──HTTPS, no key─────────────┴─▶ Caddy :443 ─┤  /static/*, /favicon.ico  │ public operations only
                                                    │                           ▼
                                                    └─ everything else ───────▶ API 127.0.0.1:8000 (one worker)
                                                                                scheduler, PostgreSQL
```

## What changes with `FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false`

- Every API operation except `get_health`, `list_competitions`, `create_access_request`, and
  `get_access_request` returns `401 unauthorized` (with
  `WWW-Authenticate: ApiKey`) without a valid, unexpired key. `/mcp` needs a key or an OAuth
  token; `/metrics` needs an admin key, as before.
- These stay public, so people and agents can learn how to ask for access: `/health`,
  `/llms.txt`, `/llms-full.txt`, `/agents`, `/agents.md`, `/problems/*`, `/skills/...`,
  `/openapi.json`, `/docs`, and the OAuth metadata. The agent docs say that a key is required.
- The landing page (`/`, the `web` service) stays public too, and has no key of its own: it
  calls only `list_competitions` (the competition list) and `create_access_request` (its form),
  and links to the public doc pages.
- Every `401` counts against the caller's IP read quota, so guessing keys is slow.
- Cacheable reads send `Cache-Control: private, ...` (`ETag` and `304` still work), so a shared
  cache never serves keyed data to someone without a key.
- Keyed responses carry `API-Key-Expires` (UTC). The `football-results` CLI warns on stderr when
  the key expires within 14 days.

The API and `python -m app.config check` refuse to start a private instance when:

- `FOOTBALL_RESULTS_API_KEYS` is set. Environment keys have no name or expiry, so use the keys
  file only.
- `FOOTBALL_RESULTS_API_KEYS_FILE` is not set.
- `FOOTBALL_RESULTS_PUBLIC_BASE_URL` is missing or not `https` (localhost is allowed for
  testing).
- The database password is a shipped default (`football`, `change-me`) or shorter than 16
  characters (also skipped for a localhost base URL). Set `POSTGRES_PASSWORD` to
  `openssl rand -hex 32` before the database is first created.
- `FOOTBALL_RESULTS_DEMO_MODE` is on. A demo is public by design.

## 1. Settings

Follow the [Ubuntu deployment](../README.md#ubuntu-deployment) steps, then set in `.env`:

```bash
API_BIND_ADDRESS=127.0.0.1
WEB_BIND_ADDRESS=127.0.0.1
# Empty: Caddy serves the landing page and the API on one origin, so its links stay relative.
WEB_PUBLIC_API_URL=
FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false
FOOTBALL_RESULTS_API_KEYS=
FOOTBALL_RESULTS_API_KEYS_FILE=/app/secrets/api-keys.yaml
FOOTBALL_RESULTS_PUBLIC_BASE_URL=https://football.example.com
# Docker's bridge networks: Caddy on the host (via the Compose gateway) and the landing app.
FOOTBALL_RESULTS_TRUSTED_PROXIES=172.16.0.0/12
```

`172.16.0.0/12` lets the API count requests per visitor, both for direct API calls through Caddy
and for form submissions through the landing app; without it, all form submissions share one
limit of 3 accepted access requests per hour. See
[Client addresses and trusted proxies](DEPLOYMENT.md#client-addresses-and-trusted-proxies) for
the chain and how to check the Docker subnet.

Run the API with **one worker** (the image default). Quotas and the reloaded keys are kept in
the process's memory, so more workers would multiply every quota.

Optional: to get a Telegram message for each new access request (ID, name, client type, intended
use, competitions; never the contact email or message), set `FOOTBALL_RESULTS_TELEGRAM_BOT_TOKEN`
and `FOOTBALL_RESULTS_TELEGRAM_CHAT_ID` (both or neither). Docker Compose falls back to the
Grafana alert settings `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; set the
`FOOTBALL_RESULTS_` names when others can read the alert chat. Without them, new requests are
only logged (ID and client type) and counted in `football_results_access_requests_total`.

## 2. HTTPS with Caddy

Install Caddy ([Install](https://caddyserver.com/docs/install)). It gets and renews a certificate
automatically ([Automatic HTTPS](https://caddyserver.com/docs/automatic-https)).
`/etc/caddy/Caddyfile`:

```caddyfile
football.example.com {
	# The landing page; everything else goes to the API (it has no / route).
	@landing path / /request-access /static/* /favicon.ico
	handle @landing {
		reverse_proxy 127.0.0.1:8001
	}
	handle {
		reverse_proxy 127.0.0.1:8000
	}
}
```

Reload with `sudo systemctl reload caddy`. `reverse_proxy` sets `X-Forwarded-For` to the
visitor's address ([reverse_proxy defaults](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#defaults));
the landing app appends Caddy's address before it calls the API. Check from outside:

```bash
curl -s https://football.example.com/ | grep -c 'action="request-access"'   # 1: the form
curl -s -o /dev/null -w '%{http_code}\n' https://football.example.com/api/v1/matches   # 401
curl -s https://football.example.com/llms.txt | head -20                     # key required
```

Keys travel in a header, so never serve a private instance over plain HTTP. If you put a CDN in
front, do not add a cache rule that ignores `Cache-Control`: the API marks keyed data `private`.

## 3. Issuing keys

Run the `keys` commands on the host, against the file that Compose mounts (`./secrets` is
mounted read-only at `/app/secrets`). Mount the directory, not the file: the commands replace the
file atomically, and the API reloads it within a few seconds without a restart.

```bash
# A client key for 30 days (the default); the key goes to stdout, once.
uv run python -m app.config keys --file secrets/api-keys.yaml issue --name alice
# Longer, or an admin key with its own quotas
uv run python -m app.config keys --file secrets/api-keys.yaml issue --name bob --days 90 \
  --read-per-minute 600
uv run python -m app.config keys --file secrets/api-keys.yaml issue --name ops --role admin
uv run python -m app.config keys --file secrets/api-keys.yaml list
```

- Name each key after one person or app (`[a-z0-9-]`). The name shows up in logs, metrics, and
  `403` messages.
- Send the key over a private channel (a password manager share or an end-to-end encrypted
  message), never by plain email or a ticket. The file stores only its SHA-256 hash, so a lost
  key cannot be recovered; issue a new one.
- New files are created with mode `0644`: they hold only hashes, and the container user
  (UID 10001) must read them. The commands keep the mode of an existing file and its leading
  `#` comment lines; other comments are dropped.
- The recipient sets `FOOTBALL_RESULTS_URL` and `FOOTBALL_RESULTS_API_KEY` for the CLI, or sends
  `X-API-Key` directly.

## 4. Access requests

People and agents ask for a key with `create_access_request` ([ADR 0010](adr/0010-access-requests-are-owned-by-the-api-and-never-auto-approved.md));
they get a request ID and a one-time claim secret. To invite someone, send them the instance URL:

- **People** fill in the form on the landing page (`https://football.example.com/`). The
  confirmation page shows the request ID and the claim secret once (`Cache-Control: no-store`)
  and explains how to claim the key. A lost claim secret cannot be recovered; they send a new
  request once the old one is decided.
- **Agents and scripts** call `create_access_request` directly (`/llms.txt` explains it), or use
  `football-results create-access-request`.
- Either way, they check the status and claim the key with `get_access_request`:
  `FOOTBALL_RESULTS_CLAIM_SECRET=<claim secret> football-results get-access-request <id>` (with
  `FOOTBALL_RESULTS_URL` set to the instance), or `GET /api/v1/access-requests/<id>` with the
  header `Claim-Secret: <claim secret>`.

A request never grants access by itself:

1. You get a Telegram message with the request ID, name, client type, intended use, and
   competitions (see [Settings](#1-settings); without Telegram, watch the API log or the
   `football_results_access_requests_total` metric).
2. Review the requests, including the contact email, on the host:

   ```bash
   uv run python -m app.config access-requests list --status pending
   ```

3. Approve or reject one:

   ```bash
   # A client key for 365 days named req-<id>-<slug of the name>, default quotas
   uv run python -m app.config access-requests approve 7 --file secrets/api-keys.yaml
   # Overrides: --name, --days (1-365), --read-per-minute
   uv run python -m app.config access-requests approve 7 --file secrets/api-keys.yaml \
     --name ada-fan-site --days 90 --read-per-minute 120
   uv run python -m app.config access-requests reject 8
   ```

4. The requester polls `get_access_request` with the claim secret and gets the key exactly once,
   within 14 days of the approval. You never see or send the key: `approve` prints only the key
   name, its expiry, and the claim deadline.

- `approve` writes the keys file first and the database second; if the approval fails (for
  example, the request was rejected meanwhile), it removes the new entry again. Only a pending
  request can be approved or rejected; anything else exits with code 2.
- Retention: the scheduler erases the key and the personal data of approved requests that are
  not claimed within 14 days (claiming erases them at once), and deletes pending and rejected
  requests 30 days after they were created. An unclaimed key stays in the keys file until you
  revoke it (`keys revoke --name req-...`); nobody else knows it.
- The commands use `FOOTBALL_RESULTS_DATABASE_URL`. On the host, point it at the PostgreSQL port
  that Compose publishes on `127.0.0.1` (`POSTGRES_PORT`, default 5432), for example
  `postgresql+psycopg_async://football:<password>@localhost:5432/football`. `list` and `reject`
  also run in the API container (`docker compose exec api python -m app.config access-requests
  list`); approve from the host, because the container mounts the keys file read-only.

## 5. Expiry, extension, rotation, and revocation

Every key has `issued_at` and `expires_at`. An expired key gets `401`, just like a wrong key.

```bash
# Set alice's expiry to 60 days from now; refused beyond one year after issue
uv run python -m app.config keys --file secrets/api-keys.yaml extend --name alice --days 60
# Rotation after a year: a new entry with a new name, then remove the old one
uv run python -m app.config keys --file secrets/api-keys.yaml issue --name alice-2027
uv run python -m app.config keys --file secrets/api-keys.yaml revoke --name alice
```

- `revoke` removes the entry; the API stops accepting the key within seconds. Revoke at once
  when a key leaks or someone leaves.
- `keys list` and `config check` flag keys that expire within 14 days. The
  `observability` profile adds the metric `football_results_api_key_expires_in_seconds{key}` and
  a Grafana alert 7 days before expiry. The alert keeps firing for an expired key until you
  extend or revoke it.
- An entry from before expiry was required stops startup with the fix in the message: run
  `keys extend --name <name>` (it starts the year now) or issue a new key.
- If the file becomes invalid while the API runs, the API logs an error and keeps the last valid
  keys. Each reload logs the names added, removed, or changed, never keys or hashes.

## 6. Prometheus

With the `observability` profile, `PROMETHEUS_API_KEY` must be an admin key from the keys file
(`issue --name prometheus --role admin --days 365`). Rotate it before it expires, as above: update `.env`
and run `docker compose --profile observability up -d prometheus`. The expiry alert covers it
too.

## 7. Provider permissions

A private instance does not change what your sources allow. Serve a provider's data to your
invited people only if its terms or written permission cover them, and keep each key tied to a
person so you can show who had access.
