"""
NativeStreamTransport: persistent subprocess with JSON/JSONL over stdin/stdout.

Per SPEC section 20: A persistent subprocess with JSON/JSONL over stdin/stdout,
using conceptual envelope: {"type":"event","event_id":"evt_143","payload":{...}}
"""

import json
import queue
import subprocess
import sys
import threading
import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone


class WorkerSession:
    """Represents an active worker session via a persistent subprocess."""

    def __init__(
        self,
        session_id: str,
        process: subprocess.Popen,
        command: List[str],
    ):
        self.session_id = session_id
        self.process = process
        self.command = command
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.last_activity = self.created_at
        self._lock = threading.Lock()
        # Set on a send_event timeout. Without request/response correlation,
        # a late reply to a timed-out turn would otherwise sit in
        # stdout_queue and be consumed as the reply to the *next* turn,
        # silently acking/failing the wrong event. Once poisoned this
        # session must never be reused for another send_event.
        self.poisoned = False

        # Background reader: readline() on a pipe is unbounded, so a hung
        # worker subprocess would block send_event() forever. A dedicated
        # daemon thread continuously drains stdout into a queue; send_event
        # then does a bounded queue.get(timeout=...) instead of a blocking
        # readline(), which is what actually enforces the turn timeout
        # (SPEC section 22: supervisor MUST detect turn timeout).
        self.stdout_queue: "queue.Queue" = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop, daemon=True
        )
        self._reader_thread.start()

    def _read_stdout_loop(self) -> None:
        """Continuously read lines from the subprocess stdout into a queue."""
        try:
            for line in iter(self.process.stdout.readline, ""):
                if line == "":
                    break
                self.stdout_queue.put(line)
        except Exception:
            pass
        finally:
            # Signal EOF/closed connection to any waiting consumer.
            self.stdout_queue.put(None)

    def is_running(self) -> bool:
        """Check if the subprocess is still running and not poisoned."""
        return not self.poisoned and self.process.poll() is None

    def update_activity(self) -> None:
        """Update the last activity timestamp."""
        self.last_activity = datetime.now(timezone.utc).isoformat()


class NativeStreamTransport:
    """
    WorkerTransport implementation using a persistent subprocess.

    Communicates with a subprocess via JSON lines over stdin/stdout.
    Envelope format: {"type":"event","event_id":"...","payload":{...}}
    """

    def __init__(
        self,
        command: Optional[List[str]] = None,
        timeout: int = 30,
        read_timeout: int = 5,
    ):
        """
        Initialize the transport.

        Args:
            command: Command to spawn as a list of argv strings.
                    If None, uses a default echo subprocess (for testing).
            timeout: Overall operation timeout in seconds.
            read_timeout: Timeout for reading individual lines in seconds.
        """
        self.command = command or [
            sys.executable,
            "-c",
            "import sys, json; [print(json.dumps(json.loads(line)), flush=True) for line in sys.stdin]",
        ]
        self.timeout = timeout
        self.read_timeout = read_timeout
        self.sessions: Dict[str, WorkerSession] = {}

    def start(self, project_context: Dict[str, Any]) -> WorkerSession:
        """
        Start a new worker session by spawning a subprocess.

        Args:
            project_context: Project configuration/context dict.

        Returns:
            WorkerSession representing the active subprocess.
        """
        session_id = f"session_{uuid.uuid4().hex[:12]}"
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffering
            )
        except Exception as e:
            raise RuntimeError(f"Failed to spawn subprocess: {e}")

        session = WorkerSession(session_id, process, self.command)
        self.sessions[session_id] = session
        return session

    def send_event(
        self,
        worker_session: WorkerSession,
        event_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Send an event to the worker and wait for the result.

        Args:
            worker_session: The WorkerSession to send to.
            event_context: Event context dict containing:
                - event_id
                - type
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
        if not worker_session.is_running():
            return {
                "ok": False,
                "error": "Worker process is not running",
            }

        # Construct the envelope to send to the worker. "envelope" marks this
        # as a transport-level event message; "type" carries the actual
        # domain event type (edit/revise/approve/...) that the worker
        # dispatches on — it must NOT be overwritten with a constant, or
        # every real event arrives at the worker as an unrecognized type.
        envelope = {
            "envelope": "event",
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
            # Send the envelope as JSON line
            message = json.dumps(envelope)
            worker_session.process.stdin.write(message + "\n")
            worker_session.process.stdin.flush()
            worker_session.update_activity()

            # Read response (single JSON line), bounded by self.timeout so a
            # hung worker subprocess cannot block the supervisor forever
            # (SPEC section 22: turn timeout MUST be detected).
            try:
                result_line = worker_session.stdout_queue.get(timeout=self.timeout)
            except queue.Empty:
                # There is no request/response correlation on this stream, so
                # if the worker eventually does reply, that reply would be
                # read as the answer to whatever the *next* send_event call
                # is for this session — silently acking/failing the wrong
                # event. Poison the session and kill the process so it can
                # never be reused; the caller (Supervisor) must start/resume
                # a fresh worker session for the next turn.
                worker_session.poisoned = True
                try:
                    worker_session.process.kill()
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": "timeout",
                }

            if not result_line:
                return {
                    "ok": False,
                    "error": "Worker process closed connection",
                }

            result = json.loads(result_line)
            worker_session.update_activity()
            return result

        except json.JSONDecodeError as e:
            return {
                "ok": False,
                "error": f"Invalid JSON from worker: {e}",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"Transport error: {e}",
            }

    def interrupt(self, worker_session: WorkerSession) -> Dict[str, Any]:
        """
        Interrupt the worker's current operation.

        Per SPEC section 28: Worker-turn cancel SHOULD call transport interrupt.
        Durable writes already committed remain.

        Args:
            worker_session: The session to interrupt.

        Returns:
            Result dict with ok/error.
        """
        if not worker_session.is_running():
            return {"ok": False, "error": "Worker process is not running"}

        try:
            # Send interrupt signal (SIGINT)
            worker_session.process.send_signal(subprocess.signal.SIGINT)
            worker_session.update_activity()
            return {"ok": True, "message": "Interrupt signal sent"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to interrupt: {e}"}

    def is_alive(self, worker_session: WorkerSession) -> bool:
        """
        Check if the worker session is alive.

        Args:
            worker_session: The session to check.

        Returns:
            True if the subprocess is running, False otherwise.
        """
        return worker_session.is_running()

    def close(self, worker_session: WorkerSession) -> None:
        """
        Close/terminate the worker session.

        Args:
            worker_session: The session to close.
        """
        if worker_session.session_id in self.sessions:
            del self.sessions[worker_session.session_id]

        if not worker_session.is_running():
            return

        try:
            # Graceful termination
            worker_session.process.terminate()
            worker_session.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Force kill if terminate didn't work
            worker_session.process.kill()
            worker_session.process.wait()
        except Exception:
            pass

    def resume(
        self,
        session_ref: str,
        project_context: Dict[str, Any],
    ) -> WorkerSession:
        """
        Resume or recreate a worker session from a session reference.

        Per SPEC section 21/23: Resume a previous session or create a new one.
        In practice, native stream sessions cannot truly resume a subprocess,
        so this creates a new session.

        Args:
            session_ref: Reference to the previous session (unused in native stream).
            project_context: Project configuration/context dict.

        Returns:
            New WorkerSession.
        """
        # Native stream doesn't persist subprocess state; create fresh session
        return self.start(project_context)
