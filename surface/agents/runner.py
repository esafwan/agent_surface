"""
run_agent(): spawns an agent backend's CLI as a subprocess, enforces a timeout
by killing the whole process group, and parses the result via the backend's
parse_output().
"""

import os
import re
import signal
import subprocess
import time
from typing import Optional

from surface.agents.base import AgentBackend, AgentError, AgentReply

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

SENTINEL_START = "<<<SURFACE_CONTENT"
SENTINEL_END = "SURFACE_CONTENT>>>"


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


def extract_sentinel_content(text: str) -> Optional[str]:
    """Extract the content between SENTINEL_START and SENTINEL_END, if present.

    Returns None if either sentinel is missing or malformed.
    """
    if text is None:
        return None
    start_idx = text.find(SENTINEL_START)
    if start_idx == -1:
        return None
    start_idx += len(SENTINEL_START)
    end_idx = text.find(SENTINEL_END, start_idx)
    if end_idx == -1:
        return None
    return text[start_idx:end_idx].strip()


def run_agent(
    backend: AgentBackend,
    prompt: str,
    *,
    model: Optional[str] = None,
    timeout: float = 60.0,
    workdir: str = ".",
    log_dir: Optional[str] = None,
) -> AgentReply:
    """Run `backend`'s CLI with `prompt`, enforcing `timeout`.

    Raises AgentError(kind="timeout"|"crash"|"bad_output") on failure.
    """
    argv = backend.build_argv(prompt, model=model, workdir=workdir, session_id=None)

    env = dict(os.environ)
    env.update(backend.env_overrides() or {})

    popen_kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir,
        env=env,
    )
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""

    returncode = proc.returncode

    if log_dir:
        _write_log(log_dir, backend.name, argv, stdout, stderr)

    if timed_out:
        raise AgentError(
            f"Agent {backend.name!r} timed out after {timeout}s", kind="timeout"
        )

    if returncode != 0:
        tail = (stderr or "")[-2000:]
        raise AgentError(
            f"Agent {backend.name!r} exited with code {returncode}: {tail}",
            kind="crash",
        )

    clean_stdout = strip_ansi(stdout or "")
    clean_stderr = strip_ansi(stderr or "")

    reply = backend.parse_output(clean_stdout, clean_stderr, returncode)

    if reply is None or not getattr(reply, "text", None):
        raise AgentError(
            f"Agent {backend.name!r} produced no usable output", kind="bad_output"
        )

    return reply


def _write_log(log_dir: str, backend_name: str, argv: list, stdout: str, stderr: str) -> None:
    """Best-effort write of raw stdout/stderr/argv to a log file. Never raises."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        ts = int(time.time())
        path = os.path.join(log_dir, f"{backend_name}-{ts}.log")
        with open(path, "w") as f:
            f.write(f"argv: {argv!r}\n")
            f.write("--- stdout ---\n")
            f.write(stdout or "")
            f.write("\n--- stderr ---\n")
            f.write(stderr or "")
    except Exception:
        pass
