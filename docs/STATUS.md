<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Status — the truth-in-labeling page

What is real, what is gated, what is only designed — on one page. A capability
is either **built** (runs end-to-end today), a **guarded seam** (the surface
exists but refuses activation / raises loudly until its real edge is wired —
never a silent stub), or **designed, not built** (the design is written in
[DIRECTION.md](DIRECTION.md); no code claims it). The authoritative seam
registry is [SEAMS.md](SEAMS.md); per-route/per-panel maturity is in
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md).

## Where it is weak today (read this first)

Live and source-first — **single-operator / single-tenant**, single-node,
run-it-yourself. The analysis spine runs end-to-end: catalog sources flow
through enrichment and fan-out; the four bounded units write `[N]`-cited
findings that pass the mandatory faithfulness verify; `country_composition`
and `world_assessor` compose the **verified** sub-claims into hedged
per-country and world reads; `scorecard_producer` writes one banded row per
active desk. Cold-start from empty volumes through to a verified scorecard is
proven from a single baseline schema migration.

And, plainly:

- The scorecard is a **mix** — some desks band from verified claims, others
  read all-`insufficient-evidence` (e.g. a desk whose unit faithfulness is
  genuinely low); the scorecard refuses to fabricate a band.
- The correctness-vs-reference gold set is **tiny** (n=1, reported
  insufficient-sample).
- The acute-forecast pilot reports **no proven skill** and abstains on
  degenerate windows.
- The GEPA `unit_optimizer` measures a real faithfulness delta that is not yet
  positive (a recent cycle: `0.34 → 0.29`), so it promotes nothing.
- The ACH / calibration layer is built and traceable but carries **no
  validated skill metric** — every resolved outcome to date is the
  self-consistency tier, not an exogenous real-world resolution.
- The faithfulness judge currently runs on the **same core model** that writes
  the analysis (documented, temporary — a dedicated cross-family judge is
  planned); the deterministic citation floor and the receipt chain backstop it.

**No enterprise, multi-tenant, RBAC, or forecast-accuracy / Brier-skill claim
is made.**

## Release boundary

