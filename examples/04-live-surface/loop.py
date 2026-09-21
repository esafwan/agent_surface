"""Live agent surface: the agent renders its own UI.

Contrast with `surface serve`: there the UI is fixed by a stage config and the
agent writes into an async queue, so a click has no visible consequence until
some worker happens to claim it. Here the agent's structured response IS the
UI -- it says what to draw and which controls are live -- and the call is
synchronous, so "working" is always on screen.

Each turn is persisted through the skill's own interaction store
(`surface render` -> handle, `surface answer` -> value), which is the
render/answer API agent_surface already shipped but never wired to a
renderer.

Agent contract (strict JSON, nothing else):
    {"draft": str|null, "ask": str, "controls": [Control], "done": bool}

Legacy `{"actions": ["approve","revise"]}` is still accepted and normalized
into a buttons control, so an older prompt never becomes wrong.

This file's default task is a poem-writing agent, matching the examples
elsewhere in this repo -- but the loop itself is generic. Everything poem-
specific lives in one string, `DEFAULT_TASK`, below; swap it for any other
task via `--task` / `--task-file` without touching the loop, the schema, the
normalizer, or the renderer.

Run:
    python examples/04-live-surface/loop.py                  # the poem demo
    python examples/04-live-surface/loop.py --pin 1234        # optional gate
    python examples/04-live-surface/loop.py \\
        --title "Recipe agent" --db recipes.sqlite3 \\
        --task 'Write recipes on request. Use mode "doc" with a "prose"
                block for the method and a "table" block for ingredients.
                Offer buttons ["Approve","Revise"].'
"""

import argparse
import hmac
import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STATIC = HERE / "static"
TURN_FILE = HERE / "turn.json"
_venv_python = ROOT / ".venv" / "bin" / "python"
PY = str(_venv_python) if _venv_python.exists() else sys.executable

# Everything below the line marked TASK is what makes this a poem agent
# specifically. Nothing above that line is poem-specific: change --task
# and this becomes a recipe agent, a bug-triage agent, whatever.
DEFAULT_TASK = """Write poems on request. Use mode "doc" with a single
"verse" block holding the FULL poem with real line breaks. Offer buttons
["Approve","Revise"]."""
DEFAULT_ASK = "What do you want me to write?"
DEFAULT_PLACEHOLDER = "e.g. a love poem"

# Set by main() from CLI args; module-level so the request handlers (which
# take no args of their own) can read them.
DB = str(HERE / "state.sqlite3")
MODEL = "claude-haiku-4-5-20251001"

MAX_CONTROLS = 3
MAX_ROWS = 50
MAX_COLS = 6

PROTOCOL = """You are an agent driving a small UI. You choose the surface.

Reply with ONLY a JSON object, no prose and no code fence:
{"mode": <"doc" | "chat" | "form">,
 "blocks": <0-4 content blocks, see below>,
 "ask": <one short line asking the user what you need next>,
 "controls": <0-3 control objects, see below>,
 "done": <true only after the user approves>}

Pick the mode that fits what you are showing, and KEEP IT unless the work
really changes shape -- flipping layout every turn is disorienting:
  "doc"   an artifact the user is reviewing (a poem, a draft, a report)
  "chat"  a back-and-forth where the exchange itself is the content
  "form"  collecting several fields at once

Blocks you may emit (at most 4):
  {"type":"verse","text":"line\\nline"}        poem/lyrics; line breaks kept
  {"type":"prose","text":"a paragraph"}       ordinary writing
  {"type":"code","text":"x = 1","lang":"py"}  monospace
  {"type":"table","columns":["a"],"rows":[["1"]]}
  {"type":"thread","turns":[{"role":"agent","text":"hi"}]}

Controls you may emit (at most 3, and prefer 2):
  {"type":"buttons","id":"decision","options":["Approve","Revise"],"primary":"Approve"}
  {"type":"choice","id":"tone","label":"Pick a tone","options":["elegiac","wry","plain"]}
  {"type":"multi","id":"edits","label":"What should change?","options":["shorter","add a title"]}
  {"type":"text","id":"note","placeholder":"say what to change..."}
  {"type":"table","id":"scan","columns":["line","syllables"],"rows":[["Under the elm",5]]}

`table` is read-only output, never an input. A free-text control is always
added for you if you omit one, so the user is never stuck.

Rules that hold regardless of task:
- The user's first message IS the request. Produce the thing immediately.
  Never stall with a clarifying question -- pick an interpretation and act.
- Every response after that first user message MUST carry real content in
  blocks. Blocks are empty only before the user has said anything at all.
- On a revise/change request, rewrite properly -- do not return the
  previous content unchanged.
- When the user approves, set done true and keep the final content."""

