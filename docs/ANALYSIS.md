<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Analysis

The analysis plane: how a matched signal becomes a coalesced analyst run, what each analyst kind reads
and writes, how an analyst is given governed agency over external tools, how the platform measures its
own output, and the analytical method the whole machine expresses.

Every mechanism below is described the same way: **what it reads, what it writes, how it is measured,
its cadence.** Cadences are the descriptor's `cadence.fallback_schedule`; whether a given descriptor is
live is a registry fact, and `RELEASE_STATE.md` carries the roster. Every threshold, budget, governor
cap and flag named here has its value in `TUNABLES.md` and nowhere else.

For acquisition see `ACQUISITION.md`; for the stores see `ARCHITECTURE.md` and `DATA_MODEL.md`; for the
models see `AI_MODELS.md`; for the correctness instrument see `CORRECTNESS_GRADER.md`; for the
references it grades against see `REFERENCE_BUILDER.md`.

**Contents:** [1 Where analysis sits](#1-where-analysis-sits) · [2 Coalescing triggers](#2-coalescing-triggers) ·
[3 The analyst kinds](#3-the-analyst-kinds) · [4 The reasoning spine](#4-the-reasoning-spine) ·
[5 The deterministic library](#5-the-deterministic-library) · [6 Situations and continuity](#6-situations-and-continuity) ·
[7 Standing open questions](#7-standing-open-questions) · [8 The journal and the voices](#8-the-journal-and-the-voices) ·
[9 Action-pack agency](#9-action-pack-agency) · [10 Measurement](#10-measurement) ·
[11 Contested claims](#11-contested-claims) · [12 Method](#12-method)

---

## 1. Where analysis sits

Legba is source-first. A source acquires an observation once, enriches it once, and publishes one
canonical signal that carries no target — observations are not interpretations. The fan-out plane
routes that signal to every desk whose predicate selects it (`ACQUISITION.md`).

The analysis plane begins where a matched signal reaches a desk's analysts. It has four moving parts:

1. **Coalescing triggers** — a matched signal, or a new upstream finding, marks an `(analyst, target)`
   pair dirty; the analyst fires when a gate trips, never once per signal (§2).
2. **Analyst kinds** — the cognitive units. Each reads a defined substrate slice, runs a method
   (LLM planner, statistical, ReAct loop or pure Python), and writes typed outputs with full
   provenance (§3).
3. **Action-pack agency** — the governed, allow-listed bundle of external tools an analyst may invoke,
   with a per-pack governor inside a global budget envelope (§9).
4. **Measurement** — a mandatory faithfulness pass over every cited claim, a correctness instrument
   graded against independently built references, and an external web audit of published claims (§10).

Those four are the mechanics. The **product** they assemble is a bottom-up reasoning spine (§4):
nine bounded units answer one narrow question each over a desk's slice; a deterministic record quotes their
verified conclusions upward; an interpretive voice reads that record and only that record; a
deterministic banded scorecard writes one honest verdict per desk; and an alert loop watches the
verified state for transitions worth an operator's attention. Every claim on the spine is cited to
source, scored for groundedness, and drillable through a receipt chain to the original signal.

---

## 2. Coalescing triggers

`runtime/triggers/`

An analyst does not run once per signal. Running a possibly LLM-bearing analyst on every matching
observation would fan out uncontrollably on a busy desk. A matched signal instead marks the
`(analyst, target)` pair dirty, and the analyst fires on whichever gate trips first, clamped by a
cooldown.

### 2.1 The gates and the clamp

`triggers/policy.py` is a pure decision kernel over a persisted accumulator and a trigger policy.
Evaluation order, first match wins:

1. **Cooldown clamp** — after a fire the pair is muted for the cooldown window. Inside it nothing
   fires, however much dirt accumulates. This is the thrash ceiling.
2. **Severity gate** — a single pending signal at or above the severity threshold wakes the analyst
   immediately, because a critical signal cannot wait for a batch to fill. The escape hatch.
3. **Accumulation gate** — the pending count reaching the threshold fires the batch, so a busy desk
   fires as soon as it has enough to chew on without waiting for the cadence tick.
4. **Cadence gate** — a periodic tick, but only when at least one signal is pending. The floor: a
   quiet desk whose signals never reach the accumulation threshold still gets re-evaluated on
   schedule, so a slow drip eventually fires.
5. Otherwise hold.

Severity ranks are an ordered ladder from info to critical; an unknown label ranks below all of them
and never wakes the severity gate.

### 2.2 The accumulator

The accumulator is persisted in Postgres, so trigger state survives a restart. It holds the pending
count, the highest pending severity rank, the last-fired and first-dirty timestamps, and a bounded set
of already-seen canonical signal ids. Five behaviours follow from the kernel: a critical signal fires
now; N accumulated signals fire together; fires are spaced at least a cooldown apart; a held signal is
never lost across a restart; and accumulation keys on a signal's **canonical** id, so two deliveries of
the same observation — via two sources, or a re-delivery — bump the counter once and the pair is not
re-woken.

### 2.3 Fire path and exactly-once dispatch

`triggers/coalescer.py` owns the I/O. On a matching signal it reads the accumulator, applies dirt, and
re-evaluates. On a fire decision it **claims the fire by compare-and-set** on the accumulator's
last-fired anchor: exactly one worker wins, so a pair that both the signal path and the cadence tick
decide to fire is dispatched once, and the loser observes a changed anchor and backs off. The claim
atomically resets the accumulator, so a handler crash does not re-fire the same batch.

A fire carries the **whole batch** accumulated since the last fire, never a single signal — that is the
coalescing guarantee.

### 2.4 The rule that keeps LLM cost off the arrival rate

Coalescing runs for deterministic and LLM-bearing analysts alike, and LLM analysts fire on the
accumulation and cadence gates rather than per signal. Two independent guards enforce it. The
**policy guard** treats any method kind other than the deterministic, forecaster and compile kinds as
LLM-bearing: its effective accumulation threshold is floored to a minimum batch, so even a declared
threshold of one cannot make an LLM fan out one call per signal, and its severity gate is disabled
unless the operator explicitly opts in. The **dispatch guard** makes the deterministic runner raise if
handed an LLM-bearing method kind at all.

### 2.5 The engine

`triggers/engine.py` binds one durable pull subscription onto the union of registered subject filters
over the shared signal stream, re-checks each delivered signal against the desk's full structured
filter and residual predicate (the coarse subject only narrowed delivery; the exact match is always
SQL plus Starlark), and feeds matches to the coalescer. A new upstream finding is a derived signal on
the same stream, so "a new upstream finding arrived" needs no special case. A cadence ticker drives the
periodic re-evaluation, and a delivered message is acknowledged once its dirty state is durably
persisted, so acknowledging a held signal never loses it.

---

## 3. The analyst kinds

`data/analysts/`

The runtime walks this package at startup and registers each kind by its `KIND_NAME`. Every kind module
exposes that name, an async `run_method(inputs, options, deps)`, and an `OUTPUT_KIND`; optionally a
`READ_SLICE` when it reads across desks or reads other analysts' outputs rather than the default signal
slice, and a `build_prompt_module` when the optimizer can compile it.

The walker fails loud. A module named in the declared list that fails to import or lacks the contract
raises rather than logging and continuing, and it collects every failure before raising, so one boot
names the whole blast radius. *Why:* a silent skip is exactly the shape that lets a rename disable a
production analyst without anyone noticing.

| Kind | Method | Reads | Writes |
|---|---|---|---|
| `inline_target` | LLM planner over one desk | that desk's signal slice plus a grounding preamble | finding |
| `cross_target_raw` | LLM planner over many desks | raw signals across the subscribed set | finding, marked cross-target |
| `meta_findings_synthesizer` | assembly or LLM planner | other analysts' verified findings, never raw signals | finding, marked meta |
| `cross_analyst_correlator` | LLM correlation detector | many analysts' outputs | finding tagged contradiction, agreement or blind spot |
| `relationship_reifier` | small-model relation typer | co-mention pairs plus open facts | a typed signed nexus, with a finding receipt |
| `competing_hypotheses` | LLM proposes and scores the matrix | focal situations × current facts × signed nexuses | hypotheses, with a finding receipt |
| `consult_on_demand` | bounded ReAct loop | a free-form question plus a scope predicate | a consult response |
| `deep_consult` | schedules a durable workflow | a free-form deep-research question | a finding, written by the workflow |
| `predictor` | statistical forecaster | a window of recent signals | a prediction |
| `deterministic` | pure-Python sub-handlers | substrate slices, the graph, time-series windows | structured findings plus substrate side-writes |
| `critic` | LLM judge against a rubric | one analyst output plus its rubric | a critique |
| `optimizer` | evolutionary prompt search in a workflow | an analyst's traces joined to its critiques | a prompt-module candidate |

Four more are **extension kinds**, registered by name through the vocabulary family rather than the
closed built-in enum, which is what makes the taxonomy genuinely open: `journal_assessor` (§8),
`entity_researcher`, `signal_salience`, and `situation_tracker` (§6). Adding a kind is one module plus
a descriptor registration; adding an *analyst* is usually just a descriptor.

**Output kinds.** `provenance/kinds.py` defines the closed set the write path routes on — finding,
situation, hypothesis, prediction, alert, meta-finding, critique, fact, nexus, prompt-module candidate,
journal, scorecard, situation update, and event. Journal rows land off the fact-finding-nexus chain
(§8); event rows are the v3 surface, gated off (`DATA_MODEL_V3.md`).

### 3.1 The archetype: how a descriptor becomes a runnable analyst

`inline_target` is worth tracing because every LLM-bearing finding kind follows its shape — a
declarative descriptor, a build-time wiring pass, and a pure run-time envelope.

**The descriptor declares** its `identity.kind` (the registry key the walker resolves to a module); a
`method.kind` the builder branches on, plus either an inline system prompt or a prompt-module reference;
a `method.llm` block whose primary is a stack reference resolved against the live stack, and whose
optional verify key is what opts the analyst into being judged (§10.2); an optional
`subscription.targets` block; a cadence block; an optional grounding block; any pack grants; any output
bindings; and a rubric for the critic.

**The presence of a `targets` block is the per-desk fan-out switch.** A descriptor that declares one
fans out one worker run per matched desk; one that omits it is a meta analyst and gets exactly one
global run. One field flips a kind between per-desk and global, and adding a desk is registering a
target with the right coverage tag — no code change, and no per-desk descriptor.

**The builder** resolves the stack reference into a live chat handler, resolves the effective system
prompt, applies any operator-promoted champion prompt (§10.8), installs the grounding hook when the
descriptor opts in, and constructs the runner.

**The run-time envelope** is deterministic around a single model call, so the optimizer can replay a
recorded trace: wake; orient (sort by relevance and pack the slice under the input-token budget — the
row cap is a backstop against a flood, not a fixed newest-N trim); plan; ground (prepend the dated
current-context preamble when the hook is wired, a no-op otherwise); reason (one completion call);
reflect (parse and validate into a payload — malformed output degrades to an unstructured finding rather
than crashing the run); narrate (stamp lineage, tags and key entities). The actor host performs the
write. The handler stays pure, with no I/O inside the kind, which is what makes replay deterministic.

**Cadence pairs with a cooldown held below the interval.** A cooldown stamped at run completion that
equals the interval lands past the next fire and silently halves the cadence; the per-target run adds a
slack band that absorbs drift when the two are close.

## 4. The reasoning spine

The kinds above are mechanics; this is how they compose into the product. The spine is built bottom-up
out of existing kinds configured by descriptor — no new Python kind was added for any tier — and
nothing rises to a higher altitude until it has been cited and scored at the lower one. That is the
spine's one rule: **authority climbs only as far as the verification underneath it reaches.**

The exemplar domain is geopolitics over the country desks, but nothing in the machinery is
desk-specific: the fan-out predicate and the unit prompts are configuration.

### 4.1 The bounded units

**Reads:** one desk's recent raw-signal slice, plus a grounding preamble of accumulated facts, signed
nexuses and graph structure (§12.5), so a unit integrates over accumulated state rather than only the
newest window. One unit additionally pulls guarded chunks from a curated vector corpus, and one pulls
the narrative sidecar. **Writes:** one strict-JSON finding whose prose carries `[N]` markers mapped to
signal ids, plus a machine-checkable indicator block of pre-registered warning signposts, plus an
optional open-questions array (§7). **Measured** by the mandatory faithfulness pass and, per unit, by
the correctness grader. **Cadence:** twice a day, staggered across the clock on distinct hour pairs so
no hour stacks the whole fan-out into one budget bucket.

Each of the **nine bounded units** is a topic-scoped `inline_target` descriptor answering **one narrow
question**. `leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`,
`internal_stability`, `military_posture` and `economic_coercion` fan out across every desk carrying a
country coverage tag. `proliferation_watch` is the same shape with a narrower predicate — the
nuclear-relevant desks only. `disruption_status` is tag-scoped off the country plane entirely, to the
thematic lane and flow desks, on a shorter window; it is the demonstration that "desk" means
*registered subject-frame*, not *country* — the unit needed no new kind and no new code path, only a
tag.

Every unit prompt carries the same **voice contract**: the read opens with an as-of line copied from
the printed slice header, so the prose cannot drift from the query that built it; stock template
sentences are banned in favour of a judgment shape; machine internals are barred from prose; and an
absence claim must be scoped to collection — "not observed in collected reporting", never a world fact.
The anchors are test-pinned per descriptor, so a prompt edit cannot silently drop them.

Skill is a **per-unit** number, never a platform boast. Adding a unit is a new descriptor.

### 4.2 The record

**Reads:** the verified findings of the tier below, admitted only above the faithfulness floor by an
inner join on the critique. **Writes:** one assembly row per desk — a block per admitted head, each
carrying that head's own lead span byte-for-byte under an ordinal, joined only by a closed connective
vocabulary in which no connective may assert a state of the world. **Measured** by deterministic arms
rather than a judge. **Cadence:** twice a day, after the units.

The record is a `meta_findings_synthesizer` descriptor in its **assembly regime**. One flag switches
the whole composition tower between the assembly regime and the older generative one, deliberately as a
single switch rather than one per tier, so the tower cannot be half-flipped; `TUNABLES.md` carries it.
In the assembly regime the record **quotes; it writes no sentence of its own.** Body and title render deterministically, confidence is the weakest carried
block, and no model call is made — the run stamps zero tokens and drops the response field, because a
field whose name is false is the small dishonesty the design is against. *Why a record that only
quotes:* a model writing the record can restate its inputs wrongly, and every tier above inherits the
error; a record that carries the span cannot.

Four deterministic arms audit it — quote fidelity, scope preservation, attribution equality, selection
honesty. They read the payload spans rather than the rendered body, and they are an audit rather than a
withhold: an arm reading below perfect is a broken constructor, not a bad finding.

A desk whose units produced no verify-passed sub-claim yields an empty slice, and the record says so
rather than inventing a read. Under the tiered-evidence flag the read splits into a verified **basis**
and a labelled, capped **periphery** of below-floor rows admissible only as hedged context; a clause
resting solely on periphery citations without hedged attribution draws a counted soft failure, so the
hedging contract is enforced at grade time rather than requested politely. Periphery ids join the
lineage, so the weak leg is visible rather than laundered. A **correctness gate** additionally demotes a
unit whose latest correctness row is missing, weak, or from a single grader family: it is *quoted*
rather than composed over — out of the load-bearing basis but still visible and lineage-linked, because
a desk silently vanishing would make a record over three units look identical to one over eight.

Each carried block also states **how many sources are actually behind it**. The attribution line already
printed an outlet count; a block resting on ONE source after wire folding now carries `[single-source]`
beside its ordinal, and the citation carries `single_source: true`. Two outlets running the same agency
dispatch — Asharq Al-Awsat and CNA ran one Reuters wire 46 days in 30 measured ones, in Title Case and
sentence case, which the content hash cannot see because the URLs differ — count as **one** source, and
the fold is stated (`N wire-folded`) rather than subtracted from the outlet count in silence. The
near-verbatim test is the one the slice renderer and dedupe tier 4 already use; the declared outlet→wire
relationships live in `descriptors/wire_map.yaml`, and a declared relationship only *relaxes* the
content bar — it never folds two items on its own. This is a published marker, not a gate: nothing is
refused, re-weighted or dropped, and a block whose origin cites findings rather than signals is UNKNOWN
and therefore unmarked rather than falsely cleared.

### 4.3 The voice

**Reads:** exactly one record row and nothing else. **Writes:** interpretive prose beside it, in its own
band, under its own analyst id, with its own citations. **Measured** by the same faithfulness pass,
always judged and never sampled out, against an evidence map built from the record. **Cadence:** twice a
day, staggered after the record it reads.

The record is the quotation; the voice is the reading. It is fenced to its record by a hard
postcondition: its lineage must be exactly that one spine row, or the run raises. Two more fences hold
the line — the prompt builder's only data argument is the payload, asserted by a test that parses the
prompt module and fails if any database or cross-analyst name appears in it; and citation markers must
resolve to real ordinals, with both an out-of-range ordinal and a body that cites nothing raising,
because a body that cites no block has no evidence map.

The voice's prompt lives in code rather than in the descriptor, so a voice change is a deploy and a diff
rather than a descriptor update no test can see. Its confidence is the record's, never its own, and it
never emits a severity tag — the absence is the enforcement, since severity reaches the column only
through the tag reader.

Its evidence map is the record's own page: per cited ordinal, the block's quoted span *and* the record's
arithmetic, joined under a visible separator. *Why both halves:* graded against the spans alone,
faithful sentences relaying the counters printed on the record failed as uncited.

The voice's badge carries a **fidelity-to-spine** figure that reads as explicitly *unmeasured* until
its arm lands — never zero, which on a badge reads as a measured failure.
The pass accepts judge marks as a declared seam for the fact arm, and no caller passes them yet, so the judge-only class stays not-run and the badge says so.

**Unsupported markers.** The voice is the one surface allowed to publish a flagged sentence rather than
drop it. A deterministic pass marks named classes — an unearned rank or superlative, a causal link the
record does not carry, an absence whose scope it does not license, prose about the record's own
machinery, a sentence carrying no ordinal at all, and two failures of the mandated gaps section: a
proper name that appears nowhere in the record, and an absence asserted over a whole *class of subject
matter* while naming none of the uncarried units the record handed over by name. Each mark carries
offsets **in UTF-16 code units**, because the reader slices the body it was served and that is the index
unit a browser slices in. The pass marks; it never gates and never edits. Alongside the marks it
publishes a **checked negative** — how many sentences it examined and which classes ran — because an
empty marker list that does not say what it checked means nothing.

*Marking is not grading.* The gaps section is also taken **off the scoped-absence slice route**, for the
same reason its guesses are marked rather than gated:
its negatives are about what this record did not carry, and the retained input slice is what the surface
*was shown*, so a row in the slice evidences that the surface saw the thing rather than refuting a claim
that it went uncarried. A live world read lost a hard fail to exactly that mismatch. Routing it out is
binding on the judge too — a claim the router takes off the slice route cannot be hard-failed on the
judge path either — so the defect publishes at the severity it earned and no higher.

### 4.4 The region tier

**Reads:** the member desk records of one region frame. **Writes:** a rollup — an arithmetic statement
over those members, carrying each member's block objects byte-identically, with origin still pointing
at the desk head that wrote the sentence. **Measured** by structural claims re-derived from the
constituent set the row itself recorded, not by a judge. **Cadence:** twice a day, after the desk
records.

The rollup carries; it does not re-quote. *Why:* quote fidelity at the region tier then becomes the
same depth-one test against the same desk head, with one implementation and no second quoting act to
get wrong — there is no new way to be unfaithful.

A deterministic rollup has no prose to grade, so no faithfulness critique is written for it and none is
faked. What replaces it is a real check: the rollup declares its own arithmetic — member count, members
carried, members missing, lineage — and the structural-claims profile re-derives every number. A rollup
that misstates its own member count is caught deterministically; one that cannot be re-derived is
marked unverifiable rather than passed.

**The honest loss is named.** A generative region read carried a cross-border sentence no single
country read contains. The rollup cannot write that sentence and does not pretend to: its connective
vocabulary may say which members were seen and may not say anything about the world. The synthesis
moves one floor up.

Because a rollup carries no faithfulness verdict, it can never be admitted through a floor-gated inner
join. That is a trap rather than a preference, and the fix is to remove the dependency: the world tier
reads the country records directly.

### 4.5 The world tier

**Reads:** the verified country records, plus the thematic composition as the one legal cited
cross-region object, and — for each carried country — that country's own voice read in full as a
context span, labelled by which match rule found it. **Writes:** the world record, and beside it the
world voice, under the same record-and-voice split as the desk tier. **Measured** the same way.
**Cadence:** twice a day, staggered so the world record assembles after the cycle's country voices.

A region with no present head degrades honestly to its member country records rather than being
silently dropped, and a region with neither is named as an unassessed gap in an appended coverage
block.

### 4.6 Thematic compositions

**Reads:** one *dimension* across every desk rather than every dimension of one desk — routed by a
thematic marker on the subscription block. **Writes:** one global cross-desk read. **Measured** by the
same faithfulness pass, with an additional correlation guard applied at grade time. **Cadence:** twice
a day.

`escalation_composition` fuses the escalation dimension across the whole desk roster. A **correlation
guard** protects against double-counting: sibling desk units can rest on the same underlying wire
signal, so the kind detects cited heads whose lineage sets intersect, collapses each correlated cluster
to one independent evidence unit, and caps the fused confidence at the de-duplicated ceiling. A desk
with no head is named in an appended coverage block rather than silently missing.

`escalation_dyad` is the same machinery pinned to one flashpoint pair. It reads the *escalation-unit*
heads rather than the desk records, and the reason is the guard: a desk record's lineage is per-unit
finding ids, disjoint between the two desks, which makes the guard inert; a unit head's lineage is
signal ids, and one cross-border incident lands as one signal row in both desks' slices, so the two
heads intersect and the guard bites. It is a lateral read and feeds nothing upward — re-injecting it
would double-count the same evidence up a path where the guard cannot see it.

### 4.7 The banded scorecard

**Reads:** already-verified claims for every active desk over a rolling window. **Writes:** exactly one
banded row per desk, every band naming the verified-claim id it rests on so a lineage walk resolves
with no dangling reference. **Measured** by band calibration (§10.7). **Cadence:** daily, staggered
after the units, the records and the verify pass so fresh verified claims exist in window. No model
call.

The banding rules are high-precision and **admission-only**: a claim's folded confidence decides
whether it may band at all, and never which rung. A claim below the floor does not band; a per-claim
faithfulness below a dedicated floor is excluded with its own legible reason. The rules never promote
and never hand-weight a number into a fabricated overall band.

The severity tag a band reads is the dimension's **standing state** — where it stands on the desk today
— not the severity of what moved in the unit's slice. Movement rides a separate delta tag, is carried
on the verdict beside the band, and no rule reads it: letting movement move a band would decay a steady
war a rung a cycle. Each verdict stamps its banding semantics so cards written under different
contracts stay comparable, and a head with no delta reads null rather than steady.

**Insufficient evidence is a first-class outcome.** A dimension with no qualifying verified claim reads
`insufficient-evidence` with an empty-but-explicit basis and a machine reason, never a fabricated band.
A desk with no qualifying claim at all still emits an all-insufficient row, so the read route returns
exactly one honest card per active desk.

**It may not abstain silently beside a record that read the desk.** The card and the prose admit rows
under the same floors in opposite order — the record puts the floor in the filter and the head-fold in
the ordering, the card folded heads first and applied floors after — which produced abstentions sitting
beside records that had consumed a verified head for the same desk. The card now resolves the record's
lineage, the rows the prose actually rests on, and bands the newest of them that passes the same
unchanged guards. No guard is relaxed: a consumed head that fails one is still refused, and the
dimension then abstains naming the consumed ids and the refusing rule, never a silent null. Each
dimension carries a basis-alignment state covering both directions, including the reverse case where
the card bands a row the prose never cited.

### 4.8 Indications and warning

Two deterministic analysts turn the units' structured output into a forward-looking watch layer, at no
model cost.

**`indicator_tracker`** reads the two most recent indicator-bearing findings per unit stream, diffs each
pre-registered indicator slug run over run, and writes a finding when a status **flips** — above all
when a signpost moves from not-observed to triggered. It marks the run trace-only when nothing flipped,
so an idempotent re-run never repeats a feed row. Cadence: a half-hourly heartbeat.

**`collection_gap`** reads the banded scorecard rows and the source-request backlog and ranks the
starved desk × dimension cells, naming why, how persistently, and which source classes would plausibly
feed them. It also writes durable collection requirements with deterministically matched candidate
sources (`ACQUISITION.md` §8). Cadence: monthly, with a daily sibling dispatching reference gaps
through the same writer.

### 4.9 Reading beyond the slice

Two `inline_target` descriptors read past the desk slice rather than within it.

**`corpus_researcher`** declares the open-question backlog as a grounding source, so it does not run a
backlog job — it reads a prioritised handful of standing questions into its ground phase and works them
alongside everything else. It holds the substrate-read, web-access and research packs, so it may reach
the open web under the governed path (§9). Cadence: twice a day.

**`cross_doc_corroborator`** is the independence adjudicator. It picks one significant checkable claim
from a global recent slice, reaches beyond it with vector search, corpus search and document reads, and
emits one cited finding verdicted corroborated by N independent sources, single-sourced, or
contradicted. Its rubric weights corroboration rigour highest, and its prompt names the vice
explicitly: counting one wire story under two mastheads as two independent confirmations. Cadence:
twice a day.

---

## 5. The deterministic library

`data/analysts/deterministic_handlers/`

These pure-Python handlers run under the `deterministic` kind, selected by the descriptor's
`sub_handler` option. No model call, so token usage is zero. Each shares one contract and emits a typed
payload whose data carries the structured result and a short human-readable body. Sub-handlers are
decoupled at import time; adding one is a new module plus a registry entry.

Deterministic analysts do not hallucinate, but they can miscount, so most carry a structural-verify
exemption badge while an opt-in subset declares **structural claims** — machine-checkable quantities
the verify pass re-derives from the finding's own lineage. The read-side badge upgrades from merely
*exempt* to *checked* only when claims existed, at least one was checkable, and nothing failed to
re-derive.

### 5.1 Keeping the entity graph current

**`entity_resolution`** reads the next batch of un-resolved signals, oldest first, and folds their
entity mentions into the graph: one profile per distinct mention deduped on a canonical surface, a
signal-to-entity provenance edge, and pairwise co-occurrence edges whose confidence accrues on repeat.
It stamps each processed signal so a backlog of zero-entity signals can never starve newly arriving
ones, and re-running is safe. Cadence: every ten minutes.

The dedup pre-lookup is alias- and article-aware and class-guarded: an article, case or alias variant is
rewritten onto an existing keeper's canonical surface before the dedup key, a fallback-elected keeper is
never class-mutated, a junk gate drops numeric, quantity and possessive surfaces, and class validation
corrects only high-confidence mistypes and never downgrades a confident person.

**`entity_researcher`** is the retroactive half: it consumes blocked candidate pairs, adjudicates the
gray band with the core-plane model, and records a verdict per pair. It **never merges** — adjudication
is side-effect-free so a bad verdict is caught by the eval harness before any graph mutation — and it
is deliberately conservative, defaulting to not-same or unsure and saying same only for a true surface
variant of one referent. Cadence: twice daily.

### 5.2 Linking duplicate observations

**`cross_source_dedup`** scans the shared raw pool for duplicates and **links** them — it never
collapses or deletes a raw row, so source-level evidence stays audit-grade. Exact content-hash matching
is mandatory and deterministic; semantic near-duplicate linking through the vector store is best-effort
and runs only when a client is injected. The canonical is chosen deterministically and points at
itself; the duplicate's canonical pointer is updated and an alias row lands. This is the link the
coalescer keys accumulation on (§2.2). Cadence: every fifteen minutes.

**`cross_source_coalesce`** is its substrate-wide sibling, closing the "same event, different source,
different wording, no shared hash" gap over one shared embedding collection. It has no non-vector
fallback, because exact-hash matching is the other analyst's job — so with its embedding service or
vector port absent it refuses loudly and writes zero aliases rather than fabricating links. Cadence:
every six hours.

### 5.3 The rest of the library

| Sub-handler | What it does |
|---|---|
| `graph_mining` | community detection, structural-balance triads and proxy-chain mining over the signed edge set. Its hostile-edge shortlist is vetted: canonical class-checked endpoints only, a genuine hostility relation type *and* negative polarity both required, a subject-attribution guard, and a per-edge quality score |
| `structural_balance` | signed-edge triadic balance over the relationship graph (§12.4) |
| `proposed_edge_governance` | promotes well-corroborated pending edges into neutral co-occurrence nexuses and rejects thin, stale ones; signed reification stays the reifier's job |
| `nexus_decay` / `fact_decay` | time-based confidence decay of reified relationships and facts. Fact decay is superseded in practice by the readout sidecar (§5.5) |
| `situation_clustering` | materialises situation frames by atomic upsert on a signature |
| `thematic_proposal` | proposes thematic frames for uncovered hot topics. Absence- and negation-framed reads are excluded from candidacy, and each slug derives from the stable situation signature so proposals dedup rather than piling up variants |
| `finding_supersession` | links near-duplicate findings so a newer one supersedes the prior, never a destructive delete |
| `anomaly_detection` | volume rate spikes, sentiment-shift scores and novel-entity emergence over bucketed windows |
| `adversarial_signals` | flags coordinated or manipulative signal patterns |
| `entity_gc` | garbage-collects stale and orphan entity profiles, and auto-pauses a source whose leading poll-outcome run is a long contiguous error streak |
| `hypothesis_lifecycle` | the earlier hypothesis maintenance leg, superseded in practice by the competing-hypotheses kind |
| `integrity_sweep` | cross-table invariant checks over the substrate |
| `composition_lineage_sweep` | resolves and repairs record lineage after supersession |
| `signal_summarizer` / `signal_embedder` / `corpus_indexer` | the corpus legs: full-body summaries, vector embeddings, and the search index |
| `reenrich_ner` / `reenrich_translation` | forward-only re-enrichment of signals the earlier coverage gates skipped |
| `corpus_retention` / `signals_retention` / `analyst_traces_retention` | thin shims over one shared sweep engine driven by the retention config table; the seeded policies disable the sweep, because deleting substrate data is an operator decision |
| `evidence_archiver` | fetches and content-addresses verified-cited evidence (`ACQUISITION.md` §6) |
| `research_measurement` | the research program's counters — pure SQL arithmetic over the research signals, each rate beside its n |

### 5.4 The alert loop

**`alert_trigger_scan`** reads verified state transitions and the production gauge, writes alert rows
whose lineage names the basis, and hands off to the sink dispatcher. It is measured by its own
watermarks and receipt counters, and it ticks every ten minutes.

The rule that makes alerting trustworthy: **a trigger fires on a verified state transition, never on a
level, and never twice.**

| Class | Fires when | The honesty rule it carries |
|---|---|---|
| band crossing | a scorecard band moved, either direction | bands rest on already-verified claims, so the class is verification-gated by construction; into or out of insufficient-evidence reads as evidence lost or gained |
| verified finding | a new high- or critical-severity finding cleared verification | superseded and structurally exempt rows excluded; a late-verify window means a verdict landing after the scan still alerts next pass rather than slipping through |
| contention flip | a contested-claim group's status or surfaced winner changed | gated on the same verified bar through a citing finding |
| baseline deviation | a desk's count exceeds its own statistical band *and* an absolute floor | rising-edge only — one alert per excursion, silently re-armed on the falling edge — and the floor is what keeps a quiet desk from producing noise |
| watchlist hit | an operator standing watch on an entity, topic or place matches | the entity leg is alias- and fold-resolved, so an unresolvable watch matches nothing rather than degrading to a text scan; a new watch never pages history |
| geo convergence | distinct source *families* converge on one place | degree cells are fed only by point-trustworthy geography and everything else bins at country granularity, so centroids cannot manufacture a convergence; several distinct families are required, so one outlet re-covering a story cannot fire it |
| production deficit | a producing loop delivered nothing against its own cadence and trailing history | the first class whose subject is Legba rather than the world; judgment is shared with the production-gauge route, so a threshold cannot mean one thing on the table and another on the phone |
| situation escalation | a verified escalation lands on the trajectory ledger past an intensity floor | the first class whose subject is a frame; fires once per ledger row |
| coverage floor | a desk's own salience-scored slice keeps naming a foreign polity none of its open frames names | the class whose subject is the gap between two of the engine's own artifacts; naming runs the shared alias and demonym maps in both directions, so coverage is recognised under any form and a similarly named neighbour stays foreign. Fires low, once per polity per desk |
| external contradiction | the auditor found a credible in-window source contradicting a high-severity published claim, and a second grader family agreed | the first class whose subject is one of our own reads being wrong about the *world* rather than unfaithful to its cites (§10.6) |

**Watermark semantics.** The first scan per class seeds silently, so activation never produces an alert
storm. Afterwards a candidate fires only when the live value differs from the stored watermark, and the
watermark advances **only after the alert row lands** — a rejected write retries next scan, so delivery
is at-least-once and a fired transition never re-fires. A per-desk cap folds the remainder into one
honest rollup whose members' watermarks still advance, and a missing dispatcher is recorded rather than
silently dropped.

### 5.5 The readout family

Five deterministic analysts share one honesty pattern: **derived, fully recomputable, wholesale
refreshed — never a mutation of the primary rows.** Each writes a sidecar and a readout finding, at no
model cost, daily.

- **`desk_baseline`** computes each desk's own statistical activity baseline — lags, rolling means,
  time since the last high-severity event, and land-neighbour spillover — with a Poisson-floored sigma
  so a near-zero desk cannot produce a degenerate band. The migration header, the analyst docstring and
  the summary finding all say the same thing: **this is not a forecast.** It is a falsifiable prior over
  our own collection counts, consumed by the baseline-deviation trigger.
- **`fact_decay_scan`** computes per-fact confidence decay into a sidecar and **never mutates a fact**,
  which a database test asserts. Corroborations are treated as sightings — a re-sighted fact's clock
  resets — and consumption is flag-gated, with the grounding query byte-identical while the flag is off.
- **`source_track_record`** computes each source's earned win-and-loss record over resolved
  contentions, smoothed and lower-bounded, filling the arbiter's earned-weight seam (§11).
- **`narrative_mapper`** reifies each contested-claim family into a narrative and builds the directed
  source-echo graph. Its posture rides verbatim in the migration header, the analyst docstring and every
  route envelope: **descriptive, not causal** — an edge says one source tends to publish the same
  contested claims after another, at this lag, never that one drives the other — and an empty systematic
  set is published as-is rather than dressed up as coordination.
- **`signal_salience`** scores each raw text signal for consequence on the free plane, stamping the
  signal rather than writing a finding. *Why it exists:* consequence did not exist as data anywhere —
  signals carry no magnitude, confidence measures support rather than consequence, and recency was the
  only ranking, which is how a tabloid frame outranked a head-of-state event. The model supplies the
  event class, actor rank and magnitude; **authority is stamped deterministically from the source's own
  class**, never model-chosen, which is the anti-tabloid-authority guard.

---

## 6. Situations and continuity

A **situation** is a first-class durable temporal frame, keyed by a signature and detected bottom-up.
Situations are the platform's deliberate stand-in for a table of occurrences: a timeline is assembled
from signals plus situation spans.

**`situation_tracker`** is the trajectory ledger's one writer. **Reads:** each open situation that
picked up new *verified* evidence since its own watermark, plus that situation's prior tracked state.
**Writes:** the answer twice — a graded situation-update finding carrying the claim through the full
faithfulness gate, and append-only ledger rows carrying the queryable trajectory. **Measured** by the
verify pass on the claim half and by the ledger's own constraints on the other. **Cadence:** hourly.

*Why one writer rather than a per-unit sidecar:* fan-out would put every unit in a race to describe the
same frame, with partial views and no single receipt saying whether the ledger got written this hour.
One writer means the run's counters *are* the ledger's health.

Three binding lessons are built into its shape. Every item the model sees is a real verified row
rendered with an ordinal, and the delta it emits is bound to the ordinals it named, so nothing enters
the turn that is not citable. A model asked what changed will always find something, so
*unchanged* is a first-class answer, the prompt says plainly that it is the expected answer most of the
time, and every other movement is structurally unable to exist without new evidence — both the Python
type and a database constraint reject it. And the ledger's clock is **evidence time**, the newest cited
item's timestamp, never run time; the one row that cites nothing is a dormancy checkpoint.

---

## 7. Standing open questions

Every other product here is an **answer**. A run ends, its finding lands, and whatever it could not
resolve evaporates with the prompt context. This makes the unresolved half durable: an **open question**
is a first-class queryable row that outlives the run that raised it, that later evidence can be matched
against, and that a researcher can be pointed at on its next natural pass.

Read the loop with one thing stated up front: **nothing in it closes a question.** Both organs only
ever *link* — an answer pointer or a review flag — and a human reads the result. That is a deliberate
stopping point, not an omission waiting on a bugfix.

### 7.1 The object and its faucets

An open question is a hypothesis row with an open-question status — the same table and shape as a
competing hypothesis, no migration and no new store. The hypothesis columns are reused rather than
extended: the thesis holds the question, the counter-thesis the competing reading, and the matrix
machinery is left at its defaults. What actually identifies the row is a durable marker object in its
diagnostic evidence, and every writer dedups by containment on that marker, so a faucet is re-runnable
without duplicating a question.

Two faucets fill the set. A **backfill** script reifies question-shaped state the substrate was already
recording — a dimension the scorecard banded insufficient while a live record cites a finding of that
same dimension (recomputed through the *same* pure reducers both read surfaces use, so the harvest
cannot drift from what the panel shows), a record whose input was later materially reversed, an open
finding graded under the floor (an *ungraded* finding is not harvested — no verdict is not a question
about the claim), an open contested-fact group, and a starved collection cell keyed without the finding
id so a gap persisting across sweeps stays one question. It is dry-run by default. The **per-finding
faucet** lets a unit payload carry a small optional array of open questions with the citation indices
that raise them; the prompt is explicit that an empty array is the correct answer when nothing is
genuinely unresolved. Conversion runs post-persist, keyed on a hash of the question, with lineage set to
the finding plus each resolved citation, and the whole conversion is wrapped degrade-not-break, so a
faucet failure can never fail the run that produced the finding.

### 7.2 The drain

The researcher reads a bounded, priority-ordered handful into its ground phase (§4.9). The ordering key
encodes what makes a question worth answering: first, whether anything live still rests on it — a
bounded forward walk over the consumption index computes each question's live reach, and a question
nothing depends on any more is genuinely lower priority; then harvest class in a fixed order; then desk
salience, age, and a stable tie-break.

If the researcher resolves one it may tag its finding with the question's slot, resolved against the
sink its ground phase filled, so a free-text hallucinated tag resolves to nothing. On persist that
becomes one bearing edge. **It is a pointer, and only a pointer** — the edge writer never touches the
destination question's status or content. What the link buys is that a reader can see what has been
said about the question since, not that the system has decided the matter.

### 7.3 The watcher

**`claim_watch`** asks the complementary question: not *go answer this* but *has anything arrived that
bears on it?* **Reads:** new signals since a durable cursor, crossed with the standing open-question set.
**Writes:** bearing edges, review flags where the question traces forward to a product still live, and a
per-run staleness-debt count. **Measured** by out-of-plane precision rounds. **Cadence:** half-hourly. It
rides the existing change-detection plane rather than adding another bespoke watcher, reusing the
watermark table for its cursor and the embedder's own wiring for its vectors.

For each (signal, question) pair it fuses three planes into one weight against a match threshold: a
**vector** plane contributing nothing below a measured similarity floor; an **entity** plane over
canonical merge-folded overlap with the question's lineage, each shared entity weighted by its
specificity; and a **geo** plane that is a tie-breaker and deliberately never a third of the budget.

The entity plane is where the matcher earns its version number, and the arithmetic is the honesty
argument. An entity carried by *most* of a desk's own questions is weak evidence that this signal bears
on this question, so its weight ramps down as its document frequency rises, computed from data already
loaded and held inert on desks with too few questions — you cannot estimate a document frequency from one
document. A second, signal-side frequency curve composes multiplicatively under one shared floor, so it
can only ever lower a weight. The consequence falls out of the numbers: geo alone, one shared entity, or
one entity plus geo all sit under the bar, so **mere desk co-membership can never constitute a match**,
by construction rather than by tuning.

Four classes of question that ask about *our own collection posture* rather than about the world are
dropped before the embed budget, the lineage walk and the specificity build — no news signal can bear on
them, and that is a measured fact. A contested-claim question is deliberately not in that set: a
contested claim genuinely is a question about the world.

Every edge stamps the **matcher version** that produced it, so edges written under a weaker rule stay
distinguishable rather than retroactively dignified — which is exactly what let a precision measurement
stratify one rule from another instead of pooling two rules into one meaningless number.

Two cursor honesties are the failure modes a naive poller hides. If the cursor falls too far behind the
stream head it skips ahead — but first computes the *exact* number of signals it is abandoning, bounded
by a probe with a clipped flag when the bound binds, and prints it in the receipt, because a watcher that
silently skips its own backlog reports a clean run while missing everything. And signals the embedder has
not yet covered are held at the tail rather than matched with a dead vector plane, so the cursor cannot
outrun the embedder and strand a run as seen-but-vectorless; three guards keep that hold from taking the
whole batch.

**What it refuses to write.** No correction content, no write-back to the flagged producer, no
recomposition — true by construction of what the handler can write rather than by a toggle, since there
is no correction path to disable. Optional model legs exist and can only *subtract*: a post-match bearing
gate may refuse an edge and a blocking confirm leg may drop a gate-passed one, and both stamp rather than
fail closed. The staleness debt is honestly a flags-found, match-unverified count, never a
corrected-or-closed metric; its read route computes the headline with the matcher's own SQL under a
byte-equality drift guard so route and receipt cannot diverge, and carries a hard-false verified field so
no reader mistakes it for a settled number. The closing half — inject a confirmed match into the
producer's next run, let supersession correct the record, close the flag — does not exist in the tree;
its precision bar has been met, so building it is a held operator decision rather than an unmet
measurement (`SEAMS.md`).

### 7.4 Forward lineage

All of the above needs one thing backward lineage cannot answer. `derived_from` walks backwards: what
did this rest on? The consumption index is the forward direction: what now rests on this? Consumption is
stamped at the point where it is **decided** — inside the record's own basis-and-periphery split, and at
the journal's rendered slice selection — and materialised on the same connection as the output write, so
a consumer row and its edges land together or not at all. The distinction it preserves is the one that
matters for triage: "new evidence contradicts something a live read is built on" and "…something a live
read mentioned as a caveat" are different-severity facts. The writer degrades rather than breaks.

---

## 8. The journal and the voices

Every other meta-analyst cuts one slice of the substrate. The journal is the one analyst pointed at the
**whole organism** — its own self, state and flow — narrating a coherent point of view *over* the rest of
the system rather than synthesising another finding about the world. Its thesis: poetry without evidence
is noise; evidence without perspective is just a log file.

**Off the fact-finding-nexus chain.** This is the most important framing. A journal row lands in a
dedicated table, never the generic output table, carries an always-empty lineage array, and its table is
deliberately absent from the lineage catalogue, so a walk from a fact, situation or nexus can never
surface a journal node. Its citations are direction-asymmetric: an up-only warrant the panel hydrates
into chips, never lineage a walk can descend into. A gating test enforces the never-writes-a-fact
invariant. The journal is a reflective layer above and across the chain, not a member of it.

**One kind, a roster of tiers — the tier is the descriptor.** Every tier declares the same kind and
selects its entry kind from its own analyst id, with no mode flag:

| Tier | Reads | Writes | Cadence |
|---|---|---|---|
| entry | the freshest window through its own self-instruments | an append-only entry | twice daily |
| consolidation | its prior consolidation plus recent entries | one forward-carried narrative superseding the prior open consolidation | daily |
| chronicle | the verified tower top | a detached third-person public-record entry, every factual assertion cited | weekly |
| lens ×4 | the verified tower top through **one declared falsifiable prior** each — trend, base rate, capability, intent | an interpretive read that asserts no new fact | weekly, staggered |
| lens diff | the four faculty reads | where they agree, split or outlie — it never merges them | weekly, after the four |

A lens's declared prior lives in its persona module, so the descriptor's content hash *is* the prior's
version: re-authoring a prior is a descriptor change with a visible hash change. A partial-unique index
guarantees at most one open consolidation.

**The stateful voice — a second kind on the same table.** Every tier above is *stateless*: it reads
today and forgets. An **inquiry** is not. It is its own analyst kind, writing the same journal row
(entry kind `inquiry`, or `crossroads` for the one fixed-mandate descriptor) through the same staged arc
and the same faithfulness gate, and it adds two things. First, a **brief**: free text on the descriptor,
under two thousand characters, rendered into the prompt verbatim, so an operator re-aims a standing
investigation with a descriptor PUT and no code edit. Second, a **ledger** — hypotheses, questions,
expectations and observations, carried across cycles, read at plan as the run's own state and written at
reflect. Three rules make that state honest rather than decorative: a hypothesis is refused unless it
carries a resolution test frozen at write, so it cannot be softened later; a question is refused unless
it says what it is about, through the same deictic guard the open-question faucet already uses; and a
reference the entry itself never cited is dropped rather than banked. Each refusal raises a deterministic
honesty flag on the entry. An unreadable ledger is never allowed to read as a fresh start — the run says
so and is flagged, because silently forgetting is the one failure a continuity voice cannot afford. It is
scored by **yield** — hypotheses tested, questions answered, observations a desk later carried — never by
correctness, so an inquiry that opens confident hypotheses it never tests scores as one that opened
nothing. Its grants are the widest of any voice (substrate read, the tower-top instruments, its own
ledger) and it has **no web at all, by construction**, which is precisely what makes reading that widely
safe. Questions it raises go through the existing open-question faucet when one is wired; it never gains
a spend path of its own.

**The engine.** An in-actor staged arc — plan, gather, field notes, narrate, reflect, honesty — with the
persona re-loaded every phase, because the worldview is the attention mechanism. Gather is a bounded
ReAct investigation over its read pack; the field-notes seam is an in-voice cited handoff rather than a
thin summary; narrate writes with tools still live under a small round cap. Reflect flags per-claim
citations permissively — flag, do not strip — and the honesty phase forces its flags **deterministically
from substrate metrics**, never trusting the agent's self-report. Both gather and voice run on the free
core plane, so the journal costs no billed spend.

**Two deterministic guards ride the shared finalisation, so the voice cannot fabricate numbers.** A
numeric-fabrication guard validates any whole-fleet source-health count the prose asserts against the
deterministic read; a mismatch flags the entry with the mismatches recorded — annotate, never rewrite, so
the fabrication stays visible — and a failed read degrades to no flag rather than a fake pass. And a lens
whose narration comes back empty gets exactly one fallback pass over the verified tower corpus; still
empty after that, it stays honestly empty, because content is never fabricated to fill a cycle.

**Packs and propose-and-gate.** The journal holds only a read pack and a propose pack, both
non-write-fact — the grant-layer backstop for the off-chain invariant. It writes only its own entries
directly. Everything outward — a correction, a change, or a revision of its own instructions, where
protected sections auto-reject — goes to a human-gated queue, never a live table, and the accept path
claims the proposal before applying so a replayed accept never double-applies. Its only un-gated effect
is its own continuity: it reads its own last entry and current consolidation into its next run. It can
write its own next breath but cannot rewrite its own rules without the operator. Routing an accepted
reflection back outward is designed, not live.

## 9. Action-pack agency

`data/analysts/agency/`

An analyst kind reasons; an **action pack** is how an analyst *acts* outside the substrate. A pack is a
modular, allow-listed, declarative bundle of tools, prompt fragments, rules and channels with its own
applicability and governor. `AGENCY_GATING_MODEL.md` carries the gating model in full; this section is
the analyst-facing half.

**What a pack declares:** an identity content-hashed at registration; tool specs each naming an
implementation and whether it enqueues an async job rather than running inline; prompt fragments and
rules merged into a granting analyst's context; emit channels; a governor of independent caps over a
budget account; and an applicability predicate compiled **at registration time**, so an un-compilable
predicate is rejected when it is registered rather than failing open when it is called.

**The effective-capability rule.** A pack may run for an `(analyst, target)` pair only inside the gated
intersection

```
analyst.action_packs  ∩  target.allowed_action_packs  ∩  pack.applicability
```

and then only if the governor admits the call under budget. Each leg fails independently, so an operator
can see which rail denied a call. The applicability gate **fails closed** — an un-evaluable predicate
denies, because a hard gate must never fail open — and per-binding governor overrides are
**tightening-only**, so an override can never loosen a pack's own cap.

**The one entry point** runs the whole fail-closed pipeline per call: resolve the three-way allow-list;
check the tool is named in the pack and has a registered handler; govern against the per-pack caps and
the global envelope; **record** an invocation row before dispatch, so the next call's window sees it;
dispatch; and settle the row's true outcome, cost, units and duration. Every decision, admit or block,
writes a governor event and publishes it. Governor checks are pre-call and conservative — this call's
forward-looking estimate is added, so a call that *would* cross a cap is blocked before it runs, and a
breach names the cap dimension, the limit and the observed value. When a per-analyst or fleet-wide budget
is hit, the actor auto-demotes rather than failing.

| Pack | Tools | Who holds it |
|---|---|---|
| `substrate_read` | the governed read surface: base primitives, corpus and document readers, investigative and graph readers, finished-intelligence readers, navigation readers — all read-only | consult, the researcher, the corroborator, the chronicle |
| `journal_read` | the tower-top readers plus the self-instruments — assessments, graph structure, balance, critic scores, calibration, run health, source health, budget status, the journal delta, the lens reads | the journal tiers and the lenses |
| `journal_propose` | the propose-and-gate write surface into the human-gated queue | the journal tiers only |
| `web_access` | web search and page fetch, read-only by contract | consult, the researcher, the auditor, the reference builders |
| `research` | one tool that lands ordinary evidence from the open web | the researcher only |
| `escalate_finding` | the escalation emit | the units and the desk record |
| `propose_facts` | propose a fact, request a source, open a question — all propose-and-gate | operator-gated |
| `media_processing` / `incident_response` / `discovery` | the async media job, the incident emit, source discovery | granted by descriptor |

A lens holds **only** the journal read pack: substrate read is deliberately omitted, because a lens
interprets the tower's verified conclusions rather than reaching past them into the raw corpus.

**The research pack is the one that lands evidence**, and it is separate from web access for a structural
reason: web access is read-only by contract and its budget account is shared by every caller, so a
research money cap could not live there. Its tool searches through the same resolved handler and guarded
transport as web search, then writes one signal row per kept hit — tagged with its retrieval origin,
capped at a credibility ceiling so it contributes no mass and can never crown a read, sourced to a
deliberately unregistered synthetic id per provider at the honest authority floor, and archived at one of
two depths the operator-owned host-licence ledger decides. Robots rules are honoured before every page
fetch, fail-closed on an unreachable robots file. A three-state flag gates the program: off means the
tool refuses loudly and the pre-research path is byte-identical; the middle state writes, archives and
measures rows while excluding them from every desk's reactive slice by one clause — the
poisoning-rollback rung; the third lifts the exclusion.

**It is live at two production sites.** The consult kind routes every reasoning-loop tool call through
its read binding, and the actor run path fires the escalation pack when a landed finding crosses the
pack's **post-verify** gate — severity read from a first-class column rather than parsed out of a tag,
and the score gated on the verify-demoted confidence rather than the model's own asserted number, so a
finding the verify pass floored cannot alert on its pre-verify figure and a high-severity tag alone is
not enough. Escalation lands on the bus; the external delivery edge past it is a declared follow-up.

## 10. Measurement

The platform grades its own output on three axes, and publishes the numbers: **faithfulness** to the
cited evidence at every layer, **correctness** against independently built references at the desk layer,
and an **external audit** of published claims against the open web. They are never pooled — they answer
different questions, and averaging them would destroy the only information each carries.

### 10.1 Traces and receipts

Every run records a trace row and extends a per-analyst SHA-256 receipt chain, each run linking to the
prior, hydrated from the analyst's last trace at first use, so a broken or reordered link is detectable
on a replay. The chain follows the analyst identity across descriptor versions. These receipts are
**chain-consistent, not signed** — they carry no cryptographic authorship or tamper-proofing; that
guarantee belongs only to the registry's separate signed audit log over descriptor mutations. Per-analyst
run timing is exposed read-only over the same rows.

**Every run leaves exactly one trace row** — output, side-write, trace-only or failure alike, with the
output reference empty when nothing was emitted. That is what makes "did this analyst run?" answerable
independently of "did it produce anything?", which is what the production-deficit trigger needs.

### 10.2 The faithfulness judge

**Reads:** a just-emitted finding's prose plus its resolved citation bridge — the unit convention
mapping `[N]` markers to signal ids, or the record convention mapping reference markers to sub-claims.
**Writes:** nothing itself; it returns a report the actor persists as a critique carrying the score,
which model judged, the route rung that resolved it, and a per-claim verdict ledger. **Measured** by
acceptance panels that gate every change to it. **Cadence:** a mandatory tail on every cited finding.

It measures **groundedness — does each claim follow from its cited evidence — not truth in the world.**
That distinction is the whole honesty thesis, and everything about the pass follows from it.

**Two layers.** A **deterministic floor**, always on, checks every fact-asserting claim's marker against
the resolved bridge: a claim asserting a fact with no marker, or a marker resolving to no real id, is an
unsupported span, and the score is the fraction of checkable claims supported. An **optional model
judge**, flag-gated, refines the per-claim verdicts. Flag off or judge unreachable **degrades to the
floor**, labelled as such and published provisionally under a ceiling — never a fabricated number. A
body whose claims fail to segment publishes an explicit unassessable state and no score, never a
perfect one.

**The judge route is opt-in, then repointable.** The ladder begins with a gate: an analyst whose model
block carries neither a judge nor a verify key gets **no judge route at all**, so nothing downstream can
conscript it into judging. Then an environment override, the explicit judge key, the verify key (where
every current descriptor lands), and the primary. Because the gate runs first, the override can retarget
judging but can never *enable* it. The descriptor default is the same model that produced the finding; a
deployment repoints every judge call cross-family, because same-model judging shares blind spots.

**Failures classify hard or soft.** Hard is the fabrication and entity-scramble family — a citation
resolving to nothing, a claim contradicted by its own cited source, a stale officeholder (kept as two
distinct reasons, curated-anchor and facts-reconciled, so calibration can tell them apart), a per-desk
finding naming only other countries, and an absence a row of the analyst's own retained slice reports.
Soft is the unsupported-inference and overclaim family — an uncited fact claim, a claim the judge could
not ground, hedge laundering, a clause resting only on periphery evidence, a claim whose only support is
the product's own register, double-counted lineage, an uncited triggered indicator, an unscoped absence,
and the demotions below. An unknown reason classifies soft, conservatively. A drift guard parses the
module, collects every reason literal it can emit, and asserts the table covers exactly that set in both
directions.

**A hard fail has to earn it.** Several classes demote an *unearned* hard fail while leaving the claim
failed: a contradicted verdict with no verbatim quote, a quote that restates rather than refutes, a
quote lifted from the prior read the claim explicitly diffs against, and a quote resolving to an analyst
finding the claim never cited. *Why:* a desk changing its mind on fresh reporting is the behaviour the
platform exists to produce, not a fault.

**The scoped-absence backstop** addresses what faithfulness is structurally blind to: a thin-collection
desk stating an absence as a **world fact** when what it established is that its own sources carried
nothing. The claim cites correctly, the judge grades it supported, and the reader is misled — a
well-grounded claim about a collection becomes a false claim about the world the instant it drops the
words "in what we collected". A pure lexical pass fires only on a strong absence assertion in the main
position, and only on spans the citation floor does not already count, so nothing is double-charged;
everything hedged, cited, forward-looking, survey-shaped or collection-scoped passes. When it fires it
adds one to the denominator and nothing to the numerator — a demotion, never a delete. It is a backstop
rather than a second opinion: on the judge-on path the judge's own absence rubric is authoritative.
Neither leg is a correctness check; both ask only whether the prose overclaims its scope.

**The fold.** The judge does not fold. The score lands on a critique row, and the substrate read gate
folds `effective_confidence = min(confidence, score)` at **read time**. Raw confidence, score and folded
value are stored side by side, so the down-weighting is auditable rather than destructive, and the fold
**never hard-deletes**: a poorly supported finding surfaces at a visible low-confidence tier. Everything
downstream — the record's inner join, the scorecard's admission, the escalation gate — reads the folded
value. Reads also stamp a below-floor flag, true for a graded finding under the floor and **null** for
an ungraded one: annotate, never a fabricated verdict.

Every critique stamps a **pipeline version**, so populations graded under different judges or rules never
pool. That stamp is what makes "the mean moved because we changed the rule" checkable rather than
asserted.

### 10.3 The critic

**Reads:** one analyst output plus the analysed analyst's rubric. **Writes:** a critique carrying
per-dimension scores, an overall confidence, and a revision delta the optimizer consumes as a
candidate-mutation hint. **Measured** by the same actuation gate as the judge. **Cadence:** every two
hours, fanning one bounded grade per ungraded finding row.

A **heterogeneity guard** requires the judge model to differ from the analysed model unless the analysed
descriptor explicitly opts in, because a model grading its own output is correlated noise rather than
signal; the kind raises rather than landing a self-graded row. A missing rubric is also a hard failure,
so the gap surfaces. Rubrics are operator-authored content — the same manual quality procedures an
analyst would apply by hand, encoded for the critic to apply at scale.

### 10.4 The correctness grader

Faithfulness measures groundedness; this measures whether a desk read was **true**.
`CORRECTNESS_GRADER.md` is the document for it — the two shares and the null-against-zero rule, the
rubric and its hash pin, the families and the external-grader fence, the spend ceiling and triage, the
calibration interlock and its re-gate procedure, the run order, the exclusion classes, target rotation,
reference currency, idempotence, the tables and the read surface. The shape belongs here:

**Reads** the independent reference current at its as-of stamp (§10.5), the desk unit heads and the
record above them. **Writes** one correctness row per unit at one as-of stamp plus a per-claim ledger
beneath it — the surface on which a disputed share is re-argued claim by claim; it writes no output row,
only a receipt. **Measured** by an instrument gate rather than by trust: it refuses to publish unless a
passing calibration row exists whose rubric hash matches and whose model set covers every model the run
will use. **Cadence:** hourly ticks under a wall-clock budget, with the roster ordered
least-recently-graded first, so a cap bounds the cost rather than deciding who is ever measured.

Three grader **families** vote: one free family on the self-hosted plane, which is the disclosed
producer-side family, and two paid families gated in series by a daily spend ceiling — at zero they are
never resolved, attempted or estimated, and every claim is written single-family — and by the
calibration interlock. Verdicts adjudicate by majority, and a split is recorded as a split rather than
resolved. Because the interlock keys on the model ids a run will use, repointing a paid family at a new
model makes it refuse until a calibration row covers that model — which is the gate working, not a
fault. A passing calibration row keyed to the live model ids of the paid families is required before they grade, and one exists on the reference deployment. Absent a current reference the grader writes an explicit no-reference or stale-reference
outcome and grades nothing. One naming trap worth stating once: the builder's **fences** and the
grader's **families** are unrelated vocabularies that both live under "correctness".

### 10.5 The reference builder

**Reads** the desk roster, its own attempt ledger off its receipts, and the open web through the
governed pack. **Writes** exactly one table — one independent reference per desk and window, carrying
the developments across the banded dimensions with a verified-span rate and a thin-dimension list.
**Measured** by that rate and by the commit walls. **Cadence:** hourly, one target per tick, due targets
first, with a backoff on repeated failure. `REFERENCE_BUILDER.md` carries the hybrid argument, the
fences, the stagger, the commit wall, the manifest, the carried notes and the run order.

The seam worth naming here is structural: **the grader does not build the reference it grades against,
and the builder writes nothing the grader writes** — a grader that could build its own reference could
close the loop on itself. The thin-dimension list is the contract between them: a dimension the builder
marked thin makes every claim on it likelier to read as silent, so low coverage there is the reference
under-claiming rather than the desk over-claiming. A separate daily analyst builds the
attention-measurement labels on a different grain and in a different table, stamped with a machine label
prefix both downstream readers exclude, so machine rows cannot leak into the operator gold axis.

### 10.6 The external audit

**Reads:** the published claim population as a *field* — the record's spans and the voice's sentences —
at no model cost, plus the open web through the governed pack. **Writes:** one critique per audited
claim, an alert row on a confirmed contradiction, and the external-truth ledger. **Measured** by a
double-graded sample and declared instrument-validity bands. **Cadence:** hourly.

It asks the axis faithfulness cannot: **is the claim true in the world?** Its verdict never reaches into
the faithfulness fold — supported and not-found both score as unaffected, and only a contradiction
scores against — because an audit that could silently lower a faithfulness number would make two
instruments into one.

**Width** is the flag that turns the daily hand-sized sweep into a full population drain. Each claim is
keyed on a stable hash of its folded text and origin span, and three populations — record spans, voice
sentences, legacy prose — stay strictly separate, because pooling them compares three different
producers.

**Rungs** are the search ladder. The free rung runs every claim's primary query; every rung after it is
metered and reached only by explicit escalation, at most one query per claim.
The metered rung is live: the pack declares it as a fallback provider under a daily cost brake, the auditor's ladder names it second, and the reformulated query is routed to it. A refusal at any rung
returns a **reason**, never an empty result set, because "we could not afford to look" and "we looked
and found nothing" must never share a shape.

**Absence claims** are the sharpest case. A claim shaped as a non-occurrence needs a status that
distinguishes an empty result from a *verified* empty — one where a live control probe proved the engine
set was answering at all. An unverified empty escalates a rung; still unverified, the claim is written
**unchecked** with a liveness reason rather than not-found, because not-found reads as "the search
answered", and never as supported. The row still records which family and component were asked and
leaves the served-by field unset, because nobody answered. The gate applies only to an unverified empty:
a search that returned hits goes to the grader under the claim-shape rule.

**The queue** rides the existing watermark partition rather than adding a table. It carries the pending
entries, the refill watermark (the newest already-enumerated row, advancing only on successful
enumeration and written in the same row as the entries, so a crash between the two is not
representable), the day's spend, the sample fraction and reason, the graded keys, and the per-claim
write attempts and dead letters — the last two deliberately not reset by the day rollover, because they
are write-reliability state rather than budget state. Drain priority is a total order by severity, lead
position, tier, then key. Exhausting a budget degrades to a hash-gated sampled mode whose fraction is
stamped on the heartbeat, on every ledger row, and on every published number.

### 10.6.1 The contrary-evidence pass

**Reads:** the same published claim population the external audit reads — the record's spans and
the voice's sentences, as a field, at no model cost. **Writes:** one contention record per material
claim. **Cadence:** daily. **Measured** by its own counter-query quality counters.

Every other retrieval on this platform points **at** the claim. The audit searches the claim's own
query; the contested-facts arbiter compares values already extracted; cross-finding contradiction
compares claims that already came in; competing hypotheses resolve against the evidence base we
happen to hold. Nothing formulated the **counter**-query. This pass is that act and nothing else:
for each material claim it writes down the strongest opposing proposition as a search query, runs
it, fetches what comes back, and records the outcome.

**It emits contention records, never verdicts.** A stance describes the retrieval — a page this
platform fetched and holds states the opposite, a page narrows the claim, nothing admissible came
back, the retrieval did not happen. It never describes the claim. A verdict column would make this
a third grader beside the faithfulness judge and the external audit, and three populations under
one name is how a number stops meaning anything. No model is asked whether a claim is true at any
point on this path.

**The counter-query has two legs and a gate between them.** Where the claim takes a side in the
calibrated polarity vocabulary the cross-finding detector already owns, its negation is not a
matter of opinion: the query is the claim's own subject plus the opposite state's words, no model
in the loop, replayable from the claim text a month later. Where it does not — most published
claims, because that vocabulary is deliberately narrow — one bounded core-plane call proposes the
query, and the prompt says three times that the reply is a query and never a verdict. Then the gate,
which is the part that matters: a query that carries no content word the claim does not already
carry is a **paraphrase**, and a paraphrase is not a weak counter-query but the absence of one
wearing its clothes — it would retrieve exactly the corroboration the audit already retrieves and
then be counted as a contrary pass that ran. It is refused in code before any search is spent, and
the count of new words is written to every row, so counter-query quality is a number to average
rather than an anxiety to assert.

**One sentence making two claims takes no negation.** A contrastive conjunction is the cheapest
signal of that shape, and the rule is there because the lane's own sampled audit caught the failure
live: a military-posture read saying a capability was "operational" was read as a statement about a
chokepoint, and the counter-query came back asking whether it was closed. Such a claim is not lost;
it goes to the model leg, behind the same gate.

**The stances are derived in code from the fetched text**, by the same closed polarity vocabulary,
against the claim's own position. A second rule catches the claims that vocabulary has no words
for — a page denying a proposition the claim asserts — and that rule has had no live calibration
behind it, so its records are served to the route and to the reader's claim chip, where a human
reads them with the counter-ref one click away, and are **withheld from compositions**. A false
contradiction rendered into a composition manufactures a disagreement for the fleet to write up,
which is what the cross-finding detector shipped zero pairs rather than risk. "Nothing found" is
the expected reading of a normal day.

**Rung 0 only.** The free rung carries every counter-query. The metered rung is declared and wired
and off — the descriptor ships it false, and while it is false the ladder is truncated to the free
rung in code, so adding a metered rung by descriptor edit cannot spend anything on its own. With it
on, a metered query is still refused unless the pack declares a daily cost cap, and every
escalation and every refusal is counted where an operator reads them.

**A counter-ref this platform cites is a page this platform holds.** Every ref went through the
audit's own fences — robots consulted before the call, a non-2xx refused, the text extracted, the
publication date discovered from the document and never invented — and carries the hash of the
bytes, under the same hash function the research plane uses. A search snippet never becomes a ref.
Nothing is written to the signal corpus: the pages are selected adversarially, by design, and
landing that selection in the corpus would move freshness, source health, salience and calibration
for every desk that reads it, silently and in one direction. A page already in the corpus is
linked, never re-created.

**THE FOUR FENCES, and the unfenced run that wrote them.** The pass's first live run was unfenced:
60 claims, 60 `web_search` + 142 `web_fetch`, 57 `none_found` — and **3 `contradicts`, all three
false**. An encyclopedia article on the Strait of Hormuz, metadata-dated 2018, "contradicting" a
claim about India's energy-security pressure; the *definition* of the DMZ, dated 2007, against a
claim about a land-mine explosion in it; and one think-tank piece from another month against
Russia's posture. That run's own search health explains all three — SearXNG reported
`unresponsive_engines` five times and the free rung's hits were dominated by wikipedia.org and
merriam-webster.com. **An encyclopedia article is what a search engine returns when it has
nothing.** Two of the three were polarity-derived, so the composition tension leg would have
rendered them as counter-evidence in the next India and Korea compositions — the exact failure the
tension leg ships zero pairs rather than risk. So a counter page has to clear four fences before it
can carry `contradicts`, and each one writes its own number onto the
row, because a fence nobody can average is an assertion:

| Fence | The rule | On the row |
|---|---|---|
| **F1 host class** | A reference / encyclopedia / dictionary host can never carry `contradicts` — it is not reporting, it has no publication date for anyone's window, and it is what a degraded rung returns. A host the platform already ingests keeps the class it was *registered* under (`source_descriptors`' own `reporting`/`analysis`/`official`/`state_media`); an unknown host **passes** and meets the other three. | `host_class` |
| **F2 dated, in window** | An undated page cannot contradict, and a dated one must fall inside the claim's own evidence window widened by `contention_ttl_hours`. A counter page from another year is `qualifies` **at most**. The gate is the reference builder's, imported: JSON-LD / meta / `<time>` / a dated URL, and never prose or a masthead. | `page_published_at` |
| **F3 subject in the matched sentence** | The polarity match must occur in a sentence that also carries the claim's subject tokens, at the rule's own floor (R2's two; three for the uncalibrated fallback). The count is published beside `query_novel_tokens` — one measures whether the *query* was about the claim, the other whether the *page* was. Row 1 scored exactly 2, so this fence alone would not have caught it, and the column says so. | `subject_overlap` |
| **F4 two independent pages** | One admissible page is `qualifies`; `contradicts` needs **two**, from two independent outlets, each past F1–F3. The outlet notion is the composition floor's own independence count (two mastheads running one dispatch are one source), imported rather than re-spelled. | `independent_pages` |

The four columns are **nullable and never backfilled** (migration 0222). A row written before the
fences measured none of them, and NULL is "not measured", not zero — a backfill would have to invent
a host class from a URL nobody re-fetched. The first run's three rows stay exactly as they are,
expired and inert; the pipeline stamp moved with the rules (`7a.1` → `7a.2`) so a fenced row and an
unfenced one can never be pooled. F4 is enforced three times on purpose — in the derivation, by a
`NOT VALID` schema check, and again in the composition reader's own SQL — because the composition is
the one surface where a false contradiction becomes prose the fleet writes up.

**Where a record surfaces.** A live, calibrated contradiction resting on two independent pages,
against a claim a composition is carrying, renders one hedged line in that composition's tension section, citing the counter page by
a plain ordinal into a counter-evidence list at the foot — not a citation marker, because the
record mints exactly one of those per carried block and that identity is load-bearing. The line
declares that two things pull apart and stops; deciding between them is a judgement this tier does
not make and a retrieval certainly does not. The judge grades that line the way it grades every
cited line, and cross-family holds by construction: the core plane produced the query, the judge is
a different family, and the pass itself grades nothing.

### 10.7 Calibration, band calibration, and the forecast scoreboard

Four deterministic analysts publish the platform's own honesty metrics, each reporting a no-skill,
insufficient-sample or abstain result rather than hiding it. None writes a table of its own except the
pilot rows; all return a receipt.

| Analyst | Reads | Writes | Cadence |
|---|---|---|---|
| `calibration_tracking` | resolved hypotheses | a **segregated** score — exogenous, graded against facts that arrived *after* the claim, kept apart from a self-consistency score from a status transition, with an insufficient-exogenous flag rather than letting one masquerade as the other | daily |
| `band_calibration_tracker` | scorecard band *transitions* | one resolvable claim per transition, graded at fixed horizons under a pinned resolution spec | daily |
| `forecast_scoreboard` | the binary-forecast pilot table | issued forward forecasts, exogenous resolutions, and a trace-only receipt | weekly |
| `unit_correctness_scorer` | the operator gold set, the diagnostic reference labels, and the faithfulness critiques | per-unit faithfulness beside two correctness axes that are **never pooled** — different sources, distinct fields, so an all-unresolvable week reads honestly empty rather than averaging to a number | daily |

**Band calibration's honesty line is structural, not editorial.** Bands are **not probabilities**, so no
proper score exists or *can* exist for this harness: the claims table has no probability column by
design, and the note rides every summary finding and the eval route. At the horizon the desk's then-live
band either held or moved further — all of which count as the change confirming — or reverted;
unresolvable claims are excluded from both denominators but reported, and a zero-denominator window
publishes an honest null rather than a rate. The leg answers "do our band changes stick?", which is a
different and honestly answerable question from "were we right?".

**The forecast scoreboard reimplements no math**: it issues one forward forecast per active desk, resolves
closed windows **exogenously** by the upstream event's own timestamp, and counts. A degenerate
probability vector **abstains** — zero rows — and the producer never bypasses that guard. Its only
persisted product is the pilot rows plus a trace-only receipt, never a finding or prediction on any trust
surface, and the numbers surface solely on the calibration scoreboard.

**The forecast ledger is sealed three ways.** First, the resolution test is **frozen as text at
mint**: every `acute_forecasts` row carries `resolution_test` — the event class, the exogenous
column/join the resolver counts on, the threshold and the window — so a row's falsifiable contract is
readable without the code that issued it (pre-column rows carry the same rule prefixed `retro:`).
Second, the **denominator is honest**: a row whose `window_end` plus the resolver's grace has passed
without a resolution is marked `resolved_by='unresolved:expired'` — distinct from `voided:`, still
retryable, and held in the scoreboard's `brier_all` at maximum penalty beside the answered-only
`brier_answered`, with `expired_count` and per-minter hypothesis open/resolved counts published beside
them (selection bias in *which* claims get resolved shows up there, not nowhere). Third, the **receipt
chain is timestamped externally**: the daily `receipt_anchor` handler Merkle-roots the latest
`analyst_traces.receipt_hash` per analyst and POSTs the 32-byte digest to two public OpenTimestamps
calendars, landing one `receipt_anchors` row per (day, calendar) — `submitted` with the calendar's proof
bytes, `pending` (retried next tick) when a calendar is unreachable. Verification is deliberately
**not** built in-tree (SEAMS #58): to verify a stored proof offline, pull `root_hash` + `proof` from
`receipt_anchors` for the day, write the proof bytes to a file (`proof.ots`), recompute the root over
that day's chain heads, and run `ots verify proof.ots <root-bytes>` — a confirmed Bitcoin attestation
proves the head set existed before the stamped time, nothing more.

**The gold set** grows a real correctness sample at sustainable cost: a small weekly stratified sample of
verified-only finding heads, one per unit for coverage, the fill chosen by rendezvous hashing so it is
deterministic for a given week across re-reads, with a parity rotation alternating high- and
low-faithfulness bands so the sample does not silently favour easy reads. The first read of a week
**pins** it, so the worksheet cannot shift under the labeller. Verdicts are a closed vocabulary, one per
finding, with a snapshot stored at label time so later supersession cannot orphan a verdict. One honest
limit belongs in the design: *labels come from outside the production plane* is **operational discipline,
not a code-enforced invariant** — the code records who labelled, with no constraint, no allow-list and no
route-side rejection. The property is real because of who runs the labelling, and auditable because every
row is stamped; it is not enforced. Read the stamp.

### 10.8 The optimizer

**Reads:** one measured unit's traces joined to its critiques. **Writes:** a prompt-module candidate
carrying a real before-and-after paired faithfulness delta. **Measured** by that delta, on the same judge
that gates the live findings: the parent arm is the unit's existing score, and the candidate arm
generates under the candidate prompt and re-verifies. **Cadence:** weekly.

The optimizer returns as a **scoped, measured experiment**, never an always-on monolith. A degenerate,
insufficiently paired, or non-positive delta is honest-null and can never promote — the measurement gate
stamps the candidate not promotable at write time. **The delta is the deliverable**, not the prompt.

**Promotion is human-gated end to end**; there is no auto-promotion path. An operator flips a candidate's
gate, and the live inference path admits the evolved prompt **only** when the measured delta is
promotable — so even a hand-flipped gate on a degenerate candidate resolves to the baseline.

It is the one analyst whose work needs a durable-workflow substrate rather than a single-shot actor,
because the search is a multi-hour deterministic outer loop driving non-deterministic activities. It runs
as a workflow on the same control plane that already runs the actors, so there is no separate
orchestration cluster. The deterministic body never does wall-clock, randomness or I/O directly — all
non-determinism lives in the activities, so the engine can replay from history — and it validates the
training set cheaply and fails fast before the expensive step. It passes the training set **by
reference**, so the serialized input stays well under the transport size cap. Its model access is
mediated by an adapter that routes every call through the platform's own provider handler, never a
third-party routing library.

### 10.9 Method versions

Every numeric instrument the platform publishes stamps a `method_version` — the revision of the
scale, rule set or resolver the number was computed under — so a diff across a method change reads as
an instrument revision, never a world move. It rides `data.method_version` on `analyst_outputs` rows,
`situations.data.method_version` on every situation write, and a `method_version` column on the
dedicated ledgers (`acute_forecasts`, `band_calibration_claims`, `grader_calibrations`). A row
written before the stamp existed carries NULL — the honest mark of the pre-version era, never a
guessed retro label. The Inspector and the scorecard panel show it as a small chip.

| Instrument | Version | Since | Covers |
|---|---|---|---|
| `scorecard_banding` | `scorecard_banding/2026-09.1` | 2026-09-24 | confidence/faithfulness floors, the band ladder, the severity→band map; `banding_semantics` / `damping_semantics` still answer WHICH contract, this answers WHICH revision |
| `situation_clustering` | `situation_clustering/2026-09.1` | 2026-09-24 | member-recency half-life, corroboration density scale, status/dormancy floors, persistence factor |
| `calibration_tracking` | `calibration_tracking/2026-09.1` | 2026-09-24 | bin count, rolling window, drift threshold, exogenous/self-consistency split, sample floors |
| `band_calibration_tracker` | `band_calibration_tracker/2026-09.1` | 2026-09-24 | resolution spec, the 14/28-day horizons, claimable directions, outcome vocabulary |
| `correctness_calibration` | `correctness_calibration/2026-09.1` | 2026-09-24 | draw size, the seeded draw, the superset-coverage gate |
| `forecast_acute` | `forecast_acute/2026-09.1` | 2026-09-24 | event class + sources, the rate model, epsilon/degeneracy clamps, resolver grace |
| `indicator_tracker` | `indicator_tracker/2026-09.1` | 2026-09-24 | the 30-day diff lookback, the two-most-recent-runs join per `(target_id, source analyst_id)`, the rule that a newly-introduced indicator id is NOT a flip, and the activation predicate |
| `desk_baseline` | `desk_baseline/2026-09.1` | 2026-09-24 | the estimator (trailing mean, median readout, the `sqrt(mean)` Poisson floor under `robust_sigma`), `MIN_ACTIVE_DAYS`, and the trigger constants it imports (`DEFAULT_BASELINE_DAYS`, `DEFAULT_N_SIGMA`, `MIN_CURRENT_*`) |
| `inquiry_yield` | `inquiry_yield/2026-09.1` | 2026-09-24 | the 7-day window, which ledger fields feed each counter, the "anticipated" (cited_refs vs. a later finding's derived_from) and "blind spot" (dispatched target with no read) cross-reference rules |
| `layer_divergence` | `layer_divergence/2026-09.1` | 2026-09-24 | the log-ratio's continuity constant, the median/MAD rolling baseline and its 1.4826 scale, the MAD floor, the two-day consecutive rule, the severity ladder, and the (layer, day) wire fold — a diff across any of these is an instrument revision, never a country changing |
| `crossroads_detectors` | `crossroads_detectors/2026-09.2` | 2026-09-24 | the four detectors' thresholds: a pattern needs the same entity or commodity across three desks' verified findings, a drift is three consecutive cycles one way on the scorecard band ladder while cited mass holds, a contradiction is opposite polarity on one subject with no contention held by the arbiter, a silence is a roster unit with no read for its cadence or a country no lens entry named | 2026-09.2 (same day): the scope line withholds a count for a read that FAILED (`findings UNMEASURED (list_findings read failed)`), so a failed read can no longer be mistaken for a measured zero

Retroactive boundaries — instrument changes that predate the stamp (a diff across one is a
revision boundary even without a version on the older row):

| Instrument | Era | What changed |
|---|---|---|
| `situation_clustering` | migration 0188 (2026-09) | the mega-frame split re-keyed `situation_signature` and re-based `intensity_score` by member share — a pre-split intensity is not comparable to a post-split one |
| `scorecard_banding` | the damper retirement (2026-08) | `damping_semantics` flipped `demote` → `off`; the per-card semantics stamps carry this boundary |
| `forecast_acute` | migration 0075 (2026-06) | the pre-clamp `{0,1}`-degenerate pilot batch was voided and is excluded from every Brier denominator |

### 10.9.1 Scale versions

A `method_version` says whether the same CODE produced two numbers. It does not say whether the two
numbers MEAN the same thing, and those are different questions that change on different days: a
method can be re-cut without moving the scale, and — as migration 0188 proved in August 2026 — a
scale can move with no code change at all. A reader comparing a situation intensity of 59 today with
59 in July was comparing nothing, and nothing on the row said so.

So every instrument that publishes a number a reader sees also stamps a `scale_version`: the
COMPARABILITY FRAME the number lives in. Two readings that share one are directly comparable; two
that do not are **not**, whatever their methods say. It is deliberately COARSER than the method
version, and it is named for the **quantity**, not the module (`intensity/2026-08`, not
`situation_clustering/…`), because a scale outlives the handler publishing onto it and two
instruments may share one. The era segment is the month the frame last moved.

It rides `data.scale_version` on `analyst_outputs` payloads and `situations.data.scale_version` on
every situation write, and a `scale_version` column on the ledgers that publish off their own tables
(`acute_forecasts`, `band_calibration_claims`, `desk_baselines` — migration 0219, which also gives
`desk_baselines` the `method_version` it never had). Un-backfilled: a row written before the stamp
carries NULL, and the reader surface renders that as **"unstamped (pre-2026-09)"**, never as a
version. Back-labelling would assert a frame that was never declared — and for `acute_forecasts` it
would assert that the voided pre-clamp batch was on the current probability scale, the precise
falsehood migration 0075 exists to prevent.

| Scale | Quantity | Published by | Era opened by |
|---|---|---|---|
| `intensity/2026-08` | `situations.intensity_score` — the frame's recency-weighted corroboration heat | `situation_clustering` | migration 0188 (2026-08-29), the mega-frame split: every stored intensity was re-based by the split frame's share of the members, so a pre-split 59 and a post-split 59 are readings on two different scales |
| `indicator_status/2026-07` | the I&W indicator status `triggered` / `not_observed` / `expired` and the reading of a transition between them — an ordinal vocabulary, not a continuous measure | `indicator_tracker` | S3-T1/T2 (2026-07-02) fixed the three-value vocabulary; it has not moved since. Adding a value, retiring one, or changing what a transition means moves the era and puts every stored flip before it on a different scale |
| `band_ladder/2026-08` | a transition on the scorecard band ladder, and the persistence rates measured over those transitions | `band_calibration_tracker` | the damper retirement (migration 0187, 2026-08-27): `damping_semantics` flipped `demote` → `off`, moving what a band means and therefore what a band-to-band transition claims. A rate may only be pooled across claims sharing this; the per-claim `semantics_migration` flag marks the individual rows that straddle the boundary |
| `acute_probability/2026-07` | `acute_forecasts.p` / `p_base` — a probability in the clamped OPEN interval `(P_EPSILON, 1-P_EPSILON)` | `forecast_acute` | the D9 epsilon-clamp, sealed by migration 0075 (2026-07-04). The clamp is what makes these numbers a scale rather than a set of assertions: an unclamped `{0,1}` certainty has no finite log-loss and is comparable with nothing, which is why the 19 pre-clamp rows were voided rather than re-graded |
| `desk_deviation/2026-07` | `desk_baselines` `expected` / `current` / the band / `deviation_sigma` — a count per 24h bucket and its distance from the trailing mean in robust sigmas | `desk_baseline` | the P3-7 CAST recipe (2026-07-27), which fixed what each metric counts (`signal_volume_24h`, `high_sev_findings_24h`) and the 24h bucket shape it counts into. Change either and a `current` of 40 before and after are two different measurements — the desk would look like it moved when only the ruler did |

A σ-distance or a persistence rate is comparable across desks only WITHIN one scale era. That is the
whole reason these numbers are readable at all, and the reason the chip sits next to every one of
them on the reader surface (`docs/UI.md`, `ScaleStamp`).

---

## 11. Contested claims

The supersession model is single-winner-by-recency within a source tier: the newest same-tier assertion
closes the prior. That is right for a genuine state change — a leader changes, the new officeholder fact
supersedes the old. It is the *wrong* model for **disagreement**: when two sources of equal standing
assert different current values for the same subject and predicate, last-writer-wins silently destroys
one side, and which side survives is an artifact of arrival order rather than of evidence.

The answer is a **detect-only arbiter** on one stance: *surface a winner from the source evidence, and
never destroy the loser.* It adjudicates which asserted value the substrate's own sources best support;
it does not decide which is true in the world, and it never injects a model's own world-knowledge.

**Reads:** open facts, bucketed by subject and predicate, with competing values fuzzy-clustered.
**Writes:** the contention sidecar plus three thin recomputable markers on the fact rows. **Measured** by
its own receipt counters and by the source track record it feeds. **Cadence:** hourly, trace-only.

**Coexistence.** Before anything can be arbitrated, both disputed values have to survive the write path.
Under the contention flag, a same-tier incoming value that is fuzzy-*distinct* from an open prior does
not close it — the two coexist open so the arbiter can group them. This is the one *behavioural* change
in the whole feature; with the flag off the path is the single-winner model byte for byte. Both fact
producers route through the same supersession function, so the rule is uniform.

**Fuzzy clustering** fires only on genuine disagreement, so it must not split a demonym from its country
nor merge two genuinely different proper nouns. Each raw value is canonicalised through the same
normalisation the entity resolver uses, then merged by normalised edit distance under a **tight**
threshold — close enough to absorb a typo or a spacing variant, far short of merging neighbours whose
names differ by one word. Junk triples are dropped through the existing extractor gates rather than
adjudicated.

**The score is multiplicative on purpose.** Each surviving cluster scores on four axes: a log-damped
count of **distinct backing sources**, keyed on lineage rather than rows so one chatty source cannot
manufacture quorum; that value's share of the group's **credibility** mass, null-safe so an unscored
source abstains rather than zeroing the value; an exponential **recency** half-life — one bounded factor
among four rather than the sole decider, which is the structural fix against last-writer-wins; and the
mean asserted **confidence**. A zero on any axis kills the value, so a single recent assertion from one
unknown source does not win on recency alone.

**The abstain gate.** At most one winner per group, and only when it has earned it. A weak best cluster,
or a near-tie the best does not clear by a dominance ratio, records an honest *disputed, no resolution*
rather than a forced pick.

**Detect-only is the hard invariant.** The arbiter never mutates a fact: it touches none of the validity,
supersession, value or confidence columns and never calls the supersession path. Its entire output is the
sidecar plus the markers, and the whole contention state is derivable from the open facts — so it is a
read-over-and-annotate layer, structurally incapable of destroying evidence.

**The tail — from detection to accountable surfacing.** A tie-break waits out a soak window, so a fresh
dispute is not adjudicated on arrival-order evidence; a decisively scored winner is not soak-gated,
because the deterministic score already earned it. A weighted tie-break may then break a near-tie on
distinct-source count, source-type diversity and credibility mass, but only past soak, only with at least
two distinct sources on the winning side, and only past a dominance ratio. An **earned** term ships behind
a flag defaulting to zero, at which the path is byte-identical; when enabled, per-source weights come live
from the track-record readout, computed at a lag and with a **self-exclusion guard** — the contention
being decided is excluded from the record that decides it, so the loop cannot feed itself.

**The optional model tie-break** is the one case the deterministic score genuinely cannot settle. Only
there, only past soak, only under its own flag, and bounded hard: the self-hosted plane only — the builder
hard-refuses a billed primary, so a mis-wired descriptor can never route the paid plane into a fact
dispute — with a small token and time budget, a per-pass call cap, and a degrade-to-abstain on any
failure. The verdict is **cached** per contention and evidence fingerprint, so the same evidence is never
re-asked, and a transport failure degrades uncached, so only genuine verdicts persist. It is still
detect-only: a model-chosen winner surfaces through the same sidecar path and never touches a fact, and
the receipt splits consultations attempted from picks made so spend and effect are separately auditable.
**The stance the feature defends is in the question asked:** which of these source-asserted values is
better supported by this evidence — never, what is the true value.

**Surfacing.** A surfaced winner is stamped with who surfaced it, when, and a human-readable rationale,
every change appending the prior record to a capped history. The decision is recomputed each pass over an
evidence fingerprint, so new evidence re-opens the dispute while the prior surface survives in history —
and that moving pair is exactly what the contention-flip alert class fingerprints. A grounding-eligible
fact is annotated so the analyst never reads a disputed value as settled, and a contested value is offered
as an evidence item into the hypothesis matrix so a live dispute is visible as evidence.

**Pass planning.** Recomputing every group every hour is the correctness story — the sidecar is exactly
recomputable and a stale verdict cannot stick — but almost nothing changes between passes. So the pass
asks, per group and before any clustering, whether this group's stored answer *could* have changed: a
fingerprint hashes exactly the inputs the decision reads, and a group whose fingerprint matches what is
stored is skipped whole. It still counts toward the receipt and still registers as live, so the
stale-collapse sweep cannot mistake a skipped group for a vanished one. *Why the coarse age bucket in
that fingerprint is not a fudge:* the ratio between two clusters' recency factors is independent of the
current time, so the dominance gate and the tie-break cannot flip through the passage of time alone; only
the absolute floor is time-sensitive, and a group crosses that at most once, slowly.

## 12. Method

The method is model-agnostic — it describes analytical *approaches*. Each maps onto a specific kind, so
the theory and the runtime are one machine.

### 12.1 Fusion levels

Analysis follows the Joint Directors of Laboratories data-fusion model, adapted for open-source
intelligence. The levels are the *analytical* architecture; the planes are the *processing* architecture
— orthogonal, with each plane contributing to several levels.

| Level | Name | In Legba | Owned mainly by |
|---|---|---|---|
| L0 | signal refinement | normalisation, dedup, quality scoring | the source baseline and the dedup analysts |
| L1 | entity assessment | extract and disambiguate entities, build vertices | the NER stage, `entity_resolution`, `entity_researcher` |
| L2 | situation assessment | cluster into situations, build edges | `situation_clustering`, `graph_mining`, the reifier |
| L3 | impact assessment | competing explanations, correlation, the desk read | the units, `competing_hypotheses`, the correlator |
| L4 | process refinement | calibration, adversarial detection, the eval loop | the calibration legs, the judge, the grader, the audit |
| L5 | user refinement | human-consumable products, on-demand answers | the records and voices, the scorecard, consult |

The matrix is a lens for *where a new capability slots in*, not a directory structure: deterministic work
becomes a sub-handler, full judgement becomes an LLM-planner kind.

### 12.2 The confidence architecture

Confidence is multi-level — each analytical object has its own semantics, and every component is stored
for auditability.

Signal composite confidence uses a hybrid gatekeeper formula: a multiplicative **gate** of source
reliability times classification confidence, times a weighted **modifier** over temporal freshness,
corroboration and specificity. The multiplicative gate means an unreliable source or a poorly classified
signal can never produce high confidence regardless of freshness or corroboration; the modifier captures
the operational quality of the specific signal. Fact confidence **decays** when a fact receives no new
corroboration, computed as a readout beside the facts and consumed behind a flag rather than written into
them (§5.5). Finding confidence is folded against its grade at read time (§10.2); record confidence is
the weakest block it carries (§4.2); rollup confidence is the carried fraction of the roster and is
explicitly *not* an evidence belief (§4.4).

### 12.3 Hypotheses and competing explanations

Analysis of competing hypotheses is enforced as **thesis and counter-thesis pairs** — the platform will
not accept a hypothesis without a competing explanation, which forces consideration of alternatives from
the moment one is created. Each hypothesis carries diagnostic evidence (observations that would prove one
branch and disprove the other) and a signed evidence balance. New hypotheses are deduped against active
ones so the same claim is not created twice.

The model proposes the hypothesis *set* and scores each matrix cell on the standard consistency scale,
one batched call per topic through the analyst provider plane, budget-gated. When the budget is exhausted
or the model is unavailable the run falls back **per cell** to a transparent lexical and polarity scorer,
and each hypothesis row records which path ran — so a reader can always tell a semantically scored matrix
from a keyword-scored one. Diagnosticity weights each item by its spread across the hypotheses, so
evidence consistent with *every* hypothesis weighs nothing; that is the core of the method. The evidence
base is scoped to the topic's **resolved-entity set** — exact membership over canonical names rather than
a substring match, so a country name no longer false-matches an unrelated organisation. Status transitions
fire on the diagnosticity-weighted integer balance passing a threshold in either direction.

Resolution runs the **exogenous** resolver first — grading each open hypothesis against facts produced
*after* it — before the status-transition fallback, and an operator label outranks both. The exogenous
resolver is a coarse directional heuristic and **abstains on undirected theses**, because status-quo
claims were auto-grading true and inflating the headline rate. No proven-forecast-accuracy claim is made.

### 12.4 The knowledge graph and structural balance

The knowledge graph is **relational**. Vertices are entity profiles; the operative edge set is the nexus
table and the id-keyed edge store; provenance edges link signals to entities; proposed relationships
accrue as candidates. Graph compute runs in process rather than in a graph engine. A separate graph
extension exists inside the same database with its labels registered, but it has never held a production
row and both read legs degrade to empty; the path route returns an explicit unpopulated error rather than
a confident "no path" over an empty graph. `AGE_PROBE_REPORT.md` carries the measurements and
`DATA_MODEL_V3.md` the engine decision.

**Structural balance** classifies every triad of connected entities by the product of its edge signs.
Three positives, or one positive and two negatives, is balanced — a stable bloc, or a shared adversary
creating an alliance. An odd number of negatives is unbalanced: a structurally unstable configuration
that gives analytical lead time on a realignment. The balance score is tracked over time, and graph
entropy over the relationship-type distribution surfaces when the landscape is actively reorganising.
Relationship edges carry temporal properties and their changes are event-sourced, producing the
time series those metrics read. Advanced temporal-graph analytics are designed for and not running at
current scale.

### 12.5 Knowledge grounding

A stale-cutoff problem is intrinsic to the analyst plane: the core model's training cutoff predates the
present, so it backfills *current* world facts — who holds office, which alliances are in force, the
state of an ongoing conflict — from a prior that may be wrong, and the signal slice rarely restates such
background facts. Grounding is the fix, and it reuses the substrate rather than adding a store.

**The substrate is the grounding store.** Temporal facts and signed nexuses, with the curated and
live-upstream seed roots, hold the temporally honest answer to who holds office *now*. Grounding is two
halves: curate the current data in (`ACQUISITION.md` §7), and inject it at analysis time.

**At analysis time**, a descriptor opts in through a grounding block, off by default. The builder
installs a per-run hook; the ground phase calls it, and the resolver collects candidate names
deterministically from the target geography and the slice's top entities, queries the substrate for the
**current** authoritative facts under the same temporal-honesty gate the rest of the plane uses, prefers
seeded and curated provenance so ground truth outranks a machine-extracted live fact, folds in a few
current signed nexuses, and renders a dated preamble the run prepends to the prompt.

Three honesty properties are built in. It **skips unreadable identifier values** in both the query and a
code backstop, so it never injects a bare entity id. It is **degrade-not-drop**: any read failure, or a
thin slice that resolves nothing, yields no preamble and the run proceeds ungrounded, because grounding
is an enrichment and never a gate. And it is **opt-in and capped**, so an analyst that does not declare
the block is untouched.

A second tier retrieves from a curated unstructured corpus through the stack embedder under a relevance
floor and a country filter, degrade-not-drop, and is deliberately a **measured pilot** rather than a
shipped default: firing it has historically thickened the low-faithfulness tail even with a non-citable
header. A per-run auto-rollback guard re-checks a disabled-unit list and a persisted state file on every
run, so a rollback suppresses injection on the *next* run without a restart, triggered on a faithfulness
drop, a low-faith ratio, or a token-cost rise. Per-run trace instrumentation records the retrieval scores
so the measurement is honest, the injected priors stay **non-citable** (a fenced background block with no
citation ids), and it is enabled per unit, staggered and review-gated.

### 12.6 Forecasting

Forecasting is the most-hedged surface here, deliberately: a declared experimental seam that ships a
methodologically real harness and makes **no validated skill claim**.

The forecast-as-claim leg is retired, because a numeric forecast is a *claim* and the rule is measure and
verify before a forecast ships as product. What remains is a **rare-event** model over a frozen class of
upstream event sources, treated as a Poisson process with a rate estimated from the historical record,
forecasting the tail probability of at least one severe hazard in a forward window — a sharp binary
question.

**The verification theory is what makes it falsifiable.** Binary probabilistic forecasts are graded with
a **strictly proper** score, so a forecaster minimises it only by reporting its true probabilities, and
that score decomposes into reliability and resolution. Accuracy alone is meaningless for rare events —
always predicting "no" scores well — so the pilot reports **skill against a reference**, the realised
climatological frequency. The claim under test is not "we are accurate" but **"we beat the base rate"**,
the only claim that earns the word *forecast*. The discipline is pre-registration with no look-ahead: the
window is strictly forward, the task, horizon and resolver are fixed before the outcome, and each
forecast is graded **exogenously** by the upstream event's own timestamp, never by the model's own
downstream evidence. A window whose issued vector was degenerate before clamping is **voided** rather
than graded — kept and counted as drained work, never scored.

**What it is currently testing.** The first seeding honestly surfaced that a country-week binary is
geography-dominated: a handful of active regions carry almost all the base rate, so a naive geographic
prior is hard to beat. The harness **detects that degeneracy and withholds the skill claim** rather than
reporting a flattering number. The machinery is real and running and the skill is unproven; it will be
claimed only once the score clears its reference over a sufficient record, or the task is sharpened to
one where geography is not the dominant signal. Until then Legba declines to call itself a forecaster.

### 12.7 Provenance

Every analytical product traces back to raw signals. An output stamps the substrate ids it read, so a walk
backtracks a world read to its country records, their units, each unit's cited signals, and each signal's
immutable acquisition provenance. Combined with the per-analyst receipt chain, the platform can answer
*why do we believe this?* at every level — which is the foundation of analytical accountability rather
than a nicety. The journal is the one explicit exception (§8), and a bearing edge is a pointer rather
than lineage (§7).

---

## See also

- `ACQUISITION.md` — sources, the canonical signal, enrichment, fan-out and subscription.
- `ARCHITECTURE.md` — the planes, the descriptor model, the registry and the actor runtime.
- `DATA_MODEL.md` — the tables, by plane, with their write semantics.
- `CORRECTNESS_GRADER.md` and `REFERENCE_BUILDER.md` — the correctness instrument and its references.
- `AGENCY_GATING_MODEL.md` — the gating model behind §9.
- `TUNABLES.md` — every threshold, budget, governor cap and flag named above.
- `SEAMS.md` — what is declared and not built.
