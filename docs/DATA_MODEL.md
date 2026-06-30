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

**Durable stores:** `signals`, `signal_aliases`, `facts` (+ the derived
contested-claims sidecar `fact_contention` / `fact_contention_values`, 0055),
`entity_profiles`
(+`entity_profile_versions`), `nexuses`, `proposed_edges`, `graph_metrics`,
`analyst_outputs`, `hypotheses`, `situations`, `analyst_traces`,
`analyst_critiques`; the **off-chain** journal stores `journal_entries` +
`journal_proposals` (0048; the journal is a *perspective over* the provenance
chain — see "The journal — off-chain by design"); the consult audit trail
`consult_sessions` + `consult_turns` (0039); the control-plane `*_descriptors`
(+ `descriptor_audit_log`); and the operational ledgers `budget_ledger`,
`action_pack_invocations`, `governor_events`, `seed_batches`,
`source_poll_outcomes` (0046), `output_dead_letter`.

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
| **Facts** | `facts` (0001/0032; `source_credibility` 0054; contested-claims markers 0055) | `fact_extractor` (ingestion) + `write_fact` (agent/seed) | **supersession-versioned** (open-only unique index; decay mutates open rows). Under `LEGBA_FACT_CONTENTION` a fuzzy-distinct same-tier value coexists open instead of superseding (#101) |
| **Contention sidecar** (derived) | `fact_contention` + `fact_contention_values` (0055) | `fact_contention_arbiter` (deterministic META, hourly :37, **detect-only** B15) | **derived / recomputable** from open `facts`; arbiter only sets the sidecar + the 3 `facts` markers, never mutates a fact |
| **Entities** | `entity_profiles` (0001/0035) + `entity_profile_versions` | `entity_resolution` | **mutate-in-place** + append-only version history |
| **Nexuses** | `nexuses` (0033) | `relationship_reifier` (`write_nexus`); `proposed_edge_governance` promotion | **supersession-versioned** (closes on polarity/label change; decay mutates open) |
| **Proposed edges** | `proposed_edges` (0001) | `entity_resolution` (co-occurrence) | **mutate-in-place** (status + confidence accrual; no version chain) |
| **Analyst outputs** | `analyst_outputs` (0011) | `write_analyst_output` | **append-only** (kind-routed; validation fail → DLQ) |
| **Hypotheses** | `hypotheses` (0004, ACH) | `competing_hypotheses` (TRACE_ONLY) | **append** rows + **status transitions mutate-in-place** |
| **Situations** | `situations` (0020; 0040/0042 first-class) | `situation_clustering` materializes (atomic upsert on `(situation_signature, analyst_id)`); `thematic_proposal` proposes uncovered hot frames | **temporal-frame** — `valid_from`/`valid_until`/`superseded_by` (open while active/dormant, `valid_until` stamped on close); `target_id` populated. Persistent FRAMES + grounding source + the **events substitute** (no `events` table — events = signals + `get_timeline`; situations = the frames) |
| **Analyst traces** | `analyst_traces` (0013) | `RuntimeReceiptChain.record` after outputs | **append-only**, SHA-256 hash-chained; one row per run (incl. TRACE_ONLY/failure) |
| **Analyst critiques** | `analyst_critiques` | the critic, on CRITIQUE kind | **append-only** (1:1 per critic run) |
| **Journal entries** (off-chain) | `journal_entries` (0048) | the `journal_assessor` META analyst kind via `write_analyst_output` (kind=`journal`) — **NOT** `analyst_outputs` | `entry` rows **append-only**; `consolidation` rows **supersession-versioned** (`valid_from`/`valid_until`/`superseded_by`, `supersede_prior_consolidation` closes the prior open consolidation; partial-unique index = **at most one open consolidation**). **Always-empty `derived_from`**; citations live only in `claims`/`cited_substrate_refs`; `honesty_flags` forced deterministically from substrate metrics. OFF the fact/finding/nexus chain |
| **Journal proposals** (human-gated) | `journal_proposals` (0048) | the journal's `journal_propose` pack (Wave 4) — `proposal_kind` ∈ `self_revision`/`correction`/`change` | **append + status mutate-in-place** (`pending`→`accepted`/`rejected`/`archived`). The review **queue**, NEVER a live table — a human accept runs the per-kind apply worker; nothing the journal proposes touches another analyst or substrate without that accept |
| **Op ledgers** | `budget_ledger`/`global_budget_envelope`/`action_pack_invocations` | budget/governor | **mutate-in-place** (upsert/backfill) |
| **Op ledgers** | `governor_events`, `budget_demotion_events`, `seed_batches`, `descriptor_audit_log`, `audit_checkpoints` | governance/audit | **append-only** |
| **Graph metrics** | `graph_metrics` (0033) | `structural_balance` / `graph_mining` via `_graph_metrics_sink.write_graph_metric` | **append-only** — signed-triad balance + centrality/community + proxy-chain sign-products land as queryable rows |
| **Source poll outcomes** | `source_poll_outcomes` (0046) | `source_actor.pull_once` (NON-productive polls only — empty-fetch / error) | **append-only** — provenance for *why* a source went silent (the H5 cadence-watchdog lateral join); productive polls are self-evidencing via their `signals` rows and are NOT logged |
| **Consult audit trail** | `consult_sessions` + `consult_turns` (0039) | the registry consult / deep-consult API (one session header per conversation/task; append-only turns) | session header **mutate-in-place** (title/status); `consult_turns` **append-only** (ReAct steps / tool_calls / cited_refs + optional deep `finding_id`) |
| **Lineage** | `derived_from UUID[]` on substrate tables | write paths stamp at write | appended on dedup/merge. **`journal_entries` is the deliberate exception** — its `derived_from` is always empty and the table is absent from the lineage catalog (`lineage_api._SUBSTRATE_TABLES`), so a downstream lineage walk can never surface a journal node |
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

## The contested-claims fact model

Task #101 Holes-B (migrations 0054 + 0055, head **0055**) turns "two credible
sources disagree on one `(subject, predicate)` value" from an invisible race into
a **first-class, derived, recomputable** state — without ever letting a machine
overwrite the disputed facts. The substrate change is small and almost entirely
*additive*; the behaviour is gated OFF by default.

**Per-fact credibility — `facts.source_credibility real` (0054).** A 0..1 trust
score of the most credible source backing this fact, propagated down from
`signals.source_credibility` so the arbiter has a per-fact credibility term to
weight competing values by. Resolved at write time as the **MAX over the backing
signals'** `source_credibility`, else the **source-tier nominal** (seed/curated
`0.9`, agent/ingestion `0.5`). `NULL` = *unknown* (a pre-0054 row, or a write with
no resolvable credibility) — the arbiter treats `NULL` as unknown, **never as
zero**. Two earlier Holes-A write-path fixes ship with this wave: the ingestion
fact-writer now passes `incoming_source_type` (so the tier guard that protects a
curated fact from a machine-extracted one is no longer bypassed), and a same-value
merge aggregates confidence by **noisy-OR** (was `GREATEST`).

