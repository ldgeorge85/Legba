<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# End-to-end flows

How data moves through Legba: the whole pipeline in one pass, then the walkthroughs, each a numbered
step list naming the table, stream or route the step touches. For the mechanisms themselves see
`ANALYSIS.md` and `ACQUISITION.md`; for the tables see `DATA_MODEL.md`; for values see `TUNABLES.md`.
Where this overview and a detailed flow disagree, the detailed flow wins.

**Contents:** [The whole pipeline](#the-whole-pipeline) · [1 A signal](#1-a-signal) ·
[2 A cadence cycle](#2-a-cadence-cycle) · [3 One unit, one question, and the verify pass](#3-a-bounded-unit-and-the-verify-pass) ·
[4 The tower](#4-the-tower-record--voice--rollup--world) · [5 The scorecard](#5-the-banded-scorecard) ·
[6 Fact extraction](#6-fact-extraction-and-supersession) · [7 Nexus reification](#7-nexus-reification) ·
[8 Competing hypotheses](#8-competing-hypotheses) · [9 Seeding](#9-a-seeding-import) ·
[10 A grounded read](#10-a-grounded-read) · [11 A journal entry](#11-a-journal-entry-or-a-lens-read) ·
[12 A consult](#12-a-consult-and-a-deep-consult) · [13 The optimizer](#13-the-optimizer-workflow) ·
[14 Correctness and the audit](#14-correctness-and-the-external-audit) ·
[15 The coherence loop](#15-the-coherence-loop) · [Entry points](#entry-points)

---

## The whole pipeline

Every arrow is a substrate boundary — a table or a stream — not a function call.

```
SOURCE ─► canonical SIGNAL ─► predicate fan-out ─► the (analyst, target) fire ─► typed OUTPUT
        (baseline enriches     (coarse NATS subject   (cadence reminder or         (routed by kind to
         once, at the source)   + SQL WHERE +          reactive coalescer,          facts / nexuses /
                                Starlark residual)     one cooldown claim)          situations / outputs)

                  … then the product spine, bottom-up, over verified claims:

  bounded UNITS ─► desk RECORD ─► desk VOICE ─► region ROLLUP ─► world RECORD + VOICE ─► banded SCORECARD
   (cite + verify    (quotes the    (reads that    (carries the     (reads the country    (deterministic
    each finding)     verified       one record)    member blocks)   records directly)     rules over
                      heads)                                                              verified claims)
```

### Stage by stage

**Source to canonical signal.** A source actor pulls raw entries on a cadence and runs the per-source
baseline **once** over each, producing a single canonical, target-agnostic signal. The baseline is
deterministic and runs synchronously before fan-out. Flow 1; `ACQUISITION.md` §1–§3.

**Signal to predicate fan-out.** The written signal is published immediately to a coarse subject. Each
desk has one aggregated durable pull consumer bound to the union of its subscription subjects; delivery
is then narrowed exactly by an indexed SQL `WHERE` and a Starlark residual. The coarse subject narrows
*delivery*; SQL and Starlark decide the *match*. Flow 1 steps 6–8.

**Fan-out to the fire.** A matched signal marks an `(analyst, target)` pair dirty, and the analyst fires
on a gate rather than per signal — see the two doors below.

**The fire to a typed output.** The analyst reads its substrate slice, runs its method, and writes one
typed output validated against its kind's model, routed by the universal write path to `situations`,
`hypotheses`, the knowledge tables, the dedicated journal table, or the generic output table. Flow 2.

### One fire, two doors

The cadence cycle and the reactive trigger plane are not two systems — they are two ways the same pair
becomes eligible to fire.

- **Cadence door.** On activation the primary actor registers a single reminder; on each tick it
  resolves the matched desks and fans them to worker actors. This is the floor — a quiet desk is still
  re-evaluated on schedule.
- **Reactive door.** A matched signal, or a new upstream finding arriving as a derived signal on the same
  stream, marks the pair dirty via the coalescer, which fires on whichever gate trips first — severity,
  accumulation, or cadence-with-pending — clamped by a per-pair cooldown.

The two doors converge on one lock: the coalescer claims the fire by compare-and-set on the
accumulator's last-fired anchor, so exactly one worker wins, and the worker re-checks a per-target
cooldown with a slack band before doing work. A pair fires once per cooldown window however many doors
opened, and LLM-bearing analysts are floored to a minimum batch, never per signal.

### Two tiers

- **Tier 1 — inline, deterministic, per signal, before fan-out.** The baseline filter chain. It writes
  altitude-zero product every downstream desk reads: the enriched signal row, ingestion facts, and
  entity rows with their provenance links. Flows 1 and 6.
- **Tier 2 — slice and cadence, per `(analyst, target)`.** The analyst kinds read an accumulated slice
  on a fire and reason over it, never per signal. This is where model cost lands, and the coalescer is
  the device that decouples that cost from signal arrival rate. Flows 2–5 and 7–15.

---

## 1. A signal

One sentence: a source actor pulls raw entries on a cadence, runs the baseline once over each, writes
the single canonical signal, and publishes it where per-desk durable consumers fan it out.

1. **The poll fires.** The reminder wakes `SourceCore.pull_once`, which reads the persisted cursor from
   `actor_filter_state` and passes it as `since`. *(table: `actor_filter_state`)*
2. **Entries are walked** under a count and wall-time budget resolved from the descriptor, then the
   handler's own advertisement, then a generic default, each clamped to a ceiling — so the poll always
   finishes inside the drain window and a handler that knows its own shape is not truncated.
3. **The baseline runs once, at the source.** Tier 1 fills the typed indexed columns; tier 2 handles
   media by reference or eagerly; tier 3 runs the descriptor-ordered enrichment chain, which may drop
   the signal by returning nothing. The chain mutates the signal in place **and** returns it — the
   in-place mutation keeps the handler-yielded object authoritative, and the return lets a filter
   replace or drop it. *(tables: `signals` columns, `facts`, `entity_profiles`, `signal_entity_links`)*
4. **The canonical write.** A surviving signal is written through `write_canonical_signal`, which pins
   the owning tenant and stamps the enrichment columns into the one row — there is no separate
   enrichment table. An intra-source exact-hash re-serve inside the lookback window instead advances the
   existing row's last-seen stamp and skips the insert. *(table: `signals`)*
5. **Ingest dedup runs in the same connection**, after the insert, because the alias link needs the row
   to exist. A hit writes an alias row and sets the duplicate's canonical pointer. *(tables:
   `signal_aliases`, `signals.canonical_signal_id`)*
6. **The publish is immediate**, as the row is written rather than batched at the end, so fan-out
   survives a later cap, error or drain. *(stream: `legba.signals.<tenant>.<source>.<modality>.<class>`)*
7. **The cursor advances** in a `finally`, on the three-branch policy: a hard error keeps the window; one
   or more consumed entries advance to the last consumed logical timestamp, not to now; zero entries
   leave the cursor alone. One poll-outcome row lands either way. *(tables: `actor_filter_state`,
   `source_poll_outcomes`)*
8. **Fan-out to desks.** The shared stream captures by subject filter; each desk has one aggregated
   durable pull consumer bound to the union of its coarse filters. Matching is two-stage — an indexed
   SQL `WHERE` over the structured columns, then the Starlark residual on the narrowed set under a
   wall-clock budget, failing closed. *(stream: `legba_signals`)*
9. **Arrival.** The signal is in the desk's pull window. From here the cadence cycle reads it.

---

## 2. A cadence cycle

One sentence: the primary analyst actor owns one cadence reminder; on each tick it matches active desks
by predicate, fans one run per matched desk to bounded worker actors, and each worker reads its slice,
invokes the kind, writes the typed output, extends the receipt chain, emits bindings, and may escalate.

**Registration (once, at activation).**

1. The primary actor registers a single reminder timed from the descriptor's fallback schedule. Workers
   carry no reminder — the primary owns cadence.

**The tick.**

2. **The reminder fires** and runs a stale-fire guard that self-disarms on a version bump or a pause.
3. **Desk matching** evaluates the subscription predicate against the active target descriptors, in one
   of three regimes: a target-bound analyst gets one run per matched desk; a critic-kind analyst
   resolves the newest ungraded findings and fans one bounded grade per finding row; any other meta
   analyst gets a single global run with no target filter. *(table: `target_descriptors`)*
4. **Fan-out** chunks the matched desks at a bounded width and dispatches each to a distinct worker actor
   id, so each gets its own turn queue and runs concurrently. The primary orchestrates and does not run
   the per-desk work itself.

**The per-desk run.**

5. **Lazy activation.** A worker with a target filter and no state record creates an active record
   inline, with no reminder. *(store: the actor state store)*
6. **Cooldown and slack.** The run is gated by a per-target cooldown; a small slack band absorbs drift
   when the cooldown and the cadence interval are close, which is what stops a cadence silently halving.
7. **Read the slice.** The kind's read-slice adapter runs — the critic reads one output row by id, a
   composition reads other analysts' verified findings, the default reads the recent signal window
   honouring the subscription's time window and narrowing by the desk's source refs and geo scope, so
   each desk reads its own slice rather than the global pool. *(tables: `signals`, `analyst_outputs`)*
8. **Budget pre-call.** The run is projected against the per-analyst and fleet-wide caps, yielding
   proceed, throttle or exhausted; an exhausted outcome records a demotion audit and a strategy.
   *(tables: `budget_ledger`, `global_budget_envelope`, `budget_demotion_events`)*
9. **Invoke the kind.** The deps-builder resolved the kind, its model handler and its deps bundle at bind
   time; transient failures retry with backoff through an exception classifier. For an opted-in
   `inline_target` this is where the ground phase fires (Flow 10).
10. **Select the payload by kind**, through the per-output-kind selector table.
11. **Write the typed output.** The writer validates against the kind's model and routes the insert to
    the right table. A validation failure routes to the dead-letter table and the run reports a hard
    failure. *(tables: `analyst_outputs`, `situations`, `hypotheses`, `journal_entries`,
    `output_dead_letter`)*
12. **Extend the receipt chain.** A chain-consistent hash row lands carrying the intermediate steps, tool
    calls, prompt hash and output reference. This is a re-computable chain over traces, not a
    cryptographic signature. *(table: `analyst_traces`)*
13. **Publish and emit.** The write helper publishes the output envelope on a per-analyst channel, then
    dispatches the descriptor's output-kind bindings best-effort. *(table: `alert_sink_deliveries` for
    the alert binding)*
14. **Optional escalation.** For findings, the escalation gate reads the first-class severity column and
    the **post-verify** folded confidence against the pack's gates, resolves the desk's allowed packs,
    and runs the tool through the agency pipeline. *(tables: `action_pack_invocations`,
    `governor_events`)*
15. **Outcome.** The run returns success, transient failure, budget-throttled, hard failure or no-op.

**One rail carries every analyst.** The units, the records and voices, the deterministic producers, the
journal tiers and the lenses all take one of the three regimes at step 3. There is no per-analyst code:
a new analyst is a new descriptor on this rail, and Flows 3–15 are configurations of it.

---

## 3. A bounded unit and the verify pass

One sentence: a unit is an `inline_target` descriptor scoped by coverage tag, which assembles a cited
slice, synthesizes a strict-JSON finding whose prose carries citation markers, then clears the mandatory
faithfulness pass and has its confidence folded at read time.

1. **A unit is just a descriptor on the Flow 2 rail.** It carries its own bounded question as an inline
   system prompt, a subscription predicate and time window, a rubric for the critic, and the verify key
   that opts it into being judged. A register-time drift guard fails loud if a unit is missing its rubric
   or its verify key, so a coverage gap cannot degrade silently at first run.
2. **Cadences stagger.** Each unit fires twice a day on a distinct hour pair, with a cooldown held below
   the interval, so no hour stacks the whole fan-out into one budget bucket.
3. **Assemble, ground, synthesize.** The run reads its recent signal slice, prepends the dated grounding
   preamble (Flow 10), and makes one completion call on the core plane. The payload carries the citation
   bridge — markers mapped to signal ids — that both the drill-down and the verify pass depend on, plus
   the pre-registered indicator block and any open questions. *(table: `analyst_outputs`)*
4. **The mandatory verify pass** runs after the finding lands, in two layers: the always-on deterministic
   citation floor, and the optional model judge resolved through the opt-in judge route. Flag off or
   judge unreachable degrades to the floor, labelled and published provisionally under a ceiling, never
   a fabricated number; a body that fails to segment publishes an unassessable state and no score.
5. **Persist as a critique and fold.** The verdict lands as a critique row; the read gate folds
   `effective_confidence = min(confidence, score)` at read time. This gates a visible low-confidence tier
   — a poorly supported finding is demoted and surfaced as such, never hard-deleted. Everything
   downstream reads the folded value. *(table: `analyst_outputs`, kind `critique`)*
6. **Drill to source.** The finding's receipt chain and citation bridge resolve hop by hop back to the
   real signal URL; the lineage route walks it, each node carrying its receipt hash and a re-computed
   consistency boolean. *(route: `GET /api/v1/lineage/finding/{id}`)*

---

## 4. The tower: record → voice → rollup → world

One sentence: a deterministic **record** quotes the verified heads of the tier below; a **voice** reads
that one record and writes the interpretation beside it; the region tier carries its members
arithmetically; and the world tier reads the country records directly.

1. **The desk record.** A composition descriptor with a subscription predicate fans out one worker per
   desk. Its read slice resolves the unit set, restricts to the running desk, and admits **only**
   verify-passed sub-claims above the floor through an inner join on the critique — so an unverified
   sub-claim never enters as assertable evidence. It writes one assembly row: a block per admitted head
   carrying that head's own lead span byte-for-byte under an ordinal, joined by a closed connective
   vocabulary, with no model call at all. Supersession keeps one live head per desk. *(table:
   `analyst_outputs`, `kind='finding'`)*
2. **The honest empty slice.** A desk whose units produced no verify-passed sub-claim yields an empty
   slice and an explicit nothing-to-synthesize row rather than an invented read. Lineage is stamped from
   the contributing unit ids, so the record back-walks one hop to the units and two to their signals.
3. **The correctness gate and the periphery.** A unit whose latest correctness row is missing, weak or
   single-family is *quoted* rather than composed over — marked into the same periphery tier the
   tiered-evidence flag uses, so there is one partition in the tree rather than two that can disagree.
   Consumption is stamped as it is decided. *(tables: `unit_correctness`, `output_consumption`)*
4. **The desk voice.** A second descriptor on the same kind reads exactly one record row, selected by its
   spine marker and its own target filter, and writes interpretive prose beside it. Its lineage must be
   exactly that one row or the run raises; its citation markers must resolve to real ordinals; its
   confidence is the record's. Its evidence map is the block's quoted span joined with the record's own
   arithmetic, so a sentence relaying the printed counters grades as supported. A deterministic pass then
   marks unsupported sentences with UTF-16 offsets and publishes the checked negative beside them.
5. **The region rollup.** The region descriptor resolves its frame to its member desks, reads their
   records, and emits an arithmetic rollup that carries each member's blocks byte-identically — origin
   still pointing at the desk head that wrote the sentence. No prose, so no faithfulness critique is
   written and none is faked; instead it declares its own arithmetic as structural claims, which the
   structural profile re-derives. A member with no record is named rather than dropped.
6. **The world tier.** The world record declares no subscription targets, so it is one global run. It
   reads the **country** records directly — because a rollup carries no faithfulness verdict and could
   never pass a floor-gated inner join — plus the thematic composition as its one legal cited
   cross-region object, and for each carried country that country's own voice in full as a labelled
   context span. The world voice then reads that one world record. Both tiers are staggered so the world
   record assembles after the cycle's country voices.
7. **The thematic compositions.** A thematic marker on the subscription block routes a global run to the
   cross-desk branch, fusing one *dimension* across the desk roster. A correlation guard detects cited
   heads whose lineage sets intersect, collapses each correlated cluster to one independent evidence
   unit, and caps the fused confidence — because sibling desk units can rest on the same wire signal.

---

## 5. The banded scorecard

One sentence: a deterministic global sweep enumerates every active desk and writes one banded row per
desk from high-precision admission rules over already-verified claims in a rolling window.

1. **One global sweep.** No subscription targets, so one run per tick reading directly through the pool,
   staggered after the units, the records and the verify pass so fresh verified claims exist in window.
2. **The banding rules are admission-only.** A dimension bands from the finding's standing-state severity
   tag; the folded confidence decides whether a claim may band at all and never which rung. A claim
   below the floor does not band; a per-claim faithfulness below its own floor is excluded with a
   legible reason. The rules never promote and never hand-weight a number.
3. **Movement does not move a band.** The delta tag rides beside the band and no rule reads it, because
   letting movement move a band would decay a steady standing state a rung a cycle. Each verdict stamps
   its banding semantics so cards written under different contracts stay comparable.
4. **Insufficient evidence is a first-class outcome.** A dimension with no qualifying claim reads
   insufficient with an empty-but-explicit basis and a machine reason; a desk with none at all still
   emits an all-insufficient row, so the read route returns exactly one honest card per active desk.
5. **Basis alignment.** Where the card would abstain beside a record that consumed a verified head for
   that desk, it resolves the record's lineage and bands the newest of those rows that passes the same
   unchanged guards — or abstains naming the consumed ids and the refusing rule. No guard is relaxed.
6. **Write and read.** Every band names the verified-claim id it rests on, and the row's lineage names
   those ids, so a walk resolves with no dangling reference. *(table: `analyst_outputs`,
   `kind='scorecard'`; route: `GET /api/v1/v3/eval/scorecard`)*

---

## 6. Fact extraction and supersession

One sentence: the fact-extractor stage is the inline tier's fact writer — it turns each in-flight signal
into atomic triples stamped as ingestion with an event-time validity, closes any prior open fact whose
value changed, and writes through the same contract the analyst path uses.

1. **The stage runs, degrade-not-drop.** It is an enrichment stage, so it always returns the signal and
   never raises: on any failure it logs, flips health to degraded, and returns. The per-source enrichment
   gate is the cost throttle.
2. **Extract triples.** The default backend reuses the relation triples the upstream NER stage already
   put on the payload, reconstructing each triple by pairing consecutive endpoints that share a
   predicate; absent those it calls the hosted extract endpoint itself. An opt-in model backend exists
   and raises rather than stubbing when it is selected without a handler.
3. **Filter the endpoints.** Both endpoints pass the shared numbers, dates and units rejection; an opted
   descriptor additionally drops a triple whose subject or value is entirely spelled-out quantities,
   which is the slice a synthesised confidence cannot be floored against.
4. **Resolve event time** through the same cursor precedence the source actor uses, always timezone-aware.
   A null validity start fails loud rather than collapsing to an epoch sentinel.
5. **Supersede, then upsert.** The prior open fact for the same subject and predicate whose **value
   differs** is closed — a same-value re-assert closes nothing — and the new open fact inserts on the
   open-only triple index, lifting confidence to the maximum and unioning lineage. This is the same write
   contract the analyst path uses, so the two producers agree. *(table: `facts`)*
6. **Decay maintains.** The decay readout computes per-fact aging into its sidecar and never mutates a
   fact; consumption is flag-gated. *(table: `fact_decay_states`)*

---

## 7. Nexus reification

One sentence: the reifier sweeps co-mentioned entity pairs, has a small model type each as a canonical
relation with a signed polarity, side-writes a first-class nexus per typed pair, and the deterministic
graph handlers then refine over the now-signed graph.

1. **One global sweep.** A meta analyst, so one run per tick; it side-writes nexus rows on its own
   connection and returns a finding as the per-run receipt.
2. **Read candidates.** Pending co-occurrence edges above a confidence floor that are not already
   reified into an open nexus, ordered by confidence and capped per run, each enriched with the pair's
   recent open facts as typing context. *(tables: `proposed_edges`, `facts`)*
3. **Type, budget-gated.** Per candidate the run checks the budget envelope — stop issuing new calls once
   exhausted, degrade rather than drop — then makes one typing call returning a single structured object.
   Unrelated pairs and off-list relation types are skipped, and the **authoritative polarity table**
   supplies the sign for a known relation type, falling back to the model's sign. That table is the same
   one the balance consumer owns: one canonical map, not two.
4. **Write the nexus.** The prior open nexus for the typed triple whose polarity **or** label differs is
   closed, then the new one inserts on the open-triple partial index, lifting confidence and unioning
   lineage. Validity starts at the pair's co-mention clock. *(table: `nexuses`)*
5. **Refine over the signed graph.** Structural balance pulls non-neutral open nexuses as canonical
   signed edges and classifies triads; graph mining adds directed signed edges so proxy-chain sign
   products can see a hostile-via-proxy path; nexus decay ages stale open rows. *(table: `graph_metrics`)*

---

## 8. Competing hypotheses

One sentence: for each focal situation the analyst reads the temporally current evidence base, has the
model propose mutually exclusive hypotheses each with a mandatory counter-thesis, scores a reproducible
evidence-by-hypothesis matrix with diagnosticity weighting, and side-writes one row per hypothesis.

1. **One global sweep**, side-writing hypothesis rows and returning a finding summary.
2. **Read the focal topics and current evidence.** Recent situations by intensity, not filtered on
   status — that gate is what starved the earlier lifecycle leg. Evidence assembles from linked findings,
   **current** facts under the open-row gate, and open signed nexuses overlapping the topic's entities;
   each item's id is a real substrate id, so lineage resolves. *(tables: `situations`, `analyst_outputs`,
   `facts`, `nexuses`)*
3. **Generate the competing set.** A budget-gated call returns the pairs, and the coercion step enforces
   at least two entries each with a non-empty thesis and counter-thesis. Any failure or budget pause
   falls back to a deterministic triad, so the matrix always gets built.
4. **Score the matrix.** Each cell is scored on the standard consistency scale by one batched call per
   topic through the analyst provider plane. Only when the budget is exhausted or the model is
   unavailable does the run fall back **per cell** to the transparent lexical and polarity scorer, and
   each row records which path ran. Diagnosticity then weights each item by its spread across the
   hypotheses, so evidence consistent with every hypothesis weighs nothing. The evidence base is scoped
   to the topic's resolved-entity set by exact membership rather than a substring match.
5. **Balance and transitions.** The per-hypothesis balance sums the diagnosticity-weighted sign of each
   diagnostic cell, rounded to an integer so it is robust to confidence gaming; passing the threshold in
   either direction transitions the status.
6. **Write one row per hypothesis**, reusing the existing table: the thesis and counter-thesis as hot
   columns, the supporting and refuting id sets, the balance, the status, and the full matrix in the
   diagnostic-evidence column. *(table: `hypotheses`)*
7. **Resolution.** The **exogenous** resolver runs first, grading each open hypothesis against facts
   produced *after* it and abstaining on undirected theses; an operator label outranks it; a terminal
   status with no exogenous resolution falls back to the self-consistency stamp. Calibration then reads
   the resolved outcome and **segregates** the two so one can never masquerade as the other.

---

## 9. A seeding import

One sentence: a registered seed adapter runs through the driver — fetch, map, resolve every entity
endpoint, write each fact and nexus stamped with its batch, record the ledger row — and re-import is an
upsert no-op.

1. **Invoke.** The seed script loads the pool and runs the named adapter; a dry run reports the
   would-write counts and touches nothing.
2. **Fetch and map** into typed seed entities, facts and nexuses. Only fetch and map differ between
   adapters; everything below is shared.
3. **Create the batch row first**, so the stamp on each fact and nexus is a valid reference; counts are
   filled in at the end. *(table: `seed_batches`)*
4. **Resolve entities.** Each endpoint upserts into the profile table on the canonical-surface conflict
   key — the exact contract the resolution handler uses — so a seeded entity and a live mention of the
   same name fold to one row. *(table: `entity_profiles`)*
5. **Write facts and nexuses**, each stamped with its source type and batch id, through the same
   provenance writers the analyst path uses. A per-record failure is logged and skipped rather than
   aborting the batch; a re-import lands on the open-only upsert and leaves the marker untouched.
6. **The curated adapter** maps each leader to a country-subject office fact keyed on the **country**,
   which is the supersession-correct shape: when a leader changes, the new fact closes the prior
   officeholder rather than leaving two open current rows.
7. **The live-upstream adapter** pulls current officeholders and bloc memberships over guarded requests,
   dropping end-dated statements and keeping the latest term start per office, with head of state and
   head of government on separate supersession keys. An identifier the label service cannot resolve is
   **dropped** rather than emitted as an unreadable value, and a leader with no parseable term start is
   skipped, because a fabricated validity start would poison decay and supersession. It emits the same
   canonical office predicate the curated adapter does, so a fresh pull cleanly supersedes a stale
   curated leader for the same country.
8. **Finalize.** The batch row's counts are updated and the run result returned.

---

## 10. A grounded read

One sentence: a unit opted into grounding runs a ground phase before its model call — the resolver reads
the current authoritative substrate facts about the desk and its slice entities, renders them into a
dated preamble, and the runner prepends it — so a stale-cutoff model reasons over current ground truth.

Grounding supersedes a stale prior on the way **in**; verify checks the output against its cites on the
way **out**. They are different mechanisms.

1. **Opt in at bind time.** Only when the descriptor's grounding block is enabled and a pool is wired
   does the builder construct the resolver and install the per-run hook. Off means no hook, and the run
   path is byte-for-byte unchanged.
2. **The phase sits after plan and before reason**, inside a guard: any failure logs and leaves the
   prompt untouched, because grounding is an enrichment and never fails a run.
3. **Collect candidates deterministically**, with no database read — the desk's own geography first, then
   the slice's top entities, de-duplicated, length-capped and junk-filtered.
4. **Resolve current facts and nexuses.** The query takes any candidate as subject under the
   current-facts gate — not superseded, and either open-ended or not yet expired — the same
   temporal-honesty gate the rest of the plane uses, ordered to prefer seeded and curated provenance so
   ground truth outranks a machine-extracted live fact, capped by the descriptor. Unreadable identifier
   values are excluded **in the query** so the budget is spent only on renderable facts, with a code
   backstop. A small leftover budget folds in current signed nexuses. *(tables: `facts`, `nexuses`)*
5. **Render the dated preamble**, one line per fact with its since-date, then the signed relationships.
   Nothing current to inject returns nothing, so no stray header is prepended.
6. **Prepend and reason.** The preamble is concatenated ahead of the rendered slice, a ground step is
   stamped into the trace, and the single completion call runs over the grounded prompt.
7. **The optional vector tier** retrieves from a curated corpus through the stack embedder under a
   relevance floor and country filter, degrade-not-drop, as a separate **non-citable** fenced block with
   no citation ids. A per-run rollback guard re-checks a disabled-unit list and a persisted state file on
   every run, so a rollback suppresses injection on the next run without a restart.

**It only corrects what the substrate holds.** A current fact absent from the seed or curated store
cannot be injected, and an exact-subject match means a name variant the slice uses but the facts table
does not key on simply will not resolve — degrading to no grounding, never a wrong fact.

---

## 11. A journal entry or a lens read

One sentence: a journal-kind analyst runs a single global run through an in-actor staged arc and writes
exactly one row into the dedicated journal table with an always-empty lineage — a perspective over the
provenance chain, never a member of it.

1. **One global run.** No subscription targets. The tier is the descriptor: the run selects its entry
   kind purely from its own analyst id, with no mode flag.
2. **The staged arc** runs plan, gather, field notes, narrate, reflect, honesty, with the persona
   re-loaded every phase. Gather is a bounded ReAct loop over the granted read pack; field notes is an
   in-voice cited handoff; narrate writes with tools still live under a small round cap. Reflect flags
   per-claim citations permissively, and honesty forces the flags **deterministically from substrate
   metrics** rather than from the agent's self-report.
3. **The off-chain write.** The run returns its payload with an empty lineage array, and the writer
   routes it to the dedicated table rather than the generic output table. The entry kind is the row
   discriminator; per-claim bindings live in the claims column and the flat cited-reference array — the
   up-only citation walk — and the supersession columns apply to the consolidation tier only, with a
   partial-unique index enforcing at most one open consolidation. *(table: `journal_entries`)*
4. **The verify pass fires on the cited fact claims** of the chronicle and lens tiers, perspective spans
   exempt, and the prose is never mutated. *(table: `analyst_outputs`, kind `critique`)*
5. **Propose and gate.** Anything outward — a correction, a change, or a revision of its own instructions
   — goes to the human-gated queue, never a live table. The accept endpoint claims the proposal before
   applying, so a replayed accept never double-applies, and the apply worker runs the change through the
   existing write and lifecycle paths. *(table: `journal_proposals`)*
6. **Read it.** *(routes: `GET /api/v1/journal`, `GET /api/v1/journal_proposals`, and the accept and
   reject endpoints)*

---

## 12. A consult, and a deep consult

**A consult** (blocking): an operator question invokes the consult actor, which runs a bounded reasoning
loop over read-only substrate tools and either returns the typed response in the envelope with no row
written, or persists a finding the endpoint reads back.

1. **The client posts** the question, scope predicate, round cap, mode, a client-minted request id and
   any prior turns. For the streaming mode it **subscribes to the step relay first**, then posts, so live
   steps published to the request-scoped subject relay as server-sent events; steps published before
   attach are lost by design. *(route: `POST /api/v1/consult`; stream:
   `GET /api/v1/consult/stream/{request_id}`)*
2. **The endpoint resolves the descriptor head**, builds the canonical actor id, and invokes the actor
   through the sidecar under a bounded timeout.
3. **The reasoning loop** runs to its round cap: a call, a parse, and either a final answer or a tool
   dispatch appended to the conversation. After the cap, one forced final turn always yields a
   structured answer. Client-held prior turns seed the conversation, so the server is stateless while
   the client is multi-turn.
4. **Every tool call is governed.** The whitelist is the read-only pack, routed through the one agency
   entry point and ledgered. Write-side tools are excluded by design — a consult is a read over
   substrate. *(tables: `action_pack_invocations`, `governor_events`)*
5. **The response payload** carries the answer, de-duplicated and hallucination-guarded cited references,
   an uncertainty figure, and the aspects it could not answer.
6. **Terminate by mode**, after the budget record and lineage resolution so both modes meter tokens and
   report lineage: the chat mode returns the payload in the envelope with **no row, no receipt chain, no
   emit bindings and no escalation**, publishing one terminal frame so the relay closes deterministically;
   the deep mode nests the payload in a finding the runtime writes and the endpoint reads back.
   *(tables: `analyst_outputs`, `consult_sessions`, `consult_turns`)*

**A deep consult** (detached): the endpoint invokes the actor, which **schedules** a durable workflow and
returns a task id in under a second rather than blocking.

1. **Submit.** The endpoint resolves the head, builds the actor id, mints a run id, and invokes under a
   short timeout because the actor returns immediately; the success envelope must carry the task id.
   *(route: `POST /api/v1/deep_consult`)*
2. **The actor short-circuits** on the kind, returning the task id **without writing a row** — the same
   guard shape as the chat short-circuit. The kind builds the workflow input, mints an instance id free
   of the reserved separator, and schedules without awaiting the result.
3. **The workflow** chains plan, acquire, analyze, synthesize, each a retried activity whose output is
   the next stage's input; a failed plan short-circuits to an empty result.
4. **The stages reuse existing primitives** rather than forking them: acquire runs the plan's tool calls
   against the **same** read-only substrate port as the consult, analyze runs under the same budget
   plane, and synthesize reuses the provenance writers verbatim.
5. **Poll for status.** The endpoint parses the run fragment out of the instance id and selects the
   produced finding row; present means completed, absent means running. The registry has no workflow
   channel — the finding row is the authoritative completion signal. *(route:
   `GET /api/v1/deep_consult/{task_id}`)*

---

## 13. The optimizer workflow

One sentence: the optimizer schedules a durable workflow from inside its actor run, which validates the
training set and then compiles a candidate prompt carrying a real before-and-after faithfulness delta;
promotion is operator-gated and the measurement gate binds even then.

1. **The cadence run** is a single global run; the read slice fetches trace and critique rows joined by
   trace id, which is why the critic must run first to produce graded rows. *(tables: `analyst_traces`,
   `analyst_outputs`, kind `critique`)*
2. **Build the input** — the analyst id and version, the parent prompt, the training set **by reference**,
   the search budget knobs, the promotion policy and the minimums — and dispatch.
3. **Schedule.** The client converts the input and schedules the workflow, returning a handle. The
   instance id must not contain the reserved separator, because result parsing splits on it.
4. **The orchestrator is deterministic** — no wall clock, randomness or I/O in the body, all
   non-determinism pushed into activities, so the engine can replay from history. Stage one validates the
   training set and stops on a shortfall; stage two compiles the candidate under a retry policy that
   applies to the activity and not the orchestrator.
5. **The search loop** loads the parent prompt, scores a baseline, runs the evolutionary search, and
   falls back to a deterministic candidate search. The same loop backs the in-process client, so
   behaviour is identical with or without a sidecar.
6. **Model routing never uses a third-party router.** A custom adapter routes every call through the
   platform's own provider handler, scoped on a background loop to avoid nested event-loop errors.
7. **Write the candidate** as a prompt-module candidate row carrying the paired delta, with the
   measurement gate stamping whether it is promotable at write time. *(table: `analyst_outputs`)*
8. **Promotion is operator-gated.** An operator flips the candidate's gate, and the live inference path
   admits the evolved prompt **only** when the measured delta is promotable — so a hand-flipped gate on a
   degenerate candidate resolves to the baseline.

---

## 14. Correctness and the external audit

**The reference, then the grade.** The builder and the grader are separate analysts on purpose: a grader
that could build its own reference could close the loop on itself. `REFERENCE_BUILDER.md` and
`CORRECTNESS_GRADER.md` carry each in full.

1. **The builder** takes one due desk per tick, ordered by cadence and backoff, and researches the window
   through the governed web pack under its own fences and wall clocks. It writes exactly one row — the
   developments across the banded dimensions, with a verified-span rate and a thin-dimension list.
   *(table: `unit_references`)*
2. **The grader** resolves the reference current at its as-of stamp; absent one it writes an explicit
   no-reference or stale-reference outcome and grades nothing. It segments the desk heads and the record
   above them with the **shipped judge segmenter**, so the claim population is the same one the judge
   sees, and mints one packet per unit under a leak scan.
3. **Three families vote** — one free, two paid and gated in series by a daily spend ceiling and by a
   calibration interlock that refuses to publish unless a passing calibration row matches the rubric hash
   and covers every model the run will use. Verdicts adjudicate by majority; a split is recorded as a
   split. *(tables: `unit_correctness`, `unit_correctness_claims`, `grader_calibrations`)*
4. **The gate consumes it.** A unit whose latest row is missing, weak or single-family is quoted rather
   than composed over at the next record run (Flow 4 step 3).

**The external audit** asks whether a published claim is true in the world.

5. **Refill.** Claims are enumerated from the record's spans and the voice's sentences as a *field*, at
   no model cost, each keyed on a stable hash of its folded text and origin span. The refill watermark
   advances only on successful enumeration and is written in the same row as the entries, so a crash
   between the two is not representable. *(table: `alert_trigger_watermarks`)*
6. **Drain** in a total order by severity, lead position, tier and key, within per-tick and per-day
   ceilings; exhausting a budget degrades to a hash-gated sampled mode whose fraction is stamped
   everywhere the numbers appear.
7. **Search, then grade.** The free rung runs every claim's primary query; a metered rung is reached only
   by explicit escalation, at most once per claim, and a refusal returns a reason rather than an empty
   result. An absence-shaped claim needs a *verified* empty — one where a live control probe proved the
   engine set was answering — or it is written unchecked with a liveness reason, never not-found and
   never supported.
8. **Check the span.** A decisive verdict requires a verbatim span resolving in a fetch of the cited
   page, folded through the shared text normaliser; a failed check leaves the verdict unchecked.
9. **Write.** One critique per audited claim, the external-truth ledger row, and — where a credible
   in-window source contradicts a high-severity published claim and a second family agrees — an alert row
   the auditor writes itself. *(tables: `external_grades`, `analyst_outputs`)*

---

## 15. The coherence loop

One sentence: lineage answers what a finding rested on; this answers whether anything has since arrived
that bears on what was left open — without ever letting a machine rewrite a settled claim.

1. **A question becomes durable.** Either the backfill reifies question-shaped state the substrate was
   already recording, or a unit payload's optional open-questions array converts post-persist, keyed on a
   hash of the question, with lineage set to the finding plus each resolved citation. Both dedup by
   containment on a durable marker, so either faucet is re-runnable. *(table: `hypotheses`, open-question
   status)*
2. **The researcher drains.** The backlog is declared as a grounding source, so a bounded, priority-
   ordered handful reaches the ground phase — ordered first by whether anything live still rests on the
   question, computed by a bounded forward walk over the consumption index. A resolved question may be
   tagged with its slot, resolved against the sink the ground phase filled, which becomes one bearing
   edge. It is a pointer and only a pointer. *(tables: `output_consumption`, `bearing_edges`)*
3. **The watcher matches.** New signals since a durable cursor are fused against the standing question
   set on three planes against one threshold, with meta-shaped questions dropped before the embed budget.
   *(cursor: `alert_trigger_watermarks`)*
4. **It writes pointers and flags.** A match lands a bearing edge; where the question traces forward to a
   product still live it also lands a review flag, and the run reports a staleness debt. Optional model
   legs can only subtract. *(tables: `bearing_edges`, `review_flags`)*
5. **Nothing closes a flag.** There is no code path that closes a review flag, writes correction content,
   or recomposes anything — true by construction of what the handler can write. The staleness debt is a
   flags-found, match-unverified count, and its route carries a hard-false verified field so no reader
   mistakes it for a settled number. *(route: `GET /api/v1/v3/system/staleness-debt`)*
6. **Gaps become objects.** In parallel and reading none of the above, the gap analyst drains the
   scorecard's starved cells and the source-request backlog into durable collection requirements, whose
   route is disposition-only. *(table: `collection_requirements`; route:
   `/api/v1/v3/collection-requirements`)*

---

## Entry points

| Concern | Module |
|---|---|
| Source poll and canonical write | `runtime/source_actor.py` |
| Per-signal baseline | `data/sources/baseline.py` |
| Enrichment filters | `data/filters/` |
| Signal subject and stream | `data/nats.py` |
| Subscription match | `runtime/subscription/filter.py` |
| Coalescing kernel, accumulator, engine | `runtime/triggers/` |
| Analyst actor: cadence, fan-out, per-target run | `runtime/dapr_actors.py` |
| Kind dispatch and deps | `runtime/analyst_deps_builder.py` |
| Typed output write and supersession | `data/provenance/writes.py` |
| Output kinds and routing | `data/provenance/kinds.py` |
| Faithfulness pass | `data/provenance/verify.py` |
| Judge route, transport, verdict parsing | `data/provenance/judge_*.py` |
| Forward consumption index | `data/provenance/consumption.py` |
| Bounded units | `data/analysts/inline_target.py` |
| Records, voices, rollups | `data/analysts/meta_findings_synthesizer.py`, `composition_slice.py`, `assembly_*.py`, `assessment_*.py`, `region_rollup.py` |
| Banded scorecard | `data/analysts/deterministic_handlers/scorecard_banding.py` |
| Correctness grader | `data/analysts/deterministic_handlers/correctness_grader.py` |
| Reference builder | `data/analysts/deterministic_handlers/reference_builder.py` |
| External audit | `data/analysts/deterministic_handlers/standing_auditor.py`, `_external_audit_*.py` |
| Alert loop | `data/analysts/deterministic_handlers/alert_trigger_scan.py` |
| Contested-claim arbiter | `data/analysts/deterministic_handlers/fact_contention_arbiter.py`, `fact_contention_pass.py` |
| Open-question watcher | `data/analysts/deterministic_handlers/claim_watch.py` |
| Journal, chronicle, lenses | `data/analysts/journal_assessor.py` |
| Situation trajectory | `data/analysts/situation_tracker.py` |
| Consult and deep consult | `data/analysts/consult_on_demand.py`, `deep_consult.py` |
| Workflows | `runtime/dapr_workflow/` |
| Grounding resolver and preamble | `runtime/grounding.py` |
| Agency: resolution, governor, entry point | `data/analysts/agency/` |
| Seed driver and adapters | `data/seed/` |
| Registry routes | `data/registry/` |
