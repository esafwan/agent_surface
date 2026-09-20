"""
Tests for Agent Surface Board CLI module.

Tests use temporary SQLite databases and direct function calls
(not subprocess) for speed and error visibility.
"""

import json
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from io import StringIO
import sys
import pytest

from surface.cli import SurfaceCLI
from surface.store import Store
from surface.stages.config import StageConfig


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sqlite3", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    wal_path.unlink(missing_ok=True)
    shm_path.unlink(missing_ok=True)


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create a temporary project directory."""
    return tmp_path / ".surface-board"


@pytest.fixture
def cli_with_temp_db(temp_db):
    """Create a CLI instance with a temporary database."""
    cli = SurfaceCLI(db_path=temp_db)
    return cli


class TestStoreCommands:
    """Tests for store subcommands."""

    def test_store_get_nonexistent_artifact(self, cli_with_temp_db, capsys):
        """Test getting a nonexistent artifact."""
        cli = cli_with_temp_db
        # Initialize store first
        store = cli.get_store(create=True)

        cli.store_get("nonexistent")
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_store_get_existing_artifact(self, cli_with_temp_db, capsys):
        """Test getting an existing artifact."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        # Create an artifact
        store.create_artifact(
            id="art_1",
            stage="script",
            title="Test Script",
            status="draft",
            meta={"author": "Alice"}
        )

        cli.store_get("art_1")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["id"] == "art_1"
        assert output["stage"] == "script"
        assert output["title"] == "Test Script"
        assert output["meta"]["author"] == "Alice"

    def test_store_list_empty(self, cli_with_temp_db, capsys):
        """Test listing artifacts when none exist."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        cli.store_list()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 0
        assert output["artifacts"] == []

    def test_store_list_with_artifacts(self, cli_with_temp_db, capsys):
        """Test listing artifacts."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script 1")
        store.create_artifact(id="art_2", stage="shots", title="Shots 1", status="review")

        cli.store_list()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 2
        assert len(output["artifacts"]) == 2

    def test_store_list_filter_by_stage(self, cli_with_temp_db, capsys):
        """Test listing artifacts filtered by stage."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")
        store.create_artifact(id="art_2", stage="shots", title="Shots")

        cli.store_list(stage="script")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 1
        assert output["artifacts"][0]["id"] == "art_1"

    def test_store_list_filter_by_status(self, cli_with_temp_db, capsys):
        """Test listing artifacts filtered by status."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script", status="draft")
        store.create_artifact(id="art_2", stage="shots", title="Shots", status="review")

        cli.store_list(status="review")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["count"] == 1
        assert output["artifacts"][0]["status"] == "review"

    def test_store_put_version_from_stdin(self, cli_with_temp_db, monkeypatch, capsys):
        """Test creating a version from stdin."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")

        # Mock stdin
        version_json = {
            "artifact_id": "art_1",
            "content": "Scene 1: INT. OFFICE",
            "content_type": "text/plain",
            "created_by": "worker",
            "note": "First draft",
        }
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(version_json)))

        cli.store_put_version("")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        assert output["version"] == 1
        assert "version_id" in output

    def test_store_put_version_from_file(self, cli_with_temp_db, tmp_path, capsys):
        """Test creating a version from a file."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")

        # Create a temporary version file
        version_file = tmp_path / "version.json"
        version_json = {
            "artifact_id": "art_1",
            "content": "Scene 1: INT. OFFICE",
            "content_type": "text/plain",
        }
        version_file.write_text(json.dumps(version_json))

        cli.store_put_version("", file=str(version_file))
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        assert output["version"] == 1

    def test_store_select_version(self, cli_with_temp_db, capsys):
        """Test selecting a version."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")
        v1 = store.put_version("art_1", content="Version 1", select=True)
        v2 = store.put_version("art_1", content="Version 2", select=False)

        cli.store_select_version("art_1", v2["version_id"])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        assert output["selected_version_id"] == v2["version_id"]

    def test_store_set_status(self, cli_with_temp_db, capsys):
        """Test setting artifact status."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script", status="draft")

        cli.store_set_status("art_1", "review")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "review"

    def test_store_add_dependency(self, cli_with_temp_db, capsys):
        """Test adding a dependency."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")
        store.create_artifact(id="art_2", stage="shots", title="Shots")

        cli.store_add_dependency("art_1", "art_2")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        assert output["upstream_artifact_id"] == "art_1"
        assert output["downstream_artifact_id"] == "art_2"

    def test_store_graph(self, cli_with_temp_db, capsys):
        """Test getting dependency graph."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")
        store.create_artifact(id="art_2", stage="shots", title="Shots")
        store.create_artifact(id="art_3", stage="keyframes", title="Keyframes")

        store.add_dependency("art_1", "art_2")
        store.add_dependency("art_2", "art_3")

        cli.store_graph("art_2")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["artifact_id"] == "art_2"
        assert len(output["upstreams"]) == 1
        assert len(output["downstreams"]) == 1


