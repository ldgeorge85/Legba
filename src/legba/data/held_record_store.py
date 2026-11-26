# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held family's ONE writer.

:func:`write_shadow_record` writes one desk shadow record's plan
(:func:`held_record.plan_shadow_record`) into the six tables of migration
``20261115T1940_held_record``, in the caller's transaction:

* IDEMPOTENT per shadow record: a receipt row (``assertion_shadow_receipts``,
  unique on the shadow id and batch) is checked first and written last; a
  record already written writes nothing (and a concurrent second writer fails
  on the receipt's unique index, rolling its transaction back).
* the MATCHING RULE (stated in :mod:`held_record`): a planned record whose
  claim key is already held is a ``restates`` link (or, for an absence in a
  later window, a new version) — never a duplicate assertion.
* the STATUS is only ever a new log row (the database chains it); the cache
  follows by trigger.
* the BYTES: each cited signal's receipt version is pinned by the one rule
  (``citation_rows_store.pin_material`` → ``byte_path.classify``), with the
  label ``received_bytes`` / ``archived_later`` / ``no_bytes_kept``; a signal
  with no receipt is written with no pin (counted).
* WHO: every row's ``written_by`` (and every status row's ``caused_by``) is
  ``rule:held_record_writer``; a version's ``proposed_by`` is the desk seat
  (``agent:<id>@<version>``, the version read from the run's finding) or NULL
  when it cannot be named — never guessed.

The reads the writer needs (pins, the proposer, the prior read, surface
subjects, entity names) go through a :class:`Lookups`; :class:`DbLookups` reads
the same database, and the replay can hand file-backed ones.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .held_record import (
    HELD,
    NOT_HELD,
    RULE_VERSION,
    WRITER_HANDLE,
    PlannedRecord,
    ShadowPlan,
    plan_shadow_record,
    text_sha,
    type_map,
)

logger = logging.getLogger(__name__)

__all__ = ["DbLookups", "Lookups", "NoLookups", "WriteResult", "write_shadow_record"]


class Lookups(Protocol):
    async def pins(self, conn: Any, signal_ids: Sequence[str]) -> Mapping[str, Any]: ...
    async def proposer(self, conn: Any, run_id: str | None, analyst_id: str | None) -> str | None: ...
    async def prior_read(self, conn: Any, run_id: str | None, ordinal: int | None) -> str | None: ...
    async def surface_entities(self, conn: Any, names: Sequence[str]) -> Mapping[str, str]: ...


class NoLookups:
    """Nothing resolvable (a fresh database): no pins, no proposer, no prior
    read, no surface resolution. Every miss is counted, never guessed."""

    async def pins(self, conn: Any, signal_ids: Sequence[str]) -> Mapping[str, Any]:
        return {}

    async def proposer(self, conn: Any, run_id: str | None, analyst_id: str | None) -> str | None:
        return None

    async def prior_read(self, conn: Any, run_id: str | None, ordinal: int | None) -> str | None:
        return None

    async def surface_entities(self, conn: Any, names: Sequence[str]) -> Mapping[str, str]:
        return {}


_PROPOSER_SQL = """
SELECT analyst_version FROM analyst_outputs
 WHERE run_id = $1::uuid AND analyst_id = $2 AND kind = 'finding'
 LIMIT 1
"""

# What the run was handed at that ordinal, when it was a finding (the desk's
# prior read): DM-A's typed handed list.
_PRIOR_READ_SQL = """
SELECT h.ids[i]::text AS output_id
  FROM run_handed h, generate_subscripts(h.ordinals, 1) AS i
 WHERE h.run_id = $1::uuid AND h.target_kind = 'finding' AND h.ids IS NOT NULL AND h.ordinals[i] = $2
 LIMIT 1
"""

# A surface name resolves only when its normalised key names exactly ONE entity.
_SURFACE_SQL = """
SELECT key, min(entity_id::text) AS entity_id
  FROM entity_alias_keys
 WHERE key_kind = 'norm' AND key = ANY($1::text[])
 GROUP BY key
HAVING count(DISTINCT entity_id) = 1
"""


def _surface_norm(name: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"^(the|a|an)\s+", "", name.strip().lower()))


