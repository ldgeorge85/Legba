<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Status — the truth-in-labeling page

What is live, what is built but switched off by default or only partly wired, and what is not built,
as of the commit this page ships in. It describes the platform. Where a row describes the geopolitics
example instance (its desks, its tower, its lenses), the row says so. This page quotes no counts: live
counts of sources, desks and units for the reference deployment are generated into
[RELEASE_STATE.md](RELEASE_STATE.md) by a script that reads the registry database, and they are that
instance's numbers, never the product's scale.

Related pages: per-route and per-panel maturity is in [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md);
every knob's value is in [TUNABLES.md](TUNABLES.md); the register of deliberately not-built things, each
with its guard rail, is [SEAMS.md](SEAMS.md); dated changes are in the [CHANGELOG](../CHANGELOG.md).

## How to read a state

| State | Means |
|---|---|
| **live** | Runs end to end in a stock deployment and its consequence is engaged. |
| **off by default** | Built and tested, behind a switch (an environment variable or a descriptor option) that ships off. The off path is the earlier behaviour, unchanged. |
| **ships draft** | Real code with tests and a descriptor that ships `draft` or `configured`; an admin activates it per instance through the audited lifecycle route. |
| **partial** | Part of the capability runs; the page says which part, and the rest is a declared seam. |
| **guarded seam** | The surface exists and refuses loudly until its real edge is wired. It never fabricates output. |
| **not built** | No code claims it. |

Tree state and live state differ by design. A descriptor ships `draft` and is activated by an admin, so
`draft` in the tree beside `active` in a registry is the intended lifecycle, not drift; the descriptor
state-drift gauge reports those rows as expected promotions.

## Where it is weak

Single-tenant, single-node, run-it-yourself. Accounts and roles exist behind a switch; groups, single
sign-on and per-source permissions do not.

- **Composed reads are quotation assemblies, because free-text synthesis did not hold up.** Two
  independent external gradings put composed prose at around half the pre-committed accuracy bar, so the
  layer was demoted rather than defended: a composition carries byte-identical spans from the reads
  below it under a deterministic title, with the interpretive voice fenced into a separately graded
  Assessment generated from the assembly's own spine. The rollback is one flag back to the prose path.
- **A subject read through one frame can miss its second story.** Frame monopoly steers attention, and
  it has been measured doing so on the example instance. A coverage census and a floor detector exist;
  until the loop closes, a subject's silence on a topic is weak evidence of absence.
- **Faithfulness cannot see a faithful-but-wrong claim.** The verify pass measures groundedness in the
  cited evidence, not truth. The commonest failure it structurally misses is an absence stated as a
  world fact when the sources simply carried nothing; a deterministic backstop and a prompt rule push
  those toward collection-scoped phrasing, but scoping a claim is not checking it against the world.
  The external audit and the correctness grader exist to do that, and they are young.
- **Correctness is measured thinly, and the number says so.** The references are built on the free
  plane and bear on only a small share of what a subject's reads say, so a correctness figure is always
  published beside its coverage and never alone, and a night that decides no claim is reported
  unmeasured rather than scored. The composition correctness gate is off for that reason.
