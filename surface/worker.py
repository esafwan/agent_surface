"""
Reference worker for Agent Surface Board Phase 0.

A standalone, runnable worker that interprets inbox events and writes through the store.
Does NOT require any LLM/AI logic — just deterministic event handling.

Per SPEC sections 13-25 (User/System Event Types, Event Delivery, Idempotency, Store/Inbox Tools).

Event envelope from native_stream.py:
  {
    "type": "event",
    "event_id": "evt_...",
    "payload": {...},
    "project_id": "...",
    "artifact_id": "...",
    "config": {...},
    "artifact": {...},
    "summary": {...}
  }

Response format (checked by supervisor.py):
  {"ok": true} or {"ok": false, "error": "..."}
"""

import json
import sys
import argparse
from typing import Any, Dict, Optional

from surface.store import Store
from surface.stages.config import StageConfig, StageConfigError


def handle_event(event: Dict[str, Any], store: Store) -> Dict[str, Any]:
    """
    Handle a single event from the inbox.

    Per SPEC section 22: Worker processes event and writes via store tools.
    All effects are idempotent (use source_event_id where available).

    Args:
        event: Event dict with keys: event_id, type, payload, artifact_id, etc.
        store: Store instance for all mutations.

    Returns:
        Result dict with "ok" key (required by transport).
    """
    event_id = event.get("event_id")
    event_type = event.get("type")
    payload = event.get("payload", {})
    artifact_id = event.get("artifact_id")
    config = event.get("config", {})

    try:
        # Handle user event types (SPEC section 13)
        if event_type == "edit":
            return _handle_edit(artifact_id, payload, event_id, store)

        elif event_type == "revise":
            return _handle_revise(artifact_id, payload, event_id, store, config)

        elif event_type == "regenerate":
            return _handle_regenerate(artifact_id, payload, event_id, store, config)

        elif event_type == "select_version":
            return _handle_select_version(artifact_id, payload, event_id, store)

        elif event_type == "approve":
            return _handle_approve(artifact_id, payload, event_id, store)

        elif event_type == "reopen":
            return _handle_reopen(artifact_id, payload, event_id, store)

        elif event_type == "cancel":
            return _handle_cancel(artifact_id, payload, event_id, store)

        elif event_type == "lock":
            return _handle_lock(artifact_id, payload, event_id, store)

        elif event_type == "unlock":
            return _handle_unlock(artifact_id, payload, event_id, store)

        elif event_type == "message":
            return _handle_message(artifact_id, payload, event_id, store)

        # Handle system event types (SPEC section 14)
        elif event_type == "job_done":
            return _handle_job_done(payload, event_id, store)

        elif event_type == "job_failed":
            return _handle_job_failed(payload, event_id, store)

        elif event_type == "stale":
            return _handle_stale(payload, event_id, store)

        elif event_type == "worker_recovered":
            return _handle_worker_recovered(payload, event_id, store)

        else:
            return {"ok": False, "error": f"Unknown event type: {event_type}"}

    except Exception as e:
        return {"ok": False, "error": f"Exception in {event_type}: {str(e)}"}


