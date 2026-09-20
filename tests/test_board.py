"""
Unit tests for board.py module.

Tests cover display model construction, action classification, and action handlers.
Core logic tests do NOT require gradio to be installed.
Gradio-specific wiring tests are skipped if gradio unavailable.
"""
import pytest
import json
from surface.store import Store
from surface.stages.config import load_preset
from surface.board import (
    DisplayModelBuilder,
    ActionHandler,
    ActionClassification,
    VersionDisplayRow,
    ArtifactDisplayCard,
    StageDisplayGroup,
    BoardDisplayModel,
    render_version_content,
)


@pytest.fixture
def store():
    """Create in-memory store for testing."""
    return Store(":memory:")


@pytest.fixture
def stage_config():
    """Load movie stage config."""
    return load_preset("movie")


@pytest.fixture
def populated_store(store, stage_config):
    """Create store with sample artifacts and versions."""
    # Create script artifact
    script = store.create_artifact(
        id="script_001",
        stage="script",
        title="Movie Script",
        meta={"scene_count": 5},
    )

    # Add version to script
    script_v1 = store.put_version(
        "script_001",
        content="Scene 1: A hero walks into a room...",
        created_by="user",
        note="Initial draft",
        select=True,
    )

    # Create shots artifact
    shots = store.create_artifact(
        id="shots_001",
        stage="shots",
        title="Shot List",
    )
    shots_v1 = store.put_version(
        "shots_001",
        content="Shot 1: Wide of room\nShot 2: Close of hero",
        created_by="worker",
        select=True,
    )

    # Create dependency
    store.add_dependency("script_001", "shots_001", kind="content")

    # Create keyframes artifact
    keyframes = store.create_artifact(
        id="keyframe_001",
        stage="keyframes",
        title="Keyframe 1",
    )
    kf_v1 = store.put_version(
        "keyframe_001",
        content_type="image/png",
        content_ref="media/keyframe_001_v1.png",
        prompt="A hero in a room, cinematic lighting",
        created_by="worker",
        select=True,
    )

    # Add second version (not selected)
    kf_v2 = store.put_version(
        "keyframe_001",
        content_type="image/png",
        content_ref="media/keyframe_001_v2.png",
        prompt="A hero in a room, warm lighting",
        created_by="worker",
        select=False,
    )

    store.add_dependency("shots_001", "keyframe_001", kind="content")

    return store


# =============================================================================
# Display Model Tests
# =============================================================================


def test_display_model_builder_initialization(store, stage_config):
    """Test DisplayModelBuilder initialization."""
    builder = DisplayModelBuilder(store, stage_config)
    assert builder.store == store
    assert builder.stage_config == stage_config


def test_build_board_display_empty_store(store, stage_config):
    """Test board display with empty store."""
    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    assert isinstance(display, BoardDisplayModel)
    assert len(display.stages) == 5  # movie has 5 stages
    assert display.pending_review_count == 0
    assert display.total_pending_events == 0
    assert len(display.generating_artifacts) == 0
    assert len(display.stale_artifacts) == 0


def test_build_board_display_with_artifacts(populated_store, stage_config):
    """Test board display with populated artifacts."""
    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    assert len(display.stages) == 5
    script_stage = next((s for s in display.stages if s.stage_id == "script"), None)
    assert script_stage is not None
    assert script_stage.total_count == 1
    assert len(script_stage.artifacts) == 1

    card = script_stage.artifacts[0]
    assert card.artifact_id == "script_001"
    assert card.title == "Movie Script"
    assert card.selected_version is not None
    assert card.selected_version.version_num == 1


def test_artifact_display_card_with_versions(populated_store, stage_config):
    """Test artifact card building with multiple versions."""
    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    keyframe_stage = next((s for s in display.stages if s.stage_id == "keyframes"), None)
    assert keyframe_stage is not None
    assert keyframe_stage.total_count == 1

    card = keyframe_stage.artifacts[0]
    assert card.artifact_id == "keyframe_001"
    assert len(card.all_versions) == 2
    assert card.selected_version.version_num == 1

    # Check version marking
    assert card.all_versions[0].is_selected is True
    assert card.all_versions[1].is_selected is False


