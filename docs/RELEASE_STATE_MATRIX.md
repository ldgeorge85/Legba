<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Release-state matrix

Release-readiness aid (REVIEW §3.4/§3.9, release-engineering). Classifies
every operator-facing **route** and **UI panel** by its release state so a
reviewer can tell at a glance what is product-grade vs preview vs pending —
without reading every component. Owned by the release-engineering stream;
regenerate the panel table from `legba-ui-v3/src/panel-registry/registry.ts`
when panels are added or change tier.

**States**

| State | Meaning |
|---|---|
| **live** | Real backend wired end-to-end; product-grade. |
| **preview** | Registered + renders, but reads no live backend yet OR renders an honest "not yet wired / pending" state. Never fabricates data. |
| **hidden** | Built but intentionally not surfaced in default navigation (operator/diagnostic depth; reachable by id / record-jump, not promoted in the menu). |
| **stub** | **Disallowed in production** — see `docs/SEAMS.md`. No panel/route is in this state; listed here only to name the bar. |

> The `def()` panel registration **now carries a machine-readable `tier`
> flag** (`registry.ts` `def()` returns `tier: 'live'` by default; the
> `PREVIEW_KINDS` set promotes guarded-preview surfaces to `tier: 'preview'`
> in one place, and the `HIDDEN_KINDS` set flips `hidden = true` for the
> not-promoted panels). So the live/preview/hidden classification below is
> mirrored in code and no longer maintained purely by hand — regenerate this
> table from the three sources (`def()` default + `PREVIEW_KINDS` +
> `HIDDEN_KINDS`) when panels change tier. A `PanelChrome.tsx` badge that
> renders the `tier` flag remains the one cosmetic follow-up.

---

## 1. Registry / runtime API routes

> **Scope note.** This matrix classifies operator-facing **routes** and **UI
> panels** only. The release state of individual analyst *kinds* (and their
> data-quality maturity) is tracked elsewhere (`planning/` + `docs/SEAMS.md`);
> a kind's omission here is NOT a claim that it ships or that its output is
> product-grade — this doc is the routes+panels surface, nothing more.

The bearer-gated route surface is enumerated in `docs/RUNBOOK.md` §4.1. All
of those are **live** (real substrate reads/writes, fail-closed auth) with
these exceptions:

| Route | State | Note |
|---|---|---|
| `GET /api/v1/v3/optimizer/candidates/{id}/diff` | **live** | Wired (`v3_api.py:519`, snapshot-based, no dspy import) — returns the candidate prompt-module text + parent path as a `PromptModuleDiff`. The `OptimizerDiff` panel reads it and renders live data; the panel stays badged `preview` only because the human-gated promote flow around it is still maturing (see §2). |
| `/a2a/skills/*` (runtime, not registry) | **live, gated** | Mounted only when `LEGBA_A2A_ENABLED` + a trusted-key list are set; UNMOUNTED otherwise (fail-loud, not stubbed). See RUNBOOK §4.3. |
| `GET /api/v1/registry/healthz` | **live (readiness)** | Pings PG (`SELECT 1`) + NATS (client connected): 200 `{status:ok}` when both respond, 503 `{status:unavailable, checks:{…}}` naming the failing component otherwise — so the Docker HEALTHCHECK / Caddy upstream drops a half-dead process from rotation. Upgraded from the old liveness stub under resilience-observability (W-1b §4). |
| `GET /metrics` (registry) | **live (unauthenticated by design)** | Prometheus text exposition (`metrics_api.py`, app-level no-prefix mount in `server.py`). Like `/healthz` it is intentionally NOT bearer-gated so a scraper can poll without a token; values are real registry counters/gauges. Companion alert rules ship in `deploy/prometheus/legba_alerts.yml`. Operator setup: RUNBOOK §4.7. |

Everything else in RUNBOOK §4.1 (findings / situations / signals / lineage /
targets / analysts / budget / source-credibility / events WS) is **live**.

### 1.1 Analyst-layer capabilities (not a route/panel, tracked here for release)

These are runtime analyst capabilities, not operator-facing routes/panels, but
they are release-relevant enough to classify alongside the surface above. (The
authoritative per-kind maturity tracking still lives in `planning/` +
`docs/SEAMS.md` — this is a release-readiness summary, not the catalog.)

