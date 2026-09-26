<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Legba

**An intelligence service you define in YAML, and that measures its own correctness.**

Legba watches sources, writes short analytic reads about the things you tell it to care about, and
checks its own work. Every sentence it writes is cited to a source item; a second pass verifies that
each claim follows from what it cites; the desks are graded against references built independently of
the platform; and a standing audit checks its claims against the open web. The numbers are published,
including the bad ones. You run it yourself.

The part that is unusual is the **checking layer**: the machinery that decides whether what has
already been written holds up, and that answers in typed, drillable terms instead of a blank. A
correctness figure never appears without the coverage it was drawn from. A contrary pass goes looking
for evidence against a claim and records the disagreement rather than a verdict. And where there is
nothing to say, `GET /v3/absence?scope=<desk>` says which kind of nothing it is — from a closed
eight-kind vocabulary, with the proof of what was checked and how long that answer is good for —
because *not checked* and *nothing found* are different answers and a blank cell hides both.

It is **software-defined**: an instance of Legba is a set of descriptors. Sources, desks, analyst
units, capability packs, model routes, cadences, grants and budgets are YAML documents registered
through one API, and the runtime is generic and reads them. Adding a country is a target descriptor.
Adding a feed is a source descriptor. Changing what the platform watches, thinks about, or is allowed
to do is a descriptor change, not a code change.

It is a **long-horizon agentic environment**: the analysts are scheduled actors with memory. They
keep a journal and a chronicle, read the record through declared interpretive lenses, act through
governed tool packs, and are graded over time on three axes that are never pooled: faithfulness to the
evidence on every layer, correctness against independent references, and the external audit.

## The tower