class TestJobCommands:
    """Tests for job-related subcommands."""

    def test_store_job_start(self, cli_with_temp_db, monkeypatch, capsys):
        """Test creating and starting a job."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="keyframes", title="Keyframes")

        job_json = {
            "prompt": "A beautiful sunset",
            "model": "test_model",
        }
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(job_json)))

        cli.store_job_start("art_1", "image_default", "image")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["id"].startswith("job_")
        assert output["artifact_id"] == "art_1"
        assert output["status"] == "queued"

    def test_store_job_finish(self, cli_with_temp_db, monkeypatch, capsys):
        """Test finishing a job."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="keyframes", title="Keyframes")
        job = store.create_job("art_1", "image_default", "image")

        # Simulate finishing with result
        result_json = {
            "content_ref": "media/art_1_v1.png",
            "content_type": "image/png",
        }
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(result_json)))

        cli.store_job_finish(job["id"])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "succeeded"

    def test_store_job_cancel(self, cli_with_temp_db, capsys):
        """Test cancelling a job."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="keyframes", title="Keyframes")
        job = store.create_job("art_1", "image_default", "image")

        cli.store_job_cancel(job["id"])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        # cancel_job flags cancel_requested; a poller cycle is what actually
        # transitions status to "cancelled" after telling the provider.
        assert output["cancel_requested"] is True
        assert output["status"] in ("queued", "running")


class TestInboxCommands:
    """Tests for inbox/event subcommands."""

    def test_inbox_next_no_events(self, cli_with_temp_db, capsys):
        """Test claiming next event when none exist."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        cli.inbox_next(wait=0)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["event"] is None

    def test_inbox_next_with_event(self, cli_with_temp_db, capsys):
        """Test claiming the next pending event."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        # Create artifact first (foreign key constraint)
        store.create_artifact(id="art_1", stage="script", title="Script")

        # Create an event
        event = store.enqueue_event(
            type="revise",
            payload={"note": "Make it brighter"},
            artifact_id="art_1",
        )

        cli.inbox_next(wait=0)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["id"] == event["id"]
        assert output["status"] == "processing"
        assert output["claimed_by"] == "cli_worker"

    def test_inbox_ack(self, cli_with_temp_db, capsys):
        """Test acknowledging an event."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")

        event = store.enqueue_event(
            type="revise",
            payload={"note": "Make it brighter"},
            artifact_id="art_1",
        )
        claimed = store.claim_next_event("cli_worker")

        cli.inbox_ack(claimed["id"])
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "acked"
        assert output["acked_at"] is not None

    def test_inbox_fail(self, cli_with_temp_db, capsys):
        """Test failing an event."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")

        event = store.enqueue_event(
            type="revise",
            payload={"note": "Make it brighter"},
            artifact_id="art_1",
        )
        claimed = store.claim_next_event("cli_worker")

        cli.inbox_fail(claimed["id"], error="Test error")
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "failed"
        assert "Test error" in output["error"]


class TestProjectCommands:
    """Tests for project subcommands."""

    def test_project_summary_empty(self, cli_with_temp_db, capsys):
        """Test project summary with no artifacts."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        cli.project_summary()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["total_artifacts"] == 0
        assert output["by_stage"] == {}
        assert output["by_status"] == {}

    def test_project_summary_with_artifacts(self, cli_with_temp_db, capsys):
        """Test project summary with artifacts."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script", status="approved")
        store.create_artifact(id="art_2", stage="shots", title="Shots", status="review")
        store.create_artifact(id="art_3", stage="shots", title="Shots 2", status="draft")

        cli.project_summary()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["total_artifacts"] == 3
        assert output["by_stage"]["script"] == 1
        assert output["by_stage"]["shots"] == 2
        assert output["by_status"]["approved"] == 1
        assert output["by_status"]["review"] == 1
        assert output["by_status"]["draft"] == 1


class TestLifecycleCommands:
    """Tests for init, status, and serve commands."""

    def test_init_creates_project(self, tmp_path, capsys):
        """Test initializing a new project."""
        db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
        cli = SurfaceCLI(db_path=db_path)

        cli.init(stage="movie", db=db_path)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        assert output["stage"] == "movie"

        # Verify store was created
        assert Path(db_path).exists()

    def test_init_fails_if_exists(self, cli_with_temp_db, capsys):
        """Test init fails if project already exists."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        cli.init(stage="movie")
        captured = capsys.readouterr()

        assert "already exists" in captured.err.lower()

    def test_status_no_project(self, tmp_path, capsys):
        """Test status when no project exists."""
        db_path = str(tmp_path / "nonexistent" / "state.sqlite3")
        cli = SurfaceCLI(db_path=db_path)

        cli.status()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["db_exists"] is False

    def test_status_with_project(self, cli_with_temp_db, capsys):
        """Test status when project exists."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        store.create_artifact(id="art_1", stage="script", title="Script")
        store.create_artifact(id="art_2", stage="shots", title="Shots")

        cli.status()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["db_exists"] is True
        assert output["artifact_count"] == 2
        assert output["by_stage"]["script"] == 1
        assert output["by_stage"]["shots"] == 1

    def test_serve_runs_iterations(self, tmp_path, capsys):
        """Test serve command runs supervisor iterations."""
        db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
        cli = SurfaceCLI(db_path=db_path)

        # Initialize project first
        cli.init(stage="movie", db=db_path)
        capsys.readouterr()  # Clear init output

        # Run serve with limited iterations
        cli.serve(db=db_path, stage="movie", supervisor_iterations=1)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        assert "completed" in output["message"].lower()


ECHO_WORKER_COMMAND = [
    sys.executable,
    "-c",
    """
