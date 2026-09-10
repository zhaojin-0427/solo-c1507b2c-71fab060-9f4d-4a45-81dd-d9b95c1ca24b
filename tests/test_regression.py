"""Regression tests for the reviewed defects:

1. publish-time mutex must bound TOTAL namespace occupancy (three 40%
   overlapping experiments cannot all publish);
2. decision-time mutex: one user enters at most one experiment per
   namespace, and only the admitted experiment enrolls them;
3. invalid schedule windows produce structured 422 issues, never a 500;
4. a 422 on experiment creation leaves no orphan metadata and the same key
   can be retried;
5. idempotent replay returns the first persisted decision verbatim even
   after a new version is published.
"""

from fastapi.testclient import TestClient

from .test_lifecycle import base_config, create_experiment


def one_variant(traffic: float, **kw):
    return base_config(traffic=traffic, variants=[
        {"key": "control", "percentage": 100, "is_control": True},
    ], **kw)


# ---------------------------------------------------------------------------
# 1. publish-time total occupancy
# ---------------------------------------------------------------------------


def test_three_40pct_overlapping_experiments_cannot_all_publish(client):
    for i, key in enumerate(["a", "b"]):
        r = client.post("/api/experiments", json={
            "key": key, "name": key, "namespace": "shared-three",
            "publish": True, "config": one_variant(40),
        })
        assert r.status_code == 201, r.text

    # third 40% experiment: pair sums (80%) are fine, but the total is 120%
    r = client.post("/api/experiments", json={
        "key": "c", "name": "c", "namespace": "shared-three",
        "publish": True, "config": one_variant(40),
    })
    assert r.status_code == 422, r.text
    codes = [i["code"] for i in r.json()["error"]["details"]["issues"]]
    assert "namespace_traffic_conflict" in codes
    # the message must state the peak total occupancy, not a pairwise sum
    msg = r.json()["error"]["message"]
    assert "120%" in msg


def test_publishing_third_experiment_draft_is_allowed_but_publish_blocked(client):
    for key in ("a", "b"):
        create_experiment(client, key=key, namespace="ns",
                          config=one_variant(40))
    # drafting never participates in mutex checks
    r = client.post("/api/experiments", json={
        "key": "c", "name": "c", "namespace": "ns",
        "publish": False, "config": one_variant(40),
    })
    assert r.status_code == 201
    r2 = client.post("/api/experiments/c/versions/1/publish")
    assert r2.status_code == 422
    assert any(i["code"] == "namespace_traffic_conflict"
               for i in r2.json()["error"]["details"]["issues"])


def test_three_40pct_experiments_ok_in_disjoint_time_windows(client):
    windows = [
        ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        ("2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"),
        ("2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z"),
    ]
    for i, (key, (s, e)) in enumerate(zip(("a", "b", "c"), windows)):
        r = client.post("/api/experiments", json={
            "key": key, "name": key, "namespace": "ns",
            "publish": True,
            "config": one_variant(40, schedules=[{"start_at": s, "end_at": e}]),
        })
        assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# 2. decision-time namespace mutual exclusion
# ---------------------------------------------------------------------------


def test_user_enters_at_most_one_experiment_per_namespace(client):
    # Publish two 50% experiments sharing one namespace. Pairwise the limit
    # is exactly 100%, but independent per-experiment gates would let one
    # user into both — the namespace ring must prevent it.
    for key in ("one", "two"):
        create_experiment(client, key=key, namespace="core",
                          config=one_variant(50))

    # Across a large user sample, every user is enrolled in at most one of
    # the two experiments, and each experiment admits roughly its share.
    enrolled_one = enrolled_two = both = neither = 0
    for i in range(3000):
        r = client.post("/api/experiments/decide/batch", json={
            "user_key": f"u-{i}", "experiment_keys": ["one", "two"]})
        results = {x["experiment_key"]: x["decision"]["enrolled"]
                   for x in r.json()["results"]}
        if results["one"]:
            enrolled_one += 1
        if results["two"]:
            enrolled_two += 1
        if results["one"] and results["two"]:
            both += 1
        if not results["one"] and not results["two"]:
            neither += 1
    assert both == 0
    assert 0.45 < enrolled_one / 3000 < 0.55
    assert 0.45 < enrolled_two / 3000 < 0.55
    assert neither == 0  # the two 50% slices partition the full ring


