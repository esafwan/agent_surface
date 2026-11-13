"""Unit tests for surface/agents/runner.py using the fake backend."""

import os
import subprocess
import time

import pytest

from surface.agents.base import AgentError
from surface.agents.registry import get_backend
from surface.agents.runner import extract_sentinel_content, run_agent

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fake_agent_pids():
    """Return list of pids of currently running fake_agent.py processes."""
    try:
        out = subprocess.check_output(["pgrep", "-f", "fake_agent.py"], text=True)
    except subprocess.CalledProcessError:
        return []
    except FileNotFoundError:
        return None  # pgrep not available on this platform
    return [int(p) for p in out.split() if p.strip()]


def test_extract_sentinel_content_success():
    text = "noise\n<<<SURFACE_CONTENT\nhello world\nSURFACE_CONTENT>>>\ntrailing"
    assert extract_sentinel_content(text) == "hello world"


def test_extract_sentinel_content_missing_returns_none():
    assert extract_sentinel_content("no sentinels here") is None
    assert extract_sentinel_content("<<<SURFACE_CONTENT only start") is None
    assert extract_sentinel_content(None) is None


def test_run_agent_echo_mode(monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
    backend = get_backend("fake")
    reply = run_agent(backend, "hello prompt", timeout=10, workdir=REPO_ROOT)
    assert reply.text == "hello prompt"


def test_run_agent_crash_mode(monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "crash")
    backend = get_backend("fake")
    with pytest.raises(AgentError) as excinfo:
        run_agent(backend, "hello", timeout=10, workdir=REPO_ROOT)
    assert excinfo.value.kind == "crash"


def test_run_agent_garbage_mode(monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "garbage")
    backend = get_backend("fake")
    with pytest.raises(AgentError) as excinfo:
        run_agent(backend, "hello", timeout=10, workdir=REPO_ROOT)
    assert excinfo.value.kind == "bad_output"


def test_run_agent_hang_mode_times_out_and_kills_process(monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "hang")
    monkeypatch.setenv("FAKE_AGENT_SLEEP", "10")
    backend = get_backend("fake")

    before = _fake_agent_pids()

    with pytest.raises(AgentError) as excinfo:
        run_agent(backend, "hello", timeout=1, workdir=REPO_ROOT)
    assert excinfo.value.kind == "timeout"

    # Give the OS a brief moment to reap the killed process, then verify it's gone.
    deadline = time.time() + 3
    remaining = _fake_agent_pids()
    while remaining and time.time() < deadline:
        time.sleep(0.2)
        remaining = _fake_agent_pids()

    if before is None:
        pytest.skip("pgrep not available on this platform; cannot verify process death")

    new_pids = set(remaining or []) - set(before)
    assert not new_pids, f"fake_agent.py process(es) still running after timeout: {new_pids}"


def test_run_agent_writes_log_file(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_AGENT_MODE", "echo")
    backend = get_backend("fake")
    log_dir = str(tmp_path / "agent_logs")
    run_agent(backend, "log me", timeout=10, workdir=REPO_ROOT, log_dir=log_dir)

    assert os.path.isdir(log_dir)
    entries = os.listdir(log_dir)
    assert len(entries) == 1
    assert entries[0].startswith("fake-")
    content = open(os.path.join(log_dir, entries[0])).read()
    assert "log me" in content
