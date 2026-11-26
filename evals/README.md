# Agent evals

Can an agent answer real questions with this API? These evals ask `copilot -p` 11 questions
through the two first-class paths: the **CLI + Agent Skill** (`cli`) and the **MCP server**
(`mcp`). Ground truth is read from the API when the eval runs, so the tasks work on any instance
and season. They are not part of pytest or CI: runs cost AI credits and need a live stack.

| Task | Kind | Asks for |
| --- | --- | --- |
| `competitions` | top task | every competition slug |
| `leader` | top task | the table leader and points |
| `last-result` | top task | a team's latest result |
| `next-fixture` | top task | a team's next opponent and date |
| `form` | top task | a team's last five results |
| `misspelled-team` | error recovery | points of a team named with a typo |
| `unknown-competition` | error recovery | a league the instance does not have (must say so) |
| `missing-season` | error recovery | a season that does not exist (must say so) |
| `season-by-year` | error recovery | a season named by its start year |
| `freshness` | freshness | when the data was last updated |
| `finished-count` | pagination | matches played so far (needs every page) |

The agent ends its reply with `ANSWER: {json}`. A run passes when that JSON matches the truth
**and** the agent used the tools under test (`football-results` in a shell, or a
`football-results` MCP tool). A task passes in a mode when 2 of 3 runs pass.

## Run

~~~bash
# 1. A local demo-mode stack (the 3. Liga, read-only) on http://localhost:8000
POSTGRES_PASSWORD=$(openssl rand -hex 32) FOOTBALL_RESULTS_OPERATOR_CONTACT=evals@localhost \
  docker compose -p football-evals -f compose.yaml -f compose.demo.yaml -f evals/compose.yaml \
  up -d --build --wait
# wait until the scheduler has imported the season: football-results standings -c 3-liga

# 2. Ground truth and scorer self-check (no agent runs, no credits)
uv run python evals/run.py --dry-run

# 3. Everything: 11 tasks x 2 modes x 3 runs = 66 agent runs
uv run python evals/run.py
uv run python evals/run.py --tasks leader form --modes cli --runs 1   # a cheap subset

# 4. Clean up
POSTGRES_PASSWORD=unused docker compose -p football-evals -f compose.yaml -f compose.demo.yaml \
  -f evals/compose.yaml down -v
~~~

Options: `--base-url` (another instance, such as the public demo; mind its read quota and use
`--jobs 1`), `--model`, `--max-ai-credits` (per run), `--jobs` (parallel runs, default 2),
`--timeout`. Each run gets an empty working directory: in `cli` mode it holds only the consumer
skill in `.github/skills/`, and the agent may run only `football-results`; in `mcp` mode the
shell is denied and the only extra tool is the MCP server. File writes, memories, URL fetches,
`curl`, and `wget` are denied in both.

Results go to `evals/results/<UTC time>/` (git-ignored): a transcript per run (`.txt`: tools
used, reply, score; `.jsonl`: raw events), copilot logs, and `summary.json`. Exit code: 0 all
pass, 1 a task failed, 2 a harness error.

**Harness errors are not agent failures.** A run is a harness error when the tools under test
never reached the agent, for example when your organisation's Copilot policy disables
third-party MCP servers (`MCP server not_configured`). Run the `mcp` mode with an account that
allows them. An administrator policy can also replace `--model`; `summary.json` records the
models that actually answered.

## Chat clients

claude.ai and ChatGPT cannot be scripted for free; check them by hand with
[CHAT_CLIENTS.md](CHAT_CLIENTS.md) after the demo is live.