# TASK -- everything above this line is the fixed protocol; everything
# below is what --task / --task-file replace. See DEFAULT_TASK above.
TASK = DEFAULT_TASK  # overwritten by main() from --task / --task-file


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def call_agent(history: List[Dict[str, str]]) -> Dict[str, Any]:
    """Synchronous call to the agent. Never raises: a malformed reply becomes
    a renderable response rather than killing the session (the model is small
    and does occasionally break contract)."""
    convo = "\n".join(f"{h['role'].upper()}: {h['text']}" for h in history)
    system = f"{PROTOCOL}\n\n{TASK}"
    prompt = f"{system}\n\nConversation so far:\n{convo}\n\nRespond with the JSON object now."
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL],
            capture_output=True, text=True, cwd="/tmp", timeout=180,
        )
    except subprocess.TimeoutExpired:
        return _malformed("The agent timed out. Try again, or rephrase.")
    if proc.returncode != 0:
        return _malformed(f"Agent call failed: {proc.stderr[:300]}")

    try:
        text = json.loads(proc.stdout).get("result", "").strip()
    except json.JSONDecodeError:
        return _malformed("Could not read the agent's envelope.")

    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1].removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Show what it actually said rather than swallowing it.
        return _malformed("The agent replied in the wrong format. Tell it to retry.",
                          raw=text[:2000])
    return parsed if isinstance(parsed, dict) else _malformed("Agent returned a non-object.")


def _malformed(ask: str, raw: Optional[str] = None) -> Dict[str, Any]:
    return {"draft": raw, "ask": ask, "controls": [], "done": False, "_malformed": True}


# ---------------------------------------------------------------------------
# Response normalization -- the renderer must never trust the model
# ---------------------------------------------------------------------------

MODES = ("doc", "chat", "form")
BLOCK_TYPES = ("verse", "prose", "code", "table", "thread")
MAX_BLOCKS = 4


