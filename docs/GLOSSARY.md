<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Glossary

Legba uses a handful of coined terms (`source-first`, `substrate`, `descriptor`,
`signal`, `nexus`, `situation`, `seam`, `agency`…) and some method names from
analysis tradecraft. This page defines each one in plain language first, then
precisely. New here? Skim **Core concepts** top-to-bottom; everything else is a
reference to dip into.

This file defines **concepts**, which are stable. For volatile specifics —
how many sources are live, which features are proven vs experimental, the
migration head — see [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) and
[SEAMS.md](SEAMS.md). Where a term is built but unproven, or deliberately not
built, the entry says so.

**Groups:** [Core concepts](#core-concepts) · [Runtime & architecture](#runtime--architecture) · [Data model](#data-model) · [Analysis & methods](#analysis--methods) · [Operations & governance](#operations--governance)

---

## Core concepts

**source-first** — Data is fetched and cleaned **once** by a source, then shared
with everything that needs it.
The platform's organizing principle: acquisition belongs to *sources*, not to the
things that consume data. A source ingests an observation once, enriches it once,
and publishes one canonical, target-agnostic **signal**; the **fan-out** plane
then routes that single signal to every **target** whose **predicate** matches.
The slogan is *"ingest once, enrich once, match many"* — one BBC feed serves
nineteen country targets without re-fetching.
*Note: "source-first" describes acquisition architecture. It is unrelated to the
AGPL "source-available" license — though Legba is also that.*

**source** — Where data comes in.
A declared connector that acquires observations — either by **polling** a feed on
a cadence or by receiving a **webhook push** — enriches each one, and publishes
canonical signals. It is the most fundamental actor in a source-first platform.
*Note: pull (cadence poll) is the battle-tested path; push (webhook) is supported
but newer and less exercised.*

**signal** — One raw observation from a source, before any interpretation.
The atomic unit of ingested data: a single canonical observation (a document,
feed item, record, or media reference) carrying content, metadata, and
provenance. A signal carries **no `target_id`** — it is an observation, not an
interpretation — which is exactly what lets one signal route to many targets.

**target** — A declared subject of analysis (e.g. a country) that signals are
routed to.
A passive subscriber declaring *what to watch*. It selects a slice of the shared
signal pool by predicate and is what analysts produce assessments about. A target
does no acquisition of its own.
*Note: targets are not geo-only — scope is polymorphic (geographic, organizational,
or entity-based).*

**analyst** — A declared unit that reasons over matched signals and writes typed
outputs.
A reasoning unit (deterministic code or LLM-backed) that reads a scoped slice of
the **substrate**, runs a method, and writes typed outputs (**findings**,
**situations**, hypotheses, predictions, critiques) with full provenance. It fires
on a **coalescing trigger** and/or a **cadence** heartbeat.

**descriptor** — A config record that *declares* a source, target, analyst, or
pack — instead of you writing code for it.
A strict, content-hashed, registry-managed declarative unit (validated by
pydantic) that says *what to watch* and *how to reason*. The runtime reads each
registered descriptor and stands up the corresponding actor — there is no code to
write per feed, per target, or per analysis. The precedent is Kubernetes CRDs, dbt
models, or Prometheus scrape configs.
*Note: the live system is the registered descriptor **rows in the database**, not
the YAML files on disk. Model changes go live via the registry `PUT` API; the
bringup scripts are create-only.*

**finding** — The default analyst output: a recorded conclusion about a target.
The primary typed output — a written analytic conclusion carrying `derived_from`
provenance and a **receipt-chain** entry. A finding is itself re-published as a
derived signal, so downstream analysts can react to it.

**situation** — A durable thematic cluster of related findings/signals — a
non-geographic frame.
A first-class temporal frame that groups related signals/findings into an ongoing
state of affairs, keyed by a `situation_signature` and detected bottom-up. Situations
are Legba's deliberate stand-in for an *events table* (there is no events table).
*Note: distinct from a finding (about a target) and from a raw "event". The
situation write-path has known maturity gaps flagged in the project's data-quality
audits.*

**per-target assessment** — The final analytic product about **one specific**
target.
A reasoned judgment about a single target (e.g. one G20 country), coalesced from
its matched signals — the top of the pipeline. Produced by **country_assessor**.
(Its sibling **world_assessor** is target-less — see those entries.)

**knowledge fusion / data fusion** — Linking many separate observations into one
connected, deduplicated, time-aware picture.
Here this means **situation-assessment over text and structured data** — turning
raw signals into a linked body of entities, facts, relations (**nexuses**),
situations, and assessments, rather than isolated records.
*Note: this is situation-assessment, **not** sensor-fusion or track-correlation.
Legba does not claim that defense/aerospace level of rigor.*

**provenance / lineage** — The traceable record of where every output came from.
*Provenance* is the recorded origin and derivation history of a row; *lineage* is
the walkable chain (over `derived_from` links) from any output back to the raw
signals and sources behind it. Together with auditability and the self-hostable
model, this is the project's stated differentiator — **not** data access or
analytic maturity.

**the exemplar use case (G20 country assessment)** — The flagship demo domain that
*proves* the pipeline — not the system's identity.
Geopolitical assessment of the G20 countries is the proven end-to-end demonstration
of the source → enrich → fan-out → assess pipeline. The same pipeline applies to
any domain you can point a source at.
*Note: there is **no code per country** — the G20 targets are materialized from one
**discovery** template. Geopolitics is the exemplar, not the product's identity.*

---

## Runtime & architecture

**substrate** — The shared storage layer everything reads from and writes to.
The persistent state layer holding all signals, facts, entities, relations,
situations, and outputs: **Postgres + Apache AGE** (relational + entity graph),
**Qdrant** (vectors), **Redis** (hot cache), and **NATS JetStream** (event bus +
durable streams). It is the hand-off point between otherwise-decoupled actors —
not a bespoke "fusion engine".

**the two tiers (Tier 1 / Tier 2)** — The two distinct *times* analysis happens,
kept strictly apart as a cost firewall.
**Tier 1** is inline, at-ingest, per-signal, and **deterministic (no LLM)** — the
**baseline enrichment** pipeline that runs on every signal before fan-out.
**Tier 2** is the **cadence/slice analysts** that batch accumulated signals and
*reason* (LLM + deterministic), never per-signal. Keeping heavy reasoning off the
ingest firehose is what decouples LLM cost from ingest volume.

**baseline enrichment** — The deterministic, no-LLM step run on every signal at
ingest.
Inside the `SourceActor`, before fan-out: `language_detect → geocode →
entity NER → classify → source_credibility → dedupe → fact_extract`. Local NLP
(GLiREL + zero-shot DeBERTa + pycountry/geocoder), no analyst LLM. This is the
*"enrich once"* in *"ingest once, enrich once, match many"*; its writes are
altitude-0 substrate (enriched signal columns + ingestion facts + entity rows).

**altitude / the altitude map** — How far above the raw signal a piece of data
sits — the docs' organizing frame.
Altitude 0 = enriched signals, facts, entities (produced at ingest). Altitudes
1-3 = findings, then situations/hypotheses/nexuses, then meta-findings (produced
by Tier-2 cadence reasoning). "Extraction is always-on at ingest; reasoning is
cadence-batched above it."

**Dapr virtual actor** — A stateful, per-descriptor object the runtime activates
on demand.
Dapr is a distributed-application runtime; its *virtual actors* are addressable,
single-invocation-at-a-time, reminder-driven, scale-from-zero stateful objects.
Legba turns each active descriptor into one such actor, distributed across runtime
replicas by Dapr's placement service.
*Note: a Dapr long-activity round-trip bug (1.17.9) can stop the durable-workflow
path resuming for the optimizer / deep-consult; mitigated by an in-process fallback.*

**SourceActor / TargetActor / AnalystActor** — The three concrete actor types:
acquire, subscribe, reason.
`SourceActor` owns one source's polling/push and baseline enrichment; `TargetActor`
represents one passive subscriber; `AnalystActor` accumulates matched signals and
fires reasoning. Each is a Dapr virtual actor stood up from a descriptor; testable
cores let the logic run without a Dapr sidecar in tests.

**control plane / runtime plane** — The service that *holds* descriptors vs the
service that *runs* the actors.
The control plane is `legba-registry` (descriptor registry, lifecycle state
machine, API/WebSocket, vault, dead-letter queue). The runtime plane is
`legba-runtime-dapr`, which turns active descriptors into running actors over the
substrate.

**reconcile loop** — The loop that makes the running actors match the registered
descriptors.
A Kubernetes-operator-style control loop: it watches registry change events (via
an *informer*) and activates/retires actors to converge observed state with
desired state, backed by a periodic resync. Activation is idempotent, so a dropped
reminder self-heals.

**Dapr reminder** — A durable timer that wakes an actor on a schedule.
A persistent, restart-surviving scheduled callback, used to drive a source's poll
cadence and an analyst's cadence heartbeat.

**fan-out / the fan-out plane** — The routing layer that delivers each signal to
**every** matching target.
The *match-many* step: one shared signal is routed to all targets whose predicate
matches, instead of copying signals per target. Implemented as coarse
**NATS-subject** filtering, narrowed by a SQL `WHERE` clause, then a fine-grained
**Starlark residual predicate**.

**predicate** — A boolean matching rule that decides whether a signal matches.
A single-expression test attached to a target/source/analyst/trigger. Predicates
appear on four "surfaces" (target scope, source-ref filter, analyst→target bind,
cadence trigger) and share one fixed helper catalog.

**Starlark residual predicate** — A small sandboxed expression that does the
final, precise yes/no match.
Starlark is Google's tiny Python-like config language (run via a sandboxed Rust
binding). It expresses the fine-grained "residual" match evaluated *after* the
cheaper NATS-subject and SQL filters have narrowed the candidates. Each evaluation
is capped at ~5ms and fails closed.

