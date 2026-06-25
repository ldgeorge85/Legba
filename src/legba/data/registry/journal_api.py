# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read surface for the Journal Assessor (planning/JOURNAL_ASSESSOR_PLAN.md §9 /
§12 Wave 3).

Exposes ONE read-only GET endpoint — ``/journal`` — that backs the UI Journal
panel:

  * the single OPEN ``entry_kind='consolidation'`` row ("Legba's current inner
    landscape"), prominent, or ``null`` before the first consolidation exists;
  * a cursor-paged stream of recent ``entry_kind='entry'`` rows; and
  * the substrate-derived ``calibration`` verdict (forecast_unproven /
    calibration_thin / BSS / sample sizes) so the §9 honesty banner is keyed off
    the live calibration metric, NOT off a self-reported payload field (§10).

CHIP HYDRATION (§3.6 / §9). A journal claim binds a cited span to a list of bare
substrate UUIDs (``claims[].refs``); the UI renders each as a provenance chip
that deep-links to the cited record. A bare UUID alone can't tell the chip what
KIND of record it points at (situation vs finding vs nexus vs fact …) and there
is no resolve-by-uuid endpoint, so this route resolves every cited ref to its
``(kind, title)`` server-side — a single union-by-id probe across the substrate
tables (UUIDs are globally unique, so each id resolves in at most one table). The
UI then calls ``selectRow(kind, id, label)`` directly without a second round-trip
or a try-each-kind fallback.

OFF-CHAIN INVARIANT (§3.1 / §3.5). This route reads ``journal_entries`` directly
and never the lineage catalog; the chip walk is UP-only (entry → what it cites)
and is built purely from the in-payload ``claims`` / ``cited_substrate_refs``.
The journal row is never surfaced as a downstream lineage node.

Wiring convention mirrors ``substrate_reads_api.py``: a small router built via
``build_journal_router(deps)``, the shared ``RegistryAPIDeps`` bundle, the same
``require_bearer`` gate, and the same opaque ``(produced_at, id)`` cursor.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_LIMIT = 25
MAX_LIMIT = 200


# ---------------------------------------------------------------------------
# Cursor helpers (same opaque (produced_at, id) scheme as substrate_reads_api).
# ---------------------------------------------------------------------------


def _encode_cursor(produced_at: datetime, row_id: UUID | str) -> str:
    payload = json.dumps(
        {"produced_at": produced_at.isoformat(), "id": str(row_id)},
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(decoded)
        produced_at = datetime.fromisoformat(obj["produced_at"])
        row_id = UUID(obj["id"])
    except Exception as exc:  # pragma: no cover - validation path
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc}",
        )
    return produced_at, row_id


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"limit must be in [1, {MAX_LIMIT}]",
        )
    return limit


# ---------------------------------------------------------------------------
# Ref resolution — bare UUID → (kind, title).
#
# Each entry in this table is one substrate table carrying universal-provenance
# columns, with the SQL expressions that yield the row's kind label and a human
# title. Mirrors lineage_api._SUBSTRATE_TABLES but is local + read-only here so
# the journal route stays self-contained (and so a journal-side change never
# perturbs the lineage walk's catalog). `nexuses` is ADDED here (it is NOT in
# the lineage catalog) because a journal claim legitimately cites a signed nexus
# (§9), and `journal_entries` itself is deliberately ABSENT — a chip never
# resolves to another journal row (§3.5).
# ---------------------------------------------------------------------------


_REF_TABLES: tuple[tuple[str, str, str], ...] = (
    # (table, kind_expr, title_expr)
    ("analyst_outputs", "kind", "title"),
    ("situations", "'situation'", "name"),
    ("facts", "'fact'", "subject || ' ' || predicate || ' ' || value"),
    ("nexuses", "'nexus'", "label"),
    ("hypotheses", "'hypothesis'", "LEFT(thesis, 240)"),
    (
        "signals",
        "'signal'",
        "payload->>'title'",
    ),
)


class ResolvedRef(BaseModel):
    """A cited substrate UUID resolved to its kind + a human label, so the UI
    chip can deep-link via ``selectRow(kind, id, label)`` without a second probe.

    ``kind`` is ``"unknown"`` when the id resolves in no substrate table (a
    superseded / pruned / cross-environment ref): the chip still renders (the
    citation is never hidden) and the click coerces to a walkable Inspector path
    rather than dead-ending.
    """

    id: str
    kind: str
    title: str | None = None


