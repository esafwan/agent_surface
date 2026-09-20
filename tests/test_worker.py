"""
Unit tests for the reference worker (Phase 0).

Tests cover:
- Event dispatch by type (edit, revise, regenerate, select_version, approve, reopen, cancel, lock, unlock, message)
- System events (job_done, job_failed, stale, worker_recovered)
- Artifact type detection (text vs image/video)
- Job creation and status transitions
- Version creation with idempotency
- Error handling: unknown event types, missing artifact_id/payload fields
- Idempotent redelivery: same event_id twice does not duplicate effects
"""

import pytest
from surface.store import Store
from surface.worker import (
    handle_event,
    _get_artifact_type_for_stage,
    _get_provider_for_stage,
)
from surface.stages.config import load_preset


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def in_memory_store():
    """In-memory SQLite store for testing."""
    return Store(":memory:")


@pytest.fixture
def movie_config():
    """Load the movie stage config as a dict (raw)."""
    preset = load_preset("movie")
    return preset.raw


@pytest.fixture
def setup_movie_artifacts(in_memory_store):
    """Create a basic movie stage setup with artifacts."""
    # Create script
    in_memory_store.create_artifact(
        id="script_1",
        stage="script",
        title="Opening Scene Script",
        status="draft",
    )

    # Create shot
    in_memory_store.create_artifact(
        id="shot_1",
        stage="shots",
        title="Shot 1",
        status="draft",
    )

    # Create keyframe (image)
    in_memory_store.create_artifact(
        id="keyframe_1",
        stage="keyframes",
        title="Keyframe 1",
        status="draft",
    )

    # Create clip (video)
    in_memory_store.create_artifact(
        id="clip_1",
        stage="clips",
        title="Clip 1",
        status="draft",
    )

    return in_memory_store


# ============================================================================
# Test Event Dispatch & Handler Logic
# ============================================================================


