"""
Unit tests for WorkerTransport implementations.

Tests cover:
- NativeStreamTransport: start/send_event/interrupt/is_alive/close/resume
- ResumeTransport: same interface via per-event invocation
- Mock subprocess communication via JSON lines
"""

import json
import pytest
import subprocess
import sys
from pathlib import Path

from surface.transports.native_stream import NativeStreamTransport, WorkerSession
from surface.transports.resume import ResumeTransport, ResumeSessionRef


# ============================================================================
# Fixtures: Mock worker subprocess for testing
# ============================================================================

@pytest.fixture
def echo_worker_command():
    """
    Command for a simple echo worker that reads JSON and echoes it back.

    This is a minimal worker suitable for testing without requiring a real agent.
    It reads JSON lines from stdin, echoes them back to stdout as {"ok": True, "result": <input>}.
    """
    return [
        sys.executable,
        "-c",
        """
import sys
import json
for line in sys.stdin:
    try:
        data = json.loads(line)
        response = {"ok": True, "result": data}
        print(json.dumps(response))
        sys.stdout.flush()
    except Exception as e:
        response = {"ok": False, "error": str(e)}
        print(json.dumps(response))
        sys.stdout.flush()
""",
    ]


@pytest.fixture
def failing_worker_command():
    """
    Command for a worker that always fails.
    """
    return [
        sys.executable,
        "-c",
        """
import sys
import json
for line in sys.stdin:
    response = {"ok": False, "error": "Worker intentionally failed"}
    print(json.dumps(response))
    sys.stdout.flush()
    sys.exit(1)
""",
    ]


# ============================================================================
# NativeStreamTransport Tests
# ============================================================================


class TestNativeStreamTransportStart:
    """Test NativeStreamTransport.start()"""

    def test_start_creates_session(self, echo_worker_command):
        """Test that start() creates a working session."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test_proj"})

        assert session is not None
        assert session.session_id is not None
        assert session.process is not None
        assert transport.is_alive(session)

        transport.close(session)

    def test_start_with_default_command(self):
        """Test that start() works with default command."""
        transport = NativeStreamTransport()
        session = transport.start({"project_id": "test"})

        assert session is not None
        assert transport.is_alive(session)

        transport.close(session)

    def test_start_invalid_command_raises(self):
        """Test that start() raises on invalid command."""
        transport = NativeStreamTransport(command=["/nonexistent/command/xyz"])
        with pytest.raises(RuntimeError):
            transport.start({"project_id": "test"})


class TestNativeStreamTransportSendEvent:
    """Test NativeStreamTransport.send_event()"""

    def test_send_event_success(self, echo_worker_command):
        """Test successful event send and response."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "artifact_id": "art_001",
            "payload": {"content": "Hello, World!"},
        }

        result = transport.send_event(session, event_context)

        assert result["ok"] is True
        assert result["result"] is not None
        assert result["result"]["event_id"] == "evt_001"

        transport.close(session)

    def test_send_event_dead_process(self, echo_worker_command):
        """Test send_event on dead worker."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})
        transport.close(session)

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }

        result = transport.send_event(session, event_context)
        assert result["ok"] is False
        assert "not running" in result["error"].lower()

    def test_send_event_constructs_envelope(self, echo_worker_command):
        """Test that send_event constructs correct envelope."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        event_context = {
            "event_id": "evt_123",
            "type": "revise",
            "project_id": "proj_456",
            "artifact_id": "art_789",
            "payload": {"note": "Make it brighter"},
            "config": {"version": "1"},
            "summary": {"pending": 5},
        }

        result = transport.send_event(session, event_context)

        assert result["ok"] is True
        # Echo worker returns the envelope as-is in result field
        assert result["result"]["event_id"] == "evt_123"
        assert result["result"]["envelope"] == "event"  # transport-framing marker
        assert result["result"]["type"] == "revise"  # the actual domain event type
        assert result["result"]["payload"]["note"] == "Make it brighter"
        assert result["result"]["artifact_id"] == "art_789"

        transport.close(session)


    def test_send_event_times_out_on_hung_worker(self):
        """A worker subprocess that never responds must not block send_event
        forever (SPEC section 22: supervisor MUST detect turn timeout)."""
        hung_worker_command = [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.readline(); time.sleep(60)",
        ]
        transport = NativeStreamTransport(command=hung_worker_command, timeout=1)
        session = transport.start({"project_id": "test"})

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }

        import time

        start = time.monotonic()
        result = transport.send_event(session, event_context)
        elapsed = time.monotonic() - start

        assert result["ok"] is False
        assert "timeout" in result["error"].lower()
        # Bounded by transport.timeout, not blocked indefinitely.
        assert elapsed < 5

        transport.close(session)

    def test_timed_out_session_is_never_reused_for_a_later_reply(self):
        """A session that times out must be poisoned so a late-arriving reply
        can never be read as the answer to a DIFFERENT, later send_event call
        on the same session. Without this, there is no request/response
        correlation on the stdout stream, so a stale reply would silently
        ack/fail whichever event is dispatched next."""
        # This worker replies to the FIRST line only after a delay long
        # enough to be classified as a timeout, then immediately replies to
        # anything else it reads. If the session were reused, that first
        # (stale) reply would be consumed as the answer to the second call.
        slow_then_fast_worker = [
            sys.executable,
            "-c",
            (
                "import sys, time, json\n"
                "line = sys.stdin.readline()\n"
                "time.sleep(0.5)\n"
                "print(json.dumps({'ok': True, 'stale': True}), flush=True)\n"
            ),
        ]
        transport = NativeStreamTransport(command=slow_then_fast_worker, timeout=0.1)
        session = transport.start({"project_id": "test"})

        first = transport.send_event(session, {
            "event_id": "evt_first", "type": "edit", "payload": {},
        })
        assert first["ok"] is False
        assert "timeout" in first["error"].lower()

        # The session must be dead now — is_alive() must reflect that so the
        # Supervisor is forced to start/resume a fresh worker session rather
        # than reusing this one.
        assert transport.is_alive(session) is False

        # Even if something tried to send another event on this same
        # (poisoned) session, it must fail immediately rather than picking
        # up the stale reply that eventually arrives.
        second = transport.send_event(session, {
            "event_id": "evt_second", "type": "edit", "payload": {},
        })
        assert second["ok"] is False
        assert second.get("result", {}).get("stale") is not True

        transport.close(session)


