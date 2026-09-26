<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Legba documentation

Four tiers. The front door tells you what it is and gets you running; the design tier describes the
shape and the reason for the shape, in the present tense; the reference tier is tables and procedures
checked against the tree; the history tier is where the dates live.

## Front door

| Page | One job |
|---|---|
| [README](../README.md) | what Legba is, in two sentences and one diagram |
| [Direction](DIRECTION.md) | the rule that decides what gets built next, and what is next |
| [Tour](TOUR.md) | your first ten minutes: a finding, its citations, its verification, the source |
| [Setup](SETUP.md) | from zero to a running instance |
| [Operating your instance](OPERATING_YOUR_INSTANCE.md) | seeding, curating, measuring and governing your own deployment |
| [FAQ](FAQ.md) | the questions people ask first |

## Design

| Page | Covers |
|---|---|
| [Architecture](ARCHITECTURE.md) | the planes, the descriptor model, the analyst kinds, the tower, the measurement layer, the actor runtime |
| [Design rules](DESIGN.md) | the implementation contract: gates, the slim registry image, migrations, flags, budgets, tests |
| [Analysis](ANALYSIS.md) | every analyst mechanism: what it reads, what it writes, how it is measured, its cadence |
| [Flows](FLOWS.md) | the end-to-end paths as numbered steps, with the table or route each step touches |
| [Acquisition](ACQUISITION.md) | how data enters: sources, enrichment, fan-out |
| [Data model](DATA_MODEL.md) | the tables that matter, grouped by plane |
| [Data model v3](DATA_MODEL_V3.md) · [its build order](V3_IMPLEMENTATION_PLAN.md) | events, the temporal surface, one typed graph |
| [Data sources](DATA_SOURCES.md) | the source catalog and its tiers |
| [Collections](COLLECTIONS.md) | curated holdings of the past: the manifest, the firewall, the verifier |
| [Layers](LAYERS.md) | the per-country layer table + aperture declaration |
| [Manual ingest](MANUAL_INGEST_FORMAT.md) | the batch format for hand-supplied data |
| [AI models](AI_MODELS.md) | the model planes, how the runtime reaches them, the judge |
| [Agency gating](AGENCY_GATING_MODEL.md) | how an analyst is allowed to act |
| [The correctness grader](CORRECTNESS_GRADER.md) · [the reference builder](REFERENCE_BUILDER.md) | the desks graded against independently built references |
| [UI](UI.md) | the operator workstation |

## Reference

| Page | Holds |
|---|---|
| [Runbook](RUNBOOK.md) | start, stop, deploy, health, routine tasks, failure signatures, release |
| [Tunables](TUNABLES.md) | every budget, cap, governor and threshold, with the plane that pays |
| [Status](STATUS.md) | what is live, gated, built-not-deployed, seam, or designed |
| [Release state](RELEASE_STATE.md) · [release matrix](RELEASE_STATE_MATRIX.md) | generated counts and per-route maturity |
| [Code map](CODE_MAP.md) | one row per module family |
| [Glossary](GLOSSARY.md) | the platform's own words |
| [Seams](SEAMS.md) | the registry of deliberately not-built things, with their guard rails |
| [Tickets](TICKETS.md) | where ticket and decision ids are defined |

## History

| Page | Holds |
|---|---|
| [Changelog](../CHANGELOG.md) | one line per dated change, newest first |
| [docs/history](history/README.md) | the incident write-ups, the decision records, and the measured reports: the AGE probe, the typing bake-off and its rerun |

## Reading paths

- **Evaluating it:** README, Status, Direction, then Analysis.
- **Running it:** Setup, Tour, Runbook, Operating your instance.
- **Changing it:** Design rules, Architecture, Code map, Seams, then [Contributing](../CONTRIBUTING.md).
- **Auditing a claim:** Tour, then Analysis on the judge, the grader and the audit, then Flows.
