# Legba — Life-of-a… End-to-End Flows

> **Status anchor:** this document is grounded in
> [`planning/ANALYSIS_LAYER_PLAN_2026-06-15.md`](../planning/ANALYSIS_LAYER_PLAN_2026-06-15.md)
> (the analysis-layer build plan & architecture anchor). It honors that doc's
> **altitude map** (§1) and **architecture-fit verdict** (§2). Every factual claim below
> cites `file:line`. **The data-analysis arc that plan called PIECES A–D (+ seeding) is
> now BUILT and LIVE** — what were the §5 PLANNED flows (deep consult, fact extraction)
> are walked here as Flows 5–9, and the plan's §4 "no producer / no `write_fact`"
> corrections are SUPERSEDED by the producing code each flow cites (refreshed against
> code at commit `a95d360`, 2026-06-16).

## How to read this

Walkthroughs, each a numbered step list with `file:line` citations:

| # | Flow | Altitude (per anchor §1) | State |
|---|---|---|---|
| 1 | **Life of a signal** — pull → baseline enrichment → fan-out → target | ingestion (feeds 0–3) | **LIVE** |
| 2 | **An analyst cadence cycle** — reminder → fan-out → slice → run_method → write → emit | 1 (first-order / maintenance) | **LIVE** |
| 3 | **A consult** — POST → actor ReAct loop → chat-SSE / deep-workflow | 3 (on-demand) | **LIVE** |
| 4 | **The optimizer Dapr workflow** — schedule → stages → promotion | meta (self-improvement) | **LIVE** |
| 5 | **A deep consult** — POST → 202 task → Dapr workflow (plan→acquire→analyze→synthesize) | 3 (on-demand, deep) | **LIVE** |
| 6 | **Fact extraction + supersession** — signal → fact_extractor → write_fact → fact_decay | 0 (extraction) | **LIVE** |
| 7 | **Nexus reification** — proposed_edges → relationship_reifier 8B typing → write_nexus → refine | 1 (meta) | **LIVE** |
| 8 | **ACH competing-hypotheses** — read set → matrix/diagnosticity → ±2 → write_hypothesis | 1 (meta) | **LIVE** |
| 9 | **Seeding import** — fetch → map → resolve → write_fact/write_nexus → seed_batches | 0 (curated import) | **LIVE** |
| 10 | **A grounded assessment** — cadence fire → GROUND phase → current-facts resolve → dated preamble → LLM | 1 (injection) | **LIVE** |

> **Two analysis tiers (ARCHITECTURE §0).** Flow 1 is the **TIER-1 INLINE** tier —
> the deterministic `data/filters/` baseline pipeline run *per-Signal at acquisition,
> before fan-out, no LLM* (its writes: enriched signals + altitude-0 facts + entities).
> Flow 6 (fact extraction) is the inline `fact_extractor` stage *inside* Tier 1. Every
> other flow here (2-5, 7-10) is the **TIER-2 SLICE/CADENCE** tier — `data/analysts/`
> reading accumulated slices/substrate on a Dapr reminder or reactive trigger and
> *reasoning*. The two never collapse: Tier 1 is at-ingest and cheap, Tier 2 is
> cadence-batched so LLM cost is decoupled from the ingest firehose.

**Now-LIVE data-analysis arc** (the 2026-06-16 build closed the rigor gap — the
prior PLANNED §5a/§5b and the §4-corrections that said "no producer" are now
SUPERSEDED by working code; every claim below cites the producing `file:line`):
- **`OutputKind.FACT` (`= "fact"`) and `OutputKind.NEXUS` (`= "nexus"`) now exist**
  alongside the original eight — ten members
  (`src/legba/data/provenance/kinds.py:69-87`), with real `write_fact`
  (`src/legba/data/provenance/writes.py:385`) and `write_nexus`
  (`writes.py:416`) routes into the dedicated `facts` / `nexuses` tables.
  Migration 0032 added `valid_until` / `superseded_by` / `confidence_components`
  to `facts` + the open-only partial-unique index
  (`src/legba/data/migrations/0032_facts_decay_columns.sql:25-62`); migration 0033
  created the `nexuses` table (`0033_nexuses.sql:30-94`); migration 0034 added the
  `seed_batches` ledger + `seed_batch_id` FK on both tables (`0034_seed_batches.sql`).
- `meta_findings_synthesizer` + `cross_analyst_correlator` (and the new
  `relationship_reifier` / `competing_hypotheses` / `calibration_tracking` /
  `deep_consult` / `structural_balance` / `graph_mining` / `nexus_decay` /
  `fact_decay`) are now **REGISTERED** by descriptor in
  `scripts/bringup_register_analysts.py:58-75`, so they produce rows on cadence.

---

## 1. Life of a signal (LIVE)

**One sentence:** a `SourceCore` actor pulls raw entries from its source on a cadence,
runs the per-source baseline (structured enrichment → optional eager media → the
optional NLP enrichment filter chain) **once** over each one, writes the single
canonical signal, and publishes it to NATS where per-target durable consumers fan it
out to every subscribed target. "Enrich once, read many"
(`src/legba/data/sources/baseline.py:5-8`).

> **This whole flow is the TIER-1 INLINE analysis tier** (ARCHITECTURE §0 / §6.1) —
> the `data/filters/` baseline pipeline run *synchronously per-Signal at acquisition,
> BEFORE fan-out*, **deterministic, no analyst LLM** (GLiREL / DeBERTa-zero-shot /
> pycountry+Nominatim + local dedupe). Its substrate writes are altitude-0: the
> **enriched signal** (geo/language/tags/entity_classes in-place on the one `signals`
> row), altitude-0 **`facts`** (`source_type='ingestion'`, `valid_from`-stamped —
> Flow 6), and **entity rows + `signal_entity_links`** off the NER spans. The full
> inline stage chain is `language_detect → geocode → ner_multilingual → classify →
> source_credibility → ingest_dedupe (dedupe_4tier tiers 1-2) → fact_extractor`
> (`src/legba/data/filters/__init__.py`). The **TIER-2** slice/cadence analysts
> (`data/analysts/`, Flows 2 / 6-10) are a *separate* tier — they read accumulated
> slices/substrate on a reminder and *reason*, never per-signal.

1. **Poll fires.** `SourceCore.pull_once()` is the poll entry point. It reads the
   persisted cursor (`since`), builds the source handler, and iterates raw entries
   under a count+wall-time budget so the poll always finishes inside Dapr's drain
   window (`src/legba/runtime/source_actor.py:597`, budget gate at lines 647-649).

2. **Per-entry processing.** For each raw entry the actor calls
   `self._process_one(conn, ctx, raw)`
   (`src/legba/runtime/source_actor.py:658`; method def at `:482`).

3. **Baseline runs once, at the source.** `_process_one` invokes `run_baseline(signal,
   ctx, …, enrichment_stage=self.sd.enrichment_stage)`
   (`src/legba/runtime/source_actor.py:490-493`). `run_baseline` applies three tiers in
   order (`src/legba/data/sources/baseline.py:242`):
   - **Tier 1 — structured enrichment (always, cheap):** `_enrich_structured(signal,
     ctx)` populates the typed columns the subscription layer pushes down to SQL/NATS —
     `language` / `tags` / `geo` / `entity_classes` from scope hints + payload
     (`src/legba/data/sources/baseline.py:283`, impl at `:135`).
   - **Tier 2 — eager media (per-source flag):** only when
     `descriptor.pipeline.media == "eager"` and the signal has a `media_ref`, fetch +
     process via a registered `MediaExtractor`
     (`src/legba/data/sources/baseline.py:286-287`). **Seam:** no media-modality
     extractor ships in-tree; an eager media signal with no real registered extractor
     raises `MediaEndpointNotConfiguredError` — typed, loud, no row written (module
     docstring `baseline.py:30-41`; `default_extractor_registry` ships only the
     text/structured passthrough at `:102-116`).
   - **Tier 3 — NLP enrichment filter chain (optional, descriptor-ordered):** if
     `enrichment_stage` is wired, `await enrichment_stage(signal, ctx)` runs the
     `descriptor.pipeline.enrichment` chain in declared order — the live shape is
     `language_detect → geocode → ner_multilingual → classify → source_credibility →
     ingest_dedupe (dedupe_4tier, tiers 1-2) → fact_extractor` — and may **drop** the
     signal by returning `None` (`src/legba/data/sources/baseline.py:290-294`;
     factory + per-stage annotate-back at `src/legba/runtime/dapr_host.py:1499-1592`).
     This is where the INLINE tier's altitude-0 writes happen besides the enriched
     signal: `ner_multilingual` promotes entity spans (→ entity rows +
     `signal_entity_links`) and `fact_extractor` writes `facts`
     (`source_type='ingestion'`; Flow 6). The host wires this hook from the registry
     pipeline factory (`src/legba/runtime/source_first_runtime.py:181-203`); tests can
     omit it. Note `dedupe_4tier`'s expensive tiers 3-4 are the *semantic* Qdrant
     vector tier — tiers 1-2 (content-hash / structured) run inline here, the
     vector tier is the only piece that touches Qdrant.

   > The baseline **mutates the signal in place AND returns it** — the in-place
   > mutation keeps the handler-yielded object authoritative; the return lets a filter
   > replace or drop it (`src/legba/data/sources/baseline.py:275-278`).

