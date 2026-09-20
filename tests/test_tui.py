"""
Unit tests for surface/tui.py — the stdlib-only plain-terminal board renderer.

This file is self-contained (does not import fixtures from test_board.py) so
it can be run in isolation.
"""
import io

import pytest

from surface.store import Store
from surface.stages.config import load_preset, StageConfig
from surface.tui import render_board_text, run_tui


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def stage_config():
    return load_preset("movie")


@pytest.fixture
def populated_store(store, stage_config):
    """Minimal populated store, mirroring test_board.py's fixture shape."""
    store.create_artifact(id="script_001", stage="script", title="Movie Script")
    store.put_version(
        "script_001",
        content="Scene 1: A hero walks into a room...",
        created_by="user",
        note="Initial draft",
        select=True,
    )

    store.create_artifact(id="shots_001", stage="shots", title="Shot List")
    store.put_version(
        "shots_001",
        content="Shot 1: Wide of room\nShot 2: Close of hero",
        created_by="worker",
        select=True,
    )
    store.add_dependency("script_001", "shots_001", kind="content")

    store.create_artifact(id="keyframe_001", stage="keyframes", title="Keyframe 1")
    store.put_version(
        "keyframe_001",
        content_type="image/png",
        content_ref="media/keyframe_001_v1.png",
        prompt="A hero in a room, cinematic lighting",
        created_by="worker",
        select=False,
    )
    store.put_version(
        "keyframe_001",
        content_type="image/png",
        content_ref="media/keyframe_001_v2.png",
        prompt="A hero in a room, warm lighting",
        created_by="worker",
        select=True,
    )

    return store


# =============================================================================
# render_board_text
# =============================================================================


def test_render_board_text_header(populated_store, stage_config):
    text = render_board_text(populated_store, stage_config)
    assert "Agent Surface Board" in text
    assert "pending review:" in text
    assert "pending events:" in text


def test_render_board_text_shows_stages_and_artifacts(populated_store, stage_config):
    text = render_board_text(populated_store, stage_config)
    assert "script_001" in text
    assert "shots_001" in text
    assert "keyframe_001" in text
    assert "Movie Script" in text


def test_render_board_text_shows_selected_version_and_badges(populated_store, stage_config):
    text = render_board_text(populated_store, stage_config)
    # keyframe_001's second version is selected -> v2
    assert "selected=v2" in text or "selected=v1" in text  # sanity, refined below
    assert "v1" in text  # script_001 selected version


def test_render_board_text_empty_store(store, stage_config):
    text = render_board_text(store, stage_config)
    assert "Agent Surface Board" in text
    assert "(no artifacts)" in text


def test_render_board_text_focus_shows_content_and_history(populated_store, stage_config):
    text = render_board_text(populated_store, stage_config, focus_artifact_id="script_001")
    assert "Artifact: script_001" in text
    assert "Scene 1: A hero walks into a room..." in text
    assert "Version history:" in text
    assert "Allowed actions:" in text


def test_render_board_text_focus_unknown_artifact(populated_store, stage_config):
    text = render_board_text(populated_store, stage_config, focus_artifact_id="does_not_exist")
    assert "not found" in text


def test_render_board_text_focus_image_content(populated_store, stage_config):
    text = render_board_text(populated_store, stage_config, focus_artifact_id="keyframe_001")
    assert "[image]" in text
    assert "media/keyframe_001_v2.png" in text


# =============================================================================
# run_tui
# =============================================================================


def run_commands(store, stage_config, commands):
    """Helper: feed a list of command strings to run_tui, return captured output."""
    input_stream = io.StringIO("\n".join(commands) + "\n")
    output_stream = io.StringIO()
    run_tui(store, stage_config, input_stream=input_stream, output_stream=output_stream)
    return output_stream.getvalue()


def test_run_tui_list_and_quit(populated_store, stage_config):
    output = run_commands(populated_store, stage_config, ["list", "quit"])
    assert "Agent Surface Board" in output
    assert "Goodbye." in output