import sys, json
for line in sys.stdin:
    try:
        data = json.loads(line)
        print(json.dumps({"ok": True, "result": {"processed": data.get("event_id")}}))
        sys.stdout.flush()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.stdout.flush()
""",
]


@pytest.fixture
def serve_project(tmp_path, capsys):
    """An initialized project ready for serve()."""
    db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
    cli = SurfaceCLI(db_path=db_path)
    cli.init(stage="movie", db=db_path)
    capsys.readouterr()
    return cli, db_path


class TestServeRuntime:
    """Tests for the wired-up `serve()` runtime (SPEC sections 9, 22, 36, 45)."""

    def test_serve_dispatches_event_to_real_worker(self, serve_project, capsys):
        """A pending event is claimed, sent to the worker subprocess, and acked."""
        cli, db_path = serve_project
        store = cli.get_store()
        store.create_artifact(id="script_1", stage="script", title="Script")
        event = store.enqueue_event(
            type="user_note",
            payload={"note": "hello"},
            artifact_id="script_1",
        )

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
        )
        output = json.loads(capsys.readouterr().out)

        assert output["ok"] is True
        assert output["iterations"] == 1

        # The event genuinely reached a worker and was acked afterwards.
        cursor = store.conn.cursor()
        cursor.execute("SELECT status FROM events WHERE id = ?", (event["id"],))
        assert cursor.fetchone()[0] == "acked"

    def test_serve_poller_advances_jobs(self, serve_project, capsys):
        """poll_once() runs each iteration, so async jobs actually progress."""
        cli, db_path = serve_project
        store = cli.get_store()
        store.create_artifact(id="shot_1", stage="shots", title="Shot")
        job = store.create_job(
            artifact_id="shot_1",
            provider="image_default",
            kind="image",
            request={"prompt": "a cat"},
        )
        assert job["status"] == "queued"

        cli.serve(
            db=db_path,
            max_iterations=5,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
        )
        capsys.readouterr()

        updated = store.get_job(job["id"])
        assert updated["status"] == "succeeded", updated
        assert updated["provider_job_id"]

    def test_serve_writes_runtime_token_port_and_pids(self, serve_project, capsys):
        """SPEC section 9: run/{token,port,board.pid,supervisor.pid,poller.pid}."""
        cli, db_path = serve_project

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        run_dir = Path(db_path).resolve().parent / "run"
        token_file = run_dir / "token"
        port_file = run_dir / "port"

        assert token_file.exists()
        assert port_file.exists()
        assert (run_dir / "supervisor.pid").read_text() == str(os.getpid())
        assert (run_dir / "poller.pid").read_text() == str(os.getpid())

        token = token_file.read_text()
        # Unpredictable: secrets.token_urlsafe(32) -> >= 40 urlsafe chars.
        assert len(token) >= 40
        assert token not in ("", "token", "surface", "changeme", "default")
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
        assert cli.runtime_info["token"] == token

        # Restrictive permissions on the secret (SPEC section 9).
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

        port = int(port_file.read_text())
        assert 1024 < port < 65536
        assert cli.runtime_info["port"] == port

    def test_serve_tokens_differ_between_runs(self, serve_project, capsys):
        """The board credential is per-run, not a fixed default."""
        cli, db_path = serve_project
        tokens = set()
        for _ in range(2):
            cli.serve(
                db=db_path,
                max_iterations=1,
                board=False,
                worker_command=ECHO_WORKER_COMMAND,
                keep_runtime_files=True,
            )
            capsys.readouterr()
            tokens.add(cli.runtime_info["token"])
        assert len(tokens) == 2

    def test_serve_clears_runtime_files_on_shutdown(self, serve_project, capsys):
        """SPEC section 45: clean shutdown invalidates pid/token files."""
        cli, db_path = serve_project

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
        )
        capsys.readouterr()

        run_dir = Path(db_path).resolve().parent / "run"
        for name in ("token", "port", "board.pid", "supervisor.pid", "poller.pid"):
            assert not (run_dir / name).exists(), name

    def test_serve_respects_runtime_dir_override(self, serve_project, tmp_path, capsys):
        cli, db_path = serve_project
        custom = tmp_path / "custom-run"

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            runtime_dir=str(custom),
            keep_runtime_files=True,
        )
        output = json.loads(capsys.readouterr().out)

        assert output["runtime_dir"] == str(custom)
        assert (custom / "token").exists()

    def test_serve_without_gradio_does_not_crash(self, serve_project, monkeypatch, capsys):
        """build_board() returning None must not break the supervisor loop."""
        cli, db_path = serve_project
        monkeypatch.setattr(cli, "_build_board_blocks", lambda store, cfg: None)

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=True,
            worker_command=ECHO_WORKER_COMMAND,
        )
        output = json.loads(capsys.readouterr().out)

        assert output["ok"] is True
        assert output["board"] == "unavailable"
        assert output["iterations"] == 1

    def test_serve_survives_missing_gradio_import(self, serve_project, monkeypatch, capsys):
        """An ImportError from the board module is logged, not raised."""
        cli, db_path = serve_project

        def _boom(store, cfg):
            raise ImportError("No module named 'gradio'")

        monkeypatch.setattr(cli, "_build_board_blocks", _boom)

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=True,
            worker_command=ECHO_WORKER_COMMAND,
        )
        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is True
        assert output["board"] == "unavailable"

    def test_serve_launches_board_when_available(self, serve_project, monkeypatch, capsys):
        """When build_board returns Blocks, launch() is called on loopback with auth."""
        cli, db_path = serve_project
        launched = {}
        done = threading.Event()

        class FakeBlocks:
            def launch(self, **kwargs):
                launched.update(kwargs)
                done.set()

        monkeypatch.setattr(cli, "_build_board_blocks", lambda store, cfg: FakeBlocks())

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=True,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        output = json.loads(capsys.readouterr().out)

        assert done.wait(5), "board thread never launched"
        assert output["board"] == "running"
        assert launched["server_name"] == "127.0.0.1"
        assert launched["share"] is False
        # SPEC section 36: authenticated, with the runtime token as credential.
        assert launched["auth"] == ("surface", cli.runtime_info["token"])
        assert launched["server_port"] == cli.runtime_info["port"]

        run_dir = Path(db_path).resolve().parent / "run"
        assert (run_dir / "board.pid").read_text() == str(os.getpid())

    def test_serve_max_iterations_bounds_the_loop(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=3,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
        )
        output = json.loads(capsys.readouterr().out)
        assert output["iterations"] == 3

    def test_serve_run_once_bounds_the_loop(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            run_once=True,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
        )
        output = json.loads(capsys.readouterr().out)
        assert output["iterations"] == 1

    def test_serve_default_worker_command_targets_worker_module(self, serve_project, capsys):
        """Without an override, the transport spawns `python -m surface.worker`."""
        cli, db_path = serve_project
        captured = {}

        import surface.cli as cli_module
        real_transport = cli_module.NativeStreamTransport

        def _spy(command=None, **kwargs):
            captured["command"] = command
            return real_transport(command=ECHO_WORKER_COMMAND, **kwargs)

        cli_module.NativeStreamTransport = _spy
        try:
            cli.serve(db=db_path, max_iterations=1, board=False)
        finally:
            cli_module.NativeStreamTransport = real_transport
        capsys.readouterr()

        assert captured["command"][0] == sys.executable
        assert captured["command"][1:3] == ["-m", "surface.worker"]
        assert captured["command"][3] == "--db"
        assert captured["command"][4].endswith("state.sqlite3")

    def test_serve_requires_initialized_store(self, tmp_path, capsys):
        cli = SurfaceCLI(db_path=str(tmp_path / "missing" / "state.sqlite3"))
        cli.serve(max_iterations=1, board=False)
        captured = capsys.readouterr()
        assert "No store" in captured.err


class TestErrorHandling:
    """Tests for error handling."""

    def test_store_operations_without_db(self, tmp_path, capsys):
        """Test store operations fail gracefully without a store."""
        db_path = str(tmp_path / "nonexistent" / "state.sqlite3")
        cli = SurfaceCLI(db_path=db_path)

        cli.store_get("art_1")
        captured = capsys.readouterr()

        assert "No store" in captured.err or "Error" in captured.err

    def test_invalid_json_input(self, cli_with_temp_db, monkeypatch, capsys):
        """Test handling of invalid JSON input."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)
        store.create_artifact(id="art_1", stage="script", title="Script")

        # Mock stdin with invalid JSON
        monkeypatch.setattr("sys.stdin", StringIO("not valid json"))

        cli.store_put_version("art_1")
        captured = capsys.readouterr()

        assert "Invalid JSON" in captured.err or "Error" in captured.err