class DbLookups(NoLookups):
    """The reads, against the database the writer writes."""

    async def pins(self, conn: Any, signal_ids: Sequence[str]) -> Mapping[str, Any]:
        from .citation_rows_store import pin_material

        return await pin_material(conn, "signal", list(signal_ids)) if signal_ids else {}

    async def proposer(self, conn: Any, run_id: str | None, analyst_id: str | None) -> str | None:
        from .principals import seat_handle

        if not run_id or not analyst_id or not _is_uuid(run_id):
            return None
        version = await conn.fetchval(_PROPOSER_SQL, run_id, analyst_id)
        return seat_handle(analyst_id, version) if version else None

    async def prior_read(self, conn: Any, run_id: str | None, ordinal: int | None) -> str | None:
        if not run_id or ordinal is None or not _is_uuid(run_id):
            return None
        return await conn.fetchval(_PRIOR_READ_SQL, run_id, int(ordinal))

    async def surface_entities(self, conn: Any, names: Sequence[str]) -> Mapping[str, str]:
        by_norm = {_surface_norm(n): n for n in names if n.strip()}
        if not by_norm:
            return {}
        rows = await conn.fetch(_SURFACE_SQL, sorted(by_norm))
        return {by_norm[r["key"]]: r["entity_id"] for r in rows if r["key"] in by_norm}


def _is_uuid(v: str) -> bool:
    import re

    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v))


@dataclass
class WriteResult:
    shadow_id: int
    written: bool
    counts: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_RECEIPT_EXISTS_SQL = """
SELECT 1 FROM assertion_shadow_receipts WHERE shadow_id = $1 AND coalesce(batch, '') = coalesce($2, '')
"""

_RECEIPT_SQL = """
INSERT INTO assertion_shadow_receipts (shadow_id, run_id, analyst_id, target_id, occurred_at, rule_version,
                                       counts, origin, batch, written_by)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, legba_principal($10))
"""

_MATCH_SQL = """
SELECT a.id::text AS id, s.status, s.version_id, v.version_no, v.valid_from, v.valid_to
  FROM assertions a
  JOIN assertion_status s ON s.assertion_id = a.id
  JOIN assertion_versions v ON v.id = s.version_id
 WHERE a.claim_key = $1 AND coalesce(a.batch, '') = coalesce($2, '')
"""

_ASSERTION_SQL = """
INSERT INTO assertions (class, type_id, basis, subject_kind, subject_entity_id, subject_key, subject_text,
                        speaker_text, claim_key, collection, origin, batch, written_by)
VALUES ($1, $2, 'derived', $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11, legba_principal($12))
RETURNING id::text
"""

_VERSION_SQL = """
INSERT INTO assertion_versions (assertion_id, version_no, prev_version_id, reason_code, text, text_sha256, slots,
                                valid_from, valid_from_precision, valid_to, shadow_id, shadow_row, run_id,
                                analyst_id, contract_version, gate_version, proposed_by, origin, batch, written_by)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13, $14, $15, $16,
        legba_principal($17), $18, $19, legba_principal($20))
RETURNING id
"""

_STATUS_SQL = """
INSERT INTO assertion_status_log (assertion_id, status, version_id, reason_code, reason, verdict, caused_by,
                                  source_ref, batch)
VALUES ($1::uuid, $2, $3, $4, $5, $6, legba_principal($7), $8, $9)
"""

_SUPPORT_SQL = """
INSERT INTO assertion_support (claimant_version_id, claimant_shadow_row, role, target_kind, signal_id, output_id,
                               target_version_id, verdict_key, verdict, fetch_version_id, fetch_link_role,
                               bytes_label, bytes_sha256, text_sha256, quote, quote_found_in, detail, origin,
                               batch, written_by)
VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb, $18,
        $19, legba_principal($20))
"""


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


@dataclass
class _Held:
    """An assertion as it stands in the table (or was just written)."""

    id: str
    status: str
    version_id: int
    version_no: int
    valid_from: Any
    valid_to: Any


def _surface_names(shadow: Mapping[str, Any]) -> list[str]:
    record = shadow.get("record") if isinstance(shadow.get("record"), Mapping) else {}
    out = []
    for o in record.get("outcomes") or []:
        d = o.get("draft") if isinstance(o, Mapping) else None
        if isinstance(d, Mapping) and d.get("subject_kind") == "surface" and d.get("subject_key"):
            out.append(str(d["subject_key"]))
    return sorted(set(out))


