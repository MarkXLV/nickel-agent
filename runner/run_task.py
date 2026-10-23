#!/usr/bin/env python3
"""Agent Gauntlet task runner.

Runs your agent against one or more tasks, then verifies the results.

    python -m runner.run_task tasks/01-csv-merge-basic
    python -m runner.run_task tasks/*

This runner is the evaluation harness. Keep it stable so local numbers
match the usage log the agent writes.

Contract:
  - The agent is invoked as `python -m agent.run --task-dir <run_dir> --model <model>`
    with cwd set to --agent-root (the directory containing the agent/ package).
  - The run dir contains a copy of task.md, workspace/, and meta.json. The
    original task directory (including verify.py) is never exposed to the agent.
  - The agent (or anything it spawns) should append one JSON line per API call
    to the file named by $GAUNTLET_USAGE_LOG:  {"input_tokens": N, "output_tokens": N}
  - verify.py runs afterwards as `python verify.py <run_workspace>`; exit 0 = pass.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"  # Claude, served via OpenRouter
DEFAULT_TIMEOUT = 600  # seconds, per task
DEFAULT_TASK_BUDGET = 0.05  # dollars; a task is SOLVED only if it passes within this
VERIFY_TIMEOUT = 180

# $/MTok, for the on-the-fly cost estimate (longest prefix wins).
PRICES = {
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
}


def est_cost_usd(model, tokens_in, tokens_out, cache_write=0, cache_read=0):
    matches = [m for m in PRICES if model.startswith(m)]
    price_in, price_out = PRICES[max(matches, key=len)] if matches else (1.00, 5.00)
    return (tokens_in / 1e6 * price_in
            + cache_write / 1e6 * price_in * 1.25
            + cache_read / 1e6 * price_in * 0.10
            + tokens_out / 1e6 * price_out)


def run_one(task_dir, agent_root, model=DEFAULT_MODEL, timeout=DEFAULT_TIMEOUT,
            keep_run_dir=False, stream_output=False):
    """Run the agent on a single task and verify. Returns a result dict.

    With stream_output=True the agent's stderr (the baseline agent narrates
    every command there) passes through to the terminal live."""
    task_dir = Path(task_dir).resolve()
    agent_root = Path(agent_root).resolve()
    run_dir = Path(tempfile.mkdtemp(prefix=f"gauntlet-{task_dir.name}-"))

    shutil.copy2(task_dir / "task.md", run_dir / "task.md")
    shutil.copytree(task_dir / "workspace", run_dir / "workspace")
    (run_dir / "meta.json").write_text(json.dumps({"task": task_dir.name}))

    usage_log = run_dir / "usage.jsonl"
    env = os.environ.copy()
    env["GAUNTLET_USAGE_LOG"] = str(usage_log)

    timed_out = False
    error = None
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.run",
         "--task-dir", str(run_dir), "--model", model],
        cwd=agent_root, env=env,
        stdout=subprocess.PIPE,
        stderr=None if stream_output else subprocess.PIPE,  # None = live to terminal
        text=True,
        start_new_session=True,  # own process group, so we can kill children too
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            error = (stderr or "").strip()[-2000:] or f"agent exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    wall_secs = time.monotonic() - started

    try:
        verify = subprocess.run(
            [sys.executable, str(task_dir / "verify.py"), str(run_dir / "workspace")],
            capture_output=True, text=True, timeout=VERIFY_TIMEOUT,
        )
        passed = verify.returncode == 0
        verify_msg = (verify.stdout + verify.stderr).strip()[:500]
    except subprocess.TimeoutExpired:
        passed = False
        verify_msg = "verify.py timed out"

    calls = tokens_in = tokens_out = cache_write = cache_read = 0
    if usage_log.exists():
        for line in usage_log.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls += 1
            tokens_in += int(entry.get("input_tokens", 0))
            tokens_out += int(entry.get("output_tokens", 0))
            cache_write += int(entry.get("cache_write_tokens", 0))
            cache_read += int(entry.get("cache_read_tokens", 0))

    if keep_run_dir:
        print(f"  run dir kept: {run_dir}", file=sys.stderr)
    else:
        shutil.rmtree(run_dir, ignore_errors=True)

    cost = est_cost_usd(model, tokens_in, tokens_out, cache_write, cache_read)
    return {
        "task": task_dir.name,
        "passed": passed,
        "solved": passed and cost <= DEFAULT_TASK_BUDGET,
        "wall_secs": round(wall_secs, 1),
        "calls": calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "est_cost_usd": round(cost, 4),
        "timed_out": timed_out,
        "error": error,
        "verify_msg": verify_msg if not passed else "",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks", nargs="+", help="task directories to run")
    parser.add_argument("--agent-root", default=str(Path(__file__).parent.parent),
                        help="directory containing the agent/ package (default: this kit)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--out", default="results.json", help="aggregate results file")
    parser.add_argument("--keep-run-dir", action="store_true",
                        help="keep the temp run dir for debugging")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress live agent output (grading uses this)")
    args = parser.parse_args()

    results = []
    tasks = [t for t in args.tasks if (Path(t) / "task.md").exists()]
    for skipped in set(args.tasks) - {str(t) for t in tasks}:
        print(f"skipping {skipped}: no task.md", file=sys.stderr)
    for i, task in enumerate(tasks, 1):
        task_path = Path(task)
        print(f"[{i}/{len(tasks)}] {task_path.name} — agent running "
              f"(up to {args.timeout}s)...", flush=True)
        result = run_one(task_path, args.agent_root, args.model,
                         args.timeout, args.keep_run_dir,
                         stream_output=not args.quiet)
        results.append(result)
        if result["solved"]:
            status = "SOLVED"
        elif result["passed"]:
            status = "OVERBUDGET"  # correct, but too expensive to count
        elif result["timed_out"]:
            status = "TIMEOUT"
        else:
            status = "FAIL"
        tokens = result["tokens_in"] + result["tokens_out"]
        print(f"{status:11s} {result['task']}  "
              f"({result['wall_secs']}s, {result['calls']} calls, {tokens:,} tokens, "
              f"≈${result['est_cost_usd']:.3f} / ${DEFAULT_TASK_BUDGET:.2f} budget)")
        if not result["passed"] and result["verify_msg"]:
            print(f"        verify: {result['verify_msg']}")
        if result["error"]:
            print(f"        agent error: {result['error'][:300]}")

    solved = sum(r["solved"] for r in results)
    passed = sum(r["passed"] for r in results)
    total_cost = sum(r["est_cost_usd"] for r in results)
    print(f"\n{solved}/{len(results)} solved "
          f"({passed - solved} passed but over budget), ≈${total_cost:.2f} spent")
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"results written to {args.out}")


if __name__ == "__main__":
    main()
