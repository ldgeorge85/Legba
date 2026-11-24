<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Status

For anyone deciding whether Legba does what they need. This page lists each capability in the
tree, whether it runs out of the box, ships switched off, or is not built.

Related pages: each console panel and qualified route by release state is in
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) (generated). Counts for a running deployment come
from `scripts/generate_release_manifest.py`, which writes [RELEASE_STATE.md](RELEASE_STATE.md).
Not-built things and their guards are in [SEAMS.md](SEAMS.md). Knob defaults are in
[TUNABLES.md](TUNABLES.md).

## States

| State | Means |
|---|---|
| **live** | Runs in a stock deployment once its descriptor is active. |
| **off by default** | Built and tested, behind an environment variable or descriptor option that ships off. |
| **ships draft** | The descriptor ships `draft`. An admin activates it through the lifecycle route. The example profile's lifecycle overlay activates it. |
| **partial** | Part of it runs; the note says which part. |
| **not built** | No code. Where a surface exists it refuses with an error. |

A descriptor that is `draft` in the tree and `active` in a registry is the intended lifecycle, not
drift.

## Known limits

- One tenant, one node, run by you. Accounts and roles exist behind a switch; groups, single
  sign-on and per-source permissions do not.
- The claim-checking pass measures whether a claim follows from what it cites, not whether it is
  true. A well-cited claim from a wrong source can pass.
- By default the checking pass uses the same model endpoint that wrote the read. A separate judge
  route is a configuration change; the cross-model judge component ships `draft`.
- When the judge route is unreachable, critiques fall back to the deterministic citation floor
  and are stamped `judge_status='deterministic'`.
- The correctness grader covers a small share of what the reads say. Its score is always shown
  with its coverage, and a night with no decided claim reads unmeasured.
- A subject read through one frame can miss a second story. A subject's silence on a topic is weak
  evidence of absence.
