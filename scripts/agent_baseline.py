"""Baseline agent — the naive ReAct-over-bash loop, kept for A/B.

It reads task.md, then loops: ask the model what to do, run the bash commands it
requests inside the workspace, feed back the output. It stops when the model
replies without requesting a command, or after MAX_TURNS.

It works, but it is expensive (data is cat-ed into context) and it trusts the
model to decide when the work is done. `agent/run.py` is the rewritten agent.

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

MAX_TURNS = 30
CMD_TIMEOUT = 60          # seconds per bash command
MAX_OUTPUT_CHARS = 10_000
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = """\
You are a helpful assistant completing a task in a workspace directory.
Read the task, then use the bash tool to get it done.
When you are finished, reply with plain text (no tool call)."""

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command in the task workspace and get its output.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string",
                                       "description": "the command to run"}},
            "required": ["command"],
        },
    },
}


def log_usage(usage):
    path = os.environ.get("GAUNTLET_USAGE_LOG")
    if not path:
        return
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    entry = {
        "input_tokens": usage.prompt_tokens - cached,
        "output_tokens": usage.completion_tokens,
        "cache_write_tokens": 0,  # cache writes are billed inside prompt_tokens here
        "cache_read_tokens": cached,
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
        output = output[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"
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

    for turn in range(1, MAX_TURNS + 1):
        response = client.chat.completions.create(
            model=args.model, max_tokens=4096, temperature=0,
            tools=[BASH_TOOL], messages=messages,
        )
        log_usage(response.usage)
        msg = response.choices[0].message

        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            print(f"    agent finished after {turn} turns", file=sys.stderr, flush=True)
            break  # model replied with text only: it considers the task done

        for tool_call in msg.tool_calls:
            command = json.loads(tool_call.function.arguments)["command"]
            # narrate on stderr — the runner streams this to the terminal
            first_line = command.splitlines()[0][:110]
            print(f"    turn {turn:2d} $ {first_line}", file=sys.stderr, flush=True)
            output = run_bash(command, workspace)
            messages.append({"role": "tool", "tool_call_id": tool_call.id,
                             "content": output})


if __name__ == "__main__":
    main()
