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

### 11. Consult `vector_search` embedder wiring — RESOLVED (L-114, 2026-07-02)

| | |
|---|---|
| **Status** | **RESOLVED (L-114 / S5-T1).** The host now threads the hosted embedding client (`embed.primary.openai_compat` — the same `HostedEmbeddingClient` the dedupe-tier-3 path already uses) through the consult substrate-query port at bring-up, so free-text `vector_search` embeds-then-searches live: `PostgresQdrantSubstrateQueryPort(pg_pool=…, qdrant_client=…, embedder=embedding_service)` in `dapr_host.bring_up_production_runtime`. The Qdrant cosine path was already built (`vector_search_by_embedding`); L-114 was purely the embedder-through-port wiring. The SAME embedder-through-port now also backs the live `search_context` corpus-RAG tool (one of the 19 `substrate_read` pack tools) and the Tier-2 `vector:world_context` grounding read — see SEAMS #20 (RESOLVED). |
| **Fix** | `src/legba/runtime/substrate_query_port.py:PostgresQdrantSubstrateQueryPort.vector_search` — when an `embedder` is present it calls `embedder.embed(query)` then delegates to `vector_search_by_embedding` (tagging the result `backing: "qdrant_cosine"`); an embed-backend failure degrades to `{"unavailable": True, "reason": "embed_failed: …"}` and an empty query short-circuits to an empty result (mirrors `search_signals`). The embedder is threaded from `dapr_host` → `analyst_deps_builder.build_analyst_run_method(embedding_service=…)` and directly into the port ctor. |
| **Guard rail** | NOT a stub — no fabricated vectors. When the embedding service wasn't provisioned (`embedder is None`) the method still returns the Protocol's explicit `{"unavailable": True, "reason": "no_embedder_wired ..."}` shape (the honest degrade), and `search_signals` still reports `scope_predicate_applied: False` rather than pretending the Starlark predicate ran (that L-104 leg is unchanged). |

### 12. Optimizer parent-prompt loading degradation

| | |
|---|---|
| **What** | `_load_parent_prompt_text` falls back to a clearly-marked `<<missing prompt module: ...>>` / `<<no prompt text found ...>>` marker string when a prompt module cannot be imported or has an unexpected shape. |
| **Why deferred** | Graceful degradation by design: the GEPA loop computes a delta against the marker instead of crashing; the marker is visible in the optimizer's output, never passed off as a real prompt. |
| **Guard rail** | `src/legba/runtime/dapr_workflow/gepa.py:_load_parent_prompt_text` — the marker text is unmistakable in any audit of optimizer runs. (Moved from the deleted `runtime/temporal/` package by C-3.) |

### 13. Non-text modality renderers (UI)

| | |
|---|---|
| **What** | UI renderers for image / audio / video / structured (incl. `application/geo+json`) signal modalities. Only the `text` renderer is `implemented: true` in `MODALITY_RENDERERS` (`legba-ui-v3/src/lib/modalityRenderers.tsx`); audio / video / image / structured / binary are all `implemented: false` and fall through to the generic raw-payload view with a "pending" badge — never a fabricated preview. (The live geospatial map is a SEPARATE v4 Dockview panel — NOT a `MODALITY_RENDERERS` entry. Its DEFAULT world map is now the **maplibre-gl** banded-verdict CHOROPLETH (`legba-ui-v3/src/v4/world/MapLibreWorldMap.tsx`), with **Leaflet** as the `hasWebGL`-false FALLBACK selected by `legba-ui-v3/src/lib/mapEngine.ts`, plus the `TileWebGLOverlay` tile-overlay harness (`legba-ui-v3/src/components/TileWebGLOverlay.tsx`) — no longer Leaflet-only. geojson sources still ingest fine via `src/legba/data/sources/geojson.py`.) |
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

### 20. Tier-2 grounding `vector:world_context` collection — RESOLVED wiring; RECALIBRATED + GUARDED PILOT (S5 vector RAG, 2026-07)