def test_mutex_excluded_trace_names_winner(client):
    for key in ("one", "two"):
        create_experiment(client, key=key, namespace="core",
                          config=one_variant(50))
    # find a user whose ring position belongs to "two"
    for i in range(50):
        user = f"probe-{i}"
        r = client.post("/api/experiments/one/decide", json={"user_key": user})
        d = r.json()
        if d["reason"] == "mutex_excluded":
            traffic = next(s for s in d["trace"] if s["step"] == "traffic")
            assert traffic["detail"]["winner"]["experiment_key"] == "two"
            ring = next(s for s in d["trace"] if s["step"] == "mutex_ring")
            slice_keys = {s["experiment_key"] for s in ring["detail"]["slices"]}
            assert slice_keys == {"one", "two"}
            # and that user IS enrolled in "two"
            r2 = client.post("/api/experiments/two/decide", json={"user_key": user})
            assert r2.json()["enrolled"] is True
            assert r2.json()["reason"] == "bucket"
            return
    raise AssertionError("expected to find a mutex_excluded user in sample")


def test_mutex_does_not_cross_namespaces(client):
    create_experiment(client, key="a", namespace="ns-a", config=one_variant(100))
    create_experiment(client, key="b", namespace="ns-b", config=one_variant(100))
    r = client.post("/api/experiments/decide/batch", json={
        "user_key": "mutex-4", "experiment_keys": ["a", "b"]})
    results = r.json()["results"]
    assert all(x["decision"]["enrolled"] is True for x in results)


def test_decision_is_stable_under_repeated_and_reordered_calls(client):
    for key in ("one", "two", "three"):
        create_experiment(client, key=key, namespace="core",
                          config=one_variant(33))
    first = {}
    for i in range(100):
        user = f"stable-{i}"
        r1 = client.post("/api/experiments/decide/batch", json={
            "user_key": user, "experiment_keys": ["one", "two", "three"]})
        r2 = client.post("/api/experiments/decide/batch", json={
            "user_key": user, "experiment_keys": ["three", "one", "two"]})
        a = [(x["experiment_key"], x["decision"]["enrolled"],
              x["decision"]["variant_key"]) for x in r1.json()["results"]]
        b = [(x["experiment_key"], x["decision"]["enrolled"],
              x["decision"]["variant_key"]) for x in r2.json()["results"]]
        assert sorted(a) == sorted(b)  # order-independent
        # at most one enrollment in the namespace
        assert sum(enrolled for _, enrolled, _ in a) <= 1


# ---------------------------------------------------------------------------
# 3. invalid schedule window -> structured 422, never 500
# ---------------------------------------------------------------------------


def test_preflight_inverted_window_returns_structured_422(client):
    create_experiment(client)
    r = client.post("/api/experiments/exp/preflight", json={
        **one_variant(100),
        "schedules": [{"start_at": "2026-09-12T00:00:00Z",
                       "end_at": "2026-09-10T00:00:00Z"}],
    })
    # preflight never stores anything, but invalid configs are rejected
    # with a structured 422 (previously this request crashed into a 500).
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["valid"] is False
    codes = [i["code"] for i in body["issues"]]
    assert "schedule_order" in codes
    assert client.get("/api/experiments/exp/versions").json() == []


def test_preflight_valid_config_returns_200(client):
    create_experiment(client)
    r = client.post("/api/experiments/exp/preflight", json=one_variant(10))
    assert r.status_code == 200
    assert r.json()["valid"] is True


def test_create_with_inverted_window_returns_structured_422(client):
    r = client.post("/api/experiments", json={
        "key": "badwin", "name": "badwin", "publish": True,
        "config": {**one_variant(100),
                   "schedules": [{"start_at": "2026-09-12T00:00:00Z",
                                  "end_at": "2026-09-10T00:00:00Z"}]},
    })
    assert r.status_code == 422
    assert any(i["code"] == "schedule_order"
               for i in r.json()["error"]["details"]["issues"])


def test_malformed_json_field_returns_400_not_500(client):
    # Defensive: the request-validation handler itself must serialize
    # (pydantic errors can embed raw ValueError objects in ctx).
    r = client.post("/api/experiments/exp/preflight", json={"unexpected": 1})
    assert r.status_code in (400, 404)  # 400 validation here, 404 if exp missing
    create_experiment(client)
    r2 = client.post("/api/experiments/exp/preflight", json={"unexpected": 1})
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# 4. rejected create leaves no orphan metadata; same key is retryable
# ---------------------------------------------------------------------------


