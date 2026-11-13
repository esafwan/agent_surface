"""Fault-injection tests through the REAL production dispatch path:

    Supervisor -> NativeStreamTransport -> real `python -m surface.worker`
    subprocess -> fake agent CLI (grandchild)

This is deliberately NOT a bypass script: every event here is claimed and
dispatched by the actual `Supervisor.claim_and_dispatch_event()` that
`surface serve` uses, over the actual `NativeStreamTransport` that
`surface serve` uses, into an actual `python -m surface.worker --agent fake`
subprocess -- the same code path a real opencode/codex/agy backend runs
through, just swapped to the deterministic `fake` backend so failures are
reproducible without a network call. Recovery here is fully automatic
(lease expiry + a later claim), never a human running `surface recover` in
a polling loop.
"""

import os
import subprocess
import sys
import time

import pytest

from surface.store import Store
from surface.supervisor import Supervisor, SupervisorConfig
from surface.stages.config import load_preset
from surface.transports.native_stream import NativeStreamTransport

pytestmark = pytest.mark.agent_fault


def _worker_command(db_path: str, agent_timeout: float = 5) -> list:
    return [
        sys.executable,
        "-m",
        "surface.worker",
        "--db",
        db_path,
        "--agent",
        "fake",
        "--agent-timeout",
        str(agent_timeout),
    ]


def _count_fake_agent_processes() -> int:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "fake_agent.py"], capture_output=True, text=True
        )
        lines = [l for l in out.stdout.strip().splitlines() if l]
        return len(lines)
    except FileNotFoundError:
        pytest.skip("pgrep not available")


@pytest.fixture
def file_store(tmp_path):
    db_path = str(tmp_path / "state.sqlite3")
    store = Store(db_path)
    store.create_artifact(id="script_1", stage="script", title="Script", status="draft")
    store.put_version("script_1", content="Original", source_event_id="evt_base")
    return store, db_path


@pytest.fixture
def movie_config():
    return load_preset("movie")


def _make_supervisor(db_path, movie_config, agent_timeout=5, transport_timeout=30, lease_seconds=1):
    transport = NativeStreamTransport(
        command=_worker_command(db_path, agent_timeout=agent_timeout),
        timeout=transport_timeout,
    )
    config = SupervisorConfig(
        worker_id="fault_test_worker",
        lease_seconds=lease_seconds,
        max_attempts=3,
    )
    return Supervisor(Store(db_path), transport, movie_config, config=config)


class TestCrashRecovery:
    def test_crash_then_retry_then_permanent_fail(self, file_store, movie_config, monkeypatch):
        store, db_path = file_store
        monkeypatch.setenv("FAKE_AGENT_MODE", "crash")

        event = store.enqueue_event(type="revise", payload={"note": "n"}, artifact_id="script_1")
        supervisor = _make_supervisor(db_path, movie_config, lease_seconds=1)

        # Attempt 1: fails, lease left to expire (per _handle_dispatch_failure).
        claimed = supervisor.claim_and_dispatch_event()
        assert claimed is not None
        row = store.conn.execute("SELECT status, attempt_count FROM events WHERE id=?", (event["id"],)).fetchone()
        assert row["status"] == "processing"
        assert row["attempt_count"] == 1

        time.sleep(1.2)  # let the 1s lease expire

        # Attempt 2: claim_next_event's own reclaim-expired-lease step resets
        # this to pending automatically -- no human/CLI polling involved.
        claimed = supervisor.claim_and_dispatch_event()
        assert claimed is not None
        row = store.conn.execute("SELECT status, attempt_count FROM events WHERE id=?", (event["id"],)).fetchone()
        assert row["attempt_count"] == 2

        time.sleep(1.2)

        # Attempt 3: reaches max_attempts -> permanently failed.
        claimed = supervisor.claim_and_dispatch_event()
        assert claimed is not None
        row = store.conn.execute("SELECT status, attempt_count FROM events WHERE id=?", (event["id"],)).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 3

        supervisor.stop_worker()


