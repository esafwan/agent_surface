"""Tests for AntigravityBackend (agy CLI adapter)."""

import shutil

import pytest

from surface.agents.agy_backend import AntigravityBackend
from surface.agents.base import AgentError
from surface.agents.runner import run_agent


def test_build_argv_defaults():
    backend = AntigravityBackend()
    argv = backend.build_argv("hello", model=None, workdir=".", session_id=None)
    assert argv == ["agy", "--model", "claude-sonnet-4-6", "--print", "hello"]


def test_build_argv_custom_model():
    backend = AntigravityBackend()
    argv = backend.build_argv("hi", model="gemini-3.8-flash-medium", workdir=".", session_id=None)
    assert argv == ["agy", "--model", "gemini-3.8-flash-medium", "--print", "hi"]


def test_parse_output_extracts_sentinel():
    backend = AntigravityBackend()
    reply = backend.parse_output("<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>\n", "", 0)
    assert reply.text == "OK"


def test_parse_output_fallback_no_sentinel():
    backend = AntigravityBackend()
    reply = backend.parse_output("Just a plain response.\n", "", 0)
    assert reply.text == "Just a plain response."


def test_parse_output_auth_error():
    backend = AntigravityBackend()
    with pytest.raises(AgentError) as exc_info:
        backend.parse_output("", "error: invalid model selection", 1)
    assert exc_info.value.kind == "auth"


def test_parse_output_crash_error():
    backend = AntigravityBackend()
    with pytest.raises(AgentError) as exc_info:
        backend.parse_output("", "some unrelated failure", 1)
    assert exc_info.value.kind == "crash"


@pytest.mark.live_agent
def test_live_agy_backend():
    if shutil.which("agy") is None:
        pytest.skip("agy binary not on PATH")
    backend = AntigravityBackend()
    reply = run_agent(
        backend,
        "Reply with exactly OK between the sentinels: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>",
        model=None,
        timeout=60,
        workdir=".",
    )
    assert "OK" in reply.text
