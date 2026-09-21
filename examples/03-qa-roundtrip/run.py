"""The bounded blocking convenience mode: `render` -> `wait(max)` -> `answer`.

SPEC section 35 scopes this to small workflows: a single structured
question, no board, no versions, no dependency graph. It exists so an agent
can ask one thing and get a durable, at-least-once-delivered answer without
either blocking a tool call indefinitely or standing up a whole board.

  render  persists a stage config + data as a durable handle
  wait    polls that handle, bounded by --max seconds -- never blocks forever
  answer  is the missing write side: something (a human, a board, a test)
          writes the answer into the handle's file

This script plays both halves in one process so it runs unattended: it
renders a question, waits for it in a background thread while a second
thread answers it after a short delay, exactly the way a human clicking on
the board would in production. Comments mark which side is playing whom.

Run:
    python examples/03-qa-roundtrip/run.py
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DB = str(HERE / "state.sqlite3")
PY = sys.executable


def surface(*args: str) -> dict:
    """Shell out to the real `surface` CLI, exactly as a user would type it."""
    proc = subprocess.run(
        [PY, "-m", "surface.cli", "--db", DB, *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"surface {' '.join(args)} failed: {proc.stderr}")
    return json.loads(proc.stdout)


# ── the agent's side: ask one question ──────────────────────────────────

question = HERE / "question.json"
question.write_text(json.dumps({
    "ask": "What is the project's name?",
    "kind": "text",
}))

print("agent : render a question, get a durable handle")
rendered = surface("render", "questionnaire", "--data", str(question))
handle = rendered["handle"]
print(f"        handle = {handle}")


# ── the human/board's side: answers after a short delay ─────────────────
# In production this is a browser click calling `POST answer`, or literally
# `surface answer <handle> --value '...'` typed in another terminal. Here
# it is a background thread so the whole example runs unattended.

def human_answers_after_delay():
    time.sleep(2)
    print("human : (2s later) answers the question")
    surface("answer", handle, "--value", json.dumps({"text": "Aurora"}))


threading.Thread(target=human_answers_after_delay, daemon=True).start()


# ── the agent's side: wait, bounded ──────────────────────────────────────
# Every wait is bounded -- this returns "pending" at 10s even if no one ever
# answers. An agent MAY call wait again later; nothing here blocks forever.

print("agent : wait up to 10s for an answer")
result = surface("wait", handle, "--max", "10")
print(f"        {result}")

if result["status"] == "answered":
    print(f"\n✅ round trip complete: project name = {result['value']['text']}")
else:
    print("\n⏳ still pending -- run again, or `surface wait` the same handle later")

question.unlink(missing_ok=True)
