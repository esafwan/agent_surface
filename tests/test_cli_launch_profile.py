"""Tests for wiring `surface/cli.py`'s `init`/`serve` to the launch profile.

These cover: `init` writing `stage` into the profile, `serve` updating only
the fields its flags correspond to, a flagless `serve` leaving a previously
saved profile untouched, and a flagless `serve` with no profile at all
behaving exactly like the pre-launch-profile default (no agent flags passed
to the worker subprocess, default turn timeout).
"""

import sys

import pytest

from surface.cli import SurfaceCLI
from surface.launch_profile import load_profile

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


def test_init_writes_stage_into_profile(tmp_path, capsys):
    db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
    cli = SurfaceCLI(db_path=db_path)
    cli.init(stage="poem", db=db_path)
    capsys.readouterr()

    store = cli.get_store()
    profile = load_profile(store.conn)
    assert profile is not None
    assert profile["stage"] == "poem"


@pytest.fixture
def serve_project(tmp_path, capsys):
    """An initialized project ready for serve()."""
    db_path = str(tmp_path / ".surface-board" / "state.sqlite3")
    cli = SurfaceCLI(db_path=db_path)
    cli.init(stage="movie", db=db_path)
    capsys.readouterr()
    return cli, db_path


class TestServeLaunchProfileWiring:
    def test_serve_with_agent_flags_updates_only_those_fields(self, serve_project, capsys):
        cli, db_path = serve_project
        store = cli.get_store()
        store.create_artifact(id="script_1", stage="script", title="Script")

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            agent="fake",
            agent_model="m",
        )
        capsys.readouterr()

        profile = load_profile(store.conn)
        assert profile is not None
        assert profile["agent"] == "fake"
        assert profile["agent_model"] == "m"
        # Fields not touched by the flags used in this call stay at defaults.
        assert profile["agent_timeout"] is None
        # init's stage write is untouched by serve.
        assert profile["stage"] == "movie"
        assert profile["interactive"] is False
        assert profile["resume_context"] is True

    def test_second_flagless_serve_does_not_reset_saved_profile(self, serve_project, capsys):
        cli, db_path = serve_project
        store = cli.get_store()
        store.create_artifact(id="script_1", stage="script", title="Script")

        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
            agent="fake",
            agent_model="m",
            agent_timeout=77,
        )
        capsys.readouterr()

        store.create_artifact(id="script_2", stage="script", title="Script 2")

        # No flags this time.
        cli.serve(
            db=db_path,
            max_iterations=1,
            board=False,
            worker_command=ECHO_WORKER_COMMAND,
        )
        capsys.readouterr()

        profile = load_profile(store.conn)
        assert profile["agent"] == "fake"
        assert profile["agent_model"] == "m"
        assert profile["agent_timeout"] == 77

    def test_serve_no_flags_no_profile_matches_old_default_behavior(self, serve_project, capsys):
        """No --agent flags and no pre-existing profile: identical to the
        pre-launch-profile behavior (no agent flags reach the worker
        subprocess, default turn timeout of 300 is used)."""
        cli, db_path = serve_project
        store = cli.get_store()
        store.create_artifact(id="script_1", stage="script", title="Script")

        import surface.cli as cli_module
        real_transport = cli_module.NativeStreamTransport
        captured_args = {}

        def _spy(command=None, timeout=None, **kwargs):
            captured_args["command"] = command
            captured_args["timeout"] = timeout
            return real_transport(command=ECHO_WORKER_COMMAND, timeout=timeout, **kwargs)

        cli_module.NativeStreamTransport = _spy
        try:
            cli.serve(
                db=db_path,
                max_iterations=1,
                board=False,
            )
        finally:
            cli_module.NativeStreamTransport = real_transport

        capsys.readouterr()

        assert "--agent" not in captured_args["command"]
        assert "--agent-model" not in captured_args["command"]
        assert "--agent-timeout" not in captured_args["command"]
        assert captured_args["timeout"] == 300

        # The profile now exists (created on init), but agent fields remain
        # at their None defaults since no flags were ever passed.
        profile = load_profile(store.conn)
        assert profile["agent"] is None
        assert profile["agent_model"] is None
        assert profile["agent_timeout"] is None
