"""Data-access layer over SQLite."""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from .db import get_conn, transaction
from .errors import ConflictError, NotFoundError
from .schemas import VersionConfigIn
from .time_utils import parse_iso, to_iso, utcnow


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
    continuity: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _config_dump(config: VersionConfigIn) -> str:
    # mode="json" emits ISO-8601 strings for the schedule datetimes. The
    # continuity declaration is input-only: the resolved/frozen block lives
    # in its own continuity_json column, so stored config_json never carries
    # a declaration that could drift from the frozen block.
    data = config.model_dump(mode="json")
    data.pop("continuity", None)
    return json.dumps(data, ensure_ascii=False)


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
                   status: str,
                   continuity: Optional[dict[str, Any]] = None) -> LoadedVersion:
    exp = get_experiment(experiment_key)
    conn = get_conn()
    continuity_json = json.dumps(continuity, ensure_ascii=False) if continuity else None
    with transaction() as tx:
        next_version = _scalar(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM experiment_versions "
            "WHERE experiment_key = ?", (experiment_key,))
        published_at = to_iso(utcnow()) if status == "published" else None
        cur = tx.execute(
            "INSERT INTO experiment_versions "
            "(experiment_key, version, status, config_json, traffic_percentage, "
            " namespace, continuity_json, created_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (experiment_key, next_version, status, _config_dump(config),
             config.traffic_percentage, exp["namespace"], continuity_json,
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
                                   status: str,
                                   continuity: Optional[dict[str, Any]] = None
                                   ) -> tuple[dict[str, Any], LoadedVersion]:
    """Atomically insert experiment + first version.

    If the version insert fails (e.g. a DB-level constraint) the whole
    transaction rolls back, so a rejected create never leaves orphan
    experiment metadata behind and the same key can be retried.
    """
    effective_salt = salt or secrets.token_hex(16)
    continuity_json = json.dumps(continuity, ensure_ascii=False) if continuity else None
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
            " namespace, continuity_json, created_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, next_version, status, _config_dump(config),
             config.traffic_percentage, namespace, continuity_json,
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
    raw_continuity = row["continuity_json"] if "continuity_json" in row.keys() else None
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
        continuity=json.loads(raw_continuity) if raw_continuity else None,
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


# ---------------------------------------------------------------------------
# Metric definitions (bound to one experiment version, immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDef:
    id: int
    experiment_key: str
    version_number: int
    metric_key: str
    metric_type: str
    event_name: str
    attribution_window_seconds: int
    direction: str
    min_sample_size: int
    srm_threshold: float
    created_at: str


def create_metric(experiment_key: str, version_number: int,
                  spec: Any) -> MetricDef:
    get_experiment(experiment_key)  # 404 if experiment missing
    version = get_version(experiment_key, version_number)  # 404 if version missing
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO metric_defs (experiment_key, version_number, "
                " metric_key, metric_type, event_name, attribution_window_seconds, "
                " direction, min_sample_size, srm_threshold, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (experiment_key, version_number, spec.metric_key,
                 spec.metric_type, spec.event_name,
                 spec.attribution_window_seconds, spec.direction,
                 spec.min_sample_size, spec.srm_threshold,
                 to_iso(utcnow())),
            )
            metric_id = cur.lastrowid
    except Exception as exc:
        if _is_unique(exc):
            raise ConflictError(
                "metric_exists",
                f"metric {spec.metric_key!r} already defined for "
                f"{experiment_key!r} v{version_number}")
        raise
    return get_metric_by_id(metric_id)


