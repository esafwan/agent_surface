"""Fake AgentBackend used for tests: invokes tests/fixtures/fake_agent.py."""

import sys
from pathlib import Path
from typing import Optional

from surface.agents.base import AgentReply
from surface.agents.runner import extract_sentinel_content
from surface.agents.base import AgentError

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fake_agent.py"


class FakeBackend:
    """Test-only backend that shells out to tests/fixtures/fake_agent.py."""

    name = "fake"
    binary = ""  # no external binary; always available
    supports_resume = False

    def build_argv(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        workdir: str = ".",
        session_id: Optional[str] = None,
    ) -> list[str]:
        return [sys.executable, str(_FIXTURE_PATH), prompt]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        out_file: Optional[str] = None,
    ) -> AgentReply:
        content = extract_sentinel_content(stdout)
        if content is None:
            raise AgentError(
                f"No sentinel content found in fake agent output: {stdout!r}",
                kind="bad_output",
            )
        return AgentReply(text=content)

    def env_overrides(self) -> dict:
        return {}


FAKE_BACKEND = FakeBackend()
