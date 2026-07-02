# Legba — End-to-End Flows

This document walks how data moves through Legba: first the whole pipeline end
to end in one pass, then fifteen "life of a …" walkthroughs, each a numbered
step list with `file:line` citations into the code. It is for developers and
operators who want to trace exactly what happens at each stage. New here?
Start with the [README](../README.md) and the [Tour](TOUR.md).

**Contents:**
[The whole pipeline, end to end](#the-whole-pipeline-end-to-end) ·
[walkthrough index](#the-walkthroughs) ·
[1 signal](#1-life-of-a-signal-live) ·
[2 analyst cadence cycle](#2-an-analyst-cadence-cycle-live) ·
[3 consult](#3-a-consult-live) ·
[4 optimizer workflow](#4-the-optimizer-dapr-workflow-live--scoped-to-one-measured-unit) ·
[5 deep consult](#5-a-deep-consult-live) ·
[6 fact extraction](#6-fact-extraction--supersession-live) ·
[7 nexus reification](#7-nexus-reification-live) ·
[8 ACH hypotheses](#8-ach-competing-hypotheses-live) ·
[9 seeding](#9-seeding-import-live) ·
[10 grounded assessment](#10-a-grounded-assessment-live) ·
[11 journal](#11-a-journal-entry-live--on-cadence-entry-12h--consolidator-daily) ·
[12 unit + verify](#12-a-bounded-reasoning-unit--the-mandatory-verify-pass-live) ·
[13 composition](#13-composition--per-country-then-world-live) ·
[14 scorecard](#14-the-banded-scorecard-live) ·
[15 scoreboard + forecast](#15-the-skill-scoreboard--measured-forecast-live--no-proven-skill-yet) ·
[appendix: entry-point index](#appendix--primary-entry-point-index)

---

## The whole pipeline, end to end

One pass over the whole pipeline before the detailed walkthroughs. Read the
diagram left to right; every arrow is a substrate boundary (a table or a
stream), not a function call. The left half is ingestion-to-output; the right
half is the product spine — bottom-up composition over already-verified
claims. Where this overview and a detailed flow disagree, the detailed flow
(with its `file:line` citations) wins.

```
SOURCE ─► canonical Signal ─► predicate fan-out ─► the (analyst,target) fire ─► typed Output
        (Tier-1 inline        (coarse NATS subject   (cadence reminder OR         (12 OutputKinds →
         enrich, once)         + SQL WHERE +          reactive coalescer,          facts / nexuses /
                               Starlark residual)     one cooldown CAS)            situations / …)

                         … then the product spine, bottom-up, over VERIFIED claims:

    4 bounded UNITS ─► per-country COMPOSITION ─► world COMPOSITION ─► banded SCORECARD
    (inline_target,     (country_composition,       (world_assessor,      (scorecard_producer,
     cite + verify       INNER JOIN on the           composes country      deterministic rules
     each finding)       verify critique)            reads)                over verified claims)
```

For scale: the substrate is a continuously growing temporal knowledge graph —
on the order of ~4.6k facts and ~4.9k nexuses at the time of writing, with
`valid_from` / `valid_until` / decay — fed by ~50 poll sources (RSS / API /
bulk) on cron. The three-feed set (BBC / Deutsche Welle / Al Jazeera) is the
cold-start verification minimum, not the deployed scope; a fresh instance
reaches current scope by running the full source catalog
(`scripts/bringup_register_source_catalog.py`). The exemplar domain is
geopolitics over 24 country desks — the 19 G20 countries plus a
high-consequence watch tier; a "desk" is a scoped subject a set of analysts
work, not a surveilled entity — but the domain is configured by YAML
descriptors, not baked into code.

### Stage by stage

**SOURCE → canonical Signal.** A `SourceCore` actor pulls raw entries on a
cadence and runs the per-source baseline **once** over each one, producing a
single canonical, target-agnostic `Signal` (no `target_id` — an observation,
not an interpretation). The baseline is the Tier-1 inline tier: deterministic,
no analyst LLM, run synchronously per-signal *before* fan-out. Its enrichment
chain (`language_detect → geocode → ner → classify → source_credibility →
ingest_dedupe → fact_extractor`) writes the enriched signal columns, ingestion
`facts`, and entity rows + `signal_entity_links` in place. (A webhook front
and an S1 NATS inbound path also exist, but they are dormant plumbing until a
live webhook source is wired; the poll path is what runs today.) Flow 1;
`ACQUISITION.md` §1–§3.

**Signal → predicate fan-out.** The written signal is published immediately to
a coarse NATS subject (`legba.signals.>`). Each target has one aggregated
durable pull consumer bound to the union of its subscription subjects;
delivery is then narrowed exactly by a two-stage match — an indexed SQL
`WHERE` over `geo` / `tags` / `entity_classes` / `languages` / `modalities`,
then a Starlark residual predicate evaluated under a 5 ms wall-clock budget.
The coarse subject narrows *delivery*; SQL + Starlark decide the *match*.
Flow 1 steps 5–8; `ACQUISITION.md` §4.

**Fan-out → the (analyst, target) fire.** A matched signal marks an
`(analyst, target)` pair dirty, and the analyst fires on a gate, not
per-signal — see "one fire, two doors" just below.

**The fire → typed Output.** The analyst reads its substrate slice, runs its
method, and writes one typed output validated against its kind's pydantic
model. There are exactly twelve `OutputKind` values
(`data/provenance/kinds.py`), routed by the universal write path to
`situations` / `hypotheses` / the knowledge-layer `facts` / `nexuses` tables,
the dedicated `journal_entries` table, or the generic `analyst_outputs` table;
there is deliberately no `signal` kind. Flow 2 steps 10–13; `ARCHITECTURE.md`
§8.1.

### One fire, two doors

The Dapr-reminder **cadence** cycle (Flow 2) and the reactive **trigger**
plane (`ANALYSIS.md` §2) are not two systems — they are two ways the *same*
`(analyst, target)` pair becomes eligible to fire:

- **Cadence door.** On activation the primary actor registers a single Dapr
  reminder (`run_cadence`); on each tick it resolves the matched targets and
  fans them to worker actors. This is the floor — a quiet target still gets
  re-evaluated on schedule. (A `subscription.targets` block makes a run
  per-target; its absence makes a single global META run — the four units and
  the per-country composition fan out; `world_assessor` and
  `scorecard_producer` run globally.)
- **Reactive door.** A matched signal (or a new upstream finding —
  event-class `derived`, just another signal on the stream) marks the pair
  dirty via the coalescer (`runtime/triggers/`), which fires on whichever gate
  trips first — severity, accumulation, or cadence-with-pending — clamped by
  a per-pair cooldown.

The two doors converge on one lock: the coalescer CAS-claims the fire on the
accumulator's last-fired anchor (`claim_fire`) so exactly one worker wins, and
the worker run then re-checks a per-target cooldown (with a slack band that
absorbs drift when `cooldown ≈ cadence`) before doing work. Net: a pair fires
once per cooldown window no matter how many doors opened, and LLM-bearing
analysts are floored to a minimum batch — never per-signal. Flow 2;
`ANALYSIS.md` §2.

### Two tiers — cheap once per observation, expensive once per slice

- **Tier 1 — inline, deterministic, per-signal, pre-fan-out.** The
  `data/filters/` baseline chain above (spaCy NER, geo resolution, relation
  facts, dedup — no analyst LLM). It writes altitude-0 product that every
  downstream target reads: "enrich once". Flow 1 is this tier; Flow 6 is the
  `fact_extractor` stage inside it. `ARCHITECTURE.md` §0 / §6.1.
- **Tier 2 — slice/cadence, reasoning, per-(analyst, target).** The
  `data/analysts/` kinds read an accumulated substrate slice on a fire and
  reason over it — never per-signal. This is where LLM cost lands, and the
  coalescer is the device that decouples that cost from signal arrival rate.
  The core analyst plane is a self-hosted gpt-oss-120B
  (`llm.primary.openai_compat`, $0); Claude Opus 4.8 is reserved for the
  billed `consult` / `deep_consult` surfaces. Flows 2–5 and 7–15 are all this
  tier. `ANALYSIS.md` §3.

### The product spine — units → composition → scorecard

Bottom-up composition in which nothing rises to a higher altitude until it has
been cited and verified at the lower one. Four layers, each reading only the
verified output of the one below:

1. **Four bounded reasoning units** (`kind: inline_target`):
   `leadership_transition`, `energy_security`, `escalation`,
   `narrative_coordination` — each scoped across the 24 desks by
   `has_tag("g20") or has_tag("watch")` and answering one narrow question. Per
   run it assembles a cited 72h signal slice plus a grounding preamble,
   cite-synthesizes a strict-JSON finding whose prose carries `[N]` citation
   markers mapped to signal ids, runs the mandatory faithfulness verify, and
   folds `effective_confidence`. Skill is a per-unit number, not a platform
   one; the units stagger across the clock to spread the shared token budget.
   Flow 12.
2. **Per-country composition** (`country_composition`): one hedged, cited
   second-order finding per desk over that desk's four verified units
   (`[[ref:N]]` markers to the sub-claim findings). Its read slice INNER JOINs
   on the faithfulness critique above the floor, so an unverified sub-claim
   never enters the composition; an empty slice yields an honest
   `confidence=0.0` "no source findings to synthesize" finding, never an
   invented read. Flow 13.
3. **World composition** (`world_assessor`, repointed from its old raw-signal
   role): one global run composing the per-country reads into a cited, hedged
   world view, surfacing cross-country disagreement rather than averaging it,
   drilling country → units → source. Flow 13.
4. **Banded scorecard** (`scorecard_producer`, deterministic — pure SQL, no
   LLM): one banded row per active desk per tick, from a few high-precision
   demote-never-promote rules over already-verified claims in a 14-day window.
   Every band names the verified-claim id it rests on; a dimension with no
   qualifying verified claim reads `insufficient-evidence` with an explicit
   machine reason, and per-claim faithfulness below the floor demotes to
   `low-faithfulness`. Flow 14.

The live scorecard is a mix, by design: some countries band, others read
all-`insufficient-evidence` (e.g. the US, whose unit faithfulness is genuinely
low, so the rules refuse to manufacture a band). That is intended behavior,
not a gap.

### The mandatory faithfulness verify

Legba measures **groundedness**, not truth: the verify pass asks *does each
claim follow from its cited evidence?* — not *is the claim true in the
world?*. Every cited finding is scored in `[0,1]` by two layers
(`data/provenance/verify.py`): a **deterministic citation-presence floor**
(always on — a fact-asserting clause with no `[N]` marker, or whose marker
resolves to no real signal id, is an unsupported span), and an **optional LLM
judge** (flag-gated by `LEGBA_VERIFY_LLM_JUDGE`; currently the same core model
that produced the finding, not cross-family — a deliberate, temporary choice
that shares the producer's blind spots; when the flag is off or the judge is
unreachable the result degrades to the floor and is labelled
`judge-unavailable`, never a fabricated number). The verdict persists as a
`critique`, and `effective_confidence = min(confidence, faithfulness_score)`
is folded at read time — gating a visible low-confidence tier, never a hard
delete. The composition INNER JOIN and the scorecard bands key on this fold.
Flow 12 steps 4–5; `ANALYSIS.md` §6.2.

### Grounding — the stale-cutoff corrector

The core analyst LLM's training cutoff predates the present, so left alone it
backfills current world facts (who holds office, which alliances hold) from a
stale prior — observed live when an assessor called the sitting US president
"former". Grounding reuses the substrate as its own knowledge store: temporal
facts and polarity-signed nexuses seeded from the curated `world_baseline` and
live `wikidata_leaders` adapters hold the temporally honest "who holds office
now", and a **GROUND phase** between PLAN and REASON prepends a dated
"AUTHORITATIVE CURRENT CONTEXT" preamble for `inline_target` analysts that
declare `grounding.enabled: true` (all four units do). The preamble is
restricted to `source_type IN ('seed','curated')`, so curated ground truth
outranks a machine-extracted live fact; grounding is off — byte-for-byte
unchanged — for any analyst that does not opt in. Flow 10; `ANALYSIS.md` §7.9.

### Scoreboards, the optimizer, and the measured forecast

The analysis leg grades itself, and a no-skill or insufficient-sample result
is published, not hidden:

- **Skill scoreboard (per unit).** Faithfulness plus correctness-vs-reference
  (`unit_correctness_scorer`, deterministic against operator gold labels —
  honest-null at 0 labels; the gold set is tiny today, n=1, reported
  insufficient-sample), the segregated calibration Brier, and the
  acute-forecast BSS — all surfaced on `GET /api/v1/v3/eval/calibration`, with
  per-analyst run timing on `GET /api/v1/v3/eval/analyst_runtime`. Flow 15.
- **GEPA self-optimizer (scoped, measured).** `unit_optimizer` over one
  measured unit (`leadership_transition`); every candidate carries a real
  before/after paired faithfulness delta, stays human-gated, and can never
  auto-promote on a degenerate, absent, or non-positive delta. The monolithic
  `country_optimizer` stays cadence-frozen. Flow 4.
- **Forecasting (a scoreboard, never a claim).** Returns only as the
  `acute_forecasts` Brier/BSS scoreboard (precise question + window +
  probability + auto-resolve); a degenerate probability vector abstains. It
  currently reports **no proven skill**, and that null is published. Flow 15.

Retirements ride with this: the monolithic `country_assessor` one-pager and
the forecast-as-claim predictors (`country_predictor`,
`india_energy_predictor`) are retired and stopped — their historical rows
remain in the DB, unread; nothing is deleted or de-registered. Details in the
roster notes below and `SEAMS.md`.

### Agency — the governed actuation surface

When an analyst needs to *act* on the world (enqueue a media job, emit to a
channel, discover a source, fetch a URL) rather than only write substrate, it
does so through the action-pack agency plane — never directly. Every action
passes a three-way gate (resolve ∩ allow ∩ applicability), a per-pack governor
against a global budget envelope, and lands a reversible, stamped provenance
row. Web egress is guarded, not trusted; writes are operator-gated by default.
This is the only place untrusted text can drive an effect.
`AGENCY_GATING_MODEL.md`; `ANALYSIS.md` §5.

### Provenance — the through-line

Every stage stamps lineage. A signal carries immutable acquisition provenance
(source, fetch time, pipeline); every analyst output stamps `derived_from`
with the substrate UUIDs it read, so `GET /api/v1/lineage/finding/{id}` walks
a world composition → its country reads → their units → each unit's cited
signals → each signal's acquisition record, with zero dangling links.
Orthogonally, every analyst run extends a per-analyst SHA-256 hash-chained
receipt chain in `analyst_traces`; each lineage node carries a `receipt_hash`
and a `chain_consistent` boolean, and the UI badge reads "chain-consistent
(single-node)" — an honest scope statement, not an external
cryptographic-signing claim (Ed25519 signing exists only on the descriptor
audit log, not on analyst outputs). Flow 2 step 12 and Flow 12 step 6;
`ANALYSIS.md` §6.1 / §7.7.

### The journal — the reflective layer over the pipeline

The `journal_assessor` is the one analyst pointed at the whole organism — its
own self, state, and flow — rather than one slice of it. It is deliberately
**off-chain**: the 11th `OutputKind` (`journal`) lands in the dedicated
`journal_entries` table with an always-empty `derived_from`, is absent from
the lineage catalog (a lineage walk can never surface a journal node), and
holds only read + propose packs, so it can never write a fact / finding /
nexus. The entry tier runs every 12h and the consolidation tier daily;
everything outward goes through the human-gated `journal_proposals` queue
(routing its reflections back through that queue is a future item, not yet
wired). Flow 11.

---

## The walkthroughs

Each flow below is a numbered step list with `file:line` citations:

| # | Flow | Tier / role | State |
|---|---|---|---|
| 1 | **Life of a signal** — pull → baseline enrichment → fan-out → target | ingestion (Tier-1 inline) | **LIVE** |
| 2 | **An analyst cadence cycle** — reminder → fan-out → slice → run_method → write → emit | the shared Tier-2 rail | **LIVE** |
| 3 | **A consult** — POST → actor ReAct loop → chat-SSE / deep-workflow | on-demand | **LIVE** |
| 4 | **The optimizer Dapr workflow** — schedule → stages → measured, gated promotion | measured self-improvement | **LIVE** (scoped to one unit) |
| 5 | **A deep consult** — POST → 202 task → Dapr workflow (plan→acquire→analyze→synthesize) | on-demand, deep | **LIVE** |
| 6 | **Fact extraction + supersession** — signal → fact_extractor → write_fact → fact_decay | extraction (Tier-1 inline) | **LIVE** |
| 7 | **Nexus reification** — proposed_edges → relationship_reifier 8B typing → write_nexus → refine | meta | **LIVE** |
| 8 | **ACH competing-hypotheses** — read set → matrix/diagnosticity → ±2 → write_hypothesis | meta | **LIVE** |
| 9 | **Seeding import** — fetch → map → resolve → write_fact/write_nexus → seed_batches | curated import | **LIVE** |
| 10 | **A grounded assessment** — cadence fire → GROUND phase → current-facts resolve → dated preamble → LLM | injection (rides the units) | **LIVE** |
| 11 | **A journal entry** — META cadence → in-actor PLAN→GATHER→NARRATE → OFF-chain journal write | introspective instrument (ON cadence: entry 12h + consolidator daily) | **LIVE** |
| 12 | **A bounded reasoning unit + the verify pass** — assemble cited slice → cite-synthesize → faithfulness VERIFY → effective_confidence fold | the product loop | **LIVE** |
| 13 | **Composition** — country_composition over the 4 verified units → world_assessor over the country reads | composition | **LIVE** |
| 14 | **The banded scorecard** — deterministic rules over already-verified claims → one honest row per active desk (g20/watch) | honest top | **LIVE** |
| 15 | **The skill scoreboard + measured forecast** — per-unit faithfulness/correctness, calibration Brier, acute-forecast BSS (honest-null / abstain) | measurement | **LIVE** (no proven skill yet) |

**Output kinds, the live roster, and retirements** (reference notes; every
claim below cites the producing `file:line`; the `OutputKind` enum has
**twelve** members —
`src/legba/data/provenance/kinds.py:77-109`):
- **`OutputKind.FACT` (`= "fact"`) and `OutputKind.NEXUS` (`= "nexus"`)** sit
  alongside the original findings/situations/hypotheses/predictions/alerts/
  meta-findings/critiques/prompt-module-candidates
  (`src/legba/data/provenance/kinds.py:77-92`), with real `write_fact`
  (`src/legba/data/provenance/writes.py:385`) and `write_nexus`
  (`writes.py:416`) routes into the dedicated `facts` / `nexuses` tables.
- **`OutputKind.SCORECARD` (`= "scorecard"`) is the 12th kind**
  (`src/legba/data/provenance/kinds.py:109`) — the deterministic banded
  per-desk verdict the `scorecard_producer` writes (Flow 14), one row per
  active desk (any target tagged g20/watch), every band naming the verified claim
  it rests on.
- **`OutputKind.JOURNAL` (`= "journal"`)** — the 11th kind
  (`src/legba/data/provenance/kinds.py:102`), Legba's first-person reflective
  voice (the `journal_assessor` META analyst — Flow 11). It is the deliberate
  EXCEPTION to everything below: it lands in the dedicated `journal_entries`
  table (migration 0048), **OFF the fact/finding/nexus chain** — a journal row
  is a *perspective OVER* the provenance chain, never a *member of* it. It
  carries an ALWAYS-EMPTY `derived_from` and is excluded from the lineage
  catalog, so a downstream lineage walk can never surface a journal node
  (`kinds.py:95-101`, `0048_journal.sql:17-21`). It must NEVER write a
  fact/finding/nexus.
  Migration 0032 added `valid_until` / `superseded_by` / `confidence_components`
  to `facts` + the open-only partial-unique index
  (`src/legba/data/migrations/0032_facts_decay_columns.sql:25-62`); migration 0033
  created the `nexuses` table (`0033_nexuses.sql:30-94`); migration 0034 added the
  `seed_batches` ledger + `seed_batch_id` FK on both tables (`0034_seed_batches.sql`).
- **The analysis-spine roster is registered by descriptor** in
  `scripts/bringup_register_analysts.py` (the live set; `country_assessor` and
  `country_predictor` are commented out — RETIRED, see below). The spine
  producers: the four bounded UNITS `leadership_transition` / `energy_security` /
  `escalation` / `narrative_coordination` (all `inline_target`, Flow 12), the
  per-country `country_composition` and global `world_assessor` (both
  `meta_findings_synthesizer`, Flow 13), the deterministic `scorecard_producer`
  (Flow 14), the measurement analysts `unit_correctness_scorer` /
  `calibration_tracking` / `forecast_scoreboard` (Flow 15), and the scoped
  `unit_optimizer` (Flow 4). Around them the substrate maintainers
  `relationship_reifier` / `competing_hypotheses` / `structural_balance` /
  `graph_mining` / `nexus_decay` / `fact_decay` / `fact_contention_arbiter` /
  `entity_gc` / `deep_consult` also produce rows on cadence.
  (`fact_contention_arbiter` is the detect-only contested-claims referee — a
  `deterministic`-kind META analyst, TRACE_ONLY, hourly at `:37`, default OFF behind
  `LEGBA_FACT_CONTENTION` — that surfaces a disputed fact's better-supported value in
  the `fact_contention` sidecar **without ever mutating a fact**; #101,
  ARCHITECTURE §5.9.)
- **Retirements / freezes (documented in SEAMS).** `country_assessor` (the
  monolithic per-country one-pager) is RETIRED and STOPPED — removed from bringup
  (commented out), because the units + composition supersede it and nothing in
  the spine reads it (SEAMS #35). Its **~1.2k historical findings REMAIN in the
  DB** (unread, not deleted — this is not a clean slate). The forecast-as-claim
  predictors `country_predictor` / `india_energy_predictor` are RETIRED/frozen and
  STOPPED (removed from bringup); their **~539 historical `prediction` rows
  REMAIN** in the DB. Forecasting returns ONLY as the measured `acute_forecasts`
  Brier/BSS scoreboard of Flow 15, never a free-text claim, and currently reports
  NO proven skill (SEAMS #31). The monolithic `country_optimizer` is
  **cadence-frozen** — its descriptor is still `state: active` and registered, but
  its cadence is null so it never fires; the measured `unit_optimizer` of Flow 4
  is its scoped return (SEAMS #30). The `journal_assessor` is **NOT frozen** — it
  runs ON cadence (entry tier every 12h, `journal_consolidator` daily) as a
  first-person introspective instrument writing ONLY `journal_entries` off the
  fact/finding/nexus chain (Flow 11); routing its reflections back outward via the
  human-gated proposal queue is a FUTURE item, not yet a live beat.

---

## 1. Life of a signal (LIVE)

**One sentence:** a `SourceCore` actor pulls raw entries from its source on a cadence,
runs the per-source baseline (structured enrichment → optional eager media → the
optional NLP enrichment filter chain) **once** over each one, writes the single
canonical signal, and publishes it to NATS where per-target durable consumers fan it
out to every subscribed target. "Enrich once, read many"
(`src/legba/data/sources/baseline.py:5-8`).

> **This whole flow is the TIER-1 INLINE analysis tier** (ARCHITECTURE §0 / §6.1) —
> the `data/filters/` baseline pipeline run *synchronously per-Signal at acquisition,
> BEFORE fan-out*, **deterministic, no analyst LLM** (GLiREL / DeBERTa-zero-shot /
> pycountry+Nominatim + local dedupe). Its substrate writes are altitude-0: the
> **enriched signal** (geo/language/tags/entity_classes in-place on the one `signals`
> row), altitude-0 **`facts`** (`source_type='ingestion'`, `valid_from`-stamped —
> Flow 6), and **entity rows + `signal_entity_links`** off the NER spans. The full
> inline stage chain is `language_detect → geocode → ner_multilingual → classify →
> source_credibility → ingest_dedupe (dedupe_4tier tiers 1-2) → fact_extractor`
> (`src/legba/data/filters/__init__.py`). The **TIER-2** slice/cadence analysts
> (`data/analysts/`, Flows 2 / 6-10) are a *separate* tier — they read accumulated
> slices/substrate on a reminder and *reason*, never per-signal.

1. **Poll fires.** `SourceCore.pull_once()` is the poll entry point. It reads the
   persisted cursor (`since`), builds the source handler, and iterates raw entries
   under a count+wall-time budget so the poll always finishes inside Dapr's drain
   window (`src/legba/runtime/source_actor.py:597`, budget gate at lines 647-649).

2. **Per-entry processing.** For each raw entry the actor calls
   `self._process_one(conn, ctx, raw)`
   (`src/legba/runtime/source_actor.py:658`; method def at `:482`).

3. **Baseline runs once, at the source.** `_process_one` invokes `run_baseline(signal,
   ctx, …, enrichment_stage=self.sd.enrichment_stage)`
   (`src/legba/runtime/source_actor.py:490-493`). `run_baseline` applies three tiers in
   order (`src/legba/data/sources/baseline.py:242`):
   - **Tier 1 — structured enrichment (always, cheap):** `_enrich_structured(signal,
     ctx)` populates the typed columns the subscription layer pushes down to SQL/NATS —
     `language` / `tags` / `geo` / `entity_classes` from scope hints + payload
     (`src/legba/data/sources/baseline.py:283`, impl at `:135`).
   - **Tier 2 — eager media (per-source flag):** only when
     `descriptor.pipeline.media == "eager"` and the signal has a `media_ref`, fetch +
     process via a registered `MediaExtractor`
     (`src/legba/data/sources/baseline.py:286-287`). **Seam:** no media-modality
     extractor ships in-tree; an eager media signal with no real registered extractor
     raises `MediaEndpointNotConfiguredError` — typed, loud, no row written (module
     docstring `baseline.py:30-41`; `default_extractor_registry` ships only the
     text/structured passthrough at `:102-116`).
   - **Tier 3 — NLP enrichment filter chain (optional, descriptor-ordered):** if
     `enrichment_stage` is wired, `await enrichment_stage(signal, ctx)` runs the
     `descriptor.pipeline.enrichment` chain in declared order — the live shape is
     `language_detect → geocode → ner_multilingual → classify → source_credibility →
     ingest_dedupe (dedupe_4tier, tiers 1-2) → fact_extractor` — and may **drop** the
     signal by returning `None` (`src/legba/data/sources/baseline.py:290-294`;
     factory + per-stage annotate-back at `src/legba/runtime/dapr_host.py:1499-1592`).
     This is where the INLINE tier's altitude-0 writes happen besides the enriched
     signal: `ner_multilingual` promotes entity spans (→ entity rows +
     `signal_entity_links`) and `fact_extractor` writes `facts`
     (`source_type='ingestion'`; Flow 6). The host wires this hook from the registry
     pipeline factory (`src/legba/runtime/source_first_runtime.py:181-203`); tests can
     omit it. Note `dedupe_4tier`'s expensive tiers 3-4 are the *semantic* Qdrant
     vector tier — tiers 1-2 (content-hash / structured) run inline here, the
     vector tier is the only piece that touches Qdrant.

   > The baseline **mutates the signal in place AND returns it** — the in-place
   > mutation keeps the handler-yielded object authoritative; the return lets a filter
   > replace or drop it (`src/legba/data/sources/baseline.py:275-278`).

4. **Canonical write.** A surviving signal is written canonically via
   `write_canonical_signal(...)`, which pins `owner_tenant` on the DB row and stamps the
   enrichment columns into the single `signals` table — there is no separate enrichment
   table (`src/legba/runtime/source_actor.py:520`; func at `:336`). Signals are
   **source-owned** and **target-agnostic** — they carry no `target_id`
   (`src/legba/runtime/dapr_actors.py:3029-3031`).

5. **Immediate publish.** Each written signal is published **as it is written**, not
   batched at the end, so fan-out survives a later cap/error/drain:
   `await self._publish([sig])` (`src/legba/runtime/source_actor.py:661-663`). The
   publish routes to a coarse NATS subject
   `legba.signals.{tenant}.{source_token}.{modality}.{event_class}`
   (`src/legba/data/nats.py:98-115`). Subject tokens cannot contain dots, so the source
   id is flattened by `subject_token()` (`src/legba/data/nats.py:86-95`).

6. **Cursor advance.** Whether the pull completed, capped, or errored, the cursor is
   always advanced for forward progress (advance policy at
   `src/legba/runtime/source_actor.py:669-714`). Content-hash dedup makes any window
   overlap a no-op.

7. **Fan-out to targets (subscription engine).** The shared `legba_signals` JetStream
   stream captures by subject filter; each target has **one aggregated durable pull
   consumer** bound to the union of coarse subject filters from its bindings
   (`src/legba/runtime/subscription/engine.py:117-186`; consumer provisioning at
   `src/legba/data/nats.py:244-313`). Matching is **two-stage**:
   1. **SQL WHERE on indexed columns** — `source_id` + `owner_tenant` pinned, then
      structured predicates on `geo` / `tags` / `entity_classes` (GIN) and
      `languages` / `modalities` (btree) (`src/legba/runtime/subscription/filter.py:62`).
   2. **Starlark residual** — `residual_matches()` compiles + evaluates the
      subscription's residual predicate on the narrowed set with a 5ms wall-clock budget
      (`src/legba/runtime/subscription/filter.py:194-219`;
      `src/legba/data/predicates/compiler.py:131-176`).

8. **Arrival at the target.** The signal is now in the per-target consumer's pull
   window. From here the analyst cadence cycle (Flow 2) reads it.

**Key shapes:** `Signal` (`src/legba/data/sources/_contract.py:136`),
`SourceDescriptor.pipeline` (`src/legba/data/schemas/source.py:119`),
`Subscription` (`src/legba/data/schemas/source.py:223`).

---

## 2. An analyst cadence cycle (LIVE)

**One sentence:** the **primary** `AnalystActor` owns one cadence reminder; on each tick
it matches active targets with a Starlark predicate, fans one run out **per matched
target** to per-worker actors (bounded at 5), and each worker reads its substrate slice,
invokes the kind's `run_method`, writes the typed output, extends the receipt chain,
emits output bindings, and may escalate.

> **Two fire paths into the same per-target run (`AnalystActor.run`).** An analyst run is
> reached by EITHER of two triggers, and both converge on the identical per-target work
> in §2c:
> 1. **Dapr-reminder cadence** (this flow, §2a/§2b) — the periodic floor: a quiet target
>    still gets re-evaluated on schedule.
> 2. **Reactive NATS coalescer** — a matched signal (or a new upstream `derived` finding)
>    marks the `(analyst, target)` pair *dirty* via the `TriggerEngine`'s durable pull
>    subscription over `legba_signals`, and the coalescer fires the analyst when a gate
>    trips (severity-wake / accumulation / cadence), CAS-claimed exactly-once so the
>    reminder tick and the reactive path never double-fire the same batch. LLM-bearing
>    kinds are floored to a batch ≥ 2 so a busy target can never fan out one LLM call per
>    signal (`src/legba/runtime/triggers/engine.py`, `coalescer.py`, `policy.py`).
>
> See `ANALYSIS.md` §2 for the coalescing-trigger decision kernel (gates + clamp + the
> exactly-once dispatch) in full; the steps below walk the reminder-cadence path.

### 2a. Cadence registration (once, at activation)

1. On `_on_activate`, the primary actor registers a single Dapr reminder named
   `run_cadence`, timed from `descriptor.cadence.fallback_schedule` via
   `cron_to_reminder_timing(schedule)`
   (`src/legba/runtime/dapr_actors.py:1234-1240`; helper imported at `:96`). Workers
   carry **no** reminder — the primary owns cadence.

### 2b. The tick

2. **Reminder fires.** `AnalystActor.receive_reminder("run_cadence", …)` runs the stale-
   fire guard `_reminder_guard` (self-disarms on version bump / pause), then resolves the
   matched targets (`src/legba/runtime/dapr_actors.py:1295`, guard at `:1307`).

3. **Target matching.** `_cadence_targets()` evaluates the
   `subscription.targets` Starlark predicate (ANALYST_SUBSCRIPTION surface, e.g.
   `has_tag('g20') or has_tag('watch')`) against the active target descriptors
   (`src/legba/runtime/dapr_actors.py:1320`, method at `:1464`). Three regimes:
   - **Target-bound analyst** → one run per matched target (`target_filter` set per
     target) (`:1342-1349`).
   - **Critic-kind meta analyst** → `_critic_ungraded_targets()` resolves the newest-N
     ungraded findings and fans one bounded worker grade per finding row (the
     `target_filter` is parsed by critic's READ_SLICE as the analyzed_output_id)
     (`:1330-1338`). This was the fix that un-stuck the critic→optimizer eval loop.
   - **Other meta analyst** (no target binding) → a single global run
     `await self.run({"trigger_kind": "cadence"})` (`:1339-1340`).

4. **Fan-out (A2 concurrency).** `_fanout_to_workers(targets)` chunks targets at
   `_FANOUT_CHUNK` (= 5, `src/legba/runtime/dapr_actors.py:548`) and dispatches each to a
   **distinct** worker actor id `analyst::<descriptor_id>::<target_id>` so each gets its
   own Dapr turn-queue and runs concurrently
   (`src/legba/runtime/dapr_actors.py:1351`, bounded-concurrent dispatch at `:1374`).
   The primary only orchestrates; it does not run the per-target work itself.

### 2c. The per-target run (`AnalystActor.run`)

`run()` is the main work entry (`src/legba/runtime/dapr_actors.py:1590`). For each worker:

5. **Lazy-activate.** A worker with a `target_filter` and no state record creates an
   ACTIVE record inline (`_minimal_worker_record`), with no reminder (primary owns
   cadence) (`:1617-1620`).

6. **Per-target cooldown + slack.** The run is gated by a per-target cooldown keyed by
   `target_filter`/`_global` in `cooldown_by_target`; a 5%-of-cooldown slack (capped
   600s) absorbs drift when `cooldown_seconds ≈ cadence interval` (fixes the 6h→12h
   silent halving) (`:1642-1670`; see commit `cefd8ca`).

7. **Read the substrate slice.** The kind's `read_slice` adapter runs (critic reads a
   specific `analyst_outputs` row by id; the default reads the recent **signal** window).
   The default reader `_read_substrate_slice` honors
   `subscription.targets.time_window` (e.g. `"336h"`), falling back to legacy flat attrs
   then **24h** (`src/legba/runtime/dapr_actors.py:2989`, window resolution
   `:3006-3027`). It narrows by the target's `source_id` refs and `geo` scope so each
   country target reads its own slice, not the global pool (`:3035-3059`).

8. **Budget precall.** `deps_bundle.budget.precall_check(conn, estimated_tokens)` projects
   the run against the daily cap → `throttle` / `exhausted` / `global_exhausted`; an
   exhausted outcome records a demotion audit and a strategy
   (`dlq` / `demote_and_continue` / `pause_until_next_window`)
   (`src/legba/runtime/dapr_actors.py:1721`). **Seam:** the
   `demote_and_continue` real cheap-model fallback is a declared seam (SEAMS F-2) — it
   logs a pause-until-reset instead of swapping in a real fallback model (`:1770-1794`).

9. **Invoke the kind's run_method.** `_invoke_run_method` dispatches the 3-arg form
   `run_method(inputs, options, kind_deps)` when `kind_deps` is present (else a 2-arg
   fallback) (`src/legba/runtime/dapr_actors.py:2570`). The kind, its LLM handler, and
   its deps bundle were resolved at deps-build time by `build_analyst_run_method`, which
   dispatches across the registered kinds (inline_target / cross_target_raw /
   meta_findings_synthesizer / cross_analyst_correlator / deterministic / predictor /
   critic / optimizer / consult_on_demand …)
   (`src/legba/runtime/analyst_deps_builder.py:99`). Transient failures retry with
   exponential backoff (max 3) via the exception classifier
   (`src/legba/runtime/dapr_actors.py` retry loop ~`:1911`, classifier `:2226`).
   - **Tier-1 knowledge grounding (`inline_target` only, opt-in).** When the analyst's
     descriptor declares `grounding.enabled: true`, the inline_target deps carry a
     `grounding_hook` installed at deps-build time (`_build_inline_target` →
     `_build_grounding_hook`, `src/legba/runtime/analyst_deps_builder.py:367`,`:378`).
     The kind's `run_method` then runs a **GROUND phase** between PLAN and REASON+ACT
     that prepends a dated "AUTHORITATIVE CURRENT CONTEXT" preamble to the LLM user
     prompt (`src/legba/data/analysts/inline_target.py:592-612`). **Flow 10 walks this
     in full.** Off (byte-for-byte unchanged) for every analyst that doesn't opt in.

10. **Select the output payload by kind.** `_select_output_payload(method_result,
    output_kind)` uses the per-`OutputKind` selector table (FINDING→`finding`,
    PREDICTION→`finding.data["prediction"]`, CRITIQUE→`finding.data["critique"]`, …)
    (`src/legba/runtime/dapr_actors.py:2665`, table near `:2657`).

11. **Write the typed output.** `write_analyst_output(conn, analyst_ctx=…, kind=…,
    output_payload=…, derived_from=…)` validates against the kind's pydantic model and
    routes the INSERT to the right table — `situations` / `hypotheses` /
    generic `analyst_outputs` (`src/legba/runtime/dapr_actors.py:1981-1989`; writer
    `src/legba/data/provenance/writes.py:115`, per-kind routing `:375`). A validation
    failure routes to `output_dead_letter` and the run reports HARD_FAIL (`:1990-1999`).

12. **Extend the receipt chain.** When a `receipt_chain` is wired, `record(...)` writes a
    chain-consistent (single-node) SHA-256 chain row into `analyst_traces` carrying
    `intermediate_steps` + `tool_calls` and the output-row id, producing
    `(receipt_hash, prev_receipt_hash)` (`src/legba/runtime/dapr_actors.py:2013-2034`;
    hashing `src/legba/data/provenance/_core.py:352-383`). This is a re-computable
    hash chain over analyst traces, NOT a cryptographic signature — read it as
    chain-consistent, not tamper-proof.

13. **NATS publish + emit bindings.** The write helper publishes the output envelope on
    `analyst.{analyst_id}.{channel}` (channel by kind: findings / situations /
    predictions / critiques / …, `src/legba/runtime/dapr_actors.py:2368-2379`). Then
    `_emit_output_bindings(...)` discovers the descriptor's output-kind handlers and
    dispatches `emit(payload, descriptor, deps, output_id, derived_from, target_id)`
    best-effort. The wired binding kinds are the **STIX 2.1 bundle** emitter
    (`src/legba/runtime/dapr_actors.py:2142`, func at `:2419`; bindings
    `src/legba/data/outputs/stix_bundle.py:112`; commits `cb621b8`/`a9744a0`), the
    **alert.emit** binding (`emit(...)` coerces a `FindingPayload` to an `AlertPayload`
    gated by `config.min_severity` / `config.min_confidence`, routes severity-aware
    surfaces, and writes per-attempt `alert_sink_deliveries` audit rows;
    `src/legba/data/outputs/alert.py:588-628`), and the **a2a_skill** binding that
    `world_assessor` declares (`analyst_world_assessor.yaml:125-128`). **Honest state:**
    the STIX and alert bindings' only in-tree binders were `country_assessor` (retired)
    and `country_predictor` (retired/frozen), so those two dispatch paths are **dormant
    today** —
    the code path is live and tested, but no *registered* analyst currently declares
    them; the one live output binding on a registered analyst is `world_assessor`'s
    `a2a_skill`.

14. **Optional escalation (A-3c).** For findings, `_maybe_escalate_finding(...)` gates on
    severity/confidence vs the pack gates, resolves `target.allowed_action_packs` +
    scope, and runs the escalation tool through the agency pipeline governance
    (`src/legba/runtime/dapr_actors.py:2163`, func at `:2485`).

15. **Outcome.** `run()` returns an `ActorRunOutcome`
    (SUCCESS / TRANSIENT_FAIL / BUDGET_THROTTLED / HARD_FAIL / NOOP).

**Producer note:** this ONE cadence + fan-out rail carries every LIVE analyst — the
four bounded `inline_target` UNITS and both `meta_findings_synthesizer` compositions
(target-bound → per-country fan-out; target-less → one global run), plus the
`deterministic` META producers (`scorecard_producer`, `unit_correctness_scorer`,
`calibration_tracking`, `forecast_scoreboard`, and the substrate maintainers) plus the
off-chain `journal_assessor` / `journal_consolidator` introspective instrument (Flow 11)
— all taking the single-global-run regime of §2b step 3. There is no per-analyst code; a
new analyst is a new DESCRIPTOR on this same rail (Flows 12–15 are all configurations of
it). The retired `country_assessor` / retired-frozen `country_predictor` used the
identical rail — retiring them is a descriptor state flip (removed from bringup), not a
code change.

---

## 3. A consult (LIVE)

**One sentence:** an operator question POSTed to the registry invokes the
`consult_default` actor through Dapr (180s blocking); the actor runs a bounded ReAct
loop over read-only substrate tools and — by **`mode`** — either returns a typed
`ConsultResponsePayload` IN the envelope with **NO row written** (`mode=chat`,
streamed step-by-step to the browser over SSE) or persists a `FINDING` the endpoint
reads back (`mode=deep`). For the **detached** deep-analysis job see Flow 5.

The request carries `mode: "chat" | "deep"` (default `chat`), an optional
client-minted `request_id` (for subscribe-before-POST SSE), and a client-held
`messages[]` history for multi-turn chat
(`src/legba/data/registry/consult_api.py:109`, invoke body at `:322-334`). The request's
`max_tool_rounds` defaults to 10 / ceiling 30 (`consult_api.py:107`); the handler's
`CHAT_DEFAULT_ROUNDS = 10` is the chat default (Piece 1, D1
`src/legba/data/analysts/consult_on_demand.py:117`).

1. **SPA POST.** `panels/system/Consult.tsx` POSTs `{question, scope_predicate,
   max_tool_rounds, mode, request_id, messages}` to `/api/v1/consult`
   (`legba-ui-v3/src/panels/system/Consult.tsx`). For `mode=chat` the SPA mints
   `request_id` and **subscribes to the SSE step relay first** —
   `GET /api/v1/consult/stream/{request_id}` (`src/legba/data/registry/consult_stream_api.py:78`)
   — then POSTs, so live ReAct steps published to the request-scoped core NATS subject
   `legba.consult.steps.{request_id}` are relayed as Server-Sent Events; steps
   published before attach are lost by design (best-effort live view, no replay)
   (`consult_stream_api.py:32-41`, subject at `:94`).

2. **Endpoint resolves the descriptor.** `invoke_consult` resolves the head version of
   the `consult_default` analyst via the descriptor registry's typed `get(...)`
   (`src/legba/data/registry/consult_api.py:210`, resolve at `:224-227`; router factory
   at `:196`; registered on the app at `src/legba/data/registry/server.py:249`).

3. **Build the actor id + invoke Dapr.** It builds the canonical actor id
   `analyst::consult_default::<version[:16]>` and PUTs to the Dapr sidecar
   `…/actors/AnalystActor/{actor_id}/method/run` with a 180s timeout
   (`src/legba/data/registry/consult_api.py:140-143`, invoke ~`:262-283`).

4. **Dispatch to the consult kind.** `AnalystActor.run` dispatches to
   `consult_on_demand.run_method(inputs, options, deps)`
   (`src/legba/runtime/dapr_actors.py:1980-1989`).

5. **ReAct loop.** The handler renders the prompt and loops `range(deps.max_rounds)`,
   where `deps.max_rounds` defaults to the constant **`MAX_TOOL_ROUNDS = 6`**
   (`src/legba/data/analysts/consult_on_demand.py:626` loop, `:111` constant, `:557`
   default): LLM call → parse JSON → if final, break; else dispatch a tool and append the
   result. After the cap, one forced final turn runs with tools unavailable.
   - **Multi-turn (chat).** The handler reads the client-held `messages[]` history off
     `inputs[0]` and seeds the conversation with it before the ReAct loop, so chat is
     stateless on the server but multi-turn on the client
     (`src/legba/data/analysts/consult_on_demand.py:668`, messages seed at `:681`).
   - **Step streaming (chat).** When a `request_id` + a NATS publisher are wired, every
     ReAct trace step is also pushed to `legba.consult.steps.{request_id}` so the live
     SSE stream and the durable trace are one source of truth
     (`consult_on_demand.py:581-583`).

6. **Tool dispatch.** Four whitelisted **read-only** tools route through
   `SubstrateQueryPort` — `search_signals`, `query_facts`, `inspect_entity`,
   `vector_search`; unknown tools error; exceptions fold back into the conversation
   (`src/legba/data/analysts/consult_on_demand.py:336-382`). Write-side tools are
   deliberately excluded by design.
   - **Now LIVE (Flow 6):** `query_facts` / `inspect_entity` read the `facts` table /
     entity graph — which is **no longer empty** now that the `fact_extractor` stage
     (Flow 6) and the seeding import (Flow 9) write real facts. The earlier "empty
     store" caveat is superseded.

7. **Build the response payload.** `ConsultResponsePayload` carries `answer` (≤65KB),
   deduplicated + hallucination-guarded `cited_substrate_refs`, `uncertainty` [0-1], and
   `unanswered_aspects` (`src/legba/data/analysts/consult_on_demand.py:390-461`).

8. **Terminate by `mode`.** The runtime branches in `AnalystActor.run` AFTER the budget
   `record(...)` + `derived_from` resolution (so chat still meters tokens + reports
   lineage), GUARDING the write path rather than forking it
   (`src/legba/runtime/dapr_actors.py:2086-2104`):
   - **`mode=chat`** → return the typed `ConsultResponsePayload` IN the envelope —
     **no row, no receipt chain, no output event, no emit bindings, no escalation** —
     and publish one terminal `{"type":"final"}` frame to
     `legba.consult.steps.{request_id}` so the SSE relay closes deterministically
     (`dapr_actors.py:2034-2053`, chat return at `:2097-2104`). The endpoint **skips the
     DB read-back** and projects the payload straight from the envelope with
     `finding_id=None` (`consult_api.py:423-434`).
   - **`mode=deep`** → `_wrap_as_finding(...)` nests the payload inside a `FindingPayload`
     (`src/legba/data/analysts/consult_on_demand.py:464-493`); the runtime writes it via
     `write_analyst_output(OutputKind.FINDING)` to `analyst_outputs`
     (`dapr_actors.py:2121-2129`). The endpoint reads the finding row back and projects
     it into `ConsultResponse`, **preferring `payload.answer` over `row.body`**
     (`consult_api.py:436-462`).

9. **Return + render.** The JSON `ConsultResponse` (answer, finding_id — `null` for chat,
   derived_from, tool_calls, cited_refs, uncertainty, unanswered_aspects) returns to the
   SPA, which renders the answer markdown, an uncertainty label, cited refs as clickable
   lineage links, and a collapsible tool trace.

**Current shape (kept honest):** chat is **server-stateless but client-multi-turn**
(the SPA holds `messages[]`) and **writes no finding by default**; the live ReAct steps
arrive over SSE while the authoritative trace is in the POST response. Deep mode persists
one finding row per call; the answer still lives in two places
(`FindingPayload.body` and `ConsultResponsePayload.answer`, both 65KB-capped) and the
endpoint prefers the payload.

---

## 4. The optimizer Dapr workflow (LIVE — scoped to ONE measured unit)

**One sentence:** the `optimizer` analyst kind (a meta-tier cadence run) schedules a
durable Dapr **Workflow** from inside its actor run; the workflow validates the training
set, then compiles a GEPA candidate prompt module (DSPy under a custom non-litellm LM
adapter), writes the candidate as a `PROMPT_MODULE_CANDIDATE` row carrying a **real
before/after faithfulness delta**, and an **operator-gated** promotion flips a candidate
into the analyst's live system prompt.

> **The LIVE registered optimizer is `unit_optimizer`, NOT the old monolith.** GEPA
> returned as a BOUNDED experiment over ONE bounded unit (`leadership_transition`), with
> `fitness_metric = faithfulness` measured by the SAME faithfulness judge
> (currently the core `llm.primary.openai_compat` model, not cross-family) that gates the live unit findings (Flow 12). A candidate carries a
> paired parent-vs-candidate faithfulness delta (live example: parent 0.34 → candidate
> 0.29, delta **−0.05**), stays `promotion_gate=human_gated`, and can **NEVER** auto-promote
> on a degenerate / insufficient-sample / judge-unavailable / non-positive delta — a
> negative delta like the one above is simply not promoted. Being a META analyst (no
> `subscription.targets`) it registers exactly ONE weekly `run_cadence` reminder
> (`analyst_unit_optimizer.yaml`, cadence `"0 4 * * 1"`), and it passes the training set
> BY REFERENCE (`TrainingSetRef`, SEAMS #23) so the serialized workflow input stays well
> under the 4 MB gRPC cap — the reminder-flood incident class (a >4 MB payload orphaning
> per-activity reminders) cannot regress here. The monolithic `country_optimizer` over the
> retired `country_assessor` stays **cadence-frozen** (descriptor still `state: active`
> and registered, but null cadence so it never fires; byte-unchanged; SEAMS #30).

This is the durability substrate that replaced Temporal — one Dapr control plane.
The optimizer kind calls through a stable `temporal_client` interface (the name is
historical; it just means "workflow client",
`src/legba/data/analysts/optimizer.py:303`). The workflow mechanics below are identical
for either optimizer descriptor — only the `unit_optimizer` one is registered/live.

1. **Cadence run.** The optimizer is a meta analyst (no target binding) — its cadence
   tick reaches `run()` as a single global run (Flow 2, step 3, third regime). Its
   `read_slice` fetches trace+critique rows joined via `trace_id`
   (`src/legba/data/analysts/optimizer.py:121-186`) — this is why the critic must run
   first to produce graded rows.

2. **Build the workflow input.** `run_method` constructs `OptimizerWorkflowInput`
   (analyst id/version, parent prompt-module path, training set, GEPA budget knobs,
   promotion policy, min-traces/min-critiques) and dispatches via `_dispatch_workflow`
   (`src/legba/data/analysts/optimizer.py:444-619`, input at `:491`, dispatch at `:520`).

3. **Schedule the workflow.** `_dispatch_workflow` calls
   `temporal_client.start_optimizer_workflow(input, workflow_id=…)` then awaits
   `handle.result()` (`src/legba/data/analysts/optimizer.py:741-763`). The Dapr client
   converts the input to a dict and calls `client.schedule_new_workflow(optimizer_workflow,
   …)` on a thread, returning a `DaprWorkflowHandle`
   (`src/legba/runtime/dapr_workflow/client.py:221`, schedule at `:239`).
   - **CRITICAL:** the Dapr instance id MUST NOT contain `::` (activity result parsing
     splits on `::` and would hang forever) — the id uses `optimizer.` instead
     (`src/legba/data/analysts/optimizer.py:508-519`).

4. **Orchestrator runs (stages).** `optimizer_workflow` is registered on the
   `WorkflowRuntime` **by function name** (the #37 fix) alongside its two activities
   (`src/legba/runtime/dapr_workflow/worker.py:100-102`; orchestrator
   `src/legba/runtime/dapr_workflow/workflow.py:134`). It is deterministic — no
   wall-clock / RNG / I/O in the body; all non-determinism is pushed into activities
   (`workflow.py:20-24`). Stages:
   1. `yield validate_training_set_activity(...)` — checks the set has enough
      traces/critiques; on `ok=False` the workflow stops
      (`src/legba/runtime/dapr_workflow/workflow.py:161`, activity at `:99`).
   2. `yield compile_candidate_activity(...)` **with a retry policy** — runs
      `asyncio.run(_run_gepa_loop(payload))`
      (`src/legba/runtime/dapr_workflow/workflow.py:112-126`, `:180`). Retry policy
      applies only to the activity, not the orchestrator (`:152-158`).

5. **The GEPA loop (shared core).** `_run_gepa_loop` loads the parent prompt, scores a
   baseline, tries the real DSPy/GEPA path, and falls back to a deterministic naive
   candidate search (`src/legba/runtime/dapr_workflow/gepa.py:254-314`). The same loop
   is used by the `InProcessWorkflowClient` fallback (`gepa.py:179-190`) so behavior is
   identical with or without a daprd sidecar.

6. **LLM routing — never litellm.** `_run_dspy_gepa_with_lm` resolves the LM via
   `configure_gepa_lm`, which builds a `LegbaProviderLM` (a custom `dspy.BaseLM` adapter)
   routing **all** LLM calls through Legba's own `LLMProviderHandler`, never litellm
   (`src/legba/runtime/dapr_workflow/gepa.py:389-442`;
   `src/legba/runtime/dapr_workflow/dspy_lm.py:192-238`). It scopes the LM via
   `dspy.context(lm=lm)` on a background loop (`_AsyncLoopBridge`) to avoid nested
   event-loop / cross-loop errors (this is the operator hard rule — dspy/litellm never
   in the analyst inference path).

7. **Write the candidate.** The workflow result rehydrates as `OptimizerWorkflowResult`
   (candidate text, training-set size, eval score + delta, generation, diagnostics)
   (`src/legba/runtime/dapr_workflow/gepa.py:100-119`). `run_method` writes it as a row
   with `kind = OutputKind.PROMPT_MODULE_CANDIDATE` into the generic `analyst_outputs`
   table (`src/legba/data/analysts/optimizer.py:532-555`,
   `OUTPUT_KIND = OutputKind.PROMPT_MODULE_CANDIDATE` at `:71`).

8. **Promotion (operator-gated).** A candidate becomes the analyst's live system prompt
   only when its `data->>'promotion_gate'` is flipped to `'promoted'` by the operator —
   there is **no auto-promotion path**:
   - the measurement gate `gepa._delta_gates_ok` stamps `data.eval.promotable` at
     candidate write time (`optimizer.run_method`); it rejects an absent / degenerate /
     non-finite / judge-unavailable / under-paired / sub-margin delta.
   - `resolve_promoted_system_prompt(analyst_id)` returns the live prompt by selecting
     the candidate whose `promotion_gate='promoted'` **and** `data.eval.promotable` is
     `true` (or a legacy candidate with no eval block) — this is the closed loop from
     champion instruction → live system prompt, so even a hand-flipped gate on a
     degenerate candidate resolves to the baseline
     (`src/legba/data/analysts/optimizer.py`).

**Bootstrap wiring:** the Dapr host builds the `DaprOptimizerWorkflowClient` and (when
`LEGBA_EMBED_WORKFLOW_WORKER=1`, the default) embeds the `WorkflowRuntime` worker
in-process (`src/legba/runtime/dapr_host.py:976-1010`). **TWO** workflows are now
registered by function name on the runtime — `optimizer_workflow` and
`deep_consult_workflow` + its four stage activities
(`src/legba/runtime/dapr_workflow/worker.py:107-117`); Flow 5 walks the second one.

---

## 5. A deep consult (LIVE)

**One sentence:** the UI's "Deep Consult" POSTs a question to a DETACHED endpoint that
HTTP-invokes the `deep_consult` analyst actor over the runtime's Dapr sidecar; the
actor's `run_method` SCHEDULES a durable `deep_consult_workflow` (plan → acquire →
analyze → synthesize) and returns a **task id in <1s** (202, NOT the 180s block of
Flow 3); the workflow's synthesize stage writes the finding (+ optional facts/
hypotheses), and a status poll reads it back. It reuses Flow 4's actor→workflow bridge,
pointed at analysis instead of optimization.

1. **Submit (202, detached).** `POST /api/v1/deep_consult` `{question, scope_predicate,
   emit_facts, emit_hypotheses}` resolves the `deep_consult` analyst head version,
   builds the actor id `analyst::deep_consult::<version[:16]>`, mints a `run_id`, and
   PUTs to the sidecar `…/actors/AnalystActor/{actor_id}/method/run` with a **30s**
   timeout (the actor returns immediately, so a short timeout suffices)
   (`src/legba/data/registry/deep_consult_api.py:132-179`). The success envelope must
   carry `task_id`; the endpoint returns `202` `{task_id, status, run_id}`
   (`deep_consult_api.py:246-260`).

2. **Actor short-circuit.** The runtime branches in `AnalystActor.run` on
   `descriptor.identity.kind == "deep_consult"`: it returns
   `{outcome:success, mode:deep_consult, task_id, status, run_id}` WITHOUT writing a row
   — same guard shape as the chat short-circuit (Flow 3 step 8)
   (`src/legba/runtime/dapr_actors.py:2063-2077`). The `deep_consult` kind's `run_method`
   builds the `DeepConsultWorkflowInput`, mints a `::`-free instance id
   `deep_consult.{scope}.{run8}` (the optimizer-hang GOTCHA: no `::` in the id), and
   schedules via the client WITHOUT awaiting `result()`, returning the task id
   (`src/legba/data/analysts/deep_consult.py:111-170`,
   `start_deep_consult_workflow` at `src/legba/runtime/dapr_workflow/deep_consult_client.py:111-125`).

3. **Workflow registered (by function name).** `deep_consult_workflow` + its four stage
   activities are registered on the `WorkflowRuntime` beside the optimizer's
   (`src/legba/runtime/dapr_workflow/worker.py:113-117`). The orchestrator body is the
   deterministic dict-chaining generator: `plan → acquire → analyze → synthesize`, each
   `yield ctx.call_activity(...)` with a retry policy; stage N's output dict is stage N+1's
   input (`src/legba/runtime/dapr_workflow/deep_consult_workflow.py:248-293`). A
   `plan.ok == False` short-circuits to an empty finding-id result (`:272-285`).

4. **Stages reuse the existing primitives (discipline §7 — refactor to share, never fork):**
   - **plan** — one LLM turn decomposes the question into a tool plan
     (`plan_activity` `deep_consult_workflow.py:190`; `_run_plan`).
   - **acquire** — runs the plan's tool calls against the **same read-only substrate port**
     as chat consult (`acquire_activity:203` / `_run_acquire`).
   - **analyze** — bounded synthesis over the evidence under the **same budget plane**
     (`analyze_activity:216` / `_run_analyze`).
   - **synthesize** — REUSES the provenance write paths VERBATIM: `write_finding`, plus
     (gated on `emit_facts` / `emit_hypotheses`) `write_fact` / `write_hypothesis`
     (`synthesize_activity:229` / `_run_synthesize`
     `src/legba/runtime/dapr_workflow/deep_consult.py:607-682`).

5. **Status poll.** `GET /api/v1/deep_consult/{task_id}` parses the trailing `run8` (first
   8 hex of the run_id) out of the instance id and SELECTs the produced `finding` row from
   `analyst_outputs WHERE kind='finding' AND analyst_id='deep_consult' AND
   replace(run_id::text,'-','') LIKE run8||'%'`: a row present → `completed` with
   `finding_id` + `answer` + `cited_refs` (the finding's `derived_from`); absent →
   `running` (`src/legba/data/registry/deep_consult_api.py:262-332`). The registry has no
   workflow-engine gRPC channel; the finding row is the authoritative completion signal.

6. **LLM adapter caveat.** If a stage drives DSPy it reuses the `LegbaProviderLM` +
   `_AsyncLoopBridge` + `dspy.context(lm=…)` scoping (never litellm), same as the
   optimizer (`src/legba/runtime/dapr_workflow/dspy_lm.py:192-238`).

---

## 6. Fact extraction + supersession (LIVE)

**One sentence:** the `fact_extractor` enrichment stage is the **TIER-1 INLINE** tier's
altitude-0 fact writer — it rides Flow 1 step 3 tier-3 (last in the inline chain, beside
the NER stage, deterministic / no analyst LLM), turns each in-flight `Signal` into atomic
`(subject, predicate, value)` facts stamped `source_type='ingestion'` + `valid_from`=event time, closes any
prior open fact whose value changed (supersession), and writes via the same open-only
upsert the analyst `write_fact` path uses — lighting up Consult (Flow 3) and the
`fact_decay` maintenance handler.

1. **Stage runs (enrichment-only, degrade-not-drop).** `FactExtractorHandler.transform`
   is a `descriptor.pipeline.enrichment` stage (wired by the registry pipeline factory in
   `dapr_host._source_enrichment_factory`, `src/legba/runtime/dapr_host.py:1429-1440`). It
   ALWAYS returns the signal unchanged and NEVER raises — on any failure it logs, flips
   health to degraded, and returns the signal
   (`src/legba/data/filters/fact_extractor.py:334-367`). The per-source `enrichment` gate
   IS the cost throttle — keep it OFF high-volume/low-value feeds (`:159-165`).

2. **Extract triples by backend.** `backend="relation"` (default, zero new infra) reuses
   the GLiREL triples already on `payload["entities"]` from the upstream
   `ner_multilingual` stage, reconstructing `(subject, predicate, object)` by pairing
   consecutive endpoints that share a predicate; when `entities` is absent it calls the
   hosted `POST /extract` itself (the same call NER makes)
   (`fact_extractor.py:380-416`). `backend="llm"` (opt-in, declared) routes the signal
   text through the analyst LLM plane via an injected `llm_handler_factory`; selecting it
   without one raises `FactExtractorUnconfigured` (no stub) (`:418-455`, guard `:271-276`).

3. **Filter the endpoints.** Both endpoints pass through the shared NER numbers/dates/units
   rejection (`_is_nonentity_candidate`); when a descriptor opts into
   `reject_quantity_endpoints` a triple whose subject OR value is ENTIRELY spelled-out
   numbers / ordinals / quantity-qualifiers ("sixth", "at least five") is dropped — the
   slice GLiREL's synthesised `confidence=1.0` can't be floored against; a single nominal
   token keeps the endpoint (`fact_extractor.py:104-116`, gate at `:487-498`).

4. **Resolve event-time.** `_event_time(signal)` reuses the source actor's exact cursor
   precedence — payload `_published_at_dt` / `_last_seen_dt` / `_event_dt`, else
   `signal.fetched_at` — always tz-aware UTC; a NULL `valid_from` fails loud rather than
   collapsing to the `1970` sentinel (`fact_extractor.py:137-151`, `:637-642`).

5. **Supersede prior, then upsert.** `_insert_ingestion_fact` runs `supersede_prior_facts`
   FIRST — closing any OPEN fact for `(lower(subject), lower(predicate))` whose VALUE
   DIFFERS (`valid_until=now()` + `superseded_by=<new id>`; a same-value re-assert closes
   nothing) — then INSERTs the new open fact with `source_type='ingestion'`, ON CONFLICT on
   `(lower(subject), lower(predicate), lower(value), COALESCE(valid_from,'1970…'))` WHERE
   open → lift `confidence` to the max + union `derived_from`
   (`fact_extractor.py:602-681`; `supersede_prior_facts`
   `src/legba/data/provenance/writes.py:658-714`). This is the SAME write contract the
   analyst-output `write_fact` → `_insert_fact` path uses, so the two producers agree
   (`writes.py:385-413`, `_insert_fact:717-819`). Optional AGE edges fire only when
   `emit_graph_edges` is set (ships false) (`fact_extractor.py:553-568`).

6. **Decay maintains.** The `fact_decay` deterministic handler now operates on real data —
   migration 0032 added the columns its UPDATEs reference. It (a) expires facts with a past
   `valid_until` (set `superseded_by`/close) and (b) decays stale-but-open confidence into
   `confidence_components.decay`, returning a FINDING receipt
   (`src/legba/data/analysts/deterministic_handlers/fact_decay.py:34-77`, `handle:110`).

7. **Consult lights up.** Consult's `query_facts` / `inspect_entity` tools (Flow 3 step 6)
   now read populated tables.

---

## 7. Nexus reification (LIVE)

**One sentence:** the `relationship_reifier` META analyst sweeps co-mentioned entity
pairs from `proposed_edges`, has an 8B LLM TYPE each as a canonical
`rel_type` + signed `polarity` + `intent` + `channel` (+ optional `intermediary`),
side-writes a first-class `nexus` row per typed pair via `write_nexus` (supersession on a
polarity/label change), and the dormant `structural_balance` / `graph_mining` /
`nexus_decay` handlers then refine over the now-SIGNED graph.

1. **Cadence run (one global META sweep).** The reifier is a META analyst (no target
   binding) → its cadence tick reaches `run()` as a single global run (Flow 2 step 3,
   third regime). `run_method` reads candidate pairs and side-writes nexus rows on its own
   connection (`src/legba/data/analysts/relationship_reifier.py:402-413`); its
   `OUTPUT_KIND` is FINDING (the per-run summary receipt — the nexus rows are side-written,
   exactly like `situation_clustering` side-writes situations) (`:72-77`).

2. **Read candidates.** `_read_candidates` pulls pending `proposed_edges` (the
   `entity_resolution` producer's `co_occurs` edges) with `confidence >=`
   `MIN_EDGE_CONFIDENCE` (0.45) that are NOT already reified into an OPEN nexus, ordered by
   confidence, capped at `MAX_CANDIDATES_PER_RUN` (40)
   (`relationship_reifier.py:317-339`, knobs `:91-98`). Each candidate is enriched with the
   pair's recent OPEN facts as typing context (`_recent_facts_for:342-359`).

3. **Type via the 8B LLM (never litellm; budget-gated).** Per candidate the run checks
   `deps.budget.check_envelope()` (stop issuing new calls once exhausted — degrade-not-drop)
   then one `chat_complete` typing call returns ONE JSON object
   `{related, subject, object, intermediary, rel_type, polarity, intent, channel,
   confidence}` (`relationship_reifier.py:150-199`, loop `:461-518`). `_coerce_typing`
   skips `related=false` / off-list `rel_type`, and `_canonical_polarity` takes the
   authoritative `POLARITY` table sign for the `rel_type` (the SAME table the
   `structural_balance` consumer owns — one canonical map) else the LLM's sign
   (`:242-309`, table import `:64`).

4. **Write the nexus (supersession on change).** `write_nexus` → `_insert_nexus` runs
   `supersede_prior_nexuses` FIRST — closing any OPEN nexus for the typed triple
   `(subject, COALESCE(intermediary,''), object, rel_type)` whose `polarity` OR `label`
   DIFFERS — then INSERTs open with ON CONFLICT on the `idx_nexuses_triple_open` partial
   index → lift confidence + union `derived_from`/`source_signal_ids` (a faithful copy of
   `_insert_fact`) (`src/legba/data/provenance/writes.py:416-442`, `_insert_nexus:888-997`,
   `supersede_prior_nexuses:822-885`). `valid_from` = the pair's `produced_at` (its
   co-mention event clock) (`relationship_reifier.py:526-530`).

5. **Refine over the signed graph (the PIECE A light-up).** The dormant deterministic
   handlers now read the OPEN signed nexuses DIRECTLY (their own pg_pool):
   - **structural_balance** — `_augment_from_nexuses` pulls non-neutral
     (`polarity <> 0`) open nexuses as canonical signed edges, enumerates triads, and
     classifies balanced / unbalanced (the signed-triad theory)
     (`src/legba/data/analysts/deterministic_handlers/structural_balance.py:275-302`,
     `handle:387`).
   - **graph_mining** — `_augment_from_nexuses` adds DIRECTED signed edges (subject →
     intermediary → object when a cut-out is set) so the proxy-chain **sign-product**
     mining sees hostile-via-proxy (negative product)
     (`graph_mining.py:322-347`, proxy chains `:171-220`, `handle:428`).
   - **nexus_decay** — the nexuses-table maintenance twin of `fact_decay`: decays stale
     open-nexus confidence, returning a FINDING (`nexus_decay.py:27-35`, `handle:65`).

---

## 8. ACH competing-hypotheses (LIVE)

**One sentence:** the `competing_hypotheses` (alias `ach`) META analyst re-homes the old
Legba ACH rigor — for each focal situation it reads the temporally-CURRENT evidence base,
has the LLM propose ≥2 MUTUALLY-EXCLUSIVE hypotheses each with a MANDATORY counter-thesis,
scores a reproducible evidence×hypothesis matrix with diagnosticity weighting, computes an
integer evidence balance, auto-transitions the lead/dominated hypotheses past ±2, and
side-writes one `HYPOTHESIS` row per hypothesis via the live `write_hypothesis` path. It is
NOT gated on `active` situations (the gate that starved the old `hypothesis_lifecycle`).

1. **Cadence run (one global META sweep).** Same META rail as Flow 7: `run_method`
   side-writes HYPOTHESIS rows + returns a FINDING summary
   (`src/legba/data/analysts/competing_hypotheses.py:717-728`, `OUTPUT_KIND=FINDING:88-94`).

2. **Read the focal topics + temporally-current evidence.** `_read_focal_topics` pulls
   recent `situations` by `intensity_score DESC` (last 14d, capped at `MAX_TOPICS_PER_RUN`
   = 12), NOT filtered on `status` (`:251-266`). `_read_evidence_for_topic` assembles up to
   `MAX_EVIDENCE_PER_TOPIC` (24) interchangeable items from three sources: linked
   **findings** (`analyst_outputs kind='finding'` via the situation's `derived_from`),
   current **facts** (`superseded_by IS NULL AND valid_until IS NULL` — the open-row query
   Piece B made meaningful), and open signed **nexuses** (Piece A) overlapping the topic's
   entities; each item's `id` is a real substrate UUID for lineage
   (`:269-371`).

3. **Generate the competing set (LLM enrichment + deterministic fallback).** Budget-gated
   `chat_complete` returns `{hypotheses:[{thesis, counter_thesis}, …]}`; `_coerce_hypotheses`
   enforces ≥`MIN_HYPOTHESES` (2) entries each with a non-empty thesis AND counter-thesis.
   Any LLM/parse failure or budget pause falls back to the deterministic escalate /
   de-escalate / status-quo triad so the matrix always gets built
   (`competing_hypotheses.py:175-196`, `_generate_hypotheses:458-510`,
   `_deterministic_hypotheses:379-401`).

4. **Score the matrix + diagnosticity (LLM-scored by default; lexical is the fallback).**
   Each `(evidence, hypothesis)` cell is scored on Heuer's CC/C/N/I/II scale (+2..−2) by
   the LLM — one batched call per topic through the analyst provider plane (never
   litellm/dspy), budget-gated via `check_envelope()`
   (`competing_hypotheses._score_consistency_matrix_llm`, called in `run_method`). The
   LLM cell scores OVERRIDE the deterministic scorer. Only when the budget envelope is
   exhausted (or the LLM is unavailable / unparsable) does the run fall back **per cell**
   to the transparent lexical/polarity scorer `_score_consistency` (escalation vs
   de-escalation keyword cues plus signed-nexus polarity) — so lexical is the
   budget-exhausted fallback, not the primary path. Each hypothesis row records which
   path ran under `diagnostic_evidence[].matrix_scorer` (`"llm"` or `"lexical"`).
   `_diagnosticity` then weighs each item by its SPREAD (max−min) across the hypotheses —
   evidence consistent with EVERY hypothesis weighs ~0 (the ACH core). The evidence base
   is scoped to the topic's **resolved-entity set** (`entity_profiles` canonical names,
   exact membership), not a `LIKE '%name%'` substring. See `ANALYSIS.md` §7.5 for the
   methodology framing.

5. **Integer evidence balance + ±2 auto-transitions.** The per-hypothesis balance sums the
   diagnosticity-weighted SIGN of each diagnostic cell, rounded to an integer (robust to
   confidence gaming). `_status_for`: the LEAD whose balance ≥ `CONFIRM_K` (2) →
   `confirmed`; any with balance ≤ −`REFUTE_K` (2) → `refuted`; else `active`
   (`:633-691`, `:694-709`, constants `:122-123`).

6. **Write one HYPOTHESIS row per hypothesis.** `write_hypothesis` writes `thesis` +
   `counter_thesis` (hot columns), `supporting_signals` / `refuting_signals` (the diagnostic
   evidence ids consistent / inconsistent with THIS hypothesis), `evidence_balance`,
   `status`, and the full ACH matrix/diagnosticity under the `diagnostic_evidence` jsonb
   column — REUSING the existing `hypotheses` table + `OutputKind.HYPOTHESIS`, no new write
   plumbing (`competing_hypotheses.py:845-904`; `write_hypothesis`
   `src/legba/data/provenance/writes.py:287-302`, `_insert_hypothesis:612-655`).

7. **Resolution + calibration loop (Brier).** `run_method` runs the **exogenous**
   resolver FIRST — `_resolve_hypotheses_against_subsequent_facts` grades each open
   hypothesis against facts produced AFTER it (net escalate/de-escalate direction of the
   subsequent facts vs the thesis direction), stamping `resolved_by='subsequent_facts'`.
   It now **ABSTAINS** on UNDIRECTED theses (status-quo / non-directional claims), which
   were auto-grading TRUE and inflating the headline rate (DQ-H2b); the operator-label
   path (`resolved_by='operator:<id>'`) outranks it. Hypotheses that reach a terminal
   `confirmed`/`refuted` status with no exogenous resolution fall back to
   `_resolve_hypotheses_by_status_transition` (`resolved_by='status_transition'`, a
   SELF-CONSISTENCY stamp). The `calibration_tracking` deterministic handler then reads
   `resolved_outcome` and **segregates the two**: `_is_exogenous` /
   `_SELF_CONSISTENCY_SOURCES` split the sample so it reports a `brier_exogenous` vs a
   `brier_self_consistency` and flags `insufficient_exogenous` when too few world-graded
   rows exist — never letting a self-consistency Brier masquerade as calibration against
   reality (`src/legba/data/analysts/deterministic_handlers/calibration_tracking.py`,
   `handle`). See `ANALYSIS.md` §7.4 for the methodology.

---

## 9. Seeding import (LIVE)

**One sentence:** `scripts/seed.py` runs a registered `SeedSource` adapter through
`SeedDriver.run_seed_source` — fetch → map → resolve every entity endpoint against
`entity_profiles` (reusing the `entity_resolution` ON CONFLICT upsert) → write each fact
via `write_fact` and each nexus via `write_nexus` stamped `source_type` + `seed_batch_id`
→ record the `seed_batches` ledger row; idempotent (re-import rides the open-only
temporal-triple uniqueness as an upsert no-op).

1. **CLI invoke.** `scripts/seed.py --source world_baseline` (or `--dry-run` for fetch+map
   only, `--list` for adapters) loads `PostgresConfig.from_env()`, opens an asyncpg pool,
   and calls `run_seed_source(pool, adapter)` (`scripts/seed.py:51-66`). This is **not a
   single-adapter path** — `--list` shows **four** registered live adapters:
   `world_baseline` (curated-YAML, walked below), `wikidata_leaders` (Wikidata SPARQL →
   `LeaderOf` facts), `acled_conflict` (ACLED conflict-events backfill → facts + signed
   nexuses), and `sipri_arms_transfers` (curated-SIPRI-YAML arms transfers → signed
   nexuses). Each rides the same `SeedDriver` fetch → map → resolve → write loop; only the
   `fetch`/`map` differ.

2. **Fetch → map.** `run_seed_source` calls `source.fetch(ctx)` then `source.map(raw)` →
   typed `SeedEntity` / `SeedFact` / `SeedNexus` payloads
   (`src/legba/data/seed/_driver.py:144-172`). On `dry_run` it reports the would-write
   counts and touches nothing (no batch row, no writes) (`:186-191`).

3. **Create the batch row first.** So the FK stamp on each fact/nexus is valid, the driver
   INSERTs the `seed_batches` row (source, kind, `source_type`, manifest) and keeps the id;
   counts are filled in at the end (`_driver.py:195-209`, `:309-314`). Migration 0034
   created `seed_batches` + the nullable indexed `seed_batch_id` FK on both `facts` and
   `nexuses` (`src/legba/data/migrations/0034_seed_batches.sql:30-85`).

4. **Resolve entities (dedupe-or-create).** `_resolve_entity` upserts each endpoint into
   `entity_profiles` with `ON CONFLICT (lower(canonical_name))` — the EXACT contract the
   `entity_resolution` sub-handler uses, so a seeded entity and a live mention of the same
   name fold to ONE row, never a duplicate (`_driver.py:95-136`, calls `:213-275`).

5. **Write facts + nexuses (stamped, idempotent).** Each `SeedFact` → `write_fact`
   (`FactPayload`) and each `SeedNexus` → `write_nexus` (`NexusPayload`), both passing
   `source_type=source.source_type` + `seed_batch_id=batch_id`. The provenance ctx is a
   synthetic `seed.<source>` analyst id (no target). `write_fact`/`write_nexus` honor
   `source_type` / `seed_batch_id` ONLY on the `facts` / `nexuses` insert routes
   (other kinds ignore them); a re-import lands on the open-only upsert and leaves the
   marker untouched — a per-record failure is logged + skipped (degrade-not-drop), never
   aborts the batch (`_driver.py:231-307`; `write_analyst_output` source-type plumbing
   `src/legba/data/provenance/writes.py:128-153`, `:450-518`).

6. **The `world_baseline` adapter.** The curated-YAML proof adapter (no network) reads
   `seeds/world_baseline.yaml` and maps: each leader → a `SeedFact`
   `(subject=leader, predicate='LeaderOf', value=country, valid_from=term start,
   confidence 0.95)` PLUS one country-subject office `SeedFact`
   `(subject=country, predicate='head of state', value=leader, valid_from=term start)`
   — the supersession-correct shape (keyed on the country, so a leader CHANGE closes the
   prior officeholder rather than leaving two open "current" rows,
   `src/legba/data/seed/adapters/world_baseline.py:110-128`); each alliance membership →
   a typed SIGNED `SeedNexus` `(subject=country, rel_type='MemberOf', object=bloc,
   polarity=+1, channel='institutional', valid_from=accession)` — written DIRECTLY, no
   LLM reifier (operator decision: relational seeds map to nexuses directly; the reifier
   of Flow 7 is for free-text) (`src/legba/data/seed/adapters/world_baseline.py:49-155`).

7. **The `wikidata_leaders` adapter (live SPARQL → current officeholders, the grounding
   feed).** The first *structured-external* adapter — it pulls the SAME knowledge shape
   from a live authoritative source, and is the curation half of the knowledge-grounding
   fix (Flow 10): its current-officeholder facts are what the GROUND phase injects.
   - **fetch.** Two guarded (SSRF-checked) SPARQL GETs against the public Wikidata Query
     Service: current heads of state/government per sovereign state with their term-start
     qualifier (the `FILTER NOT EXISTS { … P582 ?end }` open-tenure gate), and `member of`
     (P463) bloc memberships with accession dates
     (`src/legba/data/seed/adapters/wikidata_leaders.py:82-109`, `_query:209`). A
     fixture (`ctx.options['sparql_json']`) short-circuits the network for tests/dry-run
     mapping (`:185-198`).
   - **bare-QID label resolution.** The SPARQL label service sometimes returns a BARE QID
     instead of a name (live-observed: US `Q22686` has dozens of language labels but no
     `en` one). `_resolve_bare_qid_labels` gathers every bare-QID `*Label` cell, does ONE
     batched (chunked at 50) `wbgetentities` Action-API call, and rewrites the cell in
     place — preferring `labels.en.value`, FALLING BACK to the **enwiki sitelink title**
     (which IS "Donald Trump" for `Q22686`). A QID the API still can't resolve is left
     bare so `map` drops it — the adapter NEVER emits a `Qxxxx` value
     (`wikidata_leaders.py:241-368`).
   - **map (supersession-correct).** Each leader → a `LeaderOf` `SeedFact` (subject=leader)
     PLUS, per country, ONE country-subject office `SeedFact`
     `(subject=country, predicate='head of state', value=leader, valid_from=term start)`
     — preferring the executive (head_of_government) where both P6/P35 hold. This is the
     SAME canonical `head of state` predicate `world_baseline` uses, so a fresh Wikidata
     pull SUPERSEDES a stale curated leader for the same country via the Phase-B
     `valid_until` + `superseded_by` write path. A leader with no parseable term-start is
     SKIPPED (a fabricated `valid_from` would poison decay/supersession). Memberships →
     signed `+1 MemberOf` nexuses (`wikidata_leaders.py:374-516`). **Live-verified:** US
     head of state resolves to "Donald Trump" (since 2025-01-20), current, superseding the
     bare QID. Confidence 0.92 (slightly below curated 0.95: live extraction).

8. **Finalize.** The batch row's `counts` jsonb is UPDATEd with the run totals; the
   `SeedRunResult` (`counts` + `manifest` + `errors`) is returned and printed
   (`_driver.py:309-316`, result shape `:46-65`).

---

## 10. A grounded assessment (LIVE)

**One sentence:** a bounded UNIT run (any of the four `inline_target` units of Flow 12)
opted INTO grounding (`descriptor.grounding.enabled`) runs a **GROUND phase** before its
LLM call — a `SubstrateGroundingResolver` reads the CURRENT authoritative substrate facts
(head of state, bloc memberships) about the target geo + the slice's top entities,
`build_grounding_preamble` renders them into a dated "AUTHORITATIVE CURRENT CONTEXT"
block, and the runner PREPENDS it to the LLM user prompt — so a stale-cutoff model
reasons over current ground truth (e.g. Trump = the CURRENT US president since
2025-01-20) instead of its training prior (which called him "former"). The substrate
that Flows 6/7/9 fill (temporal facts + reified nexuses + seed roots, esp.
`wikidata_leaders`) IS the grounding store; this flow is the *injection* half. It is a
distinct thing from the mandatory faithfulness VERIFY of Flow 12 — grounding SUPERSEDES a
stale model prior on the way IN, verify checks the OUTPUT against its cites on the way OUT.

**Why it exists:** the analyst LLM's training cutoff predates the 2024 US election, so
left to its own prior it backfilled "former President Trump". The signal slice rarely
restates such background facts, so the model had no in-context correction
(`src/legba/runtime/grounding.py:3-25`). The fix curates current data IN (Flow 9
`wikidata_leaders`) and INJECTS it at analysis time (here).

1. **Opt-in at deps-build (once).** The descriptor's `grounding` block
   (`enabled` / `scope` / `sources` / `max_facts`, off by default —
   `src/legba/data/schemas/analyst.py:580-667`) gates a deps-builder step. Only when
   `grounding.enabled: true` AND a substrate `pg_pool` is wired does
   `_build_grounding_hook` construct a `SubstrateGroundingResolver` (closing over the
   pool) + a per-run `_hook` closure and install it on the `InlineTargetDeps.grounding_hook`
   field (`src/legba/runtime/analyst_deps_builder.py:367`, hook builder `:378-439`,
   `_build_inline_target` wiring `:368-374`). Off → `grounding_hook=None` → the run path
   is byte-for-byte unchanged. **All four bounded units opt in** (identical block, e.g.
   `analyst_leadership_transition.yaml:48-52`): `scope: [target_geo, slice_entities]`,
   `sources: [substrate, situations, graph_structure]`, `max_facts: 30`. (The block was
   ported verbatim from the retired `country_assessor` monolith when the units took over
   its per-country fan-out.)

2. **Cadence fire → ORIENT → PLAN.** The run reaches `inline_target.run_method` via
   Flow 2 (cadence reminder → fan-out → `_invoke_run_method`); ORIENT (`_orient`) PACKS
   the slice by admitting recency-ordered signals under the estimated INPUT-token budget
   (`LEGBA_LLM_INPUT_TOKEN_BUDGET`, default 32000) — NOT a fixed "newest 20" trim; the
   count cap `_MAX_INPUT_SIGNALS = 200` is only a hard backstop
   (`src/legba/data/analysts/inline_target.py:237-247`,`:294-346`) — then PLAN renders
   the base user prompt. The GROUND phase sits AFTER PLAN, BEFORE REASON+ACT.

3. **GROUND phase fires (only when the hook is wired).** `run_method` calls
   `await deps.grounding_hook(sliced, options)` inside a try/except — **degrade-not-drop**:
   any failure logs and leaves the prompt untouched (grounding is an enrichment, never
   fails the run) (`inline_target.py:592-612`).

4. **Collect candidates (deterministic, no DB).** `collect_grounding_candidates` reads
   ONLY the in-memory slice + the run's `target_id` and returns a de-duplicated,
   length-capped (≤24) list of names in priority order: `target_geo` first (the
   `country_<name>` target-id token + the most-frequent `geo` codes across the slice),
   then `slice_entities` (the NER/analyst `tags` + structured `key_entities`), with junk
   tags dropped (`src/legba/runtime/grounding.py:154-233`). Because every unit fans out
   per desk (`has_tag("g20") or has_tag("watch")`), each grounded run has a `target_id`,
   so it gets the desk's country itself first via `target_geo`, then its slice entities.

5. **Resolve CURRENT facts + signed nexuses.** `SubstrateGroundingResolver.resolve`
   queries the `facts` table for any candidate as the SUBJECT under the **current-facts
   gate** `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())` —
   the SAME temporal-honesty gate `substrate_query_port` uses — ordered
   `source_type IN ('seed','curated') DESC, confidence DESC, valid_from DESC` so seeded
   ground truth outranks a hallucinated live fact, capped at `max_facts`. It also
   excludes bare-QID values **in SQL** (`value !~ '^Q[0-9]+$'`) so the LIMIT budget is
   spent only on renderable facts, with a Python backstop. A small leftover budget folds
   in current signed nexuses (alliances/hostility) the same way (capped at 12)
   (`src/legba/runtime/grounding.py:275-383`). An empty candidate set short-circuits to
   `([], [])` (no query).

6. **Render the dated preamble.** `build_grounding_preamble` emits one block headed
   `AUTHORITATIVE CURRENT CONTEXT (as of <today> — treat as ground truth over any prior
   knowledge …)`, one line per fact (`<subject> — <predicate>: <value> (since <date>)`)
   then the signed relationships (`[supportive]` / `[antagonistic]`). Returns `None` when
   there is nothing current to inject (so no stray header is prepended). **Bare-QID
   values/edges are skipped at the resolver chokepoint** — an unreadable `Q22686` line is
   worse than no line, so the flow degrades to no-grounding for that fact rather than
   inject it (`src/legba/runtime/grounding.py:61-73`,`:333`,`:372`,`:391-423`).

7. **Prepend + reason.** A non-empty preamble is concatenated AHEAD of the rendered slice
   (`user_prompt = f"{preamble}\n{user_prompt}"`), a `ground` step is stamped into the
   trace (`inject_preamble` / `no_current_facts`), and REASON+ACT makes the single
   `chat_complete` call over the grounded prompt (`inline_target.py:604-626`). The rest of
   the run (REFLECT → NARRATE → PERSIST, Flow 2 steps 10-15) is unchanged.

**Canary (live-verified):** a US assessment's prompt context now contains ACCUMULATED
current facts + signed nexuses stamped with their `since` date, e.g. "United States —
head of government: Donald Trump (since 2025-01-20)", "United States — active conflict
with Iran (since 2026-02-28)", "United States — NATO member (since 1949)" — sourced from
the `wikidata_leaders` / seed facts + reified nexuses (Flows 7/9), current under the
supersession gate. So the run integrates over accumulated substrate, not just today's
72h signal slice.

**Honest caveats:**
- **Tier 2 is a declared FUTURE seam.** Only the structured-`substrate` source is wired.
  The schema accepts `vector:world_context` so a descriptor can pre-declare it, but the
  resolver acts ONLY on `substrate` today; a descriptor that declares ONLY a vector
  source resolves nothing and logs that it built no preamble
  (`src/legba/data/schemas/analyst.py:572` field; docstring `:555-560`;
  `src/legba/runtime/analyst_deps_builder.py:419-431`). The vector collection needs the
  embedder-through-port (L-114).
- **Grounding only fires for `inline_target` analysts.** The hook lives on
  `InlineTargetDeps`; other LLM kinds (the `meta_findings_synthesizer` compositions,
  consult, deep_consult) have no grounding wiring. The four bounded units ARE
  `kind: inline_target`, so they ground; `world_assessor` is now a
  `meta_findings_synthesizer` composition (Flow 13) and does NOT — it composes over
  already-grounded, already-verified country reads, so it inherits their ground truth
  transitively rather than re-injecting it.
- **It only corrects what the substrate actually holds.** A current fact absent from the
  seed/curated store can't be injected — grounding is as good as Flow 9's curation, and
  the resolver's exact-subject match means a name variant the slice uses but the facts
  table doesn't key on simply won't resolve (degrade to no-grounding, never a wrong fact).

---

## 11. A journal entry (LIVE — ON cadence: entry 12h + consolidator daily)

**One sentence:** the `journal_assessor` META analyst — Legba's FIRST-PERSON
reflective voice, the ONE analyst pointed at the whole organism (its own self /
state / flow) rather than one slice — runs as a single GLOBAL run ON cadence (the
entry tier every 12h, `journal_consolidator` daily), runs an in-actor staged
`PLAN → GATHER → field-notes → NARRATE` arc, and writes EXACTLY
ONE `JournalPayload` into the dedicated `journal_entries` table with an
ALWAYS-EMPTY `derived_from` — a perspective OVER the provenance chain, never a
member of it. It is an INTROSPECTIVE INSTRUMENT: it writes ONLY `journal_entries`
off the fact/finding/nexus chain, so it cannot pollute product output. Routing any
of its reflections back OUTWARD through the human-gated `journal_proposals` queue
is a FUTURE item (the queue + apply worker exist in code, but it is not yet a live
beat) — its only live effect today is its own next entry.

> **OFF the fact/finding/nexus chain (the single most important framing).** A
> journal row is the deliberate EXCEPTION to the lineage of Flows 6/7/9 (signals →
> entities/facts → relations/nexuses → situations → assessments): it is a
> *reflective layer ABOVE / ACROSS* that chain, not a node IN it. It carries an
> empty `derived_from` (`journal_assessor.py:709`; the write path
> `_insert_journal_entry` also HARD-FORCES the column empty) and the
> `journal_entries` table is deliberately ABSENT from the lineage catalog
> (`lineage_api._SUBSTRATE_TABLES`), so a `derived_from` walk FROM a
> fact/situation/nexus can NEVER surface a journal node
> (`0048_journal.sql:17-21`,`:58-60`). A gating test enforces that it never writes
> a fact/finding/nexus.

1. **Cadence run (one global META sweep).** No `subscription.targets` block →
   the `journal_assessor` kind is a META analyst, so its cadence tick reaches
   `run()` as a single global run (Flow 2 step 3, third regime, `target_filter=None`,
   like `world_assessor`). The kind is an EXTENSION analyst kind (registered via the
   vocabulary, NOT a member of the closed built-in `AnalystKind` enum); the deps
   builder dispatches it at `src/legba/runtime/analyst_deps_builder.py:278`
   (`_build_journal_assessor` `:435`). Two descriptors share this ONE kind:
   - **`journal_assessor`** — the ENTRY tier. It runs ON cadence every 12h
     (`cadence.fallback_schedule: "0 0,12 * * *"`, cooldown 42000s ≈ 11h40m, below
     the 12h interval per the §11 trap), narrate `max_tokens 16384`
     (`descriptors/analyst_journal_assessor.yaml:89-99`). The earlier entry-tier
     freeze is REVERSED — the 12h introspective beat fires again as a live cadence.
   - **`journal_consolidator`** — the CONSOLIDATION tier, SAME `identity.kind:
     journal_assessor`, distinct id, daily at 02:00 UTC (`"0 2 * * *"`, cooldown
     79200s), narrate `max_tokens 24576`. It DISTILLS its prior consolidation +
     recent entries into ONE forward-carried narrative (build-on-don't-repeat),
     emits `entry_kind='consolidation'`, and the write path fires
     `supersede_prior_consolidation` (close the prior open consolidation, open this
     one). **The tier IS the descriptor** (no mode flag): `run_method` selects
     `entry_kind` purely from `identity.id` (`journal_assessor.py:548`,
     `_entry_kind_for_analyst:90`; `descriptors/analyst_journal_consolidator.yaml`).

2. **In-actor staged arc (`method.kind: llm_planner`, NOT the deep_consult
   workflow).** `run_method` runs the one-soul staged envelope
   `PLAN → GATHER → field-notes → NARRATE → REFLECT → HONESTY` — the persona is
   loaded every phase (the worldview is the attention mechanism). It rides the
   in-actor agentic GATHER, NOT the deep_consult Dapr workflow (that path rides the
   broken long-activity round-trip, task #86, and hardcodes a FINDING)
   (`src/legba/data/analysts/journal_assessor.py:526-539`):
   - **PLAN/ORIENT** renders the base prompt; an opt-in **GROUND** phase (Flow 10
     machinery, `grounding.enabled: true`) may prepend a dated current-context
     preamble (`journal_assessor.py:568-591`).
   - **GATHER** is a deep ReAct loop over the `journal_read` pack (hard ceiling
     `gather.max_rounds = 6`), letting the agent investigate the whole animal +
     its own instruments (`:593-633`).
   - **FIELD-NOTES** is an in-voice cited handoff seam (not a thin summary), then
     **NARRATE** writes the entry with tools still live (a small ReAct loop, cap
     `_NARRATE_MAX_TOOL_ROUNDS = 2`) (`:635-652`).
   - **REFLECT** flags per-claim citations permissively (flag, don't strip);
     **HONESTY** forces the `honesty_flags` DETERMINISTICALLY from substrate
     metrics — never trusting the agent's self-report (`:655-666`).

3. **Per-phase LLM split.** The heavy GATHER investigation loop runs on the local
   gpt-oss / vLLM plane (`method.llm.primary → llm.primary.openai_compat`; a
   "Reasoning: high" content directive is injected into the gather system prompt
   only). The VOICE — the field-notes seam + the NARRATE synthesis — runs on the
   Anthropic plane, Opus 4.8 (`method.llm.narrate → llm.anthropic.opus_4_7`). So
   `max_tokens` governs ONLY the bounded Opus narrate output (it is never sent to
   the vLLM gather, which serves its own server budget) and the deep agentic loop
   is local. The deps builder reads the optional `method.llm.narrate.raw` and
   resolves a SECOND handler (`analyst_deps_builder.py:435-527`); analysts without
   `method.llm.narrate` fall back to the single primary handler, byte-unchanged.
   `budget_tokens_per_day` = 2,000,000.

4. **OFF-chain write (PERSIST).** `run_method` returns an `AnalystMethodResult`
   carrying the `JournalPayload` as `finding` and **`derived_from=[]`**
   (`journal_assessor.py:679-711`). The runtime forwards it to
   `write_analyst_output(kind=OutputKind.JOURNAL)`, which routes to the dedicated
   `journal_entries` table (`kinds.py:251-259`, table at
   `0048_journal.sql:33`) — NOT `analyst_outputs`. `entry_kind` ('entry' |
   'consolidation') is the row discriminator; the per-claim bindings live in the
   `claims` jsonb + the flat `cited_substrate_refs` UUID[] (the UP-only citation
   walk), the supersession columns (`valid_from` / `valid_until` / `superseded_by`)
   mirror facts/nexuses but apply to the consolidation tier only, and a
   partial-unique index enforces AT MOST ONE open consolidation
   (`0048_journal.sql:38-79`).

5. **Packs: read + propose-and-gate (the never-write-a-fact invariant).** The
   analyst is granted ONLY two packs (`action_packs` in both descriptors):
   `journal_read` (14 read tools incl. 9 self-instruments — `get_assessments` /
   `get_graph_structure` / `get_structural_balance` / `get_critic_scores` /
   `get_calibration` / `get_run_health` / `get_source_health` / `get_budget_status`
   / `get_journal_delta`) and `journal_propose`. BOTH are non-write-fact — the
   grant-layer backstop for the off-chain invariant. The journal writes ONLY its
   own entries + consolidations directly; EVERYTHING outward — a `correction`, a
   `change`, or a `self_revision` (INCLUDING changes to its own instructions via
   `propose_self_revision`; protected sections auto-reject) — goes to the
   HUMAN-GATED `journal_proposals` queue (`0048_journal.sql:92`), NEVER a live
   table. Its only un-gated effect is its OWN continuity (it reads its last entry +
   current consolidation into its next run).

6. **Accept → idempotent per-kind apply.** A human accepts/rejects via the review
   surface; on accept, an idempotent per-`proposal_kind` apply worker runs the
   change THROUGH the EXISTING write/lifecycle paths — `correction` → the
   supersession/lifecycle path, `change` → the registry's own update path,
   `self_revision` → the optimizer's champion-promotion path. The accept endpoint
   CAS-claims the proposal (`UPDATE … WHERE status='pending' RETURNING`) BEFORE
   apply, so a replayed accept never double-applies
   (`src/legba/data/registry/journal_proposals_apply.py:3-35`,`:113`). **Honest
   caveat:** the `change`-apply path is import-verified but NOT yet exercised
   against a live registry; the `correction` + `self_revision` apply paths ARE
   tested end-to-end.

7. **API + UI.** `GET /api/v1/journal` serves entries (the single open
   consolidation alongside; `journal_api.py:395`); `GET /api/v1/journal_proposals`
   + `POST …/{id}/accept` / `…/{id}/reject` drive the review surface
   (`journal_proposals_api.py:224`,`:258`,`:347`). The `system.journal` UI panel
   (`legba-ui-v3/src/panel-registry/registry.ts:356`) renders entries with
   provenance chips that deep-link to the cited record and `[needs_citation]` /
   perspective spans in a distinct style. **Honest caveat:** the panel was
   tsc-green + fully wired but pending its first real in-browser render at the time
   of writing.

**Status (kept honest):** LIVE and ON cadence (entry tier every 12h,
`journal_consolidator` daily) — deployed + live-validated (a real off-chain entry,
`honesty_flags` forced from substrate metrics, receipt-chained, in-voice). Prompts:
`legba.prompts.journal_assessor:JOURNAL_SYSTEM` (entry persona) +
`legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM` (consolidation persona).
**Future (designed-not-built):** routing the journal's reflections back outward via
the human-gated `journal_proposals` queue is not yet a live beat (steps 5–7 describe
built-but-unexercised code); and a critic + an optimizer OVER the journal's own
voice (Wave 5), gated on first building a critic actuator.

---

## 12. A bounded reasoning unit + the mandatory verify pass (LIVE)

**One sentence:** each of the four bounded UNITS — `leadership_transition`,
`energy_security`, `escalation`, `narrative_coordination` — is an `inline_target`
DESCRIPTOR (no new Python kind) scoped by a COVERAGE TAG to every desk via
`has_tag("g20") or has_tag("watch")`, answering ONE narrow question per run by
ASSEMBLING a cited 72h signal slice (+ the Flow 10 accumulated-facts grounding
preamble), cite-SYNTHESIZING a strict-JSON `FindingPayload` whose prose carries `[N]`
citation markers mapped to signal ids, then running the
**mandatory faithfulness VERIFY pass** and folding
`effective_confidence = min(confidence, faithfulness_score)` — the unit loop the
rest of the spine composes bottom-up.

> **A "desk" is a scoped subject-frame, not a surveilled entity.** A `target`
> descriptor is really a named SCOPE-FRAME that a set of analysts work — a desk — not
> a person or place under surveillance. The roster is now **24 desks**: the **19 G20
> country desks** (`has_tag("g20")`) PLUS a high-consequence **`watch` tier** of 5
> (`has_tag("watch")`) — Israel, Iran, Ukraine, Taiwan, North Korea (descriptor ids
> `country_watch_il` / `_ir` / `_ua` / `_tw` / `_kp`,
> `scripts/bringup_register_watch_country_targets.py`). Adding a country is
> register-a-target, no code: the four units + `country_composition` subscribe on
> `has_tag("g20") or has_tag("watch")`, and the scorecard enumerates any active target
> tagged g20/watch, so a new desk lights up the whole spine.

> **What faithfulness measures (and does not).** The verify pass scores whether each
> cited claim FOLLOWS FROM its cited evidence — *groundedness*, not *truth*. A claim
> can be faithful to a wrong source, or unfaithful to a right one; the number is about
> the citation bridge, not the world. Skill is reported PER UNIT, never as a platform
> boast (Flow 15).

1. **A unit is just a descriptor on the Flow 2 rail.** Each unit carries its OWN
   `method.system_prompt` (the bounded question, verbatim), `subscription.targets`
   predicate `has_tag("g20") or has_tag("watch")`, a 72h `time_window`, an
   `eval.rubric` (for the critic),
   and a `method.llm.verify` ref to the faithfulness judge — e.g.
   `analyst_leadership_transition.yaml`. There is NO per-unit Python kind:
   `identity.kind: inline_target` is a built-in. A register-time **unit drift guard**
   FAILS LOUD if a bounded unit is missing its `eval.rubric` or `method.llm.verify`
   (`scripts/bringup_register_analysts.py`, `UnitDriftError`) so a coverage gap can't
   degrade silently at first run.

2. **Cadences are staggered (budget discipline).** 4 units × 24 desks (19 G20 + 5
   watch) is a lot of LLM calls, so each unit fires 2×/day on a distinct hour pair —
   `leadership_transition` 01:00/13:00, `energy_security` 04:00/16:00, `escalation`
   07:00/19:00, `narrative_coordination` 10:00/22:00 UTC — with an 11h cooldown (39600s)
   and a per-unit `budget_tokens_per_day` cap (e.g. 300000). The four spread across the
   clock instead of stacking on one bucket.

3. **ASSEMBLE + GROUND + cite-SYNTHESIZE.** The run reaches `inline_target.run_method`
   via Flow 2, reads its per-country 72h signal slice, prepends the dated grounding
   preamble (Flow 10), and makes ONE `chat_complete` call on the **core analyst plane**
   (`llm.primary.openai_compat` → self-hosted gpt-oss-120B, $0). The strict-JSON
   `FindingPayload` carries `data['citations']` (the `[N]` → signal-id bridge) that both
   the drill-down (Flow 2 step 12 receipt chain) and the verify pass depend on.

4. **The MANDATORY faithfulness VERIFY pass.** After the finding lands, the runtime runs
   `verify_finding_faithfulness(...)` (`src/legba/data/provenance/verify.py:989`; fired
   at `src/legba/runtime/dapr_actors.py:2442`). It has two layers:
   - a **deterministic citation-presence FLOOR (always on)** — every fact-asserting
     claim in the prose is checked against the resolved `data['citations']` bridge; a
     claim with no `[N]` marker, or whose marker resolves to no real signal id, is an
     UNSUPPORTED span, and the score is the fraction of checkable claims that are
     supported (a planted fabrication with no citation is flagged unsupported);
   - an **optional LLM judge** (flag-gated by `LEGBA_VERIFY_LLM_JUDGE`, soft-fail) —
     **currently the SAME core reasoning model** (`llm.primary.openai_compat`,
     gpt-oss-120B) that wrote the finding, **NOT** cross-family — refines the per-claim
     verdicts. This is a deliberate, temporary choice: the earlier cross-family 8B judge
     ("legba-slm", `llm.verify.slm_8b`, Llama-3.1-8B) proved too weak (harsh + mis-aimed);
     a dedicated reasoning judge is planned. KNOWN LIMITATION: same-model judging shares
     blind spots, so the signal is weaker than an independent judge (the deterministic
     floor + signed provenance chain still backstop it). When the flag is off or the judge
     is unreachable the result degrades to the floor and is LABELLED `judge-unavailable` —
     it NEVER fabricates a number (`verify.py:319-320`).

5. **Persist as a critique + fold effective_confidence.** The verdict is written as a
   `critique` row (`overall_score = min(faithfulness_score, confidence_ceiling)`,
   `verify.py:382`). The existing finding↔critique gate then folds
   `effective_confidence = min(confidence, faithfulness_score)` at READ time
   (`substrate_query_port` / `substrate_reads_api`). This gates a visible low-confidence
   tier — a low-faithfulness finding is DEMOTED and surfaced as such, **never
   hard-deleted**. Everything downstream (composition, scorecard) reads only the folded,
   verified value.

6. **Drill-to-source.** The finding's receipt chain (Flow 2 step 12) plus its
   `data['citations']` resolve hop-by-hop back to the real signal URL:
   `GET /api/v1/lineage/finding/{id}` walks it (`src/legba/data/registry/lineage_api.py`),
   each node carrying a SHA-256 `receipt_hash` + a re-computed `chain_consistent` boolean
   (badge `"chain-consistent (single-node)"`, `lineage_api.py:110`) with zero dangling
   links. Note this is chain-CONSISTENCY over analyst traces, not a cryptographic
   signature — do not read it as tamper-proof.

---

## 13. Composition — per-country then world (LIVE)

**One sentence:** `country_composition` (kind `meta_findings_synthesizer`, per-country)
reads the FOUR verified units for a country and writes ONE hedged, cited synthesis over
**only the faithfulness-verify-PASSED sub-claims**; `world_assessor` (the SAME kind,
repointed from its old raw-signal role, global) then composes over the
`country_composition` reads into a cited, hedged world picture — a composition of
already-verified analysis, NOT a verdict from nowhere.

1. **Per-country composition.** `analyst_country_composition.yaml` carries a
   `subscription.targets: has_tag("g20") or has_tag("watch")` block, so `_cadence_targets`
   fans out ONE worker per desk (all 24 — 19 G20 + 5 watch;
   `target_filter=<country target id>`). Its `other_analysts` set is
   the four units; the kind's READ_SLICE resolves that set AND — because the run is
   target-scoped — restricts to the RUNNING desk's findings and admits ONLY
   verify-passed sub-claims above the floor (the INNER JOIN on the faithfulness
   critique). Unverified sub-claims never enter the composition. Cadence `"30 11,23"`,
   cooldown 39600s.

2. **Honest empty-slice path.** A country whose four units produced no verify-passed
   sub-claim yields an EMPTY slice, and the kind emits a `confidence=0.0` "No source
   findings to synthesize" finding rather than inventing a read
   (`analyst_country_composition.yaml:20-21`). `derived_from` is stamped from the
   contributing unit finding ids, so the per-country read back-walks one hop to the four
   units and two hops to their cited signals.

3. **World composition.** `analyst_world_assessor.yaml` declares NO `targets` block, so
   it runs as ONE global run (`target_filter=None`). The runtime selects the WORLD
   composition branch (READ_SLICE `include_meta=True` + verify-floor) so it reads the
   verified, meta-marked `country_composition` findings and composes them. Every factual
   clause is cited `[[ref:<uuid>]]` to a country-read `finding_id`. Cadence `"0 0,12"`,
   cooldown 39600s. It declares an `a2a_skill` output binding
   (`intelligence.world_assessment`).

4. **The compositions ALSO get the mandatory verify.** The faithfulness pass was
   generalized to recognize the composition's `[[ref:<uuid>]]` → sub-claim / country-read
   bridge; the fire condition is the descriptor DECLARING `method.llm.verify` (both
   compositions carry it; the old target-less `analyst_meta_synthesizer.yaml`, which has
   no verify block, stays excluded) — `dapr_actors.py:2455-2460`. So a composition's
   `effective_confidence = min(confidence, overall_score)` is folded the same way a
   unit's is, and a world read drills country → units → source with no dangling link.

> **`world_assessor` was NOT retired — it graduated.** Its old role as a first-order LLM
> pass over the tenant-wide raw signal slice was DEMOTED (SEAMS #34); it is now this
> composition. The retired thing is the monolithic per-country `country_assessor`, which
> nothing in the spine reads (Flow 2 note).

---

## 14. The banded scorecard (LIVE)

**One sentence:** `scorecard_producer` — a `deterministic`-kind META analyst
(`sub_handler=scorecard_producer`), the 12th OutputKind `scorecard` — writes ONE banded
row per active desk (any target tagged g20/watch — all 24 today) by running
high-precision RULES over the ALREADY-verified claims (no LLM, pure SQL, $0), so every
band is a legible, demote-never-promote function of a `severity:<level>` tag and the
folded `effective_confidence`, and every band NAMES the verified-claim id it rests on.

1. **One global sweep, deterministic.** No `subscription.targets` → one global run per
   tick that ENUMERATES every active target tagged g20/watch (all 24 desks), reading
   directly via `deps.pg_pool`
   (`analyst_scorecard_producer.yaml`, cadence `"40 4"`, cooldown 79200s). It runs
   `scorecard_banding.gather_and_band` over a 14-day verified-claim window
   (`DEFAULT_LOOKBACK_HOURS = 24*14`, `scorecard_banding.py:379`), staggered AFTER the
   units + composition + verify + calibration so fresh verified claims exist in-window.

2. **The banding rules (high-precision, demote-only).** For each of four fixed
   dimensions the engine bands from the finding's `severity:<level>` tag (mapped via
   `SEVERITY_TO_BAND` onto the ladder `low → watch → elevated → high → critical`) and
   its folded `effective_confidence = min(confidence, faithfulness_score)`. A claim below
   the confidence floor does NOT band; between the floor and a higher threshold the band
   is DEMOTED one rung; a per-claim faithfulness below a dedicated floor demotes to
   `low-faithfulness` (a distinct, legible reason). The rules never PROMOTE and never
   hand-weight a number into a fabricated overall band.

3. **Insufficient-evidence is a first-class outcome (honesty).** A dimension with no
   qualifying verified claim reads `band = "insufficient-evidence"` with an
   EMPTY-but-explicit basis and a machine `reason` (`below-floor` / `verify-failed` /
   no severity tag) — NEVER a fabricated band (`scorecard_banding.py:15-17`,
   `INSUFFICIENT` at `:113`). A country with NO qualifying claim still emits an
   all-insufficient row (never omitted), so the read route returns exactly one honest
   card per active desk (g20/watch). The row's `derived_from` NAMES the verified basis
   findings, and a lineage walk resolves them with zero dangling.

4. **Read it.** `GET /api/v1/v3/eval/scorecard` returns the per-country banded verdict
   (`src/legba/data/registry/v3_api.py:1073`).

> **The live scorecard is a MIX, and that is the point.** Some countries band; others
> read all-insufficient. For example the US currently reads all-insufficient because its
> unit faithfulness is genuinely low — the scorecard reports that honestly rather than
> inventing a confident band over ungrounded prose.

---

## 15. The skill scoreboard + measured forecast (LIVE — no proven skill yet)

**One sentence:** three deterministic META analysts publish the system's own honesty
metrics — `unit_correctness_scorer` (per-unit faithfulness + correctness-vs-reference),
`calibration_tracking` (the exogenous vs self-consistency Brier), and
`forecast_scoreboard` (the acute-forecast Brier/BSS pilot) — each reporting a
no-skill / insufficient-sample / abstain result HONESTLY rather than hiding it.

1. **Per-unit correctness (honest-null today).** `unit_correctness_scorer`
   (`sub_handler=unit_correctness_scorer`, cadence `"30 3"`) compares each unit's latest
   head finding against operator-authored gold rows in `unit_reference_labels`
   (migration 0057). The headline metric is Source-ID Overlap — canonical-source RECALL,
   deterministic + LLM-free + $0 (Jaccard + citations-only recall ride along as
   diagnostics). **The gold table is tiny (n=1, reported insufficient-sample);** with a
   unit's labels empty it reports `correctness_vs_reference = None` with a status string,
   never a fabricated or default number. It folds per-unit faithfulness alongside.

2. **Calibration (segregated Brier).** `calibration_tracking` reads resolved hypotheses
   and SEGREGATES the sample into a `brier_exogenous` (graded against facts that arrived
   AFTER the claim) vs a `brier_self_consistency` (a status-transition stamp), flagging
   `insufficient_exogenous` when too few world-graded rows exist — so a self-consistency
   Brier can never masquerade as calibration against reality (Flow 8 step 7).

3. **The acute-forecast scoreboard.** `forecast_scoreboard`
   (`sub_handler=forecast_scoreboard`, cadence `"50 2"`) is the weekly DRIVER of the
   pre-registered binary-forecast pilot. It calls the existing `forecast_acute` writers
   (it reimplements no math): `issue_weekly_forecasts` (one binary forecast per active
   desk for the next weekly window, idempotent), `resolve_open_acute_forecasts`
   (grades a closed window EXOGENOUSLY by upstream event time), and a read-only resolved
   count. A **degenerate / geography-dominated probability vector ABSTAINS** → zero rows;
   the producer never bypasses that guard.

4. **Forecasting surfaces as a MEASURED number, never a claim.** The only persisted
   product is `acute_forecasts` rows + the `analyst_traces` receipt; the returned
   `FindingPayload` is a per-run RECEIPT (counts only), marked TRACE_ONLY, so it NEVER
   lands a finding / prediction on any trust surface. The numbers surface ONLY on the
   calibration scoreboard `GET /api/v1/v3/eval/calibration`
   (`src/legba/data/registry/v3_api.py:1132`), fed by the segregated pilot Brier / BSS.
   **It currently reports NO proven skill** — the project earns the word "forecast" only
   when the BSS is positive on a non-degenerate, at-sample pilot, and not before. The
   forecast-as-claim predictors (`country_predictor`, `india_energy_predictor`) are
   RETIRED/frozen and STOPPED (removed from bringup) precisely so a NEW free-text forecast
   can never leak onto a trust surface (SEAMS #31); their **~539 historical `prediction`
   rows REMAIN** in the DB (unread, off every current read route — not deleted).

5. **Per-analyst runtime observability.** `GET /api/v1/v3/eval/analyst_runtime`
   (`src/legba/data/registry/v3_api.py:1267`) reports per-analyst RUN TIMING computed
   from `analyst_traces` — run count, avg/max wall-clock seconds, last run, and a
   non-success count — so a slow or silently-failing analyst is legible operationally
   (distinct from the skill/faithfulness metrics above).

---

## Appendix — primary entry-point index

| Concern | File:line |
|---|---|
| Source poll | `src/legba/runtime/source_actor.py:597` (`pull_once`) |
| Per-signal baseline | `src/legba/data/sources/baseline.py:242` (`run_baseline`) |
| Canonical signal write | `src/legba/runtime/source_actor.py:336` (`write_canonical_signal`) |
| NATS signal subject | `src/legba/data/nats.py:98` (`signal_subject`) |
| Subscription match | `src/legba/runtime/subscription/filter.py:62` |
| Cadence reminder register | `src/legba/runtime/dapr_actors.py:1234` |
| Cadence reminder fire | `src/legba/runtime/dapr_actors.py:1295` (`receive_reminder`) |
| Target matching | `src/legba/runtime/dapr_actors.py:1464` (`_cadence_targets`) |
| Per-target fan-out | `src/legba/runtime/dapr_actors.py:1351` (`_fanout_to_workers`) |
| Per-target run | `src/legba/runtime/dapr_actors.py:1590` (`run`) |
| Substrate slice read | `src/legba/runtime/dapr_actors.py:2989` (`_read_substrate_slice`) |
| Kind dispatch | `src/legba/runtime/analyst_deps_builder.py:99` (`build_analyst_run_method`) |
| Typed output write | `src/legba/data/provenance/writes.py:117` (`write_analyst_output`) |
| OutputKind enum (12) | `src/legba/data/provenance/kinds.py:77-109` (`FACT`:87 / `NEXUS`:92 / `JOURNAL`:102 / `SCORECARD`:109) |
| Emit bindings | `src/legba/runtime/dapr_actors.py:2419` (`_emit_output_bindings`) |
| STIX emit | `src/legba/data/outputs/stix_bundle.py:112` |
| alert.emit binding | `src/legba/data/outputs/alert.py:588` (`emit`) |
| Consult endpoint | `src/legba/data/registry/consult_api.py:281` (`/consult`, `mode`) |
| Consult SSE relay | `src/legba/data/registry/consult_stream_api.py:78` (`/consult/stream/{id}`) |
| Consult ReAct loop | `src/legba/data/analysts/consult_on_demand.py:626` |
| Deep-consult endpoint | `src/legba/data/registry/deep_consult_api.py:132` (`POST` → 202) |
| Deep-consult kind (schedule) | `src/legba/data/analysts/deep_consult.py:111` (`run_method`) |
| Deep-consult workflow | `src/legba/runtime/dapr_workflow/deep_consult_workflow.py:248` |
| Bounded units (descriptors) | `descriptors/analyst_leadership_transition.yaml` (+ `energy_security` / `escalation` / `narrative_coordination`) |
| Faithfulness verify pass | `src/legba/data/provenance/verify.py:989` (`verify_finding_faithfulness`); fired `src/legba/runtime/dapr_actors.py:2442` |
| effective_confidence fold | `overall_score = min(faithfulness_score, confidence)` (`verify.py:382`; read-time fold in `substrate_query_port`) |
| Per-country composition | `descriptors/analyst_country_composition.yaml` (`meta_findings_synthesizer`, per-target) |
| World composition | `descriptors/analyst_world_assessor.yaml` (`meta_findings_synthesizer`, global) |
| Banded scorecard | `src/legba/data/analysts/deterministic_handlers/scorecard_banding.py:379` (`gather_and_band`, 14d) ; read `v3_api.py:1073` |
| Per-unit correctness scorer | `src/legba/data/analysts/deterministic_handlers/unit_correctness_scorer.py` (`unit_reference_labels`, mig 0057) |
| Acute-forecast scoreboard | `descriptors/analyst_forecast_scoreboard.yaml` ; read `v3_api.py:1132` (`/eval/calibration`) |
| Per-analyst runtime timing | `src/legba/data/registry/v3_api.py:1267` (`GET /eval/analyst_runtime`) |
| Watch-tier desk targets | `scripts/bringup_register_watch_country_targets.py` (`country_watch_il/ir/ua/tw/kp`; tag `watch`) |
| Optimizer kind (scoped) | `src/legba/data/analysts/optimizer.py:444` (`run_method`); live descriptor `descriptors/analyst_unit_optimizer.yaml` |
| Workflow client | `src/legba/runtime/dapr_workflow/client.py:221` |
| Workflow orchestrator | `src/legba/runtime/dapr_workflow/workflow.py:134` |
| Workflow worker (2 workflows) | `src/legba/runtime/dapr_workflow/worker.py:58` (`build_workflow_runtime`) |
| GEPA loop | `src/legba/runtime/dapr_workflow/gepa.py:254` (`_run_gepa_loop`) |
| DSPy LM adapter (no litellm) | `src/legba/runtime/dapr_workflow/dspy_lm.py:147` |
| Promotion (operator-gated) | `src/legba/data/analysts/optimizer.py:326` / `:376` |
| Fact extractor stage | `src/legba/data/filters/fact_extractor.py:334` (`transform`) |
| write_fact / supersede | `src/legba/data/provenance/writes.py:385` / `:658` |
| Relationship reifier | `src/legba/data/analysts/relationship_reifier.py:402` (`run_method`) |
| write_nexus / supersede | `src/legba/data/provenance/writes.py:416` / `:822` |
| ACH competing-hypotheses | `src/legba/data/analysts/competing_hypotheses.py:717` (`run_method`) |
| Calibration (Brier) | `src/legba/data/analysts/deterministic_handlers/calibration_tracking.py:424` |
| Seed driver | `src/legba/data/seed/_driver.py:144` (`run_seed_source`) |
| Wikidata leaders adapter | `src/legba/data/seed/adapters/wikidata_leaders.py:147` (`fetch`/`map`) |
| Grounding block (schema) | `src/legba/data/schemas/analyst.py:580` (`GroundingBlock`) |
| Grounding hook builder | `src/legba/runtime/analyst_deps_builder.py:378` (`_build_grounding_hook`) |
| Grounding resolver | `src/legba/runtime/grounding.py:260` (`SubstrateGroundingResolver`) |
| Grounding preamble | `src/legba/runtime/grounding.py:399` (`build_grounding_preamble`) |
| GROUND phase (inject) | `src/legba/data/analysts/inline_target.py:592` |
| Facts table (0032 cols) | `src/legba/data/migrations/0001_baseline.sql:480` + `0032_facts_decay_columns.sql` |
| Nexuses table | `src/legba/data/migrations/0033_nexuses.sql:30` |
| Seed batches | `src/legba/data/migrations/0034_seed_batches.sql:30` |
| Journal kind run_method | `src/legba/data/analysts/journal_assessor.py:526` (`run_method`, `derived_from=[]`:709) |
| Journal entries / proposals tables | `src/legba/data/migrations/0048_journal.sql:33` / `:92` |
| Journal read endpoint | `src/legba/data/registry/journal_api.py:395` (`GET /api/v1/journal`) |
| Journal proposals + accept/reject | `src/legba/data/registry/journal_proposals_api.py:224` / `:258` / `:347` |
| Journal proposal apply worker | `src/legba/data/registry/journal_proposals_apply.py:113` (per-kind, idempotent) |
