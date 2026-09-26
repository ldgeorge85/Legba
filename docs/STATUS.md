<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Status — the truth-in-labeling page

What is real, what is gated, what is only designed. It describes the deployed
head `a39286bc`, and the measured numbers on this page are the ones the live
routes serve.

Live counts — active sources, desks, units, composition layers, the effective
judge route — are generated into [RELEASE_STATE.md](RELEASE_STATE.md) by a
script that reads the registry database, so they are not repeated here.
Knob values are in [TUNABLES.md](TUNABLES.md). Per-route and per-panel
maturity is in [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md). The
registry of deliberately not-built things, with its guard rails, is
[SEAMS.md](SEAMS.md).

## How to read a state

| State | Means |
|---|---|
| **live** | Runs end to end today and its consequence is engaged. |
| **live, gated off** | The compute runs and its output is visible, but the consequence sits behind a flag that defaults off in code. The off path is the legacy path, byte for byte. |
| **built, not deployed** | Real code with tests, a descriptor that is `draft`, and no live head. An operator activates it through the audited lifecycle route. |
| **guarded seam** | The surface exists and refuses activation or raises loudly until its real edge is wired. It never fabricates output. |
| **designed, not built** | The design is written; no code claims it. |

Tree state and live state differ by design. A descriptor ships `draft` and is
activated live by an operator act, so `draft` in the tree beside `active` in
the registry is the intended lifecycle, not drift — the descriptor state-drift
gauge reports those rows as expected promotions.

## Where it is weak

Single-operator, single-tenant, single-node, run-it-yourself.

- **Composed reads are quotation assemblies, because free-text synthesis did
  not hold up.** Two independent external gradings put composed prose around
  half the pre-committed accuracy bar, so the layer was demoted rather than
  defended: a composition now carries byte-identical desk spans under a
  deterministic title, with the interpretive voice fenced into a separately
  graded Assessment generated from the assembly's own spine. The rollback is
  one flag back to the prose path.
- **A single-frame desk can be blind to its second story.** Frame monopoly
  steers attention, and it has been measured doing so. A coverage census and a
  floor detector exist; until the loop closes, desk silence on a topic is weak
  evidence of absence.
- **Faithfulness cannot see a faithful-but-wrong claim.** The verify pass
  measures groundedness in the cited evidence, not truth. The commonest
  failure it structurally misses is an absence stated as a world fact by a desk
  whose sources simply carried nothing; a deterministic backstop and a prompt
  rule push those toward collection-scoped phrasing, but scoping a claim is not
  checking it against the world. The external audit and the correctness grader
  exist to do the latter, and they are young.
- **Correctness is measured thinly, and the number says so.** The references
  are built on the free plane and bear on only a small share of what a desk
  says, so a correctness figure is always published beside its coverage and is
  never readable alone: most desk-nights decide no claim at all and are
  reported unmeasured rather than scored. The composition correctness gate is
  off for exactly this reason — gating on a share drawn from a handful of
  decided claims would dress a thin measurement as a strong one.
