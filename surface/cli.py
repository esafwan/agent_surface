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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from surface.store import Store
from surface.stages.config import StageConfig, load_preset, load_stage_config
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

    def tui(self) -> None:
        """Launch the interactive stdlib-only terminal board (SPEC section 58).

        Unlike the Gradio board, this renderer has no external dependency and
        runs directly against the real terminal (sys.stdin/sys.stdout) --
        it is a full alternative renderer, not a JSON-output subcommand, so
        it does not follow the JSON-to-stdout convention the rest of the CLI
        uses.
        """
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        cursor = store.conn.cursor()
        cursor.execute("SELECT value_json FROM kv_state WHERE key = ?", ("stage_config",))
        row = cursor.fetchone()
        if not row:
            self._error("No stage config found in store")
            return

        try:
            stage_config = StageConfig(json.loads(row[0]))
        except Exception as e:
            self._error(f"Invalid stage config: {e}")
            return

        from surface.tui import run_tui
        run_tui(store, stage_config)

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
              keep_runtime_files: bool = False,
              pool_size: int = 1,
              recycle_after_events: Optional[int] = None,
              recycle_after_tokens: Optional[int] = None) -> None:
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
                recycle_after_events=recycle_after_events,
                recycle_after_tokens=recycle_after_tokens,
            )
            supervisor = Supervisor(
                store, transport, stage_config, supervisor_config,
                project_id=project_id,
                pool_size=pool_size,
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

            interrupt_flag = run_dir / "interrupt_requested"

            try:
                while not stop_event.is_set() and (bound is None or iterations < bound):
                    if interrupt_flag.exists():
                        # `surface interrupt` (a separate process) wrote this
                        # flag; consume it and forward to the live
                        # Supervisor in THIS process before removing it, so
                        # a second `interrupt` call is needed for a second
                        # interruption rather than one flag firing forever.
                        try:
                            result = supervisor.request_worker_interrupt()
                            logger.info(f"Processed interrupt request: {result}")
                        except Exception as e:
                            logger.warning(f"Error handling interrupt request: {e}")
                        finally:
                            interrupt_flag.unlink(missing_ok=True)
                    if pool_size > 1:
                        # run_pool_once() claims/dispatches up to pool_size
                        # events concurrently (one per worker session,
                        # same-artifact events still serialized via the
                        # store's artifact leases) instead of one at a time.
                        supervisor.run_pool_once()
                    else:
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
                    supervisor.stop_all_workers()
                except Exception as e:
                    logger.warning(f"Error stopping worker(s): {e}")
                for session in list(getattr(supervisor.transport, "sessions", {}).values()):
                    try:
                        supervisor.transport.close(session)
                    except Exception:
                        pass
            if not keep_runtime_files:
                self._clear_runtime_files(run_dir)

    # =========================================================================
    # Recovery CLI (SPEC section 45, 40)
    # =========================================================================

    def stop(self, runtime_dir: Optional[str] = None) -> None:
        """Stop a running `surface serve` process and clear runtime state.

        Per SPEC section 45: stop MUST NOT delete the SQLite db or any
        artifacts/versions -- only the runtime/process state under
        `.surface-board/run/` (pid files, token, port). If a pid file
        references a live process, it is sent SIGTERM and given a brief
        grace period to exit before the runtime files are cleared
        regardless of whether the process actually stopped in time.

        This process never signals its own pid (a `serve()` invoked
        in-process, e.g. under test, records its own pid in board.pid /
        supervisor.pid / poller.pid; signalling ourselves would be
        nonsensical and dangerous).
        """
        run_dir = self.runtime_dir(runtime_dir)
        run_dir_existed = run_dir.exists()

        pids_found: Dict[str, int] = {}
        for name in ("board.pid", "supervisor.pid", "poller.pid"):
            path = run_dir / name
            if not path.exists():
                continue
            try:
                pids_found[name] = int(path.read_text().strip())
            except (ValueError, OSError):
                continue

        stopped_pids: List[int] = []
        skipped_self = False
        for pid in sorted(set(pids_found.values())):
            if pid == os.getpid():
                skipped_self = True
                continue
            try:
                os.kill(pid, 0)
            except OSError:
                continue  # not running; nothing to signal

            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue

            # Brief grace period for a clean shutdown (bounded, not a real wait).
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            stopped_pids.append(pid)

        self._clear_runtime_files(run_dir)

        self._json_output({
            "ok": True,
            "runtime_dir": str(run_dir),
            "runtime_dir_existed": run_dir_existed,
            "pids_found": pids_found,
            "stopped_pids": stopped_pids,
            "skipped_self": skipped_self,
            "runtime_files_cleared": True,
        })

    def interrupt(self, runtime_dir: Optional[str] = None) -> None:
        """Request interruption of whatever the running `surface serve`
        worker is currently doing (SPEC section 22/56: robust cancellation
        of an in-flight worker turn, distinct from job cancellation).

        This process and the running `serve()` process are separate OS
        processes with no shared memory, so this cannot call
        Supervisor.request_worker_interrupt() directly. Instead it writes a
        flag file into the runtime dir; `serve()`'s loop polls for that flag
        once per iteration and, if present, calls
        `request_worker_interrupt()` on its live Supervisor before removing
        the flag. This command only confirms the flag was written -- it
        cannot confirm a `serve()` process actually observed it (there is no
        running-process guarantee here, only a best-effort signal, same
        limitation `stop`/`recover` have via pid files).
        """
        run_dir = self.runtime_dir(runtime_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        flag_path = run_dir / "interrupt_requested"
        flag_path.write_text(datetime.now(timezone.utc).isoformat())

        board_pid_path = run_dir / "supervisor.pid"
        likely_running = False
        if board_pid_path.exists():
            try:
                pid = int(board_pid_path.read_text().strip())
                os.kill(pid, 0)
                likely_running = True
            except (ValueError, OSError):
                likely_running = False

        self._json_output({
            "ok": True,
            "runtime_dir": str(run_dir),
            "flag_path": str(flag_path),
            "likely_running": likely_running,
        })

    def recover(self, force_reclaim: bool = False, stale_seconds: int = 300) -> None:
        """Diagnose (and optionally repair) stuck events/jobs (SPEC section 40).

        Reports, without mutating anything by default:
          * "processing" events whose lease has already expired -- these
            would be silently reclaimed to `pending` on the very next
            `claim_next_event` call (that reset happens unconditionally as
            step 1 of `claim_next_event`, before it looks for new work).
          * jobs stuck in `queued`/`running` whose `updated_at` is older
            than `stale_seconds` (default 300s / 5 minutes) -- i.e. no
            poller/provider activity has touched them recently.

        `--force-reclaim` makes this actively repair what it found:
          * for each event with an expired lease, it calls
            `store.reclaim_expired_leases()`, which resets those events to
            `pending` and releases their artifact locks WITHOUT also
            claiming/leasing an unrelated healthy pending event as a side
            effect (an earlier version of this command used
            `claim_next_event()` for this, which could strand a perfectly
            healthy event under a `surface-recover` worker id that would
            never actually process it).
          * for each stuck job, it marks the job `failed` with a
            descriptive error via the store's normal `update_job_status`,
            so it stops occupying `queued`/`running` and can be resubmitted.
        """
        store = self.get_store()
        if not store:
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        try:
            now_iso = datetime.now(timezone.utc).isoformat()

            cursor = store.conn.cursor()
            cursor.execute(
                """
                SELECT id, artifact_id, claimed_by, claimed_at, lease_until
                FROM events
                WHERE status = 'processing' AND lease_until IS NOT NULL AND lease_until < ?
                ORDER BY lease_until ASC
                """,
                (now_iso,),
            )
            expired_events = [dict(row) for row in cursor.fetchall()]

            cursor.execute(
                "SELECT id, artifact_id, provider, kind, status, updated_at FROM jobs "
                "WHERE status IN ('queued', 'running')"
            )
            stuck_jobs = []
            for row in cursor.fetchall():
                job = dict(row)
                try:
                    updated_dt = datetime.fromisoformat(job["updated_at"])
                except (TypeError, ValueError):
                    continue
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated_dt).total_seconds()
                if age >= stale_seconds:
                    job["age_seconds"] = age
                    stuck_jobs.append(job)

            actions: List[Dict[str, Any]] = []
            if force_reclaim:
                # reclaim_expired_leases() only resets expired-lease events
                # to pending and releases their artifact locks — unlike
                # claim_next_event(), it never also claims/leases a healthy,
                # unrelated pending event as a side effect (a prior version
                # of this command used claim_next_event() for this cleanup
                # side effect, which could strand a perfectly healthy event
                # in 'processing' under a 'surface-recover' worker id that
                # would never actually run it).
                reclaimed_ids = store.reclaim_expired_leases()
                for evt_id in reclaimed_ids:
                    actions.append({"type": "event_reset_to_pending", "id": evt_id})

                for job in stuck_jobs:
                    store.update_job_status(
                        job["id"],
                        status="failed",
                        result={
                            "error": "reclaimed by 'surface recover --force-reclaim' "
                                     f"(no update for >= {stale_seconds}s)"
                        },
                    )
                    actions.append({"type": "job_failed", "id": job["id"]})

            self._json_output({
                "ok": True,
                "expired_lease_events": expired_events,
                "stuck_jobs": stuck_jobs,
                "stale_seconds": stale_seconds,
                "force_reclaim": force_reclaim,
                "actions": actions,
            })
        except Exception as e:
            self._error(f"Error recovering: {e}")

    # =========================================================================
    # Convenience Mode: render + bounded wait (SPEC section 35)
    # =========================================================================
    #
    # Design note: there is no dedicated "interaction" table in schema.py /
    # store.py, and this task explicitly must not add one. Rather than bend
    # the artifact/version/event model to mean something it doesn't
    # ("pending answer" has no honest representation there without inventing
    # new status/event semantics), interactions are tracked in a minimal
    # local JSON file store: `.surface-board/interactions/<handle>.json`.
    #
    # Mapping:
    #   handle              == the interaction id (`interaction_<hex8>`),
    #                          and also the filename stem.
    #   render()            writes {"handle", "stage", "content", "status":
    #                       "pending", "value": None, "created_at"}.
    #   "answered"          means some external process (a human via the
    #                       board, a test, a future `surface answer` command)
    #                       has overwritten the file with "status": "answered"
    #                       and a "value" payload. This CLI change does not
    #                       add a way to *produce* an answer through the
    #                       board/worker -- that is a known limitation, see
    #                       the report to the integrator.
    #   wait()              polls that file, bounded by --max seconds.

    def _interactions_dir(self) -> Path:
        return Path(self.db_path).resolve().parent / "interactions"

    def render(self, stage_config_path_or_preset: str, data: str) -> None:
        """Render a stage config against `--data` and create a durable handle."""
        try:
            if Path(stage_config_path_or_preset).exists():
                stage_config = load_stage_config(stage_config_path_or_preset)
            else:
                stage_config = load_preset(stage_config_path_or_preset)
        except FileNotFoundError as e:
            self._error(f"Stage config not found: {e}")
            return
        except Exception as e:
            self._error(f"Invalid stage config: {e}")
            return

        data_path = Path(data)
        if not data_path.exists():
            self._error(f"Data file not found: {data}")
            return
        try:
            with open(data_path, "r") as f:
                content = json.load(f)
        except json.JSONDecodeError as e:
            self._error(f"Invalid JSON in data file '{data}': {e}")
            return

        handle = f"interaction_{uuid.uuid4().hex[:8]}"
        interactions_dir = self._interactions_dir()
        interactions_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "handle": handle,
            "stage": stage_config.id,
            "content": content,
            "status": "pending",
            "value": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (interactions_dir / f"{handle}.json").write_text(json.dumps(record, indent=2))

        self._json_output({
            "status": "pending",
            "handle": handle,
            "stage": stage_config.id,
        })

    def wait(self, handle: str, max_seconds: float) -> None:
        """Poll an interaction handle, bounded by `max_seconds`.

        Every wait is bounded (SPEC section 35): this loop always returns
        within `max_seconds` (plus one poll-interval's slack), never blocks
        indefinitely.
        """
        if max_seconds is None or max_seconds < 0:
            self._error("--max must be a non-negative number of seconds")
            return

        path = self._interactions_dir() / f"{handle}.json"
        if not path.exists():
            self._error(f"Unknown interaction handle: {handle}")
            return

        poll_interval = 0.5
        start = time.monotonic()
        while True:
            try:
                record = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                self._error(f"Could not read interaction '{handle}': {e}")
                return

            if record.get("status") == "answered":
                self._json_output({"status": "answered", "value": record.get("value")})
                return

            elapsed = time.monotonic() - start
            if elapsed >= max_seconds:
                self._json_output({"status": "pending", "handle": handle})
                return

            time.sleep(min(poll_interval, max_seconds - elapsed))

    def answer(self, handle: str, value: str) -> None:
        """Answer a `render`ed interaction, closing the round-trip with `wait`.

        Without this command, nothing in the system ever writes
        `status: "answered"` into an interaction file -- `render` creates a
        handle and `wait` polls it, but there was no path for a human/board
        to actually answer it, so `wait` could only ever return "pending" in
        real usage. This is the missing write side.

        `value` is a JSON string (e.g. '{"choice":"A"}') matching the
        `value` field `wait` returns on success.
        """
        path = self._interactions_dir() / f"{handle}.json"
        if not path.exists():
            self._error(f"Unknown interaction handle: {handle}")
            return

        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self._error(f"Could not read interaction '{handle}': {e}")
            return

        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError as e:
            self._error(f"--value must be valid JSON: {e}")
            return

        record["status"] = "answered"
        record["value"] = parsed_value
        record["answered_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(record, indent=2))

        self._json_output({"ok": True, "handle": handle, "status": "answered"})

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

    tui_parser = subparsers.add_parser(
        "tui", help="Launch the interactive stdlib-only terminal board (no gradio required)"
    )

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
    serve_parser.add_argument(
        "--pool-size", type=int, default=1,
        help="Concurrent worker sessions for unrelated artifacts "
             "(SPEC section 25; default: 1 = serial, matches Phase 0 behavior)",
    )
    serve_parser.add_argument(
        "--recycle-after-events", type=int, default=None,
        help="Recycle the worker session after this many dispatched events",
    )
    serve_parser.add_argument(
        "--recycle-after-tokens", type=int, default=None,
        help="Recycle the worker session after this many reported tokens "
             "(inert unless a worker reports token usage)",
    )

    # =========================================================================
    # Recovery CLI (SPEC section 45, 40)
    # =========================================================================
    stop_parser = subparsers.add_parser(
        "stop", help="Stop a running board (signals pids, clears runtime state only)"
    )
    stop_parser.add_argument(
        "--runtime-dir", default=None,
        help="Override the runtime directory (default: <db dir>/run)",
    )

    interrupt_parser = subparsers.add_parser(
        "interrupt",
        help="Request interruption of the running serve() worker's current turn",
    )
    interrupt_parser.add_argument(
        "--runtime-dir", default=None,
        help="Override the runtime directory (default: <db dir>/run)",
    )

    recover_parser = subparsers.add_parser(
        "recover", help="Diagnose (and optionally repair) stuck events/jobs"
    )
    recover_parser.add_argument(
        "--force-reclaim", action="store_true",
        help="Actively repair: reset expired-lease events and fail stuck jobs "
             "(see 'surface recover --help' output / docs for exact semantics)",
    )
    recover_parser.add_argument(
        "--stale-seconds", type=int, default=300,
        help="Jobs queued/running with no update for this many seconds are "
             "considered stuck (default: 300)",
    )

    # =========================================================================
    # Convenience Mode (SPEC section 35)
    # =========================================================================
    render_parser = subparsers.add_parser(
        "render", help="Render a stage config + data into a durable interaction handle"
    )
    render_parser.add_argument(
        "stage_config", help="Preset name (e.g. 'questionnaire') or path to a stage config JSON file"
    )
    render_parser.add_argument(
        "--data", required=True, help="Path to JSON data/content to render"
    )

    wait_parser = subparsers.add_parser(
        "wait", help="Poll an interaction handle for an answer, bounded by --max seconds"
    )
    wait_parser.add_argument("handle", help="Interaction handle returned by 'surface render'")
    wait_parser.add_argument(
        "--max", type=float, required=True, dest="max_seconds",
        help="Maximum seconds to poll before returning 'pending' (required; every wait is bounded)",
    )

    answer_parser = subparsers.add_parser(
        "answer", help="Answer a rendered interaction (closes the render/wait round-trip)"
    )
    answer_parser.add_argument("handle", help="Interaction handle returned by 'surface render'")
    answer_parser.add_argument(
        "--value", required=True,
        help="JSON value to answer with, e.g. '{\"choice\":\"A\"}'",
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

    elif args.command == "tui":
        cli.tui()

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
            pool_size=args.pool_size,
            recycle_after_events=args.recycle_after_events,
            recycle_after_tokens=args.recycle_after_tokens,
        )

    elif args.command == "stop":
        cli.stop(runtime_dir=args.runtime_dir)

    elif args.command == "interrupt":
        cli.interrupt(runtime_dir=args.runtime_dir)

    elif args.command == "recover":
        cli.recover(force_reclaim=args.force_reclaim, stale_seconds=args.stale_seconds)

    elif args.command == "render":
        cli.render(args.stage_config, data=args.data)

    elif args.command == "wait":
        cli.wait(args.handle, max_seconds=args.max_seconds)

    elif args.command == "answer":
        cli.answer(args.handle, value=args.value)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
