# Private instances require expiring named keys for every data operation

Publishing the API to a few known people needs access control without an external identity
provider. Anonymous reads are fine for the demo instance (ADR 0007), but other sources may reach
only people covered by their provider's permission, so these people must be identifiable and
removable. We add a **private instance** mode (`FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false`). Every
data operation, `/mcp`, and `/metrics` need a valid key or OAuth token. Discovery pages (`/health`,
agent docs, problems, skill, OpenAPI, OAuth metadata) stay public. Keys stay in the hashed keys
file of ADR 0005. A maintainer manages them with `python -m app.config keys issue|extend|revoke|list`.
`issue` prints a `frk_`-prefixed key once, and the maintainer sends it over a private channel.
Every `hash` entry has a required `issued_at` and `expires_at`. The default lifetime is 30 days,
and `expires_at` is at most 365 days after `issued_at`, so one secret lives at most a year and is
then rotated as a new entry. The API reloads the keys file when it changes.

## Considered options

- **Keys in PostgreSQL with admin endpoints, or a self-service invite link**: rejected. Both add
  public surface and state for a handful of keyed clients, and the maintainer already has shell
  access to the host.
- **An external identity provider as the only option**: rejected as a requirement. OAuth for
  `/mcp` stays optional (ADR 0006).
- **A one-year cap counted from today**: rejected. Repeated extensions would keep a leaked key
  alive forever.
- **Optional expiry**: rejected. Forgotten keys are the usual way access outlives its purpose.
- **A distinct `key-expired` problem**: rejected. It would tell a caller that a key was once valid.
  Expired keys get `unauthorized`.

## Consequences

- A keys file with a `hash` entry missing `issued_at` or `expires_at` stops the API at startup.
  `config check` names the fix (`keys extend` or reissue). OAuth `subject` entries have no expiry,
  because tokens expire at the issuer.
- A private instance refuses `FOOTBALL_RESULTS_API_KEYS` and a non-`https` public base URL
  (localhost excepted). Keys are read only from `X-API-Key`.
- Cacheable reads send `Cache-Control: private`, and the private guide has no CDN cache rule,
  so no shared cache serves keyed data to anonymous callers.
- Every `401` counts against the caller's IP read quota.
- With 30-day keys, the maintainer extends keys often. A per-key expiry metric drives a Grafana
  alert 7 days before expiry, and keyed responses carry an expiry header for the client's tools.
- Reloads log the key names that were added, removed, or changed. Together with the per-name
  request metrics, this is the audit trail.
- One API worker. Quotas stay in memory per process (ADR 0005).
