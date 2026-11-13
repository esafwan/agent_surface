"""Tests for CodexBackend."""

import shutil

import pytest

from surface.agents.base import AgentError
from surface.agents.codex_backend import CodexBackend
from surface.agents.runner import run_agent

SAMPLE_OUTPUT = """OpenAI Codex v0.144.6
--------
workdir: /Users/safwan/Code/Experiments/agent_surface
model: gpt-5.6-luna
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: low
reasoning summaries: none
session id: 01a0daa1-9fac-7520-bcee-1a68c266cff2
--------
user
Reply with exactly this and nothing else: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>

warning: Exceeded skills context budget of 2%. All skill descriptions were removed and 5 additional skills were not included in the model-visible skills list.
codex
<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>
tokens used
9,741
<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>
"""


def test_build_argv_defaults():
    backend = CodexBackend()
    argv = backend.build_argv("hello", model=None, workdir=".", session_id=None)
    assert argv == ["codex", "exec", "-m", "gpt-5.6-luna", "--sandbox", "read-only", "hello"]


def test_build_argv_custom_model():
    backend = CodexBackend()
    argv = backend.build_argv("hi", model="o3", workdir=".", session_id=None)
    assert argv == ["codex", "exec", "-m", "o3", "--sandbox", "read-only", "hi"]


def test_no_bypass_flags_in_argv():
    backend = CodexBackend()
    argv = backend.build_argv("hi", model=None, workdir=".", session_id=None)
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert not any("dangerously" in a for a in argv)


def test_parse_output_extracts_sentinel_and_usage():
    backend = CodexBackend()
    reply = backend.parse_output(SAMPLE_OUTPUT, "", 0)
    assert reply.text == "OK"
    assert reply.usage == {"tokens": 9741}
    assert reply.session_id == "01a0daa1-9fac-7520-bcee-1a68c266cff2"


def test_parse_output_fallback_no_sentinel():
    backend = CodexBackend()
    stdout = "codex\nJust a plain response with no sentinel.\n"
    reply = backend.parse_output(stdout, "", 0)
    assert "Just a plain response" in reply.text


def test_parse_output_auth_error():
    backend = CodexBackend()
    with pytest.raises(AgentError) as exc_info:
        backend.parse_output("", "Authentication failed: token expired", 1)
    assert exc_info.value.kind == "auth"


def test_parse_output_crash_error():
    backend = CodexBackend()
    with pytest.raises(AgentError) as exc_info:
        backend.parse_output("", "some unrelated failure", 1)
    assert exc_info.value.kind == "crash"


@pytest.mark.live_agent
def test_live_codex_backend():
    if shutil.which("codex") is None:
        pytest.skip("codex binary not on PATH")
    backend = CodexBackend()
    reply = run_agent(
        backend,
        "Reply with exactly OK between the sentinels: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>",
        model=None,
        timeout=60,
        workdir=".",
    )
    assert "OK" in reply.text
