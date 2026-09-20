"""
Unit tests for ACPTransport WorkerTransport implementation.

Tests cover:
- ACPTransport with mock/injected ACP client
- start/send_event/interrupt/is_alive/close/resume methods
- Graceful error handling when real ACP SDK is not installed
- Mock ACP client following the duck-typed protocol
"""

import json
import pytest
from unittest.mock import Mock, MagicMock

from surface.transports.acp import ACPTransport, ACPSessionRef


# ============================================================================
# Fixtures: Mock ACP Client
# ============================================================================


class MockACPClient:
    """
    Mock ACP client for testing.

    Implements the duck-typed interface that real ACP SDK should provide:
    - create_session(context) -> session_id
    - send_prompt(session_id, prompt, timeout=None) -> response_dict
    - cancel_session(session_id) -> result_dict
    - get_session_status(session_id) -> status_dict
    - close_session(session_id) -> None
    """

    def __init__(self):
        self.sessions = {}  # session_id -> session_state
        self.prompts_sent = []  # Track sent prompts for inspection
        self.call_count = 0

    def create_session(self, context):
        """Create a new session and return session_id."""
        session_id = f"acp_session_{len(self.sessions)}"
        self.sessions[session_id] = {
            "context": context,
            "active": True,
            "prompt_count": 0,
        }
        return session_id

    def send_prompt(self, session_id, prompt, timeout=None):
        """Send a prompt and return response."""
        if session_id not in self.sessions:
            raise RuntimeError(f"Session {session_id} not found")

        self.sessions[session_id]["prompt_count"] += 1
        self.prompts_sent.append({
            "session_id": session_id,
            "prompt": prompt,
            "timeout": timeout,
        })

        # Return mock response
        return {
            "ok": True,
            "result": {
                "event_id": prompt.get("event_id"),
                "type": prompt.get("type"),
                "processed": True,
            },
        }

    def cancel_session(self, session_id):
        """Cancel operations in a session."""
        if session_id not in self.sessions:
            return {"ok": False, "error": f"Session {session_id} not found"}

        self.sessions[session_id]["active"] = False
        return {"ok": True, "message": "Session cancelled"}

    def get_session_status(self, session_id):
        """Get session status."""
        if session_id not in self.sessions:
            return {"active": False}

        return {"active": self.sessions[session_id]["active"]}

    def close_session(self, session_id):
        """Close a session."""
        if session_id in self.sessions:
            self.sessions[session_id]["active"] = False


class FailingACPClient:
    """Mock ACP client that fails on operations."""

    def create_session(self, context):
        raise RuntimeError("ACP service unavailable")

    def send_prompt(self, session_id, prompt, timeout=None):
        raise RuntimeError("ACP send failed")

    def cancel_session(self, session_id):
        raise RuntimeError("ACP cancel failed")

    def get_session_status(self, session_id):
        raise RuntimeError("ACP status check failed")

    def close_session(self, session_id):
        raise RuntimeError("ACP close failed")


@pytest.fixture
def mock_acp_client():
    """Fixture providing a mock ACP client."""
    return MockACPClient()


@pytest.fixture
def failing_acp_client():
    """Fixture providing a failing mock ACP client."""
    return FailingACPClient()


# ============================================================================
# ACPTransport Constructor Tests
# ============================================================================


class TestACPTransportInit:
    """Test ACPTransport initialization."""

    def test_init_with_mock_client(self, mock_acp_client):
        """Test initialization with injected mock client."""
        transport = ACPTransport(acp_client=mock_acp_client)

        assert transport.acp_client is mock_acp_client
        assert transport.timeout == 30

    def test_init_with_custom_timeout(self, mock_acp_client):
        """Test initialization with custom timeout."""
        transport = ACPTransport(acp_client=mock_acp_client, timeout=60)

        assert transport.timeout == 60

    def test_init_without_client_and_no_acp_sdk_raises(self):
        """
        Test that initialization without a client and no ACP SDK installed
        raises a clear error.

        This environment does NOT have ACP SDK installed, so this should raise.
        """
        with pytest.raises(RuntimeError) as exc_info:
            ACPTransport(acp_client=None)

        error_msg = str(exc_info.value).lower()
        assert "acp" in error_msg
        assert "installed" in error_msg or "not found" in error_msg or "failed" in error_msg


# ============================================================================
# ACPTransport.start() Tests
# ============================================================================