class TestIntegration:
    """Integration tests combining multiple operations."""

    def test_full_workflow(self, tmp_path, capsys):
        """Test a complete workflow: init -> create artifacts -> events -> project summary."""
        db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
        cli = SurfaceCLI(db_path=db_path)

        # Initialize
        cli.init(stage="movie", db=db_path)
        captured = capsys.readouterr()
        init_output = json.loads(captured.out)
        assert init_output["ok"] is True

        # Create artifacts
        store = cli.get_store()
        store.create_artifact(id="script_1", stage="script", title="Script", status="approved")
        store.create_artifact(id="shots_1", stage="shots", title="Shots", status="review")

        # Add dependency
        cli.store_add_dependency("script_1", "shots_1")
        captured = capsys.readouterr()
        dep_output = json.loads(captured.out)
        assert dep_output["ok"] is True

        # Get project summary
        cli.project_summary()
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["total_artifacts"] == 2

        # Get status
        cli.status()
        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["db_exists"] is True
        assert status["artifact_count"] == 2


class TestStopCommand:
    """Tests for `surface stop` (SPEC section 45)."""

    def test_stop_noop_when_no_runtime_files(self, cli_with_temp_db, capsys):
        """No runtime dir/pid files at all is a clean no-op, not an error."""
        cli = cli_with_temp_db
        cli.get_store(create=True)

        cli.stop()
        captured = capsys.readouterr()
        assert captured.err == ""
        output = json.loads(captured.out)
        assert output["ok"] is True
        assert output["pids_found"] == {}
        assert output["stopped_pids"] == []

    def test_stop_signals_running_process_and_clears_files_only(
        self, cli_with_temp_db, capsys
    ):
        """A live pid gets SIGTERM; db/artifacts are untouched, only run/* is cleared."""
        import subprocess

        cli = cli_with_temp_db
        store = cli.get_store(create=True)
        store.create_artifact(id="art_1", stage="script", title="Script")
        store.put_version(artifact_id="art_1", content="hello", created_by="test")

        run_dir = cli.runtime_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            for name in ("board.pid", "supervisor.pid", "poller.pid"):
                (run_dir / name).write_text(str(proc.pid))
            (run_dir / "token").write_text("sometoken")
            (run_dir / "port").write_text("12345")

            cli.stop()
            captured = capsys.readouterr()
            output = json.loads(captured.out)

            assert output["ok"] is True
            assert output["stopped_pids"] == [proc.pid]

            proc.wait(timeout=5)
            assert proc.returncode is not None

            for name in ("board.pid", "supervisor.pid", "poller.pid", "token", "port"):
                assert not (run_dir / name).exists()

            # DB and artifacts must be untouched.
            assert Path(cli.db_path).exists()
            artifacts = store.list_artifacts()
            assert len(artifacts) == 1
            assert store.get_artifact("art_1") is not None
            assert len(store.list_versions("art_1")) == 1
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_stop_never_signals_own_pid(self, cli_with_temp_db, capsys):
        """A pid file matching this process's own pid must never be signalled."""
        cli = cli_with_temp_db
        cli.get_store(create=True)
        run_dir = cli.runtime_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "supervisor.pid").write_text(str(os.getpid()))

        cli.stop()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["skipped_self"] is True
        assert os.getpid() not in output["stopped_pids"]
        # We must still be alive to observe this.
        assert True


