import sqlite3
from typing import Union, Optional
from pathlib import Path

SCHEMA_VERSION = "1"

INIT_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runtime (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_role TEXT,
    transport TEXT NOT NULL,
    session_ref TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    selected_version_id TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    content_ref TEXT,
    content_type TEXT,
    content TEXT,
    prompt TEXT,
    params_json TEXT,
    created_by TEXT NOT NULL,
    note TEXT,
    source_event_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
    UNIQUE(artifact_id, n)
);

CREATE TABLE IF NOT EXISTS artifact_dependencies (
    upstream_artifact_id TEXT NOT NULL,
    downstream_artifact_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'content',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (upstream_artifact_id, downstream_artifact_id),
    FOREIGN KEY (upstream_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
    FOREIGN KEY (downstream_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    artifact_id TEXT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    claimed_by TEXT,
    claimed_at TIMESTAMP,
    lease_until TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    acked_at TIMESTAMP,
    error TEXT,
    dedupe_key TEXT,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_job_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    request_json TEXT,
    result_json TEXT,
    cost_estimate REAL DEFAULT 0.0,
    cost_actual REAL DEFAULT 0.0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifact_locks (
    artifact_id TEXT PRIMARY KEY,
    owner_worker_id TEXT NOT NULL,
    lease_until TIMESTAMP NOT NULL,
    event_id TEXT,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db(db_path: Union[str, Path, sqlite3.Connection]) -> sqlite3.Connection:
    """Initialize SQLite database with full schema and WAL mode."""
    if isinstance(db_path, sqlite3.Connection):
        conn = db_path
    else:
        # check_same_thread=False: a Store's connection is used from a single
        # logical caller, but that caller isn't always literally one OS
        # thread -- Gradio (via Starlette) dispatches each synchronous board
        # callback to its own worker thread, so a Store created once when the
        # board thread launches would otherwise crash the moment a later
        # callback runs on a different thread ("SQLite objects created in a
        # thread can only be used in that same thread"). Store.__init__
        # pairs this with an internal lock (see store.py) to serialize actual
        # access across threads, since disabling the same-thread check alone
        # does not make concurrent use of one connection safe.
        conn = sqlite3.connect(str(db_path), check_same_thread=False)

    conn.row_factory = sqlite3.Row
    conn.executescript(INIT_SQL)

    # Record schema version if not set
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM runtime WHERE key = 'schema_version'")
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO runtime (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()

    return conn