class TestACPTransportStart:
    """Test ACPTransport.start()"""

    def test_start_creates_session(self, mock_acp_client):
        """Test that start() creates a working session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test_proj"})

        assert session is not None
        assert session.session_id is not None
        assert session.is_active is True
        assert session.project_context == {"project_id": "test_proj"}
        assert transport.is_alive(session)

    def test_start_stores_session_reference(self, mock_acp_client):
        """Test that start() stores session in internal registry."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        assert session.session_id in transport.sessions
        assert transport.sessions[session.session_id] is session

    def test_start_with_full_project_context(self, mock_acp_client):
        """Test start() with complete project context."""
        context = {
            "project_id": "movie_01",
            "config": {"version": "1"},
            "summary": {"stage_counts": {"script": 1}},
            "constraints": ["16:9"],
        }

        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start(context)

        assert session.project_context == context

    def test_start_failure_raises(self, failing_acp_client):
        """Test that start() raises on ACP client failure."""
        transport = ACPTransport(acp_client=failing_acp_client)

        with pytest.raises(RuntimeError) as exc_info:
            transport.start({"project_id": "test"})

        assert "create_session" in str(exc_info.value).lower() or "failed" in str(exc_info.value).lower()

    def test_start_multiple_sessions(self, mock_acp_client):
        """Test starting multiple independent sessions."""
        transport = ACPTransport(acp_client=mock_acp_client)

        session1 = transport.start({"project_id": "proj1"})
        session2 = transport.start({"project_id": "proj2"})

        assert session1.session_id != session2.session_id
        assert transport.is_alive(session1)
        assert transport.is_alive(session2)


# ============================================================================
# ACPTransport.send_event() Tests
# ============================================================================


class TestACPTransportSendEvent:
    """Test ACPTransport.send_event()"""

    def test_send_event_success(self, mock_acp_client):
        """Test successful event send and response."""
        transport = ACPTransport(acp_client=mock_acp_client)
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
        assert result["result"]["type"] == "edit"

    def test_send_event_inactive_session(self, mock_acp_client):
        """Test send_event on inactive session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        # Mark session as inactive
        session.is_active = False

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }

        result = transport.send_event(session, event_context)

        assert result["ok"] is False
        assert "not active" in result["error"].lower()

    def test_send_event_constructs_prompt_envelope(self, mock_acp_client):
        """Test that send_event constructs correct prompt envelope."""
        transport = ACPTransport(acp_client=mock_acp_client)
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

        # Verify the prompt was sent correctly via mock client
        assert len(mock_acp_client.prompts_sent) == 1
        sent_prompt = mock_acp_client.prompts_sent[0]
        assert sent_prompt["session_id"] == session.session_id
        assert sent_prompt["prompt"]["event_id"] == "evt_123"
        assert sent_prompt["prompt"]["type"] == "revise"
        assert sent_prompt["prompt"]["artifact_id"] == "art_789"
        assert sent_prompt["prompt"]["payload"]["note"] == "Make it brighter"

    def test_send_event_with_timeout(self, mock_acp_client):
        """Test that send_event passes timeout to ACP client."""
        transport = ACPTransport(acp_client=mock_acp_client, timeout=45)
        session = transport.start({"project_id": "test"})

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }

        result = transport.send_event(session, event_context)

        # Check that timeout was passed
        assert mock_acp_client.prompts_sent[0]["timeout"] == 45

    def test_send_event_failure(self, failing_acp_client):
        """Test send_event failure handling."""
        transport = ACPTransport(acp_client=failing_acp_client)

        # Manually create a session to bypass the start() error
        # (start() would fail with failing_acp_client)
        session_ref = ACPSessionRef("test_session", failing_acp_client, {"project_id": "test"})

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }

        result = transport.send_event(session_ref, event_context)

        assert result["ok"] is False
        assert "failed" in result["error"].lower()

    def test_send_event_updates_activity(self, mock_acp_client):
        """Test that send_event updates session activity timestamp."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        original_activity = session.last_activity

        event_context = {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        }

        result = transport.send_event(session, event_context)

        assert result["ok"] is True
        assert session.last_activity != original_activity


# ============================================================================
# ACPTransport.interrupt() Tests
# ============================================================================


class TestACPTransportInterrupt:
    """Test ACPTransport.interrupt()"""

    def test_interrupt_active_session(self, mock_acp_client):
        """Test interrupt on an active session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        result = transport.interrupt(session)

        assert result["ok"] is True
        # Session should be marked inactive by mock client
        assert not mock_acp_client.sessions[session.session_id]["active"]

    def test_interrupt_inactive_session(self, mock_acp_client):
        """Test interrupt on an inactive session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})
        session.is_active = False

        result = transport.interrupt(session)

        assert result["ok"] is False
        assert "not active" in result["error"].lower()

    def test_interrupt_failure(self, failing_acp_client):
        """Test interrupt failure handling."""
        session_ref = ACPSessionRef("test_session", failing_acp_client, {"project_id": "test"})

        transport = ACPTransport(acp_client=failing_acp_client)
        result = transport.interrupt(session_ref)

        assert result["ok"] is False
        assert "failed" in result["error"].lower()


# ============================================================================
# ACPTransport.is_alive() Tests
# ============================================================================


