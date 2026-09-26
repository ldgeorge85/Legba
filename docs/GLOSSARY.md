<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Glossary

Definitions for Legba's coined terms (`source-first`, `substrate`, `descriptor`,
`signal`, `nexus`, `situation`, `seam`, `agency`, …) and the analysis-tradecraft
method names the docs use. Entries are grouped by area and alphabetized within
each group. New here? Start with the [README](../README.md) and the
[Tour](TOUR.md).

This file defines **concepts**, which are stable. For volatile specifics, go
elsewhere: live counts are generated into [RELEASE_STATE.md](RELEASE_STATE.md),
what is live versus gated versus untested is [STATUS.md](STATUS.md), per-route
and per-panel maturity is
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md), and the deliberately
not-built is [SEAMS.md](SEAMS.md). Where a term is built but unproven, or
deliberately not built, the entry says so.

**Groups:** [Core concepts](#core-concepts) · [Runtime & architecture](#runtime--architecture) · [Data model](#data-model) · [Analysis & methods](#analysis--methods) · [Operations & governance](#operations--governance)

---

## Core concepts

**analyst** — A declared reasoning unit (deterministic code or LLM-backed) that
reads a scoped slice of the **substrate**, runs a method, and writes typed
outputs (**findings**, **situations**, hypotheses, critiques) with full
provenance. It fires on a **coalescing trigger** and/or a **cadence**
heartbeat.

**analysis spine / the spine** — The product, built bottom-up, where each
stage consumes only *verified* output of the one below it: **bounded reasoning
units** → the mandatory **faithfulness verify** → the **composition tower** →
the banded **scorecard**, with a deterministic indicators-and-warning layer and
an honest skill scoreboard over all of it. "The spine" is what a change has to
justify itself against.

**bounded reasoning unit / unit** — One of nine narrow, single-question
`inline_target` analysts. Seven broad ones — **leadership_transition**, **energy_security**,
**escalation**, **narrative_coordination**, **internal_stability**,
**military_posture**, **economic_coercion** — fan out to all 32 country desks
(19 G20 + the 13-desk **watch** tier) by a `has_tag("g20") or has_tag("watch")`
predicate. An eighth, narrower unit, **proliferation_watch**, instead fans out
to only the ~8 nuclear-relevant desks via a `has_tag("nuclear_watch")`
predicate. A ninth, **disruption_status**, is tag-scoped the same way but off
the country plane entirely — `has_tag("supply_chain")`, the thematic lane/flow
desks, on a 24h window. Each run assembles a cited 72h signal slice plus a **grounding
preamble**, synthesizes one strict-JSON finding whose prose carries `[N]`
citation markers, then runs the mandatory **faithfulness verify**. Skill is
reported per unit, never as a platform-wide claim.

**composition** — A second-order finding that synthesizes already-**verified**
sub-claims, never raw signals. **country_composition** reads one desk's seven
broad verified units, plus **proliferation_watch** on nuclear desks, and writes
a hedged, cited per-country read; **region_composition**
folds the per-country reads into one of **five region frames** (Africa, Americas,
Europe, Indo-Pacific, MENA); **world_assessor** composes the region reads into one
cited world view, drillable world → region → country → unit → source; the thematic
**escalation_composition** fuses the per-desk escalation reads cross-desk under a
correlation guard. An unverified sub-claim never enters a composition; a desk with
no verify-passed claims yields an honest confidence-0.0 "nothing to synthesize"
finding. Supersession keeps one live head per desk.

**composition tower / the tower** — The stack of **compositions**: per-country,
then per-region, then the global world read, plus the thematic cross-desk
composition beside them. Each tier reads only the verified tier beneath it, so
a claim at the top is drillable back down to a source. Authority climbs only as
far as the verification underneath it reaches.

**descriptor** — A strict, content-hashed, registry-managed declarative config
record (validated by pydantic) that declares a source, target, analyst, or
pack; the runtime stands up the corresponding actor, so there is no code to
write per feed, target, or analysis. The live system is the registered
descriptor **rows in the database**, not the YAML files on disk — model
changes go live via the registry `PUT` API.

**exemplar use case (G20 country assessment)** — Geopolitical assessment of
the G20 countries is the proven end-to-end demonstration of the pipeline — not
the system's identity. There is no code per country: the G20 targets are
materialized from one **discovery** template, and the 13-country **watch tier**
(Israel, Iran, Ukraine, Taiwan, North Korea, Pakistan, plus the escalation-risk
band Sudan, Mali, Burkina Faso, Niger, DR Congo, Myanmar, Haiti) was added by
simply registering targets — as was the non-country **thematic** desk family
(the supply-chain lanes and flows), which is the clearer proof that a desk is a
registered subject-frame and not a country.

**finding** — The primary typed analyst output: a written analytic conclusion
carrying `derived_from` provenance and a **receipt-chain** entry, itself
re-published as a derived signal so downstream analysts can react to it. Every
cited finding is scored by the mandatory **faithfulness verify** pass, and its
surfaced confidence is folded to **effective_confidence** at read time.

**measured experiment** — An ambitious capability that returns ONLY as an
honestly-measured pilot, never as an always-on producer: the
**unit_optimizer** carries a real before/after faithfulness delta and can
never auto-promote on a degenerate one; the **forecast_scoreboard** reports a
Brier/BSS that currently shows NO proven skill. The always-on monoliths stay
cadence-frozen (`country_optimizer`) or retired (the forecast-as-claim
predictors).

**per-target assessment** — The analytic product about one specific target.
This is **country_composition**'s hedged synthesis over that
desk's seven broad verified reasoning **units** (plus **proliferation_watch**
on nuclear desks) — NOT the retired
**country_assessor** one-pager. Its global sibling is the world composition
(see **world_assessor**).

**provenance / lineage** — *Provenance* is the recorded origin and derivation
history of a row; *lineage* is the walkable chain (over `derived_from` links)
from any output back to the raw signals and sources behind it, resolved hop by
hop to the real source URL via `GET /api/v1/lineage/finding/{id}`.

**scorecard / banded scorecard** — One deterministic banded verdict row per
active `g20`/`watch` desk, written by **scorecard_producer** (the 12th
**OutputKind**) from high-precision rules over already-verified claims in a
rolling 14-day window — demote-never-promote, no LLM. Every band names the
verified-claim id it rests on; a dimension with no qualifying claim reads
`insufficient-evidence` with an explicit reason, never a fabricated band. The
live board is honestly a mix — some countries band, some read
all-insufficient (e.g. the US, whose unit faithfulness is genuinely low).

**signal** — The atomic unit of ingested data: one canonical observation (a
document, feed item, record, or media reference) carrying content, metadata,
and provenance, before any interpretation. A signal carries **no `target_id`**
— it is an observation, not an interpretation — which is exactly what lets one
signal route to many targets.

**situation** — A first-class durable temporal frame grouping related
signals/findings into an ongoing state of affairs, keyed by a
`situation_signature` and detected bottom-up. It is how a temporal grouping
is expressed in the product: an `events` table exists as a pure leaf, every
write flag-gated and nothing reading it, so a situation — not an event — is
what a desk reasons over. The situation write path has known maturity gaps.

**source** — A declared connector that acquires observations — either by
polling a feed on a cadence or by receiving a webhook push — enriches each
one, and publishes canonical signals. Pull (cadence poll) is the battle-tested
path; push (webhook) is supported but newer and less exercised.

**source-first** — The organizing principle: acquisition belongs to *sources*,
not to the things that consume data. A source ingests an observation once,
enriches it once, and publishes one canonical, target-agnostic **signal**; the
**fan-out** plane routes that signal to every **target** whose **predicate**
matches — *"ingest once, enrich once, match many."* Unrelated to the AGPL
"source-available" license.

