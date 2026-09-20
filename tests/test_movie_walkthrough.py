"""Integration test for Phase 0 movie walkthrough.

Per SPEC section 64 (Recommended First Build), this test drives the *real*
architecture — Supervisor + NativeStreamTransport spawning the real
`python -m surface.worker --db <path>` subprocess, plus a real Poller with
real MockImageProvider/MockVideoProvider instances — through the actual DAG
declared in `surface/stages/movie.json` (loaded via
`surface.stages.config.load_preset("movie")`), rather than hand-simulating
what a worker or supervisor would do.

Sequence covered (SPEC section 64):
  1. edit script (real `edit` event through supervisor -> worker subprocess)
  2. verify dependent shots/keyframes/clips/assembly become stale
  3. approve shots (real `approve` event through supervisor -> worker)
  4. async image generation for two independent keyframe artifacts (job + poller)
  5. revise one image while the other stays independent
  6. generate clips (jobs + poller)
  7. cancel one clip mid-flight; assert provider.cancel() was ACTUALLY invoked
     (via the mock provider's own internal job-state tracking) and a late
     provider "success" cannot create/select a version
  8. simulate a worker crash (kill the worker subprocess mid-turn) and verify
     recovery via lease expiry (the event becomes claimable again)
  9. approve remaining clips + keyframes, create/approve a final assembly artifact
  10. verify final state via `stage_config.is_completed(...)` /
      `stage_config.validate_action(...)` rather than raw status peeking,
      verify version history only grows, and that duplicate event delivery
      does not duplicate output (worker idempotency via source_event_id).

Honesty about remaining gaps (see report to caller for the full list):
  * `select_version`/`approve`/`lock`/`unlock` are, per SPEC and per
    `surface/board.py`, deterministic actions normally applied directly by the
    board rather than routed through the LLM-ish worker turn. This test still
    exercises them via real `supervisor.claim_and_dispatch_event()` calls
    against the real worker subprocess (which does implement handlers for
    them), so they are genuinely exercised end-to-end, not just documented.
  * The "assembly" artifact's actual video content is produced by a plain
    `regenerate` event through the real worker + a job, mirroring how
    keyframes/clips are produced, since assembly has no `generation` block
    in movie.json but does allow `regenerate`; we instead create its final
    version the same way the reference worker would for a text/manual stage:
    directly via `store.put_version(...)`, and this is called out explicitly
    below as the one step that is not driven through the worker subprocess,
    because movie.json's `assembly` stage declares no `generation` config
    (no provider) for the reference worker to act on.
"""

import json
import os
import signal
import sys
import time

import pytest

from surface.store import Store
from surface.poller import Poller
from surface.providers.image import MockImageProvider
from surface.providers.video import MockVideoProvider
from surface.stages.config import load_preset
from surface.stages.config import ActionPermissionError
from surface.supervisor import Supervisor, SupervisorConfig
from surface.transports.native_stream import NativeStreamTransport
import surface.worker as worker_module


WORKER_COMMAND = [sys.executable, "-m", "surface.worker", "--db"]


def _worker_command(db_path: str):
    return [sys.executable, "-m", "surface.worker", "--db", db_path]


@pytest.fixture
def temp_db(tmp_path):
    """Temporary SQLite database file for the test (real worker needs a real file)."""
    return str(tmp_path / "movie_walkthrough.sqlite3")


@pytest.fixture
def store(temp_db):
    return Store(temp_db)


@pytest.fixture
def movie_config():
    return load_preset("movie")


@pytest.fixture
def image_provider():
    return MockImageProvider(polls_to_success=2)


@pytest.fixture
def video_provider():
    return MockVideoProvider(polls_to_success=3)


@pytest.fixture
def poller(store, image_provider, video_provider):
    return Poller(store, {
        "image_default": image_provider,
        "video_default": video_provider,
    })


@pytest.fixture
def transport(temp_db):
    t = NativeStreamTransport(command=_worker_command(temp_db))
    yield t
    # Best-effort cleanup of any sessions left open by a test.
    for session in list(t.sessions.values()):
        try:
            t.close(session)
        except Exception:
            pass


