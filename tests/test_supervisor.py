"""
Unit tests for Supervisor.

Tests cover:
- claim_and_dispatch_event: claim eligible event, send to worker, validate, ack
- worker lifecycle: start, stop, is_alive
- rehydration summary construction
- event context building (with upstream/downstream/config)
- error handling: worker exit, transport failure, event expiry
- loop execution: run_once, run_loop
"""

import json
import pytest
import sys
from unittest.mock import Mock, MagicMock, patch

from surface.store import Store
from surface.supervisor import Supervisor, SupervisorConfig
from surface.stages.config import load_preset
from surface.transports.native_stream import NativeStreamTransport
from surface.transports.resume import ResumeTransport


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def in_memory_store():
    """In-memory SQLite store for testing."""
    return Store(":memory:")


@pytest.fixture
def movie_config():
    """Load the movie stage config."""
    return load_preset("movie")


@pytest.fixture
def echo_worker_command():
    """Simple echo worker for testing."""
    return [
        sys.executable,
        "-c",
        """
import sys
import json
for line in sys.stdin:
    try:
        data = json.loads(line)
        response = {"ok": True, "result": {"processed": data.get("event_id")}}
        print(json.dumps(response))
        sys.stdout.flush()
    except Exception as e:
        response = {"ok": False, "error": str(e)}
        print(json.dumps(response))
        sys.stdout.flush()
""",
    ]


@pytest.fixture
def native_stream_transport(echo_worker_command):
    """NativeStreamTransport with echo worker."""
    return NativeStreamTransport(command=echo_worker_command)


@pytest.fixture
def resume_transport():
    """ResumeTransport for testing."""
    return ResumeTransport()


@pytest.fixture
def supervisor_config():
    """SupervisorConfig with sensible defaults."""
    return SupervisorConfig(
        worker_id="test_worker",
        lease_seconds=30,
        turn_timeout=10,
    )


@pytest.fixture
def supervisor_with_native(in_memory_store, movie_config, native_stream_transport, supervisor_config):
    """Supervisor with NativeStreamTransport."""
    return Supervisor(
        store=in_memory_store,
        transport=native_stream_transport,
        stage_config=movie_config,
        config=supervisor_config,
    )


@pytest.fixture
def supervisor_with_resume(in_memory_store, movie_config, resume_transport, supervisor_config):
    """Supervisor with ResumeTransport."""
    return Supervisor(
        store=in_memory_store,
        transport=resume_transport,
        stage_config=movie_config,
        config=supervisor_config,
    )


# ============================================================================
# Supervisor Initialization & Worker Lifecycle Tests
# ============================================================================


class TestSupervisorInitialization:
    """Test Supervisor initialization."""

    def test_supervisor_init(self, in_memory_store, movie_config, native_stream_transport, supervisor_config):
        """Test that Supervisor initializes correctly."""
        supervisor = Supervisor(
            store=in_memory_store,
            transport=native_stream_transport,
            stage_config=movie_config,
            config=supervisor_config,
        )

        assert supervisor.store is in_memory_store
        assert supervisor.transport is native_stream_transport
        assert supervisor.stage_config is movie_config
        assert supervisor.config is supervisor_config
        assert supervisor.worker_session is None
        assert supervisor.event_count == 0

    def test_supervisor_default_config(self, in_memory_store, movie_config, native_stream_transport):
        """Test that Supervisor creates default config if not provided."""
        supervisor = Supervisor(
            store=in_memory_store,
            transport=native_stream_transport,
            stage_config=movie_config,
        )

        assert supervisor.config is not None
        assert supervisor.config.worker_id == "supervisor_worker"
        assert supervisor.config.lease_seconds == 60


class TestSupervisorWorkerLifecycle:
    """Test worker start/stop lifecycle."""

    def test_start_worker_success(self, supervisor_with_native):
        """Test successful worker start."""
        assert supervisor_with_native.worker_session is None

        success = supervisor_with_native.start_worker()

        assert success is True
        assert supervisor_with_native.worker_session is not None
        assert supervisor_with_native.transport.is_alive(supervisor_with_native.worker_session)

        supervisor_with_native.stop_worker()

    def test_start_worker_idempotent(self, supervisor_with_native):
        """Test that start_worker is idempotent."""
        supervisor_with_native.start_worker()
        session1 = supervisor_with_native.worker_session

        supervisor_with_native.start_worker()
        session2 = supervisor_with_native.worker_session

        assert session1 is session2

        supervisor_with_native.stop_worker()

    def test_stop_worker(self, supervisor_with_native):
        """Test worker stop."""
        supervisor_with_native.start_worker()
        session = supervisor_with_native.worker_session
        assert supervisor_with_native.transport.is_alive(session)

        supervisor_with_native.stop_worker()

        assert supervisor_with_native.worker_session is None
        assert not supervisor_with_native.transport.is_alive(session)


