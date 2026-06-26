# Data Flow — the whole river, end to end

> **Thesis.** Ingest once, enrich once, match many, reason on a slice, ground
> against curated truth, grade/calibrate/improve.

This is the conceptual through-line that links the deep docs. The mechanics live
elsewhere and are not repeated here — each section points at the canonical
treatment: `FLOWS.md` (Flow N, the life-of-a-X walkthroughs), `ANALYSIS.md`
(§N, the analysis plane), `ACQUISITION.md` (§N, how data enters), and
`AGENCY_GATING_MODEL.md` (how an analyst is allowed to act). When this doc and a
deep doc disagree, the deep doc wins — this one is the map, not the territory.

This is also the **only** doc that unifies the two fire paths that are otherwise
split across `FLOWS.md` (the Dapr-reminder **cadence** cycle) and `ANALYSIS.md`
(the reactive **trigger** plane). They are two doors into one room; §2 reconciles
them.

To ground the scale: the live instance has run **49 distinct sources** producing
**54,197 signals**, from which the analysis plane has produced **19,629 findings**,
**3,019 facts**, **3,822 nexuses**, **25 situations**, and **398 hypotheses**. The
three-feed set (BBC / Deutsche Welle / Al Jazeera) is the cold-start verification
minimum, **not** the deployed scope; a fresh instance reaches current scope by
running the full 46-source catalog
(`scripts/bringup_register_source_catalog.py`), not the 3-feed bootstrap.

---

## 1. The five stages as one river

Read left to right; every arrow is a substrate boundary, not a function call.

```
SOURCE ──► canonical Signal ──► predicate fan-out ──► the (analyst,target) fire ──► typed Output
          (Tier-1 inline       (coarse NATS subject    (cadence reminder OR        (11 OutputKinds →
           enrich, once)        + SQL WHERE +           reactive coalescer,         facts / nexuses /
                                Starlark residual)      one cooldown CAS)           situations /
                                                                                    hypotheses /
                                                                                    analyst_outputs;
                                                                                    journal off-chain)
```

**SOURCE → canonical Signal.** A `SourceCore` actor pulls raw entries on a cadence
and runs the per-source baseline **once** over each one, producing a single
canonical, target-agnostic `Signal` (no `target_id` — an observation, not an
interpretation). The baseline is the **Tier-1 inline** analysis tier: deterministic,
no analyst LLM, run synchronously per-Signal *before* fan-out. Its enrichment chain
(`language_detect → geocode → ner_multilingual → classify → source_credibility →
ingest_dedupe → fact_extractor`) writes altitude-0 product in place: the enriched
signal columns, ingestion `facts` (via the GLiREL relation backend — see
`AI_MODELS.md`), and entity rows + `signal_entity_links`. See `FLOWS.md` Flow 1 and
`ACQUISITION.md` §1–§3.

**Signal → predicate fan-out.** The written signal is published immediately to a
coarse NATS subject. Each target has one aggregated durable pull consumer bound to
the union of its subscription subjects; delivery is then narrowed **exactly** by a
two-stage match — an indexed SQL `WHERE` over `geo` / `tags` / `entity_classes` /
`languages` / `modalities`, then a Starlark **residual** predicate evaluated under a
5 ms wall-clock budget. Coarse subject narrows *delivery*; SQL + Starlark decide the
*match*. See `FLOWS.md` Flow 1 (steps 5–8) and `ACQUISITION.md` §4.

**Fan-out → the (analyst,target) fire.** This is where the dual path lives; §2
reconciles it. A matched signal marks an `(analyst, target)` pair dirty and the
analyst fires on a gate, not per-signal.

