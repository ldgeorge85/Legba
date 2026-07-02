# Legba — Architecture

This document explains Legba's concepts and why the system is shaped the way it
is: the pieces, how they compose, and where each one lives in the tree. For the
implementation decisions (APIs, files, deployment) see `DESIGN.md`; to navigate
the code see `CODE_MAP.md`; for "life of a…" walkthroughs see `FLOWS.md`; for
operations see `RUNBOOK.md`. New here? Start with the [README](../README.md)
and the [Tour](TOUR.md).

> **Honesty contract.** Every factual claim below is traceable to code
> (`file:line` cited liberally). Where a capability is *built but not wired*, or
> *declared but absent*, this doc says so in-line — it does not imply more than
> ships. The not-built list is `docs/SEAMS.md`.

**Contents:**
[0 Orientation](#0-orientation--what-processes--flows--triggers--schedules--scales) ·
[1 The altitude map](#1-the-altitude-map--the-organizing-frame) ·
[2 The problem](#2-the-problem-situational-awareness-at-scale) ·
[3 The source-first answer](#3-the-source-first-answer-ingest-once-enrich-once-match-many) ·
[4 The spine](#4-the-spine) ·
[5 The core abstractions](#5-the-core-abstractions) ·
[6 The four planes](#6-the-four-planes) ·
[7 The runtime](#7-the-runtime--the-dapr-virtual-actor-model) ·
[8 Outputs](#8-outputs--the-provenance--write-paths) ·
[9 The actor → Workflow seam](#9-the-actor--dapr-workflow-seam-the-optimizer-precedent) ·
[10 Self-improvement](#10-self-improvement--closing-the-loop) ·
[11 How it scales](#11-how-it-scales) ·
[12 Provenance + hot-pluggability](#12-provenance-and-hot-pluggability-end-to-end) ·
[13 Live, proven state](#13-live-proven-state) ·
[14 Built vs. declared seams](#14-built-vs-declared-seams) ·
[15 Read next](#15-read-next)

---

## 0. Orientation — what processes / flows / triggers / schedules / scales

Read this first if the whole shape has gone foggy. The detail sections expand each piece.

**Three planes.** (1) **Substrate** = shared state/truth: Postgres+AGE (descriptors, signals, facts, nexuses, entities, hypotheses, outputs), NATS JetStream (signal bus + events), Qdrant (vectors), Redis (hot state/cache). (2) **Control plane** = `legba-registry` (FastAPI): the descriptor registry + lifecycle FSM + API/WS + vault + DLQ — *everything is a descriptor* (source/target/analyst/stack). (3) **Runtime plane** = `legba-runtime-dapr`: turns active descriptors into **Dapr virtual actors** that read/write the substrate (+ `legba-dapr-workflow-worker` for the multi-step Workflows: deep-consult, GEPA optimizer).

**The live flow (what processes what):**
```
EXTERNAL ─▶ SourceActor ─▶ baseline filter ─▶ predicate ─▶ TargetActor ─▶ AnalystActor ─▶ emit
sources    (1/source,      pipeline (NER/geo/   fan-out     (1/target,     (per-target +   handlers
           poll/webhook)   dedupe/source_cred/  (signal→     subscribes     cross-target/   (alert/webhook/
           → canonical     fact_extract on      matching     its slice)     meta) reads     STIX/UI/MCP)
           Signal → NATS    the Signal)          targets)                    slice+substrate
                                                                             → writes outputs
```

**Two analysis tiers — never collapse them.** Analysis happens at *two distinct times* on two distinct triggers:
- **TIER 1 — INLINE, at-ingest, per-signal (deterministic, no LLM).** The `data/filters/` **baseline enrichment pipeline** runs *synchronously on each Signal at acquisition*, BEFORE fan-out, inside the `SourceActor`: `language_detect → geocode → ner_multilingual → classify → source_credibility → ingest_dedupe (dedupe_4tier, tiers 1-2) → fact_extractor`. It is deterministic / local-NLP (GLiREL + DeBERTa zero-shot + pycountry/Nominatim) — **no analyst LLM** — and its *writes are altitude-0 substrate*: the **enriched signal** (geo/language/tags/entity_classes promoted to indexed columns, in-place on the one `signals` row) plus **altitude-0 `facts`** (`source_type='ingestion'`, `valid_from`-stamped) + **entity rows / `signal_entity_links`** off the NER spans. This is the "enrich once, read many" tier (§6.1, Flow 1). See `data/filters/__init__.py` for the kind registry; `data/sources/baseline.py:282-294` for the tier ordering; `data/filters/dedupe.py:248` (`kind="dedupe_4tier"`); `data/filters/fact_extractor.py` (§5.7).
- **TIER 2 — SLICE / CADENCE analysts (`data/analysts/`, LLM + deterministic reasoning).** These read *accumulated slices / substrate* on a Dapr reminder or a reactive trigger and *reason* — they do NOT run per-signal. This is altitudes 1-3 (findings, situations, hypotheses, nexuses, meta-findings, deep consult). The cost firewall between the two: heavy reasoning is cadence-batched so LLM cost is decoupled from the ingest firehose.

**What triggers / schedules what** (Tier 2) — two mechanisms, both **Dapr reminders/triggers** (no external cron in the loop): **(a) reactive coalescing triggers** — per-target analysts fire when enough new signals accumulate (NATS-driven); **(b) cadence reminders** — cross-target & meta analysts fire on a schedule (the four bounded reasoning UNITS + the `country_composition`/`world_assessor` compositions on a ~6–12h beat, `competing_hypotheses`/`graph_mining`/`relationship_reifier` ~12h, `scorecard_producer`/`calibration_tracking` daily, `forecast_scoreboard` weekly). SourceActors schedule their own polls (the Tier-1 pipeline rides each poll). The reminder *is* the scheduler.

**Where Wikidata / grounding fit (out-of-band, decoupled by the substrate):** Wikidata is **not a live source** — it never touches the signal pipeline. It is a **seed** (`scripts/seed.py --source wikidata_leaders`, operator-run / cron-able) that writes *current* `head of state` facts INTO the substrate (superseding the stale officeholder). Separately, at *analysis time*, grounding-enabled analysts' **GROUND phase** READS those current facts back OUT of the substrate and injects a dated preamble into the LLM prompt (Flow 10). The two movements don't know about each other — the substrate is the hand-off. See §5.8.

**What scales** — the **actors are the workers** (SourceActor/TargetActor/AnalystActor); Dapr **placement** redistributes them across `legba-runtime-dapr` replicas, so you scale out by adding replicas. **Cadence-batching is the key move:** analysts run on a schedule, not per-signal, so **LLM cost is decoupled from the ingest firehose**. The substrate scales independently (PG read-replicas/partitioning, NATS, Qdrant). **Singletons needing leader election:** the reconcile loop + the discovery informer (single-replica fail-loud guard otherwise; 2-replica placement + leader election proven locally). **The real ceiling is LLM throughput** (budget-gated) — which is exactly why the heavy graph work is deterministic Python and the analysis layer is cadence-batched. (More in §11.)

**What runs OUTSIDE the source→output loop (out-of-band).** Not everything is the live `SOURCE ─▶ … ─▶ OUTPUT` pipeline. The decoupled processes — each handing off through the substrate, never on the signal hot path:
- **Seeds** (`scripts/seed.py` → `data/seed/`): curated/authoritative roots imported INTO `facts`/`nexuses` with `source_type='seed'` + a `seed_batches` ledger row — adapters `world_baseline` / `wikidata_leaders` / `acled_conflict` / `sipri_arms_transfers` (operator-run / cron-able; §5.7, Flow 9). Wikidata is a *seed*, not a live source.
- **Backfills** (`scripts/backfill_entity_canonicalization.py`, `scripts/backfill_entity_graph.py`; `SourceActor`'s optional `source_credibility` host-lookup at write-time, §6.1): one-shot substrate repairs over already-stored rows.
- **Bringup / registration** (`scripts/bringup_register_*.py` + the 46-entry `bringup_register_source_catalog.py`): pushes descriptors into the registry tables at deploy — the live source/analyst set is the DB rows, not the `descriptors/*.yaml` files.
- **Dapr Workflows** (`legba-dapr-workflow-worker`, `runtime/dapr_workflow/`): the multi-step durable jobs that don't fit a turn-based actor — the **GEPA optimizer** (§9, Flow 4) and **deep-consult** (§9, Flow 5). Scheduled detached from an actor run.
- **Maintenance / GC** (deterministic analysts on cadence, `data/analysts/deterministic_handlers/`): `fact_decay` / `nexus_decay` (temporal-confidence decay + expiry), `finding_supersession`, `entity_gc`, `signals_retention` (0036), `integrity_sweep`, `reminder_gc` (`runtime/reminder_gc.py`, GC of reminders for retired `actor_state` rows). These UPDATE/prune pre-existing rows.
- **Per-source liveness watchdog** (`liveness_watchdog.check_source_cadence_once`, cadence): detects a silent source by comparing `now()` to `max(signals.fetched_at)` per source, then lateral-joins `source_poll_outcomes` (0046) for the *why* — `SourceActor.pull_once` writes a `source_poll_outcomes` row for every NON-productive poll (empty HTTP-200-with-0-signals, or error; productive polls are self-evidencing via their `signals` rows and are not logged), carrying the handler's own health diagnosis so the watchdog alert (and the UI) can distinguish a genuinely quiet feed from a broken one.
- **Meta-analysts over the substrate** (altitude 2): `meta_findings_synthesizer` / `cross_analyst_correlator` / `competing_hypotheses` / `calibration_tracking` — they read accumulated outputs, not signals (Tier-2 cadence, but analysis-of-analysis rather than first-order).
- **Migrations** (`data/migrations/0001_baseline` + the forward chain `0032`…`0057`, current head **0057**): schema evolution, applied PG-direct out of band. The chain adds the `facts`/`nexuses`/`seed_batches` rigor schema (0032–0034), the entity composite key / signals-retention / AGE-output-label / ACH `resolved_outcome` / consult-sessions tables (0035–0039), situations-as-first-class + repairs (0040–0042), the data-quality backfills (0043–0045), `source_poll_outcomes` (0046), the `acute_forecasts` pilot table (0047), the journal table (0048), the receipt/derived-from repairs + data cleanups (0049–0053), the contested-claims schema — `facts.source_credibility` (0054) + the `fact_contention` sidecar (0055, §5.9), a second dangling-`derived_from` prune (0056), and the `unit_reference_labels` correctness-gold table (0057, §5.10).

### 0.1 Substrate data inventory — what is kept where, written-by / read-by

Per store, the actual datasets and their producers/consumers (verified 2026-06). The runtime substrate is **four backing services** — Postgres+AGE, NATS JetStream, Qdrant, Redis (the time-series-metrics and full-text-search stores that earlier drafts over-claimed have been removed; see SEAMS):

| Store | Datasets kept | Written by | Read by |
|---|---|---|---|
| **Postgres + Apache AGE** (PRIMARY / source of truth) | **descriptors** (`source_/target_/analyst_/action_pack_/stack_/wiring_descriptors`); **acquisition** (`signals`, `signal_aliases`, `signal_entity_links`); **knowledge substrate** (`facts`, `nexuses`, `entity_profiles`/`entity_profile_versions`, `hypotheses`, `proposed_edges`, `situations`, `graph_metrics`); **outputs/provenance** (`analyst_outputs`, `analyst_traces`, `analyst_critiques`, `output_dead_letter`, `descriptor_dead_letter`); **journal (OFF-chain)** (`journal_entries`, `journal_proposals` — 0048; the reflective voice, empty `derived_from`, excluded from the lineage catalog, §8.4); **runtime state** (`actor_state`, `actor_filter_state`, `trigger_state`, `discovery_state`); **governance** (`governor_events`, `budget_ledger`, `global_budget_envelope`, `budget_demotion_events`, `action_pack_invocations`, `alert_sink_deliveries`); **liveness** (`source_poll_outcomes` — provenance for non-productive source polls, 0046); **consult audit** (`consult_sessions` / `consult_turns`, 0039); **audit** (`audit_checkpoints`, `descriptor_audit_log`); **seeding** (`seed_batches`); **reference** (`iso_countries`, `source_credibility`, `vocabulary_entries`); plus the **dormant AGE graph `legba_graph`** *inside* PG (9 vertex / 14 edge labels registered, but **near-empty / off-path** — the operative graph is the relational `nexuses` table; §5.5 "AGE re-evaluation") | Tier-1 pipeline (signals/facts/entities); Tier-2 analysts + workflows (outputs/facts/nexuses/hypotheses); registry (descriptors/audit); runtime (state); seeds (facts/nexuses + `seed_batches`) | every read path — analyst slices, consult tools, grounding resolver, the API/UI, lineage walks |
| **NATS JetStream** | **transport / events, NOT a dataset store** — `legba_signals` (interest-retention signal bus), 4 registry-lifecycle streams, the DLQ stream, work-queues, consult-step relay. Transient fan-out; the durable copy is always in PG | SourceActors (signal publish), registry (lifecycle events), analysts (output envelopes + consult steps) | subscription consumers, the reconcile loop, the SSE relay, job workers |
| **Qdrant** | **1 collection `legba_signals`** — signal vectors (1024-dim BGE-M3 cosine), used by ingest-dedupe tiers 3-4 (the expensive semantic tier) | Tier-1 dedupe / embedder | dedupe tier 3-4; consult `vector_search`. (Grounding Tier-2 `vector:world_context` is a *declared future seam*, §5.8 — not a live collection) |
| **Redis** | **TTL'd cache only** — geocode cache, ingest-dedup hints, registry-health, intelmq source state (~84 keys live) | Tier-1 filters, health checker | the same filters / health (cache-aside; never a source of truth) |
| **SeaweedFS** | object store for retained media — **schema-slotted stack-component kind; NO handler shipped** (eager-media extraction is a seam, §6.3) | (none) | (none) |

> **Removed stores.** A dedicated time-series-metrics store (Grafana/TimescaleDB observability) and a full-text-search backing (OpenSearch BM25, primary + audit) were both *provisioned-but-idle* with zero callers and have been **removed from the codebase**. Time-series metrics and full-text search are now **declared seams** (see SEAMS): there is no metrics store, and `search_signals` uses Postgres FTS (`to_tsvector`/`plainto_tsquery`). `anomaly_detection` is unaffected — it reads `time_bucket()` from the **primary Postgres pool**, not a separate cluster.

---

## 1. The altitude map — the organizing frame

Everything in Legba sorts by **altitude**: how far above the raw signal it sits.
This is the frame that tells you *where* each piece belongs and keeps the build
from collapsing into a god-agent. It is the spine of the analysis layer and the
lens for the rest of this document.

| Altitude | Layer | Produced by | Status (P0–P4 spine, live) |
|---|---|---|---|
| **0 — Extraction** | temporal facts (atomic `(subject, predicate, value)` assertions, valid-from/until + supersession) | the ingest-time **`fact_extractor`** enrichment stage (per-signal, GLiREL backend) + analyst/workflow `write_fact` | **LIVE** — `OutputKind.FACT` + `write_fact` + `facts` temporal schema; ≈4.6k facts total (≈3.7k ingestion-sourced) (§5.7) |
| **0 — Relations** | reified typed signed **Nexus** (`subject →[intermediary]→ object`, `rel_type` + polarity ∈ {−1,0,+1} + intent) | the **`relationship_reifier`** META analyst (8B-LLM types co-mention pairs) | **LIVE** — `OutputKind.NEXUS` + `write_nexus` + `nexuses` table; ≈4.9k nexuses total (≈3.2k signed, polarity ≠ 0) (§5.7) |
| **1 — First-order (bounded units)** | cited, faithfulness-verified findings — each unit answers ONE narrow question | FOUR `inline_target` reasoning UNITS (`leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`; `method.kind=llm_planner`), each fanned out per desk — 24 desks: the 19 G20 country desks + a 5-country high-consequence `watch` tier (§7.2) | **LIVE** — the monolithic `country_assessor` is RETIRED and STOPPED (nothing in the spine reads it; ≈1.2k historical findings remain in the DB, unread — not a clean slate); the forecast-as-claim `country_predictor` is RETIRED/STOPPED (≈539 historical prediction rows remain) (§5.10, §14) |
| **1 — Maintenance** | situations (**first-class temporal frames**, 0040–0042) / supersessions / critiques / STIX / fact-&-nexus decay | situation_clustering (+ `thematic_proposal`), finding_supersession, `critic`, emit-bindings, `fact_decay` / `nexus_decay` / `structural_balance` / `graph_mining` | **LIVE** — situations carry `situation_signature` + `valid_from`/`valid_until`/`superseded_by` + `target_id` (0040–0042); the events substitute (no `events` table). The forecast-as-claim `predictor` producers (`country_predictor`, `india_energy_predictor`) are RETIRED/STOPPED (≈539 historical prediction rows remain) — forecasting returns only as the measured `acute_forecasts` scoreboard (§5.10) |
| **2 — Composition** | per-country + world composition (a hedged, cited synthesis over the *verified* units; an unverified sub-claim never enters — INNER JOIN on the faithfulness critique) + meta-findings | `country_composition` + `world_assessor` (both repointed to `meta_findings_synthesizer`), `cross_analyst_correlator` | **LIVE** — `world_assessor` GRADUATED into the world composition; it is NOT the old verdict-from-nowhere monolith (§5.10) |
| **top — Banded scorecard + skill scoreboard** | one banded per-country row from high-precision RULES over already-verified claims (demote-never-promote) + the per-unit skill numbers | `scorecard_producer` (deterministic META, 12th OutputKind `scorecard`), `unit_correctness_scorer` / `calibration_tracking` / `forecast_scoreboard` | **LIVE** — honest: an unqualified dimension reads `insufficient-evidence`; the live scorecard is a MIX (some countries band, e.g. the US reads all-insufficient because its unit faithfulness is genuinely low); the forecast pilot reports NO proven skill (§5.10) |
| **2 — Second-order** | hypotheses (competing claims, ACH matrix; per-cell scoring is LLM-scored on Heuer CC/C/N/I/II with a lexical fallback — §5.3) | the **`competing_hypotheses`** (alias `ach`) META analyst + `calibration_tracking` (Brier reads `resolved_outcome`; exogenous resolver built + firing — subsequent-facts auto-resolver that ABSTAINS on undirected theses + operator-label path — alongside the live self-consistency tier, §5.3) | **LIVE** — ≈940 hypotheses (253 confirmed / 287 refuted / ≈400 active) (§5.7) |
| **3 — On-demand deep** | deep consult (a staged analytical job: plan→acquire→analyze→synthesize) | the **deep-consult Dapr Workflow** | **LIVE** — registered alongside `optimizer_workflow` (§9) |
| **across — Reflective voice (OFF-CHAIN)** | the **journal** — Legba's first-person reflective voice; a `journal` row is a *perspective OVER* the whole flow, **NOT** a node in the fact/finding/nexus lineage (always-empty `derived_from`, excluded from the lineage catalog) | the **`journal_assessor`** META analyst (entry + consolidation tiers, per-phase LLM split: local gpt-oss/vLLM GATHER + Anthropic Opus 4.8 voice) | **LIVE, ON cadence** — runs as an introspective instrument (`journal_assessor` 12h entry + `journal_consolidator` daily); writes ONLY `journal_entries`, off the fact/finding/nexus chain, so it cannot pollute product output (routing its reflections back via a human-gated proposal queue is a FUTURE item); `OutputKind.JOURNAL` + dedicated `journal_entries` table (migration 0048); §8.4 |

Two clean regimes fall out: **extraction is always-on at ingest** (altitude 0,
once per signal); **deep analysis is on-demand** (altitude 3). The entire stack —
altitude 0 (temporal facts + reified Nexus), altitude 1 (the four bounded units +
the continuous live loop), altitude 2 (the per-country + world composition,
meta-findings + ACH hypotheses) and altitude 3 (deep consult) — is now wired and
live-proven; what was the "open frontier" of the pre-2026-06 drafts has been
built out as the **data-analysis rigor layer** (§5.7). Each tier rides a rail
that already existed with a working precedent (§9).

**The product is the composed, verified spine, not any single analyst.** What
Legba surfaces at altitude 1+ is a decompositional chain, read bottom-up: FOUR
narrow reasoning UNITS (each cited to source and put through a **mandatory
faithfulness verify pass**, §5.10) → a per-country **composition** that admits
only verified sub-claims → a **world composition** over the per-country reads → a
deterministic **banded scorecard** + a per-unit **skill scoreboard**. Every
claim is cited, checked for groundedness against its cited evidence, and
auditable through a receipt chain to the original signal (§8, §12). Legba **measures
groundedness (faithfulness — does each claim follow from its cited evidence?),
NOT truth**; where a leg is honestly weak today (a degenerate forecast pilot, a
tiny correctness gold set, a country whose units read all-insufficient) the
system publishes that plainly rather than papering over it (§5.10).

---

## 2. The problem: situational awareness at scale

Legba's job is to **continuously make sense of the world from many vantage
points** — countries, sectors, entities, customer estates, single persons of
interest — and to keep a current, provenance-traceable model of "what is true
now" for each of them, cheaply, in parallel, over many heterogeneous data
sources.

The naive shape of such a system — *one watcher owns its sources, pulls them,
enriches what it pulls, and reasons over the result* — does not survive contact
with scale. The forces that break it:

- **Fan-out.** Hundreds of country/topic watchers sharing the same ~10 news
  feeds means hundreds of redundant polls, cursors, and dedup pipelines over
  identical bytes.
- **Cost per acquisition.** Per-query-billed sources (asset-surface scanners,
  certificate-transparency lookups, paid OSINT) make *per-watcher* polling
  economically impossible. Sharing the acquisition is the only viable primitive.
- **Expensive enrichment.** Transcribing audio, captioning images, running NER
  and embedding must happen **once per observation**, never once per consumer of
  that observation.
- **Real-time where it matters.** Some signals (a confirmed match, a critical
  severity) must be reacted to as they land — not at the next scheduled poll —
  while everything else stays batched and cost-bounded.

The shared root cause is that *observation* (acquiring and normalizing a fact
about the world) and *interpretation* (deciding what that fact means for a
particular concern) are entangled. Legba's architecture is the move that
separates them.

## 3. The source-first answer: ingest once, enrich once, match many

The central design commitment:

> **A signal is an observation, not an interpretation.** Sources own
> acquisition. They ingest a fact about the world *once*, enrich it *once* in a
> target-agnostic way, and publish it *once* into a shared pool. Targets are
> passive subscribers that *match many* of those signals out of the shared pool
> by predicate. Analysts coalesce matched signals into findings.

Concretely, a `Signal` (`src/legba/data/sources/_contract.py:137`) carries **no
`target_id`** — the docstring states it explicitly: *"the observation is
source-owned: `target_id` is gone — it lives only on derived analyst outputs"*
(`_contract.py:140-143`). A signal records *where it came from* (`source_id`,
`source_version`, `_contract.py:156-157`), *what produced this row*
(`produced_by_kind` ∈ source/job/analyst/deterministic/system,
`_contract.py:159-161`), *what it is* (`modality`, `mime_type`, `media_ref`,
`_contract.py:168-172`), and the enrichment computed once at acquisition
(`language`, `geo`, `tags`, `entity_classes` — indexed columns for filtering,
`_contract.py:192-195`). It does **not** record what it means to anyone.
`target_id` lives only on **analyst outputs** — interpretation is target-owned;
observation is shared.

This one inversion pays off everywhere:

- **One source = one pull/connection**, no matter how many concerns consume it.
- **Heavy enrichment runs once**, at the source, for all consumers.
- A target's "view" is a **predicate-filtered slice** of the shared pool — no
  copies, no per-target marker rows.
- **Real-time and batch fall out of the same mechanism**: a published signal
  both notifies live subscribers (NATS) and persists to a queryable pool
  (Postgres), so late joiners and re-analysis read the same source of truth.

## 4. The spine

The whole system is one pipeline, read left to right. Everything above (the
altitude map, the planes, the actor model) is a refinement of this picture.

```
   SOURCES ───────► SIGNALS ──────► predicate FAN-OUT ──────► TARGETS ─────► ANALYSTS ─────► OUTPUTS
   (acquire)        (observe,        (match-many)            (a concern)    (reason)        (interpret)
                     enrich once)

  ┌────────────┐   write canonical   ┌───────────────────┐   subscription   ┌──────────────┐
  │ SourceActor│──── signal ROW ────►│  shared signal pool│   resolves to    │ TargetDescr. │
  │  (per src) │   (Postgres, ONCE)  │  Postgres `signals`│   per-target     │  SourceRef = │
  │            │                     │   = source of truth│   consumer       │  id|selector │
  │  poll      │   publish event     │                    │                  │  Subscription│
  │  (Reminder)│──► legba.signals.   │  NATS JetStream    │                  │  = filter +  │
  │  push      │    <tenant>.<src>.  │  `legba_signals`   │                  │  Starlark    │
  │  (webhook) │    <modality>.      │   = notification   │                  │  residual    │
  └────────────┘    <event_class>    │     bus            │                  └──────┬───────┘
        │           (coarse subject) └─────────┬──────────┘                         │
        │ baseline                             │  two-stage match:                  │ cadence
        │ enrich ONCE                          │  (1) SQL WHERE on indexed cols     │ reminder
        │ lang→language                        │      (geo/tags/entity_classes      │ fires the
        │ geocode→geo                          │       via GIN, modalities btree)   ▼ analyst
        │ classify→tags                        │  (2) Starlark residual on the   ┌──────────────┐
        │ ner→entity_classes                   │      narrowed stream            │ AnalystActor │
        ▼                                      │  (5ms wall-clock budget)        │  PRIMARY     │
   (enriched signal,                           └────────────────────────────────│  owns cadence│
    target-agnostic)                                                            │  reminder,   │
                                                                                │  FANS OUT 1  │
   ┌────────────────────────── derived_from (lineage) ◄────────────────────────│  run/target  │
   │                                                                            │  to PER-     │
   ▼  ASYNC JOBS (NATS work-queue + competing-consumer workers)                 │  WORKER      │
  process_media (Whisper/VLM/OCR, hosted) — the live job kind                   │  actors      │
   result → derived signal (derived_from → raw) → re-enters FAN-OUT             └──────┬───────┘
                                                                                       │ run_method
                                                                                       ▼
                                       OUTPUTS: OutputKind ∈ {finding, situation,
                                       hypothesis, prediction, alert, meta_finding,
                                       critique, fact, nexus,
                                       prompt_module_candidate, journal, scorecard}
                                       → write_analyst_output → analyst_outputs /
                                         situations / hypotheses / facts / nexuses /
                                         journal_entries (journal = OFF-chain,
                                         empty derived_from; §8.4)
                                       → NATS analyst.<id>.<channel>
                                       → emit-bindings (STIX bundle, alert sinks, …)
                                       → receipt chain (SHA-256) + derived_from
```

Each box is a section below: SOURCES + SIGNALS + FAN-OUT (§6 acquisition/analysis
planes), the actor model that runs them (§7), OUTPUTS + provenance (§8), and the
self-improvement seam that closes the loop (§10).

## 5. The core abstractions

Six declarative units compose the system. Each is a pydantic descriptor (under
`src/legba/data/schemas/`) — strict, content-hashed, registry-managed.

### 5.1 Source — *what to acquire, and how*

A `SourceDescriptor` (`schemas/source.py:183`) is a first-class acquisition unit.
It declares its `acquisition` mode (`poll` or `push`), its per-kind `config` (RSS
URL, scanner query, webhook spec), a `cadence` (for poll sources — a cron
schedule fired by a Dapr Reminder), a baseline `pipeline` (the enrich-once
stages — `SourcePipeline`, `schemas/source.py:119`), the substrate `deps` it
needs (Postgres reader, vault secrets, Qdrant — declared *once*, resolved by its
actor at activation), and an `output` policy (NATS subject, retention,
lossy/lossless). It advertises a `scope` (`owner_tenant`, `geo`, `languages`,
`tags`) — the metadata targets match against — and a `subscription_policy`
(`open` / `allowlist` / `grant`) gating who may subscribe.

A source that must register upstream before it receives anything (a webhook
subscription, a watchlist registration) declares a `provision` block whose
`on_activate` lifecycle hook fires the outbound registration and `on_retire`
deregisters.

> **Future seam:** acquisition is `poll | push` today. A long-lived-consumer
> `stream` mode (raw socket / MQTT / SSE with no webhook or poll API) is a
> documented, non-breaking extension — add the enum value and a handler when a
> real source demands it (`docs/SEAMS.md`).

### 5.2 Target — *what to watch*

A `TargetDescriptor` (`schemas/target.py`) is *scope + subscription + analysts*.
A "target" is really a **scoped subject / desk** — a named scope-frame that a set
of analysts work — not a surveilled entity. It owns **no sources of its own** — it
references shared sources through a list of `SourceRef` (`schemas/source.py:304`),
each of which is exactly one of:

- an **explicit** `source_id` (subscribe to a named source), or
- a **`source_selector`** predicate over source *scope* (subscribe to *any*
  source whose tags/geo/languages/tenant/kind match — newly-discovered matching
  sources **auto-wire**, but only for `open`-policy sources;
  `subscription/sourceref.py:167-243`).

Each `SourceRef` carries a `Subscription` (`schemas/source.py:223`): a structured
signal filter (geo, languages, tags, entity_classes, modalities — indexed) plus
an optional Starlark **residual** predicate for the long tail (`mentions(...)`,
`severity_at_least(...)`, `host_ip in cidr(...)`), evaluated on the
`TARGET_SCOPE` predicate surface.

A target's `scope` is **polymorphic by domain** (`GeoScope` / `EstateScope` /
`EntityScope`, a discriminated union). A target watching a single person or a
customer estate no longer fakes `geo: ["XX"]` — the same platform serves
geopolitical SA, attack-surface monitoring, and single-entity tracking as
first-class shapes.

### 5.3 Analyst — *how to reason*

An `AnalystDescriptor` (`schemas/analyst.py`) declares a `method`, a
`subscription` (which targets/analysts it reads, by selector predicate;
`SubscriptionTargets`, `schemas/analyst.py:212`, carrying a `time_window`), a
`cadence` (its trigger + `fallback_schedule` + `cooldown`), `outputs`,
`action_packs` (its agency grant — §5.6), and an optional `eval` block. It
produces typed outputs stamped with `target_id` + `analyst_id` + `derived_from`
provenance.

Analysts come in an **open taxonomy**. The deps-builder dispatches **twelve**
built-in kinds (`build_analyst_run_method`, `analyst_deps_builder.py:227-278`):
`inline_target`, `cross_target_raw`, `meta_findings_synthesizer`,
`cross_analyst_correlator`, `relationship_reifier`, `competing_hypotheses`
(alias `ach`), `deterministic`, `predictor`, `critic`, `optimizer`,
`consult_on_demand`, `deep_consult`, plus operator-registered extension kinds
(via the `analyst_kind` vocabulary). The same twelve are listed in
`_KIND_MODULE_NAMES` (`data/analysts/__init__.py:88-101`), consumed by
`discover_analyst_kinds()`. The *analyst kind* (the `build_*` branch) is distinct
from the `method.kind`: e.g. the four bounded units are analyst-kind
`inline_target` but carry `method.kind = llm_planner` (the LLM-planner finding
shape; `descriptors/analyst_leadership_transition.yaml`), while `country_composition`
and `world_assessor` are analyst-kind `meta_findings_synthesizer` — the world
composition is the meta kind fanning out globally, NOT a bespoke verdict engine
(`descriptors/analyst_world_assessor.yaml:44`). The graph composes the same way
at every tier — a meta-analyst subscribes to upstream analyst findings exactly as
a target subscribes to source signals, all on the shared `legba.signals.>`
stream.

The kinds added by the data-analysis rigor arc (§5.7) and now **registered**:

- **`relationship_reifier`** (`data/analysts/relationship_reifier.py`) — a META
  kind that reads candidate co-mention pairs from `proposed_edges` (anti-joined
  against open `nexuses`, confidence ≥ 0.45) plus recent open `facts` for
  context, types each pair via the 8B LLM plane into `rel_type` + canonical
  polarity sign + `intent` + `channel` (+ optional `intermediary`), and writes a
  `nexus` row (`write_nexus`, `:555`) stamped with the pair's event time. Hard
  cap of 40 candidates/run, budget-gated per call (`:464-471`), **never litellm**
  (only `deps.llm.chat_complete`).
- **`competing_hypotheses`** (alias `ach`, `data/analysts/competing_hypotheses.py`)
  — a META kind that reads focal `situations` by `intensity_score` (a 14-day
  window, **not** gated on `status='active'`), current facts
  (`superseded_by IS NULL AND valid_until IS NULL`), and open signed `nexuses`.
  It emits **≥2** mutually-exclusive hypotheses, each with a mandatory
  counter-thesis, laid out on a Heuer consistency matrix (CC/C/N/I/II → ±2) with
  diagnosticity weighting and an integer evidence balance; ±2 transitions move a
  hypothesis to a `confirmed` / `refuted` status. The evidence base is scoped to
  the topic's **resolved-entity set** (`entity_profiles` canonical names — exact
  membership, **not** a `LIKE '%name%'` substring). It writes
  `OutputKind.HYPOTHESIS` via `write_hypothesis` with the full ACH matrix in
  `diagnostic_evidence` jsonb. A deterministic escalate/de-escalate/status-quo
  fallback runs when the LLM is unavailable; **never litellm**.

  > **The per-cell consistency matrix is now LLM-scored.** The LLM proposes the
  > hypothesis *set* (thesis / counter-thesis pairs) **and** scores every matrix
  > cell on Heuer's CC/C/N/I/II scale via
  > `_score_consistency_matrix_llm` (`competing_hypotheses.py:746`) — one batched
  > call per topic through the analyst provider plane (`deps.llm.chat_complete`),
  > budget-gated by `check_envelope()`, **never litellm/dspy**. When the budget
  > envelope is exhausted (or the LLM is unavailable / unparsable), the run falls
  > back **per cell** to the deterministic lexical/polarity counter
  > `_score_consistency` (escalation vs de-escalation cues ± signed-nexus polarity
  > → −2..+2). Each hypothesis row records which path ran under
  > `diagnostic_evidence[].matrix_scorer` (`"llm"` or `"lexical"`). Because cells
  > are now semantically scored, the `confirmed` / `refuted` status transitions
  > are **more defensible** than the old "leading / dominated" framing.
  > **Residual caveat:** a budget-exhausted run still falls back to the lexical
  > scorer — check `matrix_scorer` before treating a matrix as semantic — and the
  > matrix is an analysis of the current evidence base, not an adjudicated verdict
  > (calibration is the exogenous check — its resolver is built and firing, with
  > the subsequent-facts auto-resolver now ABSTAINING on undirected theses; see
  > below + §13).
- **`deep_consult`** (`data/analysts/deep_consult.py`) — the altitude-3 on-demand
  kind whose `run_method` short-circuits to *schedule* the deep-consult Dapr
  Workflow and return a task id immediately (§9).
- **`meta_findings_synthesizer`** / **`cross_analyst_correlator`** — the
  altitude-2 meta producers, now registered (`analyst_meta_synthesizer.yaml` /
  `analyst_cross_correlator.yaml`); the prior "built but unregistered → 0 rows"
  dormancy is closed.

The bringup set (`scripts/bringup_register_analysts.py`) registers the current
producer graph: the **four bounded units** (`leadership_transition`,
`energy_security`, `escalation`, `narrative_coordination` — all `inline_target`),
`country_composition` + `world_assessor` (the per-country + world composition,
`meta_findings_synthesizer`), `scorecard_producer` + `forecast_scoreboard` +
`unit_correctness_scorer` (the banded scorecard + skill scoreboard, all
`deterministic` META, §5.10), `composition_lineage_sweep` (a read-only
lineage-integrity audit over the composition outputs), `country_critic` (critic),
`unit_optimizer` (optimizer — the scoped, faithfulness-measured GEPA return, §10),
`consult_default` (consult_on_demand), `meta_synthesizer`, `cross_correlator`,
`competing_hypotheses` (ach), `calibration_tracking`, `fact_decay`, `deep_consult`,
`relationship_reifier`, `structural_balance`, `graph_mining`, `nexus_decay`,
`entity_gc`, **`proposed_edge_governance`** (promote/reject the `proposed_edges`
queue into `nexuses`, rejecting demonym/junk endpoints — P3-1),
**`thematic_proposal`** (propose thematic frames for uncovered hot situations,
for operator promotion to thematic targets — Phase 5b), and
**`fact_contention_arbiter`** (the detect-only contested-claims referee — a
`deterministic`-kind META analyst that scans open facts hourly, groups disagreeing
values, and surfaces the better-supported one in the contention sidecar **without
ever mutating a fact** — #101, §5.9). `journal_assessor` + `journal_consolidator`
are registered and **ON cadence** (12h entry + daily consolidation) as an
introspective instrument — off the fact/finding/nexus chain (§8.4, §14).

**Sequenced retirements / freezes** (documented in `docs/SEAMS.md`). The
monolithic `country_assessor` (the per-country one-pager) is **RETIRED and
STOPPED — not registered** (its bringup line is commented out): the units +
composition supersede it, nothing in the spine reads it, and it was feeding
untrusted findings. Its ≈1.2k historical findings **remain in the DB, unread**
(retirement stops production; it does not wipe the prior rows). The
forecast-as-claim `country_predictor` (and `india_energy_predictor`) are
**RETIRED/STOPPED** (cadence `fallback_schedule` nulled) — forecasting returns
only as the measured `acute_forecasts` scoreboard, never a free-text claim
(§5.10); their ≈539 historical prediction rows likewise remain. The old
monolithic `country_optimizer` stays **cadence-FROZEN** (descriptor still
`state=active`, byte-unchanged, cadence nulled); GEPA returns instead as the
bounded `unit_optimizer` (§10), and the freeze forecloses the reminder-flood
regression class. The situation-gated
`hypothesis_lifecycle` producer is likewise not registered — it emitted 0 rows
(gated on `active` situations that go dormant) and `competing_hypotheses`
superseded it; its module + tests + deterministic-dispatch entry are kept for a
possible future lifecycle-maintenance role
(`bringup_register_analysts.py:60-67`).

### 5.4 Descriptor + the registry — *hot-pluggability and provenance*

All three families (plus action-packs and stack components) are **content-hashed
descriptors** managed by the descriptor registry (`src/legba/data/registry/`).
The registry gives every descriptor:

- **Content-hashed identity** — a descriptor's `version` *is* the hash of its
  body, so "is this the same descriptor as before?" needs no operator-managed
  version string. An operator can re-tune a target fifty times a day, each
  producing a new content hash, while the *schema* version stays stable for
  months.
- **A lifecycle FSM** (`draft → configured → active → paused → retired`) with
  per-state hooks (a source's upstream provisioning fires on `on_activate`).
- **An Ed25519-signed audit log** (`registry/audit.py`) — every mutation is
  persisted with a signature over canonical JSON, in the same transaction as the
  mutation, so an audit-write failure aborts the change.
- **A DLQ on validation failure** (`descriptor_dead_letter` table +
  `legba.dlq.descriptor.*` NATS subject) — a malformed descriptor never
  half-lands.
- **NATS events** per state change (`descriptor.<action>.<family>.<id>`), which
  the runtime's reconcile loop consumes.

Register → configure → activate. No redeploy for content changes. This is the
well-trodden CRD/connector pattern (Kubernetes CRDs, Prometheus scrape configs,
dbt models); what is distinctive is that the **cognitive layer is descriptors
too** — analysts carry eval loops and self-tuning optimizers, so the declarative
composition runs all the way up.

### 5.5 Substrate — *the shared blackboard*

The substrate is a polyglot backend; every store is itself a registry-managed
**stack component** (`schemas/stack.py`) with credentials held in a separate
XSalsa20-Poly1305 vault (`registry/credentials.py`, PyNaCl SecretBox — master key
from `LEGBA_DATA_MASTER_KEY`, 32-byte hex) — descriptors reference secrets by id,
never plaintext.

> **AGE re-evaluation — DECISION (2026-06-23).** The knowledge graph lives
> **relationally** in the reified `nexuses` table (subject / `intermediary` /
> object / `rel_type` / signed polarity / intent / channel / confidence + the
> temporal lifecycle), and all graph *computation* — centrality, structural
> balance, proxy-chain sign-product mining, broker scoring — runs in **networkx,
> in-process** over that table (`data/analysts/deterministic_handlers/`:
> `structural_balance.py`, `graph_mining.py`). The Apache AGE graph `legba_graph`
> is **retained but dormant**: both AGE write-legs ship **off by default**
> (`emit_graph_edges=False` in `fact_extractor.py`; `LEGBA_AGE_DERIVED_FROM` off in
> `dapr_actors.py`), so live it holds only a handful of demo edges
> (**≈4.9k nexuses today vs ~10 AGE edges / 27 vertices** in the dormant AGE
> graph, the latter measured 2026-06-23). **No
> active analysis path depends on AGE** — the two analytic consumers pull their
> signed edges from `nexuses` (`_augment_from_nexuses`) and only *best-effort
> augment* from AGE; `/entities/graph` and provenance lineage are plain relational
> SQL (recursive CTE over `derived_from uuid[]`); and the #99 "Notable Structure"
> grounding block + agency `query_paths` / `find_proxy_chains` / `query_brokers`
> tools all run as recursive CTEs / networkx over `nexuses`.
>
> **Decision:** keep the relational `nexuses` graph as the **canonical** knowledge
> graph with networkx for compute; treat AGE as an **optional, currently-inert**
> acceleration path that the project does **not** depend on. **Rationale:** at the
> current scale (low thousands of edges) networkx over `nexuses` is simpler, fully
> featured, transactional with the rest of the substrate, and already powers
> grounding + agency queries + the operator surface (#99); a second graph engine
> (AGE Cypher, or a standalone Neo4j/Memgraph) would add a query language, an
> `agtype` text-parsing dialect, a per-connection session tax, and a sync pipeline
> for **no current benefit**. **Falsifiable revisit trigger:** re-evaluate adopting
> a native graph engine if the live nexus edge count exceeds **~250k** *or*
> multi-hop traversal latency in the recursive-CTE / networkx path exceeds **~2 s
> p95** for a routine analyst slice — i.e. when DB-side variable-length traversal
> would earn its operational cost. The underlying investigation additionally
> recommends *dropping* AGE outright as a follow-up cleanup (an operator-gated
> change).

| Store | Role | Status |
|---|---|---|
| **Postgres** (+ dormant Apache AGE) | canonical relational pool (46 tables) + provenance; **the operative knowledge graph is the relational `nexuses` table**, with graph compute in in-process networkx. The `legba_graph` AGE graph exists (9 vertex / 14 edge labels) but is **near-empty and off the critical path** — write-legs default-off (§5.5, "AGE re-evaluation") | **LIVE — relational pool is source of truth**; AGE dormant (`data/migrations/0001_baseline.sql`, `data/postgres.py`) |
| **NATS JetStream** | notification bus + durable streams + work queues; `legba_signals` (interest retention) + 4 registry-lifecycle streams + DLQ | **LIVE** (`data/nats.py`, `registry/streams.py`) |
| **Qdrant** | vector embeddings — `legba_signals` collection (1024-dim BGE-M3 cosine), per-target collections optional | **LIVE** (`data/stack/vector_store/qdrant.py`) |
| **Vault** | `stack_credentials` table, XSalsa20-Poly1305, versioned rotation | **LIVE** (`registry/credentials.py`) |
| **Redis** | hot state / caches — geocode cache, ingest-dedup, registry health, intelmq source | **LIVE as a cache** (`data/redis.py`, `data/filters/geocode.py`, `data/filters/dedupe.py`) |
| **SeaweedFS** | object store for retained media | **schema-slotted stack-component kind; NO handler shipped** |
| time-series metrics / full-text search | observability store + BM25 backing | **REMOVED — declared seams** (no metrics store; `search_signals` uses Postgres FTS; see SEAMS) |

The Postgres `signals` table is the **source of truth** (canonical, persistent,
queryable for batch reads and backfill, `data/migrations/0001_baseline.sql`);
NATS is the **notification bus** (transient, fan-out). The same observation lives in
both, which is exactly why real-time delivery and batch re-analysis are one
mechanism rather than two. NATS subject tokens cannot contain dots, so
`SourceDescriptor.id` is flattened by `subject_token()` (`data/nats.py:86-95`).

> **Reality check.** The Postgres/AGE + NATS + Qdrant + vault + Redis-as-cache
> set is what is actually exercised. SeaweedFS has a schema-slotted stack kind
> but **no live substrate handler** — it is a declared seam, not a running
> integration. (The former TimescaleDB metrics store and OpenSearch full-text
> backing have been removed outright; time-series metrics and full-text search
> are now declared seams — see SEAMS.) Earlier drafts listed all of these as if
> first-class; this doc corrects that.

The data-analysis rigor arc (§5.7) added three things to the Postgres substrate
(migrations live under `src/legba/data/migrations/`):

- **`facts` temporal columns** (`0032_facts_decay_columns.sql`) — adds
  `valid_until`, `superseded_by`, `confidence_components` to the existing `facts`
  table, drops the old full-triple unique index, and adds the partial **open-only**
  unique index `idx_facts_temporal_triple_open` on `(lower(subject),
  lower(predicate), lower(value), COALESCE(valid_from, '1970-01-01'))` scoped to
  `WHERE valid_until IS NULL AND superseded_by IS NULL` — so the single "what is
  true now" row per triple is unique while superseded history accumulates.
- **`nexuses` table** (`0033_nexuses.sql`) — the reified-relationship store:
  `subject` / `intermediary` (nullable; NULL = direct) / `object` / `rel_type` /
  `label` / `polarity smallint` (CHECK ∈ {−1, 0, +1}, `nexuses_polarity_ck`) /
  `intent` / `channel` / `confidence` / `valid_from` / `valid_until` /
  `superseded_by` / `derived_from uuid[]` / `source_signal_ids uuid[]` / `data
  jsonb` / the universal provenance columns, default `schema_uri
  iglu:legba/nexus/jsonschema/1-0-0`. It carries the same open-only partial
  unique index pattern (`idx_nexuses_triple_open`) and a signed-open index.
- **`seed_batches` ledger** + the seeding marker (`0034_seed_batches.sql`) —
  `seed_batches` (`source` / `kind` / `source_type` / `imported_at` / `counts` /
  `manifest`), plus `seed_batch_id` added to **both** `facts` and `nexuses` and
  `source_type` added to `nexuses` (the marker the curated-seeding path stamps;
  §5.7).
- **`situations` first-class** (`0040_situations_first_class.sql` +
  `0041_situations_valid_from_repair.sql` + `0042_situations_target_id_backfill.sql`)
  — promotes situations from mutable bottom-up snapshots to **temporal frames**: a
  real `situation_signature` column + a `UNIQUE (situation_signature, analyst_id)`
  upsert key (so the standard `write_situation` provenance path has an upsert
  target and "which situation owns this finding" is a plain join), the
  `valid_from` / `valid_until` / `superseded_by` temporal columns (0041 repaired an
  inverted `valid_from` backfill), and a populated `target_id` (0042). Situations
  are now the persistent FRAMES that serve as a grounding source and the **events
  substitute** — there is no `events` table; events = signals + `get_timeline`
  (which includes situation spans). This closes what earlier drafts called the
  Phase-5 "open frontier": situations are DONE, not a horizon target.
- **`source_poll_outcomes`** (`0046_source_poll_outcomes.sql`) — append-only
  provenance for every NON-productive source poll (empty HTTP-200-with-0-signals,
  or error), carrying the handler's own `health_state` diagnosis; the per-source
  liveness watchdog lateral-joins it to explain *why* a source went silent (§0).

### 5.6 Action-pack — *modular, allow-listed analyst agency*

Analyst capability is **granted, not hard-coded**. An `ActionPack`
(`schemas/action_pack.py`) is a registrable, versioned, content-hashed bundle of
*(tools + prompt fragments/rules + escalation channels + a per-pack governor +
an applicability predicate)*. Seed packs: `media_processing` (`process_media`),
`incident_response` (`escalate`/`create_incident` → channels), `substrate_read`
(the consult kind's four governed read tools), and `escalate_finding` (fires on
gated findings). (The `discovery` pack was retired per decision F-1.)

Effective capability is an **intersection**: an analyst declares `action_packs`
(what it *may* use); a target or domain template declares `allowed_action_packs`
(what the context *permits*); a pack declares its own `applicability`. A
capability an analyst requests but its domain doesn't allow cannot fire. Every
pack carries a `governor` (budget + rate caps). This is the sole agency-grant
surface; there is no flat tool whitelist.

### 5.7 The data-analysis rigor layer — *temporal-honest knowledge*

Above the per-signal findings sits a coherent **knowledge layer** built this arc.
Its commitment is the one the pre-pivot system had and the early pivot lost:
**facts and relationships are temporal and falsifiable, not snapshots**. The
pipeline reads bottom-up:

1. **Temporal facts** (altitude 0). The ingest-time **`fact_extractor`** stage
   (`data/filters/fact_extractor.py`, a descriptor-gated `pipeline.enrichment`
   filter, `kind="fact_extractor"`) reuses the GLiREL triples that
   `ner_multilingual` already put on the signal (falling back to the hosted
   `/extract` endpoint), rejects junk (numeric/date/unit endpoints, and an
   opt-in `reject_quantity_endpoints` gate), and writes each
   `(subject, predicate, value)` into the `facts` table with
   `source_type='ingestion'` and **`valid_from` = the signal's event time**
   (`_published_at_dt`/`_last_seen_dt`/`_event_dt`, else `fetched_at`). It is
   enabled on the three world feeds — `source_bbc_world.yaml`,
   `source_aljazeera_world.yaml`, `source_dw_world.yaml`. Before each insert,
   `supersede_prior_facts` (`provenance/writes.py:658`) closes any open row for
   the same `(subject, predicate)` whose **value differs** (`valid_until=now()`
   + `superseded_by`), so the single open row per triple *is* "what is true now"
   while history accrues. Analyst- and workflow-emitted facts share the same
   write contract via `write_fact` (`OutputKind.FACT`). When two credible sources
   genuinely *disagree* on a value (rather than one superseding the other), the
   default-OFF contested-claims subsystem (§5.9) lets the rows **coexist open** and
   records the dispute in a sidecar instead of silently dropping the loser.
2. **Reified typed signed Nexus** (altitude 0, relations). The
   **`relationship_reifier`** META analyst (§5.3) lifts raw co-mention edges into
   *typed, directional, signed* relationships: `subject →[intermediary]→ object`
   with a `rel_type` label, a canonical **polarity** sign (+1 supportive / −1
   antagonistic / 0 neutral — the structural-balance convention), an `intent`,
   and a `channel`. An 8B LLM does the typing; the row lands in `nexuses` via
   `write_nexus`, with the same valid-from / supersession lifecycle as facts
   (`supersede_prior_nexuses`, `writes.py:822`). Live examples: *Iran —HostileTo→
   Strait of Hormuz* (−1), *Musk —LeaderOf→ SpaceX* (+1), *Brazil —MemberOf→
   BRICS* (+1).
3. **Deterministic refinement** over the signed graph. Three deterministic
   sub-handlers (`data/analysts/deterministic_handlers/`), each now wired to a
   descriptor: **`structural_balance`** (signed-triad balance: balanced vs.
   frustrated triads, per-node frustration; it also owns the authoritative
   `POLARITY` edge-label→sign table), **`graph_mining`** (community detection,
   centrality, and **proxy-chain** sign-products through intermediaries), and
   **`nexus_decay`** (confidence decay for nexuses older than 30 days — the
   nexus-table sibling of `fact_decay`). `structural_balance` and `graph_mining`
   now persist their results through the `_graph_metrics_sink` helper
   (`deterministic_handlers/_graph_metrics_sink.py` — `write_graph_metric`), so
   the signed-graph metrics land as queryable rows rather than log-only output.
4. **Competing hypotheses + calibration** (altitude 2). The
   **`competing_hypotheses`** (alias `ach`) META analyst (§5.3) runs the
   **Analysis of Competing Hypotheses** structure: focal situations × current
   facts × signed nexuses → ≥2 mutually-exclusive hypotheses, each with a
   mandatory counter-thesis, laid out on a Heuer consistency matrix with
   diagnosticity weighting and integer evidence balance; ±2 transitions promote a
   hypothesis to `confirmed` / `refuted`. **The per-cell consistency matrix is now
   LLM-scored** (Heuer CC/C/N/I/II via the model plane, budget-gated, never
   litellm), with the deterministic lexical/polarity counter as the
   budget-exhausted per-cell fallback (each row records `matrix_scorer`); evidence
   is scoped to the resolved-entity set, not a substring (see §5.3). The
   **`calibration_tracking`** handler closes the loop with a Brier score over
   resolved hypotheses. The **exogenous** `resolved_outcome` column (migration
   0038) is **built and firing** — stamped against facts produced *after* the
   hypothesis (`resolved_by = 'subsequent_facts'`) or by an operator label
   (`resolved_by = 'operator:<id>'`), never the hypothesis's own evidence balance,
   so that the Brier can measure consistency against new evidence rather than
   self-consistency. The subsequent-facts auto-resolver now **ABSTAINS on
   undirected theses** (it previously auto-graded undirected facts TRUE, which
   inflated the headline rate); it runs alongside the live `status_transition`
   (self-consistency) tier (rows in that tier are flagged `self_consistency_only`).
   The **goal** remains a real Brier against resolved real-world outcomes.
   Residual caveats: the subsequent-facts auto-resolver is a coarse directional
   heuristic (the operator-label path is higher-fidelity), the gradeable
   directional resolution rate is modest, and a budget-exhausted run falls back to
   the lexical scorer. No proven-forecast-accuracy claim is made.

This whole layer is **temporal-honest** end to end: every fact and nexus carries
`valid_from` / `valid_until` / `superseded_by`, an open-only partial unique index
makes "the current value" a single queryable row, and a value/polarity change
closes the prior row rather than overwriting it — so the substrate answers both
"what is true now" and "what did we believe, when".

**Seeding (curated baseline).** A small primitive lets curated knowledge enter
the same tables without masquerading as live ingestion. Every fact and nexus
carries a `source_type` (`ingestion` / `agent` / `seed`) and an optional
`seed_batch_id`; both are threaded (default `None`) through
`write_analyst_output → _insert_for_spec → _insert_fact`/`_insert_nexus`
(`writes.py:128-129`), and the `seed_batches` ledger records each import's
`source`, `counts`, and `manifest`. The `src/legba/data/seed/` framework defines
a `SeedSource` protocol + a `SeedDriver`, driven by the `scripts/seed.py` CLI,
with **four** registered adapters: `world_baseline` (curated-YAML flavor-b
reference, zero network dep), `wikidata_leaders` (Wikidata-SPARQL current heads
of state/government → `LeaderOf` facts), `acled_conflict` (ACLED conflict-events
backfill → conflict-event facts + signed nexuses), and `sipri_arms_transfers`
(curated-SIPRI-YAML arms transfers → signed nexuses). A seed write stamps
`source_type='seed'` + the batch id so a batch is selectively refreshable /
purgeable and never confused with a real observation. Live today: seed batch
`414473c8`, ≈19 seed facts + 17 seed signed nexuses.

**Seed temporal honesty — `valid_until` now threaded end-to-end.** Curated
seeds enter with their `valid_from` (term start / accession) **and** a parsed
`valid_until` where the source carries one: `FactPayload` / `NexusPayload` now
**both carry a `valid_until` field** (`models.py:329`, `:371` — added by the
Phase-B write-path work), and the seed driver threads it through
(`seed/_driver.py:366-367`, `:409-410`). The remaining nuance is that a
*differing* live observation also supersedes a seeded row via
`supersede_prior_facts`/`supersede_prior_nexuses`, but expiry **purely** by a
seed's stored end date (with no superseding observation) is still not driven by
a background sweep — the row carries its `valid_until` and the current-facts
gate (`valid_until IS NULL OR valid_until > now()`) excludes it once that date
passes, so it is temporally honest at read time even without an active sweep.

### 5.8 Knowledge grounding — *the substrate IS the grounding store*

The analyst LLM plane has a **stale-cutoff** problem: a hosted model whose
training prior predates the present backfills current-world facts (who holds
office, which alliances are in force, the state of an ongoing conflict) from
that prior — e.g. calling the *current* US president a "former" one, because its
cutoff predates the 2024 election. The signal slice rarely *restates* such
background facts, so the model has no in-context correction.

The insight that closes this: **the data-analysis rigor layer (§5.7) already IS
the grounding store.** Temporal facts (`valid_from`/`valid_until`/`superseded_by`)
+ reified signed nexuses + seed roots are exactly a current-world-state model
with a single queryable "what is true now" row per assertion. The fix is not a
new store — it is (Tier 0) curating *current* data **in**, and (Tier 1)
**injecting** it at analysis time.

- **Tier 0 — current data in (seed adapters with temporal supersession).** The
  `wikidata_leaders` seed adapter (`data/seed/adapters/wikidata_leaders.py`)
  pulls **current** heads of state/government from the live Wikidata SPARQL
  endpoint and emits, alongside the subject=leader `LeaderOf` fact, a
  **country-subject** office fact `<country> | head of state | <leader>` keyed on
  the *country* (`_HEAD_OF_STATE_PREDICATE = "head of state"`,
  `wikidata_leaders.py:72`, mirrored by `world_baseline.py:50`). That shape is
  the supersession-correct one: because `supersede_prior_facts` keys on
  `(subject, predicate)`, a leader change on a country-keyed fact **closes the
  prior officeholder** (`valid_until = now()` + `superseded_by`) via the Phase-B
  `valid_until` write path — whereas a subject=leader fact could not, since its
  subject is the person. Both adapters use the same canonical predicate, so a
  fresh Wikidata pull supersedes a stale curated `world_baseline` leader for the
  same country.
  - **Bare-QID resolution.** Wikidata's SPARQL label service occasionally returns
    a bare `Qxxxx` id instead of a name (live-observed for some P6
    head-of-government rows — notably **Trump `Q22686`**, whose entity carries no
    English `labels.en` key at all). `_resolve_bare_qid_labels`
    (`wikidata_leaders.py:241`) gathers every bare-QID label cell and does one
    batched `wbgetentities` Action-API call, preferring `labels.en.value` and
    **falling back to the `sitelinks.enwiki.title`** — that enwiki-sitelink
    fallback is exactly what resolves `Q22686` → "Donald Trump". A QID neither
    path can resolve is left bare and **dropped** at `map` (never emitted as a
    `Qxxxx` value). Live-verified: US head of state = `Donald Trump` (since
    2025-01-20), current, superseding the QID.

- **Tier 1 — injection at analysis time (opt-in descriptor block).** A
  `GroundingBlock` (`data/schemas/analyst.py:522`) is an optional
  `AnalystDescriptor.grounding` field — `enabled` (default **`False`**), `scope`
  (`target_geo` / `slice_entities`, default both), `sources` (`substrate` today;
  `vector:world_context` accepted but inert — Tier 2), `max_facts` (default 30).
  Off unless declared, so no non-opted-in analyst pays a read. When enabled,
  `analyst_deps_builder._build_grounding_hook` (`analyst_deps_builder.py:378`)
  closes a `SubstrateGroundingResolver` (`runtime/grounding.py`) over the
  substrate `pg_pool` and installs a per-run hook. The hook (a) extracts
  candidate names from the in-memory slice + the run's `target_id`
  (`collect_grounding_candidates` — no DB), (b) resolves the **current**
  authoritative rows with the same temporal-honesty gate the analysis plane uses
  (`superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`),
  **preferring `source_type IN ('seed','curated')`** so a seeded ground truth
  outranks a hallucinated live fact, and **excludes bare-QID values in SQL and
  again in Python** (never injects an unreadable `Qxxxx` line), and (c) renders a
  dated **"AUTHORITATIVE CURRENT CONTEXT (as of `<today>` — treat as ground truth
  over prior knowledge)"** preamble (`build_grounding_preamble`). The
  `inline_target` runner's **GROUND** phase
  (`data/analysts/inline_target.py:592-612`) **prepends** that preamble to the LLM
  user prompt — degrade-not-drop: any resolver/read failure logs and yields
  `None` (no preamble), never failing the run, and an empty candidate set
  produces no header. Opted IN on the **four bounded units**
  (`analyst_leadership_transition.yaml`, `analyst_energy_security.yaml`,
  `analyst_escalation.yaml`, `analyst_narrative_coordination.yaml` —
  `grounding.enabled: true`, each drawing `sources: [substrate, situations,
  graph_structure]`). The per-country + world compositions do not re-ground: they
  synthesize over the already-grounded, faithfulness-verified unit findings (§5.10).
  - **Canary (live-verified).** A US assessment's context now contains
    "United States — head of state: Donald Trump (since 2025-01-20)".

- **Tier 2 — vector `world_context` collection.** A curated unstructured-brief
  collection is a **declared future seam** (it needs the embedder-through-port,
  L-114). The schema already accepts `sources: [vector:world_context]` so
  descriptors can pre-declare it, but the deps-builder logs and the resolver
  no-ops on any non-`substrate` source until that wiring lands
  (`analyst_deps_builder.py:419-425`).

Grounding is purely additive over the substrate: it is a couple of cheap
current-facts Postgres reads gated by a default-off descriptor field, injected as
a prompt preamble — no new store, no new dependency, and never a hard failure.

### 5.9 Contested claims — *the "alternate facts" referee (#101)*

The temporal-fact model of §5.7 answers "what is true now" with a **single open
row** per `(subject, predicate, value)` triple, and `supersede_prior_facts` keeps
that property by closing a prior open row whose value differs. That is the right
shape when sources *agree* — but when two credible sources genuinely **disagree**
on a value (the "alternate facts" problem), last-writer-wins silently discards
the loser, and the substrate forgets that the claim was ever contested. The
contested-claims subsystem (#101, Holes-B) makes disagreement a **first-class,
queryable, detect-only** fact about the substrate instead — it never destroys a
fact and never adjudicates one away; it *annotates* the dispute and *surfaces*
the better-supported value while leaving every competing fact row open.

The whole subsystem is **flag-gated and ships OFF by default** (both
`LEGBA_FACT_CONTENTION` and `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` default to OFF in
code *and* in `docker-compose.yml` via `${VAR:-0}`); they are enabled (`=1`) only
on this instance through the gitignored `.env`. The contested-claims schema landed
at migrations 0054–0055; the current migration **head is 0057**.

**Per-fact source credibility (Wave 0, migration 0054).** A `facts.source_credibility
real` column carries the trust weight of the most credible source backing a fact
(`0054_facts_source_credibility.sql`). NULL means *unknown* (it is never `0` — an
unscored fact must not be treated as untrustworthy); both fact producers now stamp
it, resolved as the **MAX** over the backing signals' `signals.source_credibility`
else the tier nominal (seed/curated `0.9`, ingestion/agent `0.5`). Wave 0 also
landed two latent holes-A fixes on the ingestion write path: the ingest fact-writer
now passes `incoming_source_type` into `supersede_prior_facts` (so the A1 tier guard
is no longer bypassed — an ingestion fact can no longer close a seed/curated row),
and same-value merge now uses **noisy-OR** rather than `GREATEST` (matching the
analyst path).

**The contention sidecar (Wave 1, migration 0055).** Disagreement is recorded in a
**sidecar**, never by mutating a fact's truth columns: `fact_contention` (one group
per canonical `(subject_key, predicate_key)`, lifecycle
`contested → surfaced → collapsed`) and `fact_contention_values` (one row per distinct
non-junk value cluster). Three thin markers are added to `facts` —
`contested boolean`, `contention_id uuid`, `surfaced_winner boolean` — which are the
only `facts` columns the arbiter ever writes. The sidecar is fully **recomputable from
the open facts**; additive DDL (`CREATE TABLE / ADD COLUMN IF NOT EXISTS`) leaves every
existing row unmarked and uncontested.

**The arbiter (Wave 2) — a detect-only deterministic META analyst.**
`fact_contention_arbiter`
(`data/analysts/deterministic_handlers/fact_contention_arbiter.py`) is a new
**`deterministic`-kind** sub-handler — registered in `deterministic.py`
(`SUB_HANDLERS` + `TRACE_ONLY`) with the descriptor
`descriptors/analyst_fact_contention_arbiter.yaml` (hourly at `:37`, offset from the
other short deterministic handlers). It is **TRACE_ONLY**: it produces no
`analyst_outputs` row, only the sidecar + the three `facts` markers. Its core
invariant is **DETECT-ONLY (decision B15): it NEVER mutates a fact's
`value` / `valid_until` / `superseded_by` / `confidence`, and never calls
`supersede_prior_facts`** — the open competing rows are left exactly as they were.
Each pass:

1. **Scan open facts** (`valid_until IS NULL AND superseded_by IS NULL`) and bucket
   them by canonical `(subject, predicate)`, keeping only groups with ≥2 open rows.
2. **Fuzzy-cluster** each group's values (`provenance/value_clustering.py`:
   `canonicalize_entity` to fold demonyms/aliases, then normalized-Levenshtein under
   a **tight `FUZZY_MERGE_MAX_DISTANCE = 0.12`** ceiling) — so a typo / spacing variant
   merges (Russia/Russian, Kyiv/Kiev) while two genuinely different values stay split
   (North Korea vs South Korea). This fuzzy grouping is the move that un-dormants both
   holes-A's noisy-OR corroboration and holes-B's per-value aggregation, which were
   inert under exact-string grouping (real sources phrase the same fact differently).
3. **Junk-gate** each value cluster through the *existing* `fact_extractor` gates
   (`is_junk_entity` / `_is_inverted_relation` / reflexive- and
   containment-inversion checks). A rejected cluster is **operator-reportable** with its
   gate name (`is_junk` / `junk_reason`), not silently dropped — and it is excluded from
   the dispute (so the Poland→{Berlin, Russian} junk case is junk-dropped, *not* treated
   as a genuine disagreement).
4. **Score** each surviving value cluster `Q·C·R·F` (`_arbiter_score = q * c * r * f`,
   multiplicative): **Q** = quorum (its share of distinct *source lineage*, so a chatty
   single source can't manufacture quorum), **C** = its share of the group's
   `source_credibility` mass (NULL credibility is unknown, summed as nothing — never 0),
   **R** = exponential recency with a **30-day half-life** (one bounded factor, *not* the
   sole decider — the core fix versus last-writer-by-recency), **F** = mean confidence.
5. **Surface at most one winner** per `(subject, predicate)` group — it flips that
   value's `surfaced_winner` marker — **or abstain**: a best cluster below
   `MIN_SURFACE_SCORE = 0.15` is a "weak" abstain (honestly disputed, no resolution),
   and two top clusters that both clear the floor but where the best doesn't beat the
   runner-up by `DOMINANCE_RATIO = 1.25` is a "near-tie" abstain. The deterministic
   winner selection is a total order (distinct-source → credibility → recency →
   `value_key`), so two passes over unchanged data pick the same winner (idempotent).

**The optional vLLM-only near-tie tie-break (Wave 2b).** A bounded LLM tie-break may
run **only on a near-tie abstain** (never on a weak abstain, never to second-guess a
deterministic winner). It is double-gated: `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` must be
ON (default OFF) **and** the descriptor must declare a `method.llm.primary` — and the
deps-builder **hard-refuses an Anthropic/Opus primary** for this path
(`analyst_deps_builder._is_anthropic_component`), so the tie-break can route *only* to
the **self-hosted `llm.primary.openai_compat` vLLM plane** (the billed Anthropic plane
stays reserved for consult/deep). It is bounded — 256 tokens, a 30s call timeout, at
most `MAX_LLM_TIEBREAKS = 10` calls per pass — and **degrades to abstain on any
failure**. The receipt splits `llm_tiebreak_calls` (consultations) from `llm_tiebreaks`
(successful picks). Even a tie-break "pick" surfaces through the *same* sidecar +
marker path; it still **never** touches a fact's truth columns.

**Write-path coexistence (Wave 4, flag `LEGBA_FACT_CONTENTION`).** For the arbiter to
*see* a same-tier disagreement, both values have to stay open — so a small,
flag-gated carve-out lives inside `supersede_prior_facts` (`provenance/writes.py`):
when a same-tier incoming value is **fuzzy-DISTINCT** from an open prior
(`cluster_values([incoming, prior])` yields more than one cluster), the prior is **not
closed** — the rows coexist open and the detect-only arbiter groups them on its next
cadence. (A fuzzy-*same* value — "Russian" vs "Russia" — still merges as before; a
cross-tier upgrade still supersedes.) Both fact producers route through
`supersede_prior_facts`, so the carve-out covers the ingestion and analyst/workflow
paths alike. With the flag OFF, supersession is unchanged (pure holes-A
last-writer-wins).

**Surfacing (Wave 5) — read-only.** Once a fact is contested, four read paths *tell*
that, none of them altering a fact:

- **Grounding annotation.** The §5.8 grounding preamble adds a `CONTESTED` / `DISPUTED`
  line (`runtime/grounding.py`) — respecting the existing provenance gate, so only
  seed/curated facts reach it, and the annotation is the only addition. A surfaced
  winner renders "(CONTESTED: N sources disagree; surfaced winner)"; a contested fact
  that is *not* the surfaced winner is annotated as disputed.
- **ACH evidence.** `competing_hypotheses` emits a `contested_fact_value` evidence item
  per live contention group whose subject is in the topic's resolved-entity set, so the
  Heuer matrix sees the disagreement as evidence.
- **Read API.** `GET /api/v1/contention` (`registry/substrate_reads_api.py`) serves the
  contention groups + their value clusters.
- **UI.** A `ContestedBadge` component (`legba-ui-v3/src/v4/components/ContestedBadge.tsx`)
  is mounted in the **Why** provenance trail (fact-keyed) and the target **Claims** panel
  (subject-keyed).

**Validation status (honest).** The detect-only arbiter, the Wave-4 coexistence, and
the Wave-5 read API are **proven live** (a synthetic same-tier dispute drove
coexistence, `Q·C·R·F` surfaced the better-supported value, the detect-only invariant
held — `valid_until`/`superseded_by` stayed NULL on both rows — and the read API
surfaced the group). The Wave-2b vLLM tie-break is **proven consulted live** (it was
called on a near-tie and correctly *abstained* on symmetric evidence — provenance-first
is correct), but a successful LLM **pick** (`llm_tiebreaks ≥ 1`) is **unobserved live
so far** (it awaits an asymmetric near-tie in the soak).

### 5.10 The analysis spine — units → composition → world → scorecard, verified

This is the **product**: the decompositional chain that turns the enriched signal
pool into cited, verified, drillable reads. It is composed bottom-up, and every
first-order claim passes a **mandatory faithfulness verify pass** before anything
above it may consume it.

**1 — Four bounded reasoning UNITS.** Four `inline_target` LLM analysts —
`leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`
(`descriptors/analyst_*.yaml`, `method.kind=llm_planner`, core plane
`llm.primary.openai_compat` = self-hosted gpt-oss-120b, $0) — each answer **one
narrow question** and are scoped to every desk by a single coverage-tag fan-out
`has_tag("g20") or has_tag("watch")` (24 desks: the 19 G20 country desks + a
5-country high-consequence `watch` tier — Israel, Iran, Ukraine, Taiwan, North
Korea, descriptor ids `country_watch_il/ir/ua/tw/kp`; adding a country is
register-a-target, no code — §7.2). Each run: **ASSEMBLE** a cited 72h raw-signal
slice + the §5.8 grounding preamble of ACCUMULATED facts/nexuses/situations (e.g.
"US head of government Trump since 2025-01-20; US–Iran active conflict since
2026-02-28; NATO member since 1949"), so the system integrates over time, not just
today → cited **SYNTHESIZE**
(a strict-JSON finding whose prose carries `[N]` citation markers mapped to signal
ids) → the **VERIFY** pass below → an `effective_confidence` fold + drill-to-source
provenance. Skill is a **per-unit** number (§ skill scoreboard), never a platform
boast.

**2 — The mandatory faithfulness verify pass.** Every cited finding is scored for
**faithfulness ∈ [0,1]** — *does each fact-asserting claim follow from its cited
evidence?* — by `verify_finding_faithfulness` (`data/provenance/verify.py:263`).
Two components:

- a **deterministic citation-presence floor** (always on): every fact-asserting
  claim in the prose is checked against the resolved `data['citations']` bridge; a
  claim that asserts a fact with **no** `[N]` marker, or whose marker resolves to
  no real `signal_id`, is an **unsupported** span, and the score is the fraction of
  checkable claims that are supported;
- an **LLM judge** — declared per-unit as `method.llm.verify` — that refines the
  per-claim verdicts. **Currently this judge is the SAME core reasoning model**
  (`llm.primary.openai_compat`, gpt-oss-120B) that writes the units and
  compositions, **not** a cross-family model. This is a deliberate, temporary
  choice: the earlier cross-family 8B ("legba-slm", `llm.verify.slm_8b`,
  Llama-3.1-8B) proved too weak (harsh + mis-aimed), so the strong reasoning model
  runs the judging to prove the flow. **Known limitation:** a model verifying
  prose from its own family shares its blind spots, so the faithfulness signal is
  weaker than an independent cross-family judge — the deterministic floor and the
  signed provenance chain still backstop it, and a dedicated reasoning judge is
  planned. It is **soft-fail**: when the judge flag is off or the judge
  is unreachable the result **degrades to the deterministic floor** and is labelled
  `judge-unavailable` (`judge_status`), never a fabricated number.

The verdict is persisted as a `critique`, and the fold
`effective_confidence = min(confidence, faithfulness_score)` is applied **at read
time**. Verification **never hard-deletes** a finding — a low score gates a visible
low-confidence tier. This is deliberate: Legba **measures
groundedness, not truth**. A planted fabrication with no supporting citation is
flagged unsupported.

**3 — Per-country composition.** `country_composition`
(`descriptors/analyst_country_composition.yaml`, the `meta_findings_synthesizer`
kind fanned out per desk on the same `has_tag("g20") or has_tag("watch")` coverage
tag) reads the four verified units for its country and
writes a hedged, cited synthesis. Its read slice admits **only verify-passed
sub-claims above the floor** — an **INNER JOIN on the faithfulness critique** — so
an unverified sub-claim can never enter a composition. A country whose four units
produced no verify-passed sub-claim yields an empty slice → the kind emits a
`confidence=0.0` "no source findings to synthesize" finding rather than inventing a
read.

**4 — World composition.** `world_assessor` (the same `meta_findings_synthesizer`
kind running **globally**, `analyst_world_assessor.yaml:44`) composes over the
per-country compositions into a cited, hedged world view that drills country →
units → source. It **graduated into** this role — it is **not** the retired
verdict-from-nowhere monolith (that framing is gone).

**5 — Banded scorecard.** `scorecard_producer`
(`data/analysts/deterministic_handlers/scorecard_producer.py`, a `deterministic`
META, the **12th OutputKind `scorecard`**, $0 SQL) writes **one banded row per
active desk** — it enumerates any active target tagged `g20` or `watch` — from
high-precision RULES over already-verified claims banded across a **14-day window**
(severity tag × `effective_confidence`, **demote-never-promote**). Every band
**names the verified-claim id it rests on** (`derived_from`, a §12 lineage walk
resolves them, zero dangling); a dimension with **no qualifying verified claim**
reads `insufficient-evidence` with an explicit reason (never a fabricated band);
and a per-claim faithfulness below the floor demotes to `low-faithfulness`. It
never omits a country — an all-insufficient card is still emitted. **Honest today:**
the live scorecard is a **mix** — some countries band, and some (e.g. the US) read
**all-insufficient because their unit faithfulness is genuinely low**, which is the
system telling the truth about itself, not a gap.

**6 — Skill scoreboard.** A per-unit eval surface (`GET /api/v1/v3/eval/calibration`)
reports, each **honestly**: per-unit **faithfulness**; per-unit
**correctness-vs-reference** via `unit_correctness_scorer` against operator gold
labels (migration 0057 `unit_reference_labels`) — **honest-null** where a unit has
no labels, and the gold set is **tiny today (n=1, reported insufficient-sample)**;
the **exogenous calibration Brier** (`calibration_tracking`, §5.7); and the
**acute-forecast BSS**. A no-skill / insufficient-sample result is **published, not
hidden**. A companion observability route `GET /api/v1/v3/eval/analyst_runtime`
reports per-analyst run timing (count, avg/max wall-clock seconds, last run,
non-success) read from `analyst_traces` (`data/registry/v3_api.py:1267`).

**Forecasting returns only as a measured scoreboard.** `forecast_scoreboard`
(`data/analysts/deterministic_handlers/forecast_scoreboard.py`, a `deterministic`
META, weekly) drives the pre-registered acute-binary pilot: issue one binary
forecast per covered desk per weekly window → **exogenously** resolve by the
upstream event time → count. A **degenerate / geography-dominated probability
vector ABSTAINS** (zero rows). The numbers surface **only** on the calibration
scoreboard, **never** as a free-text finding/prediction/claim (the per-run
`FindingPayload` is a `TRACE_ONLY` receipt). It currently reports **NO proven
skill** — the project earns the word "forecast" only once the BSS is positive on a
non-degenerate at-sample pilot, and not before.

## 6. The four planes

The system decomposes into four decoupled planes — `source_first_runtime.py`
assembles the latter three on top of the substrate. The seam that makes
everything work is the split between **acquisition** (ingest-once) and
**analysis** (match-many).

### 6.1 Acquisition — the inline analysis Tier (Tier 1)

This is the **TIER-1 INLINE per-signal pipeline** of §0: it runs *synchronously,
once per Signal, at acquisition, BEFORE fan-out*, and is **deterministic / local
NLP — no analyst LLM**. Its writes are altitude-0 substrate: the **enriched
signal** (in place on the one `signals` row), altitude-0 **`facts`**
(`source_type='ingestion'`), and **entity rows + `signal_entity_links`** off the
NER spans. Everything downstream of fan-out (the cadence/slice analysts) is
Tier 2.

A `SourceActor` polls on a Reminder or wakes on an inbound webhook
(`runtime/source_actor.py`). Its `pull_once` → `_process_one`
(`source_actor.py:482`) runs the source's baseline pipeline
(`run_baseline`, `data/sources/baseline.py:242`) to produce **one** canonical,
target-agnostic signal. Baseline enrichment runs in three tiers
(`baseline.py:282-294`): tier-1 structured (always, deterministic), tier-2 eager
media (opt-in via `pipeline.media`), tier-3 NLP enrichment stages
(`descriptor.pipeline.enrichment` → `language_detect → language`,
`geocode → geo`, `classify → tags`, `ner_multilingual → entity_classes`). The
enrichment chain is wired from the descriptor at host bring-up by
`enrichment_factory` (`source_first_runtime.py:181-203`) into a `PipelineRunner`
(`runtime/pipeline.py:76`). (Source descriptors reach the registry two ways: the
operator-pinned `descriptors/source_*.yaml`, **plus** the 46-entry **catalog**
(43 `rss` + 3 `geojson`) in `scripts/bringup_register_source_catalog.py`
registered directly into `source_descriptors` — NWS, NASA EONET, WHO/CDC/HRW, RSS
feeds — so the full live source set is the DB rows, not the YAML files; see
`docs/DATA_SOURCES.md` for the catalog table and the 3 / 46 / 49 scope model.) **Enrichment mutates the signal in
place** — there is
no separate enrichment table; the enriched signal is written canonically to the
single `signals` table (`source_actor.py:520-525`), with the structured columns
indexed for subscription pushdown. The signal is then published once to a coarse
NATS subject `legba.signals.<tenant>.<source_token>.<modality>.<event_class>`
(`data/nats.py:98-115`).

> **Note:** `language_detect` and `geocode` are deterministic (pure-Python +
> pycountry/Nominatim with Redis cache). `ner_multilingual` and `classify` call
> the hosted `legba-models` service (GLiREL-large `POST /extract`, DeBERTa-v3
> zero-shot `POST /classify`) — these are remote model calls. The altitude-0
> **`fact_extractor`** stage now slots into exactly this chain next to NER (§5.7):
> it reuses NER's GLiREL triples, stamps `valid_from`, and writes `facts` rows. It
> is live on the BBC / Al Jazeera / Deutsche Welle world feeds.
>
> **Source credibility at ingest.** `signals.source_credibility` was previously
> 100% NULL because the `source_credibility` pipeline filter only runs when a
> descriptor *binds* that kind (the live descriptors don't), while the
> credibility table is scored for ~65 hosts. `SourceActor` now backfills the
> column at write time via a host lookup against the `source_credibility` table
> (`source_actor.py:340` `lookup_source_credibility`, applied at `:406-408`), so
> the score is populated even for sources that didn't declare the filter.

### 6.2 Analysis (predicate fan-out)

The **subscription engine** (`runtime/subscription/engine.py:71`) resolves each
active target's `SourceRef`s into authorized bindings — `resolve_source_refs()` →
`enforce_subscription()` (policy check against the source's
`subscription_policy`) → `subject_filters_for()` → `ensure_durable_consumer()`.
It binds **one per-target aggregated JetStream consumer** subject-filtered to
that target's coarse axes, collapsing N×M per-(target,source) consumers toward N.

Signal matching is **two-stage** (`subscription/filter.py:62-108`):

1. **Structured SQL `WHERE`** on indexed columns — `geo`/`tags`/`entity_classes`
   via GIN overlay, `languages`/`modalities` via btree — pinned to the bound
   `source_id` + `owner_tenant`.
2. **Starlark residual** on the narrowed set (`residual_matches()`,
   `filter.py:194-219`) — compiled and cached in an LRU (`compiler.py`), evaluated
   under a **5ms SIGALRM wall-clock budget**, expression-only (no `def`/`load`/
   `lambda`, ≤4KiB, banned tokens rejected). Step-cap / memory-cap are an
   acknowledged gap in the `starlark-pyo3` binding, mitigated by the
   expression-only gate + wall-clock budget (`compiler.py:36-42`).

> **Cost rule:** immediate per-signal invocation is for *cheap*
> reactions only (deterministic detectors, dedup, severity-gated alerts).
> Expensive (LLM) analysis is always coalesced; the cooldown is the cost
> governor. LLM analysts fire reactively on the accumulation / cadence gates,
> never per-signal.

> **Bounded per-run dedup (matters for the actor-invoke budget).** The
> `cross_source_dedup` deterministic analyst does **not** re-scan the whole
> `signals` table every cadence. Its candidate query (a) skips content-hash
> groups already fully canonicalised — in the DB, via
> `HAVING COUNT(*) > 1 AND COUNT(*) FILTER (WHERE canonical_signal_id IS NULL) > 0`
> (`cross_source_dedup.py:193-194`) — and (b) is capped at
> `max_groups_per_run` (`DEFAULT_MAX_GROUPS_PER_RUN = 500`,
> `cross_source_dedup.py:90`) with a stable `ORDER BY content_hash`, so each run
> does bounded work and the backlog drains across successive frequent cadences.
> This is the real fix behind raising the actor-invoke timeout
> (`LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS`, default 180s) — the cap keeps a run
> inside the budget without changing the dedupe result for any group it
> processes.

### 6.3 Async jobs

A NATS **work-queue with competing-consumer workers** (`JobWorkerPool`) backs
bounded, stateless, interchangeable jobs. `process_media` is the live job kind.
An analyst with the right action-pack enqueues a `process_media` job mid-
reasoning; the worker pulls and processes the artifact; the result lands as a
**derived signal** (`derived_from` → the raw signal) which re-enters the
fan-out → trigger path — heavy extraction is paid for only when reasoning needs
it.

> **Seam:** the job plane — queue, durable consumer, worker pool — is live, the
> `process_media` envelope + governed agency tool exist, and a completed job's
> derived signal re-enters fan-out. What remains a seam is the extraction service
> itself: with no `LEGBA_MEDIA_API_URL` configured, eager and on-demand
> extraction refuse loudly (typed `MediaEndpointNotConfiguredError`, no row
> written, `source_actor.py:408-423`).

### 6.4 Substrate

The shared stores of §5.5. Both the acquisition and analysis planes read and
write it; the async plane feeds derived signals back into it.

## 7. The runtime — the Dapr virtual-actor model

The runtime turns descriptors into running work via **Dapr virtual actors** plus
a reconcile loop that watches registry events and activates/retires actors to
match the registry. A virtual actor is addressable, turn-based (one invocation at
a time per id), reminder/timer-driven, and scale-from-zero. Parallelism is
*across* actors, distributed over runtime replicas by Dapr's placement service.
The whole runtime collapses to a **single control plane** — one `daprd` per
process (the seam that retired the Temporal backend).

Actor id grammar is `kind::descriptor_id::tail` (`dapr_actors.py:558-579`); for a
primary actor `tail` is the descriptor content-hash, for a worker actor `tail` is
the `target_id`.

### 7.1 SourceActor

Per source descriptor. Owns its poll Reminder (or webhook wake), runs the
baseline pipeline (§6.1), and writes + publishes one canonical signal. Event-time
for cursor advancement lives on `payload._published_at_dt` (RSS) / `_last_seen_dt`
(OpenSanctions) / `_event_dt` (generic), falling back to `fetched_at`
(`source_actor.py:194-208`).

### 7.2 AnalystActor — cadence reminder + fan-out

This is the heart of the analysis runtime (`AnalystActor`,
`dapr_actors.py:1158`; `run()` at `:1590`). Its lifecycle:

1. **PRIMARY owns the cadence.** On `_on_activate`, `cron_to_reminder_timing()`
   (`dapr_actors.py:1238`, from `descriptor.cadence.fallback_schedule`) registers
   the durable `run_cadence` reminder. A stale-fire self-disarm guard
   (`reminder_guard_decision`, `:582`) unregisters on version bump, skips when
   paused/error.
2. **Cadence tick → target matching.** `receive_reminder` (`:1295`) →
   `_reminder_guard` → `_cadence_targets()` (`:1464`) evaluates the
   `subscription.targets` Starlark predicate (`ANALYST_SUBSCRIPTION` surface,
   e.g. `has_tag('g20') or has_tag('watch')`) against the active target
   descriptors. **One selector binds all 24 desks** (the 19 G20 targets + the
   5-country `watch` tier) — no per-target enumeration.
3. **Fan-out (A2 concurrency).** `_fanout_to_workers()` (`:1351`) chunks the
   matched targets at `_FANOUT_CHUNK = 5` (`:548`) and dispatches **one run per
   matched target** to a distinct **per-(analyst,target) worker actor**
   `analyst::<descriptor_id>::<target_id>` (`_worker_actor_id`, `:558`), via an
   `ActorProxy.run({target_filter: tid})`, bounded by a `Semaphore` to avoid a
   thundering herd. Workers **lazy-activate** with the `target_filter` in hand,
   carrying no separate cadence — the primary owns the heartbeat.
4. **Per-target cooldown + slack.** Each worker gates on a per-target
   `cooldown_by_target[target_filter]` (`:1648`), absorbing **5%-of-cooldown
   slack** (capped 600s, `:1662-1665`) to fix drift when `cooldown_seconds ≈
   cadence interval` (the `6h→12h` bug fixed by commit `cefd8ca`).
5. **Read slice.** `_read_substrate_slice(descriptor, target_filter)`
   (`:3005-3050`) reads the kind's substrate window — default 24h signals, or the
   descriptor's `subscription.targets.time_window` (e.g. `336h`,
   `:3006-3027`). Kind-specific overrides exist (the `critic` reads
   `analyst_outputs` rows by id instead of signals).
6. **Kind dispatch.** `build_analyst_run_method` (`analyst_deps_builder.py:99`)
   has resolved the kind's module, LLM handler, and deps bundle at deps-build
   time; `run()` invokes the 3-arg `run_method(inputs, options, kind_deps)`.
7. **Budget gate + retry + demotion.** `budget.precall_check` projects token
   overrun → `throttle`/`exhausted`/`global_exhausted` with an audit trail and
   demotion-state setting; a transient-exception retry loop (exponential backoff,
   max 3, `_classify_exception` bucketing budget/transient/hard) wraps the call.

> **Declared seam:** the demotion path logs a *pause-until-reset* instead of a
> real cheap-model fallback (`fallback_run_method` is wired but the
> `demote_and_continue` strategy is a documented seam, `docs/SEAMS.md` F-2,
> `dapr_actors.py:1770-1794`).

8. **Output dispatch.** §8.

Scheduling is Dapr Reminders (fixed period) plus the trigger engine's in-process
cadence ticker; there is no Dapr Jobs (cron) integration.

## 8. Outputs — the provenance + write paths

When `run_method` returns, the actor selects the payload for the descriptor's
declared `OutputKind` and writes it through the universal provenance path.

### 8.1 The OutputKind enum (the REAL members)

`OutputKind` (`data/provenance/kinds.py:64-108`) has **exactly twelve** members:

```
finding · situation · hypothesis · prediction · alert
meta_finding · critique · fact · nexus · prompt_module_candidate
journal · scorecard
```

`fact` and `nexus` are the knowledge-layer kinds added by the data-analysis
rigor arc (§5.7). Each is fully plumbed exactly like `hypothesis`:
`OutputKind.FACT` / `write_fact` / `facts` table (`kinds.py:173-179`,
`writes.py:385`) and `OutputKind.NEXUS` / `write_nexus` / `nexuses` table
(`kinds.py:180-186`, `writes.py:416`). The fact path is dual-producer: the
ingest-time `fact_extractor` stage writes source-owned facts
(`source_type='ingestion'`, no analyst_id) through its own lower-level
`_insert_fact`, and `write_fact` is the analyst/workflow path — both share one
write contract. `fact_decay` and `nexus_decay` are the temporal-lifecycle
maintenance handlers that UPDATE pre-existing rows.

`journal` is the 11th kind and the one exception to the "kind = a node in the
lineage" rule. `OutputKind.JOURNAL` / `journal_entries` table (dedicated,
migration 0048; Iglu `iglu:legba/journal/jsonschema/1-0-0`; payload
`JournalPayload`) is Legba's **first-person reflective voice** — it is **OFF
the fact/finding/nexus chain**. A journal row is a *perspective over* the
provenance chain, never a *member of* it: it carries an **always-empty
`derived_from`** and the `journal_entries` table is deliberately **absent from
the lineage catalog** (`lineage_api._SUBSTRATE_TABLES`), so a downstream lineage
walk from any fact / situation / nexus can never surface a journal node. The
journal must **never** write a fact / finding / nexus — a gating test enforces
the invariant (`tests/data_pkg/test_journal_off_chain.py`). See §8.4 for the
producer (the `journal_assessor` META analyst).

`scorecard` is the 12th kind — the banded per-desk verdict of §5.10.
`OutputKind.SCORECARD` lands one row per active desk (every target tagged `g20` or
`watch`) in the generic `analyst_outputs` table, produced by the deterministic
`scorecard_producer`. It is
a *perspective over* already-verified sub-claims: its `derived_from` **names** the
verified basis findings each band rests on (a §12 lineage walk resolves them, zero
dangling), and **no band ever exists without a real basis id** — an
insufficient-evidence dimension carries an explicit, empty-but-honest basis rather
than a fabricated band.

There is also deliberately **no `signal` kind** — signals are source-owned rows
written by the canonical ingestion path, never routed through this registry
(`kinds.py:64-74`).

### 8.2 The write path

`write_analyst_output` (`data/provenance/writes.py:115`) is the generic wrapper:

1. `spec_for_kind` (`kinds.py:172`) looks up the kind's table + payload model +
   schema URI + NATS subject.
2. Pydantic validation; a `ValidationError` routes to the
   `output_dead_letter` table via `route_to_output_dead_letter`
   (`provenance/dlq.py`) — invalid output never half-lands.
3. `ProvenanceFields.from_analyst()` (`provenance/_core.py`) builds the universal
   provenance: `produced_at`, `derived_from` (uuid[]), `schema_uri` (Iglu).
4. `_insert_for_spec` (`writes.py:450`) routes the INSERT:
   `situation` → `situations`; `hypothesis` → `hypotheses`; `fact` → `facts`
   (open-only upsert + `supersede_prior_facts`); `nexus` → `nexuses` (open-only
   upsert + `supersede_prior_nexuses`); `journal` → `journal_entries` (its own
   `_insert_journal_entry`, `writes.py:1231`; a consolidation runs
   `supersede_prior_consolidation` first, and `derived_from` is forced empty —
   the off-chain invariant, §8.4); everything else
   (`finding`/`prediction`/`alert`/`meta_finding`/`critique`/
   `prompt_module_candidate`) → the generic `analyst_outputs` table. The
   `facts`/`nexuses` routes honor the `source_type` / `seed_batch_id` seeding
   markers (§5.7); other kinds ignore them.
5. NATS publish to `analyst.<analyst_id>.<channel>` — channel mapped by
   `_NATS_CHANNEL_BY_KIND` (`dapr_actors.py:2368-2375`: FINDING→findings,
   SITUATION→situations, …). `_safe_publish` swallows broker hiccups so the
   substrate INSERT is the source of truth.

### 8.3 Receipt chain + emit-bindings

After the row is written, the actor records a **chain-consistent (single-node)
SHA-256 receipt chain** over the canonical JSON of the run, threading
`intermediate_steps` + `tool_calls` (`compute_receipt_hash`,
`provenance/_core.py:352-383`; chained in `dapr_actors.py`) — distinct from, and
complementary to, the Ed25519-signed *registry* audit log (the Ed25519 signing is
on the descriptor audit log only, never on analyst outputs). It then dispatches to **output-kind emit handlers**
(`_emit_output_bindings`, `dapr_actors.py:2565`) discovered via
`discover_output_kinds()`. Two emitters are live:

- **STIX 2.1 bundle** (`data/outputs/stix_bundle.py`) — FindingPayload→Report,
  SituationPayload→Incident+Report, HypothesisPayload→Report[analysis],
  AlertPayload→Indicator|Report; published to `legba.outputs.stix.<target_id>`.
- **`alert` sinks** (`data/outputs/alert.py` + `alert_sinks/`) — coerces the
  live FindingPayload into a severity-gated `AlertPayload` and fans out on the
  severity ladder (NATS always; Pushover at medium+; XMPP/Matrix at high+ if the
  extras are installed). `_emit_output_bindings` threads the `pg_pool` and the
  persisted output row id (`OutputContext.alert_row_id`) so the alert sink writes
  its `alert_sink_deliveries` audit rows — the **2026-06 audit-plumbing fix**: the
  sink now writes `alert_sink_deliveries` rows when it fires (the prior
  `alert_sink_deliveries=0` was the missing plumbing, not a missing path).
  **Residual caveat:** successful-delivery audit and error audit remain in separate
  columns — there is no single unified delivery-status view yet. The `alert` emit
  path + the `alert_sink_deliveries` audit are live and the binding is a
  per-descriptor `outputs` block (a `min_severity: high` binding demonstrated it on
  the now-retired `country_assessor`); it can be re-declared on any live producer,
  so a high-severity finding reaches the operator sinks wherever the binding is
  currently attached.

Findings that cross the escalation gate additionally fire the `escalate_finding`
action pack through the governed agency pipeline (`_maybe_escalate_finding`). The
gate is a **dual OR test** (`escalation_gate_decision`, `agency/binding.py`): it
fires when an explicit finding severity is **≥ high** *OR* finding confidence is
**≥ 0.85** — an unknown/absent severity never fires on its own (conservative), so a
finding with neither a high severity nor 0.85 confidence does not escalate.

### 8.4 The journal — the off-chain reflective voice

The `journal` OutputKind (§8.1) is produced by **`journal_assessor`**, Legba's
**first-person reflective voice** — the one analyst pointed at the *whole
organism* (its own self / state / flow). Every other meta-analyst cuts one slice;
the journal narrates a coherent point of view that cuts **across the entire
flow**. Its thesis: *"Poetry without evidence is noise. Evidence without
perspective is just a log file."* **LIVE** — deployed and live-validated (a real
off-chain entry, `honesty_flags` forced deterministically from substrate metrics,
receipt-chained, in-voice).

**Off-chain, by design.** The journal is a
*perspective OVER* the provenance chain, **never a node in it**. A journal row
carries an **always-empty `derived_from`** and `journal_entries` is deliberately
**absent from the lineage catalog** (`lineage_api._SUBSTRATE_TABLES`), so a
downstream `derived_from` walk from a fact / situation / nexus can **never**
surface it. When the lineage of §12 is enumerated, the journal is the explicit
exception — it does **not** sit inside `signals → entities/facts →
relations/nexuses → situations → assessments`; it is a reflective layer
*above / across* that chain. It must **never** write a fact / finding / nexus
(gating test `tests/data_pkg/test_journal_off_chain.py`), and the grant layer
backstops the invariant (see "packs" below). Output lands in the dedicated
`journal_entries` table (migration 0048; `JournalPayload`).

**An extension kind, two descriptors, two tiers.** `journal_assessor` is an
**extension analyst kind** — registered via `register_analyst_kind` + the
`vocabulary_entries` family (`data/analysts/__init__.py:117`), **not** a member of
the closed built-in `AnalystKind` enum — so the twelve built-in kinds of §5.3 are
unchanged; the journal is registered *on top* of them. One kind, two descriptors
on two cadence tiers:

- **ENTRY tier** (`descriptors/analyst_journal_assessor.yaml`) — fires every 12h
  (`"0 0,12 * * *"`, `cooldown_seconds: 42000`), narrate `max_tokens: 16384`.
  Narrates the freshest reflective window.
- **CONSOLIDATION tier** (`descriptors/analyst_journal_consolidator.yaml`) — the
  **same kind** (`identity.kind: journal_assessor`), distinct `id`; daily at
  02:00 UTC (`"0 2 * * *"`, `cooldown_seconds: 79200`), narrate `max_tokens:
  24576`. It DISTILLS its prior consolidation + recent entries into one
  forward-carried narrative (build-on-don't-repeat), emits
  `entry_kind='consolidation'`, and fires `supersede_prior_consolidation` (closes
  the prior open consolidation, opens this one — the open-only partial-unique index
  `uq_journal_single_open_consolidation` enforces at-most-one open consolidation).
  **The tier is the descriptor** (no mode flag); `run_method` selects `entry_kind`
  from `identity.id`.

Both are **META** analysts: a single GLOBAL run per cadence tick
(`target_filter=None`, like `world_assessor`).

**Engine + per-phase LLM split.** `method.kind: llm_planner` — the in-actor
agentic GATHER envelope (one staged PLAN → GATHER → NARRATE arc; the persona is
loaded every phase), **not** the deep-consult Dapr workflow (which rides the
broken long-activity round-trip, §14 / task #86). GATHER `max_rounds: 6` (hard
ceiling); `budget_tokens_per_day: 2,000,000`; grounding enabled
(`slice_entities`). The two phases run on **two planes**: the heavy GATHER
investigation loop runs on the **core OpenAI-compatible plane**
(`llm.primary.openai_compat` — the local gpt-oss / vLLM plane, with a "Reasoning:
high" directive injected into the gather system prompt only), and the **voice**
(the in-voice field-notes seam + the NARRATE synthesis) runs on the **Anthropic
plane, Opus 4.8** (`llm.anthropic.opus_4_7`). So Anthropic spend is just the
bounded final voice synthesis (`max_tokens` governs only the Opus narrate — it is
never sent to the vLLM gather, which uses its own server budget); the deep agentic
loop is local. The deps builder reads `method.llm.narrate.raw` (optional;
`method.llm` is an open dict, no schema change) and resolves a second handler —
analysts without `method.llm.narrate` fall back to the single primary handler,
byte-unchanged. Prompts: `legba.prompts.journal_assessor:JOURNAL_SYSTEM` (entry
persona) + `legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM`.

**Packs + propose-and-gate (the hygiene invariant).** The journal is granted
**only two packs** — `journal_read` (14 read tools incl. 9 self-instruments:
`get_assessments` / `get_graph_structure` / `get_structural_balance` /
`get_critic_scores` / `get_calibration` / `get_run_health` / `get_source_health` /
`get_budget_status` / `get_journal_delta`) and `journal_propose`. **Both are
non-write-fact** — the grant-layer backstop for the never-write-a-fact invariant.
The journal writes **only** its own entries + consolidations directly; everything
outward — a correction, a change, or a `self_revision`, **including changes to its
own instructions** (`propose_self_revision`; protected sections auto-reject) —
goes to the **human-gated `journal_proposals` queue** (migration 0048), never a
live table. A human accepts/rejects; the accept path runs an idempotent per-kind
apply worker. The journal's only un-gated effect is its own continuity (it reads
its own last entry + current consolidation into its next run). *It can write its
own next breath but cannot rewrite its own rules without the operator.*

**API + UI.** `GET /api/v1/journal` serves the open consolidation + the entry
stream (`data/registry/journal_api.py`); `GET /api/v1/journal_proposals` plus
`POST /api/v1/journal_proposals/{id}/accept` and `.../reject` drive the review
surface (`journal_proposals_api.py`). The **Journal** UI panel (`system.journal`,
`legba-ui-v3/src/panels/system/Journal.tsx`) renders entries with provenance chips
that deep-link to the cited record and `[needs_citation]` / perspective spans in a
distinct style.

> **Honest caveats.** The `change`-apply path (on accept of a `change` proposal)
> is import-verified but **not yet exercised against a live registry**; the
> `correction` + `self_revision` apply paths **are** tested end-to-end. The
> Journal panel was tsc-green and fully wired but pending its first real
> in-browser render at the time of writing. Wave 5 — a critic + an optimizer over
> the journal's *own voice* — is **future / designed-not-built**, gated on first
> building a critic actuator.

## 9. The actor → Dapr-Workflow seam (the optimizer precedent)

Some work is too long and too expensive to run inside a turn-based actor: the
**optimizer** kind runs a GEPA/DSPy self-improvement loop over logged traces ⋈
critiques — a multi-step, multi-hour, deterministic-replay job. Legba runs it as
a **Dapr Workflow** (durabletask) on the same `daprd` sidecar — *not* as an actor
and *not* on any external workflow cluster. This is the **substrate that replaced
Temporal**, collapsing the runtime to one control plane.

The seam (`runtime/dapr_workflow/`) is the precedent every future durable job
follows:

- The optimizer **kind** calls a stable `temporal_client.start_optimizer_workflow()`
  (`data/analysts/optimizer.py:754` — the field name is historical; it just means
  "workflow client").
- `DaprOptimizerWorkflowClient.start_optimizer_workflow()`
  (`dapr_workflow/client.py:221`) schedules `optimizer_workflow` against daprd's
  gRPC endpoint and returns a `DaprWorkflowHandle`.
- The `optimizer_workflow` **orchestrator** (`dapr_workflow/workflow.py:134`)
  yields `validate_training_set_activity` then `compile_candidate_activity` (with
  a retry policy). The orchestrator body is **strictly deterministic** — no
  wall-clock/RNG/I/O; all non-determinism is pushed into activities (replay
  contract, `workflow.py:20-24`).
- `compile_candidate_activity` delegates to the shared GEPA core
  (`_run_gepa_loop`, `dapr_workflow/gepa.py:254`), reused identically by the
  `InProcessWorkflowClient` fallback (for tests / when `dapr.ext.workflow` is
  absent).
- All LLM calls route through `LegbaProviderLM` (`dapr_workflow/dspy_lm.py:147`),
  a custom `dspy.BaseLM` adapter that drives Legba's own `LLMProviderHandler` —
  **never litellm** (operator hard rule; litellm is inert in the worker image).
- The worker registers the orchestrator + activities **by function name**
  (`build_workflow_runtime`, `dapr_workflow/worker.py:58-107`), embedded in the
  runtime when `LEGBA_EMBED_WORKFLOW_WORKER=1` or standalone via the
  `legba-dapr-workflow-worker` container (`docker-compose.yml`, dspy lives only
  in this worker's image).

> **The pattern is now used twice.** `build_workflow_runtime`
> (`worker.py:107-117`) registers **both** workflows by function name on the one
> runtime: `optimizer_workflow` (+ `validate_training_set_activity` /
> `compile_candidate_activity`) and **`deep_consult_workflow`** (+ `plan_activity`
> / `acquire_activity` / `analyze_activity` / `synthesize_activity`), so the same
> worker / console script services both — no second container. The deep-consult
> workflow (altitude 3, §1) followed this exact seam precisely: a new orchestrator
> + activities in `worker.py`, a client in `dapr_workflow/deep_consult_client.py`,
> and a `deep_consult` kind module in `data/analysts/` whose `run_method`
> short-circuits to *schedule* the workflow and return a task id.
> `POST /api/v1/deep_consult` returns **202 + a task_id** in <1s (the actor
> schedules detached over the runtime's dapr sidecar), the staged workflow
> (plan→acquire→analyze→synthesize) runs for minutes→hours, and
> `GET /api/v1/deep_consult/{task_id}` polls by reading the produced FINDING row
> back from Postgres keyed by run_id (`registry/deep_consult_api.py`).
>
> **Gotcha:** a Dapr Workflow `instance_id` must **not** contain
> `::` — activity result parsing splits on `::` and hangs forever otherwise
> (`optimizer.py:508-519`). Worker actor ids use `::`; workflow ids must not.

## 10. Self-improvement — closing the loop

The analysis graph closes on itself. A **critic** analyst — by design a model
*different* from the one it grades (heterogeneity, not multi-agent debate), though
the live critic currently runs same-model on the core plane via
`allow_self_correlated` (the Anthropic plane is reserved for consult/deep) —
scores analyst outputs against a rubric grounded in substrate facts; the critic
reads `analyst_outputs` by id via its kind-specific read slice
(`_critic_ungraded_targets`, `dapr_actors.py:1394`). Those critiques feed the
**optimizer** (§9), which produces candidate prompt-module versions
(`OutputKind.PROMPT_MODULE_CANDIDATE` → `analyst_outputs`), gated by
human-reviewed promotion (champion instruction → live system prompt,
operator-gated). Because `derived_from` chains cross source-descriptor refs, a
critic can walk provenance from a finding all the way back to the raw signal and
its source.

**The optimizer returns scoped and measured.** The GEPA/DSPy self-improvement loop
is **not** the always-on, unmeasured monolith it was. The old `country_optimizer`
(over the retired `country_assessor`) stays **FROZEN** (byte-unchanged, cadence
nulled — SEAMS #30, foreclosing the reminder-flood regression class). GEPA returns
instead as **`unit_optimizer`** (`descriptors/analyst_unit_optimizer.yaml`, kind
`optimizer`, `method.kind=dspy_compile`), a bounded experiment over **one** measured
unit (`leadership_transition`). Every candidate carries a **real before/after paired
FAITHFULNESS delta** measured on the **same faithfulness judge** (currently the core
`llm.primary.openai_compat` model, not cross-family) that gates
the live unit findings (live: parent 0.34 → candidate 0.29, delta −0.05). It stays
`promotion_gate=human_gated` and can **never** auto-promote on a degenerate, absent,
or non-positive delta — an insufficient-sample / judge-unavailable delta is
honest-null, never faked to 0.0.

## 11. How it scales

The architecture's scaling story is the same inversion told three ways.

- **Many analysts on one shared substrate.** Adding a country, a sector, or a
  whole new domain is *registering descriptors into a running instance*, not
  deploying a new instance. Cross-target reasoning is free — the substrate is
  shared by definition, so a correlator analyst over pooled findings is just
  another subscriber.
- **Shared sources, not per-target binding.** N targets over one feed is **one**
  poll, **one** enrichment, **one** stored signal — fanned out by predicate.
  Shared sources carry `owner_tenant = shared`; per-customer sources are
  tenant-scoped; the subscription filter enforces the boundary.
- **The right execution shape for each workload.** Addressable, mostly-idle,
  request-driven work (sources, targets, analysts) is **Dapr actors**, scaling by
  replica count. Long, durable, multi-step work (the optimizer) is a **Dapr
  Workflow**. High-throughput, interchangeable, bounded work (media extraction)
  is a **NATS work-queue worker pool**, scaling by workers. No serialized actor
  is ever a throughput bottleneck — that work is a job.

NATS subjects are deliberately **coarse** (tenant/source/modality/event-class);
exact matching is the SQL `WHERE` (batch) plus the Starlark residual on the
narrowed stream (real-time). Subscriptions never try to express arbitrary
predicates as subjects.

## 12. Provenance and hot-pluggability, end to end

Two properties hold across every layer and are not bolt-ons:

- **Hot-pluggability.** Every unit — source, target, analyst, action-pack, stack
  component — is a registry descriptor with content-hashed identity, a lifecycle
  FSM, a signed audit trail, and a DLQ. Operators compose and re-tune at runtime;
  the reconcile loop converges the running actors. A new source kind or action
  pack is an **extension point**, not a schema change.
- **Provenance.** Every observation is source-attributed; every interpretation is
  `target_id` + `analyst_id` + `derived_from`. Lineage is a recursive query;
  per-analyst conclusions carry a chain-consistent (single-node) SHA-256 receipt
  chain; every descriptor mutation is Ed25519-signed (the Ed25519 signature is on
  the descriptor audit log only, never on the analyst-output receipts). The **one deliberate exception** is the `journal`
  kind (§8.4): a journal row carries an empty `derived_from` and its table is
  excluded from the lineage catalog, so the recursive `derived_from` walk never
  surfaces it — the journal is a reflective perspective *over* the lineage, not a
  node *in* it.

## 13. Live, proven state

The full loop — **altitudes 0 through 3** — runs end-to-end from cold-start
(empty volumes, the `0001_baseline.sql` baseline + the forward chain
`0032`…`0057` under `src/legba/data/migrations/`, current head **0057** — the
`facts`/`nexuses`/`seed_batches` schema plus the entity-profile composite key
(`0035`), signals retention (`0036`), the AGE output label (`0037`), the ACH
`resolved_outcome` column (`0038`), the consult-sessions tables (`0039`),
**situations-as-first-class + repairs (`0040`–`0042`)**, the data-quality
backfills (`0043`–`0045`), `source_poll_outcomes` (`0046`), the `acute_forecasts`
pilot table (`0047`, §5.10), the journal table (`0048`), the
receipt/derived-from repairs + data cleanups (`0049`–`0053`), the contested-claims
schema — `facts.source_credibility` (`0054`) + the `fact_contention` sidecar
(`0055`, §5.9), a second dangling-`derived_from` prune (`0056`), and the
`unit_reference_labels` correctness-gold table (`0057`, §5.10)):

- Real RSS sources (BBC / Deutsche Welle / Al Jazeera) acquire enriched signals —
  geo, language, and entity-class promoted to indexed columns; the
  `fact_extractor` stage extracts temporal `facts` (`source_type=ingestion`,
  ≈3.7k of ≈4.6k total) with `valid_from` stamped + value-change supersession.
- Fan-out on `legba.signals.>` routes them to the country desks, each
  subscribing by a geo/tag predicate; the **four bounded units** each bind to all
  24 desks (the 19 G20 targets + the 5-country `watch` tier) via a single
  `has_tag("g20") or has_tag("watch")` selector and fan out one run per desk.
- The units produce distinct per-country findings, each **cited to source, put
  through the mandatory faithfulness verify pass** (deterministic floor + the LLM
  judge, currently the same core `llm.primary.openai_compat` model, not
  cross-family), and stamped with `derived_from` provenance + receipt
  chains; `country_composition` synthesizes the verify-passed sub-claims into one
  per-country read, `world_assessor` composes those into a world view, and
  `scorecard_producer` writes one banded row per active desk (every target tagged
  `g20` or `watch`, banded over a 14-day window) (§5.10 — the live scorecard is an
  honest MIX, some countries all-insufficient). The STIX
  emitter serializes live payloads to bundles; the `acute_forecasts` pilot issues +
  exogenously resolves binary forecasts that report NO proven skill and surface
  only on the calibration scoreboard (§5.10).
- An entity knowledge graph (`entity_profiles` / `signal_entity_links` /
  `proposed_edges`) is kept current by an ongoing `entity_resolution`
  deterministic analyst, which now **canonicalizes** every NER span through
  `canonicalize_entity` (`deterministic_handlers/_entity_canon.py`: HTML /
  possessive strip + alias map + pycountry gazetteer type correction) *before*
  the dedup key, appending a content-addressed `derived_from` marker and an
  `entity_profile_versions` row (Phase C; the live backfill collapsed
  country-as-person rows to 0 and "United States" 13 surface fragments → 1). The
  `proposed_edge_governance` handler promotes pending `proposed_edges` into
  neutral `CoOccursWith` nexuses, and the **`relationship_reifier`** lifts the
  typed co-mention edges into the *typed, signed* `nexuses` (≈3.2k signed of ≈4.9k
  total; e.g. *Iran—HostileTo→ Strait of Hormuz* −1, *Musk—LeaderOf→ SpaceX* +1,
  *Brazil—MemberOf→ BRICS* +1).
- The **`competing_hypotheses` (ACH)** analyst produces ≈940 hypotheses (253
  confirmed / 287 refuted / ≈400 active), each with a Heuer consistency ×
  diagnosticity matrix in
  `diagnostic_evidence`. **The per-cell consistency is now LLM-scored** (Heuer
  CC/C/N/I/II, budget-gated, with the deterministic lexical/polarity counter as
  the budget-exhausted per-cell fallback — each row records `matrix_scorer`; §5.3),
  and evidence is scoped to the resolved-entity set, so `confirmed` / `refuted`
  are defensible. `calibration_tracking` scores them with a Brier number whose
  **exogenous** `resolved_outcome` (migration 0038) — stamped against subsequent
  facts or an operator label rather than the hypothesis's own evidence balance — is
  **built and firing**, with the subsequent-facts auto-resolver now **ABSTAINING
  on undirected theses** (it previously auto-graded them TRUE, inflating the
  headline rate); it runs alongside the live `status_transition` (self-consistency)
  tier (rows there flagged `self_consistency_only`). The goal remains a real Brier
  against resolved real-world outcomes. Residual caveats: the subsequent-facts
  auto-resolver is a coarse directional heuristic (the operator-label path is
  higher-fidelity), the gradeable directional resolution rate is modest, and a
  budget-exhausted run falls back to the lexical scorer; no proven-forecast-accuracy
  claim (§5.7).
  `meta_findings_synthesizer` / `cross_analyst_correlator` run as registered
  altitude-2 producers.
- The **optimizer** runs as a real Dapr Workflow on an isolated worker (GEPA
  proven on a 175k-token run, never litellm), scoped as **`unit_optimizer`** over
  the one measured unit (`leadership_transition`) with a real before/after
  faithfulness delta on the same faithfulness judge (currently the core model, not
  cross-family) (live: parent 0.34 → candidate 0.29,
  delta −0.05), human-gated and non-auto-promoting; the old monolithic
  `country_optimizer` stays FROZEN (§10). The **deep-consult** workflow rides
  the same worker (plan→acquire→analyze→synthesize), submitted detached via
  `POST /api/v1/deep_consult` (202 + task_id), polled to a persisted finding.
- An on-demand **consult** engine runs a ReAct analyst against the live substrate
  (`POST /api/v1/consult`, `consult_on_demand` kind, 4 governed read tools) in
  two modes: **`chat`** (default — multi-turn via a client-held `messages[]`, no
  durable finding; each ReAct step streamed to a request-scoped NATS subject and
  relayed to the browser as SSE via `GET /api/v1/consult/stream/{request_id}`)
  and **`deep`** (persists a finding row).
- A **curated-seeding** primitive (`source_type` + `seed_batch_id` + the
  `seed_batches` ledger + the `src/legba/data/seed/` framework + `scripts/seed.py`)
  imports flavor-b `world_baseline` knowledge into the same tables; seed batch
  `414473c8` is live.

The whole loop is now wired; the data-analysis rigor layer (§5.7) closed the
"open frontier" that earlier drafts described.

## 14. Built vs. declared seams

The loop in §13 is live — including the full altitude-0..3 data-analysis rigor
layer (§5.7), which was the open frontier in the pre-2026-06 drafts. What is
*not* built is declared, never implied — the full list is
`docs/SEAMS.md`. The architecturally significant remaining seams:

- **Eager media extraction** — job plane + `process_media` envelope live; the
  Whisper/VLM/OCR handlers and SeaweedFS object store are seams; non-text media
  is referenced, not extracted (§6.3).
- **Fallback-model demotion** — `demote_and_continue` logs a pause instead of a
  real cheap-model fallback (`docs/SEAMS.md` F-2).
- **`stream` acquisition** — `poll | push` live; long-lived-consumer mode is a
  documented enum extension (§5.1).
- **SeaweedFS object store** — schema-slotted stack kind; no live handler (§5.5).
- **Time-series metrics / full-text search** — the former TimescaleDB metrics
  store and OpenSearch BM25 backing were removed; both are declared seams now
  (no metrics store; `search_signals` uses Postgres FTS) — see SEAMS.
- **Nexus → AGE-edge mirroring** — typed signed `nexuses` are the relational
  source of truth; mirroring them onto `legba_graph` edges is deferred (the
  `derived_from` AGE hook in `write_analyst_output` is left in place but not
  called, `writes.py:225`).
- **Dapr long-activity workflow round-trip** (SEAM #23) — on daprd 1.17.9 a
  Workflow orchestrator does not reliably resume after a *long* activity (short
  ones deliver), even though the activity runs fine and returns; the GEPA
  optimizer + deep-consult round-trip therefore **degrade to an in-process
  fallback**. Verified not our code (threads idle post-compile, reproduces on a
  fresh engine). Declared, not silently worked around.
- **Sequenced retirements / freezes** (not seams-of-omission — deliberate,
  documented in `docs/SEAMS.md`): the monolithic `country_assessor` is RETIRED and
  STOPPED (the units + composition supersede it; ≈1.2k historical findings remain
  in the DB, unread); the forecast-as-claim predictors (`country_predictor`,
  `india_energy_predictor`) are RETIRED/STOPPED (cadence nulled; ≈539 historical
  prediction rows remain), and the old monolithic `country_optimizer` is
  cadence-FROZEN (descriptor still `state=active`, cadence nulled). Each is a nulled
  cadence or an unregistered descriptor, restorable by re-declaring, never a silent
  stub. (The `journal_assessor` is NOT frozen — it runs on cadence as an
  introspective instrument, §8.4.)
- **RBAC / multi-tenant isolation** — designed, not built (`docs/DIRECTION.md`
  §1–§2, `docs/SEAMS.md` #14). See the perimeter note below.

**Deployment perimeter — single-operator, single-tenant (LOCKED).** Legba ships
as a single-operator, single-tenant deployment: one operator, one instance, one
shared Caddy `basic_auth` credential, one logical tenant
(`owner_tenant = 'default'`/`'shared'`). There is **no in-application RBAC,
multi-tenant isolation, or per-role access control** at this release — the
network/deployment perimeter (Caddy `basic_auth` + loopback-bound internal
services) is the security boundary, and the acquisition-plane `owner_tenant`
column is forward-compat metadata, not an isolation guarantee you should rely on
to separate untrusting tenants. Scoped tokens, SSO, and analysis-plane tenancy
enforcement are real future direction items (`docs/DIRECTION.md` §0–§2), gated
and never silently half-built. No enterprise / multi-tenant / RBAC capability is
claimed.

## 15. Read next

- `FLOWS.md` — "life of a…" walkthroughs (a signal, an analyst cycle, a fact, a
  reified nexus, an ACH hypothesis, a consult, the optimizer + deep-consult
  workflows).
- `CODE_MAP.md` — package/module map keyed to responsibilities.
- `DESIGN.md` — full implementation design (APIs, files, deployment).
- `RUNBOOK.md` — runtime bootstrap, deps resolvers, operations.
- `AI_MODELS.md` — the model-serving surface (LLM, embeddings, NLP).
