"""`surface resume`: reattach to a live board, or start one from the saved
launch profile (SPEC-adjacent T3b: launcher for the Surface App).

Three pieces:
  * `detect_live_runtime` -- is there a genuinely live board for this
    project right now, and if so what's its URL.
  * a resume-lock (`run/resume.lock`, `fcntl.flock`) -- serializes
    concurrent `resume` invocations so two don't both decide "not live,
    I'll start one" at the same time.
  * `resume` -- the CLI entry point: reattach if live, otherwise validate
    the saved launch profile and hand off to `SurfaceCLI.serve` in-process.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from string import Template
from typing import Optional

from surface.agents.registry import BACKENDS, is_available, resolve_binary
from surface.launch_profile import DEFAULTS, load_profile
from surface.store import Store

# Backends with a known interactive/resume CLI mode. Backends not listed
# here (e.g. "agy", "fake") have no known interactive mode -- that's the
# "unknown" case handled by `open_agent_terminal`, not an error.
INTERACTIVE: dict[str, dict] = {
    "claude": {"cmd": "claude", "resume_flag": ["--continue"]},
    "codex": {"cmd": "codex", "resume_flag": ["resume", "--last"]},
    "opencode": {"cmd": "opencode", "resume_flag": ["--continue"]},
}

RESUME_LOCK_TIMEOUT = 3.0
RESUME_LOCK_POLL = 0.1


def _run_dir_for(project_dir: Path) -> Path:
    return project_dir / ".surface-board" / "run"


def _db_path_for(project_dir: Path) -> Path:
    return project_dir / ".surface-board" / "state.sqlite3"


def detect_live_runtime(project_dir: Path) -> Optional[str]:
    """Return the board URL if a runtime is genuinely live, else None.

    "Genuinely live" requires all three:
      * `run/port` and `run/board.pid` both exist and parse as numbers;
      * the pid is alive (`os.kill(pid, 0)`, treating `PermissionError`
        as "alive but not ours" and `ProcessLookupError` as dead);
      * the port actually accepts a TCP connection -- pid-liveness alone
        is not trusted, since a recycled pid can give a false positive.

    The URL is the plain board origin (`http://127.0.0.1:<port>/`), with no
    embedded credentials -- `serve()` protects the board with HTTP basic
    auth (see `board_web.check_basic_auth`) but never prints a URL with the
    token embedded in it, and embedding a secret in a URL that gets passed
    to `webbrowser.open()` (or logged) risks leaking it via browser history
    or process argv. The caller is responsible for telling the user how to
    authenticate (see `resume()`); the browser's own basic-auth prompt
    handles the rest interactively.
    """
    run_dir = _run_dir_for(Path(project_dir))
    port_file = run_dir / "port"
    pid_file = run_dir / "board.pid"

    if not port_file.exists() or not pid_file.exists():
        return None

    try:
        port = int(port_file.read_text().strip())
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # process exists, just not ours to signal -- still alive
    except OSError:
        return None

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return None

    return f"http://127.0.0.1:{port}/"


@contextmanager
def _resume_lock(run_dir: Path, timeout: float = RESUME_LOCK_TIMEOUT,
                  poll: float = RESUME_LOCK_POLL):
    """Serialize concurrent `resume` invocations via `run/resume.lock`.

    Blocking-with-timeout: retries `flock(LOCK_EX | LOCK_NB)` for up to
    `timeout` seconds rather than blocking forever. Yields True if the
    lock was acquired, False if the timeout elapsed first -- callers
    should treat False as "another resume is handling it" and re-check
    rather than proceeding to start a second runtime.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "resume.lock"
    lock_path.touch(exist_ok=True)
    fd = open(lock_path, "w")
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        fd.close()


def build_serve_argv(profile: dict, project_dir: Path) -> list[str]:
    """Build the `surface --db <path> serve [...]` argv for `profile`.

    Omits `--agent`/`--agent-model`/`--agent-timeout` entirely when the
    corresponding profile value is None, matching serve's own "no flag
    = old headless default" behavior.
    """
    db_path = _db_path_for(Path(project_dir))
    argv = ["surface", "--db", str(db_path), "serve"]

    agent = profile.get("agent")
    agent_model = profile.get("agent_model")
    agent_timeout = profile.get("agent_timeout")

    if agent is not None:
        argv.extend(["--agent", str(agent)])
    if agent_model is not None:
        argv.extend(["--agent-model", str(agent_model)])
    if agent_timeout is not None:
        argv.extend(["--agent-timeout", str(agent_timeout)])

    return argv


def _fallback_profile_from_stage_config(store: Store) -> Optional[dict]:
    """Synthesize a minimal profile from the raw `stage_config` kv row.

    Older projects created before launch_profile landed never got a
    `launch_profile` row written, but do have `stage_config` -- don't
    hard-fail resume just because the newer row is missing.
    """
    cursor = store.conn.cursor()
    cursor.execute("SELECT value_json FROM kv_state WHERE key = ?", ("stage_config",))
    row = cursor.fetchone()
    if not row:
        return None
    try:
        stage_data = json.loads(row[0])
    except (TypeError, ValueError):
        stage_data = {}
    profile = dict(DEFAULTS)
    profile["stage"] = stage_data.get("id")
    return profile


