

You're given a working-but-bad LLM agent, ten visible tasks, and a $15 API
key. Your job: rewrite the agent so it solves tasks it has never seen —
**correctly, cheaply, and every single run**. You're graded on two things:
an automated run against a hidden task suite, and a short write-up of what
you changed and why you believe it generalizes.

```
runner/     task runner — OFF-LIMITS (grading uses this exact runner)
agent/      the agent — this is what you rewrite
tasks/      10 visible practice tasks: task.md + workspace/ + verify.py
```

---

## 1. Setup

Run these exact steps from a terminal:

```bash
# 1. install uv if you don't have it (or use plain pip, step 3b)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. go to this folder
cd agent

# 3. install dependencies (creates .venv/ on first run, ~30s)
uv sync
# 3b. alternatively, without uv:  pip install anthropic pandas pytest

# 4. set the API key we sent you — in THIS terminal (repeat per new terminal)
export OPENROUTER_API_KEY=sk-or-v1-...

# 5. smoke test: run the agent on one task (~30s, ~$0.02)
uv run python -m runner.run_task tasks/01-csv-merge-basic
```

Step 5 should stream the agent's commands live and end with a line like
`SOLVED 01-csv-merge-basic (... ≈$0.019 / $0.05 budget)`. If it prints an
authentication error, the key isn't exported in this terminal (check with
`echo $OPENROUTER_API_KEY`). If `tasks/...` isn't found, you're not inside
the `specific-labs-takehome` folder.

Notes:

- The model is pinned to **Claude Haiku 4.5, served via OpenRouter**
  (model id `anthropic/claude-haiku-4.5`, endpoint
  `https://openrouter.ai/api/v1`). Your agent may only call this model,
  with this key. No other models, providers, or external services.
- **Your key has a hard $15 limit (enforced by OpenRouter) and it covers
  everything, including your development iterations.** A full visible-suite run costs the baseline
  agent ≈ $0.85, so budget roughly 15 whole-suite runs — iterate on single
  tasks, not the whole suite. If you burn the cap, contact your recruiter;
  a replacement is discretionary and noted for grading.
- Suggested effort: 4–6 hours. AI coding assistants (Claude Code, Cursor,
  etc.) are explicitly allowed — your write-up should make it obvious that
  you understand every change you submitted.

## 2. The task

Modify anything under `agent/`. Do not touch `runner/` — grading uses the
pristine runner, so local changes to it only make your results lie to you.

**A task is SOLVED only if your agent passes its verifier while spending
≤ $0.05 on that task.** Correct-but-expensive counts for nothing.

**How grading works: after you submit your repo, we take your agent —
unmodified — and run it against a hidden test suite on our machine.** That
run is your grade. The hidden suite is **14 tasks** in the same five
categories as the visible ones (file wrangling, log extraction, PII
redaction, script repair, reconciliation) with different specifics. Each
hidden task runs **twice**; a task that passes once and fails once earns
half credit, so flakiness costs real points. 10-minute wall-clock cap per
run.

**Your minimum goal before submitting: all 10 visible tasks at SOLVED —
consistently, across repeated runs.** The visible suite is your only
rehearsal for the hidden one; if it isn't at 10/10, the hidden suite will
not be kinder.

```
solved      = passed verification AND ≤ $0.05 spent on that task
final score = 0.75 × hidden solve rate + 0.25 × efficiency
efficiency  = min(1, $0.03 / cost-per-solve)
```

`cost-per-solve` = ALL dollars your agent burns during the graded runs
(wasted spend on failed and over-budget tasks included) ÷ distinct tasks
solved. Cost is measured from the usage log your agent writes (see the
contract in `agent/run.py`) — keep it honest; we cross-check it against
your key's OpenRouter usage ledger. Cache-write tokens bill at 1.25× the
input rate and cache-read tokens at 0.1×, same as the real API.

For reference: the unmodified agent scores **~6%**. Strong submissions
score **70+**. Hardcoding visible-task answers gains nothing — hidden tasks
differ in every specific.

## 3. Running the test suite

```bash
# one task (do this while iterating)
uv run python -m runner.run_task tasks/01-csv-merge-basic

# the whole visible suite
uv run python -m runner.run_task tasks/*

# keep the temp run dir to inspect what your agent actually did
uv run python -m runner.run_task tasks/05-pii-redact-basic --keep-run-dir
```

While a task runs, your agent's commands stream live (the baseline narrates
on stderr — keep that habit). Every result line shows cost against the
budget. Out of the box you'll see something like:

```
SOLVED      01-csv-merge-basic        (11s,  6 calls,  14,390 tokens, ≈$0.019 / $0.05 budget)
OVERBUDGET  02-csv-schema-normalize   (59s, 26 calls, 167,970 tokens, ≈$0.193 / $0.05 budget)
OVERBUDGET  03-log-extract-errors     (36s, 13 calls,  62,687 tokens, ≈$0.075 / $0.05 budget)
...
2/10 solved (8 passed but over budget), ≈$0.83 spent
```

`OVERBUDGET` means the work was correct but cost more than a nickel — worth
zero. Visible tasks ship with their verifiers (`verify.py`) — read them;
they define exactly what "done" means, mechanically.

## 4. The agent you're given is bad on purpose

`agent/run.py` is ~100 lines of naive loop: ask the model for a bash
command, run it, feed the output back, repeat until the model stops asking.
It works — but it spends tokens lavishly (which is why it blows the $0.05
budget on most tasks) and it trusts the model to decide when the work is
done (which is why its correctness cracks on unfamiliar tasks).

Diagnosing exactly *where* the money and the mistakes come from is part of
the assignment. Your instruments: the live command narration while a task
runs, the per-task call/token/cost lines, and `--keep-run-dir` to inspect
what your agent actually left behind. Rewrite it however you like — the
only contract you must preserve is documented at the top of the file. And
measure: intuitions about what helps should survive an A/B run before you
trust them.

## 5. About the hidden tasks (a hint)

Thinking up and building your own *harder* task variants — then running your
agent against them — is a good way to approach the hidden suite.

- Same five categories, same task anatomy, same $0.05 budget, same pinned
  model. Two of the fourteen combine categories (e.g. extract *then*
  reconcile).
- They state the same *kinds* of rules as the visible tasks — but with less
  hand-holding. Where a visible task warns you about a trap, its hidden
  cousin just contains it.
- The traps live in execution, not comprehension: exact preservation of
  values you weren't asked to change, redacting *only* what is actually PII
  (over-redaction fails like under-redaction), leaving no stray files,
  handling files that aren't the encoding you assumed.
- Verifiers are deterministic and check outputs, never methods. If your
  agent reliably reads the rules, does exactly what they say, checks its own
  work cheaply, and cleans up — the hidden suite is the same game you've
  been practicing.

## 6. Submitting

Two deliverables — there is no call or live demo; these carry all the weight:

1. **Your repo.** Push your kit to a private GitHub repo (or zip it) and
   send it over. Include any extra test tasks you built.
2. **Your write-up** (`WRITEUP.md`, ~1 page). Cover:
   - **What you changed and why** — what was wrong with the baseline (as you
     diagnosed it, with numbers), and what each of your changes does about it.
   - **Why you believe it will pass the hidden suite** — the evidence, not
     the hope: your own held-out tasks and how the agent scored on them,
     cost-per-solve measurements, repeated runs showing it passes every time.
     "I built three tasks the agent had never seen and measured solve rate
     and cost across three runs" is strong; vibes are not.

Anything non-obvious about running your agent goes at the top of the
write-up.
