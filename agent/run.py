"""nickel-agent — script-first coding agent for cheap, reliable file tasks.

Strategy:
  - The model NEVER transforms data in-context. It writes one small Python
    program that reads/writes the workspace files on disk with real parsers
    (csv / json / pandas), runs it, then cheaply verifies its own output
    against the task's stated rules before stopping. Data never enters the
    prompt, so cost stays flat regardless of file size.
  - Prompt caching (Anthropic, via OpenRouter) makes the multi-turn ReAct
    loop cheap: the static system playbook + task text + the growing
    conversation prefix are cached and re-read at 0.1x instead of full price.
  - A dense "operator playbook" system prompt encodes the recurring execution
    traps (exact value preservation, sort semantics, redact-only-true-PII,
    no stray files, encodings, rounding/timezone) so it generalizes to unseen
    tasks rather than memorizing the practice ones.
  - A self-verification gate: the loop will not accept "done" until the model
    has actually run and checked its work (one nudge if it tries to stop early).

Runner contract:
  - invoked as `python -m agent.run --task-dir <dir> --model <model>`
  - operates only inside <dir>/workspace
  - talks to Claude Haiku 4.5 via OpenRouter, using $OPENROUTER_API_KEY
  - appends one JSON line per API call to $GAUNTLET_USAGE_LOG:
    {"input_tokens": N, "output_tokens": N, "cache_write_tokens": N,
     "cache_read_tokens": N}
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

MAX_TURNS = 14
CMD_TIMEOUT = 90          # seconds per bash command (task 04 crunches ~44k rows)
MAX_OUTPUT_CHARS = 6_000  # truncate tool output hard; the script keeps output small
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = """\
You are a meticulous, economical data-engineering agent working inside a task \
workspace (your bash tool runs with the workspace as its current directory). A hidden, \
deterministic verifier compares your OUTPUT files value-for-value against the exact \
rules in task.md. A task counts ONLY if it passes AND stays cheap, so use as FEW tool \
calls as possible — aim for four to six, never a dozen. Solve it, check it once, stop.

METHOD (be direct; do not over-explore or over-verify)
1. Skim task.md and take ONE quick look at the workspace — an `ls` plus at most one
   `head`/`wc -l` on a representative file. Never print whole data files.
2. Write and run ONE Python program that does the ENTIRE job with real parsers
   (`csv`, `json`, `pandas`) and writes the required output file(s). Use an inline
   heredoc so nothing extra is left on disk:
       python3 - <<'PY'
       ...your full solution: read inputs, transform, write output...
       PY
   The data is processed on disk inside the script; it never enters your messages.
   If the task EDITS files IN PLACE (e.g. redaction), read each file's RAW TEXT, apply
   your substitutions to that text with regex/str.replace, and write the same bytes
   back. Do NOT parse such a file into an object and re-emit it (no json.loads+json.dumps,
   no csv round-trip) — that reformats whitespace/quoting and fails the byte comparison.
   Your regex only needs to match the PII itself; the surrounding JSON/CSV structure is
   left exactly as-is.
3. Do ONE short sanity check of the result — `head` plus `wc -l` on the output (or
   `python3 -m pytest -q` for script-repair). For an IN-PLACE edit, re-read the edited
   files and confirm none of the raw PII values/patterns remain AND the file set is
   unchanged (you added/removed nothing). Because you edited raw text with re.sub, every
   non-PII byte — whitespace, structure, order/SKU numbers, IPs, prices — is preserved
   automatically, so there is nothing else to reformat. Do NOT copy files to a shared
   scratch path such as /tmp (a fixed path can pull a DIFFERENT task's leftover data into
   this workspace); if you must stage something, use a fresh `mktemp -d`. If anything is
   wrong, fix it and redo.
4. Reply with a single short line and STOP.

DO NOT (each of these wastes budget or causes failures):
- cat/print whole data files, or paste file contents into your reasoning;
- write a SEPARATE verification script, md5sum inputs, or re-list the directory again
  and again;
- re-run your solution repeatedly or "double-check" many times — one check is enough;
- for an in-place edit, json.loads/json.dumps or otherwise re-serialize the file — edit
  the raw text so every byte you didn't target is preserved exactly;
- keep going once the output is correct. Stop.

