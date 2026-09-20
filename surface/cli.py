"""
Command-line interface for Agent Surface Board.

Exposes store tools, inbox operations, project management, and board runtime
via argparse. JSON-only results go to stdout; diagnostics to stderr.

Per SPEC section 17 (Store/Inbox Tools CLI) and section 9 (Runtime Directory).
"""

import argparse
import json
import logging
import sys
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

        # Load stage config
        stages_dir = Path(__file__).parent / "stages"
        config_file = stages_dir / f"{stage}.json"

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

    def serve(self, db: Optional[str] = None, stage: str = "movie",
              run_once: bool = False, supervisor_iterations: int = 1) -> None:
        """Start the board runtime: store, supervisor, poller, and board.

        For Phase 0 testing, this can run a bounded number of iterations
        instead of an infinite loop. Use run_once=True or set
        supervisor_iterations to control loop exit.
        """
        if db:
            self.db_path = db

        db_file = Path(self.db_path)
        if not db_file.exists():
            self._error(f"No store at {self.db_path}. Use 'surface init' first.")
            return

        # Load store
        store = self.get_store()
        if not store:
            self._error("Could not open store")
            return

        # Load stage config
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

            # Set up providers
            providers = {
                "image_default": MockImageProvider(),
                "video_default": MockVideoProvider(),
            }

            # Create poller
            poller = Poller(store, providers)

            # Create supervisor with a native stream transport
            transport = NativeStreamTransport()
            supervisor_config = SupervisorConfig(
                worker_id="cli_worker",
                lease_seconds=60,
                turn_timeout=300,
            )
            supervisor = Supervisor(store, transport, stage_config, supervisor_config)

            # Try to load Gradio board
            try:
                from surface.board import build_board
                board = build_board(store, stage_config)
                if board:
                    logger.info("Board UI loaded (Gradio available)")
                else:
                    logger.info("Board UI not available (Gradio not installed)")
            except (ImportError, Exception):
                logger.info("Gradio not installed; board UI unavailable")

            # Main loop
            logger.info(f"Starting supervisor loop (will run {supervisor_iterations} iteration(s))...")
            for i in range(supervisor_iterations):
                logger.info(f"Supervisor iteration {i+1}/{supervisor_iterations}")

                # Try to start worker
                supervisor.start_worker()

                # Claim and process one event
                event = store.claim_next_event("cli_worker")
                if event:
                    logger.info(f"Processing event: {event['id']}")
                    # In Phase 0, we just ack it after reading
                    store.ack_event(event["id"])
                else:
                    logger.info("No pending events")

                # Poll for job completions
                jobs_processed = poller.poll_once()
                if jobs_processed > 0:
                    logger.info(f"Poller processed {jobs_processed} job(s)")

            logger.info("Supervisor loop completed")

            self._json_output({
                "ok": True,
                "message": "Board runtime completed",
                "stage": stage_config.id,
            })
        except Exception as e:
            logger.exception(f"Error in serve: {e}")
            self._error(f"Error starting board runtime: {e}")

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
    init_parser.add_argument(
        "--db", help="Override DB path"
    )

    status_parser = subparsers.add_parser("status", help="Get project status")

    serve_parser = subparsers.add_parser(
        "serve", help="Start the board runtime (store, supervisor, poller, board)"
    )
    serve_parser.add_argument(
        "--db", help="Override DB path"
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
        default=1,
        help="Number of supervisor loop iterations (default: 1)",
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
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
