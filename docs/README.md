<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Legba documentation

Every page in this directory, grouped by what you are trying to do. The root [README](../README.md)
says what Legba is; [CONTRIBUTING](../CONTRIBUTING.md) is the short guide for changing it.

## Start

| Page | One job |
|---|---|
| [Tour](TOUR.md) | your first ten minutes: a finding, its citations, its verification, the source |
| [Setup](SETUP.md) | from zero to a running instance, and who can get in afterwards |
| [Bring-up](BRINGUP.md) | how a fresh instance gets its descriptors registered |
| [Operating your instance](OPERATING_YOUR_INSTANCE.md) | seeding, curating, measuring and governing a deployment; turning on sign-in, and what each role can and cannot do |
| [FAQ](FAQ.md) | short answers to first questions, linked to the pages that own them |

## Concepts

| Page | One job |
|---|---|
| [Architecture](ARCHITECTURE.md) | the planes, the descriptor model, the analyst kinds, the tower, the measurement layer, the actor runtime |
| [Flows](FLOWS.md) | the end-to-end paths as numbered steps, with the table or route each step touches |
| [Data model](DATA_MODEL.md) | the tables by plane: who wrote each row, citations as rows, the held record, origins, events, the temporal surface and the graph projection |
| [Analysis](ANALYSIS.md) | every analyst mechanism: what it reads, what it writes, how it is measured, its cadence |
| [The correctness grader](CORRECTNESS_GRADER.md) | the per-unit grader and its badges |
| [The reference builder](REFERENCE_BUILDER.md) | the job that builds the references the grader scores against |
| [Forecast ledger](LEDGER.md) | forecasts: the resolver vocabulary, the seal, resolution, adding a forecast |
| [Acquisition](ACQUISITION.md) | how data enters: sources, enrichment, fan-out |
| [On-request fetching](operations/on-request-fetching.md) | the ingestion intake and the page pipeline |
| [Manual ingest](MANUAL_INGEST_FORMAT.md) | the batch format for hand-supplied data |
| [Data sources](DATA_SOURCES.md) | the source catalog model: scope, kinds, classes, licences, source layers |
| [Claim sources](operations/claim-sources.md) | how to switch on a claim source |
| [Collections](COLLECTIONS.md) | curated holdings of the past: the manifest, the firewall, the verifier |
| [AI models](AI_MODELS.md) | every model, its plane and purpose, and how the runtime reaches it |
| [Agency gating](AGENCY_GATING_MODEL.md) | the trust model for what an analyst may fetch or do |
| [UI](UI.md) | the console and the phone shell: pages, panels, sign-in and roles on screen, building and running |
| [Instance profiles](PROFILES.md) | the manifest that makes an instance: what each key means, how one is selected |
| [Direction](DIRECTION.md) | the rule that orders the work, and what is next |

## Operations

| Page | One job |
|---|---|
| [Runbook](RUNBOOK.md) | start, stop, deploy, health, routine tasks (accounts included), failure signatures, release |
| [Rolling deploy](operations/rolling-deploy.md) | shipping a change to a provisioned stack with the tiered deploy script; the nightly suite |
| [Archive tiering](operations/archive-tiering.md) | hot to cold tiering of the evidence archive, restore, moving the cold tier |
| [Alerting](ALERTING.md) | one alert path, one log, one page rule |

## Reference

| Page | One job |
|---|---|
| [Status](STATUS.md) | what is live, what is off by default or partial, and what is not built |
| [Release state](RELEASE_STATE.md) | generated: the reference deployment's counts (the example instance's, not the product's scale) |
| [Release-state matrix](RELEASE_STATE_MATRIX.md) | every route and UI panel classified by release state (its panel table is generated) |
| [API roles](API_ROLES.md) | generated: every route's minimum role, with and without sign-in |
| [Declared types](TYPES.md) | generated: the type families in the registry |
| [Adding a type](ADDING_A_TYPE.md) | how to add a type to the registry |
| [Tunables](TUNABLES.md) | every budget, cap, governor and threshold, with the plane that pays |
| [Seams](SEAMS.md) | the registry of deliberately not-built things, with their guard rails |
| [Code map](CODE_MAP.md) | one row per module family |
| [Glossary](GLOSSARY.md) | the platform's own words |

## Contributing

| Page | One job |
|---|---|
| [Design rules](DESIGN.md) | the implementation contract: gates, the slim registry image, migrations, flags, banned dependencies, security boundaries |
| [Ticket and decision ids](../CONTRIBUTING.md#ticket-and-decision-ids-in-comments) | how to read the ids cited in code comments |

## History

| Page | One job |
|---|---|
| [Changelog](../CHANGELOG.md) | one line per dated change, newest first |
| [History index](history/README.md) | what each measured report below answers |
| [AGE probe](history/AGE_PROBE_REPORT.md) | the graph-engine probe of 2026-08-03 |
| [Typing bake-off](history/TYPING_BAKEOFF.md) | the typing bake-off of 2026-08-03 and its rerun of 2026-09-22 |
| [Consult divergence](history/CONSULT_DIVERGENCE_2026-09-28.md) | one consult question run five times on two models |

## Reading paths

- **Evaluating it:** the root README, Status, Direction, then Analysis.
- **Running it:** Setup, Tour, Runbook, Operating your instance.
- **Changing it:** Design rules, Architecture, Code map, Seams, then [Contributing](../CONTRIBUTING.md).
- **Auditing a claim:** Tour, then Analysis on the judge, the grader and the audit, then Flows.