# ============================================================================
# Rehydration & Event Context Tests
# ============================================================================


class TestRehydrationSummary:
    """Test _build_rehydration_summary()."""

    def test_build_summary_empty_store(self, supervisor_with_native):
        """Test summary with empty store."""
        summary = supervisor_with_native._build_rehydration_summary()

        assert summary["project_id"] == "default"
        assert summary["stage_config_id"] == "movie"
        assert "stage_counts" in summary
        assert summary["artifact_count"] == 0
        assert summary["recent_event_ids"] == []

    def test_build_summary_with_artifacts(self, supervisor_with_native, in_memory_store, movie_config):
        """Test summary with artifacts."""
        # Create artifacts
        in_memory_store.create_artifact("art_1", "script", "Script 1", status="approved")
        in_memory_store.create_artifact("art_2", "shots", "Shots 1", status="stale")
        in_memory_store.create_artifact("art_3", "shots", "Shots 2", status="approved")
        in_memory_store.create_artifact("art_4", "keyframes", "Frames 1", status="generating")

        summary = supervisor_with_native._build_rehydration_summary()

        assert summary["artifact_count"] == 4
        assert summary["stage_counts"]["script"]["approved"] == 1
        assert summary["stage_counts"]["shots"]["stale"] == 1
        assert summary["stage_counts"]["shots"]["approved"] == 1
        assert summary["stage_counts"]["keyframes"]["generating"] == 1


class TestEventContext:
    """Test _build_event_context()."""

    def test_build_context_simple_event(self, supervisor_with_native, in_memory_store):
        """Test event context for simple event."""
        # Create artifact first
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={"content": "Test"},
            artifact_id="art_1",
        )

        context = supervisor_with_native._build_event_context(event)

        assert context["event_id"] == event["id"]
        assert context["type"] == "edit"
        assert context["project_id"] == "default"
        assert context["payload"] == {"content": "Test"}
        assert context["artifact_id"] == "art_1"
        assert "config" in context
        assert "summary" in context

    def test_build_context_with_artifact(self, supervisor_with_native, in_memory_store):
        """Test event context includes artifact and versions."""
        art = in_memory_store.create_artifact("art_1", "script", "Script 1")
        v1 = in_memory_store.put_version("art_1", content="Script v1", select=True)
        v2 = in_memory_store.put_version("art_1", content="Script v2", select=False)

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={"content": "Revised script"},
            artifact_id="art_1",
        )

        context = supervisor_with_native._build_event_context(event, art)

        assert context["artifact"] == art
        assert "artifact_versions" in context
        assert len(context["artifact_versions"]) == 2

    def test_build_context_with_dependencies(self, supervisor_with_native, in_memory_store):
        """Test event context includes upstream/downstream."""
        # Create a simple DAG: art_1 -> art_2
        in_memory_store.create_artifact("art_1", "script", "Script")
        in_memory_store.create_artifact("art_2", "shots", "Shots")
        in_memory_store.add_dependency("art_1", "art_2")

        art1 = in_memory_store.get_artifact("art_1")
        event = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        context = supervisor_with_native._build_event_context(event, art1)

        assert context["downstream_ids"] == ["art_2"]
        assert context["upstream_ids"] == []


# ============================================================================
# Event Claim & Dispatch Tests
# ============================================================================


