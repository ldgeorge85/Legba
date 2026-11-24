<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Declared types

The reference tables of every family in the type registry
(`src/legba/data/type_registry/`). They are generated from the declarations by
`scripts/gen_type_tables.py`; the gate runs it with `--check`. To add a type,
read [ADDING_A_TYPE.md](ADDING_A_TYPE.md); never edit a table here by hand.

## Deterministic handlers

The LLM-free sub-handlers of the `deterministic` analyst kind. A handler's id
is the descriptor's `options.sub_handler` and, by convention, its analyst id.
"Writes a finding" means a row in `analyst_outputs`, verify-exempt and badged
`unverified — structural` (or re-derived, where structural claims are
verified); "a trace only" means the real product is side-written and the run
is audited in `analyst_traces`. "Refuses without a pool" means the handler
raises rather than report a zeroed result it never measured.

<!-- BEGIN GENERATED deterministic-handlers (scripts/gen_type_tables.py; do not edit) -->
| Handler | One run writes | Refuses without a pool | Structural claims verified | What it does |
|---|---|---|---|---|
| `adversarial_signals` | a finding |  |  | Adversarial-signal scoring over recent signals. |
| `alert_trigger_scan` | a trace only | yes |  | Verification-gated trigger scan over verified state transitions. |
| `analyst_traces_retention` | a finding |  |  | TTL purge of aged analyst traces (off by default). |
| `anomaly_detection` | a finding |  |  | Signal-volume rate spikes, sentiment-shift z-scores and novel-entity emergence over recent windows. |
| `archive_retention` | a trace only |  |  | Expires the stored bytes of uncited archive rows (off by default). |
| `band_calibration_tracker` | a finding | yes |  | Scorecard band transitions logged as resolvable claims and resolved against later scorecard rows. |
| `calibration_tracking` | a finding |  |  | Analyst confidence against outcome: Brier score, reliability bins and drift across windows. |
| `claim_watch` | a trace only | yes |  | Flag-only matcher of new signals against open questions. |
| `collection_gap` | a finding |  |  | Monthly collection requirements: the starved desk and dimension cells. |
| `composition_lineage_sweep` | a finding | yes |  | Per-floor derived_from walk over the composition roots; names a broken floor. |
| `contrary_evidence_pass` | a trace only | yes |  | One counter-query per material claim; a contention row whose stance is derived in code. |
| `corpus_indexer` | a trace only |  |  | Projects signals into the OpenSearch full-text corpus. |
| `corpus_retention` | a trace only |  |  | Drains the corpus tombstone queue: deletes documents whose signal row is gone. |
| `correctness_grader` | a finding | yes |  | Grades each unit's claims against its reference, under a passing calibration. |
| `cross_source_coalesce` | a trace only |  |  | Substrate-wide semantic and temporal near-duplicate linker. |
| `cross_source_dedup` | a trace only |  |  | Links and marks duplicate signals across sources. |
| `desk_baseline` | a finding | yes |  | Per-desk statistical baseline and the current window's deviation. |
| `desk_reference` | a finding | yes |  | The out-of-plane daily desk reference and the two attention diffs it feeds. |
| `due_and_absent` | a finding | yes |  | What was expected by an explicit date and has no matching record in this collection. |
| `entity_gc` | a trace only |  |  | Garbage-collects orphaned entities and their dangling proposed edges. |
| `entity_resolution` | a trace only |  |  | Folds new NER mentions into canonical entities. |
| `event_clustering` | a trace only |  |  | Clusters canonical signals into events and maintains their lifecycle ledger. |
| `event_reconciler` | a trace only |  |  | Writes correlated_with and evolves_from event edges. |
| `evidence_archiver` | a trace only |  |  | Archives the original bytes behind cited signals, content-addressed. |
| `evidence_refetch` | a trace only |  |  | Re-fetches the textless backlog for hosts whose fetch policy changed. |
| `fact_contention_arbiter` | a trace only |  |  | Detect-only contested-claims arbiter: builds the contention sidecar and marks facts. |
| `fact_decay` | a trace only |  |  | The legacy temporal decay stamp on facts. |
| `fact_decay_scan` | a finding | yes |  | Daily decay readout stamped into the fact decay sidecar; never touches facts. |
| `finding_supersession` | a trace only |  |  | Finding-level dedup: stamps supersession on existing findings. |
| `forecast_resolver` | a trace only |  |  | Proposes resolutions for due sealed forecast questions; settles series thresholds by rule. |
| `forecast_scoreboard` | a trace only |  |  | Drives the acute-forecast pilot: issue, resolve, count. |
| `geo_convergence_scan` | a finding |  | yes | Geographic convergence of several source families (emission folded into the trigger scan). |
| `graph_mining` | a finding |  |  | Community detection, structural-balance triads and proxy-chain mining over the knowledge graph. |
| `graph_projector` | a trace only |  |  | Whole rebuild of the cross-layer graph projection. |
| `hypothesis_lifecycle` | a finding |  |  | Emits forward hypotheses over rising situations and tests standing ones. |
| `indicator_tracker` | a finding |  | yes | Run-over-run flips of the structured indicators, debounced. |
| `inquiry_yield` | a finding |  |  | Weekly counts over the inquiry ledger, per descriptor. |
| `integrity_sweep` | a trace only | yes |  | Referential-integrity sweep: repairs or flags dangling references. |
| `kev_digest` | a finding | yes |  | The KEV catalogue entries new since the last digest, copied field by field. |
| `layer_divergence` | a finding |  | yes | A country's layer-to-layer divergence against its own rolling baseline. |
| `narrative_mapper` | a finding | yes | yes | Reifies contested-claim families as narratives and refreshes the source-echo graph. |
| `nexus_decay` | a trace only |  |  | Temporal decay stamps on reified relationships. |
| `profile_projector` | a trace only |  |  | Whole rebuild of each profile's projection plane. |
| `proposed_edge_governance` | a trace only |  |  | Promotes corroborated proposed edges and ages out thin stale ones. |
| `receipt_anchor` | a trace only |  |  | Daily external timestamp (OpenTimestamps) on the receipt chain. |
| `reenrich_ner` | a trace only |  |  | One-time NER backfill over the pre-fix historical backlog. |
| `reenrich_pending` | a trace only |  |  | Re-enriches rows the source actor wrote unenriched. |
| `reenrich_translation` | a trace only |  |  | Translation backfill for the non-Latin backlog. |
| `reference_builder` | a finding | yes |  | Builds a target's 14-day reference through the web, keeping only fenced developments. |
| `reference_deterministic` | a finding | yes |  | Cuts a machine-readable list into keyed per-subject references. |
| `research_measurement` | a finding | yes |  | The outbound-research counters: novelty, corroboration, consequence. |
| `scorecard_producer` | a trace only |  |  | One banded scorecard row per active desk. |
| `series_change` | a finding | yes |  | Anchored-window robust-z change detector over live series observations. |
| `signal_embedder` | a trace only |  |  | Embeds signal bodies into the vector collection. |
| `signal_summarizer` | a trace only |  |  | Distils long signal bodies into an analysis-tuned summary on the core plane. |
| `signals_retention` | a finding |  |  | TTL purge of aged signals (off by default). |
| `situation_clustering` | a finding |  |  | Materialises situations from stamped signatures. |
| `source_track_record` | a finding | yes |  | Daily per-source earned record over resolved contentions. |
| `standing_auditor` | a trace only | yes |  | The standing external audit: sampled world claims checked on the web, one critique per claim. |
| `structural_balance` | a trace only |  |  | Signed-edge triadic balance over the entity-relationship graph. |
| `thematic_proposal` | a finding |  | yes | Proposes thematic frames for uncovered hot situations. |
| `unit_correctness_scorer` | a finding |  |  | Each bounded unit's latest head scored against the operator's gold rows (source-id overlap recall). |
| `verify_backfill` | a trace only |  |  | Writes the faithfulness critique for cited findings whose verify leg never ran. |
| `watch_horizon_sweep` | a trace only |  |  | Closes consult watches at their horizon and folded duplicates. |
<!-- END GENERATED deterministic-handlers -->