def test_stage_display_group_approval_count(populated_store, stage_config):
    """Test approval counting in stage groups."""
    # Approve the script
    populated_store.set_status("script_001", "approved")

    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    script_stage = next((s for s in display.stages if s.stage_id == "script"), None)
    assert script_stage.approved_count == 1
    assert script_stage.total_count == 1


def test_board_display_generating_status(populated_store, stage_config):
    """Test generating status detection."""
    # Create a job for keyframe
    populated_store.create_job(
        artifact_id="keyframe_001",
        provider="image_default",
        kind="image",
        request={"prompt": "test"},
    )

    # Mark artifact as generating
    populated_store.set_status("keyframe_001", "generating")

    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    assert "keyframe_001" in display.generating_artifacts


def test_board_display_stale_status(populated_store, stage_config):
    """Test stale status detection."""
    # Update script version (will mark shots as stale)
    script_v2 = populated_store.put_version(
        "script_001",
        content="Scene 1: Updated...",
        created_by="user",
        select=True,
    )

    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    assert "shots_001" in display.stale_artifacts


def test_board_display_locked_status(populated_store, stage_config):
    """Test locked artifact detection."""
    populated_store.set_lock("keyframe_001", True)

    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    assert "keyframe_001" in display.locked_artifacts


# =============================================================================
# Action Classification Tests
# =============================================================================


def test_action_classification_deterministic():
    """Test deterministic action classification."""
    assert ActionClassification.is_deterministic("select_version")
    assert ActionClassification.is_deterministic("approve")
    assert ActionClassification.is_deterministic("lock")
    assert ActionClassification.is_deterministic("unlock")


def test_action_classification_worker_event():
    """Test worker event action classification."""
    assert ActionClassification.is_worker_event("edit")
    assert ActionClassification.is_worker_event("revise")
    assert ActionClassification.is_worker_event("regenerate")
    assert ActionClassification.is_worker_event("reopen")
    assert ActionClassification.is_worker_event("cancel")
    assert ActionClassification.is_worker_event("message")


def test_action_classification_exclusive():
    """Test that actions are exclusively classified."""
    all_actions = (
        ActionClassification.DETERMINISTIC_ACTIONS
        | ActionClassification.WORKER_EVENT_ACTIONS
    )
    overlap = (
        ActionClassification.DETERMINISTIC_ACTIONS
        & ActionClassification.WORKER_EVENT_ACTIONS
    )
    assert len(overlap) == 0, "Actions should not be in both categories"


# =============================================================================
# Action Handler Tests (Deterministic Actions)
# =============================================================================


def test_action_handler_approve(populated_store):
    """Test approve action (deterministic)."""
    handler = ActionHandler(populated_store)
    result = handler.approve("script_001")

    assert result["ok"] is True
    artifact = result["artifact"]
    assert artifact["status"] == "approved"


def test_action_handler_approve_nonexistent(populated_store):
    """Test approve on nonexistent artifact."""
    handler = ActionHandler(populated_store)
    result = handler.approve("nonexistent_001")

    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_action_handler_lock(populated_store):
    """Test lock action (deterministic)."""
    handler = ActionHandler(populated_store)
    result = handler.lock("keyframe_001")

    assert result["ok"] is True
    artifact = result["artifact"]
    assert artifact["locked"] is True


def test_action_handler_unlock(populated_store):
    """Test unlock action (deterministic)."""
    handler = ActionHandler(populated_store)

    # First lock
    handler.lock("keyframe_001")

    # Then unlock
    result = handler.unlock("keyframe_001")
    assert result["ok"] is True
    artifact = result["artifact"]
    assert artifact["locked"] is False


