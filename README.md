<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
<p align="center">
  <img src="logo_small.png" alt="Legba" width="400">
</p>

# Legba

**A measured data-analysis platform: you define what to watch and what questions to ask in YAML; it
writes cited reads and grades its own correctness, and publishes the grade with its coverage.**

Legba watches streams of observations, writes short analytic reads about the subjects you tell it to
care about, and checks its own work. Every sentence it writes is cited to a source item; a second pass
verifies that each claim follows from what it cites; the subjects are graded against references built
independently of the platform; and a standing audit checks its claims against the open web. The numbers
are published, including the bad ones. You run it yourself.

The page below runs in four parts, in this order: **the platform** (what Legba is), **the exemplar**
(the one instance running today, geopolitics), **instances** (how a different case is stood up) and
**the business** (what is offered around the AGPL platform).

The part that is unusual is the **checking layer**: the machinery that decides whether what has
already been written holds up, and that answers in typed, drillable terms instead of a blank. A
correctness figure never appears without the coverage it was drawn from. A contrary pass goes looking
for evidence against a claim and records the disagreement rather than a verdict. And where there is
nothing to say, `GET /v3/absence?scope=<desk>` says which kind of nothing it is — from a closed
nine-kind vocabulary, with the proof of what was checked and how long that answer is good for —
because *not checked* and *nothing found* are different answers and a blank cell hides both.

It is **software-defined**: an instance of Legba is a set of descriptors. Sources, subjects (a *desk*
is the registered subject frame), analyst units, capability packs, model routes, cadences, grants and
budgets are YAML documents registered through one API, and the runtime is generic and reads them.
Adding a subject (a country, a regime, a company) is a target descriptor. Adding a feed is a source
descriptor. Changing what the platform watches, thinks about, or is allowed to do is a descriptor
change, not a code change.

It is a **long-horizon agentic environment**: the analysts are scheduled actors with memory. They
keep a journal and a chronicle, read the record through declared interpretive lenses, act through
governed tool packs, and are graded over time on three axes that are never pooled: faithfulness to the
evidence on every layer, correctness against independent references, and the external audit.

## The exemplar: geopolitics

The first instance, and the one running on the reference host, is geopolitics, deliberately the broadest
and hardest case we could pick because it stresses every part of the platform at once. It has been live
since June. **It is not the product, and its numbers are not the product's scale.** Every figure in this
section is *this instance's* (the geopolitics exemplar's): country desks in two tiers, a set of bounded
units asked of each, thematic desks beside them (supply chain, cyber), region frames and a situation
frame. Its counts are generated, not typed, in [docs/RELEASE_STATE.md](docs/RELEASE_STATE.md). Its shape
(country desks, region rollups, a world record, the political-lean lenses, the per-country reference
builder) is one instance's descriptor set, in [`profiles/geopolitics/`](profiles/geopolitics/profile.yaml).

### The tower, as this instance builds it

```
 sources ──► signals ──► facts · entities · relations · situations · events   (the substrate)
                            │
                            ▼
              bounded units, one narrow question each                         (per desk: country
              leadership · energy · escalation · narrative · stability ·      desks + thematic desks;
              posture · coercion · proliferation · disruption                 cited [N], judge-verified)
                            │
                            ▼
              country composition ──► region rollup ──► world record          (verified spans quoted
              + the country voice        (deterministic)   + the world voice   byte-for-byte; the voice
                                                                              is its own graded row)
                            │
                            ▼
              scorecard · alerts · journal · lenses · consult                 (what a reader sees)

  beside every floor: the faithfulness judge · the correctness grader · the external audit ·
                      the contrary pass · typed absence (GET /v3/absence?scope=<desk>)
```

Each floor rests on the floor below and cites it. A reader can drill any sentence down to the source
item: the citation, the verify verdict, the receipt, the archived bytes. Where the evidence supports
nothing, the read says so instead of filling the gap. The floors up to the unit findings are the
platform's; the country composition, the region rollup and the world record are this instance's top
floor, and a different instance carries its own or none.

