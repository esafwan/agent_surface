"""Provider webhook ingestion helper.

Per SPEC section 27 ("Poller / Provider Adapter"), the poller "MAY poll
APIs, receive webhooks, watch subprocesses, or watch files" — webhooks are
an explicitly valid alternative to polling for discovering job completion.

Scope boundary: this module is NOT an HTTP server and adds no web
framework dependency (no Flask/FastAPI/etc). It only provides the payload
parsing + job lookup + completion logic that a future HTTP layer (or a CLI
command) would call once it has already received and deserialized an
inbound request body into a plain dict. Wiring an actual HTTP endpoint to
call `handle_webhook_payload()` is left to a later pass.

Design choice — code sharing with the poller:
`handle_webhook_payload()` constructs a `Poller` instance (or accepts one)
and delegates the actual state-transition/cancellation-safety logic to
`Poller.apply_status()`, the same method `poll_once()` uses once it has a
status string in hand. This keeps exactly one implementation of "what
happens on success/failure/cancellation" (idempotent version creation,
cancellation-safety check, event emission, cost recording) rather than
duplicating it here. The only thing this module adds on top is: parsing
the webhook payload shape and scanning queued/running jobs for the one
whose `provider_job_id` matches, since the store has no direct
"get job by provider_job_id" lookup.
"""

import logging
from typing import Any, Dict, Optional

from surface.poller import Poller
from surface.providers.base import Provider
from surface.store import Store

logger = logging.getLogger(__name__)


# WebhookEvent shape (documented, not enforced as a class — providers will
# send arbitrary-ish JSON and callers can normalize into this shape before
# calling handle_webhook_payload, or pass it directly if it already matches):
#
#   {
#       "provider_job_id": "img_ab12cd34_p3",   # required: provider's job id
#       "status": "succeeded" | "failed" | "cancelled" | "running" | "queued",
#       "result": {...},  # optional, currently unused directly — providers
#                         # report their result via provider.collect(), which
#                         # apply_status() calls internally on "succeeded"
#   }


def _find_job_by_provider_job_id(store: Store, provider_job_id: str) -> Optional[Dict[str, Any]]:
    """Scan non-terminal jobs for the one matching a provider_job_id.

    The store has no direct "get job by provider_job_id" index, so this
    scans the same "queued" + "running" sets the poller itself scans in
    `poll_once()`. A webhook for a job already in a terminal state
    (succeeded/failed/cancelled) will simply not be found here, which is
    fine — there's nothing left to apply.
    """
    for status in ("queued", "running"):
        for job in store.list_jobs_by_status(status):
            if job.get("provider_job_id") == provider_job_id:
                return job
    return None


def handle_webhook_payload(
    payload: Dict[str, Any],
    store: Store,
    providers: Dict[str, Provider],
) -> Dict[str, Any]:
    """Handle one inbound provider webhook payload.

    Looks up the in-flight job by `payload["provider_job_id"]` and, if
    found, applies the same success/failure/cancellation completion policy
    the poller would apply on its next `poll_once()` — via
    `Poller.apply_status()` — instead of waiting for the poller to
    discover completion by polling.

    Args:
        payload: Dict with at least "provider_job_id" and "status" keys.
                 See the WebhookEvent shape documented above this function.
        store: Store instance for job/artifact lookups.
        providers: Dict mapping provider name/kind to Provider instances,
                   same shape as passed to `Poller(store, providers)`.

    Returns:
        Dict describing the outcome:
            {"ok": True, "job_id": ..., "status": ...} on success,
            {"ok": False, "error": "..."} if the payload is malformed,
            the provider_job_id is unknown/not in-flight, or the job's
            provider is not registered.
    """
    provider_job_id = payload.get("provider_job_id")
    status = payload.get("status")

    if not provider_job_id:
        return {"ok": False, "error": "missing provider_job_id"}
    if not status:
        return {"ok": False, "error": "missing status"}

    job = _find_job_by_provider_job_id(store, provider_job_id)
    if job is None:
        logger.warning(f"Webhook for unknown/not-in-flight provider_job_id: {provider_job_id}")
        return {"ok": False, "error": f"no in-flight job found for provider_job_id: {provider_job_id}"}

    job_id = job["id"]
    artifact_id = job["artifact_id"]
    provider_name = job["provider"]

    provider = providers.get(provider_name)
    if not provider:
        logger.warning(f"No provider found for {provider_name}, marking job {job_id} as failed")
        poller = Poller(store, providers)
        poller._handle_job_failure(job_id, artifact_id, f"Unknown provider: {provider_name}", "job_failed")
        return {"ok": False, "error": f"unknown provider: {provider_name}"}

    # Cancellation safety: a late-arriving webhook success for a job the
    # user already cancelled must be rejected the same way a late poll
    # would be. Poller.apply_status -> _handle_job_success already
    # re-checks cancel_requested/status against the store before doing
    # anything, so this falls out "for free" from delegating to it — but we
    # also short-circuit obviously-cancelled jobs here for a clearer result.
    if job.get("cancel_requested") or job.get("status") == "cancelled":
        logger.info(f"Webhook for job {job_id} ignored: job was cancelled")
        return {"ok": False, "error": "job was cancelled; webhook ignored", "job_id": job_id}

    poller = Poller(store, providers)
    poller.apply_status(job_id, artifact_id, provider, provider_job_id, status)

    final_job = store.get_job(job_id)
    return {"ok": True, "job_id": job_id, "status": final_job["status"] if final_job else status}