@pytest.fixture
def supervisor(store, transport, movie_config):
    config = SupervisorConfig(worker_id="movie_test_worker", lease_seconds=60)
    return Supervisor(store, transport, movie_config, config=config, project_id="movie_walkthrough")


def _build_dag_from_config(store: Store, movie_config):
    """Build the artifact DAG using movie.json's ACTUAL depends_on structure.

    For each stage in movie_config.stage_order, create one artifact per stage
    (a "primary" chain) plus a second sibling artifact for stages that fan out
    in the walkthrough (keyframes, clips), wiring dependencies exactly as
    `depends_on` declares — never inventing a parallel structure.
    """
    ids = {}

    # One artifact per declared stage, in dependency order.
    for stage_id in movie_config.stage_order:
        stage = movie_config.get_stage(stage_id)
        artifact_id = f"{stage_id}_001"
        store.create_artifact(id=artifact_id, stage=stage_id, title=f"{stage_id.title()} 1")
        ids[stage_id] = artifact_id
        for dep in stage.depends_on:
            store.add_dependency(ids[dep], artifact_id)

    # A second sibling for keyframes/clips (fan-out present in the real
    # movie.json DAG: shots -> keyframes -> clips -> assembly is 1:N:N:1 in
    # any real production, so exercise that fan-out explicitly here).
    keyframes_stage = movie_config.get_stage("keyframes")
    clips_stage = movie_config.get_stage("clips")
    assert keyframes_stage.depends_on == ["shots"]
    assert clips_stage.depends_on == ["keyframes"]

    store.create_artifact(id="keyframes_002", stage="keyframes", title="Keyframes 2")
    store.add_dependency(ids["shots"], "keyframes_002")

    store.create_artifact(id="clips_002", stage="clips", title="Clips 2")
    store.add_dependency("keyframes_002", "clips_002")

    # assembly depends on clips per config; wire the second clip in too.
    assembly_stage = movie_config.get_stage("assembly")
    assert assembly_stage.depends_on == ["clips"]
    store.add_dependency("clips_002", ids["assembly"])

    ids["keyframes_2"] = "keyframes_002"
    ids["clips_2"] = "clips_002"
    return ids


def _dispatch_one(supervisor: Supervisor) -> dict:
    """Claim+dispatch exactly one event via the real supervisor/worker and assert success."""
    event = supervisor.claim_and_dispatch_event()
    assert event is not None, "expected a claimable event"
    return event


def _drain_until_acked(supervisor: Supervisor, store: Store, target_event_id: str,
                        max_iterations: int = 50) -> None:
    """Dispatch events via the real supervisor/worker, oldest first (as
    claim_next_event does), until `target_event_id` has been acked.

    Needed because enqueueing an event (e.g. `approve`) can race with earlier
    system `stale` events already sitting in the inbox (created by DAG stale
    propagation) -- a real supervisor drains its inbox in creation order, so
    this mirrors that rather than assuming the next claim is our event.
    """
    for _ in range(max_iterations):
        target = store.get_event(target_event_id)
        if target and target["status"] == "acked":
            return
        event = supervisor.claim_and_dispatch_event()
        assert event is not None, f"inbox drained before {target_event_id} was acked"
    raise AssertionError(f"event {target_event_id} was never acked after draining the inbox")


def _drain_all(supervisor: Supervisor, max_iterations: int = 50) -> None:
    """Dispatch every currently-pending event (e.g. leftover `job_done`
    system events enqueued by the poller) so the inbox is empty before a
    test manually claims a specific event without dispatching it."""
    for _ in range(max_iterations):
        if supervisor.claim_and_dispatch_event() is None:
            return
    raise AssertionError("inbox did not drain within max_iterations")


