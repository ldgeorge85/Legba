# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read surface for the Journal Assessor (planning/JOURNAL_ASSESSOR_PLAN.md §9 /
§12 Wave 3; Voices panel step 1, planning/VOICES_PANEL_SPEC.md §3).

Exposes the Journal panel's read surface:

  * ``GET /journal`` — the single OPEN ``entry_kind='consolidation'`` row
    ("Legba's current inner landscape"), prominent, or ``null`` before the
    first consolidation exists; a cursor-paged stream of recent entries
    (``entry`` / ``chronicle`` by default, or the repeatable ``kind`` filter's
    selection, §3.1); and the substrate-derived ``calibration`` verdict
    (forecast_unproven / calibration_thin / BSS / sample sizes) so the §9
    honesty banner is keyed off the live calibration metric, NOT off a
    self-reported payload field (§10).
  * ``GET /journal/{id}`` — a single row, always at ``fields=full`` weight, for
    the Voices reader pane's on-select fetch (§3.3).

FIELDS MODE (§3.3). ``fields=summary`` (list-view weight — id/entry_kind/title/
honesty_flags/period_*/produced_at/analyst_* + the verify score, §3.4) vs.
``fields=full`` (today's behavior — adds body/claims/cited_substrate_refs/
verify_body). Summary requests skip ``_resolve_refs`` entirely (it only hydrates
fields summary rows don't carry) and skip the critique body join (verify_body is
full-only) so the grouped list (§2b of the spec) stays a cheap read regardless of
how many chronicle-weight rows are in the window.

CHIP HYDRATION (§3.6 / §9). A journal claim binds a cited span to a list of bare
substrate UUIDs (``claims[].refs``); the UI renders each as a provenance chip
that deep-links to the cited record. A bare UUID alone can't tell the chip what
KIND of record it points at (situation vs finding vs nexus vs fact …) and there
is no resolve-by-uuid endpoint, so this route resolves every cited ref to its
``(kind, title)`` server-side — a single union-by-id probe across the substrate
tables (UUIDs are globally unique, so each id resolves in at most one table). The
UI then calls ``selectRow(kind, id, label)`` directly without a second round-trip
or a try-each-kind fallback.

VERIFY SCORE (§3.4). Journal rows are verified the same way an ``inline_target``
finding is (V1, the journal verify profile / chronicle gate — see
``dapr_actors.py``'s journal-output verify fire condition): the verify pass
lands a ``kind='critique'`` row on ``analyst_outputs`` whose
``data->>'analyzed_output_id'`` names the journal entry's id and whose
``title LIKE 'Faithfulness verify%'`` — the SAME join shape
``substrate_reads_api.py``'s ``/findings`` lateral uses for findings, mirrored
here for journal rows (which have no analogous join today). ``verify_score`` is
that critique's ``overall_score`` (nullable — ``null`` when no such critique
exists, never fabricated); ``verify_body`` (``fields=full`` only) is the
critique's ``body`` text, which lists each unsupported/contested span as
``  - [judge_contradicted] ...`` / ``  - [judge_unsupported] ...`` /
``  - [no_citation] ...`` lines (``verify.build_faithfulness_critique_payload``)
— the per-claim verdict detail the Voices reader pane renders as a compact
flagged-claim block.

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
from typing import Any, NamedTuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# The full entry_kind vocabulary the `kind` filter accepts (VOICES_PANEL_SPEC
# §3.1). `entry_kind` itself is a free TEXT column (no DB CHECK constraint), so
# this allowlist is the validation boundary — a typo 400s rather than silently
# returning zero rows. `lens`/`lens_diff` are accepted now (harmless — no such
# rows exist yet, §3.1) even though the `lens` secondary filter (§3.2) waits for
# LV-1's `journal_entries.data` column.
_VALID_KINDS = frozenset({"entry", "consolidation", "chronicle", "lens", "lens_diff"})

