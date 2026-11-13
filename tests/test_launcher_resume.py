"""Tests for surface/launcher.py ("surface resume")."""

import fcntl
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from surface.agents.registry import BACKENDS
from surface.cli import SurfaceCLI
from surface.launch_profile import save_profile
from surface.launcher import (
    _open_board,
    build_app_window_argv,
    build_serve_argv,
    detect_live_runtime,
    find_chromium_binary,
    open_app_window,
    resume,
)
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
            assert url == f"http://127.0.0.1:{port}/"
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
            assert opened["url"] == f"http://127.0.0.1:{port}/"
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

    def test_resume_lock_held_across_serve_prevents_second_concurrent_start(
        self, tmp_path, monkeypatch
    ):
        """Regression test for the resume-lock race.

        `_resume_lock` must stay held for the *entire* `serve()` call, not
        just the detect-and-decide step before it -- otherwise a second
        concurrent `resume()` can slip in while the first is still
        starting up (before its port/pid/token files are genuinely live)
        and kick off a second `serve()` on the same DB.

        This simulates that race deterministically: the stubbed `serve()`
        signals an "I've started" event and then blocks on a second event
        until released. While it's blocked, the test thread tries to grab
        `resume.lock` itself with a non-blocking `flock` -- if the
        production code releases the resume lock before calling `serve()`
        (the bug), that non-blocking acquire succeeds; if the lock is
        correctly held across `serve()`, it raises `OSError`. The key
        behavioral assertion is that the stubbed `serve()` is invoked at
        most once across both concurrent `resume()` calls.
        """
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
            "agent_timeout": None,
            "stage": "movie",
            "interactive": False,
            "resume_context": True,
            "schema": None,
        })
        store.conn.close()

        serve_started = threading.Event()
        serve_release = threading.Event()
        call_count = {"n": 0}
        lock_held_while_serving = {"value": None}

        def _fake_serve(self, **kwargs):
            call_count["n"] += 1
            serve_started.set()
            serve_release.wait(timeout=5)

        monkeypatch.setattr(SurfaceCLI, "serve", _fake_serve)

        results = []

        def _run_resume():
            results.append(resume(tmp_path))

        t1 = threading.Thread(target=_run_resume)
        t1.start()

        assert serve_started.wait(timeout=5), "serve() was never invoked"

        # While the first resume()'s serve() is "running", the resume lock
        # must still be held -- confirm it from the test thread with an
        # independent non-blocking flock attempt.
        run_dir = db_path.parent / "run"
        lock_path = run_dir / "resume.lock"
        probe_fd = open(lock_path, "w")
        try:
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held_while_serving["value"] = False
                fcntl.flock(probe_fd, fcntl.LOCK_UN)
            except OSError:
                lock_held_while_serving["value"] = True
        finally:
            probe_fd.close()

        # Fire a second concurrent resume() while the first is still
        # genuinely mid-serve (its serve() hasn't been released yet). It
        # must not start a second serve() -- it should either block on
        # the lock until it times out and backs off cleanly, or detect
        # the lock is held. Crucially, serve() must stay invoked exactly
        # once while the first resume() is still serving.
        t2 = threading.Thread(target=_run_resume)
        t2.start()
        # Let t2 run its full lock-wait-and-back-off cycle while t1's
        # serve() is still blocked (not yet released).
        t2.join(timeout=5)

        assert lock_held_while_serving["value"] is True, (
            "resume lock was released before serve() completed -- the race "
            "the lock is supposed to prevent"
        )
        assert call_count["n"] == 1, (
            f"serve() was invoked {call_count['n']} times while the first "
            "resume() was still serving; expected exactly 1"
        )

        serve_release.set()
        t1.join(timeout=5)

        assert call_count["n"] == 1, (
            f"serve() was invoked {call_count['n']} times in total; expected exactly 1"
        )

    def test_fresh_start_polls_and_opens_browser_before_serve_releases(
        self, tmp_path, monkeypatch
    ):
        """When no live runtime exists yet, resume() must start serve() in
        the background, poll detect_live_runtime() until it comes up, and
        open the browser to the real URL -- all before the (still-blocked)
        serve() call itself ever returns.
        """
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
            "agent_timeout": None,
            "stage": "movie",
            "interactive": False,
            "resume_context": True,
            "schema": None,
        })
        store.conn.close()

        run_dir = db_path.parent / "run"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        serve_release = threading.Event()

        def _fake_serve(self, **kwargs):
            # Simulate what a real serve() does before it starts listening
            # forever: reserve a port, write token/port/pid files.
            _write_runtime_files(run_dir, port=port, pid=os.getpid(), token="tok123")
            serve_release.wait(timeout=5)

        monkeypatch.setattr(SurfaceCLI, "serve", _fake_serve)

        opened = {}
        monkeypatch.setattr(
            "webbrowser.open", lambda url: opened.setdefault("url", url)
        )

        try:
            results = []

            def _run_resume():
                results.append(resume(tmp_path, _poll_timeout=5.0))

            t = threading.Thread(target=_run_resume)
            t.start()

            # The browser-open must happen while serve() is still blocked
            # (i.e. before serve_release is set).
            deadline = __import__("time").monotonic() + 5
            while "url" not in opened and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.05)

            assert opened.get("url") == f"http://127.0.0.1:{port}/"
            assert not serve_release.is_set()
            assert t.is_alive(), (
                "resume() returned before the serve thread was released -- "
                "it must join() and block until serve() actually finishes"
            )

            serve_release.set()
            t.join(timeout=5)
            assert not t.is_alive()
            assert results == [0]
        finally:
            sock.close()

    def test_fresh_start_timeout_when_board_never_comes_up(self, tmp_path, monkeypatch, capsys):
        """If serve() never manages to write the port/pid files (e.g. it
        failed to start), resume() must not hang forever -- it should print
        a clear timeout message and still join the serve thread cleanly.
        """
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
            "agent_timeout": None,
            "stage": "movie",
            "interactive": False,
            "resume_context": True,
            "schema": None,
        })
        store.conn.close()

        def _fake_serve(self, **kwargs):
            # Never writes port/pid files -- simulates a startup failure --
            # and returns fairly quickly (short block, not blocking forever).
            __import__("time").sleep(0.3)

        monkeypatch.setattr(SurfaceCLI, "serve", _fake_serve)

        opened = {}
        monkeypatch.setattr(
            "webbrowser.open", lambda url: opened.setdefault("url", url)
        )

        code = resume(tmp_path, _poll_timeout=0.5)
        assert code == 0
        assert "url" not in opened
        captured = capsys.readouterr()
        assert "did not come up" in captured.out

    def test_app_window_reaches_open_board_on_reattach(self, tmp_path, monkeypatch):
        run_dir = tmp_path / ".surface-board" / "run"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            _write_runtime_files(run_dir, port=port, pid=12345, token="tok123")
            monkeypatch.setattr(os, "kill", lambda pid, sig: None)

            captured = {}

            def _fake_open_board(url, project_dir, app_window):
                captured["url"] = url
                captured["project_dir"] = project_dir
                captured["app_window"] = app_window

            monkeypatch.setattr("surface.launcher._open_board", _fake_open_board)

            def _fail_serve(*args, **kwargs):
                raise AssertionError("serve() should not be invoked on a live runtime")

            monkeypatch.setattr(SurfaceCLI, "serve", _fail_serve)

            code = resume(tmp_path, app_window=True)
            assert code == 0
            assert captured["url"] == f"http://127.0.0.1:{port}/"
            assert captured["app_window"] is True
        finally:
            sock.close()

    def test_app_window_reaches_open_board_on_fresh_start(self, tmp_path, monkeypatch):
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
            "agent_timeout": None,
            "stage": "movie",
            "interactive": False,
            "resume_context": True,
            "schema": None,
        })
        store.conn.close()

        run_dir = db_path.parent / "run"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        def _fake_serve(self, **kwargs):
            _write_runtime_files(run_dir, port=port, pid=os.getpid(), token="tok123")

        monkeypatch.setattr(SurfaceCLI, "serve", _fake_serve)

        captured = {}

        def _fake_open_board(url, project_dir, app_window):
            captured["url"] = url
            captured["app_window"] = app_window

        monkeypatch.setattr("surface.launcher._open_board", _fake_open_board)

        try:
            code = resume(tmp_path, app_window=True, _poll_timeout=5.0)
            assert code == 0
            assert captured["url"] == f"http://127.0.0.1:{port}/"
            assert captured["app_window"] is True
        finally:
            sock.close()