**NATS JetStream / NATS subject** — The durable message bus, and its hierarchical
topic strings.
NATS is a lightweight message bus; *JetStream* is its persistent streaming layer,
carrying the signal-notification stream (`legba.signals.>`), lifecycle events, the
dead-letter queue, and work queues. A *subject* is a dot-delimited routing key
(e.g. `legba.signals.<tenant>.<source>.<modality>.<event_class>`) that lets
consumers pre-filter cheaply before finer SQL/Starlark matching.

**coalescing trigger / coalescer** — The rule that waits until enough related
signals pile up, then runs the analyst **once** (not once per signal).
It accumulates matched signals for an (analyst, target) pair and fires when a gate
trips — an accumulation threshold plus a severity gate, clamped by a **cooldown**,
with the **cadence** heartbeat as a floor. LLM analysts are always floored to a
coalesced batch, never run per-signal.
*Note: a read-modify-write "dirty accumulation" upsert can drop concurrent
increments under last-writer-wins; absorbed in practice by the cadence floor.*

**cadence / cadence heartbeat** — The guaranteed periodic firing, so an analyst
still runs when signals are quiet.
A scheduled interval (a cron-derived Dapr reminder, e.g. every 6h/12h/1d) on which
an analyst is re-evaluated — the coverage floor beneath the reactive trigger path.

