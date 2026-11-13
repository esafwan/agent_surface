"""
AgentBackend protocol and shared types for agent CLI adapters.

An AgentBackend knows how to build an argv for a specific agent CLI (e.g. a
"fake" test backend, or later opencode/codex/agy) and how to parse that CLI's
raw stdout/stderr/returncode into a normalized AgentReply.
"""

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

# Allowed AgentError kinds.
_VALID_KINDS = {"auth", "rate_limit", "timeout", "bad_output", "crash"}


@dataclass
class AgentReply:
    """Normalized result of running an agent backend."""

    text: str
    session_id: Optional[str] = None
    usage: Optional[dict] = None


class AgentError(Exception):
    """Raised when an agent backend invocation fails.

    `kind` must be one of: "auth", "rate_limit", "timeout", "bad_output", "crash".
    """

    def __init__(self, message: str, *, kind: str):
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"Invalid AgentError kind {kind!r}; must be one of {sorted(_VALID_KINDS)}"
            )
        super().__init__(message)
        self.kind = kind
        self.message = message


@runtime_checkable
class AgentBackend(Protocol):
    """Protocol implemented by each agent CLI adapter."""

    name: str
    supports_resume: bool

    def build_argv(
        self,
        prompt: str,
        *,
        model: Optional[str],
        workdir: str,
        session_id: Optional[str],
    ) -> list[str]:
        """Build the subprocess argv to invoke this agent CLI with the given prompt."""
        ...

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        out_file: Optional[str] = None,
    ) -> AgentReply:
        """Parse raw process output into a normalized AgentReply.

        Should raise AgentError (kind="bad_output") if no usable content is found.
        """
        ...

    def env_overrides(self) -> dict:
        """Environment variable overrides to apply when spawning this backend's process."""
        ...
