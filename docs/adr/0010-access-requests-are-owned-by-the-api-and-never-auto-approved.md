# Access requests are owned by the API and never auto-approved

A private instance (ADR 0008) needs a way for people and agents to ask for a key. We add an
**access request**: anyone may create one through a public API operation (also on a private
instance), which the landing page form and agents both use. The API stores it in PostgreSQL and
notifies the operator (Telegram, without the contact email). A request never grants access by
itself: the operator approves it on the server with `python -m app.config access-requests approve`,
which issues an expiring named key with the existing keys-file helpers, or rejects it. The
requester gets a one-time **claim secret** when creating the request and uses it to read the status
and, after approval, the key exactly once. Requests not approved are deleted after 30 days.

## Considered options

- **Automatic key issuance**: rejected. It would make the private instance public in practice and
  break ADR 0008 (invited people only, providers' permission tied to each key).
- **The landing app stores requests**: rejected. The landing app stays a thin, stateless front on
  the same origin; agents would otherwise need a second, undocumented interface.
- **Approval through an admin API operation**: rejected. The API would need write access to the
  keys file, which today only the server-side helpers edit.
- **Deliver keys by email or Telegram**: rejected. It needs SMTP, puts secrets into third-party
  channels, and does not work for agents. The claim secret works the same for humans and agents.

## Consequences

- `create_access_request` and `list_competitions` are public operations even on a private
  instance; the landing page needs no key of its own.
- The landing app forwards the client IP and is a trusted proxy, so the per-IP request limit
  counts visitors, not the landing app.
- A lost claim secret cannot be recovered; the requester creates a new request.
