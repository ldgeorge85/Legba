<p align="center">
  <img src="logo_small.png" alt="Legba" width="400">
</p>

<h1 align="center">Legba</h1>
<p align="center"><em>Cited, grounding-verified intelligence you can drill to source — over any feed you can reach, self-hostable.</em></p>
<p align="center">
  <a href="docs/TOUR.md"><b>Tour</b></a> ·
  <a href="docs/FAQ.md"><b>FAQ</b></a> ·
  <a href="docs/SETUP.md">Setup</a> ·
  <a href="docs/STATUS.md">Status</a> ·
  <a href="docs/README.md">All docs</a>
</p>

## What it is

Legba watches a set of feeds and writes intelligence assessments you can check.
Point it at sources (news RSS, GeoJSON hazard feeds, APIs, webhooks), declare
what you care about (we run 32 country desks — 19 G20 plus a 13-country watch
tier; counts are generated — see [docs/RELEASE_STATE.md](docs/RELEASE_STATE.md)),
and it produces short analytic reads — each
claim cited to a source, each citation checked by a second verification pass,
and everything auditable hop-by-hop back to the original item.

```
 feeds ──► signals ──► facts / entities / situations      (the knowledge substrate)
                          │
                          ▼
              8 bounded reasoning units                    (one narrow question each,
   leadership · energy · escalation · narrative ·          per country desk — except
   internal-stability · military-posture · economic-coercion  proliferation, narrow:
   · proliferation                                          8 nuclear-relevant desks)
                          │  cited [N] → grounding-verified
                          ▼
   country ──► region ──► world composition                (synthesis over GROUNDING-
        (+ a cross-desk thematic escalation read)           VERIFIED sub-claims only)
                          │
                          ▼
                   banded scorecard                        (one row per desk; missing
                                                            evidence says so honestly)
```

The pitch is the discipline, not the data: most tools let an LLM *assert*.
Here every assertion is **cited**, a **mandatory second pass checks that each
claim actually follows from what it cites** (an LLM judge plus a deterministic
citation check), and a hash-chained receipt trail connects every output back to
source. The whole chain — source item, citation, verify verdict, composed
conclusion — is preserved and replayable after the fact. You run it yourself
(AGPL, self-hosted, no SaaS dependencies), so you can inspect all of it.

**What "grounding-verified" means — and doesn't.** The verify pass measures
*groundedness*: "does this claim follow from the evidence it cites?" — not "is
this claim true in the world?" Faithfulness is a `[0,1]` score folded into
`effective_confidence = min(confidence, faithfulness)`; a fabricated claim gets
flagged and demoted to a visible low-confidence tier, never silently deleted.
Legba makes **no forecast-accuracy claim** and ships single-operator /
single-tenant. Weak spots are reported, not hidden — see
[docs/STATUS.md](docs/STATUS.md).

The engine is domain-agnostic — sources, desks, and analysts are declarative
descriptors, so swapping the geopolitics exemplar for another domain is
configuration, not code.

## Quick start

```bash
docker compose --profile runtime build      # one-time image build
deploy/deploy.sh                            # phased, idempotent bring-up (project "legba")
deploy/deploy.sh --seed                     # optional: + curated knowledge seeds
```

One script stands up the whole thing in the load-bearing order: schema →
credential vault → the substrate stack → the 100+ source catalog (plus the shared
and state-media feeds) → the 32 country desks → the analyst set → runtime.
Clean-slate only (no migration path from
pre-pivot Legba). Step-by-step manual bring-up, a throwaway validation stack,
and troubleshooting live in [docs/SETUP.md](docs/SETUP.md) and
[docs/RUNBOOK.md](docs/RUNBOOK.md).

```bash
# Is it alive? Signals landing, analysts producing:
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind;"
```

The operator UI is served by Caddy on `:443` (basic-auth perimeter); the
registry API on `:8090` (bearer token). All model inference is hosted
out-of-process — nothing heavy runs in-container.

**Then take the [Tour](docs/TOUR.md)** — your first ten minutes: see a finding,
read its citations, check its verification, and drill it to the source article.

## How it works, in one paragraph

Sources own acquisition: a source polls (or receives a push), emits one
canonical, target-agnostic **signal**, enriched once (language, geo, entities)
and published once. A fan-out plane routes each signal to every subscribed
**desk** by predicate — one BBC feed serves all 32 desks without
re-fetching. Per desk, up to eight bounded **units** each answer one narrow
question — seven run on every desk; the eighth, `proliferation_watch`, only on
the 8 nuclear-relevant desks — over a cited 72-hour slice plus accumulated
context from the temporal knowledge
substrate (facts and relationships with validity windows — so it integrates
over weeks, not just today, and stale model priors get overridden). Unit
findings pass the **verify gate**; only verified sub-claims compose upward into
the per-country read, the regional and world reads, and the scorecard. Every derived row
carries lineage (`derived_from`) plus a SHA-256 receipt chain, walkable via
`GET /api/v1/lineage`. Deep dives: [architecture](docs/ARCHITECTURE.md) ·
[flows](docs/FLOWS.md) · [analysis methodology](docs/ANALYSIS.md).

## What's in the box

- **Descriptor-driven everything** — sources, desks, analysts, and capability
  packs are declarative, registered at runtime (content-hashed, Ed25519-signed
  audit log). Adding a desk or a feed is registration, not a deploy.
