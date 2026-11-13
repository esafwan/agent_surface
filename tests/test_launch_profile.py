import sqlite3

import pytest

from surface.launch_profile import (
    DEFAULTS,
    LaunchProfileError,
    load_profile,
    merge_profile,
    save_profile,
    validate_profile,
)
from surface.schema import init_db


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def test_load_returns_none_when_absent(conn):
    assert load_profile(conn) is None


def test_round_trip(conn):
    profile = dict(DEFAULTS, agent="fake", agent_timeout=30, stage="demo", interactive=True)
    save_profile(conn, profile)
    assert load_profile(conn) == profile


def test_round_trip_writes_kv_state_row(conn):
    save_profile(conn, dict(DEFAULTS, agent="fake"))
    row = conn.execute("SELECT value_json FROM kv_state WHERE key = ?", ("launch_profile",)).fetchone()
    assert row is not None


def test_merge_none_leaves_fields_unchanged():
    old = dict(DEFAULTS, agent="fake", agent_model="m1", stage="demo")
    merged = merge_profile(old, {"agent": None, "agent_model": "m2", "stage": None})
    assert merged["agent"] == "fake"
    assert merged["stage"] == "demo"
    assert merged["agent_model"] == "m2"
    # Input not mutated
    assert old["agent_model"] == "m1"


def test_merge_with_no_old_starts_from_defaults():
    merged = merge_profile(None, {"interactive": True, "agent": None})
    assert merged == dict(DEFAULTS, interactive=True)


def test_reject_absolute_stage_path():
    with pytest.raises(LaunchProfileError, match="absolute"):
        validate_profile(dict(DEFAULTS, stage="/etc/stages/demo.json"))


def test_reject_absolute_schema_path(conn):
    with pytest.raises(LaunchProfileError, match="absolute"):
        save_profile(conn, dict(DEFAULTS, schema="/abs/schema.json"))
    assert load_profile(conn) is None


def test_reject_unknown_agent():
    with pytest.raises(LaunchProfileError, match="unknown agent backend"):
        validate_profile(dict(DEFAULTS, agent="no-such-agent"))


def test_accept_known_agent_and_none():
    validate_profile(dict(DEFAULTS, agent="fake"))
    validate_profile(dict(DEFAULTS, agent=None))


def test_reject_unknown_top_level_key():
    with pytest.raises(LaunchProfileError, match="unknown launch profile key"):
        validate_profile(dict(DEFAULTS, bogus=1))


@pytest.mark.parametrize("bad", [0, -5, 1.5, "30", True])
def test_reject_non_positive_agent_timeout(bad):
    with pytest.raises(LaunchProfileError, match="agent_timeout"):
        validate_profile(dict(DEFAULTS, agent_timeout=bad))


def test_save_rejects_invalid_and_leaves_no_row(conn):
    with pytest.raises(LaunchProfileError):
        save_profile(conn, dict(DEFAULTS, agent_timeout=0))
    assert load_profile(conn) is None


def test_load_does_not_validate(conn):
    # A profile referencing a removed backend must still load.
    conn.execute(
        "INSERT OR REPLACE INTO kv_state (key, value_json) VALUES (?, ?)",
        ("launch_profile", '{"version": 1, "agent": "removed-backend"}'),
    )
    assert load_profile(conn) == {"version": 1, "agent": "removed-backend"}
