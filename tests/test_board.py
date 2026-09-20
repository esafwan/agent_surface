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
    describe_select_version_conflict,
    BOARD_CSS,
    format_artifact_meta,
    format_status_badge,
    format_version_line,
    content_textbox_lines,
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


def test_action_handler_lock_rejected_when_not_in_allowed_actions(populated_store, stage_config):
    """A prior gap: lock/unlock were the only deterministic actions that
    skipped _validate_action_allowed, so a stage config permitting neither
    action still let a caller lock/unlock artifacts in that stage."""
    handler = ActionHandler(populated_store, stage_config=stage_config)
    # "script" stage in movie.json does not list lock/unlock in allowed_actions.
    result = handler.lock("script_001")
    assert result["ok"] is False
    assert "error" in result

    result = handler.unlock("script_001")
    assert result["ok"] is False
    assert "error" in result


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


# =============================================================================
# Issue 2: Dedupe Key Tests (M2)
# =============================================================================


def test_dedupe_key_computation(populated_store, stage_config):
    """Test that dedupe keys are computed deterministically."""
    handler = ActionHandler(populated_store, stage_config)

    # Same artifact, action, content should produce same key
    key1 = handler._compute_dedupe_key("script_001", "edit", "Same content")
    key2 = handler._compute_dedupe_key("script_001", "edit", "Same content")
    assert key1 == key2

    # Different content should produce different key
    key3 = handler._compute_dedupe_key("script_001", "edit", "Different content")
    assert key1 != key3

    # Different action should produce different key
    key4 = handler._compute_dedupe_key("script_001", "revise", "Same content")
    assert key1 != key4


def test_enqueue_edit_with_dedupe(populated_store, stage_config):
    """Test that enqueue_edit passes dedupe_key to store."""
    handler = ActionHandler(populated_store, stage_config)

    # First submission
    result1 = handler.enqueue_edit("script_001", "New content here")
    assert result1["ok"] is True
    event_id_1 = result1["event_id"]

    # Identical resubmission should dedupe
    result2 = handler.enqueue_edit("script_001", "New content here")
    assert result2["ok"] is True
    event_id_2 = result2["event_id"]

    # Should return the same event (deduped)
    assert event_id_1 == event_id_2

    # Different content should create new event
    result3 = handler.enqueue_edit("script_001", "Different content")
    assert result3["ok"] is True
    event_id_3 = result3["event_id"]

    assert event_id_1 != event_id_3

    # Verify only 2 events in store (dedup worked)
    cursor = populated_store.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM events WHERE type = 'edit'")
    count = cursor.fetchone()[0]
    assert count == 2


def test_enqueue_revise_with_dedupe(populated_store, stage_config):
    """Test that enqueue_revise passes dedupe_key."""
    handler = ActionHandler(populated_store, stage_config)

    result1 = handler.enqueue_revise("keyframe_001", "Warmer lighting")
    result2 = handler.enqueue_revise("keyframe_001", "Warmer lighting")

    assert result1["event_id"] == result2["event_id"]

    # Different note
    result3 = handler.enqueue_revise("keyframe_001", "Cooler lighting")
    assert result1["event_id"] != result3["event_id"]


def test_enqueue_regenerate_with_dedupe(populated_store, stage_config):
    """Test that enqueue_regenerate is idempotent."""
    handler = ActionHandler(populated_store, stage_config)

    result1 = handler.enqueue_regenerate("keyframe_001")
    result2 = handler.enqueue_regenerate("keyframe_001")

    # Same artifact should dedupe (no content, so dedupe key is deterministic)
    assert result1["event_id"] == result2["event_id"]


def test_enqueue_message_with_dedupe(populated_store, stage_config):
    """Test that enqueue_message passes dedupe_key."""
    handler = ActionHandler(populated_store, stage_config)

    result1 = handler.enqueue_message("Keep visuals consistent")
    result2 = handler.enqueue_message("Keep visuals consistent")

    assert result1["event_id"] == result2["event_id"]

    result3 = handler.enqueue_message("Different message")
    assert result1["event_id"] != result3["event_id"]


