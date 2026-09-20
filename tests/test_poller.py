"""Unit tests for the async job poller."""

import pytest
from surface.poller import Poller
from surface.providers.image import MockImageProvider
from surface.providers.video import MockVideoProvider
from surface.store import Store


class TestPollerBasicFlow:
    """Tests for basic job polling flow."""

    def test_poll_once_submits_queued_job(self):
        """poll_once should submit queued jobs and update provider_job_id."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        # Create a queued job
        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "A sunset"},
        )
        job_id = job["id"]

        # Initially no provider_job_id
        assert job["provider_job_id"] is None
        assert job["status"] == "queued"

        # Create poller with image provider
        provider = MockImageProvider(polls_to_success=1)
        poller = Poller(store, {"image": provider})

        # First poll should submit the job
        processed = poller.poll_once()
        assert processed == 1

        # Job should now have provider_job_id and be running
        updated_job = store.get_job(job_id)
        assert updated_job["provider_job_id"] is not None
        assert updated_job["status"] == "running"

    def test_poll_once_checks_running_job_status(self):
        """poll_once should check status of running jobs."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        provider = MockImageProvider(polls_to_success=2)
        job_id = provider.submit({"prompt": "test"})

        # Create job with provider_job_id already set
        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
            provider_job_id=job_id,
        )
        store.update_job_status(job["id"], "running", provider_job_id=job_id)

        poller = Poller(store, {"image": provider})

        # First poll: checks status (should be running)
        poller.poll_once()
        job_after_first = store.get_job(job["id"])
        assert job_after_first["status"] == "running"

        # Second poll: checks status again (should still be running)
        poller.poll_once()
        job_after_second = store.get_job(job["id"])
        assert job_after_second["status"] == "running"

        # Third poll: should succeed
        poller.poll_once()
        job_after_third = store.get_job(job["id"])
        assert job_after_third["status"] == "succeeded"

    def test_successful_job_creates_version_and_emits_event(self):
        """Successful job should create artifact version and emit job_done event."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        provider = MockImageProvider(polls_to_success=1)
        job_id_provider = provider.submit({"prompt": "A sunset"})

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "A sunset"},
            provider_job_id=job_id_provider,
        )
        store.update_job_status(job["id"], "running", provider_job_id=job_id_provider)

        # Before polling, no versions
        versions_before = store.list_versions("img_1")
        assert len(versions_before) == 0

        poller = Poller(store, {"image": provider})

        # First poll: advances provider status to running
        poller.poll_once()
        job_mid = store.get_job(job["id"])
        assert job_mid["status"] == "running"

        # Second poll should advance status to succeeded and create version
        poller.poll_once()

        # Check job is succeeded
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "succeeded"
        assert job_after["result"]["content_ref"] is not None

        # Check version was created
        versions_after = store.list_versions("img_1")
        assert len(versions_after) == 1
        assert versions_after[0]["content_type"] == "image/png"

        # Check artifact is still selected for the new version
        artifact = store.get_artifact("img_1")
        assert artifact["selected_version_id"] == versions_after[0]["id"]

        # Check job_done event was emitted
        events = store.conn.execute(
            "SELECT * FROM events WHERE type='job_done' AND artifact_id=?",
            ("img_1",),
        ).fetchall()
        assert len(events) == 1
        event = dict(events[0])
        payload = event["payload_json"]
        assert job["id"] in payload

    def test_failed_job_emits_job_failed_event(self):
        """Failed job should emit job_failed event."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        # Create job with non-existent provider_job_id to simulate provider failure
        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
            provider_job_id="nonexistent_provider_job",
        )
        store.update_job_status(job["id"], "running", provider_job_id="nonexistent_provider_job")

        provider = MockImageProvider()
        poller = Poller(store, {"image": provider})

        # Polling the job with non-existent provider_job_id should fail
        poller.poll_once()

        # Job should be marked as failed
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "failed"

        # job_failed event should be emitted
        events = store.conn.execute(
            "SELECT * FROM events WHERE type='job_failed' AND artifact_id=?",
            ("img_1",),
        ).fetchall()
        assert len(events) == 1

    def test_submit_failure_marks_job_failed(self):
        """If provider.submit() raises exception, job should be marked failed."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        job = store.create_job(
            artifact_id="img_1",
            provider="broken",
            kind="image",
            request={"prompt": "test"},
        )

        # No provider for "broken", so submit will fail
        poller = Poller(store, {})

        poller.poll_once()

        # Job should be marked as failed
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "failed"

        # job_failed event should be emitted
        events = store.conn.execute(
            "SELECT * FROM events WHERE type='job_failed'",
        ).fetchall()
        assert len(events) == 1


class TestPollerCancellation:
    """Tests for job cancellation handling."""

    def test_cancel_requested_calls_provider_cancel(self):
        """poll_once should call provider.cancel() if cancel_requested is set."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        provider = MockImageProvider(polls_to_success=10)
        job_id_provider = provider.submit({"prompt": "test"})

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
            provider_job_id=job_id_provider,
        )
        store.update_job_status(job["id"], "running", provider_job_id=job_id_provider)

        # Mark for cancellation
        store.cancel_job(job["id"])

        poller = Poller(store, {"image": provider})
        poller.poll_once()

        # Job should be marked as cancelled
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "cancelled"
        assert job_after["cancel_requested"] is True

    def test_cancellation_then_late_success_ignored(self):
        """Late success from provider should not override a cancelled job."""
        store = Store(":memory:")
        store.create_artifact(id="vid_1", stage="clips", title="Video")

        provider = MockVideoProvider(polls_to_success=10)
        job_id_provider = provider.submit({"prompt": "A dance", "duration": 5})

        job = store.create_job(
            artifact_id="vid_1",
            provider="video",
            kind="video",
            request={"prompt": "A dance", "duration": 5},
            provider_job_id=job_id_provider,
        )
        store.update_job_status(job["id"], "running", provider_job_id=job_id_provider)

        # Poll a few times to get the job running
        poller = Poller(store, {"video": provider})
        for _ in range(3):
            poller.poll_once()

        # Verify job is still running
        job_mid = store.get_job(job["id"])
        assert job_mid["status"] == "running"

        # Cancel the job
        store.cancel_job(job["id"])

        # Poll again - should cancel at provider
        poller.poll_once()
        job_after_cancel = store.get_job(job["id"])
        assert job_after_cancel["status"] == "cancelled"

        # Continue polling many times - job should stay cancelled, not succeed
        # This simulates the provider eventually reporting success after cancellation
        for _ in range(15):
            poller.poll_once()

        job_final = store.get_job(job["id"])
        assert job_final["status"] == "cancelled", "Job must remain cancelled despite late success"

        # Should be no versions created for this artifact
        versions = store.list_versions("vid_1")
        assert len(versions) == 0, "No versions should be created for cancelled job"

        # No job_done event should exist (only potential job_failed or nothing)
        events = store.conn.execute(
            "SELECT * FROM events WHERE type='job_done' AND artifact_id=?",
            ("vid_1",),
        ).fetchall()
        assert len(events) == 0

    def test_cancelled_job_never_creates_version(self):
        """Even if provider reports success, cancelled job should not create version."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        provider = MockImageProvider(polls_to_success=3)
        job_id_provider = provider.submit({"prompt": "test"})

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
            provider_job_id=job_id_provider,
        )
        store.update_job_status(job["id"], "running", provider_job_id=job_id_provider)

        # Mark for cancellation immediately
        store.cancel_job(job["id"])

        poller = Poller(store, {"image": provider})

        # Poll multiple times - provider should eventually succeed but poller should not apply it
        for _ in range(10):
            poller.poll_once()

        # Verify artifact has no versions
        versions = store.list_versions("img_1")
        assert len(versions) == 0


class TestPollerMultipleProviders:
    """Tests for poller with multiple provider types."""

    def test_poller_dispatches_to_correct_provider(self):
        """Poller should route jobs to correct provider based on provider field."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")
        store.create_artifact(id="vid_1", stage="clips", title="Video")

        img_provider = MockImageProvider(polls_to_success=1)
        vid_provider = MockVideoProvider(polls_to_success=3)

        img_job_provider_id = img_provider.submit({"prompt": "img"})
        vid_job_provider_id = vid_provider.submit({"prompt": "vid", "duration": 5})

        img_job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "img"},
            provider_job_id=img_job_provider_id,
        )
        store.update_job_status(img_job["id"], "running", provider_job_id=img_job_provider_id)

        vid_job = store.create_job(
            artifact_id="vid_1",
            provider="video",
            kind="video",
            request={"prompt": "vid", "duration": 5},
            provider_job_id=vid_job_provider_id,
        )
        store.update_job_status(vid_job["id"], "running", provider_job_id=vid_job_provider_id)

        poller = Poller(store, {"image": img_provider, "video": vid_provider})

        # First poll advances both to running state
        poller.poll_once()
        img_job_after_first = store.get_job(img_job["id"])
        vid_job_after_first = store.get_job(vid_job["id"])
        assert img_job_after_first["status"] == "running"
        assert vid_job_after_first["status"] == "running"

        # Second poll: Image job completes (polls_to_success=1), Video still running
        poller.poll_once()

        # Image job should be completed by image provider
        img_job_after = store.get_job(img_job["id"])
        assert img_job_after["status"] == "succeeded"

        # Video job should still be running (requires more polls)
        vid_job_after = store.get_job(vid_job["id"])
        assert vid_job_after["status"] == "running"

    def test_poller_processes_multiple_jobs(self):
        """poll_once should process multiple jobs in one call."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image 1")
        store.create_artifact(id="img_2", stage="keyframes", title="Image 2")

        provider = MockImageProvider(polls_to_success=1)

        job1 = store.create_job(
            artifact_id="img_1", provider="image", kind="image", request={"prompt": "job1"}
        )
        job2 = store.create_job(
            artifact_id="img_2", provider="image", kind="image", request={"prompt": "job2"}
        )

        poller = Poller(store, {"image": provider})

        # First poll: both jobs submitted (processed count = 2)
        processed = poller.poll_once()
        assert processed == 2

        # Both should now be running
        j1 = store.get_job(job1["id"])
        j2 = store.get_job(job2["id"])
        assert j1["status"] == "running"
        assert j2["status"] == "running"


class TestPollerEdgeCases:
    """Tests for edge cases and error handling."""

    def test_unknown_provider_fails_job(self):
        """Jobs with unknown provider should be marked as failed."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        job = store.create_job(
            artifact_id="img_1",
            provider="unknown_provider",
            kind="image",
            request={"prompt": "test"},
        )

        poller = Poller(store, {})  # No providers registered

        # Should process the job and mark it as failed
        processed = poller.poll_once()
        assert processed == 1

        # Job should be marked as failed
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "failed"
        assert "Unknown provider" in job_after["result"].get("error", "")

    def test_poller_with_no_jobs(self):
        """poll_once should return 0 if no jobs to process."""
        store = Store(":memory:")
        provider = MockImageProvider()
        poller = Poller(store, {"image": provider})

        processed = poller.poll_once()
        assert processed == 0

    def test_completed_jobs_not_repolled(self):
        """poll_once should only process queued/running jobs, not succeeded/failed."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
        )

        # Manually mark as succeeded
        store.update_job_status(job["id"], "succeeded")

        provider = MockImageProvider()
        poller = Poller(store, {"image": provider})

        # poll_once should not touch this job
        processed = poller.poll_once()
        assert processed == 0

    def test_poller_initializes_with_no_providers(self):
        """Poller should initialize successfully with empty provider dict."""
        store = Store(":memory:")
        poller = Poller(store)
        assert poller.providers == {}

        poller_with_empty = Poller(store, {})
        assert poller_with_empty.providers == {}


class TestPollerRestartSurvival:
    """SPEC section 40: 'Poller crash: durable jobs remain; restart and
    continue.' A fresh Poller + fresh provider instance (same store) must be
    able to resume and complete a job in flight when the original process
    that submitted it is gone."""

    def test_restart_with_fresh_provider_completes_inflight_job(self):
        """Simulate a poller/provider restart mid-flight: a new Poller and a
        brand-new MockImageProvider instance (no in-memory history of the
        job) must still be able to drive the job to completion using only
        the durable store + the provider_job_id encoded on the job row."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        # "Before restart": original poller/provider submits the job.
        original_provider = MockImageProvider(polls_to_success=2)
        original_poller = Poller(store, {"image": original_provider})

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "A sunset"},
        )
        job_id = job["id"]

        # Submits the job to the provider; provider_job_id now persisted.
        original_poller.poll_once()
        submitted_job = store.get_job(job_id)
        assert submitted_job["status"] == "running"
        assert submitted_job["provider_job_id"] is not None

        # "Restart": original_provider/original_poller are discarded (as if
        # the process crashed); brand new instances take over, sharing only
        # the durable store.
        fresh_provider = MockImageProvider(polls_to_success=2)
        fresh_poller = Poller(store, {"image": fresh_provider})

        # The fresh provider has never seen this provider_job_id before.
        assert submitted_job["provider_job_id"] not in fresh_provider._jobs

        # Poll enough times to drive it to completion.
        for _ in range(5):
            fresh_poller.poll_once()
            final_job = store.get_job(job_id)
            if final_job["status"] == "succeeded":
                break

        final_job = store.get_job(job_id)
        assert final_job["status"] == "succeeded"

        # A version should have been created and the artifact moved to review.
        artifact = store.get_artifact("img_1")
        assert artifact["status"] == "review"
        versions = store.list_versions("img_1")
        assert len(versions) == 1