4. **Canonical write.** A surviving signal is written canonically via
   `write_canonical_signal(...)`, which pins `owner_tenant` on the DB row and stamps the
   enrichment columns into the single `signals` table — there is no separate enrichment
   table (`src/legba/runtime/source_actor.py:520`; func at `:336`). Signals are
   **source-owned** and **target-agnostic** — they carry no `target_id`
   (`src/legba/runtime/dapr_actors.py:3029-3031`).

5. **Immediate publish.** Each written signal is published **as it is written**, not
   batched at the end, so fan-out survives a later cap/error/drain:
   `await self._publish([sig])` (`src/legba/runtime/source_actor.py:661-663`). The
   publish routes to a coarse NATS subject
   `legba.signals.{tenant}.{source_token}.{modality}.{event_class}`
   (`src/legba/data/nats.py:98-115`). Subject tokens cannot contain dots, so the source
   id is flattened by `subject_token()` (`src/legba/data/nats.py:86-95`).

6. **Cursor advance.** Whether the pull completed, capped, or errored, the cursor is
   always advanced for forward progress (advance policy at
   `src/legba/runtime/source_actor.py:669-714`). Content-hash dedup makes any window
   overlap a no-op.

7. **Fan-out to targets (subscription engine).** The shared `legba_signals` JetStream
   stream captures by subject filter; each target has **one aggregated durable pull
   consumer** bound to the union of coarse subject filters from its bindings
   (`src/legba/runtime/subscription/engine.py:117-186`; consumer provisioning at
   `src/legba/data/nats.py:244-313`). Matching is **two-stage**:
   1. **SQL WHERE on indexed columns** — `source_id` + `owner_tenant` pinned, then
      structured predicates on `geo` / `tags` / `entity_classes` (GIN) and
      `languages` / `modalities` (btree) (`src/legba/runtime/subscription/filter.py:62`).
   2. **Starlark residual** — `residual_matches()` compiles + evaluates the
      subscription's residual predicate on the narrowed set with a 5ms wall-clock budget
      (`src/legba/runtime/subscription/filter.py:194-219`;
      `src/legba/data/predicates/compiler.py:131-176`).

8. **Arrival at the target.** The signal is now in the per-target consumer's pull
   window. From here the analyst cadence cycle (Flow 2) reads it.

**Key shapes:** `Signal` (`src/legba/data/sources/_contract.py:136`),
`SourceDescriptor.pipeline` (`src/legba/data/schemas/source.py:119`),
`Subscription` (`src/legba/data/schemas/source.py:223`).

---

## 2. An analyst cadence cycle (LIVE)

**One sentence:** the **primary** `AnalystActor` owns one cadence reminder; on each tick
it matches active targets with a Starlark predicate, fans one run out **per matched
target** to per-worker actors (bounded at 5), and each worker reads its substrate slice,
invokes the kind's `run_method`, writes the typed output, extends the receipt chain,
emits output bindings, and may escalate.

> **Two fire paths into the same per-target run (`AnalystActor.run`).** An analyst run is
> reached by EITHER of two triggers, and both converge on the identical per-target work
> in §2c:
> 1. **Dapr-reminder cadence** (this flow, §2a/§2b) — the periodic floor: a quiet target
>    still gets re-evaluated on schedule.
> 2. **Reactive NATS coalescer** — a matched signal (or a new upstream `derived` finding)
>    marks the `(analyst, target)` pair *dirty* via the `TriggerEngine`'s durable pull
>    subscription over `legba_signals`, and the coalescer fires the analyst when a gate
>    trips (severity-wake / accumulation / cadence), CAS-claimed exactly-once so the
>    reminder tick and the reactive path never double-fire the same batch. LLM-bearing
>    kinds are floored to a batch ≥ 2 so a busy target can never fan out one LLM call per
>    signal (`src/legba/runtime/triggers/engine.py`, `coalescer.py`, `policy.py`).
>
> See `ANALYSIS.md` §2 for the coalescing-trigger decision kernel (gates + clamp + the
> exactly-once dispatch) in full; the steps below walk the reminder-cadence path.

### 2a. Cadence registration (once, at activation)

1. On `_on_activate`, the primary actor registers a single Dapr reminder named
   `run_cadence`, timed from `descriptor.cadence.fallback_schedule` via
   `cron_to_reminder_timing(schedule)`
   (`src/legba/runtime/dapr_actors.py:1234-1240`; helper imported at `:96`). Workers
   carry **no** reminder — the primary owns cadence.

### 2b. The tick

2. **Reminder fires.** `AnalystActor.receive_reminder("run_cadence", …)` runs the stale-
   fire guard `_reminder_guard` (self-disarms on version bump / pause), then resolves the
   matched targets (`src/legba/runtime/dapr_actors.py:1295`, guard at `:1307`).

3. **Target matching.** `_cadence_targets()` evaluates the
   `subscription.targets` Starlark predicate (ANALYST_SUBSCRIPTION surface, e.g.
   `has_tag('g20')`) against the active target descriptors
   (`src/legba/runtime/dapr_actors.py:1320`, method at `:1464`). Three regimes:
   - **Target-bound analyst** → one run per matched target (`target_filter` set per
     target) (`:1342-1349`).
   - **Critic-kind meta analyst** → `_critic_ungraded_targets()` resolves the newest-N
     ungraded findings and fans one bounded worker grade per finding row (the
     `target_filter` is parsed by critic's READ_SLICE as the analyzed_output_id)
     (`:1330-1338`). This was the fix that un-stuck the critic→optimizer eval loop.
   - **Other meta analyst** (no target binding) → a single global run
     `await self.run({"trigger_kind": "cadence"})` (`:1339-1340`).

4. **Fan-out (A2 concurrency).** `_fanout_to_workers(targets)` chunks targets at
   `_FANOUT_CHUNK` (= 5, `src/legba/runtime/dapr_actors.py:548`) and dispatches each to a
   **distinct** worker actor id `analyst::<descriptor_id>::<target_id>` so each gets its
   own Dapr turn-queue and runs concurrently
   (`src/legba/runtime/dapr_actors.py:1351`, bounded-concurrent dispatch at `:1374`).
   The primary only orchestrates; it does not run the per-target work itself.

### 2c. The per-target run (`AnalystActor.run`)

`run()` is the main work entry (`src/legba/runtime/dapr_actors.py:1590`). For each worker:

5. **Lazy-activate.** A worker with a `target_filter` and no state record creates an
   ACTIVE record inline (`_minimal_worker_record`), with no reminder (primary owns
   cadence) (`:1617-1620`).

6. **Per-target cooldown + slack.** The run is gated by a per-target cooldown keyed by
   `target_filter`/`_global` in `cooldown_by_target`; a 5%-of-cooldown slack (capped
   600s) absorbs drift when `cooldown_seconds ≈ cadence interval` (fixes the 6h→12h
   silent halving) (`:1642-1670`; see commit `cefd8ca`).

7. **Read the substrate slice.** The kind's `read_slice` adapter runs (critic reads a
   specific `analyst_outputs` row by id; the default reads the recent **signal** window).
   The default reader `_read_substrate_slice` honors
   `subscription.targets.time_window` (e.g. `"336h"`), falling back to legacy flat attrs
   then **24h** (`src/legba/runtime/dapr_actors.py:2989`, window resolution
   `:3006-3027`). It narrows by the target's `source_id` refs and `geo` scope so each
   country target reads its own slice, not the global pool (`:3035-3059`).

8. **Budget precall.** `deps_bundle.budget.precall_check(conn, estimated_tokens)` projects
   the run against the daily cap → `throttle` / `exhausted` / `global_exhausted`; an
   exhausted outcome records a demotion audit and a strategy
   (`dlq` / `demote_and_continue` / `pause_until_next_window`)
   (`src/legba/runtime/dapr_actors.py:1721`). **Seam:** the
   `demote_and_continue` real cheap-model fallback is a declared seam (SEAMS F-2) — it
   logs a pause-until-reset instead of swapping in a real fallback model (`:1770-1794`).

9. **Invoke the kind's run_method.** `_invoke_run_method` dispatches the 3-arg form
   `run_method(inputs, options, kind_deps)` when `kind_deps` is present (else a 2-arg
   fallback) (`src/legba/runtime/dapr_actors.py:2570`). The kind, its LLM handler, and
   its deps bundle were resolved at deps-build time by `build_analyst_run_method`, which
   dispatches across the registered kinds (inline_target / cross_target_raw /
   meta_findings_synthesizer / cross_analyst_correlator / deterministic / predictor /
   critic / optimizer / consult_on_demand …)
   (`src/legba/runtime/analyst_deps_builder.py:99`). Transient failures retry with
   exponential backoff (max 3) via the exception classifier
   (`src/legba/runtime/dapr_actors.py` retry loop ~`:1911`, classifier `:2226`).
   - **Tier-1 knowledge grounding (`inline_target` only, opt-in).** When the analyst's
     descriptor declares `grounding.enabled: true`, the inline_target deps carry a
     `grounding_hook` installed at deps-build time (`_build_inline_target` →
     `_build_grounding_hook`, `src/legba/runtime/analyst_deps_builder.py:367`,`:378`).
     The kind's `run_method` then runs a **GROUND phase** between PLAN and REASON+ACT
     that prepends a dated "AUTHORITATIVE CURRENT CONTEXT" preamble to the LLM user
     prompt (`src/legba/data/analysts/inline_target.py:592-612`). **Flow 10 walks this
     in full.** Off (byte-for-byte unchanged) for every analyst that doesn't opt in.

