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
    python examples/02-agent-as-worker/board_only.py
"""

import json
import sys
from pathlib import Path

from surface.board import build_board
from surface.stages.config import StageConfig, load_preset
from surface.store import Store

HERE = Path(__file__).resolve().parent
DB = str(HERE / "state.sqlite3")
PORT = 7871
TOKEN = "agent-as-worker-demo"


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
(HERE / "run").mkdir(parents=True, exist_ok=True)

print(f"  Board: http://127.0.0.1:{PORT}  (user: surface, password: {TOKEN})")
print(f"  Claim events against: surface --db {DB} inbox next --wait 60")
# Stdout is fully buffered once it isn't a tty (e.g. backgrounded with `&` or
# redirected to a log file, as the README suggests). blocks.launch() below
# blocks forever, so without this flush the banner above would sit in the
# buffer and never reach the log -- the process would look silent even
# though it's serving.
sys.stdout.flush()

blocks.launch(
    server_name="127.0.0.1",
    server_port=PORT,
    auth=("surface", TOKEN),
    quiet=False,
    show_api=False,
)
