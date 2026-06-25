<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Legba Data Model

A reference for the data *tiers* — what each pipeline stage reads, writes, and
whether it **mutates** a row in place, **appends** a new one, **supersession-
versions** (temporal), or is **ephemeral routing** with no durable row at all.

Companion to `ARCHITECTURE.md` (component/flow) and `ANALYSIS.md` (analyst
behaviour). Grounded in `data/migrations/` + the write paths in
`data/provenance/writes.py`, `runtime/source_actor.py`, and the inline filters.

## The shape, in one breath

```
SOURCE → SIGNAL (enriched inline) → fan-out (routing, not data)
        → SUBSTRATE (facts · entities · nexuses · proposed_edges)
        ⇄ ANALYSIS (analyst_outputs · hypotheses · situations · analyst_traces)
        → OUTPUTS (alert / webhook / STIX / A2A / MCP / NATS / substrate)
```

**Durable stores:** `signals`, `signal_aliases`, `facts`, `entity_profiles`
(+`entity_profile_versions`), `nexuses`, `proposed_edges`, `graph_metrics`,
`analyst_outputs`, `hypotheses`, `situations`, `analyst_traces`,
`analyst_critiques`; the consult audit trail `consult_sessions` + `consult_turns`
(0039); the control-plane `*_descriptors` (+ `descriptor_audit_log`); and the
operational ledgers `budget_ledger`, `action_pack_invocations`, `governor_events`,
`seed_batches`, `source_poll_outcomes` (0046), `output_dead_letter`.

## Per-tier table

| Tier | Table(s) | Produced by | Write semantics |
|---|---|---|---|
| **Signal (canonical pool)** | `signals` (0001) | `source_actor.write_canonical_signal` + `baseline.run_baseline` | **append-only** row (ON CONFLICT DO NOTHING); enriched in-memory pre-write |
| Inline: language/geocode/ner/classify/source_credibility | `signals` columns + `payload` | the `data/filters/` stages | **mutate-in-place** (same row) |
| Inline: slm_entity_resolve / slm_relationship_validate | `signals.payload` verdicts | the SLM filters | **mutate-in-place** (drops bad triples, never the signal) |
| Inline: ingest_dedupe | `signal_aliases` + `signals.canonical_signal_id` | `ingest_dedupe` (after insert) | **append derived-row** + mutate canonical column; never deletes |
| Inline: fact_extractor | `facts` (`source_type='ingestion'`) | `fact_extractor` | **append + supersession-versioned** |
| **Target descriptor** | `target_descriptors` | Registry | **append-only** (versioned, `is_head`) |
| **Fan-out / subscription** | *(none — in-memory + NATS)* | subscription engine + per-target JetStream consumer | **ephemeral-routing** (no per-target row; signal never copied) |
| **TargetActor runtime** | Dapr `actor_state` | TargetActor | **mutate-in-place** FSM; passive subscriber = NOOP on tick |
| **Facts** | `facts` (0001/0032) | `fact_extractor` (ingestion) + `write_fact` (agent/seed) | **supersession-versioned** (open-only unique index; decay mutates open rows) |
| **Entities** | `entity_profiles` (0001/0035) + `entity_profile_versions` | `entity_resolution` | **mutate-in-place** + append-only version history |
| **Nexuses** | `nexuses` (0033) | `relationship_reifier` (`write_nexus`); `proposed_edge_governance` promotion | **supersession-versioned** (closes on polarity/label change; decay mutates open) |
| **Proposed edges** | `proposed_edges` (0001) | `entity_resolution` (co-occurrence) | **mutate-in-place** (status + confidence accrual; no version chain) |
| **Analyst outputs** | `analyst_outputs` (0011) | `write_analyst_output` | **append-only** (kind-routed; validation fail → DLQ) |
| **Hypotheses** | `hypotheses` (0004, ACH) | `competing_hypotheses` (TRACE_ONLY) | **append** rows + **status transitions mutate-in-place** |
| **Situations** | `situations` (0020; 0040/0042 first-class) | `situation_clustering` materializes (atomic upsert on `(situation_signature, analyst_id)`); `thematic_proposal` proposes uncovered hot frames | **temporal-frame** — `valid_from`/`valid_until`/`superseded_by` (open while active/dormant, `valid_until` stamped on close); `target_id` populated. Persistent FRAMES + grounding source + the **events substitute** (no `events` table — events = signals + `get_timeline`; situations = the frames) |
| **Analyst traces** | `analyst_traces` (0013) | `RuntimeReceiptChain.record` after outputs | **append-only**, SHA-256 hash-chained; one row per run (incl. TRACE_ONLY/failure) |
| **Analyst critiques** | `analyst_critiques` | the critic, on CRITIQUE kind | **append-only** (1:1 per critic run) |
| **Op ledgers** | `budget_ledger`/`global_budget_envelope`/`action_pack_invocations` | budget/governor | **mutate-in-place** (upsert/backfill) |
| **Op ledgers** | `governor_events`, `budget_demotion_events`, `seed_batches`, `descriptor_audit_log`, `audit_checkpoints` | governance/audit | **append-only** |
| **Graph metrics** | `graph_metrics` (0033) | `structural_balance` / `graph_mining` via `_graph_metrics_sink.write_graph_metric` | **append-only** — signed-triad balance + centrality/community + proxy-chain sign-products land as queryable rows |
| **Source poll outcomes** | `source_poll_outcomes` (0046) | `source_actor.pull_once` (NON-productive polls only — empty-fetch / error) | **append-only** — provenance for *why* a source went silent (the H5 cadence-watchdog lateral join); productive polls are self-evidencing via their `signals` rows and are NOT logged |
| **Consult audit trail** | `consult_sessions` + `consult_turns` (0039) | the registry consult / deep-consult API (one session header per conversation/task; append-only turns) | session header **mutate-in-place** (title/status); `consult_turns` **append-only** (ReAct steps / tool_calls / cited_refs + optional deep `finding_id`) |
| **Lineage** | `derived_from UUID[]` on substrate tables | write paths stamp at write | appended on dedup/merge |
| **Outputs / emit** | `analyst_outputs` (+ `alert_sink_deliveries`); webhook/STIX/A2A/MCP/NATS sinks | `outputs/*.py` emit | substrate = **append-only**; emit = **ephemeral / side-table** |
| **DLQ** | `output_dead_letter` (0007) | `route_to_output_dead_letter` | append + operator-resolution mutate |