def context_hint(conn, n: int = 10) -> str:
    """Build a short human-readable summary of the project's current state.

    Pulled from the store (stage, open/unapproved board items, and the last
    `n` events) so a human picking up an interactive agent session gets
    oriented without having to open the board. This describes
    agent_surface's own persisted state, not any prior LLM conversation.
    """
    store = Store(conn)

    stage = None
    cursor = conn.cursor()
    cursor.execute("SELECT value_json FROM kv_state WHERE key = ?", ("stage_config",))
    row = cursor.fetchone()
    if row:
        try:
            stage = json.loads(row[0]).get("id")
        except (TypeError, ValueError, AttributeError):
            stage = None
    if stage is None:
        profile = load_profile(conn)
        if profile:
            stage = profile.get("stage")

    artifacts = store.list_artifacts()
    open_items = [a for a in artifacts if a.get("status") not in ("approved", "done")]

    events = store.list_active_events()
    recent = events[-n:] if events else []

    lines = [
        "Project context (agent_surface's own state — not your prior conversation):",
        f"  stage: {stage or 'unknown'}",
        f"  open items: {len(open_items)}",
    ]
    if recent:
        descriptions = []
        for ev in recent:
            kind = ev.get("type") or ev.get("event_type") or "event"
            created_at = ev.get("created_at") or "unknown time"
            descriptions.append(f"{kind} ({created_at})")
        lines.append(f"  recent events: {', '.join(descriptions)}")
    else:
        lines.append("  recent events: none")

    return "\n".join(lines)