**cooldown** — A minimum wait enforced between an analyst's firings.
A per-pair minimum interval throttling how often the same (analyst, target) can
fire — the cost governor for expensive LLM analysis.

**CAS-claim** — A lock-free "only one wins" check, so a single worker runs a given
batch even if two paths race.
A compare-and-swap (CAS) operation that lets exactly one worker claim the right to
fire an (analyst, target) batch, so the reactive path and the cadence tick never
double-dispatch the same run.

**worker actors / bounded fan-out** — Helper actors that run a wide analyst's
per-target work concurrently, with a cap.
A primary `AnalystActor` owns the cadence heartbeat and dispatches one run per
matched target to lazily-activated worker actors, bounded by a semaphore — so a
wide analyst over many countries runs in parallel without serializing or
overloading.

**the four planes** — Legba's top-level split: Acquisition, Analysis, Async jobs,
Substrate.
Acquisition (source ownership, baseline enrichment, fan-out/subscription);
Analysis (target subscribers, analyst reasoning, triggers, **agency**); Async jobs
(a NATS work-queue with competing-consumer workers); Substrate (the storage layer).

**job plane / competing-consumer workers** — A background queue where
interchangeable workers each pull one job.
A durable NATS work-queue plus a worker pool and an execution ledger, for heavier
off-actor work. `process_media` is the one live job kind; jobs carry an
idempotency key so duplicates collapse to one execution.

