<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Code map

Where each concern lives in the code, for contributors. One row per package or
module family: what it does and the file to start from. Every path is a tracked
path in this repository.

For how the parts fit together read [ARCHITECTURE.md](ARCHITECTURE.md) and
[ANALYSIS.md](ANALYSIS.md). The implementation rules are in
[CONTRIBUTING.md](../CONTRIBUTING.md), and running an instance is in
[RUNBOOK.md](RUNBOOK.md). Code that is in the tree but not built or not live is
listed in [SEAMS.md](SEAMS.md) and [STATUS.md](STATUS.md).

Cite code as `module.symbol`, never by line number: `grep` finds a symbol, and
the citation survives a file split. This page gives no counts of modules or
rows; `git ls-files` answers those from a checkout.

---

## 1. Top-level layout

```
src/legba/            the platform (Python package)
legba-ui-v3/          the React/TypeScript console
legba-models/         the model service (vLLM LLM, bge-m3 embeddings, NLLB/spaCy NER)
legba-media/          the media-extraction service (answers 503 with no backend; SEAMS #1)
descriptors/          example source/target/analyst/pack/stack/discovery YAML, LIFECYCLE.md, LIFECYCLE.d/, RETIRED.md
profiles/             instance profiles: geopolitics (the example) and a sanctions skeleton
scripts/              registrars, seeders, backfills, evaluation scripts, ops shell
docker/               Dockerfiles (registry / runtime / worker / mcp), Caddyfile, nats.conf
dapr/components/      Dapr component manifests (statestore, pubsub, secretstore, configuration)
deploy/               bring-up script, systemd units, cron lines, Prometheus rules, swarm stacks
seeds/                curated-dataset drop point (only *.example.yaml files are tracked)
tests/                the test suite
docs/                 design and reference docs
docker-compose.yml    the whole stack, profile-gated
pyproject.toml        package metadata and console scripts
```

`src/legba/data/` is the declarative and substrate half: descriptor schemas, the
registry, the handler libraries, the store adapters and the migrations.
`src/legba/runtime/` is the execution half: the Dapr actor host, the four
runtime planes and the reconcile loop. `src/legba/ui/` holds the MCP server.

The split is by responsibility, not a hard import boundary. `data/` reaches into
`runtime/` mainly for one thing: analyst kind modules import the shared result
and handler types from `runtime/analyst_method.py`, which is kept small for that
reason. The few other crossings are commented at their call sites.

---

## 2. `src/legba/data/`

### 2.1 Package index

