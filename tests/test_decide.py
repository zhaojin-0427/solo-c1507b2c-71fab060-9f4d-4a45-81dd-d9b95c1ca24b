"""Decision engine behavior: stability, whitelist, audience, schedules, trace."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from .test_lifecycle import base_config, create_experiment


def decide(client: TestClient, key: str = "exp", **payload_over):
    payload = {"user_key": "user-1"}
    payload.update(payload_over)
    return client.post(f"/api/experiments/{key}/decide", json=payload)


def test_basic_enrollment_and_full_trace(client):
    create_experiment(client, config=base_config())
    r = decide(client, user_key="alice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolled"] is True
    assert body["variant_key"] in {"control", "treatment"}
    assert body["reason"] == "bucket"
    steps = [s["step"] for s in body["trace"]]
    assert steps == ["version_resolved", "bucket", "schedule", "whitelist",
                     "audience", "traffic", "variant_assignment"]
    bucket_step = next(s for s in body["trace"] if s["step"] == "bucket")
    assert 0 <= bucket_step["detail"]["bucket"] < 10000
    assert "formula" in bucket_step["detail"]


def test_decision_is_stable_for_same_version(client):
    create_experiment(client, config=base_config())
    first = decide(client, user_key="bob").json()
    for _ in range(5):
        again = decide(client, user_key="bob").json()
        assert again["bucket"] == first["bucket"]
        assert again["variant_key"] == first["variant_key"]
        assert again["version_number"] == 1


def test_new_version_reshuffles_but_remains_deterministic(client):
    create_experiment(client, config=base_config(traffic=100))
    v1 = {decide(client, user_key=f"u-{i}").json()["bucket"] for i in range(20)}

    r = client.post("/api/experiments/exp/versions",
                    params={"publish": True}, json=base_config(traffic=50))
    assert r.status_code == 201

    v2_buckets = {decide(client, user_key=f"u-{i}").json()["bucket"] for i in range(20)}
    # version number is part of the hash material => reshuffle expected
    assert v1 != v2_buckets

    # pinning v1 reproduces the original decision exactly
    pinned = decide(client, user_key="u-1", version=1).json()
    original = decide(client, user_key="u-1", version=1).json()
    assert pinned["version_number"] == 1
    assert pinned["bucket"] == original["bucket"]


def test_distinct_salts_decorrelate_experiments(client):
    create_experiment(client, key="a", namespace="na", config=base_config())
    create_experiment(client, key="b", namespace="nb", config=base_config())
    buckets = {decide(client, k, user_key="same-user").json()["bucket"]
               for k in ("a", "b")}
    assert len(buckets) == 2  # different salts -> different bucket with high certainty


def test_whitelist_overrides_traffic_and_audience(client):
    cfg = base_config(traffic=0)  # nobody admitted organically
    cfg["whitelist"] = [{"user_key": "vip", "variant_key": "treatment"}]
    cfg["audience"] = {"condition": {"field": "tier", "op": "eq", "value": "gold"}}
    create_experiment(client, config=cfg)

    body = decide(client, user_key="vip", attributes={"tier": "bronze"}).json()
    assert body["enrolled"] is True
    assert body["variant_key"] == "treatment"
    assert body["reason"] == "whitelist"
    # trace shows the organic bucket that was overridden
    wl = next(s for s in body["trace"] if s["step"] == "whitelist")
    assert wl["result"] == "matched"
    assert "bucketed_variant" in wl["detail"]

    # regular user is rejected by traffic (and audience would also fail)
    other = decide(client, user_key="norm", attributes={"tier": "bronze"}).json()
    assert other["enrolled"] is False
    assert other["reason"] == "audience_mismatch"


def test_audience_mismatch_reports_tree_in_trace(client):
    cfg = base_config()
    cfg["audience"] = {
        "all": [
            {"condition": {"field": "country", "op": "in", "value": ["CN", "SG"]}},
            {"any": [
                {"condition": {"field": "age", "op": "gte", "value": 18}},
                {"condition": {"field": "beta", "op": "eq", "value": True}},
            ]},
        ]
    }
    create_experiment(client, config=cfg)

    body = decide(client, user_key="u",
                  attributes={"country": "US", "age": 30}).json()
    assert body["reason"] == "audience_mismatch"
    aud = next(s for s in body["trace"] if s["step"] == "audience")
    assert aud["result"] == "mismatch"
    tree = aud["detail"]["tree"]
    assert tree["type"] == "all" and tree["matched"] is False
    assert tree["children"][0]["matched"] is False

    # matching audience passes
    ok = decide(client, user_key="u2",
                attributes={"country": "CN", "age": 20}).json()
    assert ok["enrolled"] is True


def test_traffic_gate_miss_reason(client):
    create_experiment(client, config=base_config(traffic=10))
    # deterministic: find at least one rejected user across a sample
    reasons = {decide(client, user_key=f"scout-{i}").json()["reason"]
               for i in range(50)}
    assert "not_in_traffic" in reasons


def test_schedule_window_active_and_inactive(client):
    now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    cfg = base_config()
    cfg["schedules"] = [
        {"start_at": (now - timedelta(days=1)).isoformat(),
         "end_at": (now + timedelta(days=1)).isoformat()},
    ]
    create_experiment(client, config=cfg)
    ok = decide(client, user_key="u", at=now.isoformat()).json()
    assert ok["enrolled"] is True

    past = decide(client, user_key="u",
                  at=(now - timedelta(days=3)).isoformat()).json()
    assert past["enrolled"] is False
    assert past["reason"] == "not_in_schedule"


def test_pinned_draft_version_is_refused(client):
    create_experiment(client, config=base_config(), publish=False)
    r = decide(client, user_key="u", version=1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_not_published"
