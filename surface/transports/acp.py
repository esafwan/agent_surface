"""
ACPTransport: Agent Client Protocol (ACP) WorkerTransport implementation.

Per SPEC section 19:
- ACP SHOULD be the preferred standards-based live transport
- Supervisor acts as ACP client; coding agent acts as ACP agent
- Needed capabilities: session create/resume, prompt/event delivery,
  optional streaming progress, turn completion, cancellation
- ACP-specific details MUST remain inside this module

IMPORTANT DESIGN NOTE:
Since no real ACP Python SDK is confirmed available in this environment
and this is intended as a Phase 1 hardening implementation, this adapter
is designed against an assumed duck-typed ACP client interface rather
than a specific verified SDK. The assumed interface is documented in
ACPTransport docstrings. Real ACP integration would need to:

1. Verify the actual ACP Python SDK's method signatures
2. Update this adapter to match the verified SDK exactly
3. Test against the real SDK and a live ACP-compatible agent

The internal protocol interface assumes the ACP client supports:
- create_session(context_dict) -> session_id
- send_prompt(session_id, prompt_dict) -> response_dict
- cancel_session(session_id) -> result_dict
- get_session_status(session_id) -> status_dict
- close_session(session_id) -> None

Reference: https://agentclientprotocol.com/
"""

import json
import uuid
from typing import Any, Dict, Optional
from datetime import datetime, timezone

# Try to import ACP client library; if unavailable, set flag for runtime check
ACP_AVAILABLE = False
ACP_IMPORT_ERROR = None
try:
    # Adjust these imports when actual ACP SDK is verified
    # Common candidates: acp_sdk, acp, agent_client_protocol
    import acp  # type: ignore
    ACP_AVAILABLE = True
except ImportError as e:
    ACP_IMPORT_ERROR = str(e)


class ACPSessionRef:
    """Reference to an ACP agent session."""

    def __init__(self, session_id: str, acp_client: Any, project_context: Dict[str, Any]):
        self.session_id = session_id
        self.acp_client = acp_client  # Duck-typed ACP client
        self.project_context = project_context
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.last_activity = self.created_at
        self.is_active = True

    def update_activity(self) -> None:
        """Update the last activity timestamp."""
        self.last_activity = datetime.now(timezone.utc).isoformat()


