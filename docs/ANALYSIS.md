<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Analysis

For someone configuring or extending the analysis layer: how a matched signal becomes an analyst
run, what each analyst reads and writes, how desk reads are composed, and how claims are checked and
graded. Each mechanism lists what it reads, what it writes, its switch and its cadence (the
descriptor's `cadence.fallback_schedule`). Values are in [TUNABLES.md](TUNABLES.md); terms such as
desk, unit, record, voice and frame are in [GLOSSARY.md](GLOSSARY.md).

A descriptor's `identity.state` decides whether it runs. Several analysts below ship `draft` and run
only where a profile's lifecycle overlay (`descriptors/LIFECYCLE.d/`) activates them; the critic ships
`retired` and the optimizers `paused`. [RELEASE_STATE.md](RELEASE_STATE.md) lists each one's state.

---

## 1. Where analysis sits

A source publishes one canonical signal per observation, and the fan-out plane routes it to every
desk whose predicate selects it ([DATA_SOURCES.md](DATA_SOURCES.md)). Analysis starts there:
coalescing triggers decide when an analyst runs (§2), analyst kinds do the work (§3), action packs
give governed access to external tools (§9), and measurement checks and grades the output (§10). Desk
units, records, voices and the scorecard (§4) are built from these parts by descriptor.

---

## 2. Coalescing triggers

Code: `runtime/triggers/`.

An analyst does not run once per signal. A matched signal marks the `(analyst, target)` pair dirty,
and the analyst fires when a gate trips.

### 2.1 The gates

`triggers/policy.py` evaluates in this order; the first match wins:

1. Cooldown: after a fire the pair is muted for the cooldown window, whatever arrives.
2. Severity: one pending signal at or above the severity threshold fires immediately. An unknown
   severity label ranks below `info` and never trips this gate.
3. Accumulation: the pending count reaching the threshold fires the batch.
4. Cadence: the periodic tick fires if at least one signal is pending.
5. Otherwise the pair holds.

### 2.2 Accumulator and dispatch

The accumulator lives in Postgres and survives restarts. It holds the pending count, the highest
pending severity, the last-fired and first-dirty times, and a bounded set of canonical signal ids, so
two deliveries of one observation count once. `triggers/coalescer.py` claims a fire by
compare-and-set on the last-fired time, so exactly one worker dispatches it, and resets the
accumulator in the same step. A fire carries the whole batch since the last fire.

`triggers/engine.py` holds one durable pull subscription over the registered subject filters,
re-checks each signal against the desk's full filter and Starlark predicate, and acknowledges a
message once its dirty state is persisted. A new upstream finding arrives as a derived signal on the
same stream.

### 2.3 LLM-bearing analysts

Every method kind except the deterministic, forecaster and compile kinds counts as LLM-bearing. For
those the policy floors the accumulation threshold to a minimum batch and disables the severity gate
unless the trigger policy sets `allow_llm_severity_wake`. The deterministic runner raises if handed
an LLM-bearing kind.

---

## 3. The analyst kinds

Code: `data/analysts/`.

At startup the runtime walks this package and registers each kind by its `KIND_NAME`. A kind module
exposes `KIND_NAME`, an async `run_method(inputs, options, deps)` and an `OUTPUT_KIND`; optionally a
`READ_SLICE` (when it reads across desks or reads other analysts' outputs) and a
`build_prompt_module` (when the optimizer can compile it). A declared module that fails to import or
lacks the contract stops the boot, and the walker lists every failure at once.

| Kind | Method | Reads | Writes |
|---|---|---|---|
| `inline_target` | LLM planner over one desk | the desk's signal slice plus a grounding preamble | finding |
| `cross_target_raw` | LLM planner over many desks | raw signals across the subscribed set | finding, marked cross-target |
| `meta_findings_synthesizer` | assembly, or LLM planner | other analysts' verified findings | finding, marked meta |
| `cross_analyst_correlator` | LLM correlation | many analysts' outputs | finding tagged contradiction, agreement or blind spot |
| `relationship_reifier` | small-model relation typer | co-mention pairs plus open facts | signed nexus, with a finding receipt |
| `competing_hypotheses` | LLM proposes and scores the matrix | situations, facts, signed nexuses | hypotheses, with a finding receipt |
| `consult_on_demand` | bounded ReAct loop | a question plus a scope predicate | consult response |
| `deep_consult` | durable workflow | a deep-research question | finding, written by the workflow |
| `predictor` | statistical forecaster | a window of recent signals | prediction |
| `deterministic` | Python sub-handlers (§5) | substrate slices, the graph, time series | structured findings and side-writes |
| `critic` | LLM judge against a rubric | one output plus its rubric | critique |
| `optimizer` | prompt search in a workflow | an analyst's traces joined to its critiques | prompt-module candidate |

Four extension kinds register by name through the vocabulary family: `journal_assessor` (§8),
`entity_researcher` (§5.1), `signal_salience` (§5.5) and `situation_tracker` (§6). A new kind is one
module plus a descriptor registration. A new analyst is usually only a descriptor.

The write path routes on a closed set of output kinds in `provenance/kinds.py`: finding, situation,
hypothesis, prediction, alert, meta-finding, critique, fact, nexus, prompt-module candidate, journal,
scorecard, situation update and event. Event rows are written only when `LEGBA_EVENTS` is on (default
off).

### 3.1 From descriptor to run

The descriptor declares:

- `identity.kind`, the registry key the walker resolves to a module;
- `method.kind`, plus an inline system prompt or a prompt-module reference;
- `method.llm`, whose `primary` is a stack reference and whose optional `verify` or `judge` key opts
  the analyst into the claim check (§10.2);
- `subscription.targets` (optional): present, the analyst runs once per matched desk; absent, it is a
  meta analyst with one global run;
- a cadence block, an optional grounding block, pack grants, output bindings, and a rubric.

Adding a desk is registering a target with the right coverage tag; no descriptor changes.

The builder resolves the stack reference into a chat handler, picks the system prompt (or a promoted
candidate, §10.8), and installs the grounding hook if declared. The run is deterministic around one
model call, so a trace can be replayed: orient (pack the slice under `LEGBA_LLM_INPUT_TOKEN_BUDGET`,
with `LEGBA_SLICE_ROW_CAP` as a backstop), plan, ground, reason, reflect (malformed output becomes an
unstructured finding), narrate. The actor host does the write.

Keep `cadence.cooldown_seconds` below the schedule interval; a cooldown equal to it halves the
cadence.

---

## 4. Desk units and compositions

The desk read is built bottom-up from existing kinds. Each tier reads only what the tier below has
cited and scored. Classification is per profile: axes are vocabulary families (`<profile>.<axis>`),
tags are `<family>:<value>` checked at write time by `data/tags.py` (`bound`, `unclassified`, or
`src:<value>` for a bare string), and a desk id is stored as `data.subject_id`, never as a category
([DATA_MODEL.md](DATA_MODEL.md)).

### 4.1 The bounded units

- Reads: one desk's recent signal slice, plus a grounding preamble of facts, signed nexuses and graph structure (§12.4)
- Writes: one strict-JSON finding with `[N]` markers mapped to signal ids, an indicator block, and an optional open-questions array (§7)
- Switch: one `inline_target` descriptor per unit; `subscription.targets` selects the desks
- Cadence: twice a day, staggered across distinct hours

Each unit answers one narrow question for one desk. The example profile ships nine:
`leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`,
`internal_stability`, `military_posture` and `economic_coercion` over every desk with a country
coverage tag; `proliferation_watch` over a narrower predicate; and `disruption_status` over flow and
shipping-lane desks on a shorter window. A new unit is a new descriptor.

Every unit prompt carries the same contract, pinned by a test per descriptor: the read opens with the
as-of line from the slice header, stock template sentences are banned, machine internals stay out of
the prose, and an absence is stated as "not observed in collected reporting", never as a world fact.

### 4.2 The record

- Reads: the units' verified findings, admitted above `LEGBA_COMPOSITION_VERIFY_FLOOR` by an inner join on the critique
- Writes: one row per desk, a block per admitted unit read quoting its lead span byte-for-byte under an ordinal
- Switch: `LEGBA_COMPOSITION_ASSEMBLY` (`1` for every composition analyst, or a comma list of analyst ids; unset is the older generative path)
- Cadence: twice a day, after the units

The record is a `meta_findings_synthesizer` descriptor in its assembly regime. It writes no sentence
of its own: blocks are joined by a closed connective vocabulary, confidence is the weakest block's,
and no model is called. Every composition row stamps `data.data.assembly.regime` (`assembly`,
`legacy` or `rollup`) so the regimes are never pooled. Four deterministic checks (quote fidelity,
scope preservation, attribution equality, selection) audit the payload spans and report without
withholding. A desk with no verified unit claim gets an empty record that says so.

- `LEGBA_COMPOSITION_TIERED_EVIDENCE` splits the record into a verified basis and a capped periphery
  of below-floor rows, usable only as hedged context.
- `LEGBA_COMPOSITION_CORRECTNESS_GATE` (with `_MIN_CORRECTNESS`, `_MIN_COVERAGE`,
  `_ALLOW_SINGLE_FAMILY`) quotes a unit with a missing, weak or single-family correctness row outside
  the basis, still visible and in lineage.
- Carried status (`data/carried_state.py`): a read stating a status above `low` with none of its own
  indicators `triggered` becomes `carried_unsupported` once the newest triggered read on that subject
  is older than `LEGBA_CARRY_EXPIRY_WINDOWS` (default 3) cadence windows, or 90 days. The record lifts
  it out of the basis and names it; `GET /v3/absence?scope=` serves the same kind for units no
  composition reads.
- Level indicators (`method.options.level_indicators`, `data/indicator_shape.py`) read
  `observed_at_level` when a signal in the window reports a standing condition at a declared bar, which
  re-evidences the status. One cited only to the unit's own memory is reset to `not_observed` and
  listed in `data.level_indicator_demotions`.
- A block resting on one source after wire folding carries `[single-source]`. Outlets running the
  same agency dispatch count as one source and the fold is printed as `N wire-folded`
  (`descriptors/wire_map.yaml` declares outlet-to-wire relationships). Nothing is dropped.

### 4.3 The voice

- Reads: exactly one record row
- Writes: interpretive prose under its own analyst id, citing the record's ordinals
- Switch: its descriptor; the prompt lives in code
- Cadence: twice a day, after the record

Its lineage must be exactly its one record row, or the run raises; a test fails if the prompt module
names any database or other analyst; every citation must resolve to a real ordinal. Confidence is
the record's, and the voice never emits a severity tag. It is always judged, against an evidence map
of each block's span plus the record's arithmetic.

A deterministic pass marks, without removing, sentences with an unearned superlative, a causal link
the record lacks, an over-wide absence, prose about the machinery, no ordinal, a proper name absent
from the record, or a gaps-section absence over a whole subject class. Mark offsets are UTF-16 code
units, the unit a browser slices in. The pass publishes how many sentences it checked and which
classes ran.

### 4.4 The region tier

- Reads: the member desk records of one region frame
- Writes: a rollup with member count, members carried and missing, and each member's blocks copied unchanged
- Switch: `analyst_region_composition`; under assembly the regime is `rollup`
- Cadence: twice a day, after the desk records

A rollup has no prose, so the structural-claims check re-derives its numbers in place of a
faithfulness critique. It cannot write cross-border synthesis, and since it has no faithfulness
verdict the world tier reads country records directly.

Frames are entries of the `frame` vocabulary family (`descriptors/vocabulary_frame.yaml`), each with
a `parent` link and a member rule (`tag:<t>` or `analyst:<id>`). With `LEGBA_FRAME_FAMILY` off
(default) compositions use the legacy id-prefix convention; a test holds the two equal.

### 4.5 The world tier

- Reads: the verified country records, the thematic composition, and each carried country's voice as context
- Writes: the world record and the world voice, split like the desk tier
- Cadence: twice a day, after the country voices

A region with no record falls back to its member country records. A region with neither is listed as
an unassessed gap in a coverage block.

### 4.6 Thematic compositions

`escalation_composition` reads one dimension across every desk and writes one global read, judged
like any cited finding. Sibling units can rest on the same wire signal, so a correlation guard finds
cited reads whose lineage sets intersect, collapses each cluster to one evidence unit, and caps
confidence at the de-duplicated level. Twice a day.

`escalation_dyad` uses the same machinery for one pair of desks. It reads the escalation unit reads,
whose lineage is signal ids (a shared incident lands in both desks' slices, so the guard sees it). It
feeds nothing upward.

### 4.7 The banded scorecard

- Reads: verified claims for every active desk over a rolling window
- Writes: one banded row per desk; each band names the verified claim it rests on
- Switch: `analyst_scorecard_producer` (deterministic, no model call)
- Cadence: daily, after the units, records and claim check

Confidence decides whether a claim may band, never which band. A band reads the dimension's
standing severity; movement rides a separate delta tag that no rule reads. A dimension with no
qualifying claim reads `insufficient-evidence` with a reason, and every active desk gets a row. The
card bands the newest read the record consumed that passes the same guards; if one fails, the
dimension abstains and names the read and the rule.

### 4.8 Indications and warning

`indicator_tracker` follows each pre-registered indicator through its unit's runs and reports status
flips. A watermark per stream means each run is walked once. Every change is written as a `raw` flip
for audit; a change becomes a `debounced` flip (the only kind fed, escalated or served) once the new
status holds for `hysteresis_runs` runs (default 2) or `hysteresis_hours` (default one unit cadence).
Each flip is written once to `indicator_flips`, and a run that inserts no debounced flip writes only a
trace, so an idempotent re-run never repeats a feed row. Flips found on a stream's first sweep are
recorded as `backfill` and never fed. Every 30 minutes.

`collection_gap` reads scorecard rows and the source-request backlog, ranks starved desk × dimension
cells with the reason and the source classes that could feed them, and writes collection requirements
with matched candidate sources ([DATA_SOURCES.md](DATA_SOURCES.md)). Monthly, with a daily sibling for
reference gaps.

### 4.9 Reading beyond the slice

`corpus_researcher` reads a prioritised handful of open questions (§7) into its ground phase. It
holds the substrate-read, web-access and research packs (§9). Twice a day.

`cross_doc_corroborator` picks one checkable claim from a recent global slice, searches beyond it
with vector search, corpus search and document reads, and writes one cited verdict: corroborated by N
independent sources, single-sourced, or contradicted. Its prompt names one wire story under two
mastheads as one source. Twice a day.

---

## 5. The deterministic library

Code: `data/analysts/deterministic_handlers/`.

These handlers run under the `deterministic` kind, chosen by `method.sub_handler`. They make no model
call. Adding one is a module plus a registry entry. Most carry a structural-verify exemption; an
opt-in subset declares structural claims, quantities the claim check re-derives from the finding's
own lineage, and their badge reads "checked" only when every checkable claim re-derived.

### 5.1 The entity graph

`entity_resolution` folds the next batch of unresolved signals into the graph (one profile per
canonical mention, signal-to-entity edges, co-occurrence edges) and stamps each processed signal.
Every 10 minutes. `entity_researcher` records a same / not-same / unsure verdict per blocked
candidate pair using the core model, and never merges. Twice daily.

### 5.2 Duplicate observations

`cross_source_dedup` links exact content-hash duplicates (and semantic near-duplicates when a vector
client is present) by setting the canonical pointer and writing an alias row; it never deletes. The
coalescer keys on this link. Every 15 minutes. `cross_source_coalesce` links same-event reports with
no shared hash over one embedding collection, and writes nothing if its embedder is absent. Every 6
hours.

### 5.3 Other handlers

| Sub-handler | What it does |
|---|---|
| `graph_mining` | community detection, balance triads and proxy chains over signed edges; the hostile-edge shortlist requires checked endpoints, a hostility relation and negative polarity |
| `structural_balance` | signed triadic balance over the relationship graph (§12.3) |
| `proposed_edge_governance` | promotes corroborated pending edges to neutral nexuses, rejects thin stale ones |
| `nexus_decay` / `fact_decay` | time-based confidence decay; fact decay is superseded by `fact_decay_scan` (§5.5) |
| `situation_clustering` | upserts situation frames on a signature |
| `thematic_proposal` | proposes thematic frames for uncovered topics; slugs derive from the situation signature |
| `finding_supersession` | links a newer near-duplicate finding as superseding the older one |
| `anomaly_detection` | volume spikes, sentiment shifts and novel entities over bucketed windows |
| `adversarial_signals` | flags coordinated or manipulative signal patterns |
| `entity_gc` | removes stale and orphan entity profiles; pauses a source on a long error streak |
| `hypothesis_lifecycle` | older hypothesis maintenance, superseded by `competing_hypotheses` |
| `integrity_sweep` | cross-table invariant checks |
| `composition_lineage_sweep` | repairs record lineage after supersession |
| `signal_summarizer` / `signal_embedder` / `corpus_indexer` | summaries, embeddings and the search index |
| `reenrich_ner` / `reenrich_translation` | forward-only re-enrichment of signals earlier gates skipped |
| `reenrich_pending` | re-runs enrichment for signals marked `payload.enrichment_pending`; clears the marker on success |
| `verify_backfill` | writes the missing faithfulness critique for cited findings that have none; stamped `judge_trigger = 'verify_backfill'`; every 30 minutes |
| `corpus_retention` / `signals_retention` / `analyst_traces_retention` | one sweep engine over the retention config table; the seeded policies are disabled |
| `evidence_archiver` | fetches and content-addresses cited evidence ([DATA_SOURCES.md](DATA_SOURCES.md)) |
| `archive_retention` | expires stored bytes of uncited archive rows after 45 days; off unless `LEGBA_ARCHIVE_RETENTION` |
| `research_measurement` | counters over research-landed signals, each rate beside its n |

### 5.4 The alert loop

`alert_trigger_scan` reads verified state transitions and the production gauge, writes alert rows
whose lineage names the basis, and hands them to the sinks. Every 10 minutes. Its classes are band
crossing, verified high or critical finding, contention flip, baseline deviation, watchlist hit, geo
convergence, production deficit, situation escalation, coverage floor and external contradiction.
Each fires on a verified transition, never on a level. The first scan per class seeds watermarks
silently; afterwards a watermark advances only after the alert row lands, so a failed write retries
and a transition fires once. A per-desk cap folds the remainder into one alert. Which kinds page is
set in [ALERTING.md](ALERTING.md).

### 5.5 Readouts

These write a recomputable sidecar and a readout finding, never changing primary rows. Daily, except
`signal_salience`, which runs hourly and calls the model.

- `desk_baseline`: each desk's activity baseline with a Poisson-floored sigma; a prior over
  collection counts that feeds the baseline-deviation alert, not a forecast.
- `fact_decay_scan`: per-fact confidence decay; grounding reads it when `LEGBA_FACT_DECAY_WEIGHTING`
  is on.
- `source_track_record`: each source's record over resolved contentions, for the arbiter (§11).
- `narrative_mapper`: narratives over contested claims and a source-echo graph; an edge records
  publication order and lag only.
- `signal_salience`: stamps each text signal with a consequence score; the model supplies event
  class, actor rank and magnitude, and authority comes from the source's registered class.

---

## 6. Situations

A situation is a durable frame keyed by a signature, built by `situation_clustering`.
`situation_tracker` is the only writer of the trajectory ledger.

- Reads: each open situation with new verified evidence since its watermark, plus its prior state
- Writes: a claim-checked situation-update finding and append-only ledger rows
- Switch: `analyst_situation_tracker`; `LEGBA_SITUATION_TRACKER_MAX_SITUATIONS` per tick
- Cadence: hourly

The model sees only verified rows with ordinals and must cite them. "Unchanged" is the expected
answer; any other movement without new evidence is rejected by the Python type and a database
constraint. The ledger's clock is the newest cited item's time.

---

## 7. Open questions

An open question is a `hypotheses` row with an open-question status and a marker object that writers
dedupe on. Nothing here closes a question; it links evidence and raises review flags for a person.

Questions come from a backfill script (dry-run by default) over question-shaped state such as a
dimension banded insufficient or a finding graded under the floor; from a unit finding's optional
`open_questions` array; and from `consult_watch_mint`, which turns a consult answer's "what would
change my mind" items into watches. A consult watch folds onto an open one on the same desk within
±3 days when the claim cores match or their embeddings reach cosine 0.86. Consult watches
(`created_by='consult'`) stay out of desk grounding and the researcher's backlog unless
`LEGBA_DESK_GROUNDING_INCLUDES_CONSULT_WATCHES` is on (default off).

The researcher (§4.9) reads questions by live reach (a forward walk over the consumption index),
then harvest class, salience and age. A finding that answers one adds a bearing edge, which never
changes the question.

### 7.1 The watcher

- Reads: signals since a durable cursor, against the open questions
- Writes: bearing edges, review flags where a question traces to a live product, a staleness count
- Switch: `analyst_claim_watch`; optional model legs in `method.options` that can only remove matches
- Cadence: every 30 minutes

`claim_watch` weighs each (signal, question) pair on vector similarity above a floor, canonical
entity overlap (each entity weighted down as it becomes common across the desk's questions), and
geography as a tie-breaker. Geography alone or one shared entity stays under the bar. Each edge
stamps the matcher version. A cursor that falls too far behind skips ahead and records how many
signals it skipped. The watcher writes no corrections; closing the loop is listed in
[SEAMS.md](SEAMS.md).

### 7.2 Forward lineage

`derived_from` answers "what did this rest on"; the consumption index answers "what rests on this".
It is written with the output, where the record or journal selects its inputs, and records whether
each use was basis or caveat.

---

## 8. The journal

The journal narrates the system's own state. Its rows go to a dedicated table with empty lineage,
outside the lineage catalogue, so no walk from a fact or finding reaches it; a test enforces that it
never writes a fact. All tiers use the `journal_assessor` kind, and the analyst id selects the tier.

| Tier | Reads | Writes | Cadence |
|---|---|---|---|
| entry | the freshest window through its self-instruments | an append-only entry | twice daily |
| consolidation | the prior consolidation plus recent entries | one superseding narrative | daily |
| chronicle | the verified top of the composition stack | a cited third-person record entry | weekly |
| lens ×4 | the same, through one prior each (trend, base rate, capability, intent) | an interpretive read, no new facts | weekly |
| lens diff | the four lens reads | where they agree, split or diverge | weekly |
| profile lens | the same, through a profile's declared prior | an interpretive read, no new facts | per profile |

A lens prior lives in its persona module, so changing it changes the descriptor hash. Profiles add
lenses through their prompt package and a `<profile>.lens` family ([PROFILES.md](PROFILES.md)).

An inquiry (`analyst_inquiry_pilot`; `crossroads` for the fixed-mandate one) adds a brief (under 2,000
characters on the descriptor) and a ledger of hypotheses, questions, expectations and observations
carried across cycles. A hypothesis needs a resolution test frozen at write, and refusals flag the
entry. Inquiries are scored by `inquiry_yield` and have no web access.

Each run goes plan, gather (a bounded ReAct loop), field notes, narrate, reflect, then sets flags
from substrate metrics; a numeric guard flags source-health counts that disagree with the
deterministic read. The journal holds only `journal_read` and `journal_propose`: it writes its own
entries, and everything else goes to a human-gated queue.

---

## 9. Action packs

Code: `data/analysts/agency/`. The full gating model is in
[AGENCY_GATING_MODEL.md](AGENCY_GATING_MODEL.md).

A pack bundles tools, prompt fragments, rules and emit channels with an applicability predicate
(compiled at registration) and a governor. It runs for an `(analyst, target)` pair only inside

```
analyst.action_packs  ∩  target.allowed_action_packs  ∩  pack.applicability
```

and only if the governor admits the call. A predicate that cannot be evaluated denies, and
per-binding governor overrides can only tighten. Each call validates its arguments against the
grammar shown to the model (refusals come back as `tool_blocked`), checks per-pack caps and the
global envelope including this call's estimate, records an invocation row, dispatches and settles
outcome, cost and duration. A per-analyst or global budget hit demotes the actor.

| Pack | Tools | Held by |
|---|---|---|
| `substrate_read` | read-only substrate, corpus, document, graph and product readers | consult, the researcher, the corroborator, the chronicle |
| `journal_read` | top-of-stack readers and self-instruments | journal tiers and lenses |
| `journal_propose` | proposals into the human-gated queue | journal tiers |
| `web_access` | web search and page fetch, read-only | consult, the researcher, the auditor, the reference builders |
| `research` | lands web results as evidence | the researcher |
| `escalate_finding` | the escalation emit | units and the desk record |
| `propose_facts` | propose a fact, request a source, open a question (all human-gated) | by grant |
| `media_processing` / `incident_response` / `discovery` | async media job, incident emit, source discovery | by grant |

The research pack lands each kept web hit as a signal with its retrieval origin, a credibility cap
and a synthetic source id, after a robots check. `LEGBA_RESEARCH_EVIDENCE` is `off` (default; the
tool refuses), `substrate` (rows written, kept out of desk slices) or `desks`. The escalation pack
fires after the claim check, on the verify-demoted confidence. A consult answer whose claims resolve
to no cited row shows a provenance-hold banner.

---

## 10. Measurement

Three separate axes are published and never averaged together: faithfulness of each claim to its
cited evidence, correctness of desk reads against independent references, and an external audit of
published claims against the web.

### 10.1 Traces and receipts

Every run writes one trace row (output, side-write, trace-only or failure) and extends a per-analyst
SHA-256 receipt chain that follows the analyst across descriptor versions. The chain is not signed;
signatures belong to the audit log (§10.7). One row per run lets the production-deficit alert tell
"did not run" from "produced nothing".

### 10.2 The claim check (faithfulness judge)

- Reads: a new finding's prose and its citation bridge (`[N]` markers to signal ids, or record markers to sub-claims)
- Writes: a critique with the score, the judging model, the route rung and a per-claim verdict list
- Switch: `method.llm.verify` or `method.llm.judge` opts in; `LEGBA_VERIFY_LLM_JUDGE` turns on the model judge (code default off; `.env.example` sets 1)
- Cadence: after every cited finding

It checks whether each claim follows from its cited evidence, not whether it is true. A deterministic
floor always runs: a fact claim with no marker, or a marker that resolves to nothing, is unsupported,
and the score is the supported share. The model judge refines the verdicts; without it the floor
result is published under a provisional ceiling. An unsegmentable body is published as unassessable,
and a unit read citing only its own memory as `carried_ungraded`, both with no score.

The judge route is `LEGBA_JUDGE_STACK_REF`, then `method.llm.judge`, `method.llm.verify`,
`method.llm.primary`; an analyst with neither `judge` nor `verify` gets none, and the override cannot
enable one. Shipped descriptors point `verify` at the same endpoint as `primary`, so by default the
model checks its own output. To judge with another model family, point `LEGBA_JUDGE_STACK_REF` at a
registered component of that family; the cross-family judge component in `descriptors/` ships
`draft`. `method.llm.judge_chain` and `LEGBA_JUDGE_CHAIN_DEFAULT` set fallback rungs.

Hard failures: a citation resolving to nothing, a claim its own source contradicts, a stale
officeholder, a desk finding naming only other countries, an absence the analyst's own slice
contradicts. Everything else (uncited fact claims, hedge laundering, periphery-only clauses, unscoped
absences, `carried_unsupported` anchors, and others) is soft, and unknown reasons are soft. A
contradiction is demoted to soft when its quote is missing, only restates the claim, comes from the
prior read the claim updates, or comes from a finding the claim did not cite. The
officeholder and country tables come from the profile's gazetteer ([PROFILES.md](PROFILES.md)); a
guard without tables reports `gazetteer_not_applicable_<guard>` ([SEAMS.md](SEAMS.md) #67).

The judge does not change the finding. Reads compute `effective_confidence = min(confidence, score)`
and store all three; the record's join, the scorecard and escalation read the folded value. Every
critique stamps a pipeline version so different rule sets are never pooled.

### 10.3 The critic

The `critic` kind grades one output against its analyst's rubric, refuses to grade with the model that
produced it unless the analysed descriptor opts in, and fails without a rubric. The shipped critic
descriptor is `retired`.

### 10.4 The correctness grader

Grades unit reads against the current independent reference and writes one correctness row per unit
with a per-claim ledger. It refuses to publish without a passing calibration row covering its rubric
hash and every model it will use. Up to three families vote; the paid ones are skipped when
`LEGBA_GRADER_DAILY_CEILING_USD` is zero. Nine hourly ticks a day, least-recently-graded first. See
[GRADING.md](GRADING.md).

### 10.5 The reference builder

Builds one independent reference per desk and window from the web, with a verified-span rate and a
list of thin dimensions (`LEGBA_REFERENCE_BUILDER_ENABLED`). Hourly, one target per tick. The grader
never builds its own reference. See [GRADING.md](GRADING.md).

### 10.6 The external audit

- Reads: published claims (record spans and voice sentences) and the web through `web_access`
- Writes: one critique per audited claim, an alert on a confirmed contradiction, the external-truth ledger
- Switch: `analyst_standing_auditor`; `LEGBA_EXTERNAL_GRADING_WIDTH` widens the small daily sweep to a full drain
- Cadence: hourly

It asks whether a claim is true in the world, and only a contradiction scores against a claim; the
result never enters the faithfulness fold. Record spans, voice sentences and legacy prose are kept as
separate populations. The search ladder is `serp_provider_order`, and the shipped descriptor lists only
the free rung; a metered rung, if added, is used only for undecided absence claims, one query each,
and only when `web_access` declares a daily cost cap. An absence claim needs a verified empty result
(a control probe showed the engines answering); otherwise it is written `unchecked`. An exhausted
budget switches to a hash-sampled mode whose fraction is stamped on every published number.

### 10.6.1 The contrary-evidence pass

- Reads: the same published claims as the external audit
- Writes: one contention record per material claim, describing what the search found, never a verdict
- Switch: `analyst_contrary_evidence_pass` (`draft`); `paid_rung` ships `false`, which truncates the ladder to the free rung in code
- Cadence: daily

For each claim the pass searches for the strongest opposing proposition. If the claim takes a side in
the polarity vocabulary, the query is built from the claim's subject and the opposite state's words;
otherwise one core model call proposes it. A query that adds no content word the claim lacks is a
paraphrase and is refused before searching; `query_novel_tokens` is recorded on every row. Stances
are derived in code from the fetched text. Fetched pages follow the audit's fetch rules and are not
added to the signal corpus.

A page must pass four fences to carry `contradicts`; one admissible page is `qualifies`.

| Fence | Rule | Column |
|---|---|---|
| F1 host class | not a reference, encyclopedia or dictionary host | `host_class` |
| F2 dated, in window | a machine-readable date inside the claim's window plus `contention_ttl_hours` | `page_published_at` |
| F3 subject in sentence | the polarity match shares a sentence with the claim's subject tokens | `subject_overlap` |
| F4 two independent pages | two pages from independent outlets, each past F1–F3 | `independent_pages` |

The columns are nullable and not backfilled (migration 0222). F4 is also a schema check and a filter
in the composition reader. A contradiction that passes all four renders as one hedged line in the
composition's tension section, which the judge grades like any cited line.

### 10.7 Calibration, forecasts and anchoring

| Analyst | Reads | Writes | Cadence |
|---|---|---|---|
| `calibration_tracking` | resolved hypotheses | an exogenous score (graded against later facts), kept apart from a self-consistency score | daily |
| `band_calibration_tracker` | scorecard band transitions | one resolvable claim per transition, graded at fixed horizons | daily |
| `forecast_scoreboard` | the binary-forecast pilot table | issued forecasts, exogenous resolutions, a trace-only receipt | daily |
| `unit_correctness_scorer` | the gold set, reference labels, faithfulness critiques | per-unit faithfulness beside two correctness axes, never pooled | daily |

Band changes are not probabilities, so band calibration counts whether a change held, moved further
or reverted at the horizon; unresolvable claims are reported and left out of the denominators. The
forecast scoreboard issues one forecast per desk, resolves windows by the upstream event's
timestamp, and abstains on a degenerate vector. Each `acute_forecasts` row stores its
`resolution_test` as text at mint; one past its window plus grace is `unresolved:expired` and counts
at maximum penalty in `brier_all`, beside `brier_answered`.

#### Receipt anchoring

`receipt_anchor` runs daily. It Merkle-roots the latest `analyst_traces.receipt_hash` per analyst
and posts the digest to two OpenTimestamps calendars, one `receipt_anchors` row per day and calendar
(`submitted` with proof bytes, or `pending` and retried). Legba does not verify the proof
([SEAMS.md](SEAMS.md) #58). To verify a day offline:

1. Read `root_hash` and `proof` from `receipt_anchors` for that day.
2. Write the proof bytes to `proof.ots`.
3. Recompute the root over that day's chain heads.
4. Run `ots verify proof.ots <root-bytes>`.

Since migration 0262 the ordered leaves are stored too (`data/anchor_proof.py`), so one leaf can be
proven by its sibling path (`GET /v3/ledger/seal-proof`; [LEDGER.md](LEDGER.md)).

#### Audit-log chain

Every `descriptor_audit_log` row carries `chain_seq`, `prev_hash` and `row_hash` (SHA-256 over the
signed content, Ed25519 signature, `signer_did`, id, position and previous hash; migration 0248). A
trigger makes chained rows undeletable; content changes only through a chained `redact` event. The
daily anchor includes the chain head. `scripts/audit_chain.py verify` reports the first break and
each row's signature status.

Threat model: a holder of the database and the signing key can rewrite the whole log and recompute
every hash, but cannot make it match a head already anchored outside, so a rewrite older than the last
anchor is detectable. Any lesser edit, deletion, insertion or reordering breaks the chain at that row.

#### The gold set

A small weekly stratified sample of verified unit reads, pinned on first read, is labelled with a
closed verdict vocabulary. The code records who labelled each row but does not restrict who may.

### 10.8 The optimizer

- Reads: one unit's traces joined to its critiques
- Writes: a prompt-module candidate with a paired before-and-after faithfulness delta
- Switch: `analyst_unit_optimizer`, `analyst_country_optimizer` (both ship `paused`)
- Cadence: weekly

The candidate prompt is re-run and re-judged by the same judge. A non-positive or under-paired delta
is stamped not promotable. Promotion is manual, and the live path uses an evolved prompt only if its
delta is promotable. The search runs as a durable workflow on the actors' control plane.

### 10.9 Method versions

Every published numeric instrument stamps a `method_version`, the revision of the rule set it ran
under, so a change across a method revision reads as an instrument change. It is stored as
`data.method_version` on `analyst_outputs`, `situations.data.method_version`, and a `method_version`
column on `acute_forecasts`, `band_calibration_claims` and `grader_calibrations`. Rows written before
the stamp carry NULL. The Inspector and scorecard panel show it as a chip.

| Instrument | Version | Since | Covers |
|---|---|---|---|
| `scorecard_banding` | `scorecard_banding/2026-09.1` | 2026-09-24 | confidence and faithfulness floors, the band ladder, the severity-to-band map |
| `situation_clustering` | `situation_clustering/2026-09.1` | 2026-09-24 | member-recency half-life, corroboration density, status and dormancy floors, persistence |
| `calibration_tracking` | `calibration_tracking/2026-11.1` | 2026-11-11 | signed hypothesis confidence, Brier with its own n, staleness, volume predictor excluded, one n threshold |
| `band_calibration_tracker` | `band_calibration_tracker/2026-09.1` | 2026-09-24 | resolution spec, 14/28-day horizons, claimable directions, outcome vocabulary |
| `correctness_calibration` | `correctness_calibration/2026-09.1` | 2026-09-24 | draw size, the seeded draw, the superset-coverage gate |
| `forecast_acute` | `forecast_acute/2026-09.1` | 2026-09-24 | event class and sources, rate model, epsilon and degeneracy clamps, resolver grace |
| `indicator_tracker` | `indicator_tracker/2026-10.1` | 2026-10-09 | 30-day lookback, watermarked once-only walk per stream, new ids are not flips, hysteresis, activation predicate |
| `desk_baseline` | `desk_baseline/2026-09.1` | 2026-09-24 | the estimator and Poisson floor, `MIN_ACTIVE_DAYS`, the trigger constants it imports |
| `series_change` | `series_change/2026-11.3` | 2026-11-14 | band rule over live series; `level_rules`, `step_series` and named `series_rules` (`policy_rate`, `fx_move`) |
| `kev_digest` | `kev_digest/2026-11.1` | 2026-11-14 | new register entries since the last watermark, one copied line per entry, quiet runs trace-only |
| `due_and_absent` | `due_and_absent/2026-11.1` | 2026-11-14 | which dated rows are expectations, their grace and tolerance windows, state-change lines only |
| `inquiry_yield` | `inquiry_yield/2026-10.1` | 2026-09-24 | the 7-day window, counter inputs, the anticipated and blind-spot rules, paged findings read |
| `layer_divergence` | `layer_divergence/2026-09.1` | 2026-09-24 | log-ratio constant, median/MAD baseline and floor, two-day rule, severity ladder, wire fold |
| `crossroads_detectors` | `crossroads_detectors/2026-09.2` | 2026-09-24 | the four detectors' thresholds; a failed read reports `UNMEASURED`, never zero |

Changes that predate the stamp:

| Instrument | Era | What changed |
|---|---|---|
| `situation_clustering` | migration 0188 (2026-09) | the frame split re-keyed `situation_signature` and re-based `intensity_score` by member share |
| `scorecard_banding` | damper retirement (2026-08) | `damping_semantics` changed from `demote` to `off`; per-card stamps carry this |
| `forecast_acute` | migration 0075 (2026-06) | the unclamped pilot batch was voided and is excluded from every Brier denominator |

### 10.9.1 Scale versions

A `method_version` says whether the same code produced two numbers. A `scale_version` says whether
two numbers are comparable. The two change independently: migration 0188 moved the intensity scale
with no method change.

Two readings with the same `scale_version` are directly comparable; two with different ones are not.
A scale is named for the quantity, not the module (`intensity/2026-08`), and its era is the month the
scale last moved. It is stored as `data.scale_version` on `analyst_outputs`,
`situations.data.scale_version`, and a `scale_version` column on `acute_forecasts`,
`band_calibration_claims` and `desk_baselines` (migration 0219). Older rows are not backfilled; the
reader shows NULL as "unstamped (pre-2026-09)".

| Scale | Quantity | Published by | Era opened by |
|---|---|---|---|
| `intensity/2026-08` | `situations.intensity_score` | `situation_clustering` | migration 0188, the frame split, which re-based every stored intensity |
| `indicator_status/2026-07` | indicator status `triggered` / `not_observed` / `expired` and its transitions | `indicator_tracker` | the three-value vocabulary (2026-07-02) |
| `band_ladder/2026-08` | a band-ladder transition and persistence rates over transitions | `band_calibration_tracker` | the damper retirement, migration 0187 |
| `acute_probability/2026-07` | `acute_forecasts.p` / `p_base` in the open interval `(P_EPSILON, 1-P_EPSILON)` | `forecast_acute` | the epsilon clamp, migration 0075 |
| `desk_deviation/2026-07` | `desk_baselines` expected, current, band and `deviation_sigma` per 24h bucket | `desk_baseline` | the baseline recipe (2026-07-27) |

Compare a sigma distance or a persistence rate across desks only within one scale era. The reader
shows the scale chip next to each such number ([UI.md](UI.md), `ScaleStamp`).

---

## 11. Contested claims

Fact supersession is newest-wins within a source tier. When equal-standing sources disagree on a
current value, `fact_contention_arbiter` surfaces a winner from the source evidence and keeps both
facts.

- Reads: open facts grouped by subject and predicate, values fuzzy-clustered
- Writes: the contention sidecar and three recomputable markers on the fact rows; never a fact's value, validity or confidence
- Switch: `LEGBA_FACT_CONTENTION` (both values stay open on write); `LEGBA_CONTENTION_EARNED_WEIGHT`; `LEGBA_FACT_CONTENTION_LLM_TIEBREAK`
- Cadence: hourly, trace-only

Each value cluster is scored as the product of distinct backing sources (log-damped, keyed on
lineage), credibility share, a recency half-life and mean confidence. At most one winner per group; a
weak best or a near-tie under the dominance ratio is recorded as disputed. Ties wait out a soak window,
then a weighted tie-break may decide with at least two sources on the winning side. The earned weight
(default zero) comes from `source_track_record` and excludes the contention being decided. The
optional model tie-break runs only on the self-hosted plane, under small caps, with cached verdicts.
Each surfaced winner records who, when and why, with a capped history; unchanged groups are skipped by
input fingerprint until `LEGBA_CONTENTION_REFRESH_HOURS`.

---

## 12. Method

### 12.1 Confidence

- Signal: source reliability × classification confidence, times a weighted modifier over freshness,
  corroboration and specificity.
- Fact: decays without new corroboration, in a sidecar (§5.5).
- Finding: folded with its claim-check score at read time (§10.2).
- Record: the weakest carried block (§4.2). Rollup: the carried share of the roster (§4.4).

### 12.2 Hypotheses

`competing_hypotheses` stores each hypothesis as a thesis and counter-thesis pair with diagnostic
evidence and a signed balance. The model proposes the set and scores each matrix cell in one batched
call per topic; if it is unavailable each cell falls back to a lexical and polarity scorer, and the
row records which path ran. Evidence is the topic's resolved-entity set.

Scoring (`build_ach_matrix`, `_status_for`): for each evidence item, `consistent_with(e)` counts live
hypotheses with a positive cell. A positive cell adds `+1 / consistent_with(e)`, a negative cell adds
`-(max - min) / 4` of its row, zero adds nothing. The balance is `round(Σ)`, compared with
`CONFIRM_K` / `REFUTE_K`. `diagnosticity_mode: shared|off` (default `shared`,
`ANALYST_KIND_OPTIONS["competing_hypotheses"]`); `off` restores the older spread-only scoring, under
which evidence shared by two rivals counted fully for both. Each row's `diagnostic_evidence[0]`
records the mode and per-item `consistent_with` and weight.

Resolution runs the exogenous resolver first (against facts produced later), then the
status-transition fallback; a person's label overrides both. The exogenous resolver abstains on
theses with no direction.

### 12.3 The graph

The graph is relational: entity profiles, the nexus table and the edge store, computed in process with
networkx. There is no graph database (migration `20261115T1829_drop_empty_age_graph` dropped the
unused one). `LEGBA_GRAPH_PROJECTION` (default off) builds `graph_arcs`, one table of cross-layer joins
([DATA_MODEL.md](DATA_MODEL.md)). Structural balance classifies each triad by the product of its edge
signs; the score and the entropy of relationship types are tracked over time.
`LEGBA_EDGE_TRANSITION_LEDGER` (default off) records every edge mint and polarity flip as an event.

### 12.4 Grounding

A model's training data predates the present. A descriptor with a grounding block (default off) gets
a dated preamble of current facts and signed nexuses for the target's geography and the slice's top
entities, preferring curated facts over extracted ones. A failed or empty read means no preamble.
Curating those facts is covered in [DATA_SOURCES.md](DATA_SOURCES.md). A second, per-unit pilot tier
injects passages from a curated document corpus as a non-citable block, with a rollback guard (state
file `LEGBA_RAG_ROLLBACK_STATE`) that turns it off for a unit after a faithfulness drop or a cost rise.

### 12.5 Forecasting

The forecast ledger ([LEDGER.md](LEDGER.md)) holds sealed questions that name what resolves them and
when. `forecast_resolver` proposes outcomes with their evidence; a series-threshold question settles
by rule, any other needs a person to confirm or override (a contributor may confirm; correcting a
decided outcome needs an admin).

The acute pilot (`forecast_acute`) forecasts the chance of at least one severe hazard in a forward
window from a Poisson rate, scored with a strictly proper score against the base rate. Everything is
fixed before the outcome and resolved by the upstream event's timestamp. Degenerate windows are
voided, and the harness withholds a skill figure when a few regions dominate the base rate.

### 12.6 Provenance

Every output stamps the substrate ids it read, so a walk goes from a composed read down to each
signal's acquisition record. New findings also store citations as rows, one per sentence and per
supporting record (`LEGBA_CITATION_ROWS`). The held record, fed only when `LEGBA_HELD_RECORD_SHADOW`
is on, keeps proposed statements as versioned records with a hash-chained status log
([DATA_MODEL.md](DATA_MODEL.md)). The journal is outside lineage, and a bearing edge is a pointer.

---

## See also

- [DATA_SOURCES.md](DATA_SOURCES.md): sources, the canonical signal, enrichment and fan-out.
- [ARCHITECTURE.md](ARCHITECTURE.md): planes, descriptors, the registry and the actor runtime.
- [DATA_MODEL.md](DATA_MODEL.md): tables and their write rules.
- [GRADING.md](GRADING.md): the correctness grader and reference builder.
- [AGENCY_GATING_MODEL.md](AGENCY_GATING_MODEL.md): pack gating.
- [TUNABLES.md](TUNABLES.md): values for the thresholds, budgets and flags named here.
- [SEAMS.md](SEAMS.md): what is declared and not built.