**Three thin markers on `facts` (0055).** So a reader can tell a genuine update
apart from a live dispute, with no JSON blob and no per-value columns:

| Column | Type | Meaning |
|---|---|---|
| `contested` | `boolean NOT NULL DEFAULT false` | this open fact is one of ≥2 coexisting values in a live dispute |
| `contention_id` | `uuid` | the `fact_contention` group this row belongs to (`NULL` = uncontested) |
| `surfaced_winner` | `boolean NOT NULL DEFAULT false` | the arbiter currently surfaces *this* row as the group's winner |

A partial index `idx_facts_contested ON facts (contention_id) WHERE contested`
keeps the arbiter's marker-clearing sweep + downstream surfacing joins cheap
regardless of total `facts` size.

**Two sidecar tables (0055) — the derived group view.** Per-value support is
multi-valued (N distinct values, each with its own source set / credibility sum /
counts) and **recomputed on every arbiter pass**, so it lives in a sidecar rather
than on the single-row `facts` table:

- **`fact_contention`** — one group per `(subject_key, predicate_key)`,
  `UNIQUE (subject_key, predicate_key)`. Lifecycle `status ∈ contested → surfaced
  → collapsed` (collapses when a group falls back to one value). Carries the
  arbiter's current winner (`surfaced_value` / `surfaced_fact_id`, both `NULL` on
  abstain), the distinct non-junk `value_count`, the operator-reportable
  `junk_count`, and `arbiter_version` / `resolved_at`.