## Instances

An instance is the platform plus one **profile**: the descriptors, seeds, source catalog, rosters and top
floor that make a case. [docs/PROFILES.md](docs/PROFILES.md) explains the manifest. Two shapes:

- **The expanded shared instance** (one substrate visibly serving several unlike cases, say geopolitics
  beside supply chain beside cyber, each read through its own taxonomy). Designed, not built: today the
  thematic supply-chain desks and a cyber desk are the other families on the shared substrate, and they
  prove the primitive (a desk is a registered subject frame, no new code) while still sitting inside the
  geopolitics instance.
- **Focused private instances** (one use case on its own server, with its own sources, subjects, unit
  questions and top floor). `profiles/geopolitics/` is the exemplar's manifest; the first focused profile,
  [`profiles/sanctions_export_control/`](profiles/sanctions_export_control/profile.yaml), is a skeleton
  that validates and plans and is not registered anywhere yet. A second bring-up on a different profile
  is the proof that the platform separates from the exemplar; until it lands, that separation is a claim
  with a skeleton under it, not a demonstration.

The engine is domain-agnostic in concept; some of it is not yet domain-neutral in implementation, since
exemplar-shaped assumptions remain in some prompts and modules. [docs/PROFILES.md](docs/PROFILES.md)
says what a profile carries today.

## The business

Two shapes of offer sit around the platform, and this repository carries no price list:

- **As-a-service on the expanded shared instance:** subscriptions over a roster of subjects on the shared
  substrate. It needs the expanded instance and multi-tenancy, neither of which exists yet.
- **Private instances:** custom sources, custom integration, a managed private deployment, curated source
  and reference packs, for one use case.