def get_metric_by_id(metric_id: int) -> MetricDef:
    row = get_conn().execute(
        "SELECT * FROM metric_defs WHERE id = ?", (metric_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"metric id {metric_id} not found")
    return _row_to_metric(row)


def get_metric(experiment_key: str, version_number: int,
               metric_key: str) -> MetricDef:
    row = get_conn().execute(
        "SELECT * FROM metric_defs WHERE experiment_key = ? "
        "AND version_number = ? AND metric_key = ?",
        (experiment_key, version_number, metric_key),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"metric {metric_key!r} not found for {experiment_key!r} "
            f"v{version_number}")
    return _row_to_metric(row)


def list_metrics(experiment_key: str,
                 version_number: Optional[int] = None) -> list[MetricDef]:
    get_experiment(experiment_key)  # 404 early
    sql = "SELECT * FROM metric_defs WHERE experiment_key = ?"
    params: list[Any] = [experiment_key]
    if version_number is not None:
        sql += " AND version_number = ?"
        params.append(version_number)
    sql += " ORDER BY version_number, metric_key"
    return [_row_to_metric(r) for r in get_conn().execute(sql, params).fetchall()]


def list_matching_metrics(experiment_key: str,
                          event_name: str) -> list[MetricDef]:
    """All defined metrics (any version) listening on this event.

    Used for the ingestion-time attribution preview.
    """
    rows = get_conn().execute(
        "SELECT * FROM metric_defs WHERE experiment_key = ? AND event_name = ? "
        "ORDER BY version_number, metric_key",
        (experiment_key, event_name),
    ).fetchall()
    return [_row_to_metric(r) for r in rows]


def _row_to_metric(row: Any) -> MetricDef:
    return MetricDef(
        id=row["id"],
        experiment_key=row["experiment_key"],
        version_number=row["version_number"],
        metric_key=row["metric_key"],
        metric_type=row["metric_type"],
        event_name=row["event_name"],
        attribution_window_seconds=row["attribution_window_seconds"],
        direction=row["direction"],
        min_sample_size=row["min_sample_size"],
        srm_threshold=row["srm_threshold"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Result events
# ---------------------------------------------------------------------------


def insert_result_event(event_key: str, experiment_key: str, user_key: str,
                        event_name: str, occurred_at_iso: str,
                        value: Optional[float]) -> tuple[bool, dict[str, Any]]:
    """Idempotent event insert keyed by the globally unique event_key.

    Returns (inserted_now, row_dict). On a duplicate the originally stored
    event is returned unchanged (re-reporting never re-counts).
    """
    value_present = value is not None
    value_valid = value_present and math.isfinite(value)
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO result_events (event_key, experiment_key, user_key, "
                " event_name, occurred_at, value, value_present, value_valid, "
                " received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_key, experiment_key, user_key, event_name,
                 occurred_at_iso, value, 1 if value_present else 0,
                 1 if value_valid else 0, to_iso(utcnow())),
            )
            inserted_id = cur.lastrowid
    except Exception as exc:
        if not _is_unique(exc):
            raise
        return False, get_result_event_by_key(event_key)
    return True, get_result_event_by_id(inserted_id)


def get_result_event_by_key(event_key: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM result_events WHERE event_key = ?", (event_key,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"event {event_key!r} not found")
    return _event_row(row)


def get_result_event_for_experiment(experiment_key: str,
                                    event_key: str) -> dict[str, Any]:
    """Fetch an event through its owning experiment path only.

    An event key under a mismatching experiment path must look like 404,
    never leak another experiment's data.
    """
    row = get_conn().execute(
        "SELECT * FROM result_events WHERE event_key = ? AND experiment_key = ?",
        (event_key, experiment_key),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"event {event_key!r} not found for experiment {experiment_key!r}")
    return _event_row(row)


def get_result_event_by_id(event_id: int) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM result_events WHERE id = ?", (event_id,)).fetchone()
    return _event_row(row)


def list_result_events(experiment_key: str,
                       event_name: Optional[str] = None,
                       user_key: Optional[str] = None,
                       limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM result_events WHERE experiment_key = ?"
    params: list[Any] = [experiment_key]
    if event_name is not None:
        sql += " AND event_name = ?"
        params.append(event_name)
    if user_key is not None:
        sql += " AND user_key = ?"
        params.append(user_key)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_event_row(r) for r in get_conn().execute(sql, params).fetchall()]


def count_result_events(experiment_key: str, event_name: str) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) FROM result_events "
        "WHERE experiment_key = ? AND event_name = ?",
        (experiment_key, event_name),
    ).fetchone()
    return int(row[0])


def enrolled_exposures(experiment_key: str,
                       version_number: Optional[int] = None
                       ) -> list[dict[str, Any]]:
    """Enrolled exposures, optionally restricted to one version.

    With no version filter this returns enrolled exposures across EVERY
    version — the cross-version input that decides which version owns an
    event.
    """
    sql = ("SELECT id, experiment_key, version_number, user_key, variant_key, "
           " enrolled, recorded_at FROM exposures "
           "WHERE experiment_key = ? AND enrolled = 1")
    params: list[Any] = [experiment_key]
    if version_number is not None:
        sql += " AND version_number = ?"
        params.append(version_number)
    sql += " ORDER BY id"
    rows = get_conn().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def all_version_exposures(experiment_key: str,
                          version_number: int) -> list[dict[str, Any]]:
    """Every exposure row (enrolled and gate-miss) of one version."""
    rows = get_conn().execute(
        "SELECT id, experiment_key, version_number, user_key, variant_key, "
        "enrolled, recorded_at FROM exposures "
        "WHERE experiment_key = ? AND version_number = ? ORDER BY id",
        (experiment_key, version_number),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["enrolled"] = bool(d["enrolled"])
        result.append(d)
    return result


def published_versions(experiment_key: str) -> list[tuple[str, int]]:
    """``(published_at ISO, version)`` sorted ascending."""
    rows = get_conn().execute(
        "SELECT version, published_at FROM experiment_versions "
        "WHERE experiment_key = ? AND status = 'published' "
        "AND published_at IS NOT NULL ORDER BY published_at, version",
        (experiment_key,),
    ).fetchall()
    return [(r["published_at"], r["version"]) for r in rows]


def events_for_analysis(experiment_key: str, event_name: str,
                        start_iso: Optional[str],
                        end_iso: Optional[str]) -> list[dict[str, Any]]:
    """All matching events in the time range, NOT scoped to a version.

    Version ownership is decided in the attribution logic (the user's
    nearest prior enrolled exposure across versions), not in SQL, so an
    event cannot be counted on two versions simultaneously. Events are
    deduplicated by the event_key UNIQUE constraint and ordered by
    (occurred_at, id) so ties resolve deterministically.
    """
    sql = ("SELECT id, event_key, user_key, event_name, occurred_at, value, "
           " value_present, value_valid FROM result_events "
           "WHERE experiment_key = ? AND event_name = ?")
    params: list[Any] = [experiment_key, event_name]
    if start_iso is not None:
        sql += " AND occurred_at >= ?"
        params.append(start_iso)
    if end_iso is not None:
        sql += " AND occurred_at < ?"  # half-open [start, end)
        params.append(end_iso)
    sql += " ORDER BY occurred_at, id"
    rows = get_conn().execute(sql, params).fetchall()
    return [_event_row(r) for r in rows]


def total_events_including_duplicates(experiment_key: str, event_name: str,
                                      start_iso: Optional[str],
                                      end_iso: Optional[str]) -> int:
    """Raw ingest attempts in the window (table only holds one row per event_key).

    Duplicate reports are rejected at insert time, so this equals distinct
    events; the field is kept explicit for audit reconciliation.
    """
    sql = ("SELECT COUNT(*) FROM result_events "
           "WHERE experiment_key = ? AND event_name = ?")
    params: list[Any] = [experiment_key, event_name]
    if start_iso is not None:
        sql += " AND occurred_at >= ?"
        params.append(start_iso)
    if end_iso is not None:
        sql += " AND occurred_at < ?"
        params.append(end_iso)
    return int(get_conn().execute(sql, params).fetchone()[0])


def _event_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["value_present"] = bool(d["value_present"])
    d["value_valid"] = bool(d["value_valid"])
    d["occurred_at_dt"] = parse_iso(d["occurred_at"])
    return d


def _is_unique(exc: Exception) -> bool:
    import sqlite3
    return isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc)


# ---------------------------------------------------------------------------
# Sequential analysis plans + checkpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SequentialPlan:
    id: int
    plan_key: str
    experiment_key: str
    version_number: int
    metric_key: str
    control_variant_key: str
    target_variant_key: str
    hypothesis: str
    direction: str
    alpha: float
    max_sample_size: int
    planned_checks: int
    conditional_power_threshold: float
    boundary_type: str
    design_assumptions: Optional[dict[str, Any]]
    control_share: float
    target_share: float
    computed: dict[str, Any]
    created_at: str


def _row_to_seq_plan(row: Any) -> SequentialPlan:
    return SequentialPlan(
        id=row["id"], plan_key=row["plan_key"],
        experiment_key=row["experiment_key"],
        version_number=row["version_number"],
        metric_key=row["metric_key"],
        control_variant_key=row["control_variant_key"],
        target_variant_key=row["target_variant_key"],
        hypothesis=row["hypothesis"], direction=row["direction"],
        alpha=row["alpha"], max_sample_size=row["max_sample_size"],
        planned_checks=row["planned_checks"],
        conditional_power_threshold=row["conditional_power_threshold"],
        boundary_type=row["boundary_type"],
        design_assumptions=(json.loads(row["design_assumptions_json"])
                            if row["design_assumptions_json"] else None),
        control_share=row["control_share"],
        target_share=row["target_share"],
        computed=json.loads(row["computed_json"]),
        created_at=row["created_at"])


def create_sequential_plan(spec: Any) -> SequentialPlan:
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO sequential_plans (plan_key, experiment_key, "
                " version_number, metric_key, control_variant_key, "
                " target_variant_key, hypothesis, direction, alpha, "
                " max_sample_size, planned_checks, conditional_power_threshold, "
                " boundary_type, design_assumptions_json, control_share, "
                " target_share, computed_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (spec["plan_key"], spec["experiment_key"],
                 spec["version_number"], spec["metric_key"],
                 spec["control_variant_key"], spec["target_variant_key"],
                 spec["hypothesis"], spec["direction"], spec["alpha"],
                 spec["max_sample_size"], spec["planned_checks"],
                 spec["conditional_power_threshold"], spec["boundary_type"],
                 (json.dumps(spec["design_assumptions"])
                  if spec["design_assumptions"] is not None else None),
                 spec["control_share"], spec["target_share"],
                 json.dumps(spec["computed"]), to_iso(utcnow())))
            plan_id = cur.lastrowid
    except Exception as exc:
        if _is_unique(exc):
            msg = str(exc)
            if "plan_key" in msg:
                raise ConflictError(
                    "sequential_plan_exists",
                    f"sequential plan {spec['plan_key']!r} already exists")
            raise ConflictError(
                "sequential_plan_exists_for_metric",
                f"a sequential plan already exists for "
                f"{spec['experiment_key']!r} v{spec['version_number']} "
                f"metric {spec['metric_key']!r}")
        raise
    return get_sequential_plan_by_id(plan_id)