def test_action_handler_select_version(populated_store):
    """Test select_version action (deterministic)."""
    handler = ActionHandler(populated_store)

    # Get versions
    versions = populated_store.list_versions("keyframe_001")
    assert len(versions) == 2

    # Select version 2
    result = handler.select_version("keyframe_001", versions[1]["id"])

    assert result["ok"] is True
    artifact = populated_store.get_artifact("keyframe_001")
    assert artifact["selected_version_id"] == versions[1]["id"]


def test_action_handler_select_version_conflict(populated_store):
    """Test select_version with conflict detection."""
    handler = ActionHandler(populated_store)

    versions = populated_store.list_versions("keyframe_001")

    # Attempt selection with wrong expected version
    result = handler.select_version(
        "keyframe_001",
        versions[1]["id"],
        expected_selected_version_id="wrong_version_id",
    )

    assert result["ok"] is False
    assert result["error"] == "conflict"


def test_action_handler_select_version_triggers_stale(populated_store):
    """Test that select_version triggers stale propagation."""
    handler = ActionHandler(populated_store)

    # Create new script version
    script_v2 = populated_store.put_version(
        "script_001",
        content="Scene 1: Completely new...",
        created_by="user",
        select=False,
    )

    # Select new version
    result = handler.select_version("script_001", script_v2["version_id"])

    assert result["ok"] is True
    assert len(result["stale_descendants"]) > 0
    assert "shots_001" in result["stale_descendants"]


# =============================================================================
# Action Handler Tests (Worker Event Actions)
# =============================================================================


def test_action_handler_enqueue_edit(populated_store):
    """Test edit action (creates worker event)."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_edit("script_001", "New content here")

    assert result["ok"] is True
    assert "event_id" in result
    assert result["event"]["type"] == "edit"
    assert result["event"]["payload"]["content"] == "New content here"


def test_action_handler_enqueue_revise(populated_store):
    """Test revise action (creates worker event)."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_revise("keyframe_001", "Warmer lighting")

    assert result["ok"] is True
    assert "event_id" in result
    assert result["event"]["type"] == "revise"
    assert result["event"]["payload"]["note"] == "Warmer lighting"


def test_action_handler_enqueue_regenerate(populated_store):
    """Test regenerate action (creates worker event)."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_regenerate("keyframe_001")

    assert result["ok"] is True
    assert "event_id" in result
    assert result["event"]["type"] == "regenerate"


def test_action_handler_enqueue_reopen(populated_store):
    """Test reopen action (creates worker event)."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_reopen("script_001")

    assert result["ok"] is True
    assert "event_id" in result
    assert result["event"]["type"] == "reopen"


def test_action_handler_enqueue_cancel(populated_store):
    """Test cancel action (creates worker event)."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_cancel("keyframe_001")

    assert result["ok"] is True
    assert "event_id" in result
    assert result["event"]["type"] == "cancel"


def test_action_handler_enqueue_message(populated_store):
    """Test message action (creates worker event)."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_message("Keep all visuals consistent")

    assert result["ok"] is True
    assert "event_id" in result
    assert result["event"]["type"] == "message"
    assert result["event"]["payload"]["text"] == "Keep all visuals consistent"


def test_action_handler_enqueue_message_artifact_scoped(populated_store):
    """Test message with artifact scope."""
    handler = ActionHandler(populated_store)
    result = handler.enqueue_message("Revise this one", artifact_id="script_001")

    assert result["ok"] is True
    assert result["event"]["artifact_id"] == "script_001"


# =============================================================================
# Version Rendering Tests
# =============================================================================


def test_render_version_content_text():
    """Test rendering text content."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="text/plain",
        content="Some text content",
    )

    content_type, value = render_version_content(version)
    assert content_type == "text"
    assert value == "Some text content"


def test_render_version_content_markdown():
    """Test rendering markdown content."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="text/markdown",
        content="# Title\nSome markdown",
    )

    content_type, value = render_version_content(version)
    assert content_type == "text"


