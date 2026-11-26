# Running a private instance

A **private instance** serves only invited people: every API operation needs a named key that
expires ([ADR 0008](adr/0008-private-instances-require-expiring-named-keys.md)). It needs no
identity provider. The maintainer issues keys with one command and sends them over a private
channel. Use it when your sources allow sharing data with named people but not with the public;
for a public deployment, see the [demo instance](DEMO.md).

```text
invited client ──HTTPS + X-API-Key──▶ Caddy :443 ──▶ API 127.0.0.1:8000 (one worker)
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
FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false
FOOTBALL_RESULTS_API_KEYS=
FOOTBALL_RESULTS_API_KEYS_FILE=/app/secrets/api-keys.yaml
FOOTBALL_RESULTS_PUBLIC_BASE_URL=https://football.example.com
# The Docker bridge: Caddy on the host connects from it.
FOOTBALL_RESULTS_TRUSTED_PROXIES=172.16.0.0/12
```

Run the API with **one worker** (the image default). Quotas and the reloaded keys are kept in
the process's memory, so more workers would multiply every quota.

## 2. HTTPS with Caddy

Install Caddy ([Install](https://caddyserver.com/docs/install)). It gets and renews a certificate
automatically ([Automatic HTTPS](https://caddyserver.com/docs/automatic-https)).
`/etc/caddy/Caddyfile`:

```caddyfile
football.example.com {
	reverse_proxy 127.0.0.1:8000
}
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

## 4. Expiry, extension, rotation, and revocation

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

## 5. Prometheus

With the `observability` profile, `PROMETHEUS_API_KEY` must be an admin key from the keys file
(`issue --name prometheus --role admin --days 365`). Rotate it before it expires, as above: update `.env`
and run `docker compose --profile observability up -d prometheus`. The expiry alert covers it
too.

## 6. Provider permissions

A private instance does not change what your sources allow. Serve a provider's data to your
invited people only if its terms or written permission cover them, and keep each key tied to a
person so you can show who had access.