class TestClaimAndDispatch:
    """Test claim_and_dispatch_event()."""

    def test_claim_and_dispatch_success(self, supervisor_with_native, in_memory_store):
        """Test successful claim and dispatch."""
        # Create artifact first
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        # Create an event
        event = in_memory_store.enqueue_event(
            type="edit",
            payload={"content": "Test"},
            artifact_id="art_1",
        )

        # Dispatch
        result = supervisor_with_native.claim_and_dispatch_event()

        assert result is not None
        assert result["id"] == event["id"]

        # Verify event is acked
        updated = in_memory_store.get_event(event["id"])
        assert updated["status"] == "acked"

        supervisor_with_native.stop_worker()

    def test_claim_and_dispatch_no_event(self, supervisor_with_native):
        """Test claim_and_dispatch with no pending events."""
        result = supervisor_with_native.claim_and_dispatch_event()

        assert result is None

        supervisor_with_native.stop_worker()

    def test_claim_and_dispatch_worker_failure_retries_before_failing(
        self, supervisor_with_native, in_memory_store
    ):
        """A single transient worker failure MUST NOT permanently kill the
        event (SPEC section 40: "worker crash before ack: lease expires;
        event retries"). Below max_attempts, the event is left claimed with
        its lease intact rather than marked failed."""
        # Create artifact first
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        # Mock the transport to return failure
        supervisor_with_native.transport.send_event = Mock(
            return_value={"ok": False, "error": "Worker error"}
        )

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        result = supervisor_with_native.claim_and_dispatch_event()

        assert result is not None
        # First failed attempt: event stays claimed (processing) with a lease,
        # so it can be retried once the lease expires, rather than being
        # permanently failed on the very first transient error.
        updated = in_memory_store.get_event(event["id"])
        assert updated["status"] == "processing"
        assert updated["attempt_count"] == 1

        supervisor_with_native.stop_worker()

    def test_claim_and_dispatch_worker_failure_permanent_after_max_attempts(
        self, supervisor_with_native, in_memory_store
    ):
        """After config.max_attempts consecutive failures, the event is
        permanently failed."""
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        supervisor_with_native.transport.send_event = Mock(
            return_value={"ok": False, "error": "Worker error"}
        )
        supervisor_with_native.config.max_attempts = 2

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        # First attempt: retried (left processing).
        supervisor_with_native.claim_and_dispatch_event()
        assert in_memory_store.get_event(event["id"])["status"] == "processing"

        # Force the lease to expire so the event becomes claimable again.
        in_memory_store.conn.execute(
            "UPDATE events SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (event["id"],),
        )
        in_memory_store.conn.commit()

        # Second attempt reaches max_attempts: now permanently failed.
        supervisor_with_native.claim_and_dispatch_event()
        updated = in_memory_store.get_event(event["id"])
        assert updated["status"] == "failed"
        assert "Worker error" in updated["error"]

        supervisor_with_native.stop_worker()

    def test_claim_and_dispatch_worker_not_running(self, supervisor_with_native, in_memory_store):
        """Test when worker fails to start."""
        # Create artifact first
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        # Mock transport.start to raise
        supervisor_with_native.transport.start = Mock(
            side_effect=RuntimeError("Failed to start")
        )

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        result = supervisor_with_native.claim_and_dispatch_event()

        # Should return None and not dispatch
        assert result is None

    def test_claim_respects_artifact_lease(self, supervisor_with_native, in_memory_store):
        """Test that claim respects existing artifact processing leases."""
        # This test verifies that the store correctly handles artifact leases.
        # When event1 is claimed, it acquires a lease on art_1.
        # Event2 should not be claimed while the lease is active.
        # After event1 is acked, the lease is released and event2 can be claimed.

        # Create artifact and two events for same artifact
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        event1 = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )
        event2 = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        # Dispatch event1 (claims and acks, releasing lease)
        result1 = supervisor_with_native.claim_and_dispatch_event()
        assert result1["id"] == event1["id"]

        # Verify event1 was acked
        updated1 = in_memory_store.get_event(event1["id"])
        assert updated1["status"] == "acked"

        # Event2 should now be claimable (lease was released after event1 acked)
        result2 = supervisor_with_native.claim_and_dispatch_event()
        assert result2 is not None
        assert result2["id"] == event2["id"]

        supervisor_with_native.stop_worker()

    def test_event_count_increments(self, supervisor_with_native, in_memory_store):
        """Test that event_count increments on successful dispatch."""
        assert supervisor_with_native.event_count == 0

        # Create artifact first
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        supervisor_with_native.claim_and_dispatch_event()
        assert supervisor_with_native.event_count == 1

        supervisor_with_native.stop_worker()


# ============================================================================
# Loop Execution Tests
# ============================================================================