class TestRecoverCommand:
    """Tests for `surface recover` (SPEC section 40)."""

    def test_recover_reports_expired_lease_event_without_mutating(
        self, cli_with_temp_db, capsys
    ):
        cli = cli_with_temp_db
        store = cli.get_store(create=True)
        store.create_artifact(id="art_1", stage="script", title="Script")
        event = store.enqueue_event(type="user_note", payload={}, artifact_id="art_1")
        claimed = store.claim_next_event(worker_id="w1")
        assert claimed["id"] == event["id"]

        # Force the lease into the past directly (public API has no setter for this).
        store.conn.execute(
            "UPDATE events SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", event["id"]),
        )
        store.conn.commit()

        cli.recover()
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["force_reclaim"] is False
        ids = [e["id"] for e in output["expired_lease_events"]]
        assert event["id"] in ids

        # No mutation by default: still processing.
        fresh = store.get_event(event["id"])
        assert fresh["status"] == "processing"

    def test_recover_reports_stuck_job(self, cli_with_temp_db, capsys):
        cli = cli_with_temp_db
        store = cli.get_store(create=True)
        store.create_artifact(id="art_1", stage="shots", title="Shot")
        job = store.create_job(
            artifact_id="art_1", provider="image_default", kind="image", request={}
        )
        store.conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
        store.conn.commit()

        cli.recover(stale_seconds=300)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        ids = [j["id"] for j in output["stuck_jobs"]]
        assert job["id"] in ids

        fresh = store.get_job(job["id"])
        assert fresh["status"] == "queued"  # untouched by default

    def test_recover_force_reclaim_resets_expired_event_and_fails_stuck_job(
        self, cli_with_temp_db, capsys
    ):
        cli = cli_with_temp_db
        store = cli.get_store(create=True)
        store.create_artifact(id="art_1", stage="script", title="Script")
        event = store.enqueue_event(type="user_note", payload={}, artifact_id="art_1")
        store.claim_next_event(worker_id="w1")
        store.conn.execute(
            "UPDATE events SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", event["id"]),
        )

        store.create_artifact(id="art_2", stage="shots", title="Shot")
        job = store.create_job(
            artifact_id="art_2", provider="image_default", kind="image", request={}
        )
        store.conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
        store.conn.commit()

        cli.recover(force_reclaim=True, stale_seconds=300)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["force_reclaim"] is True
        assert len(output["actions"]) >= 2

        # The event is no longer stuck 'processing' with an expired lease --
        # it's either back to pending, or explicitly failed with a message.
        fresh_event = store.get_event(event["id"])
        assert fresh_event["status"] in ("pending", "failed")

        fresh_job = store.get_job(job["id"])
        assert fresh_job["status"] == "failed"

    def test_recover_force_reclaim_does_not_strand_an_unrelated_healthy_event(
        self, cli_with_temp_db, capsys
    ):
        """A prior version of --force-reclaim used claim_next_event() purely
        for its lease-reset side effect; if that same call also claimed an
        unrelated healthy pending event, that event was left leased/
        'processing' under a 'surface-recover' worker id that would never
        actually process it -- self-healing only after its lease expired.
        reclaim_expired_leases() must reset only expired-lease events and
        never touch/claim a healthy pending event at all."""
        cli = cli_with_temp_db
        store = cli.get_store(create=True)

        # An expired-lease event to be reclaimed.
        store.create_artifact(id="art_expired", stage="script", title="Expired")
        expired_event = store.enqueue_event(
            type="user_note", payload={}, artifact_id="art_expired"
        )
        store.claim_next_event(worker_id="w1")
        store.conn.execute(
            "UPDATE events SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", expired_event["id"]),
        )
        store.conn.commit()

        # A completely healthy, never-claimed pending event on a different
        # artifact -- must remain untouched by --force-reclaim.
        store.create_artifact(id="art_healthy", stage="script", title="Healthy")
        healthy_event = store.enqueue_event(
            type="user_note", payload={}, artifact_id="art_healthy"
        )

        cli.recover(force_reclaim=True)
        capsys.readouterr()

        fresh_healthy = store.get_event(healthy_event["id"])
        assert fresh_healthy["status"] == "pending"
        assert fresh_healthy["claimed_by"] is None
        assert fresh_healthy["lease_until"] is None

        fresh_expired = store.get_event(expired_event["id"])
        assert fresh_expired["status"] == "pending"


