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
4. **The eval loop** — `critic` analysts grade outputs against rubrics; the
   `optimizer` analyst runs a DSPy + GEPA reflective prompt-evolution loop (as a
   Dapr Workflow) whose promotion is human-gated. (§6)

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
(`build_analyst_run_method`; the same twelve are in `_KIND_MODULE_NAMES`, walked
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
| **`inline_target`** | `llm_planner` | one target's 24h signal slice (`subscription.targets`, `has_tag("g20")`) | 7-phase envelope WAKE→ORIENT(newest 20)→PLAN→GROUND→REASON→REFLECT→NARRATE | **FINDING** | yes (one `chat_complete`) | **6h** (`0 */6 * * *`) |
| **`relationship_reifier`** | `llm_planner` (META) | `proposed_edges` co-mention pairs + open facts | small-LLM types each pair → canonical predicate + **signed polarity** | **NEXUS** side-write (`write_nexus`); FINDING receipt | yes (8B-path, 384-tok JSON/pair) | **12h** (`45 */12 * * *`) |
| **`deterministic` / `graph_mining`** | `deterministic` (`sub_handler: graph_mining`) | signed `nexus` rows + the Apache AGE subgraph | `networkx` communities / centrality / proxy-chains | `graph_metrics` sink + **FINDING** | **no** | **12h** (`52 */12 * * *`) |
| **`competing_hypotheses`** (alias `ach`) | `llm_planner` (META) | focal situations × current facts × signed nexuses | evidence × hypothesis matrix + diagnosticity → `confirmed` / `refuted` | **HYPOTHESIS** side-write (`write_hypothesis`); FINDING receipt | yes (LLM proposes set + scores cells, budget-gated) | **12h** (`50 */12 * * *`) |
| **`predictor`** | `stat_forecaster` | a 14d (`336h`) signal window for the target | daily-aggregated AutoARIMA forecast (point + CI) | **PREDICTION** | no LLM in the critical path (optional narrative only) | **12h** (`0 */12 * * *`) |
| `cross_target_raw` | `llm_planner` | raw signals across N subscribed targets | one multi-target pass | FINDING (`cross_target=true`) | yes | available kind — not in the live bring-up set (no descriptor) |
| `meta_findings_synthesizer` | `llm_planner` (META) | other analysts' first-order findings | second-order synthesis | FINDING (`meta=true`) | yes | 12h (`30 */12 * * *`) |
| `cross_analyst_correlator` | `llm_planner` | many analysts' outputs | contradiction / agreement / blind-spot detector | FINDING | yes | 12h (`45 */12 * * *`) |
| `critic` | `critic` | one analyst output + its `eval.rubric` | LLM judge → per-dimension scores | **CRITIQUE** | yes (heterogeneity-guarded) | 2h (`0 */2 * * *`) |
| `optimizer` | `dspy_compile` | an analyst's traces ⋈ critiques | DSPy + GEPA compile (Dapr Workflow) | **PROMPT_MODULE_CANDIDATE** | yes (isolated worker) | daily (`0 3 * * *`) |
| `consult_on_demand` | `react_loop` | free-form question + scope predicate | single-turn ReAct over 4 read tools | FINDING | yes | on-demand (no `fallback_schedule`) |
| `deep_consult` | `react_loop` | free-form deep-research question | schedules the deep-consult Dapr Workflow | FINDING | yes (within the workflow) | on-demand (no `fallback_schedule`) |
| **`journal_assessor`** (extension kind) | `llm_planner` (META, in-actor GATHER one-soul arc) | the whole organism via its own `journal_read` self-instruments (its own last entry / consolidation, plus `get_assessments` / `get_graph_structure` / `get_critic_scores` / `get_calibration` / `get_run_health` / `get_budget_status` / …) | one-soul PLAN→GATHER→NARRATE arc; narrate a coherent first-person POV OVER the system | **JOURNAL** → `journal_entries`, OFF-chain (empty `derived_from`) | yes (per-phase split: gpt-oss / vLLM gather + Opus 4.8 voice) | **12h** entry (`0 0,12 * * *`) / **daily** consolidator (`0 2 * * *`) |

Each cadence pairs with a `cooldown_seconds` held **below** the interval — a
cooldown stamped at run-completion that equals the interval lands past the next
fire and silently halves the cadence (the 6h→12h trap noted in
`analyst_country_assessor.yaml`). The eleven `OutputKind` values are FINDING,
SITUATION, HYPOTHESIS, PREDICTION, ALERT, META_FINDING, CRITIQUE, FACT, NEXUS,
PROMPT_MODULE_CANDIDATE, JOURNAL (`provenance/kinds.py:76-101`) — the eleventh,
`JOURNAL`, lands OFF the fact/finding/nexus chain (§3.10).

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
deterministically. This is the kind the 19 G20 country assessors instantiate (§8),
and the kind that carries the **knowledge-grounding** injection for the
world / country assessors — see §7.9.

#### 3.1.1 How `inline_target` is defined + wired

`inline_target` is worth tracing end-to-end because it is the archetype every
LLM-bearing finding kind follows: a declarative descriptor, a build-time wiring
pass that resolves the descriptor's references into a runnable handler, and a
pure run-time envelope. Three real files carry it — the kind module
`data/analysts/inline_target.py`, the builder branch
`runtime/analyst_deps_builder.py`, and the descriptor schema
`data/schemas/analyst.py` — instantiated by the two descriptors
`descriptors/analyst_country_assessor.yaml` and
`descriptors/analyst_world_assessor.yaml`.

**The descriptor anatomy.** An `AnalystDescriptor` (`schemas/analyst.py:578`)
declares:

- `identity.kind: inline_target` (`schemas/analyst.py:185`) — the registry key
  `discover_analyst_kinds` resolves to the module.
- `method.kind: llm_planner` + `method.prompt_module` + `method.llm.primary`
  (`schemas/analyst.py:399,409,419`). `llm.primary` is a `stack_ref`
  `FactoryValue` (`raw: llm.primary.openai_compat`, `expected_family:
  llm_provider`) the builder resolves against the live stack; `method.llm`
  also carries `max_tokens` and the per-analyst `budget_tokens_per_day`. The
  country assessor caps `max_tokens: 1536` / `budget_tokens_per_day: 400000`;
  the world assessor `2048` / `120000`.
- `subscription.targets` (`schemas/analyst.py:258`) — a `predicate` +
  `time_window` (`schemas/analyst.py:223,225`). **The presence of a `targets`
  block is the per-target fan-out switch**: `country_assessor.yaml` declares
  `predicate: 'has_tag("g20")'`, `time_window: 24h`, and the actor fans out one
  worker run per matched G20 target. `world_assessor.yaml` **omits** `targets`
  entirely (`AnalystActor._cadence_targets()` returns `None` → exactly one
  global run with `target_filter=None`, reading the tenant-wide 24h slice). One
  field flips the kind between 19 per-country runs and one world-picture run.
- `cadence.fallback_schedule` + `cadence.cooldown_seconds`
  (`schemas/analyst.py:455`) — `0 */6 * * *` with `cooldown_seconds: 21000`
  (5h50m, held below the 6h interval per the §3.0 cooldown trap).
- the `grounding` block (`schemas/analyst.py:596` → `GroundingBlock`) —
  `enabled: true`, `scope: [target_geo, slice_entities]`, `sources: [substrate]`,
  `max_facts: 30` (the Tier-1 current-world-state injection; §7.9).
- `action_packs` (`schemas/analyst.py:590`) — the country assessor grants
  `media_processing`, `incident_response`, `escalate_finding` (§5 / §5.0).
- `outputs` bindings (`schemas/analyst.py:586`) — the country assessor binds
  `a2a_skill` (`intelligence.g20_country_assessment`), `stix_bundle` (TLP:amber
  + file sink), and `alert` (`min_severity: high`); the world assessor binds
  only the `a2a_skill`. The `FindingPayload` lands in `analyst_outputs` via the
  kind's default `OUTPUT_KIND.FINDING` regardless of bindings.
- `eval.rubric` (`schemas/analyst.py:515`) — the weighted-dimension JSON the
  `country_critic` grades the finding against; required for the critic→optimizer
  loop. The critic now runs on the SAME core self-hosted plane as the analyst
  (`llm.primary.openai_compat`), so `allow_self_correlated: true` — it is no
  longer a cross-provider check (§3.8 / §6). The hosted Anthropic plane
  (`claude-opus-4-8`) is reserved for the consult / deep-consult kinds only.

**The build-time wiring.** At startup `discover_analyst_kinds`
(`data/analysts/__init__.py:104`) walks the kind modules and registers each by
its `KIND_NAME` (`inline_target.py:92`). When the deps-builder binds a
descriptor whose `identity.kind` is `inline_target`, it dispatches into
`_build_inline_target` (`analyst_deps_builder.py:324`), which:

1. **resolves the LLM** — the `method.llm.primary` StackRef is resolved via
   `build_llm_handler_from_stack_component` (`analyst_deps_builder.py:218`,
   called at `:342`) into a live chat handler.
2. **resolves the prompt module** → the system prompt string
   (`analyst_deps_builder.py:347`), from `method.prompt_module`.
3. **applies the GEPA promoted-prompt override** — `resolve_promoted_system_prompt`
   (`analyst_deps_builder.py:355`) reads the operator-promoted champion
   instruction for this analyst id from Postgres and overrides the static
   system prompt when one is promoted (closing the optimizer's promotion loop;
   §6.4).
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
- **ORIENT** (`:549`) — deterministically sort + trim to the newest
  `_MAX_INPUT_SIGNALS = 20` signals (`:175`, `:207`).
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
already structured.

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
final synthesis turn always yields a structured answer. Its tool whitelist is four
read-only substrate primitives, **three live today** — `search_signals`,
`query_facts`, `inspect_entity` — plus `vector_search`, a **designed seam pending
vector-store wiring** (it dispatches only when a vector store is wired). Write-back tools
are deliberately excluded — consult is a read over substrate. It emits a
`ConsultResponsePayload` (carried into substrate as a finding, and returned
directly to non-runtime dispatchers) with `cited_substrate_refs`.

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
Evidence without perspective is just a log file."* It is **LIVE** —
deployed and live-validated (a real off-chain entry, `honesty_flags` forced
deterministically from substrate metrics, receipt-chained, in-voice).

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
ceiling). The model planes are split per phase: the heavy GATHER investigation
loop runs on the **local gpt-oss / vLLM plane** (the core OpenAI-compatible
`llm.primary.openai_compat` stack component, with a "Reasoning: high" directive
injected into the gather system prompt only), while the **voice** — the in-voice
field-notes seam and the NARRATE synthesis — runs on the **Anthropic plane
(Opus 4.8)** (`method.llm.narrate` → `llm.anthropic.opus_4_7`). So Anthropic spend
is just the bounded final voice synthesis (`max_tokens` governs only the Opus
narrate; it is never sent to the vLLM gather, which serves its own budget); the
deep agentic loop is local. The deps-builder reads the optional
`method.llm.narrate.raw` (`method.llm` is an open dict, no schema change) and
resolves a second handler — an analyst without `method.llm.narrate` falls back to
the single primary handler, byte-unchanged.

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

