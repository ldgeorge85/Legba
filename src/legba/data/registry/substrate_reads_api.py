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

from .api import RegistryAPIDeps, require_bearer


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
    values: list[ContentionValueRow] = Field(default_factory=list)


class FindingsPage(BaseModel):
    data: list[FindingRow]
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
    )


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

    # ---------------- findings ----------------

    @router.get("/findings", response_model=FindingsPage)
    async def list_findings(
        since: datetime | None = Query(default=None),
        target_id: str | None = Query(default=None),
        target_id_null: bool = Query(default=False),
        analyst_id: str | None = Query(default=None),
        analyst_id_in: str | None = Query(default=None),
        severity: Severity | None = Query(default=None),
        q: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> FindingsPage:
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
        if cursor is not None:
            cur_at, cur_id = _decode_cursor(cursor)
            args.append(cur_at)
            args.append(cur_id)
            where.append(
                f"(f.produced_at, f.id) < (${len(args) - 1}, ${len(args)})"
            )

        args.append(limit + 1)
        # The lateral picks the LATEST critique per finding and surfaces BOTH the
        # gate input (overall_score) AND the P0-T3 faithfulness-verify detail
        # block (cr.data->'data'->'verification' — written by the verify pass via
        # CritiquePayload.data). The verification block is NULL for a finding with
        # no faithfulness critique, so the API surfaces no fabricated block and
        # effective_confidence stays == confidence (no regression).
        sql = f"""
            SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
                   f.data, f.target_id, f.target_version, f.analyst_id,
                   f.analyst_version, f.produced_at, f.derived_from,
                   f.schema_uri, f.run_id, f.created_at,
                   c.critic_score AS critic_score,
                   c.verification AS verification
              FROM analyst_outputs f
              LEFT JOIN LATERAL (
                  SELECT (cr.data->>'overall_score')::real AS critic_score,
                         (cr.data->'data'->'verification') AS verification
                    FROM analyst_outputs cr
                   WHERE cr.kind = 'critique'
                     AND cr.data->>'analyzed_output_id' = f.id::text
                     AND cr.data->>'overall_score' IS NOT NULL
                   ORDER BY cr.produced_at DESC, cr.id DESC
                   LIMIT 1
              ) c ON TRUE
             WHERE {' AND '.join(where)}
             ORDER BY f.produced_at DESC, f.id DESC
             LIMIT ${len(args)}
        """

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        out = [_hydrate_finding(r) for r in rows[:limit]]
        next_cursor: str | None = None
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
        limit: int = Query(default=DEFAULT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> SituationsPage:
        limit = _validate_limit(limit)

        where: list[str] = []
        args: list[Any] = []

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

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        out = [_hydrate_situation(r) for r in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit and out:
            last = out[-1]
            next_cursor = _encode_cursor(last.produced_at, last.id)
        return SituationsPage(data=out, next_cursor=next_cursor)

    # ---------------- signals ----------------

    @router.get("/signals", response_model=SignalsPage)
    async def list_signals(
        target_id: str | None = Query(default=None),
        since: datetime | None = Query(default=None),
        source_id: str | None = Query(default=None),
        language: str | None = Query(default=None),
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
                       source_credibility, content_hash, derived_from, schema_uri
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
                       fc.opened_at, fc.resolved_at, fc.updated_at
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
