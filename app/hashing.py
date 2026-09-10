"""Stable hash bucketing.

Two distinct hash positions are used:

* ``variant_bucket`` — SHA-256 of

      salt | version_number | experiment_key | user_key

  interpreted big-endian (bytes 0..7) and folded onto [0, 10000). It selects
  the variant range. Including the experiment salt defeats cross-experiment
  correlation; including the version number means a newly published version
  deliberately reshuffles variants while an unchanged version pins every
  user forever.

* the *namespace gate bucket* lives in ``mutex.py`` — it is scoped to the
  mutex namespace (not the experiment/version) so all experiments in one
  namespace agree on the user's single position on the traffic ring.
"""

from __future__ import annotations

import hashlib

BUCKET_SPACE = 10_000


def variant_bucket(experiment_key: str, salt: str, version_number: int,
                   user_key: str) -> int:
    material = "|".join([salt, str(version_number), experiment_key, user_key])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


# Backward-compatible alias.
def stable_bucket(experiment_key: str, salt: str, version_number: int,
                  user_key: str) -> int:
    return variant_bucket(experiment_key, salt, version_number, user_key)
