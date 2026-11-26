# The published CLI defaults to the demo instance

The CLI is published to PyPI as `football-results-cli`, and the consumer skill teaches agents to
run it with `uvx`. With the old default of `http://localhost:8000`, the first command anyone
outside a checkout runs fails, and the agent has to work out `FOOTBALL_RESULTS_URL` on its own.
We therefore make the CLI's default URL the **public demo instance** (ADR 0007).
`FOOTBALL_RESULTS_URL` and `--url` still override it, and they are the only way to reach a
private or self-hosted instance. The demo host is not known until it is deployed. Until then the
default stays `http://localhost:8000`, and a failed connection to that default says that
`FOOTBALL_RESULTS_URL` is unset and how to set it. The host goes into one constant (`DEMO_URL`
in `cli/football_results_cli/client.py`) through `scripts/set_demo_host.py`. The release workflow
refuses to publish while the placeholder is still in place.

## Considered options

- **Keep `localhost`**: rejected. It works only in a checkout with a local stack, which is not
  how a published CLI is used.
- **No default (the URL is required)**: rejected. It costs every new user and agent one failed
  command, and the demo is safe to call anonymously.

## Consequences

- Installed CLIs keep calling the demo host they were built with. Moving the demo to another host
  needs a redirect on the old host or a new CLI release. The CLI does not follow redirects (it
  must not send a key to another host), so in practice it needs a new release.
- A key in `FOOTBALL_RESULTS_API_KEY` belongs to the instance that issued it. The CLI therefore
  refuses (`usage-error`, exit 2) to send a key to the demo default. A caller with a key must set
  `FOOTBALL_RESULTS_URL` or `--url` explicitly, even to the demo host.