class TestInterruptCommand:
    """Tests for `surface interrupt` (SPEC section 22/56)."""

    def test_interrupt_writes_flag_file(self, cli_with_temp_db, capsys, tmp_path):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        runtime_dir = str(tmp_path / "run")

        cli.interrupt(runtime_dir=runtime_dir)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["ok"] is True
        flag_path = Path(output["flag_path"])
        assert flag_path.exists()
        assert flag_path.name == "interrupt_requested"

    def test_interrupt_reports_not_likely_running_without_pid_file(
        self, cli_with_temp_db, capsys, tmp_path
    ):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        cli.interrupt(runtime_dir=str(tmp_path / "run"))
        output = json.loads(capsys.readouterr().out)
        assert output["likely_running"] is False


class TestTuiCommand:
    """Tests for `surface tui` (Phase 3: stdlib/non-gradio renderer)."""

    def test_tui_without_store_errors_cleanly(self, cli_with_temp_db, capsys):
        cli = cli_with_temp_db
        cli.tui()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() != ""

    def test_tui_runs_against_a_real_initialized_store(
        self, tmp_path, monkeypatch, capsys
    ):
        """End-to-end: cli.tui() loads the stage config from kv_state (the
        same path `init` writes and `serve` reads) and drives a real
        run_tui() session against it, with no gradio import anywhere in the
        chain."""
        db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
        cli = SurfaceCLI(db_path=db_path)
        cli.init(stage="movie", db=db_path)
        capsys.readouterr()  # discard init's JSON output

        store = cli.get_store()
        store.create_artifact(id="script_001", stage="script", title="Script")

        monkeypatch.setattr("sys.stdin", StringIO("list\nquit\n"))
        cli.tui()
        captured = capsys.readouterr()
        assert "script_001" in captured.out
        assert "Goodbye" in captured.out


