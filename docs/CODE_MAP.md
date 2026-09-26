<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Code map

Where each concern lives. One row per package or module family, with the job it
does and the file to start from. Every path here is a tracked path in this
repository.

This file answers "where is the code". For what the platform is and why it has
this shape read [ARCHITECTURE.md](ARCHITECTURE.md) and [ANALYSIS.md](ANALYSIS.md);
for the implementation rules read [DESIGN.md](DESIGN.md); for running it read
[RUNBOOK.md](RUNBOOK.md). What is built but not live is in
[SEAMS.md](SEAMS.md) and [STATUS.md](STATUS.md) — presence in the tree is not a
claim that a thing runs.

Cite code as `module.symbol`, never by line number: `grep` resolves a symbol and
it survives every split. Counts in this file are counts of files in the tree,
which a reader can re-derive with `git ls-files`; live row counts are not here,
because they cannot be checked from a checkout.

**Contents:** [1 Top-level layout](#1-top-level-layout) ·
[2 data/](#2-srclegbadata--the-declarative-model-and-the-substrate) ·
[3 runtime/](#3-srclegbaruntime--execution) ·
[4 UI](#4-the-ui--legba-ui-v3) ·
[5 Entry points, infra, descriptors, scripts](#5-entry-points-infra-descriptors-scripts) ·
[6 Where to add a thing](#6-where-to-add-a-thing)

---

## 1. Top-level layout

```
src/legba/            the platform (Python package)
legba-ui-v3/          the React/TypeScript single-page UI
legba-models/         the model service (vLLM LLM + bge-m3 embeddings + NLLB/spaCy NER)
legba-media/          the media-extraction service (serves the media seam; 503s with no backend)
descriptors/          example + operator-pinned source/target/analyst/pack/stack/discovery YAML
scripts/              registrars, seeders, backfills, measurement harnesses, ops shell
docker/               Dockerfiles (registry / runtime / worker / mcp) + Caddyfile + nats.conf
dapr/components/      Dapr component manifests (statestore, pubsub, secretstore, configuration)
deploy/               bring-up script, systemd units, cron lines, Prometheus rules, swarm stacks
seeds/                curated-dataset drop point (only the *.example.yaml files are tracked)
tests/                the test suite
docs/                 design and reference docs (this file)
docker-compose.yml    the whole stack, profile-gated
pyproject.toml        package metadata + console scripts
```

The package splits on one seam. `src/legba/data/` is the **declarative and
substrate** half: descriptor schemas, the registry, the handler libraries, the
store adapters, the migrations. `src/legba/runtime/` is the **execution** half:
the Dapr actor host, the four runtime planes, the reconcile loop.

The split is by responsibility, not by a hard import boundary. `data/` does
reach into `runtime/`, and almost entirely for one thing: an analyst kind
module imports the shared result and handler types from
`runtime/analyst_method.py`, which is why that module is a 68-line leaf. The
handful of other crossings are named at their call sites.

---

## 2. `src/legba/data/` — the declarative model and the substrate

### 2.1 Package index

| Package | Its job | Start from | What lives there |
|---|---|---|---|
| `schemas/` | The descriptor types: strict, `extra="forbid"`, content-hashable pydantic | `source.py` | `SourceDescriptor`/`Subscription`/`SourceRef`/`SourceDeps` (`source.py`), `TargetDescriptor` + polymorphic `TargetScope` + `OutputBinding` (`target.py`), `AnalystDescriptor` + the open `AnalystKind` + the optional `GroundingBlock` (`analyst.py`), `ActionPack`/`ActionPackRef`/`PackGovernor` (`action_pack.py`), `CollectionDescriptor` + its own four-state lifecycle and the eight-surface `FirewallBlock` (`collection.py` — a bounded HOLDING of the past, not a feed), `StackComponentDescriptor` (`stack.py`), the lifecycle FSM + `AbstractionLevel` (`lifecycle.py`), shared property types (`properties.py`), vocabulary shapes (`vocabulary.py`), version + content-hash helpers (`versioning.py`), the journal JSON-Schema (`iglu/`) |
| `registry/` | The control plane: descriptor registry, credential vault, HTTP/WS API | `server.py` | 69 modules. The registry core (`descriptor.py` + the per-family INSERT and lifecycle state machines it delegates to, `descriptor_families.py`; `audit.py`, `signing.py`, `dlq.py`, `events.py`/`streams.py`/`emitter.py`, `vocabulary_cache.py`, `stack.py`/`stack_events.py`), the XSalsa20-Poly1305 vault (`credentials.py`), the `legba-registry` FastAPI app (`server.py`) and its 34 routers (§2.2), and the non-router judgment modules the routers share (`production_gauge*.py`, `source_freshness.py`, `scorecard_reconcile.py`, `consult_persistence.py`/`consult_runs.py`, `goldset_sampling.py`, `journal_proposals_apply.py`, `rate_limit.py`, `health.py`, `discovered_materializer.py`, `conversion.py`, `descriptor_refs.py`, `errors.py`) |
| `sources/` | The source-kind acquisition handler library | `_contract.py` | The handler Protocol + `Signal` (`_contract.py`, `_protocols.py`) — a Protocol, not an ABC, so an out-of-tree package registers a kind without importing a base class; the per-source baseline pipeline (`baseline.py`); 16 kind handlers (`acled`, `common_crawl`, `discord`, `firecrawl`, `gdelt`, `gdelt_files`, `generic_webhook`, `geojson`, `intelmq`, `json_api`, `mediacloud`, `opensanctions`, `rss`, `scraper` + `scrapers/`, `telegram`, `ucdp`); the shared inbound push router (`webhook_router.py`); outbound provisioning (`provision.py`); the egress guard every fetcher goes through (`_egress.py`, plus the flag-gated browser-fingerprint adapter `_egress_impersonate.py`) |
| `filters/` | In-flight enrichment and transforms over a `Signal` | `_contract.py` | The `StreamHandler` Protocol (`_contract.py`); baseline enrichers (`language_detect.py`, `geocode.py`, `ner.py`, `classify.py`, `source_credibility.py`); ingest dedup tiers 1–2 (`ingest_dedupe.py`, `dedupe.py`); fact extraction from relation triples (`fact_extractor.py`); the SLM refiners that call the model service (`slm_classification_refine.py`, `slm_entity_resolve.py`, `slm_relationship_validate.py`); leaf helpers (`_country_geometry.py`, `_fact_graph.py`, `_ner_entity_class.py`, `entity_candidate_port.py`) and the admin-0 geometry asset (`world_admin0.min.json`) |
| `analysts/` | Analyst-kind implementations, one module per kind, plus the families they were split into | `__init__.py` | 75 top-level modules + `agency/` (15) + `deterministic_handlers/` (103). `discover_analyst_kinds()` (`__init__.py`) finds the 16 modules that declare `KIND_NAME`. Detail in §2.3–2.5 |
| `analysts/` | Analyst-kind implementations, one module per kind, plus the families they were split into | `__init__.py` | 76 top-level modules + `agency/` (15) + `deterministic_handlers/` (100). `discover_analyst_kinds()` (`__init__.py`) finds the 17 modules that declare `KIND_NAME`. Detail in §2.3–2.5 |
| `provenance/` | `derived_from` chains, receipts, the verify pass, budget | `kinds.py` | The 14-member `OutputKind` enum + `KIND_REGISTRY` (`kinds.py`), per-kind payload models (`models.py`), the writers (`writes.py`, with the open-row supersession cluster in `writes_supersession.py`, and `_core.py`), the P7 origin-class vocabulary and gate renderers (`origin.py`), the SHA-256 hash-chained receipt chain (`receipts.py`), the faithfulness verify pass and the judge bricks it was split into (§2.6), the external-grading ledger (`external_grades.py`, `external_span_check.py`, `external_truth.py`, `round_lineage.py`), durable checkpoints (`checkpointer.py`), budget accounting (`budget.py`), the output dead-letter (`dlq.py`) |
| `outputs/` | Output-kind emit handlers: an analyst payload fanned to an operator surface | `_contract.py` | The `AlertEmitter`/emit Protocol + `discover_output_kinds()` (`_contract.py`, `__init__.py`); `substrate.py` (the typed write-back facade over `provenance/writes.py`), `nats_stream.py`, `webhook.py`, `alert.py` + `alert_sinks/` (`matrix.py`, `nats.py`, `pushover.py`, `xmpp.py`), `ui_panel.py`, `mcp_tool.py`, `a2a_skill.py`, `stix_bundle.py` + `taxii_client.py` (a real TAXII 2.1 push client; only the destination is unprovisioned — SEAMS #10) |
| `alerts/` | Outward alert fan-out — the second, separate alerting package | `sinks.py` | The `AlertSink` interface, the dispatcher and the one converged `AlertSinkPayload` (`sinks.py`), with `ntfy_sink.py` and `webhook_sink.py`. Distinct from `outputs/alert_sinks/`: the internal alert edge writes `alert_sink_deliveries`, this package is what pushes a delivery outward |
| `migrations/` | The SQL schema, applied in filename order | `0001_baseline.sql` | 132 `.sql` files. `0001_baseline.sql` is a flattened baseline (extensions, the Apache AGE graph, the relational tables, seed rows); the forward chain runs `0032` upward and is sparsely numbered, with `0214_source_layers.sql` the highest today. `migrate.py` globs `*.sql` in order, so a new migration is a next-numbered file and nothing else. Per-migration purpose lives in [DATA_MODEL.md](DATA_MODEL.md), not here |
| `events/` | The V3 event surface | `lifecycle.py` | The five-state lifecycle FSM and append-only ledger vocabulary the schema CHECKs mirror (`lifecycle.py`), the `evt:` signature builder and its Postgres twin (`signature.py`), the write-path SQL `provenance.writes` drives (`_writes.py`). P1's deterministic producers live in `analysts/deterministic_handlers/event_clustering.py` and `event_reconciler.py`; both stay draft and `LEGBA_EVENTS`-gated |
| `layers/` | The per-country LAYER TABLE + APERTURE DECLARATION (migration 0214) | `_vocab.py` | The closed six-layer vocabulary + the three-state aperture-declaration vocabulary, a leaf (`_vocab.py`); the idempotent, re-versioning upsert into `source_layers`/`desk_apertures` (`loader.py`), called only by `scripts/load_layer_map.py` — no analyst, no route, no registry-lifecycle registration reads or writes either table yet. The `layer_map` descriptor kind's pydantic model lives in `registry/layer_map_schema.py` (not `schemas/` — it is not a registry-CRUD peer of Source/Target/AnalystDescriptor); `scripts/gen_layer_map_draft.py` derives a first-draft country map from `descriptors/source_*.yaml` + `descriptors/wire_map.yaml`, never loaded automatically. See [LAYERS.md](LAYERS.md) |
| `discovery/` | The descriptor-discovery pipeline: external lists and queries become descriptors | `registry.py` | Two flavours with two dispatch tables. **Target** kinds (`country_list_discovery.py`, `file_sd_discovery.py`, `static.py`) resolve through `registry._KIND_MODULE_NAMES` and are what an actor's discovery cycle runs. **Source** kinds resolve through `materializer._build_source_discovery_handler`; `query_source_discovery.py` is the only one. Plus the materializers (`materializer.py`, `source_materializer.py`), `autowire.py`, `relabel.py`, `deps_resolver.py`, `disappearance.py`, `source_validate.py`, `source_contract.py`, `_contract.py` |
| `predicates/` | The Starlark predicate DSL used by subscriptions and matching | `compiler.py` | Compile-once-on-register into an LRU-cached `CompiledPredicate`, single expression only, statements rejected (`compiler.py`); the in-sandbox evaluator (`evaluator.py`); the per-surface helper catalog (`helpers.py`); compile/eval errors (`errors.py`) |
| `stack/` | Provider adapters resolved through the stack registry | (per family) | `llm/` (`anthropic.py`, `vllm.py`, `openai.py`, `base.py`, `pricing.py`, `tool_rounds.py`, `tool_round_compaction.py`, `stream_observer.py`), `search/` (`searxng.py`, `brave.py`, `serper.py`, `json_generic.py`, `route.py`, `liveness.py`, `base.py`), `vector_store/qdrant.py`, `nats/jetstream.py`, `nlp_service/client.py`, `postgres/age.py`, `proxy/` (`bright_data.py`, `local_none.py`). There is deliberately no `embedding/` family: embeddings resolve through the hosted `embed.primary.openai_compat` endpoint, so no in-process embedding model ships |
| `seed/` | Curated baseline seeding and the manual-ingest lanes | `_base.py` | The `SeedSource` protocol + `SeedFact`/`SeedEntity`/`SeedNexus`/`SeedContext` (`_base.py`); `SeedDriver.run_seed_source` — fetch, map, resolve entities, `write_fact`/`write_nexus` stamped `source_type='seed'` + `seed_batch_id`, record the batch (`_driver.py`); the `ADAPTERS` registry (`__init__.py`) wiring four adapters in `adapters/` (`world_baseline.py`, `wikidata_leaders.py`, `acled_conflict.py`, `sipri_arms_transfers.py`); the structured manual-ingest lanes (`manual_batch.py`, `manual_schema.py`); and two curated readers that write no facts (`source_ratings_loader.py`, `exemplar_shelf.py`) |
| `rag/` | What populates the vector corpora | `lane4_loader.py` | The heading-aware chunker (`chunker.py`) and the manual-ingest vector loader (`lane4_loader.py`) — resolve, chunk, embed through the hosted embedder, upsert into the `world_context` / `tradecraft` collections. This is the write side; the read side is grounding (§3.4) |
| `facts/` | The fact confidence-decay model | `decay.py` | A pure library: it computes a derived `decayed_confidence` from stored confidence and time since last sighting, and mutates nothing. The stamping consumer is `deterministic_handlers/fact_decay_scan.py`, writing the `fact_decay_states` sidecar |
| `situations/` | Situation trajectory arithmetic | `trajectory.py` | The pure trajectory computation the `situation_tracker` kind and the trajectory read route share |
| `jobs/` | Async job envelopes and their store | `envelope.py` | `envelope.py`, `media.py` (`MediaExtractionResult` — a stub result is structurally unrepresentable), `store.py` |
| `tools/` | Analyst-callable external tools | `mnemosyne_trust_query.py` | The outbound trust-query tool |
| `conversions/` | Descriptor-version upgraders | `target_v1_to_v2.py` | `target_v1_to_v2.py`, `target_v2_to_v3.py`, `analyst_v1_to_v2.py`. They are example converters and the fixture surface for the conversion-webhook tests, so they earn their place at zero live webhook rows |
| substrate ports (`data/` root) | One typed port per backing store, plus process bootstrap | `config.py` | `postgres.py` (asyncpg + the AGE codec), `nats.py` (JetStream; `SIGNAL_STREAM_NAME` and the `legba.signals.<tenant>.<source>.<modality>.<event_class>` subject grammar), `qdrant.py`, `redis.py`, `opensearch.py` (a fifth port, importable on a host without `opensearch-py`). Alongside them: env-driven config (`config.py`), the migration runner (`migrate.py`), vocabulary seed/query (`vocabulary.py`), the substrate smoke check (`smoke.py`, which owns `RETIRED_TABLES`), the kind loader (`kind_discovery.py`), the archive path (`archive.py`) and graph path helpers (`graph_paths.py`) |
| shared leaves (`data/` root) | Stdlib-only modules both halves import | `_entity_canon.py` | The entity canon lives here, not under `analysts/`, so ingestion and the analyst plane share one canon without a layering violation: `_entity_canon.py` (the implementation), `_entity_candidates.py`, `_entity_resolve.py`, `_entity_eval.py`. Beside it: `_country_aliases.py` (name alias groups), `_fips_iso.py` (the FIPS 10-4 → ISO 3166-1 crosswalk GDELT is translated through at the boundary, because the desks route on ISO and GDELT ships FIPS), `_polity_match.py` (the polity/home-country matcher the coverage-floor detector and the reference diff both read), `_url_canon.py`, `_geo_routing.py`, `_frame_anchor.py`, `_frame_content.py`, `correctness_axis.py`, `correctness_reference_currency.py`, `critic_fold.py` (the ONE set-based "latest critique for this finding" SQL builder — twenty-two reads in both halves of the tree fold the critic through it), `research_evidence.py`, `research_flag.py`, `retrieval_origin.py`, `run_accounting.py`, `pinned_context.py` |
| `clients/`, `shared/` | The outbound A2A client and shared crypto | `mnemosyne_a2a.py` | `clients/mnemosyne_a2a.py`, `shared/crypto.py` |
| `prompts/` | Per-kind prompt modules — the `prompt_module` targets | (per kind) | 26 sub-packages, addressed **by string** from a descriptor's `prompt_module` field, which is why renaming one breaks at run time and not at import time: `inline_target/`, `meta_findings_synthesizer/`, `country_assessor/`, `cross_target_raw/`, `cross_analyst_correlator/`, `competing_hypotheses/`, `consult_on_demand/`, `critic/`, `predictor/`, `journal_assessor/`, `journal_consolidator/`, `chronicle_assessor/`, `inquiry/` (the stateful voice — persona, honesty rules and the state-block headers the kind's renderer reads), the function-typed faculty-lens set `lens_baserate/`, `lens_capability/`, `lens_diff/`, `lens_intent/`, `lens_trend/`, and the stance-typed leans `lens_left/`, `lens_right/`, `lens_centre/`, `lens_pragmatist/`, `lens_militarist/`, `lens_isolationist/` — all over the shared `lens_common/` |

### 2.2 `registry/` — the routers

`server.py` is the authority on what is mounted; it includes 34 routers. The
prefix column is the mount, not the route.

| Router module | Mount | Concern |
|---|---|---|
| `api.py` | `/api/v1/registry` | Descriptor CRUD, sources, action packs, stack, vault. The ~5-name kernel its siblings need (`RegistryAPIDeps`/`_get_deps`, `require_bearer`, `sunset_headers`, `_authorize_ws_token`) lives in the leaf `_deps.py` and is re-exported here, so a sibling gets the kernel without importing the whole HTTP surface |
| `v3_api.py` | `/api/v1/v3` | Runtime actor rows, eval reads, UI v3 views. `escalation_delivery.py` holds its escalation-delivery models and reducer — imported back and aliased, not mounted |
| `since_api.py`, `timeline_api.py`, `narratives_api.py`, `situation_trajectory_api.py`, `events_api.py` | `/api/v1/v3` | Change-since reads, the timeline (facts / situations / findings / events), the narrative/echo graph, situation trajectories; the P6 event surface (`GET /v3/events` paged, `GET /v3/events/{id}` dossier, `GET /v3/events/{id}/lifecycle` ledger) |
| `source_assurance_api.py`, `source_quality_api.py`, `source_credibility_api.py` | `/api/v1/v3`, `/api/v1` | The source ratings/dossier ledger, the one source-quality read view, per-source credibility. `source_freshness.py` is the shared cron reader all three lean on — cadence → interval → budget → grade, plus `next_fire_after` (when does this next run), one implementation and now three readers |
| `absence_api.py` | `/api/v1/v3` | TYPED ABSENCE as one named thing (`GET /v3/absence?scope=`): the audit's scoped absence and its failed searches, the reads' below-floor dimensions, the declared-absent source layers, silent units and silent sources answered for one desk (plus the collections `history_gap`: a curated collection declares a series-and-subject for this desk and `observations` does not hold it, proved by the LOAD receipt that should have written it) in ONE closed eight-kind vocabulary. Every item carries a proof (`what_was_checked` / `checked_at` / `ref` + `ref_kind`) and a clock (`as_of` = when it was MEASURED, never `now()`; `expires_at` = the next run of whatever measured it; `stale` = that moment already past). Each kind reads independently: one that cannot be read for a scope is named in `not_measured` at HTTP 200, so "not checked" and "nothing absent" never collapse. `BOUNDED_UNITS` mirrors `gapStripModel.ts`'s unit table and the drift is pinned by a test that parses the TypeScript |
| `collections_api.py` | `/api/v1/v3` | The ERA COVERAGE MAP (`GET /v3/collections/coverage?scope=`): per series, the valid-time spans a curated collection's manifest DECLARES the provider holds against the spans `observations` actually holds, and the holes between them. The load ledger answers "did the load finish"; this answers "which YEARS are on record", which is the question a reader of a cited historical number has. Four statuses kept deliberately apart — complete, holes, declared-but-not-loaded, and `provider_holds_nothing` (an absence at the SOURCE, carried with the manifest's own reason and never restated as ours). The comparison itself is `collections_coverage.py`, shared with the `history_gap` typed absence on `/v3/absence` — one reader, two surfaces, so the map and the absence cannot tell a reader two different stories about the same silence |
| `production_gauge_api.py` | `/api/v1/v3` | The expected-vs-actual production gauge, worst first. Its expectation model is `production_gauge.py`, which the `production_deficit` alert-trigger class reads too — one implementation, two readers, no mirrored SQL. `production_gauge_integrity.py` adds the integrity loops (judge availability, descriptor prompt drift, descriptor state drift) |
| `grader_roster_api.py` | `/api/v1/v3` | The correctness grader's ROSTER (`GET /v3/eval/grader_roster?nights=`): the latest `unit_correctness` row per (target, desk) inside a trailing window, each desk's window means, and the roster totals — BOTH the mean of desks and the claim-pooled figure, each labelled, because neither may be served as "the" correctness. Its own leaf rather than a section of `v3_api.py` (one line under its size ceiling), and never pooled with the operator gold-set axis `v3_api.py` serves at `/v3/eval/correctness`. Reuses `unit_correctness_api`'s `correctness_badge` / `_reference_state` rather than re-deriving either, so the roster and the Inspector badge cannot disagree about a null share or about the word "stale"; same slim-image rule as its sibling |
| `judge_stats_api.py`, `external_audit_api.py` | `/api/v1/v3` | Judge population statistics; the standing auditor's board, whose contradiction rate is absent rather than `0.0` when nothing was checked |
| `layers_api.py` | `/api/v1/v3` | The source-layer divergence map (`GET /v3/layers/divergence`): the newest `layer_divergence` receipt desk by desk, plus each desk's newest fired divergence. Reads `analyst_traces.output_payload` for the receipt — a quiet run is suppressed to trace-only, so `analyst_outputs` alone is blank on exactly the days the instrument worked. Passes every pair row through verbatim and computes no statistic of its own |
| `graph_structure_api.py`, `graph_walk_api.py`, `graph_triggers_api.py`, `graph_arcs_api.py`, `belief_api.py` | `/api/v1`, `/api/v1/v3` | Graph structure reads; the anchored ego walk over `entity_edges` (`GET /graph/ego`, `GET /graph/edge/{id}` — no depth parameter by design, every hop is a fresh anchored ego); the pre-registered engine gauges, where an unreadable gauge says so rather than returning 0; the P4b cross-layer projection read (`GET /v3/graph/arcs` — plane-filtered bounded ego/walk with `as_of`, refusing `projection_disabled` / `projection_empty` / `projection_stale` as distinct states); the v3 decision-time belief register (`GET /v3/belief?as_of=`) with the verdict fold stamped and `verdict_pending_at_as_of` counted |
| `substrate_reads_api.py` | `/api/v1` | Read-through substrate queries for the panels. It includes `unit_correctness_api.py`'s router rather than mounting it separately, so route and auth gate belong to this surface; that module is a sibling file only because inlining it would cross the module-size gate. `substrate_reads_folds.py` is a second such sibling: the four set-based critique folds `/findings` runs, declared once. Neither may import `analysts.deterministic_handlers`: the registry image is slim and that import graph reaches `feedparser` |
| `lineage_api.py` | `/api/v1` | The provenance walk. `_SUBSTRATE_TABLES` is the lineage catalog, and `journal_entries` is deliberately absent from it |
| `entities_api.py` | `/api/v1` | Entity knowledge-graph reads |
| `journal_api.py`, `journal_proposals_api.py` | `/api/v1` | The reflective-voice feed; the human-gated proposal queue, whose accept path runs the idempotent per-kind worker in `journal_proposals_apply.py` |
| `consult_api.py`, `consult_stream_api.py`, `consult_sessions_api.py`, `deep_consult_api.py` | `/api/v1` | On-demand consult, its token stream, session persistence, and the deep variant. Synthesis models and the recovery projection are in `consult_synthesis_api.py`, imported by `consult_api.py` |
| `labels_api.py`, `goldset_api.py` | `/api/v1`, `/api/v1/v3` | Operator gold labels; the gold-set cohort surface (sampling in `goldset_sampling.py`) |
| `watchlist_api.py`, `collection_requirements_api.py`, `retention_policies_api.py` | `/api/v1/v3` | Standing watches, durable collection requirements, retention policy rows |
| `read_events_api.py` | `/api/v1` | The append-only read-telemetry ledger: a batch append that drops and counts a bad event rather than failing the batch, and a rollup grouped in Postgres so a polling tile cannot become a table scan |
| `runtime_telemetry_api.py`, `budget_api.py`, `export_api.py` | `/api/v1`, `/api/v1/budget`, `/api/v1/v3` | Actor health, budget envelopes, export. The `### Citations` bullets are the leaf `export_citation_lines.py` (extracted verbatim when the HISTORICAL OBSERVATION bullet crossed the module-size threshold): an observation endnote prints the ROW — provider, series, subject, value with its unit, the valid period, the file `sha256` — not a label for it, so a reader of the printed document can check the number against the provider without joining to anything |
| `metrics_api.py` | app root | Prometheus text exposition, mounted with no prefix and deliberately not bearer-gated so a scraper needs no token |
| `health.py` | — | The liveness/readiness surface |

### 2.3 `analysts/` — the kind modules

Seventeen modules declare a `KIND_NAME` and are what `discover_analyst_kinds()`
registers. Twelve are `AnalystKind` enum members; five
(`journal_assessor`, `entity_researcher`, `signal_salience`,
`situation_tracker`, `inquiry`) are extension kinds registered through the
vocabulary tables, which is how a kind is added without touching the enum.

| Module | Kind | What it does |
|---|---|---|
| `inline_target.py` | `inline_target` | The bounded reasoning unit: one narrow question over one desk's slice. A run is ASSEMBLE a cited slice + a grounding preamble → cited SYNTHESIZE (strict JSON whose prose carries `[N]` markers resolving to signal ids) → a mandatory faithfulness VERIFY → an `effective_confidence` fold |
| `meta_findings_synthesizer.py` | `meta_findings_synthesizer` | The composition tower: the per-country, per-region, thematic and world compositions all run this one module under different descriptors. Its slice inner-joins the faithfulness critique, so an unverified sub-claim cannot enter. The entry-point surface — the deps Protocol and the Runner — lives in `meta_findings_runner.py` (P2 size-gate split; the runner reaches `_run` by deferred import) |
| `deterministic.py` | `deterministic` | The LLM-free dispatcher. `SUB_HANDLERS` maps 51 names onto modules in `deterministic_handlers/` (§2.5) |
| `journal_assessor.py` | `journal_assessor` | The first-person reflective voice, and the one analyst pointed at the whole organism rather than one slice. A global run per tick, `method.kind: llm_planner` (an in-actor staged PLAN → GATHER → NARRATE arc, not the workflow path). It is off the fact/finding/nexus chain: it writes only `journal_entries`, carries an always-empty `derived_from`, and is absent from the lineage catalog, so no downstream walk can surface a journal node. The same kind backs the consolidation, chronicle and faculty-lens descriptors |
| `inquiry.py` | `inquiry` | The STATEFUL journal voice. Same staged arc, same `journal_entries` row (`entry_kind='inquiry'`, or `'crossroads'` for that one descriptor id), same empty `derived_from` — plus a PUT-able `method.options.brief` and an `inquiry_ledger` read at PLAN and written at REFLECT. A hypothesis without a frozen resolution test and a question that does not say what it is about are both refused in code. Its default GATHER binding is `substrate_read`; `journal_read` and the `inquiry_state` ledger ride the per-tool binding channel. No web pack, ever |
| `competing_hypotheses.py` | `competing_hypotheses` | ACH: competing hypotheses scored on an evidence × diagnosticity matrix, resolved against the exogenous `resolved_outcome` column so calibration grades against later evidence rather than against itself |
| `relationship_reifier.py` | `relationship_reifier` | Types co-mentioned entity pairs into signed typed nexuses. Its receipt builder is the sibling `_reifier_receipt.py`; selection and alias pairing are `reifier_selection.py` / `reifier_alias_pairs.py` / `relationship_typing_batch.py` / `edge_qualification.py` |
| `critic.py` | `critic` | Scores another analyst's output with an LLM judge |
| `optimizer.py` | `optimizer` | Dispatches the GEPA loop (§3.5). It refuses at its top for the mothballed optimizer ids (SEAMS #53) |
| `predictor.py` | `predictor` | The forecast-as-claim kind. Its descriptors are retired |
| `cross_target_raw.py` | `cross_target_raw` | Cross-target raw reads |
| `cross_analyst_correlator.py` | `cross_analyst_correlator` | Cross-analyst correlation |
| `consult_on_demand.py`, `deep_consult.py` | `consult_on_demand`, `deep_consult` | The operator-question analysts. Round protocol, tool rendering, reply parsing, transcript, resynthesis, the spend guard and the PROVENANCE CENSUS (`consult_provenance_census.py` — cited refs counted by their OWN `origin_class` column, never by which tool returned them, plus the sentences carrying no citation; an unmeasurable class is `None`, never 0) are the `consult_*.py` siblings |
| `entity_researcher.py` | `entity_researcher` | Researches an unresolved entity through the governed packs |
| `signal_salience.py` | `signal_salience` | Per-signal consequence scoring; `MASS_FLOOR` is the shared magnitude ceiling the research write path also reads |
| `situation_tracker.py` | `situation_tracker` | The one writer of the situation trajectory ledger: it diffs each open situation against its previous tick and lands typed transitions |

### 2.4 `analysts/` — the families the kind modules were split into

Each family came out under the module-size gate. The gate is honoured by
extracting a cohesive unit into a sibling, never by raising the ceiling
(`tests/test_module_size_gate.py`).

| Family | Modules | What the family owns |
|---|---|---|
| Assembly (construction) | `assembly_spans.py`, `assembly_payload.py`, `assembly_render.py`, `assembly_salience.py`, `assembly_carry.py` | The quotation-assembly regime a composition writes instead of free-text synthesis. Spans are built byte-identically against the full untruncated source body fetched at synthesis time (sha256 + UTF-8 byte offsets); the payload carries the coverage and drop ledgers; the render resolves `[[ref:N]]` markers into desk citations; salience orders and caps the blocks. The audit side is `provenance/assembly_arms.py` (§2.6) — the arms read the payload, never the render |
| Assessment | `assessment_channel.py`, `assessment_prompts.py`, `assessment_unsupported.py`, `assessment_prompt_world_v4.py` | The labeled, badged read generated from an assembly's own spine. The prompt is built from the spine alone; `derived_from == [spine_id]` is AST-guarded so the voice cannot read outside its own record; any sentence the spine does not support is marked with an exact offset rather than dropped or laundered; the assessment is graded as its own population |
| Composition | `composition_slice.py`, `composition_window.py`, `composition_prompts.py`, `composition_prompt_assembly.py`, `composition_citations.py`, `composition_event_citations.py`, `composition_coercion.py`, `composition_correctness_gate.py`, `window_ledger.py`, `region_rollup.py` | The slice narrows the verified sub-claim pool a composition may draw from, floored like every other input. `composition_window.py` computes the real evidence window from the heads actually consumed, so the as-of instant cannot be model-derived. `composition_prompts.py` holds the prompt bodies, so a doctrine revision is a diff on prose. `composition_coercion.py` salvages a JSON-wrapped body when the intent is unambiguous and raises to the dead-letter when it is not, rather than publishing the envelope as the body. The correctness gate quotes a unit into the periphery tier when its correctness is missing or below bar, never drops it. `region_rollup.py` carries country assemblies up byte-identically, so the world read composes over country assemblies rather than over a re-narration of them |
| Slice and render | `slice_render.py`, `wire_pair_collapse.py`, `spread_block.py`, `source_independence.py`, `unit_grounding.py`, `unit_grounding_clause.py`, `history_grounding.py`, `unit_names.py`, `open_question_conversion.py`, `question_text.py` | Render-side slice statistics; same-wire-story collapse, where two mastheads carrying one agency dispatch reach a desk as one numbered signal (both ids stay in `derived_from`, and the guard is two distinct mastheads); the independence count one tier up — how many sources stand behind a claim the record quotes, after folding syndicated copies, publishing `[single-source]` beside the ordinal and `single_source` / `wire_folded` on the citation (the declared outlet→wire relationships are `descriptors/source_wire_map.yaml`, pinned to the map that ships in the image; nothing here gates or re-weights); grounding assembly for a unit — the shared prompt contract is its own sibling (`unit_grounding_clause.py`, extracted verbatim so the gate is honoured by splitting), and `history_grounding.py` holds the HISTORICAL SERIES block: the seventh grounding block, the only one that is not this platform's own memory, the only one that takes MORE THAN ONE ordinal (one per series line, because a single ordinal over eleven independent numbers is how a 2016 figure gets graded as a current claim), and the home of the `stale_tense` marker — produced ONCE and rendered unchanged into the prompt line, the judge's evidence and the exported endnote; the post-persist open-question conversion |
| Journal | `journal_slice.py`, `journal_clusters.py`, `journal_reflect.py`, `journal_leak_guards.py` | The journal's slice, its clustering, the reflection arc, and the guards that keep product content out of the voice |
| Gather and planning | `gather_native.py`, `gather_surface.py`, `planner_action.py`, `output_contract.py`, `handler_options.py`, `handler_options_base.py`, `handler_options_catalog.py`, `handler_options_programs.py` | The native tool-round GATHER path and its surface, the planner action shape, the output contract, and descriptor-driven handler options. `handler_options.py` is the `OptionSpec` machinery (reserved keys, resolve/degrade); `handler_options_catalog.py` assembles `HANDLER_OPTIONS`/`ANALYST_KIND_OPTIONS` from the events/inquiry/programs sibling catalogs, and `handler_options.py` re-exports both dicts under the same names |
| Misc leaves | `claim_contradiction.py`, `research_regime.py`, `_llm_budget.py`, `_tradecraft.py` | Claim-level contradiction detection (what the judge's quote rules act on), the research regime, the per-run LLM budget, tradecraft references |

`agency/` is the action-pack agency plane: `agency.py` (the `run_pack_tool` hard
gate), `governor.py` (per-pack caps and budget), `resolution.py` (capability
resolution — `analyst.action_packs ∩ target.allowed_action_packs ∩
pack.applicability`), `binding.py` (`AgencyToolBinding` + `EscalationBinding`,
the production on-ramp), `tools.py`, `web_tools.py`, `write_tools.py`,
`substrate_read.py`, `journal_read.py`, `journal_propose.py`,
`research_tools.py` (the `web_evidence` tool), `robots.py` (the fetch-path
robots gate, fail-closed on an unreachable robots file), `search_cost.py` (the
pre-dispatch cost brake, reading the same invocation ledger the governor caps
on), `events.py`.

### 2.5 `analysts/deterministic_handlers/` — the LLM-free analysts

103 modules. `deterministic.SUB_HANDLERS` is the dispatch table with 51 entries;
every other module in the directory is an imported helper of one of those, not
an orphan. Grouped by what they do:

| Group | Modules |
|---|---|
| The analysis spine | `scorecard_producer.py` + `scorecard_banding.py` (one banded row per active desk, from high-precision rules over already-verified claims; a dimension with no qualifying claim reads `insufficient-evidence` with a machine reason, never a fabricated band), `unit_correctness_scorer.py`, `composition_lineage_sweep.py`, `finding_supersession.py` |
| Indicators and warning | `indicator_tracker.py` (run-over-run diffs on the `data.indicators[]` block the units emit; generic, with no unit-specific code), `collection_gap.py` (starved desk × dimension cells) |
| Source layers | `layer_divergence.py` with `_layer_fold.py` (the wire fold, per (layer, day) and never across layers — the same dispatch in the domestic and the foreign press is the narrative-control pair's subject, not a duplicate) and `_layer_divergence_reads.py` (the four queries over `source_layers` / `desk_apertures` / `signals`). It makes the CHANGE in a country's own layer-to-layer gap the finding, against that country's own rolling baseline, and fires only on a two-day move; a layer the desk's aperture declares `absent` contributes no count and the finding says so. The map's classification error is not yet bounded — SEAMS #59 |
| Correctness grading | `correctness_grader.py` with `_correctness_grade.py`, `_correctness_segment.py`, `_correctness_rubric.py`, `_correctness_adjudicate.py`, `_correctness_calibration.py`, `_correctness_packet.py`, `_challenge_detect.py` |
| Reference building | `reference_builder.py` with `_reference_loop.py` (the bounded tool loop through the `web_access` pack), `_reference_page.py` (page → text + publish date + content-addressed archive entry), `_reference_dates.py` (the publish-date LADDER the gate reads — six ranked rungs from JSON-LD down to an official host's `Last-Modified`, each naming itself in `date_source`, with the dissenting rungs recorded rather than dropped; re-exported through `_reference_page` so no caller moved), `_reference_fences.py` (the acceptance fences, each run after the model commits, over text it cannot reach), `_reference_instruction.py` (the stage-1 instruction shared with the operator top-up packet, so the scheduled build and a manual one cannot ask for different shapes), `_reference_store.py` (the one writer, shared with `scripts/load_unit_reference.py`), `_reference_roster.py` (the scheduler; it orders by last *attempt*, not last success, because a failed build writes no row and ordering by success pins the lane on a target it cannot build), `_reference_manifest.py`, `_reference_notes.py`, `_reference_validity.py`. The grader reads `unit_references` and can never write one — a grader that could build its own reference could close the loop on itself |
| External audit | `standing_auditor.py` with `_external_audit_sampling.py`, `_external_audit_claims.py`, `_external_audit_search.py`, `_external_audit_fetch.py`, `_external_audit_grader.py`, `_external_audit_queue.py`, `_external_audit_paging.py`, `_external_audit_width.py`, `_external_audit_width_writes.py`. The auditor checks a claim against the world rather than against internal consistency, always through the `web_access` pack and never through ad-hoc HTTP, and writes a heartbeat on every run including one that audits nothing, so "nothing to contradict" and "the auditor is dead" cannot look alike |
| Attention measurement | `desk_reference.py`, `_reference_diff.py` (two deliberately separate metrics — did the slice carry the story at all, and among stories it carried did the desk engage it) |
| Alerting and watch | `alert_trigger_scan.py` with the per-class siblings `_band_crossing_scan.py`, `_steady_state_guard.py`, `_daily_page_budget.py`, `_watchlist_scan.py`, `_production_deficit_scan.py`, `_situation_escalation_scan.py`, `_contention_flip_scan.py`, `_coverage_floor_scan.py`, `geo_convergence_scan.py`; `claim_watch.py` with `claim_watch_guards.py` and `claim_watch_sql.py`; `bearing_gate.py` |
| Entity and graph | `entity_resolution.py` (its canon is the shared `data/_entity_canon.py`), `entity_gc.py`, `proposed_edge_governance.py`, `_graph_metrics_sink.py`, `graph_mining.py`, `structural_balance.py`, `graph_projector.py` (P4b — whole-rebuild of the `graph_arcs` projection; `graph_mining`/`structural_balance` read `graph_arcs WHERE plane='world'` while `LEGBA_GRAPH_PROJECTION` is on), `nexus_decay.py`, `_entity_geo.py` |
| Facts, claims, situations | `fact_decay.py`, `fact_decay_scan.py`, `fact_contention_arbiter.py`, `fact_contention_pass.py`, `situation_clustering.py`, `thematic_proposal.py`, `hypothesis_lifecycle.py`, `integrity_sweep.py` |
| Dedup and coalescing | `cross_source_dedup.py` (a bounded per-run scan: it skips already-canonicalised groups and caps the scan, so a backlog drains across cadences inside the actor-invoke budget), `cross_source_coalesce.py` (substrate-wide, off by default — SEAMS #19) |
| Corpus and enrichment sweeps | `corpus_indexer.py`, `corpus_retention.py` (the delete mirror: it drains the tombstone queue against OpenSearch, re-verifying each row is really gone first), `signal_summarizer.py`, `signal_embedder.py`, `reenrich_ner.py`, `reenrich_translation.py` |
| Calibration and baselines | `calibration_tracking.py`, `band_calibration_tracker.py`, `desk_baseline.py`, `source_track_record.py`, `forecast_acute.py`, `forecast_scoreboard.py` |
| Retention and archive | `_retention_sweep.py` (the one engine both retention handlers drive, configured by the `retention_policies` table), `signals_retention.py`, `analyst_traces_retention.py`, `evidence_archiver.py` |
| Research | `_research_dispatch.py` (turns a coverage-floor breach into a standing open question the existing backlog already drains — no new queue, no new table), `research_measurement.py` (publishes the honest-null research counters as rows stamped `meta=true`, so they can never be composed into what they measure), `_reference_gap_dispatch.py` |
| Other | `narrative_mapper.py`, `adversarial_signals.py`, `anomaly_detection.py`, `_title_frame_gauge.py` |

### 2.6 `provenance/` — the verify pass and the judge bricks

`verify.py` holds both the provenance/lineage sanity checks
(`verify_provenance_complete`, `validate_lineage`) and the faithfulness verify
(`verify_finding_faithfulness`). Its module-size ceiling is not raised; the way
under it is a new brick.

| Brick | What it owns |
|---|---|
| `judge_evidence.py` | The evidence view the judge grades against |
| `judge_assessability.py` | Claim shape and score state: `unassessable` versus `scored`, the provisional ceiling on a non-LLM verdict, and the critique-payload contract that publishes them |
| `judge_input_checks.py` | Grading a composition against what it was *shown* rather than what it cited — a buried lead and a detected input contradiction the body never surfaced, both soft failures |
| `judge_quote_rules.py` | The hard-fail quote contract and the withdraw-only guard family that retracts a contradiction whose quote actually confirms the claim |
| `judge_verdict_parsing.py` | Verdict extraction and reasons. The AST-level fail-class drift guard parses this module too, so a reason code does not read as dead the moment its function moves house |
| `judge_floor_escalation.py` | Floor-triggered judging: an unjudged finding about to be excluded by the composition floor is re-entered through the judge first, so the floor may only exclude on judged evidence |
| `judge_absence_rubric.py` | The absence rubric — an absence stated as a world fact versus one scoped to what was collected |
| `judge_transport.py`, `judge_pipeline_version.py` | The judge call transport; the pipeline-version stamp every critique carries, so pre- and post-revision populations never pool |
| `composition_integrity.py` | Grades a composition against the desk reads it cites, using evidence the pass already held. Carries its own collection-scope predicate, because a time bound answers *when* and only a collection bound answers *what was searched* |
| `world_knowledge_guards.py` | The world-knowledge guard family |
| `assembly_arms.py` | Four deterministic auditors over the assembly payload: quote fidelity, scope preservation, attribution equality, selection honesty. Every reason code is hard, because the arms grade a generated document — each says "the record does not say what it says it says". They are an audit, not a gate: a fire names the constructor as the defect |
| `text_fold.py` | The one normalisation site — NFKC plus a punctuation table plus casefold — called by every comparator that compares model prose to producer prose. It imports nothing from the package, so anything may import it |
| `citation_markers.py` | Citation-marker parsing, including folding full-width bracket variants back to ASCII before `[N]` parsing |
| `event_citations.py` | The v3 `event:<uuid>` ref kind's shared leaf: the flag + cap env reads and `expand_event_citation`, the ranked `signal_event_links` expansion that emits per-signal entries with real `source_text` — the event's own summary is never emitted. stdlib + asyncpg only, so either citation builder can call it |
| `absence_slice.py`, `assessment_aperture.py`, `bearing.py`, `structural_claims.py`, `value_clustering.py`, `assessment_weighting.py` | The absence slice, the assessment's aperture section (off the slice route — the input slice is what the surface was shown, so it cannot refute a claim about what went uncarried), bearing computation, structural re-derivation of asserted quantities, contested-value clustering, assessment weighting |
| `external_grades.py`, `external_span_check.py`, `external_truth.py`, `round_lineage.py` | The append-only external-grading ledger and its non-pooling aggregation; the evidentiary contract written as code (source-tier lookup, a verbatim decisive-span match through the shared fold, time anchoring against the read's own evidence window); the live publication accessor; and the frozen human-graded proof-round numbers, DB-less by design |
| `consumption.py`, `entity_edge_writes.py`, `output_graph.py` | Forward lineage, entity-edge writes, the output graph |

---

## 3. `src/legba/runtime/` — execution

78 files. Entry point `dapr_host.py`.

| Family | Its job | Start from | What lives there |
|---|---|---|---|
| Actor host and actors | Turn descriptors into running Dapr actors | `dapr_host.py` | The `legba-runtime-dapr` FastAPI host, plane bring-up and deps resolution (`dapr_host.py`); the production actor classes `TargetActor` and `AnalystActor` (`dapr_actors.py`); `SourceActor` + the directly-testable `SourceCore` (`source_actor.py`); cadence and cron helpers (`dapr_cron.py`); the four-plane assembler (`source_first_runtime.py`). The run path proper lives in the modules `dapr_actors.py` was decomposed into: `actor_turn.py`, `actor_substrate_slice.py`, `actor_critic.py`, `actor_output_emit.py`, `actor_payload.py`, `actor_ids.py`, `actor_retry.py`. `actor_turn_witness.py` records which actor ids are inside a turn in this process — an ASGI middleware on the host's own callback path, read by the trigger plane so a fire absorbed by a busy actor is not logged as a failed run. The actor id scheme is `kind::descriptor_id::content_hash[:16]` |
| Reconcile | Converge running actors onto the registry | `reconcile.py` | Informer → work queue → pure per-kind reconcilers `(observed, desired) → ReconcileAction` → an executor that is the only mutator (`reconcile.py`); the NATS event informer (`nats_informer.py`); the lifecycle FSM (`lifecycle.py`); `ActorStateStore`/`ActorStateRecord` (`state.py`); desired-state reads (`registry_client.py`); reminder garbage collection (`reminder_gc.py`); the inbound-webhook drain (`inbound_drain.py`) — the durable pull consumer that takes the accepted webhook envelope off the request path and runs it through the normal ingest pipeline, replaying every un-acked envelope after a restart because it has no other catch-up path |
| `subscription/` | Source → target fan-out, and the subscription seam | `engine.py` | `SubscriptionEngine` resolve/enforce/plan/bind (`engine.py`), `SourceRef` resolution (`sourceref.py`), open/allowlist/grant policy (`policy.py`), coarse-subject planning (`subjects.py`), two-stage matching — an exact SQL `WHERE` plus a Starlark residual (`filter.py`), replay (`backfill.py`) |
| `triggers/` | The coalescing trigger plane | `engine.py` | `TriggerEngine` over `Coalescer`: it dirty-marks an (analyst, target) pair and fires on cadence, accumulation or a severity gate, clamped by cooldown (`engine.py`, `coalescer.py`); run dispatch (`dispatch.py`), whose fourth outcome `coalesced` is a fire the target actor was already inside a turn for — `trigger.coalesced_into_turn`, not `trigger.run.failed`; policy and durable trigger state (`policy.py`, `state.py`) |
| `jobs/` | The NATS work queue and its competing-consumer workers | `queue.py` | `JobQueue` (`queue.py`), `JobWorkerPool` (`worker.py`), `dispatch.py`, the media handler (`process_media.py`), the model-service media client (`media_client.py`). A derived signal from a job re-enters the fan-out path |
| `dapr_workflow/` | Durable multi-hour workflows | `worker.py` | The GEPA algorithm, workflow-I/O dataclasses and the in-process fallback client (`gepa.py`); the deterministic orchestrator (`workflow.py`); the `WorkflowRuntime` registrar and the `legba-dapr-workflow-worker` entry (`worker.py`); the dispatch client (`client.py`); the deep-consult workflow and its client (`deep_consult.py`, `deep_consult_workflow.py`, `deep_consult_client.py`); the `LegbaProviderLM` dspy adapter that never uses litellm (`dspy_lm.py`). dspy lives only in this opt-in worker image |
| Wiring and factories | Build per-actor deps and construct ports | `analyst_deps_builder.py` | Per-kind analyst deps and run-method dispatch (`analyst_deps_builder.py`, `analyst_deps_kinds.py`, `analyst_method.py`, `deps.py`); port factories (`source_factory.py`, `embedding_factory.py`, `qdrant_factory.py`, `nlp_client_factory.py`, `receipt_chain_factory.py`, `search_handler_factory.py`, `substrate_singleton_factory.py`, `llm_handler_cache.py`); the enrichment pipeline (`pipeline.py`); budget enforcement (`budget.py`); the substrate read port (`substrate_query_port.py`, with the shared temporal helpers in `substrate_temporal.py`, the graph-walk machinery in `substrate_graph_walks.py`, the situation/timeline/event frame reads in `substrate_frame_reads.py` — extracted as a sibling before P6's event tools so the port stays under the module-size gate — and the COLLECTION series reads in `_observations_read.py`: the only reader of `observations`, bitemporal (`as_of` picks the latest revision RECORDED by that instant, which is a different question from the valid window `from`/`to` bound), set-based `DISTINCT ON` rather than a per-series probe, gated to holdings whose descriptor head is `loaded`, and carrying the redundant-looking `valid_from <= to` predicate that is what actually prunes the partitions) and corpus readers (`substrate_corpus_readers.py`, `corpus_read_projection.py`); audit and checkpoint wiring (`audit_checkpointer_wiring.py`); the `web_access` binding the standing auditor reaches search through (`external_audit_binding.py`) and the grader route (`external_grader_route.py`) |
| Grounding | Inject current world state before the model call | `grounding.py` | `SubstrateGroundingResolver` reads the current authoritative facts and nexuses under a temporal-honesty gate; `collect_grounding_candidates` pulls candidate names from the in-memory slice and the run's target with no DB call; `build_grounding_preamble` renders the dated authoritative-context block. `analyst_deps_builder._build_grounding_hook` builds the per-run hook only when the descriptor opts in and a pool is available, and yields `None` on any read failure rather than a stray header |
| Liveness and leadership | Keep one writer, and notice a stall | `liveness_watchdog.py` | The pipeline liveness watchdog (`liveness_watchdog.py`), advisory-lock leader election and the single-replica safety assertion (`leader.py`), LLM route liveness (`llm_route_liveness.py`), the RAG rollback guard (`rag_rollback.py`), logging setup (`logging_setup.py`), target resolution (`target_resolution.py`), dispatched questions (`dispatched_question.py`), the journal slice reader (`journal_slice_v2.py`) |

### 3.1 The four planes

`source_first_runtime.py` assembles the planes the host boots on top of the
substrate and the reconcile loop:

1. **Acquisition** — `source_actor.py` + `data/sources/baseline.py` + `subscription/`.
2. **Analysis** — `TargetActor`/`AnalystActor` + `triggers/` + `data/analysts/agency/` + `budget.py`.
3. **Async jobs** — `jobs/`.
4. **Substrate** — the `data/` ports.

`SourceActor` owns acquisition: poll on a Dapr reminder or push through the
webhook router, run the per-source baseline, `write_canonical_signal`, publish
on the signal subject. One ingest per source regardless of how many consumers
subscribe. It also backfills `signals.source_credibility` at write time through
a host lookup, because the credibility pipeline filter only runs for descriptors
that bind it.

---

## 4. The UI — `legba-ui-v3/`

A Vite + React + TypeScript SPA (Tailwind, Dockview panels). `legba-ui-build`
builds it in compose and `legba-caddy` serves the artifacts and reverse-proxies
the registry API. Co-located `*.test.ts(x)` files are the Vitest suite.

| Area | Its job | Start from | What lives there |
|---|---|---|---|
| App shell | Bootstrap, routing, auth | `App.tsx` | `App.tsx` / `main.tsx`, the JWT chain (`auth/jwt.ts`), client state (`state/` — `selection.ts` is the unified record-selection store, alongside `scope.ts`, `feedView.ts`, `consultSession.ts`, `exportBasket.ts`) |
| `lib/` | API client, live tail, per-view models | `api.ts` | The registry client (`api.ts`), the WebSocket live tail (`ws.ts`, `useLiveTail.ts`), starter descriptors, `workspaces.ts` (the stances as data: id, label, the question each answers, its keyboard index, an ordered seed and pinned proportions — the landing is a stance's seed, not a hardcoded grid), `readTelemetry.ts` (the read-events emitter: batched and debounced, a bounded queue that drops oldest, beacon on page hide, a dedupe window, fail-silent in every path — a failed batch is dropped rather than retried, because a retry would backdate a week of reading into one minute), `readScoreboard.ts`, `citationsModel.ts` (the shared citation vocabulary every cited surface reads, so a grounding citation resolves the same way everywhere), and the per-view models (`graphModel`, `findingsViews`, `alertModel`, `geoPoints`, `timelinePoints`, …) |
| `panel-registry/` | The dynamic panel registry | `registry.ts` | `registry.ts` (`def()` returns `tier: 'live'` by default; `PREVIEW_KINDS` promotes a guarded-preview surface and `HIDDEN_KINDS` drops a panel from navigation while keeping it resolvable by id), `loader.ts`, `useRegistry.ts`, `navGroups.ts`, `synthesize.ts`, and `aliases.ts` — the retirement mechanism: a table of retired kind → survivor, consumed by `resolvePanel`, both panel openers and a pre-pass on both layout loaders, so a saved layout naming a retired kind resolves to its survivor rather than restoring a tombstone. The pre-pass dedupes globally rather than per dock group, which is what stops two retired tiles resolving onto the same survivor three times |
| `components/` | Shared chrome | `PanelChrome.tsx` | The record-jump `CommandPalette.tsx`, the workspace switcher `Sidebar.tsx` and `WorkspaceBar.tsx`, the unified `inspector/`, the reading kit (`CitedProse.tsx`, `VerdictBadge.tsx`, `ProvenanceBadge.tsx`, `ProvenanceCard.tsx`, `SeverityBadge.tsx`), `DescriptorBuilder`/`DescriptorEditor`/`DescriptorView`, `ScopePicker`, `StarterPicker`, `StatusBar`, `PanelErrorBoundary`, `density/` |
| `panels/` | The workbench panel set | (per area) | `source/`, `target/`, `analyst/`, `registry/`, `system/`, `merged/` (the consolidation targets: alerts and watches, consult, provenance, timeline), `v4/`, and `_DeferredStub.tsx` for a not-yet-built panel. Note `panels/v4/` and the top-level `v4/` are different directories that share a name |
| `v4/` | The rooms shell — the current front door | `world/` | `world/` (the default banded-verdict maplibre-gl choropleth, with a Leaflet fallback when WebGL is unavailable, plus KPIs, a time scrubber and the live feed), `flow/` (the canvas-as-view-over-registry with live telemetry), `why/` (the provenance trail, lineage and entity graphs, the world assessment), `read/`, `case/`, and shared `components/` |
| `mobile/` | The mobile surface | `MobileApp.tsx` | `MobileApp.tsx` plus `main.tsx` and `mobile.css`, the views in `mobile/components/` (`JournalView.tsx`, `ReportView.tsx`, `ReportsList.tsx`, `ConsultSheet.tsx`), and the model and cache leaves (`mobileModel.ts`, `mobileConsult.ts`, `mobileCache.ts`, `useUrlState.ts`) |

Panel tier and the current live/preview/hidden classification are generated from
`registry.ts` into [RELEASE_STATE_MATRIX.md](RELEASE_STATE_MATRIX.md) §2 — read
that table rather than counting panels here. UI behaviour is
[UI.md](UI.md).

---

## 5. Entry points, infra, descriptors, scripts

### Console scripts (`pyproject.toml [project.scripts]`)

| Script | Target | Role |
|---|---|---|
| `legba-registry` | `legba.data.registry.server:main` | The registry HTTP/WS API |
| `legba-runtime-dapr` | `legba.runtime.dapr_host:main` | The Dapr actor host |
| `legba-dapr-workflow-worker` | `legba.runtime.dapr_workflow.worker:main` | The durable-workflow worker |
| `legba-mcp` | `legba.ui.mcp_server:main` | The MCP server surface |

Migrations apply through `python -m legba.data.migrate`.

### Docker, Dapr, deploy

- `docker-compose.yml` — the substrate (`redis`, `postgres`, `qdrant`, `opensearch`, `nats`), the Dapr control plane (`dapr-placement`, `dapr-scheduler` and its init pair, `dapr-sidecar`), the Legba services (`legba-registry`, `legba-runtime-dapr`, `legba-dapr-workflow-worker`, `legba-mcp`), the optional services (`rsshub`, `searxng`, `ntfy`, `legba-media`), and the UI pair (`legba-ui-build`, `legba-caddy`). Service `profiles:` gate the optional halves.
- `docker/` — `Dockerfile.registry`, `Dockerfile.runtime`, `Dockerfile.worker` (built **from** the runtime image, so both profiles build together), `Dockerfile.mcp`, `Caddyfile`, `nats.conf`, `searxng/settings.yml`.
- `dapr/components/` — `statestore.yaml` (Postgres actor state), `pubsub.yaml`, `secretstore.yaml`, `configuration.yaml`.
- `deploy/` — `deploy.sh` (the bring-up), `baseline/` (the shipped baseline SQL), `systemd/` (registry, runtime and backup units + timer), `cron.d/` (the host watchdog, log collector and nightly-suite lines), `prometheus/legba_alerts.yml`, `swarm/` (multi-node stack files), `compose.isolation.yml`.
- `legba-models/`, `legba-media/` — the two out-of-process model services, each with its own Dockerfile, compose file and `app/main.py`.

### `descriptors/`

247 tracked YAML files: 103 `source_*`, 101 `analyst_*`, 17 `target_*`,
11 `action_pack_*`, 5 `layer_*`, 4 `stack_component_*`, 2 `discovery_*`,
2 `template_*`, 1 `wire_map`, 1 `collection_*`. The `collection_` prefix is
load-bearing rather than cosmetic: the source machinery globs
`source_*.yaml`, so a mis-prefixed holding would be activated as a live
feed.

The in-tree file is the pinned definition; the **live head** is whatever the
registry holds, and the two differ by design — activation is an operator act
through the audited lifecycle route, so a descriptor that ships `draft` and runs
`active` is the intended path, not drift. The live analyst and source sets are
registry rows, not a directory listing: `ls descriptors/` undercounts sources
(the catalog registrar writes directly into `source_descriptors`) and
undercounts country desks (the desk registrars write targets directly). What is
where, by state in the tree, is in [STATUS.md](STATUS.md).

The analysis-spine descriptors are the ones to read first: the bounded units
(`analyst_leadership_transition.yaml`, `analyst_energy_security.yaml`,
`analyst_escalation.yaml`, `analyst_narrative_coordination.yaml`,
`analyst_internal_stability.yaml`, `analyst_military_posture.yaml`,
`analyst_economic_coercion.yaml`, `analyst_proliferation_watch.yaml`,
`analyst_disruption_status.yaml`), the composition tower
(`analyst_country_composition.yaml`, `analyst_region_composition.yaml`,
`analyst_world_assessor.yaml`, `analyst_escalation_composition.yaml`,
`analyst_escalation_dyad.yaml`), the assessment channel
(`analyst_country_assessment.yaml`, `analyst_world_assessment.yaml`), the
deterministic indicator pair (`analyst_indicator_tracker.yaml`,
`analyst_collection_gap.yaml`), the measurement set
(`analyst_scorecard_producer.yaml`, `analyst_unit_correctness_scorer.yaml`,
`analyst_correctness_grader.yaml`, `analyst_reference_builder.yaml`,
`analyst_standing_auditor.yaml`, `analyst_desk_reference.yaml`,
`analyst_forecast_scoreboard.yaml`), and the voice roster
(`analyst_journal_assessor.yaml`, `analyst_journal_consolidator.yaml`,
`analyst_chronicle_assessor.yaml`, `analyst_lens_*.yaml`).

### `scripts/`

**Two transports, two shared helpers; a script uses one, never both.**
`_p17_registrar.py` is the direct-DB path — it wraps `DescriptorRegistry` so a
bring-up is deterministic about which database it populates, and it sets a
process-global database default at import time. `_bringup_http.py` is the REST
path against a running registry (`registry_base`, `registry_client`,
`load_yaml`, `exists_head`, `register_create_only`). Importing the DB helper
into a REST-only tool would hand it a database selection it never asked for.

| Group | Scripts | What they do |
|---|---|---|
| Registrars | `bringup_register_*.py` (~50) | Register descriptors into a running registry: stack, sources, action packs, analysts, the G20 and watch country targets, the region and situation targets, the supply-chain pack, discovery. `bringup_register_analysts.py` registers the live analyst set and fails loud at register time on a unit whose eval rubric and verify block have drifted apart. `bringup_register_source_catalog.py` is the main source path — a `CatalogEntry` table registered directly into `source_descriptors` |
| Bring-up | `bringup_source_first_host.py`, `bringup_vault_load.py`, `bringup_set_budget_envelope.py`, `bringup_tag_targets.py`, `quick_start.py` | Boot the host, load vault secrets, set the budget envelope, tag targets |
| Seeding | `seed.py` (`--list` / `--source` / `--dry-run`), `seed_source_ratings.py`, `seed_sources.py`, `seed_import.py`, `export_substrate.py`, `export_seed_v2.sh` | Drive `data/seed/`, load curated ratings, export and re-import a substrate |
| Manual ingest | `manual_ingest.py`, `manual_ingest_vectors.py`, `load_unit_reference.py` | The structured and vector manual lanes, and the operator reference load (sharing `_reference_store.py` with the scheduled build) |
| Collections | `verify_collection_manifest.py`, `load_collection.py` | The two halves of collection loading, deliberately separate programs with different blast radii. The verifier goes to the providers and prints the coverage it MEASURES beside what the manifest DECLARES; it imports no DB driver and writes nothing. The loader validates the manifest through `legba/collection/1.0.0`, reuses the verifier's provider parsers (one parser, two readers), and writes rows DIRECTLY into `observations` — no NATS, no ingest pipeline, no enrichment. Idempotent on the table's unique key, resumable through `collection_loads`. See [COLLECTIONS.md](COLLECTIONS.md) |
| Backfills | `backfill/`, `backfill_corpus.py`, `backfill_entity_graph.py`, `backfill_entity_canonicalization.py`, `backfill_gazetteer_geo.py` | One-shot repairs over existing rows |
| Deploy and release | `rolling_deploy.sh`, `release_gate.sh`, `release_smoke.sh`, `prepush_scan.sh`, `make_release_manifest.sh`, `generate_release_manifest.py`, `backup.sh`, `backup_scheduled.sh` | The deploy, the release gate, the secret and codename scan, the manifest, backups |
| Generators with drift tests | `gen_release_state_matrix.py`, `gen_descriptor_prompt_manifest.py`, `gen_entity_merge_migration.py` | Each owns a marker-delimited block or a committed artifact and has a test that regenerates and compares, so the artifact cannot go stale silently |
| Host cron | `host_search_canary.sh`, `host_stall_watchdog.sh`, `host_llm_heartbeat.sh`, `host_log_collector.sh`, `host_nightly_suite.sh`, `host_ai1_saturation_watch.sh`, `models_host_vllm_watchdog.sh` | The out-of-container watchdogs and probes installed from `deploy/cron.d/` |
| Measurement harnesses | `kg2_*.py`, `r5_ablation_*.py`, `age_probe/`, `measure_*.py`, `voice4_flip/`, `*_replay.py` | Bake-offs, ablations and replays that produced the reports in the history tier. They are not part of the run path |
| Ops | `sweep_orphan_actors.py`, `purge_proposed_situations.py`, `cleanup_archives.sh`, `diagnose_stale_leaders.py`, `ops_pause_*.py`, `rag_watch.py`, `correctness_grade_now.py`, `reference_build_now.py`, `reference_topup_packet.py`, `harvest_open_questions.py`, `trigger_multi_country_runs.py` | Routine operator tasks; the procedures are in [RUNBOOK.md](RUNBOOK.md) |
| Tests | `run_tests.sh`, `run_tests_in_container.sh`, `run_ui_tests_in_container.sh`, `spike_smoke.py`, `deploy_smoke_cold_activation.sh` | Suite entry points |

### `tests/`

669 tracked files: `data_pkg/` (521), `runtime/` (107), `descriptors/`,
`clients/`, the journal waves, and 16 at the top level. Three top-level tests
are gates rather than unit tests and are worth knowing by name:
`test_no_undeclared_stubs.py` (parses the allowlist block in
[SEAMS.md](SEAMS.md)), `test_module_size_gate.py` (pins a line ceiling on every
module that was already large when the gate was written), and
`test_runtime_no_dspy_litellm.py` (keeps dspy and litellm out of the runtime and
analyst inference paths).

---

## 6. Where to add a thing

| Goal | Add here | Then |
|---|---|---|
| A source kind | A handler module in `data/sources/` implementing the `_contract.py` Protocol | Register the kind; write a `SourceDescriptor` |
| An analyst kind | A module in `data/analysts/` declaring `KIND_NAME` (or a handler in `deterministic_handlers/` for a deterministic one) | Add the value to `AnalystKind`, or register it through the vocabulary; wire the run path in `runtime/analyst_method.py` and `runtime/analyst_deps_builder.py` |
| A deterministic sub-handler | A module in `data/analysts/deterministic_handlers/` | Add its entry to `deterministic.SUB_HANDLERS`; write the descriptor |
| A filter or enricher | A handler in `data/filters/` implementing the `StreamHandler` Protocol | Reference it from a source's `pipeline` |
| An output sink | A handler in `data/outputs/` implementing the `AlertEmitter` Protocol (plus a sub-sink in `alert_sinks/` for an alert surface) | Bind it through a target or analyst `OutputBinding` |
| A migration | The next-numbered `data/migrations/NNNN_*.sql` | Apply with `python -m legba.data.migrate` |
| A predicate helper | `data/predicates/helpers.py` | Usable in source-selector and subscription Starlark |
| A provider adapter | A module under `data/stack/<family>/` | Register a `StackComponentDescriptor` |
| A discovery kind | A module in `data/discovery/` plus its materializer entry | Write a `discovery_*.yaml` |
| A curated seed source | An adapter in `data/seed/adapters/` implementing `SeedSource`, plus a dataset under `seeds/` | Register it in `data/seed/__init__.py` `ADAPTERS`; run `scripts/seed.py --source <name>` |
| A UI panel | A `.tsx` under `legba-ui-v3/src/panels/<area>/` | Register it in `panel-registry/registry.ts`, then re-run `scripts/gen_release_state_matrix.py` |
| A deliberately-not-built thing | Nothing in `src/` | Declare it in [SEAMS.md](SEAMS.md) with a guard rail that fails loud, and add its `file:symbol` line to the allowlist block |

---

See also: [ARCHITECTURE.md](ARCHITECTURE.md), [DESIGN.md](DESIGN.md),
[ANALYSIS.md](ANALYSIS.md), [FLOWS.md](FLOWS.md), [DATA_MODEL.md](DATA_MODEL.md),
[ACQUISITION.md](ACQUISITION.md), [UI.md](UI.md), [RUNBOOK.md](RUNBOOK.md),
[SEAMS.md](SEAMS.md), [STATUS.md](STATUS.md).
