"""Tests for surface/launcher.py ("surface resume")."""

import json
import os
import socket

import pytest

from surface.agents.registry import BACKENDS
from surface.cli import SurfaceCLI
from surface.launch_profile import save_profile
from surface.launcher import build_serve_argv, detect_live_runtime, resume
from surface.store import Store


def _write_runtime_files(run_dir, port, pid, token="tok123"):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "port").write_text(str(port))
    (run_dir / "board.pid").write_text(str(pid))
    (run_dir / "token").write_text(token)


class TestDetectLiveRuntime:
    def test_live_runtime_returns_url(self, tmp_path, monkeypatch):
        run_dir = tmp_path / ".surface-board" / "run"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            _write_runtime_files(run_dir, port=port, pid=12345, token="tok123")
            monkeypatch.setattr(os, "kill", lambda pid, sig: None)

            url = detect_live_runtime(tmp_path)
            assert url == f"http://surface:tok123@127.0.0.1:{port}/"
        finally:
            sock.close()

    def test_dead_pid_returns_none(self, tmp_path, monkeypatch):
        run_dir = tmp_path / ".surface-board" / "run"
        _write_runtime_files(run_dir, port=54321, pid=99999)

        def _kill(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", _kill)

        assert detect_live_runtime(tmp_path) is None

    def test_missing_files_returns_none(self, tmp_path):
        assert detect_live_runtime(tmp_path) is None


class TestBuildServeArgv:
    def test_all_fields_set_produces_all_flags(self, tmp_path):
        profile = {
            "agent": "fake",
            "agent_model": "some-model",
            "agent_timeout": 90,
        }
        argv = build_serve_argv(profile, tmp_path)
        assert argv[0] == "surface"
        assert "--db" in argv
        assert argv[argv.index("serve") :] == [
            "serve",
            "--agent", "fake",
            "--agent-model", "some-model",
            "--agent-timeout", "90",
        ]

    def test_agent_none_omits_flag(self, tmp_path):
        profile = {"agent": None, "agent_model": None, "agent_timeout": None}
        argv = build_serve_argv(profile, tmp_path)
        assert "--agent" not in argv
        assert "--agent-model" not in argv
        assert "--agent-timeout" not in argv
        assert argv[-1] == "serve"


class TestResume:
    def test_no_project_returns_exit_code_2(self, tmp_path, capsys):
        code = resume(tmp_path)
        assert code == 2
        captured = capsys.readouterr()
        assert "surface init" in captured.out

    def test_unregistered_agent_returns_exit_code_2(self, tmp_path, capsys):
        db_path = tmp_path / ".surface-board" / "state.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(db_path))
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("stage_config", json.dumps({"id": "movie"})),
        )
        store.conn.commit()
        # Write a profile referencing an agent name that isn't registered,
        # bypassing save_profile's own validation (which would reject it).
        assert "not-a-real-agent" not in BACKENDS
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("launch_profile", json.dumps({
                "version": 1,
                "agent": "not-a-real-agent",
                "agent_model": None,
                "agent_timeout": None,
                "stage": "movie",
                "interactive": False,
                "resume_context": True,
                "schema": None,
            })),
        )
        store.conn.commit()
        store.conn.close()

        code = resume(tmp_path)
        assert code == 2
        captured = capsys.readouterr()
        assert "not-a-real-agent" in captured.out
        assert "unknown agent" in captured.out

    def test_live_runtime_opens_browser_without_touching_serve(self, tmp_path, monkeypatch):
        run_dir = tmp_path / ".surface-board" / "run"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            _write_runtime_files(run_dir, port=port, pid=12345, token="tok123")
            monkeypatch.setattr(os, "kill", lambda pid, sig: None)

            opened = {}
            monkeypatch.setattr(
                "webbrowser.open", lambda url: opened.setdefault("url", url)
            )

            def _fail_serve(*args, **kwargs):
                raise AssertionError("serve() should not be invoked on a live runtime")

            monkeypatch.setattr(SurfaceCLI, "serve", _fail_serve)

            code = resume(tmp_path)
            assert code == 0
            assert opened["url"] == f"http://surface:tok123@127.0.0.1:{port}/"
        finally:
            sock.close()

    def test_no_live_runtime_dispatches_to_serve_with_profile_args(self, tmp_path, monkeypatch):
        db_path = tmp_path / ".surface-board" / "state.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(db_path))
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("stage_config", json.dumps({"id": "movie"})),
        )
        store.conn.commit()
        save_profile(store.conn, {
            "version": 1,
            "agent": "fake",
            "agent_model": None,
            "agent_timeout": 45,
            "stage": "movie",
            "interactive": False,
            "resume_context": True,
            "schema": None,
        })
        store.conn.close()

        captured = {}

        def _fake_serve(self, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(SurfaceCLI, "serve", _fake_serve)

        code = resume(tmp_path)
        assert code == 0
        assert captured["agent"] == "fake"
        assert captured["agent_timeout"] == 45
        assert captured["db"] == str(db_path)
