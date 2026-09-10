"""Stable hash bucketing.

Two distinct hash positions are used:

* ``variant_bucket`` — SHA-256 of

      salt | version_number | experiment_key | user_key

  interpreted big-endian (bytes 0..7) and folded onto [0, 10000). It selects
  the variant range. Including the experiment salt defeats cross-experiment
  correlation; including the version number means a newly published version
  deliberately reshuffles variants while an unchanged version pins every
  user forever.

* ``assignment_bucket`` — the generalized form used by cross-version
  continuity (see ``continuity.py``). It hashes

      assignment_seed | experiment_key | user_key

  where the assignment seed is ``salt|version_number`` for an ordinary
  version and the *inherited* seed of an older published version for a
  continuity version. Feeding ``"salt|version_number"`` as the seed
  reproduces the legacy material byte-for-byte, so historical decisions are
  unchanged.

* the *namespace gate bucket* lives in ``mutex.py`` — it is scoped to the
  mutex namespace (not the experiment/version) so all experiments in one
  namespace agree on the user's single position on the traffic ring.
"""

from __future__ import annotations

import hashlib

BUCKET_SPACE = 10_000


def assignment_bucket(experiment_key: str, assignment_seed: str,
                      user_key: str) -> int:
    material = "|".join([assignment_seed, experiment_key, user_key])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


def variant_bucket(experiment_key: str, salt: str, version_number: int,
                   user_key: str) -> int:
    """Legacy per-version bucket; the version-scoped seed is ``salt|version``."""
    return assignment_bucket(
        experiment_key, f"{salt}|{version_number}", user_key)


# Backward-compatible alias.
def stable_bucket(experiment_key: str, salt: str, version_number: int,
                  user_key: str) -> int:
    return variant_bucket(experiment_key, salt, version_number, user_key)
