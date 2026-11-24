<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# FAQ

Short answers, linked to the page that owns the long one. Terms are defined in the
[Glossary](GLOSSARY.md); per-feature state is in [STATUS.md](STATUS.md). Where an answer uses the
geopolitics example instance, it says so.

## What it is

**In one sentence?** A measured data-analysis platform: you define what to watch and what questions to
ask in YAML; it writes cited reads and grades its own correctness, and publishes the grade with its
coverage. It turns sources into cited, verified, drillable reads over whatever subjects you configure,
and grades those reads on three axes it publishes.

**What does "source-first" mean?** Acquisition belongs to sources, not to the things that consume
data. A source ingests an observation once, enriches it once, and publishes one canonical,
target-agnostic signal; the fan-out plane routes that one signal to every subject whose predicate wants
it. One feed serves every subject without refetching. It is unrelated to the licence.

**What is the platform, and what is the example instance?** *The platform* is the software: sources
that emit one canonical observation, a temporal substrate, bounded reasoning units, composition by
quotation, and the checking layer (faithfulness judge, correctness grader, external audit, contrary
pass, typed absence), all configured by descriptors. It performs cited assessment over text and
structured data; it does not claim to establish truth. *The example instance* is geopolitical
open-source analysis: its country desks, region rollups, world record and political-lean lenses are
that instance's descriptor set, not the platform's shape, and any number from it is that instance's.
An *instance* is the platform plus a profile ([PROFILES.md](PROFILES.md)); a sanctions and
export-control skeleton profile ships as a second shape. The root [README](../README.md) has the
longer frame and [DIRECTION.md](DIRECTION.md) where it is going.

**Does it forecast?** Not as a claim. Forecasts live in a sealed ledger of precise questions, each with
a resolution rule and a date; a resolver proposes each outcome, a person decides it, and the scorecard
withholds every score until enough questions have resolved ([LEDGER.md](LEDGER.md)). Forecasts are
never surfaced as findings. Retired forecast-as-claim analysts are listed in [SEAMS.md](SEAMS.md).

**How is it different from a scraper, a SIEM, or an LLM agent?** A scraper fetches; Legba fetches
once and fuses into a temporal, deduplicated, provenance-tracked substrate. A SIEM correlates
telemetry for one estate with detection rules; Legba is a general assessment pipeline over arbitrary
sources whose product is a traceable analytic judgment. An LLM agent is one reasoning loop; here the
reasoning is decomposed into narrow units whose every claim is cited, verified and traceable, with the
substrate rather than the model's memory as the source of truth.

## How it works

**What does the analysis produce?** A stack built bottom-up. Bounded units, each answering one narrow
question over a cited slice of signals plus grounding from the substrate. Compositions that quote the
verified unit claims byte for byte, each with an interpretive voice fenced to its own record. A banded
scorecard from verified claims that says "insufficient" where they support nothing. A journal, a
chronicle and interpretive lenses off the product chain. An instance declares its own units and its
own top floor; in the example instance the floors are a country record per desk, a deterministic
regional rollup and a world record. [Analysis](ANALYSIS.md) has each mechanism.

**What is the faithfulness pass?** Every cited read is scored by a judge model plus a deterministic
citation floor for whether each claim follows from the evidence it cites. The score folds into
`effective_confidence = min(confidence, faithfulness)` at read time and gates a visible low tier; it
never deletes. The judge route is repointable by one configuration line; when the route is down the
pass degrades to the floor and says so. Every critique stamps the judge model and
pipeline revision, so populations never pool.

**Does it measure truth?** The faithfulness pass measures groundedness, not truth: a well-cited claim
from a wrong source can score faithful. Two other graders exist to reach the world. The correctness
grader scores the subjects against references built independently of the platform's own substrate. The external audit samples top-layer claims and checks them against live web search,
including claims that something did not happen. The three are never pooled.

**Descriptor, analyst, target, source?** A descriptor is the declarative record you register. A target
is a declared subject frame, a desk, not a surveilled entity. An analyst reads a slice of substrate
and writes typed outputs about targets. A source acquires data. You declare descriptors; the runtime
stands up actors from them.

**Do I write code per source or per subject?** No. Subjects are materialised from descriptors and one
tag predicate; the units fan out by predicate; adding a subject is registering a target. In the example
instance a desk need not be a country: the supply-chain desks and the cyber desk run the same primitive
with no new code.

**What is the substrate?** Postgres for the relational and temporal store, Qdrant for vectors, Redis
for hot state, OpenSearch for full text, NATS for the event bus. Facts, relations, situations and
events carry validity windows, so it is a temporal knowledge graph that grows continuously. Events,
as-of readers and a typed graph projected into ordinary tables (there is no separate graph database)
are in [DATA_MODEL.md](DATA_MODEL.md#events-temporal-and-graph). Citations are stored as rows beside
each finding, and every row that records a judgement names its writer.

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
graded three ways. The experimental parts return only as measured experiments with a human gate: the
prompt self-optimiser promotes nothing until its measured delta is positive; the forecast scorecard
withholds scores until enough questions resolve; calibration is honest-null where unmeasured. Retired analysts stay retired and unread. The
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

**Who can see and change what?** Out of the box, one perimeter password at the edge, and everyone who
passes it is the same admin. With sign-in turned on (`LEGBA_AUTH_SIGNIN=1`), people sign in with local
accounts and hold one of three roles: a **reader** reads every reader surface, keeps their own Consult
sessions, watch list and exports, and changes nothing shared; a **contributor** also grades, decides
journal proposals and forecast outcomes, and acknowledges or snoozes alerts; an **admin** does
everything else, including sources, descriptors, users and the engine's own surfaces. The owner is an admin who cannot be demoted. Groups, single
sign-on and per-source permissions are not built. [Operating your instance §9](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in);
every route's minimum role is in [API_ROLES.md](API_ROLES.md).

**Is there a phone app?** The reader pages work on a phone at `/m/`, and with sign-in on that shell
installs from the browser as a web app. There is no app-store app and no web push; alerts go to ntfy.
[UI.md](UI.md#the-phone-shell).

**Can I self-host it, and under what licence?** Yes; that is the design. AGPL-3.0-or-later, whose
network clause covers networked use. A commercial licence is available; an outside code contribution
needs a CLA. You run it yourself and can audit every output down to the source.

**What does a deployment look like?** A profile-gated compose stack brought up by one script in the
order that matters, then changes shipped with the tiered rolling deploy. Fresh volumes only; there is
no migration path from earlier designs. Start with the out-of-box catalog and activate further sources
one at a time. [Setup](SETUP.md), then [rolling deploy](operations/rolling-deploy.md).

**What does it cost to run?** Every scheduled analyst runs on the self-hosted core plane at no
per-run cost. The hosted routes, the judge, the audit grader and rater, the grader's paid families,
and consult, are each one config line with a cost brake, and the plane that pays for each path is in
[TUNABLES.md](TUNABLES.md).

**Is the geopolitics use case hard-coded?** Not by design, and not in the checking layer. Point a source
at a different domain and declare different targets and analysts, and the same cite, verify and audit
pipeline applies. Example-shaped assumptions still sit in some prompts and modules, and the default
bring-up registers the geopolitics profile; a second profile is what moves them into profiles or
descriptors, never a fork. [PROFILES.md](PROFILES.md) says what a profile carries today.
