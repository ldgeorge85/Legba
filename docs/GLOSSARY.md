<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Glossary

Short definitions of the terms used across the Legba docs, grouped by area and
alphabetical within each group. Each entry links to the page that explains it
in full. New to Legba? Start with the [Tour](TOUR.md).

## Core concepts

**analyst**: A declared reasoning unit, either deterministic code or
model-backed. It reads a scoped slice of the substrate, runs one method, and
writes typed outputs with provenance. See [ANALYSIS.md](ANALYSIS.md).

**bounded reasoning unit (unit)**: An `inline_target` analyst that answers one
narrow question for every desk its tag predicate matches. Each run writes one
cited finding, which then goes through faithfulness verify.

**composition**: An output built only from verified sub-claims of the tier
below it. Country, region and world compositions stack into the
**composition tower**; every claim can be walked back down to a source.

**descriptor**: A strict, content-hashed config record that declares a source,
target, analyst, pack or stack component. The runtime starts one actor per
active descriptor. See [ARCHITECTURE.md](ARCHITECTURE.md).

**desk / subject**: The console's names for a target. A desk can be a country,
a region, an organisation or a theme such as a shipping lane.

**finding**: The main analyst output: a written conclusion with `derived_from`
provenance. It is re-published as a derived signal so other analysts can react
to it.

**profile**: A package of prompts, vocabularies, descriptors and UI labels that
fits the platform to one domain. Geopolitics is the shipped example. See
[PROFILES.md](PROFILES.md).

**provenance / lineage**: Provenance is the recorded origin of a row. Lineage
is the chain of `derived_from` links from an output back to its signals and
source URLs, served by `GET /api/v1/lineage/finding/{id}`.

**scorecard**: One deterministic row per scored desk that bands each unit
dimension from verified claims in a rolling window. A dimension with no
qualifying claim reads `insufficient-evidence` with a reason.

**signal**: One ingested observation (a document, feed item, record or media
reference) with its content, metadata and provenance. A signal has no
`target_id`, so one signal can route to many targets.

**situation**: A durable record of an ongoing state of affairs that groups
related signals and findings. Situations link to **events**, bounded
occurrences stored in the `events` table.

**source**: A connector that acquires observations by polling, webhook push or
a held-open subscription, enriches each one, and publishes signals. See
[DATA_SOURCES.md](DATA_SOURCES.md).

**source-first**: The design rule that acquisition belongs to sources. A
source ingests and enriches an observation once; fan-out routes it to every
matching target.

**target**: A passive subscriber that selects a slice of the shared signal pool
by predicate. Analysts write about targets.

## Runtime & architecture

**actor types**: `SourceActor` runs one source, `TargetActor` represents one
subscriber, and `AnalystActor` collects matched signals and runs reasoning.
Each is a Dapr virtual actor started from a descriptor.

**baseline enrichment**: The deterministic steps run on every signal at ingest:
language detection, geocoding, entity extraction, classification, source
credibility, dedupe and fact extraction.

**cadence**: A scheduled interval on which an analyst runs even when no new
signals have triggered it.

**coalescing trigger**: Collects matched signals for an (analyst, target) pair
and runs the analyst once per batch when a threshold trips. A **cooldown** sets
the minimum gap between runs.

**control plane / runtime plane**: The control plane is `legba-registry`
(descriptors, lifecycle, API, vault). The runtime plane is
`legba-runtime-dapr`, which runs the actors.

**Dapr**: The distributed-application runtime Legba runs on. It provides
virtual actors, persistent reminders for cadences, and durable workflows for
the optimizer and deep consult.

**fan-out**: The routing step that delivers one signal to every target whose
predicate matches: NATS subject filtering, then SQL, then a sandboxed Starlark
expression.

**job plane**: A durable NATS work queue with competing workers for heavier
work such as `process_media`. An idempotency key makes duplicates run once.

**predicate**: A single boolean expression that decides whether a signal
matches. Used on target scopes, source filters, analyst bindings and cadence
triggers.

**seam**: A capability that is deliberately not built. It is listed in
[SEAMS.md](SEAMS.md), and its code path raises an error instead of returning
placeholder output.