- **`fact_contention_values`** — one row per distinct **non-junk value cluster**
  in a group (`UNIQUE (contention_id, value_key)`, FK `ON DELETE CASCADE`),
  carrying the aggregated support and the deterministic **Q·C·R·F** `arbiter_score`
  (quorum × credibility-share × recency-half-life × confidence). Key support
  columns: `distinct_source_count` (DISTINCT lineage, **not** row count — defeats a
  chatty source), `source_credibility_sum` (SUM of non-`NULL`
  `facts.source_credibility`), `confidence_max` / `confidence_mean`,
  `supporting_fact_ids`, `representative_fact_id`, `latest_asserted_at`, and
  `surfaced_winner`. A junk cluster is **recorded** (`is_junk=true` + a
  `junk_reason` naming which `fact_extractor` gate fired, never silently dropped)
  and excluded from the dispute count.

Both sidecar tables are **fully derived** — they can be dropped and rebuilt from
the open `facts` rows at any time. That recomputability is the test that proves
they are a *view over* the chain, not primary data.

**DETECT-ONLY (invariant B15).** The hourly (:37) `deterministic`-kind
`fact_contention_arbiter` META analyst that populates these tables **NEVER**
mutates a `facts` value / `valid_until` / `superseded_by` / `confidence` — it only
writes the sidecar rows + the three marker columns (and is itself TRACE_ONLY: one
`analyst_traces` row, no `analyst_outputs`). It scans open facts, fuzzy-clusters
values (canonicalize-entity + normalized-Levenshtein, distance threshold `0.12` —
so *Russia / Russian* and *Kyiv / Kiev* merge but *North / South Korea* stay
split), junk-gates through the existing `fact_extractor` gates, scores each cluster
Q·C·R·F, and surfaces **at most one** winner per `(subject, predicate)` or
**abstains** on a near-tie (`MIN_SURFACE_SCORE 0.15`, `DOMINANCE_RATIO 1.25`).

**Two differing OPEN values for one `(subject, predicate)` is now an intended
first-class state — under a flag.** The open-triple unique index keys on
`lower(value)`, so distinct values have always been *able* to coexist; what
changed is the write path. Under `LEGBA_FACT_CONTENTION` (default OFF), inside
`provenance/writes.py` `supersede_prior_facts`, a same-tier incoming value that is
fuzzy-*distinct* from an open prior **no longer closes** that prior — the two rows
coexist open so the arbiter can group them (both fact producers route through
`supersede_prior_facts`). With the flag OFF, the prior single-winner-by-recency
behaviour is unchanged. An optional vLLM tie-break (`LEGBA_FACT_CONTENTION_LLM_
TIEBREAK`, default OFF) runs **only** on a near-tie abstain, on the self-hosted
plane; it is unrelated to the substrate shape and leaves these tables unchanged
except for which cluster (if any) is marked the winner.

> **Flag posture (honest).** Both `LEGBA_FACT_CONTENTION` and
> `LEGBA_FACT_CONTENTION_LLM_TIEBREAK` default **OFF** in code *and* in
> `docker-compose` (`${VAR:-0}`); they are enabled (`=1`) only on this instance via
> the gitignored `.env`. The detect-only arbiter, Wave-4 coexistence, and the
> read-side surfacing are proven live; the vLLM tie-break is proven **consulted**
> live (it abstained on symmetric evidence — correct, provenance-first), but a
> successful LLM *pick* is **unobserved live** so far. Plan + decisions:
> `planning/HOLES_B_CONTESTED_CLAIMS_SCOPED_PLAN.md`.

## The journal — off-chain by design

The 11th `OutputKind` — `journal` — is the one row family that is **a
perspective *over* the provenance chain, never a *member* of it.** It is
Legba's first-person reflective voice: the one analyst (`journal_assessor`)
pointed at the whole organism — its own self, state, and flow — narrating a
coherent point of view across the rest of the system rather than cutting a
single slice. *Poetry without evidence is noise. Evidence without perspective
is just a log file.* Because it is an **extension** analyst kind
(`register_analyst_kind` + the vocabulary family, not a member of the closed
built-in `AnalystKind` enum), the count of built-in analyst kinds is unchanged
— the journal sits on top as the `journal_assessor` extension kind.

