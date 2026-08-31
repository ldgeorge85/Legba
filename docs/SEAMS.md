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

**RESOLVED 2026-06-09 (decision F-1).** Re-verified 2026-07-28 (C5 hygiene
pass) — compacted to the [Resolved seams appendix](#resolved-seams-compact-appendix);
see there for the verification evidence and detail.

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
| **Why deferred** | No operator-confirmed TAXII server target exists yet. The transport is finished; only the destination is unprovisioned. Note the companion fact (`docs/DIRECTION.md` §3): the two descriptors that declared `outputs.stix_bundle` bindings are retired/frozen, so **no active analyst currently emits a bundle** — the STIX leg is dormant end-to-end until an emitter is re-bound; the markdown/JSON report-export surface is the nearer product direction. Broader STIX/TAXII direction: see `docs/DIRECTION.md`. |
| **Guard rail** | `src/legba/data/outputs/taxii_client.py:push_bundle_to_taxii` / `upload_bundle_to_taxii` — raise `TaxiiServerNotConfiguredError` (a `RuntimeError`, NOT a stub) when asked to push with no `server_url`, no HTTP client, or a cleartext non-loopback host. An un-provisioned/half-configured `taxii` binding refuses loudly; it never fabricates a delivery or silently drops the TLP-marked bundle. (No allowlist line — the code fails loud, it does not stub output.) |

### 11. Consult `vector_search` embedder wiring — RESOLVED (L-114, 2026-07-02)

**RESOLVED 2026-07-02 (L-114 / S5-T1).** Re-verified 2026-07-28 (C5 hygiene
pass) — compacted to the [Resolved seams appendix](#resolved-seams-compact-appendix).
Note: SEAMS #20 (the Tier-2 `vector:world_context` grounding pilot) shares
this embedder-through-port wiring but is **NOT** resolved/compacted — it
stays open below as an active guarded pilot with honest residual risk.

### 12. Optimizer parent-prompt loading degradation

| | |
|---|---|
| **What** | `_load_parent_prompt_text` falls back to a clearly-marked `<<no prompt text found ...>>` marker string when a prompt module imports but has an **unexpected shape** (no `build()`, no dspy-module-shaped attribute). |
| **Why deferred** | Graceful degradation by design: the GEPA loop computes a delta against the marker instead of crashing; the marker is visible in the optimizer's output, never passed off as a real prompt. |
| **Guard rail** | `src/legba/runtime/dapr_workflow/gepa.py:_load_parent_prompt_text` — the marker text is unmistakable in any audit of optimizer runs. (Moved from the deleted `runtime/temporal/` package by C-3.) |
| **NARROWED by K-3 (2026-08-03)** | The `<<missing prompt module: ...>>` half of this seam is **retired**. A module that cannot be IMPORTED is a dead reference, not a shape surprise, and the old behaviour was the worst available: return the marker at `logger.debug` (invisible in production) and then hand it to GEPA as the parent text, so a promoted candidate could be a mutation of a placeholder that becomes a live analyst's system prompt. Import failure now raises `PromptModuleImportError`. The seam above covers only the shape case it was written for. |

### 13. Non-text modality renderers (UI)

| | |
|---|---|
| **What** | UI renderers for image / audio / video / structured (incl. `application/geo+json`) signal modalities. Only the `text` renderer is `implemented: true` in `MODALITY_RENDERERS` (`legba-ui-v3/src/lib/modalityRenderers.tsx`); audio / video / image / structured / binary are all `implemented: false` and fall through to the generic raw-payload view with a "pending" badge — never a fabricated preview. (The live geospatial map is a SEPARATE v4 Dockview panel — NOT a `MODALITY_RENDERERS` entry. Its DEFAULT world map is now the **maplibre-gl** banded-verdict CHOROPLETH (`legba-ui-v3/src/v4/world/MapLibreWorldMap.tsx`), with **Leaflet** as the `hasWebGL`-false FALLBACK selected by `legba-ui-v3/src/lib/mapEngine.ts`, plus the `TileWebGLOverlay` tile-overlay harness (`legba-ui-v3/src/components/TileWebGLOverlay.tsx`) — no longer Leaflet-only. geojson sources still ingest fine via `src/legba/data/sources/geojson.py`.) |
| **Why deferred** | UI track work (see `docs/UI_ROADMAP.md`); blocked on the media-extraction seam (#1) producing real derived content to render. |
| **Guard rail** | UI-side: unknown modalities fall through to the generic payload view — no backend symbol involved (the scanner does not cover `legba-ui-v3/`). |

### 14. RBAC / STIX direction / MCP surface / multi-tenancy

| | |
|---|---|
| **What** | Platform-level direction items: role-based access control, the fuller STIX/TAXII story (beyond seam #10), and real multi-tenant isolation (today `tenant_id` is stamped through envelopes/ledgers but there is one operating tenant). *(2026-07 update: the MCP surface expansion formerly listed here is now BUILT — seven built-in substrate tools serve a standalone `legba-mcp` process, fixing the standalone-empty catalog; `docs/DIRECTION.md` §4 carries the honest residuals — stdio-only transport, registry must be reachable, descriptor-declared tools still empty standalone.)* |
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
| **What shipped** | (1) The two Lane-4 corpora were chunked + embedded into Qdrant via `src/legba/data/rag/lane4_loader.py` + `chunker.py` (populated live: 293 / 1,716 points). (2) Tier-1 (the structured path) stays real and live: `src/legba/runtime/grounding.py:SubstrateGroundingResolver` reads CURRENT authoritative `facts`/`nexuses` (temporal-honesty gate `superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`, curated/seed-preferred) and `build_grounding_preamble` injects them — now opted in on all NINE bounded units + the journal tiers. (3) Tier-2 is now WIRED: the resolver embeds the target/slice query through the port embedder and semantic-searches `world_context` with a RELEVANCE FLOOR + COUNTRY FILTER, DEGRADE-not-drop when the corpus returns nothing (an empty/low-score corpus read → no preamble, never a fabricated brief). (4) RAG is now run as a GUARDED, MEASURED PILOT on **`internal_stability` ONLY** — `leadership_transition` RAG is **OFF** (the 2026-07-03 rollback is now live, DB-confirmed); the other units carry `sources: [substrate, situations, graph_structure]` (Tier-1 only). (See the **Recalibration** + **Residual** rows below for the pilot state, the auto-rollback guard, and the honest tail-risk.) (5) The consult/GATHER surface reads the same corpora via the live `search_context` tool (one of the 19 `substrate_read` pack tools). |
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

**CLOSED 2026-06-19.** Re-verified 2026-07-28 (C5 hygiene pass) — compacted
to the [Resolved seams appendix](#resolved-seams-compact-appendix).

### 23. Dapr long-activity workflow round-trip (daprd 1.17.9) — RESOLVED for the GEPA optimizer (2026-06-29; = #86)

**RESOLVED for the GEPA optimizer, 2026-06-29** (`31473ed` / `c76d44d`).
Re-verified 2026-07-28 (C5 hygiene pass) — compacted to the [Resolved seams
appendix](#resolved-seams-compact-appendix). **Residual, carried forward**:
the `deep_consult` durable leg shares the same fix but was NOT independently
re-verified on the Dapr backend — its in-process synchronous fallback stays
the live default.

### 24. `nlp_client` boot-singleton cannot re-resolve — RESOLVED (2026-06, #91 / `985db7e`)

**RESOLVED 2026-06 (#91 / `985db7e`).** Re-verified 2026-07-28 (C5 hygiene
pass) — compacted to the [Resolved seams appendix](#resolved-seams-compact-appendix).

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

### 33. journal_assessor cadence — UNFROZEN, runs live on cadence (historical freeze REVERSED 2026-07-01)

| | |
|---|---|
| **Current reality** | The journal (the 11th `OutputKind`, kind `journal_assessor`, writes only `journal_entries` off the fact/finding/nexus chain) **runs live on cadence today**: `descriptors/analyst_journal_assessor.yaml` `cadence.fallback_schedule = "0 0,12 * * *"` (every 12h, entry tier) plus `descriptors/analyst_journal_consolidator.yaml` at `"0 2 * * *"` (daily consolidation tier) — both verified non-null in the live descriptor YAMLs. It is NOT frozen. (Corrected 2026-07-28, C5 hygiene pass — the entry below previously led with the historical NULLED state, which read as current and contradicted reality.) |
| **History** | P0-T6 originally SUBTRACTED the autonomous cadence (nulled `fallback_schedule`, from the prior `"0 0,12 * * *"`) so the journal would return only as operator-triggered introspection/observability, not an always-on spine producer. That freeze was **REVERSED on 2026-07-01** (operator decision, the journal_assessor revival, task #100): the cadence was set back to live and the journal has run as an autonomous producer since, still OFF the fact/finding/nexus chain (it writes only `journal_entries`, never a fact/finding/nexus). |
| **Guard rail** | Unchanged mechanism, now running the OTHER direction: the `if schedule:` reminder gate (`src/legba/runtime/dapr_actors.py`) registers a `run_cadence` reminder BECAUSE `fallback_schedule` is non-null — verify via `reminder.registered` for `journal_assessor` / `journal_consolidator` in the boot log and a non-zero etcd reminder count for the actor type. **No allowlist line** — YAML outside `src/legba/**`, no scannable stub symbol; this was a declared CADENCE freeze, now reversed, not a code stub. To re-freeze: null `fallback_schedule` again on both descriptors. |

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
| **Reconcile (RETIRED 2026-07-01, operator decision)** | The demotion above is now SUPERSEDED: `country_assessor` is **RETIRED**, not merely demoted — live head `state='retired'` (`POST /retire`) + removed from `scripts/bringup_register_analysts.py` `ANALYST_FILES`. Rationale: NOTHING in the trusted spine reads it (`country_composition` reads the seven broad UNITS, plus `proliferation_watch` on the nuclear-relevant desks), yet as a still-firing feeder it was the single largest producer (~150 findings/48h) of UNVERIFIED monolithic output — exactly the verdict-from-nowhere pollution the units + composition supersede. Because it is REACTIVE (`subscription.targets` `has_tag("g20")`), nulling its cadence is insufficient (cf. seam #31) — retirement at the lifecycle level (runtime NOOPs `already_retired_or_absent`) + bringup removal is what actually stops it. `world_assessor` (the world composition) is NOT affected — it graduated into the P3 composition. To restore: un-retire + re-add to `ANALYST_FILES` + re-register. |

### 36. External alert delivery is BUS-ONLY (no paged-human edge)

| | |
|---|---|
| **What** | The alert rewire is real and live: `severity` is a first-class READ COLUMN (`analyst_outputs.severity`, indexed on `high`/`critical`), and the `escalate_finding` action pack (`descriptors/action_pack_escalate.yaml`) fires on the POST-VERIFY alert score (`effective_confidence × per-severity weight`) crossing the gate — a verify-DEMOTED finding no longer alerts on a high-severity tag alone. What is NOT built is an EXTERNAL human-facing delivery edge. On dispatch the pack's channel emitter publishes the escalation onto the NATS subject `channels.escalations` (bus-only) and DURABLY audits each delivery — a per-delivery row in `alert_sink_deliveries` (repurposed 2026-07-03 from the retired `alert` output-kind path: channel, target, finding/output id, severity, effective_confidence, outcome), alongside the `action_pack_invocations` / `governor_events` trail — so "who was alerted, with what confidence" is answerable after the fact. What is still NOT built is a paged-human edge: there is NO pager / webhook / email / SMS sink wired — a consumer must tail the bus. |
| **Why deferred** | The delivery-edge choice (which pager / webhook / on-call system) is an operator/infra decision, not wave-scope code. The internal alert MECHANICS (severity column, verify-folded gate, agency-governed dispatch) are the load-bearing part and are complete; wiring a concrete external sink is the deferred leg. (`system.alert_center` in the UI is likewise a client-only view today — RELEASE_STATE_MATRIX §2.) |
| **Guard rail** | NOT a stub — nothing is fabricated; the escalation is durably published to the bus and audited. There is simply no external-sink code path, so nothing can silently claim a human was paged. (No allowlist line — no stub symbol; the bus publish is real, the external edge is absent, not faked.) |
| **Reconcile (RESOLVED 2026-07-28 — the sink plane landed)** | The external delivery edge is now **BUILT**: a modular alert-sink plane (`src/legba/data/alerts/` — `AlertSink` protocol + registry + `AlertSinkDispatcher`) fans each escalation / global-stall / trigger-scan alert to registered sinks — a generic webhook sink (`LEGBA_ALERT_WEBHOOK_URL`) and a native ntfy push sink (`LEGBA_ALERT_NTFY_URL`) — with one durable `alert_sink_deliveries` row per outcome, per-alert idempotency, and a per-sink cooldown whose suppressed alerts **coalesce onto the next send** (never silently thinned). Payloads carry the verification posture + a receipt link. The **code default remains no outward delivery**: with both URLs unset, every alert is a ledgered `skipped_unconfigured` (visible, never silent) and the NATS subject stays the always-on bus edge — so what remains of this seam is an operator config decision, not a code gap. An unconfigured sink drops out of fan-out only when a configured sibling exists (the no-sinks visibility guarantee holds). |

### 37. UCDP GED source — CODE RESOLVED (token auth landed); source RETIRED pending an operator-held credential

| | |
|---|---|
| **What** | `descriptors/source_ucdp_ged.yaml` (`source.ucdp.ged`, handler `src/legba/data/sources/ucdp.py:UCDPSourceHandler`). The prior entry said the source is "registered `state: active`" and "effectively PAUSED"; both are now FALSE. Live head is **`state='retired'`** (2026-07-28), and the in-tree descriptor ships `state: draft` — a draft descriptor registers in bulk but wires no live actor. |
| **The whole history (2026-08-04 re-verification)** | A review re-raised this as "401 on every poll since registration". The live record says otherwise: registered `active` 2026-07-03 01:56 UTC → **exactly one** poll, 04:00 UTC, `401 Unauthorized` → paused 07:33 → **token auth landed in the handler at 07:36**, three minutes later → retired 2026-07-28. `source_poll_outcomes` holds **one row, ever**, for this source; signals **0, ever**. The 401 is not reproducible by any code still in the tree. |
| **What changed in code** | UCDP introduced a free, registration-gated access token. The handler now sends it as `x-ucdp-access-token` (vault ref `source.ucdp.access_token`, env fallback `LEGBA_UCDP_ACCESS_TOKEN`) and, with **no** token resolvable, **skips the pull entirely** — no HTTP, no 401 spam — recording `ucdp: no token configured`. So a token-less rig stays quiet and healthy by construction rather than by having been paused. |
| **What remains (operator, not code)** | Request a token at <https://ucdp.uu.se/apidocs/>, store it as vault secret `source.ucdp.access_token`, re-register this descriptor, and transition `draft → configured → active` (exact calls in the descriptor's OPERATOR FLIP header). Same shape as the TAXII destination in #10 and the backup offsite in #18: complete code, unprovisioned external credential. |
| **Guard rail** | NOT a stub — the handler never fabricates events. It either has a token and fetches, or has none and writes nothing. The one historical 401 landed a loud `error` row in `source_poll_outcomes` with `signals_written = 0`; no conflict event was ever invented. The sibling WATCH-desk state-media sources added alongside it (`source.irna.english` / `source.ukrinform.english`) ARE firing live. (No allowlist line — no stub symbol.) |

### 38. Manual ingestion lanes — built + pytest-covered, not yet exercised against a live production ingest

| | |
|---|---|
| **What** | The manual-ingest lanes — `src/legba/data/seed/manual_batch.py` + `manual_schema.py` (structured facts/nexuses/entities/signals/docs batches; format spec `docs/MANUAL_INGEST_FORMAT.md`; drivers `scripts/manual_ingest.py`) and `src/legba/data/rag/lane4_loader.py` (the vector-corpus loader behind `scripts/manual_ingest_vectors.py`) — are real and complete, with substantial pytest coverage (`tests/data_pkg/test_manual_batch.py`, `test_manual_schema.py`, `test_lane4_vector_loader.py`, plus the batch fixtures). The Lane-4 vector loader HAS been run to populate the live `tradecraft`/`world_context` corpora (SEAMS #20). What is honestly UNPROVEN is a full end-to-end manual STRUCTURED batch ingest against the live registry/substrate in production — the coverage is pytest-level (real pool in tests), not a live-operated production run. |
| **Why deferred** | These are operator lanes exercised on demand; a standing production run is an operator step, not wave-scope code. Documenting the pytest-only status honestly (rather than implying a proven live pipeline) is the point of this row. |
| **Guard rail** | NOT a stub — every lane validates its input schema and FAILS LOUD on a malformed batch (see the `manual_batch_bad` fixture), and every write carries provenance/lineage; a bad batch is rejected, never partially fabricated into the substrate. (No allowlist line — no stub symbol; the code is real and complete, only a live production run is outstanding.) |

### 39. Non-Latin / telegram NER re-enrichment — RESOLVED (backfill drained)

**RESOLVED 2026-07-09** (the `reenrich_ner` backfill, migration 0085).
Re-verified 2026-07-28 (C5 hygiene pass) — compacted to the [Resolved seams
appendix](#resolved-seams-compact-appendix).

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

### 42. Evidence-archive retention interplay + object-store backend (P2-1 riders)

| | |
|---|---|
| **What** | The `evidence_archiver` (P2-1) is BUILT: cited signals' original bytes are fetched (SSRF egress guard + the P2-2 license gate), stored content-addressed on the plain-filesystem `legba_archive` volume, hash-stamped onto `signals.object_ref` (`cas:sha256/<hex>`) + the `evidence_archive` sidecar (mig 0104). NOT built: (a) any retention/expiry machinery over the archive — archived objects are evidence and NOTHING deletes them (no `media_ref_expires_at` sweep, no object GC, no operator-gated erasure for a license policy flip; `skipped_license`/`skipped_size` sidecar rows are the recorded re-evaluation queue for such a flip); (b) an object-store backend (MinIO/SeaweedFS per DIRECTION §5) — the `cas:sha256/<hex>` relative address is deliberately backend-agnostic so a later store swap rewrites zero rows; (c) archive-wide coverage beyond cited-only (the per-source depth lane). |
| **Why deferred** | Operator-decided stage-1 scope (program §A3): cited-only + plain FS first, prove it out before widening or adding a store; deletion policy for *evidence* needs an operator decision, not a default. |
| **Guard rail** | Nothing half-built to guard: the archiver only ever ADDS objects and stamps rows; archived signals are upgraded to `retention_class='evidence_hold'`, which the existing `signals_retention` purge already exempts, so no purge can orphan an archive. A missing/unwritable archive root no-ops the tick LOUDLY (`skipped_no_root` counter + warning), never a silent drop. |

### 43. `signals_retention` TTL is options-only — RESOLVED (env fallback landed, W-2 2026-07-28)

**RESOLVED 2026-07-28 (W-2).** Re-verified 2026-07-28 (C5 hygiene pass) —
compacted to the [Resolved seams appendix](#resolved-seams-compact-appendix).

### 44. RESOLVED (2026-07) — world/thematic compositions now carry the two-tier split

**RESOLVED 2026-07-28.** Re-verified 2026-07-28 (C5 hygiene pass) —
compacted to the [Resolved seams appendix](#resolved-seams-compact-appendix).

### 45. RESOLVED (2026-07) — tier-aware LLM-judge rubric

**RESOLVED 2026-07-28.** Re-verified 2026-07-28 (C5 hygiene pass) —
compacted to the [Resolved seams appendix](#resolved-seams-compact-appendix).

### 46. Provenance-badge backend fallback signal (`live|fallback|absent` — the `fallback` input)

| | |
|---|---|
| **What** | The UI provenance enum on displayed numbers (`legba-ui-v3/src/lib/provenance.ts`) classifies `live \| fallback \| absent`, but `fallback` is only ever returned when the caller passes an EXPLICIT backend fallback flag — and most backend routes do not yet carry one (no "this number came from a canned table / last-known value" stamp). Until a route stamps it, a degraded number reads `absent` (when missing) or `live` (when the route serves it without saying it degraded). |
| **Why deferred** | Wiring a per-route fallback stamp is backend-by-backend work; the UI seam was built first so the honest enum exists to receive it. |
| **Guard rail** | The UI NEVER fabricates a fallback state: no explicit backend signal ⇒ never `fallback` (documented in the module header; the `fallback` input is named as the seam a backend follow-up fills). Nothing can claim a degraded number is live-computed — the failure mode is under-labeling (`live` without a degradation note), which the per-route stamps close as they land. |

### 47. Map co-mention arcs are honest-empty (single-country `geo[]` data seam)

| | |
|---|---|
| **What** | The map's co-mention ArcLayer needs a signal whose `geo[]` names ≥2 countries to form an arc, but baseline enrichment currently resolves each signal to a SINGLE country — so no pair ever forms and the layer renders honest-empty (`legba-ui-v3/src/v4/world/MapLibreWorldMap.tsx`, the recorded DATA SEAM comment; matching note in `lib/mapLayers.ts`). A data seam upstream of the UI, not a UI bug. |
| **Why deferred** | Multi-country geo tagging is an enrichment-quality work item (and interacts with the geo-contamination fixes that deliberately made tagging MORE conservative); an empty layer is honest, a guessed second country is not. |
| **Guard rail** | The layer renders exactly what the data supports — empty — and the seam is stated in code at the render site; nothing draws a fabricated arc. (UI-side + enrichment data; no allowlist line — no stub symbol.) |

### 48. `narrative_coordination` grounding on the narratives sidecar (grounding-token seam)

| | |
|---|---|
| **What** | The `narrative_coordination` LLM unit could ground on the reified `narratives` sidecar (carrier sources, echo lags, lead/follow edges) via a new `"narratives"` grounding-source token — a sources-dispatch entry in `analyst_deps_builder.py` plus a block builder in `grounding.py`. Not wired: the unit today reads its signal slice + the standard grounding tiers only (`src/legba/data/analysts/deterministic_handlers/narrative_mapper.py`, the "Seams" docstring section). A sibling display enrichment — tagging carriers with `state_affiliation` from the ratings rubric — is likewise noted-not-wired. |
| **Why deferred** | Additive and deliberately out of the P4 wave scope; the narrative objects needed to exist and prove stable before an LLM unit grounds on them. |
| **Guard rail** | Nothing half-built to guard — the token does not exist in the `GroundingBlock.sources` Literal (`src/legba/data/schemas/analyst.py`), so a descriptor declaring `"narratives"` is refused at schema validation (loud pydantic error, the same mechanism as the `stream` seam #2) rather than silently injecting nothing; the narratives read routes and the mapper are real and complete. (No allowlist line — no stub symbol.) |

### 49. `claim_watch` closer (K-5 stage) — not built; the watcher is flag-only

| | |
|---|---|
| **What** | `claim_watch` (KW-3, `src/legba/data/analysts/deterministic_handlers/claim_watch.py`) is the WATCHER half only: a deterministic META analyst that matches new signals (since a durable cursor watermark on the shared `alert_trigger_watermarks` table, `trigger_class='claim_watch'`) against the standing open-question set (`hypotheses` rows, `status='open_question'`) via a fused vector+entity+geo plane, and side-writes `bearing_edges` (migration 0107) + `review_flags` (migration 0107, only for questions tracing to LIVE consumers via `output_consumption`) + a per-run `staleness_debt` gauge (open flags whose flagged consumer is still live). It NEVER writes correction content, NEVER writes back to the flagged producer, and NEVER recomposes anything — flags/edges only, by construction. **One further absence rides this seam:** the **K-5 closer** — injecting a confirmed match as evidence into the original producer's next natural run (slice injection), letting supersession correct the record, and closing the review flag by supersession — does not exist in-tree. **Partially closed (C3 wave):** `staleness_debt` now HAS a read route — `GET /api/v1/v3/system/staleness-debt` (`data/registry/v3_api.py`), which computes the headline number with the matcher's own SQL (mirrored under a byte-equality drift guard) and reports `match_verified: false` on the wire. `review_flags` rows are still only aggregated by it (counts, distinct consumers/foundations, reason breakdown) — there is still **no per-row read surface** for `review_flags` or `bearing_edges`, no UI panel, and no MCP tool touching either. **A strictly prior absence, closed 2026-08-03 (W1-C2):** this entry described an *unread* problem while the live data showed an *unwritten* one. The forward walk is seeded `WHERE output_consumption.consumed_id = <hypothesis_id>`, and no producer in the tree had ever written such a row — the only two stampers (composition basis/periphery, journal slice) record fact/finding ids — so `review_flags` was **0 rows all-time** and `flags_written` 0 on every tick, for reasons unrelated to substrate health. `inline_target.run_method` now stamps `CONSUMPTION_CONTEXT_QUESTION` when it resolves an `addressed_question` against the run's grounding question sink, so the surface can fire. Its VOLUME is now gated on the corpus_researcher answer-link rate (single digits to date), not on the walk. **F5 follow-up, closed in-tree 2026-08-10 (K-4 R4 §9):** the answer-link rate never reached the watched set — live measurement found `output_consumption` at 0 rows for ALL 112 watched question ids (9,556 rows overall), so the consumer-only walk still left `review_flags` at 0 rows all-time and `bearing_edges` was the watcher's only artifact. The flag path is now wired to something that exists: with `method.options.question_flags: "on"` (ships `'off'` in code — the X-1 byte-identical contract; armed live by descriptor PUT on the 2026-08-10 train, and the first self-flag rows have landed) a matched question with NO forward consumer writes ONE open SELF-flag — `output_id = founded_on_id =` the hypothesis id, reason `new_evidence_bears_on_unconsumed_question`, one open flag per question via the 0107 partial unique index — so watched-question hits are queryable rows and the `/system/staleness-debt` reason breakdown surfaces them with no new read machinery. |
| **Why deferred** | Sequenced by design (the claim_watch closer program's planning notes, §5–6). K-5 arms ONLY after **K-4**, a match-precision gold loop (sampled matches labeled out-of-plane, the W31 provenance pattern: frontier model + live search, `labeled_by` stamped, operator spot-checked), proves the watcher's fused-plane matching clears an agreed precision bar — matcher precision is the whole gate: too loose and everything is perpetually "under review" (the debt metric stops meaning anything); too tight and the correcting signal is missed. **DEC-K1** sets that bar — the recommendation on the table is pairwise precision ≥0.85 on the K-4 labeled sample. **K-4 has now been run FOUR rounds, and the bar moved from missed to met.** First full measurement 2026-07-29: 122 stratified pairs labeled out of plane (frontier model + live search, `labeled_by` stamped; a calibration pass agreed on 100% of the decision-critical class). Pairwise precision by matching plane — `vector+entity+geo` 1.000 (17/17, but effective n≈9: seven rows are one event cluster ×2 sibling questions), `vector+entity` 0.538, `entity+geo` 0.120 (failures shift to WRONG-DIMENSION rather than wrong-theater), `entity`-only 0.000 (0/54 — hub-entity bridging plus NER junk), meta-questions 0.035 (57 rows structurally unmatchable by a news matcher), substantive theses 0.492, **pooled 0.279**. The apparent version split (3.0.0 0.456 vs 3.1.0 0.056) is **sample composition, not version quality** — the 3.1.0 stratum was the hub-heavy non-vector tail, so plane mix is the real axis. Three measured levers shipped in response (matcher `3.2.0`: meta-question exclusion at match time, global signal-side hub-entity damping, and an omnibus cap of 8 questions per signal plus same-URL dedupe), projecting pooled ≈0.49 post-exclusion — better, still short. Round 2 (2026-07-30, at volume) measured 0.15 and named the residual failures BEARING failures, which bought the `3.3.0` bearing pipeline; round 3 measured the gated `3.3.0` stream at 0.267 population-weighted and bought the `4.0.0` precision train (blocking confirm leg, desk identity in both bearing prompts, deictic-thesis refusal, contention liveness + subject anchor); **round 4 (2026-08) measured the live `4.0.0` stream at 0.908 — over the 0.85 bar.** `4.1.0` ships R4's follow-ups (consequence-specificity clause, article-id URL dedupe, the F5 self-flag surface). **K-5 still stays parked**: with the bar met, building the closer is a held OPERATOR decision (DEC-K1's second half — arming a write-back loop is a direction call, not an automatic consequence of a number). |
| **Guard rail** | NOT a stub — there is no half-built closer path to guard; the watcher structurally cannot do more than flag (no correction-content write, no recomposition — true by construction of what the handler writes, not a runtime toggle). It is no longer LLM-free, and that is worth stating precisely rather than eliding: since matcher `3.3.0` an optional **post-match bearing gate** may ask a small self-hosted model "does this signal bear on this thesis?" and REFUSE an edge on a NO, and since `4.0.0` a **blocking confirm leg** on the $0 core plane may drop a gate-passed edge (confirm-NO measured 7.8× less precise than confirm-YES on R3's labels; every row carries a `bearing_watch` band). Both only ever SUBTRACT from what the deterministic planes already selected, cannot write anything new, ship **OFF in code** (`bearing_gate: 'off'` — flipped on by a descriptor PUT, which the reference deployment has done), and an unreachable model stamps `unavailable` and writes the edge anyway — the gate never fails closed. Nothing about the closer changes: refusing an edge is not correcting a product. `review_flags` rows stay open until a future closer (or an operator) resolves them; the 0107 forbid-delete trigger blocks silent flag disappearance. Until DEC-K1 is decided and met, `staleness_debt` is published honestly as a "flags found, match unverified" count — never a corrected/closed metric. Every edge also carries the `matcher_version` that produced it (`claim_watch/4.1.0` today), so edges written under an earlier, weaker matching rule stay distinguishable rather than being retroactively dignified — that stamp is exactly what let the K-4 measurement stratify 3.0.0 from 3.1.0 instead of pooling them into one meaningless number. The C3 read route does not soften that: it carries a hard-`false` `match_verified` field, reports flags on already-superseded consumers as a SEPARATE count rather than folding them into the debt, and returns the matcher's last run time so a reader can tell a genuine zero from a matcher that never ran — the number is exposed, not dignified. (No allowlist line — no stub symbol; the closer is simply absent, not faked.) |

### 50. Search control-query canary (scheduled half) — RESOLVED (cron installed 2026-07-29)

Closed. See the resolved-seams appendix below. The scheduled half now exists on
both sides: `scripts/host_search_canary.sh` (`RUNBOOK.md` §24.1) is the hook,
and its cron line is installed in `/etc/cron.d/legba-watchdog` on a **15-minute**
clock, paging only after **two consecutive** not-live probes (persisted streak,
1-hour cooldown, alert-only — it never restarts anything). The correctness
guarantee it sits behind is unchanged and was never dependent on it: an
unverified liveness verdict maps to `SearchStatus.EMPTY`, which the `web_search`
tool returns as a **failure** (`search_liveness_unverified`), never as a
successful zero-result search, and which `SearchResponse.supports_absence_claim`
refuses to license. The canary buys earlier operator notice that the plane has
gone dark, not a stronger absence claim.

**Second consumer as of 2026-08-29 — the search plane is now load-bearing for a
scheduled analyst, not only for on-demand consult.** The `standing_auditor`
(ARCHITECTURE §5.10 step 7) checks the platform's own world-claims against live
external search through the `web_access` pack. It declares **no new seam**: the
capability is built, the pack governs it, and with no search binding the handler
**refuses to spend a core-plane call at all** and files a heartbeat whose
`degraded_reason` names the gap — a refusal plus a visible receipt, never an
audit against nothing. The relevant operator consequence is that a dark search
plane now silently costs a verification surface as well as a consult tool, which
is precisely what this canary's earlier notice is worth.

### 51. `watchlist.actions` — a watch can only notify (Stage 2 not built)

| | |
|---|---|
| **What** | A watchlist row (`watchlist`, migration 0105) declares **what to watch** — an alias/fold-resolved `entity`, a `text` pattern over the search plane, or a `geo` place — and a `min_severity`. It cannot declare **what to do**: the table has no `actions` column, and the scanner (`_watchlist_scan.scan_watchlist`) only builds `AlertCandidate`s, so a watch's sole possible effect is a `watchlist_hit` alert routed to the notify path. The proposed Stage 2 — a `watchlist.actions` jsonb rule store letting a watch also invoke a tool, force an analyst run, or open a collection requirement — does not exist: no column, no dispatcher branch, no per-rule requester identity, no per-rule budget account. The CRUD route (`watchlist_api.build_watchlist_router`) validates only the three pattern kinds and contains no notion of an action. |
| **Why deferred** | Deliberately demand-gated, and the demand is measurably absent: the live `watchlist` table holds **zero rows** (`watchlist_hit` has exactly one watermark, the seed marker). Building a rule-execution plane onto an authoring surface with no adopted rules would be building the general case before the specific one is used once. The two technical prerequisites, by contrast, are **already landed** — Stage 0, deterministic thresholds becoming real descriptor config (`method.options`, the X-1 wave); and Stage 1, the escalation edge's action becoming selectable config (`action_tool`, validated against the bound pack's live tool list) rather than a hardcoded literal at the fire site. So the gate on Stage 2 is *adoption*, not missing plumbing. |
| **Guard rail** | Nothing half-built to guard — the notify-only behavior is the *whole* behavior, true by construction of what the scanner can emit rather than by a runtime toggle, and a watch author is never offered an action field that silently does nothing. Were the column added with the proposed default (`[{"kind":"notify"}]`), the migration would be a semantic no-op, which is the honest shape for a store whose only implemented verb is today's behavior. Any Stage-2 build must land the requester identity and the per-rule budget account **with** the dispatcher, never after: an operator-authored rule that can invoke tools is an agency surface, and the agency plane's hard gate is not optional. (No allowlist line — no stub symbol; the action plane is absent, not faked.) |

### 52. Receipt prompt columns — `prompt_rendered` is now BOUND (capped); `prompt_module_hash` IS still unbound; the GEPA training input was the casualty

| | |
|---|---|
| **What** | `analyst_traces.prompt_module_hash` and `analyst_traces.prompt_rendered` were **0-populated over 187,550 rows, all-time** (live-verified 2026-08-04). A silent-absence census read that as two unbound columns. Only one of them ever was — and that one has since been bound. |
| **`prompt_rendered` — RESOLVED 2026-08-20 (bound, with a stated cap)** | The row below is kept because the *reasoning* still governs the shape of the fix, not because the column is still NULL. It is now written: `run_accounting.record_prompt_rendered` overwrites a SINGLE slot per run rather than accumulating (bounded to one prompt's worth of memory however many GATHER rounds and judge legs a run makes, and nothing is added to the `llm_calls` JSONB), and `current_prompt_rendered` is read once, at the instant the actor flushes the trace write — so in the ordinary run shape the captured prompt is the synthesis call's. **The stored text is capped at `_MAX_PROMPT_RENDERED_CHARS = 32_000` with an explicit truncation marker naming the full length — never a silent cut.** The digest that used to be the whole substitute is now the cap's guarantee: `prompt_sha256` is **always computed over the FULL, untruncated text** and, since **migration `0186` (2026-08-27)**, lives in its own column, deliberately NOT folded into `compute_receipt_hash`'s canonical payload — supplementary provenance, not chain material, the same posture as `llm_calls`/`tool_calls`. So a capped row is still byte-verifiable against a re-render (`scripts/render_prompt_pack.py` depends on exactly that claim), and the historical receipt chain is untouched: the old `None` hashes as `None` and all 187,550 existing receipts still re-verify. **Residual, stated rather than buried:** on a desk with a verbose system prompt the static prompt plus the authoritative-context and register blocks can consume the whole 32,000 characters *before a single numbered signal is stored*, so on those desks the persisted trace cannot show which evidence the desk read. The reachability question is answered by `analyst_traces.input_row_refs` (not truncated) instead — see `RUNBOOK.md` §11 *Reconstruct a truncated rendered prompt*. Raising the cap, or giving the numbered-signal block its own column, is open: the row-bloat argument that set the cap has not been re-measured. |
| **`prompt_module_hash` — a real gap, deferred with a reason** | This one genuinely never got wired: `dapr_actors.py` reads `getattr(method_result, "prompt_module_hash", None)` and **nothing in `src/` ever defines that attribute**, so it is `None` at every call site forever. It closes a real audit hole — `analyst_version` hashes the descriptor BODY, so editing a prompt constant (`legba.prompts.*`) and redeploying changes behaviour under an identical version. The obvious cheap bind is not actually available at the write site: the resolved system prompt lands on the per-kind **runner** (`analyst_deps_builder` → `InlineTargetRunner(system_prompt=…)`), not on `StandardDeps`/`deps_bundle`, so binding means threading a hash through every analyst kind's result — an actor-plane change wanting a live force-run per kind, not a small-train edit. Scoped, not silently dropped. Partial-coverage caveat to accept up front when it is built: descriptors using the DSPy package shape (`module.path`, no colon) resolve no constant and would stay NULL. |
| **The live casualty (historical)** | `optimizer.py` selects `t.prompt_rendered AS input` for the GEPA training set, so **every training row's input was `""`** for as long as the column was NULL — GEPA optimized against empty inputs and said nothing. Made audible 2026-08-04 (`optimizer.training_set.all_inputs_empty` warning when a whole set is empty); the column is bound as of 2026-08-20, so newly written traces carry a real (possibly capped) input. This is not proof the training set is now sound — the GEPA plane is MOTHBALLED (#53) and nothing has re-measured it — so treat the casualty as *no longer structurally guaranteed*, not as fixed. |
| **Guard rail** | Nothing fabricated in either state: a NULL column read as NULL, `compute_receipt_hash` hashed the `None` honestly, and `lineage_api` re-feeds the stored value so historical receipts still verify byte-for-byte; a truncated value carries its explicit marker and its full-text digest rather than pretending to be complete. The empty GEPA input is loud rather than silent, and GEPA promotion is operator-gated regardless. (No allowlist line — no stub symbol; one nullable column and one capped field.) |

### 53. GEPA optimizer plane — MOTHBALLED (RUST-4, decision 2026-08-21)

| | |
|---|---|
| **What** | The GEPA / DSPy self-optimizer plane (`src/legba/runtime/dapr_workflow/gepa.py`, `workflow.py`, `worker.py`, `dspy_lm.py`; `src/legba/data/analysts/optimizer.py`; `descriptors/analyst_country_optimizer.yaml` and `descriptors/analyst_unit_optimizer.yaml`) is **MOTHBALLED**, orchestrator decision 2026-08-21 on the evidence gathered read-only in `planning/RUST4_EVIDENCE_2026-08-21.md`. Code, tests, and the UI panels (`Optimizer.tsx`/`OptimizerDiff.tsx`) all STAY — nothing is deleted. What stops: both descriptors' operational trigger paths (annotated `state: paused` in-tree; see below), the worker image's place in the routine deploy path (`docker-compose.yml` / `deploy/swarm/*` keep the `legba-dapr-workflow-worker` service — it also hosts the actively-used `deep_consult_workflow`, SEAMS #1b of the evidence file — but it is commented as mothball-adjacent), the dead optimizer restart calls in `scripts/host_stall_watchdog.sh`, and the false-red mask on 11 nightly dspy-import tests (now explicit skips, not a `KNOWN_FAILURES` allowlist entry). |
| **Why deferred (mothballed, not fixed-then-shipped)** | Evidence, not a hunch: in six lifetime runs of the only optimizer descriptor that ever actually ticked (`unit_optimizer`), 4/6 fell back to `naive_best_of_n` (dspy path never engaged), 1/6 hit the Dapr long-activity round-trip bug (#86; `workflow_timeout` at 1800s, reproduced 2026-08-17 — SEAMS #23's "RESOLVED" note covers the GEPA compile activity specifically and has NOT been re-verified against this later recurrence), and the ONE completed real `dspy_gepa` compile (2026-08-10) produced a candidate whose faithfulness COLLAPSED against the parent prompt (delta -0.5354 vs a +0.03 promotion floor — not a marginal miss). `country_optimizer` (the original monolith) has never run at all — it was already frozen by SEAMS #30. Meanwhile the manual VOICE-4 prompt wave shipped and measurably won. The case for keeping GEPA on the deploy/schedule path evaporated; the case for keeping the CODE (a real, tested ~8,357-line implementation) did not — it returns if a future measured proving run wants it. |
| **Guard rail** | Two layers, both loud, neither a silent no-op: **(1) code** — `src/legba/data/analysts/optimizer.py::run_method` refuses at its top with `OptimizerMothballedError` (a `RuntimeError`, not a `NotImplementedError` — no allowlist line needed, same idiom as SEAMS #10's `TaxiiServerNotConfiguredError`) whenever `options["analyst_id"]` is `country_optimizer` or `unit_optimizer` (`MOTHBALLED_OPTIMIZER_IDS`) — this is the single choke point every trigger funnels through (cadence reminder, reactive fire, or a manual force-run all stamp the optimizer's own identity into `options["analyst_id"]` before calling `run_method`), and it surfaces as `ActorRunOutcome.HARD_FAIL` in the actor's run record, never a plausible-looking `naive_best_of_n` finding. **(2) descriptor** — both `descriptors/analyst_country_optimizer.yaml` and `descriptors/analyst_unit_optimizer.yaml` carry `state: paused` in-tree (LifecycleState's built-in, resumable, non-terminal halt state — `ALLOWED_TRANSITIONS[ACTIVE] = {PAUSED, RETIRED}`, `src/legba/data/schemas/lifecycle.py`); once an operator PUTs this to the registry the reconciler (`src/legba/runtime/reconcile.py` line ~190, `rec.lifecycle != desired.lifecycle_target`) drives the live actor PAUSED, which tears down its `run_cadence` reminder (mirrors the mechanism SEAMS #30 already uses for `country_optimizer`'s null-cadence freeze) — so no tick reaches `run_method` at all in steady state, and the code-level refusal above is the backstop for anything that still reaches it (a stray reminder in flight, a manual force-run before the pause registers). **THIS BUILD IS TREE-ONLY** — the `state: paused` edit has not been PUT to the live registry, so as of this commit the descriptors are still live `active` in production; the code-level refusal (layer 1) is what actually protects production until an operator deploys+PUTs the descriptor change. No allowlist line for `OptimizerMothballedError` (RuntimeError guard rail, not a stub — same as SEAMS #10). |
| **To restore** | Flip both descriptors' `state` back to `active` (registry PUT, operator-approved), remove the `MOTHBALLED_OPTIMIZER_IDS` check (or the specific id) in `optimizer.py::run_method`, re-add the worker service to the routine deploy profile combination, and re-run a bounded proving run per the evidence file §4 blockers (the #86 round-trip bug still needs one of its three fix options landed first). |

---

## Resolved seams (compact appendix)

Entries below were declared seams re-verified against the live source during
the 2026-07-28 C5 registry-hygiene pass and confirmed resolved. Original
numbering is kept stable because other docs (`STATUS.md`, `RUNBOOK.md`,
`RELEASE_STATE_MATRIX.md`, `FLOWS.md`) cite specific `SEAMS #N` — the numbered
slot above each row now carries a one-line pointer here instead of the full
historical narrative.

| # | Seam | Resolved | Verified against (2026-07-28) |
|---|------|----------|-------------------------------|
| 3 | Deep-crawl discovery jobs (`crawl_discovery`/`query_discovery`) | 2026-06-09 (decision F-1) | `discover_sources_tool` removed, `src/legba/data/analysts/agency/tools.py`; `descriptors/action_pack_discovery.yaml` `state: retired` |
| 11 | Consult `vector_search` embedder wiring | 2026-07-02 (L-114) | `src/legba/runtime/substrate_query_port.py` embeds the query through the port-threaded embedder before `vector_search_by_embedding` |
| 22 | Live GATHER actuation of `web_access`/`propose_facts` tools (S6) | 2026-06-19 | `inline_target._GATHER_TOOLS` spans read+web+write; `dapr_actors._gather_write_bindings_for_target` live and wired |
| 23 | Dapr long-activity workflow round-trip — GEPA optimizer leg | 2026-06-29 (=#86) | `TrainingSetRef` + `materialize_training_set` (pass-by-reference) in `dapr_workflow/gepa.py`; `-max-body-size` lever in `docker-compose.yml`. **Residual, still true**: the `deep_consult` sibling shares the fix but was NOT independently re-verified — its in-process fallback stays the live default. |
| 24 | `nlp_client` boot-singleton cannot re-resolve | 2026-06 (#91 / `985db7e`) | `src/legba/runtime/nlp_client_factory.py:LazyNlpClient` resolves on first use and re-resolves on handler-build |
| 39 | Non-Latin/telegram NER re-enrichment backlog | 2026-07-09 | `reenrich_ner` backfill (migration 0085) drained ~9k signals; `src/legba/data/analysts/deterministic_handlers/reenrich_ner.py` live |
| 43 | `signals_retention` TTL is options-only | 2026-07-28 (W-2) | `LEGBA_SIGNALS_RETENTION_TTL_DAYS` env fallback in `signals_retention.py`; `tests/data_pkg/test_signals_retention.py` |
| 44 | World/thematic compositions two-tier evidence split | 2026-07-28 | `LEGBA_COMPOSITION_TIERED_EVIDENCE` in `meta_findings_synthesizer.py`; `tests/data_pkg/test_composition_tiered_evidence.py` |
| 45 | Tier-aware LLM-judge rubric | 2026-07-28 | `verify._judge_periphery_rubric`; same test file, judge-rubric section |
| 50 | Search control-query canary — scheduled half | 2026-07-29 | `scripts/host_search_canary.sh` + its `*/15` line in `/etc/cron.d/legba-watchdog`; probes forcing `verify_engine_liveness(..., force=True)` and confirmed executing on the clock. Pages only after 2 consecutive not-live probes; alert-only, never restarts. |

**Not moved despite a "RESOLVED" status line in its title**: seam **#20**
(Tier-2 `vector:world_context` grounding) — the embedder-through-port WIRING
is resolved (that's what #11 covers), but the entry itself is an ACTIVE
guarded pilot with honest open residuals (single-unit rollout, unproven
faithfulness benefit, a known tail-risk recurrence guard, an ephemeral
rollback-state path) — re-verified 2026-07-28 that `src/legba/runtime/rag_rollback.py`
and the single-unit pilot (`descriptors/analyst_leadership_transition.yaml`
has RAG off) are still exactly as described. It stays in the main declared-seams
list, not the resolved appendix.

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
