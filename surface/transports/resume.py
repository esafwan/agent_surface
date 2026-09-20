"""
ResumeTransport: per-event subprocess fallback transport.

Per SPEC section 21: Fallback concept spawning a fresh subprocess per event,
using the pattern: agent --resume <session_id> "<serialized event>"
"""

import json
import subprocess
import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone


class ResumeSessionRef:
    """Reference to a resumed session (minimal state tracking)."""

    def __init__(self, session_id: str, project_context: Dict[str, Any]):
        self.session_id = session_id
        self.project_context = project_context
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.event_count = 0

    def is_alive(self) -> bool:
        """ResumeSessionRef doesn't maintain a process; always considered alive."""
        return True


class ResumeTransport:
    """
    WorkerTransport fallback implementation using per-event subprocess invocation.

    Spawns a fresh subprocess per event with command:
    agent --resume <session_id> "<serialized event>"
    """

    def __init__(
        self,
        command_prefix: Optional[List[str]] = None,
        timeout: int = 300,
    ):
        """
        Initialize the transport.

        Args:
            command_prefix: Base command to invoke (e.g., ["python", "my_agent.py"]).
                           If None, uses a default echo-based fallback.
            timeout: Overall operation timeout in seconds.
        """
        self.command_prefix = command_prefix or ["python", "-c", "import sys, json; print(json.dumps({'ok': True, 'result': json.loads(sys.argv[2])}))"]
        self.timeout = timeout
        self.sessions: Dict[str, ResumeSessionRef] = {}

    def start(self, project_context: Dict[str, Any]) -> ResumeSessionRef:
        """
        Create a session reference for resume-per-event pattern.

        No subprocess is spawned here; just a session reference is created.

        Args:
            project_context: Project configuration/context dict.

        Returns:
            ResumeSessionRef representing the session.
        """
        session_id = f"session_{uuid.uuid4().hex[:12]}"
        session_ref = ResumeSessionRef(session_id, project_context)
        self.sessions[session_id] = session_ref
        return session_ref

    def send_event(
        self,
        worker_session: ResumeSessionRef,
        event_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Send an event by spawning a fresh subprocess per event.

        Args:
            worker_session: The ResumeSessionRef session.
            event_context: Event context dict containing event details.

        Returns:
            Turn result dict with ok/result/error keys.
        """
        # Construct the full event envelope
        envelope = {
            "type": "event",
            "event_id": event_context.get("event_id"),
            "payload": event_context.get("payload", {}),
            "project_id": event_context.get("project_id"),
            "artifact_id": event_context.get("artifact_id"),
            "config": event_context.get("config"),
            "artifact": event_context.get("artifact"),
            "summary": event_context.get("summary"),
        }

        # Serialize event as JSON
        serialized_event = json.dumps(envelope)

        # Build command: command_prefix --resume <session_id> "<event_json>"
        command = self.command_prefix + [
            "--resume",
            worker_session.session_id,
            serialized_event,
        ]

        try:
            # Spawn subprocess, wait for completion
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            worker_session.event_count += 1

            if result.returncode != 0:
                return {
                    "ok": False,
                    "error": f"Worker exited with code {result.returncode}: {result.stderr}",
                }

            # Parse stdout as JSON result
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "error": f"Worker output not valid JSON: {result.stdout}",
                }

        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"Worker process timeout (>{self.timeout}s)",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"Transport error: {e}",
            }

    def interrupt(self, worker_session: ResumeSessionRef) -> Dict[str, Any]:
        """
        Interrupt the worker.

        Per ResumeTransport design, we cannot interrupt an already-spawned process.
        This is a no-op for resume-per-event.

        Args:
            worker_session: The session (unused).

        Returns:
            Result dict indicating not supported.
        """
        return {
            "ok": False,
            "error": "ResumeTransport does not support interrupt (per-event model)",
        }

    def is_alive(self, worker_session: ResumeSessionRef) -> bool:
        """
        Check if the session is alive.

        For ResumeTransport, sessions are always considered alive
        (we can spawn a new process any time).

        Args:
            worker_session: The session to check.

        Returns:
            Always True.
        """
        return worker_session.is_alive()

    def close(self, worker_session: ResumeSessionRef) -> None:
        """
        Close/forget a session reference.

        No actual process to terminate.

        Args:
            worker_session: The session to close.
        """
        if worker_session.session_id in self.sessions:
            del self.sessions[worker_session.session_id]

    def resume(
        self,
        session_ref: str,
        project_context: Dict[str, Any],
    ) -> ResumeSessionRef:
        """
        Resume a previous session reference.

        Per SPEC section 21: If resume is unavailable, create a fresh session
        using the rehydration summary from project_context.

        Args:
            session_ref: Reference to the previous session ID.
            project_context: Project configuration/context dict.

        Returns:
            ResumeSessionRef (may be the same session or a new one).
        """
        # If we still have the session, reuse it
        if session_ref in self.sessions:
            return self.sessions[session_ref]

        # Otherwise, create a new session with the project context
        return self.start(project_context)
