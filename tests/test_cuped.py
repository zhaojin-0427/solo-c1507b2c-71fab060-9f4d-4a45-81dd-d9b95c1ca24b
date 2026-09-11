"""End-to-end tests for the CUPED covariate-adjustment module.

Covers:

* immutable pre-exposure plan creation rules (continuous metric only, no
  prior exposure, one plan per metric, window positivity);
* leakage safety: covariates are read only from the half-open window ending
  strictly before each user's FIRST enrolled exposure, and targets only from
  events occurred before the cutoff;
* pooled θ estimation, raw/adjusted means, control differences, 95% CIs and
  variance-reduction rates (sum/mean aggregation, exclude/fill policies);
* anomalies: zero covariate variance, insufficient sample and an adjusted
  variance that is larger than raw — the raw analysis is always retained;
* frozen snapshots: re-requesting the same plan + cutoff returns the stored
  result byte for byte, and events arriving afterwards never rewrite history.

Exposure ``recorded_at`` is the server clock at insert time, so covariates
are stamped a fixed number of days *before the test's wall clock* and target
events a short sleep *after* the exposures; short sleeps keep every strict
inequality exact without depending on clock resolution.
"""

import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from .test_lifecycle import base_config, create_experiment

API = "/api"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _tick(seconds: float = 0.02) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _experiment(client: TestClient, key: str,
                whitelist: dict[str, str] | None = None):
    cfg = base_config(traffic=100)
    if whitelist is not None:
        cfg["whitelist"] = [{"user_key": u, "variant_key": v}
                            for u, v in whitelist.items()]
    create_experiment(client, key=key, namespace=f"ns-{key}", config=cfg)


def _metric(client: TestClient, key: str, metric_key: str = "spend",
            event_name: str = "spend", direction: str = "maximize",
            window: int = 86400 * 60, min_sample: int = 2):
    r = client.post(
        f"{API}/experiments/{key}/versions/1/metrics",
        json={"metric_key": metric_key, "metric_type": "continuous",
              "event_name": event_name,
              "attribution_window_seconds": window,
              "direction": direction, "min_sample_size": min_sample,
              "srm_threshold": 1.0})
    assert r.status_code == 201, r.text
    return r.json()


def _binary_metric(client: TestClient, key: str, metric_key: str = "buy"):
    r = client.post(
        f"{API}/experiments/{key}/versions/1/metrics",
        json={"metric_key": metric_key, "metric_type": "binary",
              "event_name": metric_key,
              "attribution_window_seconds": 86400,
              "direction": "maximize", "min_sample_size": 1,
              "srm_threshold": 1.0})
    assert r.status_code == 201, r.text