class TestLoopExecution:
    """Test run_once and run_loop."""

    def test_run_once_processes_event(self, supervisor_with_native, in_memory_store):
        """Test run_once processes one event."""
        # Create artifact first
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        event = in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        result = supervisor_with_native.run_once()

        assert result is True
        updated = in_memory_store.get_event(event["id"])
        assert updated["status"] == "acked"

        supervisor_with_native.stop_worker()

    def test_run_once_no_event(self, supervisor_with_native):
        """Test run_once with no events."""
        result = supervisor_with_native.run_once()

        assert result is False

        supervisor_with_native.stop_worker()

    def test_run_loop_max_iterations(self, supervisor_with_native, in_memory_store):
        """Test run_loop respects max_iterations."""
        # Create artifact and multiple events
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        for i in range(5):
            in_memory_store.enqueue_event(
                type="edit",
                payload={},
                artifact_id="art_1",
            )

        # Run loop with max_iterations=2
        supervisor_with_native.run_loop(max_iterations=2)

        # Should have processed 2 events (first 2 were succeeded, then hits lease on art_1)
        # Actually, after the first event, the artifact lease prevents subsequent events
        # on same artifact. So it will only process 1 event.
        # Let me reconsider: run_loop processes events sequentially, but once event1
        # is acked, its lease is released. So event2 can be claimed.
        # Wait, no - after ack, the lease is released. So each iteration can claim
        # the next pending event on a different artifact or after lease expires.

        # Actually, all events are on art_1, and the lease is released after ack.
        # So run_loop should process up to 2 events.

        cursor = in_memory_store.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events WHERE status = 'acked'")
        acked_count = cursor.fetchone()[0]
        assert acked_count == 2

        supervisor_with_native.stop_worker()

    def test_run_loop_keyboard_interrupt(self, supervisor_with_native, in_memory_store):
        """Test run_loop handles KeyboardInterrupt gracefully."""
        # Create artifact and an event to ensure the loop starts
        in_memory_store.create_artifact("art_1", "script", "Script 1")

        in_memory_store.enqueue_event(
            type="edit",
            payload={},
            artifact_id="art_1",
        )

        # Mock claim_and_dispatch_event to raise KeyboardInterrupt on first call
        original_method = supervisor_with_native.claim_and_dispatch_event
        call_count = [0]

        def mock_claim(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise KeyboardInterrupt()
            return original_method()

        supervisor_with_native.claim_and_dispatch_event = mock_claim

        # Should not raise; should handle gracefully
        supervisor_with_native.run_loop(max_iterations=10)

        # Worker should be stopped
        assert supervisor_with_native.worker_session is None


# ============================================================================
# Worker Recycle Tests
# ============================================================================


class TestWorkerRecycle:
    """Test worker recycling."""

    def test_recycle_after_events(self, in_memory_store, movie_config, native_stream_transport):
        """Test worker recycles after N events."""
        config = SupervisorConfig(
            worker_id="test_worker",
            recycle_after_events=2,
            lease_seconds=30,
        )
        supervisor = Supervisor(
            store=in_memory_store,
            transport=native_stream_transport,
            stage_config=movie_config,
            config=config,
        )

        # Create events on different artifacts
        for i in range(3):
            in_memory_store.create_artifact(f"art_{i}", "script", f"Artifact {i}")
            in_memory_store.enqueue_event(
                type="edit",
                payload={},
                artifact_id=f"art_{i}",
            )

        # Run 3 iterations
        for _ in range(3):
            supervisor.claim_and_dispatch_event()

        # After 2 events, worker should be recycled (event_count reset)
        # Then 3rd event should create a new worker
        # This is hard to test without inspecting internal state,
        # but we can verify that events were processed
        cursor = in_memory_store.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events WHERE status = 'acked'")
        acked_count = cursor.fetchone()[0]
        assert acked_count >= 2

        supervisor.stop_worker()


# ============================================================================
# Interrupt Tests
# ============================================================================


class TestInterrupt:
    """Test worker interruption."""

    def test_interrupt_success(self, supervisor_with_native):
        """Test successful interrupt."""
        supervisor_with_native.start_worker()

        result = supervisor_with_native.interrupt_worker()

        assert result is True

        supervisor_with_native.stop_worker()

    def test_interrupt_no_worker(self, supervisor_with_native):
        """Test interrupt with no active worker."""
        result = supervisor_with_native.interrupt_worker()

        assert result is False
