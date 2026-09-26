<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# FAQ

Short answers, linked to the page that owns the long one. Terms are defined in the
[Glossary](GLOSSARY.md); counts are generated into [RELEASE_STATE.md](RELEASE_STATE.md); per-feature
state is in [STATUS.md](STATUS.md).

## What it is

**In one sentence?** An intelligence service you define in YAML, and that measures its own
correctness. It turns sources into cited, verified, drillable reads over whatever domain you
configure, and grades those reads on three axes it publishes.

**What does "source-first" mean?** Acquisition belongs to sources, not to the things that consume
data. A source ingests an observation once, enriches it once, and publishes one canonical,
target-agnostic signal; the fan-out plane routes that one signal to every desk whose predicate wants
it. One feed serves every desk without refetching. It is unrelated to the licence.

**Is this an intelligence tool?** The geopolitics desks are the shipped exemplar, not the system's
identity. The same pipeline applies to any domain you can point a source at. It performs cited
situation assessment over text and structured data; it claims no sensor-fusion rigour and does not
claim to establish truth.

**Does it forecast?** Not as a claim. Forecasting exists only as a pre-registered scoreboard of precise
questions with resolution windows, scored by Brier, never surfaced as a finding, and it reports no
proven skill today. Retired forecast-as-claim analysts are listed in [SEAMS.md](SEAMS.md).

**How is it different from a scraper, a SIEM, or an LLM agent?** A scraper fetches; Legba fetches
once and fuses into a temporal, deduplicated, provenance-tracked substrate. A SIEM correlates
telemetry for one estate with detection rules; Legba is a general assessment pipeline over arbitrary
sources whose product is a traceable analytic judgment. An LLM agent is one reasoning loop; here the
reasoning is decomposed into narrow units whose every claim is cited, verified and traceable, with the
substrate rather than the model's memory as the source of truth.

## How it works

**What does the analysis produce?** A stack built bottom-up. Nine bounded units, one narrow question
each, over cited signal slices plus grounding from the substrate. A country record per desk that
quotes its verified unit claims byte for byte, with a country voice fenced to that record. A
deterministic regional rollup and a world record that carries the country records, with a world
voice that reads the country voices. A banded scorecard from verified claims that says "insufficient"
where they support nothing. A journal, a chronicle and a set of interpretive lenses off the product
chain. [Analysis](ANALYSIS.md) has each mechanism.

**What is the faithfulness pass?** Every cited read is scored by a judge model plus a deterministic
citation floor for whether each claim follows from the evidence it cites. The score folds into
`effective_confidence = min(confidence, faithfulness)` at read time and gates a visible low tier; it
never deletes. The judge runs cross-family on a hosted route, repointable by one env line; when the
route is down the pass degrades to the floor and says so. Every critique stamps the judge model and
pipeline revision, so populations never pool.

**Does it measure truth?** The faithfulness pass measures groundedness, not truth: a well-cited claim
from a wrong source can score faithful. Two other graders exist to reach the world. The correctness
grader scores the desks against references the platform built independently, with three model
families. The external audit samples top-layer claims and checks them against live web search,
including claims that something did not happen. The three are never pooled.

**Descriptor, analyst, target, source?** A descriptor is the declarative record you register. A target
is a declared subject frame, a desk, not a surveilled entity. An analyst reads a slice of substrate
and writes typed outputs about targets. A source acquires data. You declare descriptors; the runtime
stands up actors from them.

**Do I write code per source or per country?** No. The desks are materialised from descriptors and one
tag predicate; the units fan out by predicate; adding a country is registering a target. A desk need
not be a country: the thematic supply-chain desks run the same primitive with no new code.

**What is the substrate?** Postgres for the relational and temporal store, Qdrant for vectors, Redis
for hot state, OpenSearch for full text, NATS for the event bus. Facts, relations, situations and
events carry validity windows, so it is a temporal knowledge graph that grows continuously. Events,
as-of readers and a typed cross-layer graph are the v3 data model:
[DATA_MODEL_V3.md](DATA_MODEL_V3.md).

**Why does a signal carry no target?** Because it is a neutral observation; the same signal routes to
many targets. That target-agnosticism is the keystone of the source-first model.

**What is a nexus? A situation? An event?** A nexus is a first-class typed, polarity-signed
relationship row between two entities. A situation is a durable thematic cluster of related findings
and signals with a trajectory ledger. An event is a bounded occurrence with time, place, participants
and evidence links, minted from the tower.

**What is the journal?** The platform's first-person reflective voice, pointed at itself, off the
finding chain by construction: its rows carry an empty lineage, land in their own table, and never
appear in a lineage walk. The chronicle is the third-person public record; the lenses are declared
falsifiable priors that read the verified tower top and argue about it. Anything outward from these
goes to a human-gated proposal queue.

**What is Dapr, and an actor?** A distributed-application runtime; a virtual actor is an
addressable, single-threaded stateful object. Each active descriptor becomes one actor.

**How does a signal reach the right analysts?** By predicate: coarse subject filtering, a SQL
narrowing, then a bounded residual predicate. An analyst coalesces the signals routed to its target
and fires when a gate trips or its cadence does.

## Scope and honesty

**What is proven and what is experimental?** The full spine runs end to end from a cold start and is
graded three ways. The experimental legs return only as measured experiments with a human gate: the
prompt self-optimiser promotes nothing until its measured delta is positive; forecasting reports no
skill; calibration is honest-null where unmeasured. Retired analysts stay retired and unread. The
truth-in-labeling table is [STATUS.md](STATUS.md).

**Why does a desk read "insufficient"?** Because its evidence did not clear the bar. The scorecard
bands only from verified claims and names the claim each band rests on; where none qualifies it says
so with the reason. That is the honest output surfacing weak inputs, not a gap being papered over.

**What is a seam?** A deliberately unbuilt capability, declared in [SEAMS.md](SEAMS.md), that refuses
to run rather than faking output. A stub scanner enforces the rule. Hitting a seam means the feature
does not exist yet, not that it is broken.

**What does provenance give me?** Every output records what it derived from, so lineage walks back to
the raw signal and its real source URL with no dangling links. Every analyst run writes a hash-chained
receipt; a lineage node shows "chain-consistent (single-node)", an integrity check on one node, not a
distributed signature. The point is that you can verify how an output was produced.

**Can an analyst act?** Only through governed agency: tools come from an allow-listed action pack,
enforced by a governor against budgets and the grant, allow and applicability gate, fail-closed. Web
fetches are guarded and flagged; agent-proposed writes are capped in confidence and can never touch
authoritative data or the control plane.

## Running it

**Can I self-host it, and under what licence?** Yes; that is the design. AGPL-3.0-or-later, whose
network clause covers networked use. A commercial licence is available; an outside code contribution
needs a CLA. You run it yourself and can audit every output down to the source.

**What does a deployment look like?** A profile-gated compose stack brought up by one script in the
order that matters. Fresh volumes only; there is no migration path from earlier designs. Start with
the out-of-box catalog and activate further sources one at a time. [Setup](SETUP.md).

**What does it cost to run?** Every scheduled analyst runs on the self-hosted core plane at no
per-run cost. The hosted routes, the judge, the audit grader and rater, the grader's paid families,
and consult, are each one config line with a cost brake, and the plane that pays for each path is in
[TUNABLES.md](TUNABLES.md).

**Is the geopolitics use case hard-coded?** No. Point a source at a different domain and declare
different targets and analysts, and the same cite, verify and audit pipeline applies.
