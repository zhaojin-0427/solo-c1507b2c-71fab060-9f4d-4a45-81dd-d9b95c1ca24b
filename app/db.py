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
    continuity_json TEXT,
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

-- ---------------------------------------------------------------------------
-- Sequential (group-sequential) analysis plans and checkpoints.
--
-- A plan is written BEFORE any exposure on the analyzed version and is
-- immutable afterwards: there is no UPDATE endpoint at all, and the triggers
-- below block in-place changes at the storage layer. Checkpoints are frozen
-- snapshots: their result_json is fixed at submit time and later events can
-- never alter it (the same cutoff is re-served from storage verbatim).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sequential_plans (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_key                   TEXT NOT NULL UNIQUE,
    experiment_key             TEXT NOT NULL REFERENCES experiments(key),
    version_number             INTEGER NOT NULL,
    metric_key                 TEXT NOT NULL,
    control_variant_key        TEXT NOT NULL,
    target_variant_key         TEXT NOT NULL,
    hypothesis                 TEXT NOT NULL CHECK (hypothesis IN ('one_sided', 'two_sided')),
    direction                  TEXT NOT NULL CHECK (direction IN ('maximize', 'minimize')),
    alpha                      REAL NOT NULL,
    max_sample_size            INTEGER NOT NULL,
    planned_checks             INTEGER NOT NULL,
    conditional_power_threshold REAL NOT NULL,
    boundary_type              TEXT NOT NULL CHECK (boundary_type IN ('pocock', 'obrien_fleming')),
    design_assumptions_json    TEXT,
    control_share              REAL NOT NULL,
    target_share               REAL NOT NULL,
    computed_json              TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    UNIQUE (experiment_key, version_number, metric_key)
);

CREATE TABLE IF NOT EXISTS sequential_checkpoints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_key         TEXT NOT NULL REFERENCES sequential_plans(plan_key),
    sequence         INTEGER NOT NULL,
    cutoff_at        TEXT NOT NULL,
    information_time REAL NOT NULL,
    recommendation   TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (plan_key, cutoff_at),
    UNIQUE (plan_key, sequence)
);

CREATE INDEX IF NOT EXISTS idx_seq_plans_exp
    ON sequential_plans (experiment_key, version_number);
CREATE INDEX IF NOT EXISTS idx_seq_cp_plan
    ON sequential_checkpoints (plan_key, sequence);

-- Plans and checkpoints are write-once: no UPDATE is ever needed (a plan's
-- active/terminal state is derived from its latest checkpoint), so the
-- storage layer refuses every modification or deletion outright.
CREATE TRIGGER IF NOT EXISTS trg_seq_plan_no_update
BEFORE UPDATE ON sequential_plans
BEGIN
    SELECT RAISE(ABORT, 'sequential plans are immutable: create a new plan');
END;

CREATE TRIGGER IF NOT EXISTS trg_seq_plan_no_delete
BEFORE DELETE ON sequential_plans
BEGIN
    SELECT RAISE(ABORT, 'sequential plans are immutable and cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_seq_checkpoint_no_update
BEFORE UPDATE ON sequential_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'checkpoint snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_seq_checkpoint_no_delete
BEFORE DELETE ON sequential_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'checkpoint snapshots are immutable and cannot be deleted');
END;

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
-- The frozen continuity block (assignment seed, variant order, renames) is
-- part of that immutable configuration and is covered by the same trigger.
CREATE TRIGGER IF NOT EXISTS trg_version_published_immutable
BEFORE UPDATE OF config_json, traffic_percentage, namespace, experiment_key, continuity_json
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

# Column/trigger definitions added after the first release. An existing
# SQLite file predates them: CREATE TABLE IF NOT EXISTS never alters a table
# and CREATE TRIGGER IF NOT EXISTS never replaces a trigger body, so both are
# reconciled here, idempotently, on every bootstrap.
_MIGRATIONS = [
    (
        "SELECT 1 FROM pragma_table_info('experiment_versions') "
        "WHERE name = 'continuity_json'",
        "ALTER TABLE experiment_versions ADD COLUMN continuity_json TEXT",
    ),
]

# The immutability trigger gained continuity_json in its UPDATE OF list; an
# old database keeps the old trigger body until it is explicitly replaced.
_TRIGGER_RECREATE = """
DROP TRIGGER IF EXISTS trg_version_published_immutable;
CREATE TRIGGER trg_version_published_immutable
BEFORE UPDATE OF config_json, traffic_percentage, namespace, experiment_key, continuity_json
ON experiment_versions
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published version is immutable: create a new version');
END;
"""


def _run_migrations(conn: sqlite3.Connection) -> None:
    for probe, ddl in _MIGRATIONS:
        if conn.execute(probe).fetchone() is None:
            conn.execute(ddl)
    conn.executescript(_TRIGGER_RECREATE)
    conn.commit()


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
        _run_migrations(_conn)
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
    _run_migrations(_conn)
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
