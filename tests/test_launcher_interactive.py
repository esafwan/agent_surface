"""Tests for the `--with-agent` interactive handoff in surface/launcher.py."""

import json
import subprocess
import sys

import pytest

from surface.cli import SurfaceCLI
from surface.launcher import context_hint, open_agent_terminal, resume
from surface.store import Store


class TestOpenAgentTerminal:
    def test_claude_builds_correctly_escaped_applescript(self, tmp_path, monkeypatch):
        # A path with both a space and a double quote to actually exercise
        # the escaping, not just a clean path.
        project_dir = tmp_path / 'my "project" dir'
        project_dir.mkdir()

        captured = {}

        def _fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, returncode=0)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            "surface.launcher.resolve_binary",
            lambda name: "/usr/local/bin/claude",
        )

        result = open_agent_terminal(project_dir, "claude")

        assert result is True
        assert captured["argv"][0] == "osascript"
        script = captured["argv"][2]
        assert "do script" in script
        # The inner shell command, unescaped, should be exactly this:
        inner = f'cd {project_dir} && /usr/local/bin/claude --continue'
        # We can't trivially un-escape AppleScript here, but we can check
        # that the quoted project dir and binary/flag both appear, and that
        # the embedded double quote from the path was escaped (\") rather
        # than breaking out of the AppleScript string literal.
        assert "--continue" in script
        assert "/usr/local/bin/claude" in script
        assert '\\"project\\"' in script or '\\\\"' in script
        assert str(project_dir).replace('"', '\\"') in script

    def test_unknown_backend_returns_false_without_subprocess(self, tmp_path, monkeypatch):
        called = {"ran": False}

        def _fake_run(*args, **kwargs):
            called["ran"] = True
            return subprocess.CompletedProcess(args, returncode=0)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(sys, "platform", "darwin")

        result = open_agent_terminal(tmp_path, "agy")

        assert result is False
        assert called["ran"] is False

    def test_non_darwin_returns_false_without_subprocess(self, tmp_path, monkeypatch):
        called = {"ran": False}

        def _fake_run(*args, **kwargs):
            called["ran"] = True
            return subprocess.CompletedProcess(args, returncode=0)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            "surface.launcher.resolve_binary",
            lambda name: "/usr/local/bin/claude",
        )

        result = open_agent_terminal(tmp_path, "claude")

        assert result is False
        assert called["ran"] is False


class TestContextHint:
    def test_empty_project_does_not_crash(self, tmp_path):
        db_path = tmp_path / "state.sqlite3"
        store = Store(str(db_path))
        try:
            hint = context_hint(store.conn)
        finally:
            store.conn.close()

        assert isinstance(hint, str)
        assert "stage: unknown" in hint
        assert "open items: 0" in hint
        assert "recent events: none" in hint


class TestResumeIntegration:
    def test_with_agent_unknown_backend_still_returns_normally(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / ".surface-board" / "state.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(db_path))
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("stage_config", json.dumps({"id": "movie"})),
        )
        store.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
            ("launch_profile", json.dumps({
                "version": 1,
                "agent": "fake",
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

        # Don't actually run the (blocking) board loop.
        monkeypatch.setattr(SurfaceCLI, "serve", lambda self, **kwargs: None)

        code = resume(tmp_path, with_agent=True)

        assert code == 0
        captured = capsys.readouterr()
        assert "no interactive mode known for backend 'fake'" in captured.out


class TestUrlDoesNotLeakToken:
    def test_detect_live_runtime_url_has_no_token(self, tmp_path, monkeypatch):
        import os
        import socket

        from surface.launcher import detect_live_runtime

        token = "super-secret-token-value"
        run_dir = tmp_path / ".surface-board" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            (run_dir / "port").write_text(str(port))
            (run_dir / "board.pid").write_text("12345")
            (run_dir / "token").write_text(token)
            monkeypatch.setattr(os, "kill", lambda pid, sig: None)

            url = detect_live_runtime(tmp_path)

            assert url == f"http://127.0.0.1:{port}/"
            assert token not in url
        finally:
            sock.close()
