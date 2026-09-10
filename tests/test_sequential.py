"""Regression tests for the sequential-testing module.

Covers the four verified defects:

1. a checkpoint containing attributed events used to crash with
   ``NameError: name 'timezone' is not defined`` (missing import in the
   as-of aggregation path);
2. a future ``cutoff_at`` froze an incomplete snapshot — it must now be
   rejected (422 cutoff_in_future), while an exact cutoff replay still returns
   the stored snapshot verbatim;
3. for a *minimize* metric the design-drift conditional power was computed
   from the signed (negative) ``absolute_effect``, turning a favorable
   downward effect into a ``futility_stop``;
4. the continuous-metric standard error used exposure counts as denominators
   instead of the actual valid observation counts.

The tests exercise binary and continuous metrics under both optimization
directions and additionally assert monotonic cutoff/information ordering and
frozen-snapshot stability.

Time note: exposure ``recorded_at`` is stamped with the server clock at
insert time, so these tests run a real chronological timeline (plan ->
exposures -> events -> slightly-later cutoff), using short sleeps to keep the
strict inequalities exact.
"""

import math
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from .test_lifecycle import base_config, create_experiment

API = "/api"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _tick(seconds: float = 0.02) -> None:
    """Advance the real clock a little so strict (< cutoff) ordering holds."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _two_arm_experiment(client: TestClient, key: str = "seq",
                        force_whitelist: dict[str, str] | None = None):
    """Published 50/50 experiment with control/treatment variants."""
    cfg = base_config(traffic=100)
    if force_whitelist:
        cfg["whitelist"] = [{"user_key": u, "variant_key": v}
                            for u, v in force_whitelist.items()]
    create_experiment(client, key=key, namespace=f"ns-{key}", config=cfg)


def _metric(client: TestClient, key: str, metric_key: str, metric_type: str,
            event_name: str, direction: str, window: int = 86400):
    r = client.post(
        f"{API}/experiments/{key}/versions/1/metrics",
        json={"metric_key": metric_key, "metric_type": metric_type,
              "event_name": event_name,
              "attribution_window_seconds": window,
              "direction": direction, "min_sample_size": 1,
              "srm_threshold": 1.0})
    assert r.status_code == 201, r.text
    return r.json()


def _plan(client: TestClient, key: str, metric_key: str, plan_key: str = "p1",
          *, hypothesis: str = "two_sided", alpha: float = 0.05,
          max_sample_size: int = 400, planned_checks: int = 5,
          cp_threshold: float = 0.2, boundary_type: str = "obrien_fleming",
          design_assumptions=None):
    body = {"plan_key": plan_key,
            "control_variant_key": "control",
            "target_variant_key": "treatment",
            "hypothesis": hypothesis, "alpha": alpha,
            "max_sample_size": max_sample_size,
            "planned_checks": planned_checks,
            "conditional_power_threshold": cp_threshold,
            "boundary_type": boundary_type}
    if design_assumptions is not None:
        body["design_assumptions"] = design_assumptions
    r = client.post(
        f"{API}/experiments/{key}/versions/1/metrics/{metric_key}"
        f"/sequential-plans", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _expose(client: TestClient, key: str, user: str):
    # recorded_at is the server clock at insert time (request `at` is not used
    # for the stored stamp), so no timestamp is passed here.
    r = client.post(f"{API}/experiments/{key}/decide",
                    json={"user_key": user, "record_exposure": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enrolled"] is True, d
    return d


def _event(client: TestClient, key: str, user: str, event_name: str,
           value=None, event_key=None, at: datetime | None = None):
    body = {"event_key": event_key or f"ev-{user}-{event_name}",
            "user_key": user, "event_name": event_name,
            "occurred_at": _iso(at or datetime.now(timezone.utc))}
    if value is not None:
        body["value"] = value
    r = client.post(f"{API}/experiments/{key}/events", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _checkpoint(client: TestClient, plan_key: str,
                cutoff: datetime | None = None, status: int = 201):
    cutoff = cutoff or datetime.now(timezone.utc)
    r = client.post(f"{API}/sequential-plans/{plan_key}/checkpoints",
                    json={"cutoff_at": _iso(cutoff)})
    assert r.status_code == status, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Defect 1: event-bearing checkpoint no longer NameErrors
# + binary maximize end-to-end statistics + snapshot stability
# ---------------------------------------------------------------------------


def test_binary_maximize_checkpoint_with_events(client):
    users = [f"u{i}" for i in range(40)]
    wl = {u: ("control" if i < 20 else "treatment")
          for i, u in enumerate(users)}
    _two_arm_experiment(client, key="seqbin", force_whitelist=wl)
    _metric(client, "seqbin", "buy", "binary", "buy", "maximize")
    plan = _plan(client, "seqbin", "buy", plan_key="pb", cp_threshold=0.05,
                 design_assumptions={"kind": "binary",
                                     "control_rate": 0.10,
                                     "absolute_effect": 0.02})
    assert len(plan["planned_boundaries"]) == 5
    assert plan["planned_boundaries"][0]["upper_z"] > 4.0

    # exposures after plan creation
    by_variant: dict[str, list[str]] = {"control": [], "treatment": []}
    for u in users:
        d = _expose(client, "seqbin", u)
        by_variant[d["variant_key"]].append(u)
    assert len(by_variant["control"]) == 20
    assert len(by_variant["treatment"]) == 20

    _tick()
    converters = by_variant["control"][:2] + by_variant["treatment"][:6]
    for u in converters:
        _event(client, "seqbin", u, "buy", event_key=f"buy-{u}")

    _tick()
    cp = _checkpoint(client, "pb")  # defect 1 used to raise NameError here
    assert cp["duplicate"] is False
    res = cp["result"]
    assert res["sequence"] == 1
    rows = {a["role"]: a for a in res["arms"]}
    assert rows["control"]["n"] == 20
    assert rows["target"]["n"] == 20
    assert rows["control"]["conversions"] == 2
    assert rows["target"]["conversions"] == 6
    p_c, p_t = 2 / 20, 6 / 20
    assert abs(rows["control"]["value"] - p_c) < 1e-9
    assert abs(rows["target"]["value"] - p_t) < 1e-9

    # information time under 50/50 allocation: total / max = 40/400 = 0.1
    assert abs(res["information"]["information_time"] - 0.1) < 1e-9

    # pooled two-proportion Z
    p_bar = 8 / 40
    se = math.sqrt(p_bar * (1 - p_bar) * (1 / 20 + 1 / 20))
    expected_z = (p_t - p_c) / se
    assert abs(res["statistic"]["z"] - expected_z) < 1e-6
    assert res["statistic"]["z"] > 0  # favorable direction
    assert res["effect"]["difference"] == round(p_t - p_c, 8)
    assert res["boundary"]["lower_z"] is not None  # two-sided
    assert res["recommendation"] == "continue"
    assert res["terminal"] is False

    # design CP uses a positive (favorable) drift
    cp_block = res["conditional_power"]
    assert cp_block["basis"] == "design_absolute_effect"
    assert cp_block["design"]["drift"] > 0

    # exact replay returns the same frozen snapshot
    _tick()
    replay = _checkpoint(client, "pb",
                         datetime.fromisoformat(cp["cutoff_at"]))
    assert replay["duplicate"] is True
    assert replay["result"] == res

    # later events must not alter the frozen snapshot when replayed
    _event(client, "seqbin", by_variant["treatment"][6], "buy",
           event_key="buy-late-1")
    replay2 = _checkpoint(client, "pb",
                          datetime.fromisoformat(cp["cutoff_at"]))
    assert replay2["result"] == res


# ---------------------------------------------------------------------------
# Defect 2: future cutoff rejected, nothing frozen; replays still work
# ---------------------------------------------------------------------------


def test_future_cutoff_is_rejected_and_does_not_freeze(client):
    _two_arm_experiment(client)
    _metric(client, "seq", "buy", "binary", "buy", "maximize")
    _plan(client, "seq", "buy")

    future = datetime.now(timezone.utc) + timedelta(hours=2)
    r = client.post(f"{API}/sequential-plans/p1/checkpoints",
                    json={"cutoff_at": _iso(future)})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "cutoff_in_future"

    hist = client.get(f"{API}/sequential-plans/p1/checkpoints").json()
    assert hist["checkpoints"] == []
    assert hist["plan"]["checkpoints_recorded"] == 0


def test_exact_duplicate_cutoff_replays(client):
    _two_arm_experiment(
        client, force_whitelist={"a": "control", "b": "treatment"})
    _metric(client, "seq", "buy", "binary", "buy", "maximize")
    _plan(client, "seq", "buy", planned_checks=5)
    _expose(client, "seq", "a")
    _expose(client, "seq", "b")
    _tick()
    # only one user converts, so pooled variance is nonzero
    _event(client, "seq", "a", "buy", event_key="buy-a")
    _tick()
    cutoff = datetime.now(timezone.utc)
    cp1 = _checkpoint(client, "p1", cutoff)
    again = _checkpoint(client, "p1", cutoff)
    assert again["duplicate"] is True
    assert again["result"] == cp1["result"]


# ---------------------------------------------------------------------------
# Defect 3: minimize metric design drift must be oriented favorably
# ---------------------------------------------------------------------------


def test_binary_minimize_design_conditional_power_is_oriented(client):
    """A beneficial downward effect must not yield a futility stop."""
    users = [f"m{i}" for i in range(40)]
    wl = {u: ("control" if i < 20 else "treatment")
          for i, u in enumerate(users)}
    _two_arm_experiment(client, key="seqmin", force_whitelist=wl)
    _metric(client, "seqmin", "churn", "binary", "churn", "minimize")
    _plan(client, "seqmin", "churn", plan_key="pmin", cp_threshold=0.5,
          design_assumptions={"kind": "binary",
                              "control_rate": 0.20,
                              "absolute_effect": -0.10})

    by_variant: dict[str, list[str]] = {"control": [], "treatment": []}
    for u in users:
        d = _expose(client, "seqmin", u)
        by_variant[d["variant_key"]].append(u)

    _tick()
    # control churns 20% (4/20), target only 5% (1/20): strongly favorable
    churned = by_variant["control"][:4] + by_variant["treatment"][:1]
    for u in churned:
        _event(client, "seqmin", u, "churn", event_key=f"ch-{u}")

    _tick()
    res = _checkpoint(client, "pmin")["result"]
    assert res["statistic"]["z"] > 0  # target churn lower -> favorable Z
    design = res["conditional_power"]["design"]
    assert design is not None
    # stored absolute_effect is -0.02, but the shown drift must be positive
    assert design["drift"] > 0
    assert design["se_at_max_information"] > 0
    assert res["recommendation"] in ("continue", "significant_win"), \
        res["recommendation"]
    assert "conditional_power_below_threshold" not in res["stop_reasons"]


def test_binary_minimize_unfavorable_trend_has_low_observed_power(client):
    users = [f"u{i}" for i in range(40)]
    wl = {u: ("control" if i < 20 else "treatment")
          for i, u in enumerate(users)}
    _two_arm_experiment(client, key="seqbad", force_whitelist=wl)
    _metric(client, "seqbad", "churn", "binary", "churn", "minimize")
    _plan(client, "seqbad", "churn", plan_key="pbad", cp_threshold=0.5,
          design_assumptions={"kind": "binary",
                              "control_rate": 0.10,
                              "absolute_effect": -0.02})
    by_variant: dict[str, list[str]] = {"control": [], "treatment": []}
    for u in users:
        d = _expose(client, "seqbad", u)
        by_variant[d["variant_key"]].append(u)
    _tick()
    # target churns much MORE than control: unfavorable for minimize
    churned = by_variant["control"][:2] + by_variant["treatment"][:8]
    for u in churned:
        _event(client, "seqbad", u, "churn", event_key=f"ch-{u}")
    _tick()
    res = _checkpoint(client, "pbad")["result"]
    assert res["statistic"]["z"] < 0  # oriented unfavorable
    assert res["conditional_power"]["observed"]["value"] < 0.5


# ---------------------------------------------------------------------------
# Defect 4: continuous SE uses valid observation counts, not exposures
# ---------------------------------------------------------------------------


def test_continuous_maximize_uses_observation_counts_in_se(client):
    users = [f"c{i}" for i in range(20)]
    wl = {u: ("control" if i < 10 else "treatment")
          for i, u in enumerate(users)}
    _two_arm_experiment(client, key="seqcm", force_whitelist=wl)
    _metric(client, "seqcm", "spent", "continuous", "spent", "maximize")
    _plan(client, "seqcm", "spent", plan_key="pcm",
          design_assumptions={"kind": "continuous",
                              "standard_deviation": 5.0,
                              "absolute_effect": 3.0})

    by_variant: dict[str, list[str]] = {"control": [], "treatment": []}
    for u in users:
        d = _expose(client, "seqcm", u)
        by_variant[d["variant_key"]].append(u)
    _tick()

    # Only 3 responses per arm despite 10 exposures apiece.
    for u, v in zip(by_variant["control"][:3], [10.0, 12.0, 11.0]):
        _event(client, "seqcm", u, "spent", value=v, event_key=f"s-{u}")
    for u, v in zip(by_variant["treatment"][:3], [20.0, 24.0, 22.0]):
        _event(client, "seqcm", u, "spent", value=v, event_key=f"s-{u}")

    _tick()
    res = _checkpoint(client, "pcm")["result"]
    arms = {a["role"]: a for a in res["arms"]}
    assert arms["control"]["n"] == 10             # exposures
    assert arms["control"]["observations"] == 3   # valid observations
    assert arms["target"]["observations"] == 3
    assert arms["control"]["value"] == 11.0
    assert arms["target"]["value"] == 22.0

    # Welch Z must denominate by k=3 observations (not n=10 exposures)
    vc, vt = 1.0, 4.0  # sample variances of the two triples
    se_expected = math.sqrt(vc / 3 + vt / 3)
    z_expected = (22.0 - 11.0) / se_expected
    assert abs(res["statistic"]["standard_error"] - se_expected) < 1e-6
    assert abs(res["statistic"]["z"] - z_expected) < 1e-6
    assert "/3" in res["statistic"]["formula"]
    assert "n_c=10" in res["statistic"]["formula"]
    # information time reflects enrolled exposures: 20/400 = 0.05
    assert abs(res["information"]["information_time"] - 0.05) < 1e-9


def test_continuous_minimize_design_drift_oriented_and_observation_se(client):
    users = [f"l{i}" for i in range(20)]
    wl = {u: ("control" if i < 10 else "treatment")
          for i, u in enumerate(users)}
    _two_arm_experiment(client, key="seqlat", force_whitelist=wl)
    _metric(client, "seqlat", "latency", "continuous", "latency", "minimize")
    _plan(client, "seqlat", "latency", plan_key="plat", cp_threshold=0.5,
          design_assumptions={"kind": "continuous",
                              "standard_deviation": 2.0,
                              "absolute_effect": -3.0})
    by_variant: dict[str, list[str]] = {"control": [], "treatment": []}
    for u in users:
        d = _expose(client, "seqlat", u)
        by_variant[d["variant_key"]].append(u)
    _tick()
    for u, v in zip(by_variant["control"][:4], [100.0, 102.0, 98.0, 100.0]):
        _event(client, "seqlat", u, "latency", value=v, event_key=f"l-{u}")
    for u, v in zip(by_variant["treatment"][:4], [96.0, 94.0, 98.0, 96.0]):
        _event(client, "seqlat", u, "latency", value=v, event_key=f"l-{u}")
    _tick()
    res = _checkpoint(client, "plat")["result"]
    assert res["statistic"]["z"] > 0  # lower target latency is favorable
    assert res["conditional_power"]["design"]["drift"] > 0  # oriented, -3 -> +
    assert res["recommendation"] in ("continue", "significant_win")
    arms = {a["role"]: a for a in res["arms"]}
    assert arms["control"]["observations"] == 4
    assert arms["target"]["observations"] == 4
    # SE denominated by the 4 observations (not 10 exposures)
    assert abs(res["statistic"]["standard_error"]
               - math.sqrt(2.6666667 / 4 + 2.6666667 / 4)) < 1e-6


# ---------------------------------------------------------------------------
# Monotonic ordering: time and information must strictly increase
# ---------------------------------------------------------------------------


def test_checkpoints_must_increase_in_time_and_information(client):
    users = [f"o{i}" for i in range(40)]
    wl = {u: ("control" if i < 20 else "treatment")
          for i, u in enumerate(users)}
    _two_arm_experiment(client, key="seqord", force_whitelist=wl)
    _metric(client, "seqord", "buy", "binary", "buy", "maximize")
    _plan(client, "seqord", "buy", plan_key="pord", planned_checks=5)

    first_batch = users[:4] + users[20:24]  # 4 control + 4 treatment
    for u in first_batch:
        _expose(client, "seqord", u)
    # a few conversions (before the cutoff) so pooled variance is nonzero
    _event(client, "seqord", users[0], "buy", event_key="o-buy-0")
    _event(client, "seqord", users[20], "buy", event_key="o-buy-20")
    _event(client, "seqord", users[21], "buy", event_key="o-buy-21")
    _tick()
    cutoff1 = datetime.now(timezone.utc)
    c1 = _checkpoint(client, "pord", cutoff1)
    assert c1["result"]["sequence"] == 1
    info1 = c1["result"]["information"]["information_time"]
    assert info1 > 0

    # back-dated cutoff rejected
    r = client.post(
        f"{API}/sequential-plans/pord/checkpoints",
        json={"cutoff_at": _iso(cutoff1 - timedelta(minutes=30))})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "non_increasing_cutoff"

    # same information (no new exposures) at a later cutoff is rejected
    _tick()
    r2 = client.post(
        f"{API}/sequential-plans/pord/checkpoints",
        json={"cutoff_at": _iso(datetime.now(timezone.utc))})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "non_increasing_information"

    # add strictly more exposures in both arms -> checkpoint 2 accepted
    for u in users[8:]:
        _expose(client, "seqord", u)
    _tick()
    c2 = _checkpoint(client, "pord")
    assert c2["result"]["sequence"] == 2
    assert (c2["result"]["information"]["information_time"] > info1)

    hist = client.get(
        f"{API}/sequential-plans/pord/checkpoints").json()
    assert [c["sequence"] for c in hist["checkpoints"]] == [1, 2]
    assert "z =" in hist["checkpoints"][0]["formula"]
    assert "t =" in hist["checkpoints"][0]["formula"]
    assert hist["plan"]["checkpoints_recorded"] == 2


# ---------------------------------------------------------------------------
# Plan validation: metric type, direction, variants, parameters
# ---------------------------------------------------------------------------


def test_plan_validation_rejects_bad_variants_types_and_parameters(client):
    _two_arm_experiment(client)
    _metric(client, "seq", "buy", "binary", "buy", "maximize")
    base = {"plan_key": "px", "control_variant_key": "control",
            "target_variant_key": "treatment", "hypothesis": "two_sided",
            "max_sample_size": 100}
    url = (f"{API}/experiments/seq/versions/1/metrics/buy/sequential-plans")

    r = client.post(url, json={**base, "target_variant_key": "ghost"})
    assert r.status_code == 422
    assert "unknown_variant" in {
        i["code"] for i in r.json()["error"]["details"]["issues"]}

    r = client.post(url, json={**base, "plan_key": "py",
                               "target_variant_key": "control"})
    assert r.status_code == 422
    assert "distinct_arms_required" in {
        i["code"] for i in r.json()["error"]["details"]["issues"]}

    r = client.post(url, json={**base, "plan_key": "pz",
                               "design_assumptions": {
                                   "kind": "continuous",
                                   "standard_deviation": 1.0,
                                   "absolute_effect": 1.0}})
    assert r.status_code == 422
    assert "assumption_metric_type" in {
        i["code"] for i in r.json()["error"]["details"]["issues"]}

    r = client.post(url, json={**base, "plan_key": "pw",
                               "design_assumptions": {
                                   "kind": "binary", "control_rate": 0.2,
                                   "absolute_effect": -0.05}})
    assert r.status_code == 422
    assert "effect_direction" in {
        i["code"] for i in r.json()["error"]["details"]["issues"]}

    r = client.post(url, json={**base, "plan_key": "pv",
                               "max_sample_size": 3, "planned_checks": 5})
    assert r.status_code == 422
    assert "max_sample_too_small" in {
        i["code"] for i in r.json()["error"]["details"]["issues"]}

    # bad enum / out-of-range alpha -> 400 at request parse
    r = client.post(url, json={**base, "plan_key": "pu", "alpha": 1.5,
                               "boundary_type": "haybittle"})
    assert r.status_code == 400


def test_plan_must_predate_exposures_and_is_unique_per_metric(client):
    _two_arm_experiment(client)
    _metric(client, "seq", "buy", "binary", "buy", "maximize")
    _expose(client, "seq", "early")

    r = client.post(
        f"{API}/experiments/seq/versions/1/metrics/buy/sequential-plans",
        json={"plan_key": "plate", "control_variant_key": "control",
              "target_variant_key": "treatment",
              "hypothesis": "one_sided", "max_sample_size": 100})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_after_exposure"

    assert client.post(
        f"{API}/experiments/seq/versions/99/metrics/buy/sequential-plans",
        json={"plan_key": "q", "control_variant_key": "control",
              "target_variant_key": "treatment",
              "hypothesis": "one_sided", "max_sample_size": 100}
    ).status_code == 404