10. **Select the output payload by kind.** `_select_output_payload(method_result,
    output_kind)` uses the per-`OutputKind` selector table (FINDING→`finding`,
    PREDICTION→`finding.data["prediction"]`, CRITIQUE→`finding.data["critique"]`, …)
    (`src/legba/runtime/dapr_actors.py:2665`, table near `:2657`).

11. **Write the typed output.** `write_analyst_output(conn, analyst_ctx=…, kind=…,
    output_payload=…, derived_from=…)` validates against the kind's pydantic model and
    routes the INSERT to the right table — `situations` / `hypotheses` /
    generic `analyst_outputs` (`src/legba/runtime/dapr_actors.py:1981-1989`; writer
    `src/legba/data/provenance/writes.py:115`, per-kind routing `:375`). A validation
    failure routes to `output_dead_letter` and the run reports HARD_FAIL (`:1990-1999`).

12. **Extend the receipt chain.** When a `receipt_chain` is wired, `record(...)` writes a
    tamper-evident SHA-256 chain row into `analyst_traces` carrying `intermediate_steps`
    + `tool_calls` and the output-row id, producing `(receipt_hash, prev_receipt_hash)`
    (`src/legba/runtime/dapr_actors.py:2013-2034`; hashing
    `src/legba/data/provenance/_core.py:352-383`).

13. **NATS publish + emit bindings.** The write helper publishes the output envelope on
    `analyst.{analyst_id}.{channel}` (channel by kind: findings / situations /
    predictions / critiques / …, `src/legba/runtime/dapr_actors.py:2368-2379`). Then
    `_emit_output_bindings(...)` discovers the descriptor's output-kind handlers and
    dispatches `emit(payload, descriptor, deps, output_id, derived_from, target_id)`
    best-effort. Two bindings are wired:
    - **STIX 2.1 bundle** emitter (`src/legba/runtime/dapr_actors.py:2142`, func at
      `:2419`; bindings `src/legba/data/outputs/stix_bundle.py:112`) — wired in commits
      `cb621b8`/`a9744a0`.
    - **alert.emit** — `country_assessor` binds the `alert` kind; the binding's
      `emit(...)` coerces the live `FindingPayload` to an `AlertPayload` **gated by the
      descriptor's `config.min_severity` / `config.min_confidence`** (sub-threshold
      findings short-circuit to `[]`), routes severity-aware surfaces, and writes
      per-attempt `alert_sink_deliveries` audit rows (`output_id` → `ctx.alert_row_id`)
      (`src/legba/data/outputs/alert.py:588-628`).

14. **Optional escalation (A-3c).** For findings, `_maybe_escalate_finding(...)` gates on
    severity/confidence vs the pack gates, resolves `target.allowed_action_packs` +
    scope, and runs the escalation tool through the agency pipeline governance
    (`src/legba/runtime/dapr_actors.py:2163`, func at `:2485`).

15. **Outcome.** `run()` returns an `ActorRunOutcome`
    (SUCCESS / TRANSIENT_FAIL / BUDGET_THROTTLED / HARD_FAIL / NOOP).

**Altitude note (anchor §1):** today's LIVE producers at altitude 1 are
`inline_target` (country_assessor), `llm_planner` (world_assessor), and `predictor`;
the maintenance kinds (situation_clustering, finding_supersession, critic, predictor,
STIX emit) are LIVE. The **altitude-2 meta kinds ride this exact same cadence + fan-out
rail unchanged** — they are built but await a descriptor (anchor §2.3, §4).

---

## 3. A consult (LIVE)

**One sentence:** an operator question POSTed to the registry invokes the
`consult_default` actor through Dapr (180s blocking); the actor runs a bounded ReAct
loop over read-only substrate tools and — by **`mode`** — either returns a typed
`ConsultResponsePayload` IN the envelope with **NO row written** (`mode=chat`,
streamed step-by-step to the browser over SSE) or persists a `FINDING` the endpoint
reads back (`mode=deep`). For the **detached** deep-analysis job see Flow 5.

The request carries `mode: "chat" | "deep"` (default `chat`), an optional
client-minted `request_id` (for subscribe-before-POST SSE), and a client-held
`messages[]` history for multi-turn chat
(`src/legba/data/registry/consult_api.py:109`, invoke body at `:322-334`). The request's
`max_tool_rounds` defaults to 10 / ceiling 30 (`consult_api.py:107`); the handler's
`CHAT_DEFAULT_ROUNDS = 10` is the chat default (Piece 1, D1
`src/legba/data/analysts/consult_on_demand.py:117`).

1. **SPA POST.** `panels/system/Consult.tsx` POSTs `{question, scope_predicate,
   max_tool_rounds, mode, request_id, messages}` to `/api/v1/consult`
   (`legba-ui-v3/src/panels/system/Consult.tsx`). For `mode=chat` the SPA mints
   `request_id` and **subscribes to the SSE step relay first** —
   `GET /api/v1/consult/stream/{request_id}` (`src/legba/data/registry/consult_stream_api.py:78`)
   — then POSTs, so live ReAct steps published to the request-scoped core NATS subject
   `legba.consult.steps.{request_id}` are relayed as Server-Sent Events; steps
   published before attach are lost by design (best-effort live view, no replay)
   (`consult_stream_api.py:32-41`, subject at `:94`).

2. **Endpoint resolves the descriptor.** `invoke_consult` resolves the head version of
   the `consult_default` analyst via the descriptor registry's typed `get(...)`
   (`src/legba/data/registry/consult_api.py:210`, resolve at `:224-227`; router factory
   at `:196`; registered on the app at `src/legba/data/registry/server.py:249`).

3. **Build the actor id + invoke Dapr.** It builds the canonical actor id
   `analyst::consult_default::<version[:16]>` and PUTs to the Dapr sidecar
   `…/actors/AnalystActor/{actor_id}/method/run` with a 180s timeout
   (`src/legba/data/registry/consult_api.py:140-143`, invoke ~`:262-283`).

4. **Dispatch to the consult kind.** `AnalystActor.run` dispatches to
   `consult_on_demand.run_method(inputs, options, deps)`
   (`src/legba/runtime/dapr_actors.py:1980-1989`).

5. **ReAct loop.** The handler renders the prompt and loops `range(deps.max_rounds)`,
   where `deps.max_rounds` defaults to the constant **`MAX_TOOL_ROUNDS = 6`**
   (`src/legba/data/analysts/consult_on_demand.py:626` loop, `:111` constant, `:557`
   default): LLM call → parse JSON → if final, break; else dispatch a tool and append the
   result. After the cap, one forced final turn runs with tools unavailable.
   - **Multi-turn (chat).** The handler reads the client-held `messages[]` history off
     `inputs[0]` and seeds the conversation with it before the ReAct loop, so chat is
     stateless on the server but multi-turn on the client
     (`src/legba/data/analysts/consult_on_demand.py:668`, messages seed at `:681`).
   - **Step streaming (chat).** When a `request_id` + a NATS publisher are wired, every
     ReAct trace step is also pushed to `legba.consult.steps.{request_id}` so the live
     SSE stream and the durable trace are one source of truth
     (`consult_on_demand.py:581-583`).

6. **Tool dispatch.** Four whitelisted **read-only** tools route through
   `SubstrateQueryPort` — `search_signals`, `query_facts`, `inspect_entity`,
   `vector_search`; unknown tools error; exceptions fold back into the conversation
   (`src/legba/data/analysts/consult_on_demand.py:336-382`). Write-side tools are
   deliberately excluded by design.
   - **Now LIVE (Flow 6):** `query_facts` / `inspect_entity` read the `facts` table /
     entity graph — which is **no longer empty** now that the `fact_extractor` stage
     (Flow 6) and the seeding import (Flow 9) write real facts. The earlier "empty
     store" caveat is superseded.

7. **Build the response payload.** `ConsultResponsePayload` carries `answer` (≤65KB),
   deduplicated + hallucination-guarded `cited_substrate_refs`, `uncertainty` [0-1], and
   `unanswered_aspects` (`src/legba/data/analysts/consult_on_demand.py:390-461`).