## Alert kinds

The closed list of the single alert path, in the Watch surface's order.
"Per the page rule" means the shipped `alerts/page_rule.yaml` has an entry for
the kind; "logged only" means it never pages under the shipped rule.
[ALERTING.md](ALERTING.md) explains the path, the episodes and the rule.

<!-- BEGIN GENERATED alert-kinds (scripts/gen_type_tables.py; do not edit) -->
| Kind | Group | Subject | Lifecycle | Topic | Pages | Summary |
|---|---|---|---|---|---|---|
| `escalations` | findings | target | event | world | per the page rule | A desk's agent escalated a finding (a transition, not a level). |
| `verified_finding` | findings | target | event | world | per the page rule | A verified high-severity finding. |
| `band_crossing` | findings | target | event | world | per the page rule | A desk scorecard band moved (one push per trigger tick). |
| `situation_escalation` | findings | target | event | world | per the page rule | A tracked situation escalated. |
| `baseline_deviation` | baseline | target | event | world | logged only | A desk's daily signal or high-severity-finding count left its baseline band. |
| `watchlist_hit` | findings | target | event | world | per the page rule | An operator watchlist entry matched. |
| `contention_flip` | findings | target | event | world | per the page rule | A contested claim flipped (kill-listed by default). |
| `geo_convergence` | findings | target | event | world | per the page rule | Several sources converged on one place (kill-listed by default). |
| `rollup` | findings | target | event | world | per the page rule | The per-desk cap folded several trigger alerts into one. |
| `volume_surge` | baseline | target | condition | world | per the page rule | A subject's signal count over the last hour is far above its own baseline and comes from several independent outlets. |
| `analyst_alert_output` | findings | target | event | world | logged only | An analyst's first-generation 'alert' output fired. |
| `source_cadence_stall` | source health | source | condition | engine | per the page rule | A source has polled nothing for longer than its cadence allows. |
| `source_degraded` | source health | source | condition | engine | per the page rule | A source's polls come back empty while upstream has newer entries. |
| `source_staleness_stale` | source health | source | condition | engine | per the page rule | A source's newest signal is stale for its grade. |
| `source_staleness_dead` | source health | source | condition | engine | per the page rule | A source's newest signal is old enough to call it dead. |
| `source_stream_down` | source health | source | condition | engine | logged only | A subscribed source's held connection is refused, stalled, reconnecting for several windows, or not held at all. |
| `evidence_drift` | source health | source | event | engine | logged only | Pages a source published were edited, retracted or removed after the platform read them, and something relied on them (one episode per source per day; never paged by default). |
| `analyst_cadence_stall` | analyst failure | analyst | condition | engine | per the page rule | An analyst's newest successful run is older than twice its interval. |
| `analyst_cursor_lag` | analyst failure | analyst | condition | engine | per the page rule | An analyst's cursor is further behind real time than its declared lag. |
| `analyst_hard_fail` | analyst failure | analyst | event | engine | per the page rule | An analyst run failed hard under retry.hard.strategy=dlq_and_alert. |
| `production_deficit` | analyst failure | analyst | event | engine | per the page rule | A loop produced less than its declared output. |
| `coverage_floor` | analyst failure | target | event | engine | per the page rule | A desk's coverage fell under its floor. |
| `external_audit` | analyst failure | target | event | engine | per the page rule | An external web check contradicted a sampled high-severity claim. |
| `llm_route_dead` | analyst failure | route | condition | engine | per the page rule | A busy LLM route answered almost nothing over 24 hours. |
| `llm_route_auth_failed` | analyst failure | route | condition | engine | per the page rule | An LLM route refuses authentication (401/402/403/404). |
| `search_degraded` | analyst failure | platform | condition | engine | per the page rule | The search plane's NOT_FOUND share is degraded. |
| `consult_unhealthy` | consult | route | condition | engine | per the page rule | Several consecutive consult turns failed or did not finish. |
| `thinking_fallback` | analyst failure | route | event | engine | logged only | A thinking call failed for a reason the thinking alias causes and was retried once on the plain alias (logged only, never paged by default). |
| `liveness_stall` | pipeline | platform | event | engine | per the page rule | No signal and no finding for the stall window. |
| `alert_volume_high` | pipeline | platform | condition | engine | per the page rule | Pushes on one topic over 24 hours exceed the declared cap. |
| `reminder_orphan_collected` | pipeline | platform | event | engine | logged only | An orphan Dapr reminder was collected. |
| `forecast_resolution` | forecasts | question | event | engine | per the page rule | A forecast question has a new resolution proposal or waits on a judgement. |
| `due_and_absent` | expectations | platform | condition | engine | logged only | Something expected by a date has no matching record in this collection (logged only; an absence in the collection, not a statement about the world). |
| `host_disk` | host | host | condition | engine | per the page rule | The root filesystem entered a usage band (df), or left one. |
| `host_llm_heartbeat` | host | host | event | engine | per the page rule | The host's LLM-plane heartbeat found the plane silent, dead or degraded. |
| `host_search_canary` | host | host | event | engine | per the page rule | The host's search canary failed consecutive probes. |
| `host_stall_recovered` | host | host | event | engine | logged only | The host stall watchdog restarted the plane after a stall. |
| `nightly_suite` | deploy | deploy | event | nightly | per the page rule | The nightly test suite's verdict. |
<!-- END GENERATED alert-kinds -->