**The fire → typed Output.** The analyst reads its substrate slice, runs its method,
and writes one typed output validated against its kind's pydantic model. There are
**exactly eleven** `OutputKind` values — `finding · situation · hypothesis · prediction
· alert · meta_finding · critique · fact · nexus · prompt_module_candidate · journal`
(`data/provenance/kinds.py`) — routed by the universal write path to `situations` /
`hypotheses` / the knowledge-layer `facts` / `nexuses` tables, the dedicated
`journal_entries` table, or the generic `analyst_outputs` table. The 11th kind,
`journal`, sits **off-chain** — it is a perspective *over* the provenance chain, not a
lineage member (see §8). There is deliberately no `signal` kind. See `FLOWS.md`
Flow 2 (steps 10–13) and `ARCHITECTURE.md` §8.1.

---

## 2. The unified fire path — one room, two doors

`ANALYSIS.md` describes a reactive **trigger** plane; `FLOWS.md` describes a Dapr
**cadence** cycle. They are not two systems — they are two ways the *same*
`(analyst, target)` pair becomes eligible to fire, reconciled at the dispatch seam.

- **Cadence door (`FLOWS.md` Flow 2).** On activation the primary actor registers a
  single Dapr reminder (`run_cadence`). When it ticks, `_cadence_targets()` resolves
  the matched targets and fans them to distinct worker actors. This is the *floor* —
  a quiet target still gets re-evaluated on a schedule, so a slow drip eventually
  fires.

- **Reactive door (`ANALYSIS.md` §2).** A matched signal (or a new upstream finding —
  event-class `derived`, just another signal on the stream) marks the pair dirty via
  the coalescer (`runtime/triggers/`). It fires on whichever gate trips first —
  **severity** (a critical signal wakes it now), **accumulation** (N pending fire the
  batch together), or **cadence** (the periodic tick, only when something is pending)
  — clamped by a per-pair **cooldown** that is the thrash ceiling.

The two doors converge on one lock. Both the signal path and the cadence tick can
independently *decide* to fire the same pair; the coalescer **CAS-claims** the fire
on the accumulator's last-fired anchor (`claim_fire`), so exactly one worker wins
and the loser backs off (`ANALYSIS.md` §2.3). The worker run then re-checks a
**per-target cooldown** keyed by `target_filter` before doing work, with a slack
band that absorbs drift when `cooldown ≈ cadence` (`FLOWS.md` Flow 2, step 6). Net:
*coalesce + CAS dedup* means a pair fires once per cooldown window no matter how many
doors opened. LLM-bearing analysts are floored to a minimum batch and never fire
per-signal (`ANALYSIS.md` §2.4).

---

## 3. The two tiers — the cost-decoupling spine

The river runs at two altitudes, and keeping them separate is the whole point: cheap
deterministic work happens once per observation; expensive reasoning happens once per
*slice*, not once per signal.

- **Tier 1 — inline, deterministic, per-Signal, pre-fan-out.** The `data/filters/`
  baseline chain (§1). spaCy NER, geo resolution, GLiREL relation facts, dedup. No
  analyst LLM. Writes altitude-0 product (enriched signal, ingestion facts, entity
  links). This is "enrich once" — every downstream target reads the same enriched
  row. See `ARCHITECTURE.md` §0/§6.1 and `ACQUISITION.md` §3.

- **Tier 2 — slice / cadence, reasoning, per-(analyst,target).** The
  `data/analysts/` kinds read an accumulated substrate **slice** on a fire and
  *reason* over it — never per-signal. This is where LLM cost lands, and the
  coalescer (§2) is precisely the device that decouples that cost from signal
  arrival rate. See `ANALYSIS.md` §3 and `FLOWS.md` Flow 2.

The coalescer is the membrane between the tiers: Tier 1 produces a stream of cheap
enriched signals; Tier 2 consumes *batches* of them on a clamped schedule.

---

## 4. The analysis-leg loop — judging and improving its own work

Producing a typed output is not the end of the river; the analysis leg closes back
on itself. Two families of side-write ride the *same* cadence + fan-out rail as the
producing analysts, with no special dispatch path.