- **The coherence loop flags; it does not correct.** New evidence bearing on a
  standing open question lands a review flag and an append-only edge, and stops
  there. Nothing propagates a correction back into the product that rested on
  the stale reading, and nothing closes a question. The debt count is readable
  and says `match_verified: false` on the wire (SEAMS #49).
- **No skill claim is made anywhere.** The acute-forecast pilot reports no
  proven skill and abstains on degenerate windows. The calibration Brier is
  self-consistency-tiered absent an exogenous outcome. The correctness gold set
  is a small sample. The optimizer measures a real faithfulness delta that is
  not yet positive, so it promotes nothing. Each of those is published rather
  than hidden.
- **The judge can be down.** The effective judge route is repointable by env
  and on the reference deployment it points at a hosted cross-family model, so
  a billing wall or an outage stamps critiques `judge_status='deterministic'` —
  the citation floor, published provisional under a ceiling, never a fabricated
  pass. A gauge replays that shape as paging. Pointing the judge back at the
  local plane is one config line, at the cost of same-model grading.
- **Vector RAG grounding is a guarded pilot on one unit.** Firing it has
  thickened the low-faithfulness tail before, so a per-run guard reverts
  injection the moment that recurs.
- **Labeler independence is discipline, not an invariant.** Gold labels come
  from outside the production plane and the labeler identity is stamped on
  every label — recorded, never validated.

No enterprise, multi-tenant, RBAC, or forecast-accuracy claim is made.

## The analysis spine

| Capability | State | Note |
|---|---|---|
| Source-first acquisition + predicate fan-out | live | Poll and push. Intra-source exact-duplicate collapse at ingest is on by default; publisher-origin geo contamination is fixed, so a place-less signal goes untagged rather than mistagged. State and social outlets are seeded below the ingestion nominal credibility so they cannot out-credit their peers. |
| Baseline enrichment (language, geo, NER, relation extraction) | live | Out of process on the model service. Non-Latin bodies are translated before extraction, and the historical backlog was re-enriched in place. |
| Bounded reasoning units → cited findings | live | Eight over the country desks, one over the thematic desks. Reactive and cadence firing; `[N]`-cited strict-JSON findings; every run ends in a mandatory verify. |
| Supply-chain thematic pack | live (Tier A desks active) | The second exemplar that a desk is a registered subject frame rather than a country: no new analyst kind and no new code path, only descriptors and a widened roster. Tier B desks stay draft, gated on measured collection. Nothing composes these desks upward yet — a lane read is a leaf finding. |
| Mandatory faithfulness verify | live | An always-on deterministic citation floor plus a flag-gated LLM judge resolved through the judge route. `effective_confidence = min(confidence, faithfulness)` is folded at read time and gates a visible low-confidence tier; it never hard-deletes. A body whose claims will not segment publishes `unassessable` and no score, never a perfect one. |
| Judge route + per-claim verdict ledger | live | Every critique stamps which model judged it and which pipeline revision, so pre- and post-revision populations never pool. Failures classify hard versus soft, and the full per-claim ledger persists including supported claims. |
| Unscoped-absence backstop | live (soft class) | A deterministic detector that fires only on a strong absence assertion the citation floor does not already count, and passes anything hedged, cited or collection-scoped. It adds one to the denominator, never to the numerator. It backstops the judge-off path; it is not a second opinion. |
| Composition tower (country → region → world, plus thematic) | live | The read slice inner-joins the faithfulness critique, so an unverified sub-claim never enters, and an empty slice yields an explicit no-read rather than an invention. One live head per desk. The region tier is deterministic arithmetic over its country records — structural claims and leads carried byte-identically, with the roster as the denominator, no prose and no model — and the world record reads the country assemblies directly. Compositions carry temporal continuity, with the prior read stripped of confidence and lineage so memory cannot corroborate itself. |
| Quotation assembly + fenced Assessment | live | What a composition writes instead of free-text synthesis. Spans are byte-identical against the untruncated source, with four deterministic audit arms over the payload; the Assessment reads only its own spine, AST-guarded, and marks any unsupported sentence rather than dropping it. |
| Banded scorecard | live | One banded row per active desk from high-precision rules over already-verified claims. Every band names the claim it rests on; a dimension with no qualifying claim reads `insufficient-evidence` with a machine reason. Live output is a mix, and that is the design. |
| Deterministic indicators and collection gaps | live | Run-over-run diffs on the structured indicator block the units emit, and starved desk × dimension cells. No LLM. |
| Situation ledger + trajectory | live | One writer diffs each open situation against its previous tick and lands typed transitions, so "how did this evolve" is a query. |
| Reflective journal, chronicle, lenses, inquiry, crossroads | live | An off-chain voice roster on its own cadence: the journal and its consolidation, the chronicle, five faculty lenses and six lean lenses (left, right, centre, pragmatist, militarist, isolationist) each carrying one declared falsifiable prior, a stateful `inquiry` kind whose hypotheses need a frozen resolution test before they can resolve, and a crossroads reader over the day's substrate (patterns across desks, drifts on the band ladder, contradictions the arbiter has not seen, silences by cadence). It writes only its own rows, carries an always-empty `derived_from`, and is absent from the lineage catalog, so no downstream walk can surface it. Everything it wants to change outward goes to a human-gated proposal queue. |
| Reified nexuses, structural balance, graph mining | live | Signed typed edges and the signed-graph analyses over them. |
| ACH competing hypotheses + calibration | live (no skill claim) | Outcomes resolve against an exogenous column so calibration does not grade itself, and a Brier built only from status transitions is flagged self-consistency-only. |
| On-demand consult and deep consult | live | The one billed path, operator-pressed. A model picker chooses the plane; a chosen plane that cannot be honoured raises rather than silently billing the default. |
| Events, the temporal readers and the typed graph | live (every writer flag-gated, defaults off in code) | A first-class `events` row with its append-only transition ledger, `as_of` on the read surface so a dated question is answerable, and one edge vocabulary projected whole into `graph_arcs` behind a kill switch and a staleness refusal. An event citation is a ref kind expanded into per-signal entries at build time, so the judge still grounds on the reports' own text and never on an event summary. |
| Typed absence as one named thing | live | `GET /v3/absence?scope=<desk>` answers with a scope, one kind from a closed eight-kind vocabulary, a proof and a shelf life — never a blank. The desk gap strip, the Morning Read's gaps band, the Inspector and the desk-brief export all read it, so *not checked* and *nothing absent* stay different answers everywhere. |
| Curated collections of the past | live (one pilot holding loaded) | A `collection` is its own descriptor family beside target, analyst, source and action pack. Its rows land in `observations`, bitemporal and partitioned, behind a firewall: a historical row can never reach the freshness, source-health or trigger paths the live plane runs on. A cited observation resolves in the card, carries its unit and its stale-tense marker, and is graded on its own rendered bytes. |
| Contrary-evidence pass | live | The one organ that goes looking for opposition: a counter-query on the research ladder for material claims, landing `contention` records and never a verdict. Four fences stand between a search result and a `contradicts` — the host's class, a date inside the claim's window, the subject named in the matched sentence, and two independent pages — because the first unfenced run produced three contradictions and all three were false. |
| Layered source fan-out and the divergence map | live | Per country, a curated and versioned `source_layers` table over a closed six-layer vocabulary, with every aperture declared, so a layer that is structurally absent reads as declared-absent rather than as agreement. `GET /v3/layers/divergence` serves the per-desk map, and the divergence unit files a finding only on a move against a country's own rolling baseline, not on the gap itself. |

## Measurement

| Capability | State | Note |
|---|---|---|
| Standing external auditor | live | The first analyst that checks a claim against the world rather than against internal consistency, always through the governed web pack. It writes a heartbeat on every run, including one that audits nothing, so silence and death cannot look alike. |
| External grading at width | live | An append-only ledger written by a third model family, fenced from both the writer's family and the judge's family by component id and registered family. The evidentiary contract is code, not a rubric prompt. |
| Attention measurement | live (unvalidated instrument) | One out-of-plane reference per desk, unit and day from its own web search, diffed against the desk's slice on two deliberately separate metrics. It counts; it does not repair, and it raises no alerts. |
| Skill scoreboard | live, honest-null | Per-unit faithfulness and correctness-vs-reference, the calibration Brier, and the acute-forecast score — published as `brier_answered` beside `brier_all` (expired forecasts held at maximum penalty) + `expired_count` and per-minter hypothesis open/resolved counts, so an unresolved backlog can never hide behind the answered-only number. A thin or degenerate result is published as withheld, never as a bare positive number. |
| Correctness grader | live (thin coverage, published as such) | It grades every country desk nightly against a reference built blind to the platform's own substrate, and it fails loud and fails empty: with no current reference it writes nothing, calls no model, and names which gap it is. |
| Grader roster | live | `GET /api/v1/v3/eval/grader_roster?nights=7` serves correctness and coverage together at desk grain, because neither is readable without the other. Over the last seven nights it reports 232 desk-grain rows, 58 of them with any claim the reference decided and 174 unmeasured, a mean correctness of 46.7% at 8.2% mean coverage, and pooled over claims 1,168 claims of which 103 were decided and 52 borne out. A handful of claims confirmed and nine in ten never looked at is what that pair says, and the composition correctness gate stays off because of it. Those figures are a reading of the live route at the deployed head, not a standing claim — re-read the route rather than this line. |
| Independent reference builder | live (free plane; thin) | The only writer of the reference table the grader reads — a grader that could build its own reference could close the loop on itself. Its scheduler orders by last attempt, not last success, so a target the lane cannot build does not pin the queue. |
| Measured optimizer | built, experimental; plane mothballed | Scoped to one measured unit, human-gated, and unable to promote on a non-positive delta. The plane itself is mothballed by decision and refuses at its entry point. |
| Read receipts | live | An append-only read-telemetry ledger behind the scoreboard panel. |
| Production gauge + integrity loops | live (paging) | Expected-versus-actual per producing loop, plus three integrity loops asking whether what a loop produces is still what we think it is. |

## Built, with the consequence gated off

The compute runs and the result is visible; the consequence engages only when
an operator opts in. Values are in [TUNABLES.md](TUNABLES.md).

- Two-tier composition evidence (basis plus a labelled, capped periphery).
- Composition correctness gate — quotes a unit into the periphery rather than dropping it.
- Structural-claims verification — verdicts always computed and shown; folding them into confidence is gated.
- Fact-decay weighting, contested-claim accumulation and the LLM contention tie-break.
- Outbound research at the desk rung: the write path and measurement run, but desk slices still exclude research signals.
- Retention sweeps: one engine, both policies at a zero TTL, so nothing is purged.
- External retrieval: the engine is profile-gated and the grant leg exists only on the consult surfaces.
- Browser-fingerprint fetch and the paid search rung: both ship inert.
- The open-question harvest is an operator one-shot, not a cadence.

## Retired and frozen

Sequenced and documented in [SEAMS.md](SEAMS.md), so the trusted spine stays
clean. In every case the historical rows remain in the database, unread — a
stop, not a clean slate.

- **`country_assessor`**, the monolithic per-country one-pager: retired and
  stopped. The units plus the composition supersede it.
- **`country_predictor`** and **`india_energy_predictor`**, forecast-as-claim:
  retired and stopped. Forecasting returns only as a measured scoreboard, never
  as free-text claim.
- **`country_optimizer`**, the always-on unmeasured optimizer: cadence-frozen.
- **`cross_correlator`**: retired. Its mission is carried by the contested-claims
  arbiter.
- **`world_assessor` is not retired** — it graduated into the world composition.
- **The journal roster is not frozen** — it runs on cadence as an introspective
  instrument, off the product chain.

## Not built

Media extraction models (the job plane is real end to end; with no endpoint it
refuses loudly). Non-text modality renderers. The `stream` acquisition mode.
Evidence-archive retention and expiry, and a non-filesystem object store.
The `claim_watch` closer — the watcher flags, nothing corrects or closes. A
paged-human alert edge: delivery is bus-only. A TAXII destination, and the
inbound webhook push half, both waiting on provisioning rather than on code.
RBAC, SSO and real multi-tenant isolation. Horizontal scale-out.

Each with its guard rail and its reason: [SEAMS.md](SEAMS.md).