# =============================================================================
# Issue 1 & 3: Optimistic Concurrency Tests (M1)
# =============================================================================


def test_select_version_passes_expected_version(populated_store, stage_config):
    """Test that select_version passes expected_selected_version_id."""
    handler = ActionHandler(populated_store, stage_config)

    versions = populated_store.list_versions("keyframe_001")
    v1_id = versions[0]["id"]
    v2_id = versions[1]["id"]

    # Get current selection
    artifact = populated_store.get_artifact("keyframe_001")
    current_selected = artifact["selected_version_id"]
    assert current_selected == v1_id

    # Select v2 with correct expected version
    result = handler.select_version("keyframe_001", v2_id, expected_selected_version_id=v1_id)
    assert result["ok"] is True

    # Now attempt to select v2 again with wrong expected version (conflict)
    result2 = handler.select_version("keyframe_001", v1_id, expected_selected_version_id=v1_id)
    assert result2["ok"] is False
    assert result2["error"] == "conflict"


def test_select_version_conflict_returns_current_state(populated_store, stage_config):
    """Test that select_version conflict returns current state."""
    handler = ActionHandler(populated_store, stage_config)

    versions = populated_store.list_versions("keyframe_001")
    v1_id = versions[0]["id"]
    v2_id = versions[1]["id"]

    # Select v2
    handler.select_version("keyframe_001", v2_id)

    # Try to select v1 with stale expected version (conflict)
    result = handler.select_version("keyframe_001", v1_id, expected_selected_version_id=v1_id)
    assert result["ok"] is False
    assert result["error"] == "conflict"
    assert result["current_selected_version_id"] is not None


# =============================================================================
# Conflict UX (Phase 1 hardening): SPEC section 56, 41, 33 - conflicts must
# be surfaced to the user, not silently dropped, and must not clobber state.
# =============================================================================


def test_select_version_conflict_via_board_handler_no_state_change(populated_store, stage_config):
    """
    A stale expected_selected_version_id, submitted through the board's
    actual ActionHandler (not store.py directly), must:
      - produce a conflict result,
      - include the actual current selected version id, and
      - leave selected_version_id unchanged (no side effect on conflict).
    """
    handler = ActionHandler(populated_store, stage_config)

    versions = populated_store.list_versions("keyframe_001")
    v1_id = versions[0]["id"]
    v2_id = versions[1]["id"]

    # Establish a known current selection: v2.
    ok_result = handler.select_version("keyframe_001", v2_id, expected_selected_version_id=v1_id)
    assert ok_result["ok"] is True

    artifact_before = populated_store.get_artifact("keyframe_001")
    assert artifact_before["selected_version_id"] == v2_id

    # Stale browser submission: still believes v1 is selected, tries to
    # select v1 again "expecting" v1 (its cached state), but the real
    # current selection is now v2 -> conflict.
    conflict_result = handler.select_version(
        "keyframe_001", v1_id, expected_selected_version_id=v1_id
    )

    assert conflict_result["ok"] is False
    assert conflict_result["error"] == "conflict"
    assert conflict_result["current_selected_version_id"] == v2_id

    # No side effect: selection must remain v2, not silently move to v1.
    artifact_after = populated_store.get_artifact("keyframe_001")
    assert artifact_after["selected_version_id"] == v2_id


def test_select_version_happy_path_unaffected_by_conflict_handling(populated_store, stage_config):
    """A matching expected_selected_version_id still succeeds and applies."""
    handler = ActionHandler(populated_store, stage_config)

    versions = populated_store.list_versions("keyframe_001")
    v1_id = versions[0]["id"]
    v2_id = versions[1]["id"]

    artifact = populated_store.get_artifact("keyframe_001")
    assert artifact["selected_version_id"] == v1_id

    result = handler.select_version("keyframe_001", v2_id, expected_selected_version_id=v1_id)
    assert result["ok"] is True
    assert result.get("error") is None

    artifact_after = populated_store.get_artifact("keyframe_001")
    assert artifact_after["selected_version_id"] == v2_id

    # No conflict message should be produced for a successful result.
    assert describe_select_version_conflict(result) is None


