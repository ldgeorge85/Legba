<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Glossary

Definitions for Legba's coined terms (`source-first`, `substrate`, `descriptor`,
`signal`, `nexus`, `situation`, `seam`, `agency`, …) and the analysis-tradecraft
method names the docs use. Entries are grouped by area and alphabetized within
each group. New here? Start with the [README](../README.md) and the
[Tour](TOUR.md).

This file defines **concepts**, which are stable. For volatile specifics — how
many sources are live, which features are proven vs experimental, the migration
head — see [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) and
[SEAMS.md](SEAMS.md). Where a term is built but unproven, or deliberately not
built, the entry says so.

**Groups:** [Core concepts](#core-concepts) · [Runtime & architecture](#runtime--architecture) · [Data model](#data-model) · [Analysis & methods](#analysis--methods) · [Operations & governance](#operations--governance)

---

## Core concepts

**analyst** — A declared reasoning unit (deterministic code or LLM-backed) that
reads a scoped slice of the **substrate**, runs a method, and writes typed
outputs (**findings**, **situations**, hypotheses, critiques) with full
provenance. It fires on a **coalescing trigger** and/or a **cadence**
heartbeat.

**bounded reasoning unit / unit** — One of seven narrow, single-question
`inline_target` analysts — **leadership_transition**, **energy_security**,
**escalation**, **narrative_coordination**, **internal_stability**,
**military_posture**, **economic_coercion** — fanned out to all 25 country desks
(19 G20 + the 6-desk **watch** tier) by a `has_tag("g20") or has_tag("watch")`
predicate. Each run assembles a cited 72h signal slice plus a **grounding
preamble**, synthesizes one strict-JSON finding whose prose carries `[N]`
citation markers, then runs the mandatory **faithfulness verify**. Skill is
reported per unit, never as a platform-wide claim.

**composition** — A second-order finding that synthesizes already-**verified**
sub-claims, never raw signals. **country_composition** reads one desk's seven
verified units and writes a hedged, cited per-country read; **region_composition**
folds the per-country reads into one of **five region frames** (Africa, Americas,
Europe, Indo-Pacific, MENA); **world_assessor** composes the region reads into one
cited world view, drillable world → region → country → unit → source; the thematic
**escalation_composition** fuses the per-desk escalation reads cross-desk under a
correlation guard. An unverified sub-claim never enters a composition; a desk with
no verify-passed claims yields an honest confidence-0.0 "nothing to synthesize"
finding. Supersession keeps one live head per desk.

**descriptor** — A strict, content-hashed, registry-managed declarative config
record (validated by pydantic) that declares a source, target, analyst, or
pack; the runtime stands up the corresponding actor, so there is no code to
write per feed, target, or analysis. The live system is the registered
descriptor **rows in the database**, not the YAML files on disk — model
changes go live via the registry `PUT` API.

**exemplar use case (G20 country assessment)** — Geopolitical assessment of
the G20 countries is the proven end-to-end demonstration of the pipeline — not
the system's identity. There is no code per country: the G20 targets are
materialized from one **discovery** template, and the 6-country **watch tier**
(Israel, Iran, Ukraine, Taiwan, North Korea, Pakistan) was added by simply
registering targets.

**finding** — The primary typed analyst output: a written analytic conclusion
carrying `derived_from` provenance and a **receipt-chain** entry, itself
re-published as a derived signal so downstream analysts can react to it. Every
cited finding is scored by the mandatory **faithfulness verify** pass, and its
surfaced confidence is folded to **effective_confidence** at read time.

**knowledge fusion / data fusion** — Here: situation-assessment over text and
structured data — turning raw signals into a linked, deduplicated, time-aware
body of entities, facts, **nexuses**, situations, and assessments rather than
isolated records. It is *not* sensor-fusion or track-correlation; Legba does
not claim that defense/aerospace level of rigor.

**measured experiment** — An ambitious capability that returns ONLY as an
honestly-measured pilot, never as an always-on producer: the
**unit_optimizer** carries a real before/after faithfulness delta and can
never auto-promote on a degenerate one; the **forecast_scoreboard** reports a
Brier/BSS that currently shows NO proven skill. The always-on monoliths stay
cadence-frozen (`country_optimizer`) or retired (the forecast-as-claim
predictors).

**per-target assessment** — The analytic product about one specific target.
Since 2026-07 this is **country_composition**'s hedged synthesis over that
desk's seven verified reasoning **units** — NOT the retired
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
`situation_signature` and detected bottom-up — Legba's deliberate stand-in for
an events table (there is no events table). The situation write-path has known
maturity gaps flagged in the data-quality audits.

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
the *"enrich once"* in *"ingest once, enrich once, match many."*

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
crash. Used for the multi-hour GEPA optimizer and deep-consult; subject to a
long-activity round-trip bug (1.17.9) and falls back to a non-durable
in-process loop.

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

**contested claim / contention** — Two **open** facts asserting *different*
values for the same (subject, predicate), with neither superseding the other —
the "alternate facts" case **supersession** deliberately does not resolve.
Rather than silently pick one by recency, Legba lets the rivals coexist open
and records the disagreement in a sidecar so it surfaces honestly as
*disputed*. Gated behind `LEGBA_FACT_CONTENTION` (default off); see
[ANALYSIS.md](ANALYSIS.md) §7.11.

**entity** — A resolved, disambiguated real-world actor (person, org, place)
stored as one canonical profile (keyed by name + class, with version history);
different surface mentions ("US", "U.S.", "United States") are merged by
entity resolution. Entity-resolution fragmentation and NER junk are known open
data-quality gaps in the audits.

**fact** — An atomic, temporally-versioned assertion (subject, predicate,
value) in the `facts` table with `valid_from` / `valid_until` and a
`superseded_by` pointer, carrying a `source_type`
(`ingestion` / `seed` / `curated` / `proposed`) and a confidence. Raw ingested
facts are stamped confidence 1.0 regardless of trust, so only **seed/curated**
facts are used for grounding.

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
provenance only; all seven **bounded reasoning units** opt in. The Tier-2 vector
`world_context` RAG source is now **LIVE** (retrieved from a curated Qdrant corpus
through the stack embedder port, bge-m3 1024-dim; opportunistic, relevance-floored,
country-filtered, degrade-not-drop) and **staggered on** for `leadership_transition`
+ `internal_stability`.

**modality** — The medium of a signal — text, image, audio, video, structured,
or binary — treated as a first-class axis from ingest onward. A modality
registry binds each one to an ingest extractor and a UI renderer, so adding a
content type needs no schema change; some renderers and the media extractors
(Whisper/VLM/OCR) are declared seams.

**nexus / nexuses** — A coined Legba term: a *reified* (stored, queryable)
relationship row between two entities, carrying a relation type, a
**+1 / 0 / −1 polarity sign**, intent, channel, confidence, and validity time.
Nexuses feed signed-graph analysis; the sign is a polarity label, not a
cryptographic signature.

**OutputKind** — The twelve typed kinds an analyst can emit (finding,
situation, hypothesis, prediction, alert, meta_finding, critique, fact, nexus,
prompt-module candidate, **journal**, **scorecard**); a registry maps each to
its table, payload model, schema URI, and NATS subject. `analyst_outputs` is
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

**TRACE_ONLY / side-write** — An analyst run that writes its real result
straight into the knowledge tables (a "side-write") and records only an audit
trace — no findings-feed entry. A no-change meta run is forced to trace-only
so it doesn't spam the findings feed.

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

**analyst kind / `method.kind`** — An open taxonomy (~twelve built-in kinds
plus operator-registered extensions) classifying what an analyst reads and
writes (`inline_target`, `meta_findings_synthesizer`, `predictor`, `critic`,
`consult_on_demand`, `deterministic`, …). `method.kind` names *how* it reasons
(`llm_planner`, `react_loop`, `stat_forecaster`, `deterministic`, …).

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
synthesize Dapr workflow and persists the result. Production consult is
governed through the `substrate_read` pack — a tool not in that pack blocks as
`unknown_tool`.

**country_assessor** — RETIRED (2026-07): the former monolithic per-country
one-pager. Nothing in the trusted spine reads it, and it was the largest
producer of *unverified* monolithic output; the four **bounded reasoning
units** plus **country_composition** now produce the per-country read. ~1.2k
historical findings remain in the DB, unread.

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

**faithfulness verify** — The mandatory pass scoring whether each cited claim
follows from its cited evidence — **groundedness, not truth**. A deterministic
citation-presence floor (always on) marks any claim with no `[N]` marker, or
one resolving to no real signal, as UNSUPPORTED; an optional LLM judge —
currently the same core model that produced the finding, **not** cross-family
(a deliberate, temporary choice; same-model judging shares blind spots) —
refines the verdicts, degrading to the floor labelled `judge-unavailable`
when unreachable. The verdict persists as a `critique` and folds
`effective_confidence = min(confidence, overall_score)`.

**forecast_scoreboard** — The deterministic weekly driver of the
acute-forecast pilot — the only honest home of a forecast number. It issues
one binary forecast per G20 country per weekly window and exogenously resolves
closed ones; the numbers surface ONLY on the calibration scoreboard route,
never as a free-text claim or finding. Skill is withheld
(`forecast_unproven=True`) until the BSS is positive on a non-degenerate,
at-sample pilot.

**hypothesis** — A candidate explanation scored against evidence, stored in
its own table: a thesis with a mandatory counter-thesis and a running signed
evidence balance (±2 transitions auto-flip it confirmed/refuted), later
resolvable against outcomes.

**inline_target** — The per-target LLM analyst kind: it reads one target's
recent signal slice, prepends the **grounding preamble**, produces one
first-order cited finding whose prose carries `[N]` markers, then runs the
**faithfulness verify** pass. It is the kind behind the four **bounded
reasoning units**, and the kind that opts into grounding and **agency** tools.

**JDL data-fusion model (L0–L5)** — The Joint Directors of Laboratories
reference model (signals → entities → situations → impact → refinement).
[ANALYSIS.md](ANALYSIS.md) uses it only as a *conceptual map* for where
Legba's pipeline sits — it does not imply sensor-fusion rigor.

**Journal assessor** — A global META analyst (`journal_assessor`,
`target_filter=None`) that narrates a first-person point of view *across* the
entire flow — the one analyst pointed at the whole organism rather than one
slice. Two tiers share one extension kind: a 12h entry tier and a daily
`journal_consolidator` that distills prior entries into one forward-carried
narrative. It is granted only the non-write-fact `journal_read` +
`journal_propose` packs, so everything outward goes through
**propose-and-gate**; its only un-gated effect is its own continuity. Live,
deployed and live-validated.

**Journal (OutputKind)** — The 11th typed output: a journal
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
`india_energy_predictor`) are retired and stopped (~539 historical prediction
rows remain, unread); forecasting returns only as the **forecast_scoreboard**,
and no forecast-skill claim is made.

**structural balance / graph mining** — Signed-graph analyses over the
entity/nexus graph: *structural balance* classifies signed relationship
triangles as balanced or "frustrated"; *graph mining* finds communities,
centrality, and brokers. Computed with networkx over the `nexuses` table;
built but unproven research surfaces.

**substrate slice / read slice** — The bounded window of substrate an analyst
reads each run (default ~last 24h, scope-filtered, ~50 rows plus peer
findings) rather than the whole pool. Each analyst kind has its own reader.

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

**escalate_finding / alert sink** — A pack that fires when a finding crosses an
escalation gate keyed on **post-verify `effective_confidence × severity`** (severity
is a first-class read column now, not a tag, so a verify-demoted finding does NOT
alert), with per-attempt delivery audit rows. External delivery currently lands on
the NATS subject `channels.escalations` only (**bus-only**); other alert-sink handlers
(Pushover, XMPP/Matrix) exist in code but are not the live escalation edge, and no
paged-human integration is claimed.

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
