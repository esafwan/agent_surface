import pytest
from surface.store import Store


def test_schema_creation_and_initialization():
    store = Store(":memory:")
    # Check runtime schema version
    cursor = store.conn.cursor()
    cursor.execute("SELECT value FROM runtime WHERE key='schema_version'")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "1"

    # Verify tables exist
    tables = [
        "runtime",
        "sessions",
        "artifacts",
        "versions",
        "artifact_dependencies",
        "events",
        "jobs",
        "artifact_locks",
        "kv_state",
    ]
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {r[0] for r in cursor.fetchall()}
    for t in tables:
        assert t in existing_tables


def test_artifact_crud_and_locks():
    store = Store(":memory:")
    art = store.create_artifact(
        id="art_1", stage="script", title="Script Title", meta={"author": "Alice"}
    )
    assert art["id"] == "art_1"
    assert art["stage"] == "script"
    assert art["title"] == "Script Title"
    assert art["status"] == "draft"
    assert art["meta"] == {"author": "Alice"}
    assert art["locked"] is False

    updated = store.set_status("art_1", "review")
    assert updated["status"] == "review"

    locked_art = store.set_lock("art_1", True)
    assert locked_art["locked"] is True

    listed = store.list_artifacts(stage="script", status="review")
    assert len(listed) == 1
    assert listed[0]["id"] == "art_1"


def test_version_immutability_and_selection():
    store = Store(":memory:")
    store.create_artifact(id="art_1", stage="shots", title="Shots")

    # Put version 1
    v1 = store.put_version(
        "art_1", content="Shot 1: Close up", created_by="worker", select=True
    )
    assert v1["ok"] is True
    assert v1["version"] == 1
    art = store.get_artifact("art_1")
    assert art["selected_version_id"] == v1["version_id"]

    # Put version 2
    v2 = store.put_version(
        "art_1", content="Shot 1: Wide shot", created_by="worker", select=False
    )
    assert v2["ok"] is True
    assert v2["version"] == 2
    art = store.get_artifact("art_1")
    assert art["selected_version_id"] == v1["version_id"]

    # Select version 2
    sel = store.select_version("art_1", v2["version_id"])
    assert sel["ok"] is True
    art = store.get_artifact("art_1")
    assert art["selected_version_id"] == v2["version_id"]

    # List versions
    versions = store.list_versions("art_1")
    assert len(versions) == 2
    assert versions[0]["content"] == "Shot 1: Close up"
    assert versions[1]["content"] == "Shot 1: Wide shot"

    # Selection optimistic concurrency conflict check
    conflict = store.select_version(
        "art_1", v1["version_id"], expected_selected_version_id="wrong_ver_id"
    )
    assert conflict["ok"] is False
    assert conflict["error"] == "conflict"
    assert conflict["current_selected_version_id"] == v2["version_id"]


def test_dag_stale_propagation_and_cycle_detection():
    store = Store(":memory:")

    # Create DAG: Script -> Shotlist -> Frame -> Video
    store.create_artifact(id="script", stage="script", title="Script")
    store.create_artifact(id="shotlist", stage="shotlist", title="Shotlist")
    store.create_artifact(id="frame", stage="frame", title="Image Frame")
    store.create_artifact(id="video", stage="video", title="Video Clip")

    # Add dependencies
    store.add_dependency("script", "shotlist")
    store.add_dependency("shotlist", "frame")
    store.add_dependency("frame", "video")

    # Test cycle detection: trying to add video -> script must raise ValueError
    with pytest.raises(ValueError, match="creates a cycle"):
        store.add_dependency("video", "script")

    # Put initial versions on downstream artifacts and approve them
    store.put_version("shotlist", content="Shots v1")
    store.set_status("shotlist", "approved")

    store.put_version("frame", content="Frame v1")
    store.set_status("frame", "approved")

    store.put_version("video", content="Video v1")
    store.set_status("video", "approved")

    # Now update selected version on upstream 'script'
    res = store.put_version("script", content="Script v2", select=True)

    # Downstream artifacts must all be marked stale
    assert set(res["stale_descendants"]) == {"shotlist", "frame", "video"}

    assert store.get_artifact("shotlist")["status"] == "stale"
    assert store.get_artifact("frame")["status"] == "stale"
    assert store.get_artifact("video")["status"] == "stale"

    # Versions must be retained
    assert len(store.list_versions("shotlist")) == 1
    assert len(store.list_versions("frame")) == 1

    # Check system stale events were emitted
    events = store.conn.execute("SELECT * FROM events WHERE type='stale'").fetchall()
    assert len(events) == 3