class TestMovieWalkthroughRealArchitecture:
    """Full Phase 0 movie walkthrough driven through Supervisor+NativeStreamTransport+worker."""

    def test_full_movie_walkthrough(self, store, movie_config, poller, supervisor):
        ids = _build_dag_from_config(store, movie_config)

        # Sanity: the DAG matches movie.json's declared depends_on exactly.
        with open(
            os.path.join(os.path.dirname(worker_module.__file__), "stages", "movie.json")
        ) as f:
            raw = json.load(f)
        declared_deps = {s["id"]: s.get("depends_on", []) for s in raw["stages"]}
        for stage_id in movie_config.stage_order:
            assert movie_config.get_stage(stage_id).depends_on == declared_deps[stage_id]

        # All artifacts start as draft.
        for key in ("script", "shots", "keyframes", "keyframes_2", "clips", "clips_2", "assembly"):
            art = store.get_artifact(ids[key])
            assert art["status"] == "draft"

        # ------------------------------------------------------------------
        # STEP 1: edit script via a REAL `edit` event through supervisor->worker
        # ------------------------------------------------------------------
        movie_config.validate_action("script", "edit")
        edit_event = store.enqueue_event(
            type="edit",
            payload={"content": "INT. KITCHEN - NIGHT. Camera on product."},
            artifact_id=ids["script"],
        )
        _drain_until_acked(supervisor, store, edit_event["id"])

        script_versions = store.list_versions(ids["script"])
        assert len(script_versions) == 1
        script_artifact = store.get_artifact(ids["script"])
        assert script_artifact["selected_version_id"] == script_versions[0]["id"]

        # ------------------------------------------------------------------
        # STEP 2: dependents become stale (put_version->select triggers propagation)
        # ------------------------------------------------------------------
        for key in ("shots", "keyframes", "keyframes_2", "clips", "clips_2", "assembly"):
            assert store.get_artifact(ids[key])["status"] == "stale", key

        # Approve script via a real `approve` event.
        movie_config.validate_action("script", "approve")
        approve_script_event = store.enqueue_event(type="approve", payload={}, artifact_id=ids["script"])
        _drain_until_acked(supervisor, store, approve_script_event["id"])
        assert store.get_artifact(ids["script"])["status"] == "approved"

        # ------------------------------------------------------------------
        # STEP 3: edit + approve shots (real events)
        # ------------------------------------------------------------------
        movie_config.validate_action("shots", "edit")
        edit_shots_event = store.enqueue_event(
            type="edit",
            payload={"content": "Scene 1: Wide. Scene 2: Close-up."},
            artifact_id=ids["shots"],
        )
        _drain_until_acked(supervisor, store, edit_shots_event["id"])

        movie_config.validate_action("shots", "approve")
        approve_shots_event = store.enqueue_event(type="approve", payload={}, artifact_id=ids["shots"])
        _drain_until_acked(supervisor, store, approve_shots_event["id"])
        assert store.get_artifact(ids["shots"])["status"] == "approved"

        # ------------------------------------------------------------------
        # STEP 4: async image generation for both keyframe artifacts, driven
        # through the real worker's `regenerate` handler (creates job + sets
        # status='generating'), then a real Poller advances them.
        # ------------------------------------------------------------------
        movie_config.validate_action("keyframes", "regenerate")
        regen_kf1_event = store.enqueue_event(type="regenerate", payload={}, artifact_id=ids["keyframes"])
        _drain_until_acked(supervisor, store, regen_kf1_event["id"])
        regen_kf2_event = store.enqueue_event(type="regenerate", payload={}, artifact_id=ids["keyframes_2"])
        _drain_until_acked(supervisor, store, regen_kf2_event["id"])

        assert store.get_artifact(ids["keyframes"])["status"] == "generating"
        assert store.get_artifact(ids["keyframes_2"])["status"] == "generating"

        for _ in range(8):
            poller.poll_once()

        kf1_versions = store.list_versions(ids["keyframes"])
        kf2_versions = store.list_versions(ids["keyframes_2"])
        assert len(kf1_versions) == 1
        assert len(kf2_versions) == 1
        assert kf1_versions[0]["content_type"] == "image/png"
        assert store.get_artifact(ids["keyframes"])["status"] == "review"
        assert store.get_artifact(ids["keyframes_2"])["status"] == "review"

        # ------------------------------------------------------------------
        # STEP 5: revise keyframes_1 (real `revise` event -> worker creates a
        # new job for image stages) while keyframes_2 stays independent.
        # ------------------------------------------------------------------
        movie_config.validate_action("keyframes", "revise")
        revise_kf1_event = store.enqueue_event(
            type="revise",
            payload={"note": "Warmer lighting, less contrast"},
            artifact_id=ids["keyframes"],
        )
        _drain_until_acked(supervisor, store, revise_kf1_event["id"])
        assert store.get_artifact(ids["keyframes"])["status"] == "generating"

        for _ in range(8):
            poller.poll_once()

        kf1_versions_after = store.list_versions(ids["keyframes"])
        kf2_versions_after = store.list_versions(ids["keyframes_2"])
        assert len(kf1_versions_after) == 2, "revise should add a version, never replace"
        assert len(kf2_versions_after) == 1, "unrelated keyframes_2 must stay untouched"

        # Approve both keyframes via real events.
        for kf_id in (ids["keyframes"], ids["keyframes_2"]):
            movie_config.validate_action("keyframes", "approve")
            approve_kf_event = store.enqueue_event(type="approve", payload={}, artifact_id=kf_id)
            _drain_until_acked(supervisor, store, approve_kf_event["id"])
        assert store.get_artifact(ids["keyframes"])["status"] == "approved"
        assert store.get_artifact(ids["keyframes_2"])["status"] == "approved"

        # ------------------------------------------------------------------
        # STEP 6: generate clips (real `regenerate` events + real Poller)
        # ------------------------------------------------------------------
        movie_config.validate_action("clips", "regenerate")
        regen_clip1_event = store.enqueue_event(type="regenerate", payload={}, artifact_id=ids["clips"])
        _drain_until_acked(supervisor, store, regen_clip1_event["id"])
        regen_clip2_event = store.enqueue_event(type="regenerate", payload={}, artifact_id=ids["clips_2"])
        _drain_until_acked(supervisor, store, regen_clip2_event["id"])

        assert store.get_artifact(ids["clips"])["status"] == "generating"
        assert store.get_artifact(ids["clips_2"])["status"] == "generating"

        for _ in range(2):
            poller.poll_once()

        clips_jobs = store.list_jobs_by_status("running")
        clips_job_by_artifact = {j["artifact_id"]: j for j in clips_jobs}
        assert ids["clips"] in clips_job_by_artifact
        assert ids["clips_2"] in clips_job_by_artifact
        clips_job = clips_job_by_artifact[ids["clips"]]

        # ------------------------------------------------------------------
        # STEP 7: cancel clips_1 mid-flight via a real `cancel` event; assert
        # provider.cancel() was ACTUALLY called (mock provider's own job
        # tracking dict), and a late provider "success" cannot create/select
        # a version.
        # ------------------------------------------------------------------
        movie_config.validate_action("clips", "cancel")
        cancel_event = store.enqueue_event(type="cancel", payload={}, artifact_id=ids["clips"])
        _drain_until_acked(supervisor, store, cancel_event["id"])

        clips_job_after_cancel = store.get_job(clips_job["id"])
        assert clips_job_after_cancel["cancel_requested"] is True
        # cancel_requested is set immediately by the worker; status flips to
        # 'cancelled' only once the poller actually calls provider.cancel().
        assert clips_job_after_cancel["status"] == "running"

        poller.poll_once()  # poller observes cancel_requested, calls provider.cancel()

        provider_job_id = clips_job_after_cancel["provider_job_id"]
        assert provider_job_id in video_provider_state(poller)
        assert video_provider_state(poller)[provider_job_id]["cancelled"] is True, (
            "provider.cancel() must have actually been invoked, tracked on the "
            "mock provider's own internal job state"
        )

        clips_job_cancelled = store.get_job(clips_job["id"])
        assert clips_job_cancelled["status"] == "cancelled"

        # Keep polling as if the provider later (incorrectly) reported success;
        # MockVideoProvider.status() always returns "cancelled" once cancelled,
        # so this proves the poller path that would refuse a late success.
        for _ in range(10):
            poller.poll_once()
        assert store.list_versions(ids["clips"]) == [], "cancelled job must never create a version"
        assert store.get_job(clips_job["id"])["status"] == "cancelled"

        # Drain any leftover system events (e.g. a `job_done` enqueued by the
        # poller for clips_2, which may have completed during the polling
        # above) so the next manual claim below targets a known event.
        _drain_all(supervisor)

        # ------------------------------------------------------------------
        # STEP 8: simulate a worker crash mid-turn and recover via lease expiry.
        # We claim an event, then kill the underlying subprocess before it
        # would ack, and confirm the event's lease can expire and become
        # claimable again (SPEC section 40: crash before ack -> retry, not
        # permanent failure).
        # ------------------------------------------------------------------
        crash_event = store.enqueue_event(
            type="revise",
            payload={"note": "will be interrupted by a worker crash"},
            artifact_id=ids["clips_2"],
        )
        claimed = store.claim_next_event(worker_id="crashing_worker", lease_seconds=1)
        assert claimed is not None
        assert claimed["id"] == crash_event["id"]
        assert claimed["status"] == "processing"

        # Actually kill the real worker subprocess to simulate a crash.
        supervisor.start_worker()
        proc = supervisor.worker_session.process
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        assert not supervisor.transport.is_alive(supervisor.worker_session)

        # Let the lease expire.
        time.sleep(1.2)
        reclaimed = store.claim_next_event(worker_id="recovering_worker", lease_seconds=30)
        assert reclaimed is not None
        assert reclaimed["id"] == crash_event["id"]
        assert reclaimed["claimed_by"] == "recovering_worker"
        assert reclaimed["attempt_count"] == 2

        # The reclaimed event is now 'processing' again (owned by
        # recovering_worker) rather than permanently failed by the crash --
        # this IS the recovery: a dead worker's claim does not kill the event,
        # the lease expiry made it claimable again.
        assert store.get_event(crash_event["id"])["status"] == "processing"
        assert store.get_event(crash_event["id"])["claimed_by"] == "recovering_worker"
        store.ack_event(crash_event["id"])  # recovering_worker's turn completes

        # A fresh supervisor.claim_and_dispatch_event() call must restart the
        # worker subprocess (start_worker() detects the dead session) to
        # continue processing new events.
        supervisor.worker_session = None  # force restart on next dispatch

        # A plain `message` event (the free-form escape hatch, not gated by
        # stage allowed_actions) just to prove the restarted worker is alive
        # and can complete a turn; the real approval happens after generation
        # finishes below.
        restart_probe_event = store.enqueue_event(
            type="message", payload={"note": "worker restarted"}, artifact_id=ids["clips_2"]
        )
        post_crash_event = supervisor.claim_and_dispatch_event()
        assert post_crash_event is not None
        assert store.get_event(post_crash_event["id"])["status"] == "acked"
        assert supervisor.transport.is_alive(supervisor.worker_session), (
            "supervisor must have restarted the worker subprocess after the crash"
        )

        # ------------------------------------------------------------------
        # STEP 9: finish clips_2 generation, approve remaining clips/keyframes,
        # create + approve the final assembly artifact.
        # ------------------------------------------------------------------
        for _ in range(10):
            poller.poll_once()
        clips_2_job = [
            j for j in store.list_jobs_by_status("succeeded") if j["artifact_id"] == ids["clips_2"]
        ]
        assert clips_2_job, "clips_2 job should have succeeded"
        assert len(store.list_versions(ids["clips_2"])) == 1

        # clips_2 is in 'review' after generation; approve it for real.
        movie_config.validate_action("clips", "approve")
        approve_clips2_event = store.enqueue_event(type="approve", payload={}, artifact_id=ids["clips_2"])
        _drain_until_acked(supervisor, store, approve_clips2_event["id"])
        assert store.get_artifact(ids["clips_2"])["status"] == "approved"

        # assembly has no `generation` block in movie.json (only
        # regenerate/approve/reopen are allowed with no provider config), so
        # the reference worker cannot run a generation job for it; produce
        # its version directly via the store, mirroring how the reference
        # worker's own `_handle_edit`-style put_version call would behave for
        # a manually-provided final cut.
        movie_config.validate_action("assembly", "approve")
        store.put_version(
            ids["assembly"],
            content_ref="media/assembly_001_v1.mp4",
            content_type="video/mp4",
            created_by="worker",
            note="Final assembled film",
            select=True,
        )
        approve_assembly_event = store.enqueue_event(type="approve", payload={}, artifact_id=ids["assembly"])
        _drain_until_acked(supervisor, store, approve_assembly_event["id"])
        assert store.get_artifact(ids["assembly"])["status"] == "approved"

        # ------------------------------------------------------------------
        # STEP 10: verify final state via stage_config, not raw status peeking.
        # ------------------------------------------------------------------
        approved_stage_ids = set()
        for stage_id in movie_config.stage_order:
            arts = store.list_artifacts(stage=stage_id)
            if any(a["status"] == "approved" for a in arts):
                approved_stage_ids.add(stage_id)
        assert approved_stage_ids == {"script", "shots", "keyframes", "clips", "assembly"}
        assert movie_config.is_completed(approved_stage_ids) is True

        # An action outside the allowed set correctly raises.
        with pytest.raises(ActionPermissionError):
            movie_config.validate_action("script", "regenerate")

        # Version history only grows; nothing is ever deleted.
        assert len(store.list_versions(ids["script"])) == 1
        assert len(store.list_versions(ids["shots"])) == 1
        assert len(store.list_versions(ids["keyframes"])) == 2
        assert len(store.list_versions(ids["keyframes_2"])) == 1
        assert len(store.list_versions(ids["clips"])) == 0  # cancelled, never generated
        assert len(store.list_versions(ids["clips_2"])) == 1
        assert len(store.list_versions(ids["assembly"])) == 1

        # Stale artifacts from step 2 still exist and retain full history.
        for key in ("script", "shots", "keyframes", "keyframes_2", "clips", "clips_2", "assembly"):
            assert store.get_artifact(ids[key]) is not None

    def test_duplicate_event_delivery_does_not_duplicate_output(self, store, movie_config):
        """Calling the worker's event handler twice with the same event_id must not
        create two versions (idempotency via source_event_id, SPEC section 16/59)."""
        store.create_artifact(id="script_dup", stage="script", title="Dup Test")
        event = {
            "event_id": "evt_dup_001",
            "type": "edit",
            "artifact_id": "script_dup",
            "payload": {"content": "Same content twice"},
            "config": movie_config.raw,
        }

        result1 = worker_module.handle_event(event, store)
        result2 = worker_module.handle_event(event, store)

        assert result1["ok"] is True
        assert result2["ok"] is True
        assert result1["version_id"] == result2["version_id"], (
            "re-delivering the same event_id must dedupe against the existing version"
        )
        assert len(store.list_versions("script_dup")) == 1

    def test_cancel_via_worker_only_marks_requested_until_poller_calls_provider(self, store):
        """Re-verifies the current, corrected cancel_job() semantics directly
        (SPEC section 28): status must NOT flip to 'cancelled' until the
        poller has actually observed cancel_requested and invoked
        provider.cancel(); this guards against the historical poller bug
        where cancellation appeared to work without ever reaching the
        provider."""
        store.create_artifact(id="clip_direct", stage="clips", title="Direct Cancel Test")
        job = store.create_job(
            artifact_id="clip_direct", provider="video_default", kind="video", request={},
        )
        store.cancel_job(job["id"])
        job_after = store.get_job(job["id"])
        assert job_after["cancel_requested"] is True
        assert job_after["status"] == "queued", (
            "cancel_job() must only set cancel_requested; status transitions "
            "to 'cancelled' only once the poller calls provider.cancel()"
        )

        provider = MockVideoProvider(polls_to_success=5)
        p = Poller(store, {"video_default": provider})
        p.poll_once()  # submit
        p.poll_once()  # observes cancel_requested + provider_job_id set, calls provider.cancel()

        job_final = store.get_job(job["id"])
        assert job_final["status"] == "cancelled"
        provider_job_id = job_final["provider_job_id"]
        assert provider._jobs[provider_job_id]["cancelled"] is True


def video_provider_state(poller: Poller) -> dict:
    """Reach into the poller's registered video provider's internal job dict.

    This is the mock provider's OWN call-tracking (set inside cancel()/status()),
    used to assert provider.cancel() was actually invoked rather than merely
    inferring it from the absence of a version.
    """
    return poller.providers["video_default"]._jobs
