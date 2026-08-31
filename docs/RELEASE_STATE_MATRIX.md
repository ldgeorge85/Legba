<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Release-state matrix

Release-readiness aid (REVIEW §3.4/§3.9, release-engineering). Classifies
every operator-facing **route** and **UI panel** — plus the release-relevant
**analyst-layer capabilities** that make up the analysis spine — by its release
state so a reviewer can tell at a glance what is product-grade vs preview vs
pending, and where a capability is honestly weak today, without reading every
component. Owned by the release-engineering stream; the panel table in §2 is
machine-generated from `legba-ui-v3/src/panel-registry/registry.ts` by
`scripts/gen_release_state_matrix.py` — run it after adding a panel or
changing a panel's tier (`python3 scripts/gen_release_state_matrix.py`); a
drift test (`tests/data_pkg/test_release_state_matrix_current.py`) fails the
suite if the committed table falls out of sync with `registry.ts`.

**States**

| State | Meaning |
|---|---|
| **live** | Real backend wired end-to-end; product-grade. |
| **preview** | Registered + renders, but reads no live backend yet OR renders an honest "not yet wired / pending" state. Never fabricates data. |
| **hidden** | Built but intentionally not surfaced in default navigation (operator/diagnostic depth; reachable by id / record-jump, not promoted in the menu). |
| **frozen** | Built + registered, but its autonomous cadence is intentionally NULLED (`cadence.fallback_schedule: null`) so it fires on no tick — a SEQUENCED freeze documented in `docs/SEAMS.md`, not a failure. Restorable by a one-line cadence change. |
| **retired** | Live head lifecycle-state `retired` (+ removed from bringup) so the runtime wires no worker for it — a deliberate removal from the trusted spine, documented in `docs/SEAMS.md`. |
| **stub** | **Disallowed in production** — see `docs/SEAMS.md`. No panel/route is in this state; listed here only to name the bar. |

> The `def()` panel registration **carries a machine-readable `tier` flag**
> (`registry.ts` `def()` returns `tier: 'live'` by default; the `PREVIEW_KINDS`
> set promotes guarded-preview surfaces to `tier: 'preview'` in one place, and
> the `HIDDEN_KINDS` set flips `hidden = true` for the not-promoted panels). So
> the live/preview/hidden classification below is mirrored in code and no longer
> maintained purely by hand — regenerate this table from the three sources
> (`def()` default + `PREVIEW_KINDS` + `HIDDEN_KINDS`) when panels change tier.
> **A RETIRED panel is not in this table at all**: since
> UI_HOLISTIC_DESIGN_2026-08-24 §4.4 a retired kind leaves the registry and
> becomes a row in `panel-registry/aliases.ts`, which resolves its id onto the
> survivor that renders it. Twelve kinds left this table that way (67 → 55); the
> ids still resolve, they just no longer cost a registry row. A
> `PanelChrome.tsx` badge that renders the `tier` flag remains the one cosmetic
> follow-up. (The `frozen` / `retired` states apply to analyst capabilities in
> §1.1, not to UI panels — they live in descriptor cadence / lifecycle state, not
> in `registry.ts`.)

---

## 1. Registry / runtime API routes

> **Scope note.** This matrix classifies operator-facing **routes** and **UI
> panels** (§1, §2) plus the release-relevant **analyst-layer capabilities**
> that constitute the analysis spine (§1.1). The authoritative per-kind
> data-quality maturity tracking still lives in `planning/` + `docs/SEAMS.md`;
> a capability's omission here is NOT a claim that it ships or that its output is
> product-grade.

The bearer-gated route surface is enumerated in `docs/RUNBOOK.md` §4.1. All
of those are **live** (real substrate reads/writes, fail-closed auth) with
these exceptions:

