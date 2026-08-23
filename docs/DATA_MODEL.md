<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Legba Data Model

A reference for the data *tiers* — what each pipeline stage reads, writes, and
whether it **mutates** a row in place, **appends** a new one, **supersession-
versions** (temporal), or is **ephemeral routing** with no durable row at all.

Companion to `ARCHITECTURE.md` (component/flow) and `ANALYSIS.md` (analyst
behaviour). Grounded in `data/migrations/` + the write paths in
`data/provenance/writes.py`, `runtime/source_actor.py`, and the inline filters.
New here? Start with the [README](../README.md) and the [Tour](TOUR.md).

**Contents:**
[The shape, in one breath](#the-shape-in-one-breath) ·
[Per-tier table](#per-tier-table) ·
[The three key questions](#the-three-key-questions) ·
[The mandatory faithfulness verify](#the-mandatory-faithfulness-verify) ·
[The scorecard](#the-scorecard--the-12th-outputkind) ·
[The contested-claims fact model](#the-contested-claims-fact-model) ·
[The 2026-07-28 wave (0091–0105)](#the-2026-07-28-wave--new-tables-migrations-00910105) ·
[The follow-on wave (0106–0115)](#the-follow-on-wave--new-tables-migrations-01060115) ·
[The journal](#the-journal--off-chain-by-design) ·
[Old → new vocabulary](#old--new-vocabulary) ·
[Mutate-vs-append cheat-sheet](#mutate-vs-append-cheat-sheet) ·
[Known thin / inert legs](#known-thin--inert-legs-honest)

## The shape, in one breath

```
SOURCE → SIGNAL (enriched inline) → fan-out (routing, not data)
        → SUBSTRATE (facts · entities · nexuses · proposed_edges)
        ⇄ ANALYSIS (analyst_outputs · analyst_critiques · hypotheses
                    · situations · analyst_traces · acute_forecasts)
        → OUTPUTS (alert / webhook / STIX / A2A / MCP / NATS / substrate)
```

**Durable stores:** `signals`, `signal_aliases`, `facts` (+ the derived
contested-claims sidecar `fact_contention` / `fact_contention_values`, 0055),
`entity_profiles`
(+`entity_profile_versions`), `nexuses`, `proposed_edges`, `graph_metrics`,
`analyst_outputs` (kind-routed across 12 `OutputKind`s, incl. the 12th
deterministic `scorecard` row family), `hypotheses`, `situations`,
`analyst_traces`, `analyst_critiques` (both the eval-loop critic's verdicts
**and** the mandatory faithfulness-verify verdicts); the measurement leg
`acute_forecasts` (0047, the isolated forecast pilot) + `unit_reference_labels`
(0057, the per-unit gold/reference set); the **off-chain** journal stores
`journal_entries` +
`journal_proposals` (0048; the journal is a *perspective over* the provenance
chain — see "The journal — off-chain by design"); the consult audit trail
`consult_sessions` + `consult_turns` (0039); the control-plane `*_descriptors`
(+ `descriptor_audit_log`); and the operational ledgers `budget_ledger`,
`action_pack_invocations`, `governor_events`, `seed_batches`,
`source_poll_outcomes` (0046, + `newest_entry_ts` 0092, + the `success`
outcome 0114), `output_dead_letter`.

The 2026-07-28 wave (migrations 0091–0105, detailed in
[its own section below](#the-2026-07-28-wave--new-tables-migrations-00910105))
adds: the **alerting** stores `alert_trigger_watermarks` (0091) + `watchlist`
(0105) beside the existing `alert_sink_deliveries` ledger; the **evaluation**
stores `band_calibration_claims` (0093) + `correctness_labels` /
`goldset_week_samples` (0096); the **source-assurance** stores `source_ratings`
/ `source_dossiers` (0094) + `source_track_records` (0099); the **derived
readout sidecars** `fact_decay_states` (0098), `narratives` +
`narrative_echo_edges` (0102), `desk_baselines` (0103), and the contention
tie-break cache `fact_contention_tiebreak` (0097); and the **evidence archive**
sidecar `evidence_archive` (0104 — the bytes live content-addressed on the
`legba_archive` filesystem volume, not in Postgres).

The **follow-on wave** (migrations `0106`–`0115`, detailed in
[its own section below](#the-follow-on-wave--new-tables-migrations-01060115))
adds the **lineage-forward** stores `output_consumption` (0106) +
`review_flags` / `bearing_edges` (0107), the **one-janitor** config table
`retention_policies` (0109), the **retrieval-origin** axis on `signals` /
`evidence_archive` (0112), the operator-reviewable
`collection_requirements` backlog (0113), and a third `source_poll_outcomes`
outcome — `success` (0114) — closing the error-streak latch guard's blind
spot to a recovered source.

## Per-tier table

| Tier | Table(s) | Produced by | Write semantics |
|---|---|---|---|
| **Signal (canonical pool)** | `signals` (0001) | `source_actor.write_canonical_signal` + `baseline.run_baseline` | **append-only** row (ON CONFLICT DO NOTHING); enriched in-memory pre-write. Since 2026-07 an **intra-source exact-hash re-serve** (same `(source_id, content_hash, tenant)` within 168h) **bumps the existing row's `fetched_at` and skips the insert** — `LEGBA_INTRASOURCE_DEDUP`, default ON, provably lossless for an exact hash; a later `evidence_archiver` pass may also stamp `object_ref = cas:sha256/<hex>` + `retention_class='evidence_hold'` on a verified-cited row |
| Inline: language/geocode/ner/classify/source_credibility | `signals` columns + `payload` | the `data/filters/` stages | **mutate-in-place** (same row) |
| Inline: slm_entity_resolve / slm_relationship_validate | `signals.payload` verdicts | the SLM filters | **mutate-in-place** (drops bad triples, never the signal) |
| Inline: ingest_dedupe | `signal_aliases` + `signals.canonical_signal_id` | `ingest_dedupe` (after insert) | **append derived-row** + mutate canonical column; never deletes |
| Inline: fact_extractor | `facts` (`source_type='ingestion'`) | `fact_extractor` | **append + supersession-versioned** |
| **Target descriptor** | `target_descriptors` | Registry | **append-only** (versioned, `is_head`) |
| **Fan-out / subscription** | *(none — in-memory + NATS)* | subscription engine + per-target JetStream consumer | **ephemeral-routing** (no per-target row; signal never copied) |
| **TargetActor runtime** | Dapr `actor_state` | TargetActor | **mutate-in-place** FSM; passive subscriber = NOOP on tick |
| **Facts** | `facts` (0001/0032; `source_credibility` 0054; contested-claims markers 0055) | `fact_extractor` (ingestion) + `write_fact` (agent/seed) | **supersession-versioned** (open-only unique index; decay mutates open rows). A write-path **relation-direction / demonym / relative-temporal junk gate** now rejects inversions (*NATO member of Turkiye*) and non-entity subjects, with adjective-nationality VALUE normalization scoped to geographic/relational predicates only (*Kyiv capital of Russian* → Russia; *speaks Russian* untouched); 0077 closed the historical strays. Under `LEGBA_FACT_CONTENTION` a fuzzy-distinct same-tier value coexists open instead of superseding (#101) |
| **Contention sidecar** (derived) | `fact_contention` + `fact_contention_values` (0055) | `fact_contention_arbiter` (deterministic META, hourly :37, **detect-only** B15) | **derived / recomputable** from open `facts`; arbiter only sets the sidecar + the 3 `facts` markers, never mutates a fact |
| **Entities** | `entity_profiles` (0001/0035) + `entity_profile_versions` | `entity_resolution` | **mutate-in-place** + append-only version history. Pre-lookup is now **alias/article-aware + class-guarded** with a junk gate (numeric/quantity/possessive surfaces rejected; a fallback-elected keeper is never class-mutated) so post-merge re-fragmentation (*the Strait of Hormuz* vs *Strait of Hormuz*) no longer forks; 0076 re-folded the strays |
| **Nexuses** | `nexuses` (0033) | `relationship_reifier` (`write_nexus`); `proposed_edge_governance` promotion | **supersession-versioned** (closes on polarity/label change; decay mutates open). Write-path **junk/vague-endpoint** gate (relative-time + vague-bloc/adjective singletons) at both producers, a same-referent **self-edge** gate, and demonym/plural **dyad canonicalization** (so *Russia\|Russian × Ukraine\|Ukrainian* stops inflating dyad counts); 0078 closed the historical strays |
| **Proposed edges** | `proposed_edges` (0001) | `entity_resolution` (co-occurrence) | **mutate-in-place** (status + confidence accrual; no version chain) |
| **Analyst outputs** | `analyst_outputs` (0011) | `write_analyst_output` | **append-only** (kind-routed across 12 `OutputKind`s incl. `finding` / `meta_finding` / `scorecard`; validation fail → DLQ) |
| **Scorecard** | `analyst_outputs` (kind=`scorecard`) | `scorecard_producer` (deterministic META, daily, pure SQL) | **append-only** — ONE banded row per active **g20/watch-tagged desk** (19 G20 + 13 watch = 32) over already-verified claims in a **14-day band window**; every band names its verified-claim basis id; a dimension with no qualifying verified claim reads `insufficient-evidence` (never fabricated) |
| **Acute forecasts** (pilot) | `acute_forecasts` (0047) | `forecast_scoreboard` (deterministic META, weekly) | **append** (idempotent weekly issue) + **resolution mutate-in-place** (graded EXOGENOUSLY when the forward window closes — `resolved_by='forecast_acute_exogenous'`; a pre-clamp-degenerate window is VOIDED, `resolved_by='voided:pre_clamp_degenerate'`, kept and counted but never scored). ISOLATED from the findings feed; surfaced only on the calibration scoreboard; reports NO proven skill today (honest — a degenerate p-vector abstains at issue, zero rows) |
| **Unit gold set** | `unit_reference_labels` (0057) + `correctness_labels` / `goldset_week_samples` (0096) | the labels API (`registry/labels_api.py`) + the weekly gold-set worksheet | **append-only.** Since 2026-08-03 the correctness axis is fed by the weekly gold-set verdicts (`correctness_labels`, via the shared `correctness_axis` module — first labeled cohort n=8); the deterministic `unit_reference_labels` reference leg stays tiny (n≈1 → reported insufficient-sample, honestly unmeasured) |
| **Hypotheses** | `hypotheses` (0004, ACH) | `competing_hypotheses` (TRACE_ONLY) | **append** rows + **status transitions mutate-in-place** |
| **Situations** | `situations` (0020; 0040/0042 first-class) | `situation_clustering` materializes (atomic upsert on `(situation_signature, analyst_id)`); `thematic_proposal` proposes uncovered hot frames | **temporal-frame** — `valid_from`/`valid_until`/`superseded_by` (open while active/dormant, `valid_until` stamped on close); `target_id` populated. Persistent FRAMES + grounding source + the **events substitute** (no `events` table — events = signals + `get_timeline`; situations = the frames) |
| **Situation events** (trajectory ledger) | `situation_events` (0184) | `situation_tracker` (the ONE ledger writer) | **append-only, schema-enforced** (DELETE and UPDATE both barred by trigger) — one row per movement: `delta ∈ escalates \| de_escalates \| broadens \| unchanged_checkpoint` with a `state_from`/`state_to` trajectory axis (owned by the tracker, deliberately not a second writer on `situations.status`); a delta claim without new evidence is unrepresentable (`CHECK (delta='unchanged_checkpoint' OR derived_from <> '{}')`), and `occurred_at` is evidence time, not run time. Activated live 2026-08-09 |
| **Analyst traces** | `analyst_traces` (0013) | `RuntimeReceiptChain.record` after outputs | **append-only**, SHA-256 hash-chained (**chain-consistent, single-node** receipts); one row per run (incl. TRACE_ONLY/failure); per-analyst run timing is surfaced read-only by `GET /api/v1/v3/eval/analyst_runtime` (run count, avg/max wall-clock seconds, last run, non-success) |
| **Analyst critiques** | `analyst_critiques` | the eval-loop critic (CRITIQUE kind) **+** the mandatory faithfulness-verify pass (`title LIKE 'Faithfulness verify%'`) | **append-only** — one per critic run; the verify verdict is folded at read time into `effective_confidence = min(confidence, faithfulness_score)` (a low score demotes, never hard-deletes) |
| **Journal entries** (off-chain) | `journal_entries` (0048) | the `journal_assessor` META analyst kind via `write_analyst_output` (kind=`journal`) — **NOT** `analyst_outputs` | `entry` rows **append-only**; `consolidation` rows **supersession-versioned** (`valid_from`/`valid_until`/`superseded_by`, `supersede_prior_consolidation` closes the prior open consolidation; partial-unique index = **at most one open consolidation**). **Always-empty `derived_from`**; citations live only in `claims`/`cited_substrate_refs`; `honesty_flags` forced deterministically from substrate metrics. OFF the fact/finding/nexus chain |
| **Journal proposals** (human-gated) | `journal_proposals` (0048) | the journal's `journal_propose` pack (Wave 4) — `proposal_kind` ∈ `self_revision`/`correction`/`change` | **append + status mutate-in-place** (`pending`→`accepted`/`rejected`/`archived`). The review **queue**, NEVER a live table. The journal may enqueue proposals, but **routing an accepted one back** into any analyst/substrate is a **FUTURE item, not yet wired live** — nothing the journal proposes touches another analyst or substrate today |
| **Op ledgers** | `budget_ledger`/`global_budget_envelope`/`action_pack_invocations` | budget/governor | **mutate-in-place** (upsert/backfill) |
| **Op ledgers** | `governor_events`, `budget_demotion_events`, `seed_batches`, `descriptor_audit_log`, `audit_checkpoints` | governance/audit | **append-only** |
| **Graph metrics** | `graph_metrics` (0033) | `structural_balance` / `graph_mining` via `_graph_metrics_sink.write_graph_metric` | **append-only** — signed-triad balance + centrality/community + proxy-chain sign-products land as queryable rows |
| **Source poll outcomes** | `source_poll_outcomes` (0046, `success` outcome 0114) | `source_actor.pull_once` — **one row per poll**: `success` (>=1 signal written, or an intra-source duplicate collapsed; `signals_written` carries the count) / `empty` (clean HTTP-200, nothing new) / `error` | **append-only** — provenance for *why* a source went silent (the H5 cadence-watchdog lateral join) **and** for the fact that it recovered. 0046 logged only NON-productive polls, on the premise that a productive one is self-evidencing via its `signals` rows; that holds for a reader inspecting one poll and fails for every reader that walks a RUN, because an absence cannot break a run — a repaired source kept presenting its historical `error` rows as the leading run and `entity_gc` op 4 re-paused it mid-ingest |
| **Consult audit trail** | `consult_sessions` + `consult_turns` (0039) | the registry consult / deep-consult API (one session header per conversation/task; append-only turns) | session header **mutate-in-place** (title/status); `consult_turns` **append-only** (ReAct steps / tool_calls / cited_refs + optional deep `finding_id`) |
| **Lineage** | `derived_from UUID[]` on substrate tables | write paths stamp at write | appended on dedup/merge. **`journal_entries` is the deliberate exception** — its `derived_from` is always empty and the table is absent from the lineage catalog (`lineage_api._SUBSTRATE_TABLES`), so a downstream lineage walk can never surface a journal node |
| **Forward lineage** | `output_consumption` (0106) | stamped at the consumption point (the composition's basis/periphery split; the journal's rendered slice) and materialized on the same connection as the output write | **append** — the inverse index of `derived_from`: *what now rests on this row*. Distinguishes `composition_basis` (load-bearing) from `composition_periphery` (hedged context only), so "who would be affected if this were wrong" is answerable without re-deriving it |
| **Review flags** | `review_flags` (0107) | `claim_watch` | **append + close-by-supersession** — one open flag per (product, foundation) pair; a BEFORE DELETE trigger makes deletion a database error. **Nothing in-tree closes a flag today** (`SEAMS.md` #49) |
| **Bearing edges** | `bearing_edges` (0107) | `claim_watch` (`signal → hypothesis`) + the corpus researcher's answer-link (`finding → hypothesis`) | **append-only** dated typed pointers (`ON CONFLICT DO NOTHING`). A bearing edge is *not* lineage: it says "this later thing bears on that earlier question", and it never mutates the question it points at |
| **Collection requirements** | `collection_requirements` (0113) | `collection_gap` | **append (idempotent on `natural_key`) + disposition mutate-in-place** — content columns are write-once; the route may only move `status`/`reviewed_by`/`reviewed_at`/`disposition_note` |
| **Retention config** | `retention_policies` (0109) | operator via `/v3/retention-policies` PATCH (or SQL) | **mutate-in-place config**, read by the shared sweep engine. Both seeded policies ship `ttl_days = 0` = **sweep disabled**. The route may only move `ttl_days`/`keep_classes`/`batch_size`/`enabled`/`description` — `policy_name`/`table_name`/`env_fallback_var` are the code-side pairing to a Python adapter and are never writable through it |
| **Corpus tombstones** | `corpus_tombstones` (0175) | every `signals`-deletion site, in the SAME transaction as its DELETE (`_retention_sweep._purge_signals`, `collapse_intrasource_dupes`, `seed_corpus_orphan_tombstones`) | **append + drain-stamp**. The OpenSearch corpus's delete QUEUE: `doc_id` IS the deleted `signals.id` IS the OpenSearch `_id`, so no mapping is needed. Drained by the `corpus_retention` sweep, which RE-VERIFIES the row is really gone before deleting (a stale tombstone can never destroy a live doc). Rows are never removed — `purged_at` keeps every dropped id queryable, which is the audit trail the platform lacked when 41.5% of the corpus silently orphaned |
| **Outputs / emit** | `analyst_outputs` (+ `alert_sink_deliveries`); webhook/STIX/A2A/MCP/NATS sinks | `outputs/*.py` emit | substrate = **append-only**; emit = **ephemeral / side-table** |
| **DLQ** | `output_dead_letter` (0007) | `route_to_output_dead_letter` | append + operator-resolution mutate |

## The three key questions

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

A *target* here is a **scoped subject / desk** — a named scope-frame a set of
analysts work — **not** a surveilled entity. The trusted spine covers **32
desks** selected by a coverage tag: the 19 G20 country desks (tag `g20`) plus a
13-desk high-consequence **watch** tier — Israel, Iran, Ukraine, Taiwan, North
Korea, Pakistan, and the escalation-risk band Sudan, Mali, Burkina Faso, Niger,
DR Congo, Myanmar, Haiti (descriptor ids `country_watch_<iso2>`, tag `watch`). The seven
broad units + `country_composition` subscribe on `has_tag("g20") or has_tag("watch")`
(the eighth unit, `proliferation_watch`, subscribes narrower on
`has_tag("nuclear_watch")` instead)
and the scorecard enumerates any active desk tagged either — so adding a country
is **register-a-target, no code**. (The composition tower adds the 5 `region_*`
region-frame targets and the thematic `escalation_composition`.)

### What does analysis read and write?
**Reads depend on the analyst's altitude.** A first-order reasoning analyst —
the nine bounded reasoning UNITS — seven broad ones (`leadership_transition`, `energy_security`,
`escalation`, `narrative_coordination`, `internal_stability`, `military_posture`,
`economic_coercion`), fanned out per g20/watch desk, plus `proliferation_watch`
(narrow: tag-scoped to the ~8 nuclear-relevant desks, not the full g20/watch
roster) and `disruption_status` (tag-scoped off the country plane entirely, to
the thematic `supply_chain` lane/flow desks) — and the
generic `inline_target` — reads a scope-filtered signal slice (24h default; the
units widen it to a **72h** raw-signal window) + open `facts` / `nexuses` /
`hypotheses` + a Tier-1 grounding preamble of **accumulated** substrate
facts/nexuses/situations (e.g. *"US head of government Trump since 2025-01-20;
US–Iran active conflict since 2026-02-28; NATO member since 1949"*) — so a unit
integrates over time rather than only over today's signals. The `_orient` packer
admits recency-ordered signals under the **input-token budget** (default 32000,
`LEGBA_LLM_INPUT_TOKEN_BUDGET`); `_MAX_INPUT_SIGNALS = 200` is only a hard
backstop, not a fixed "newest N" trim. A **composition** analyst
(`meta_findings_synthesizer` — `country_composition` per g20/watch desk,
`world_assessor` globally) reads **no raw signals**: it reads OTHER analysts'
verified findings (only faithfulness-verify-passed sub-claims, INNER-JOINed on
the critique). Under `LEGBA_COMPOSITION_TIERED_EVIDENCE` (**default OFF in
code**; enabled on this instance) the composition read splits **two tiers**: a
verified **basis** (effective confidence ≥ the 0.50 floor) plus an
explicitly-labeled, capped (≤ 8) **periphery** of below-floor / unverified
findings admissible *only* as hedged context — unhedged use of a
periphery-only citation is a **counted** soft verify failure, conflicts with
the basis are surfaced as "tensions worth watching", and each composition
records an additive `data.evidence_tiers` envelope ("built on N verified + M
weak"; periphery ids join `derived_from`). See `ANALYSIS.md` §3.11. The deterministic `scorecard_producer` reads the already-verified
claims for banding. **Writes** via two separate channels: (a) **`analyst_outputs`**
(kind = finding / prediction / critique / prompt_module_candidate / **scorecard**),
and/or (b) **substrate side-writes** (facts / nexuses / hypotheses / entities)
written directly by the run method. After a cited FINDING (or a composition)
lands, a **mandatory faithfulness-verify pass** persists its verdict as an
`analyst_critiques` row; `effective_confidence = min(confidence,
faithfulness_score)` is folded at read time (see "The mandatory faithfulness
verify"). The **TRACE_ONLY meta-kinds** (`competing_hypotheses`,
`relationship_reifier`, the deterministic maintenance sub-handlers) write **no
`analyst_outputs` row** — their product is the side-write. **Every run** — output,
side-write, TRACE_ONLY, or failure — leaves **exactly one** hash-chained
`analyst_traces` row (`output_row_refs` empty when nothing was emitted).

## The mandatory faithfulness verify

Every cited FINDING (and every `country_composition` / `world_assessor`
composition) draws a **mandatory faithfulness-verify pass** before it is trusted
downstream. The pass measures **groundedness, not truth** — does each cited
clause actually follow from the signal (or sub-claim) it cites? — and scores the
finding in `[0,1]`. It is the union of two checks: an **always-on deterministic
citation-presence floor** and a **flag-gated LLM judge** resolved through its
own repointable route (`LEGBA_JUDGE_STACK_REF` env > `method.llm.judge` >
`.verify` > `.primary` — descriptor default same-model, the reference
deployment cross-family on a hosted Gemma judge; gated by
`LEGBA_VERIFY_LLM_JUDGE`, soft-failing to the floor — stamped
`judge_status='deterministic'`, published PROVISIONAL under a ceiling — if the
component is unresolved). Every critique row stamps `judge_llm_ref` and a
`judge_pipeline_version` (`2026-08-10/1` today) so verdict populations from
different judges or rule revisions never pool. The verdict is **persisted as an `analyst_critiques` row**, and at
read time the finding↔critique gate folds `effective_confidence =
min(confidence, faithfulness_score)`. A low score **demotes** a finding into a
visible low-confidence tier and excludes it from composition/scorecard — it is
**never hard-deleted**. A planted fabrication is flagged unsupported. This is a
best-effort tail on the run: a verify failure leaves the finding durable and
un-demoted (no regression), and TRACE_ONLY runs have no row to grade.

**Judge provenance + the per-claim ledger (2026-07).** Every faithfulness
critique now stamps **`judge_llm_ref`** — *which model judged this* — both
top-level and inside `data.verification` (empty = the deterministic floor
alone); classifies every failing span **hard vs. soft** (`fail_class`:
`unresolved_citation` / `judge_contradicted` / `stale_leader` /
`stale_leader_vs_facts` / `cross_target_leak` are **hard** — the
entity-scramble class; `no_citation` / `judge_unsupported` /
`hedge_laundering` / `double_counted` / `indicator_uncited_triggered` /
`unhedged_periphery_citation` / `unscoped_absence_claim` are **soft** — the
unsupported-inference class);
and persists a full per-claim **`claim_verdicts` ledger *including supported
claims*** in `data.verification` (capped, with an honest truncation flag).
These are labels-and-persistence only — none feeds the score. The judge LLM
itself resolves through an opt-in route ladder with a dormant
independence-prompt profile; see `ANALYSIS.md` §6.2 for the behaviour.

Honest caveat: faithfulness is a per-unit number, not a platform-wide boast.
Some units score genuinely low, and the read surfaces that rather than hiding it
(see the scorecard note below).

## The scorecard — the 12th OutputKind

`scorecard` is the 12th and newest `OutputKind`. It has **no dedicated table** —
it lands in the generic `analyst_outputs` table (`kind='scorecard'`), written by
`scorecard_producer` (a deterministic META analyst; daily; pure SQL, no LLM,
`$0`). Each tick it writes **exactly one banded row per active g20/watch desk**
(19 G20 + 13 watch = 32) from a few high-precision RULES over that desk's
**already-verified** claims (the seven broad unit findings + the `country_composition`
— `proliferation_watch` is deliberately NOT one of the fixed scorecard
dimensions, since it would mis-render `insufficient-evidence` on the 17
non-nuclear desks it doesn't cover; its read still surfaces via
`country_composition`)
inside a **14-day band window**. It is a *perspective over* verified sub-claims,
never a fresh judgment:

- each band is derived from the finding's `severity:<level>` tag and its folded
  `effective_confidence`, and **names the ≥1 verified-claim id it rests on** (the
  row's `derived_from`), so a lineage walk resolves the basis with zero dangling;
- that tag is the dimension's **standing state** — where it stands on the desk
  today, not how far it moved this slice (FRAME-3, 2026-08-21). The movement rides
  a separate `severity_delta:<rose|fell|steady|new>` tag, is reported on the
  verdict beside the band, and **never enters** it; each card stamps
  `banding_semantics` so a band written under the older delta-severity contract is
  distinguishable from one written under this. An absent delta reads `null`, never
  `steady`;
- the rules **demote, never promote**: a claim below the confidence floor, or
  below the dedicated faithfulness floor, reads `low-faithfulness`; a dimension
  with no qualifying verified claim reads `insufficient-evidence` with an explicit
  machine `reason` and an **empty-but-explicit** basis — never a fabricated band.

**Honest today:** the live scorecard is a MIX. Some countries band on real
verified claims; others read all-`insufficient-evidence` because their units'
findings genuinely did not clear the faithfulness floor (the US reads
all-insufficient for exactly this reason — its unit faithfulness is low, and the
scorecard reports that rather than inventing a verdict).

## The contested-claims fact model

Task #101 Holes-B (migrations 0054 + 0055) turns "two credible
sources disagree on one `(subject, predicate)` value" from an invisible race into
a **first-class, derived, recomputable** state — without ever letting a machine
overwrite the disputed facts. The substrate change is small and almost entirely
*additive*; the behaviour is gated OFF by default.

> **Migration head — now `0185`.** (`0095`, `0100`, `0110` and `0111` are
> unused: `0095`/`0100` were skipped in the release wave, and `0110`/`0111`
> were reserved for the C3 source-quality ledger, which landed at `0115`
> after `0112`–`0114` took the intervening slots — and needed only one of the
> two. The runner
> has **no manifest and no head constant** — `migrate.py:_discover()` globs
> `*.sql` and sorts, applying each file in its own transaction and recording
> it by filename in `legba_data_migrations`, so numeric gaps are harmless and
> "head" simply means the lexicographically-last file present.) On top of
> the write-path gates documented below, successive migrations advanced the head
> `0060 → 0105`. Five reversible data-hygiene migrations make up the
> 2026-07-06 audit sub-range (`0076 → 0080`): `0076` entity re-fold + junk close
> (`entity_profiles` 12,257 → 12,144), `0077` semantic / demonym / relative-temporal
> junk facts closed (reversible `valid_until`), `0078` nexus junk & self-edge close
> + demonym/plural dyad canonicalization (reversible), `0079` a `cross_correlator`
> stale-head sweep (reversible), and `0080` a state-media `source_credibility` seed
> + a cross-target mislabel close. These close junk rows / seed credibility — they
> are data-hygiene closes, not schema changes. Migrations `0081 → 0085` are the
> signal-content-depth corpus/embedding markers (signal_summarized / indexed /
> reindex / embedding / reenriched) recording the full-body corpus, OpenSearch
> index, and Qdrant embedding backfill state. `0086 → 0090` are the
> entity-identity / salience / journal-data wave (entity-researcher +
> entity-blocking infrastructure, a nexus-fragment close, the per-signal
> salience schema, and the journal `data` column). `0091 → 0105` are the
> 2026-07-28 release wave — see
> [the wave section below](#the-2026-07-28-wave--new-tables-migrations-00910105) —
> and `0106 → 0115` the follow-on wave (see
> [its section](#the-follow-on-wave--new-tables-migrations-01060115)).
> `0116` adds ONE additive column, `bearing_edges.data jsonb NOT NULL DEFAULT
> '{}'` — the bearing gate's per-edge semantic-judgment stamp
> (`bearing_gate` = `yes`/`unavailable`/`deferred`, the judging component +
> prompt version, and the core-plane `bearing_confirm` verdict + reason). The
> default is what `claim_watch` writes with the gate OFF, which is the shipped
> state, so the column changes no existing row and no existing behaviour.
> The 2026-08 arc carries the head on `0117 → 0185` (sparse numbering — later
> slots are deliberately spaced): data-hygiene soft-closes and retirements
> (`0117`–`0121` — prefix-paired relational facts, telegram widget chrome,
> the never-written `entity_alias` table, poisoned journal rows, the GDELT
> doc-api source), `0122` a `judge_pipeline_version` stamp on band-calibration
> claims, `0130` a sub-floor embedding quarantine, `0140`–`0142` signal
> freshness/geo backfills, **`0143`–`0145` the `entity_edges` graph substrate**
> (one typed edge table backfilled from nexuses and promoted edges, later from
> facts at `0180`), `0150`/`0160`/`0165` fixture drops + proposed-edge
> retirements + a person-phonetic index, `0170` the correctness-axis
> promotion (comment-only), **`0175` corpus tombstones** (the OpenSearch
> delete path), `0176` the capital-metonymy fact soft-close, `0180`–`0182`
> entity-graph backfills + parked-endpoint adjudication, **`0184` the
> `situation_events` trajectory ledger**, and **`0185` the merge-keeper
> repoint** (proposed edges folded onto merge keepers — the twice-deferred
> `0183` replaced by a set-based mover-closure computation, proven on a full
> copy of live data before the train applied it).
>
> One in-tree wart worth knowing before you read the SQL: **`0108`'s internal
> comments all say "0106"**. It was renumbered `0106 → 0108` when a parallel
> branch claimed the `0106`/`0107` slots, and the body text was not re-swept.
> The runner keys on the **filename**, so `0108` is the authoritative number.

**Per-fact credibility — `facts.source_credibility real` (0054).** A 0..1 trust
score of the most credible source backing this fact, propagated down from
`signals.source_credibility` so the arbiter has a per-fact credibility term to
weight competing values by. Resolved at write time as the **MAX over the backing
signals'** `source_credibility`, else the **source-tier nominal** (seed/curated
`0.9`, agent/ingestion `0.5`). State/social outlets are now seeded *below* that
`0.5` ingestion nominal (0080 — presstv `0.25`, irna `0.30`, ukrinform `0.45`,
telegram/`t.me` `0.30`) so a state-affiliated source no longer out-credits its
peers. `NULL` = *unknown* (a pre-0054 row, or a write with
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
  `junk_count`, and `arbiter_version` / `resolved_at`. The **arbiter tail**
  (0097, 2026-07) adds the surfacing provenance — `surfaced_by`
  (`deterministic` / `llm`), `surfaced_at`, `surface_rationale`, and
  `surface_history jsonb` (newest-first, capped) — plus a separate cache table
  **`fact_contention_tiebreak`** (one cached LLM near-tie verdict per
  `(contention_id, evidence_fingerprint)`, `verdict ∈ pick|unsure`; only
  genuine verdicts are cached — a transport failure degrades to abstain,
  uncached). Still **no new column on `facts`**: the detect-only invariant
  (B15) is intact, and `status` + `surfaced_fact_id` moving is exactly what
  the `contention_flip` alert trigger fingerprints (`ANALYSIS.md` §7.11).
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
> successful LLM *pick* is **unobserved live** so far. The arbiter-tail flags
> follow the same posture: the read-side **contention annotation**
> (`LEGBA_CONTENTION_SURFACING`) defaults **ON**; the slice-reorder preference
> (`LEGBA_CONTENTION_SURFACING_PREFER`) and the earned-track-record tie-break
> weight (`LEGBA_CONTENTION_EARNED_WEIGHT`, a float defaulting `0.0`) default
> **OFF** — at weight 0 the tie-break is byte-identical to the pre-earned path.

## The 2026-07-28 wave — new tables (migrations 0091–0105)

Migrations `0091`–`0105` (`0095`/`0100` intentionally unused — the migration
runner discovers by sorted glob, so gaps are harmless) land the alerting loop,
the calibration/correctness record, the source-assurance fabric, and the graph
readouts. All are additive and idempotent (`CREATE … IF NOT EXISTS`), and the
derived sidecars share one pattern: **fully recomputable, wholesale-refreshed,
no supersession chain, no FK where keyed on descriptor/source ids** — like the
contention sidecar above, they are views *over* the chain, not primary data.

| Mig | Table(s) / change | Write semantics |
|---|---|---|
| **0091** | `alert_trigger_watermarks` — PK `(trigger_class, watermark_key)`, `state jsonb`, `fired_at` | **mutate-in-place upsert** — durable "this transition already fired" state for `alert_trigger_scan` (seven trigger classes today — incl. `watchlist_hit`, the folded-in `geo_convergence`, and the production gauge's `production_deficit`) plus `claim_watch`'s cursor row. First-ever scan per class **seeds silently**; a watermark advances **only after the alert row lands**, so a rejected write retries next scan and a transition never re-fires |
| **0092** | `source_poll_outcomes` + `newest_entry_ts` column | recorded per parsed HTTP-200 **before** the since-filter (26h future-skew clamp; 304 carry-forward) — the quiet-vs-cursor-fault discriminator (`ACQUISITION.md` §1.1.1) |
| **0093** | `band_calibration_claims` — one resolvable claim per scorecard band **transition**, UNIQUE `(desk, dimension, scorecard_row_id)`; per-horizon (14/28-day) outcome columns; **no probability column by design** (bands are not probabilities — no Brier exists or can). Plus `band_calibration_scan_state` | claims **append + never-overwrite** (`INSERT … DO NOTHING`); each horizon's outcome (`held` / `worsened` / `improved` / `reverted` / `insufficient` / `unresolvable`) is stamped once at resolution |
| **0094** | `source_ratings` — multi-rater assurance ratings; `visibility_class ∈ public\|private` run as **concurrent currents** via the partial unique `(source_id, rater, visibility_class) WHERE superseded_by IS NULL`; nullable Admiralty `A–F` × `1–6`; typed `rubric jsonb`; deferrable self-FK supersession. Plus `source_dossiers` (one current cited dossier per source, same supersession pattern) | **supersession-versioned** (append the new current, stamp `superseded_by` on the prior). The schema headers state the standing rule: **grades never touch the faithfulness score** |
| **0096** | `correctness_labels` — operator gold-set verdicts; **closed vocab** `correct \| partially_correct \| incorrect \| unresolvable`; **UNIQUE `finding_id`** upsert identity; `finding_snapshot jsonb` captured at label time (supersession can't orphan a verdict); `created_at` is the weekly-exclusion key. Plus `goldset_week_samples` — PK `(week, finding_id)`, the pinned weekly sample | labels **upsert** (one verdict per finding; re-label overwrites); samples **append-only** (`DO NOTHING` — first read pins the week) |
| **0097** | `fact_contention` surfacing columns + the `fact_contention_tiebreak` cache (see the contested-claims section above) | arbiter-tail state; still **detect-only** — no `facts` column added |
| **0098** | `fact_decay_states` — per-fact decay readout; PK `fact_id` (FK CASCADE); `decayed_confidence`, `decay_state ∈ fresh\|aging\|stale\|revoke_candidate`, `decay_class`, `last_sighting_at` + `sighting_source ∈ signal\|created_at` | **derived / recomputable** — `fact_decay_scan` upserts the sidecar and **never mutates a fact's confidence** (a DB test asserts byte-unchanged); consumption is flag-gated (`LEGBA_FACT_DECAY_WEIGHTING`, default OFF) |
| **0099** | `source_track_records` — the **earned** per-source record over resolved contentions; PK `source_id` (no FK); `win_rate_smoothed` (Beta(2,2)), `win_rate_lower` (Wilson), `low_sample`, corroboration counts, `lag_hours` (default 72 — only contentions surfaced ≥72h ago count) | **derived / recomputable** wholesale refresh (daily draft analyst). The arbiter's tie-break recomputes weights live with a **self-exclusion** guard (the contention being decided is excluded — acyclicity); the stored aggregate is a readout |
| **0101** | index `idx_analyst_traces_retention_run_started_at` on `analyst_traces` | serves the age-only purge scan of the `analyst_traces_retention` handler (ships **disabled**; opt-in via `LEGBA_ANALYST_TRACES_TTL_DAYS` — `RUNBOOK.md` §4.0.1) |
| **0102** | `narratives` — a contested-claim family reified; PK `contention_id` (1:1, no FK); carriers, first/last-seen, `lead_source_id`, `max_echo_lag_hours`, `variants jsonb`. Plus `narrative_echo_edges` — **directed** PK `(leader_source_id, follower_source_id)`; co-carriage counts, echo ratio, lag stats, `systematic bool` | **derived / recomputable** — `narrative_mapper` (daily, detect-only) wholesale-refreshes both. The honesty header — *descriptive, not causal* — rides the migration, the analyst, and every route envelope; "0 systematic edges" is published as-is. NOTE: the **code** classes were renamed `Propagation*` (`PropagationEdge` / `PropagationEdgeOut`); the **tables keep the echo names** |
| **0103** | `desk_baselines` — per-desk statistical baseline; PK `(desk_id, metric)`, `metric ∈ signal_volume_24h \| high_sev_findings_24h`; robust center/sigma (`robust_sigma = max(stddev, sqrt(mean))` — a Poisson floor), band low/high, `deviation ∈ within\|above\|below`, `features jsonb` (lags 1/7/28, rolling means 7/28, time-since-last-high-sev, land-neighbour spillover) | **derived / recomputable** (daily). The migration header states it plainly: **this table is NOT a forecast** — it is a falsifiable prior the alert trigger's deviation class consumes |
| **0104** | `evidence_archive` — the archive sidecar; PK `signal_id` with **no FK on purpose** (archived evidence outlives any future signal purge); `status ∈ archived \| failed \| skipped_license \| skipped_size`; `object_ref = cas:sha256/<hex>` mirrored onto `signals.object_ref`; `sha256`, sizes, `license_class`, `text_extracted`, attempts | **upsert sidecar**. The bytes live on the filesystem CAS volume (`legba_archive`), not in Postgres; **nothing deletes archived objects** (`SEAMS.md` #42) |
| **0105** | `watchlist` — operator standing watches; `kind ∈ entity\|text\|geo`, `pattern jsonb`, optional `min_severity`, `active` | **mutate-in-place** CRUD via `/api/v1/v3/watchlist` (the v3 family's first write surface; delete is **soft** — `active=false`). No-refire state rides the 0091 watermarks under `trigger_class='watchlist_hit'` |

**Read-time projections added by the wave** (all additive; none fabricated):

- **`below_floor`** on findings reads — `true` when a *graded* finding's
  `effective_confidence` sits below the 0.50 faithfulness floor; an ungraded
  row reads `null`, never a fabricated verdict (`substrate_reads_api.py`).
- **`verify_exempt`** — the reads stamp `"structural"` for the deterministic
  analysts in `STRUCTURAL_VERIFY_EXEMPT_ANALYSTS` (`provenance/kinds.py`); for
  the `structural_claims` opt-in subset, a passing deterministic re-derivation
  upgrades the badge to **`structural-verified`** (`ANALYSIS.md` §6.2).
- **`archived` + `archive_sha256`** on signal, lineage, and export
  projections — derived directly from `signals.object_ref`
  (`cas:sha256/<hex>`), no sidecar join.
- The per-claim **`claim_verdicts` ledger** (including SUPPORTED claims,
  capped 120 × 300 chars with an honest `claim_verdicts_truncated` flag) rides
  the existing critique `data` JSONB — **no migration** (`ANALYSIS.md` §6.2).

## The follow-on wave — new tables (migrations 0106–0115)

A second wave began the same day. Its theme is **coherence over time**: the
chain could already answer *"what did this finding rest on?"*, but not
*"what now rests on this finding?"* — nor *"has anything since arrived that
bears on a question we left open?"* The first three new stores answer those;
`collection_requirements` makes "we could not see it" a durable object instead
of a sentence in a monthly finding. The wave's tail (`0114`, `0115`) is
coherence of a different kind — organ consolidation: one poll-provenance row
per poll instead of a failure-only ledger, and one source-quality read surface
instead of four separately-grown ones.

| Mig | Table(s) / change | Write semantics |
|---|---|---|
| **0106** | `output_consumption` — the **forward** consumption index. PK `(consumer_id, consumed_id, context)`, plus `consumer_kind`, `consumed_at`; a second index `(consumed_id, consumed_at DESC)` is the forward-walk direction. **No FK on purpose** (consumers span `analyst_outputs` *and* `journal_entries`) | **append** (PK dedups). `context` is `text` with **no CHECK** — an open vocabulary; the three literals any code writes today are `composition_basis` (a load-bearing verified above-floor input head), `composition_periphery` (a below-floor/unverified row of a two-tier composition), and `journal_slice` (a row of the journal's rendered priming slice). The writer (`provenance/consumption.py`) **never raises** — a failed consumption write degrades, it cannot fail the compose |
| **0107** | `review_flags` — one row per (product, question-it-was-founded-on) pair whose foundation has since moved. `output_id`, `founded_on_id`, `moved_at`, `reason`, nullable `closed_by` / `closed_at` with a paired CHECK (`(closed_by IS NULL) = (closed_at IS NULL)`), a **partial unique index** allowing exactly ONE open flag per pair, and a **BEFORE DELETE trigger** that unconditionally raises — deletion is schema-impossible, closure is by supersession | **append + close-by-supersession.** `reason` is open text; two literals are written today, both by `claim_watch`: `new_evidence_bears_on_open_question` (consumer flags) and `new_evidence_bears_on_unconsumed_question` (the 4.1.0 question self-flag — evidence bearing on a watched question no product consumes). Rows are **never deleted** — the trigger makes silent flag disappearance a database error, not a code convention |
| **0107** | `bearing_edges` — dated, typed, weighted "X bears on Y" pointers. All columns `NOT NULL`: `edge_kind` (default `bears_on`), `src_kind`/`src_id`/`src_as_of`, `dst_kind`/`dst_id`/`dst_as_of`, `weight real`, `planes text[]` (CHECK non-empty), `provenance_class` (CHECK ∈ `live` \| `exemplar`), `matcher_version`, `UNIQUE (src_id, dst_id, edge_kind)` | **append-only**, `INSERT … ON CONFLICT DO NOTHING`. Honest scope note: append-only here is **writer discipline, not a trigger** — unlike `review_flags`, `bearing_edges` has no forbid-delete trigger; what guarantees it is that `provenance/bearing.py` contains exactly one statement (the insert) and no UPDATE/DELETE path exists anywhere in the tree. Written by exactly two producers today: `claim_watch` (`signal → hypothesis`, planes drawn from `vector`/`entity`/`geo`, `matcher_version='claim_watch/4.1.0'` today — the stamp records which matching rule wrote each edge, and rows from every earlier version remain — plus the `0116` `data` stamp when the bearing gate is on) and the corpus researcher's answer-link (`finding → hypothesis`, `planes=['corpus_research']`, `matcher_version='corpus_researcher_backlog/1.0.0'`). Both stamp `provenance_class='live'`; `exemplar` is reserved for a future curated set and is written by nothing |
| **0108** | `entity_block_key(text)` gains a leading-`a`/`an` strip; `idx_entity_profiles_block_key` rebuilt | function + index only, no table change. (This is the migration whose in-file comments still say "0106" — see the head note above.) |
| **0109** | `retention_policies` — the **one-janitor** config table. PK `policy_name`, plus `table_name`, `ttl_days integer NOT NULL DEFAULT 0`, `keep_classes text[]`, `batch_size` (CHECK > 0, default 5000), `enabled`, `env_fallback_var`, `description`, `created_by`, timestamps | **operator-edited config**, seeded `ON CONFLICT DO NOTHING` with exactly two rows — `signals_retention` (`keep_classes = {retain_always, evidence_hold}`) and `analyst_traces_retention` (`keep_classes = {}`) — **both at `ttl_days = 0`, which disables the sweep**. Deleting substrate data is an operator decision, so every seeded policy ships INERT. `/v3/retention-policies` (list/get/PATCH) edits `ttl_days`/`keep_classes`/`batch_size`/`enabled`/`description`; `policy_name`/`table_name`/`env_fallback_var` stay SQL-only (the code-side pairing to a Python adapter) |
| **0112** | `retrieval_origin text` added (nullable) to **both** `signals` and `evidence_archive`, with a partial index on the `signals` column where not null; the `evidence_archive` status CHECK is widened to add `skipped_license_unreviewed` | **stamped at write.** The value vocabulary is a **convention enforced in code, not a CHECK**: `NULL`/absent = a curated registered source (the default — nothing was backfilled), `curated_source` = the same thing said explicitly, `web_search:<component_id>` = retrieved through a named external search provider. One resolver (`legba.data.retrieval_origin.resolve_retrieval_origin`) serves both the archive gate and the corpus facet, so the two cannot drift |
| **0113** | `collection_requirements` — durable, operator-reviewable collection requirements. `natural_key text UNIQUE` (the idempotency key), `origin` (CHECK ∈ `collection_gap` \| `source_request`), `desk`, `dimension`, `topic`, `rationale`, `evidence_kind` (CHECK ∈ `analyst_output` \| `hypothesis`) + `evidence_id`, `source_classes_wanted text[]`, `candidate_sources jsonb`, `suggested_fetch_url`, `fillable` + `unfillable_reason` (paired CHECK: not-fillable **must** carry a reason), `priority_rank`, `status` (CHECK ∈ `proposed` \| `reviewed` \| `registered` \| `dismissed`), `reviewed_by` / `reviewed_at` (paired CHECK), `disposition_note` | **append (`ON CONFLICT (natural_key) DO NOTHING`) + disposition mutate-in-place.** Exactly one writer (`collection_gap`) and exactly one dispositioner (`/v3/collection-requirements`, PATCH-only). The content columns are immutable once written; the route may only move the disposition sidecar — see `ACQUISITION.md` §6.1 |
| **0114** | `source_poll_outcomes.outcome` CHECK widened to admit a third value, `success` — a productive poll (≥1 signal written) now leaves a row, where before the table was failure-only (`empty` \| `error`) and a productive poll wrote nothing at all | **constraint DROP/ADD only, no data migration** — every existing `empty`/`error` row stays valid. Closes the **error-streak latch guard**'s blind spot: `entity_gc` operation 4 auto-pauses a source after >20 contiguous *leading* `error` rows, and with no `success` outcome a recovered source's error run could only be broken by an `empty` row or the `last_signal` recency bound — never by the fact that it started producing again (bit `ukrinform` / `nasa.eonet` 2026-07-22 and `gdelt.files` 2026-07-27, each repaired by hand). A `success` row now breaks the leading run for every reader that walks it, with no operator intervention |
| **0115** | `source_quality` — a **VIEW**, the C3 source-quality ledger. One typed row per source id known to ANY leg, joining `source_credibility` (asserted, per HOST), `source_ratings` + `source_dossiers` (asserted, 0094), `source_track_records` (earned, 0099) and observed `signals` production (computed). Every non-backbone column is prefixed `asserted_` / `earned_` / `computed_` — the asserted-vs-earned split is the A6 honesty property, so it is enforced by column naming, and **no composite score column exists**. The host leg bridges the keying mismatch by extracting the descriptor's declared endpoint host and probing exact-then-trimmed parent domains, the same rule `filters.source_credibility.extract_lookup_hosts` applies at the signal write path (drift test-enforced) | **no writes — a view owns no state.** Drop and recreate it and the content is identical; that recomputability is the proof it is derived. The freshness GRADE is *not* a column: its budget derives from a cron expression through croniter (Python, not SQL), so the view carries the inputs and `registry/source_freshness.py` grades them at read — one grading implementation, two readers. Nothing here feeds the faithfulness score, and the arbiter's earned tie-break does not read it (byte-identity test-enforced) |
| **0116** | `bearing_edges.data jsonb NOT NULL DEFAULT '{}'` — ONE additive column carrying the **bearing gate**'s per-edge semantic judgment. Keys the writer stamps: `bearing_gate` (`yes` \| `unavailable` \| `deferred` — a gate **NO writes no row at all**, so `no` never appears here), `bearing_gate_ref` (the judging stack component), `bearing_gate_prompt` (the prompt version), and, for gate-passed edges, the core-plane second opinion `bearing_confirm` (`yes` \| `no` \| `unavailable`) + `bearing_confirm_reason` / `bearing_confirm_prompt` | **stamped at insert, never updated** — the table's writer-discipline append-only posture is unchanged. The gate ships **OFF**, in which case `claim_watch` binds `'{}'`, the column's own default, so a gate-off run's rows are byte-identical to what `claim_watch/3.2.0` wrote and no existing row is touched. `unavailable` / `deferred` exist because the gate must **never** fail closed: an 8B outage or a spent call budget stamps and WRITES the edge, and consumers filter on the stamp |

**What is NOT in this wave, and should be read as absent, not implied:**

- **No closer.** `review_flags` rows open and stay open. Nothing in-tree
  closes one, propagates a correction back into the flagged product, or
  recomposes anything (`SEAMS.md` #49).
- **No per-row read route** for `bearing_edges` or `review_flags`. The
  `staleness_debt` count they imply IS readable since the C3 wave
  (`GET /v3/system/staleness-debt`, aggregates only); the rows themselves are
  still receipt-and-SQL territory.
- **No `retrieval_origin` backfill.** Every pre-0112 row reads `NULL`, which
  means *unknown-but-presumed-curated*, not *verified curated*.

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

**Several tiers, one kind.** `entry_kind` discriminates the append-only tiers —
`entry` (the 12h field-notes beat), `chronicle` (the weekly third-person
public-record tier), `lens` (weekly faculty reads: four analyst ids —
`lens_trend`, `lens_baserate`, `lens_capability`, `lens_intent` — each carrying
one declared falsifiable prior over the verified tower top, asserting no new
fact), and `lens_diff` (the chorus pass that narrates where the four faculty
reads agree, split, or outlie — it never merges them) — from `consolidation`
(the daily distillation tier). The `/journal` default stream carries all append
tiers; consolidation remains slot-only.
A consolidation **supersession-versions** exactly like `facts`/`nexuses`:
`supersede_prior_consolidation` closes the prior open row
(`valid_until`/`superseded_by` stamped at the new id) **before** inserting the
new one, and a partial-unique index guarantees **at most one open
consolidation** (race/replay-safe). The journal's only un-gated effect is its
own continuity — it reads its own last entry + current open consolidation into
its next run.

**Everything outward is human-gated.** Live today, the journal functions as a
pure introspective instrument: it writes **only** its own entries +
consolidations directly (on cadence — the 12h `entry` beat is running, not
frozen). It may also queue a `correction`, a `change`, or a `self_revision`
(including a proposed edit to its own instructions; protected sections
auto-reject) onto the `journal_proposals` queue — **never a live table**. But
**routing an accepted reflection back** into another analyst / its own
instructions / substrate is a **FUTURE item, not done**: the human-gated
per-kind apply worker exists in code but is **not yet wired into live
operation**, and no accepted proposal has been routed back so far. So the
journal cannot touch any other analyst or the substrate today — by construction
it only narrates. Reads are served by `GET /api/v1/journal`; the review surface
is `GET /api/v1/journal_proposals` plus the `/journal_proposals/{id}/accept`
and `/reject` endpoints.

## Old → new vocabulary

| Old | Now | Changed |
|---|---|---|
| signals | `signals` | target-*agnostic* (no target_id); enriched inline; deduped via aliases |
| facts | `facts` | **temporal-versioned** (`valid_from`/`valid_until`/`superseded_by`, decay, `source_type`+`seed_batch_id`) |
| entities | `entity_profiles` (+`_versions`) | **canonicalized**, composite key `(name, entity_class)`, version history |
| findings | `analyst_outputs` (kind=`finding`) | generalized to a **12-kind** table (finding … `journal` … the 12th `scorecard`) + DLQ + emit channels; cited findings now carry a **mandatory faithfulness-verify** verdict folded into `effective_confidence` |
| situations | `situations` | **first-class temporal frames** (0040/0042): clustered bottom-up + a grounding source + the **events substitute** (no events table; events = signals + `get_timeline`, which now includes situation spans); `thematic_proposal` proposes uncovered hot frames for operator promotion to thematic targets |
| hypotheses | `hypotheses` | full **ACH** (≥2 theses, evidence×diagnosticity, ±2 transitions); calibration's **exogenous `resolved_outcome` resolver is built and firing** (subsequent-facts auto-resolver + operator-label path, migration 0038) alongside the live self-consistency tier — see "Known thin / inert legs" |

**New since the old system:** reified **signed/typed nexuses**; **temporal-
versioned facts**; **hash-chained `analyst_traces`**; the **agency ledgers**
(`action_pack_invocations` / `governor_events`); the **grounding seed substrate**
(`seed_batches`); `analyst_critiques` (now carrying the **mandatory faithfulness-
verify** verdicts alongside the eval-loop critic's); the budget ledgers; versioned
control-plane descriptors + audit log; the universal `derived_from[]` lineage
column; the **12th `scorecard` kind** (one banded row per active g20/watch desk);
the isolated forecast pilot `acute_forecasts` + the per-unit gold set
`unit_reference_labels`.

**A note on producers (data-model relevant, not a behaviour spec).** The rows in
`analyst_outputs` are no longer written by one monolithic per-country analyst.
The trusted spine is bottom-up: nine bounded `inline_target` UNITS →
`country_composition` (per country) → `region_composition` (5 region frames) →
`world_assessor` (global), plus the thematic `escalation_composition` →
`scorecard_producer` (deterministic banding). The old monolithic
`country_assessor` one-pager is **retired and stopped** (live head
`state='retired'` + removed from bringup; nothing in the trusted spine reads it),
so it writes no new rows — but its **~1.2k historical `finding` rows remain in
the DB, unread** (retirement is not a clean slate). The forecast-as-claim
producers (`country_predictor` and `india_energy_predictor`) are
**retired/frozen and stopped**, and the monolithic `country_optimizer` is
**cadence-frozen** (descriptor still `state='active'`); none write new rows, but
**~539 historical `prediction` rows remain**. Forecasting returns only as the
`acute_forecasts` Brier/BSS scoreboard (never a free-text claim; **NO proven
skill today**, honest), and self-optimization only as the scoped,
faithfulness-measured `unit_optimizer` — GEPA over a single measured unit
(`leadership_transition`), human-gated, with a real before/after paired
faithfulness delta (a live candidate scored parent `0.34` → candidate `0.29`,
delta `−0.05`, and was **refused**). Its candidates land as
`prompt_module_candidate`, human-gated — never auto-promoted. `world_assessor`
was NOT retired: it graduated into the world composition. See `SEAMS.md` for the
sequenced retirements/freezes and `ANALYSIS.md` for producer behaviour.

## Mutate-vs-append cheat-sheet

- **Append-only:** `analyst_outputs` (all 12 kinds, incl. the per-country
  `scorecard` rows), `analyst_traces`, `analyst_critiques` (critic + faithfulness-
  verify verdicts), `journal_entries` `entry` rows, `signal_aliases`,
  `entity_profile_versions`, `unit_reference_labels`, `acute_forecasts` issue rows,
  the audit/governor/seed ledgers, every `*_descriptors` version,
  `alert_sink_deliveries`, `band_calibration_claims` issue rows (horizon
  outcomes stamped once at resolution, like `acute_forecasts`),
  `goldset_week_samples` (first read pins the week), `output_consumption`
  (the forward index), `bearing_edges` (append-only by writer discipline —
  the module has no UPDATE/DELETE path — not by a trigger).
- **Append + close-by-supersession:** `review_flags` — one open flag per
  (product, foundation) pair, closed by naming the later output; a BEFORE
  DELETE trigger makes deletion a database error. Nothing closes one today.
- **Supersession-versioned (temporal):** `facts` (value change), `nexuses`
  (polarity/label change), `journal_entries` `consolidation` rows
  (a newer consolidation supersedes the prior open one) — new open row, old row
  closed; `facts`/`nexuses` also decay open rows; `source_ratings` /
  `source_dossiers` (a new current rating/dossier stamps `superseded_by` on the
  prior; public and private raters run as concurrent currents).
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
  non-append substrate table; the intra-source dedup `fetched_at` bump and the
  archiver's `object_ref`/`retention_class` stamp are further in-place
  updates), `entity_profiles`, `proposed_edges`,
  `hypotheses`/`situations` *status*, `journal_proposals` *status*
  (`pending`→`accepted`/`rejected`), `acute_forecasts` resolution (the exogenous
  grade stamped when the forward window closes), `budget_ledger`,
  `action_pack_invocations`, `output_dead_letter` resolution,
  `alert_trigger_watermarks` (durable trigger state), `watchlist` (CRUD +
  soft delete), `correctness_labels` (one-verdict-per-finding upsert),
  `evidence_archive` (outcome upsert), `collection_requirements` *disposition
  only* (`status` / `reviewed_by` / `reviewed_at` / `disposition_note` — the
  content columns are write-once), `retention_policies` *operator-tunable
  fields only* (`ttl_days` / `keep_classes` / `batch_size` / `enabled` /
  `description` via `/v3/retention-policies` PATCH, or SQL — `policy_name` /
  `table_name` / `env_fallback_var` stay SQL-only).
- **Ephemeral (no durable row):** subscription wiring, per-target JetStream
  consumers, the predicate match, NATS-stream output.
- **Derived / recomputable (rebuildable from the primary rows):** the
  contested-claims
  sidecar `fact_contention` / `fact_contention_values` — the detect-only arbiter
  upserts them each pass and also sets the `contested` / `contention_id` /
  `surfaced_winner` markers on `facts` in place, but never mutates a fact value
  (invariant B15) — plus the 2026-07 readout sidecars, all wholesale-refreshed
  by their daily analysts: `fact_decay_states`, `source_track_records`,
  `narratives` + `narrative_echo_edges`, `desk_baselines`, and the
  `fact_contention_tiebreak` verdict cache.

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
- **`acute_forecasts` — a scored pilot with NO proven skill yet (honest).** The
  isolated weekly binary-forecast pilot (0047) exists precisely to *earn back*
  the word "forecast" with an exogenously-resolved Brier/BSS — kept out of the
  findings feed and reported only on the calibration scoreboard. Today it reports
  **no skill**: a degenerate / geography-dominated probability vector ABSTAINS
  (zero rows), and skill is WITHHELD (`brier_forecast_acute=None`,
  `forecast_unproven=True`) until the sample is non-degenerate AND at-size AND
  BSS>0. It is a measured experiment, not a product claim.
- **`unit_reference_labels` — the correctness gold set is tiny (honest).** The
  per-unit correctness leg scores a unit's live read against a labeled reference
  answer, but the labeled set is very small today (n≈1), so per-unit correctness
  reports **insufficient-sample** rather than a headline accuracy — faithfulness
  (groundedness), not correctness-vs-reference, is the meaningful per-unit
  number right now. A separate **weekly operator gold-set loop** (0096:
  `correctness_labels` + `goldset_week_samples`) now grows an *additive*
  operator-correctness figure on the eval scoreboard — deliberately **never
  pooled** with this deterministic reference leg (`ANALYSIS.md` §6).