> **Honest caveats.** The `change`-apply path (on accept of a `change` proposal)
> is import-verified but **not yet exercised against a live registry**; the
> `correction` and `self_revision` apply paths **are** tested end-to-end. The
> `system.journal` panel was tsc-green and fully wired but **pending its first
> real in-browser render** at the time of writing. Wave 5 — a critic + an
> optimizer over the journal's **own voice** — is **future / designed-not-built**,
> gated on first building a critic actuator.

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
is a separate, **continuous** job this handler owns. Each fire folds the next batch
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
| `graph_mining` | community detection, structural-balance triads, proxy-chain mining over the Apache AGE graph |
| `anomaly_detection` | signal-volume rate spikes, sentiment-shift z-scores, novel-entity emergence over `time_bucket()` windows on the primary Postgres pool |
| `structural_balance` | signed-edge triadic balance on the entity-relationship graph (§7.3) |
| `calibration_tracking` | analyst confidence-vs-outcome (Brier score, reliability bins, drift) (§7.4) |
| `adversarial_signals` | flags coordinated / manipulative signal patterns |
| `entity_gc` | garbage-collects stale / orphan entity profiles |
| `fact_decay` / `nexus_decay` | time-based confidence decay of facts and reified relationships |
| `finding_supersession` | links near-duplicate findings for the same situation so a newer finding supersedes the prior one (never a destructive delete; mirrors `signal_aliases`) |

