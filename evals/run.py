"""Run the agent evals: every task through the CLI + skill and through MCP, with `copilot -p`.

    uv run python evals/run.py --dry-run          # ground truth and self-check, no agent runs
    uv run python evals/run.py                    # 11 tasks x 2 modes x 3 runs
    uv run python evals/run.py --tasks leader form --modes mcp --runs 1

See evals/README.md. Not part of pytest or CI: runs cost AI credits and need a live stack.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from tasks import TASKS, Expected, World, parse_answer, prompt_for

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills/football-results/SKILL.md"
RESULTS = ROOT / "evals/results"
MODES = ("cli", "mcp")
# Keep the agent on the tools under test: no file writes, memories, or direct HTTP.
COMMON_DENY = ("write", "memory", "url", "shell(curl:*)", "shell(wget:*)")


@dataclass
class Run:
    task: str
    mode: str
    attempt: int
    passed: bool
    reason: str
    answer: dict | None
    seconds: float
    tools: list[str]
    models: list[str]
    harness_error: str | None = None


@dataclass
class Transcript:
    """What a `copilot --output-format json` run did, read from its JSONL events."""

    text: str = ""
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    mcp_status: str | None = None
    warnings: list[str] = field(default_factory=list)


def read_events(jsonl: str) -> Transcript:
    transcript = Transcript()
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "assistant.message" and data.get("content"):
            transcript.text = data["content"]
            if data.get("model") and data["model"] not in transcript.models:
                transcript.models.append(data["model"])
        elif kind == "tool.execution_start":
            name = data.get("toolName", "?")
            if name in ("bash", "shell"):
                name = f"shell: {data.get('arguments', {}).get('command', '')}"
            transcript.tools.append(name)
        elif kind == "session.mcp_server_status_changed":
            if data.get("serverName") == "football-results":
                transcript.mcp_status = data.get("status")
        elif kind == "session.warning":
            transcript.warnings.append(data.get("message", ""))
    return transcript


def used_tool_under_test(mode: str, transcript: Transcript) -> bool:
    if mode == "cli":
        return any(t.startswith("shell: football-results") for t in transcript.tools)
    return any(t.startswith("football-results") for t in transcript.tools)


def harness_error(mode: str, transcript: Transcript, errors: str) -> str | None:
    """A reason when the run says nothing about the agent: the tools under test were missing."""
    if mode == "mcp" and transcript.mcp_status in ("not_configured", "failed", "disabled"):
        return f"MCP server {transcript.mcp_status}: {' '.join(transcript.warnings)}".strip()
    if not transcript.text and errors.strip():
        return errors.strip().splitlines()[-1]
    return None


def copilot_command(
    args: argparse.Namespace, mode: str, prompt: str, workdir: Path, log_dir: Path
) -> list[str]:
    command = [
        "copilot",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--no-ask-user",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--add-dir",
        str(workdir),
        "--log-dir",
        str(log_dir),
    ]
    deny = list(COMMON_DENY)
    if mode == "cli":
        command += ["--allow-tool", "shell(football-results:*)"]
    else:
        server = {"type": "http", "url": f"{args.base_url}/mcp", "tools": ["*"]}
        command += [
            "--additional-mcp-config",
            json.dumps({"mcpServers": {"football-results": server}}),
            "--allow-tool",
            "football-results",
        ]
        deny.append("shell")
    for rule in deny:
        command += ["--deny-tool", rule]
    if args.model:
        command += ["--model", args.model]
    if args.max_ai_credits:
        command += ["--max-ai-credits", str(args.max_ai_credits)]
    return command


def agent_env(args: argparse.Namespace) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "FOOTBALL_RESULTS_API_KEY"}
    env["FOOTBALL_RESULTS_URL"] = args.base_url
    # The workspace's `football-results` (uv sync installs it next to this interpreter).
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_once(
    args: argparse.Namespace, out: Path, task_id: str, expected: Expected, mode: str, attempt: int
) -> Run:
    name = f"{task_id}.{mode}.{attempt}"
    with tempfile.TemporaryDirectory(prefix=f"eval-{task_id}-") as tmp:
        workdir = Path(tmp)
        if mode == "cli":
            skill_dir = workdir / ".github/skills/football-results"
            skill_dir.mkdir(parents=True)
            shutil.copy(SKILL, skill_dir / "SKILL.md")
        started = datetime.now(UTC)
        try:
            completed = subprocess.run(
                copilot_command(args, mode, prompt_for(expected), workdir, out / f"logs/{name}"),
                cwd=workdir,
                env=agent_env(args),
                capture_output=True,
                check=False,
                text=True,
                timeout=args.timeout,
            )
            output, errors = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            output, errors = str(error.stdout or ""), f"timed out after {args.timeout}s"
        seconds = (datetime.now(UTC) - started).total_seconds()
    (out / f"{name}.jsonl").write_text(output)
    transcript = read_events(output)
    answer = parse_answer(transcript.text)
    problem = harness_error(mode, transcript, errors)
    if problem:
        passed, reason = False, f"harness: {problem}"
    elif answer is None:
        passed, reason = False, "no ANSWER line"
    elif not used_tool_under_test(mode, transcript):
        passed, reason = False, f"did not use the {mode} tools ({transcript.tools or 'none'})"
    else:
        passed, reason = expected.score(answer)
    tools = "\n".join(f"  {t}" for t in transcript.tools) or "  (none)"
    (out / f"{name}.txt").write_text(
        f"{prompt_for(expected)}\n\n--- tools ---\n{tools}\n--- reply ---\n{transcript.text}\n"
        f"--- stderr ---\n{errors}\n--- score ---\n{'PASS' if passed else 'FAIL'}: {reason}\n"
    )
    print(f"  {task_id:<20} {mode:<4} #{attempt}  {'PASS' if passed else 'FAIL'}  {reason}")
    return Run(
        task_id,
        mode,
        attempt,
        passed,
        reason,
        answer,
        round(seconds, 1),
        transcript.tools,
        transcript.models,
        problem,
    )


def dry_run(args: argparse.Namespace, cases: dict[str, Expected]) -> int:
    failures = 0
    for task_id, expected in cases.items():
        passed, reason = expected.score(expected.truth)
        failures += not passed
        print(f"## {task_id}  self-check: {'ok' if passed else 'FAILED ' + reason}")
        print(prompt_for(expected))
        print(f"expected: {json.dumps(expected.truth)}\n")
    for mode in args.modes:
        command = copilot_command(args, mode, "<prompt>", Path("<workdir>"), Path("<logs>"))
        print(f"{mode} command:", " ".join(command))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000", help="API under test")
    parser.add_argument("--tasks", nargs="*", choices=[t.id for t in TASKS], help="default: all")
    parser.add_argument("--modes", nargs="*", choices=MODES, default=list(MODES))
    parser.add_argument("--runs", type=int, default=3, help="runs per task and mode")
    parser.add_argument("--pass-ratio", type=float, default=2 / 3, help="share of runs to pass")
    parser.add_argument("--jobs", type=int, default=2, help="agent runs in parallel")
    parser.add_argument("--model", help="copilot --model")
    parser.add_argument("--max-ai-credits", type=float, help="copilot --max-ai-credits per run")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per agent run")
    parser.add_argument("--dry-run", action="store_true", help="print tasks and ground truth")
    args = parser.parse_args(argv)
    args.base_url = args.base_url.rstrip("/")

    with httpx.Client(base_url=args.base_url, timeout=30) as api:
        api.get("/health").raise_for_status()
        world = World(api)
        selected = [t for t in TASKS if not args.tasks or t.id in args.tasks]
        cases = {task.id: task.build(world) for task in selected}
        if args.dry_run:
            return dry_run(args, cases)
        if not shutil.which("copilot"):
            print("copilot is not on PATH", file=sys.stderr)
            return 2

        out = RESULTS / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out.mkdir(parents=True)
        print(f"{world.name} {world.season} at {args.base_url}; transcripts in {out}")
        jobs = [
            (task_id, expected, mode, attempt)
            for task_id, expected in cases.items()
            for mode in args.modes
            for attempt in range(1, args.runs + 1)
        ]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            runs = list(pool.map(lambda job: run_once(args, out, *job), jobs))

    needed = math.ceil(args.runs * args.pass_ratio)
    rows = []
    for task_id in cases:
        for mode in args.modes:
            passes = sum(r.passed for r in runs if r.task == task_id and r.mode == mode)
            rows.append({"task": task_id, "mode": mode, "passes": passes, "ok": passes >= needed})
    summary = {
        "base_url": args.base_url,
        "competition": world.slug,
        "season": world.season,
        "model": args.model,
        "runs_per_task": args.runs,
        "passes_needed": needed,
        "tasks": rows,
        "runs": [asdict(r) for r in runs],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print(f"\n{'task':<20} " + " ".join(f"{m:>6}" for m in args.modes))
    for task_id in cases:
        cells = [
            f"{r['passes']}/{args.runs}{'' if r['ok'] else '!':<1}"
            for m in args.modes
            for r in rows
            if r["task"] == task_id and r["mode"] == m
        ]
        print(f"{task_id:<20} " + " ".join(f"{c:>6}" for c in cells))
    failed = [r for r in rows if not r["ok"]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} task-modes pass (need {needed}/{args.runs})")
    print("models:", ", ".join(sorted({m for r in runs for m in r.models})) or "unknown")
    broken = sorted({f"{r.mode}: {r.harness_error}" for r in runs if r.harness_error})
    for problem in broken:
        print(f"HARNESS ERROR (not an agent failure) {problem}", file=sys.stderr)
    return 2 if broken else 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
