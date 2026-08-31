# Analysis

The analysis plane and the analytical methodology of Legba: how matched signals
become coalesced analyst runs, what each analyst kind reads and writes, how
analysts are given governed agency over external tools, how the system evaluates
and improves its own analysts, and the analytical theory the whole machine
expresses.

This is one of the four planes of the platform. For the acquisition plane
(sources, baseline enrichment, fan-out, subscriptions) see `ACQUISITION.md`; for
the substrate stores see `ARCHITECTURE.md`; for the AI models the analysts
call see `AI_MODELS.md`; for operating the runtime see `RUNBOOK.md`.

**Contents:** [1. Where analysis sits](#1-where-analysis-sits) ·
[2. Coalescing triggers](#2-coalescing-triggers) ·
[3. Analyst kinds — the taxonomy](#3-analyst-kinds--the-taxonomy) ·
[4. The deterministic sub-handler library](#4-the-deterministic-sub-handler-library) ·
[5. Action-pack agency](#5-action-pack-agency) ·
[6. The eval loop](#6-the-eval-loop--analyst--critic--optimizer) ·
[7. Analytical methodology](#7-analytical-methodology) ·
[8. Proven end-to-end](#8-proven-end-to-end)

---

## 1. Where analysis sits

Legba is source-first. A `SourceActor` acquires data, produces **one** canonical,
target-agnostic signal, enriches it once (language, geo, entity NER), and
publishes it once to NATS JetStream on `legba.signals.>`. Signals carry no
`target_id` — they are observations, not interpretations.

The fan-out plane routes each published signal to the many targets that subscribe
to it by predicate (coarse NATS subjects, narrowed by a structured SQL `WHERE`
plus a Starlark residual — see `ACQUISITION.md`). A target is a passive subscriber;
it has no acquisition of its own.

**The analysis plane begins where a matched signal reaches a target's analysts.**
It has four moving parts:

1. **Coalescing triggers** — a matched signal (or a new upstream finding) marks an
   `(analyst, target)` pair *dirty*; the analyst fires when a gate trips, not per
   signal. (§2)
2. **Analyst kinds** — the cognitive units. Each kind reads a defined substrate
   slice, runs a method (LLM-planner, statistical, or pure-Python), and writes
   typed outputs with full provenance. (§3, §4)
3. **Action-pack agency** — the governed, allow-listed bundle of external tools an
   analyst may invoke (enqueue media jobs, emit to channels, discover sources),
   with a per-pack governor and a global budget envelope. (§5)
4. **The eval loop** — a **mandatory faithfulness verify** pass scores every cited
   finding for groundedness and folds `effective_confidence`; `critic` analysts
   grade outputs against rubrics; the `optimizer` returns only as a scoped,
   measured `unit_optimizer` experiment (the old always-on monolith is
   cadence-frozen). (§6)

Those four parts are the *mechanics*. The **product** they assemble is a
bottom-up reasoning spine (§3.11): nine bounded reasoning units answer one narrow
question each — eight over the country desks, plus one domain unit
(`disruption_status`) over the thematic supply-chain desks, tag-scoped exactly
the way `proliferation_watch` is. Seven answer per country desk — the 19 G20 members plus a 13-desk
high-consequence **watch tier** (Israel, Iran, North Korea, Pakistan, Taiwan,
Ukraine, and the escalation-risk band Sudan, Mali, Burkina Faso, Niger,
DR Congo, Myanmar, Haiti), 32 desks in all; the eighth, `proliferation_watch`,
is narrower — tag-scoped to only the ~8 nuclear-relevant desks. A per-country
composition, then a per-region
composition, then a world composition synthesize the *verified* sub-claims (a
thematic `escalation_composition` also fuses the escalation dimension across every
desk); a deterministic banded scorecard writes one honest per-desk verdict; two
deterministic indications-&-warning analysts diff the structured indicators
run-over-run and name starved collection cells; a skill scoreboard reports how
well each unit and each forecast actually did; and a deterministic **alert
loop** (§4.4.1) watches the verified state for transitions worth an
operator's attention — band crossings, verified high-severity findings,
contested-claim flips, baseline deviations, watchlist hits — each alert
carrying its verification posture and a receipt link. Every claim on that spine is cited
to source, checked by the faithfulness verify pass (which measures *groundedness —
does each claim follow from its cited evidence — not truth in the world*), and
drillable through a receipt chain to the original signal.

The methodology those parts express — JDL data-fusion levels, the confidence
architecture, hypotheses / ACH, the entity knowledge graph, and temporal-graph
concepts — is in §7.

---

## 2. Coalescing triggers

`src/legba/runtime/triggers/`

An analyst does **not** run once per signal. Running a (possibly LLM-bearing)
analyst on every matching observation would fan out uncontrollably on a busy
target. Instead, a matched signal marks the `(analyst, target)` pair dirty, and
the analyst fires on whichever gate trips first, clamped by a cooldown.

### 2.1 The three gates and the clamp

`triggers/policy.py` is a pure decision kernel (`decide()`) over a persisted
accumulator and a `TriggerPolicy`. Evaluation order — first match wins:

1. **Cooldown clamp** — after a fire the pair is muted for `cooldown_seconds`.
   Inside that window nothing fires, no matter how much dirt accumulates. This is
   the thrash ceiling.
2. **Severity gate** — a single pending signal at or above `severity_gate` wakes
   the analyst immediately (a critical signal cannot wait for a batch to fill or a
   tick to land). The escape hatch.
3. **Accumulation gate** — `pending_count >= accumulation_threshold` fires the
   batch. A busy target fires as soon as it has enough to chew on, without waiting
   for the cadence tick.
4. **Cadence gate** — a periodic tick (`cadence_seconds`), but only when there is
   at least one pending signal. The floor: a quiet target whose signals never
   reach the accumulation threshold still gets re-evaluated on a schedule, so a
   slow drip eventually fires.
5. Otherwise **hold** (dirty-but-below-threshold, or not dirty).

Severity ranks are an ordered ladder `info < low < medium < high < critical`
(`SEVERITY_ORDER`); an unknown label ranks `-1` and never wakes the severity gate.

### 2.2 The accumulator and its five durable behaviours

`TriggerAccumulator` is persisted in Postgres (`triggers/state.py`) so trigger
state **survives a restart**. It holds the pending count, the highest pending
severity rank, the last-fired and first-dirty timestamps, and a bounded set of
already-seen canonical signal ids.

Five behaviours are guaranteed by the kernel:

- **severity-wake** — a critical signal fires now (§2.1 gate 2).
- **batch** — N accumulated signals fire together (gate 3).
- **cooldown-cap** — fires are spaced at least `cooldown_seconds` apart (gate 1).
- **restart-survives** — the accumulator is durable; a held signal is never lost.
- **alias-no-double-wake** — the coalescer keys accumulation on a signal's
  **canonical** id (`canonical_signal_id ?? id`), set by the `cross_source_dedup`
  analyst (§4.2). Two deliveries of the same observation — via two sources, or a
  re-delivery — bump the counter once. `apply_dirty()` returns `counted=False`
  for an already-seen canonical id and the pair is not re-woken.

### 2.3 Fire path and exactly-once dispatch

`triggers/coalescer.py` owns the I/O. On a matching signal it reads the
accumulator, applies dirt, and re-evaluates. On a fire decision it **CAS-claims**
the fire on the accumulator's last-fired anchor (`claim_fire`) — exactly one
worker wins, so a pair that both the signal path and the cadence tick decide to
fire is dispatched once; the loser observes a changed anchor and backs off. The
claim atomically resets the accumulator (zeroes pending, clears the seen-set), so
a handler crash does not re-fire the same batch.

A fire carries the **whole batch** (`pending_count` signals accumulated since the
last fire), never a single signal — that is the coalescing guarantee. The
`TriggerFire` is handed to an `AnalystTriggerRunner` (`triggers/dispatch.py`), the
seam between triggering and analyst execution.

### 2.4 The LLM-safety rule

Coalescing runs for deterministic **and** LLM-bearing analysts: an
`ActorTriggerRunner` (`triggers/dispatch.py`) dispatches a coalesced fire to the
analyst actor for any method kind. LLM analysts fire on the accumulation /
cadence gates — **never per-signal**. Two independent guards enforce that:

- **Policy guard** (`policy_from_descriptor`): any method kind other than
  `deterministic` / `stat_forecaster` / `dspy_compile` is treated as LLM-bearing.
  Its effective accumulation threshold is floored to `min_llm_batch` (2) — so even
  a declared threshold of 1 cannot make an LLM fan out one call per signal — and
  its severity gate is disabled unless the operator explicitly sets
  `allow_llm_severity_wake`.
- **Dispatch guard** (`DeterministicTriggerRunner`): the deterministic runner
  raises if handed any LLM-bearing `method_kind`. It is for deterministic analysts
  only, full stop.

### 2.5 The NATS-driven engine

`triggers/engine.py` is the thin production loop. It binds one durable pull
subscription onto the union of registered subject filters over the `legba_signals`
stream, re-checks each delivered signal against the target's **full** structured
filter and Starlark residual (the coarse subject only narrowed delivery; the exact
match is always SQL/Starlark), and feeds matches to the coalescer. A new upstream
**finding** (a derived signal an analyst published, event-class `derived`) is just
another matching signal on the same stream — the same dirty→gate path handles "a
new upstream finding" with no special case. A cadence ticker drives the periodic
re-evaluation. A delivered message is acked once its dirty-state is durably
persisted (or it fired), so acking a held signal never loses it.

---

## 3. Analyst kinds — the taxonomy

`src/legba/data/analysts/`

The runtime walks this package at startup (`discover_analyst_kinds`) and registers
each kind by its `KIND_NAME`. Every kind module exposes:

- `KIND_NAME` — the registry / descriptor key.
- `run_method(inputs, options, deps) -> AnalystMethodResult` — the async entry
  point the analyst actor calls per run.
- `OUTPUT_KIND` — the `OutputKind` (FINDING, PREDICTION, SITUATION, HYPOTHESIS,
  CRITIQUE, META_FINDING, PROMPT_MODULE_CANDIDATE, JOURNAL …) the host's
  dispatcher writes for the kind's result.
- optional `READ_SLICE` — a per-kind substrate-slice reader, when the kind reads
  across many targets or reads other analysts' outputs rather than the default
  signals slice.
- optional `build_prompt_module()` — the DSPy module the optimizer can compile
  (omitted by purely-deterministic kinds).

Adding a kind is one new module plus a descriptor registration. The actor host
treats the LLM-planner kinds interchangeably. The open taxonomy is real: the
`journal_assessor` (§3.10) is an **extension** analyst kind — registered via
`register_analyst_kind` + the `vocabulary_entries` family rather than the closed
built-in `AnalystKind` enum — so the count of built-in kinds is unchanged and the
journal rides on top.

The **twelve** built-in kinds the deps-builder dispatches
(`build_analyst_run_method`; these twelve — plus the `journal_assessor` extension
kind that rides on top — are the module names in `_KIND_MODULE_NAMES`, walked
by `discover_analyst_kinds`):

| Kind | Method | Reads | Writes |
|---|---|---|---|
| `inline_target` | LLM planner over one target | one target's signal slice | finding (situations / hypotheses as future emissions) |
| `cross_target_raw` | LLM planner over many targets | raw signals across N subscribed targets | finding (`cross_target=true`, contributing target ids) |
| `meta_findings_synthesizer` | narrower-context LLM | other analysts' **findings** (not raw signals) | second-order finding (`meta=true`) |
| `cross_analyst_correlator` | LLM correlation detector | many analysts' outputs | finding tagged contradiction / agreement / blind_spot |
| `relationship_reifier` | 8B-LLM relation typer | candidate co-mention pairs (`proposed_edges`) + open facts | typed signed `nexus` (`OutputKind.NEXUS`) — §7.3 |
| `competing_hypotheses` (alias `ach`) | LLM proposes the hypothesis set; **LLM scores each matrix cell** (Heuer CC/C/N/I/II, budget-gated) with the deterministic lexical/polarity scorer as the budget-exhausted fallback | focal situations × current facts × signed nexuses (scoped by resolved-entity set) | hypotheses (`OutputKind.HYPOTHESIS`) — §7.5 |
| `consult_on_demand` | single-turn ReAct loop | free-form question + scope predicate; substrate via tools | consult response (carried as a finding) |
| `deep_consult` | schedules the deep-consult Dapr Workflow, returns a task id | free-form deep-research question | persisted finding (via the workflow) |
| `predictor` | AutoARIMA + optional LLM narrative | a window of recent signals | prediction (point + CI + narrative) |
| `deterministic` | pure-Python sub-handlers | substrate slices / graph / Postgres time-series windows | structured findings + substrate mutations |
| `critic` | LLM judge against a rubric | one analyst output + its rubric | critique (per-dimension scores + revision delta) |
| `optimizer` | DSPy + GEPA (Dapr Workflow, isolated worker) | an analyst's traces + critiques | prompt-module candidate (promotion-gated) |

(The `deterministic` row is itself an umbrella over the pure-Python sub-handlers —
`entity_resolution`, `structural_balance`, `graph_mining`, `nexus_decay`,
`fact_decay`, `calibration_tracking`, `integrity_sweep`, `finding_supersession`,
… — each registered against its own descriptor.)

### 3.0 Analyst kinds — quick reference

A one-row-per-kind index of the registered kinds, keyed on what an operator
reads off a descriptor: the **`method.kind`** the deps-builder branches on
(`schemas/analyst.py:399` — `llm_planner` / `deterministic` / `stat_forecaster`
/ `react_loop` / `critic` / `dspy_compile`), what the kind **reads**, its core
operation, the **`OutputKind`** the dispatcher writes (`provenance/kinds.py:57`),
whether it calls an LLM, and its cadence. **Cadence values are read off the live
G20 bring-up descriptors** in `descriptors/` (`fallback_schedule`), not the
`india_energy` smoke-test descriptors. Two kinds (`relationship_reifier`,
`competing_hypotheses`) carry a primary `OUTPUT_KIND = FINDING` **receipt** but
**side-write** their real artifact (`NEXUS` / `HYPOTHESIS`) on the run
connection — the receipt vs. side-write split is called out in the Writes column.

| Kind | `method.kind` | Reads | Core op | Writes (`OutputKind`) | LLM? | Cadence |
|---|---|---|---|---|---|---|
| **`inline_target`** (the 9 bounded UNITS) | `llm_planner` | one desk's 72h signal slice + grounding preamble — seven units subscribe via `subscription.targets`, `has_tag("g20") or has_tag("watch")`; the eighth, `proliferation_watch`, is narrower — `has_tag("nuclear_watch")`, ~8 nuclear-relevant desks; the ninth, `disruption_status`, is scoped off the country plane entirely — `has_tag("supply_chain")`, the thematic lane/flow desks, on a 24h window rather than 72h — one unit (`internal_stability`) also opportunistically pulls guarded `vector:world_context` RAG-pilot chunks (§7.9) | 7-phase envelope WAKE→ORIENT(token-budget packed, ≤200 backstop)→PLAN→GROUND→REASON→REFLECT→NARRATE, then the mandatory faithfulness VERIFY | **FINDING** (cited) | yes (one `chat_complete` + the route-resolved faithfulness verify judge) | **2×/day** staggered per unit across the clock (`0 1,13` / `0 4,16` / `0 7,19` / `0 10,22` / `0 2,14` / `0 5,17` / `0 9,21` for the seven broad units; `0 3,15` for `proliferation_watch`; `0 6,18` for `disruption_status`) |
| **`relationship_reifier`** | `llm_planner` (META) | `proposed_edges` co-mention pairs + open facts | small-LLM types each pair → canonical predicate + **signed polarity** | **NEXUS** side-write (`write_nexus`); FINDING receipt | yes (8B-path, 384-tok JSON/pair) | **12h** (`45 */12 * * *`) |
| **`deterministic` / `graph_mining`** | `deterministic` (`sub_handler: graph_mining`) | signed `nexus` rows + the Apache AGE subgraph | `networkx` communities / centrality / proxy-chains | `graph_metrics` sink + **FINDING** | **no** | **12h** (`52 */12 * * *`) |
| **`competing_hypotheses`** (alias `ach`) | `llm_planner` (META) | focal situations × current facts × signed nexuses | evidence × hypothesis matrix + diagnosticity → `confirmed` / `refuted` | **HYPOTHESIS** side-write (`write_hypothesis`); FINDING receipt | yes (LLM proposes set + scores cells, budget-gated) | **12h** (`50 */12 * * *`) |
| **`predictor`** | `stat_forecaster` | a 14d (`336h`) signal window for the target | daily-aggregated AutoARIMA forecast (point + CI) | **PREDICTION** | no LLM in the critical path (optional narrative only) | **RETIRED + STOPPED** — forecast-as-claim (`country_predictor` retired, `india_energy_predictor` cadence-nulled); ~539 historical prediction rows remain in the DB, unread; forecasting now returns only via the scored `forecast_scoreboard` (§7.10) |
| `cross_target_raw` | `llm_planner` | raw signals across N subscribed targets | one multi-target pass | FINDING (`cross_target=true`) | yes | available kind — not in the live bring-up set (no descriptor) |
| `meta_findings_synthesizer` (the COMPOSITIONS) | `llm_planner` (META) | other analysts' verify-passed findings, composed bottom-up: the 7 broad units (+ `proliferation_watch` on nuclear desks) → `country_composition`; the country reads → `region_composition` (5 region frames); the region reads → `world_assessor`; **plus** `escalation_composition`, which fuses the `escalation`-unit head of every desk into one thematic cross-desk read | verify-floored, cited second-order synthesis (a T7 correlation guard caps shared-lineage double-counting) | FINDING (`meta=true`) + its own faithfulness VERIFY | yes | per-country `30 11,23`; per-region `45 11,23`; thematic escalation `30 8,20`; global world `0 0,12` |
| `cross_analyst_correlator` | `llm_planner` | the LIVE composition + unit findings (read-slice repointed off the retired `country_assessor`/`country_predictor`) | contradiction / agreement / blind-spot detector; supersedes prior same-`situation_signature` heads | FINDING (+ mandatory faithfulness VERIFY) | yes | 12h (`45 */12 * * *`) |
| `critic` | `critic` | one analyst output + its `eval.rubric` | LLM judge → per-dimension scores | **CRITIQUE** | yes (heterogeneity-guarded) | 2h (`0 */2 * * *`) |
| `optimizer` (`unit_optimizer`) | `dspy_compile` | one measured unit's traces ⋈ critiques | DSPy + GEPA compile (Dapr Workflow) with a real before/after **faithfulness** delta; human-gated, never auto-promotes on a degenerate/non-positive delta | **PROMPT_MODULE_CANDIDATE** | yes (isolated worker) | weekly (`0 4 * * 1`); the old `country_optimizer` monolith is cadence-FROZEN — descriptor still `state=active`, `fallback_schedule` nulled (§6) |
| `consult_on_demand` | `react_loop` | free-form question + scope predicate | single-turn ReAct over 4 read tools | FINDING | yes | on-demand (no `fallback_schedule`) |
| `deep_consult` | `react_loop` | free-form deep-research question | schedules the deep-consult Dapr Workflow | FINDING | yes (within the workflow) | on-demand (no `fallback_schedule`) |
| **`journal_assessor`** (extension kind) | `llm_planner` (META, in-actor GATHER one-soul arc) | the whole organism via its own `journal_read` self-instruments (its own last entry / consolidation, plus `get_assessments` / `get_graph_structure` / `get_critic_scores` / `get_calibration` / `get_run_health` / `get_budget_status` / …) | one-soul PLAN→GATHER→NARRATE arc; narrate a coherent first-person POV OVER the system | **JOURNAL** → `journal_entries`, OFF-chain (empty `derived_from`) | yes (both phases now on the core plane — gpt-oss / vLLM gather + core-plane voice; Anthropic is reserved for consult/deep_consult only) | **ON cadence** — 12h entry (`0 0,12`) + daily consolidation (`0 2`); an introspective instrument that writes only `journal_entries` and cannot pollute product output (§3.10) |
| **`scorecard_producer`** | `deterministic` (`sub_handler`, META) | already-verified claims (14-day window) per active desk tagged `g20`/`watch` | high-precision banding rules (severity × effective_confidence as an ADMISSION test — H3 retired the one-rung damper, so no rule demotes or promotes); one honest row per desk, aligned to the composition's consumed basis | **SCORECARD** side-write; TRACE_ONLY receipt | **no** ($0) | daily (`40 4 * * *`) |
| **`forecast_scoreboard`** | `deterministic` (`sub_handler`, META) | the `acute_forecasts` pilot table | weekly issue → **exogenous** resolve → count of the acute-binary forecast pilot; abstains on a degenerate p-vector | `acute_forecasts` rows; TRACE_ONLY receipt (never a claim/finding) | **no** ($0) | weekly |
| **`unit_correctness_scorer`** | `deterministic` (`sub_handler`, META) | each unit's findings vs operator gold labels (`unit_reference_labels`) | per-unit faithfulness + correctness-vs-reference (honest-null when a unit has 0 labels) | FINDING (the skill-scoreboard feed) | **no** ($0) | daily |
| **`indicator_tracker`** (I&W) | `deterministic` (`sub_handler`, META) | the two most-recent indicator-bearing findings per unit-stream (`data.indicators[]`) | diffs each pre-registered indicator `id` slug run-over-run; emits a FINDING when a status FLIPS (esp. `not_observed → triggered` — a warning signpost firing); `force_trace_only` when nothing flipped | FINDING | **no** ($0) | 30-min heartbeat (`15-59/30 * * * *`) |
| **`situation_tracker`** (extension kind — continuity P2's ONE ledger writer) | `llm_planner` (META) | each open situation that picked up new **verified** evidence since its own watermark, plus the situation's prior tracked state | asks, per situation: escalate / de-escalate / broaden / no move — one sentence why, citing which items; the answer lands twice — a graded `situation_update` finding (through the full faithfulness verify gate) and append-only `situation_events` rows (migration 0184, the queryable trajectory). First activation 2026-08-09 (audited FSM route) seeded every open situation; real transitions landed on the next tick | **FINDING** (`situation_update`, cited) + `situation_events` side-write | yes | hourly (`41 * * * *`) |
| **`collection_gap`** (I&W) | `deterministic` (`sub_handler`, META) | the banded SCORECARD rows (`kind='scorecard'`) over the window **+ the `source_request` hypothesis backlog** | ranks the STARVED desk × dimension cells (`insufficient-evidence` bands), names why + how persistently + which source classes would feed them; **also writes durable `collection_requirements` rows** (mig 0113) from both inputs, with deterministically-matched candidate sources; `force_trace_only` when nothing is starved | FINDING + `collection_requirements` side-write | **no** ($0) | monthly (`0 6 1 * *`) |
| **`alert_trigger_scan`** | `deterministic` (`sub_handler`, META) | verified state transitions: scorecard bands, high-severity verified findings, contention flips, desk-baseline deviation, watchlist hits, geo convergence — plus the production gauge | seven deterministic trigger classes over durable watermarks (a transition never re-fires); per-desk cap 3 + honest rollup; hands off to the alert-sink dispatcher (§4.4.1). Class 7 (`production_deficit`) inverts the family: it fires when a loop that *should* have produced did not — analyst cadence/production, source production (with `upstream_quiet` / `conversion_stall` / `no_polls` sub-states), backlog drains, judge availability, and prompt/state drift between tree and registry (`GET /v3/system/production-gauge`) | **ALERT** side-write; TRACE_ONLY receipt | **no** ($0) | 10-min (`3-59/10 * * * *`) |
| **`geo_convergence_scan`** | `deterministic` (`sub_handler`, META) | recent signals' geo tags + trustworthy-precision points | two-tier honest binning (1° cells for point-trustworthy geo only; country bins for ISO2) + source-FAMILY diversity ≥3; formation/dissolution edges on the 0091 watermarks (§4.4.2) | ALERT/FINDING side-writes; TRACE_ONLY receipt | **no** ($0) | 30-min (`7-59/30 * * * *`) — **folded into `alert_trigger_scan` (trigger class 6) 2026-07-29; the standalone entry point became a no-op deprecation stub and the descriptor has since been retired live** |
| **`band_calibration_tracker`** | `deterministic` (`sub_handler`, META) | scorecard band transitions + the `band_calibration_claims` table | logs each band transition as a resolvable claim; grades at 14/28-day horizons (`hard_band_at_horizon_v1`); **explicitly NO Brier** (§6.5) | FINDING (structural-exempt) | **no** ($0) | daily (`35 3 * * *`) |
| **`fact_decay_scan`** | `deterministic` (`sub_handler`, META) | open `facts` + their `derived_from` signal timestamps | per-class MISP decay curves + corroborations-as-sightings → the `fact_decay_states` **readout sidecar**; NEVER mutates a fact's confidence (§4.4.4) | FINDING (per-state readout) | **no** ($0) | daily (`52 4 * * *`) |
| **`source_track_record`** | `deterministic` (`sub_handler`, META) | resolved/surfaced `fact_contention` groups past the earned lag | per-source win/loss record (Beta(2,2)-smoothed, Wilson-bounded) → `source_track_records`; fills the arbiter's earned-weight seam (§7.11) | FINDING (readout) | **no** ($0) | daily (`23 4 * * *`) |
| **`narrative_mapper`** | `deterministic` (`sub_handler`, META) | contested-claim families + their carrier sources' publish times | reifies narratives + the directed source-echo graph (`narratives` / `narrative_echo_edges`); DETECT-ONLY, descriptive-not-causal (§4.4.5) | FINDING (readout) | **no** ($0) | daily (`17 5 * * *`) |
| **`desk_baseline`** | `deterministic` (`sub_handler`, META) | per-desk signal/finding counts (28d), neighbour desks | statistical baseline (lags, rolling means, time-since-event, spillover; Poisson-floored sigma) → `desk_baselines`; **NOT a forecast** (§4.4.3) | FINDING (readout) | **no** ($0) | daily (`23 4 * * *`) |
| **`evidence_archiver`** | `deterministic` (`sub_handler`, META) | verified-cited signals with no archived copy | fetches + content-addresses original bytes (SSRF/politeness/20MB/license-gated) → the CAS store + `evidence_archive` sidecar (`ARCHITECTURE.md` §8.6) | FINDING (run readout) | **no** ($0) | 30-min (`19-59/30 * * * *`) |
| **`claim_watch`** | `deterministic` (`sub_handler`, META) | new signals since a durable cursor × the standing `open_question` set | fuses a vector / entity / geo match per (signal, question) pair; an optional bearing gate (8B) + blocking confirm (core plane) may subtract edges when ON; writes `bearing_edges` + `review_flags` and counts a `staleness_debt`. **Flag-only — the closer is not built** (§4.5.3, `SEAMS.md` #49) | `bearing_edges` / `review_flags` side-writes; TRACE_ONLY receipt | matching is deterministic; the optional gate/confirm legs consult the 8B + the $0 core plane when ON | 30-min (`7-59/30 * * * *`); in-tree `draft`, active live |
| **`analyst_traces_retention`** | `deterministic` (`sub_handler`, META) | `analyst_traces` past the policy TTL | since 2026-07-28 a **thin shim** over one shared sweep engine driven by the `retention_policies` table (mig 0109) — its sibling `signals_retention` (no descriptor in-tree; operator-wired) is the same shim over the same engine. Both seeded policies carry `ttl_days = 0` = **sweep disabled** | TRACE_ONLY | **no** ($0) | daily (draft, OFF machinery) |

The ten 2026-07 rows (from `alert_trigger_scan` down) all **ship
`state: draft`** — registration creates no live actor, and flipping
`draft → active` is a deliberate deploy/operator step; on the reference
deployment the alert + readout family runs on cadence. Their behaviour is
detailed in §4.4 (the alert loop + readout family), §4.5 (standing open
questions + `claim_watch`) and §6.5–§6.6 (the calibration + gold-set legs).

Each cadence pairs with a `cooldown_seconds` held **below** the interval — a
cooldown stamped at run-completion that equals the interval lands past the next
fire and silently halves the cadence (the 6h→12h / 12h→24h trap noted in the unit
descriptors). The **twelve** `OutputKind` values are FINDING, SITUATION,
HYPOTHESIS, PREDICTION, ALERT, META_FINDING, CRITIQUE, FACT, NEXUS,
PROMPT_MODULE_CANDIDATE, JOURNAL, SCORECARD (`provenance/kinds.py`) — `JOURNAL`
lands OFF the fact/finding/nexus chain (§3.10), and `SCORECARD` is the
deterministic banded per-country verdict (§3.11).

### 3.1 `inline_target`

`inline_target.py`. The base cognitive kind. Reads one target's substrate slice
(signals produced for that target), runs an LLM planner over a sort-and-trim of
the most relevant signals, and emits one structured `FindingPayload` describing
the most significant patterns. The run is structured as an envelope —
WAKE, ORIENT (deterministic relevance trim), PLAN (deterministic prompt + budget
selection), **GROUND** (optional — when the analyst opts into knowledge grounding,
prepend the dated current-world-state preamble; §7.9), REASON+ACT (the LLM call,
wrapped as a DSPy module), REFLECT (parse + validate JSON into a payload; malformed
JSON downgrades to an unstructured finding rather than crashing), NARRATE (stamp
`derived_from` lineage and tags), PERSIST (the actor host does the substrate
write). The kind handler stays pure so the optimizer can replay it
deterministically. This is the kind the **nine bounded reasoning units**
instantiate — one narrow question each. Seven fan out across the 32 country desks (the
19 G20 members plus the 13-desk watch tier; §3.11, §8); the eighth,
`proliferation_watch`, is narrower — tag-scoped to only the ~8 nuclear-relevant
desks; the ninth, `disruption_status`, is scoped to a different desk family
altogether — `has_tag("supply_chain")`, the thematic lane/flow desks — which is
the demonstration that "desk" means *registered subject-frame*, not *country*:
the ninth unit needed no new analyst kind and no new code path, only a tag.
This is also the kind that carries the
**knowledge-grounding** injection (§7.9), including — on one unit
(`internal_stability`) — the live opportunistic, guarded `vector:world_context`
RAG pilot (§7.9); a
landed unit finding then clears the mandatory **faithfulness verify** pass (§6.2).
The earlier monolithic `country_assessor` was an `inline_target` too, now retired
and stopped — nothing in the spine reads it, though its ~1.2k historical findings
remain in the DB, unread (§3.11).

#### 3.1.1 How `inline_target` is defined + wired

`inline_target` is worth tracing end-to-end because it is the archetype every
LLM-bearing finding kind follows: a declarative descriptor, a build-time wiring
pass that resolves the descriptor's references into a runnable handler, and a
pure run-time envelope. Three real files carry it — the kind module
`data/analysts/inline_target.py`, the builder branch
`runtime/analyst_deps_builder.py`, and the descriptor schema
`data/schemas/analyst.py` — instantiated live by the nine bounded-unit
descriptors (`descriptors/analyst_leadership_transition.yaml`,
`analyst_energy_security.yaml`, `analyst_escalation.yaml`,
`analyst_narrative_coordination.yaml`, `analyst_internal_stability.yaml`,
`analyst_military_posture.yaml`, `analyst_economic_coercion.yaml`,
`analyst_proliferation_watch.yaml`, `analyst_disruption_status.yaml`; §3.11). The earlier monolithic
`descriptors/analyst_country_assessor.yaml` was the original archetype and is now
**retired** (live head `state='retired'`, removed from bringup); the values traced
below are the units'. `analyst_world_assessor.yaml` was repointed off
`inline_target` to `meta_findings_synthesizer` — it is now the world composition
(§3.11), no longer an `inline_target`.

**The descriptor anatomy.** An `AnalystDescriptor` (`schemas/analyst.py:578`)
declares:

- `identity.kind: inline_target` (`schemas/analyst.py:185`) — the registry key
  `discover_analyst_kinds` resolves to the module.
- `method.kind: llm_planner` + `method.prompt_module` + `method.llm.primary`
  (`schemas/analyst.py:399,409,419`). `llm.primary` is a `stack_ref`
  `FactoryValue` (`raw: llm.primary.openai_compat`, `expected_family:
  llm_provider`) the builder resolves against the live stack; `method.llm`
  also carries `max_tokens` and the per-analyst `budget_tokens_per_day`. Each
  bounded unit caps `max_tokens: 1536` / `budget_tokens_per_day: 300000`, and —
  the key addition — declares a `method.llm.verify` stack_ref
  (`raw: llm.primary.openai_compat`) that names the faithfulness judge's
  descriptor default (§6.2) — the **same core model** as the producer. The
  effective judge resolves through a ladder whose top rung is the
  `LEGBA_JUDGE_STACK_REF` env override; the reference deployment repoints every
  judge call **cross-family** at a hosted Gemma judge that way (same-model
  judging shares blind spots — the descriptor default remains self-hostable). Units carry their
  prompt **inline** as `method.system_prompt`, not a
  `prompt_module` (the T7 unit-factory pattern — a unit is just a topic-scoped
  descriptor, no new Python module). Since the Phase-V voice wave every unit
  prompt also carries the **voice contract**: the read opens with an as-of
  line copied from the printed slice header (run date + window — the prose
  cannot drift from the query that built it), the stock template sentences
  are banned with a replacement judgment shape, machine internals
  (microsecond timestamps, internal scores) are barred from prose, and
  absence claims carry collection scoping; the anchors are test-pinned per
  descriptor so a prompt edit cannot silently drop them.
- `subscription.targets` (`schemas/analyst.py:258`) — a `predicate` +
  `time_window` (`schemas/analyst.py:223,225`). **The presence of a `targets`
  block is the per-target fan-out switch**: each unit declares
  `predicate: 'has_tag("g20") or has_tag("watch")'`, `time_window: 72h`, and the
  actor fans out one worker run per matched desk. A META analyst (the compositions,
  the scorecard) **omits** `targets` entirely (`AnalystActor._cadence_targets()`
  returns `None` → exactly one global run with `target_filter=None`). One field
  flips a kind between the 32 per-desk runs and one global run. Adding a desk is
  register-a-target (tag it `g20` or `watch`), no code change.
- `cadence.fallback_schedule` + `cadence.cooldown_seconds`
  (`schemas/analyst.py:455`) — each unit fires **2×/day** on a staggered hour pair
  (e.g. `0 1,13 * * *`) with `cooldown_seconds: 39600` (11h, held below the 12h
  interval per the §3.0 cooldown trap); the nine units stagger across the clock
  (`proliferation_watch` at its own `0 3,15` slot, between `internal_stability`
  and `energy_security`; the supply-chain `disruption_status` at `0 6,18`) so no
  hour stacks the whole fan-out into one budget bucket.
- the `grounding` block (`schemas/analyst.py:596` → `GroundingBlock`) —
  `enabled: true`, `scope: [target_geo, slice_entities]`,
  `sources: [substrate, situations, graph_structure]`, `max_facts: 30` (the Tier-1
  current-world-state injection; §7.9).
- `action_packs` (`schemas/analyst.py:590`) — the bounded units grant **none**;
  the retired `country_assessor` monolith was the analyst that granted the
  `media_processing` / `incident_response` / `escalate_finding` seed packs
  (§5 / §5.0).
- `outputs` bindings (`schemas/analyst.py:586`) — the units bind **no** output
  channels; the `FindingPayload` lands in `analyst_outputs` via the kind's default
  `OUTPUT_KIND.FINDING` regardless. (The world composition binds a single
  `a2a_skill` `intelligence.world_assessment`; §3.11.)
- `eval.rubric` (`schemas/analyst.py:515`) — the weighted-dimension JSON the
  `critic` grades the finding against (each unit ships its own topic-scoped
  rubric); required for the critic→optimizer loop. The critic now runs on the SAME
  core self-hosted plane as the analyst (`llm.primary.openai_compat`), so
  `allow_self_correlated: true` — it is no longer a cross-provider check (§3.8 / §6).
  The hosted Anthropic plane (`claude-opus-4-8`) is reserved for the consult /
  deep-consult kinds only.

**The build-time wiring.** At startup `discover_analyst_kinds`
(`data/analysts/__init__.py:104`) walks the kind modules and registers each by
its `KIND_NAME` (`inline_target.py:92`). When the deps-builder binds a
descriptor whose `identity.kind` is `inline_target`, it dispatches into
`_build_inline_target` (`analyst_deps_builder.py:324`), which:

1. **resolves the LLM** — the `method.llm.primary` StackRef is resolved via
   `build_llm_handler_from_stack_component` (`analyst_deps_builder.py:218`,
   called at `:342`) into a live chat handler.
2. **resolves the effective system prompt** → the string the runner uses
   (`analyst_deps_builder.py:347`), from `method.prompt_module` **or** — for the
   bounded units — the inline `method.system_prompt`.
3. **applies the GEPA promoted-prompt override** — `resolve_promoted_system_prompt`
   (`analyst_deps_builder.py:355`) reads the operator-promoted champion
   instruction for this analyst id from Postgres and overrides the static
   system prompt when one is promoted (closing the optimizer's promotion loop;
   §6.4). (For the bounded units this write-back is a **declared follow-up seam** —
   the `unit_optimizer` *measures* a candidate today but a promoted unit prompt is
   not yet auto-fed back into unit synthesis; §6.3.)
4. **installs the grounding hook** — `_build_grounding_hook`
   (`analyst_deps_builder.py:360`, defined at `:378`) returns a closure that
   prepends the dated current-context preamble when `grounding.enabled`, else
   `None`.
5. **constructs the runner** — `InlineTargetRunner(llm, max_tokens=…,
   system_prompt=…, grounding_hook=…)` (`analyst_deps_builder.py:368`;
   `InlineTargetRunner` at `inline_target.py:683`), returned alongside the
   kind's `OUTPUT_KIND`.

**The run-time 7-phase envelope.** Each run is a deterministic envelope around a
single LLM call so the optimizer can replay it (`inline_target.py`):

- **WAKE** (`:546`) — open the envelope.
- **ORIENT** (`:549`) — deterministically sort by relevance and **pack signals
  under the input-token budget** (the real bound); `_MAX_INPUT_SIGNALS = 200` is
  only a hard backstop count so a flood can't blow the packer, **not** a fixed
  "newest 20" trim (`:244`, `:305`).
- **PLAN** (`:584`) — render the prompt + select the token budget.
- **GROUND** (`:592`) — when grounded, prepend the dated "AUTHORITATIVE CURRENT
  CONTEXT" preamble (§7.9); a no-op when the hook is `None`.
- **REASON** (`:614`) — **one** `chat_complete` call (`_reason_via_llm`, `:620`).
- **REFLECT** (`:642`) — coerce + parse the JSON into a `FindingPayload`
  (`_coerce_finding`); malformed JSON downgrades to an **unstructured** finding
  (`tags=["unstructured"]`, `:337`) rather than crashing the run.
- **NARRATE** (`:653`) — stamp `derived_from` lineage, tags, and `key_entities`
  (`_narrate`, `:372`).

The run writes **one** `FindingPayload` (`OUTPUT_KIND = OutputKind.FINDING`,
`inline_target.py:100`); the actor host performs the substrate write (PERSIST,
`:661`). The handler itself stays pure — no I/O inside the kind — which is what
lets the optimizer replay a recorded trace deterministically.

### 3.2 `cross_target_raw`

`cross_target_raw.py`. The broader-substrate sibling of `inline_target`: the
analyst's subscription predicate resolves to a set of `target_ids` at bind time,
the kind reads the union of those targets' raw signals, runs a single multi-target
LLM pass, and emits a `FindingPayload` tagged `cross_target=true` with the
contributing target ids. It supplies its own `READ_SLICE` because its window
crosses targets.

### 3.3 `meta_findings_synthesizer`

`meta_findings_synthesizer.py`. Reads **other analysts' first-order findings**
(rows in `analyst_outputs` with `kind='finding'`), not raw signals, and synthesizes
them into a second-order `FindingPayload` marked `meta=true` with
`contributing_analysts`. The lineage walker backtracks one hop to the first-order
findings and two hops to the underlying signals. Inputs are capped (default 15
findings) and its token budget is the smallest of the LLM kinds — its inputs are
already structured. This is the kind that backs **every** composition leg of the
reasoning spine (§3.11): `country_composition` (a `targets` block → per-country
fan-out over the 7 broad units, plus `proliferation_watch` on nuclear desks), `region_composition` (5 region frames, per-region
fan-out over the country reads), `world_assessor` (no `targets` block → one global
run over the 5 region reads), and the thematic `escalation_composition` (no
`targets` block → one global run that fuses the `escalation`-unit head of every
desk). When a descriptor declares `method.llm.verify`, its `READ_SLICE` also
applies the faithfulness-verify floor, so a composition admits only verify-passed
sub-claims; the thematic and world branches additionally run a T7 correlation
guard that collapses shared-lineage evidence so two desks resting on one wire
signal are not double-counted.

**Two-tier composition evidence (2026-07, flag-gated).** Under
`LEGBA_COMPOSITION_TIERED_EVIDENCE` (**default OFF in code**; enabled on the
reference deployment) the composition read splits into two explicitly-labeled
tiers. The flag used to move the basis bar as well — from the historical `0.0`
default to `0.50` — but since the 2026-08-15 raise of
`meta_findings_synthesizer.DEFAULT_VERIFY_FLOOR` the bar is `0.50` either way,
so flipping it now only adds the periphery section:

- **The basis** — verify-passed findings whose
  `effective_confidence = min(confidence, faithfulness)` clears the **0.50
  floor** (`LEGBA_COMPOSITION_VERIFY_FLOOR` pins it explicitly; the tiered
  default mirrors the scorecard's faithfulness floor). Only the basis may
  carry unhedged assertions.
- **The periphery** — below-floor / unverified claim-bearing findings, capped
  at **8** (worst-first by severity then recency, body excerpts capped), fed
  to the LLM under an explicit weak-evidence label. Periphery material is
  admissible **only as hedged context**: a composed clause resting *solely*
  on periphery citations without hedged attribution draws a **counted**
  `unhedged_periphery_citation` soft verify failure (§6.2) — the hedging
  contract is enforced at grade time, not requested politely. Conflicts
  between periphery and basis are surfaced as **"tensions worth watching"**,
  never silently merged.

Each tiered composition records an additive `data.evidence_tiers` envelope —
"built on N verified + M weak signals" (`basis_count` / `periphery_count` /
`periphery_ids` / `floor`) — and the periphery ids join `derived_from`, so
the weak leg is visible in lineage rather than laundered.

### 3.4 `cross_analyst_correlator`

`cross_analyst_correlator.py`. A broad subscription over many analysts' outputs
(findings, predictions, meta-findings). Its prompt frames three explicit detectors
so a small/fast LLM keeps the discrimination sharp:

- **contradiction** — two or more analysts disagree on the same target / topic.
- **agreement** — a cluster of analysts converge on the same claim.
- **blind_spot** — the substrate clearly tracks a topic but no analyst output
  mentions it.

It writes one `FindingPayload` whose `data` carries `correlation_type`,
`referenced_outputs` (the UUIDs the LLM explicitly cited), and
`referenced_analyst_ids`. Lineage queries can use the broad `derived_from` (every
output read) or the narrow `referenced_outputs`. A "global situational-awareness
coordinator" is not a special construct — it is a `cross_analyst_correlator` with
a wide subscription.

Three 2026-07-06 fixes make it a real reader instead of a blind one. (1) Its
`READ_SLICE` was **repointed off the retired `country_assessor` / `country_predictor`**
onto the **live composition + unit layer** — it now correlates real verified
findings instead of degrading to "insufficient data" blind_spots over an empty read.
(2) It **enters the mandatory faithfulness verify pass** like every other finding
producer, so its `effective_confidence` is clamped by `min(confidence, faithfulness)`
(§6.2). (3) It **supersedes prior same-relationship heads via a stable
`situation_signature`** (`_situation_signature`, from `correlation_type` + the sorted
referenced-target set): a fresh run about the same relationship supersedes the older
head via the write-path fold rather than piling up duplicates, and a `blind_spot` head
**decays only when its scope is revisited** — a still-real coverage gap is re-asserted
each ~12h cadence and its fresh head supersedes the prior, while a gap the correlator
has stopped asserting expires on a decay TTL (M17). (A `0079` reversible sweep closed
the historical stale heads left over from the old read slice.)

### 3.5 `consult_on_demand`

`consult_on_demand.py`. The one kind with **no scheduled cadence** — purely
on-demand, dispatched via the `legba_consult` MCP tool or the operator consult
panel (`POST /api/v1/consult`). An A2A skill (`intelligence.consult_on_demand`)
is *wired to mount* when the A2A trio is threaded into `build_dapr_host_app`, but
that router is **not mounted on the production runtime** (`/a2a/skills` returns
404; xfail-tracked) and is operator-gated by `LEGBA_A2A_ENABLED` — it is a
tracked seam, not a live dispatch path. It takes a natural-
language question plus an optional scope predicate and runs a **single-turn ReAct
loop** (not the seven-phase envelope), capped at `MAX_TOOL_ROUNDS` (6). Each round
the LLM either returns a final answer or requests a tool; after the cap a forced
final synthesis turn always yields a structured answer. Its tool whitelist is the
**`substrate_read` action pack (§5.0) — 17 read-only tools, all live today**:
the four base primitives `search_signals` / `query_facts` / `inspect_entity` /
`vector_search` (semantic near-neighbour over the signal embeddings, now wired —
SEAM #11 embedder RESOLVED), `search_context` (semantic search over the curated
Lane-4 reference corpora in Qdrant — `tradecraft` and `world_context`, §7.9), the
richer investigative readers `query_nexuses` / `query_hypotheses` / `get_timeline`
/ `compare_targets`, the graph traversal readers `query_paths` / `find_proxy_chains`
/ `query_brokers`, the finished-intelligence readers `list_findings` /
`list_situations` / `query_predictions`, and the consult-surface navigation readers
`list_targets` / `list_sources`. Every call is **governed** — routed through
`Agency.run_pack_tool` and ledgered in `action_pack_invocations` (§5). Write-back
tools are deliberately excluded — consult is a read over substrate. It emits a
`ConsultResponsePayload` (carried into substrate as a finding, and returned
directly to non-runtime dispatchers) with `cited_substrate_refs`.

**Per-request plane picker.** Each consult / deep_consult request may pick which
registered LLM plane answers it: `model = "opus"` (the Anthropic Opus plane, billed,
**the default** — omitting the field preserves prior behaviour) or `model = "core"`
(the free self-hosted core plane). A server-side allowlist maps the friendly value
to a component id (`consult_api.py` — `"opus" → llm.anthropic.opus_4_7`,
`"core" → llm.primary.openai_compat`), so the client never names a component. It is
**fail-closed**: if a chosen non-default plane can't be honored the run raises rather
than silently billing the default, and a provider outage surfaces as a graceful HTTP
503 naming the *other* plane (not a bare 502). Budget accounting keys off the chosen
plane; the shared per-day consult token cap still binds on both. The Consult and
DeepConsult panels carry a matching model dropdown ("Opus (Anthropic · billed)"
default / "Core (free)") that remembers the last choice.

### 3.6 `predictor`

`predictor.py`. Takes a window of recent signals (event counts, optional sentiment),
fits a daily-aggregated **AutoARIMA** forecaster (`statsforecast`, chosen over
Prophet/ETS for conformal prediction intervals and graceful behaviour on short
15–30 day series), and wraps the numeric forecast in an **optional** LLM narrative
that cites the input signals. If no LLM handler is supplied, or it raises, the
predictor emits the numeric forecast with a terse fixed narrative. It writes a
`PredictionPayload` (point estimate, CI bounds, horizon, narrative) via
`OUTPUT_KIND = PREDICTION`.

### 3.7 `deterministic`

`deterministic.py` + `deterministic_handlers/`. A dispatcher kind: the bound
descriptor's `options.sub_handler` selects which pure-Python sub-handler runs. **No
LLM** — all work is over already-materialized substrate slices (networkx for graph
mining, scipy/numpy for stats, SQL upserts for maintenance). Token usage is always
zero. Each sub-handler emits a typed payload whose `data` carries the structured
result and a short human-readable `body`. Sub-handlers are decoupled at import time;
adding one is a new module plus an entry in `SUB_HANDLERS`. The full sub-handler
set is in §4.

### 3.8 `critic`

`critic.py`. Reads **one** analyst output plus the analyzed analyst's
`eval.rubric`, runs an LLM judge against the rubric, and writes a `CritiquePayload`
carrying per-rubric-dimension scores, an overall confidence, and a `revision_delta`
the optimizer consumes as a candidate-mutation hint. A **heterogeneity guard**
(`_assert_heterogeneous`) requires the judge model to differ from the analyzed
model unless the analyzed descriptor opts into `eval.allow_self_correlated` — a
model grading its own output is correlated noise, not signal; the kind raises
`SelfCorrelatedJudgeError` so the runtime DLQs rather than landing a self-graded
row. A missing rubric is also a hard failure so the gap surfaces. The per-run
critique lands in `analyst_outputs` (`kind='critique'`); a separate runtime-emitted
trace-level row in `analyst_critiques` (keyed by `run_id`) is what the optimizer's
training query joins against.

### 3.9 `optimizer`

`optimizer.py`. The self-improvement kind. See §6.

### 3.10 `journal_assessor` — the first-person reflective voice

`journal_assessor.py` + `descriptors/analyst_journal_assessor.yaml` /
`descriptors/analyst_journal_consolidator.yaml`. Every other meta-analyst cuts
**one** slice of the substrate; the journal is the **one** analyst pointed at the
whole organism — its own self, state, and flow — narrating a coherent
first-person point of view *over* the rest of the system rather than synthesizing
another finding about the world. Its thesis: *"Poetry without evidence is noise.
Evidence without perspective is just a log file."* It was built and live-validated (a real off-chain entry, `honesty_flags` forced
deterministically from substrate metrics, receipt-chained, in-voice), and it
**runs on cadence** as an introspective instrument: the entry tier
(`journal_assessor`, `fallback_schedule: "0 0,12 * * *"`) and the consolidation
tier (`journal_consolidator`, `fallback_schedule: "0 2 * * *"`) both tick (§3.0).
Because it writes **only** `journal_entries` — off the fact/finding/nexus chain —
it can never pollute product output, so running it live carries no risk to the
reasoning spine. It is an observability / self-narration surface **beside** the
spine, not a producer **in** it. (Routing a journal reflection back into the
system as an actionable change — via the human-gated `journal_proposals` queue —
is a **future** item, not yet wired end-to-end.)

**Off the fact/finding/nexus chain — a perspective OVER the chain, not a member
of it.** This is the single most important framing. A journal row is the **11th
`OutputKind`** (`OutputKind.JOURNAL`, §3.0) and lands in a **dedicated
`journal_entries` table** (migration 0048), **never** `analyst_outputs` and never
a `fact` / `finding` / `nexus`. It carries an **always-empty `derived_from`** and
the `journal_entries` table is deliberately **absent from the lineage catalog**
(`lineage_api._SUBSTRATE_TABLES`), so a downstream lineage walk from a
fact / situation / nexus can **never** surface a journal node. Its citations are
direction-asymmetric: they live only in `claims` / `cited_substrate_refs` (an
up-only warrant the panel hydrates into chips), never as lineage a chain walk can
descend into. A gating test (`tests/.../test_journal_off_chain.py`) enforces the
never-write-a-fact invariant. **Do not** place the journal inside the
signals → entities/facts → relations/nexuses → situations → assessments lineage;
it is a reflective layer **above / across** that chain.

**One kind, two descriptors (the tier IS the descriptor).** Both descriptors
declare `identity.kind: journal_assessor` (`OUTPUT_KIND = OutputKind.JOURNAL`);
`run_method` selects the entry kind from `identity.id` — no mode flag. The
**entry tier** (`journal_assessor`) fires every 12h (`0 0,12 * * *`,
`cooldown_seconds: 42000`) and narrates the freshest window. The **consolidation
tier** (`journal_consolidator`, same kind, distinct id) runs daily at 02:00 UTC
(`0 2 * * *`, `cooldown_seconds: 79200`) and **distills** its prior consolidation
plus recent entries (via `get_journal_delta`) into one forward-carried narrative
(build-on-don't-repeat), emits `entry_kind='consolidation'`, and fires
`supersede_prior_consolidation` — closing the prior open consolidation and opening
this one (a partial-unique index enforces at-most-one open consolidation). Like
`world_assessor`, both are **META** analysts: no `subscription.targets`, so one
**global** run per cadence tick (`target_filter=None`).

**The engine and the per-phase LLM split.** `method.kind: llm_planner` — the
**in-actor agentic GATHER envelope** (a one-soul staged arc PLAN → GATHER →
NARRATE, the persona re-loaded every phase as the attention mechanism), **not**
the `deep_consult` Dapr workflow (that path rides the broken long-activity
round-trip, task #86). The GATHER loop is capped at `max_rounds: 6` (a hard
ceiling). **Both phases now run on the core plane.** The two-handler structure is
kept — the heavy GATHER investigation loop runs on the **local gpt-oss / vLLM plane**
(the core OpenAI-compatible `llm.primary.openai_compat` stack component, with a
"Reasoning: high" directive injected into the gather system prompt only), and the
**voice** — the in-voice field-notes seam and the NARRATE synthesis — is a *second*
handler (`method.llm.narrate`) that **also resolves to `llm.primary.openai_compat`**
now (it previously ran on the Anthropic Opus plane). So the journal costs **no
Anthropic spend at all**: the billed Anthropic Opus plane is reserved for the
on-demand consult / deep_consult kinds only (§3.5). The deps-builder still reads the
optional `method.llm.narrate.raw` (`method.llm` is an open dict, no schema change)
and resolves that second handler — an analyst without `method.llm.narrate` falls
back to the single primary handler, byte-unchanged.

**Packs and propose-and-gate — the hygiene invariant.** The journal is granted
**only two** packs — `journal_read` (14 read tools including 9 self-instruments:
`get_assessments`, `get_graph_structure`, `get_structural_balance`,
`get_critic_scores`, `get_calibration`, `get_run_health`, `get_source_health`,
`get_budget_status`, `get_journal_delta`) and `journal_propose` — and **both are
non-write-fact**, the grant-layer backstop for the never-write-a-fact invariant.
The journal writes **only** its own entries and consolidations directly.
**Everything outward** — a correction, a change, or a `self_revision`,
**including changes to its own instructions** (`propose_self_revision`; protected
sections auto-reject) — goes to the **human-gated `journal_proposals` queue**,
never a live table; a human accepts or rejects, and the accept path runs an
idempotent per-kind apply worker. Its only un-gated effect is its own continuity:
it reads its own last entry plus current consolidation into its next run. It never
touches another analyst directly — *it can write its own next breath but cannot
rewrite its own rules without the operator.*

**API + UI.** `GET /api/v1/journal` serves the open consolidation plus the entry
stream; `GET /api/v1/journal_proposals` and the `POST
/api/v1/journal_proposals/{id}/accept|reject` endpoints drive the operator review
surface (reject requires a `decision_reason`). The `system.journal` UI panel
renders entries with provenance chips that deep-link to the cited record, styling
`[needs_citation]` / perspective spans distinctly. The personas are
`legba.prompts.journal_assessor:JOURNAL_SYSTEM` (entry) and
`legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM` (consolidation).

**The voice roster + two honesty guards (2026-07).** The journal has since
grown from two tiers into a roster: the 12h `entry` beat and the daily
consolidation are joined by a weekly third-person **`chronicle`**, four
falsifiable-prior faculty **`lens`** reads (trend / base-rate / capability /
intent), and a **`lens_diff`** chorus pass that narrates where the four
faculty reads agree, split, or outlie — all append tiers on the same
off-chain kind (the tier remains the descriptor; see `DATA_MODEL.md`,
"Several tiers, one kind"). Two deterministic guards ride the shared
finalization so the voice cannot fabricate numbers:

- **The numeric-fabrication guard** (`_source_health_number_check`,
  `journal_assessor.py`). Any whole-fleet source-health count the prose
  asserts (`active_total` / `total_wired` …) is validated against the
  deterministic `get_source_health` read; a mismatch flags the entry
  **`source_health_fabricated`** with the mismatches recorded as an honesty
  step — annotate, never rewrite: the prose is preserved so the fabrication
  stays visible. Degrade-not-drop on a failed read (no tool binding / no
  claim / read failure → no flag, never a fake pass). It fires on every
  tier, including the lens reads that motivated it.
- **The empty-read fallback.** A faculty lens whose narrate comes back
  *empty* gets exactly one fallback pass redirected to reason over the
  **verified tower corpus** (its stored, verified material); a read still
  empty after the fallback stays honestly empty — content is never
  fabricated to fill a cycle.

> **Honest caveats.** The `change`-apply path (on accept of a `change` proposal)
> is import-verified but **not yet exercised against a live registry**; the
> `correction` and `self_revision` apply paths **are** tested end-to-end. The
> `system.journal` panel was tsc-green and fully wired but **pending its first
> real in-browser render** at the time of writing. Wave 5 — a critic + an
> optimizer over the journal's **own voice** — is **future / designed-not-built**,
> gated on first building a critic actuator.

---

### 3.11 The reasoning spine — units → composition → scorecard → verify

The kinds above are the *mechanics*; this is how they compose into the product.
The spine is built bottom-up out of **existing** kinds configured by descriptor —
no new Python kind was added for any leg — and every leg is cited and, where it
makes a factual claim, faithfulness-verified (§6.2). The exemplar domain shown is
geopolitics / the 32 country desks; nothing in the machinery is desk-specific — the
fan-out predicate (`has_tag("g20") or has_tag("watch")`) and the unit prompts are
configuration, and adding a desk is register-a-target with the right coverage tag.

**1 — Nine bounded reasoning units (`inline_target`).** `leadership_transition`,
`energy_security`, `escalation`, `narrative_coordination`, `internal_stability`,
`military_posture`, `economic_coercion`, `proliferation_watch`, and
`disruption_status` are each a
topic-scoped `inline_target` descriptor that answers **one narrow question**.
The first seven fan out one run per desk
(`has_tag("g20") or has_tag("watch")` — the 19 G20 members plus the 13-desk watch
tier: Israel, Iran, North Korea, Pakistan, Taiwan, Ukraine, and the
escalation-risk band Sudan, Mali, Burkina Faso, Niger, DR Congo, Myanmar,
Haiti). The eighth, `proliferation_watch`, is narrower: it fans out instead on
`has_tag("nuclear_watch")`, one run per each of the ~8 nuclear-relevant desks
(`country_g20_{cn,in,ru,us}` + `country_watch_{il,ir,kp,pk}`) — same shape,
same gates, one-eighth the fan-out. The ninth, `disruption_status`, is
tag-scoped the same way but off the country plane entirely —
`has_tag("supply_chain")`, the thematic lane/flow desks, on a 24h window rather
than 72h — which is the demonstration that the tag predicate, not the country
plane, is the fan-out primitive. Each run assembles a cited
72h raw-signal slice **plus a Tier-1 grounding preamble of accumulated facts, signed
nexuses, and situations** (§7.9) — so a unit integrates over accumulated state, not
just the newest window; one unit (`internal_stability`)
additionally pulls opportunistic, guarded `vector:world_context` RAG-pilot chunks
under a relevance floor + country filter (§7.9) — then synthesizes a
strict-JSON `FindingPayload` whose prose carries `[N]` markers mapped to the
signal ids **and a machine-checkable `data.indicators[]` block** (pre-registered
warning signposts the I&W analysts diff, leg 6), then clears the mandatory
faithfulness verify pass; the fold
`effective_confidence = min(confidence, faithfulness_overall_score)` is applied at
read time (§6.2). Skill is a **per-unit** number (leg 8 below), never a platform
boast. Adding a unit is a new descriptor, not new code.

**2 — Per-country composition (`country_composition`, `meta_findings_synthesizer`).**
A per-country second-order finding that reads the seven broad verified units
*for that country*, plus `proliferation_watch` on the ~8 nuclear-relevant desks
(listed alongside them in `other_analysts`; the same verify-floored INNER JOIN
naturally yields zero rows for it on the other 24 desks, not an error), and
writes a hedged, cited synthesis. Its `READ_SLICE` honors the run's
`target_filter` and admits **only** faithfulness-verify-passed sub-claims above the
floor — unverified sub-claims never enter the composition as assertable
evidence (under the two-tier evidence flag a labeled, capped periphery of
weak/unverified claims may additionally inform *hedged* context, with unhedged
use a counted verify failure — §3.3). A country whose up-to-eight
units produced no verify-passed sub-claim yields an empty slice, and the kind emits
an honest `confidence = 0.0` "no source findings to synthesize" finding rather than
inventing a read. `derived_from` back-walks one hop to the units, two hops to
their cited signals; the composition then gets its own faithfulness verify over the
`[[ref:<uuid>]]` → sub-claim bridge.

**3 — Per-region composition (`region_composition`, `meta_findings_synthesizer`).**
A region floor inserted between the country reads and the world read
(unit sub-claim → country read → **region read** → world read). Five region frames
are registered as target descriptors — `region_africa`, `region_americas`,
`region_europe`, `region_indo_pacific`, `region_mena` — and the kind fans out one
run per region, reading the verified `country_composition` heads of that region's
member desks and synthesizing one cited, hedged regional read. It carries the same
verify floor and its own faithfulness verify.

**4 — World composition (`world_assessor`, `meta_findings_synthesizer`).**
`world_assessor` was **repointed off `inline_target`** onto
`meta_findings_synthesizer`: it is no longer a first-order raw-signal one-pager
(that verdict-from-nowhere framing was retired). It now composes over the **five
region reads** (not the 32 country reads directly) into one cited, hedged world
view that drills region → country → units → source, and runs the same faithfulness
verify over its own citations. Its `READ_SLICE` world branch degrades honestly: a
region with a present region head feeds in `region` mode; a region with no head
degrades to that region's member `country_composition` heads (`country_fallback`
mode, never silently dropped); a region with neither is named as an unassessed
**GAP** in an appended REGION COVERAGE block. It is a single global run per tick
(no `targets` block) and binds one `a2a_skill` `intelligence.world_assessment`.

**5 — Thematic escalation composition (`escalation_composition`, `meta_findings_synthesizer`).**
A cross-desk *thematic* composition that fuses **one dimension** — the `escalation`
unit — across every g20+watch desk into one global escalation read (rather than
composing all of one country's dimensions, up to eight on nuclear desks). It is a single global run keyed off
a `subscription.substrate.thematic_dimension` marker that routes it to the thematic
branch. A **T7 correlation guard** protects against double-counting: sibling
desk-units can rest on the same underlying wire signal, so the kind detects cited
heads whose `derived_from` sets intersect (union-find over the shared lineage),
collapses each correlated cluster to one independent evidence unit, and caps the
fused confidence to the de-duplicated evidence ceiling. A desk with no escalation
head is named in an appended DESK COVERAGE block, never silently missing.

**6 — Deterministic indications & warning (`indicator_tracker`, `collection_gap`).**
Two LLM-free ($0) deterministic META analysts turn the units' structured output
into a forward-looking watch layer. `indicator_tracker` diffs each unit's
pre-registered `data.indicators[]` block **run-over-run** (per target × source
unit, joined by the indicator `id` slug) and emits a FINDING whenever a status
FLIPS — most importantly a `not_observed → triggered` activation (a warning
signpost firing). `collection_gap` reads the banded scorecard rows and ranks the
STARVED desk × dimension cells (the honest `insufficient-evidence` bands), naming
why, how persistently, and which source classes (the `source_class` taxonomy —
see `DATA_SOURCES.md`) would plausibly feed them. Both set `force_trace_only` when
nothing changed, so an
idempotent re-run never repeats a feed row.

**7 — Banded scorecard (`scorecard_producer`, deterministic META, `SCORECARD`).**
A pure-SQL, LLM-free ($0) META sweep that enumerates every active target tagged
`g20`/`watch` and writes **one** banded `kind='scorecard'`
row per desk from high-precision **rules** over the already-verified
claims (severity tag × `effective_confidence`, banded over a 14-day window;
`effective_confidence` decides ADMISSION only — since H3 (2026-08-27) the engine
**neither demotes nor promotes**, and the retired one-rung damper survives only as
the recorded `damped_would_have_been`). Every band names the verified-claim id it rests on
(`basis`, a real `analyst_outputs.id`), and the row's `derived_from` names those
ids so a lineage walk resolves with zero dangling refs. A dimension with no
qualifying verified claim reads **`insufficient-evidence`** with an explicit
`reason` — never a fabricated band — and a per-claim faithfulness below the floor
excludes the claim with the dedicated reason **`low-faithfulness`**. Since H3 it may
not abstain SILENTLY beside a composition that consumed a verified head for that
desk: the card resolves the composition's `derived_from` and either bands the head
the prose actually used (same guards, no relaxation) or names the consumed ids and
the rule that refused them (`basis_alignment`). A country with no qualifying claim at
all still emits an all-insufficient row (never omit, never invent), so the read
route returns exactly one honest card per active country.

**8 — Skill scoreboard (per-unit eval + calibration + forecast).** How well the
spine actually does is reported honestly, per leg, with no aggregate boast:
`unit_correctness_scorer` (deterministic META) reports **per-unit** faithfulness
plus **correctness-vs-reference** against operator gold labels, **honest-null**
when a unit has no labels; the exogenous calibration **Brier** (§7.4) and the
acute-forecast **BSS** (§7.10) are each reported separately, and a *no-skill* or
*insufficient-sample* result is **published, not hidden**. The 2026-07 wave
adds two more separately-reported legs: **band calibration** — every scorecard
band change logged as a resolvable claim and graded deterministically at
14/28-day horizons, explicitly *without* a Brier (§6.5) — and an **operator
correctness** figure from a weekly pinned gold-set labeling loop, additive and
**never pooled** with the deterministic recall/reference leg (§6.6).

Honest state of the spine today: the live scorecard is a **mix** — some countries
band from verified claims while others (e.g. the US) read all-insufficient because
their units' faithfulness is genuinely low; the correctness-vs-reference gold set
is **tiny** (n≈1, reported insufficient-sample); and the acute-forecast pilot
currently reports **no proven skill** (§7.10). The system surfaces those weak spots
rather than papering over them — that is the point.

---

## 4. The deterministic sub-handler library

`src/legba/data/analysts/deterministic_handlers/`

These pure-Python handlers run under the `deterministic` kind. They are the
substrate's continuous maintenance and structural analysis — the work that needs no
LLM. Coalescing is proven against these handlers (§2.4). Each shares one contract:
`async def handle(inputs, options, deps) -> AnalystMethodResult`.

### 4.1 `entity_resolution` — keeps the knowledge graph current

The source baseline's `ner_multilingual` filter writes entity mentions into
`signals.payload.entities`, but resolving those mentions into the entity substrate
is a separate, **continuous** job this handler owns. Two 2026-07-06 coverage fixes
to that upstream filter widen what reaches this handler at all: **telegram** messages
carry their body in `payload.text` (previously skipped by the field list → 0
entities), so `text` was added to the NER text fields (M12); and **non-Latin scripts**
(Arabic / Cyrillic / Hebrew / CJK) yielded essentially zero spaCy spans, so those
bodies are now **translated to English via the hosted NLLB `/translate` endpoint
before extraction** (M11) — both **forward-only**, so re-enriching the ~9k older
telegram / non-Latin signals is a separate operator job (see the backlog, §7.8).
Each fire folds the next batch
of un-resolved signals (selected `entities_resolved_at IS NULL`, oldest-first,
bounded `LIMIT`) into the graph:

- `entity_profiles` — one node per distinct mention, deduped by
  `lower(canonical_name)`; `location`-class mentions inherit the signal's geocoded
  lat/lon/country so the entity-geo map has points.
- `signal_entity_links` — a `signal→entity` provenance edge (`role=mentioned`),
  `ON CONFLICT DO NOTHING`.
- `proposed_edges` — pairwise `co_occurs` edges among a signal's (capped) mentions;
  confidence accrues on repeat co-occurrence via an upsert.

It stamps `entities_resolved_at = NOW()` on each processed signal so a backlog of
zero-entity signals can never starve newly-arriving ones, and re-running is safe
(upserts + `ON CONFLICT`). This handler is what keeps the live entity graph current
(§8).

**The pre-lookup is alias/article-aware + class-guarded (2026-07-06).** After the
Phase-4 entity merge, new resolutions were **re-fragmenting** — "the Strait of
Hormuz" forked a fresh node from "Strait of Hormuz" — so the dedup pre-lookup now
rewrites an article/case/alias variant onto an existing keeper's canonical surface
before the dedup key, and a **fallback-elected keeper is never class-mutated**
(a geographic keeper adopting an incoming variant is not silently retyped). Two more
gates ride the same write path: a **junk gate** drops numeric / quantity / possessive
surfaces, and a **conservative class validation** corrects only high-confidence
mistypes (a region → `location`, a sports team / org → `organization`) and **never
downgrades a confident `person`**. A `0076` reversible migration folded the leftover
historical fragments (`entity_profiles` 12,257 → 12,144).

### 4.2 `cross_source_dedup` — links duplicate observations

Scans the shared raw signal pool for cross-source duplicates and **links** them —
it never collapses or deletes a raw row, so source-level evidence stays audit-grade.
Two strategies:

1. **Exact content-hash** (mandatory, deterministic) — signals sharing a non-empty
   `content_hash`, whether across distinct sources ("same content via A & B") or
   the same source, are tied to one canonical.
2. **Semantic near-dup via Qdrant** (best-effort, only when a Qdrant client is
   injected and signals carry an `embedding_ref`) — nearest-neighbour links above a
   threshold (default 0.95). Without Qdrant it runs content-hash only.

The canonical is chosen deterministically (earliest `fetched_at`, tie-broken by
smallest UUID) and points its own `canonical_signal_id` at itself. Aliases land in
`signal_aliases` (`ON CONFLICT DO NOTHING`) and the duplicate's
`canonical_signal_id` is updated to the canonical. This is the link the coalescer
keys accumulation on (§2.2, alias-no-double-wake), and what a `canonical_only`
subscription filters against so a target sees one row per duplicate set.

### 4.3 The rest of the library

| Sub-handler | What it does |
|---|---|
| `cross_source_coalesce` | the substrate-wide sibling of `cross_source_dedup` (§4.2): a periodic, no-LLM cross-source **linker** that closes the "same real-world event, different source, different wording, no shared `content_hash`" gap. It embeds recent canonical signals across **all** sources into one shared Qdrant collection and reuses the ingest dedupe's tier-3 (cosine) + tier-4 (temporal window + title Levenshtein) logic to link near-duplicates — **link-never-collapse** via `signal_aliases`, raw rows never deleted. It has **no** non-vector fallback (exact-hash is `cross_source_dedup`'s job), so when its embedding service or Qdrant port is absent it refuses **loud** (emits a `coalesce_unavailable` finding, writes zero aliases — declared SEAM #19) rather than fabricating links. **Off-by-default** (not in the default bring-up set; its `enabled` option defaults False — an operator must opt in) |
| `graph_mining` | community detection, structural-balance triads, proxy-chain mining over the Apache AGE graph. Its "interesting" **hostile-edge shortlist is now vetted** (2026-07-06): canonical class-checked endpoints only (drops NER fragments like `Parl`/`Fed`/`West`/`Leader`), a genuine hostility `rel_type` **and** negative polarity required (a neutral "conducted via" edge isn't relabeled hostile), a subject-attribution guard (so "protesters in X" is **not** emitted as "State X hostile to \<person\>", while a real "State →Targets→ person" survives), and a per-edge quality score |
| `thematic_proposal` | proposes thematic (non-geo) situation frames for uncovered hot topics. **Absence / negation-framed compositions are excluded** from candidacy, and each slug derives from the stable `situation_signature` (one situation = one slug) so proposals **dedup** rather than piling up variants (2026-07-06) |
| `anomaly_detection` | signal-volume rate spikes, sentiment-shift z-scores, novel-entity emergence over `time_bucket()` windows on the primary Postgres pool |
| `structural_balance` | signed-edge triadic balance on the entity-relationship graph (§7.3) |
| `calibration_tracking` | analyst confidence-vs-outcome (Brier score, reliability bins, drift) (§7.4) |
| `adversarial_signals` | flags coordinated / manipulative signal patterns |
| `entity_gc` | garbage-collects stale / orphan entity profiles |
| `fact_decay` / `nexus_decay` | time-based confidence decay of facts and reified relationships. The historical confidence-*mutating* `fact_decay` leg is superseded in practice by the readout-only `fact_decay_scan` sidecar model (§4.4.4) — decay is now computed beside the facts and consumed behind a flag, never written into them |
| `finding_supersession` | links near-duplicate findings for the same situation so a newer finding supersedes the prior one (never a destructive delete; mirrors `signal_aliases`) |

**Future seam:** `finding_supersession` links near-dup findings, but full
finding-level supersession enforcement and situation clustering across the feed are
not yet end-to-end.

### 4.4 The 2026-07 wave — the alert loop + the readout family

Nine deterministic analysts landed in the 2026-07-28 wave — the §3.0 rows from
`alert_trigger_scan` down, excluding `claim_watch`, which came with the
follow-on wave the same day and is covered in §4.5.3.
All are LLM-free ($0), all register `state: draft`
(activation is a deliberate operator step), and the readout half shares one
honesty pattern with the contested-claims sidecar (§7.11): **derived, fully
recomputable, wholesale-refreshed — never a mutation of the primary rows.**
The alert half feeds the modular sink plane documented in `ARCHITECTURE.md`
§8.5 (dispatcher, per-outcome ledger, cooldown coalescing, webhook + ntfy
sinks); this section covers what fires and why.

#### 4.4.1 `alert_trigger_scan` — deterministic triggers over verified state

The rule that makes alerting trustworthy: **a trigger fires on a verified
state *transition*, never on a level, and never twice.** Five trigger
classes:

- **`band_crossing`** — a scorecard band moved. Both directions alert:
  up-ladder = `deterioration` (high), down-ladder = `improvement` (medium);
  moving into/out of `insufficient-evidence` reads `evidence-lost` /
  `evidence-gained` (medium). Bands rest on already-verified claims (§3.11
  leg 7), so this class is verification-gated by construction.
- **`verified_finding`** — a new high/critical-severity finding that has
  *cleared verification*: a faithfulness critique exists and
  `min(confidence, faithfulness) ≥ 0.50`, superseded rows excluded,
  structural-exempt analysts excluded. A **24h late-verify window** means a
  finding whose verify lands minutes after the scan still alerts on the next
  pass rather than slipping through.
- **`contention_flip`** — a contested-claim group's `status` /
  `surfaced_fact_id` changed (the exact fields the arbiter tail moves,
  §7.11), gated on the same verified bar via a citing finding.
- **`baseline_deviation`** — the desk's current 24h count exceeds its own
  28-day `mean + 2σ` **and** an absolute floor (10 signals / 3 findings — no
  σ-noise alerts on quiet desks). **Rising-edge only**: one alert per
  excursion, silently re-armed on the falling edge. The scan prefers a fresh
  (≤48h) `desk_baselines` sidecar row (§4.4.3) and falls back byte-identically
  to the inline computation, stamping `baseline_source` either way.
- **`watchlist_hit`** — operator-defined standing watches (`/v3/watchlist`,
  mig 0105): an **entity** (alias/fold-resolved through the entity substrate;
  unresolvable matches nothing rather than degrading to a text scan), a
  free-text **topic** (Postgres `tsquery`, `simple` config — AND-of-terms, no
  stemming; the limits are stated, not hidden), or a **place** (ISO2
  countries, or point+radius admitted only on trustworthy-precision geo).
  Per-watch cap 3 + an honest rollup; a new watch never pages history
  (`produced_at ≤ created_at` guard).

**Watermark semantics** (`alert_trigger_watermarks`, mig 0091): the first-ever
scan per class **seeds silently** (no alert storm on activation); afterwards a
candidate fires only when the live value differs from the stored watermark,
and the watermark advances **only after the alert row lands** — a rejected
write retries next scan, so delivery toward the sinks is at-least-once and a
fired transition never re-fires. A **per-desk cap** (3, worst-first by
severity) folds the remainder into a single honest `rollup` candidate whose
members' watermarks still advance. Output is `kind='alert'` rows in
`analyst_outputs` with `derived_from` naming the basis; fan-out goes through
the shared dispatcher, and a missing dispatcher is *recorded*
(`fanout_unavailable`), never silently dropped.

#### 4.4.2 `geo_convergence_scan` — convergence without fake precision

Detects distinct source **families** converging on one place. Two honesty
rules do the work:

- **Two-tier binning.** 1°×1° cells are fed **only** by point-trustworthy
  geo — sub-country precision (`region` / `municipality` / `address`) or
  authoritative source geometry; everything else bins at **country**
  granularity off the ISO2 tags. A country-centroid geocode is never
  cell-binned — the contamination that would otherwise manufacture "three
  sources converge on one square degree" out of three centroids.
- **Family diversity, not volume.** A convergence needs **≥ 3 distinct
  source families** — a single outlet (or family of outlets) re-covering a
  story can never fire it.

Formation fires once and dissolution once (info-severity), on the same 0091
watermark table (no migration of its own); a persisting convergence never
re-fires, and the first scan seeds silently.

#### 4.4.3 `desk_baseline` — a falsifiable prior, never a forecast

Computes each desk's own statistical activity baseline into the
`desk_baselines` sidecar (mig 0103): per-desk daily counts for two metrics
(`signal_volume_24h`, `high_sev_findings_24h`) with a feature recipe of lags
(1/7/28d), rolling means (7/28d), time-since-last-high-severity-event, and
**land-neighbour spillover** — pure stdlib, with
`robust_sigma = max(stddev, √mean)` (a Poisson floor so a near-zero desk
cannot produce a degenerate σ). Deviation is `within | above | below` against
`expected ± 2σ` with the same absolute floors the alert trigger uses. The
migration header, the analyst docstring, and the summary finding all state it
plainly: **this is NOT a forecast** — it is a falsifiable prior over our own
collection counts, consumed by the `baseline_deviation` trigger and read at
`GET /api/v1/v3/eval/desk_baselines`.

#### 4.4.4 Fact decay — aging as a readout, consumption behind a flag

`fact_decay_scan` computes per-fact confidence decay into the
`fact_decay_states` sidecar (mig 0098) and **never mutates a fact** (a DB
test asserts the facts rows byte-unchanged). Per-class curves
(lifetime days / decay speed, MISP polynomial `1 − (t/lifetime)^(1/speed)`):

| Class | Lifetime | Speed |
|---|---|---|
| `structural` | 1460d | 0.20 |
| `officeholder` | 730d | 0.25 |
| `affiliation` | 365d | 0.30 |
| `stance` | 180d | 0.30 |
| `event` | 45d | 0.50 |
| *(default)* | 120d | 0.30 |

Seed/curated facts age at 2× the lifetime. **Corroborations are sightings**:
`last_sighting_at` is the newest signal timestamp across the fact's
`derived_from` (falling back to `created_at`, with `sighting_source` recording
which) — a re-sighted fact's clock resets. States are
`fresh | aging | stale | revoke_candidate` (revoke when the decayed
confidence falls to ≤ 0.20, checked first). **Consumption is flag-gated**:
`LEGBA_FACT_DECAY_WEIGHTING` (default OFF) lets the grounding read weight by
decayed confidence; at OFF the grounding SQL is byte-identical (asserted).

#### 4.4.5 Narratives + the source-echo graph — detect-only, descriptive-not-causal

`narrative_mapper` reifies each contested-claim family into a first-class
**narrative** (mig 0102: 1:1 with a contention — carrier sources, first/last
seen, the lead source, the maximum echo lag, value variants) and builds the
directed **source-echo graph** (`narrative_echo_edges`: leader→follower
co-carriage counts, echo ratio, lag statistics, and a `systematic` flag that
needs real floors — minimum co-carriage, ratio ≥ 0.6 inside a 48h window —
before it asserts anything). The honesty posture is carried *verbatim* in the
migration header, the analyst docstring, and every route envelope:
**descriptive, not causal** — an echo edge says "B tends to publish the same
contested claims after A, at this lag", never "A drives B" — and an empty
systematic set is published as-is ("none systematic yet"), never dressed up
as coordination. Read at `GET /api/v1/v3/narratives` + `/narratives/echo`.
(Code note: the in-code classes were renamed `Propagation*`
(`PropagationEdge` / `PropagationEdgeOut`); the tables keep the echo names.)

### 4.5 Standing open questions — keeping a question alive after the run ends

Every other product in this document is an **answer**. An analyst run ends,
its finding lands, and whatever it could *not* resolve evaporates with the
prompt context. This wave makes the unresolved half durable: an **open
question** is a first-class, queryable row that outlives the run that raised
it, that later evidence can be matched against, and that a researcher can be
pointed at on its next natural pass.

Read the whole loop with one caveat in mind, stated up front: **nothing in it
closes a question.** There is no code path anywhere in the tree that moves a
row out of `status='open_question'`. Both organs below only ever *link* — an
answer-pointer or a review flag — and a human reads the result. That is a
deliberate stopping point, not an omission waiting on a bugfix (§4.5.3).

#### 4.5.1 The object, and the two faucets that fill it

An open question is a `hypotheses` row with `status='open_question'` — the
**same table and the same shape** as an ACH hypothesis, no migration, no new
store. The ACH-shaped columns are reused rather than extended: `thesis` holds
the question, `counter_thesis` the competing reading, and the ACH machinery's
`situation_id` / `evidence_balance` / supporting-and-refuting arrays are simply
left at their defaults. `status` has **no CHECK constraint** — the vocabulary
is convention, and what actually identifies the row is a durable marker object
in `diagnostic_evidence`: `{"marker": "open_question_origin", "origin": …}`.
Every writer dedups by JSONB containment on that marker, so both faucets are
re-runnable without duplicating a question.

Two faucets fill the set (a third — the `open_question` agency write tool in
the operator-gated `propose_facts` pack — has existed for some time and, per
the harvest script's own docstring, **had never once been opened by a model**;
that is what motivated the other two):

- **The backfill** (`scripts/harvest_open_questions.py`, `origin: "harvest"`).
  A deterministic, **operator-run one-shot** — there is no cadence wiring for
  it anywhere in the tree — that reads question-shaped state the substrate was
  *already* recording and reifies it. Five harvest classes:
  `scorecard_disagreement` (a dimension the scorecard banded
  `insufficient-evidence` while the live composition head cites a finding of
  that same dimension — recomputed through the *same* pure reducers both read
  surfaces use, so the harvest cannot drift from what the panel shows),
  `freshness_advisory` (a composition head whose input was later materially
  reversed — §"compose-time freshness"), `below_floor` (an open finding graded
  under the verify floor; **ungraded findings are not harvested** — no verdict
  is not a question about the claim), `fact_contention` (an open contested-fact
  group; `collapsed` groups are resolved and skipped), and `collection_gap`
  (a starved desk × dimension cell, keyed *without* the finding id so a gap
  that persists across monthly sweeps stays **one** question rather than a new
  one each month). It is **dry-run by default** — `--apply` writes, `--limit`
  bounds each class (default 100), `--classes` narrows the set.
- **The per-finding faucet** (`origin: "unit_payload"`). Each of the ten
  inline-unit descriptors now permits an optional top-level `open_questions`
  array on the finding payload — `{"question": …, "refs": [<the [N] indices
  that raise it>]}` — with the prompt asking for **at most 3** and the schema
  tolerating **5** (`MAX_OPEN_QUESTIONS`, question ≤ 2048 chars, `refs` ≤ 32).
  The prompt rule is explicit that an empty array is the correct answer when
  nothing is genuinely unresolved: *never invent questions to fill the quota*.
  Conversion runs **post-persist**, keyed `finding_id:sha256(question)`, with
  `derived_from` set to the finding plus each `refs` index resolved back to its
  citation signal (an unresolvable ref degrades to finding-only lineage rather
  than failing). The whole conversion is wrapped degrade-not-break: a faucet
  failure can never fail the run that produced the finding.

#### 4.5.2 The drain — the backlog as a Tier-1 grounding source

`corpus_researcher` (`state: active`) declares `grounding.sources:
[open_questions]` — the backlog is wired in as a **Tier-1 grounding source**,
beside `substrate` / `situations` / `graph_structure` / `narratives`, not as a
bespoke queue. So the researcher does not "run a backlog job"; it simply reads
a prioritized handful of standing questions into its GROUND phase and works
them alongside everything else it grounds on.

Bounded and priority-ordered, per code: at most **8** questions reach the
prompt (a hard resolver cap, independent of what the descriptor asks for),
drawn from at most 1000 candidates, each rendered at ≤ 400 chars. The ordering
key is the interesting part, because it encodes what makes a question *worth*
answering:

1. **Does anything live still rest on it?** A bounded forward walk (≤ 6 hops)
   over `output_consumption` (§4.5.4) computes each question's `live_reach`.
   Questions with a live consumer sort first, and among those, higher reach
   first. A question nothing depends on any more is genuinely lower priority.
2. **Harvest class**, in a fixed order — `below_floor`, then
   `fact_contention`, `freshness_advisory`, `scorecard_disagreement`,
   `unit_payload`, `collection_gap`.
3. **Desk salience**, then **age** (older first), then id as a stable
   tie-break.

If the researcher resolves one, it may tag its finding with the question's
slot (`addressed_question: "Q2"`, resolved against the sink the GROUND phase
filled — a free-text hallucinated tag resolves to nothing). On persist that
becomes **one `bearing_edges` row**, `finding → hypothesis`, weight `1.0`,
`planes=['corpus_research']`, `matcher_version='corpus_researcher_backlog/1.0.0'`.

**It is a pointer, and only a pointer.** The edge writer never touches the
destination question's status or content; the question stays open forever
through this path. What the answer-link buys is that a human (or a later
organ) reading the question can see what has been said about it since — not
that the system has decided the matter. The researcher may also reach outside
the substrate for that answer; the honesty contract governing that is in
`ARCHITECTURE.md` §8.8.

#### 4.5.3 `claim_watch` — the watcher half, and only the watcher half

The complementary direction: not *"go answer this question"* but *"has
anything arrived that bears on it?"* `claim_watch` is a deterministic
sub-handler (no LLM, `$0`) that rides the **existing** change-detection plane
rather than adding an eighteenth bespoke watcher — it reuses the
`alert_trigger_watermarks` table (mig 0091) for its cursor under
`trigger_class='claim_watch'`, and the signal embedder's own Qdrant/embedder
wiring for its vectors.

For each (new signal, open question) pair it fuses **three planes** into one
weight against a `0.45` match threshold:

| Plane | Weight | Rule |
|---|---|---|
| **vector** | up to 0.50 | cosine over the `legba_signals` embeddings; contributes nothing below a `0.45` similarity floor — **measured, not intuited** (14,280 live pairs over 4 desks: genuinely related thesis-vs-body pairs cluster p50 0.36 / p90 0.43, random pairs p90 0.385 / p99 ≈0.444, so the previous `0.60` floor admitted only ~0.2% of *related* pairs and the plane was effectively inert; `0.45` ≈ p90(random) + 0.05 admits the top decile of related pairs) |
| **entity** | 0.20 first, +0.18 each further (max 3 counted) | canonical, merge-folded entity overlap between the signal and the question's two-hop lineage — each shared entity **weighted by its specificity** |
| **geo** | 0.10 | the signal's ISO2 tags intersect the question's desk scope. A tie-breaker, deliberately never a third of the budget |

The entity plane is where the matcher earned its version number, and the
arithmetic is the honesty argument. **Document-frequency specificity**: an
entity carried by *most* of a desk's own questions is weak evidence that
*this* signal bears on *this* question — so its weight ramps from full down to
a `0.25` floor as its document frequency rises past `0.50`, computed from data
already loaded and held inert on desks with fewer than 5 questions (you cannot
estimate a document frequency from one document). The consequence falls out of
the numbers: geo alone scores `0.10`, one shared entity `0.20`, one entity plus
geo `0.30` — all under the `0.45` bar. **Mere desk co-membership can never
constitute a match**, by construction rather than by tuning. A question also
ages: an age factor decays over a 365-day lifetime to a `0.60` floor, a slow
brake rather than the primary conservatism.

The version stamp on every edge (`matcher_version`) records which of those
semantics produced it: `claim_watch/1.0.0` was a boolean entity plane (every
match pinned at a flat weight — i.e. desk co-membership), `2.0.0` graded but
unweighted (most edges pinned at the three-entity cap), `3.0.0` the
specificity-weighted form, `3.1.0` the measured vector floor above, `3.2.0`
the three-lever response below, `3.3.0` the bearing-pipeline seam (a
post-match semantic gate that can only subtract), `4.0.0` the precision train
(blocking confirm leg + desk identity in the bearing prompts + deictic-thesis
refusal + contention liveness/subject anchors), and `4.1.0` the current form
(a consequence-specificity clause in both bearing prompts, article-id URL
dedupe, and the question self-flag surface). Because the stamp rides the row, edges written under
a weaker matcher stay distinguishable rather than being retroactively
dignified — and that is not bookkeeping for its own sake: it is exactly what
let the first precision measurement stratify `3.0.0` from `3.1.0` instead of
pooling two different rules into one meaningless number.

**The measurement, and what it bought (2026-07-29).** 122 stratified pairs were
labeled **out of plane** (frontier model with live search, `labeled_by` stamped,
a calibration pass agreeing on 100% of the decision-critical class). Pairwise
precision split hard by which planes participated:

| Matching planes | Precision | Note |
|---|---|---|
| `vector`+`entity`+`geo` | **1.000** (17/17) | effective n≈9 — seven rows are one event cluster ×2 sibling questions |
| `vector`+`entity` | 0.538 (14/26) | |
| `entity`+`geo` | 0.120 (3/25) | failures shift to **wrong-dimension**, not wrong-theater |
| `entity` only | **0.000** (0/54) | hub-entity bridging (a handful of ubiquitous actors) plus NER junk |
| meta-questions | 0.035 (2/57) | structurally unmatchable by a news matcher |
| substantive theses | 0.492 (32/65) | |
| **pooled** | **0.279** (34/122) | against a recommended ≥0.85 bar — **not met** at round 1 (round 4 later measured the live gated stream at 0.908 — see below) |

The apparent version gap (`3.0.0` 0.456 vs `3.1.0` 0.056) is **sample
composition, not version quality**: the `3.1.0` stratum was the hub-heavy
non-vector tail. Plane mix, not version, is the real axis — and vector
participation is the best class, which is what the `3.1.0` floor change fed
(43 vector-bearing edges within hours, against 1 ever before).

That reading is what `3.2.0` responds to, with three levers each aimed at a
measured failure class rather than at a hunch:

1. **Meta-question exclusion at match time.** Four question classes
   (`collection_gap`, `below_floor`, `freshness_advisory`,
   `scorecard_disagreement`) ask about *our own collection posture*, not about
   the world, so no news signal can bear on them — the 0.035 row above is that
   fact measured. They are dropped **before** the embed budget, the lineage walk
   and the specificity build (cheapest possible point), and they remain ordinary
   open questions in every other read path — this is a matcher-scope decision,
   not a deletion. `fact_contention` is deliberately *not* in the set: a
   contested claim genuinely is a question about the world.
2. **Global hub-entity damping.** The `3.0.0` specificity curve is
   *desk-question-side* — it cannot see that a handful of actors saturate the
   whole signal stream, which is precisely how `entity`-only reached 0.000. A
   second, **signal-side** document-frequency curve over the newest 10,000
   *attributed* signals composes **multiplicatively** onto the first under one
   shared floor (so it can only ever lower a weight, never raise one): full
   credit at or below 2% global df, ramping to the `0.25` floor by 10%. The
   thresholds are absolute and measured, not percentiles. Below 200 attributed
   signals the curve is **inert and reports itself inert** rather than silently
   scoring on noise.
3. **An omnibus damper.** Live-blog and digest signals carry huge entity sets
   and were spraying 15–20 questions each. At most **8** questions per signal
   now survive (strongest by fused weight; the overflow is *counted*, not
   hidden), and same-`canonical_url` duplicates are collapsed keeping the
   **newest** row — which, because rows arrive in ascending fetch order, means
   the batch's final row can never be dropped and the cursor advance is
   untouched.

Post-exclusion pooled precision projected ≈0.49 — a real improvement and still
well short of the bar at the time. The later rounds moved it: round 3 measured
the `3.3.0` bearing-gated stream at 0.267 and bought the `4.0.0` precision
train; round 4 then measured the live `4.0.0` stream at **0.908**, over the
0.85 bar — the closer remains unbuilt as a held operator decision, no longer
as an unmet measurement. Seven new receipt
counters make each lever auditable instead of assumed
(`skipped_meta_questions`, `questions_matchable`, `omnibus_capped`,
`signals_url_deduped`, `global_specificity_sample`, `global_specificity_inert`,
`global_specificity_downweighted`), three surfacing in the receipt title when
they engage. Entity-only matching is deliberately **not** deleted yet: damping
may recover it, and settling that is a re-measurement, not a judgement call.

Two cursor honesties are worth naming because they are the failure modes a
"just poll for new rows" watcher hides:

- **A bounded, counted freshness horizon.** If the cursor falls more than 6
  hours behind the stream head, it skips ahead — but it first computes the
  *exact* number of signals it is abandoning (bounded by a 200k probe, with a
  `skip_count_clipped` flag when that bound binds), logs it, and prints it in
  the run receipt. A watcher that silently skips its own backlog reports a
  clean run while missing everything; this one reports the hole.
- **A tail-hold for the not-yet-embedded.** Signals the embedder has not yet
  covered are *held back* at the tail of the batch rather than matched with a
  dead vector plane — so the cursor cannot outrun the embedder and permanently
  strand a run of signals as "seen, vector-less". It is guarded three ways
  (only at the stream head, only while the plane is otherwise healthy, only
  for rows inside a 2-hour grace) so it can never hold the whole batch.

**What it writes, and what it refuses to write.** A match lands a
`bearing_edges` row (`signal → hypothesis`). Where the question traces forward
to a product that is *still live*, it also lands a `review_flags` row with
reason `new_evidence_bears_on_open_question`. And it reports a per-run
**`staleness_debt`** — the count of open flags whose flagged consumer is still
a live head.

It writes no correction content, never writes back to the flagged producer,
and never recomposes anything. That is true **by construction of what the
handler can write**, not by a runtime toggle — there is no correction path to
disable. (Since `3.3.0`/`4.0.0` the matcher is no longer LLM-free, and that is
worth stating precisely: an optional post-match **bearing gate** on the
self-hosted 8B may REFUSE an edge, and a blocking **confirm leg** on the $0
core plane may drop a gate-passed edge — both can only subtract from what the
deterministic planes selected, both stamp rather than fail closed, and both
are ON on the reference deployment. Since `4.1.0` a **question self-flag**
also fires: evidence bearing on a watched question no product consumes raises
`review_flags` reason `new_evidence_bears_on_unconsumed_question`.) The
closing half (inject a confirmed match into the producer's next natural run,
let supersession correct the record, close the flag) **does not exist
in-tree**. Its match-precision bar was measured across four out-of-plane
rounds — pooled 0.279 at round 1, 0.267 on the gated stream at round 3, then
**0.908** on the live `4.0.0` stream at round 4, over the ≥0.85 bar
(`SEAMS.md` #49) — so building the closer is now a held operator decision.
`staleness_debt` remains honestly a *flags-found, match-unverified* count —
never a corrected-or-closed metric. Since the C3 wave it is readable at
`GET /v3/system/staleness-debt` rather than only from the producing run's
`analyst_traces` receipt; the route computes the headline number with the
matcher's own SQL (mirrored under a byte-equality drift guard, so route and
receipt cannot diverge), reports flags on already-superseded consumers as a
separate count, and carries a hard-`false` `match_verified` field so no reader
can mistake it for a settled number. `review_flags` and `bearing_edges`
themselves still have no per-row read surface.

#### 4.5.4 Forward lineage — what now rests on this?

All of the above needs one thing the chain could not previously answer.
`derived_from` walks **backwards**: *what did this finding rest on?*
`output_consumption` (mig 0106) is the forward index: *what now rests on this
finding?* Consumption is stamped at the point where it is **decided** — inside
the composition's own basis/periphery split, and at the journal's rendered
slice selection — and materialized on the same connection and in the same flow
as the output write, so a consumer row and its edges land together or not at
all.

The distinction the index preserves is the one that matters for triage:
`composition_basis` (a verified, above-floor, **load-bearing** input) versus
`composition_periphery` (a below-floor row admitted only as hedged context,
§3.11). "New evidence contradicts something a live world read is built on" and
"…something a live world read mentioned as a caveat" are different-severity
facts, and the index is what lets the review flag tell them apart. The writer
degrades rather than breaks: a failed consumption write is logged and dropped,
never propagated into the compose.

---

## 5. Action-pack agency

`src/legba/data/analysts/agency/`

An analyst kind reasons; an **action pack** is how an analyst *acts* on the world
outside the substrate — enqueue a media-processing job, emit to an alert channel,
discover new sources. Packs are the Claude-Code-skill model made declarative and
governed: a modular, allow-listed bundle of tools / prompt-fragments / rules /
channels with its own applicability and governor.

### 5.0 ActionPacks — definition + the agency plane

**What an `ActionPack` is.** The pack is a single declarative model
(`data/schemas/action_pack.py:90`) with:

- `identity` (`:93`) — id / name / version (content-hash-stamped at register).
- `tools` (`:94`) — a list of `ToolSpec` (`:48`), each with `name` (`:53`),
  `impl` (`:54`, `module:callable` or `None` → resolved by name to a built-in),
  `config` (`:55`), and `async_job` (`:56`) — when `True` the tool **enqueues an
  async job onto the NATS job plane** instead of running inline.
- `prompt_fragments` (`:96`) + `rules` (`:97`) — the prompt text and guardrails
  merged into a granting analyst's context (the "skill" surface).
- `channels` (`:98`) — emit targets, each a `Channel` with a `kind` in
  `alert` / `webhook` / `a2a_skill` / `nats_stream` / `mcp_tool` / `stix_bundle`
  (`:69`).
- `governor` (`:99`) — a `PackGovernor` (`:75`) of independent caps:
  `max_invocations_per_hour`, `api_rate_per_minute`, `max_cost_usd_per_day`,
  `max_sources_per_window` (+ `crawl_max_depth` / `crawl_max_pages`), over a
  `budget_account` (`:80-87`).
- `applies_to_tags` (`:102`) + `applicability_predicate` (`:103`) — the Starlark
  predicate over the target scope, **compiled at register time** by the model's
  `_compile_predicate` validator (`:105`), so an un-compilable predicate is
  rejected at registration rather than failing open at call time.

**The effective-capability rule.** A pack may run for an
(analyst, target) pair only inside the gated intersection
(`agency/resolution.py:5`, computed in `resolve_pack`, `:222`):

```
analyst.action_packs  ∩  target.allowed_action_packs  ∩  pack.applicability
```

— and then only if the **governor** admits the specific call under budget (§5.2).
`resolve_pack` sets exactly one denial reason per failing leg and merges the
pack's governor with any tightening-only per-binding override (§5.1).

**Where packs are registered.** The pack descriptors live in `descriptors/`
(`action_pack_media_processing.yaml`, `action_pack_incident_response.yaml`,
`action_pack_substrate_read.yaml`, `action_pack_escalate.yaml`) and are
registered via `scripts/bringup_register_action_packs.py`, which POSTs each YAML
to the registry's action-pack descriptor endpoint.

**The agency plane** (`data/analysts/agency/`) is the runtime that enforces all
of the above:

- `agency.py` — `Agency.run_pack_tool`, the **single hard-gate entry point**
  (the module's "one entry point"; §5.3).
- `resolution.py` — `resolve_pack`: the three-way allow-list.
- `governor.py` — `PackGovernorEnforcer`: `precall_check` (allow/deny against the
  caps), `record_invocation` (the ledger insert), `settle` (true cost/units).
- `events.py` — `record_governor_event`: every allow/deny lands a
  `governor_events` row + a best-effort NATS publish on `governor.events.>`.
- `binding.py` — `AgencyToolBinding.run_tool`, the production on-ramp that shapes
  a `ToolCall` and calls `Agency.run_pack_tool` (plus the escalation binding).
- `substrate_read.py` — the **17** read tool handlers (the four base primitives
  `search_signals` / `query_facts` / `inspect_entity` / `vector_search`, the
  corpus-RAG `search_context`, the investigative + graph + finished-intelligence +
  navigation readers; §3.5) the consult kind is governed through.
- `tools.py` — the tool registry, `ToolContext`, the `ChannelEmitter`, and the
  seed handlers (`process_media` enqueues the async job; `escalate` /
  `create_incident` emit to channels).

**The lifecycle.** `Agency.run_pack_tool` (`agency/agency.py:86`) runs one
fail-closed pipeline per call: **RESOLVE** the three-way allow-list →
**TOOL KNOWN** (named in the pack + has a handler) → **GOVERN** (per-pack
`precall_check` + the global budget envelope) → **RECORD** an
`action_pack_invocations` row via `record_invocation` (`governor.py:258`) so the
next call's window sees it → **DISPATCH** the handler (an `async_job` tool like
`process_media` enqueues a `JobEnvelope` onto the NATS job plane rather than
blocking) → **SETTLE** the row's true outcome / cost / units. Every decision —
admit or block — writes a `governor_events` row (`events.py:89`) and publishes on
`governor.events.>`.

**It is live.** The old major-review finding was that the agency plane had
**zero callers**. That is now closed in code: `Agency.run_pack_tool` is reached
from exactly two production sites — the consult kind routes every ReAct tool call
through its `substrate_read` binding (`consult_on_demand.py`, via
`deps.agency_binding.run_tool`), and the actor run path fires the
`escalate_finding` pack when a landed finding crosses the pack's **post-verify**
gate (`runtime/actor_output_emit.py`, `_maybe_escalate_finding` →
`bound.run_tool("escalate", …)`). Two rewires (S3-T4 / S8-T2) sharpened that
gate versus the old "severity tag **or** raw confidence" test:

- **Severity is now a first-class read column** (`analyst_outputs.severity`,
  partial-indexed for `high`/`critical`), no longer only a `severity:<level>` tag
  parsed out of the payload — the emit path resolves it from `payload.severity`,
  then `payload.data['severity']`, then the tag as a fallback.
- **The score gates on the verify-DEMOTED `effective_confidence`, not the raw
  LLM-asserted `confidence`** — `min(confidence, faithfulness_score)` (§6.2). A
  finding the faithfulness verify pass floored therefore **cannot** alert on its
  pre-verify number, nor on a high-severity tag alone (the raw-confidence escape
  hatch is closed). A separate trigger class lets an explicit `alert`-kind output
  escalate on its own.

The escalate pack is tag-scoped to `g20` so a non-G20 target denies with a visible
governor BLOCK, and the A-3 wiring + JetStream binding for `channels.>` /
`governor.events.>` landed in commits `10491f9` / `6938c0b`. **Honest delivery
state:** the escalation currently lands on the NATS subject `channels.escalations`
only — it is **bus-only** (a message on the escalations channel), not a wired
pager / email / webhook to a human; the external-delivery edge past the bus is a
declared follow-up. (Whether specific invocation rows exist *right now* is a
live-DB fact this design doc does not assert — but the call sites, channels, and
ledger writes are wired and reachable, not stubs.)

### 5.1 The three-way allow-list

`agency/resolution.py`. Effective agency is a gated intersection, with each leg an
independently-failing gate so the operator can see exactly which rail denied a call:

```
analyst.action_packs  ∩  target.allowed_action_packs  ∩  pack.applicability
```

- **GRANT** — the analyst declares the pack in its `action_packs`.
- **ALLOW** — the target / domain permits the pack in its `allowed_action_packs`.
- **APPLICABLE** — the pack's own `applies_to_tags` overlap the target's scope tags
  **and** its `applicability_predicate` (a Starlark predicate over the target scope)
  evaluates true. The predicate **fails closed** — an un-evaluable applicability
  gate denies, because the hard gate must never fail open.

`resolve_pack()` returns a `PackResolution` per pack with `effective = granted and
allowed and applicable`; on denial exactly one reason is set. Effective resolution
also merges the pack's governor with any per-binding overrides — overrides are
**tightening-only** (the more restrictive cap wins; an override may never loosen a
pack's own cap).

### 5.2 The per-pack governor and the global budget

Resolution answers *may this pack ever run here*; the governor answers *may it run
right now under budget*. `agency/governor.py` enforces a `PackGovernor`'s caps over a
rolling window against the `action_pack_invocations` ledger, each dimension
independently — any breach is a BLOCK:

- `max_invocations_per_hour` — invocations in the trailing hour.
- `api_rate_per_minute` — invocations in the trailing minute.
- `max_cost_usd_per_day` — summed `cost_usd` for the UTC day.
- `max_sources_per_window` — summed source units in the trailing hour (the
  crawl/discovery cap).
- the system-wide **global budget envelope** — if the runtime's `tokens_cap` is fully
  consumed, the pack call is blocked too (the global gate beats per-pack headroom).

The checks are conservative and pre-call: the forward-looking `estimated_cost` /
`estimated_units` for *this* call are added, so a call that would cross a cap is
blocked **before** it runs. A breach returns a `GovernorDecision` with the precise
`cap_dimension` / `cap_limit` / `observed` for the operator event.

The global budget envelope itself (`runtime/budget.py`) is the wider runtime gate:
a pre-call envelope check + post-call ledger update over `budget_ledger`, with
per-analyst `budget_tokens_per_day` and the global `global_budget_envelope` read
side-by-side. When either dimension is hit the actor auto-demotes (outcomes:
`ok` / `throttle` / `exhausted` / `global_exhausted`).

### 5.3 The one entry point

`agency/agency.py`. `Agency.run_pack_tool` is the single entry point that runs the
whole hard-gate pipeline, fail-closed at every step:

1. **RESOLVE** — three-way allow-list (§5.1). Denial → BLOCK event, the tool never
   runs.
2. **TOOL KNOWN** — the requested tool must be named in the pack and have a
   registered handler. Unknown → BLOCK.
3. **GOVERN** — per-pack governor + global envelope (§5.2). Breach → BLOCK.
4. **RECORD** — an admit lands an `action_pack_invocations` row (so the next call's
   window sees it) + an ALLOW event.
5. **DISPATCH** — the tool handler runs. Seed handlers: `process_media` (enqueues a
   real async job on the job plane), `escalate` / `create_incident` (emit to
   channels), and the **17** `substrate_read` read tools (the consult kind's
   governed read surface — the four base primitives plus the corpus-RAG
   `search_context` and the investigative / graph / finished-intelligence /
   navigation readers; §3.5). (The `discover_sources` tool was removed per
   decision F-1; deep-crawl discovery is a designed direction item, see
   `docs/DIRECTION.md`.)
6. **SETTLE** — the invocation row's outcome (and true cost / units) is stamped.

Every BLOCK is operator-visible: a `governor_events` row plus a best-effort NATS
publish on `governor.events.>`. The per-pack governor caps (rate / invocation /
source / daily-cost) are **enforced live, fail-closed** over the
`action_pack_invocations` ledger, and the action-pack **grant** and
subscription-policy **locking** operator UIs ship live in `legba-ui-v3` (registered +
tested). The related **Backfill Replay** panel also ships in v3 but is a preview-tier
surface — the panel is live while its backend POST returns an honest 501 (the
cross-plane runtime trigger is not yet exposed through the registry).

That loop is now closed in production: the consult kind routes every ReAct
tool call through `Agency.run_pack_tool` via its `substrate_read` binding
(every call resolved, governed, and ledgered in `action_pack_invocations`),
and the actor run path fires the `escalate_finding` pack when a landed
finding crosses the pack's severity/confidence gate — the channel emitter
publishes the escalation to `channels.escalations` and the invocation
settles with governor-event audit rows.

---

## 6. The eval loop — analyst → critic → optimizer

Legba evaluates and improves its own analysts. The loop is three kinds composing:
an analyst produces outputs; a `critic` grades them against a rubric; the
`optimizer` evolves the analyst's prompt from the accumulated traces and critiques;
an operator gates promotion.

### 6.1 Traces and critiques

Every analyst run records an `analyst_traces` row and extends a per-analyst,
**chain-consistent (single-node) SHA-256 receipt chain**
(`runtime/receipt_chain_factory.py`, `data/provenance/receipts.py`): each run links
to the prior via `prev_receipt_hash`, hydrated from the analyst's last trace at
first use, so a broken or reordered link is detectable on a single-node replay.
(These receipts are **not** signed — they carry no cryptographic authorship or
tamper-proofing; that guarantee belongs only to the descriptor **registry's**
separate Ed25519-signed audit log, which signs descriptor mutations — see
`ARCHITECTURE.md`.) The chain follows the analyst
identity across descriptor versions. A `critic` run (§3.8) lands a trace-level
`analyst_critiques` row keyed by `run_id`.

Per-analyst **run-timing** off those traces is exposed read-only at
`GET /api/v1/v3/eval/analyst_runtime` (`data/registry/v3_api.py`) — per analyst over
a window: run count, avg/max wall-clock seconds, last run, and non-success count —
a lightweight observability surface over the same `analyst_traces` rows.

### 6.2 The mandatory faithfulness verify — and the critic

Two graders feed the same actuation gate. Both persist a `critique` row and both
fold into `effective_confidence`, so a poorly-supported finding surfaces a
*lowered* confidence rather than its own self-reported one.

**The faithfulness verify pass (mandatory).** Every cited finding is scored for
**groundedness** — does each fact-asserting claim actually follow from the signal
it cites? — by `verify_finding_faithfulness` (`data/provenance/verify.py`, the
P0-T2 pass). This measures *faithfulness to the cited evidence, not truth in the
world* — the distinction is the whole honesty thesis. A **deterministic floor**
(always on) checks every claim's `[N]` marker against the resolved citation
bridge: a claim asserting a fact with no marker, or a marker resolving to no real
signal id, is an **unsupported** span, and the score is the fraction of checkable
claims that are supported. An **optional LLM judge** — resolved through the judge route
(`LEGBA_JUDGE_STACK_REF` env > `method.llm.judge` > `.verify` > `.primary`;
descriptor default same-model, the reference deployment cross-family on a
hosted Gemma judge), flag-gated on `LEGBA_VERIFY_LLM_JUDGE` —
refines the per-claim verdicts; when the flag is off or the judge is unreachable
the result **degrades to the floor** (`judge_status='deterministic'`) and
publishes **PROVISIONAL under a 0.85 ceiling**, never a fabricated number. A
body whose claims fail to segment publishes an explicit **`unassessable`**
state and no score (never a perfect 1.0). Every critique stamps
`judge_llm_ref` and a `judge_pipeline_version` (`2026-08-10/1` today) so
verdict populations from different judges or rule revisions never pool; a
withdraw-only guard family on hard fails retracts contradictions whose quote
actually confirms the claim (word-numeral/digit/unit normalization,
endpoint-aware ranges, diverging prose direction — each replay-proven to flip
only its adjudicated cases). The verdict lands as a `critique` titled "Faithfulness
verify …" carrying `overall_score`, and `effective_confidence = min(confidence,
overall_score)` is folded at read time (`substrate_reads_api.py`). A planted
fabrication is flagged unsupported. The nine bounded units and all four
compositions — per-country, per-region, thematic escalation, and world — declare
`method.llm.verify` and so all run this pass; the generalized pass recognizes a
composition's `[[ref:<uuid>]]` →
sub-claim bridge and checks each composed clause against the cited sub-claim's own
text (the thematic and world branches additionally apply the T7 shared-lineage
anti-double-counting floor at grade time).

**Calibration of the pass (2026-07-06 — M13 / M14 / M15).** Three guards were added
so the floor stops mis-scoring *honest* findings, all **demote-only** (they flag /
adjust the folded confidence, never delete):

- **M13 — stale-leader guard.** A finding that calls the current US officeholder
  "former" (or names a wrong current holder) is flagged, and the temporal-grounding
  preamble now carries a current-officeholder anchor — closing the stale-cutoff
  "former President" error class (§7.9) at grade time as well as at grounding time.
- **M14 — null-result rubric + parser fixes.** An honest corpus-scoped **absence**
  finding ("no proliferation activity; the N signals focus on unrelated topics") is
  now graded as a faithful **survey** of the cited evidence rather than as uncited
  fabrication; and the citation-marker parser now **expands range markers** (`[1-92]`
  → the integer members, capped so `[1-999999]` can't fan out) and treats explicit
  `[no citation]` lines as **floor-exempt**, so a range-cited or deliberately-uncited
  clause is no longer crushed to ~0.
- **M15 — target-consistency guard.** A per-country UNIT finding whose named
  subject-country contradicts its desk (it names a *different* country and never its
  own target geo) is flagged as a cross-target leak. (A `0080` migration also closed
  the historical cross-target mislabels this guard now catches at write time.)

The fold stays `effective_confidence = min(confidence, faithfulness)` throughout.

**The judge route + judge provenance (2026-07-28 wave).** The judge LLM is no
longer implicitly "whatever `method.llm.verify` says" — it resolves through an
explicit, opt-in ladder (`analyst_deps_builder.resolve_judge_route`):

1. **The opt-in gate comes first.** An analyst whose `method.llm` block
   carries *neither* a `judge` nor a `verify` key gets **no judge route at
   all** — nothing downstream can conscript it into judging.
2. `LEGBA_JUDGE_STACK_REF` (env) — **repoints** every opted-in analyst's
   judge at one component (the drop-in slot for a future dedicated judge
   model). Because the gate runs first, the env can retarget but never
   *enable* judging.
3. `method.llm.judge` → 4. `method.llm.verify` (today's live key on every
   descriptor) → 5. `method.llm.primary`.

Every critique now stamps **`judge_llm_ref`** — which model actually judged —
both top-level and in `data.verification` (empty = the deterministic floor
alone ran), plus **`judge_route`** — *which ladder rung resolved it*
(`configured` for the env override / `method.llm.judge` | `fallback_verify` |
`fallback_primary`; absent on floor-only rows, never fabricated) — so the UI's
provenance badge can tell an explicitly-configured judge from a ladder
fallback. Failures are classified **hard vs. soft** (`fail_class`,
`_FAIL_CLASS_BY_REASON` in `verify.py`): the entity-scramble class —
`unresolved_citation`, `judge_contradicted`, `stale_leader`,
`stale_leader_vs_facts`, `cross_target_leak` — is **hard**; the
unsupported-inference class — `no_citation`, `judge_unsupported`,
`hedge_laundering`, `double_counted`, `indicator_uncited_triggered`,
`unhedged_periphery_citation`, `unscoped_absence_claim` — is **soft** (an
unknown reason defaults
soft). An AST drift guard (`tests/data_pkg/test_verify_claim_ledger.py`)
parses `verify.py`, collects every reason literal the module can emit, and
asserts the table covers exactly that set in both directions. And the pass
now persists a full per-claim **`claim_verdicts` ledger *including SUPPORTED
claims*** (`supported | hard_fail | soft_fail`, capped 120 claims × 300
chars with an honest `claim_verdicts_truncated` flag) — previously only
failures were recorded, so the UI's citation-hover verdict card had nothing
to show for a clean claim. All of this is labels-and-provenance: **none of it
changes the score**.

Two more guards ride the same pass:

- **`stale_leader_vs_facts`** — officeholder claims in the prose are
  reconciled against the substrate's *current open officeholder facts* (not
  just the M13 curated anchor); a claim naming a superseded holder draws a
  distinct **hard** flag. Flag-never-correct (the seed facts can themselves
  be stale — the guard annotates, it never rewrites), fail-open and bounded,
  and kept as its own reason so calibration can tell the facts-backed flag
  from the regex-anchored M13 one.
- A dormant **independence judge profile**: `LEGBA_JUDGE_PROMPT_PROFILE ∈
  current (default) | independent` swaps only the generic judge system
  prompt for an independent-reviewer framing (the M14 survey / absence
  rubrics are profile-invariant); `scripts/temp_ab_replay.py
  --judge-profile` exists for the measured flip when a second model arrives.

**`unscoped_absence_claim` — the failure mode faithfulness cannot see
(2026-07-28).** The gold-set loop (§6.6) surfaced a class the whole verify
apparatus is structurally blind to: **5 of 8** sampled downgrades in one
week's cohort were findings that were *faithful to their inputs and wrong
about the world*. The shape is always the same — a thin-collection desk states
an absence as a **world fact** ("no coercive commodity measures are in place";
"large-scale exercise: not observed") when what it actually established is that
its own sources carried nothing. The claim cites correctly, the judge grades it
supported, and the reader is misled — because faithfulness measures
groundedness in the cited evidence, and a well-grounded claim about a
collection can be a false claim about the world the instant it drops the words
"in what we collected".

Two changes, one deterministic and one editorial:

- **A deterministic soft backstop.** `unscoped_absence_spans` (`verify.py`)
  is pure lexical matching — no LLM. It fires only on a **strong absence
  assertion in the main position** (an explicit opener like "no evidence
  of…" / "there is no…"; a bare "No…" whose predicate is an absence verb
  like *was reported / were observed / occurred*; or a terminal label-colon
  verdict like "…: not observed") **and only on spans the citation floor
  does not already count**, so nothing is ever double-charged. Everything
  hedged ("we assess", "appears", "unlikely"), cited, forward-looking,
  survey-shaped ("none of the 78 signals…"), or **scoped** — by ~35
  collection-scoping phrases (*this desk*, *collected*, *the corpus*, *the
  available reporting*, *in the signals*…), honored from the claim itself
  **or from the immediately preceding span** — passes. When it does fire it
  folds through `_fold_guard_spans`, which adds exactly **one to the
  denominator** of the deterministic score and nothing to the numerator:
  one checkable-but-unsupported claim, a demotion, never a delete.
- **Prompt discipline on all ten inline units.** Each unit descriptor now
  carries a collection-scoped absence rule in its own voice — a negative
  must read *"not observed in collected reporting"* / *"no signals in this
  desk's sources…"*, never a world fact — placed beside that prompt's
  existing honesty rules. A test asserts the rule is present on all ten and
  fails if one drifts out.

**An honest scoping note on where the backstop actually bites.** Its span text
is the *raw* claim, which means the judge-path dedup treats it as already
graded whenever the LLM judge ran on that same span — and the judge's absence
rubric already covers unscoped absence. So on the judge-on path the
deterministic backstop is deliberately absorbed and the judge stays
authoritative; where it genuinely bites is the **judge-off / floor-only**
path. It is a backstop, not a second opinion. And neither leg is a
*correctness* check: both still only ask whether the prose overclaims its
scope, not whether the world agrees.

Downstream, findings reads stamp **`below_floor`** — `true` for a *graded*
finding whose `effective_confidence` sits under the 0.50 floor, `null` for an
ungraded one (annotate-not-exclude; never a fabricated verdict).

**The structural-claims deterministic verify profile (2026-07).**
Deterministic analysts don't hallucinate — but they *can* miscount, and until
this wave their findings simply read `verify_exempt: structural` (the
19-analyst `STRUCTURAL_VERIFY_EXEMPT_ANALYSTS` set in `provenance/kinds.py`).
An opt-in subset (`STRUCTURAL_CLAIMS_VERIFY_ANALYSTS`: `geo_convergence_scan`,
`indicator_tracker`, `thematic_proposal`, `narrative_mapper`) now declares
machine-checkable **structural claims** (`data.structural_claims`) whose
asserted quantities the verify pass **re-derives from the finding's own
lineage** — operations `count` / `distinct_count` / `sum` / `equals`,
including an `@derived_from` basis sentinel checked against the actual
`derived_from` list. Outcomes per claim: `supported` /
**`structural_miscount`** (the number does not re-derive — a flagged
critique) / `unverifiable_structural`. The read-side badge upgrades
`structural` → **`structural-verified`** only when claims existed, at least
one was checkable, and there were zero miscounts and zero unverifiables — so
the badge now distinguishes *checked* deterministic output from merely
*exempt* deterministic output. Gate: `LEGBA_STRUCTURAL_VERIFY_GATE` (default
**OFF** = compute-and-show, never demote — the structural critique's score is
pinned 1.0; ON makes it carry the re-derivation fraction).

**The critic (rubric grader).** The critic is a separate LLM judge against the
analyzed analyst's `eval.rubric`, with the heterogeneity guard of §3.8. Rubrics
are operator-authored content the descriptor's `eval.rubric` field points at — the
same manual quality procedures an analyst would apply by hand, encoded for the
critic to apply at scale.

Neither grader is a spectator: both **actuate**. The surfaced confidence of a
graded finding is `effective_confidence = min(confidence, critic_score)` — where
`critic_score` is the grader's `overall_score` (the faithfulness verify's, for a
verified finding) — so a finding graded poorly surfaces a *lowered* confidence
(`data/registry/substrate_reads_api.py`, the critic-actuator fields). The raw
`confidence`, the score, and the folded `effective_confidence` are all stored
side-by-side, so the down-weighting is auditable, not destructive — and the fold
**never hard-deletes**; a low-confidence finding drops to a visible low-confidence
tier.

### 6.3 The optimizer — DSPy + GEPA as a Dapr Workflow

The optimizer returns as a **scoped, measured experiment**, not an always-on
monolith. It runs today as the `unit_optimizer` descriptor over exactly one
bounded unit (`leadership_transition`), and every candidate it emits carries a
**real before/after paired faithfulness delta** measured on the same faithfulness
verify judge (whatever the judge route resolves — §6.2) that gates the
live findings: the parent arm is the unit's existing
faithfulness `overall_score`; the candidate arm generates under the candidate
prompt and re-verifies. A degenerate, insufficiently-paired, or non-positive delta
is **honest-null and can never promote** — the measurement gate (`_delta_gates_ok`)
stamps `data.eval.promotable=false`. The delta is the deliverable: promotion stays
`human_gated`, and even a hand-flipped `promoted` cannot reach inference without a
positive, non-degenerate, sufficiently-sampled measured delta. The old always-on
monolithic `country_optimizer` (over the retired `country_assessor`) is
**cadence-frozen** — the descriptor still reads `state=active` but its
`fallback_schedule` is nulled (SEAMS #30) — so the reminder-flood regression class
cannot recur. Live example delta: parent 0.34 → candidate 0.29 (−0.05) — a
*negative* delta, so no promotion.

The optimizer (`optimizer.py`) reads an analyst's `analyst_traces` joined with its
`analyst_critiques` (window + minimums are descriptor-set; the `unit_optimizer`
bootstraps at `min_traces_required: 8`), and runs the **GEPA** evolutionary loop
(reflective Pareto-frontier prompt evolution, arXiv 2507.19457) over them to emit a
candidate `prompt_module`.

GEPA is a multi-hour deterministic outer loop driving non-deterministic LLM
activities, so it is the **one** analyst whose work needs a durable-workflow
substrate rather than a single-shot actor. It runs as a **Dapr Workflow** on the
daprd sidecar's durabletask engine — the same control plane that already runs the
actors, placement, scheduler and state store, so there is no separate orchestration
cluster.

`runtime/dapr_workflow/workflow.py` is the deterministic generator body:

1. `validate_training_set_activity` — cheap, fail-fast. If the training set is below
   the descriptor's thresholds, the workflow short-circuits with a zero-delta
   "skipped" result (no candidate promoted; an audit row still lands).
2. `compile_candidate_activity` — the LLM-bearing GEPA step, run with a 2-attempt
   retry policy so a transient LLM failure is retried rather than failing the run.

The deterministic body never does wall-clock / RNG / I/O directly; all
non-determinism lives in the activities, so the engine can replay the workflow from
history. The optimizer's LLM access is mediated by `dspy.GEPA` + `dspy.settings.lm`.
The kind is backend-agnostic — it constructs a workflow client satisfying
`start_optimizer_workflow(...) -> handle` regardless of substrate, and degrades to an
in-process GEPA loop when `dapr.ext.workflow` is unavailable (minimal envs / tests).

### 6.4 Human-gated promotion

Promotion to live is **human-gated end to end** — there is no auto-promotion path.
An operator flips a candidate's `data->>'promotion_gate'` to `'promoted'`, and
`resolve_promoted_system_prompt` (the live inference path) admits the evolved prompt
ONLY when the MEASURED delta is promotable. The single measurement gate is
`gepa._delta_gates_ok`, which stamps `data.eval.promotable` at candidate write time
(`optimizer.run_method`) and rejects an absent / degenerate / non-finite /
judge-unavailable / under-paired / sub-margin delta. So even a hand-flipped
`promotion_gate='promoted'` on a degenerate candidate resolves to the baseline.

The candidate lands in `analyst_outputs` as a `PROMPT_MODULE_CANDIDATE` row; no
prompt goes live without clearing this gate.

### 6.5 Band calibration — the scorecard graded as resolvable claims

The scorecard asserts bands; since 2026-07 those assertions are **held to
account**. `band_calibration_tracker` (deterministic META, daily) logs every
scorecard band *transition* as a resolvable claim in
`band_calibration_claims` (mig 0093 — one claim per
`(desk, dimension, scorecard_row_id)`, never overwritten) and grades each
claim deterministically at **14- and 28-day horizons** under the pinned
resolution spec `hard_band_at_horizon_v1`: at the horizon the desk's then-live
band either **`held`** or moved further (**`worsened`** / **`improved`**) —
all three count as the change *confirming* (it stuck or continued) — or
**`reverted`** (against);
`insufficient` / `unresolvable` claims are **excluded from both denominators
but reported**, and a zero-denominator window publishes an honest `None`
rather than a rate. The product is **persistence and reversal rates**,
surfaced as an additive `band_calibration` section on
`GET /api/v1/v3/eval/calibration`.

The honesty line is structural, not editorial: bands are **not
probabilities**, so **no Brier score, Brier skill score, or forecast-skill
claim exists or *can* exist for this harness** — the `HONESTY_NOTE` carrying
that sentence rides every summary finding and the eval route (`no_brier:
true`), the claims table has no probability column by design, and claimable
directions are restricted to real movements (`deterioration` /
`improvement`). This leg answers "do our band changes *stick*?", which is a
different — and honestly answerable — question from "were we right?".

### 6.6 The correctness gold-set loop — out-of-plane verdicts, never pooled

Faithfulness measures groundedness, not correctness; the reference-label leg
(§3.11 leg 8) is honest but tiny. The gold-set loop grows a real correctness
sample at a sustainable cost — **8 findings a week, labeled from outside the
production plane**. The labeler may be the operator or an out-of-plane
frontier model that grounds each verdict in live web sources; what the design
forbids is the plane grading itself (the same-model blind spot §3.11 names).
Every label stamps its `labeled_by` provenance:

- **A pinned weekly stratified sample.** Verified-only finding heads (a
  faithfulness critique must exist), one per unit for coverage, the fill
  chosen by **rendezvous hashing** (`sha256("week:finding_id")`, highest
  wins — deterministic for a given week across re-reads), with a weekly
  parity rotation that alternates preferring high- vs. low-faithfulness
  bands so the sample doesn't silently favor easy reads. The first read of a
  week **pins** it (`goldset_week_samples`, insert-on-conflict-do-nothing) —
  the worksheet cannot shift under the labeler.
- **Closed-vocabulary verdicts.** `GET /v3/eval/goldset/worksheet` renders
  the week; `POST /v3/eval/goldset/label` records one of
  `correct | partially_correct | incorrect | unresolvable` into
  `correctness_labels` (UNIQUE per finding — re-labeling overwrites; a
  `finding_snapshot` is stored at label time so later supersession cannot
  orphan the verdict).
- **Additive, segregated scoring.** `/v3/eval/scores` grows an
  **operator-correctness** figure per unit (correct=1.0, partial=0.5,
  incorrect=0.0; `unresolvable` excluded) that is **never pooled** with the
  deterministic source-overlap `correctness_vs_reference` leg — an
  all-unresolvable week reads honestly empty rather than averaging to a
  number. This one *is* structural: the two figures are computed from
  different sources and returned as distinct fields, and a test pins the
  reference leg reading honestly unmeasured while the operator leg is
  non-null.

**Honest state, and one honest limit.** The **first cohort (2026-W31) is
fully labeled** — n=8, which is the weekly sample size, not a corpus. It
immediately earned its keep: it is what surfaced the unscoped-absence class
in §6.2 (5 of its 8 downgrades were that one shape).

The limit is in the provenance claim. *"Labels come from outside the
production plane"* is **operational discipline, not a code-enforced
invariant.** What the code actually does is **record** `labeled_by` — there
is no CHECK constraint, no allow-list, and no route-side rejection of a label
attributed to a production-plane analyst id. The property is real because of
who runs the labeling (the operator, or an out-of-plane frontier model
grounding each verdict in live web sources), and it is *auditable* because
every row is stamped; it is not *enforced*. Read the stamp, don't trust the
architecture for this one.

---

## 7. Analytical methodology

The methodology is model-agnostic — it describes analytical *approaches*. Each maps
onto a specific analyst kind, so the theory and the runtime are one machine.

### 7.1 JDL data-fusion levels

Legba's analysis follows the Joint Directors of Laboratories (JDL) data-fusion
model, adapted for open-source intelligence. The levels are the *analytical*
architecture; the planes (acquisition / analysis / jobs / substrate) are the
*processing* architecture — orthogonal, each plane contributing to several levels.

| Level | JDL name | In Legba | Owned mainly by |
|---|---|---|---|
| L0 | Signal refinement | normalization, dedup, quality scoring of raw signals | source baseline + `cross_source_dedup` |
| L1 | Entity assessment | extract + disambiguate entities, build graph vertices | baseline NER + `entity_resolution` |
| L2 | Situation assessment | cluster signals into situations, build edges | `graph_mining` + LLM situation-scoped analysts |
| L3 | Impact assessment | competing explanations, prediction, correlation | `inline_target`, `predictor`, `cross_analyst_correlator` |
| L4 | Process refinement | calibration, adversarial detection, the eval loop | `calibration_tracking`, `adversarial_signals`, `critic` + `optimizer` |
| L5 | User refinement | human-consumable products, on-demand answers | findings / situations feed, `consult_on_demand` |

The matrix is a lens for *where a new capability slots in*, not a directory
structure: deterministic, no-LLM work → a `deterministic` sub-handler; full
reasoning / judgement → an LLM-planner kind.

### 7.2 The confidence architecture

Confidence is multi-level — each analytical object has its own semantics, and every
component is stored for auditability.

**Signal composite confidence** uses a hybrid gatekeeper formula:

```
Confidence = Gate × Modifier
Gate     = source_reliability × classification_confidence
Modifier = 0.40·temporal_freshness + 0.35·corroboration + 0.25·specificity
```

The multiplicative **gate** means an unreliable source or a poorly-classified
signal can never produce high confidence regardless of freshness or corroboration;
the weighted **modifier** captures the operational quality of the specific signal.
Source reliability is the source's historical track record; corroboration steps up
with independent-source count; specificity rewards named actors / dates / locations
over vague rumour.

**Fact confidence decays** when a fact receives no new corroboration (active decay
after a quiet window, contradiction override, corroboration boost, temporal
expiration on `valid_until`, supersession of a same-subject/predicate fact). The
relevant decay sub-handlers are `fact_decay` and `nexus_decay` — and since
2026-07 fact decay is computed as a **readout sidecar** (`fact_decay_scan`,
per-class curves, corroborations-as-sightings) that never mutates the fact
rows, with consumption flag-gated (§4.4.4).

> **Fact write-path supersession model (updated 2026-06-29 — task #101 Holes-A
> + Holes-B; see §7.8, §7.11).** The **default** write path is
> **single-winner-by-recency within a source tier**. Three refinements landed in
> Holes-A: (1) it is **source-tier-aware** — a machine-extracted ingestion/agent
> fact no longer closes (supersedes) an open human-curated `seed`/`curated` fact
> (`seed == curated > ingestion == agent`); same-tier recency still wins, so a
> legitimate leader change of the same tier supersedes as before; (2) when N
> sources **agree** on the same `(subject, predicate, value)`, confidence
> aggregates via a bounded **noisy-OR** (cap 0.99) rather than MAX, so
> corroboration raises belief; (3) every `facts` row now carries a
> `source_credibility real` prior (§7.11). With those alone the model stays
> **single-winner**: it keys on `(subject, predicate)` latest-wins (the
> leader-change semantics §7.9 relies on), does not keep coexisting disputed
> values, and does no credibility-weighted arbitration. **The full contested-claim
> arbiter is now BUILT (Holes-B, §7.11)** — disputed values kept alive at the fact
> layer + a credibility-weighted *surfaced* winner — but it is **detect-only and
> flag-gated**: it never mutates a fact, the write-path coexistence that keeps two
> disputed values open ships **OFF by default** (flag `LEGBA_FACT_CONTENTION`), and
> it is enabled only on this instance. Machine-extracted ingestion fact confidence
> is floored conservatively (`_INGESTION_DEFAULT_CONFIDENCE` ~0.5, below the 0.95
> curated seed), not the old hardcoded 1.0.

### 7.3 The entity knowledge graph and structural balance

The knowledge graph is **relational, not AGE**. Vertices are `entity_profiles`;
the operative edge set is the `nexuses` table; provenance edges are
`signal_entity_links`; proposed relationships accrue in `proposed_edges`.
`entity_resolution` keeps it current (§4.1) and `graph_mining` extends it, doing
its graph compute in in-process networkx.

The Apache AGE graph `legba_graph` exists inside the same Postgres (11 vertex /
21 edge labels registered by migrations 0001 and 0037) but has **never held a
production row** — its only contents were 27 smoke-test fixtures from
2026-06-17, deleted by migration 0150. Both read legs (`graph_mining` and
`structural_balance`'s `_augment_from_age`) degrade to empty and always have.
`/api/v1/graph/path` now returns an explicit `graph_unpopulated` error rather
than a confident "no path" over an empty graph. Whether AGE is ever fed is an
open decision with pre-registered triggers; `docs/AGE_PROBE_REPORT.md` carries
the measurements that decision should be made from.

**Structural Balance Theory** (signed-network analysis, the `structural_balance`
sub-handler) classifies every triad of connected entities by the product of its
edge signs (`AlliedWith = +1`, `HostileTo = −1`):

| Triad | Product | Balanced? | Meaning |
|---|---|---|---|
| +++ | +1 | yes | stable alliance bloc |
| ++− | −1 | no | structurally unstable — predicts realignment |
| +−− | +1 | yes | shared adversary creates an alliance |
| −−− | −1 | no | rare, unstable — a pair will reconcile |

An unbalanced triad is a structurally unstable configuration that gives analytical
lead time on realignments. The balance score (balanced ÷ total triads) is tracked
over time, and **graph entropy** — Shannon entropy over the relationship-type
distribution — surfaces when the relationship landscape is actively reorganizing.

### 7.4 Calibration

The calibration loop is meant to close the confidence question: are confidence
scores actually predictive of outcomes? `calibration_tracking` is built to track
**Brier scores**, rolling reliability bins, a **discrimination score** (the
confidence gap between signals that proved significant and those that went
nowhere — a positive score means confidence is predictive), and drift across
windows, feeding the L4 process-refinement level and the eval loop. **Live state
(2026-06-18): the loop now produces a Brier** — but it is a *self-consistency*
Brier, not yet calibration against exogenous reality (the distinction below is the
essential caveat).

> **The outcome-resolution leg now fires, but at the self-consistency tier
> (2026-06-18).** Phase D wired status-transition resolution: when a hypothesis
> reaches a terminal `confirmed`/`refuted` status, its `resolved_outcome` column
> (migration `0038_hypotheses_resolved_outcome.sql`: `resolved_outcome` /
> `resolved_at` / `resolved_by`) is stamped `resolved_by='status_transition'`, so
> `calibration_tracking` computes a real Brier instead of `n = 0`. **The honest
> caveat is that this is self-consistency, not exogenous calibration:** the same
> evidence that drove the hypothesis to `confirmed`/`refuted` is what resolves its
> outcome, so the score measures internal consistency, not predictive accuracy
> against reality. The finding is flagged `self_consistency_only` to make that
> explicit.
>
> **The exogenous design is preserved and preferred — and the subsequent-facts
> resolver now fires.** `calibration_tracking` still reads `resolved_outcome` (not
> the hypothesis `status` directly), and the two *exogenous* resolver paths — the
> automated subsequent-facts resolver (`resolved_by = 'subsequent_facts'`, grading a
> hypothesis against facts produced **after** it) and an operator label
> (`resolved_by = 'operator:<id>'`) — are the higher-fidelity resolution and outrank
> the self-consistency stamp. The subsequent-facts resolver is now **wired and firing
> live**: `competing_hypotheses.run_method` calls
> `_resolve_hypotheses_against_subsequent_facts` FIRST each sweep, before the
> status-transition fallback. Its honest limit (DQ-H2b): it is a **coarse directional
> heuristic** and now **ABSTAINS on UNDIRECTED theses** (status-quo / non-directional
> claims) — those were auto-grading TRUE at ~0.98 and inflated the headline rate, so
> they are no longer counted as exogenous resolutions. `calibration_tracking`
> segregates the sample (`_is_exogenous` / `_SELF_CONSISTENCY_SOURCES`) and reports a
> `brier_exogenous` distinct from the `brier_self_consistency`, flagging
> `insufficient_exogenous` when too few world-graded rows exist rather than surfacing
> a self-consistency Brier as if it were calibration. The evidence×diagnosticity
> matrix and the ±2 status transitions (§7.5) are real and live; the exogenous Brier
> grows as directional hypotheses age past the resolution window and the operator
> label path remains the highest-fidelity resolution.
>
> **Residual design caveats (apply once it does fire).** The subsequent-facts
> auto-resolver is a **coarse directional heuristic** (net escalate/de-escalate
> direction of subsequent facts vs the thesis direction), so
> `resolved_by = 'subsequent_facts'` outcomes would be directional, not
> adjudicated; the **operator-label path is the higher-fidelity** resolution. No
> claim of proven forecast accuracy is made.

### 7.5 Hypotheses and ACH

Analysis of Competing Hypotheses (ACH, Heuer) is enforced as **thesis /
counter-thesis pairs** — the system will not accept a hypothesis without a competing
explanation, which forces consideration of alternatives from the moment a hypothesis
is created. Each hypothesis carries **diagnostic evidence** (observations that would
prove one branch and disprove the other) and an **evidence balance** counter (net
signed support: positive favours the thesis, negative the counter-thesis). New
hypotheses are deduped against active ones (cosine similarity ≥ 0.80 preferred,
Jaccard ≥ 0.45 fallback) so the same claim is not created twice. ACH maps onto the
`inline_target` and `meta_findings_synthesizer` kinds with hypothesis-evaluation
tool whitelists; hypotheses are a typed output kind (`OutputKind.HYPOTHESIS`).

> **How the matrix is scored — the rigor is now real.** The LLM proposes the
> hypothesis *set* (thesis / counter-thesis pairs) **and** scores each matrix cell.
> Every `(evidence, hypothesis)` cell is scored on Heuer's CC/C/N/I/II scale by the
> LLM (`competing_hypotheses.py:_score_consistency_matrix_llm`, one batched call
> per topic, reached only through the analyst provider plane — never litellm/dspy),
> budget-gated via `check_envelope()`. When the budget envelope is exhausted (or
> the LLM is unavailable / unparsable), the run falls back **per cell** to the
> deterministic lexical/polarity scorer (`_score_consistency` — escalation vs
> de-escalation keyword cues plus signed-nexus polarity, mapped onto −2..+2). Each
> hypothesis row records which path ran under `diagnostic_evidence[].matrix_scorer`
> (`"llm"` or `"lexical"`). The evidence base is scoped to the topic's
> **resolved-entity set** (`entity_profiles` / canonical names — exact membership),
> **not** a `LIKE '%name%'` substring, so "Iran" no longer false-matches an
> unrelated organisation.
>
> Because cells are now semantically scored, the `confirmed` / `refuted` status
> transitions (still the diagnosticity-weighted integer `evidence_balance` past
> ±2) are **more defensible** than the old "leading / dominated" framing — a
> confirmed hypothesis is one the LLM-scored diagnostic evidence supports, not just
> one whose keyword count dominates. **Honest residual caveat:** a *budget-exhausted
> run still falls back to the lexical scorer* (check `matrix_scorer`), and even an
> LLM-scored matrix is an analysis of the current evidence base, not a verdict — so
> no proven-forecast-accuracy claim is made. Crucially, the matrix is **matrix-scored
> and now self-consistency Brier-tracked, but not yet calibrated against exogenous
> outcomes**: the evidence×diagnosticity scoring and the ±2 `confirmed` / `refuted`
> transitions are live and real, and the downstream outcome-resolution leg (§7.4) now
> fires at terminal status (`resolved_by='status_transition'`, flagged
> `self_consistency_only`) so `calibration_tracking` produces a Brier — but that Brier
> grades a hypothesis against the same evidence that drove it, so it is *not* an
> exogenous check on whether these hold up against reality. The **subsequent_facts**
> exogenous resolver now **fires** (`run_method` runs it before the status-transition
> fallback) but ABSTAINS on undirected theses (DQ-H2b), so the exogenous Brier covers
> only directional hypotheses and grows as they age; the operator label remains the
> highest-fidelity resolution (§7.4).

### 7.6 Temporal-graph concepts

Relationship edges carry temporal properties — `weight`, `confidence`,
`evidence_count`, `last_evidenced`, `volatility` — and edge changes are
event-sourced into Postgres, producing a time-series of relationship evolution
("when did A's relationship with B shift from allied to hostile?", "which
relationships are most volatile now?"). That time-series feeds the structural-balance
and graph-entropy metrics. **Future seam:** advanced graph analytics (tensor
decomposition, KG embeddings, Hawkes processes, temporal motifs) are designed for
but not yet running at the current deployment's scale.

### 7.7 Provenance and evidence chains

Every analytical product traces back to raw signals. An analyst output stamps
`derived_from` with the substrate UUIDs it read, so the lineage walker can backtrack:
a meta-finding → its first-order findings → the underlying signals; a finding → its
signals → each signal's immutable acquisition provenance (source, fetch time,
pipeline). Combined with the per-analyst SHA-256 receipt chain (§6.1), the system can
answer "why do we believe this?" at every level — which is the foundation of
analytical accountability, not a nicety.

The **one** explicit exception is the journal (§3.10): a `journal` row is a
perspective *over* this chain, not a node *in* it. It carries an always-empty
`derived_from` and is absent from the lineage catalog, so this `derived_from`
walk never descends into a journal entry. Its citations live only as an up-only
warrant (`cited_substrate_refs`) the panel hydrates into chips — they point *out*
at the substrate it read, never *in* as lineage a walk can surface.

### 7.8 Known data-quality limitations (live, final state 2026-06-18)

The analytical *machinery* in §7 is built. A 2026-06-17 live-data audit surfaced a
set of gaps where the running data did not meet the methodology; a 2026-06-18 pass
(Phases B / C / D) closed most of them. This list records the **final** state —
what is fixed, and the genuine residual items — so the doc never reads as
more-finished than the data.

**Closed (Phases B / C / D):**

- ~~**Ingestion fact confidence is a hardcoded 1.0**~~ — **FIXED (Phase B)**:
  derived from the extractor signal, with a 0.75 fallback. **Relation backend note:**
  the deployed hosted NLP stack runs **GLiREL** (`jackboyla/glirel-large-v0`) for the
  `/extract` relation extraction the `fact_extractor` reuses — it emits **real
  per-relation confidence scores** (live facts span 0.75 / 0.80 / 0.92 / 0.95, only a
  small tail at exactly 1.0), NOT a synthetic 1.0. The in-repo `fact_extractor` code
  comments still saying "REBEL" are **stale**; correcting them to GLiREL — and
  reconciling the conf-1.0-sentinel handling against GLiREL's real scores — is a
  tracked **code** follow-up (not yet done).
- ~~**Fact write-path does not gate NER junk**~~ — **FIXED (Phase B)**: a
  `_is_junk_triple` gate now rejects numbers/dates/units/malformed
  subject-or-object triples on the facts write-path, matching the signal-layer
  rejection; identical-triple dedupe was added at the same point.
- ~~**Fact supersession keys subject+predicate latest-wins, not the full triple**~~
  — **MITIGATED (Phase B)**: identical `(subject, predicate, object)` triples are
  now deduped on write rather than accumulating. (Supersession still keys on
  subject+predicate latest-wins by design — that is the leader-change semantics §7.9
  relies on; the deduped-write is what stops redundant identical rows piling up.)
- **Fact supersession is now source-tier-aware + agreement aggregates confidence
  (2026-06-26, task #101 Holes-A)** — a machine-extracted ingestion/agent fact no
  longer closes an open human-curated `seed`/`curated` fact (`seed == curated >
  ingestion == agent`; same-tier recency still wins); and N agreeing sources on a
  `(subject, predicate, value)` combine confidence via a bounded noisy-OR (cap 0.99),
  not MAX. The **default** write path is still single-winner-by-recency within a tier;
  coexisting disputed values + a credibility-weighted surfaced winner are now the
  **built** detect-only contested-claims arbiter (task #101 Holes-B, §7.11), gated
  behind `LEGBA_FACT_CONTENTION` (default OFF) and enabled only on this instance.
- ~~**Predicate vocabulary unreconciled**~~ — **FIXED (Phase B)**:
  `vocabulary.normalize_predicate` converges both write paths on the canonical
  lowercase-spaced form.
- ~~**`signals.source_credibility` is 100% NULL**~~ — **FIXED (Phase D)**: populated
  at the canonical signal write-path via host lookup against the scored
  `source_credibility` table (unknown host → NULL by design). **State/social media
  now explicitly seeded (2026-07-06, migration `0080`):** un-scored state-affiliated
  hosts were previously out-crediting their own seeded peers, so `presstv.ir` 0.25 /
  `irna.ir` 0.30 / `ukrinform.net` 0.45 / `t.me` (telegram) 0.30 were seeded — all
  **below** the 0.5 ingestion nominal — matching the existing tehrantimes/tass state
  band.
- ~~**`graph_metrics` sink empty**~~ — **FIXED (Phase D)**: `structural_balance` /
  `graph_mining` / `nexus_decay` now write a `graph_metrics` row per run
  (`deterministic_handlers/_graph_metrics_sink.py`).
- ~~**`proposed_edges` are ungoverned**~~ — **FIXED (Phase D)**: the
  `proposed_edge_governance` analyst promotes well-corroborated pending edges into
  neutral `CoOccursWith` nexuses (conf ≥ floor) and rejects thin+stale ones; signed
  reification stays the LLM reifier's job.
- ~~**Entity-resolution fragmentation/mistyping**~~ — **FIXED (Phase C)**:
  `entity_resolution` runs a deterministic `canonicalize_entity` pass
  (`data/_entity_canon.py`) before the dedup key: HTML-entity +
  possessive strip, a curated surface-form alias map ({US, U.S., USA, United States,
  America} → "United States", …), and pycountry-gazetteer **type correction** (a
  country name is forced to class `country`, never `person`; `NWS …` offices →
  `organization`). Merges are provenanced into `derived_from` (+ a readable
  `data.merged_aliases`), and `entity_profile_versions` is written on creation and
  material mutation. NEW resolutions converge; the existing fragmented rows were
  merged by the operator-run `scripts/backfill_entity_canonicalization.py` (applied
  live — country-as-person rows → 0, 'United States' 13 fragments → 1).
- ~~**ACH outcome-resolution wholly unfired**~~ — **WIRED (Phase D)**: when a
  hypothesis reaches a terminal `confirmed`/`refuted` status its `resolved_outcome`
  is stamped (`resolved_by='status_transition'`), so `calibration_tracking` now
  computes a real Brier instead of `n = 0`. See the residual caveat below — this is
  a *self-consistency* Brier, not calibration against exogenous reality.

**2026-07-06 audit remediation — write-path gates (migrations `0076`–`0080`):**

- **Fact write-path gates sharpened.** A **predicate-argument / relation-direction
  gate** rejects inversions ("NATO member of Turkiye") and quantity/person objects
  for `member`/`part` predicates; a **demonym + relative-temporal subject reject**
  drops those subjects; and **adjective-nationality VALUE normalization** is scoped
  to geographic/relational predicates only ("Kyiv capital of Russian" → Russia, while
  "speaks Russian" is untouched). Crucially, a pre-existing **over-aggressive
  "sports roster" gate was fixed** — it had been silently dropping real IGO-membership
  facts ("France member of European Union" and peers), which now land. A `0077`
  reversible migration closed the historical semantic/demonym/temporal junk facts
  (reversible `valid_until`).
- **Nexus write-path gates added.** A junk / vague-endpoint gate (relative-time and
  vague-bloc / adjective singletons) fires at **both** producers, a same-referent
  **self-edge** gate drops A→A loops, and **demonym / plural dyad canonicalization**
  stops "Russia|Russian × Ukraine|Ukrainian" inflating dyad counts. A `0078`
  reversible migration swept the historical nexus junk / self-edges and canonicalized
  the dyads.

**Residual (genuine, still open):**

- **ACH exogenous calibration is firing but thin.** The status-transition resolution
  grades a hypothesis against the *same* evidence that drove its prediction (a
  self-consistency stamp `calibration_tracking` segregates out). The exogenous
  `subsequent_facts` resolver — grading against facts produced *after* the hypothesis
  — now **fires live** (`run_method` runs it before the status-transition fallback)
  and ABSTAINS on undirected theses (DQ-H2b) so it stops auto-grading status-quo
  claims TRUE. `calibration_tracking` reports a `brier_exogenous` distinct from the
  self-consistency Brier and flags `insufficient_exogenous` when the world-graded
  sample is too small — which it still is, because the resolver only grades
  *directional* hypotheses that have aged past the resolution window, and the operator
  label path is unused. So a calibration-against-reality number now exists but is
  **statistically thin**; *matrix-scored, self-consistency Brier-tracked, and exogenous
  Brier-firing-but-sparse* (§7.4, §7.5).

(See CODE_MAP §2.14 for the seed-side honesty notes.)

### 7.9 Knowledge grounding — current-world-state injection

A stale-cutoff problem is intrinsic to the analyst plane: the core analyst LLM has
a training cutoff that predates the present, so it backfills *current* world facts
(who holds office, which alliances are in force, the present state of an ongoing
conflict) from a prior that may be wrong — observed live, the assessor called the
current US president a "former" president because its training data predates the
2024 election. The signal slice rarely restates such background facts, so the model
has nothing in-context to correct it. Knowledge grounding is the fix, and it reuses
the substrate already built in §7.2–§7.6 rather than adding a new store.

**The substrate IS the grounding store.** The temporal facts (`valid_from` /
`valid_until` / `superseded_by`, §7.2) and the signed reified nexuses (§7.3),
together with the **seed roots** (the curated `world_baseline` adapter and the live
Wikidata leaders adapter — see `AI_MODELS.md` §6 and `ARCHITECTURE.md`), hold the
temporally-honest answer to "who holds office *now*". Grounding is two halves:
*curate the current data in*, and *inject it at analysis time*.

**Tier 0 — curate current data in.** The `wikidata_leaders` seed adapter
(`data/seed/adapters/wikidata_leaders.py`) pulls current heads of state/government
from the live Wikidata SPARQL endpoint and emits a **country-subject** office fact
`'<country> | head of state | <leader>'`, keyed on the *country*. Keying on the
country (not the person) is what makes supersession correct: when a leader changes,
the new fact closes the prior officeholder's row (`valid_until = now` +
`superseded_by`) via the Phase-B `valid_until` write path, instead of leaving two
open "current" rows. The curated `world_baseline` adapter emits the same shape, so a
fresh Wikidata pull cleanly supersedes a stale curated leader for the same country.
A known wrinkle is handled honestly: the SPARQL label service fails for some
entities (the live-observed case is US head of government Q22686 — Donald Trump —
which has *no* English label), so a bare-QID value is resolved via a `wbgetentities`
label lookup with an enwiki-sitelink-title fallback; an entity that still can't be
labelled is **dropped**, never emitted as an unreadable `Qxxxx` value. Live-verified:
US head of state = 'Donald Trump' (since 2025-01-20), current, superseding the QID.

**Tier 1 — inject at analysis time.** A descriptor opts in via a `GroundingBlock`
(`data/schemas/analyst.py` — `grounding: {enabled, scope, sources, max_facts}`, off
by default). When enabled, the deps-builder (`analyst_deps_builder._build_grounding_hook`,
gated on `grounding.enabled` + an available `pg_pool`) installs a per-run hook backed
by `SubstrateGroundingResolver` (`runtime/grounding.py`). On each run the
`inline_target` **GROUND** phase (§3.1) calls the hook, which:

1. collects candidate names — the target geo + the top entities in the signal slice
   (deterministic, no DB read for the candidate pass);
2. queries the substrate for the **current** authoritative facts for those subjects —
   the same temporal-honesty gate the rest of the analysis plane uses
   (`superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`),
   ordered to **prefer `source_type IN ('seed','curated')`** so seeded ground truth
   outranks a hallucinated live fact, plus a small number of current signed nexuses;
3. renders a dated **"AUTHORITATIVE CURRENT CONTEXT (as of <today> — treat as ground
   truth over prior knowledge)"** preamble, which the GROUND phase prepends to the LLM
   user prompt.

Three honesty properties are built in. The resolver **skips bare-QID values** in both
SQL (`value !~ '^Q[0-9]+$'`) and a Python backstop, so it never injects an unreadable
`Qxxxx` line. It is **degrade-not-drop**: any read failure (or a thin slice that
resolves nothing) yields no preamble and the run proceeds un-grounded — grounding is
an enrichment, never a gate. And it is **opt-in and token-capped** (`max_facts`), so an
analyst that doesn't declare the block is untouched. Grounding is opted IN on **all
nine bounded reasoning units** (`leadership_transition`, `energy_security`,
`escalation`, `narrative_coordination`, `internal_stability`, `military_posture`,
`economic_coercion`, `proliferation_watch`, `disruption_status`), which read
`sources: [substrate, situations, graph_structure]` (one of them,
`internal_stability`, also adds `vector:world_context` — Tier 2 below) — the retired `country_assessor`
monolith carried it first. Because this preamble is drawn from **accumulated** facts,
signed nexuses, and situations (not just the 72h raw window), a unit reasons over
state that integrates over time — e.g. lines like "US head of government Trump since
2025-01-20; US–Iran active conflict since 2026-02-28; NATO member since 1949".
**Canary passed live:** a US unit's context now contains
"United States — head of state: Donald Trump (since 2025-01-20)".

**Tier 2 — vector `world_context` RAG (a GUARDED, MEASURED PILOT).** A curated
unstructured-brief collection (free-text background the structured facts can't carry)
is **wired and serving**, but deliberately as a *pilot*, not a finished capability.
The embedder-through-port leg (L-114) that Tier 2 waited on is **built and SEAM #11 is
RESOLVED**: the query is embedded through the stack embedder port (**bge-m3,
1024-dim**) and cosine-searched against two live **Qdrant** collections — `tradecraft`
(analytic-tradecraft / SAT standards, ~1,716 chunks) and `world_context`
(country/topic priors + doctrine summaries, ~293 chunks). When a `GroundingBlock`
lists `vector:world_context`, the resolver runs **inline opportunistic RAG**: it
retrieves chunks under a **relevance floor** with a **country filter**, and is
**degrade-not-drop** — an empty or below-floor corpus yields no chunks and the run
proceeds on the structured tiers rather than injecting an empty or off-topic preamble.

**Recalibrated + re-activated on `internal_stability` ONLY (2026-07-06).** The
embedder (bge-m3) was never the problem — the fixes were in *retrieval usage*: a
focused `"<country> <theme>"` query (replacing a diluted unit-name + entity blob),
**doc contextualization** (chunks embedded with a `"<Country> — <section>"` lead), the
293-point corpus **re-embedded in place** (`scripts/reembed_world_context.py`), and
the relevance floor lowered **0.65 → 0.55** (on-target now ~0.6, off-target ~0.42).
RAG is currently **flipped ON for one unit — `internal_stability`** — and
`leadership_transition` RAG is **OFF** (the 2026-07-03 rollback is now live). The
other units carry only the structured sources; expansion is staggered and
review-gated per unit.

**A real per-run auto-rollback guard** (`src/legba/runtime/rag_rollback.py`) replaces
the old comments-only one: it re-checks a disabled-units env
(`LEGBA_WORLD_CONTEXT_DISABLED_UNITS`) plus a persisted state file
(`LEGBA_RAG_ROLLBACK_STATE`) on **every** run, so a rollback suppresses injection on
the *next* run **without a restart**. Triggers are a faithfulness drop / low-faith
ratio / token-cost rise (≥ 35%), actuated by `scripts/rag_watch.py --enforce`. Per-run
trace instrumentation records `world_context_top_score` / `retained` / `min_score` so
the measurement is honest. The injected priors remain **non-citable** (a fenced
background block, no `[N]` ids). The corpora are loaded by the Lane-4 vector loader
(`data/rag/lane4_loader.py` + `chunker.py`) via `scripts/manual_ingest_vectors.py`.

> **Known limit (honest).** Firing RAG has *historically thickened the
> low-faithfulness tail* even with the non-citable header; the guard reverts if that
> recurs, which is exactly why this stays a pilot and not a shipped default. The pilot
> state file currently lives at an **ephemeral path** — moving it to a volume for
> persistence is a tracked follow-up (§7.11 backlog / `SEAMS.md`).

### 7.10 Forecasting and prediction — the methodology under test

Forecasting is the platform's most-hedged surface, deliberately so: it is a
**declared experimental seam** (`SEAMS.md`; `RELEASE_STATE_MATRIX.md`) that ships a
*methodologically real harness* yet makes **no validated skill claim**. This
section names the theory the two forecasting legs are **designed under**, and the
specific falsifiable question each is **currently testing** — the honesty caveat is
not that the method is hand-waved, but that the *result* is not yet earned.

**Two legs, two methods.**

- **Event-volume forecasting (`predictor`).** Classical **Box–Jenkins ARIMA**
  time-series modelling (via `statsforecast`'s `auto_arima`) over a target's recent
  signal-count series, emitting a point estimate plus a confidence interval; when
  no trend can be fit it degrades to a `naive_mean` baseline and labels itself as
  such (`forecast_method`) rather than dressing a flat mean as a model. It answers
  *"how much activity next period?"* — a magnitude estimate, not an event call.
  **This forecast-as-claim leg is now retired and stopped** (`country_predictor`
  retired, `india_energy_predictor` cadence-nulled; SEAMS #31 / #32) — neither
  ticks, though ~539 historical prediction rows remain in the DB, unread: a numeric
  forecast is a *claim*, and the direction's rule is measure-and-verify before a
  forecast ships as product. Forecasting now returns only through the scored acute
  pilot below, which reports **no proven skill** yet (honest — see below).
- **Acute-hazard probabilistic forecasting (`forecast_acute`, the pilot).** A
  **rare-event / Poisson-process** model: the count of severe hazards in a
  region-week (frozen class-K sources — USGS M4.5+ earthquakes, NASA EONET events)
  is treated as Poisson with rate λ estimated from the historical record, and the
  forecast is the **tail probability** P(≥1 event in the FORWARD week) = 1 − e^(−λ).
  It answers a sharp **binary** question — *"will at least one severe acute hazard
  occur in country X next week?"*. It is driven by the deterministic META
  `forecast_scoreboard` analyst (weekly: **issue** the forward forecasts → **resolve**
  closed windows exogenously by the upstream event time → **count**). Its only
  persisted product is `acute_forecasts` rows plus a **TRACE_ONLY** receipt — never a
  finding, prediction, or free-text claim on any trust surface — and the numbers
  surface **solely** on the calibration scoreboard route
  (`GET /api/v1/v3/eval/calibration`).

**The verification theory (what makes it falsifiable).**

- **Proper scoring rules.** Binary probabilistic forecasts are graded with the
  **Brier score** (Brier 1950) — a *strictly proper* rule, so a forecaster
  minimises it only by reporting its true probabilities; the Brier decomposes
  (Murphy) into **reliability + resolution** (calibration vs discrimination, §7.4).
- **Skill against a reference, not raw accuracy.** Accuracy alone is meaningless
  for rare events (always predicting "no" scores well), so the pilot reports a
  **Brier Skill Score (BSS)** against a **base-rate / reference-class** forecast —
  the realized climatological frequency (the *outside view*, Kahneman & Tversky;
  reference-class forecasting, Tetlock). The claim under test is not "we are
  accurate" but **"we beat the base rate"** — the only claim that earns the word
  *forecast*.
- **Pre-registration, no look-ahead, exogenous resolution.** The window is strictly
  **forward** (issued before the period it covers — no leakage), the task / horizon
  / resolver are fixed **before** the outcome (pre-registration), and each forecast
  is graded **exogenously** — by the upstream event's own timestamp, never by the
  model's own downstream evidence. This is the discipline of forecasting tournaments
  (IARPA ACE / Tetlock's *Good Judgment Project*) and plain Popperian
  falsifiability: the call can be *wrong*, and the record will show it. A window
  whose issued p-vector was pre-clamp degenerate is **voided**, not graded
  (`resolved_by='voided:pre_clamp_degenerate'` — the row is kept and counted as
  drained work, never scored); real windows grade `forecast_acute_exogenous`,
  and the first exogenously graded cohort landed 2026-08. The acute
  Brier is stored **segregated** (`brier_forecast_acute`) and never pooled into the
  self-consistency calibration headline (§7.4).

**What it is currently testing — and why no claim yet.** The pilot is live but
**accumulating**. The first seeding honestly surfaced that a *country-week binary*
is **geography-dominated** — a handful of seismically active regions carry almost
all the base rate, so a naive geographic prior is hard to beat and the BSS is not
yet meaningful. The harness **detects this degeneracy** (a probabilistic-share
guard) and **withholds the skill claim** instead of reporting a flattering number;
it emits `degenerate / accumulating`. So the honest position is: the *machinery*
(Poisson model, strictly-proper score, base-rate reference, exogenous forward
resolution, segregated metric) is real and running, but **forecasting skill is
unproven** — it will be claimed only once the BSS clears the reference over a
sufficient record (target n ≥ 30, ~3 weeks) **or** the task is sharpened to one
where geography is not the dominant signal. Until then Legba **declines to call
itself a forecaster** (README; `DIRECTION.md`) — the seam is documented as
*designed and under test*, not *delivered*.

### 7.11 Contested claims — the alternate-facts arbiter

The supersession model of §7.2 is **single-winner-by-recency within a tier**: the
newest same-tier `(subject, predicate)` assertion closes the prior. That is the
right default for a genuine state change — a leader changes, the new officeholder
fact supersedes the old (§7.9). But it is the *wrong* model for **disagreement**:
when two sources of equal standing assert different *current* values for the same
`(subject, predicate)` — the "alternate facts" problem — last-writer-wins silently
destroys one side, and which side survives is an artifact of arrival order, not
evidence. Legba's answer (task #101, "Holes-B") is a **detect-only contested-claim
arbiter** built on one stance: *surface a winner from the SOURCE evidence, and
never destroy the loser*. It is a methodology, not a verdict — it adjudicates which
asserted value the substrate's own sources best support, it does **not** decide
which is true in the world, and it **never** injects a model's own world-knowledge
to settle a dispute.

**Per-fact source credibility (the weighting input).** Every `facts` row carries a
`source_credibility real` (Wave 0, migration `0054`). It is **resolved** as the MAX
over the backing signals' per-host `signals.source_credibility` (the real scored
track record, §7.8), falling back to a coarse **per-tier nominal** when no host
score exists: `seed`/`curated` → 0.9, `ingestion`/`agent` → 0.5. A NULL is
**UNKNOWN, never 0** — an unscored source is not treated as worthless, it simply
abstains from the credibility mass. This is the *C* axis of the arbiter and the
honest measure of "how much should this source's vote count".

**Coexistence — never destroy the loser (Wave 4).** Before anything can be
arbitrated, both disputed values have to *survive* the write path. Inside
`supersede_prior_facts` (`provenance/writes.py`), when a same-tier incoming value is
**fuzzy-distinct** from an open prior, the prior is **not** closed — the two
coexist open so the arbiter can group them — instead of last-writer-wins. This is
the one *behavioural* change in the whole feature, gated behind
`LEGBA_FACT_CONTENTION` (**default OFF**, in code and in docker-compose
`${VAR:-0}`); with the flag off the path is byte-for-byte the §7.2 single-winner
model. Both fact producers (the analyst and ingestion paths) route through
`supersede_prior_facts`, so the coexistence rule is uniform.

**Fuzzy value clustering (Wave 2/3).** The arbiter only fires on *genuine*
disagreement, so it must not split "Russia" from "Russian", or "Kyiv" from "Kiev",
into a false dispute — nor merge "North Korea" with "South Korea" into a false
agreement. `provenance/value_clustering.py` canonicalizes each raw value via the
shared `canonicalize_entity` pass (the same demonym/alias/gazetteer normalization
`entity_resolution` uses, §4.1) and then merges by **normalized Levenshtein**
distance under a **tight** threshold `FUZZY_MERGE_MAX_DISTANCE = 0.12` — close
enough to absorb a typo or spacing variant, far short of merging two genuinely
different proper nouns (North/South Korea stays split). The same fuzzy helper also
un-dormants the Holes-A noisy-OR corroboration leg, which previously grouped support
by *exact* normalized string and therefore almost never fired on real,
differently-phrased sources.

**The deterministic Q·C·R·F score (Wave 2).** The arbiter is a `deterministic`
META sub-handler (`fact_contention_arbiter`), TRACE_ONLY, firing **hourly at :37**.
Each pass it scans **open** facts (`valid_until IS NULL AND superseded_by IS
NULL`), buckets by `(subject, predicate)`, fuzzy-clusters the competing values, and
**junk-gates** each cluster through the existing `fact_extractor` gates (so a
number/date/unit or an inverted-relation triple is dropped, not adjudicated — the
live Poland→Berlin case is junk, not a dispute). It scores each surviving value
cluster multiplicatively, **Q·C·R·F**:

- **Q (quorum)** — a log-damped count of **distinct backing sources** (keyed on
  `derived_from` lineage, not rows, so one chatty source cannot manufacture
  quorum), normalized within the group.
- **C (credibility share)** — this value's share of the group's total
  `source_credibility` mass (NULL-safe: unknown sources contribute nothing rather
  than zero-ing the value).
- **R (recency)** — an exponential **half-life** decay on the value's latest
  assertion. Recency is *one bounded factor among four*, not the sole decider —
  that is the structural fix versus last-writer-by-recency.
- **F (confidence)** — the mean asserted confidence of the value's facts.

The product is multiplicative on purpose: a zero on **any** axis kills the value (a
single recent assertion from one unknown-credibility source does not win on recency
alone).

**The abstain gate (honest non-resolution).** The arbiter surfaces **at most one**
winner per `(subject, predicate)` group, and only when it has earned it. It
**abstains** — recording an honest "disputed, no resolution" rather than a forced
pick — when the best cluster is **weak** (`best_score < MIN_SURFACE_SCORE = 0.15`)
or the top two clusters are a **near-tie** (the best does not beat the runner-up by
`DOMINANCE_RATIO = 1.25`). A surfaced winner is a *strong, dominant* value; a
near-tie is left visibly unresolved.

**Detect-only — the hard invariant (B15).** The arbiter **never** mutates a
fact. It touches zero of `valid_until` / `superseded_by` / `value` / `confidence`
and never calls the supersession path. Its entire output is the **sidecar**
(`fact_contention` + `fact_contention_values`, Wave 1, migration `0055`) plus three
thin recomputable markers on `facts` — `contested`, `contention_id`,
`surfaced_winner`. The whole contention state is derivable from the open facts, so
the arbiter is a **read-over / annotate** layer, structurally incapable of
destroying evidence — the same provenance-first discipline the journal (§3.10) and
the lineage chain (§7.7) express.

**The optional vLLM tie-break (Wave 2b) — and the provenance-first stance.** The
one case the deterministic score genuinely cannot settle is a **near-tie** between
two non-junk clusters that both clear the surface floor. *Only* there, and only when
`LEGBA_FACT_CONTENTION_LLM_TIEBREAK` is set (**default OFF**), the arbiter may
consult an LLM to break the tie — and it is bounded hard: the **self-hosted vLLM
plane only** (the deps-builder *hard-refuses* an Anthropic/Opus primary so a
mis-wired descriptor can never route the billed consult plane into a fact dispute),
256 tokens, a 30s timeout, at most `MAX_LLM_TIEBREAKS = 10` calls per pass, and it
**degrades to abstain on any failure**. Critically, **the tie-break is still
detect-only**: an LLM-chosen winner is surfaced through the *same* sidecar + marker
path; it never touches a fact value or `valid_until`. The receipt splits
`llm_tiebreak_calls` (consultations attempted) from `llm_tiebreaks` (successful
picks) so the spend and the effect are separately auditable. The stance the whole
feature defends is here: the LLM is asked *"which of these two SOURCE-asserted
values is better supported by this evidence?"*, **never** *"what is the true
value?"* — it adjudicates the sources, it does not supply world-knowledge. This is
exactly the right behaviour observed live: consulted on a dispute with **symmetric**
evidence, the live vLLM **correctly abstained** rather than inventing a tiebreaker,
which is the provenance-first answer.

**Surfacing (Wave 5, read-only).** Contention is exposed without ever asserting a
disputed value as settled truth:

- **Grounding preamble (§7.9)** — a grounding-eligible fact (the existing
  seed/curated provenance gate still applies; ingestion content is never injected)
  is annotated: a surfaced winner reads `[CONTESTED: N sources disagree; surfaced
  winner]`, while a contested-but-unresolved value reads `[DISPUTED: … no surfaced
  winner — do not treat as settled]` so the analyst LLM never reads a disputed
  value as fact.
- **ACH (§7.5)** — a `contested_fact_value` evidence item is offered into
  `competing_hypotheses`, so a live dispute is visible as evidence in the matrix.
- **Read API** — `GET /api/v1/contention` (registry) serves the `fact_contention` /
  `fact_contention_values` sidecar read-only.
- **UI** — a `ContestedBadge` component mounts in the **Why** provenance trail
  (fact-keyed) and the target **Claims** panel (subject-keyed).

**The arbiter tail (2026-07-28 wave) — from detection to accountable
surfacing.** The machinery above answers *who disputes what*; the tail
(migration 0097) decides when a **winner** may be surfaced and keeps every
decision accountable:

- **Soak.** The tie-break layers wait out a soak window
  (`LEGBA_CONTENTION_SURFACE_SOAK_HOURS`, default **48h**) so a fresh dispute
  is not adjudicated on arrival-order evidence; a Q·C·R·F-*decisive* winner is
  not soak-gated (the deterministic score already earned it).
- **Weighted tie-break.** On a deterministic near-tie abstain, a weight —
  distinct-source count + source-*type* diversity + credibility mass (+ the
  earned term below) — may break it, but only past soak, only with ≥ 2
  distinct sources on the winning side, and only past a dominance ratio
  (default 1.5). The **earned-track-record** term ships as a seam behind
  `LEGBA_CONTENTION_EARNED_WEIGHT` (**default 0.0 = OFF, byte-identical**):
  when enabled, per-source weights come **live** from the
  `source_track_records` readout (mig 0099 — Beta(2,2)-smoothed,
  Wilson-lower-bounded win rates over how each source's claims fared in
  *previously resolved* contentions), computed with a **72h lag** and a
  **self-exclusion guard** — the contention being decided is excluded from
  the record that decides it, so the loop cannot feed itself (acyclicity).
- **A cached LLM near-tie adjudicator.** Only past soak, only on a genuine
  near-tie, only under `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` (default OFF),
  bounded exactly as before (≤ 10 calls/pass, 256 tokens, 30s, self-hosted
  plane only) — and now **cached** per `(contention_id,
  evidence_fingerprint)` in `fact_contention_tiebreak` (`verdict ∈
  pick | unsure`): the same evidence is never re-asked, and a transport
  failure degrades to abstain **uncached** (only genuine verdicts persist).
- **Coexistence surfacing.** A surfaced winner is stamped `surfaced_by`
  (`deterministic` / `llm`), `surfaced_at`, and a human-readable
  `surface_rationale`; every change appends the prior record to a capped,
  newest-first `surface_history`. The losing fact is **never mutated** —
  invariant B15 stands, asserted in tests; surfacing is presentation with
  receipts, not supersession.
- **Re-open on new evidence.** The decision is recomputed every pass over an
  evidence fingerprint, so new evidence re-opens the dispute (the prior
  surface survives in history) — and the moving `status` /
  `surfaced_fact_id` pair is exactly what the `contention_flip` alert class
  fingerprints (§4.4.1).
- **Read-side flags.** The grounding/read annotation
  (`LEGBA_CONTENTION_SURFACING`) defaults **ON**; the slice-reorder
  preference (`LEGBA_CONTENTION_SURFACING_PREFER`) defaults **OFF**.

**Build / validation state (honest).** All waves (0, 1, 2, 2b, 4, 5) plus the
arbiter tail are **built,
deployed, and enabled on this instance** (the contested-claims schema landed at
migrations `0054`–`0055`; the tail's surfacing columns + tie-break cache at
`0097` and the earned-record table at `0099`; the live migration head is now
**0185**; both
`LEGBA_FACT_CONTENTION` and `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` ship **OFF by
default** in code and compose, set to `1` only here via the gitignored `.env`).
Proven **live**: the detect-only arbiter (Q·C·R·F surfaced the better-supported
value, the detect-only invariant held — both facts' `valid_until`/`superseded_by`
stayed NULL), Wave-4 coexistence (a same-tier fuzzy-distinct value coexisted,
`supersede_closed = 0`), and the Wave-5 read API. The Wave-2b vLLM tie-break is
proven **consulted live** (a genuine round-trip on a near-tie,
`llm_tiebreak_calls = 1`, no errors) and correctly **abstained** on symmetric
evidence — but a *successful* LLM **pick** (`llm_tiebreaks ≥ 1`) is **not yet
observed live**; it awaits an asymmetric near-tie in the soak.

---

## 8. Proven end-to-end

The analysis plane is live in the real stack, cold-startable from empty volumes
(applying the single `0001_baseline.sql` schema migration):

- Real RSS/API sources (BBC, Deutsche Welle, Al Jazeera, and **105 active source
  descriptors** in all — the live count is generated, never hand-typed: see
  `RELEASE_STATE.md`) produce enriched signals, each carrying geo, language, and
  entity classes promoted to indexed columns.
- Those signals fan out on `legba.signals.>` to the **32 country desks** — the 19
  G20 members plus a 13-desk high-consequence **watch tier** (Israel, Iran,
  North Korea, Pakistan, Taiwan, Ukraine, and the escalation-risk band Sudan,
  Mali, Burkina Faso, Niger, DR Congo, Myanmar, Haiti; descriptor ids
  `country_watch_<iso2>`). Adding a desk is
  register-a-target with a `g20`/`watch` coverage tag, no code change.
- Alongside the country plane, **10 thematic desks** carry the supply-chain
  domain — six shipping/chokepoint `lane_*` frames and four `flow_*` commodity
  frames, tagged `[supply_chain, disruption, <lane|flow>]`. Three are active
  (Hormuz, Red Sea, Malacca / South China Sea); the rest ship `draft` and fan
  out only once an operator activates them. Same registration path, same tag
  predicate, no code change — a desk is a subject-frame, not a country.
- Each country desk is coalesced (§2) into **eight** of the nine bounded reasoning
  units (`inline_target`) — seven broad ones fan across every `g20`/`watch` desk; an
  eighth, narrower unit, `proliferation_watch`, instead fans only across the
  ~8 nuclear-relevant desks (`has_tag("nuclear_watch")`); the ninth,
  `disruption_status`, does not touch the country plane at all — it fans across
  the thematic `has_tag("supply_chain")` desks on a 24h window (06:00/18:00 UTC)
  and emits the same cited, verify-gated finding shape — each answering one
  narrow question over the desk's slice
  plus its accumulated-state grounding preamble (one unit, `internal_stability`, also
  pulls the guarded live `world_context` vector RAG pilot), emitting a cited finding that then
  clears the mandatory **faithfulness verify** pass (§6.2) — with full `derived_from`
  provenance and per-analyst receipt chains.
- `country_composition` synthesizes each desk's seven broad verify-passed unit
  findings, plus `proliferation_watch`'s on nuclear desks,
  into one hedged, cited per-desk read; `region_composition` fuses the country reads
  into five regional reads; `world_assessor` composes those five region
  reads into one cited world view; a thematic `escalation_composition` fuses the
  escalation dimension across every desk (§3.11).
- `scorecard_producer` enumerates every active `g20`/`watch` desk and writes one
  banded verdict row per desk from rules over the already-verified claims — an
  all-insufficient row where no claim qualifies, never a fabricated band.
- Two deterministic indications-&-warning analysts (§3.11 leg 6) close the loop:
  `indicator_tracker` diffs each unit's pre-registered indicators run-over-run and
  fires on a `not_observed → triggered` flip; `collection_gap` ranks the starved
  desk × dimension cells off the scorecard's honest `insufficient-evidence` bands.
- An ongoing `entity_resolution` deterministic analyst (§4.1) keeps an entity
  knowledge graph (`entity_profiles` / `signal_entity_links` / `proposed_edges`)
  current.

The live scorecard is honestly a **mix**: some countries band from verified claims,
while others (e.g. the US) read all-insufficient because their units' faithfulness
is genuinely low — the system surfaces that rather than papering over it.

### Future seams (not yet end-to-end — see `docs/SEAMS.md`)

- **Situation clustering** — `finding_supersession` ships as a deterministic
  cadence analyst that links near-duplicate findings (§4.3); clustering those into
  feed-level situations automatically is not yet enforced.
- ~~Analyst-side agency invocation~~ — **closed**: consult tools run through
  the governed `substrate_read` pack; gated findings fire `escalate_finding`
  end-to-end (§5.3).
- **Media eager-extraction** — the `process_media` job envelope and `process_media`
  action exist; the extraction handlers (Whisper / VLM / OCR) are thin — see
  `ACQUISITION.md`.

Live (in case you read otherwise elsewhere): reactive LLM trigger dispatch, the
per-pack governor caps over the `action_pack_invocations` ledger, and the
action-pack-grant / subscription-policy-locking operator UIs (the related Backfill
Replay panel is live but preview-tier — its backend POST is an honest 501 today).

---

## See also

- `ACQUISITION.md` — the acquisition plane: sources, baseline enrichment, fan-out,
  subscriptions.
- `ARCHITECTURE.md` — the substrate stores, the descriptor registry, the
  Dapr-actor runtime.
- `AI_MODELS.md` — the vLLM / embedding / translation / NER models the analysts call
  through the stack registry.
- `RUNBOOK.md` — running and operating the runtime.
