"""Tests for `surface app create` (T4): render_command_script / write_command_script."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from surface.launcher import (
    create_desktop_shortcut,
    render_command_script,
    write_command_script,
)


def test_render_command_script_substitutes_fallback_and_preserves_shell_syntax():
    script = render_command_script("/usr/local/bin/surface")

    assert "/usr/local/bin/surface" in script
    # Literal shell syntax must come through as single '$', not doubled.
    assert '$(dirname "$0")' in script
    assert "${SURFACE_BIN:-}" in script
    assert "$BIN" in script
    # No leftover doubled-dollar artifacts from the template.
    assert "$$" not in script
    # The generated launcher defaults to the chromeless app-window experience.
    assert "resume . --app-window" in script


def test_rendered_script_has_valid_shell_syntax(tmp_path):
    script = render_command_script("/usr/local/bin/surface")
    script_path = tmp_path / "rendered.command"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        ["bash", "-n", str(script_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_render_command_script_escapes_double_quote_in_fallback_bin(tmp_path):
    # A fallback_bin containing a double quote must not break the
    # generated script's shell syntax (the template substitutes it into
    # a double-quoted `BIN="$fallback_bin"` assignment).
    malicious = '/usr/local/bin/sur"face'
    script = render_command_script(malicious)
    script_path = tmp_path / "rendered.command"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        ["bash", "-n", str(script_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_write_command_script_creates_executable_file(tmp_path):
    written = write_command_script(tmp_path)

    assert written == tmp_path / "Open Surface Board.command"
    assert written.exists()
    assert written.stat().st_mode & 0o777 == 0o755


def test_write_command_script_raises_without_force(tmp_path):
    write_command_script(tmp_path)

    with pytest.raises(FileExistsError):
        write_command_script(tmp_path)


def test_write_command_script_overwrites_with_force(tmp_path):
    first = write_command_script(tmp_path)
    original_mtime = first.stat().st_mtime_ns

    second = write_command_script(tmp_path, force=True)

    assert second == first
    assert second.exists()
    assert second.stat().st_mode & 0o777 == 0o755


def test_relocated_script_has_no_reference_to_original_project_dir():
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
        src = Path(src_dir).resolve()
        dst = Path(dst_dir).resolve()

        written = write_command_script(src)
        copied = dst / written.name
        shutil.copy2(written, copied)

        content = copied.read_text(encoding="utf-8")
        assert str(src) not in content


def test_create_desktop_shortcut_creates_symlink_resolving_to_target(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    written = write_command_script(project_dir)

    link = create_desktop_shortcut(written)

    assert link == fake_home / "Desktop" / "Open Surface Board.command"
    assert link.is_symlink()
    assert link.resolve() == written.resolve()


def test_create_desktop_shortcut_raises_without_force(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    written = write_command_script(project_dir)

    create_desktop_shortcut(written)

    with pytest.raises(FileExistsError):
        create_desktop_shortcut(written)


def test_create_desktop_shortcut_overwrites_with_force(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    written = write_command_script(project_dir)

    first = create_desktop_shortcut(written)
    second = create_desktop_shortcut(written, force=True)

    assert second == first
    assert second.is_symlink()
    assert second.resolve() == written.resolve()


def test_app_create_without_desktop_flag_only_creates_command_file(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    from surface import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["surface", "app", "create", str(project_dir)])
    cli_module.main()

    assert (project_dir / "Open Surface Board.command").exists()
    assert not (fake_home / "Desktop").exists()


def test_app_create_desktop_flag_cli_creates_both_files(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    from surface import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["surface", "app", "create", str(project_dir), "--desktop"])
    cli_module.main()

    command_file = project_dir / "Open Surface Board.command"
    desktop_link = fake_home / "Desktop" / "Open Surface Board.command"
    assert command_file.exists()
    assert desktop_link.is_symlink()
    assert desktop_link.resolve() == command_file.resolve()


def test_app_create_desktop_partial_failure_reports_clearly(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Pre-create the desktop shortcut so create_desktop_shortcut() raises.
    desktop_dir = fake_home / "Desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "Open Surface Board.command").write_text("# pre-existing\n")

    from surface import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["surface", "app", "create", str(project_dir), "--desktop"])
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()
    assert exc_info.value.code == 1

    out = capsys.readouterr().out
    command_file = project_dir / "Open Surface Board.command"
    # Main launcher must have succeeded despite the desktop-shortcut failure.
    assert command_file.exists()
    assert str(command_file) in out
    assert "Desktop shortcut" in out
    assert "--force" in out