class TestNativeStreamTransportInterrupt:
    """Test NativeStreamTransport.interrupt()"""

    def test_interrupt_living_process(self, echo_worker_command):
        """Test interrupt on a living process."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        # Send event (doesn't block)
        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }
        result = transport.send_event(session, event_context)
        assert result["ok"] is True

        # Interrupt
        interrupt_result = transport.interrupt(session)
        assert interrupt_result["ok"] is True

        transport.close(session)

    def test_interrupt_dead_process(self, echo_worker_command):
        """Test interrupt on a dead process."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})
        transport.close(session)

        interrupt_result = transport.interrupt(session)
        assert interrupt_result["ok"] is False


class TestNativeStreamTransportIsAlive:
    """Test NativeStreamTransport.is_alive()"""

    def test_is_alive_living_process(self, echo_worker_command):
        """Test is_alive on a running process."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        assert transport.is_alive(session) is True

        transport.close(session)

    def test_is_alive_dead_process(self, echo_worker_command):
        """Test is_alive on a terminated process."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        transport.close(session)

        assert transport.is_alive(session) is False


class TestNativeStreamTransportClose:
    """Test NativeStreamTransport.close()"""

    def test_close_terminates_process(self, echo_worker_command):
        """Test that close() terminates the process."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        assert transport.is_alive(session) is True
        transport.close(session)
        assert transport.is_alive(session) is False

    def test_close_idempotent(self, echo_worker_command):
        """Test that close() is idempotent."""
        transport = NativeStreamTransport(command=echo_worker_command)
        session = transport.start({"project_id": "test"})

        transport.close(session)
        transport.close(session)  # Should not raise


class TestNativeStreamTransportResume:
    """Test NativeStreamTransport.resume()"""

    def test_resume_creates_new_session(self, echo_worker_command):
        """Test that resume() creates a new session."""
        transport = NativeStreamTransport(command=echo_worker_command)

        # Start original session
        original = transport.start({"project_id": "test"})
        original_id = original.session_id
        transport.close(original)

        # Resume should create a new session
        resumed = transport.resume(original_id, {"project_id": "test"})

        assert resumed is not None
        assert resumed.session_id != original_id  # New session ID
        assert transport.is_alive(resumed)

        transport.close(resumed)


# ============================================================================
# ResumeTransport Tests
# ============================================================================


class TestResumeTransportStart:
    """Test ResumeTransport.start()"""

    def test_start_creates_session_ref(self):
        """Test that start() creates a session reference."""
        transport = ResumeTransport()
        session_ref = transport.start({"project_id": "test"})

        assert session_ref is not None
        assert session_ref.session_id is not None
        assert transport.is_alive(session_ref)


class TestResumeTransportSendEvent:
    """Test ResumeTransport.send_event()"""

    def test_send_event_spawns_subprocess(self):
        """Test that send_event spawns a subprocess per event."""
        # Use a simple Python script to simulate the agent receiving --resume and event
        import tempfile
        import os

        # Create a temporary script that acts as the agent
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("""
import sys
import json

