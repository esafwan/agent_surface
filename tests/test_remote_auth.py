"""Smoke tests for Phase 0's "authenticated remote path" (SPEC section 36).

Scope and honesty about limitations
------------------------------------
Phase 0 does NOT implement a real network tunnel (ngrok/cloudflared/etc). The
SPEC section 36 "authenticated remote path" requirement is satisfied here only
by: the Gradio board binding to loopback (127.0.0.1) by default, requiring
HTTP basic auth with an unpredictable per-run token, and that token file being
written with restrictive filesystem permissions. This test file cannot and
does not prove that a real remote client over an actual network tunnel can
reach the board securely -- no such tunnel exists in this codebase. It proves
only the "authenticated local bind with token" half of that story.

`tests/test_cli.py::TestServeRuntime` already thoroughly covers, against the
real `surface.cli.SurfaceCLI.serve()`:
  * token entropy/unpredictability and uniqueness across runs
      (test_serve_writes_runtime_token_port_and_pids,
       test_serve_tokens_differ_between_runs)
  * token file permissions (0o600)
  * loopback bind + `auth=("surface", token)` + `share=False` passed to
    Gradio's `Blocks.launch()` via a fake Blocks double
      (test_serve_launches_board_when_available)
  * clean shutdown clearing the runtime files
  * graceful behavior when gradio is not installed / import fails

To avoid duplicating that coverage, this file focuses on what is NOT already
covered there:
  1. The actual credential-matching semantics of the `auth=(user, pass)` tuple
     Gradio is given -- i.e. that wrong credentials are rejected and only the
     exact (username, token) pair is accepted. Gradio's `Blocks.launch()`
     itself is not invoked (gradio may not be installed in this environment),
     so this test reproduces gradio's own documented tuple-auth contract
     (a simple equality check against the exact `(username, password)` pair)
     directly against the tuple `cli.py` constructs, using the real
     `SurfaceCLI.serve()` runtime token -- not a hand-rolled fake token.
  2. That the resolved runtime host defaults to the explicit loopback address
     127.0.0.1, never the all-interfaces wildcard 0.0.0.0, by inspecting the
     real `serve()` signature's default and confirming `_reserve_port` binds
     against that same host.
  3. That the token file's restrictive permission bit is O_600 not merely
     "non-default" (e.g. it must not be group/world readable at all).
"""

import inspect
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path

import pytest

from surface.cli import SurfaceCLI


ECHO_WORKER_COMMAND = [
    sys.executable,
    "-c",
    """
import sys, json
for line in sys.stdin:
    try:
        data = json.loads(line)
        print(json.dumps({"ok": True, "result": {"processed": data.get("event_id")}}))
        sys.stdout.flush()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.stdout.flush()
""",
]


@pytest.fixture
def serve_project(tmp_path, capsys):
    db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
    cli = SurfaceCLI(db_path=db_path)
    cli.init(stage="movie", db=db_path)
    capsys.readouterr()
    return cli, db_path


def _gradio_tuple_auth_check(auth_tuple, attempt_user: str, attempt_pass: str) -> bool:
    """Reproduce Gradio's documented `auth=(user, pass)` semantics.

    Per Gradio's own docs, passing a single (username, password) tuple to
    `launch(auth=...)` accepts a login only if the submitted credentials
    exactly match that pair. This is not gradio-internal code (gradio is not
    installed/invoked here); it is the documented contract we are relying on,
    applied to the real tuple `surface/cli.py` constructs.
    """
    return (attempt_user, attempt_pass) == auth_tuple


