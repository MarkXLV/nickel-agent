#!/usr/bin/env python3
"""Summarize one or more runner results.json files.

Usage: python scripts/summarize.py results1.json [results2.json ...]

Prints per-task status and an aggregate: solve rate, total spend, and
cost-per-solve (total dollars across ALL tasks / distinct tasks solved) — the
same efficiency metric used in the results table (total spend / tasks solved).
"""
import json
import sys
from collections import defaultdict


def main():
    files = sys.argv[1:]
    if not files:
        print("usage: summarize.py results.json [...]")
        sys.exit(2)

    # task -> list of (solved, passed, cost) across runs
    per_task = defaultdict(list)
    total_cost = 0.0
    for fn in files:
        for r in json.loads(open(fn).read()):
            per_task[r["task"]].append((r["solved"], r["passed"], r["est_cost_usd"]))
            total_cost += r["est_cost_usd"]

    print(f"{'task':30s} {'runs':>4s} {'solved':>7s} {'passed':>7s} {'avg$':>8s}")
    distinct_solved = 0
    n_runs = 0
    n_solved_instances = 0
    for task in sorted(per_task):
        runs = per_task[task]
        n_runs += len(runs)
        s = sum(x[0] for x in runs)
        p = sum(x[1] for x in runs)
        n_solved_instances += s
        avg = sum(x[2] for x in runs) / len(runs)
        if s == len(runs):
            distinct_solved += 1  # solved on every run
        flag = "" if s == len(runs) else "  <-- FLAKY/UNSOLVED" if s else "  <-- UNSOLVED"
        print(f"{task:30s} {len(runs):>4d} {s:>7d} {p:>7d} {avg:>8.4f}{flag}")

    n_tasks = len(per_task)
    solved_rate = n_solved_instances / n_runs if n_runs else 0
    cost_per_solve = total_cost / n_solved_instances if n_solved_instances else float("inf")
    print("-" * 60)
    print(f"tasks: {n_tasks}   task-runs: {n_runs}")
    print(f"solve rate (instances): {n_solved_instances}/{n_runs} = {solved_rate:.1%}")
    print(f"solved on EVERY run: {distinct_solved}/{n_tasks}")
    print(f"total spend: ${total_cost:.4f}")
    print(f"cost-per-solve: ${cost_per_solve:.4f}  "
          f"(efficiency vs $0.03 cap: {min(1, 0.03/cost_per_solve):.2f})")


if __name__ == "__main__":
    main()