| Capability | State | Note |
|---|---|---|
| Analyst knowledge-grounding (Tier 1, structured) | **live, off-by-default** | The `grounding` descriptor block (`GroundingBlock`, `enabled: false` default) opts an analyst into a deps-builder step that, before the LLM call, reads CURRENT authoritative `facts`/`nexuses` (temporal-honesty gate, curated/seed-preferred) for the target geo + slice entities and PREPENDS a dated "AUTHORITATIVE CURRENT CONTEXT" preamble. Wired + opted IN on `analyst_world_assessor` + `analyst_country_assessor` (`grounding.enabled: true`); live-verified injecting the current US head of state into a US assessment. Token-capped (`max_facts`), degrade-not-drop (a read miss → no preamble, never fabricates). **Tier 2** (the `vector:world_context` collection) is a declared SEAM (#20, needs the L-114 embedder-through-port). |
| `proposed_edge_governance` analyst | **live** | Promotes pending `proposed_edges` into neutral `CoOccursWith` nexuses (`descriptors/analyst_proposed_edge_governance.yaml`). |
| Phase-D graph-metrics legs | **live** | `structural_balance` / `graph_mining` / `nexus_decay` now WRITE rows via `_graph_metrics_sink.py` (previously inert). |
| ACH outcome-resolution + calibration | **live (self-consistency-flagged)** | `competing_hypotheses` status-transitions resolve to `resolved_outcome`; `calibration_tracking` computes a Brier. **Caveat:** absent an exogenous outcome the Brier is a SELF-CONSISTENCY measure, tagged `brier_self_consistency_only` / `self_consistency_only=true` — NOT calibration against reality. The exogenous-outcome seam is preserved. |
| `signals.source_credibility` at ingest | **live (legacy backlog)** | Now populated at ingest by a host-lookup in `source_actor.lookup_source_credibility` (the column was 100% NULL because the pipeline FILTER only ran when a descriptor bound the `source_credibility` kind, which the live descriptors don't). **Caveat:** this fixes NEW signals; pre-fix rows stay NULL until an optional backfill runs. |

---

## 2. UI panels (`panel-registry/registry.ts`)

### Live — real backend, product-grade

* Target room: `target.overview`, `.signals`, `.findings`, `.situations`,
  `.sources`, `.map`, `.graph`, `.timeline`, `.claims`
* Analyst room: `analyst.runs`, `.outputs`, `.cross_target`, `.critiques`
* Registry editors (operator): `registry.targets`, `.analysts`, `.stack`,
  `.action_packs`, `.sources`,
  `source.detail`, `.subscription_builder`, `.subscription_policy`,
  `.fanout`
* **Live Feed:** `system.findings` is the single unified Live Feed (merged
  findings + signals — `/findings` + `/signals`, two NATS tails
  `analyst.*.finding` + `legba.signals.>`, three controls: Live on/off +
  Source All/Findings/Signals + Cluster). It subsumes and replaces the former
  `v4.feed` rail (the only panel genuinely **deleted**, not hidden).
* System read panels: `system.lineage`, `.budget`,
  `.eval_scorecard`, `.optimizer` (candidate queue),
  `.dead_letter`, `.actor_health`, `.stream_lag`,
  `.governor`, `.audit`, `.consult`, `.deep_consult`,
  `.entities`, `.entity_graph`, `.inspector`
* v4 rooms: `v4.map`, `v4.flow`, `v4.why`

### Preview — registered, honest pending / client-only state

| Panel | Why preview |
|---|---|
| `system.optimizer.diff` (`OptimizerDiff`) | Backend `GET /v3/optimizer/candidates/{id}/diff` is **now wired** (`v3_api.py:519`, snapshot-based, no dspy import); kept badged `preview` only because the human-gated promote flow around it is still maturing. The diff itself renders live data. |
| `system.backfill` (`Backfill`) | Honest pending UI; the replay button is gated/disabled rather than wired to a destructive backfill. Should be **disabled-not-exposed** in a product build. |
| `system.search` (`Search`) | Client-only today (no server-backed global search index wired). |
| `system.alert_center` (`AlertCenter`) | Client-only alert view; `alert.emit` + `alert_sink_deliveries` are wired backend-side, but this panel does not yet read them. |
| `system.report_export` (`ReportExport`) | Client-only export composer; no server report-render route. |
| `system.tenant_view` (`TenantView`) | Client-only owner-grouping convenience; Legba ships single-tenant (`docs/DIRECTION.md` §0) — it surfaces the descriptor-`owner` rollup, it does NOT enforce any tenant isolation boundary. **Now also in `HIDDEN_KINDS`** (#90 Wave A) so it is not surfaced in default nav. |

### Hidden — built + registered, not surfaced in default nav

These are the `HIDDEN_KINDS` set in `registry.ts`: the `def()` registration
sets `hidden = true`, so the panel stays in `PANEL_REGISTRY` (existing layouts
that reference it by id still resolve — it is **present-but-hidden, NOT
file-deleted**) but is dropped from the sidebar / singleton list. They remain
fully **live** code; "hidden" is a navigation-tier flag, not a build state. The
one genuinely deleted panel is `v4.feed` (subsumed by the `system.findings`
Live Feed above) — it is not in this set.

The set spans two waves:

* **§6 redesign DROP set:** `system.pulse` (its Pulse view is reused wholesale
  as the Live Feed's Pulse mode), `system.eval`, `system.users`,
  `system.streams`, `registry.wirings`, `registry.mutations`,
  `dashboard.dynamic`.
* **#90 Wave A consolidation:** `registry.discovery` (no descriptor carries a
  `discovery` block), `system.backfill` (honest-501 stub; also in
  `PREVIEW_KINDS`), `system.targets.roster` (collapsed into the Target
  Registry), `v4.case` (Casework board shelved), `system.tenant_view`
  (single-tenant), `v4.assessment` (World Assessment is a FINDING, shown in the
  Inspector), `system.runtime` (Runtime Actor Health deduped against
  `system.actor_health`).

The deeper operator/diagnostic panels NOT in `HIDDEN_KINDS` (dead-letter,
governor, audit-chain, stream-lag, etc.) remain registered and reachable by
record-jump / explicit add; the 2026-06 redesign demoted the 37-item menu but
kept them live.

---

## 3. Release-gate hook

`scripts/release_gate.sh` stage 4 builds `legba-ui-build` (the tsc gate), so
a panel that fails type-checking blocks the release. The `tier` flag now lives
in `def()` (+ the `PREVIEW_KINDS` / `HIDDEN_KINDS` sets), so the classification
is machine-readable in code — but there is still no automated check that THIS
matrix stays in sync with `registry.ts`; keep it current by hand against those
three sources when panels change tier.