def get_sequential_plan_by_id(plan_id: int) -> SequentialPlan:
    row = get_conn().execute(
        "SELECT * FROM sequential_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"sequential plan id {plan_id} not found")
    return _row_to_seq_plan(row)


def get_sequential_plan(plan_key: str) -> SequentialPlan:
    row = get_conn().execute(
        "SELECT * FROM sequential_plans WHERE plan_key = ?", (plan_key,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"sequential plan {plan_key!r} not found")
    return _row_to_seq_plan(row)


def get_sequential_plan_for_metric(experiment_key: str, version_number: int,
                                   metric_key: str
                                   ) -> Optional[SequentialPlan]:
    row = get_conn().execute(
        "SELECT * FROM sequential_plans WHERE experiment_key = ? "
        "AND version_number = ? AND metric_key = ?",
        (experiment_key, version_number, metric_key)).fetchone()
    return _row_to_seq_plan(row) if row else None


def list_sequential_plans(experiment_key: str) -> list[SequentialPlan]:
    get_experiment(experiment_key)  # 404 early
    rows = get_conn().execute(
        "SELECT * FROM sequential_plans WHERE experiment_key = ? "
        "ORDER BY created_at, plan_key", (experiment_key,)).fetchall()
    return [_row_to_seq_plan(r) for r in rows]


def count_exposures_before(experiment_key: str, version_number: int,
                           cutoff_iso: str) -> int:
    """Enrolled exposure rows of one version recorded strictly before cutoff."""
    row = get_conn().execute(
        "SELECT COUNT(*) FROM exposures WHERE experiment_key = ? "
        "AND version_number = ? AND enrolled = 1 AND recorded_at < ?",
        (experiment_key, version_number, cutoff_iso)).fetchone()
    return int(row[0])


def insert_checkpoint(plan_key: str, sequence: int, cutoff_iso: str,
                      information_time: float, recommendation: str,
                      result: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Insert a checkpoint snapshot; duplicate cutoff re-serves the stored row.

    Returns (inserted_now, stored_row_dict) — on a duplicate cutoff the first
    snapshot is returned unchanged, regardless of any later data.
    """
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO sequential_checkpoints (plan_key, sequence, "
                " cutoff_at, information_time, recommendation, result_json, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (plan_key, sequence, cutoff_iso, information_time,
                 recommendation, json.dumps(result), to_iso(utcnow())))
            cp_id = cur.lastrowid
    except Exception as exc:
        if not _is_unique(exc):
            raise
        return False, get_checkpoint_at(plan_key, cutoff_iso)
    return True, get_checkpoint_by_id(cp_id)


def get_checkpoint_by_id(cp_id: int) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM sequential_checkpoints WHERE id = ?", (cp_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"checkpoint id {cp_id} not found")
    return _checkpoint_row(row)


def get_checkpoint_at(plan_key: str, cutoff_iso: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM sequential_checkpoints WHERE plan_key = ? "
        "AND cutoff_at = ?", (plan_key, cutoff_iso)).fetchone()
    if row is None:
        raise NotFoundError(
            f"checkpoint at {cutoff_iso} for plan {plan_key!r} not found")
    return _checkpoint_row(row)


def list_checkpoints(plan_key: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM sequential_checkpoints WHERE plan_key = ? "
        "ORDER BY sequence", (plan_key,)).fetchall()
    return [_checkpoint_row(r) for r in rows]


def _checkpoint_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json"))
    return d


def enrolled_exposures_before(experiment_key: str, version_number: int,
                              cutoff_iso: str) -> list[dict[str, Any]]:
    sql = ("SELECT id, experiment_key, version_number, user_key, variant_key, "
           " enrolled, recorded_at FROM exposures "
           "WHERE experiment_key = ? AND enrolled = 1 AND recorded_at < ?")
    params: list[Any] = [experiment_key, cutoff_iso]
    if version_number is not None:
        sql += " AND version_number = ?"
        params.append(version_number)
    sql += " ORDER BY id"
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def version_exposures_before(experiment_key: str, version_number: int,
                             cutoff_iso: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT id, experiment_key, version_number, user_key, variant_key, "
        "enrolled, recorded_at FROM exposures "
        "WHERE experiment_key = ? AND version_number = ? "
        "AND recorded_at < ? ORDER BY id",
        (experiment_key, version_number, cutoff_iso)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["enrolled"] = bool(d["enrolled"])
        result.append(d)
    return result


def events_before(experiment_key: str, event_name: str,
                  cutoff_iso: str) -> list[dict[str, Any]]:
    """Deduplicated events with occurred_at strictly before cutoff."""
    rows = get_conn().execute(
        "SELECT id, event_key, user_key, event_name, occurred_at, value, "
        " value_present, value_valid FROM result_events "
        "WHERE experiment_key = ? AND event_name = ? AND occurred_at < ? "
        "ORDER BY occurred_at, id",
        (experiment_key, event_name, cutoff_iso)).fetchall()
    return [_event_row(r) for r in rows]


# ---------------------------------------------------------------------------
# CUPED covariate-adjustment plans + frozen snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CupedPlan:
    id: int
    plan_key: str
    experiment_key: str
    version_number: int
    metric_key: str
    covariate_event_name: str
    preexposure_window_seconds: int
    target_aggregation: str
    covariate_aggregation: str
    missing_covariate_policy: str
    created_at: str


def _row_to_cuped_plan(row: Any) -> CupedPlan:
    return CupedPlan(
        id=row["id"], plan_key=row["plan_key"],
        experiment_key=row["experiment_key"],
        version_number=row["version_number"],
        metric_key=row["metric_key"],
        covariate_event_name=row["covariate_event_name"],
        preexposure_window_seconds=row["preexposure_window_seconds"],
        target_aggregation=row["target_aggregation"],
        covariate_aggregation=row["covariate_aggregation"],
        missing_covariate_policy=row["missing_covariate_policy"],
        created_at=row["created_at"])


def create_cuped_plan(spec: dict[str, Any]) -> CupedPlan:
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO cuped_plans (plan_key, experiment_key, "
                " version_number, metric_key, covariate_event_name, "
                " preexposure_window_seconds, target_aggregation, "
                " covariate_aggregation, missing_covariate_policy, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (spec["plan_key"], spec["experiment_key"],
                 spec["version_number"], spec["metric_key"],
                 spec["covariate_event_name"],
                 spec["preexposure_window_seconds"],
                 spec["target_aggregation"], spec["covariate_aggregation"],
                 spec["missing_covariate_policy"], to_iso(utcnow())))
            plan_id = cur.lastrowid
    except Exception as exc:
        if _is_unique(exc):
            msg = str(exc)
            if "plan_key" in msg:
                raise ConflictError(
                    "cuped_plan_exists",
                    f"cuped plan {spec['plan_key']!r} already exists")
            raise ConflictError(
                "cuped_plan_exists_for_metric",
                f"a cuped plan already exists for "
                f"{spec['experiment_key']!r} v{spec['version_number']} "
                f"metric {spec['metric_key']!r}")
        raise
    return get_cuped_plan_by_id(plan_id)


def get_cuped_plan_by_id(plan_id: int) -> CupedPlan:
    row = get_conn().execute(
        "SELECT * FROM cuped_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"cuped plan id {plan_id} not found")
    return _row_to_cuped_plan(row)


def get_cuped_plan(plan_key: str) -> CupedPlan:
    row = get_conn().execute(
        "SELECT * FROM cuped_plans WHERE plan_key = ?", (plan_key,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"cuped plan {plan_key!r} not found")
    return _row_to_cuped_plan(row)


def get_cuped_plan_for_metric(experiment_key: str, version_number: int,
                              metric_key: str) -> Optional[CupedPlan]:
    row = get_conn().execute(
        "SELECT * FROM cuped_plans WHERE experiment_key = ? "
        "AND version_number = ? AND metric_key = ?",
        (experiment_key, version_number, metric_key)).fetchone()
    return _row_to_cuped_plan(row) if row else None


def list_cuped_plans(experiment_key: str) -> list[CupedPlan]:
    get_experiment(experiment_key)  # 404 early
    rows = get_conn().execute(
        "SELECT * FROM cuped_plans WHERE experiment_key = ? "
        "ORDER BY created_at, plan_key", (experiment_key,)).fetchall()
    return [_row_to_cuped_plan(r) for r in rows]


def insert_cuped_snapshot(plan_key: str, cutoff_iso: str,
                          theta: Optional[float], paired_users: int,
                          anomalies: list[str], result: dict[str, Any]
                          ) -> tuple[bool, dict[str, Any]]:
    """Insert a frozen CUPED snapshot; a duplicate cutoff re-serves stored row.

    Returns (inserted_now, stored_row_dict). The duplicate-cutoff check and
    the sequence allocation happen in one serialized write transaction, so
    concurrent first-time submissions can never collide.
    """
    with transaction() as tx:
        existing = tx.execute(
            "SELECT id FROM cuped_snapshots WHERE plan_key = ? AND cutoff_at = ?",
            (plan_key, cutoff_iso)).fetchone()
        if existing is not None:
            snapshot_id = existing["id"]
            inserted = False
        else:
            next_sequence = tx.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next "
                "FROM cuped_snapshots WHERE plan_key = ?",
                (plan_key,)).fetchone()["next"]
            cur = tx.execute(
                "INSERT INTO cuped_snapshots (plan_key, sequence, cutoff_at, "
                " theta, paired_users, anomalies_json, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_key, next_sequence, cutoff_iso, theta, paired_users,
                 json.dumps(anomalies), json.dumps(result),
                 to_iso(utcnow())))
            snapshot_id = cur.lastrowid
            inserted = True
    return inserted, get_cuped_snapshot_by_id(snapshot_id)


def get_cuped_snapshot_by_id(snapshot_id: int) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM cuped_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"cuped snapshot id {snapshot_id} not found")
    return _cuped_snapshot_row(row)


def get_cuped_snapshot_at(plan_key: str, cutoff_iso: str) -> Optional[dict[str, Any]]:
    row = get_conn().execute(
        "SELECT * FROM cuped_snapshots WHERE plan_key = ? AND cutoff_at = ?",
        (plan_key, cutoff_iso)).fetchone()
    return _cuped_snapshot_row(row) if row else None


def list_cuped_snapshots(plan_key: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM cuped_snapshots WHERE plan_key = ? ORDER BY sequence",
        (plan_key,)).fetchall()
    return [_cuped_snapshot_row(r) for r in rows]


def _cuped_snapshot_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["anomalies"] = json.loads(d.pop("anomalies_json"))
    d["result"] = json.loads(d.pop("result_json"))
    return d


# ---------------------------------------------------------------------------
# Multi-metric release-decision plans + frozen decision snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleasePlan:
    id: int
    plan_key: str
    experiment_key: str
    version_number: int
    control_variant_key: str
    target_variant_key: str
    primary_metric_key: str
    primary_min_effect: float
    correction: str
    alpha: float
    guardrails: list[dict[str, Any]]
    created_at: str


def _row_to_release_plan(row: Any) -> ReleasePlan:
    return ReleasePlan(
        id=row["id"], plan_key=row["plan_key"],
        experiment_key=row["experiment_key"],
        version_number=row["version_number"],
        control_variant_key=row["control_variant_key"],
        target_variant_key=row["target_variant_key"],
        primary_metric_key=row["primary_metric_key"],
        primary_min_effect=row["primary_min_effect"],
        correction=row["correction"], alpha=row["alpha"],
        guardrails=json.loads(row["guardrails_json"]),
        created_at=row["created_at"])


def create_release_plan(spec: dict[str, Any]) -> ReleasePlan:
    try:
        with transaction() as tx:
            cur = tx.execute(
                "INSERT INTO release_plans (plan_key, experiment_key, "
                " version_number, control_variant_key, target_variant_key, "
                " primary_metric_key, primary_min_effect, correction, alpha, "
                " guardrails_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (spec["plan_key"], spec["experiment_key"],
                 spec["version_number"], spec["control_variant_key"],
                 spec["target_variant_key"], spec["primary_metric_key"],
                 spec["primary_min_effect"], spec["correction"],
                 spec["alpha"], json.dumps(spec["guardrails"]),
                 to_iso(utcnow())))
            plan_id = cur.lastrowid
    except Exception as exc:
        if _is_unique(exc):
            raise ConflictError(
                "release_plan_exists",
                f"release plan {spec['plan_key']!r} already exists")
        raise
    return get_release_plan_by_id(plan_id)


def get_release_plan_by_id(plan_id: int) -> ReleasePlan:
    row = get_conn().execute(
        "SELECT * FROM release_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"release plan id {plan_id} not found")
    return _row_to_release_plan(row)


def get_release_plan(plan_key: str) -> ReleasePlan:
    row = get_conn().execute(
        "SELECT * FROM release_plans WHERE plan_key = ?", (plan_key,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"release plan {plan_key!r} not found")
    return _row_to_release_plan(row)


def list_release_plans(experiment_key: str) -> list[ReleasePlan]:
    get_experiment(experiment_key)  # 404 early
    rows = get_conn().execute(
        "SELECT * FROM release_plans WHERE experiment_key = ? "
        "ORDER BY created_at, plan_key", (experiment_key,)).fetchall()
    return [_row_to_release_plan(r) for r in rows]


def insert_release_snapshot(plan_key: str, cutoff_iso: str, decision: str,
                            result: dict[str, Any]
                            ) -> tuple[bool, dict[str, Any]]:
    """Insert a frozen release-decision snapshot; duplicate cutoff re-serves.

    Returns (inserted_now, stored_row_dict). The duplicate-cutoff check and
    the sequence allocation happen in one serialized write transaction, so
    concurrent first-time submissions can never collide, and a re-requested
    cutoff always returns the first frozen result unchanged.
    """
    with transaction() as tx:
        existing = tx.execute(
            "SELECT id FROM release_snapshots "
            "WHERE plan_key = ? AND cutoff_at = ?",
            (plan_key, cutoff_iso)).fetchone()
        if existing is not None:
            snapshot_id = existing["id"]
            inserted = False
        else:
            next_sequence = tx.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next "
                "FROM release_snapshots WHERE plan_key = ?",
                (plan_key,)).fetchone()["next"]
            cur = tx.execute(
                "INSERT INTO release_snapshots (plan_key, sequence, "
                " cutoff_at, decision, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (plan_key, next_sequence, cutoff_iso, decision,
                 json.dumps(result), to_iso(utcnow())))
            snapshot_id = cur.lastrowid
            inserted = True
    return inserted, get_release_snapshot_by_id(snapshot_id)


def get_release_snapshot_by_id(snapshot_id: int) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT * FROM release_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"release snapshot id {snapshot_id} not found")
    return _release_snapshot_row(row)


def get_release_snapshot_at(plan_key: str,
                            cutoff_iso: str) -> Optional[dict[str, Any]]:
    row = get_conn().execute(
        "SELECT * FROM release_snapshots WHERE plan_key = ? AND cutoff_at = ?",
        (plan_key, cutoff_iso)).fetchone()
    return _release_snapshot_row(row) if row else None


def list_release_snapshots(plan_key: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM release_snapshots WHERE plan_key = ? ORDER BY sequence",
        (plan_key,)).fetchall()
    return [_release_snapshot_row(r) for r in rows]


def _release_snapshot_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json"))
    return d

