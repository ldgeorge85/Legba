# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P4b — the graph projector: whole-rebuild of the cross-layer arc store.

The judge's verdict (``planning/graph_debate/JUDGE_SYNTHESIS.md``) killed every
incremental mirror: each dead mirror in this codebase was an incremental
mirror whose delete path was never written. This handler therefore has NO
incremental path — it builds the whole ``graph_arcs`` table from source each
run into ``graph_arcs_new`` (one ``INSERT ... SELECT`` per source table), then
swaps the two tables inside a single transaction:

    ALTER TABLE graph_arcs     RENAME TO graph_arcs_old;
    ALTER TABLE graph_arcs_new RENAME TO graph_arcs;
    DROP TABLE graph_arcs_old;

Readers see the old snapshot until the commit lands and the new one
atomically after — there is no half-built read state and no divergence class:
a structure only ever produced by SELECT can be stale, never wrong. Staleness
is the published ``graph_arcs_meta.projected_at`` scalar; build cost is
``build_seconds`` — the E4 trigger reading (``graph_triggers_api._E4_SQL``);
``source_counts`` is the per-``src_table`` actual the S-1 parity loop reads.

**Determinism** (spec §4.2 rule 2): every arc column is a pure function of the
source rows — no ``now()``, no wall clock, no sampling — so two builds over
the same substrate snapshot are row-identical.

**The gate**: ``LEGBA_GRAPH_PROJECTION`` (default OFF) — while off the handler
returns an honest no-op receipt and the table is never touched.

**Deliberate exclusions** (spec §4.1): ``proposed_edges`` is a candidate
queue, not a graph (promoted rows arrive via ``entity_edges``);
``journal_entries`` is off-chain by construction — a projection including it
would let a graph walk surface a journal node.

Descriptor: ``descriptors/analyst_graph_projector.yaml`` (state: draft).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping, Sequence

from ...graph_projection import graph_projection_enabled
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "graph_projector"

#: Deploy marker — the runbook's grep target. Bump on any vocabulary change.
#: P4b-D1 (2026-09-23): EVERY table name in this module is schema-qualified.
#: The runtime pool sets ``search_path = ag_catalog, "$user", public``, so an
#: unqualified ``CREATE TABLE graph_arcs_new`` landed in ag_catalog on the
#: first live build, the swap renamed public.graph_arcs away and dropped it,
#: and the projection lived in the AGE schema for two builds. Migration 0210
#: moved it back; this file never relies on the search path again.
GRAPH_PROJECTION_VERSION = "2026-09/p4b"

#: The arc column list every source INSERT shares — spelled once so a column
#: added to the table fails loud in every statement at once.
_ARC_COLS = (
    "from_kind, from_id, to_kind, to_id, arc_type, plane, family, polarity, "
    "confidence, valid_from, valid_until, src_table, src_id"
)

#: uuid regex — citations are free-form jsonb and a malformed id must not
#: abort the whole build on a cast error.
_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

#: uuid5 namespace for the deterministic node ids of TEXT-keyed endpoints
#: (narrative_echo_edges is keyed by source names, not uuids). URL namespace.
_TEXT_NODE_NS = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

# ---------------------------------------------------------------------------
# The source table — one INSERT ... SELECT per source, keyed by src_table so
# `source_counts` is directly the S-1 parity surface. Each statement is a pure
# projection: no now(), no randomness, no ordering-dependent state.
# ---------------------------------------------------------------------------

