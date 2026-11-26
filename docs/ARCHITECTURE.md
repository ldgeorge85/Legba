<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Architecture

For a reader who wants to know what Legba is made of and how the pieces fit.
Names, tables and values are in `CODE_MAP.md`, `DATA_MODEL.md`, `GLOSSARY.md`
and `TUNABLES.md`; procedures are in `RUNBOOK.md`; the not-built list is
`SEAMS.md`.

**Contents:**
[1 The shape](#1-the-shape) ·
[2 The planes](#2-the-planes) ·
[3 The descriptor model](#3-the-descriptor-model--an-instance-is-its-descriptors) ·
[4 Acquisition](#4-acquisition--ingest-once-enrich-once) ·
[5 Fan-out and triggering](#5-fan-out-and-triggering--match-many) ·
[6 The analyst kinds](#6-the-analyst-kinds) ·
[7 The tower](#7-the-tower--the-record-and-the-voice) ·
[8 Outputs and provenance](#8-outputs-and-provenance) ·
[9 The measurement layer](#9-the-measurement-layer) ·
[10 Agency](#10-agency--what-an-analyst-is-allowed-to-do) ·
[11 The model planes](#11-the-model-planes-and-who-pays) ·
[12 The actor runtime](#12-the-actor-runtime) ·
[13 Scale](#13-how-it-scales-and-where-it-stops) ·
[14 The perimeter](#14-the-perimeter) ·
[15 What is not built](#15-what-is-not-built) ·
[16 Read next](#16-read-next)

---

## 1. The shape

Legba watches many vantage points at once (in the example instance: country
desks, thematic lanes, sectors, single entities) and keeps a current,
provenance-traceable model of what is true for each of them.

The naive shape of such a system is one watcher that owns its feeds, pulls them,
enriches what it pulls, and reasons over the result. That shape does not survive
fan-out: a hundred desks sharing ten feeds means a hundred redundant polls, a
hundred dedupe pipelines over identical bytes, and a hundred transcriptions of
the same audio. The root cause is that *observation* (acquiring and normalising
a fact about the world) and *interpretation* (deciding what that fact means for
one concern) are entangled. Everything below is the consequence of separating
them. A *desk* below is the example instance's word for a target: one scoped
concern a set of analysts work.

```
  SOURCES ──────► SIGNALS ─────► predicate FAN-OUT ─────► DESKS ──────► ANALYSTS ─────► OUTPUTS
  (acquire)       (observe,      (match-many)            (a concern)   (reason)        (interpret)
                   enrich once)
```

A signal is an observation, not an interpretation. A source acquires a fact
about the world once, enriches it once in a target-agnostic way, and publishes
it once into a shared pool. A `Signal` therefore carries no `target_id` —
interpretation is target-owned and lives only on derived analyst outputs
(`data/sources/_contract.py`; the field set is `extra="forbid"`, so a new
structured fact is declared on the model, not added to `payload`).

Four properties fall out of that one inversion:

- One source is one connection, however many concerns consume it.
- Heavy enrichment runs once, at the source, for every consumer.
- A desk's view is a predicate-filtered slice of the shared pool — no copies, no
  per-target marker rows.
- Real-time and batch are the same mechanism: a published signal both notifies
  live subscribers over NATS and persists to a queryable pool in Postgres, so a
  late joiner and a re-analysis read the same source of truth.

Work sorts by **altitude** — how far above the raw signal it sits. Altitude 0 is
extraction, always-on at ingest: temporal facts and typed signed relations.
Altitude 1 is the bounded reasoning units, one narrow question each. Altitude 2
is composition over verified units, and analysis of analysis. Altitude 3 is
on-demand deep work. The altitude frame is what keeps the build from collapsing
into one god-agent; `ANALYSIS.md` works each altitude in depth.

## 2. The planes

Four planes, three of them runtime and one of them storage.

| Plane | Runs as | Job |
|---|---|---|
| **Substrate** | `postgres` (built on the Apache AGE image; the graph itself is removed), `nats`, `qdrant`, `redis`, `opensearch` | shared state and truth |
| **Control** | `legba-registry` | the descriptor registry, the lifecycle FSM, the credential vault, the REST and WebSocket surface, the dead-letter queues |
| **Runtime** | `legba-runtime-dapr` plus its `daprd` sidecar, `dapr-placement`, `dapr-scheduler` | turns active descriptors into Dapr virtual actors that read and write the substrate |
| **Durable jobs** | `legba-dapr-workflow-worker` | the multi-step Dapr Workflows that do not fit a turn-based actor |

Around them: `legba-caddy` is the edge (it serves the built single-page console,
the installable phone shell under `/m/`, and proxies `/api/*` to the registry),
`legba-ui-build` is a one-shot job that publishes both static trees into a shared
volume, and `legba-mcp` is a stdio image launched per conversation rather than a
long-running service. Local sidecars ship behind compose profiles and are off
unless the person running the instance turns them on: `searxng` (search),
`rsshub` (extra sources), `ntfy` (alerts), `legba-media` (media extraction).
`docker-compose.yml` is the one file that defines all of them; `RUNBOOK.md` owns
bring-up order, the tiered deploy (`scripts/rolling_deploy.sh`) and the nightly
test suite (`scripts/host_nightly_suite.sh`).

No model runs inside a Legba container. The language models, the embedding
model and the baseline NLP models are served out of process on model-serving
hosts and reached over HTTP through stack components resolved from the registry.
That is what makes the model layer swappable by descriptor rather than by
rebuild; `AI_MODELS.md` owns the model surface.

### 2.1 What each store holds

| Store | Holds | Why it is the one that holds it |
|---|---|---|
| **Postgres** | descriptors, signals, facts, nexuses, entity profiles and edges, situations, events, hypotheses, analyst outputs, traces, critiques, citations as rows, the held record, journal entries, users and sessions, runtime and governance state | the source of truth; every durable write lands here first |
| **NATS JetStream** | the signal bus, the descriptor-lifecycle streams, the job work-queues, the dead-letter stream, the consult relay | transport and notification, never a dataset; the durable copy is always in Postgres |
| **Qdrant** | signal vectors for dedupe, plus the curated `world_context` and `tradecraft` retrieval corpora | similarity, which SQL cannot answer |
| **Redis** | geocode cache, ingest-dedupe hints, registry health, source state | a cache with a time-to-live; never a source of truth |
| **OpenSearch** | one index over the whole raw body of every ingested signal | lexical recall over full text, which the relational pool indexes only shallowly |
| **Filesystem archive** | content-addressed bytes: what each fetch received (`fetch_versions`) and the archived pages of cited signals; older objects can move to an S3-compatible cold tier | a receipt chain that ends at a URL ends at something that rots |

There is no graph database. The Apache AGE graph inside Postgres held no row
and was dropped by migration `20261115T1829_drop_empty_age_graph`; the extension
stays installed until the database image is swapped for stock Postgres (SEAMS).
The operative graph is relational: `nexuses` and the id-keyed `entity_edges`,
with graph computation in in-process networkx, and an optional rebuilt
projection (`graph_arcs`, off by default) that names every cross-layer join in
one vocabulary. A second query language, a second dialect and a sync pipeline
would buy nothing, and the relational tables are transactional with everything
else. `DATA_MODEL.md` lists the tables.

## 3. The descriptor model — an instance is its descriptors

An instance of Legba *is* a set of descriptors. Sources, desks, analysts,
capability packs and stack components are declarative YAML documents registered
through one API into Postgres; the runtime is generic and reads them. The live
set is the `*_descriptors` database rows, not the files under `descriptors/` —
the files are the reviewable home of a body, and a registrar script pushes them
in so the tree and the registry cannot drift.

The main families share one shape (content-hashed, versioned, lifecycle-managed,
audited); collections, the bounded holdings of the past, are a further family of
the same shape (`COLLECTIONS.md`):

| Family | Owns |
|---|---|
| **source** | acquisition: kind, `poll` or `push`, cadence, provisioning hooks, the baseline pipeline, output policy, subscription policy |
| **target** (a desk) | what to watch: a polymorphic scope, the `SourceRef` subscriptions it reads, `allowed_action_packs` |
| **analyst** | how to reason: kind, subscription predicate, method, cadence, outputs, `action_packs`, optional grounding and eval blocks |
| **action pack** | a capability bundle: tools, prompt fragments, rules, channels, a governor, an applicability predicate |
| **stack component** | a substrate or model endpoint: one of nine kinds (`llm_provider`, `embedding`, `nlp_service`, `search_provider`, `vector_store`, `nats`, `postgres`, `redis`, `proxy_pool`), with credentials held by reference |

The model rests on four properties.

**Content-hashed identity.** A descriptor's `version` is the hash of its body,
so "is this the same descriptor as before?" needs no hand-managed version
string. A desk can be re-tuned fifty times a day, each change producing a new
content hash, while the schema version stays stable for months.

**A lifecycle FSM** — `draft → configured → active → paused → retired`, plus an
`error` state — in `runtime/lifecycle.py`. `configured` is the explicit
"validated, components bound, ready but not running" step, because registering a
descriptor and running it are different decisions. A new descriptor that needs
a person's verification ships `state: draft`; bulk registration of drafts creates
no actor at all, so many descriptors can be registered at once and activated one
desk at a time.

**An audit trail that cannot be skipped.** Every mutation appends an
Ed25519-signed, hash-chained row to `descriptor_audit_log` in the same
transaction as the mutation, so an audit-write failure aborts the change rather
than leaving an unrecorded one. The row names who made the change (a principal:
a signed-in user, a service, or the shared admin token), and every descriptor
version carries the same principal in `written_by`.

**A dead-letter queue on validation failure.** A malformed descriptor lands in
`descriptor_dead_letter` with a NATS event, and the caller is told the mutation
did not land. A half-registered descriptor is the failure this forecloses.

State changes publish on `descriptor.<action>.<family>.<id>`, which the runtime's
reconcile loop consumes (§12). Register, configure, activate: a content change
needs no redeploy. The pattern is the one Kubernetes custom resources and
Prometheus scrape configs use, and it covers the analysts too, so the reasoning
layer is configured the same way as the sources.

Descriptor config fields use named property factories (`schemas/properties.py`)
— `Property.Secret`, `Property.StackRef`, `Property.Cron`, `Property.Dropdown`
and the rest — so the runtime knows enough to render a form, validate a value,
store a credential by reference and resolve a dynamic option without each
descriptor reinventing them. A secret is always a vault reference; plaintext
never enters a descriptor body.

Descriptors carry an `abstraction_level`: an L1 instance with every field
explicit, an L2 template that others inherit from, or an L3 pattern that
materialises many L1 descriptors as a coherent set. A descriptor with a
`discovery` block must be L2 or L3, enforced by a model validator — a template
materialises instances, and an instance cannot.

## 4. Acquisition — ingest once, enrich once

A `SourceActor` owns one source descriptor. It polls on a durable reminder or
wakes on an inbound webhook, and produces exactly one canonical signal per
observation.

Baseline enrichment runs synchronously on that signal, at acquisition, before
fan-out, and is deterministic or local NLP — no analyst language model on this
path. The stages are declared per source in `pipeline.enrichment` and run in
order: language detection, named-entity recognition, fact extraction,
classification, geocoding. Enrichment mutates the signal in place; there is no
separate enrichment table. The results are promoted into typed, indexed columns
— `language`, `geo`, `tags`, `entity_classes` — which are the coarse axes the
fan-out matches on.

Two things are written at altitude 0 on this path besides the signal itself:
temporal `facts` stamped with the signal's event time, and entity rows with
their links back to the spans that produced them.

Ingest dedupe runs here too, in two cheap deterministic tiers — canonical-URL
hash, then content hash. It **links duplicates and deletes nothing**: a duplicate
raw row is kept and pointed at its canonical through `signal_aliases` and
`canonical_signal_id`, so a dedupe decision can be revisited when the rule
changes.

The signal is then published once to a deliberately coarse NATS subject,
`legba.signals.<tenant>.<source>.<modality>.<event_class>`. Subject tokens
cannot contain dots, so a source id is flattened before it becomes one.

The whole ordering — and the substrate write contract, the per-poll outcome
ledger and the source catalog — is `DATA_SOURCES.md`.

## 5. Fan-out and triggering — match many

A desk references shared sources through `SourceRef` entries, each of which is
exactly one of an explicit `source_id` or a `source_selector` predicate over
source *scope*. A selector auto-wires newly discovered matching sources, but only
for sources whose own `subscription_policy` is `open`; `allowlist` and `grant`
need an explicit opt-in. The policy lives on the source because the source owns
who may read it.

The subscription engine resolves each active desk's references into authorised
bindings and binds **one aggregated JetStream consumer per desk**,
subject-filtered to that desk's coarse axes. Binding one consumer per desk
rather than one per (desk, source) pair is what keeps the consumer count linear
in desks.

Matching is two-stage:

1. A structured SQL `WHERE` on the indexed columns — `geo`, `tags` and
   `entity_classes` through GIN, `language` and `modality` through btree —
   pinned to the bound source and tenant.
2. A Starlark residual predicate on the narrowed stream, for the long tail that
   an index cannot express (`mentions(...)`, `severity_at_least(...)`).

Subjects stay coarse on purpose: exact matching is the job of SQL and the
residual, and a subject taxonomy that tries to encode arbitrary predicates
becomes un-evolvable.

**When** an analyst runs is decided by two mechanisms, both inside the platform —
there is no external cron in the loop.

- **Coalescing triggers.** A trigger engine over an accumulator marks
  (analyst, desk) pairs dirty as matched signals land and fires on accumulation
  or severity, clamped by a per-pair cooldown. Expensive reasoning is always
  coalesced: the accumulation floor is the cost governor, so a language-model
  analyst fires on a batch and never per signal.
- **Cadence reminders.** A durable Dapr reminder from the descriptor's
  `cadence.fallback_schedule` guarantees coverage when too few signals arrive to
  trip the trigger. The reminder is the scheduler.

An analyst that does not read signals is not wired to the signal trigger. A
composition reads its own units' heads, so registering a reactive trigger for it
would let two unrelated signals wake it ahead of the day's units and then let
the reactive fire's cooldown suppress the correctly-ordered scheduled tick. Only
an analyst whose subscription declares `signal` among its `data_types` gets a
per-desk trigger registration; everything else runs on its cadence alone.

## 6. The analyst kinds

`AnalystKind` is an **open taxonomy**. Twelve kinds are built in —
`inline_target`, `cross_target_raw`, `meta_findings_synthesizer`,
`cross_analyst_correlator`, `relationship_reifier`, `competing_hypotheses`,
`deterministic`, `predictor`, `critic`, `optimizer`, `consult_on_demand`,
`deep_consult` — and an admin registers further kinds at runtime through the
`analyst_kind` vocabulary without a migration. The journal's kind is registered
that way, which is why the built-in count does not move when the journal ships.

The analyst *kind* selects the Python module and the deps bundle. It is distinct
from `method.kind` (`llm_planner`, `llm_single_turn`, `deterministic`, `hybrid`,
`react_loop`, `stat_forecaster`, `critic`, `dspy_compile`), which is what the
runtime dispatches the call on. In the example instance the bounded units are
all kind `inline_target` with `method.kind: llm_planner`, and the compositions
and assessment voices are all kind `meta_findings_synthesizer`. Reusing a kind is
deliberate: a new analyst that fits an existing kind is a descriptor, and every
integration keyed on kind — the verify dispatch allowlist, the judge sampling
default, the findings route — stays a no-op.

The graph composes the same way at every tier: a meta-analyst subscribes to
upstream analyst findings exactly as a desk subscribes to source signals.

Under the `deterministic` kind sits a library of sub-handlers that do the work
where a language model would add cost and noise but no judgement — entity
resolution, cross-source dedupe, supersession, decay and garbage collection,
corpus indexing and retention, the alert trigger scan, the evidence archiver,
the integrity sweep, the scorecard, the standing auditor. `ANALYSIS.md` is the
per-kind and per-handler reference.

## 7. The tower — the record and the voice

The tower is the composed, verified chain of analyst outputs. It reads
bottom-up. The tower below is described as the geopolitical example instance
builds it; the mechanisms are the platform's, the unit and tier names are the
example's.

**Bounded units.** Each answers one narrow question over a cited raw-signal
slice plus a grounding preamble of accumulated facts, situations and graph
structure, and each ends in a mandatory faithfulness pass. In the example, most
units (leadership transition, energy security, escalation, narrative
coordination, internal stability, military posture, economic coercion) bind to
every country desk through one coverage-tag predicate; one (proliferation watch)
is tag-scoped to a subset of desks; and one (disruption status) is scoped off the
country plane entirely, to thematic supply-chain and flow desks on a shorter
window.

That last unit shows that fan-out runs on the tag predicate: a whole new desk
family costs a set of target descriptors and one selector, with no new analyst
kind and no new code path.
Adding a subject is the same move: register a target descriptor and the whole
spine picks it up by predicate.

**Composition is assembly, and the voice is a separate row.** A composition tier
does not write prose about its inputs. It quotes them: each source head's lead
span, byte-for-byte, under ordinals, with a closed connective vocabulary and a
deterministic title. The interpretive read is then written as its own analyst
row, from that assembly and from nothing else, with `derived_from` fenced to the
single assembly it read.

Free-text composition can launder a qualifier: a qualifier a desk attaches to
a claim disappears at the layer above it, so the composition reads more
confident than any read it rests on. A quotation cannot drop a qualifier, and a
voice that cites exactly one row can be graded against that row. The regime
rides one flag, `LEGBA_COMPOSITION_ASSEMBLY`, read in one place, so the cascade
cannot be half-flipped. The code default is off.

The tiers, in order:

| Tier | What it produces |
|---|---|
| `country_composition` | the per-desk **record** — an assembly of its units' lead spans |
| `country_assessment` | the per-desk **voice** — the cross-dimension read, fenced to that assembly |
| `region_composition` | a deterministic region rollup: byte-identical lead carry over the region's member desks, with the roster as the denominator. No model, no prompt, no judge — there is nothing here for a model to add, and a rollup that carries bytes can be verified by arithmetic |
| `world_assessor` | the world **record** — an assembly over the country assemblies, each block carrying that country's own assessment in full as context |
| `world_assessment` | the world **voice**, fenced to that one assembly |
| `escalation_composition`, `escalation_dyad` | thematic cross-desk reads, carrying a correlation guard so correlated desks are not double-counted |
| `hybrid_threat_composition` | a theme-filtered cross-desk read of cross-border hybrid activity (infrastructure sabotage, hoax threats, jamming, drone incursions). It lists several units and admits a desk's head only when its lead span carries one of the theme's terms; a desk with none is reported as quiet, not as a gap. It composes only under the assembly regime, and the world read carries its head as its own labelled thematic block |
| `scorecard_producer` | one banded row per active desk, from high-precision rules over already-verified claims |

Two disciplines hold across every tier. A composition's read slice admits only
verify-passed sub-claims — an inner join on the faithfulness critique — so an
unverified sub-claim is structurally unable to enter. And an empty slice yields
an explicit zero-confidence "no source findings to synthesize" read stamped with
its desk, so a desk that produced nothing shows up as a named gap.

Supersession keeps one live head per desk per analyst; prior heads are kept, not
deleted.

Alongside the tower runs an **interpretive tier** on the journal kind: the
first-person journal and its daily consolidation, a third-person weekly
chronicle, and a faculty of weekly lens analysts that each carry one declared
falsifiable prior, plus a pass that narrates where the faculty reads agree and
split without merging them into a consensus voice. These read the verified tower
top, assert no new fact, and are faithfulness-verified like everything else.

The journal is off-chain, the one exception in the provenance model. A journal
row is a perspective *over* the chain, never a node *in* it: it carries an
always-empty `derived_from`, and `journal_entries` is deliberately absent from
the lineage catalog, so a lineage walk from any fact, situation or finding can
never surface it. Its citations are an up-only reference. The invariant is held
twice — by a gating test and by the grant layer, since neither pack the journal
holds can write a fact. Everything the journal wants to change outside its own
entries goes to a human-gated proposal queue: it can write its own entries but
cannot change its own rules without a person accepting the change.

## 8. Outputs and provenance

`OutputKind` is the closed registry of what an analyst may produce — fourteen
members: `finding`, `situation`, `situation_update`, `hypothesis`, `prediction`,
`alert`, `meta_finding`, `critique`, `fact`, `nexus`, `event`,
`prompt_module_candidate`, `journal`, `scorecard`. Each maps to a table, a
pydantic payload model, an Iglu schema URI and a NATS subject pattern. There is
deliberately no `signal` kind: signals are source-owned rows written by the
canonical ingestion path and never routed through this registry.

Every typed output goes through one wrapper, `write_analyst_output`:

1. Look up the kind's table, payload model, schema URI and subject.
2. Validate the payload. A validation error routes to `output_dead_letter`
   and the run continues; an invalid output never half-lands.
3. Build the universal provenance: `produced_at`, `derived_from` as a UUID
   array, the schema URI, the run id.
4. Insert. `situation`, `hypothesis`, `fact`, `nexus`, `event` and `journal`
   have dedicated tables; everything else lands in the generic
   `analyst_outputs`. The `facts` and `nexuses` routes close any open prior row
   whose value differs, stamping `valid_until` and `superseded_by`, so the
   single open row per assertion *is* "what is true now" while the history
   accrues underneath it.
5. Publish to `analyst.<analyst_id>.<channel>`, best-effort — a broker hiccup
   must not lose a write, so the insert is the source of truth.

There is no second write path, so no output can skip the provenance step.

After the row lands, the actor advances a per-analyst **receipt chain** — a
SHA-256 hash chain over the canonical JSON of each run, threading intermediate
steps and tool calls. It is a single-node integrity chain: it detects an
inconsistent local re-hash, and it is not a signed or distributed guarantee. The
chain heads are periodically checkpointed, and those checkpoint rows are the
Ed25519-signed artifact — the per-run receipts themselves are not signed.

Emit bindings then fire per output kind: a STIX 2.1 bundle producer, and the
alert path. Every alert producer, analyst or host script, calls one function
(`legba.data.alerts.raise_alert`), which records an episode in `alerts` and a
row per raise in `alert_events` with the paging decision and its reason, then
hands paged alerts to the registered sinks with a ledger row per delivery
attempt. Anti-noise rules (a steady-state guard, a page budget with a per-kind
diversity cap, a kill list) decide whether a raise pages, and the invariant is
that **a suppressed alert still writes its row**: nothing is silently dropped.
`ALERTING.md` owns the alert kinds and the page rule.

Lineage is a recursive query over `derived_from`, walked with cycle and dangling
detection. Because those chains cross source-descriptor references, a walk runs
from any finding back to the raw signal and its source. A new finding's
citations are also stored as rows (one per sentence, one per supporting record,
pinned to the stored version the material was read from), so "what does this
sentence rest on" is a join rather than a walk through JSON. For cited signals the
walk terminates in the **evidence archive** rather than a URL: the archiver
fetches the original bytes of signals cited by verified findings and stores them
content-addressed, behind an egress guard, a size cap, per-host politeness and a
licence gate. The address is deliberately backend-relative so a later object
store rewrites zero rows.

## 9. The measurement layer

Every claim is cited; a cited claim is not trusted until it has been scored. The
platform grades itself on three axes that answer three different questions, and
each is kept in its own population.

**Faithfulness — does this claim follow from what it cites?** This runs on every
layer, and it measures groundedness, not truth. Two components combine. A
deterministic citation-presence floor checks each fact-asserting claim in the
prose against the resolved citation bridge; a claim with no marker, or a marker
resolving to no real row, is an unsupported span, and the score is the fraction
of checkable claims that are supported. An LLM judge then refines the per-claim
verdicts. The judge resolves through its own route; by default that is the
same endpoint as the analyst that wrote the read, and an admin can point it at a
different model (§11). The pass is soft-fail: with the
judge unreachable the result degrades to the deterministic floor, stamped as
such and published under a ceiling, never a fabricated number.

The verdict is persisted as a critique, and the fold
`effective_confidence = min(confidence, faithfulness_score)` is applied at read
time. Verification never hard-deletes a finding — a low score gates a visible
low-confidence tier. A low score says a claim is not supported by what it
cites; it does not say the claim is false.

Two rules make the number comparable over time. Every critique stamps the judge
route and a pipeline version, so verdict populations from different judges or
different rule revisions never pool. And the floor may only ever exclude a
finding on judged evidence — an unjudged finding about to be excluded is
re-entered through the judge first, with its trigger and pre-escalation score
recorded, because measurement sampling is a fine basis for a statistic and a bad
basis for an exclusion.

**Correctness — is this read true?** Faithfulness cannot see a tower that is
internally immaculate and factually wrong. The correctness grader scores a
unit's claims against an independent **reference** for that desk and window,
reporting two shares that are unreadable apart: correctness over the claims the
reference bears on, and coverage over everything the unit said. References are
built by a separate job, blind to the substrate — a grader
that could build its own reference could close the loop on itself.
`GRADING.md` covers both.

**The external audit — does the open web contradict a claim?** A standing
auditor samples the world read and a rotated subset of desk reads, extracts
checkable claims, and checks each against live external search through the
governed web pack, recording supported, contradicted, not-found or unchecked. It
writes a heartbeat on every run *including a run that audits nothing*, because
"there was nothing to contradict" and "the auditor is dead" must not look alike
from outside; it makes no core-plane call at all when it has no search binding;
and its verdicts carry their own pipeline stamp, separate from the judge's,
because two instruments measuring different things must never pool.

Around those three sit the calibration surfaces: a per-unit skill scoreboard, a
Brier score over hypotheses resolved exogenously, band calibration over the
scorecard's own claims, the platform's own acute hazard calls, and the
**forecast ledger** (`LEDGER.md`): sealed questions that each name what resolves
them and when, every issuer's probability on each, a resolver that proposes an
outcome from the evidence for a person to confirm, and a scorecard kept against
the world by date. Each reports null where nothing was measured and publishes
a no-skill result when that is the result. A forecast exists **only** as
a ledger row, never as a free-text claim, and a degenerate probability vector
abstains rather than producing a number.

Because every stamp partitions, a readout
that needs resolved outcomes can be starved when the stamp lifetime is shorter
than the resolution horizon. A registry lets a reader pool consecutive stamps
for a metric family only when the lineage affirmatively declares the family
cannot move across that boundary, and it discloses the pooled set on the wire
rather than widening silently.

## 10. Agency — what an analyst is allowed to do

An analyst reasons over untrusted text. Anyone who can publish to a monitored
feed can put a sentence in front of it. So capability is **granted, not
hard-coded**: an action pack is a registered, versioned, content-hashed bundle
of tools, prompt fragments, rules, channels, a governor and an applicability
predicate, and effective capability is the intersection of three independent
grants — what the analyst declares, what the target permits, and where the pack
applies. The target's permit leg is the single lever that keeps the write
surface off, and off is the default.

A pack that is effective still passes a per-pack governor before dispatch —
invocations per hour, calls per minute, sources per window, cost per day — over
an invocation ledger, plus a global token envelope. A breach blocks the tool and
writes a visible governor event. There is no flat tool whitelist
anywhere; this is the only agency surface.

`AGENCY_GATING_MODEL.md` is the full trust model, including why the write tools
are gated off by default and what a denied call leaves behind.

One consequence of that model shows up in external retrieval. Search is a stack
component family like any other, with a provider ladder that escalates only on
degradation, never on preference, and a metered rung that refuses to run when no
spend cap is declared or no cost ledger is bound. Because a search that
quietly returns zero results reads downstream exactly like "nothing exists",
the plane separates statuses: an empty whose engine liveness was never
measured is *unknown* and may not support an absence claim; an empty verified
against a live control probe licenses only a scoped sentence naming the query
and the time; a degraded empty is returned as a tool **failure**, because
a successful zero-result search would mislead every reader downstream.

## 11. The model planes, and who pays

Three roles of model, resolved through stack components:

- **The core analyst plane** — your main chat endpoint, which runs every
  scheduled analyst: the units, the tower, the voices, the journal and lens
  tier, the deterministic-plus-LLM handlers.
- **The judge and grader routes** — the faithfulness judge, the correctness
  grader, the external-audit grader and its second rater. Each resolves through
  its own route. By default the faithfulness judge resolves to the same
  endpoint as the analyst it judges; setting `LEGBA_JUDGE_STACK_REF` points
  every judge call at another registered model.
- **The consult plane** — the on-demand consult and deep-consult kinds run on
  the core plane by default; a person may pick another allowlisted model for one
  request.

The shipped descriptors carry `budget_tokens_per_day: 0` (no per-analyst token
cap), and their LLM routes carry `temperature: 1.0`. The core-plane handler
sends no `max_tokens` unless a caller opts in, so the server applies its own
limit.

`AI_MODELS.md` covers the model roles and how to register an endpoint.

## 12. The actor runtime

The runtime turns active descriptors into **Dapr virtual actors**: addressable,
turn-based (one invocation at a time per id), reminder-driven, scale-from-zero.
Parallelism is across actors, distributed over runtime replicas by Dapr's
placement service. The whole runtime collapses onto one control plane — one
`daprd` per process — which is the seam that retired a separate workflow
backend. There is no Temporal infrastructure anywhere.

Actor ids follow `kind::descriptor_id::tail`, where the tail is the descriptor
content hash for a primary actor and the desk id for a worker. The descriptor
identity is recoverable from the id, so a client needs no lookup table.

**The reconcile loop** bridges desired state (the registry) to observed state
(actor records). Per-kind reconcilers are pure functions of (observed, desired)
returning an action; a single executor is the only mutator, mapping each action
to a lifecycle call on an actor proxy. The informer is a NATS subscription on
`descriptor.>`, a periodic resync is the backstop, and one synchronous resync on
bring-up activates every active descriptor. A content-hash change yields a soft
restart — re-activate, which re-reads the body — so a source cursor survives a
re-tune; a retire-then-create would lose it. The loop is a singleton, gated by a
Postgres advisory lease when more than one replica runs.

**Fan-out.** A primary analyst actor owns the cadence heartbeat and does not
assess inline. On each tick it evaluates its subscription predicate against the
active desks and dispatches one run per matched desk to a distinct
per-(analyst, desk) worker actor, bounded by a semaphore so a wide analyst does
not stampede. Workers lazy-activate with their desk filter in hand and carry no
cadence of their own. Cooldowns are per-(analyst, desk) with a small slack
allowance, because a cooldown equal to the cadence interval otherwise drifts a
run into the next window and skips it.

**Turn budgets.** Dapr actors are turn-based with reentrancy off, so a hung call
inside a turn holds that actor's queue indefinitely, and the reconciler's own
durability heal fires against every active descriptor on every resync. Left
unbounded, one wedged actor turns a heal into a plane-wide freeze. Two bounds
are a pair and neither alone is sufficient: a deadline on the reconciler's
per-actor heal, which bounds what one wedged actor costs the queue, and bounded
I/O *inside* the turn, which is what actually lets the turn complete and release
the queue behind it. A breaker converts repeated timeouts into skip-and-retry
with a cooloff and logs the skip, so a wedged actor shows up in the logs. Every
bound is environment-overridable and falls back to its default on an unset or
malformed value, so a typo cannot silently disable one.

**Failure semantics.** In-flight exceptions classify as transient, budget or
hard. Transient retries with backoff; budget pauses or demotes for the rest of
the day bucket, with an audit row; hard routes to a dead-letter surface and
alerts. Budget is enforced pre-call against a per-(analyst, day) ledger and a
global envelope.

**Durable jobs.** Work that is too long for a turn runs as a Dapr Workflow on the
same sidecar, registered by function name on one runtime: the deep-consult
workflow (plan, acquire, analyze, synthesize) and the optimizer's prompt-evolution
loop. The optimizer is switched off: its descriptors are paused and its entry
point refuses to run. The worker, the code and the tests stay, since the worker
also hosts the deep-consult workflow. The orchestrator body is strictly
deterministic (no wall clock, no randomness, no I/O), with all non-determinism
pushed into activities, which is the replay contract. A request returns a task
id immediately and the caller polls for the persisted result. A workflow
instance id must not contain `::`, because activity result parsing splits on it;
worker actor ids use `::`, workflow ids must not.

A separate NATS work-queue with competing-consumer workers backs bounded,
interchangeable jobs such as media extraction. A completed job lands a derived
signal that re-enters fan-out, so heavy extraction is paid for only when
reasoning needs it.

## 13. How it scales, and where it stops

Scaling follows from the same separation of observation and interpretation.

- **Many analysts on one shared substrate.** Adding a country, a sector or a
  whole domain is registering descriptors into a running instance, not deploying
  another instance. Cross-desk reasoning needs no extra plumbing, because the
  substrate is shared.
- **Shared sources, not per-desk binding.** N desks over one feed is one poll,
  one enrichment, one stored signal, fanned out by predicate.
- **The right execution shape per workload.** Addressable, mostly-idle,
  request-driven work is actors, scaling by replica count. Long durable
  multi-step work is a workflow. High-throughput interchangeable work is a
  work-queue worker pool, scaling by workers. No serialised actor is ever a
  throughput bottleneck — that work is a job.

Analysts run on a schedule, not per signal, so model cost does not grow with
ingest volume. The real ceiling is model throughput, which is why the heavy
graph work is deterministic Python.

Three single-node limits are deliberate: JetStream streams are single-replica;
`signals` is one unpartitioned table with a retention sweep that ships disabled;
and the trigger accumulator's read-modify-write assumes one replica per pair, so
two replicas can interleave and lose a pending-count increment — which costs a
*late* fire, never a lost signal, since the cadence ticker still sweeps dirty
pairs. The reconcile loop and the discovery informer are singletons gated by a
leader lease.

## 14. The perimeter

Legba ships as a single-tenant deployment: one instance, one logical tenant, run
by one owner. The network and deployment perimeter is the outer security
boundary: the edge proxy plus loopback-bound internal services.

Inside it, who may do what depends on one switch, `LEGBA_AUTH_SIGNIN`:

- **Off (the default).** The edge holds one shared admin credential and every
  person who passes the edge acts with it; a write records that credential
  (`shared:admin-token`), never a guessed person.
- **On.** People sign in with local accounts (argon2id passwords, an HttpOnly
  session cookie with an idle limit). Each holds one role: **reader** (reads the
  record, keeps their own Consult sessions; cannot change anything shared),
  **contributor** (a reader who may also make gated judgements: grading labels,
  journal proposal decisions, alert acknowledgements, forecast resolution
  confirmations), or **admin** (sources, descriptors, the stack, users
  and the engine's own surfaces). The **owner** is an admin with a flag, set once
  by a bootstrap command. Every mounted route has a minimum role in one table
  (`registry/route_policy.py`; `API_ROLES.md` is generated from it), each service
  gets its own token, and the shared admin token becomes a logged break-glass
  credential.

There is no multi-tenant isolation, and the acquisition plane's `owner_tenant`
column is forward-compatible metadata, not an isolation guarantee between
untrusting tenants.

Acquisition-plane tenancy is real and enforced at match time: the column is
indexed on `signals`, pinned on the row, the subject and the published envelope
from one source of truth, pushed into the subscription SQL and re-checked in the
residual matcher. Analysis-plane tenancy is not built — `analyst_outputs` carries
no tenant column, so there is nothing to enforce against.

## 15. What is not built

Anything not built is a **declared seam** that fails loud when called.
`SEAMS.md` is the single registry, and a test enforces it: it scans the whole
source tree for stub markers and fails unless each hit is the bare
abstract-method idiom or listed in the machine-readable allowlist in that file.
There is no per-line escape.

The larger seams are media extraction (the service ships and refuses every
request), the subscribe acquisition
mode's further transports (the mode and its first transport are built, behind a
switch that is off), the cheap-model budget demotion path (which today equals an
audited pause), the correcting half of the standing-question watcher, the held
record's promotion, contradiction and expiry rules, and retention sweeps that
ship disabled. `STATUS.md` lists the capabilities that are built and their
limits.

## 16. Read next

- `ANALYSIS.md` — the kinds, the units, the compositions, the judge, the grader,
  the audit, in depth.
- `FLOWS.md` — life of a signal, an analyst cycle, a fact, a consult.
- `DATA_MODEL.md` — the tables, by plane, including who writes each, citations
  as rows, the held record, events, the temporal surface and the graph
  projection.
- `../CONTRIBUTING.md` — the implementation rules new code follows.
- `AGENCY_GATING_MODEL.md` · `AI_MODELS.md` · `TUNABLES.md` · `SEAMS.md` ·
  `STATUS.md`.
- `RUNBOOK.md` — bring-up, health, and what to do when something is wrong.
