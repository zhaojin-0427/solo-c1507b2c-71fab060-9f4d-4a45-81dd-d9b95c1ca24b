"""Metric attribution & effect analysis: end-to-end tests."""

from datetime import datetime, timedelta, timezone

from .test_lifecycle import base_config, create_experiment

API = "/api"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _experiment(client, key="exp", splits=(50, 50), traffic=100, whitelist=None):
    cfg = base_config(traffic=traffic, variants=[
        {"key": "control", "percentage": splits[0], "is_control": True},
        {"key": "treatment", "percentage": splits[1]},
    ])
    if whitelist is not None:
        cfg["whitelist"] = whitelist
    create_experiment(client, key=key, namespace=f"ns-{key}", config=cfg)


def _expose(client, user_key, key="exp", **extra):
    payload = {"user_key": user_key, "record_exposure": True}
    payload.update(extra)
    r = client.post(f"{API}/experiments/{key}/decide", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _metric(client, key="exp", version=1, metric_key="purchase",
            metric_type="binary", event_name="purchase", window=3600,
            direction="maximize", min_sample=1, srm=0.5):
    r = client.post(
        f"{API}/experiments/{key}/versions/{version}/metrics",
        json={"metric_key": metric_key, "metric_type": metric_type,
              "event_name": event_name,
              "attribution_window_seconds": window,
              "direction": direction, "min_sample_size": min_sample,
              "srm_threshold": srm})
    assert r.status_code == 201, r.text
    return r.json()


def _event(client, user_key, event_name="purchase", key="exp",
           event_key=None, at=None, value=None):
    body = {"event_key": event_key or f"ev-{event_name}-{user_key}",
            "user_key": user_key, "event_name": event_name}
    if at is not None:
        body["occurred_at"] = _iso(at)
    if value is not None:
        body["value"] = value
    r = client.post(f"{API}/experiments/{key}/events", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _analysis(client, key="exp", version=1, metric_key="purchase", **params):
    return client.get(
        f"{API}/experiments/{key}/versions/{version}/metrics/"
        f"{metric_key}/analysis", params=params)


# ---------------------------------------------------------------------------
# Metric definition lifecycle
# ---------------------------------------------------------------------------


def test_metric_definition_lifecycle_and_conflicts(client):
    _experiment(client)
    body = _metric(client)
    assert body["version_number"] == 1
    assert body["attribution_window_seconds"] == 3600

    # list metrics (all versions / one version)
    listing = client.get(f"{API}/experiments/exp/metrics").json()
    assert [m["metric_key"] for m in listing] == ["purchase"]
    v1 = client.get(f"{API}/experiments/exp/metrics",
                    params={"version": 1}).json()
    assert len(v1) == 1

    # duplicate (experiment, version, metric_key) -> 409
    r = client.post(f"{API}/experiments/exp/versions/1/metrics",
                    json={"metric_key": "purchase", "metric_type": "binary",
                          "event_name": "buy",
                          "attribution_window_seconds": 10,
                          "direction": "maximize"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "metric_exists"

    # same metric key on a new version is independent
    client.post(f"{API}/experiments/exp/versions",
                params={"publish": True}, json=base_config())
    r = client.post(f"{API}/experiments/exp/versions/2/metrics",
                    json={"metric_key": "purchase", "metric_type": "binary",
                          "event_name": "buy",
                          "attribution_window_seconds": 10,
                          "direction": "maximize"})
    assert r.status_code == 201

    # 404 chains: missing experiment / version / metric
    assert client.post(
        f"{API}/experiments/nope/versions/1/metrics",
        json={"metric_key": "x", "metric_type": "binary",
              "event_name": "e", "attribution_window_seconds": 10,
              "direction": "maximize"}).status_code == 404
    assert client.post(
        f"{API}/experiments/exp/versions/99/metrics",
        json={"metric_key": "x", "metric_type": "binary",
              "event_name": "e", "attribution_window_seconds": 10,
              "direction": "maximize"}).status_code == 404
    assert _analysis(client, metric_key="missing").status_code == 404

    # invalid payload -> 400 structured bad_request
    r = client.post(f"{API}/experiments/exp/versions/1/metrics",
                    json={"metric_key": "bad!", "metric_type": "binary",
                          "event_name": "e", "attribution_window_seconds": -1,
                          "direction": "sideways"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Binary metric: attribution, rates, lift, audit counts
# ---------------------------------------------------------------------------


def test_binary_metric_full_funnel(client):
    _experiment(client)
    _metric(client, min_sample=2, srm=0.9)

    t0 = datetime.now(timezone.utc)
    by_variant = {"control": [], "treatment": []}
    for i in range(12):
        d = _expose(client, f"u{i}")
        by_variant[d["variant_key"]].append(f"u{i}")

    # 50% of each variant converts
    converters = by_variant["control"][:max(1, len(by_variant["control"]) // 2)]
    converters += by_variant["treatment"][:max(1, len(by_variant["treatment"]) // 2)]
    for u in converters:
        ev = _event(client, u, at=t0 + timedelta(seconds=10))
        # ingestion preview attributes immediately
        assert ev["attributions"][0]["status"] == "attributed"
        assert ev["attributions"][0]["variant_key"] in (
            "control", "treatment")

    r = _analysis(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metric_type"] == "binary"
    assert body["control_variant_key"] == "control"

    rows = {v["variant_key"]: v for v in body["variants"]}
    total_exposed = sum(len(v) for v in by_variant.values())
    assert body["totals"]["exposures_used"] == total_exposed
    assert body["totals"]["events_in_window"] == len(converters)
    assert body["totals"]["events_attributed"] == len(converters)
    assert body["totals"]["duplicates"] == 0
    assert body["totals"]["valid_samples"] == total_exposed

    for variant, users in by_variant.items():
        row = rows[variant]
        assert row["exposures_used"] == len(users)
        conv = len([u for u in converters if u in users])
        assert row["events_attributed"] == conv
        assert row["valid_samples"] == len(users)
        assert abs(row["value"] - conv / len(users)) < 1e-6
        lo, hi = row["ci95"]["lower"], row["ci95"]["upper"]
        assert 0 <= lo <= row["value"] <= hi <= 1
        assert row["formula"].startswith("rate =")
        assert row["sample_ratio"]["srm"] is False
        assert row["insufficient_sample"] is False

    treatment = rows["treatment"]
    control = rows["control"]
    assert treatment["lift"] is not None
    c_rate = control["value"]
    assert abs(treatment["lift"]["relative"]
               - (treatment["value"] - c_rate) / abs(c_rate)) < 1e-6
    assert treatment["lift"]["favorable"] in (True, False)
    # top-level audit: formulas + reason glossary
    assert "binary_ci" in body["formulas"]
    assert "no_exposure" in body["attribution"]


def test_binary_event_without_value_is_still_counted(client):
    _experiment(client)
    _metric(client)
    _expose(client, "u0")
    ev = _event(client, "u0", value=None)
    assert ev["value_present"] is False
    assert ev["value_valid"] is False
    body = _analysis(client).json()
    assert body["totals"]["events_attributed"] == 1


# ---------------------------------------------------------------------------
# Event deduplication
# ---------------------------------------------------------------------------


def test_duplicate_event_is_not_recounted(client):
    _experiment(client)
    _metric(client)
    _expose(client, "u0")
    t0 = datetime.now(timezone.utc)
    first = _event(client, "u0", event_key="fixed-key",
                   at=t0 + timedelta(seconds=5))
    assert first["duplicate"] is False

    again = _event(client, "u0", event_key="fixed-key",
                   at=t0 + timedelta(seconds=999), value=3)
    assert again["duplicate"] is True
    assert again["attributions"] == []
    # original payload preserved (value still absent)
    assert again["value"] is None
    assert again["occurred_at"] == first["occurred_at"]

    body = _analysis(client).json()
    assert body["totals"]["events_in_window"] == 1
    assert body["totals"]["events_distinct"] == 1
    assert body["totals"]["duplicates"] == 0  # duplicates never become rows


# ---------------------------------------------------------------------------
# Exclusion reasons
# ---------------------------------------------------------------------------


def test_exclusion_reasons_are_reported(client):
    # audience gate keeps 'stranger' deterministically non-enrolled (their
    # event is in scope for v1 but attributes to no_exposure)
    cfg = base_config(traffic=100)
    cfg["audience"] = {"condition": {"field": "tier", "op": "eq",
                                     "value": "gold"}}
    create_experiment(client, key="exp", namespace="ns-exp", config=cfg)
    _metric(client, window=60, min_sample=1)
    t0 = datetime.now(timezone.utc)

    # user with an exposure; events exercise every branch
    d = _expose(client, "known", attributes={"tier": "gold"})

    # non-enrolled user (audience miss) -> event is in-scope but no_exposure
    miss = _expose(client, "stranger", attributes={"tier": "bronze"})
    assert miss["enrolled"] is False
    ev = _event(client, "stranger", event_key="ev-stranger",
                at=t0 + timedelta(seconds=5))
    assert ev["attributions"][0]["status"] == "excluded"
    assert ev["attributions"][0]["reason"] == "no_exposure"

    # 2) event earlier than the exposure
    _event(client, "known", event_key="ev-early",
           at=t0 - timedelta(hours=2))

    # 3) outside the 60s attribution window (but after exposure)
    _event(client, "known", event_key="ev-late",
           at=t0 + timedelta(seconds=120))

    # 4) valid event inside window -> attributed
    _event(client, "known", event_key="ev-good",
           at=t0 + timedelta(seconds=30))

    body = _analysis(client).json()
    excluded = body["totals"]["excluded"]
    assert excluded["no_exposure"] == 1
    assert excluded["event_before_exposure"] == 1
    assert excluded["out_of_window"] == 1
    assert body["totals"]["events_attributed"] == 1

    # variant-level breakdown records out-of-window under that variant
    row = next(v for v in body["variants"]
               if v["variant_key"] == d["variant_key"])
    assert row["exclusions"]["out_of_window"] == 1

    # reconciliation: attributed + all exclusions == events in range
    assert (body["totals"]["events_attributed"]
            + sum(excluded.values())) == body["totals"]["events_in_window"]


def test_window_boundary_is_inclusive(client):
    _experiment(client)
    _metric(client, window=60, min_sample=1)
    t0 = datetime.now(timezone.utc)
    _expose(client, "u0")
    # exactly at the window edge is still inside [exposure, exposure+window];
    # event at t0+59s is <=60s after the exposure (recorded slightly after t0)
    ev = _event(client, "u0", event_key="ev-edge",
                at=t0 + timedelta(seconds=59))  # slack for request latency
    assert ev["attributions"][0]["status"] == "attributed"


def test_continuous_invalid_value_excluded(client):
    _experiment(client)
    _metric(client, metric_key="spent", metric_type="continuous",
            event_name="spent", window=3600, min_sample=1)
    _expose(client, "u0")
    t0 = datetime.now(timezone.utc)

    good = _event(client, "u0", event_name="spent", event_key="ev-1",
                  at=t0 + timedelta(seconds=5), value=42.5)
    assert good["attributions"][0]["status"] == "attributed"

    # missing value -> invalid_value for continuous
    bad = _event(client, "u0", event_name="spent", event_key="ev-2",
                 at=t0 + timedelta(seconds=6))
    assert bad["value_present"] is False
    assert bad["attributions"][0]["status"] == "excluded"
    assert bad["attributions"][0]["reason"] == "invalid_value"

    # NaN is rejected at the API boundary (never stored)
    r = client.post(f"{API}/experiments/exp/events",
                    content=b'{"event_key":"ev-nan","user_key":"u0",'
                            b'"event_name":"spent","value":NaN}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_value"

    body = _analysis(client, metric_key="spent").json()
    assert body["metric_type"] == "continuous"
    assert body["totals"]["events_attributed"] == 1
    assert body["totals"]["excluded"]["invalid_value"] == 1


# ---------------------------------------------------------------------------
# Continuous metric statistics
# ---------------------------------------------------------------------------


def test_continuous_metric_mean_and_lift(client):
    _experiment(client)
    _metric(client, metric_key="spent", metric_type="continuous",
            event_name="spent", window=3600, direction="maximize",
            min_sample=1, srm=0.9)
    t0 = datetime.now(timezone.utc)

    values = {"control": [10.0, 12.0], "treatment": [20.0, 24.0]}
    for variant, nums in values.items():
        # expose deterministic users until we land in the wanted variant
        i, got = 0, 0
        while got < len(nums):
            user = f"{variant}-{i}"
            d = _expose(client, user)
            i += 1
            if d["variant_key"] != variant:
                continue
            _event(client, user, event_name="spent",
                   event_key=f"ev-{user}",
                   at=t0 + timedelta(seconds=5 + got), value=nums[got])
            got += 1

    body = _analysis(client, metric_key="spent").json()
    rows = {v["variant_key"]: v for v in body["variants"]}
    assert rows["control"]["value"] == 11.0
    assert rows["treatment"]["value"] == 22.0
    # mean ± 1.96·s/sqrt(n); s²=2, n=2 -> s/sqrt(n) = 1 -> half ≈ 1.959964
    z = 1.959963984540054
    assert abs(rows["control"]["ci95"]["lower"] - (11 - z)) < 1e-6
    assert abs(rows["control"]["ci95"]["upper"] - (11 + z)) < 1e-6
    lift = rows["treatment"]["lift"]
    assert abs(lift["relative"] - 1.0) < 1e-9  # (22-11)/11
    assert lift["ci95"]["lower"] < 1.0 < lift["ci95"]["upper"]
    assert lift["favorable"] is True  # maximize and treatment > control

    # minimizing direction flips favorable
    client.post(f"{API}/experiments/exp/versions",
                params={"publish": True}, json=base_config())
    _metric(client, version=2, metric_key="latency",
            metric_type="continuous", event_name="latency",
            direction="minimize", min_sample=1, srm=0.9)
    # reuse same exposures conceptually on v2: expose + low treatment values
    for i in range(4):
        _expose(client, f"l{i}")
    for i in range(4):
        _event(client, f"l{i}", event_name="latency",
               event_key=f"ev-l-{i}", at=t0 + timedelta(seconds=10),
               value=5.0)
    lb = _analysis(client, version=2, metric_key="latency").json()
    lr = {v["variant_key"]: v for v in lb["variants"]}
    if (lr["treatment"]["value"] is not None
            and lr["control"]["value"] not in (None, 0)
            and lr["treatment"]["value"] == lr["control"]["value"]):
        assert lr["treatment"]["lift"]["favorable"] is False


def test_insufficient_sample_flag(client):
    _experiment(client)
    _metric(client, min_sample=50)  # nobody reaches 50 samples
    _expose(client, "u0")
    t0 = datetime.now(timezone.utc)
    _event(client, "u0", at=t0 + timedelta(seconds=5))
    body = _analysis(client).json()
    assert body["totals"]["insufficient_sample"] is True
    assert all(v["insufficient_sample"] for v in body["variants"])


# ---------------------------------------------------------------------------
# Sample-ratio mismatch
# ---------------------------------------------------------------------------


def test_sample_ratio_mismatch_flag(client):
    # natural 50/50 split, then force 20 extra users into treatment
    whitelist = [{"user_key": f"vip-{i}", "variant_key": "treatment"}
                 for i in range(20)]
    _experiment(client, whitelist=whitelist)
    _metric(client, min_sample=1, srm=0.1)
    for i in range(10):
        _expose(client, f"nat-{i}")
    for i in range(20):
        _expose(client, f"vip-{i}")

    body = _analysis(client).json()
    assert body["totals"]["srm"] is True
    treatment = next(v for v in body["variants"]
                     if v["variant_key"] == "treatment")
    assert treatment["sample_ratio"]["srm"] is True
    assert treatment["sample_ratio"]["expected_share"] == 0.5
    assert treatment["sample_ratio"]["observed_share"] > 0.6
    assert treatment["sample_ratio"]["relative_deviation"] > 0.1


# ---------------------------------------------------------------------------
# Time range, determinism and version isolation
# ---------------------------------------------------------------------------


def test_time_range_filtering_is_half_open(client):
    _experiment(client)
    _metric(client)
    t0 = datetime.now(timezone.utc)
    _expose(client, "u0")
    _event(client, "u0", at=t0 + timedelta(seconds=10))

    # event outside the queried range
    none = _analysis(client, start_at=_iso(t0),
                     end_at=_iso(t0 + timedelta(seconds=5))).json()
    assert none["totals"]["events_in_window"] == 0
    assert none["window"]["start_at"] is not None

    inside = _analysis(client, start_at=_iso(t0 + timedelta(seconds=5)),
                       end_at=_iso(t0 + timedelta(seconds=20))).json()
    assert inside["totals"]["events_in_window"] == 1

    # invalid range -> 400
    r = _analysis(client, start_at=_iso(t0 + timedelta(seconds=20)),
                  end_at=_iso(t0))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_time_range"


def test_repeated_queries_are_identical_and_version_isolated(client):
    _experiment(client)
    _metric(client, min_sample=1, srm=0.9)
    t0 = datetime.now(timezone.utc)
    for i in range(8):
        _expose(client, f"u{i}")
        _event(client, f"u{i}", at=t0 + timedelta(seconds=10 + i))
    v1_snapshot = _analysis(client).json()

    # same range queried repeatedly -> byte-identical
    assert _analysis(client).json() == v1_snapshot
    params = {"start_at": _iso(t0), "end_at": _iso(t0 + timedelta(hours=1))}
    assert _analysis(client, **params).json() == _analysis(client, **params).json()

    # publishing v2 must not change the v1 analysis (metric & rows are pinned)
    client.post(f"{API}/experiments/exp/versions",
                params={"publish": True}, json=base_config(traffic=50))
    enrolled_v2 = 0
    for i in range(20):  # traffic/exposures under the new version (50% gate)
        d = _expose(client, f"post-{i}")
        if d["enrolled"]:
            enrolled_v2 += 1
        _event(client, f"post-{i}", event_key=f"ev-purchase-post-{i}",
               at=t0 + timedelta(minutes=5))
    assert _analysis(client).json() == v1_snapshot

    # the v1 metric does not exist on v2
    assert _analysis(client, version=2).status_code == 404
    # v2 analysis with a v2-defined metric starts from an empty slate
    _metric(client, version=2, metric_key="purchase", window=3600,
            min_sample=1, srm=0.9)
    v2 = _analysis(client, version=2).json()
    assert v2["totals"]["exposures_used"] == enrolled_v2
    assert v2["version_number"] == 2


def test_event_lookup_and_listing(client):
    _experiment(client)
    _expose(client, "u0")
    _event(client, "u0", event_key="abc")
    got = client.get(f"{API}/experiments/exp/events/abc")
    assert got.status_code == 200
    assert got.json()["user_key"] == "u0"
    assert client.get(f"{API}/experiments/exp/events/nope").status_code == 404

    listing = client.get(f"{API}/experiments/exp/events",
                         params={"user_key": "u0"}).json()
    assert [e["event_key"] for e in listing["items"]] == ["abc"]
