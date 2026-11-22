# Named API keys with roles and per-key quotas live in a hashed keys file

Every key in `FOOTBALL_RESULTS_API_KEYS` could write, read the admin endpoints, and scrape
`/metrics`, and a key changed only the rate-limit bucket, not the quota. Giving someone a key for
a higher quota would have made them an admin. Keys therefore get a **name**, a **role**
(`client`: reads only; `admin`: today's rights), and optional per-minute `read` and `write`
quotas. They live in an optional YAML file (`FOOTBALL_RESULTS_API_KEYS_FILE`) that stores only
`sha256:` hashes of random keys and is read at startup. Keys from the environment variable keep
working as admin keys named `env`. A valid key whose role does not allow an operation gets the
new problem `forbidden` (403); a missing or wrong key stays `unauthorized` (401). A keyed client
that runs out of quota gets the existing `rate-limited` (429), with `q` in `RateLimit-Policy`
showing its own quota. The header syntax stays at draft -11.

## Considered options

- **Keys in the database** with an admin endpoint and a migration: rejected for now as a larger
  public surface (operation, CLI command, MCP rule) for a single operator who edits a file anyway.
  A later step can import the file.
- **Extending the env-var syntax** (`name:role:quota:key`): rejected as unreadable and untyped,
  and it would put plain keys next to their metadata.
- **Daily quotas**: rejected for now. The limiter keeps counters in memory, so a daily window
  would reset on every restart; honest daily quotas need PostgreSQL counters.
- **A new `quota-exceeded` slug**: rejected. A per-key quota means the same as the existing limit,
  and clients already handle `rate-limited` and `Retry-After`.
- **bcrypt or argon2 hashes**: rejected. Keys are at least 32 random bytes, so a slow hash adds no
  protection and costs time on every request.

## Consequences

- Without a keys file, behaviour is unchanged: anonymous clients are counted per IP, and
  environment keys are admins.
- Counters stay per process: with several workers, each enforces the full per-key quota.
- Metrics label requests and `429`s by key **name** (`env`, `anonymous` for no key), never by the
  key, so cardinality is bounded by the file.
- The CLI sends `FOOTBALL_RESULTS_API_KEY` on every request, so a client key gets its quota on
  reads too.
- Changing keys needs a restart of the API.
