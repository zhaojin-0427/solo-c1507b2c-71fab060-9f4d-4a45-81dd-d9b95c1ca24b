"""SQLite connection management and schema bootstrap.

SQLite is opened in the default serialized/thread-safe mode and every
multi-statement write goes through ``write_lock`` together with a
deferred transaction, which makes the idempotency check-then-insert for
exposures race-free even under multi-threaded uvicorn workers.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_key  TEXT NOT NULL REFERENCES experiments(key),
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'published')),
    config_json     TEXT NOT NULL,
    traffic_percentage REAL NOT NULL,
    namespace       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    published_at    TEXT,
    UNIQUE (experiment_key, version)
);

CREATE TABLE IF NOT EXISTS exposures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    experiment_key  TEXT NOT NULL,
    version_id      INTEGER NOT NULL,
    version_number  INTEGER NOT NULL,
    user_key        TEXT NOT NULL,
    bucket          INTEGER,
    gate_bucket     INTEGER,
    variant_key     TEXT,
    enrolled        INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    trace_json      TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_defs (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_key             TEXT NOT NULL REFERENCES experiments(key),
    version_number             INTEGER NOT NULL,
    metric_key                 TEXT NOT NULL,
    metric_type                TEXT NOT NULL CHECK (metric_type IN ('binary', 'continuous')),
    event_name                 TEXT NOT NULL,
    attribution_window_seconds INTEGER NOT NULL,
    direction                  TEXT NOT NULL CHECK (direction IN ('maximize', 'minimize')),
    min_sample_size            INTEGER NOT NULL,
    srm_threshold              REAL NOT NULL,
    created_at                 TEXT NOT NULL,
    UNIQUE (experiment_key, version_number, metric_key)
);

CREATE TABLE IF NOT EXISTS result_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key      TEXT NOT NULL UNIQUE,
    experiment_key TEXT NOT NULL,
    user_key       TEXT NOT NULL,
    event_name     TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    value          REAL,
    value_present  INTEGER NOT NULL,
    value_valid    INTEGER NOT NULL,
    received_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_exp_status
    ON experiment_versions (experiment_key, status);
CREATE INDEX IF NOT EXISTS idx_versions_ns
    ON experiment_versions (namespace, status);
CREATE INDEX IF NOT EXISTS idx_exposures_exp
    ON exposures (experiment_key);
CREATE INDEX IF NOT EXISTS idx_exposures_exp_var
    ON exposures (experiment_key, variant_key);
CREATE INDEX IF NOT EXISTS idx_exposures_user
    ON exposures (user_key);
CREATE INDEX IF NOT EXISTS idx_exposures_exp_ver_user
    ON exposures (experiment_key, version_number, user_key, enrolled);
CREATE INDEX IF NOT EXISTS idx_metrics_exp_version
    ON metric_defs (experiment_key, version_number);
CREATE INDEX IF NOT EXISTS idx_events_lookup
    ON result_events (experiment_key, event_name, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_user
    ON result_events (experiment_key, user_key, event_name);
CREATE INDEX IF NOT EXISTS idx_events_key
    ON result_events (event_key);

-- Published configuration is frozen: no in-place edits, no re-publishing.
CREATE TRIGGER IF NOT EXISTS trg_version_published_immutable
BEFORE UPDATE OF config_json, traffic_percentage, namespace, experiment_key
ON experiment_versions
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published version is immutable: create a new version');
END;

CREATE TRIGGER IF NOT EXISTS trg_version_no_status_republish
BEFORE UPDATE OF status ON experiment_versions
WHEN OLD.status = 'published' AND NEW.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published version cannot be republished');
END;
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """Process-wide connection (FastAPI dependency / app internals)."""
    global _conn
    if _conn is None:
        _conn = _connect(settings.db_path)
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """(Re)initialize the global connection. Used at startup and by tests."""
    global _conn
    if _conn is not None:
        _conn.close()
    if db_path is not None:
        # tests override the path via object.__setattr__ on frozen settings
        object.__setattr__(settings, "db_path", db_path)
    _conn = _connect(settings.db_path)
    _conn.executescript(SCHEMA)
    _conn.commit()
    return _conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Serialized write transaction (BEGIN IMMEDIATE)."""
    conn = get_conn()
    with _write_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
