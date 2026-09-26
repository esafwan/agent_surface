"""End-to-end test: real board HTTP API -> real Supervisor -> real
NativeStreamTransport -> real `python -m surface.worker --agent fake`
subprocess -> real store write -> board reflects it, with no board/API
source code touched to make this work.

Uses the `fake` backend (not a real network-calling CLI) so this runs in
the normal test suite without external dependencies -- the equivalent live
test with a real CLI backend (opencode/agy) was run manually and is
recorded in ongoing/phase-1-2-harden-generalize/live_runs/ and the STATE.md
update, since a real network call doesn't belong in the default suite.
"""

import sys

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from surface.board_web import create_app
from surface.stages.config import load_preset
from surface.store import Store
from surface.supervisor import Supervisor, SupervisorConfig
from surface.transports.native_stream import NativeStreamTransport


@pytest.fixture
def file_store(tmp_path):
    """A file-backed store (not :memory:) -- required so a real worker
    subprocess (a separate OS process) can open the same database."""
    db_path = str(tmp_path / "state.sqlite3")
    store = Store(db_path)
    store.create_artifact(id="poem_1", stage="poem", title="Poem", status="draft")
    store.put_version("poem_1", content="(nothing yet)", created_by="worker", select=True)
    return store, db_path


@pytest.fixture
def poem_config():
    return load_preset("poem")


def test_unauthenticated_request_is_401(file_store, poem_config):
    store, db_path = file_store
    token = "e2e-test-token"
    client = TestClient(create_app(store, poem_config, token=token))

    assert client.get("/api/state").status_code == 401
    assert client.get("/").status_code == 401
    assert client.post("/api/action", json={"action": "revise", "artifact_id": "poem_1", "note": "n"}).status_code == 401


def test_revise_through_real_board_and_real_worker_subprocess(file_store, poem_config, monkeypatch, tmp_path):
    """The full real path, driven exactly as a browser click would:
    POST /api/action (real board HTTP) -> Supervisor.claim_and_dispatch_event()
    (real dispatch) -> NativeStreamTransport (real transport) -> a real
    `python -m surface.worker --agent fake` subprocess -> store write ->
    GET /api/state (real board HTTP) reflects it live, no manual DB poking."""
    store, db_path = file_store
    counter_file = tmp_path / "counter.txt"
    monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
    monkeypatch.setenv("FAKE_AGENT_COUNTER_FILE", str(counter_file))

    token = "e2e-test-token"
    client = TestClient(create_app(store, poem_config, token=token))
    auth = ("surface", token)

    # 1. The exact HTTP call a browser's Revise button makes.
    resp = client.post(
        "/api/action",
        json={"action": "revise", "artifact_id": "poem_1", "note": "make it about resilience"},
        auth=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["ok"] is True

    # 2. Dispatch it through the real production Supervisor/transport/worker
    # chain -- exactly what `surface serve`'s loop does on every tick.
    worker_cmd = [sys.executable, "-m", "surface.worker", "--db", db_path, "--agent", "fake"]
    transport = NativeStreamTransport(command=worker_cmd, timeout=30)
    supervisor = Supervisor(store, transport, poem_config, config=SupervisorConfig(lease_seconds=30))
    claimed = supervisor.claim_and_dispatch_event()
    assert claimed is not None
    supervisor.stop_worker()

    # 3. The board's own HTTP state endpoint -- what the browser polls --
    # reflects the agent-authored version with no manual store access.
    state = client.get("/api/state", auth=auth).json()
    artifacts = [a for stage in state["stages"] for a in stage["artifacts"]]
    poem = next(a for a in artifacts if a["artifact_id"] == "poem_1")
    assert len(poem["all_versions"]) == 2
    assert poem["selected_version"]["created_by"] == "agent:fake"
    assert "make it about resilience" in poem["selected_version"]["content"]

    # Agent was invoked exactly once -- no duplicate dispatch.
    assert len(counter_file.read_text().strip().splitlines()) == 1
