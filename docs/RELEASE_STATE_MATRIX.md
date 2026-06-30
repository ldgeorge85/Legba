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
| `GET /api/v1/journal` | **live** | Serves journal entries + consolidations from the dedicated `journal_entries` table (`journal_api.py`, `build_journal_router`, mounted in `server.py` at `/api/v1`). Off-chain — reads `journal_entries` directly, never the lineage catalog. Renders the per-claim binding (`claims` / `cited_substrate_refs`) the `system.journal` panel deep-links from (see §2). |
| `GET /api/v1/journal_proposals` | **live** | Lists/filters the human-gated review queue from `journal_proposals` (`journal_proposals_api.py`, `build_journal_proposals_router`, mounted at `/api/v1`). The journal SUGGESTS into this queue; a human DISPOSES. |
| `POST /api/v1/journal_proposals/{proposal_id}/accept` · `…/reject` | **live** | Accept/reject a queued proposal. Accept runs an idempotent per-kind apply worker; reject requires a `decision_reason`. **Caveat:** the `correction` + `self_revision` apply paths are tested end-to-end; the `change`-apply path is import-verified but NOT yet exercised against a live registry. |
| `GET /api/v1/v3/system/analyst-cadence` | **live** | Per-analyst cadence health (`v3_api.py`, `build_v3_router`, mounted at `/api/v1/v3`): last run, age, runs 1h/24h, last outcome, and a healthy/stale/silent status — read from `analyst_traces` (the actual run record), NOT `actor_state.last_run_at`, which is NULL. Powers the System Status panel's Analysis layer (§2). |
| `GET /api/v1/v3/system/source-firing` | **live** | Per-source firing matrix (`v3_api.py`, mounted at `/api/v1/v3`): signals 24h/7d, last-seen age, last poll outcome, recent error count, and a firing/silent/error/paused status per source. Powers the System Status panel's Acquisition layer (§2). |
| `GET /api/v1/contention` | **live** | Read-only contested-claim groups + their per-value support clusters (Holes-B Wave 5, #101 — `substrate_reads_api.py`, `build_substrate_reads_router`, mounted in registry `server.py` at `/api/v1`). Plain SELECTs over the deployed `fact_contention` / `fact_contention_values` sidecar (migration 0055) — it NEVER mutates a fact, a group, or a marker. Filters `status` (defaults to LIVE disputes `contested`+`surfaced`), `subject` (lower-cased `subject_key`), `fact_id` (single group via `facts.contention_id`), `include_junk`, `since`. Backs the `ContestedBadge` UI surface (§2) and the consult read surface. The sidecar is populated by the `fact_contention_arbiter` analyst (§1.1); whether disputes ACCUMULATE depends on the write-path coexistence flag `LEGBA_FACT_CONTENTION` (default **OFF** in code + docker-compose), so on a default build this route serves an empty/legacy set. Proven live (returns real groups on this instance). |

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
| `fact_contention_arbiter` analyst | **live (detect-only)** | The contested-claims referee (Holes-B Wave 2, #101 — `descriptors/analyst_fact_contention_arbiter.yaml`, `deterministic`-kind GLOBAL META analyst, hourly at `:37`, `TRACE_ONLY` in `deterministic.py`). DETECT-ONLY invariant (B15): it NEVER mutates a fact value / `valid_until` / `superseded_by` / `confidence` — it scans OPEN facts, fuzzy-clusters competing values (`provenance/value_clustering.py`: canonicalize-entity + normalized-Levenshtein, threshold 0.12 — Russia/Russian and Kyiv/Kiev merge, North/South Korea stay split), junk-gates via the existing fact-extractor gates, scores each value cluster `Q·C·R·F` (quorum, credibility-share, recency half-life, confidence), and surfaces at most ONE winner per `(subject, predicate)` or ABSTAINS on a near-tie (`MIN_SURFACE_SCORE` 0.15, `DOMINANCE_RATIO` 1.25). Its ONLY writes are the `fact_contention` / `fact_contention_values` sidecar + the three thin `facts` markers (`contested`, `contention_id`, `surfaced_winner`), all recomputable from open facts (migration 0055). **Optional Wave-2b LLM tie-break** runs ONLY on a near-tie abstain, on the SELF-HOSTED vLLM plane (the deps-builder hard-refuses an Anthropic/Opus primary), bounded (256 tokens, 30s, ≤10 calls/pass), degrades to abstain on any failure — flag `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` (default **OFF** in code + docker-compose). Detect-only arbiter proven live; the vLLM tie-break is proven CONSULTED live (it abstained on symmetric evidence — correct, provenance-first), but a successful LLM PICK is unobserved-live so far. Whether disputes COEXIST for it to group depends on the write-path flag `LEGBA_FACT_CONTENTION` (default **OFF**); both flags are enabled (`=1`) only on this instance via the gitignored `.env`. |
| Phase-D graph-metrics legs | **live** | `structural_balance` / `graph_mining` / `nexus_decay` now WRITE rows via `_graph_metrics_sink.py` (previously inert). |
| ACH outcome-resolution + calibration | **live (self-consistency-flagged)** | `competing_hypotheses` status-transitions resolve to `resolved_outcome`; `calibration_tracking` computes a Brier. **Caveat:** absent an exogenous outcome the Brier is a SELF-CONSISTENCY measure, tagged `brier_self_consistency_only` / `self_consistency_only=true` — NOT calibration against reality. The exogenous-outcome seam is preserved. |
| `signals.source_credibility` at ingest | **live (legacy backlog)** | Now populated at ingest by a host-lookup in `source_actor.lookup_source_credibility` (the column was 100% NULL because the pipeline FILTER only ran when a descriptor bound the `source_credibility` kind, which the live descriptors don't). **Caveat:** this fixes NEW signals; pre-fix rows stay NULL until an optional backfill runs. |
| Journal assessor (first-person reflective voice) | **live** | The ONE analyst pointed at the whole organism (its own self / state / flow) — a perspective OVER the rest of the flow, not another slice of it. Emits the 11th OutputKind `journal` into the dedicated `journal_entries` table (migration 0048), fully OFF the fact/finding/nexus chain (always-empty `derived_from`, excluded from the lineage catalog — it NEVER writes a fact/finding/nexus). `journal_assessor` is an EXTENSION analyst kind (registered via the vocabulary family, not a member of the closed built-in `AnalystKind` enum). Two descriptors, one kind: an ENTRY tier (`analyst_journal_assessor`, every 12h) and a CONSOLIDATION tier (`analyst_journal_consolidator`, daily 02:00 UTC, which distils prior consolidation + recent entries into one forward-carried narrative and fires `supersede_prior_consolidation`). Runs as a single GLOBAL meta run per tick (`target_filter=None`). **Per-phase LLM split:** the agentic GATHER investigation loop runs on the core OpenAI-compatible plane (`llm.primary.openai_compat`); the VOICE (in-voice field-notes + NARRATE synthesis) runs on the Anthropic plane (Opus 4.8, `llm.anthropic.opus_4_7`) — so Anthropic spend is only the bounded final voice synthesis. Grant-locked to two non-write-fact packs (`journal_read` incl. 9 self-instruments, `journal_propose`). Live-validated (a real off-chain entry, honesty_flags forced from substrate metrics, receipt-chained, in-voice). **Future:** a critic + optimizer over the journal's own voice (Wave 5) is designed-not-built, gated on first building a critic actuator. |
| Journal propose-and-gate queue | **live (human-gated)** | Everything the journal wants to affect outward — a `correction`, a `change`, or a `self_revision` (including edits to its OWN instructions via `propose_self_revision`; protected sections auto-reject) — goes to the human-gated `journal_proposals` queue, NEVER a live table. A human accepts/rejects (routes in §1); accept runs an idempotent per-kind apply worker. The journal's only un-gated effect is its OWN continuity (it reads its last entry + current consolidation into its next run). **Caveat:** the `correction` + `self_revision` apply paths are tested end-to-end; the `change`-apply path is import-verified but NOT yet exercised against a live registry. |

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
  `.entities`, `.entity_graph`, `.inspector`,
  `.journal` (`system.journal`, "Journal" — renders journal entries off
  `GET /api/v1/journal` with provenance chips that deep-link to the cited
  record and `[needs_citation]` / perspective spans in a distinct style;
  `tier: 'live'`, not in `PREVIEW_KINDS`/`HIDDEN_KINDS`. **Note:** tsc-green +
  fully wired but pending its first real in-browser render at the time of
  writing.)
* **System Status** (`system.status`, "System Status" — the per-component /
  per-layer health view that the operator repeatedly asked for: composes
  **Acquisition** (per-source firing matrix off
  `GET /api/v1/v3/system/source-firing`), **Analysis** (per-analyst cadence
  health off `GET /api/v1/v3/system/analyst-cadence`, read from `analyst_traces`
  because `actor_state.last_run_at` is NULL — the gap the existing Actor Health
  panel could not fill), **Queues** (consumer backpressure off the
  orphan-filtered `GET /api/v1/v3/streams/consumer_lag`), and **Infra** into one
  page; `tier: 'live'`, not in `PREVIEW_KINDS`/`HIDDEN_KINDS`. **Note:** tsc-green
  with both new routes confirmed serving live data, but pending its first real
  in-browser render at the time of writing.)
* v4 rooms: `v4.map`, `v4.flow`, `v4.why`
* **`ContestedBadge`** (Holes-B Wave 5, #101 — `v4/components/ContestedBadge.tsx`):
  not a standalone registered panel but a self-contained **component** mounted
  into two existing live panels. It reads `GET /api/v1/contention` (§1) through
  the pure, unit-tested `@/lib/contentionModel`, renders NOTHING when the claim
  is not contested (the common case → zero visual noise), and is read-only (it
  never mutates a fact, group, or marker; a 5xx lookup failure shows a subtle
  affordance rather than masking as "uncontested"). Two mount points:
  **(1) `v4.why` ProvenanceTrail** (`ProvenanceTrail.tsx`) — fact-keyed
  (`<ContestedBadge factId={…} />`, precise `?fact_id=` lookup → 0/1 group) on a
  lineage node whose `row_kind === 'fact'`; **(2) `target.claims`** (`Claims.tsx`)
  — subject-keyed (`<ContestedBadge subject={claim.statement} />`, `?subject=`
  → 0..N groups, surfaces the first LIVE one) since findings carry no real
  `facts.id`. When contested it shows a "Contested" badge ("Contested — no
  winner" on a near-tie abstain) plus a per-value support panel (distinct-source
  count, credibility-share, arbiter score, surfaced-winner flag). **live** code,
  but the underlying disputes only ACCUMULATE when the write-path flag
  `LEGBA_FACT_CONTENTION` (default **OFF**) is set — so on a default build both
  mount points render no badge.

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
