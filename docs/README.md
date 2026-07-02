<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Legba documentation

Start with the [project README](../README.md) for what Legba is and the quick
start. This page is the index: pick your path.

## Reading paths

**"Show me." (10 minutes)**
[TOUR.md](TOUR.md) — read a finding, check its verification, drill it to
source. Then [FAQ.md](FAQ.md) for the questions you're already forming.

**"I'm standing one up." (operator)**
[SETUP.md](SETUP.md) (from-zero bootstrap) → [RUNBOOK.md](RUNBOOK.md) (day-2
operations, migrations, troubleshooting) → [UI.md](UI.md) (the workstation) →
[DATA_SOURCES.md](DATA_SOURCES.md) (adding feeds) →
[MANUAL_INGEST_FORMAT.md](MANUAL_INGEST_FORMAT.md) (hand-loading data, if present).

**"How does it work?" (understanding)**
[ARCHITECTURE.md](ARCHITECTURE.md) (the four planes, why it's shaped this way)
→ [FLOWS.md](FLOWS.md) (life-of-a-signal / finding / composition narratives) →
[ANALYSIS.md](ANALYSIS.md) (the analytical methodology: units, verify,
compositions, evals) → [GLOSSARY.md](GLOSSARY.md) (every coined term).

**"What's real?" (evaluator / auditor)**
[STATUS.md](STATUS.md) (truth-in-labeling: built / guarded seam / designed) →
[SEAMS.md](SEAMS.md) (the registry of intentionally-not-built things — they
fail loud, never fake output) →
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) (per-route / per-panel
maturity) → [DIRECTION.md](DIRECTION.md) (designed-not-built futures: RBAC,
tenancy, STIX/TAXII, MCP, scale-out).

**"I'm going into the code." (contributor)**
[CODE_MAP.md](CODE_MAP.md) (where everything lives) → [DESIGN.md](DESIGN.md)
(implementation decisions) → [DATA_MODEL.md](DATA_MODEL.md) (tables, tiers,
mutation rules) → [ACQUISITION.md](ACQUISITION.md) (the ingest plane) →
[AGENCY_GATING_MODEL.md](AGENCY_GATING_MODEL.md) (when an LLM analyst may act).

## All documents

| Document | One line |
|---|---|
| [TOUR.md](TOUR.md) | Your first ten minutes: see, distrust, verify, drill. |
| [FAQ.md](FAQ.md) | Plain answers to a newcomer's actual questions. |
| [SETUP.md](SETUP.md) | From-zero bootstrap to a running instance. |
| [RUNBOOK.md](RUNBOOK.md) | Day-2 operations: migrations, registration, troubleshooting. |
| [UI.md](UI.md) | The operator workstation, panel by panel. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The four planes and the reasoning behind the shape. |
| [FLOWS.md](FLOWS.md) | End-to-end narratives: life of a signal, a finding, a composition. |
| [ANALYSIS.md](ANALYSIS.md) | The analysis plane: units, coalescing, verify, compositions, evals, agency. |
| [ACQUISITION.md](ACQUISITION.md) | The acquisition plane: sources, enrichment, fan-out. |
| [DATA_SOURCES.md](DATA_SOURCES.md) | Source-handler kinds + the live catalog; how to add your own. |
| [DATA_MODEL.md](DATA_MODEL.md) | Data tiers: what mutates, what appends, what supersedes. |
| [AI_MODELS.md](AI_MODELS.md) | The hosted models, providers, call patterns, budgets. |
| [GLOSSARY.md](GLOSSARY.md) | Every coined term, defined once. |
| [DESIGN.md](DESIGN.md) | Implementation design and decision record. |
| [CODE_MAP.md](CODE_MAP.md) | Navigational map of the codebase. |
| [SEAMS.md](SEAMS.md) | THE registry of declared not-built seams (fail-loud contract). |
| [STATUS.md](STATUS.md) | Release boundary table + where it is weak today. |
| [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) | Route/panel-level maturity classification. |
| [DIRECTION.md](DIRECTION.md) | Designed-not-built: RBAC/SSO, tenancy, STIX/TAXII, MCP, multimodal, scale-out. |
| [AGENCY_GATING_MODEL.md](AGENCY_GATING_MODEL.md) | The trust model for agentic analyst tools. |
| [TICKETS.md](TICKETS.md) | Ticket-reference conventions. |

Conventions: coined terms (**signal**, **desk/target**, **unit**,
**composition**, **seam**, **substrate**, …) are defined in the
[GLOSSARY](GLOSSARY.md) and introduced once per document. Documents state what
IS; future work lives only in [DIRECTION.md](DIRECTION.md) and
[SEAMS.md](SEAMS.md).
