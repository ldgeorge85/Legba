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


# How many candidate structures to pull per final row before the duplicate
# collapse (:func:`_collapse_structure_items`). Duplicate RUNS are the observed
# shape — five identical Belgium-Egypt-IGO triads in a row — so a modest
# multiplier is enough to refill the cap with DISTINCT structures; the merge +
# sort it feeds is over an already-bounded in-memory shortlist, so the extra
# candidates cost nothing measurable.
_STRUCTURE_OVERSELECT = 6


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


def _structure_dedup_key(item: Mapping[str, Any]) -> tuple[str, str]:
    """Grouping key for :func:`_collapse_structure_items`.

    Two shortlist items are DUPLICATE RENDERS when they share a structure kind
    AND a rationale — the rationale is the entire body of the rendered snippet,
    so same kind + same rationale means the two rows differ ONLY in their
    label, which is exactly the run the P5 gallery caught.
    """
    kind = item.get("kind")
    kind = kind.strip() if isinstance(kind, str) and kind.strip() else "structure"
    rationale = item.get("rationale")
    rationale = rationale.strip().casefold() if isinstance(rationale, str) else ""
    return (kind, rationale)


def _collapse_structure_items(
    items: list[dict[str, Any]], *, limit: int,
) -> list[tuple[dict[str, Any], list[str]]]:
    """Collapse duplicate-render structure items into one row each, keeping counts.

    P5 finding: a META slice's tail ran five CONSECUTIVE
    ``[ASSESSED STRUCTURE]`` pseudo-signals whose snippets were byte-identical
    ("unbalanced signed triad (sign product negative — 1 hostile edge(s));
    Heider-unstable, predicts realignment") and whose titles differed only in
    the third vertex (Belgium-Egypt-IMO, -IHO, -OIF, -UNESCO, -IBRD). Five
    numbered rows, five citation slots, ~900 chars — for ONE structural fact
    with five instances.

    Collapsing groups them on (kind, rationale) and returns the FIRST item of
    each group with its sibling labels, so the caller can render one row that
    names every vertex and states the count. No label is lost — the repetition
    is. Because the group is capped AFTER collapsing, a slice that used to
    spend its whole structure budget on one repeated fact now carries up to
    ``limit`` DISTINCT structural facts.

    Order is preserved (scope-first then score-desc, per
    :func:`_select_graph_structure_items`). With no duplicates the result is
    byte-for-byte the pre-collapse behavior: the first ``limit`` items, each
    with an empty sibling list.
    """
    grouped: dict[tuple[str, str], tuple[dict[str, Any], list[str]]] = {}
    for it in items:
        key = _structure_dedup_key(it)
        label = str(it.get("label") or "").strip()
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = (it, [])
            continue
        if label and label not in existing[1] and label != str(
            existing[0].get("label") or ""
        ).strip():
            existing[1].append(label)
    return list(grouped.values())[:limit]


#: Slice window used when a descriptor declares none — the pre-2026-05-29
#: default, kept so an un-updated descriptor reads what it always read.
DEFAULT_SLICE_WINDOW_HOURS: int = 24


