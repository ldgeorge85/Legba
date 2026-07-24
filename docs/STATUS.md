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
through enrichment and fan-out; the seven bounded units write `[N]`-cited
findings that pass the mandatory faithfulness verify; the composition tower
(`country_composition` → `region_composition` → `world_assessor`, plus the
thematic `escalation_composition`) composes the **verified** sub-claims into
hedged per-country, per-region, and world reads; `scorecard_producer` writes
one banded row per active desk. Cold-start from empty volumes through to a
verified scorecard is proven from a single baseline schema migration.

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
- The `world_context` vector RAG is a **guarded, measured pilot** on a single
  unit (`internal_stability`) — firing it has historically thickened the
  low-faithfulness tail, so a per-run auto-rollback guard reverts injection the
  moment that recurs; `leadership_transition` RAG is rolled back and OFF.

**No enterprise, multi-tenant, RBAC, or forecast-accuracy / Brier-skill claim
is made.**

## Release boundary

| Capability | State | Notes |
|---|---|---|
| Source-first acquisition + predicate fan-out | **built** | ~57-source catalog (~50 live/active sources incl. seed/baseline; added state-media feeds IRNA / PressTV / Ukrinform + UCDP GED); a `source_class` taxonomy (`reporting` / `analysis` / `official` / `state_media`) tags each source, and state / social outlets are seeded a below-nominal `source_credibility` (PressTV 0.25, IRNA 0.30, Ukrinform 0.45, Telegram 0.30 — all under the 0.5 ingestion nominal) so they no longer out-credit their peers; poll + push acquisition. The Telegram source is **active** (re-authed; gap recovery is a bounded catch-up, never an unbounded replay); `voa.africa` is **retired** (its RSS layer serves a frozen snapshot behind a live-looking `lastBuildDate`); the GDELT file-dump lane polls on a 15-min cycle; `source.ucdp.ged` is **paused pending an access token**. `stream` mode is a guarded enum seam. |
| Baseline enrichment (language / geo / GLiREL NER + relation extraction) | **built** | Hosted out-of-process; relation backend is **GLiREL** (`jackboyla/glirel-large-v0`). Telegram bodies are now NER'd, and non-Latin scripts (Arabic / Russian / Ukrainian) are translated to English via the hosted NLLB `/translate` endpoint before extraction. The historical backlog has been drained: a `reenrich_ner` batch re-enriched the ~10k older telegram / non-Latin signals in place (idempotent marker per signal), so the forward pipeline and the archive now share the same enrichment floor. |
| Seven bounded reasoning units → cited findings | **built** | `leadership_transition` / `energy_security` / `escalation` / `narrative_coordination` / `internal_stability` / `military_posture` / `economic_coercion`, each fanned out across the 25 country desks (19 G20 + a 6-country watch tier) via `has_tag("g20") or has_tag("watch")`; reactive + cadence firing; `[N]`-cited strict-JSON findings. |
| Mandatory faithfulness verify pass | **built** | Faithfulness judge (currently the same core gpt-oss-120b model, **not** cross-family — temporary; known limitation) + deterministic citation-presence floor; `effective_confidence = min(confidence, faithfulness_score)`; gates a visible low-confidence tier, never hard-deletes. Measures groundedness, not truth. |
| Composition tower (per-country → per-region → world) | **built** | `country_composition` → `region_composition` (5 region frames: Africa, Americas, Europe, Indo-Pacific, MENA) → `world_assessor`, plus the thematic cross-desk `escalation_composition` (carries a correlation guard against double-counting correlated desks) — all `meta_findings_synthesizer`; read slice INNER-JOINs the faithfulness critique — unverified sub-claims never enter; empty-slice yields an explicit no-read, not an invention. One live head per desk (supersession). |
| Deterministic I&W (indicators + collection gaps) | **built** | `indicator_tracker` (run-over-run diffs on the structured indicators the units emit) + `collection_gap` (starved desk × dimension cells) — both `deterministic` META, no LLM. |
| Compose-time freshness + honesty instruments | **built** | Three read-time honesty surfaces on the composition tower: (1) a **freshness advisory** — at compose time every input's transitive lineage is re-resolved to its *current* head, and a materially-reversed sub-finding (\|Δconf\| ≥ 0.25 after the citing tier composed) is surfaced as a prompt directive + stamped `data.freshness` (advisory-only, never a gate); (2) **denominator-honest source health** — `get_source_health` reports the whole-fleet summary (total wired / by state / active fresh-stalled-erroring) plus the named paused/retired feeds, so "N of M active fresh, of K wired" replaces the flattering half; (3) **scorecard↔composition disagreements** — the journal's assessment read reconciles each country's banded scorecard against its live composition head and surfaces every finding one product excluded that the other cites (bounded, fail-safe, read-time only). |
| Per-signal salience (consequence scoring + consumption) | **built** | An hourly `signal_salience` sweep scores each raw text signal `{event_class, actor_rank, magnitude}` over a closed taxonomy on the core plane, with `authority` stamped deterministically from the source's `source_class` (never model-chosen); strict echo-bound parse (an unbindable row degrades to unscored, never a wrong-signal score); idempotent marker drains the pool. **Consumption is live:** the journal's priming slice is salience-ordered (a stable sort on the salience key with a fresh-signal floor, so the per-source diversity order survives and a not-yet-scored breaking signal is never truncated out); composition inputs sort salience-first; findings carry `data.salience` = the max-magnitude input (the leaf signal's identity propagates up the tower); and an **advisory `salience_check`** on every composition records whether the lead opened on its top-consequence input (never gates). |
| Pipeline liveness watchdog (stall detection) | **built (durable global-stall alert)** | Observes signal + finding traffic and per-analyst / per-source cadences; a global pipeline stall now also persists a **durable** `alert_sink_deliveries` row (surfaced on the escalations panel + counted by the delivery-failure canary), not only the streamless NATS alert subject. Per-analyst / per-source cadence alerts still publish to the streamless subject only. A **host-side auto-recovery actuator** (root cron, every 5 min) now checks the freshest signal's age straight from Postgres and, past a 45-minute threshold, executes the proven recovery (restart sidecar, then runtime) behind a safety ladder — maintenance flag, deploy-in-progress skip, warmup grace, query-must-succeed, and a 90-minute cooldown that escalates instead of loop-restarting; each recovery lands a durable `auto_recovered` row. See the runbook's host-stall-watchdog section. The one observed global-stall class was root-caused live (py-spy under a pre-armed catch): a synchronous CPU-bound graph-mining enumeration pinning the event loop. It is now bounded (path cutoff + enumeration cap), runs off-loop in an executor, and a wall-clock budget abandons overruns with an honest `ABANDONED` finding instead of a freeze; the watchdog's runtime-first restart order was validated under fire. Boot-time client singletons (qdrant / embedder / substrate) lazily re-resolve, so a deploy-order race can no longer leave consult or the embedder silently dead. |
| Journal verify profile (chronicle gate) | **built** | Every journal entry is faithfulness-verified post-persist through the same deterministic floor + judge as findings: the entry's **cited fact claims** are judged against their resolved substrate rows (all citable ref kinds — findings, signals, situations, facts, nexuses, hypotheses); **perspective claims are exempt by construction** (the entry prose is never mutated); the verdict lands as a standard critique row keyed to the entry id. An unresolved (possibly fabricated) ref can never pass the deterministic floor. |
| Post-verify alert gate (severity as a read column) | **built (bus-only delivery)** | Severity is a first-class read column (not a tag); alerts key on post-verify `effective_confidence × severity` (a verify-demoted finding does not alert); `escalate_finding` pack is the delivery edge. External delivery currently lands on the NATS subject `channels.escalations` only (bus-only) — no paged-human integration is claimed. |
| Opportunistic vector RAG (grounding, not citable) | **built; `world_context` is a guarded, measured pilot** | Two live Qdrant collections — `tradecraft` (~1716 chunks) and `world_context` (~293 chunks, re-embedded in place with a `<Country> — <section>` contextualization lead), bge-m3 1024-dim via the stack embedder port; inline retrieval now uses a focused `<country> <theme>` query + a relevance floor lowered to **0.55** + country filter, degrade-not-drop when the corpus is empty; injected priors stay **non-citable** (fenced background, no `[N]` ids). `tradecraft` is stable; `world_context` is ON (tuned config) for **`leadership_transition`, `internal_stability`, and `energy_security`** — the first two measured over ~11 days with no rollback-guard trip and an in-lane faithfulness gain slightly above the no-RAG drift band; `energy_security` was added as the differential read; **`escalation` is the deliberate no-RAG control**. A **real** per-run auto-rollback guard (`rag_rollback.py`, re-checked every run — no restart needed) suppresses injection on a faithfulness drop / low-faith ratio / ≥35% token-cost rise, and per-run trace records `world_context_top_score` / retained / `min_score`. **Known limit:** firing RAG has historically thickened the low-faithfulness tail; the guard reverts if it recurs, and the pilot state file currently lives at an ephemeral path. |
| Banded per-country scorecard | **built** | `scorecard_producer` (deterministic META; 12th OutputKind `scorecard`); one row per active country desk (any target tagged `g20`/`watch`); every band names its verified-claim basis; no-basis dimensions read `insufficient-evidence`; sub-floor faithfulness demotes to `low-faithfulness`. |
| Skill scoreboard (faithfulness + correctness-vs-reference) | **built, honest-null** | Per-unit eval + exogenous calibration Brier + acute-forecast BSS. Correctness-vs-reference gold set is **tiny (n=1, reported insufficient-sample)**; a no-skill result is published, not hidden. |
| Measured GEPA self-optimizer (`unit_optimizer`) | **built (experimental)** | Scoped to one unit; every candidate carries a real paired faithfulness delta on the same faithfulness judge (currently the core model); `human_gated`, **never auto-promotes** on a degenerate / insufficient / non-positive delta (live example: `0.34 → 0.29`). Monolithic `country_optimizer` is cadence-frozen. |
| Acute-forecast pilot (Brier / BSS) | **built, no proven skill** | `forecast_scoreboard` (deterministic META) issues one pre-registered weekly binary call per G20 country, resolved **exogenously** by upstream event time, scored by **BSS vs per-country climatology**. Segregated key (`brier_forecast_acute`), never pooled into the headline calibration Brier; abstains (zero rows) on a degenerate / geography-dominated vector; surfaced **only** on the calibration scoreboard, never as a claim. Earns the word "forecast" only when BSS > 0 on a non-degenerate sample — **not today**. |
| Temporal facts (`valid_from` / `valid_until` / `superseded_by`) | **built** | Open-only partial unique index. |
| Contested-claims arbiter ("alternate facts") | **built** | Detect-only, flag-gated (#101). A deterministic `Q·C·R·F` arbiter keeps disputed `(subject, predicate)` values coexisting open and surfaces a credibility-weighted winner in a sidecar — it **never mutates a fact** and abstains on a near-tie. Write-path coexistence and the optional LLM near-tie tie-break ship **OFF by default**; read API `GET /api/v1/contention`. A successful LLM tie-break *pick* is not yet observed live. See [ANALYSIS.md](ANALYSIS.md) §7.11. |
| Reified typed `nexuses` + structural-balance / graph-mining | **built** | Canonical polarity sign + intent + temporal bounds + supersession; ~4.9k nexuses live (~3.2k signed, polarity≠0). |
| ACH competing hypotheses + calibration | **built (no skill claim)** | Per-cell consistency is LLM-scored (Heuer CC/C/N/I/II, budget-gated) with a deterministic lexical scorer as the budget-exhausted fallback. Calibration Brier reads an exogenous `resolved_outcome`; the `subsequent_facts` / operator-label resolvers are built and preferred but **have not yet fired live** — every resolved outcome to date is the self-consistency (`status_transition`) tier, flagged `self_consistency_only`. No proven-forecast-accuracy claim. See [ANALYSIS.md](ANALYSIS.md) §7.4–§7.5. |
| First-person reflective journal (off-chain voice) | **built (runs on cadence)** | 11th OutputKind (`journal`); dedicated `journal_entries` table — off the fact/finding/nexus chain (always-empty `derived_from`, excluded from lineage). `journal_assessor` (12h entry) + `journal_consolidator` (daily) run as an introspective voice (both GATHER and VOICE phases now run on the core plane); writes never reach product output. Routing reflections back via the human-gated `journal_proposals` queue is a future item. |
| Faculty lenses + chorus diff (interpretive tier) | **built (weekly cadence)** | Four function-typed lens analysts on the journal kind — `lens_trend`, `lens_baserate`, `lens_capability`, `lens_intent` — each carries **one declared falsifiable prior** and writes a weekly interpretive read (`entry_kind='lens'`) over the verified tower top: no new fact asserted, no publish edge, same post-persist faithfulness verify as the journal. A fifth id, `lens_diff`, runs after the four and narrates where the faculty reads agree / split / outlie — it never merges them into a consensus voice. All five run on the $0 core plane, staggered Monday mornings UTC. The `/journal` default stream carries all append tiers (`entry` / `chronicle` / `lens` / `lens_diff`); consolidation stays slot-only. |
| Chronicle — the public-record tier | **built (weekly cadence)** | A third analyst id (`chronicle_assessor`) on the journal kind writes `entry_kind='chronicle'` rows: detached third-person long-form prose over the verified tower top (assessments, watch-desk findings, contentions, corpus documents), every factual assertion cited inline, disputed accounts attributed side by side, no self/apparatus reference. Weekly (Mondays 06:00 UTC), gated by the same post-persist faithfulness verify as the journal; entries appear in the `/journal` stream. **No publish edge exists** — entries accumulate internally; an operator-gated publish sink is a future item. |
| On-demand consult (chat + deep) + MCP `legba_consult` tool | **built** | `POST /api/v1/consult`; each request picks its plane — **Opus** (Anthropic Opus 4.8, billed, the default when none is chosen) or **Core** (the free self-hosted core plane). A server-side allowlist maps the friendly value → component id (the client never names a component). **Fail-closed:** a chosen non-default plane that can't be honored raises rather than silently billing the default; a provider outage surfaces as a graceful 503 naming the other plane. The shared per-day consult token cap binds on both. Consult / deep-consult are now the **only** features that use the Anthropic plane. |
| STIX 2.1 bundle producer (NATS + file sinks) | **built** | Real + e2e-proven. |
| Curated seeding (`world_baseline`) | **built** | `seed_batches` ledger; `world_baseline` + `wikidata_leaders` + `acled_conflict` + `sipri_arms_transfers` adapters live; the UCDP GED adapter (`source.ucdp.ged`) is built but currently **paused pending an access token**; World Bank designed. |
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
  STOPPED.** The seven units + composition supersede it; nothing in the spine
  reads it. Its ~1.2k historical findings remain in the DB, unread — not
  deleted, not a clean slate.
- **`country_predictor`, `india_energy_predictor` (forecast-as-claim) —
  RETIRED / STOPPED.** Forecasting returns only as the measured
  `acute_forecasts` Brier/BSS scoreboard, never a free-text claim; ~539
  historical prediction rows remain in the DB.
- **`country_optimizer` (monolithic GEPA) — cadence-FROZEN** (descriptor still
  `state=active`). GEPA returns only as the scoped, faithfulness-measured
  `unit_optimizer`.
- **`journal_assessor` — RUNS (on cadence).** An off-chain voice roster that
  writes only `journal_entries`, never product output: the 12-hour entry +
  daily consolidator, the weekly third-person chronicle, four weekly faculty
  lenses (one falsifiable prior each), and the weekly `lens_diff` chorus pass.
- **`world_assessor` — NOT retired.** It graduated into the world composition
  (repointed onto `meta_findings_synthesizer`).

## Significant not-built seams (plain-language summary)

Media extraction models (the `process_media` job plane is real end-to-end, but
with no extraction endpoint configured it refuses loudly rather than producing
anything); non-text UI renderers (badged placeholders); the inbound webhook
push half (accept-and-enqueue + durable drain are built, dormant until a live
webhook source is wired); `stream` acquisition (poll and push are live). The
full registry with guard rails and rationale: [SEAMS.md](SEAMS.md).
