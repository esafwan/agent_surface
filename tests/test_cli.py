"""
Tests for Agent Surface Board CLI module.

Tests use temporary SQLite databases and direct function calls
(not subprocess) for speed and error visibility.
"""

import json
import tempfile
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