class TestACPTransportIsAlive:
    """Test ACPTransport.is_alive()"""

    def test_is_alive_active_session(self, mock_acp_client):
        """Test is_alive on an active session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        assert transport.is_alive(session) is True

    def test_is_alive_inactive_session(self, mock_acp_client):
        """Test is_alive on an inactive session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        session.is_active = False

        assert transport.is_alive(session) is False

    def test_is_alive_after_cancel(self, mock_acp_client):
        """Test is_alive after cancelling the session."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        transport.interrupt(session)

        # After cancellation, session should not be alive
        assert transport.is_alive(session) is False

    def test_is_alive_handles_status_check_failure(self, failing_acp_client):
        """Test is_alive gracefully handles status check failure."""
        session_ref = ACPSessionRef("test_session", failing_acp_client, {"project_id": "test"})

        transport = ACPTransport(acp_client=failing_acp_client)
        result = transport.is_alive(session_ref)

        # Should return False on failure to check status
        assert result is False


# ============================================================================
# ACPTransport.close() Tests
# ============================================================================


class TestACPTransportClose:
    """Test ACPTransport.close()"""

    def test_close_marks_inactive(self, mock_acp_client):
        """Test that close() marks session as inactive."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        assert transport.is_alive(session) is True
        transport.close(session)
        assert transport.is_alive(session) is False

    def test_close_removes_from_registry(self, mock_acp_client):
        """Test that close() removes session from internal registry."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        assert session.session_id in transport.sessions
        transport.close(session)
        assert session.session_id not in transport.sessions

    def test_close_idempotent(self, mock_acp_client):
        """Test that close() is idempotent."""
        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        transport.close(session)
        transport.close(session)  # Should not raise


# ============================================================================
# ACPTransport.resume() Tests
# ============================================================================


class TestACPTransportResume:
    """Test ACPTransport.resume()"""

    def test_resume_reuses_existing_session(self, mock_acp_client):
        """Test that resume() reuses an existing session."""
        transport = ACPTransport(acp_client=mock_acp_client)

        original = transport.start({"project_id": "test"})
        original_id = original.session_id

        resumed = transport.resume(original_id, {"project_id": "test"})

        assert resumed.session_id == original_id
        assert resumed is original

    def test_resume_creates_new_if_inactive(self, mock_acp_client):
        """Test that resume() creates new session if original is inactive."""
        transport = ACPTransport(acp_client=mock_acp_client)

        original = transport.start({"project_id": "test"})
        original_id = original.session_id
        original.is_active = False

        resumed = transport.resume(original_id, {"project_id": "test"})

        # Should create new session since original is inactive
        assert resumed.session_id != original_id

    def test_resume_creates_new_if_missing(self, mock_acp_client):
        """Test that resume() creates new session if original is missing."""
        transport = ACPTransport(acp_client=mock_acp_client)

        resumed = transport.resume("nonexistent_session", {"project_id": "test"})

        assert resumed is not None
        assert resumed.session_id != "nonexistent_session"
        assert transport.is_alive(resumed)

    def test_resume_with_new_project_context(self, mock_acp_client):
        """Test resume() with new project context on new session creation."""
        transport = ACPTransport(acp_client=mock_acp_client)

        original_context = {"project_id": "proj1"}
        original = transport.start(original_context)
        transport.close(original)

        new_context = {
            "project_id": "proj2",
            "config": {"version": "2"},
            "summary": {"stage_counts": {"script": 2}},
        }
        resumed = transport.resume("missing_session", new_context)

        assert resumed.project_context == new_context
        assert transport.is_alive(resumed)


# ============================================================================
# Integration Tests
# ============================================================================


class TestACPTransportIntegration:
    """Integration tests for the full transport lifecycle."""

    def test_full_session_lifecycle(self, mock_acp_client):
        """Test complete session lifecycle: start → send_event → interrupt → close."""
        transport = ACPTransport(acp_client=mock_acp_client)

        # Start
        session = transport.start({"project_id": "test"})
        assert transport.is_alive(session)

        # Send event
        result = transport.send_event(session, {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {"content": "Test"},
        })
        assert result["ok"] is True

        # Interrupt
        interrupt_result = transport.interrupt(session)
        assert interrupt_result["ok"] is True

        # Close
        transport.close(session)
        assert not transport.is_alive(session)

    def test_multiple_sessions_independence(self, mock_acp_client):
        """Test that multiple sessions operate independently."""
        transport = ACPTransport(acp_client=mock_acp_client)

        session1 = transport.start({"project_id": "proj1"})
        session2 = transport.start({"project_id": "proj2"})

        # Send events to both
        result1 = transport.send_event(session1, {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "proj1",
            "payload": {},
        })
        result2 = transport.send_event(session2, {
            "event_id": "evt_002",
            "type": "revise",
            "project_id": "proj2",
            "payload": {},
        })

        assert result1["ok"] is True
        assert result2["ok"] is True
        assert result1["result"]["event_id"] == "evt_001"
        assert result2["result"]["event_id"] == "evt_002"

        # Close one, other still alive
        transport.close(session1)
        assert not transport.is_alive(session1)
        assert transport.is_alive(session2)

    def test_session_ref_tracks_activity(self, mock_acp_client):
        """Test that ACPSessionRef tracks activity timestamp."""
        import time

        transport = ACPTransport(acp_client=mock_acp_client)
        session = transport.start({"project_id": "test"})

        initial_activity = session.last_activity

        # Wait a tiny bit
        time.sleep(0.01)

        # Send an event (which updates activity)
        transport.send_event(session, {
            "event_id": "evt_001",
            "type": "edit",
            "project_id": "test",
            "payload": {},
        })

        # Activity should be updated
        assert session.last_activity > initial_activity
