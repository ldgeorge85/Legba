# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-target substrate-read endpoints for the daily-driver UI panels.

This module exports three GET endpoints — `findings`, `situations`,
`signals` — that read directly from their respective public tables on
the primary Postgres substrate. They back the new
P-1..P-3 panels described in `plans/legba_panels_redesign_2026_05_28.md`
and follow the same wiring convention as `v3_api.py`: a small router
constructed via `build_substrate_reads_router(deps)`, the shared
`RegistryAPIDeps` bundle, and the same `require_bearer` gate used by the
rest of the registry surface.

Mount this router from `server.py` next to the existing v3 router. The
parent session integrates with:

    from .substrate_reads_api import build_substrate_reads_router
    app.include_router(build_substrate_reads_router(deps), prefix="/api/v1")

Design rules (Lewis's no-stub/no-fake rule):

  * Pydantic response models mirror the underlying table columns one
    for one, minus pure internal/transport columns (none of these
    tables carry a `prev_receipt_hash`-style internal field today). If
    a panel needs a value the table doesn't store, we omit the field —
    we do not synthesize.

  * Cursor pagination uses an opaque base64 of `(produced_at_iso, id)`
    so a client can walk a strictly monotonic `produced_at DESC, id
    DESC` ordering even across rows that share the same `produced_at`
    (which is common for batch inserts). `next_cursor` is `null` when
    the result set was smaller than the requested limit.

  * All filters are optional. `since` is ISO-8601 datetime; `limit`
    defaults to 50 with a hard cap of 500.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..archive import sha256_from_object_ref
from ..findings_projection import project_citations
from ..provenance import access as _access
from ..provenance.kinds import structural_badge
from ..provenance.verify import structural_verify_gate_enabled
from .api import RegistryAPIDeps, require_bearer
from .substrate_reads_folds import (
    CRITIC_SCORE_CTE as _CRITIC_SCORE_CTE,
    FACET_SCAN_CAP as _FACET_SCAN_CAP,
    FAITHFULNESS_VERIFICATION_CTE as _FAITHFULNESS_VERIFICATION_CTE,
    STRUCTURAL_BADGE_CTE as _STRUCTURAL_BADGE_CTE,
    STRUCTURAL_VERIFICATION_CTE as _STRUCTURAL_VERIFICATION_CTE,
)
from .unit_correctness_api import build_unit_correctness_router

# E-1 — the system-wide verification floor (the 0.50 decision), MIRRORED from
# ``analysts.deterministic_handlers.scorecard_banding.FAITH_FLOOR``. A local
# copy on purpose (the registry-slim rule — importing the handler package
# would drag ~20 runtime sub-handler modules into this route module; same
# rationale as verify.py's slim-safe local maps). Lockstep is test-enforced:
# tests/data_pkg/test_substrate_reads_api.py asserts equality with the source.
_FAITH_FLOOR = 0.50


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_LIMIT = 50
MAX_LIMIT = 500


# Substrate enum values shared by `analyst_outputs.severity` and the
# `situations` lifecycle taxonomy. We surface these as `Literal[...]`
# query types so the OpenAPI doc enumerates them.
Severity = Literal["low", "medium", "high", "critical"]
SituationState = Literal["active", "resolved", "escalating"]
# `public.fact_contention.status` lifecycle (migration 0055): a group is
# opened `contested`, walks to `surfaced` when the arbiter picks a winner, and
# `collapsed` once it drops below 2 non-junk clusters. The UI surfaces the LIVE
# disputes (contested / surfaced); a `collapsed` group is no longer contested.
ContentionStatus = Literal["contested", "surfaced", "collapsed"]
# GLASS-1 — how the faithfulness verify pass ran, as stamped in the critique's
# ``verification.judge_status``: ``llm`` (the judge graded), ``deterministic``
# (only the floor ran), ``unsampled`` (J2 — the sampling gate deliberately did
# not select this row; an honest state, never an error, and a FIRST-CLASS
# filter value here so the unsampled stratum is one query, not a client sieve).
JudgeStatus = Literal["llm", "deterministic", "unsampled"]


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(produced_at: datetime, row_id: UUID | str) -> str:
    """Pack `(produced_at_iso, id)` into an opaque base64 token."""
    payload = json.dumps(
        {"produced_at": produced_at.isoformat(), "id": str(row_id)},
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Reverse of `_encode_cursor`. Raises HTTPException(400) on bad input."""
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
# Response models — column-for-column mirrors of the underlying tables.
# ---------------------------------------------------------------------------


class FindingRow(BaseModel):
    """One row of `analyst_outputs` with `kind='finding'`.

    Mirrors the columns of `public.analyst_outputs` exactly, plus two
    ADDITIVE critic-actuator fields (S3, nullable / backwards-compatible):

      * ``critic_score`` — the L-175 critic's ``overall_score`` for THIS
        finding, when a critique exists. NULL when the finding was never
        critiqued (the common case today). Resolved by the direct
        finding↔critique link (the critique row carries the finding's id in
        ``data.analyzed_output_id``) rather than the analyst_traces FK chain,
        which is keyed on the critic's own run.
      * ``effective_confidence`` — the critic-folded surfaced confidence,
        ``min(confidence, critic_score)`` when a critic score exists, else the
        finding's own ``confidence``. This is the critic ACTUATION: a finding
        the critic graded poorly surfaces a lowered confidence, so the score
        DOES something instead of being a spectator.
      * ``verification`` — P0-T3: the faithfulness verify pass's detail block
        (``faithfulness_score`` + the named ``unsupported_spans`` + the
        ``judge_status`` label) when a faithfulness critique exists, so the
        operator sees WHY confidence was demoted. NULL for a legacy / unverified
        finding — and then ``effective_confidence == confidence`` (no
        regression, no fabricated block).
      * ``verify_exempt`` — P0-4: ``"structural"`` when the emitting analyst is
        a deterministic structural/mining analyst whose findings NEVER route
        through the faithfulness verify pass (the
        ``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` registry in provenance.kinds).
        Server-derived so every client renders the honest
        ``unverified — structural`` badge instead of an unmarked row. NULL for
        every verify-covered analyst.
    """
    id: str
    kind: str
    title: str
    body: str
    confidence: float
    severity: str | None
    data: dict[str, Any]
    target_id: str | None
    target_version: str | None
    analyst_id: str | None
    analyst_version: str | None
    produced_at: datetime
    derived_from: list[str] = Field(default_factory=list)
    schema_uri: str
    run_id: str | None
    created_at: datetime
    # S3 critic-actuator (additive, nullable).
    critic_score: float | None = None
    effective_confidence: float | None = None
    # P0-T3 faithfulness-verify detail (additive, nullable). Names the
    # unsupported spans so the demotion is explained, never opaque.
    verification: dict[str, Any] | None = None
    # P0-4 (additive, nullable): "structural" for verify-exempt deterministic
    # structural/mining analysts — the honest badge stamp, never fabricated.
    verify_exempt: str | None = None
    # E-1 (2026-07-27 sweep item 3, additive, nullable): the EXPLICIT
    # below-floor mark. ``True`` = a graded finding whose surfaced
    # ``effective_confidence`` sits under the system-wide 0.50 floor
    # (``_FAITH_FLOOR`` — a test-enforced mirror of
    # scorecard_banding.FAITH_FLOOR, the one source of the decision);
    # ``False`` = graded and clears the floor; ``None`` = never graded (no
    # critic/faithfulness score — no fabricated verdict, matching the
    # ``verification`` block's honesty rule). Server-derived so every client
    # renders the below-floor badge instead of re-deriving 0.50 client-side.
    # ANNOTATE-not-exclude: the row still serves (the alert/scorecard gates
    # are where the floor EXCLUDES); this makes it distinguishable everywhere.
    below_floor: bool | None = None


class SituationRow(BaseModel):
    """One row of `public.situations`."""
    id: str
    data: dict[str, Any]
    name: str
    status: str
    category: str
    last_event_at: datetime | None
    event_count: int
    intensity_score: float
    target_id: str | None
    target_version: str | None
    analyst_id: str | None
    analyst_version: str | None
    produced_at: datetime
    derived_from: list[str] = Field(default_factory=list)
    schema_uri: str
    run_id: str | None
    created_at: datetime
    updated_at: datetime


class SignalRow(BaseModel):
    """One row of `public.signals`."""
    id: str
    data: dict[str, Any]
    title: str
    source_id: str | None
    source_url: str
    guid: str
    category: str
    event_timestamp: datetime | None
    language: str
    confidence: float
    classification_scores: dict[str, Any] | None
    target_id: str | None
    target_version: str | None
    analyst_id: str | None
    analyst_version: str | None
    produced_at: datetime
    derived_from: list[str] = Field(default_factory=list)
    schema_uri: str
    run_id: str | None
    created_at: datetime
    updated_at: datetime
    descriptor_source_id: str
    # Source-first typed filter columns (additive — panels can match on these
    # directly instead of digging into `data`).
    geo: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entity_classes: list[str] = Field(default_factory=list)
    # P2-1 evidence archival (additive) — derived from the EXISTING
    # signals.object_ref column (`cas:sha256/<hex>`, stamped by the
    # evidence_archiver when our copy of the original bytes exists). Lets the
    # UI/exports show "evidence preserved" + the verifiable hash without a
    # sidecar join. False/None for un-archived rows — never fabricated.
    archived: bool = False
    archive_sha256: str | None = None


class ContentionValueRow(BaseModel):
    """One competing NON-junk OR junk value cluster of a contention group.

    Mirrors `public.fact_contention_values` (Holes-B Wave 1, migration 0055)
    column-for-column, minus pure-internal ids the UI doesn't render. Each row
    is one distinct value the sources offered for the group's
    `(subject, predicate)`, carrying its aggregated support (distinct lineage,
    summed source-credibility, confidence stats), the deterministic arbiter
    `Q·C·R·F` score, and whether the arbiter surfaced it as the winner. A
    junk-gated cluster carries `is_junk=true` + the operator-reportable
    `junk_reason` (never silently dropped) and is excluded from the dispute
    count.
    """
    value_key: str
    representative_fact_id: str | None
    distinct_source_count: int
    source_credibility_sum: float
    confidence_max: float
    confidence_mean: float
    source_types: list[str] = Field(default_factory=list)
    arbiter_score: float | None
    surfaced_winner: bool
    is_junk: bool
    junk_reason: str | None
    latest_asserted_at: datetime | None


class ContentionRow(BaseModel):
    """One contention group — `public.fact_contention` (migration 0055) plus
    its per-value support clusters.

    A group is opened when >= 2 credible sources disagree on a
    `(subject_key, predicate_key)` value. `status` walks
    contested -> surfaced -> collapsed; `surfaced_value` is the arbiter's
    current deterministic winner (NULL when it ABSTAINED on a near-tie). The
    UI's "Contested" badge + per-value support panel read directly off this
    shape. READ-ONLY: this endpoint never mutates a fact, a group, or a marker.
    """
    id: str
    subject_key: str
    predicate_key: str
    status: str
    surfaced_value: str | None
    value_count: int
    junk_count: int
    opened_at: datetime
    resolved_at: datetime | None
    updated_at: datetime
    # P3-2 coexistence record (migration 0097) — HOW/WHEN the current winner was
    # surfaced. Read-only annotation (never mutates a fact); NULL when nothing is
    # surfaced (abstained / collapsed).
    surfaced_by: str | None = None          # 'deterministic' | 'llm'
    surfaced_at: datetime | None = None
    surface_rationale: str | None = None
    values: list[ContentionValueRow] = Field(default_factory=list)


class FindingSummaryRow(BaseModel):
    """One `analyst_outputs` (`kind='finding'`) row at `fields=summary` weight.

    A DISTINCT model from `FindingRow` (mirrors `journal_api.py`'s
    `JournalEntrySummaryOut` — nullable fields are never bolted onto the full
    model), so the response schema stays honest about what a summary row
    actually carries. FLAT field names (`assembly_tier` rather than a
    `data: {"data": {"assembly": {"tier": ...}}}` reconstruction) — the same
    convention `journal_api.py`'s `verify_score` flattening already uses — so a
    client reads `row.assembly_tier` instead of re-running the full payload's
    defensive nested-object parser against a partially-populated shape.

    The nine base columns are `id, analyst_id, target_id, kind, title,
    created_at, produced_at, severity, confidence` (build report's field list).
    The six data leaves are the ones `MorningRead.tsx`'s `RunHistory` and the
    mobile navigator (`mobileModel.toReportRow` / `ReportsList`) actually read
    off `assembly` / `verification` / `drops` — see the build report for the
    exact call sites and the fields real list rendering ALSO reaches for
    (`assembly.blocks[].target_name`/`.spans` for the lead thread and
    `assembly.blocks.length` for the block count) that this projection
    deliberately excludes because they are full block content, not leaves.
    """

    id: str
    analyst_id: str | None
    target_id: str | None
    kind: str
    title: str
    created_at: datetime
    produced_at: datetime
    severity: str | None
    confidence: float
    # `data.data.assembly.tier` / `.regime` / `.lead.kind` (assembly.v1, D-4).
    # `None` for a legacy/pre-assembly row or one with no `assembly` key at
    # all — never fabricated.
    assembly_tier: str | None = None
    assembly_regime: str | None = None
    assembly_lead_kind: str | None = None
    # `data.data.assembly.drops.counts` — the ledger sub-object alone (NOT the
    # sibling `drops.trimmed`/`drops.below_floor`/`drops.no_head` ITEM LISTS,
    # which are exactly the per-block detail this weight exists to shed).
    drops_counts: dict[str, Any] | None = None
    # The row's surfaced `verification` block's two published leaves — the
    # SAME object-level fallback `_hydrate_finding` uses (a faithfulness
    # critique's block if one exists, else a structural critique's), read at
    # the SQL level rather than the whole block (`claim_verdicts` /
    # `unsupported_spans` / `branch_scores` are most of that object's weight).
    verification_faithfulness_score: float | None = None
    verification_score_state: str | None = None


class FindingsSummaryPage(BaseModel):
    data: list[FindingSummaryRow]
    next_cursor: str | None


class FindingsPage(BaseModel):
    data: list[FindingRow]
    next_cursor: str | None


class CitationJudgmentEntry(BaseModel):
    """One citation reduced to the judgment weight's key set (7b-v).

    Mirrors ``findings_projection.JUDGMENT_FIELDS`` field-for-field — never
    the full stored citation (``evidence_text``, ``title``, ``tier``,
    ``derived_from``, ``effective_confidence``, the bare ``ref_id``/
    ``signal_id``, …). Same shape whatever KIND of citation it is (a signal,
    a composition sub-claim, a desk grounding block) — a reader never has to
    sniff which.
    """
    ordinal: int | None = None
    source: str | None = None
    source_id: str | None = None
    produced_at: str | None = None
    single_source: bool = False
    wire_folded: bool = False
    marker_class: str | None = None


class FindingJudgmentRow(BaseModel):
    """One `analyst_outputs` (`kind='finding'`) row at `fields=judgment`
    weight (7b-v) — the Morning Read's CHECKED band population
    (`GET /findings?fields=judgment`).

    Carries the verify verdict WHOLE (`verification`, including its own
    `unsupported_spans` — the thing the band exists to show, and exactly what
    `fields=summary` drops) plus enough about each citation to render
    "cited [N]" and say who is behind it. Deliberately NEVER carries `data`
    (the assembly's quoted blocks are most of a row's weight), `derived_from`,
    or `body` — the three leaves that make the default weight ~5.5 MB for a
    200-row page.
    """
    id: str
    kind: str
    title: str
    analyst_id: str | None
    analyst_version: str | None
    target_id: str | None
    target_version: str | None
    produced_at: datetime
    severity: str | None
    confidence: float
    schema_uri: str
    verification: dict[str, Any] | None = None
    citations: list[CitationJudgmentEntry] = Field(default_factory=list)


class FindingsJudgmentPage(BaseModel):
    data: list[FindingJudgmentRow]
    next_cursor: str | None


class SituationsPage(BaseModel):
    data: list[SituationRow]
    next_cursor: str | None


class SignalsPage(BaseModel):
    data: list[SignalRow]
    next_cursor: str | None


class ContentionPage(BaseModel):
    data: list[ContentionRow]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Row hydration helpers
# ---------------------------------------------------------------------------


def _load_jsonb(value: Any) -> dict[str, Any]:
    """asyncpg returns jsonb either as `dict` (with codec) or `str` (raw)."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _load_jsonb_opt(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _load_jsonb(value)


def _stringify_uuid_list(values: Any) -> list[str]:
    if not values:
        return []
    return [str(v) for v in values]


def _hydrate_finding(row: Any) -> FindingRow:
    confidence = float(row["confidence"])
    # S3 — critic actuation. ``critic_score`` is surfaced by the LEFT JOIN to
    # the finding's critique (when one exists); fold it into the surfaced
    # confidence as min(original, critic) so a poorly-graded finding reads as
    # lower-confidence. NULL critic_score (uncritiqued) → effective == original.
    critic_score_raw = row.get("critic_score")
    critic_score = float(critic_score_raw) if critic_score_raw is not None else None
    effective_confidence = (
        min(confidence, critic_score) if critic_score is not None else confidence
    )
    # P0-T3 — the faithfulness-verify detail (names the unsupported spans), only
    # present when a faithfulness critique exists; NULL → no fabricated block.
    verification = _load_jsonb_opt(row.get("verification"))
    # C2b (P4-6) — the structural_claims verdict for a verify-EXEMPT structural
    # finding (its own lateral; a structural critique is NEVER a faithfulness
    # one). ``structural_verified`` flips the honest badge to
    # ``structural-verified``; the structural verification detail is surfaced when
    # no faithfulness block exists (a structural finding has none). OFF-safe: the
    # structural score demotes effective_confidence ONLY when the gate flag is on
    # (default off — compute-and-show, do-not-gate).
    sv_raw = row.get("structural_verified")
    structural_verified = sv_raw if isinstance(sv_raw, bool) else (
        str(sv_raw).lower() == "true" if sv_raw is not None else None
    )
    if verification is None:
        verification = _load_jsonb_opt(row.get("structural_verification"))
    if structural_verify_gate_enabled():
        sv_score_raw = row.get("structural_score")
        sv_score = float(sv_score_raw) if sv_score_raw is not None else None
        if sv_score is not None:
            effective_confidence = min(effective_confidence, sv_score)
    # D-5: the row is passed so a DETERMINISTIC ROLLUP gets its own badge pair
    # rather than rendering as an ordinary unverified LLM read. Rollup rows
    # carry no faithfulness block by design and DO carry a structural verdict.
    verify_exempt = structural_badge(
        row["analyst_id"], structural_verified, row.get("data")
    )
    # E-1 — the explicit below-floor mark: a verdict ONLY for a GRADED finding
    # (critic_score present); an ungraded row stays None (never fabricated).
    below_floor = (
        (effective_confidence < _FAITH_FLOOR) if critic_score is not None else None
    )
    return FindingRow(
        id=str(row["id"]),
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        confidence=confidence,
        severity=row["severity"],
        data=_load_jsonb(row["data"]),
        target_id=row["target_id"],
        target_version=row["target_version"],
        analyst_id=row["analyst_id"],
        analyst_version=row["analyst_version"],
        produced_at=row["produced_at"],
        derived_from=_stringify_uuid_list(row["derived_from"]),
        schema_uri=row["schema_uri"],
        run_id=str(row["run_id"]) if row["run_id"] else None,
        created_at=row["created_at"],
        critic_score=critic_score,
        effective_confidence=effective_confidence,
        verification=verification,
        # P0-4 / C2b — the structural verify-exemption stamp, derived server-side
        # from the ONE registry: ``structural`` (unverified) or, when a passing
        # structural critique exists for this finding, ``structural-verified``.
        verify_exempt=verify_exempt,
        below_floor=below_floor,
    )


def _hydrate_finding_summary(row: Any) -> FindingSummaryRow:
    """Map a `fields=summary` SQL row (leaf scalars already extracted by the
    query — see the route's `if fields == "summary"` branch) to
    `FindingSummaryRow`.

    The `coalesce(c.verification, s.structural_verification)` in the SQL is
    OBJECT-level, matching `_hydrate_finding`'s own Python fallback
    (`if verification is None: verification = structural_verification`) —
    not a per-key coalesce. A per-key coalesce would misread a faithfulness
    block whose `faithfulness_score` is legitimately `null` (Q-1's
    `unassessable` score_state) as "no faithfulness block, fall through to
    structural"; the object-level form never reaches for the wrong source.
    """
    drops_counts = _load_jsonb_opt(row.get("drops_counts"))
    ffs_raw = row.get("verification_faithfulness_score")
    return FindingSummaryRow(
        id=str(row["id"]),
        analyst_id=row["analyst_id"],
        target_id=row["target_id"],
        kind=row["kind"],
        title=row["title"],
        created_at=row["created_at"],
        produced_at=row["produced_at"],
        severity=row["severity"],
        confidence=float(row["confidence"]),
        assembly_tier=row.get("assembly_tier"),
        assembly_regime=row.get("assembly_regime"),
        assembly_lead_kind=row.get("assembly_lead_kind"),
        drops_counts=drops_counts,
        verification_faithfulness_score=(
            float(ffs_raw) if ffs_raw is not None else None
        ),
        verification_score_state=row.get("verification_score_state"),
    )


def _hydrate_finding_judgment(row: Any) -> FindingJudgmentRow:
    """Map a `fields=judgment` SQL row (§ the route's `if fields ==
    "judgment"` branch) to `FindingJudgmentRow`.

    `verification` is the SAME object-level `coalesce(faithfulness,
    structural)` fallback `_hydrate_finding`/`_hydrate_finding_summary` use,
    carried WHOLE this time (not flattened to two leaves). `citations_raw` is
    the SQL's own nested-then-flat resolve of a finding's citation array
    (`data.data.citations` else `data.citations` — mirrors `export_api
    ._citation_list`'s fallback order); `findings_projection.
    project_citations` reduces it to the judgment key set here, so this
    route never sends the finding's full `data` blob to answer this weight.
    """
    return FindingJudgmentRow(
        id=str(row["id"]),
        kind=row["kind"],
        title=row["title"],
        analyst_id=row["analyst_id"],
        analyst_version=row["analyst_version"],
        target_id=row["target_id"],
        target_version=row["target_version"],
        produced_at=row["produced_at"],
        severity=row["severity"],
        confidence=float(row["confidence"]),
        schema_uri=row["schema_uri"],
        verification=_load_jsonb_opt(row.get("verification")),
        citations=[
            CitationJudgmentEntry(**entry)
            for entry in project_citations(row.get("citations_raw"))
        ],
    )


async def fetch_situations_page(
    pg: Any,
    *,
    state: SituationState | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    as_of: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> SituationsPage:
    """One situations page — the query the ``/api/v1/situations`` route and
    the V3/P3 ``/api/v1/v3/situations`` alias BOTH run (the v3 surface needed
    the same list so the temporal acceptance proof can curl the v3 prefix;
    extracting the query keeps the two routes on one WHERE clause forever).

    ``as_of`` (V3/P3) is the canonical validity-time read: the frames that
    held on date D, including ones closed since —
    ``COALESCE(valid_from, -infinity) <= D AND (valid_until IS NULL OR
    valid_until > D)`` — never ANDed with an open-row gate. A malformed
    ``as_of`` never reaches here; FastAPI 422s it at the boundary.
    """
    limit = _validate_limit(limit)

    where: list[str] = []
    args: list[Any] = []

    if as_of is not None:
        args.append(as_of)
        d = len(args)
        where.append(
            f"COALESCE(valid_from, '-infinity'::timestamptz) <= ${d} "
            f"AND (valid_until IS NULL OR valid_until > ${d})"
        )
    if state is not None:
        args.append(state)
        where.append(f"status = ${len(args)}")
    if target_id is not None:
        args.append(target_id)
        where.append(f"target_id = ${len(args)}")
    if since is not None:
        args.append(since)
        where.append(f"produced_at >= ${len(args)}")
    if cursor is not None:
        cur_at, cur_id = _decode_cursor(cursor)
        args.append(cur_at)
        args.append(cur_id)
        where.append(
            f"(produced_at, id) < (${len(args) - 1}, ${len(args)})"
        )

    args.append(limit + 1)
    where_clause = (
        f"WHERE {' AND '.join(where)}" if where else ""
    )
    sql = f"""
        SELECT id, data, name, status, category, last_event_at,
               event_count, intensity_score,
               target_id, target_version, analyst_id, analyst_version,
               produced_at, derived_from, schema_uri, run_id,
               created_at, updated_at
          FROM situations
         {where_clause}
         ORDER BY produced_at DESC, id DESC
         LIMIT ${len(args)}
    """

    async with pg.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    out = [_hydrate_situation(r) for r in rows[:limit]]
    next_cursor: str | None = None
    if len(rows) > limit and out:
        last = out[-1]
        next_cursor = _encode_cursor(last.produced_at, last.id)
    return SituationsPage(data=out, next_cursor=next_cursor)


def _hydrate_situation(row: Any) -> SituationRow:
    return SituationRow(
        id=str(row["id"]),
        data=_load_jsonb(row["data"]),
        name=row["name"],
        status=row["status"],
        category=row["category"],
        last_event_at=row["last_event_at"],
        event_count=int(row["event_count"]),
        intensity_score=float(row["intensity_score"]),
        target_id=row["target_id"],
        target_version=row["target_version"],
        analyst_id=row["analyst_id"],
        analyst_version=row["analyst_version"],
        produced_at=row["produced_at"],
        derived_from=_stringify_uuid_list(row["derived_from"]),
        schema_uri=row["schema_uri"],
        run_id=str(row["run_id"]) if row["run_id"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _hydrate_signal(row: Any, *, target_id: str | None = None) -> SignalRow:
    """Map a source-first ``signals`` row to the UI-facing SignalRow.

    Source-first (pivot): signals are TARGET-AGNOSTIC + modality-first. The
    historical UI shape is preserved by mapping the new columns onto it —
    ``data``=payload (keeps ``data.geo`` for the Map), ``produced_at``/
    ``event_timestamp``=fetched_at, ``title``=payload.title, ``category``=
    first tag. The dropped per-target/per-analyst columns become None; the new
    typed filter columns (geo/tags/entity_classes) are surfaced additively.
    """
    payload = _load_jsonb(row["payload"])
    geo = list(row.get("geo") or [])
    tags = list(row.get("tags") or [])
    title = (
        payload.get("title")
        or payload.get("headline")
        or row.get("canonical_url")
        or "(untitled)"
    )
    fetched_at = row["fetched_at"]
    return SignalRow(
        id=str(row["id"]),
        data=payload,
        title=str(title),
        source_id=row["source_id"],
        source_url=row.get("canonical_url") or "",
        guid=row.get("content_hash") or "",
        category=(tags[0] if tags else ""),
        event_timestamp=fetched_at,
        language=row.get("language") or "",
        confidence=float(row["source_credibility"]) if row.get("source_credibility") is not None else 0.0,
        classification_scores=None,
        target_id=target_id,
        target_version=None,
        analyst_id=None,
        analyst_version=None,
        produced_at=fetched_at,
        derived_from=_stringify_uuid_list(row["derived_from"]),
        schema_uri=row["schema_uri"],
        run_id=None,
        created_at=fetched_at,
        updated_at=fetched_at,
        descriptor_source_id=row["source_id"] or "",
        geo=geo,
        tags=tags,
        entity_classes=list(row.get("entity_classes") or []),
        # P2-1: archived-evidence surface, derived from signals.object_ref
        # alone (cas:sha256/<hex>); never fabricated for foreign ref shapes.
        archived=row.get("object_ref") is not None,
        archive_sha256=sha256_from_object_ref(row.get("object_ref")),
    )


def _hydrate_contention_value(row: Any) -> ContentionValueRow:
    """Map one `fact_contention_values` row to its UI-facing model."""
    rep = row["representative_fact_id"]
    return ContentionValueRow(
        value_key=row["value_key"],
        representative_fact_id=str(rep) if rep is not None else None,
        distinct_source_count=int(row["distinct_source_count"]),
        source_credibility_sum=float(row["source_credibility_sum"]),
        confidence_max=float(row["confidence_max"]),
        confidence_mean=float(row["confidence_mean"]),
        source_types=list(row["source_types"] or []),
        arbiter_score=(
            float(row["arbiter_score"]) if row["arbiter_score"] is not None else None
        ),
        surfaced_winner=bool(row["surfaced_winner"]),
        is_junk=bool(row["is_junk"]),
        junk_reason=row["junk_reason"],
        latest_asserted_at=row["latest_asserted_at"],
    )


def _hydrate_contention(group_row: Any, value_rows: list[Any]) -> ContentionRow:
    """Map one `fact_contention` group + its value clusters to ContentionRow.

    The per-value rows are surfaced in their stored arbiter order (winner /
    highest score first; the SQL orders them), so the UI's support panel reads
    top-down strongest-first without re-sorting.
    """
    # Backward-compatible reads: a pre-0097 group row (fetched before the
    # coexistence columns existed) simply lacks these keys → None.
    def _opt(row: Any, key: str) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    return ContentionRow(
        id=str(group_row["id"]),
        subject_key=group_row["subject_key"],
        predicate_key=group_row["predicate_key"],
        status=group_row["status"],
        surfaced_value=group_row["surfaced_value"],
        value_count=int(group_row["value_count"]),
        junk_count=int(group_row["junk_count"]),
        opened_at=group_row["opened_at"],
        resolved_at=group_row["resolved_at"],
        updated_at=group_row["updated_at"],
        surfaced_by=_opt(group_row, "surfaced_by"),
        surfaced_at=_opt(group_row, "surfaced_at"),
        surface_rationale=_opt(group_row, "surface_rationale"),
        values=[_hydrate_contention_value(v) for v in value_rows],
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_substrate_reads_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the substrate-read router bound to the registry deps.

    All three endpoints are GETs, all are bearer-gated via
    `require_bearer`, and all read directly from the primary Postgres
    pool via `deps.descriptor_registry.pg.acquire()` — the same path the
    v3 telemetry router uses.
    """
    router = APIRouter(tags=["substrate-reads"])

    # ---------------- units/{target_id}/correctness (G2) ----------------
    #
    # The per-unit correctness NUMBER, from a sibling module so this file stays
    # under the module-size gate's 1,500-line entry threshold. Mounted HERE
    # rather than in `server.py` so the route inherits this router's prefix and
    # `require_bearer` posture without a second wiring site to keep in step.
    router.include_router(build_unit_correctness_router(deps))

    # ---------------- findings ----------------

    @router.get("/findings", response_model=None)
    async def list_findings(
        since: datetime | None = Query(default=None),
        target_id: str | None = Query(default=None),
        target_id_null: bool = Query(default=False),
        analyst_id: str | None = Query(default=None),
        analyst_id_in: str | None = Query(default=None),
        severity: Severity | None = Query(default=None),
        verified: bool | None = Query(default=None),
        judge_status: JudgeStatus | None = Query(default=None),
        q: str | None = Query(default=None),
        # Wave-E — opt-in access-class filter (SEAMS #59). CSV of
        # data/provenance/access.py's ACCESS_CLASSES; a finding matches when
        # ANY signal in its `derived_from` carries one of the given classes
        # (single-hop, mirroring access_ceiling_sql's own scope note — a
        # finding several tiers removed from raw signals may derive_from
        # only other analyst_outputs and so match nothing). Unset (the
        # default) is BYTE-IDENTICAL to today's behaviour — no clause added.
        access_class_in: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        # P-mobile — the list-view weight (build report
        # "findings fields=summary"). Omitted (the default) is BYTE-IDENTICAL to
        # the route's pre-existing behavior — same SQL, same hydration, same
        # `FindingsPage` shape — so every existing caller (MorningRead, the
        # workstation Feed, the mobile navigator's current full-row fetch) is
        # unaffected. `fields=summary` opts into `FindingsSummaryPage`: the nine
        # scalar columns every list row needs (§ below) plus the handful of
        # `assembly`/`verification`/`drops` leaves MorningRead's history rail and
        # the mobile navigator actually read — never the full `body`, the
        # per-block `assembly.blocks` (quoted prose + spans + signals), or the
        # verify pass's `claim_verdicts`/`unsupported_spans` detail, which are
        # what make a day of reads hundreds of KB. A `Literal` type (rather than
        # `journal_api.py`'s hand-rolled `_validate_fields`, which 400s) is used
        # deliberately here so an unknown value 422s — this route's own spec.
        # 7b-v — `fields=judgment`: the Morning Read CHECKED band's weight.
        # The verify verdict WHOLE (`verification`, `unsupported_spans`
        # included — exactly what `fields=summary` drops) plus a `citations`
        # list reduced to `findings_projection.JUDGMENT_FIELDS` — never the
        # finding's full `data`, `derived_from`, or `body`.
        fields: Literal["summary", "judgment"] | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> FindingsPage | FindingsSummaryPage | FindingsJudgmentPage:
        limit = _validate_limit(limit)

        # Findings are aliased ``f`` because S3 LEFT JOINs each finding to its
        # critic critique (``c``). The critique row is an ``analyst_outputs``
        # row with kind='critique' whose ``data->>'analyzed_output_id'`` names
        # this finding — a DIRECT link (the analyst_traces FK chain is keyed on
        # the critic's own run, not the analyzed finding, so it's the wrong
        # join). The lateral picks the LATEST critique per finding.
        where: list[str] = ["f.kind = 'finding'"]
        args: list[Any] = []

        if since is not None:
            args.append(since)
            where.append(f"f.produced_at >= ${len(args)}")
        # P1-T1 reachability — the ~1100 NULL-target "orphan" findings are
        # unreachable from any country view. `target_id_null=true` returns ONLY
        # those orphans. It takes precedence over a (meaningless) co-passed
        # `target_id` exact filter, since a NULL row can't also equal a value.
        if target_id_null:
            where.append("f.target_id IS NULL")
        elif target_id is not None:
            args.append(target_id)
            where.append(f"f.target_id = ${len(args)}")
        if analyst_id is not None:
            args.append(analyst_id)
            where.append(f"f.analyst_id = ${len(args)}")
        # P1-T1 reachability — analyst-set reach. `analyst_id_in` is a CSV of
        # analyst ids; return the UNION (any finding whose analyst_id is in the
        # set). Composes with a single `analyst_id` (AND) when both are passed.
        if analyst_id_in is not None:
            ids = [a.strip() for a in analyst_id_in.split(",") if a.strip()]
            if ids:
                args.append(ids)
                where.append(f"f.analyst_id = ANY(${len(args)}::text[])")
        if severity is not None:
            args.append(severity)
            where.append(f"f.severity = ${len(args)}")
        # GLASS-1 — the server-side verification facet. Both predicates run
        # over the SAME surfaced verification block ``_hydrate_finding``
        # projects (the faithfulness lateral's block, else the structural
        # fallback), so the filter and the badge a row renders can never
        # disagree — and, being in the WHERE, the page fill + next_cursor are
        # computed over the FILTERED population (the client-side sieve this
        # replaces filtered pages it had already fetched, so at any real
        # corpus size the facet lied about the filtered population).
        #
        # H17 — these two are the ONLY predicates that read the critique folds,
        # so they are collected separately: everything in ``where`` filters the
        # bounded page CTE, and these filter AFTER the join. That split is what
        # lets the page LIMIT live inside the CTE (so the fold reads at most
        # ``limit + 1`` ids) whenever the facet is not in play — which is every
        # call that does not ask for it.
        fold_where: list[str] = []
        surfaced_verification = "coalesce(c.verification, s.structural_verification)"
        if verified is not None:
            # ``verified=true`` means what the feed's isVerified() means: the
            # surfaced block carries a MEASURED faithfulness_score (an
            # ``unsampled`` row still qualifies — the deterministic floor
            # ran). ``verified=false`` is everything else: no verify pass,
            # structural-only, or a block without the number. jsonb_typeof is
            # NULL-safe — a missing block/key is never 'number' (and always
            # DISTINCT FROM it), so no fabricated verdict either way.
            op = "=" if verified else "IS DISTINCT FROM"
            fold_where.append(
                f"jsonb_typeof({surfaced_verification} -> 'faithfulness_score') "
                f"{op} 'number'"
            )
        # Bound AFTER the page-CTE predicates so that ``where``'s parameters stay
        # $1..$where_argc contiguous (the scan-floor probe below re-runs exactly
        # those, and a bind list has to match).
        pending_judge_status = judge_status
        # P1-T1 reachability — keyword reach. Full-text match over the
        # concatenated title+body. `to_tsvector(...) @@ plainto_tsquery(...)` is
        # correct without a dedicated index (seq scan, scoped by the other
        # predicates); COALESCE guards the (NOT NULL today, but defensive) text.
        if q is not None and q.strip():
            args.append(q)
            where.append(
                "to_tsvector('simple', coalesce(f.title, '') || ' ' || "
                f"coalesce(f.body, '')) @@ plainto_tsquery('simple', ${len(args)})"
            )
        # Wave-E — SEAMS #59, opt-in only (see the param docstring above).
        if access_class_in is not None:
            classes = [c.strip() for c in access_class_in.split(",") if c.strip()]
            try:
                signal_clause = _access.access_class_clause("acs", classes)
            except _access.AccessClassError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc
            where.append(
                "EXISTS (SELECT 1 FROM public.signals acs "
                f"WHERE acs.id = ANY(f.derived_from) AND {signal_clause})"
            )
        if cursor is not None:
            cur_at, cur_id = _decode_cursor(cursor)
            args.append(cur_at)
            args.append(cur_id)
            where.append(
                f"(f.produced_at, f.id) < (${len(args) - 1}, ${len(args)})"
            )

        where_argc = len(args)
        if pending_judge_status is not None:
            # Exact text match on the block's ``judge_status``, mirroring the
            # client verdict model: a legacy block without the key matches NO
            # value (never coalesced to 'deterministic' here — that fold is
            # the production gauge's health heuristic, not a filter truth).
            args.append(pending_judge_status)
            fold_where.append(
                f"{surfaced_verification} ->> 'judge_status' = ${len(args)}"
            )

        args.append(limit + 1)
        # Where the page LIMIT lands (H17). With no verification facet the page
        # is decided BEFORE the fold, so the LIMIT bounds the CTE and the fold
        # reads at most `limit + 1` ids — the cheapest possible read.
        #
        # With the facet on it cannot: the facet reads the fold, so the page can
        # only be cut after the join. The CTE is then bounded by
        # `_FACET_SCAN_CAP` instead, because the alternative measured badly —
        # an unbounded page CTE over the whole findings table folds ~41k
        # critiques through a 125 MB external merge (4.8 s live, 2026-09-24),
        # against 186 ms at the cap and 905 ms for the lateral this replaces.
        # The cap does NOT drop rows: `_facet_scan_floor_cursor` below turns a
        # capped scan into a `next_cursor`, so the client walks the rest of the
        # population a scan at a time instead of being told the feed ended.
        _page_limit = f"LIMIT ${len(args)}"
        page_limit = f"LIMIT {_FACET_SCAN_CAP}" if fold_where else _page_limit
        after_fold = (
            (("WHERE " + " AND ".join(fold_where) + "\n                 ")
             if fold_where else "")
            + "ORDER BY f.produced_at DESC, f.id DESC"
            + (f"\n                 {_page_limit}" if fold_where else "")
        )

        async def _facet_next_cursor(conn: Any, rows: list[Any]) -> str | None:
            """The cursor a SHORT facet page still owes the client.

            A page that filled tells us nothing was missed — the ordinary
            `len(rows) > limit` rule answers. A short one is ambiguous: either
            the population really ended, or the scan cap cut it. Asking for the
            cap-th row of the SAME ordered scan settles it, and that row is
            exactly where the next scan must resume.
            """
            if not fold_where or len(rows) > limit:
                return None
            floor = await conn.fetchrow(
                "SELECT f.produced_at, f.id FROM analyst_outputs f "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY f.produced_at DESC, f.id DESC "
                f"OFFSET {_FACET_SCAN_CAP - 1} LIMIT 1",
                *args[:where_argc],
            )
            if floor is None:
                return None
            return _encode_cursor(floor["produced_at"], floor["id"])

        if fields == "summary":
            # §"fields=summary" — the SAME two laterals (a finding's LATEST
            # faithfulness critique, else its latest structural critique), but
            # the SELECT list pulls only the leaf scalars the list surfaces
            # render: no `f.body`, no full `f.data` (the assembly's quoted
            # blocks/spans/signals live under there), no `c.critic_score` (the
            # summary spec's `confidence` is the row's own stored column, not
            # the critic-folded `effective_confidence`), and no whole
            # `verification` blob (`claim_verdicts`/`unsupported_spans`/
            # `branch_scores` are the bulk of that object and no summary
            # consumer reads them). `coalesce(c.verification, s.
            # structural_verification)` mirrors `_hydrate_finding`'s own
            # Python-level fallback (object-level, not per-key — see
            # `_hydrate_finding_summary`'s docstring) so a row with a
            # faithfulness block whose `faithfulness_score` is legitimately
            # `null` (the Q-1 `unassessable` state) is never misread as
            # falling through to the structural score.
            sql = f"""
                WITH f AS MATERIALIZED (
                    SELECT f.id, f.kind, f.title, f.confidence, f.severity,
                           f.target_id, f.analyst_id, f.produced_at,
                           f.created_at, f.data
                      FROM analyst_outputs f
                     WHERE {' AND '.join(where)}
                     ORDER BY f.produced_at DESC, f.id DESC
                     {page_limit}
                ), {_FAITHFULNESS_VERIFICATION_CTE},
                   {_STRUCTURAL_VERIFICATION_CTE}
                SELECT f.id, f.kind, f.title, f.confidence, f.severity,
                       f.target_id, f.analyst_id, f.produced_at, f.created_at,
                       f.data->'data'->'assembly'->>'tier' AS assembly_tier,
                       f.data->'data'->'assembly'->>'regime' AS assembly_regime,
                       f.data->'data'->'assembly'->'lead'->>'kind'
                           AS assembly_lead_kind,
                       f.data->'data'->'assembly'->'drops'->'counts'
                           AS drops_counts,
                       (coalesce(c.verification, s.structural_verification)
                           ->> 'faithfulness_score')::real
                           AS verification_faithfulness_score,
                       (coalesce(c.verification, s.structural_verification)
                           ->> 'score_state') AS verification_score_state
                  FROM f
                  LEFT JOIN c ON c.fid = f.id::text
                  LEFT JOIN s ON s.fid = f.id::text
                 {after_fold}
            """
            async with deps.descriptor_registry.pg.acquire() as conn:
                summary_rows = await conn.fetch(sql, *args)
                summary_next_cursor = await _facet_next_cursor(conn, summary_rows)
            out_summary = [_hydrate_finding_summary(r) for r in summary_rows[:limit]]
            if len(summary_rows) > limit and out_summary:
                last_summary = out_summary[-1]
                summary_next_cursor = _encode_cursor(
                    last_summary.produced_at, last_summary.id,
                )
            return FindingsSummaryPage(data=out_summary, next_cursor=summary_next_cursor)

        if fields == "judgment":
            # 7b-v — the CHECKED band's weight. The SAME two laterals as
            # `fields=summary` (a finding's latest faithfulness critique, else
            # its latest structural critique), but this time `verification` is
            # surfaced WHOLE — the band's whole job is to read
            # `unsupported_spans` off it, which `fields=summary` deliberately
            # drops. `citations_raw` resolves the SAME nested-then-flat
            # citations shape `export_api._citation_list` documents
            # (`data->'data'->'citations'` else `data->'citations'`) but pulls
            # ONLY that array — never the finding's full `data` (the assembly's
            # quoted blocks, which is most of a row's weight and no consumer
            # of this weight reads).
            sql = f"""
                WITH f AS MATERIALIZED (
                    SELECT f.id, f.kind, f.title, f.analyst_id,
                           f.analyst_version, f.target_id, f.target_version,
                           f.produced_at, f.severity, f.confidence,
                           f.schema_uri, f.data
                      FROM analyst_outputs f
                     WHERE {' AND '.join(where)}
                     ORDER BY f.produced_at DESC, f.id DESC
                     {page_limit}
                ), {_FAITHFULNESS_VERIFICATION_CTE},
                   {_STRUCTURAL_VERIFICATION_CTE}
                SELECT f.id, f.kind, f.title, f.analyst_id, f.analyst_version,
                       f.target_id, f.target_version, f.produced_at,
                       f.severity, f.confidence, f.schema_uri,
                       coalesce(c.verification, s.structural_verification)
                           AS verification,
                       coalesce(f.data->'data'->'citations', f.data->'citations')
                           AS citations_raw
                  FROM f
                  LEFT JOIN c ON c.fid = f.id::text
                  LEFT JOIN s ON s.fid = f.id::text
                 {after_fold}
            """
            async with deps.descriptor_registry.pg.acquire() as conn:
                judgment_rows = await conn.fetch(sql, *args)
                judgment_next_cursor = await _facet_next_cursor(conn, judgment_rows)
            out_judgment = [
                _hydrate_finding_judgment(r) for r in judgment_rows[:limit]
            ]
            if len(judgment_rows) > limit and out_judgment:
                last_judgment = out_judgment[-1]
                judgment_next_cursor = _encode_cursor(
                    last_judgment.produced_at, last_judgment.id,
                )
            return FindingsJudgmentPage(
                data=out_judgment, next_cursor=judgment_next_cursor,
            )

        # The lateral picks the LATEST faithfulness-verify critique per finding
        # and surfaces BOTH the gate input (overall_score) AND the P0-T3
        # faithfulness-verify detail block (cr.data->'data'->'verification' —
        # written by the verify pass via CritiquePayload.data). The verification
        # block is NULL for a finding with no faithfulness critique, so the API
        # surfaces no fabricated block and effective_confidence stays ==
        # confidence (no regression).
        #
        # S8-T2 — PIN to the faithfulness critique (``title LIKE
        # 'Faithfulness verify%'``), not just any newest ``overall_score``
        # critique. Without the pin a later GENERIC critique (e.g. a
        # country_critic that also stamps ``overall_score``) would win the
        # ``produced_at DESC`` race and OVERWRITE the faithfulness demotion,
        # un-demoting a finding the verify pass floored. Mirrors the composition
        # GATHER lateral (meta_findings_synthesizer) + gepa parent SQL, which
        # already pin.
        # C2b (P4-6) — a SECOND lateral picks the latest STRUCTURAL verify
        # critique (``title LIKE 'Structural verify%'``, DISTINCT from the
        # faithfulness lateral above — a structural finding never has a
        # faithfulness critique). It surfaces the ``structural_verified`` marker
        # (flips the badge to ``structural-verified``), the structural
        # verification detail (shown when no faithfulness block exists), and the
        # structural score (folded into effective_confidence ONLY when the gate
        # flag is on — see _hydrate_finding). Pinned by title so it can never
        # shadow the faithfulness demotion.
        sql = f"""
            WITH f AS MATERIALIZED (
                SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
                       f.data, f.target_id, f.target_version, f.analyst_id,
                       f.analyst_version, f.produced_at, f.derived_from,
                       f.schema_uri, f.run_id, f.created_at
                  FROM analyst_outputs f
                 WHERE {' AND '.join(where)}
                 ORDER BY f.produced_at DESC, f.id DESC
                 {page_limit}
            ), {_CRITIC_SCORE_CTE}, {_STRUCTURAL_BADGE_CTE}
            SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
                   f.data, f.target_id, f.target_version, f.analyst_id,
                   f.analyst_version, f.produced_at, f.derived_from,
                   f.schema_uri, f.run_id, f.created_at,
                   c.critic_score AS critic_score,
                   c.verification AS verification,
                   s.structural_verified AS structural_verified,
                   s.structural_score AS structural_score,
                   s.structural_verification AS structural_verification
              FROM f
              LEFT JOIN c ON c.fid = f.id::text
              LEFT JOIN s ON s.fid = f.id::text
             {after_fold}
        """

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            next_cursor = await _facet_next_cursor(conn, rows)

        out = [_hydrate_finding(r) for r in rows[:limit]]
        if len(rows) > limit and out:
            last = out[-1]
            next_cursor = _encode_cursor(last.produced_at, last.id)
        return FindingsPage(data=out, next_cursor=next_cursor)

    # ---------------- situations ----------------

    @router.get("/situations", response_model=SituationsPage)
    async def list_situations(
        state: SituationState | None = Query(default=None),
        target_id: str | None = Query(default=None),
        since: datetime | None = Query(default=None),
        # V3/P3 — the canonical as-of read on the frame's validity span:
        # the frames that held on date D, including ones closed since. A
        # malformed value 422s at the FastAPI boundary, never defaults.
        as_of: datetime | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> SituationsPage:
        return await fetch_situations_page(
            deps.descriptor_registry.pg,
            state=state, target_id=target_id, since=since, as_of=as_of,
            limit=limit, cursor=cursor,
        )

    # ---------------- signals ----------------

    @router.get("/signals", response_model=SignalsPage)
    async def list_signals(
        target_id: str | None = Query(default=None),
        since: datetime | None = Query(default=None),
        source_id: str | None = Query(default=None),
        language: str | None = Query(default=None),
        # Wave-E — opt-in access-class filter (SEAMS #59). CSV of
        # data/provenance/access.py's ACCESS_CLASSES, matched against the
        # row's own `access_class` column (stamped at ingest, migration
        # 0216). Unset (the default) is BYTE-IDENTICAL to today's behaviour
        # — no clause is added and every existing caller is unaffected.
        access_class_in: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> SignalsPage:
        limit = _validate_limit(limit)

        where: list[str] = []
        args: list[Any] = []

        # Hide exact-duplicate rows: snapshot feeds (active-alert / recent-quake
        # endpoints) re-ingest the same item on every poll, and dedup stamps each
        # dup's ``canonical_signal_id`` to the kept row. Show only canonical
        # signals (self-pointer or not-yet-deduped) so the read surface isn't ~4x
        # the real volume.
        where.append("(canonical_signal_id IS NULL OR canonical_signal_id = id)")

        # Source-first: signals are TARGET-AGNOSTIC. A ``target_id`` filter is
        # resolved to the target's scope.geo (the per-target discriminator, the
        # same as the analyst slice) — signals whose geo overlaps. Falls back
        # to unfiltered when the target has no geo scope (e.g. estate/entity).
        async with deps.descriptor_registry.pg.acquire() as conn:
            if target_id is not None:
                trow = await conn.fetchrow(
                    "SELECT body FROM target_descriptors "
                    "WHERE descriptor_id = $1 AND is_head = TRUE",
                    target_id,
                )
                tgeo: list[str] = []
                if trow and trow["body"]:
                    tbody = trow["body"]
                    if isinstance(tbody, str):
                        tbody = json.loads(tbody)
                    tgeo = [g for g in ((tbody.get("scope") or {}).get("geo") or []) if g]
                if tgeo:
                    args.append(tgeo)
                    where.append(f"geo && ${len(args)}::text[]")
            if since is not None:
                args.append(since)
                where.append(f"fetched_at >= ${len(args)}")
            if source_id is not None:
                args.append(source_id)
                where.append(f"source_id = ${len(args)}")
            if language is not None:
                args.append(language)
                where.append(f"language = ${len(args)}")
            if access_class_in is not None:
                classes = [c.strip() for c in access_class_in.split(",") if c.strip()]
                try:
                    where.append(_access.access_class_clause("", classes))
                except _access.AccessClassError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                    ) from exc
            if cursor is not None:
                cur_at, cur_id = _decode_cursor(cursor)
                args.append(cur_at)
                args.append(cur_id)
                where.append(
                    f"(fetched_at, id) < (${len(args) - 1}, ${len(args)})"
                )

            args.append(limit + 1)
            where_clause = f"WHERE {' AND '.join(where)}" if where else ""
            sql = f"""
                SELECT id, source_id, source_version, fetched_at, payload,
                       canonical_url, language, geo, tags, entity_classes,
                       source_credibility, content_hash, derived_from, schema_uri,
                       object_ref
                  FROM signals
                 {where_clause}
                 ORDER BY fetched_at DESC, id DESC
                 LIMIT ${len(args)}
            """
            rows = await conn.fetch(sql, *args)

        out = [_hydrate_signal(r, target_id=target_id) for r in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit and out:
            last = out[-1]
            next_cursor = _encode_cursor(last.produced_at, last.id)
        return SignalsPage(data=out, next_cursor=next_cursor)

    # ---------------- contention (Holes-B Wave 5, #101) ----------------

    @router.get("/contention", response_model=ContentionPage)
    async def list_contention(
        status_filter: ContentionStatus | None = Query(default=None, alias="status"),
        subject: str | None = Query(default=None),
        fact_id: str | None = Query(default=None),
        include_junk: bool = Query(default=False),
        since: datetime | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> ContentionPage:
        """List contested-claim groups + their per-value support clusters.

        Read-only SELECTs over the deployed `fact_contention` /
        `fact_contention_values` sidecar (migration 0055). Backs the UI's
        "Contested" badge + per-value support panel and the consult surface.

        Filters (all optional):
          * `status` — `contested` / `surfaced` / `collapsed`. UNSET defaults
            to the LIVE disputes only (`contested` + `surfaced`); pass
            `status=collapsed` to see resolved/folded groups.
          * `subject` — case-insensitive exact match on `subject_key`
            (the lower-cased subject). Lets the Why/fact view fetch the
            dispute for the fact it's rendering.
          * `fact_id` — return the single group a given `facts` row belongs to
            (resolved via `facts.contention_id`). The fact/Why view's direct
            lookup.
          * `include_junk` — when false (default), junk-gated value clusters
            (`is_junk=true`) are omitted from each group's `values`; the
            group's `junk_count` still reports how many were excluded.
        """
        limit = _validate_limit(limit)

        where: list[str] = []
        args: list[Any] = []

        if status_filter is not None:
            args.append(status_filter)
            where.append(f"fc.status = ${len(args)}")
        else:
            # Default to LIVE disputes only — a collapsed group is resolved.
            where.append("fc.status IN ('contested', 'surfaced')")
        if subject is not None:
            args.append(subject.strip().lower())
            where.append(f"fc.subject_key = ${len(args)}")
        if since is not None:
            args.append(since)
            where.append(f"fc.updated_at >= ${len(args)}")
        if cursor is not None:
            cur_at, cur_id = _decode_cursor(cursor)
            args.append(cur_at)
            args.append(cur_id)
            where.append(
                f"(fc.updated_at, fc.id) < (${len(args) - 1}, ${len(args)})"
            )

        async with deps.descriptor_registry.pg.acquire() as conn:
            # A `fact_id` filter resolves to the group that fact belongs to
            # (facts.contention_id). Done as a pre-resolve so it composes with
            # the other filters as a single extra group-id predicate.
            if fact_id is not None:
                try:
                    fact_uuid = UUID(fact_id)
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="fact_id must be a UUID",
                    )
                grp = await conn.fetchval(
                    "SELECT contention_id FROM facts WHERE id = $1",
                    fact_uuid,
                )
                if grp is None:
                    return ContentionPage(data=[], next_cursor=None)
                args.append(grp)
                where.append(f"fc.id = ${len(args)}")

            args.append(limit + 1)
            where_clause = f"WHERE {' AND '.join(where)}" if where else ""
            group_sql = f"""
                SELECT fc.id, fc.subject_key, fc.predicate_key, fc.status,
                       fc.surfaced_value, fc.value_count, fc.junk_count,
                       fc.opened_at, fc.resolved_at, fc.updated_at,
                       fc.surfaced_by, fc.surfaced_at, fc.surface_rationale
                  FROM fact_contention fc
                 {where_clause}
                 ORDER BY fc.updated_at DESC, fc.id DESC
                 LIMIT ${len(args)}
            """
            group_rows = await conn.fetch(group_sql, *args)

            # Per-group value clusters in arbiter order (winner / top score
            # first). One batched query keyed on the page's group ids — no
            # N+1. Junk clusters are filtered unless `include_junk`.
            page_groups = group_rows[:limit]
            values_by_group: dict[Any, list[Any]] = {}
            if page_groups:
                group_ids = [g["id"] for g in page_groups]
                junk_clause = "" if include_junk else "AND fcv.is_junk = false"
                value_rows = await conn.fetch(
                    f"""
                    SELECT fcv.contention_id, fcv.value_key,
                           fcv.representative_fact_id, fcv.distinct_source_count,
                           fcv.source_credibility_sum, fcv.confidence_max,
                           fcv.confidence_mean, fcv.source_types,
                           fcv.arbiter_score, fcv.surfaced_winner, fcv.is_junk,
                           fcv.junk_reason, fcv.latest_asserted_at
                      FROM fact_contention_values fcv
                     WHERE fcv.contention_id = ANY($1::uuid[])
                       {junk_clause}
                     ORDER BY fcv.surfaced_winner DESC,
                              fcv.arbiter_score DESC NULLS LAST,
                              fcv.distinct_source_count DESC
                    """,
                    group_ids,
                )
                for vr in value_rows:
                    values_by_group.setdefault(vr["contention_id"], []).append(vr)

        out = [
            _hydrate_contention(g, values_by_group.get(g["id"], []))
            for g in page_groups
        ]
        next_cursor: str | None = None
        if len(group_rows) > limit and out:
            last_group = page_groups[-1]
            next_cursor = _encode_cursor(last_group["updated_at"], last_group["id"])
        return ContentionPage(data=out, next_cursor=next_cursor)

    return router