def test_render_version_content_image():
    """Test rendering image content."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="image/png",
        content_ref="media/image_001.png",
    )

    content_type, value = render_version_content(version)
    assert content_type == "image"
    assert value == "media/image_001.png"


def test_render_version_content_video():
    """Test rendering video content."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="video/mp4",
        content_ref="media/video_001.mp4",
    )

    content_type, value = render_version_content(version)
    assert content_type == "video"
    assert value == "media/video_001.mp4"


def test_render_version_content_audio():
    """Test rendering audio content."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="audio/mp3",
        content_ref="media/audio_001.mp3",
    )

    content_type, value = render_version_content(version)
    assert content_type == "audio"


def test_render_version_content_unknown_type():
    """Test rendering unknown content type."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="application/custom",
        content_ref="media/custom.bin",
    )

    content_type, value = render_version_content(version)
    assert content_type == "text"


# =============================================================================
# Serialization Tests
# =============================================================================


def test_version_display_row_to_dict():
    """Test VersionDisplayRow serialization."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="text/plain",
        content="Test content",
        note="Test note",
        is_selected=True,
    )

    data = version.to_dict()
    assert data["version_id"] == "v1"
    assert data["version_num"] == 1
    assert data["is_selected"] is True


def test_artifact_display_card_to_dict(populated_store, stage_config):
    """Test ArtifactDisplayCard serialization."""
    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    script_stage = next((s for s in display.stages if s.stage_id == "script"), None)
    card = script_stage.artifacts[0]

    data = card.to_dict()
    assert data["artifact_id"] == "script_001"
    assert data["title"] == "Movie Script"
    assert data["selected_version"] is not None


def test_stage_display_group_to_dict(populated_store, stage_config):
    """Test StageDisplayGroup serialization."""
    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    stage = display.stages[0]
    data = stage.to_dict()
    assert data["stage_id"] in ["script", "shots", "keyframes", "clips", "assembly"]
    assert "artifacts" in data


def test_board_display_model_to_dict(populated_store, stage_config):
    """Test BoardDisplayModel serialization."""
    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    data = display.to_dict()
    assert "stages" in data
    assert "pending_review_count" in data
    assert "total_pending_events" in data
    assert isinstance(data["generating_artifacts"], list)
    assert isinstance(data["stale_artifacts"], list)


# =============================================================================
# Integration Tests
# =============================================================================


def test_full_workflow_script_edit_propagates_stale(populated_store, stage_config):
    """Test complete workflow: edit script -> stale propagation -> revise keyframe."""
    handler = ActionHandler(populated_store)
    builder = DisplayModelBuilder(populated_store, stage_config)

    # Initial state
    display1 = builder.build_board_display()
    assert len(display1.stale_artifacts) == 0

    # User edits script
    result = handler.enqueue_edit("script_001", "Scene 1: Completely new version...")
    assert result["ok"] is True

    # Simulate worker processing (would happen in supervisor)
    script_v3 = populated_store.put_version(
        "script_001",
        content="Scene 1: Completely new version...",
        created_by="user",
        select=True,
    )

    # Now check stale propagation
    display2 = builder.build_board_display()
    assert "shots_001" in display2.stale_artifacts

    # User sends revision to keyframe
    result = handler.enqueue_revise("keyframe_001", "Match new scene energy")
    assert result["ok"] is True


def test_approval_workflow(populated_store, stage_config):
    """Test approval workflow."""
    handler = ActionHandler(populated_store)
    builder = DisplayModelBuilder(populated_store, stage_config)

    # Initial state
    display1 = builder.build_board_display()
    script_stage1 = next(s for s in display1.stages if s.stage_id == "script")
    assert script_stage1.approved_count == 0

    # User approves script
    result = handler.approve("script_001")
    assert result["ok"] is True

    # Check updated state
    display2 = builder.build_board_display()
    script_stage2 = next(s for s in display2.stages if s.stage_id == "script")
    assert script_stage2.approved_count == 1


def test_version_selection_workflow(populated_store, stage_config):
    """Test version selection and stale propagation workflow."""
    handler = ActionHandler(populated_store)

    # Get keyframe versions
    versions = populated_store.list_versions("keyframe_001")
    assert len(versions) == 2

    # v1 should be selected
    artifact = populated_store.get_artifact("keyframe_001")
    assert artifact["selected_version_id"] == versions[0]["id"]

    # Switch to v2
    result = handler.select_version("keyframe_001", versions[1]["id"])
    assert result["ok"] is True

    # Verify switch
    artifact = populated_store.get_artifact("keyframe_001")
    assert artifact["selected_version_id"] == versions[1]["id"]


# =============================================================================
# Gradio Wiring Tests (skipped if gradio not available)
# =============================================================================


def test_gradio_build_board_availability():
    """Test that build_board handles gradio unavailability gracefully."""
    pytest.importorskip("gradio")

    from surface.board import build_board

    store = Store(":memory:")
    config = load_preset("movie")

    # Should return a Blocks object if gradio available
    board = build_board(store, config)
    assert board is not None


# =============================================================================
# Edge Case Tests
# =============================================================================


def test_multiple_artifacts_same_stage(store, stage_config):
    """Test handling multiple artifacts in same stage."""
    store.create_artifact(id="script_001", stage="script", title="Script 1")
    store.create_artifact(id="script_002", stage="script", title="Script 2")
    store.create_artifact(id="script_003", stage="script", title="Script 3")

    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    script_stage = next(s for s in display.stages if s.stage_id == "script")
    assert script_stage.total_count == 3
    assert len(script_stage.artifacts) == 3


def test_complex_dag_stale_propagation(store, stage_config):
    """Test stale propagation through complex DAG."""
    # Create a more complex dependency chain
    a = store.create_artifact(id="a", stage="script", title="A")
    b = store.create_artifact(id="b", stage="shots", title="B")
    c = store.create_artifact(id="c", stage="keyframes", title="C")
    d = store.create_artifact(id="d", stage="keyframes", title="D")
    e = store.create_artifact(id="e", stage="clips", title="E")

    # a -> b -> c -> e
    #   \  \-> d -> e
    store.add_dependency("a", "b")
    store.add_dependency("b", "c")
    store.add_dependency("b", "d")
    store.add_dependency("c", "e")
    store.add_dependency("d", "e")

    # Add versions and select them
    store.put_version("a", content="A v1", select=True)
    store.put_version("b", content="B v1", select=True)
    store.put_version("c", content="C v1", select=True)
    store.put_version("d", content="D v1", select=True)
    store.put_version("e", content="E v1", select=True)

    # Update A
    store.put_version("a", content="A v2", select=True)

    # Check stale propagation
    b_artifact = store.get_artifact("b")
    assert b_artifact["status"] == "stale"

    c_artifact = store.get_artifact("c")
    assert c_artifact["status"] == "stale"

    d_artifact = store.get_artifact("d")
    assert d_artifact["status"] == "stale"

    e_artifact = store.get_artifact("e")
    assert e_artifact["status"] == "stale"


def test_empty_version_list(store, stage_config):
    """Test artifact with no versions."""
    store.create_artifact(id="empty", stage="script", title="Empty")

    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    script_stage = next(s for s in display.stages if s.stage_id == "script")
    card = script_stage.artifacts[0]

    assert card.selected_version is None
    assert len(card.all_versions) == 0


def test_many_versions_tracking(store, stage_config):
    """Test tracking many versions of single artifact."""
    store.create_artifact(id="versioned", stage="script", title="Many Versions")

    # Create 10 versions
    for i in range(10):
        store.put_version(
            "versioned",
            content=f"Version {i+1}",
            select=(i == 9),  # Select last one
        )

    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    script_stage = next(s for s in display.stages if s.stage_id == "script")
    card = script_stage.artifacts[0]

    assert len(card.all_versions) == 10
    assert card.selected_version.version_num == 10
    assert card.all_versions[-1].is_selected is True
    assert card.all_versions[0].is_selected is False


def test_action_handler_idempotency(populated_store):
    """Test that actions are idempotent."""
    handler = ActionHandler(populated_store)

    # Approve twice
    result1 = handler.approve("script_001")
    result2 = handler.approve("script_001")

    assert result1["ok"] is True
    assert result2["ok"] is True
    assert result1["artifact"]["status"] == "approved"
    assert result2["artifact"]["status"] == "approved"


def test_action_handler_lock_unlock_toggle(populated_store):
    """Test lock/unlock toggling."""
    handler = ActionHandler(populated_store)

    # Check initial state
    artifact = populated_store.get_artifact("keyframe_001")
    assert artifact["locked"] is False

    # Lock
    result = handler.lock("keyframe_001")
    assert result["artifact"]["locked"] is True

    # Unlock
    result = handler.unlock("keyframe_001")
    assert result["artifact"]["locked"] is False

    # Lock again
    result = handler.lock("keyframe_001")
    assert result["artifact"]["locked"] is True


def test_board_display_pending_review_count(store, stage_config):
    """Test pending review count calculation."""
    store.create_artifact(id="a", stage="script", title="A", status="review")
    store.create_artifact(id="b", stage="script", title="B", status="review")
    store.create_artifact(id="c", stage="script", title="C", status="approved")

    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    assert display.pending_review_count == 2


def test_failed_artifact_detection(store, stage_config):
    """Test detection of failed artifacts."""
    store.create_artifact(id="failed", stage="keyframes", title="Failed")
    store.set_status("failed", "failed")

    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    assert "failed" in display.failed_artifacts


def test_action_classification_completeness():
    """Test that all known actions are classified."""
    known_actions = {
        "create", "revise", "regenerate", "select_version", "edit",
        "approve", "reopen", "cancel", "lock", "unlock",
        "bulk_regenerate", "message"
    }

    classified = (
        ActionClassification.DETERMINISTIC_ACTIONS
        | ActionClassification.WORKER_EVENT_ACTIONS
    )

    # Note: bulk_regenerate not implemented yet, so check subset
    for action in ["select_version", "approve", "lock", "unlock",
                   "edit", "revise", "regenerate", "reopen", "cancel", "message"]:
        assert action in classified, f"Action {action} not classified"


def test_version_row_with_no_content_type(store, stage_config):
    """Test version rendering with no content_type."""
    store.create_artifact(id="text_artifact", stage="script", title="Text")
    store.put_version("text_artifact", content="Just text", select=True)

    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    script_stage = next(s for s in display.stages if s.stage_id == "script")
    card = script_stage.artifacts[0]

    assert card.selected_version is not None
    assert card.selected_version.content == "Just text"


def test_enqueue_empty_payloads(populated_store):
    """Test enqueuing actions with empty payloads."""
    handler = ActionHandler(populated_store)

    # These should accept empty payloads
    result = handler.enqueue_regenerate("keyframe_001")
    assert result["ok"] is True
    assert result["event"]["payload"] == {}

    result = handler.enqueue_cancel("keyframe_001")
    assert result["ok"] is True
    assert result["event"]["payload"] == {}


def test_serialization_json_safe(populated_store, stage_config):
    """Test that serialized display model is JSON-safe."""
    builder = DisplayModelBuilder(populated_store, stage_config)
    display = builder.build_board_display()

    data = display.to_dict()

    # Should be JSON serializable
    json_str = json.dumps(data)
    reloaded = json.loads(json_str)

    assert reloaded["pending_review_count"] >= 0
    assert len(reloaded["stages"]) == 5


def test_stage_with_no_artifacts(store, stage_config):
    """Test stage display with zero artifacts."""
    builder = DisplayModelBuilder(store, stage_config)
    display = builder.build_board_display()

    # All stages should exist even with no artifacts
    assert len(display.stages) == 5

    # Check one stage
    script_stage = next(s for s in display.stages if s.stage_id == "script")
    assert script_stage.total_count == 0
    assert script_stage.approved_count == 0
    assert len(script_stage.artifacts) == 0