**substrate**: The storage layer: Postgres (relational data and the entity
graph), Qdrant (vectors), OpenSearch (full-text search), Redis (cache) and NATS
JetStream (streams).

**Tier 1 / Tier 2**: Tier 1 is per-signal, deterministic work at ingest. Tier 2
is batched, scheduled reasoning by analysts, so model cost does not grow with
ingest volume.

**type registry**: The code registry of declared types under
`src/legba/data/type_registry/`. Writers refuse an undeclared type. See
[TYPES.md](TYPES.md) and [ADDING_A_TYPE.md](ADDING_A_TYPE.md).

## Data model

**canonical signal**: Duplicate signals are linked to one
`canonical_signal_id` through `signal_aliases`. The duplicate rows are kept.

**citations as rows**: A finding's citations are also stored as rows
(`finding_sentences`, `citation_support`, `citation_unresolved` and others),
insert-only. See [DATA_MODEL.md](DATA_MODEL.md).

**contention**: Two open facts with different values for the same subject and
predicate. Both stay open and `fact_contention_arbiter` records the
disagreement without changing either. Off unless `LEGBA_FACT_CONTENTION` is
set.

**earned track record**: How often a source's claims ended on the winning side
of resolved contentions, written by the draft `source_track_record` analyst.

**entity**: A resolved real-world actor (person, organisation, place) stored as
one profile, with different spellings merged.

**evidence archive**: Stored copies of evidence cited by verified findings,
named by their SHA-256 (`object_ref = cas:sha256/<hex>`). Archived signals are
marked `evidence_hold` and are not purged.

**fact**: A versioned (subject, predicate, value) row in `facts` with validity
times, a `source_type` and a confidence. Only `seed` and `curated` facts feed
grounding.

**fetch receipt (`fetch_versions`)**: The bytes each fetch received, stored as
versions per address. Records parsed from a fetch link to its version, so a
later change to a cited page is visible.

**graph projection (`graph_arcs`)**: A derived table of joins between layers
(mentions, entity edges, events, citations). Rebuilt whole. Off unless
`LEGBA_GRAPH_PROJECTION` is set.

**held record (`assertions`)**: Versioned statements with a hash-chained status
log. Written only when `LEGBA_HELD_RECORD_SHADOW` is set, and not shown to
readers.

**license_class / source_class**: A source's declared licence and editorial
class (reporting, analysis, official, state media), each from a closed list.
The evidence archiver refuses material with no licence class.

**modality**: The medium of a signal: text, image, audio, video, structured or
binary. Each maps to an extractor and a UI renderer.

**narrative**: One row per family of competing accounts, recording which
sources carried it and when. Written by the draft `narrative_mapper` analyst.

**nexus**: A stored relationship between two entities with a type, a polarity
(+1, 0 or −1), a confidence and validity times.

**origin group**: The unit used to count independent sources, so two feeds of
one publisher count once. An admin can correct a group.

**OutputKind**: The closed list of typed analyst outputs, such as finding,
situation, hypothesis, critique, fact, nexus, journal, scorecard and event.

**principal / `written_by`**: Who wrote a row, as a handle such as
`user:<name>`, `agent:<analyst>@<version>` or `shared:admin-token`.

**receipt chain**: Each analyst run's `analyst_traces` row carries a
`receipt_hash` chained over the previous run's. This checks consistency on one
node; findings are not signed.

**seed**: Curated reference data imported as facts and nexuses with
`source_type='seed'`. The repository ships only `seeds/*.example.yaml`.

**source_credibility**: A per-fact trust weight. `NULL` means unknown, not
zero.

**supersession**: When a newer, different value arrives, the older fact is
closed (`valid_until`, `superseded_by`), not overwritten, so history is kept.

**unit reference**: A reference for one desk and window, built from the open
web by the reference builder. The correctness grader grades desk reads against
it. See [GRADING.md](GRADING.md).

## Analysis & methods

**analyst kind / `method.kind`**: `kind` says what an analyst reads and writes
(`inline_target`, `critic`, `consult_on_demand`, ...). `method.kind` says how
it reasons (`llm_planner`, `react_loop`, `deterministic`, ...).