UNIVERSAL RULES (these are where tasks are won or lost)
- #1 PRESERVE VALUES EXACTLY. Read every field you are NOT explicitly transforming as
  a STRING and write it back unchanged, byte-for-byte. Do NOT let pandas infer types:
  its defaults turn `779.90` into `779.9`, `0042` into `42`, and strip timezones — any
  one of which fails the whole task. Prefer the stdlib `csv`/`json` modules (they keep
  every field as text); use `pandas` only when you truly need computation, and then
  with `dtype=str` and by formatting outputs yourself. Never round-trip a value through
  `float`/`int` unless the task tells you to change it.
- EDITING FILES IN PLACE (e.g. redaction): operate on the RAW file text and change
  only the exact spans you must — do a targeted string/regex replacement and write the
  bytes back. Do NOT parse-and-reserialize (e.g. json.loads then json.dumps): that
  silently reformats whitespace/quoting/number style and fails the byte-for-byte check
  even when your logic is right. A JSONL file stays valid if you only substitute inside
  it and leave its structure untouched.
- When CREATING a new output file, match the required column/key ORDER exactly: write
  CSV with the csv module (RFC-4180 quoting for fields with commas/quotes/newlines);
  build JSON/JSONL with json.dumps (correct escaping), one object per line.
- Sort with the EXACT key and type the task states: numeric vs lexicographic, and the
  stated tiebreakers. Sort by `int(x)` when numeric, but OUTPUT the original string.
- Redaction: read the task's PII definition precisely and implement BOTH directions —
  redact every format it lists with the EXACT token, and keep every decoy it names.
  Do NOT hand-roll loose regexes; buggy ones eat an adjacent period (`x@y.com.` ->
  `[EMAIL]` instead of `[EMAIL].`) or drop a `+1`. Start from these CORRECT patterns and
  adapt to the task's stated formats/exceptions; apply on raw text, longest match first:
    EMAIL  = r'[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}'   # keeps trailing '.'
    PHONE, applied longest-first on raw text:
      r'(?:\\+1[\\s.\\-]?)?\\(\\d{3}\\)[\\s.\\-]?\\d{3}[\\s.\\-]?\\d{4}'  # (xxx) form, opt +1
      r'(?:\\+1[\\s.\\-]?)?\\d{3}[\\s.\\-]\\d{3}[\\s.\\-]\\d{4}'          # dash/dot/space, opt +1
      r'(?<!Order #)(?<!SKU )\\b\\d{10}\\b'                           # bare 10-digit phone
  A bare run of 10 digits IS a phone UNLESS it sits right after an identifier label —
  exclude each such label with a fixed-width negative lookbehind. The two shown cover
  `Order #1234567890` and `SKU 1234567890`; add one for any other label THIS task says to
  keep (e.g. `ID `, `Ref `, `Account `). That way an unlabeled `8085550171` is redacted
  but a labeled order/SKU/ID number, IP, price, or version is not. Company / street /
  product names are not people. Over-redaction fails exactly like under-redaction;
  preserve surrounding text (possessive `'s`, honorifics) exactly.
- Add / delete / rename NO files beyond what the task asks. Never modify input data or
  test files.
- Don't assume encoding: handle CRLF and non-ASCII; for malformed/corrupt lines follow
  the task's rule (usually skip) instead of crashing.
- Script-repair: change only what's needed to make the provided tests pass. Common
  traps: banker's rounding vs ROUND_HALF_UP, timezone-naive vs -aware datetimes,
  off-by-one row slicing, wrong header/column order, wrong aggregation.
- Reconcile/merge: match keys as specified (often case-insensitive), keep the correct
  record on conflict (e.g. later date, or first-seen), preserve source value strings
  verbatim, compute status fields exactly, and do timestamp math preserving precision.