| Capability | State | Notes |
|---|---|---|
| Source-first acquisition + predicate fan-out | **built** | ~46-source catalog (~50 live sources incl. seed/baseline); poll + push acquisition. `stream` mode is a guarded enum seam. |
| Baseline enrichment (language / geo / GLiREL NER + relation extraction) | **built** | Hosted out-of-process; relation backend is **GLiREL** (`jackboyla/glirel-large-v0`). |
| Four bounded reasoning units → cited findings | **built** | `leadership_transition` / `energy_security` / `escalation` / `narrative_coordination`, each fanned out across the 24 country desks (19 G20 + a 5-country watch tier) via `has_tag("g20") or has_tag("watch")`; reactive + cadence firing; `[N]`-cited strict-JSON findings. |
| Mandatory faithfulness verify pass | **built** | Faithfulness judge (currently the same core gpt-oss-120b model, **not** cross-family — temporary; known limitation) + deterministic citation-presence floor; `effective_confidence = min(confidence, faithfulness_score)`; gates a visible low-confidence tier, never hard-deletes. Measures groundedness, not truth. |
| Per-country + world composition | **built** | `country_composition` and `world_assessor` (both `meta_findings_synthesizer`); read slice INNER-JOINs the faithfulness critique — unverified sub-claims never enter; empty-slice yields an explicit no-read, not an invention. |
| Banded per-country scorecard | **built** | `scorecard_producer` (deterministic META; 12th OutputKind `scorecard`); one row per active country desk (any target tagged `g20`/`watch`); every band names its verified-claim basis; no-basis dimensions read `insufficient-evidence`; sub-floor faithfulness demotes to `low-faithfulness`. |
| Skill scoreboard (faithfulness + correctness-vs-reference) | **built, honest-null** | Per-unit eval + exogenous calibration Brier + acute-forecast BSS. Correctness-vs-reference gold set is **tiny (n=1, reported insufficient-sample)**; a no-skill result is published, not hidden. |
| Measured GEPA self-optimizer (`unit_optimizer`) | **built (experimental)** | Scoped to one unit; every candidate carries a real paired faithfulness delta on the same faithfulness judge (currently the core model); `human_gated`, **never auto-promotes** on a degenerate / insufficient / non-positive delta (live example: `0.34 → 0.29`). Monolithic `country_optimizer` is cadence-frozen. |
| Acute-forecast pilot (Brier / BSS) | **built, no proven skill** | `forecast_scoreboard` (deterministic META) issues one pre-registered weekly binary call per G20 country, resolved **exogenously** by upstream event time, scored by **BSS vs per-country climatology**. Segregated key (`brier_forecast_acute`), never pooled into the headline calibration Brier; abstains (zero rows) on a degenerate / geography-dominated vector; surfaced **only** on the calibration scoreboard, never as a claim. Earns the word "forecast" only when BSS > 0 on a non-degenerate sample — **not today**. |
| Temporal facts (`valid_from` / `valid_until` / `superseded_by`) | **built** | Open-only partial unique index. |
| Contested-claims arbiter ("alternate facts") | **built** | Detect-only, flag-gated (#101). A deterministic `Q·C·R·F` arbiter keeps disputed `(subject, predicate)` values coexisting open and surfaces a credibility-weighted winner in a sidecar — it **never mutates a fact** and abstains on a near-tie. Write-path coexistence and the optional LLM near-tie tie-break ship **OFF by default**; read API `GET /api/v1/contention`. A successful LLM tie-break *pick* is not yet observed live. See [ANALYSIS.md](ANALYSIS.md) §7.11. |
| Reified typed `nexuses` + structural-balance / graph-mining | **built** | Canonical polarity sign + intent + temporal bounds + supersession; ~4.9k nexuses live (~3.2k signed, polarity≠0). |
| ACH competing hypotheses + calibration | **built (no skill claim)** | Per-cell consistency is LLM-scored (Heuer CC/C/N/I/II, budget-gated) with a deterministic lexical scorer as the budget-exhausted fallback. Calibration Brier reads an exogenous `resolved_outcome`; the `subsequent_facts` / operator-label resolvers are built and preferred but **have not yet fired live** — every resolved outcome to date is the self-consistency (`status_transition`) tier, flagged `self_consistency_only`. No proven-forecast-accuracy claim. See [ANALYSIS.md](ANALYSIS.md) §7.4–§7.5. |
| First-person reflective journal (off-chain voice) | **built (runs on cadence)** | 11th OutputKind (`journal`); dedicated `journal_entries` table — off the fact/finding/nexus chain (always-empty `derived_from`, excluded from lineage). `journal_assessor` (12h entry) + `journal_consolidator` (daily) run as an introspective voice; writes never reach product output. Routing reflections back via the human-gated `journal_proposals` queue is a future item. |
| On-demand consult (chat + deep) + MCP `legba_consult` tool | **built** | `POST /api/v1/consult`; consult + deep-consult run on Claude Opus 4.8 (billed, used sparingly). |
| STIX 2.1 bundle producer (NATS + file sinks) | **built** | Real + e2e-proven. |
| Curated seeding (`world_baseline`) | **built** | `seed_batches` ledger; `world_baseline` + `wikidata_leaders` + `acled_conflict` + `sipri_arms_transfers` adapters live (UCDP / World Bank designed). |
| Time-series metrics (observability) + BM25 search | **declared seam** | No metrics store; `search_signals` falls back to Postgres FTS (SEAMS #21). `anomaly_detection` is unaffected — it reads `time_bucket()` from the primary Postgres pool. |
| SeaweedFS object store | **guarded seam** | Schema-slotted stack kind; no live integration module (deferred). |
| Eager media extraction (Whisper / VLM / OCR) + non-text UI renderers | **guarded seam** | Job loop is built end-to-end and refuses loudly with no endpoint configured (SEAMS #1/#13). |
| TAXII 2.1 upload + webhook output | **guarded seam** | STIX producer is live; TAXII upload raises `TaxiiServerNotConfiguredError` until an operator-confirmed server exists (SEAMS #10). |
| A2A skill router | **guarded seam** | Wired to mount, **not mounted on the production runtime** — the gated-off route fails loud (`/a2a/skills` → 503, SEAMS #15); operator-gated by `LEGBA_A2A_ENABLED`. |
| RBAC / SSO, multi-tenant isolation, MCP surface expansion | **designed, not built** | **Legba ships single-tenant.** No enterprise / multi-tenant / RBAC claim is made. Design in [DIRECTION.md](DIRECTION.md) §1–§2. |
| Horizontal scale-out (multi-node) | **designed, not built** | Single-node today; the hot ingest/analysis path is replica-safe, but the runtime ships single-replica with a fail-loud guard. Design in [DIRECTION.md](DIRECTION.md) §6. |
| Deep-crawl discovery jobs | **designed, not built** | Source discovery runs via the registry route, not the job plane ([DIRECTION.md](DIRECTION.md) §8). |

## Retirements & freezes

Sequenced and documented in [SEAMS.md](SEAMS.md), so the trusted spine stays
clean:

- **`country_assessor` (monolithic per-country one-pager) — RETIRED /
  STOPPED.** The four units + composition supersede it; nothing in the spine
  reads it. Its ~1.2k historical findings remain in the DB, unread — not
  deleted, not a clean slate.
- **`country_predictor`, `india_energy_predictor` (forecast-as-claim) —
  RETIRED / STOPPED.** Forecasting returns only as the measured
  `acute_forecasts` Brier/BSS scoreboard, never a free-text claim; ~539
  historical prediction rows remain in the DB.
- **`country_optimizer` (monolithic GEPA) — cadence-FROZEN** (descriptor still
  `state=active`). GEPA returns only as the scoped, faithfulness-measured
  `unit_optimizer`.
- **`journal_assessor` — RUNS (on cadence).** An off-chain introspective voice
  (12-hour entry + daily consolidator) that writes only `journal_entries`,
  never product output.
- **`world_assessor` — NOT retired.** It graduated into the world composition
  (repointed onto `meta_findings_synthesizer`).

## Significant not-built seams (plain-language summary)

Media extraction models (the `process_media` job plane is real end-to-end, but
with no extraction endpoint configured it refuses loudly rather than producing
anything); non-text UI renderers (badged placeholders); the inbound webhook
push half (accept-and-enqueue + durable drain are built, dormant until a live
webhook source is wired); `stream` acquisition (poll and push are live). The
full registry with guard rails and rationale: [SEAMS.md](SEAMS.md).
