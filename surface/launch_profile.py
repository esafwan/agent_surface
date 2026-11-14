"""Per-project launch profile persisted in the ``kv_state`` table.

The profile is stored as a JSON document under ``PROFILE_KEY`` using the same
``INSERT OR REPLACE INTO kv_state (key, value_json)`` convention as the
``stage_config`` and ``project_id`` rows written by ``surface/cli.py``.

Loading never validates: a profile that references a since-removed agent
backend must still be readable. Validation runs on save only.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

from surface.agents.registry import BACKENDS

PROFILE_KEY = "launch_profile"

DEFAULTS: dict = {
    "version": 1,
    "agent": None,
    "agent_model": None,
    "agent_timeout": None,
    "stage": None,
    "interactive": False,
    "resume_context": True,
    "schema": None,
    "require_auth": False,
    "entry_path": None,
}


class LaunchProfileError(ValueError):
    pass


def load_profile(conn: sqlite3.Connection) -> Optional[dict]:
    """Return the stored launch profile, or None if no row exists.

    Intentionally does not validate the decoded profile.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT value_json FROM kv_state WHERE key = ?", (PROFILE_KEY,))
    row = cursor.fetchone()
    if not row:
        return None
    return json.loads(row[0])


def save_profile(conn: sqlite3.Connection, profile: dict) -> None:
    """Validate and persist the launch profile. Raises LaunchProfileError."""
    validated = validate_profile(profile)
    conn.execute(
        "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
        (PROFILE_KEY, json.dumps(validated)),
    )
    conn.commit()


def merge_profile(old: Optional[dict], updates: dict) -> dict:
    """Return a new profile: ``old`` (or DEFAULTS) overlaid with ``updates``.

    Keys in ``updates`` whose value is None are skipped, so None means
    "leave this field unchanged" rather than "clear it".
    """
    base = DEFAULTS if old is None else old
    merged = dict(base)
    for key, value in updates.items():
        if value is None:
            continue
        merged[key] = value
    return merged


def _check_relocatable(field: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise LaunchProfileError(f"{field!r} must be a string preset name or relative path, got {type(value).__name__}")
    if os.path.isabs(value):
        raise LaunchProfileError(
            f"{field!r} must not be an absolute path ({value!r}); use a relative path or a preset name so the project stays relocatable"
        )


def validate_profile(p: dict) -> dict:
    """Return a normalized copy of ``p``, or raise LaunchProfileError."""
    if not isinstance(p, dict):
        raise LaunchProfileError(f"launch profile must be a dict, got {type(p).__name__}")

    unknown = sorted(set(p.keys()) - set(DEFAULTS.keys()))
    if unknown:
        allowed = ", ".join(sorted(DEFAULTS.keys()))
        raise LaunchProfileError(f"unknown launch profile key(s): {', '.join(unknown)} (allowed: {allowed})")

    agent = p.get("agent")
    if agent is not None and agent not in BACKENDS:
        known = ", ".join(sorted(BACKENDS.keys())) or "(none registered)"
        raise LaunchProfileError(f"unknown agent backend {agent!r}; known backends: {known}")

    _check_relocatable("stage", p.get("stage"))
    _check_relocatable("schema", p.get("schema"))

    timeout = p.get("agent_timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise LaunchProfileError(f"'agent_timeout' must be a positive int or None, got {timeout!r}")

    require_auth = p.get("require_auth")
    if require_auth is not None and not isinstance(require_auth, bool):
        raise LaunchProfileError(f"'require_auth' must be a bool or None, got {type(require_auth).__name__}")

    entry_path = p.get("entry_path")
    if entry_path is not None:
        if not isinstance(entry_path, str):
            raise LaunchProfileError(f"'entry_path' must be a string starting with '/', got {type(entry_path).__name__}")
        if (not entry_path.startswith("/")) or entry_path.startswith("//") or "://" in entry_path:
            raise LaunchProfileError(
                f"'entry_path' must be a site-relative path starting with a single '/' "
                f"(e.g. '/static/haiku.html'), got {entry_path!r}"
            )

    return dict(p)
