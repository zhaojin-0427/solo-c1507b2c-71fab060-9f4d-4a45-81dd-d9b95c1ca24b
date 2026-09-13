"""End-to-end tests for the multi-metric release-decision module.

Covers:

* immutable pre-exposure plan creation rules (variant pair, one primary +
  guardrails, cross-version / duplicate / unbounded-window metrics rejected,
  no plan after the version has exposures, DB-level immutability);
* frozen as-of snapshots: only exposures/events before the cutoff count and
  users whose attribution window has not fully elapsed are excluded;
* per-metric sample sizes, oriented effects, 95% CIs, raw/adjusted p-values
  (Holm and Bonferroni) and threshold gaps, all with plugged-in formulas;
* the three-way decision: ship (primary over its effect threshold and
  significant after correction, every guardrail within its non-inferiority
  bound), do_not_ship (any guardrail significantly violated), otherwise
  insufficient_evidence — with explicit reason codes;
* idempotent replay: the same plan + cutoff re-serves the frozen snapshot
  byte for byte and later events never rewrite history.

Time note: exposure ``recorded_at`` is the server clock at insert time, and a
user only becomes eligible once ``first_exposure + attribution_window`` has
elapsed by the cutoff. Tests therefore use a 1-second attribution window,
report each user's events immediately after that user's own exposure (so
``occurred_at`` lands inside the window), and sleep a little over a second
before snapshotting — the same real-clock discipline as the sequential and
CUPED suites.
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.release import adjust_pvalues

from .test_lifecycle import base_config, create_experiment

API = "/api"
WINDOW = 1  # attribution window in seconds; one short sleep makes users eligible


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wait_window(seconds: float = WINDOW) -> None:
    """Let the attribution window fully elapse after the last exposure."""
    time.sleep(seconds + 0.1)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _experiment(client: TestClient, key: str, n_control: int, n_treatment: int):
    """Published 50/50 experiment; every user is force-assigned by whitelist."""
    cfg = base_config(traffic=100)
    cfg["whitelist"] = (
        [{"user_key": f"c{i}", "variant_key": "control"}
         for i in range(n_control)]
        + [{"user_key": f"t{i}", "variant_key": "treatment"}
           for i in range(n_treatment)])
    create_experiment(client, key=key, namespace=f"ns-{key}", config=cfg)


def _metric(client: TestClient, key: str, metric_key: str,
            metric_type: str = "binary", direction: str = "maximize",
            window: int = WINDOW, version: int = 1):
    r = client.post(
        f"{API}/experiments/{key}/versions/{version}/metrics",
        json={"metric_key": metric_key, "metric_type": metric_type,
              "event_name": metric_key,
              "attribution_window_seconds": window,
              "direction": direction, "min_sample_size": 1,
              "srm_threshold": 1.0})
    assert r.status_code == 201, r.text
    return r.json()


def _plan(client: TestClient, key: str, plan_key: str = "rp1",
          primary=("buy", 0.0), guardrails=(), correction="holm",
          alpha: float = 0.05, version: int = 1, status: int = 201,
          control: str = "control", target: str = "treatment"):
    body = {
        "plan_key": plan_key,
        "control_variant_key": control,
        "target_variant_key": target,
        "primary": {"metric_key": primary[0],
                    "min_favorable_effect": primary[1]},
        "guardrails": [{"metric_key": g[0], "non_inferiority_margin": g[1]}
                       for g in guardrails],
        "correction": correction,
        "alpha": alpha,
    }
    r = client.post(
        f"{API}/experiments/{key}/versions/{version}/release-plans",
        json=body)
    assert r.status_code == status, r.text
    return r.json()


def _expose(client: TestClient, key: str, user: str):
    r = client.post(f"{API}/experiments/{key}/decide",
                    json={"user_key": user, "record_exposure": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enrolled"] is True, d
    return d


def _event(client: TestClient, key: str, user: str, name: str,
           value=None, at: datetime | None = None):
    body = {"event_key": f"{name}-{user}", "user_key": user,
            "event_name": name, "occurred_at": _iso(at or _now())}
    if value is not None:
        body["value"] = value
    r = client.post(f"{API}/experiments/{key}/events", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _conv(prefix: str, count: int, name: str = "buy", value=None,
          start: int = 0) -> list[tuple[str, str, object]]:
    """Build (user_key, event_name, value) triples for _traffic."""
    return [(f"{prefix}{i}", name, value) for i in range(start, start + count)]


def _traffic(client: TestClient, key: str, n_control: int, n_treatment: int,
             events=()):
    """Expose every user; report each user's events immediately after their
    own exposure so ``occurred_at`` lands inside the 1-second window even
    when the full loop takes longer than the window itself."""
    per_user: dict[str, list] = {}
    for user, name, value in events:
        per_user.setdefault(user, []).append((name, value))
    for i in range(n_control):
        u = f"c{i}"
        _expose(client, key, u)
        for name, value in per_user.get(u, ()):
            _event(client, key, u, name, value=value)
    for i in range(n_treatment):
        u = f"t{i}"
        _expose(client, key, u)
        for name, value in per_user.get(u, ()):
            _event(client, key, u, name, value=value)


def _snapshot(client: TestClient, plan_key: str, cutoff: datetime | None = None,
              status: int = 201):
    r = client.post(f"{API}/release-plans/{plan_key}/snapshots",
                    json={"cutoff_at": _iso(cutoff or _now())})
    assert r.status_code == status, r.text
    return r.json()


def _metrics(result: dict):
    """(primary, guardrails) from a snapshot result."""
    return result["metrics"][0], result["metrics"][1:]


# ---------------------------------------------------------------------------
# Plan creation rules
# ---------------------------------------------------------------------------


def test_create_plan_happy_path(client):
    _experiment(client, "rel", 1, 1)
    _metric(client, "rel", "buy")
    _metric(client, "rel", "retain")
    out = _plan(client, "rel", primary=("buy", 0.02),
                guardrails=[("retain", 0.05)], correction="holm", alpha=0.1)
    assert out["plan_key"] == "rp1"
    assert out["version_number"] == 1
    assert out["control_variant_key"] == "control"
    assert out["target_variant_key"] == "treatment"
    assert out["primary"]["metric"]["metric_key"] == "buy"
    assert out["primary"]["min_favorable_effect"] == 0.02
    assert out["guardrails"][0]["metric"]["metric_key"] == "retain"
    assert out["guardrails"][0]["non_inferiority_margin"] == 0.05
    assert out["correction"] == "holm" and out["alpha"] == 0.1
    assert out["snapshots_recorded"] == 0

    # listed and fetchable; unknown plan key is a 404
    items = client.get(f"{API}/experiments/rel/release-plans").json()["items"]
    assert [p["plan_key"] for p in items] == ["rp1"]
    assert client.get(f"{API}/release-plans/rp1").status_code == 200
    assert client.get(f"{API}/release-plans/nope").status_code == 404


def test_plan_validation_variant_rules(client):
    _experiment(client, "relv", 1, 1)
    _metric(client, "relv", "buy")

    r = client.post(f"{API}/experiments/relv/versions/1/release-plans",
                    json={"plan_key": "p1", "control_variant_key": "ghost",
                          "target_variant_key": "treatment",
                          "primary": {"metric_key": "buy",
                                      "min_favorable_effect": 0.0}})
    assert r.status_code == 422
    assert "unknown_variant" in {i["code"] for i in
                                 r.json()["error"]["details"]["issues"]}

    out = _plan(client, "relv", plan_key="p2", control="control",
                target="control", status=422)
    codes = {i["code"] for i in out["error"]["details"]["issues"]}
    assert "distinct_arms_required" in codes

    out = _plan(client, "relv", plan_key="p3", control="treatment",
                target="control", status=422)
    codes = {i["code"] for i in out["error"]["details"]["issues"]}
    assert "control_mismatch" in codes


def test_plan_validation_zero_allocation_arm(client):
    cfg = base_config(traffic=100, variants=[
        {"key": "control", "percentage": 50, "is_control": True},
        {"key": "treatment", "percentage": 50},
        {"key": "zombie", "percentage": 0},
    ])
    create_experiment(client, key="relz", namespace="ns-relz", config=cfg)
    _metric(client, "relz", "buy")
    out = _plan(client, "relz", target="zombie", status=422)
    codes = {i["code"] for i in out["error"]["details"]["issues"]}
    assert "zero_allocation_arm" in codes


def test_plan_rejects_cross_version_and_unknown_metrics(client):
    _experiment(client, "relx", 1, 1)
    _metric(client, "relx", "buy")  # defined on v1 only

    # publish a second version; "buy" does not exist on v2
    r = client.post(f"{API}/experiments/relx/versions?publish=true",
                    json=base_config(traffic=100))
    assert r.status_code == 201, r.text
    _metric(client, "relx", "retain", version=2)

    out = _plan(client, "relx", version=2, primary=("buy", 0.0), status=422)
    issues = out["error"]["details"]["issues"]
    assert [i["code"] for i in issues] == ["metric_not_on_version"]

    out = _plan(client, "relx", version=2, primary=("retain", 0.0),
                guardrails=[("ghost", 0.05)], status=422)
    issues = out["error"]["details"]["issues"]
    assert [i["code"] for i in issues] == ["metric_not_on_version"]
    assert issues[0]["location"] == "guardrails.0"


def test_plan_rejects_duplicate_metrics(client):
    _experiment(client, "reld", 1, 1)
    _metric(client, "reld", "buy")
    _metric(client, "reld", "retain")

    # primary repeated as a guardrail
    out = _plan(client, "reld", primary=("buy", 0.0),
                guardrails=[("buy", 0.05)], status=422)
    codes = [i["code"] for i in out["error"]["details"]["issues"]]
    assert codes == ["duplicate_metric"]

    # the same guardrail twice
    out = _plan(client, "reld", primary=("buy", 0.0),
                guardrails=[("retain", 0.05), ("retain", 0.1)], status=422)
    codes = [i["code"] for i in out["error"]["details"]["issues"]]
    assert codes == ["duplicate_metric"]


def test_plan_rejects_unbounded_attribution_window(client):
    _experiment(client, "relu", 1, 1)
    _metric(client, "relu", "buy", window=0)  # 0 means unbounded
    out = _plan(client, "relu", primary=("buy", 0.0), status=422)
    issues = out["error"]["details"]["issues"]
    assert [i["code"] for i in issues] == ["unbounded_attribution_window"]


def test_plan_rejected_after_exposure(client):
    _experiment(client, "rele", 1, 1)
    _metric(client, "rele", "buy")
    _traffic(client, "rele", 1, 1)  # one enrolled exposure on each arm
    r = client.post(
        f"{API}/experiments/rele/versions/1/release-plans",
        json={"plan_key": "late", "control_variant_key": "control",
              "target_variant_key": "treatment",
              "primary": {"metric_key": "buy", "min_favorable_effect": 0.0}})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_after_exposure"


def test_duplicate_plan_key_rejected(client):
    _experiment(client, "relp", 1, 1)
    _metric(client, "relp", "buy")
    _plan(client, "relp", plan_key="dup")
    r = client.post(
        f"{API}/experiments/relp/versions/1/release-plans",
        json={"plan_key": "dup", "control_variant_key": "control",
              "target_variant_key": "treatment",
              "primary": {"metric_key": "buy", "min_favorable_effect": 0.0}})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "release_plan_exists"


def test_plan_and_snapshot_are_db_immutable(client):
    _experiment(client, "reli", 2, 2)
    _metric(client, "reli", "buy")
    _plan(client, "reli")
    _traffic(client, "reli", 2, 2)
    _wait_window()
    _snapshot(client, "rp1")

    conn = get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE release_plans SET alpha = 0.5")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM release_plans")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE release_snapshots SET decision = 'ship'")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM release_snapshots")
    conn.rollback()


# ---------------------------------------------------------------------------
# Correction math (unit)
# ---------------------------------------------------------------------------


def test_adjust_pvalues_holm_and_bonferroni():
    assert adjust_pvalues([], "holm") == []
    assert adjust_pvalues([0.01, 0.04, 0.03], "bonferroni") == [0.03, 0.12, 0.09]
    assert adjust_pvalues([0.01, 0.04, 0.03], "holm") == [0.03, 0.06, 0.06]
    # adjusted values are capped at 1 and Holm enforces monotonicity
    assert adjust_pvalues([0.8, 0.9], "bonferroni") == [1.0, 1.0]
    assert adjust_pvalues([0.5, 0.01], "holm") == [0.5, 0.02]


# ---------------------------------------------------------------------------
# Snapshot decisions
# ---------------------------------------------------------------------------


def test_snapshot_not_evaluable_without_exposures(client):
    _experiment(client, "rel0", 1, 1)
    _metric(client, "rel0", "buy")
    _plan(client, "rel0")
    out = _snapshot(client, "rp1")
    primary, _ = _metrics(out["result"])
    assert primary["evaluable"] is False
    assert primary["not_evaluable_reason"] == "empty_arm"
    assert primary["status"] == "not_evaluable"
    assert primary["p_value"] is None and primary["p_adjusted"] is None
    assert primary["threshold_gap"] is None
    decision = out["result"]["decision"]
    assert decision["decision"] == "insufficient_evidence"
    assert decision["reasons"] == ["primary_not_evaluable:empty_arm"]
    assert decision["family_size"] == 0


def test_ship_decision_binary_with_handcomputed_holm(client):
    _experiment(client, "rels", 100, 100)
    _metric(client, "rels", "buy")
    _metric(client, "rels", "retain")
    _plan(client, "rels", primary=("buy", 0.05),
          guardrails=[("retain", 0.05)])
    _traffic(client, "rels", 100, 100,
             events=_conv("c", 10, "buy") + _conv("t", 25, "buy")
                     + _conv("c", 50, "retain") + _conv("t", 52, "retain"))
    _wait_window()
    out = _snapshot(client, "rp1")

    assert out["duplicate"] is False and out["sequence"] == 1
    primary, guardrails = _metrics(out["result"])
    guard = guardrails[0]

    # primary: p_c = 0.10, p_t = 0.25, effect 0.15, SE ~ 0.052678
    assert primary["role"] == "primary" and primary["status"] == "pass"
    assert primary["arms"]["control"]["users"] == 100
    assert primary["arms"]["control"]["conversions"] == 10
    assert primary["arms"]["target"]["conversions"] == 25
    assert primary["effect"] == pytest.approx(0.15, abs=1e-8)
    assert primary["standard_error"] == pytest.approx(0.0526780, abs=1e-6)
    assert primary["ci95"]["lower"] == pytest.approx(0.15 - 1.96 * 0.0526780,
                                                     abs=1e-4)
    assert primary["ci95"]["upper"] == pytest.approx(0.15 + 1.96 * 0.0526780,
                                                     abs=1e-4)
    assert primary["p_value"] == pytest.approx(0.002204, abs=1e-4)
    # Holm over k=2: the smallest raw p is doubled
    assert primary["p_adjusted"] == pytest.approx(2 * primary["p_value"],
                                                  abs=1e-8)
    assert primary["threshold"] == 0.05
    assert primary["threshold_gap"] == pytest.approx(0.10, abs=1e-8)
    assert "superiority p = 1 - Φ" in primary["formula"]
    assert "0.150000" in primary["formula"]

    # guardrail: effect 0.02 well inside the 0.05 margin
    assert guard["metric_key"] == "retain" and guard["role"] == "guardrail"
    assert guard["status"] == "within_bound"
    assert guard["threshold"] == -0.05
    assert guard["threshold_gap"] == pytest.approx(0.07, abs=1e-8)
    assert guard["p_value"] == pytest.approx(0.83899, abs=1e-4)
    assert guard["p_adjusted"] == pytest.approx(guard["p_value"], abs=1e-8)
    assert "violation p = Φ" in guard["formula"]

    decision = out["result"]["decision"]
    assert decision["decision"] == "ship"
    assert decision["reasons"] == ["primary_effect_threshold_met",
                                   "primary_significant_after_correction",
                                   "guardrail_within_bound:retain"]
    assert decision["correction"] == "holm"
    assert decision["family_size"] == 2
    assert "Holm" in decision["formula"]
    assert out["formulas"]["decision"]


def test_do_not_ship_when_guardrail_significantly_violated(client):
    _experiment(client, "relg", 100, 100)
    _metric(client, "relg", "buy")
    _metric(client, "relg", "crash", direction="minimize")
    _plan(client, "relg", primary=("buy", 0.05),
          guardrails=[("crash", 0.05)])
    _traffic(client, "relg", 100, 100,
             events=_conv("c", 10, "buy") + _conv("t", 25, "buy")
                     + _conv("c", 10, "crash") + _conv("t", 30, "crash"))
    _wait_window()
    out = _snapshot(client, "rp1")

    primary, guardrails = _metrics(out["result"])
    guard = guardrails[0]
    assert primary["status"] == "pass"  # the primary alone would ship
    assert guard["status"] == "violated"
    # minimize guardrail: crash rate 10% -> 30% is oriented -0.20
    assert guard["effect"] == pytest.approx(-0.20, abs=1e-8)
    assert guard["threshold_gap"] == pytest.approx(-0.15, abs=1e-8)
    assert guard["p_value"] == pytest.approx(0.003084, abs=1e-4)
    assert guard["p_adjusted"] < 0.05

    decision = out["result"]["decision"]
    assert decision["decision"] == "do_not_ship"
    assert decision["reasons"] == ["guardrail_violated:crash"]


def test_insufficient_when_primary_not_significant(client):
    _experiment(client, "reln", 10, 10)
    _metric(client, "reln", "buy")
    _plan(client, "reln", primary=("buy", 0.0))
    _traffic(client, "reln", 10, 10,
             events=_conv("c", 1, "buy") + _conv("t", 2, "buy"))
    _wait_window()
    out = _snapshot(client, "rp1")

    primary, _ = _metrics(out["result"])
    assert primary["status"] == "not_significant"
    assert primary["threshold_gap"] >= 0  # the estimate is favorable
    assert primary["p_adjusted"] >= 0.05
    decision = out["result"]["decision"]
    assert decision["decision"] == "insufficient_evidence"
    assert decision["reasons"] == ["primary_not_significant_after_correction"]


def test_insufficient_when_primary_below_effect_threshold(client):
    _experiment(client, "relt", 100, 100)
    _metric(client, "relt", "buy")
    _plan(client, "relt", primary=("buy", 0.20))  # demands +20 points
    _traffic(client, "relt", 100, 100,
             events=_conv("c", 10, "buy") + _conv("t", 25, "buy"))
    _wait_window()
    out = _snapshot(client, "rp1")

    primary, _ = _metrics(out["result"])
    # significant (p ~ 0.002) but the 0.15 estimate is under the 0.20 bar
    assert primary["p_adjusted"] < 0.05
    assert primary["threshold_gap"] == pytest.approx(-0.05, abs=1e-8)
    assert primary["status"] == "below_threshold"
    decision = out["result"]["decision"]
    assert decision["decision"] == "insufficient_evidence"
    assert decision["reasons"] == ["primary_effect_below_threshold"]


def test_insufficient_when_guardrail_crossed_but_not_significant(client):
    _experiment(client, "relc", 100, 100)
    _metric(client, "relc", "buy")
    _metric(client, "relc", "retain")
    _plan(client, "relc", primary=("buy", 0.05),
          guardrails=[("retain", 0.05)])
    # guardrail estimate slips 8 points past the margin, but noisily
    _traffic(client, "relc", 100, 100,
             events=_conv("c", 10, "buy") + _conv("t", 25, "buy")
                     + _conv("c", 80, "retain") + _conv("t", 72, "retain"))
    _wait_window()
    out = _snapshot(client, "rp1")

    primary, guardrails = _metrics(out["result"])
    guard = guardrails[0]
    assert primary["status"] == "pass"
    assert guard["status"] == "crossed_not_significant"
    assert guard["threshold_gap"] == pytest.approx(-0.03, abs=1e-8)
    assert guard["p_adjusted"] >= 0.05
    decision = out["result"]["decision"]
    assert decision["decision"] == "insufficient_evidence"
    assert decision["reasons"] == [
        "guardrail_crossed_bound_not_significant:retain"]


def test_continuous_primary_minimize_direction_ships(client):
    _experiment(client, "relm", 10, 10)
    _metric(client, "relm", "latency", metric_type="continuous",
            direction="minimize")
    _plan(client, "relm", primary=("latency", 2.0))
    events = ([(f"c{i}", "latency", v)
               for i, v in enumerate([20, 21, 19, 20, 21,
                                      19, 20, 21, 19, 20])]
              + [(f"t{i}", "latency", v)
                 for i, v in enumerate([9, 10, 11, 10, 9,
                                        11, 10, 9, 11, 10])])
    _traffic(client, "relm", 10, 10, events=events)
    _wait_window()
    out = _snapshot(client, "rp1")

    primary, guardrails = _metrics(out["result"])
    assert guardrails == []
    # minimize: oriented effect = -(10 - 20) = +10
    assert primary["effect"] == pytest.approx(10.0, abs=1e-8)
    assert primary["arms"]["control"]["observations"] == 10
    assert primary["arms"]["control"]["value"] == pytest.approx(20.0)
    assert primary["threshold_gap"] == pytest.approx(8.0, abs=1e-8)
    assert primary["status"] == "pass"
    assert out["result"]["decision"]["decision"] == "ship"


def test_bonferroni_and_holm_adjust_the_same_family_differently(client):
    _experiment(client, "relb", 100, 100)
    _metric(client, "relb", "buy")
    _metric(client, "relb", "retain")
    # two plans on the same version, created before any exposure
    _plan(client, "relb", plan_key="rp-holm", primary=("buy", 0.05),
          guardrails=[("retain", 0.05)], correction="holm")
    _plan(client, "relb", plan_key="rp-bonf", primary=("buy", 0.05),
          guardrails=[("retain", 0.05)], correction="bonferroni")
    _traffic(client, "relb", 100, 100,
             events=_conv("c", 10, "buy") + _conv("t", 25, "buy")
                     + _conv("c", 50, "retain") + _conv("t", 52, "retain"))
    _wait_window()

    holm = _snapshot(client, "rp-holm")
    bonf = _snapshot(client, "rp-bonf")
    hg = _metrics(holm["result"])[1][0]
    bg = _metrics(bonf["result"])[1][0]
    # same raw p, but Bonferroni doubles every p while Holm leaves the
    # largest one (nearly) untouched
    assert hg["p_value"] == bg["p_value"]
    assert hg["p_adjusted"] == pytest.approx(hg["p_value"], abs=1e-8)
    assert bg["p_adjusted"] == pytest.approx(min(1.0, 2 * bg["p_value"]),
                                             abs=1e-8)
    assert bg["p_adjusted"] > hg["p_adjusted"]
    assert holm["result"]["decision"]["decision"] == "ship"
    assert bonf["result"]["decision"]["decision"] == "ship"


# ---------------------------------------------------------------------------
# Window completion, exclusions, cutoff rules, freezing
# ---------------------------------------------------------------------------


def test_users_with_unfinished_attribution_window_are_excluded(client):
    _experiment(client, "relw", 10, 10)
    # a 2-second window gives the exposure loop ample room to finish while
    # every user's window is still open
    _metric(client, "relw", "buy", window=2)
    _plan(client, "relw", primary=("buy", 0.0))
    _traffic(client, "relw", 10, 10,
             events=_conv("c", 4, "buy") + _conv("t", 6, "buy"))

    # snapshot immediately: every user's window is still open
    early = _snapshot(client, "rp1")
    primary, _ = _metrics(early["result"])
    assert primary["evaluable"] is False
    assert primary["not_evaluable_reason"] == "empty_arm"
    assert primary["excluded_users"]["window_incomplete"] == 20
    assert primary["exclusions"]["window_incomplete_user"] == 10
    assert primary["events_attributed"] == 0
    assert early["result"]["decision"]["decision"] == "insufficient_evidence"

    # after the window elapses the very same users and events count
    _wait_window(2)
    late = _snapshot(client, "rp1")
    primary, _ = _metrics(late["result"])
    assert late["sequence"] == 2
    assert primary["evaluable"] is True
    assert primary["arms"]["control"]["users"] == 10
    assert primary["arms"]["control"]["conversions"] == 4
    assert primary["arms"]["target"]["conversions"] == 6
    assert primary["excluded_users"]["window_incomplete"] == 0
    assert primary["events_attributed"] == 10
    assert primary["effect"] == pytest.approx(0.2, abs=1e-8)


def test_exclusion_reasons_are_listed_per_metric(client):
    _experiment(client, "relx2", 10, 10)
    _metric(client, "relx2", "buy")
    _plan(client, "relx2", primary=("buy", 0.0))
    t0 = _now()
    _traffic(client, "relx2", 10, 10, events=_conv("t", 5, "buy"))
    # ghost user: no exposure at all
    _event(client, "relx2", "ghost", "buy")
    # event predating the user's first exposure
    _event(client, "relx2", "c0", "buy", at=t0 - timedelta(hours=1))
    _wait_window()
    out = _snapshot(client, "rp1")
    primary, _ = _metrics(out["result"])
    assert primary["exclusions"]["no_exposure"] == 1
    assert primary["exclusions"]["event_before_exposure"] == 1
    assert primary["events_attributed"] == 5
    assert out["formulas"]["exclusions"]


def test_cutoff_must_not_be_future_or_before_plan(client):
    _experiment(client, "relcut", 1, 1)
    _metric(client, "relcut", "buy")
    plan = _plan(client, "relcut")

    r = client.post(f"{API}/release-plans/rp1/snapshots",
                    json={"cutoff_at": _iso(_now() + timedelta(hours=1))})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "cutoff_in_future"

    created = datetime.fromisoformat(plan["created_at"])
    r = client.post(
        f"{API}/release-plans/rp1/snapshots",
        json={"cutoff_at": _iso(created - timedelta(seconds=1))})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "cutoff_before_plan"


def test_same_cutoff_replays_frozen_snapshot_and_history_is_kept(client):
    _experiment(client, "relf", 50, 50)
    _metric(client, "relf", "buy")
    _plan(client, "relf", primary=("buy", 0.0))

    # expose users one by one, remembering each pre-exposure instant; the
    # first batch of conversions is reported right after each user's exposure
    exposed_at: dict[str, datetime] = {}
    batch1 = {f"c{i}" for i in range(5)} | {f"t{i}" for i in range(15)}
    for i in range(50):
        for prefix in ("c", "t"):
            u = f"{prefix}{i}"
            exposed_at[u] = _now()
            _expose(client, "relf", u)
            if u in batch1:
                _event(client, "relf", u, "buy")
    _wait_window()

    cutoff = _now()
    first = _snapshot(client, "rp1", cutoff)
    assert first["duplicate"] is False
    assert first["result"]["decision"]["decision"] == "ship"

    # more conversions are REPORTED after the freeze, backdated into each
    # user's still-open attribution window (they occurred before the frozen
    # cutoff but did not exist when it was computed)
    for i in range(15, 35):
        u = f"t{i}"
        _event(client, "relf", u, "buy",
               at=exposed_at[u] + timedelta(seconds=0.3))

    # exact replay: the frozen result is re-served verbatim — later data,
    # even data backdated into the frozen window, never rewrites history
    replay = _snapshot(client, "rp1", cutoff)
    assert replay["duplicate"] is True
    assert replay["result"] == first["result"]
    assert replay["submitted_at"] == first["submitted_at"]
    target = replay["result"]["metrics"][0]["arms"]["target"]
    assert target["conversions"] == 15  # not 35

    # a later cutoff does see the new events; the frozen one stays untouched
    later = _snapshot(client, "rp1")
    assert later["sequence"] == 2
    assert later["result"]["metrics"][0]["arms"]["target"][
        "conversions"] == 35

    history = client.get(f"{API}/release-plans/rp1/snapshots").json()
    assert [s["sequence"] for s in history["snapshots"]] == [1, 2]
    assert all(s["decision"] == "ship" for s in history["snapshots"])
    assert history["snapshots"][0]["primary_status"] == "pass"

    by_seq = client.get(f"{API}/release-plans/rp1/snapshots/1").json()
    assert by_seq["result"] == first["result"]
    assert client.get(
        f"{API}/release-plans/rp1/snapshots/99").status_code == 404
