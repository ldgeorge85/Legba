<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Data model

What each table is for, which plane owns it, and how it is written. Grounded in
`data/migrations/` and the write paths in `data/provenance/writes.py`, `runtime/source_actor.py` and
the inline filters.

`DATA_MODEL_V3.md` owns the **event surface, the temporal surface and the one typed graph** — the
`events` family, as-of semantics, and the edge vocabulary and projection contract. This document names
those tables and does not describe them. `ARCHITECTURE.md` owns the stores themselves; `ANALYSIS.md`
owns producer behaviour; `TUNABLES.md` owns every threshold.

**Contents:** [The shape](#the-shape-in-one-breath) · [Write semantics](#write-semantics-the-six-classes) ·
[Acquisition](#acquisition-plane) · [Knowledge](#knowledge-plane) · [Analysis](#analysis-plane) ·
[Measurement](#measurement-plane) · [Events, temporal, graph](#events-temporal-and-graph) ·
[Operational](#operational-plane) · [Control](#control-plane) ·
[The three questions](#the-three-questions) · [Lineage](#lineage-backward-forward-and-off-chain) ·
[Cheat sheet](#mutate-against-append-cheat-sheet) · [Migrations](#migration-mechanics)

## The shape, in one breath

```
SOURCE → SIGNAL (enriched inline) → fan-out (routing, not data)
        → SUBSTRATE (facts · entities · nexuses · proposed_edges)
        ⇄ ANALYSIS (analyst_outputs · hypotheses
                    · situations · analyst_traces)
        → OUTPUTS (alert / webhook / STIX / A2A / MCP / NATS / substrate)
```

Every arrow is a substrate boundary — a table or a stream — not a function call. Fan-out is the one
arrow that persists nothing: a signal is routed, never copied per target.

## Write semantics: the six classes

Every table below is labelled with one of these. The classes are the point of this document: knowing
whether a row can change under you is what makes a query trustworthy.

- **append-only** — rows are inserted and never updated or deleted. Some are enforced by a trigger
  that makes a delete a database error; others are writer discipline, where the writing module
  contains exactly one statement and no update or delete path exists in the tree. The two are
  distinguished per table, because only the first survives a future careless caller.
- **supersession-versioned (temporal)** — a change opens a new row and closes the prior one by
  stamping `valid_until` and `superseded_by`. The open row is the current one; history stays readable.
- **mutate-in-place** — one row, updated. Reserved for state that is genuinely a current value
  (a cursor, a status, a ledger balance) rather than an assertion.
- **append plus close-by-supersession** — a hybrid: rows are appended and closed by naming the later
  row, never deleted.
- **derived / recomputable** — a sidecar that can be dropped and rebuilt wholesale from the primary
  rows. That recomputability is the test that proves it is a view *over* the chain rather than primary
  data, and it is the honesty posture every readout in the platform shares.
- **ephemeral** — no durable row at all.

---

## Acquisition plane

| Table | Purpose | Write semantics |
|---|---|---|
| `signals` | the canonical observation pool — one row per observation, target-agnostic, with the baseline's enrichment in indexed columns. Carries `origin_class` + `collection_id` (migration 0209: the six-class provenance vocabulary; the three history classes are refused at the table until the reader sweep lands — SEAMS #57) | **append-only** insert, then **mutate-in-place** for enrichment, the intra-source re-serve `last_seen_at` bump, the archiver's `object_ref` and `retention_class` stamp, and a later entity-merge repoint of `canonical_signal_id`. This is the one substrate table that is not strictly append-only |
| `signal_aliases` | the cross-source duplicate link — a duplicate is linked to its canonical, never collapsed | **append-only** |
| `source_poll_outcomes` | one row per poll: `success` / `empty` / `error`, plus `newest_entry_ts` — the provenance for *why* a source went quiet and for the fact that it recovered | **append-only** |
| `source_credibility` | the per-host scored track record the signal write path resolves into `signals.source_credibility` | **mutate-in-place** (operator-editable) |
| `evidence_archive` | the archival outcome sidecar; the bytes live content-addressed on a filesystem volume, not in Postgres, and nothing deletes an archived object | **upsert sidecar**; no foreign key on purpose, so archived evidence outlives a future signal purge |
| `corpus_tombstones` | the search corpus's delete queue: the deleted `signals.id` is the corpus document id, so no mapping is needed. Rows are never removed — the drain stamp keeps every dropped id queryable | **append plus drain-stamp**, written in the same transaction as its delete |
| `iso_countries` | the country reference table the geo and desk vocabularies resolve against | **static reference** |

## Knowledge plane

| Table | Purpose | Write semantics |
|---|---|---|
| `facts` | atomic `(subject, predicate, value)` assertions with temporal validity and a source tier. Carries `origin_class` + `collection_id` and — since 0209 — `superseded_at`, the decision-time close stamp `analyst_outputs` already had (backfilled for older rows as the successor's `created_at`, an approximation) | **supersession-versioned** over an open-only unique index. Supersession is single-winner-by-recency *within a source tier*, so a machine-extracted fact cannot close a human-curated one; agreeing sources combine confidence by a bounded noisy-OR rather than a maximum |
| `nexuses` | reified, typed, signed relationships between entities — the operative edge set of the knowledge graph | **supersession-versioned**; a polarity or label change closes the prior row |
| `entity_profiles` | one node per distinct resolved entity, deduped on a canonical surface | **mutate-in-place** plus an append-only version history in `entity_profile_versions` |
| `entity_edges` / `entity_edges_unresolved` | the id-keyed entity-to-entity edge store — endpoints are profile ids behind real foreign keys, never names, so a merge repoints rather than strands. An endpoint that cannot be resolved is parked with a reason rather than dropped or guessed | **append-only**; the park rate is the honest residue |
| `entity_judgement` | one adjudication verdict per candidate entity pair — an audit row and a re-adjudication cache | **append-only** |
| `proposed_edges` | pairwise co-occurrence candidates accruing confidence, the reifier's input queue | **mutate-in-place** (status plus confidence accrual; no version chain) |
| `signal_entity_links` | the signal-to-entity provenance edge | **append-only** |
| `graph_metrics` | signed-triad balance, centrality and community, proxy-chain sign products, as queryable rows | **append-only** |
| `seed_batches` | the curated-import ledger; facts and nexuses carry its id | **append-only** |
| `fact_contention` / `fact_contention_values` | the contested-claim sidecar: one group per `(subject, predicate)`, one row per distinct non-junk value cluster with its support and its deterministic score | **derived / recomputable** — the arbiter upserts them each pass and sets three thin markers on `facts`, but never mutates a fact |
| `fact_contention_tiebreak` | one cached tie-break verdict per `(contention_id, evidence_fingerprint)`, so the same evidence is never re-asked | **append-only**; only genuine verdicts cache, a transport failure degrades uncached |
| `fact_decay_states` | the per-fact decay readout | **derived / recomputable**; it never mutates a fact's confidence, and a database test asserts the facts rows byte-unchanged |

## Analysis plane

| Table | Purpose | Write semantics |
|---|---|---|
| `analyst_outputs` | the generic typed-output table, kind-routed across the `OutputKind` vocabulary — findings, meta-findings, alerts, critiques, prompt-module candidates, scorecards, situation updates | **append-only**; a validation failure routes to the dead-letter table rather than landing a malformed row |
| `analyst_traces` | one row per run — output, side-write, trace-only or failure alike — carrying the run's intermediate steps, tool calls and prompt hash, hash-chained per analyst | **append-only** |
| `analyst_outputs` rows with `kind = 'critique'` | every faithfulness-verify verdict, one row per verified output, `confidence` carrying the score and the judge model and pipeline revision stamped in `data`; the legacy `analyst_critiques` table exists and is empty | **append-only** |
| `hypotheses` | competing-hypothesis pairs with their evidence matrix, and — reusing the same shape — the standing open-question set | **append** plus **status mutate-in-place** |
| `situations` | first-class durable temporal frames keyed by a situation signature | **temporal-frame**: `valid_from` / `valid_until` / `superseded_by`, closed when the frame closes |
| `situation_events` | the situation trajectory ledger — one row per movement, with an evidence-time clock | **append-only, schema-enforced**: both delete and update are barred by trigger, and a movement claim without new evidence is unrepresentable by a check constraint |
| `finding_supersessions` | the near-duplicate finding link, so a newer finding supersedes a prior one without a destructive delete | **append-only** |
| `journal_entries` | the off-chain reflective voice — entries, chronicles, lens reads, and the consolidation tier | `entry`-family rows **append-only**; `consolidation` rows **supersession-versioned**, with a partial-unique index guaranteeing at most one open consolidation |
| `journal_proposals` | the human-gated review queue for anything the journal wants to change outward | **append** plus **status mutate-in-place**; never a live table |
| `output_consumption` | the **forward** index: what now rests on this row. Distinguishes a load-bearing basis input from a hedged-context periphery one | **append**; no foreign key on purpose, because consumers span both output tables. The writer never raises — a failed consumption write degrades rather than failing the compose |
| `review_flags` | one open flag per (product, foundation) pair whose foundation has since moved | **append plus close-by-supersession**; a before-delete trigger makes deletion a database error |
| `bearing_edges` | dated, typed, weighted "this later thing bears on that earlier question" pointers, stamped with the matcher version that wrote them | **append-only by writer discipline** — the writing module contains exactly one statement and no update or delete path exists in the tree |
| `collection_requirements` | a durable, operator-reviewable statement that something could not be seen | **append** (idempotent on a natural key) plus **disposition mutate-in-place**; the content columns are write-once |
| `alert_trigger_watermarks` | durable "this transition already fired" state per trigger class, plus the change-detector cursors that ride the same partition | **mutate-in-place upsert**; a watermark advances only after the alert row lands |
| `alert_sink_deliveries` | one row per delivery attempt; it doubles as the state store that stops a restart re-firing a standing alert | **append-only** |
| `watchlist` | operator standing watches over an entity, a text pattern or a place | **mutate-in-place**; delete is soft |
| `desk_baselines` | each desk's own statistical activity prior, with a Poisson-floored sigma. The migration header, the analyst and the summary finding all say the same thing: this is not a forecast | **derived / recomputable** |
| `narratives` / `narrative_echo_edges` | a contested-claim family reified, and the directed source-echo graph over it. Descriptive, not causal — an edge says one source tends to publish the same contested claims after another, at a measured lag | **derived / recomputable**, wholesale-refreshed |
| `consult_sessions` / `consult_turns` | the consult audit trail: one session header per conversation, append-only turns carrying the reasoning steps, tool calls and cited references | header **mutate-in-place**, turns **append-only** |
| `output_dead_letter` | rows that failed validation on the write path | **append** plus operator-resolution mutate |

## Measurement plane

| Table | Purpose | Write semantics |
|---|---|---|
| `unit_references` | one independent reference per (desk, window) — the developments the grader grades against, built by the reference builder and never by the grader | **append-only** on a content hash |
| `unit_correctness` / `unit_correctness_claims` | one unit's correctness against its reference at one as-of stamp, and the per-claim ledger beneath it — the surface on which a disputed share is re-argued claim by claim | **append-only** |
| `grader_calibrations` | one calibration run. The grader refuses to publish a number unless a passing row exists whose rubric hash matches and whose model set covers every model the run will use | **append-only**; `method_version` stamps the gate revision (0211) |
| `external_grades` | the platform's database home for external truth: one graded claim per grader family and pipeline version | **append-only** |
| `correctness_labels` / `goldset_week_samples` | the weekly operator gold set — the closed-vocabulary verdict per finding, and the pinned weekly sample | labels **upsert** (one verdict per finding); samples **append-only**, so the first read pins the week and the worksheet cannot shift under the labeller |
| `unit_reference_labels` | the secondary diagnostic correctness axis, deliberately never pooled with the operator one | **append-only** |
| `band_calibration_claims` / `band_calibration_scan_state` | every scorecard band transition logged as a resolvable claim, graded at fixed horizons. There is no probability column by design — bands are not probabilities | claims **append, never overwritten**; each horizon's outcome is stamped once at resolution; `method_version` stamps the harness revision (0211) |
| `acute_forecasts` | the isolated binary-forecast pilot, kept out of the findings feed | **append** at issue plus **resolution mutate-in-place**, graded exogenously when the forward window closes; `method_version` stamps the instrument revision (0211), `resolution_test` freezes the falsifiable contract as text at mint (0212), and a due-but-unresolved row is marked `resolved_by='unresolved:expired'` — held in the honest denominator, still retryable |
| `receipt_anchors` | one OpenTimestamps attestation per (day, calendar): the Merkle root over the day's analyst_traces chain heads, the calendar's proof bytes, status `submitted`/`pending` | **append-only** (one row per day per calendar; a `pending` row upgrades in place when its calendar answers) — 0212 |
| `read_events` | the reading instrument — opens and drills against the published surfaces | **append-only** |
| `source_ratings` / `source_dossiers` | the asserted source-assurance layer: multi-rater grades and one current cited dossier per source, with public and private raters running as concurrent currents | **supersession-versioned** |
| `source_track_records` | the **earned** per-source record over resolved contentions, smoothed and lower-bounded, computed at a lag | **derived / recomputable** |
| `source_quality` | a **view** joining the asserted, earned and computed legs. Every non-key column is prefixed `asserted_` / `earned_` / `computed_`, and no composite score column exists — the split is enforced by column naming | **none; a view owns no state** |

## Events, temporal and graph

`events`, `signal_event_links`, `event_entity_links`, `event_edges`, `situation_event_links` and the
append-only `event_lifecycle_events` ledger are the v3 event surface. **`DATA_MODEL_V3.md` is the
document for them** — identity and dedup, the lifecycle machine, the promotion rule, as-of semantics,
the edge vocabulary and the projection contract. Two facts belong here because they change how the
rest of this document reads: every live event write is gated behind a flag that ships off, and the
first phase ships no consumer, so nothing reads these tables yet.

The first consumer landed with the v3 **event citation** (P2, flag `LEGBA_EVENT_CITATIONS`, off):
a finding may cite `event:<uuid>`; the citation builder (`provenance/event_citations.py` on the
unit path, `analysts/composition_event_citations.py` on the composition path) expands the token
through `signal_event_links` — ranked `relevance DESC, linked_at DESC`, capped by
`LEGBA_EVENT_EXPANSION_MAX_SIGNALS` (default 4, `event_expansion_truncated` stamps the overflow)
— into ordinary per-signal citation entries, each carrying the signal's real `source_text`
through the same precedence and 3,200-char cap as every other citation. The judge grounds on the
signals; `verify.py` is unchanged; the event's own `summary` is never emitted anywhere; the
finding's `derived_from` carries the event id AND the expanded signal ids, and the lineage walk
resolves both (`events` is a lineage node; `journal_entries` stays excluded). `event` is
deliberately not in `GROUNDING_REF_KINDS` — expanded entries carry `signal_id`, so the grounding
test already returns false, and admitting the kind would let an event be graded against its own
summary.

The v3 **temporal read surface** has landed (P3): `as_of` on the substrate readers over the
supersession-versioned registers (`facts`, `nexuses`, the `entity_edges` graph walks, `situations`)
reads the rows that held on date D — `COALESCE(valid_from, -infinity) <= D AND
(valid_until IS NULL OR valid_until > D)` — with `unbounded_start` counting the rows that have no
recorded start; `since`/`until` bound `get_timeline` on each stream's anchor;
`believed_as_of` on `list_findings` and `GET /api/v1/v3/belief` read the decision clock
(`produced_at`/`superseded_at`) with the verdict fold stamped and `verdict_pending_at_as_of`
counted; and `entity_edge_events` (migration 0206, flag `LEGBA_EDGE_TRANSITION_LEDGER`) is the
append-only transition ledger for edge mints and polarity flips — the ~8,872 folds that already
happened are unrecoverable and the migration header says so.

The v3 **graph projection** has landed (P4b, flag `LEGBA_GRAPH_PROJECTION`, off):
`graph_arcs` (migration 0207) is a disposable, plane-tagged (`world` / `evidence` / `lineage`)
projection of every cross-layer arc — deliberately no foreign keys, rebuilt whole by the
`graph_projector` sweep into `graph_arcs_new` and swapped in atomically, never incrementally.
`graph_arcs_meta` carries the one-row build receipt (`projected_at`, `arc_count`,
`source_counts`, `build_seconds` — the E4 trigger reading); every reader publishes the stamp and
refuses distinctly (`projection_disabled` / `projection_empty` / `projection_stale`) when the
flag is off, nothing was built, or the build is older than
`LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS` (7,200 s). `GET /api/v1/v3/graph/arcs` is the bounded
ego/walk read, world-plane by default; `graph_mining` and `structural_balance` build their
snapshots from `graph_arcs WHERE plane='world'` while the flag is on.

Situations remain the events substitute for everything that does read today: there is no table of
occurrences in the analysis plane, and a timeline is assembled from signals plus situation spans.

## Operational plane

| Table | Purpose | Write semantics |
|---|---|---|
| `budget_ledger` / `global_budget_envelope` | per-analyst and fleet-wide token accounting | **mutate-in-place** |
| `budget_demotion_events` | one row per budget-driven demotion | **append-only** |
| `action_pack_invocations` | the agency ledger — one row per governed tool call, settled with its true outcome, cost, units and duration. The next call's rate window reads it | **append** plus settle |
| `governor_events` | every allow and every block, with the cap dimension, the limit and the observed value | **append-only** |
| `retention_policies` | the one-janitor config table the shared sweep engine reads. Both seeded policies ship with a zero TTL, which disables the sweep, because deleting substrate data is an operator decision | **mutate-in-place config**; the route may move only the operator-tunable fields, while the code-side pairing to a Python adapter stays SQL-only |
| `actor_state` / `actor_filter_state` / `trigger_state` | runtime state: the Dapr actor store, the crash-safe source cursor and provisioning store, and the coalescing accumulator | **mutate-in-place** |
| `legba_jobs` | the async job plane's queue | **mutate-in-place** |
| `discovery_state` | per-discovery cycle state, including the disappearance history | **mutate-in-place** |

## Control plane

| Table | Purpose | Write semantics |
|---|---|---|
| `source_descriptors` / `target_descriptors` / `analyst_descriptors` / `action_pack_descriptors` | the four descriptor families — the live system *is* these rows, not the YAML files on disk | **append-only**, content-hash versioned, one head per id |
| `stack_components` / `stack_credentials` | the model, embedder, search and store components descriptors resolve their stack references against | **append-only** versions; credentials are vault-backed |
| `wiring_descriptors` | explicit subscription grants, keyed `(source_id, target_id)` | **append-only** |
| `vocabulary_entries` | the open vocabularies the registry validates against, including the extension analyst kinds | **append-only** |
| `descriptor_audit_log` / `audit_checkpoints` | the signed audit log over descriptor mutations, and its periodic checkpoints. This is the one place a cryptographic signature exists | **append-only** |
| `descriptor_dead_letter` / `descriptor_conversion_archives` / `conversion_webhooks` / `conversion_executions` | rejected registrations, and the descriptor conversion surface | **append** plus resolution mutate |
| `ui_panel_registrations` | the workstation's panel registry | **mutate-in-place** |
| `legba_data_migrations` | applied-migration bookkeeping, keyed by filename | **append-only** |

---

## The three questions

### Does the inline pipeline change the signal, or add to it?

Both, stage-specific. The `signals` *row* is written once. The enrichment stages then mutate that one
row in place — language, geo, entity classes, classification, credibility. Only two stages write
elsewhere: `ingest_dedupe` appends a `signal_aliases` row and sets `canonical_signal_id`, and
`fact_extractor` appends to `facts`. `signals` is therefore the one substrate table that is not
strictly append-only, and the honest list of what later re-updates it is in the acquisition table
above.

### What does the target and fan-out layer record?

Almost nothing persistent — it is routing and control, not data. There is **no `target_id` on
`signals` and no per-target delivery table**. What exists is `target_descriptors` (the geo, tags and
predicate contract), in-memory subscription wiring plus one durable aggregated consumer per target, a
two-stage match computed fresh at delivery, and the target actor's lifecycle and cursor state. A
non-discovery target is a passive subscriber.

A target is a scoped subject — a **desk**, a named scope-frame a set of analysts work — not a
surveilled entity. Desks are selected by coverage tag, and the roster spans country desks, region
frames and a non-country thematic family; adding one is registering a target with the right tag, with
no code change. `RELEASE_STATE.md` carries the live roster.

### What does analysis read and write?

Reads depend on the analyst's altitude. A first-order reasoning unit reads a scope-filtered signal
slice plus open facts, nexuses and hypotheses, plus a grounding preamble of accumulated substrate
state — so it integrates over time rather than only over today's signals. A composition reads **no raw
signals**: it reads other analysts' verified findings, inner-joined on the faithfulness critique above
the floor, so an unverified sub-claim never enters as assertable evidence. A deterministic handler
reads whatever materialised slice its job needs.

Writes go through two channels: `analyst_outputs` for the typed output, and direct substrate
side-writes for facts, nexuses, hypotheses, entities and the ledgers. The trace-only kinds write no
output row at all — their product *is* the side-write. **Every run**, whatever it produced, leaves
exactly one hash-chained `analyst_traces` row, with the output reference empty when nothing was
emitted. That is what makes "did this analyst run?" answerable independently of "did it produce
anything?".

---

## Lineage: backward, forward, and off-chain

**Backward** is `derived_from`, a UUID array on every substrate table, stamped at write. A lineage walk
resolves a world read through its country reads, their units, each unit's cited signals, and each
signal's acquisition record.

**Forward** is `output_consumption`: what now rests on this row. It is stamped at the point where
consumption is *decided* — inside a composition's own basis-and-periphery split, and at the journal's
rendered slice selection — and materialised on the same connection as the output write, so a consumer
row and its edges land together or not at all. The distinction it preserves is the one that matters
for triage: "new evidence contradicts something a live read is built on" and "…something a live read
mentioned as a caveat" are different-severity facts.

**A bearing edge is not lineage.** It says a later thing bears on an earlier question, and it never
mutates the question it points at.

**The journal is the one deliberate exception to the chain.** A journal row carries an always-empty
`derived_from`, and `journal_entries` is deliberately absent from the lineage catalogue, so a walk
from a fact, situation or nexus can never surface a journal node. Its citations live only in its own
claim and cited-reference columns — an up-only warrant that points out at the substrate it read, never
in as lineage a walk can descend into. A gating test holds the never-writes-a-fact line, and the
grant layer backs it up: the journal holds only non-write-fact packs. The journal is a perspective
*over* the chain, not a member of it.

---

## Mutate against append cheat sheet

- **Append-only, trigger-enforced:** `situation_events` (delete and update both barred),
  `review_flags` (delete barred; closure is by supersession), `event_lifecycle_events`.
- **Append-only, writer discipline:** `analyst_outputs`, `analyst_traces`,
  `journal_entries` entry-family rows, `signal_aliases`, `entity_profile_versions`, `bearing_edges`,
  `output_consumption`, `external_grades`, `unit_references`, `unit_correctness`,
  `unit_correctness_claims`, `grader_calibrations`, `read_events`, `acute_forecasts` issue rows,
  `band_calibration_claims` issue rows, `goldset_week_samples`, `source_poll_outcomes`,
  `graph_metrics`, `seed_batches`, the governor and audit ledgers, and every descriptor version.
- **Supersession-versioned:** `facts` (on a value change), `nexuses` (on a polarity or label change),
  `situations` (on frame close), `journal_entries` consolidation rows, `source_ratings` and
  `source_dossiers`. Facts and nexuses also decay their open rows. Under the contention flag, a
  same-tier incoming value that is fuzzy-distinct from an open prior does **not** close it — the two
  coexist open as a first-class dispute; with the flag off, single-winner-by-recency is unchanged.
- **Mutate-in-place:** `signals` enrichment and its later stamps, `entity_profiles`, `proposed_edges`,
  hypothesis and situation status, journal-proposal status, `acute_forecasts` resolution,
  `correctness_labels`, `evidence_archive`, `collection_requirements` disposition only,
  `retention_policies` tunable fields only, `alert_trigger_watermarks`, `watchlist`, the budget and
  invocation ledgers, `output_dead_letter` resolution, and the runtime state stores.
- **Derived / recomputable:** `fact_contention` and `fact_contention_values`, `fact_contention_tiebreak`,
  `fact_decay_states`, `source_track_records`, `narratives` and `narrative_echo_edges`,
  `desk_baselines`, and the `source_quality` view.
- **Ephemeral:** subscription wiring, per-target consumers, the predicate match, NATS output streams.

---

## Migration mechanics

The runner has no manifest and no head constant: it globs `*.sql`, sorts, applies each file in its own
transaction, and records it by filename. So numeric gaps are harmless, "head" simply means the
lexicographically-last file present, and two independent files may land out of numeric order. Every
migration is additive and idempotent where it can be, and a data-hygiene close is written reversibly.

`CHANGELOG.md` and `history/ANALYSIS_decisions.md` carry which migration landed what, and when.