- **The coherence loop flags; it does not correct.** New evidence bearing on a standing open question
  lands a review flag and an append-only edge, and stops there. Nothing propagates a correction into
  the product that rested on the stale reading, and nothing closes a question (SEAMS #49).
- **No skill claim is made.** The forecast ledger withholds every score until enough questions have
  resolved; the acute-forecast pilot reports no proven skill and abstains on degenerate windows; the
  correctness gold set is small; the optimizer's measured delta is not positive, so it promotes nothing.
  Each of those is published rather than hidden.
- **The judge can be down.** The judge route is repointable by configuration. When the route it points
  at is unreachable, critiques are stamped `judge_status='deterministic'`: the citation floor, published
  as provisional under a ceiling, never a fabricated pass. Pointing the judge at the local plane is one
  configuration line, at the cost of same-model grading.
- **Vector retrieval grounding is a guarded pilot on one unit.** It has thickened the low-faithfulness
  tail before, so a per-run guard stops injecting the moment that recurs.
- **Labeler independence is discipline, not an invariant.** Gold labels come from outside the
  production plane and the labeler is stamped on every label: recorded, never validated.

No enterprise, multi-tenant, fine-grained permission or forecast-accuracy claim is made.

## The analysis spine

| Capability | State | Note |
|---|---|---|
| Source-first acquisition and predicate fan-out | live | Poll and push. Intra-source exact-duplicate collapse at ingest is on. A place-less signal goes untagged rather than mistagged. |
| Baseline enrichment (language, geo, entities, relations) | live | Out of process on the model service. Non-Latin bodies are translated before extraction. |
| Bounded reasoning units writing cited findings | live | Each unit answers one narrow question over a cited slice; an instance declares its own units (the example instance runs a set over its country desks and one over its thematic desks). Reactive and cadence firing; `[N]`-cited strict-JSON findings; every run ends in a mandatory verify. |
| A second desk family on the same primitive | live (example instance) | The example's supply-chain desks and its cyber desk run the same unit kinds with no new code path, only descriptors, showing that a desk is a registered subject frame rather than a country. Both families still sit inside the example's domain. |
| Mandatory faithfulness verify | live | An always-on deterministic citation floor plus a judge model resolved through the judge route. `effective_confidence = min(confidence, faithfulness)` is folded at read time and gates a visible low-confidence tier; nothing is deleted. A body whose claims will not segment publishes `unassessable` and no score. |
| Judge route and per-claim verdict ledger | live | Every critique stamps which model judged it and which pipeline revision, so populations from different revisions never pool. |
| Unscoped-absence backstop | live | A deterministic detector that fires only on a strong absence assertion the citation floor does not already count, and passes anything hedged, cited or collection-scoped. |
| Composition tower | live (example instance's shape) | Compositions quote verified unit claims and never paraphrase them; the read slice joins the faithfulness critique, so an unverified sub-claim never enters, and an empty slice yields an explicit no-read. In the example the floors are country, region (deterministic arithmetic, no model) and world, plus thematic. Prior reads are carried stripped of confidence and lineage, so memory cannot corroborate itself. |
| Quotation assembly and fenced Assessment | live | Spans are byte-identical against the untruncated source, with deterministic audit arms over the payload; the Assessment reads only its own spine and marks any unsupported sentence rather than dropping it. |
| Banded scorecard | live | One banded row per active desk from high-precision rules over verified claims. Every band names the claim it rests on; a dimension with no qualifying claim reads `insufficient-evidence` with a reason. |
| Deterministic indicators and collection gaps | live | Run-over-run diffs on the structured indicator block, and starved subject-by-dimension cells. No model. |
| Situation ledger and trajectory | live | One writer diffs each open situation against its previous tick and lands typed transitions. |
| Journal, chronicle, lenses, inquiry, crossroads | live | An off-chain voice roster on its own cadence. The faculty lenses are the platform's; the political-lean lenses are the example instance's. It writes only its own rows, carries an empty `derived_from`, and is absent from the lineage catalog. Anything it wants to change outward goes to a human-gated proposal queue. |
| Reified relations, structural balance, graph mining | live | Signed typed edges and the signed-graph analyses over them. |
| Competing hypotheses and calibration | live (no skill claim) | Outcomes resolve against an exogenous column. The self-consistency resolution sweep is off by descriptor option, so a hypothesis's label moves only on subsequent facts. |
| Consult and deep consult | live | The on-demand path, run only when a person asks. A model picker chooses the plane, and a chosen plane that cannot be honoured raises rather than silently using another. A provenance census over the answer's cited refs floors its uncertainty when few resolve. Every turn stores its usage receipt with per-call receipts and a verification stamp. A signed-in person sees only their own sessions. A turn that changes the system needs an admin. |
| Events and the as-of readers | off by default | The `events` row, its append-only transition ledger and its writers run only with `LEGBA_EVENTS` on; `as_of` on the read surface answers dated questions. |
| The typed graph projection | off by default | One edge vocabulary projected into `graph_arcs` behind `LEGBA_GRAPH_PROJECTION`, with a staleness refusal. There is no graph database: the empty one was removed. |
| Typed absence | live | `GET /v3/absence?scope=<desk>` answers with a scope, one kind from a closed vocabulary, a proof and a shelf life, never a blank. Every reader surface that shows a gap reads it. |
| Due and absent | ships draft; its alerts are logged only | A deterministic check for what was expected by an explicit date (a sealed forecast, a calendar entry, an inquiry's resolve-by) and has no matching record. It writes alerts that are logged, never paged. |
| Search health | live | One rule decides whether a recorded search answered or ran degraded; a not-found on a degraded search is typed `search_failed`. `GET /v3/system/search-health` publishes the degraded share per provider. |
| Curated collections of the past | live | A `collection` descriptor family whose rows land in `observations` behind a firewall from the live plane. A cited observation resolves in the card with its stale-tense marker. |
| Contrary-evidence pass | live | A counter-query for material claims, landing `contention` records and never a verdict, behind four fences on `contradicts`. |
| Layered source fan-out and divergence | live (example instance's layers) | A curated `source_layers` table over a closed layer vocabulary with every aperture declared; `GET /v3/layers/divergence` serves the map. |
| Desk row contract | partial | A typed row contract for desk output, gated before write, runs as a detached shadow beside the live findings; nothing reads its rows on a reader surface. |

## Record, provenance and accounts

| Capability | State | Note |
|---|---|---|
| Citations as rows | live | Every new finding writes `finding_sentences`, `citation_support`, `citation_unresolved` and `run_handed` rows in its own transaction. Older findings are filled by a backfill script. |
| A named writer on every row | live | `principals` names every writer (a person, a service, an analyst seat, a rule, the shared admin credential); judgement, decision, configuration and citation rows carry `written_by`. With sign-in off every person writes as the shared admin credential. |
| Source origins and origin groups | live, not yet used | Derived by a stored rule and correctable by an admin through an append-only log. Independence counting does not read them yet. |
| The held record of statements | partial, off by default | The `assertions` family, its hash-chained status log and its support links exist; the only feed is the desk-row shadow of desks named in `LEGBA_HELD_RECORD_SHADOW` (unset = off). Two API reads and a verify script read it; no console surface shows it. |
| Sign-in, roles and read-only readers | off by default | `LEGBA_AUTH_SIGNIN=1` turns on local accounts, an owner, invitations and three roles (reader, contributor, admin), a minimum role on every route ([API_ROLES.md](API_ROLES.md)) and a credential per service. Off, the edge's perimeter password is the only gate and every person is the same admin. [Operating your instance §9](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in). |
| Dropping the perimeter password | off by default | `LEGBA_EDGE_PERIMETER=0` is accepted only with sign-in on. |
| The phone shell at `/m/` | live | The reader surfaces on a phone. Installing it as a web app needs sign-in on (the edge serves the app's bootstrap files only then). No web push; alerts go to ntfy. |
| Alert acknowledge and snooze | live | A contributor or an admin; shared, recorded and audited. |
| One registry of types | live | Every handler, output kind and alert kind is declared once; the tables are derived and [TYPES.md](TYPES.md) is generated. |

## Acquisition

| Capability | State | Note |
|---|---|---|
| Claim sources | live; each source ships configured | A claim source supports only "X claimed Y", enforced on the verify floor, fact writes, counting and intake, and cannot go active without a recorded sign-off ([claim sources](operations/claim-sources.md)). |
| On-request ingestion intake | off by default | One intake for pages something asks for, with admission in code, budgets and per-host politeness; the drain runs only with `LEGBA_INTAKE_DRAIN=on` ([on-request fetching](operations/on-request-fetching.md)). |
| Subscribe connector | off by default | `acquisition: subscribe` over server-sent events, resumable by cursor; the supervisor runs only with `LEGBA_SUBSCRIBE_SUPERVISOR` on. Other transports are SEAMS #78. |
| Evidence archive and its cold tier | ships draft; tiering runs when scheduled | Content-addressed archive of cited bytes; tiering moves older objects into bundles that every reader reads transparently ([archive tiering](operations/archive-tiering.md)). Expiry is built and off (`LEGBA_ARCHIVE_RETENTION`). |
| Entity name lookup by key table | off by default | `LEGBA_ENTITY_ALIAS_LOOKUP` chooses `scan` (the default), `shadow` or `table`. |

## Measurement

| Capability | State | Note |
|---|---|---|
| Standing external auditor | live | Checks a claim against the world rather than against internal consistency, through the governed web pack, and writes a heartbeat on every run so silence and death cannot look alike. |
| External grading at width | live | An append-only ledger written by a third model family, fenced from the writer's and the judge's families. |
| Attention measurement | live (unvalidated instrument) | One out-of-plane reference per desk, unit and day, diffed against the desk's slice. It counts; it does not repair. |
| Skill scoreboard | live, honest-null | Per-unit faithfulness and correctness, the calibration Brier and the acute-forecast score, with the unresolved backlog published beside the answered-only number. A thin result is withheld. |
| Correctness grader and grader roster | live (thin coverage, published as such) | Grades desks nightly against references built blind to the platform's substrate; `GET /v3/eval/grader_roster` serves correctness and coverage together. With no current reference it writes nothing. |
| Independent reference builder | live (free plane) | The only writer of the references the grader reads. |
| Forecast ledger, resolver and scorecard | live; the resolver ships draft | Sealed questions with a resolver from a closed vocabulary; the resolver proposes and a person decides every outcome except a series threshold, from the console's resolution queue; the scorecard withholds every score until five questions resolve ([LEDGER.md](LEDGER.md)). |
| Published evaluation runs | live | Aggregates only, append-only, corrected by a new version. |
| Measured optimizer | built; plane mothballed | Scoped to one measured unit, human-gated, unable to promote on a non-positive delta; its entry point refuses. |
| Read receipts | live | An append-only read-telemetry ledger. |
| Production gauge and integrity loops | live (paging) | Expected-versus-actual per producing loop, plus integrity loops asking whether what a loop produces is still what it should be. |
| Tiered deploy and the nightly suite | live (tooling) | `scripts/rolling_deploy.sh --tier` ships a change with the gate its tier needs; `scripts/host_nightly_suite.sh` runs the whole suite each night ([rolling deploy](operations/rolling-deploy.md)). |

## Built, with the consequence switched off

The compute runs and the result is visible; the consequence engages only when an admin opts in.
Values are in [TUNABLES.md](TUNABLES.md).

- Two-tier composition evidence (basis plus a labelled, capped periphery).
- The composition correctness gate.
- Folding structural-claims verdicts into confidence (the verdicts are always computed and shown).
- Fact-decay weighting, contested-claim accumulation and the model tie-break on contention.
- Outbound research at the desk rung: the write path and measurement run, but desk slices exclude
  research signals.
- Retention sweeps: one engine, both policies at a zero TTL, so nothing is purged; run traces are kept
  unless their purge is switched on.
- External retrieval: the engine is profile-gated and the grant exists only on the Consult surfaces.
- Browser-fingerprint fetch and the paid search rung: both ship inert.
- The open-question harvest is a one-shot, not a cadence.

## Retired and frozen

Each is recorded in [SEAMS.md](SEAMS.md). The historical rows stay in the database, unread: a stop,
not a clean slate. The names are the example instance's analysts.

- The monolithic per-country assessor: retired. The units plus the composition supersede it.
- Forecast-as-claim predictors: retired. Forecasting returns only as the sealed forecast ledger, never
  as free-text claim.
- The always-on unmeasured optimizer: cadence-frozen.
- The cross-correlator: retired; the contested-claims arbiter carries its job.

## Not built

- Media extraction models: the job plane is real end to end, and with no endpoint it refuses loudly.
  Non-text renderers.
- Subscribe transports other than server-sent events (SEAMS #78).
- Re-archiving a signal cited after its bytes expired, and a non-filesystem object store for the hot
  tier.
- The standing-question closer: the watcher flags; nothing corrects or closes.
- A TAXII destination and the inbound webhook push half, both waiting on provisioning rather than on
  code.
- Groups, single sign-on, per-source and per-set permissions, a contributor's own Consult changes and
  narrowed service credentials (SEAMS #83–#87). Multi-tenant isolation. Horizontal scale-out.

Each with its guard rail and its reason: [SEAMS.md](SEAMS.md).
