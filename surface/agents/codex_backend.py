"""CodexBackend: AgentBackend adapter for the codex CLI."""

import re
from typing import Optional

from surface.agents.base import AgentReply, AgentError
from surface.agents.runner import extract_sentinel_content

_TOKENS_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)")


class CodexBackend:
    """Adapter for the codex CLI agent backend.

    Uses `--sandbox read-only` (codex's own safe non-interactive default)
    rather than any approval/sandbox bypass flag -- codex exec already runs
    non-interactively with `approval: never` when a sandbox is specified.
    """

    name = "codex"
    binary = "codex"
    supports_resume = False

    def build_argv(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        workdir: str = ".",
        session_id: Optional[str] = None,
    ) -> list[str]:
        model = model or "gpt-5.6-luna"
        return ["codex", "exec", "-m", model, "--sandbox", "read-only", prompt]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        out_file: Optional[str] = None,
    ) -> AgentReply:
        if returncode != 0:
            stderr_lower = (stderr or "").lower()
            if "authentication failed" in stderr_lower:
                raise AgentError(message=stderr, kind="auth")
            raise AgentError(message=stderr, kind="crash")

        text = stdout or ""

        # The response section is everything after the last standalone
        # "codex" marker line and before a trailing "tokens used" section.
        lines = text.split("\n")
        codex_marker_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "codex":
                codex_marker_idx = i
        if codex_marker_idx is not None:
            response_lines = lines[codex_marker_idx + 1 :]
        else:
            response_lines = lines

        # Cut off a trailing "tokens used\n<number>" section if present.
        response_text = "\n".join(response_lines)
        tokens_match = _TOKENS_RE.search(response_text)
        usage = None
        if tokens_match:
            usage = {"tokens": int(tokens_match.group(1).replace(",", ""))}
            response_text = response_text[: tokens_match.start()]

        session_id = None
        m = re.search(r"session id:\s*(\S+)", text)
        if m:
            session_id = m.group(1)

        content = extract_sentinel_content(response_text)
        if content is not None:
            return AgentReply(text=content, session_id=session_id, usage=usage)

        return AgentReply(text=response_text.strip(), session_id=session_id, usage=usage)

    def env_overrides(self) -> dict:
        return {}


CODEX_BACKEND = CodexBackend()
