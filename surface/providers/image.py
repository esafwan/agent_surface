"""Mock image generation provider for testing."""

import uuid
from typing import Any, Dict, Optional

from surface.providers.base import Provider


class MockImageProvider(Provider):
    """Mock image provider that simulates generation deterministically and quickly.

    Each submitted job polls through queued -> running states and completes
    after a fixed number of status() calls. No real network calls or model execution.
    """

    def __init__(self, polls_to_success: int = 3):
        """Initialize the mock image provider.

        Args:
            polls_to_success: Number of status() calls before job transitions to succeeded.
                            Allows testing of polling loops.
        """
        self.polls_to_success = polls_to_success
        # Maps provider_job_id -> {"status": str, "poll_count": int, "cancelled": bool}
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def submit(self, request: Dict[str, Any]) -> str:
        """Submit an image generation request.

        Args:
            request: Dict containing prompt, params, etc.

        Returns:
            provider_job_id: Unique ID for this job.
        """
        job_id = f"img_{uuid.uuid4().hex[:8]}"
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
            status: "queued", "running", "succeeded", or "failed".
        """
        if provider_job_id not in self._jobs:
            return "failed"

        job = self._jobs[provider_job_id]

        # If cancelled, return cancelled state
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
            "content_ref": f"media/image_{provider_job_id}.png",
            "content_type": "image/png",
            "metadata": {
                "job_id": provider_job_id,
                "prompt": job.get("request", {}).get("prompt", ""),
                "seed": 42,
            },
        }