- **The eval loop: analyst → critic → optimizer → calibration.** A `critic` analyst
  grades outputs against the descriptor's `eval.rubric` and **actuates** — the
  surfaced confidence of a graded finding is `effective_confidence = min(confidence,
  critic_score)`, stored side-by-side with the raw confidence so the down-weighting
  is auditable, not destructive (`ANALYSIS.md` §6.2). The `optimizer` reads
  accumulated traces + critiques and runs a DSPy + **GEPA** reflective
  prompt-evolution loop, emitting a `prompt_module_candidate`; promotion to live is
  human-gated by default (`ANALYSIS.md` §6.3–§6.4). GEPA is a multi-hour outer loop,
  so it is the one analyst that runs as a **Dapr Workflow** rather than a single-shot
  actor — and degrades to an in-process loop where the durable-task engine round-trip
  is unavailable (declared SEAM #23; see `SEAMS.md`). Calibration scores forecast
  outputs against realized outcomes (`ANALYSIS.md` §7.4).

- **The structural side-write rails.** `relationship_reifier` types co-mention pairs
  into signed `nexus` rows (`OutputKind.NEXUS`); `competing_hypotheses` (ACH) scores
  an evidence × diagnosticity matrix — LLM-scored by default, with the lexical scorer
  as the budget-exhausted fallback — into `hypothesis` rows; situation clustering
  promotes recurrent signal/finding clusters into first-class `situation` objects.
  Each is a normal Tier-2 analyst writing a typed `OutputKind`. See `ANALYSIS.md`
  §3, §7.3, §7.5.

---

## 5. Grounding — the stale-cutoff corrector

The core analyst LLM has a training cutoff that predates the present, so left alone
it backfills *current* world facts (who holds office, which alliances hold, the state
of an ongoing conflict) from a stale prior — observed live, an assessor called the
sitting US president a "former" president. The signal slice rarely restates such
background, so the model has nothing in-context to correct it.

Grounding fixes this by **reusing the substrate as its own knowledge store** rather
than adding one. The temporal facts (`valid_from` / `valid_until` / `superseded_by`)
and signed nexuses, seeded from the curated `world_baseline` adapter and the live
`wikidata_leaders` adapter, hold the temporally-honest "who holds office now". An
opt-in **GROUND phase** runs between PLAN and REASON for `inline_target` analysts that
declare `grounding.enabled: true`, prepending a dated "AUTHORITATIVE CURRENT CONTEXT"
preamble — restricted to `source_type IN ('seed','curated')`. Machine-extracted
ingestion facts are floored at a conservative `_INGESTION_DEFAULT_CONFIDENCE` (~0.5,
below the 0.95 curated seed), so the gate still prefers seed/curated ground truth
over a hallucinated live fact rather than admitting a whitelist of dirty facts. Off
(byte-for-byte unchanged) for any analyst that does not opt in. See `FLOWS.md` Flow
10 and `ANALYSIS.md` §7.9.

---

## 6. Agency — the governed actuation surface

When an analyst needs to *act* on the world (enqueue a media job, emit to a channel,
discover a source, fetch a URL) rather than only write substrate, it does so through
the **action-pack agency** plane — never directly. Every action passes a three-way
gate (resolve ∩ allow ∩ applicability), a per-pack governor against a global budget
envelope, and lands a reversible, stamped provenance row. Web egress is guarded, not
trusted; writes are operator-gated by default. The live GATHER actuation seam is
closed. This is the only place untrusted text can drive an effect, and it is treated
as such. See `AGENCY_GATING_MODEL.md` (all sections) and `ANALYSIS.md` §5.

---

## 7. Provenance — the through-line

Every stage above stamps lineage, and that is what makes the river auditable end to
end. A signal carries immutable acquisition provenance (source, fetch time,
pipeline). Every analyst output stamps `derived_from` with the substrate UUIDs it
read, so the lineage walker can backtrack a meta-finding → its first-order findings →
their signals → each signal's acquisition record. The one exception is the `journal`
kind (§8): it carries an **always-empty** `derived_from` and is deliberately absent
from the lineage catalog, so a `derived_from` walk can never surface a journal node —
it is a perspective *over* the chain, not a node in it. Orthogonally, every analyst run
extends a per-analyst, tamper-evident **SHA-256 receipt chain** in `analyst_traces`
(`receipt_hash`, `prev_receipt_hash`), following the analyst identity across
descriptor versions. Together, `derived_from` (what it read) and the receipt chain
(how it ran) let the system answer "why do we believe this?" at every altitude —
the foundation of analytical accountability. See `ANALYSIS.md` §6.1 and §7.7.

---

## 8. The journal — the reflective layer *over* the river

Every stage above is part of the chain. The `journal_assessor` is not: it is Legba's
first-person reflective voice, the one analyst pointed at the **whole organism** —
its own self, state, and flow — rather than at one slice of it. Where every other
meta-analyst cuts a single slice (a country, the graph, the run health), the journal
cuts *across* the entire flow and narrates a coherent point of view over it. Its
thesis: *"Poetry without evidence is noise. Evidence without perspective is just a
log file."*

It is the 11th `OutputKind` (`journal`) and it is deliberately **off-chain**. A
journal row is a *perspective over* the provenance chain, never a *member of* it:

- It lands in the dedicated `journal_entries` table (migration 0048), **not**
  `analyst_outputs` — and never the knowledge-layer `facts` / `nexuses` tables. It
  must never write a fact / finding / nexus (a grant-layer backstop — it holds only
  the read-only `journal_read` pack and the propose-only `journal_propose` pack — and
  a gating test enforce this).
- Its `derived_from` is **always empty**, and the table is deliberately absent from
  the lineage catalog (`lineage_api._SUBSTRATE_TABLES`). A `derived_from` walk *from*
  a fact / situation / nexus can therefore never surface a journal node (§7). Citations
  live only in the row's `claims` / `cited_substrate_refs` (an up-only reference, not
  a lineage edge), which the UI renders as provenance chips that deep-link to the cited
  record. So the journal is **not** a stage in the
  `signals → entities/facts → relations/nexuses → situations → assessments` lineage —
  it is the reflective layer *above and across* it.

