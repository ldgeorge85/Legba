# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``entity_alias_keys`` — backfill and drift verification.

The key table (migration 0263) is maintained by trigger in the same
transaction as every ``entity_profiles`` write. Two things the trigger cannot
do are here:

* :func:`backfill_batch` — fill (or repair) the keys of the profiles that
  existed before the trigger, one keyset batch of ``entity_profiles`` ids at a
  time. Each batch locks its profile rows ``FOR SHARE`` (a concurrent profile
  UPDATE waits for the batch's commit and then re-keys itself through the
  trigger, so the backfill can never write a stale key over a fresh one),
  inserts the missing keys and deletes the extra ones. A batch with nothing to
  change writes nothing, so a re-run is a read. It writes ONLY
  ``entity_alias_keys``.
* :func:`verify_batch` — compare the table against the profiles. The expected
  keys are computed here from the SCAN'S OWN EXPRESSIONS (the text of
  ``_entity_resolve._EXACT_ALIAS_SQL`` / ``_ALIAS_SQL``), not by calling the
  table's key function, so a wrong key function shows up as drift too. One
  statement per batch, so each batch is a consistent snapshot and a concurrent
  write can never read as drift.

Both walk ``entity_profiles`` in ``id`` order from a cursor; neither holds a
long transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Default keyset batch (profiles per transaction).
BATCH_SIZE = 2000

_ALIASES = (
    "jsonb_array_elements_text(CASE WHEN jsonb_typeof(p.data->'merged_aliases') = 'array' "
    "THEN p.data->'merged_aliases' ELSE '[]'::jsonb END)"
)


def _norm(x: str) -> str:
    return (
        "regexp_replace(regexp_replace(lower(btrim(" + x + ")), "
        "'^(the|a|an)\\s+', '', 'g'), '\\s+', ' ', 'g')"
    )


#: Expected keys of the profiles in ``ids`` computed from the scan's own
#: expressions (independent of ``entity_alias_keys_for``). Same 2000-byte cap.
_EXPECTED_INLINE_SQL = f"""
    SELECT DISTINCT e.key_kind, e.key, e.entity_id FROM (
        SELECT 'norm'::text AS key_kind, {_norm('p.canonical_name')} AS key,
               p.id AS entity_id
          FROM entity_profiles p WHERE p.id IN (SELECT id FROM ids)
        UNION ALL
        SELECT 'norm', {_norm('al')}, p.id
          FROM entity_profiles p, {_ALIASES} AS al
         WHERE p.id IN (SELECT id FROM ids)
        UNION ALL
        SELECT 'alias_lower', lower(btrim(al)), p.id
          FROM entity_profiles p, {_ALIASES} AS al
         WHERE p.id IN (SELECT id FROM ids)
    ) e
    WHERE e.key IS NOT NULL AND octet_length(e.key) <= 2000
"""

_VERIFY_SQL = f"""
WITH ids AS (
    SELECT id FROM entity_profiles
     WHERE ($1::uuid IS NULL OR id > $1::uuid)
     ORDER BY id LIMIT $2
), exp AS ({_EXPECTED_INLINE_SQL}
), act AS (
    SELECT a.key_kind, a.key, a.entity_id
      FROM entity_alias_keys a WHERE a.entity_id IN (SELECT id FROM ids)
), missing AS (
    SELECT * FROM exp EXCEPT SELECT * FROM act
), extra AS (
    SELECT * FROM act EXCEPT SELECT * FROM exp
)
SELECT (SELECT id::text FROM ids ORDER BY id DESC LIMIT 1) AS last_id,
       (SELECT count(*) FROM ids)                          AS profiles,
       (SELECT count(*) FROM exp)                          AS expected,
       (SELECT count(*) FROM act)                          AS actual,
       (SELECT count(*) FROM missing WHERE key_kind = 'norm')        AS missing_norm,
       (SELECT count(*) FROM missing WHERE key_kind = 'alias_lower') AS missing_alias,
       (SELECT count(*) FROM extra WHERE key_kind = 'norm')          AS extra_norm,
       (SELECT count(*) FROM extra WHERE key_kind = 'alias_lower')   AS extra_alias,
       (SELECT array_agg(DISTINCT entity_id::text) FROM (
            SELECT entity_id FROM missing UNION SELECT entity_id FROM extra
            LIMIT 20) s)                                    AS drift_ids
"""

_LOCK_IDS_SQL = """
    SELECT id FROM entity_profiles
     WHERE ($1::uuid IS NULL OR id > $1::uuid)
     ORDER BY id LIMIT $2
       FOR SHARE
"""

_READ_IDS_SQL = """
    SELECT id FROM entity_profiles
     WHERE ($1::uuid IS NULL OR id > $1::uuid)
     ORDER BY id LIMIT $2
"""

