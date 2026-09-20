"""Job poller for async generation tracking and completion.

The Poller independently scans the jobs table for pending/running jobs,
polls provider status, and handles completion (success or failure).
On success, confirms job was not cancelled, persists result, creates artifact version,
and emits job_done event. On failure, emits job_failed event.
"""

import logging
from typing import Any, Dict, Optional

from surface.providers.base import Provider
from surface.store import Store

logger = logging.getLogger(__name__)


def get_project_cost_summary(store: Store) -> Dict[str, float]:
    """Sum estimated and actual provider cost across ALL jobs in the store.

    Per SPEC section 39 ("Runtime SHOULD track estimated/actual provider
    cost where available"), this makes accumulated spend queryable, not
    just recorded on individual job rows. Mirrors the small summation
    pattern used by `worker.py`'s `_sum_job_costs` budget helper, but is
    intentionally duplicated here (not imported) so `poller.py` and
    `worker.py` stay independently owned modules this round.

    Includes jobs in every status (queued/running/succeeded/failed/
    cancelled) since `cost_estimate` may be set at creation time regardless
    of outcome, and `cost_actual` is only ever set on success. Callers that
    want budget-style "only running spend" semantics should filter jobs
    themselves (see `worker.py::_sum_job_costs` for that variant).

    Args:
        store: Store instance to scan.

    Returns:
        Dict with "cost_estimate_total" and "cost_actual_total" (floats).
    """
    statuses = ("queued", "running", "succeeded", "failed", "cancelled")
    estimate_total = 0.0
    actual_total = 0.0
    for status in statuses:
        for job in store.list_jobs_by_status(status):
            estimate_total += float(job.get("cost_estimate") or 0.0)
            actual_total += float(job.get("cost_actual") or 0.0)
    return {
        "cost_estimate_total": estimate_total,
        "cost_actual_total": actual_total,
    }