**assembly (`assembly.v1`)**: What a composition writes: quoted spans copied
exactly from the desk reads beneath it, each with its hash and byte offsets.
Writing one makes no model call.

**basis / periphery**: With tiered evidence on, the basis is the verified
sub-claims above the confidence floor. The periphery is a capped set of weaker
claims that may only support hedged context.

**calibration**: Checking predictions and hypotheses against later outcomes.

**`carried, ungraded`**: The verify result for a unit read that cites only the
desk's own earlier reads. No score is published.

**competing hypotheses (ACH)**: Analysis of Competing Hypotheses: score
evidence against a thesis and counter-thesis by how well it tells them apart.

**consult / deep consult**: On-demand question answering. `consult` runs a tool
loop over read-only substrate tools and returns cited answers. `deep_consult`
runs a longer workflow and saves the result.

**correctness grader**: Grades a desk read against an independent unit
reference. With no current reference it writes nothing. See
[GRADING.md](GRADING.md).

**critic**: An analyst that scores another analyst's output against a rubric.
It can only lower `effective_confidence`.

**cross-framing**: One published claim shown beside every unit of its desk:
how each unit's read treats the same matter, or the typed absence explaining
its missing read. Shown in the `analysis.cross_framing` panel.

**desk baseline**: A per-desk statistical prior from the desk's own signal
history, written by the draft `desk_baseline` analyst. It is not a forecast.

**effective_confidence**: `min(confidence, faithfulness_score)`, computed at
read time. When verify did not run it is `None` and the claim shows as
`verify-failed`.

**external audit**: An analyst that checks claims from desk and world reads
against the web. It records supported, contradicted, not-found or unchecked
per claim. See [GRADING.md](GRADING.md).

**faithfulness verify**: The pass that scores whether each cited claim follows
from its cited evidence. It measures grounding, not truth. By default the judge
is the same model endpoint that wrote the finding.

**forecast ledger**: Sealed forecast questions, each naming what resolves it
and when. A person confirms each proposed outcome. See [LEDGER.md](LEDGER.md).

**forecast_scoreboard**: The weekly forecast pilot, scored by Brier score
against a baseline. Skill is withheld (`forecast_unproven=True`) until it
beats the baseline.

**grounding preamble**: A dated block of currently valid `seed` and `curated`
facts added to a unit's prompt, so the model does not rely on stale training
data.

**hypothesis**: A candidate explanation with a required counter-thesis and a
running evidence balance, resolvable against outcomes.

**inline_target**: The per-target model analyst kind. It reads one target's
recent signals and the grounding preamble and writes one cited finding.

**journal assessor**: A global analyst (`journal_assessor`) that writes
first-person entries across the whole system, plus a weekly **chronicle**.

**Journal (OutputKind)**: Output stored in `journal_entries`, off the
fact/finding/nexus chain: `derived_from` is always empty and it cannot write a
fact, finding or nexus.

**judge route**: How the verify judge's model is chosen:
`LEGBA_JUDGE_STACK_REF` first, then the descriptor's own refs. The model used
is recorded with each verdict.

**lenses**: Weekly analysts on the journal kind (`lens_trend`,
`lens_baserate`, `lens_capability`, `lens_intent`), each with one declared
prior. `lens_diff` describes where they agree and differ.

**leans (stance-typed lenses)**: Six more lenses in the geopolitics profile
(`lens_left`, `lens_right`, `lens_centre`, `lens_pragmatist`,
`lens_militarist`, `lens_isolationist`), each committed to one stance. They ship
as draft.

**META analyst**: An analyst with no single target that runs over other
outputs, such as the compositions and `scorecard_producer`.

**open question**: A question a read could not settle, kept on a backlog. The
**staleness debt** counts open questions whose evidence has moved.

**optimizer (GEPA)**: Prompt tuning for one unit, run in an isolated worker. A
person approves promotion. `_honest_null_eval` records a null delta when a
candidate cannot be measured.

**record**: What a composition tier writes: one quoted lead span per verified
input under a deterministic title.

**reference builder**: The analyst that builds unit references from the open
web. Its output must pass fixed checks that run after the model finishes. See
[GRADING.md](GRADING.md).