**target** — A passive subscriber declaring *what to watch*: it selects a
slice of the shared signal pool by predicate and is what analysts produce
assessments about; it does no acquisition of its own. A "target" is a scoped
**subject / desk** a set of analysts work — not a surveilled entity — and its
scope can be geographic, organizational, or entity-based.

---

## Runtime & architecture

**altitude / the altitude map** — How far above the raw signal a piece of data
sits. Altitude 0 = enriched signals, facts, entities (produced at ingest);
altitudes 1–3 = findings, then situations/hypotheses/nexuses, then
meta-findings (produced by Tier-2 cadence reasoning). The **journal** is the
one layer that cuts *across* this map rather than sitting at a fixed altitude.

**baseline enrichment** — The deterministic, no-LLM step run on every signal
at ingest, inside the `SourceActor` before fan-out: `language_detect → geocode
→ entity NER → classify → source_credibility → dedupe → fact_extract`. This is
the *"enrich once"* in *"ingest once, enrich once, match many."* The `entity
NER` step is **translate-then-NER** for non-Latin scripts — Arabic/Russian/
Ukrainian signals (and Telegram message bodies, read from `payload.text`) are
translated to English via the hosted NLLB `/translate` endpoint before
extraction, where they previously yielded zero entities. The historical
backlog is drained: a `reenrich_ner` batch re-enriched the ~10k older
non-Latin/Telegram signals in place.

**cadence / cadence heartbeat** — A scheduled interval (a cron-derived Dapr
reminder, e.g. every 6h/12h/1d) on which an analyst is re-evaluated — the
coverage floor beneath the reactive trigger path.

**CAS-claim** — A compare-and-swap operation that lets exactly one worker claim
the right to fire an (analyst, target) batch, so the reactive path and the
cadence tick never double-dispatch the same run.

**coalescing trigger / coalescer** — The rule that accumulates matched signals
for an (analyst, target) pair and runs the analyst **once** — not once per
signal — when a gate trips (accumulation threshold plus severity gate),
clamped by a **cooldown**, with the **cadence** heartbeat as a floor. LLM
analysts are always floored to a coalesced batch.

**control plane / runtime plane** — The control plane is `legba-registry`
(descriptor registry, lifecycle state machine, API/WebSocket, vault,
dead-letter queue); the runtime plane is `legba-runtime-dapr`, which turns
active descriptors into running actors over the substrate.

**cooldown** — A per-pair minimum interval throttling how often the same
(analyst, target) can fire — the cost governor for expensive LLM analysis.

**Dapr reminder** — A persistent, restart-surviving scheduled callback, used
to drive a source's poll cadence and an analyst's cadence heartbeat.

**Dapr virtual actor** — Dapr is a distributed-application runtime; its
virtual actors are addressable, single-invocation-at-a-time, reminder-driven,
scale-from-zero stateful objects. Legba turns each active descriptor into one
such actor, distributed across runtime replicas by Dapr's placement service.

**Dapr Workflow** — Dapr's durable multi-step orchestration: a deterministic
orchestrator yields to activities and replays event history to resume after a
crash. Used for the multi-hour optimizer and for deep-consult. A
long-activity round-trip defect in the sidecar is fixed for the optimizer leg
by passing the training set by reference; deep-consult shares the fix but was
never independently re-verified, so its non-durable in-process fallback stays
the live default.

**fan-out / the fan-out plane** — The *match-many* routing layer: one shared
signal is delivered to every target whose predicate matches, instead of
copying signals per target. Implemented as coarse **NATS-subject** filtering,
narrowed by a SQL `WHERE` clause, then a fine-grained **Starlark residual
predicate**.

**the four planes** — Legba's top-level split: Acquisition (source ownership,
baseline enrichment, fan-out/subscription), Analysis (target subscribers,
analyst reasoning, triggers, **agency**), Async jobs (a NATS work-queue with
competing-consumer workers), and Substrate (the storage layer).

**job plane / competing-consumer workers** — A durable NATS work-queue plus a
worker pool and an execution ledger, for heavier off-actor work.
`process_media` is the one live job kind; jobs carry an idempotency key so
duplicates collapse to one execution.

**module-size gate** — The test that pins a line ceiling on every production
module that was already large when the gate was written, and on any module that
crosses the entry threshold afterwards. The way under a ceiling is to extract a
cohesive sibling, never to raise the number — raising one means editing the test,
which is a visible act in the diff, and that visibility is the whole mechanism.

**NATS JetStream / NATS subject** — NATS is a lightweight message bus;
*JetStream* is its persistent streaming layer, carrying the
signal-notification stream (`legba.signals.>`), lifecycle events, the
dead-letter queue, and work queues. A *subject* is a dot-delimited routing key
that lets consumers pre-filter cheaply before finer SQL/Starlark matching.

**predicate** — A single-expression boolean matching rule attached to a
target/source/analyst/trigger, deciding whether a signal matches. Predicates
appear on four surfaces (target scope, source-ref filter, analyst→target
bind, cadence trigger) and share one fixed helper catalog.

**reconcile loop** — A Kubernetes-operator-style control loop that watches
registry change events (via an *informer*) and activates/retires actors to
converge observed state with desired state, backed by a periodic resync.
Activation is idempotent, so a dropped reminder self-heals.

**seam / declared seam** — A capability intentionally **not built**, registered
in [SEAMS.md](SEAMS.md) with a guard rail that *raises* rather than stubbing
or faking output — enforced by a stub-scanner test (the platform's no-stub
rule). A declared seam does not work yet; don't mistake it for a finished
capability, or for a bug.

**slim registry image** — The registry container installs an explicit,
registry-only dependency list rather than the whole package, so the control
plane does not carry the runtime's analyst and acquisition dependencies. The
consequence is a rule: a registry route module must import cleanly under that
image, and a deferred import of a runtime handler still fails at request time.

**SourceActor / TargetActor / AnalystActor** — The three concrete actor types:
`SourceActor` owns one source's polling/push and baseline enrichment;
`TargetActor` represents one passive subscriber; `AnalystActor` accumulates
matched signals and fires reasoning. Each is a Dapr virtual actor stood up
from a descriptor.