def _resolve_surfaces(plan: ShadowPlan, resolved: Mapping[str, str], types: Mapping[str, Any]) -> int:
    """A surface subject that names exactly one entity becomes that entity
    (its surface text kept); its claim key is recomputed on the entity."""
    from .held_record import Subject, claim_key

    n = 0
    by_id = {d.id: d for d in types.values()}
    for r in plan.records:
        if r.subject.kind != "surface" or r.subject.text not in resolved:
            continue
        eid = resolved[r.subject.text].lower()
        r.subject = Subject("entity", eid, r.subject.text, eid)
        r.claim_key = claim_key(r.type_id, eid, {"text": r.text}, by_id[r.type_id].attr("key"))
        n += 1
    return n


async def write_shadow_record(
    conn: Any,
    shadow: Mapping[str, Any],
    *,
    lookups: Lookups | None = None,
    origin: str = "shadow",
    batch: str | None = None,
    types: Mapping[str, Any] | None = None,
) -> WriteResult:
    """Write one desk shadow record into the held family. The caller owns the
    transaction (this function opens none). ``WriteResult.written`` is False
    when the record was written before (nothing is written twice)."""
    if (origin == "replay") != (batch is not None):
        raise ValueError("a replay names its batch; a shadow write names none")
    shadow_id = int(shadow["id"])
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('held_record:shadow:' || $1::text, 0))",
                       str(shadow_id))
    if await conn.fetchval(_RECEIPT_EXISTS_SQL, shadow_id, batch):
        return WriteResult(shadow_id, False)
    lookups = lookups or DbLookups()
    types = types if types is not None else type_map()
    plan = plan_shadow_record(shadow, types=types)
    resolved = await lookups.surface_entities(conn, _surface_names(shadow))
    c: Counter = Counter()
    c["surface_resolved"] = _resolve_surfaces(plan, resolved, types)
    pins = await lookups.pins(conn, sorted({sid for r in plan.records for sid, _, _ in r.evidence}))
    proposer = await lookups.proposer(conn, plan.run_id, plan.analyst_id)
    c["proposer_named"] = int(proposer is not None)

    w = _Writer(conn, plan, pins, proposer, origin, batch, c)
    by_row: dict[str, _Held] = {}
    for rec in plan.records:
        by_row[rec.row_id] = await w.record(rec, by_row)
    for link in plan.links:
        output_id = await lookups.prior_read(conn, plan.run_id, link.ref)
        if output_id is None:
            c["restatement_prior_read_unresolved"] += 1
            continue
        await w.support(None, w.row_ref(link.row_id), "restates", "finding", output_id=output_id,
                        detail={"status": link.status, "backing": list(link.backing), "ref": link.ref})
        c["restatement_links_prior_read"] += 1

    counts = {**plan.counts(), **dict(c)}
    await conn.execute(_RECEIPT_SQL, shadow_id, plan.run_id, plan.analyst_id, plan.target_id,
                       _ts(plan.occurred_at), RULE_VERSION, json.dumps(counts, default=str, sort_keys=True),
                       origin, batch, WRITER_HANDLE)
    return WriteResult(shadow_id, True, counts)


def _ts(v: Any) -> Any:
    from .held_record import _dt

    return _dt(v)


