"""Launch the board WITHOUT the supervisor.

`surface serve` always starts a supervisor that dispatches inbox events to the
deterministic reference worker (`python -m surface.worker`), which does no
generation -- on revise it copies the previous content verbatim. For an
LLM-in-the-loop test the agent itself must be the worker, so it needs the board
running while the inbox stays unclaimed. There is no CLI flag for that
(`--no-board` is the opposite), hence this launcher.
"""

import json
from pathlib import Path

from surface.board import build_board
from surface.stages.config import StageConfig
from surface.store import Store

DB = "demo/poem/state.sqlite3"
PORT = 7871
TOKEN = "poemdemo"

store = Store(DB)
row = store.conn.execute(
    "SELECT value_json FROM kv_state WHERE key = 'stage_config'"
).fetchone()
stage_config = StageConfig(json.loads(row[0]))

blocks = build_board(store, stage_config)
Path("demo/poem/run").mkdir(parents=True, exist_ok=True)
blocks.launch(
    server_name="127.0.0.1",
    server_port=PORT,
    auth=("surface", TOKEN),
    quiet=False,
    show_api=False,
)