class TestFindChromiumBinary:
    def test_detection_order_respected(self, monkeypatch, tmp_path):
        chrome_path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        edge_path = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        brave_path = Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        checked = []

        def _fake_exists(path):
            checked.append(str(path))
            # Only the Brave path (checked third) exists -- confirm Chrome
            # and Edge were checked first and both returned False.
            return str(path) == str(brave_path)

        monkeypatch.setattr(os.path, "exists", _fake_exists)

        result = find_chromium_binary()
        assert result == str(brave_path)
        assert str(chrome_path) in checked
        assert str(edge_path) in checked
        assert checked.index(str(chrome_path)) < checked.index(str(brave_path))
        assert checked.index(str(edge_path)) < checked.index(str(brave_path))

    def test_falls_back_to_shutil_which(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda path: False)

        def _fake_which(name):
            return "/usr/bin/chromium-browser" if name == "chromium-browser" else None

        monkeypatch.setattr(shutil, "which", _fake_which)

        assert find_chromium_binary() == "/usr/bin/chromium-browser"

    def test_returns_none_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda path: False)
        monkeypatch.setattr(shutil, "which", lambda name: None)

        assert find_chromium_binary() is None


class TestBuildAppWindowArgv:
    def test_exact_argv(self, tmp_path):
        profile_dir = tmp_path / "app-profile"
        argv = build_app_window_argv("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                                      "http://127.0.0.1:1234/", profile_dir)
        assert argv == [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "--app=http://127.0.0.1:1234/",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]


class TestOpenAppWindow:
    def test_no_binary_returns_false_without_popen(self, tmp_path, monkeypatch):
        monkeypatch.setattr("surface.launcher.find_chromium_binary", lambda: None)

        def _fail_popen(*args, **kwargs):
            raise AssertionError("Popen should not be called when no binary is found")

        monkeypatch.setattr(subprocess, "Popen", _fail_popen)

        assert open_app_window("http://127.0.0.1:1234/", tmp_path) is False

    def test_success_creates_profile_dir_and_calls_popen(self, tmp_path, monkeypatch):
        monkeypatch.setattr("surface.launcher.find_chromium_binary", lambda: "/fake/chrome")

        captured = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        result = open_app_window("http://127.0.0.1:1234/", tmp_path)
        assert result is True

        profile_dir = tmp_path / ".surface-board" / "app-profile"
        assert profile_dir.is_dir()
        assert captured["argv"] == [
            "/fake/chrome",
            "--app=http://127.0.0.1:1234/",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        assert captured["kwargs"]["start_new_session"] is True

    def test_popen_oserror_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("surface.launcher.find_chromium_binary", lambda: "/fake/chrome")

        def _raise_oserror(*args, **kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(subprocess, "Popen", _raise_oserror)

        assert open_app_window("http://127.0.0.1:1234/", tmp_path) is False


class TestOpenBoard:
    def test_app_window_true_and_succeeds_never_calls_webbrowser(self, tmp_path, monkeypatch):
        monkeypatch.setattr("surface.launcher.open_app_window", lambda url, pd: True)

        def _fail_webbrowser_open(url):
            raise AssertionError("webbrowser.open should not be called")

        monkeypatch.setattr("webbrowser.open", _fail_webbrowser_open)

        _open_board("http://127.0.0.1:1234/", tmp_path, True)

    def test_app_window_true_and_fails_falls_back_with_note(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("surface.launcher.open_app_window", lambda url, pd: False)

        opened = {}
        monkeypatch.setattr(
            "webbrowser.open", lambda url: opened.setdefault("url", url)
        )

        _open_board("http://127.0.0.1:1234/", tmp_path, True)

        assert opened["url"] == "http://127.0.0.1:1234/"
        captured = capsys.readouterr()
        assert "no Chromium-based browser found" in captured.out

    def test_app_window_false_calls_webbrowser_directly(self, tmp_path, monkeypatch, capsys):
        def _fail_open_app_window(*args, **kwargs):
            raise AssertionError("open_app_window should not be invoked when app_window is False")

        monkeypatch.setattr("surface.launcher.open_app_window", _fail_open_app_window)

        opened = {}
        monkeypatch.setattr(
            "webbrowser.open", lambda url: opened.setdefault("url", url)
        )

        _open_board("http://127.0.0.1:1234/", tmp_path, False)

        assert opened["url"] == "http://127.0.0.1:1234/"
        captured = capsys.readouterr()
        assert "no Chromium-based browser found" not in captured.out
