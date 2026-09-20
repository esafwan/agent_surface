"""
Command-line interface for Agent Surface Board.

Exposes store tools, inbox operations, project management, and board runtime
via argparse. JSON-only results go to stdout; diagnostics to stderr.

Per SPEC section 17 (Store/Inbox Tools CLI) and section 9 (Runtime Directory).
"""

import argparse
import importlib.util
import inspect
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from surface.store import Store
from surface.stages.config import StageConfig
from surface.supervisor import Supervisor, SupervisorConfig
from surface.transports.native_stream import NativeStreamTransport
from surface.poller import Poller
from surface.providers.image import MockImageProvider
from surface.providers.video import MockVideoProvider

# Configure logging for diagnostics (stderr)
logging.basicConfig(
    level=logging.INFO,
    format="%(name)s: %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


class SurfaceCLI:
    """Main CLI handler for agent_surface board runtime."""

    def __init__(self, db_path: str = ".surface-board/state.sqlite3"):
        self.db_path = db_path
        self.store: Optional[Store] = None
        self.project_dir = Path(".surface-board")
        # Populated by serve(): {"runtime_dir", "token", "port", "host"}.
        self.runtime_info: Optional[Dict[str, Any]] = None
        self._board_thread: Optional[threading.Thread] = None

    def get_store(self, create: bool = False) -> Optional[Store]:
        """Get or create a store instance. Returns None if DB doesn't exist and create=False."""
        if self.store:
            return self.store

        db_file = Path(self.db_path)
        if not db_file.exists() and not create:
            return None

        if create:
            db_file.parent.mkdir(parents=True, exist_ok=True)

        self.store = Store(str(db_file))
        return self.store

    # =========================================================================
    # Store Commands
    # =========================================================================

    def store_get(self, artifact_id: str) -> None:
        """Get a single artifact by ID."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            artifact = store.get_artifact(artifact_id)
            if not artifact:
                self._error(f"Artifact '{artifact_id}' not found")
                return
            self._json_output(artifact)
        except Exception as e:
            self._error(f"Error getting artifact: {e}")

    def store_list(self, stage: Optional[str] = None, status: Optional[str] = None) -> None:
        """List artifacts, optionally filtered by stage and/or status."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            artifacts = store.list_artifacts(stage=stage, status=status)
            # Include counts
            self._json_output({
                "artifacts": artifacts,
                "count": len(artifacts),
            })
        except Exception as e:
            self._error(f"Error listing artifacts: {e}")

    def store_put_version(self, artifact_id: str, file: Optional[str] = None) -> None:
        """Create a new version from JSON input (stdin or --file)."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            # Read JSON from file or stdin
            if file:
                with open(file, "r") as f:
                    payload = json.load(f)
            else:
                payload = json.load(sys.stdin)

            # Extract parameters; artifact_id can come from CLI or payload
            actual_artifact_id = artifact_id or payload.get("artifact_id")
            if not actual_artifact_id:
                self._error("artifact_id must be provided via --artifact-id or in JSON")
                return

            result = store.put_version(
                artifact_id=actual_artifact_id,
                content=payload.get("content"),
                content_ref=payload.get("content_ref"),
                content_type=payload.get("content_type"),
                prompt=payload.get("prompt"),
                params=payload.get("params"),
                created_by=payload.get("created_by", "worker"),
                note=payload.get("note"),
                source_event_id=payload.get("source_event_id"),
                select=payload.get("select", True),
            )
            self._json_output(result)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON input: {e}")
        except Exception as e:
            self._error(f"Error creating version: {e}")

    def store_select_version(
        self, artifact_id: str, version_id: str,
        expected_selected_version_id: Optional[str] = None
    ) -> None:
        """Select a version for an artifact."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            result = store.select_version(
                artifact_id=artifact_id,
                version_id=version_id,
                expected_selected_version_id=expected_selected_version_id,
            )
            self._json_output(result)
        except Exception as e:
            self._error(f"Error selecting version: {e}")

    def store_set_status(self, artifact_id: str, status: str) -> None:
        """Set the status of an artifact."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            artifact = store.set_status(artifact_id, status)
            self._json_output(artifact)
        except Exception as e:
            self._error(f"Error setting status: {e}")

    def store_add_dependency(
        self, upstream_artifact_id: str, downstream_artifact_id: str,
        kind: str = "content"
    ) -> None:
        """Add a dependency between two artifacts."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            store.add_dependency(upstream_artifact_id, downstream_artifact_id, kind)
            self._json_output({
                "ok": True,
                "upstream_artifact_id": upstream_artifact_id,
                "downstream_artifact_id": downstream_artifact_id,
                "kind": kind,
            })
        except Exception as e:
            self._error(f"Error adding dependency: {e}")

    def store_graph(self, artifact_id: str) -> None:
        """Get the dependency graph for an artifact."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            graph = store.get_graph(artifact_id)
            self._json_output(graph)
        except Exception as e:
            self._error(f"Error getting graph: {e}")

    # =========================================================================
    # Job Commands
    # =========================================================================

    def store_job_start(self, artifact_id: str, provider: str, kind: str,
                       file: Optional[str] = None) -> None:
        """Create and start a job (reads request JSON from stdin or --file)."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            # Read job request from file or stdin
            if file:
                with open(file, "r") as f:
                    request = json.load(f)
            else:
                request = json.load(sys.stdin)

            job = store.create_job(
                artifact_id=artifact_id,
                provider=provider,
                kind=kind,
                request=request,
                cost_estimate=request.get("cost_estimate", 0.0),
            )
            self._json_output(job)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON input: {e}")
        except Exception as e:
            self._error(f"Error creating job: {e}")

    def store_job_finish(self, job_id: str, file: Optional[str] = None) -> None:
        """Mark a job as succeeded with result (reads result JSON from stdin or --file)."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            # Read result from file or stdin
            if file:
                with open(file, "r") as f:
                    result = json.load(f)
            else:
                result = json.load(sys.stdin)

            job = store.finish_job(job_id, result=result)
            self._json_output(job)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON input: {e}")
        except Exception as e:
            self._error(f"Error finishing job: {e}")

    def store_job_cancel(self, job_id: str) -> None:
        """Cancel a job."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            job = store.cancel_job(job_id)
            self._json_output(job)
        except Exception as e:
            self._error(f"Error cancelling job: {e}")

    # =========================================================================
    # Inbox Commands
    # =========================================================================

    def inbox_next(self, wait: int = 0, worker_id: str = "cli_worker") -> None:
        """Claim the next pending event, with optional polling."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            import time
            elapsed = 0
            while elapsed < wait:
                event = store.claim_next_event(worker_id)
                if event:
                    self._json_output(event)
                    return
                time.sleep(0.5)
                elapsed += 0.5

            # Final attempt (wait=0 will try once)
            event = store.claim_next_event(worker_id)
            if event:
                self._json_output(event)
            else:
                self._json_output({"event": None, "message": "No pending events"})
        except Exception as e:
            self._error(f"Error claiming event: {e}")

    def inbox_ack(self, event_id: str) -> None:
        """Acknowledge an event after processing."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            event = store.ack_event(event_id)
            self._json_output(event)
        except Exception as e:
            self._error(f"Error acking event: {e}")

    def inbox_fail(self, event_id: str, error: str = "Worker error") -> None:
        """Mark an event as failed."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            event = store.fail_event(event_id, error)
            self._json_output(event)
        except Exception as e:
            self._error(f"Error failing event: {e}")

    # =========================================================================
    # Project Commands
    # =========================================================================

    def project_summary(self) -> None:
        """Get a basic summary of the project: stage/status counts."""
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            artifacts = store.list_artifacts()

            # Count by stage and status
            by_stage = {}
            by_status = {}

            for art in artifacts:
                stage = art.get("stage", "unknown")
                status = art.get("status", "unknown")

                if stage not in by_stage:
                    by_stage[stage] = 0
                by_stage[stage] += 1

                if status not in by_status:
                    by_status[status] = 0
                by_status[status] += 1

            self._json_output({
                "total_artifacts": len(artifacts),
                "by_stage": by_stage,
                "by_status": by_status,
            })
        except Exception as e:
            self._error(f"Error getting project summary: {e}")

    # =========================================================================
    # Lifecycle Commands
    # =========================================================================

    def init(self, stage: str = "movie", db: Optional[str] = None) -> None:
        """Initialize a new project with a stage config."""
        if db:
            self.db_path = db

        db_file = Path(self.db_path)
        project_dir = db_file.parent

        if db_file.exists():
            self._error(f"Project already exists at {self.db_path}")
            return

        # Load stage config. Constrain the stage name to a safe identifier so
        # it cannot be used to escape the stages directory (SPEC section 37).
        if not re.fullmatch(r"[A-Za-z0-9_-]+", stage):
            self._error(f"Invalid stage name: {stage}")
            return

        stages_dir = (Path(__file__).parent / "stages").resolve()
        config_file = (stages_dir / f"{stage}.json").resolve()
        if stages_dir not in config_file.parents:
            self._error(f"Invalid stage name: {stage}")
            return

        if not config_file.exists():
            self._error(f"Stage config '{stage}' not found at {config_file}")
            return

        try:
            with open(config_file, "r") as f:
                config_data = json.load(f)

            # Validate config
            try:
                stage_config = StageConfig(config_data)
            except Exception as e:
                self._error(f"Invalid stage config: {e}")
                return

            # Create project directory and store
            project_dir.mkdir(parents=True, exist_ok=True)
            store = self.get_store(create=True)

            # Save project metadata
            if store:
                store.conn.execute(
                    "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
                    ("stage_config", json.dumps(config_data)),
                )
                store.conn.execute(
                    "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
                    ("project_id", json.dumps("default")),
                )
                store.conn.commit()

                self._json_output({
                    "ok": True,
                    "stage": stage,
                    "db_path": str(self.db_path),
                    "project_dir": str(project_dir),
                })
            else:
                self._error("Failed to create store")
        except Exception as e:
            self._error(f"Error initializing project: {e}")

    def status(self) -> None:
        """Report project status: whether DB exists and basic counts."""
        db_file = Path(self.db_path)
        exists = db_file.exists()

        result = {
            "db_exists": exists,
            "db_path": str(self.db_path),
        }

        if exists:
            try:
                store = self.get_store()
                if store:
                    artifacts = store.list_artifacts()
                    result["artifact_count"] = len(artifacts)

                    # Count by stage
                    by_stage = {}
                    for art in artifacts:
                        stage = art.get("stage", "unknown")
                        by_stage[stage] = by_stage.get(stage, 0) + 1
                    result["by_stage"] = by_stage
            except Exception as e:
                logger.warning(f"Error reading store: {e}")

        self._json_output(result)

    # =========================================================================
    # Runtime Directory (SPEC section 9)
    # =========================================================================

    def runtime_dir(self, runtime_dir: Optional[str] = None) -> Path:
        """Resolve the runtime directory (`.surface-board/run` by default).

        Per SPEC section 9 the runtime directory lives next to the store:
        `<project>/.surface-board/run/{board.pid, supervisor.pid, poller.pid,
        token, port}`.
        """
        if runtime_dir:
            return Path(runtime_dir)
        return Path(self.db_path).resolve().parent / "run"

    @staticmethod
    def _write_runtime_file(run_dir: Path, name: str, value: str,
                            secret: bool = False) -> Path:
        """Write a single runtime file with restrictive permissions."""
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass
        path = run_dir / name
        path.write_text(str(value))
        # SPEC section 9: runtime token files SHOULD use restrictive permissions.
        try:
            os.chmod(path, 0o600 if secret else 0o644)
        except OSError:
            pass
        return path

    @staticmethod
    def _clear_runtime_files(run_dir: Path) -> None:
        """Remove pid/token/port files on clean shutdown (SPEC section 45)."""
        for name in ("board.pid", "supervisor.pid", "poller.pid", "token", "port"):
            try:
                (run_dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as e:  # pragma: no cover - defensive
                logger.warning(f"Could not remove runtime file {name}: {e}")

    @staticmethod
    def _reserve_port(host: str, port: int = 0) -> int:
        """Resolve a concrete port to bind the board to.

        Binding to port 0 and immediately releasing lets us record the resolved
        port in `.surface-board/run/port` before the board actually binds it.
        """
        if port:
            return port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return sock.getsockname()[1]

    def _build_board_blocks(self, store: Store, stage_config: StageConfig) -> Any:
        """Call build_board with whatever signature it currently exposes."""
        from surface.board import build_board

        kwargs: Dict[str, Any] = {}
        params = inspect.signature(build_board).parameters
        if "media_dir" in params:
            kwargs["media_dir"] = str(Path(self.db_path).resolve().parent / "media")
        return build_board(store, stage_config, **kwargs)

    def _launch_board(self, stage_config: StageConfig, host: str, port: int,
                      token: str, run_dir: Path) -> bool:
        """Launch the Gradio board in a daemon thread.

        Returns True if a board was launched, False if Gradio is unavailable.

        The board gets its own Store (its own sqlite3 connection), because
        sqlite3 connections are not shareable across threads.
        """
        try:
            board_store = Store(self.db_path)
            blocks = self._build_board_blocks(board_store, stage_config)
        except ImportError:
            logger.warning("Board UI unavailable: gradio is not installed")
            return False
        except Exception as e:
            logger.warning(f"Board UI unavailable: {e}")
            return False

        if blocks is None:
            logger.warning(
                "Board UI unavailable (gradio not installed); "
                "running supervisor + poller only"
            )
            return False

        def _run() -> None:
            try:
                # SPEC section 36: bind loopback by default and require auth.
                blocks.launch(
                    server_name=host,
                    server_port=port,
                    auth=("surface", token),
                    share=False,
                    quiet=True,
                    show_error=True,
                    prevent_thread_lock=False,
                )
            except Exception as e:  # pragma: no cover - depends on gradio
                logger.error(f"Board failed to launch: {e}")

        thread = threading.Thread(target=_run, name="surface-board", daemon=True)
        thread.start()
        self._board_thread = thread
        # The board runs in this process, so board.pid is our pid.
        self._write_runtime_file(run_dir, "board.pid", str(os.getpid()))
        logger.info(f"Board listening on http://{host}:{port} (basic auth user 'surface')")
        return True

    def serve(self, db: Optional[str] = None, stage: str = "movie",
              run_once: bool = False,
              supervisor_iterations: Optional[int] = None,
              max_iterations: Optional[int] = None,
              runtime_dir: Optional[str] = None,
              host: str = "127.0.0.1",
              port: int = 0,
              board: bool = True,
              worker_command: Optional[List[str]] = None,
              transport: Optional[Any] = None,
              poll_interval: float = 0.5,
              keep_runtime_files: bool = False) -> None:
        """Start the board runtime: store, supervisor, poller, and board.

        Architecture (single process):
          * the Gradio board runs in a daemon thread with its own Store
            connection and HTTP basic auth bound to loopback;
          * the supervisor + poller cycle runs on the main thread;
          * the worker runs as a real subprocess spawned by
            NativeStreamTransport (`python -m surface.worker --db <path>`).

        Bounded runs (`--max-iterations` / `--run-once`) exist so tests and
        scripted use do not block; with no bound the loop runs until SIGINT.
        """
        if db:
            self.db_path = db

        db_file = Path(self.db_path)
        if not db_file.exists():
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        store = self.get_store()
        if not store:
            self._error("Could not open store")
            return

        # Resolve the iteration bound. `supervisor_iterations` is the legacy
        # flag name; `max_iterations` is the spelling used by the CLI now.
        bound = max_iterations if max_iterations is not None else supervisor_iterations
        if run_once:
            bound = 1

        run_dir = self.runtime_dir(runtime_dir)
        supervisor: Optional[Supervisor] = None
        board_launched = False
        iterations = 0

        try:
            cursor = store.conn.cursor()
            cursor.execute("SELECT value_json FROM kv_state WHERE key = ?", ("stage_config",))
            row = cursor.fetchone()
            if not row:
                self._error("No stage config found in store")
                return

            config_data = json.loads(row[0])
            try:
                stage_config = StageConfig(config_data)
            except Exception as e:
                self._error(f"Invalid stage config: {e}")
                return

            cursor.execute("SELECT value_json FROM kv_state WHERE key = ?", ("project_id",))
            project_row = cursor.fetchone()
            project_id = json.loads(project_row[0]) if project_row else "default"

            # -----------------------------------------------------------------
            # Runtime directory: token + port + pids (SPEC section 9)
            # -----------------------------------------------------------------
            # SPEC section 36: Phase 0 must prove one authenticated remote path.
            # The board is served over HTTP basic auth with an unpredictable
            # per-run token as the password; the token never appears in a URL.
            token = secrets.token_urlsafe(32)
            resolved_port = self._reserve_port(host, port)
            self._write_runtime_file(run_dir, "token", token, secret=True)
            self._write_runtime_file(run_dir, "port", str(resolved_port))
            self._write_runtime_file(run_dir, "supervisor.pid", str(os.getpid()))
            self._write_runtime_file(run_dir, "poller.pid", str(os.getpid()))
            self.runtime_info = {
                "runtime_dir": str(run_dir),
                "token": token,
                "port": resolved_port,
                "host": host,
            }

            # -----------------------------------------------------------------
            # Poller with the mock Phase 0 providers (names match movie.json)
            # -----------------------------------------------------------------
            providers = {
                "image_default": MockImageProvider(),
                "video_default": MockVideoProvider(),
            }
            poller = Poller(store, providers)

            # -----------------------------------------------------------------
            # Supervisor over a real worker subprocess
            # -----------------------------------------------------------------
            if transport is None:
                command = worker_command or [
                    sys.executable,
                    "-m",
                    "surface.worker",
                    "--db",
                    str(Path(self.db_path).resolve()),
                ]
                if worker_command is None and importlib.util.find_spec("surface.worker") is None:
                    logger.warning(
                        "surface.worker module not found; worker dispatch will fail "
                        "until it exists (expected: python -m surface.worker --db <path>)"
                    )
                transport = NativeStreamTransport(command=command)

            supervisor_config = SupervisorConfig(
                worker_id="cli_worker",
                lease_seconds=60,
                turn_timeout=300,
            )
            supervisor = Supervisor(
                store, transport, stage_config, supervisor_config,
                project_id=project_id,
            )

            # -----------------------------------------------------------------
            # Board (daemon thread, own store connection)
            # -----------------------------------------------------------------
            if board:
                board_launched = self._launch_board(
                    stage_config, host, resolved_port, token, run_dir
                )
            else:
                logger.info("Board disabled by --no-board")

            # -----------------------------------------------------------------
            # Supervisor + poller loop
            # -----------------------------------------------------------------
            stop_event = threading.Event()

            def _handle_sigint(signum, frame):  # pragma: no cover - signal path
                logger.info("Received interrupt; shutting down")
                stop_event.set()

            previous_handler = None
            installed_handler = False
            try:
                previous_handler = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, _handle_sigint)
                installed_handler = True
            except ValueError:
                # Not on the main thread; rely on KeyboardInterrupt instead.
                pass

            if bound is None:
                logger.info("Starting supervisor loop (until interrupted)...")
            else:
                logger.info(f"Starting supervisor loop ({bound} iteration(s))...")

            try:
                while not stop_event.is_set() and (bound is None or iterations < bound):
                    supervisor.claim_and_dispatch_event()
                    jobs_processed = poller.poll_once()
                    if jobs_processed:
                        logger.info(f"Poller processed {jobs_processed} job(s)")
                    iterations += 1
                    if bound is None:
                        stop_event.wait(poll_interval)
            except KeyboardInterrupt:  # pragma: no cover - signal path
                logger.info("Supervisor loop interrupted")
            finally:
                if installed_handler and previous_handler is not None:
                    try:
                        signal.signal(signal.SIGINT, previous_handler)
                    except ValueError:
                        pass

            logger.info("Supervisor loop completed")

            self._json_output({
                "ok": True,
                "message": "Board runtime completed",
                "stage": stage_config.id,
                "iterations": iterations,
                "board": "running" if board_launched else "unavailable",
                "host": host,
                "port": resolved_port,
                "runtime_dir": str(run_dir),
            })
        except Exception as e:
            logger.exception(f"Error in serve: {e}")
            self._error(f"Error starting board runtime: {e}")
        finally:
            # SPEC section 45: clean shutdown stops the worker, closes the
            # transport, and invalidates the runtime pid/token files.
            if supervisor is not None:
                try:
                    supervisor.stop_worker()
                except Exception as e:
                    logger.warning(f"Error stopping worker: {e}")
                for session in list(getattr(supervisor.transport, "sessions", {}).values()):
                    try:
                        supervisor.transport.close(session)
                    except Exception:
                        pass
            if not keep_runtime_files:
                self._clear_runtime_files(run_dir)

    # =========================================================================
    # Utilities
    # =========================================================================

    def _json_output(self, data: Any) -> None:
        """Output JSON to stdout."""
        print(json.dumps(data, indent=2), file=sys.stdout)

    def _error(self, message: str) -> None:
        """Output error message to stderr."""
        print(f"Error: {message}", file=sys.stderr)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="surface",
        description="Agent Surface Board: CLI for board runtime and store operations",
    )

    # Global options
    parser.add_argument(
        "--db",
        default=".surface-board/state.sqlite3",
        help="Path to SQLite store (default: .surface-board/state.sqlite3)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # =========================================================================
    # Store subcommands
    # =========================================================================
    store_parser = subparsers.add_parser("store", help="Store operations")
    store_subparsers = store_parser.add_subparsers(dest="store_command", required=True)

    # store get
    get_parser = store_subparsers.add_parser("get", help="Get an artifact")
    get_parser.add_argument("artifact_id", help="Artifact ID")

    # store list
    list_parser = store_subparsers.add_parser("list", help="List artifacts")
    list_parser.add_argument("--stage", help="Filter by stage")
    list_parser.add_argument("--status", help="Filter by status")

    # store put-version
    put_version_parser = store_subparsers.add_parser(
        "put-version", help="Create a new artifact version"
    )
    put_version_parser.add_argument(
        "--artifact-id", dest="artifact_id", help="Artifact ID (or from JSON)"
    )
    put_version_parser.add_argument(
        "--file", help="Read version JSON from file (default: stdin)"
    )

    # store select-version
    select_version_parser = store_subparsers.add_parser(
        "select-version", help="Select a version for an artifact"
    )
    select_version_parser.add_argument("artifact_id", help="Artifact ID")
    select_version_parser.add_argument("version_id", help="Version ID to select")
    select_version_parser.add_argument(
        "--expected-selected-version-id",
        dest="expected_selected_version_id",
        help="Check concurrency: fail if current selection differs",
    )

    # store set-status
    set_status_parser = store_subparsers.add_parser(
        "set-status", help="Set artifact status"
    )
    set_status_parser.add_argument("artifact_id", help="Artifact ID")
    set_status_parser.add_argument("status", help="New status")

    # store add-dependency
    add_dependency_parser = store_subparsers.add_parser(
        "add-dependency", help="Add a dependency between artifacts"
    )
    add_dependency_parser.add_argument("upstream_artifact_id", help="Upstream artifact ID")
    add_dependency_parser.add_argument("downstream_artifact_id", help="Downstream artifact ID")
    add_dependency_parser.add_argument(
        "--kind", default="content", help="Dependency kind (default: content)"
    )

    # store graph
    graph_parser = store_subparsers.add_parser(
        "graph", help="Get dependency graph for an artifact"
    )
    graph_parser.add_argument("artifact_id", help="Artifact ID")

    # store job-start
    job_start_parser = store_subparsers.add_parser("job-start", help="Start a job")
    job_start_parser.add_argument("artifact_id", help="Artifact ID")
    job_start_parser.add_argument("provider", help="Provider name")
    job_start_parser.add_argument("kind", help="Job kind (e.g., 'image', 'video')")
    job_start_parser.add_argument(
        "--file", help="Read job request JSON from file (default: stdin)"
    )

    # store job-finish
    job_finish_parser = store_subparsers.add_parser("job-finish", help="Finish a job")
    job_finish_parser.add_argument("job_id", help="Job ID")
    job_finish_parser.add_argument(
        "--file", help="Read result JSON from file (default: stdin)"
    )

    # store job-cancel
    job_cancel_parser = store_subparsers.add_parser("job-cancel", help="Cancel a job")
    job_cancel_parser.add_argument("job_id", help="Job ID")

    # =========================================================================
    # Inbox subcommands
    # =========================================================================
    inbox_parser = subparsers.add_parser("inbox", help="Inbox/event operations")
    inbox_subparsers = inbox_parser.add_subparsers(dest="inbox_command", required=True)

    # inbox next
    next_parser = inbox_subparsers.add_parser("next", help="Claim next event")
    next_parser.add_argument(
        "--wait",
        type=int,
        default=0,
        help="Poll for up to N seconds (default: 0 = non-blocking)",
    )
    next_parser.add_argument(
        "--worker-id", default="cli_worker", help="Worker ID for claim"
    )

    # inbox ack
    ack_parser = inbox_subparsers.add_parser("ack", help="Acknowledge an event")
    ack_parser.add_argument("event_id", help="Event ID")

    # inbox fail
    fail_parser = inbox_subparsers.add_parser("fail", help="Fail an event")
    fail_parser.add_argument("event_id", help="Event ID")
    fail_parser.add_argument(
        "--error", default="Worker error", help="Error message"
    )

    # =========================================================================
    # Project subcommands
    # =========================================================================
    project_parser = subparsers.add_parser("project", help="Project operations")
    project_subparsers = project_parser.add_subparsers(dest="project_command", required=True)

    # project summary
    summary_parser = project_subparsers.add_parser("summary", help="Get project summary")

    # =========================================================================
    # Lifecycle commands
    # =========================================================================
    init_parser = subparsers.add_parser("init", help="Initialize a new project")
    init_parser.add_argument(
        "--stage", default="movie", help="Stage config to use (default: movie)"
    )
    # NOTE: --db is a global option (see `parser.add_argument("--db", ...)`
    # above); it must not be redeclared per-subcommand with a different
    # default (e.g. None) or it clobbers the global default in args.db.

    status_parser = subparsers.add_parser("status", help="Get project status")

    serve_parser = subparsers.add_parser(
        "serve", help="Start the board runtime (store, supervisor, poller, board)"
    )
    serve_parser.add_argument(
        "--stage", default="movie", help="Stage config (default: movie)"
    )
    serve_parser.add_argument(
        "--run-once", action="store_true", help="Run supervisor once then exit"
    )
    serve_parser.add_argument(
        "--supervisor-iterations",
        type=int,
        default=None,
        help="Legacy alias for --max-iterations",
    )
    serve_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Bound the supervisor/poller loop to N iterations "
             "(default: unbounded, runs until Ctrl-C)",
    )
    serve_parser.add_argument(
        "--runtime-dir",
        default=None,
        help="Override the runtime directory (default: <db dir>/run)",
    )
    serve_parser.add_argument(
        "--host", default="127.0.0.1",
        help="Board bind address (default: 127.0.0.1, loopback per SPEC s36)",
    )
    serve_parser.add_argument(
        "--port", type=int, default=0,
        help="Board port (default: 0 = pick a free port)",
    )
    serve_parser.add_argument(
        "--no-board", dest="board", action="store_false", default=True,
        help="Run supervisor + poller only; do not launch the board UI",
    )
    serve_parser.add_argument(
        "--poll-interval", type=float, default=0.5,
        help="Seconds to sleep between unbounded loop iterations (default: 0.5)",
    )
    serve_parser.add_argument(
        "--keep-runtime-files", action="store_true",
        help="Do not delete run/{token,port,*.pid} on shutdown",
    )

    # Parse arguments
    args = parser.parse_args()

    # Create CLI instance
    cli = SurfaceCLI(db_path=args.db)

    # Dispatch to commands
    if args.command == "store":
        if args.store_command == "get":
            cli.store_get(args.artifact_id)
        elif args.store_command == "list":
            cli.store_list(stage=args.stage, status=args.status)
        elif args.store_command == "put-version":
            cli.store_put_version(args.artifact_id or "", file=args.file)
        elif args.store_command == "select-version":
            cli.store_select_version(
                args.artifact_id,
                args.version_id,
                expected_selected_version_id=args.expected_selected_version_id,
            )
        elif args.store_command == "set-status":
            cli.store_set_status(args.artifact_id, args.status)
        elif args.store_command == "add-dependency":
            cli.store_add_dependency(
                args.upstream_artifact_id,
                args.downstream_artifact_id,
                kind=args.kind,
            )
        elif args.store_command == "graph":
            cli.store_graph(args.artifact_id)
        elif args.store_command == "job-start":
            cli.store_job_start(args.artifact_id, args.provider, args.kind, file=args.file)
        elif args.store_command == "job-finish":
            cli.store_job_finish(args.job_id, file=args.file)
        elif args.store_command == "job-cancel":
            cli.store_job_cancel(args.job_id)

    elif args.command == "inbox":
        if args.inbox_command == "next":
            cli.inbox_next(wait=args.wait, worker_id=args.worker_id)
        elif args.inbox_command == "ack":
            cli.inbox_ack(args.event_id)
        elif args.inbox_command == "fail":
            cli.inbox_fail(args.event_id, error=args.error)

    elif args.command == "project":
        if args.project_command == "summary":
            cli.project_summary()

    elif args.command == "init":
        cli.init(stage=args.stage, db=args.db)

    elif args.command == "status":
        cli.status()

    elif args.command == "serve":
        cli.serve(
            db=args.db,
            stage=args.stage,
            run_once=args.run_once,
            supervisor_iterations=args.supervisor_iterations,
            max_iterations=args.max_iterations,
            runtime_dir=args.runtime_dir,
            host=args.host,
            port=args.port,
            board=args.board,
            poll_interval=args.poll_interval,
            keep_runtime_files=args.keep_runtime_files,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