def _plan(client: TestClient, key: str, plan_key: str = "p1", *,
          covariate_event: str = "prespend", window: int = 86400 * 14,
          target_agg: str = "sum", covariate_agg: str = "sum",
          missing: str = "exclude", metric_key: str = "spend",
          status: int = 201):
    body = {"plan_key": plan_key, "covariate_event_name": covariate_event,
            "preexposure_window_seconds": window,
            "target_aggregation": target_agg,
            "covariate_aggregation": covariate_agg,
            "missing_covariate_policy": missing}
    r = client.post(
        f"{API}/experiments/{key}/versions/1/metrics/{metric_key}/cuped-plans",
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


def _event(client: TestClient, key: str, user: str, name: str, at: datetime,
           *, value=None, event_key: str | None = None):
    body = {"event_key": event_key or f"{name}-{user}-{at.isoformat()}",
            "user_key": user, "event_name": name,
            "occurred_at": _iso(at)}
    if value is not None:
        body["value"] = value
    r = client.post(f"{API}/experiments/{key}/events", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _snapshot(client: TestClient, plan_key: str,
              cutoff: datetime | None = None, status: int = 201):
    cutoff = cutoff or datetime.now(timezone.utc)
    r = client.post(f"{API}/cuped-plans/{plan_key}/snapshots",
                    json={"cutoff_at": _iso(cutoff)})
    assert r.status_code == status, r.text
    return r.json()


def _variants(result: dict) -> dict[str, dict]:
    return {v["variant_key"]: v for v in result["variants"]}


# ---------------------------------------------------------------------------
# Plan creation rules
# ---------------------------------------------------------------------------


def test_plan_requires_continuous_metric(client):
    _experiment(client, "cupbin")
    _binary_metric(client, "cupbin")
    r = client.post(
        f"{API}/experiments/cupbin/versions/1/metrics/buy/cuped-plans",
        json={"plan_key": "pb", "covariate_event_name": "x",
              "preexposure_window_seconds": 3600})
    assert r.status_code == 422
    issues = r.json()["error"]["details"]["issues"]
    assert issues[0]["code"] == "metric_not_continuous"


def test_plan_rejects_nonpositive_window_at_request_layer(client):
    _experiment(client, "cupwin")
    _metric(client, "cupwin")
    r = client.post(
        f"{API}/experiments/cupwin/versions/1/metrics/spend/cuped-plans",
        json={"plan_key": "pw", "covariate_event_name": "x",
              "preexposure_window_seconds": 0})
    assert r.status_code == 400  # schema bound (ge=1), not a structured issue


def test_plan_must_predate_any_exposure_and_is_unique(client):
    _experiment(client, "cuplate")
    _metric(client, "cuplate")
    _expose(client, "cuplate", "early")
    r = client.post(
        f"{API}/experiments/cuplate/versions/1/metrics/spend/cuped-plans",
        json={"plan_key": "plate", "covariate_event_name": "x",
              "preexposure_window_seconds": 3600})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_after_exposure"

    # unknown experiment / version / metric -> 404 chain
    r = client.post(
        f"{API}/experiments/ghost/versions/1/metrics/spend/cuped-plans",
        json={"plan_key": "pg", "covariate_event_name": "x",
              "preexposure_window_seconds": 3600})
    assert r.status_code == 404


def test_plan_unique_per_metric_and_per_plan_key(client):
    _experiment(client, "cupdup")
    _metric(client, "cupdup")
    _plan(client, "cupdup", "pdup")
    r = client.post(
        f"{API}/experiments/cupdup/versions/1/metrics/spend/cuped-plans",
        json={"plan_key": "pother", "covariate_event_name": "x",
              "preexposure_window_seconds": 3600})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "cuped_plan_exists_for_metric"
    r = client.post(
        f"{API}/experiments/cupdup/versions/1/metrics/spend/cuped-plans",
        json={"plan_key": "pdup", "covariate_event_name": "x",
              "preexposure_window_seconds": 3600})
    # the per-metric guard fires before the unique plan_key constraint
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "cuped_plan_exists_for_metric"
    # listing + fetch expose the single immutable plan
    items = client.get(f"{API}/experiments/cupdup/cuped-plans").json()["items"]
    assert [p["plan_key"] for p in items] == ["pdup"]
    assert client.get(f"{API}/cuped-plans/pdup").status_code == 200
    assert client.get(f"{API}/cuped-plans/nope").status_code == 404


# ---------------------------------------------------------------------------
# Adjustment: theta, means, difference, CI, variance reduction
# ---------------------------------------------------------------------------


def test_cuped_adjusts_variance_while_preserving_lift(client):
    # Y = X for control and Y = X + 5 for treatment: theta must be exactly 1
    # and the adjusted within-arm variance collapses to zero.
    users = [f"u{i}" for i in range(40)]
    wl = {u: ("control" if i < 20 else "treatment")
          for i, u in enumerate(users)}
    _experiment(client, "cupadj", wl)
    _metric(client, "cupadj")
    _plan(client, "cupadj", "padj", covariate_agg="mean")

    cov_t = datetime.now(timezone.utc) - timedelta(days=7)
    for i, u in enumerate(users):
        _event(client, "cupadj", u, "prespend", cov_t,
               value=float(10 + i % 5), event_key=f"cov-{u}")

    by_variant = {"control": [], "treatment": []}
    for u in users:
        d = _expose(client, "cupadj", u)
        by_variant[d["variant_key"]].append(u)

    _tick()
    tgt_t = datetime.now(timezone.utc)
    for u in users:
        x = 10 + users.index(u) % 5
        lift = 5.0 if u in by_variant["treatment"] else 0.0
        _event(client, "cupadj", u, "spend", tgt_t, value=x + lift,
               event_key=f"tgt-{u}")

    _tick()
    out = _snapshot(client, "padj")
    assert out["duplicate"] is False
    res = out["result"]
    assert res["anomalies"] == []
    assert res["adjusted"] is True
    assert res["theta"]["value"] == 1.0
    assert res["theta"]["paired_users"] == 40

    rows = _variants(res)
    x_bar = 12.0  # mean of 10..14
    assert rows["control"]["raw_mean"] == x_bar
    assert rows["treatment"]["raw_mean"] == x_bar + 5
    assert rows["control"]["adjusted_mean"] == x_bar
    assert rows["treatment"]["adjusted_mean"] == x_bar + 5
    assert rows["control"]["adjusted_variance"] == 0
    assert rows["treatment"]["adjusted_variance"] == 0
    assert rows["control"]["variance_reduction_rate"] == 1.0
    assert rows["treatment"]["variance_reduction_rate"] == 1.0
    # zero residual variance -> degenerate point CI
    assert rows["treatment"]["ci95"]["lower"] == x_bar + 5
    assert rows["treatment"]["ci95"]["upper"] == x_bar + 5
    # raw CI is strictly wider than the adjusted (point) CI
    assert rows["treatment"]["raw_ci95"]["upper"] > x_bar + 5

    cmp_row = res["comparisons"][0]
    assert cmp_row["variant_key"] == "treatment"
    assert cmp_row["raw_difference"] == 5.0
    assert cmp_row["adjusted_difference"] == 5.0
    assert cmp_row["variance_reduction_rate"] == 1.0
    assert cmp_row["favorable"] is True
    assert cmp_row["ci95"]["lower"] == 5.0 and cmp_row["ci95"]["upper"] == 5.0

    totals = res["totals"]
    assert totals["enrolled_users"] == 40
    assert totals["paired_users"] == 40
    assert totals["covariate_events_used"] == 40
    assert totals["target_events_attributed"] == 40
    assert totals["overall_variance_reduction_rate"] == 1.0
    assert totals["excluded_users"] == {"total": 0, "no_target": 0,
                                        "missing_covariate": 0}


def test_cuped_minimize_marks_unfavorable_difference(client):
    cov_t = datetime.now(timezone.utc) - timedelta(days=2)
    # both arms carry the SAME multiset of covariates, so the pooled theta is
    # driven purely by the within-arm X-Y relationship (theta = 1)
    arm_users = {"control": [f"c{i}" for i in range(10)],
                 "treatment": [f"t{i}" for i in range(10)]}
    wl = {u: arm for arm, names in arm_users.items() for u in names}
    _experiment(client, "cupmin", wl)
    _metric(client, "cupmin", direction="minimize")
    _plan(client, "cupmin", "pmin")
    for arm, names in arm_users.items():
        for i, u in enumerate(names):
            _event(client, "cupmin", u, "prespend", cov_t,
                   value=float(20 + i % 4), event_key=f"cov-{u}")
    for arm in arm_users.values():
        for u in arm:
            _expose(client, "cupmin", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    for arm, names in arm_users.items():
        for i, u in enumerate(names):
            lift = -3.0 if arm == "treatment" else 0.0
            _event(client, "cupmin", u, "spend", tgt_t,
                   value=float(20 + i % 4) + lift, event_key=f"tgt-{u}")
    _tick()
    cmp_row = _snapshot(client, "pmin")["result"]["comparisons"][0]
    assert cmp_row["adjusted_difference"] == -3.0
    assert cmp_row["favorable"] is True  # lower is better for minimize


# ---------------------------------------------------------------------------
# Leakage safety: covariate window ends strictly before first exposure
# ---------------------------------------------------------------------------


def test_covariate_window_excludes_post_exposure_and_stale_events(client):
    users = ["c0", "c1", "t0", "t1"]
    wl = {u: ("control" if u.startswith("c") else "treatment") for u in users}
    _experiment(client, "cuplk", wl)
    _metric(client, "cuplk")
    _plan(client, "cuplk", "plk", window=86400 * 14)

    # valid covariates: one week before the test runs (within the 14d window)
    cov_t = datetime.now(timezone.utc) - timedelta(days=7)
    # stale covariate: far outside the look-back window -> must be ignored
    stale_t = datetime.now(timezone.utc) - timedelta(days=30)
    for u in users:
        _event(client, "cuplk", u, "prespend", cov_t, value=10.0,
               event_key=f"cov-{u}")
    _event(client, "cuplk", "c0", "prespend", stale_t, value=5000.0,
           event_key="cov-stale")

    exposure_at = {}
    for u in users:
        exposure_at[u] = datetime.now(timezone.utc)
        _expose(client, "cuplk", u)

    # covariate events at / after the exposure must never enter X. The window
    # is anchored to the exposure's recorded_at (slightly after this instant),
    # so an event a few seconds later is unambiguously post-exposure.
    _event(client, "cuplk", "c0", "prespend",
           exposure_at["c0"] + timedelta(seconds=5),
           value=9001.0, event_key="cov-post")

    _tick()
    tgt_t = datetime.now(timezone.utc)
    for u in users:
        _event(client, "cuplk", u, "spend", tgt_t, value=12.0,
               event_key=f"tgt-{u}")

    _tick()
    res = _snapshot(client, "plk")["result"]
    totals = res["totals"]
    # exactly four valid covariate events; the stale and post-exposure ones
    # are absent, so the pooled covariate mean stays at 10
    assert totals["covariate_events_used"] == 4
    assert res["theta"]["covariate_mean"] == 10.0


# ---------------------------------------------------------------------------
# Missing-covariate policies
# ---------------------------------------------------------------------------


def test_missing_covariate_exclude_policy_drops_users(client):
    users = ["c0", "c1", "t0", "t1"]
    wl = {u: ("control" if u.startswith("c") else "treatment") for u in users}
    _experiment(client, "cupex", wl)
    _metric(client, "cupex")
    _plan(client, "cupex", "pex", missing="exclude")

    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    for u in ["c0", "c1", "t0"]:  # t1 has no covariate
        _event(client, "cupex", u, "prespend", cov_t, value=float(len(u)),
               event_key=f"cov-{u}")
    for u in users:
        _expose(client, "cupex", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    for u in users:
        _event(client, "cupex", u, "spend", tgt_t, value=10.0,
               event_key=f"tgt-{u}")
    _tick()
    res = _snapshot(client, "pex")["result"]
    totals = res["totals"]
    assert totals["enrolled_users"] == 4
    assert totals["users_with_target"] == 4
    assert totals["paired_users"] == 3
    assert totals["filled_covariate_users"] == 0
    assert totals["excluded_users"]["missing_covariate"] == 1
    rows = _variants(res)
    assert rows["treatment"]["paired_users"] == 1
    # a single paired user leaves the sample variance / CI undefined
    assert rows["treatment"]["raw_variance"] is None
    assert rows["treatment"]["ci95"]["lower"] is None


def test_missing_covariate_population_mean_fill_keeps_users(client):
    users = ["c0", "c1", "t0", "t1"]
    wl = {u: ("control" if u.startswith("c") else "treatment") for u in users}
    _experiment(client, "cupfill", wl)
    _metric(client, "cupfill")
    _plan(client, "cupfill", "pfill", missing="population_mean")

    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    for u, x in [("c0", 2.0), ("c1", 4.0)]:  # both treatment users lack X
        _event(client, "cupfill", u, "prespend", cov_t, value=x,
               event_key=f"cov-{u}")
    for u in users:
        _expose(client, "cupfill", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    targets = {"c0": 10.0, "c1": 12.0, "t0": 20.0, "t1": 22.0}
    for u, y in targets.items():
        _event(client, "cupfill", u, "spend", tgt_t, value=y,
               event_key=f"tgt-{u}")
    _tick()
    res = _snapshot(client, "pfill")["result"]
    totals = res["totals"]
    assert totals["paired_users"] == 4
    assert totals["filled_covariate_users"] == 2
    assert totals["excluded_users"]["missing_covariate"] == 0
    # theta comes only from the two complete (control) pairs
    assert res["theta"]["paired_users"] == 2
    assert res["theta"]["covariate_mean"] == 3.0
    rows = _variants(res)
    # filled X = pooled mean 3 -> adjustment term is zero -> raw == adjusted
    assert rows["treatment"]["raw_mean"] == 21.0
    assert rows["treatment"]["adjusted_mean"] == 21.0
    assert rows["treatment"]["filled_covariate_users"] == 2


def test_user_without_target_is_excluded_from_pairs(client):
    wl = {"c0": "control", "t0": "treatment"}
    _experiment(client, "cupnt", wl)
    _metric(client, "cupnt", min_sample=1)
    _plan(client, "cupnt", "pnt")
    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    _event(client, "cupnt", "c0", "prespend", cov_t, value=3.0,
           event_key="cov-c0")
    _event(client, "cupnt", "t0", "prespend", cov_t, value=5.0,
           event_key="cov-t0")
    for u in wl:
        _expose(client, "cupnt", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    _event(client, "cupnt", "c0", "spend", tgt_t, value=8.0,
           event_key="tgt-c0")  # t0 never converts
    _tick()
    res = _snapshot(client, "pnt")["result"]
    totals = res["totals"]
    assert totals["enrolled_users"] == 2
    assert totals["users_with_target"] == 1
    assert totals["paired_users"] == 1
    assert totals["excluded_users"]["no_target"] == 1


# ---------------------------------------------------------------------------
# Aggregation: sum vs mean per user
# ---------------------------------------------------------------------------


def test_mean_aggregation_for_covariate_and_target(client):
    users = ["c0", "c1", "t0", "t1"]
    wl = {u: ("control" if u.startswith("c") else "treatment") for u in users}
    _experiment(client, "cupmean", wl)
    _metric(client, "cupmean")
    _plan(client, "cupmean", "pmean", target_agg="mean",
          covariate_agg="mean")

    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    # two covariate events per user: their MEAN is (x + (x+2))/2 = x+1
    for i, u in enumerate(users):
        x = float(2 * i)
        _event(client, "cupmean", u, "prespend", cov_t, value=x,
               event_key=f"cov-{u}-a")
        _event(client, "cupmean", u, "prespend", cov_t, value=x + 2.0,
               event_key=f"cov-{u}-b")
    for u in users:
        _expose(client, "cupmean", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    # two target events per user averaging x+10
    for i, u in enumerate(users):
        _event(client, "cupmean", u, "spend", tgt_t,
               value=float(2 * i) + 9.0, event_key=f"tgt-{u}-a")
        _event(client, "cupmean", u, "spend", tgt_t,
               value=float(2 * i) + 11.0, event_key=f"tgt-{u}-b")
    _tick()
    res = _snapshot(client, "pmean")["result"]
    assert res["totals"]["paired_users"] == 4
    assert res["totals"]["covariate_events_used"] == 8
    assert res["totals"]["target_events_attributed"] == 8
    rows = _variants(res)
    # control users i=0,1 -> covariate means 1, 3; target means 10, 12
    assert rows["control"]["raw_mean"] == 11.0
    # treatment users i=2,3 -> covariate means 5, 7; target means 14, 16
    assert rows["treatment"]["raw_mean"] == 15.0


def test_invalid_value_target_event_is_excluded_from_y(client):
    wl = {"c0": "control", "c1": "control", "t0": "treatment",
          "t1": "treatment"}
    _experiment(client, "cupow", wl)
    _metric(client, "cupow", window=3600, min_sample=2)
    _plan(client, "cupow", "pow")
    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    for u in wl:
        _event(client, "cupow", u, "prespend", cov_t, value=4.0,
               event_key=f"cov-{u}")
    for u in wl:
        _expose(client, "cupow", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    for u in wl:
        _event(client, "cupow", u, "spend", tgt_t, value=10.0,
               event_key=f"tgt-{u}")
    # one target event without any numeric value: stored but invalid for a
    # continuous metric, so it is excluded from Y rather than collapsing it
    _event(client, "cupow", "t0", "spend", tgt_t,
           event_key="tgt-novalue")
    _tick()
    res = _snapshot(client, "pow")["result"]
    totals = res["totals"]
    assert totals["target_events_attributed"] == 4
    assert totals["target_events_excluded"]["invalid_value"] == 1
    # the value-less target must not have turned t0's sum into something odd
    assert _variants(res)["treatment"]["raw_mean"] == 10.0


# ---------------------------------------------------------------------------
# Anomalies (raw analysis is always retained)
# ---------------------------------------------------------------------------


def test_zero_covariate_variance_keeps_raw_analysis(client):
    users = ["c0", "c1", "t0", "t1"]
    wl = {u: ("control" if u.startswith("c") else "treatment") for u in users}
    _experiment(client, "cupzv", wl)
    _metric(client, "cupzv")
    _plan(client, "cupzv", "pzv")
    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    for u in users:  # every covariate identical -> Var(X) = 0
        _event(client, "cupzv", u, "prespend", cov_t, value=7.0,
               event_key=f"cov-{u}")
    for u in users:
        _expose(client, "cupzv", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    for u, y in zip(users, [10.0, 12.0, 20.0, 22.0]):
        _event(client, "cupzv", u, "spend", tgt_t, value=y,
               event_key=f"tgt-{u}")
    _tick()
    res = _snapshot(client, "pzv")["result"]
    assert "zero_covariate_variance" in res["anomalies"]
    assert res["adjusted"] is False
    assert res["theta"]["value"] is None
    assert res["theta"]["estimable"] is False
    rows = _variants(res)
    assert rows["control"]["raw_mean"] == 11.0
    assert rows["control"]["adjusted_mean"] is None
    # the displayed CI falls back to the raw CI rather than disappearing
    assert rows["control"]["ci95"]["lower"] == rows["control"]["raw_ci95"]["lower"]
    cmp_row = res["comparisons"][0]
    assert cmp_row["adjusted_difference"] is None
    assert cmp_row["raw_difference"] == 10.0
    assert cmp_row["ci95"]["lower"] == cmp_row["raw_ci95"]["lower"]


def test_insufficient_sample_is_flagged_per_variant_and_totals(client):
    users = ["c0", "t0"]
    wl = {"c0": "control", "t0": "treatment"}
    _experiment(client, "cupis", wl)
    _metric(client, "cupis", min_sample=10)
    _plan(client, "cupis", "pis")
    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    for u, x in zip(users, [2.0, 8.0]):
        _event(client, "cupis", u, "prespend", cov_t, value=x,
               event_key=f"cov-{u}")
    for u in users:
        _expose(client, "cupis", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    for u, y in zip(users, [10.0, 20.0]):
        _event(client, "cupis", u, "spend", tgt_t, value=y,
               event_key=f"tgt-{u}")
    _tick()
    res = _snapshot(client, "pis")["result"]
    assert "insufficient_sample" in res["anomalies"]
    assert all(v["insufficient_sample"] for v in res["variants"])


def test_adjusted_variance_increase_is_flagged_and_raw_kept(client):
    # Deterministic configuration on which the pooled OLS theta injects
    # residual spread into an arm whose raw outcomes are constant:
    # control X=[5,4,2,0] Y=[5,5,5,4], treatment X=[9,8,8,6] Y=[0,0,0,0].
    wl = {"c0": "control", "c1": "control", "c2": "control",
          "c3": "control", "t0": "treatment", "t1": "treatment",
          "t2": "treatment", "t3": "treatment"}
    _experiment(client, "cupvi", wl)
    _metric(client, "cupvi")
    _plan(client, "cupvi", "pvi")
    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    cov_values = {"c0": 5, "c1": 4, "c2": 2, "c3": 0,
                  "t0": 9, "t1": 8, "t2": 8, "t3": 6}
    for u, x in cov_values.items():
        _event(client, "cupvi", u, "prespend", cov_t, value=float(x),
               event_key=f"cov-{u}")
    for u in wl:
        _expose(client, "cupvi", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    y_values = {"c0": 5, "c1": 5, "c2": 5, "c3": 4,
                "t0": 0, "t1": 0, "t2": 0, "t3": 0}
    for u, y in y_values.items():
        _event(client, "cupvi", u, "spend", tgt_t, value=float(y),
               event_key=f"tgt-{u}")
    _tick()
    res = _snapshot(client, "pvi")["result"]
    assert res["adjusted"] is True
    assert "adjusted_variance_increased" in res["anomalies"]
    assert res["totals"]["overall_variance_reduction_rate"] < 0
    # raw columns are still fully present alongside the adjusted ones
    rows = _variants(res)
    assert rows["treatment"]["raw_variance"] == 0
    assert rows["treatment"]["adjusted_variance"] > 0


def test_snapshot_with_no_data_is_a_raw_only_anomaly_snapshot(client):
    _experiment(client, "cupempty")
    _metric(client, "cupempty")
    _plan(client, "cupempty", "pempty")
    _tick()
    res = _snapshot(client, "pempty")["result"]
    assert res["adjusted"] is False
    assert "zero_covariate_variance" in res["anomalies"]
    assert "insufficient_sample" in res["anomalies"]
    assert res["totals"]["paired_users"] == 0
    assert all(v["raw_mean"] is None for v in res["variants"])


# ---------------------------------------------------------------------------
# Frozen snapshots, sequences and cutoff rules
# ---------------------------------------------------------------------------


def test_snapshot_is_frozen_and_replay_returns_it_verbatim(client):
    users = ["c0", "c1", "t0", "t1"]
    wl = {u: ("control" if u.startswith("c") else "treatment") for u in users}
    _experiment(client, "cupfrz", wl)
    _metric(client, "cupfrz")
    _plan(client, "cupfrz", "pfrz")
    cov_t = datetime.now(timezone.utc) - timedelta(days=1)
    for i, u in enumerate(users):
        _event(client, "cupfrz", u, "prespend", cov_t,
               value=float(2 + i), event_key=f"cov-{u}")
    for u in users:
        _expose(client, "cupfrz", u)
    _tick()
    tgt_t = datetime.now(timezone.utc)
    for i, u in enumerate(users):
        _event(client, "cupfrz", u, "spend", tgt_t,
               value=float(10 + 2 * i), event_key=f"tgt-{u}")
    _tick()
    cutoff = datetime.now(timezone.utc)
    first = _snapshot(client, "pfrz", cutoff)
    assert first["sequence"] == 1

    replay = _snapshot(client, "pfrz", cutoff)
    assert replay["duplicate"] is True
    assert replay["result"] == first["result"]

    # Events arriving LATER (one valid pre-exposure covariate, one fresh
    # target) must not rewrite the frozen snapshot.
    _event(client, "cupfrz", "c0", "prespend", cov_t, value=999.0,
           event_key="cov-late")
    _event(client, "cupfrz", "t1", "spend",
           datetime.now(timezone.utc), value=999.0, event_key="tgt-late")
    replay2 = _snapshot(client, "pfrz", cutoff)
    assert replay2["duplicate"] is True
    assert replay2["result"] == first["result"]

    # a later cutoff creates sequence 2 and sees the new data
    _tick()
    second = _snapshot(client, "pfrz")
    assert second["duplicate"] is False
    assert second["sequence"] == 2
    assert second["result"] != first["result"]

    # history + direct fetch
    hist = client.get(f"{API}/cuped-plans/pfrz/snapshots").json()
    assert [s["sequence"] for s in hist["snapshots"]] == [1, 2]
    assert hist["plan"]["snapshots_recorded"] == 2
    one = client.get(f"{API}/cuped-plans/pfrz/snapshots/1").json()
    assert one["result"] == first["result"]
    missing = client.get(f"{API}/cuped-plans/pfrz/snapshots/9")
    assert missing.status_code == 404


def test_snapshot_rejects_future_and_pre_plan_cutoffs(client):
    _experiment(client, "cupcut")
    _metric(client, "cupcut")
    _plan(client, "cupcut", "pcut")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    r = client.post(f"{API}/cuped-plans/pcut/snapshots",
                    json={"cutoff_at": _iso(future)})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "cutoff_in_future"

    r = client.post(f"{API}/cuped-plans/pcut/snapshots",
                    json={"cutoff_at": _iso(datetime.now(timezone.utc)
                                            - timedelta(days=400))})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "cutoff_before_plan"

    # nothing was frozen
    hist = client.get(f"{API}/cuped-plans/pcut/snapshots").json()
    assert hist["snapshots"] == []