| | |
|---|---|
| **Status** | **RESOLVED / PROVISIONED (was: un-provisioned).** The prior entry said the resolver "acts ONLY on the `substrate` source today" and the `world_context` collection was un-provisioned infra. Both are now FALSE — the vector RAG leg is LIVE. Two curated Qdrant collections are populated and served: `world_context` (country/topic priors + doctrine summaries — **293 chunks** live) and `tradecraft` (analytic standards / SAT handbooks — **1,716 chunks** live), both `bge-m3` **1024-dim cosine**. The embedder is threaded through the stack port (SEAMS #11, resolved), and the grounding resolver DOES consume the `vector:world_context` source. |
| **What shipped** | (1) The two Lane-4 corpora were chunked + embedded into Qdrant via `src/legba/data/rag/lane4_loader.py` + `chunker.py` (populated live: 293 / 1,716 points). (2) Tier-1 (the structured path) stays real and live: `src/legba/runtime/grounding.py:SubstrateGroundingResolver` reads CURRENT authoritative `facts`/`nexuses` (temporal-honesty gate `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`, curated/seed-preferred) and `build_grounding_preamble` injects them — now opted in on all SEVEN bounded units + the journal tiers. (3) Tier-2 is now WIRED: the resolver embeds the target/slice query through the port embedder and semantic-searches `world_context` with a RELEVANCE FLOOR + COUNTRY FILTER, DEGRADE-not-drop when the corpus returns nothing (an empty/low-score corpus read → no preamble, never a fabricated brief). (4) RAG is now run as a GUARDED, MEASURED PILOT on **`internal_stability` ONLY** — `leadership_transition` RAG is **OFF** (the 2026-07-03 rollback is now live, DB-confirmed); the other units carry `sources: [substrate, situations, graph_structure]` (Tier-1 only). (See the **Recalibration** + **Residual** rows below for the pilot state, the auto-rollback guard, and the honest tail-risk.) (5) The consult/GATHER surface reads the same corpora via the live `search_context` tool (one of the 19 `substrate_read` pack tools). |
| **Recalibration (2026-07-06)** | The pilot was recalibrated + re-activated after the 2026-07-03 rollback. The embedder (`bge-m3`) was fine; the fixes were retrieval USAGE: a focused `"<country> <theme>"` query (was a diluted unit-name + entity blob), doc contextualization (chunks embedded with a `"<Country> — <section>"` lead), the 293-point corpus **re-embedded in place** (`scripts/reembed_world_context.py`), and the relevance floor lowered **0.65 → 0.55** (on-target now ~0.6, off-target ~0.42). Per-run trace instrumentation records `world_context_top_score` / `retained` / `min_score` so the measurement is honest, and the injected priors remain **NON-CITABLE** (fenced background, no `[N]` ids). A **REAL per-run auto-rollback guard** (`src/legba/runtime/rag_rollback.py`) now replaces the old comments-only one: it re-checks a disabled-units env (`LEGBA_WORLD_CONTEXT_DISABLED_UNITS`) + a persisted state file (`LEGBA_RAG_ROLLBACK_STATE`) on EVERY run, so a rollback suppresses injection on the NEXT run **without a restart**; triggers = faithfulness drop / low-faith ratio / token-cost rise (≥35%), actuated by `scripts/rag_watch.py --enforce`. |
| **Residual (honest)** | This is a PROVISIONED-and-recalibrated leg run as a **guarded pilot** — its faithfulness BENEFIT is still being MEASURED, so treat the lift as UNPROVEN. Honest limits: (a) the pilot is ON for **one unit only** (`internal_stability`; `leadership_transition` is rolled back OFF) — staggered per-unit expansion continues, and whether the flip actually IMPROVES faithfulness is measured via the armed `rag_watch` (`scripts/rag_watch.py`) against a captured pre-flip baseline, with a one-line rollback (drop `vector:world_context` from the unit's `grounding.sources`); (b) **KNOWN TAIL RISK** — firing RAG historically THICKENED the low-faithfulness TAIL even with the non-citable header (the uncited-prior-leak mechanism, where a model reads the fenced background as fact and asserts it without an `[N]`); the per-run guard REVERTS the pilot if that recurs, which is exactly why this stays a guarded pilot, not "done"; (c) the pilot **state file currently lives at an EPHEMERAL path** — move `LEGBA_RAG_ROLLBACK_STATE` to a mounted volume so the rollback state survives a container recreate (backlog). |
| **Guard rail** | NOT a stub (no allowlist line — the code degrades-not-drops, it does not fabricate). `src/legba/runtime/analyst_deps_builder.py:_build_grounding_hook` — a grounding read failure anywhere yields `None` (`grounding.resolve.failed` / `inline_target.grounding.failed`), never a stray/empty header; a sub-floor or empty corpus read contributes no preamble. A descriptor whose `grounding.sources` omits `substrate` still logs `analyst_deps_builder.grounding.no_substrate_source` and returns `None` rather than injecting a fabricated block. The auto-rollback guard is fail-SAFE: on a triggered rollback (or a unit named in `LEGBA_WORLD_CONTEXT_DISABLED_UNITS`) `rag_rollback` suppresses `vector:world_context` injection on the very next run — the worst case is Tier-1-only grounding, never a fabricated prior. |

### 21. Time-series metrics / observability store removed — full-text search is LIVE (the OpenSearch signal corpus)

| | |
|---|---|
| **What** | The time-series **metrics / observability** store was *provisioned-but-idle* with zero callers and has been **removed from the codebase**: the pre-pivot Grafana/TimescaleDB stack — a `metrics(time, metric, dimension, value)` hypertable + intended rollup dashboards. Its store, config dataclass, descriptor-schema class, stack-registry kind, health checker, the `metrics_collection` deterministic sub-handler, and the compose service/volume were all deleted. **The full-text-search backing is NOT removed — it is LIVE and load-bearing** (the earlier "OpenSearch backing removed" claim is retired): a single-node **OpenSearch** cluster (service `legba-opensearch-1`) holds the full-text signal corpus index `legba_signals_corpus` (~112k docs), populated by the `corpus_indexer` deterministic analyst and queried by the `search_corpus` / `read_document` `substrate_read` tools. This is the read leg of the **signal-content-depth** subsystem: full-body signal reads → OpenSearch `legba_signals_corpus` → the `search_corpus`/`read_document` tools + `signal_summarizer`, `signal_embedder` (→ Qdrant `vector_search`), plus the `corpus_researcher` and `cross_doc_corroborator` analysts. |
| **Why removed (metrics only)** | The **metrics** store was never wired into a live write/read path (the metrics writer had no caller and `deps.metrics_client` was never populated), so keeping the idle container + config was honesty debt, not capability. A real observability stack is a direction item, not wave scope. **Important:** `anomaly_detection` is **unaffected** — it reads `time_bucket()` from the **primary Postgres pool**, never a separate Timescale cluster, and survives. **Contrast the full-text leg:** OpenSearch IS wired end-to-end — the `corpus_indexer` analyst indexes every full-body signal into `legba_signals_corpus` and the read tools serve it, so it is a live, load-bearing capability, not deferred infra. |
| **Guard rail** | No stub to guard on the metrics side — those code paths were deleted, not faked: there is no metrics store at all, so nothing writes time-series metrics and nothing can silently pretend to. (No allowlist line for the removed metrics store — there is no symbol.) On the search side, `src/legba/runtime/substrate_query_port.py:PostgresQdrantSubstrateQueryPort.search_signals` uses **Postgres FTS** (`to_tsvector`/`plainto_tsquery`) over structured signal rows — a real result, never a fabricated one — and is independent of the OpenSearch full-body corpus that `search_corpus`/`read_document` serve. |

### 22. Live GATHER actuation of the `web_access` / `propose_facts` tools (S6) — CLOSED (2026-06-19)

| | |
|---|---|
| **Status** | **CLOSED.** The run-path wiring that lets a *running* `inline_target` assessor invoke the S6 external + write-back tools mid-run now ships. The previously-deferred edits landed in the three WF-C run-path files. |
| **What shipped** | (1) `inline_target._GATHER_TOOLS` now spans the read surface **plus** `web_fetch`/`web_search` (`web_access`) and `propose_fact`/`request_source`/`open_question` (`propose_facts`); the GATHER loop ROUTES each tool to the binding for ITS owning pack so `Agency.run_pack_tool` enforces tool↔pack ownership + the per-pack governor (read tools → the `substrate_read` binding; write/web tools → their per-tool binding). (2) `dapr_host` builds the per-pack write/web GATHER bindings — but ONLY for an inline_target assessor that ALSO grants the pack via `action_packs`, and ONLY when the base `substrate_read` GATHER binding is itself wired; `pg_pool` is threaded onto each binding's `ToolContext`. (3) `dapr_actors._gather_write_bindings_for_target` re-points each binding to the running target's `allowed_action_packs` per run and, for the write pack, injects a per-run `WritebackContext` (the run's pg_pool + a fresh per-run `AnalystContext`) **copy-on-write** — it clones the binding + its `ToolContext`, never mutating the shared base (the documented fan-out race). (4) `inline_target._gather_system_suffix` splices the bound packs' operator-authored `prompt_fragments`+`rules` (from `descriptors/action_pack_web_access.yaml` / `action_pack_propose_facts.yaml`) into the GATHER system prompt. |
| **Trust-model constraints it shipped under** | The wired tools are **PROPOSE-grade ONLY** and stay inside the existing three-way agency gate — nothing here bypasses it. `propose_fact` writes `source_type='proposed'` via `write_fact` (never authoritative, never `_insert_fact`); `request_source`/`open_question` write `hypotheses` rows via `write_hypothesis`. **NONE** mutate the control-plane (no source/target/analyst descriptor writes). `web_fetch`/`web_search` egress **only** through `SsrfGuardedTransport`. Every write carries MANDATORY `derived_from` lineage (review S-1 — the assessor's reasoning is driven by untrusted RSS text, so an uncited write is refused). A write/web pack a target does NOT allow is a loud BLOCK at resolution; a write/web tool named with no wired binding is a clean `tool_unbound` no-op folded back to the planner — never an ungoverned call, never dispatched through the read binding. |
| **Guard rail** | Everything still fails loud, nothing fabricates: each write handler returns a `failed` `ToolResult` when `ctx.writeback` is absent (`src/legba/data/analysts/agency/write_tools.py`); the web handlers refuse non-public egress; a granted-but-unbindable write/web pack is FAIL-LOUD at deps build (`dapr_host` returns `None` → activation refuses), mirroring the consult/escalation/substrate_read legs. Exercised end-to-end through the real `Agency.run_pack_tool` in `tests/data_pkg/agency/test_web_and_propose_tools_e2e.py`, and the run-path routing + copy-on-write + propose-with-lineage + unbound/blocked degrade-not-drop paths in `tests/data_pkg/test_analyst_inline_target.py` (SEAM #22 block). (No allowlist line — there is no stub symbol; the handlers and the wiring are real and complete.) |

### 23. Dapr long-activity workflow round-trip (daprd 1.17.9) — RESOLVED for the GEPA optimizer (2026-06-29; = #86)

| | |
|---|---|
| **Status** | **RESOLVED for the GEPA optimizer** (`31473ed` / `c76d44d`). The optimizer's durable **Dapr Workflow** round-trip now completes live on the Dapr backend (`Orchestration completed with status: COMPLETED`). The apparent "long activity won't resume" was diagnosed as a **>4 MB gRPC payload overflow** (`RESOURCE_EXHAUSTED: message larger than max ...`), not purely a daprd 1.17.9 resume bug — the optimizer inlined ~500 training rows into the workflow input. The durable `deep_consult` round-trip **shares the same mechanism** but was **NOT independently re-verified**; the in-process fallback path remains live for it (and for the optimizer) as a safety net. |
| **Fix** | Pass the training set **by reference** rather than inlining it: the workflow input now carries a tiny `TrainingSetRef` and the worker re-fetches the rows via `materialize_training_set` (`optimizer.materialize.ok rows=...` on the live path), so the message no longer crosses the gRPC cap. Belt-and-suspenders gRPC guardrail: the daprd sidecar runs with `-max-body-size ${LEGBA_DAPR_MAX_BODY_SIZE:-16Mi}` (`docker-compose.yml`), the independent supported lever for the HTTP + gRPC limit. The earlier **compile-hang sub-issue stays FIXED** (the bridge LM call had no timeout → infinite hang → no trace → silent death; now per-call + dispatch timeouts, valset cap, real rollouts, and an observable `workflow_timeout` trace). Tracking: `planning/BACKLOG.md` §0/§8 (Dapr long-activity round-trip note). |
| **Guard rail** | NOT a stub — the durable path now succeeds and the fallback still produces a real result observably. Pass-by-reference: `src/legba/runtime/dapr_workflow/gepa.py` (`TrainingSetRef` + `materialize_training_set`), `src/legba/runtime/dapr_workflow/worker.py` / `client.py` (the `-max-body-size` lever + pass-by-reference notes). If the durable round-trip ever fails again, the optimizer still degrades observably to the in-process path (`run_optimizer_in_process`, the `StubWorkflowHandle` non-stub of the allowlist; `optimizer_workflow.in_process` / `.unavailable` signposts, RUNBOOK §4.2) and never fabricates output. The residual open edge is the **un-re-verified deep_consult** durable leg — it shares the fix but has not been independently confirmed on the Dapr backend, so the in-process synchronous path is still its live default. |

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

### 28. System Status panel first in-browser render (UI track)

| | |
|---|---|
| **What** | The `system.status` Dockview panel (`legba-ui-v3/src/panel-registry/registry.ts`, `Component: SystemStatus`) composes per-component / per-layer health — Acquisition (`GET /api/v1/v3/system/source-firing`), Analysis (`GET /api/v1/v3/system/analyst-cadence`), Queues (orphan-filtered `GET /api/v1/v3/streams/consumer_lag`), Infra — into one operator page. Both new backend routes (`v3_api.py`, `build_v3_router`) were confirmed serving live data; the panel is tsc-green and registered. |
| **Why deferred** | At the time of writing the panel was pending its first real in-browser render (the rendered-first eyeball loop, consistent with the broader UI track — `docs/UI_ROADMAP.md`, and #27 for the journal panel). Not a code stub: the panel and its routes are built and live; only the in-browser visual confirmation was outstanding. |
| **Guard rail** | UI-side, no backend symbol involved (the stub scanner does not cover `legba-ui-v3/`). The panel reads only the real read routes (the cadence route reads `analyst_traces`, the firing route reads `signals` + `source_poll_outcomes`); it fabricates nothing — an unrendered panel shows real (or empty) health rows, never invented status. |

### 29. Embedding/cosine semantic tier for contested-claim value clustering (#101 Wave 3 follow-up)

| | |
|---|---|
| **What** | A semantic (embedding + cosine) tier on top of the contested-claims fuzzy value clusterer. Wave 3 of #101 (the contested-claims arbiter) shipped ONLY the cheap **canon + normalized-Levenshtein** tier: `src/legba/data/provenance/value_clustering.py:cluster_values` folds national demonyms / country aliases via `canonicalize_entity`, applies a tiny value-only spelling-variant map (Kyiv/Kiev, Beijing/Peking, …), then single-link clusters by normalized-Levenshtein distance under `FUZZY_MERGE_MAX_DISTANCE = 0.12` (merges "Russia"/"Russian" and "Kyiv"/"Kiev"; keeps "North Korea"/"South Korea" split at the 0.182 floor). A SEMANTIC tier — grouping paraphrases that share NO surface form ("clashes ongoing" vs "fighting continues") via an embedding model + cosine threshold — is the **flagged follow-up** (DECIDED 2026-06-29, `planning/HOLES_B_CONTESTED_CLAIMS_SCOPED_PLAN.md` decision 3). It is not built: the `fact_contention_arbiter` calls `cluster_values` directly and there is no embedding/qdrant code path in either the clusterer or the arbiter. |
| **Why deferred** | The Levenshtein tier already covers the load-bearing transliteration/spacing/hyphen variants the live corpus actually exhibits, where exact-string grouping found 0 same-triple groups. The **L-114** embedder-through-port wiring has since landed (seam #11 resolved — the `HostedEmbeddingClient` is now in hand), so the residual blocker here is NOT the embedder: the semantic tier still needs a provisioned vector store to cluster in AND carries the harder false-merge risk (cosine readily merges near-opposite claims), and no embedding/qdrant code path is wired into the clusterer or the arbiter. Scoping it as a separate flagged cut keeps the detect-only arbiter conservative + provenance-first. |
| **Guard rail** | Nothing half-built to guard — there is no semantic code path, so nothing can silently claim a paraphrase merge it cannot make. `cluster_values` is pure canon+Levenshtein and degrades safely on the no-surface-overlap case: two semantically-equal-but-textually-disjoint values simply land in SEPARATE clusters (each its own group), which the arbiter treats as distinct candidate values — it never fabricates a merge. The decision + threshold rationale live in `src/legba/data/provenance/value_clustering.py` (module docstring) and `planning/HOLES_B_CONTESTED_CLAIMS_SCOPED_PLAN.md`. (No allowlist line — there is no stub symbol; the shipped tier is real and complete, the semantic tier is absent, not faked.) |

### 30. GEPA self-optimizer cadence — SEQUENCED FREEZE (P0-T6 → returns P4)

| | |
|---|---|
| **What** | `descriptors/analyst_country_optimizer.yaml` — the GEPA / DSPy self-optimizer (kind `optimizer`; walks `analyst_traces ⋈ analyst_critiques` and emits a `PromptModuleCandidate` for `country_assessor`) — has its `cadence.fallback_schedule` **NULLED** (`null`, from the prior `"0 3 * * *"` daily compile). This is the SUBTRACT step: an always-on, unmeasured self-optimization monolith is frozen so it can RETURN at **P4** as a MEASURED experiment (a GEPA promotion gated on a real before/after delta), not abandoned. The descriptor stays `state: active` with kind/eval/outputs intact; only the cadence is frozen. |
| **Why deferred** | Sequenced, not deleted. The direction composes the oracle bottom-up — measure + verify + cite BEFORE autonomy — so the autonomy (the optimizer) is held at the TOP of the tower until the floor (verified cited synthesis) exists to optimize against. Self-tuning an unverified producer would optimize the wrong objective. Returns at P4 behind a measured-promotion gate (`planning/PLATFORM_DIRECTION_PLAN_2026-06-30.md`, P0-T6 → P4). |
| **Guard rail** | The freeze is mechanically self-enforcing: the on-activate reminder gate `if schedule:` (`src/legba/runtime/dapr_actors.py`, the `register_reminder(name="run_cadence", …)` block) registers **no** `run_cadence` reminder when `fallback_schedule` is null, so the optimizer fires on no tick — verifiable by the ABSENCE of `dapr_actors.analyst.reminder.registered` for `country_optimizer` in the boot log and a 0 etcd reminder count for its actor type. The liveness watchdog correctly EXCLUDES a null-schedule analyst (`src/legba/runtime/liveness_watchdog.py:_fetch_cadence_rows` — the `coalesce(... ,'') <> ''` filter), so the freeze raises no false silently-dead alarm. **No allowlist line** — the descriptor YAML is outside `src/legba/**` and introduces no scannable stub symbol; this is a declared CADENCE freeze, not a code stub. To restore: set `fallback_schedule` back to `"0 3 * * *"`. |
| **Reconcile (P4-T8, RETURNED AS A SCOPED MEASURED EXPERIMENT)** | The GEPA optimizer RETURNS at P4 ONLY as a NARROW single-unit measured-promotion experiment (the SEPARATE `unit_optimizer` descriptor, P4-T6, `analyzed_analyst_id=leadership_transition`, `fitness_metric=faithfulness`). Promotion is HUMAN-GATED end to end: an operator flips a candidate's `data->>'promotion_gate'` to `'promoted'`, and `optimizer.resolve_promoted_system_prompt` (the LIVE inference path, wired in `analyst_deps_builder`) admits the evolved prompt ONLY when the MEASURED delta is promotable. The single measurement gate is `gepa._delta_gates_ok` — it stamps `data.eval.promotable` at candidate write time (`optimizer.run_method`) and requires a POSITIVE (≥ `min_promote_delta`), FINITE (a `math.isfinite` guard rejects a raw NaN/inf → `non_finite_score`), NON-DEGENERATE, judge-scored, sufficiently-paired eval delta. There is **NO auto-promotion path**: the earlier `should_auto_promote` `auto_with_threshold` helper had ZERO production call sites (no descriptor declares that policy; `run_method`/`OptimizerDeps` carry no DB connection to reach it) and was REMOVED (P4 pre-push review H2/C3) so the honesty suite no longer tests dead code. Guarded by `tests/test_p4t8_honesty_optimizer_promotion.py` (the honesty suite; run `pytest -k p4t8_honesty`), now pinned to the LIVE `_delta_gates_ok` gate — it FAILS if `promotable` is stamped `True` on an absent (`None` mean → `degenerate_or_absent_delta`), degenerate (`eval_degenerate`), judge-unavailable, under-paired, sub-margin, or RAW non-finite/NaN (`non_finite_score`) delta. The **monolithic `country_optimizer` cadence STAYS FROZEN** (`fallback_schedule: null`) — the freeze above is NOT lifted; the return is a different, gated, measured descriptor. Any re-enabled experiment MUST use the by-reference `TrainingSetRef` path (seam #23) + a narrow single-unit scope and MUST keep the Dapr scheduler job count FLAT (the 777k-err/12h reminder-flood class must not regress — a LIVE operational verify, NOT a pytest; the honesty suite does not prove flood-safety). |

### 31. country_predictor forecast-as-claim cadence — SEQUENCED FREEZE (P0-T6 → returns P4)

| | |
|---|---|
| **What** | `descriptors/analyst_country_predictor.yaml` — the per-G20 event-tempo `predictor` (AutoARIMA + conformal CI, stat-only). P0-T6 NULLED its `cadence.fallback_schedule`, but **that was INSUFFICIENT** and it was NOT frozen: it carries `subscription.targets` (`has_tag("g20")` signals) + `state: active`, so it kept firing **REACTIVELY** on signal accumulation (~1/hr live `prediction` rows, not trace-only — confirmed 2026-07-01). **P3-T8 COMPLETES the freeze:** the descriptor is **removed from `scripts/bringup_register_analysts.py` `ANALYST_FILES`** (so bringup can't re-activate it) **AND the live head is `retired`** (`POST /retire`), so the runtime wires no per-target workers (boot log: `NOOP … already_retired_or_absent`) and it emits 0 rows. A numeric forecast is a CLAIM; forecasting RETURNS at **P4-T7** as a precise-question **Brier/BSS scoreboard** — a different design, not this reactive producer. |
| **Why deferred** | Sequenced, not deleted. Per the rule (measure + verify before a claim becomes product), an unscored forecast cannot be a producer in the cited-synthesis spine. Returns at P4-T7 once the scoring loop (Brier / calibration over resolved outcomes) exists (`planning/PLATFORM_DIRECTION_PLAN_2026-06-30.md`, P0-T6 → P4). |
| **Guard rail** | **LESSON: nulling the cadence does NOT freeze a REACTIVE (target-subscribed) analyst** — only a cadence-only analyst (cf. seam #30 GEPA, which IS silent). A reactive analyst must be `retired` (or its `subscription.targets` removed) to stop firing. Now enforced by retirement: a retired head is not reconciled (runtime NOOPs `already_retired_or_absent`), and its absence from `ANALYST_FILES` means a fresh bringup won't re-create it. Verify: 0 new `country_predictor` rows after a runtime recreate; state `retired` in `analyst_descriptors`. **No allowlist line** — declared freeze via lifecycle state, not a code stub. To restore the OLD leg: un-retire + re-add to `ANALYST_FILES` + re-register (but P4-T7 supersedes it with the scoreboard design). |
| **Reconcile (P4-T8, RETURNED AS A SCOPED MEASURED EXPERIMENT)** | Forecasting RETURNS at P4-T7 ONLY as the `acute_forecasts` Brier/BSS scoreboard, surfaced SOLELY on `GET /api/v1/v3/eval/calibration` (`CalibrationScoreboard`) — NEVER as a free-text claim/finding on a trust surface. The degenerate-batch path ABSTAINS (`forecast_acute.p_vector_is_degenerate` → `forecast_acute.abstain_degenerate`, zero rows, forecast_acute.py ~L344-359); the pilot reports skill in its OWN segregated keys, tagged `forecast_pilot_degenerate` and WITHHELD (`brier_forecast_acute=None`, `forecast_unproven=True`, `calibration_thin=True`) until READY **and** NON-DEGENERATE **and** BSS>0. Guarded by `tests/data_pkg/test_p4t8_honesty_forecast_skill.py` (run `pytest -k p4t8_honesty`), which FAILS if a degenerate / under-sample / thin-exogenous pilot surfaces bare positive skill (no `forecast_skill_positive` tag; the route reads `forecast_unproven`/`calibration_thin`). The `country_predictor` descriptor STAYS retired (this seam) — the scoreboard supersedes the reactive producer; a DIFFERENT design, NOT a lift of the freeze. |

### 32. india_energy_predictor forecast-as-claim cadence — SEQUENCED FREEZE (P0-T6 → returns P4)

| | |
|---|---|
| **What** | `descriptors/analyst_india_energy_predictor.yaml` — the india_energy_infra event-tempo `predictor` (the sibling forecast-as-claim analyst) — has its `cadence.fallback_schedule` **NULLED** (`null`, from the prior `"*/30 * * * *"` every-30-min). Frozen alongside `country_predictor` (seam #31) for the same reason: a forecast is a CLAIM that must be SCORED before it ships. Returns at **P4** behind the same Brier scoreboard. The descriptor stays `state: active` with kind/outputs intact; only the cadence is frozen. |
| **Why deferred** | Sequenced, not deleted — identical rationale to seam #31 (: measure + verify before a forecast becomes product). Returns at P4 with the predictor scoring loop (`planning/PLATFORM_DIRECTION_PLAN_2026-06-30.md`, P0-T6 → P4). |
| **Guard rail** | Mechanically self-enforcing as seams #30/#31: the `if schedule:` gate registers no `run_cadence` reminder for a null schedule (verify: no `reminder.registered` for `india_energy_predictor`; 0 etcd reminders for its actor type). Liveness watchdog excludes it (null-schedule filter). **No allowlist line** — YAML outside `src/legba/**`, no scannable stub symbol; declared CADENCE freeze, not a code stub. To restore: set `fallback_schedule` back to `"*/30 * * * *"`. |
| **Reconcile (P4-T8, RETURNED AS A SCOPED MEASURED EXPERIMENT)** | Identical rationale to seam #31: forecasting RETURNS at P4-T7 ONLY as the segregated `acute_forecasts` Brier/BSS scoreboard on `GET /api/v1/v3/eval/calibration` (`CalibrationScoreboard`), NEVER as a free-text forecast claim/finding; the degenerate-vector path ABSTAINS (`forecast_acute.abstain_degenerate`, zero rows) and skill is WITHHELD (`brier_forecast_acute=None` / `forecast_unproven=True`) until ready AND non-degenerate AND BSS>0 — guarded by `tests/data_pkg/test_p4t8_honesty_forecast_skill.py` (`pytest -k p4t8_honesty`). The `india_energy_predictor` cadence STAYS null-frozen — the scoreboard supersedes the reactive producer; a DIFFERENT design, NOT a lift of the freeze. |

### 33. journal_assessor cadence — SEQUENCED FREEZE (P0-T6 → returns as introspection/observability)

| | |
|---|---|
| **What** | `descriptors/analyst_journal_assessor.yaml` — the journal's first-person reflective voice (the 11th `OutputKind`, kind `journal_assessor`, writes only `journal_entries` off the fact/finding/nexus chain) — has its `cadence.fallback_schedule` **NULLED** (`null`, from the prior `"0 0,12 * * *"` every-12h entry tier). SUBTRACT freezes its AUTONOMOUS cadence so the journal RETURNS only as **introspection / observability** (operator- or manual-triggered self-narration), NOT as an always-on producer in the cited-synthesis spine. The descriptor stays `state: active` with kind/persona/packs intact; only the cadence is frozen. |
| **Why deferred** | Sequenced, not deleted. The journal narrates a voice-bearing point of view OVER the organism; in the order that introspective voice is observability, not a spine producer — it returns on demand (operator trigger) rather than on a 12h tick (`planning/PLATFORM_DIRECTION_PLAN_2026-06-30.md`, P0-T6). The journal already NEVER writes a fact/finding/nexus, so freezing its cadence removes only the autonomous tick, not a verified output leg. |
| **Guard rail** | Mechanically self-enforcing as seams #30–#32: the `if schedule:` gate registers no `run_cadence` reminder for a null schedule, so no entry tick fires (verify: no `reminder.registered` for `journal_assessor`; 0 etcd reminders for its actor type). Liveness watchdog excludes it (null-schedule filter). **No allowlist line** — YAML outside `src/legba/**`, no scannable stub symbol; declared CADENCE freeze, not a code stub. To restore: set `fallback_schedule` back to `"0 0,12 * * *"`. |
| **Reconcile (UNFROZEN 2026-07-01, operator decision)** | This freeze is REVERSED: `descriptors/analyst_journal_assessor.yaml` `cadence.fallback_schedule` is set back to `"0 0,12 * * *"` (the 12h entry tier) alongside the daily consolidator tier — the journal RETURNS as an autonomous producer (still OFF the fact/finding/nexus chain; it writes only `journal_entries`). This is the journal_assessor revival (task #100). So the SUBTRACT above is historical: the cadence is LIVE again, not null. |

### 34. world-assessment verdict-banner FRAMING — UI FREEZE (P0-T7 → composed/verified world view returns P3)

| | |
|---|---|
| **What** | The v4 UI presented the `world_assessor` one-pager as a monolithic **headline world VERDICT** — the verdict-from-nowhere framing this direction exists to kill. SUBTRACT **demotes the FRAMING** (UI only): `legba-ui-v3/src/v4/why/WorldAssessment.tsx` world-mode header scope label changed `World assessment` → `world_assessor finding · one producer` plus an honest sub-caption ("one analyst's read, not a global verdict; the composed, verified world view returns at P3"); the dormant strip `legba-ui-v3/src/v4/components/AssessmentBanner.tsx` relabeled `World assessment` → `world_assessor finding`. The data path, the component, and per-country (`country_assessor`) reads are **unchanged** — world_assessor findings stay fully reachable. **This is a UI framing change ONLY: the `world_assessor` descriptor and its cadence are NOT touched — it KEEPS its tick as a PRODUCER.** |
| **Why deferred** | Sequenced, not deleted. Per the order (per-country reads → cited synthesis → verify → provenance, then compose UP), a single producer's narrative cannot be presented as THE global world verdict before the verified composition exists. The world-assessment view RETURNS at **P3** as a verified composition OVER per-country assessments (`planning/PLATFORM_DIRECTION_PLAN_2026-06-30.md`, P0-T7 → P3). Until then the UI labels the one-pager honestly as one producer's finding. |
| **Guard rail** | UI-side, no backend symbol involved (the stub scanner does not cover `legba-ui-v3/`). The relabel is text/markup only — no new props/types, so the typecheck (`docker compose build legba-ui-build`) is the gate. The standalone `v4.assessment` panel is already `hidden` in the registry ("World Assessment is a FINDING → shown in Inspector"); `AssessmentBanner` remains imported nowhere (a dormant strip — relabeled defensively for if/when it mounts). **No allowlist line** — UI framing freeze, not a code stub; nothing fabricated, world_assessor output stays reachable. To restore the verdict framing: revert the two label strings (and remove the `world-assessment-framing` caption). |
| **Reconcile (LANDED at P3 2026-07-01)** | The deferred world view has RETURNED: `world_assessor` graduated into the P3 world COMPOSITION — a verified read OVER the per-country `country_composition` reads (see seam #35) — and the UI now renders it (`WorldAssessment.tsx` fetches `country_composition` per country + `world_assessor` for the world view; the stale "returns at P3" caption is removed). So the "one producer's finding / not a global verdict" framing above is SUPERSEDED: the composed, verified world view IS the surface now. The `AssessmentBanner` strip is still unmounted (its stale caption is a dormant-only residue). |

---

### 35. country_assessor monolithic framing — DEMOTED to feeder (P2-T8 → composed world view returns P3)

| | |
|---|---|
| **What** | The per-country read surface presented the monolithic `country_assessor` one-pager as **THE country verdict** (`legba-ui-v3/src/v4/why/WorldAssessment.tsx`, country mode). P2-T8 **demotes the FRAMING**: the bounded reasoning UNITS (at P2-T8 the four `leadership_transition`, `energy_security`, `escalation`, `narrative_coordination`; SINCE EXPANDED to SEVEN — adding `internal_stability`, `military_posture`, `economic_coercion`) — each a single cited + faithfulness-verified + measured read carrying its P2-T6 eval badge — are now the headline product surface (`legba-ui-v3/src/v4/why/CountryUnitsAssessment.tsx`); the `country_assessor` synthesis is rendered BELOW inside a collapsible "Full synthesis · feeder" `<details>`. **The `country_assessor` descriptor, cadence, and output are UNCHANGED — it KEEPS its tick as a producer/feeder and stays fully reachable via search (`analyst_id` filter) and the Inspector.** UI framing change only. |
| **Why deferred** | Sequenced, not deleted. Per the order (decompose + measure the small units BEFORE composing up), a monolithic country verdict cannot headline once the bounded, individually-measured units exist — the units ARE the mid-floor product. A verified composition OVER the units (the country/world verdict re-assembled and checked) returns at **P3** (`planning/PLATFORM_DIRECTION_PLAN_2026-06-30.md`, P2-T8 → P3). Until then the monolith is an honest feeder, not the headline. |
| **Guard rail** | UI-side; no backend symbol (the stub scanner does not cover `legba-ui-v3/`). The units it surfaces are REAL + measured (P2-T2 cited/verified/gated + P2-T5/T6 eval), so nothing is fabricated and `country_assessor` output is never hidden — only reframed. Typecheck (`docker compose build legba-ui-build`) is the gate. **No allowlist line** — UI framing demotion, not a code stub. To restore the monolith-as-headline: render the `country_assessor` one-pager first again in WorldAssessment's country branch. |
| **Reconcile (RETIRED 2026-07-01, operator decision)** | The demotion above is now SUPERSEDED: `country_assessor` is **RETIRED**, not merely demoted — live head `state='retired'` (`POST /retire`) + removed from `scripts/bringup_register_analysts.py` `ANALYST_FILES`. Rationale: NOTHING in the trusted spine reads it (`country_composition` reads the SEVEN UNITS), yet as a still-firing feeder it was the single largest producer (~150 findings/48h) of UNVERIFIED monolithic output — exactly the verdict-from-nowhere pollution the units + composition supersede. Because it is REACTIVE (`subscription.targets` `has_tag("g20")`), nulling its cadence is insufficient (cf. seam #31) — retirement at the lifecycle level (runtime NOOPs `already_retired_or_absent`) + bringup removal is what actually stops it. `world_assessor` (the world composition) is NOT affected — it graduated into the P3 composition. To restore: un-retire + re-add to `ANALYST_FILES` + re-register. |

### 36. External alert delivery is BUS-ONLY (no paged-human edge)

| | |
|---|---|
| **What** | The alert rewire is real and live: `severity` is a first-class READ COLUMN (`analyst_outputs.severity`, indexed on `high`/`critical`), and the `escalate_finding` action pack (`descriptors/action_pack_escalate.yaml`) fires on the POST-VERIFY alert score (`effective_confidence × per-severity weight`) crossing the gate — a verify-DEMOTED finding no longer alerts on a high-severity tag alone. What is NOT built is an EXTERNAL human-facing delivery edge. On dispatch the pack's channel emitter publishes the escalation onto the NATS subject `channels.escalations` (bus-only) and DURABLY audits each delivery — a per-delivery row in `alert_sink_deliveries` (repurposed 2026-07-03 from the retired `alert` output-kind path: channel, target, finding/output id, severity, effective_confidence, outcome), alongside the `action_pack_invocations` / `governor_events` trail — so "who was alerted, with what confidence" is answerable after the fact. What is still NOT built is a paged-human edge: there is NO pager / webhook / email / SMS sink wired — a consumer must tail the bus. |
| **Why deferred** | The delivery-edge choice (which pager / webhook / on-call system) is an operator/infra decision, not wave-scope code. The internal alert MECHANICS (severity column, verify-folded gate, agency-governed dispatch) are the load-bearing part and are complete; wiring a concrete external sink is the deferred leg. (`system.alert_center` in the UI is likewise a client-only view today — RELEASE_STATE_MATRIX §2.) |
| **Guard rail** | NOT a stub — nothing is fabricated; the escalation is durably published to the bus and audited. There is simply no external-sink code path, so nothing can silently claim a human was paged. (No allowlist line — no stub symbol; the bus publish is real, the external edge is absent, not faked.) |

### 37. UCDP GED source — registered but live poll UNAUTHORIZED (paused pending access token)

| | |
|---|---|
| **What** | `descriptors/source_ucdp_ged.yaml` (`source.ucdp.ged`, handler `src/legba/data/sources/ucdp.py:UCDPSourceHandler`) is built, unit-tested against a fixture, and registered `state: active`. The descriptor was authored assuming the GED API is public / no-auth (like the USGS feed), but the LIVE poll on this rig returns **HTTP 401 Unauthorized** (`https://ucdpapi.pcr.uu.se/api/gedevents/24.1?…`) → **0 signals ingested**. So the source is effectively PAUSED pending a provisioned access credential/token, even though the descriptor head reads active. |
| **Why deferred** | The GED endpoint now gates access (an access token / credential this deployment does not hold). Obtaining + provisioning that credential is an operator/infra step, not wave-scope code. The handler is complete; only the live upstream access is unprovisioned (the same shape as the TAXII destination in #10 and the backup offsite in #18). To gate it cleanly until then, flip `identity.state` to `draft` (a draft descriptor registers but wires no live actor). |
| **Guard rail** | NOT a stub — the handler never fabricates events. An unauthorized/failed poll lands a loud `error` row in `source_poll_outcomes` (`401 Unauthorized`, `signals_written = 0`) and writes ZERO signals; it never invents a conflict event. The sibling WATCH-desk state-media sources added alongside it (`source.irna.english` / `source.presstv.english` / `source.ukrinform.english`) ARE firing live. (No allowlist line — no stub symbol; the handler is real, the upstream access is unprovisioned.) |

### 38. Manual ingestion lanes — built + pytest-covered, not yet exercised against a live production ingest

| | |
|---|---|
| **What** | The manual-ingest lanes — `src/legba/data/seed/manual_batch.py` + `manual_schema.py` (structured facts/nexuses/entities/signals/docs batches; format spec `docs/MANUAL_INGEST_FORMAT.md`; drivers `scripts/manual_ingest.py`) and `src/legba/data/rag/lane4_loader.py` (the vector-corpus loader behind `scripts/manual_ingest_vectors.py`) — are real and complete, with substantial pytest coverage (`tests/data_pkg/test_manual_batch.py`, `test_manual_schema.py`, `test_lane4_vector_loader.py`, plus the batch fixtures). The Lane-4 vector loader HAS been run to populate the live `tradecraft`/`world_context` corpora (SEAMS #20). What is honestly UNPROVEN is a full end-to-end manual STRUCTURED batch ingest against the live registry/substrate in production — the coverage is pytest-level (real pool in tests), not a live-operated production run. |
| **Why deferred** | These are operator lanes exercised on demand; a standing production run is an operator step, not wave-scope code. Documenting the pytest-only status honestly (rather than implying a proven live pipeline) is the point of this row. |
| **Guard rail** | NOT a stub — every lane validates its input schema and FAILS LOUD on a malformed batch (see the `manual_batch_bad` fixture), and every write carries provenance/lineage; a bad batch is rejected, never partially fabricated into the substrate. (No allowlist line — no stub symbol; the code is real and complete, only a live production run is outstanding.) |

### 39. Non-Latin / telegram NER re-enrichment — RESOLVED (backfill drained)

| | |
|---|---|
| **What** | *(Historical seam, closed.)* The NER enrichment fixes were originally **forward-only**, leaving ~9k already-ingested telegram / non-Latin signals with empty entities. The `reenrich_ner` backfill has since drained that backlog in place (~10k signals re-enriched, idempotent per-signal marker), so the forward pipeline and the archive share the same enrichment floor. |
| **Why kept** | Retained as a record that the seam existed and how it closed; the forward path (telegram body NER + NLLB pre-translation for Arabic / Russian / Ukrainian) is unchanged and live. |
| **Guard rail** | None needed — the backfill wrote real extractions; a signal it could not enrich stays an honest empty. |

### 40. 2026-07-06 audit remediation — deferred follow-ups (declared backlog)

| | |
|---|---|
| **What** | The 2026-07-06 audit remediation (migrations 0076–0080, ~18 fixes) closed its headline data-quality defects — `cross_correlator` now ENTERS the mandatory faithfulness verify pass (confidence clamped by `min(conf, faith)`) and reads the LIVE composition/unit layer (was escaping verify + degrading against the retired `country_assessor`/`country_predictor`); state/social-media `source_credibility` is SEEDED below the ingestion nominal (presstv 0.25 / irna 0.30 / ukrinform 0.45 / telegram 0.30 — was NULL, so state outlets out-credited their peers); and the entity write-path is now alias/article-aware + class-guarded (closing the post-P4 re-fragmentation where "the Strait of Hormuz" forked from "Strait of Hormuz"). Those are FIXED, not open seams. The residual **deferred follow-ups** are declared here so they are not lost: (a) a **broader nexus historical-junk sweep** (migration 0078 canonicalized forward + swept a first pass; a wider historical pass over older dyads remains); (b) an **M15 city/leader gazetteer** to back the target-consistency + stale-leader verify guards with a real place/officeholder table (today those guards are heuristic); (c) a **cross_correlator claim-discriminator inside `situation_signature`** (so two correlations that share a signature but assert DIFFERENT claims do not collapse under supersession); (d) a **proactive Anthropic balance / spend monitor** (the billed consult/deep_consult Opus plane can run dry — an outage today surfaces only as a graceful HTTP 503 naming the other plane, not a pre-emptive low-balance alert). |
| **Why deferred** | Each is a scoped enhancement on top of a fix that already landed and is honest as shipped — the guards/gates work heuristically now; the gazetteer, the wider historical sweep, the signature discriminator, and the balance monitor are refinements, not corrections of a fabrication. They are operator/backlog items, not wave-scope code. |
| **Guard rail** | Nothing half-built to guard — none of these has an in-tree code path that could silently fabricate. The shipped guards degrade honestly without them: the M13 stale-leader / M15 target-consistency verify guards DEMOTE (never delete) via `effective_confidence = min(confidence, faithfulness)`; cross_correlator supersession keys off a stable `situation_signature` and a blind_spot head decays only when its scope is revisited; and a billed-plane outage FAILS CLOSED to a graceful 503 rather than silently billing or fabricating. (No allowlist line — no stub symbols; these are declared backlog, not stubbed code.) |

### 41. Action-pack staleness — WARN-only rider, no live eviction

| | |
|---|---|
| **What** | A periodic (5-min) sweep compares each registered action-pack descriptor's content hash against the version an analyst's dependencies were built from and logs a loud WARNING when they drift (the pack was PUT after the analyst's deps were assembled). What is NOT built: **eviction/rebuild** — a stale pack binding is *reported*, not automatically refreshed; the analyst keeps its already-built toolset until its next natural deps rebuild (restart or descriptor change). |
| **Why deferred** | Safe eviction needs a pack→analyst reverse index (which analysts hold which pack version) so a refresh can be targeted; without it a blanket rebuild would churn every live analyst on any pack PUT. The warning makes the drift visible instead of silent, which was the incident class being fixed. |
| **Guard rail** | Warn-only by construction — the rider never mutates a binding, so it cannot half-apply. The drift window is bounded by the next runtime recreate (the standing deploy step), and the WARNING names the pack + analyst so an operator can force the rebuild deliberately. |

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
