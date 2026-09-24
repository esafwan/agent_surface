"""Property-based tests for freshness computation on random DAGs.

These tests verify that compute_freshness correctly handles:
- Transitive staleness (changes propagate downstream)
- Reversion restores freshness without regeneration
- Late async results can arrive stale (snapshot-input property)
- Status and freshness are independent (approved but stale)
- stale_because correctly identifies the root cause
"""

import random
from typing import List, Tuple

import pytest

from surface.store import Store


def _stale_because_ids(freshness: dict) -> set:
    """Extract the set of upstream_artifact_id from a stale_because list of
    {"upstream_artifact_id": ..., "reason": ...} dicts."""
    return {d["upstream_artifact_id"] for d in freshness["stale_because"]}


def _build_random_dag(
    store: Store, num_artifacts: int, seed: int
) -> Tuple[List[str], random.Random]:
    """Build a random acyclic DAG and create initial versions for all artifacts.

    Returns a tuple of (artifact_ids, rng) where rng is the seeded Random instance.

    Strategy: create num_artifacts in order (0, 1, ..., n-1), then add edges only
    from lower-numbered to higher-numbered artifacts, which is trivially acyclic.
    Each artifact gets one initial version with auto-captured inputs.
    """
    rng = random.Random(seed)
    artifact_ids = [f"art_{i}" for i in range(num_artifacts)]

    # Create all artifacts
    for aid in artifact_ids:
        store.create_artifact(id=aid, stage="test", title=f"Artifact {aid}")

    # Add random edges (only lower to higher index to avoid cycles)
    for i in range(num_artifacts):
        for j in range(i + 1, num_artifacts):
            # Random edge probability
            if rng.random() < 0.3:  # 30% edge density
                try:
                    store.add_dependency(artifact_ids[i], artifact_ids[j])
                except ValueError:
                    # Cycle detection (shouldn't happen with lower->higher strategy)
                    pass

    # Create initial versions for all artifacts (in topological order: lower indices first)
    for aid in artifact_ids:
        store.put_version(aid, content=f"Content of {aid}", select=True)

    return artifact_ids, rng


def test_freshness_on_random_dags():
    """Generate multiple random DAGs and verify all artifacts are fresh right after creation."""
    for seed in range(5):
        store = Store(":memory:")
        artifact_ids, _ = _build_random_dag(store, num_artifacts=30, seed=seed)

        # Every artifact should be fresh immediately after initial version creation
        for aid in artifact_ids:
            freshness = store.compute_freshness(aid)
            assert (
                freshness["fresh"] is True
            ), f"Seed {seed}: {aid} should be fresh but is stale: {freshness}"
            assert freshness["stale_because"] == [], (
                f"Seed {seed}: {aid} should have empty stale_because but got {freshness['stale_because']}"
            )


def test_upstream_change_makes_downstream_stale():
    """Verify that changing an upstream artifact makes all transitive downstreams stale."""
    rng = random.Random(42)

    for trial in range(3):
        store = Store(":memory:")
        artifact_ids, _ = _build_random_dag(store, num_artifacts=25, seed=42 + trial)

        # Find a non-leaf artifact (has at least one downstream)
        non_leaf_candidates = []
        for aid in artifact_ids:
            graph = store.get_graph(aid)
            if graph["downstreams"]:
                non_leaf_candidates.append(aid)

        if not non_leaf_candidates:
            # If no non-leaf found, skip this trial
            continue

        changed_artifact = rng.choice(non_leaf_candidates)

        # Change this artifact (put a new version and auto-select it)
        store.put_version(changed_artifact, content="Modified content", select=True)

        # Collect all transitive downstreams via BFS
        visited = set()
        queue = [changed_artifact]
        expected_stale = set()

        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)

            graph = store.get_graph(curr)
            for downstream_info in graph["downstreams"]:
                downstream_id = downstream_info["downstream_artifact_id"]
                expected_stale.add(downstream_id)
                queue.append(downstream_id)

        # Verify direct and transitive downstreams are stale
        for downstream_id in expected_stale:
            freshness = store.compute_freshness(downstream_id)
            assert (
                freshness["fresh"] is False
            ), f"Trial {trial}: {downstream_id} should be stale after {changed_artifact} changed"
            stale_ids = _stale_because_ids(freshness)
            assert changed_artifact in stale_ids or any(
                dep_id in stale_ids for dep_id in expected_stale
            ), f"Trial {trial}: {downstream_id} stale_because should include {changed_artifact} or intermediate"

        # Verify unrelated artifacts are still fresh
        unrelated = set(artifact_ids) - expected_stale - {changed_artifact}
        for aid in unrelated:
            freshness = store.compute_freshness(aid)
            assert (
                freshness["fresh"] is True
            ), f"Trial {trial}: Unrelated artifact {aid} should be fresh but got {freshness}"