def normalize(response: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce whatever the agent returned into exactly what the UI can draw."""
    previous_draft = previous.get("draft")

    # Mode is sticky: an unknown or missing mode keeps the current surface
    # rather than yanking the layout out from under the user.
    mode = response.get("mode")
    if mode not in MODES:
        mode = previous.get("mode") or "doc"

    blocks = _normalize_blocks(response.get("blocks"))
    draft = response.get("draft")
    if not blocks and isinstance(draft, str) and draft.strip():
        blocks = [{"type": "verse", "text": draft}]      # legacy shape
    if not blocks:
        # Sticky content: an agent hiccup must not wipe the user's work view.
        blocks = previous.get("blocks") or []

    draft = _draft_of(blocks) or previous_draft
    ask = response.get("ask")
    if not isinstance(ask, str) or not ask.strip():
        ask = "What next?"
    done = bool(response.get("done")) and bool(draft)

    raw_controls = response.get("controls")
    if not isinstance(raw_controls, list) or not raw_controls:
        raw_controls = _from_legacy_actions(response.get("actions"))

    controls: List[Dict[str, Any]] = []
    for i, c in enumerate(raw_controls):
        norm = _normalize_control(c, i)
        if norm:
            controls.append(norm)

    inputs = [c for c in controls if c["type"] != "table"]
    if not done and not any(c["type"] == "text" for c in inputs):
        controls.append({"type": "text", "id": "note", "label": "",
                         "placeholder": "type your answer…"})

    # Cap, but never drop the text escape hatch.
    if len(controls) > MAX_CONTROLS:
        text_c = [c for c in controls if c["type"] == "text"][:1]
        others = [c for c in controls if c["type"] != "text"]
        controls = (others + text_c)[:MAX_CONTROLS] if text_c else others[:MAX_CONTROLS]
        if text_c and text_c[0] not in controls:
            controls = controls[:MAX_CONTROLS - 1] + text_c

    return {"mode": mode, "blocks": blocks, "draft": draft, "ask": ask,
            "controls": controls, "done": done,
            "malformed": bool(response.get("_malformed"))}


def _normalize_blocks(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for b in raw[:MAX_BLOCKS]:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "table":
            cols, rows = b.get("columns"), b.get("rows")
            if not isinstance(cols, list) or not isinstance(rows, list):
                continue
            out.append({"type": "table",
                        "columns": [str(c) for c in cols[:MAX_COLS]],
                        "rows": [[str(c) for c in r[:MAX_COLS]]
                                 for r in rows[:MAX_ROWS] if isinstance(r, list)]})
        elif btype == "thread":
            turns = b.get("turns")
            if not isinstance(turns, list):
                continue
            out.append({"type": "thread", "turns": [
                {"role": "user" if t.get("role") == "user" else "agent",
                 "text": str(t.get("text", ""))}
                for t in turns if isinstance(t, dict)][:40]})
        else:
            text = b.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            # Unknown block type degrades to prose rather than vanishing.
            kind = btype if btype in BLOCK_TYPES else "prose"
            block = {"type": kind, "text": text}
            if kind == "code" and isinstance(b.get("lang"), str):
                block["lang"] = b["lang"]
            out.append(block)
    return out


def _draft_of(blocks: List[Dict[str, Any]]) -> Optional[str]:
    """The versionable artifact: the first text-bearing block."""
    for b in blocks:
        if b.get("type") in ("verse", "prose", "code") and b.get("text"):
            return b["text"]
    return None


def _from_legacy_actions(actions: Any) -> List[Dict[str, Any]]:
    if not isinstance(actions, list) or not actions:
        return []
    options = [str(a).strip().title() for a in actions if str(a).strip()]
    if not options:
        return []
    return [{"type": "buttons", "id": "action", "options": options, "primary": options[0]}]


def _normalize_control(c: Any, index: int) -> Optional[Dict[str, Any]]:
    if not isinstance(c, dict):
        return None
    ctype = c.get("type")
    cid = c.get("id") if isinstance(c.get("id"), str) and c.get("id") else f"c{index}"
    label = c.get("label") if isinstance(c.get("label"), str) else ""

    if ctype == "table":
        cols = c.get("columns")
        rows = c.get("rows")
        if not isinstance(cols, list) or not isinstance(rows, list):
            return None
        cols = [str(x) for x in cols[:MAX_COLS]]
        clean = [[str(cell) for cell in row[:MAX_COLS]]
                 for row in rows[:MAX_ROWS] if isinstance(row, list)]
        return {"type": "table", "id": cid, "label": label, "columns": cols, "rows": clean}

    if ctype in ("buttons", "choice", "multi"):
        options = c.get("options")
        if not isinstance(options, list):
            return None
        options = [str(o) for o in options if str(o).strip()]
        if not options:
            return None
        out = {"type": ctype, "id": cid, "label": label, "options": options}
        if ctype == "buttons":
            primary = c.get("primary")
            out["primary"] = primary if primary in options else options[0]
        return out

    if ctype == "text":
        return {"type": "text", "id": cid, "label": label,
                "placeholder": c.get("placeholder") or "type your answer…"}

    # Unknown type -> degrade to text rather than dropping it silently.
    return {"type": "text", "id": cid, "label": label or str(ctype or ""),
            "placeholder": "type your answer…"}


def flatten(values: Dict[str, Any]) -> str:
    """One human-readable line for the agent's history, so the model never has
    to parse its own control ids back."""
    parts: List[str] = []
    for v in values.values():
        if isinstance(v, list):
            if v:
                parts.append(", ".join(str(x) for x in v))
        elif str(v).strip():
            parts.append(str(v).strip())
    return " — ".join(parts)


# ---------------------------------------------------------------------------
# Interaction store (the skill's own render/answer API)
# ---------------------------------------------------------------------------

def persist_turn(response: Dict[str, Any]) -> Optional[str]:
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
    if not handle:
        return
    subprocess.run(
        [PY, "-m", "surface.cli", "--db", DB, "answer", handle,
         "--value", json.dumps({"text": text})],
        capture_output=True, text=True, cwd=str(ROOT),
    )


# ---------------------------------------------------------------------------
# Session -- the server is the single source of truth, so a browser reload
# mid-turn repaints the real state instead of orphaning the loop.
# ---------------------------------------------------------------------------

def fresh_session() -> Dict[str, Any]:
    return {
        "history": [],
        "handle": None,
        "response": {"mode": "doc", "blocks": [], "draft": None,
                     "ask": OPENING_ASK,
                     "controls": [{"type": "text", "id": "note", "label": "",
                                   "placeholder": OPENING_PLACEHOLDER}],
                     "done": False, "malformed": False},
        "versions": [],  # [{"n": 1, "draft": str, "note": str, "at": float}]
        "inflight": False,
        "started_at": 0.0,
        "turns": 0,
    }


OPENING_ASK = DEFAULT_ASK              # overwritten by main() from --ask
OPENING_PLACEHOLDER = DEFAULT_PLACEHOLDER  # overwritten by main() from --placeholder

SESSION: Dict[str, Any] = fresh_session()

PIN: Optional[str] = None
SECRET = secrets.token_bytes(32)
_attempts: List[float] = []


def session_cookie() -> str:
    return hmac.new(SECRET, b"agent-surface", "sha256").hexdigest()


def authed(request: Request) -> bool:
    if PIN is None:
        return True
    return hmac.compare_digest(request.cookies.get("sat_session", ""), session_cookie())


def render_payload() -> Dict[str, Any]:
    return {
        "auth": PIN is not None,
        "inflight": SESSION["inflight"],
        "elapsed": round(time.time() - SESSION["started_at"], 1) if SESSION["inflight"] else 0,
        "turns": SESSION["turns"],
        "versions": [{"n": v["n"], "note": v["note"]} for v in SESSION["versions"]],
        **SESSION["response"],
    }


app = FastAPI()


@app.get("/gate")
def gate() -> FileResponse:
    return FileResponse(STATIC / "gate.html")


@app.post("/gate")
async def gate_submit(request: Request) -> JSONResponse:
    now = time.time()
    _attempts[:] = [t for t in _attempts if now - t < 60]
    if len(_attempts) >= 5:
        return JSONResponse({"ok": False, "locked": True}, status_code=429)
    body = await request.json()
    _attempts.append(now)
    if PIN and hmac.compare_digest(str(body.get("pin", "")), PIN):
        resp = JSONResponse({"ok": True})
        resp.set_cookie("sat_session", session_cookie(), httponly=True, samesite="strict")
        _attempts.clear()
        return resp
    return JSONResponse({"ok": False}, status_code=401)


@app.get("/")
def index(request: Request):
    if not authed(request):
        return RedirectResponse("/gate")
    return FileResponse(STATIC / "index.html")


@app.get("/state")
def state(request: Request):
    if not authed(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    return render_payload()


@app.post("/turn")
async def turn(request: Request):
    if not authed(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    body = await request.json()
    text = flatten(body.get("values") or {})
    if not text:
        return render_payload()
    # The agent call blocks for 3-10s (sometimes far longer). Run it off the
    # event loop or /state and the static files stall for its whole duration.
    return await run_in_threadpool(_run_turn, text)


def _run_turn(text: str) -> Dict[str, Any]:
    record_answer(SESSION["handle"], text)
    SESSION["history"].append({"role": "user", "text": text})
    SESSION["inflight"] = True
    SESSION["started_at"] = time.time()

    previous = SESSION["response"]
    previous_draft = previous.get("draft")
    try:
        raw = call_agent(SESSION["history"])
        response = normalize(raw, previous)
    finally:
        SESSION["inflight"] = False

    SESSION["history"].append({"role": "agent", "text": json.dumps(raw)})
    SESSION["handle"] = persist_turn(response)
    SESSION["turns"] += 1
    if response["draft"] and response["draft"] != previous_draft:
        SESSION["versions"].append({
            "n": len(SESSION["versions"]) + 1,
            "draft": response["draft"],
            "note": text[:48],
            "at": time.time(),
        })
    SESSION["response"] = response
    return render_payload()


@app.post("/reset")
def reset(request: Request):
    """Start over. The session lives on the server, so a client-side reload
    would just re-fetch the finished state -- the reset has to happen here."""
    if not authed(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    SESSION.clear()
    SESSION.update(fresh_session())
    return render_payload()


@app.get("/version/{n}")
def version(n: int, request: Request):
    if not authed(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    for v in SESSION["versions"]:
        if v["n"] == n:
            return {"n": v["n"], "draft": v["draft"], "note": v["note"]}
    return JSONResponse({"error": "not found"}, status_code=404)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def main() -> None:
    global PIN, DB, MODEL, TASK, OPENING_ASK, OPENING_PLACEHOLDER, SESSION
    parser = argparse.ArgumentParser(description="Live agent surface")
    parser.add_argument("--pin", help="Optional 4- or 6-digit gate (default: no auth)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7872)
    parser.add_argument("--db", help="Path to the SQLite interaction store "
                        "(default: state.sqlite3 next to this file)")
    parser.add_argument("--model", default=MODEL, help="claude -p --model to call")
    parser.add_argument("--task", help="Task instructions, replacing the poem "
                        "default (see DEFAULT_TASK in this file)")
    parser.add_argument("--task-file", help="Path to a file holding --task text")
    parser.add_argument("--ask", default=DEFAULT_ASK,
                        help="Opening question shown before the first turn")
    parser.add_argument("--placeholder", default=DEFAULT_PLACEHOLDER,
                        help="Opening input placeholder")
    args = parser.parse_args()

    if args.pin is not None:
        if not re.fullmatch(r"\d{4}|\d{6}", args.pin):
            parser.error("--pin must be 4 or 6 digits")
        PIN = args.pin

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and PIN is None:
        parser.error(f"refusing to bind {args.host} without --pin")

    if args.task and args.task_file:
        parser.error("pass either --task or --task-file, not both")
    if args.task_file:
        TASK = Path(args.task_file).read_text().strip()
    elif args.task:
        TASK = args.task

    if args.db:
        DB = args.db
    MODEL = args.model
    OPENING_ASK = args.ask
    OPENING_PLACEHOLDER = args.placeholder
    # The module-level SESSION was built at import time from the defaults;
    # rebuild it now that --ask/--placeholder may have overridden them.
    SESSION.clear()
    SESSION.update(fresh_session())

    # The PIN gates the browser, not the machine. The session cookie is signed
    # with a per-process secret (restart = everyone logged out), and the app is
    # served over plain HTTP -- so anything that can read loopback traffic or
    # this process's memory reads the PIN. This is a convenience lock for a
    # single-user localhost app; it is not an authentication system and must
    # not be exposed to a network.
    print(f"agent surface on http://{args.host}:{args.port}"
          f"{'  (pin required)' if PIN else '  (no auth)'}", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
