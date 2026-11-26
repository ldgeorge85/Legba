<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Data model

What each table is for, which plane owns it, who writes it, and whether a row can change after it
is written. Grounded in `src/legba/data/migrations/`, the write paths under `src/legba/data/`
(`provenance/writes.py`, `runtime/source_actor.py`, the inline filters) and the one declared table of
writers, `src/legba/data/write_attribution.py`.

[ARCHITECTURE.md](ARCHITECTURE.md) owns the stores themselves; [ANALYSIS.md](ANALYSIS.md) owns
producer behaviour; [TUNABLES.md](TUNABLES.md) owns every threshold; [TYPES.md](TYPES.md) lists the
type registry the tables below refer to.

**Contents:** [The shape](#the-shape-in-one-breath) · [Write semantics](#write-semantics-the-six-classes) ·
[Who wrote a row](#who-wrote-a-row-principals-and-written_by) ·
[Acquisition](#acquisition-plane) · [Origins](#origins-and-origin-groups) ·
[Knowledge](#knowledge-plane) · [Analysis](#analysis-plane) ·
[Citations as rows](#citations-as-rows) · [The held record](#the-held-record) ·
[Measurement](#measurement-plane) · [Events, temporal, graph](#events-temporal-and-graph) ·
[Classifications](#classifications--dataclassifications) ·
[People and sign-in](#people-and-sign-in) · [Operational](#operational-plane) ·
[Control](#control-plane) · [The three questions](#the-three-questions) ·
[Lineage](#lineage-backward-forward-and-off-chain) ·
[Cheat sheet](#mutate-against-append-cheat-sheet) · [Migrations](#migration-mechanics)

## The shape, in one breath

```
SOURCE → SIGNAL (enriched inline) → fan-out (routing, not data)
        → SUBSTRATE (facts · entities · nexuses · proposed_edges · events · observations)
        ⇄ ANALYSIS (analyst_outputs · hypotheses
                    · situations · analyst_traces)
        → OUTPUTS (alert / webhook / STIX / A2A / MCP / NATS / substrate)
```

Every arrow is a substrate boundary (a table or a stream), not a function call. Fan-out is the one
arrow that persists nothing: a signal is routed, never copied per target.

Everything is relational, in one Postgres cluster. There is no separate graph database: the Apache
AGE graph the platform once kept was empty, and migration `20261115T1829_drop_empty_age_graph`
drops it (refusing if any label table holds a row). The `age` extension itself is still installed so
that existing connections keep loading it; removing it is a later image change. Graph-shaped reads
go through the id-keyed `entity_edges` store and the rebuilt `graph_arcs` projection
([below](#the-cross-layer-graph-projection)).

## Write semantics: the six classes

Every table below is labelled with one of these. Knowing whether a row can change under you is what
makes a query trustworthy.

- **append-only**: rows are inserted and never updated or deleted. Some are enforced by a trigger
  that makes an update or delete a database error; others are writer discipline, where the writing
  module contains exactly one insert and no update or delete path exists in the tree. The two are
  distinguished per table, because only the first survives a future careless caller.
- **supersession-versioned (temporal)**: a change opens a new row and closes the prior one by
  stamping `valid_until` and `superseded_by`. The open row is the current one; history stays readable.
- **mutate-in-place**: one row, updated. Reserved for state that is genuinely a current value
  (a cursor, a status, a ledger balance) rather than an assertion.
- **append plus close-by-supersession**: rows are appended and closed by naming the later row, never
  deleted.
- **derived / recomputable**: a sidecar that can be dropped and rebuilt wholesale from the primary
  rows. Recomputability is what proves it is a view over the chain rather than primary data.
- **ephemeral**: no durable row at all.

## Who wrote a row: principals and `written_by`

Three different "who" are recorded and never merged into one "source":

- who **wrote** the row: a principal (this section);
- where the **content** came from: an origin and its origin group ([below](#origins-and-origin-groups));
- who is **speaking** inside the content: the claimant of a claim source.

A **principal** is one row in `principals` with a stable id and a unique, immutable handle
`<kind>:<name>`. The kinds are declarations in the type registry
(`src/legba/data/type_registry/principal_kinds/`):

| kind | name | example |
|---|---|---|
| `user` | the person's `users.id` | `user:0b6c…0001` |
| `service` | a platform service | `service:runtime` |
| `agent` | an analyst seat, `<descriptor id>@<version>` | `agent:desk_x@3f9a` |
| `rule` | a deterministic handler or code path | `rule:collection_gap` |
| `shared` | a credential several people hold | `shared:admin-token` |

Principals are created on first use and never seeded or edited by hand. A write names its principal
by handle, and the database function `legba_principal(handle)` (migration
`20261115T1827_principals_origins_written_by`) upserts the row and returns its id inside the same
statement, so no write makes an extra round trip. `src/legba/data/principals.py` validates the
handle before it reaches SQL. With sign-in switched off every person presents the one shared admin
token, so a person's write names `shared:admin-token`: the credential, never a guessed person.

`write_attribution.py` puts every table into exactly one of three classes, and
`tests/data_pkg/test_write_attribution.py` fails when a table is missing from it:

- **(a)** the row already names its writer in a stamped column: the seat (`analyst_id` +
  `analyst_version`), the audit actor the registry stamps, the user a session belongs to, or the
  parent row it is written with. Nothing is added.
- **(b)** the table holds judgements, labels, decisions, descriptor versions, watch lists or entered
  knowledge, and carries a nullable `written_by uuid REFERENCES principals(id)`, filled from the
  request principal, the running seat or the rule. Rows written before the migration stay NULL:
  their writer was never recorded and is not guessed.
- **(c)** a machine table whose writer is implied by the table (signals, observations, fetch
  versions, projections). The writer is recorded once, in `write_attribution.py`, as a principal
  handle; no column is added.

`scripts/report_written_by.py` reports the last seven days of attribution.

---

## Acquisition plane

| Table | Purpose | Write semantics |
|---|---|---|
| `signals` | the canonical observation pool: one row per observation, target-agnostic, with the baseline's enrichment in indexed columns. Carries `origin_class` + `collection_id` (the provenance vocabulary, [below](#origin-class-and-the-history-firewall)) and a nullable `data jsonb` whose `classifications` key is the bound form of `signals.tags` ([Classifications](#classifications--dataclassifications)); NULL means never classified | **append-only** insert, then **mutate-in-place** for enrichment, the intra-source re-serve `last_seen_at` bump, the archiver's `object_ref` and `retention_class` stamp, and a later entity-merge repoint of `canonical_signal_id`. The one substrate table that is not strictly append-only |
| `signal_aliases` | the cross-source duplicate link: a duplicate is linked to its canonical, never collapsed | **append-only** |
| `source_poll_outcomes` | one row per poll: `success` / `gated` (the feed delivered and the keyword gate dropped every entry) / `empty` / `error`, plus `newest_entry_ts`, `keyword_filtered` and, for non-document sinks, `sink_counts`. A subscribed source writes one row per window, with the connection's state in `stream` | **append-only** |
| `source_stream_gaps` | one row per declared gap in what a subscribed source received (queue overflow, a reconnect that could not resume). The subscribe mode is built and off by default; see [DATA_SOURCES.md](DATA_SOURCES.md) | **append-only** |
| `fetch_versions` | the bytes a fetch actually received, as a version sequence per canonical address (or per source and body hash for a pushed envelope). An identical re-fetch bumps `last_seen_at` and `fetch_count`; a different body is a new row. The bytes live content-addressed in the evidence store, referenced by `object_ref`; for a page, the extracted main text is kept with its own hash | **append** plus last-seen bump; never overwritten |
| `fetch_version_records` | which records (signals, observations) were derived from which version, and in what role (`response` or `page`) | **append-only, trigger-enforced** |
| `fetch_drift_observations` | a row each time a second fetch of a page yields different extracted text or no longer answers | **append-only** |
| `fetch_version_tombstones` | what the retention runner pruned: bytes only; the version row, hash and extracted text stay | **append-only** |
| `ingestion_requests` / `ingestion_request_records` | the on-request intake: one row per request to fetch an address, re-poll a source window or re-check a stored version, under a named source's pipeline and sink, with who asked and why; and which records the request's run wrote. States `queued → claimed → done / failed`, `expired`, or refused at submit. See [operations/on-request-fetching.md](operations/on-request-fetching.md) | requests **append** plus **state mutate-in-place**; records **append-only** |
| `source_credibility` | the per-host scored track record the signal write path resolves into `signals.source_credibility`; also the per-host licence ledger | **mutate-in-place**, through the registry route (an admin act, `written_by`) |
| `evidence_archive` | the archival outcome per signal; the bytes live content-addressed on a filesystem volume, not in Postgres, and nothing deletes a cited object (the off-by-default `archive_retention` sweep removes only the bytes of uncited rows, keeping the row as `expired_uncited`) | **upsert sidecar**; no foreign key on purpose, so archived evidence outlives a signal purge |
| `evidence_archive_bundles` / `evidence_archive_cold` | the cold tier: one row per uploaded, read-back-verified bundle, and one row per content address that has a cold copy (bundle plus byte range). `hot_removed_at` is stamped only after the bundle verified | **append-only**; nothing deletes from either |
| `corpus_tombstones` | the search corpus's delete queue: the deleted `signals.id` is the corpus document id. Rows are never removed; the drain stamp keeps every dropped id queryable | **append plus drain-stamp**, written in the same transaction as its delete |
| `iso_countries` | the country reference table the geo vocabularies resolve against | **static reference** |
| `source_layers` / `desk_apertures` | the source-layer map loaded from its file: which layer each source sits in for a subject, and what each desk declares it can see ([DATA_SOURCES.md](DATA_SOURCES.md)) | **derived** from the map file by its loader |

### Origins and origin groups

An **origin** is one place material comes from: a source descriptor (`source:<id>`), a knowledge set
(`knowledge_set:<set>`), or a host on the licence ledger reached outside any descriptor
(`ledger_host:<host>`). Every origin belongs to one **origin group**, the unit that independence is
meant to count: two feeds of one publisher, or two channels of one claimant, are one group.

| Table | Purpose | Write semantics |
|---|---|---|
| `origin_groups` | one group per independent origin: `host:<registrable domain>`, `claimant:<name>`, `telegram:<channel>`, `set:<knowledge set>`, `descriptor:<id>`, or a group an admin named | **append**; `written_by` names the derivation rule or the admin |
| `origins` | one row per origin, with its group, the `rule` that derived the assignment and its detail, and `corrected` | **mutate-in-place** group pointer; a corrected row is never re-derived |
| `origin_assignment_log` | every assignment, derived or corrected, with its principal and reason | **append-only, trigger-enforced** |

The first assignment is **derived** by `src/legba/data/origins.py`, rule by rule, and the rule is
kept on the row so every grouping can be argued with: a claim source's declared claimant; a single
Telegram channel; a Telegram bundle (one group for the bundle, which under-counts until an admin
splits it); a feed reached through an in-stack proxy (grouped by the publisher the route names); the
registrable domain of the configured address; a licence-ledger host; a knowledge set; and, when none
applies, the descriptor on its own. An **item** resolves finer than its source: a Telegram item is its channel (`telegram:<channel>`), an item of an aggregator such as GDELT is the publisher domain of its article (`host:<domain>`), and anything else is its source's group. A deployment's **owner map** (`seeds/owner_map.yaml`, git-ignored; format in `seeds/owner_map.example.yaml`) joins what a domain cannot, such as one publisher on two domains. `scripts/derive_owner_groups.py` re-derives all of it: a dry-run by default, `--apply` to write, idempotent, and an admin-corrected origin is never moved. An admin may move an origin to another group
(`PUT /api/v1/registry/origins/{origin_id}/group`); `GET /api/v1/registry/origins` and
`…/origins/{origin_id}/log` read them. `scripts/report_origin_groups.py --list` names the known wrong
cases (uncommon public suffixes, one publisher on two domains, proxied and direct feeds of one
publisher).

**Independence counting does not use origin groups yet.** Every count of independent sources still
counts source descriptors; `origin_groups_for_sources` is the read a counter would use, and no live
path calls it.

## Knowledge plane

| Table | Purpose | Write semantics |
|---|---|---|
| `facts` | atomic `(subject, predicate, value)` assertions with temporal validity and a source tier. Carries `origin_class` + `collection_id` and `superseded_at`, the decision-time close stamp (backfilled for older rows as the successor's `created_at`, an approximation) | **supersession-versioned** over an open-only unique index. Supersession is single-winner-by-recency *within a source tier*, so a machine-extracted fact cannot close a human-curated one; agreeing sources combine confidence by a bounded noisy-OR rather than a maximum. The tiers: `seed` = `curated` = `operator` (2) above `ingestion` = `agent` and anything else (1). An `operator`-tier row is a person's own entry written through a Consult write (`assert_fact`): `origin_class` stays `live`, confidence 1.0 unless given, and it grounds like curated. It supersedes nothing by rule: the write closes exactly the rows its `supersede` argument names (`none`; `differing`, the other open values except seed and curated rows; or a list of ids), records those ids as its pre-image, and its revert reopens exactly them. A plain close (`close_fact`) stamps `valid_until` and `data.closed_by` / `data.closed_reason` |
| `nexuses` | reified, typed, signed relationships between entities | **supersession-versioned**; a polarity or label change closes the prior row |
| `entity_profiles` | one node per distinct resolved entity, deduplicated on a canonical surface | **mutate-in-place** plus an append-only history in `entity_profile_versions`. A person-directed write (the Consult write plane's entity class, or an accepted journal `merge_entities`) bumps `version` and appends a version row whose `data.provenance` carries the actor (`consult_write:<id>` / `journal_proposal:<id>`), role, accepting principal, time and reason. A write-plane merge records its pre-image on the loser and defers edge folding to compaction, so an unmerge is exact until `entity_gc` compacts the loser ([SEAMS #68](SEAMS.md)) |
| `entity_alias_keys` | the indexed lookup keys (lower-cased alias, normalised name) the entity resolver probes, one row per distinct key and entity | **derived / recomputable** |
| `entity_aliases` | provider naming to entity ids, scoped per collection, so a provider quirk can never reshape the live graph | **append-only**; no writer fills it yet |
| `entity_edges` / `entity_edges_unresolved` | the id-keyed entity-to-entity edge store: endpoints are profile ids behind real foreign keys, never names, so a merge repoints rather than strands. An endpoint that cannot be resolved is parked with a reason rather than dropped or guessed | **append-only** on the park table; edges supersession-versioned |
| `entity_edge_events` | the transition ledger for edge mints and polarity flips; behind `LEGBA_EDGE_TRANSITION_LEDGER` (off by default). Transitions before it was written are not recoverable | **append-only** |
| `entity_judgement` | one adjudication verdict per candidate entity pair: an audit row and a re-adjudication cache | **append-only** |
| `proposed_edges` | pairwise co-occurrence candidates accruing confidence, the reifier's input queue | **mutate-in-place** (status plus confidence; no version chain) |
| `proposed_edge_markers` | one row per candidate pair that ended rejected or retired (its lowercased key, final state, when, confidence); the pair is never proposed again | **insert-only in practice** (a trigger on `proposed_edges` writes it; removed only if the same row leaves a final state) |
| `signal_entity_links` | the signal-to-entity provenance edge | **append-only** |
| `graph_metrics` | signed-triad balance, centrality and community, proxy-chain sign products, as queryable rows | **append-only** |
| `seed_batches` | the curated-import ledger; facts and nexuses carry its id | **append-only** |
| `fact_contention` / `fact_contention_values` | the contested-claim sidecar: one group per `(subject, predicate)`, one row per distinct value cluster with its support and deterministic score | **derived / recomputable**: the arbiter upserts them each pass and sets three thin markers on `facts`, but never mutates a fact |
| `fact_contention_tiebreak` | one cached tie-break verdict per `(contention_id, evidence_fingerprint)` | **append-only**; only genuine verdicts cache |
| `fact_decay_states` | the per-fact decay readout | **derived / recomputable**; it never mutates a fact's confidence |
| `observations` | numeric series points, bitemporal (`valid_from`/`valid_to` is the period the number is about, `record_time` when the provider published or revised it), partitioned by year on `valid_from`. History rows come from a collection; live rows from a source's observation sink ([Collections and observations](#collections-and-observations)) | **append-only**; live rows trigger-enforced, a revision is a new row |
| `collection_loads` | one row per (collection, manifest version): resume key, counts, timestamps, status | **mutate-in-place** while a load runs |

## Analysis plane

| Table | Purpose | Write semantics |
|---|---|---|
| `analyst_outputs` | the typed-output table, routed by kind across the `OutputKind` vocabulary: findings, meta-findings, alerts, critiques, prompt-module candidates, scorecards, situation updates | **append-only**; a validation failure routes to `output_dead_letter` rather than landing a malformed row |
| `analyst_traces` | one row per run (output, side-write, trace-only or failure alike) with the run's steps, tool calls and prompt hash, hash-chained per analyst | **append-only**; an old run's prompt, input and output may be moved into `analyst_trace_bundles` (the only update) |
| `analyst_trace_bundles` | the prompt, input and output of up to N old runs of one analyst, xz-compressed together; read only through `trace_bulk.hydrate_traces` | **insert-only** |
| `analyst_outputs` rows with `kind = 'critique'` | every faithfulness verdict, one row per verified output, `confidence` carrying the score and the judge model and pipeline revision in `data`. The older `analyst_critiques` table remains, written only by a legacy judge path in `runtime/actor_payload.py` | **append-only** |
| `hypotheses` | competing-hypothesis pairs with their evidence matrix, and, in the same shape, the standing open-question set | **append** plus **status mutate-in-place** |
| `inquiry_ledger` | an inquiry's continuity: its hypotheses (each with a resolution test frozen at write), questions, observations and expectations, with status `open` / `confirmed` / `refuted` / `answered` / `expired` / `withdrawn`. The `inquiry_state` action pack is its only read and write surface | **append** plus **status mutate-in-place**; `written_by` |
| `situations` | durable temporal frames keyed by a situation signature | **temporal-frame**: `valid_from` / `valid_until` / `superseded_by`, closed when the frame closes |
| `situation_events` | the situation trajectory ledger: one row per movement, on an evidence-time clock | **append-only, trigger-enforced** (delete and update barred); a movement without new evidence is unrepresentable by a check constraint |
| `finding_supersessions` | the near-duplicate finding link, so a newer finding supersedes a prior one without a delete | **append-only** |
| `journal_entries` | the off-chain reflective voice: entries, chronicles, lens reads, and the consolidation tier | `entry`-family rows **append-only**; `consolidation` rows **supersession-versioned**, with a partial-unique index guaranteeing at most one open consolidation |
| `journal_proposals` | the human-gated review queue for anything the journal wants to change outward | **append** plus **status mutate-in-place**; never a live table |
| `consult_sessions` / `consult_turns` | the Consult audit trail: one session header per conversation (its `principal` the server-stamped actor), append-only turns carrying the reasoning steps, tool calls and cited references | header **mutate-in-place**, turns **append-only** |
| `consult_writes` | one row per change a person directed through Consult (a source paused, a cadence moved, a fact asserted, an entity merged), whether executed, staged for confirmation, refused, failed, cancelled or expired, with its stored arguments and pre- and post-image so it can be replayed or reverted. No foreign keys, so no cascade deletes it | **append** plus status; `written_by` |
| `scheduled_events` | the register of known future dated things (summits, votes, deadlines) every Consult run gets in its context; ships empty, filled by a person or a seed | **append** plus soft retire (`retired_at`); `written_by` |
| `output_consumption` | the **forward** index: what now rests on this row, distinguishing a basis input from a hedged periphery one | **append**; no foreign key on purpose, because consumers span both output tables. The writer never raises |
| `review_flags` | one open flag per (product, foundation) pair whose foundation has since moved | **append plus close-by-supersession**; a trigger makes deletion a database error |
| `bearing_edges` | dated, typed, weighted "this later thing bears on that earlier question" pointers, stamped with the matcher version | **append-only by writer discipline** |
| `collection_requirements` | a durable, reviewable statement that something could not be seen | **append** (idempotent on a natural key) plus **disposition mutate-in-place**; the content columns are write-once |
| `indicator_flips` | every indicator status flip the `indicator_tracker` observed: `raw` (between two consecutive runs) or `debounced` (a change of the held status), with the onset run, confirming run and the raw toggles it took to settle | **append-only by writer discipline**, idempotent on its evidence key |
| `decision_shadow_ledger` | one row per decision a shadow model was asked beside the model that decided (today the `claim_watch` bearing gate), with both answers, confidence and latency or an error class, and a hash proving both judged one text. Read only by `scripts/decision_shadow_report.py`; nothing decides on it | **append-only by writer discipline**; an answer or an error per row (CHECK) |
| `desk_rows_shadow` | one row per desk run that the row-shaped output contract was also run beside, in shadow: the rows, their gate outcomes, the rendered read and counts. Written only when `LEGBA_DESK_ROWS_SHADOW` names a desk (off by default). The held-record feed is its one reader | **append-only** |
| `alert_trigger_watermarks` | durable "this transition already fired" state per trigger class, plus change-detector cursors | **mutate-in-place upsert**; a watermark advances only after the alert lands |
| `alerts` / `alert_events` | the one alert record ([ALERTING.md](ALERTING.md)): one row per episode (a kind about a subject, at most one open per dedupe key), and one row per raise with its paging decision and reason. The current acknowledgement and snooze ride on `alerts` | episodes **mutate-in-place** (occurrences, close, ack, snooze); raises **append-only** |
| `alert_episode_actions` | every acknowledge, un-acknowledge, snooze and un-snooze, with the person and role | **append-only** |
| `alert_sink_deliveries` | one row per delivery attempt; also the state that stops a restart re-firing a standing alert | **append-only** |
| `watchlist` | a person's standing watches over an entity, a text pattern or a place | **mutate-in-place**; delete is soft; `written_by` |
| `desk_baselines` | each desk's own statistical activity prior, with a Poisson-floored sigma. Not a forecast | **derived / recomputable** |
| `narratives` / `narrative_echo_edges` | a contested-claim family reified, and the directed source-echo graph over it. Descriptive, not causal | **derived / recomputable**, wholesale-refreshed |
| `output_dead_letter` | rows that failed validation on the write path | **append** plus resolution |

## Citations as rows

Since migration `20261115T1828_citations_as_rows`, a new finding's citations are also kept as rows,
written beside the stored citations (`analyst_outputs.data->'data'->'citations'` and the `[N]`
markers in its prose) in the same transaction, by `src/legba/data/citation_rows_store.py`.
"What does this sentence rest on, and which stored bytes was it read from" becomes a join instead of
a walk through JSON.

| Table | What it holds |
|---|---|
| `finding_sentences` | one row per sentence of a finding, in order |
| `citation_support` | one row per "this sentence rests on that record": a typed key column per target kind, the `fetch_versions` row the material was read from, and copies made at write time so a later relabel never rewrites history |
| `citation_unresolved` | a marker or citation entry that names nothing, with the reason. Logged, not refused: refusing is `LEGBA_CITATION_REFUSE_UNRESOLVED`, off |
| `run_handed` | what the run was handed (its `[N]` index), typed by kind, so "cited something it was never shown" is a query |
| `finding_citation_receipts` | one row per finding with the counts of the write; its presence makes the write and the backfill idempotent |

The kinds a citation may name are the `citation_target` family in the type registry (signal,
observation, finding, event, situation, hypothesis, inquiry, desk baseline); the writer refuses an
undeclared kind. All five tables are **insert-only by trigger**: every update is refused, and a
delete is allowed only for a backfill batch named in the session setting
`legba.citation_rows_rollback`; live rows can never be deleted that way. There are no foreign keys to
the cited rows (they can be removed by retention or test teardown); the real foreign keys are to
`fetch_versions` and `principals`. Every row names its writer in `written_by`: the finding's seat
(`agent:<id>@<version>`) or handler (`rule:<id>`), or `rule:citation_rows_backfill` for a backfilled
row. Older findings are filled by `citation_rows_backfill`; `scripts/citation_rows_report.py` counts
dangling targets. The Record sheet's support route (`GET /api/v1/v3/record/finding/{id}/support`)
reads them and falls back to the stored citations for findings that have none.

## The held record

Migration `20261115T1940_held_record` adds the tables in which the record holds what it has
established, over time, as opposed to what one run wrote. In the database the family is
`assertion`; on screen the checked kind is called a "statement". A statement becomes a "fact" only
by a person's visible act, which is not built.

| Table | What it holds | Write rule |
|---|---|---|
| `assertions` | identity: class (`statement`, `attributed`, `assessment`, `absence`; a closed list in a CHECK), type (an `assertion_type` declaration in the type registry), `basis` (`derived`; `declared` for entered knowledge; `extracted` reserved), the subject (an entity id when it is one, else a target id, place code or surface name, with the text kept), the speaker of an attributed record, and the claim key | insert-only |
| `assertion_versions` | what it says, for which period (`valid_from` / `valid_to`; an absence's window), its slots, and the shadow record, run and proposing seat it came from | insert-only: a revision is a new version |
| `assertion_status_log` | every status (`held`, `not_held`, `contested`, `superseded`, `withdrawn`), its reason, the verifier's verdict and the principal that caused it (`caused_by`) | append-only; `seq`, `prev_hash`, `recorded_at` and `row_hash` set by the database, hash-chained per assertion |
| `assertion_status` | the current status per assertion | the one updatable table: a cache kept by a trigger on the log, rebuildable from it (`scripts/held_record_verify.py` proves rebuild equals cache) |
| `assertion_support` | what a version rests on: exactly one claimant (a version, or a desk shadow row for a restatement) and exactly one typed target (signal, observation, finding sentence, finding, another version, a verdict), with the quote and the receipt version pinned | insert-only; a derived record is never evidence for another |
| `assertion_shadow_receipts` | one row per desk shadow record taken in, with its counts | insert-only; the idempotency key |

The rules (`src/legba/data/held_record.py`): a fact row the gates passed is held only on a
"supports" verdict, and is otherwise written not held, with the verdict; an assessment is held only
when every premise is a held statement; an absence is held, scoped to its sources and window; a
restatement is a link, never a new record. One assertion per claim key (the type, the normalised
subject, and the type's declared key); a match is a restatement link, or for an absence in a later
window a new version, never a duplicate.

**What feeds it and who sees it.** The only feed is the desk shadow records in `desk_rows_shadow`,
for the desks named in `LEGBA_HELD_RECORD_SHADOW` (empty, so off, by default; a deployment may name
one desk to try it). The only readers are `GET /api/v1/v3/record/assertions[/{id}]` and the verify
script; no console surface shows it. Promotion, contradiction detection, expiry, redaction and
entered knowledge are declared and not built: [SEAMS.md](SEAMS.md), row `held-record`.

## Measurement plane

| Table | Purpose | Write semantics |
|---|---|---|
| `unit_references` | one independent reference per (desk, window): the developments the grader grades against, built by the reference builder and never by the grader | **append-only** on a content hash |
| `unit_correctness` / `unit_correctness_claims` | one unit's correctness against its reference at one as-of stamp, and the per-claim ledger beneath it | **append-only** |
| `grader_calibrations` | one calibration run. The grader refuses to publish a number unless a passing row exists whose rubric hash matches and whose model set covers every model the run will use | **append-only**; `method_version` stamps the gate revision |
| `external_grades` | the database home for external truth: one graded claim per grader family and pipeline version | **append-only** |
| `correctness_labels` / `goldset_week_samples` | the weekly human gold set: the closed-vocabulary verdict per finding, and the pinned weekly sample | labels **upsert** (one verdict per finding; `written_by`); samples **append-only**, so the first read pins the week |
| `unit_reference_labels` | the secondary diagnostic correctness axis, never pooled with the human one | **append-only**; `written_by` |
| `band_calibration_claims` / `band_calibration_scan_state` | every scorecard band transition logged as a resolvable claim, graded at fixed horizons. No probability column by design: bands are not probabilities | claims **append, never overwritten**; each horizon's outcome stamped once; `method_version` stamps the harness revision |
| `acute_forecasts` | the platform's own binary hazard calls, kept out of the findings feed | **append** at issue plus **resolution mutate-in-place**, graded when the forward window closes; `resolution_test` freezes the falsifiable contract at mint; a due-but-unresolved row is `resolved_by='unresolved:expired'` and stays in the denominator |
| `forecast_questions` | the exogenous forecast ledger ([LEDGER.md](LEDGER.md)): one row per question, with its resolver kind and spec, window, due date, void rules and `sealed_at`. `status` (`open` / `resolved` / `void`) is the one mutable column once sealed | **append** plus **status mutate-in-place**; `written_by` |
| `forecast_numbers` | one probability per (question, issuer kind, issuer ref), clamped to 0.01–0.99, or NULL with an `absent_reason` (an issuer with no number is recorded as absent, never imputed) | **append**, sealed; `written_by` |
| `forecast_resolutions` / `override_log` | the outcome (`yes` / `no` / `void`), one active row per question; a correction is a new row plus an override row | **append** plus `active` flip |
| `forecast_resolution_records` | every act on a resolution: proposal, confirmation, rejection, override, correction, each naming the row it answers, with evidence and the acting role | **append-only, trigger-enforced** |
| `forecast_resolution_proposals` / `scorecard_entries` | declared by the forecast ledger migration; no writer in the tree | — |
| `eval_runs` / `eval_run_arms` | the published evaluation record the Ledger page shows: one row per published version of a run and its per-arm aggregates. No per-question text is stored | **append**; a correction is a new version; withdrawal is the one change a row takes |
| `evidence_drift_classifications` | one classification per `fetch_drift_observations` row: what kind of change a page underwent, from a closed list | **append-only, trigger-enforced** |
| `evidence_drift_flags` | one flag per dependant (a record built from the changed version, a finding or entry citing it) when the change is substantive, with a lifecycle `open → reviewed → resolved` | identity columns write-once; lifecycle **mutate-in-place**; never deleted |
| `evidence_drift_assessments` / `evidence_drift_claim_effects` | what kind of change a classified page change was (set by code; a model writes only the summary), and per flag whether the cited text survives | **append-only** |
| `evidence_drift_auto_closes` / `evidence_drift_would_auto_close` / `evidence_rederive_requests` | flags closed by rule, the closes a rule would have made, and "re-derive this with the changed text" requests | **append** |
| `receipt_anchors` | one OpenTimestamps attestation per (day, calendar): the Merkle root over the day's trace chain heads and forecast seals, the calendar's proof bytes, status | **append-only**; a `pending` row upgrades in place when its calendar answers |
| `receipt_anchor_trees` / `receipt_anchor_leaves` | each anchored tree's ordered leaves, so an inclusion proof can be served for any leaf of a stored day (days anchored before these tables have none) | **append-only** |
| `read_events` | the reading instrument: opens and drills against the published surfaces | **append-only**; `written_by` |
| `source_ratings` / `source_dossiers` | the asserted source-assurance layer: multi-rater grades and one current cited dossier per source | **supersession-versioned** |
| `source_track_records` | the **earned** per-source record over resolved contentions, smoothed and lower-bounded, computed at a lag | **derived / recomputable** |
| `source_quality` | a **view** joining the asserted, earned and computed legs; every non-key column is prefixed `asserted_` / `earned_` / `computed_`, and no composite score column exists | **none; a view owns no state** |
| `forecast_scores` | a **view** over the forecast ledger | **none** |

## Events, temporal and graph

This section covers four related pieces: the **event** tables, the two **time** axes and the
history firewall, the **collections** that hold the past, and the **projections** (`graph_arcs`,
`profile_projection`). The event write path and the graph projection each ship behind a flag that is
off by default ([flags](#event-temporal-and-graph-flags)).

### Events

An **event** is one real-world occurrence, evidenced by one or more signals, with a time span, a
place, actors in roles, a category and a lifecycle. A **situation** is not the same thing: a
situation is a persistent *frame* the system watches, keyed by `sig:<topic>#dim:<dimension>`, whose
members are findings; an event is a bounded *occurrence* whose members are signals. One situation
tracks many events; one event may be tracked by more than one situation.

| Table | Purpose | Write semantics |
|---|---|---|
| `events` | one row per (`event_signature`, `analyst_id`), re-materialised in place. Title, `summary` (never evidence; see below), `category`, `event_type` (`incident` / `development` / `shift` / `threshold` / `unclassified`), `severity`, `lifecycle_state`, `time_start` / `time_end`, `geo` (ISO2 array) / `geo_lat` / `geo_lon` / `locations`, `confidence`, `signal_count` and `distinct_source_count` (counted from the link table inside each write), `oversized`, the supersession columns, `source_method` (`clustering` / `tower` / `manual`), `origin_class`, and the standard provenance envelope | **upsert** on the signature key plus **supersession-versioned** close |
| `signal_event_links` | which signals evidence which event, with `relevance`, the evidence time `linked_at` (the signal's `fetched_at`), and three source axes denormalised at link time: `source_class` (editorial authority: `reporting`, `analysis`, `official`, `state_media`, `claim`), `source_kind` (the handler, for example `rss` or `telegram_channel`) and `source_id`. No foreign key on `signal_id`, so retention never cascade-deletes provenance | **append** (upsert on the pair) |
| `event_entity_links` | actors in roles (`actor`, `target`, `location`, `observer`, `victim`, `mediator`) | **append**; cascades with the entity, folded on an entity merge |
| `event_edges` | typed event-to-event edges: `part_of` (src is a sub-event of dst), `caused_by` (src was caused by dst), `evolves_from` (src succeeds dst), and the symmetric `correlated_with` and `contradicts`, stored with `src < dst` by a CHECK | **supersession-versioned**; one open edge per (src, dst, type) |
| `situation_event_links` | which situations track which events | **append** |
| `event_lifecycle_events` | the lifecycle ledger: one row per transition, on evidence time (`occurred_at`) with decision time in `created_at`. Every transition but `resolved` must carry evidence (a CHECK) | **append-only, trigger-enforced** |

**Lifecycle.** Five states and a pure transition function in `src/legba/data/events/lifecycle.py`,
with a database CHECK mirroring the vocabulary:

| from | to | trigger | ledger `transition` |
|---|---|---|---|
| *(none)* | `emerging` | promotion | `opened` |
| `emerging` | `developing` | `signal_count >= 3` | `advanced` |
| `developing` | `active` | `signal_count >= 5` and `confidence >= 0.6` | `advanced` |
| `active` | `evolving` | the link rate over the trailing 24 h is at least twice the trailing 7-day baseline, or a new actor or country attaches | `accelerated` |
| `evolving` | `active` | the rate is back inside 0.5 to 2 times the baseline | `stabilised` |
| `emerging` | `resolved` | no new link in 48 h | `resolved` |
| `developing` | `resolved` | no new link in 72 h | `resolved` |
| `active`, `evolving` | `resolved` | no new link in 7 days | `resolved` |
| `resolved` | `developing` | a new signal links | `reactivated` |

Every clock runs on evidence time, never run time, so a backlog drain cannot resolve a live event and
a re-ingest cannot reactivate a dead one. `reactivated` is a transition, not a state. Silence writes
nothing: a resolved event that stays quiet writes no heartbeat rows.

**Candidates and promotion.** The `event_clustering` handler matches enriched signals by
single linkage on four weighted features (entity overlap 0.30, title similarity 0.30, temporal
proximity within 24 h 0.20, category match 0.20; threshold 0.50), with embedding similarity and shared
fact subjects as separate gates (`_event_matcher.py`). A cluster becomes an event only when it has at
least two distinct sources, or one `official` source; claim-class members never count toward the
two-source bar, and a cluster evidenced only by claims is not promoted. A cluster that reaches
30 members is stamped `oversized`, is not promoted, and is counted on the run receipt: hitting the
cap is evidence the threshold is wrong. The tower path promotes a verified finding that cites at
least two resolvable signals, or a `situation_events` movement that carries evidence. A candidate
that misses its bar is not written and not queued. A clustering event and a tower event about one
occurrence are two rows (two producers' readings), joined by a `correlated_with` edge from
`event_reconciler`.

**The signature** is minted from the promoted cluster, not used to form it:
`evt:<topic>|<top-K identity-folded entity tokens>#evt:<anchor token of the primary polity>`
(`src/legba/data/events/signature.py`).

**Citing an event.** Behind `LEGBA_EVENT_CITATIONS` (off by default), a finding may cite
`event:<uuid>`. The citation builder (`provenance/event_citations.py`, and
`analysts/composition_event_citations.py` on the composition path) expands the token through
`signal_event_links`, ranked by relevance then recency and capped by
`LEGBA_EVENT_EXPANSION_MAX_SIGNALS` (default 4, with `event_expansion_truncated` stamped on
overflow), into ordinary per-signal citation entries, each carrying the signal's own source text
under the same precedence and 3,200-character cap as every other citation. So:

- the faithfulness judge grounds on the signals; `verify.py` is unchanged;
- the event's own `summary` never reaches the judge or any evidence field;
- `event` is deliberately not one of `GROUNDING_REF_KINDS`: those are blocks with no `signal_id`
  whose rendered text *is* the evidence (for example an `observation` row), and admitting `event`
  would let an event be graded against its own summary;
- the finding's `derived_from` carries the event id and the expanded signal ids, and the lineage
  walk resolves both.

**What an event may not be used for.** An event never writes, closes or supersedes a fact; it is
never groundable evidence on its own; it is never admitted to a composition's basis or a scorecard
band's basis; and it is never deleted to remove evidence: an event whose signals no longer resolve is
closed with `data.closed_reason = 'evidence_purged'` and a ledger row.

### Time: two clocks, two parameter names

| axis | columns | the question | tables |
|---|---|---|---|
| **validity time** | `valid_from` / `valid_until` | what was true in the world on date D | `facts`, `nexuses`, `entity_edges`, `situations`, `events` |
| **decision time** | `produced_at` / `superseded_at` (`created_at` elsewhere) | what the platform believed on date D | `analyst_outputs`, `analyst_traces`, every receipt |

`as_of` always means validity time and `believed_as_of` always means decision time; they are never
the same parameter. The canonical as-of predicate is

```sql
    COALESCE(valid_from, '-infinity'::timestamptz) <= $as_of
AND (valid_until IS NULL OR valid_until > $as_of)
```

and it comes with two rules. An as-of read must not apply the open-row predicate
(`superseded_by IS NULL AND valid_until IS NULL`): a row superseded since was *the* answer on D.
And a NULL `valid_from` is over-included and counted: readers return `unbounded_start: N` beside
`count`, so a reader can see how much of an answer rests on a start date nobody recorded.

The substrate query tools (`runtime/substrate_query_port.py`) take these parameters: `as_of` on
`query_facts`, `query_nexuses`, `query_paths`, `find_proxy_chains`, `query_brokers`,
`list_situations` and `query_events` (a path walk applies the predicate on every hop); `since` and
`until` on `get_timeline` and `query_events`; `believed_as_of` on `list_findings`. `belief_as_of`
(also `GET /api/v1/v3/belief`) returns what was published about a desk on D, folded against the
faithfulness verdict either as it stood on D (`fold_verdicts='as_of'`, the default, with
`verdict_pending_at_as_of` counted) or as it stands now (`'latest'`); the response says which, and the
two are never summed. All four parameters are ISO-8601 strings parsed at the boundary, refused when
malformed, never coerced to now. An absent parameter keeps the current-state read unchanged.

`entity_edges` with `entity_edge_events` is bitemporal; `facts`, `nexuses` and `situations` keep a
validity interval and a decision stamp, but a supersession rewrites the loser in place, so their
belief *history* is not reconstructable. An `as_of` answer on those tables is what held in the
world, not what was believed at D.

### Origin class and the history firewall

`signals`, `facts`, `events` and `observations` carry `origin_class`, where the row came from, in a
closed six-class vocabulary: `live`, `web_retrieval`, `seed` (present), and `backfill_native`,
`backfill_reconstructed`, `archive` (history). The open-row gate alone cannot tell an imported 2016
fact from one minted this hour, so "now" reads would absorb imported history into freshness, source
health, calibration, salience and alerts. The firewall is therefore the class:
`data/provenance/origin.py` renders the one live gate (`origin_class_clause()`, the open-row pair plus
the present classes) and the one validity predicate, and every open-row read on `facts` and `events`
and every reader on the eight fenced surfaces (cadence analysts, freshness, source health,
calibration, salience, alerts, reactive triggers, surge detection) takes its predicate from there.
The reactive trigger plane gates the delivered row instead (`runtime/triggers/coalescer.py`), since a
NATS message has no WHERE clause. `tests/data_pkg/test_origin_gate_inventory.py` pins every
`superseded_by IS NULL` site in `src/`, and `test_origin_firewall_surfaces.py` proves the gate against
a planted history burst.

Nothing may yet write a history row to `signals`, `facts` or `events`: the CHECK constraints named
`<table>_origin_class_history_writer_not_built` refuse it ([SEAMS #62](SEAMS.md)). Collection series
land in `observations` instead.

### Collections and observations

A *source* is a live feed; a *collection* is a bounded, versioned holding of the past, loaded once by
a person and never scheduled ([COLLECTIONS.md](COLLECTIONS.md)). It is its own descriptor family
(`collection_descriptors`, files `descriptors/collection_*.yaml`) with a four-state lifecycle
(`draft → reviewed → loaded → superseded`) and a `firewall:` block that must name all eight fenced
surfaces or the descriptor does not validate.

Series land in **`observations`**: bitemporal, so a 2016 figure revised in 2023 is two rows;
partitioned by range on `valid_from`, one partition per year plus a default; keyed on
`(collection_id, series_id, subject, valid_from, valid_to, record_time)`, which is what makes the
loader idempotent. No row exists for a period a provider does not hold, and a row without a value
cannot be stored. Two kinds of row share the table:

- a **history** row (`archive` or `backfill_*`) comes from the collection loader and carries no
  `source_id`;
- a **live** row comes from a source's observation sink and must carry its `source_id`, a
  `collection_id` of `live:<source_id>` (a value no collection id can match, so no history read can
  select it), the hash of the fetched response it was parsed from, a methodology version, its
  period granularity and how `record_time` was set. Live rows are append-only by trigger; an as-of
  read picks the latest `record_time` at or before the as-of.

`entity_aliases` (provider naming to entity ids, scoped per collection) and `collection_loads` (one
row per collection and manifest version) complete it. The manifest version is the hash of the
manifest, not of the descriptor, so re-approving a licence line does not invalidate a load.

### The cross-layer graph projection

The layers are joined by foreign keys, `derived_from uuid[]` arrays, text endpoints resolved through
`resolve_entity_name()`, and one polymorphic table (`bearing_edges`). `graph_arcs` names those joins
in one vocabulary and projects them into one table that is **derived, disposable and rebuilt whole**.
It is not authoritative: `entity_edges` stays the entity-to-entity store with real foreign keys.

Each arc has a `plane`: `world` (what the sources say about the world: entity edges, proxy channels,
event membership and event edges, fact endpoints, narrative echoes), `evidence` (mentions, duplicate
links, entity-merge tombstones) or `lineage` (who cited whom, supersessions, verdicts, bearing
pointers). The plane is NOT NULL and `world` is the default on every world-facing reader, because
most arcs are output lineage and a world query over "all arcs" would report who cited whom as the
state of the world. Left out on purpose: `proposed_edges` (a candidate queue; promoted rows arrive
through `entity_edges`), `journal_entries` (off-chain), and target routing (ephemeral).

| Table | Purpose |
|---|---|
| `graph_arcs` | one row per arc: from and to kind and id, `arc_type`, `plane`, `family`, `polarity`, `confidence`, validity, and the authoritative `src_table` / `src_id` it was read from. No foreign keys |
| `graph_arcs_meta` | the one-row build receipt: `projected_at`, `arc_count`, `source_counts`, `build_seconds`, and `checked_at`, `input_watermark`, `last_outcome` (`built` / `skipped_unchanged`), `excluded_sources`, `persistence` |

The contract, enforced in `src/legba/data/graph_projection.py` and the `graph_projector` handler:

1. **One direction.** Nothing reads `graph_arcs` to write a substrate row.
2. **Whole rebuild only.** The projector builds a staging table from the source tables and swaps it
   in inside one transaction; there is no incremental path. Every arc is a pure function of the
   source rows, so two builds over one snapshot are row-identical. A tick whose inputs have not
   changed (a watermark over the source tables' write counters, the projector version and options)
   records `checked_at` and skips.
3. **Unlogged.** The table is built `UNLOGGED`; after a database crash it comes back empty and readers
   answer `projection_rebuilding` until the next tick rebuilds it.
4. **Optional groups.** The citation-lineage copies and the pairwise `contends_with` arcs are off by
   default (descriptor options `include_lineage_copies`, `include_contention_arcs`): no graph reader
   traverses them, and the authoritative copies stay on the source rows.
5. **Staleness is published and fails loud.** Every read returns the receipt; readers refuse with
   `projection_disabled` (flag off), `projection_empty` (never built), `projection_rebuilding` or
   `projection_stale` (older than `LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS`, default 7,200 s, measured
   from the newer of `projected_at` and `checked_at`) rather than answering "nothing found".
6. **Kill switch.** `LEGBA_GRAPH_PROJECTION`, off by default: nothing is built and every reader says
   so. Removing the feature is dropping the table; no substrate row changes.

`GET /api/v1/v3/graph/arcs` is the bounded ego and walk read, world plane by default. While the flag
is on, `graph_mining` and `structural_balance` build their bounded networkx snapshots from
`graph_arcs WHERE plane='world'` (entity edges only), which sees every edge family rather than only
`nexuses`.

### The profile projection

`profile_projection` follows the same pattern for each profile's read-time vocabulary (flag
`LEGBA_PROFILE_PROJECTION`, off by default). One row per `(plane, section, key)`, where a plane is
one profile's projection and the sections are its vocabulary (`subject_groups`, `units`, `tiers`,
`labels`, `bands`, `scope`, `crosswalk`; [PROFILES.md](PROFILES.md)). The `profile_projector` analyst
rebuilds each declared plane whole, one transaction per plane, so two profiles on one substrate never
read each other's rows. The `(plane, '_meta', 'build')` row is the plane's receipt.
`GET /api/v1/registry/projection/{plane}[/{section}]` publishes the receipt and refuses by name:
`projection_disabled`, `projection_empty`, `projection_stale` (past
`LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS`, default 7,200 s), and 404 `plane_unknown` /
`section_undeclared`.

### Event, temporal and graph flags

| flag | default | gates |
|---|---|---|
| `LEGBA_EVENTS` | off | the event write path (clustering, lifecycle, ledger); while off, every event write refuses |
| `LEGBA_EVENT_CITATIONS` | off | the `event` citation expansion |
| `LEGBA_EVENT_EXPANSION_MAX_SIGNALS` | 4 | the per-citation expansion cap |
| `LEGBA_EDGE_TRANSITION_LEDGER` | off | the `entity_edge_events` write |
| `LEGBA_GRAPH_PROJECTION` | off | the projector and every `graph_arcs` reader |
| `LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS` | 7200 | the staleness refusal |
| `LEGBA_PROFILE_PROJECTION` | off | the profile projector and its readers |
| `emit_graph_edges` (fact extractor option) | false | retired: `true` is refused at validation, because the graph database it wrote to was removed |

No flag gates the `as_of` parameters: their absence is the off state.

### `claim_contentions`: the contrary-evidence record

One row per material claim the contrary-evidence pass contended: what was asked, on which rung, what
was fetched, and what the retrieval came to. It holds a **retrieval outcome and never a verdict**
(ANALYSIS §10.6.1).

| column | type | what it is |
|---|---|---|
| `claim_id` | `text` | sha256 over the folded claim text and its byte-exact origin span; joins `external_grades.claim_key`. Stable across a replay |
| `claim_text` | `text` | the claim as published |
| `finding_id` · `block_ordinal` | `uuid` · `int` | the read the claim was published in, and its ordinal inside that read |
| `origin_head_id` · `span_role` | `uuid` · `text` | the desk head the span was quoted from, and its role |
| `query` · `query_source` · `query_novel_tokens` | `text` · `text` · `int` | the counter-query, whether code or a model proposed it, and how many content words it carries that the claim does not |
| `polarity_group` · `polarity_sign` | `text` · `smallint` | the position the claim took in the calibrated vocabulary, when it took one |
| `rung` | `text` | the rung that answered |
| `stance` | `text` | `contradicts` · `qualifies` · `none_found` · `search_failed`. "looked and found nothing" and "could not look" never share a value |
| `derivation` | `text` | `polarity` (calibrated; the only one a composition reads) · `negation` (uncalibrated; human surfaces only) · `none` |
| `reason` | `text` | why a retrieval did not happen, from a closed set |
| `statement` | `text` | the hedged line a composition renders |
| `refs` | `jsonb` | the pages actually fetched: url, sha256, length, the date the page states, extraction result, fetch time, status, its own stance and the quoted sentence. A search snippet never becomes a ref |
| `ref_signal_ids` | `uuid[]` | corpus rows whose content hash already matched a fetched page. The pass links and never creates: its pages are chosen adversarially, and landing them in the corpus would skew freshness, source health, salience and calibration |
| `as_of` · `retrieved_at` · `expires_at` | `timestamptz` | the moment the row speaks about, when the pages were fetched, and when the next pass would cover the claim |
| `receipt_id` | `uuid` | `analyst_traces.run_id` of the pass that wrote the row |
| `host_class` | `text` | what kind of host the decisive counter page came from: `reference` (encyclopedia, dictionary or glossary, which can never carry `contradicts`), a registered source class, or `unknown` |
| `page_published_at` | `date` | the date parsed from the page's machine-readable metadata or a dated URL; NULL on a stance-bearing row means the page was undated, and an undated page cannot contradict |
| `subject_overlap` | `int` | how many of the claim's subject tokens the matched sentence carried |
| `independent_pages` | `int` | how many independent outlets carried an admissible contradiction; `contradicts` needs at least two, one is `qualifies` |

The last four columns are nullable and never backfilled: NULL on an older row means *not measured*,
not zero, and the pipeline stamp moved with the rules so fenced and unfenced rows are never pooled.
CHECKs carry the rules: a stance that names evidence must carry `refs`; only a stance-bearing row has
a derivation; `host_class` stays in its vocabulary; the fence counts are non-negative; and a
`contradicts` row names at least two independent pages (the last three are `NOT VALID`, checking
every new insert without rewriting older rows). The replay key is
`(claim_id, pipeline_version, as_of_day)`, so a re-run inside a day is a no-op. A partial index on
`(claim_id, as_of DESC) WHERE stance = 'contradicts' AND derivation = 'polarity'` serves the
composition's per-cycle question; `GET /api/v1/v3/contentions` is the reader route.

## Classifications — `data.classifications`

A tag is a classification only when it names a registered vocabulary family and a value of it:
`<family>:<value>`. The one rule is `data/tags.py:bind_tag`, run against the vocabulary as registered
(the runtime holds it in a 300 s cache read from `GET /api/v1/registry/vocabulary`):

| status | when | stored |
|---|---|---|
| `bound` | the family is loaded and the value (or an alias of it) is a current value | `{family, value, status}`, `value` canonical |
| `unclassified` | a family-shaped prefix whose family is not loaded, or a value the family does not have | `{family, value, status, raw}`, the raw string kept verbatim |
| `legacy_src` | a bare value with no family prefix | `{family: "src", value, status}`: the reserved `src` namespace means provenance, not classification |

A prefix is a family only when it is family-shaped: `src`, a platform family, or a namespaced
`<profile>.<axis>`; `News: World` and `severity:high` are bare values.

Every substrate row carries the result in `data.classifications` (a list, in input order,
deduplicated):

| table | written by | what is bound |
|---|---|---|
| `facts` | `fact_extractor._insert_ingestion_fact` (ingest) and `provenance.writes._insert_fact` (analysts and a person's Consult write through `insert_operator_fact`; an unregistered predicate binds `unclassified` or `src:` and never blocks the write), on insert | `predicate` as written, plus any `data.tags` |
| `situations` | `situation_clustering._upsert_situation` and `provenance.writes._insert_situation`, on every upsert | the bound `category`, plus any `data.tags` |
| `events` | `provenance.writes._insert_event`, on every upsert | the bound `category`, plus any `data.tags` |
| `entity_profiles` | `entity_resolution._resolve_batch`, on insert | `entity_class:<entity_class>` |
| `signals` | on insert: `source_actor.write_canonical_signal`, through `data/source_classification.py:signal_data`; existing rows: `scripts/reclassify_tags.py --apply` (batches of 1,000 on the primary key, with a resume cursor) | on insert, the source's declared classification (`scope.classifications`, then its legacy `scope.tags` as `src:`) followed by the signal's own namespaced tags; a bare publisher category is not stamped; nothing to stamp leaves `data IS NULL`. The reclassify apply writes the union of the row's existing entries and every raw `signals.tags` value bound |

**One shape.** Both `signals` stampers write `[{family, value, status[, raw]}]`, one entry per
`(family, value)`, first occurrence kept. The reclassify apply keeps the list's order: an existing
entry that some raw tag also yields is replaced by that tag's current binding; an existing entry no
raw tag yields (the source's `src:` scope entries) is kept verbatim; every remaining raw-tag binding
follows. A second apply over its own output is a no-op.

Rows written before classification binding carry no `classifications` key;
`scripts/retrofit_case_columns.py` stamps them on `situations`, `events` and `entity_profiles`.
`signals.tags` itself is never rewritten. `has_tag()` compares a signal tag under its namespaced key
(`src:<tag>` for a bare one), so a publisher category cannot satisfy a target-tag predicate.

### Source-declared classification

A source descriptor declares what its stream is: `scope.classifications: [<family>:<value>]`
(registered families only, never bare, never `src`). The registry refuses at registration a family it
has not loaded, then an unknown value of a loaded one. An empty list is left out of the body, so no
existing descriptor's content hash moves.

Every `SourceContext` carries the binding as `classification`, built at its four construction sites:
the poll and push actor, the discovery probe, the manual backfill and the auto-pause re-probe. At
ingest the per-source baseline puts the declared namespaced keys first on the signal's own `tags`, so
the 64-tag cap never drops them, and the canonical write stamps `data.classifications`. A slice
declaring the key (`scope.signal_tags`) therefore reads the source's signals with no further wiring.

**Family-shaped tags through ingest.** The ingest coercion (`sources/baseline._coerce_tag`) keeps a
payload tag of the form `<family>:<value>` with a family-shaped prefix verbatim, value case included,
and flattens every other value (`World News` → `world_news`, `News: World` → `news__world`). The
source schema's `Tag` is one pattern for both shapes (`source_classification.TAG_PATTERN`, at most
64 characters); a target's `scope.tags` uses the same type.

### The case-bearing columns

Four columns carry a classification, and each resolves to a registered value or to the literal
`unclassified`, bound at write time by `data/case_columns.py`:

| column | registered family | outside it | the CHECK |
|---|---|---|---|
| `situations.category`, `events.category` | a registered classification written as `<family>:<value>`: a namespaced value that binds, or a bare value that binds as a `salience_class`; a registered `data.event_class` on the row is used when the value is not one | `unclassified` | none |
| `events.event_type` | `event_type` (seeded from `descriptors/vocabulary_event_type.yaml`: `incident`, `development`, `shift`, `threshold`) | `unclassified` | `events_event_type_check` = the four plus `unclassified`; the family can narrow the column, never widen it |
| `entity_profiles.entity_type` | `entity_class` | `unclassified`, never the catch-all `entity` | none |

**A subject is not a category.** A category value that is the id of a registered subject (a
`target_descriptors` row) moves to `data.subject_id` and the column reads `unclassified`. Whatever the
column no longer holds is kept verbatim in `data.category_raw`, so nothing is dropped.

**The topic key.** Other readers depend on the producer's value: the tower's situation leg signs events from
it, and the event matcher compares it. Those readers read `case_columns.topic_key_sql`,
`COALESCE(data->>'category_raw', category)`, which is the producer's value before and after the
retrofit, so no event identity moves. The `category` filters on `GET /api/v1/v3/events` and the
Consult `query_events` tool match the registered column or `data.category_raw`; display readers show
the registered column.

**Write sites.** `provenance.writes._insert_situation` / `_insert_event`,
`situation_clustering._upsert_situation`, `entity_resolution._resolve_batch`,
`seed._driver._resolve_entity` and `scripts/backfill_entity_graph.py`. Existing rows are
`scripts/retrofit_case_columns.py`'s: `--dry-run` (the default) prints a census per column;
`--apply` writes one guarded UPDATE per batch with a resume cursor, never touches `updated_at`, and is
idempotent.

**One tag, two spellings.** Two stored `signals.tags` strings are the same tag exactly when
`signal_tag_key` gives them the same key, and `data/tags.py:signal_tag_match_forms` is that key's
whole preimage: `src:x` and a bare `x` are one tag, `demo.axis:x` is only itself, and
`src:demo.axis:x` is not `demo.axis:x`. Every reader that filters on a declared tag expands it through
this one function.

### Slice filters — `scope.signal_tags`

A target's `scope` may declare which signals its slice reads by tag, and an analyst may narrow further
(`data/signal_slice_scope.py`, used by both slice readers):

| where | key | clause on the slice's one statement |
|---|---|---|
| target `scope` | `signal_tags: [..]` | `tags && $n`; strict: an untagged signal is not in it |
| target `scope` | `exclude_signal_tags: [..]` | `NOT (tags && $m)` |
| analyst `subscription.substrate` | `signal_tags: [..]`, `untagged_fallback: bool` (default `true`) | `(tags && $n OR tags = '{}')`; with `untagged_fallback: false`, `tags && $n` |

Each entry is `<family>:<value>` or a bare legacy value, expanded through the equivalence rule before
it reaches SQL. The registry refuses at registration, by name, a declared tag whose family it has not
loaded and then one whose value the family does not have; a bare tag always passes. In the
cross-target slice each target's tags AND its own source and geo scope, and the targets OR, so one
target's exclude never hides another's rows. Every scoped slice is canonical-only (a re-polled alias
is never a second citation), and `method.options.slice_per_source_cap` ([TUNABLES.md](TUNABLES.md))
caps rows per source.

## People and sign-in

Local accounts sit behind `LEGBA_AUTH_SIGNIN` (off by default; with it off every person presents the
shared admin token). See [OPERATING_YOUR_INSTANCE.md](OPERATING_YOUR_INSTANCE.md) for the procedure
and [API_ROLES.md](API_ROLES.md) for which route each role may call.

| Table | Purpose | Write semantics |
|---|---|---|
| `users` | one row per person: internal id, optional email, display name, role (`reader`, `contributor`, `admin`; older rows may hold the earlier names, read through a map), status (`invited`, `active`, `disabled`), the auth provider, and `is_owner` (an admin with the owner flag, set by the one-time bootstrap command `python -m legba.data.users_bootstrap`) | **mutate-in-place** |
| `user_credentials` | one row per (user, provider) secret; only `local_password` writes here, an argon2id hash, never plaintext | **mutate-in-place** |
| `user_sessions` | sessions: only the sha256 of the browser's opaque session id is stored, so reading this table cannot sign anyone in. Expiring, revocable, with an idle limit (`last_seen_at`) | **append** plus revoke and last-seen |
| `user_invites` | invite tokens (sha256 at rest), bound to one email and role, expiring, single-use | **append** plus `used_at` |
| `user_seen_cursors` | per person and surface, when they last looked, so "since you last looked" follows a person across the phone shell and the desktop. Written only with sign-in on | **mutate-in-place** |

## Operational plane

| Table | Purpose | Write semantics |
|---|---|---|
| `budget_ledger` / `global_budget_envelope` | per-analyst and global token accounting | **mutate-in-place** |
| `budget_demotion_events` | one row per budget-driven demotion | **append-only** |
| `action_pack_invocations` | the agency ledger: one row per governed tool call, settled with its true outcome, cost, units and duration. The next call's rate window reads it | **append** plus settle |
| `governor_events` | every allow and every block, with the cap dimension, the limit and the observed value | **append-only** |
| `retention_policies` | the config table the shared retention sweep reads. A policy's default TTL is 0, which disables it, because deleting substrate data is a deliberate decision | **mutate-in-place config**; the route may move only the tunable fields; `written_by` |
| `actor_state` / `actor_filter_state` / `trigger_state` | runtime state: the actor store, the crash-safe source cursor and provisioning store, and the coalescing accumulator. The first two are created by the runtime, not by a migration | **mutate-in-place** |
| `legba_jobs` | the async job plane's queue (created by its store) | **mutate-in-place** |
| `discovery_state` | per-discovery cycle state, including the disappearance history | **mutate-in-place** |

## Control plane

| Table | Purpose | Write semantics |
|---|---|---|
| `source_descriptors` / `target_descriptors` / `analyst_descriptors` / `action_pack_descriptors` / `collection_descriptors` | the descriptor families: the live system *is* these rows, not the YAML files on disk | **append-only**, content-hash versioned, one head per id; `written_by` |
| `stack_components` / `stack_credentials` | the model, embedder, search and store components descriptors resolve their stack references against | **append-only** versions; credentials are vault-backed; `written_by` |
| `wiring_descriptors` | explicit subscription grants, keyed `(source_id, target_id)` | **append-only**; `written_by` |
| `vocabulary_entries` | the open vocabularies the registry validates against. `family` is either a platform family (a closed, protected set of sixteen: `entity_class`, `relationship_type`, `analyst_kind`, `source_kind`, `output_kind`, `filter_kind`, `enrichment_kind`, `discovery_kind`, `edge_family`, `salience_class`, `collection_gap_kind`, `band`, `source_layer`, `frame`, `event_type`, `crosswalk`) or a profile's namespaced `<profile>.<axis>` (lowercase, dotted, at most three segments). The admission rule is `schemas/vocabulary.py:validate_family`: a bare new name is refused `family_unknown`, a profile redefining or shadowing a platform family `family_protected`. The seeded families are loaded from their code literals (`data/taxon_families.py`) and descriptor files, and code degrades per family to the literal when a family is not loaded; a value outside a family binds `unclassified`. `parent` is a family-qualified id; the cache walks it (depth 8, cycle-safe). A family the cache cannot admit is left unloaded and listed in `families_unloaded` | **append-only**; `written_by` |
| `descriptor_audit_log` / `audit_checkpoints` | the audit log over descriptor mutations, naming the actor and role: each row Ed25519-signed and hash-chained to the one before (`chain_seq`, `prev_hash`, `row_hash`), and its periodic checkpoints | **append-only** |
| `descriptor_dead_letter` / `descriptor_conversion_archives` / `conversion_webhooks` / `conversion_executions` | rejected registrations, and the descriptor conversion surface | **append** plus resolution |
| `principals` | one row per writer ([above](#who-wrote-a-row-principals-and-written_by)) | **append**, created on first use |
| `profile_projection` | each profile's read-time vocabulary as a materialised plane ([above](#the-profile-projection)) | **derived / recomputable** |
| `ui_panel_registrations` | the workstation's panel registry | **mutate-in-place** |
| `legba_data_migrations` | applied-migration bookkeeping, keyed by filename, with each file's hash | **append-only** |

The type registry (`src/legba/data/type_registry/`) is code, not a table: one module per declared
type, in families such as `principal_kind`, `citation_target`, `assertion_type` and `alert_kind`
([TYPES.md](TYPES.md), [ADDING_A_TYPE.md](ADDING_A_TYPE.md)). Tables that refer to a type store its id
and the writer refuses an undeclared one.

---

## The three questions

### Does the inline pipeline change the signal, or add to it?

Both, stage-specific. The `signals` row is written once. The enrichment stages then update that one
row in place: language, geo, entity classes, classification, credibility. Only two stages write
elsewhere: `ingest_dedupe` appends a `signal_aliases` row and sets `canonical_signal_id`, and
`fact_extractor` appends to `facts`. `signals` is therefore the one substrate table that is not
strictly append-only, and the list of what later updates it is in the acquisition table above.

### What does the target and fan-out layer record?

Almost nothing persistent: it is routing and control, not data. There is **no `target_id` on
`signals` and no per-target delivery table**. What exists is `target_descriptors` (the geo, tags and
predicate contract), in-memory subscription wiring plus one durable aggregated consumer per target, a
two-stage match computed fresh at delivery, and the target actor's lifecycle and cursor state. A
non-discovery target is a passive subscriber.

A target is a scoped subject (in the example instance, a **desk**: a named scope a set of analysts
work), not a surveilled entity. Targets are selected by coverage tag; adding one is registering a
target with the right tag, with no code change. [RELEASE_STATE.md](RELEASE_STATE.md) carries the
roster of a deployment.

### What does analysis read and write?

Reads depend on the analyst's altitude. A first-order reasoning unit reads a scope-filtered signal
slice plus open facts, nexuses and hypotheses, plus a grounding preamble of accumulated substrate
state, so it integrates over time rather than only over today's signals. A composition reads **no raw
signals**: it reads other analysts' verified findings, inner-joined on the faithfulness critique above
the floor, so an unverified sub-claim never enters as assertable evidence. A deterministic handler
reads whatever materialised slice its job needs.

Writes go through two channels: `analyst_outputs` for the typed output, and direct substrate
side-writes for facts, nexuses, hypotheses, entities and the ledgers. The trace-only kinds write no
output row at all; their product is the side-write. **Every run**, whatever it produced, leaves
exactly one hash-chained `analyst_traces` row, with the output reference empty when nothing was
emitted, so "did this analyst run?" is answerable independently of "did it produce anything?".

---

## Lineage: backward, forward, and off-chain

**Backward** is `derived_from`, a UUID array on every substrate table, stamped at write. A lineage
walk resolves a composed read through the reads it rests on, their units, each unit's cited signals
(and, since citations became rows, the exact sentences and stored versions), and each signal's
acquisition record. Events are a lineage node; the journal is not.

**Forward** is `output_consumption`: what now rests on this row. It is stamped where consumption is
*decided* (inside a composition's basis-and-periphery split, and at the journal's slice selection) and
written on the same connection as the output, so a consumer row and its edges land together or not at
all. "New evidence contradicts something a live read is built on" and "…something a live read
mentioned as a caveat" are different-severity facts, and this index keeps them apart.

**A bearing edge is not lineage.** It says a later thing bears on an earlier question, and it never
mutates the question it points at.

**The journal is the one deliberate exception to the chain.** A journal row carries an always-empty
`derived_from`, and `journal_entries` is absent from the lineage catalogue, so a walk from a fact,
situation or nexus can never surface a journal node. Its citations live only in its own columns, an
outward-pointing warrant, never lineage a walk can descend into. A test holds the never-writes-a-fact
line, and the grant layer backs it: the journal holds only packs that cannot write a fact.

---

## Mutate against append cheat sheet

- **Append-only, trigger-enforced:** `situation_events`, `review_flags` (closure is by supersession),
  `event_lifecycle_events`, `origin_assignment_log`, `fetch_version_records`,
  `forecast_resolution_records`, `evidence_drift_classifications`, live `observations` rows, the five
  citation-row tables and the held-record tables (except the `assertion_status` cache).
- **Append-only, writer discipline:** `analyst_outputs`, `analyst_traces`, `journal_entries`
  entry-family rows, `signal_aliases`, `entity_profile_versions`, `bearing_edges`,
  `output_consumption`, `external_grades`, `unit_references`, `unit_correctness`,
  `unit_correctness_claims`, `grader_calibrations`, `read_events`, `acute_forecasts` issue rows,
  `band_calibration_claims` issue rows, `goldset_week_samples`, `source_poll_outcomes`,
  `graph_metrics`, `seed_batches`, `alert_events`, `alert_episode_actions`, `desk_rows_shadow`,
  `decision_shadow_ledger`, the governor and audit ledgers, and every descriptor version.
- **Supersession-versioned:** `facts` (on a value change), `nexuses` (on a polarity or label change),
  `entity_edges`, `event_edges`, `situations` and `events` (on close), `journal_entries` consolidation
  rows, `source_ratings` and `source_dossiers`. Facts and nexuses also decay their open rows. Under
  the contention flag, a same-tier incoming value that is fuzzy-distinct from an open prior does
  **not** close it: the two coexist open as a dispute.
- **Mutate-in-place:** `signals` enrichment and its later stamps, `entity_profiles`, `proposed_edges`,
  hypothesis and situation status, journal-proposal status, `acute_forecasts` resolution,
  `forecast_questions` status, `correctness_labels`, `evidence_archive`, `collection_requirements`
  disposition only, `retention_policies` tunable fields only, `alert_trigger_watermarks`, `alerts`
  episodes, `watchlist`, `origins` group pointer, `ingestion_requests` state, the user tables, the
  budget and invocation ledgers, `output_dead_letter` resolution, and the runtime state stores.
- **Derived / recomputable:** `fact_contention`, `fact_contention_values`, `fact_contention_tiebreak`,
  `fact_decay_states`, `source_track_records`, `narratives` and `narrative_echo_edges`,
  `desk_baselines`, `entity_alias_keys`, `source_layers` and `desk_apertures`, `graph_arcs`,
  `profile_projection`, `assertion_status`, and the `source_quality` and `forecast_scores` views.
- **Ephemeral:** subscription wiring, per-target consumers, the predicate match, NATS output streams.

---

## Migration mechanics

Migrations live in `src/legba/data/migrations/`. `0001_baseline.sql` is a flattened baseline of the
earliest schema; after it come numbered files (`0032_…` onward) and then time-named files
(`YYYYMMDDTHHMM_…`), so parallel work cannot collide on a number. The runner (`legba.data.migrate`)
applies them in that order (numbered first, then time-named; a name it cannot order is an error, never
skipped), each file in its own transaction, and records each by filename and hash in
`legba_data_migrations`; a re-run skips applied files. Numeric gaps are harmless. A fresh deploy
through `deploy/deploy.sh` first applies the shipped baseline `deploy/baseline/0001_baseline.sql`,
which also pre-seeds the ledger for the history it reproduces, and then runs the runner for
everything after it. Every migration is
additive and idempotent where it can be, and a data-hygiene close is written reversibly.

[CHANGELOG.md](../CHANGELOG.md) records which migration landed what, and when.
