# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The graph projector: whole-rebuild of the cross-layer arc store.

Incremental mirrors are ruled out: each dead mirror in this codebase was an
incremental mirror whose delete path was never written. This handler therefore has NO
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

**P-7 (2026-11-14) — the rebuild made cheap without changing what readers
get.** Measured on live: one rebuild wrote ~7.8 GB of write-ahead log (about
190 GB a day, nearly all the WAL the host writes) and took ~519 s, because the
staging table was created journaled with all five indexes in place and then
filled row by row. Now:

  * the staging table is ``UNLOGGED`` (no WAL for its heap or indexes; this
    host has no streaming replica and no WAL archive, so nothing downstream
    loses it), created with only its primary key (the ``ON CONFLICT``
    arbiter); the secondary indexes readers actually use are built AFTER the
    load (:data:`_POST_LOAD_INDEXES`), then ``ANALYZE`` once, then the same
    transactional swap. The swapped-in table STAYS unlogged: ``SET LOGGED``
    re-journals the whole table (measured), which is the cost being removed.
    After a Postgres crash it comes back empty — readers then answer
    ``projection_rebuilding`` (``graph_projection.projection_state``) and the
    next tick rebuilds (it never skips over an empty table);
  * the arc diet (:data:`LINEAGE_COPIES`, :data:`CONTENTION`): the citation
    lineage copied out of ``derived_from`` / ``citations[]`` /
    ``output_consumption`` / ``situations.derived_from`` and the pairwise
    ``contends_with`` arcs are OFF by default — no graph reader traverses
    them (the two mining analysts read ``plane='world' AND
    src_table='entity_edges'`` only; the ``/v3/graph/arcs`` route has no
    caller). Each group is one descriptor option to turn back on;
  * skip-when-unchanged: a cheap watermark over every table the enabled
    sources read (``pg_stat_user_tables`` write counters + relfilenode, the
    resolver functions' source, this version, the options) is stored on the
    receipt; an identical watermark next tick means no input changed, so the
    tick records ``checked_at`` and skips (readers measure freshness from the
    newer of ``projected_at`` and ``checked_at``).

Descriptor: ``descriptors/analyst_graph_projector.yaml`` (state: draft).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Mapping, NamedTuple, Sequence

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
#: P-7 (2026-11-14): unlogged build, indexes after load, arc diet, skip rule.
GRAPH_PROJECTION_VERSION = "2026-11/p7"

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

#: Source groups. CORE always projects; the other two are the P-7 arc diet,
#: each behind one descriptor option (default off — no graph reader uses
#: them; see the module docstring).
CORE = "core"
#: fact -> fact ``contends_with``: pairwise arcs between contested value
#: clusters — 1.81M of 11.0M arcs on 2026-11-14, read by no graph reader (the
#: contention itself is served by ``fact_contention``/``_values`` by key).
CONTENTION = "contention"
#: The citation lineage copied out of reference arrays: finding ``cites``
#: (``analyst_outputs.derived_from``, ``data.citations[]``,
#: ``output_consumption``) and situation ``member_of``
#: (``situations.derived_from``) — 5.54M arcs on 2026-11-14, the
#: authoritative copy stays on the source rows (GIN-indexed arrays).
LINEAGE_COPIES = "lineage_copies"

#: Descriptor option per optional group (bool, default False).
GROUP_OPTIONS: Mapping[str, str] = {
    CONTENTION: "include_contention_arcs",
    LINEAGE_COPIES: "include_lineage_copies",
}


class _Source(NamedTuple):
    """One ``INSERT ... SELECT``: the ``src_table`` it is keyed by, its group,
    the OTHER tables it reads (for the skip watermark), and the statement."""

    src_table: str
    group: str
    reads: tuple[str, ...]
    sql: str


# ---------------------------------------------------------------------------
# The source table — one INSERT ... SELECT per source, keyed by src_table so
# `source_counts` is directly the S-1 parity surface. Each statement is a pure
# projection: no now(), no randomness, no ordering-dependent state.
# ---------------------------------------------------------------------------

_SOURCES: Sequence[_Source] = (
    # -- EVIDENCE plane ------------------------------------------------------
    # signal -> entity `mentions`. A signal can link the same entity under
    # several roles; the arc key is role-blind, so keep the most confident one.
    _Source("signal_entity_links", CORE, (), f"""
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
    _Source("signal_aliases", CORE, (), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'signal', sa.alias_signal_id, 'signal', sa.canonical_signal_id,
       'duplicate_of', 'evidence', sa.reason, 0, sa.score, sa.produced_at,
       NULL, 'signal_aliases', NULL
  FROM signal_aliases sa
 WHERE sa.canonical_signal_id <> sa.alias_signal_id
ON CONFLICT DO NOTHING
"""),
    # signal -> signal `duplicate_of` (canonical_signal_id redirects).
    _Source("signals", CORE, (), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'signal', s.id, 'signal', s.canonical_signal_id, 'duplicate_of',
       'evidence', 'canonical', 0, NULL, s.created_at, NULL, 'signals', s.id
  FROM signals s
 WHERE s.canonical_signal_id IS NOT NULL AND s.canonical_signal_id <> s.id
ON CONFLICT DO NOTHING
"""),
    # -- WORLD plane ----------------------------------------------------------
    # signal -> event `evidences`; family carries the link's source_class.
    _Source("signal_event_links", CORE, (), f"""
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
    _Source("entity_edges", CORE, (), f"""
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
    _Source("entity_edges", CORE, (), f"""
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
    _Source("event_entity_links", CORE, (), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'entity', l.entity_id, 'event', l.event_id, 'involved_in', 'world',
       l.role, 0, l.confidence, l.created_at, NULL, 'event_entity_links', NULL
  FROM event_entity_links l
ON CONFLICT DO NOTHING
"""),
    # event -> event typed edges (part_of / caused_by / evolves_from /
    # correlated_with / contradicts).
    _Source("event_edges", CORE, (), f"""
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
    _Source("situation_event_links", CORE, (), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'event', l.event_id, 'situation', l.situation_id, 'tracked_by',
       'world', 'membership', 0, l.relevance, l.created_at, NULL,
       'situation_event_links', NULL
  FROM situation_event_links l
ON CONFLICT DO NOTHING
"""),
    # fact -> entity `subject_of` / `value_of` — resolve_entity_name() is the
    # 0143 name->terminal-survivor resolver; unresolved names emit no arc.
    _Source("facts", CORE, ("entity_profiles",), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'fact', f.id, 'entity', r.entity_id, 'subject_of', 'world',
       'subject', 0, f.confidence,
       COALESCE(f.valid_from, f.produced_at), NULL, 'facts', f.id
  FROM facts f
  CROSS JOIN LATERAL (SELECT resolve_entity_name(f.subject) AS entity_id) r
 WHERE r.entity_id IS NOT NULL
ON CONFLICT DO NOTHING
"""),
    _Source("facts", CORE, ("entity_profiles",), f"""
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
    _Source("fact_contention_values", CONTENTION, ("fact_contention",), f"""
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
    _Source("narrative_echo_edges", CORE, (), f"""
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
    _Source("situations", LINEAGE_COPIES, (), f"""
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
    _Source("situation_events", CORE, (), f"""
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
    _Source("analyst_outputs", LINEAGE_COPIES, ("signals", "events"), f"""
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
    _Source("analyst_outputs", LINEAGE_COPIES, (), f"""
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
    _Source("output_consumption", LINEAGE_COPIES, (), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT 'finding', oc.consumer_id, 'finding', oc.consumed_id, 'cites',
       'lineage', oc.context, 0, NULL, oc.consumed_at, NULL,
       'output_consumption', NULL
  FROM output_consumption oc
ON CONFLICT DO NOTHING
"""),
    # finding -> finding `supersedes` — the supersession chain.
    _Source("analyst_outputs", CORE, (), f"""
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
    _Source("analyst_outputs", CORE, (), f"""
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
    _Source("bearing_edges", CORE, (), f"""
INSERT INTO public.graph_arcs_new ({_ARC_COLS})
SELECT b.src_kind, b.src_id, b.dst_kind, b.dst_id, 'bears_on', 'lineage',
       b.edge_kind, 0, b.weight, b.src_as_of, NULL, 'bearing_edges', b.id
  FROM bearing_edges b
ON CONFLICT DO NOTHING
"""),
    # -- EVIDENCE plane (entity merge tombstones) -----------------------------
    _Source("entity_profiles", CORE, (), f"""
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
#: auto-named indexes carry ``graph_arcs_new_*`` names after the rename;
#: cosmetic only.
_SWAP_SQL = (
    "ALTER TABLE public.graph_arcs RENAME TO graph_arcs_old",
    "ALTER TABLE public.graph_arcs_new RENAME TO graph_arcs",
    "DROP TABLE public.graph_arcs_old",
)

#: P-7: the staging table — columns, NOT NULLs, defaults and CHECKs copied
#: from the serving table, but NO indexes (``INCLUDING ALL`` used to copy all
#: five, so 11M rows were inserted into five btrees row by row, journaled).
#: ``{persistence}`` is ``UNLOGGED `` by default.
_CREATE_STAGING_SQL = (
    "CREATE {persistence}TABLE public.graph_arcs_new "
    "(LIKE public.graph_arcs INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
)
#: The one index present DURING the load: the arc key, which is also the
#: ``ON CONFLICT DO NOTHING`` arbiter every source relies on (first source
#: wins a duplicate key — the order of :data:`_SOURCES` is the contract).
_STAGING_PK_SQL = (
    "ALTER TABLE public.graph_arcs_new "
    "ADD PRIMARY KEY (from_kind, from_id, to_kind, to_id, arc_type)"
)

#: P-7: the secondary indexes built AFTER the load — only those a reader
#: uses (plan evidence in the P-7 lane report):
#:
#:   * ``src_table`` — ``graph_mining`` (both reads) and ``structural_balance``
#:     select ``src_table = 'entity_edges' AND plane = 'world'``; a bitmap on
#:     this index is their access path (and the S-1 audit read's);
#:   * ``(to_kind, to_id, plane) WHERE valid_until IS NULL`` — the
#:     ``/v3/graph/arcs`` route's ``in``/``both`` direction (the default);
#:     without it an anchor's incoming arcs are a whole-table scan.
#:
#: DROPPED (0207 created them): ``(from_kind, from_id, plane) WHERE open`` —
#: the primary key's leading ``(from_kind, from_id)`` serves the same ``out``
#: lookup; ``(from_id, to_id) WHERE world AND open`` — no query joins on it;
#: the planner only ANDed it into the mining bitmap, which made the mining
#: read slower (it walked 2.0M world entries to keep 79k rows).
_POST_LOAD_INDEXES: Sequence[str] = (
    "CREATE INDEX ON public.graph_arcs_new (src_table)",
    "CREATE INDEX ON public.graph_arcs_new (to_kind, to_id, plane) "
    "WHERE valid_until IS NULL",
)

_META_UPSERT_SQL = """
INSERT INTO public.graph_arcs_meta
    (id, projected_at, arc_count, source_counts, build_seconds,
     checked_at, input_watermark, last_outcome, excluded_sources,
     persistence)
VALUES (true, now(), $1, $2::jsonb, $3, now(), $4::jsonb, 'built',
        $5::jsonb, $6)
ON CONFLICT (id) DO UPDATE SET
    projected_at     = EXCLUDED.projected_at,
    arc_count        = EXCLUDED.arc_count,
    source_counts    = EXCLUDED.source_counts,
    build_seconds    = EXCLUDED.build_seconds,
    checked_at       = EXCLUDED.checked_at,
    input_watermark  = EXCLUDED.input_watermark,
    last_outcome     = EXCLUDED.last_outcome,
    excluded_sources = EXCLUDED.excluded_sources,
    persistence      = EXCLUDED.persistence
"""

#: The skip receipt: the projection is proven current as of now, without a
#: rebuild. ``projected_at`` / ``build_seconds`` / counts are left alone (they
#: describe the build that is still serving).
_META_SKIP_SQL = """
UPDATE public.graph_arcs_meta
   SET checked_at = now(), last_outcome = 'skipped_unchanged'
 WHERE id
"""

_META_READ_SQL = "SELECT m.* FROM public.graph_arcs_meta m WHERE m.id"

#: The serving table's state for the skip guard: persistence + any row.
_SERVING_STATE_SQL = """
SELECT c.relpersistence::text AS relpersistence,
       EXISTS (SELECT 1 FROM public.graph_arcs) AS has_arcs
  FROM pg_class c
 WHERE c.oid = 'public.graph_arcs'::regclass
"""

#: The input watermark: every write counter Postgres keeps for the tables the
#: enabled sources read, plus the relfilenode (TRUNCATE / a rewrite change it
#: without counting rows). The counters are cumulative and only grow until a
#: stats reset — which changes them too — so "equal" can only mean nothing
#: was inserted, updated or deleted. Read BEFORE the build's first statement:
#: a write committed after this read moves the counters by the next tick, so
#: the error direction is an extra rebuild, never a missed one.
_WATERMARK_TABLES_SQL = """
SELECT c.relname,
       pg_relation_filenode(c.oid)::text AS filenode,
       COALESCE(s.n_tup_ins, 0) AS ins,
       COALESCE(s.n_tup_upd, 0) AS upd,
       COALESCE(s.n_tup_del, 0) AS del
  FROM pg_class c
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
 WHERE c.relnamespace = 'public'::regnamespace
   AND c.relkind IN ('r', 'p')
   AND c.relname = ANY($1::text[])
 ORDER BY c.relname
"""

#: The resolver functions the ``facts`` sources call — a redefinition is an
#: input change the row counters cannot see.
_WATERMARK_FUNCS_SQL = """
SELECT COALESCE(md5(string_agg(p.oid::regprocedure::text || ':' ||
                               md5(p.prosrc), ',' ORDER BY p.oid)), '')
  FROM pg_proc p
 WHERE p.pronamespace = 'public'::regnamespace
   AND p.proname IN ('resolve_entity_name', 'resolve_entity')
"""

#: Ceiling on skipping: past this build age the tick rebuilds even over an
#: unchanged watermark, so a blind spot in the watermark is bounded.
DEFAULT_MAX_SKIP_SECONDS = 86_400.0


def _finding(data: Mapping[str, Any], *, changed: bool) -> AnalystMethodResult:
    """The projector's per-run receipt (never lands on a trust surface)."""
    if changed:
        title = "rebuilt"
    elif data.get("skipped"):
        title = "skipped: inputs unchanged"
    else:
        title = "no-op"
    return AnalystMethodResult(
        finding=FindingPayload(
            title=f"Graph projector ({title})",
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


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def enabled_groups(options: Mapping[str, Any]) -> frozenset[str]:
    """CORE plus every optional group whose option is on."""
    # Literal reads (one per GROUP_OPTIONS entry), so the option catalog's
    # reachability sweep sees every declared knob read by name.
    on = {
        CONTENTION: _coerce_bool(options.get("include_contention_arcs"), False),
        LINEAGE_COPIES: _coerce_bool(options.get("include_lineage_copies"), False),
    }
    assert set(on) == set(GROUP_OPTIONS), "a GROUP_OPTIONS entry has no literal read"
    return frozenset({CORE} | {group for group, flag in on.items() if flag})


def selected_sources(groups: frozenset[str]) -> list[_Source]:
    """The :data:`_SOURCES` entries in ``groups``, order preserved (the
    ``ON CONFLICT`` first-wins order is part of the contract)."""
    return [src for src in _SOURCES if src.group in groups]


def excluded_source_tables(groups: frozenset[str]) -> list[str]:
    """``src_table`` values with NO enabled statement — the S-1 gauge must not
    call these a missing leg (they are off by option, not broken)."""
    kept = {src.src_table for src in selected_sources(groups)}
    return sorted({src.src_table for src in _SOURCES} - kept)


def watermark_tables(sources: Sequence[_Source]) -> list[str]:
    """Every table the given sources read."""
    tables: set[str] = set()
    for src in sources:
        tables.add(src.src_table)
        tables.update(src.reads)
    return sorted(tables)


async def _input_watermark(
    conn: Any, sources: Sequence[_Source], *, persistence: str,
    groups: frozenset[str],
) -> dict[str, Any]:
    """The cheap change detector (see :data:`_WATERMARK_TABLES_SQL`)."""
    rows = await conn.fetch(_WATERMARK_TABLES_SQL, watermark_tables(sources))
    tables = {
        str(r["relname"]): [
            str(r["filenode"]), int(r["ins"]), int(r["upd"]), int(r["del"]),
        ]
        for r in rows
    }
    funcs = str(await conn.fetchval(_WATERMARK_FUNCS_SQL) or "")
    return {
        "version": GRAPH_PROJECTION_VERSION,
        "groups": sorted(groups),
        "persistence": persistence,
        "functions": funcs,
        "tables": tables,
    }


def _as_mapping(value: Any) -> Any:
    """asyncpg returns jsonb as text; normalise to Python."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _watermark_digest(watermark: Mapping[str, Any] | None) -> str | None:
    if not watermark:
        return None
    return hashlib.sha256(
        json.dumps(watermark, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


async def _skip_decision(
    conn: Any, watermark: Mapping[str, Any], *, persistence: str,
    max_skip_seconds: float,
) -> tuple[bool, str]:
    """``(skip, reason)``. Skips ONLY when the serving table exists and holds
    arcs, has the requested persistence, the last receipt was a real build
    with an identical watermark, and that build is younger than the ceiling.
    Every other case rebuilds and names why."""
    try:
        meta_row = await conn.fetchrow(_META_READ_SQL)
        serving = await conn.fetchrow(_SERVING_STATE_SQL)
    except Exception as exc:  # table/column absent: a build repairs it
        return False, f"state_unreadable:{type(exc).__name__}"
    if meta_row is None:
        return False, "no_receipt"
    if serving is None or not serving["has_arcs"]:
        return False, "serving_table_empty"
    want = "u" if persistence == "unlogged" else "p"
    if str(serving["relpersistence"]) != want:
        return False, "persistence_changed"
    meta = dict(meta_row)
    prior = _as_mapping(meta.get("input_watermark"))
    if not prior:
        return False, "no_prior_watermark"
    if prior != json.loads(json.dumps(watermark, sort_keys=True)):
        return False, "inputs_changed"
    projected_at = meta.get("projected_at")
    if projected_at is not None and max_skip_seconds > 0:
        if time.time() - projected_at.timestamp() > max_skip_seconds:
            return False, "max_skip_age"
    return True, "inputs_unchanged"


async def handle(
    inputs: Sequence[Mapping[str, Any]] | None = None,
    options: Mapping[str, Any] | None = None,
    deps: Any = None,
    **_kwargs: Any,
) -> AnalystMethodResult:
    """Whole-rebuild ``graph_arcs`` from every enabled source, or skip when
    no input changed.

    Flag-gated on ``LEGBA_GRAPH_PROJECTION``; ``options.dry_run`` builds and
    counts the staging table but skips the swap + meta write (the honest
    first-activation probe — it reports exactly what a real build would
    install without installing it). P-7 options (all optional):
    ``include_contention_arcs`` / ``include_lineage_copies`` (default off —
    the arc diet), ``unlogged`` (default on), ``skip_when_unchanged``
    (default on), ``max_skip_seconds`` (default 86400).
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
    persistence = (
        "unlogged" if _coerce_bool(options.get("unlogged"), True) else "logged"
    )
    skip_enabled = _coerce_bool(options.get("skip_when_unchanged"), True)
    max_skip_seconds = _coerce_float(
        options.get("max_skip_seconds"), DEFAULT_MAX_SKIP_SECONDS
    )
    groups = enabled_groups(options)
    sources = selected_sources(groups)
    excluded = excluded_source_tables(groups)
    t0 = time.monotonic()
    source_counts: dict[str, int] = {}
    timings: dict[str, float] = {}
    async with pool.acquire() as conn:
        # The watermark is read BEFORE anything else (see its docstring).
        watermark = await _input_watermark(
            conn, sources, persistence=persistence, groups=groups
        )
        reason = "dry_run" if dry_run else "skip_disabled"
        if skip_enabled and not dry_run:
            skip, reason = await _skip_decision(
                conn, watermark, persistence=persistence,
                max_skip_seconds=max_skip_seconds,
            )
            if skip:
                await conn.execute(_META_SKIP_SQL)
                logger.info(
                    "graph_projector.skipped reason=%s watermark=%s",
                    reason, _watermark_digest(watermark),
                )
                return _finding(
                    {
                        "projection_enabled": True,
                        "skipped": True,
                        "reason": reason,
                        "watermark": _watermark_digest(watermark),
                        "build_seconds": 0.0,
                    },
                    changed=False,
                )
        if reason == "serving_table_empty":
            # The crash-recovery rebuild — reported ONCE, here, as what it is.
            logger.warning(
                "graph_projector.rebuilding_after_empty_serving_table — the "
                "unlogged projection was emptied (crash recovery); readers "
                "answer projection_rebuilding until this build swaps in"
            )
        # Fresh staging table: only the arc key during the load.
        await conn.execute("DROP TABLE IF EXISTS public.graph_arcs_new")
        await conn.execute(
            _CREATE_STAGING_SQL.format(
                persistence="UNLOGGED " if persistence == "unlogged" else ""
            )
        )
        await conn.execute(_STAGING_PK_SQL)
        for src in sources:
            t_src = time.monotonic()
            try:
                n = await conn.fetchval(
                    f"WITH ins AS ({src.sql} RETURNING 1) "
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
                    source_counts[src.src_table] = source_counts.get(
                        src.src_table, 0
                    )
                    logger.warning(
                        "graph_projector.source_missing table=%s", src.src_table
                    )
                    continue
                raise
            source_counts[src.src_table] = (
                source_counts.get(src.src_table, 0) + int(n)
            )
            timings[src.src_table] = round(
                timings.get(src.src_table, 0.0) + time.monotonic() - t_src, 3
            )
        load_seconds = time.monotonic() - t0
        arc_count = int(
            await conn.fetchval("SELECT count(*)::bigint FROM public.graph_arcs_new")
        )
        t_idx = time.monotonic()
        for stmt in _POST_LOAD_INDEXES:
            await conn.execute(stmt)
        await conn.execute("ANALYZE public.graph_arcs_new")
        index_seconds = time.monotonic() - t_idx
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
                    json.dumps(watermark, sort_keys=True),
                    json.dumps(excluded),
                    persistence,
                )
            build_seconds = time.monotonic() - t0
    receipt = {
        "projection_enabled": True,
        "dry_run": dry_run,
        "skipped": False,
        "rebuild_reason": reason,
        "arc_count": arc_count,
        "source_counts": source_counts,
        "excluded_sources": excluded,
        "groups": sorted(groups),
        "persistence": persistence,
        "build_seconds": round(build_seconds, 3),
        "load_seconds": round(load_seconds, 3),
        "index_seconds": round(index_seconds, 3),
        "source_seconds": timings,
        "watermark": _watermark_digest(watermark),
        "sources": len(sources),
    }
    logger.info(
        "graph_projector.build done arcs=%d seconds=%.3f load=%.3f index=%.3f "
        "persistence=%s dry_run=%s reason=%s",
        arc_count, build_seconds, load_seconds, index_seconds, persistence,
        dry_run, reason,
    )
    return _finding(receipt, changed=not dry_run)