**Future seam:** `finding_supersession` links near-dup findings, but full
finding-level supersession enforcement and situation clustering across the feed are
not yet end-to-end.

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
- `substrate_read.py` — the four read tool handlers (`search_signals`,
  `query_facts`, `inspect_entity`, `vector_search`) the consult kind is governed
  through.
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
`escalate_finding` pack when a landed finding crosses the pack's
severity/confidence gate (`runtime/dapr_actors.py`, `_maybe_escalate_finding` →
`bound.run_tool("escalate", …)`; gate `severity ≥ high` **or** `confidence ≥
0.85`, channel-emitting to `channels.escalations`). The escalate pack is
tag-scoped to `g20` so a non-G20 target denies with a visible governor BLOCK,
and the A-3 wiring + JetStream binding for `channels.>` / `governor.events.>`
landed in commits `10491f9` / `6938c0b`. (Whether specific invocation rows exist
*right now* is a live-DB fact this design doc does not assert — but the call
sites, channels, and ledger writes are wired and reachable, not stubs.)

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
   channels), and the four `substrate_read` tools (`search_signals` /
   `query_facts` / `inspect_entity` / `vector_search` — the consult kind's
   governed read surface). (The `discover_sources` tool was removed per
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
tamper-evident **SHA-256 receipt chain** (`runtime/receipt_chain_factory.py`,
`data/provenance/receipts.py`): each run links to the prior via `prev_receipt_hash`,
hydrated from the analyst's last trace at first use. The chain follows the analyst
identity across descriptor versions. (This is distinct from the descriptor
**registry's** Ed25519-signed audit log, which signs descriptor mutations — see
`ARCHITECTURE.md`.) A `critic` run (§3.8) lands a trace-level
`analyst_critiques` row keyed by `run_id`.

### 6.2 The critic

The critic is an LLM judge against the analyzed analyst's `eval.rubric`, with the
heterogeneity guard of §3.8. Rubrics are operator-authored content the descriptor's
`eval.rubric` field points at — the same manual quality procedures an analyst
would apply by hand, encoded for the critic to apply at scale.

The critic is not a spectator: it **actuates**. The surfaced confidence of a graded
finding is `effective_confidence = min(confidence, critic_score)` — a finding the
critic graded poorly surfaces a *lowered* confidence rather than its own self-reported
one (`data/registry/substrate_reads_api.py`, the S3 critic-actuator fields). The raw
`confidence`, the `critic_score`, and the folded `effective_confidence` are all stored
side-by-side, so the down-weighting is auditable, not destructive.

### 6.3 The optimizer — DSPy + GEPA as a Dapr Workflow

The optimizer (`optimizer.py`) reads an analyst's `analyst_traces` joined with its
`analyst_critiques` (default window 30 days, default minimum 50 ground-truth rows,
hard cap 500), and runs the **GEPA** evolutionary loop (reflective Pareto-frontier
prompt evolution, arXiv 2507.19457) over them to emit a candidate `prompt_module`.

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

A candidate's promotion to live is gated by the descriptor's `eval.promotion`
policy (`should_auto_promote`):

- `human_gated` (the default) — never auto-promotes. An operator promotes manually.
- `auto_with_threshold` — becomes eligible only after **5** successful manual
  promotions for that analyst (counted from prior promoted candidate rows) **and**
  when the candidate's eval score exceeds the parent's.

The candidate lands in `analyst_outputs` as a `PROMPT_MODULE_CANDIDATE` row; no
prompt goes live without clearing this gate.

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
relevant decay sub-handlers are `fact_decay` and `nexus_decay`.

> **Fact write-path supersession model (updated 2026-06-26 — task #101 Holes-A;
> see §7.8).** Supersession is **single-winner-by-recency within a source tier**,
> NOT a contested-claim arbiter. Two refinements landed: (1) it is **source-tier-
> aware** — a machine-extracted ingestion/agent fact no longer closes (supersedes)
> an open human-curated `seed`/`curated` fact (`seed == curated > ingestion ==
> agent`); same-tier recency still wins, so a legitimate leader change of the same
> tier supersedes as before; (2) when N sources **agree** on the same
> `(subject, predicate, value)`, confidence aggregates via a bounded **noisy-OR**
> (cap 0.99) rather than MAX, so corroboration raises belief. The model is still
> **single-winner**: it keys on `(subject, predicate)` latest-wins (the leader-change
> semantics §7.9 relies on), does not keep coexisting disputed values, and has no
> credibility-weighted arbitration. That full **contested-claim arbiter** (disputed
> values kept alive at the fact layer + a credibility-weighted surfaced winner) is
> **designed, not built** — `planning/CONTESTED_CLAIMS_PLAN.md`. Machine-extracted
> ingestion fact confidence is floored conservatively (`_INGESTION_DEFAULT_CONFIDENCE`
> ~0.5, below the 0.95 curated seed), not the old hardcoded 1.0.

### 7.3 The entity knowledge graph and structural balance

The knowledge graph lives in Apache AGE (a Postgres extension, graph `legba_graph`).
Vertices are `entity_profiles`; provenance edges are `signal_entity_links`; proposed
relationships accrue in `proposed_edges`. `entity_resolution` keeps it current
(§4.1) and `graph_mining` extends it.

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
load-bearing caveat).

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
  not MAX. **Still single-winner-by-recency within a tier** — coexisting disputed
  values and credibility-weighted arbitration are **designed, not built**
  (`planning/CONTESTED_CLAIMS_PLAN.md`).
