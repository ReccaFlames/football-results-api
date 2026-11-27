# A built-in authorization server signs chat clients in with an API key

claude.ai and ChatGPT connect to remote MCP servers only through OAuth; they cannot send an
`X-API-Key` header. On a private instance ([ADR 0008](0008-private-instances-require-expiring-named-keys.md))
every MCP call needs a key, so these clients cannot connect today.
[ADR 0006](0006-optional-oauth-resource-server-for-mcp.md) made `/mcp` a resource server for an
*external* authorization server and rejected a built-in one. The research for this item (research
note §8) found that no small self-hosted authorization server fits both clients on an 8 GB home
server: Keycloak 26.8 needs about 1.25 GB and marks CIMD and resource indicators experimental,
Logto recommends an 8 GB host on its own, Pocket ID rejects ChatGPT's client metadata, Zitadel
ignores `resource`, authentik's DCR needs a token, and Authelia has no client registration.

The two clients need only a narrow slice of OAuth: authorization code with PKCE (S256), public
clients, the `resource` parameter, and client registration by Client ID Metadata Documents (CIMD).
Both identify themselves with an https `client_id` URL on their own host. We therefore add an
**optional built-in authorization server whose sign-in is the user's existing API key**
(`FOOTBALL_RESULTS_OAUTH_BUILTIN`). The consent page asks for the key; the server issues opaque,
hashed, short-lived access tokens bound to that key, so a token is exactly as powerful as the key,
uses the key's quotas, and stops working the moment the key expires or is revoked.

## Decision

- Off by default and mutually exclusive with an external issuer (`FOOTBALL_RESULTS_OAUTH_ISSUER`).
  It needs `FOOTBALL_RESULTS_PUBLIC_BASE_URL` (no path; https except on localhost) and at least one
  key, and it is refused in demo mode, where `/mcp` is anonymous anyway.
- The issuer is the public base URL; metadata is served at
  `/.well-known/oauth-authorization-server` (RFC 8414) with
  `client_id_metadata_document_supported` and `authorization_response_iss_parameter_supported`
  (RFC 9207), and only `none` as the token endpoint auth method.
- Clients register by CIMD only. A `client_id` must be an https URL whose host is in
  `FOOTBALL_RESULTS_OAUTH_CLIENT_HOSTS` (default `claude.ai,chatgpt.com`), which also bounds the
  outbound fetches the server makes. Redirect URIs must match the document exactly (loopback
  ports excepted, RFC 8252).
- No users, no passwords, no sessions, no cookies, no JavaScript. The consent page is a single
  form; a wrong key re-renders it.
- Tokens are opaque random strings stored as SHA-256 hashes in PostgreSQL. Codes live 5 minutes,
  access tokens 1 hour, refresh tokens 30 days (rotated on every use; reuse of a rotated token
  revokes the grant). Every use re-resolves the key, so a revoked or expired key cuts access at
  once. Expired rows are purged by the scheduler.
- The API remains a resource server for `/mcp` only; the REST API keeps `X-API-Key`.

## Considered options

- **Keycloak in production** (the ADR 0006 example): rejected for the home server. It works for
  claude.ai through DCR, but ChatGPT prefers CIMD (experimental in Keycloak 26.8), and it costs
  more memory than the API, scheduler, and database together.
- **Logto, Pocket ID, Zitadel, authentik, Authelia**: rejected for the gaps listed above.
- **Cloudflare Access managed OAuth**: rejected. It issues tokens for Cloudflare, not for this
  server, so the resource server could not check the audience or map the caller to a key.
- **Users and passwords in the built-in server**: rejected. Keys already are the identities,
  with names, roles, quotas, and expiry (ADR 0005, ADR 0008); a second identity store would need
  its own lifecycle.
- **Dynamic client registration (RFC 7591)**: not needed now. Both clients use CIMD when the
  server advertises it, and DCR would add stored, unauthenticated client records. Revisit if a
  client needs it.
- **JWT access tokens**: rejected. Opaque tokens checked against the database can be revoked with
  the key and need no signing keys to rotate.

## Consequences

- Reverses ADR 0006's rejection of a built-in authorization server, in this narrow form. External
  issuers keep working unchanged.
- Pasting an API key into a consent page is a phishing target. The page names the client and its
  host, tells the user to continue only if they started the connection, and the client host
  allowlist limits where codes can go.
- New tables for codes, grants, and tokens (migration `0014_oauth_server`), and the `/privacy`
  page describes them.
- Chat clients run from shared egress addresses, so `/oauth/token` shares one anonymous write
  quota per address; acceptable for small private instances.
- Client support is re-checked by connecting claude.ai and ChatGPT to the deployed private
  instance (roadmap 20g). Clients whose CIMD host is not in the default list need an operator
  setting.