8. **Terminate by `mode`.** The runtime branches in `AnalystActor.run` AFTER the budget
   `record(...)` + `derived_from` resolution (so chat still meters tokens + reports
   lineage), GUARDING the write path rather than forking it
   (`src/legba/runtime/dapr_actors.py:2086-2104`):
   - **`mode=chat`** → return the typed `ConsultResponsePayload` IN the envelope —
     **no row, no receipt chain, no output event, no emit bindings, no escalation** —
     and publish one terminal `{"type":"final"}` frame to
     `legba.consult.steps.{request_id}` so the SSE relay closes deterministically
     (`dapr_actors.py:2034-2053`, chat return at `:2097-2104`). The endpoint **skips the
     DB read-back** and projects the payload straight from the envelope with
     `finding_id=None` (`consult_api.py:423-434`).
   - **`mode=deep`** → `_wrap_as_finding(...)` nests the payload inside a `FindingPayload`
     (`src/legba/data/analysts/consult_on_demand.py:464-493`); the runtime writes it via
     `write_analyst_output(OutputKind.FINDING)` to `analyst_outputs`
     (`dapr_actors.py:2121-2129`). The endpoint reads the finding row back and projects
     it into `ConsultResponse`, **preferring `payload.answer` over `row.body`**
     (`consult_api.py:436-462`).

9. **Return + render.** The JSON `ConsultResponse` (answer, finding_id — `null` for chat,
   derived_from, tool_calls, cited_refs, uncertainty, unanswered_aspects) returns to the
   SPA, which renders the answer markdown, an uncertainty label, cited refs as clickable
   lineage links, and a collapsible tool trace.

**Current shape (kept honest):** chat is **server-stateless but client-multi-turn**
(the SPA holds `messages[]`) and **writes no finding by default**; the live ReAct steps
arrive over SSE while the authoritative trace is in the POST response. Deep mode persists
one finding row per call; the answer still lives in two places
(`FindingPayload.body` and `ConsultResponsePayload.answer`, both 65KB-capped) and the
endpoint prefers the payload.

---

## 4. The optimizer Dapr workflow (LIVE)

**One sentence:** the `optimizer` analyst kind (a meta-tier cadence run) schedules a
durable Dapr **Workflow** from inside its actor run; the workflow validates the training
set, then compiles a GEPA candidate prompt module (DSPy under a custom non-litellm LM
adapter), writes the candidate as a `PROMPT_MODULE_CANDIDATE` row, and an
**operator-gated** promotion flips a candidate into the analyst's live system prompt.

This is the durability substrate that replaced Temporal — one Dapr control plane (anchor
§2.2). The optimizer kind calls through a stable `temporal_client` interface (the name is
historical; it just means "workflow client",
`src/legba/data/analysts/optimizer.py:303`).

1. **Cadence run.** The optimizer is a meta analyst (no target binding) — its cadence
   tick reaches `run()` as a single global run (Flow 2, step 3, third regime). Its
   `read_slice` fetches trace+critique rows joined via `trace_id`
   (`src/legba/data/analysts/optimizer.py:121-186`) — this is why the critic must run
   first to produce graded rows.

2. **Build the workflow input.** `run_method` constructs `OptimizerWorkflowInput`
   (analyst id/version, parent prompt-module path, training set, GEPA budget knobs,
   promotion policy, min-traces/min-critiques) and dispatches via `_dispatch_workflow`
   (`src/legba/data/analysts/optimizer.py:444-619`, input at `:491`, dispatch at `:520`).

3. **Schedule the workflow.** `_dispatch_workflow` calls
   `temporal_client.start_optimizer_workflow(input, workflow_id=…)` then awaits
   `handle.result()` (`src/legba/data/analysts/optimizer.py:741-763`). The Dapr client
   converts the input to a dict and calls `client.schedule_new_workflow(optimizer_workflow,
   …)` on a thread, returning a `DaprWorkflowHandle`
   (`src/legba/runtime/dapr_workflow/client.py:221`, schedule at `:239`).
   - **CRITICAL:** the Dapr instance id MUST NOT contain `::` (activity result parsing
     splits on `::` and would hang forever) — the id uses `optimizer.` instead
     (`src/legba/data/analysts/optimizer.py:508-519`).

4. **Orchestrator runs (stages).** `optimizer_workflow` is registered on the
   `WorkflowRuntime` **by function name** (the #37 fix) alongside its two activities
   (`src/legba/runtime/dapr_workflow/worker.py:100-102`; orchestrator
   `src/legba/runtime/dapr_workflow/workflow.py:134`). It is deterministic — no
   wall-clock / RNG / I/O in the body; all non-determinism is pushed into activities
   (`workflow.py:20-24`). Stages:
   1. `yield validate_training_set_activity(...)` — checks the set has enough
      traces/critiques; on `ok=False` the workflow stops
      (`src/legba/runtime/dapr_workflow/workflow.py:161`, activity at `:99`).
   2. `yield compile_candidate_activity(...)` **with a retry policy** — runs
      `asyncio.run(_run_gepa_loop(payload))`
      (`src/legba/runtime/dapr_workflow/workflow.py:112-126`, `:180`). Retry policy
      applies only to the activity, not the orchestrator (`:152-158`).

5. **The GEPA loop (shared core).** `_run_gepa_loop` loads the parent prompt, scores a
   baseline, tries the real DSPy/GEPA path, and falls back to a deterministic naive
   candidate search (`src/legba/runtime/dapr_workflow/gepa.py:254-314`). The same loop
   is used by the `InProcessWorkflowClient` fallback (`gepa.py:179-190`) so behavior is
   identical with or without a daprd sidecar.

6. **LLM routing — never litellm.** `_run_dspy_gepa_with_lm` resolves the LM via
   `configure_gepa_lm`, which builds a `LegbaProviderLM` (a custom `dspy.BaseLM` adapter)
   routing **all** LLM calls through Legba's own `LLMProviderHandler`, never litellm
   (`src/legba/runtime/dapr_workflow/gepa.py:389-442`;
   `src/legba/runtime/dapr_workflow/dspy_lm.py:192-238`). It scopes the LM via
   `dspy.context(lm=lm)` on a background loop (`_AsyncLoopBridge`) to avoid nested
   event-loop / cross-loop errors (this is the operator hard rule — dspy/litellm never
   in the analyst inference path).

7. **Write the candidate.** The workflow result rehydrates as `OptimizerWorkflowResult`
   (candidate text, training-set size, eval score + delta, generation, diagnostics)
   (`src/legba/runtime/dapr_workflow/gepa.py:100-119`). `run_method` writes it as a row
   with `kind = OutputKind.PROMPT_MODULE_CANDIDATE` into the generic `analyst_outputs`
   table (`src/legba/data/analysts/optimizer.py:532-555`,
   `OUTPUT_KIND = OutputKind.PROMPT_MODULE_CANDIDATE` at `:71`).

8. **Promotion (operator-gated).** A candidate becomes the analyst's live system prompt
   only when its `data->>'promotion_gate'` is flipped to `'promoted'`:
   - `should_auto_promote(...)` defaults to `human_gated` → never auto-promotes
     (`src/legba/data/analysts/optimizer.py:376-414`). The only auto path is
     `auto_with_threshold`, still keyed on a `promoted` gate set externally by the
     operator's promotion action (`:401-419`).
   - `resolve_promoted_system_prompt(analyst_id)` returns the live prompt by selecting
     the candidate whose `promotion_gate='promoted'` — this is the closed loop from
     champion instruction → live system prompt
     (`src/legba/data/analysts/optimizer.py:326-369`).

**Bootstrap wiring:** the Dapr host builds the `DaprOptimizerWorkflowClient` and (when
`LEGBA_EMBED_WORKFLOW_WORKER=1`, the default) embeds the `WorkflowRuntime` worker
in-process (`src/legba/runtime/dapr_host.py:976-1010`). **TWO** workflows are now
registered by function name on the runtime — `optimizer_workflow` and
`deep_consult_workflow` + its four stage activities
(`src/legba/runtime/dapr_workflow/worker.py:107-117`); Flow 5 walks the second one.

---

## 5. A deep consult (LIVE)

**One sentence:** the UI's "Deep Consult" POSTs a question to a DETACHED endpoint that
HTTP-invokes the `deep_consult` analyst actor over the runtime's Dapr sidecar; the
actor's `run_method` SCHEDULES a durable `deep_consult_workflow` (plan → acquire →
analyze → synthesize) and returns a **task id in <1s** (202, NOT the 180s block of
Flow 3); the workflow's synthesize stage writes the finding (+ optional facts/
hypotheses), and a status poll reads it back. It reuses Flow 4's actor→workflow bridge,
pointed at analysis instead of optimization.

1. **Submit (202, detached).** `POST /api/v1/deep_consult` `{question, scope_predicate,
   emit_facts, emit_hypotheses}` resolves the `deep_consult` analyst head version,
   builds the actor id `analyst::deep_consult::<version[:16]>`, mints a `run_id`, and
   PUTs to the sidecar `…/actors/AnalystActor/{actor_id}/method/run` with a **30s**
   timeout (the actor returns immediately, so a short timeout suffices)
   (`src/legba/data/registry/deep_consult_api.py:132-179`). The success envelope must
   carry `task_id`; the endpoint returns `202` `{task_id, status, run_id}`
   (`deep_consult_api.py:246-260`).