**Dapr Workflow** — Dapr's durable multi-step orchestration that survives restarts.
A deterministic orchestrator yields to non-deterministic activities; the engine
replays event history to resume after a crash. Used for the multi-hour GEPA
optimizer and deep-consult.
*Note: subject to the long-activity round-trip bug above; falls back to a
non-durable in-process loop.*

**seam / declared seam** — A deliberately **unbuilt** feature that is declared and
**fails loudly**.
Legba's honesty mechanism: a capability intentionally not built is registered in
[SEAMS.md](SEAMS.md) with a guard rail that *raises* rather than stubbing or faking
output — enforced by a stub-scanner test. This is the platform's no-stub rule.
*Note: a declared seam does **not** work yet — don't mistake it for a finished
capability, or for a bug.*

---

## Data model

**entity** — A canonicalized real-world thing (person, org, place) extracted from
signals.
A resolved, disambiguated actor stored as one canonical profile (keyed by name +
class, with version history). Different surface mentions ("US", "U.S.", "United
States") are merged into one entity by *entity resolution*.
*Note: entity-resolution fragmentation and NER junk are known open data-quality
gaps in the audits.*

**fact** — An asserted statement (subject, predicate, value) derived from signals
or seeds.
An atomic, temporally-versioned assertion in the `facts` table with `valid_from` /
`valid_until` and a `superseded_by` pointer. Each carries a `source_type`
(`ingestion` / `seed` / `curated` / `proposed`) and a confidence.
*Note: raw ingested facts are stamped confidence 1.0 regardless of trust
("poisoned"), so only **seed/curated** facts are used for grounding.*

**nexus / nexuses** — A first-class, typed, **signed** relationship between two
entities.
A *reified* relationship row (subject →[intermediary]→ object) carrying a relation
type, a **+1 / 0 / −1 polarity sign**, intent, channel, confidence, and validity
time. "Reified" means the relationship is a stored, queryable object rather than an
implicit graph edge. Nexuses feed signed-graph analysis.
*Note: "nexus" is a coined Legba term — it just means a reified, typed, signed
relationship record.*

**proposed_edges** — Candidate relationships awaiting promotion into nexuses.
Provisional untyped edges inferred from entity co-mention, queued for a governance
handler to promote into typed nexuses (or reject as junk, e.g. demonyms).

**temporal facts / supersession** — Facts carry validity time-ranges and can be
replaced by newer facts.
When a newer, differing value arrives, the prior "true now" row is *closed*
(`valid_until` stamped, `superseded_by` set) rather than overwritten — so history
accrues. An open-only partial-unique index keeps exactly one current row per
(subject, predicate, value). This is "temporal honesty": the store answers both
*what is true now* and *what did we believe, when*.

**OutputKind** — The fixed set of typed outputs an analyst can emit.
The ~ten typed kinds (finding, situation, hypothesis, prediction, nexus, fact,
critique, alert, prompt-module candidate…). A registry maps each kind to its
table, pydantic payload model, schema URI, and NATS subject. `analyst_outputs` is
the generic table for kinds without a dedicated table.
*Note: maintainer analysts write fact/nexus/entity straight into their substrate
tables as a **side-write** (see TRACE_ONLY) rather than posting them to the
findings feed.*

**TRACE_ONLY / side-write** — An analyst run that writes its real result straight
into the knowledge tables and records only an audit trace.
Some analysts (fact/nexus/entity maintainers) write results directly into substrate
tables (a "side-write") and emit only a trace row, no feed entry.
*Note: a no-change meta run is forced to trace-only so it doesn't spam the findings
feed.*

**receipt chain / hash-chained receipts** — A tamper-evident log where each run's
record hashes the previous one.
Each analyst run appends a trace row whose `receipt_hash` chains (SHA-256) over the
previous run's hash, so run history can't be silently altered. Periodic
**Ed25519-signed** `audit_checkpoints` sign the chain head for independent
verification.
*Note: the positioning shorthand "Ed25519 hash-chained receipts" refers to this
combination (SHA-256 chaining + Ed25519 checkpoints).*

**content-hashed identity** — A record's version *is* the hash of its content.
Descriptors and signals are identified by a hash of their body, so any change is
automatically a new immutable version — no manual version strings — which also
enables integrity checks and dedup.

**canonical signal / dedup** — The one authoritative row that duplicate signals
are linked to.
Deduplication links duplicates to a single `canonical_signal_id` via a
`signal_aliases` table ("link, never collapse" — raw rows are kept for audit). A
four-tier scheme escalates from exact content-hash/URL match to semantic cosine +
temporal/title similarity via Qdrant.
*Note: the semantic tiers refuse loudly if their embedding/vector ports are absent,
rather than fabricating links.*

**modality** — The medium of a signal: text, image, audio, video, structured, or
binary.
The coarse content type, treated as a first-class axis from ingest onward. A
modality registry binds each one to an ingest extractor and a UI renderer, so
adding a content type needs no schema change.
*Note: some renderers and the media extractors (Whisper/VLM/OCR) are placeholders /
declared seams — the registry slot exists ahead of the handler.*

**grounding / knowledge grounding** — Injecting current facts into an analyst's
prompt so the LLM isn't wrong about recent events.
At run time, the GROUND phase prepends a dated "authoritative current context"
preamble of currently-valid facts (heads of state, alliances) pulled from the
substrate — correcting the hosted LLM's stale training cutoff. Restricted to
still-valid facts (`superseded_by IS NULL`) of **seed/curated** provenance only.
*Note: the designed Tier-2 vector `world_context` is a seam; grounding currently
uses structured seed/curated facts only.*

**seed / seeding** — Curated baseline data loaded into the substrate as
authoritative ground truth.
Importing curated reference data (current leaders, alliances, conflict data)
straight into facts/nexuses marked `source_type='seed'`, tracked in a
`seed_batches` ledger and idempotent on re-run. Adapters include `world_baseline`
(curated) and `wikidata_leaders` (live SPARQL).
*Note: some adapters (e.g. SIPRI) are registered but unseeded; ACLED is paused.*

---

## Analysis & methods

**analyst kind / `method.kind`** — The category of an analyst, and the execution
strategy it dispatches on.
An open taxonomy (~twelve built-in `build_*` branches plus operator-registered
extensions) classifying what an analyst reads and writes (`inline_target`,
`cross_target_raw`, `meta_findings_synthesizer`, `competing_hypotheses`,
`predictor`, `critic`, `optimizer`, `consult_on_demand`, `deterministic`…).
`method.kind` names *how* it reasons (`llm_planner`, `react_loop`,
`stat_forecaster`, `deterministic`, `dspy_compile`…).

**inline_target** — The per-target LLM analyst that assesses one target and emits
a finding.
The base LLM-planner kind: it reads one target's recent signal slice and produces a
first-order finding. Used by **country_assessor** and **world_assessor**, and the
kind that can opt into grounding and **agency** tools.

**country_assessor** — The exemplar per-country analyst.
An `inline_target` LLM analyst that fans out **one run per matched G20 country**
(predicate `has_tag("g20")`, ~6h cadence, grounding opted-in), producing a distinct
**per-target assessment** finding for each country.

**world_assessor** — The exemplar **global** analyst (target-less).
A sibling `inline_target` analyst that **omits `targets`** entirely, so it runs
**exactly once on cadence** and produces a single world-level assessment finding —
not a per-specific-target product. (It is the canary for grounding and cadence
health.)

**META analyst / meta-finding** — An analyst that reads other analysts'
conclusions, not raw signals.
An analyst with no single-target binding that runs once globally (on cadence) over
other analysts' outputs or the whole graph, producing second-order findings
(analysis-of-analysis), e.g. `cross_analyst_correlator` or the
`relationship_reifier`.
*Note: after any change to what an analyst reads, a no-target meta analyst must be
live-forced to confirm it still runs — the e2e suite can miss meta-slice breakage.*

**substrate slice / read slice** — The bounded window of substrate an analyst
reads each run.
The scoped subset (default ~last 24h, scope-filtered, ~50 rows plus peer findings)
an analyst reads per fire, rather than the whole pool. Each analyst kind has its
own reader.

**the 7-phase envelope / GROUND phase** — The fixed deterministic stages wrapping
a single analyst LLM call.
Named stages (WAKE/ORIENT/PLAN/GROUND/REASON/REFLECT/NARRATE/PERSIST) that wrap one
LLM call so a run is reproducible and replayable. The **GROUND** phase injects the
grounding preamble between PLAN and REASON.

**competing_hypotheses / ACH** — A structured method scoring rival hypotheses
against the evidence.
Analysis of Competing Hypotheses (Richards Heuer's tradecraft method): lay out
mutually-exclusive thesis/counter-thesis pairs and score each evidence item against
each on a consistency scale, weighted by *diagnosticity* (how well a piece of
evidence discriminates between hypotheses), to surface the least-contradicted one.
*Built but UNPROVEN — it writes real rows but has no validated skill metric; cell
scoring falls back to a lexical scorer when the token budget is exhausted.*

**hypothesis** — A candidate explanation scored against evidence, stored in its own
table.
Created as a thesis with a mandatory counter-thesis and a running signed evidence
balance (±2 transitions auto-flip it confirmed/refuted), later resolvable against
outcomes.

**predictor / forecast_acute** — A statistical forecasting analyst, and the
hazard-forecast pilot.
The `predictor` kind fits a time-series model (AutoARIMA, falling back to a
naive-mean baseline) over recent signal counts. `forecast_acute` is the pilot
estimating P(≥1 severe hazard) per G20 country at a 7-day horizon via a Poisson
rate model.
*Built but UNPROVEN — **no forecast-skill claim is made**; the pilot currently
reports "degenerate / accumulating".*

**calibration / outcome-resolution** — Checking whether stated confidence matches
real outcomes.
Resolving each prediction/hypothesis against later outcomes to see if the system's
probabilities match reality. The meaningful (*exogenous*) tier resolves against
independent external facts; a weaker `self_consistency_only` tier grades against the
hypothesis's own evidence and is flagged as such.
*Built but UNPROVEN — exogenous resolvers are wired but the live record is
effectively n=0; no validated skill.*

**Brier score / Brier skill score (BSS)** — Standard accuracy yardsticks for
probabilistic forecasts.
**No forecast-skill claim is currently made** — these are the measures the
experimental pilot would have to beat first. The *Brier score* is the mean squared
error between predicted probability and the 0/1 outcome (lower is better); *BSS*
expresses it relative to a baseline (per-country climatology), positive only when
the forecast beats that baseline. The pilot's number lives in a segregated key and
never pools into anything headline.

**degeneracy guard** — A check that withholds a skill claim when forecasts are
trivially near-0 / near-1.
It refuses a forecast-skill claim when the calls are geography-dominated — beating
climatology on "which countries are seismic" is static geography, not anticipating
the future.

**critic** — An analyst that grades other analysts' outputs against a rubric.
A meta analyst using an LLM judge (deliberately a *different* model) to score
another analyst's output per-dimension against an operator-authored rubric — feeding
the optimizer. A heterogeneity guard blocks a model from grading its own output.
*Note: the critic **actuates** — `effective_confidence = min(self-confidence,
critic_score)` — so a poor grade can only auditably reduce surfaced confidence,
never inflate it.*

**optimizer / GEPA** — A self-improvement loop that evolves analyst prompts from
traces and critiques.
GEPA is a reflective, Pareto-frontier prompt-evolution method (run via DSPy in an
isolated worker) that mutates an analyst's prompt module from logged traces and
critiques, producing a champion candidate.
*Built but UNPROVEN; promotion of a champion to the live system prompt is
**operator-gated**, never automatic. `litellm`/`dspy` are barred from the
production inference path — dspy lives only in the opt-in optimizer worker.*

**eval loop** — The analyst → critic → optimizer → calibration self-improvement
cycle.
The cycle by which the system grades its own analysts (critic), evolves better
prompts (optimizer), and checks confidence against outcomes (calibration).
*All three legs are built but unproven research surfaces.*

**consult / deep_consult** — On-demand analysts that answer operator questions over
the substrate.
`consult` answers a free-form question by running a **ReAct** (reason-then-act) loop
over governed read-only substrate tools, returning cited references and stated
uncertainty. `deep_consult` schedules a longer plan → acquire → analyze → synthesize
Dapr workflow and persists the result.
*Note: production consult is governed through the `substrate_read` pack — a tool not
in that pack blocks as `unknown_tool` even though ungoverned unit tests pass. The
Anthropic-plane consult is metered/billed; live tests are run sparingly.*

**structural balance / graph mining** — Signed-graph analyses over the
entity/nexus graph.
*Structural balance* classifies signed relationship triangles as balanced or
"frustrated" ("the enemy of my enemy is my friend"); *graph mining* finds
communities, centrality, brokers, and proxy-chain sign-products. Computed in-process
with networkx over the relational `nexuses` table.
*Built but UNPROVEN advanced-graph research surfaces.*

**JDL data-fusion model (L0–L5)** — A standard reference model for layering data
into higher-level understanding.
The Joint Directors of Laboratories model (signals → entities → situations → impact
→ refinement). [ANALYSIS.md](ANALYSIS.md) uses it only as a *conceptual map* for
where Legba's pipeline sits.
*Note: this does **not** imply sensor-fusion rigor — Legba does situation-assessment,
not track correlation.*

---

## Operations & governance

**action-pack / pack** — A governed bundle of tools, prompts, rules, and budget
caps an analyst may use.
A registrable, versioned bundle granting an analyst specific tools, prompt
fragments, escalation channels, and a **governor** — the sole surface by which an
analyst is granted **agency**. Examples: `substrate_read`, `escalate_finding`,
`web_access`, `propose_facts`.
*Note: production consult tools MUST be in the pack — an entry missing from the live
pack blocks as `unknown_tool`.*

**agency** — An analyst's governed ability to take *actions* / use tools, not just
read and write.
The capability for an analyst to invoke tools mid-run (the GATHER phase) — fetch the
web, propose facts, enqueue jobs, emit alerts — strictly allow-listed and budgeted
rather than hard-coded.
*Note: web text fetched via agency is flagged UNVERIFIED; agent-proposed writes are
PROPOSE-grade (capped confidence, `source_type='proposed'`) and cannot mutate the
control plane.*

**the three-way agency gate (grant ∩ allow ∩ applicability)** — A tool call runs
only where all three permissions overlap.
A pack is *effective* only where the analyst's **grant**, the target's **allow**-list,
and the pack's **applicability** predicate all overlap — enforced by the governor and
defaulting fail-closed.

**governor** — The enforcer that caps each pack's usage and spend.
The `PackGovernorEnforcer` applies per-pack invocation/rate/cost caps and the global
token envelope before each tool call (precall-check → record → settle), logging every
decision to ledgers and emitting an operator-visible event on a block.
*Note: a known seam — a batch reserve can overshoot the pack cap by ≤4 (read-only,
$0 impact).*

**token budget / global token envelope** — Per-analyst and system-wide caps on LLM
token spend.
`budget_tokens_per_day` caps one analyst's daily token use; a system-wide envelope
blocks any pack call once exhausted. On exhaustion the strategy is *demote-and-continue*
to a cheaper fallback model, or — if none is wired — pause loudly until the next budget
window (`BUDGET_THROTTLED`).

**escalate_finding / alert sink** — Promoting a high-severity finding to an outbound
alert.
A pack that fires when a finding crosses an escalation gate (severity ≥ high OR
confidence ≥ 0.85), delivering to alert sinks (NATS, Pushover, XMPP/Matrix) up a
severity ladder, with per-attempt delivery audit rows.

**emit / output binding** — Post-write dispatch that turns a stored output into an
external artifact.
Best-effort handlers that serialize a written output to an external format/sink — a
STIX 2.1 bundle (optionally over TAXII 2.1), an alert, a webhook, a NATS stream, an
A2A envelope, or MCP — *degrade, don't drop*, so a sink failure never blocks the
durable write.
*Note: some emit surfaces (TAXII push, the A2A skill router) are off-by-default
declared seams.*

**SSRF guard** — Protection that blocks outbound fetches to internal/private
addresses.
An `SsrfGuardedTransport` refuses agency web fetches to loopback, RFC-1918 private
ranges, link-local, and the cloud-metadata IP (169.254.169.254), raising a clean tool
failure. The planner controls the *query*, never the *endpoint*.

**discovery / discovery template** — Materializing sources and targets from a
template, rather than ingesting signals.
A pipeline that creates source/target *instances* from one template — e.g. the G20
country targets are all produced from a single template, which is why there is **no
per-country code**.

**lifecycle FSM** — The state machine every descriptor moves through.
`draft → configured → active → paused → retired`, with per-state hooks, separating
*register* from *configure* from *activate*. The registrar advances a freshly-registered
descriptor to declared-active on first register.

**stack / stack registry** — The registry of backing services (Postgres, NATS,
models…) that descriptors reference.
Registry-managed descriptors for shared substrate components (Postgres, NATS, Qdrant,
vault, LLM providers, embedder), with credentials held separately in the **vault**.
Descriptors bind via a `StackRef`.
*Note: a shared-schema change requires rebuilding **both** `legba-runtime-dapr` and
`legba-registry`; a stale registry silently stops analysts firing.*

**credential vault** — An encrypted store for source and model secrets.
A `CredentialVault` using NaCl SecretBox (authenticated encryption via PyNaCl),
decrypted with an environment-provided master key kept out of image layers. Descriptors
hold only a secret *pointer*, never plaintext.

**DLQ / dead-letter** — A queue where failed/invalid items are parked instead of
lost.
A holding store (`descriptor_dead_letter`, `output_dead_letter`) for malformed or
failed descriptors/outputs, so a bad item fails cleanly and is available for operator
resubmission rather than half-landing or corrupting a table.

**bringup / registrar / catalog** — The deploy-time scripts that register the live
descriptor set.
Scripts that push descriptors into the registry, so the live source/analyst/target set
is the database rows (the "catalog"), not the YAML files.
*Note: re-registering to a live runtime needs the correct DB and python entrypoint, and
the registrars are **create-only** — model changes go via the registry `PUT` API.*

**single-tenant / `owner_tenant`** — Legba runs for one operator; the tenant tag is
forward-compat metadata.
`owner_tenant` is stamped on sources/signals/outputs but is **not** an enforced
isolation boundary today — Legba ships single-tenant, single-operator, single-node.
*Note: RBAC / SSO / multi-tenant / row-level security are designed but explicitly NOT
built.*

**security perimeter (Caddy basic-auth)** — The single outer boundary: a reverse proxy
with HTTP basic auth.
Caddy serves the operator UI over automatic HTTPS behind HTTP basic authentication and
proxies `/api` to the registry; internal services bind to loopback. Registry endpoints
additionally require a bearer token and fail closed if it is unset.
*Note: this is the whole perimeter — there is no RBAC/SSO; OIDC forward-auth is designed,
not built.*

**self-hostable / AGPL** — You run it on your own infrastructure, under the AGPL-3.0
license.
Released under the GNU Affero GPL-3.0-or-later; its §13 network clause requires offering
source to users of a network service run on modified code, and copyleft keeps derivatives
open. Self-hostability under AGPL is part of the stated moat; commercial/dual-licensing is
intended (a CLA is needed before outside contributions).
*Note: AGPL "source-available" is a licensing fact — distinct from the "source-first"
acquisition architecture.*

**cold-start verification set** — The smallest feed set that proves the pipeline works
from empty.
A minimal 3-feed bootstrap (BBC World, Deutsche Welle, Al Jazeera) that verifies the whole
source → enrich → fan-out → assess loop from empty data volumes, before scaling to the full
source catalog. See [SETUP.md](SETUP.md) §7.
*Note: if the registry is empty at boot, the NLP client stays null and enrichment silently
does nothing — seed/register **before** relying on enrichment.*