async def _resolve_refs(conn: Any, ids: list[str]) -> dict[str, ResolvedRef]:
    """Resolve a flat list of bare UUIDs to ``{id: ResolvedRef}``.

    One indexed lookup per substrate table over the WHOLE id set (UUIDs are
    globally unique, so a given id matches in at most one table). Ids that match
    nowhere get a ``kind='unknown'`` placeholder so the UI always has a chip for
    every cited ref (the citation is the honesty surface — never dropped, §9).
    """
    out: dict[str, ResolvedRef] = {}
    if not ids:
        return out
    # Dedupe + drop non-UUID strings defensively (a malformed payload ref must
    # not blow up the whole read).
    uniq: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        s = str(raw)
        if s in seen:
            continue
        seen.add(s)
        try:
            UUID(s)
        except (ValueError, AttributeError, TypeError):
            continue
        uniq.append(s)

    remaining = set(uniq)
    for table, kind_expr, title_expr in _REF_TABLES:
        if not remaining:
            break
        sql = (
            f"SELECT id, ({kind_expr}) AS rk, ({title_expr}) AS rt "
            f"FROM {table} WHERE id = ANY($1::uuid[])"
        )
        try:
            rows = await conn.fetch(sql, list(remaining))
        except Exception:  # pragma: no cover - a missing table never aborts the read
            continue
        for row in rows:
            rid = str(row["id"])
            title = row["rt"]
            out[rid] = ResolvedRef(
                id=rid,
                kind=str(row["rk"]),
                title=str(title) if title is not None else None,
            )
            remaining.discard(rid)

    # Anything still unresolved → an honest placeholder chip.
    for rid in remaining:
        out[rid] = ResolvedRef(id=rid, kind="unknown", title=None)
    return out


# ---------------------------------------------------------------------------
# Response models.
# ---------------------------------------------------------------------------


class JournalClaimOut(BaseModel):
    """One cited claim — a span of the body bound to its resolved refs (§3.6).

    ``kind`` is the CLAIM kind (``fact`` | ``perspective``), distinct from a
    ref's substrate kind. A ``[needs_citation]``-prefixed ``text_span`` is left
    verbatim (the UI renders the prefix in the unverified-perspective style; the
    span is NEVER hidden — §4.5). ``refs`` are resolved so each chip knows where
    to deep-link.
    """

    text_span: str
    kind: str
    refs: list[ResolvedRef] = Field(default_factory=list)


class JournalEntryOut(BaseModel):
    """One ``journal_entries`` row hydrated for the panel."""

    id: str
    entry_kind: str
    title: str
    body: str
    claims: list[JournalClaimOut] = Field(default_factory=list)
    cited_substrate_refs: list[ResolvedRef] = Field(default_factory=list)
    honesty_flags: list[str] = Field(default_factory=list)
    period_start: datetime
    period_end: datetime
    produced_at: datetime
    analyst_id: str | None
    analyst_version: str | None


class CalibrationVerdict(BaseModel):
    """The substrate-derived calibration posture for the §10 honesty banner.

    Read directly from the freshest ``kind='calibration'`` finding — the SAME
    source the journal's deterministic honesty post-step keys off — so the banner
    can CROSS-CHECK the stored ``honesty_flags`` against live metrics rather than
    trusting a self-reported field. ``available`` is false before any calibration
    finding exists, in which case both legs read unproven (absence of proof is
    not proof of skill, §10).
    """

    available: bool
    forecast_unproven: bool = True
    calibration_thin: bool = True
    brier_skill_score: float | None = None
    exogenous_sample_size: int | None = None
    forecast_acute_sample_size: int | None = None
    forecast_acute_status: str | None = None
    produced_at: datetime | None = None


class JournalOut(BaseModel):
    """``GET /journal`` body: the open consolidation (or null) + the entry stream
    + the calibration verdict."""

    consolidation: JournalEntryOut | None
    entries: list[JournalEntryOut]
    next_cursor: str | None
    calibration: CalibrationVerdict


# ---------------------------------------------------------------------------
# Row hydration.
# ---------------------------------------------------------------------------


