"""Mock video generation provider for testing."""

import uuid
from typing import Any, Dict

from surface.providers.base import Provider


class MockVideoProvider(Provider):
    """Mock video provider that simulates generation deterministically and quickly.

    Similar to MockImageProvider but for video content. Demonstrates proper
    handling of cancellation where late-arriving success must not override
    a cancelled state.
    """

    def __init__(self, polls_to_success: int = 5):
        """Initialize the mock video provider.

        Args:
            polls_to_success: Number of status() calls before job transitions to succeeded.
                            Allows testing of longer polling loops.
        """
        self.polls_to_success = polls_to_success
        # Maps provider_job_id -> {"status": str, "poll_count": int, "cancelled": bool}
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def submit(self, request: Dict[str, Any]) -> str:
        """Submit a video generation request.

        Args:
            request: Dict containing prompt, duration, params, etc.

        Returns:
            provider_job_id: Unique ID for this job.
        """
        job_id = f"vid_{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {
            "status": "queued",
            "poll_count": 0,
            "cancelled": False,
            "request": request,
        }
        return job_id

    def status(self, provider_job_id: str) -> str:
        """Check job status, advancing through queued -> running -> succeeded.

        Args:
            provider_job_id: Job ID from submit().

        Returns:
            status: "queued", "running", "succeeded", "failed", or "cancelled".
        """
        if provider_job_id not in self._jobs:
            return "failed"

        job = self._jobs[provider_job_id]

        # If cancelled, return cancelled state (critical: never override with success)
        if job["cancelled"]:
            return "cancelled"

        # Advance through poll sequence: queued -> running -> succeeded
        if job["status"] == "queued":
            job["status"] = "running"
            return "running"

        if job["status"] == "running":
            job["poll_count"] += 1
            if job["poll_count"] >= self.polls_to_success:
                job["status"] = "succeeded"
                return "succeeded"
            return "running"

        return job["status"]

    def cancel(self, provider_job_id: str) -> bool:
        """Cancel a job.

        Args:
            provider_job_id: Job ID from submit().

        Returns:
            success: True if job was cancelled.
        """
        if provider_job_id not in self._jobs:
            return False

        job = self._jobs[provider_job_id]
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return False  # Cannot cancel completed job

        job["cancelled"] = True
        job["status"] = "cancelled"
        return True

    def collect(self, provider_job_id: str) -> Dict[str, Any]:
        """Collect result of a completed job.

        Args:
            provider_job_id: Job ID from submit().

        Returns:
            result: Dict with content_ref, content_type, and metadata.
        """
        if provider_job_id not in self._jobs:
            return {}

        job = self._jobs[provider_job_id]
        return {
            "content_ref": f"media/video_{provider_job_id}.mp4",
            "content_type": "video/mp4",
            "metadata": {
                "job_id": provider_job_id,
                "prompt": job.get("request", {}).get("prompt", ""),
                "duration": job.get("request", {}).get("duration", 5),
                "seed": 123,
            },
        }