if len(sys.argv) >= 3 and sys.argv[1] == '--resume':
    session_id = sys.argv[2]
    event_json = sys.argv[3] if len(sys.argv) > 3 else '{}'
    try:
        data = json.loads(event_json)
        response = {"ok": True, "result": {"session_id": session_id, "event_id": data.get("event_id")}}
        print(json.dumps(response))
        sys.exit(0)
    except Exception as e:
        response = {"ok": False, "error": str(e)}
        print(json.dumps(response))
        sys.exit(1)
""")
            script_path = f.name

        try:
            transport = ResumeTransport(command_prefix=[sys.executable, script_path])
            session_ref = transport.start({"project_id": "test"})

            event_context = {
                "event_id": "evt_001",
                "type": "edit",
                "project_id": "test",
                "payload": {"content": "Test"},
            }

            result = transport.send_event(session_ref, event_context)

            assert result["ok"] is True
            assert result["result"]["session_id"] == session_ref.session_id
            assert result["result"]["event_id"] == "evt_001"
            assert session_ref.event_count >= 1
        finally:
            os.unlink(script_path)


class TestResumeTransportInterrupt:
    """Test ResumeTransport.interrupt()"""

    def test_interrupt_not_supported(self):
        """Test that interrupt returns error (not supported)."""
        transport = ResumeTransport()
        session_ref = transport.start({"project_id": "test"})

        result = transport.interrupt(session_ref)
        assert result["ok"] is False
        assert "does not support interrupt" in result["error"].lower()


class TestResumeTransportIsAlive:
    """Test ResumeTransport.is_alive()"""

    def test_is_alive_always_true(self):
        """Test that is_alive always returns True."""
        transport = ResumeTransport()
        session_ref = transport.start({"project_id": "test"})

        assert transport.is_alive(session_ref) is True

        transport.close(session_ref)

        # Even after close, resume transport can spawn new processes
        # so it's "alive" in that sense
        assert transport.is_alive(session_ref) is True


class TestResumeTransportClose:
    """Test ResumeTransport.close()"""

    def test_close_removes_reference(self):
        """Test that close() removes the session reference."""
        transport = ResumeTransport()
        session_ref = transport.start({"project_id": "test"})

        assert session_ref.session_id in transport.sessions
        transport.close(session_ref)
        assert session_ref.session_id not in transport.sessions


class TestResumeTransportResume:
    """Test ResumeTransport.resume()"""

    def test_resume_reuses_existing_session(self):
        """Test that resume() reuses an existing session."""
        transport = ResumeTransport()
        original = transport.start({"project_id": "test"})
        original_id = original.session_id

        resumed = transport.resume(original_id, {"project_id": "test"})

        assert resumed.session_id == original_id

    def test_resume_creates_new_on_missing(self):
        """Test that resume() creates new session if old one missing."""
        transport = ResumeTransport()

        resumed = transport.resume("nonexistent_session", {"project_id": "test"})

        assert resumed is not None
        assert resumed.session_id != "nonexistent_session"