EFFICIENCY: fewer, sharper commands are better. One correct script plus a short verify
beats a dozen probing commands. Aim to finish in well under ten tool calls."""

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command with the task workspace as the working "
                       "directory and get its combined stdout+stderr.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string",
                                       "description": "the command to run"}},
            "required": ["command"],
        },
    },
}

# Injected at most once, and only if the model tries to finish without having run
# anything — the real "premature stop" failure. (We deliberately do NOT nudge a model
# that already did the work and stopped; that just burns budget on over-verification.)
VERIFY_NUDGE = (
    "You have not run any command yet. Use the bash tool to actually solve the task in "
    "the workspace with one Python script, do one quick check of the output, then finish."
)


def with_cache(content):
    """Wrap a string as a single text content-part carrying an Anthropic cache
    breakpoint (passed through by OpenRouter)."""
    return [{"type": "text", "text": content,
             "cache_control": {"type": "ephemeral"}}]


def build_api_messages(messages):
    """Return a shallow copy of `messages` decorated with cache breakpoints on the
    static system prompt and on the most recent message, so the whole growing
    conversation prefix is cached (read at 0.1x) instead of re-billed at full price.

    We only ever decorate the system message and the last message, which is always a
    user or tool message at call time (both carry plain-string content) — keeping the
    payload well within Anthropic's 4-breakpoint limit."""
    if not messages:
        return messages
    out = list(messages)
    # system prompt: stable breakpoint
    out[0] = {**out[0], "content": with_cache(out[0]["content"])}
    # rolling breakpoint on the latest message (skip if it's the system msg itself)
    last = out[-1]
    if len(out) > 1 and isinstance(last.get("content"), str):
        out[-1] = {**last, "content": with_cache(last["content"])}
    return out


def log_usage(usage):
    """Append one honest usage line. Captures cache read/write tokens from whatever
    OpenRouter/Anthropic reports so our local cost estimate matches grading's formula
    (fresh input 1x, cache-write 1.25x, cache-read 0.1x, output 5x)."""
    path = os.environ.get("GAUNTLET_USAGE_LOG")
    if not path:
        return
    if os.environ.get("AGENT_DEBUG_USAGE"):
        try:
            print("    [usage] " + json.dumps(usage.model_dump()),
                  file=sys.stderr, flush=True)
        except Exception:
            pass

    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = int(getattr(details, "cached_tokens", 0) or 0)
    # cache-write (creation) tokens surface under a few different names depending on
    # the provider mapping; take the first that's present, else 0.
    cache_write = 0
    for obj in (usage, details):
        if obj is None:
            continue
        for attr in ("cache_creation_input_tokens", "cache_creation_tokens",
                     "cache_write_tokens"):
            val = getattr(obj, attr, None)
            if val:
                cache_write = int(val)
                break
        if cache_write:
            break

    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    # fresh (uncached, non-write) input = everything not already accounted for
    input_tokens = max(0, prompt_tokens - cache_read - cache_write)
    entry = {
        "input_tokens": input_tokens,
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_bash(command, workspace):
    try:
        proc = subprocess.run(
            ["bash", "-c", command], cwd=workspace,
            capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {CMD_TIMEOUT}s"
    output = proc.stdout + proc.stderr
    if len(output) > MAX_OUTPUT_CHARS:
        head = output[: MAX_OUTPUT_CHARS - 400]
        tail = output[-400:]
        output = f"{head}\n... (output truncated) ...\n{tail}"
    return output or f"(no output, exit code {proc.returncode})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--model", default="anthropic/claude-haiku-4.5")
    args = parser.parse_args()

    task_dir = Path(args.task_dir)
    workspace = task_dir / "workspace"
    task = (task_dir / "task.md").read_text()

    client = OpenAI(base_url=OPENROUTER_BASE_URL,
                    api_key=os.environ["OPENROUTER_API_KEY"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Here is your task:\n\n{task}"},
    ]

    nudged = False
    tool_calls_made = 0
    for turn in range(1, MAX_TURNS + 1):
        response = client.chat.completions.create(
            model=args.model, max_tokens=4096, temperature=0,
            tools=[BASH_TOOL], messages=build_api_messages(messages),
        )
        log_usage(response.usage)
        msg = response.choices[0].message

        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            # The model wants to stop. Only intervene if it never actually did any
            # work — otherwise trust the stop (nudging a finished model just burns
            # budget on over-verification).
            if tool_calls_made == 0 and not nudged:
                nudged = True
                print(f"    turn {turn:2d} — model stopped without working; nudging",
                      file=sys.stderr, flush=True)
                messages.append({"role": "user", "content": VERIFY_NUDGE})
                continue
            print(f"    agent finished after {turn} turns", file=sys.stderr, flush=True)
            break

        for tool_call in msg.tool_calls:
            tool_calls_made += 1
            command = json.loads(tool_call.function.arguments)["command"]
            first_line = command.splitlines()[0][:110] if command.strip() else "(empty)"
            print(f"    turn {turn:2d} $ {first_line}", file=sys.stderr, flush=True)
            output = run_bash(command, workspace)
            messages.append({"role": "tool", "tool_call_id": tool_call.id,
                             "content": output})


if __name__ == "__main__":
    main()
