# CODE_MAP

A navigational map of the Legba codebase: where each concern lives, how to
add things, and the key entry points. Legba is a **source-first** analysis &
knowledge-fusion platform — automated coalescing, operator-managed discovery & promotion;
declarative descriptors (sources / targets / analysts)
over a shared substrate, turned into running Dapr virtual actors that read and
write that substrate.

For *what* the system does and *why*, read `ARCHITECTURE.md` (the source-first
model) and `ACQUISITION.md`. For running it, read `RUNBOOK.md`. This file is
purely "where is the code".

The authoritative declared-seam list is `docs/SEAMS.md`. Where a module is
**built-but-unwired** (real code, no live caller / no live descriptor) or has a
known **code↔schema drift**, this map says so inline — don't infer "done" from
"present".

---

## 0. Package responsibility index

The fastest way to find a thing. One row per major package: its single
responsibility, the file you start from, and what lives there. **Built-but-
unwired** and **absent** markers are load-bearing — they distinguish "the code
exists" from "the live system uses it".

### `src/legba/data/` — declarative model + substrate

| Package | Responsibility (one line) | Start file | What lives here |
|---|---|---|---|
| `data/schemas/` | The descriptor types (strict, content-hashed pydantic) | `source.py` | `SourceDescriptor`/`Subscription`/`SourceRef` (`source.py`), `TargetDescriptor`+polymorphic `TargetScope` (`target.py`), `AnalystDescriptor`+open `AnalystKind`+optional `GroundingBlock` (`analyst.py`), `ActionPack`/`PackGovernor` (`action_pack.py`), `StackComponentDescriptor` (`stack.py`), lifecycle FSM (`lifecycle.py`), shared property types (`properties.py`), vocabulary shapes (`vocabulary.py`), version/content-hash helpers (`versioning.py`) |
| `data/registry/` | Control plane: descriptor registry, vault, HTTP/WS API | `server.py` | content-hashed instance registry + Ed25519 audit + DLQ + NATS events (`descriptor.py`, `audit.py`, `signing.py`, `dlq.py`, `events.py`/`streams.py`/`emitter.py`), XSalsa20-Poly1305 credential vault (`credentials.py`), stack registry (`stack.py`), the `legba-registry` FastAPI app + routers (`server.py` + `api.py`/`v3_api.py`/`substrate_reads_api.py`/`lineage_api.py`/`entities_api.py`/`runtime_telemetry_api.py`/`budget_api.py`/`source_credibility_api.py`/`consult_api.py`), discovery/version conversion (`discovered_materializer.py`, `conversion.py`) |
| `data/sources/` | Source-kind acquisition handler library | `_contract.py` | the handler Protocol + `Signal` (`_contract.py`/`_protocols.py`), the **per-source baseline pipeline** (`baseline.py`), 14 kind-handler modules (`rss.py`, `gdelt.py`, `acled.py`, `mediacloud.py`, `opensanctions.py`, `common_crawl.py`, `intelmq.py`, `firecrawl.py`, `scraper.py`, `telegram.py`, `discord.py`, `geojson.py`, `json_api.py`, `generic_webhook.py`+`webhook_router.py`), outbound provisioning (`provision.py`), egress helper (`_egress.py`) |
| `data/filters/` | In-flight enrichment / transform handlers over a `Signal` | `_contract.py` | `StreamHandler` Protocol (`_contract.py`), baseline enrichers (`language_detect.py`, `geocode.py`, `ner.py`, `classify.py`, `source_credibility.py`), ingest dedup tiers 1–2 (`ingest_dedupe.py`, `dedupe.py`), SLM-backed refiners that call the model service (`slm_classification_refine.py`, `slm_entity_resolve.py`, `slm_relationship_validate.py`) |
| `data/analysts/` | Analyst-kind implementations (one module per kind) | `__init__.py` | kind modules discovered via `discover_analyst_kinds()` (`__init__.py`): `inline_target.py`, `cross_target_raw.py`, `meta_findings_synthesizer.py`, `cross_analyst_correlator.py`, `relationship_reifier.py` (META — co-mention pairs → signed typed nexuses, 8B LLM), `competing_hypotheses.py` (META ACH — evidence×diagnosticity matrix is LLM-scored + ±2 transitions; outcome-resolution + calibration now FIRE against the EXOGENOUS `resolved_outcome` column, migration 0038 — subsequent-facts/operator outcome, self-consistency-flagged when only status-transition), `deterministic.py`, `predictor.py`, `critic.py`, `optimizer.py` (GEPA — see §3.5), `consult_on_demand.py`; deterministic impls in `deterministic_handlers/` (`entity_resolution.py` (+ `_entity_canon.py`), `cross_source_dedup.py` (BOUNDED per-run scan — skips already-canonicalised content-hash groups in the DB + caps at `max_groups_per_run`=500), `cross_source_coalesce.py` (substrate-wide cross-source semantic/temporal LINKER, off-by-default — SEAM #19), `finding_supersession.py`, `situation_clustering.py`, `thematic_proposal.py` (Phase-5 — detects thematic non-geo situation frames + PROPOSES them), `hypothesis_lifecycle.py`, `graph_mining.py`, `proposed_edge_governance.py` (Phase D — promotes pending `proposed_edges` into neutral `CoOccursWith` nexuses), `_graph_metrics_sink.py`, `anomaly_detection.py`, `fact_decay.py`, `calibration_tracking.py`, `integrity_sweep.py`, `entity_gc.py`, `adversarial_signals.py`, `structural_balance.py`, `nexus_decay.py`); action-pack agency plane in `agency/` (`agency.py` hard gate, `governor.py`, `resolution.py`, `binding.py`, `substrate_read.py`, `tools.py`, `events.py`) |
| `data/provenance/` | Output-kind payloads, write helpers, receipts, budget, DLQ | `kinds.py` | the 10-member `OutputKind` enum + `KIND_REGISTRY` (`kinds.py`, now incl. `FACT` + `NEXUS`), per-kind pydantic payloads (`models.py`, incl. `FactPayload`/`NexusPayload`), the analyst-output writers (`writes.py`, `_core.py` — incl. `write_fact`/`write_nexus` + `supersede_prior_facts`/`supersede_prior_nexuses`, `source_type`/`seed_batch_id` threading), receipt-chain hashing + verify (`receipts.py`/`_core.py`, `verify.py`), durable checkpointer (`checkpointer.py`), budget accounting (`budget.py`), output DLQ (`dlq.py`) |
| `data/outputs/` | Output-kind emit handlers (analyst payloads → operator surfaces) | `_contract.py` | the `AlertEmitter`/emit Protocol + `discover_output_kinds()` (`_contract.py`/`__init__.py`), `substrate.py` (typed write-back facade), `nats_stream.py`, `webhook.py`, `alert.py`, `ui_panel.py`, `mcp_tool.py`, `a2a_skill.py`, `stix_bundle.py` (STIX 2.1) |
| substrate adapters (`data/` root) | One typed port per backing store + bootstrap | `config.py` | `postgres.py` (asyncpg + AGE codec), `nats.py` (JetStream, signal subject grammar), `qdrant.py`, `redis.py`, env-driven config (`config.py`), migration runner (`migrate.py`), vocabulary seed/query (`vocabulary.py`), substrate smoke check (`smoke.py`, owns `RETIRED_TABLES`) |
| `data/migrations/` | SQL schema (applied in order) | `0001_baseline.sql` | **Flattened baseline + forward chain.** `0001_baseline.sql` (commit `06bab95`) collapsed the former 30-step chain into one file — extensions + AGE graph (9 vertex / 14 edge labels) + all 40 relational tables + seed data (incl. the former-0031 source-credibility `tier`/`state_affiliation` columns + seeded credibility rows). The data-analysis arc then re-opened the forward chain, now `0032`…`0046` (head = **0046**): `0032_facts_decay_columns.sql` (facts `valid_until`/`superseded_by`/`confidence_components`), `0033_nexuses.sql` (reified `nexuses` table), `0034_seed_batches.sql` (curated-seed batch ledger), `0035_entity_profiles_composite_key.sql`, `0036_signals_retention.sql`, `0037_age_output_label.sql`, `0038_hypotheses_resolved_outcome.sql` (the EXOGENOUS ACH outcome column), `0039_consult_sessions.sql`, then the DQ-sweep tail `0040`…`0046`: situations first-class + temporal repair (`0040`/`0041_situations_valid_from_repair.sql`/`0042_situations_target_id_backfill.sql`), `0043_ingestion_conf1_backfill.sql` + `0044_purge_ingestion_leader_junk.sql` (conf-1.0 sentinel cleanup), `0045_backfill_demonym_nexuses.sql` (NER demonym→country), `0046_source_poll_outcomes.sql` (the `source_poll_outcomes` non-productive-poll provenance table). There is no `0014`. The runner (`migrate.py`) globs `*.sql` in order |
| `data/predicates/` | Starlark predicate DSL (subscription / matching residual) | `compiler.py` | compile-once-on-register → LRU `CompiledPredicate` (`compiler.py`), in-sandbox evaluator (`evaluator.py`), per-surface helper catalog (`helpers.py`), compile/eval errors (`errors.py`) |
| `data/stack/` | Provider adapters resolved through the stack registry | (per family) | `llm/` (`anthropic.py`, `vllm.py`, `openai.py`, `base.py`, `pricing.py`), `embedding/`, `vector_store/qdrant.py`, `nats/jetstream.py`, `nlp_service/client.py`, `postgres/age.py`, `proxy/` |
| `data/discovery/` | Descriptor discovery pipeline (external lists/queries → descriptors) | `registry.py` | discovery kinds (`country_list_discovery.py`, `query_source_discovery.py`, `file_sd_discovery.py`, `static.py`), materializers (`materializer.py`, `source_materializer.py`), `autowire.py`, `relabel.py`, `deps_resolver.py`, `disappearance.py`, `source_validate.py` |
| `data/seed/` | Curated baseline seeding (datasets → stamped facts/nexuses) | `_base.py` | `SeedSource` protocol + `SeedFact`/`SeedEntity`/`SeedNexus` payloads + `SeedContext` (`_base.py`), `SeedDriver` (`run_seed_source`: fetch→map→resolve entities→`write_fact`/`write_nexus` stamped `source_type='seed'`+`seed_batch_id`→record `seed_batches`) (`_driver.py`), `ADAPTERS` registry `get_adapter`/`list_adapters` (`__init__.py`, wiring **four** adapters), the `adapters/` adapter set (`world_baseline.py` curated-YAML leaders→facts + alliances→signed nexuses + a country-subject `head of state` office fact; `wikidata_leaders.py` SPARQL current heads of state/government → `LeaderOf` + country-subject `head of state` facts + `MemberOf` signed nexuses, with `wbgetentities` bare-QID label resolution (enwiki-sitelink fallback — resolves `Q22686`→"Donald Trump"); `acled_conflict.py` conflict backfill; `sipri_arms_transfers.py` arms-transfer — REGISTERED but **never seeded**, 0 rows). Datasets in `seeds/` |
| `data/jobs/`, `data/tools/`, `data/conversions/` | Job envelopes / analyst-callable tools / version upgraders | — | `jobs/` (`envelope.py`, `media.py`, `store.py`), `tools/` (`mnemosyne_trust_query.py`), `conversions/` (`target_v2_to_v3.py`, …) |

### `src/legba/runtime/` — execution

| Package | Responsibility (one line) | Start file | What lives here |
|---|---|---|---|
| `runtime/` (host + actors) | Turn descriptors into running Dapr actors | `dapr_host.py` | the `legba-runtime-dapr` FastAPI host + plane bring-up + deps resolvers (`dapr_host.py`), production actor classes `TargetActor`/`AnalystActor` (`dapr_actors.py`), `SourceActor`+`SourceCore` acquisition (`source_actor.py`), cadence/cron helpers (`dapr_cron.py`), the four-plane assembler (`source_first_runtime.py`) |
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
| `panels/` | The legacy/workbench panel set (now demoted) | (per area) | `source/`, `target/`, `analyst/`, `registry/`, `system/` panel areas + `dashboard/Dynamic.tsx`; `_DeferredStub.tsx` is the not-yet-built placeholder |
| `v4/` | The "Three Rooms" v4 shell (current front door) | `RoomStub.tsx` | `world/` (Leaflet world map + KPIs + time scrubber + live feed), `flow/` (NiFi-style canvas-as-view-over-registry with live telemetry + wiring modal), `why/` (provenance trail + lineage/entity graphs + world-assessment), `case/` (case board/rail), shared `components/` |

> **Quick "where do I add X" → §6.**

---

## 0a. Corrections / honesty notes (read before trusting older docs)

These override anything in older docs or comments that implies otherwise. Each
is traceable to code:

- **`OutputKind.FACT` and `write_fact` NOW EXIST** (the data-analysis arc — this
  reverses the older "no FACT / no write_fact" note). `OutputKind` now has **10**
  members — the original seven (`FINDING`, `SITUATION`, `HYPOTHESIS`,
  `PREDICTION`, `ALERT`, `META_FINDING`, `CRITIQUE`) plus `FACT`, `NEXUS`, and
  `PROMPT_MODULE_CANDIDATE` (`data/provenance/kinds.py:69-87`). `provenance/writes.py`
  now exposes `write_fact` (`:385`) and `write_nexus` (`:416`) plus
  `supersede_prior_facts` (`:658`) / `supersede_prior_nexuses` (`:822`); both
  write helpers thread `source_type` / `seed_batch_id` (`:128-129`/`:460-461`) so
  curated-seed rows are stamped and selectively superseded apart from agent-
  authored ones. Facts are now created through the output subsystem (by
  `fact_extractor` enrichment + the seed driver), not only `UPDATE`d by
  `fact_decay`.
- **`facts` decay columns NOW EXIST** (`migrations/0032_facts_decay_columns.sql`
  added `valid_until` / `superseded_by` / `confidence_components`), reversing the
  older code↔schema-drift note — `fact_decay`'s temporal-expiry and
  confidence-decay branches run against real columns now.
- **`meta_findings_synthesizer` / `cross_analyst_correlator` are now REGISTERED**
  (this reverses the older "built-but-unregistered" note): the bring-up set
  instantiates them as `analyst_meta_synthesizer.yaml` /
  `analyst_cross_correlator.yaml` (`scripts/bringup_register_analysts.py:58-59`),
  alongside `relationship_reifier`, `competing_hypotheses`, and the
  `structural_balance` / `graph_mining` / `nexus_decay` / `calibration_tracking` /
  `fact_decay` deterministic handlers. `cross_target_raw` remains
  built-but-unregistered (the kind module exists and the deps-builder dispatches
  it, but no live descriptor instantiates it). Present + dispatchable ≠ running.
- **`nexuses` table is BACK (reified).** The earlier "RETIRED" note is reversed:
  `0033_nexuses.sql` re-creates a `nexuses` table for the reified signed/typed
  relationship edges produced by `relationship_reifier` and consumed by
  `structural_balance` / `graph_mining` / `nexus_decay`. (`smoke.py`'s
  `RETIRED_TABLES` no longer asserts its absence.) Ignore older "nexuses is
  retired / AGE-edges-only" references.
- **STIX emit is wired** (recently — commit `cb621b8`). The analyst run path
  dispatches emit-capable output kinds via `_emit_output_bindings`
  (`dapr_actors.py:2142`/`:2419`), discovered through `discover_output_kinds()`
  (`:2401`); `stix_bundle.emit` produces a STIX 2.1 bundle. (TAXII *upload*
  remains a documented stub — see `stix_bundle.py`.) Older "BUILT-BUT-UNWIRED"
  notes on STIX predate this wiring.
- **Tier-1 knowledge grounding EXISTS and is LIVE** (the stale-cutoff fix). A new
  `runtime/grounding.py` (`SubstrateGroundingResolver` + `build_grounding_preamble`),
  an opt-in `GroundingBlock` descriptor field (`data/schemas/analyst.py:522`,
  default off), the `inline_target` **GROUND** phase (`:592-612`), and the
  `analyst_deps_builder._build_grounding_hook` gate inject a dated "AUTHORITATIVE
  CURRENT CONTEXT" preamble — built from the CURRENT seed/curated substrate facts
  — into the `world_assessor` / `country_assessor` LLM prompt. The `valid_until`
  field now exists on `FactPayload`/`NexusPayload`, so the seed/supersession path
  is temporally honest end-to-end (reverses the older "valid_until dropped" note
  in §2.14). The ACH calibration leg also now FIRES against the exogenous
  `resolved_outcome` column (migration 0038) — reverses the older "NOT yet
  firing" note in §0/§2.7.

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
via the vocabulary registry.

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
| `runtime_telemetry_api.py` | `/api/v1` | actor health / runtime telemetry |
| `budget_api.py` | `/api/v1/budget` | budget envelope reads |
| `source_credibility_api.py` | `/api/v1` | source-credibility reads |
| `consult_api.py` | `/api/v1` | on-demand consult (proxies the consult analyst actor via daprd) |

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
verbatim from the former `0004` (see the header comment at
`0001_baseline.sql:1-9`). So the single file now builds the extensions + the AGE
graph (9 vertex / 14 edge labels) + all 40 relational tables + seed data —
including everything the historical chain added: the source-first
signal/subscription tables (former `0024`), the coalescing trigger state (former
`0028`, `trigger_state` at `0001_baseline.sql:851`), the entity-graph tables
(former `0029`), and the source-credibility `tier` + `state_affiliation` columns
+ seed rows (former `0031`, `source_credibility` at `:759`).
**The forward chain re-opened after the baseline** for the data-analysis arc:
`0032_facts_decay_columns.sql` (facts `valid_until` / `superseded_by` /
`confidence_components` — reversing the former code↔schema drift),
`0033_nexuses.sql` (re-lands a first-class reified `nexuses` table; the former
`0030` drop is moot), `0034_seed_batches.sql` (the curated-seed batch ledger),
then `0035_entity_profiles_composite_key.sql`, `0036_signals_retention.sql`,
`0037_age_output_label.sql`, `0038_hypotheses_resolved_outcome.sql` (the
exogenous ACH `resolved_outcome` column that lets calibration grade against new
evidence rather than itself), and `0039_consult_sessions.sql`. The DQ-sweep then
extended the chain to **head `0046`**: `0040`/`0041_situations_valid_from_repair.sql`
/ `0042_situations_target_id_backfill.sql` (situations as first-class objects +
temporal repair), `0043_ingestion_conf1_backfill.sql` +
`0044_purge_ingestion_leader_junk.sql` (the conf-1.0-sentinel cleanup),
`0045_backfill_demonym_nexuses.sql` (NER demonym→country), and
`0046_source_poll_outcomes.sql` (the `source_poll_outcomes` non-productive-poll
provenance table). The chain has no `0014`.
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
`rss.py`, `gdelt.py`, `acled.py`, `mediacloud.py`, `opensanctions.py`,
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

One module per built-in analyst kind: `inline_target.py`, `cross_target_raw.py`,
`meta_findings_synthesizer.py`, `relationship_reifier.py` (META — types
co-mentioned entity pairs from `proposed_edges` into signed typed `nexuses`,
8B LLM, never litellm), `competing_hypotheses.py` (META ACH — competing
hypotheses + LLM-scored evidence×diagnosticity matrix + ±2 transitions; the
outcome-resolution/calibration leg now FIRES against the EXOGENOUS
`resolved_outcome` column (migration 0038) — the outcome is stamped against
subsequent facts or an operator label, and `calibration_tracking` flags the
Brier `self_consistency_only` when every resolved row came only from a
status-transition; see ANALYSIS §7.4-7.5),
`deterministic.py`, `predictor.py`, `critic.py`, `optimizer.py` (GEPA loop — see
§3.5), `cross_analyst_correlator.py`, `consult_on_demand.py`.
`deterministic_handlers/` holds the deterministic analyst impls (e.g.
`entity_resolution.py` (+ `_entity_canon.py` — `canonicalize_entity` surface-form
alias/gazetteer merge + NER type correction, Phase C), `cross_source_dedup.py`
(BOUNDED per-run scan — `cross_source_dedup.py:193-194` skips
already-canonicalised content-hash groups in the DB and `:90`/`:501` caps at
`max_groups_per_run`=500, so the backlog drains across cadences inside the
actor-invoke budget), `cross_source_coalesce.py`
(off-by-default substrate-wide cross-source semantic/temporal linker — SEAM #19),
`finding_supersession.py`, `situation_clustering.py`, `thematic_proposal.py`
(Phase-5 — detects thematic non-geo situation frames + PROPOSES them for
promotion), `hypothesis_lifecycle.py`, `structural_balance.py`,
`graph_mining.py`, `nexus_decay.py`, `calibration_tracking.py`, `fact_decay.py`,
`proposed_edge_governance.py` (Phase D — promote/reject the `proposed_edges` queue),
`_graph_metrics_sink.py` (Phase D — `write_graph_metric` helper),
`anomaly_detection.py`, …). `agency/` is the **action-pack
agency plane**: `agency.py` (`run_pack_tool` hard gate), `governor.py`
(per-pack governor + budget), `resolution.py` (capability resolution
`analyst.action_packs ∩ target.allowed_action_packs ∩ pack.applicability`),
`tools.py`, `substrate_read.py` (the consult kind's governed read tools),
`binding.py` (the per-analyst production on-ramp — `AgencyToolBinding` +
`EscalationBinding`), `events.py`.

### 2.8 `outputs/` — output-kind handler library

Analyst-emitted payloads fanned to operator surfaces. `_contract.py` is the
`AlertEmitter`/emit Protocol; `discover_output_kinds()` (`__init__.py`) is the
registry the actor run path dispatches against. Kinds: `substrate.py` (typed
write-back facade over `provenance/writes.py`), `nats_stream.py`, `webhook.py`,
`alert.py` + `alert_sinks/` (`matrix.py`, `nats.py`, `pushover.py`, `xmpp.py`),
`ui_panel.py`, `mcp_tool.py`, `a2a_skill.py`, `stix_bundle.py`. The
`stix_bundle.emit` STIX 2.1 path is **wired** — the run path calls
`_emit_output_bindings` (`runtime/dapr_actors.py:2142`/`:2419`) which discovers
emit-capable kinds and dispatches them; TAXII *upload* remains a documented stub.

### 2.9 `provenance/` — derived-from + receipts + budget

`models.py` / `kinds.py` the `OutputKind` enum (10 members) + per-kind payloads
(finding / situation / hypothesis / prediction / alert / meta_finding / critique
/ **fact** (`FactPayload`) / **nexus** (`NexusPayload`) / prompt_module_candidate),
`_core.py` + `writes.py` the provenance writers (full `derived_from` chains; one
`write_*` per kind — incl. `write_fact` / `write_nexus` + `supersede_prior_facts`
/ `supersede_prior_nexuses`, both threading `source_type` / `seed_batch_id` so
curated-seed rows are stamped and superseded apart from agent-authored ones — see
§0a), `receipts.py` per-analyst SHA-256 hash-chained receipt chain, `verify.py`
chain verification, `checkpointer.py` durable checkpoints, `budget.py` budget
accounting, `dlq.py` provenance dead-letter.

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
  NATO/EU/BRICS/GCC alliances → signed nexuses) reading `seeds/world_baseline.yaml`.
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
  code-wired only), but no batch has run it and it has 0 rows.

CLI: `scripts/seed.py` (`--list` / `--source` / `--dry-run`).

> **Honesty notes (updated — both prior gaps now closed).** `valid_until` is
> **threaded end-to-end now**: `FactPayload` / `NexusPayload`
> (`provenance/models.py:329`/`:371`) carry a `valid_until` field and `_driver.py`
> passes it (`:366-367`/`:409-410`), so an adapter's parsed term-end is persisted
> rather than dropped. (A seed row is still also superseded by a *differing* live
> observation; what remains absent is a background sweep that expires a row purely
> on its stored end date — but the current-facts read gate already excludes it
> once `valid_until` passes.) The `seed_batches` ledger is **now idempotent at
> the ledger level** too: `_driver.py` hashes the payload set (`_content_hash`,
> `:76`) into the manifest and dedupes the batch row on
> `(source, kind, manifest->>'content_hash')` (`:286-300`), so re-running a
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
  (`lookup_source_credibility`, `:340`, applied at `:406-408`) — the column was
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
`pg_pool` is available (off otherwise; `vector:world_context` sources no-op as a
declared Tier-2 follow-up). The `inline_target` runner's **GROUND** phase
(`data/analysts/inline_target.py:592-612`) prepends the preamble to the LLM user
prompt (degrade-not-drop). The opt-in schema field is `GroundingBlock`
(`data/schemas/analyst.py:522`, `AnalystDescriptor.grounding`); opted IN on
`descriptors/analyst_world_assessor.yaml` + `analyst_country_assessor.yaml`.

### 3.5 `dapr_workflow/` — the optimizer GEPA loop

The optimizer analyst's multi-hour, durable GEPA evolutionary loop runs as a
**Dapr Workflow** on the daprd sidecar (the `legba-dapr-workflow-worker`
process — `worker.py:main`). `gepa.py` the GEPA algorithm itself + the
workflow-I/O dataclasses + the in-process fallback client (moved here from
the retired `runtime/temporal/` package by C-3 — that dir is now an empty
leftover, no `.py` modules), `workflow.py` the
deterministic workflow, `worker.py` the `WorkflowRuntime` registering it,
`client.py` the dispatch client. The optimizer analyst
(`analysts/optimizer.py`) dispatches into it; when no workflow runtime is
reachable it degrades to the in-process GEPA loop.

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
                           ActorHealth, ReportExport, TargetsRoster, TenantView, Users
    dashboard/Dynamic.tsx  registration-driven dynamic dashboard
    _DeferredStub.tsx      placeholder for not-yet-built panels (future-seam UIs)
```

**Panel tiers (present-but-hidden is a load-bearing distinction).**
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
  removed from the tree. The one genuinely deleted panel is `v4.feed`.

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
per-country news targets, `target_india_energy_infra.yaml`), analysts
(`analyst_country_assessor.yaml`, `analyst_country_critic.yaml`,
`analyst_entity_resolution.yaml`, `analyst_cross_source_dedup.yaml`, the
data-analysis-arc set `analyst_relationship_reifier.yaml`,
`analyst_competing_hypotheses.yaml`, `analyst_structural_balance.yaml`,
`analyst_graph_mining.yaml`, `analyst_nexus_decay.yaml`,
`analyst_calibration_tracking.yaml`, `analyst_fact_decay.yaml`,
optimizer / predictor / consult variants), action-packs (`action_pack_*.yaml`),
discovery (`discovery_geopolitical_g20.yaml`,
`discovery_geopolitical_countries.yaml`), templates (`template_country*.yaml`).

### `scripts/` — bring-up & ops

`bringup_register_*.py` register descriptors into a running registry (stack,
sources, analysts, action-packs, G20 country targets + discovery, entity
resolution, …); `bringup_register_analysts.py` registers the live analyst set
(`hypothesis_lifecycle` is RETIRED from that set — the file is kept, but
`competing_hypotheses` supersedes it; the data-analysis-arc analysts +
`country_predictor` are registered here instead). **`bringup_register_source_catalog.py`
is the load-bearing source-registration path** — a 46-entry `CatalogEntry` tuple
(43 `rss` + 3 `geojson`) registered *directly* into `source_descriptors` (owner
`s1_catalog`): NWS (`source.nws.active_alerts`), NASA EONET, USGS quakes,
WHO/CDC/HRW, ~43 RSS feeds, etc. So the live source set is the
**`source_descriptors` DB rows**, NOT just the operator-pinned
`descriptors/source_*.yaml` — `ls descriptors/` undercounts. The full
catalog table (with the three-tier 3 / 46 / 49 scope model) lives in
`docs/SOURCES.md`. (A `CatalogEntry`'s
`enrich_text` flag selects the geojson enrichment chain: `[language_detect,
ner_multilingual, geocode]` when true vs geocode-only when false.)
`bringup_source_first_host.py`
boots the source-first host; `bringup_vault_load.py` loads vault secrets.
`seed.py` is the **curated-baseline seeding CLI** (`--list` / `--source` /
`--dry-run`, drives `data/seed/`). Other seeders (`seed_data.py`,
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

## 7. Future seams (present in the tree, not yet load-bearing)

These exist as stubs / thin handlers or pending wirings; don't mistake them for
live features. The authoritative declared-seam list is `docs/SEAMS.md`.

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
  `GET /registry/governor_events` (`data/registry/api.py:1898`, queries the
  `governor_events` table), `GET /v3/eval/scorecard` (`v3_api.py:685`), and the
  optimizer prompt-diff `GET /v3/optimizer/candidates/{id}/diff` (`v3_api.py:519`,
  built entirely from substrate, no dspy import). The one deliberate exception is
  `POST /registry/targets/{id}/backfill` — an **honest 501** (`api.py:1952`,
  status_code=501 — refuses loud, never a silent 404; the `Backfiller` backend
  exists in `runtime/subscription/backfill.py` but the registry-plane trigger is
  intentionally not exposed).
- **Non-text UI renderers** — the modality→renderer registry is keyed, but the
  MapLibre renderer for `structured`/geo+json and the audio/video/image players
  are badged placeholders.
- **Situation clustering at scale** — `finding_supersession` ships as a
  deterministic analyst that links near-dups; auto-clustering them into
  feed-level situations is not yet enforced.

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