- ~~**Predicate vocabulary unreconciled**~~ — **FIXED (Phase B)**:
  `vocabulary.normalize_predicate` converges both write paths on the canonical
  lowercase-spaced form.
- ~~**`signals.source_credibility` is 100% NULL**~~ — **FIXED (Phase D)**: populated
  at the canonical signal write-path via host lookup against the scored
  `source_credibility` table (unknown host → NULL by design).
- ~~**`graph_metrics` sink empty**~~ — **FIXED (Phase D)**: `structural_balance` /
  `graph_mining` / `nexus_decay` now write a `graph_metrics` row per run
  (`deterministic_handlers/_graph_metrics_sink.py`).
- ~~**`proposed_edges` are ungoverned**~~ — **FIXED (Phase D)**: the
  `proposed_edge_governance` analyst promotes well-corroborated pending edges into
  neutral `CoOccursWith` nexuses (conf ≥ floor) and rejects thin+stale ones; signed
  reification stays the LLM reifier's job.
- ~~**Entity-resolution fragmentation/mistyping**~~ — **FIXED (Phase C)**:
  `entity_resolution` runs a deterministic `canonicalize_entity` pass
  (`deterministic_handlers/_entity_canon.py`) before the dedup key: HTML-entity +
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
analyst that doesn't declare the block is untouched. Grounding is opted IN on the
`world_assessor` and `country_assessor` descriptors. **Canary passed live:** a US
assessment's context now contains "United States — head of state: Donald Trump (since
2025-01-20)".

