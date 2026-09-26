<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Data model v3 — events, the temporal surface, and one typed graph

**Status:** built and running. The `events` row and its append-only transition ledger, the
temporal readers, the event ref kind and the whole-rebuild `graph_arcs` projection are live,
each behind a writer flag that defaults off in code. Sections that read as proposals — the
open questions at the end, and the measurements that decided them — are kept because they
are the reasoning the shipped design rests on, each stamped with the day it was measured.
**Companion:** [`V3_IMPLEMENTATION_PLAN.md`](V3_IMPLEMENTATION_PLAN.md) — the phases,
the house rules for the agents who build them, and the live acceptance proof per phase.

Read [`DATA_MODEL.md`](DATA_MODEL.md) first. This document is an *addition* to it, not a
replacement: every tier, write semantic and provenance rule in that file still holds under
v3.

**Contents:**
[§0 Summary](#0--the-one-page) ·
[§1 The record](#1--the-record) ·
[§2 Events](#2--events) ·
[§3 The temporal surface](#3--the-temporal-surface) ·
[§4 The unified graph](#4--the-unified-graph) ·
[§5 Typing throughput](#5--typing-throughput) ·
[§6 Interfaces](#6--interfaces) ·
[§7 Migration and compatibility](#7--migration--compatibility) ·
[§8 Open questions](#8--open-questions-for-the-operator)

---

## 0 · The one page

Three versions of this data model have existed.

* **v1** — the March 2026 cognitive architecture: an agent with a cycle loop, a `derived_events`
  table, an event lifecycle state machine, reified nexuses, temporal facts, and Apache AGE as
  the graph of record. (`archive/COGNITIVE_ARCHITECTURE.md`,
  `archive/DATA_LIFECYCLE.md`)
* **v2** — the June pivot's source-first substrate: signals, facts, entities, nexuses,
  analyst outputs, situations; every factual sentence drilling to a signal; a mandatory
  faithfulness verify; AGE dormant. Events were dropped. ([`DATA_MODEL.md`](DATA_MODEL.md))
* **v3** — this document. It adds back the three things v1 had and the pivot dropped,
  rebuilt on v2's provenance discipline rather than beside it.

### What v3 adds

**A · Events, derived from the tower.** A first-class `events` row — what happened, when,
where, who with what role, category, severity, lifecycle, confidence — with many-to-many
links to the signals that evidence it, typed event↔event edges, and a link to the situations
that track it. Candidates come from two places: clustering over enriched signals, and the
tower itself (a desk finding's cited signal set; a `situation_events` ledger row's evidence).
The new part is provenance: an event cites signals, a finding may cite an event, and the
judge grounding that citation resolves it **to the event's signals' source text, never to the
event's own summary**.

**B · The temporal surface as readers, not stores.** `facts`, `nexuses`, `entity_edges` and
`situations` already carry `valid_from` / `valid_until` / `superseded_by`; `entity_edges` also
carries `observed_count` / `first_seen_at` / `last_seen_at`; `analyst_outputs` versions by
`superseded_by` / `superseded_at`. What does not exist is any way to *ask* them a dated
question: there is no `as_of` parameter anywhere on the substrate-read surface, and
`get_timeline` — the one tool whose name promises a window — has no time parameter at all
(verified 2026-09-21, `src/legba/runtime/substrate_query_port.py:1828`). v3 adds the readers
and one genuinely missing store: an append-only edge transition ledger, because `entity_edges`
supersession overwrites in place and ~8.9 k re-observations have already been folded away
irrecoverably (§3.4).

**C · One typed graph over every layer, as a projection.** One edge vocabulary spanning
signal, entity, fact, nexus, event, situation and finding — the arcs that already exist as
foreign keys, `derived_from uuid[]` arrays and `bearing_edges`, named once. The relational
tables stay authoritative; the graph is a whole-rebuild projection with a kill switch, never
an incremental mirror. The engine question stays deferred to the pre-registered 2026-11-03
sitting on triggers E1–E5.

### What v3 is NOT

It changes **nothing** about:

* **the tower** — nine bounded units → `country_composition` → `region_rollup` →
  `world_assessor`, plus the thematic compositions. No new altitude, no new gate.
* **the judge** — the mandatory faithfulness verify, `effective_confidence =
  min(confidence, faithfulness_score)`, the hard/soft `fail_class` split, the
  `judge_pipeline_version` stamp. An event citation is a new *ref kind* resolved into the
  existing envelope; the rubric, the scoring and the floor are untouched.
* **the correctness grader and the external audit** — they drill through the same citation
  chain to the same `source_text` and the same archived bytes. An event is a waypoint on that
  walk, never a terminus.
* **the assembly regime** — desks stay single-round and stateless; compositions still admit
  only verify-passed sub-claims; the composition gate stays where the operator left it.
* **the provenance chain** — `derived_from uuid[]`, `analyst_traces`, `output_consumption`,
  the lineage catalog. Events join it; nothing leaves it.
* **`signals`, `facts`, `entity_profiles`, `nexuses`, `analyst_outputs`** — no column is
  dropped, no write path is re-pointed, no existing row changes meaning. Every v3 table is
  additive and every v3 writer is flag-gated off on arrival.

### The three claims this document makes that the brief did not

1. **The AGE probe has already been run and it failed.** [`AGE_PROBE_REPORT.md`](history/AGE_PROBE_REPORT.md)
   §5.1–§5.4 (2026-08-03) measured exactly the shapes a cross-layer projection needs and found
   AGE could not serve them at any of six configurations — and that per-hop temporal predicates
   are *inexpressible* in AGE 1.7, which a temporal projection requires by construction. §4.3
   of this document therefore does not propose a 90-day AGE probe. See §4.5.
2. **Kùzu is not available as a projection target.** Upstream was archived 2025-10-10 and the
   project is read-only (§4.6). MIT licence, so forks exist; none is a dependency a
   one-operator stack should take.
3. **The typing-throughput experiment has already been applied, and the instrument has
   already diagnosed the shortfall.** The reifier descriptor is live at `max_candidates: 600`
   × 2 runs/day, `batch_size: 12`, `qualification_bar: 0.42` — the bake-off's own
   recommendations — and realised production is ~68 typed edges/day against the projected
   ~205. The funnel the handler already writes to its receipt says why: the live **accept rate
   is 7-11.5 %, not the 46.8 % the projection assumed**, the run never fills its 600-candidate
   budget (eligible 437-481), and the candidate scan's own "LIMIT was binding" counter has
   fired on every run for a fortnight unread. So P5 is one decisive comparison, not a
   measurement phase, and "raise the cap" is not the lever. See §5.

---

## 1 · The record

This has been designed before. The purpose of this section is that it is not designed a
fourth time without reading what the first three said.

### 1.1 What March designed (v1)

| Piece | Where | What it said |
|---|---|---|
| `derived_events` + `signal_event_links` | `git show 6efd7219:src/legba/shared/schemas/derived_events.py` | An event is "a real-world occurrence derived from one or more signals… the primary analytical unit". `DerivedEvent` carried `title`, `summary`, `category`, `event_type ∈ {incident, development, shift, threshold}`, `severity ∈ {critical, high, medium, low, routine}`, `time_start`/`time_end`, `locations`/`geo_countries`/`geo_coordinates`, `actors`, `tags`, `confidence`, `signal_count`, `source_method ∈ {auto, agent, manual}`. `SignalEventLink` carried `relevance`. |
| The clusterer | `archive/DATA_LIFECYCLE.md` §2 | Single-linkage over unclustered signals. Similarity = entity overlap 30 % + title Jaccard 30 % + temporal proximity 20 % + category match 20 %; threshold 0.5; cluster size cap 30; structured-source singletons auto-promoted. 94.6 % of events were clusterer-made, 5.4 % agent-made. |
| The lifecycle FSM | `archive/COGNITIVE_ARCHITECTURE.md` Phase 3 (DEPLOYED in v1) | Six states, eight deterministic transitions: `EMERGING → DEVELOPING` at signal_count ≥ 3; `DEVELOPING → ACTIVE` at ≥ 5 **and** confidence ≥ 0.6; `ACTIVE → EVOLVING` on a > 2× velocity change **or** a new actor/location; `EVOLVING → ACTIVE` on stabilisation; `→ RESOLVED` on silence (48 h / 72 h / 7 d by state); **`RESOLVED → REACTIVATED` when a new signal links after resolution**, then `REACTIVATED → DEVELOPING`. |
| Events as graph nodes (DECIDED) | same, §"Events as Graph Nodes" | "Postgres stays the authoritative store for event attributes… AGE holds the relationships." Seven edge types: `INVOLVED_IN` (Entity→Event, role ∈ actor/location/observer/victim), `PART_OF`, `CAUSED_BY`, `EVOLVES_FROM`, `CORRELATED_WITH`, `CONTRADICTS` (all Event→Event), `TRACKED_BY` (Event→Situation). Hierarchy unbounded. `EVENT_EDGE_TYPES` / `EVENT_EDGE_SCHEMA` were written: `git show 6efd7219:src/legba/shared/graph_events.py`. |
| Temporal facts | `git show 6efd7219:docs/NEXUS_AND_TEMPORAL.md` | `valid_from` / `valid_until` / `superseded_by` / `confidence` / `evidence_set`; single-value and volatile predicate auto-supersede; decay. Every context-injection read filtered `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > NOW())`. |
| The as-of query | `archive/TEMPORAL_FACTS_PLAN.md` | Written out as SQL and never built: `WHERE valid_from <= D AND (valid_until IS NULL OR valid_until > D)`. |
| Event-sourced edges | `archive/TEMPORAL_GRAPH_RESEARCH.md` §1 | "**Event-sourced (recommended)** — store every relationship *change* as an immutable event… enables full temporal reconstruction… supports **bitemporal** tracking: *valid time* vs *transaction time*." Its named sink was TimescaleDB. |
| What v1 measured about its own events | `archive/COGNITIVE_ARCHITECTURE.md` defect B1 | "**Mega-bucket events** — 147/528 events at signal_count=30 cap. Dua Lipa bikini, basketball scores, and Iran strikes in same event. CRITICAL." Plus 29 orphan events with zero signals, agent-created without backing. |

### 1.2 What the pivot kept and dropped (v2, June)

The pivot proposal's own list of what it replaced does not name events. They
are absent from it because they were not *replaced* — they were simply not carried over. The
pivot kept the nine analyst kinds, the output kinds, the registry, the eval loop and the receipt
chain, and re-cut `signals` to be target-agnostic and modality-first.

What the pivot dropped, and what stood in for it:

* **`events` — dropped outright.** [`DATA_MODEL.md`](DATA_MODEL.md) says it three times in
  terms: *"the **events substitute** (no `events` table; events = signals + `get_timeline`,
  which includes situation spans); situations = the frames"*. `situations` got
  `situation_signature`, the `UNIQUE (situation_signature, analyst_id)` upsert key and the
  temporal frame (migrations 0040–0042) and became the stand-in.
* **AGE — kept installed, made dormant.** [`ARCHITECTURE.md`](ARCHITECTURE.md), *"AGE
  re-evaluation — DECISION (2026-06-23)"*: the knowledge graph lives relationally in
  `nexuses`, all graph computation runs in networkx in-process, **both AGE write-legs ship
  off by default** (`emit_graph_edges=False`; `LEGBA_AGE_DERIVED_FROM` off), and *"no active
  analysis path depends on AGE"*.
* **TimescaleDB — removed from the codebase.** [`DATA_MODEL.md`](DATA_MODEL.md): the
  time-series metrics store was provisioned-but-idle with zero callers and was deleted;
  time-series metrics are a declared seam, and `anomaly_detection` reads `time_bucket()` from
  the primary Postgres pool. So the March event-sourcing recommendation lost its named sink.
* **The as-of query — never built, in either version.**

What the pivot *added* that v1 did not have, and that v3 is built on: the mandatory
faithfulness verify with a persisted `analyst_critiques` verdict; `derived_from uuid[]` on
every substrate table; `output_consumption` (0106) as the inverse index; hash-chained
`analyst_traces`; `evidence_archive` (0104) holding the original bytes of cited signals,
content-addressed; the external audit's verbatim-span contract
(`src/legba/data/provenance/external_span_check.py`).

### 1.3 What August decided, and what was built (the graph debate)

A judged synthesis over three advocates (A = commit to AGE, B = relational-native, C = dedicated
engine) returned the weighted verdict **B 83.0, A 65.5, C 62.5**.

* **§1.3 — the finding that governs everything.** *"The binding constraint on 'graph the world
  state, fully queryable' is LLM typing throughput, not storage, not key type, and not the
  traversal engine."* Measured then: ≈12.4 typed edges/day against ≈9,941 candidate
  arrivals/day — a drain ratio of 0.125 %.
* **§4.1 — Decisions 1–7, all built.** `entity_edges` is **entity↔entity only, not
  polymorphic** (Decision 1 — a polymorphic endpoint cannot carry a foreign key, and
  `bearing_edges`, the house's own polymorphic precedent, has no FK constraints at all);
  CASCADE on endpoints, SET NULL on intermediary, with a counted receipt (2); **four**
  `edge_family` tiers `relation` / `reference` / `cooccurrence` / `structural` (3 — because
  86 % of the signed graph was imported Wikidata IGO membership and `structural_balance` was
  measuring the UN); candidates stay out, only `promoted` crosses (4); dual-write in **one
  transaction**, hard-fail (5); merge-fold inside `merge_pair`'s transaction (6); migration
  **0143** (7). Shipped as `0143_entity_edges.sql` (+ `entity_edges_unresolved`,
  `resolve_entity_name()`, `fold_entity_edges()`), `0144` (backfill from `nexuses`), `0145`
  (from promoted `proposed_edges`), `0180` (from relational `facts`).
* **§4.2 — the engine is DEFERRED, once, with pre-registered triggers.** An engine is
  warranted when **any two** of E1–E5 fire; the sitting is **2026-11-03**. E1 open
  `edge_family='relation'` edges > 250,000. E2 p95 latency of the shipped graph tools > 2,000 ms
  over ≥100 real invocations in 30 days. E3 sustained demand for shapes the typed SQL builder
  cannot express (≥4 hops, or per-hop type + temporal predicates) at ≥20 invocations/day for
  14 days AND ≥20 % such shapes. E4 full in-process snapshot rebuild > 60 s. E5 attribute-rich
  centrality/community required interactive (<2 s) rather than on cadence. **E2 and E3 both
  read "instrument first" — the gauges do not exist.**
* **§4.3 — the AGE interim: freeze.** Pin the digest; delete the 27 smoke fixtures; make the
  read path distinguish `no_path` / `engine_unreachable` / `empty_graph`; gate
  `_augment_from_age` off explicitly; do **not** fix the `.id`/`name` mismatch (it is dormant
  while the graph is empty, and fixing it invites populating it).
* **The probe that was not supposed to happen yet.** §4.2 routed E3/E5 to "AGE probe first,
  not Neo4j". [`AGE_PROBE_REPORT.md`](history/AGE_PROBE_REPORT.md) ran it early, on 2026-08-03, and
  §5.4 records the result: *"That probe has now been run, early, and it came back negative…
  if E3 or E5 fires later, the ladder should not route to an AGE probe; it has already been
  taken, and the answer was no."* Details in §4.5 below.
* **[`TYPING_BAKEOFF_2026-08-03.md`](history/TYPING_BAKEOFF_2026-08-03.md) §1** found the diagnosis
  attached to §1.3 was wrong: the reifier's candidate window had no `status` filter and a
  keeper-blind dedup guard, so **every one of its 80 LLM calls a day was spent on rows
  structurally incapable of producing an edge**. Its recommendations (queue fix, batch N=12,
  qualification bar 0.42 with ≥2 independent sources) are **all applied** — see §5.

### 1.4 What exists today — measured

Postgres counts and the AGE inspection below were measured read-only against
`legba-postgres-1` at **2026-09-21 04:13 UTC**; the Qdrant and OpenSearch counts at
**2026-09-21 04:20 UTC**. Each is reproducible from the column names given.

| Store | Reading |
|---|---|
| Postgres schema `public` | **86 base tables + 2 views** (`entity_profiles_resolved`, `source_quality`). Migration head **0201**; 119 files applied, zero drift; 0198/0199 do not exist. |
| `signals` | 280,490 |
| `signal_entity_links` | 848,436 (PK `(signal_id, entity_id, role)`; FK `entity_id → entity_profiles ON DELETE CASCADE`) |
| `entity_profiles` | 143,292 |
| `facts` | 148,605 — **open 131,516**, expired (`valid_until` set) 8,962, superseded 8,127; **64 distinct predicates** |
| `nexuses` | 35,843 |
| `entity_edges` | 45,613 — open 45,582; by family **cooccurrence 25,147 · relation 16,860 · reference 3,606**. 55.1 % of the whole store is the single untyped predicate `co occurs with`. |
| `proposed_edges` | 802,702 — pending 320,161 · retired 196,160 · rejected 173,594 · orphaned 80,858 · promoted 27,996 · merged 3,933 |
| `bearing_edges` | 22,969 (`src_kind`/`src_id`/`dst_kind`/`dst_id`, no FKs — the house's one polymorphic edge table) |
| `analyst_outputs` | 107,155 |
| `situations` | 295 — **active 11 · dormant 240 · closed 44** |
| `situation_events` | 3,214 (append-only, DELETE and UPDATE barred by trigger) |
| `hypotheses` | 7,811 |
| `evidence_archive` | 98,813 |
| `journal_entries` | 302 |
| Qdrant `legba_signals` | 195,391 points, 1024-d cosine (193,872 indexed) |
| OpenSearch `legba_signals_corpus` | 257,881 docs |
| AGE `legba_graph` | **ZERO vertices, ZERO edges** — confirmed by `count(*)` on `legba_graph."_ag_label_vertex"` and `"_ag_label_edge"` (the inheritance parents, so the count covers every label child). The live catalog carries **11 named vertex labels and 21 named edge labels** (+2 AGE base labels = 34), of which the code seeds 9 + 14 from `src/legba/data/vocabulary.py` via `ensure_seed_vocabulary()` plus `Output` / `DerivedFrom` from migration 0037; the remainder are residue from smoke tests. Among the seeded vertex labels: **`Event`**. Among the edge labels: **`InvolvedIn`, `PartOf`, `PartyTo`, `Targets`, `ConductedVia`** — v1's event vocabulary, still declared, never used. |
| Typed-edge production, 7 days to 2026-09-21 04:13Z | `relationship_reifier` **476** `relation` + 12 `cooccurrence`; `proposed_edge_governance` **2,428** `cooccurrence`. 100 % of typed (`relation`) edges come from the reifier. Steady-state ≈ 46–80 relation edges/day. |
| Reifier configuration, live | `max_candidates: 600` per run, `batch_size: 12`, `qualification_bar: 0.42`, `min_independent_sources: 2`, `temperature: 1.0`, `budget_tokens_per_day: 0`; cadence `45 */12 * * *` (2 runs/day ⇒ 1,200 candidates/day). Read from `analyst_descriptors` head. The code constant `MAX_CANDIDATES_PER_RUN = 40` is the *default*, overridden by the descriptor. |
| As-of query surface | **None.** `grep -n "as_of" src/legba/runtime/substrate_query_port.py` returns zero hits. `query_facts`, `query_nexuses` and `query_paths` hard-code the open-row predicate in SQL with no parameter; `get_timeline` has **no time parameter at all**; `list_findings` and `list_situations` carry only a relative `since_hours`. |
| `events` | **No such table.** Four tables match `%event%` — `budget_demotion_events`, `governor_events`, `read_events`, `situation_events` — and none is an event store. |

**One naming collision to fix in the reader's head before §2.** `situation_events` (migration
0184) is **not** a situation↔event junction table, despite carrying v1's name for one. It is
the *situation trajectory ledger*: one append-only row per movement of a situation, with
`delta ∈ escalates | de_escalates | broadens | unchanged_checkpoint`, a `state_from`/`state_to`
trajectory axis over `{watching, escalating, de_escalating, dormant, closed}`, an `occurred_at`
that is evidence time rather than run time, and a CHECK making an evidence-free delta claim
unrepresentable. v3 does not reuse it for event membership (§2.2).

---

## 2 · Events

An **event** is one real-world occurrence, evidenced by one or more signals, with a time span,
a place, actors in roles, a category and a lifecycle. It is the analytical unit v1 had and v2
replaced with `situations` + `get_timeline`.

A **situation** is not the same thing and v3 does not merge them. A situation is a persistent
*frame* — a thread the system watches, keyed by `sig:<topic>#dim:<dimension>`, whose members
are findings. An event is a bounded *occurrence*, keyed by its own identity, whose members
are signals. One situation tracks many events; one event may be tracked by more than one
situation.

### 2.1 Schema

Six tables. All additive; no existing column changes; every writer flag-gated off on arrival.
Migration numbers reserved in §7.1.

```sql
-- 0202_events.sql
CREATE TABLE events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- IDENTITY. The upsert key is the situations contract verbatim: one row per
    -- (signature, producer), re-materialized in place, never duplicated.
    event_signature text NOT NULL,
    analyst_id      text NOT NULL,        -- NOT NULL: NULLs are distinct in a unique
                                          -- index and would silently duplicate
                                          -- (the writes.py:858 guard, made structural)
    title           text NOT NULL,
    summary         text NOT NULL DEFAULT '',   -- NEVER evidence. See §2.5.

    -- CLASSIFICATION — March's vocabulary, unchanged (derived_events.py @ 6efd7219)
    category    text NOT NULL DEFAULT '',
    event_type  text NOT NULL DEFAULT 'incident'
        CHECK (event_type IN ('incident','development','shift','threshold')),
    severity    text NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('critical','high','medium','low','routine')),

    -- LIFECYCLE. Current state is ALSO derivable from event_lifecycle_events; this
    -- column is the fast-SQL convenience, and the ledger is the record. Same split
    -- as situations.status vs situation_events.state_to.
    lifecycle_state text NOT NULL DEFAULT 'emerging'
        CHECK (lifecycle_state IN
               ('emerging','developing','active','evolving','resolved')),
    lifecycle_changed_at timestamptz NOT NULL DEFAULT now(),

    -- WHEN — validity time. An event spans; a signal is a point.
    time_start timestamptz,
    time_end   timestamptz,

    -- WHERE — the shape `signals` and `facts` already use, so the map needs no join
    geo        text[] NOT NULL DEFAULT '{}',     -- ISO2, signals.geo shape
    geo_lat    double precision,
    geo_lon    double precision,
    locations  text[] NOT NULL DEFAULT '{}',     -- free-text place surfaces

    -- QUALITY
    confidence            real NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    -- the event's `signal_event_links` membership, counted from the link table
    -- itself and never carried on a payload: every write path reconciles both
    -- columns inside its own transaction after the link upserts
    -- (`events._writes.reconcile_event_rollups`), and migration 0218 corrected
    -- the rows written before that rule existed
    signal_count          int  NOT NULL DEFAULT 0,
    distinct_source_count int  NOT NULL DEFAULT 0,
    oversized             boolean NOT NULL DEFAULT false,  -- §2.4, the B1 guard

    -- TEMPORAL FRAME — the house supersession contract
    valid_from    timestamptz,
    valid_until   timestamptz,
    superseded_by uuid REFERENCES events(id) ON DELETE SET NULL,

    -- PROVENANCE — the standard envelope
    source_method text NOT NULL DEFAULT 'clustering'
        CHECK (source_method IN ('clustering','tower','manual')),
    source_type   text NOT NULL DEFAULT 'agent',
    derived_from  uuid[] NOT NULL DEFAULT '{}',
    target_id text, target_version text, analyst_version text, run_id uuid,
    schema_uri  text NOT NULL DEFAULT 'iglu:legba/event/jsonschema/1-0-0',
    data        jsonb NOT NULL DEFAULT '{}',
    produced_at timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT events_span_ordered
        CHECK (time_end IS NULL OR time_start IS NULL OR time_end >= time_start)
);

CREATE UNIQUE INDEX uq_events_signature_analyst
    ON events (event_signature, analyst_id);
CREATE INDEX idx_events_lifecycle  ON events (lifecycle_state, lifecycle_changed_at DESC);
CREATE INDEX idx_events_asof       ON events (valid_from, valid_until);
CREATE INDEX idx_events_time       ON events (time_start DESC);
CREATE INDEX idx_events_geo        ON events USING gin (geo);
CREATE INDEX idx_events_derived    ON events USING gin (derived_from);
CREATE INDEX idx_events_target     ON events (target_id) WHERE target_id IS NOT NULL;
-- the open-event read's own shape: one producer's events, newest-updated first
CREATE INDEX idx_events_analyst_updated ON events (analyst_id, updated_at DESC, id);

-- Many-to-many to the evidence. NO foreign key on signal_id, deliberately: it is the
-- shape `signal_entity_links` already ships, and a retention sweep must never
-- cascade-delete provenance. The orphan class March had (29 events with zero
-- signals) is closed by the invariant in §2.6 instead, which is a rule a receipt
-- can count rather than a delete a nobody sees.
CREATE TABLE signal_event_links (
    signal_id uuid NOT NULL,
    event_id  uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    relevance real NOT NULL DEFAULT 1.0
        CHECK (relevance >= 0.0 AND relevance <= 1.0),
    -- the three source axes, denormalized at link time so an event's cross-class
    -- composition is one query, never a four-table join (§2.8)
    source_class text NOT NULL DEFAULT 'reporting',
    source_kind  text NOT NULL DEFAULT '',     -- the HANDLER: rss / telegram_channel / …
    source_id    text NOT NULL DEFAULT '',
    linked_at   timestamptz NOT NULL,          -- EVIDENCE time (the signal's fetched_at)
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, event_id)
);
CREATE INDEX idx_sel_event ON signal_event_links (event_id, relevance DESC);

-- Actors, with roles. CASCADE on the entity, like signal_entity_links — plus the
-- counted pre-delete receipt the judge's Decision 2 requires, and the merge fold
-- called from entity_researcher.merge_pair (:568) beside fold_entity_edges (:638).
CREATE TABLE event_entity_links (
    event_id  uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id uuid NOT NULL REFERENCES entity_profiles(id) ON DELETE CASCADE,
    role text NOT NULL DEFAULT 'actor'
        CHECK (role IN ('actor','target','location','observer','victim','mediator')),
    confidence   real NOT NULL DEFAULT 0.5,
    derived_from uuid[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, entity_id, role)
);
CREATE INDEX idx_eel_entity ON event_entity_links (entity_id, role);
```

```sql
-- 0203_event_edges.sql
CREATE TABLE event_edges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    dst_event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    edge_type text NOT NULL CHECK (edge_type IN
        ('part_of','caused_by','evolves_from','correlated_with','contradicts')),
    confidence real NOT NULL DEFAULT 0.5,
    why  text NOT NULL DEFAULT '',
    derived_from uuid[] NOT NULL DEFAULT '{}',
    valid_from timestamptz, valid_until timestamptz,
    superseded_by uuid REFERENCES event_edges(id) ON DELETE SET NULL,
    analyst_id text, analyst_version text, run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_edges_no_self CHECK (src_event_id <> dst_event_id)
);
CREATE UNIQUE INDEX uq_event_edges_open
    ON event_edges (src_event_id, dst_event_id, edge_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX idx_event_edges_out ON event_edges (src_event_id, edge_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL;
CREATE INDEX idx_event_edges_in  ON event_edges (dst_event_id, edge_type)
    WHERE valid_until IS NULL AND superseded_by IS NULL;

CREATE TABLE situation_event_links (
    situation_id uuid NOT NULL REFERENCES situations(id) ON DELETE CASCADE,
    event_id     uuid NOT NULL REFERENCES events(id)     ON DELETE CASCADE,
    relevance    real NOT NULL DEFAULT 1.0,
    derived_from uuid[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (situation_id, event_id)
);
```

**Edge direction and symmetry, stated because both have bitten this codebase before.**
`caused_by` reads *src was caused by dst* — March's own example was
`(OilPriceSpike)-[:CAUSED_BY]->(HormuzClosure)`. `part_of` reads *src is a sub-event of dst*;
`evolves_from` reads *src succeeds dst*. `correlated_with` and `contradicts` are **symmetric**
and are stored canonically with `src_event_id < dst_event_id` by uuid ordering, enforced by a
CHECK on those two types — the same defect class as the `Russia|Russian × Ukraine|Ukrainian`
dyad inflation that migration 0078 had to clean out of `nexuses`.

```sql
-- 0204_event_lifecycle_events.sql — the append-only lifecycle ledger.
-- This is 0184_situation_events.sql's contract, applied to events.
CREATE TABLE event_lifecycle_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id    uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    occurred_at timestamptz NOT NULL,          -- EVIDENCE time, not run time
    transition  text NOT NULL CHECK (transition IN
        ('opened','advanced','accelerated','stabilised','resolved','reactivated')),
    state_from  text NOT NULL CHECK (state_from IN
        ('emerging','developing','active','evolving','resolved')),
    state_to    text NOT NULL CHECK (state_to IN
        ('emerging','developing','active','evolving','resolved')),
    why          text NOT NULL CHECK (btrim(why) <> ''),
    derived_from uuid[] NOT NULL DEFAULT '{}',
    analyst_id text, analyst_version text, run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),   -- DECISION time
    -- 'resolved' is the SILENCE transition: it asserts that nothing arrived, so it
    -- is the only one that may be evidence-free. Everything else claims a change
    -- and therefore requires the evidence that made it. Structurally, not by
    -- convention — this is situation_events_delta_requires_evidence, retyped.
    CONSTRAINT event_lifecycle_requires_evidence
        CHECK (transition = 'resolved' OR derived_from <> '{}')
);
CREATE INDEX idx_ele_event ON event_lifecycle_events (event_id, occurred_at DESC);
CREATE INDEX idx_ele_reactivations ON event_lifecycle_events (created_at DESC)
    WHERE transition = 'reactivated';
-- DELETE and UPDATE barred by trigger, verbatim from 0184.
```

**`situation_events` is not reused, and the reason is not squeamishness about names.**
`situation_events` (0184) is the *trajectory ledger*: one row per movement of a situation,
`delta ∈ escalates | de_escalates | broadens | unchanged_checkpoint`, `state_from`/`state_to`
over `{watching, escalating, de_escalating, dormant, closed}`, a NOT NULL `source_output_id`
with `UNIQUE (situation_id, source_output_id)`, and DELETE/UPDATE barred by trigger. It carries
v1's name for a junction table while being a different thing. Event membership has no
`source_output_id` (an event is not produced by one finding), and forcing it in would either
break the unique constraint or invent an output id — both worse than a three-column junction
table. `situation_event_links` is that table.

### 2.2 Identity and dedup

Event identity is **candidate matching first, signature second** — the shape the house already
uses for signals. The signature is not a similarity function; it is the stable content key
that makes re-running idempotent.

**The matcher.** Single-linkage over the enriched signal slice, on March's four features plus
two the pivot built and v1 did not have:

| feature | weight | implementation, reused not rewritten |
|---|---:|---|
| entity overlap | 0.30 | Jaccard over `_entity_canon.identity_fold(name)` sets — class-agnostic, idempotent, re-folds to itself (`_entity_canon.py:2114`). Gated by `is_junk_entity` (:1214) and by `differs_by_direction` (:1719), the negative gate earned by a census in which 30 of 726 phonetically-accepted pairs were opposing compass directions on an identical stem. |
| title similarity | 0.30 | normalized Levenshtein over `Dedupe4TierHandler.normalized_title` — the same function `cross_source_coalesce._titles_close` calls, so three surfaces cannot drift |
| temporal proximity | 0.20 | over `signals.fetched_at`, 24 h window (the `cross_source_coalesce` / ingest tier-4 window) |
| category match | 0.20 | `signals.tags` / classify output |
| **embedding cosine** | measured separately | Qdrant `legba_signals` (195,391 points, 1024-d cosine), **with the structural gate from `cross_source_dedup`** — `embedding_ref ~ <uuid regex>` — because 60.8 % of pairs at ≥0.80 are degenerate (both sides embedded from byte-identical or sub-floor input) and no threshold separates them (`cross_source_dedup.py:175-217`) |
| **shared fact subjects** | measured separately | `facts.derived_from @> ARRAY[signal_id]` — the relation-shaped triples extracted at ingest |

**Hold March's five numbers (0.30/0.30/0.20/0.20, threshold 0.50) at their v1 values in P1 and
measure the two new terms as additive gates, not as a re-weighting.** Re-guessing five weights
and adding two features in one change makes the result unattributable. The bake-off precedent
(`TYPING_BAKEOFF_2026-08-03.md` §7.3) is the house standard here: change one thing, sweep it,
report the sweep.

**Cross-source promotion bar.** A cluster becomes an `events` row only when it has **≥2 distinct
`source_id`** — the `MIN_INDEPENDENT_SOURCES = 2` precedent from `edge_qualification`, which
exists because a chatty source is not corroboration — **or** one signal whose source carries
`source_class='official'`. That second clause is March's "structured source singletons
auto-promoted" (NWS, USGS, GDACS) expressed in the vocabulary the pivot actually has.

**The mega-bucket guard — the one March defect that must not recur.** v1's own audit recorded
it as CRITICAL: *"147/528 events at signal_count=30 cap. Dua Lipa bikini, basketball scores,
and Iran strikes in same event."* v3 keeps the cap at **30** and changes what hitting it means:

> A cluster that reaches `_MAX_CLUSTER_MEMBERS` is stamped `oversized = true`, is **not
> promoted**, and is counted on the run receipt. A cap that is hit is evidence the threshold is
> wrong, not a cluster to ship.

A nonzero `oversized` count is a visible tuning signal on every run, which is precisely what v1
lacked. Truncating at the cap — what v1 did — converts a threshold bug into a published event.

**The signature.** Minted from the promoted cluster, not used to form it:

```
evt:<topic>|<top-K identity-folded entity tokens>#evt:<anchor_token(primary polity)>
```

* `#evt:` is a **reserved grammar slot that already exists and nothing writes** —
  `finding_supersession._SIGNATURE_EVENT_MARKER = "#evt:"` (`:309`). The existing parsers
  (`with_dimension`, `signature_dimension`, `strip_dimension`, all `rsplit(marker, 1)`) already
  read past it. Using it costs no parser change.
* `anchor_token` (`_frame_anchor.py:295`) has a **verified Postgres twin**, `ANCHOR_TOKEN_SQL`
  (`:272`), asserted row-for-row by `test_anchor_token_python_and_sql_agree`. The backfill
  (§7.2) is SQL and the handler is Python; the twin is why they cannot disagree. Any new token
  function in this design must ship the same pair.
* **K > 0 here, deliberately, and the situations lesson is why it needs saying.**
  `finding_supersession._SITUATION_SIGNATURE_ENTITY_K = 0` (`:222`) — so the live situation
  signature is `sig:<topic>#dim:<dimension>` and the `|<entities>` half is **unreachable code**.
  The entity segment was turned off for *frames* because it fragmented them. An event is
  narrower than a frame and its actor set is the greater part of its identity, so K belongs
  above zero here. It is a tunable with a swept default, not a guess, and P1's acceptance
  measures fragmentation at K ∈ {0, 2, 3, 4}.

**The re-attachment requirement — the court case months later.** A signal arriving in September
about a March occurrence re-attaches to the March event when the matcher clears threshold
against the event's existing member set, which is a **30-day member lookback** away from being
impossible. `situation_clustering._DEFAULT_LOOKBACK_DAYS = 30` (`:106`) bounds *its* member
scan for exactly the cost reason. Events need a second path, and it is cheap: the matcher
compares a new signal against **open event summaries' entity sets and embedding centroids**,
which is O(open events), not O(signals in window). With 295 situations as the scale analogue
the open-event population is small enough that no lookback horizon is needed at all. Reattachment
then writes a `reactivated` ledger row (§2.3). **The lookback bounds the signal scan, never the
event scan** — that distinction is the whole feature.

### 2.3 The lifecycle FSM

Five states, a pure transition function, and a ledger. The module is
`src/legba/data/events/lifecycle.py`, written as a leaf on the model of
`src/legba/data/situations/trajectory.py`: vocabulary constants, `next_state(...)` raising
rather than coercing, a frozen dataclass validated at construction, and a DB CHECK mirroring
the Python vocabulary so no future writer can bypass it.

| from | to | deterministic trigger | ledger `transition` |
|---|---|---|---|
| *(none)* | `emerging` | promotion (§2.2) | `opened` |
| `emerging` | `developing` | `signal_count >= 3` | `advanced` |
| `developing` | `active` | `signal_count >= 5` AND `confidence >= 0.6` | `advanced` |
| `active` | `evolving` | link arrival rate over the trailing 24 h ≥ 2× the trailing 7-day baseline, **or** a new actor / new ISO2 attaches | `accelerated` |
| `evolving` | `active` | rate back inside [0.5×, 2×] baseline | `stabilised` |
| `emerging` | `resolved` | no new link in 48 h | `resolved` |
| `developing` | `resolved` | no new link in 72 h | `resolved` |
| `active`, `evolving` | `resolved` | no new link in 7 days | `resolved` |
| `resolved` | `developing` | **a new signal links** | `reactivated` |

Three rules that are not in March and are not negotiable:

1. **Every clock runs on evidence time, never run time.** The window is measured over
   `signal_event_links.linked_at` (the signal's `fetched_at`), the way `situation_events.occurred_at`
   is evidence time. A backlog drain must not resolve a live event, and a re-ingest must not
   reactivate a dead one.
2. **`REACTIVATED` is a transition, not a state.** March made it a sixth state that immediately
   became `DEVELOPING` — a state never observed at rest, which is an edge wearing a state's
   clothes. The house already ruled on this shape once: `trajectory._REOPENABLE` takes a
   `dormant` or `closed` situation straight back to `watching` on new evidence, with the
   reopening recorded as the ledger row rather than as a state. The requirement the operator
   asked for — the court case months later reopens the event — is met exactly: the row is
   `reactivated` in `event_lifecycle_events`, indexed by `idx_ele_reactivations`, and
   `"how often do resolved events come back"` becomes one query.
3. **Silence never reactivates and never closes twice.** `resolved` is the only evidence-free
   transition (the CHECK constraint enforces it). A resolved event that stays quiet writes
   nothing — no heartbeat rows.

### 2.4 The two candidate sources, and the promotion rule

**Source 1 — clustering over enriched signals** (§2.2). `source_method='clustering'`. This is
the volume path: ~4 k signals/day arrive enriched with language, geo, NER, classification,
credibility, salience, embeddings and extracted fact triples — far more than v1's clusterer had.

**Source 2 — the tower.** Two shapes, both already sitting in the substrate:

* **A desk finding's cited signal set.** A verify-passed `finding` whose citations resolve to
  ≥2 signals is, by construction, a set of signals an analyst asserted belong together, and
  the faithfulness judge has already checked that the claim follows from them. That is a
  stronger event candidate than any clustering score.
* **A `situation_events` ledger row.** A row with `delta <> 'unchanged_checkpoint'` carries a
  non-empty `derived_from` of verified findings and a `why` — a movement of a watched thread,
  dated at evidence time. 3,214 such rows exist.

`source_method='tower'`.

**The promotion rule, one sentence per path:**

| path | bar | rationale |
|---|---|---|
| clustering | ≥2 distinct `source_id`, **or** one `source_class='official'`; **and** not `oversized` | corroboration, not chattiness |
| tower — finding | the finding passed faithfulness at the house floor (`effective_confidence >= 0.50`) and cites ≥2 resolvable signals | the judge already grounded it |
| tower — situation_events | `delta <> 'unchanged_checkpoint'` and `derived_from <> '{}'` | the DB CHECK already guarantees the evidence |
| manual | operator | — |

A candidate that does not clear its bar is **not written and not queued**. There is no
`event_candidates` table — the judge's Decision 4 applied to a second domain: a candidate queue
that drains at a fraction of its arrival rate becomes a 800 k-row hairball (`proposed_edges`
today: 802,702 rows, 320,161 permanently pending). Clustering is deterministic over the signal
slice, so a re-run recomputes what it needs; the run receipt counts what it declined and why.

**Collision between the two paths is expected and is resolved by the signature.** A tower
candidate and a clustering candidate for the same occurrence mint the same
`event_signature`; the second write is an upsert on `(event_signature, analyst_id)`.
Because `analyst_id` is part of the key, a clustering event and a tower event about the same
occurrence are **two rows, not one** — and that is correct: they are two producers' readings,
and merging them silently would be the same error as pooling two graders' verdicts. They are
joined by an `event_edges` row of type `correlated_with` written by a deterministic
reconciler, so a reader sees both and the lineage says who said what.

### 2.5 Provenance — the new part

**The new ref kind is `event`, and it is resolved by expansion at citation-build time, never
at judge time.**

This is the design decision that keeps the judge untouched. `source_text` is not fetched by
`verify.py` — it is captured when the slice is rendered, in
`inline_target._citation_entry` (`:1262`), through a precedence chain that is the faithfulness
trust boundary (`:1313-1323`):

```python
raw_source = (fields.get("archived_text")   # the archived full article LEADS
              or fields.get("raw_body") or fields.get("text") or fields.get("body")
              or fields.get("content") or fields.get("content_text")
              or fields.get("summary") or fields.get("description") or "")
```

— with `distilled_body`, our own LLM summary, **deliberately excluded** so the judge catches a
summariser hallucination instead of rubber-stamping it. The text is capped at
`_SOURCE_TEXT_CHARS = 3200` (`inline_target.py:378`) and read by the judge at
`judge_evidence.py:171`. `verify.py` is stdlib-only and slim-image-safe by design.

So: **when an analyst cites `event:<uuid>`, the citation builder emits one citation entry per
contributing signal** — each a normal entry carrying `signal_id` and `source_text` through the
same precedence and the same cap — and stamps `ref_kind="event"` and `event_id` on each entry
so the lineage remains visible. The judge grounds on signal source text through the path it
already uses. **`verify.py` requires no change at all.**

Four rules follow, and each is a test:

1. **`event` is NOT added to `GROUNDING_REF_KINDS`.** That set —
   `{prior_read, situation_register, desk_baseline, open_questions, window_ledger,
   open_events, observation}` — is for blocks that have **no `signal_id`** and carry their
   rendered bytes in `evidence_text`. (`observation` is the collections one: ONE row of a curated
   historical holding, carrying a real `ref_id` because an `observations` row HAS a uuid, and
   graded on the deterministic rendering of the row itself — provider, series, subject, value
   with its unit, valid period, record time, source URL, file `sha256`. That text is not a
   *summary of* evidence, it **is** the evidence, which is exactly the property `event` lacks
   and exactly why the two are on opposite sides of this line.) `is_grounding_citation` returns `False` the moment an
   entry has a `signal_id` (`kinds.py:477`), which is exactly right for an expanded event
   entry. Adding `event` to the set would be the bug the whole feature exists to avoid: it
   would permit an event to be cited with `evidence_text` set to the event's own summary, and
   pass.
2. **The event's `summary` never reaches the judge.** It is not written into `source_text`,
   `evidence_text` or `snippet` by any path. A test asserts that an `events.summary` string
   never appears in a rendered evidence envelope.
3. **The expansion is ranked, capped and honestly truncated.** Ordered by
   `signal_event_links.relevance DESC, linked_at DESC`, capped at
   `_EVENT_EXPANSION_MAX_SIGNALS` (proposed 4 — the judge's envelope is already bounded at
   `_EVIDENCE_TOTAL_CHARS = 8000` / `_EVIDENCE_SOURCE_CHARS = 6000`, `verify.py:1700` and
   `:1684`; note [`TUNABLES.md`](TUNABLES.md) §2 is **stale** on these three, listing
   4,000 / 3,000 / 2,400 against the code's 8,000 / 6,000 / 4,800), with
   `event_expansion_truncated: true` stamped on each entry when the event has more members.
   The same honest-truncation idiom the `claim_verdicts` ledger already uses.
4. **`derived_from` carries both.** A finding citing an event stamps the **event id and the
   expanded signal ids**, so a lineage walk resolves either way and `output_consumption`
   still names the load-bearing rows for "who would be affected if this were wrong".

**What the downstream graders do — corrected against the code.**

* **The faithfulness judge** grounds on the expanded signals' `source_text`, as above. Unchanged
  rubric, unchanged scoring, unchanged floor.
* **The correctness grader does not see citations at all, and an event changes nothing for it.**
  The brief assumed it drills to signals; it does not. G-5 is explicit
  (`_external_audit_grader.py:34-37`): *"the grader never sees the read's own citations."*
  Citation markers are stripped before grading (`_correctness_segment.py:485-487`), the packet
  ships `{p1_id, assertion, reference}` only, and the terminus is
  `unit_references.ref_json.developments[].decisive_span` — a verbatim quote from an
  externally-built reference, not a row in our evidence graph.
* **The external audit** grades a claim against fetched web pages with a verbatim span check
  (`external_span_check.check_span` through the shared `text_fold.fold_contains`). It reads
  claim text, not citations. Unchanged.
* **The lineage walk** gains `events` in `lineage_api._SUBSTRATE_TABLES` so `/lineage` resolves
  an event node. `journal_entries` stays excluded, as it is today.

**Events flow through the write choke point.** `OutputKind` gains a 14th member, `EVENT`, with
an `EventPayload` and a `KIND_REGISTRY` entry routing to the `events` table — the registry is a
dict with a public `register_kind(spec, overwrite=False)` (`kinds.py:648`), and
`_insert_event` carries the `ON CONFLICT` the way `_insert_situation` (`writes.py:841`) already
does. This deliberately avoids creating a **fourth** bypass of `write_analyst_output`: there are
three today (`fact_extractor.py:2084`, `journal_proposals_apply.py:267`,
`situation_clustering.py:846`) and each one is a place the DLQ, the NATS publish and the
provenance envelope have to be re-implemented. A new `OutputKind` is a shared-schema change,
so the registry image is rebuilt first and the new-kind activation checklist runs — the
procedure `descriptors/analyst_situation_tracker.yaml`'s own header documents.

### 2.6 What an event may NOT be used for

These are invariants, each with a test in P0/P2.

* **Never a fact truth-maker by itself.** An event does not write, close or supersede a `facts`
  row. The fact writers remain `fact_extractor` and `write_fact`. An event is a *container for
  evidence*, not an assertion about the world — the distinction `nexuses` blurred and migration
  0078 had to clean up.
* **Never groundable evidence on its own.** §2.5 rules 1 and 2.
* **Never admitted to a composition basis.** A composition's basis is verify-passed sub-claims
  (`INNER JOIN` on the faithfulness critique). An event is something a finding may cite; it is
  not a claim and has no faithfulness score.
* **Never a scorecard band basis.** Bands rest on already-verified claims and name the claim id
  they rest on.
* **Never deleted to remove evidence.** An event whose linked signals no longer resolve is
  **closed** — `valid_until` stamped, `data.closed_reason = 'evidence_purged'`, a ledger row
  written — never dropped. A deterministic integrity pass counts such events on every run, so
  the orphan class March shipped (29 events with zero signals) is a number on a receipt rather
  than a silent population. Signals cited by verified findings already carry
  `retention_class = 'evidence_hold'` and are archived content-addressed in `evidence_archive`
  (98,813 rows), so the common case does not arise.
* **Never a journal node.** The journal stays off-chain with an always-empty `derived_from`
  and stays out of the lineage catalog.

### 2.7 Geo

`events.geo text[]` (ISO2, the `signals.geo` shape) + `geo_lat` / `geo_lon` +
`locations text[]`. Resolution at write time: the modal ISO2 across the linked signals'
`signals.geo`; the centroid from the highest-relevance signal carrying `data.geo.{lat,lon}`.

This is the map's whole reason to want events. Target `scope.geo` is `[ISO2]` for country desks
and `[]` for region, lane and flow desks, so a region finding has no centroid and never places —
the 2026-09-21 map fix placed the 13 watch desks and could do nothing for the rest, by design.
`legba-ui-v3/src/v4/world/mapData.ts` already falls back to `centroidOf(countries)` for
findings and situations; an event needs no fallback because it carries its own geo.

### 2.8 Source class on links

`signal_event_links` denormalizes three axes at link time, because they are three different
questions and the codebase already has a module whose job is keeping them apart
(`src/legba/data/retrieval_origin.py`, whose banner forbids expressing retrieval origin as a
source class):

| column | what it is | vocabulary |
|---|---|---|
| `source_class` | **editorial authority** | closed Literal of four: `reporting`, `analysis`, `official`, `state_media` (`schemas/source.py:41`). **There is no `source_class` DB column on `source_descriptors`** — it lives in `body->'scope'->>'source_class'` and is Pydantic-enforced only. |
| `source_kind` | **the handler — this is where RSS and Telegram differ** | open string, no CHECK; the de-facto vocabulary is `runtime/source_factory.py:77-94`. Note the handler for Telegram registers as **`telegram_channel`**, not `telegram`. |
| `source_id` | the descriptor | — |

So the cross-class reconciliation the brief asks for is: an RSS item and a Telegram post about
one occurrence become **one event with two links**, differing in `source_kind`, both
`source_class='reporting'`. That is correct — a Reuters wire story and a Reuters Telegram post
carry the same editorial authority and arrived by different transports.

**The honest gap: there is no `field_report` and no `private` source class today.** A
case-insensitive grep for `field[ _-]report` and `humint` across `src/`, `docs/`, `migrations/`,
`descriptors/` and `seeds/` returns zero hits, and `private` appears only as a redaction
stoplist word and in descriptor comments explaining why a privately-owned outlet is still
`reporting`. The nearest real concept is `license_class`, which governs retention rights, not
authority. v3 builds the machinery — per-link class, an event that spans classes, a
composition query over it — and adding a fifth class is a one-line Pydantic Literal edit plus a
descriptor, with no migration. Whether to add one is §8, Q4.
---

## 3 · The temporal surface

The stores already carry time. Nothing can ask them a dated question. v3 adds readers, one
missing ledger, and a vocabulary that keeps two different clocks from being confused.

### 3.1 What already carries time — verified column by column

Measured from `information_schema.columns` on 2026-09-21. `✓` means a column with **that
exact name** exists.

| table | `valid_from` | `valid_until` | `superseded_by` | `superseded_at` | `observed_count` | `first_seen_at` | `last_seen_at` | `occurred_at` | `produced_at` | `created_at` | `updated_at` |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `facts` | ✓ | ✓ | ✓ | — | — | — | — | — | ✓ | ✓ | ✓ |
| `entity_edges` | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ |
| `nexuses` | ✓ | ✓ | ✓ | — | — | — | — | — | ✓ | ✓ | ✓ |
| `situations` | ✓ | ✓ | ✓ | — | — | — | — | — | ✓ | ✓ | ✓ |
| `journal_entries` | ✓ | ✓ | ✓ | — | — | — | — | — | ✓ | ✓ | ✓ |
| `analyst_outputs` | **—** | **—** | ✓ | **✓** | — | — | — | — | ✓ | ✓ | **—** |
| `situation_events` | — | — | — | — | — | — | — | **✓** | — | ✓ | — |
| `signals` | — | — | — | — | — | — | ✓ | — | — | ✓ | ✓ |
| `proposed_edges` | — | — | — | — | — | — | — | — | ✓ | ✓ | **—** |
| `bearing_edges` | — | — | — | — | — | — | — | — | — | ✓ | **—** |

Four corrections to the design brief, each load-bearing:

1. **`analyst_outputs` does not carry `valid_from` / `valid_until`.** It versions by
   `superseded_by uuid` + `superseded_at timestamptz`. Its validity interval is therefore
   `[produced_at, superseded_at)`, and any belief reader must be written against those two
   columns, not against the `valid_*` pair every other table uses.
2. **No table anywhere has a bare `first_seen`, `last_seen`, `closed_at` or `as_of` column.**
   `entity_edges` has `first_seen_at` / `last_seen_at`; `signals` has `last_seen_at` only;
   `bearing_edges` has `src_as_of` / `dst_as_of`, never a bare `as_of`.
3. **`superseded_at` exists on exactly one table — `analyst_outputs`.** Every other
   supersession-bearing table carries only `superseded_by` + `valid_until`, so "when was this
   closed" is answered by `valid_until` there and by `superseded_at` here.
4. **`analyst_outputs`, `proposed_edges`, `situation_events` and `bearing_edges` have no
   `updated_at`.** A watermarked reader cannot assume one.

There is also a correction to [`DATA_MODEL.md`](DATA_MODEL.md) itself that a belief reader
trips over immediately: **the table `analyst_critiques` holds zero rows** (measured
2026-09-21) and its column set (`trace_id`, `judge_analyst_id`, `rubric_uri`, `scores`,
`overall_score`, `revision_delta`, `produced_at`) is not the faithfulness shape. The
faithfulness verdicts live in `analyst_outputs` with `kind='critique'` and
`title LIKE 'Faithfulness verify%'`, carrying `data.verification.*`, keyed to the graded row
by `data->>'analyzed_output_id'` — which is exactly what the partial index
`idx_analyst_outputs_critique_analyzed_output_id` indexes. Live `kind` distribution:
critique 46,660 · finding 45,050 · alert 11,655 · scorecard 2,430 · situation_update 816 ·
prediction 539 · prompt_module_candidate 10.

### 3.2 As-of semantics — two clocks, two parameter names

The system has two time axes and they answer different questions.

| axis | columns | the question | where it lives |
|---|---|---|---|
| **validity time** | `valid_from` / `valid_until` | *what was true in the world on date D* | `facts`, `nexuses`, `entity_edges`, `situations`, `events` (new) |
| **decision time** | `produced_at` / `superseded_at` (`created_at` elsewhere) | *what Legba believed on date D* | `analyst_outputs`, `analyst_traces`, every receipt |

**Decision: `as_of` always means validity time; `believed_as_of` always means decision time.**
They are never the same parameter and never the same default. Calling both `as_of` is the
single easiest way to publish a confident answer to a question nobody asked, which is the
class of defect the faithfulness programme exists to remove.

**The as-of predicate, canonical form:**

```sql
    COALESCE(valid_from, '-infinity'::timestamptz) <= $as_of
AND (valid_until IS NULL OR valid_until > $as_of)
```

Two rules go with it, and both are counter-intuitive enough to need saying:

* **An as-of read MUST NOT apply the open-row predicate.** Every current reader carries
  `superseded_by IS NULL AND valid_until IS NULL` hard-coded in SQL. A row that has since been
  superseded was *the* answer on date D — excluding it is exactly the error the whole feature
  exists to fix. The house write paths already stamp `valid_until = now()` on the loser at
  supersede time (`entity_edge_writes.py:231`; the same contract on `facts` per
  `NEXUS_AND_TEMPORAL.md` §3), so `valid_until` alone carries the close and `superseded_by`
  must be dropped from the filter whenever `as_of` is supplied.
* **`valid_from` is frequently NULL, and the honest handling is to over-include and say so.**
  `COALESCE(valid_from, '-infinity')` treats an unbounded start as "held for all time up to
  `valid_until`"; `COALESCE(valid_from, created_at)` would silently hide a 1949 NATO
  membership extracted from a 2026 article from every query before 2026. So: over-include,
  and return `unbounded_start: N` in the tool's response envelope beside `count`, so the
  reader can see how much of the answer rests on a start date nobody recorded. This is the
  `insufficient-evidence` idiom applied to time.

**Bitemporality, honestly.** With the edge transition ledger of §3.5 in place,
`entity_edges` becomes genuinely bitemporal: `occurred_at` on the ledger row is validity
time, `created_at` is decision time. `facts`, `nexuses` and `situations` remain
**uni-temporal-plus-a-decision-stamp**: their validity interval is queryable, their belief
*history* is not, because a supersession rewrites the loser in place rather than appending.
v3 does not fix that for those three tables — the cost is a ledger per table and the demand
does not exist — but the spec records it so nobody later mistakes an `as_of` answer for a
reconstruction of what was believed at D.

**The origin class, and the reader firewall (P7, migration 0209).** `signals`,
`facts` and `events` now carry `origin_class` — where the row came from, in a
closed six-class vocabulary (`live · web_retrieval · seed · backfill_native ·
backfill_reconstructed · archive`) — plus a `collection_id` pointer with no
foreign key yet (the `collection_descriptors` family arrived with migration 0220). The reason the
column had to precede the rows: the open-row gate
(`superseded_by IS NULL AND valid_until IS NULL`) cannot tell a backfilled
2016 fact from one minted this hour, so "now" reads would silently absorb
imported history into freshness, source health, calibration, salience and the
alert plane. The firewall is therefore the class, not the validity window —
`live_gate_sql()` in `data/provenance/origin.py` renders the one open-gate
string (the 0032 pair plus `origin_class IN ('live','web_retrieval','seed')`),
`as_of_gate_sql()` renders the one validity predicate, and the two v3 reader
files take both from there.

**The sweep has landed (migration 0220).** Every open-row READ and
SUPERSESSION site on `facts` and `events`, and every reader on the eight
surfaces a collection is fenced from — cadence analysts, freshness, source
health, calibration, salience, alerts, reactive triggers, surge detection —
now renders its predicate from `origin_class_clause()`; the reactive trigger
plane gates the delivered ROW instead
(`runtime/triggers/coalescer.py:is_live_origin`), because there is no WHERE
clause on a NATS message. `tests/data_pkg/test_origin_gate_inventory.py` pins
every `superseded_by IS NULL` site in `src/` into one of three buckets —
swept, `ON CONFLICT` index predicate (which cannot carry the leg without
ceasing to match its partial index), or on a table with no `origin_class`
column at all — and `tests/data_pkg/test_origin_firewall_surfaces.py` proves
the gate against a synthetic 40-row history burst planted beside a 3-row live
population. SEAMS #57 is resolved. The CHECK constraints do NOT go away: they
are renamed `<table>_origin_class_history_writer_not_built`, because nothing
may WRITE a history row to these three tables yet (SEAMS #62) — collection
SERIES land in `observations`, documents have no loader, and `facts`'
open-triple partial unique index carries no `origin_class` leg, so a history
fact would collide with a live one rather than coexist. `facts`
also gains `superseded_at`, closing the asymmetry `analyst_outputs` already
had for the decision-time read — stamped by the write path on every close and
backfilled for pre-0209 rows as the successor's `created_at`, an approximation
the migration header says plainly.

**Collections and `observations` (migration 0220).** A *source* is a
live feed; a *collection* is a bounded, versioned HOLDING of the past, loaded
once by the operator and never scheduled. It is its own descriptor family
(`legba/collection/1.0.0`, `collection_descriptors`, file convention
`descriptors/collection_*.yaml` and never `source_`) with its own four-state
lifecycle — `draft → reviewed → loaded → superseded` — and a `firewall:`
block that must name all eight fenced surfaces by equality or the descriptor
does not validate.

Series land in **`observations`**: bitemporal (`valid_from`/`valid_to` is the
period a number is ABOUT, `record_time` is when the provider published or
revised it, so a 2016 figure revised in 2023 is TWO rows), natively
partitioned by RANGE on `valid_from` — one partition per year 2016–2027 plus
a DEFAULT, no TimescaleDB — and keyed on
`(collection_id, series_id, subject, valid_from, valid_to, record_time)`,
which is what makes the loader idempotent rather than any bookkeeping in the
loader itself. `origin_class` is CHECKed to the three HISTORY classes only:
a collection can never write a live-class row. **No row exists for a year a
provider does not hold** — absence is absence, and a `value`/`value_text`
CHECK makes a valueless row unstorable.

Two sidecars complete it: **`entity_aliases`** (provider naming → our entity
ids, scoped per collection, read only by the loader and by the collection readers so
a provider quirk can never re-shape the live graph) and **`collection_loads`**
(one row per (collection, manifest version) carrying the resume key, the
counts, the timestamps and the status). The manifest VERSION is the hash of
the manifest, not of the descriptor, so re-approving a licence line does not
invalidate a load that already happened.

### 3.3 Tool changes

Six existing tools change and two are added. Exact current signatures are in §6.1.

| tool | change | why this parameter and not another |
|---|---|---|
| `query_facts` | `+ as_of: str \| None = None` | validity: what the fact store held on D |
| `query_nexuses` | `+ as_of` | same |
| `query_paths` | `+ as_of` | the walk must apply the predicate **per hop**, on both the seed and the recursive arm of `_PATH_WALK_SQL`. A path through an edge that was closed on D is not a path that existed on D. |
| `find_proxy_chains`, `query_brokers` | `+ as_of` | they share `_PATH_WALK_SQL` / `_BROKER_WALK_SQL`; leaving them out would make three tools disagree about the same graph |
| `list_situations` | `+ as_of` | validity: `situations` carries `valid_from`/`valid_until` |
| `get_timeline` | `+ since`, `+ until` (absolute ISO-8601), **not** `as_of` | a timeline *is* the history; an as-of on it is meaningless. What it lacks is a **window** — today it has no time parameter at all and returns the newest 200 rows per stream. |
| `list_findings` | `+ believed_as_of`, **not** `as_of` | a finding is a belief, not a world-state. `analyst_outputs` has no `valid_*` columns (§3.1). |
| `query_events` (new) | `as_of`, plus `since`/`until` | §6.1 |
| `belief_as_of` (new) | §3.4 | |

`as_of`, `since`, `until` and `believed_as_of` are **ISO-8601 strings on the wire** (the model
emits JSON) parsed to `timestamptz` at the port boundary, refusing loud on a malformed value —
never coerced to `now()`, which would answer a different question and say nothing.

**Default is `None`, and `None` keeps today's behaviour byte-for-byte.** Every existing caller,
prompt catalogue and test path is unchanged when the parameter is absent: the open-row
predicate still applies, the same SQL plan runs, the same rows come back. That is the
compatibility contract for phase P3 and the thing its acceptance proof checks.

### 3.4 The belief-as-of reader

`belief_as_of(target_id, as_of, *, fold_verdicts="as_of", limit=20)` answers *what did Legba
publish about this desk on date D, and how good did it think that read was at the time*.

```sql
-- the heads that were live at D
SELECT o.* FROM analyst_outputs o
 WHERE o.kind = 'finding'
   AND ($target IS NULL OR o.target_id = $target)
   AND o.produced_at <= $d
   AND (o.superseded_at IS NULL OR o.superseded_at > $d)
```

folded against the faithfulness verdict, with the fold itself dated:

```sql
-- fold_verdicts='as_of'  : the verdict Legba held on D (NULL if it had not judged yet)
-- fold_verdicts='latest' : today's verdict, applied retrospectively
LEFT JOIN LATERAL (
  SELECT c.* FROM analyst_outputs c
   WHERE c.kind = 'critique'
     AND c.data->>'analyzed_output_id' = o.id::text
     AND (NOT $as_of_fold OR c.produced_at <= $d)
   ORDER BY c.produced_at DESC LIMIT 1
) v ON TRUE
```

Two readings, deliberately both available and never pooled:

* **`fold_verdicts='as_of'` (the default)** reconstructs the *state of belief* at D. Findings
  whose verdict had not landed by D come back with `effective_confidence = NULL` and
  `verdict_pending_at_as_of = true`. That count is the honest headline: on most days a
  meaningful share of what Legba had published was not yet verified, and a reader deserves to
  see that rather than a retro-graded number.
* **`fold_verdicts='latest'`** applies today's verdict to D's rows. Useful for "was that read
  any good", useless for "what did it think at the time".

Every response stamps which fold was used. Summing the two is meaningless and the reader
returns no single pooled number — the same rule `external_truth.headline_pair` already
enforces for the record/voice split.

### 3.5 The edge transition history — checked, and it is missing

The brief asked whether `entity_edges`' supersession already gives the March event-sourcing
recommendation. **It does not.** Verified 2026-09-21:

* `SELECT tgname FROM pg_trigger WHERE tgrelid='entity_edges'::regclass AND NOT tgisinternal`
  returns **zero rows**. No history table, no shadow table, no audit table exists
  (`entity_edges_unresolved` is an endpoint park, not a history).
* The entire write path is three statements in one module,
  `src/legba/data/provenance/entity_edge_writes.py`; a repo-wide grep for
  `UPDATE|INSERT INTO|DELETE FROM entity_edges` finds no other site.
* **The close is an in-place UPDATE** (`entity_edge_writes.py:231`, `close_prior_entity_edges`
  at :209): `SET valid_until = now(), updated_at = now() … AND polarity <> $5`. Only a
  polarity *flip* supersedes. The prior row survives, but its pre-close attribute values are
  never snapshotted.
* **A same-polarity re-assert mutates the open row and writes no new row at all**
  (`_INSERT_SQL` at :248, `DO UPDATE` at :271): `observed_count = observed_count + 1`,
  `confidence = GREATEST(...)`, `last_seen_at = GREATEST(...)`, arrays unioned.
* Live consequence: of 45,613 edges, **31** carry `superseded_by` (0.068 %), while
  `sum(observed_count) = 54,485` — **~8,872 re-observations have already been folded into
  existing rows with no history row written, and are unrecoverable.**

**Decision: build `entity_edge_events`, an append-only transition ledger, and record
*transitions* rather than *observations*.** A row per re-observation would be ~2,900/week of
low-information noise already summarised by `observed_count` / `last_seen_at`. A row per
transition is ~420/day today and answers the question that matters — *when did this edge
change, and on what evidence*.

```sql
-- 0206_entity_edge_events.sql  (number reserved in §7.1)
CREATE TABLE entity_edge_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_id       uuid NOT NULL REFERENCES entity_edges(id) ON DELETE CASCADE,
    occurred_at   timestamptz NOT NULL,          -- VALIDITY time (evidence time)
    transition    text NOT NULL
        CHECK (transition IN ('observed','polarity_flip','retyped',
                              'closed','folded','reopened')),
    polarity_from smallint, polarity_to smallint,
    edge_type_from text,    edge_type_to text,
    why           text NOT NULL CHECK (btrim(why) <> ''),
    derived_from  uuid[] NOT NULL DEFAULT '{}',
    analyst_id    text, analyst_version text, run_id uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),   -- DECISION time
    CONSTRAINT entity_edge_events_evidence
        CHECK (transition = 'observed' OR derived_from <> '{}')
);
CREATE INDEX idx_entity_edge_events_edge ON entity_edge_events (edge_id, occurred_at DESC);
CREATE INDEX idx_entity_edge_events_when ON entity_edge_events (occurred_at DESC);
-- DELETE and UPDATE barred by trigger, the situation_events (0184) contract verbatim.
```

Written from `entity_edge_writes.py` **inside the same transaction as the upsert** — the
judge's Decision 5 applied to a second table in the same store: both writes or neither.
Gated by `LEGBA_EDGE_TRANSITION_LEDGER`, default **off**; when off, `entity_edge_writes`
behaves byte-identically to today.

What this does **not** do: it does not backfill the 8,872 lost folds (they are gone), and it
does not give `facts` / `nexuses` / `situations` the same property. Both limits are stated in
the table's own header comment so a later reader does not assume otherwise.

### 3.6 No TimescaleDB

The pivot removed the time-series store from the codebase with zero callers
([`DATA_MODEL.md`](DATA_MODEL.md)); a metrics store is a declared seam and
`anomaly_detection` reads `time_bucket()` from the primary pool. Every temporal read in this
section is an index-driven predicate on `timestamptz` columns that already exist. v3 adds no
store.
---

## 4 · The unified graph

Today the layers are joined by foreign keys, `derived_from uuid[]` arrays, text endpoints
resolved through `resolve_entity_name()`, and one polymorphic table (`bearing_edges`). Every
one of those joins is an edge that nobody has named. v3 names them once, in one vocabulary,
and projects them into one queryable structure that is **derived, disposable and rebuilt
whole**.

It does not make them a new authoritative table. The judge's Decision 1 stands and is not
reopened: `entity_edges` is entity↔entity with real foreign keys, *not* polymorphic, because a
polymorphic endpoint cannot carry one — and `bearing_edges`, the house's own polymorphic
precedent, has no FK constraints at all. The cross-layer graph is a **projection**.

### 4.1 The edge vocabulary

One row per arc type. `plane` is the load-bearing column — see §4.2.

| from-kind | to-kind | `arc_type` | plane | meaning | source table / column today |
|---|---|---|---|---|---|
| signal | entity | `mentions` | evidence | NER-resolved mention, with role and confidence | `signal_entity_links (signal_id, entity_id, role)` — 848,436 |
| signal | signal | `duplicate_of` | evidence | dedupe alias (4-tier + cross-source) | `signal_aliases` + `signals.canonical_signal_id` — 15,471 |
| signal | event | `evidences` | world | **new** | `signal_event_links` |
| entity | entity | `<edge_type>` | world | typed / imported / co-mention relation, four families | `entity_edges (src_id, dst_id, edge_type, edge_family, polarity)` — 45,613 |
| entity | entity | `via` | world | the reified proxy channel (the Iran→Hamas→Israel shape) | `entity_edges.intermediary_id` |
| entity | entity | `merged_into` | evidence | entity-resolution tombstone → keeper | `entity_profiles.merged_into` — 1,374 |
| entity | event | `involved_in` | world | actor / target / location / observer / victim / mediator | **new** `event_entity_links` |
| event | event | `part_of` · `caused_by` · `evolves_from` · `correlated_with` · `contradicts` | world | **new** | `event_edges` |
| event | situation | `tracked_by` | world | **new** | `situation_event_links` |
| fact | entity | `subject_of` · `value_of` | world | a fact's text endpoints, resolved | `facts.subject` / `facts.value` via `resolve_entity_name()` (0143); the relational subset already projected by 0180 |
| fact | fact | `contends_with` | world | live dispute over one `(subject, predicate)` | `fact_contention` / `fact_contention_values` |
| situation | finding | `member_of` | lineage | the verified findings constituting the frame | `situations.derived_from` — 14,657 |
| situation | situation | `moved_by` | lineage | trajectory ledger row (delta + state transition) | `situation_events.situation_id` — 3,214 |
| finding | signal | `cites` | lineage | a cited claim's evidence | `analyst_outputs.derived_from` + `data.citations` |
| finding | event | `cites` | lineage | **new** — the event ref kind (§2.5) | `analyst_outputs.derived_from` |
| finding | finding | `cites` | lineage | composition basis / periphery, tier-labelled | `output_consumption` — 48,076 |
| finding | finding | `supersedes` | lineage | head chain | `analyst_outputs.superseded_by` — 42,008 |
| finding | finding | `judged_by` | lineage | the faithfulness verdict | `analyst_outputs` `kind='critique'`, `data->>'analyzed_output_id'` |
| signal \| finding | hypothesis | `bears_on` | lineage | dated typed pointer; *not* lineage in the derived-from sense | `bearing_edges` — 22,969 |
| narrative | narrative | `echoes` | world | narrative propagation | `narrative_echo_edges` — 44 |

**Deliberately excluded, each for a stated reason:**

* **`proposed_edges` (802,702 rows, 320,161 permanently pending, 100 % untyped `co_occurs`)** —
  the judge's Decision 4. It is a candidate queue, not a graph. Only `promoted` rows cross, and
  they cross into `entity_edges`, which the projection already reads.
* **`journal_entries`** — the journal is off-chain by construction: always-empty `derived_from`,
  absent from `lineage_api._SUBSTRATE_TABLES`. A projection that included it would let a graph
  walk surface a journal node, which is exactly the property the journal design forbids.
* **target / signal routing** — fan-out is ephemeral. There is no `target_id` on `signals` and
  no per-target delivery row; a signal is routed, never copied.

**Measured arc population, 2026-09-21:** ≈ **4.0 million arcs** — 2,947,956 from
`analyst_outputs.derived_from` alone, 848,436 mentions, 45,613 entity edges, 48,076 consumption
arcs, 42,008 supersessions, 22,969 bearing edges, 15,471 aliases, 14,657 situation members,
1,374 merges, 44 echoes.

Two consequences fall straight out of that number and both shape the design:

1. **74 % of the cross-layer graph is output lineage, not world state.** This is the
   `edge_family` lesson (Decision 3) arriving in a second domain: 86 % of the signed edge set
   was imported Wikidata IGO membership, and `structural_balance` was reporting UN co-membership
   as world alignment. A world-graph query that defaults to "all arcs" would here be reporting
   *who cited whom* as the state of the world. **`plane ∈ {world, evidence, lineage}` is
   therefore NOT NULL, and `world` is the default filter on every world-facing reader.**
2. **The projection materially advances trigger E4.** E4 fires when a full in-process snapshot
   rebuild exceeds 60 s; the AGE probe measured the networkx rebuild at ~8 µs/edge (914 ms at
   100 k, 7,701 ms at 1 M), putting the threshold at ≈7.8 M edges. At 4.0 M arcs a whole-graph
   in-process snapshot is ≈32 s — **about 51 % of the way to E4 today**, before events. This is
   the single strongest argument against making the in-process snapshot the projection target,
   and it is why §4.3 does not.

### 4.2 The projection contract

**The projection target is a relational table, `graph_arcs`, rebuilt whole.**

```sql
-- 0207_graph_arcs.sql
CREATE TABLE graph_arcs (
    from_kind text NOT NULL,   -- signal|entity|fact|nexus|event|situation|finding|hypothesis|narrative
    from_id   uuid NOT NULL,
    to_kind   text NOT NULL,
    to_id     uuid NOT NULL,
    arc_type  text NOT NULL,
    plane     text NOT NULL CHECK (plane IN ('world','evidence','lineage')),
    family    text,            -- entity_edges.edge_family, event_edges.edge_type, …
    polarity  smallint NOT NULL DEFAULT 0 CHECK (polarity IN (-1,0,1)),
    confidence real,
    valid_from  timestamptz,
    valid_until timestamptz,   -- so a per-hop temporal predicate is EXPRESSIBLE
    src_table text NOT NULL,   -- the authoritative row this arc was read from
    src_id    uuid,
    PRIMARY KEY (from_kind, from_id, to_kind, to_id, arc_type)
);
CREATE INDEX idx_graph_arcs_out ON graph_arcs (from_kind, from_id, plane)
    WHERE valid_until IS NULL;
CREATE INDEX idx_graph_arcs_in  ON graph_arcs (to_kind, to_id, plane)
    WHERE valid_until IS NULL;
CREATE INDEX idx_graph_arcs_world ON graph_arcs (from_id, to_id)
    WHERE plane = 'world' AND valid_until IS NULL;
-- No foreign keys, deliberately: a projection is disposable, and it is rebuilt from
-- tables that DO carry them. This is the one place the judge's Decision 1 does not
-- apply, precisely because nothing is authoritative here.
CREATE TABLE graph_arcs_meta (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),   -- singleton
    projected_at timestamptz NOT NULL,
    arc_count bigint NOT NULL,
    source_counts jsonb NOT NULL DEFAULT '{}',
    build_seconds real NOT NULL
);
```

**Six contract rules.**

1. **Authoritative store → projection, never the reverse.** Nothing reads `graph_arcs` to write
   a substrate row. A test asserts no `INSERT`/`UPDATE` on any substrate table appears in the
   projection module or its readers.
2. **Whole rebuild only. There is no incremental projector and there never will be.** This is
   the strongest unrebutted argument in the graph debate (§3.2, B): *"every dead mirror in this
   codebase is an **incremental** mirror with a delete path that was never written; a structure
   only ever built by `SELECT * FROM …` cannot diverge in content, only in age, and age is one
   scalar."* Six mirrors, six silent divergences — AGE, Qdrant, OpenSearch, semantic dedupe,
   freshness, the corpus. The builder is `INSERT INTO graph_arcs_new SELECT …` once per source,
   then one `ALTER TABLE … RENAME` swap inside a transaction.
3. **Rebuild-from-scratch must always work, and a test proves it.** Build twice from the same
   substrate snapshot and assert the two tables are row-identical. A projection you cannot
   rebuild is a store.
4. **Staleness is published and fails loud.** `graph_arcs_meta.projected_at` is returned on
   every read; a reader past `LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS` refuses with
   `projection_stale` rather than answering. An empty projection answers `projection_empty`,
   never `found=False` — the exact defect the judge filed as P0 item 2 (`/graph/path` returning
   a confident "no path" from a 27-row fixture graph).
5. **Kill switch.** `LEGBA_GRAPH_PROJECTION`, default **off**. Off means the table is not built,
   the builder does not run, and every reader answers `projection_disabled`. Dropping the
   feature entirely is `DROP TABLE graph_arcs` plus a flag — no substrate row changes.
6. **Registered as an S-1 closed loop** on commit one: expected-vs-actual arc counts per source
   table, build seconds, staleness. The judge's step 9, applied here.

**The in-process snapshot keeps its current job and does not grow into this one.**
`graph_mining` (`_MAX_NODES = 5,000`) and `structural_balance` (`_MAX_NODES = 1,500`) build
*scoped* networkx subgraphs for algorithms. Under v3 they build them from
`graph_arcs WHERE plane='world'` instead of from `nexuses`, which fixes the family blindness
and keeps the node caps. A whole-graph 4 M-arc networkx object would need gigabytes in a 4 GB
runtime container and would put E4 within sight for no measured demand.

### 4.3 Engine: the August decision stands, on better evidence than it had

**The engine question stays deferred to the pre-registered sitting on 2026-11-03, read off
gauges E1–E5, two of which must fire.** Nothing in v3 requires an engine: `graph_arcs` is
queried by recursive CTE over ordinary btrees, which is the only executor in the whole AGE
probe that never timed out at any scale.

What v3 changes about that sitting is the ladder underneath it.

### 4.4 The trigger gauges — what can be measured today, and what cannot

| trigger | threshold | reading today | gauge status |
|---|---|---|---|
| **E1** open `edge_family='relation'` edges | > 250,000 | **16,860** (all families 45,613) | **computable now**, one `SELECT` |
| **E2** p95 latency of shipped graph tools over ≥100 real invocations in 30 days | > 2,000 ms | **not computable** | **BLOCKED.** `action_pack_invocations` columns are `id, pack_id, pack_version, tool_name, budget_account, requested_by, tenant_id, cost_usd, units, outcome, job_id, occurred_at` — **there is no duration column.** E2 needs `duration_ms` added (a migration + one write-site change in `agency.run_pack_tool`), or it cannot be read at the sitting. |
| **E3** shapes the typed SQL builder cannot express, ≥20 invocations/day for 14 days AND ≥20 % such shapes | 280 invocations/fortnight | **lifetime** `query_paths` 4 · `find_proxy_chains` 1 · `query_brokers` 1 = **6**. For scale: `list_findings` 239 · `list_situations` 87 · `get_timeline` 30 · `query_nexuses` 13 · `query_facts` 13 · `inspect_entity` 8 | countable now from `action_pack_invocations`; the *shape* classifier does not exist |
| **E4** full in-process snapshot rebuild | > 60 s | ≈**32 s** at 4.0 M arcs, extrapolating the probe's measured ~8 µs/edge — **not measured directly** | needs a timed rebuild receipt; trivially added to the projector |
| **E5** attribute-rich centrality/community required interactive (<2 s) | any product commitment | cadence today; `graph_mining` emits `modularity: NULL` | a product decision, not a meter |

**Two things this table says plainly.** First, **E2 cannot be read on 2026-11-03 as the
instrumentation stands** — the gauge the judge assumed exists does not. Second, **E4 is much
closer than the judge's arithmetic suggested**, because the judge measured `entity_edges` and
v3's projection is the whole cross-layer graph. Both belong in the phase plan (P4a) and both
are cheap.

The demand picture is otherwise unchanged from the probe's §5.3: every graph-walk tool is still
in single digits over its entire lifetime, and the probe's own adjudication of that holds — the
tools that see most use (`query_facts`, `query_nexuses`) do not touch AGE at all, so disuse is
not a story about a bad instrument. It is a story about nobody asking the question yet. v3's
answer to that is §6 and the inquiry kind, not an engine.

### 4.5 AGE — the probe was run, and it failed

**I disagree with the brief on this point and the record is the reason.** The design the
operator agreed proposes *"Apache AGE (installed; a 90-day probe with the two mirrors turned
on + Event/Situation/Output labels + a kill switch)"*. That probe has already been taken.

[`AGE_PROBE_REPORT.md`](history/AGE_PROBE_REPORT.md), 2026-08-03, against the exact pinned digest the
live substrate runs (PostgreSQL 18.1 / AGE 1.7.0), six configurations spanning a 200× range of
effort, at 100 k and 1 M edges. Its §5.1 findings, all of which bear directly on a cross-layer
projection:

1. **Open-ended ego expansion does not move.** `(a)-[*1..k]-(b)` with one endpoint anchored —
   *the* graph-viewer verb — stayed at ~180-210 ms / ~4.8-5.4 s / ~33-38 s for 1/2/3 hops across
   `default`, `tuned`, `tuned_propidx`, `jit_off`, `parallel8` and `workmem256`. The cause is
   structural: to bind an unbound terminal vertex, AGE cross-joins the expansion against the
   **entire vertex label table** — nine neighbours found by removing 449,991 rows from a
   Cartesian product. At 1 M edges 2- and 3-hop ego **time out past 60 s**.
2. **Pattern matching — the capability AGE was supposed to win — timed out in every arm at
   both scales.** The unstable-signed-triad query was the debate's example of a shape "the typed
   SQL builder cannot express"; the relational twin answers it in ~10-12 ms, in twelve lines of
   SQL.
3. **Per-hop predicates over a variable-length path are not expressible at all.** `ALL(...)`
   and `ANY(...)` are unimplemented in AGE 1.7 (§3.6): `MATCH (a)-[r*1..2]-(b) WHERE
   ALL(x IN r WHERE x.is_open = 1)` is a **parse error**. There is no phrasing that says
   "traverse only edges that were open on date D". The probe found this by accident, when the
   two executors disagreed: AGE returned 3,861 vertices against the twin's 2,671 because it was
   walking closed edges.

Point 3 alone disqualifies AGE for v3. **Every arc in `graph_arcs` carries `valid_from` /
`valid_until`, and §3 is an entire section about asking dated questions.** A projection into
AGE would have to either drop closed arcs at projection time — losing the history that is the
point — or answer temporal questions over arcs that were already superseded. The report names
the second for what it is: *"the exact class of silent wrongness this remediation programme
exists to remove."*

And the report's own §5.4 pre-empts the proposal in terms:

> *"`JUDGE_SYNTHESIS` §4.2 pre-registered a ladder: 'E3 or E5 fires alone ⇒ **AGE probe
> first**, not Neo4j'… **That probe has now been run, early, and it came back negative.** …
> So if E3 or E5 fires later, the ladder should not route to an AGE probe; it has already been
> taken, and the answer was no."*

**Recommendation: keep the judge's freeze, and retire AGE as a projection candidate.**

* The two write-legs stay **off** and are not re-armed. `LEGBA_AGE_DERIVED_FROM` (read at
  `runtime/dapr_actors.py:800`, default off) and the descriptor field `emit_graph_edges`
  (declared `fact_extractor.py:1085`, `false` in every live source descriptor) are left exactly
  as they are.
* The extension stays installed. Dropping it means swapping the substrate image
  (`apache/age` → `postgres:18`) — a recreate of the store holding all the truth, for no
  benefit. The graph stays empty and errors honestly.
* The `.id` / `{name}` contract mismatch stays unfixed and stays recorded, as the judge ruled:
  it is dormant while the graph is empty, and fixing it invites populating it.
* The `Event` vertex label and the `InvolvedIn` / `PartOf` / `PartyTo` / `Targets` /
  `ConductedVia` edge labels sitting in `legba_graph` today are v1 residue. They stay declared
  and empty. v3 does not write them.
* **If the operator wants the AGE probe run anyway**, §8 Q6 states the smallest honest version:
  it is a re-run of a measurement whose result is on file, and the thing that would change the
  answer is an AGE release implementing `ALL()`/`ANY()` — upstream has published nothing since
  the pinned 1.7.0 digest (the probe noted `apache/age:latest` still resolved to the same
  digest five months on).

### 4.6 Kùzu — archived upstream; not a candidate

The judge's §2 #28 recorded advocate C's claim that Kùzu was archived as *"provisionally ✔ —
operator should re-check before any embedded plan"*, having declined to fetch upstream under a
read-only mandate. Re-checked 2026-09-21:

* **Licence: MIT.**
* **The GitHub repository was archived on 2025-10-10**, the same day v0.11.3 shipped as a final
  release. The reason surfaced in February 2026 via an EU Digital Markets Act filing: Apple had
  agreed on 2025-10-09 to acquire Kùzu Inc. Upstream is **read-only** — released versions keep
  working and 0.11.3 bundles the previously server-downloaded extensions, but there will be no
  further fixes or features from the original authors.
* Because the code is MIT-licensed, community forks exist (**LadybugDB**, **Bighorn**). They are
  young and unproven.

**Recommendation: no.** For a one-operator, self-hosted, AGPLv3 stack with commercial
dual-licence intent, an abandoned upstream is the same class of risk the judge named as *"the
largest uncompensated supply-chain risk in the stack"* when it was an unpinned `:latest` tag —
except unfixable rather than merely unpinned. Kùzu's genuine advantages (embedded, no daemon,
columnar, Cypher) are all things `graph_arcs` supplies without a dependency. If the 2026-11-03
sitting ever wants an embedded engine, a fork's maintenance record will be a year older and
readable then; it is not readable now.

*(Source: web search 2026-09-21 — archival date, the DMA filing and the fork names are reported
by secondary coverage, not read off the repository. Marked **PARTIALLY VERIFIED**: the MIT
licence and the archived status are consistent across every source found; the acquisition
detail is single-thread reporting.)*

### 4.7 Neo4j is excluded — and that changes the pre-registered ladder

The operator excludes Neo4j. That is a licence-and-principle decision and this document does
not argue it. It does have a consequence that must be written down, because the August ladder
routed to it:

> §4.2: *"**E1 or E4 fires (scale) ⇒ dedicated engine: Neo4j Community Edition 5.x over Bolt**…
> Fallback if the operator rules GPLv3 out on principle: **NebulaGraph** (Apache-2.0), accepting
> three daemon classes and nGQL."*

So with Neo4j excluded, **the scale branch of the ladder now terminates at NebulaGraph**, and
the pre-registered disqualifications stand: **Memgraph** — BSL 1.1 until 2030-07-15, whose
Additional Use Grant restricts exactly Legba's self-hostable-compose distribution model;
**FalkorDB** — SSPL. Two more, evaluated here because the brief asked and no reason was found
to pursue either:

| candidate | licence | why not |
|---|---|---|
| **NebulaGraph** | Apache-2.0 | The surviving scale fallback. Three daemon classes (meta / graph / storage) and nGQL rather than Cypher. A fifth, sixth and seventh stateful process for one operator whose box already runs Postgres, NATS, Qdrant, OpenSearch and Redis. Viable only if E1 or E4 actually fires. |
| **JanusGraph** | Apache-2.0 | Requires a storage backend (Cassandra / HBase / BerkeleyDB) **and** an index backend (Elasticsearch / Solr). Two more stateful services before the graph itself. Rejected on ops, not capability. |
| **Kùzu** | MIT | §4.6 — upstream archived. |
| **Memgraph** | BSL 1.1 → 2030-07-15 | Pre-registered disqualification (distribution model). |
| **FalkorDB** | SSPL | Pre-registered disqualification. |
| **Apache AGE** | Apache-2.0 | §4.5 — probe run, negative, and cannot express per-hop temporal predicates. |
| **`graph_arcs` + recursive CTE** | — | The recommendation. No new licence, no new process, no new backup leg; per-hop temporal predicates are ordinary SQL; and it is the only executor in the probe that never timed out. |
---

## 5 · Typing throughput

§1.3 of the judgment remains the binding constraint on any world graph: *"the binding
constraint on 'graph the world state, fully queryable' is LLM typing throughput, not storage,
not key type, and not the traversal engine."* v3 does not repeal it.

### 5.1 The experiment the brief asks for is already applied

The design brief asks the plan to "include the reifier cap/cadence experiment under
measurement". **That experiment was run on 2026-08-03 and its recommendations are live.**
Verified against the `analyst_descriptors` head, 2026-09-21:

| bake-off recommendation (§7.1-7.4) | status |
|---|---|
| fix `_read_candidates` — `status='pending'`, keeper-aware bidirectional dedup guard | **applied**, `src/legba/data/analysts/reifier_selection.py` (the two-stage score-then-fetch, status predicate re-asserted at `:157`) |
| batch at N = 12 | **applied**, live `batch_size: 12` |
| raise `max_candidates` | **applied**, live `max_candidates: 600` per run × `45 */12 * * *` = **1,200 candidates/day** (the code default `MAX_CANDIDATES_PER_RUN = 40` is overridden by the descriptor) |
| apply the bar as the ordering | **applied**, live `qualification_bar: 0.42`, `min_independent_sources: 2` |
| single typer, `core120b`, no escalation ladder | **applied**, `llm.primary.openai_compat`, `temperature: 1.0`, `budget_tokens_per_day: 0` |
| send the hand-check worksheet back before any model swap | **outstanding — operator's** (`docs/data/kg2_bakeoff/handcheck_worksheet.csv`) |

### 5.2 The gap, and the instrument that already measured it

| | bake-off projection (§7.5) | realised, 7 days to 2026-09-21 |
|---|---:|---:|
| candidates offered/day | 1,200 | 1,200 (configured) |
| **new typed (`relation`) edges/day** | **~205 steady state** | **~68** (476 `entity_edges` rows in 7 days) |
| USD/day | $0 | $0 |

Realised production is ~33 % of projection. **"Raise the cap" cannot be the answer — the cap
is already 15× what the judge measured against, and the projection was computed at this cap.**

**And the diagnosis does not need new instrumentation, because the handler already writes the
funnel to its receipt** (`relationship_reifier.py:1441`, persisted in `analyst_traces.output_payload`).
The last 14 runs (09-14 13:05Z → 09-21 01:01Z), read 2026-09-21:

| run (UTC) | `selection_examined` | `already_reified` | `eligible` | `accepted` | `rejected` | `written` |
|---|---:|---:|---:|---:|---:|---:|
| 09-21 01:01 | 1800 | 1356 | 441 | **31** | 398 | 31 |
| 09-20 13:05 | 1800 | 1360 | 437 | 28 | 399 | 28 |
| 09-20 01:04 | 1800 | 1351 | 446 | 29 | 405 | 29 |
| 09-19 13:04 | 1800 | 1352 | 445 | 30 | 403 | 30 |
| 09-19 01:06 | 1800 | 1334 | 463 | 40 | 411 | 40 |
| 09-18 13:08 | 1800 | 1336 | 461 | 39 | 408 | 39 |
| 09-18 01:12 | 1800 | 1335 | 462 | 41 | 411 | 41 |
| 09-17 13:10 | 1800 | 1327 | 470 | 39 | 420 | 39 |
| 09-17 01:08 | 1800 | 1317 | 480 | 54 | 415 | 54 |
| 09-16 13:05 | 1800 | 1316 | 481 | 52 | 418 | 52 |
| 09-16 01:03 | 1800 | 1325 | 472 | 46 | 412 | 46 |
| 09-15 13:04 | 1800 | 1331 | 466 | 51 | 402 | 51 |
| 09-15 01:03 | 1800 | 1328 | 469 | 42 | 417 | 42 |
| 09-14 13:05 | 1800 | 1320 | 477 | 55 | 410 | 55 |

**Five readings, in order of size.**

1. **The accept rate is 7–11.5 %, not the 46.8 % the projection assumed.** 31/441 on the newest
   run; 55/477 on the oldest shown. This is the dominant term by a wide margin: at 46.8 % the
   *existing* eligible supply would already produce ~206 edges/day — the projection, exactly.
   **The throughput problem is the typer's verdict distribution, not the cap, not the cadence
   and not the queue.**
2. **The run never fills its budget.** `eligible` is 437–481 against a `max_candidates` of 600,
   on every run. Raising the cap again buys nothing; there is no 600th candidate to give it.
3. **The scan is the supply constraint, and the code already says so, every run, unread.**
   `examine = min(MAX_EXAMINE 8000, max_candidates 600 × EXAMINE_MULTIPLIER 3) = 1800`
   (`reifier_selection.py:93,98,327`). `selection_qualified == selection_examined == 1800` on
   all 14 runs, and the counter's own docstring (`:199`) reads *"Read this against `examined`.
   Equal ⇒ the scan's LIMIT was the binding"* constraint. It has been binding on every run and
   nothing surfaces it. ~74 % of what the scan does see (1,317–1,360 of 1,800) is
   `already_reified` — correct behaviour, and it means the head of the queue is increasingly
   covered ground.
4. **There is no write-path loss.** `written == accepted` on all 14 runs. The junk /
   vague-endpoint / self-edge gates are not eating production.
5. **A fold, second-order but real.** 577 nexus writes over the 6.5 days shown against 488
   `entity_edges` rows written by the reifier in 7 days — roughly 15–20 % of writes fold into
   an existing open edge (`observed_count + 1`) rather than inserting. That is the upsert
   working, not a defect, and it is not currently counted as its own line.

And a trend: `accepted` fell 55 → 31 across 6.5 days while `already_reified` rose 1,320 → 1,356.
Coverage is saturating the head of a scan that is too shallow to reach past it.

### 5.3 So the experiment is one decisive comparison, not a measurement phase

The funnel is already published. What is *not* known is **why the live accept rate is a fifth
of the bake-off's**, and there is exactly one experiment that separates the two possibilities:

> **Re-run the bake-off harness on 200 candidates drawn from today's pool, same procedure,
> same seed discipline, and compare its accept rate to production's 7–11 %.**
>
> * Harness accepts ~47 % on today's pool ⇒ the pool is fine and **the production path is the
>   defect** — the batched prompt, the N=12 rendering, or a model/route drift between the
>   harness and `llm.primary.openai_compat` as the handler calls it. That is a code fix worth
>   ~3× the graph's growth rate.
> * Harness also accepts ~10 % ⇒ **the pool has changed** since August (320,161 pending today
>   against 174,632 then), the bar is admitting a thinner population, and the lever is the bar
>   — re-swept against `docs/data/kg2_bakeoff/pool_summary.json`'s 11-setting curve, on today's
>   pool.

The pipeline is in the tree and is reproducible: `scripts/kg2_pool_measure.py` →
`kg2_sample_prep.py` → `kg2_typing_bakeoff.py` → `kg2_bakeoff_score.py`, with
`SAMPLE_SEED = 20260803` so an unchanged pool reproduces `sample_candidates.csv` exactly.
Artefacts to reuse rather than regenerate: `agreement.json` (pairwise κ, accept behaviour by
qualification stratum), `economics.json` (tokens/edge, wall/edge, parse-failure rate),
`batch_size_sweep.json` (the N ∈ {1,6,12,24,40} sweep), `worksheet.csv` (200 candidates ×
4 models, side by side).

**Two cheap changes ride along regardless of the outcome**, because both are readings the
platform is already producing and throwing away:

* **surface the binding-scan signal** — when `selection_qualified == selection_examined`, say
  so on the receipt as a named flag rather than leaving it to be inferred from two integers;
* **count the fold** — split `written` into `inserted` / `folded` using the
  `RETURNING (xmax = 0) AS inserted` idiom `situation_clustering.py:846` already uses.

**Out of scope: the model.** The bake-off's §7.1 ladder ruled `core120b` primary and rejected
`slm8b` outright, and its §6.2 found **no usable difficulty signal** — not qualification score,
not evidence length, not source count — so a two-tier ladder would route by coin-flip and pay
for the privilege. A model change is gated on the operator's hand-check labels, which do not
exist yet (§8 Q6).

### 5.4 What events do and do not do to this constraint

**Events reduce the need.** Relations between events and actors are fewer and more meaningful
than entity co-mentions. An occurrence with four participants produces four
`event_entity_links` rows carrying roles; the same four entities co-mentioned produce six
undirected pairs carrying nothing, each of which must then be *typed by an LLM* to mean
anything. **25,147 of the 45,613 rows in `entity_edges` — 55.1 % — are the single untyped
predicate `co occurs with`**, and a large share of those are pairs that co-occurred because
they were in one story about one occurrence. Expressed as shared event membership they need no
typing call at all. The same logic applies upstream: the 320,161 permanently-pending
`proposed_edges` are 100 % `co_occurs`, and the reifier's own funnel says ~74 % of what it
scans is ground it has already covered.

**Events do not remove the need, and it would be dishonest to imply otherwise.** An event is an
*occurrence*; a standing entity↔entity relation is a *state* that persists between occurrences,
and only the typer produces those. The live typed population is mostly states:
`member of` 5,312 · `located in` 3,735 · `part of` 1,563 · `hostile to` 983 · `employed by` 889
· `allied with` 492. No amount of event modelling derives "Turkey is a NATO member" from a
co-mention.

**No quantitative claim is made about the size of the reduction.** The share of today's
co-occurrence edges better modelled as event co-participation is *measurable* once P1 has
produced events — it is a P5 output (`% of open cooccurrence edges whose endpoint pair shares
≥1 event`), not a P5 assumption. Anyone quoting a number for it before that measurement is
guessing.

## 6 · Interfaces

### 6.1 Substrate tools

The `substrate_read` pack ships **19** tools today (the model-visible name is the Python method
name; there is no renaming layer). v3 changes seven and adds three.

**Changed — one optional parameter each, defaulting to `None`:**

```python
# src/legba/runtime/substrate_query_port.py
async def query_facts(self, *, subject=None, predicate=None, value=None,
                      limit=30, as_of: str | None = None) -> dict[str, Any]
async def query_nexuses(self, *, subject=None, obj=None, rel_type=None,
                        polarity=None, limit=30, as_of: str | None = None) -> ...
async def query_paths(self, *, subject, obj, max_hops=3, polarity_product=None,
                      limit=30, families=None, as_of: str | None = None) -> ...
async def find_proxy_chains(self, *, ... , as_of: str | None = None) -> ...
async def query_brokers(self, *, camp_a, camp_b, max_hops=3, limit=50,
                        families=None, as_of: str | None = None) -> ...
async def list_situations(self, *, status=None, target_id=None, since_hours=None,
                          limit=20, as_of: str | None = None) -> ...
async def get_timeline(self, *, subject, limit=40,
                       since: str | None = None, until: str | None = None) -> ...
async def list_findings(self, *, target_id=None, analyst_id=None, severity=None,
                        since_hours=None, include_superseded=False, limit=20,
                        believed_as_of: str | None = None) -> ...
```

`query_paths` / `find_proxy_chains` / `query_brokers` apply the predicate **on both the seed
and the recursive arm** of `_PATH_WALK_SQL` (`:318`) and `_BROKER_WALK_SQL` (`:363`) — a
per-hop temporal predicate, which is ordinary SQL here and is the thing AGE cannot express at
all (§4.5).

**Added:**

```python
async def query_events(self, *, target_id=None, geo=None, category=None,
                       lifecycle_state=None, entity=None, since=None, until=None,
                       as_of=None, limit=20) -> dict[str, Any]
async def inspect_event(self, *, event_id: str) -> dict[str, Any]
    # the event, its ranked signals, its actors with roles, its event edges,
    # the situations tracking it, and its lifecycle ledger
async def belief_as_of(self, *, as_of: str, target_id=None,
                       fold_verdicts: str = "as_of", limit=20) -> dict[str, Any]
```

**One tool parameter touches five places, and they are only kept in step by convention —
`tool_rounds.py`'s own comment says so.** Any phase that adds one must land all five:

1. the port method — `src/legba/runtime/substrate_query_port.py`
2. the Protocol stub — `src/legba/data/analysts/consult_on_demand.py` (`SubstrateQueryPort`,
   methods at `:333-545`). **Note the drift already present:** `families` is on the three
   graph-walk implementations and missing from their Protocol stubs (`:421/:431/:441`), and
   nothing catches it because `ToolContext.substrate` is `Any`
   (`agency/tools.py:133`) and the implementation does not inherit the Protocol.
3. the argument coercion — `src/legba/data/analysts/agency/substrate_read.py::_call_port`
   (`:102`), the `if/elif` that turns untyped LLM JSON into kwargs
4. the JSON schema — `src/legba/data/stack/llm/tool_rounds.py::TOOL_SCHEMAS` (`:243`)
5. every prose catalogue that names the tool — `analysts/gather_surface.py:75`
   (`_GATHER_SYSTEM_SUFFIX`, the desk/GATHER list), `analysts/consult_on_demand.py:583`
   (`_SYSTEM_PROMPT`), `analysts/journal_assessor.py:2282` (`_JOURNAL_READ_TOOL_SCHEMAS`)

plus the pack descriptor's `tools:` list (`descriptors/action_pack_substrate_read.yaml`, one
entry per name) re-registered through `scripts/bringup_register_action_packs.py`.

**Two incidental defects the temporal phase should fix while it is in there** (both found
2026-09-21, neither caused by v3):

* **Five pack tools have no JSON schema** — `query_paths`, `find_proxy_chains`, `query_brokers`,
  `list_targets`, `list_sources` fall through to `_fallback_spec` (`tool_rounds.py:454`). Because
  `is_write_tool()` is `return name not in TOOL_SCHEMAS or name in WRITE_TOOLS` (`:444`), **all
  five are classified as writes and serialized**. Three read-only graph walks are running on
  the serial write path.
* **The graph walks are consult-only.** `_GATHER_READ_TOOLS` (`inline_target.py:2593`) omits
  them, so no desk can reach them. That is a large part of why E3's demand gauge reads 6
  lifetime invocations.

**The module-size gate binds here and the plan must respect it.**
`substrate_query_port.py` is **3,761 lines against a pinned ceiling of 3,910**
(`tests/test_module_size_gate.py:266`) — **149 lines of headroom**. Adding `as_of` across six
methods with docstrings will breach it. The gate's own instruction is to **extract, never
raise**; the natural seam is a leaf module `src/legba/runtime/substrate_temporal.py` holding
the predicate builders and the ISO-8601 parsing, with the three graph-walk tools as a second
extraction if more room is needed.

### 6.2 Registry routes

All under `src/legba/data/registry/`, one leaf `*_api.py` per router, built by
`build_<name>_router(deps) -> APIRouter` and mounted in `server.py` with the import **inside**
the factory block (the slim-image discipline: a route module must not pull
`legba.data.analysts`).

| route | module | notes |
|---|---|---|
| `GET /api/v1/v3/events` | `events_api.py` (new) | paged; filters mirror `query_events`; default page 50 / max 500, the house page contract |
| `GET /api/v1/v3/events/{event_id}` | same | the `inspect_event` shape |
| `GET /api/v1/v3/events/{event_id}/lifecycle` | same | the append-only ledger, `situation_trajectory_api.py` as the template |
| `GET /api/v1/v3/belief` | `belief_api.py` (new) | `as_of` + `fold_verdicts`; stamps which fold was used |
| `GET /api/v1/v3/graph/arcs` | `graph_arcs_api.py` (new) | plane-filtered ego / bounded walk over the projection, with `as_of`; refuses `projection_disabled` / `projection_stale` / `projection_empty` |
| `GET /api/v1/v3/graph-triggers` | `graph_triggers_api.py` (new) | the E1–E5 readout for the 2026-11-03 sitting |

`graph_walk_api.py` already carries the parameter shape to copy for dates:
`since: datetime | None = Query(default=None, ...)` / `until: ...` (`:644-649`).

### 6.3 UI surfaces

| surface | files | change |
|---|---|---|
| **World map** | `legba-ui-v3/src/v4/world/mapData.ts`, `src/panels/v4/MapPanel.tsx`, `src/lib/mapLayers.ts` | an **events layer**. This is the map's biggest win: `centroidOf(countries)` currently backfills geo for findings and situations, and a region / lane / flow desk carries `scope.geo = []` so it never places at all. An event carries `geo` + `geo_lat/lon` directly. |
| **Timeline** | `src/panels/merged/Timeline.tsx`, `src/panels/target/Timeline.tsx`, `src/lib/timelinePoints.ts`, `src/lib/timelineWindows.ts` | events as spans (`time_start` → `time_end`) rather than points; the lifecycle state as the badge |
| **Situations** | `src/panels/target/Situations.tsx` | the tracked-events list per situation |
| **Graph** | the existing graph panel | reads `/graph/arcs` with `plane=world` default instead of the candidate pool |
| **Journal** | `journal_read` pack | optional `get_events`; the journal narrates, it does not write |

A new panel is a row in `ui_panel_registrations`, the registration the 2026-09-20 matrix commit
(56 → 57) exercised.

### 6.4 Descriptors

| descriptor | family | notes |
|---|---|---|
| `analyst_event_clustering.yaml` | analyst, **new kind** | the ONE event writer: clusters the slice, upserts events, recomputes lifecycle for every open event, writes ledger rows on transition — `situation_clustering`'s shape. `state: draft` until the new-kind activation checklist passes. `budget_tokens_per_day: 0`, `temperature: 1.0`. |
| `analyst_event_reconciler.yaml` | analyst, new kind | deterministic `event_edges` only — `correlated_with` between two producers' rows on one signature, `evolves_from` between temporally adjacent events sharing ≥K actors. **`caused_by` and `contradicts` are NOT produced** (§8 Q7, SEAMS #56). |
| `analyst_graph_projector.yaml` | analyst, new kind | the whole-rebuild projector + the S-1 closed loop |
| `action_pack_substrate_read.yaml` | action_pack | `+ query_events`, `+ inspect_event`, `+ belief_as_of` in `tools:` |
| `analyst_relationship_reifier.yaml` | analyst | **P5 stage 1 changes nothing here** — counters only, in code |

---

## 7 · Migration and compatibility

### 7.1 Numbering

v3 opened at a head of **0201** (`0201_fact_contention_input_fingerprint.sql`); **0198 and
0199 do not exist**. The runner has no manifest and no head constant —
`migrate.py:_discover()` globs `*.sql` and sorts, applying each file in its own transaction and
recording it by filename — so gaps are harmless and "head" means the lexicographically-last
file present. v3 starts at **0202**.

| number | file | phase | contents |
|---|---|---|---|
| 0202 | `0202_events.sql` | P0 | `events`, `signal_event_links`, `event_entity_links` + indexes |
| 0203 | `0203_event_edges.sql` | P0 | `event_edges`, `situation_event_links` |
| 0204 | `0204_event_lifecycle_events.sql` | P0 | the ledger + the DELETE/UPDATE-forbidding triggers (0184's contract) |
| 0205 | `0205_backfill_events_from_tower.sql` | P0 | §7.2 |
| 0206 | `0206_entity_edge_events.sql` | P3 | the edge transition ledger (§3.5) |
| 0207 | `0207_graph_arcs.sql` | P4 | `graph_arcs` + `graph_arcs_meta` |
| 0208 | `0208_invocation_duration.sql` | P4 | `action_pack_invocations.duration_ms int` — the E2 gauge (§4.4). Additive, nullable. |
| 0209, 0210 | *(reserved, spare)* | — | so a parallel branch does not collide |

**On the out-of-order pair.** P4a writes **0208** and P4b writes **0207**, so
0208 is applied before 0207 exists. That is safe here and the reason is worth stating rather
than assuming: the runner has no manifest and records each file by **filename** after applying
it in its own transaction, so it simply applies 0207 when it arrives; and the two files are
independent — `graph_arcs` does not reference `action_pack_invocations.duration_ms` and vice
versa. The judge's Decision 7 warning (writing `0123` against an in-flight train) was about a
*dependent* ordering, which this is not. If a later change makes them dependent, renumber before
merging, not after.

### 7.2 Backfill strategy — bounded, dry-run first, refuses rather than floods

**Path 1 — `situation_events` (measured 2026-09-21).** 3,214 rows: `unchanged_checkpoint`
1,778 · `escalates` 1,053 · `broadens` 300 · `de_escalates` 83. Evidence-bearing rows
(`delta <> 'unchanged_checkpoint' AND derived_from <> '{}'`) = **1,436**, and the DB CHECK
guarantees every one carries evidence. Each is one high-quality event candidate: a dated
movement of a watched thread with a `why` and verified findings behind it. **Expected yield
≤1,436 events, fewer after signature collapse.**

**Path 2 — verified findings.** Bounded hard, because the raw population is not a candidate
set: **42,765** findings in the last 90 days carry ≥2 `derived_from`, and **18,309** in the
last 30. A 1:1 backfill would mint tens of thousands of events, which is the mega-bucket
defect's mirror image — a flood instead of a blob. The bound:

* **30 days**, not 90;
* verify-passed at the house floor (`effective_confidence >= 0.50`);
* ≥2 signals that still resolve;
* collapsed by `event_signature`, so the yield is *distinct occurrences*, not findings;
* a hard ceiling `_BACKFILL_MAX_EVENTS` (proposed **5,000**) above which **the migration
  refuses and reports its counts rather than writing**.

**Dry-run first, and the number goes in the migration header.** 0144/0145/0180 all did this —
each header states the measured expectation (*"12,732 open nexuses → 12,236 resolvable
(96.10 %) → 11,935 landed + 301 collapsed + 496 parked"*) and the run is asserted against it.
The distinct-signature yield of path 2 is **not knowable until the signature function runs**, so
P0's acceptance is: run the dry-run, put the number in the header, then commit.

Unresolvable candidates are **parked, never dropped** — `entity_edges_unresolved` is the
precedent, and the same header discipline applies.

### 7.3 What stays byte-identical

With every v3 flag off (§7.5), the platform behaves exactly as it does today:

* no existing column, index or constraint is dropped or altered; every migration is additive
  (0208 adds one nullable column);
* no existing write path is re-pointed. `write_analyst_output` gains a kind; it loses nothing;
* the `as_of` / `since` / `until` / `believed_as_of` parameters default to `None`, and `None`
  runs today's SQL against today's plan. **This is P3's acceptance test:** the existing tool
  tests pass unmodified;
* no desk, composition, judge, grader or audit prompt changes;
* the reifier's live options are untouched by P5 stage 1.

**One honest exception.** `OutputKind.EVENT` exists in the enum whether or not anything writes
it, and a new `OutputKind` is a **shared-schema change**: the registry image must be rebuilt
first, or stale `/typed` 500s silently stop analysts. Behaviour is byte-identical; the schema
surface is not. The new-kind activation checklist (rebuild registry → grep the boot log for
`reconcile.failed` → confirm every kind still activates 200) is mandatory, exactly as
`descriptors/analyst_situation_tracker.yaml`'s header records for `situation_update`.

### 7.4 Rollback

| change | rollback |
|---|---|
| 0202–0204 (event tables) | `LEGBA_EVENTS=0` stops the writer; `DROP TABLE` in reverse dependency order removes them. No substrate row was touched. |
| 0205 (backfill) | `DELETE FROM events WHERE source_method='tower' AND created_at < <ts>` — cascades take the links. Idempotent, so a re-run is also a repair. |
| `OutputKind.EVENT` | not removable without a registry rebuild; harmless to leave (a declared kind nothing writes, like `ConsultResponsePayload` today) |
| event citations (P2) | `LEGBA_EVENT_CITATIONS=0` — the expansion stops, existing citations keep resolving because every expanded entry is an ordinary signal entry |
| `as_of` parameters (P3) | omit the parameter; or revert the port module. Behaviour with the parameter absent is unchanged by construction. |
| 0206 (edge ledger) | `LEGBA_EDGE_TRANSITION_LEDGER=0`, then `DROP TABLE entity_edge_events` |
| 0207 (projection) | `LEGBA_GRAPH_PROJECTION=0`, then `DROP TABLE graph_arcs, graph_arcs_meta`. This is the whole exit cost — the same "`DROP` plus a flag" reversibility argument the judge credited to advocate A, honoured here without the engine. |
| 0208 (`duration_ms`) | leave it; a nullable column costs nothing |
| P5 | stage 1 is counters only — revert the module |

### 7.5 Flags

| flag | default | gates |
|---|---|---|
| `LEGBA_EVENTS` | **off** | the event write path (clusterer, lifecycle, ledger) |
| `LEGBA_EVENT_CITATIONS` | **off** | the `event` ref-kind expansion at citation-build time |
| `LEGBA_EDGE_TRANSITION_LEDGER` | **off** | the `entity_edge_events` write inside `entity_edge_writes` |
| `LEGBA_GRAPH_PROJECTION` | **off** | the projector and every `graph_arcs` reader |
| `LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS` | 7200 | the staleness refusal |
| `LEGBA_EVENT_EXPANSION_MAX_SIGNALS` | 4 | the per-citation expansion cap |
| `LEGBA_AGE_DERIVED_FROM` | **off** (unchanged) | left exactly as it is; v3 does not arm it |
| `emit_graph_edges` (descriptor) | **false** (unchanged) | left exactly as it is |

No flag gates the `as_of` parameters: absence of the parameter *is* the off state, and a flag
would add a second way to be off.

---


### 7.6 `claim_contentions` — the contrary-evidence record (migrations 0221 + 0222)

One row per material claim the contrary-evidence pass contended: what was asked, on which rung,
what was fetched, and what the retrieval came to. It holds a **retrieval outcome and never a
verdict** — see ANALYSIS §10.6.1 for why that distinction is the design rather than a caveat.

| column | type | what it is |
|---|---|---|
| `claim_id` | `text` | the audit's own claim key — sha256 over the folded claim text and its byte-exact origin span. Joins `external_grades.claim_key`, so an audit grade and a contention over the same published sentence are one query. A content hash, not a uuid: stable across a replay a month later |
| `claim_text` | `text` | the claim as published |
| `finding_id` · `block_ordinal` | `uuid` · `int` | the read the claim was published **in**, and its ordinal inside that read. The reader-surface join |
| `origin_head_id` · `span_role` | `uuid` · `text` | the desk head the span was quoted **from**, and its role |
| `query` · `query_source` · `query_novel_tokens` | `text` · `text` · `int` | the counter-query, whether code or a model proposed it, and how many content words it carries that the claim does not. The last is the paraphrase gate's own number, on every row |
| `polarity_group` · `polarity_sign` | `text` · `smallint` | the position the claim took in the calibrated vocabulary, when it took one |
| `rung` | `text` | the rung that answered |
| `stance` | `text` | `contradicts` · `qualifies` · `none_found` · `search_failed`. The last two are not the same thing: "we looked and found nothing" and "we could not look" must never share a shape |
| `derivation` | `text` | `polarity` (calibrated; the only one a composition reads) · `negation` (uncalibrated fallback; human surfaces only) · `none` |
| `reason` | `text` | why a retrieval did not happen, from a closed set |
| `statement` | `text` | the hedged line a composition renders |
| `refs` | `jsonb` | the pages actually **fetched**: url, sha256, chars, the date the page states, whether extraction worked, when it was fetched, its status code, its own stance and the quoted sentence. A search snippet never becomes a ref |
| `ref_signal_ids` | `uuid[]` | corpus rows whose content hash already matched a fetched page. This pass **links and never creates**: its pages are selected adversarially and landing them in the corpus would move freshness, source health, salience and calibration in one direction, silently |
| `as_of` · `retrieved_at` · `expires_at` | `timestamptz` | the moment the row speaks about, when the pages were fetched, and when the next pass would have covered the claim. A reader is never left to guess which clock it holds |
| `receipt_id` | `uuid` | `analyst_traces.run_id` of the pass that wrote the row — the join to the receipt-hash chain. There is no other `receipt_id` convention in this tree and the column states its join rather than implying one |
| `host_class` | `text` | **F1** — what kind of host the decisive counter page came from: `reference` (encyclopedia / dictionary / glossary, which can never carry `contradicts`), the source-class taxonomy's own word for a registered source (`reporting` · `analysis` · `official` · `state_media`), or `unknown`, which passes. One vocabulary, not two |
| `page_published_at` | `date` | **F2** — the date the date gate parsed from the page's own machine-readable metadata or a dated URL. Never prose, never a masthead. NULL on a stance-bearing row means the page was **undated**, and an undated page cannot contradict |
| `subject_overlap` | `int` | **F3** — how many of the claim's subject tokens the matched sentence carried. The other half of `query_novel_tokens`: one says whether the query was about the claim, this says whether the page was |
| `independent_pages` | `int` | **F4** — how many independent outlets carried an admissible contradiction, by the composition floor's own independence count. `contradicts` needs ≥ 2; one admissible page is `qualifies` |

The four fence columns (0222) are **nullable and never backfilled**: a row written before them
measured none of the four, and NULL is *not measured* — not zero. The first live run's three
`contradicts` rows, all of them false, stay exactly as they are; the pipeline stamp moved with the
rules (`2026-09/7a.1` → `7a.2`) so a fenced row and an unfenced one can never be pooled. See
ANALYSIS §10.6.1 for the run that wrote the fences.

Five CHECKs carry the honesty rather than a convention: a stance that names evidence must carry it
(`refs` non-empty); only a stance-bearing row may carry a derivation; `host_class` stays inside the
closed vocabulary; the two fence counts are non-negative; and a `contradicts` row must name at least
two independent pages. The last three are `NOT VALID` — they grandfather the rows already in the
table while checking every future insert, and the F4 one is never validated because validating it
would mean deleting the evidence or inventing a number for it. The replay key is
`(claim_id, pipeline_version, as_of_day)` — one record per claim per pass version per UTC day, so a
re-run inside a day is a no-op and a later day appends honest history.

Five indexes, each for one read: the replay key; the claim drill; the per-desk route; the per-target
route (`scope` on `GET /v3/contentions` accepts either handle, as every other reader surface means
the target); and a **partial** index on `(claim_id, as_of DESC) WHERE stance = 'contradicts' AND
derivation = 'polarity'` for the composition's per-cycle question — 88 kB against 2.6 MB for the
full claim index at 20k rows, which is the whole argument for making it partial: the `none_found`
bulk is, by design, most of the table.

## 8 · Open questions for the operator

Each with a recommendation, as the house rule requires. None of these blocks P0.

**Q1 · The AGE probe: accept the 2026-08-03 verdict, or run it again?**
The agreed design asks for a 90-day AGE probe with the two mirrors armed. That probe has been
taken and came back negative (§4.5), and one of its findings — `ALL()`/`ANY()` are unimplemented
in AGE 1.7, so per-hop temporal predicates are inexpressible — is a *correctness* disqualifier
for a projection whose every arc carries `valid_from`/`valid_until`.
**Recommendation: accept the verdict. Do not arm the mirrors.** The only thing that would
change the answer is an AGE release implementing the list predicates, and checking that is
reading a changelog, not running a probe. *Alternative:* run it anyway — cost is a phase, and
the result is on file.

**Q2 · The projection target: `graph_arcs`, or wait for the sitting?**
**Recommendation: build `graph_arcs`** (§4.2). It needs no engine, no licence, no new process
and no backup leg; it makes per-hop temporal walks ordinary SQL; and it is what makes E3 and E5
*readable* at the 2026-11-03 sitting instead of "instrument first" for a second time.
*Alternative:* defer everything graph-shaped to the sitting — cheap today, but the sitting then
reads the same two blank gauges it was pre-registered to read.

**Q3 · Neo4j is excluded — what is the scale branch now?**
The August ladder routed E1/E4 to Neo4j CE with NebulaGraph as the GPLv3-refusal fallback
(§4.7). With Neo4j out, NebulaGraph is the branch: Apache-2.0, three daemon classes, nGQL, and
the fifth-through-seventh stateful services on a one-operator box.
**Recommendation: record NebulaGraph as the fallback and treat E1/E4 firing as a trigger to
*re-open the question*, not to auto-buy.** E1 is 15× away (16,860 of 250,000) and E4 is the one
v3 moves — worth watching, not worth pre-committing.

**Q4 · A fifth `source_class` for non-public material?**
There is no `field_report` and no `private` class today; `source_class` is a closed Literal of
four with no DB column (§2.8).
**Recommendation: do not add one until such a source exists.** v3 builds the per-link machinery
regardless, and adding a class later is a one-line Pydantic edit plus a descriptor — no
migration, no backfill.

**Q5 · The inquiry kind's tool grants — web, or no web?**
The brief describes "full internal tools, no web". The ledger's own 2026-09-21 proposal reads
"one kind from the journal_assessor pattern + brief option + **substrate/web packs**". These
disagree and the operator owns it.
**Settled as: the first grant is `substrate_read` (including `query_events`, `inspect_event`,
`belief_as_of`) + `journal_read`, and NOT `web_access`** — because the `web_access` pack
governor is lifted to 1,000,000/h and 1,000,000/min, so a directed reader on that pack is an
unmetered egress and cost surface. Web is addable afterwards with its own tighten-only
`governor_override`, which is a descriptor PUT and no roll. The kind ships on that grant.

**Q6 · The hand-check worksheet.**
`docs/data/kg2_bakeoff/handcheck_worksheet.csv` — 40 most-contested typing candidates, three
empty `OPERATOR_*` columns, unlabeled since 2026-08-03. It gates any typer model change (§5.3),
because the bake-off found no difficulty signal on which to build a router without ground truth.
**Recommendation: label it, or accept that the typer model is frozen at `core120b`.** The second
is a perfectly good answer; it just needs saying out loud.

**Q7 · Event↔event causal typing.**
`caused_by` and `contradicts` are LLM judgements with real cost and real error modes; the other
three edge types are deterministic.
**Recommendation: ship deterministic edges only, and declare the causal pair as SEAMS #56** —
the writer raises on those two types with a SEAMS reference, so the feature is visibly
not-built rather than quietly half-built. Revisit when events exist and the demand is legible.

**Q8 · The backfill ceiling.**
§7.2 proposes 30 days, verify-passed at the floor, ≥2 resolvable signals, collapsed by
signature, and a hard `_BACKFILL_MAX_EVENTS = 5,000` above which the migration refuses.
**Recommendation: 5,000.** The `situation_events` path is bounded at 1,436 by the data itself
and is safe at any ceiling; the findings path is the one that needs a wall, and 5,000 is ~4×
the ledger path — enough to be useful, small enough to review.

### Decisions

All eight were accepted as recommended, and the build followed them. Recorded here so the
questions above read as settled; the binding table lives in
[`V3_IMPLEMENTATION_PLAN.md`](V3_IMPLEMENTATION_PLAN.md) §0.1.

- **Q1** accept the negative AGE verdict; mirrors stay unarmed; no re-probe.
- **Q2** build `graph_arcs` (§4.2).
- **Q3** NebulaGraph is the recorded scale fallback; E1/E4 firing re-opens the question, never auto-buys.
- **Q4** no fifth `source_class` until a non-public source exists.
- **Q5** the inquiry kind's first grant is `substrate_read` + `journal_read`, not `web_access`.
- **Q6** the typer model is declared frozen at `core120b`; the worksheet stays optional and gates any change.
- **Q7** deterministic event edges only; the causal pair is SEAMS #56.
- **Q8** backfill ceiling 5,000.
