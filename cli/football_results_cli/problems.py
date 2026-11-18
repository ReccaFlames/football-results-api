"""Exit codes and RFC 9457 problem details written to stderr.

Exit codes and the CLI problem slugs are public names: scripts and agents branch on them, so add
new ones but never renumber or rename.
"""

import json
import sys
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_UPSTREAM = 4
EXIT_AUTH = 5

CLI_PROBLEM_PREFIX = "urn:football-results:cli:"

# Problems raised by the CLI itself (the server never returns them): slug -> (title, exit code).
CLI_PROBLEM_TYPES: dict[str, tuple[str, int]] = {
    "usage-error": ("Invalid command-line usage", EXIT_USAGE),
    "connection-failed": ("Cannot connect to the API", EXIT_UPSTREAM),
    "timeout": ("The API did not respond in time", EXIT_UPSTREAM),
    "invalid-response": ("Unexpected response from the API", EXIT_UPSTREAM),
    "stale-data": ("The data is stale", EXIT_UPSTREAM),
    "job-failed": ("The job failed", EXIT_UPSTREAM),
    "wait-timeout": ("The job did not finish in time", EXIT_UPSTREAM),
}


class CliFailure(Exception):
    """A failure to report as a problem on stderr, ending the process with ``exit_code``."""

    def __init__(self, problem: dict[str, Any], exit_code: int) -> None:
        super().__init__(problem.get("detail", ""))
        self.problem = problem
        self.exit_code = exit_code


def cli_problem(slug: str, detail: str, **extensions: Any) -> CliFailure:
    title, exit_code = CLI_PROBLEM_TYPES[slug]
    problem = {"type": f"{CLI_PROBLEM_PREFIX}{slug}", "title": title, "detail": detail}
    problem.update({key: value for key, value in extensions.items() if value is not None})
    return CliFailure(problem, exit_code)


def exit_code_for(status: int, problem_type: str | None = None) -> int:
    """Exit code for an HTTP error status and server problem ``type``."""
    if status in (401, 403) or (problem_type or "").endswith("/problems/writes-disabled"):
        return EXIT_AUTH
    if status == 404:
        return EXIT_NOT_FOUND
    if status in (409, 429) or status >= 500:
        return EXIT_UPSTREAM
    if 400 <= status < 500:
        return EXIT_USAGE
    return EXIT_UPSTREAM


def write_problem(problem: dict[str, Any]) -> None:
    sys.stderr.write(json.dumps(problem, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stderr.flush()
