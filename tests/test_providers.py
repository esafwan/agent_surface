"""Unit tests for provider implementations."""

import pytest
from surface.providers.image import MockImageProvider
from surface.providers.video import MockVideoProvider


class TestMockImageProvider:
    """Tests for MockImageProvider."""

    def test_submit_returns_job_id(self):
        """Submit should return a unique job ID."""
        provider = MockImageProvider()
        request = {"prompt": "A red ball"}
        job_id = provider.submit(request)
        assert job_id.startswith("img_")
        assert len(job_id) > 4

    def test_status_queued_to_running_to_succeeded(self):
        """Job progresses through states with each status() call."""
        provider = MockImageProvider(polls_to_success=2)
        job_id = provider.submit({"prompt": "test"})

        # First status call: queued -> running
        status1 = provider.status(job_id)
        assert status1 == "running"

        # Second status call: still running (poll_count=1)
        status2 = provider.status(job_id)
        assert status2 == "running"

        # Third status call: running -> succeeded (poll_count reaches 2)
        status3 = provider.status(job_id)
        assert status3 == "succeeded"

        # Subsequent calls still return succeeded
        status4 = provider.status(job_id)
        assert status4 == "succeeded"

    def test_cancel_job(self):
        """Cancel should mark job as cancelled and return true."""
        provider = MockImageProvider()
        job_id = provider.submit({"prompt": "test"})

        # Cancel should succeed
        result = provider.cancel(job_id)
        assert result is True

        # Status should now be cancelled
        status = provider.status(job_id)
        assert status == "cancelled"

    def test_cancel_completed_job_fails(self):
        """Cannot cancel an already completed job."""
        provider = MockImageProvider(polls_to_success=1)
        job_id = provider.submit({"prompt": "test"})

        # Poll to completion
        provider.status(job_id)
        provider.status(job_id)

        # Try to cancel after completion
        result = provider.cancel(job_id)
        assert result is False

    def test_cancel_prevents_success_override(self):
        """Cancelled job should not report success even if polling continues."""
        provider = MockImageProvider(polls_to_success=5)
        job_id = provider.submit({"prompt": "test"})

        # Poll a few times
        provider.status(job_id)
        provider.status(job_id)

        # Cancel the job
        provider.cancel(job_id)
        status = provider.status(job_id)
        assert status == "cancelled"

        # Further polling should still return cancelled, not succeeded
        status = provider.status(job_id)
        assert status == "cancelled"

    def test_collect_returns_result(self):
        """Collect should return content_ref, content_type, and metadata."""
        provider = MockImageProvider()
        job_id = provider.submit({"prompt": "A sunset"})

        result = provider.collect(job_id)
        assert "content_ref" in result
        assert "content_type" in result
        assert "metadata" in result

        assert result["content_ref"] == f"media/image_{job_id}.png"
        assert result["content_type"] == "image/png"
        assert result["metadata"]["prompt"] == "A sunset"

    def test_collect_returns_actual_cost(self):
        """Collect should report a deterministic actual_cost.

        Formula: $0.02 * polls_to_success target.
        """
        provider = MockImageProvider(polls_to_success=3)
        job_id = provider.submit({"prompt": "A sunset"})
        result = provider.collect(job_id)
        assert result["actual_cost"] == pytest.approx(0.06)

        provider2 = MockImageProvider(polls_to_success=1)
        job_id2 = provider2.submit({"prompt": "x"})
        result2 = provider2.collect(job_id2)
        assert result2["actual_cost"] == pytest.approx(0.02)

    def test_unknown_job_returns_failed(self):
        """Status for unknown job should return "failed"."""
        provider = MockImageProvider()
        status = provider.status("unknown_job_id")
        assert status == "failed"

    def test_unknown_job_cannot_be_cancelled(self):
        """Cancelling unknown job should return False."""
        provider = MockImageProvider()
        result = provider.cancel("unknown_job_id")
        assert result is False

    def test_multiple_concurrent_jobs(self):
        """Provider should handle multiple jobs independently."""
        provider = MockImageProvider(polls_to_success=2)

        job1 = provider.submit({"prompt": "job1"})
        job2 = provider.submit({"prompt": "job2"})
        job3 = provider.submit({"prompt": "job3"})

        # Poll job1 to completion
        provider.status(job1)
        provider.status(job1)
        assert provider.status(job1) == "succeeded"

        # Job2 and job3 should still be running
        assert provider.status(job2) == "running"
        assert provider.status(job3) == "running"

        # Cancel job2, others unaffected
        provider.cancel(job2)
        assert provider.status(job2) == "cancelled"
        assert provider.status(job3) == "running"


