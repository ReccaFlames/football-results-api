# `/mcp` is an optional OAuth resource server for an external authorization server

Hosted chat clients cannot always send an `X-API-Key` header, but MCP 2026-07-28 lets them use
OAuth. Authorization is optional in the spec. When a server offers it, the server must publish
protected resource metadata (RFC 9728), check that a token was issued for it (audience), and never
forward the token. We make `/mcp` an **optional resource server**: an operator who sets an issuer
gets `/.well-known/oauth-protected-resource/mcp`. JWT access tokens are validated against the
issuer's JWKS (signature, issuer, audience = the public `/mcp` URL, expiry) and map to the
identities of [ADR 0005](0005-named-api-keys-with-roles-and-quotas.md). A keys-file entry can name
a token `subject` instead of a key hash. Unknown subjects become `client`s with the default keyed
quotas. API keys keep working, and anonymous read tools stay available unless the operator sets
`required`. OAuth covers `/mcp` only; the REST API keeps `X-API-Key`.

## Considered options

- **Built-in authorization server** (the `mcp` SDK's `OAuthAuthorizationServerProvider`):
  rejected. It needs users, login, consent, token storage, key rotation, and client registration
  (CIMD or DCR), which is a large, security-sensitive surface for a single operator.
- **The SDK's `auth=` and `token_verifier=` on `MCPServer`**: does not work here. The checks and
  the metadata route live in the SDK's Starlette app, which `McpEndpoint` bypasses, and the SDK
  guard cannot allow anonymous callers. We reuse its `TokenVerifier` and `AccessToken` types but
  run our own check.
- **GitHub OAuth tokens directly**: rejected. They are issued for GitHub's API, not for this
  server, so accepting them breaks the audience rule.
- **OAuth required whenever it is enabled**: rejected as the default, because the read tools are
  public data and work anonymously today.

## Consequences

- An operator who wants OAuth must run an authorization server (the docs walk through Keycloak in
  an optional Compose profile). Without one, nothing changes.
- Inner tool calls carry the resolved identity in the ASGI scope, never the token, so
  `request_refresh` works for an admin subject without token passthrough.
- Client support for OAuth (registration by CIMD, DCR, or pre-registration) varies and is
  re-checked before implementation. API keys remain the fallback for clients that can send headers.
- Adds the PyJWT dependency.