def open_agent_terminal(project_dir: Path, backend_name: str) -> bool:
    """Open a new Terminal window resuming `backend_name`'s interactive CLI.

    Returns True if a terminal was successfully launched, False if the
    backend has no known interactive mode, isn't on PATH, or this isn't
    macOS -- callers should treat False as "print a friendly fallback
    message", not as an error.
    """
    if backend_name not in INTERACTIVE:
        return False

    try:
        binary = resolve_binary(backend_name)
    except KeyError:
        return False
    if not binary:
        return False

    if sys.platform != "darwin":
        return False

    spec = INTERACTIVE[backend_name]
    parts = [str(project_dir)]
    cmd = (
        f"cd {shlex.quote(str(project_dir))} && "
        f"{shlex.quote(binary)} "
        f"{' '.join(shlex.quote(f) for f in spec['resume_flag'])}"
    )
    escaped_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    try:
        result = subprocess.run(
            ["osascript", "-e", f'tell application "Terminal" to do script "{escaped_cmd}"'],
            capture_output=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _agent_handoff(project_dir: Path, profile: Optional[dict], with_agent: bool,
                    conn=None) -> None:
    """Print context + open an interactive terminal when requested.

    Triggers when `with_agent` is True OR the profile's `interactive` flag
    is set -- either is sufficient. Never raises and never blocks board
    startup: a missing/unknown backend just prints a friendlier message.
    """
    if profile is None:
        return
    if not (with_agent or profile.get("interactive")):
        return

    agent_name = profile.get("agent")
    if agent_name is None:
        print("--with-agent: no agent configured for this project; skipping interactive handoff")
        return

    if profile.get("resume_context", True) and conn is not None:
        print(context_hint(conn))

    if open_agent_terminal(project_dir, agent_name):
        print(f"opened an interactive '{agent_name}' terminal session")
    else:
        print(f"no interactive mode known for backend '{agent_name}'; see docs")


def resume(project_dir: Path, no_browser: bool = False, with_agent: bool = False,
           db_override: Optional[str] = None) -> int:
    """Reattach to a live board, or start one from the saved launch profile.

    `db_override`, when given, takes precedence over the
    `<project_dir>/.surface-board/state.sqlite3` convention (mirrors the
    global `--db` flag taking precedence over the project-dir-derived
    default). The runtime directory follows the resolved db path's
    location (`<db parent>/run`), same as `serve()`'s own `runtime_dir()`.
    """
    project_dir = Path(project_dir).resolve()

    profile: Optional[dict] = None
    db_path = Path(db_override).resolve() if db_override else _db_path_for(project_dir)
    run_dir = db_path.parent / "run"

    with _resume_lock(run_dir) as acquired:
        url = detect_live_runtime(project_dir)
        if url is None and not acquired:
            # Lock busy -- another resume is presumably mid-decision.
            # Re-check once more before giving up.
            url = detect_live_runtime(project_dir)

        if url is not None:
            print(url)
            print("(sign in with user 'surface', password: see "
                  f"{run_dir / 'token'}, or it will prompt you)")
            if not no_browser:
                webbrowser.open(url)

            if db_path.exists():
                reattach_store = Store(str(db_path))
                try:
                    reattach_profile = load_profile(reattach_store.conn)
                    if reattach_profile is None:
                        reattach_profile = _fallback_profile_from_stage_config(reattach_store)
                    _agent_handoff(project_dir, reattach_profile, with_agent,
                                    conn=reattach_store.conn)
                finally:
                    reattach_store.conn.close()
            return 0

        if not acquired:
            print(
                f"could not acquire resume lock for {project_dir} "
                "(another 'surface resume' appears to be in progress)"
            )
            return 2

        if not db_path.exists():
            print(
                f"No project found at {project_dir} (or no stage configured) "
                "— run 'surface init --stage <name>' first"
            )
            return 2

        store = Store(str(db_path))
        try:
            profile = load_profile(store.conn)
            if profile is None:
                profile = _fallback_profile_from_stage_config(store)
            if profile is None:
                print(
                    f"No project found at {project_dir} (or no stage configured) "
                    "— run 'surface init --stage <name>' first"
                )
                return 2

            agent = profile.get("agent")
            if agent is not None:
                try:
                    available = is_available(agent)
                except KeyError:
                    known = ", ".join(sorted(BACKENDS.keys())) or "(none registered)"
                    print(
                        f"unknown agent '{agent}' in launch profile; run "
                        f"'surface serve --agent <one of: {known}>' to update it"
                    )
                    return 2
                if not available:
                    print(
                        f"agent '{agent}' not found on PATH; install it or "
                        "run 'surface serve --agent <other>'"
                    )
                    return 2

            _agent_handoff(project_dir, profile, with_agent, conn=store.conn)
        finally:
            store.conn.close()

        # Keep the resume lock held across the entire serve() call -- not
        # just the detect-and-decide step above. serve() does real work
        # (reserving a port, writing token/port/pid files, starting the
        # board thread) before the port actually accepts connections; if
        # the lock were released here, a second concurrent `resume()`
        # could acquire it, call `detect_live_runtime()` (which would
        # still return None because this serve() hasn't finished binding
        # yet), and start a second supervisor on the same DB. An
        # `fcntl.flock` is released automatically when this process exits
        # or the fd closes, so holding it for the server's lifetime is
        # safe -- a background/supervisor process holding a lock on its
        # own run-dir for its whole lifetime is the normal pattern here.
        from surface.cli import SurfaceCLI  # local import: avoid any import cycle

        cli = SurfaceCLI(db_path=str(db_path))
        cli.serve(
            db=str(db_path),
            stage=profile.get("stage") or "movie",
            agent=profile.get("agent"),
            agent_model=profile.get("agent_model"),
            agent_timeout=profile.get("agent_timeout"),
        )
        return 0


def render_command_script(fallback_bin: str) -> str:
    """Render the bundled `.command` launcher template.

    `fallback_bin` is substituted for the single `$fallback_bin` placeholder;
    every other `$` in the template is written doubled (`$$`) so
    `string.Template` emits it as a literal single `$`, preserving the
    shell script's own `$(...)`/`${...}` syntax untouched.
    """
    template_path = Path(__file__).parent / "templates" / "launcher.command"
    text = template_path.read_text(encoding="utf-8")
    # Escape shell-meaningful characters so a fallback_bin containing them
    # can't break the generated script's syntax or trigger unwanted
    # expansion inside the double-quoted `BIN="$fallback_bin"` assignment.
    # Order matters: backslash must be escaped first, or the backslashes
    # inserted by the later replacements would themselves get doubled.
    escaped_fallback_bin = (
        fallback_bin.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return Template(text).substitute(fallback_bin=escaped_fallback_bin)


def _resolve_fallback_bin() -> str:
    """Best-effort absolute path to the `surface` binary for the template.

    Preference order:
      1. `shutil.which("surface")` -- works whether this process was
         started via the installed console-script entry point or found on
         PATH some other way; it's the simplest, most portable case.
      2. `Path(sys.argv[0]).resolve()` -- when `surface` isn't on PATH
         right now but this process was clearly invoked as that script
         (argv[0] ends with "surface"), trust how it was actually invoked.

    Returns "" (never a multi-word string) when neither panned out --
    the rendered `.command` template's `[ ! -x "$BIN" ]` check and
    `"$BIN" resume .` invocation both require a single executable PATH,
    so there is no usable single-word fallback left to offer. The
    template handles an empty fallback by printing a clear error instead
    of trying to exec something broken.
    """
    which = shutil.which("surface")
    if which:
        return str(Path(which).resolve())

    argv0 = Path(sys.argv[0])
    if argv0.name == "surface":
        resolved = argv0.resolve()
        if resolved.exists():
            return str(resolved)

    return ""


def write_command_script(project_dir: Path, force: bool = False) -> Path:
    """Write a double-clickable `Open Surface Board.command` launcher.

    Raises `FileExistsError` if the target already exists and `force` is
    False. Sets the file mode to 0o755 (executable) either way.
    """
    project_dir = Path(project_dir).resolve()
    target = project_dir / "Open Surface Board.command"
    if target.exists() and not force:
        raise FileExistsError(str(target))

    fallback_bin = _resolve_fallback_bin()
    script = render_command_script(fallback_bin)
    target.write_text(script, encoding="utf-8")
    os.chmod(target, 0o755)
    return target