# Default stream selection when `kind` is omitted: every append tier —
# diary entries, chronicle, and the lens faculties + their diff (LV-2 tail,
# 2026-07-23; the panel's filter rail narrows client-side). Consolidation
# stays slot-only, never a stream row.
_DEFAULT_STREAM_KINDS = ("entry", "chronicle", "lens", "lens_diff")

_VALID_FIELDS = frozenset({"summary", "full"})


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


def _validate_kinds(kind: list[str] | None) -> list[str] | None:
    """Validate the repeatable ``kind`` filter (§3.1). ``None`` (param omitted)
    is passed through so the caller can distinguish "no filter" from an
    explicit (impossible) empty selection. A value outside ``_VALID_KINDS``
    400s — silently returning zero rows on a typo is a worse failure mode."""
    if kind is None:
        return None
    bad = [k for k in kind if k not in _VALID_KINDS]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid kind(s) {bad}; must be one of {sorted(_VALID_KINDS)}",
        )
    return kind


def _validate_fields(fields: str) -> str:
    if fields not in _VALID_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"fields must be one of {sorted(_VALID_FIELDS)}",
        )
    return fields


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


class VerifyResult(NamedTuple):
    """The latest 'Faithfulness verify' critique's gate score + body text for
    one journal entry (§3.4)."""

    score: float
    body: str


async def _read_verify_results(
    conn: Any, entry_ids: list[str],
) -> dict[str, VerifyResult]:
    """Batch-resolve ``{entry_id: VerifyResult}`` for the given journal entry
    ids (§3.4) — one query over the WHOLE id set, mirroring ``_resolve_refs``'s
    single-batched-pass shape rather than a per-row lateral.

    Mirrors ``substrate_reads_api.py``'s ``/findings`` critique join: the
    critique is an ``analyst_outputs`` row with ``kind='critique'`` whose
    ``data->>'analyzed_output_id'`` names the analyzed row (here, the journal
    entry's id — the journal verify profile / V1 chronicle gate stamps this the
    same way the finding verify path does), PINNED to
    ``title LIKE 'Faithfulness verify%'`` so a later generic critique can never
    win the ``produced_at`` race and mask the faithfulness verdict (S8-T2's
    reasoning, reproduced here since journal rows have no existing critique
    join to extend). ``DISTINCT ON`` picks the latest critique per entry.

    An entry with no faithfulness critique is simply absent from the returned
    dict — the caller reads that as ``verify_score=None`` (never fabricated,
    never defaulted to a number, §3.4's "flagged gap, not buildable" position).
    """
    out: dict[str, VerifyResult] = {}
    if not entry_ids:
        return out
    uniq = sorted({str(i) for i in entry_ids})
    sql = """
        SELECT DISTINCT ON (data->>'analyzed_output_id')
               data->>'analyzed_output_id' AS entry_id,
               (data->>'overall_score')::real AS score,
               body
          FROM analyst_outputs
         WHERE kind = 'critique'
           AND data->>'analyzed_output_id' = ANY($1::text[])
           AND data->>'overall_score' IS NOT NULL
           AND title LIKE 'Faithfulness verify%'
         ORDER BY data->>'analyzed_output_id', produced_at DESC, id DESC
    """
    rows = await conn.fetch(sql, uniq)
    for row in rows:
        eid = row["entry_id"]
        score = row["score"]
        if eid is None or score is None:
            continue
        out[eid] = VerifyResult(score=float(score), body=str(row["body"] or ""))
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
    """One ``journal_entries`` row hydrated for the panel, at ``fields=full``
    weight (§3.3)."""

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
    # §3.4 — the 'Faithfulness verify' critique's gate score, nullable (no
    # fabricated number when no such critique exists yet).
    verify_score: float | None = None
    # §3.4, full-only — the critique body text, which names each
    # unsupported/contested span as a ``  - [judge_contradicted] ...`` /
    # ``  - [judge_unsupported] ...`` / ``  - [no_citation] ...`` line; the
    # reader pane's per-claim verdict block parses these.
    verify_body: str | None = None


