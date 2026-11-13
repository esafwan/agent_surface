"""Tests for backend binary attributes and availability helpers in the registry."""

import shutil

import pytest

from surface.agents.base import which_binary
from surface.agents.registry import BACKENDS, is_available, resolve_binary


def _fake_which(found: dict[str, str]):
    """Build a shutil.which replacement that only resolves names in `found`."""

    def _which(name, *args, **kwargs):
        return found.get(name)

    return _which


def test_binary_attributes_match_cli_names():
    assert BACKENDS["codex"].binary == "codex"
    assert BACKENDS["opencode"].binary == "opencode"
    assert BACKENDS["agy"].binary == "agy"
    assert BACKENDS["fake"].binary == ""


def test_which_binary_wraps_shutil_which(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({"codex": "/usr/local/bin/codex"}))
    assert which_binary("codex") == "/usr/local/bin/codex"
    assert which_binary("missing") is None


def test_is_available_fake_always_true(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    assert is_available("fake") is True


@pytest.mark.parametrize("name", ["codex", "opencode", "agy"])
def test_is_available_real_backend_found(monkeypatch, name):
    binary = BACKENDS[name].binary
    monkeypatch.setattr(shutil, "which", _fake_which({binary: f"/opt/bin/{binary}"}))
    assert is_available(name) is True


@pytest.mark.parametrize("name", ["codex", "opencode", "agy"])
def test_is_available_real_backend_not_found(monkeypatch, name):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    assert is_available(name) is False


def test_is_available_unknown_backend_raises_keyerror():
    with pytest.raises(KeyError) as exc_info:
        is_available("not_a_real_backend")
    msg = str(exc_info.value)
    assert "not_a_real_backend" in msg
    assert "known backends" in msg
    assert "codex" in msg


def test_resolve_binary_fake_returns_empty_string(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    assert resolve_binary("fake") == ""


@pytest.mark.parametrize("name", ["codex", "opencode", "agy"])
def test_resolve_binary_real_backend_found(monkeypatch, name):
    binary = BACKENDS[name].binary
    path = f"/opt/bin/{binary}"
    monkeypatch.setattr(shutil, "which", _fake_which({binary: path}))
    assert resolve_binary(name) == path


@pytest.mark.parametrize("name", ["codex", "opencode", "agy"])
def test_resolve_binary_real_backend_not_found(monkeypatch, name):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    assert resolve_binary(name) is None


def test_resolve_binary_unknown_backend_raises_keyerror():
    with pytest.raises(KeyError):
        resolve_binary("not_a_real_backend")