class Poller:
    """Async job poller and completion handler.

    Polls provider APIs/tools for job completion, handles state transitions,
    persists results, and emits completion events.
    """

    def __init__(self, store: Store, providers: Optional[Dict[str, Provider]] = None):
        """Initialize the poller.

        Args:
            store: Store instance for job/artifact management.
            providers: Dict mapping provider name/kind to Provider instances.
                      E.g. {"image": MockImageProvider(), "video": MockVideoProvider()}.
        """
        self.store = store
        self.providers = providers or {}

    def poll_once(self) -> int:
        """Poll pending/running jobs once, advancing state and handling completion.

        Scans jobs table for:
        - Queued jobs (provider_job_id not set): calls provider.submit()
        - Running jobs: calls provider.status()
        - Cancelled jobs with cancel_requested: calls provider.cancel() if not already done

        On job completion (success):
        - Verifies job was not cancelled
        - Persists provider result to job record
        - Creates new artifact version with result content
        - Updates job status to "succeeded"
        - Emits "job_done" system event

        On job failure:
        - Updates job status to "failed"
        - Emits "job_failed" system event

        Returns:
            Number of jobs processed in this poll cycle.
        """
        jobs_processed = 0

        # Get all jobs that are not terminal (queued, running)
        queued_jobs = self.store.list_jobs_by_status("queued")
        running_jobs = self.store.list_jobs_by_status("running")
        jobs = queued_jobs + running_jobs

        for job in jobs:
            job_id = job["id"]
            artifact_id = job["artifact_id"]
            provider_name = job["provider"]
            provider_job_id = job["provider_job_id"]
            cancel_requested = bool(job["cancel_requested"])

            # Get provider
            provider = self.providers.get(provider_name)
            if not provider:
                # Unknown provider is a fatal error - mark job as failed
                logger.warning(f"No provider found for {provider_name}, marking job {job_id} as failed")
                self._handle_job_failure(job_id, artifact_id, f"Unknown provider: {provider_name}", "job_failed")
                jobs_processed += 1
                continue

            # Handle cancellation request. This must fire even if the job was
            # cancelled before ever being submitted to the provider (no
            # provider_job_id yet) — otherwise a job cancelled while still
            # queued falls through to the submit branch below and gets
            # charged/started anyway, only cancelled on the *next* poll.
            if cancel_requested:
                if provider_job_id:
                    try:
                        provider.cancel(provider_job_id)
                        logger.info(f"Cancelled provider job {provider_job_id} for job {job_id}")
                    except Exception as e:
                        logger.warning(f"Error cancelling job {job_id}: {e}")
                else:
                    logger.info(f"Job {job_id} cancelled before submission; skipping provider.submit()")
                # Mark as cancelled in store; also move the artifact out of
                # "generating" so it doesn't spin forever with no path to
                # change (SPEC section 11 lists "cancelled" as a core state).
                self.store.update_job_status(job_id, "cancelled")
                self.store.set_status(artifact_id, "cancelled")
                jobs_processed += 1
                continue

            # Step 1: If not yet submitted, call provider.submit()
            if not provider_job_id:
                try:
                    request = job.get("request", {})
                    provider_job_id = provider.submit(request)
                    self.store.update_job_status(job_id, "running", provider_job_id=provider_job_id)
                    logger.info(f"Submitted job {job_id} to provider, got {provider_job_id}")
                    jobs_processed += 1
                    continue
                except Exception as e:
                    logger.error(f"Failed to submit job {job_id}: {e}")
                    self._handle_job_failure(job_id, artifact_id, str(e), "job_failed")
                    jobs_processed += 1
                    continue

            # Step 2: Poll provider for status
            try:
                status = provider.status(provider_job_id)
                logger.debug(f"Job {job_id} (provider: {provider_job_id}) status: {status}")
                self.apply_status(job_id, artifact_id, provider, provider_job_id, status)
                jobs_processed += 1

            except Exception as e:
                logger.error(f"Error polling job {job_id}: {e}")
                self._handle_job_failure(job_id, artifact_id, str(e), "job_failed")
                jobs_processed += 1

        return jobs_processed

    def apply_status(
        self,
        job_id: str,
        artifact_id: str,
        provider: Provider,
        provider_job_id: str,
        status: str,
    ) -> None:
        """Apply a provider-reported status to a job, using the same
        completion policy regardless of how the status was learned (a
        poller's own `provider.status()` call, or a provider-pushed webhook
        event carrying a terminal status). Shared by `poll_once()` and
        `surface.webhook.handle_webhook_payload()` — see that module's
        docstring for the design rationale.

        Args:
            job_id: Store job ID.
            artifact_id: Artifact being generated.
            provider: Provider instance for this job.
            provider_job_id: Provider's job ID.
            status: One of "succeeded", "failed", "cancelled", "queued", "running".
        """
        if status == "succeeded":
            self._handle_job_success(job_id, artifact_id, provider, provider_job_id)

        elif status == "failed":
            self._handle_job_failure(job_id, artifact_id, "Provider returned failed", "job_failed")

        elif status == "cancelled":
            # Job was cancelled by provider or user
            self.store.update_job_status(job_id, "cancelled")
            logger.info(f"Job {job_id} cancelled at provider")

        elif status in ("queued", "running"):
            # Still in progress, nothing to do yet.
            logger.debug(f"Job {job_id} still {status}, will check again later")

        else:
            logger.warning(f"Unknown status {status} for job {job_id}")

    def _handle_job_success(self, job_id: str, artifact_id: str, provider: Provider, provider_job_id: str) -> None:
        """Handle successful job completion.

        Args:
            job_id: Store job ID.
            artifact_id: Artifact being generated.
            provider: Provider that completed the job.
            provider_job_id: Provider's job ID.
        """
        try:
            # Verify job was not cancelled
            job = self.store.get_job(job_id)
            if job is None:
                logger.warning(f"Job {job_id} not found, skipping success handling")
                return
            if job["cancel_requested"] or job["status"] == "cancelled":
                logger.info(f"Job {job_id} was cancelled, ignoring late success")
                return

            # Collect result from provider
            result = provider.collect(provider_job_id)
            content_ref = result.get("content_ref")
            content_type = result.get("content_type", "text/plain")
            metadata = result.get("metadata", {})
            # SPEC section 39: track actual provider cost where available.
            # Providers report this via an "actual_cost" key on collect()'s
            # result; not all providers will have one, so default to None
            # and only pass cost_actual through to the store when present.
            actual_cost = result.get("actual_cost")

            # Create new artifact version with result. Use the job_id as the
            # idempotency key: job_id is stable and unique per job, so a
            # crash/restart that re-observes the same provider success will
            # dedupe against the version already created for this job instead
            # of creating a duplicate (SPEC section 16, section 59).
            version_result = self.store.put_version(
                artifact_id,
                content_ref=content_ref,
                content_type=content_type,
                params=metadata,
                created_by="poller",
                note="Generated via provider",
                source_event_id=f"job:{job_id}",
                select=True,  # Auto-select unless cancelled
            )

            # A successful generation moves the artifact out of "generating"
            # into "review" so a human can act on it (SPEC section 11).
            self.store.set_status(artifact_id, "review")

            # Update job status to succeeded with result, recording actual
            # cost (SPEC section 39) when the provider reported one.
            self.store.update_job_status(
                job_id,
                "succeeded",
                result={
                    "content_ref": content_ref,
                    "content_type": content_type,
                    "version_id": version_result.get("version_id"),
                    "metadata": metadata,
                },
                cost_actual=actual_cost,
            )

            # Emit job_done event
            self.store.enqueue_event(
                type="job_done",
                payload={
                    "job_id": job_id,
                    "artifact_id": artifact_id,
                    "content_ref": content_ref,
                    "version_id": version_result.get("version_id"),
                },
                artifact_id=artifact_id,
            )

            logger.info(f"Job {job_id} succeeded, created version {version_result.get('version_id')}")

        except Exception as e:
            logger.error(f"Error handling success for job {job_id}: {e}")
            self._handle_job_failure(job_id, artifact_id, f"Success handling failed: {e}", "job_failed")

    def _handle_job_failure(self, job_id: str, artifact_id: str, error_message: str, event_type: str) -> None:
        """Handle job failure or error.

        Args:
            job_id: Store job ID.
            artifact_id: Artifact being generated.
            error_message: Error description.
            event_type: Event type to emit (usually "job_failed").
        """
        try:
            self.store.update_job_status(job_id, "failed", result={"error": error_message})
            # A failed generation must not leave the artifact stuck showing
            # "generating" forever with no way for the poller's own state to
            # ever change it again.
            self.store.set_status(artifact_id, "failed")
            self.store.enqueue_event(
                type=event_type,
                payload={
                    "job_id": job_id,
                    "artifact_id": artifact_id,
                    "error": error_message,
                },
                artifact_id=artifact_id,
            )
            logger.error(f"Job {job_id} failed: {error_message}")
        except Exception as e:
            logger.error(f"Error marking job {job_id} as failed: {e}")
