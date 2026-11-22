# Research: optional hosted access (roadmap item 18)

Researched 2026-11-22 for roadmap item 18 (OAuth for remote MCP, per-key quotas, public demo
instance). Primary sources only. **Unverified** marks claims that could not be confirmed from an
accessible primary source; do not rely on them without checking again.

## Summary and recommendations

| Topic | Finding | Recommendation |
| --- | --- | --- |
| MCP OAuth | Authorization is **optional** in MCP 2026-07-28. If a server implements it, protected resource metadata (RFC 9728) is required, clients must use PKCE and RFC 8707 `resource`, and the server must validate the token audience and never pass the token upstream. | Keep anonymous read tools. Add OAuth as an opt-in resource-server feature that delegates to an external authorization server; tokens map to the same identities as API keys. |
| `mcp` SDK 2.3.0 | `MCPServer(auth=AuthSettings(...), token_verifier=...)` adds bearer auth and the PRM route, but **only inside the SDK's own Starlette app**, which this project bypasses. The SDK guard requires auth for the whole endpoint (no optional auth). | Implement our own optional bearer check and PRM route in `McpEndpoint`; reuse the SDK's `TokenVerifier`/`AccessToken` types. |
| Authorization server | Building one is a large, security-sensitive surface. Free self-hosted options exist, but MCP-relevant features (CIMD, DCR, RFC 8707) vary and are often unverified. | Delegate (operator's choice of IdP); validate JWTs via JWKS with issuer and audience checks. |
| Per-key quotas | Today every valid key is all-powerful and shares one quota policy. The rate-limit draft is now **-12** (`RateLimit: "read";a=..;w=..`), which renames -11's `r`/`t`. Multiple policies per response are allowed. | Named keys with roles and per-key quotas; keep fixed windows; advertise extra policies in the existing headers. |
| Public demo | ESPN's terms restrict automated extraction and public distribution; TheSportsDB's free tier and 90minut.pl/OpenLigaDB terms are unverified for public redistribution; football-data.org requires attribution. | A read-only demo should serve only competitions whose sources clearly permit it, or be scoped as a private/invite-only demo. Oracle Always Free or a small VPS behind Cloudflare free fits "self-hosting stays free". |

## 1. MCP authorization spec (2026-07-28)

- The current revision is 2026-07-28 ([specification](https://modelcontextprotocol.io/specification)).
  Authorization is **optional** for HTTP transports; when supported, it follows the authorization
  spec ([authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)).
- **Protected resource metadata (RFC 9728)** is mandatory when authorization is implemented.
  Clients discover it either from the `resource_metadata` parameter of a `WWW-Authenticate`
  header on a `401`, or from the well-known path
  `/.well-known/oauth-protected-resource[/<path>]` (for `/mcp`:
  `/.well-known/oauth-protected-resource/mcp`). The 2025-06-18 revision required the header
  ([2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)).
- **Authorization server discovery**: the PRM `authorization_servers` list, then RFC 8414 or
  OpenID Connect discovery on the issuer.
- **Client MUSTs**: PKCE (S256) and verify the AS advertises it; send the RFC 8707 `resource`
  parameter in authorization and token requests
  ([security considerations](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations)).
- **Server MUSTs**: validate that the token was issued for this server (audience); never pass the
  inbound token to upstream APIs (no token passthrough).
- **Client registration**: Client ID Metadata Documents (CIMD) are SHOULD; Dynamic Client
  Registration (RFC 7591) is MAY and deprecated; pre-registration is allowed
  ([client registration](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration)).
- **Scopes**: a `403` with `WWW-Authenticate: Bearer error="insufficient_scope", scope="..."`
  asks the client to step up.
- Mixed access (some tools anonymous, some authenticated) is not forbidden by the spec; how each
  client reacts to a `401` on only some calls is **unverified**.

## 2. Client support

| Client | Remote HTTP | Static headers (API key) | OAuth | Source |
| --- | --- | --- | --- | --- |
| GitHub Copilot CLI | Yes | Yes (`--header`, `headers` in config) | Unverified (DCR/CIMD) | [docs](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) |
| VS Code (Copilot Chat) | Yes | Yes (environment-backed headers) | Documented, incl. pre-registered `oauthClientId`; DCR/CIMD unverified | [docs](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) |
| Claude Code | Yes (`claude mcp add --transport http`) | Unverified | Unverified (docs blocked by organization policy) | — |

Conclusion: shell and IDE clients can already send `X-API-Key`. OAuth matters mainly for hosted
chat clients that cannot set headers; their support must be verified per client before relying
on it.

## 3. `mcp` Python SDK 2.3.0

Paths are relative to `.venv/lib/python3.14/site-packages/mcp/`.

- `MCPServer.__init__` accepts `auth: AuthSettings`, `token_verifier: TokenVerifier`, and
  `auth_server_provider` (`server/mcpserver/server.py:157-180`).
- `AuthSettings.resource_server_url` and `validate_token_resource` (`server/auth/settings.py:42-69`):
  resource checking is **off unless enabled**; 3.0 will default it to `True`. A warning is emitted
  when it is unset.
- The lowlevel server wires `AuthenticationMiddleware(BearerAuthBackend)`, `RequireAuthMiddleware`
  around the Streamable HTTP endpoint, and `create_protected_resource_routes` for the PRM route
  (`server/lowlevel/server.py:781-851`). All of this is part of the Starlette app returned by
  `streamable_http_app()`.
- **Bypass hazard:** this project calls `streamable_http_app()` only to create the session manager
  and then routes requests to `session_manager.handle_request` from `McpEndpoint`
  (`app/mcp_server.py:518-536`). Passing `auth=` to `MCPServer` would therefore **not** protect
  `/mcp` and would not serve PRM. The project must add its own check.
- `RequireAuthMiddleware` rejects every unauthenticated request with `401` and puts
  `resource_metadata` in `WWW-Authenticate`; its `403 insufficient_scope` has no `scope`
  parameter (`server/auth/middleware/bearer_auth.py:115-142`). It cannot express optional auth.
- The SDK contains an authorization-server framework (`server/auth/handlers/{authorize,token,
  register,revoke,metadata}.py`, `OAuthAuthorizationServerProvider`); the application must
  implement storage, login, and consent. No JWT or introspection helper ships in the SDK; a
  `TokenVerifier` must be written (for example with PyJWT and a JWKS client).

## 4. Delegated vs built-in authorization server

| Server | Licence | MCP-relevant capabilities (verified) | Unverified |
| --- | --- | --- | --- |
| Keycloak | Apache-2.0 | OIDC DCR incl. anonymous registration ([docs](https://www.keycloak.org/securing-apps/client-registration)) | CIMD, RFC 8707, footprint |
| Authentik | MIT (+ enterprise parts) | PKCE, JWKS, introspection; 2 cores / 2 GB RAM ([docs](https://docs.goauthentik.io/install-config/install/docker-compose/)) | DCR, CIMD, RFC 8707 |
| Zitadel | AGPL-3.0 | S256 PKCE, pre-registration ([docs](https://zitadel.com/docs/apis/openidoauth/endpoints)) | DCR, CIMD, RFC 8707 |
| Ory Hydra (OSS) | Apache-2.0 | PKCE; vendor states OSS lacks resource indicators and full MCP audience validation ([guide](https://www.ory.com/docs/guides/mcp/hydra-oauth-oss)) | CIMD, DCR |
| Authelia | Apache-2.0 | S256 PKCE, `resource`/audience handling; DCR planned ([docs](https://www.authelia.com/configuration/identity-providers/openid-connect/clients/)) | CIMD, JWT access tokens |
| Dex | Apache-2.0 | Static clients; audience only for ID tokens ([docs](https://dexidp.io/docs/configuration/custom-scopes-claims-clients/)) | Most MCP features |
| Logto | MPL-2.0 | MCP guide: pre-registration, audience-bound JWT access tokens, JWKS ([guide](https://docs.logto.io/use-cases/ai/mcp-server-add-auth)) | DCR, CIMD |
| Kanidm | MPL-2.0 | S256 PKCE, RFC 9068 JWTs, introspection ([docs](https://kanidm.github.io/kanidm/stable/integrations/oauth2.html)) | DCR, CIMD, RFC 8707 |

- **GitHub OAuth apps** support PKCE and manual registration; DCR, CIMD, and RFC 8707 are
  unverified, and tokens are for GitHub's APIs, not audience-bound to our server
  ([docs](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)).
  Accepting a GitHub token directly would be token passthrough-adjacent and is not recommended.
- **Single operator, delegated:** the API becomes only a resource server: serve PRM, verify JWTs
  against the issuer's JWKS (issuer, audience = the `/mcp` URL, expiry, scopes), and map a claim
  (`sub`, or `client_id`) to quota and role. Running the IdP (Keycloak/Authentik ≈ 1–2 GB RAM) is
  extra operational work, which is why OAuth should stay optional.
- **Built in:** the SDK's AS framework would need users, login UI, consent, token storage, key
  rotation, and CIMD/DCR, all security-sensitive. Not proportionate for this project.

## 5. Per-key quotas

- **Current state** (`app/main.py`, `app/ratelimit.py`): keys are a comma-separated env var; any
  valid key may write, read admin endpoints, and scrape `/metrics`; a valid key changes the
  rate-limit client from `ip:` to `key:<hash>` but not the quota.
- **Headers:** draft -12 (2026-11-20) keeps `RateLimit-Policy: "read";q=120;w=60` and allows
  several policies (`burst;q=100;w=60,"daily";q=1000;w=86400`). `RateLimit` changes from -11's
  `r` (remaining) and `t` (reset) to `a` (available) and `w` (effective window), and adds optional
  `pk` (partition key, byte sequence) and `c` (cost)
  ([-12](https://www.ietf.org/archive/id/draft-ietf-httpapi-ratelimit-headers-12.html),
  [-11](https://www.ietf.org/archive/id/draft-ietf-httpapi-ratelimit-headers-11.html)).
  Switching syntax is a client-visible change; keep -11 unless decided otherwise.
- **Errors:** `429` with `Retry-After` (RFC 6585) and an RFC 9457 problem; the existing
  `rate-limited` slug fits per-minute limits. A daily quota could reuse it or add a new slug.
- **Practice:** football-data.org's free plan is 10 calls/minute
  ([register](https://www.football-data.org/client/register)); TheSportsDB free is 30/minute
  ([pricing](https://www.thesportsdb.com/pricing)). GitHub REST limits per hour per user (not
  re-fetched here).
- **Where keys live:**

| Store | Pros | Cons |
| --- | --- | --- |
| Env var (today) | Simple, Compose-friendly | No names, roles, or quotas without a mini-syntax; restart to change |
| Config file (YAML, hashed keys) | Reviewable, validated by `config check`, no migration | Restart (or reload) to change; secrets in a file need care (store hashes) |
| Database (hashed keys + admin endpoint) | Runtime changes, usage stats | Migration, admin API, CLI; bigger surface |

- **Counters:** in-process fixed windows reset on restart and are per worker. Acceptable for
  per-minute limits; a daily quota would reset on every deploy, so PostgreSQL counters (one upsert
  per request, or batched) are needed if daily quotas must hold.

## 6. Public demo instance

| Provider | May a public, free, non-commercial demo serve its data? |
| --- | --- |
| ESPN | **No, without permission.** Disney's terms restrict automated extraction and public distribution ([terms §2](https://disneytermsofuse.com/english/)). |
| football-data.org | **Probably**, with the attribution "Football data provided by the Football-Data.org API", one application per key, and no display after cancellation ([register/terms](https://www.football-data.org/client/register)). |
| TheSportsDB | **Unverified.** Free key allowed for development; no app-store publication on the free tier; third-party rights apply ([terms](https://www.thesportsdb.com/docs_terms_of_use.php)). |
| OpenLigaDB | **Likely with ODbL attribution and share-alike** (per ADR 0004), but the site returned 403 here: unverified. |
| 90minut.pl | **Unverified.** Site and `robots.txt` unreachable here; HTML scraping of a private site needs explicit permission for public redistribution. |

| Hosting | Free in Nov 2026? | Notes |
| --- | --- | --- |
| Oracle Cloud Always Free | Yes | Ubuntu VMs; capacity limits and idle reclamation ([docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)) |
| Google Cloud | Yes (limited) | One US `e2-micro`, 30 GB disk; run PostgreSQL yourself ([docs](https://docs.cloud.google.com/free/docs/free-cloud-features)) |
| Railway | $1/month credit | Services stop when credit runs out ([docs](https://docs.railway.com/pricing/free-plan)) |
| Render | Partly | Free PostgreSQL expires after 30 days ([docs](https://render.com/docs/free)) |
| Fly.io | No | No free tier for new organizations ([docs](https://docs.fly.io/about/pricing)) |
| Koyeb | Unverified | Lists a small free PostgreSQL ([pricing](https://www.koyeb.com/pricing)) |

**Abuse protection:** per-IP limits (already built in), read-only mode (no API keys configured →
writes return `writes-disabled`), Cloudflare free: one IP rate-limit rule with a 10 s period
([docs](https://developers.cloudflare.com/waf/rate-limiting-rules/)); JSON is not cached by
default, so add a cache rule only for public GETs
([docs](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/)). Behind
Cloudflare, `FOOTBALL_RESULTS_TRUSTED_PROXIES` must list Cloudflare's ranges.

## Sources

- MCP specification 2026-07-28: <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>
- MCP 2025-06-18 authorization: <https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization>
- RFC 9728, RFC 8414, RFC 8707, RFC 7591, RFC 9457, RFC 6585 (rfc-editor.org)
- draft-ietf-httpapi-ratelimit-headers-11 and -12: <https://www.ietf.org/archive/id/>
- `mcp` 2.3.0 source in `.venv` (paths above)
- Client docs: GitHub Copilot CLI, VS Code MCP configuration (links above)
- Provider terms and hosting pages (links in the tables)