def resolve_slice_window_hours(descriptor: AnalystDescriptor) -> int:
    """The descriptor's slice window in hours (default 24).

    The field is declared on ``subscription.targets``
    (``SubscriptionTargets.time_window``, e.g. ``"336h"``); earlier code read it
    off the block, which has no such attribute, so EVERY analyst silently fell
    back to 24h regardless of its descriptor — fatal for the predictor, which
    needs a multi-day daily series to forecast at all. Honors
    ``subscription.targets.time_window`` first, then the legacy flat attrs.

    EXTRACTED from ``_read_substrate_slice`` (Phase-V D8a) because the value now
    has a SECOND consumer: the actor stamps it into ``options`` so the unit's
    rendered slice header can state the window it was actually cut with. The
    defect that motivated the extraction was ``narrative_coordination``'s prompt
    asserting a "last-24h signal slice" while its descriptor declared 72h — the
    unit read three days and told the operator it read one, for weeks, on a
    question (synchrony) where the window IS the measurement. One resolver,
    two consumers, no way for the prose and the query to disagree again.
    """
    sub = getattr(descriptor, "subscription", None)
    if sub is None:
        return DEFAULT_SLICE_WINDOW_HOURS
    _targets = getattr(sub, "targets", None)
    cand = (
        (getattr(_targets, "time_window", None) if _targets is not None else None)
        or getattr(sub, "time_window", None)
        or getattr(sub, "time_window_hours", None)
    )
    if isinstance(cand, bool):
        return DEFAULT_SLICE_WINDOW_HOURS
    if isinstance(cand, int) and cand > 0:
        return cand
    if isinstance(cand, str) and cand.endswith("h"):
        try:
            parsed = int(cand[:-1])
        except ValueError:
            return DEFAULT_SLICE_WINDOW_HOURS
        if parsed > 0:
            return parsed
    return DEFAULT_SLICE_WINDOW_HOURS


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
    # Piece 2 (gather_only): an analyst that OPTS IN via
    # ``subscription.substrate.gather_only`` does NOT consume this coarse cadence
    # slice — it gathers its own evidence live through the GATHER read tools
    # (search_corpus over the full corpus, list_situations, …). Return [] so the
    # run reaches GATHER with an empty slice instead of a firehose recency pool.
    # Safe: ONLY a gather_only descriptor opts in; every other analyst is
    # byte-for-byte unchanged (the flag defaults absent/false).
    _sub = getattr(descriptor, "subscription", None)
    if (getattr(_sub, "substrate", {}) or {}).get("gather_only"):
        return []

    window_hours = resolve_slice_window_hours(descriptor)

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
        # C2b: canonical-only. Snapshot feeds (NWS active-alerts, recent-quake
        # endpoints) re-ingest the same item on every poll; dedup stamps each
        # dup's ``canonical_signal_id`` to the kept row. Without this clause a
        # UNIT slice fills with re-polled alias duplicates as separate numbered
        # citations (one quake cited 251x — false corroboration + wasted
        # context). Mirrors the subscription path (runtime/subscription/filter.py
        # canonical_only) and the reads API (registry/substrate_reads_api.py).
        "(canonical_signal_id IS NULL OR canonical_signal_id = id)",
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
    # C2b: apply the per-source diversity cap to the broad global pool AND to
    # geo-scoped country slices — a firehose source (live: NWS = 46/50 of a US
    # slice) must not monopolise a desk's window. A source-id-narrowed slice is
    # already scoped to the target's own chosen feeds, so it is left uncapped.
    apply_diversity_cap = is_broad_pool or bool(target_geo)
    row_cap = _slice_row_cap()
    # Over-fetch on the predicate/diversity paths so the post-pass (per-source
    # diversity / residual predicate filter) isn't starved before it fills row_cap.
    fetch_limit = max(200, row_cap * 3) if (scope_predicate or apply_diversity_cap) else row_cap
    rows = await conn.fetch(
        f"""
        SELECT id, source_id, source_version, canonical_url,
               payload, language, geo, tags, fetched_at, derived_from,
               entity_classes, source_credibility, modality, salience
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
    elif apply_diversity_cap:
        # Per-source diversity cap: walk recency-ordered, admit ≤ cap rows per
        # source_id, until row_cap. A firehose source can't monopolise the slice
        # (broad-pool OR geo-scoped); geopolitical/news sources reach the
        # assessor. Back-fills if diversity is exhausted, so a thin single-source
        # day is never smaller than the plain recency cut. Env-tunable.
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
            # D4 contamination guard: scope whenever the TARGET carries a geo
            # discriminator — country desks AND thematic lane_/flow_ targets
            # (which carry geo sets too). Keying on the "country" name prefix
            # let thematic targets leak globally-ranked structure rows into
            # their evidence (gallery P1 finding: Hormuz carrying Belgium/
            # Afghanistan noise). A META/no-target run (no geo) stays global.
            per_country = bool(
                target_filter
                and isinstance(target_filter, str)
                and bool(target_geo)
            )
            # Over-select, then collapse duplicate renders (P5: five identical
            # Belgium-Egypt-IGO triads in a row), then cap — so the structure
            # budget buys ``struct_cap`` DISTINCT structural facts rather than
            # one fact repeated ``struct_cap`` times. The over-fetch multiplier
            # only matters when duplicates exist; with none, the collapse is a
            # no-op over the same first ``struct_cap`` items as before.
            candidates = _select_graph_structure_items(
                gpayloads, target_geo=target_geo,
                limit=struct_cap * _STRUCTURE_OVERSELECT,
                target_scoped=per_country,
            )
            collapsed_total = 0
            for it, siblings in _collapse_structure_items(
                candidates, limit=struct_cap
            ):
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
                if siblings:
                    # Keep the COUNT and every collapsed vertex — one row that
                    # names all instances beats N rows repeating one rationale.
                    collapsed_total += len(siblings)
                    summary = (
                        f"{summary} {len(siblings) + 1} instances of this same "
                        f"structure were found; the others are: "
                        f"{'; '.join(siblings)}."
                    )
                    label = f"{label} (+{len(siblings)} more)"
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
                            # Receipt: how many duplicate-render siblings this
                            # row absorbed. Read by inline_target's ORIENT
                            # ``structures_collapsed`` counter; absent/0 on a
                            # row that collapsed nothing.
                            "duplicates_collapsed": len(siblings),
                        },
                    }
                )
            if collapsed_total:
                logger.info(
                    "substrate_slice.graph_structure.collapsed target=%s "
                    "candidates=%d rows=%d duplicates_collapsed=%d",
                    target_filter, len(candidates),
                    min(struct_cap, len(candidates)), collapsed_total,
                )
        except Exception as exc:  # degrade-not-drop — structure leg is enrichment
            logger.warning("substrate_slice.graph_structure.failed err=%s", exc)

    # QW1-B — DESK GROUNDING leg. The composition floor got a memory in Phase 1
    # (``meta_findings_synthesizer``, grep CONTINUITY); this is the SAME idiom one
    # floor down, widened from two blocks to five: the unit's own PRIOR READ of
    # this target, its WINDOW LEDGER (FRAME-2 — this unit's own dated, verified,
    # severity-tagged reads of the trailing fortnight, the carry that stops a
    # 72-hour read forgetting its own window), the desk's OPEN SITUATION
    # REGISTER, its DESK BASELINE (what is normal here), and its STANDING OPEN
    # QUESTIONS. Each arrives as a MARKED row
    # (``UNIT_GROUNDING_ROW_KEY``) that ``inline_target.run_method`` lifts out of
    # the evidence slice before ORIENT and renders as its own citable ``[N]``
    # block — never as a signal, never in ``derived_from``.
    #
    # THREE GATES, all load-bearing:
    #   * kind — ONLY ``inline_target``. It is the one kind whose run_method knows
    #     how to partition and render these rows; handing a marked row to any
    #     other kind would feed it a pseudo-signal it would read as evidence.
    #   * target — a desk id is required. Every block is desk-scoped by
    #     construction and an unscoped read would hand a desk another desk's
    #     frames (the D4 contamination class).
    #   * non-empty slice — an EMPTY slice must stay empty so the actor's
    #     ``no_inputs`` NOOP still fires. Synthesizing off memory alone, with no
    #     current evidence, is the fabrication this platform refuses.
    # ``identity.kind`` is DECLARED ``str`` but a caller may hand in the
    # ``AnalystKind`` member (it is a str-Enum, so pydantic strict-mode keeps it).
    # ``str(AnalystKind.INLINE_TARGET)`` is ``'AnalystKind.INLINE_TARGET'``, not
    # the value — so unwrap ``.value`` first or the gate silently never fires.
    _identity = getattr(descriptor, "identity", None)
    _kind_raw = getattr(_identity, "kind", None)
    _kind = str(getattr(_kind_raw, "value", _kind_raw) or "")
    if out and target_filter and _kind == "inline_target":
        try:
            from ..data.analysts.unit_grounding import gather_unit_grounding_rows

            out.extend(
                await gather_unit_grounding_rows(
                    conn,
                    analyst_id=getattr(_identity, "id", None),
                    target_filter=target_filter,
                )
            )
        except Exception as exc:  # degrade-not-drop — grounding leg is enrichment
            logger.warning("substrate_slice.unit_grounding.failed err=%s", exc)

    return out