**Tier 2 — vector `world_context` collection.** A curated unstructured-brief
collection (free-text background the structured facts can't carry) is a **declared
future seam**: the `GroundingBlock` accepts `vector:world_context` as a source so a
descriptor can pre-declare it, but the resolver only acts on the structured
`substrate` source today — Tier 2 needs the embedder-through-port wiring (L-114).
A descriptor that declares *only* the vector source resolves nothing and says so in
the log, rather than injecting an empty preamble.

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
- **Acute-hazard probabilistic forecasting (`forecast_acute`, the pilot).** A
  **rare-event / Poisson-process** model: the count of severe hazards in a
  region-week (frozen class-K sources — USGS M4.5+ earthquakes, NASA EONET events)
  is treated as Poisson with rate λ estimated from the historical record, and the
  forecast is the **tail probability** P(≥1 event in the FORWARD week) = 1 − e^(−λ).
  It answers a sharp **binary** question — *"will at least one severe acute hazard
  occur in country X next week?"*.

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
  falsifiability: the call can be *wrong*, and the record will show it. The acute
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

---

## 8. Proven end-to-end

The analysis plane is live in the real stack, cold-startable from empty volumes
(applying the single `0001_baseline.sql` schema migration):

- Real RSS sources (BBC, Deutsche Welle, Al Jazeera) produce ~200 enriched signals,
  each carrying geo, language, and entity classes promoted to indexed columns.
- Those signals fan out on `legba.signals.>` to **19 G20 country targets**.
- Each target is coalesced (§2) by a country-assessor `inline_target` analyst,
  producing distinct per-country findings with full `derived_from` provenance and
  per-analyst receipt chains.
- An ongoing `entity_resolution` deterministic analyst (§4.1) keeps an entity
  knowledge graph (`entity_profiles` / `signal_entity_links` / `proposed_edges`)
  current.

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