def test_reversion_restores_freshness_without_regeneration():
    """Revert upstream version; downstream should become fresh with no new put_version call."""
    store = Store(":memory:")

    # Build a 3-deep chain: A -> B -> C
    store.create_artifact(id="A", stage="test", title="A")
    store.create_artifact(id="B", stage="test", title="B")
    store.create_artifact(id="C", stage="test", title="C")

    store.add_dependency("A", "B")
    store.add_dependency("B", "C")

    # Create initial versions (v1 for each)
    v1_A = store.put_version("A", content="A v1", select=True)["version_id"]
    v1_B = store.put_version("B", content="B v1", select=True)["version_id"]
    v1_C = store.put_version("C", content="C v1", select=True)["version_id"]

    # Verify all are fresh
    assert store.compute_freshness("B")["fresh"] is True
    assert store.compute_freshness("C")["fresh"] is True

    # Create and select v2 of A (B and C now stale)
    v2_A = store.put_version("A", content="A v2", select=True)["version_id"]
    assert store.compute_freshness("B")["fresh"] is False
    assert store.compute_freshness("C")["fresh"] is False

    # Revert A back to v1 without creating new B/C versions
    store.select_version("A", v1_A)

    # Both B and C should be fresh again (via reversion, no regeneration)
    assert store.compute_freshness("B")["fresh"] is True, (
        "B should be fresh after A reverted to v1, without B regenerating"
    )
    assert store.compute_freshness("C")["fresh"] is True, (
        "C should be fresh after A reverted to v1 (transitive), without C regenerating"
    )

    # Verify no new versions were created
    assert len(store.list_versions("B")) == 1, "B should still have only v1"
    assert len(store.list_versions("C")) == 1, "C should still have only v1"


def test_late_job_result_arrives_stale():
    """Async job result with snapshot inputs arrives stale when upstream moved."""
    store = Store(":memory:")

    # Build A -> B chain
    store.create_artifact(id="A", stage="test", title="A")
    store.create_artifact(id="B", stage="test", title="B")
    store.add_dependency("A", "B")

    # Create v1 of A
    v1_A = store.put_version("A", content="A v1", select=True)["version_id"]

    # Create v1 of B, auto-capturing A's v1 as input
    v1_B = store.put_version("B", content="B v1", select=True)["version_id"]

    # Simulate job dispatch: capture A's current version at dispatch time
    snapshot_inputs = {"A": v1_A}

    # Before job completes, upstream moves
    v2_A = store.put_version("A", content="A v2", select=True)["version_id"]
    assert store.compute_freshness("B")["fresh"] is False, "B should be stale after A moved"

    # Job finishes and writes result using snapshot inputs (the old A version)
    v2_B = store.put_version(
        "B",
        content="B v2 (from old job)",
        select=True,
        inputs=snapshot_inputs,  # Explicit: "this was built against A v1"
    )["version_id"]

    # B v2 is immediately stale because it says it was built from A v1 but A is now v2
    freshness = store.compute_freshness("B")
    assert freshness["fresh"] is False, (
        "B v2 should be born stale: it recorded A v1 as input but A is now v2"
    )
    assert "A" in _stale_because_ids(freshness), (
        "stale_because should name A as the cause"
    )