class TestMockVideoProvider:
    """Tests for MockVideoProvider."""

    def test_submit_returns_job_id(self):
        """Submit should return a unique job ID."""
        provider = MockVideoProvider()
        request = {"prompt": "A person dancing", "duration": 10}
        job_id = provider.submit(request)
        assert job_id.startswith("vid_")
        assert len(job_id) > 4

    def test_status_progression(self):
        """Video jobs progress with configurable polls_to_success."""
        provider = MockVideoProvider(polls_to_success=3)
        job_id = provider.submit({"prompt": "test", "duration": 5})

        # Progress through states
        assert provider.status(job_id) == "running"
        assert provider.status(job_id) == "running"
        assert provider.status(job_id) == "running"
        assert provider.status(job_id) == "succeeded"

    def test_cancel_prevents_late_success_override(self):
        """Critical: cancelled video should never report success on late polls."""
        provider = MockVideoProvider(polls_to_success=10)
        job_id = provider.submit({"prompt": "test"})

        # Poll a few times
        for _ in range(3):
            provider.status(job_id)

        # Cancel the job
        success = provider.cancel(job_id)
        assert success is True
        assert provider.status(job_id) == "cancelled"

        # Continue polling many more times
        # The provider should NEVER return succeeded after cancellation
        for _ in range(20):
            status = provider.status(job_id)
            assert status == "cancelled", f"Expected cancelled but got {status}"

    def test_collect_for_video(self):
        """Video provider collect should return video-specific content type."""
        provider = MockVideoProvider()
        job_id = provider.submit({"prompt": "A cat", "duration": 5})

        result = provider.collect(job_id)
        assert result["content_type"] == "video/mp4"
        assert result["content_ref"] == f"media/video_{job_id}.mp4"
        assert result["metadata"]["duration"] == 5

    def test_collect_metadata_from_request(self):
        """Collect should include metadata from original request."""
        provider = MockVideoProvider()
        request = {"prompt": "Dancing robot", "duration": 8}
        job_id = provider.submit(request)

        result = provider.collect(job_id)
        metadata = result["metadata"]
        assert metadata["prompt"] == "Dancing robot"
        assert metadata["duration"] == 8
        assert "seed" in metadata

    def test_collect_returns_actual_cost(self):
        """Collect should report a deterministic actual_cost.

        Formula: $0.10 * requested duration (seconds).
        """
        provider = MockVideoProvider()
        job_id = provider.submit({"prompt": "A cat", "duration": 5})
        result = provider.collect(job_id)
        assert result["actual_cost"] == pytest.approx(0.5)

        provider2 = MockVideoProvider()
        job_id2 = provider2.submit({"prompt": "A cat", "duration": 10})
        result2 = provider2.collect(job_id2)
        assert result2["actual_cost"] == pytest.approx(1.0)

    def test_multiple_jobs_isolation(self):
        """Video jobs should be independent."""
        provider = MockVideoProvider(polls_to_success=2)

        job1 = provider.submit({"prompt": "video1"})
        job2 = provider.submit({"prompt": "video2"})

        # Progress job1 to success
        provider.status(job1)
        provider.status(job1)
        assert provider.status(job1) == "succeeded"

        # Job2 should be unaffected
        assert provider.status(job2) == "running"

        # Can still cancel job2
        assert provider.cancel(job2) is True
        assert provider.status(job2) == "cancelled"