```
 sources ──► signals ──► facts · entities · relations · situations · events   (the substrate)
                            │
                            ▼
              nine bounded units, one narrow question each                    (per desk: 32 country
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
nothing, the read says so instead of filling the gap.

The engine is domain-agnostic. The geopolitics desks are the shipped exemplar; a desk is a registered
subject frame, and a second family of thematic supply-chain desks runs on the same primitive with no
new code.

## Quick start

```bash
docker compose --profile runtime build      # build the app images once
deploy/deploy.sh                            # phased, idempotent bring-up
deploy/deploy.sh --seed                     # optional: add the curated knowledge seeds
```

One script stands the platform up in the order that matters: schema, credential vault, substrate,
the source catalog, the desks, the analysts, then the runtime. Fresh volumes only; there is no
migration path from earlier designs.

```bash
# Is it alive? Signals landing, analysts producing:
docker compose exec -T postgres psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind;"
```

The operator console is served by Caddy on `:443` behind a basic-auth perimeter; the registry API on
`:8090` with a bearer token. Model inference is hosted out of process; nothing heavy runs in a
container. What `deploy.sh` registers out of the box is a fixed, unauthenticated catalog; further
verified sources ship in the tree as drafts and are activated one feed at a time by the operator, so a
running deployment carries more than the catalog. The live counts are generated, not typed:
[docs/RELEASE_STATE.md](docs/RELEASE_STATE.md).

Then take the [Tour](docs/TOUR.md): see a finding, read its citations, check its verification, and
drill it to the source article.

## How it works, in one paragraph

A source polls or receives a push and emits one canonical signal, enriched once with language, geo and
entities, and published once. A fan-out plane routes each signal to every desk whose predicate matches
it, so one feed serves every desk without refetching. Per desk, each bounded unit answers its one
question over a cited slice plus the temporal substrate, which carries facts and relations with
validity windows, so a unit integrates over weeks rather than a day. Unit findings pass the faithfulness
judge; only verified sub-claims above the composition floor are quoted upward into the country record,
the regional rollup and the world record, and only those records are read by the voices. Every derived
row carries lineage and a hash-chained receipt, walkable through the lineage API. Deep dives:
[architecture](docs/ARCHITECTURE.md), [flows](docs/FLOWS.md), [analysis](docs/ANALYSIS.md).

## What is in the box

- **Everything is a descriptor.** Sources, desks, analysts, packs and model routes are registered at
  runtime through a content-hashed, signed, audited registry. Adding a desk or a feed is registration.
- **One signal shape from many source kinds:** RSS, GeoJSON, JSON APIs, GDELT, Telegram channels,
  webhooks, scrapers and more. [Catalog](docs/DATA_SOURCES.md).
- **The verified spine:** units, compositions, voices and scorecard, with the faithfulness gate between
  every layer, and provenance you can walk hop by hop.
- **Three graders that never pool.** The faithfulness judge on every written layer. The correctness
  grader, which builds its own references and scores the desks with three model families. The external
  audit, which samples top-layer claims and checks them against live web search, including claims that
  something did not happen. [How it is measured](docs/ANALYSIS.md#measurement).
- **Verification-gated alerts** on verified state changes and operator watchlists, through a ledgered
  sink plane with a daily page budget; what is suppressed is recorded, not lost.
- **An evidence archive.** Cited pages are fetched and stored content-addressed and licence-gated, so a
  citation resolves to a preserved copy rather than a rotting link.
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
- **On-demand consult** against the live substrate through governed read tools, the one billed path,
  run only when the operator presses it.
- **An operator workstation:** composable panels opening on a Morning Read, with feed, inspector, map,
  scorecard, lineage and entity-graph views. [Guide](docs/UI.md).
- **Honesty gates.** Anything not built is a declared seam that fails loud. Experiments that have not
  measured skill say so. [Status](docs/STATUS.md), [Seams](docs/SEAMS.md).

## AI models, and who pays

Every scheduled analyst runs on a self-hosted OpenAI-compatible plane at no per-run cost: the units,
the compositions, the voices, the lenses, enrichment and the reference builder. Three hosted routes
are in the loop and are single config lines: the faithfulness judge runs cross-family on a hosted
endpoint, the external audit's grader and second rater run on hosted models, and consult runs on
Anthropic's Claude, only when pressed. The generated release state reports the effective routes for a
deployment, so the claim stays checkable. Every cap, budget and route, with the plane that pays for it,
is in one table: [docs/TUNABLES.md](docs/TUNABLES.md). Details: [docs/AI_MODELS.md](docs/AI_MODELS.md).

## Documentation

| Tier | Read |
|---|---|
| Front door | [Direction](docs/DIRECTION.md) · [Tour](docs/TOUR.md) · [Setup](docs/SETUP.md) · [Operating your instance](docs/OPERATING_YOUR_INSTANCE.md) · [FAQ](docs/FAQ.md) |
| Design | [Architecture](docs/ARCHITECTURE.md) · [Design rules](docs/DESIGN.md) · [Analysis](docs/ANALYSIS.md) · [Flows](docs/FLOWS.md) · [Data model](docs/DATA_MODEL.md) · [Data model v3](docs/DATA_MODEL_V3.md) · [Acquisition](docs/ACQUISITION.md) · [Sources](docs/DATA_SOURCES.md) · [Models](docs/AI_MODELS.md) · [UI](docs/UI.md) |
| Reference | [Runbook](docs/RUNBOOK.md) · [Tunables](docs/TUNABLES.md) · [Status](docs/STATUS.md) · [Release state](docs/RELEASE_STATE.md) · [Code map](docs/CODE_MAP.md) · [Glossary](docs/GLOSSARY.md) · [Seams](docs/SEAMS.md) |
| History | [Changelog](CHANGELOG.md) · [docs/history](docs/history/README.md) |

The full index: [docs/README.md](docs/README.md).

## Status, honestly

The spine runs end to end, from empty volumes to a verified scorecard. It is single-operator,
single-tenant and single-node. Some desks band from verified claims while others honestly read
"insufficient evidence"; the correctness gold set is small; the forecast pilot reports no proven skill;
composed prose is a quotation assembly because free-text synthesis did not survive grading. Every gap
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
