# Legba — Implementation Design

**Scope:** the authoritative in-repo implementation design for the source-first
Legba platform. This is the cold-start orientation for engineers and operators
walking up to the repo: what each subsystem is, how the pieces fit, and where to
look in the code. It pairs with `docs/RUNBOOK.md` (operations), `docs/UI.md`
(the operator console), and `docs/ARCHITECTURE.md` (the conceptual orientation
and design rationale).

Sections marked **(future seam)** describe declared shape that the code carries
but does not yet exercise end-to-end; everything else is live.

---

## 1. Frame

### 1.1 What Legba is

Legba is a **decompositional intelligence system**: it turns a firehose of
sources into **cited, verified, drillable reports** over whatever domain you
configure. Three kinds of declarative **descriptors** — sources, targets,
analysts (plus action-packs) — are registered into a shared **substrate**, and a
**Dapr virtual-actor runtime** turns each descriptor into a running actor that
reads and writes that substrate. The geopolitical (G20 + high-consequence
watch-tier) configuration is the shown exemplar, not a lock-in; the same machinery hosts attack-surface
monitoring or a data-center watch as a different *set of descriptors*.

What sets Legba apart is the **discipline**, not the data: every claim in the
product is cited to a source, checked by a mandatory faithfulness pass, and
auditable through a receipt chain back to the originating signal. It does **not**
claim to measure *truth* — it measures **groundedness**: does each claim follow
from the evidence it cites? That is a narrower, honest guarantee, and saying so
is the point.

**The acquisition mechanic — ingest once, enrich once, match many:**

- A **source** owns acquisition. Its actor polls or receives push, produces
  **one canonical, target-agnostic signal** per observation, enriches it once
  (baseline language / geo / entity NER), and publishes it once to NATS
  JetStream. Signals are *observations*, not interpretations — they carry no
  `target_id`.
- A **fan-out plane** routes each published signal to the **many** targets that
  subscribe to it, by predicate.
- A **target** is a passive subscriber: a declarative "what to watch" that
  references shared sources and slices their signals by predicate.
- An **analyst** coalesces the signals matched for a (analyst, target) pair and
  produces findings / meta-findings / situations / hypotheses / nexuses /
  scorecards, each with full `derived_from` provenance and a per-analyst
  **SHA-256 hash-chained receipt chain** (badge: "chain-consistent
  (single-node)" — an integrity chain, not a signed or distributed guarantee;
  §9).

**The analysis spine (the product), composed bottom-up.** The trusted output is
*not* one model's verdict — it is a composition of small, individually-checked
reads:

1. **Four bounded reasoning units** (`inline_target` LLM analysts —
   `leadership_transition`, `energy_security`, `escalation`,
   `narrative_coordination`), each fanned out to every **country desk** by a
   `has_tag("g20") or has_tag("watch")` predicate (the 19 G20 desks plus a
   5-country high-consequence **watch** tier — 24 desks total), each answering
   **one** narrow question over a cited 72-hour signal slice plus an
   accumulated-facts grounding preamble, each ending in a **mandatory
   faithfulness verify** (§3.5).
2. **Per-country composition** (`country_composition`, a
   `meta_findings_synthesizer`) reads the four *verified* units for a country and
   writes a hedged, cited synthesis; an unverified sub-claim never enters it.
3. **World composition** (`world_assessor`, repointed to
   `meta_findings_synthesizer`) composes over the per-country compositions into a
   cited, hedged world view that drills country → units → source. It is *not* the
   old verdict-from-nowhere monolith; that framing was retired.
4. **A banded scorecard** (`scorecard_producer`, §3.6) writes one deterministic,
   rule-derived band per active country desk over already-verified claims (banded
   across a 14-day window), and an honest **skill scoreboard** publishes per-unit
   eval + calibration results — including the results that show *no* skill.

**Design principles (why it is shaped this way):**

- **Decomposition.** A narrow question with a bounded slice is checkable; a
  single "assess this country" monolith is not. The product is assembled from
  units that can each be scored, superseded, and drilled independently.
- **Measure + verify before autonomy.** The verified, cited synthesis is the
  floor; the more autonomous legs (self-optimization, forecasting) are held at
  the top and **return only as measured experiments** with a real before/after
  number and a human gate (§3.6, §6.7). An unverified producer is not worth
  self-tuning.
- **Honesty contracts.** Where a capability is weak today, the system says so
  *in the product surface*: a unit with low faithfulness reads
  "low-faithfulness", a scorecard dimension with no qualifying claim reads
  "insufficient-evidence" (never a fabricated band), and the forecasting pilot
  publishes "no proven skill" rather than a number. `docs/SEAMS.md` is the
  authoritative not-built / frozen list.

Everything composable is a descriptor; the runtime executes whatever is
registered. Adding a country, a feed, or an analysis pattern is a registration,
not a deploy.

### 1.2 Where it sits

A single instance hosts many domains at once — geopolitical awareness, attack-
surface monitoring, data-center watch — as different *sets of descriptors*, not
different deployments. The substrate, registry, runtime, and outputs are shared;
the descriptors specialize. AI models are hosted out-of-process via the
`legba-models` service (§10); LLM providers are resolved through the
stack registry.

### 1.3 Proven state

A cold start from empty volumes (the single `0001_baseline.sql` schema migration)
brings the full loop up: roughly 50 poll sources on cron (RSS / API / bulk — the
BBC / Deutsche Welle / Al Jazeera feeds are the canonical RSS exemplars) →
enriched signals (geo + language + entity classes promoted to indexed columns) →
fan-out on `legba.signals.>` → the country desks (19 G20 + a 5-country watch
tier). The **reactive acquisition
path** (source polls → fan-out → coalesced trigger fires → analyst run) is
**proven live in the real stack** — most recently re-verified after the
`dapr-scheduler` embedded-etcd fix that restores reminder recurrence (§6.3 /
RUNBOOK §0); before that fix the loop fired once at boot then went silent.

