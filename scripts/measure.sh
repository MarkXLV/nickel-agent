#!/usr/bin/env bash
# Measurement harness for nickel-agent.
#
#   scripts/measure.sh smoke      # 1 task, sanity check
#   scripts/measure.sh ab         # baseline vs new, one task per category + held-out
#   scripts/measure.sh visible    # full practice suite x3 (gate: 10/10 SOLVED each)
#   scripts/measure.sh heldout    # held-out suite x3
#   scripts/measure.sh all        # ab -> visible -> heldout
#
# The OpenRouter key is read from ./.env (a line: export OPENROUTER_API_KEY=sk-or-...)
# or from the environment if already exported.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "ERROR: OPENROUTER_API_KEY not set (put it in ./.env or export it)"; exit 1
fi

mkdir -p results
CAT_TASKS="tasks/01-csv-merge-basic tasks/03-log-extract-errors tasks/05-pii-redact-basic tasks/07-script-repair-simple tasks/09-reconcile-inventory"
HELD="tasks/held-out/hz1-csv-merge-quoted tasks/held-out/hz2-log-extract-levels tasks/held-out/hz3-pii-redact-decoys tasks/held-out/hz4-extract-reconcile"

run() { uv run python -m runner.run_task "$@" --quiet; }

smoke() {
  echo "### SMOKE: tasks/01 (new agent)"
  run tasks/01-csv-merge-basic --out results/smoke.json
}

ab() {
  echo "### A/B: new agent vs baseline, per category + held-out"
  echo "--- NEW agent ---"
  run $CAT_TASKS $HELD --out results/ab_new.json
  echo "--- BASELINE agent (swapping run.py) ---"
  cp agent/run.py agent/.run_new.tmp
  cp scripts/agent_baseline.py agent/run.py
  run $CAT_TASKS $HELD --out results/ab_baseline.json
  cp agent/.run_new.tmp agent/run.py && rm -f agent/.run_new.tmp
  echo "=== NEW ==="; uv run python scripts/summarize.py results/ab_new.json
  echo "=== BASELINE ==="; uv run python scripts/summarize.py results/ab_baseline.json
}

visible() {
  for i in 1 2 3; do
    echo "### VISIBLE suite run $i/3"
    run tasks/* --out "results/visible_run$i.json"
  done
  uv run python scripts/summarize.py results/visible_run1.json results/visible_run2.json results/visible_run3.json
}

heldout() {
  for i in 1 2 3; do
    echo "### HELD-OUT suite run $i/3"
    run $HELD --out "results/heldout_run$i.json"
  done
  uv run python scripts/summarize.py results/heldout_run1.json results/heldout_run2.json results/heldout_run3.json
}

case "${1:-all}" in
  smoke) smoke ;;
  ab) ab ;;
  visible) visible ;;
  heldout) heldout ;;
  all) ab; visible; heldout ;;
  *) echo "usage: $0 {smoke|ab|visible|heldout|all}"; exit 2 ;;
esac
