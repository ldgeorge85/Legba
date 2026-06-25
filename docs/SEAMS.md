# SEAMS — the declared-seam registry

This is THE single registry of intentionally not-built things in Legba
(no-stub dev rule, decision D3). The rule it enforces:

> A stub or mock in a production path (`src/**`) is forbidden. Anything not
> built must be **declared here** and must **fail loud / refuse activation**
> at its guard rail — it never fabricates output. A stubbed feature is not
> "done".

Mechanical enforcement: `tests/test_no_undeclared_stubs.py` scans
`src/legba/**` for stub markers (loud-fail `NotImplementedError` raises,
stub/fake/mock/placeholder/echo-named symbols, mock imports, TODO'd empty
returns) and fails unless every hit is either the abstract-method idiom or
listed in the [machine-readable allowlist](#machine-readable-allowlist)
below. There is **no per-line pragma escape** — registering the seam here,
with a why and a guard rail, is the only way through the gate.

Adding an entry means making two edits in this file:

1. a narrative row in the table below (what / why deferred / guard rail), and
2. a `src/legba/<path>.py:<dotted.symbol>` line in the allowlist block.

Removing the code must remove the entry — the test also fails on stale
allowlist lines.

---

## Declared seams

### 1. Eager media extraction (media plane)

| | |
|---|---|
| **What** | Real hosted media extraction (Whisper transcribe / VLM caption / OCR) behind `LEGBA_MEDIA_API_URL`, plus real eager-tier media extractors. The job/loop plumbing is BUILT (G3 close): `process_media` lands the derived signal, re-publishes it into fan-out (`event_class="derived"`), and inherits parent geo/tags/entity_classes/language. The former stand-ins (`EchoCaptionExtractor`, the `MediaClient` `_stub_extract` edge) were **removed outright** — a stub result is structurally unrepresentable (`MediaExtractionResult.source` is `Literal["hosted"]`). |
| **Why deferred** | The live media MODEL is not provisioned (no Whisper/VLM/OCR weights deployed; `LEGBA_MEDIA_API_URL` unset on the rig), and no real eager-tier media extractor exists in-tree. The **deploy path is built** (D2 PREP, locked decision): a deployable `legba-media/` service ships the exact runtime HTTP contract behind a `media` compose profile + env wiring, but it serves the seam (HTTP 503 on each extraction kind, never a fabricated result) until a real model backend is wired in `legba-media/app/main.py:load_backends`. So `LEGBA_MEDIA_API_URL` can be set to a deployed-but-unprovisioned service and the runtime STILL refuses loud (the service 503s → client `MediaEndpointUnreachable`). Holding the live endpoint is the stated seam. |
| **Guard rail** | All refuse-loud: `src/legba/runtime/jobs/media_client.py:MediaClient` (`extract` raises `MediaEndpointNotConfiguredError` when unconfigured; `MediaEndpointUnreachable` when down/5xx — worker retries); `src/legba/runtime/jobs/process_media.py:process_media_handler` (terminal failed when endpoint or subscription engine missing — a derived signal can never land without re-entering fan-out); `src/legba/runtime/source_actor.py:SourceCore` (`media: "eager"` descriptor refuses activation unless real media-modality extractors are injected); `src/legba/data/sources/baseline.py:_enrich_media_eager` (per-signal refusal); `src/legba/data/analysts/agency/tools.py:process_media_tool` (enqueue-side refusal). Server-side mirror: `legba-media/app/main.py` 503s on any extraction kind with no model backend loaded (outside `src/legba/**`, so not stub-scanner scope). |

### 2. `stream` acquisition kind (sources)

| | |
|---|---|
| **What** | A third source acquisition mode (`stream`) beyond `poll` / `push` for long-lived streaming feeds. |
| **Why deferred** | No streaming source in the catalog needs it yet; poll cadence + push webhooks cover the current sources. |
| **Guard rail** | `src/legba/data/schemas/source.py:SourceDescriptor` — `acquisition: Literal["poll", "push"]`; a descriptor declaring `stream` is refused at schema validation (loud pydantic error), it cannot activate half-built. |

### 3. Deep-crawl discovery jobs (decision F-1 — RESOLVED by removal)

| | |
|---|---|
| **What** | `crawl_discovery` / `query_discovery` job kinds (agent-driven deep-crawl / web-query source discovery). The dead enqueue (`discover_sources_tool`) was **removed** per F-1 — it fed kinds no worker handler ever consumed; the `discovery` action pack is retired (`descriptors/action_pack_discovery.yaml`, state=retired). Source discovery ships via the registry discovery route. |
| **Why deferred** | Job-based deep crawl is a designed direction item — `docs/DIRECTION.md` §8 — not wave scope. |
| **Guard rail** | Nothing left to guard: the enqueue path no longer exists. `src/legba/runtime/jobs/worker.py:JobWorker` still fails loud on ANY unhandled kind (generic backstop), and `KNOWN_JOB_KINDS` documents `process_media` as the one shipped kind. |

### 4. Fallback-model budget demotion (decision F-2)

| | |
|---|---|
| **What** | `method.retry.budget.strategy = "demote_and_continue"`: on budget exhaustion, switch the analyst to a cheaper fallback LLM and keep running. The actor-side machinery exists (`fallback_run_method` dispatch, demotion state, `budget_demotion_events` audit rows), but the host never wires a `fallback_run_method`. |
| **Why deferred** | Decision F-2: until a fallback model is provisioned and wired, `demote_and_continue` is an **explicit audited pause until the budget window resets** — the `budget_demotion_events` row is written, a warning names this seam, `cooldown_until` is set to the bucket end, and the run returns `BUDGET_THROTTLED`. No cheaper-model output is fabricated, and nothing degrades silently. |
| **Guard rail** | `src/legba/runtime/dapr_actors.py:_AnalystDeps` — `fallback_run_method=None` in production, and the strategy dispatch handles `demote_and_continue` as the audited pause explicitly (the proceed-via-fallback branch was removed with the G5 fix; the demotion-flag/fallback dispatch machinery remains as this seam's landing zone). |

### 5. Object store (media retention)

| | |
|---|---|
| **What** | A blob/object store for retained media copies (`SourceDeps.object_store`, `Signal.object_ref`). No object-store client exists in the tree. |
| **Why deferred** | Retention today is `reference_only` (media_ref pointers); no source descriptor declares `object_store: true`. |
| **Guard rail** | `src/legba/data/discovery/deps_resolver.py:resolve_discovery_deps` — declaring `deps.object_store: true` with no injected client raises `RuntimeError` at activation (loud refusal, not a silent no-op). Declared shape: `src/legba/data/schemas/source.py:SourceDeps`. |

### 6. Proxy usage ledger persistence (Bright Data)

| | |
|---|---|
| **What** | `proxy_usage_ledger` Postgres table + the `report_usage` write path for residential-proxy bandwidth attribution. The ledger migration has not landed (it lands once a budget analyst actually reads from it). |
| **Why deferred** | Phase-2 of the proxy work item was the handler itself; the ledger is read by nothing yet. |
| **Guard rail** | `src/legba/data/stack/proxy/bright_data.py:ProxyPoolHandler.report_usage` — raises `UsageLedgerUnavailable` when no pg_store is wired, when the ledger table is absent, or when the insert fails. It **never returns an unpersisted record as success** (it previously did; guard added by N-1). `src/legba/data/stack/proxy/bright_data.py:UsageLedgerUnavailable`. |

### 7. `country_list_discovery` remote list sources

| | |
|---|---|
| **What** | `list_source: "url:..."` (fetch the country/topic list from a URL) and `list_source: "substrate:..."` (arbitrary substrate snapshots) for the country-list discovery kind. Only `iso_3166` (substrate-cached snapshot) and `inline:<json>` are built. |
| **Why deferred** | Wave-C scope; the URL fetcher is not wired. |
| **Guard rail** | `src/legba/data/discovery/country_list_discovery.py:CountryListDiscovery._resolve_rows` — both prefixes raise `NotImplementedError` with an actionable message at discovery time (loud fail, accepted shape under the no-stub rule). |

### 8. Common Crawl `S3Client` protocol surface

| | |
|---|---|
| **What** | `S3Client` in the CC-NEWS handler is a structural protocol (bare `raise NotImplementedError` methods) that aiobotocore's client satisfies; tests substitute an in-process backend. This is the abstract-protocol idiom, not a behavioral stub — registered here because the task audit named it. |
| **Why deferred** | Nothing is deferred; the production path constructs a real UNSIGNED aiobotocore client via `_make_aiobotocore_client_factory`. |
| **Guard rail** | `src/legba/data/sources/common_crawl.py:S3Client` — calling an unimplemented protocol method raises immediately. |

### 9. Provenance INSERT routing

| | |
|---|---|
| **What** | `_insert_for_spec` routes provenance-stamped writes to per-table INSERT helpers; a write-spec table with no registered routing raises `NotImplementedError`. |
| **Why deferred** | Not a feature gap — a forward-compat loud-fail guard so a new output table cannot be silently dropped before its INSERT routing lands. |
| **Guard rail** | `src/legba/data/provenance/writes.py:_insert_for_spec` — the `else` branch raises with the offending table + fix instruction. |

### 10. TAXII 2.1 server provisioning (STIX outputs)

| | |
|---|---|
| **What** | An operator-confirmed upstream **TAXII 2.1 server / collection** to push STIX bundles to. The client + wiring are **real and built** (export-interop): `src/legba/data/outputs/taxii_client.py:push_bundle_to_taxii` POSTs the bundle's objects (as a TAXII envelope) to `{server_url}/{api_root}/collections/{collection_id}/objects/` via the structural HTTP port; `stix_bundle.emit` invokes it behind the descriptor `outputs.stix_bundle.config.taxii` binding flag, best-effort (degrade-not-drop — transient/5xx retried with backoff then returned as a structured result, never raised, never blocking the durable NATS/file sinks). What is **not** provisioned is a live destination server — no descriptor in-tree carries a `taxii` binding with a real `server_url`. |
| **Why deferred** | No operator-confirmed TAXII server target exists yet. The transport is finished; only the destination is unprovisioned. Broader STIX/TAXII direction: see `docs/DIRECTION.md`. |
| **Guard rail** | `src/legba/data/outputs/taxii_client.py:push_bundle_to_taxii` / `upload_bundle_to_taxii` — raise `TaxiiServerNotConfiguredError` (a `RuntimeError`, NOT a stub) when asked to push with no `server_url`, no HTTP client, or a cleartext non-loopback host. An un-provisioned/half-configured `taxii` binding refuses loudly; it never fabricates a delivery or silently drops the TLP-marked bundle. (No allowlist line — the code fails loud, it does not stub output.) |

### 11. Consult `vector_search` embedder wiring

| | |
|---|---|
| **What** | Semantic `vector_search` on the consult substrate-query port. The Qdrant query path exists (`vector_search_by_embedding`), but no embedding model is surfaced through the port, so free-text `vector_search` cannot run. |
| **Why deferred** | Embedder-through-port wiring is the L-114 follow-up. |
| **Guard rail** | `src/legba/runtime/substrate_query_port.py:PostgresQdrantSubstrateQueryPort` — `vector_search` returns the Protocol's explicit `{"unavailable": True, "reason": "no_embedder_wired ..."}` shape instead of fabricating a vector; `search_signals` likewise reports `scope_predicate_applied: False` rather than pretending the Starlark predicate ran. |

### 12. Optimizer parent-prompt loading degradation

| | |
|---|---|
| **What** | `_load_parent_prompt_text` falls back to a clearly-marked `<<missing prompt module: ...>>` / `<<no prompt text found ...>>` marker string when a prompt module cannot be imported or has an unexpected shape. |
| **Why deferred** | Graceful degradation by design: the GEPA loop computes a delta against the marker instead of crashing; the marker is visible in the optimizer's output, never passed off as a real prompt. |
| **Guard rail** | `src/legba/runtime/dapr_workflow/gepa.py:_load_parent_prompt_text` — the marker text is unmistakable in any audit of optimizer runs. (Moved from the deleted `runtime/temporal/` package by C-3.) |

### 13. Non-text modality renderers (UI)

| | |
|---|---|
| **What** | UI renderers for image / audio / video / structured (incl. `application/geo+json`) signal modalities. Only the `text` renderer is `implemented: true` in `MODALITY_RENDERERS` (`legba-ui-v3/src/lib/modalityRenderers.tsx`); audio / video / image / structured / binary are all `implemented: false` and fall through to the generic raw-payload view with a "pending" badge — never a fabricated preview. (The live geospatial map is a SEPARATE v4 Dockview panel built on Leaflet — NOT a `MODALITY_RENDERERS` entry; geojson sources still ingest fine via `src/legba/data/sources/geojson.py`.) |
| **Why deferred** | UI track work (see `docs/UI_ROADMAP.md`); blocked on the media-extraction seam (#1) producing real derived content to render. |
| **Guard rail** | UI-side: unknown modalities fall through to the generic payload view — no backend symbol involved (the scanner does not cover `legba-ui-v3/`). |

### 14. RBAC / STIX direction / MCP surface / multi-tenancy

| | |
|---|---|
| **What** | Platform-level direction items: role-based access control, the fuller STIX/TAXII story (beyond seam #10), the expanded MCP surface, and real multi-tenant isolation (today `tenant_id` is stamped through envelopes/ledgers but there is one operating tenant). |
| **Why deferred** | These are product-direction decisions, not wave-scope code seams. The authoritative write-up is `docs/DIRECTION.md` (authored in parallel with this registry). |
| **Guard rail** | `docs/DIRECTION.md`. In-tree today: registry API requires bearer auth (`require_bearer`); MCP tool outputs (`src/legba/data/outputs/mcp_tool.py`) and STIX bundle export (`src/legba/data/outputs/stix_bundle.py`) are real, built surfaces. |

### 15. A2A skill surface operator-gated OFF (fail-closed)

| | |
|---|---|
| **What** | The L-193 A2A skill router (`GET`/`POST /a2a/skills[/{skill_id}]`) is fully built (`src/legba/data/outputs/a2a_skill.py`, signed envelopes, trusted-key directory) and IS mounted on the production runtime — but only when `LEGBA_A2A_ENABLED=1` plus a non-empty `LEGBA_A2A_TRUSTED_KEYS` allowlist (or `LEGBA_DEV_MODE=1`). By default the surface is OFF (B-2 fail-closed security posture). |
| **Why deferred** | Mounting an inter-agent skill surface with no caller allowlist would expose the unauthenticated GET endpoints (and any `auth_required=false` skill) to every caller. The default-off posture is intentional, not an oversight — earlier docs (DESIGN:446, RUNBOOK:229) overstated it as always-mounted. |
| **Guard rail** | When disabled, `main()` mounts a tiny stub at `/a2a/skills[/{skill_id}]` that returns **HTTP 503** with `error: a2a_skill_surface_disabled` and the enable recipe — a FAIL-LOUD response, never a silent 404 (`src/legba/runtime/dapr_host.py`, the `_a2a_disabled` handler on the `resolve_a2a_mount() is None` branch). The full surface is xfail-tracked by `tests/.../test_a2a_skill_router_e2e.py`. Enable recipe: set `LEGBA_A2A_ENABLED=1` + `LEGBA_A2A_TRUSTED_KEYS=did:legba:caller=<verify-key-hex>,…` and restart the runtime. |
### 16. Scheduler-side reminder enumeration (orphan-reminder GC part 2)

| | |
|---|---|
| **What** | Part 2 of the Dapr orphan-reminder GC: enumerating the dapr-scheduler's *full* reminder set (by reading the embedded-etcd keyspace directly) so the sweep can also GC reminders whose owning `actor_state` row was itself lost. Part 1 — the desired-vs-observed sweep that unregisters reminders owned by RETIRED `actor_state` rows — IS BUILT and live (`src/legba/runtime/reminder_gc.py:sweep_orphan_reminders`, wired into the reconcile resync loop). |
| **Why deferred** | daprd 1.17.9 exposes no reminder-listing API on :3500, so part 2 must read the dapr-scheduler etcd keyspace directly (a separate, riskier integration). Part 1 closes the documented orphan-on-retire failure mode (RUNBOOK §0); part 2 is for the rarer lost-`actor_state`-row case, which the version-drift sibling sweep + on-fire guard already mitigate. |
| **Guard rail** | No stub: part 1 ships fully and acts only on provably-orphan (RETIRED) reminders — it never fabricates a delete and never touches a live actor. Part 2 is gated behind the unset `LEGBA_REMINDER_GC_SCHEDULER_SCAN` env flag (documented in `reminder_gc.py`); until built there is simply no scheduler-scan code path, so nothing half-built can run. Fallback for the lost-row case remains the documented full scheduler-data wipe (RUNBOOK §0/§11). |
### 17. Multi-replica headcount is operator-declared, not runtime-counted (scaling-multinode interim guard)

| | |
|---|---|
| **What** | The interim single-replica safety guard (`assert_singleton_safe`) trusts the **operator-declared** `LEGBA_REPLICA_COUNT` env var; the runtime has no way to introspect how many sibling replicas the orchestrator actually launched. So the guard refuses to boot on the honest *config* (`>1` declared + no leader election) but cannot detect a deploy that silently launches 2 replicas while each still has `LEGBA_REPLICA_COUNT` unset/1. The real cross-replica safety is the **leader election** below it (Postgres advisory lock), which is correct regardless of the declared count — the count guard is the cheap fail-loud belt that catches the common misconfiguration. |
| **Why deferred** | A true runtime headcount needs membership coordination (a registry/heartbeat table or the Dapr placement membership API). Out of scope for the interim guard per locked decision D3 (`planning/SCALING.md`): prove the design locally, keep the guard cheap + honest. The full multi-replica deployment with autoscaling is a direction item. |
| **Guard rail** | NOT a stub (no allowlist line — the code fails loud, it does not fabricate). `src/legba/runtime/leader.py:assert_singleton_safe` raises `SingletonSafetyError` at boot when `LEGBA_REPLICA_COUNT > 1` without `LEGBA_LEADER_ELECTION`; `src/legba/runtime/leader.py:LeaderLease` (advisory-lock leader election) is the actual cross-replica correctness primitive and runs regardless of the declared count. The honest limit is documented in `planning/SCALING.md` §9 + `docs/RUNBOOK.md` §22. |
### 18. Backup offsite destination (resilience-observability W-1b §5)

| | |
|---|---|
| **What** | A concrete offsite destination for the scheduled backup (`scripts/backup_scheduled.sh`). The full backup (pg/redis/qdrant/NATS JetStream), retention, the systemd timer, and the offsite *push logic* (rsync / s3 / rclone) are all BUILT. What is deferred is the *destination wiring*: no `LEGBA_BACKUP_OFFSITE_DEST` is provisioned on the rig, so backups are local-only until an operator sets one. |
| **Why deferred** | No offsite/object-store target is provisioned for this deployment yet; choosing one (bucket / backup host / rclone remote) is an operator/infra decision, not wave-scope code. |
| **Guard rail** | `scripts/backup_scheduled.sh` (shell, outside `src/legba/**` so not stub-scanner scope): with `LEGBA_BACKUP_OFFSITE_DEST` unset it WARNs loudly and writes an `OFFSITE_NOT_CONFIGURED.txt` marker into the generation dir (never silently claims an offsite copy exists); with it set, a failed push exits non-zero so the systemd unit goes `failed`. Restore drill: `docs/RUNBOOK.md` §23.3. |

### 19. Cross-source coalesce vector-deps path (P2 data-integrity)

| | |
|---|---|
| **What** | The substrate-wide cross-source semantic/temporal coalesce analyst (`cross_source_coalesce` deterministic sub-handler) is **real and built**: it embeds recent canonical signals across all sources into a shared Qdrant collection (`legba_coalesce`) and links near-duplicate signals reporting the SAME real-world event from DIFFERENT sources, reusing `Dedupe4TierHandler`'s tier-3 (cosine) + tier-4 (temporal window + title Levenshtein) logic — link-never-collapse via `signal_aliases`. The host threads the live `embedding_service` + `qdrant` ports onto the analyst deps bundle (`dapr_host.standard_deps.extras`). What is the **seam** is the run that fires when EITHER port is absent (e.g. the L-122 hosted embedding endpoint or the Qdrant cluster isn't provisioned on the rig): there is no non-vector deterministic fallback for "same event, different words" (exact content_hash is `cross_source_dedup`'s job), so the handler cannot produce links. |
| **Why deferred** | Semantic coalescing fundamentally needs an embedding model + a vector store; both are hosted/provisioned infra (`embed.primary.openai_compat` + `vector.qdrant.cluster_main`), not in-tree code. When a deployment runs without them, the analyst must refuse rather than fabricate. The analyst is also off-by-default (NOT in the default bringup set; the handler's `enabled` option defaults False — the `signals_retention` precedent) so it never runs unless an operator opts in. |
| **Guard rail** | NOT a stub (no allowlist line — the code refuses loud + degrades-not-drops, it does not fabricate). `src/legba/data/analysts/deterministic_handlers/cross_source_coalesce.py:handle` — when a required port (`embedding_service` / `qdrant`) is missing on the live-pool path it emits a `FindingPayload` tagged `coalesce_unavailable` with `data.unavailable = "missing:<ports>"` and writes ZERO `signal_aliases` rows (never a fabricated link); per-row embed failures degrade that one row out of the run rather than sinking the sweep, and a Qdrant persist hiccup never blocks the in-memory pairing. |

### 20. Tier-2 grounding `vector:world_context` collection

| | |
|---|---|
| **What** | Tier-2 of analyst knowledge-grounding: a curated *unstructured-brief* vector collection (`world_context`) that the grounding resolver would query semantically alongside the structured substrate facts. Tier-1 (the structured path) is **real and live**: `src/legba/runtime/grounding.py:SubstrateGroundingResolver` reads CURRENT authoritative `facts` / `nexuses` (temporal-honesty gate `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`, curated/seed-preferred) and `build_grounding_preamble` injects them; opted in on `analyst_world_assessor` / `analyst_country_assessor` (`grounding.enabled: true`). The descriptor schema already **accepts** `vector:world_context` as a grounding source (`src/legba/data/schemas/analyst.py:GroundingBlock.sources` — `Literal[..., "vector:world_context"]`) so a descriptor can pre-declare it, but the resolver acts ONLY on the `substrate` source today. |
| **Why deferred** | The vector path needs the embedder-through-port wiring (the same **L-114** follow-up that blocks seam #11's free-text `vector_search`) plus a curated `world_context` collection to embed into. Neither is provisioned; the structured substrate already covers the load-bearing case (current officeholders + bloc memberships) without an embedding dependency. |
| **Guard rail** | NOT a stub (no allowlist line — the code degrades-not-drops, it does not fabricate). `src/legba/runtime/analyst_deps_builder.py:_build_grounding_hook` — when a descriptor's `grounding.sources` does NOT include `substrate` (i.e. only declares `vector:world_context`), it logs `analyst_deps_builder.grounding.no_substrate_source` and the per-run hook returns `None` (no preamble) rather than injecting an empty/fabricated block. A grounding read failure anywhere also yields `None` (`grounding.resolve.failed` / `inline_target.grounding.failed`), never a stray header. |

### 21. Time-series metrics / observability + full-text search backing (stores removed)

| | |
|---|---|
| **What** | Two backing stores that were *provisioned-but-idle* with zero callers and have been **removed from the codebase**: (a) the time-series **metrics / observability** store (the pre-pivot Grafana/TimescaleDB stack — a `metrics(time, metric, dimension, value)` hypertable + intended rollup dashboards), and (b) the **full-text-search** backing (OpenSearch BM25, primary + an isolated audit cluster). Their stores, config dataclasses, descriptor-schema classes, stack-registry kinds, health checkers, the `metrics_collection` deterministic sub-handler, and the compose services/volumes were all deleted. |
| **Why deferred** | Neither was ever wired into a live write/read path (the metrics writer had no caller and `deps.metrics_client` was never populated; nothing indexed signals into OpenSearch), so keeping idle containers + config was honesty debt, not capability. A real observability stack and a dedicated BM25 backing are direction items, not wave scope. **Important:** `anomaly_detection` is **unaffected** — it reads `time_bucket()` from the **primary Postgres pool**, never a separate Timescale cluster, and survives. The full-text/search **Protocol surface also survives**; only the OpenSearch backing is gone. |
| **Guard rail** | No stub to guard — the code paths were deleted, not faked. For full-text search, `src/legba/runtime/substrate_query_port.py:PostgresQdrantSubstrateQueryPort.search_signals` falls back to **Postgres FTS** (`to_tsvector`/`plainto_tsquery`) — a real result, never a fabricated one. There is no metrics store at all: nothing writes time-series metrics, so nothing can silently pretend to. (No allowlist line — there is no symbol; the components are removed.) |

### 22. Live GATHER actuation of the `web_access` / `propose_facts` tools (S6) — CLOSED (2026-06-19)

| | |
|---|---|
| **Status** | **CLOSED.** The run-path wiring that lets a *running* `inline_target` assessor invoke the S6 external + write-back tools mid-run now ships. The previously-deferred edits landed in the three WF-C run-path files. |
| **What shipped** | (1) `inline_target._GATHER_TOOLS` now spans the read surface **plus** `web_fetch`/`web_search` (`web_access`) and `propose_fact`/`request_source`/`open_question` (`propose_facts`); the GATHER loop ROUTES each tool to the binding for ITS owning pack so `Agency.run_pack_tool` enforces tool↔pack ownership + the per-pack governor (read tools → the `substrate_read` binding; write/web tools → their per-tool binding). (2) `dapr_host` builds the per-pack write/web GATHER bindings — but ONLY for an inline_target assessor that ALSO grants the pack via `action_packs`, and ONLY when the base `substrate_read` GATHER binding is itself wired; `pg_pool` is threaded onto each binding's `ToolContext`. (3) `dapr_actors._gather_write_bindings_for_target` re-points each binding to the running target's `allowed_action_packs` per run and, for the write pack, injects a per-run `WritebackContext` (the run's pg_pool + a fresh per-run `AnalystContext`) **copy-on-write** — it clones the binding + its `ToolContext`, never mutating the shared base (the documented fan-out race). (4) `inline_target._gather_system_suffix` splices the bound packs' operator-authored `prompt_fragments`+`rules` (from `descriptors/action_pack_web_access.yaml` / `action_pack_propose_facts.yaml`) into the GATHER system prompt. |
| **Trust-model constraints it shipped under** | The wired tools are **PROPOSE-grade ONLY** and stay inside the existing three-way agency gate — nothing here bypasses it. `propose_fact` writes `source_type='proposed'` via `write_fact` (never authoritative, never `_insert_fact`); `request_source`/`open_question` write `hypotheses` rows via `write_hypothesis`. **NONE** mutate the control-plane (no source/target/analyst descriptor writes). `web_fetch`/`web_search` egress **only** through `SsrfGuardedTransport`. Every write carries MANDATORY `derived_from` lineage (review S-1 — the assessor's reasoning is driven by untrusted RSS text, so an uncited write is refused). A write/web pack a target does NOT allow is a loud BLOCK at resolution; a write/web tool named with no wired binding is a clean `tool_unbound` no-op folded back to the planner — never an ungoverned call, never dispatched through the read binding. |
| **Guard rail** | Everything still fails loud, nothing fabricates: each write handler returns a `failed` `ToolResult` when `ctx.writeback` is absent (`src/legba/data/analysts/agency/write_tools.py`); the web handlers refuse non-public egress; a granted-but-unbindable write/web pack is FAIL-LOUD at deps build (`dapr_host` returns `None` → activation refuses), mirroring the consult/escalation/substrate_read legs. Exercised end-to-end through the real `Agency.run_pack_tool` in `tests/data_pkg/agency/test_web_and_propose_tools_e2e.py`, and the run-path routing + copy-on-write + propose-with-lineage + unbound/blocked degrade-not-drop paths in `tests/data_pkg/test_analyst_inline_target.py` (SEAM #22 block). (No allowlist line — there is no stub symbol; the handlers and the wiring are real and complete.) |

### 23. Dapr long-activity workflow round-trip → in-process fallback (daprd 1.17.9)

| | |
|---|---|
| **What** | The durable **Dapr Workflow** round-trip does not resume the orchestrator after a **long-running activity** completes (short activities deliver; long ones run fine and return, but the orchestrator is never re-woken). This blocks the durable persistence leg of the GEPA optimizer workflow and the durable `deep_consult` round-trip. Each degrades to the **in-process** path instead: the optimizer runs its GEPA loop in-process (`run_optimizer_in_process`, the `StubWorkflowHandle` non-stub of seam allowlist) and deep_consult runs synchronously, so the analysis still completes — what is lost is the durable, externally-resumable workflow execution, not the result. |
| **Why deferred** | Verified **NOT a Legba bug**: the activity executes and returns, the threads sit idle post-compile, the result is tiny, it reproduces on a fresh engine and is `num_threads`-independent. The blocker is in daprd 1.17.9's workflow runtime, not our orchestrator/activity code. The **compile-hang sub-issue is FIXED** (the bridge LM call had no timeout → infinite hang → no trace → silent death; now per-call + dispatch timeouts, valset cap, real rollouts, and an observable `workflow_timeout` trace). Tracking: `planning/` (Dapr long-activity round-trip note). |
| **Guard rail** | NOT a stub — both paths produce a real result and fall back observably. Optimizer: `src/legba/runtime/dapr_workflow/gepa.py` (in-process GEPA via `run_optimizer_in_process`; `optimizer_workflow.in_process` / `.unavailable` log signposts, RUNBOOK §4.2). The compile timeout is observable as a `workflow_timeout` trace rather than a silent hang. The durable path remains the desired end state; until daprd resumes long activities, the in-process fallback is the live behaviour and never fabricates output. |

### 24. `nlp_client` boot-singleton cannot re-resolve — RESOLVED (2026-06, #91 / `985db7e`)

| | |
|---|---|
| **What** | The runtime builds its NLP / embedding / vector clients **once at boot** from the registered stack components (`dapr_host.bring_up_production_runtime`). If the `nlp.local.legba_models` stack component is absent/unseeded at boot, `nlp_client` stays `None` for the whole process lifetime — it cannot be re-resolved without a runtime restart. Bringing the runtime up against an empty registry and seeding the stack *afterwards* leaves source enrichment unable to build (`source_deps_resolver.enrichment_build_failed`), so signals land with no `geo`/entities and geo-scoped analysts have nothing to match. |
| **Resolution** | Closed by **#91** (`985db7e`): `src/legba/runtime/nlp_client_factory.py:LazyNlpClient` resolves the NLP / embedding / vector clients on FIRST use and **re-resolves on handler-build**, so a stack component seeded *after* boot is picked up **without a runtime restart** — the boot-time permanent-`None` is gone (+71-line factory, +130-line test). The documented bring-up ORDER + fail-loud boot signpost remain as belt-and-suspenders. _(Prior status: deferred as a reconcile-loop-driven architecture change; #91 delivered the lazy/self-healing equivalent.)_ |
| **Guard rail** | NOT a stub — the missing client degrades loud, never fabricates enrichment. Boot logs `dapr_host.nlp_client.built component=nlp.local.legba_models` on success and `nlp_client.unavailable` when the component is absent; per-signal enrichment build refuses loud (`source_deps_resolver.enrichment_build_failed … requires an nlp_client_factory`). Operator recipe (seed BEFORE boot, or `--force-recreate` the runtime after seeding) is `docs/RUNBOOK.md` §0. |

### 25. Journal `change`-proposal apply path not yet exercised against a live registry

| | |
|---|---|
| **What** | The journal assessor (the 11th `OutputKind` — Legba's first-person reflective voice, LIVE) PROPOSES every outward effect into the human-gated `journal_proposals` queue; on operator accept, an idempotent per-kind apply worker (`src/legba/data/registry/journal_proposals_apply.py:apply_accepted_proposal`) runs the change through an EXISTING write/lifecycle path. The `correction` apply (`supersede_prior_facts`) and the `self_revision` apply (a PROMOTED `prompt_module_candidate` row the optimizer's `resolve_promoted_system_prompt` reads, with the §7.5(b) protected-section gate) are BUILT and tested END-TO-END against a real pool. The third kind — `change` (`_apply_change`: a descriptor/stack diff merged onto the current head and persisted via the registry's own `update` path, the same one `PUT /stack/{id}` / `PUT /descriptors/{family}/{id}` use) — is real, import-verified, and unit-smoke covered, but has **not yet been exercised against a live registry**. |
| **Why deferred** | The `change` apply needs a populated registry head to fetch + deep-merge + re-stamp; the e2e accept/reject suite seeds Postgres but stubs the registry deps (`tests/journal_w4/test_accept_reject_lifecycle.py` — the docstring on `_DEPS_STUB` records exactly this: change apply "needs a registry, covered by import + unit smoke"). Wiring a live-registry fixture for the `change` leg is follow-up work; the apply code path itself is complete, not stubbed. |
| **Guard rail** | NOT a stub — every apply path fails loud, none fabricate. `_apply_change` raises `ProposalApplyError` on an unknown op / a bad diff shape, and the accept endpoint rolls a failed apply forward to `archived` with the reason (it is never left dangling in `accepted`). The whole queue is the backstop: the journal can write ONLY its own `journal_entries` rows directly (off-chain, empty `derived_from`, excluded from the lineage catalog — it NEVER writes a fact/finding/nexus), so an unexercised `change` apply cannot leak any live mutation — a human must accept first, and the apply only ever runs the existing registry `update`. |

### 26. Journal Wave 5 — critic + optimizer over the journal's own voice (designed-not-built)

| | |
|---|---|
| **What** | Routing the journal's voice through a CRITIC (a grounding-fidelity-vs-perspective rubric) plus a GEPA optimizer over logged journal traces + the accept/reject decision log (`planning/JOURNAL_ASSESSOR_PLAN.md` §12 Wave 5). Today the journal's entries are LOGGED-AND-SCOREABLE but the voice is not yet looped through a score. |
| **Why deferred** | Gated on first **building a critic actuator**. The critic is structurally NON-ACTUATING on the live path today — `overall_score` is computed but ignored (the adversarial-review finding, seam-adjacent to the broader "critic has no actuator" gap); "measured" means scoreable, not yet tuned. Scoring a moving target is also premature until the entry shape is stable. This is a designed direction item, not wave-scope code — no half-built critic/optimizer-over-voice path exists in-tree. |
| **Guard rail** | Nothing to guard — there is no code path. The live journal runs its PLAN → GATHER → NARRATE arc and writes entries/consolidations; it is not wired to any critic-driven confidence adjustment, so nothing can silently claim the voice was vetted by a critic. The dependency (build a critic actuator first) is documented in `planning/JOURNAL_ASSESSOR_PLAN.md` §5 / §12. |

### 27. Journal UI panel first in-browser render (UI track)

| | |
|---|---|
| **What** | The `system.journal` Dockview panel (`legba-ui-v3/src/panel-registry/registry.ts`, `Component: SystemJournal`) renders journal entries with provenance chips deep-linking to the cited record and `[needs_citation]` / perspective spans in a distinct style, over `GET /api/v1/journal`; the operator review surface drives `GET /journal_proposals` + the accept/reject endpoints. The panel is tsc-green and fully wired. |
| **Why deferred** | At the time of writing the panel was pending its first real in-browser render (the rendered-first eyeball loop, consistent with the broader UI track — `docs/UI_ROADMAP.md`). Not a code stub: the panel is built and registered; only the in-browser visual confirmation was outstanding. |
| **Guard rail** | UI-side, no backend symbol involved (the stub scanner does not cover `legba-ui-v3/`). The panel reads only the real read routes; it fabricates nothing — an unrendered panel shows real (or empty) data, never a fabricated entry. |

---

## Audited identifiers that are NOT stubs

These trip the name scanner but were audited as real implementations; they
are allowlisted with their justification (the registry IS the escape
mechanism — there is no per-line pragma):

* `src/legba/runtime/dapr_workflow/gepa.py:StubWorkflowHandle` — handle-shaped
  wrapper around a **real** in-process optimizer result
  (`run_optimizer_in_process` does the actual GEPA work); only the
  workflow/run ids are synthetic (`in_process::<id>`). (Moved from the
  deleted `runtime/temporal/` package by C-3.)
* `src/legba/data/sources/gdelt.py:_StubParam` — duck-typed stand-in for
  `bigquery.ScalarQueryParameter` carrying real name/value pairs; exists so
  query-parameter iteration works whether or not google-cloud-bigquery is
  importable. No fake behavior.
* `src/legba/data/sources/gdelt.py:GDELTBigQuerySourceHandler._build_query_job_config._Stub`
  — same pattern: a duck-typed `QueryJobConfig` attribute container built
  only when the BigQuery SDK is absent; the real config object is used when
  the SDK imports.

---

## Machine-readable allowlist

The block between the BEGIN/END markers is parsed by
`tests/test_no_undeclared_stubs.py`. One `src/legba/<path>.py:<dotted.symbol>`
per line; a class-level entry covers symbols nested under it. Lines starting
with `#` are comments.

<!-- BEGIN SEAM ALLOWLIST -->
```
# seam 1 — eager media extraction (refuse-loud guards; stub edge removed by A-2)
src/legba/runtime/jobs/media_client.py:MediaClient
src/legba/runtime/jobs/process_media.py:process_media_handler
src/legba/data/jobs/media.py:MediaExtractionResult
# seam 6 — proxy usage ledger loud-fail guard
src/legba/data/stack/proxy/bright_data.py:ProxyPoolHandler.report_usage
src/legba/data/stack/proxy/bright_data.py:UsageLedgerUnavailable
# seam 7 — country_list_discovery url:/substrate: list sources (loud NotImplementedError)
src/legba/data/discovery/country_list_discovery.py:CountryListDiscovery._resolve_rows
# seam 8 — common crawl S3 protocol surface (abstract idiom, registered per audit)
src/legba/data/sources/common_crawl.py:S3Client
# seam 9 — provenance INSERT routing guard
src/legba/data/provenance/writes.py:_insert_for_spec
# seam 10 — TAXII push is REAL; only an un-provisioned destination refuses
# loud (TaxiiServerNotConfiguredError is a RuntimeError guard rail, NOT a
# NotImplementedError stub — no allowlist line needed; the code never
# fabricates output).
# audited non-stub identifiers (see section above)
src/legba/runtime/dapr_workflow/gepa.py:StubWorkflowHandle
src/legba/data/sources/gdelt.py:_StubParam
src/legba/data/sources/gdelt.py:GDELTBigQuerySourceHandler._build_query_job_config._Stub
```
<!-- END SEAM ALLOWLIST -->