class TestTimeoutKillsOrphanAndRecovers:
    def test_hang_times_out_no_orphan_then_recovers(self, file_store, movie_config, monkeypatch):
        store, db_path = file_store
        # agent_timeout (worker-internal) is deliberately LARGER than the
        # transport timeout, so the TRANSPORT's own timeout/killpg fires
        # first -- this is the real Supervisor-level kill path (native_stream
        # timeout -> os.killpg), not the worker's own internal agent timeout.
        monkeypatch.setenv("FAKE_AGENT_MODE", "hang")
        monkeypatch.setenv("FAKE_AGENT_SLEEP", "30")

        event = store.enqueue_event(type="revise", payload={"note": "n"}, artifact_id="script_1")
        supervisor = _make_supervisor(
            db_path, movie_config, agent_timeout=60, transport_timeout=2, lease_seconds=1
        )

        start = time.monotonic()
        claimed = supervisor.claim_and_dispatch_event()
        elapsed = time.monotonic() - start
        assert claimed is not None
        assert elapsed < 10  # bounded by transport timeout, not the 30s hang

        # No orphaned fake_agent.py grandchild should survive the killpg.
        deadline = time.time() + 5
        remaining = _count_fake_agent_processes()
        while remaining and time.time() < deadline:
            time.sleep(0.2)
            remaining = _count_fake_agent_processes()
        assert remaining == 0, "fake_agent.py grandchild survived the transport-level kill"

        # Recovery: switch to a mode that succeeds quickly, wait for the
        # lease to expire, and confirm a fresh worker session completes it
        # automatically -- no human running `surface recover`.
        monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
        time.sleep(1.2)

        claimed = supervisor.claim_and_dispatch_event()
        assert claimed is not None
        row = store.conn.execute("SELECT status FROM events WHERE id=?", (event["id"],)).fetchone()
        assert row["status"] == "acked"

        versions = store.list_versions("script_1")
        assert len(versions) == 2  # exactly one revise landed, not a duplicate
        assert versions[1]["created_by"] == "agent:fake"

        supervisor.stop_worker()


class TestGarbageOutputPermanentlyFails:
    def test_garbage_output_eventually_fails_cleanly(self, file_store, movie_config, monkeypatch):
        store, db_path = file_store
        monkeypatch.setenv("FAKE_AGENT_MODE", "garbage")

        event = store.enqueue_event(type="revise", payload={"note": "n"}, artifact_id="script_1")
        supervisor = _make_supervisor(db_path, movie_config, lease_seconds=1)

        for _ in range(3):
            claimed = supervisor.claim_and_dispatch_event()
            assert claimed is not None
            time.sleep(1.2)

        row = store.conn.execute("SELECT status FROM events WHERE id=?", (event["id"],)).fetchone()
        assert row["status"] == "failed"

        # No duplicate/garbage version was ever written.
        versions = store.list_versions("script_1")
        assert len(versions) == 1

        supervisor.stop_worker()


class TestDuplicateDeliveryIsIdempotent:
    def test_retry_after_successful_write_does_not_duplicate_or_recall_agent(
        self, file_store, movie_config, monkeypatch, tmp_path
    ):
        """If a version was already written for an event (e.g. the worker
        wrote it but was killed before the transport could read the ack
        line), a later retry of the SAME event must not call the agent again
        or create a second version."""
        store, db_path = file_store
        counter_file = tmp_path / "counter.txt"
        monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
        monkeypatch.setenv("FAKE_AGENT_COUNTER_FILE", str(counter_file))

        event = store.enqueue_event(type="revise", payload={"note": "n"}, artifact_id="script_1")
        supervisor = _make_supervisor(db_path, movie_config, lease_seconds=1)

        claimed = supervisor.claim_and_dispatch_event()
        assert claimed is not None
        row = store.conn.execute("SELECT status FROM events WHERE id=?", (event["id"],)).fetchone()
        assert row["status"] == "acked"
        assert len(counter_file.read_text().strip().splitlines()) == 1

        # Manually re-run the worker's handle_event for the same event_id
        # against the same store, simulating a redelivery -- put_version's
        # dedupe plus the worker's own pre-check must both hold.
        from surface.worker import AgentConfig, handle_event

        event_ctx = {
            "event_id": event["id"],
            "type": "revise",
            "artifact_id": "script_1",
            "payload": {"note": "n"},
            "config": movie_config.raw,
        }
        result = handle_event(event_ctx, store, AgentConfig(name="fake", timeout=5))
        assert result["ok"] is True

        # Agent must not have been invoked a second time.
        assert len(counter_file.read_text().strip().splitlines()) == 1
        versions = store.list_versions("script_1")
        assert len(versions) == 2

        supervisor.stop_worker()
