# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows: the writer, beside the finding, in its transaction.

``writes.write_analyst_output`` calls :func:`dual_write_citation_rows` for a
NEW finding, on the same connection and inside the same transaction as the
``analyst_outputs`` INSERT (``LEGBA_CITATION_ROWS``, default on). It writes
the five tables of migration ``20261115T1828_citations_as_rows`` from the
finding's own stored fields (``body`` and ``data['citations']``) plus, for a
desk run, the index the run was handed. Today's fields are untouched.

* One inner savepoint around the rows: a failure in them is logged and the
  finding still lands (a row-shape bug must never cost a finding), EXCEPT the
  refusal below, which is raised on purpose.
* Unresolvable markers are logged (``citation_unresolved``, with a reason, and
  a count on the finding's receipt row), not refused. The refusal is the
  switch ``LEGBA_CITATION_REFUSE_UNRESOLVED`` (default OFF, SEAMS
  #citation-refusal): on, a finding with any unresolved marker raises
  :class:`UnresolvedCitationRefused` before anything is written, which rolls
  back the finding's own INSERT with it.
* Idempotent on ``finding_citation_receipts.finding_id``: a second call for
  the same finding writes nothing (the backfill relies on it).
* ``written_by``: the finding's writer, the deterministic
  handler (``rule:<id>``) or the seat (``agent:<id>@<version>``)
  (``principals.output_writer_handle``); the backfill names
  ``rule:citation_rows_backfill``.
* Byte pins: a signal's or observation's receipt version is chosen by
  ``byte_path.classify`` (the one rule the Record sheet and the drift pass
  read); when every version it links to has lost its bytes, the received one
  is still pinned (its hash names the exact bytes) and labelled
  ``no_bytes_kept``.

Three indexed reads per finding (the targets' existence, by primary key, one
query per kind present; the receipts of the cited material; nothing else) and
five INSERTs.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from .byte_path import classify
from .principals import PrincipalError, output_writer_handle, validate_handle
from .citation_rows import (
    CitationPlan,
    HandedRef,
    SupportRow,
    TargetSpec,
    UnresolvedRow,
    plan_citation_rows,
    target_specs,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CITATION_ROWS_ENV",
    "REFUSE_UNRESOLVED_ENV",
    "UnresolvedCitationRefused",
    "WrittenCitationRows",
    "citation_rows_enabled",
    "dual_write_citation_rows",
    "pin_material",
    "refuse_unresolved_enabled",
    "write_citation_rows",
]

#: The dual write. Default ON; ``0`` stops it (the lane's reversal: nothing
#: reads the rows yet but the Record sheet's support route, which falls back).
CITATION_ROWS_ENV = "LEGBA_CITATION_ROWS"
#: The refusal. Default OFF (SEAMS #citation-refusal).
REFUSE_UNRESOLVED_ENV = "LEGBA_CITATION_REFUSE_UNRESOLVED"

_OFF = {"0", "false", "no", "off"}
_ON = {"1", "true", "yes", "on"}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def citation_rows_enabled() -> bool:
    return os.environ.get(CITATION_ROWS_ENV, "1").strip().lower() not in _OFF


def refuse_unresolved_enabled() -> bool:
    return os.environ.get(REFUSE_UNRESOLVED_ENV, "0").strip().lower() in _ON


class UnresolvedCitationRefused(ValueError):
    """A finding carries a marker or citation that names no record, and the
    refusal switch is on. Raised before any row is written."""

    def __init__(self, finding_id: Any, unresolved: Sequence[UnresolvedRow]) -> None:
        reasons: dict[str, int] = {}
        for u in unresolved:
            reasons[u.reason] = reasons.get(u.reason, 0) + 1
        super().__init__(
            f"finding {finding_id}: {len(unresolved)} unresolvable citation(s) {reasons}; "
            f"{REFUSE_UNRESOLVED_ENV} is on")
        self.reasons = reasons


@dataclass(frozen=True)
class WrittenCitationRows:
    finding_id: str
    sentences: int
    supports: int
    unresolved: int
    handed: int
    handed_recorded: bool


# ---------------------------------------------------------------------------
# Reads: does each target exist; which bytes was each material record read from
# ---------------------------------------------------------------------------


async def _existing(conn: Any, plan: CitationPlan, specs: Mapping[str, TargetSpec]) -> set[tuple[str, str]]:
    wanted: dict[str, set[str]] = {}
    for s in plan.supports:
        wanted.setdefault(s.target.kind, set()).add(s.target.key)
    found: set[tuple[str, str]] = set()
    for kind, keys in wanted.items():
        spec = specs[kind]
        if not _IDENT.match(spec.table):
            raise ValueError(f"citation_target {kind}: table {spec.table!r} is not a plain identifier")
        if spec.key == "uuid":
            sql = f"SELECT id::text AS k FROM {spec.table} WHERE id = ANY($1::uuid[])"
        elif spec.table == "desk_baselines":
            sql = "SELECT desk_id || ':' || metric AS k FROM desk_baselines WHERE desk_id || ':' || metric = ANY($1::text[])"
        else:
            raise ValueError(f"citation_target {kind}: no existence read for a {spec.key} key on {spec.table}")
        for r in await conn.fetch(sql, sorted(keys)):
            found.add((kind, str(r["k"])))
    return found


_PIN_SQL = """
SELECT r.record_kind, r.record_id::text AS record_id, r.role, r.version_id, r.locator,
       v.role AS version_role, v.object_ref, v.bytes_pruned_at,
       (v.extracted_text IS NOT NULL) AS has_text, v.sha256, v.text_sha256
  FROM fetch_version_records r
  JOIN fetch_versions v ON v.id = r.version_id
 WHERE r.record_kind = $1 AND r.record_id = ANY($2::uuid[])
"""

_RECEIVED_ORDER = {"page": 0, "response": 1, "inbound": 2, "archived": 3}


@dataclass(frozen=True)
class _Pin:
    version_id: int
    link_role: str
    label: str
    sha256: str | None
    text_sha256: str | None


async def pin_material(conn: Any, kind: str, keys: Sequence[str]) -> dict[str, _Pin]:
    """``{key: pin}`` for material records of one kind (``signal`` /
    ``observation``): the receipt version ``byte_path.classify`` picks, or,
    when no version keeps its bytes, the received one by its hash
    (``no_bytes_kept``). One query. A key with no receipt is absent. Also the
    held family's pin, so both tables pin by one rule."""
    out: dict[str, _Pin] = {}
    rows_by: dict[str, list[Mapping[str, Any]]] = {}
    for r in await conn.fetch(_PIN_SQL, kind, sorted(set(keys))):
        rows_by.setdefault(str(r["record_id"]), []).append(r)
    for key, rows in rows_by.items():
        state = classify(key, rows)
        vid = state.version_id
        role = state.link_role
        if vid is None:  # no bytes held: pin the received version by its hash
            first = min(rows, key=lambda r: (_RECEIVED_ORDER.get(str(r["role"]), 9), int(r["version_id"])))
            vid, role = int(first["version_id"]), str(first["role"])
        row = next(r for r in rows if int(r["version_id"]) == vid)
        out[key] = _Pin(vid, str(role), state.label, row["sha256"], row["text_sha256"])
    return out


async def _pins(conn: Any, plan: CitationPlan, specs: Mapping[str, TargetSpec]) -> dict[tuple[str, str], _Pin]:
    out: dict[tuple[str, str], _Pin] = {}
    by_kind: dict[str, set[str]] = {}
    for s in plan.supports:
        if specs[s.target.kind].material:
            by_kind.setdefault(s.target.kind, set()).add(s.target.key)
    for kind, keys in by_kind.items():
        for key, pin in (await pin_material(conn, kind, sorted(keys))).items():
            out[(kind, key)] = pin
    return out


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------

_RECEIPT_SQL = """
INSERT INTO finding_citation_receipts (
    finding_id, run_id, analyst_id, target_id, produced_at, sentences, cited_sentences,
    markers, supports, unresolved, handed_recorded, handed, origin, backfill_batch, foreign_markers,
    written_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, legba_principal($16))
ON CONFLICT (finding_id) DO NOTHING
RETURNING finding_id, written_by
"""

_SENTENCES_SQL = """
INSERT INTO finding_sentences (
    finding_id, ordinal, char_start, char_end, text, text_sha256, kind, marker_count,
    run_id, analyst_id, origin, backfill_batch, written_by)
SELECT $1, o, s, e, t, h, k, m, $2, $3, $4, $5, $13::uuid
  FROM unnest($6::int[], $7::int[], $8::int[], $9::text[], $10::text[], $11::text[], $12::int[])
       AS u(o, s, e, t, h, k, m)
RETURNING id, ordinal
"""

_SUPPORT_SQL = """
INSERT INTO citation_support (
    finding_id, sentence_id, sentence_ordinal, marker, role, target_kind, via_block,
    signal_id, observation_id, output_id, situation_id, event_id, hypothesis_id, inquiry_id, legacy_key,
    fetch_version_id, fetch_link_role, bytes_label, bytes_sha256, text_sha256,
    quote, shown_field, shown_sha256, stance, speaker, source_id,
    run_id, analyst_id, origin, backfill_batch, written_by)
VALUES ($1, $2, $3, $4, $5, $6, $7,
        $8::uuid, $9::uuid, $10::uuid, $11::uuid, $12::uuid, $13::uuid, $14::uuid, $15,
        $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31::uuid)
"""

_UNRESOLVED_SQL = """
INSERT INTO citation_unresolved (
    finding_id, sentence_id, sentence_ordinal, marker, reason, ref_kind, ref_value,
    run_id, analyst_id, origin, backfill_batch, written_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::uuid)
"""

_HANDED_SQL = """
INSERT INTO run_handed (run_id, finding_id, target_kind, via_block, ids, keys, ordinals, origin, backfill_batch,
                        written_by)
VALUES ($1, $2, $3, $4, $5::uuid[], $6::text[], $7::int[], $8, $9, $10::uuid)
"""

_TYPED = ("signal_id", "observation_id", "output_id", "situation_id", "event_id",
          "hypothesis_id", "inquiry_id", "legacy_key")


def _typed(s: SupportRow) -> list[str | None]:
    return [s.target.key if s.target.column == col else None for col in _TYPED]


async def write_citation_rows(
    conn: Any,
    *,
    finding_id: UUID | str,
    body: str | None,
    citations: Sequence[Mapping[str, Any]] | None,
    produced_at: datetime,
    run_id: UUID | str | None = None,
    analyst_id: str | None = None,
    target_id: str | None = None,
    handed: Sequence[HandedRef] | None = None,
    origin: str = "live",
    batch: str | None = None,
    written_by: str | None = None,
    specs: Mapping[str, TargetSpec] | None = None,
    plan: CitationPlan | None = None,
) -> WrittenCitationRows | None:
    """Write one finding's rows. ``None`` when its receipt row already exists
    (written before: nothing is written twice). The caller owns the
    transaction; this function opens none.

    ``written_by`` is the writer's principal HANDLE: resolved
    once by ``legba_principal`` in the receipt INSERT, and that id stamped on
    every other row of the write. ``None`` leaves the column NULL."""
    specs = specs if specs is not None else target_specs()
    plan = plan if plan is not None else plan_citation_rows(body, citations, handed=handed, specs=specs)
    fid = str(finding_id)
    rid = str(run_id) if run_id is not None else None
    existing = await _existing(conn, plan, specs)
    missing = [
        UnresolvedRow(s.sentence_ordinal, s.marker, "target_missing", s.target.kind, s.target.key)
        for s in plan.supports if (s.target.kind, s.target.key) not in existing
    ]
    unresolved = list(plan.unresolved) + missing
    if unresolved and origin == "live" and refuse_unresolved_enabled():
        raise UnresolvedCitationRefused(fid, unresolved)
    pins = await _pins(conn, plan, specs)
    handed_groups = plan.handed or ()
    receipt = await conn.fetchrow(
        _RECEIPT_SQL, fid, rid, analyst_id, target_id, produced_at, len(plan.sentences),
        plan.cited_sentences, plan.markers, len(plan.supports), len(unresolved),
        plan.handed is not None, plan.handed_count, origin, batch, plan.foreign_markers,
        validate_handle(written_by) if written_by is not None else None)
    if receipt is None:
        return None
    wb = receipt["written_by"]
    sentence_ids: dict[int, int] = {}
    if plan.sentences:
        ss = plan.sentences
        for r in await conn.fetch(
            _SENTENCES_SQL, fid, rid, analyst_id, origin, batch,
            [s.ordinal for s in ss], [s.char_start for s in ss], [s.char_end for s in ss],
            [s.text for s in ss], [s.text_sha256 for s in ss], [s.kind for s in ss],
            [s.marker_count for s in ss], wb,
        ):
            sentence_ids[int(r["ordinal"])] = int(r["id"])
    if plan.supports:
        rows = []
        for s in plan.supports:
            pin = pins.get((s.target.kind, s.target.key))
            rows.append((
                fid, sentence_ids.get(s.sentence_ordinal) if s.sentence_ordinal is not None else None,
                s.sentence_ordinal, s.marker, s.role, s.target.kind, s.target.via_block,
                *_typed(s),
                pin.version_id if pin else None, pin.link_role if pin else None,
                (pin.label if pin else ("no_bytes_kept" if specs[s.target.kind].material else None)),
                pin.sha256 if pin else None, pin.text_sha256 if pin else None,
                s.quote, s.shown_field, s.shown_sha256, s.stance, s.speaker, s.source_id,
                rid, analyst_id, origin, batch, wb,
            ))
        await conn.executemany(_SUPPORT_SQL, rows)
    if unresolved:
        await conn.executemany(_UNRESOLVED_SQL, [
            (fid, sentence_ids.get(u.sentence_ordinal) if u.sentence_ordinal is not None else None,
             u.sentence_ordinal, u.marker, u.reason, u.ref_kind, u.ref_value, rid, analyst_id, origin, batch,
             wb)
            for u in unresolved
        ])
    if handed_groups and rid is not None:
        await conn.executemany(_HANDED_SQL, [
            (rid, fid, g.kind, g.via_block,
             list(g.keys) if specs[g.kind].key == "uuid" else None,
             list(g.keys) if specs[g.kind].key != "uuid" else None,
             list(g.ordinals), origin, batch, wb)
            for g in handed_groups
        ])
    return WrittenCitationRows(fid, len(plan.sentences), len(plan.supports), len(unresolved),
                               plan.handed_count, plan.handed is not None)


async def dual_write_citation_rows(
    conn: Any,
    *,
    finding_id: UUID | str,
    payload: Any,
    produced_at: datetime,
    run_id: UUID | str | None,
    analyst_id: str | None,
    target_id: str | None,
    handed: Sequence[HandedRef] | None = None,
    analyst_version: str | None = None,
) -> WrittenCitationRows | None:
    """The finding write's hook. Runs inside the caller's transaction in its
    own savepoint; every failure but the refusal is logged and swallowed."""
    data = getattr(payload, "data", None)
    citations = data.get("citations") if isinstance(data, Mapping) else None
    if not isinstance(citations, Sequence) or isinstance(citations, (str, bytes)):
        citations = ()
    body = getattr(payload, "body", None)
    try:
        written_by = output_writer_handle(analyst_id, analyst_version)
    except PrincipalError as exc:  # a malformed id/version names no writer; the rows still land
        logger.warning("citation_rows.writer_unnamed finding=%s err=%s", finding_id, exc)
        written_by = None
    try:
        specs = target_specs()
        plan = plan_citation_rows(body, citations, handed=handed, specs=specs)
        async with conn.transaction():
            return await write_citation_rows(
                conn, finding_id=finding_id, body=body, citations=citations,
                produced_at=produced_at, run_id=run_id, analyst_id=analyst_id,
                target_id=target_id, handed=handed, written_by=written_by,
                specs=specs, plan=plan)
    except UnresolvedCitationRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — a row-shape bug must never cost a finding
        logger.warning("citation_rows.dual_write_failed finding=%s err_class=%s err=%s",
                       finding_id, type(exc).__name__, exc)
        return None
