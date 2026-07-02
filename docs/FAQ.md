<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# FAQ

Plain answers to the questions a newcomer actually asks. Terms in **bold** are
defined in the [Glossary](GLOSSARY.md) — this page stays narrative and links there
rather than re-defining anything. For current numbers and per-feature status, see
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) and [SEAMS.md](SEAMS.md).

## What it is

### In one sentence, what is Legba?
A **source-first**, decompositional analysis system: it turns a firehose of sources
into **cited, verified, drillable reports** over whatever domain you configure. It
ingests data into a provenance-tracked **substrate** (signals → facts/entities →
**nexuses** / **situations**), then reasons over it in small steps — narrow reasoning
**units** → a per-country synthesis → a world synthesis → a banded scorecard — where
every claim is cited to a source, checked by a mandatory faithfulness pass, and
walkable back to the raw signal. You declare the whole pipeline with **descriptors**
instead of writing code.

### What does "source-first" mean?
Acquisition belongs to **sources**, not to the things that consume data. A source
ingests an observation once, enriches it once, and publishes one canonical,
target-agnostic **signal**; the **fan-out** plane then routes that single signal to
every interested **target** — *"ingest once, enrich once, match many."* One BBC feed
serves every country desk (all twenty-four) without re-fetching. (It is unrelated to
the AGPL "source-available" license.)

### Is this an OSINT / "intelligence" tool?
Geopolitical / G20 country assessment is the **shown exemplar use case, not the
system's identity**. The same source → enrich → fan-out → reason pipeline applies to
any domain you can point a source at. It performs *cited situation-assessment over text
and structured data* and measures how well each claim is **grounded in its cited
evidence** — it does **not** claim sensor-fusion or track-correlation rigor, and it does
not claim to establish truth (see *Does it measure truth?* below).

### Does it forecast or predict events?
**Not as a claim — and it currently reports no proven skill.** Forecasting exists only
as a precise-question **`acute_forecasts` scoreboard**: each entry is a pre-registered
question + resolution window + probability that auto-resolves and is scored by **Brier /
Brier skill score**, surfaced *solely* on the calibration scoreboard route — never as a
free-text finding or claim on a trust surface. A degenerate or geography-dominated
probability vector **abstains** (writes zero rows). Today the pilot is a degenerate/thin
sample, so skill is **withheld and reported as unproven**, not asserted. The older
forecast-as-claim predictors (`country_predictor`, `india_energy_predictor`) that used
to write numeric `prediction` rows are **retired and stopped** — their ~539 historical
`prediction` rows remain in the DB, unread — see [SEAMS.md](SEAMS.md).

