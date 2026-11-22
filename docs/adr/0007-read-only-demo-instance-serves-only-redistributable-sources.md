# The demo instance is read-only and serves only sources that permit public redistribution

A public demo instance helps people and agents try the API without self-hosting. Provider terms
decide what it may serve. Disney's terms restrict automated extraction and public distribution of
ESPN data, and the Premier League and the Europa League fallback come from ESPN. TheSportsDB's
free tier and 90minut.pl give no clear permission to redistribute publicly. OpenLigaDB's data is
licensed under the ODbL, which allows it with attribution. The demo therefore serves **only the
3. Liga** at launch, from its own competition file. It is **read-only**: no admin keys, so writes
return `writes-disabled`. It runs MCP anonymously, without OAuth. The repository ships the
configuration, a deployment guide (Cloudflare free in front, Oracle Cloud Always Free as the host),
and a demo notice in `/llms.txt` and `/agents`. Running a public instance stays an operator
decision outside the roadmap.

## Considered options

- **Serve every configured competition**: rejected because of ESPN's terms and the unclear
  permissions of TheSportsDB and 90minut.pl.
- **Add the 90minut.pl competitions now**: deferred until 90minut.pl grants written permission.
- **A paid host or managed PostgreSQL**: rejected so that the demo stays as free as self-hosting.
- **Sign-up or CAPTCHA**: rejected. Low per-IP quotas, caching, and optional client keys are
  enough for public read-only data.

## Consequences

- Demo mode rejects, at startup and in `config check`, competitions whose primary or fallback
  provider is not on the allow-list of publicly redistributable providers. Adding one needs a
  documented permission and a code change.
- Under the ODbL, the demo must keep the OpenLigaDB attribution. A database derived from it is
  subject to share-alike.
- The demo shows a small part of the API (one competition with no cards, corners, or fouls).