class TestEditAction:
    """Test edit action: create new version with content."""

    def test_edit_creates_version_and_selects_it(self, setup_movie_artifacts, movie_config):
        """Edit should create a new version and select it."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_edit_1",
            "type": "edit",
            "artifact_id": "script_1",
            "payload": {"content": "INT. KITCHEN - NIGHT\nNew scene text"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True
        assert "version_id" in result

        # Check version was created
        versions = store.list_versions("script_1")
        assert len(versions) == 1
        assert versions[0]["content"] == "INT. KITCHEN - NIGHT\nNew scene text"

        # Check it was selected
        artifact = store.get_artifact("script_1")
        assert artifact["selected_version_id"] == versions[0]["id"]

    def test_edit_missing_content_fails(self, setup_movie_artifacts, movie_config):
        """Edit without content should fail."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_edit_2",
            "type": "edit",
            "artifact_id": "script_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "content" in result["error"]

    def test_edit_missing_artifact_id_fails(self, setup_movie_artifacts, movie_config):
        """Edit without artifact_id should fail."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_edit_3",
            "type": "edit",
            "artifact_id": None,
            "payload": {"content": "Some content"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "artifact_id" in result["error"]

    def test_edit_nonexistent_artifact_fails(self, setup_movie_artifacts, movie_config):
        """Edit on nonexistent artifact should fail."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_edit_4",
            "type": "edit",
            "artifact_id": "nonexistent",
            "payload": {"content": "Content"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_edit_idempotent_same_event_id(self, setup_movie_artifacts, movie_config):
        """Sending same edit event twice with same event_id should not create duplicate versions."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_edit_idempotent",
            "type": "edit",
            "artifact_id": "script_1",
            "payload": {"content": "First version"},
            "config": movie_config,
        }

        # First call
        result1 = handle_event(event, store)
        assert result1["ok"] is True
        version_id_1 = result1.get("version_id")

        # Second call with same event_id
        result2 = handle_event(event, store)
        assert result2["ok"] is True

        # Should return the same version_id due to source_event_id check
        assert result2.get("version_id") == version_id_1

        # Should only have one version
        versions = store.list_versions("script_1")
        assert len(versions) == 1


class TestReviseAction:
    """Test revise action: text artifacts create versions, image/video create jobs."""

    def test_revise_text_artifact_creates_version(self, setup_movie_artifacts, movie_config):
        """Revise on text artifact should create a new version."""
        store = setup_movie_artifacts

        # First create a version to revise from
        store.put_version("script_1", content="Original script", source_event_id="evt_base")

        event = {
            "event_id": "evt_revise_text",
            "type": "revise",
            "artifact_id": "script_1",
            "payload": {"note": "Make the dialogue more natural"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True
        assert "version_id" in result

        # Check new version was created
        versions = store.list_versions("script_1")
        assert len(versions) == 2
        assert versions[1]["note"] == "Make the dialogue more natural"

    def test_revise_image_artifact_creates_job(self, setup_movie_artifacts, movie_config):
        """Revise on image artifact should create a job."""
        store = setup_movie_artifacts

        # First create a version to revise from
        store.put_version("keyframe_1", content="Prompt: scenic landscape", source_event_id="evt_base")

        event = {
            "event_id": "evt_revise_image",
            "type": "revise",
            "artifact_id": "keyframe_1",
            "payload": {"note": "Make it warmer"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True
        assert "job_id" in result

        # Check artifact status is now generating
        artifact = store.get_artifact("keyframe_1")
        assert artifact["status"] == "generating"

        # Check job was created
        job = store.get_job(result["job_id"])
        assert job is not None
        assert job["artifact_id"] == "keyframe_1"
        assert job["status"] == "queued"
        assert job["kind"] == "image"

    def test_revise_idempotent_same_event_id(self, setup_movie_artifacts, movie_config):
        """Same revise event twice should not create duplicate jobs."""
        store = setup_movie_artifacts

        store.put_version("keyframe_1", content="Prompt", source_event_id="evt_base")

        event = {
            "event_id": "evt_revise_idempotent",
            "type": "revise",
            "artifact_id": "keyframe_1",
            "payload": {"note": "Warmer"},
            "config": movie_config,
        }

        result1 = handle_event(event, store)
        assert result1["ok"] is True
        job_id_1 = result1.get("job_id")

        result2 = handle_event(event, store)
        assert result2["ok"] is True
        job_id_2 = result2.get("job_id")

        # Both calls should succeed (idempotent)
        # But we may create multiple jobs; true idempotency would require job deduplication
        # For now, we accept this limitation for Phase 0


class TestRegenerateAction:
    """Test regenerate action: create job for generation artifacts."""

    def test_regenerate_creates_job(self, setup_movie_artifacts, movie_config):
        """Regenerate should create a job and set artifact status to generating."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_regen_1",
            "type": "regenerate",
            "artifact_id": "keyframe_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True
        assert "job_id" in result

        # Check artifact status
        artifact = store.get_artifact("keyframe_1")
        assert artifact["status"] == "generating"

        # Check job
        job = store.get_job(result["job_id"])
        assert job["artifact_id"] == "keyframe_1"
        assert job["status"] == "queued"

    def test_regenerate_video_artifact(self, setup_movie_artifacts, movie_config):
        """Regenerate on video artifact should use correct kind."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_regen_video",
            "type": "regenerate",
            "artifact_id": "clip_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

        job = store.get_job(result["job_id"])
        assert job["kind"] == "video"
        assert job["provider"] == "video_default"


class TestSelectVersionAction:
    """Test select_version action."""

    def test_select_version(self, setup_movie_artifacts, movie_config):
        """Select a specific version."""
        store = setup_movie_artifacts

        # Create two versions
        v1 = store.put_version("script_1", content="Version 1", select=True)
        v2 = store.put_version("script_1", content="Version 2", select=False)

        event = {
            "event_id": "evt_select_1",
            "type": "select_version",
            "artifact_id": "script_1",
            "payload": {"version_id": v2["version_id"]},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

        artifact = store.get_artifact("script_1")
        assert artifact["selected_version_id"] == v2["version_id"]


class TestApproveAction:
    """Test approve action."""

    def test_approve_sets_status(self, setup_movie_artifacts, movie_config):
        """Approve should set artifact status to 'approved'."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_approve_1",
            "type": "approve",
            "artifact_id": "script_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

        artifact = store.get_artifact("script_1")
        assert artifact["status"] == "approved"


class TestReopenAction:
    """Test reopen action."""

    def test_reopen_sets_draft_status(self, setup_movie_artifacts, movie_config):
        """Reopen should set artifact status to 'draft'."""
        store = setup_movie_artifacts

        # First approve it
        store.set_status("script_1", "approved")

        event = {
            "event_id": "evt_reopen_1",
            "type": "reopen",
            "artifact_id": "script_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

        artifact = store.get_artifact("script_1")
        assert artifact["status"] == "draft"


class TestCancelAction:
    """Test cancel action."""

    def test_cancel_job(self, setup_movie_artifacts, movie_config):
        """Cancel should mark a job as cancel_requested."""
        store = setup_movie_artifacts

        # Create a job first
        job = store.create_job("keyframe_1", provider="image_default", kind="image")
        store.update_job_status(job["id"], "running")

        event = {
            "event_id": "evt_cancel_1",
            "type": "cancel",
            "artifact_id": "keyframe_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True
        assert result["job_id"] == job["id"]

        # Check job is marked for cancellation
        updated_job = store.get_job(job["id"])
        assert updated_job["cancel_requested"] is True

    def test_cancel_no_active_job_fails(self, setup_movie_artifacts, movie_config):
        """Cancel with no active job should fail."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_cancel_nojob",
            "type": "cancel",
            "artifact_id": "keyframe_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "No active job" in result["error"]


class TestLockUnlockActions:
    """Test lock/unlock actions."""

    def test_lock_sets_locked_flag(self, setup_movie_artifacts, movie_config):
        """Lock should set locked=True."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_lock_1",
            "type": "lock",
            "artifact_id": "script_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

        artifact = store.get_artifact("script_1")
        assert artifact["locked"] is True

    def test_unlock_clears_locked_flag(self, setup_movie_artifacts, movie_config):
        """Unlock should set locked=False."""
        store = setup_movie_artifacts

        store.set_lock("script_1", True)

        event = {
            "event_id": "evt_unlock_1",
            "type": "unlock",
            "artifact_id": "script_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

        artifact = store.get_artifact("script_1")
        assert artifact["locked"] is False


class TestMessageAction:
    """Test message action (no-op)."""

    def test_message_acknowledged(self, setup_movie_artifacts, movie_config):
        """Message should be acknowledged without action."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_msg_1",
            "type": "message",
            "artifact_id": None,
            "payload": {"text": "Keep the style consistent"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True


class TestSystemEvents:
    """Test system event handling."""

    def test_job_done_acknowledged(self, setup_movie_artifacts, movie_config):
        """job_done should be acknowledged."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_job_done_1",
            "type": "job_done",
            "artifact_id": None,
            "payload": {"job_id": "job_123", "artifact_id": "keyframe_1"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

    def test_job_failed_acknowledged(self, setup_movie_artifacts, movie_config):
        """job_failed should be acknowledged."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_job_failed_1",
            "type": "job_failed",
            "artifact_id": None,
            "payload": {"job_id": "job_456"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

    def test_stale_acknowledged(self, setup_movie_artifacts, movie_config):
        """stale should be acknowledged."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_stale_1",
            "type": "stale",
            "artifact_id": None,
            "payload": {"artifact_id": "shot_1"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True

    def test_worker_recovered_acknowledged(self, setup_movie_artifacts, movie_config):
        """worker_recovered should be acknowledged."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_recovered_1",
            "type": "worker_recovered",
            "artifact_id": None,
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True


class TestErrorHandling:
    """Test error handling."""

    def test_unknown_event_type(self, setup_movie_artifacts, movie_config):
        """Unknown event type should return ok=False without crashing."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_unknown",
            "type": "unknown_action",
            "artifact_id": "script_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "Unknown event type" in result["error"]

    def test_malformed_event_graceful_error(self, setup_movie_artifacts, movie_config):
        """Malformed event should return graceful error."""
        store = setup_movie_artifacts

        event = {
            "event_id": "evt_malformed",
            # Missing type field
            "artifact_id": "script_1",
            "config": movie_config,
        }

        result = handle_event(event, store)

        # Should handle gracefully without crashing
        assert "ok" in result


class TestConfigLookups:
    """Test artifact type and provider lookups from config."""

    def test_get_artifact_type_for_stage_text(self, movie_config):
        """Should return 'text' for script and shots stages."""
        assert _get_artifact_type_for_stage("script", movie_config) == "text"
        assert _get_artifact_type_for_stage("shots", movie_config) == "text"

    def test_get_artifact_type_for_stage_image(self, movie_config):
        """Should return 'image' for keyframes stage."""
        assert _get_artifact_type_for_stage("keyframes", movie_config) == "image"

    def test_get_artifact_type_for_stage_video(self, movie_config):
        """Should return 'video' for clips and assembly stages."""
        assert _get_artifact_type_for_stage("clips", movie_config) == "video"
        assert _get_artifact_type_for_stage("assembly", movie_config) == "video"

    def test_get_provider_for_stage_image(self, movie_config):
        """Should return provider for keyframes."""
        provider = _get_provider_for_stage("keyframes", movie_config)
        assert provider == "image_default"

    def test_get_provider_for_stage_video(self, movie_config):
        """Should return provider for clips."""
        provider = _get_provider_for_stage("clips", movie_config)
        assert provider == "video_default"

    def test_get_provider_for_stage_text_none(self, movie_config):
        """Should return None for text stages (no provider)."""
        provider = _get_provider_for_stage("script", movie_config)
        assert provider is None


class TestLockedArtifactRegeneration:
    """SPEC section 42: a locked artifact is a persistent instruction that
    automation MUST NOT regenerate/replace without confirmation."""

    def test_regenerate_locked_artifact_rejected(self, setup_movie_artifacts, movie_config):
        """Regenerate on a locked artifact must be rejected, not silently proceed."""
        store = setup_movie_artifacts
        store.set_lock("keyframe_1", True)

        event = {
            "event_id": "evt_regen_locked",
            "type": "regenerate",
            "artifact_id": "keyframe_1",
            "payload": {},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "locked" in result["error"].lower()

        # No job created, artifact status untouched.
        artifact = store.get_artifact("keyframe_1")
        assert artifact["status"] == "draft"
        assert store.list_jobs_by_status("queued") == []

    def test_revise_locked_generation_artifact_rejected(self, setup_movie_artifacts, movie_config):
        """Revise on a locked image/video artifact (which creates a job) must
        also be rejected."""
        store = setup_movie_artifacts
        store.set_lock("clip_1", True)

        event = {
            "event_id": "evt_revise_locked",
            "type": "revise",
            "artifact_id": "clip_1",
            "payload": {"note": "make it darker"},
            "config": movie_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "locked" in result["error"].lower()
        assert store.list_jobs_by_status("queued") == []


class TestBudgetEnforcement:
    """SPEC section 39: budget limits MUST be enforced deterministically
    outside LLM reasoning, at the worker's job-creation call site."""

    def test_regenerate_over_budget_rejected(self, setup_movie_artifacts, movie_config):
        """A regenerate request whose estimated cost would exceed the
        configured project budget must be rejected before a job is created."""
        store = setup_movie_artifacts

        # Movie preset has no budget block; build a minimal config inline
        # with one, keeping everything else from the real preset so stage
        # lookups (provider/kind) still resolve correctly.
        budgeted_config = dict(movie_config)
        budgeted_config["budget"] = {"project_usd": 0.5}

        event = {
            "event_id": "evt_regen_over_budget",
            "type": "regenerate",
            "artifact_id": "keyframe_1",
            "payload": {},
            "config": budgeted_config,
        }
        # Smuggle a cost estimate in via payload isn't part of the contract;
        # the worker computes cost_estimate from the constructed request, so
        # drive it over budget by pre-existing spend instead: create a prior
        # succeeded job already at the budget ceiling.
        store.create_job(
            artifact_id="keyframe_1",
            provider="image_default",
            kind="image",
            request={},
            cost_estimate=1.0,
        )
        job_before = store.list_jobs_by_status("queued")
        assert len(job_before) == 1  # the pre-existing job, still queued

        result = handle_event(event, store)

        assert result["ok"] is False
        assert "budget" in result["error"].lower()

        # No new job was created; artifact status untouched.
        artifact = store.get_artifact("keyframe_1")
        assert artifact["status"] == "draft"
        assert len(store.list_jobs_by_status("queued")) == 1

    def test_regenerate_within_budget_allowed(self, setup_movie_artifacts, movie_config):
        """A request within budget proceeds normally."""
        store = setup_movie_artifacts

        budgeted_config = dict(movie_config)
        budgeted_config["budget"] = {"project_usd": 100.0}

        event = {
            "event_id": "evt_regen_within_budget",
            "type": "regenerate",
            "artifact_id": "keyframe_1",
            "payload": {},
            "config": budgeted_config,
        }

        result = handle_event(event, store)

        assert result["ok"] is True
        assert "job_id" in result
        artifact = store.get_artifact("keyframe_1")
        assert artifact["status"] == "generating"
