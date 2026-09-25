"""Tests for OpencodeBackend."""

import shutil
import pytest

from surface.agents.base import AgentError
from surface.agents.opencode_backend import OpencodeBackend
from surface.agents.runner import run_agent


@pytest.fixture
def backend():
    """Fixture providing an OpencodeBackend instance."""
    return OpencodeBackend()


class TestBuildArgv:
    """Tests for OpencodeBackend.build_argv()."""

    def test_build_argv_with_default_model(self, backend):
        """build_argv uses default model when none provided."""
        prompt = "Hello, world!"
        argv = backend.build_argv(prompt, model=None, workdir=".")
        assert argv == ["opencode", "run", "-m", "opencode-go/glm-5.3", "Hello, world!"]

    def test_build_argv_with_custom_model(self, backend):
        """build_argv uses provided model."""
        prompt = "Test prompt"
        argv = backend.build_argv(prompt, model="opencode-go/minimax-m3", workdir=".")
        assert argv == ["opencode", "run", "-m", "opencode-go/minimax-m3", "Test prompt"]

    def test_build_argv_returns_list_not_string(self, backend):
        """build_argv returns a list (safe for subprocess.Popen with argv list)."""
        argv = backend.build_argv("test", model="opencode-go/glm-5.3", workdir=".")
        assert isinstance(argv, list)
        assert all(isinstance(arg, str) for arg in argv)


class TestParseOutput:
    """Tests for OpencodeBackend.parse_output()."""

    def test_parse_output_strips_router_header_and_extracts_sentinel(self, backend):
        """parse_output strips router header and extracts sentinel content."""
        # Real output format from the spec
        stdout = (
            "> oc-router · glm-5.3\n"
            "<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>"
        )
        reply = backend.parse_output(stdout, "", 0)
        assert reply.text == "OK"
        assert reply.session_id is None
        assert reply.usage is None

    def test_parse_output_fallback_without_sentinel(self, backend):
        """parse_output falls back to full text when no sentinel found."""
        stdout = (
            "> oc-router · glm-5.3\n"
            "This is a plain response without sentinels"
        )
        reply = backend.parse_output(stdout, "", 0)
        assert reply.text == "This is a plain response without sentinels"

    def test_parse_output_no_router_header_with_sentinel(self, backend):
        """parse_output handles sentinel content without router header."""
        stdout = "<<<SURFACE_CONTENT hello SURFACE_CONTENT>>>"
        reply = backend.parse_output(stdout, "", 0)
        assert reply.text == "hello"

    def test_parse_output_no_router_header_no_sentinel(self, backend):
        """parse_output handles plain text without router header."""
        stdout = "Just plain text response"
        reply = backend.parse_output(stdout, "", 0)
        assert reply.text == "Just plain text response"

    def test_parse_output_empty_stdout(self, backend):
        """parse_output handles empty stdout gracefully."""
        reply = backend.parse_output("", "", 0)
        assert reply.text == ""

    def test_parse_output_auth_error_unauthorized(self, backend):
        """parse_output raises AgentError with kind='auth' for 'unauthorized' in stderr."""
        with pytest.raises(AgentError) as exc_info:
            backend.parse_output("", "Unauthorized access", 1)
        assert exc_info.value.kind == "auth"
        assert "Unauthorized access" in exc_info.value.message

    def test_parse_output_auth_error_unable_to_connect(self, backend):
        """parse_output raises AgentError with kind='auth' for 'unable to connect' in stderr."""
        with pytest.raises(AgentError) as exc_info:
            backend.parse_output("", "Unable to connect to server", 1)
        assert exc_info.value.kind == "auth"

    def test_parse_output_auth_error_case_insensitive(self, backend):
        """parse_output checks auth patterns case-insensitively."""
        with pytest.raises(AgentError) as exc_info:
            backend.parse_output("", "UNAUTHORIZED: invalid token", 1)
        assert exc_info.value.kind == "auth"

    def test_parse_output_no_error_on_returncode_zero(self, backend):
        """parse_output never raises when returncode is 0, even with error text."""
        # This should not raise, even though stderr contains error-like text
        reply = backend.parse_output("response", "Unauthorized in stderr", 0)
        assert reply.text == "response"

    def test_parse_output_sentinel_with_whitespace(self, backend):
        """parse_output extracts sentinel content with whitespace handling."""
        stdout = "<<<SURFACE_CONTENT   content with spaces   SURFACE_CONTENT>>>"
        reply = backend.parse_output(stdout, "", 0)
        # extract_sentinel_content strips the content
        assert reply.text == "content with spaces"

    def test_parse_output_multiline_content_in_sentinel(self, backend):
        """parse_output handles multiline content within sentinels."""
        stdout = (
            "<<<SURFACE_CONTENT\nLine 1\nLine 2\nLine 3\nSURFACE_CONTENT>>>"
        )
        reply = backend.parse_output(stdout, "", 0)
        # extract_sentinel_content extracts everything between sentinels
        assert "Line 1" in reply.text
        assert "Line 2" in reply.text
        assert "Line 3" in reply.text


class TestEnvOverrides:
    """Tests for OpencodeBackend.env_overrides()."""

    def test_env_overrides_returns_empty_dict(self, backend):
        """env_overrides returns an empty dict."""
        result = backend.env_overrides()
        assert isinstance(result, dict)
        assert len(result) == 0


@pytest.mark.live_agent
def test_live_opencode_backend():
    """Live test invoking real opencode CLI with run_agent.

    Skips if opencode binary is not on PATH.
    This test actually calls the real opencode CLI.
    """
    # Skip if opencode is not available
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI not found on PATH")

    backend = OpencodeBackend()
    prompt = "Reply with exactly OK between the sentinels: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>"

    # Run with a generous timeout
    reply = run_agent(backend, prompt, model="opencode-go/glm-5.3", timeout=60.0)

    # Verify the reply contains "OK"
    assert reply.text is not None
    assert "OK" in reply.text