def _handle_edit(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle edit action: create a new version with new content.
    Typically for text artifacts.
    Per SPEC section 13: edit example has payload with "content".
    """
    if not artifact_id:
        return {"ok": False, "error": "edit requires artifact_id"}

    content = payload.get("content")
    if content is None:
        return {"ok": False, "error": "edit payload must contain 'content'"}

    artifact = store.get_artifact(artifact_id)
    if not artifact:
        return {"ok": False, "error": f"Artifact {artifact_id} not found"}

    try:
        result = store.put_version(
            artifact_id,
            content=content,
            source_event_id=event_id,
            select=True,
            created_by="worker",
        )
        return {"ok": True, "version_id": result.get("version_id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Artifact types that represent async provider generation (image/video jobs
# via _create_generation_job). Every other VALID_ARTIFACT_TYPES value (text,
# form, diff, file, audio) is revised as a direct new version instead.
_GENERATION_ARTIFACT_TYPES = ("image", "video")


def _handle_revise(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Handle revise action.
    For text artifacts: create a new version with the note.
    For image/video artifacts: create a job.
    Per SPEC section 13: revise example has payload with "note".
    """
    if not artifact_id:
        return {"ok": False, "error": "revise requires artifact_id"}

    note = payload.get("note", "")

    artifact = store.get_artifact(artifact_id)
    if not artifact:
        return {"ok": False, "error": f"Artifact {artifact_id} not found"}

    # Determine artifact type from config
    artifact_type = _get_artifact_type_for_artifact(artifact_id, artifact, config)

    # Only image/video artifact types represent async generation work (see
    # _GENERATION_ARTIFACT_TYPES). Every other type — including "form",
    # "diff", "file" (SPEC section 30's VALID_ARTIFACT_TYPES) — must be
    # revised the same way "text" is: a new version, not a generation job.
    # A prior bug treated "anything that isn't text" as generation, so a
    # revise on a questionnaire ("form") artifact incorrectly tried to spin
    # up an image/video job.
    if artifact_type not in _GENERATION_ARTIFACT_TYPES:
        # Use selected version's content as basis, revise with note
        if artifact["selected_version_id"]:
            selected_ver = store.get_version(artifact["selected_version_id"])
            if selected_ver:
                content = selected_ver.get("content", "")
            else:
                content = ""
        else:
            content = ""

        try:
            result = store.put_version(
                artifact_id,
                content=content,  # Could be enhanced by worker reasoning
                prompt=None,
                note=note,
                source_event_id=event_id,
                select=True,
                created_by="worker",
            )
            return {"ok": True, "version_id": result.get("version_id")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # For image/video artifacts, create a job
    else:
        cost_estimate = float(payload.get("cost_estimate", 0.0) or 0.0)
        return _create_generation_job(
            artifact_id, artifact, event_id, store, config,
            note=note, cost_estimate=cost_estimate,
        )


def _handle_regenerate(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Handle regenerate action: create a job for image/video generation.
    Per SPEC section 26: Worker flow: receive event → construct request → store job → set artifact `generating` → end turn.
    """
    if not artifact_id:
        return {"ok": False, "error": "regenerate requires artifact_id"}

    artifact = store.get_artifact(artifact_id)
    if not artifact:
        return {"ok": False, "error": f"Artifact {artifact_id} not found"}

    cost_estimate = float(payload.get("cost_estimate", 0.0) or 0.0)
    return _create_generation_job(
        artifact_id, artifact, event_id, store, config, cost_estimate=cost_estimate
    )


_JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


def _find_job_for_event(store: Store, artifact_id: str, event_id: str) -> Optional[Dict[str, Any]]:
    """Find an existing job created for this (artifact_id, event_id), if any.

    store.create_job has no built-in dedupe key, so this is the worker's
    idempotency guard against redelivered events (e.g. the supervisor's
    bounded retry) creating duplicate provider jobs.
    """
    for status in _JOB_STATUSES:
        for job in store.list_jobs_by_status(status):
            if job.get("artifact_id") != artifact_id:
                continue
            if job.get("request", {}).get("source_event_id") == event_id:
                return job
    return None


def _create_generation_job(
    artifact_id: str,
    artifact: Dict[str, Any],
    event_id: str,
    store: Store,
    config: Dict[str, Any],
    note: str = "",
    cost_estimate: float = 0.0,
) -> Dict[str, Any]:
    """
    Create a generation job for an artifact.
    Sets artifact status to 'generating' and returns immediately.
    Worker MUST NOT poll for completion (SPEC section 26).

    Per SPEC section 42: a locked artifact ("persistent instruction that
    automation MUST NOT regenerate/replace without confirmation") MUST NOT
    be silently regenerated.
    Per SPEC section 39: budget limits MUST be enforced deterministically
    outside LLM reasoning, before the job (and its cost) is committed.
    """
    # S8: reject regeneration of a locked artifact outright.
    if artifact.get("locked"):
        return {"ok": False, "error": "artifact is locked"}

    try:
        # Get stage config to find provider and kind
        stage_id = artifact["stage"]
        provider = _get_provider_for_stage(stage_id, config)
        artifact_type = _get_artifact_type_for_stage(stage_id, config)

        if not provider:
            provider = "default"

        kind = artifact_type or "text"

        # Idempotency: store.create_job has no built-in dedupe key, so a
        # retried event (SPEC section 40; also the S3 bounded-retry path)
        # would otherwise create a second, duplicate provider job. Stamp the
        # originating event_id into the request and check for an existing
        # job with the same (artifact_id, source_event_id) across every job
        # status before creating a new one.
        existing_job = _find_job_for_event(store, artifact_id, event_id)
        if existing_job is not None:
            return {"ok": True, "job_id": existing_job["id"]}

        # Construct request with artifact's selected version content/prompt
        request = {"note": note, "source_event_id": event_id}
        if artifact["selected_version_id"]:
            selected_ver = store.get_version(artifact["selected_version_id"])
            if selected_ver:
                if selected_ver.get("content"):
                    request["content"] = selected_ver["content"]
                if selected_ver.get("prompt"):
                    request["prompt"] = selected_ver["prompt"]

        # S7: enforce budget limits (SPEC section 39) before creating the job.
        # cost_estimate comes from the caller (the event payload), NOT from
        # `request` — `request` is a dict this function itself constructs
        # for the provider and never contains a cost_estimate key, so reading
        # it from there always evaluated to 0.0 and made enforcement inert.
        budget_error = _check_budget(config, stage_id, cost_estimate, store)
        if budget_error:
            return {"ok": False, "error": budget_error}

        # Create job (idempotent via source_event_id check would need to be in job creation)
        job = store.create_job(
            artifact_id=artifact_id,
            provider=provider,
            kind=kind,
            request=request,
            cost_estimate=cost_estimate,
        )

        # Set artifact status to 'generating'
        store.set_status(artifact_id, "generating")

        return {"ok": True, "job_id": job.get("id")}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def _check_budget(
    config: Dict[str, Any],
    stage_id: str,
    estimated_cost: float,
    store: Store,
) -> Optional[str]:
    """
    Validate a prospective job's cost against the stage config's budget
    block via StageConfig.validate_budget_limit, if a budget is configured.

    Per SPEC section 39: "Budget limits MUST be enforced deterministically
    outside LLM reasoning." This is the deterministic enforcement point,
    called before store.create_job() for any generation job.

    Returns an error message string if the job should be rejected, or None
    if it's within budget (or no budget is configured).
    """
    if not config or not config.get("budget"):
        return None

    try:
        stage_config = StageConfig(config)
    except StageConfigError:
        # Config isn't a valid/complete StageConfig payload; nothing to
        # enforce against deterministically, so don't block on it.
        return None

    current_project_cost = _sum_job_costs(store, stage_id=None)
    current_stage_cost = _sum_job_costs(store, stage_id=stage_id, config=config)

    result = stage_config.validate_budget_limit(
        estimated_cost=estimated_cost,
        current_project_cost=current_project_cost,
        current_stage_cost=current_stage_cost,
        stage_id=stage_id,
    )

    if result.get("exceeds_project_budget"):
        return "budget exceeded: project_usd limit would be exceeded"
    if result.get("exceeds_stage_budget"):
        return "budget exceeded: stage_usd limit would be exceeded"
    return None


def _sum_job_costs(
    store: Store,
    stage_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Sum cost_estimate across all non-cancelled/non-failed jobs, optionally
    restricted to jobs whose artifact belongs to a given stage.

    Used to compute cumulative project/stage cost for deterministic budget
    enforcement (SPEC section 39).
    """
    total = 0.0
    for status in ("queued", "running", "succeeded"):
        for job in store.list_jobs_by_status(status):
            if stage_id is not None:
                artifact = store.get_artifact(job["artifact_id"])
                if not artifact or artifact.get("stage") != stage_id:
                    continue
            total += float(job.get("cost_estimate") or 0.0)
    return total


def _handle_select_version(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle select_version action.
    This is normally deterministic (handled by board.py), but worker may also receive it.
    """
    if not artifact_id:
        return {"ok": False, "error": "select_version requires artifact_id"}

    version_id = payload.get("version_id")
    if not version_id:
        return {"ok": False, "error": "select_version payload must contain 'version_id'"}

    try:
        result = store.select_version(artifact_id, version_id)
        if result.get("ok"):
            return {"ok": True, "selected_version_id": version_id}
        else:
            return {"ok": False, "error": result.get("error", "selection failed")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_approve(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle approve action: set artifact status to 'approved'.
    This is normally deterministic (handled by board.py), but worker may also receive it.
    """
    if not artifact_id:
        return {"ok": False, "error": "approve requires artifact_id"}

    try:
        store.set_status(artifact_id, "approved")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_reopen(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle reopen action: set artifact status back to 'draft' for further work.
    """
    if not artifact_id:
        return {"ok": False, "error": "reopen requires artifact_id"}

    try:
        store.set_status(artifact_id, "draft")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_cancel(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle cancel action: request cancellation of in-progress generation job.
    Per SPEC section 28: Only sets cancel_requested; poller will call provider.cancel().
    """
    if not artifact_id:
        return {"ok": False, "error": "cancel requires artifact_id"}

    try:
        # Find the active job (queued or running) for this artifact
        jobs = store.list_jobs_by_status("queued")
        jobs.extend(store.list_jobs_by_status("running"))

        active_job = None
        for job in jobs:
            if job["artifact_id"] == artifact_id:
                active_job = job
                break

        if not active_job:
            return {"ok": False, "error": f"No active job found for artifact {artifact_id}"}

        store.cancel_job(active_job["id"])
        return {"ok": True, "job_id": active_job["id"]}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_lock(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle lock action: prevent automated regeneration.
    This is normally deterministic (handled by board.py), but worker may also receive it.
    """
    if not artifact_id:
        return {"ok": False, "error": "lock requires artifact_id"}

    try:
        store.set_lock(artifact_id, True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_unlock(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle unlock action: allow automated regeneration.
    This is normally deterministic (handled by board.py), but worker may also receive it.
    """
    if not artifact_id:
        return {"ok": False, "error": "unlock requires artifact_id"}

    try:
        store.set_lock(artifact_id, False)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_message(
    artifact_id: Optional[str],
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle message action: free-form escape hatch.
    No required store mutation; just acknowledge.
    Per SPEC section 13: message is the free-form escape hatch.
    """
    # Message is acknowledged but no action taken
    return {"ok": True}


def _handle_job_done(
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle job_done system event.
    Per SPEC section 14: emitted when a job completes successfully.
    Worker acknowledges; poller handles version creation.
    """
    # For Phase 0: acknowledge but no action
    # In Phase 1: could do intelligent version selection, etc.
    return {"ok": True}


def _handle_job_failed(
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle job_failed system event.
    Per SPEC section 14: emitted when a job fails.
    Worker acknowledges; may update artifact status.
    """
    # For Phase 0: acknowledge but no action
    return {"ok": True}


def _handle_stale(
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle stale system event.
    Per SPEC section 12: emitted when upstream dependency changes.
    Worker acknowledges; artifact already marked stale by store.
    """
    # Stale is already marked by store; worker just acknowledges
    return {"ok": True}


def _handle_worker_recovered(
    payload: Dict[str, Any],
    event_id: str,
    store: Store,
) -> Dict[str, Any]:
    """
    Handle worker_recovered system event.
    Per SPEC section 21/23: emitted when supervisor recovers from worker crash.
    Worker acknowledges and rehydrates if needed.
    """
    # For Phase 0: acknowledge
    return {"ok": True}


def _get_artifact_type_for_artifact(
    artifact_id: str,
    artifact: Dict[str, Any],
    config: Dict[str, Any],
) -> str:
    """
    Determine artifact type from config using stage.
    Returns "text", "image", "video", or "unknown".
    """
    return _get_artifact_type_for_stage(artifact.get("stage", ""), config)


def _get_artifact_type_for_stage(stage_id: str, config: Dict[str, Any]) -> str:
    """
    Look up artifact_type for a stage from config.
    """
    stages = config.get("stages", [])
    for stage in stages:
        if stage.get("id") == stage_id:
            return stage.get("artifact_type", "text")
    return "text"


def _get_provider_for_stage(stage_id: str, config: Dict[str, Any]) -> Optional[str]:
    """
    Look up generation provider for a stage from config.
    """
    stages = config.get("stages", [])
    for stage in stages:
        if stage.get("id") == stage_id:
            gen_config = stage.get("generation", {})
            return gen_config.get("provider")
    return None


def run_worker(db_path: str) -> None:
    """
    Main worker loop.
    Reads JSON event envelopes from stdin, processes them, writes results to stdout.
    Per SPEC section 20: native stream transport uses JSON/JSONL over stdin/stdout.
    """
    store = Store(db_path)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                result = handle_event(event, store)
            except json.JSONDecodeError as e:
                result = {"ok": False, "error": f"Invalid JSON: {str(e)}"}
            except Exception as e:
                result = {"ok": False, "error": f"Processing error: {str(e)}"}

            # Write response as JSON line (must have "ok" key)
            response_line = json.dumps(result)
            print(response_line)
            sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Log to stderr, exit gracefully
        print(json.dumps({"ok": False, "error": f"Fatal error: {str(e)}"}))
        sys.stdout.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Agent Surface Board reference worker (Phase 0)"
    )
    parser.add_argument(
        "--db",
        type=str,
        default=":memory:",
        help="Path to SQLite database (default: :memory:)",
    )
    args = parser.parse_args()

    run_worker(args.db)