On top of that path the full **analysis spine (§1.1) is live**: the four bounded
units → per-country composition → world composition → banded scorecard, each cited
and each unit checked by the mandatory faithfulness pass, drillable to source. The
older monolithic per-country analyst (`country_assessor`) is **retired and
stopped** — nothing in the trusted spine reads it (the composition reads the four
verified units), and it was the single largest producer of unverified one-pager
output (`docs/SEAMS.md` #35); its ~1.2k historical findings remain in the DB,
unread (not a clean slate). The forecast-as-claim predictors (`country_predictor`,
`india_energy_predictor`) are **retired / frozen and stopped** (~539 historical
prediction rows remain), and the monolithic self-optimizer (`country_optimizer`)
is **cadence-frozen** (its descriptor is still `state=active`); the forecasting and
self-optimization legs return only as the measured experiments in §3.6 / §6.7.

An entity knowledge graph (`entity_profiles` / `signal_entity_links` /
`proposed_edges`) is kept current by an ongoing `entity_resolution` deterministic
analyst; finding-level supersession (§7.2) keeps the per-situation finding set
from accumulating near-dups each cadence cycle; a temporal `facts` / `nexuses`
substrate (~4.6k facts, ~4.9k nexuses and growing) accumulates over time and
feeds the units' grounding preamble (§3.4).

---

## 2. The four planes

Legba factors into four planes. The first three are runtime; the fourth is
storage.

1. **Acquisition** — `SourceActor` pulls/receives, runs baseline enrichment once
   per signal, writes the canonical signal to the `signals` table, and publishes
   it onto the coarse `legba.signals.<tenant>.<source>.<modality>.<event_class>`
   subject (captured by the `legba_signals` JetStream stream). The
   subscription/fan-out engine resolves which targets subscribe to which sources
   and binds per-target JetStream consumers subject-filtered to that taxonomy.
2. **Analysis** — `TargetActor` is the subscriber identity the fan-out delivers
   to; `AnalystActor` reads its matched slice and runs its method. A coalescing
   **trigger engine** (live + reactive — §6.6) decides *when* an analyst
   fires (cadence + accumulation + severity, clamped by cooldown), dispatching a
   coalesced fire to the analyst actor for *any* method kind, LLM included. An
   **agency** plane lets an analyst resolve → govern → dispatch an action-pack
   tool mid-run.
3. **Async jobs** — a NATS work-queue (`legba.runtime.jobs.JobQueue`) with a
   competing-consumer `JobWorkerPool`. `process_media` is the live job kind;
   landed derived signals re-enter the fan-out → trigger path. (Source
   discovery runs via the registry route, not the job plane; job-based
   deep crawl is a designed direction item — `docs/DIRECTION.md`.)
4. **Substrate** — Postgres (relational; the operative knowledge graph is the
   relational `nexuses` table + networkx, with the Apache AGE graph dormant —
   ARCHITECTURE.md §5.5), Qdrant (vectors),
   Redis (hot state + caches), NATS JetStream (event bus + durable streams + KV).

The production host (`legba-runtime-dapr`) boots all four:
`runtime/dapr_host.bring_up_production_runtime` wires the substrate connections,
the reconcile loop, and the deps resolvers; `runtime/source_first_runtime.
bring_up_source_first_planes` assembles the job, subscription, trigger, and
agency planes on top.

---

## 3. The actors

All actors are Dapr-native (`dapr.actor.Actor`), hosted in one process
(`runtime/dapr_actors.py` for Target/Analyst; `runtime/source_actor.py` for
Source). There is no embedded asyncio host — `daprd` routes every
`ActorProxy` invocation to the host's FastAPI surface on port 6090.

**Identity.** Dapr addresses an actor by `(actor_type, actor_id)`. The
`actor_id` grammar is `kind::descriptor_id::content_hash[:16]` — the sole
constructor is `runtime/reconcile._default_actor_id`. The descriptor identity
(id + version) is recoverable from the id, so clients need no lookup table.

**State.** Each actor persists a `record` (lifecycle, last run, outcome, error
counters, cooldowns) via `self._state_manager`, backed by a Postgres Dapr state
component on the isolated `dapr` database. State survives sidecar restarts.

**Dependency wiring.** Dapr's actor factory can't take custom kwargs, so heavy
deps live in a process-global registry keyed by `actor_id`. On a cache miss
(e.g. post-restart, before the host re-registers), a process-registered async
**resolver** reconstructs the deps from the descriptor via the registry's REST
surface and caches the result. The fallback is gated by
`LEGBA_DEPS_FALLBACK_ENABLED` (default on).

### 3.1 SourceActor — acquisition

Owns one `SourceDescriptor`. Pulls/ingests **once** regardless of consumer
count, writes one canonical `Signal`, publishes it. Two acquisition modes:

- **poll** — at activation registers a Dapr **Reminder** from `cadence.schedule`
  (durable across restarts). Each fire pulls the handler, runs the per-source
  baseline enrichment, writes via `write_canonical_signal`, publishes, and
  persists the cursor.
- **push** — registers the handler against the shared inbound-webhook router; an
  inbound POST wakes the handler, which emits Signals through an `emit_signal`
  callback onto the same baseline → write → publish path.

Either mode runs the **source-side ingest dedupe** (tiers 1–2 — canonical-URL
hash then content hash) after the canonical insert, built from the descriptor's
`pipeline.ingestion_filters`; a duplicate raw row is *linked* to its canonical
(`signal_aliases` + `canonical_signal_id`), never collapsed (§7.1). This is the
cheap deterministic counterpart to the pool-wide `cross_source_dedup` analyst.

A source may also register an outbound **upstream watch** at activation
(`provision` block) and deprovision it on retire, idempotently. The core logic
lives in a plain `SourceCore` class (directly testable without a sidecar); the
`SourceActor` wrapper delegates to it.

The baseline enrichment chain (`descriptor.pipeline.enrichment`:
`language_detect` / `geocode` / `ner_multilingual` / `classify`) is built once by
the host (which owns the NLP / Qdrant / embedding clients) and threaded in. After
enrichment the host promotes payload annotations (`payload.geo.country_iso2`,
`payload.language`, `payload.entities[].class`) into the **typed, indexed**
`signals.geo` / `signals.language` / `signals.entity_classes` columns — the
coarse axes the fan-out and per-target scoping match on. Enrichment is
annotate-only and best-effort: a filter error never drops the signal.

**Tenant-consistency invariant.** The fan-out publishes the *in-memory* Signal
(`model_dump_json`), so any field the write path pins on the row must also be
stamped on the object — specifically `owner_tenant`, which is set from the
source's `scope.owner_tenant` before write *and* publish. This matters because
the subject (`legba.signals.<tenant>.…`), the subscription binding, and the
real-time matcher (`subscription/filter.matches`) all key on `owner_tenant`:
if the published envelope's tenant disagreed with the row's, the durable
consumer would still *deliver* every signal but the matcher would *reject*
every one on its tenant guard, silently disabling all reactive triggering while
the batch/cadence path (which reads the row) kept working. Source acquisition
therefore stamps the row, the subject, and the envelope from one source of
truth.

### 3.2 TargetActor — subscriber + discovery host

A target is a passive subscriber. The TargetActor stays registered for two roles:

1. **Subscriber identity** the subscription engine fans out to.
2. **Discovery materialiser host** — when the descriptor carries a `discovery`
   block, `run()` routes to `_run_discovery_cycle` instead of an ingest path:
   it resolves the discovery handler, drains its candidate stream, and hands the
   list to `registry.discovered_materializer.reconcile_discovered_targets`,
   which upserts L1 child target descriptors and retires vanished ones (guarded
   by a disappearance-ratio threshold).

A target's `sources` are `SourceRef` subscriptions, not target-owned pull
bindings; the target does not poll sources itself.

### 3.3 AnalystActor — reasoning

Owns one `AnalystDescriptor`. The run path reads the analyst's matched substrate
slice (a per-kind `read_slice` reader), invokes its `run_method` (LLM or
deterministic), enforces budget, writes the typed output via the provenance
writers, and records into the receipt chain.

Two firing paths:

- **Coalesced fire** — the trigger engine dispatches `run({"trigger_kind":
  "coalesced_fire", "target_filter": <target>, ...})` for a (analyst, target)
  pair that crossed its threshold.
- **Cadence heartbeat** — a Dapr Reminder from `cadence.fallback_schedule`
  guarantees coverage even when too few signals arrive to trip the trigger. A
  **target-bound** analyst (one whose `subscription.targets` selector matches
  targets) fans out **one run per matched target**, each with an independent
  per-(analyst, target) cooldown — this is what produces per-country findings
  instead of one global blob. A **meta** analyst (no target binding) does a
  single global run.

**Per-(analyst, target) concurrency.** A target-bound analyst is a *primary*
actor that does not assess inline — it fans out to **per-(analyst, target)
worker actors**, addressed `analyst::<descriptor_id>::<target_id>`. The primary
dispatches the matched targets to their workers via `ActorProxy`, **bounded
concurrent** (a semaphore of `_FANOUT_CHUNK`, default 5) so a wide analyst
(e.g. an `inline_target` unit like `energy_security`, or `country_composition`,
each fanned across the ~24 active country desks — 19 G20 plus a 5-country watch
tier) runs its per-target work in
parallel instead of serializing through one actor's turn queue. Workers are
**lazy-activated**: they carry no descriptor of their own and are only ever
reached via the primary's fan-out or a coalesced fire, which hands them a
`target_filter` + resolved deps so they create a minimal active record and run.
Both cadence fan-out and coalesced fires land on the same worker path.

Per-(analyst, target) cooldowns live in the actor record (`cooldown_by_target`),
keyed by `target_filter`; a global cooldown gates the whole analyst (e.g. on
budget exhaustion). Demotion-on-budget and the typed retry policies (§6.5) also
live here.

**Per-phase LLM split (optional second handler).** The deps builder
(`runtime/analyst_deps_builder.py`) resolves `method.llm.primary` into the run's
LLM handler. A descriptor may additionally declare an **optional**
`method.llm.narrate` StackRef (`method.llm` is an open dict, so this needs no
schema change); when present the builder resolves a **second** handler so a
method can run its heavy investigation phase on one model and a separate
synthesis/voice phase on another. The journal (§7.6) is the live consumer: its
agentic GATHER loop runs on the local primary plane while its NARRATE voice runs
on the Anthropic plane. When `method.llm.narrate` is absent the second handler
stays unset and every analyst falls back to the single primary handler,
byte-unchanged.

### 3.4 Knowledge grounding — analysis-time current-world-state injection

**Problem.** The analyst plane's core LLM carries a training cutoff that predates
the present. For an assessment task that turns on *current* world state —
who holds office, which alliances are in force, the present state of an ongoing
conflict — the model backfills from a stale prior. The live failure that forced
this design: the assessor called the current US president a "former" president,
because its training data predates the 2024 election, and the signal slice (recent
news headlines) rarely restates a background fact like "X is the head of state".
The model had no in-context correction.

**Design rationale — the substrate is already the grounding store.** Legba already
stores the temporally-honest answer. The temporal `facts` rows (`valid_from` /
`valid_until` / `superseded_by`, §7.2 / `ANALYSIS.md` §7.2), the reified `nexuses`
(typed relationships), and the **seed roots** (the curated `world_baseline`
adapter and the live Wikidata leaders adapter) carry "who holds office now" and
"which blocs are in force now" as first-class, supersession-aware data. So the fix
is *not* a new store, a fine-tune, or a RAG index — it is **curate the current data
in, then inject it at analysis time** over the substrate that already exists. This
keeps the model swappable (grounding corrects whatever cutoff the bound model has)
and keeps the ground truth auditable (it is curated/seed `facts` rows with full
provenance, not prompt text).

**The three tiers.**

- **Tier 0 — curate current data in.** The `wikidata_leaders` seed adapter
  (`src/legba/data/seed/adapters/wikidata_leaders.py`) pulls current heads of
  state/government from live Wikidata SPARQL and emits a **country-subject** office
  fact (`'<country>' | 'head of state' | '<leader>'`), keyed on the country so a
  leader change *supersedes* the prior officeholder (`valid_until = now` +
  `superseded_by`) via the Phase-B `valid_until` write path — keying on the person
  would leave two open "current" rows. The curated `world_baseline` adapter emits the
  same shape, so a fresh Wikidata pull supersedes a stale curated leader. (Honesty
  detail: some Wikidata entities have no SPARQL-resolvable English label — the
  live case is Q22686 / Donald Trump — so a bare QID is resolved via a
  `wbgetentities` label lookup + enwiki-sitelink fallback, and a still-unlabelled
  entity is dropped rather than emitted as an unreadable `Qxxxx`.)
- **Tier 1 — inject at analysis time.** A descriptor opts in with a `GroundingBlock`
  (`src/legba/data/schemas/analyst.py` — `grounding: {enabled, scope, sources,
  max_facts}`, off by default). The deps-builder
  (`analyst_deps_builder._build_grounding_hook`) installs a per-run hook backed by
  `SubstrateGroundingResolver` (`src/legba/runtime/grounding.py`); the
  `inline_target` **GROUND** phase prepends a dated "AUTHORITATIVE CURRENT CONTEXT
  (as of <today> — treat as ground truth over prior knowledge)" preamble built from
  the **current** authoritative facts (the temporal-honesty gate `superseded_by IS
  NULL AND (valid_until IS NULL OR valid_until > now())`, preferring `seed`/`curated`
  provenance) about the target geo + top slice entities. It is degrade-not-drop (a
  read failure leaves the prompt untouched), token-capped (`max_facts`), and skips
  bare-QID values so it never injects an unreadable line. Opted in on the **four
  bounded units** (`leadership_transition` / `energy_security` / `escalation` /
  `narrative_coordination`, `grounding.enabled: true`) and the journal (§7.6); the
  compositions (`country_composition` / `world_assessor`) instead compose over the
  units' already-verified findings rather than a raw preamble.
- **Tier 2 — vector `world_context` collection** (declared **future seam**). A
  curated unstructured-brief collection for free-text background the structured
  facts can't carry; the `GroundingBlock` accepts `vector:world_context` as a source
  so descriptors can pre-declare it, but the resolver acts only on the structured
  `substrate` source until the embedder-through-port wiring (L-114) lands.

`ANALYSIS.md` §7.9 is the in-depth narrative; `AI_MODELS.md` §6 explains why this
sits where it does relative to the bound model (it mitigates the model's training
cutoff with substrate-sourced ground truth).

### 3.5 The mandatory faithfulness verify pass — measuring groundedness, not truth

The load-bearing honesty contract: a cited finding is not trusted until a
**faithfulness verify pass** has scored it. This measures **groundedness** — does
each cited claim follow from the evidence it cites — **not** truth; the system
never asserts a claim is *correct about the world*, only whether it is
*supported by what it cites*.

Two checks combine:

- A **deterministic citation-presence floor.** The synthesized prose carries
  inline `[N]` citation markers mapped to the signal ids in the assembled slice;
  a claim with no resolvable citation floors to unsupported. (The marker parser
  normalizes full-width / variant citation brackets before matching — a real
  live failure class where a model emits `【3】`/`［3］` instead of ASCII `[3]`.)
- An **LLM judge.** A descriptor that declares `method.llm.verify` and runs with
  `LEGBA_VERIFY_LLM_JUDGE` on has each cited finding scored for faithfulness in
  `[0,1]` by an LLM judge. **Currently that judge is the SAME core model**
  (`llm.primary.openai_compat`, gpt-oss-120B) that produced the finding — it is
  **not** cross-family. This is a deliberate, temporary choice: the earlier
  cross-family 8B judge (`llm.verify.slm_8b`, "legba-slm", Llama-3.1-8B) proved
  too weak (harsh + mis-aimed), so the strong reasoning model runs the judging to
  prove the flow. **Known limitation:** a model grading its own house style shares
  its blind spots, so the faithfulness signal is weaker than an independent
  cross-family judge; the deterministic floor and the signed provenance chain
  still backstop it, and a dedicated reasoning judge is planned. If the judge is
  unresolved the pass **soft-fails to the deterministic floor** rather than
  silently passing everything.

The score is folded, not enforced destructively:
`effective_confidence = min(confidence, faithfulness_score)` is computed at read
time (`runtime/actor_critic.py`) and gates a **visible low-confidence tier** — a
weakly-grounded finding is demoted and labelled, never hard-deleted. A planted
fabrication (a claim with no supporting cited evidence) is flagged unsupported.
The four units carry `method.llm.verify`; so do both compositions, and
`country_composition` **INNER JOINs on the faithfulness critique** so an
unverified sub-claim is structurally unable to enter the per-country synthesis.

### 3.6 The banded scorecard, the skill scoreboard, and the measured experiments

**Banded scorecard (`scorecard_producer`, `deterministic` META).** A deterministic
handler (`data/analysts/deterministic_handlers/scorecard_banding.py`) reads
already-verified claims over a 14-day window and side-writes **one banded
`scorecard` row per active g20/watch country desk** (the 12th `OutputKind`, §7.2).
Bands come
from high-precision rules (severity tag × `effective_confidence`,
**demote-never-promote**); every band names the verified-claim id it rests on. A
dimension with no qualifying verified claim reads **"insufficient-evidence"** with
an explicit reason — never a fabricated band — and a per-claim faithfulness below
the floor demotes to **"low-faithfulness"**. The live scorecard is deliberately a
*mix*: some countries band, and some (e.g. the US) read all-insufficient because
that unit's faithfulness is genuinely low today. That is the contract working, not
a gap to paper over.

**Skill scoreboard (honest-null everywhere unmeasured).** Per-unit eval combines a
faithfulness number with correctness-vs-reference (`unit_correctness_scorer`),
reported honest-null where unmeasured; plus the exogenous calibration Brier and the
acute-forecast BSS. A no-skill or insufficient-sample result is **published, not
hidden**. Today the correctness-vs-reference gold set is tiny (n=1, reported
insufficient-sample), and the acute-forecast pilot reports **no proven skill** (a
degenerate sample; skill withheld) — both surfaced as such.

**The measured experiments (measure-before-autonomy in practice).** The two
ambitious legs return *only* as measured, gated experiments:

- **GEPA self-optimizer → `unit_optimizer`.** The old monolithic
  `country_optimizer` stays **cadence-frozen** (its `cadence.fallback_schedule`
  is nulled so it fires on no tick, though its descriptor is still `state=active`
  — no reminder-flood regression; §6.7 / SEAMS #30). GEPA returns scoped to
  **one** measured unit
  (`leadership_transition`) as a `unit_optimizer` descriptor whose every candidate
  carries a **real before/after paired faithfulness delta** measured on the same
  faithfulness judge (currently the core model, not cross-family; a live run read
  parent 0.34 → candidate 0.29, delta −0.05). It stays
  `promotion_gate=human_gated` and can **never** auto-promote on a
  degenerate / absent / non-positive delta (`optimizer.should_auto_promote` runs
  its measurement gates first; guarded by the P4-T8 honesty suite).
- **Forecasting → an acute-forecast scoreboard.** The forecast-as-claim
  predictors (`country_predictor`, `india_energy_predictor`) are **retired /
  frozen and stopped** (SEAMS #31–#32; ~539 historical prediction rows remain).
  Forecasting returns only as a precise-question
  `acute_forecasts` Brier / BSS scoreboard (question + window + probability +
  auto-resolve), surfaced **solely** on the calibration route
  (`GET /api/v1/v3/eval/calibration`), **never** as a free-text claim or finding.
  A geography-dominated / degenerate probability vector **abstains** (zero rows),
  and skill is withheld until a non-degenerate at-sample BSS is positive — so it
  currently reports no skill, honestly.

---

## 4. The descriptor model

Four descriptor families share one shape: content-hashed, versioned,
lifecycle-FSM'd, audited, registry-managed. Code: `src/legba/data/schemas/`.

### 4.1 Families

| Family | Schema | Owns |
|---|---|---|
| **source** | `schemas/source.py` `SourceDescriptor` | acquisition: kind, `acquisition` (poll/push), cadence, provision, baseline pipeline, output stream policy, `subscription_policy` |
| **target** | `schemas/target.py` `TargetDescriptor` | what to watch: polymorphic scope, `sources` (`SourceRef[]`), pipeline, optional inline analyst, outputs, `allowed_action_packs` |
| **analyst** | `schemas/analyst.py` `AnalystDescriptor` | how to reason: kind, subscription, mapping, method, cadence, outputs, `action_packs`, eval, optional `grounding` (§3.4 knowledge grounding) |
| **action_pack** | `schemas/action_pack.py` `ActionPack` | a capability bundle: tools, prompt-fragments, rules, channels, governor, applicability |

All four enforce `strict=True, extra="forbid"`, a `schema_uri` of the form
`legba/<family>/<MAJOR.MINOR.PATCH>`, and a `version` that is the content hash.

### 4.2 The source-first contract (sources ↔ targets)

The wiring between a source and a target is fully declarative
(`schemas/source.py`):

- **`SourceRef`** (on a target) sets *exactly one* of `source_id` (subscribe to a
  named source) or `source_selector` (auto-wire any source whose **scope**
  matches), plus a `subscription`.
- **`Subscription`** slices a source's signal stream: a structured filter
  (`geo` / `languages` / `tags` / `entity_classes` / `modalities`, indexed and
  pushed to the coarse NATS subject + a SQL `WHERE`) plus an optional Starlark
  **residual** predicate (the long tail: `mentions()`, `severity_at_least()`,
  …). `canonical_only` (default true) makes delivery dedup-aware.
- **`SourceSelector`** matches *source scope* (not signals) — a coarse query over
  source-descriptor metadata deciding whether a discovered source joins the
  target. Only `open` sources auto-wire; `allowlist` / `grant` need explicit
  opt-in. `owner_tenant` scopes which sources a target's selector will match on
  the acquisition plane (forward-compat metadata; Legba ships single-tenant —
  this is not an enforced multi-tenant isolation boundary, see
  `docs/DIRECTION.md` §0–§2).
- **`subscription_policy`** (on the source: `open` / `allowlist` / `grant`) is
  enforced at subscription-registration time.

### 4.3 Polymorphic target scope

`TargetScope` is a discriminated union on `domain` (`schemas/target.py`):

- **`GeoScope`** — geopolitical / OSINT: `geo`, `languages` (the founding case).
- **`EstateScope`** — asset-estate (ASM / customer estates): `customer_id`,
  `asset_tags`, `cloud_accounts`.
- **`EntityScope`** — single person / org / asset: `entity_ref`.

Shared `_ScopeBase` fields: `entity_classes`, `relationship_types`,
`time_horizon_days`, `tags`, and a compile-checked Starlark `predicate`. A target
watching one person no longer fakes `geo: ["XX"]`.

### 4.4 Property factories

Descriptor config fields use named factories (`schemas/properties.py`) so the
runtime knows enough to render UI, validate, store credentials, and resolve
dynamic values without each descriptor reinventing them:
`Property.Secret(name)` (vault reference — never plaintext), `Property.StackRef`
(stack-component reference), `Property.Text/Number/Cron/RateLimit`,
`Property.Dropdown.Static/Refreshable`, `Property.List/Dict`, `Property.OAuth2`,
`Property.Free`. On the wire a factory value serializes as a small dict carrying
`raw` + `factory_kind`; the runtime unwraps it before handing config to handlers.

### 4.5 L1 / L2 / L3 tiers

`abstraction_level` on every descriptor:

- **L1** — a concrete instance with every field explicit.
- **L2** — a curated template with sensible defaults for one category
  (e.g. a country template). Inherit-from-able via `inherits` (single-level,
  multiple bases, left-to-right precedence; the descriptor overrides bases).
- **L3** — a blessed pattern that materializes *many* L1 descriptors as a
  coherent set.

A descriptor carrying a `discovery` block must be L2/L3 (enforced by a model
validator) — it is a template, not an instance.

### 4.6 Lifecycle FSM

`draft → configured → active → paused → retired` (plus `resume`), in
`runtime/lifecycle.py`. `configured` is the explicit "validated, schemas
resolved, components bound, ready but not running" state — `register ≠ configure
≠ activate`. Transitions persist through the registry and emit NATS events on
`descriptor.<state>.<family>.<id>`. Each actor drives its own FSM on activate /
pause / retire.

### 4.7 Discovery (template → instances)

An optional `discovery` block turns a descriptor into a Prometheus-service-
discovery-style template that materializes N instances from a dynamic candidate
list. The block carries the discovery `kind`, a `list_source`, a typed `relabel`
chain (rewrites each candidate's label set into template variables), and a
`resync_policy` (disappearance-ratio threshold, default 30%, to stop a flaky
list source from cascading mass-retirements). Both targets (`DiscoveryBlock`)
and sources (`SourceDiscoveryBlock`) support it; the source flavor adds
`validate_before_register` (liveness + trial pull before a candidate becomes a
source). The operator-facing **Discovery Pipeline** panel (§5.5) renders these
discovery descriptors, their candidate lists, and materialised instances.

### 4.8 Analyst kinds + action-packs

`AnalystKind` is an **open taxonomy**: twelve built-in kinds (`inline_target`,
`cross_target_raw`, `meta_findings_synthesizer`, `cross_analyst_correlator`,
`relationship_reifier`, `competing_hypotheses` (alias `ach`), `deterministic`,
`predictor`, `critic`, `optimizer`, `consult_on_demand`, `deep_consult`) plus
runtime extensions registered through `ANALYST_KIND_REGISTRY` (mirrored from
`vocabulary_entries` rows with `family='analyst_kind'`) — most notably the
`journal_assessor` **extension kind** (§7.6), which is registered via
`register_analyst_kind` and *not* a member of the closed built-in enum, so the
built-in count is unchanged. The `method.kind`
(`llm_planner` / `llm_single_turn` / `deterministic` / `hybrid` / `react_loop` /
`stat_forecaster` / `critic` / `dspy_compile`) is what the runtime dispatches on.

**Coalescing runs for deterministic and LLM-bearing analysts.** An
`ActorTriggerRunner` dispatches a coalesced fire to the analyst actor for any
method kind; LLM analysts fire on the accumulation / cadence gates, never
per-signal (the per-signal guard lives in the policy, not the runner).

**Action-packs** are the agency-grant surface (superseding any flat tool
whitelist). A pack bundles `tools` (each optionally an `async_job` onto the job
plane), `prompt_fragments`, `rules`, `channels` (output bindings), a per-pack
`governor` (budget + rate caps), and an applicability predicate. Effective
capability is the intersection `analyst.action_packs ∩
target.allowed_action_packs ∩ pack.applicability`, gated by the governor. Seed
packs: `media_processing`, `incident_response`, `substrate_read` (the consult
kind's governed read tools), and `escalate_finding` (the example pack that
fires on gated findings). (The `discovery` pack was retired per decision F-1.)
The dispatch path
(`data/analysts/agency/agency.py`) is **resolve → govern → dispatch**: the
`PackGovernorEnforcer` (`governor.py`) **live-enforces** the per-pack caps
(`max_invocations_per_hour`, `api_rate_per_minute`, `max_sources_per_window`,
`max_cost_usd_per_day`) over the `action_pack_invocations` ledger (`0025`) plus the
global token envelope (`0022`), pre-call — any breach BLOCKs the tool and emits an
operator-visible `governor_events` row + NATS event. The operator-facing **Action-Pack Grants**
panel (§5.5) renders the effective intersection and the governor caps.

---

## 5. The registries + REST surface

Three registries back the descriptor model. Code: `src/legba/data/registry/`.

### 5.1 Descriptor registry (`descriptor.py`)

Four families (`target` / `analyst` / `source` / `action_pack`), each on its own
`*_descriptors` Postgres table (`target_descriptors`, `analyst_descriptors`,
`source_descriptors`, `action_pack_descriptors`). Register flow: validate →
content-hash → check head → INSERT with `is_head=true` → **Ed25519-signed audit
row** → NATS publish on `descriptor.registered.<family>.<id>`. Operations:
`register`, `register_raw` (with conversion-chain walk), `update`, `retire`,
`promote`, `rollback`, `get`, `get_typed`, `list`, history. Validation failures
route to a **descriptor DLQ** rather than aborting; `DescriptorValidationError`
is recoverable. Content-hashed instances are immutable artifacts — rollback
shifts the active head pointer back.

### 5.2 Stack registry + credential vault (`stack.py`, `credentials.py`)

The stack registry holds the substrate components (LLM providers, vector store,
embedding, NATS, Postgres, Redis, proxy pool, NLP
service). Descriptors reference a component by `Property.StackRef`. Secrets live
in a separate **`CredentialVault`** (PyNaCl SecretBox / XSalsa20-Poly1305);
descriptors carry `Property.Secret(secret_id)` and never plaintext. The vault
shares the Postgres pool and decrypts with `LEGBA_DATA_MASTER_KEY` from the
runtime container's env.

### 5.3 Vocabulary

`entity_classes`, `relationship_types`, and `analyst_kind` are runtime-extensible
via the vocabulary registry + a NATS-invalidated cache; new values are seen on
the next descriptor bind without a migration.

### 5.4 REST surface (`api.py`, `server.py`, `v3_api.py`)

A FastAPI app mounted at `/api/v1/registry/` (console entrypoint
`legba-registry`, default port 8090). Endpoint families: `/descriptors/{family}/…`
(CRUD + history + `typed` + retire/promote/rollback), `/stack/…`, `/vault/…`
(register/exists/delete — never returns plaintext), `/conversions/…`,
`/dead_letter`, `/audit` (with inline Ed25519 verification), `/vocabulary/…`, the
consult engine (`POST /api/v1/consult`), substrate-read + lineage + entities +
telemetry endpoints (including the `/api/v1/v3/eval/*` observability routes —
per-unit eval, calibration, and `GET /api/v1/v3/eval/analyst_runtime` for
per-analyst run timing — count, avg/max wall-clock seconds, last run, non-success
count — from `analyst_traces`), and a WebSocket `/events` multiplexing NATS. Auth is a
bearer token via `LEGBA_REGISTRY_API_TOKEN`. B-2 fail-closed: an unset token
returns HTTP 503 on every guarded request unless `LEGBA_DEV_MODE=1` is set
explicitly. The runtime
talks to this surface (never to descriptor rows directly) so it picks up the
registry's auth + audit + content-hash-head logic.

### 5.5 Operator UI panels (`legba-ui-v3/`)

The operator console (`docs/UI.md` is authoritative) is a dockview SPA whose
panels are lazy-registered in `legba-ui-v3/src/panel-registry/registry.ts`. Beyond
the registry/target/analyst/system panels, four operator panels expose the
source-first control surfaces that previously had no UI. **As of 2026-06 all four
ship registered + tested in `legba-ui-v3`; three (Action-Pack Grants, Subscription
Policy/Builder, Discovery Pipeline) are fully live end-to-end, and Backfill Replay
is a `PREVIEW_KINDS` tier — the panel is live but its backend POST is an honest 501
(the cross-plane runtime trigger is not yet exposed through the registry).** None
are seams:

- **Action-Pack Grants** (`registry.action_packs`, `panels/registry/ActionPacks.tsx`)
  — renders the three-way agency-grant intersection `analyst.action_packs ∩
  target.allowed_action_packs ∩ pack.applicability` (§4.8), grants/revokes a pack to
  an analyst or target by editing its descriptor through the registry CRUD (a new
  content-hashed, audited version), and shows the effective intersection + the
  pack's governor caps.
- **Subscription Policy** (`source.subscription_policy`,
  `panels/source/SubscriptionPolicy.tsx`) — the source-side `open` / `allowlist` /
  `grant` policy (§4.2) and which targets are authorized to subscribe; paired with a
  **Subscription Builder** (`source.subscription_builder`) that composes a
  `SourceRef` + structured/residual `Subscription`.
- **Discovery Pipeline** (`registry.discovery`, `panels/registry/DiscoveryPipeline.tsx`)
  — discovery descriptors (§4.7), their resolved candidate lists, and the L1
  instances they materialise/retire (disappearance-ratio guard).
- **Backfill Replay** (`system.backfill`, `panels/system/Backfill.tsx`) — operator
  re-drive of historical signals through fan-out/triggers (`runtime/subscription/
  backfill.py`). **Preview tier:** the panel is live, but its backend POST returns an
  honest 501 today — the cross-plane runtime trigger is not yet exposed through the
  registry (`PREVIEW_KINDS` in `registry.ts`).

---

## 6. The runtime

### 6.1 Dapr host + actor types

`legba-runtime-dapr` (`runtime/dapr_host.py`) builds the FastAPI app `daprd`
routes into, registers `TargetActor` / `AnalystActor` / `SourceActor` with the
`ActorRuntime`, and on startup runs `bring_up_production_runtime`: substrate
connections (Postgres / NATS / Redis), the actor state store, the registry HTTP
client, the deps resolvers, the reconcile loop + NATS informer, the source-first
planes, the audit checkpointer, and the optimizer's Dapr-Workflow worker. The
host mounts the A2A skill output router **only when operator-enabled**
(`LEGBA_A2A_ENABLED=1` + a non-empty `LEGBA_A2A_TRUSTED_KEYS` allowlist, or
`LEGBA_DEV_MODE=1`); by default the surface is fail-closed OFF and the host
answers `/a2a/skills` with a 503 + enable recipe (never a silent 404) — see
SEAMS #15. It also mounts an in-process tools registry (e.g. the outbound
Mnemosyne trust-query tool, wired only when a base URL + signing key are
present).

### 6.2 Reconcile loop (`reconcile.py`)

A Kubernetes-operator-style loop bridges desired state (the registry) to observed
state (actor records). **Pure** per-kind reconcilers take `(observed, desired)`
and return a `ReconcileAction` (`CREATE_ACTOR` / `TRANSITION_LIFECYCLE` /
`RESTART_ACTOR` / `RETIRE_ACTOR` / `NOOP`); the **action executor** is the only
mutator — it maps each action to an `ActorProxy` lifecycle call (`activate` /
`pause` / `retire`). All three families (`target`, `analyst`, `source`)
reconcile through the same logic; the family discriminator lives in the desired
state + the executor's `_proxy_for`. The **informer** is a NATS subscription on
`descriptor.>`; a 5-minute periodic resync is the backstop; one synchronous
initial resync on bring-up activates every active descriptor.

A content-hash change yields a **soft** `RESTART_ACTOR` (re-`activate()`, which
re-reads the body) so source cursors survive; a full retire-then-create would
lose them.

### 6.3 Scheduling

Cadence is **durable** via Dapr Reminders (`runtime/dapr_cron.py` converts cron →
`(due_time, period)` reminder timing — Dapr takes Go-duration timings, not cron
strings; only fixed-period reminders are used — there is no variable-period Dapr
Jobs integration), persisted
by the Dapr **scheduler** service (embedded-etcd-backed) so reminders fire across
sidecar restarts. A poll `SourceActor` registers a `poll_<source_id>` reminder
from `cadence.schedule`; an analyst registers a single `run_cadence` reminder from
`cadence.fallback_schedule`. Event-driven firing is the coalescing trigger engine
(§6.6); cadence is the coverage heartbeat.
**Self-heal:** the reconcile resync (§6.2) re-asserts active analysts' reminders —
an idempotent `activate()` re-registers `run_cadence` — so a silently-dropped
reminder recovers within the resync interval instead of stranding the actor once
it idles out (30m).

**Operational reality — reminder recurrence depends on clean embedded-etcd.** The
scheduler's reminder *recurrence* (honoring the `period`, not just the first-fire
`dueTime`) is only as reliable as its persistent etcd state. A
corrupted/inconsistent `dapr-scheduler` etcd dir (the `deploy/dapr-scheduler-data/`
bind-mount) is silent-deadly: every reminder fires *once* at boot and then stops
recurring, so source polls and analyst cadence both go quiet while the scheduler
reports healthy and logs zero errors. This is **not** a Dapr bug or a misuse of
`register_reminder(due_time, period)` — a clean etcd reproduces correct recurrence;
a *partial* clear (deleting only the cluster-marker files) leaves etcd inconsistent
and re-breaks it. The fix is a **full wipe** of the scheduler data dir followed by
a dependency-ordered control-plane restart — see **RUNBOOK §0** for the exact
procedure and verification. (The dapr-python SDK also serializes the period with a
Greek-mu `µs` unit, which the scheduler honors but with a small per-period fire
burst, absorbed by the per-(analyst, target) cooldown/dedup.)

### 6.4 Budget (`runtime/budget.py`)

`BudgetEnforcer` tracks token usage in `budget_ledger`, keyed by
`(analyst_id, day)` (per-day UTC bucket). Pre-call checks gate a run; on
exhaustion the actor either pauses until the next window or auto-demotes to the
`method.llm.fallback` model for the rest of the bucket (per the analyst's
`method.retry.budget` policy). A **global envelope** (migration `0022`) can flip
*all* analysts to fallback for the current bucket. The per-pack **governor** caps
(rate / invocation / source / daily-cost) are a separate, complementary enforcer
on the agency dispatch path (§4.8) — **live**, over the `action_pack_invocations`
ledger (`0025`), and it also consults the same global envelope.

### 6.5 Failure semantics (`dapr_actors.py`)

In-flight exceptions are classified `transient` / `budget` / `hard`. Transient
(5xx / 429 / network) retries with backoff per `method.retry.transient`; budget
pauses or demotes; hard (4xx / validation / unhandled) DLQs and alerts. Output
writes validate the payload first and route invalid payloads to
`output_dead_letter` rather than aborting the run.

### 6.6 Source-first planes (`source_first_runtime.py`)

`bring_up_source_first_planes` assembles, in order:

- **Job plane** — `JobQueue` (work-queue stream + shared durable consumer) +
  `JobWorkerPool` (competing consumers). The `process_media` handler is thin
  today (**future seam**: SeaweedFS object store + hosted Whisper/VLM/OCR
  endpoints; the job envelope exists).
- **Agency plane** — an `Agency` over the live job queue + a channel emitter +
  a NATS governor-event publisher, exposed via `AGENCY_HOLDER`. Two built-in
  paths drive it in production (A-3): the consult kind routes its ReAct tool
  calls through the governed `substrate_read` pack, and the actor run path
  fires the `escalate_finding` pack when a finding crosses its gate (the
  per-analyst on-ramp is `data/analysts/agency/binding.py`). Live-proven at
  the 2026-06-10 cutover.
- **Subscription / fan-out plane** — `SubscriptionEngine.register_target`
  resolves each active target's `source_refs` to authorized bindings, enforces
  source policy, and binds **one** per-target aggregated JetStream consumer,
  subject-filtered to its coarse axes over `legba.signals.>`. Signal subjects are
  coarse: `legba.signals.<tenant>.<source>.<modality>.<event_class>`.
- **Trigger plane** (**live + reactive**) — a `TriggerEngine` over a
  `Coalescer`. It consumes the matched-signal stream, marks (analyst, target)
  pairs dirty in `trigger_state`, and fires on cadence / accumulation / severity
  (clamped by cooldown). A fire routes to the analyst's `AnalystActor` via
  `ActorProxy`, target-scoped. The injected runner is the `ActorTriggerRunner`,
  which dispatches a coalesced fire for **any** method kind — deterministic *and*
  LLM-bearing — because the "no LLM fire per signal" rule is enforced upstream in
  the trigger **policy** (accumulation floored to a min LLM batch), so a fire that
  reaches the runner is already a coalesced batch. This is what makes the path
  reactive: an LLM analyst now fires on accumulation/severity, not only on its
  Dapr cadence reminder.

The wiring step (`_wire_targets_and_triggers`) is fail-soft per target: a
legacy/incompatible descriptor or a vanished source is logged and skipped, never
sinking the whole bring-up. Analyst→target binding has two paths: a target's
inline `analyst_ref`, or an analyst's `subscription.targets` selector whose
Starlark predicate (e.g. `has_tag("g20") or has_tag("watch")`) is evaluated
against each target's scope — a null predicate matches every target. This is how
one unit descriptor (e.g. `energy_security`) or the `country_composition` analyst
coalesces over all g20/watch country desks without enumerating them. The full reactive loop is **proven
live** (source polls → enriched signals → fan-out → coalesced fires → cited,
verified per-country reads; confirmed after the scheduler-etcd fix, §6.3). Honest caveat on boot ordering:
on a cold rig the wiring pass can complete with `trigger_regs=0` if it runs before
targets/analysts have reached `active` — the informer/5-minute resync re-wires
once they do, and until then the cadence heartbeat still drives coverage (see
RUNBOOK §0 and the bring-up signposts).

### 6.7 The optimizer GEPA loop (Dapr Workflow) — cadence-frozen monolith, scoped return

The `optimizer` analyst kind runs its durable DSPy + **GEPA** prompt-evolution
loop as a **Dapr Workflow** on the existing `daprd` sidecar — a deterministic
workflow body with non-deterministic LLM activities and replay-based durability,
fit for multi-hour batch work. The host starts an in-process `WorkflowRuntime`
worker (`runtime/dapr_workflow/worker.build_workflow_runtime`) and threads a
workflow client into the optimizer's durable-handle slot; the optimizer actor's
`run()` dispatches a workflow and polls status. An in-process GEPA fallback runs
when `dapr.ext.workflow` is unavailable. A scale-out standalone worker is
available under `--profile dapr-workflow`.

**This is the measure-before-autonomy contract in the runtime (§3.6).** The old
monolithic `country_optimizer` — always-on, unmeasured, and the source of a
reminder-flood incident (a >4 MB workflow payload orphaning scheduler reminders)
— stays **cadence-frozen** (its descriptor remains `state=active`): its
`cadence.fallback_schedule` is nulled, so the on-activate `if schedule:` gate
registers no `run_cadence` reminder and it fires on no tick (SEAMS #30). GEPA **returns** only as the separate, scoped `unit_optimizer`
descriptor (§3.6) over the single `leadership_transition` unit, whose
`method.kind: dspy_compile` candidates each carry a real paired faithfulness delta
on the same faithfulness judge (currently the core model, not cross-family), stay
`human_gated`, and cannot auto-promote on a
degenerate / absent / non-positive delta.

There is **no Temporal infrastructure** anywhere in the system — no Temporal
frontend/history/matching cluster, no `temporal_persistence` / `temporal_visibility`
database pair, no second worker image. The runtime collapses to one control plane
(the `daprd` sidecar). The former `runtime/temporal/` Python package was deleted
(P-CUT/C-3): its live pieces — the substrate-agnostic workflow-I/O dataclasses and
the GEPA loop body (`_run_gepa_loop`) — now live in `runtime/dapr_workflow/gepa.py`,
so the algorithm lives in exactly one place. The `OptimizerDeps.temporal_client`
slot is just "the workflow client" (historically named, kept stable; env-gated to
the Dapr-Workflow client or the in-process fallback). Nothing dials Temporal.io.

---

## 7. Data shape

Substrate schema is built by a single `src/legba/data/migrations/0001_baseline.sql`
(Postgres) plus the forward chain (`0032`…`0057`, migration head **`0057`**). A cold start from empty volumes
applies them in order. (The historical 0001→0031 migration chain was flattened to this
baseline for the clean-slate release; it remains in git history.)

### 7.1 The target-agnostic signals table (`0024_pivot_substrate.sql`)

`signals` is the shared, source-owned, modality-first raw pool. It carries **no
`target_id`**. Notable columns:

- **Provenance:** `source_id`, `source_version`, `produced_by_id`,
  `produced_by_kind` (`source` | `job` | `analyst` | `deterministic` | `system`),
  `fetched_at`, `owner_tenant`.
- **Modality-first:** `modality`, `mime_type`, `media_ref`, `embedding_ref`,
  `retention_class`, `object_ref`.
- **Content + enrichment:** `payload` (JSONB), `canonical_url`, `raw_provenance`.
- **Typed structured-filter columns** (baseline enrichment, indexed for
  subscription pushdown): `language`, `geo TEXT[]`, `tags TEXT[]`,
  `entity_classes TEXT[]`, `source_credibility`. GIN indexes on the arrays.
- **Dedup (link, never collapse):** `content_hash`, `canonical_signal_id`, and a
  separate `signal_aliases` link table — every raw row is kept; dedup links
  aliases to a canonical, never deletes. Two dedup tiers run **at ingest**
  (`data/filters/ingest_dedupe.py`, wired into `SourceCore` from the source's
  `pipeline.ingestion_filters` `dedupe_tier_1`/`dedupe_tier_2` stages): tier 1
  (canonical-URL hash) and tier 2 (content hash), resolved transitively to a true
  self-canonical root. The batch `cross_source_dedup` deterministic analyst is the
  pool-wide cadence sweep that complements this cheap ingest-time pass; the richer
  semantic (vector) + temporal tiers 3–4 live in the target-side
  `Dedupe4TierHandler` marker. (Source-side **tiers 1–2 are live**; the cross-
  source semantic/temporal coalescing beyond exact-key linking is now **built,
  off-by-default** — the `cross_source_coalesce` deterministic sub-handler
  embeds recent canonical signals into a shared Qdrant collection and links
  near-duplicate signals reporting the SAME event from DIFFERENT sources via
  tier-3 cosine + tier-4 temporal/title logic, link-never-collapse. It is a
  declared SEAM (#19): it requires the `embedding_service` + `qdrant` ports and
  **refuses loud** when either is absent — emitting a `coalesce_unavailable`
  finding and writing zero `signal_aliases` rows rather than fabricating links —
  because there is no non-vector deterministic fallback for "same event,
  different words".)
- **Lineage:** `derived_from UUID[]` (GIN-indexed), `schema_uri`.
- `entities_resolved_at` (in the baseline schema) — the per-signal marker the ongoing
  `entity_resolution` analyst stamps as it folds NER mentions into the entity
  graph (idempotent, forward-progressing).

### 7.2 Analyst outputs + provenance lineage

`analyst_outputs` (`0012`) is the generic, kind-discriminated table for
`finding` / `meta_finding` / `alert` / `critique` / `prediction` /
`prompt_module_candidate` / `scorecard`; `situations`, `hypotheses`, `facts`,
`nexuses`, and `journal_entries` get dedicated tables. The `OutputKind` registry
(`provenance/kinds.py`) is a **twelve-member** enum (`finding`, `situation`,
`hypothesis`, `prediction`, `alert`, `meta_finding`, `critique`, `fact`,
`nexus`, `prompt_module_candidate`, `journal`, `scorecard`) and maps each kind →
table + pydantic payload model + Iglu `schema_uri` + NATS subject pattern. The
11th kind, `journal` (`JournalPayload` → `journal_entries`, migration `0048`, Iglu
`iglu:legba/journal/jsonschema/1-0-0`), is the first-person reflective voice
(§7.6) and is deliberately **off the fact/finding/nexus chain** (see below). The
12th, `scorecard` (`ScorecardPayload`, `iglu:legba/scorecard/jsonschema/1-0-0`,
NATS `analyst.{analyst_id}.scorecard`), lands in the generic `analyst_outputs`
table: it is the deterministic banded row `scorecard_producer` side-writes per
active g20/watch country desk (§3.6).

Every analyst-produced row carries universal provenance: producing id + version,
`produced_at`, `derived_from UUID[]`, `schema_uri`, `run_id`. The write-side
wrappers (`provenance/writes.py`) validate the payload, INSERT, optionally
publish, and DLQ on invalid payload. Lineage is a recursive CTE over
`derived_from`; `verify.py` walks it with cycle + dangling detection. AGE
`:DerivedFrom` edge mirroring is a **future seam** (a hook exists).

**The off-chain exception (`journal`).** Every kind above is a *member of* the
provenance chain — its `derived_from` walk reaches back through
signals → facts → relations/nexuses → situations → assessments, and its table is
registered in the lineage catalog (`registry/lineage_api._SUBSTRATE_TABLES`:
`signals` / `situations` / `hypotheses` / `facts` / `analyst_outputs`).
The `journal` kind is the deliberate exception: a journal row is a *perspective
**over*** the chain, never a *node **in*** it. It carries an **always-empty
`derived_from`** (its citations live only in the journal-local `claims` /
`cited_substrate_refs` columns — an UP-only reference, never a lineage edge) and
its `journal_entries` table is intentionally **absent** from the lineage catalog,
so a downstream `derived_from` walk from any fact/situation/nexus can **never
surface a journal node**. This is a *direction-asymmetric lineage node*: the
journal reads the chain but the chain cannot reach it. A gating test
(`tests/.../test_journal_off_chain.py`) enforces that the journal never writes a
`fact` / `finding` / `nexus`. Do not place the journal inside the lineage; it is
a reflective layer above/across it (§7.6).

**Supersession (`0027` — live)** — analysts re-assessing an evolving situation
re-emit near-duplicate findings; `analyst_outputs.situation_signature` clusters
them and `superseded_by` / a `finding_supersessions` link table mark which finding
wins, non-destructively (both rows kept; the canonical is the one row with
`superseded_by IS NULL`). Finding-level supersession + situation clustering runs as
an automated pass: the `finding_supersession` deterministic handler
(`data/analysts/deterministic_handlers/finding_supersession.py`, registered in the
deterministic-handler registry, mirroring the `signal_aliases` link pattern). Its
situation signature is explicit-first (`data.situation_id` / `situation_signature`)
then a derived `sig:<topic>|<sorted entity tokens>` key; findings with no derivable
signature are left unclustered. (Substrate + automated pass are both in place.)

### 7.3 Entity graph

The entity graph is **relational**: `entity_profiles`, `signal_entity_links`,
and `proposed_edges`. The `entity_resolution` deterministic analyst folds each
new signal's NER mentions into it: co-occurrence edges upsert on
`(lower(source_entity), lower(target_entity), relationship_type)`
(`uq_proposed_edges_triple`, in the baseline schema). The Apache AGE graph
`legba_graph` exists alongside but is **dormant / off the critical path** (its
write-legs ship off by default); `/entities/graph` queries `proposed_edges`
directly, not AGE (see ARCHITECTURE.md §5.5 "AGE re-evaluation", 2026-06-23).

### 7.4 Runtime + audit tables

`actor_state` (Dapr state), `budget_ledger` + `global_budget_envelope` (`0022`),
`analyst_traces` (run traces + receipt-chain heads), `trigger_state` (`0028`,
the coalescing accumulator keyed `(analyst_id, target_id)`), `legba_jobs` (job
ledger), `descriptor_audit_log` (Ed25519-signed append-only), `audit_checkpoints`
(per-analyst chain-head checkpoints), `descriptor_dead_letter` /
`output_dead_letter`, `source_credibility` (`0014`), `cost_model` (`0015`),
`action_pack_governor` state (`0025`), discovery state (`0026`),
`ui_panel_registrations` (`0017`), and ISO-country + geopolitical-vocabulary
seeds (`0019` / `0020`).

### 7.5 Modality registry (partly live, partly future seam)

The signals table is **modality-first** (§7.1 — `modality`, `mime_type`,
`media_ref`, `canonical_url`, `embedding_ref` are first-class columns on every
signal). Those columns are the contract for a single registry keyed by
**`modality`** (coarse), optionally narrowed by **`mime_type`** (fine), that binds
two halves which today are wired independently. Resolution is most-specific-first
(`mime_type` → `modality` → `binary` fallback) — the same pattern as LLM
subprovider inference (§10).

One key, two handlers:

| modality | ingest | UI **renderer** |
|---|---|---|
| `text` | passthrough *(live)* | title / body *(live)* |
| `structured` (e.g. `application/geo+json`) | **GeoJSON source kind, model-free, *live*** — `data/sources/geojson.py`, registered in the source factory; emits `modality="structured"` + `mime_type="application/geo+json"` Signals from a GeoJSON (RFC 7946) URL, geo promoted to the `geo` column from feature properties | MapLibre map *(placeholder — `maplibre-gl` is a dep, renderer entry `implemented:false`)* |
| `audio` | Whisper transcribe → derived text signal *(seam)* | `<audio>` (media_ref) + transcript child *(placeholder)* |
| `video` | VLM caption + transcript → derived text *(seam)* | embedded player / `canonical_url` watch link + transcript *(placeholder)* |
| `image` | OCR + caption → derived text *(seam)* | `<img>` (media_ref) + OCR text *(placeholder)* |
| `binary` | none — reference only | download link *(placeholder)* |

- **Ingest half** has two shapes. A whole-document structured feed is a **source
  kind** (the GeoJSON handler — model-free: GeoJSON is already structured, so
  there is no extraction model in the loop). Per-attachment extraction extends the
  `MediaExtractor` protocol / `default_extractor_registry()` in
  `data/sources/baseline.py`, dispatched by modality in the acquisition baseline;
  each extractor
  emits a **derived** signal stamped `derived_from` the source signal so the
  extracted text is searchable/embeddable and appears in the lineage walk. Today
  `text` (passthrough) and `structured`/GeoJSON (source kind) are real; the
  Whisper/VLM/OCR extractors are the seam.
- **Renderer half** is the `modality → renderer` registry in
  `legba-ui-v3/src/lib/modalityRenderers.tsx` (resolved most-specific-first,
  mime > modality > default), consumed by the lineage `ModalityRef`. Today only
  `text` is a real renderer; every other modality — including `structured` /
  `application/geo+json` — is a badged placeholder + a link (`implemented:false`),
  so even the live GeoJSON ingest currently lands as a structured-badge placeholder
  in the UI. A real renderer (a MapLibre map, a player) is a drop-in entry, no
  call-site change.

Adding a new type = drop in one ingest handler + one renderer keyed by its
modality/mime — **no schema or plumbing change**, because the signal columns
already carry everything the registry needs. A modality seam test
(`tests/data_pkg/test_modality_seam.py`) proves a non-text signal routes,
enriches (graceful skip), and dispatches without breaking. `structured`/GIS was
the model-free first candidate and is now the first non-text modality with a live
ingest path; remaining work is the inference-bearing extractors (Whisper/VLM/OCR)
and the real renderers (map/player). This is the multimodal track.

### 7.6 The journal assessor — Legba's first-person reflective voice

Every other meta-analyst cuts **one** slice of the substrate; the **journal**
cuts across the **whole flow**. It is the one analyst pointed at the entire
organism — its own self, state, and flow — narrating a coherent point of view
*over* the rest of the system rather than another finding inside it. (Its working
thesis: *"Poetry without evidence is noise. Evidence without perspective is just
a log file."*) It is **live** — deployed and live-validated (a real off-chain
entry, `honesty_flags` forced deterministically from substrate metrics,
receipt-chained, in-voice).

**Off the chain (the load-bearing property).** The journal is the 11th
`OutputKind` (§7.2) but a *perspective over* the provenance chain, never a
*member of* it: its rows land in a **dedicated `journal_entries` table**
(migration `0048`), carry an **always-empty `derived_from`**, and the table is
deliberately **excluded from the lineage catalog**, so a downstream
`derived_from` walk can never surface a journal node (§7.2). It must never write
a `fact` / `finding` / `nexus` — enforced both at the grant layer (below) and by
a gating test. Its citations live only in the journal-local `claims` /
`cited_substrate_refs` columns (an UP-only reference into the chain).

**One kind, two tiers (the tier is the descriptor).** A single extension kind
`journal_assessor` (§4.8) backs two descriptors with distinct ids; there is no
mode flag — `run_method` selects `entry_kind` from `identity.id`:

- **`journal_assessor`** — the **entry** tier. Cadence every 12h
  (`0 0,12 * * *`), narrating the freshest window.
- **`journal_consolidator`** — the **consolidation** tier, *same kind*, daily at
  02:00 UTC (`0 2 * * *`). It distills its prior consolidation + recent entries
  into one forward-carried narrative (build-on-don't-repeat), emits
  `entry_kind='consolidation'`, and fires `supersede_prior_consolidation` (close
  the prior open consolidation, open this one — the standard `valid_until` /
  `superseded_by` supersession pattern, enforced to **at most one open
  consolidation** by a partial-unique index).

**Engine + the per-phase LLM split.** Both tiers run the in-actor
`llm_planner` GATHER envelope (the one-soul staged arc PLAN → GATHER → NARRATE,
with the persona reloaded every phase) — a single **global** meta run per cadence
tick (`target_filter=None`, like `world_assessor`), **not** the `deep_consult`
Dapr workflow (that path rides the broken long-activity round-trip, task #86).
The deep GATHER investigation loop runs on the local gpt-oss / vLLM plane
(`method.llm.primary` → `llm.primary.openai_compat`); the **voice** (the in-voice
field-notes seam + the NARRATE synthesis) runs on the Anthropic plane, Opus 4.8
(`method.llm.narrate` → `llm.anthropic.opus_4_7`). So Anthropic spend is just the
bounded final voice synthesis (its `max_tokens` governs only the Opus narrate —
16384 entry / 24576 consolidation — and is never sent to the local gather plane,
which uses its own server budget); the agentic loop itself is local. This is the
optional-second-handler split described in §3.3.

**Two packs, propose-and-gate (the hygiene invariant).** The journal is granted
exactly two action-packs — `journal_read` (14 read tools, including 9
self-instruments: graph structure, structural balance, critic scores,
calibration, run health, source health, budget status, the journal delta, …) and
`journal_propose`. Both are non-write-fact, which is the **grant-layer backstop**
for the never-write-a-fact invariant. The journal writes only its own entries +
consolidations directly; everything outward — a correction, a change, or a
`self_revision` (including to its own instructions; protected sections
auto-reject) — goes to the **human-gated `journal_proposals` queue**, never to a
live table. A human accepts/rejects; the accept path runs an idempotent per-kind
apply worker. Its only un-gated effect is its **own continuity** (it reads its
own last entry + current consolidation into its next run): *it can write its own
next breath but cannot rewrite its own rules without the operator.*

**Surfaces.** Tables: `journal_entries`, `journal_proposals` (both migration
`0048`). API: `GET /api/v1/journal` serves the open consolidation + entry stream;
`GET /api/v1/journal_proposals` + `POST /api/v1/journal_proposals/{id}/accept` /
`/reject` drive the review surface. The **Journal** UI panel (`system.journal`,
`panels/system/Journal.tsx`) renders entries with provenance chips that deep-link
to the cited record, styling `[needs_citation]` / perspective spans distinctly.
Prompts: `legba.prompts.journal_assessor:JOURNAL_SYSTEM` (entry persona) +
`legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM` (consolidation persona).

**Honest caveats.** The `correction` + `self_revision` apply paths are tested
end-to-end; the `change`-apply path is import-verified but not yet exercised
against a live registry. The Journal panel is tsc-green + fully wired but pending
its first real in-browser render. A critic + an optimizer *over the journal's own
voice* (Wave 5) is **designed, not built** — gated on first building a critic
actuator.

---

## 8. The predicate DSL — Starlark

Predicates appear on four surfaces (`data/predicates/compiler.py`
`PredicateSurface`): `TARGET_SCOPE`, `SOURCE_FILTER`, `ANALYST_SUBSCRIPTION`,
`CADENCE_TRIGGER`. They are the residual matchers that narrow the coarse NATS +
SQL structured filter.

The DSL is **Starlark** via the `starlark-pyo3` (Rust) binding. Properties:

- **Expression-only.** A predicate must be a single Starlark expression; banned
  source tokens (`def`, `load(`, `lambda`, `while`, `import`, top-level `for`
  statements) are rejected at compile, so there are no statements, no loops, no
  imports. Comprehensions remain available.
- **Fixed helper catalog** (`data/predicates/helpers.py`, catalog version
  `1.0.0`) — `mentions`, `mentions_any`, `geo_match`, `geo_in`, `org_match`,
  `recent`, `signal_age_hours`, `credibility`, `entity_class_in`, `has_tag`,
  `has_any_tag`, `severity_at_least`, `scope_geo`, `scope_entity_classes`,
  `target_id`, `target_kind`, `abstraction_level`, `event_type`,
  `event_payload_get` — each tagged with the surfaces it is bound on.
- **Compiled once at registration**, cached in a thread-safe LRU keyed by
  `(source_hash, surface, catalog_version)`.
- **Wall-clock budget** (5 ms default) at eval via `evaluator.run_with_budget`.
  (Per-eval step + memory caps are not exposed by the current Rust binding; the
  expression-only gate + wall-clock cap are the live guards.)

Compilation failures route to the descriptor DLQ; the schema validators
compile-check every predicate field at registration time.

---

## 9. Security boundaries

- **Credential vault** — XSalsa20-Poly1305 (PyNaCl SecretBox); descriptors carry
  `Property.Secret` references only; the vault decrypts with
  `LEGBA_DATA_MASTER_KEY` (env, not in image layers).
- **Ed25519 audit log** — every descriptor state change appends a signed row to
  `descriptor_audit_log`; the `/audit` endpoint verifies inline. Signing identity
  via `registry/signing.py`.
- **Per-analyst receipt chains** — each analyst maintains a **SHA-256
  hash-chained** sequence of canonical-JSON run receipts (`provenance/receipts.py`),
  loaded from the latest `analyst_traces` row and advanced per run. The lineage
  API exposes each node's `receipt_hash` + a `chain_consistent` boolean (UI badge:
  "chain-consistent (single-node)"). This is a single-node integrity chain — it
  detects an inconsistent local re-hash; it is **not** a signed, distributed, or
  tamper-proof guarantee, and the doc does not claim otherwise. Chain heads are
  periodically checkpointed into `audit_checkpoints`, and those checkpoint rows
  (not the per-run receipts themselves) are Ed25519-signed by the host's audit
  checkpointer.
- **A2A envelopes** — analyst outputs bound to an `a2a_skill` are emitted as
  Ed25519-signed envelopes (canonical JSON + nonce + DID), byte-compatible with
  Mnemosyne's verifier.
- **DLQ everywhere** — descriptor-validation failures, invalid output payloads,
  and retry-exhausted NATS publishes route to dead-letter surfaces with resubmit
  paths rather than aborting.
- **Tenancy** — `owner_tenant` on signals + sources + the subscription policy is
  an indexed multi-tenant seam, single-tenant-first in enforcement today.

---

## 10. AI models

Models are hosted out-of-process via the `legba-models` service: a vLLM
LLM endpoint (gpt-oss-120b), `BAAI/bge-m3` embeddings, NLLB translation, and
spaCy/GLiREL NER.
The runtime reaches them through stack-registry components (an `NlpServiceClient`
for NER + classification, an embedding service for dedup + semantic correlation,
LLM handlers for analysts). LLM **providers** (Anthropic / vLLM / OpenAI) are
resolved through the stack registry per `Property.StackRef`. Three distinct model
planes are wired, by role:

- **Core analyst plane** — the self-hosted `gpt-oss-120b`
  (`llm.primary.openai_compat`, $0 to run) drives every production analyst: the
  four units, the compositions, and the deterministic-plus-LLM handlers.
- **Faithfulness verify judge** — currently the **same core `gpt-oss-120b`**
  (`llm.primary.openai_compat`) scores groundedness on the verify pass (§3.5). It
  is **not** cross-family — a deliberate, temporary choice after the 8B judge
  (`llm.verify.slm_8b`, "legba-slm") proved too weak; a dedicated reasoning judge
  is planned (known limitation: same-model judging shares blind spots).
- **Consult plane** — Claude Opus 4.8 backs the `consult_on_demand` /
  `deep_consult` kinds **only** (it is metered/billed, so it is used sparingly).

An on-demand **consult engine** (`consult_on_demand` analyst kind, a ReAct loop
over the governed `substrate_read` action-pack tools) answers operator questions
over the substrate via `POST /api/v1/consult` with cited refs + uncertainty.

HARD rule: no litellm / dspy in the runtime image or the analyst inference path;
dspy lives only in the opt-in GEPA worker image (§6.7).

---

## 11. Deployment

One `docker-compose.yml`, profile-gated (`docs/RUNBOOK.md` covers operations):

- **Substrate** (no profile, `docker compose up -d`): `redis`, `postgres`
  (`apache/age`), `qdrant`, `nats` (JetStream).
- **`--profile dapr`**: `dapr-placement`, `dapr-scheduler` (+ its init/persistent
  etcd dir), `dapr-init-db` (isolated `dapr` database), `dapr-sidecar` (`daprd`,
  app-id `legba-runtime`, pinned `daprio/dapr:1.17.9`).
- **`--profile runtime`** (canonical bring-up; transitively activates `dapr` +
  `ui`): `legba-registry` (`docker/Dockerfile.registry`, ~420 MB),
  `legba-runtime-dapr` (`docker/Dockerfile.runtime`, the Dapr-actor host),
  `legba-ui-build` (one-shot SPA build → shared `legba_ui_dist` volume), and
  `legba-caddy` (edge: SPA static + `/api/*` → `legba-registry:8090`).
- **`--profile dapr-workflow`**: `legba-dapr-workflow-worker` — the scale-out
  optimizer worker (reuses the runtime image; embeds in `legba-runtime-dapr` by
  default, so this container is optional).
- **`--profile mcp`**: `legba-mcp` (`docker/Dockerfile.mcp`) — MCP stdio for
  Claude Code, launched per-conversation via `docker run -i`.

Secrets (`LEGBA_DATA_MASTER_KEY`, model + vendor keys) flow in via `env_file:
.env`, kept out of image layers. A retired host-mode systemd fallback is
documented in `docs/RUNBOOK.md`.

---

## 12. Sibling docs

- `docs/ARCHITECTURE.md` — conceptual orientation and design rationale.
- `docs/RUNBOOK.md` — bring-up, operations, troubleshooting.
- `docs/UI.md` — the operator console (panels, auth chain).
- `docs/ACQUISITION.md`, `docs/ANALYSIS.md` — the acquisition and analysis
  planes in depth.