class _Writer:
    def __init__(self, conn: Any, plan: ShadowPlan, pins: Mapping[str, Any], proposer: str | None,
                 origin: str, batch: str | None, counts: Counter) -> None:
        self.conn, self.plan, self.pins, self.proposer = conn, plan, pins, proposer
        self.origin, self.batch, self.c = origin, batch, counts

    def row_ref(self, row_id: str) -> str:
        return f"shadow:{self.plan.shadow_id}:{row_id}"

    async def support(self, version_id: int | None, shadow_row: str | None, role: str, kind: str, *,
                      signal_id: str | None = None, output_id: str | None = None,
                      target_version_id: int | None = None, verdict: str | None = None,
                      verdict_key: str | None = None, quote: str | None = None, found_in: str | None = None,
                      detail: Mapping[str, Any] | None = None) -> None:
        pin = self.pins.get(signal_id) if signal_id else None
        if kind == "signal":
            self.c["evidence_with_bytes" if pin is not None else "evidence_without_bytes"] += 1
            if pin is not None:
                self.c[f"bytes:{pin.label}"] += 1
        await self.conn.execute(
            _SUPPORT_SQL, version_id, shadow_row, role, kind, signal_id, output_id, target_version_id,
            verdict_key, verdict,
            pin.version_id if pin else None, pin.link_role if pin else None, pin.label if pin else None,
            pin.sha256 if pin else None, pin.text_sha256 if pin else None, quote, found_in,
            json.dumps(dict(detail or {}), default=str, sort_keys=True), self.origin, self.batch, WRITER_HANDLE)
        self.c[f"support:{role}"] += 1

    async def _status(self, assertion_id: str, status: str, version_id: int, reason_code: str,
                      verdict: str | None, row_id: str) -> None:
        from .held_record import STATUS_REASONS

        await self.conn.execute(_STATUS_SQL, assertion_id, status, version_id, reason_code,
                                STATUS_REASONS.get(reason_code), verdict, WRITER_HANDLE, self.row_ref(row_id),
                                self.batch)
        self.c[f"status:{status}"] += 1

    async def _version(self, assertion_id: str, no: int, prev: int | None, reason: str,
                       rec: PlannedRecord) -> int:
        p = self.plan
        vid = await self.conn.fetchval(
            _VERSION_SQL, assertion_id, no, prev, reason, rec.text, text_sha(rec.text),
            json.dumps(rec.slots, default=str, sort_keys=True), rec.valid_from, rec.valid_from_precision,
            rec.valid_to, p.shadow_id, rec.row_id, p.run_id, p.analyst_id, p.contract_version, p.gate_version,
            self.proposer, self.origin, self.batch, WRITER_HANDLE)
        self.c["versions"] += 1
        return int(vid)

    async def _rests_on(self, version_id: int, rec: PlannedRecord, by_row: Mapping[str, _Held]) -> None:
        for sid, quote, found_in in rec.evidence:
            await self.support(version_id, None, "evidence", "signal", signal_id=sid, quote=quote,
                               found_in=found_in)
        if rec.verdict is not None:
            await self.support(version_id, None, "verdict", "verdict", verdict=rec.verdict,
                               verdict_key=self.row_ref(rec.row_id))
        for premise in rec.premises:
            held = by_row.get(premise)
            if held is not None:
                await self.support(version_id, None, "premise", "assertion_version",
                                   target_version_id=held.version_id, detail={"premise_row": premise})

    async def record(self, rec: PlannedRecord, by_row: Mapping[str, _Held]) -> _Held:
        existing = by_row.get(rec.same_as_row) if rec.same_as_row else None
        if existing is None:
            row = await self.conn.fetchrow(_MATCH_SQL, rec.claim_key, self.batch)
            if row is not None:
                existing = _Held(row["id"], row["status"], int(row["version_id"]), int(row["version_no"]),
                                 row["valid_from"], row["valid_to"])
                self.c["match:table"] += 1
        else:
            self.c["match:in_record"] += 1
        if existing is None:
            return await self._new(rec, by_row)
        return await self._match(rec, existing, by_row)

    async def _new(self, rec: PlannedRecord, by_row: Mapping[str, _Held]) -> _Held:
        s = rec.subject
        aid = await self.conn.fetchval(
            _ASSERTION_SQL, rec.class_, rec.type_id, s.kind, s.entity_id, s.key, s.text, rec.speaker_text,
            rec.claim_key, self.plan.target_id, self.origin, self.batch, WRITER_HANDLE)
        self.c["assertions"] += 1
        vid = await self._version(aid, 1, None, "first", rec)
        await self._rests_on(vid, rec, by_row)
        await self._status(aid, rec.status, vid, rec.reason_code, rec.verdict, rec.row_id)
        return _Held(aid, rec.status, vid, 1, rec.valid_from, rec.valid_to)

    async def _match(self, rec: PlannedRecord, held: _Held, by_row: Mapping[str, _Held]) -> _Held:
        new_window = rec.class_ == "absence" and (rec.valid_from, rec.valid_to) != (held.valid_from, held.valid_to)
        if new_window:
            vid = await self._version(held.id, held.version_no + 1, held.version_id, "later_window", rec)
            await self._status(held.id, HELD, vid, "later_window", None, rec.row_id)
            self.c["match:new_version"] += 1
            return _Held(held.id, HELD, vid, held.version_no + 1, rec.valid_from, rec.valid_to)
        await self.support(None, self.row_ref(rec.row_id), "restates", "assertion_version",
                           target_version_id=held.version_id,
                           detail={"verdict": rec.verdict, "row_status": rec.status,
                                   "cited": [sid for sid, _, _ in rec.evidence]})
        self.c["match:restatement_link"] += 1
        if held.status == NOT_HELD and rec.status == HELD:
            await self._rests_on(held.version_id, rec, by_row)
            await self._status(held.id, HELD, held.version_id, "restated_and_verified", rec.verdict, rec.row_id)
            self.c["match:now_held"] += 1
            return _Held(held.id, HELD, held.version_id, held.version_no, held.valid_from, held.valid_to)
        return held