## The three load-bearing questions

### Does the inline pipeline change the signal, or add to it?
**Both, stage-specific.** The `signals` *row* is written once (append-only). The
enrichment stages **mutate that one row in place** (language, geo, ner, classify,
source_credibility, SLM verdicts in `payload`). Only **two** stages write
elsewhere: `ingest_dedupe` appends a `signal_aliases` row + sets
`canonical_signal_id`; `fact_extractor` appends to `facts`. Honest caveat:
`signals` is the **one substrate table that is not strictly append-only** — a
later entity-merge can re-`UPDATE` `canonical_signal_id` / append `derived_from`.

### What does the target / fan-out layer record?
**Almost nothing persistent — it is routing/control, not data.** There is **no
`target_id` on `signals` and no per-target delivery table**. A signal is routed,
never copied per target. What exists: `target_descriptors` (the geo/tags/predicate
*contract*), in-memory subscription wiring + one durable JetStream PULL consumer
per target, a two-stage SQL-WHERE→Starlark match computed fresh at delivery, and
the TargetActor's lifecycle/cursor `actor_state`. A non-discovery target is a
**passive subscriber** — NOOP on tick.

### What does analysis read and write?
**Reads:** a signal slice (default 24h, scope-filtered, ~50 rows) + open `facts`,
`nexuses`, `hypotheses`, peer findings + the grounding preamble. **Writes** via two
separate channels: (a) **`analyst_outputs`** (kind = finding / prediction /
critique / prompt_module_candidate), and/or (b) **substrate side-writes**
(facts / nexuses / hypotheses / entities) written directly by the run method.
The **TRACE_ONLY meta-kinds** (`competing_hypotheses`, `relationship_reifier`,
the deterministic maintenance sub-handlers) write **no `analyst_outputs` row** —
their product is the side-write. **Every run** — output, side-write, TRACE_ONLY,
or failure — leaves **exactly one** hash-chained `analyst_traces` row
(`output_row_refs` empty when nothing was emitted).