class TestRenderWaitCommands:
    """Tests for `surface render` + `surface wait` (SPEC section 35)."""

    def _write_data(self, tmp_path, data):
        data_path = tmp_path / "data.json"
        data_path.write_text(json.dumps(data))
        return str(data_path)

    def test_render_creates_pending_handle(self, cli_with_temp_db, tmp_path, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": ["q1"]})

        cli.render("questionnaire", data=data_path)
        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["status"] == "pending"
        assert output["handle"].startswith("interaction_")

        interaction_file = cli._interactions_dir() / f"{output['handle']}.json"
        assert interaction_file.exists()
        record = json.loads(interaction_file.read_text())
        assert record["content"] == {"questions": ["q1"]}
        assert record["status"] == "pending"

    def test_render_missing_data_file_fails_cleanly(self, cli_with_temp_db, tmp_path, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)

        cli.render("questionnaire", data=str(tmp_path / "nope.json"))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "not found" in captured.err.lower()

    def test_render_unknown_preset_fails_cleanly(self, cli_with_temp_db, tmp_path, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": []})

        cli.render("totally_unknown_preset_xyz", data=data_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error" in captured.err.lower()

    def test_wait_pending_after_bound_elapses(self, cli_with_temp_db, tmp_path, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": ["q1"]})

        cli.render("questionnaire", data=data_path)
        handle = json.loads(capsys.readouterr().out)["handle"]

        start = time.monotonic()
        cli.wait(handle, max_seconds=0.3)
        elapsed = time.monotonic() - start

        output = json.loads(capsys.readouterr().out)
        assert output == {"status": "pending", "handle": handle}
        assert elapsed < 2.0  # bounded, fast

    def test_wait_returns_answered_when_externally_answered(
        self, cli_with_temp_db, tmp_path, capsys
    ):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": ["q1"]})

        cli.render("questionnaire", data=data_path)
        handle = json.loads(capsys.readouterr().out)["handle"]

        # Simulate an external "answer" being written by another process.
        interaction_file = cli._interactions_dir() / f"{handle}.json"
        record = json.loads(interaction_file.read_text())
        record["status"] = "answered"
        record["value"] = {"q1": "yes"}
        interaction_file.write_text(json.dumps(record))

        cli.wait(handle, max_seconds=0.5)
        output = json.loads(capsys.readouterr().out)
        assert output == {"status": "answered", "value": {"q1": "yes"}}

    def test_wait_picks_up_late_answer_within_bound(self, cli_with_temp_db, tmp_path, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": ["q1"]})

        cli.render("questionnaire", data=data_path)
        handle = json.loads(capsys.readouterr().out)["handle"]
        interaction_file = cli._interactions_dir() / f"{handle}.json"

        def answer_soon():
            time.sleep(0.15)
            record = json.loads(interaction_file.read_text())
            record["status"] = "answered"
            record["value"] = {"q1": "no"}
            interaction_file.write_text(json.dumps(record))

        t = threading.Thread(target=answer_soon)
        t.start()
        cli.wait(handle, max_seconds=2.0)
        t.join()

        output = json.loads(capsys.readouterr().out)
        assert output == {"status": "answered", "value": {"q1": "no"}}

    def test_wait_unknown_handle_fails_cleanly(self, cli_with_temp_db, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)

        cli.wait("interaction_doesnotexist", max_seconds=0.2)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "unknown" in captured.err.lower() or "error" in captured.err.lower()

    def test_render_answer_wait_round_trip_via_cli_commands_only(
        self, cli_with_temp_db, tmp_path, capsys
    ):
        """Closes the render/wait round-trip gap: previously nothing in the
        system could ever write status="answered" into an interaction file
        in real usage (only tests poked the file directly), so `wait` could
        never return "answered" outside a test. `surface answer` is the
        missing write side; this drives all three commands exactly as a
        real user/script would, with no direct file manipulation."""
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": ["Pick one"]})

        cli.render("questionnaire", data=data_path)
        handle = json.loads(capsys.readouterr().out)["handle"]

        cli.answer(handle, value='{"choice": "A"}')
        answer_output = json.loads(capsys.readouterr().out)
        assert answer_output["ok"] is True
        assert answer_output["status"] == "answered"

        cli.wait(handle, max_seconds=1.0)
        wait_output = json.loads(capsys.readouterr().out)
        assert wait_output["status"] == "answered"
        assert wait_output["value"] == {"choice": "A"}

    def test_answer_unknown_handle_fails_cleanly(self, cli_with_temp_db, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        cli.answer("interaction_doesnotexist", value="{}")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "unknown" in captured.err.lower()

    def test_answer_invalid_json_value_fails_cleanly(self, cli_with_temp_db, tmp_path, capsys):
        cli = cli_with_temp_db
        cli.get_store(create=True)
        data_path = self._write_data(tmp_path, {"questions": []})
        cli.render("questionnaire", data=data_path)
        handle = json.loads(capsys.readouterr().out)["handle"]

        cli.answer(handle, value="{not valid json")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "json" in captured.err.lower()
