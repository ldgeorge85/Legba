<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Release-state matrix

Every operator-facing route and UI panel, classified by release state, so a
reviewer can tell product-grade from preview without reading each component.

This page classifies surfaces. What is live, gated or untested across the
platform as a whole is [STATUS.md](STATUS.md); the registry of deliberately
not-built things is [SEAMS.md](SEAMS.md).

## States

| State | Meaning |
|---|---|
| **live** | Real backend wired end to end; product-grade. |
| **preview** | Registered and renders, but reads no live backend yet, or renders an honest pending state. It never fabricates data. |
| **hidden** | Built and registered but not surfaced in default navigation. Reachable by id and by record-jump; a saved layout referencing it still resolves. Hidden is a navigation flag, not a build state — a hidden panel can be live. |
| **stub** | Disallowed in a production path ([SEAMS.md](SEAMS.md)). No route or panel is in this state; it is listed only to name the bar. |

A retired panel is not in this table at all. A retired kind leaves
`registry.ts` and becomes a row in `panel-registry/aliases.ts`, which resolves
its id onto the survivor that renders it — the id still resolves, it just no
longer costs a registry row.

---

## 1. Routes

The bearer-gated route surface is enumerated in [RUNBOOK.md](RUNBOOK.md) §4.1
and is **live** throughout — real substrate reads and writes behind fail-closed
auth. These are the rows that need a qualifier:

| Route | State | Note |
|---|---|---|
| `GET /api/v1/registry/healthz` | live (readiness) | Pings Postgres (`SELECT 1`) and NATS. 200 when both respond; 503 naming the failing component otherwise, so the container healthcheck and the Caddy upstream drop a half-dead process out of rotation. Deliberately unauthenticated. |
| `GET /metrics` | live (unauthenticated by design) | Prometheus text exposition, mounted at the app root with no prefix and no bearer gate so a scraper needs no token. Values are real registry counters. Alert rules ship in `deploy/prometheus/legba_alerts.yml`. |
| `POST /api/v1/registry/targets/{target_id}/backfill` | honest 501 | Refuses loud rather than 404ing. The `Backfiller` backend exists in `runtime/subscription/backfill.py`; the registry-plane trigger is intentionally not exposed. |
| `/a2a/skills`, `/a2a/skills/{skill_id}` (runtime, not registry) | live, gated | Mounted only with `LEGBA_A2A_ENABLED` plus a trusted-key allowlist. Otherwise the same paths answer 503 with `a2a_skill_surface_disabled` and the enable recipe — fail-loud, never a silent 404 (SEAMS #15). |
| `GET /api/v1/v3/eval/calibration` | live (honest-null) | The skill scoreboard. A thin or degenerate pilot returns an explicit withheld-claim flag, never a bare positive number. |
| `GET /api/v1/v3/eval/country_scorecard` | live | Projects the persisted bands; it never re-bands and never fabricates one. An empty list means nothing has been computed yet. |
| `GET /api/v1/v3/system/external-audit` | live | The external auditor's board. The contradiction rate is **absent**, not `0.0`, when nothing was checked. |
| `GET /api/v1/v3/system/production-gauge` | live | One row per producing loop, worst first, over the same expectation model the `production_deficit` alert class reads. |
| `GET /api/v1/v3/system/staleness-debt` | live | The coherence-loop debt count, which reports `match_verified: false` on the wire (SEAMS #49). |
| `GET /api/v1/lineage/{row_kind}/{row_id}` | live | Walks the receipt chain hop by hop. Each node carries a receipt hash and a `chain_consistent` boolean, badged "chain-consistent (single-node)" — hash-chaining, not a cryptographic signing claim for analyst output. |
| `GET /api/v1/graph/ego`, `GET /api/v1/graph/edge/{id}` | live | Anchored one-hop neighbourhood with unfiltered facets as the honest denominator, and the why-this-edge-exists read. No depth parameter by design: every hop is a fresh anchored ego. |
| `GET /api/v1/units/{target_id}/correctness` | live | The latest correctness row per bounded unit for one target, plus the target's reference state. Included into the substrate-reads router, so it inherits that surface's auth gate. |
| `GET /api/v1/contention` | live (read-only) | Contested-claim groups and their per-value support clusters. It never mutates a fact, a group or a marker. Disputes only accumulate when the write-path flag is on. |
| `POST /api/v1/read-events` | live | Append-only. A malformed event in a batch is dropped and counted rather than failing the batch; the table refuses `UPDATE` and `DELETE` at a trigger. |
| `GET /api/v1/v3/optimizer/candidates/{id}/diff` | live | Snapshot-based, with no dspy import, over the scoped optimizer. Its panel is badged preview because the human-gated promote flow around it is still maturing, not because the route is pending. |

---

## 2. UI panels

### 2.1 The generated table

Kind, category, scope, title, binding, modes, tier and hidden are generated
directly from `legba-ui-v3/src/panel-registry/registry.ts` by
`scripts/gen_release_state_matrix.py`. Re-run it after adding a panel or
changing a tier; `tests/data_pkg/test_release_state_matrix_current.py`
regenerates the block and fails the suite if the committed table disagrees, so
it cannot go stale silently. Never hand-edit between the markers.

The *why* behind a preview or hidden classification has no machine-readable
source, so it stays hand-maintained in §2.2 and §2.3 and is untouched by
regeneration.

<!-- BEGIN GENERATED PANEL TABLE -->

_Generated by `scripts/gen_release_state_matrix.py` from `legba-ui-v3/src/panel-registry/registry.ts` — do not hand-edit between the markers; re-run the script instead. 61 panel kinds registered — 59 live, 2 preview, 7 hidden (a panel can be both live/preview AND hidden — hidden is a navigation-tier flag, not a build state)._

| Kind | Panel ID | Category | Scope key | Default title | Requires binding | Modes | Tier | Hidden |
|---|---|---|---|---|---|---|---|---|
| `target.claims` | `target_claims` | target | target_id | Target Claims | yes | personal, cis | **live** | no |
| `target.desk_brief_page` | `target_desk_brief_page` | target | target_id | Desk Brief | yes | personal, cis | **live** | no |
| `target.findings` | `target_findings` | target | target_id | Target Findings | yes | personal, cis | **live** | no |
| `target.graph` | `target_graph` | target | target_id | Target Graph | yes | personal, cis | **live** | no |
| `target.map` | `target_map` | target | target_id | Target Map | yes | personal, cis | **live** | no |
| `target.overview` | `target_overview` | target | target_id | Target Overview | yes | personal, cis | **live** | no |
| `target.signals` | `target_signals` | target | target_id | Target Signals | yes | personal | **live** | no |
| `target.situations` | `target_situations` | target | target_id | Target Situations | yes | personal, cis | **live** | no |
| `target.sources` | `target_sources` | target | target_id | Target Sources | yes | personal | **live** | no |
| `target.timeline` | `target_timeline` | target | target_id | Target Timeline | yes | personal, cis | **live** | no |
| `analyst.critiques` | `analyst_critiques` | analyst | analyst_id | Critic Scores | yes | personal | **live** | no |
| `analyst.cross_target` | `analyst_cross_target` | analyst | analyst_id | Cross-target Analyst | yes | personal, cis | **live** | no |
| `analyst.outputs` | `analyst_outputs` | analyst | analyst_id | Analyst Outputs | yes | personal, cis | **live** | no |
| `analyst.runs` | `analyst_runs` | analyst | analyst_id | Analyst Runs | yes | personal | **live** | no |
| `registry.action_packs` | `registry_action_packs` | operator | — | Action-Pack Grants | no | personal | **live** | no |
| `registry.analysts` | `registry_analysts` | operator | — | Analyst Registry | no | personal | **live** | no |
| `registry.sources` | `registry_sources` | operator | — | Source Registry | no | personal | **live** | no |
| `registry.stack` | `registry_stack` | operator | — | Stack Registry | no | personal | **live** | no |
| `registry.targets` | `registry_targets` | operator | — | Target Registry | no | personal | **live** | no |
| `source.detail` | `source_detail` | operator | — | Source Detail | no | personal | **live** | no |
| `source.fanout` | `source_fanout` | operator | — | Fan-out Explorer | no | personal | **live** | yes |
| `source.subscription_builder` | `source_subscription_builder` | operator | — | Subscription Builder | no | personal | **live** | yes |
| `source.subscription_policy` | `source_subscription_policy` | operator | — | Subscription Policy | no | personal | **live** | yes |
| `system.entities` | `system_entities` | operator | — | Entities | no | personal | **live** | no |
| `system.graph_walk` | `system_graph_walk` | operator | — | Graph Walk | no | personal | **live** | no |
| `system.settings` | `system_settings` | operator | — | Model Stack Settings | no | personal | **live** | no |
| `analysis.cross_framing` | `analysis_cross_framing` | system | — | Cross-framing | no | personal | **live** | yes |
| `system.actor_health` | `system_actor_health` | system | — | Actor Health | no | personal | **live** | no |
| `system.alerts_watches` | `system_alerts_watches` | system | — | Alerts & Watches | no | personal, cis | **live** | no |
| `system.audit` | `system_audit` | system | — | Audit-Chain Browser | no | personal | **live** | no |
| `system.budget` | `system_budget` | system | — | Budget Ledger | no | personal | **live** | no |
| `system.consult` | `system_consult` | system | — | Consult | no | personal, cis | **live** | no |
| `system.dead_letter` | `system_dead_letter` | system | — | Dead-letter Inspector | no | personal | **live** | no |
| `system.eval_boards` | `system_eval_boards` | system | — | Eval Boards | no | personal | **live** | no |
| `system.eval_scorecard` | `system_eval_scorecard` | system | — | Eval Scorecard | no | personal | **live** | no |
| `system.findings` | `system_findings` | system | — | Live Feed | no | personal, cis | **live** | no |
| `system.goldset` | `system_goldset` | system | — | Weekly Grading | no | personal | **live** | no |
| `system.governor` | `system_governor` | system | — | Governor Events | no | personal | **live** | no |
| `system.inspector` | `system_inspector` | system | — | Inspector | no | personal, cis | **live** | no |
| `system.journal` | `system_journal` | system | — | Journal | no | personal, cis | **live** | no |
| `system.journal_gate` | `system_journal_gate` | system | — | Journal Gate | no | personal | **live** | no |
| `system.judge_stats` | `system_judge_stats` | system | — | Judge Stats | no | personal | **live** | no |
| `system.layer_divergence` | `system_layer_divergence` | system | — | Layer Divergence | no | personal | **live** | no |
| `system.navigator` | `system_navigator` | system | — | Navigator | no | personal, cis | **live** | no |
| `system.optimizer` | `system_optimizer` | system | — | Optimizer Candidates | no | personal | **live** | no |
| `system.optimizer.diff` | `system_optimizer_diff` | system | — | Prompt-Module Diff | no | personal | **preview** | yes |
| `system.production_gauge` | `system_production_gauge` | system | — | Production Gauge | no | personal | **live** | no |
| `system.provenance` | `system_provenance` | system | — | Provenance | no | personal, cis | **live** | no |
| `system.read_scoreboard` | `system_read_scoreboard` | system | — | Read Scoreboard | no | personal | **live** | no |
| `system.report_export` | `system_report_export` | system | — | Report Export | no | personal, cis | **live** | no |
| `system.search` | `system_search` | system | — | Global Search | no | personal, cis | **preview** | no |
| `system.source_health` | `system_source_health` | system | — | Source Health | no | personal | **live** | no |
| `system.status` | `system_status` | system | — | System Status | no | personal | **live** | no |
| `system.stream_lag` | `system_stream_lag` | system | — | Consumer-Lag Monitor | no | personal | **live** | yes |
| `system.timeline` | `system_timeline` | system | — | Timeline | no | personal, cis | **live** | no |
| `system.wall` | `system_wall` | system | — | The Wall | no | personal, cis | **live** | no |
| `system.wall_movers` | `system_wall_movers` | system | — | Movers Since Last Visit | no | personal, cis | **live** | yes |
| `v4.assessment` | `v4_assessment` | system | — | World Assessment | no | personal, cis | **live** | no |
| `v4.kpi` | `v4_kpi` | system | — | At a Glance | no | personal, cis | **live** | no |
| `v4.map` | `v4_map` | system | — | World Map | no | personal, cis | **live** | no |
| `v4.morning_read` | `v4_morning_read` | system | — | Morning Read | no | personal, cis | **live** | no |

<!-- END GENERATED PANEL TABLE -->

### 2.2 Why preview

| Panel | Why |
|---|---|
| `system.optimizer.diff` | The diff route is wired and the panel renders live data; the human-gated promote flow around it is still maturing. Also hidden — it is merged into a peer surface. |
| `system.search` | Client-only: there is no dedicated server-backed global search route behind it yet. |

### 2.3 Why hidden

Hidden panels are fully live code merged into a peer surface, or operator
depth that the default menu does not promote. They stay in the registry so an
existing layout referencing them by id still resolves.

| Panel | Why |
|---|---|
| `system.optimizer.diff` | Merged into the optimizer candidate queue. |
| `source.subscription_builder`, `source.subscription_policy`, `source.fanout` | Merged into the source detail surface. |
| `system.stream_lag` | Operator depth; the System Status panel carries the queue layer. |
| `system.wall_movers` | A quadrant of the Wall rather than aliased onto it, so a saved layout holding the tile keeps rendering the quadrant it asked for. |

---

## 3. Release gate

`scripts/release_gate.sh` stage 4 builds `legba-ui-build`, so a panel that
fails type-checking blocks the release. The tier flag lives in `registry.ts`
`def()` plus the `PREVIEW_KINDS` and `HIDDEN_KINDS` sets, and §2.1 is
regenerated from exactly those three sources with a drift test behind it. §1
and §2.2–2.3 are operator knowledge with no machine source: keep them current
by hand when a route changes state.
