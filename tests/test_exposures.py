"""Batch decisions, simulation distribution, exposure idempotency & traceability."""

from .test_lifecycle import base_config, create_experiment


def _exp_with(client, key="exp", namespace="ns", traffic=100, splits=(50, 50)):
    cfg = base_config(traffic=traffic, variants=[
        {"key": "control", "percentage": splits[0], "is_control": True},
        {"key": "treatment", "percentage": splits[1]},
    ])
    create_experiment(client, key=key, namespace=namespace, config=cfg)


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_batch_decide_isolates_per_experiment_errors(client):
    _exp_with(client, key="ok")
    r = client.post("/api/experiments/decide/batch", json={
        "user_key": "bob",
        "experiment_keys": ["ok", "missing", "ok"],
        "attributes": {},
    })
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["experiment_key"] for x in results] == ["ok", "missing", "ok"]
    assert results[0]["decision"]["enrolled"] is True
    assert results[1]["error_code"] == "not_found"
    assert results[2]["decision"]["bucket"] == results[0]["decision"]["bucket"]


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def test_simulate_reports_distribution_close_to_configured(client):
    _exp_with(client, splits=(30, 70))
    r = client.post("/api/experiments/exp/simulate", json={"users": 20000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["users"] == 20000
    assert body["enrolled"] == 20000
    by = {row["variant_key"]: row for row in body["variants"]}
    assert abs(by["control"]["actual_percentage"] - 30) < 2.0
    assert abs(by["treatment"]["actual_percentage"] - 70) < 2.0


def test_simulate_counts_miss_reasons(client):
    cfg = base_config(traffic=50)
    cfg["audience"] = {"condition": {"field": "tier", "op": "eq", "value": "gold"}}
    create_experiment(client, config=cfg)
    r = client.post("/api/experiments/exp/simulate", json={
        "users": 1000,
        "attributes": {"tier": "bronze"},
    })
    body = r.json()
    assert body["enrolled"] == 0
    assert body["miss_reasons"] == {"audience_mismatch": 1000}


def test_simulate_partial_traffic_keeps_enrolled_split_uniform(client):
    # 50% traffic gate with 50/50 variants: half miss the gate, the enrolled
    # half must still split ~50/50 between the two variants.
    _exp_with(client, traffic=50, splits=(50, 50))
    body = client.post("/api/experiments/exp/simulate",
                       json={"users": 40000}).json()
    assert 48 < body["enrolled_percentage"] < 52
    assert body["miss_reasons"] == {"not_in_traffic": body["not_enrolled"]}
    by = {r["variant_key"]: r for r in body["variants"]}
    for row in by.values():
        assert 45 < row["actual_percentage"] < 55
        assert 20 < row["overall_percentage"] < 30


# ---------------------------------------------------------------------------
# Exposures: idempotency, summary, traceability
# ---------------------------------------------------------------------------


def test_exposure_idempotency_and_summary(client):
    _exp_with(client, splits=(50, 50))
    payload = {"user_key": "alice", "record_exposure": True,
               "idempotency_key": "job-20260910-alice"}
    r1 = client.post("/api/experiments/exp/decide", json=payload)
    assert r1.status_code == 200
    assert r1.json()["exposure_recorded"] is True

    # same idempotency key -> existing record, same decision, not re-inserted
    r2 = client.post("/api/experiments/exp/decide", json=payload)
    assert r2.json()["exposure_recorded"] is False
    assert r2.json()["version_id"] == r1.json()["version_id"]
    assert r2.json()["variant_key"] == r1.json()["variant_key"]

    # lookup by idempotency key exposes the full trace + version provenance
    lookup = client.get(
        "/api/exposures/idempotency/job-20260910-alice").json()
    assert lookup["user_key"] == "alice"
    assert lookup["version_number"] == 1
    assert lookup["trace"][0]["step"] == "version_resolved"

    # other users add counts; misses are recorded too
    for i in range(20):
        client.post("/api/experiments/exp/decide", json={
            "user_key": f"crowd-{i}", "record_exposure": True})
    summary = client.get("/api/experiments/exp/exposures/summary").json()
    assert summary["total_decisions"] == 21  # 20 + deduped alice
    assert summary["enrolled"] + summary["not_enrolled"] == 21
    assert sum(v["count"] for v in summary["by_variant"]) == summary["enrolled"]
    assert "bucket" in summary["by_reason"]

    # variant-filtered exposure browse
    v = client.get("/api/experiments/exp/exposures",
                   params={"variant": "treatment", "limit": 5}).json()
    assert len(v["items"]) <= 5
    assert all(x["variant_key"] == "treatment" for x in v["items"])


def test_exposure_summary_can_be_filtered_by_version(client):
    _exp_with(client, splits=(50, 50))
    client.post("/api/experiments/exp/decide", json={
        "user_key": "u1", "record_exposure": True})
    client.post("/api/experiments/exp/versions",
                params={"publish": True}, json=base_config(traffic=100))
    client.post("/api/experiments/exp/decide", json={
        "user_key": "u1", "record_exposure": True})

    all_rows = client.get("/api/experiments/exp/exposures/summary").json()
    v1 = client.get("/api/experiments/exp/exposures/summary",
                    params={"version": 1}).json()
    assert all_rows["total_decisions"] == 2
    assert v1["total_decisions"] == 1
    assert v1["version"] == 1


def test_miss_decisions_are_recorded_with_reason(client):
    cfg = base_config(traffic=0)
    create_experiment(client, config=cfg)
    client.post("/api/experiments/exp/decide", json={
        "user_key": "locked-out", "record_exposure": True})
    summary = client.get("/api/experiments/exp/exposures/summary").json()
    assert summary["not_enrolled"] == 1
    assert summary["by_reason"].get("not_in_traffic") == 1
