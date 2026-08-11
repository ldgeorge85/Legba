# Legba — Direction

The forward engineering direction for capabilities Legba deliberately does NOT
ship yet — the public answer to "where is RBAC / STIX / MCP / …?". One page per
item, each grounded in the code as it exists today, with the integration points
named so the design is checkable against the tree. New here? Start with the
[README](../README.md) and the [Tour](TOUR.md).

**Contents:**
[0 Deployment perimeter](#0-deployment-perimeter--single-operator-single-tenant-locked) ·
[1 RBAC / SSO](#1-rbac--sso) ·
[2 Tenancy enforcement](#2-tenancy-enforcement) ·
[3 STIX / TAXII + MISP](#3-stix-21--taxii-export--misp-sync) ·
[4 MCP server](#4-mcp-server) ·
[5 Multimodal](#5-multimodal-for-real) ·
[6 Scale-out](#6-scale-out) ·
[7 Fallback-model budget demotion](#7-fallback-model-budget-demotion-decision-f-2) ·
[8 Deep-crawl discovery jobs](#8-deep-crawl-discovery-jobs-decision-f-1) ·
[9 Data-integrity sweeps](#9-data-integrity-sweeps-re-homed-as-integrity_sweep--built) ·
[10 Knowledge grounding](#10-knowledge-grounding--current-world-state-injection-built-tier-2-designed)

One distinction applies everywhere below: the mandatory verify pass measures
**groundedness, not truth**. It scores whether each claim *follows from its
cited evidence* (a faithfulness score in `[0,1]`, from an LLM judge resolved
through its own repointable route — descriptor default same-model, the
reference deployment cross-family on a hosted Gemma judge; see `AI_MODELS.md`
§3 — plus a deterministic citation-presence floor); it does **not** adjudicate whether the claim is true
about the world. Within the **built** system this document still separates the
**measured core** — the nine units, the per-country / per-region / world
composition tower, the
banded scorecard, and the provenance / drill-down that carries them — from **the
ambitious legs, which now return ONLY as measured, honestly-reported
experiments**:

- **Skill is a per-unit number**, never a platform-wide boast: per-unit
  faithfulness + correctness-vs-reference, honest-null where unmeasured. The live
  banded scorecard is deliberately a *mix* — some country dimensions band from a
  qualifying verified claim, others read `insufficient-evidence` with an explicit
  reason (e.g. the US card currently reads all-insufficient because that unit's
  faithfulness is genuinely low), and the correctness gold set is
  small (a first weekly cohort of n=8 verdicts feeds the correctness axis;
  the deterministic reference leg is still n=1, reported insufficient-sample).
  No band is ever fabricated.
- **Forecasting** returns ONLY as a precise-question `acute_forecasts` Brier /
  BSS scoreboard (question + window + probability + auto-resolve), surfaced solely
  on the calibration route, **never** as a free-text claim or finding. It
  **currently reports NO proven skill** — a degenerate / geography-dominated
  probability vector abstains (zero rows) and the skill number is withheld rather
  than dressed up. That null result is *published*, not hidden. The forecast-as-claim
  predictors (`country_predictor`, `india_energy_predictor`) are **retired / frozen and
  stopped**; their ~539 historical `prediction` rows remain in the DB, unread by the
  spine — forecasting returns *only* as the scoreboard above, no longer as a free-text claim.
- **The GEPA self-optimizer** returns scoped to ONE measured unit
  (`leadership_transition`) as a `unit_optimizer` descriptor; every candidate
  carries a REAL before/after paired faithfulness delta measured on the same
  faithfulness judge (whatever the judge route resolves; a live run read parent `0.34` → candidate `0.29`, delta `-0.05`), stays
  `promotion_gate = human_gated`, and can never auto-promote on an absent /
  degenerate / non-positive delta. The old monolithic `country_optimizer` stays
  **cadence-frozen** (its descriptor is still `state: active`, but it no longer ticks —
  no reminder-flood regression).

The point is unchanged: a leg with no proven skill is stated as having none,
right here, rather than quietly overclaimed. The per-page status lines below grade
*built vs designed*.

The rule that shapes this document: **a feature that is not built is a declared
seam that fails loud — never a quiet stub, never fabricated output.** Each page
below states the problem, the chosen approach, the concrete integration points
(file and symbol), and an explicit status line. Three statuses appear:

- **built** — the production path runs end-to-end today.
- **guarded seam** — the surface exists but refuses activation / raises loudly
  until the real edge is wired (declared in `docs/SEAMS.md`).
- **designed, NOT built** — this document is the design; no code claims it.

---

## 0. Deployment perimeter — single-operator, single-tenant (LOCKED)

**Legba ships as a single-operator, single-tenant deployment.** This is a
locked product decision (D1), not a temporary gap: the release perimeter is one
operator running one instance behind a single Caddy `basic_auth` credential,
operating one logical tenant (`owner_tenant = 'default'` / `'shared'`). The
system makes **no multi-tenant, RBAC, or per-role isolation guarantee**, and the
documentation makes no such capability claim.

Concretely, for this release:

- **One credential, one identity.** Auth is a single shared bearer behind the
  Caddy perimeter; there is no role/scope split, no SSO, no per-user identity.
  See §1 for the *designed* RBAC direction — it is explicitly deferred, not
  shipped.
- **One tenant.** Analysis-plane outputs (findings / alerts / critiques /
  situations / facts / nexuses / hypotheses) are tenant-blind by design at this
  release. The acquisition plane already carries `owner_tenant` for forward
  compatibility, but it is **not an isolation boundary** you should rely on to
  separate untrusting tenants. See §2 for the *designed* enforcement direction —
  also deferred, not shipped.
- **The perimeter IS the boundary.** Security for this release rests on the
  network/deployment perimeter (Caddy `basic_auth`, loopback-bound internal
  services), not on in-application multi-tenant access control. Do not deploy
  Legba as a shared-tenant service for mutually-untrusting users.

Multi-tenant isolation and RBAC are real future direction items (§1, §2,
`docs/SEAMS.md` #14) — they are designed and gated, never silently half-built.
Until they ship, **no enterprise / multi-tenant / RBAC capability is claimed
anywhere in the docs.**

---

## 1. RBAC / SSO

**Problem.** The registry API has exactly one credential: a single shared
bearer token (`LEGBA_REGISTRY_API_TOKEN`) checked by `require_bearer`
(`src/legba/data/registry/api.py`). One token equals full access — descriptor
CRUD, vault credential writes, substrate reads, consult invocation. There is
no read-only token to hand to a dashboard, no operator/admin split, and no
identity: the audit layer (`src/legba/data/registry/audit.py`,
`AuditEntry.actor_id` / `actor_role`) records whatever principal string
`require_bearer` returns, with `actor_role` hard-coded to `"operator"`.
Perimeter auth is Caddy `basic_auth` with bearer injection (`docker/Caddyfile`,
the `basic_auth_perimeter` snippet; `header_up` swaps the browser's Basic for
the registry bearer) — one password, one identity, no session lifecycle.

**Chosen approach.** Three steps, strictly in this order:

1. **Scoped tokens at `require_bearer`** — `read` / `operator` / `admin`.
   `require_bearer` grows into a `Principal` resolver (token → `{principal,
   scope, tenant}`); routers declare their floor via a dependency factory
   (`require_scope("operator")`) wrapping the existing
   `Depends(require_bearer)` sites (`consult_api.py::invoke_consult`,
   `substrate_reads_api.py`, `lineage_api.py`, the new per-analyst runtime-eval
   route `GET /api/v1/v3/eval/analyst_runtime` (`v3_api.py::eval_analyst_runtime`
   — a natural `read`-scope floor: run count, avg/max wall-clock, last run,
   non-success, from `analyst_traces`), the descriptor/vault routers).
   Token records live in Postgres (hashed), rotatable per-scope; the resolved
   scope is stamped into `AuditEntry.actor_role` instead of the constant.
   Dev mode (env unset → accept-all, logged once at WARN) survives unchanged —
   it is the documented L-113 behavior, not an accident.
2. **OIDC at the Caddy layer (`forward_auth`) BEFORE any in-app SSO.** Caddy
   already owns the perimeter and already rewrites `Authorization` upstream;
   `forward_auth` against an OIDC provider replaces `basic_auth` in the same
   snippet, and the validated identity maps to a scoped token via the same
   `header_up` channel. The application keeps verifying exactly one thing — a
   bearer with a scope — and never grows cookie/session/redirect machinery.
   In-app SSO is rejected until a concrete requirement (per-user API keys
   issued from the UI) forces it.
3. **Tenancy claim mapping.** The token record (and later the OIDC claim)
   carries `owner_tenant`; `require_bearer` resolves it into the `Principal`,
   and request handlers thread `principal.tenant` into every substrate query.
   This depends on page 2 — there is nothing to enforce against until
   `owner_tenant` exists on the analysis-plane tables.

**Integration points.** `require_bearer` / `_current_token`
(`src/legba/data/registry/api.py`); `AuditEntry`
(`src/legba/data/registry/audit.py`); `docker/Caddyfile` (`basic_auth_perimeter`,
the `handle /api/*` blocks); every `Depends(require_bearer)` site under
`src/legba/data/registry/`.

**Status: designed, NOT built.** Today: one shared bearer, full access,
Caddy basic_auth perimeter, manual rotation.

---

## 2. Tenancy enforcement

**Problem.** `owner_tenant` is threaded through the **acquisition plane**
end-to-end and stops dead at the **analysis plane**. Built today: the signal
contract carries it (`Signal.owner_tenant`,
`src/legba/data/sources/_contract.py`), `SourceActor` pins it on the DB row,
the NATS subject token, and the binding
(`src/legba/runtime/source_actor.py` — the descriptor's
`scope.owner_tenant` wins over anything the handler set); it is indexed on
`signals` (`signals_owner_tenant_idx`, migration `0024_pivot_substrate.sql`);
the subscription plane pins it in pushed-down SQL and re-checks it in the
residual matcher (`src/legba/runtime/subscription/filter.py` — both the
`owner_tenant = $n` WHERE clause and the row-level
`row.get("owner_tenant") != owner_tenant` guard); the publish subject embeds
it (`legba.signals.<tenant>.<source>.<modality>.<event_class>`,
`signal_subject` in `src/legba/data/nats.py`); the trigger plane persists it
(`trigger_state.tenant`, `TriggerStateStore.save_dirty`,
`src/legba/runtime/triggers/state.py`).

Not built: `analyst_outputs` (migration `0012_analyst_outputs.sql`) has **no
`owner_tenant` column** — findings, alerts, critiques are tenant-blind the
moment they are written. The substrate read APIs
(`substrate_reads_api.py::list_findings` / `list_situations` / `list_signals`)
and the lineage walk (`lineage_api.py::walk_lineage`) filter nothing by
tenant. Descriptor projections expose tenancy for sources only
(`SourceDescriptorOut.owner_tenant`, `src/legba/data/registry/api.py`);
target and analyst projections have no tenancy column at all.

**Chosen approach.** Enforcement prerequisite first, then enforcement:

1. Additive migration: `analyst_outputs.owner_tenant TEXT NOT NULL DEFAULT
   'default'` + index, mirroring the `signals` column exactly.
2. Stamp at write time: the provenance write path
   (`src/legba/data/provenance/writes.py`, `KIND_REGISTRY` kind→table routing)
   takes the tenant from the analyst's target-descriptor scope, carried
   through the trigger fire → actor `run()` options the same way `target_id`
   already is.
3. Surface `owner_tenant` on the target/analyst descriptor projections so the
   UI and the registry list endpoints can scope by it.
4. Then per-request enforcement: the `Principal.tenant` from page 1 becomes a
   mandatory `WHERE owner_tenant = $tenant` on every substrate read and on
   each hop of the lineage BFS. Deny-by-default; single-tenant installs ride
   the `'default'` tenant and notice nothing.

Ordering is the point: enforcing tenancy against tables that do not carry the
column would be theater. The column and the stamping land first.

**Integration points.** `0012_analyst_outputs.sql` (the missing column);
`src/legba/data/provenance/writes.py` (stamping site);
`substrate_reads_api.py` / `lineage_api.py` (enforcement sites);
`SourceDescriptorOut.from_row` (`src/legba/data/registry/api.py`) as the
projection pattern to replicate for targets/analysts.

**Status: designed, NOT built.** Acquisition-plane tenancy is built and
enforced at match time; analysis-plane tenancy (column, stamping, read-time
enforcement) is not.

---

## 3. STIX 2.1 / TAXII export + MISP sync

**Problem.** Interop with the rest of the threat-intel world is a
table-stake: a platform whose findings cannot land in OpenCTI / MISP /
EclecticIQ is a silo. The wire format is STIX 2.1 over TAXII 2.1.

**What exists (built).** The bundle producer already ships as an output kind:
`src/legba/data/outputs/stix_bundle.py` (`KIND_NAME = "stix_bundle"`, L-120).
Mapping: `FindingPayload` → `report` (`threat-report`); `SituationPayload` →
`incident` + wrapping `report`; `HypothesisPayload` → `report`
(`analysis`-typed, hypothesis label); `AlertPayload` → `indicator` at
medium+ severity, `report` below (`alert_to_indicator_or_report`). TLP
marking on every SDO from the descriptor's `outputs.stix_bundle.tlp`. Legba's
`derived_from` UUID lineage becomes `derived-from` `relationship` SDOs
(`_derived_from_relationships`). Cited entities become `identity` /
`location` SDOs (`_extract_cited_entities`). Bundles publish to NATS
(`legba.outputs.stix.<target_id>`) with an optional file sink; the TAXII
collection convention is `legba_target_<target_id>_collection`.

**What exists now.** Pushing to an upstream TAXII 2.1 server is **built**
(export-interop): `src/legba/data/outputs/taxii_client.py:push_bundle_to_taxii`
POSTs the bundle's objects (as a TAXII envelope) to
`{server_url}/{api_root}/collections/{collection_id}/objects/` over the
structural HTTP port, and `stix_bundle.emit` invokes it behind the descriptor
`outputs.stix_bundle.config.taxii` binding (best-effort, degrade-not-drop). An
un-provisioned destination raises `TaxiiServerNotConfiguredError` (seam 10 — a
loud `RuntimeError` guard, not a stub).

**What does not exist.** Serving TAXII (a collections endpoint a peer can
poll), MISP sync, and signal→`observed-data` mapping. No live TAXII server is
provisioned, so no in-tree descriptor carries a real `taxii` binding yet.

**Honest note on current bindings.** The two descriptors that declared the
`outputs.stix_bundle` binding — `analyst_country_assessor` and
`analyst_country_predictor` — were both taken out of the live path by the P0–P4
sequencing (`country_assessor` **retired**, superseded by the units +
composition; the forecast-as-claim `country_predictor` **frozen**,
`fallback_schedule: null`). The producer and the emit dispatch are built and
exercised, but **no active analyst currently emits a bundle**; re-binding the
export to a live output (the units / compositions / the banded scorecard) is the
near step, not new plumbing.

**Chosen approach.**

1. **TAXII upload — DONE.** Direct HTTPS POST to
   `{api_root}/collections/{id}/objects/` (a TAXII 2.1 envelope) via the
   reused `httpx`-shaped HTTP port — no `taxii2-client` dependency added to
   the runtime image. Activates only when a descriptor `taxii` binding carries
   an operator-confirmed `server_url` (+ optional basic/bearer credentials).
2. **TAXII serve** as a thin read-only FastAPI router over stored bundles
   (per-target collections, the naming convention above), mounted on the
   registry behind a page-1 `read`-scoped token. No new service.
3. **MISP sync** as a push adapter (PyMISP): `report` → Event, `indicator` →
   Attribute, cited `identity`/`location` → MISP objects — driven off the same
   `OutputEnvelope` stream the exporter consumes, so MISP and TAXII stay
   consistent by construction.
4. **Signals stay out by default.** Mapping the raw signal pool to
   `observed-data` would export the firehose; findings are the product.
   Per-descriptor opt-in only. Prior art for the text→SDO conventions
   (entity extraction into identities, report typing) is Obstracts/txt2stix;
   the mapping table in `stix_bundle.py` follows the same shape.

**Integration points.** `export_outputs_to_stix`, `StixBundleExporter`,
`emit`, `upload_bundle_to_taxii` (all `src/legba/data/outputs/stix_bundle.py`);
the TAXII 2.1 push client (`push_bundle_to_taxii`, `TaxiiConfig`,
`TaxiiServerNotConfiguredError` in `src/legba/data/outputs/taxii_client.py`);
output-kind discovery (`discover_output_kinds`,
`src/legba/data/outputs/__init__.py`); the runtime emit dispatch
(`_emit_output_bindings` in `src/legba/runtime/dapr_actors.py`, threading the
shared `output_http_client`); payload models in
`src/legba/data/provenance/kinds.py` (`OutputKind` / `KIND_REGISTRY`).

**Status:** bundle producer **built**; TAXII **push built** (only the
destination server is unprovisioned — seam 10,
`TaxiiServerNotConfiguredError`); TAXII serve + MISP sync **designed, NOT
built**. The nearer product direction named above has since shipped: the
**markdown/JSON report export is now first-class** (`POST /v3/export` —
findings + journal entries with live-resolved citations, verify states,
evidence hashes, and receipt links); the STIX machinery is kept but stays out
of the daily flow until an emitter is re-bound.

---

## 4. MCP server

**Problem.** Legba's analytical surface should be callable as tools by any
MCP client (Claude Code first). The question is what exists versus what is
still HTTP-only.

**What exists (built).** `src/legba/ui/mcp_server.py` is a working stdio MCP
server (entry point `legba-mcp`, MCP protocol 2025-11-25 via the `mcp` SDK).
Its catalog is entirely descriptor-driven: `create_server` reads
`MCP_TOOL_REGISTRY` (`src/legba/data/outputs/mcp_tool.py`, L-377), which the
runtime populates at descriptor-activate time from `outputs.mcp_tool`
blocks. The registry gives: validated per-tool config (`McpToolConfig` —
tool-name shape, object-typed input schema), two dispatch modes
(`latest_output` via an injected `latest_output_provider`, and
`consult_on_demand` via an injected `on_demand_dispatcher` — the descriptor
path that restores the legacy `consult` tool), thread-safe register /
unregister-per-analyst, and `tools/list` + `tools/call` plumbing with unknown
tools answered by the available-catalog list rather than a crash.

**The gap.** Two-fold. First, the catalog is **process-wide, populated by the
runtime** — a standalone `legba-mcp` process sees an empty registry; it only
has tools when it shares a process with (or is fed by) the runtime. Second,
the daily-driver surfaces exist as HTTP, not MCP: consult
(`POST /api/v1/consult`, `consult_api.py::invoke_consult` — dispatches the
`consult_on_demand` analyst through its Dapr actor), substrate reads
(`GET /api/v1/findings|situations|signals`,
`substrate_reads_api.py::build_substrate_reads_router`), lineage walks
(`GET /api/v1/lineage/{row_kind}/{row_id}`,
`lineage_api.py::walk_lineage` — upstream/downstream/both BFS with depth cap).

**Chosen approach.** Make `legba-mcp` a thin **HTTP client of the registry**,
not an in-process peer of the runtime: a built-in tool set — `consult`,
`substrate_findings` / `substrate_situations` / `substrate_signals`,
`lineage_walk`, `since`, `export` — each wrapping the corresponding registry
endpoint with the registry bearer (page 1: a `read`-scoped token; `consult`
needs `operator`). Because these are constructed from code, a standalone
process serves them regardless of runtime population — which fixes the
standalone-empty problem. Descriptor-declared tools remain the SECOND catalog
source (merged + deduped by tool name; the built-in wins a name collision);
that source still only populates in a process that shares the runtime.
Transport: stdio first; HTTP/SSE transport later if a remote client
materializes.

**Integration points.** `create_server` (`src/legba/ui/mcp_server.py`);
the built-in tool set (`src/legba/ui/mcp_builtin_tools.py`);
`MCPToolRegistry.register_from_descriptor` / `.handle`
(`src/legba/data/outputs/mcp_tool.py`); the HTTP routers named above plus
`since_api.py` + `export_api.py`; `RegistryHTTPClient.request_json`
(`src/legba/runtime/registry_client.py`) as the reused HTTP-client wire.

**Status:** the seven built-in tools (`substrate_findings` /
`substrate_situations` / `substrate_signals`, `lineage_walk`, `since`,
`export`, `consult`) **BUILT** — `mcp_builtin_tools.py` + `create_server`
merge + `RegistryHTTPClient.request_json`, with unit tests
(`tests/data_pkg/test_mcp_builtin_tools.py`: request-build, response-shape,
standalone-non-empty, the reads+consult-only no-mutation assertion, error
passthrough, dedupe precedence). No registry mutations ride MCP — reads + the
sanctioned consult-run only (`export` is a read-only document composer).
Descriptor-tool plumbing + stdio server were already built. **Residual
caveats (honest):** transport is stdio only (HTTP/SSE not built); the built-in
tools are a thin HTTP client, so a standalone `legba-mcp` needs the registry
reachable at `LEGBA_REGISTRY_URL` (e.g. `--network legba_default`); and the
SECOND source (descriptor-declared tools) still lists empty in a standalone
process — only the built-ins survive without a shared-runtime process.

### Current setup

How to run the stdio server. A standalone `legba-mcp` process now lists the
seven **built-in** tools (`substrate_findings` / `substrate_situations` /
`substrate_signals`, `lineage_walk`, `since`, `export`, `consult`) with no
runtime present — they are code-constructed HTTP wrappers, not
runtime-populated state. Descriptor-declared tools (the SECOND source) still
list empty in a standalone process. Every tool fails loud: a registry 4xx/5xx
returns a *described* error object (status + detail), a transport failure
returns `registry_unreachable`, and an unknown tool call returns the
available-tool list — never fabricated output.

The built-in tools reach the registry over HTTP, configured from env (the same
names the bringup scripts use): `LEGBA_REGISTRY_URL` (the origin is taken from
it; the `/api/v1/registry` suffix is stripped since the tools address
`/api/v1/...` paths directly) and `LEGBA_REGISTRY_TOKEN` /
`LEGBA_REGISTRY_API_TOKEN` (bearer). A `read`-scoped token serves the six read
tools; `consult` needs an `operator`-scoped token. The `consult` tool is
long-running (it blocks up to 300s for the ReAct loop) and threads that timeout
to the registry.

An analyst descriptor surfaces an additional tool by declaring an
`outputs.mcp_tool` binding (`tool_name`, `description`, `input_schema`, and a
`mode` — `latest_output` returns the analyst's most recent output for the bound
scope; `consult_on_demand` triggers an on-demand analyst run with the call's
args). On descriptor activation the runtime calls
`MCPToolRegistry.register_from_descriptor`; on retire it unregisters. The
built-in + descriptor catalogs are merged and deduped by tool name (the
built-in wins). All tools are read-only or run-triggering — no registry
mutations ride MCP (asserted by
`mcp_builtin_tools.assert_reads_and_consult_only`).

Build the image and point the MCP client (e.g. Claude Code) at it, launched
per conversation. `--network=legba_default` is required so the container can
reach the registry host named in `LEGBA_REGISTRY_URL`:

```
docker compose --profile mcp build     # docker/Dockerfile.mcp → legba/legba-mcp:latest
```

```jsonc
{
  "mcpServers": {
    "legba": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--network=legba_default",
               "--env-file=/usr/local/deployments/active/legba/.env",
               "legba/legba-mcp:latest"]
    }
  }
}
```

Host-mode alternative (module or entry point):

```bash
cd /usr/local/deployments/active/legba
PYTHONPATH=src python -m legba.ui.mcp_server   # as a module
legba-mcp                                      # entry point, after pip install -e .
```

Transport is stdio (JSON-RPC); logging goes to stderr so stdout stays clean
for the protocol.

---

## 5. Multimodal for real

**Problem.** The schema and plumbing are modality-first — `Signal.modality` /
`mime_type` / `media_ref` / `retention_class` / `object_ref`
(`src/legba/data/sources/_contract.py`), a modality token in every NATS
subject, and a real async job plane for heavy extraction (`process_media`,
P-07: queue / worker / ledger / derived-signal landing all run for real) —
but the **extraction edges are not real models**. The former soft spots —
a `MediaClient` stub fallback and the eager tier's `EchoCaptionExtractor`
placeholder caption — were removed outright (review G3 close): with
`LEGBA_MEDIA_API_URL` unset, `MediaClient.extract` raises a typed
`MediaEndpointNotConfiguredError`, `process_media` enqueue and execution
refuse (terminal failed, no row written), and a `media: "eager"` descriptor
refuses activation. A stub result is structurally unrepresentable
(`MediaExtractionResult.source` is `Literal["hosted"]`). The loop side is
closed too: a landed derived signal is re-published into fan-out
(`event_class="derived"`) with parent geo/tags/entity_classes/language
inherited. What remains not-built — and declared in `docs/SEAMS.md` — is
the real extraction service behind the endpoint.

**Chosen approach.**

1. **Actual extractor endpoints, Whisper first.** The hosted models service
   (`docs/AI_MODELS.md`; the `NlpServiceClient` pattern in
   `src/legba/data/stack/nlp_service/client.py`) grows `/transcribe` backed by
   Whisper (`whisper-large-v3` is already the provenance label in
   `EXTRACTION_MODELS`). `MediaClient.extract` already POSTs `media_ref` to
   `EXTRACTION_PATHS["transcribe"]` and records the endpoint-reported model —
   a real endpoint is a config change (`LEGBA_MEDIA_API_URL`), zero plumbing.
   Then `/ocr`, then `/caption` (VLM), same client.
2. **Object store.** SeaweedFS, which the schema already names: a retain-tier
   handler that fetches bytes when `retention_class != reference_only`, sets
   `object_ref` to our copy, honors `media_ref_expires_at` with a sweep that
   exempts `evidence_hold` and anything referenced via `derived_from`.
3. **Non-text renderers.** UI-side rendering of `media_ref` / `object_ref`
   per modality (audio player + transcript signal, image + caption, video
   keyframes). This is UI-track territory (`docs/UI.md` describes the current
   panel surface); listed here because "multimodal" is not done until an
   operator can *see* the media.

**Integration points.** `MediaClient.extract` / `from_env` / `has_endpoint`
(`src/legba/runtime/jobs/media_client.py`); `MediaExtractor` protocol +
`default_extractor_registry` (`src/legba/data/sources/baseline.py`);
`process_media_handler` (`src/legba/runtime/jobs/process_media.py`);
retention fields on `Signal` (`src/legba/data/sources/_contract.py`).

**Status:** job plane **built**; extraction edges **guarded seams** (refuse
activation, per the parallel branch + `docs/SEAMS.md`); Whisper endpoint,
SeaweedFS retention handler, and renderers **designed, NOT built**.

---

## 6. Scale-out

**Problem.** Three single-node truths are baked in, all deliberate, all
documented here so nobody discovers them in an incident:

1. **NATS is R1.** `NatsStore.ensure_stream` (`src/legba/data/nats.py`)
   builds `StreamConfig` without `num_replicas` — every stream
   (`legba_signals`, interest retention, created by
   `subscription/engine.py::ensure_topology`; the `workqueue`-retention job
   stream from `runtime/jobs/queue.py::ensure_topology`) lives on one
   JetStream node. Node loss = stream loss (signals are also in Postgres;
   in-flight job messages are not).
2. **`signals` is a plain unpartitioned table** (`0024_pivot_substrate.sql`)
   with thirteen indexes. It now has a **scheduled TTL purge** (decision D4,
   graph-and-data Wave-1b): the `signals_retention` deterministic sub-handler
   (`deterministic_handlers/signals_retention.py`) deletes signals older than a
   configurable `ttl_days` and co-deletes their value-referenced children
   (`signal_entity_links`, `signal_aliases`) in one transaction so nothing
   orphans; `retain_always` / `evidence_hold` signals are never purged.
   Migration `0036_signals_retention.sql` adds the `(retention_class,
   fetched_at)` purge-scan index. The purge ships **disabled** (`ttl_days<=0`,
   the default) — an operator opts in by setting a positive TTL on the
   descriptor's `options`. Range-partitioning remains the long-term answer when
   volume warrants (heavy migration; not justified at PoC volume).
3. **The coalescer assumes one replica per pair.** Fire-claiming is already
   multi-worker-safe — `claim_fire` is an optimistic CAS (`UPDATE … WHERE
   last_fired_at IS NOT DISTINCT FROM <expected>`,
   `src/legba/runtime/triggers/state.py`) — but dirty accumulation is a
   read-modify-write: `Coalescer.on_signal` does `get` → `apply_dirty` →
   `save_dirty` (`src/legba/runtime/triggers/coalescer.py` →
   `state.py::save_dirty`, a last-writer-wins upsert). Two engine replicas
   processing signals for the same `(analyst, target)` can interleave and
   lose a pending-count increment. Lost updates here mean a *late* fire (the
   cadence ticker still sweeps dirty pairs), not a lost signal — but it is a
   correctness ceiling on replication.

**Chosen approach.**

1. **NATS R3**: 3-node JetStream cluster; `num_replicas` plumbed through
   `ensure_stream` (env-tunable, default 1 for the dev rig, 3 in production
   topology). Interest/workqueue retention semantics are unchanged by
   replication.
2. **Partition `signals` by `fetched_at`** (native range partitions,
   monthly), which matches the existing index access patterns
   (`signals_fetched_at_idx DESC`). Retention = detach-and-archive old
   partitions, with a referenced-rows exemption (anything reachable from a
   `derived_from` array or under `evidence_hold` is copied forward before
   detach). Plain native range partitions — no extension dependency — keep
   the migration chain boring.
3. **Multi-replica coalescer**, two acceptable designs, decided by load
   shape: (a) **partition the pairs** — deterministic subject-hash assignment
   of `(analyst, target)` to engine replicas via filtered durable consumers,
   keeping the RMW single-writer per pair; or (b) **make `save_dirty`
   atomic** — move the merge into SQL (`pending_count =
   trigger_state.pending_count + 1`-style arithmetic upsert plus a
   SQL-side seen-set union) so interleaving cannot lose counts. (a) is the
   default choice: it preserves the in-memory accumulator semantics and the
   seen-cap eviction order without a JSONB-merge in SQL. `claim_fire`'s CAS
   already holds for N replicas either way.

**Integration points.** `ensure_stream` (`src/legba/data/nats.py`);
`ensure_topology` (`src/legba/runtime/subscription/engine.py`,
`src/legba/runtime/jobs/queue.py`); `signals` DDL
(`src/legba/data/migrations/0024_pivot_substrate.sql`);
`TriggerStateStore.save_dirty` / `claim_fire`
(`src/legba/runtime/triggers/state.py`); `Coalescer.on_signal`
(`src/legba/runtime/triggers/coalescer.py`).

**Status: designed, NOT built.** R1, unpartitioned, single-replica coalescer
assumption — all stated in code comments and here.

**Adjacent (also designed, NOT built): Docker Swarm conversion assessment
(2026-07-25).** A separate, smaller scale-out step — moving the data layer
(PG/Qdrant/OpenSearch/NATS/redis) to a second node via `docker stack deploy`
— was assessed on paper; it does not touch the three truths above (a data-node
*move* changes where R1 NATS lives, not that it is R1). Draft stack files live
under `deploy/swarm/` (explicitly non-deployable until phase-1 validation);
the per-service inventory, Dapr-on-swarm risk analysis, k3s fallback triggers
and the phase 1/2/3 runbook are in
`planning/SWARM_CONVERSION_ASSESSMENT_2026-07-25.md`. Nothing is deployed;
the compose file + deploy.sh remain the only live path.

---

## 7. Fallback-model budget demotion (decision F-2)

**Problem.** When an analyst exhausts its token budget, the declared strategy
`demote_and_continue` promises "keep running on a cheaper model." The actor
machinery for this is **fully built**: `_AnalystDeps` carries
`fallback_run_method` / `fallback_kind_deps` / `primary_llm_ref` /
`fallback_llm_ref` (`src/legba/runtime/dapr_actors.py`), the pre-call budget
check (`BudgetEnforcer.precall_check`, `src/legba/runtime/budget.py`) feeds a
strategy dispatch that writes an audit row to `budget_demotion_events`
(migration `0022_global_budget_envelope.sql`) via
`BudgetEnforcer.record_demotion`, marks the actor demoted for the rest of the
bucket (`_ANALYST_DEMOTED_UNTIL` / `_GLOBAL_DEMOTED_UNTIL`), and the run path
swaps to the fallback deps and stamps `options["llm_demoted"] = True` +
`options["llm_ref"]` so demoted output is distinguishable in provenance.

What is **not** built is the supply side: the production deps builder
(`src/legba/runtime/analyst_deps_builder.py`) resolves only
`method.llm["primary"]` and never populates `fallback_run_method`. The actor
code handles this case explicitly — `demote_and_continue` **without a fallback
wired** falls into the `pause_until_next_window` branch: cooldown +
`BUDGET_THROTTLED`, after the audit row. So today, `demote_and_continue` ===
**an explicit, audited pause-until-window**. That degradation is deliberate:
no fallback wired means pause loudly, never silently keep burning the
expensive model and never fabricate a cheaper run that does not exist.

**Chosen approach.** Wire `method.llm.fallback` end-to-end in the deps
builder, symmetric with primary:

1. Descriptor: `method.llm.fallback: <stack_ref>` (the `llm` dict is open;
   the key is already named by the `BudgetRetryPolicy` docstring in
   `src/legba/data/schemas/analyst.py` — "auto-demote to
   `method.llm.fallback`").
2. Deps builder: resolve the fallback StackRef through the same stack-registry
   path as primary, construct a second LLM client + kind-deps bundle, and
   populate `fallback_run_method` / `fallback_kind_deps` /
   `fallback_llm_ref` on `_AnalystDeps`. Kinds whose deps embed the LLM
   (the `llm_single_turn` / `react_loop` builders) get the same builder
   invoked twice with a different stack ref — no new kind code.
3. Accounting: demoted runs still meter into the budget ledger at the
   fallback model's per-token rate (cost model, migration
   `0015_cost_model.sql`) — demotion reduces spend, it does not stop
   metering. Demotion already clears on bucket rollover or explicit
   `clear_analyst_demotion`.
4. Validation: a descriptor declaring `strategy: demote_and_continue` without
   a resolvable `method.llm.fallback` should fail activation (refuse, loud)
   rather than silently behaving as pause — today's silent equivalence is
   acceptable only while nothing can wire a fallback at all.

**Integration points.** `BudgetRetryPolicy`
(`src/legba/data/schemas/analyst.py`); the strategy dispatch + demotion state
in `src/legba/runtime/dapr_actors.py`; `BudgetEnforcer.precall_check` /
`record_demotion` (`src/legba/runtime/budget.py`); the primary-LLM resolution
to mirror in `src/legba/runtime/analyst_deps_builder.py`.

**Status:** actor-side demotion machinery **built**; fallback wiring
**designed, NOT built** — `demote_and_continue` currently equals an audited
`pause_until_next_window`. Note (2026-07): the faithfulness judge now resolves
through its own opt-in route (`LEGBA_JUDGE_STACK_REF` env >
`method.llm.judge` > `.verify` > `.primary` — the judge-route separation), so
the same future second model can serve both the judge route and this fallback
slot; F-2 itself still waits on that absent second model.

---

## 8. Deep-crawl discovery jobs (decision F-1)

**Problem.** Discovery — finding new targets and new sources — has two
candidate execution routes in the tree, and only one of them is real. The
**shipped path is registry-route**: a discovery descriptor runs inside
`TargetActor._run_discovery_cycle` (`src/legba/runtime/dapr_actors.py`),
resolves its kind via the discovery-kind registry
(`discover_discovery_kinds`, `src/legba/data/discovery/registry.py`), drains
`discover(ctx)` into candidates, and hands them to the materializers —
`reconcile_discovered_targets`
(`src/legba/data/registry/discovered_materializer.py`) for targets, and the
source-side twin with **validate-before-register**
(`src/legba/data/discovery/source_materializer.py`) for sources. The shipped
kinds are list/query-driven: `country_list_discovery`, `file_sd_discovery`,
`query_source_discovery`.

The second route was a dangling promise: `KNOWN_JOB_KINDS`
(`src/legba/data/jobs/envelope.py`) documents `crawl_discovery` /
`query_discovery` as job kinds, but `default_dispatch`
(`src/legba/runtime/jobs/dispatch.py`) registers only `process_media` — an
enqueued discovery job would die `failed: no handler`. The agency tool that
performed that enqueue (`discover_sources_tool`,
`src/legba/data/analysts/agency/tools.py`) is **being dropped this wave**
per decision F-1: an enqueue with no consumer is a silent dead end, which is
exactly the failure mode the no-stubs rule exists to kill. The job kind
strings stay documented; nothing enqueues them until something consumes them.

**Chosen approach.** Deep crawl returns as a **job handler**, not as a new
descriptor route:

1. Register `crawl_discovery` in `JobDispatch`. Envelope `input_refs`:
   `{seed, depth, max_pages, allow_patterns, max_sources}` plus the
   governor's crawl caps — the generic `JobEnvelope` already carries
   `budget_account`, `tenant_id`, and an idempotency key, so a re-requested
   crawl of the same seed collapses.
2. The handler walks the frontier using the **existing acquisition
   handlers** (`firecrawl` / `scraper` source kinds under
   `src/legba/data/sources/`) rather than a new crawler — fetch, extract
   candidate feed/source URLs, score.
3. Output converges with the registry route: the handler emits
   `CandidateSource` rows into the **same validate-before-register
   materializer** (`source_materializer.py`) the descriptor-driven kinds use.
   One registration gate, regardless of how a candidate was found. Discovered
   descriptors carry the `job_id` as provenance.
4. Only after the handler exists does any enqueue return — agency tool,
   operator endpoint, or a `crawl`-flavored discovery kind that *defers* its
   heavy walk to the job plane (the long-cycle crawl does not belong inside
   an actor's `run()` slot; the job plane's worker pool and ledger are built
   for exactly this).

**Integration points.** `JobEnvelope` / `KNOWN_JOB_KINDS`
(`src/legba/data/jobs/envelope.py`); `JobDispatch.register` /
`default_dispatch` (`src/legba/runtime/jobs/dispatch.py`); the worker pool
(`src/legba/runtime/jobs/worker.py`); `CandidateSource` + the
validate-before-register flow (`src/legba/data/discovery/source_materializer.py`,
`source_validate.py`); `query_source_discovery`'s docstring, which already
names the crawl flavor as a sibling kind behind the same protocol.

**Status: designed, NOT built.** Registry-route discovery is the shipped
path; the deep-crawl job handler is the design above; the dangling enqueue is
removed rather than left to fail downstream.

## 9. Data-integrity sweeps (re-homed as `integrity_sweep` — BUILT)

**Problem.** The pre-pivot `integrity_verification` deterministic sub-handler ran
eight referential-integrity checks, but its first check anchored on the dropped
`events` table — so in production the whole sweep aborted, the error was
swallowed, and it emitted a zeroed "no issues" finding (fake success). It was
deleted under the no-stub rule (review 2.4).

**What re-homed.** A git-archaeology + live-schema pass found that most of the old
checks referenced tables the pivot dropped — `events`, `signal_event_links`,
`situation_events`. So only the checks that genuinely run against live pivot-era
tables survive, with the integrity sweep re-homed onto the current schema:

  - orphan `signal_entity_links` (signal-side + entity-side) — unchanged.
  - orphan `proposed_edges` (`source_entity` / `target_entity` absent from
    `entity_profiles.canonical_name`) — checks the pivot's candidate-edge table.
    Finds real drift live (7 + 24 orphans at build time).
  - `facts` with no `evidence_set` — unchanged.
  - broken `analyst_outputs.superseded_by` — finding-pool supersession integrity.

> **Correction (the data-analysis rigor arc, §5.7 of `ARCHITECTURE.md`).** An
> earlier draft of this page claimed `facts.superseded_by` and the `nexuses` table
> had been dropped. **Both are live.** Migration `0032` restored
> `facts.superseded_by` / `valid_from` / `valid_until` / `confidence_components`
> (temporal facts), and `0033` created the reified, typed, signed `nexuses` table
> (read by `structural_balance` / `graph_mining`). The integrity sweep's
> `proposed_edges` check is the candidate-edge guard; it does **not** stand in for
> a missing `nexuses` table, which exists and carries its own open-only valid-row
> index. See `ARCHITECTURE.md` §5.7 (the data-analysis rigor layer).

**Status: BUILT.** Shipped as the `integrity_sweep` deterministic analyst
(`deterministic_handlers/integrity_sweep.py` + `descriptors/analyst_integrity_sweep.yaml`
+ `scripts/bringup_register_integrity_sweep.py`), hourly cadence, META/global. It
**refuses loud** — a missing relation propagates instead of zeroing, so a 0-issue
finding is a *genuine* clean sweep, never an aborted one. It is **read-only**
(counts drift, emits an honest summary finding each run, tagged `integrity_clean`
vs `integrity_issues_present`); the predecessor's destructive auto-repairs are
deliberately NOT re-homed — surfacing the counts for an operator is the safe
first step. (The deterministic output contract requires a finding per run, so it
emits an honest summary every cadence tick rather than only when issues exist.)

## 10. Knowledge grounding — current-world-state injection (BUILT; Tier 2 designed)

**Problem.** The analyst plane's core LLM has a training cutoff that predates the
present, so on any assessment that turns on *current* world state — officeholders,
in-force alliances, the present state of an ongoing conflict — it backfills from a
stale prior. The live failure that forced this: the assessor called the current US
president a "former" president (its training data predates the 2024 election), and
the signal slice (recent headlines) rarely restates a background fact like "X is the
head of state", so the model had no in-context correction. The shipped design
is described in `ANALYSIS.md` §7.9.

**Chosen approach — the substrate is the grounding store.** Legba already stores the
temporally-honest answer (temporal `facts` with `valid_from`/`valid_until`/
`superseded_by`, signed `nexuses`, and the seed roots). The fix is **curate the
current data in, then inject it at analysis time** — no new store, no fine-tune.
Three tiers:

1. **Tier 0 — curate in (BUILT).** The `wikidata_leaders` seed adapter
   (`src/legba/data/seed/adapters/wikidata_leaders.py`) pulls current heads of
   state/government from live Wikidata SPARQL and emits a **country-subject** office
   fact, now split by office type — `'<country>' | 'head of state' | '<leader>'`
   (Wikidata P35) distinct from `'<country>' | 'head of government' | '<leader>'`
   (P6) — keyed on the country, so a leader change *supersedes* the prior
   officeholder via the Phase-B `valid_until` write path; the curated
   `world_baseline` adapter emits the same shape. A bare-QID leader (the SPARQL
   label service fails for some) is resolved via a `wbgetentities` label lookup +
   enwiki-sitelink fallback; un-resolvable QIDs are dropped, never injected. The
   flaky alliances query soft-degrades rather than aborting the seed. Live-verified
   after re-seed: monarchies correctly carry a head-of-state (P35) distinct from
   their head-of-government (P6); each officeholder fact carries its own
   `valid_from`, superseding the prior holder on a change.
2. **Tier 1 — inject at analysis time (BUILT).** An opt-in `GroundingBlock`
   (`src/legba/data/schemas/analyst.py`; off by default) installs a deps-builder hook
   (`analyst_deps_builder._build_grounding_hook` → `SubstrateGroundingResolver`,
   `src/legba/runtime/grounding.py`); the `inline_target` GROUND phase prepends a
   dated "AUTHORITATIVE CURRENT CONTEXT (treat as ground truth over prior knowledge)"
   preamble built from the current authoritative facts (the temporal-honesty gate,
   preferring `seed`/`curated` provenance) about the target geo + slice entities.
   Degrade-not-drop, token-capped, bare-QID-skipping. Opted in on **all eight bounded
   units** (`leadership_transition` / `energy_security` / `escalation` /
   `narrative_coordination` / `internal_stability` / `military_posture` /
   `economic_coercion` / `proliferation_watch`) — the grounding was **ported off the now-retired
   `country_assessor` monolith onto the units** (2026-07-01), which also widened the
   raw-signal window to 72h so a unit integrates the multi-week substrate, not only
   the fresh slice. The per-country / per-region / world compositions read
   already-grounded, already-verified units and so need no preamble of their own.
   Canary passed live.
3. **Tier 2 — vector `world_context` collection (BUILT / LIVE).** A curated
   unstructured-brief collection for free-text background the structured facts can't
   carry. The `GroundingBlock` accepts `vector:world_context` as a source, and — with the
   embedder-through-port wiring (L-114) now landed — the resolver retrieves from the
   curated `world_context` Qdrant corpus (~293 chunks; a `tradecraft` corpus of ~1716
   chunks also exists) through the stack embedder port (bge-m3, 1024-dim): a separate,
   non-citable grounding preamble, opportunistic, relevance-floored, country-filtered,
   degrade-not-drop when the corpus is empty. It is **staggered on** — currently enabled
   for `leadership_transition` + `internal_stability` (their `grounding.sources` include
   `vector:world_context`), pending review-gated expansion.

**Integration points.** `GroundingBlock` (`src/legba/data/schemas/analyst.py`);
`SubstrateGroundingResolver` / `build_grounding_preamble` /
`collect_grounding_candidates` (`src/legba/runtime/grounding.py`); the GROUND phase
(`src/legba/data/analysts/inline_target.py`); `_build_grounding_hook`
(`src/legba/runtime/analyst_deps_builder.py`); the seed adapters
(`src/legba/data/seed/adapters/wikidata_leaders.py`, `world_baseline.py`); the RAG
loader (`src/legba/data/rag/`); the nine unit descriptors carrying the `grounding:`
block (`descriptors/analyst_leadership_transition.yaml`, `analyst_energy_security.yaml`,
`analyst_escalation.yaml`, `analyst_narrative_coordination.yaml`,
`analyst_internal_stability.yaml`, `analyst_military_posture.yaml`,
`analyst_economic_coercion.yaml`, `analyst_proliferation_watch.yaml`,
`analyst_disruption_status.yaml`).

**Status:** Tier 0 + Tier 1 **built** (deployed + canary-verified live); Tier 2
(vector `world_context`) is now **BUILT / LIVE** — the embedder-through-port wiring
(L-114) landed and RAG is staggered on for `leadership_transition` + `internal_stability`
(SEAM #11 resolved). See `ANALYSIS.md` §7.9 and `DESIGN.md` §3.4.

**Adjacent confidence dynamics (2026-07, cross-refs).** Two built readouts now
sit beside the grounding store's confidence story, both consumption-flag-gated
**OFF in code**: (1) **fact decay** — `fact_decay_scan` (draft) computes
per-class confidence-decay curves with corroborations as clock-resetting
sightings into a `fact_decay_states` sidecar, and the grounding read can carry
that decay annotation on injected facts behind `LEGBA_FACT_DECAY_WEIGHTING`
(default OFF; stored confidence is never mutated); (2) the **earned track
record** — `source_track_record` (draft) scores each source's win/loss over
resolved contentions, consumable in arbiter tie-breaks behind
`LEGBA_CONTENTION_EARNED_WEIGHT` (default OFF; never touches faithfulness).
Both are honest-state rows in `STATUS.md`.
