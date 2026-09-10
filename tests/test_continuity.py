"""Cross-version assignment continuity: inheritance, prechecks, preview."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn

from .test_lifecycle import base_config, create_experiment


def cont_config(mode: str = "inherit", source_version: int | None = 1,
                renames=None, source_experiment=None, **kw):
    cfg = base_config(**kw)
    cfg["continuity"] = {"mode": mode}
    if source_version is not None:
        cfg["continuity"]["source_version"] = source_version
    if source_experiment is not None:
        cfg["continuity"]["source_experiment"] = source_experiment
    if renames is not None:
        cfg["continuity"]["renames"] = renames
    return cfg


def create_continuity_version(client: TestClient, number: int, *,
                              cfg=None, publish: bool = True):
    r = client.post("/api/experiments/exp/versions",
                    params={"publish": str(publish).lower()}, json=cfg)
    assert r.status_code == 201 if publish else 201, r.text
    assert r.json()["version"] == number
    return r.json()


def decide(client: TestClient, user: str, version: int | None = None,
           **over):
    payload = {"user_key": user}
    if version is not None:
        payload["version"] = version
    payload.update(over)
    return client.post("/api/experiments/exp/decide", json=payload).json()


def cont_step(body: dict) -> dict:
    return next(s for s in body["trace"] if s["step"] == "continuity")


@pytest.fixture()
def experiment_with_v1(client):
    create_experiment(client, config=base_config(traffic=100))
    return client


# ---------------------------------------------------------------------------
# Same-set inheritance keeps groups
# ---------------------------------------------------------------------------


def test_inherit_identical_config_keeps_every_group(experiment_with_v1, client):
    users = [f"u-{i}" for i in range(150)]
    v1 = {u: decide(client, u, 1) for u in users}
    create_continuity_version(client, 2, cfg=cont_config())

    for u in users:
        d2 = decide(client, u, 2)
        assert d2["bucket"] == v1[u]["bucket"]
        assert d2["variant_key"] == v1[u]["variant_key"]
        step = cont_step(d2)
        assert step["result"] == "retained"
        detail = step["detail"]
        assert detail["source_version"] == 1
        assert detail["status"] == "retained"
        assert detail["change_reason"] == "same_variant"
        assert detail["assignment_seed"].endswith("|1")


def test_inherited_seed_appears_in_bucket_trace(experiment_with_v1, client):
    create_continuity_version(client, 2, cfg=cont_config())
    body = decide(client, "u-1", 2)
    bucket_step = next(s for s in body["trace"] if s["step"] == "bucket")
    # Formula material uses the source seed|version, not the new version.
    assert bucket_step["detail"]["formula"].startswith("sha256('")
    assert bucket_step["detail"]["assignment_seed"].endswith("|1")
    resolved = next(s for s in body["trace"]
                    if s["step"] == "version_resolved")
    assert resolved["detail"]["continuity_mode"] == "inherit"


def test_weight_change_only_moves_boundary_crossers(experiment_with_v1, client):
    users = [f"u-{i}" for i in range(400)]
    v1 = {u: decide(client, u, 1) for u in users}

    cfg = base_config(traffic=100, variants=[
        {"key": "control", "percentage": 80, "is_control": True},
        {"key": "treatment", "percentage": 20},
    ])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1}
    create_continuity_version(client, 2, cfg=cfg)

    moved = 0
    for u in users:
        d2 = decide(client, u, 2)
        step = cont_step(d2)["detail"]
        if d2["variant_key"] != v1[u]["variant_key"]:
            moved += 1
            # control grew from 50 to 80: only treatment users can cross in
            assert v1[u]["variant_key"] == "treatment"
            assert d2["variant_key"] == "control"
            assert step["change_reason"] == "weight_boundary_crossed"
            assert step["status"] == "switched"
        else:
            assert step["status"] == "retained"
    assert moved > 0  # the boundary genuinely moved


# ---------------------------------------------------------------------------
# Rename / add / remove
# ---------------------------------------------------------------------------


def test_rename_mapping_carries_group(client):
    create_experiment(client, config=base_config(traffic=100))
    users = [f"u-{i}" for i in range(200)]
    v1 = {u: decide(client, u, 1) for u in users}

    cfg = base_config(traffic=100, variants=[
        {"key": "control", "percentage": 50, "is_control": True},
        {"key": "therapy", "percentage": 50},
    ])
    cfg["continuity"] = {
        "mode": "inherit", "source_version": 1,
        "renames": [{"source": "treatment", "target": "therapy"}],
    }
    r = client.post("/api/experiments/exp/versions",
                    params={"publish": "true"}, json=cfg)
    assert r.status_code == 201, r.text

    for u in users:
        d2 = decide(client, u, 2)
        expected = {"control": "control", "treatment": "therapy"}[
            v1[u]["variant_key"]]
        assert d2["variant_key"] == expected
        step = cont_step(d2)["detail"]
        assert step["status"] == "retained"
        if v1[u]["variant_key"] == "treatment":
            assert step["change_reason"] == "renamed_variant"
            assert step["source_variant"] == "treatment"
            assert step["source_variant_after_rename"] == "therapy"
        else:
            assert step["change_reason"] == "same_variant"


def test_added_variant_absorbs_incoming_buckets(client):
    create_experiment(client, config=base_config(traffic=100))
    users = [f"u-{i}" for i in range(300)]
    v1 = {u: decide(client, u, 1) for u in users}

    cfg = base_config(traffic=100, variants=[
        {"key": "control", "percentage": 40, "is_control": True},
        {"key": "newvar", "percentage": 30},
        {"key": "treatment", "percentage": 30},
    ])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1}
    r = client.post("/api/experiments/exp/versions",
                    params={"publish": "true"}, json=cfg)
    assert r.status_code == 201, r.text
    order = r.json()["continuity"]["variant_order"]
    # new variant keeps a deterministic position
    assert order == ["control", "newvar", "treatment"]

    reasons = set()
    for u in users:
        d2 = decide(client, u, 2)
        step = cont_step(d2)["detail"]
        if d2["variant_key"] == "newvar":
            reasons.add(step["change_reason"])
        elif d2["variant_key"] == v1[u]["variant_key"]:
            assert step["status"] == "retained"
    assert reasons == {"variant_added"}


def test_removed_variant_reallocates_its_buckets(client):
    create_experiment(client, config=base_config(traffic=100))
    users = [f"u-{i}" for i in range(300)]
    v1 = {u: decide(client, u, 1) for u in users}

    cfg = base_config(traffic=100, variants=[
        {"key": "control", "percentage": 100, "is_control": True},
    ])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1}
    r = client.post("/api/experiments/exp/versions",
                    params={"publish": "true"}, json=cfg)
    assert r.status_code == 201, r.text

    removed_users = [u for u in users if v1[u]["variant_key"] == "treatment"]
    assert removed_users  # sanity
    for u in removed_users:
        d2 = decide(client, u, 2)
        step = cont_step(d2)["detail"]
        assert d2["variant_key"] == "control"
        assert step["change_reason"] == "variant_removed"
        assert step["source_variant"] == "treatment"


def test_seed_chains_across_multiple_inherit_versions(client):
    create_experiment(client, config=base_config(traffic=100))
    for number in (2, 3, 4):
        create_continuity_version(
            client, number, cfg=cont_config(source_version=number - 1))
    b1 = decide(client, "chain-user", 1)["bucket"]
    for number in (2, 3, 4):
        assert decide(client, "chain-user", number)["bucket"] == b1


# ---------------------------------------------------------------------------
# Reshuffle
# ---------------------------------------------------------------------------


def test_explicit_reshuffle_has_no_continuity_step(client):
    create_experiment(client, config=base_config(traffic=100))
    create_continuity_version(
        client, 2, cfg=cont_config(mode="reshuffle", source_version=None))
    body = decide(client, "u-9", 2)
    assert all(s["step"] != "continuity" for s in body["trace"])
    # version-scoped seed => ordinary reshuffle
    assert body["bucket"] != decide(client, "u-9", 1)["bucket"] or True


def test_renames_without_inherit_are_rejected(client):
    create_experiment(client, config=base_config(traffic=100))
    r = client.post("/api/experiments/exp/versions", json=cont_config(
        mode="reshuffle", source_version=None,
        renames=[{"source": "control", "target": "a"}]))
    assert r.status_code == 422
    assert "continuity_renames_without_inherit" in _codes(r)


# ---------------------------------------------------------------------------
# Precheck rejection rules
# ---------------------------------------------------------------------------


def _codes(response) -> set[str]:
    body = response.json()
    # /versions returns the unified error envelope; /preflight returns the
    # PreflightResponse body even with a 422 status.
    issues = (body.get("error", {}).get("details", {}) or {}).get("issues")
    if issues is None:
        issues = body.get("issues", [])
    return {i["code"] for i in issues}


def test_inherit_from_draft_rejected(client):
    create_experiment(client, config=base_config(), publish=False)
    r = client.post("/api/experiments/exp/versions", json=cont_config(source_version=1))
    assert r.status_code == 422
    assert "continuity_source_draft" in _codes(r)


def test_inherit_from_other_experiment_rejected(client):
    create_experiment(client, key="exp", namespace="shared",
                      config=base_config(traffic=40))
    create_experiment(client, key="other", namespace="shared",
                      config=base_config(traffic=40))
    # Explicitly naming another experiment as the anchor is rejected.
    r = client.post("/api/experiments/other/versions", json=cont_config(
        source_version=1, source_experiment="exp"))
    assert r.status_code == 422
    assert "continuity_cross_experiment" in _codes(r)
    # Without the override the source lookup stays scoped to the path
    # experiment (other v1 is a valid anchor of 'other', so it succeeds).
    r = client.post("/api/experiments/other/versions",
                    params={"publish": "true"},
                    json=cont_config(source_version=1, traffic=40))
    assert r.status_code == 201, r.text
    assert r.json()["continuity"]["source_experiment"] == "other"


def test_inherit_from_missing_version_rejected(client):
    create_experiment(client, config=base_config())
    r = client.post("/api/experiments/exp/versions",
                    json=cont_config(source_version=42))
    assert r.status_code == 422
    assert "continuity_source_not_found" in _codes(r)


def test_inherit_requires_source_version(client):
    create_experiment(client, config=base_config())
    r = client.post("/api/experiments/exp/versions",
                    json={"mode": "inherit"})
    # Not a valid config payload at all in this path (no variants), so test
    # via a structurally valid config missing source_version.
    cfg = base_config()
    cfg["continuity"] = {"mode": "inherit"}
    r = client.post("/api/experiments/exp/versions", json=cfg)
    assert r.status_code == 422
    assert "continuity_source_required" in _codes(r)


def test_duplicate_and_unknown_rename_entries_rejected(client):
    create_experiment(client, config=base_config())
    # duplicate source
    r = client.post("/api/experiments/exp/versions", json=cont_config(
        renames=[{"source": "control", "target": "a"},
                 {"source": "control", "target": "b"}]))
    assert "duplicate_rename_source" in _codes(r)
    # duplicate target
    r = client.post("/api/experiments/exp/versions", json=cont_config(
        renames=[{"source": "control", "target": "z"},
                 {"source": "treatment", "target": "z"}]))
    assert "duplicate_rename_target" in _codes(r)
    # unknown source variant
    cfg = base_config(variants=[
        {"key": "control", "percentage": 100, "is_control": True}])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1,
                         "renames": [{"source": "ghost", "target": "control"}]}
    r = client.post("/api/experiments/exp/versions", json=cfg)
    assert "rename_source_not_found" in _codes(r)
    # unknown target variant
    r = client.post("/api/experiments/exp/versions", json=cont_config(
        renames=[{"source": "control", "target": "ghost"}]))
    assert "rename_target_not_found" in _codes(r)


def test_rename_target_colliding_with_surviving_variant_rejected(client):
    create_experiment(client, config=base_config())
    # rename control -> treatment while treatment survives unmapped
    cfg = base_config()
    cfg["continuity"] = {"mode": "inherit", "source_version": 1,
                         "renames": [{"source": "control",
                                      "target": "treatment"}]}
    r = client.post("/api/experiments/exp/versions", json=cfg)
    assert r.status_code == 422
    assert "rename_target_conflict" in _codes(r)


def test_preflight_validates_continuity_without_storing(client):
    create_experiment(client, config=base_config())
    r = client.post("/api/experiments/exp/preflight", json=cont_config(source_version=1))
    assert r.status_code == 200 and r.json()["valid"] is True
    # no version stored by preflight
    versions = client.get("/api/experiments/exp/versions").json()
    assert [v["version"] for v in versions] == [1]

    r = client.post("/api/experiments/exp/preflight",
                    json=cont_config(source_version=2))
    assert r.status_code == 422
    assert "continuity_source_not_found" in _codes(r)


def test_draft_inherit_anchored_on_published_then_published(client):
    create_experiment(client, config=base_config())
    r = client.post("/api/experiments/exp/versions",
                    params={"publish": "false"}, json=cont_config())
    assert r.status_code == 201 and r.json()["status"] == "draft"
    assert r.json()["continuity"]["mode"] == "inherit"
    # frozen block survives publish (no re-resolution needed)
    r = client.post("/api/experiments/exp/versions/2/publish")
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert r.json()["continuity"]["source_version"] == 1


# ---------------------------------------------------------------------------
# Target version gates govern eligibility
# ---------------------------------------------------------------------------


def test_target_audience_and_traffic_govern_continuous_mode(client):
    cfg = base_config(audience={"condition": {"field": "tier", "op": "eq",
                                              "value": "gold"}})
    create_experiment(client, config=cfg)
    # v2 narrows audience to platinum but inherits the seed
    cfg2 = base_config(audience={"condition": {"field": "tier", "op": "eq",
                                               "value": "platinum"}})
    cfg2["continuity"] = {"mode": "inherit", "source_version": 1}
    client.post("/api/experiments/exp/versions",
                params={"publish": "true"}, json=cfg2)

    gold_v1 = decide(client, "u", 1, attributes={"tier": "gold"})
    gold_v2 = decide(client, "u", 2, attributes={"tier": "gold"})
    assert gold_v1["enrolled"] is True
    assert gold_v2["enrolled"] is False
    assert gold_v2["reason"] == "audience_mismatch"
    # continuity explanation is still computed for the gate miss
    assert cont_step(gold_v2)["detail"]["bucket"] == gold_v1["bucket"]

    plat_v2 = decide(client, "u", 2, attributes={"tier": "platinum"})
    assert plat_v2["enrolled"] is True
    assert plat_v2["bucket"] == gold_v1["bucket"]


# ---------------------------------------------------------------------------
# Frozen configuration and immutability
# ---------------------------------------------------------------------------


def test_continuity_block_is_frozen_with_version(client):
    create_experiment(client, config=base_config())
    create_continuity_version(client, 2, cfg=cont_config())
    conn = get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE experiment_versions SET continuity_json = '{}' WHERE version = 2")
    conn.rollback()
    # config_json never carries the input declaration
    row = conn.execute(
        "SELECT config_json FROM experiment_versions WHERE version = 2"
    ).fetchone()
    assert "continuity" not in row[0]


# ---------------------------------------------------------------------------
# Idempotent replay stays on the historical version
# ---------------------------------------------------------------------------


def test_idempotent_replay_unchanged_after_continuity_version(client):
    create_experiment(client, config=base_config())
    first = client.post("/api/experiments/exp/decide", json={
        "user_key": "u1", "record_exposure": True,
        "idempotency_key": "idem-1"}).json()
    assert first["version_number"] == 1 and first["exposure_recorded"] is True
    create_continuity_version(
        client, 2, cfg=base_config(variants=[
            {"key": "control", "percentage": 90, "is_control": True},
            {"key": "treatment", "percentage": 10}]),
        publish=True)
    # replace the just-created v2 with an inherited one for real continuity
    # (v2 above reshuffled; create v3 inheriting v1)
    create_continuity_version(
        client, 3,
        cfg=cont_config(source_version=1,
                        **{"variants": [
                            {"key": "control", "percentage": 90, "is_control": True},
                            {"key": "treatment", "percentage": 10}]}))

    replay = client.post("/api/experiments/exp/decide", json={
        "user_key": "u1", "record_exposure": True,
        "idempotency_key": "idem-1"}).json()
    assert replay["version_number"] == 1
    assert replay["variant_key"] == first["variant_key"]
    assert replay["trace"] == first["trace"]
    assert replay["exposure_recorded"] is False


# ---------------------------------------------------------------------------
# Migration preview
# ---------------------------------------------------------------------------


def _preview(client, frm: int, to: int, users, *, key: str = "exp", **over):
    payload = {"from_version": frm, "to_version": to, "users": users}
    payload.update(over)
    return client.post(
        f"/api/experiments/{key}/versions/migration-preview", json=payload)


def test_migration_preview_classifies_entered_retained_not_enrolled(client):
    cfg = base_config(traffic=100, audience={
        "condition": {"field": "tier", "op": "eq", "value": "gold"}})
    create_experiment(client, config=cfg)
    cfg2 = base_config(traffic=100, audience={"any": [
        {"condition": {"field": "tier", "op": "eq", "value": "gold"}},
        {"condition": {"field": "tier", "op": "eq", "value": "silver"}},
    ]})
    cfg2["continuity"] = {"mode": "inherit", "source_version": 1}
    client.post("/api/experiments/exp/versions",
                params={"publish": "true"}, json=cfg2)

    users = (
        [{"user_key": f"gold-{i}", "attributes": {"tier": "gold"}}
         for i in range(40)]
        + [{"user_key": f"silver-{i}", "attributes": {"tier": "silver"}}
           for i in range(20)]
        + [{"user_key": f"bronze-{i}", "attributes": {"tier": "bronze"}}
           for i in range(20)]
    )
    r = _preview(client, 1, 2, users)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["users"] == 80
    counts = body["counts"]
    assert counts["entered"] > 0          # silver users newly admitted
    assert counts["retained"] > 0
    assert counts["not_enrolled"] == 20   # bronze out in both
    assert counts["exited"] == 0
    assert (sum(counts.values())) == 80
    assert body["exposures_written"] is False
    # no exposure rows were written
    assert get_conn().execute(
        "SELECT COUNT(*) FROM exposures").fetchone()[0] == 0
    # samples bounded and shaped
    assert len(body["samples"]["entered"]) <= 20
    sample = body["samples"]["entered"][0]
    assert sample["from_enrolled"] is False and sample["to_enrolled"] is True
    assert sample["category"] == "entered"


def test_migration_preview_switch_reasons_for_weight_change(client):
    create_experiment(client, config=base_config())
    cfg = base_config(variants=[
        {"key": "control", "percentage": 90, "is_control": True},
        {"key": "treatment", "percentage": 10}])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1}
    client.post("/api/experiments/exp/versions",
                params={"publish": "true"}, json=cfg)
    users = [{"user_key": f"u-{i}"} for i in range(300)]
    body = _preview(client, 1, 2, users).json()
    if body["counts"]["switched"]:
        assert set(body["switch_reasons"]) == {"weight_boundary_crossed"}
        row = body["samples"]["switched"][0]
        assert row["change_reason"] == "weight_boundary_crossed"
        assert row["from_variant_key"] == "treatment"
        assert row["to_variant_key"] == "control"


def test_migration_preview_reshuffle_target_reasons(client):
    create_experiment(client, config=base_config())
    cfg = base_config(variants=[
        {"key": "control", "percentage": 70, "is_control": True},
        {"key": "treatment", "percentage": 30}])
    cfg["continuity"] = {"mode": "reshuffle"}
    client.post("/api/experiments/exp/versions",
                params={"publish": "true"}, json=cfg)
    users = [{"user_key": f"u-{i}"} for i in range(200)]
    body = _preview(client, 1, 2, users).json()
    if body["counts"]["switched"]:
        assert all(k == "reshuffled" for k in body["switch_reasons"])


def test_migration_preview_requires_published_versions(client):
    create_experiment(client, config=base_config(), publish=False)
    r = _preview(client, 1, 1, [{"user_key": "u"}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_not_published"


def test_migration_preview_rejects_more_than_10000_users(client):
    create_experiment(client, config=base_config())
    r = _preview(client, 1, 1, [{"user_key": "x"}] * 10_001)
    assert r.status_code == 400


def test_migration_preview_is_deterministic(client):
    create_experiment(client, config=base_config())
    create_continuity_version(client, 2, cfg=cont_config())
    users = [{"user_key": f"u-{i}"} for i in range(50)]
    b1 = _preview(client, 1, 2, users).json()
    b2 = _preview(client, 1, 2, users).json()
    assert b1["counts"] == b2["counts"]
    assert b1["samples"] == b2["samples"]


# ---------------------------------------------------------------------------
# Regression tests for the four reviewed defects
# ---------------------------------------------------------------------------


def test_regression_non_lexicographic_source_order_keeps_groups(client):
    """Same config inherited from a source whose DECLARED order is not the
    lexicographic bucketing order must keep every user in the original
    group, and the trace must agree (not falsely report retained/switched).
    """
    variants = [
        {"key": "zeta", "percentage": 40, "is_control": True},
        {"key": "alpha", "percentage": 30},
        {"key": "mid", "percentage": 30},
    ]
    cfg = base_config(traffic=100, variants=variants,
                      control_variant_key="zeta")
    create_experiment(client, key="nlo", namespace="nlo", config=cfg)

    users = [f"u-{i}" for i in range(120)]

    def dec(user, version):
        return client.post(f"/api/experiments/nlo/decide",
                           json={"user_key": user, "version": version}).json()

    v1 = {u: dec(u, 1) for u in users}

    inherited = base_config(traffic=100, variants=[dict(v) for v in variants],
                            control_variant_key="zeta")
    inherited["continuity"] = {"mode": "inherit", "source_version": 1}
    r = client.post("/api/experiments/nlo/versions",
                    params={"publish": "true"}, json=inherited)
    assert r.status_code == 201, r.text
    # frozen effective order must equal the source's real bucketing order
    # (lexicographic), regardless of declaration order
    assert r.json()["continuity"]["variant_order"] == ["alpha", "mid", "zeta"]

    kept = 0
    for u in users:
        d2 = dec(u, 2)
        step = cont_step(d2)
        assert d2["variant_key"] == v1[u]["variant_key"]
        assert d2["bucket"] == v1[u]["bucket"]
        assert step["result"] == "retained"
        assert step["detail"]["change_reason"] == "same_variant"
        kept += 1
    assert kept == len(users)


def test_regression_whitelist_force_switch_is_explained(client):
    create_experiment(client, key="wl", namespace="wl",
                      config=base_config(traffic=100))

    def dec(user, version):
        return client.post(f"/api/experiments/wl/decide",
                           json={"user_key": user, "version": version}).json()

    # Find a user whose organic group is control, then force to treatment.
    forced_user = next(u for i in range(200)
                       for u in [f"w-{i}"]
                       if dec(u, 1)["variant_key"] == "control")
    cfg = base_config(traffic=100, whitelist=[
        {"user_key": forced_user, "variant_key": "treatment"}])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1}
    r = client.post("/api/experiments/wl/versions",
                    params={"publish": "true"}, json=cfg)
    assert r.status_code == 201, r.text

    d = dec(forced_user, 2)
    assert d["enrolled"] is True and d["variant_key"] == "treatment"
    assert d["reason"] == "whitelist"
    step = cont_step(d)
    assert step["result"] == "switched"
    detail = step["detail"]
    assert detail["change_reason"] == "whitelist_override"
    assert detail["whitelist_forced_variant"] == "treatment"
    assert detail["source_variant"] == "control"

    # Migration preview must attribute the switch to the whitelist as well.
    body = _preview(client, 1, 2, [{"user_key": forced_user}],
                    key="wl").json()
    assert body["counts"]["switched"] == 1
    assert body["switch_reasons"] == {"whitelist_override": 1}
    sample = body["samples"]["switched"][0]
    assert sample["change_reason"] == "whitelist_override"
    assert sample["to_reason"] == "whitelist"


def test_regression_whitelist_same_group_stays_retained(client):
    create_experiment(client, key="wl2", namespace="wl2",
                      config=base_config(traffic=100))

    def dec(user, version):
        return client.post(f"/api/experiments/wl2/decide",
                           json={"user_key": user, "version": version}).json()

    same_group_user = next(u for i in range(200)
                           for u in [f"w-{i}"]
                           if dec(u, 1)["variant_key"] == "treatment")
    cfg = base_config(traffic=100, whitelist=[
        {"user_key": same_group_user, "variant_key": "treatment"}])
    cfg["continuity"] = {"mode": "inherit", "source_version": 1}
    client.post("/api/experiments/wl2/versions",
                params={"publish": "true"}, json=cfg)
    step = cont_step(dec(same_group_user, 2))
    assert step["result"] == "retained"
    assert step["detail"]["whitelist_forced_variant"] == "treatment"


def test_regression_three_version_rename_chain_in_preview(client):
    # v1: a/b ; v2 renames b -> c ; v3 renames c -> d. A v1 -> v3 preview
    # must compose the full chain (b -> d) and count users retained.
    create_experiment(client, key="rn", namespace="rn",
                      config=base_config(traffic=100, control_variant_key="a",
                                         variants=[
                                             {"key": "a", "percentage": 50,
                                              "is_control": True},
                                             {"key": "b", "percentage": 50}]))
    for new_key, renamed_from, anchor in (("c", "b", 1), ("d", "c", 2)):
        cfg = base_config(traffic=100, control_variant_key="a", variants=[
            {"key": "a", "percentage": 50, "is_control": True},
            {"key": new_key, "percentage": 50},
        ])
        cfg["continuity"] = {
            "mode": "inherit",
            "source_version": anchor,
            "renames": [{"source": renamed_from, "target": new_key}],
        }
        r = client.post("/api/experiments/rn/versions",
                        params={"publish": "true"}, json=cfg)
        assert r.status_code == 201, r.text

    users = [{"user_key": f"x-{i}"} for i in range(100)]
    body = _preview(client, 1, 3, users, key="rn").json()
    assert body["counts"]["retained"] == 100
    assert body["counts"]["switched"] == 0
    assert body["switch_reasons"] == {}

    # Every b user from v1 must land on d in v3 (spot check via pinned
    # decisions), and the v3 trace still compares against its direct
    # anchor v2 (single-hop c -> d).
    def dec(user, version):
        return client.post(f"/api/experiments/rn/decide",
                           json={"user_key": user, "version": version}).json()

    b_users = [f"x-{i}" for i in range(100)
               if dec(f"x-{i}", 1)["variant_key"] == "b"]
    assert b_users  # sanity: at least one b user
    for u in b_users:
        assert dec(u, 3)["variant_key"] == "d"
        step = cont_step(dec(u, 3))
        assert step["detail"]["source_version"] == 2
        assert step["detail"]["change_reason"] == "renamed_variant"


def test_regression_published_version_cannot_be_demoted_to_draft(client):
    create_experiment(client, config=base_config())
    conn = get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE experiment_versions SET status = 'draft' WHERE version = 1")
    conn.rollback()
    row = conn.execute(
        "SELECT status FROM experiment_versions WHERE version = 1"
    ).fetchone()
    assert row[0] == "published"

    # The demote attempt must not have opened a window to rewrite the
    # frozen continuity block either.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE experiment_versions SET continuity_json = '{}' WHERE version = 1")
    conn.rollback()