| Package | Its job | Start from | Notable files |
|---|---|---|---|
| `schemas/` | Descriptor types: strict, `extra="forbid"`, content-hashable pydantic models | `source.py` | `source.py`, `target.py`, `analyst.py`, `action_pack.py`, `collection.py`, `stack.py`, `lifecycle.py`, `versioning.py` |
| `registry/` | The control plane: descriptor registry, credential vault, HTTP/WS API | `server.py` | `descriptor.py` and `descriptor_families.py` (registry core), `credentials.py` (XSalsa20-Poly1305 vault), the routers (§2.2) |
| `sources/` | Source-kind acquisition handlers | `_contract.py` | The handler Protocol and `Signal`; `baseline.py`; one module per kind; `webhook_router.py`; `_egress.py` (every fetch goes through it) |
| `sources/kinds/` | Source-kind declarations, plus the `legba.source_kinds` entry-point group | `__init__.py` | `series_api` (`series_api.py`) is the generic JSON/CSV series kind, configured in the descriptor |
| `filters/` | In-flight enrichment over a `Signal` | `_contract.py` | `language_detect.py`, `geocode.py`, `ner.py`, `classify.py`, `ingest_dedupe.py`, `keyword_gate.py`, `fact_extractor.py`, the `slm_*` refiners |
| `option_catalog/` | The `method.options` catalog and its resolve/degrade rules; a stdlib-only leaf so the slim registry image can validate options | `handler_options.py` | `handler_options_catalog.py` assembles `HANDLER_OPTIONS` and `ANALYST_KIND_OPTIONS`; guard test `test_option_catalog_leaf.py` |
| `analysts/` | Analyst-kind implementations and their helper families | `__init__.py` | `discover_analyst_kinds()` finds modules that declare `KIND_NAME`; see §2.3 to §2.5 |
| `provenance/` | Lineage, receipts, the verify pass and the judge | `kinds.py` | `OutputKind` and `KIND_REGISTRY` (`kinds.py`), `writes.py`, `origin.py`, `receipts.py` (SHA-256 chain), `verify.py` (§2.6) |
| `outputs/` | Emit handlers that send an analyst payload to an outward surface | `_contract.py` | `substrate.py`, `nats_stream.py`, `webhook.py`, `alert.py` + `alert_sinks/`, `mcp_tool.py`, `stix_bundle.py` + `taxii_client.py` (SEAMS #10) |
| `alerts/` | The one alert path and its push delivery | `raise_alert.py` | `raise_alert.py` (every producer calls it), `page_rule.py` + `page_rule.yaml`, `sinks.py`, `ntfy_sink.py`, `webhook_sink.py`, `topics.py`; see [ALERTING.md](ALERTING.md) |
| `migrations/` | The SQL schema | `0001_baseline.sql` | A flattened baseline, a numbered chain from `0032`, then time-named `YYYYMMDDTHHMM_slug.sql` (UTC); order rule in `__init__.py` |
| `events/` | The event surface | `lifecycle.py` | Lifecycle FSM and ledger vocabulary, `signature.py`, `_writes.py`; producers are `event_clustering.py` and `event_reconciler.py` (draft, `LEGBA_EVENTS`-gated) |
| `layers/` | The per-country layer table and aperture declarations (migration 0214) | `_vocab.py` | `loader.py`, called only by `scripts/load_layer_map.py`; served by `registry/layers_api.py`; see [DATA_SOURCES.md](DATA_SOURCES.md) |
| `discovery/` | Turns external lists and queries into descriptors | `registry.py` | Target kinds (`country_list_discovery.py`, `file_sd_discovery.py`, `static.py`); source kind `query_source_discovery.py`; `materializer.py` |
| `predicates/` | The Starlark predicate language used by subscriptions and matching | `compiler.py` | `evaluator.py`, `helpers.py` (per-surface helper catalog), `errors.py` |
| `stack/` | Provider adapters resolved through the stack registry | (per family) | `llm/` (provider clients, `call_gate.py`, `thinking.py`, `tool_rounds.py`), `search/`, `vector_store/`, `nats/`, `nlp_service/`, `postgres/`, `proxy/` |
| `seed/` | Curated baseline seeding and manual ingest | `_base.py` | `SeedSource` protocol, `_driver.py`, `adapters/`, `manual_batch.py`, `manual_schema.py` |
| `rag/` | Writes the vector corpora | `lane4_loader.py` | `chunker.py`; the manual-ingest vector loader embeds and upserts into `world_context` / `tradecraft` |
| `facts/` | Fact confidence decay, as a pure library | `decay.py` | Stamped by `deterministic_handlers/fact_decay_scan.py` into `fact_decay_states` |
| `situations/` | Situation trajectory arithmetic | `trajectory.py` | Shared by `situation_tracker` and the trajectory route |
| `jobs/` | Async job envelopes and their store | `envelope.py` | `media.py` (`MediaExtractionResult`), `store.py` |
| `tools/` | Analyst-callable external tools | `mnemosyne_trust_query.py` | The outbound trust-query tool |
| `conversions/` | Descriptor-version upgraders | `target_v1_to_v2.py` | Example converters, also the fixtures for the conversion-webhook tests |
| `rounds/` | Round specs: declared graphs of seats, run by the evaluation harness (SEAMS #92) | `spec.py` | `validate.py`, `interpreter.py`, `seats.py`, `model_seats.py`, `tool_binding.py`, `calibration.py` |
| `type_registry/` | One `declare(...)` module per type; every list of type names derives from it | `__init__.py` | See [ADDING_A_TYPE.md](ADDING_A_TYPE.md) and [TYPES.md](TYPES.md) |
| `clients/`, `shared/` | The outbound A2A client and shared leaves | `mnemosyne_a2a.py` | `shared/crypto.py`; `shared/auth_signin.py` (the `LEGBA_AUTH_SIGNIN` switch and the three role names) |
| `prompts/` (`src/legba/prompts/`) | Per-kind prompt modules, addressed by string from a descriptor's `prompt_module` | (per kind) | A rename breaks at run time, not import time. A profile's own prompts live in `profiles/<id>/prompts/legba_<id>/` |

### Modules at the `data/` root

| Area | Files | What they do |
|---|---|---|
| Substrate ports | `postgres.py`, `nats.py`, `qdrant.py`, `redis.py`, `opensearch.py`, `config.py` | One typed port per store, plus env-driven config. `nats.py` holds the signal subject grammar |
| Migrations and smoke | `migrate.py`, `smoke.py` | The migration runner and the substrate smoke check (`smoke.py` owns `RETIRED_TABLES`) |
| Vocabulary and tags | `vocabulary.py`, `taxon_families.py`, `tags.py`, `signal_slice_scope.py`, `source_classification.py` | Tag binding (`<family>:<value>`), the target and analyst tag filters, and a source's declared classifications |
| Frames | `frames.py`, `_frame_anchor.py`, `_frame_content.py` | Resolves frame members and rosters; read by `composition_slice.py` when `LEGBA_FRAME_FAMILY` is on |
| Entity canon | `_entity_canon.py`, `_entity_candidates.py`, `_entity_resolve.py`, `_entity_eval.py` | One entity canon shared by ingestion and the analysts |
| Geography | `_country_aliases.py`, `_fips_iso.py`, `_polity_match.py`, `_geo_routing.py` | Name aliases, the FIPS to ISO crosswalk for GDELT, polity matching, geo routing |
| Shared rules | `critic_fold.py`, `carried_state.py`, `indicator_shape.py`, `correctness_axis.py`, `run_accounting.py` | The one "latest critique per finding" SQL, the carry rule for unsupported unit reads, level-shaped indicators |
| Reader display policy | `display_policy.py` | What a reader-class caller may see of a row or payload; every reader transport calls it |
| Users and sign-in | `users.py`, `users_bootstrap.py` | Users, credentials, sessions and invites (migration 0236), `AuthProvider` with `local_password` built and `oidc` declared (SEAMS #73) |
| Instance profile | `profile.py`, `lens_family.py` | Loads `profiles/<id>/profile.yaml` (`--profile` > `LEGBA_PROFILE` > `profiles/geopolitics`); see [PROFILES.md](PROFILES.md) |
| Profile readers | `rosters.py`, `profile_constants.py`, `gazetteer.py`, `subject.py`, `profile_projection.py` | The one reader each of the profile's rosters, constants, gazetteer, subjects and projection view |
| Forecast ledger | `forecast_ledger.py`, `anchor_proof.py` | Questions, numbers and seals (migration 0242); the daily anchor's Merkle tree and inclusion proofs (migration 0262); see [LEDGER.md](LEDGER.md) |
| Forecast resolution | `forecast_resolvers.py`, `forecast_evidence.py`, `forecast_resolution.py`, `forecast_scoring.py` | Rules per resolver kind, the evidence search, the append-only resolution record (migration 0244), the scorecard |
| Evaluation record | `eval_runs.py` | Published evaluation runs, aggregates only (migration 0260); the loader is `scripts/publish_eval_run.py` |
| Who wrote a row | `principals.py`, `write_attribution.py`, `origins.py` | Principal handles, the writer class of every table, origins and origin groups; see [DATA_MODEL.md](DATA_MODEL.md) |
| Citations as rows | `citation_rows.py`, `citation_rows_store.py`, `citation_rows_reads.py`, `citation_rows_backfill.py` | A finding's citations kept as rows, written in the finding's transaction |
| Desk rows and held record | `desk_rows_*.py`, `held_record*.py` | The row-shaped desk contract run in shadow (`LEGBA_DESK_ROWS_SHADOW`) and the held record fed from it (`LEGBA_HELD_RECORD_SHADOW`); both off |
| Statement layer | `statement_*.py` | Statement gates, typing and verifier seats; not scheduled (SEAMS #81) |
| Evidence drift | `evidence_drift_*.py` | Detects and classifies changes to pages after they were read |
| Claim sources | `claim_support.py`, `claim_hygiene.py`, `claim_attribution.py` | The `claim` source class rules and claim-text hygiene; see [operations/claim-sources.md](operations/claim-sources.md) |
| Archive | `archive.py`, `archive_store.py`, `archive_tier.py`, `archive_codec.py` | The evidence archive, its S3-compatible backend and the tier mover |
| Intake | `intake_store.py`, `intake_contract.py` | On-request ingestion requests; see [operations/on-request-fetching.md](operations/on-request-fetching.md) |
| Consult leaves | `consult_write_vocabulary.py`, `consult_write_patch.py`, `consult_case_record.py`, `workstation_vocabulary.py`, `verified_findings.py` | Shared words and shapes for Consult writes and cases |

### 2.2 `registry/` routers

`server.py` decides what is mounted. The mount column is the router prefix,
not the full route.

Every mounted route passes one app-level dependency,
`route_roles.enforce_route_role`. With sign-in off it reads
`route_roles.ROUTE_ROLES` (`operator`, `reader-own-rows`, `read`). With
`LEGBA_AUTH_SIGNIN=1` it reads `route_policy.ROUTE_MIN_ROLES`, which names the
minimum role (`reader`, `contributor`, `admin`) of every route and refuses an
unlisted one below `admin`. [API_ROLES.md](API_ROLES.md) is generated from it by
`scripts/gen_api_roles_doc.py`.

Two ASGI middlewares identify the caller before any dependency runs.
`bearer_auth.py:BearerPrincipalMiddleware` reads the `Authorization` header
(and the SSE `?token=` and the socket subprotocol) and stamps the principal and
role. `session_auth.py:SessionCookieMiddleware` resolves a signed-in user's
`legba_session` cookie when no bearer was sent. The role gate refuses a caller
that neither stamped. `display_class.py:DisplayPolicyMiddleware` then applies
the reader display policy to every JSON response.

| Router module | Mount | Concern |
|---|---|---|
| `users_api.py` | `/api/v1` | Invite, accept, sign in and out, `GET /auth/me`, user admin. The owner is created once with `python -m legba.data.users_bootstrap` |
| `api.py` | `/api/v1/registry` | Descriptor CRUD, sources, action packs, stack, vault. The shared kernel is the leaf `_deps.py` |
| `v3_api.py` | `/api/v1/v3` | Actor rows, evaluation reads, console views; optimizer review in `optimizer_review*.py` |
| `since_api.py`, `timeline_api.py`, `narratives_api.py`, `situation_trajectory_api.py`, `events_api.py` | `/api/v1/v3` | Change-since reads, the timeline, narratives, situation trajectories, events |
| `source_assurance_api.py`, `source_quality_api.py`, `source_credibility_api.py` | `/api/v1/v3`, `/api/v1` | Source ratings, source quality and credibility; all read `source_freshness.py` |
| `absence_api.py` | `/api/v1/v3` | `GET /v3/absence`: typed absences for one desk, each with a proof and a measured-at time |
| `alert_volume.py`, `alert_volume_api.py` | `/api/v1/v3` | What counts as a push, and the per-topic, per-kind tally |
| `search_health.py`, `search_health_api.py` | `/api/v1/v3` | Whether a recorded search answered or ran degraded, per provider and engine |
| `collections_api.py` | `/api/v1/v3` | Coverage of curated collections: declared spans against stored spans (`collections_coverage.py`) |
| `production_gauge_api.py` | `/api/v1/v3` | Expected against actual production, worst first (`production_gauge.py`) |
| `grader_roster_api.py` | `/api/v1/v3` | The correctness grader's roster: latest row per target and desk, both averages labelled |
| `flow_api.py`, `flow_graph.py`, `flow_params.py`, `flow_runs.py` | `/api/v1/v3` | Read-only routes for the Flow canvas: the descriptor graph, a node, a version, a run |
| `judge_stats_api.py`, `external_audit_api.py` | `/api/v1/v3` | Judge statistics and health; the standing auditor's board |
| `layers_api.py` | `/api/v1/v3` | `GET /v3/layers/divergence`: the newest `layer_divergence` receipt per desk |
| `graph_structure_api.py`, `graph_walk_api.py`, `graph_triggers_api.py`, `graph_arcs_api.py`, `belief_api.py` | `/api/v1`, `/api/v1/v3` | Graph reads, the anchored ego walk, engine gauges, the projection read, the belief register |
| `projection_api.py` | `/api/v1/registry` | `GET /projection/{plane}[/{section}]`, the per-profile projection read |
| `substrate_reads_api.py` | `/api/v1` | Substrate reads for the panels; includes `unit_correctness_api.py` |
| `record_sheet_api.py` | `/api/v1/v3` | The Record sheet: version text, receipts, a finding's support rows, the held record |
| `origins_api.py` | `/api/v1/registry` | Origins and origin groups, with the admin's corrections |
| `lineage_api.py`, `entities_api.py` | `/api/v1` | The provenance walk; entity graph reads |
| `journal_api.py`, `journal_proposals_api.py` | `/api/v1` | The journal feed; the proposal queue, applied by `journal_proposals_apply.py` |
| `consult_api.py`, `consult_stream_api.py`, `consult_sessions_api.py`, `deep_consult_api.py` | `/api/v1` | Consult, its token stream, sessions, and the deep variant; turn stamps in `consult_turn_stamps.py` |
| `consult_writes_api.py`, `consult_write_exec.py`, `consult_write_*.py` | `/api/v1` | The Consult write front door and its one executor, with a branch per object class |
| `labels_api.py`, `goldset_api.py` | `/api/v1`, `/api/v1/v3` | Human gold labels and the gold-set cohort |
| `watchlist_api.py`, `collection_requirements_api.py`, `retention_policies_api.py` | `/api/v1/v3` | Watches, collection requirements, retention policies |
| `forecast_ledger_api.py`, `forecast_scorecard_api.py`, `forecast_records_api.py`, `forecast_resolution_api.py`, `seal_proof_api.py` | `/api/v1/v3` | The forecast ledger, scorecard, resolution records and decisions, and seal proofs |
| `map_layers_api.py` | `/api/v1/v3` | The map's live-series and at-sea reads |
| `eval_runs_api.py` | `/api/v1/v3` | `GET /v3/eval/runs`: published evaluation runs, every version |
| `read_events_api.py` | `/api/v1` | Read telemetry: batch append and rollup |
| `runtime_telemetry_api.py`, `budget_api.py`, `export_api.py` | `/api/v1`, `/api/v1/budget`, `/api/v1/v3` | Actor health, budget envelopes, export |
| `metrics_api.py` | app root | Prometheus text exposition, without a bearer so a scraper needs no token |
| `health.py` | none | Liveness and readiness |

### 2.3 `analysts/` kind modules

A kind module declares `KIND_NAME`, and `discover_analyst_kinds()` registers it.
Most kinds are `AnalystKind` enum members. The rest (`journal_assessor`,
`entity_researcher`, `signal_salience`, `situation_tracker`, `inquiry`) register
through the vocabulary tables, which is how a kind is added without editing the
enum.

| Module | Kind | What it does |
|---|---|---|
| `inline_target.py` | `inline_target` | The bounded unit: one question over one desk's slice, with cited synthesis and a verify pass |
| `meta_findings_synthesizer.py` | `meta_findings_synthesizer` | Country, region, thematic and world compositions; entry point in `meta_findings_runner.py` |
| `deterministic.py` | `deterministic` | Dispatcher for the model-free handlers; `SUB_HANDLERS` comes from `data/type_registry/` |
| `journal_assessor.py` | `journal_assessor` | The journal voice; writes only `journal_entries`, outside the lineage catalog |
| `inquiry.py` | `inquiry` | The stateful journal voice, with an `inquiry_ledger` read at plan time and written at reflect time |
| `competing_hypotheses.py` | `competing_hypotheses` | Competing hypotheses scored on an evidence matrix, resolved against `resolved_outcome` |
| `relationship_reifier.py` | `relationship_reifier` | Types co-mentioned entity pairs into signed nexuses |
| `critic.py` | `critic` | Scores another analyst's output with a model judge |
| `optimizer.py` | `optimizer` | Runs the GEPA loop (§3); refuses for the mothballed ids (SEAMS #53) |
| `predictor.py` | `predictor` | Forecast-as-claim; its descriptors are retired |
| `cross_target_raw.py`, `cross_analyst_correlator.py` | same names | Cross-target raw reads; cross-analyst correlation |
| `consult_on_demand.py`, `deep_consult.py` | same names | Consult; the `consult_*.py` siblings are listed below |
| `entity_researcher.py` | `entity_researcher` | Researches an unresolved entity through the governed packs |
| `signal_salience.py` | `signal_salience` | Per-signal consequence scoring; `salience_institutional.py` applies the institutional-change lift |
| `situation_tracker.py` | `situation_tracker` | The one writer of the situation trajectory ledger |

The Consult siblings:

| Files | What they do |
|---|---|
| `consult_round_protocol.py`, `consult_system_prompt.py`, `consult_prompt_context.py` | Round protocol, system prompt and per-run context |
| `consult_context_ceiling.py` | Withholds old tool results once a call would pass `context_ceiling_tokens` |
| `consult_provenance_census.py`, `consult_evidence_floor.py` | Counts cited refs by origin class; floors stated uncertainty by how many refs resolve |
| `consult_watch_mint.py`, `consult_watch_fold.py` | Turns an answer's watch candidates into open questions, folding near-duplicates |
| `consult_judging.py` | Sends an answer's cited sentences through the faithfulness check; first rung `LEGBA_CONSULT_JUDGE_STACK_REF` |
| `consult_case_schema.py`, `consult_case.py`, `consult_tool_routing.py` | Presenting a case: the drive tools, the case gate and round routing |
| `consult_tool_providers.py` | One `ConsultToolProvider` per pack: tool specs, prompt fragment and gate |
| `consult_write_gate.py` | The write gate on an armed turn; execution happens in `registry/consult_write_exec.py` |
| `consult_spend_guard.py`, `consult_turn_cost.py` | The usage receipt and the optional per-turn cost ceiling |
| `consult_bakeoff.py`, `consult_bakeoff_prompts.py` | Read-pack variants used for evaluation runs |

### 2.4 `analysts/` families

Each family was split out under the module-size gate
(`tests/test_module_size_gate.py`), which caps the line count of large modules.

| Family | Modules | What it owns |
|---|---|---|
| Assembly | `assembly_*.py` | The quotation assembly a composition writes: spans, payload, render, salience, block caps, the world floor |
| Assessment | `assessment_*.py`, `off_roster_activity.py`, `off_roster_aperture.py` | The labelled assessment generated from an assembly's own spine |
| Composition | `composition_*.py`, `window_ledger.py`, `region_rollup.py`, `thematic_theme.py` | Composition slice, evidence window, deterministic assembly, prompts, coercion, correctness gate, themes |
| Slice and render | `slice_render.py`, `wire_pair_collapse.py`, `spread_block.py`, `source_independence.py`, `unit_grounding*.py`, `history_grounding.py`, `unit_names.py` | Slice statistics, wire-story collapse, source independence counts, unit grounding blocks |
| Journal | `journal_slice.py`, `journal_clusters.py`, `journal_reflect.py`, `journal_leak_guards.py` | The journal's slice, clustering, reflection and leak guards |
| Gather and planning | `gather_native.py`, `gather_surface.py`, `gather_context_budget.py`, `planner_action.py`, `output_contract.py` | The native tool-round GATHER path, its evidence cap, the planner action and output contract |
| Misc leaves | `claim_contradiction.py`, `research_regime.py`, `_llm_budget.py`, `_tradecraft.py` | Claim contradiction, the research regime, per-run model budget, tradecraft references |

`agency/` is the action-pack plane. `agency.py:run_pack_tool` is the gate every
tool call passes: resolve, check the tool, validate arguments against the schema
the model saw, govern, record, dispatch. Beside it: `governor.py` (per-pack caps),
`resolution.py` (which packs an analyst may use), `binding.py`, `tools.py`,
`web_tools.py`, `write_tools.py`, `substrate_read.py`, `research_tools.py`,
`robots.py` (fails closed when robots.txt is unreachable), `search_cost.py`,
`workstation_drive.py` and `consult_write.py`.

### 2.5 `analysts/deterministic_handlers/`

`deterministic.SUB_HANDLERS` has one entry per declaration in
`data/type_registry/deterministic_handlers/`. Every other module in the
directory is a helper of one of those.

| Group | Modules |
|---|---|
| Analysis spine | `scorecard_producer.py`, `scorecard_banding.py`, `unit_correctness_scorer.py`, `composition_lineage_sweep.py`, `finding_supersession.py` |
| Indicators | `indicator_tracker.py` with `_indicator_debounce.py` and `_indicator_flip_store.py`; `collection_gap.py` |
| Source layers | `layer_divergence.py`, `_layer_fold.py`, `_layer_divergence_reads.py`; the map's error rate is not measured (SEAMS #60) |
| Correctness grading | `correctness_grader.py` with `_correctness_*.py` and `_challenge_detect.py` |
| Reference building | `reference_builder.py` with `_reference_*.py`; `reference_deterministic.py` with `_reference_keyed.py` |
| External audit | `standing_auditor.py` with `_external_audit_*.py`; it searches through the `web_access` pack only |
| Attention | `desk_reference.py`, `_reference_diff.py` |
| Alerting and watch | `alert_trigger_scan.py` and its `_*_scan.py` siblings, `claim_watch.py`, `watch_horizon_sweep.py`, `bearing_gate.py`, `bearing_shadow.py` |
| Entity and graph | `entity_resolution.py`, `entity_gc.py`, `proposed_edge_governance.py`, `graph_mining.py`, `structural_balance.py`, `graph_projector.py`, `profile_projector.py`, `nexus_decay.py` |
| Facts, claims, situations | `fact_decay.py`, `fact_decay_scan.py`, `fact_contention_arbiter.py`, `situation_clustering.py`, `hypothesis_lifecycle.py`, `integrity_sweep.py` |
| Dedup and coalescing | `cross_source_dedup.py`, `cross_source_coalesce.py` (off by default; SEAMS #19) |
| Corpus and enrichment | `corpus_indexer.py`, `corpus_retention.py`, `signal_summarizer.py`, `signal_embedder.py`, `reenrich_*.py` |
| Calibration and baselines | `calibration_tracking.py`, `band_calibration_tracker.py`, `desk_baseline.py`, `source_track_record.py`, `forecast_acute.py`, `forecast_scoreboard.py` |
| Retention and archive | `_retention_sweep.py`, `signals_retention.py`, `analyst_traces_retention.py`, `evidence_archiver.py`, `archive_retention.py` |
| Research | `_research_dispatch.py`, `research_measurement.py`, `_reference_gap_dispatch.py` |
| Other | `narrative_mapper.py`, `adversarial_signals.py`, `anomaly_detection.py`, `_title_frame_gauge.py` |

### 2.6 `provenance/` verify pass and judge

`verify.py` holds the lineage checks (`verify_provenance_complete`,
`validate_lineage`) and the faithfulness check (`verify_finding_faithfulness`).
New logic goes into a sibling module, because `verify.py` is at its size limit.

| Module | What it owns |
|---|---|
| `judge_evidence.py`, `judge_assessability.py` | The evidence view the judge grades against; claim shape and score state |
| `judge_input_checks.py`, `judge_quote_rules.py`, `judge_absence_rubric.py` | Checks against what a composition was shown, the quote contract, the absence rubric |
| `judge_verdict_parsing.py` | Verdict extraction and reason codes |
| `judge_transport.py`, `judge_pipeline_version.py`, `judge_pipeline_lineage.py`, `judge_floor_escalation.py` | The judge call, the pipeline-version stamp, and judging a finding before the floor excludes it |
| `uncited_share.py` | The share of fact-asserting sentences with no citation, stamped on every unit critique |
| `grounding_backstops.py` | Deterministic backstops run before the judge |
| `composition_integrity.py`, `tension_pairing.py` | Grades a composition against the desk reads it cites; pairs blocks for a cross-unit tension |
| `world_knowledge_guards.py` | Guards backed by the profile gazetteer; without it they answer `not_applicable` (SEAMS #67) |
| `assembly_arms.py`, `assembly_floor_arm.py` | Four deterministic audits of the assembly payload |
| `text_fold.py`, `citation_markers.py`, `event_citations.py` | Text normalisation, citation-marker parsing, `event:<uuid>` expansion |
| `absence_slice.py`, `assessment_aperture.py`, `bearing.py`, `structural_claims.py`, `value_clustering.py`, `assessment_weighting.py` | Absence slice, assessment aperture, bearing, structural claims, contested-value clustering |
| `external_grades.py`, `external_span_check.py`, `external_truth.py`, `round_lineage.py` | The external-grading ledger and its evidence rules |
| `consumption.py`, `entity_edge_writes.py` | Forward lineage and entity-edge writes |

---

## 3. `src/legba/runtime/`

The entry point is `dapr_host.py`.

| Family | Its job | Start from | Notable files |
|---|---|---|---|
| Actor host | Turns descriptors into running Dapr actors | `dapr_host.py` | `dapr_actors.py` (`TargetActor`, `AnalystActor`), `source_actor.py` (`SourceActor`, `SourceCore`), `actor_turn.py`, `actor_critic.py`, `source_first_runtime.py` |
| Source polling | One poll from fetch to stored signal | `source_actor.py` | `source_keyword_gate.py`, `source_poll_outcome.py`, `source_cursor.py`, `fetch_receipts.py`, `source_poll_records.py` |
| Sinks | Route a handler's records by `sink.family` | `sinks/__init__.py` | `sinks/signal.py` (documents into `signals`), `sinks/observation.py` (live series into `observations`); SEAMS #77 |
| Reconcile | Converges running actors onto the registry | `reconcile.py` | `nats_informer.py`, `lifecycle.py`, `state.py`, `registry_client.py`, `vocabulary_runtime.py`, `reminder_gc.py`, `inbound_drain.py` |
| `subscription/` | Source-to-target fan-out | `engine.py` | `sourceref.py`, `policy.py`, `subjects.py`, `filter.py` (SQL plus a Starlark residual), `backfill.py` |
| `triggers/` | The coalescing trigger plane | `engine.py` | `coalescer.py`, `dispatch.py`, `policy.py`, `state.py` |
| `jobs/` | The NATS work queue and its workers | `queue.py` | `worker.py`, `dispatch.py`, `process_media.py`, `media_client.py` |
| `dapr_workflow/` | Durable multi-hour workflows | `worker.py` | `gepa.py`, `workflow.py`, `client.py`, `deep_consult*.py`, `dspy_lm.py`; dspy exists only in this worker image |
| Wiring and factories | Builds per-actor dependencies and ports | `analyst_deps_builder.py` | `analyst_deps_kinds.py`, `analyst_method.py`, `deps.py`, the `*_factory.py` modules, `pipeline.py`, `budget.py` |
| Substrate reads | The read port the analysts and tools use | `substrate_query_port.py` | `substrate_*.py` siblings, `_observations_read.py` (collection series), `_observations_live_read.py` (live series) |
| Judge chain | The fallback chain of judge routes | `judge_fallback_chain.py` | `external_grader_route.py`, `external_audit_binding.py`, `rejudge_floor.py` |
| Grounding | Adds current facts to the prompt before the model call | `grounding.py` | `grounding_scope.py` (which `world_context` filter a target gets), `rag_rollback.py` |
| Source health | Feed staleness and its alerts | `source_staleness.py` | `source_staleness_watch.py`, `source_stall_diagnosis.py`, `substrate_source_health.py`, `enrichment_timing.py` |
| Intake | On-request ingestion | `intake_drain.py` | `intake_run.py`, `intake_context.py`, `intake_follow.py`, `intake_drift.py` |
| Subscribe mode | Long-lived stream connectors (`LEGBA_SUBSCRIBE_SUPERVISOR`, off) | `stream_supervisor.py` | `stream_connector.py`, `stream_transport.py`, `stream_sse.py`, `stream_ingest.py`, `stream_liveness.py` |
| Liveness and leadership | One writer per role, and stall detection | `liveness_watchdog.py` | `leader.py`, `search_degraded_watch.py`, `alert_volume_watch.py`, `llm_route_liveness.py` |
| Consult runtime | Consult bindings and write client | `consult_bindings.py` | `consult_writes_client.py`, `dispatched_question.py` |
| Shadows | Runtime ends of the desk-rows shadow and the held-record feed, both off | `desk_rows_shadow_wiring.py` | `held_record_loop.py` |

### 3.1 The four planes

`source_first_runtime.py` assembles the planes the host boots on top of the
substrate and the reconcile loop:

1. Acquisition: `source_actor.py`, `data/sources/baseline.py`, `subscription/`.
2. Analysis: `TargetActor` and `AnalystActor`, `triggers/`, `data/analysts/agency/`, `budget.py`.
3. Async jobs: `jobs/`.
4. Substrate: the `data/` ports.

`SourceActor` polls on a Dapr reminder or receives pushes through the webhook
router, runs the source's baseline pipeline, writes the canonical signal and
publishes it. Each source is ingested once, however many targets subscribe.

---

## 4. The console: `legba-ui-v3/`

A Vite, React and TypeScript single-page app with Tailwind and Dockview panels.
The `legba-ui-build` service builds it and `legba-caddy` serves it and proxies
the registry API. Vitest tests sit beside the code as `*.test.ts(x)`.

| Area | Its job | Start from | Notable files |
|---|---|---|---|
| App shell | Bootstrap, routing, sign-in | `App.tsx` | `main.tsx`, `auth/` (`SignInGate.tsx`, `session.ts`, `roles.ts`), `state/` (`selection.ts`, `scope.ts`) |
| `lib/` | API client, live tail, view models | `api.ts` | `ws.ts`, `applyAction.ts` (the one action executor), `commands.ts`, `surfaces.ts`, `profileUi.ts`, `readTelemetry.ts`, `citationsModel.ts` |
| `panel-registry/` | The panel registry | `registry.ts` | `descriptions.ts`, `kindPlan.ts`, `aliases.ts` (retired kind to survivor, applied when a saved layout loads) |
| `components/` | Shared chrome | `PanelChrome.tsx` | `CommandPalette.tsx`, `ObjectRail.tsx`, `SurfaceBar.tsx`, `ConsultDock.tsx`, `RecordSheet.tsx`, `inspector/`, the badges |
| `panels/` | The panel set | (per area) | `source/`, `target/`, `analyst/`, `registry/`, `system/`, `ledger/`, `merged/`, `flow/` (the read-only Flow canvas) |
| `v4/` | The rooms shell, the default front door | `world/` | `world/` (the banded map), `flow/`, `why/`, `read/`, `case/`; distinct from `panels/v4/` |
| `mobile/` | The phone app at `/m/` (entry `m.html`, `vite.config.mobile.ts`) | `MobileApp.tsx` | `shell/`, `screens/`, `pwa/`, `mobileModel.ts`, `mobileCache.ts` |

Panel tiers (live, preview, hidden) are generated from `registry.ts` into
[RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md). Console behaviour is in
[UI.md](UI.md).

---

## 5. Entry points, infrastructure, descriptors, scripts

### Console scripts (`pyproject.toml [project.scripts]`)

| Script | Target | Role |
|---|---|---|
| `legba-registry` | `legba.data.registry.server:main` | The registry HTTP/WS API |
| `legba-runtime-dapr` | `legba.runtime.dapr_host:main` | The Dapr actor host |
| `legba-dapr-workflow-worker` | `legba.runtime.dapr_workflow.worker:main` | The durable-workflow worker |
| `legba-mcp` | `legba.ui.mcp_server:main` | The MCP server |

Migrations apply with `python -m legba.data.migrate` (`--dry-run` lists pending
files without applying them). A fresh deploy loads the shipped baseline
(`deploy/baseline/`) and then runs this runner; `deploy/deploy.sh` does both.
The runner records each applied file in `legba_data_migrations` and skips it on
a re-run, so a schema change is always a new file. The substrate smoke check is
`python -m legba.data.smoke` (`--json` for machine output). Bootstrapping the
registries needs `LEGBA_DATA_REGISTRY_DSN`, `LEGBA_DATA_MASTER_KEY` and the
`LEGBA_DATA_DEFAULT_*` component ids; `.env.example` lists the rest.

### Docker, Dapr, deploy

- `docker-compose.yml`: the substrate (`redis`, `postgres`, `qdrant`, `opensearch`, `nats`), the Dapr control plane, the Legba services, the optional services (`rsshub`, `searxng`, `ntfy`, `legba-media`) and the console pair (`legba-ui-build`, `legba-caddy`). Compose `profiles:` gate the optional services.
- `docker/`: `Dockerfile.registry`, `Dockerfile.runtime`, `Dockerfile.worker` (built from the runtime image), `Dockerfile.mcp`, `Caddyfile`, `nats.conf`, `searxng/settings.yml`.
- `dapr/components/`: `statestore.yaml` (Postgres actor state), `pubsub.yaml`, `secretstore.yaml`, `configuration.yaml`.
- `deploy/`: `deploy.sh`, `baseline/`, `systemd/`, `cron.d/`, `prometheus/legba_alerts.yml`, `swarm/`, `compose.isolation.yml`.
- `legba-models/`, `legba-media/`: the two model services, each with its own Dockerfile, compose file and `app/main.py`.

### `descriptors/`

Tracked YAML files by prefix: `source_*`, `analyst_*`, `target_*`,
`action_pack_*`, `layer_*`, `stack_component_*`, `discovery_*`, `template_*`,
`vocabulary_*`, `collection_*`, plus `wire_map.yaml`. `LIFECYCLE.md` and
`LIFECYCLE.d/` record the state the example profile's bring-up gives each file,
and `RETIRED.md` lists retired ids. Collection holdings must use the
`collection_` prefix, because the source loader picks up every `source_*.yaml`
as a live feed.

A file in the tree is the pinned definition; the registry holds the live head.
Activation is an admin's act through the lifecycle route, so a descriptor that
ships `draft` and runs `active` is expected. The live sets are registry rows:
the catalog and desk registrars write some sources and targets directly, so
`ls descriptors/` does not list them all. [STATUS.md](STATUS.md) gives the state
of each part.

Read the analysis descriptors first: the bounded units
(`analyst_leadership_transition.yaml`, `analyst_energy_security.yaml`,
`analyst_escalation.yaml`, `analyst_narrative_coordination.yaml`,
`analyst_internal_stability.yaml`, `analyst_military_posture.yaml`,
`analyst_economic_coercion.yaml`, `analyst_proliferation_watch.yaml`,
`analyst_disruption_status.yaml`), the compositions
(`analyst_country_composition.yaml`, `analyst_region_composition.yaml`,
`analyst_world_assessor.yaml`, `analyst_escalation_composition.yaml`,
`analyst_escalation_dyad.yaml`, `analyst_hybrid_threat_composition.yaml`), the
assessments (`analyst_country_assessment.yaml`, `analyst_world_assessment.yaml`),
the indicator pair (`analyst_indicator_tracker.yaml`,
`analyst_collection_gap.yaml`), the measurement set
(`analyst_scorecard_producer.yaml`, `analyst_unit_correctness_scorer.yaml`,
`analyst_correctness_grader.yaml`, `analyst_reference_builder.yaml`,
`analyst_reference_deterministic.yaml`, `analyst_standing_auditor.yaml`,
`analyst_desk_reference.yaml`, `analyst_forecast_scoreboard.yaml`) and the
journal set (`analyst_journal_assessor.yaml`,
`analyst_journal_consolidator.yaml`, `analyst_chronicle_assessor.yaml`,
`analyst_lens_*.yaml`).

### `scripts/`

Scripts reach the registry one of two ways, and a script uses only one.
`_p17_registrar.py` writes to the database directly through
`DescriptorRegistry`, and sets a process-wide database default at import.
`_bringup_http.py` talks REST to a running registry. Importing the database
helper into a REST-only tool would give it a database selection it never asked
for.

| Group | Scripts | What they do |
|---|---|---|
| Registrars | `bringup_register_descriptors.py`, `bringup_register_stack.py`, `bringup_register_*.py` | `bringup_register_descriptors.py` is the one `deploy.sh` runs: it registers the profile's descriptors at their `LIFECYCLE.md` states; see [SETUP.md](SETUP.md) |
| Bring-up | `bringup_source_first_host.py`, `bringup_vault_load.py`, `bringup_set_budget_envelope.py`, `bringup_tag_targets.py`, `quick_start.py` | Boot the host, load vault secrets, set the budget envelope, tag targets |
| Seeding | `seed.py`, `seed_source_ratings.py`, `seed_sources.py`, `seed_import.py`, `export_substrate.py`, `export_seed_v2.sh`, `load_forecasts.py` | Run `data/seed/`, load ratings, export and re-import a substrate, load forecasts |
| Manual ingest | `manual_ingest.py`, `manual_ingest_vectors.py`, `load_unit_reference.py` | Structured and vector manual ingest; a hand-loaded reference |
| Collections | `verify_collection_manifest.py`, `load_collection.py` | Check a manifest against its providers without writing; load rows into `observations`; see [COLLECTIONS.md](COLLECTIONS.md) |
| Backfills | `backfill/`, `backfill_corpus.py`, `backfill_entity_graph.py`, `backfill_entity_canonicalization.py`, `backfill_gazetteer_geo.py` | One-shot repairs over existing rows |
| Deploy and release | `rolling_deploy.sh`, `deploy_tier.py`, `run_targeted_gate.sh`, `release_gate.sh`, `release_smoke.sh`, `prepush_scan.sh`, `generate_release_manifest.py`, `backup.sh`, `restore_drill.sh` | Deploys, the release gate, the secret scan, the manifest, encrypted backups and the restore drill |
| Generators | `gen_release_state_matrix.py`, `gen_descriptor_prompt_manifest.py`, `gen_not_published.py`, `gen_api_roles_doc.py`, `gen_profile_*.py` | Each writes a committed file, and a test fails when that file is stale |
| Host cron | `host_search_canary.sh`, `host_stall_watchdog.sh`, `host_llm_heartbeat.sh`, `host_log_collector.sh`, `host_nightly_suite.sh`, `models_host_vllm_watchdog.sh` | Watchdogs and probes installed from `deploy/cron.d/` |
| Evaluation and replay | `measure_*.py`, `*_replay.py` and the evaluation folders | Offline evaluation and replay scripts; not part of the run path |
| Reports | `report_written_by.py`, `report_origin_groups.py`, `derive_owner_groups.py`, `citation_rows_report.py`, `held_record_verify.py`, `held_record_replay.py`, `audit_chain.py` | Reports on writers, origin groups and citation rows; held-record and audit-chain checks |
| Ops | `fleet_stop.sh`, `fleet_start.sh`, `sweep_orphan_actors.py`, `diagnose_stale_leaders.py`, `ops_pause_*.py`, `correctness_grade_now.py`, `reference_build_now.py` | Routine admin tasks; the procedures are in [RUNBOOK.md](RUNBOOK.md) |
| Tests | `run_tests.sh`, `run_tests_in_container.sh`, `run_ui_tests_in_container.sh`, `deploy_smoke_cold_activation.sh` | Test suite entry points |

### `tests/`

`data_pkg/` is the largest directory, beside `runtime/`, `descriptors/`,
`clients/` and a few top-level tests. `scripts/host_nightly_suite.sh` runs the
suite nightly. Three top-level tests are gates:
`test_no_undeclared_stubs.py` (reads the allowlist in [SEAMS.md](SEAMS.md)),
`test_module_size_gate.py` (caps the line count of modules that were already
large) and `test_runtime_no_dspy_litellm.py` (keeps dspy and litellm out of the
runtime and analyst paths).

---

## 6. Where to add a thing

| Goal | Add here | Then |
|---|---|---|
| A source kind | A handler in `data/sources/` implementing the `_contract.py` Protocol | Declare the kind in `data/sources/kinds/` (or the `legba.source_kinds` entry point); write a source descriptor |
| A declared type | One module in `data/type_registry/<family>/` | See [ADDING_A_TYPE.md](ADDING_A_TYPE.md) |
| An analyst kind | A module in `data/analysts/` declaring `KIND_NAME` | Add it to `AnalystKind` or register it through the vocabulary; wire it in `runtime/analyst_method.py` and `runtime/analyst_deps_builder.py` |
| A deterministic sub-handler | A module in `data/analysts/deterministic_handlers/` | Add one declaration module in `data/type_registry/deterministic_handlers/`; write the descriptor |
| A filter or enricher | A handler in `data/filters/` implementing `StreamHandler` | Reference it from a source's `pipeline` |
| An output sink | A handler in `data/outputs/` implementing the `AlertEmitter` Protocol (plus a sub-sink in `alert_sinks/` for alerts) | Bind it through a target or analyst `OutputBinding` |
| A migration | A time-named `data/migrations/YYYYMMDDTHHMM_slug.sql` (UTC) | Apply with `python -m legba.data.migrate` |
| A predicate helper | `data/predicates/helpers.py` | Usable in source-selector and subscription Starlark |
| A provider adapter | A module under `data/stack/<family>/` | Register a `StackComponentDescriptor` |
| A discovery kind | A module in `data/discovery/` and its materializer entry | Write a `discovery_*.yaml` |
| A curated seed source | An adapter in `data/seed/adapters/` implementing `SeedSource`, and a dataset under `seeds/` (gitignored) | Add it to `ADAPTERS` in `data/seed/__init__.py`; run `scripts/seed.py --source <name>` |
| A console panel | A `.tsx` under `legba-ui-v3/src/panels/<area>/` | Register it in `panel-registry/registry.ts`, then run `scripts/gen_release_state_matrix.py` |
| Something deliberately not built | Nothing in `src/` | Declare it in [SEAMS.md](SEAMS.md) with its guard, and add its `file:symbol` line to the allowlist if the scanner flags it |
