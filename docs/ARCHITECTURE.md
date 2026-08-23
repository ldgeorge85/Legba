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

**What triggers / schedules what** (Tier 2) — two mechanisms, both **Dapr reminders/triggers** (no external cron in the loop): **(a) reactive coalescing triggers** — per-target analysts fire when enough new signals accumulate (NATS-driven); **(b) cadence reminders** — cross-target & meta analysts fire on a schedule (the nine bounded reasoning UNITS + the `country_composition`/`region_composition`/`world_assessor` composition tower + the thematic `escalation_composition` on a ~6–12h beat, the deterministic I&W pair `indicator_tracker`/`collection_gap`, `competing_hypotheses`/`graph_mining`/`relationship_reifier` ~12h, `scorecard_producer`/`calibration_tracking` daily, `forecast_scoreboard` weekly; plus the 2026-07 deterministic wave — `alert_trigger_scan` every 10 min (which since 2026-07-29 also carries geo convergence as its sixth trigger class — the standalone `geo_convergence_scan` was folded into it and has since been retired live — and since 2026-08 the production gauge's `production_deficit` as its seventh) and `evidence_archiver` every 30 min feed the alert/archive loops, and the daily readout family `band_calibration_tracker` / `fact_decay_scan` / `source_track_record` / `narrative_mapper` / `desk_baseline` computes derived sidecars, all registered as drafts an operator activates). SourceActors schedule their own polls (the Tier-1 pipeline rides each poll). The reminder *is* the scheduler.

**Where Wikidata / grounding fit (out-of-band, decoupled by the substrate):** Wikidata is **not a live source** — it never touches the signal pipeline. It is a **seed** (`scripts/seed.py --source wikidata_leaders`, operator-run / cron-able) that writes *current* `head of state` facts INTO the substrate (superseding the stale officeholder). Separately, at *analysis time*, grounding-enabled analysts' **GROUND phase** READS those current facts back OUT of the substrate and injects a dated preamble into the LLM prompt (Flow 10). The two movements don't know about each other — the substrate is the hand-off. See §5.8.

**What scales** — the **actors are the workers** (SourceActor/TargetActor/AnalystActor); Dapr **placement** redistributes them across `legba-runtime-dapr` replicas, so you scale out by adding replicas. **Cadence-batching is the key move:** analysts run on a schedule, not per-signal, so **LLM cost is decoupled from the ingest firehose**. The substrate scales independently (PG read-replicas/partitioning, NATS, Qdrant). **Singletons needing leader election:** the reconcile loop + the discovery informer (single-replica fail-loud guard otherwise; 2-replica placement + leader election proven locally). **The real ceiling is LLM throughput** (budget-gated) — which is exactly why the heavy graph work is deterministic Python and the analysis layer is cadence-batched. (More in §11.)

**What runs OUTSIDE the source→output loop (out-of-band).** Not everything is the live `SOURCE ─▶ … ─▶ OUTPUT` pipeline. The decoupled processes — each handing off through the substrate, never on the signal hot path:
- **Seeds** (`scripts/seed.py` → `data/seed/`): curated/authoritative roots imported INTO `facts`/`nexuses` with `source_type='seed'` + a `seed_batches` ledger row — adapters `world_baseline` / `wikidata_leaders` / `acled_conflict` / `sipri_arms_transfers` (operator-run / cron-able; §5.7, Flow 9). Wikidata is a *seed*, not a live source.
- **Backfills** (`scripts/backfill_entity_canonicalization.py`, `scripts/backfill_entity_graph.py`; `SourceActor`'s optional `source_credibility` host-lookup at write-time, §6.1): one-shot substrate repairs over already-stored rows.
- **Bringup / registration** (`scripts/bringup_register_*.py` + the ~47-entry `bringup_register_source_catalog.py`): pushes descriptors into the registry tables at deploy — the live source/analyst set is the DB rows (≈57 head source descriptors), not the `descriptors/*.yaml` files.
- **Dapr Workflows** (`legba-dapr-workflow-worker`, `runtime/dapr_workflow/`): the multi-step durable jobs that don't fit a turn-based actor — the **GEPA optimizer** (§9, Flow 4) and **deep-consult** (§9, Flow 5). Scheduled detached from an actor run.
- **Maintenance / GC** (deterministic analysts on cadence, `data/analysts/deterministic_handlers/`): `fact_decay` / `nexus_decay` (temporal-confidence decay + expiry), `finding_supersession`, `entity_gc`, `signals_retention` (0036), `integrity_sweep`, `reminder_gc` (`runtime/reminder_gc.py`, GC of reminders for retired `actor_state` rows). These UPDATE/prune pre-existing rows.
- **Per-source liveness watchdog** (`liveness_watchdog.check_source_cadence_once`, cadence): detects a silent source by comparing `now()` to `max(signals.fetched_at)` per source, then lateral-joins `source_poll_outcomes` (0046) for the *why* — `SourceActor.pull_once` writes one `source_poll_outcomes` row per poll: `success` (>=1 signal written, or an intra-source duplicate collapsed), `empty` (HTTP-200-with-0-signals), or `error`, carrying the handler's own health diagnosis so the watchdog alert (and the UI) can distinguish a genuinely quiet feed from a broken one. Success rows were added by 0114; before them a productive poll wrote nothing, so "no outcome row" was ambiguous between "the poll never ran" and "the poll worked", and no reader walking a RUN of rows could see a recovery.
- **Meta-analysts over the substrate** (altitude 2): `meta_findings_synthesizer` / `cross_analyst_correlator` / `competing_hypotheses` / `calibration_tracking` — they read accumulated outputs, not signals (Tier-2 cadence, but analysis-of-analysis rather than first-order).
- **Migrations** (`data/migrations/0001_baseline` + the forward chain `0032`…`0185`, current head **0185**; `0095`/`0100`/`0110`/`0111` and a few later slots intentionally unused — the runner discovers by sorted glob, so gaps are harmless): schema evolution, applied PG-direct out of band. Beyond 0060 the chain adds the contested-claims + data-quality-program migrations (0061–0075), the 2026-07-06 audit sweep (0076–0080: entity re-fold + junk gate, semantic/junk-fact close, nexus junk/self-edge/dyad canonicalize, cross_correlator stale-head sweep, state-media `source_credibility` seed — the 0077–0080 closes are reversible), and the signal-content-depth / NER-reenrich wave (0081–0085: the `signal_summarized` / `signal_indexed` / reindex-summarized / `signal_embedding` / `signal_reenriched` markers that drive the OpenSearch corpus + Qdrant embeddings + NER backfill). The chain adds the `facts`/`nexuses`/`seed_batches` rigor schema (0032–0034), the entity composite key / signals-retention / AGE-output-label / ACH `resolved_outcome` / consult-sessions tables (0035–0039), situations-as-first-class + repairs (0040–0042), the data-quality backfills (0043–0045), `source_poll_outcomes` (0046), the `acute_forecasts` pilot table (0047), the journal table (0048), the receipt/derived-from repairs + data cleanups (0049–0053), the contested-claims schema — `facts.source_credibility` (0054) + the `fact_contention` sidecar (0055, §5.9), a second dangling-`derived_from` prune (0056), the `unit_reference_labels` correctness-gold table (0057, §5.10), and the composition-supersession fold + critique index + null-target composition-head fold (0058–0060, supporting the one-live-head-per-desk composition tower).

### 0.1 Substrate data inventory — what is kept where, written-by / read-by

Per store, the actual datasets and their producers/consumers (verified 2026-06). The runtime substrate is **five backing services** — Postgres+AGE, NATS JetStream, Qdrant, Redis, OpenSearch (the time-series-metrics store that earlier drafts over-claimed has been removed; full-text search, by contrast, is **LIVE and load-bearing** on a single-node OpenSearch cluster; see SEAMS):

| Store | Datasets kept | Written by | Read by |
|---|---|---|---|
| **Postgres + Apache AGE** (PRIMARY / source of truth) | **descriptors** (`source_/target_/analyst_/action_pack_/stack_/wiring_descriptors`); **acquisition** (`signals`, `signal_aliases`, `signal_entity_links`); **knowledge substrate** (`facts`, `nexuses`, `entity_profiles`/`entity_profile_versions`, `hypotheses`, `proposed_edges`, `situations`, `graph_metrics`); **outputs/provenance** (`analyst_outputs`, `analyst_traces`, `analyst_critiques`, `output_dead_letter`, `descriptor_dead_letter`); **journal (OFF-chain)** (`journal_entries`, `journal_proposals` — 0048; the reflective voice, empty `derived_from`, excluded from the lineage catalog, §8.4); **runtime state** (`actor_state`, `actor_filter_state`, `trigger_state`, `discovery_state`); **governance** (`governor_events`, `budget_ledger`, `global_budget_envelope`, `budget_demotion_events`, `action_pack_invocations`, `alert_sink_deliveries`); **liveness** (`source_poll_outcomes` — one provenance row per source poll: `success`/`empty`/`error`, 0046 + 0114); **consult audit** (`consult_sessions` / `consult_turns`, 0039); **audit** (`audit_checkpoints`, `descriptor_audit_log`); **seeding** (`seed_batches`); **reference** (`iso_countries`, `source_credibility`, `vocabulary_entries`); **alerting (2026-07)** (`alert_trigger_watermarks` — durable trigger/convergence watermarks, 0091; `watchlist` — operator standing watches, 0105); **evaluation** (`band_calibration_claims` 0093; `correctness_labels` + `goldset_week_samples` 0096; `unit_reference_labels` 0057); **source assurance** (`source_ratings` + `source_dossiers` 0094; `source_track_records` 0099; the merged `source_quality` VIEW over both plus `source_credibility` and observed production, 0115); **derived readout sidecars** (`fact_decay_states` 0098; `narratives` + `narrative_echo_edges` 0102; `desk_baselines` 0103; the contention tie-break cache `fact_contention_tiebreak` 0097); **evidence archive** (`evidence_archive` 0104 — the CAS sidecar; bytes live on the `legba_archive` volume, not in PG); plus the **dormant AGE graph `legba_graph`** *inside* PG (9 vertex / 14 edge labels registered, but **near-empty / off-path** — the operative graph is the relational `nexuses` table; §5.5 "AGE re-evaluation") | Tier-1 pipeline (signals/facts/entities); Tier-2 analysts + workflows (outputs/facts/nexuses/hypotheses); registry (descriptors/audit); runtime (state); seeds (facts/nexuses + `seed_batches`) | every read path — analyst slices, consult tools, grounding resolver, the API/UI, lineage walks |
| **NATS JetStream** | **transport / events, NOT a dataset store** — `legba_signals` (interest-retention signal bus), 4 registry-lifecycle streams, the DLQ stream, work-queues, consult-step relay. Transient fan-out; the durable copy is always in PG | SourceActors (signal publish), registry (lifecycle events), analysts (output envelopes + consult steps) | subscription consumers, the reconcile loop, the SSE relay, job workers |
| **Qdrant** | **3 collections** — `legba_signals` (signal vectors, 1024-dim BGE-M3 cosine, ingest-dedupe tiers 3-4) **plus the two LIVE RAG corpora** `tradecraft` (~1716 chunks) and `world_context` (~293 chunks), both 1024-dim bge-m3 cosine, embedded through the stack embedder port | Tier-1 dedupe / embedder; the RAG loader (`data/rag/`) for the corpora | dedupe tier 3-4; consult `vector_search` + `search_context`; the **live** grounding resolver — `vector:world_context` retrieval is provisioned and flipped ON for `leadership_transition` + `internal_stability` (opportunistic, relevance-floored, country-filtered, degrade-not-drop), §5.8 |
| **Redis** | **TTL'd cache only** — geocode cache, ingest-dedup hints, registry-health, intelmq source state (~84 keys live) | Tier-1 filters, health checker | the same filters / health (cache-aside; never a source of truth) |
| **OpenSearch** (single-node) | **1 index** — `legba_signals_corpus` (BM25 full-text over the whole raw body of every ingested signal, 182.6k docs of which 106.8k are live and 75.9k were orphaned by purges that had no delete path — the signal-content-depth corpus) | the `corpus_indexer` deterministic analyst (full-body signal reads → OpenSearch) **writes**; the `corpus_retention` analyst **deletes** (drains the `corpus_tombstones` queue, mig 0175) | the `search_corpus` (BM25 lexical search) + `read_document` (by-id full-doc fetch) substrate-read tools; the `corpus_researcher` / `cross_doc_corroborator` agentic analysts |
| **SeaweedFS** | object store for retained media — **schema-slotted stack-component kind; NO handler shipped** (eager-media extraction is a seam, §6.3) | (none) | (none) |
| **Filesystem CAS archive** (`legba_archive` volume) | the **evidence archive** — original bytes of signals cited by verified findings, content-addressed at `<root>/<sha[:2]>/<sha256>` (`LEGBA_ARCHIVE_ROOT`, default `/var/lib/legba/archive`); addressed from PG as `signals.object_ref = cas:sha256/<hex>` + the `evidence_archive` sidecar (0104). Backend-agnostic address: a later object-store swap rewrites zero rows (§8.6, SEAMS #42) | the `evidence_archiver` deterministic analyst (verified-cited-only, SSRF-guarded, license-gated) | lineage / export / signal projections (`archived` + sha); the corpus indexer re-indexes archived full text |

> **OpenSearch (LIVE full-text corpus) + the one removed store.** A dedicated time-series-metrics store (Grafana/TimescaleDB observability) was *provisioned-but-idle* with zero callers and has been **removed from the codebase**; time-series metrics are now a **declared seam** (see SEAMS) — there is no metrics store, and `anomaly_detection` reads `time_bucket()` from the **primary Postgres pool**, not a separate cluster. Full-text search, by contrast, is **LIVE and load-bearing**: a **single-node OpenSearch cluster** (service `legba-opensearch-1`) holds the `legba_signals_corpus` index — the full raw body of every ingested signal (~112k docs), populated by the `corpus_indexer` deterministic analyst and queried by the `search_corpus` / `read_document` substrate-read tools (the signal-content-depth subsystem, alongside `signal_summarizer`, `signal_embedder` → Qdrant `vector_search`, `corpus_researcher`, and `cross_doc_corroborator`). The legacy `search_signals` tool still uses Postgres FTS (`to_tsvector`/`plainto_tsquery`).

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
| **1 — First-order (bounded units)** | cited, faithfulness-verified findings — each unit answers ONE narrow question | NINE `inline_target` reasoning UNITS. Seven broad ones (`leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`, `internal_stability`, `military_posture`, `economic_coercion`; `method.kind=llm_planner`), each fanned out per desk — 32 desks: the 19 G20 country desks + a 13-country high-consequence `watch` tier (§7.2). An eighth, narrower unit, `proliferation_watch`, is instead tag-scoped to only the ~8 nuclear-relevant desks. A ninth, `disruption_status`, is tag-scoped the same way but to a DIFFERENT desk family — `has_tag("supply_chain")`, the thematic lane/flow desks (§7.2), on a 24h window instead of 72h — which is the proof that the tag predicate, not the country plane, is the fan-out primitive: no new analyst kind, no new code path | **LIVE** — the monolithic `country_assessor` is RETIRED and STOPPED (nothing in the spine reads it; ≈1.2k historical findings remain in the DB, unread — not a clean slate); the forecast-as-claim `country_predictor` is RETIRED/STOPPED (≈539 historical prediction rows remain) (§5.10, §14) |
| **1 — Maintenance** | situations (**first-class temporal frames**, 0040–0042) / supersessions / critiques / STIX / fact-&-nexus decay | situation_clustering (+ `thematic_proposal`), finding_supersession, `critic`, emit-bindings, `fact_decay` / `nexus_decay` / `structural_balance` / `graph_mining` | **LIVE** — situations carry `situation_signature` + `valid_from`/`valid_until`/`superseded_by` + `target_id` (0040–0042); the events substitute (no `events` table). The forecast-as-claim `predictor` producers (`country_predictor`, `india_energy_predictor`) are RETIRED/STOPPED (≈539 historical prediction rows remain) — forecasting returns only as the measured `acute_forecasts` scoreboard (§5.10) |
| **2 — Composition** | the composition tower (a hedged, cited synthesis over the *verified* units; an unverified sub-claim never enters — INNER JOIN on the faithfulness critique) + meta-findings | `country_composition` → `region_composition` (5 region frames: Africa, Americas, Europe, Indo-Pacific, MENA) → `world_assessor`, plus the thematic cross-desk `escalation_composition` (carries a correlation guard against double-counting correlated desks) — all on `meta_findings_synthesizer`; `cross_analyst_correlator` | **LIVE** — `world_assessor` GRADUATED into the world composition; it is NOT the old verdict-from-nowhere monolith; one live head per desk survives supersession (§5.10) |
| **top — Banded scorecard + skill scoreboard** | one banded per-country row from high-precision RULES over already-verified claims (demote-never-promote) + the per-unit skill numbers | `scorecard_producer` (deterministic META, 12th OutputKind `scorecard`), `unit_correctness_scorer` / `calibration_tracking` / `forecast_scoreboard` | **LIVE** — honest: an unqualified dimension reads `insufficient-evidence`; the live scorecard is a MIX (some countries band, e.g. the US reads all-insufficient because its unit faithfulness is genuinely low); the forecast pilot reports NO proven skill (§5.10) |
| **2 — Second-order** | hypotheses (competing claims, ACH matrix; per-cell scoring is LLM-scored on Heuer CC/C/N/I/II with a lexical fallback — §5.3) | the **`competing_hypotheses`** (alias `ach`) META analyst + `calibration_tracking` (Brier reads `resolved_outcome`; exogenous resolver built + firing — subsequent-facts auto-resolver that ABSTAINS on undirected theses + operator-label path — alongside the live self-consistency tier, §5.3) | **LIVE** — ≈940 hypotheses (253 confirmed / 287 refuted / ≈400 active) (§5.7) |
| **3 — On-demand deep** | deep consult (a staged analytical job: plan→acquire→analyze→synthesize) | the **deep-consult Dapr Workflow** | **LIVE** — registered alongside `optimizer_workflow` (§9) |
| **across — Reflective voice (OFF-CHAIN)** | the **journal** — Legba's first-person reflective voice; a `journal` row is a *perspective OVER* the whole flow, **NOT** a node in the fact/finding/nexus lineage (always-empty `derived_from`, excluded from the lineage catalog) | the **`journal_assessor`** META analyst (entry + consolidation tiers; per-phase LLM split, BOTH phases on the local gpt-oss/vLLM core plane — GATHER loop + VOICE — the voice previously ran on Anthropic Opus 4.8 but moved fully to core 2026-07-06, so the journal is $0 / no Anthropic spend) | **LIVE, ON cadence** — runs as an introspective instrument (`journal_assessor` 12h entry + `journal_consolidator` daily); writes ONLY `journal_entries`, off the fact/finding/nexus chain, so it cannot pollute product output (routing its reflections back via a human-gated proposal queue is a FUTURE item); `OutputKind.JOURNAL` + dedicated `journal_entries` table (migration 0048); §8.4 |

Two clean regimes fall out: **extraction is always-on at ingest** (altitude 0,
once per signal); **deep analysis is on-demand** (altitude 3). The entire stack —
altitude 0 (temporal facts + reified Nexus), altitude 1 (the nine bounded units +
the continuous live loop), altitude 2 (the per-country → per-region → world
composition tower, meta-findings + ACH hypotheses) and altitude 3 (deep consult) — is now wired and
live-proven; what was the "open frontier" of the pre-2026-06 drafts has been
built out as the **data-analysis rigor layer** (§5.7). Each tier rides a rail
that already existed with a working precedent (§9).

**The product is the composed, verified spine, not any single analyst.** What
Legba surfaces at altitude 1+ is a decompositional chain, read bottom-up: NINE
narrow reasoning UNITS (each cited to source and put through a **mandatory
faithfulness verify pass**, §5.10 — the eight on the country plane feed the
tower; the supply-chain `disruption_status` is a leaf read today, since the
composition tower is keyed to country desks) → a per-country **composition** that admits
only verified sub-claims → a per-region **composition** → a **world composition**
over the per-region reads (plus a thematic cross-desk `escalation_composition`) → a
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
from the `method.kind`: e.g. the nine bounded units are analyst-kind
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
producer graph: the **nine bounded units** (`leadership_transition`,
`energy_security`, `escalation`, `narrative_coordination`, `internal_stability`,
`military_posture`, `economic_coercion`, `proliferation_watch`,
`disruption_status` — all `inline_target`),
`country_composition` + `region_composition` + `world_assessor` (the per-country →
per-region → world composition tower) plus the thematic `escalation_composition`
(all `meta_findings_synthesizer`), the deterministic I&W pair `indicator_tracker`
(run-over-run indicator diffs) + `collection_gap` (starved desk × dimension cells),
`scorecard_producer` + `forecast_scoreboard` +
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
| **Filesystem CAS archive** | evidence archive — original bytes of verified-cited signals, content-addressed `cas:sha256/<hex>` on the `legba_archive` volume (`LEGBA_ARCHIVE_ROOT`) | **LIVE** (`data/archive.py`; written by the `evidence_archiver`; §8.6, SEAMS #42) |
| **OpenSearch** (single-node) | full-text signal corpus — `legba_signals_corpus` (182.6k docs / 106.8k live, BM25 over the whole raw signal body) | **LIVE** (`corpus_indexer` indexes; `corpus_retention` deletes; `search_corpus`/`read_document` read; `data/opensearch.py`) |
| time-series metrics | observability store | **REMOVED — declared seam** (no metrics store; `search_signals` uses Postgres FTS; see SEAMS) |

The Postgres `signals` table is the **source of truth** (canonical, persistent,
queryable for batch reads and backfill, `data/migrations/0001_baseline.sql`);
NATS is the **notification bus** (transient, fan-out). The same observation lives in
both, which is exactly why real-time delivery and batch re-analysis are one
mechanism rather than two. NATS subject tokens cannot contain dots, so
`SourceDescriptor.id` is flattened by `subject_token()` (`data/nats.py:86-95`).

> **Reality check.** The Postgres/AGE + NATS + Qdrant + vault + Redis-as-cache
> + OpenSearch set is what is actually exercised. SeaweedFS has a schema-slotted
> stack kind but **no live substrate handler** — it is a declared seam, not a
> running integration. (The former TimescaleDB metrics store has been removed
> outright and time-series metrics is now a declared seam — see SEAMS. Full-text
> search, by contrast, is **LIVE on a single-node OpenSearch cluster** — the
> `legba_signals_corpus` index, ~112k docs, read via `search_corpus`/`read_document`.)
> Earlier drafts listed all of these as if first-class; this doc corrects that.

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
- **`source_poll_outcomes`** (`0046_source_poll_outcomes.sql`, `success`
  outcome `0114_source_poll_outcome_success.sql`) — append-only provenance for
  **every** source poll: `success` (>=1 signal written, or an intra-source
  duplicate collapsed — `signals_written` carries the count), `empty` (clean
  HTTP-200-with-0-signals), or `error`, each carrying the handler's own
  `health_state` diagnosis. The per-source liveness watchdog lateral-joins it to
  explain *why* a source went silent (§0), and `entity_gc` op 4 walks it as a
  run to decide auto-pause. 0046 logged only NON-productive polls; because an
  absence cannot break a run, a repaired source's historical `error` rows stayed
  the leading run forever and the auto-pause latch re-fired on sources that were
  actively ingesting. Recording success ends that class at the source.

### 5.6 Action-pack — *modular, allow-listed analyst agency*

Analyst capability is **granted, not hard-coded**. An `ActionPack`
(`schemas/action_pack.py`) is a registrable, versioned, content-hashed bundle of
*(tools + prompt fragments/rules + escalation channels + a per-pack governor +
an applicability predicate)*. Seed packs: `media_processing` (`process_media`),
`incident_response` (`escalate`/`create_incident` → channels), `substrate_read`
(the consult kind's **19 governed read tools**, incl. the live semantic
`search_context` corpus search + the `search_corpus`/`read_document` OpenSearch
full-text readers), and `escalate_finding` (the delivery edge: it
alerts a finding when **post-verify `effective_confidence × severity`** crosses its
`confidence_gate` — a verify-demoted finding does not alert; severity is a
first-class read column, not a tag; external delivery currently lands on the NATS
subject `channels.escalations` only, bus-only). (The `discovery` pack was retired
per decision F-1.)

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
  produces no header. Opted IN on all **nine bounded units**
  (`analyst_leadership_transition.yaml`, `analyst_energy_security.yaml`,
  `analyst_escalation.yaml`, `analyst_narrative_coordination.yaml`,
  `analyst_internal_stability.yaml`, `analyst_military_posture.yaml`,
  `analyst_economic_coercion.yaml`, `analyst_proliferation_watch.yaml`,
  `analyst_disruption_status.yaml` —
  `grounding.enabled: true`, each drawing
  `sources: [substrate, situations, graph_structure]`). Two units —
  `leadership_transition` and `internal_stability` — additionally draw
  `vector:world_context`, the LIVE opportunistic RAG source (a separate,
  non-citable grounding preamble retrieved from the curated `world_context` Qdrant
  corpus with a relevance floor + country filter, degrade-not-drop; the staggered
  RAG expansion, §5.8). The composition tower does not re-ground: it
  synthesizes over the already-grounded, faithfulness-verified unit findings (§5.10).
  - **Canary (live-verified).** A US assessment's context now contains
    "United States — head of state: Donald Trump (since 2025-01-20)".

- **Tier 2 — vector `world_context` collection (LIVE).** A curated unstructured-brief
  collection, now wired (the embedder-through-port L-114 landed; SEAM #11 resolved). The
  resolver retrieves from the `world_context` Qdrant corpus (~293 chunks; a `tradecraft`
  corpus of ~1716 chunks also exists) through the stack embedder port (bge-m3, 1024-dim)
  as a separate, non-citable grounding preamble — opportunistic, relevance-floored,
  country-filtered, degrade-not-drop when the corpus is empty. It is **staggered on**:
  currently flipped ON for `leadership_transition` + `internal_stability` (their
  `grounding.sources` include `vector:world_context`); the other units resolve only the
  structured `substrate` source, pending review-gated expansion
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
at migrations 0054–0055; the current migration **head is 0185** (see `DATA_MODEL.md` for the wave tables).

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

### 5.10 The analysis spine — units → composition tower → world → scorecard, verified

This is the **product**: the decompositional chain that turns the enriched signal
pool into cited, verified, drillable reads. It is composed bottom-up, and every
first-order claim passes a **mandatory faithfulness verify pass** before anything
above it may consume it.

**1 — Nine bounded reasoning UNITS.** Nine `inline_target` LLM analysts
(`descriptors/analyst_*.yaml`, `method.kind=llm_planner`, core plane
`llm.primary.openai_compat` = self-hosted gpt-oss-120b, $0) — each answer **one
narrow question**. Seven —
`leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`,
`internal_stability`, `military_posture`, `economic_coercion` — are scoped to every desk by a single coverage-tag fan-out
`has_tag("g20") or has_tag("watch")` (32 desks: the 19 G20 country desks + a
13-country high-consequence `watch` tier — Israel, Iran, Ukraine, Taiwan, North
Korea, Pakistan, and the escalation-risk band Sudan, Mali, Burkina Faso,
Niger, DR Congo, Myanmar, Haiti; descriptor ids `country_watch_<iso2>`; adding a country is
register-a-target, no code — §7.2). The eighth, `proliferation_watch`, is
narrower: it is instead scoped by `has_tag("nuclear_watch")` to only the ~8
nuclear-relevant desks (`country_g20_{cn,in,ru,us}` + `country_watch_{il,ir,kp,pk}`).
The ninth, `disruption_status`, is scoped off the country plane entirely —
`has_tag("supply_chain")`, the thematic lane/flow desks, on a 24h window rather
than 72h.
Each run: **ASSEMBLE** a cited 72h raw-signal
slice + the §5.8 grounding preamble of ACCUMULATED facts/nexuses/situations (e.g.
"US head of government Trump since 2025-01-20; US–Iran active conflict since
2026-02-28; NATO member since 1949"), so the system integrates over time, not just
today → cited **SYNTHESIZE**
(a strict-JSON finding whose prose carries `[N]` citation markers mapped to signal
ids) → the **VERIFY** pass below → an `effective_confidence` fold + drill-to-source
provenance. Skill is a **per-unit** number (§ skill scoreboard), never a platform
boast.

**2 — The mandatory faithfulness verify pass.** Every cited finding — and every
journal entry's cited fact claims (the journal verify profile; perspective
spans are exempt and the entry is never mutated) — is scored for
**faithfulness ∈ [0,1]** — *does each fact-asserting claim follow from its cited
evidence?* — by `verify_finding_faithfulness` (`data/provenance/verify.py:263`).
Two components:

- a **deterministic citation-presence floor** (always on): every fact-asserting
  claim in the prose is checked against the resolved `data['citations']` bridge; a
  claim that asserts a fact with **no** `[N]` marker, or whose marker resolves to
  no real `signal_id`, is an **unsupported** span, and the score is the fraction of
  checkable claims that are supported;
- an **LLM judge**, resolved through its own repointable route
  (`LEGBA_JUDGE_STACK_REF` env > `method.llm.judge` > `.verify` > `.primary`)
  — that refines the per-claim verdicts. The **shipped descriptor default is
  same-model** (`llm.primary.openai_compat` judging its own plane's prose —
  self-hostable, with the known limitation that a model verifying prose from
  its own family shares its blind spots); the **reference deployment sets the
  env rung to a hosted cross-family judge** (Gemma on Cerebras,
  `llm.judge.cerebras_gemma4_31b.openai_compat`), so what judges live findings
  is a different model family from what wrote them — the generated
  [RELEASE_STATE.md](RELEASE_STATE.md) reports both layers. Every critique
  stamps `judge_llm_ref` and a `judge_pipeline_version` so verdict populations
  from different judges or rule revisions never pool. It is **soft-fail**:
  when the judge flag is off or the judge is unreachable the result
  **degrades to the deterministic floor** (`judge_status='deterministic'`,
  published PROVISIONAL under a ceiling), never a fabricated number — and a
  `judge_availability` gauge pages when the judge goes quiet.

The verdict is persisted as a `critique`, and the fold
`effective_confidence = min(confidence, faithfulness_score)` is applied **at read
time**. Verification **never hard-deletes** a finding — a low score gates a visible
low-confidence tier. This is deliberate: Legba **measures
groundedness, not truth**. A planted fabrication with no supporting citation is
flagged unsupported.

**3 — Per-country composition.** `country_composition`
(`descriptors/analyst_country_composition.yaml`, the `meta_findings_synthesizer`
kind fanned out per desk on the same `has_tag("g20") or has_tag("watch")` coverage
tag) reads the seven broad verified units for its country, plus `proliferation_watch`
on nuclear desks, and
writes a hedged, cited synthesis. Its read slice admits **only verify-passed
sub-claims above the floor** — an **INNER JOIN on the faithfulness critique** — so
an unverified sub-claim can never enter a composition (`proliferation_watch`
naturally contributes zero rows on the 24 non-nuclear desks, not an error). A
country whose units
produced no verify-passed sub-claim yields an empty slice → the kind emits a
`confidence=0.0` "no source findings to synthesize" finding rather than inventing a
read. Supersession keeps **one live head per desk**.

**4 — Per-region composition.** `region_composition`
(`descriptors/analyst_region_composition.yaml`, the same `meta_findings_synthesizer`
kind) composes the verified per-country reads into **five region frames** — Africa,
Americas, Europe, Indo-Pacific, MENA (targets `region_*`) — each a cited, hedged
regional synthesis over its member desks. Same INNER-JOIN-on-verify discipline; same
empty-slice honesty.

**4b — Thematic composition.** `escalation_composition`
(`descriptors/analyst_escalation_composition.yaml`) is a **thematic cross-desk**
composition (not geographic): it fans over the per-desk escalation reads and
synthesizes a single cross-desk escalation picture. It carries a **correlation
guard** (`data.correlation_guard`) so correlated desks are not double-counted, and
its output passes the same faithfulness verify.

**5 — World composition.** `world_assessor` (the same `meta_findings_synthesizer`
kind running **globally**, `analyst_world_assessor.yaml:44`) composes over the
per-region compositions into a cited, hedged world view that drills world → region →
country → units → source. It **graduated into** this role — it is **not** the retired
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
**correctness** via the shared `correctness_axis` module over the weekly
gold-set verdicts (`correctness_labels` — the scorer had been reading the dead
`unit_reference_labels` table, fixed 2026-08-03) — **honest-null** where a unit
has no labels, with tiny-n labeling (the first labeled cohort is n=8; the
deterministic reference leg is still n=1, reported insufficient-sample);
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
`docs/DATA_SOURCES.md` for the catalog table and the 3 / 46 / 49 scope model. A
2026-07 breadth wave adds **51 draft source descriptors**: 41 verified no-auth
Wave-A feeds (`scripts/bringup_register_wave_a_sources.py`) plus 10 riding a
profile-gated local **RSSHub** lane — compose profile `sources-extra`, loopback
`:1200`, reached by the ordinary `rss` handler through the SSRF guard's
`LEGBA_EGRESS_ALLOW_HOSTS` allowlist (compose default `rsshub`) — all
registered `state: draft`, activated operator-paced; see `ACQUISITION.md`.) **Enrichment mutates the signal in
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
> — and (b) is capped at `max_groups_per_run`
> (`DEFAULT_MAX_GROUPS_PER_RUN = 500`) with a stable `ORDER BY content_hash`, so
> each run does bounded work and the backlog drains across successive cadences.
> The semantic pass is bounded the same way
> (`DEFAULT_MAX_SEMANTIC_CANDIDATES = 500`); it used to be *unbounded*, returning
> ~100k rows a run.

> **And it is a SINGLETON (2026-08-02).** The descriptor used to carry a bare
> `subscription.targets` block with no predicate, which the runtime reads as
> "fan out to every active target" (`AnalystActor._cadence_targets`) — so a
> sweep documented as target-agnostic ran 44 times a cadence, each copy
> executing the identical full-pool query, 43 of them pure waste. `target_id`
> never narrowed a query; the handler read it once, to suffix the finding title.
> Dropping the block (the same shape `signal_embedder` and `entity_resolution`
> already use) is what actually made it target-agnostic. Measured effect: this
> analyst was 5,496 runs and 61.9 of 73.6 analyst-hours per day, and the
> dominant Postgres load in the system; per-run cost fell from ~24s to ~0.85s
> when the per-candidate PK lookup and the per-candidate Qdrant round trip were
> replaced by one set-based gate query and one batched `query_batch_points`.
> The cost is that it leaves the REACTIVE plane — no per-target
> `TriggerRegistration` means no `coalesced_fire`, so it runs on the 15-minute
> cadence only. That is sufficient by measurement (~48k groups and ~48k semantic
> candidates a day against ~4k signals/day of ingest) and the sweep is
> idempotent, so nothing is lost by arriving a few minutes later.

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
   descriptors. **One selector binds all 32 desks** (the 19 G20 targets + the
   13-country `watch` tier) — no per-target enumeration. **The predicate is the
   only thing that defines a desk family**, which is why a whole new domain
   costs no runtime change: the supply-chain pack registers **10 thematic desks**
   — six chokepoint/shipping `lane_*` frames and four `flow_*` commodity frames,
   each tagged `[supply_chain, disruption, <lane|flow>]` — and one unit
   (`disruption_status`) selecting `has_tag('supply_chain')` fans out over them
   through this identical path. Three lanes are active (Hormuz, Red Sea, Malacca
   / South China Sea); the other seven ship `draft` and match nothing until an
   operator activates them, so the tier grows one measured desk at a time rather
   than all at once.
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
(the in-voice field-notes seam + the NARRATE synthesis) resolves a second handler
(`method.llm.narrate`) that ALSO runs on the **core plane**
(`llm.primary.openai_compat` — it previously ran on Anthropic Opus 4.8 but moved
fully to core 2026-07-06, so the journal costs NO Anthropic spend; that plane is
reserved for `consult`/`deep_consult`). So `max_tokens` governs only the narrate
voice output — it is never sent to the vLLM gather, which uses its own server
budget; the deep agentic loop is local. The deps builder reads `method.llm.narrate.raw` (optional;
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

### 8.5 The alert-sink plane — verification-gated alerting (2026-07)

The 2026-07-28 wave closed the loop from a verified state change to an
operator's device without the console open. It is two decoupled halves: a
**deterministic trigger analyst** decides *what* is alert-worthy (documented
with the other analysts in `ANALYSIS.md` — `alert_trigger_scan`, seven trigger
classes: six over verified state transitions plus the production gauge's
`production_deficit`, with durable watermarks so a transition never re-fires), and a **modular sink plane** (`src/legba/data/alerts/`)
decides *where* it goes.

- **The dispatcher.** `AlertSinkDispatcher` (`data/alerts/sinks.py`) fans one
  converged `AlertSinkPayload` out to every registered sink (`AlertSink` is a
  `@runtime_checkable` Protocol; `register_alert_sink` adds one — a new sink is
  one module + one register call). It is built process-wide at host bring-up
  (`source_first_runtime.py`) and reached from four edges: the escalation pack
  (`agency/tools.py`, channel `escalations`), the liveness watchdog's
  global-stall edge (channel `liveness_stall`), `alert_trigger_scan` (channel
  `trigger_scan`), and `geo_convergence_scan`.
- **A ledger row per outcome.** Every sink attempt lands an
  `alert_sink_deliveries` row — `delivered` / `failed` /
  `skipped_unconfigured` / `skipped_cooldown` (below-severity and
  duplicate-suppressed attempts write no row); the sink target URL is
  redacted to host-only before it is ledgered. Per-alert idempotency keys on
  the alert's output-row id (a bounded in-process LRU — a restart may
  re-attempt, which is the right failure direction for an alert).
- **Anti-noise, honestly.** A per-sink cooldown
  (`LEGBA_ALERT_SINK_COOLDOWN_SECONDS`, default 60s) suppresses bursts — but
  suppressed alerts **coalesce onto the next delivery** ("+N more alert(s)
  during cooldown" with a bounded preview and an honest overflow line),
  never silently thin. And an unconfigured sink **drops out of fan-out when a
  configured sibling exists**; when *no* sink is configured, every sink stays
  in so the `skipped_unconfigured` ledger rows keep the gap visible.
- **Two sinks ship.** A generic webhook sink (`LEGBA_ALERT_WEBHOOK_URL`,
  severity floor `LEGBA_ALERT_WEBHOOK_MIN_SEVERITY` default `high`) and a
  native **ntfy** push sink (`data/alerts/ntfy_sink.py` — `X-Title` /
  `X-Priority` / `X-Tags` and a tap-to-open `X-Click` receipt link;
  `LEGBA_ALERT_NTFY_URL` / `_TOKEN` / `_MIN_SEVERITY`). A profile-gated local
  ntfy service ships in compose (profile `alerts`, loopback `:8093`, cache
  volume so topic history survives recreates) — the last inch to a phone is
  the ntfy app pointed at an operator-exposed vhost.
- **Verification posture is mandatory.** The payload's `verify_state` is
  never empty: a real `faithfulness=<score>` where a verify verdict exists,
  else an explicit `unverified — <reason>`. Every alert carries a receipt
  link into the lineage API (`/api/v1/lineage/{row_kind}/{row_id}`; absolute
  when `LEGBA_PUBLIC_BASE_URL` is set).

### 8.6 The evidence archive — the receipt chain's terminal copy

Before this wave the receipt chain ended at a URL — and URLs rot. The
**evidence archiver** (`data/analysts/deterministic_handlers/
evidence_archiver.py`, a deterministic cadence analyst) fetches the original
bytes of signals **cited by verified findings** — the selection is
verified-cited-only: a signal in the `derived_from` of a non-superseded
finding whose faithfulness critique clears the floor, with no `object_ref`
yet and a non-empty `canonical_url` — and stores them content-addressed
(`data/archive.py`: `<root>/<sha256[:2]>/<sha256>` under
`LEGBA_ARCHIVE_ROOT`, atomic temp-file + rename, dedup by content). The
signal's `object_ref` becomes **`cas:sha256/<hex>`** — a deliberately
backend-agnostic relative address (a later object-store swap rewrites zero
rows) — its `retention_class` is upgraded to `evidence_hold`, and its
extracted full text is marked for re-indexing into the search corpus. A
sidecar row (`evidence_archive`, migration 0104 — **no FK**, so archived
evidence outlives any future signal purge) records the outcome:
`archived` / `failed` / `skipped_license` / `skipped_size`.

The fetch path is guarded: an SSRF egress guard, per-host politeness
(2s default), a hard 20 MB size cap, and the **LIC-2 license gate** — a
source whose declared
`license_class` is in the forbidden set is *skipped with an honest recorded
counter*, never quietly fetched; an unknown class archives with the class
recorded for later re-evaluation. Lineage, export, and signal projections
carry the `archived` state + hash, so a receipt walk now terminates in a
verifiable local copy rather than a rotting URL. What is deliberately NOT
built — retention/expiry over the archive (nothing deletes evidence), an
object-store backend, and beyond-cited coverage — is declared in
`SEAMS.md` #42.

### 8.7 The operator read surface — the v3 route family + the MCP built-ins

The wave added a family of read routes under `/api/v1/v3` (all bearer-gated
like the rest of §4 in `RUNBOOK.md`):

| Route | What it serves |
|---|---|
| `GET /v3/since?cursor=&channel=` | "what changed since" — verified-new (floor + exempt gates) / superseded reversals / band changes / situation edges / alerts, stateless with a client-owned cursor (90-day cap, per-section cap with honest `total` + `truncated`; `channel=` scopes the alerts section). Backs the console's movers view |
| `GET /v3/timeline` | the validity-window timeline — facts / situations / findings as temporal ranges + supersession edges (the temporal substrate's first temporal read) |
| `POST /v3/export` | collection basket → markdown / JSON with live-resolved citations (pruned refs read `resolved: false`, never faked), verify states, receipt links; 50-item cap → 413 |
| `GET /v3/narratives` (+ `/echo`, `/{contention_id}`) | reified narratives + the directed source-echo graph (detect-only; every envelope carries the descriptive-not-causal honesty note) |
| `GET /v3/eval/desk_baselines` | the per-desk statistical baseline board (explicitly NOT a forecast) |
| `GET /v3/eval/goldset/worksheet` + `POST /v3/eval/goldset/label` | the weekly correctness gold-set labeling loop |
| `GET /v3/eval/band_trajectory` | per-desk band history for the calibration view |
| `GET /v3/eval/calibration` | grew an **additive** `band_calibration` section (no Brier — stated on the route) |
| `GET /v3/source-quality` (+ `/v3/sources/{id}/quality`) | the **merged source-quality ledger** (C3, migration 0115): asserted (Admiralty grade + dossier + per-host credibility score) / earned (contested track record) / computed (cadence-derived freshness) as three typed sections that are never blended — no composite score exists. Supersedes the two rows below |
| `GET /v3/sources/{id}/assurance` | **deprecated** (`Deprecation` / `Sunset` / `Link: rel=successor-version`, superseded by `/quality`; keeps serving its original shape — a 3xx would hand callers a different body). The source-assurance ledger read (ratings + dossier + earned track record; `include_private` opt-in seam) |
| `GET /api/v1/source_credibility` (+ `/{host}`) | **deprecated** likewise (successor `/v3/source-quality`). The per-host credibility READS only — the PUT / DELETE / bulk writes are untouched, since a read surface has no successor for them |
| `GET /v3/system/staleness-debt` | the `claim_watch` review-flag debt gauge — previously readable only from the producing run's `analyst_traces` receipt. Computes the headline number with the matcher's own SQL (mirrored under a byte-equality drift guard) and carries a hard-`false` `match_verified`: flags found, match unverified (SEAMS #49) |
| `GET/POST/PUT/DELETE /v3/watchlist` | operator standing watches — **the first WRITE surface in the v3 route family** (update is PUT, delete is soft — `active=false`) |
| `GET /v3/collection-requirements` (+ `/{id}`, `PATCH /{id}`) | the durable collection-requirement backlog — **disposition-only**. There is no POST and no DELETE (a test asserts both 404/405), the PATCH may touch only `status` / `reviewed_by` / `reviewed_at` / `disposition_note`, and nothing on this route can register or activate a source. `ACQUISITION.md` §6.1 |
| `GET /v3/system/source-firing` | now grades each source's freshness (`ok` / `stale` / `warn` / `empty` / `ungraded`) against a cadence-derived budget |

The **MCP surface** (`src/legba/ui/mcp_server.py`, the `legba-mcp` stdio
image) now ships **seven built-in tools** — `substrate_findings`,
`substrate_situations`, `substrate_signals`, `lineage_walk`, `since`,
`export`, `consult` — fixing the standalone-empty catalog (previously the
catalog was descriptor-driven only, and a standalone container saw none).
Built-ins are reads + consult **only** (`assert_reads_and_consult_only()`
— no registry mutation rides MCP); the descriptor-driven catalog remains
the second source, and a built-in wins a name collision.

### 8.8 The external-retrieval plane — search as a stack family

Until this wave every byte Legba reasoned over came from a **registered
source**: a curated feed, polled on a cadence, with a credibility grade and a
provenance chain. External search breaks that invariant deliberately — a
web-retrieved document is a **new exogenous input** with no source descriptor
behind it — so the whole design is about paying for that honestly rather than
quietly folding open-web text into the same pool.

**A family, not a special case.** `search_provider` is a ninth stack-component
kind (`KIND_MODELS` in `registry/stack.py`), registered, credentialed,
health-checked and addressed by component id exactly like `llm_provider` /
`embedding` / `nlp_service` / `vector_store`. Per-provider handlers sit behind
a subprovider key — a **SearXNG** handler and a **generic JSON** handler ship;
several further names are declared in the config vocabulary and fail **loudly
at bind time** if registered, never silently. Resolution copies the judge
route's ladder rung-for-rung, including the property that matters most: an
**opt-in gate comes first** — a descriptor whose search block names neither a
`primary` nor a `fallback` gets no route at all, so `LEGBA_SEARCH_STACK_REF`
can *repoint* an already-opted-in surface but can never *enable* one.

**The honesty contract — measuring absence instead of assuming it.** This is
the interesting part, and it exists because of one specific failure: a search
that quietly returns zero results reads, to any downstream reader, exactly like
"nothing exists". Five statuses separate those cases:

| `SearchStatus` | Condition | May it support an absence claim? |
|---|---|---|
| `ok` | results returned, provider healthy | n/a |
| `degraded` | provider admitted partial service (e.g. unresponsive engines), results returned | n/a — results usable, degradation surfaced |
| `empty` | zero results, engine liveness **not yet measured** | **no** — this is *unknown* |
| `empty_verified` | zero results, and a control probe showed the engine set answering *at that moment* | **yes — and only a scoped one** |
| `degraded_empty` | zero results with the plane degraded, or the liveness probe returned dead / failed outright | **no** — returned as a tool **failure** |

Three consequences are worth stating plainly:

- **A degraded empty is a failure, not a result.** `web_search` returns
  `status="failed"` with an error naming the reason
  (`search_degraded_no_results` / `search_liveness_unverified`) and telling the
  caller in words *"this is UNKNOWN, not absence — do NOT conclude that no
  evidence exists."* Returning it as a successful zero-result search would be
  technically accurate and practically a lie.
- **Liveness is measured, not assumed.** A fixed, deliberately non-topical
  control probe (a two-token globally-indexed proper noun that every general
  web engine has millions of documents for) asks the engine set whether it is
  answering at all. Results ⇒ live; zero ⇒ dead; an exception ⇒ probe-failed,
  which is folded into the same degraded treatment as dead. The verdict is
  cached **per provider** for 5 minutes (operator-tunable, and the code
  explicitly refuses a zero TTL — that would mean one extra upstream query per
  empty result), with concurrent callers collapsing onto one in-flight probe.
- **Even a verified empty licenses only a scoped sentence.** The response
  hands back a fixed `absence_statement` naming the exact query and timestamp,
  ending *"That is a SCOPED absence — it does NOT establish that no such
  reporting or subject exists."* The `web_access` pack's rules repeat the
  prohibition on escalating it to "there is no reporting on X".

**`web_access` is a pack, and the pack is not the capability.** It grants two
tools — `web_fetch` (one SSRF-guarded GET) and `web_search` — and is held by
three surfaces: `corpus_researcher`, `consult_default` and `deep_consult`. The
three-way agency gate (§5.6) still applies, and on the two consult surfaces the
grant is presently the **grant leg only** — inert until the runtime binds a
`web_access`-aware GATHER path. Registering a component activates nothing by
itself; the local SearXNG service ships behind a `search` compose profile and
is **off by default**, and even started it changes no analyst behaviour until
an operator points a component at it, opens egress to it, and the pack/target
grants line up.

**`retrieval_origin` — and why web evidence is demoted.** Migration 0112 adds a
`retrieval_origin` axis to `signals` and `evidence_archive`. `NULL` (every
pre-existing row — there is no backfill) means a curated registered source;
`web_search:<component_id>` marks a row that came in through a named external
provider. It is a **code convention, not a CHECK** — one resolver serves both
the archive gate and the corpus facet so the two cannot drift.

The axis exists to keep one number honest. Web-retrieved evidence resolving a
calibration outcome is stamped `web_evidence` and lands in the **weak
calibration tier**, alongside the lexical `subsequent_facts` resolver — and the
weak tier is structurally excluded from the exogenous set the **headline Brier**
is computed over. It is reported, with its own sample size, beside the headline
rather than inside it. The reasoning is that an exogenous resolution the system
went and found for itself is a materially weaker claim than one that arrived
independently, and pooling them would let the system improve its own headline
score by searching harder.

**And the archiver fails closed.** The evidence archiver will not fetch and
store the bytes of a **web-origin** row whose licence class is unset or
`unknown`: it records a `skipped_license_unreviewed` sidecar row with the URL,
licence class and origin, and skips the download. Curated sources keep the
older posture (unknown licence archives, class recorded) because a registered
source has been through an operator; an arbitrary open-web domain has not.

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
FAITHFULNESS delta** measured on the **same faithfulness judge** (whatever the
judge route resolves — the verify pass above) that gates
the live unit findings (live: parent 0.34 → candidate 0.29, delta −0.05). It stays
`promotion_gate=human_gated` and can **never** auto-promote on a degenerate, absent,
or non-positive delta — an insufficient-sample / judge-unavailable delta is
honest-null, never faked to 0.0.

**MOTHBALLED (RUST-4, decision 2026-08-21).** The GEPA optimizer plane above is
honest history, not current operation: one real `dspy_gepa` compile ever ran
(2026-08-10), its candidate landed below the promotion bar (faithfulness delta
−0.5354 against a +0.03 floor), and the manual VOICE-4 prompt wave shipped in
its place. Code, tests, and the `legba-dapr-workflow-worker` image all stay —
the worker still hosts the actively-used `deep_consult_workflow` — but both
optimizer descriptors are annotated `state: paused` and
`optimizer.py::run_method` refuses loud (`OptimizerMothballedError`) rather
than running. Details, evidence, and the restore path: `docs/SEAMS.md` #53,
`planning/RUST4_EVIDENCE_2026-08-21.md`.

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
(empty volumes, the `0001_baseline.sql` baseline + the forward chain under
`src/legba/data/migrations/`, current head **`0185`** — the early arc
`0032`…`0085` being the
`facts`/`nexuses`/`seed_batches` schema plus the entity-profile composite key
(`0035`), signals retention (`0036`), the AGE output label (`0037`), the ACH
`resolved_outcome` column (`0038`), the consult-sessions tables (`0039`),
**situations-as-first-class + repairs (`0040`–`0042`)**, the data-quality
backfills (`0043`–`0045`), `source_poll_outcomes` (`0046`), the `acute_forecasts`
pilot table (`0047`, §5.10), the journal table (`0048`), the
receipt/derived-from repairs + data cleanups (`0049`–`0053`), the contested-claims
schema — `facts.source_credibility` (`0054`) + the `fact_contention` sidecar
(`0055`, §5.9), a second dangling-`derived_from` prune (`0056`), the
`unit_reference_labels` correctness-gold table (`0057`, §5.10), and the
composition-supersession fold + critique index + null-target composition-head fold
(`0058`–`0060`), the contested-claims + data-quality-program migrations
(`0061`–`0075`), the 2026-07-06 audit sweep (`0076`–`0080`), the
signal-content-depth / NER-reenrich wave (`0081`–`0085`: the OpenSearch-corpus +
Qdrant-embedding + NER-backfill markers), the entity-identity / salience /
journal-data wave (`0086`–`0090`), the 2026-07-28 release wave
(`0091`–`0105`; `0095`/`0100` intentionally unused — alert-trigger watermarks,
poll `newest_entry_ts`, band-calibration claims, the source-assurance ledger,
correctness labels + gold-set pinning, contention surfacing + the tie-break
cache, fact-decay states, source track records, the traces-retention index,
narratives + echo edges, desk baselines, the evidence archive, and the
watchlist), and the 2026-08 arc through `0185` (bearing edges + review flags,
corpus tombstones, entity-graph backfills and merge repairs, the situation
trajectory ledger `situation_events` at `0184`, and the proposed-edge repoint
at `0185`) — see `DATA_MODEL.md` for the per-migration tables):

- Real RSS sources (BBC / Deutsche Welle / Al Jazeera) acquire enriched signals —
  geo, language, and entity-class promoted to indexed columns; the
  `fact_extractor` stage extracts temporal `facts` (`source_type=ingestion`,
  ≈3.7k of ≈4.6k total) with `valid_from` stamped + value-change supersession.
- Fan-out on `legba.signals.>` routes them to the country desks, each
  subscribing by a geo/tag predicate; **seven of the nine bounded units** each bind to all
  32 desks (the 19 G20 targets + the 13-country `watch` tier) via a single
  `has_tag("g20") or has_tag("watch")` selector and fan out one run per desk. The
  eighth, `proliferation_watch`, binds instead to only the ~8 nuclear-relevant
  desks via `has_tag("nuclear_watch")`; the ninth, `disruption_status`, binds to
  the thematic supply-chain desks via `has_tag("supply_chain")` — the same
  selector mechanism pointed at a non-country desk family.
- The units produce distinct per-country findings, each **cited to source, put
  through the mandatory faithfulness verify pass** (deterministic floor + the
  route-resolved LLM judge — cross-family on the reference deployment), and
  stamped with `derived_from` provenance + receipt
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
  `canonicalize_entity` (`data/_entity_canon.py`: HTML /
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
  (`POST /api/v1/consult`, `consult_on_demand` kind, 19 governed read tools incl. the
  live semantic `search_context` corpus search + the `search_corpus`/`read_document`
  OpenSearch full-text readers) in
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
- **The `claim_watch` closer** — the watcher half is built and live; the
  closing half (propagate a confirmed match back into the producer's next run
  and close the flag by supersession) does not exist in-tree. Its
  match-precision gate has since been measured across four out-of-plane rounds
  — 0.279 pooled at first measurement, 0.908 on the live bearing-gated stream
  at round 4, over the 0.85 bar — so building the closer is now a held
  operator decision, not an unmet measurement (`docs/SEAMS.md` #49,
  `ANALYSIS.md` §4.5.3).
- **The search control-query canary** — the liveness probe itself is built and
  fires **reactively**, on an empty search (§8.8). The *scheduled* half runs
  too: `scripts/host_search_canary.sh` (`RUNBOOK.md` §24.1) is installed as a
  host cron line on a 15-minute clock, paging only after two consecutive
  not-live probes (`docs/SEAMS.md` #50, closed).
- **Retention sweeps** — one shared engine and a `retention_policies` config
  table are built; both seeded policies ship at `ttl_days = 0` (disabled).
  `/v3/retention-policies` (list/get/PATCH) edits `ttl_days` / `keep_classes` /
  `batch_size` / `enabled` / `description`; `policy_name` / `table_name` /
  `env_fallback_var` — the code-side pairing to a Python retention adapter —
  stay SQL-only, and there is still no create/delete route.
- **Time-series metrics** — the former TimescaleDB metrics store was removed; it
  is a declared seam now (no metrics store; `anomaly_detection` reads
  `time_bucket()` from the primary Postgres pool) — see SEAMS. (Full-text search
  is **NOT** a seam: it is LIVE on a single-node OpenSearch cluster — the
  `legba_signals_corpus` corpus, indexed by `corpus_indexer` and read via
  `search_corpus`/`read_document`. `search_signals` still uses Postgres FTS.)
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
