"""AntigravityBackend: AgentBackend adapter for the agy (Antigravity) CLI."""

from typing import Optional

from surface.agents.base import AgentReply, AgentError
from surface.agents.runner import extract_sentinel_content


class AntigravityBackend:
    """Adapter for the agy (Antigravity) CLI agent backend."""

    name = "agy"
    supports_resume = False

    def build_argv(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        workdir: str = ".",
        session_id: Optional[str] = None,
    ) -> list[str]:
        model = model or "claude-sonnet-4-6"
        return ["agy", "--model", model, "--print", prompt]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        out_file: Optional[str] = None,
    ) -> AgentReply:
        if returncode != 0:
            stderr_lower = (stderr or "").lower()
            if "invalid model selection" in stderr_lower or "not recognized" in stderr_lower:
                raise AgentError(message=stderr, kind="auth")
            raise AgentError(message=stderr, kind="crash")

        text = stdout or ""
        content = extract_sentinel_content(text)
        if content is not None:
            return AgentReply(text=content)
        return AgentReply(text=text.strip())

    def env_overrides(self) -> dict:
        return {}


AGY_BACKEND = AntigravityBackend()