- **A dozen-plus source kinds, one signal shape** — `rss`, `geojson`,
  `json_api`, `gdelt_query`, `acled`, `opensanctions`, `scraper`, `firecrawl`,
  `telegram_channel`, `generic_webhook`, more ([catalog](docs/DATA_SOURCES.md)).
- **The grounding-verified analysis spine** — units → compositions → scorecard, with the
  faithfulness gate between every layer ([how to read one](docs/TOUR.md)).
- **Provenance you can walk** — lineage API + receipt chains
  ("chain-consistent (single-node)" — an honest badge, not a tamper-proof claim).
- **Verification-gated alerts, with receipts** — deterministic triggers on
  *verified* state changes (band crossings, new high-severity verified
  findings, contested-claim flips, deviation from a desk's own statistical
  baseline, hits on operator-defined **watchlists**) fan out through a
  ledgered sink plane (webhook / ntfy push, operator-activated); every alert
  states its verification posture and links back to its receipt chain, and
  cooldown-suppressed alerts coalesce rather than vanish.
- **An evidence archive** — signals cited by verified findings have their
  original bytes fetched and stored content-addressed (SHA-256), license-gated,
  so a citation resolves to a preserved copy rather than a rotting URL.
- **A calibration record** — scorecard band changes are logged as resolvable
  claims and graded at 14/28-day horizons, published as persistence and
  reversal rates (deliberately not a Brier score — bands aren't probabilities).
- **Temporal and narrative views** — a validity-window timeline over the
  temporal substrate, and contested-claim families reified as narratives with
  a who-publishes-first source-echo graph (detect-only, descriptive-not-causal).
- **On-demand consult** — ask questions against the live substrate
  (`POST /api/v1/consult`; ReAct over governed read tools).
- **An introspective journal + voice roster** — the system's first-person voice
  about its own state, plus a weekly third-person chronicle and four
  falsifiable-prior "lens" reads with a chorus diff — all kept off the product
  chain so they can never pollute findings.
- **Measured experiments, labeled as such** — a prompt self-optimizer and an
  acute-forecast scoreboard exist behind honesty gates; neither claims skill it
  hasn't measured ([details](docs/STATUS.md)).
- **Operator UI** — a composable panel workstation (feed, inspector, map,
  scorecard, lineage, entity graph) — [guide](docs/UI.md).

## AI models

The analyst plane runs on a self-hosted **gpt-oss-120b** (vLLM, $0/token);
consult uses **Claude Opus 4.8** (billed, sparingly); the faithfulness judge
currently runs on the same core model (a documented, temporary limitation — a
dedicated cross-family judge is planned). Enrichment: bge-m3 embeddings, NLLB
translation, spaCy/GLiREL NER. All hosted out-of-process and resolved through
the stack registry. Details: [docs/AI_MODELS.md](docs/AI_MODELS.md).

## Documentation

| I want to… | Read |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | Implementation design — core abstractions, data flows, decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Conceptual orientation — the four planes and why the system is shaped this way |
| [docs/ACQUISITION.md](docs/ACQUISITION.md) | The acquisition plane — `SourceActor`, baseline enrichment, fan-out / subscription |
| [docs/ANALYSIS.md](docs/ANALYSIS.md) | The analysis plane — units, composition, verify, coalescing triggers, action-pack agency |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | The source catalog — the tiered scope model (3 cold-start / 46 catalog / live), the per-source table, and the source-handler kinds reachable through them |
| [docs/MANUAL_INGEST_FORMAT.md](docs/MANUAL_INGEST_FORMAT.md) | The manual-ingest batch format — manifest + per-kind JSONL schemas for hand-supplied data |
| [docs/AI_MODELS.md](docs/AI_MODELS.md) | The hosted models, providers, and how the runtime reaches them |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operator runbook — bring-up, migrations, registration, troubleshooting |
| [docs/CODE_MAP.md](docs/CODE_MAP.md) | Code map — modules, function flows, dependencies |
| [docs/UI.md](docs/UI.md) | Operator UI guide — panels, auth chain, daily-driver workflow |
| [docs/DIRECTION.md](docs/DIRECTION.md) | Engineering direction — designed-not-built: RBAC/SSO, tenancy, STIX/TAXII, MCP, multimodal, scale-out, budget fallback, deep crawl |

Full index with reading paths: [docs/README.md](docs/README.md).

## Status, honestly

The spine runs end-to-end today — cold-start from empty volumes to a verified
scorecard is proven. It is also plainly imperfect: some desks band from
verified claims while others honestly read `insufficient-evidence`; the
correctness gold set is tiny; the forecast pilot reports **no proven skill**;
the self-optimizer has yet to produce a promotable improvement. Every gap is
declared: [docs/STATUS.md](docs/STATUS.md) is the truth-in-labeling table,
[docs/SEAMS.md](docs/SEAMS.md) the registry of intentionally-not-built things
(they fail loud, never fake output). Release history: [CHANGELOG.md](CHANGELOG.md)
(public history is squashed per release; the changelog is the record). Retired legacy analysts and what replaced
them: [docs/STATUS.md §Retirements](docs/STATUS.md#retirements--freezes).

## Contact & license

Talk shop: legba@civislux.us. Copyright (C) 2026 Lewis George. Licensed
**AGPL-3.0-or-later** — see [LICENSE](LICENSE); note the network clause (§13).
A commercial license is available for uses the copyleft doesn't fit — enquire
via the [repository](https://github.com/ldgeorge85/legba).
