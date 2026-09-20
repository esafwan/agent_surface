"""Mock video generation provider for testing."""

from typing import Any, Dict, Optional

from surface.providers.base import Provider
from surface.providers._mock_common import build_job_id, parse_target


class MockVideoProvider(Provider):
    """Mock video provider that simulates generation deterministically and quickly.

    Similar to MockImageProvider but for video content. Demonstrates proper
    handling of cancellation where late-arriving success must not override
    a cancelled state.

    Per SPEC section 40 ("Poller crash: durable jobs remain; restart and
    continue"), a fresh provider instance (e.g. after a poller restart) must
    still be able to answer status() correctly for a job it never saw
    submit() called for. See `_mock_common` / `MockImageProvider` for the
    id-encoding approach shared by both mock providers.
    """

    PREFIX = "vid"

    def __init__(self, polls_to_success: int = 5):
        """Initialize the mock video provider.

        Args:
            polls_to_success: Number of status() calls before job transitions to succeeded.
                            Allows testing of longer polling loops.
        """
        self.polls_to_success = polls_to_success
        # Maps provider_job_id -> {"status": str, "poll_count": int, "cancelled": bool, "target": int}
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def submit(self, request: Dict[str, Any]) -> str:
        """Submit a video generation request.

        Args:
            request: Dict containing prompt, duration, params, etc.

        Returns:
            provider_job_id: Unique ID for this job, encoding the
                polls-to-success target so a restarted provider can resume
                tracking it correctly.
        """
        job_id = build_job_id(self.PREFIX, self.polls_to_success)
        self._jobs[job_id] = {
            "status": "queued",
            "poll_count": 0,
            "cancelled": False,
            "target": self.polls_to_success,
            "request": request,
        }
        return job_id

    def _get_or_init(self, provider_job_id: str) -> Optional[Dict[str, Any]]:
        """Look up tracking state for a job id, lazily initializing it.

        See MockImageProvider._get_or_init for rationale: this is what lets a
        poller restart resume and complete an in-flight job.
        """
        if provider_job_id in self._jobs:
            return self._jobs[provider_job_id]

        target = parse_target(provider_job_id, self.PREFIX)
        if target is None:
            return None

        job = {
            "status": "queued",
            "poll_count": 0,
            "cancelled": False,
            "target": target,
            "request": {},
        }
        self._jobs[provider_job_id] = job
        return job

    def status(self, provider_job_id: str) -> str:
        """Check job status, advancing through queued -> running -> succeeded.

        Args:
            provider_job_id: Job ID from submit().

        Returns:
            status: "queued", "running", "succeeded", "failed", or "cancelled".
        """
        job = self._get_or_init(provider_job_id)
        if job is None:
            return "failed"

        # If cancelled, return cancelled state (critical: never override with success)
        if job["cancelled"]:
            return "cancelled"

        # Advance through poll sequence: queued -> running -> succeeded
        if job["status"] == "queued":
            job["status"] = "running"
            return "running"

        if job["status"] == "running":
            job["poll_count"] += 1
            if job["poll_count"] >= job["target"]:
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
        job = self._get_or_init(provider_job_id)
        if job is None:
            return False

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
        job = self._get_or_init(provider_job_id)
        if job is None:
            return {}

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
