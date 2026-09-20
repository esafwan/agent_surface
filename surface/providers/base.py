"""Abstract base class for generation providers."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class Provider(ABC):
    """Abstract base class for generation providers.

    Providers handle async submission, polling, and cancellation of generation jobs.
    Implementations may wrap real APIs (image generation, video generation) or
    provide mock/deterministic behavior for testing.
    """

    @abstractmethod
    def submit(self, request: Dict[str, Any]) -> str:
        """Submit a generation request to the provider.

        Args:
            request: Request payload containing generation parameters (prompt, duration, etc).

        Returns:
            provider_job_id: Unique identifier for the job with this provider.
                           Used for subsequent status/cancel/collect calls.
        """
        pass

    @abstractmethod
    def status(self, provider_job_id: str) -> str:
        """Check the status of a submitted job.

        Args:
            provider_job_id: Identifier returned from submit().

        Returns:
            status: One of "queued", "running", "succeeded", "failed".
        """
        pass

    @abstractmethod
    def cancel(self, provider_job_id: str) -> bool:
        """Cancel a pending or running job.

        Args:
            provider_job_id: Identifier returned from submit().

        Returns:
            success: True if cancellation was accepted, False otherwise.
                    A job that has already completed cannot be cancelled.
        """
        pass

    @abstractmethod
    def collect(self, provider_job_id: str) -> Dict[str, Any]:
        """Collect the result of a completed job.

        Args:
            provider_job_id: Identifier returned from submit().

        Returns:
            result: Dict containing:
                   - content_ref: Storage path/URL for the generated content
                   - content_type: MIME type (e.g., "image/png", "video/mp4")
                   - metadata: Optional dict with additional info (seed, params, etc)
        """
        pass
