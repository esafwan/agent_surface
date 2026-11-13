"""OpencodeBackend: AgentBackend adapter for the opencode CLI."""

from typing import Optional

from surface.agents.base import AgentReply, AgentError
from surface.agents.runner import extract_sentinel_content


class OpencodeBackend:
    """Adapter for the opencode CLI agent backend."""

    name = "opencode"
    binary = "opencode"
    supports_resume = False

    def build_argv(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        workdir: str = ".",
        session_id: Optional[str] = None,
    ) -> list[str]:
        """Build argv for opencode run -m <model> <prompt>."""
        model = model or "opencode-go/glm-5.3"
        return ["opencode", "run", "-m", model, prompt]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        out_file: Optional[str] = None,
    ) -> AgentReply:
        """Parse opencode output, extracting sentinel content or falling back to full text.

        Strips the router header line (starting with '>'), extracts sentinel content if present,
        and falls back to the full text if no sentinel is found.
        """
        # Check for auth errors (only raise on non-zero returncode)
        if returncode != 0:
            stderr_lower = (stderr or "").lower()
            if "unauthorized" in stderr_lower or "unable to connect" in stderr_lower:
                raise AgentError(message=stderr, kind="auth")

        # Strip router header line (starts with '>') if present
        text = stdout or ""
        lines = text.split("\n", 1)
        if lines and lines[0].startswith(">"):
            # Skip the first line (router header)
            text = lines[1] if len(lines) > 1 else ""

        # Try to extract sentinel content
        content = extract_sentinel_content(text)
        if content is not None:
            return AgentReply(text=content)

        # Fall back to full text (stripped of leading/trailing whitespace)
        return AgentReply(text=text.strip())

    def env_overrides(self) -> dict:
        """No special environment overrides needed."""
        return {}


OPENCODE_BACKEND = OpencodeBackend()