## Citation targets

What a finding's sentence may rest on: the kinds a `citation_support` row
(migration `20261115T1828_citations_as_rows`, lane DM-A) may point at, each
with its own typed key column. "Material" means the record has received bytes
behind it, and the citation row pins the receipt version it was read from.
"Named by" is how a citation entry in a finding's document names it: a
`ref_kind` for one record, or a grounding block that stands for several.
`legacy_key` holds a kind with no uuid; it is counted, never joined.

<!-- BEGIN GENERATED citation-targets (scripts/gen_type_tables.py; do not edit) -->
| Kind | Table | Key column | Material (bytes pinned) | Standing | Named by | What it is |
|---|---|---|---|---|---|---|
| `desk_baseline` | `desk_baselines` | `legacy_key` |  | background | `desk_baseline` block | A desk baseline metric keyed 'desk:metric' (no uuid): held in legacy_key, counted, never joined. |
| `event` | `events` | `event_id` |  | background | `event`, `open_events` block | An event: a member of the open-events block, or an event token whose reports did not resolve. |
| `finding` | `analyst_outputs` | `output_id` |  | background | `finding`, `prior_read`, `window_ledger` block | An earlier read (an analyst_outputs row): a composition's sub-read, the desk's prior read, a window-ledger member; the desk's own earlier read restates and is never new evidence. |
| `hypothesis` | `hypotheses` | `hypothesis_id` |  | background | `open_questions` block | A standing open question (a hypotheses row), as a member of the open-questions block. |
| `inquiry` | `inquiry_ledger` | `inquiry_id` |  | background | `inquiry_ledger` | An inquiry-ledger entry, cited by id. |
| `observation` | `observations` | `observation_id` | yes | evidence | `observation` | One number of a series (an observations row), cited by its line in the historical-series block; its receipt version is pinned. |
| `signal` | `signals` | `signal_id` | yes | evidence | `signal` | A collected item (a signals row); the receipt version it was read from is pinned on the citation row. |
| `situation` | `situations` | `situation_id` |  | background | `situation`, `situation_register` block | An open situation, cited by id or as a member of the open-situation register block. |
<!-- END GENERATED citation-targets -->