The platform stays AGPL-3.0-or-later; the value is in the instance and the operation of it. A commercial
licence is available for uses the copyleft does not fit (see [Contact and license](#contact-and-license)).

## Quick start

```bash
docker compose --profile runtime build      # build the app images once
deploy/deploy.sh                            # phased, idempotent bring-up
deploy/deploy.sh --seed                     # optional: add the curated knowledge seeds
```

One script stands the platform up in the order that matters: schema, credential vault, substrate,
the source catalog, the profile's subjects (the geopolitics desks, by default), the analysts, then the
runtime. Fresh volumes only; there is no migration path from earlier designs.

```bash
# Is it alive? Signals landing, analysts producing:
docker compose exec -T postgres psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind;"
```

The console is served by Caddy on `:443`, with a phone shell under `/m/`; the registry API is on `:8090`.
People sign in with local accounts when sign-in is switched on (`LEGBA_AUTH_SIGNIN`): a reader reads, a
contributor also acts on alerts and forecast outcomes, an admin changes the instance, and one owner
cannot be removed. With sign-in off, the console sits behind the edge's basic-auth perimeter and the API
takes a bearer token. [Setup](docs/SETUP.md) and
[Operating your instance](docs/OPERATING_YOUR_INSTANCE.md) cover both. Model inference is hosted out of
process; nothing heavy runs in a container. What `deploy.sh` registers out of the box is a fixed catalog
of sources that need no credentials; further verified sources ship in the tree as drafts and are
activated one feed at a time by an admin, so a running deployment carries more than the catalog. The live counts are generated, not typed:
[docs/RELEASE_STATE.md](docs/RELEASE_STATE.md).

Then take the [Tour](docs/TOUR.md): see a finding, read its citations, check its verification, and
drill it to the source article.

## How it works, in one paragraph

A source polls or receives a push and emits one canonical signal, enriched once with language, geo and
entities, and published once. A fan-out plane routes each signal to every subject whose predicate matches
it, so one feed serves every subject without refetching. Per subject (a desk), each bounded unit
answers its one question over a cited slice plus the temporal substrate, which carries facts and
relations with validity windows, so a unit integrates over weeks rather than a day. Unit findings pass
the faithfulness judge; only verified sub-claims above the composition floor are quoted upward into the
composition (in the exemplar: the country record, the regional rollup and the world record), and only
those records are read by the voices. Every derived row carries lineage and a hash-chained receipt,
walkable through the lineage API. Deep dives:
[architecture](docs/ARCHITECTURE.md), [flows](docs/FLOWS.md), [analysis](docs/ANALYSIS.md).

## What is in the box

- **Everything is a descriptor.** Sources, subjects, analysts, packs and model routes are registered at
  runtime through a content-hashed, signed, audited registry. Adding a subject or a feed is registration.
- **One signal shape from many source kinds:** RSS, GeoJSON, JSON APIs, GDELT, Telegram channels,
  webhooks, scrapers and more. [Catalog](docs/DATA_SOURCES.md).
- **The verified spine:** units, compositions, voices and scorecard, with the faithfulness gate between
  every layer, and provenance you can walk hop by hop.
- **Three graders that never pool.** The faithfulness judge on every written layer. The correctness
  grader, which builds its own references and scores the subjects with three model families. The external
  audit, which samples top-layer claims and checks them against live web search, including claims that
  something did not happen. [How it is measured](docs/ANALYSIS.md#measurement).
- **Verification-gated alerts** on verified state changes and watchlists, through a ledgered
  sink plane with a daily page budget; what is suppressed is recorded, not lost.
- **An evidence archive.** Cited pages are fetched and stored content-addressed and licence-gated, so a
  citation resolves to a preserved copy rather than a rotting link — with a per-host fetch policy that
  aims the browser-fingerprint leg at challenge-blocked publishers only, and per-feed body-extraction
  counters on the source-health read.
- **A temporal substrate**, with validity windows on facts, relations, situations and events, a
  situation ledger that records how each open situation evolved, and as-of reads — `as_of` on the
  substrate readers and `GET /api/v1/v3/belief` answer "what did Legba believe on date D". Every
  substrate row also carries an `origin_class` — live, seed, web-retrieval or a refused-until-swept
  history class — so imported history can never silently read as "now".
- **An event plane, dark by default.** Deterministic event clustering and reconciliation upsert
  evidence-linked event rows from signal clusters and tower findings, write every lifecycle
  transition to an append-only ledger, and reconcile occurrences with correlated-with and
  evolves-from edges. Every event write is gated by `LEGBA_EVENTS`. The read side is `GET
  /api/v1/v3/events` (+ `/{id}` dossier and `/{id}/lifecycle` ledger) and the
  `query_events`/`inspect_event` tools in `action_pack_substrate_read`; the world map draws
  geocoded events as lifecycle-colored rings and both timelines put them on their own lane.
- **Event citations.** A finding may cite `event:<uuid>`; the citation builder expands it to the
  event's member signals at build time, so the judge grounds on raw signal text — the event's own
  summary is never evidence. Off by default (`LEGBA_EVENT_CITATIONS`).
- **A cross-layer graph projection, dark by default.** `graph_arcs` is a disposable,
  plane-tagged (world / evidence / lineage) projection of every cross-layer arc, rebuilt whole
  by the `graph_projector` sweep and read at `GET /api/v1/v3/graph/arcs` with an `as_of`
  temporal filter — never an incremental mirror. A stale, empty or disabled projection refuses
  by name (`projection_stale` / `projection_empty` / `projection_disabled`), never a quiet
  empty graph. Gated by `LEGBA_GRAPH_PROJECTION`; the descriptor ships `draft`.
- **The journal and the lenses.** A first-person journal with consolidation, a third-person chronicle,
  and a faculty of interpretive lenses, each with one declared falsifiable prior, kept off the product
  chain so they can never pollute a finding.
- **On-demand consult** against the live substrate through governed read tools. It runs on the
  self-hosted plane by default and only when a person asks.
- **Citations as rows, and a named writer on every row.** Each sentence of a finding and each thing it
  cites is a row, so "what supports this sentence" is a query, and every row records which person, service
  or rule wrote it.
- **A forecast ledger.** Dated questions are sealed with their numbers before the outcome is known and
  resolved against a stated rule; no skill figure is shown until enough have resolved.
- **Accounts and roles.** Local sign-in with reader, contributor and admin roles and one owner.
- **Fetching on request, off by default.** One queue takes requests to fetch a page from schedules,
  pipelines and analysts, with depth limits and budgets; a subscribe connector for streams ships beside it,
  also off by default.
- **A console and a phone shell:** a board, subject pages, the ledger and a watch list, with map, lineage
  and record views, and a five-tab phone shell under `/m/`. [Guide](docs/UI.md).
- **Honesty gates.** Anything not built is a declared seam that fails loud. Experiments that have not
  measured skill say so. [Status](docs/STATUS.md), [Seams](docs/SEAMS.md).

## AI models, and who pays

Every scheduled analyst runs on a self-hosted OpenAI-compatible plane at no per-run cost: the units,
the compositions, the voices, the lenses, enrichment and the reference builder. Three hosted routes
are in the loop and are single config lines: the faithfulness judge runs cross-family on a hosted
endpoint, the external audit's grader and second rater run on hosted models, and consult can be pointed at a hosted model but runs on the self-hosted plane by default. The generated release state reports the effective routes for a
deployment, so the claim stays checkable. Every cap, budget and route, with the plane that pays for it,
is in one table: [docs/TUNABLES.md](docs/TUNABLES.md). Details: [docs/AI_MODELS.md](docs/AI_MODELS.md).

## Documentation

| Tier | Read |
|---|---|
| Front door | [Direction](docs/DIRECTION.md) · [Tour](docs/TOUR.md) · [Setup](docs/SETUP.md) · [Operating your instance](docs/OPERATING_YOUR_INSTANCE.md) · [FAQ](docs/FAQ.md) |
| Design | [Architecture](docs/ARCHITECTURE.md) · [Design rules](docs/DESIGN.md) · [Analysis](docs/ANALYSIS.md) · [Flows](docs/FLOWS.md) · [Data model](docs/DATA_MODEL.md) · [Acquisition](docs/ACQUISITION.md) · [Sources](docs/DATA_SOURCES.md) · [Models](docs/AI_MODELS.md) · [Profiles](docs/PROFILES.md) · [UI](docs/UI.md) |
| Reference | [Runbook](docs/RUNBOOK.md) · [Tunables](docs/TUNABLES.md) · [Status](docs/STATUS.md) · [Release state](docs/RELEASE_STATE.md) · [Code map](docs/CODE_MAP.md) · [Glossary](docs/GLOSSARY.md) · [Seams](docs/SEAMS.md) |
| History | [Changelog](CHANGELOG.md) · [docs/history](docs/history/README.md) |

The full index: [docs/README.md](docs/README.md).

## Status, honestly

The spine runs end to end, from empty volumes to a verified scorecard. It is single-tenant and single-node, with accounts and
roles for the people who use it. In the exemplar, some desks band from verified claims while others
honestly read "insufficient evidence"; the correctness gold set is small; the forecast ledger holds sealed questions and reports no skill figure
until enough have resolved; a held record of statements a desk stands behind exists for one desk and is not yet shown to
readers; composed prose is a quotation assembly because free-text synthesis did not survive grading. Every gap
is declared in [docs/STATUS.md](docs/STATUS.md), and everything deliberately not built is in
[docs/SEAMS.md](docs/SEAMS.md). The record of what changed and when is [CHANGELOG.md](CHANGELOG.md);
public history is squashed per release.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md): the four gates the tree enforces mechanically (no undeclared
stubs, tests on the real binding path, module-size ceilings, the lint ratchet), what CI does and does
not cover, and why an outside code contribution needs a CLA. Bug reports and "your docs say X, your
code does Y" findings need no CLA and are the most useful thing you can send.

## Contact and license

Talk shop: legba@civislux.us. Copyright (C) 2026 Lewis George. Licensed AGPL-3.0-or-later; see
[LICENSE](LICENSE) and note the network clause. A commercial license is available for uses the
copyleft does not fit; enquire via the [repository](https://github.com/ldgeorge85/legba).
