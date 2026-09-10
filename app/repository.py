"""Data-access layer over SQLite."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from .db import get_conn, transaction
from .errors import ConflictError, NotFoundError
from .schemas import VersionConfigIn
from .time_utils import to_iso, utcnow


@dataclass(frozen=True)
class LoadedVersion:
    id: int
    experiment_key: str
    version: int
    status: str
    traffic_percentage: float
    namespace: str
    salt: str
    config: VersionConfigIn
    created_at: str
    published_at: Optional[str]


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _config_dump(config: VersionConfigIn) -> str:
    # mode="json" emits ISO-8601 strings for the schedule datetimes
    return config.model_dump_json()


def _config_load(raw: str) -> VersionConfigIn:
    return VersionConfigIn.model_validate_json(raw)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def create_experiment(key: str, name: str, namespace: str,
                      salt: Optional[str]) -> dict[str, Any]:
    conn = get_conn()
    effective_salt = salt or secrets.token_hex(16)
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO experiments (key, name, namespace, salt, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, name, namespace, effective_salt, to_iso(utcnow())),
            )
            row_id = cur.lastrowid
    except Exception as exc:  # sqlite3.IntegrityError
        if _is_unique(exc):
            raise ConflictError("experiment_exists", f"experiment {key!r} already exists")
        raise
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (row_id,)).fetchone()
    return dict(row)


def get_experiment(key: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM experiments WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"experiment {key!r} not found")
    return dict(row)


def list_experiments() -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM experiments ORDER BY key"
    ).fetchall()
    result = []
    for row in rows:
        exp = dict(row)
        exp["latest_version"] = _scalar(
            "SELECT MAX(version) FROM experiment_versions WHERE experiment_key = ?",
            (exp["key"],))
        exp["published_version"] = _scalar(
            "SELECT MAX(version) FROM experiment_versions "
            "WHERE experiment_key = ? AND status = 'published'",
            (exp["key"],))
        result.append(exp)
    return result


def _scalar(sql: str, params: tuple) -> Any:
    return get_conn().execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def create_version(experiment_key: str, config: VersionConfigIn,
                   status: str) -> LoadedVersion:
    exp = get_experiment(experiment_key)
    conn = get_conn()
    with transaction() as tx:
        next_version = _scalar(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM experiment_versions "
            "WHERE experiment_key = ?", (experiment_key,))
        published_at = to_iso(utcnow()) if status == "published" else None
        cur = tx.execute(
            "INSERT INTO experiment_versions "
            "(experiment_key, version, status, config_json, traffic_percentage, "
            " namespace, created_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (experiment_key, next_version, status, _config_dump(config),
             config.traffic_percentage, exp["namespace"],
             to_iso(utcnow()), published_at),
        )
        version_id = cur.lastrowid
    return get_version_by_id(version_id)


def get_version_by_id(version_id: int) -> LoadedVersion:
    row = get_conn().execute(
        "SELECT v.*, e.salt FROM experiment_versions v "
        "JOIN experiments e ON e.key = v.experiment_key "
        "WHERE v.id = ?", (version_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"version id {version_id} not found")
    return _row_to_version(row)


def get_version(experiment_key: str, version: int) -> LoadedVersion:
    row = get_conn().execute(
        "SELECT v.*, e.salt FROM experiment_versions v "
        "JOIN experiments e ON e.key = v.experiment_key "
        "WHERE v.experiment_key = ? AND v.version = ?",
        (experiment_key, version),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"experiment {experiment_key!r} version {version} not found")
    return _row_to_version(row)


def list_versions(experiment_key: str) -> list[LoadedVersion]:
    get_experiment(experiment_key)  # 404 if missing
    rows = get_conn().execute(
        "SELECT v.*, e.salt FROM experiment_versions v "
        "JOIN experiments e ON e.key = v.experiment_key "
        "WHERE v.experiment_key = ? ORDER BY v.version",
        (experiment_key,),
    ).fetchall()
    return [_row_to_version(r) for r in rows]


def get_published_version(experiment_key: str,
                          version: Optional[int] = None) -> LoadedVersion:
    """Resolve the effective published version (or a pinned one)."""
    if version is not None:
        loaded = get_version(experiment_key, version)
        if loaded.status != "published":
            from .errors import APIError
            raise APIError(409, "version_not_published",
                           f"version {version} of {experiment_key!r} is {loaded.status}")
        return loaded

    experiment = get_experiment(experiment_key)
    row = get_conn().execute(
        "SELECT v.*, e.salt FROM experiment_versions v "
        "JOIN experiments e ON e.key = v.experiment_key "
        "WHERE v.experiment_key = ? AND v.status = 'published' "
        "ORDER BY v.version DESC LIMIT 1",
        (experiment["key"],),
    ).fetchone()
    if row is None:
        from .errors import APIError
        raise APIError(409, "no_published_version",
                       f"experiment {experiment_key!r} has no published version")
    return _row_to_version(row)


def latest_published_versions_in_namespace(namespace: str,
                                           exclude_experiment: str | None = None
                                           ) -> list[dict[str, Any]]:
    """Latest published version of every experiment in a namespace.

    Used for publish-time mutex checks: per experiment only the newest
    published version counts, since older ones have been superseded.
    Pass ``exclude_experiment`` to skip the experiment being published.
    """
    sql = """
        SELECT v.experiment_key, v.version, v.namespace, v.traffic_percentage,
               v.config_json
        FROM experiment_versions v
        JOIN (
            SELECT experiment_key, MAX(version) AS max_version
            FROM experiment_versions
            WHERE status = 'published' AND namespace = ?
            {exclude}
            GROUP BY experiment_key
        ) m ON m.experiment_key = v.experiment_key
           AND m.max_version = v.version
        WHERE v.status = 'published'
    """
    params: list[Any] = [namespace]
    if exclude_experiment is not None:
        sql = sql.format(exclude="AND experiment_key != ?")
        params.append(exclude_experiment)
    else:
        sql = sql.format(exclude="")
    rows = get_conn().execute(sql, params).fetchall()
    return [{
        "experiment_key": r["experiment_key"],
        "version": r["version"],
        "namespace": r["namespace"],
        "traffic_percentage": r["traffic_percentage"],
        "config": json.loads(r["config_json"]),
    } for r in rows]


def load_latest_published_in_namespace(namespace: str
                                       ) -> list[LoadedVersion]:
    """Loaded latest published versions for every experiment in a namespace.

    Decision-time mutex ring: includes each experiment's salt so the ring
    gate bucket is scoped to the namespace.
    """
    rows = get_conn().execute(
        """
        SELECT v.*, e.salt FROM experiment_versions v
        JOIN experiments e ON e.key = v.experiment_key
        JOIN (
            SELECT experiment_key, MAX(version) AS max_version
            FROM experiment_versions
            WHERE status = 'published' AND namespace = ?
            GROUP BY experiment_key
        ) m ON m.experiment_key = v.experiment_key
           AND m.max_version = v.version
        WHERE v.status = 'published'
        ORDER BY v.experiment_key
        """,
        (namespace,),
    ).fetchall()
    return [_row_to_version(r) for r in rows]


def create_experiment_with_version(key: str, name: str, namespace: str,
                                   salt: Optional[str],
                                   config: VersionConfigIn,
                                   status: str) -> tuple[dict[str, Any], LoadedVersion]:
    """Atomically insert experiment + first version.

    If the version insert fails (e.g. a DB-level constraint) the whole
    transaction rolls back, so a rejected create never leaves orphan
    experiment metadata behind and the same key can be retried.
    """
    effective_salt = salt or secrets.token_hex(16)
    with transaction() as tx:
        try:
            cur = tx.execute(
                "INSERT INTO experiments (key, name, namespace, salt, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, name, namespace, effective_salt, to_iso(utcnow())),
            )
        except Exception as exc:
            if _is_unique(exc):
                raise ConflictError("experiment_exists",
                                    f"experiment {key!r} already exists")
            raise
        exp_id = cur.lastrowid
        next_version = 1
        published_at = to_iso(utcnow()) if status == "published" else None
        tx.execute(
            "INSERT INTO experiment_versions "
            "(experiment_key, version, status, config_json, traffic_percentage, "
            " namespace, created_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, next_version, status, _config_dump(config),
             config.traffic_percentage, namespace,
             to_iso(utcnow()), published_at),
        )
    exp = dict(get_conn().execute(
        "SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone())
    return exp, get_version(key, next_version)


def publish_version(experiment_key: str, version: int) -> LoadedVersion:
    get_experiment(experiment_key)
    with transaction() as tx:
        row = tx.execute(
            "SELECT * FROM experiment_versions "
            "WHERE experiment_key = ? AND version = ?",
            (experiment_key, version),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"experiment {experiment_key!r} version {version} not found")
        if row["status"] == "published":
            raise ConflictError("version_already_published",
                                f"version {version} is already published and immutable")
        tx.execute(
            "UPDATE experiment_versions SET status = 'published', published_at = ? "
            "WHERE id = ? AND status = 'draft'",
            (to_iso(utcnow()), row["id"]),
        )
    return get_version(experiment_key, version)


def _row_to_version(row: Any) -> LoadedVersion:
    return LoadedVersion(
        id=row["id"],
        experiment_key=row["experiment_key"],
        version=row["version"],
        status=row["status"],
        traffic_percentage=row["traffic_percentage"],
        namespace=row["namespace"],
        salt=row["salt"],
        config=_config_load(row["config_json"]),
        created_at=row["created_at"],
        published_at=row["published_at"],
    )


# ---------------------------------------------------------------------------
# Exposures
# ---------------------------------------------------------------------------


def insert_exposure(idempotency_key: str, experiment_key: str,
                    version_id: int, version_number: int, user_key: str,
                    bucket: Optional[int], gate_bucket: Optional[int],
                    variant_key: Optional[str],
                    enrolled: bool, reason: str, trace_json: str) -> tuple[bool, dict[str, Any]]:
    """Idempotent insert.

    Returns (recorded_now, row_dict). On a duplicate idempotency key the
    stored decision is returned unchanged — it can be used to trace which
    config version produced the original verdict.
    """
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO exposures (idempotency_key, experiment_key, version_id, "
                " version_number, user_key, bucket, gate_bucket, variant_key, enrolled, "
                " reason, trace_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (idempotency_key, experiment_key, version_id, version_number,
                 user_key, bucket, gate_bucket, variant_key,
                 1 if enrolled else 0, reason, trace_json, to_iso(utcnow())),
            )
            inserted_id = cur.lastrowid
    except Exception as exc:
        if not _is_unique(exc):
            raise
        existing = get_exposure_by_key(idempotency_key)
        return False, existing
    return True, get_exposure_by_id(inserted_id)


def get_exposure_by_key(idempotency_key: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM exposures WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"exposure {idempotency_key!r} not found")
    return _exposure_row(row)


def get_exposure_by_id(exposure_id: int) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM exposures WHERE id = ?", (exposure_id,)
    ).fetchone()
    return _exposure_row(row)


def list_exposures(experiment_key: str, variant: Optional[str] = None,
                   user_key: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = ("SELECT * FROM exposures WHERE experiment_key = ?")
    params: list[Any] = [experiment_key]
    if variant is not None:
        sql += " AND variant_key = ?"
        params.append(variant)
    if user_key is not None:
        sql += " AND user_key = ?"
        params.append(user_key)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_exposure_row(r) for r in get_conn().execute(sql, params).fetchall()]


def exposure_summary(experiment_key: str, version: Optional[int] = None) -> dict[str, Any]:
    where = "experiment_key = ?"
    params: list[Any] = [experiment_key]
    if version is not None:
        where += " AND version_number = ?"
        params.append(version)

    rows = get_conn().execute(
        f"SELECT variant_key, COUNT(*) AS c FROM exposures WHERE {where} "
        f"GROUP BY variant_key ORDER BY variant_key",
        params,
    ).fetchall()
    by_variant = [{"variant_key": r["variant_key"], "count": r["c"]} for r in rows]

    reason_rows = get_conn().execute(
        f"SELECT reason, COUNT(*) AS c FROM exposures WHERE {where} GROUP BY reason",
        params,
    ).fetchall()
    by_reason = {r["reason"]: r["c"] for r in reason_rows}

    total_r = get_conn().execute(
        f"SELECT COUNT(*), COALESCE(SUM(enrolled), 0) FROM exposures WHERE {where}",
        params,
    ).fetchone()
    total, enrolled = total_r[0], total_r[1]
    return {
        "experiment_key": experiment_key,
        "version": version,
        "total_decisions": total,
        "enrolled": enrolled,
        "not_enrolled": total - enrolled,
        "by_variant": by_variant,
        "by_reason": by_reason,
    }


def _exposure_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["enrolled"] = bool(d["enrolled"])
    d["trace"] = json.loads(d.pop("trace_json"))
    return d


def _is_unique(exc: Exception) -> bool:
    import sqlite3
    return isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc)