class JournalEntrySummaryOut(BaseModel):
    """One ``journal_entries`` row at ``fields=summary`` weight (§3.3) — the
    grouped-list read. A DISTINCT model (not nullable fields bolted onto
    ``JournalEntryOut``) so the TS type stays honest about what a summary row
    actually carries: no ``body``/``claims``/``cited_substrate_refs``, and no
    ``_resolve_refs`` pass paid for fields this shape doesn't render.
    """

    id: str
    entry_kind: str
    title: str
    honesty_flags: list[str] = Field(default_factory=list)
    period_start: datetime
    period_end: datetime
    produced_at: datetime
    analyst_id: str | None
    analyst_version: str | None
    verify_score: float | None = None


class CalibrationVerdict(BaseModel):
    """The substrate-derived calibration posture for the §10 honesty banner.

    Read directly from the freshest ``calibration_tracking`` finding — the SAME
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
    """``GET /journal?fields=full`` body (default): the open consolidation (or
    null) + the entry stream + the calibration verdict, each row at full
    (body+claims) weight."""

    consolidation: JournalEntryOut | None
    entries: list[JournalEntryOut]
    next_cursor: str | None
    calibration: CalibrationVerdict


class JournalSummaryOut(BaseModel):
    """``GET /journal?fields=summary`` body — the SAME envelope shape as
    ``JournalOut`` (§3.3), but each row is the list-cheap
    ``JournalEntrySummaryOut`` (no body/claims/cited_substrate_refs, no
    ``_resolve_refs`` pass paid for). A distinct response model (rather than a
    union on ``JournalOut``) so the two shapes never accidentally cross-validate
    against each other."""

    consolidation: JournalEntrySummaryOut | None
    entries: list[JournalEntrySummaryOut]
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


def _hydrate_entry(
    row: Any,
    resolved: dict[str, ResolvedRef],
    verify: dict[str, VerifyResult] | None = None,
) -> JournalEntryOut:
    """Map a ``journal_entries`` row to its ``fields=full`` panel shape, binding
    each claim's + the flat union's refs to their resolved ``(kind, title)``,
    and (§3.4) folding in the row's verify score + critique body when a
    'Faithfulness verify' critique exists for it."""
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
    vr = (verify or {}).get(str(row["id"]))

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
        verify_score=vr.score if vr is not None else None,
        verify_body=vr.body if vr is not None else None,
    )


def _hydrate_summary(
    row: Any, verify: dict[str, VerifyResult] | None = None,
) -> JournalEntrySummaryOut:
    """Map a ``journal_entries`` row to its ``fields=summary`` panel shape
    (§3.3) — no body/claims/cited_substrate_refs, so no ``resolved`` map is
    needed here at all."""
    vr = (verify or {}).get(str(row["id"]))
    return JournalEntrySummaryOut(
        id=str(row["id"]),
        entry_kind=row["entry_kind"],
        title=row["title"],
        honesty_flags=list(row["honesty_flags"] or []),
        period_start=row["period_start"],
        period_end=row["period_end"],
        produced_at=row["produced_at"],
        analyst_id=row["analyst_id"],
        analyst_version=row["analyst_version"],
        verify_score=vr.score if vr is not None else None,
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
    """Read the freshest ``calibration_tracking`` finding and reduce it to the
    banner verdict — the same deterministic logic as the runtime
    ``SubstrateQueryPort.get_calibration`` (the journal honesty post-step's
    source), replicated read-only here so the banner is substrate-keyed.

    B0-3 (read-truth): the writer produces ``kind='finding'`` +
    ``analyst_id='calibration_tracking'`` (nothing writes ``kind='calibration'``)
    and the metrics live one JSONB level down at ``data.data`` (the row's
    ``data`` column is the WHOLE FindingPayload dump)."""
    row = await conn.fetchrow(
        "SELECT id, produced_at, data FROM analyst_outputs "
        "WHERE kind = 'finding' AND analyst_id = 'calibration_tracking' "
        "AND superseded_by IS NULL "
        "ORDER BY produced_at DESC, id DESC LIMIT 1"
    )
    if row is None:
        return CalibrationVerdict(available=False)
    payload = _load_jsonb(row["data"]) or {}
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
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


# The hydrated columns the panel reads from journal_entries at `fields=full`
# (no internal / off-chain columns — derived_from is always empty for journal
# rows, §3.5).
_ENTRY_COLS = (
    "id, entry_kind, title, body, claims, cited_substrate_refs, honesty_flags, "
    "period_start, period_end, produced_at, analyst_id, analyst_version"
)

# The list-cheap column set at `fields=summary` (§3.3) — drops body/claims/
# cited_substrate_refs, the fields that make a chronicle row markedly heavier
# than a diary row and that a future lens_diff.data matrix would only add to.
_SUMMARY_COLS = (
    "id, entry_kind, title, honesty_flags, "
    "period_start, period_end, produced_at, analyst_id, analyst_version"
)


def _stream_where(kind: list[str] | None) -> tuple[list[str], bool]:
    """Build the stream's ``entry_kind`` predicate from the validated `kind`
    filter (§3.1), and report whether the consolidation slot should still be
    fetched.

    * ``kind`` omitted → default: stream = all append tiers
      (entry+chronicle+lens+lens_diff), consolidation slot fetched.
    * ``kind`` provided → the stream filters to exactly the requested kinds
      MINUS ``'consolidation'`` (consolidation is never a stream row, it has
      its own slot); the slot is fetched only when ``'consolidation'`` was
      itself requested — filtering to "just chronicles" also hides the pinned
      slot, per §3.1.
    """
    if kind is None:
        return list(_DEFAULT_STREAM_KINDS), True
    stream_kinds = [k for k in kind if k != "consolidation"]
    want_consolidation = "consolidation" in kind
    return stream_kinds, want_consolidation


def build_journal_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the read-only Journal router bound to the registry deps.

    Mount under ``/api/v1`` so the paths resolve at ``/api/v1/journal`` (the
    stream + consolidation slot + calibration verdict) and
    ``/api/v1/journal/{id}`` (a single row, always full weight — the Voices
    reader pane's on-select fetch, §3.3). Bearer-gated, reading the primary
    Postgres pool via ``deps.descriptor_registry.pg.acquire()`` — the same path
    the substrate-reads + lineage routers use.
    """
    router = APIRouter(tags=["journal"])

    @router.get("/journal", response_model=None)
    async def get_journal(
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        kind: list[str] | None = Query(default=None),
        fields: str = Query(default="full"),
        principal: str = Depends(require_bearer),
    ) -> JournalOut | JournalSummaryOut:
        limit = _validate_limit(limit)
        kind = _validate_kinds(kind)
        fields = _validate_fields(fields)
        summary = fields == "summary"

        stream_kinds, want_consolidation = _stream_where(kind)

        args: list[Any] = []
        # The stream carries the append tiers selected by `kind` (default:
        # ALL of them — diary, chronicle, lens, lens_diff; the card exposes
        # entry_kind so the panel can badge them apart). §3.1: parameterized
        # `= ANY(...)` replaces the old hardcoded `IN ('entry','chronicle')`.
        # The open consolidation stays its own slot below (never a stream row).
        args.append(stream_kinds)
        # 2026-08-02 — the stream honours the soft-close the consolidation slot
        # below has always honoured. Migration 0120 closed the tool-JSON
        # envelopes + empty stubs; without this they keep rendering as Voices
        # cards. The row stays fetchable by id (`/journal/{id}` is a permalink
        # and deliberately unfiltered) — closed, not erased.
        where = ["entry_kind = ANY($1::text[])", "valid_until IS NULL"]
        if cursor is not None:
            cur_at, cur_id = _decode_cursor(cursor)
            args.append(cur_at)
            args.append(cur_id)
            where.append(f"(produced_at, id) < (${len(args) - 1}, ${len(args)})")
        args.append(limit + 1)

        cols = _SUMMARY_COLS if summary else _ENTRY_COLS
        entries_sql = f"""
            SELECT {cols}
              FROM journal_entries
             WHERE {' AND '.join(where)}
             ORDER BY produced_at DESC, id DESC
             LIMIT ${len(args)}
        """

        # The single OPEN consolidation — the partial-unique index in 0048
        # guarantees at most one (valid_until IS NULL AND superseded_by IS
        # NULL). Fetched only when the `kind` filter still wants it (§3.1).
        consolidation_sql = f"""
            SELECT {cols}
              FROM journal_entries
             WHERE entry_kind = 'consolidation'
               AND valid_until IS NULL
               AND superseded_by IS NULL
             ORDER BY produced_at DESC, id DESC
             LIMIT 1
        """

        async with deps.descriptor_registry.pg.acquire() as conn:
            entry_rows = await conn.fetch(entries_sql, *args)
            consolidation_row = (
                await conn.fetchrow(consolidation_sql) if want_consolidation else None
            )
            calibration = await _read_calibration(conn)

            page_rows = list(entry_rows[:limit])
            hydrate_rows = list(page_rows)
            if consolidation_row is not None:
                hydrate_rows = [consolidation_row, *hydrate_rows]

            # §3.4 — the verify-score join, batched over every hydrated row's
            # id, both modes (the summary chip pill needs the score too; only
            # the full body text is summary-skipped).
            verify = await _read_verify_results(
                conn, [str(r["id"]) for r in hydrate_rows],
            )

            resolved: dict[str, ResolvedRef] = {}
            if not summary:
                # §3.3 — skip `_resolve_refs` entirely for summary requests; it
                # exists only to hydrate fields summary rows don't carry.
                resolved = await _resolve_refs(conn, _all_ref_ids(hydrate_rows))

        next_cursor: str | None = None

        if summary:
            consolidation_summary = (
                _hydrate_summary(consolidation_row, verify)
                if consolidation_row is not None
                else None
            )
            entries_summary = [_hydrate_summary(r, verify) for r in page_rows]
            if len(entry_rows) > limit and entries_summary:
                last = entries_summary[-1]
                next_cursor = _encode_cursor(last.produced_at, last.id)
            return JournalSummaryOut(
                consolidation=consolidation_summary,
                entries=entries_summary,
                next_cursor=next_cursor,
                calibration=calibration,
            )

        consolidation = (
            _hydrate_entry(consolidation_row, resolved, verify)
            if consolidation_row is not None
            else None
        )
        entries = [_hydrate_entry(r, resolved, verify) for r in page_rows]
        if len(entry_rows) > limit and entries:
            last = entries[-1]
            next_cursor = _encode_cursor(last.produced_at, last.id)

        return JournalOut(
            consolidation=consolidation,
            entries=entries,
            next_cursor=next_cursor,
            calibration=calibration,
        )

    @router.get("/journal/{entry_id}", response_model=JournalEntryOut)
    async def get_journal_entry(
        entry_id: UUID,
        principal: str = Depends(require_bearer),
    ) -> JournalEntryOut:
        """A single ``journal_entries`` row at full weight (§3.3) — the Voices
        reader pane's on-select fetch (``GET /journal/{id}``), symmetric with
        the existing resolve machinery rather than a single-row-via-list-filter
        workaround."""
        sql = f"SELECT {_ENTRY_COLS} FROM journal_entries WHERE id = $1"
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(sql, entry_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"journal entry {entry_id} not found",
                )
            resolved = await _resolve_refs(conn, _all_ref_ids([row]))
            verify = await _read_verify_results(conn, [str(row["id"])])
        return _hydrate_entry(row, resolved, verify)

    return router
