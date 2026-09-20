"""Integration test for Phase 0 movie walkthrough.

Per SPEC section 64 (Recommended First Build), this test executes the complete
movie pipeline sequence end-to-end:
  1. edit script -> stale shots/keyframes/clips/assembly
  2. approve shots
  3. async images (jobs + poller)
  4. revise one image (while others independent)
  5. generate clips (jobs + poller)
  6. cancel one clip mid-flight (late success should not create version)
  7. kill worker (simulate crash) -> recover (lease expiry/reclaim)
  8. approve remaining clips
  9. assemble (final artifact)
  10. finish (verify state: approved stages, no version deletes, stale artifacts retained)

Uses real temp SQLite store, MockImageProvider/MockVideoProvider, Poller,
and a simple fake worker (functions that simulate worker behavior by directly
calling store API).
"""

import json
import pytest
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from surface.store import Store
from surface.poller import Poller
from surface.providers.image import MockImageProvider
from surface.providers.video import MockVideoProvider
from surface.stages.config import load_preset


class TestMovieWalkthrough:
    """Full movie walkthrough integration test."""

    @pytest.fixture
    def temp_db(self):
        """Temporary SQLite database for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        # Cleanup is done in test teardown if needed
        Path(db_path).unlink(missing_ok=True)

    @pytest.fixture
    def store(self, temp_db):
        """Store instance for the test."""
        return Store(temp_db)

    @pytest.fixture
    def movie_config(self):
        """Load the movie stage config."""
        return load_preset("movie")

    @pytest.fixture
    def image_provider(self):
        """Mock image provider with 2 polls to success."""
        return MockImageProvider(polls_to_success=2)

    @pytest.fixture
    def video_provider(self):
        """Mock video provider with 3 polls to success."""
        return MockVideoProvider(polls_to_success=3)

    @pytest.fixture
    def poller(self, store, image_provider, video_provider):
        """Poller with both image and video providers."""
        return Poller(store, {
            "image_default": image_provider,
            "video_default": video_provider,
        })

    def _create_stage_artifacts(self, store: Store):
        """Helper: create one artifact per stage (not shot/keyframe/clip sets, just one per stage for now)."""
        # Script: starting artifact
        script = store.create_artifact(
            id="script_001",
            stage="script",
            title="Script - 30 second product film",
            status="draft",
        )
        # Shots: depends on script
        shots = store.create_artifact(
            id="shots_001",
            stage="shots",
            title="Shot List",
            status="draft",
        )
        store.add_dependency("script_001", "shots_001")

        # Keyframes: depends on shots
        keyframes = store.create_artifact(
            id="keyframes_001",
            stage="keyframes",
            title="Keyframe Set 1",
            status="draft",
        )
        store.add_dependency("shots_001", "keyframes_001")

        # Additional keyframe for revision test
        keyframes_2 = store.create_artifact(
            id="keyframes_002",
            stage="keyframes",
            title="Keyframe Set 2",
            status="draft",
        )
        store.add_dependency("shots_001", "keyframes_002")

        # Clips: depends on keyframes
        clips = store.create_artifact(
            id="clips_001",
            stage="clips",
            title="Clip 1",
            status="draft",
        )
        store.add_dependency("keyframes_001", "clips_001")

        clips_2 = store.create_artifact(
            id="clips_002",
            stage="clips",
            title="Clip 2",
            status="draft",
        )
        store.add_dependency("keyframes_002", "clips_002")

        # Assembly: depends on clips
        assembly = store.create_artifact(
            id="assembly_001",
            stage="assembly",
            title="Final Assembly",
            status="draft",
        )
        store.add_dependency("clips_001", "assembly_001")
        store.add_dependency("clips_002", "assembly_001")

        return {
            "script": script,
            "shots": shots,
            "keyframes": keyframes,
            "keyframes_2": keyframes_2,
            "clips": clips,
            "clips_2": clips_2,
            "assembly": assembly,
        }

    def test_full_movie_walkthrough(self, store, movie_config, poller):
        """Full Phase 0 movie walkthrough: edit → approve → generate → cancel → recover → approve → assemble."""

        # ========================================================================
        # STEP 1: Create stage artifacts and verify DAG structure
        # ========================================================================
        artifacts = self._create_stage_artifacts(store)

        # All should start as draft
        for key, art in artifacts.items():
            assert art["status"] == "draft", f"{key} should start as draft"
            assert art["selected_version_id"] is None

        # ========================================================================
        # STEP 2: Edit script -> creates new version, marks dependents stale
        # ========================================================================
        script_v1 = store.put_version(
            "script_001",
            content="INT. KITCHEN - NIGHT. Camera on product.",
            created_by="worker",
            note="Initial script",
            select=True,
        )
        assert script_v1["ok"] is True
        assert script_v1["version"] == 1
        # Select should trigger stale propagation
        assert set(script_v1["stale_descendants"]) == {
            "shots_001", "keyframes_001", "keyframes_002", "clips_001", "clips_002", "assembly_001"
        }

        # Verify dependents are now stale
        assert store.get_artifact("shots_001")["status"] == "stale"
        assert store.get_artifact("keyframes_001")["status"] == "stale"
        assert store.get_artifact("keyframes_002")["status"] == "stale"
        assert store.get_artifact("clips_001")["status"] == "stale"
        assert store.get_artifact("clips_002")["status"] == "stale"
        assert store.get_artifact("assembly_001")["status"] == "stale"

        # Verify no versions were deleted (spec: Stale, never destroy)
        script_versions = store.list_versions("script_001")
        assert len(script_versions) == 1

        # Approve the script
        store.set_status("script_001", "approved")

        # ========================================================================
        # STEP 3: Edit shots, approve them
        # ========================================================================
        shots_v1 = store.put_version(
            "shots_001",
            content="Scene 1: Wide. Scene 2: Close-up.",
            created_by="worker",
            note="Shotlist from script",
            select=True,
        )
        assert shots_v1["ok"] is True

        # Approve shots (mark as approved, clearing stale)
        store.set_status("shots_001", "approved")
        assert store.get_artifact("shots_001")["status"] == "approved"

        # ========================================================================
        # STEP 4: Launch keyframe generation jobs (async image jobs)
        # ========================================================================
        # Create job for keyframes_001
        keyframes_1_job = store.create_job(
            artifact_id="keyframes_001",
            provider="image_default",
            kind="image",
            request={"prompt": "Product in kitchen, warm lighting"},
        )
        assert keyframes_1_job["status"] == "queued"
        keyframes_1_job_id = keyframes_1_job["id"]

        # Create job for keyframes_002
        keyframes_2_job = store.create_job(
            artifact_id="keyframes_002",
            provider="image_default",
            kind="image",
            request={"prompt": "Product close-up, detail shot"},
        )
        assert keyframes_2_job["status"] == "queued"
        keyframes_2_job_id = keyframes_2_job["id"]

        # Set keyframes to generating status
        store.set_status("keyframes_001", "generating")
        store.set_status("keyframes_002", "generating")

        # Poll until jobs complete (keyframes_001 should complete first)
        # polls_to_success=2, so need 3 polls per job: queued->running, running->running, running->succeeded
        for _ in range(6):
            poller.poll_once()

        # Verify jobs completed
        kf1_job = store.get_job(keyframes_1_job_id)
        kf2_job = store.get_job(keyframes_2_job_id)
        assert kf1_job["status"] == "succeeded", "keyframes_1 job should succeed"
        assert kf2_job["status"] == "succeeded", "keyframes_2 job should succeed"

        # Verify versions were created for both keyframes
        kf1_versions = store.list_versions("keyframes_001")
        kf2_versions = store.list_versions("keyframes_002")
        assert len(kf1_versions) == 1
        assert len(kf2_versions) == 1
        assert kf1_versions[0]["content_type"] == "image/png"
        assert kf2_versions[0]["content_type"] == "image/png"

        # Verify artifacts have selected versions and moved to review
        kf1_artifact = store.get_artifact("keyframes_001")
        kf2_artifact = store.get_artifact("keyframes_002")
        assert kf1_artifact["selected_version_id"] == kf1_versions[0]["id"]
        assert kf2_artifact["selected_version_id"] == kf2_versions[0]["id"]

        # ========================================================================
        # STEP 5: Revise one image (keyframes_001) while keyframes_002 stays independent
        # ========================================================================
        # Create revise event for keyframes_001
        revise_event = store.enqueue_event(
            type="revise",
            payload={"note": "Warmer lighting, less contrast"},
            artifact_id="keyframes_001",
        )
        assert revise_event["status"] == "pending"

        # Simulate worker processing: create new job for revised image
        kf1_revised_job = store.create_job(
            artifact_id="keyframes_001",
            provider="image_default",
            kind="image",
            request={"prompt": "Product in kitchen, very warm lighting, soft shadows"},
        )
        store.set_status("keyframes_001", "generating")

        # Poll until revised job completes
        for _ in range(6):
            poller.poll_once()

        kf1_revised = store.get_job(kf1_revised_job["id"])
        assert kf1_revised["status"] == "succeeded"

        # Verify keyframes_001 has 2 versions now
        kf1_versions_updated = store.list_versions("keyframes_001")
        assert len(kf1_versions_updated) == 2

        # Verify keyframes_002 still has only 1 version (independent, unaffected)
        kf2_versions_final = store.list_versions("keyframes_002")
        assert len(kf2_versions_final) == 1

        # ========================================================================
        # STEP 6: Generate video clips from keyframes
        # ========================================================================
        # Approve keyframes first (for clips to depend on)
        store.set_status("keyframes_001", "approved")
        store.set_status("keyframes_002", "approved")

        # Create clip jobs (clips depend on keyframes)
        clips_1_job = store.create_job(
            artifact_id="clips_001",
            provider="video_default",
            kind="video",
            request={"prompt": "Smooth pan across product", "duration": 3},
        )
        clips_1_job_id = clips_1_job["id"]

        clips_2_job = store.create_job(
            artifact_id="clips_002",
            provider="video_default",
            kind="video",
            request={"prompt": "Detail shot, close-up reveal", "duration": 2},
        )
        clips_2_job_id = clips_2_job["id"]

        store.set_status("clips_001", "generating")
        store.set_status("clips_002", "generating")

        # Poll a few times to get both jobs running
        for _ in range(4):
            poller.poll_once()

        # Both should be running
        clips_1_current = store.get_job(clips_1_job_id)
        clips_2_current = store.get_job(clips_2_job_id)
        assert clips_1_current["status"] == "running"
        assert clips_2_current["status"] == "running"

        # ========================================================================
        # STEP 7a: Cancel one clip (clips_001) mid-flight
        # ========================================================================
        store.cancel_job(clips_1_job_id)

        # Poll again to process the cancel
        poller.poll_once()

        clips_1_cancelled = store.get_job(clips_1_job_id)
        assert clips_1_cancelled["status"] == "cancelled"
        assert clips_1_cancelled["cancel_requested"] is True

        # Verify no version was created for cancelled job
        clips_1_versions = store.list_versions("clips_001")
        assert len(clips_1_versions) == 0, "Cancelled job should not create version"

        # ========================================================================
        # STEP 7b: Simulate worker crash -> recover via lease expiry
        # ========================================================================
        # Ack any pending stale events first so they don't interfere
        pending_events = store.conn.execute(
            "SELECT id FROM events WHERE status='pending'"
        ).fetchall()
        for (evt_id,) in pending_events:
            store.ack_event(evt_id)

        # Now claim an event without acking it (simulating worker crash mid-turn)
        revise_event_2 = store.enqueue_event(
            type="revise",
            payload={"note": "Another revision"},
            artifact_id="clips_002",
        )

        # Claim the event
        claimed = store.claim_next_event(worker_id="worker_A", lease_seconds=1)
        assert claimed is not None
        assert claimed["id"] == revise_event_2["id"]
        assert claimed["status"] == "processing"
        assert claimed["claimed_by"] == "worker_A"
        assert claimed["attempt_count"] == 1

        # Simulate time passing: update the event's lease_until to past time
        # (we can't actually wait, so we directly manipulate the database)
        now_dt = datetime.now(timezone.utc)
        store.conn.execute(
            "UPDATE events SET lease_until = ? WHERE id = ?",
            ((now_dt - timedelta(seconds=1)).isoformat(), revise_event_2["id"]),
        )
        store.conn.commit()

        # Now claim_next_event should recover the expired event
        # First reset the event to pending (lease expiry recovery)
        claimed_recovered = store.claim_next_event(worker_id="worker_C", lease_seconds=1)
        assert claimed_recovered is not None
        assert claimed_recovered["id"] == revise_event_2["id"]
        assert claimed_recovered["status"] == "processing"
        assert claimed_recovered["claimed_by"] == "worker_C"
        assert claimed_recovered["attempt_count"] == 2  # Claim count incremented

        # Ack the recovered event
        store.ack_event(revise_event_2["id"])

        # ========================================================================
        # STEP 8: Continue generating clips_002 (clips_001 was cancelled)
        # ========================================================================
        # Poll until clips_002 job completes (it's still running)
        for _ in range(10):  # Enough polls to advance video (polls_to_success=3)
            poller.poll_once()

        clips_2_final = store.get_job(clips_2_job_id)
        assert clips_2_final["status"] == "succeeded"

        # Verify version was created for clips_002
        clips_2_versions = store.list_versions("clips_002")
        assert len(clips_2_versions) == 1

        # Approve clips_002
        store.set_status("clips_002", "approved")

        # ========================================================================
        # STEP 9: Create and approve final assembly artifact
        # ========================================================================
        # Assembly depends on both clips, but clips_001 was cancelled (has no version)
        # so the pipeline should handle this. For this test, we'll just approve
        # the assembly artifact as-is.

        # In a real workflow, the worker would create the assembly version
        # (e.g., ffmpeg combining the completed clips). For this test, simulate it:
        assembly_v1 = store.put_version(
            "assembly_001",
            content_ref="media/assembly_001_v1.mp4",
            content_type="video/mp4",
            created_by="worker",
            note="Final assembled film",
            select=True,
        )
        assert assembly_v1["ok"] is True

        # Approve assembly
        store.set_status("assembly_001", "approved")

        # ========================================================================
        # STEP 10: Verify final state
        # ========================================================================

        # Verify required stages are approved
        script_final = store.get_artifact("script_001")
        shots_final = store.get_artifact("shots_001")
        kf1_final = store.get_artifact("keyframes_001")
        kf2_final = store.get_artifact("keyframes_002")
        clips_1_final = store.get_artifact("clips_001")
        clips_2_final = store.get_artifact("clips_002")
        assembly_final = store.get_artifact("assembly_001")

        # Script, shots, keyframes_2, clips_2, assembly should be approved
        assert script_final["status"] == "approved"
        assert shots_final["status"] == "approved"
        assert kf1_final["status"] == "approved"
        assert kf2_final["status"] == "approved"
        assert clips_2_final["status"] == "approved"
        assert assembly_final["status"] == "approved"

        # clips_001 should be generating/cancelled/stale/failed (cancelled job, never finished)
        assert clips_1_final["status"] in ("draft", "generating", "cancelled", "stale", "failed")

        # Verify versions were never deleted (each artifact retains all its versions)
        script_all_versions = store.list_versions("script_001")
        assert len(script_all_versions) == 1
        shots_all_versions = store.list_versions("shots_001")
        assert len(shots_all_versions) == 1
        kf1_all_versions = store.list_versions("keyframes_001")
        assert len(kf1_all_versions) == 2, "keyframes_001 should have 2 versions (original + revised)"
        kf2_all_versions = store.list_versions("keyframes_002")
        assert len(kf2_all_versions) == 1
        clips_1_all_versions = store.list_versions("clips_001")
        assert len(clips_1_all_versions) == 0, "clips_001 was cancelled, no version created"
        clips_2_all_versions = store.list_versions("clips_002")
        assert len(clips_2_all_versions) == 1
        assembly_all_versions = store.list_versions("assembly_001")
        assert len(assembly_all_versions) == 1

        # Verify stale artifacts from step 1 still exist (not deleted)
        # All should still be in store even if they went stale early
        for art_id in ["script_001", "shots_001", "keyframes_001", "keyframes_002", "clips_001", "clips_002", "assembly_001"]:
            art = store.get_artifact(art_id)
            assert art is not None, f"{art_id} should still exist"

        # Final verification: check job events
        job_done_events = store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='job_done'"
        ).fetchone()
        job_failed_events = store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='job_failed'"
        ).fetchone()

        # We had 4 successful jobs (2 image, 1 video for clips_2, 1 video for clips_1 which was cancelled but might emit events)
        # and clips_1 was cancelled so no job_done for it
        assert job_done_events[0] >= 3, "Should have at least 3 successful job_done events"

    def test_version_history_immutability(self, store):
        """Verify that version history is immutable and grows only."""
        art = store.create_artifact(
            id="immutable_test",
            stage="script",
            title="Test Immutability",
        )

        # Create versions
        v1 = store.put_version("immutable_test", content="Version 1", select=True)
        v2 = store.put_version("immutable_test", content="Version 2", select=True)
        v3 = store.put_version("immutable_test", content="Version 3", select=True)

        # Get all versions
        versions = store.list_versions("immutable_test")
        assert len(versions) == 3

        # Selecting an older version should not delete newer versions
        store.select_version("immutable_test", v1["version_id"])
        versions_after_select = store.list_versions("immutable_test")
        assert len(versions_after_select) == 3

        # Verify all 3 versions still have their content
        for v in versions_after_select:
            assert v["content"] in ["Version 1", "Version 2", "Version 3"]

    def test_stale_propagation_isolation(self, store):
        """Verify that stale propagation doesn't affect unrelated branches."""
        # Create two independent artifact chains
        # Chain 1: A -> B -> C
        store.create_artifact(id="a1", stage="s1", title="A1")
        store.create_artifact(id="b1", stage="s2", title="B1")
        store.create_artifact(id="c1", stage="s3", title="C1")
        store.add_dependency("a1", "b1")
        store.add_dependency("b1", "c1")

        # Chain 2: A2 -> B2 -> C2
        store.create_artifact(id="a2", stage="s1", title="A2")
        store.create_artifact(id="b2", stage="s2", title="B2")
        store.create_artifact(id="c2", stage="s3", title="C2")
        store.add_dependency("a2", "b2")
        store.add_dependency("b2", "c2")

        # Put versions on downstream artifacts (without selecting, so they don't mark their own downstream as stale)
        store.put_version("b1", content="B1 v1", select=False)
        store.put_version("c1", content="C1 v1", select=False)
        store.put_version("b2", content="B2 v1", select=False)
        store.put_version("c2", content="C2 v1", select=False)

        # Update A1 (first version), which should mark only B1, C1 as stale
        store.put_version("a1", content="A1 v1", select=True)

        # Now update A1 again with a different version
        store.put_version("a1", content="A1 v2", select=True)

        # Verify B1, C1 are stale (marked by A1 update)
        assert store.get_artifact("b1")["status"] == "stale"
        assert store.get_artifact("c1")["status"] == "stale"

        # Verify B2, C2 are NOT stale (different chain, not affected by A1 changes)
        assert store.get_artifact("b2")["status"] == "draft"
        assert store.get_artifact("c2")["status"] == "draft"

    def test_cancellation_prevents_late_success(self, store, video_provider, poller):
        """Verify that cancelling a job prevents late success from creating a version."""
        art = store.create_artifact(
            id="cancellation_test",
            stage="clips",
            title="Cancellation Test Clip",
        )

        # Create a job with many polls_to_success to simulate long-running job
        provider = MockVideoProvider(polls_to_success=20)
        job = store.create_job(
            artifact_id="cancellation_test",
            provider="video_long",
            kind="video",
            request={"prompt": "test"},
        )

        # Create poller with long-running provider
        long_poller = Poller(store, {"video_long": provider})

        # Submit the job
        long_poller.poll_once()
        job_after_submit = store.get_job(job["id"])
        assert job_after_submit["status"] == "running"
        assert job_after_submit["provider_job_id"] is not None

        # Poll a few times to advance status
        for _ in range(3):
            long_poller.poll_once()

        job_mid = store.get_job(job["id"])
        assert job_mid["status"] == "running"
        assert len(store.list_versions("cancellation_test")) == 0

        # Cancel the job
        store.cancel_job(job["id"])

        # Poll many more times (simulating late success from provider)
        for _ in range(30):
            long_poller.poll_once()

        # Verify job is still cancelled
        job_final = store.get_job(job["id"])
        assert job_final["status"] == "cancelled"

        # Verify NO version was created
        versions_final = store.list_versions("cancellation_test")
        assert len(versions_final) == 0, "Cancelled job must not create version even if provider later reports success"

        # Verify no job_done event
        events = store.conn.execute(
            "SELECT * FROM events WHERE type='job_done' AND artifact_id='cancellation_test'"
        ).fetchall()
        assert len(events) == 0

