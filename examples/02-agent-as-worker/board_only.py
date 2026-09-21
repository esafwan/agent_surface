"""Launch the board WITHOUT the supervisor, and be the worker yourself.

`surface serve` always starts a supervisor that dispatches inbox events to the
deterministic reference worker (`python -m surface.worker`), which does no
generation -- on revise it just re-commits the previous content unchanged
(see "The Shipped Worker Does Not Generate" in SKILL.md). For a real agent to
sit behind the board, the board has to run while the inbox stays unclaimed,
so something else -- an agent -- can claim events instead. There is no CLI
flag for board-without-supervisor (`--no-board` is the opposite), hence this
launcher: it calls `build_board()` directly.

This script only starts the board and seeds one empty artifact. Being the
worker is the other half of the loop, done separately:

    surface --db examples/02-agent-as-worker/state.sqlite3 inbox next --wait 60
    # ... write a real version, e.g. via Store.put_version() ...
    surface --db examples/02-agent-as-worker/state.sqlite3 inbox ack <event_id>

That claim/write/ack loop *is* being the worker. Run this script, open the
board, click Revise with a note, then run the `inbox next` command above in
another terminal to see the event waiting, and write whatever you want as
the new version.

Run:
    python examples/02-agent-as-worker/board_only.py             # no auth
    python examples/02-agent-as-worker/board_only.py --pin 1234   # optional gate
"""

import argparse
import json
import re
import sys
from pathlib import Path

from surface.board import build_board
from surface.stages.config import StageConfig, load_preset
from surface.store import Store

HERE = Path(__file__).resolve().parent

parser = argparse.ArgumentParser(description="Board without the supervisor")
parser.add_argument("--db", default=str(HERE / "state.sqlite3"))
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=7871)
parser.add_argument("--pin", help="Optional 4- or 6-digit gate (default: no auth). "
                    "Gradio's auth needs a username too, so this pairs it "
                    "with the fixed user 'surface'.")
args = parser.parse_args()

if args.pin is not None and not re.fullmatch(r"\d{4}|\d{6}", args.pin):
    parser.error("--pin must be 4 or 6 digits")

loopback = args.host in ("127.0.0.1", "localhost", "::1")
if not loopback and args.pin is None:
    parser.error(f"refusing to bind {args.host} without --pin")

DB = args.db


def ensure_seeded() -> Store:
    """Idempotent: safe to re-run. Creates the project and one empty
    artifact if this is a fresh directory."""
    fresh = not Path(DB).exists()
    store = Store(DB)
    if fresh:
        stage_config = load_preset("poem")
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("stage_config", json.dumps(stage_config.raw)),
        )
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("project_id", json.dumps("default")),
        )
        store.conn.commit()
        store.create_artifact(id="poem_1", stage="poem", title="Poem", status="draft")
        store.put_version(
            "poem_1",
            content="(nothing yet -- type a request in Revise and press it)",
            content_type="text/markdown",
            created_by="worker",
            note="awaiting the first request",
        )
        print(f"  Seeded a fresh project at {DB}")
    return store


store = ensure_seeded()
row = store.conn.execute(
    "SELECT value_json FROM kv_state WHERE key = 'stage_config'"
).fetchone()
stage_config = StageConfig(json.loads(row[0]))

blocks = build_board(store, stage_config)
(Path(DB).resolve().parent / "run").mkdir(parents=True, exist_ok=True)

print(f"  Board: http://{args.host}:{args.port}"
      f"{f'  (user: surface, password: {args.pin})' if args.pin else '  (no auth)'}")
print(f"  Claim events against: surface --db {DB} inbox next --wait 60")
# Stdout is fully buffered once it isn't a tty (e.g. backgrounded with `&` or
# redirected to a log file, as the README suggests). blocks.launch() below
# blocks forever, so without this flush the banner above would sit in the
# buffer and never reach the log -- the process would look silent even
# though it's serving.
sys.stdout.flush()

# The PIN, when set, gates the browser, not the machine -- see the same
# caveat in examples/04-live-surface/loop.py. This is a convenience lock
# for a single-user localhost app, not an authentication system.
blocks.launch(
    server_name=args.host,
    server_port=args.port,
    auth=("surface", args.pin) if args.pin else None,
    quiet=False,
    show_api=False,
)
