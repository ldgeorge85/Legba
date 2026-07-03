# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Substrate-slice readers (cluster M) — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Mapping

import asyncpg

from ..data.nats import SIGNALS_EXCLUDE_BACKFILL_SQL
from ..data.schemas.analyst import AnalystDescriptor

logger = logging.getLogger(__name__)


def _global_slice_per_source_cap() -> int:
    """Max rows any single source may contribute to a broad-pool analyst slice
    (FIX-3). Env LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP; default 15 (≤30% of the
    50-row slice) so a firehose source can't monopolise the global assessor."""
    raw = os.getenv("LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP")
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return 15


def _slice_row_cap() -> int:
    """Max signals the cadence substrate-slice reader returns to an analyst.

    Env ``LEGBA_SLICE_ROW_CAP``; default 120 (raised from the historical 50 in
    2026-06 so the inline_target INPUT-token budget — not a hard row count — is
    the real bound on assessment prompt size; the assembler then packs these
    down to its token budget)."""
    raw = os.getenv("LEGBA_SLICE_ROW_CAP")
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return 120


def _diversify_by_source(
    rows: list[Any], *, per_source_cap: int, limit: int,
) -> list[Any]:
    """Recency-preserving per-source diversity cap. Walks the recency-ordered
    rows, admitting at most ``per_source_cap`` per ``source_id`` until ``limit``
    is reached; then, if still short (few sources), back-fills the remainder in
    recency order so the slice is never smaller than the plain recency cut."""
    kept: list[Any] = []
    per_source: dict[Any, int] = {}
    overflow: list[Any] = []
    for r in rows:
        if len(kept) >= limit:
            break
        sid = r["source_id"]
        if per_source.get(sid, 0) < per_source_cap:
            kept.append(r)
            per_source[sid] = per_source.get(sid, 0) + 1
        else:
            overflow.append(r)
    if len(kept) < limit:
        kept.extend(overflow[: limit - len(kept)])
    return kept


# How many graph-structure shortlist items the slice reader folds in as typed
# input rows (the "interesting" structures the knowledge graph surfaced — scoped
# to the target, then topped up globally). Small: the structural picture is a few
# load-bearing entries, not the whole graph. Env-tunable.
def _slice_graph_structure_cap() -> int:
    raw = os.getenv("LEGBA_SLICE_GRAPH_STRUCTURE_CAP")
    if raw and raw.strip():
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return 8


# Human group labels for the shared ``interesting`` shortlist kinds, mirrored
# from grounding so the slice row title reads naturally without importing the
# private map. An unknown kind falls back to a de-underscored version.
_SLICE_INTERESTING_KIND_LABELS: dict[str, str] = {
    "tense_actor": "structurally tense actor",
    "broker": "broker between camps",
    "new_hostile_edge": "newly-hostile relationship",
    "sign_imbalanced_triad": "sign-imbalanced triad",
    "proxy_chain": "indirect / proxy link",
}


