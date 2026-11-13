"""Registry of known AgentBackend implementations."""

from surface.agents.agy_backend import AGY_BACKEND
from surface.agents.base import AgentBackend, which_binary
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


def is_available(name: str) -> bool:
    """Return True if the named backend's CLI binary is installed (or needs none).

    A backend whose `binary` is "" (e.g. the fake test backend) is always
    available. Raises KeyError if `name` is not a registered backend.
    """
    backend = get_backend(name)
    if backend.binary == "":
        return True
    return which_binary(backend.binary) is not None


def resolve_binary(name: str) -> str | None:
    """Resolve the named backend's CLI binary to an absolute path.

    Returns the path from PATH lookup when found, None when the binary is not
    on PATH, and the raw `binary` string ("") for backends that need no PATH
    lookup. Raises KeyError if `name` is not a registered backend.
    """
    backend = get_backend(name)
    if backend.binary == "":
        return ""
    return which_binary(backend.binary)