### How is this different from a SIEM, a scraper, or an LLM agent?
There's real overlap, so here's the honest cut. A **scraper** just fetches; Legba fetches
once and *fuses* into a connected, deduplicated, temporally-versioned knowledge store with
full **provenance**. A **SIEM** also does rule-based correlation and alerting, but over
security/log telemetry for one estate, with detection rules; Legba is a general,
descriptor-driven *assessment* pipeline over arbitrary sources, where the durable product
is a traceable analytic judgment, not a detection. An **LLM agent** is one reasoning loop;
here the reasoning is *decomposed* into narrow **units** whose every claim is cited,
independently **faithfulness-verified**, and lineage-traceable hop-by-hop to the source,
with the **substrate** (not the model's memory) as the source of truth. The discipline —
cite → verify → audit — is the point, not the model.

## How it works

### What does the analysis actually produce?
A stack of cited, verified reports, built bottom-up:

1. **Four bounded reasoning units** — `leadership_transition`, `energy_security`,
   `escalation`, `narrative_coordination` — each an LLM **analyst** (kind
   `inline_target`) scoped to every country desk via a `has_tag("g20") or has_tag("watch")`
   fan-out (the 19 G20 plus a 5-country **watch** tier = 24 desks), each
   answering **one narrow question**. A run assembles a cited 72h signal slice plus a
   grounding preamble of accumulated facts/nexuses/situations from the substrate,
   synthesizes a strict-JSON **finding** whose prose carries `[N]` citation markers
   mapped to signal ids, then runs the **mandatory faithfulness verify**.
2. **Per-country composition** (`country_composition`) reads a country's four *verified*
   units and writes one hedged, cited synthesis; an unverified sub-claim never enters it
   (it joins on the faithfulness critique).
3. **World composition** (`world_assessor`) composes over the country compositions into a
   cited, hedged world view you can drill **country → units → source**. This is *not* the
   old single-shot "world verdict from nowhere" — that framing was retired; `world_assessor`
   graduated into the composition.
4. **A banded scorecard** (`scorecard_producer`, deterministic) writes one banded row per
   active `g20`/`watch`-tagged desk (the 24) from high-precision rules over already-verified
   claims in a **14-day band window** — every band names the verified-claim id it rests on,
   and a dimension with no qualifying verified claim reads *insufficient-evidence* with an
   explicit reason rather than a fabricated band.
5. **A skill scoreboard** reports per-unit eval (faithfulness + correctness-vs-reference),
   the exogenous calibration Brier, and the acute-forecast BSS — each honestly, including
   no-skill / insufficient-sample results.

### What is the faithfulness verify pass?
Every cited finding is scored for **faithfulness** in `[0,1]` by an LLM judge —
currently the same core reasoning model (`llm.primary.openai_compat`, gpt-oss-120B)
that wrote the finding, **not** cross-family — plus a deterministic citation-presence floor:
*does each claim actually follow from the evidence it cites?* `effective_confidence =
min(confidence, faithfulness_score)` is folded at **read time** and gates a visible
low-confidence tier — it **never hard-deletes**. A planted fabrication is flagged
unsupported. This is a mandatory pass, not an optional lint. Running the judge on
the same model that wrote the prose is a deliberate, temporary choice — the earlier
8B cross-family judge (`llm.verify.slm_8b`, "legba-slm", Llama-3.1-8B) proved too
weak (harsh + mis-aimed) — and a **known limitation**: a model verifying its own
family shares its blind spots, so the faithfulness signal is weaker than an
independent cross-family judge; the citation-presence floor and the signed
provenance chain still backstop it, and a dedicated reasoning judge is planned.

### Does it measure truth?
**No — and that distinction is the whole thesis.** The verify pass measures
**groundedness** (faithfulness): whether each claim follows from the evidence it cites,
not whether the world actually is that way. A well-cited claim drawn from a wrong source
can still score faithful. So everything the scorecard bands and the compositions carry is
"supported by its cited evidence", not "true". We say so on purpose.

### What is a descriptor versus an analyst versus a target?
A **descriptor** is the declarative config record you register. A **target** is a declared
**scope-frame** — a named subject/desk (e.g. a country) that a set of analysts work, not a
surveilled entity — that passively subscribes to matching signals. An
**analyst** is the reasoning unit that reads a slice of substrate and writes typed outputs
(**findings**, etc.) about targets. A **source** is the fourth kind — it acquires the data.
You declare descriptors; the runtime stands up **actors** from them.

### Do I have to write code for each source or each country?
No. There is no code to write per feed, per target, or per analysis. You register
descriptors and the **Dapr virtual-actor** runtime instantiates them. The 24 country desks
(the 19 G20 plus a 5-country **watch** tier) are materialized from one **discovery**
template — there is no per-country code, and the four reasoning units fan out to all of them
via one `has_tag("g20") or has_tag("watch")` predicate. Adding a country is just registering
a target, no code.

### What is the substrate?
The shared storage layer every actor reads from and writes to: **Postgres + Apache AGE**
(relational + entity graph), **Qdrant** (vectors), **Redis** (hot cache), and **NATS
JetStream** (event bus + durable streams). It is a **temporal knowledge graph** — facts
and nexuses carry `valid_from`/`valid_until` + decay and grow continuously — holding all
signals, facts, entities, nexuses, situations, and outputs, and it is the hand-off point
between decoupled actors.

### What's a signal, and why does it carry no target?
A **signal** is one canonical, target-agnostic observation produced from a source — content
plus metadata plus provenance, before any interpretation. It deliberately carries no
`target_id`: because it's a neutral observation, the same signal can route to *many*
targets. That target-agnosticism is the keystone of the source-first model.

### What is a nexus / a situation?
A **nexus** is a first-class, reified, typed, polarity-signed (+1 / 0 / −1) relationship
row between two entities — a coined Legba term for a stored relationship object rather than
an implicit graph edge (the sign is a polarity label, not a cryptographic signature). A
**situation** is a durable thematic cluster of related findings/signals — a non-geographic
frame that serves as Legba's deliberate stand-in for an events table.

### What is the journal?
The **journal** is Legba's first-person reflective voice — the one analyst pointed at the
whole organism (its own self, state, and flow). It is deliberately **off the
fact/finding/nexus chain**: a journal entry is a *perspective over* the **provenance**
chain, never a *member of* it. It carries an always-empty `derived_from`, lands in its own
`journal_entries` table (not `analyst_outputs`), and is excluded from the **lineage**
catalog — so a lineage walk from any fact/situation/nexus can never surface a journal entry,
and the journal can never produce a fact/finding/nexus or corrupt the substrate. Everything
*outward* — a correction, a change, even a revision to its own instructions — would be a
**propose-and-gate** suggestion into a human-gated review queue, never a live edit. It
**runs on cadence** as an introspective instrument — `journal_assessor` writes a 12h entry
and `journal_consolidator` a daily roll-up — but only ever into `journal_entries`, so it
cannot pollute product output. Routing its reflections back out through the human-gated
proposal queue is a **future** item, not yet done (see [SEAMS.md](SEAMS.md)).

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
The **proven core** is now the full cited-and-verified spine, demonstrated end-to-end on the
G20 exemplar from a cold start: source acquisition → **baseline enrichment** → predicate
**fan-out** → the four bounded **units** → **per-country composition** → **world composition**,
with each claim cited, faithfulness-verified, and folded through `effective_confidence`, plus
the deterministic **banded scorecard** and full **lineage** / provenance and **temporal facts**.

The ambitious legs return **only as measured, honest experiments**, never as always-on
capabilities:

- **GEPA self-optimizer** — scoped to ONE unit (`leadership_transition`) as a `unit_optimizer`
  descriptor. Every candidate carries a *real before/after paired faithfulness delta* measured
  on the same faithfulness judge (currently the core model, not cross-family; a recent live run: parent `0.34` → candidate `0.29`, delta `−0.05`),
  stays `promotion_gate=human_gated`, and can **never** auto-promote on a degenerate / absent /
  non-positive delta. The old monolithic `country_optimizer` stays **cadence-frozen** (descriptor
  still `state=active`, but its cadence is nulled — no reminder-flood regression).
- **Forecasting** — only the `acute_forecasts` Brier/BSS scoreboard described above; reports
  **no proven skill** today.
- **Correctness-vs-reference** — a gold set that is currently **tiny (n=1)** and reported as
  *insufficient-sample*, not a score.
- **Exogenous calibration Brier** — real rows, honest-null where unmeasured.

**Retired / frozen** (all documented in [SEAMS.md](SEAMS.md)): the monolithic `country_assessor`
one-pager is **retired and stopped** — nothing in the trusted spine reads it (the units +
composition supersede it), though its ~1.2k historical `finding` rows remain in the DB, unread;
the forecast-as-claim predictors are **retired and stopped** (~539 historical `prediction` rows
remain); and the monolithic `country_optimizer` is **cadence-frozen**. (The journal is *not*
frozen — it still runs on cadence as an off-chain introspective instrument, see above.)

### What is the scorecard, and why does the US read "insufficient"?
`scorecard_producer` writes one banded row per active `g20`/`watch`-tagged desk (the 24) from
high-precision rules over already-verified claims in a 14-day band window (severity tag ×
`effective_confidence`, **demote-never-promote**; a
per-claim faithfulness below a floor demotes to "low-faithfulness"). The **live scorecard is a
mix**: some countries band, and some dimensions come back **insufficient-evidence** — the US, for
one, currently reads all-insufficient — because its underlying unit faithfulness is genuinely low.
That is the honest output surfacing weak inputs, not a gap being papered over.

### What's a "seam" and why do features "fail loud"?
A **seam** is a deliberately unbuilt capability that is declared in [SEAMS.md](SEAMS.md) and
*refuses to run* (raises) rather than stubbing or faking output. It's the project's no-stub
honesty rule, enforced by a stub-scanner test. If you hit a seam, the feature genuinely
doesn't work yet — it is not a bug and not a finished feature.

### What does provenance / lineage actually give me?
Every output records `derived_from` (the exact substrate rows it came from), so you can walk
**lineage** back to the raw signals and sources — `GET /api/v1/lineage/finding/{id}` resolves
the chain hop by hop to the real source URL, with **zero dangling links** (a lineage-integrity
sweep prunes dangling `derived_from`). Each analyst run also writes a **SHA-256 hash-chained
receipt** over its `analyst_traces` row; a lineage node carries that `receipt_hash` and a
`chain_consistent` boolean, surfaced as a **"chain-consistent (single-node)"** badge. That is
hash-chain *integrity within one node* — not a distributed/notarized signature, so don't read it
as tamper-proof or "signed". (Ed25519 signing exists separately, on the descriptor audit-log's
audit checkpoints — not on analyst outputs.) Per-analyst run timing (run count, avg/max
wall-clock seconds, last run, non-success) is surfaced read-only at
`GET /api/v1/v3/eval/analyst_runtime`, read straight from `analyst_traces`. The point is that
you can *verify how the output was produced*, not that it accesses more data or reasons more
cleverly than anything else — the
discipline (cite → verify → audit), plus the self-hostable descriptor model, is what sets Legba
apart.

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
outside contribution needs a CLA. You run it yourself and can audit every output down to the
source — that auditability, not privileged data, is the whole proposition.

### What does a deployment look like / how do I start small?
It's a profile-gated docker-compose stack (substrate, dapr, runtime, dapr-workflow, mcp),
brought up with a one-command `deploy/deploy.sh`. It is **clean-slate only** — there is no
migration from pre-pivot Legba. You stand up a fresh empty substrate, register a **stack**, then
register the **cold-start verification set** (3 RSS feeds + the G20 targets + analysts + packs)
via `bringup_register_p17_workingset.py` and verify the whole loop from empty — see
[SETUP.md](SETUP.md) §7. Scaling to the full ~50-source catalog is a separate, deliberate step
(`bringup_register_source_catalog.py`, §8). One gotcha: if the registry is empty at boot,
enrichment's NLP client stays null and silently does nothing — **register first**.

### Is the G20 / geopolitics use case hard-coded?
No. It's the shown **exemplar**, materialized from one **discovery** template and a few
descriptors — not the product's identity and not per-country code. Point a source at a
different domain and declare different targets/analysts, and the same cite → verify → audit
pipeline applies.