_SOURCES: Sequence[tuple[str, str]] = (
    # -- EVIDENCE plane ------------------------------------------------------
    # signal -> entity `mentions`. A signal can link the same entity under
    # several roles; the arc key is role-blind, so keep the most confident one.
    ("signal_entity_links", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'signal', sel.signal_id, 'entity', sel.entity_id, 'mentions',
       'evidence', sel.role, 0, sel.confidence, sel.created_at, NULL,
       'signal_entity_links', NULL
  FROM (
      SELECT DISTINCT ON (signal_id, entity_id)
             signal_id, entity_id, role, confidence, created_at
        FROM signal_entity_links
       ORDER BY signal_id, entity_id, confidence DESC, created_at DESC, role
  ) sel
ON CONFLICT DO NOTHING
"""),
    # signal -> signal `duplicate_of` (alias rows).
    ("signal_aliases", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'signal', sa.alias_signal_id, 'signal', sa.canonical_signal_id,
       'duplicate_of', 'evidence', sa.reason, 0, sa.score, sa.produced_at,
       NULL, 'signal_aliases', NULL
  FROM signal_aliases sa
 WHERE sa.canonical_signal_id <> sa.alias_signal_id
ON CONFLICT DO NOTHING
"""),
    # signal -> signal `duplicate_of` (canonical_signal_id redirects).
    ("signals", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'signal', s.id, 'signal', s.canonical_signal_id, 'duplicate_of',
       'evidence', 'canonical', 0, NULL, s.created_at, NULL, 'signals', s.id
  FROM signals s
 WHERE s.canonical_signal_id IS NOT NULL AND s.canonical_signal_id <> s.id
ON CONFLICT DO NOTHING
"""),
    # -- WORLD plane ----------------------------------------------------------
    # signal -> event `evidences`; family carries the link's source_class.
    ("signal_event_links", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'signal', l.signal_id, 'event', l.event_id, 'evidences', 'world',
       l.source_class, 0, l.relevance, l.linked_at, NULL,
       'signal_event_links', NULL
  FROM signal_event_links l
ON CONFLICT DO NOTHING
"""),
    # entity -> entity typed edges. A superseded-but-open row closes at its
    # own last_seen_at — the honest bound the row itself carries (its real
    # supersession instant was never stored).
    ("entity_edges", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'entity', e.src_id, 'entity', e.dst_id, e.edge_type, 'world',
       e.edge_family, e.polarity, e.confidence,
       COALESCE(e.valid_from, e.first_seen_at),
       CASE WHEN e.superseded_by IS NOT NULL
            THEN COALESCE(e.valid_until, e.last_seen_at)
            ELSE e.valid_until END,
       'entity_edges', e.id
  FROM entity_edges e
ON CONFLICT DO NOTHING
"""),
    # entity -> entity `via` — the reified proxy channel: two hops per
    # intermediated edge (src->intermediary, intermediary->dst).
    ("entity_edges", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'entity', e.src_id, 'entity', e.intermediary_id, 'via', 'world',
       e.edge_family, e.polarity, e.confidence,
       COALESCE(e.valid_from, e.first_seen_at),
       CASE WHEN e.superseded_by IS NOT NULL
            THEN COALESCE(e.valid_until, e.last_seen_at)
            ELSE e.valid_until END,
       'entity_edges', e.id
  FROM entity_edges e WHERE e.intermediary_id IS NOT NULL
UNION ALL
SELECT 'entity', e.intermediary_id, 'entity', e.dst_id, 'via', 'world',
       e.edge_family, e.polarity, e.confidence,
       COALESCE(e.valid_from, e.first_seen_at),
       CASE WHEN e.superseded_by IS NOT NULL
            THEN COALESCE(e.valid_until, e.last_seen_at)
            ELSE e.valid_until END,
       'entity_edges', e.id
  FROM entity_edges e WHERE e.intermediary_id IS NOT NULL
ON CONFLICT DO NOTHING
"""),
    # entity -> event `involved_in` (actors / places on occurrences).
    ("event_entity_links", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'entity', l.entity_id, 'event', l.event_id, 'involved_in', 'world',
       l.role, 0, l.confidence, l.created_at, NULL, 'event_entity_links', NULL
  FROM event_entity_links l
ON CONFLICT DO NOTHING
"""),
    # event -> event typed edges (part_of / caused_by / evolves_from /
    # correlated_with / contradicts).
    ("event_edges", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'event', e.src_event_id, 'event', e.dst_event_id, e.edge_type, 'world',
       e.edge_type, 0, e.confidence,
       COALESCE(e.valid_from, e.created_at),
       CASE WHEN e.superseded_by IS NOT NULL
            THEN COALESCE(e.valid_until, e.updated_at)
            ELSE e.valid_until END,
       'event_edges', e.id
  FROM event_edges e
ON CONFLICT DO NOTHING
"""),
    # event -> situation `tracked_by`.
    ("situation_event_links", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'event', l.event_id, 'situation', l.situation_id, 'tracked_by',
       'world', 'membership', 0, l.relevance, l.created_at, NULL,
       'situation_event_links', NULL
  FROM situation_event_links l
ON CONFLICT DO NOTHING
"""),
    # fact -> entity `subject_of` / `value_of` — resolve_entity_name() is the
    # 0143 name->terminal-survivor resolver; unresolved names emit no arc.
    ("facts", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'fact', f.id, 'entity', r.entity_id, 'subject_of', 'world',
       'subject', 0, f.confidence,
       COALESCE(f.valid_from, f.produced_at), NULL, 'facts', f.id
  FROM facts f
  CROSS JOIN LATERAL (SELECT resolve_entity_name(f.subject) AS entity_id) r
 WHERE r.entity_id IS NOT NULL
ON CONFLICT DO NOTHING
"""),
    ("facts", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'fact', f.id, 'entity', r.entity_id, 'value_of', 'world',
       'value', 0, f.confidence,
       COALESCE(f.valid_from, f.produced_at), NULL, 'facts', f.id
  FROM facts f
  CROSS JOIN LATERAL (SELECT resolve_entity_name(f.value) AS entity_id) r
 WHERE r.entity_id IS NOT NULL
ON CONFLICT DO NOTHING
"""),
    # fact -> fact `contends_with` — pairwise arcs between the NON-junk value
    # clusters of each still-contested (subject, predicate) dispute.
    ("fact_contention_values", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'fact', a.representative_fact_id, 'fact', b.representative_fact_id,
       'contends_with', 'world', c.predicate_key, 0, NULL,
       c.opened_at, c.resolved_at, 'fact_contention_values', a.id
  FROM fact_contention_values a
  JOIN fact_contention_values b
    ON b.contention_id = a.contention_id AND b.id > a.id
  JOIN fact_contention c ON c.id = a.contention_id
 WHERE c.status = 'contested'
   AND a.representative_fact_id IS NOT NULL
   AND b.representative_fact_id IS NOT NULL
   AND NOT a.is_junk AND NOT b.is_junk
ON CONFLICT DO NOTHING
"""),
    # narrative -> narrative `echoes`. narrative_echo_edges is keyed by source
    # NAMES (text) — the node id is uuid5(ns, 'legba:narrative-source:'||id),
    # deterministic so two builds produce identical endpoints.
    ("narrative_echo_edges", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'narrative',
       uuid_generate_v5('{_TEXT_NODE_NS}'::uuid,
                        'legba:narrative-source:' || n.leader_source_id),
       'narrative',
       uuid_generate_v5('{_TEXT_NODE_NS}'::uuid,
                        'legba:narrative-source:' || n.follower_source_id),
       'echoes', 'world',
       CASE WHEN n.systematic THEN 'systematic' ELSE 'incidental' END,
       0, n.echo_ratio, n.computed_at, NULL, 'narrative_echo_edges', NULL
  FROM narrative_echo_edges n
 WHERE n.leader_source_id <> n.follower_source_id
ON CONFLICT DO NOTHING
"""),
    # -- LINEAGE plane --------------------------------------------------------
    # situation -> finding `member_of` — the situation's composing findings.
    ("situations", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'situation', s.id, 'finding', ref, 'member_of', 'lineage',
       s.category, 0, NULL, s.produced_at, NULL, 'situations', s.id
  FROM situations s
  CROSS JOIN LATERAL unnest(s.derived_from) AS ref
ON CONFLICT DO NOTHING
"""),
    # situation_event -> situation `moved_by` — the 0184 trajectory ledger row
    # as a node; the delta is the family. (Spec §4.1 names the endpoints
    # "situation | situation"; the ledger row, not the situation, is the mover
    # — a self-loop would carry no information.)
    ("situation_events", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'situation_event', se.id, 'situation', se.situation_id, 'moved_by',
       'lineage', se.delta, 0, NULL, se.occurred_at, NULL,
       'situation_events', se.id
  FROM situation_events se
ON CONFLICT DO NOTHING
"""),
    # finding -> signal|event|finding `cites` — derived_from lineage, with the
    # endpoint kind resolved by which table actually holds the id. Refs that
    # resolve to nothing emit no arc — an unresolvable kind is not a guess.
    ("analyst_outputs", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'finding', ao.id, r.to_kind, ref.ref, 'cites', 'lineage', ao.kind, 0,
       NULL, ao.produced_at,
       CASE WHEN ao.superseded_by IS NOT NULL
            THEN COALESCE(ao.superseded_at, ao.created_at) END,
       'analyst_outputs', ao.id
  FROM analyst_outputs ao
  CROSS JOIN LATERAL unnest(ao.derived_from) AS ref(ref)
  CROSS JOIN LATERAL (
      SELECT CASE
                 WHEN EXISTS (SELECT 1 FROM signals    WHERE id = ref.ref)
                      THEN 'signal'
                 WHEN EXISTS (SELECT 1 FROM events     WHERE id = ref.ref)
                      THEN 'event'
                 WHEN EXISTS (SELECT 1 FROM analyst_outputs
                               WHERE id = ref.ref)
                      THEN 'finding'
             END AS to_kind
  ) r
 WHERE r.to_kind IS NOT NULL
ON CONFLICT DO NOTHING
"""),
    # finding -> signal|event `cites` — the model-authored citations[] block.
    ("analyst_outputs", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'finding', ao.id, x.to_kind, x.to_id, 'cites', 'lineage', ao.kind, 0,
       NULL, ao.produced_at,
       CASE WHEN ao.superseded_by IS NOT NULL
            THEN COALESCE(ao.superseded_at, ao.created_at) END,
       'analyst_outputs', ao.id
  FROM analyst_outputs ao
  CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(ao.data->'data'->'citations') = 'array'
           THEN ao.data->'data'->'citations'
           WHEN jsonb_typeof(ao.data->'citations') = 'array'
           THEN ao.data->'citations'
           ELSE '[]'::jsonb END
  ) AS cit
  CROSS JOIN LATERAL (
      SELECT 'signal' AS to_kind, (cit->>'signal_id')::uuid AS to_id
       WHERE cit->>'signal_id' ~ '{_UUID_RE}'
      UNION ALL
      SELECT 'event', (cit->>'event_id')::uuid
       WHERE cit->>'event_id' ~ '{_UUID_RE}'
  ) x
ON CONFLICT DO NOTHING
"""),
    # finding -> finding `cites` — the consumption ledger (a composition read
    # F is the cleanest "F was cited by" evidence there is).
    ("output_consumption", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'finding', oc.consumer_id, 'finding', oc.consumed_id, 'cites',
       'lineage', oc.context, 0, NULL, oc.consumed_at, NULL,
       'output_consumption', NULL
  FROM output_consumption oc
ON CONFLICT DO NOTHING
"""),
    # finding -> finding `supersedes` — the supersession chain.
    ("analyst_outputs", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'finding', ao.id, 'finding', ao.superseded_by, 'supersedes',
       'lineage', ao.kind, 0, NULL,
       COALESCE(ao.superseded_at, ao.produced_at), NULL,
       'analyst_outputs', ao.id
  FROM analyst_outputs ao
 WHERE ao.superseded_by IS NOT NULL
ON CONFLICT DO NOTHING
"""),
    # finding -> finding `judged_by` — a critique's verdict arc.
    ("analyst_outputs", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'finding', (ao.data->>'analyzed_output_id')::uuid, 'finding', ao.id,
       'judged_by', 'lineage', ao.kind, 0, NULL, ao.produced_at,
       CASE WHEN ao.superseded_by IS NOT NULL
            THEN COALESCE(ao.superseded_at, ao.created_at) END,
       'analyst_outputs', ao.id
  FROM analyst_outputs ao
 WHERE ao.kind = 'critique'
   AND ao.data->>'analyzed_output_id' ~ '{_UUID_RE}'
ON CONFLICT DO NOTHING
"""),
    # signal|finding -> hypothesis `bears_on` — kinds pass through verbatim;
    # bearing_edges.src_kind/dst_kind are already the node-kind vocabulary.
    ("bearing_edges", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT b.src_kind, b.src_id, b.dst_kind, b.dst_id, 'bears_on', 'lineage',
       b.edge_kind, 0, b.weight, b.src_as_of, NULL, 'bearing_edges', b.id
  FROM bearing_edges b
ON CONFLICT DO NOTHING
"""),
    # -- EVIDENCE plane (entity merge tombstones) -----------------------------
    ("entity_profiles", f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'entity', ep.id, 'entity', ep.merged_into, 'merged_into', 'evidence',
       'merge', 0, NULL, ep.updated_at, NULL, 'entity_profiles', ep.id
  FROM entity_profiles ep
 WHERE ep.merged_into IS NOT NULL
ON CONFLICT DO NOTHING
"""),
)