#: Reconcile the keys of ``$1`` (uuid[]) to what the key function says:
#: insert the missing, delete the extra; returns the three counts.
_APPLY_SQL = """
WITH exp AS (
    SELECT DISTINCT k.key_kind, k.key, p.id AS entity_id
      FROM entity_profiles p,
           LATERAL public.entity_alias_keys_for(p.canonical_name, p.data) AS k
     WHERE p.id = ANY($1::uuid[])
), del AS (
    DELETE FROM entity_alias_keys a
     WHERE a.entity_id = ANY($1::uuid[])
       AND NOT EXISTS (
           SELECT 1 FROM exp
            WHERE exp.key_kind = a.key_kind AND exp.key = a.key
              AND exp.entity_id = a.entity_id)
    RETURNING 1
), ins AS (
    INSERT INTO entity_alias_keys (key_kind, key, entity_id)
    SELECT key_kind, key, entity_id FROM exp
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT (SELECT count(*) FROM exp) AS expected,
       (SELECT count(*) FROM ins) AS inserted,
       (SELECT count(*) FROM del) AS deleted
"""

#: Dry-run form of :data:`_APPLY_SQL`: the same counts, nothing written.
_DRY_SQL = """
WITH exp AS (
    SELECT DISTINCT k.key_kind, k.key, p.id AS entity_id
      FROM entity_profiles p,
           LATERAL public.entity_alias_keys_for(p.canonical_name, p.data) AS k
     WHERE p.id = ANY($1::uuid[])
), act AS (
    SELECT key_kind, key, entity_id FROM entity_alias_keys
     WHERE entity_id = ANY($1::uuid[])
)
SELECT (SELECT count(*) FROM exp) AS expected,
       (SELECT count(*) FROM (SELECT * FROM exp EXCEPT SELECT * FROM act) m) AS inserted,
       (SELECT count(*) FROM (SELECT * FROM act EXCEPT SELECT * FROM exp) x) AS deleted
"""


@dataclass
class BatchResult:
    last_id: str | None
    profiles: int
    expected: int
    inserted: int
    deleted: int


async def backfill_batch(
    conn: Any, *, after_id: str | None, batch_size: int = BATCH_SIZE,
    apply: bool = False,
) -> BatchResult:
    """One keyset batch. ``apply=False`` (the default) writes nothing.

    With ``apply`` the batch runs in its own transaction on ``conn``.
    """
    if apply:
        async with conn.transaction():
            ids = [r["id"] for r in await conn.fetch(_LOCK_IDS_SQL, after_id, batch_size)]
            if not ids:
                return BatchResult(None, 0, 0, 0, 0)
            r = await conn.fetchrow(_APPLY_SQL, ids)
    else:
        async with conn.transaction(readonly=True):
            ids = [r["id"] for r in await conn.fetch(_READ_IDS_SQL, after_id, batch_size)]
            if not ids:
                return BatchResult(None, 0, 0, 0, 0)
            r = await conn.fetchrow(_DRY_SQL, ids)
    return BatchResult(
        last_id=str(ids[-1]), profiles=len(ids),
        expected=int(r["expected"]), inserted=int(r["inserted"]),
        deleted=int(r["deleted"]),
    )


@dataclass
class VerifyResult:
    last_id: str | None
    profiles: int
    expected: int
    actual: int
    missing_norm: int
    missing_alias: int
    extra_norm: int
    extra_alias: int
    drift_ids: list[str]

    @property
    def drift(self) -> int:
        return self.missing_norm + self.missing_alias + self.extra_norm + self.extra_alias


async def verify_batch(
    conn: Any, *, after_id: str | None, batch_size: int = BATCH_SIZE,
) -> VerifyResult:
    """Compare one keyset batch of profiles with their key rows (read-only)."""
    async with conn.transaction(readonly=True):
        r = await conn.fetchrow(_VERIFY_SQL, after_id, batch_size)
    return VerifyResult(
        last_id=r["last_id"], profiles=int(r["profiles"]),
        expected=int(r["expected"]), actual=int(r["actual"]),
        missing_norm=int(r["missing_norm"]), missing_alias=int(r["missing_alias"]),
        extra_norm=int(r["extra_norm"]), extra_alias=int(r["extra_alias"]),
        drift_ids=list(r["drift_ids"] or []),
    )


async def verify_all(conn: Any, *, batch_size: int = BATCH_SIZE) -> VerifyResult:
    """Walk every profile; the summed result (drift ids capped at 20)."""
    total = VerifyResult(None, 0, 0, 0, 0, 0, 0, 0, [])
    after: str | None = None
    while True:
        r = await verify_batch(conn, after_id=after, batch_size=batch_size)
        if r.profiles == 0:
            break
        for f in ("profiles", "expected", "actual", "missing_norm",
                  "missing_alias", "extra_norm", "extra_alias"):
            setattr(total, f, getattr(total, f) + getattr(r, f))
        total.drift_ids = (total.drift_ids + r.drift_ids)[:20]
        total.last_id = after = r.last_id
    return total


__all__ = [
    "BATCH_SIZE",
    "BatchResult",
    "VerifyResult",
    "backfill_batch",
    "verify_all",
    "verify_batch",
]
