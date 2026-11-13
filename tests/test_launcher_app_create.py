"""Tests for `surface app create` (T4): render_command_script / write_command_script."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from surface.launcher import render_command_script, write_command_script


def test_render_command_script_substitutes_fallback_and_preserves_shell_syntax():
    script = render_command_script("/usr/local/bin/surface")

    assert "/usr/local/bin/surface" in script
    # Literal shell syntax must come through as single '$', not doubled.
    assert '$(dirname "$0")' in script
    assert "${SURFACE_BIN:-}" in script
    assert "$BIN" in script
    # No leftover doubled-dollar artifacts from the template.
    assert "$$" not in script


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