def test_rejected_create_is_fully_retryable_with_same_key(client):
    bad = {
        "key": "retry-exp", "name": "retry", "namespace": "ns",
        "publish": True,
        "config": {**one_variant(100), "variants": [
            {"key": "control", "percentage": 70, "is_control": True}]},
    }
    r = client.post("/api/experiments", json=bad)
    assert r.status_code == 422

    # no orphan metadata anywhere
    assert client.get("/api/experiments/retry-exp").status_code == 404
    assert all(e["key"] != "retry-exp" for e in client.get("/api/experiments").json())

    # fix the payload and retry with the exact same key
    bad["config"] = one_variant(100)
    r2 = client.post("/api/experiments", json=bad)
    assert r2.status_code == 201, r2.text
    assert r2.json()["published_version"] == 1


def test_rejected_publish_create_does_not_block_namespace_other_things(client):
    # A create that fails mutex validation must not have consumed capacity
    # in the namespace (no phantom reservation).
    create_experiment(client, key="a", namespace="ns", config=one_variant(80))
    r = client.post("/api/experiments", json={
        "key": "b", "name": "b", "namespace": "ns",
        "publish": True, "config": one_variant(40),
    })
    assert r.status_code == 422
    r2 = client.post("/api/experiments", json={
        "key": "b", "name": "b", "namespace": "ns",
        "publish": True, "config": one_variant(20),
    })
    assert r2.status_code == 201


# ---------------------------------------------------------------------------
# 5. idempotent replay returns the first persisted decision
# ---------------------------------------------------------------------------


def test_idempotent_replay_returns_first_persisted_decision(client):
    cfg = base_config(traffic=100, whitelist=[
        {"user_key": "vip", "variant_key": "treatment"}], audience={
            "condition": {"field": "tier", "op": "eq", "value": "gold"}})
    create_experiment(client, key="idm", namespace="idm-ns", config=cfg)

    first = client.post("/api/experiments/idm/decide", json={
        "user_key": "vip", "attributes": {"tier": "gold"},
        "record_exposure": True, "idempotency_key": "k-fix"}).json()
    assert first["version_number"] == 1
    assert first["reason"] == "whitelist"
    assert first["variant_key"] == "treatment"
    assert first["exposure_recorded"] is True

    # publish a v2 that removes the whitelist
    client.post("/api/experiments/idm/versions",
                params={"publish": True},
                json=base_config(traffic=100, audience={
                    "condition": {"field": "tier", "op": "eq", "value": "gold"}}))

    # replay the SAME key with contradictory attributes and unpinned version
    replay = client.post("/api/experiments/idm/decide", json={
        "user_key": "vip", "attributes": {"tier": "bronze"},
        "record_exposure": True, "idempotency_key": "k-fix"}).json()
    assert replay["exposure_recorded"] is False
    assert replay["version_number"] == 1
    assert replay["reason"] == "whitelist"
    assert replay["variant_key"] == "treatment"
    assert replay["trace"] == first["trace"]

    # and the lookup endpoint agrees (response == database)
    stored = client.get("/api/exposures/idempotency/k-fix").json()
    assert stored["version_number"] == 1
    assert stored["reason"] == "whitelist"
    assert stored["variant_key"] == "treatment"


def test_idempotent_replay_is_independent_of_request_attributes(client):
    cfg = base_config(traffic=100, audience={
        "condition": {"field": "country", "op": "eq", "value": "CN"}})
    create_experiment(client, key="geo", namespace="geo-ns", config=cfg)
    first = client.post("/api/experiments/geo/decide", json={
        "user_key": "u1", "attributes": {"country": "CN"},
        "record_exposure": True, "idempotency_key": "geo-key"}).json()
    assert first["enrolled"] is True
    replay = client.post("/api/experiments/geo/decide", json={
        "user_key": "u1", "attributes": {"country": "US"},
        "record_exposure": True, "idempotency_key": "geo-key"}).json()
    assert replay["enrolled"] is True
    assert replay["reason"] == first["reason"]
    assert replay["variant_key"] == first["variant_key"]
    assert replay["version_number"] == first["version_number"]
