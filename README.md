# nickel-agent

A coding agent for file-wrangling tasks — CSV merges, log extraction, PII redaction, script repair, reconciliation — that has to be **correct and cheap**. A task counts only if it passes a deterministic verifier **and** stays under **$0.05**.

The naive version of this agent scored **2/10** on the practice suite at **~$0.74/run**. After the rewrite it is **10/10** at **~$0.24/run**, and it holds that result on held-out tasks it was never tuned on.

The model is pinned to **Claude Haiku 4.5** via OpenRouter.

---

## Idea

The model should **never transform data in the context window**. It writes one small Python program that reads and writes files on disk with real parsers (`csv` / `json` / `pandas`), runs it, cheaply checks its own output, and stops.

That one bet fixes the two failure modes of a typical ReAct-over-bash agent:

1. **Cost.** `cat`-ing data into the prompt re-bills the file on every turn. Task 10 used to burn 114k tokens (~$0.14). It now costs ~$0.027.
2. **Correctness.** Shell one-liners break on exactly what verifiers check: quoting, key order, sort type, leading zeros, CRLF, rounding, timezones.

Prompt caching (Anthropic via OpenRouter) makes the remaining multi-turn loop cheap: the system playbook, the task text, and the growing prefix are re-read at 0.1×.

---

## Results

Practice suite, average of 3 runs vs the original agent:

| task | baseline | nickel-agent | cheaper |
|---|---|---|---|
| 01 csv-merge | FAIL · $0.025 · 18k tok | **SOLVED · $0.016 · 13k** | 1.5× |
| 02 csv-normalize | OVERBUDGET · $0.130 · **110k** | **SOLVED · $0.020 · 5k** | 6.4× |
| 03 log-extract | SOLVED · $0.030 · 24k | SOLVED · $0.023 · 11k | 1.3× |
| 04 log-sessionize | OVERBUDGET · $0.080 · 65k | **SOLVED · $0.023 · 9k** | 3.4× |
| 05 pii-basic | OVERBUDGET · $0.080 · 67k | **SOLVED · $0.029 · 14k** | 2.7× |
| 06 pii-names | OVERBUDGET · $0.084 · 73k | **SOLVED · $0.031 · 15k** | 2.7× |
| 07 script-simple | SOLVED · $0.043 · 36k | SOLVED · $0.025 · 14k | 1.7× |
| 08 script-pandas | OVERBUDGET · $0.056 · 48k | **SOLVED · $0.029 · 11k** | 1.9× |
| 09 reconcile-inv | OVERBUDGET · $0.068 · 57k | **SOLVED · $0.020 · 9k** | 3.4× |
| 10 reconcile-events | OVERBUDGET · $0.143 · **114k** | **SOLVED · $0.027 · 10k** | 5.4× |
| **suite** | **2/10 · $0.74** | **10/10 · $0.24** | — |

Reliability checks:

- Practice suite, 3 repeated runs: **30/30 solved**, cost-per-solve **$0.0244**
- Held-out suite I wrote (4 harder tasks, including a combined extract-then-reconcile): **12/12 solved**, cost-per-solve **$0.0242**
- Worst single-task cost: **$0.032**, so there is margin for an extra turn

The held-out suite also caught a real bug the practice tasks missed: an early version staged a backup at a fixed `/tmp` path and leaked one PII task's files into another. That is now forbidden.

---

## What changed

All of this lives in `agent/run.py`:

- **Script-first discipline.** Token use becomes independent of file size; real parsers get quoting, encoding, and key order right by construction.
- **Prompt caching, with honest usage accounting.** `log_usage` matches OpenRouter's reported `cost` to 5 decimals, including cache-write at 1.25× and cache-read at 0.1×.
- **Operator playbook.** The system prompt encodes *kinds* of traps (preserve values byte-for-byte, exact sort semantics, redact only actual PII, no stray files) rather than answers to specific tasks.
- **Bounded self-verification.** The loop will not accept "done" until the model has actually run and checked its work. `temperature=0` for run-to-run stability. Anti-patterns: don't re-verify endlessly, don't reserialize files, don't touch shared `/tmp` paths.

The original naive agent is kept at `scripts/agent_baseline.py` for A/B runs.

---

## Setup

Requires Python 3.12+ and an [OpenRouter](https://openrouter.ai) key.

```bash
# install uv if you don't have it, or use pip (step 3b)
curl -LsSf https://astral.sh/uv/install.sh | sh

cd nickel-agent
uv sync
# 3b. alternatively: pip install openai pandas pytest

export OPENROUTER_API_KEY=sk-or-v1-...
```

Smoke test (~30s, ~$0.02):

```bash
uv run python -m runner.run_task tasks/01-csv-merge-basic
```

You should see a line like `SOLVED 01-csv-merge-basic (... ≈$0.019 / $0.05 budget)`.

---

## Running tasks

```bash
# one task
uv run python -m runner.run_task tasks/01-csv-merge-basic

# the whole practice suite
uv run python -m runner.run_task tasks/*

# keep the temp run dir to inspect what the agent wrote
uv run python -m runner.run_task tasks/05-pii-redact-basic --keep-run-dir

# held-out tasks (harder variants I built)
uv run python -m runner.run_task tasks/held-out/*
```

A task is `SOLVED` only if it passes `verify.py` **and** spends ≤ $0.05. Correct-but-expensive is `OVERBUDGET`. Commands stream live on stderr; every result line shows cost against the budget.

```
SOLVED      01-csv-merge-basic        (11s,  6 calls,  14,390 tokens, ≈$0.019 / $0.05 budget)
```

---

## Layout

```
agent/run.py              the agent — script-first loop, caching, playbook
runner/                   task runner, usage accounting, verify.py harness
tasks/                    10 practice tasks: task.md + workspace/ + verify.py
tasks/held-out/           4 harder tasks the agent was never tuned on
scripts/
  agent_baseline.py       original naive agent, for A/B
  measure.sh              suite measurement harness
  summarize.py            roll up cost / solve rate
```

Each task is `SOLVED` only when its own `verify.py` accepts the output. Verifiers check outputs, never methods.