def _load_jsonb(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _hydrate_entry(row: Any, resolved: dict[str, ResolvedRef]) -> JournalEntryOut:
    """Map a ``journal_entries`` row to its panel shape, binding each claim's +
    the flat union's refs to their resolved ``(kind, title)``."""
    raw_claims = _load_jsonb(row["claims"]) or []
    claims: list[JournalClaimOut] = []
    if isinstance(raw_claims, list):
        for c in raw_claims:
            if not isinstance(c, dict):
                continue
            span = c.get("text_span")
            if not isinstance(span, str) or not span:
                continue
            ref_ids = [str(r) for r in (c.get("refs") or [])]
            claims.append(
                JournalClaimOut(
                    text_span=span,
                    kind=str(c.get("kind") or "fact"),
                    refs=[resolved[r] for r in ref_ids if r in resolved],
                )
            )

    cited_ids = [str(r) for r in (row["cited_substrate_refs"] or [])]
    cited = [resolved[r] for r in cited_ids if r in resolved]

    return JournalEntryOut(
        id=str(row["id"]),
        entry_kind=row["entry_kind"],
        title=row["title"],
        body=row["body"],
        claims=claims,
        cited_substrate_refs=cited,
        honesty_flags=list(row["honesty_flags"] or []),
        period_start=row["period_start"],
        period_end=row["period_end"],
        produced_at=row["produced_at"],
        analyst_id=row["analyst_id"],
        analyst_version=row["analyst_version"],
    )


def _all_ref_ids(rows: list[Any]) -> list[str]:
    """Flatten every cited ref id across a set of rows (claims-bound + the flat
    union) into one de-dupable list for a SINGLE batched resolution pass."""
    ids: list[str] = []
    for row in rows:
        ids.extend(str(r) for r in (row["cited_substrate_refs"] or []))
        raw_claims = _load_jsonb(row["claims"]) or []
        if isinstance(raw_claims, list):
            for c in raw_claims:
                if isinstance(c, dict):
                    ids.extend(str(r) for r in (c.get("refs") or []))
    return ids


async def _read_calibration(conn: Any) -> CalibrationVerdict:
    """Read the freshest ``kind='calibration'`` finding and reduce it to the
    banner verdict — the same deterministic logic as the runtime
    ``SubstrateQueryPort.get_calibration`` (the journal honesty post-step's
    source), replicated read-only here so the banner is substrate-keyed."""
    row = await conn.fetchrow(
        "SELECT id, produced_at, data FROM analyst_outputs "
        "WHERE kind = 'calibration' "
        "ORDER BY produced_at DESC, id DESC LIMIT 1"
    )
    if row is None:
        return CalibrationVerdict(available=False)
    data = _load_jsonb(row["data"]) or {}
    bss = data.get("brier_skill_score")
    ready = bool(data.get("forecast_acute_ready"))
    degenerate = bool(data.get("forecast_acute_degenerate"))
    forecast_proven = (
        ready and not degenerate and isinstance(bss, (int, float)) and bss > 0.0
    )
    exo_n = data.get("exogenous_sample_size")
    calibration_thin = not isinstance(exo_n, int) or exo_n < 5
    return CalibrationVerdict(
        available=True,
        forecast_unproven=not forecast_proven,
        calibration_thin=calibration_thin,
        brier_skill_score=bss if isinstance(bss, (int, float)) else None,
        exogenous_sample_size=exo_n if isinstance(exo_n, int) else None,
        forecast_acute_sample_size=(
            data.get("forecast_acute_sample_size")
            if isinstance(data.get("forecast_acute_sample_size"), int)
            else None
        ),
        forecast_acute_status=(
            str(data["forecast_acute_status"])
            if data.get("forecast_acute_status") is not None
            else None
        ),
        produced_at=row["produced_at"]
        if isinstance(row["produced_at"], datetime)
        else None,
    )


# ---------------------------------------------------------------------------
# Router factory.
# ---------------------------------------------------------------------------


# The hydrated columns the panel reads from journal_entries (no internal /
# off-chain columns — derived_from is always empty for journal rows, §3.5).
_ENTRY_COLS = (
    "id, entry_kind, title, body, claims, cited_substrate_refs, honesty_flags, "
    "period_start, period_end, produced_at, analyst_id, analyst_version"
)


def build_journal_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the read-only Journal router bound to the registry deps.

    Mount under ``/api/v1`` so the path resolves at ``/api/v1/journal``. One GET,
    bearer-gated, reading the primary Postgres pool via
    ``deps.descriptor_registry.pg.acquire()`` — the same path the substrate-reads
    + lineage routers use.
    """
    router = APIRouter(tags=["journal"])

    @router.get("/journal", response_model=JournalOut)
    async def get_journal(
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> JournalOut:
        limit = _validate_limit(limit)

        args: list[Any] = []
        where = ["entry_kind = 'entry'"]
        if cursor is not None:
            cur_at, cur_id = _decode_cursor(cursor)
            args.append(cur_at)
            args.append(cur_id)
            where.append(f"(produced_at, id) < (${len(args) - 1}, ${len(args)})")
        args.append(limit + 1)

        entries_sql = f"""
            SELECT {_ENTRY_COLS}
              FROM journal_entries
             WHERE {' AND '.join(where)}
             ORDER BY produced_at DESC, id DESC
             LIMIT ${len(args)}
        """

        # The single OPEN consolidation — the partial-unique index in 0048
        # guarantees at most one (valid_until IS NULL AND superseded_by IS NULL).
        consolidation_sql = f"""
            SELECT {_ENTRY_COLS}
              FROM journal_entries
             WHERE entry_kind = 'consolidation'
               AND valid_until IS NULL
               AND superseded_by IS NULL
             ORDER BY produced_at DESC, id DESC
             LIMIT 1
        """

        async with deps.descriptor_registry.pg.acquire() as conn:
            entry_rows = await conn.fetch(entries_sql, *args)
            consolidation_row = await conn.fetchrow(consolidation_sql)
            calibration = await _read_calibration(conn)

            page_rows = list(entry_rows[:limit])
            hydrate_rows = list(page_rows)
            if consolidation_row is not None:
                hydrate_rows = [consolidation_row, *hydrate_rows]
            # ONE batched ref-resolution pass over the consolidation + the page.
            resolved = await _resolve_refs(conn, _all_ref_ids(hydrate_rows))

        consolidation = (
            _hydrate_entry(consolidation_row, resolved)
            if consolidation_row is not None
            else None
        )
        entries = [_hydrate_entry(r, resolved) for r in page_rows]

        next_cursor: str | None = None
        if len(entry_rows) > limit and entries:
            last = entries[-1]
            next_cursor = _encode_cursor(last.produced_at, last.id)

        return JournalOut(
            consolidation=consolidation,
            entries=entries,
            next_cursor=next_cursor,
            calibration=calibration,
        )

    return router