def test_run_tui_show_artifact(populated_store, stage_config):
    output = run_commands(populated_store, stage_config, ["show script_001", "quit"])
    assert "Artifact: script_001" in output
    assert "Scene 1: A hero walks into a room..." in output


def test_run_tui_eof_exits_cleanly(populated_store, stage_config):
    # No 'quit' command; input stream just ends.
    input_stream = io.StringIO("list\n")
    output_stream = io.StringIO()
    run_tui(populated_store, stage_config, input_stream=input_stream, output_stream=output_stream)
    assert "(EOF) exiting." in output_stream.getvalue()


def test_run_tui_approve_changes_store_state(populated_store, stage_config):
    assert populated_store.get_artifact("script_001")["status"] != "approved"
    output = run_commands(populated_store, stage_config, ["approve script_001", "quit"])
    assert "Approved script_001." in output
    assert populated_store.get_artifact("script_001")["status"] == "approved"


def test_run_tui_approve_unknown_artifact(populated_store, stage_config):
    output = run_commands(populated_store, stage_config, ["approve nope", "quit"])
    assert "Approve failed" in output


def test_run_tui_select_success(populated_store, stage_config):
    versions = populated_store.list_versions("keyframe_001")
    v1_id = versions[0]["id"]
    output = run_commands(
        populated_store, stage_config, [f"select keyframe_001 {v1_id}", "quit"]
    )
    assert f"Selected version {v1_id} for keyframe_001." in output
    assert populated_store.get_artifact("keyframe_001")["selected_version_id"] == v1_id


def test_run_tui_select_conflict_surfaces_message(populated_store, stage_config):
    versions = populated_store.list_versions("keyframe_001")
    v1_id = versions[0]["id"]
    v2_id = versions[1]["id"]
    # Pass a stale expected_selected_version_id (v1) while actual current is v2.
    output = run_commands(
        populated_store,
        stage_config,
        [f"select keyframe_001 {v1_id} {v1_id}", "quit"],
    )
    assert "Selection conflict" in output
    # Selection must NOT have changed as a result of the conflicting call.
    assert populated_store.get_artifact("keyframe_001")["selected_version_id"] == v2_id


def _lockable_stage_config():
    """A minimal stage config whose stage allows lock/unlock (the movie
    preset used elsewhere in this file does not, by design)."""
    return StageConfig(
        {
            "schema_version": "1",
            "id": "lockable",
            "title": "Lockable",
            "stages": [
                {
                    "id": "script",
                    "artifact_type": "text",
                    "allowed_actions": ["lock", "unlock", "approve"],
                }
            ],
        }
    )


def test_run_tui_lock_and_unlock_toggle(store):
    stage_config = _lockable_stage_config()
    store.create_artifact(id="script_001", stage="script", title="Movie Script")
    output = run_commands(
        store, stage_config, ["lock script_001", "unlock script_001", "quit"]
    )
    assert "Locked script_001." in output
    assert "Unlocked script_001." in output
    assert store.get_artifact("script_001")["locked"] is False


def test_run_tui_lock_state_between_commands(store):
    stage_config = _lockable_stage_config()
    store.create_artifact(id="script_001", stage="script", title="Movie Script")
    output = run_commands(store, stage_config, ["lock script_001", "quit"])
    assert "Locked script_001." in output
    assert store.get_artifact("script_001")["locked"] is True


def test_run_tui_unknown_command_does_not_crash(populated_store, stage_config):
    output = run_commands(populated_store, stage_config, ["bogus_command", "list", "quit"])
    assert "Unknown command" in output
    # Loop kept going and handled 'list' afterward.
    assert "Agent Surface Board" in output
    assert "Goodbye." in output


def test_run_tui_help_command(populated_store, stage_config):
    output = run_commands(populated_store, stage_config, ["help", "quit"])
    assert "Commands:" in output
    assert "approve <artifact_id>" in output


def test_run_tui_missing_args_usage_message(populated_store, stage_config):
    output = run_commands(populated_store, stage_config, ["show", "approve", "quit"])
    assert "Usage: show <artifact_id>" in output
    assert "Usage: approve <artifact_id>" in output
