# CODE_MAP

A navigational map of the Legba codebase: where each concern lives, how to add
things, and the key entry points. This file is purely "where is the code" — for
the concepts and why the system is shaped this way read `ARCHITECTURE.md` (and
`ANALYSIS.md` for the analysis spine); for the implementation decisions read
`DESIGN.md`; for running it read `RUNBOOK.md`. New here? Start with the
[README](../README.md) and the [Tour](TOUR.md).

The declared-seam list is `docs/SEAMS.md`. Where a module is
**built-but-unwired** (real code, no live caller / no live descriptor) or has a
known **code↔schema drift**, this map says so inline — don't infer "done" from
"present".

**Contents:**
[0 Package responsibility index](#0-package-responsibility-index) ·
[0a Corrections / honesty notes](#0a-corrections--honesty-notes-read-before-trusting-older-docs) ·
[1 Top-level layout](#1-top-level-layout) ·
[2 `src/legba/data/`](#2-srclegbadata--declarative-model--substrate) ·
[3 `src/legba/runtime/`](#3-srclegbaruntime--execution) ·
[4 UI](#4-ui--legba-ui-v3) ·
[5 Entry points, infra, scripts](#5-entry-points-infra-scripts) ·
[6 Where to add a thing](#6-where-to-add-a-thing) ·
[7 Future seams](#7-future-seams-present-in-the-tree-not-yet-live)

---

## 0. Package responsibility index

The fastest way to find a thing. One row per major package: its single
responsibility, the file you start from, and what lives there. **Built-but-
unwired** and **absent** markers matter — they distinguish "the code
exists" from "the live system uses it".

### `src/legba/data/` — declarative model + substrate

| Package | Responsibility (one line) | Start file | What lives here |
|---|---|---|---|
| `data/schemas/` | The descriptor types (strict, content-hashed pydantic) | `source.py` | `SourceDescriptor`/`Subscription`/`SourceRef` (`source.py`), `TargetDescriptor`+polymorphic `TargetScope` (`target.py`), `AnalystDescriptor`+open `AnalystKind`+optional `GroundingBlock` (`analyst.py`), `ActionPack`/`PackGovernor` (`action_pack.py`), `StackComponentDescriptor` (`stack.py`), lifecycle FSM (`lifecycle.py`), shared property types (`properties.py`), vocabulary shapes (`vocabulary.py`), version/content-hash helpers (`versioning.py`) |
| `data/registry/` | Control plane: descriptor registry, vault, HTTP/WS API | `server.py` | content-hashed instance registry + Ed25519 audit + DLQ + NATS events (`descriptor.py`, `audit.py`, `signing.py`, `dlq.py`, `events.py`/`streams.py`/`emitter.py`), XSalsa20-Poly1305 credential vault (`credentials.py`), stack registry (`stack.py`), the `legba-registry` FastAPI app + routers (`server.py` + `api.py`/`v3_api.py`/`substrate_reads_api.py`/`lineage_api.py`/`entities_api.py`/`runtime_telemetry_api.py`/`budget_api.py`/`source_credibility_api.py`/`consult_api.py` — **that is 9 of 28 `*_api.py` modules in a 53-module package**; the rest, unnamed here until 2026-08-02, are `collection_requirements_api`, `consult_sessions_api`, `consult_stream_api`, `deep_consult_api`, `export_api`, `goldset_api`, `graph_structure_api`, `graph_walk_api` (K-G4, 2026-08-03 — `/graph/ego` + `/graph/edge/{id}` over `entity_edges`), `journal_api`, `journal_proposals_api`, `labels_api`, `metrics_api`, `narratives_api`, `production_gauge_api`, `retention_policies_api`, `since_api`, `source_assurance_api`, `source_quality_api`, `timeline_api`, `watchlist_api` — `server.py` is the authority on which are mounted), discovery/version conversion (`discovered_materializer.py`, `conversion.py`). The ~5-name kernel those siblings import — `RegistryAPIDeps`/`_get_deps`, `require_bearer`, `sunset_headers`, `_authorize_ws_token` — was extracted from `api.py` into the leaf `_deps.py` (K-2, 2026-08-03) precisely because 26 of the 50 modules were importing a 2.5k-line HTTP surface to get it. `api.py` imports `_deps` one way and re-exports it, so `api.<name>` still resolves everywhere and no importer was rewritten (that is K-5, operator-gated); `build_router` — the fifth name, and ~1.4k of `api.py`'s remaining 2.35k lines — deliberately stays. `tests/data_pkg/test_registry_api_kernel.py` pins both halves: the re-export must not narrow, and `_deps` must never import `api` |
| `data/sources/` | Source-kind acquisition handler library | `_contract.py` | the handler Protocol + `Signal` (`_contract.py`/`_protocols.py`), the **per-source baseline pipeline** (`baseline.py`), 16 kind-handler modules (`rss.py`, `gdelt.py`, `gdelt_files.py` (S1 — the 15-min file-dump replacement for the rate-limited `gdelt` BigQuery/DOC-API path), `acled.py`, `ucdp.py` (S1-T9 — the Uppsala Conflict Data Program GED feed), `mediacloud.py`, `opensanctions.py`, `common_crawl.py`, `intelmq.py`, `firecrawl.py`, `scraper.py`, `telegram.py`, `discord.py`, `geojson.py`, `json_api.py`, `generic_webhook.py`+`webhook_router.py`), outbound provisioning (`provision.py`), egress helper (`_egress.py`) |
| `data/filters/` | In-flight enrichment / transform handlers over a `Signal` | `_contract.py` | `StreamHandler` Protocol (`_contract.py`), baseline enrichers (`language_detect.py`, `geocode.py`, `ner.py`, `classify.py`, `source_credibility.py`), ingest dedup tiers 1–2 (`ingest_dedupe.py`, `dedupe.py`), SLM-backed refiners that call the model service (`slm_classification_refine.py`, `slm_entity_resolve.py`, `slm_relationship_validate.py`) |
| `data/analysts/` | Analyst-kind implementations (one module per kind) | `__init__.py` | kind modules discovered via `discover_analyst_kinds()` (`__init__.py`): `inline_target.py` (the **nine bounded reasoning UNITS** — leadership_transition / energy_security / escalation / narrative_coordination / internal_stability / military_posture / economic_coercion / proliferation_watch (narrow: tag-scoped to the ~8 nuclear-relevant desks, not the full 32) / disruption_status (tag-scoped off the country plane entirely, to the thematic `supply_chain` lane/flow desks) — the base of the spine; each cited-synthesizes ONE narrow question then runs a mandatory faithfulness verify), `cross_target_raw.py` (registered in code, ZERO descriptors — see §0a), `meta_findings_synthesizer.py` (the composition tower — the per-country `country_composition`, the per-region `region_composition` (5 region frames), the thematic cross-desk `escalation_composition` with a correlation guard, AND the repointed GLOBAL `world_assessor` composition), `cross_analyst_correlator.py`, `deep_consult.py`, `relationship_reifier.py` (META — co-mention pairs → signed typed nexuses, 8B LLM), `competing_hypotheses.py` (META ACH — evidence×diagnosticity matrix is LLM-scored + ±2 transitions; outcome-resolution + calibration now FIRE against the EXOGENOUS `resolved_outcome` column, migration 0038 — subsequent-facts/operator outcome, self-consistency-flagged when only status-transition), `deterministic.py`, `predictor.py`, `critic.py`, `optimizer.py` (GEPA — see §3.5), `consult_on_demand.py`, `journal_assessor.py` (the `journal` kind — Legba's first-person reflective voice, the ONE analyst pointed at the whole organism; OFF the fact/finding/nexus chain — see §2.7/§2.9); deterministic impls in `deterministic_handlers/` — **50 modules, the whole set enumerated in §2.7** (`entity_resolution.py`, whose canon is the SHARED `data/_entity_canon.py`, NOT a handlers-local module — the old `deterministic_handlers/_entity_canon.py` re-export shim was deleted 2026-08-02), `cross_source_dedup.py` (BOUNDED per-run scan — skips already-canonicalised content-hash groups in the DB + caps at `max_groups_per_run`=500), `cross_source_coalesce.py` (substrate-wide cross-source semantic/temporal LINKER, off-by-default — SEAM #19), `finding_supersession.py`, `indicator_tracker.py` (deterministic I&W — run-over-run diffs on the structured indicators the units emit), `collection_gap.py` (deterministic I&W — flags starved desk × dimension cells), `situation_clustering.py`, `thematic_proposal.py` (Phase-5 — detects thematic non-geo situation frames + PROPOSES them), `hypothesis_lifecycle.py`, `graph_mining.py`, `proposed_edge_governance.py` (Phase D — promotes pending `proposed_edges` into neutral `CoOccursWith` nexuses), `_graph_metrics_sink.py`, `anomaly_detection.py`, `fact_decay.py`, `calibration_tracking.py`, `integrity_sweep.py`, `entity_gc.py`, `adversarial_signals.py`, `structural_balance.py`, `nexus_decay.py`, plus the analysis-spine META handlers `scorecard_banding.py` + `scorecard_producer.py` (P4 banded scorecard), `unit_correctness_scorer.py` (P2 correctness-vs-reference gold), `forecast_acute.py` + `forecast_scoreboard.py` (the acute-forecast Brier/BSS pilot), `composition_lineage_sweep.py`, `fact_contention_arbiter.py`, `signals_retention.py`, and the enrichment / alerting / retention families this list used to omit entirely: `claim_watch.py` (the directory's LARGEST module), `alert_trigger_scan.py`, `geo_convergence_scan.py`, `narrative_mapper.py`, `evidence_archiver.py`, `band_calibration_tracker.py`, `desk_baseline.py`, `source_track_record.py`, `corpus_indexer.py`, `corpus_retention.py` (the corpus DELETE path — drains the `corpus_tombstones` queue of migration 0175 against OpenSearch, re-verifying each row is really gone first), `signal_summarizer.py`, `signal_embedder.py`, `reenrich_ner.py`, `reenrich_translation.py`, `fact_decay_scan.py`, `analyst_traces_retention.py`, `_retention_sweep.py` (the shared retention engine both retention handlers call), `_entity_geo.py`, `_watchlist_scan.py`, `bearing_gate.py` (off by default)); action-pack agency plane in `agency/` (`agency.py` hard gate, `governor.py`, `resolution.py`, `binding.py`, `substrate_read.py`, `tools.py`, `events.py`) |
| `data/provenance/` | Output-kind payloads, write helpers, receipts, budget, DLQ | `kinds.py` | the 12-member `OutputKind` enum + `KIND_REGISTRY` (`kinds.py`, now incl. `FACT` + `NEXUS` + `JOURNAL` — the journal routes to its own `journal_entries` table, OFF the fact/finding/nexus chain — + `SCORECARD`, the P4 banded per-country verdict), per-kind pydantic payloads (`models.py`, incl. `FactPayload`/`NexusPayload`/`JournalPayload`/`ScorecardPayload`), the analyst-output writers (`writes.py`, `_core.py` — incl. `write_fact`/`write_nexus` + `supersede_prior_facts`/`supersede_prior_nexuses`, `source_type`/`seed_batch_id` threading), the SHA-256 hash-chained receipt chain + verify machinery (`receipts.py`/`_core.py`, `verify.py` — incl. `verify_finding_faithfulness`, the P0-T2 faithfulness pass), durable checkpointer (`checkpointer.py`), budget accounting (`budget.py`), output DLQ (`dlq.py`) |
| `data/outputs/` | Output-kind emit handlers (analyst payloads → operator surfaces) | `_contract.py` | the `AlertEmitter`/emit Protocol + `discover_output_kinds()` (`_contract.py`/`__init__.py`), `substrate.py` (typed write-back facade), `nats_stream.py`, `webhook.py`, `alert.py`, `ui_panel.py`, `mcp_tool.py`, `a2a_skill.py`, `stix_bundle.py` (STIX 2.1) |
| substrate adapters (`data/` root) | One typed port per backing store + bootstrap | `config.py` | `postgres.py` (asyncpg + AGE codec), `nats.py` (JetStream, signal subject grammar), `qdrant.py`, `redis.py`, `opensearch.py` (a FIFTH port — see its own row below), env-driven config (`config.py`), migration runner (`migrate.py`), vocabulary seed/query (`vocabulary.py`), substrate smoke check (`smoke.py`, owns `RETIRED_TABLES`). The `data` root also holds the SHARED entity canon — `_entity_canon.py` (the real 2.1k-line implementation), `_entity_candidates.py`, `_entity_resolve.py`, `_entity_eval.py` — which lives here, not under `analysts/`, so ingestion and the analyst plane share one canon without a layering violation. Alongside it, two stdlib-only leaf codelists: `_country_aliases.py` (country NAME alias groups — Myanmar/Burma, Türkiye/Turkey) and `_fips_iso.py` (the FIPS 10-4 → ISO 3166-1 alpha-2 crosswalk GDELT ingestion translates through at the boundary, B-6 — the desks route on ISO, GDELT ships FIPS, and the codes that overlap-but-disagree misdeliver silently) |
| `data/migrations/` | SQL schema (applied in order) | `0001_baseline.sql` | **Flattened baseline + forward chain.** `0001_baseline.sql` (commit `06bab95`) collapsed the former 30-step chain into one file — extensions + AGE graph (9 vertex / 14 edge labels) + all 40 relational tables + seed data (incl. the former-0031 source-credibility `tier`/`state_affiliation` columns + seeded credibility rows). The data-analysis arc then re-opened the forward chain (`0032`…`0046`), and the analysis-spine + hygiene arc carried it on through `0047`…`0060`, the data-quality program extended it `0061`…`0075`, the 2026-07-06 audit sweep carried it through `0076`…`0080` (entity re-fold, fact/nexus junk close, cross_correlator sweep, state-media credibility), and the signal-content-depth / NER-reenrich wave carried it through `0081`…`0085` (signal_summarized/indexed/reindex/embedding/reenriched markers). Migrations past `0085` (the entity-identity/salience/journal-data wave `0086`…`0090`, the 2026-07-28 release wave `0091`…`0105`, the follow-on wave `0106`…`0116`, and the 2026-08 arc `0117`…`0185` — sparse-numbered; head `0185` today) are **not re-narrated here** — `DATA_MODEL.md`'s ["Migration head" note](DATA_MODEL.md#the-contested-claims-fact-model) and its two per-migration wave tables are the current source; this file just tracks where the tables/handlers *live in the tree*. The `0058`–`0060` composition-fold tail is enumerated in §2.4: `0032_facts_decay_columns.sql` (facts `valid_until`/`superseded_by`/`confidence_components`), `0033_nexuses.sql` (reified `nexuses` table), `0034_seed_batches.sql` (curated-seed batch ledger), `0035_entity_profiles_composite_key.sql`, `0036_signals_retention.sql`, `0037_age_output_label.sql`, `0038_hypotheses_resolved_outcome.sql` (the EXOGENOUS ACH outcome column), `0039_consult_sessions.sql`, then the DQ-sweep tail `0040`…`0046`: situations first-class + temporal repair (`0040`/`0041_situations_valid_from_repair.sql`/`0042_situations_target_id_backfill.sql`), `0043_ingestion_conf1_backfill.sql` + `0044_purge_ingestion_leader_junk.sql` (conf-1.0 sentinel cleanup), `0045_backfill_demonym_nexuses.sql` (NER demonym→country), `0046_source_poll_outcomes.sql` (the `source_poll_outcomes` non-productive-poll provenance table). There is no `0014`. The runner (`migrate.py`) globs `*.sql` in order |
| `data/predicates/` | Starlark predicate DSL (subscription / matching residual) | `compiler.py` | compile-once-on-register → LRU `CompiledPredicate` (`compiler.py`), in-sandbox evaluator (`evaluator.py`), per-surface helper catalog (`helpers.py`), compile/eval errors (`errors.py`) |
| `data/stack/` | Provider adapters resolved through the stack registry | (per family) | `llm/` (`anthropic.py`, `vllm.py`, `openai.py`, `base.py`, `pricing.py`), **`search/`** (SearXNG meta-search — `searxng.py`, `route.py`, `json_generic.py`, `liveness.py`, `base.py` — the deployed discovery engine, and previously absent from both stack-family lists in this file), `vector_store/qdrant.py`, `nats/jetstream.py`, `nlp_service/client.py`, `postgres/age.py`, `proxy/`. There is no `embedding/` family: the in-process BGE-M3 handler retired at L-205 when embeddings moved to the hosted `embed.primary.openai_compat` endpoint, and the empty namespace it left was deleted 2026-08-02 |
| `data/discovery/` | Descriptor discovery pipeline (external lists/queries → descriptors) | `registry.py` | TWO flavors with two dispatch tables. **Target** kinds (`country_list_discovery.py`, `file_sd_discovery.py`, `static.py`) come from `registry.py`'s `_KIND_MODULE_NAMES` and are what the actor's discovery cycle runs. **Source** kinds resolve separately, through `materializer._build_source_discovery_handler`'s own map — `query_source_discovery.py` is the only one, reached via `run_source_discovery_cycle` (exported + tested, but no live descriptor declares the kind, and nothing in `runtime/` calls that cycle). Do not read its absence from `_KIND_MODULE_NAMES` as death; read it as the other flavor. Plus materializers (`materializer.py`, `source_materializer.py`), `autowire.py`, `relabel.py`, `deps_resolver.py`, `disappearance.py`, `source_validate.py` |
| `data/seed/` | Curated baseline seeding (datasets → stamped facts/nexuses) | `_base.py` | `SeedSource` protocol + `SeedFact`/`SeedEntity`/`SeedNexus` payloads + `SeedContext` (`_base.py`), `SeedDriver` (`run_seed_source`: fetch→map→resolve entities→`write_fact`/`write_nexus` stamped `source_type='seed'`+`seed_batch_id`→record `seed_batches`) (`_driver.py`), `ADAPTERS` registry `get_adapter`/`list_adapters` (`__init__.py`, wiring **four** adapters), the `adapters/` adapter set (`world_baseline.py` curated-YAML leaders→facts + alliances→signed nexuses + a country-subject `head of state` office fact; `wikidata_leaders.py` SPARQL current heads of state/government → `LeaderOf` + country-subject `head of state` facts + `MemberOf` signed nexuses, with `wbgetentities` bare-QID label resolution (enwiki-sitelink fallback — resolves `Q22686`→"Donald Trump"); `acled_conflict.py` conflict backfill; `sipri_arms_transfers.py` arms-transfer — REGISTERED but **never seeded**: code-wired only, no batch has run it). Datasets in `seeds/` |
| `data/jobs/`, `data/tools/`, `data/conversions/` | Job envelopes / analyst-callable tools / version upgraders | — | `jobs/` (`envelope.py`, `media.py`, `store.py`), `tools/` (`mnemosyne_trust_query.py`), `conversions/` (`target_v1_to_v2.py`, `target_v2_to_v3.py`, `analyst_v1_to_v2.py` — EXAMPLE converters, self-declared stand-ins for a real major-version bump; they are the reference implementations of the webhook contract and the fixture surface for the conversion-webhook tests, so they are not dead weight even at zero live webhook rows) |
| **`data/alerts/`** | Outward alert fan-out (the second, separate alerting package) | `sinks.py` | the modular `AlertSink` interface + dispatcher + the one converged `AlertSinkPayload` (summary / severity / target+geo / source links / verify state / effective confidence / a lineage receipt link), with `ntfy_sink.py` + `webhook_sink.py`. **Do not confuse this with `data/outputs/alert_sinks/`** (`matrix.py`, `nats.py`, `pushover.py`, `xmpp.py`) — both exist on disk and this map used to document only the latter. The internal alert edges write `alert_sink_deliveries`; THIS package is what pushes them outward |
| **`data/rag/`** | What POPULATES the `vector:world_context` RAG | `lane4_loader.py` | heading-aware chunker (`chunker.py`, ~400-800 tokens) + the manual-ingest Lane-4 vector loader (`lane4_loader.py` — resolve → chunk → embed through the hosted embedder → upsert into the `world_context` / `tradecraft` collections). The RAG READ side is described under §"RAG"; this is the write side |
| **`data/facts/`** | The fact confidence-decay MODEL | `decay.py` | a PURE library (no DB, no mutation of stored `facts.confidence`) computing a derived `decayed_confidence` from stored confidence + time since last sighting. The stamping consumer is `deterministic_handlers/fact_decay_scan`, writing the `fact_decay_states` sidecar; the consumption seam is flag-gated. Decay is NOT all in the handler, which is what this map used to imply |
| **`data/opensearch.py`** | A FIFTH backing-store port | — | an `AsyncOpenSearch` wrapper that imports cleanly on a host without `opensearch-py` installed. Read the "one typed port per backing store" row above as four NAMED, not four total |
| **`clients/`, `shared/`** | Outbound A2A client / shared crypto | `mnemosyne_a2a.py` | `clients/mnemosyne_a2a.py` (outbound A2A), `shared/crypto.py` |
| **`prompts/`** | Per-kind DSPy prompt modules — the `prompt_module` targets | (per kind) | 19 sub-packages, addressed BY STRING from descriptor `prompt_module` fields (~23 descriptors), which is why renaming one breaks silently: `inline_target/`, `meta_findings_synthesizer/`, `journal_assessor/`, `journal_consolidator/`, `chronicle_assessor/`, `competing_hypotheses/`, `consult_on_demand/`, `country_assessor/`, `critic/`, `predictor/`, `cross_analyst_correlator/`, `cross_target_raw/`, plus the **six-lens VOICES faculty** — `lens_baserate/`, `lens_capability/`, `lens_diff/`, `lens_intent/`, `lens_trend/` over the shared `lens_common/` |

### `src/legba/runtime/` — execution

| Package | Responsibility (one line) | Start file | What lives here |
|---|---|---|---|
| `runtime/` (host + actors) | Turn descriptors into running Dapr actors | `dapr_host.py` | the `legba-runtime-dapr` FastAPI host + plane bring-up + deps resolvers (`dapr_host.py`), production actor classes `TargetActor`/`AnalystActor` (`dapr_actors.py`), `SourceActor`+`SourceCore` acquisition (`source_actor.py`), cadence/cron helpers (`dapr_cron.py`), the four-plane assembler (`source_first_runtime.py`). **`dapr_actors.py` was DECOMPOSED (task #93, 2026-06-24)** and this map never learned it: six modules came out and are where the run path actually lives — `actor_substrate_slice.py` (the `_read_substrate_slice` reader), `actor_critic.py`, `actor_output_emit.py` (`_emit_output_bindings`), `actor_payload.py`, `actor_ids.py`, `actor_retry.py`. What did NOT come out is the process-global dependency registry, still inside `dapr_actors.py` behind four `global` statements that have to move as one unit |
| `runtime/` (reconcile) | Converge running actors to the registry | `reconcile.py` | informer → work-queue → pure reconcilers → executor (`reconcile.py`), NATS event informer (`nats_informer.py`), lifecycle FSM (`lifecycle.py`), `ActorStateStore`/`ActorStateRecord` (`state.py`), desired-state reads (`registry_client.py`) |
| `runtime/subscription/` | Source→target fan-out + subscription seam | `engine.py` | `SubscriptionEngine` resolve/enforce/plan/bind (`engine.py`), `SourceRef` resolution (`sourceref.py`), open/allowlist/grant policy (`policy.py`), coarse-subject planning (`subjects.py`), two-stage exact match SQL `WHERE`+Starlark residual (`filter.py`), replay (`backfill.py`) |
| `runtime/triggers/` | Coalescing trigger plane | `engine.py` | `TriggerEngine` over `Coalescer` — dirty-marks (analyst,target) and fires on cadence / accumulation / severity gate clamped by cooldown (`engine.py`, `coalescer.py`), run dispatch (`dispatch.py`), policy + durable trigger state (`policy.py`, `state.py`) |
| `runtime/jobs/` | NATS work-queue + competing-consumer workers | `queue.py` | `JobQueue` (`queue.py`), `JobWorkerPool` (`worker.py`), `dispatch.py`, `process_media.py` handler, model-service media client (`media_client.py`) |
| `runtime/dapr_workflow/` | The optimizer's multi-hour durable GEPA loop | `worker.py` | GEPA algorithm + workflow-I/O dataclasses + in-process fallback client (`gepa.py`), deterministic orchestrator + activities (`workflow.py`), `WorkflowRuntime` registrar + `legba-dapr-workflow-worker` entry (`worker.py`), dispatch client (`client.py`), the `LegbaProviderLM` dspy adapter that **never** uses litellm (`dspy_lm.py`) |
| `runtime/` (wiring & factories) | Build per-actor deps + construct ports | `analyst_deps_builder.py` | per-kind analyst deps + run-method dispatch (`analyst_deps_builder.py`, incl. `_build_grounding_hook`, `analyst_method.py`, `deps.py`), Tier-1 knowledge grounding (`grounding.py` — `SubstrateGroundingResolver` + `collect_grounding_candidates` + `build_grounding_preamble`), port factories (`source_factory.py`, `embedding_factory.py`, `qdrant_factory.py`, `nlp_client_factory.py`, `receipt_chain_factory.py`), enrichment pipeline (`pipeline.py`), budget enforcement (`budget.py`), substrate read port (`substrate_query_port.py`), audit/checkpoint wiring (`audit_checkpointer_wiring.py`) |

### `legba-ui-v3/src/` — the SPA

| Package | Responsibility (one line) | Start file | What lives here |
|---|---|---|---|
| app shell | Bootstrap + routing + auth | `App.tsx` | `App.tsx`/`main.tsx` shell, JWT chain (`auth/jwt.ts`), client state (`state/`) |
| `lib/` | API client + live-tail + per-view models | `api.ts` | registry client (`api.ts`), WS/live-tail (`ws.ts`, `useLiveTail.ts`), starter descriptors, per-view models (`graphModel`, `findingsViews`, `alertModel`, `geoPoints`, `timelinePoints`, …) |
| `panel-registry/` + `components/` | Dynamic panel registry + shared chrome | `registry.ts` | panel registry/loader (`registry.ts`, `loader.ts`, `useRegistry.ts`), unified selection store (`state/selection.ts`), the Inspector (`components/inspector/` — `InspectorPanel.tsx` + `RecordLink.tsx` + `useInspectorDetail.ts`), shared components (`CommandPalette.tsx` record-jump palette, `Sidebar.tsx` workspace switcher + demoted menu, DescriptorBuilder/Editor, ScopePicker, StatusBar, PanelChrome) |
| `panels/` | The legacy/workbench panel set (now demoted) | (per area) | `source/`, `target/`, `analyst/`, `registry/`, `system/` panel areas + `merged/` (the S7 consolidation targets: AlertsWatches / Consult / Provenance / Timeline). `panels/dashboard/` is GONE — deleted in S7-T2 along with its `dashboard.dynamic` kind; `_DeferredStub.tsx` is the not-yet-built placeholder. Note `panels/v4/` and the top-level `v4/` are DIFFERENT directories that share a name |
| `v4/` | The "Three Rooms" v4 shell (current front door) | `RoomStub.tsx` | `world/` (the default **maplibre-gl banded-verdict choropleth** `MapLibreWorldMap` + `countryVerdicts`, with a **Leaflet fallback** when `hasWebGL` is false + KPIs + time scrubber + live feed), `flow/` (NiFi-style canvas-as-view-over-registry with live telemetry + wiring modal), `why/` (provenance trail + lineage/entity graphs + world-assessment), `case/` (case board/rail), shared `components/` (incl. the reading kit — `CitedProse` / `VerdictBadge`) |

> **Quick "where do I add X" → §6.**

---

## 0a. Corrections / honesty notes (read before trusting older docs)

These override anything in older docs or comments that implies otherwise. Each
is traceable to code.

> **How to cite in this file: symbols, never line numbers.** A 2026-08-02 audit
> re-checked twenty of the line citations this map used to carry and found
> **fourteen wrong, five of them by 150-900 lines** — a reader following one
> landed in unrelated code, which is worse than no pointer at all. They are gone;
> what remains is `module.symbol`, which `grep` resolves and which survives every
> split. Two related rules the same audit earned the hard way: **enumerations
> freeze at their write date** (the `deterministic_handlers` list named 29 of 49,
> omitting the directory's two largest modules), and **the honesty notes decay
> fastest of all** — three notes below that read as freshly-verified corrections
> had themselves reversed again by the time anyone re-checked. If you correct a
> note here, say what you verified it against and when, so the next reader can
> tell a checked claim from an inherited one. And keep live-DB row counts OUT:
> they cannot be checked from the tree, so they rot in silence.

- **The product is now the ANALYSIS SPINE, not `country_assessor`.** Older docs
  frame the monolithic per-country `country_assessor` one-pager (and an old
  verdict-from-nowhere `world_assessor`) as the product — BOTH framings are
  retired. The live spine is built bottom-up: (1) **eight bounded `inline_target`
  UNITS** — seven broad ones (`leadership_transition` / `energy_security` / `escalation` /
  `narrative_coordination` / `internal_stability` / `military_posture` /
  `economic_coercion`), each fanned out per desk across the **32 country
  desks** (19 G20 + a 13-desk high-consequence `watch` tier — Israel, Iran,
  Ukraine, Taiwan, North Korea, Pakistan, and the escalation-risk band Sudan,
  Mali, Burkina Faso, Niger, DR Congo, Myanmar, Haiti, ids
  `country_watch_il/ir/ua/tw/kp/pk/sd/ml/bf/ne/cd/mm/ht`; the units +
  `country_composition` subscribe on `has_tag("g20") or has_tag("watch")`) — plus an
  eighth, narrower unit, `proliferation_watch`, tag-scoped to the ~8
  nuclear-relevant desks (`has_tag("nuclear_watch")`) — and
  answering ONE narrow question with cited prose + a **mandatory faithfulness
  verify**; (2)
  `country_composition` (kind `meta_findings_synthesizer`) synthesizing a
  country's up to eight VERIFIED units (the seven broad ones plus, on nuclear
  desks only, `proliferation_watch`; unverified sub-claims are INNER-JOINed out); (2b)
  `region_composition` composing verified per-country reads into 5 region frames +
  the thematic cross-desk `escalation_composition` (correlation-guarded); (3)
  `world_assessor` (repointed to `meta_findings_synthesizer`) composing over the
  region compositions — NOT the old monolith; (4) `scorecard_producer` (a
  deterministic META, the 12th `OutputKind` `scorecard`) writing ONE banded row
  per active g20/watch desk (enumerating any active target tagged `g20`/`watch`)
  from high-precision rules over already-verified claims.
  `country_assessor` is **RETIRED and STOPPED** (commented out of
  `bringup_register_analysts.py`; nothing in the spine reads it — but its
  historical findings REMAIN in the DB, unread, so this is a
  stop, not a clean slate); the forecast-as-claim `country_predictor` /
  `india_energy_predictor` are **RETIRED/frozen and STOPPED** (their historical
  prediction rows likewise REMAIN), and the monolithic `country_optimizer` is
  **cadence-frozen** (descriptor still `state=active`; no reminder-flood
  regression — see SEAMS). The `journal_assessor` is **NOT frozen** — it RUNS on
  cadence (12h entry + daily consolidator) as an introspective instrument, OFF the
  fact/finding/nexus chain (it cannot pollute product output). See §2.7 for the
  full spine.
- **`OutputKind.FACT` and `write_fact` NOW EXIST** (the data-analysis arc — this
  reverses the older "no FACT / no write_fact" note). `OutputKind` now has **12**
  members — the original seven (`FINDING`, `SITUATION`, `HYPOTHESIS`,
  `PREDICTION`, `ALERT`, `META_FINDING`, `CRITIQUE`) plus `FACT`, `NEXUS`,
  `PROMPT_MODULE_CANDIDATE`, `JOURNAL`, and `SCORECARD`
  (`data/provenance/kinds.py`).
  `provenance/writes.py`
  now exposes `write_fact` and `write_nexus` plus
  `supersede_prior_facts` / `supersede_prior_nexuses`; both
  write helpers thread `source_type` / `seed_batch_id` so
  curated-seed rows are stamped and selectively superseded apart from agent-
  authored ones. Facts are now created through the output subsystem (by
  `fact_extractor` enrichment + the seed driver), not only `UPDATE`d by
  `fact_decay`.
- **`facts` decay columns NOW EXIST** (`migrations/0032_facts_decay_columns.sql`
  added `valid_until` / `superseded_by` / `confidence_components`), reversing the
  older code↔schema-drift note — `fact_decay`'s temporal-expiry and
  confidence-decay branches run against real columns now.
- **Read the KIND and the DESCRIPTOR separately — `meta_findings_synthesizer`
  is very much alive; the descriptor called `analyst_meta_synthesizer` is not.**
  This paragraph previously said both it and `cross_analyst_correlator` "are now
  REGISTERED", citing the bring-up list; re-verified 2026-08-02 against
  `scripts/bringup_register_analysts.py`, **both those lines are commented out**
  ("RETIRED — Piece 3 (Task B) legacy synthesizer" / "RETIRED 2026-07-09 …
  0 consumers"). What retired is the LEGACY standalone synthesizer descriptor,
  superseded by the composition spine. The `meta_findings_synthesizer` KIND is
  bound `state: active` by five OTHER descriptors — `analyst_world_assessor`,
  `analyst_country_composition`, `analyst_region_composition`,
  `analyst_escalation_composition`, `analyst_escalation_dyad` — so the module is
  the #2 largest in the tree and runs constantly. `cross_analyst_correlator` has
  no live descriptor at all. `cross_target_raw` is registered in code (the kind
  loader imports it and `analyst_deps_builder` dispatches it) but **zero
  descriptors declare it**, so no actor can bind it — present + dispatchable ≠
  running, and a retired descriptor NAME never proves its kind is dead.
- **`nexuses` table is BACK (reified).** The earlier "RETIRED" note is reversed:
  `0033_nexuses.sql` re-creates a `nexuses` table for the reified signed/typed
  relationship edges produced by `relationship_reifier` and consumed by
  `structural_balance` / `graph_mining` / `nexus_decay`. (`smoke.py`'s
  `RETIRED_TABLES` no longer asserts its absence.) Ignore older "nexuses is
  retired / AGE-edges-only" references.
- **STIX emit is wired, and TAXII upload is no longer a stub.** The analyst run
  path dispatches emit-capable output kinds via
  `actor_output_emit._emit_output_bindings`, which resolves them through
  `discover_output_kinds()` and is called from the run path in `dapr_actors`;
  `stix_bundle.emit` produces a STIX 2.1 bundle. The older note here said "TAXII
  *upload* remains a documented stub" — **re-verified 2026-08-02, it is closed**:
  `outputs/taxii_client.py` is a full TAXII 2.1 push client (hand-rolled over the
  shared `HttpClientLike` port rather than pulling in `taxii2-client`), imported
  by `stix_bundle.py` and invoked from its `_maybe_push_taxii` tail. What gates
  the path is a DESCRIPTOR, not missing code: the one binding descriptor,
  `analyst_country_assessor.yaml`, sits at `state: draft`. Older
  "BUILT-BUT-UNWIRED" notes on STIX predate the wiring entirely.
- **Tier-1 knowledge grounding EXISTS and is LIVE** (the stale-cutoff fix). A new
  `runtime/grounding.py` (`SubstrateGroundingResolver` + `build_grounding_preamble`),
  an opt-in `GroundingBlock` descriptor field (`data/schemas/analyst.py`,
  default off), the `inline_target` **GROUND** phase, and the
  `analyst_deps_builder._build_grounding_hook` gate inject a dated "AUTHORITATIVE
  CURRENT CONTEXT" preamble — built from the CURRENT seed/curated substrate facts
  — into the LLM prompt of the nine bounded UNITS (grounding is opted IN on
  `leadership_transition` / `energy_security` / `escalation` /
  `narrative_coordination` / `internal_stability` / `military_posture` /
  `economic_coercion` / `proliferation_watch`), so a unit reasons over accumulated substrate state
  and its stale model priors are superseded. Two units (`leadership_transition`,
  `internal_stability`) additionally draw the LIVE `vector:world_context` RAG source
  (opportunistic, relevance-floored, country-filtered, degrade-not-drop — see §"RAG"
  below). The `valid_until`
  field now exists on `FactPayload`/`NexusPayload`, so the seed/supersession path
  is temporally honest end-to-end (reverses the older "valid_until dropped" note
  in §2.14). The ACH calibration leg also now FIRES against the exogenous
  `resolved_outcome` column (migration 0038) — reverses the older "NOT yet
  firing" note in §0/§2.7.
- **The `journal` kind EXISTS and is LIVE** (the 11th `OutputKind` — Legba's
  first-person reflective voice). Unlike every other meta-analyst, which cuts ONE
  slice, the journal cuts ACROSS the whole flow, narrating a coherent point of
  view OVER the rest of the system ("Poetry without evidence is noise. Evidence
  without perspective is just a log file."). It is **OFF the fact/finding/nexus
  chain** — the single most important framing: a journal row is a *perspective
  OVER* the provenance chain, never a *member of* it. It lands in a **dedicated
  `journal_entries` table (migration 0048)**, NOT `analyst_outputs`; it carries an
  ALWAYS-EMPTY `derived_from` and is deliberately ABSENT from the lineage catalog
  (`lineage_api._SUBSTRATE_TABLES`), so a downstream lineage walk FROM a
  fact/situation/nexus can NEVER surface a journal node. It must NEVER write a
  fact/finding/nexus (grant-layer backstop: it is granted ONLY the read +
  propose packs; chain-layer enforcement is gated by
  `tests/data_pkg/test_journal_off_chain.py`). Do NOT place the journal inside the
  signals → entities/facts → relations/nexuses → situations → assessments
  lineage; it is a reflective layer ABOVE / ACROSS that chain (the exception that
  is NOT a node in any `derived_from` walk). Impl: `data/analysts/journal_assessor.py`;
  payload `JournalPayload` (`data/provenance/models.py`); see §2.7/§2.9.

---

## 1. Top-level layout

```
src/legba/            Python package (the platform)
legba-ui-v3/          React/TypeScript single-page UI
legba-models/         AI-model service (vLLM LLM + bge-m3 embeddings + NLLB/spaCy NER)
descriptors/          Example source/target/analyst/action-pack/discovery YAML
scripts/              Bring-up registrars, seeders, backfills, smoke tests
docker/               Dockerfiles (registry / runtime / mcp) + Caddyfile
dapr/components/      Dapr component manifests (statestore, pubsub, secrets, config)
docker-compose.yml    Full stack: substrate + Dapr + Legba services + UI + Caddy
pyproject.toml        Package metadata + console scripts
docs/                 Design & operations docs (this file lives here)
tests/                Test suite
```

The package splits along the architectural seam: `src/legba/data/` is the
**declarative + substrate** half (schemas, registry, handler libraries, the
substrate adapters, migrations) and `src/legba/runtime/` is the **execution**
half (the Dapr actor host, the four runtime planes, reconcile loop).

---

## 2. `src/legba/data/` — declarative model + substrate

### 2.1 `schemas/` — the descriptor types

The Pydantic descriptor schemas (strict, `extra="forbid"`, content-hashable):

| File | What |
|---|---|
| `source.py` | `SourceDescriptor` (acquisition unit), `SourceRef`, `Subscription`, `subscription_policy` |
| `target.py` | `TargetDescriptor`, polymorphic `TargetScope` (Geo/Estate/Entity), `OutputBinding`, source-ref subscriptions |
| `analyst.py` | `AnalystDescriptor`, the open `AnalystKind` taxonomy, coalescing/trigger config, `GroundingBlock` (optional `grounding` field — Tier-1 knowledge grounding, off by default) |
| `action_pack.py` | `ActionPack` + `ActionPackRef` + `PackGovernor` (allow-listed capability bundles) |
| `stack.py` | `StackComponentDescriptor` (substrate components + LLM providers) |
| `lifecycle.py` | `LifecycleState` FSM + `AbstractionLevel` |
| `properties.py` | shared property types (`Cron`, `FactoryValue`, …) |
| `vocabulary.py` | vocabulary-entry shapes (entity classes, relationship types, analyst kinds) |
| `versioning.py` | descriptor version / content-hash helpers |

`AnalystKind` is an **open** taxonomy of built-in kinds (`inline_target`,
`cross_target_raw`, `meta_findings_synthesizer`, `relationship_reifier`,
`competing_hypotheses`, `deterministic`, `predictor`, `critic`, `optimizer`,
`cross_analyst_correlator`, `consult_on_demand`) plus operator-registered kinds
via the vocabulary registry. The `journal_assessor` kind is one such
**extension kind** — registered via `register_analyst_kind` + the
`vocabulary_entries` family, NOT a member of the closed built-in enum (so the
built-in count is unchanged) — see §2.7.

### 2.2 `registry/` — descriptor registry, vault, HTTP/WS API

The control plane. `descriptor.py` is the content-hashed instance registry
(Ed25519-signed audit log, DLQ on validation failure, NATS events); `stack.py`
+ `credentials.py` are the stack registry + XSalsa20-Poly1305 credential vault;
`signing.py` / `audit.py` own the receipt/audit chains; `dlq.py` the
dead-letter path; `events.py` / `streams.py` / `emitter.py` the NATS event
surface; `vocabulary_cache.py` mirrors the vocabulary tables.

The HTTP/WebSocket API is `server.py` (the `legba-registry` entry point, port
8090) mounting several routers:

| Router module | Mount prefix | Concern |
|---|---|---|
| `api.py` | `/api/v1/registry` | descriptor CRUD, sources, action-packs, stack, vault |
| `v3_api.py` | `/api/v1/v3` | runtime actor rows + UI v3 views |
| `substrate_reads_api.py` | `/api/v1` | read-through substrate queries for UI panels |
| `lineage_api.py` | `/api/v1` | provenance / derived-from lineage |
| `entities_api.py` | `/api/v1` | entity knowledge-graph reads |
| `graph_walk_api.py` | `/api/v1` | K-G4 graph WALK over `entity_edges` — `GET /graph/ego` (anchored 1-hop neighbourhood + family/type/polarity/confidence/time filters, unfiltered facets as the honest denominator, `known`-set induced-edge stitching) and `GET /graph/edge/{id}` (why an edge exists: snippet, resolved source signals, provenance). No depth parameter by design — every hop is a fresh anchored ego, see `docs/AGE_PROBE_REPORT.md` §5.2 |
| `runtime_telemetry_api.py` | `/api/v1` | actor health / runtime telemetry |
| `budget_api.py` | `/api/v1/budget` | budget envelope reads |
| `source_credibility_api.py` | `/api/v1` | source-credibility reads |
| `consult_api.py` | `/api/v1` | on-demand consult (proxies the consult analyst actor via daprd) |
| `journal_api.py` | `/api/v1` | journal entries read — `GET /api/v1/journal` (the reflective-voice feed) |
| `journal_proposals_api.py` | `/api/v1` | journal proposal review queue — `GET /api/v1/journal_proposals` + `POST .../{id}/accept` / `.../{id}/reject` (human-gated) |
| `production_gauge_api.py` | `/api/v1/v3` | S-1 expected-vs-actual production gauge — `GET /api/v1/v3/system/production-gauge`, one row per producing loop (analyst cadence, analyst output, source signal production, declared backlog drain), worst-first |

`production_gauge.py` holds the gauge's expectation model and is the SAME
judgment the `production_deficit` alert-trigger class reads (the
`source_freshness.py` precedent — one implementation, two readers, so no
mirrored SQL and no drift guard).

`production_gauge_integrity.py` (R-train 2026-08-05) adds three INTEGRITY loops
to the same `read_gauge` walk. The four original classes ask whether a loop
PRODUCES; these ask whether what it produces is still what we think it is:
`judge_availability` (the adjudicated share of critiques — a 26-hour judge outage
wrote 611 floor-only critiques with no alarm), `descriptor_prompt_drift` and
`descriptor_state_drift` (live registry head vs the tree, compared against
`descriptor_prompts.json`, which `scripts/gen_descriptor_prompt_manifest.py`
compiles from `descriptors/*.yaml` because that directory ships in no image).
All three are read-only; a staleness test regenerates the manifest and asserts
byte-equality.

`discovered_materializer.py` + `conversion.py` support discovery and
descriptor-version conversion. `health.py` is the liveness surface.

### 2.3 Substrate adapters (`data/` root modules)

One module per backing store, each a thin typed port:
`postgres.py` (+ `stack/postgres/age.py` for Apache AGE graph),
`qdrant.py` (vectors), `redis.py`,
`nats.py` (JetStream: `SIGNAL_STREAM_NAME = "legba_signals"`, subject grammar
`legba.signals.<tenant>.<source_token>.<modality>.<event_class>`).
`config.py` is process config; `migrate.py` applies migrations;
`vocabulary.py` seeds/queries vocabularies; `smoke.py` is a substrate smoke
check.

### 2.4 `migrations/` — schema

**Flattened to one baseline.** Commit `06bab95` collapsed the former 30-step
`0001`…`0031` chain into a single `0001_baseline.sql` (clean-slate release — no
live instances to upgrade). It was derived by `pg_dump --column-inserts` of a
fresh full-chain migrate, with the Apache AGE graph setup (`create_graph` /
`create_vlabel` / `create_elabel`, which `pg_dump` cannot reproduce) carried
verbatim from the former `0004` (see the header comment at the top of
`0001_baseline.sql`). So the single file now builds the extensions + the AGE
graph (9 vertex / 14 edge labels) + all 40 relational tables + seed data —
including everything the historical chain added: the source-first
signal/subscription tables (former `0024`), the coalescing trigger state (former
`0028`, the `trigger_state` table), the entity-graph tables
(former `0029`), and the source-credibility `tier` + `state_affiliation` columns
+ seed rows (former `0031`, the `source_credibility` table).
**The forward chain re-opened after the baseline** for the data-analysis arc:
`0032_facts_decay_columns.sql` (facts `valid_until` / `superseded_by` /
`confidence_components` — reversing the former code↔schema drift),
`0033_nexuses.sql` (re-lands a first-class reified `nexuses` table; the former
`0030` drop is moot), `0034_seed_batches.sql` (the curated-seed batch ledger),
then `0035_entity_profiles_composite_key.sql`, `0036_signals_retention.sql`,
`0037_age_output_label.sql`, `0038_hypotheses_resolved_outcome.sql` (the
exogenous ACH `resolved_outcome` column that lets calibration grade against new
evidence rather than itself), and `0039_consult_sessions.sql`. The DQ-sweep then
extended the chain through `0040`/`0041_situations_valid_from_repair.sql`
/ `0042_situations_target_id_backfill.sql` (situations as first-class objects +
temporal repair), `0043_ingestion_conf1_backfill.sql` +
`0044_purge_ingestion_leader_junk.sql` (the conf-1.0-sentinel cleanup),
`0045_backfill_demonym_nexuses.sql` (NER demonym→country), and
`0046_source_poll_outcomes.sql` (the `source_poll_outcomes` non-productive-poll
provenance table). The analysis-spine + hygiene arc then carried the chain
through `0047`…`0060` (the data-quality program `0061`…`0075`, the 2026-07-06
audit sweep `0076`…`0080`, and the signal-content-depth / NER-reenrich wave
`0081`…`0085`, signal_summarized/indexed/reindex/embedding/reenriched markers).
The head has since advanced well past `0085` — see the note at the top of
this section and `DATA_MODEL.md` for `0086` onward:
`0047_acute_forecasts.sql` (the `acute_forecasts` Brier/BSS
pilot table), `0048_journal.sql` (the off-chain `journal_entries` table),
`0049_facts_collapse_dup_open.sql`, `0050_receipt_chain_fork_tombstone.sql`,
`0051`/`0056_prune_dangling_derived_from*.sql` (lineage-integrity dangling-ref
prunes), `0052_remediation_data_cleanup.sql`,
`0053_retire_template_junk_sources.sql`, `0054_facts_source_credibility.sql`,
`0055_fact_contention.sql` (the contested-claims sidecar),
`0057_unit_reference_labels.sql` (the operator gold labels the P2
`unit_correctness_scorer` grades against), and the composition-tower tail
`0058_composition_supersession_fold.sql` / `0059_critique_analyzed_output_id_index.sql`
/ `0060_fold_null_target_composition_heads.sql` (one live head per desk across the
composition supersession). The chain has no `0014`.
The runner
(`migrate.py`) globs `*.sql` in order; cold-start from empty volumes is one-shot.
The historical 30-step chain remains in git; add a migration by dropping the
next-numbered `.sql` here (see §5).

### 2.5 `sources/` — source-kind handler library

Acquisition handlers. `_contract.py` / `_protocols.py` define the handler
Protocol (`pull` / `health_check` / lifecycle hooks + the `Signal` it yields)
— no ABC inheritance, so external packages register kinds without importing a
base class. `baseline.py` is the **per-source baseline pipeline** (runs once
per signal: language/geo/entity enrichment + media-tier branching). Handlers:
`rss.py`, `gdelt.py` (BigQuery), `gdelt_files.py` (the keyless 15-min raw
event-CSV file-dump path — replaced `gdelt`'s DOC 2.0 API kind after it started
429-ing at the IP level, see `docs/DATA_SOURCES.md` §3), `acled.py`,
`ucdp.py` (S1-T9 — Uppsala Conflict Data Program GED events, token-gated,
feeds `escalation`/`military_posture`), `mediacloud.py`, `opensanctions.py`,
`common_crawl.py`, `intelmq.py`, `firecrawl.py`, `scraper.py` (+ `scrapers/`),
`telegram.py`, `discord.py`, `geojson.py` (model-free `structured` /
`application/geo+json` modality), `json_api.py` (generic cursor-driven polled
JSON/CSV API kind — url-template windows + JSONPath-lite extraction + vault
auth), `generic_webhook.py` + `webhook_router.py`
(shared inbound push router). `provision.py` owns idempotent outbound
upstream-watch provisioning.

### 2.6 `filters/` — enrichment-kind handler library

In-flight enrichment / transforms over a `Signal`. `_contract.py` is the
`StreamHandler` Protocol. Baseline enrichers: `language_detect.py`,
`geocode.py`, `ner.py`, `classify.py`, `dedupe.py`, `ingest_dedupe.py`
(source-side dedup tiers 1–2, applied by `SourceCore`), `source_credibility.py`.
`fact_extractor.py` is the fact-extraction enrichment stage — turns the hosted
NLP stack's GLiREL relation triples (`jackboyla/glirel-large-v0`, which emit
REAL per-relation confidence scores, NOT a synthetic 1.0 — live facts span
0.75/0.80/0.92/0.95) (or the 8B provider plane) into `FACT` outputs, stamps
`valid_from` event-time, applies a real ingestion confidence (a 0.75 fallback,
`_INGESTION_DEFAULT_CONFIDENCE`, for a missing score),
rejects NER-junk triples (`_is_junk_triple` — numeric/date/unit endpoints) plus
the opt-in `reject_quantity_endpoints` gate, dedupes identical triples, and
normalizes the predicate to the canonical lowercase-spaced vocabulary form
(`vocabulary.normalize_predicate`, shared with the `write_fact`/`write_nexus`
paths). (In-repo `src/*.py` comments that still say "REBEL" are STALE — the
deployed relation backend is GLiREL; the code-comment cleanup + a
conf-1.0-sentinel-vs-GLiREL-real-scores reconciliation are a tracked code
follow-up, not yet done.) SLM-backed refiners (call the model
service): `slm_classification_refine.py`, `slm_entity_resolve.py`,
`slm_relationship_validate.py`.

### 2.7 `analysts/` — analyst-kind implementations

**The analysis spine (the product), bottom-up.** These are the modules to read
first; each stage only ever consumes VERIFIED output of the one below it:

1. **Nine bounded reasoning UNITS** — `inline_target.py`, one narrow question
   each. Seven fan out per desk across the **32 country desks** via a
   `has_tag("g20") or has_tag("watch")` subscription: the 19 G20 desks plus a
   13-desk high-consequence `watch` tier — Israel, Iran, Ukraine, Taiwan, North
   Korea, Pakistan, and the escalation-risk band Sudan, Mali, Burkina Faso,
   Niger, DR Congo, Myanmar, Haiti (ids
   `country_watch_il/ir/ua/tw/kp/pk/sd/ml/bf/ne/cd/mm/ht`, registered by
   `scripts/bringup_register_watch_country_targets.py`; adding a desk is
   register-a-target, no code). The eighth, `proliferation_watch`, is narrower:
   it is tag-scoped instead to the ~8 nuclear-relevant desks via
   `has_tag("nuclear_watch")`. The ninth, `disruption_status`, is tag-scoped
   the same way but off the country plane entirely, via
   `has_tag("supply_chain")` to the thematic lane/flow desks.
   The live units are `leadership_transition`, `energy_security`, `escalation`,
   `narrative_coordination`, `internal_stability`, `military_posture`,
   `economic_coercion`, and `proliferation_watch` (descriptors of the same
   name). A run: ASSEMBLE a
   cited 72h signal slice + a Tier-1 "AUTHORITATIVE CURRENT CONTEXT" grounding
   preamble of accumulated facts/nexuses/situations (§3.4) → cited SYNTHESIZE (a
   strict-JSON `FindingPayload` whose prose carries `[N]` markers mapped to
   signal ids; `_normalize_citation_markers` folds full-width `【N】`/`［N］`
   variants back to ASCII before parsing) → a **mandatory faithfulness VERIFY**
   → an `effective_confidence` fold + drill-to-source provenance. Skill is a
   PER-UNIT number, never a platform boast.
2. **Per-country composition** — `meta_findings_synthesizer.py` run as the
   `country_composition` descriptor: reads a country's up to eight verified
   units (the seven broad ones plus, on nuclear desks only, `proliferation_watch`)
   and writes a hedged, cited synthesis. Unverified sub-claims never enter it — the
   gather INNER-JOINs on the faithfulness critique. Supersession keeps one live
   head per desk.
2b. **Per-region + thematic composition** — the SAME `meta_findings_synthesizer.py`
   run as `region_composition` (5 region frames: Africa, Americas, Europe,
   Indo-Pacific, MENA) and `escalation_composition` (a thematic cross-desk
   escalation composition carrying a correlation guard against double-counting
   correlated desks).
3. **World composition** — the SAME `meta_findings_synthesizer.py` run as
   `world_assessor` (a single GLOBAL run, repointed from `inline_target`):
   composes over the region compositions into a cited, hedged world view that
   drills world → region → country → units → source. It is NOT the retired
   verdict-from-nowhere monolith.
4. **Banded scorecard** — `deterministic_handlers/scorecard_producer.py` (via
   `scorecard_banding.py`): a deterministic, LLM-free META that writes ONE
   `kind='scorecard'` row per active g20/watch desk (it enumerates any active
   target tagged `g20`/`watch`) from high-precision rules over already-verified
   claims banded over a 14-day window (severity tag × `effective_confidence`,
   demote-never-promote). Every band NAMES the verified-claim id it rests on; a
   dimension with no qualifying verified claim reads `insufficient-evidence` with
   an explicit machine `reason` (never a fabricated band); a per-claim
   faithfulness below the floor demotes to `low-faithfulness`. Live output is a
   MIX — e.g. the US unit's faithfulness is genuinely low, so its card reads
   all-insufficient. That is the honest design, not a bug.

**The mandatory verify pass** lives in `provenance/verify.py`
(`verify_finding_faithfulness`) dispatched by
`runtime/actor_critic.py::verify_inline_target_finding`. A DETERMINISTIC citation
floor ALWAYS runs (a claim with no resolving `[N]` marker is an unsupported
span); an OPTIONAL LLM judge — resolved through the judge route
(`LEGBA_JUDGE_STACK_REF` env > `method.llm.judge` > `.verify` > `.primary`;
descriptor default same-model, the reference deployment cross-family on a
hosted Gemma judge) — engages only when the descriptor
declares `method.llm.verify` AND `LEGBA_VERIFY_LLM_JUDGE` is on, and soft-fails to the
floor when unreachable (`judge_status='deterministic'`, PROVISIONAL under a
ceiling — never a fabricated number). Every critique stamps `judge_llm_ref` +
`judge_pipeline_version`. The verdict is
persisted as a `critique`; `effective_confidence = min(confidence,
faithfulness_score)` is folded at read time and gates a visible low-confidence
tier — it never hard-deletes.

**Skill scoreboard (honest-null where unmeasured).**
`deterministic_handlers/unit_correctness_scorer.py` reports each unit's
faithfulness + correctness-vs-reference against the operator gold labels
(migration 0057); the gold set is TINY (n≈1, reported insufficient-sample).
`calibration_tracking.py` reports the exogenous Brier, and the acute-forecast BSS
comes from `forecast_scoreboard.py` / `forecast_acute.py`. Each no-skill or
insufficient-sample result is PUBLISHED, not hidden — the forecast pilot
currently reports NO proven skill.

**Retirements / freezes (documented in SEAMS).** `country_assessor` (the
monolithic per-country one-pager) is RETIRED and STOPPED — the units +
composition supersede it and nothing in the spine reads it; ~1.2k historical
`country_assessor` findings REMAIN in the DB (unread), so this is a stop, not a
clean slate. `predictor.py` still backs the forecast-as-claim `country_predictor`
/ `india_energy_predictor`, but both are RETIRED/frozen and STOPPED (~539
historical prediction rows REMAIN); forecasting returns ONLY as the measured
`acute_forecasts` Brier/BSS scoreboard, never as a free-text claim — and that
scoreboard currently reports NO proven skill (honest). The monolithic
`country_optimizer` stays cadence-frozen (descriptor still `state=active`; no
reminder-flood regression); GEPA returns scoped to ONE measured unit as
`unit_optimizer` (§3.5). The `journal_assessor` is NOT frozen — it RUNS on cadence
(12h entry + daily consolidator) as an introspective instrument (see below).

**The rest of the analyst-kind modules.**
One module per built-in analyst kind: `inline_target.py`, `cross_target_raw.py`
(present, imported by the kind loader, dispatched by `analyst_deps_builder`, and
bound by NOTHING — zero descriptors declare the kind),
`meta_findings_synthesizer.py`, `relationship_reifier.py` (META — types
co-mentioned entity pairs from `proposed_edges` into signed typed `nexuses`,
8B LLM, never litellm), `competing_hypotheses.py` (META ACH — competing
hypotheses + LLM-scored evidence×diagnosticity matrix + ±2 transitions; the
outcome-resolution/calibration leg now FIRES against the EXOGENOUS
`resolved_outcome` column (migration 0038) — the outcome is stamped against
subsequent facts or an operator label, and `calibration_tracking` flags the
Brier `self_consistency_only` when every resolved row came only from a
status-transition; see ANALYSIS §7.4-7.5),
`deterministic.py`, `predictor.py` (RETIRED/STOPPED — the forecast-as-claim kind;
see the spine notes above), `critic.py`, `optimizer.py` (GEPA loop —
`unit_optimizer` LIVE / `country_optimizer` cadence-frozen; see §3.5),
`cross_analyst_correlator.py`,
`deep_consult.py`, `consult_on_demand.py`,
`journal_assessor.py` (the `journal` kind — see below).
`deterministic_handlers/` holds the deterministic analyst impls (e.g. the spine
handlers `scorecard_banding.py` + `scorecard_producer.py`,
`unit_correctness_scorer.py`, `forecast_acute.py` + `forecast_scoreboard.py`,
`composition_lineage_sweep.py`, plus `fact_contention_arbiter.py`,
`signals_retention.py`, and the earlier set:
`entity_resolution.py` (its canon is `data/_entity_canon.py` — `canonicalize_entity`
surface-form alias/gazetteer merge + NER type correction, Phase C — which lives in
the SHARED `data` layer so ingestion, the resolver, the reifier and
`proposed_edge_governance` reach one canon without a layering violation; the
handlers-local re-export shim that used to sit here was deleted 2026-08-02, and a
map that points at a 39-line shim instead of the 2,115-line implementation is
worse than no pointer), `cross_source_dedup.py`
(BOUNDED per-run scan — `_resolve_exact_pool` skips
already-canonicalised content-hash groups in the DB and `handle` caps the scan at
the `max_groups_per_run` option, default 500, so the backlog drains across
cadences inside the actor-invoke budget), `cross_source_coalesce.py`
(off-by-default substrate-wide cross-source semantic/temporal linker — SEAM #19),
`finding_supersession.py`, `situation_clustering.py`, `thematic_proposal.py`
(Phase-5 — detects thematic non-geo situation frames + PROPOSES them for
promotion), `hypothesis_lifecycle.py`, `structural_balance.py`,
`graph_mining.py`, `nexus_decay.py`, `calibration_tracking.py`, `fact_decay.py`,
`proposed_edge_governance.py` (Phase D — promote/reject the `proposed_edges` queue),
`_graph_metrics_sink.py` (Phase D — `write_graph_metric` helper),
`anomaly_detection.py`).

**The rest of the directory — it is 50 modules, not the ~29 this map used to
name, and the two LARGEST were among the unnamed.** The families the older
enumeration froze out:

- alerting / watch — `claim_watch.py` (the biggest module in the directory),
  `alert_trigger_scan.py`, `_watchlist_scan.py`;
- geo / narrative — `geo_convergence_scan.py`, `narrative_mapper.py`,
  `_entity_geo.py`;
- corpus enrichment sweeps — `corpus_indexer.py`, `corpus_retention.py` (its
  DELETE mirror — drains `corpus_tombstones` so a purged signal's doc leaves
  the index; before it, nothing ever deleted from OpenSearch and 41.5% of the
  corpus pointed at rows that no longer existed), `signal_summarizer.py`,
  `signal_embedder.py`, `reenrich_ner.py`, `reenrich_translation.py`;
- calibration / baselines — `band_calibration_tracker.py`, `desk_baseline.py`,
  `source_track_record.py`;
- retention + archive — `_retention_sweep.py` (the ONE engine both retention
  handlers drive, configured by the `retention_policies` table),
  `analyst_traces_retention.py`, `evidence_archiver.py`, `fact_decay_scan.py`;
- gated off — `bearing_gate.py` (`DEFAULT_BEARING_GATE = "off"`).

The dispatch table is `deterministic.SUB_HANDLERS` (40 entries); the modules NOT
in it are imported helpers, not orphans. Note the package `__init__.py` eagerly
imports only a subset while `deterministic.py` imports every registered one —
two partial, hand-maintained import lists for one directory. `agency/` is the **action-pack
agency plane**: `agency.py` (`run_pack_tool` hard gate), `governor.py`
(per-pack governor + budget), `resolution.py` (capability resolution
`analyst.action_packs ∩ target.allowed_action_packs ∩ pack.applicability`),
`tools.py`, `substrate_read.py` (the consult kind's governed read tools),
`binding.py` (the per-analyst production on-ramp — `AgencyToolBinding` +
`EscalationBinding`), `events.py`.

**`journal_assessor.py` — the `journal` kind (Legba's first-person reflective
voice).** The ONE analyst pointed at the whole organism (its own self / state /
flow): every other meta-analyst cuts ONE slice, the journal cuts ACROSS the
entire flow, narrating a coherent point of view OVER the rest of the system.
It is an **extension kind** (registered via `register_analyst_kind` +
the `vocabulary_entries` family, NOT a built-in `AnalystKind` enum member —
`scripts/bringup_register_analysts.py`), and a **META analyst** — a single GLOBAL
run per cadence tick (`target_filter=None`, like `world_assessor`). `OUTPUT_KIND
= OutputKind.JOURNAL`; it is **OFF the fact/finding/nexus chain** (writes only its
own `journal_entries` row — never a fact/finding/nexus; gated by
`tests/data_pkg/test_journal_off_chain.py`; see §2.9 for the off-chain framing).
The engine is `method.kind: llm_planner` — the **in-actor agentic GATHER**
envelope (a one-soul staged arc PLAN → GATHER → NARRATE, the persona re-loaded
every phase), NOT the `deep_consult` Dapr workflow (that path rides the broken
long-activity round-trip — see §3.5 / SEAMS). The single `run_method` selects the
`entry_kind` from the running analyst's `id`. **Two descriptors, ONE kind** (both
declare `identity.kind: journal_assessor`):
- `descriptors/analyst_journal_assessor.yaml` — the **entry tier**
  (`entry_kind='entry'`, pure append). It RUNS on cadence
  (`fallback_schedule: "0 0,12 * * *"` — every 12h; the earlier entry-tier freeze
  is REVERSED), writing one introspective entry each tick as an always-on
  instrument that is OFF the fact/finding/nexus chain (so it cannot pollute
  product output). Routing its reflections back into live descriptors via the
  human-gated proposal queue (below) is a FUTURE item, not the always-on path.
- `descriptors/analyst_journal_consolidator.yaml` — the **consolidation tier**
  (daily at 02:00 UTC, `"0 2 * * *"`); distils its prior consolidation + recent
  entries into one forward-carried narrative, emits `entry_kind='consolidation'`,
  and fires `supersede_prior_consolidation` (closes the prior open consolidation,
  opens this one — at most one open consolidation, enforced by a partial-unique
  index in 0048).
**Per-phase LLM split.** The heavy GATHER investigation loop runs on the local
gpt-oss / vLLM plane (`method.llm.primary` → the `llm.primary.openai_compat`
stack component); the VOICE (the NARRATE synthesis) resolves a SECOND handler
(`method.llm.narrate`) that ALSO points at `llm.primary.openai_compat` — the same
core plane (it previously ran on Anthropic Opus 4.8; moved fully to core
2026-07-06, so the journal costs NO Anthropic spend — the billed Anthropic plane
is reserved for the on-demand `consult` / `deep_consult` kinds only).
The deps builder reads the optional `method.llm.narrate.raw` and resolves a
second handler; analysts without `method.llm.narrate` fall back to the single
primary handler byte-unchanged (`method.llm` is an open dict — no schema change).
`narrate.max_tokens` (16384 entry / 24576 consolidation) governs only the narrate
voice output; it is never sent to the vLLM gather (which uses its own server
budget). `gather.max_rounds` = 6 (hard ceiling); `budget_tokens_per_day` =
2,000,000; grounding enabled (slice_entities).
**Prompts.** `legba.prompts.journal_assessor:JOURNAL_SYSTEM` (entry persona) +
`legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM` (consolidation persona).
**Packs + propose-and-gate (the never-write-a-fact hygiene invariant).** Granted
ONLY two packs — `journal_read` (`descriptors/action_pack_journal_read.yaml`: 14
read tools, incl. 9 net-new self-instruments — `get_assessments`,
`get_graph_structure`, `get_structural_balance`, `get_critic_scores`,
`get_calibration`, `get_run_health`, `get_source_health`, `get_budget_status`,
`get_journal_delta`) and `journal_propose`
(`descriptors/action_pack_journal_propose.yaml`). BOTH are non-write-fact — the
grant-layer backstop for the off-chain invariant. The journal writes ONLY its own
entries + consolidations directly; everything outward — a correction, a change,
or a `self_revision` (incl. changes to its own instructions via
`propose_self_revision`; protected sections auto-reject) — goes to the
HUMAN-GATED `journal_proposals` queue, never a live table. A human accepts /
rejects; the accept path runs an idempotent per-kind apply worker. Its only
un-gated effect is its OWN continuity (it reads its own last entry + current
consolidation into its next run). HONEST CAVEAT: the `change`-apply path is
import-verified but NOT yet exercised against a live registry; the `correction` +
`self_revision` apply paths ARE tested end-to-end. (A critic + an optimizer over
the journal's own voice — Wave 5 — is designed-not-built, gated on first building
a critic actuator.)

### 2.8 `outputs/` — output-kind handler library

Analyst-emitted payloads fanned to operator surfaces. `_contract.py` is the
`AlertEmitter`/emit Protocol; `discover_output_kinds()` (`__init__.py`) is the
registry the actor run path dispatches against. Kinds: `substrate.py` (typed
write-back facade over `provenance/writes.py`), `nats_stream.py`, `webhook.py`,
`alert.py` + `alert_sinks/` (`matrix.py`, `nats.py`, `pushover.py`, `xmpp.py`),
`ui_panel.py`, `mcp_tool.py`, `a2a_skill.py`, `stix_bundle.py` + `taxii_client.py`.
The `stix_bundle.emit` STIX 2.1 path is **wired** — the run path calls
`actor_output_emit._emit_output_bindings`, which discovers emit-capable kinds and
dispatches them. `taxii_client.push_bundle_to_taxii` is a real TAXII 2.1 push
client (see §0a — not a stub); what holds the leg back is its one binding
descriptor sitting at `state: draft`.

### 2.9 `provenance/` — derived-from + receipts + budget

`models.py` / `kinds.py` the `OutputKind` enum (12 members) + per-kind payloads
(finding / situation / hypothesis / prediction / alert / meta_finding / critique
/ **fact** (`FactPayload`) / **nexus** (`NexusPayload`) / prompt_module_candidate
/ **journal** (`JournalPayload`) / **scorecard** (`ScorecardPayload`)),
`_core.py` + `writes.py` the provenance writers (full `derived_from` chains; one
`write_*` per kind — incl. `write_fact` / `write_nexus` + `supersede_prior_facts`
/ `supersede_prior_nexuses`, both threading `source_type` / `seed_batch_id` so
curated-seed rows are stamped and superseded apart from agent-authored ones — see
§0a), `receipts.py` per-analyst SHA-256 hash-chained receipt chain (each node
carries a `receipt_hash` + a `chain_consistent` boolean — the badge is
"chain-consistent (single-node)", NOT a cryptographic signature; Ed25519 signing
lives only on the descriptor audit log), `verify.py` BOTH the provenance/lineage
sanity checks (`verify_provenance_complete` / `validate_lineage`) AND the P0-T2
**faithfulness verify** (`verify_finding_faithfulness` — deterministic citation
floor always-on + an optional flag-gated LLM judge (route-resolved; cross-family on the reference deployment), degrading to a labelled
floor, never a fabricated score), `checkpointer.py` durable checkpoints,
`budget.py` budget accounting, `dlq.py` provenance dead-letter.

The **judge subsystem** is the named extraction seam `verify.py` is being split
along (its module-size ceiling is DO-NOT-RAISE; the way under it is a brick, not
a bigger number). Four so far: `judge_evidence.py` renders the evidence view the
judge grades against; `judge_assessability.py` owns claim SHAPE (the corrected
labeled-scaffold rule, the JSON tripwire) and SCORE STATE (`unassessable` vs
`scored`, the PROVISIONAL ceiling for a non-`llm` verdict, and the critique-payload
contract that publishes them); `judge_input_checks.py` grades a composition
against what it was SHOWN rather than what it cited — a buried salience lead (R3)
and a detected input contradiction the body never surfaced (R2), both counted soft
failures; and `judge_quote_rules.py` owns the hard-fail quote contract — the
withdraw-only guard family (word-numeral/digit/unit/percent confirmation
fingerprints, endpoint-aware ranges, diverging prose direction, carve-out
clauses, one-authority claim routing) that retracts a contradiction whose quote
actually confirms the claim. The contradiction detection itself is `analysts/claim_contradiction.py`.

> **The `journal` kind is the one OFF-chain exception** (`kinds.py`,
> `OutputKind.JOURNAL` + its `KIND_REGISTRY` spec).
> Unlike every other kind, `OutputKind.JOURNAL` routes to its own dedicated
> `journal_entries` table (migration 0048), NOT `analyst_outputs`, and a journal
> row is a *perspective OVER* the provenance chain rather than a *member of* it:
> it carries an ALWAYS-EMPTY `derived_from` and the table is deliberately ABSENT
> from the lineage catalog (`registry/lineage_api.py` `_SUBSTRATE_TABLES`), so a
> downstream `derived_from` walk from a fact/situation/nexus can NEVER surface a
> journal node. Citations live ONLY in the row's `claims` / `cited_substrate_refs`
> (an UP-only walk). The journal never produces a fact/finding/nexus — when a doc
> enumerates the `signals → entities/facts → relations/nexuses → situations →
> assessments` lineage, the journal is explicitly the reflective layer ABOVE that
> chain, NOT a node in it. See §2.7 (`journal_assessor.py`).

### 2.10 `discovery/` — descriptor discovery pipeline

Turns external lists/queries into materialized descriptors:
`country_list_discovery.py`, `query_source_discovery.py`,
`file_sd_discovery.py`, `static.py` (discovery kinds); `materializer.py` /
`source_materializer.py` (emit descriptors), `autowire.py`, `relabel.py`,
`deps_resolver.py`, `disappearance.py`, `source_validate.py`,
`source_contract.py`, `registry.py`. (Several operator-facing discovery UIs are
future seams — see §7.)

### 2.11 `predicates/` — Starlark predicate DSL

The subscription / matching residual language. `compiler.py` compiles-once-on-
register into an LRU-cached `CompiledPredicate` (rejects statements — single
Starlark expression only); `evaluator.py` runs it in-sandbox; `helpers.py` the
helper catalog; `errors.py` compile/eval errors.

### 2.12 `stack/` — provider adapters resolved through the stack registry

`llm/` (`anthropic.py`, `vllm.py`, `openai.py`, `base.py`, `pricing.py`),
`embedding/`, `vector_store/qdrant.py`, `nats/jetstream.py`,
`nlp_service/client.py` (the model-service NER / translation client),
`postgres/age.py` (graph), `proxy/` (`bright_data.py`, `local_none.py`).

### 2.13 `jobs/`, `tools/`, `conversions/`

`jobs/` — async-job envelopes + store (`envelope.py`, `media.py`, `store.py`).
`tools/` — analyst-callable external tools (`mnemosyne_trust_query.py`).
`conversions/` — descriptor-version upgraders (`target_v2_to_v3.py`, …).

### 2.14 `seed/` — curated baseline seeding

Seeds the substrate with curated baseline facts/nexuses (the cold-start
knowledge floor). `_base.py` defines the `SeedSource` protocol + the
`SeedFact` / `SeedEntity` / `SeedNexus` payloads + `SeedContext`. `_driver.py`'s
`SeedDriver.run_seed_source` runs the pipeline: fetch → map → resolve entities →
write via `provenance.write_fact` / `write_nexus` (stamped `source_type='seed'`
+ a `seed_batch_id`) → record the batch in `seed_batches`. `__init__.py` is the
`ADAPTERS` registry (`get_adapter` / `list_adapters`), now wiring **four**
adapters under `adapters/`:

- `world_baseline.py` — the curated-YAML adapter (G20 leaders → facts,
  NATO/EU/BRICS/GCC alliances → signed nexuses) reading `seeds/world_baseline.yaml`
  — which you will NOT find in a clone: the seed MACHINERY ships, the curated
  seed DATA is gitignored by policy (`seeds/*.yaml`, minus `*.example.yaml`).
  Adapters degrade on the missing file rather than failing the run.
- `wikidata_leaders.py` — a Wikidata SPARQL leaders adapter (current heads of
  state/government → subject=leader `LeaderOf` facts + a supersession-correct
  **country-subject** `head of state` office fact keyed on the country, plus
  `member of` P463 → `MemberOf` signed +1 nexuses). `_resolve_bare_qid_labels`
  (post-SPARQL) does ONE batched `wbgetentities` Action-API call to resolve any
  bare-`Qxxxx` label the SPARQL label service left unlabelled, preferring
  `labels.en.value` and FALLING BACK to the `sitelinks.enwiki.title` (the
  fallback that resolves the en-label-less Trump `Q22686` → "Donald Trump"); an
  unresolvable QID is left bare and dropped (never emitted as a `Qxxxx` value).
- `acled_conflict.py` — an ACLED conflict backfill adapter.
- `sipri_arms_transfers.py` — a SIPRI arms-transfer adapter. **Registered but
  never actually seeded** — it is wired into `ADAPTERS` (so "deployed" means
  code-wired only), and no batch has ever run it. Check `seed_batches` for the
  live picture rather than trusting a row count written down here.

CLI: `scripts/seed.py` (`--list` / `--source` / `--dry-run`).

> **Honesty notes (updated — both prior gaps now closed).** `valid_until` is
> **threaded end-to-end now**: `FactPayload` / `NexusPayload`
> (`provenance/models.py`) carry a `valid_until` field and `_driver.py`
> passes it through, so an adapter's parsed term-end is persisted
> rather than dropped. (A seed row is still also superseded by a *differing* live
> observation; what remains absent is a background sweep that expires a row purely
> on its stored end date — but the current-facts read gate already excludes it
> once `valid_until` passes.) The `seed_batches` ledger is **now idempotent at
> the ledger level** too: `_driver.py` hashes the payload set
> (`_content_hash`) into the manifest and dedupes the batch row on
> `(source, kind, manifest->>'content_hash')`, so re-running a
> curated source UPDATEs the prior batch row instead of recording a duplicate —
> the ledger no longer overstates writes on re-run.

---

## 3. `src/legba/runtime/` — execution

The Dapr actor host and the four runtime planes. Entry point: `dapr_host.py`.

### 3.1 Actor host & actors

- `dapr_host.py` — the **`legba-runtime-dapr` process**. FastAPI app (default
  port 6090) that daprd routes `ActorProxy` invocations to; owns Dapr Actor
  wiring, substrate bring-up, and reconcile-loop attach.
- `dapr_actors.py` — the production actor classes `TargetActor` and
  `AnalystActor` (inherit `dapr.actor.Actor`, persist via the Postgres
  `legba-actor-state` component, use Dapr Reminders for cadence). Actor id
  scheme: `kind::descriptor_id::content_hash[:16]`. `TargetActor` is the
  passive subscriber identity the fan-out plane delivers to (it does not pull
  sources itself).
- `source_actor.py` — `SourceActor` + the directly-testable `SourceCore`. Owns
  **acquisition**: poll (Dapr Reminder) or push (webhook router) → run the
  per-source baseline → `write_canonical_signal` → publish to
  `legba.signals.<tenant>.<source>.<modality>.<event_class>` (the in-memory
  Signal is also stamped with the source tenant so the published envelope
  matches the row + subject + binding). One ingest per source regardless of
  consumer count. It also backfills `signals.source_credibility` at write time
  via a host lookup against the `source_credibility` table
  (`source_actor.lookup_source_credibility`) — the column was
  previously 100% NULL because the `source_credibility` pipeline filter only runs
  for descriptors that bind it, and the live descriptors don't.
- `dapr_cron.py` — cadence / cron helpers for actor scheduling.

### 3.2 Reconcile loop

- `reconcile.py` — informer (NATS `descriptor.>` events + periodic resync) →
  work queue → pure per-kind reconcilers `(observed, desired)`→`ReconcileAction`
  → executor (the only mutator). `nats_informer.py` is the event informer;
  `lifecycle.py` the lifecycle FSM; `state.py` the `ActorStateStore` /
  `ActorStateRecord`; `registry_client.py` reads desired state.

### 3.3 The four planes

`source_first_runtime.py` assembles the planes the host boots on top of the
substrate + reconcile loop:

1. **Acquisition** — `source_actor.py` + `sources/baseline.py` +
   `subscription/` (fan-out).
2. **Analysis** — `TargetActor` / `AnalystActor` subscribers + `triggers/`
   (coalescing) + `analysts/agency/` (action-pack agency + governor) +
   `budget.py`.
3. **Async jobs** — `jobs/` (NATS work-queue + competing-consumer workers).
4. **Substrate** — the `data/` adapters (§2.3).

`subscription/` (fan-out + subscription seam):
- `engine.py` `SubscriptionEngine` — resolves a target's `SourceRef`s, enforces
  `subscription_policy`, binds one per-target aggregated JetStream consumer.
- `sourceref.py` resolution, `policy.py` (open / allowlist / grant),
  `subjects.py` coarse-subject planning, `filter.py` exact match (SQL `WHERE` +
  Starlark residual), `backfill.py` replay.

`triggers/` (coalescing trigger plane):
- `engine.py` `TriggerEngine` over `coalescer.py` `Coalescer` — marks
  (analyst, target) pairs dirty and fires on cadence / accumulation threshold /
  severity gate (clamped by cooldown). `dispatch.py` dispatches the analyst run;
  `policy.py`, `state.py` (durable trigger state).

`jobs/` (runtime job plane):
- `queue.py` `JobQueue`, `worker.py` `JobWorkerPool` (competing consumers),
  `dispatch.py`, `process_media.py` handler, `media_client.py` (model-
  service media client). Derived signals from jobs re-enter the fan-out path.

### 3.4 Wiring & factories

`deps.py` / `analyst_deps_builder.py` build per-actor dependency bundles;
`source_factory.py` / `embedding_factory.py` / `qdrant_factory.py` /
`nlp_client_factory.py` / `receipt_chain_factory.py` construct ports;
`pipeline.py` the enrichment pipeline; `analyst_method.py` the analyst run
method; `budget.py` budget enforcement; `substrate_query_port.py` the
substrate read port; `audit_checkpointer_wiring.py` audit / checkpoint
wiring. (The orphaned `lineage.py` / `scheduling.py` modules were deleted by
C-3 — zero callers; lineage reads live in the registry `lineage_api`, cadence
in the Dapr reminder plumbing.)

`grounding.py` is the **Tier-1 knowledge-grounding** module (analysis-time
current-world-state injection — the stale-cutoff fix): `SubstrateGroundingResolver`
reads the CURRENT authoritative facts/nexuses (the temporal-honesty gate
`superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`,
preferring `source_type IN ('seed','curated')`, and excluding bare-QID values in
both SQL and a Python backstop); `collect_grounding_candidates` pulls candidate
names from the in-memory slice + the run's `target_id` (no DB);
`build_grounding_preamble` renders a dated "AUTHORITATIVE CURRENT CONTEXT (as of
`<today>`)" block. `analyst_deps_builder._build_grounding_hook` constructs the
per-run hook only when the descriptor sets `grounding.enabled: true` AND a
`pg_pool` is available (off otherwise). The `vector:world_context` source is now
**LIVE** (the L-114 wiring landed): it retrieves from the curated `world_context`
Qdrant corpus through the stack embedder port (bge-m3, 1024-dim) as a separate,
non-citable grounding preamble — opportunistic, relevance-floored, country-filtered,
degrade-not-drop when the corpus is empty — and is currently flipped ON for
`leadership_transition` + `internal_stability`. The `inline_target` runner's **GROUND** phase
(`data/analysts/inline_target.py`) prepends the preamble to the LLM user prompt
(degrade-not-drop). The opt-in schema field is `GroundingBlock`
(`data/schemas/analyst.py`, `AnalystDescriptor.grounding`); it is opted IN on all
nine bounded UNITS — `analyst_leadership_transition.yaml`,
`analyst_energy_security.yaml`, `analyst_escalation.yaml`,
`analyst_narrative_coordination.yaml`, `analyst_internal_stability.yaml`,
`analyst_military_posture.yaml`, `analyst_economic_coercion.yaml`,
`analyst_proliferation_watch.yaml`, `analyst_disruption_status.yaml` (and the
on-cadence `journal_assessor`). The
retired `analyst_country_assessor.yaml` also carries the block, but it no longer
runs.

### 3.5 `dapr_workflow/` — the optimizer GEPA loop

The optimizer analyst's multi-hour, durable GEPA evolutionary loop runs as a
**Dapr Workflow** on the daprd sidecar (the `legba-dapr-workflow-worker`
process — `worker.py:main`). `gepa.py` the GEPA algorithm itself + the
workflow-I/O dataclasses + the in-process fallback client (moved here from
the retired `runtime/temporal/` package by C-3 — that directory does not exist
at all any more; the older "now an empty leftover" note is stale), `workflow.py` the
deterministic workflow, `worker.py` the `WorkflowRuntime` registering it,
`client.py` the dispatch client. The optimizer analyst
(`analysts/optimizer.py`) dispatches into it; when no workflow runtime is
reachable it degrades to the in-process GEPA loop.

**Scope + honesty.** GEPA returns ONLY as the bounded `unit_optimizer` descriptor
(`descriptors/analyst_unit_optimizer.yaml`, `method.kind: dspy_compile`), scoped
to ONE measured unit (`leadership_transition`) with `fitness_metric=faithfulness`.
Every candidate carries a REAL before/after paired faithfulness delta measured on
the same faithfulness judge (whatever the judge route resolves; a live run read parent 0.34 → candidate 0.29, delta −0.05); it
stays `promotion_gate=human_gated` and can NEVER auto-promote on a degenerate,
absent, or non-positive delta. The old monolithic `country_optimizer` (and its
`india_energy_optimizer` sibling) stays cadence-frozen (descriptor still
`state=active`; no reminder-flood regression; SEAMS). LM access inside the worker is the custom
`LegbaProviderLM` dspy adapter (`dspy_lm.py`) that NEVER uses litellm; dspy lives
ONLY in this opt-in worker image, never in the runtime or analyst inference path.

---

## 4. UI — `legba-ui-v3/`

A Vite + React + TypeScript SPA (Tailwind, dockview panels). Build artifacts in
`dist/`; served by Caddy in compose (`legba-ui-build` builds the SPA,
`legba-caddy` serves it + reverse-proxies the registry API).

```
src/
  App.tsx, main.tsx        app shell + bootstrap
  auth/jwt.ts              JWT auth chain
  state/                   client state — selection.ts (unified record-selection store)
  lib/                     api.ts (registry client), ws.ts/useLiveTail.ts (live tail),
                           starter-descriptors.ts, and per-view models (graphModel,
                           findingsViews, alertModel, geoPoints, timelinePoints, …)
  panel-registry/          dynamic panel registry + loader (registry.ts, loader.ts, useRegistry.ts)
  components/              CommandPalette.tsx (record-jump palette), Sidebar.tsx (workspace
                           switcher + demoted menu), inspector/ (InspectorPanel.tsx +
                           RecordLink.tsx + useInspectorDetail.ts — the unified Inspector),
                           DescriptorBuilder/Editor, ScopePicker, StarterPicker,
                           StatusBar, PanelChrome
  panels/
    source/                SourceRegistry, SourceDetail, FanoutExplorer, SubscriptionBuilder
    target/                Overview, Signals, Findings, Situations, Hypotheses, Claims,
                           Graph, Map, Timeline, Sources
    analyst/               Runs, Critiques, Forecasts, CrossTarget, Outputs
    registry/              Targets, Analysts, Stack, Wirings, Mutations
    system/                Findings, Entities, EntityGraph, Lineage, Search, Pulse, Runtime,
                           Streams/StreamLag, Budget, Optimizer/OptimizerDiff, Eval/EvalScorecard,
                           Consult, AuditChain, DeadLetter, GovernorEvents, AlertCenter,
                           ActorHealth, ReportExport, TargetsRoster, TenantView, Users,
                           Journal (the reflective-voice feed — panel id `system.journal`,
                           renders entries with provenance chips deep-linking the cited
                           record + [needs_citation]/perspective spans; tsc-green + wired,
                           first in-browser render pending)
    merged/                the S7 consolidation targets — AlertsWatches,
                           Consult, Provenance, Timeline (each with its test).
                           This directory ABSORBED `dashboard/`, which is gone:
                           `dashboard/Dynamic.tsx` and its `dashboard.dynamic`
                           panel kind were deleted in S7-T2
    _DeferredStub.tsx      placeholder for not-yet-built panels (future-seam UIs)
```

**Panel tiers (present-but-hidden is a deliberate distinction).**
`panel-registry/registry.ts` `def()` returns a machine-readable `tier: 'live'`
by default; two sets then reclassify in one place:

- `PREVIEW_KINDS` flips `tier = 'preview'` (guarded-preview / honest-pending
  backend, e.g. `system.backfill`'s honest-501, `system.optimizer.diff`,
  client-only `system.search` / `alert_center` / `report_export` /
  `tenant_view`).
- `HIDDEN_KINDS` flips `hidden = true` so a panel stays in `PANEL_REGISTRY`
  (layouts referencing it by id still resolve) but drops out of the sidebar /
  singleton list. **These panels are present-but-hidden, NOT file-deleted** —
  the §6 redesign + #90 Wave A consolidation HID them (`system.pulse`,
  `system.eval`, `registry.discovery`, `system.targets.roster`, `v4.case`,
  `v4.assessment`, `system.runtime`, `system.tenant_view`, …), they were not
  removed from the tree. **The deleted set is larger than the one panel this
  note used to name** (re-verified 2026-08-02 against `registry.ts`): alongside
  `v4.feed`, the S7-T1 §6 DROP set — `system.pulse` / `system.eval` /
  `system.users` / `system.streams`, `registry.wirings` / `registry.mutations`,
  `dashboard.dynamic`, `registry.discovery`, `system.backfill` /
  `system.runtime` / `system.tenant_view`, `system.targets.roster` and
  `v4.case` — "was DELETED outright in S7-T2 — those kinds no longer exist".
  What remains genuinely present-but-hidden are LIVE panels merged into a peer:
  `system.optimizer.diff`, `source.subscription_builder`,
  `source.subscription_policy`, `source.fanout`.

**`system.findings` is the single unified "Live Feed"** (`SystemFindings`):
a merged findings + signals view (`/findings` + `/signals`, two NATS tails
`analyst.*.finding` + `legba.signals.>`, controls: Live on/off + Source
All/Findings/Signals + Cluster). It SUBSUMED the former `v4/world` live-feed
rail — the dedicated `LiveFeed.tsx` / `FeedPanel.tsx` components were removed
(not just hidden) and `v4.feed` deleted from the registry.

UI overview: `docs/UI.md`. Co-located
`*.test.ts(x)` files are the Vitest suite.

---

## 5. Entry points, infra, scripts

### Console scripts (`pyproject.toml [project.scripts]`)

| Script | Target | Role |
|---|---|---|
| `legba-registry` | `legba.data.registry.server:main` | registry HTTP/WS API (port 8090) |
| `legba-runtime-dapr` | `legba.runtime.dapr_host:main` | Dapr actor host (port 6090) |
| `legba-dapr-workflow-worker` | `legba.runtime.dapr_workflow.worker:main` | optimizer GEPA workflow worker |
| `legba-mcp` | `legba.ui.mcp_server:main` | MCP server surface |

Migrations are applied via `python -m legba.data.migrate` (see `migrate.py`).

### Docker / Dapr

- `docker-compose.yml` — substrate (redis, postgres, qdrant, nats), Dapr (`dapr-placement`, `dapr-scheduler`,
  `dapr-sidecar`), the Legba services (`legba-registry`, `legba-runtime-dapr`,
  `legba-dapr-workflow-worker`, `legba-mcp`), `legba-ui-build`, `legba-caddy`.
- `docker/` — `Dockerfile.registry`, `Dockerfile.runtime`, `Dockerfile.mcp`,
  `Caddyfile`.
- `dapr/components/` — `statestore.yaml` (Postgres actor state), `pubsub.yaml`,
  `secretstore.yaml`, `configuration.yaml`.

### `descriptors/` — example / seed YAML

Sources (`source_bbc_world.yaml`, `source_dw_world.yaml`,
`source_aljazeera_world.yaml`), targets (G20 `target_country_g20.yaml` +
per-country news targets), analysts — the
**analysis spine** descriptors first: the nine units
(`analyst_leadership_transition.yaml`, `analyst_energy_security.yaml`,
`analyst_escalation.yaml`, `analyst_narrative_coordination.yaml`,
`analyst_internal_stability.yaml`, `analyst_military_posture.yaml`,
`analyst_economic_coercion.yaml`, `analyst_proliferation_watch.yaml`), the composition tower
(`analyst_country_composition.yaml`, `analyst_region_composition.yaml`,
`analyst_world_assessor.yaml`, the thematic `analyst_escalation_composition.yaml`),
the deterministic I&W pair (`analyst_indicator_tracker.yaml`,
`analyst_collection_gap.yaml`),
the scorecard + eval (`analyst_scorecard_producer.yaml`,
`analyst_unit_correctness_scorer.yaml`, `analyst_forecast_scoreboard.yaml`), and
the measured optimizer (`analyst_unit_optimizer.yaml`); plus the supporting set
(`analyst_country_critic.yaml`, `analyst_entity_resolution.yaml`,
`analyst_cross_source_dedup.yaml`, the data-analysis-arc
`analyst_relationship_reifier.yaml`, `analyst_competing_hypotheses.yaml`,
`analyst_structural_balance.yaml`, `analyst_graph_mining.yaml`,
`analyst_nexus_decay.yaml`, `analyst_calibration_tracking.yaml`,
`analyst_fact_decay.yaml`, `consult` variants). The RETIRED/STOPPED
`analyst_country_assessor.yaml` + forecast `predictor` descriptors and the
cadence-frozen `country_optimizer` are kept in-tree but excluded from (or frozen
out of) the live set — historical `country_assessor` findings (~1.2k) and
`prediction` rows (~539) still REMAIN in the DB (see the bring-up note below). Action-packs (`action_pack_*.yaml`), discovery
(`discovery_geopolitical_g20.yaml`, `discovery_geopolitical_countries.yaml`),
templates (`template_country*.yaml`).

### `scripts/` — bring-up & ops

**Two transports, two shared helpers — pick by which one the script needs.**
`_p17_registrar.py` is the DIRECT-DB path: it wraps `DescriptorRegistry` so a
bring-up is deterministic about which database it populates, and it sets a
process-global `LEGBA_DATA_PG_DB` default at import time. `_bringup_http.py` is
the REST path against a running registry server — `registry_base` /
`registry_client` / `load_yaml` / `exists_head` / `register_create_only`, the
last being the create-only loop that 25 scripts had each carried a byte-identical
copy of until 2026-08-02. Importing the DB helper into a REST-only tool would
hand it a database selection it never asked for, so a script uses one or the
other, never both.

`bringup_register_*.py` register descriptors into a running registry (stack,
sources, analysts, action-packs, G20 country targets + the 13-desk `watch` tier
(`bringup_register_watch_country_targets.py` — ids
`country_watch_il/ir/ua/tw/kp/pk/sd/ml/bf/ne/cd/mm/ht`)
+ discovery, entity resolution, …); `bringup_register_analysts.py` registers the
live analyst set —
the analysis-spine descriptors (the nine units, the composition tower —
`country_composition` / `region_composition` / `world_assessor` / thematic
`escalation_composition` — the I&W pair `indicator_tracker` / `collection_gap`,
`scorecard_producer`, `unit_correctness_scorer`, `forecast_scoreboard`,
`unit_optimizer`) plus the data-analysis-arc handlers. Deliberately EXCLUDED from
the live set (commented out, files kept): `country_assessor` (RETIRED/STOPPED —
the units + composition supersede it; ~1.2k historical findings remain in the DB),
`country_predictor` (excluded + STOPPED — a forecast is a claim that must be
scored first; ~539 historical prediction rows remain), and `hypothesis_lifecycle`
(superseded by `competing_hypotheses`). This script also fails LOUD at register
time on a unit that declares an `eval.rubric`/`method.llm.verify` drift (the P2-T7
unit drift guard). **`bringup_register_source_catalog.py`
is the main source-registration path** — a 46-entry `CatalogEntry` tuple
(43 `rss` + 3 `geojson`) registered *directly* into `source_descriptors` (owner
`s1_catalog`): NWS (`source.nws.active_alerts`), NASA EONET, USGS quakes,
WHO/CDC/HRW, ~43 RSS feeds, etc. So the live source set is the
**`source_descriptors` DB rows**, NOT just the operator-pinned
`descriptors/source_*.yaml` — `ls descriptors/` undercounts. The full
catalog table (with the three-tier 3 / 46 / 49 scope model) lives in
`docs/DATA_SOURCES.md`. (A `CatalogEntry`'s
`enrich_text` flag selects the geojson enrichment chain: `[language_detect,
ner_multilingual, geocode]` when true vs geocode-only when false.)
`bringup_source_first_host.py`
boots the source-first host; `bringup_vault_load.py` loads vault secrets.
`seed.py` is the **curated-baseline seeding CLI** (`--list` / `--source` /
`--dry-run`, drives `data/seed/`). Other seeders (`seed_data.py` — gitignored
data-carrying script, absent from a clone by policy;
`seed_sources.py`, `seed_predictor_signals.py`, `quick_start.py`), backfills
(`backfill/`, `backfill_entity_graph.py`), trigger drivers
(`trigger_multi_country_runs.py`), smoke (`spike_smoke.py`, `run_tests*.sh`),
purges (`purge_proposed_situations.py`).

---

## 6. Where to add a thing

| Goal | Add here | Then |
|---|---|---|
| **A new source kind** | a handler module in `data/sources/` implementing the `_contract.py` Protocol | register the kind; write a `SourceDescriptor` (see `descriptors/source_*.yaml`) |
| **A new analyst kind** | a module in `data/analysts/` (or a handler in `analysts/deterministic_handlers/` for deterministic); add the value to `AnalystKind` (or register via vocabulary) | wire the run path in `runtime/analyst_method.py` / `analyst_deps_builder.py` |
| **A new filter / enricher** | a handler in `data/filters/` implementing `_contract.py` `StreamHandler` | reference it from a source's `pipeline` |
| **A new output sink** | a handler in `data/outputs/` implementing `_contract.py` `AlertEmitter` (+ a sub-sink in `alert_sinks/` if it's an alert surface) | bind it via a target/analyst `OutputBinding` |
| **A migration** | the next-numbered `data/migrations/NNNN_*.sql` | apply with `python -m legba.data.migrate` |
| **A predicate helper** | `data/predicates/helpers.py` | usable in source-selector / subscription Starlark |
| **A stack/provider adapter** | a module under `data/stack/<family>/` | register a `StackComponentDescriptor` |
| **A discovery kind** | a module in `data/discovery/` + a materializer | write a `discovery_*.yaml` |
| **A curated seed source** | an adapter in `data/seed/adapters/` implementing `_base.py` `SeedSource` + a dataset under `seeds/` | register it in `data/seed/__init__.py` `ADAPTERS`; run `scripts/seed.py --source <name>` |
| **A UI panel** | a `.tsx` under `legba-ui-v3/src/panels/<area>/` | register it in `panel-registry/` |

---

## 7. Future seams (present in the tree, not yet live)

These exist as stubs / thin handlers or pending wirings; don't mistake them for
live features. The full declared-seam list is `docs/SEAMS.md`.

- **Media extraction models** — the `process_media` job plane is real
  end-to-end (lands the derived signal, re-publishes it into fan-out with
  inherited geo/tags); with no `LEGBA_MEDIA_API_URL` configured the path
  refuses loud (`runtime/jobs/media_client.py` raises) — the extraction
  *service* is the seam, not the plumbing.
- ~~Analyst-side agency invocation~~ — **CLOSED (A-3)**: the agency hard-gate
  (`data/analysts/agency/agency.py` `Agency.run_pack_tool`) is invoked in
  production by two built-in paths — consult routes its ReAct tool calls
  through the governed `substrate_read` pack
  (`data/analysts/agency/substrate_read.py`), and the actor run path fires the
  `escalate_finding` pack when a finding crosses the gate (the production
  on-ramp is `data/analysts/agency/binding.py`). Live-proven at the 2026-06-10
  cutover (escalate invocations + governor allow events + channel emit).
- **Backend routes behind shipped panels** — these are now **LIVE**:
  `GET /registry/governor_events` (`data/registry/api.py`, queries the
  `governor_events` table), `GET /v3/eval/scorecard` (`v3_api.py`),
  `GET /api/v1/v3/eval/analyst_runtime` (`v3_api.py` — per-analyst run timing:
  count / avg + max wall-clock seconds / last run / non-success, derived from
  `analyst_traces`), and the
  optimizer prompt-diff `GET /v3/optimizer/candidates/{id}/diff` (`v3_api.py`,
  built entirely from substrate, no dspy import). The one deliberate exception is
  `POST /registry/targets/{id}/backfill` — an **honest 501** in `api.py`
  (refuses loud, never a silent 404; the `Backfiller` backend
  exists in `runtime/subscription/backfill.py` but the registry-plane trigger is
  intentionally not exposed).
- **Non-text UI renderers** — the modality→renderer registry is keyed, but the
  MapLibre renderer for `structured`/geo+json and the audio/video/image players
  are badged placeholders.

**No longer a seam — situation clustering is LIVE.** `finding_supersession`
stamps a `situation_signature` on clustered findings; `situation_clustering.py`
(`deterministic_handlers/`) reads those, groups by signature, and UPSERTs one
`situations` row per cluster (registered + `active`, its own bringup script
`scripts/bringup_register_situation_clustering.py` — not folded into
`bringup_register_analysts.py`). It was verified against live rows when it
landed; row counts are not restated here, because a number in this file cannot
be checked from the tree and goes quietly wrong (query `situations` /
`analyst_traces` if you need the current figures).

Live, in case other notes suggest otherwise: reactive LLM-analyst
trigger dispatch (`runtime/triggers/` fires LLM analysts on the coalescing
accumulation/severity gates, not only on cadence); source-side dedup tiers 1–2
(`data/filters/ingest_dedupe.py`, applied by `runtime/source_actor.py`
`SourceCore` from the descriptor's `pipeline.ingestion_filters`); the
subscription-policy-locking / discovery-pipeline / action-pack-grant /
backfill-replay operator panels (real `legba-ui-v3` panels, no longer
`_DeferredStub`); and the per-pack governor caps live-enforced over
`action_pack_invocations` at the agency entry point.

---

See also: `ARCHITECTURE.md`, `DESIGN.md`, `ACQUISITION.md`, `ANALYSIS.md`,
`AI_MODELS.md`, `RUNBOOK.md`, `UI.md`.
