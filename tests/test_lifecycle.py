"""End-to-end tests: lifecycle, immutability, validation rules."""

from fastapi.testclient import TestClient


def base_config(traffic: float = 100.0, **kw):
    cfg = {
        "traffic_percentage": traffic,
        "variants": [
            {"key": "control", "percentage": 50, "is_control": True},
            {"key": "treatment", "percentage": 50},
        ],
        "control_variant_key": "control",
        "whitelist": [],
    }
    cfg.update(kw)
    return cfg


def create_experiment(client: TestClient, key: str = "exp",
                      namespace: str | None = "ns", config=None,
                      publish: bool = True):
    payload = {"key": key, "name": f"Experiment {key}"}
    if namespace is not None:
        payload["namespace"] = namespace
    if config is not None:
        payload["config"] = config
        payload["publish"] = publish
    r = client.post("/api/experiments", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Lifecycle / versioning
# ---------------------------------------------------------------------------


def test_create_and_list_experiment(client):
    exp = create_experiment(client, config=base_config())
    assert exp["published_version"] == 1
    assert exp["namespace"] == "ns"
    assert exp["salt"]

    r = client.get("/api/experiments")
    assert r.status_code == 200
    assert any(e["key"] == "exp" for e in r.json())


def test_draft_then_publish_creates_immutable_history(client):
    create_experiment(client, config=base_config(), publish=False)
    # draft is not used for decisions
    r = client.post("/api/experiments/exp/decide",
                    json={"user_key": "u1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"

    r = client.post("/api/experiments/exp/versions/1/publish")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"

    # publishing again is rejected
    r = client.post("/api/experiments/exp/versions/1/publish")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_already_published"

    # a second version is new immutable history
    r = client.post("/api/experiments/exp/versions",
                    params={"publish": True},
                    json=base_config(traffic=25))
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2

    versions = client.get("/api/experiments/exp/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["config"]["traffic_percentage"] == 100


def test_published_version_cannot_be_mutated(client):
    create_experiment(client, config=base_config())
    # Direct DB-level attempt to rewrite a published config must be blocked
    # by the immutability trigger.
    import sqlite3
    import pytest

    from app.db import get_conn
    conn = get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE experiment_versions SET config_json = '{}' WHERE version = 1")
    conn.rollback()


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


def test_rejects_percentages_not_summing_to_100(client):
    create_experiment(client)
    r = client.post("/api/experiments/exp/versions",
                    json=base_config(traffic=100, variants=[
                        {"key": "control", "percentage": 60, "is_control": True},
                        {"key": "treatment", "percentage": 30},
                    ]))
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["error"]["details"]["issues"]]
    assert "percentages_sum" in codes


def test_rejects_missing_control_and_bad_whitelist(client):
    create_experiment(client)
    cfg = base_config()
    cfg["control_variant_key"] = "ghost"
    cfg["whitelist"] = [{"user_key": "vip-1", "variant_key": "ghost"}]
    r = client.post("/api/experiments/exp/versions", json=cfg)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["details"]["issues"]}
    assert {"unknown_control", "unknown_whitelist_variant"} <= codes


def test_schedule_overlap_within_one_config_rejected(client):
    create_experiment(client)
    cfg = base_config()
    cfg["schedules"] = [
        {"start_at": "2026-09-10T00:00:00Z", "end_at": "2026-09-12T00:00:00Z"},
        {"start_at": "2026-09-11T00:00:00Z", "end_at": "2026-09-13T00:00:00Z"},
    ]
    r = client.post("/api/experiments/exp/versions", json=cfg)
    assert r.status_code == 422
    assert any(i["code"] == "schedule_overlap"
               for i in r.json()["error"]["details"]["issues"])


def test_mutex_namespace_traffic_conflict_rejected(client):
    # exp-a occupies 60% forever in namespace "shared"
    create_experiment(client, key="exp-a", namespace="shared",
                      config=base_config(traffic=60))

    # 50% more in the same always-on namespace exceeds 100%
    r = client.post("/api/experiments", json={
        "key": "exp-b", "name": "B", "namespace": "shared",
        "config": base_config(traffic=50), "publish": True,
    })
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["error"]["details"]["issues"]]
    assert "namespace_traffic_conflict" in codes


def test_mutex_namespace_non_overlapping_windows_allowed(client):
    create_experiment(
        client, key="exp-a", namespace="shared",
        config=base_config(traffic=90, schedules=[
            {"start_at": "2026-09-01T00:00:00Z",
             "end_at": "2026-09-10T00:00:00Z"},
        ]))
    r = client.post("/api/experiments", json={
        "key": "exp-b", "name": "B", "namespace": "shared",
        "publish": True,
        "config": base_config(traffic=90, schedules=[
            {"start_at": "2026-09-20T00:00:00Z",
             "end_at": "2026-09-30T00:00:00Z"},
        ]),
    })
    assert r.status_code == 201, r.text


def test_preflight_does_not_store_anything(client):
    create_experiment(client)
    r = client.post("/api/experiments/exp/preflight", json=base_config(traffic=10))
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert client.get("/api/experiments/exp/versions").json() == []