## Principal kinds

Who can write a row (data-model lane 9). A principal is one row in
`principals`, named by its handle `<kind>:<name>`; tables that hold
judgements, labels, decisions, descriptor changes and entered knowledge carry
a `written_by` reference to it, and every table's writer class is declared in
`src/legba/data/write_attribution.py`. With sign-in off every person writes as
`shared:admin-token`, exactly as every request was one admin before.

<!-- BEGIN GENERATED principal-kinds (scripts/gen_type_tables.py; do not edit) -->
| Kind | Name after the colon | Example | What it is |
|---|---|---|---|
| `agent` | the analyst descriptor id, @, the descriptor version | `agent:desk_northaven@3f9a0c` | An analyst seat that calls a model; one principal per descriptor version, so a changed prompt is a different writer. |
| `rule` | the deterministic handler id, or a named code path | `rule:collection_gap` | Deterministic code: a deterministic handler (its id is its analyst id) or a named code path such as a materializer or a loader. |
| `service` | the service name (registry, runtime, worker, console), or fleet for the shared service token | `service:runtime` | A process of the stack presenting a service credential; fleet is the shared service token any process may fall back to. |
| `shared` | admin-token (the shared admin credential) or dev (a development-mode registry with no credential) | `shared:admin-token` | A credential several people may hold; a write under it names the credential, never a person (with sign-in off every person writes as shared:admin-token). |
| `user` | the person's users.id | `user:0b6c2f0e-1111-4c2a-9d3e-000000000001` | A signed-in person, linked to their users row by id; each person is their own principal, whatever their role. |
<!-- END GENERATED principal-kinds -->

## Assertion types

What kinds of held record exist (data-model lane 10, migration
`20261115T1940_held_record`). An assertion is something said about a subject,
for a period, with versions, an append-only hash-chained status log and links
to what it rests on; on screen the checked kind is a "statement", and
`assertion` is the database name of the family. The CLASS is a closed list in
code (statement, attributed, assessment, absence): it decides what may hold a
record and what it may be used for, so a new class is a reviewed change. The
TYPE is a declaration: one module in `src/legba/data/type_registry/assertion_types/`.
Its claim key (with the subject and the type) is what two records are matched
on: an exact match is a restatement link, never a second record. Statements
can be held only through the gates and a "supports" verdict; nothing becomes a
"fact" automatically.

<!-- BEGIN GENERATED assertion-types (scripts/gen_type_tables.py; do not edit) -->
| Type | Class | Claim key | Bases | Desk row kinds | What it is |
|---|---|---|---|---|---|
| `collection_absence` | absence | subject + topic, collection | derived | `absence` | Nothing matching a topic was collected from a desk's sources in a window: a record about the collection, never about the world. |
| `judgement` | assessment | subject + text | derived | `assessment` | A desk's judgement with its confidence word, resting on held statements as premises; never established. |
| `attributed` | attributed | subject + text | derived | `attributed` | What a named speaker said ('X said Y'): it holds that it was said, never that it is so. |
| `event` | statement | subject + text | derived | `event` | Something that happened to the subject at a time, checked against the quotes it rests on. |
| `level` | statement | subject + text | derived | `level` | A figure about the subject (one figure per record), checked against the quotes it rests on. |
| `state` | statement | subject + text | derived | `state` | A condition of the subject that holds over a period, checked against the quotes it rests on. |
<!-- END GENERATED assertion-types -->
