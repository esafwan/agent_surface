"""Board without supervisor, web renderer, so a real CLI agent can claim events."""
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, "/Users/safwan/Code/Experiments/agent_surface")

from surface.board_web import run_web_board
from surface.stages.config import StageConfig
from surface.store import Store

DB = sys.argv[1]
HOST = "127.0.0.1"
PORT = int(sys.argv[2])

store = Store(DB)
row = store.conn.execute("SELECT value_json FROM kv_state WHERE key = 'stage_config'").fetchone()
stage_config = StageConfig(json.loads(row[0]))

token = secrets.token_urlsafe(32)
Path(DB).resolve().parent.joinpath("run").mkdir(parents=True, exist_ok=True)
Path(DB).resolve().parent.joinpath("run", "token").write_text(token)
print(f"BOARD_URL=http://{HOST}:{PORT}")
print(f"TOKEN={token}")
sys.stdout.flush()

run_web_board(store, stage_config, host=HOST, port=PORT, token=token, log_level="warning")