- New evidence on an open question raises a review flag. Nothing corrects the earlier read or
  closes the question (SEAMS #49).
- Forecast and calibration scores are withheld until enough questions resolve. The prompt
  optimizer cannot promote a candidate without a positive measured change.
- Gold labels record who labelled them. Independence of the labeller is not checked.

## Analysis

| Capability | State | Note |
|---|---|---|
| Source-first acquisition and predicate fan-out | live | One fetch per source; signals routed to every subject whose predicate matches. |
| Enrichment (language, translation, geo, entities, relations) | live | Runs on the `legba-models` service. |
| Bounded reasoning units writing cited findings | live | Each unit answers one question over a cited slice; every run ends in verification. |
| Claim-checking pass | live | Citation floor plus a judge model. `effective_confidence = min(confidence, faithfulness)`; nothing is deleted. |
| Unscoped-absence backstop | live | Flags a strong "nothing happened" claim that cites nothing. |
| Compositions by quotation | live | Quote verified unit claims byte for byte; a separately graded Assessment carries the interpretation. |
| World assessment | ships draft | The top composed read in the example profile. |
| Banded scorecard | live | One band per dimension per subject, each naming its claim, or `insufficient-evidence`. |
| Indicators and collection gaps | live | Deterministic, no model. |
| Situation tracker | ships draft | Typed transitions for each open situation. |
| Journal and chronicle | live | Write only their own rows; outward changes go to a human-gated proposal queue. |
| Lenses | ships draft | The six political-lean lenses belong to the example profile. |
| Inquiry and crossroads | ships draft | |
| Relations, structural balance, graph mining | live | Signed typed edges and analyses over them. |
| Competing hypotheses and calibration | live | Hypothesis labels move only on later facts. |
| Consult and deep consult | live | Run only when a person asks. A signed-in person sees only their own sessions. |
| Events and as-of reads | off by default | `LEGBA_EVENTS`. |
| Typed graph projection | off by default | `LEGBA_GRAPH_PROJECTION`; ordinary tables, no graph database. |
| Typed absence | live | `GET /v3/absence?scope=<subject>`: a kind, a reason and a re-check date. |
| Due and absent | ships draft | Reports what was expected by a date and has no record. Its alerts are logged, not paged. |
| Search health | live | `GET /v3/system/search-health`: degraded share per search provider. |
| Curated collections of the past | live | Stored apart from the live record. |
| Contrary-evidence pass | ships draft | Searches for material against a claim; records a contention, never a verdict. |
| Source layers and divergence | ships draft | `GET /v3/layers/divergence`; the layer maps belong to the example profile. |
| Desk row contract | partial | Runs as a shadow beside live findings; no reader page shows it. |

## Record, provenance and accounts

| Capability | State | Note |
|---|---|---|
| Citations as rows | live | Written with each new finding; `scripts/citation_rows_backfill.py` converts older ones. |
| A named writer on every row | live | With sign-in off, every person writes as the shared admin credential. |
| Source origins and origin groups | live | Correctable by an admin. Independence counting does not use them yet. |
| Held record of statements | partial, off by default | Fed only from desks named in `LEGBA_HELD_RECORD_SHADOW`; no console page. |
| Sign-in and roles | off by default | `LEGBA_AUTH_SIGNIN=1`. See [Turning on sign-in](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in) and [API_ROLES.md](API_ROLES.md). |
| Dropping the perimeter password | off by default | `LEGBA_EDGE_PERIMETER=0`, accepted only with sign-in on. |
| Phone shell at `/m/` | live | Installable only with sign-in on. No web push; alerts go to ntfy. |
| Alert acknowledge and snooze | live | Contributor or admin. |
| Type registry | live | [TYPES.md](TYPES.md) is generated from it. |

## Acquisition

| Capability | State | Note |
|---|---|---|
| Claim sources | live | Each ships `configured` and needs a recorded sign-off ([claim sources](operations/claim-sources.md)). |
| On-request fetch intake | off by default | `LEGBA_INTAKE_DRAIN=on` ([on-request fetching](operations/on-request-fetching.md)). |
| Subscribe connector (server-sent events) | off by default | `LEGBA_SUBSCRIBE_SUPERVISOR`. Other transports: SEAMS #78. |
| Evidence archive and cold tier | ships draft | [Archive tiering](operations/archive-tiering.md). Expiry is built and off (`LEGBA_ARCHIVE_RETENTION`). |
| Entity lookup by key table | off by default | `LEGBA_ENTITY_ALIAS_LOOKUP`: `scan` (default), `shadow` or `table`. |

## Measurement

| Capability | State | Note |
|---|---|---|
| Standing external auditor | ships draft | Checks sampled claims against web search; writes a heartbeat each run. |
| Correctness grader | ships draft | Grades subjects nightly against independently built references. |
| Reference builder | ships draft | Builds the references the grader reads. |
| Grader roster | live | `GET /v3/eval/grader_roster`: correctness and coverage together. |
| Skill scoreboard | live | Withholds a thin result. |
| Forecast ledger and scorecard | live | No score until five questions resolve ([LEDGER.md](LEDGER.md)). |
| Forecast resolver | ships draft | Proposes outcomes; a person decides them in the resolution queue. |
| Published evaluation runs | live | Aggregates only, append-only. |
| Prompt optimizer | paused | Human-gated; refuses to promote without a positive measured change. |
| Read telemetry | live | An append-only ledger of what people read. |
| Production gauge and integrity loops | live | Expected against actual output per producing loop; pages on a deficit. |
| Tiered deploy and nightly suite | live | [Rolling deploy](operations/rolling-deploy.md). |

## Built, consequence off

The computation runs and its result is shown; the effect applies only when an admin turns it on.

- Two-tier composition evidence (a capped, labelled periphery).
- The composition correctness gate.
- Folding structural-claims verdicts into confidence.
- Fact-decay weighting, contested-claim accumulation and the model tie-break on contention.
- Outbound research at the desk level (desk slices exclude research signals).
- Retention sweeps (both policies ship with a zero TTL, so nothing is purged; run traces are kept
  unless their purge is turned on).
- External retrieval (granted only to Consult).
- Browser-fingerprint fetch and the paid search provider.

## Retired

Listed in [SEAMS.md](SEAMS.md). Their old rows stay in the database and are not read.

- The per-country assessor, replaced by units and compositions.
- Forecast-as-claim predictors, replaced by the sealed forecast ledger.
- The cross-correlator, replaced by the contested-claims arbiter.

## Not built

- Media extraction models (the job plane refuses without an endpoint) and non-text renderers.
- Subscribe transports other than server-sent events (SEAMS #78).
- Re-archiving a cited signal after its bytes expired; an object store for the hot archive tier.
- Closing or correcting a standing question automatically.
- A TAXII destination and inbound webhook push.
- Groups, single sign-on, per-source permissions, contributor Consult changes and narrowed service
  credentials (SEAMS #83–#87). Multi-tenant isolation. Horizontal scale-out.