**It is NOT in the lineage.** A journal row lands in the dedicated
`journal_entries` table (migration 0048, schema_uri
`iglu:legba/journal/jsonschema/1-0-0`, `JournalPayload`) — **NOT**
`analyst_outputs`. The off-chain invariant is enforced two ways: (1) at the
**grant layer** — the analyst is granted only two **non-write-fact** packs
(`journal_read`, 14 read tools incl. 9 self-instruments; `journal_propose`),
so it has no path to a fact/finding/nexus write; and (2) at the **chain layer**
— its `derived_from` is **always empty** and the table is deliberately absent
from the lineage catalog (`lineage_api._SUBSTRATE_TABLES`), so the
`signals → entities/facts → relations/nexuses → situations → assessments`
derived_from walk can **never** surface a journal node. A gating test
(`test_journal_off_chain.py`) holds the never-writes-a-fact line. Citations
are carried *up-only* in `claims` / `cited_substrate_refs`, not as lineage
edges; `honesty_flags` are forced deterministically from substrate metrics, and
every row is part of the same hash-chained receipt machinery as the rest of the
analysts.

**Two tiers, one kind.** `entry_kind` discriminates `entry` (append-only, the
12h field-notes tier) from `consolidation` (the daily distillation tier).
A consolidation **supersession-versions** exactly like `facts`/`nexuses`:
`supersede_prior_consolidation` closes the prior open row
(`valid_until`/`superseded_by` stamped at the new id) **before** inserting the
new one, and a partial-unique index guarantees **at most one open
consolidation** (race/replay-safe). The journal's only un-gated effect is its
own continuity — it reads its own last entry + current open consolidation into
its next run.

**Everything outward is human-gated.** The journal writes **only** its own
entries + consolidations directly. A `correction`, a `change`, or a
`self_revision` (including a proposed edit to its own instructions; protected
sections auto-reject) goes to the `journal_proposals` queue — **never a live
table**. A human accepts or rejects; only the accept path runs the idempotent
per-kind apply worker. (Honest caveat: the `correction` and `self_revision`
apply paths are tested end-to-end; the `change` apply path is import-verified
but not yet exercised against a live registry.) Reads are served by
`GET /api/v1/journal`; the review surface is `GET /api/v1/journal_proposals`
plus the `/journal_proposals/{id}/accept` and `/reject` endpoints.

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
  `journal_entries` `entry` rows, `signal_aliases`, `entity_profile_versions`,
  the audit/governor/seed ledgers, every `*_descriptors` version,
  `alert_sink_deliveries`.
- **Supersession-versioned (temporal):** `facts` (value change), `nexuses`
  (polarity/label change), `journal_entries` `consolidation` rows
  (a newer consolidation supersedes the prior open one) — new open row, old row
  closed; `facts`/`nexuses` also decay open rows.
  - **Fact supersession is single-winner-by-recency *within a source tier*** (task
    #101 Holes-A): a machine-extracted `ingestion`/`agent` fact does **not** close an
    open human-curated `seed`/`curated` fact (`seed == curated > ingestion == agent`);
    same-tier recency still wins. Agreement on a `(subject, predicate, value)`
    aggregates confidence via a bounded noisy-OR (cap 0.99), not MAX.
  - **Contested values now coexist OPEN under a flag (task #101 Holes-B).** Under
    `LEGBA_FACT_CONTENTION` (default OFF), a same-tier incoming value that is
    fuzzy-*distinct* from an open prior does **not** close that prior — the two
    open rows coexist as a first-class dispute, surfaced through the
    `fact_contention` sidecar by the **detect-only** arbiter. See "The contested-
    claims fact model" below.
- **Mutate-in-place:** `signals` enrichment + later merge re-update (the one
  non-append substrate table), `entity_profiles`, `proposed_edges`,
  `hypotheses`/`situations` *status*, `journal_proposals` *status*
  (`pending`→`accepted`/`rejected`), `budget_ledger`, `action_pack_invocations`,
  `output_dead_letter` resolution.
- **Ephemeral (no durable row):** subscription wiring, per-target JetStream
  consumers, the predicate match, NATS-stream output.
- **Derived / recomputable (rebuildable from open `facts`):** the contested-claims
  sidecar `fact_contention` / `fact_contention_values` — the detect-only arbiter
  upserts them each pass and also sets the `contested` / `contention_id` /
  `surfaced_winner` markers on `facts` in place, but never mutates a fact value
  (invariant B15).

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