class ACPTransport:
    """
    WorkerTransport implementation using Agent Client Protocol (ACP).

    This adapter assumes the ACP client is a duck-typed object providing:

    Methods:
    - create_session(context: Dict) -> str
        Create a new agent session. Context includes project_id, config, etc.
        Returns session_id (string).

    - send_prompt(session_id: str, prompt: Dict) -> Dict
        Send a prompt/event to the agent. Prompt includes type, event_id,
        payload, artifact details, etc.
        Returns response dict with ok (bool), result (optional), error (optional).

    - cancel_session(session_id: str) -> Dict
        Cancel an ongoing agent operation in the session.
        Returns result dict with ok (bool), error (optional).

    - get_session_status(session_id: str) -> Dict
        Check if the session is still active.
        Returns status dict with ok (bool), active (bool), error (optional).

    - close_session(session_id: str) -> None
        Close and cleanup a session.

    Since the real ACP SDK is not available in this environment, tests
    inject a mock ACP client satisfying this interface. Real integration
    requires verifying the actual ACP SDK signatures and updating this
    adapter accordingly.
    """

    def __init__(self, acp_client: Optional[Any] = None, timeout: int = 30):
        """
        Initialize ACPTransport.

        Args:
            acp_client: Duck-typed ACP client object. If None, attempts to
                       instantiate from the ACP SDK (if available).
                       For testing, inject a mock object.
            timeout: Overall operation timeout in seconds.

        Raises:
            ImportError: If acp_client is None and ACP SDK is not installed.
            RuntimeError: If ACP SDK is not available.
        """
        if acp_client is None:
            if not ACP_AVAILABLE:
                raise RuntimeError(
                    "ACP transport requires ACP SDK to be installed. "
                    f"Import error: {ACP_IMPORT_ERROR}. "
                    "Install ACP SDK or inject a mock client for testing. "
                    "For now, use NativeStreamTransport or ResumeTransport."
                )
            # If ACP is available, instantiate default client
            # (This is a placeholder; real SDK integration would do this properly)
            raise RuntimeError(
                "Real ACP SDK instantiation not yet implemented. "
                "Inject mock client for testing."
            )

        self.acp_client = acp_client
        self.timeout = timeout
        self.sessions: Dict[str, ACPSessionRef] = {}

    def start(self, project_context: Dict[str, Any]) -> ACPSessionRef:
        """
        Start a new ACP agent session.

        Per SPEC section 19: session create capability.

        Args:
            project_context: Project configuration/context dict including
                           project_id, config, summary, etc.

        Returns:
            ACPSessionRef representing the active ACP session.

        Raises:
            RuntimeError: If session creation fails.
        """
        try:
            # Request session creation via ACP client
            session_id = self.acp_client.create_session(project_context)

            if not session_id:
                raise RuntimeError("ACP client returned empty session_id")

            session_ref = ACPSessionRef(session_id, self.acp_client, project_context)
            self.sessions[session_id] = session_ref
            return session_ref

        except Exception as e:
            raise RuntimeError(f"Failed to create ACP session: {e}")

    def send_event(
        self,
        worker_session: ACPSessionRef,
        event_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Send an event/prompt to the ACP agent and wait for the result.

        Per SPEC section 19: prompt/event delivery, optional streaming
        progress, turn completion.

        Args:
            worker_session: The ACPSessionRef session.
            event_context: Event context dict containing:
                - event_id
                - type (edit, revise, regenerate, etc.)
                - artifact_id (optional)
                - payload (dict)
                - project_id
                - config (optional)
                - artifact (optional)
                - summary (optional)

        Returns:
            Turn result dict with keys:
            - ok: bool
            - result: optional dict of worker-produced artifacts/versions
            - error: optional error message
        """
        if not worker_session.is_active:
            return {
                "ok": False,
                "error": "ACP session is not active",
            }

        # Construct the prompt envelope for ACP
        prompt = {
            "type": event_context.get("type"),
            "event_id": event_context.get("event_id"),
            "payload": event_context.get("payload", {}),
            "project_id": event_context.get("project_id"),
            "artifact_id": event_context.get("artifact_id"),
            "config": event_context.get("config"),
            "artifact": event_context.get("artifact"),
            "summary": event_context.get("summary"),
        }

        try:
            # Send prompt via ACP and get response
            # (Real ACP SDK may support streaming; for now, simple request/response)
            response = self.acp_client.send_prompt(
                worker_session.session_id,
                prompt,
                timeout=self.timeout,
            )

            worker_session.update_activity()

            # Normalize response to standard turn result format
            if isinstance(response, dict):
                if "ok" in response:
                    return response
                else:
                    # Assume success if no ok field but response is dict
                    return {"ok": True, "result": response}
            else:
                return {
                    "ok": False,
                    "error": f"Unexpected response type from ACP: {type(response)}",
                }

        except Exception as e:
            return {
                "ok": False,
                "error": f"ACP send_prompt failed: {e}",
            }

    def interrupt(self, worker_session: ACPSessionRef) -> Dict[str, Any]:
        """
        Interrupt the worker's current operation.

        Per SPEC section 19: cancellation capability.
        Per SPEC section 28: Worker-turn cancel SHOULD call transport interrupt.

        Args:
            worker_session: The session to interrupt.

        Returns:
            Result dict with ok/error.
        """
        if not worker_session.is_active:
            return {"ok": False, "error": "ACP session is not active"}

        try:
            result = self.acp_client.cancel_session(worker_session.session_id)
            worker_session.update_activity()

            if isinstance(result, dict) and "ok" in result:
                return result
            else:
                return {"ok": True, "message": "Cancel request sent to ACP agent"}

        except Exception as e:
            return {"ok": False, "error": f"ACP cancel failed: {e}"}

    def is_alive(self, worker_session: ACPSessionRef) -> bool:
        """
        Check if the ACP session is alive.

        Args:
            worker_session: The session to check.

        Returns:
            True if the session is active and responsive, False otherwise.
        """
        if not worker_session.is_active:
            return False

        try:
            status = self.acp_client.get_session_status(worker_session.session_id)

            if isinstance(status, dict):
                return status.get("active", False)
            else:
                return True  # Assume alive if no status dict

        except Exception:
            # If we can't check status, assume dead
            return False

    def close(self, worker_session: ACPSessionRef) -> None:
        """
        Close/terminate the ACP agent session.

        Args:
            worker_session: The session to close.
        """
        if worker_session.session_id in self.sessions:
            del self.sessions[worker_session.session_id]

        try:
            self.acp_client.close_session(worker_session.session_id)
        except Exception:
            pass

        worker_session.is_active = False

    def resume(
        self,
        session_ref: str,
        project_context: Dict[str, Any],
    ) -> ACPSessionRef:
        """
        Resume or recreate an ACP agent session.

        Per SPEC section 19: session resume capability.
        Per SPEC section 21/23: Resume previous session or rehydrate fresh.

        Args:
            session_ref: Reference to the previous session ID.
            project_context: Project configuration/context dict.

        Returns:
            ACPSessionRef (resumed session if available, new session otherwise).
        """
        # If we still have the session, reuse it
        if session_ref in self.sessions:
            existing = self.sessions[session_ref]
            if existing.is_active:
                return existing

        # Otherwise, create a new session with the project context
        return self.start(project_context)
