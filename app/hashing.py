"""Stable hash bucketing.

Two independent buckets are derived from one SHA-256 digest:

* ``variant_bucket`` — first 8 bytes % 10000, selects the variant range
* ``gate_bucket``    — next 8 bytes % 10000, evaluated against the
  experiment's traffic_percentage cutoff

Using different digest bytes keeps the traffic gate independent from the
variant split: with traffic_percentage = 50 and a 50/50 variant split, the
enrolled half still spreads 50/50 across variants instead of collapsing onto
the first range. Both buckets are deterministic functions of

    salt | version_number | experiment_key | user_key

so an unchanged published version pins every user forever, the experiment
salt decorrelates users across experiments, and publishing a new version
deliberately reshuffles.
"""

from __future__ import annotations

import hashlib

BUCKET_SPACE = 10_000


def _digest(experiment_key: str, salt: str, version_number: int,
            user_key: str) -> bytes:
    material = "|".join([salt, str(version_number), experiment_key, user_key])
    return hashlib.sha256(material.encode("utf-8")).digest()


def variant_bucket(experiment_key: str, salt: str, version_number: int,
                   user_key: str) -> int:
    return int.from_bytes(_digest(experiment_key, salt, version_number,
                                  user_key)[:8], "big") % BUCKET_SPACE


def gate_bucket(experiment_key: str, salt: str, version_number: int,
                user_key: str) -> int:
    return int.from_bytes(_digest(experiment_key, salt, version_number,
                                  user_key)[8:16], "big") % BUCKET_SPACE


# Backward-compatible alias used by the decision trace / tests.
def stable_bucket(experiment_key: str, salt: str, version_number: int,
                  user_key: str) -> int:
    return variant_bucket(experiment_key, salt, version_number, user_key)