def _select_graph_structure_items(
    payloads: Mapping[str, Any], *, target_geo: list[str], limit: int,
    target_scoped: bool = False,
) -> list[dict[str, Any]]:
    """From the latest graph_metrics payloads, pick the ``interesting`` shortlist
    items scoped to the target — entries whose ``entities``/``label`` mention any
    of the target's geo scope FIRST (mirrors the signal slice's ``geo &&`` /
    entity filter so per-country scoping holds), then the highest-scored GLOBAL
    items to top up. A meta analyst (no ``target_geo``) gets the top global items.

    Items are merged across metric kinds + de-duplicated on (kind, label). Junk
    rows (no label, non-numeric score) are dropped. Returns the chosen contract
    items (dicts) ordered scope-first then score-desc, capped at ``limit``.

    ``target_scoped`` (D4 contamination fix): when True AND a ``target_geo``
    scope is present, the GLOBAL out-of-scope tail is DROPPED rather than used to
    top up the limit — a per-country slice must not inherit the globally-most-
    central (US-centric) structures as input rows. The slice reader passes True
    only for a per-country run; a META / no-target slice (no ``target_geo``)
    leaves it False so the global structures still feed the world assessor.
    """
    if limit <= 0:
        return []
    geo_lc = {g.casefold() for g in target_geo if isinstance(g, str) and g.strip()}
    merged: list[tuple[dict[str, Any], bool, float]] = []
    seen: set[tuple[str, str]] = set()
    for payload in payloads.values():
        raw = payload.get("interesting") if isinstance(payload, Mapping) else None
        if not isinstance(raw, list):
            continue
        for it in raw:
            if not isinstance(it, Mapping):
                continue
            label = it.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            kind = it.get("kind")
            kind = kind.strip() if isinstance(kind, str) and kind.strip() else "structure"
            key = (kind, label.strip().casefold())
            if key in seen:
                continue
            seen.add(key)
            try:
                score = float(it.get("score"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                score = 0.0
            entities = [
                str(e) for e in (it.get("entities") or []) if isinstance(e, str) and e.strip()
            ]
            in_scope = bool(geo_lc) and (
                any(e.casefold() in geo_lc for e in entities)
                or label.strip().casefold() in geo_lc
            )
            merged.append((dict(it), in_scope, score))
    # PER-COUNTRY: keep ONLY in-scope items (drop the global top-up). Only when a
    # geo scope actually exists — a scoped run with no geo degrades to global
    # rather than emitting nothing.
    if target_scoped and geo_lc:
        merged = [t for t in merged if t[1]]
    # scope-first, then by the producer's own score (descending).
    merged.sort(key=lambda t: (t[1], t[2]), reverse=True)
    return [it for it, _scope, _score in merged[:limit]]


async def _read_substrate_slice(
    conn: asyncpg.Connection,
    *,
    descriptor: AnalystDescriptor,
    target_filter: str | None,
) -> list[dict[str, Any]]:
    """Substrate slice for the analyst — honors descriptor.subscription.time_window.

    The descriptor's ``subscription.time_window`` field carries an
    integer hours value (e.g. 336 = 14 days) — read it and use as the
    cutoff. Falls back to a 24h window when the field is unset or
    invalid, which preserves the pre-2026-05-29 default for descriptors
    that haven't been updated. K-3 (predictor e2e) flagged this as a
    silent NOOP source — Brazil's 21 signals are 7+ days old, so a
    predictor with a 336h window was emitting `no_inputs` because the
    slice reader was looking at 24h instead.
    """
    # Read time_window off the descriptor (default 24h). The field is declared
    # on subscription.targets (SubscriptionTargets.time_window, e.g. "336h");
    # earlier code read it off the block, which has no such attribute, so EVERY
    # analyst silently fell back to 24h regardless of its descriptor — fatal for
    # the predictor (needs a multi-day daily series to forecast at all). Honor
    # subscription.targets.time_window first, then legacy flat attrs.
    window_hours = 24
    sub = getattr(descriptor, "subscription", None)
    if sub is not None:
        _targets = getattr(sub, "targets", None)
        cand = (
            (getattr(_targets, "time_window", None) if _targets is not None else None)
            or getattr(sub, "time_window", None)
            or getattr(sub, "time_window_hours", None)
        )
        if isinstance(cand, int) and cand > 0:
            window_hours = cand
        elif isinstance(cand, str) and cand.endswith("h"):
            try:
                window_hours = int(cand[:-1])
            except ValueError:
                pass

    # Source-first (pivot §4): signals are TARGET-AGNOSTIC — there is no
    # ``target_id`` column. A target's slice is the union of signals from its
    # subscribed sources. Narrow by the target's explicit ``source_id`` refs
    # when resolvable; otherwise read the recent tenant-wide pool. (The fine
    # per-binding residual match lives on the subscription engine's live
    # fan-out path; this cadence read is the coarse substrate fallback.)
    source_ids: list[str] = []
    target_geo: list[str] = []
    # Initialized BEFORE the target lookup so a NO-target META analyst
    # (target_filter is None: world_assessor / situation_clustering /
    # thematic_proposal / …) doesn't hit an UnboundLocalError at the
    # fetch_limit check below — those read the tenant-wide pool with no
    # predicate. Only a thematic target sets this to a real predicate.
    scope_predicate: str | None = None
    if target_filter:
        try:
            trow = await conn.fetchrow(
                "SELECT body FROM target_descriptors "
                "WHERE descriptor_id = $1 AND is_head = TRUE",
                target_filter,
            )
            if trow and trow["body"]:
                tbody = trow["body"]
                if isinstance(tbody, str):
                    tbody = json.loads(tbody)
                for sref in (tbody.get("sources") or []):
                    sid = sref.get("source_id") if isinstance(sref, dict) else None
                    if sid:
                        source_ids.append(sid)
                # The target's geo scope narrows the signal slice to its
                # country/region (now that the source baseline promotes
                # geo onto the indexed column). Selector-based targets carry
                # no explicit source_id, so geo is the primary per-target
                # discriminator — without it every country target would read
                # the same global pool (the duplicate-findings failure mode).
                scope = (tbody.get("scope") or {}) if isinstance(tbody, dict) else {}
                target_geo = [g for g in (scope.get("geo") or []) if g]
                # A THEMATIC target (non-geo) carries no geo discriminator; its
                # scope.predicate is what narrows the slice (5c). Applied
                # post-query below. Null for every existing target (geo-driven),
                # so the blast radius is exactly the new thematic targets.
                scope_predicate = scope.get("predicate") if isinstance(scope, dict) else None
        except Exception:                                       # pragma: no cover
            source_ids = []
            target_geo = []
            scope_predicate = None

    # This is a UNIT's fresh REACTIVE-window slice — exclude backfill (S4-T4).
    # A manually-ingested, backdated observation informs facts/grounding via the
    # accumulation paths but must NEVER appear as a "fresh" signal here.
    clauses = [
        f"fetched_at > NOW() - INTERVAL '{window_hours} hours'",
        SIGNALS_EXCLUDE_BACKFILL_SQL,
    ]
    params: list[Any] = []
    if source_ids:
        params.append(source_ids)
        clauses.append(f"source_id = ANY(${len(params)})")
    if target_geo:
        params.append(target_geo)
        # geo overlap — the signal mentions at least one of the target's
        # scoped countries/regions.
        clauses.append(f"geo && ${len(params)}::text[]")
    where = "WHERE " + " AND ".join(clauses)
    # The BROAD-POOL case = a global/meta analyst (world_assessor) with no source
    # narrowing, no geo, no predicate. Its recency-only LIMIT 50 gets dominated
    # by whatever source is highest-volume (live: NWS weather alerts ~70% of 24h
    # signals → 40/50 of the recency window), starving geopolitical signals so
    # the assessment is all hazards. Detect it + apply a per-source DIVERSITY cap
    # (FIX-3). When a scope.predicate is present we over-fetch for the residual
    # filter. Either way over-fetch so the post-pass isn't starved. The SELECT
    # carries the typed columns the residual ctx reads (entity_classes /
    # source_credibility / modality) so a predicate isn't fed nulls.
    is_broad_pool = not source_ids and not target_geo and not scope_predicate
    row_cap = _slice_row_cap()
    # Over-fetch on the broad/predicate paths so the post-pass (per-source
    # diversity / residual predicate filter) isn't starved before it fills row_cap.
    fetch_limit = max(200, row_cap * 3) if (scope_predicate or is_broad_pool) else row_cap
    rows = await conn.fetch(
        f"""
        SELECT id, source_id, source_version, canonical_url,
               payload, language, geo, tags, fetched_at, derived_from,
               entity_classes, source_credibility, modality
        FROM signals
        {where}
        ORDER BY fetched_at DESC
        LIMIT {fetch_limit}
        """,
        *params,
    )
    if scope_predicate:
        # Apply the target's scope.predicate to focus the slice (off the event
        # loop — the predicate engine's SIGALRM budget must not run under the
        # asyncio loop), then cap at the historical 50.
        from .subscription.filter import filter_rows_by_residual
        kept = await asyncio.to_thread(
            filter_rows_by_residual, scope_predicate, [dict(r) for r in rows]
        )
        rows = kept[:row_cap]
    elif is_broad_pool:
        # Per-source diversity cap: walk recency-ordered, admit ≤ cap rows per
        # source_id, until 50. A firehose source can't monopolise the slice;
        # geopolitical/news sources reach the assessor. Env-tunable.
        rows = _diversify_by_source(rows, per_source_cap=_global_slice_per_source_cap(), limit=row_cap)

    # Back-compat shaping: the analyst method + AnalystContext read the
    # historical signal-row keys (target_id/target_version/source_url/title/
    # data/produced_at). Map the new columns onto them so neither the LLM
    # prompt formatter nor the output-context plumbing needs a rewrite.
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        d["target_id"] = target_filter
        d["target_version"] = None
        d["source_url"] = d.get("canonical_url")
        d["title"] = payload.get("title") if isinstance(payload, dict) else None
        d["data"] = payload
        d["produced_at"] = d.get("fetched_at")
        out.append(d)

    # Graph-structure leg — feed the knowledge graph's own "interesting" shortlist
    # (the shared contract that structural_balance + graph_mining ADD to their
    # graph_metrics payload) into the assessor's input as clearly-typed CONTEXT
    # rows. This is the "feed that structure into the analytical products" payoff
    # on the PRIMARY product: a country target gets the structures involving its
    # entities (scoped via target_geo, mirroring the signal `geo &&` filter) plus
    # the top global ones; a meta analyst gets the top global ones. These are NOT
    # ground-truth facts — they carry id=None so the ORIENT phase excludes them
    # from `derived_from`, and a title/snippet that names them as analysis-derived
    # structure. Degrade-not-drop: any read/parse failure logs + adds nothing.
    struct_cap = _slice_graph_structure_cap()
    if struct_cap > 0:
        try:
            grows = await conn.fetch(
                """
                SELECT DISTINCT ON (metric_kind) metric_kind, payload
                FROM graph_metrics
                WHERE metric_kind IN ('structural_balance', 'graph_mining')
                ORDER BY metric_kind, computed_at DESC
                """
            )
            gpayloads: dict[str, Any] = {}
            for gr in grows:
                gp = gr["payload"]
                if isinstance(gp, str):
                    try:
                        gp = json.loads(gp)
                    except Exception:
                        gp = {}
                gpayloads[gr["metric_kind"]] = gp if isinstance(gp, dict) else {}
            # D4 contamination fix: a PER-COUNTRY run (a country_* target with a
            # geo scope) drops the global out-of-scope structure top-up so the
            # country slice never inherits the globally-most-central (US-centric)
            # structures. A META / no-target slice (target_filter None) keeps the
            # global structures — the global picture is correct for the global
            # assessor.
            per_country = bool(
                target_filter
                and isinstance(target_filter, str)
                and target_filter.strip().startswith("country")
            )
            items = _select_graph_structure_items(
                gpayloads, target_geo=target_geo, limit=struct_cap,
                target_scoped=per_country,
            )
            for it in items:
                kind = it.get("kind")
                kind = kind.strip() if isinstance(kind, str) and kind.strip() else "structure"
                klabel = _SLICE_INTERESTING_KIND_LABELS.get(kind, kind.replace("_", " "))
                label = str(it.get("label") or "").strip()
                rationale = it.get("rationale")
                rationale = rationale.strip() if isinstance(rationale, str) else ""
                # Render through the shared signal renderer: title carries the
                # typed structure label; the snippet (data.summary) carries the
                # producer's rationale so the assessor reads WHY it's interesting.
                summary = (
                    f"Analysis-derived knowledge-graph structure ({klabel}). {rationale}"
                ).strip()
                out.append(
                    {
                        "id": None,  # excluded from derived_from — context, not a fact
                        "source_id": "graph_metrics",
                        "source_version": None,
                        "canonical_url": None,
                        "source_url": None,
                        "language": None,
                        "geo": [],
                        "tags": ["assessed_structure", f"structure:{kind}"],
                        "fetched_at": None,
                        "derived_from": None,
                        "entity_classes": None,
                        "source_credibility": None,
                        "modality": None,
                        "target_id": target_filter,
                        "target_version": None,
                        "title": f"[ASSESSED STRUCTURE] {label}",
                        "produced_at": None,
                        "data": {
                            "kind": "assessed_structure",
                            "structure_kind": kind,
                            "summary": summary,
                            "entities": it.get("entities") or [],
                            "score": it.get("score"),
                        },
                    }
                )
        except Exception as exc:  # degrade-not-drop — structure leg is enrichment
            logger.warning("substrate_slice.graph_structure.failed err=%s", exc)

    return out