def test_approved_status_survives_staleness():
    """Approved status is independent of staleness; both can be true simultaneously."""
    store = Store(":memory:")

    # Build A -> B chain
    store.create_artifact(id="A", stage="test", title="A")
    store.create_artifact(id="B", stage="test", title="B")
    store.add_dependency("A", "B")

    # Create versions
    store.put_version("A", content="A v1", select=True)
    store.put_version("B", content="B v1", select=True)

    # Approve B when fresh
    store.set_status("B", "approved")
    assert store.get_artifact("B")["status"] == "approved"
    assert store.compute_freshness("B")["fresh"] is True

    # Change A (B becomes stale)
    store.put_version("A", content="A v2", select=True)

    # B should now be stale BUT still approved (status untouched by staleness)
    art_B = store.get_artifact("B")
    assert art_B["status"] == "approved", (
        "Status should not change when staleness propagates"
    )
    assert store.compute_freshness("B")["fresh"] is False, (
        "But freshness should correctly report stale"
    )


def test_stale_because_names_the_right_upstream():
    """stale_because correctly identifies which upstream(s) caused the staleness."""
    store = Store(":memory:")

    # Create a linear chain A -> B -> C
    # Verify that stale_because correctly names the immediate upstream cause
    store.create_artifact(id="A", stage="test", title="A")
    store.create_artifact(id="B", stage="test", title="B")
    store.create_artifact(id="C", stage="test", title="C")

    store.add_dependency("A", "B")
    store.add_dependency("B", "C")

    # Create initial versions: A v1 -> B v1 -> C v1
    store.put_version("A", content="A v1", select=True)
    store.put_version("B", content="B v1", select=True)
    store.put_version("C", content="C v1", select=True)

    # Verify all are fresh initially
    assert store.compute_freshness("A")["fresh"] is True
    assert store.compute_freshness("B")["fresh"] is True
    assert store.compute_freshness("C")["fresh"] is True

    # Change A (now B and C are stale)
    store.put_version("A", content="A v2", select=True)

    # B should be stale because of A (direct upstream)
    freshness_B = store.compute_freshness("B")
    assert freshness_B["fresh"] is False
    assert "A" in _stale_because_ids(freshness_B), (
        f"B's stale_because should name A, got {freshness_B['stale_because']}"
    )

    # C should be stale because of B (direct upstream in its recording)
    # and transitively because B is stale
    freshness_C = store.compute_freshness("C")
    assert freshness_C["fresh"] is False
    assert "B" in _stale_because_ids(freshness_C), (
        f"C's stale_because should name B, got {freshness_C['stale_because']}"
    )


def test_missing_provenance_record_is_not_silently_fresh():
    """A real dependency with no version_inputs row must not read as fresh.

    Build A -> B. Create A's version normally. Then create B's version via
    put_version(B, ..., inputs={}) -- an empty explicit inputs mapping,
    simulating a code path that skipped provenance capture -- even though B
    really does depend on A (a real artifact_dependencies edge exists).
    compute_freshness(B) must report NOT fresh, with a "no_record" entry for
    A, rather than silently treating "nothing recorded" as "known fresh".
    """
    store = Store(":memory:")

    store.create_artifact(id="A", stage="test", title="A")
    store.create_artifact(id="B", stage="test", title="B")
    store.add_dependency("A", "B")

    store.put_version("A", content="A v1", select=True)

    # Simulate a code path that skipped input capture entirely.
    store.put_version("B", content="B v1", select=True, inputs={})

    freshness_B = store.compute_freshness("B")
    assert freshness_B["fresh"] is False, (
        "B has a real dependency on A but no version_inputs record for it -- "
        "must not be silently treated as fresh"
    )
    matching = [
        d for d in freshness_B["stale_because"] if d["upstream_artifact_id"] == "A"
    ]
    assert len(matching) == 1, (
        f"expected exactly one stale_because entry for A, got {freshness_B['stale_because']}"
    )
    assert matching[0]["reason"] == "no_record", (
        f"expected reason 'no_record' for A, got {matching[0]}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
