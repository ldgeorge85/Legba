<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# FAQ

Plain answers to the questions a newcomer actually asks. Terms in **bold** are
defined in the [Glossary](GLOSSARY.md) — this page stays narrative and links there
rather than re-defining anything. For current numbers and per-feature status, see
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) and [SEAMS.md](SEAMS.md).

## What it is

### In one sentence, what is Legba?
A **source-first** platform for automated analysis and **knowledge fusion** over
whatever sources you can reach: it ingests data, fuses it into a provenance-tracked
**substrate** (signals → entities/facts → relations/**nexuses** → **situations** →
per-target assessments), and lets you declare the whole pipeline with **descriptors**
instead of code.

### What does "source-first" mean?
Acquisition belongs to **sources**, not to the things that consume data. A source
ingests an observation once, enriches it once, and publishes one canonical,
target-agnostic **signal**; the **fan-out** plane then routes that single signal to
every interested **target** — *"ingest once, enrich once, match many."* One BBC feed
serves nineteen country targets without re-fetching. (It is unrelated to the AGPL
"source-available" license.)

### Is this an OSINT / "intelligence" tool?
Geopolitical / G20 country assessment is the **proven exemplar use case, not the
system's identity**. The same source → enrich → fan-out → assess pipeline applies to
any domain you can point a source at. It performs *situation-assessment over text and
structured data*; it does **not** claim sensor-fusion or track-correlation rigor.

### Does it forecast or predict events?
**No — no forecast-skill claim is made.** It has built `predictor`, calibration, and
ACH surfaces that write real, traceable rows, but none has a validated skill metric.
There is one *falsifiable* pilot (a pre-registered weekly P(≥1 severe hazard) call per
G20 country, scored exogenously by **Brier skill score** against climatology) — and it
currently reports "degenerate / accumulating" and earns no skill claim. The machinery is
real and deployed; the *claim* is deliberately withheld until it beats the baseline on a
non-degenerate sample.

### How is this different from a SIEM, a scraper, or an LLM agent?
There's real overlap, so here's the honest cut. A **scraper** just fetches; Legba fetches
once and *fuses* into a connected, deduplicated, temporally-versioned knowledge store with
full **provenance**. A **SIEM** also does rule-based correlation and alerting, but over
security/log telemetry for one estate, with detection rules; Legba is a general,
descriptor-driven *assessment* pipeline over arbitrary sources, where the durable product
is a traceable analytic judgment, not a detection. An **LLM agent** is one reasoning loop;
here LLM **analysts** are one governed actor type among many, every output is
lineage-traceable and hash-chain-receipted, and the **substrate** (not the model's memory)
is the source of truth.

## How it works

### What is a descriptor versus an analyst versus a target?
A **descriptor** is the declarative config record you register. A **target** is a declared
subject of analysis (e.g. a country) that passively subscribes to matching signals. An
**analyst** is the reasoning unit that reads a slice of substrate and writes typed outputs
(**findings**, etc.) about targets. A **source** is the fourth kind — it acquires the data.
You declare descriptors; the runtime stands up **actors** from them.

### Do I have to write code for each source or each country?
No. There is no code to write per feed, per target, or per analysis. You register
descriptors and the **Dapr virtual-actor** runtime instantiates them. The 19 G20 country
targets are materialized from one **discovery** template — there is no per-country code.

### What is the substrate?
The shared storage layer every actor reads from and writes to: **Postgres + Apache AGE**
(relational + entity graph), **Qdrant** (vectors), **Redis** (hot cache), and **NATS
JetStream** (event bus + durable streams). It holds all signals, facts, entities, nexuses,
situations, and outputs, and is the hand-off point between decoupled actors.

### What's a signal, and why does it carry no target?
A **signal** is one canonical, target-agnostic observation produced from a source — content
plus metadata plus provenance, before any interpretation. It deliberately carries no
`target_id`: because it's a neutral observation, the same signal can route to *many*
targets. That target-agnosticism is the keystone of the source-first model.

### What is a nexus / a situation?
A **nexus** is a first-class, reified, typed, *signed* (+1 / 0 / −1) relationship row
between two entities — a coined Legba term for a stored relationship object rather than an
implicit graph edge. A **situation** is a durable thematic cluster of related
findings/signals — a non-geographic frame that serves as Legba's deliberate stand-in for an
events table.

### What is the journal?
The **journal** is Legba's first-person reflective voice — the one analyst pointed at the
whole organism (its own self, state, and flow). Every other meta-analyst cuts one slice; the
journal cuts *across* the entire flow and narrates a coherent point of view over the rest of
the system. *"Poetry without evidence is noise. Evidence without perspective is just a log
file."* It is deliberately **off the fact/finding/nexus chain**: a journal entry is a
*perspective over* the **provenance** chain, never a *member of* it. It carries an
always-empty `derived_from`, lands in its own `journal_entries` table (not `analyst_outputs`),
and is excluded from the **lineage** catalog — so a lineage walk from any fact/situation/nexus
can never surface a journal entry, and the journal can never produce a fact/finding/nexus or
corrupt the substrate. The one effect it has on its own is its **continuity** (it reads its
own last entry and current consolidation into its next run). Everything *outward* — a
correction, a change, even a revision to its own instructions — is a **propose-and-gate**
suggestion into a human-gated review queue, never a live edit: it can write its own next
breath, but it cannot rewrite its own rules without the operator.

### What is Dapr and what's an "actor"?
**Dapr** is a distributed-application runtime; a *virtual actor* is an addressable,
single-threaded, on-demand stateful object. Legba turns each active descriptor into one such
actor — a `SourceActor`, `TargetActor`, or `AnalystActor` — distributed across runtime
replicas.

### How does one signal reach the right analysts (fan-out)?
By **predicate**. The **fan-out** plane delivers each published signal to every target whose
predicate matches: coarse **NATS-subject** filtering, narrowed by a SQL `WHERE` clause, then
a fine-grained **Starlark residual predicate** (capped at ~5ms, fail-closed). An analyst then
**coalesces** the signals routed to its target and fires once a threshold/severity gate trips
or its **cadence** heartbeat fires.

## Scope & honesty

### What is proven versus experimental?
The **proven core** is the source-first pipeline: source acquisition → **baseline
enrichment** → predicate **fan-out** → per-target coalescing into **findings**, with full
provenance and **temporal facts**, demonstrated end-to-end on the G20 exemplar from a
cold start. The **experimental** layer (built but UNPROVEN, no validated skill) is
calibration/outcome-resolution, the GEPA **optimizer**, ACH **competing hypotheses**, and
the advanced **graph analytics** — they write real, traceable rows but are labeled research
surfaces, not claimed capabilities. Some knowledge-layer write-paths in between (e.g.
**situations** and parts of the **nexus** leg) have maturity gaps flagged in the project's
own data-quality audits — see [SEAMS.md](SEAMS.md) and the audit notes.

### What's a "seam" and why do features "fail loud"?
A **seam** is a deliberately unbuilt capability that is declared in [SEAMS.md](SEAMS.md) and
*refuses to run* (raises) rather than stubbing or faking output. It's the project's no-stub
honesty rule, enforced by a stub-scanner test. If you hit a seam, the feature genuinely
doesn't work yet — it is not a bug and not a finished feature.

### What does provenance / lineage actually give me?
Every output records `derived_from` (the exact substrate rows it came from), so you can walk
**lineage** back to the raw signals and sources via a recursive query. Each analyst run also
writes a tamper-evident **receipt chain** (SHA-256-linked, with periodic Ed25519-signed
checkpoints). Provenance, auditability, and the self-hostable descriptor model are the stated
moat — not data access or analytic maturity.

### Can an analyst take actions (fetch the web, write facts)?
Only through governed **agency**: an analyst gets tools via an allow-listed **action-pack**,
enforced by a **governor** against budgets and the **grant ∩ allow ∩ applicability** gate,
all fail-closed. Web fetches are **SSRF**-guarded and flagged UNVERIFIED; agent-proposed
writes are PROPOSE-grade (capped confidence, `source_type='proposed'`) and cannot mutate
authoritative data or the control plane.

## Running it

### Can I self-host it? What license?
Yes — it's designed to be self-hosted, under the **GNU AGPL-3.0-or-later** (a strong copyleft
whose §13 network clause covers networked use). Commercial/dual-licensing is intended; an
outside contribution needs a CLA. Self-hostability under AGPL is part of the moat.

### What does a deployment look like / how do I start small?
It's a profile-gated docker-compose stack (substrate, dapr, runtime, dapr-workflow, mcp). It
is **clean-slate only** — there is no migration from pre-pivot Legba. You stand up a fresh
empty substrate, register a **stack**, then register the **cold-start verification set** (3
RSS feeds + the G20 targets + analysts + packs) via `bringup_register_p17_workingset.py` and
verify the whole loop from empty — see [SETUP.md](SETUP.md) §7. Scaling to the full ~46-source
catalog is a separate, deliberate step (`bringup_register_source_catalog.py`, §8). One gotcha:
if the registry is empty at boot, enrichment's NLP client stays null and silently does nothing
— **register first**.

### Is the G20 / geopolitics use case hard-coded?
No. It's the proven **exemplar**, materialized from one **discovery** template and a few
descriptors — not the product's identity and not per-country code. Point a source at a
different domain and declare different targets/analysts, and the same pipeline applies.