| Route | State | Note |
|---|---|---|
| `GET /api/v1/v3/optimizer/candidates/{id}/diff` | **live** | Wired (`v3_api.py:880`, snapshot-based, no dspy import) — returns the candidate prompt-module text + parent path as a `PromptModuleDiff` for the SCOPED `unit_optimizer` (§1.1), not the frozen monolith. The `OptimizerDiff` panel reads it and renders live data; the panel stays badged `preview` only because the human-gated promote flow around it is still maturing (see §2). |
| `/a2a/skills/*` (runtime, not registry) | **live, gated** | Mounted only when `LEGBA_A2A_ENABLED` + a trusted-key list are set; UNMOUNTED otherwise (fail-loud, not stubbed). See RUNBOOK §4.3. |
| `GET /api/v1/registry/healthz` | **live (readiness)** | Pings PG (`SELECT 1`) + NATS (client connected): 200 `{status:ok}` when both respond, 503 `{status:unavailable, checks:{…}}` naming the failing component otherwise — so the Docker HEALTHCHECK / Caddy upstream drops a half-dead process from rotation. Upgraded from the old liveness stub under resilience-observability (W-1b §4). |
| `GET /metrics` (registry) | **live (unauthenticated by design)** | Prometheus text exposition (`metrics_api.py`, app-level no-prefix mount in `server.py`). Like `/healthz` it is intentionally NOT bearer-gated so a scraper can poll without a token; values are real registry counters/gauges. Companion alert rules ship in `deploy/prometheus/legba_alerts.yml`. Operator setup: RUNBOOK §4.7. |
| `GET /api/v1/lineage/{row_kind}/{row_id}` | **live** | Walks the receipt chain hop by hop (`lineage_api.py`, `build_lineage_router`, mounted at `/api/v1/lineage`) — e.g. `…/lineage/finding/{id}` resolves finding → unit → cited signal to the real source URL. Each node carries a SHA-256 `receipt_hash` + a `chain_consistent` boolean (surfaced as the badge "chain-consistent (single-node)" — NOT a cryptographic tamper-proof / signing claim for analyst outputs). A lineage-integrity sweep prunes dangling `derived_from` so the walk has zero dangling links. |
| `GET /api/v1/v3/eval/country_scorecard` | **live** | The latest P4-T2 banded scorecard per active g20/watch desk (`v3_api.py:1200`, `kind='scorecard'` freshest live-head per desk — enumerates any active target tagged `g20`/`watch`, 25 today). PROJECTS the persisted `data.bands` — no re-banding, no fabricated band. An empty list means no scorecard has been computed yet (honest, not an error). Backs the eval scorecard panel (§2). |
| `GET /api/v1/v3/eval/calibration` | **live (honest-null)** | The honest skill scoreboard (`v3_api.py:1132`, `CalibrationScoreboard`): the exogenous-calibration Brier + the acute-forecast BSS. A thin / degenerate pilot returns "skill claim withheld" (`forecast_unproven=True` / `calibration_thin=True`), NEVER a bare positive number — the forecasting pilot currently reports NO proven skill (see §1.1). |
| `GET /api/v1/v3/system/analyst-cadence` | **live** | Per-analyst cadence health (`v3_api.py`, `build_v3_router`, mounted at `/api/v1/v3`): last run, age, runs 1h/24h, last outcome, and a healthy/stale/silent status — read from `analyst_traces` (the actual run record), NOT `actor_state.last_run_at`, which is NULL. Powers the System Status panel's Analysis layer (§2). |
| `GET /api/v1/v3/system/source-firing` | **live** | Per-source firing matrix (`v3_api.py`, mounted at `/api/v1/v3`): signals 24h/7d, last-seen age, last poll outcome, recent error count, and a firing/silent/error/paused status per source. Powers the System Status panel's Acquisition layer (§2). |
| `GET /api/v1/v3/eval/analyst_runtime` | **live** | Per-analyst run-timing observability (`v3_api.py:1267`, inline-SQL over `analyst_traces`, registry-slim): for each analyst over a `window_hours` (default 24, ≤720) it returns the run count, avg/max wall-clock seconds, last run, and non-success count. Read-only; surfaces the run-time telemetry (`run_started_at` / `run_ended_at` / `status`) that is written per run but not otherwise exposed on an API. |
| `GET /api/v1/journal` · `GET /api/v1/journal_proposals` · `POST /api/v1/journal_proposals/{id}/accept` · `…/reject` | **live** | The journal read + human-gated review-queue routes (`journal_api.py` / `journal_proposals_api.py`, mounted at `/api/v1`). The `journal_assessor` (12h entry) + `journal_consolidator` (daily) run ON cadence (§1.1), so the read route serves the accruing entries and the proposals routes drive the accept/reject queue. Accept runs an idempotent per-kind apply worker; reject requires a `decision_reason`. **Caveat:** the `correction` + `self_revision` apply paths are tested end-to-end; the `change`-apply path is import-verified but NOT yet exercised against a live registry (SEAMS #25). Routing the journal's reflections back into the spine via this queue is still a FUTURE item, not done. |
| `GET /api/v1/contention` | **live** | Read-only contested-claim groups + their per-value support clusters (Holes-B Wave 5, #101 — `substrate_reads_api.py`, `build_substrate_reads_router`, mounted in registry `server.py` at `/api/v1`). Plain SELECTs over the deployed `fact_contention` / `fact_contention_values` sidecar (migration 0055) — it NEVER mutates a fact, a group, or a marker. Filters `status` (defaults to LIVE disputes `contested`+`surfaced`), `subject` (lower-cased `subject_key`), `fact_id` (single group via `facts.contention_id`), `include_junk`, `since`. Backs the `ContestedBadge` UI surface (§2) and the consult read surface. The sidecar is populated by the `fact_contention_arbiter` analyst (§1.1); whether disputes ACCUMULATE depends on the write-path coexistence flag `LEGBA_FACT_CONTENTION` (default **OFF** in code + docker-compose), so on a default build this route serves an empty/legacy set. Proven live (returns real groups on this instance). |

Everything else in RUNBOOK §4.1 (findings / situations / signals / lineage /
targets / analysts / budget / source-credibility / events WS / the cross-analyst
critic rollup `GET /api/v1/v3/eval/scorecard`) is **live**.

### 1.1 Analyst-layer capabilities — the analysis spine (release-relevant, not a route/panel)

These are runtime analyst capabilities, not operator-facing routes/panels, but
they are the **product** and are release-relevant enough to classify here. The
spine composes bottom-up: NINE bounded reasoning UNITS → a mandatory
faithfulness VERIFY pass → a COMPOSITION TOWER (per-country → regional → world,
plus a thematic cross-desk escalation composition) → a banded SCORECARD, with a
deterministic indicators-&-warning layer and an honest SKILL scoreboard over all
of it. (Authoritative per-kind maturity still lives in `planning/` +
`docs/SEAMS.md` — this is a release-readiness summary.)

**The core discipline, not the data, is the product:** every claim is cited to
a source, checked by a mandatory faithfulness pass, and auditable through the
receipt chain. The system MEASURES groundedness (does each claim follow from its
cited evidence?), NOT truth — where a leg is honestly weak or degenerate today,
that is published, not hidden.

| Capability | State | Note |
|---|---|---|
| Nine bounded reasoning UNITS (`leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`, `internal_stability`, `military_posture`, `economic_coercion`, `proliferation_watch`, `disruption_status`) | **live** | `kind: inline_target` descriptors, each scoped by its own `subscription.targets.predicate` → ONE run per active DESK (staggered cron pairs so the units spread across the clock). Seven broad units scope `has_tag("g20") or has_tag("watch")`: the roster is driven by a COVERAGE TAG — the 19 G20 country desks PLUS a 13-target high-consequence WATCH tier (Israel, Iran, Ukraine, Taiwan, North Korea, Pakistan, plus the escalation-risk band Sudan, Mali, Burkina Faso, Niger, DR Congo, Myanmar, Haiti) = **32 country desks** today. An eighth, narrower unit, `proliferation_watch` (narrow: tag-scoped `has_tag("nuclear_watch")`), instead runs on only the ~8 nuclear-relevant desks (`country_g20_{cn,in,ru,us}` + `country_watch_{il,ir,kp,pk}`) at its own staggered 03:00/15:00 UTC slot — same shape, same gates, one-eighth the fan-out. A ninth, `disruption_status`, is tag-scoped the same way but to a **non-country** desk family — `has_tag("supply_chain")`, the thematic lane/flow desks, 06:00/18:00 UTC over a 24h window (3 of 10 such desks active, the rest `draft`). (A "desk" is a scoped subject-frame that a set of analysts work, NOT a surveilled entity; adding one is register-a-target, no code — the thematic family is that claim demonstrated rather than asserted.) Each ASSEMBLES a cited signal slice (72h for the country units, 24h for `disruption_status`; packed under the input-token budget — `inline_target._MAX_INPUT_SIGNALS = 200` is only a hard backstop, not a fixed-count trim) + a dated "AUTHORITATIVE CURRENT CONTEXT" grounding preamble of ACCUMULATED facts/nexuses/situations (`grounding.enabled: true` on all nine), answers ONE narrow question, and emits a strict-JSON cited `FindingPayload` whose prose carries `[N]` markers mapped to signal ids PLUS a machine-checkable `data.indicators[]` block (the structured-I&W contract the `indicator_tracker` diffs). The deterministic cite-the-prose `[N]` floor + the faithfulness verify gate (below) cover them for free. Skill is a PER-UNIT number (see the skill scoreboard), never a platform boast. Live counts are generated in [RELEASE_STATE.md](RELEASE_STATE.md) rather than maintained here. |
| Mandatory faithfulness VERIFY pass | **live** | Every cited unit/composition finding is scored for faithfulness in [0,1] by an LLM judge (`method.llm.verify`, declared on all nine units + every composition (country/region/world/escalation) — resolved through the judge route, `LEGBA_JUDGE_STACK_REF` env > `method.llm.judge` > `.verify` > `.primary`: the descriptor default is `raw: llm.primary.openai_compat`, the SAME core model that generated the finding, while the reference deployment's env rung repoints every judge call CROSS-FAMILY at a hosted Gemma judge — `RELEASE_STATE.md` reports both layers) PLUS a deterministic citation-presence floor. Same-model descriptor default = self-hostable; KNOWN LIMITATION — it shares the producer's blind spots (the floor + the signed provenance chain still backstop it), which is what the cross-family repoint answers. `effective_confidence = min(confidence, faithfulness_score)` is folded at read time and gates a visible low-confidence tier — it NEVER hard-deletes. It measures GROUNDEDNESS, not truth; a planted fabrication is flagged unsupported. A set of DEMOTE-ONLY calibration guards refine the fold: a STALE-LEADER guard flags a finding that calls the current US officeholder "former" (or names a wrong current holder), backed by a current-officeholder anchor in the temporal-grounding preamble; a NULL-RESULT rubric grades an honest corpus-scoped absence finding ("no proliferation activity; N signals focus on unrelated topics") as a faithful SURVEY of the cited evidence rather than uncited fabrication (and the citation-marker parser now expands ranges like `[1-92]` and treats `[no citation]` lines as floor-exempt); and a TARGET-CONSISTENCY guard flags a per-country finding whose named subject-country ≠ its desk — all fold through `effective_confidence = min(confidence, faithfulness)`, never a delete. **Honest note:** live faithfulness is genuinely low on some units — e.g. the US country reads all-insufficient on the scorecard because its unit faithfulness is genuinely low. That is surfaced, not suppressed. |
| Per-country COMPOSITION (`country_composition`, kind `meta_findings_synthesizer`) | **live** | A `targets`-bearing descriptor → per-desk fan-out (`has_tag("g20") or has_tag("watch")`, the same 32 g20/watch desks). Reads the SEVEN broad verified units for THAT desk, plus `proliferation_watch` on the ~8 nuclear-relevant desks (listed in `other_analysts`, verify-floored via the same INNER JOIN — it naturally contributes zero rows on the other 24 desks, not an error), and writes a hedged, cited synthesis; the READ_SLICE admits ONLY faithfulness-verify-PASSED sub-claims above the floor (unverified sub-claims never enter it). A country whose units produced no verify-passed sub-claim yields an EMPTY slice → a `confidence=0.0` "No source findings to synthesize" finding rather than an invented read. `derived_from` back-walks one hop to the up-to-eight units, two hops to their cited signals. Ticks organically (live). |
| Regional COMPOSITION (`region_composition`, kind `meta_findings_synthesizer`) | **live** | A `targets`-bearing descriptor → per-region fan-out (`has_tag("region")` → 5 region frames: `region_americas`, `region_europe`, `region_indo_pacific`, `region_mena`, `region_africa`). Reads the verified per-country `country_composition` findings for the desks in that region and writes a hedged, cited regional synthesis — the middle tier of the composition tower (units → country → region → world). Same mandatory verify + `effective_confidence` fold + `[[ref:<uuid>]]` citation-to-a-verified-read discipline. Ticks organically (live). |
| World COMPOSITION (`world_assessor`, repointed to `meta_findings_synthesizer`) | **live** | The TOP of the tower: NO `targets` block → ONE global meta run per tick, reading the verified `region_composition` findings (NOT the raw per-country reads directly, and NOT the old raw-signal executive one-pager — that verdict-from-nowhere framing was demoted, SEAMS #34, and the analyst graduated into the composition). It drills world → region → country → units → source. Each clause is cited `[[ref:<uuid>]]` to a verified regional read; the same mandatory verify + `effective_confidence` fold apply. Ticks organically (live). |
| Thematic cross-desk COMPOSITION (`escalation_composition`, kind `meta_findings_synthesizer`) | **live** | A target-less THEMATIC composition (`subscription.substrate.thematic_dimension: escalation` is the sole discriminator) that reads the `escalation` unit's verified findings ACROSS all desks and synthesizes one cross-desk escalation read. Carries a T7 CORRELATION GUARD (`data.correlation_guard`): it collapses each correlated cluster of desk findings to ONE independent evidence unit so a single wire event echoed across many desks is not double-counted, and the composition faithfulness verify enforces it. Ticks organically (live). |
| Banded SCORECARD (`scorecard_producer`, deterministic META; 12th OutputKind `scorecard`) | **live** | Each tick writes ONE banded row per active g20/watch desk (a global sweep enumerating any active target tagged `g20`/`watch` — 25 today) from high-precision RULES over already-verified claims banded across a rolling 14-day window (`severity` read column × `effective_confidence`, demote-never-promote), pure SQL / no LLM / $0. Every band NAMES the verified-claim id it rests on; a dimension with no qualifying verified claim reads "insufficient-evidence" with an explicit reason (never a fabricated band), and a per-claim faithfulness below the floor demotes to "low-faithfulness". A country with no qualifying claim STILL emits an all-insufficient row (never omit, never invent). Served on `GET /api/v1/v3/eval/country_scorecard`. **Honest note:** the live scorecard is a MIX — some countries band, others (e.g. the US) read all-insufficient. |
| Skill SCOREBOARD (per-unit eval + calibration + acute-forecast pilot) | **live, honest-null / honestly weak** | `unit_correctness_scorer` (deterministic META, $0) folds per-unit faithfulness + a correctness-vs-reference source-recall against operator gold in `unit_reference_labels` (migration 0057); `calibration_tracking` folds the exogenous Brier; the acute-forecast BSS rides the same `GET /api/v1/v3/eval/calibration`. **Weak spots are PUBLISHED, not hidden:** the correctness gold set is tiny (reported insufficient-sample — with the table empty/near-empty a unit reports `correctness_vs_reference = None` + a status string, never a fabricated number); the exogenous Brier absent a real outcome is tagged a SELF-CONSISTENCY measure (`self_consistency_only=true`), NOT calibration against reality; and the forecasting pilot currently reports NO proven skill (skill WITHHELD until non-degenerate AND BSS>0). |
| GEPA self-optimizer (`unit_optimizer`, kind `optimizer`, `dspy_compile`) | **live, human-gated (candidate arm)** | The optimizer RETURNS scoped to ONE measured unit (`leadership_transition`), NOT the frozen monolith. Every candidate carries a REAL paired before/after FAITHFULNESS delta on the same faithfulness judge (whatever the judge route resolves; live: parent 0.34 → candidate 0.29, delta **−0.05**), stays `promotion_gate=human_gated`, and can NEVER promote on a degenerate / absent / non-positive / non-finite delta (`gepa._delta_gates_ok` stamps `data.eval.promotable` at write time — no auto-promotion path; honesty suite `pytest -k p4t8_honesty`). dspy/GEPA live ONLY in the opt-in worker image — never the runtime or analyst inference path. The training set is passed BY REFERENCE (`TrainingSetRef`) and one weekly reminder fires, so the >4 MB reminder-flood class cannot regress. |
| Acute-forecast pilot driver (`forecast_scoreboard`, deterministic META) | **live (driver), reports no skill** | The weekly driver for the pre-registered acute-binary forecast pilot (#92): issues one binary forecast per G20 country, resolves closed windows EXOGENOUSLY by upstream event time, and its per-run finding is TRACE_ONLY — forecasting NEVER lands as a free-text claim/finding on a trust surface. A geography-dominated / degenerate probability vector ABSTAINS (zero rows). The numbers surface SOLELY on the calibration scoreboard, and the pilot currently reports NO proven skill (honest — `forecast_unproven=True`). |
| Deterministic I&W — indicator diffs (`indicator_tracker`, deterministic META) | **live (runs; no trigger emitted yet)** | Reads the machine-checkable `data.indicators[]` block every unit now mirrors from its "Indicators to watch" prose (stable slug `id` + `status` ∈ `triggered`/`not_observed`/`expired`) and DIFFS them run-over-run per desk, emitting a finding + a second escalation trigger class when an indicator flips `triggered`. Runs on cadence (14 successful runs live; 200 unit outputs carry structured `indicators[]`), but has emitted NO trigger finding yet — no indicator has flipped `triggered` (honest: the run path is proven, the trigger path is not exercised live). Pure SQL / no LLM. |
| Deterministic I&W — collection gaps (`collection_gap`, deterministic META) | **live** | On a monthly cadence reads the honest `insufficient-evidence` bands the SCORECARD already computes and turns them into a forward-looking COLLECTION requirement: which desk×dimension cells are STARVED, WHY (aggregated banding `reason`), so acquisition gaps surface as a first-class product rather than a silent absence. Pure SQL / no LLM (1 output live). |
| Alert rewire — verify-folded escalation (`escalate_finding` action pack) | **live (delivery BUS-ONLY)** | Severity moved from a free-text tag to a first-class READ COLUMN (`analyst_outputs.severity`, indexed on `high`/`critical`); alerts key on the POST-VERIFY ALERT SCORE (`effective_confidence × per-severity weight`) crossing the pack gate, so a verify-DEMOTED finding can no longer alert on a high-severity tag alone. An `indicator_tracker` flip into `triggered` is a second trigger class. Fires through the full agency lifecycle (resolve ∩ allow ∩ applicability → governor → dispatch → settle, audited in `action_pack_invocations`/`governor_events`). Each delivery writes a durable per-delivery audit row (`alert_sink_deliveries`, repurposed 2026-07-03) so the emission is auditable. **Honest limit:** external delivery lands ONLY on the NATS subject `channels.escalations` (bus-only) — there is NO paged-human / webhook / email edge wired yet; a subscriber must tail the bus. |

**Retirements / freezes (sequenced, documented in `docs/SEAMS.md`).** The
ambitious legs return ONLY as measured experiments; the always-on unmeasured
producers are frozen or retired first.

| Capability | State | Note |
|---|---|---|
| `country_assessor` (monolithic per-country one-pager) | **retired (stopped)** | Live head `state='retired'` (`POST /retire`) + removed from `scripts/bringup_register_analysts.py` `ANALYST_FILES` (SEAMS #35, 2026-07-01) — it fires no more. NOTHING in the trusted spine reads it — `country_composition` reads the SEVEN broad UNITS (plus `proliferation_watch` on nuclear desks) — yet before retirement, as a still-firing REACTIVE feeder (`subscription.targets` `has_tag("g20")`), it was the single largest producer of UNVERIFIED monolithic output. Because it was reactive, nulling its cadence was insufficient (cf. seam #31); retirement at the lifecycle level (runtime NOOPs `already_retired_or_absent`) + bringup removal is what stops it. **Not a clean slate:** ~1.2k historical `finding` rows it produced REMAIN in the DB, unread by the spine. The descriptor YAML still reads `state: active` on disk — the LIVE head is authoritative. |
| `country_predictor` (forecast-as-claim producer) | **retired (stopped)** | Nulling its cadence left it firing REACTIVELY (~1/hr `prediction` rows), so P3-T8 RETIRED the live head + removed it from `ANALYST_FILES` (SEAMS #31) — it fires no more. A numeric forecast is a CLAIM; forecasting returns ONLY as the acute-forecast Brier/BSS scoreboard (`forecast_scoreboard`, above), never a free-text claim. **Not a clean slate:** ~539 historical `prediction` rows from the two predictors REMAIN in the DB. |
| `india_energy_predictor` (sibling forecast-as-claim producer) | **frozen (stopped)** | `cadence.fallback_schedule` NULLED (SEAMS #32) — the `if schedule:` reminder gate registers no `run_cadence` reminder, so it fires on no tick. Returns behind the same acute-forecast scoreboard (its share of the ~539 historical `prediction` rows remains). |
| `country_optimizer` (the always-on unmeasured GEPA monolith) | **cadence-frozen** | `cadence.fallback_schedule` NULLED (SEAMS #30) — no `run_cadence` reminder, verifiably silent (the descriptor head still reads `state: active`; it is the CADENCE that is frozen, not a lifecycle retirement). It returns as the SCOPED, measured `unit_optimizer` above; the monolith stays byte-frozen (no reminder-flood regression). |

**Other live runtime capabilities.** (Maintenance / substrate legs — accurate as
of this revision.)

| Capability | State | Note |
|---|---|---|
| Analyst knowledge-grounding (Tier 1, structured) | **live** | The `grounding` descriptor block (`GroundingBlock`, `enabled: false` default) opts an analyst into a deps-builder step that, before the LLM call, reads CURRENT authoritative `facts`/`nexuses`/situations (temporal-honesty gate `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`, curated/seed-preferred) for the target geo + slice entities and PREPENDS a dated "AUTHORITATIVE CURRENT CONTEXT" preamble that also SUPERSEDES stale model priors. Opted IN (`grounding.enabled: true`) on all nine bounded units + the journal tiers; live-verified injecting the current US head of state. Token-capped (`max_facts`), degrade-not-drop (a read miss → no preamble, never fabricates). The retired `country_assessor`'s grounding retired with it; the compositions read already-verified sub-claims rather than raw signals, so they do not re-ground. **Tier 2** (the `vector:world_context` semantic collection) is now PROVISIONED and FLIPPED ON as a guarded, measured pilot on ONE unit (`internal_stability`; `leadership_transition` was rolled back) — see the "Vector RAG grounding (Tier 2)" row below and SEAMS #20 (no longer un-provisioned). |
| Vector RAG grounding (Tier 2) + consult corpus search | **live (guarded pilot on ONE unit; MEASURING)** | Two curated Qdrant collections are LIVE and populated — `tradecraft` (analytic-standards / SAT handbooks, 1,716 chunks) and `world_context` (country/topic priors + doctrine summaries, 293 chunks re-embedded in place), both `bge-m3` 1024-dim cosine — embedded THROUGH the stack port (the S5-T1 / SEAMS #11 embedder-through-port wiring, resolved). Two live surfaces: (a) the consult/GATHER `search_context` tool — one of the semantic-search read tools in the now-19-tool `substrate_read` pack — semantic-searches the corpora and returns cited chunks; (b) inline OPPORTUNISTIC RAG on the grounding path, degrade-not-drop when the corpus returns nothing. After the 2026-07-03 rollback, `world_context` RAG was RECALIBRATED and re-activated as a GUARDED, MEASURED pilot on the `internal_stability` unit ONLY (`leadership_transition` RAG is now OFF — its rollback is live). The embedder (`bge-m3`) was fine; the fixes were retrieval USAGE — a focused "`<country> <theme>`" query (was a diluted unit-name+entity blob), doc contextualization (chunks embedded with a "`<Country> — <section>`" lead), the corpus re-embedded in place, and the relevance floor lowered 0.65→0.55 (on-target now ~0.6, off-target ~0.42). A REAL per-run AUTO-ROLLBACK guard (`src/legba/runtime/rag_rollback.py`) replaces the old comments-only one: it re-checks a disabled-units env (`LEGBA_WORLD_CONTEXT_DISABLED_UNITS`) + a persisted state file (`LEGBA_RAG_ROLLBACK_STATE`) on EVERY run, so a rollback suppresses injection on the NEXT run WITHOUT a restart; triggers = faithfulness drop / low-faith ratio / token-cost rise (≥35%), actuated by `scripts/rag_watch.py --enforce` (re-embed helper `scripts/reembed_world_context.py`). Per-run trace records `world_context_top_score` / `retained` / `min_score` so the measurement is honest. The injected priors remain NON-CITABLE (fenced background, no `[N]` ids). **Honest limit:** whether the flip IMPROVES faithfulness is being MEASURED, not yet proven — firing RAG historically thickened the low-faithfulness TAIL even with the header; the guard reverts if it recurs, and the pilot state file currently lives at an EPHEMERAL path (move to a volume for persistence). Only 1 of 8 units is flipped on. |
| `journal_assessor` + `journal_consolidator` (first-person reflective voice, one of the 12 OutputKinds) | **live (introspective instrument)** | Both run ON cadence — `journal_assessor` every 12h (`fallback_schedule: "0 0,12 * * *"`, entry tier) and `journal_consolidator` daily (`"0 2 * * *"`), descriptor heads `state: active`. This is an introspective instrument, NOT a producer in the cited-synthesis spine: it writes ONLY `journal_entries` off the fact/finding/nexus chain (empty `derived_from`, excluded from the lineage catalog), so it CANNOT pollute product output. Both its GATHER and its VOICE phases now run FULLY on the core plane; the Anthropic plane is reserved for the on-demand consult / deep_consult features ONLY (the VOICE phase previously ran on Anthropic Opus). Its read + human-gated propose/accept routes (§1) are live; routing its reflections back into the spine via that queue is a FUTURE item, not done. |
| `proposed_edge_governance` analyst | **live** | Promotes pending `proposed_edges` into neutral `CoOccursWith` nexuses (`descriptors/analyst_proposed_edge_governance.yaml`). |
| `fact_contention_arbiter` analyst | **live (detect-only)** | The contested-claims referee (Holes-B Wave 2, #101 — `descriptors/analyst_fact_contention_arbiter.yaml`, `deterministic`-kind GLOBAL META analyst, hourly at `:37`, `TRACE_ONLY`). DETECT-ONLY invariant (B15): it NEVER mutates a fact value / `valid_until` / `superseded_by` / `confidence` — it scans OPEN facts, fuzzy-clusters competing values (`provenance/value_clustering.py`: canonicalize-entity + normalized-Levenshtein, threshold 0.12 — Russia/Russian and Kyiv/Kiev merge, North/South Korea stay split), junk-gates, scores each value cluster `Q·C·R·F` (quorum, credibility-share, recency half-life, confidence), and surfaces at most ONE winner per `(subject, predicate)` or ABSTAINS on a near-tie. Its ONLY writes are the `fact_contention` / `fact_contention_values` sidecar + three thin `facts` markers (`contested`, `contention_id`, `surfaced_winner`), all recomputable from open facts (migration 0055). **Optional Wave-2b LLM tie-break** runs ONLY on a near-tie abstain, on the SELF-HOSTED vLLM plane (hard-refuses an Anthropic/Opus primary), bounded (256 tokens, 30s, ≤10 calls/pass), degrades to abstain on any failure — flag `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` (default **OFF**). Detect-only arbiter proven live; the vLLM tie-break is proven CONSULTED live (it correctly abstained on symmetric evidence), but a successful LLM PICK is unobserved-live so far. Whether disputes COEXIST for it to group depends on the write-path flag `LEGBA_FACT_CONTENTION` (default **OFF**); both flags are enabled only on this instance via the gitignored `.env`. |
| Phase-D graph-metrics legs | **live** | `structural_balance` / `graph_mining` / `nexus_decay` WRITE rows via `_graph_metrics_sink.py` (previously inert). The `graph_mining` "interesting" hostile-edge shortlist is now VETTED — canonical class-checked endpoints only (drops NER fragments like Parl/Fed/West/Leader), requires a genuine hostility `rel_type` + negative polarity (a neutral "conducted via" edge is not relabeled hostile), a subject-attribution guard (so "protesters in X" is not emitted as "State X hostile to `<person>`" while a real State→person hostility survives), and a per-edge quality score. |
| `cross_correlator` (cross-desk correlation + blind-spot finder) | **live (verify-gated)** | Now ENTERS the mandatory faithfulness verify pass (confidence clamped by `min(conf, faith)`); its read-slice was REPOINTED off the retired `country_assessor`/`country_predictor` onto the LIVE composition + unit layer, so it correlates real verified findings instead of degrading to "insufficient data" blind_spots. It supersedes via a stable `situation_signature`, and blind_spot heads decay only when their scope is revisited. (`world_assessor` likewise supersedes prior same-signature heads at write time, synchronous.) |
| `thematic_proposal` candidacy vetting | **live** | Absence/negation-framed compositions are EXCLUDED from thematic-composition candidacy; candidate slugs derive from the stable `situation_signature` (one situation = one slug) and are deduped. |
| Multilingual / telegram NER at ingest | **live (forward-only)** | Telegram messages (body in `payload.text`) are now NER'd (were skipped → 0 entities); non-Latin scripts (Arabic / Russian / Ukrainian) are TRANSLATED to English via the hosted NLLB `/translate` endpoint before extraction (were 0 entities). **Honest limit:** forward-only — a re-enrichment batch of the ~9k old telegram / non-Latin signals is a separate operator job, not yet run. |
| Entity / fact / nexus write-path gates | **live** | Resolution write-paths hardened after the 2026-07 re-fragmentation (migrations 0076–0080). ENTITY: an alias/article-aware + class-guarded pre-lookup (stops "the Strait of Hormuz" forking from "Strait of Hormuz"; a fallback-elected keeper is never class-mutated) + a junk gate (numeric/quantity/possessive surfaces) + conservative class validation. FACT: a predicate-argument/relation-direction gate (rejects "NATO member of Turkiye" inversions), a demonym + relative-temporal subject reject, and adjective-nationality VALUE normalization scoped to geographic/relational predicates only — while un-breaking a prior over-aggressive "sports roster" gate that was silently dropping real IGO-membership facts. NEXUS: a junk/vague-endpoint gate, a same-referent self-edge gate, and demonym/plural dyad canonicalization (so "Russia\|Russian × Ukraine\|Ukrainian" stops inflating dyad counts). |
| ACH outcome-resolution + calibration | **live (self-consistency-flagged)** | `competing_hypotheses` status-transitions resolve to `resolved_outcome`; `calibration_tracking` computes a Brier. **Caveat:** absent an exogenous outcome the Brier is a SELF-CONSISTENCY measure, tagged `brier_self_consistency_only` / `self_consistency_only=true` — NOT calibration against reality. The exogenous-outcome seam is preserved. |
| `signals.source_credibility` at ingest | **live (legacy backlog)** | Populated at ingest by a host-lookup in `source_actor.lookup_source_credibility`; state / social-media outlets are now SEEDED below the 0.5 ingestion nominal (presstv 0.25, irna 0.30, ukrinform 0.45, telegram/t.me 0.30) so state-affiliated outlets no longer out-credit their peers. **Caveat:** this fixes NEW signals; pre-fix rows stay NULL until an optional backfill runs. |
| Journal propose-and-gate queue | **live (human-gated)** | Everything the journal wants to affect outward — a `correction`, a `change`, or a `self_revision` (including edits to its OWN instructions via `propose_self_revision`; protected sections auto-reject) — goes to the human-gated `journal_proposals` queue, NEVER a live table. A human accepts/rejects (routes in §1); accept runs an idempotent per-kind apply worker. **Caveat:** the `correction` + `self_revision` apply paths are tested end-to-end; the `change`-apply path is import-verified but NOT yet exercised against a live registry (SEAMS #25). (The journal's own entry/consolidator cadence RUNS — see the `journal_assessor` + `journal_consolidator` row above; only the routing of accepted proposals back into the spine is a future item.) |

---

## 2. UI panels (`panel-registry/registry.ts`)

### 2.0 Generated panel table

The classification below (kind, category, scope, title, binding, modes,
tier, hidden) is generated directly from `registry.ts` — it is the
mechanically-current ground truth for "what tier is panel X" and "is panel Y
hidden", closing the gap §3 used to name (this matrix drifting from
`registry.ts` with no automated check). The **why** behind a `preview` or
`hidden` classification is operator knowledge that has no source in
`registry.ts` — that narrative stays hand-maintained in the "Live / Preview /
Hidden" prose subsections below the table, unchanged by regeneration.

<!-- BEGIN GENERATED PANEL TABLE -->

_Generated by `scripts/gen_release_state_matrix.py` from `legba-ui-v3/src/panel-registry/registry.ts` — do not hand-edit between the markers; re-run the script instead. 56 panel kinds registered — 54 live, 2 preview, 6 hidden (a panel can be both live/preview AND hidden — hidden is a navigation-tier flag, not a build state)._

| Kind | Panel ID | Category | Scope key | Default title | Requires binding | Modes | Tier | Hidden |
|---|---|---|---|---|---|---|---|---|
| `target.claims` | `target_claims` | target | target_id | Target Claims | yes | personal, cis | **live** | no |
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
| `system.actor_health` | `system_actor_health` | system | — | Actor Health | no | personal | **live** | no |
| `system.alerts_watches` | `system_alerts_watches` | system | — | Alerts & Watches | no | personal, cis | **live** | no |
| `system.audit` | `system_audit` | system | — | Audit-Chain Browser | no | personal | **live** | no |
| `system.budget` | `system_budget` | system | — | Budget Ledger | no | personal | **live** | no |
| `system.consult` | `system_consult` | system | — | Consult | no | personal | **live** | no |
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

<!-- END GENERATED PANEL TABLE -->

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
  `tier: 'live'`. Entries accrue on the journal's own cadence — `journal_assessor`
  every 12h + `journal_consolidator` daily, an introspective instrument off the
  fact/finding/nexus chain — §1.1.)
* **Consult model picker** (`system.consult` / `system.deep_consult`): each
  consult / deep_consult request may now CHOOSE which registered LLM plane
  answers — a model dropdown labels "Opus (Anthropic · billed)" (THE DEFAULT —
  no selection preserves prior behavior) vs "Core (free)" (the self-hosted core
  plane), and remembers the last choice. A server-side allowlist maps the
  friendly value → component id (the client never names a component);
  FAIL-CLOSED — a chosen non-default plane that can't be honored RAISES rather
  than silently billing the default, and a provider outage surfaces as a
  graceful HTTP 503 naming the OTHER plane (e.g. "The Core plane is unavailable …
  select the Opus model"), not a bare 502. Budget accounting keys off the chosen
  plane; the shared per-day consult token cap still binds on both. **Honest
  limit:** the F1 UI image needs a redeploy to show the neutral "Core (free)"
  label live.
* **`system.eval_scorecard`** ("Eval Scorecard") now renders TWO honest surfaces:
  the **skill scoreboard** (P4-T4, off `GET /v3/eval/calibration` — the exogenous
  Brier + acute-forecast BSS; a thin/degenerate pilot shows "skill claim
  withheld", NEVER a bare positive number) and the **banded per-country
  scorecard** (off `GET /v3/eval/country_scorecard` — one honest card per active
  g20/watch desk (25 today); a dimension with no qualifying verified claim shows
  "insufficient-evidence", never a fabricated band). It also carries the
  cross-analyst critic rollup (`/v3/eval/scorecard`).
* **System Status** (`system.status`, "System Status" — the per-component /
  per-layer health view: composes **Acquisition** (per-source firing matrix off
  `GET /api/v1/v3/system/source-firing`), **Analysis** (per-analyst cadence
  health off `GET /api/v1/v3/system/analyst-cadence`, read from `analyst_traces`
  because `actor_state.last_run_at` is NULL — the gap the Actor Health panel
  could not fill), **Queues** (consumer backpressure off the orphan-filtered
  `GET /api/v1/v3/streams/consumer_lag`), and **Infra** into one page;
  `tier: 'live'`.)
* v4 rooms: `v4.map`, `v4.flow`, `v4.why`. The **`v4.map`** room's default world
  map is now the maplibre-gl banded-verdict CHOROPLETH (`MapLibreWorldMap.tsx`),
  with Leaflet as the `hasWebGL`-false fallback (`mapEngine.ts`) — SEAMS #13. The
  **`v4.why`** room now LEADS with
  the seven broad bounded-unit reads (`CountryUnitsAssessment.tsx`, each carrying its
  per-unit eval badge) as the headline per-country product surface — the panel's
  `UNITS` list does not yet include `proliferation_watch`, so its nuclear-desk
  read is not surfaced here; the old
  `country_assessor` synthesis was demoted to a collapsible feeder (SEAMS #35)
  and is now retired at the backend (§1.1). `ProvenanceTrail.tsx` drills a
  selected node unit → cited source with the per-hop "chain-consistent
  (single-node)" badge, and the `LineageGraph` / temporal-map lenses render the
  progressive hash-chained lineage DAG over a read (per-hop "chain-consistent
  (single-node)" badge, not a cryptographic signing claim). `WorldAssessment.tsx`
  renders the
  world composition (`world_assessor`), NOT a monolithic verdict banner (that
  framing was demoted, SEAMS #34).
* **`ContestedBadge`** (Holes-B Wave 5, #101 — `v4/components/ContestedBadge.tsx`):
  not a standalone registered panel but a self-contained **component** mounted
  into two existing live panels. It reads `GET /api/v1/contention` (§1) through
  the pure, unit-tested `@/lib/contentionModel`, renders NOTHING when the claim
  is not contested (the common case → zero visual noise), and is read-only (it
  never mutates a fact, group, or marker; a 5xx lookup failure shows a subtle
  affordance rather than masking as "uncontested"). Two mount points:
  **(1) `v4.why` ProvenanceTrail** — fact-keyed (`<ContestedBadge factId={…} />`,
  precise `?fact_id=` lookup → 0/1 group) on a lineage node whose
  `row_kind === 'fact'`; **(2) `target.claims`** (`Claims.tsx`) — subject-keyed
  (`<ContestedBadge subject={claim.statement} />`, `?subject=` → 0..N groups,
  surfaces the first LIVE one) since findings carry no real `facts.id`. When
  contested it shows a "Contested" badge ("Contested — no winner" on a near-tie
  abstain) plus a per-value support panel (distinct-source count,
  credibility-share, arbiter score, surfaced-winner flag). **live** code, but the
  underlying disputes only ACCUMULATE when the write-path flag
  `LEGBA_FACT_CONTENTION` (default **OFF**) is set — so on a default build both
  mount points render no badge.

### Preview — registered, honest pending / client-only state

| Panel | Why preview |
|---|---|
| `system.optimizer.diff` (`OptimizerDiff`) | Backend `GET /v3/optimizer/candidates/{id}/diff` is **wired** (`v3_api.py:880`, snapshot-based, no dspy import) over the SCOPED `unit_optimizer` (§1.1); kept badged `preview` only because the human-gated promote flow around it is still maturing. The diff itself renders live data. |
| `system.backfill` (`Backfill`) | Honest pending UI; the replay button is gated/disabled rather than wired to a destructive backfill. Should be **disabled-not-exposed** in a product build. |
| `system.search` (`Search`) | Client-only today (no server-backed global search index wired). |
| `system.alert_center` (`AlertCenter`) | Client-only alert view; `alert_sink_deliveries` is now the unified per-delivery audit written by the live channel-emit escalate edge (repurposed 2026-07-03), but this panel does not yet read it. |
| `system.report_export` (`ReportExport`) | Client-only export composer; no server report-render route. |
| `system.tenant_view` (`TenantView`) | Client-only owner-grouping convenience; Legba ships single-tenant (`docs/DIRECTION.md` §0) — it surfaces the descriptor-`owner` rollup, it does NOT enforce any tenant isolation boundary. **Also in `HIDDEN_KINDS`** (#90 Wave A) so it is not surfaced in default nav. |

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
  Inspector — consistent with the `world_assessor` composition of §1.1),
  `system.runtime` (Runtime Actor Health deduped against `system.actor_health`).

The deeper operator/diagnostic panels NOT in `HIDDEN_KINDS` (dead-letter,
governor, audit-chain, stream-lag, etc.) remain registered and reachable by
record-jump / explicit add; the 2026-06 redesign demoted the 37-item menu but
kept them live.

---

## 3. Release-gate hook

`scripts/release_gate.sh` stage 4 builds `legba-ui-build` (the tsc gate), so
a panel that fails type-checking blocks the release. The `tier` flag lives in
`def()` (+ the `PREVIEW_KINDS` / `HIDDEN_KINDS` sets), so the classification is
machine-readable in code — but there is still no automated check that THIS
matrix stays in sync with `registry.ts` (§2) or with the descriptor cadence /
lifecycle state that drives the §1.1 `frozen` / `retired` rows; keep it current
by hand against those sources when panels change tier or an analyst is
frozen/retired.