## Old → new vocabulary

| Old | Now | Changed |
|---|---|---|
| signals | `signals` | target-*agnostic* (no target_id); enriched inline; deduped via aliases |
| facts | `facts` | **temporal-versioned** (`valid_from`/`valid_until`/`superseded_by`, decay, `source_type`+`seed_batch_id`) |
| entities | `entity_profiles` (+`_versions`) | **canonicalized**, composite key `(name, entity_class)`, version history |
| findings | `analyst_outputs` (kind=`finding`) | generalized to a **multi-kind** table + DLQ + emit channels |
| situations | `situations` | **first-class temporal frames** (0040/0042): clustered bottom-up + a grounding source + the **events substitute** (no events table; events = signals + `get_timeline`, which now includes situation spans); `thematic_proposal` proposes uncovered hot frames for operator promotion to thematic targets |
| hypotheses | `hypotheses` | full **ACH** (≥2 theses, evidence×diagnosticity, ±2 transitions); calibration's **exogenous `resolved_outcome` resolver is built and firing** (subsequent-facts auto-resolver + operator-label path, migration 0038) alongside the live self-consistency tier — see "Known thin / inert legs" |

**New since the old system:** reified **signed/typed nexuses**; **temporal-
versioned facts**; **hash-chained `analyst_traces`**; the **agency ledgers**
(`action_pack_invocations` / `governor_events`); the **grounding seed substrate**
(`seed_batches`); `analyst_critiques`; the budget ledgers; versioned control-plane
descriptors + audit log; the universal `derived_from[]` lineage column.

## Mutate-vs-append cheat-sheet

- **Append-only:** `analyst_outputs`, `analyst_traces`, `analyst_critiques`,
  `signal_aliases`, `entity_profile_versions`, the audit/governor/seed ledgers,
  every `*_descriptors` version, `alert_sink_deliveries`.
- **Supersession-versioned (temporal):** `facts` (value change), `nexuses`
  (polarity/label change) — new open row, old row closed; both decay open rows.
- **Mutate-in-place:** `signals` enrichment + later merge re-update (the one
  non-append substrate table), `entity_profiles`, `proposed_edges`,
  `hypotheses`/`situations` *status*, `budget_ledger`, `action_pack_invocations`,
  `output_dead_letter` resolution.
- **Ephemeral (no durable row):** subscription wiring, per-target JetStream
  consumers, the predicate match, NATS-stream output.

## Known thin / inert legs (honest)

These earlier "horizon" legs have since landed; the honest residual caveats are
narrower now:

- **`situations` — first-class, not "clustering future".** Migrations 0040–0042
  gave `situations` a real `situation_signature` column + a `UNIQUE (signature,
  analyst_id)` upsert key and the temporal frame (`valid_from` / `valid_until` /
  `superseded_by`), repaired the inverted `valid_from` backfill (0041), and
  populated `target_id` (0042). Situations are now the persistent FRAMES that
  serve as a grounding source and the **events substitute** (no `events` table;
  events = signals + `get_timeline`, which includes situation spans);
  `thematic_proposal` proposes uncovered hot frames for operator promotion.
- **`graph_metrics` — products now land.** `structural_balance` and `graph_mining`
  persist through `_graph_metrics_sink.write_graph_metric` (signed-triad balance,
  centrality/community, proxy-chain sign-products), so the signed-graph metrics
  are queryable rows rather than log-only output.
- **ACH `resolved_outcome` calibration — exogenous resolver built and firing,
  plus a live self-consistency tier.** The exogenous resolver (subsequent-facts
  auto-resolver + operator-label path, migration 0038) ABSTAINS on undirected
  theses rather than auto-grading them TRUE, and runs alongside the live
  self-consistency (`status_transition`) tier. Residual caveats: the
  subsequent-facts auto-resolver is a coarse directional heuristic (the
  operator-label path is higher-fidelity), the gradeable directional resolution
  rate is modest, and a budget-exhausted matrix run falls back to the lexical
  per-cell scorer (check `matrix_scorer`). No proven-forecast-accuracy claim is
  made.
