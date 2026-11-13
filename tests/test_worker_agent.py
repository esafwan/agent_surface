"""Tests for the worker's optional real-agent mode (T5).

Uses the deterministic `fake` backend (tests/fixtures/fake_agent.py) so
these tests need no network access or real CLI, but exercise the exact same
AgentConfig / handle_event code path a real backend (opencode/codex/agy)
runs through.
"""

import os

import pytest

from surface.store import Store
from surface.worker import AgentConfig, handle_event
from surface.stages.config import load_preset


@pytest.fixture
def in_memory_store():
    return Store(":memory:")


@pytest.fixture
def movie_config():
    preset = load_preset("movie")
    return preset.raw


@pytest.fixture
def script_artifact(in_memory_store):
    in_memory_store.create_artifact(id="script_1", stage="script", title="Script", status="draft")
    in_memory_store.put_version("script_1", content="Original script", source_event_id="evt_base")
    return in_memory_store


def test_revise_without_agent_is_unchanged(script_artifact, movie_config):
    """No agent_config (or agent_config with name=None): byte-identical to
    the original deterministic reference worker behavior."""
    store = script_artifact
    event = {
        "event_id": "evt_no_agent",
        "type": "revise",
        "artifact_id": "script_1",
        "payload": {"note": "make it punchier"},
        "config": movie_config,
    }
    result = handle_event(event, store)
    assert result["ok"] is True
    versions = store.list_versions("script_1")
    assert len(versions) == 2
    assert versions[1]["content"] == "Original script"  # re-committed unchanged
    assert versions[1]["created_by"] == "worker"


def test_revise_with_fake_agent_uses_agent_content(script_artifact, movie_config, tmp_path, monkeypatch):
    counter_file = tmp_path / "counter.txt"
    monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
    monkeypatch.setenv("FAKE_AGENT_COUNTER_FILE", str(counter_file))
    store = script_artifact
    agent_config = AgentConfig(name="fake", timeout=10)

    event = {
        "event_id": "evt_agent_1",
        "type": "revise",
        "artifact_id": "script_1",
        "payload": {"note": "make it punchier"},
        "config": movie_config,
    }
    result = handle_event(event, store, agent_config)
    assert result["ok"] is True
    versions = store.list_versions("script_1")
    assert len(versions) == 2
    assert versions[1]["created_by"] == "agent:fake"
    # The fake backend in "echo" mode prints the prompt back between
    # sentinels, so the note text should show up in the resulting content.
    assert "make it punchier" in versions[1]["content"]
    assert counter_file.exists()
    assert len(counter_file.read_text().strip().splitlines()) == 1


def test_revise_retry_skips_second_agent_call(script_artifact, movie_config, tmp_path, monkeypatch):
    """A version already exists for this event_id (a retried/redelivered
    event) -- the agent must not be invoked a second time."""
    counter_file = tmp_path / "counter.txt"
    monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
    monkeypatch.setenv("FAKE_AGENT_COUNTER_FILE", str(counter_file))
    store = script_artifact
    agent_config = AgentConfig(name="fake", timeout=10)

    event = {
        "event_id": "evt_agent_retry",
        "type": "revise",
        "artifact_id": "script_1",
        "payload": {"note": "note one"},
        "config": movie_config,
    }
    result1 = handle_event(event, store, agent_config)
    assert result1["ok"] is True
    assert len(counter_file.read_text().strip().splitlines()) == 1

    # Redeliver the exact same event (same event_id) -- simulates a
    # Supervisor retry after a lease expiry / crash.
    result2 = handle_event(event, store, agent_config)
    assert result2["ok"] is True
    assert result2["version_id"] == result1["version_id"]

    # The agent must not have been invoked again.
    assert len(counter_file.read_text().strip().splitlines()) == 1

    versions = store.list_versions("script_1")
    assert len(versions) == 2  # base + one revise, not two revises


def test_revise_agent_crash_returns_ok_false(script_artifact, movie_config, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "crash")
    store = script_artifact
    agent_config = AgentConfig(name="fake", timeout=10)

    event = {
        "event_id": "evt_agent_crash",
        "type": "revise",
        "artifact_id": "script_1",
        "payload": {"note": "note"},
        "config": movie_config,
    }
    result = handle_event(event, store, agent_config)
    assert result["ok"] is False
    assert "agent:crash" in result["error"]
    # No version should have been written.
    versions = store.list_versions("script_1")
    assert len(versions) == 1


def test_revise_agent_garbage_output_returns_ok_false(script_artifact, movie_config, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "garbage")
    store = script_artifact
    agent_config = AgentConfig(name="fake", timeout=10)

    event = {
        "event_id": "evt_agent_garbage",
        "type": "revise",
        "artifact_id": "script_1",
        "payload": {"note": "note"},
        "config": movie_config,
    }
    result = handle_event(event, store, agent_config)
    assert result["ok"] is False
    assert "agent:bad_output" in result["error"]


def test_revise_agent_timeout_returns_ok_false(script_artifact, movie_config, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "hang")
    monkeypatch.setenv("FAKE_AGENT_SLEEP", "10")
    store = script_artifact
    agent_config = AgentConfig(name="fake", timeout=1)

    event = {
        "event_id": "evt_agent_timeout",
        "type": "revise",
        "artifact_id": "script_1",
        "payload": {"note": "note"},
        "config": movie_config,
    }
    result = handle_event(event, store, agent_config)
    assert result["ok"] is False
    assert "agent:timeout" in result["error"]


def test_non_revise_events_unaffected_by_agent_config(script_artifact, movie_config, monkeypatch):
    """approve/lock/etc never touch the agent at all, agent_config or not."""
    monkeypatch.setenv("FAKE_AGENT_MODE", "crash")  # would fail loudly if ever invoked
    store = script_artifact
    agent_config = AgentConfig(name="fake", timeout=10)

    result = handle_event(
        {"event_id": "evt_approve", "type": "approve", "artifact_id": "script_1", "payload": {}},
        store,
        agent_config,
    )
    assert result["ok"] is True