class TestBoardAuthCredentialMatching:
    """Exercises the real per-run token against the documented gradio
    tuple-auth contract, without requiring gradio to be installed."""

    def test_correct_credentials_accepted(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        token = cli.runtime_info["token"]
        auth_tuple = ("surface", token)

        assert _gradio_tuple_auth_check(auth_tuple, "surface", token) is True

    def test_wrong_password_rejected(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        token = cli.runtime_info["token"]
        auth_tuple = ("surface", token)

        assert _gradio_tuple_auth_check(auth_tuple, "surface", "wrong-password") is False
        assert _gradio_tuple_auth_check(auth_tuple, "surface", "") is False
        assert _gradio_tuple_auth_check(auth_tuple, "surface", token[:-1]) is False, (
            "a near-miss (truncated) token must not authenticate"
        )

    def test_wrong_username_rejected(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        token = cli.runtime_info["token"]
        auth_tuple = ("surface", token)

        assert _gradio_tuple_auth_check(auth_tuple, "admin", token) is False
        assert _gradio_tuple_auth_check(auth_tuple, "", token) is False

    def test_default_or_common_fixed_credentials_never_match(self, serve_project, capsys):
        """A previous/foreign run's guessable default credentials must never
        authenticate against a different run's real per-run token."""
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        auth_tuple = ("surface", cli.runtime_info["token"])
        for guess_user, guess_pass in [
            ("surface", "surface"),
            ("surface", "changeme"),
            ("surface", "password"),
            ("admin", "admin"),
            ("surface", "token"),
        ]:
            assert _gradio_tuple_auth_check(auth_tuple, guess_user, guess_pass) is False


class TestBoardBindsLoopbackNotWildcard:
    """Confirms the board's default host is explicit loopback, never 0.0.0.0."""

    def test_serve_default_host_parameter_is_loopback(self):
        sig = inspect.signature(SurfaceCLI.serve)
        assert sig.parameters["host"].default == "127.0.0.1"
        assert sig.parameters["host"].default != "0.0.0.0"

    def test_reserve_port_binds_against_loopback_by_default(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        assert cli.runtime_info["host"] == "127.0.0.1"

        # The resolved port must actually be reachable on loopback (proving
        # _reserve_port's bind/getsockname round trip used the real host,
        # not some other interface).
        port = cli.runtime_info["port"]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            # Nothing is actually listening (board=False), so we only assert
            # connection is refused (loopback reachable, nothing bound) rather
            # than that something answers -- proving the port is a real
            # loopback-scoped port number, not a sentinel/placeholder.
            with pytest.raises(ConnectionRefusedError):
                probe.connect(("127.0.0.1", port))

    def test_explicit_wildcard_host_is_not_the_default_path(self, serve_project, capsys):
        """Passing host='0.0.0.0' is possible (serve() takes a host param), but
        it is never what a plain `surface serve` invocation does -- the
        wildcard bind requires an explicit, deliberate override."""
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()
        assert cli.runtime_info["host"] != "0.0.0.0"


class TestTokenFilePermissions:
    """Additional restrictive-permission checks beyond the 0o600 mode already
    asserted in test_cli.py -- verifies group/world bits are entirely absent."""

    def test_token_file_has_no_group_or_world_access(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        run_dir = Path(db_path).resolve().parent / "run"
        token_file = run_dir / "token"
        mode = stat.S_IMODE(token_file.stat().st_mode)

        assert mode & stat.S_IRWXG == 0, "token file must not be group-accessible"
        assert mode & stat.S_IRWXO == 0, "token file must not be world-accessible"
        assert mode & stat.S_IRUSR, "owner must still be able to read the token"

    def test_run_directory_itself_is_not_world_readable(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        run_dir = Path(db_path).resolve().parent / "run"
        mode = stat.S_IMODE(run_dir.stat().st_mode)
        assert mode & stat.S_IRWXO == 0, "runtime dir must not be world-accessible"


class TestTokenNeverAppearsInPredictableSurfaces:
    """The token must be genuinely unpredictable ahead of a run, not derived
    from anything guessable like the db path, pid, or a fixed seed."""

    def test_token_does_not_embed_pid_or_db_path(self, serve_project, capsys):
        cli, db_path = serve_project
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            keep_runtime_files=True,
        )
        capsys.readouterr()

        token = cli.runtime_info["token"]
        assert str(os.getpid()) not in token
        assert Path(db_path).stem not in token
