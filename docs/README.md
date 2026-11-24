<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Legba documentation

Every page in this directory, grouped by what you are trying to do. The root
[README](../README.md) says what Legba is; [CONTRIBUTING](../CONTRIBUTING.md) covers changing it.

## Evaluate

| Page | What it covers |
|---|---|
| [FAQ](FAQ.md) | Short answers to first questions |
| [Status](STATUS.md) | What runs, what ships off or as a draft, what is not built |
| [Tour](TOUR.md) | A first walk through the console, and the same steps with `curl` |
| [Architecture](ARCHITECTURE.md) | The planes, the descriptor model and the actor runtime |

## Install and run

| Page | What it covers |
|---|---|
| [Setup](SETUP.md) | From an empty host to a running instance |
| [Runbook](RUNBOOK.md) | Start, stop, deploy, health checks, routine tasks, failures |
| [Rolling deploy](operations/rolling-deploy.md) | Shipping a change to a running stack |
| [Sign-in](OPERATING_YOUR_INSTANCE.md#9-turning-on-sign-in) | Accounts, roles and invitations |
| [Models host](OPERATING_YOUR_INSTANCE.md#7-the-models-host) | Running the `legba-models` service |
| [MCP](OPERATING_YOUR_INSTANCE.md#8-the-mcp-surface) | Connecting an MCP client |
| [Tuning your instance](OPERATING_YOUR_INSTANCE.md) | Measuring and adjusting an instance to your sources |
| [Claim sources](operations/claim-sources.md) | Switching on a claim source |
| [On-request fetching](operations/on-request-fetching.md) | The fetch intake |
| [Archive tiering](operations/archive-tiering.md) | Moving the evidence archive to a cold tier, and restoring it |
| [Alerting](ALERTING.md) | The alert path and the paging rule |
| [AI models](AI_MODELS.md) | The model roles and how to register an endpoint |
| [Tunables](TUNABLES.md) | Budgets, caps and thresholds, with their defaults |
| [Console](UI.md) | The console's pages, the phone shell, building the UI |

## Concepts

| Page | What it covers |
|---|---|
| [Flows](FLOWS.md) | The end-to-end paths, step by step |
| [Data model](DATA_MODEL.md) | The tables, who writes each row, events and the graph projection |
| [Analysis](ANALYSIS.md) | Each analyst mechanism: what it reads and writes, and its cadence |
| [Sources](DATA_SOURCES.md) | Sources, enrichment, fan-out, source kinds and layers |
| [Grading](GRADING.md) | The correctness grader and the references it scores against |
| [Forecast ledger](LEDGER.md) | Sealed forecasts, resolution and the scorecard |
| [Collections](COLLECTIONS.md) | Curated records of the past |
| [Agency gating](AGENCY_GATING_MODEL.md) | What an analyst may fetch or do |
| [Instance profiles](PROFILES.md) | The manifest that defines an instance |
| [Glossary](GLOSSARY.md) | Terms used in these pages |

## Reference

| Page | What it covers |
|---|---|
| [Seams](SEAMS.md) | Things deliberately not built, and how each refuses |
| [Code map](CODE_MAP.md) | Where each part of the code lives |
| [Manual ingest](MANUAL_INGEST_FORMAT.md) | The batch format for hand-supplied data |
| [Adding a type](ADDING_A_TYPE.md) | Adding a type to the type registry |
| [Declared types](TYPES.md) | Generated: the type families in the registry |
| [API roles](API_ROLES.md) | Generated: each route's minimum role |
| [Release-state matrix](RELEASE_STATE_MATRIX.md) | Console panels and qualified routes by release state (panel table generated) |
| [Release state](RELEASE_STATE.md) | Generated: what the tree ships; run the generator for your own instance's counts |
| [Changelog](../CHANGELOG.md) | What changed in each release |