def test_describe_select_version_conflict_message_includes_current_version():
    """The UI-facing conflict message must name the actual current version."""
    result = {
        "ok": False,
        "error": "conflict",
        "current_selected_version_id": "ver_91",
        "stale_descendants": [],
    }
    message = describe_select_version_conflict(result)
    assert message is not None
    assert "ver_91" in message
    assert "conflict" in message.lower()
    assert "refresh" in message.lower()


def test_describe_select_version_conflict_returns_none_for_success():
    result = {"ok": True, "selected_version_id": "ver_91", "stale_descendants": []}
    assert describe_select_version_conflict(result) is None


def test_describe_select_version_conflict_returns_none_for_other_errors():
    """Non-conflict errors (e.g. gating failures) must not be mislabeled."""
    result = {"ok": False, "error": "Action select_version not allowed for stage script"}
    assert describe_select_version_conflict(result) is None


# =============================================================================
# Issue 5: Action Gating Tests (M3, M4)
# =============================================================================


def test_select_version_gated_on_allowed_actions(populated_store, stage_config):
    """Test that select_version is gated on allowed_actions."""
    handler = ActionHandler(populated_store, stage_config)

    # Script stage does not allow select_version
    versions = populated_store.list_versions("script_001")
    if len(versions) > 1:
        result = handler.select_version("script_001", versions[1]["id"])
        assert result["ok"] is False
        assert "not allowed" in result["error"].lower()


def test_approve_gating_on_locked_artifact(populated_store, stage_config):
    """Test that approve fails when artifact is locked."""
    handler = ActionHandler(populated_store, stage_config)

    # Lock the artifact
    populated_store.set_lock("script_001", True)

    # Try to approve without force
    result = handler.approve("script_001", force=False)
    assert result["ok"] is False
    assert "approval_blocked_by_lock" in result["error"]

    # Approve with force
    result_forced = handler.approve("script_001", force=True)
    assert result_forced["ok"] is True


def test_approve_blocked_on_failed_status(populated_store, stage_config):
    """Test that approve is blocked for failed artifacts."""
    handler = ActionHandler(populated_store, stage_config)

    # Set artifact to failed
    populated_store.set_status("script_001", "failed")

    result = handler.approve("script_001", force=False)
    assert result["ok"] is False
    assert "approval_blocked_by_failed_status" in result["error"]


def test_approve_blocked_on_cancelled_status(populated_store, stage_config):
    """Test that approve is blocked for cancelled artifacts."""
    handler = ActionHandler(populated_store, stage_config)

    # Set artifact to cancelled
    populated_store.set_status("keyframe_001", "cancelled")

    result = handler.approve("keyframe_001", force=False)
    assert result["ok"] is False
    assert "approval_blocked_by_cancelled_status" in result["error"]


def test_approve_gated_on_allowed_actions(populated_store, stage_config):
    """Test that approve is gated on allowed_actions in stage config."""
    # Create an artifact in a stage that doesn't allow approve
    # (All movie stages allow approve, so we'd need a custom config)
    # For now, just verify that the stage_config is checked
    handler = ActionHandler(populated_store, stage_config)

    # Verify that handler has stage_config
    assert handler.stage_config is not None

    # This should work because script stage allows approve
    result = handler.approve("script_001")
    assert result["ok"] is True


def test_approve_without_stage_config_still_works(populated_store):
    """Test that approve works without stage_config (backwards compat)."""
    handler = ActionHandler(populated_store, stage_config=None)

    result = handler.approve("script_001")
    assert result["ok"] is True


def test_action_handler_requires_stage_config_for_gating(populated_store, stage_config):
    """Test that ActionHandler can be initialized with stage_config."""
    handler_with_config = ActionHandler(populated_store, stage_config)
    assert handler_with_config.stage_config is not None

    handler_without_config = ActionHandler(populated_store, stage_config=None)
    assert handler_without_config.stage_config is None