2. **Actor short-circuit.** The runtime branches in `AnalystActor.run` on
   `descriptor.identity.kind == "deep_consult"`: it returns
   `{outcome:success, mode:deep_consult, task_id, status, run_id}` WITHOUT writing a row
   — same guard shape as the chat short-circuit (Flow 3 step 8)
   (`src/legba/runtime/dapr_actors.py:2063-2077`). The `deep_consult` kind's `run_method`
   builds the `DeepConsultWorkflowInput`, mints a `::`-free instance id
   `deep_consult.{scope}.{run8}` (the optimizer-hang GOTCHA: no `::` in the id), and
   schedules via the client WITHOUT awaiting `result()`, returning the task id
   (`src/legba/data/analysts/deep_consult.py:111-170`,
   `start_deep_consult_workflow` at `src/legba/runtime/dapr_workflow/deep_consult_client.py:111-125`).

3. **Workflow registered (by function name).** `deep_consult_workflow` + its four stage
   activities are registered on the `WorkflowRuntime` beside the optimizer's
   (`src/legba/runtime/dapr_workflow/worker.py:113-117`). The orchestrator body is the
   deterministic dict-chaining generator: `plan → acquire → analyze → synthesize`, each
   `yield ctx.call_activity(...)` with a retry policy; stage N's output dict is stage N+1's
   input (`src/legba/runtime/dapr_workflow/deep_consult_workflow.py:248-293`). A
   `plan.ok == False` short-circuits to an empty finding-id result (`:272-285`).

4. **Stages reuse the existing primitives (discipline §7 — refactor to share, never fork):**
   - **plan** — one LLM turn decomposes the question into a tool plan
     (`plan_activity` `deep_consult_workflow.py:190`; `_run_plan`).
   - **acquire** — runs the plan's tool calls against the **same read-only substrate port**
     as chat consult (`acquire_activity:203` / `_run_acquire`).
   - **analyze** — bounded synthesis over the evidence under the **same budget plane**
     (`analyze_activity:216` / `_run_analyze`).
   - **synthesize** — REUSES the provenance write paths VERBATIM: `write_finding`, plus
     (gated on `emit_facts` / `emit_hypotheses`) `write_fact` / `write_hypothesis`
     (`synthesize_activity:229` / `_run_synthesize`
     `src/legba/runtime/dapr_workflow/deep_consult.py:607-682`).