**Starlark residual predicate** — A small sandboxed expression (Starlark is
Google's tiny Python-like config language) evaluating the fine-grained
"residual" match *after* the cheaper NATS-subject and SQL filters have
narrowed the candidates. Each evaluation is capped at ~5ms and fails closed.

**substrate** — The persistent state layer holding all signals, facts,
entities, relations, situations, and outputs: **Postgres + Apache AGE**
(relational + entity graph), **Qdrant** (vectors), **Redis** (hot cache), and
**NATS JetStream** (event bus + durable streams). It is the hand-off point
between otherwise-decoupled actors.

**the two tiers (Tier 1 / Tier 2)** — The two distinct *times* analysis
happens, kept strictly apart as a cost firewall. Tier 1 is inline, at-ingest,
per-signal, deterministic (**baseline enrichment**, no LLM); Tier 2 is the
cadence/slice analysts that batch accumulated signals and *reason*, never
per-signal — decoupling LLM cost from ingest volume.

**worker actors / bounded fan-out** — A primary `AnalystActor` owns the
cadence heartbeat and dispatches one run per matched target to
lazily-activated worker actors, bounded by a semaphore — a wide analyst over
many countries runs in parallel without overload.

---

## Data model

**canonical signal / dedup** — Deduplication links duplicate signals to a
single `canonical_signal_id` via a `signal_aliases` table ("link, never
collapse" — raw rows are kept for audit). A four-tier scheme escalates from
exact content-hash/URL match to semantic similarity via Qdrant; the semantic
tiers refuse loudly if their embedding/vector ports are absent.

**content-hashed identity** — A record's version *is* the hash of its content:
descriptors and signals are identified by a body hash, so any change is
automatically a new immutable version — no manual version strings — which also
enables integrity checks and dedup.

**contention sidecar** — Two tables (`fact_contention` +
`fact_contention_values`) holding one contention group per disputed
(subject, predicate) and its competing value clusters, plus three thin markers
on the `facts` rows themselves. The sidecar is recomputable from the open
facts — a derived index over the disagreement, never the source of truth.

**cross-framing** — Holding ONE published claim still and putting every
bounded unit of its desk beside it: how each unit's own read frames the same
matter, and — for a unit with no read — the **typed absence** that explains it.
The join is RECORDED, never inferred: a unit frames a claim when its block
cites at least one of the signals that claim cites, a link the composition's
producer stamps on the assembly payload (`blocks[].signals[].signal_id` and
`also_cited_by[]`), checkable by id. So the surface can distinguish four things
a single-unit read collapses into one: a unit that FRAMES the claim differently,
a unit that has a read and cites none of this claim's evidence (silent on the
matter, not contradicting it), a unit with no read and a typed absence naming
it, and a unit with no read and no typed absence at all. The last two are the
reading the arrangement is uniquely able to take — **absent, not contradicted**
— and what every other surface discards. It computes no verdict and no
statistic of its own; the framings are the units' own sentences verbatim, the
faithfulness is each block's own, and the layer-divergence rows travel verbatim
from the divergence map. Reader surface: the `analysis.cross_framing` panel
(docs/UI.md), derivation in `legba-ui-v3/src/lib/framingModel.ts`.

**contested claim / contention** — Two **open** facts asserting *different*
values for the same (subject, predicate), with neither superseding the other —
the "alternate facts" case **supersession** deliberately does not resolve.
Rather than silently pick one by recency, Legba lets the rivals coexist open
and records the disagreement in a sidecar so it surfaces honestly as
*disputed*. Gated behind `LEGBA_FACT_CONTENTION` (default off); see
[ANALYSIS.md](ANALYSIS.md) §7.11.

**earned track record** — A per-source win/loss record computed from the
system's **own substrate**: how often a source's claims ended on the winning
side of resolved **contentions** (Beta-smoothed, Wilson-lower-bounded,
corroboration-counted), written by the `source_track_record` analyst (ships
draft). It is *earned*, not asserted — distinct from operator-authored
ratings — and guarded for acyclicity (a lag window plus self-exclusion, so a
source can never vote on its own dispute). Consuming it in arbiter tie-breaks
is flag-gated **off by default**, and it never touches faithfulness.

**entity** — A resolved, disambiguated real-world actor (person, org, place)
stored as one canonical profile (keyed by name + class, with version history);
different surface mentions ("US", "U.S.", "United States") are merged by
entity resolution. Entity-resolution fragmentation and NER junk are known open
data-quality gaps in the audits.

**evidence archive / content addressing (`cas:sha256`)** — The preservation
sidecar for cited evidence: signals cited by **verified** findings have their
original bytes fetched (SSRF-guarded, license-gated, size-capped) and stored
**content-addressed** — the stored object's name *is* the SHA-256 of its
bytes, recorded on the signal as `object_ref = cas:sha256/<hex>` — so the
receipt chain terminates in a verifiable stored copy rather than a rotting
URL, and the address survives any later storage-backend swap. Archived
signals become `evidence_hold` (purge-exempt); nothing ever deletes archived
evidence today (a declared seam). The archiver ships as a draft descriptor.

**fact** — An atomic, temporally-versioned assertion (subject, predicate,
value) in the `facts` table with `valid_from` / `valid_until` and a
`superseded_by` pointer, carrying a `source_type`
(`ingestion` / `seed` / `curated` / `proposed`) and a confidence. An
ingestion fact carries the extractor's real per-relation score where there is
one and a declared default where there is not; only **seed/curated** facts are
used for grounding.

**fact_contention_arbiter** — A detect-only deterministic global META analyst
(hourly, **TRACE_ONLY**) that scans open facts, fuzzy-clusters their values
(so *Kyiv/Kiev* merge while *North/South Korea* stay split), scores each
cluster (see **Q·C·R·F score**), and writes only the **contention sidecar** +
markers. Its hard invariant: it **never** mutates a fact — disagreement is
annotated, never adjudicated into the facts table.

**grounding / grounding preamble** — At run time the GROUND phase prepends a
dated "authoritative current context" preamble of currently-valid facts,
nexuses, and situations from the substrate, correcting the LLM's stale
training cutoff. Restricted to still-valid facts of **seed/curated**
provenance only; all nine **bounded reasoning units** opt in. The Tier-2 vector
`world_context` RAG source is a **guarded, measured pilot** on the
`internal_stability` unit only — retrieved from a curated, re-embedded Qdrant
corpus through the stack embedder port (bge-m3 1024-dim) with a focused
`<country> <theme>` query, contextualized chunks, a lowered relevance floor,
country-filtering, and degrade-not-drop. Its injected priors stay **non-citable**
(fenced background, no `[N]` ids); `leadership_transition` RAG is **off** (rolled
back); and a per-run **RAG rollback guard** (see **rag_watch**) reverts injection
if faithfulness drops or token cost rises.

**modality** — The medium of a signal — text, image, audio, video, structured,
or binary — treated as a first-class axis from ingest onward. A modality
registry binds each one to an ingest extractor and a UI renderer, so adding a
content type needs no schema change; some renderers and the media extractors
(Whisper/VLM/OCR) are declared seams.

**narrative** — A *reified* contested-claim family: one durable row per
family of competing accounts, carrying its **carrier sources**, first-seen
times, echo lags, lead source, and value variants — written wholesale each
pass by the draft `narrative_mapper` analyst as a live readout over the
contention sidecar. **Detect-only and descriptive-not-causal**: a narrative
records who carried a claim and when, never *why*, and the mapper is honest
when no systematic pattern exists.

**nexus / nexuses** — A coined Legba term: a *reified* (stored, queryable)
relationship row between two entities, carrying a relation type, a
**+1 / 0 / −1 polarity sign**, intent, channel, confidence, and validity time.
Nexuses feed signed-graph analysis; the sign is a polarity label, not a
cryptographic signature.

**OutputKind** — The fourteen typed kinds an analyst can emit (finding,
situation, hypothesis, prediction, alert, meta_finding, critique, fact, nexus,
prompt-module candidate, **journal**, **scorecard**, situation_update,
**event**); a registry maps each to its table, payload model, schema URI, and
NATS subject. The **event** kind is a pure leaf — every write is flag-gated and
nothing reads it yet. `analyst_outputs` is
the generic table for kinds without a dedicated one; **journal** lands in its
own `journal_entries` table off the fact/finding/nexus chain.

**proposed_edges** — Provisional untyped candidate relationships inferred from
entity co-mention, queued for a governance handler to promote into typed
nexuses (or reject as junk, e.g. demonyms).

**Q·C·R·F score** — The arbiter's multiplicative weight on a competing value
cluster: **Q** quorum (distinct backing lineage) × **C** credibility share ×
**R** recency decay × **F** mean confidence. Any zero factor zeroes the
cluster; the score only ranks clusters for the **surfaced winner vs abstain**
decision and never edits a fact.

**read receipts (`read_events`)** — The append-only ledger of what an operator
actually read, written by a fail-silent client emitter at a handful of
chokepoints and rolled up in the database. A malformed event is dropped and
counted rather than failing its batch, and a failed batch is dropped rather
than retried — a retry would backdate a week of reading into one minute.

**receipt chain / hash-chained receipts** — Each analyst run appends an
`analyst_traces` row whose `receipt_hash` chains (SHA-256) over the previous
run's hash; a lineage node carries a `chain_consistent` boolean, surfaced as a
**"chain-consistent (single-node)"** badge. This is hash-chaining —
single-node integrity — **not** a cryptographic signature; analyst findings
are not signed or tamper-proof (Ed25519 signing exists only on the descriptor
audit-log's checkpoints).

**seed / seeding** — Importing curated reference data (current leaders,
alliances, conflict data) straight into facts/nexuses marked
`source_type='seed'`, tracked in a `seed_batches` ledger and idempotent on
re-run. Adapters include `world_baseline` (curated) and `wikidata_leaders`
(live SPARQL); some adapters (e.g. SIPRI) are registered but unseeded.

**source-echo edge** — A directed lead→follow edge in the narrative echo
graph: source A systematically publishes a narrative's claims before source B,
at a measured median lag and co-carriage ratio. An edge is only marked
*systematic* when the pattern clears honest thresholds — the live graph
showing co-carriage but zero systematic fast-echo is reported exactly as that,
never dressed up as coordination. Descriptive, not causal.

**source_credibility** — A nullable per-fact trust weight where **NULL means
unknown** (never 0): nominally 0.9 for seed/curated and 0.5 for
ingestion/agent facts, resolved as the MAX over the backing signals'
credibility when present. It feeds the credibility factor of the **Q·C·R·F
score**.

**surfaced winner vs abstain** — Per contention group the arbiter surfaces at
most one winner — the top-scoring cluster, iff it clears a minimum score and
beats the runner-up by a dominance ratio — otherwise it **abstains**, leaving
the group explicitly disputed. A surfaced winner is a read-side label only; it
never closes the losing facts. An optional bounded LLM tie-break (default off)
may run only on a near-tie, degrading back to abstain on any failure.

**temporal facts / supersession** — When a newer, differing value arrives, the
prior "true now" row is *closed* (`valid_until` stamped, `superseded_by` set)
rather than overwritten, so history accrues. The store answers both *what is
true now* and *what did we believe, when*.

**text fold** — The one normalisation site: NFKC, a punctuation table covering
the dash, quote and space forms, soft-hyphen removal, and case folding. Every
comparator that compares model prose to producer prose calls it, so a quote
match cannot depend on which hand-rolled normaliser a module happened to carry.
It imports nothing from its own package, so anything may import it.

**TRACE_ONLY / side-write** — An analyst run that writes its real result
straight into the knowledge tables (a "side-write") and records only an audit
trace — no findings-feed entry. A no-change meta run is forced to trace-only
so it doesn't spam the findings feed.

**unit reference (`unit_references`)** — An independently built reference for
one country desk over one window, one per unit dimension (eight on a country desk), assembled
from the open web by the **reference builder** and never from Legba's own substrate. It is what the **correctness
grader** grades a desk's reads against. The builder is the only writer: a grader
that could build its own reference could close the loop on itself.

**write-path coexistence** — Inside `supersede_prior_facts`, a same-tier
incoming value that is fuzzy-**distinct** from an open prior (not a typo/alias
of it) does not close it — the two coexist open as a candidate contention the
arbiter can group. Gated behind `LEGBA_FACT_CONTENTION` (default off).

---

## Analysis & methods

**the 7-phase envelope / GROUND phase** — The fixed deterministic stages
(WAKE/ORIENT/PLAN/GROUND/REASON/REFLECT/NARRATE) that wrap one analyst LLM
call so a run is reproducible and replayable; the mandatory faithfulness
**VERIFY** is a separate pass after NARRATE. ORIENT packs the scoped signal
slice under the input-token budget (with a hard signal-count backstop); GROUND
injects the grounding preamble between PLAN and REASON.

**analyst kind / `method.kind`** — An open taxonomy (sixteen built-in kinds
plus operator-registered extension kinds, which is how a kind is added without
touching the enum) classifying what an analyst reads and
writes (`inline_target`, `meta_findings_synthesizer`, `predictor`, `critic`,
`consult_on_demand`, `deterministic`, …). `method.kind` names *how* it reasons
(`llm_planner`, `react_loop`, `stat_forecaster`, `deterministic`, …).

**assembly (`assembly.v1`)** — What a **composition** writes instead of
free-text synthesis: a document of quoted spans carried byte-identically from
the desk reads beneath it, under a deterministic title, with a coverage ledger
accounting for every roster unit and a drop ledger whose arithmetic closes.
Each span is built against the full untruncated source body fetched at
synthesis time and carries its hash and byte offsets. Four deterministic audit
arms — quote fidelity, scope preservation, attribution equality, selection
honesty — grade the payload afterwards; every one of their reason codes is
hard, because none can say "the model outran its evidence", only "the record
does not say what it says it says". They are an audit, not a gate: a fire names
the constructor as the defect.

**Assessment (the fenced channel)** — The interpretive read generated *from* an
**assembly**'s own spine and graded as its own population, so the voice
survives the demotion of free-text composition without contaminating the
record. Its `derived_from` is exactly the spine it reads, enforced at the
syntax-tree level, and any sentence the spine does not support is marked with
an exact offset rather than dropped or laundered.

**attention measurement (`desk_reference`)** — An out-of-plane reference per
desk, unit and window, built from its own web search by a model that has never
seen this corpus, and diffed against the desk's own slice on two deliberately
separate metrics: whether the slice carried the story at all, and — among
stories it did carry — whether the desk engaged it. It counts; it does not
repair, and it raises no alerts.

**band persistence / reversal rate** — The scorecard's calibration record:
every band change is logged as a resolvable claim and graded
**deterministically** at 14/28-day horizons — *held* (persisted), *reverted*
(the band moved back), or *worsened/improved* — published as persistence and
reversal rates. Deliberately **not a Brier score**: bands are rule-derived
labels, not probabilities, and pretending otherwise would manufacture a skill
metric; the route and the finding both say so.

**basis / periphery (two-tier composition evidence)** — The two evidence
tiers a composition may consume when the tiered-evidence flag is on (default
**off**). The **basis** is the verify-passed sub-claims clearing the floor
(`effective_confidence ≥ 0.50` by default) — the load-bearing evidence the
composition may cite as established. The **periphery** is a small, capped,
explicitly-labeled tail of below-floor or unverified sub-claims that may only
inform *hedged* context; a bald claim resting solely on periphery citations is
a counted verify failure. Weak signal is thus distilled, never laundered into
the basis and never silently dropped. Each composition stamps "built on N
verified + M weak."

**bearing gate** — An optional post-match check on the **claim_watch** matcher
that asks whether new evidence actually bears on the standing question it
matched, rather than merely sharing vocabulary with it. Off by default; when on,
it is what lifts the matcher's measured precision to its bar.

**Brier score / Brier skill score (BSS)** — The *Brier score* is the mean
squared error between predicted probability and the 0/1 outcome (lower is
better); *BSS* expresses it relative to a baseline (per-country climatology),
positive only when the forecast beats that baseline. **No forecast-skill claim
is currently made** — the pilot's number lives in a segregated key and never
pools into anything headline.

**calibration / outcome-resolution** — Resolving each prediction/hypothesis
against later outcomes to check whether stated confidence matches reality. The
meaningful (*exogenous*) tier resolves against independent external facts; a
weaker `self_consistency_only` tier grades against the hypothesis's own
evidence and is flagged as such. Built but unproven — the live exogenous
record is effectively n=0.

**chronicle** — The weekly third-person tier of the voice roster: the
public-record narration over what the platform did and found, written on the
**journal** kind and, like every other member of that roster, off the
fact/finding/nexus chain.

**competing_hypotheses / ACH** — Analysis of Competing Hypotheses (Richards
Heuer's tradecraft method): score each evidence item against
mutually-exclusive thesis/counter-thesis pairs, weighted by *diagnosticity*,
to surface the least-contradicted one. Built but unproven — it writes real
rows but has no validated skill metric; cell scoring falls back to a lexical
scorer when the token budget is exhausted.

**consult / deep_consult** — On-demand analysts that answer operator questions
over the substrate. `consult` runs a **ReAct** (reason-then-act) loop over
governed read-only substrate tools, returning cited references and stated
uncertainty; `deep_consult` schedules a longer plan → acquire → analyze →
synthesize Dapr workflow and persists the result. Each request may pick which
registered LLM plane answers — **Opus** (Anthropic, billed, the default; no
selection preserves prior behavior) or **Core** (the free **self-hosted core**
plane) — via a server-side allowlist that maps the friendly value to a
component id (the client never names a component); the choice **fails closed**
(a chosen plane that can't be honored raises rather than silently billing the
default) and a provider outage surfaces as a graceful HTTP 503 naming the
*other* plane. Anthropic is now reserved for consult/deep_consult only (the
**journal** moved fully to the core plane). Production consult is governed
through the `substrate_read` pack — a tool not in that pack blocks as
`unknown_tool`.

**correctness grader / unit correctness** — The instrument that grades a desk's
read against an independently built **unit reference** rather than against its
own evidence — correctness, where **faithfulness verify** measures groundedness.
It fails loud and it fails empty: with no current reference at the stamp it
writes nothing, calls no model, and names which gap it is (no reference at all,
or one aged past its grace window). It can never write a reference.

**correctness gate** — The composition's rule that authority climbs only as far
as the verification underneath it reaches. A desk unit whose **correctness**
number is missing, single-family, or below the operator's bar is *quoted* into
the **periphery** tier rather than dropped, and the verdicts and the bars in
force are stamped onto the composition. Its thresholds are environment, not
descriptor options, on the same discipline as the grader's ceiling.

**country_assessor** — RETIRED: the former monolithic per-country
one-pager. Nothing in the trusted spine reads it, and it was the largest
producer of *unverified* monolithic output; the **bounded reasoning units**
plus **country_composition** produce the per-country read. Its
historical findings remain in the database, unread — a stop, not a clean
slate.

**coverage floor** — The detector that compares a desk's own entity census
against the polities its open situation frames actually name, and fires when a
foreign polity no frame names is carrying the desk's own reporting. It exists
because a single-frame desk can be structurally blind to its second story.

**critic** — A meta analyst using an LLM judge to score another analyst's
output against an operator-authored rubric. The critic *actuates* —
`effective_confidence = min(self-confidence, critic_score)` — so a poor grade
can only reduce surfaced confidence, never inflate it. The live critic
currently runs on the same core plane as the analysts it grades (a deliberate,
reversible choice); the mandatory **faithfulness verify** is the always-on
member of this family.

**degeneracy guard** — A check that withholds a forecast-skill claim when the
calls are trivially near-0 / near-1 or geography-dominated — beating
climatology on "which countries are seismic" is static geography, not
anticipating the future.

**desk baseline** — A per-desk statistical prior computed from Legba's own
signal history (lags, rolling means, time-since-event, neighbour spillover;
pure-stdlib, Poisson-floored) by the draft `desk_baseline` analyst. It is a
**falsifiable prior, never a forecast claim**: its only product roles are a
divergence input to the alert trigger scan (a desk deviating from its own
baseline) and an honest reference the LLM read can be compared against.

**effective_confidence** — The read-time fold
`min(confidence, faithfulness_score)`: a poorly-grounded claim can only be
demoted, never inflated. It gates a visible low-confidence tier (never a hard
delete) and drives the **scorecard**'s demote-never-promote banding; when
verify never ran it is `None` — a first-class `verify-failed` state, not a
value papered over.

**eval loop** — The analyst → critic → optimizer → calibration
self-improvement cycle. The auto-improvement legs (optimizer, exogenous
calibration) remain unproven research surfaces; the one grading leg that is
live and always-on is the mandatory **faithfulness verify**, which folds
**effective_confidence** on every cited finding.

**evidence window** — The true oldest-to-newest span of the heads a
**composition** actually consumed, computed from those rows rather than asked
of the model, and handed to the prompt as a copy-only block. It replaced a
model-derived as-of instant that drifted from the query that built the slice.

**external audit / standing auditor** — The analyst that checks a claim against
the *world* rather than against internal consistency: it samples the world read
and a rotating subset of desk reads, extracts checkable claims, and checks each
through the governed **web_access** pack — never ad-hoc HTTP — writing a
supported, contradicted, not-found or unchecked verdict per claim. It writes a
heartbeat on every run, including a run that audits nothing, so "nothing to
contradict" and "the auditor is dead" cannot look alike. Its verdicts carry
their own population key, separate from the judge's.

**external grading at width** — The standing audit run at volume, written to an
append-only ledger by a *third* model family fenced away from both the writer's
family and the judge's by component id and by registered family — so pointing
the grader at another component of the judge's own family is refused too. The
evidentiary contract is code, not a rubric prompt: source-tier lookup, a
verbatim decisive-span match through the shared **text fold**, and time
anchoring against the read's own **evidence window**.

**faithfulness verify** — The mandatory pass scoring whether each cited claim
follows from its cited evidence — **groundedness, not truth**. A deterministic
citation-presence floor (always on) marks any claim with no `[N]` marker, or
one resolving to no real signal, as UNSUPPORTED; an LLM judge refines the
verdicts through its own repointable route — the shipped descriptor default is
the same core model that produced the finding (same-model judging shares
blind spots, which is why the reference deployment repoints the route
cross-family at a hosted judge via `LEGBA_JUDGE_STACK_REF`) — degrading to
the floor labelled `judge-unavailable` (PROVISIONAL, under a ceiling) when
unreachable. Every critique stamps a `judge_pipeline_version` so verdicts
from different judge revisions never pool. The verdict persists as a `critique` and folds
`effective_confidence = min(confidence, overall_score)`.

**forecast_scoreboard** — The deterministic weekly driver of the
acute-forecast pilot — the only honest home of a forecast number. It issues
one binary forecast per G20 country per weekly window and exogenously resolves
closed ones; the numbers surface ONLY on the calibration scoreboard route,
never as a free-text claim or finding. Skill is withheld
(`forecast_unproven=True`) until the BSS is positive on a non-degenerate,
at-sample pilot.

**heterogeneity guard** — The **critic**'s refusal to correlate an output with
itself: a run carries the producing analyst's identity so the critic can tell
whether it is about to grade its own family, and missing identity is handled as
unknown rather than as a pass.

**honest-null** — The house rule for an unmeasured or degenerate result:
publish the absence as itself. A rate over zero checked items is *absent*, never
`0.0`; a thin pilot reports a withheld claim, never a bare positive number; a
dimension with no qualifying evidence reads `insufficient-evidence` with a
machine-readable reason, never a fabricated band.

**hypothesis** — A candidate explanation scored against evidence, stored in
its own table: a thesis with a mandatory counter-thesis and a running signed
evidence balance (±2 transitions auto-flip it confirmed/refuted), later
resolvable against outcomes.

**inline_target** — The per-target LLM analyst kind: it reads one target's
recent signal slice, prepends the **grounding preamble**, produces one
first-order cited finding whose prose carries `[N]` markers, then runs the
**faithfulness verify** pass. It is the kind behind the nine **bounded
reasoning units**, and the kind that opts into grounding and **agency** tools.

**Journal assessor** — A global META analyst (`journal_assessor`,
`target_filter=None`) that narrates a first-person point of view *across* the
entire flow — the one analyst pointed at the whole organism rather than one
slice. The voice roster shares one extension kind: the 12h entry tier, a daily
`journal_consolidator` that distills prior entries into one forward-carried
narrative, the weekly third-person **chronicle**, and the weekly **lens**
tier. It is granted only the non-write-fact `journal_read` +
`journal_propose` packs, so everything outward goes through
**propose-and-gate**; its only un-gated effect is its own continuity. Live,
deployed and live-validated.

**judge route** — The resolution ladder that decides which model judges a
finding: an environment override first, then the descriptor's own judge,
verify, and primary refs. The shipped descriptor default is the model that
produced the finding, which is self-hostable but shares its blind spots; the
reference deployment uses the environment rung to repoint every judge call at a
different model family without touching a descriptor. The rung in force is
recorded, not assumed.

**lens / lens_diff (faculty lenses)** — Four weekly interpretive analysts on
the journal kind (`lens_trend`, `lens_baserate`, `lens_capability`,
`lens_intent`), each carrying **one declared falsifiable prior**; every lens
reads the verified tower top and writes an `entry_kind='lens'` read that
asserts no new fact and has no publish edge. `lens_diff` runs after the four
and narrates where they agree, split, or outlie — a chorus diff, never a
merged consensus. All verify through the same post-persist faithfulness gate
as journal entries.

**leans (stance-typed lenses)** — Six further lenses on the same journal kind
(`lens_left`, `lens_right`, `lens_centre`, `lens_pragmatist`,
`lens_militarist`, `lens_isolationist`). Identical machinery
to the four faculties — one declared falsifiable prior per persona module, the
shared `lens_common` frame, `journal_read` only, `entry_kind='lens'`, the same
verify gate — but typed by **stance** (which way of weighing the tower the read
commits to) rather than by **function** (which cognitive move it makes). They
run daily in the 09:00–11:30 UTC band. The chorus diff's roster is deliberately
**not** widened to them: `lens_diff`'s persona declares a four-prior aperture
verbatim, so folding the leans in is its own change with its own persona edit.

**Journal (OutputKind)** — The typed output for a journal
entry/consolidation landing in the dedicated `journal_entries` table, not
`analyst_outputs`. It is **off the fact/finding/nexus chain** — an
always-empty `derived_from`, excluded from the lineage catalog — so a lineage
walk can never surface a journal node, and the journal can never write a
fact/finding/nexus (enforced by a gating test). Citations live only in the
row's `claims` / `cited_substrate_refs`, an up-only reference, not a lineage
edge.

**META analyst / meta-finding** — An analyst with no single-target binding
that runs once globally (on cadence) over other analysts' outputs or the whole
graph, producing second-order findings — e.g. the **composition** analysts,
the deterministic `scorecard_producer`, `cross_analyst_correlator`, and the
**Journal assessor**.

**open question** — A durable standing question raised by a read that could not
settle something, carried on a backlog a research analyst drains. New evidence
bearing on one lands a review flag and an append-only edge — and stops there:
nothing propagates a correction back into the product that rested on the stale
reading, and nothing ever closes a question (the **staleness debt** says so on
the wire).

**optimizer / GEPA / unit_optimizer** — GEPA is a reflective, Pareto-frontier
prompt-evolution method (run via DSPy in an isolated worker) that mutates a
prompt module from logged traces and critiques. It returns as a **measured
experiment**: the **unit_optimizer** is scoped to ONE bounded unit, every
candidate carries a real before/after faithfulness delta, and promotion stays
human-gated — never auto-firing on a degenerate, absent, or non-positive
delta. The unmeasured monolith (`country_optimizer`) is cadence-frozen;
`litellm`/`dspy` are barred from the production inference path.

**predictor / forecast_acute** — The `predictor` kind fits a time-series model
(AutoARIMA, falling back to a naive-mean baseline) over recent signal counts;
`forecast_acute` is the pilot estimating P(≥1 severe hazard) per G20 country
at a 7-day horizon. The forecast-as-*claim* producers (`country_predictor`,
`india_energy_predictor`) are retired and stopped, their historical rows left
in place and unread; forecasting returns only as the **forecast_scoreboard**,
and no forecast-skill claim is made.

**reference builder** — The analyst that builds one independent **unit
reference** per desk per window from the open web, on the free core plane,
through a bounded tool loop over the governed **web_access** pack. Six
acceptance fences run *after* the model commits, over text it cannot reach —
a fetched-URL manifest, a domain blocklist, an enforced note turn, a date gate,
a discovery-tier allowlist, and span verification. Its scheduler orders by last
*attempt*, not last success, because a failed build writes no row and ordering
by success pins the queue on the one target the lane cannot build.

**region rollup** — The region tier's assembly form: a byte-identical carry of
the country assemblies beneath it, so the world read composes over country
assemblies directly rather than over a layer that could re-narrate them.

**salience** — A per-signal consequence score used to order and cap what enters
a slice and what an **assembly** carries, with a declared floor beneath which a
row contributes no mass. It orders attention; it never stands in for
confidence.

**stale tense** — The marker every line of the HISTORICAL SERIES desk-grounding block ends
with, rendered exactly `(historical: valid YYYY..YYYY, recorded YYYY-MM)`
(`analysts/history_grounding.stale_tense_marker`). A **collection** holds numbers about past
periods, and a number printed into a prompt with no tense on it is one a read can re-assert as
a current value. The marker is produced ONCE and rides three surfaces unchanged — the prompt
line the model reads, the `evidence_text` the verify judge grades the cited claim against, and
the exported endnote — so a read that says "GDP growth is 2.8%" is graded against a row that
says, in the same words on every surface, that the figure is valid for 2025 and was recorded in
2026-07. A row missing either time prints `????` in that slot: a marker that invents a period
is worse than none, because the whole point of it is that the period is checkable.

**staleness debt** — The readable count of standing **open questions** whose
evidence has moved and whose dependent product has not. It reports
`match_verified: false` on the wire, because the matcher's precision is
measured out of plane rather than asserted, and because the closing half does
not exist.

**structural balance / graph mining** — Signed-graph analyses over the
entity/nexus graph: *structural balance* classifies signed relationship
triangles as balanced or "frustrated"; *graph mining* finds communities,
centrality, and brokers. Computed with networkx over the `nexuses` table;
built but unproven research surfaces.

**structural-verified vs unverified-structural** — The two honest badges a
*deterministic* (structural) analyst's claim-bearing output can carry.
Structural analysts are exempt from the LLM faithfulness judge (there is no
prose-grounding question to judge), so their outputs long read
`unverified — structural`. The structural-claims verify profile now
**re-derives asserted quantities from the output's own lineage**: a match
upgrades the badge to *structural-verified*, a miscount lands a flagged
critique. Verdicts are computed and shown always; folding them into
`effective_confidence` is flag-gated **off by default**.

**substrate slice / read slice** — The bounded window of substrate an analyst
reads each run (default ~last 24h, scope-filtered, ~50 rows plus peer
findings) rather than the whole pool. Each analyst kind has its own reader.

**tensions worth watching** — The required surfacing, in a two-tier
composition, of periphery evidence that *conflicts with the basis*: a weak or
unverified account contradicting the verified read is named as a tension —
hedged and attributed — rather than blended in or dropped. The phrase is the
prompt contract's own vocabulary; its point is that disagreement is signal,
not noise to launder away.

**typed absence** — The platform's name for the thing it says when it has
nothing: an absence with a **scope**, a **kind**, a **proof** and a **shelf
life**, rather than a blank. Eight kinds, one closed vocabulary, every item
carrying exactly one — `not_collected` (nothing covers the subject for this
desk), `collected_but_silent` (a covering source exists, is healthy, and
carried nothing — silent-but-healthy is its OWN kind, because a quiet desk is
not a broken one), `source_stale` (the producer that measures the subject is
past its own cadence budget or failing), `searched_found_nothing` (the audit
searched and nothing decided the claim; the proof carries what was searched),
`search_failed` (the search itself did not answer — unreachable, blocked, over
budget — never the same fact as finding nothing), `below_floor` (evidence
exists and did not clear the floor: the banded scorecard's
`insufficient-evidence`), `layer_declared_absent` (a **desk aperture** an
operator declared absent, with the reason), and `history_gap` (a curated **collection**
declares a series-and-subject for this desk and the `observations` table does not hold it —
the only kind about the PAST rather than the present; its proof is a LOAD receipt rather than
a read, because a gap is the absence of a row and the honest thing to point at is the run that
should have written it, and a year the PROVIDER never published is not this kind at all).
**The proof rule:** every item
carries `what_was_checked`, `checked_at`, and a `ref` (with its `ref_kind`) —
an item whose proof cannot be constructed is not emitted, because the whole
difference between an absence and a blank is that an absence says what was
looked at and where the record of that look lives. **The stamp rule:** every
item carries `as_of` (when the absence was MEASURED — the run, receipt or scan
instant it came from, never the moment somebody asked), `as_of_basis` naming
which instant that is, and `expires_at` (when it stops being current: the next
scheduled run of whatever measured it, from that producer's own cadence). A
past `expires_at` renders `stale: true` and reads as *last known absence, not
re-checked* — an old absence must never pass for a current one. A declaration
with no clock (`layer_declared_absent`) carries `expires_at: null` and a
`review` path instead. Served by `GET /api/v1/v3/absence?scope=<target_id>`
(`registry/absence_api.py`), which publishes the same seven meanings in its own
`kinds` block, names any kind it could not read in `not_measured`, and returns
an empty list — never an error and never a fabricated row — for a desk with
nothing absent. **Where it is shown:** the desk **gap strip** drills into it;
the **Morning Read**'s *Gaps* band reads it once per desk it shows and renders
the eight kinds grouped, each item with its clock ("last known absence, not
re-checked" where `expires_at` has passed) and the proof's `ref_kind` offered
as a control only where it resolves — a kind the route could not read says so
in the route's own words, a kind it read and found nothing under is named as
read, and the card-derived gaps follow as the rows no kind already names; and
the **desk brief** (`POST /v3/export` with `appendix.absences` + the desk's
`appendix.scope`) prints all eight kinds after the cited events, composed
SERVER-side off the same reader (`registry/export_absences.py`) so the
markdown and the JSON of one export carry the same block, and given its own
page in the printed document.

**unscoped absence (the scoped-absence backstop)** — The failure class
**faithfulness verify** is structurally blind to: a desk stating an absence as
a *world fact* when what it established is that its own sources carried
nothing. A deterministic detector fires only on a strong absence assertion in
the main position, only on spans the citation floor does not already count, and
passes anything hedged, cited, forward-looking or collection-scoped; a hit adds
one to the score's denominator and nothing to its numerator. It is a backstop
on the judge-off path, not a second opinion, and scoping a claim is still not
checking it against the world.

**unassessable / provisional** — Two honest score states a **faithfulness
verify** verdict can carry instead of a number. *Unassessable*: the body's
claims would not segment, so no score is published — never a perfect 1.0.
*Provisional*: the LLM judge did not run and the verdict rests on the
deterministic citation floor alone, so it publishes under a ceiling and is
labelled as such, never as a clean pass.

**voice contract** — The prompt rules every read carries so the prose cannot
drift from the query that built it: an as-of line copied from the printed slice
header, the stock template sentences banned with a replacement judgment shape,
and machine internals — microsecond timestamps, internal scores — barred from
the body.

**wire-pair collapse** — The rule that two mastheads carrying one agency
dispatch reach a desk as a single numbered signal. Both signal ids stay in
`derived_from` and the survivor renders a carried-by line; the guard is that
at least two *distinct* mastheads are required, which is what keeps same-title
automated alerts from collapsing into each other.

**world_assessor** — The global, target-less **composition**: it runs exactly
once per tick, composing over the per-region **region_composition** reads (which
in turn fold the per-country **country_composition** reads) —
every factual clause cited to a verified region/country read, drillable
world → region → country → unit → source. It no longer writes a raw-signal
executive one-pager (that framing was retired); it graduated into the world
composition and remains the canary for grounding + cadence health.

---

## Operations & governance

**action-pack / pack** — A registrable, versioned bundle granting an analyst
specific tools, prompt fragments, escalation channels, and a **governor** —
the sole surface by which an analyst is granted **agency**. Examples:
`substrate_read`, `escalate_finding`, `web_access`, `propose_facts`.
Production consult tools must be in the live pack — a missing entry blocks as
`unknown_tool`.

**agency** — An analyst's governed ability to invoke tools mid-run (the GATHER
phase) — fetch the web, propose facts, enqueue jobs, emit alerts — strictly
allow-listed and budgeted rather than hard-coded. Web text fetched via agency
is flagged UNVERIFIED; agent-proposed writes are PROPOSE-grade (capped
confidence, `source_type='proposed'`) and cannot mutate the control plane.

**bringup / registrar / catalog** — The deploy-time scripts that push
descriptors into the registry, so the live source/analyst/target set is the
database rows (the "catalog"), not the YAML files. Registrars are
**create-only** — model changes go via the registry `PUT` API — and
re-registering to a live runtime needs the correct DB and python entrypoint.

**coalesced alert** — The anti-noise contract of the alert-sink plane: when a
per-sink cooldown suppresses alerts, they are not silently thinned — each
suppressed alert keeps its own ledger row, and the **next** allowed
notification carries them as "+N more during cooldown" with a bounded
preview. A burst is distilled into one honest notification; nothing outward
is ever dropped without a trace.

**cold-start verification set** — The minimal 3-feed bootstrap (BBC World,
Deutsche Welle, Al Jazeera) that verifies the whole source → enrich → fan-out
→ assess loop from empty volumes, before scaling to the full source catalog
(see [SETUP.md](SETUP.md) §7). If the registry is empty at boot, the NLP
client stays null and enrichment silently does nothing — register first.

**credential vault** — An encrypted store for source and model secrets: a
`CredentialVault` using NaCl SecretBox (authenticated encryption via PyNaCl),
decrypted with an environment-provided master key kept out of image layers.
Descriptors hold only a secret *pointer*, never plaintext.

**discovery / discovery template** — A pipeline that creates source/target
*instances* from one template, rather than ingesting signals — e.g. the G20
country targets are all produced from a single template, which is why there is
no per-country code.

**DLQ / dead-letter** — A holding store (`descriptor_dead_letter`,
`output_dead_letter`) for malformed or failed descriptors/outputs, so a bad
item fails cleanly and is available for operator resubmission rather than
half-landing or corrupting a table.

**emit / output binding** — Best-effort post-write handlers that serialize a
stored output to an external format/sink — a STIX 2.1 bundle (optionally over
TAXII 2.1), an alert, a webhook, a NATS stream, an A2A envelope, or MCP.
*Degrade, don't drop*: a sink failure never blocks the durable write. Some
emit surfaces (TAXII push, the A2A skill router) are off-by-default declared
seams.

**escalate_finding / alert-sink plane** — `escalate_finding` is the pack that
fires when a finding crosses an escalation gate keyed on **post-verify
`effective_confidence × severity`** (severity is a first-class read column, not
a tag, so a verify-demoted finding does NOT alert). Outward delivery runs
through the modular **alert-sink plane**: a dispatcher fans each alert to
registered sinks — a generic webhook and a native **ntfy** push sink — with one
durable ledger row per outcome, per-alert idempotency, and a cooldown that
produces **coalesced alerts**. Every payload states its verification posture
(a faithfulness score or an explicit `unverified — <reason>`) and carries a
**receipt link**. Sinks activate by operator env (`LEGBA_ALERT_WEBHOOK_URL` /
`LEGBA_ALERT_NTFY_URL`); unset means every alert is an audited
`skipped_unconfigured` row and the NATS subject `channels.escalations` remains
the always-on bus edge.

**exemplar shelf** — The curated index of pattern threads: which patterns are
on offer, which slots survived curation, and a resolver that answers for every
id the shelf has ever carried. Its invariant is that a culled id stays
*resolvable* and is never *offered* — a merged id resolves to the survivor it
was folded into, a retired one to the reason it was retired — and slots are
never renumbered. It writes nothing.

**freshness grade** — A per-source honesty grade on the source-firing surface
— `ok` / `stale` / `warn` / `empty` / `ungraded` — judged against a budget
*derived from the source's own declared cadence* (the cron-walked maximum gap
times a grace factor), so a slow-by-design weekly feed is not falsely alarmed
and a quiet fast feed is. A source with no parsable cadence reads
`ungraded` — never a fake `ok`.

**governor** — The `PackGovernorEnforcer`: per-pack invocation/rate/cost caps
plus the global token envelope, applied before each tool call (precall-check →
record → settle), logging every decision to ledgers and emitting an
operator-visible event on a block. Known seam: a batch reserve can overshoot
the pack cap by ≤4 (read-only, $0 impact).

**lifecycle FSM** — The state machine every descriptor moves through:
`draft → configured → active → paused → retired`, with per-state hooks
separating *register* from *configure* from *activate*. The registrar advances
a freshly-registered descriptor to declared-active on first register.

**propose-and-gate / `journal_proposals`** — A human always sits between the
**journal**'s voice and any change it wants to make: everything outward — a
`correction`, a `change`, or a `self_revision` (protected sections
auto-reject) — goes to the human-gated `journal_proposals` queue, never a live
table. A human accepts or rejects; the accept path runs an idempotent per-kind
apply worker. The journal's only un-gated effect is its own continuity.

**production gauge** — The silent-absence detector: one row per producing loop
comparing what it should have produced against what it did, worst first, over
the same expectation model the production-deficit alert class reads. Three
integrity loops sit on the same walk and ask the harder question — whether what
a loop produces is still what we think it is (judge availability, and descriptor
prompt and state drift against the tree).

**RAG rollback guard / `rag_watch`** — A real per-run safety guard on the
`world_context` RAG pilot (`src/legba/runtime/rag_rollback.py`) that
re-checks a disabled-units env var and a
persisted state file on **every** run, so a rollback suppresses prior injection
on the *next* run without a restart. Triggers are a faithfulness drop, a
low-faith ratio, or a token-cost rise (≥35%); it is actuated by
`scripts/rag_watch.py --enforce`, with per-run trace instrumentation
(`world_context_top_score` / `retained` / `min_score`) so the measurement is
honest. Known limit: the pilot state file currently lives at an ephemeral path
(move to a volume for persistence).

**receipt link** — The URL every outward alert carries back into the lineage
API: tapping the notification opens the receipt chain for the exact finding or
transition that fired it, so an alert is never an unaccountable ping — the
claim, its citations, its verify verdict, and its source hops are one link
away. Built from `LEGBA_PUBLIC_BASE_URL`; the link is part of the mandatory
alert anatomy, not an optional nicety.

**security perimeter (Caddy basic-auth)** — The single outer boundary: Caddy
serves the operator UI over HTTPS behind HTTP basic auth and proxies `/api` to
the registry; internal services bind to loopback, and registry endpoints
additionally require a bearer token that fails closed if unset. This is the
whole perimeter — there is no RBAC/SSO (designed, not built).

**self-hostable / AGPL** — Released under the GNU Affero GPL-3.0-or-later,
whose §13 network clause requires offering source to users of a network
service run on modified code. Commercial/dual-licensing is intended (a CLA is
needed before outside contributions); AGPL "source-available" is a licensing
fact, distinct from the "source-first" acquisition architecture.

**single-tenant / `owner_tenant`** — `owner_tenant` is stamped on
sources/signals/outputs but is **not** an enforced isolation boundary today —
Legba ships single-tenant, single-operator, single-node. RBAC / SSO /
multi-tenant row-level security are designed but explicitly not built.

**SSRF guard** — An `SsrfGuardedTransport` that refuses agency web fetches to
loopback, RFC-1918 private ranges, link-local, and the cloud-metadata IP
(169.254.169.254), raising a clean tool failure. The planner controls the
*query*, never the *endpoint*.

**stack / stack registry** — Registry-managed descriptors for shared substrate
components (Postgres, NATS, Qdrant, LLM providers, embedder), with credentials
held separately in the **vault**; descriptors bind via a `StackRef`. A
shared-schema change requires rebuilding **both** `legba-runtime-dapr` and
`legba-registry` — a stale registry silently stops analysts firing.

**System Status panel (`system.status`)** — The per-component / per-layer
health view answering "are all sources firing? how is the queue? which cadence
triggers are stalled?" in one operator page. It composes four layers:
Acquisition (per-source firing matrix), Analysis (per-analyst cadence health,
read from `analyst_traces`), Queues (consumer lag), and Infra (substrate
reachability).

**the three-way agency gate (grant ∩ allow ∩ applicability)** — A pack is
*effective* only where the analyst's **grant**, the target's **allow**-list,
and the pack's **applicability** predicate all overlap — enforced by the
governor and defaulting fail-closed. See
[AGENCY_GATING_MODEL.md](AGENCY_GATING_MODEL.md).

**token budget / global token envelope** — `budget_tokens_per_day` caps one
analyst's daily token use; a system-wide envelope blocks any pack call once
exhausted. On exhaustion the strategy is *demote-and-continue* to a cheaper
fallback model, or — if none is wired — pause loudly until the next budget
window (`BUDGET_THROTTLED`).

**watchlist / watch** — An operator-defined standing watch: an **entity**
(alias- and merge-fold-resolved), a free-text **topic** (served by the
existing text-search plane, with its limits stated rather than papered over),
or a **place** (countries, or a point+radius that matches only
trustworthy-precision geo). Watches are CRUD'd via `/v3/watchlist` (soft
delete) and fire through the same verification-gated alert loop as everything
else, as the `watchlist_hit` trigger class — per-watch caps with honest
rollups, and a new watch starts against a no-history guard so it cannot flood
from backfill.

---

## Terms of the record, the voice and the measurement layer

**record** — What a composition tier writes: a deterministic assembly of one lead span per verified head, quoted byte for byte under ordinals, with a deterministic title. The country record, the world record. A record makes no model call.

**rollup (`region_rollup.v1`)** — The region tier's deterministic arithmetic over its country records: structural claims and carried leads, no prose, no model call, no faithfulness verdict. The world tier reads the country records directly because a rollup carries nothing a floor-gated inner join could admit.

**periphery** — The blocks a record carries beyond its lead and co-leads, capped, so the reader sees what the tier considered without the record growing past its shape.

**rung** — One provider in the audit's search ladder. Rung zero is the free engine; the paid rung is declared per binding and receives the reformulated query when the grader offers one, so the one second search per claim lands on an index that can change the answer.

**width** — The external audit's operating mode: every eligible claim from the newest heads is queued and graded, rather than a small daily sample. The width stamp on a grade names the instrument revision it was taken under.

**panel** — One registered view in the operator console: a kind in the panel registry paired with one component, opened into the Dockview dock. A panel *instance* is one panel bound to one target or analyst, fetched from the registry rather than hard-coded, so adding a desk adds its panels.

**workspace / stance** — A curated arrangement of the dock for one reason to open the app, selected from the workspace bar. Switching serialises the outgoing arrangement into its own slot and restores the incoming one; a stance is an object you return to, not a reset.

**mission** — A stance plus the three axes a stance never carried: the target scope the wall is about, the temporal window the map and the scrubber are set to, and the map's source-layer selection — chosen as ONE object from the workspace bar's mission chooser, and named for the reader's job rather than for the arrangement (Morning Read, Desk Watch, Crisis, Release). Choosing one moves the map and the clock, not just the tab strip, and leaves the Consult tile open with the ambient scope pin already set, so switching missions does not orphan the session. A mission *declares* what it wants to be about and *resolves* that against what the reader has actually selected: with no desk selected, Desk Watch leaves the scope exactly as it was and says so on the bar rather than inventing a desk or clearing the wall. The two stances about the machine — The Gate and Engine — carry no mission, because they have no desk, window or layers to declare.

**source_class** — A source descriptor's declared editorial class (reporting, analysis, official, state media), a closed vocabulary that lets the analysis plane weight a claim by what kind of source made it, separately from host credibility. A per-channel map can override it on a multi-channel source.

**license_class** — The declared licence posture of a source's material, a closed vocabulary carried on the source scope and mirrored as a per-host ledger the operator curates. Declared, never inferred: an unset class is not "unknown", and the evidence archiver fails closed on it.