It is a single **META** analyst kind, run globally once per cadence tick
(`target_filter=None`, like `world_assessor`), under the in-actor `llm_planner`
GATHER envelope (PLAN → GATHER → NARRATE) — **not** the `deep_consult` Dapr workflow.
The heavy GATHER investigation loop runs on the core OpenAI-compatible plane
(`llm.primary.openai_compat`, the local gpt-oss / vLLM plane); only the bounded final
**voice** synthesis runs on the Anthropic plane (Opus 4.8, `llm.anthropic.opus_4_7`).
Two descriptors share the one kind: an **entry** tier (`journal_assessor`, every 12h)
and a **consolidation** tier (`journal_consolidator`, daily at 02:00 UTC) that distils
its prior consolidation plus recent entries into one forward-carried narrative and
supersedes the prior open consolidation. It is an **extension** analyst kind
(registered via the vocabulary family, not a member of the closed built-in
`AnalystKind` enum), so the count of built-in kinds is unchanged.

Its only un-gated effect is its own continuity (each run reads its own last entry and
current consolidation into the next). Everything outward — a correction, a change, or
a self-revision, including a change to its own instructions — is **proposed** into the
human-gated `journal_proposals` queue, never written to a live table; an operator
accepts or rejects. It can write its own next breath, but it cannot rewrite its own
rules without the operator. See `ANALYSIS.md` (the journal kind) and
`ARCHITECTURE.md` §8.1.

---

## See also

- `FLOWS.md` — the executable life-of-a-X walkthroughs (Flow 1 signal, Flow 2
  cadence, Flow 6 facts, Flow 7 nexus, Flow 8 ACH, Flow 10 grounded assessment).
- `ANALYSIS.md` — the analysis plane in depth (triggers §2, kinds §3, agency §5,
  eval loop §6, methodology §7).
- `ACQUISITION.md` — how data enters and reaches analysis (§1 SourceActor, §3
  baseline, §4 fan-out).
- `AGENCY_GATING_MODEL.md` — how an analyst is allowed to act.
- `ARCHITECTURE.md` §8.1 — the real `OutputKind` enum members.
