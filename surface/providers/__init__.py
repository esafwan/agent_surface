"""Provider adapters for async job execution.

Providers implement a standard interface for submitting, polling, and cancelling
generation jobs with external services or local tools.
"""

from surface.providers.base import Provider

__all__ = ["Provider"]