**typed absence**: An absence with a scope, a kind, a proof and a shelf life,
never a blank. Each item says what was checked, when (`as_of`), and when it
expires (`expires_at`).

Kinds: `not_collected`, `collected_but_silent`, `source_stale`,
`searched_found_nothing`, `search_failed`, `below_floor`,
`layer_declared_absent`, `history_gap`, `carried_unsupported`. Served by
`GET /api/v1/v3/absence?scope=<target_id>`.

**unassessable / provisional**: Verify results without a clean score.
Unassessable: no score is published. Provisional: the model judge did not run
(`judge-unavailable`), so the score is capped and labelled.

**world_assessor**: The global composition over the region reads, drillable
down to a source.

## Operations & governance

**action pack (pack)**: A versioned bundle of tools and limits granted to an
analyst, such as `substrate_read` or `web_access`. A tool missing from the pack
fails as `unknown_tool`.

**agency**: An analyst's ability to call tools during a run, granted only
through packs. Web fetches go through `SsrfGuardedTransport`, which refuses
private and loopback addresses.

**agency gate**: A pack is effective only where the analyst's grant, the
target's allow-list and the pack's applicability overlap. See
[AGENCY_GATING_MODEL.md](AGENCY_GATING_MODEL.md).

**alert sinks**: A webhook and an ntfy sink, set by `LEGBA_ALERT_WEBHOOK_URL`
and `LEGBA_ALERT_NTFY_URL`. An unset sink logs `skipped_unconfigured`. See
[ALERTING.md](ALERTING.md).

**credential vault**: An encrypted store for source and model secrets.
Descriptors hold only a pointer to a secret.

**dead-letter queue**: Tables that hold malformed or failed descriptors and
outputs so they can be fixed and resubmitted.

**emit / output binding**: Handlers that send a stored output elsewhere (STIX,
webhook, NATS, A2A, MCP) after it is written. A failed send never undoes the
write.

**escalate_finding**: The pack that raises an alert when a finding's
`effective_confidence × severity` crosses a gate.

**governor**: `PackGovernorEnforcer`, which applies per-pack call and rate caps
before each tool call and logs every decision.

**`honest_quiet`**: The watchdog's label for an empty poll whose crawl found
nothing newer than what is stored. It is not a stall until the streak reaches
36 (`LEGBA_SOURCE_HONEST_QUIET_STREAK_THRESHOLD`), which raises
`honest_quiet_prolonged`.

**ingestion intake**: One queue of requests to fetch an address, re-poll a
source or re-check a stored page. See
[operations/on-request-fetching.md](operations/on-request-fetching.md).

**lifecycle**: The states a descriptor moves through: `draft`, `configured`,
`active`, `paused`, `retired`.

**propose-and-gate**: Changes the journal asks for wait in
`journal_proposals` until a person accepts or rejects them.

**registration**: The bring-up scripts that insert descriptors into the
registry. Later changes go through the registry `PUT` API. See
[SETUP.md](SETUP.md).

**security perimeter**: Caddy serves the console over HTTPS and proxies `/api`
to the registry. Registry routes require a bearer token.

**sign-in / roles**: Local accounts behind `LEGBA_AUTH_SIGNIN` (off by
default). Roles are reader, contributor and admin, plus an owner flag. Legba is
single-tenant. See [API_ROLES.md](API_ROLES.md).

**stack**: Registry descriptors for shared components (databases, NATS, model
endpoints, embedder). Analysts bind to them through a `StackRef`.

**token budget**: Optional per-run and per-day token caps on an analyst. When
one runs out the run is skipped as `budget_throttled` until the window resets.

**watchlist**: A person's standing watch on an entity, topic or place. Matches
alert as `watchlist_hit`.

## Console

**page**: The console has eight pages. Reader side: Board, Subject, Ledger,
Watch. Operator side: Engine, Evaluation, Gate, Investigate. See
[UI.md](UI.md).

**panel**: One registered view, such as `system.status`, opened in a region of
a page.

**phone shell (`/m/`)**: The installable phone web app, using the same API as
the console.

**Record sheet**: The drill-in behind a statement that shows its citations,
verify result and source hops.