def test_event_claim_ack_and_idempotency():
    store = Store(":memory:")
    store.create_artifact(id="art_1", stage="shots", title="Shots")

    # Enqueue event
    evt = store.enqueue_event(
        type="revise",
        payload={"note": "make it brighter"},
        artifact_id="art_1",
        dedupe_key="dedupe_101",
    )
    assert evt["id"].startswith("evt_")
    assert evt["status"] == "pending"

    # Dedupe check
    evt_dup = store.enqueue_event(
        type="revise",
        payload={"note": "make it brighter"},
        artifact_id="art_1",
        dedupe_key="dedupe_101",
    )
    assert evt_dup["id"] == evt["id"]

    # Claim event
    claimed = store.claim_next_event(worker_id="worker_A", lease_seconds=10)
    assert claimed is not None
    assert claimed["id"] == evt["id"]
    assert claimed["status"] == "processing"
    assert claimed["claimed_by"] == "worker_A"

    # Try claiming again when artifact is leased by worker_A
    evt2 = store.enqueue_event(
        type="revise", payload={"note": "another edit"}, artifact_id="art_1"
    )
    claimed_again = store.claim_next_event(worker_id="worker_B", lease_seconds=10)
    # Since art_1 is locked by worker_A, claimed_again should be None
    assert claimed_again is None

    # Ack event 1
    acked = store.ack_event(evt["id"])
    assert acked["status"] == "acked"

    # Now event 2 can be claimed
    claimed2 = store.claim_next_event(worker_id="worker_B", lease_seconds=10)
    assert claimed2 is not None
    assert claimed2["id"] == evt2["id"]

    # Fail event 2
    failed = store.fail_event(evt2["id"], error="Worker timed out")
    assert failed["status"] == "failed"
    assert failed["error"] == "Worker timed out"


def test_version_put_idempotency():
    store = Store(":memory:")
    store.create_artifact(id="art_1", stage="shots", title="Shots")

    v1 = store.put_version(
        "art_1",
        content="Version content",
        source_event_id="evt_999",
        select=True,
    )
    assert v1["version"] == 1

    # Rerunning with same source_event_id returns existing version
    v1_repeat = store.put_version(
        "art_1",
        content="Different content",
        source_event_id="evt_999",
        select=True,
    )
    assert v1_repeat["version_id"] == v1["version_id"]
    assert v1_repeat["version"] == 1
    assert len(store.list_versions("art_1")) == 1


def test_job_lifecycle_and_cancellation():
    store = Store(":memory:")
    store.create_artifact(id="art_1", stage="keyframes", title="Keyframe 1")

    # Create Job
    job = store.create_job(
        artifact_id="art_1",
        provider="image_default",
        kind="image",
        request={"prompt": "hero sunset"},
        cost_estimate=0.05,
    )
    assert job["id"].startswith("job_")
    assert job["status"] == "queued"
    assert job["cost_estimate"] == 0.05

    # Update status to running
    job_run = store.update_job_status(
        job["id"], status="running", provider_job_id="prov_123"
    )
    assert job_run["status"] == "running"
    assert job_run["provider_job_id"] == "prov_123"

    # Finish job
    job_done = store.finish_job(
        job["id"], result={"image_url": "media/img1.png"}, cost_actual=0.04
    )
    assert job_done["status"] == "succeeded"
    assert job_done["result"] == {"image_url": "media/img1.png"}
    assert job_done["cost_actual"] == 0.04

    # Cancellation test
    job2 = store.create_job(
        artifact_id="art_1", provider="video_default", kind="video"
    )
    # cancel_job only flags cancel_requested; status stays queued/running until
    # the poller has actually told the provider to cancel (SPEC section 28) -
    # otherwise the job leaves the poller's scan set before cancel is ever
    # attempted against the provider.
    cancelled_job = store.cancel_job(job2["id"])
    assert cancelled_job["status"] == "queued"
    assert cancelled_job["cancel_requested"] is True

    # Poller (or equivalent) then marks it cancelled after handling it.
    finally_cancelled = store.update_job_status(job2["id"], "cancelled")
    assert finally_cancelled["status"] == "cancelled"

    # A late "finish" for an already-cancelled job MUST NOT become succeeded.
    late_finish = store.finish_job(job2["id"], result={"image_url": "late.png"})
    assert late_finish["status"] == "cancelled"

    # Late result on cancelled job MUST NOT succeed
    late_finish = store.finish_job(
        job2["id"], result={"video_url": "media/vid2.mp4"}
    )
    assert late_finish["status"] == "cancelled"