5. **Status poll.** `GET /api/v1/deep_consult/{task_id}` parses the trailing `run8` (first
   8 hex of the run_id) out of the instance id and SELECTs the produced `finding` row from
   `analyst_outputs WHERE kind='finding' AND analyst_id='deep_consult' AND
   replace(run_id::text,'-','') LIKE run8||'%'`: a row present → `completed` with
   `finding_id` + `answer` + `cited_refs` (the finding's `derived_from`); absent →
   `running` (`src/legba/data/registry/deep_consult_api.py:262-332`). The registry has no
   workflow-engine gRPC channel; the finding row is the authoritative completion signal.

6. **LLM adapter caveat.** If a stage drives DSPy it reuses the `LegbaProviderLM` +
   `_AsyncLoopBridge` + `dspy.context(lm=…)` scoping (never litellm), same as the
   optimizer (`src/legba/runtime/dapr_workflow/dspy_lm.py:192-238`).

---

## 6. Fact extraction + supersession (LIVE)

**One sentence:** the `fact_extractor` enrichment stage is the **TIER-1 INLINE** tier's
altitude-0 fact writer — it rides Flow 1 step 3 tier-3 (last in the inline chain, beside
the NER stage, deterministic / no analyst LLM), turns each in-flight `Signal` into atomic
`(subject, predicate, value)` facts stamped `source_type='ingestion'` + `valid_from`=event time, closes any
prior open fact whose value changed (supersession), and writes via the same open-only
upsert the analyst `write_fact` path uses — lighting up Consult (Flow 3) and the
`fact_decay` maintenance handler.

1. **Stage runs (enrichment-only, degrade-not-drop).** `FactExtractorHandler.transform`
   is a `descriptor.pipeline.enrichment` stage (wired by the registry pipeline factory in
   `dapr_host._source_enrichment_factory`, `src/legba/runtime/dapr_host.py:1429-1440`). It
   ALWAYS returns the signal unchanged and NEVER raises — on any failure it logs, flips
   health to degraded, and returns the signal
   (`src/legba/data/filters/fact_extractor.py:334-367`). The per-source `enrichment` gate
   IS the cost throttle — keep it OFF high-volume/low-value feeds (`:159-165`).

2. **Extract triples by backend.** `backend="relation"` (default, zero new infra) reuses
   the GLiREL triples already on `payload["entities"]` from the upstream
   `ner_multilingual` stage, reconstructing `(subject, predicate, object)` by pairing
   consecutive endpoints that share a predicate; when `entities` is absent it calls the
   hosted `POST /extract` itself (the same call NER makes)
   (`fact_extractor.py:380-416`). `backend="llm"` (opt-in, declared) routes the signal
   text through the analyst LLM plane via an injected `llm_handler_factory`; selecting it
   without one raises `FactExtractorUnconfigured` (no stub) (`:418-455`, guard `:271-276`).

3. **Filter the endpoints.** Both endpoints pass through the shared NER numbers/dates/units
   rejection (`_is_nonentity_candidate`); when a descriptor opts into
   `reject_quantity_endpoints` a triple whose subject OR value is ENTIRELY spelled-out
   numbers / ordinals / quantity-qualifiers ("sixth", "at least five") is dropped — the
   slice GLiREL's synthesised `confidence=1.0` can't be floored against; a single nominal
   token keeps the endpoint (`fact_extractor.py:104-116`, gate at `:487-498`).

4. **Resolve event-time.** `_event_time(signal)` reuses the source actor's exact cursor
   precedence — payload `_published_at_dt` / `_last_seen_dt` / `_event_dt`, else
   `signal.fetched_at` — always tz-aware UTC; a NULL `valid_from` fails loud rather than
   collapsing to the `1970` sentinel (`fact_extractor.py:137-151`, `:637-642`).

5. **Supersede prior, then upsert.** `_insert_ingestion_fact` runs `supersede_prior_facts`
   FIRST — closing any OPEN fact for `(lower(subject), lower(predicate))` whose VALUE
   DIFFERS (`valid_until=now()` + `superseded_by=<new id>`; a same-value re-assert closes
   nothing) — then INSERTs the new open fact with `source_type='ingestion'`, ON CONFLICT on
   `(lower(subject), lower(predicate), lower(value), COALESCE(valid_from,'1970…'))` WHERE
   open → lift `confidence` to the max + union `derived_from`
   (`fact_extractor.py:602-681`; `supersede_prior_facts`
   `src/legba/data/provenance/writes.py:658-714`). This is the SAME write contract the
   analyst-output `write_fact` → `_insert_fact` path uses, so the two producers agree
   (`writes.py:385-413`, `_insert_fact:717-819`). Optional AGE edges fire only when
   `emit_graph_edges` is set (ships false) (`fact_extractor.py:553-568`).

6. **Decay maintains.** The `fact_decay` deterministic handler now operates on real data —
   migration 0032 added the columns its UPDATEs reference. It (a) expires facts with a past
   `valid_until` (set `superseded_by`/close) and (b) decays stale-but-open confidence into
   `confidence_components.decay`, returning a FINDING receipt
   (`src/legba/data/analysts/deterministic_handlers/fact_decay.py:34-77`, `handle:110`).

7. **Consult lights up.** Consult's `query_facts` / `inspect_entity` tools (Flow 3 step 6)
   now read populated tables.

---

## 7. Nexus reification (LIVE)

**One sentence:** the `relationship_reifier` META analyst sweeps co-mentioned entity
pairs from `proposed_edges`, has an 8B LLM TYPE each as a canonical
`rel_type` + signed `polarity` + `intent` + `channel` (+ optional `intermediary`),
side-writes a first-class `nexus` row per typed pair via `write_nexus` (supersession on a
polarity/label change), and the dormant `structural_balance` / `graph_mining` /
`nexus_decay` handlers then refine over the now-SIGNED graph.

1. **Cadence run (one global META sweep).** The reifier is a META analyst (no target
   binding) → its cadence tick reaches `run()` as a single global run (Flow 2 step 3,
   third regime). `run_method` reads candidate pairs and side-writes nexus rows on its own
   connection (`src/legba/data/analysts/relationship_reifier.py:402-413`); its
   `OUTPUT_KIND` is FINDING (the per-run summary receipt — the nexus rows are side-written,
   exactly like `situation_clustering` side-writes situations) (`:72-77`).

2. **Read candidates.** `_read_candidates` pulls pending `proposed_edges` (the
   `entity_resolution` producer's `co_occurs` edges) with `confidence >=`
   `MIN_EDGE_CONFIDENCE` (0.45) that are NOT already reified into an OPEN nexus, ordered by
   confidence, capped at `MAX_CANDIDATES_PER_RUN` (40)
   (`relationship_reifier.py:317-339`, knobs `:91-98`). Each candidate is enriched with the
   pair's recent OPEN facts as typing context (`_recent_facts_for:342-359`).

3. **Type via the 8B LLM (never litellm; budget-gated).** Per candidate the run checks
   `deps.budget.check_envelope()` (stop issuing new calls once exhausted — degrade-not-drop)
   then one `chat_complete` typing call returns ONE JSON object
   `{related, subject, object, intermediary, rel_type, polarity, intent, channel,
   confidence}` (`relationship_reifier.py:150-199`, loop `:461-518`). `_coerce_typing`
   skips `related=false` / off-list `rel_type`, and `_canonical_polarity` takes the
   authoritative `POLARITY` table sign for the `rel_type` (the SAME table the
   `structural_balance` consumer owns — one canonical map) else the LLM's sign
   (`:242-309`, table import `:64`).

4. **Write the nexus (supersession on change).** `write_nexus` → `_insert_nexus` runs
   `supersede_prior_nexuses` FIRST — closing any OPEN nexus for the typed triple
   `(subject, COALESCE(intermediary,''), object, rel_type)` whose `polarity` OR `label`
   DIFFERS — then INSERTs open with ON CONFLICT on the `idx_nexuses_triple_open` partial
   index → lift confidence + union `derived_from`/`source_signal_ids` (a faithful copy of
   `_insert_fact`) (`src/legba/data/provenance/writes.py:416-442`, `_insert_nexus:888-997`,
   `supersede_prior_nexuses:822-885`). `valid_from` = the pair's `produced_at` (its
   co-mention event clock) (`relationship_reifier.py:526-530`).

5. **Refine over the signed graph (the PIECE A light-up).** The dormant deterministic
   handlers now read the OPEN signed nexuses DIRECTLY (their own pg_pool):
   - **structural_balance** — `_augment_from_nexuses` pulls non-neutral
     (`polarity <> 0`) open nexuses as canonical signed edges, enumerates triads, and
     classifies balanced / unbalanced (the signed-triad theory)
     (`src/legba/data/analysts/deterministic_handlers/structural_balance.py:275-302`,
     `handle:387`).
   - **graph_mining** — `_augment_from_nexuses` adds DIRECTED signed edges (subject →
     intermediary → object when a cut-out is set) so the proxy-chain **sign-product**
     mining sees hostile-via-proxy (negative product)
     (`graph_mining.py:322-347`, proxy chains `:171-220`, `handle:428`).
   - **nexus_decay** — the nexuses-table maintenance twin of `fact_decay`: decays stale
     open-nexus confidence, returning a FINDING (`nexus_decay.py:27-35`, `handle:65`).

---

## 8. ACH competing-hypotheses (LIVE)

**One sentence:** the `competing_hypotheses` (alias `ach`) META analyst re-homes the old
Legba ACH rigor — for each focal situation it reads the temporally-CURRENT evidence base,
has the LLM propose ≥2 MUTUALLY-EXCLUSIVE hypotheses each with a MANDATORY counter-thesis,
scores a reproducible evidence×hypothesis matrix with diagnosticity weighting, computes an
integer evidence balance, auto-transitions the lead/dominated hypotheses past ±2, and
side-writes one `HYPOTHESIS` row per hypothesis via the live `write_hypothesis` path. It is
NOT gated on `active` situations (the gate that starved the old `hypothesis_lifecycle`).

1. **Cadence run (one global META sweep).** Same META rail as Flow 7: `run_method`
   side-writes HYPOTHESIS rows + returns a FINDING summary
   (`src/legba/data/analysts/competing_hypotheses.py:717-728`, `OUTPUT_KIND=FINDING:88-94`).

2. **Read the focal topics + temporally-current evidence.** `_read_focal_topics` pulls
   recent `situations` by `intensity_score DESC` (last 14d, capped at `MAX_TOPICS_PER_RUN`
   = 12), NOT filtered on `status` (`:251-266`). `_read_evidence_for_topic` assembles up to
   `MAX_EVIDENCE_PER_TOPIC` (24) interchangeable items from three sources: linked
   **findings** (`analyst_outputs kind='finding'` via the situation's `derived_from`),
   current **facts** (`superseded_by IS NULL AND valid_until IS NULL` — the open-row query
   Piece B made meaningful), and open signed **nexuses** (Piece A) overlapping the topic's
   entities; each item's `id` is a real substrate UUID for lineage
   (`:269-371`).

3. **Generate the competing set (LLM enrichment + deterministic fallback).** Budget-gated
   `chat_complete` returns `{hypotheses:[{thesis, counter_thesis}, …]}`; `_coerce_hypotheses`
   enforces ≥`MIN_HYPOTHESES` (2) entries each with a non-empty thesis AND counter-thesis.
   Any LLM/parse failure or budget pause falls back to the deterministic escalate /
   de-escalate / status-quo triad so the matrix always gets built
   (`competing_hypotheses.py:175-196`, `_generate_hypotheses:458-510`,
   `_deterministic_hypotheses:379-401`).

4. **Score the matrix + diagnosticity (LLM-scored by default; lexical is the fallback).**
   Each `(evidence, hypothesis)` cell is scored on Heuer's CC/C/N/I/II scale (+2..−2) by
   the LLM — one batched call per topic through the analyst provider plane (never
   litellm/dspy), budget-gated via `check_envelope()`
   (`competing_hypotheses._score_consistency_matrix_llm`, called in `run_method`). The
   LLM cell scores OVERRIDE the deterministic scorer. Only when the budget envelope is
   exhausted (or the LLM is unavailable / unparsable) does the run fall back **per cell**
   to the transparent lexical/polarity scorer `_score_consistency` (escalation vs
   de-escalation keyword cues plus signed-nexus polarity) — so lexical is the
   budget-exhausted fallback, not the primary path. Each hypothesis row records which
   path ran under `diagnostic_evidence[].matrix_scorer` (`"llm"` or `"lexical"`).
   `_diagnosticity` then weighs each item by its SPREAD (max−min) across the hypotheses —
   evidence consistent with EVERY hypothesis weighs ~0 (the ACH core). The evidence base
   is scoped to the topic's **resolved-entity set** (`entity_profiles` canonical names,
   exact membership), not a `LIKE '%name%'` substring. See `ANALYSIS.md` §7.5 for the
   methodology framing.

5. **Integer evidence balance + ±2 auto-transitions.** The per-hypothesis balance sums the
   diagnosticity-weighted SIGN of each diagnostic cell, rounded to an integer (robust to
   confidence gaming). `_status_for`: the LEAD whose balance ≥ `CONFIRM_K` (2) →
   `confirmed`; any with balance ≤ −`REFUTE_K` (2) → `refuted`; else `active`
   (`:633-691`, `:694-709`, constants `:122-123`).

6. **Write one HYPOTHESIS row per hypothesis.** `write_hypothesis` writes `thesis` +
   `counter_thesis` (hot columns), `supporting_signals` / `refuting_signals` (the diagnostic
   evidence ids consistent / inconsistent with THIS hypothesis), `evidence_balance`,
   `status`, and the full ACH matrix/diagnosticity under the `diagnostic_evidence` jsonb
   column — REUSING the existing `hypotheses` table + `OutputKind.HYPOTHESIS`, no new write
   plumbing (`competing_hypotheses.py:845-904`; `write_hypothesis`
   `src/legba/data/provenance/writes.py:287-302`, `_insert_hypothesis:612-655`).

7. **Resolution + calibration loop (Brier).** `run_method` runs the **exogenous**
   resolver FIRST — `_resolve_hypotheses_against_subsequent_facts` grades each open
   hypothesis against facts produced AFTER it (net escalate/de-escalate direction of the
   subsequent facts vs the thesis direction), stamping `resolved_by='subsequent_facts'`.
   It now **ABSTAINS** on UNDIRECTED theses (status-quo / non-directional claims), which
   were auto-grading TRUE and inflating the headline rate (DQ-H2b); the operator-label
   path (`resolved_by='operator:<id>'`) outranks it. Hypotheses that reach a terminal
   `confirmed`/`refuted` status with no exogenous resolution fall back to
   `_resolve_hypotheses_by_status_transition` (`resolved_by='status_transition'`, a
   SELF-CONSISTENCY stamp). The `calibration_tracking` deterministic handler then reads
   `resolved_outcome` and **segregates the two**: `_is_exogenous` /
   `_SELF_CONSISTENCY_SOURCES` split the sample so it reports a `brier_exogenous` vs a
   `brier_self_consistency` and flags `insufficient_exogenous` when too few world-graded
   rows exist — never letting a self-consistency Brier masquerade as calibration against
   reality (`src/legba/data/analysts/deterministic_handlers/calibration_tracking.py`,
   `handle`). See `ANALYSIS.md` §7.4 for the methodology.

---

## 9. Seeding import (LIVE)

**One sentence:** `scripts/seed.py` runs a registered `SeedSource` adapter through
`SeedDriver.run_seed_source` — fetch → map → resolve every entity endpoint against
`entity_profiles` (reusing the `entity_resolution` ON CONFLICT upsert) → write each fact
via `write_fact` and each nexus via `write_nexus` stamped `source_type` + `seed_batch_id`
→ record the `seed_batches` ledger row; idempotent (re-import rides the open-only
temporal-triple uniqueness as an upsert no-op).

1. **CLI invoke.** `scripts/seed.py --source world_baseline` (or `--dry-run` for fetch+map
   only, `--list` for adapters) loads `PostgresConfig.from_env()`, opens an asyncpg pool,
   and calls `run_seed_source(pool, adapter)` (`scripts/seed.py:51-66`). This is **not a
   single-adapter path** — `--list` shows **four** registered live adapters:
   `world_baseline` (curated-YAML, walked below), `wikidata_leaders` (Wikidata SPARQL →
   `LeaderOf` facts), `acled_conflict` (ACLED conflict-events backfill → facts + signed
   nexuses), and `sipri_arms_transfers` (curated-SIPRI-YAML arms transfers → signed
   nexuses). Each rides the same `SeedDriver` fetch → map → resolve → write loop; only the
   `fetch`/`map` differ.

2. **Fetch → map.** `run_seed_source` calls `source.fetch(ctx)` then `source.map(raw)` →
   typed `SeedEntity` / `SeedFact` / `SeedNexus` payloads
   (`src/legba/data/seed/_driver.py:144-172`). On `dry_run` it reports the would-write
   counts and touches nothing (no batch row, no writes) (`:186-191`).

3. **Create the batch row first.** So the FK stamp on each fact/nexus is valid, the driver
   INSERTs the `seed_batches` row (source, kind, `source_type`, manifest) and keeps the id;
   counts are filled in at the end (`_driver.py:195-209`, `:309-314`). Migration 0034
   created `seed_batches` + the nullable indexed `seed_batch_id` FK on both `facts` and
   `nexuses` (`src/legba/data/migrations/0034_seed_batches.sql:30-85`).

4. **Resolve entities (dedupe-or-create).** `_resolve_entity` upserts each endpoint into
   `entity_profiles` with `ON CONFLICT (lower(canonical_name))` — the EXACT contract the
   `entity_resolution` sub-handler uses, so a seeded entity and a live mention of the same
   name fold to ONE row, never a duplicate (`_driver.py:95-136`, calls `:213-275`).

5. **Write facts + nexuses (stamped, idempotent).** Each `SeedFact` → `write_fact`
   (`FactPayload`) and each `SeedNexus` → `write_nexus` (`NexusPayload`), both passing
   `source_type=source.source_type` + `seed_batch_id=batch_id`. The provenance ctx is a
   synthetic `seed.<source>` analyst id (no target). `write_fact`/`write_nexus` honor
   `source_type` / `seed_batch_id` ONLY on the `facts` / `nexuses` insert routes
   (other kinds ignore them); a re-import lands on the open-only upsert and leaves the
   marker untouched — a per-record failure is logged + skipped (degrade-not-drop), never
   aborts the batch (`_driver.py:231-307`; `write_analyst_output` source-type plumbing
   `src/legba/data/provenance/writes.py:128-153`, `:450-518`).

6. **The `world_baseline` adapter.** The curated-YAML proof adapter (no network) reads
   `seeds/world_baseline.yaml` and maps: each leader → a `SeedFact`
   `(subject=leader, predicate='LeaderOf', value=country, valid_from=term start,
   confidence 0.95)` PLUS one country-subject office `SeedFact`
   `(subject=country, predicate='head of state', value=leader, valid_from=term start)`
   — the supersession-correct shape (keyed on the country, so a leader CHANGE closes the
   prior officeholder rather than leaving two open "current" rows,
   `src/legba/data/seed/adapters/world_baseline.py:110-128`); each alliance membership →
   a typed SIGNED `SeedNexus` `(subject=country, rel_type='MemberOf', object=bloc,
   polarity=+1, channel='institutional', valid_from=accession)` — written DIRECTLY, no
   LLM reifier (operator decision: relational seeds map to nexuses directly; the reifier
   of Flow 7 is for free-text) (`src/legba/data/seed/adapters/world_baseline.py:49-155`).

7. **The `wikidata_leaders` adapter (live SPARQL → current officeholders, the grounding
   feed).** The first *structured-external* adapter — it pulls the SAME knowledge shape
   from a live authoritative source, and is the curation half of the knowledge-grounding
   fix (Flow 10): its current-officeholder facts are what the GROUND phase injects.
   - **fetch.** Two guarded (SSRF-checked) SPARQL GETs against the public Wikidata Query
     Service: current heads of state/government per sovereign state with their term-start
     qualifier (the `FILTER NOT EXISTS { … P582 ?end }` open-tenure gate), and `member of`
     (P463) bloc memberships with accession dates
     (`src/legba/data/seed/adapters/wikidata_leaders.py:82-109`, `_query:209`). A
     fixture (`ctx.options['sparql_json']`) short-circuits the network for tests/dry-run
     mapping (`:185-198`).
   - **bare-QID label resolution.** The SPARQL label service sometimes returns a BARE QID
     instead of a name (live-observed: US `Q22686` has dozens of language labels but no
     `en` one). `_resolve_bare_qid_labels` gathers every bare-QID `*Label` cell, does ONE
     batched (chunked at 50) `wbgetentities` Action-API call, and rewrites the cell in
     place — preferring `labels.en.value`, FALLING BACK to the **enwiki sitelink title**
     (which IS "Donald Trump" for `Q22686`). A QID the API still can't resolve is left
     bare so `map` drops it — the adapter NEVER emits a `Qxxxx` value
     (`wikidata_leaders.py:241-368`).
   - **map (supersession-correct).** Each leader → a `LeaderOf` `SeedFact` (subject=leader)
     PLUS, per country, ONE country-subject office `SeedFact`
     `(subject=country, predicate='head of state', value=leader, valid_from=term start)`
     — preferring the executive (head_of_government) where both P6/P35 hold. This is the
     SAME canonical `head of state` predicate `world_baseline` uses, so a fresh Wikidata
     pull SUPERSEDES a stale curated leader for the same country via the Phase-B
     `valid_until` + `superseded_by` write path. A leader with no parseable term-start is
     SKIPPED (a fabricated `valid_from` would poison decay/supersession). Memberships →
     signed `+1 MemberOf` nexuses (`wikidata_leaders.py:374-516`). **Live-verified:** US
     head of state resolves to "Donald Trump" (since 2025-01-20), current, superseding the
     bare QID. Confidence 0.92 (slightly below curated 0.95: live extraction).

8. **Finalize.** The batch row's `counts` jsonb is UPDATEd with the run totals; the
   `SeedRunResult` (`counts` + `manifest` + `errors`) is returned and printed
   (`_driver.py:309-316`, result shape `:46-65`).

---

## 10. A grounded assessment (LIVE)

**One sentence:** a `world_assessor` / `country_assessor` run (Flow 2's `inline_target`
rail) opted INTO grounding (`descriptor.grounding.enabled`) runs a **GROUND phase**
before its LLM call — a `SubstrateGroundingResolver` reads the CURRENT authoritative
substrate facts (head of state, bloc memberships) about the target geo + the slice's top
entities, `build_grounding_preamble` renders them into a dated "AUTHORITATIVE CURRENT
CONTEXT" block, and the runner PREPENDS it to the LLM user prompt — so a stale-cutoff
model reasons over current ground truth (e.g. Trump = the CURRENT US president since
2025-01-20) instead of its training prior (which called him "former"). The substrate
that Flows 6/7/9 fill (temporal facts + reified nexuses + seed roots, esp.
`wikidata_leaders`) IS the grounding store; this flow is the *injection* half.

**Why it exists:** the analyst LLM's training cutoff predates the 2024 US election, so
left to its own prior it backfilled "former President Trump". The signal slice rarely
restates such background facts, so the model had no in-context correction
(`src/legba/runtime/grounding.py:3-25`). The fix curates current data IN (Flow 9
`wikidata_leaders`) and INJECTS it at analysis time (here).

1. **Opt-in at deps-build (once).** The descriptor's `grounding` block
   (`enabled` / `scope` / `sources` / `max_facts`, off by default —
   `src/legba/data/schemas/analyst.py:522-575`) gates a deps-builder step. Only when
   `grounding.enabled: true` AND a substrate `pg_pool` is wired does
   `_build_grounding_hook` construct a `SubstrateGroundingResolver` (closing over the
   pool) + a per-run `_hook` closure and install it on the `InlineTargetDeps.grounding_hook`
   field (`src/legba/runtime/analyst_deps_builder.py:367`, hook builder `:378-439`,
   `_build_inline_target` wiring `:368-374`). Off → `grounding_hook=None` → the run path
   is byte-for-byte unchanged. **Opted in on `analyst_world_assessor.yaml:110-114` +
   `analyst_country_assessor.yaml:117-121`** (`scope: [target_geo, slice_entities]`,
   `sources: [substrate]`, `max_facts: 30`).

2. **Cadence fire → ORIENT → PLAN.** The run reaches `inline_target.run_method` via
   Flow 2 (cadence reminder → fan-out → `_invoke_run_method`); ORIENT trims the slice and
   PLAN renders the base user prompt (`src/legba/data/analysts/inline_target.py:548-590`).
   The GROUND phase sits AFTER PLAN, BEFORE REASON+ACT.

3. **GROUND phase fires (only when the hook is wired).** `run_method` calls
   `await deps.grounding_hook(sliced, options)` inside a try/except — **degrade-not-drop**:
   any failure logs and leaves the prompt untouched (grounding is an enrichment, never
   fails the run) (`inline_target.py:592-612`).

4. **Collect candidates (deterministic, no DB).** `collect_grounding_candidates` reads
   ONLY the in-memory slice + the run's `target_id` and returns a de-duplicated,
   length-capped (≤24) list of names in priority order: `target_geo` first (the
   `country_<name>` target-id token + the most-frequent `geo` codes across the slice),
   then `slice_entities` (the NER/analyst `tags` + structured `key_entities`), with junk
   tags dropped (`src/legba/runtime/grounding.py:154-233`). A global `world_assessor` run
   has no `target_id`, so its grounding leans on `slice_entities`; a per-country
   `country_assessor` run gets the country itself first via `target_geo`.

5. **Resolve CURRENT facts + signed nexuses.** `SubstrateGroundingResolver.resolve`
   queries the `facts` table for any candidate as the SUBJECT under the **current-facts
   gate** `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())` —
   the SAME temporal-honesty gate `substrate_query_port` uses — ordered
   `source_type IN ('seed','curated') DESC, confidence DESC, valid_from DESC` so seeded
   ground truth outranks a hallucinated live fact, capped at `max_facts`. It also
   excludes bare-QID values **in SQL** (`value !~ '^Q[0-9]+$'`) so the LIMIT budget is
   spent only on renderable facts, with a Python backstop. A small leftover budget folds
   in current signed nexuses (alliances/hostility) the same way (capped at 12)
   (`src/legba/runtime/grounding.py:275-383`). An empty candidate set short-circuits to
   `([], [])` (no query).

6. **Render the dated preamble.** `build_grounding_preamble` emits one block headed
   `AUTHORITATIVE CURRENT CONTEXT (as of <today> — treat as ground truth over any prior
   knowledge …)`, one line per fact (`<subject> — <predicate>: <value> (since <date>)`)
   then the signed relationships (`[supportive]` / `[antagonistic]`). Returns `None` when
   there is nothing current to inject (so no stray header is prepended). **Bare-QID
   values/edges are skipped at the resolver chokepoint** — an unreadable `Q22686` line is
   worse than no line, so the flow degrades to no-grounding for that fact rather than
   inject it (`src/legba/runtime/grounding.py:61-73`,`:333`,`:372`,`:391-423`).

7. **Prepend + reason.** A non-empty preamble is concatenated AHEAD of the rendered slice
   (`user_prompt = f"{preamble}\n{user_prompt}"`), a `ground` step is stamped into the
   trace (`inject_preamble` / `no_current_facts`), and REASON+ACT makes the single
   `chat_complete` call over the grounded prompt (`inline_target.py:604-626`). The rest of
   the run (REFLECT → NARRATE → PERSIST, Flow 2 steps 10-15) is unchanged.

**Canary (live-verified):** a US assessment's prompt context now contains
"United States — head of state: Donald Trump (since 2025-01-20)" — sourced from the
`wikidata_leaders` seed fact (Flow 9 step 7), current under the supersession gate.

**Honest caveats:**
- **Tier 2 is a declared FUTURE seam.** Only the structured-`substrate` source is wired.
  The schema accepts `vector:world_context` so a descriptor can pre-declare it, but the
  resolver acts ONLY on `substrate` today; a descriptor that declares ONLY a vector
  source resolves nothing and logs that it built no preamble
  (`src/legba/data/schemas/analyst.py:572` field; docstring `:555-560`;
  `src/legba/runtime/analyst_deps_builder.py:419-431`). The vector collection needs the
  embedder-through-port (L-114).
- **Grounding only fires for `inline_target` analysts.** The hook lives on
  `InlineTargetDeps`; other LLM kinds (predictor narrative, consult, deep_consult) have
  no grounding wiring. Both opted-in assessors ARE `kind: inline_target` (the
  `world_assessor` descriptor justifies this at `analyst_world_assessor.yaml:8-16`).
- **It only corrects what the substrate actually holds.** A current fact absent from the
  seed/curated store can't be injected — grounding is as good as Flow 9's curation, and
  the resolver's exact-subject match means a name variant the slice uses but the facts
  table doesn't key on simply won't resolve (degrade to no-grounding, never a wrong fact).

---

## Appendix — primary entry-point index

| Concern | File:line |
|---|---|
| Source poll | `src/legba/runtime/source_actor.py:597` (`pull_once`) |
| Per-signal baseline | `src/legba/data/sources/baseline.py:242` (`run_baseline`) |
| Canonical signal write | `src/legba/runtime/source_actor.py:336` (`write_canonical_signal`) |
| NATS signal subject | `src/legba/data/nats.py:98` (`signal_subject`) |
| Subscription match | `src/legba/runtime/subscription/filter.py:62` |
| Cadence reminder register | `src/legba/runtime/dapr_actors.py:1234` |
| Cadence reminder fire | `src/legba/runtime/dapr_actors.py:1295` (`receive_reminder`) |
| Target matching | `src/legba/runtime/dapr_actors.py:1464` (`_cadence_targets`) |
| Per-target fan-out | `src/legba/runtime/dapr_actors.py:1351` (`_fanout_to_workers`) |
| Per-target run | `src/legba/runtime/dapr_actors.py:1590` (`run`) |
| Substrate slice read | `src/legba/runtime/dapr_actors.py:2989` (`_read_substrate_slice`) |
| Kind dispatch | `src/legba/runtime/analyst_deps_builder.py:99` (`build_analyst_run_method`) |
| Typed output write | `src/legba/data/provenance/writes.py:117` (`write_analyst_output`) |
| OutputKind enum (10) | `src/legba/data/provenance/kinds.py:57` (`FACT`:79 / `NEXUS`:84) |
| Emit bindings | `src/legba/runtime/dapr_actors.py:2419` (`_emit_output_bindings`) |
| STIX emit | `src/legba/data/outputs/stix_bundle.py:112` |
| alert.emit binding | `src/legba/data/outputs/alert.py:588` (`emit`) |
| Consult endpoint | `src/legba/data/registry/consult_api.py:281` (`/consult`, `mode`) |
| Consult SSE relay | `src/legba/data/registry/consult_stream_api.py:78` (`/consult/stream/{id}`) |
| Consult ReAct loop | `src/legba/data/analysts/consult_on_demand.py:626` |
| Deep-consult endpoint | `src/legba/data/registry/deep_consult_api.py:132` (`POST` → 202) |
| Deep-consult kind (schedule) | `src/legba/data/analysts/deep_consult.py:111` (`run_method`) |
| Deep-consult workflow | `src/legba/runtime/dapr_workflow/deep_consult_workflow.py:248` |
| Optimizer kind | `src/legba/data/analysts/optimizer.py:444` (`run_method`) |
| Workflow client | `src/legba/runtime/dapr_workflow/client.py:221` |
| Workflow orchestrator | `src/legba/runtime/dapr_workflow/workflow.py:134` |
| Workflow worker (2 workflows) | `src/legba/runtime/dapr_workflow/worker.py:58` (`build_workflow_runtime`) |
| GEPA loop | `src/legba/runtime/dapr_workflow/gepa.py:254` (`_run_gepa_loop`) |
| DSPy LM adapter (no litellm) | `src/legba/runtime/dapr_workflow/dspy_lm.py:147` |
| Promotion (operator-gated) | `src/legba/data/analysts/optimizer.py:326` / `:376` |
| Fact extractor stage | `src/legba/data/filters/fact_extractor.py:334` (`transform`) |
| write_fact / supersede | `src/legba/data/provenance/writes.py:385` / `:658` |
| Relationship reifier | `src/legba/data/analysts/relationship_reifier.py:402` (`run_method`) |
| write_nexus / supersede | `src/legba/data/provenance/writes.py:416` / `:822` |
| ACH competing-hypotheses | `src/legba/data/analysts/competing_hypotheses.py:717` (`run_method`) |
| Calibration (Brier) | `src/legba/data/analysts/deterministic_handlers/calibration_tracking.py:424` |
| Seed driver | `src/legba/data/seed/_driver.py:144` (`run_seed_source`) |
| Wikidata leaders adapter | `src/legba/data/seed/adapters/wikidata_leaders.py:147` (`fetch`/`map`) |
| Grounding block (schema) | `src/legba/data/schemas/analyst.py:522` (`GroundingBlock`) |
| Grounding hook builder | `src/legba/runtime/analyst_deps_builder.py:378` (`_build_grounding_hook`) |
| Grounding resolver | `src/legba/runtime/grounding.py:260` (`SubstrateGroundingResolver`) |
| Grounding preamble | `src/legba/runtime/grounding.py:399` (`build_grounding_preamble`) |
| GROUND phase (inject) | `src/legba/data/analysts/inline_target.py:592` |
| Facts table (0032 cols) | `src/legba/data/migrations/0001_baseline.sql:480` + `0032_facts_decay_columns.sql` |
| Nexuses table | `src/legba/data/migrations/0033_nexuses.sql:30` |
| Seed batches | `src/legba/data/migrations/0034_seed_batches.sql:30` |
