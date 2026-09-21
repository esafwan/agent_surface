"""Satellite-shaped agent loop on top of agent_surface.

Contrast with `surface serve`: there the UI is fixed by a stage config and the
agent writes into an async queue, so a click has no visible consequence until
some worker happens to claim it. Here the agent's structured response IS the
UI -- it says what to draw and which actions are live -- and the call is
synchronous, so "working" is always on screen.

Each turn is persisted through the skill's own interaction store
(`surface render` -> handle, `surface answer` -> value), which is the
Satellite-shaped API agent_surface already shipped but never wired to a
renderer.

Agent contract (strict JSON, nothing else):
    {"draft": str|null, "ask": str, "actions": [str], "done": bool}
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import gradio as gr

ROOT = Path(__file__).resolve().parents[2]
DB = str(ROOT / "demo/satellite/state.sqlite3")
TURN_FILE = ROOT / "demo/satellite/turn.json"
PY = str(ROOT / ".venv/bin/python")
MODEL = "claude-haiku-4-5-20251001"
PORT = 7872
TOKEN = "satellite"

SYSTEM = """You are a poem-writing agent driving a small UI.

Reply with ONLY a JSON object, no prose and no code fence:
{"draft": <the full current poem as a string, or null if none yet>,
 "ask": <one short line asking the user what you need next>,
 "actions": <subset of ["submit","approve","revise"]>,
 "done": <true only after the user approves>}

Rules:
- The user's first message IS the poem request. Write the poem immediately.
  Never stall with a clarifying question -- pick an interpretation and write.
- Every response after that first user message MUST have a real poem in
  draft. draft is null only before the user has said anything at all.
- Put the FULL poem in draft and set actions to ["approve","revise"].
- On a revise request, rewrite the poem properly -- do not return the
  previous text unchanged.
- When the user approves, set done true and keep the final poem in draft."""


def call_agent(history: List[Dict[str, str]]) -> Dict[str, Any]:
    """Synchronous call to the agent. Returns the parsed structured response."""
    convo = "\n".join(f"{h['role'].upper()}: {h['text']}" for h in history)
    prompt = f"{SYSTEM}\n\nConversation so far:\n{convo}\n\nRespond with the JSON object now."
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL],
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=180,
    )
    if proc.returncode != 0:
        return {
            "draft": None,
            "ask": f"Agent call failed: {proc.stderr[:300]}",
            "actions": ["submit"],
            "done": False,
        }
    envelope = json.loads(proc.stdout)
    text = envelope.get("result", "").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return json.loads(text)


def persist_turn(response: Dict[str, Any]) -> Optional[str]:
    """Record this turn in the skill's interaction store; return the handle."""
    TURN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TURN_FILE.write_text(json.dumps(response))
    proc = subprocess.run(
        [PY, "-m", "surface.cli", "--db", DB, "render", "poem", "--data", str(TURN_FILE)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    try:
        return json.loads(proc.stdout).get("handle")
    except json.JSONDecodeError:
        return None


def record_answer(handle: Optional[str], text: str) -> None:
    """Close the round-trip on the handle the user is answering."""
    if not handle:
        return
    subprocess.run(
        [PY, "-m", "surface.cli", "--db", DB, "answer", handle,
         "--value", json.dumps({"text": text})],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def turn(user_text: str, history: List[Dict[str, str]], handle: Optional[str]):
    """One synchronous round trip, yielding an in-flight state first."""
    user_text = (user_text or "").strip()
    if not user_text:
        yield gr.skip(), gr.skip(), gr.skip(), history, handle, gr.skip()
        return

    record_answer(handle, user_text)
    history = history + [{"role": "user", "text": user_text}]

    # The in-flight state the board could never express.
    yield (
        gr.update(value="⏳ **agent is writing…**"),
        gr.skip(),
        gr.update(value="", interactive=False),
        history,
        handle,
        gr.update(interactive=False),
    )

    response = call_agent(history)
    history = history + [{"role": "agent", "text": json.dumps(response)}]
    new_handle = persist_turn(response)

    draft = response.get("draft") or "_(no poem yet)_"
    ask = response.get("ask", "")
    actions = response.get("actions", [])
    done = response.get("done", False)

    status = f"✅ **approved — done**" if done else f"**{ask}**  ·  actions: `{', '.join(actions)}`"
    yield (
        gr.update(value=status),
        gr.update(value=draft),
        gr.update(value="", interactive=not done,
                  placeholder="approve" if "approve" in actions else "what poem do you want?"),
        history,
        new_handle,
        gr.update(interactive=not done),
    )


with gr.Blocks(title="Satellite Loop", theme=gr.themes.Soft()) as demo:
    gr.Markdown("## Poem agent — synchronous loop")
    status = gr.Markdown("**What poem do you want?**  ·  actions: `submit`")
    draft = gr.Markdown("_(no poem yet)_", elem_id="draft")
    with gr.Row():
        box = gr.Textbox(show_label=False, container=False, scale=5,
                         placeholder="what poem do you want?")
        send = gr.Button("Send", variant="primary", scale=0, min_width=100)

    history = gr.State([])
    handle = gr.State(None)

    for trigger in (box.submit, send.click):
        trigger(turn, [box, history, handle], [status, draft, box, history, handle, send])

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=PORT,
                auth=("surface", TOKEN), quiet=False)
