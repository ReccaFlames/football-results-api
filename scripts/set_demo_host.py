"""Set the public demo host in the files that ship it (ADR 0009).

    uv run python scripts/set_demo_host.py football.example.com
    uv run python scripts/set_demo_host.py --check   # exit 1 while a placeholder remains

The release workflow runs `--check`, so nothing is published with a placeholder.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "FOOTBALL_DEMO_HOST"
SERVER_JSON = Path("server.json")
CLIENT = Path("cli/football_results_cli/client.py")
UNSET_DEMO_URL = "DEMO_URL: str | None = None"
HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def placeholders(root: Path = ROOT) -> list[str]:
    """Return a description of every placeholder still present."""
    found = []
    if PLACEHOLDER in (root / SERVER_JSON).read_text():
        found.append(f"{SERVER_JSON}: {PLACEHOLDER}")
    if UNSET_DEMO_URL in (root / CLIENT).read_text():
        found.append(f"{CLIENT}: {UNSET_DEMO_URL}")
    return found


def set_demo_host(host: str, root: Path = ROOT) -> None:
    host = host.strip().lower()
    if not HOST.match(host):
        raise ValueError(f"not a host name: {host!r} (no scheme, port, or path)")
    if not placeholders(root):
        raise ValueError("the demo host is already set; edit the files by hand to change it")
    server = root / SERVER_JSON
    server.write_text(server.read_text().replace(PLACEHOLDER, host))
    client = root / CLIENT
    client.write_text(
        client.read_text().replace(UNSET_DEMO_URL, f'DEMO_URL: str | None = "https://{host}"')
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("host", nargs="?", help="demo host name, e.g. football.example.com")
    group.add_argument("--check", action="store_true", help="fail while a placeholder remains")
    args = parser.parse_args(argv)
    if args.check:
        remaining = placeholders()
        for item in remaining:
            print(f"placeholder: {item}", file=sys.stderr)
        return 1 if remaining else 0
    try:
        set_demo_host(args.host)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    print(f"demo host set to {args.host}; run the tests and commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
