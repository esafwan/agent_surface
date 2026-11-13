"""Registry of known AgentBackend implementations."""

from surface.agents.agy_backend import AGY_BACKEND
from surface.agents.base import AgentBackend
from surface.agents.codex_backend import CODEX_BACKEND
from surface.agents.fake_backend import FAKE_BACKEND
from surface.agents.opencode_backend import OPENCODE_BACKEND

BACKENDS: dict[str, AgentBackend] = {
    "fake": FAKE_BACKEND,
    "opencode": OPENCODE_BACKEND,
    "codex": CODEX_BACKEND,
    "agy": AGY_BACKEND,
}


def get_backend(name: str) -> AgentBackend:
    """Look up a registered AgentBackend by name.

    Raises KeyError with a helpful message listing known backend names if
    `name` is not registered.
    """
    try:
        return BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(BACKENDS.keys())) or "(none registered)"
        raise KeyError(f"Unknown agent backend {name!r}; known backends: {known}")