#: The atomic swap — the serving table changes hands inside ONE transaction
#: so a reader never sees a half-built projection. The staging table's
#: LIKE-copied indexes carry auto-generated ``graph_arcs_new_*`` names after
#: the rename; cosmetic only.
_SWAP_SQL = (
    "ALTER TABLE public.graph_arcs RENAME TO graph_arcs_old",
    "ALTER TABLE public.graph_arcs_new RENAME TO graph_arcs",
    "DROP TABLE public.graph_arcs_old",
)

_META_UPSERT_SQL = """
INSERT INTO public.graph_arcs_meta
    (id, projected_at, arc_count, source_counts, build_seconds)
VALUES (true, now(), $1, $2::jsonb, $3)
ON CONFLICT (id) DO UPDATE SET
    projected_at  = EXCLUDED.projected_at,
    arc_count     = EXCLUDED.arc_count,
    source_counts = EXCLUDED.source_counts,
    build_seconds = EXCLUDED.build_seconds
"""


def _finding(data: Mapping[str, Any], *, changed: bool) -> AnalystMethodResult:
    """The projector's per-run receipt (never lands on a trust surface)."""
    return AnalystMethodResult(
        finding=FindingPayload(
            title=f"Graph projector ({'rebuilt' if changed else 'no-op'})",
            body=(
                f"graph_projector {GRAPH_PROJECTION_VERSION}: {dict(data)}"
            ),
            confidence=1.0,
            evidence=[],
            tags=["deterministic", SUB_HANDLER_NAME, "v3_graph"],
            data={
                "sub_handler": SUB_HANDLER_NAME,
                "pipeline_version": GRAPH_PROJECTION_VERSION,
                **dict(data),
            },
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        derived_from=[],
        force_trace_only=not changed,
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    """Descriptor options arrive loosely typed; be explicit, not clever."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def handle(
    inputs: Sequence[Mapping[str, Any]] | None = None,
    options: Mapping[str, Any] | None = None,
    deps: Any = None,
    **_kwargs: Any,
) -> AnalystMethodResult:
    """Whole-rebuild ``graph_arcs`` from every projected source table.

    Flag-gated on ``LEGBA_GRAPH_PROJECTION``; ``options.dry_run`` builds and
    counts the staging table but skips the swap + meta write (the honest
    first-activation probe — it reports exactly what a real build would
    install without installing it).
    """
    options = options or {}
    if not graph_projection_enabled():
        return _finding(
            {
                "projection_enabled": False,
                "arc_count": 0,
                "source_counts": {},
            },
            changed=False,
        )

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return _finding(
            {"synthetic": True, "arc_count": 0, "source_counts": {}},
            changed=False,
        )

    dry_run = _coerce_bool(options.get("dry_run"), False)
    t0 = time.monotonic()
    source_counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        # Fresh staging table, shaped exactly like the serving one (PK, CHECKs,
        # indexes included — the ON CONFLICT arbiters need the PK).
        await conn.execute("DROP TABLE IF EXISTS public.graph_arcs_new")
        await conn.execute(
            "CREATE TABLE public.graph_arcs_new (LIKE public.graph_arcs INCLUDING ALL)"
        )
        for src_table, sql in _SOURCES:
            try:
                n = await conn.fetchval(
                    f"WITH ins AS ({sql} RETURNING 1) "
                    "SELECT count(*)::bigint FROM ins"
                )
            except Exception as exc:
                # Fail loud per source: a broken source SELECT is a build
                # defect, not a partial-projection excuse — but a substrate
                # table that does not exist YET on a fresh plane (e.g. the
                # P1 event tables while LEGBA_EVENTS has never run) must not
                # kill the whole build. UndefinedTableError (SQLSTATE 42P01 —
                # a missing RELATION only, never a column/syntax error) is
                # therefore the one tolerated failure, recorded honestly as
                # zero so the S-1 missing-legs leg still sees it.
                if getattr(exc, "sqlstate", "") == "42P01":
                    source_counts[src_table] = source_counts.get(src_table, 0)
                    logger.warning(
                        "graph_projector.source_missing table=%s", src_table
                    )
                    continue
                raise
            source_counts[src_table] = source_counts.get(src_table, 0) + int(n)
        arc_count = int(
            await conn.fetchval("SELECT count(*)::bigint FROM public.graph_arcs_new")
        )
        build_seconds = time.monotonic() - t0
        if not dry_run:
            async with conn.transaction():
                for stmt in _SWAP_SQL:
                    await conn.execute(stmt)
                await conn.execute(
                    _META_UPSERT_SQL,
                    arc_count,
                    json.dumps(source_counts, sort_keys=True),
                    float(build_seconds),
                )
            build_seconds = time.monotonic() - t0
    receipt = {
        "projection_enabled": True,
        "dry_run": dry_run,
        "arc_count": arc_count,
        "source_counts": source_counts,
        "build_seconds": round(build_seconds, 3),
        "sources": len(_SOURCES),
    }
    logger.info(
        "graph_projector.build done arcs=%d seconds=%.3f dry_run=%s",
        arc_count, build_seconds, dry_run,
    )
    return _finding(receipt, changed=not dry_run)