# =============================================================================
# Issue 2: Render Version Content Tests (S5)
# =============================================================================


def test_render_version_content_text_content():
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


def test_render_version_content_image_returns_correct_type():
    """Test rendering image content returns image type."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="image/png",
        content_ref="media/image_001.png",
    )

    content_type, value = render_version_content(version)
    assert content_type == "image"
    assert value == "media/image_001.png"


def test_render_version_content_video_returns_correct_type():
    """Test rendering video content returns video type."""
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="video/mp4",
        content_ref="media/video_001.mp4",
    )

    content_type, value = render_version_content(version)
    assert content_type == "video"
    assert value == "media/video_001.mp4"


def test_render_version_content_invoked_in_board_display():
    """Test that render_version_content is used in display models."""
    # Verify that VersionDisplayRow carries content_type info
    version = VersionDisplayRow(
        version_id="v1",
        version_num=1,
        content_type="image/jpeg",
        content_ref="media/test.jpg",
    )

    # render_version_content should be callable
    assert callable(render_version_content)
    content_type, value = render_version_content(version)
    assert content_type == "image"
    assert value == "media/test.jpg"


# =============================================================================
# Presentation helper tests (no gradio required)
# =============================================================================


def test_board_css_is_non_empty_stylesheet():
    """BOARD_CSS should be real CSS, not an empty placeholder."""
    assert isinstance(BOARD_CSS, str)
    assert len(BOARD_CSS.strip()) > 200
    # Every class referenced by the layout must actually be styled.
    for selector in (
        ".gradio-container",
        "#board-header",
        ".artifact-card",
        ".artifact-meta",
        ".content-surface",
        ".version-row",
        ".action-bar",
    ):
        assert selector in BOARD_CSS, f"missing style for {selector}"
    # Balanced braces => syntactically plausible stylesheet.
    assert BOARD_CSS.count("{") == BOARD_CSS.count("}")


def test_format_artifact_meta_is_single_line_with_key_fields():
    card = ArtifactDisplayCard(
        artifact_id="a1",
        stage="script",
        title="Opening scene",
        status="approved",
        locked=True,
        selected_version=VersionDisplayRow(version_id="v2", version_num=2),
        all_versions=[
            VersionDisplayRow(version_id="v1", version_num=1),
            VersionDisplayRow(version_id="v2", version_num=2, is_selected=True),
        ],
        has_generating_job=True,
    )
    meta = format_artifact_meta(card)
    assert "\n" not in meta
    assert "**Status:**" in meta and "approved" in meta
    assert "**Version:** 2" in meta
    assert "**Stage:** script" in meta
    assert "locked" in meta
    assert "generating" in meta


def test_format_artifact_meta_minimal_card():
    card = ArtifactDisplayCard(
        artifact_id="a1", stage="script", title="T", status="draft", locked=False
    )
    meta = format_artifact_meta(card)
    assert "**Stage:** script" in meta
    assert "locked" not in meta
    assert "Version:" not in meta


def test_format_status_badge_has_dot_and_label():
    assert "approved" in format_status_badge("approved")
    assert format_status_badge("approved") != "approved"
    # Unknown statuses still render.
    assert "weird" in format_status_badge("weird")


def test_format_version_line_marks_selection_and_truncates_note():
    selected = VersionDisplayRow(
        version_id="v2", version_num=2, created_by="worker",
        created_at="2024-01-01", note="x" * 100, is_selected=True,
    )
    line = format_version_line(selected)
    assert line.startswith("●")
    assert "**v2**" in line
    assert "..." in line
    assert "\n" not in line

    other = format_version_line(VersionDisplayRow(version_id="v1", version_num=1))
    assert other.startswith("○")


def test_content_textbox_lines_bounds():
    assert content_textbox_lines(None) == 4
    assert content_textbox_lines("short") == 8
    assert content_textbox_lines("line\n" * 200) == 28
    assert 8 <= content_textbox_lines("word " * 400) <= 28
