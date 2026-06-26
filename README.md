<p align="center">
  <img src="logo_small.png" alt="Legba" width="400">
</p>

<h1 align="center">Legba</h1>
<p align="center"><em>A source-first platform for automated analysis &amp; knowledge fusion over whatever sources you can reach.</em></p>
<p align="center">
  <a href="docs/GLOSSARY.md"><b>Glossary</b></a> ·
  <a href="docs/FAQ.md"><b>FAQ</b></a> ·
  <a href="docs/DIRECTION.md">Direction</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a>
</p>

Legba ingests data from any source — pulled on a cadence, or pushed to a webhook — and **fuses** it into a provenance-tracked knowledge substrate — the classic data/knowledge-fusion lineage of **signals → entities/facts → relations/nexuses → situations → per-target assessments**, where every output is fully traceable to its sources via lineage and hash-chained receipt chains. It turns declarative descriptors — **sources**, **targets**, and **analysts** — into running actors over a shared substrate. A Dapr virtual-actor runtime reads each registered descriptor and stands up the corresponding actor; those actors acquire raw observations, route them, reason over them, and write findings back to the substrate. There is no code to write per feed, per target, or per analysis: you declare what to watch and how to reason, register it, and the runtime executes it. Everything is content-hashed, Ed25519-signed, and carries full provenance.

The moat is **provenance, auditability, and the descriptor-driven, source-first, self-hostable (AGPL) model** — not data access or analytic maturity. **Geopolitical / G20 country assessment is the proven exemplar use case**, not the system's identity: the same source → enrich → fan-out → assess pipeline applies to any domain you can point a source at. Legba does **not** claim sensor-fusion or track-correlation rigor; it is situation-assessment over text and structured data.

## The source-first model

**Ingest once, enrich once, match many.** Acquisition belongs to **sources**, not targets. A `SourceActor` polls on a Dapr reminder (or receives a webhook push), produces **one canonical, target-agnostic signal**, enriches it once (baseline: language detection, geo, and entity NER), and publishes it once to NATS JetStream. Signals carry **no `target_id`** — they are observations, not interpretations.

**Fan-out routes each signal to many subscribers by predicate.** A target is a passive subscriber: it declares a `SourceRef` (an explicit `source_id` or a `source_selector` predicate) and a `subscription_policy` (`open` / `allowlist` / `grant`). The fan-out plane delivers each published signal to every matching target — coarse NATS subjects (tenant / source / modality / event-class) narrowed by a SQL `WHERE` clause and a Starlark residual predicate. One BBC feed fans out to nineteen country targets without re-fetching anything.

**Analysts coalesce matched signals into findings.** An `AnalystActor` accumulates the signals routed to its target and fires on a coalescing trigger — an accumulation threshold plus a severity gate, clamped by a cooldown, with a cadence heartbeat as the coverage floor. Both deterministic *and* LLM-bearing analysts fire reactively: the trigger policy floors an LLM analyst to a coalesced accumulation batch (never per-signal), so it reacts to breaking accumulation as well as running on cadence. A target-bound analyst executes its matched targets **concurrently** via per-(analyst, target) worker actors (bounded fan-out), so a wide analyst over many countries doesn't serialize. It produces **findings, situations, hypotheses, predictions, and critiques**, each carrying full `derived_from` provenance and a per-analyst hash-chained receipt chain (Ed25519-checkpointed).

### The four planes

1. **Acquisition** — `SourceActor` ownership of polling/push, baseline enrichment, and the fan-out / subscription engine. Signals are **modality-first** (`text` / `image` / `audio` / `video` / `structured` / `binary`); a dozen-plus source-handler kinds feed the same canonical `Signal` shape.
2. **Analysis** — `TargetActor` subscribers, `AnalystActor` reasoning, coalescing triggers, action-pack agency with a per-pack governor and a per-analyst token budget.
3. **Async jobs** — a NATS work-queue (`LEGBA_JOBS`) with competing-consumer workers; `process_media` is the live job kind (source discovery runs via the registry route, not the job plane).
4. **Substrate** — Postgres + Apache AGE (relational + entity graph), Qdrant (vectors), Redis (hot state), NATS JetStream (event bus + durable streams). Time-series metrics (observability) and full-text search (BM25) are **declared seams — not built**: there is no metrics store, and full-text `search_signals` uses Postgres FTS (see [docs/SEAMS.md](docs/SEAMS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §5.5).

## Proven core vs experimental

Legba draws a hard line between what is the **proven product** and what is **built but unproven research**. Read the front door this way:

**The proven core — the product.** Source acquisition → baseline enrichment → predicate fan-out → per-target coalescing into findings, with full provenance (lineage + Ed25519-checkpointed receipt chains), temporal facts (supersession), reified signed relations, and the operator UI. This is the source-first knowledge-fusion pipeline, proven end-to-end from a cold start through to per-country assessments (the G20 exemplar). When this README says "proven", it means this path.

**Experimental / research — built, traceable, but NOT proven capabilities.** Calibration and outcome-resolution, the GEPA prompt optimizer, ACH competing-hypotheses scoring, and the advanced graph analytics (structural balance / graph mining) are all **built and write real traceable rows**, but none has a validated skill metric. They are research surfaces, not claimed capabilities — in particular **no forecast-accuracy / Brier-skill claim is made**. They are kept and labeled as experimental throughout; see the [release-boundary table](#release-boundary) for the per-feature state. The one *falsifiable path* to earning that claim is now wired: a **pre-registered weekly binary call** — `P(≥1 severe hazard event)` per G20 country at a fixed 7-day forward horizon, resolved **exogenously** against the upstream hazard catalogs (USGS / NWS / NASA) by their own event timestamps, scored by **Brier skill score (BSS)** against per-country climatology. The pilot's number lives in its own segregated key (`brier_forecast_acute`), is never pooled into the headline calibration Brier, and the project earns the word "forecast" **only when BSS > 0 on a non-degenerate sample** — the finding carries an explicit **degeneracy guard** that withholds the claim when the calls are geography-dominated (only ever near-0/near-1 certainties), since beating climatology on "which countries are seismic" is static geography, not anticipating the future. The first live seeding has already exercised this: at the country-week granularity the hazard catalogs are geography-dominated, so the finding honestly reports `forecast pilot: degenerate / accumulating` and makes **no skill claim** — the harness (exogenous resolution + Brier + BSS) is real and deployed, awaiting either a longer record or a finer-grained, genuinely-uncertain task variant.

## Quick start

> **Clean-slate only — no migration path from pre-pivot Legba.** This is a complete
> refactor from the v1/v2 target-first design; the data model, substrate schema, and
> APIs are incompatible with pre-pivot instances. There is no upgrade or data-migration
> path: stand up a fresh empty substrate (Postgres+AGE, NATS JetStream, Qdrant, Redis)
> and apply migrations from 0001_baseline forward. Do not point this build at a pre-pivot
> database.

Canonical bring-up is container-mode. The substrate is always-on; Dapr and the app images activate under the `runtime` profile. **Ordering is load-bearing: migrate → register the stack → register the working set → *then* boot (or `--force-recreate`) the runtime.** The runtime builds its NLP / embedding clients **once at boot** from the registered stack; bring it up against an un-seeded registry and `nlp_client` stays `None` for the whole process lifetime — signals land with no geo/entities and geo-scoped analysts never match. Read [docs/RUNBOOK.md](docs/RUNBOOK.md) **§0 (critical operator notes)** first, then §2–§9 for the full procedure.

> **Canonical one-command deploy: [`deploy/deploy.sh`](deploy/deploy.sh).** It runs
> the entire phased, idempotent bring-up below — preflight → schema (the single
> proven baseline `deploy/baseline/0001_baseline.sql`) → the ordered registrars →
> optional seeds → runtime → boot-verify — in the proven load-bearing order:
> ```bash
> docker compose --profile runtime build      # one-time image build
> deploy/deploy.sh                             # real stack (project legba), no seeds
> deploy/deploy.sh --seed                      # + curated knowledge seeds
> ```
> The manual steps below are the same sequence the script automates, kept for
> reference / partial re-runs. For a throwaway clean-slate validation stack on
> the same host, use `--project legba_val --no-caddy` (data-isolated; see
> [docs/SETUP.md](docs/SETUP.md) and [docs/RUNBOOK.md](docs/RUNBOOK.md)).

```bash
cd /usr/local/deployments/active/legba

# 1. Build the app images (one-time, or after a code change; layer-cached on re-run).
docker compose --profile runtime build

# 2. Bring up the substrate + the registry only — NOT the runtime yet.
#    (Substrate is profile-less; this also starts the registry so we can seed.)
docker compose up -d                              # redis / postgres / qdrant / nats
docker compose up -d legba-registry               # registry API (no runtime actor host)

# 3. Apply migrations (idempotent; runs against the substrate Postgres).
#    The only CLI flag is --dry-run; there is NO --primary-only flag.
docker exec legba-legba-registry-1 python -m legba.data.migrate

# 4. Load credentials into the encrypted vault + register the substrate stack.
docker exec legba-legba-registry-1 python scripts/bringup_vault_load.py
docker exec legba-legba-registry-1 python scripts/bringup_register_stack.py

# 5. Register the fresh source-first working set in one shot:
#    3 shared news sources + 19 G20 country targets + 4 analysts + action packs.
docker exec legba-legba-registry-1 python scripts/bringup_register_p17_workingset.py

# 6. NOW boot the runtime + Dapr + UI + Caddy, against the seeded registry, so the
#    runtime builds its nlp_client. --force-recreate is required if the runtime was
#    ever booted earlier in this lifetime (it pins nlp_client=None until recreated).
docker compose --profile runtime up -d --force-recreate
```

> If you already ran `docker compose --profile runtime up -d` before seeding, you
> must `--force-recreate legba-runtime-dapr` after seeding (per RUNBOOK §0) — a plain
> restart will not rebuild the boot-time `nlp_client`.

**What you get.** This minimal working set registers three shared sources (BBC World, Deutsche Welle, Al Jazeera) as a deliberately small, easily-verified **cold-start verification set** — *not* the limit of the catalog and *not* the proven-live scope. The full source catalog (`scripts/bringup_register_source_catalog.py`) **defines 46 sources** (43 news RSS + 3 geo-hazard GeoJSON) across the dozen-plus handler kinds; **it is a separate manual registration step — not auto-run on deploy and not part of the working-set bring-up above** — so run it to reach the current/full deployed scope. The three RSS feeds are the minimal end-to-end bootstrap a fresh instance lights up first; a fresh instance reaches current scope by registering the full 46-source catalog, not by stopping at the 3-feed minimal set. See [docs/SOURCES.md](docs/SOURCES.md) for the catalog and [docs/SETUP.md](docs/SETUP.md) for the registration command. Those sources begin polling and publishing enriched, target-agnostic signals; the fan-out plane routes them by geo predicate to the nineteen G20 country targets, each coalesced by a `country_assessor` analyst into per-country findings with provenance and receipt chains. An ongoing `entity_resolution` deterministic analyst keeps an entity knowledge graph (`entity_profiles` / `signal_entity_links` / `proposed_edges`) current. Cold-start from empty volumes through to per-country findings is proven end-to-end from a single baseline schema migration.

**Validated scope (live deployment).** The deployed instance has run far beyond the 3-feed bootstrap: **49 distinct sources** actively producing signals (the 46-source catalog plus seed/baseline adapters) → **54,197 signals** ingested → **19,629 findings** produced → **3,019 facts** · **3,822 nexuses** · **25 situations** · **398 hypotheses**.

The operator UI is served by Caddy on `:443` (auto-HTTPS, basic-auth perimeter); the registry API is on `:8090` behind a bearer token. The runtime reaches the LLM, embedding, NER, and translation models over HTTP — none run in-container (see [docs/AI_MODELS.md](docs/AI_MODELS.md)).

```bash
# Confirm signals are landing + analysts are producing output:
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT count(*) FROM analyst_outputs;"
```

## Features

- **Descriptor-driven.** Sources, targets, analysts, and action packs are declarative descriptors. A content-hashed, Ed25519-signed registry stores every instance, emits NATS events on change, and routes validation failures to a DLQ. Add / pause / retire at runtime — no deploys for content changes.
- **Shared substrate with universal provenance.** Every derived row carries `target_id`, `analyst_id`, version stamps, and a `derived_from` UUID array. Cross-target reasoning is a query over the substrate, walkable via the `/api/v1/lineage` endpoint. Per-analyst hash-chained receipt chains (Ed25519-checkpointed) make outputs auditable.
- **Source-first fan-out.** One enriched signal is matched to many targets by predicate — coarse NATS subjects narrowed by SQL `WHERE` plus a Starlark residual. Targets subscribe; they do not pull.
- **Many source kinds, one signal shape.** A dozen-plus handlers — `rss`, `gdelt_query`, `acled`, `mediacloud`, `opensanctions`, `scraper`, `firecrawl`, `telegram_channel`, `discord_webhook`, `common_crawl_news`, `intelmq_collector_bridge`, `generic_webhook`, `json_api`, and `geojson` — all yield the same canonical `Signal`. Acquisition is poll or push (`stream` is a documented future seam); see [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
- **Multimodal seam.** Signals are modality-first; the modality → (ingest extractor, UI renderer) registry is scaffolded with `text` live and the rest keyed for drop-in. The new `geojson` source is the first **model-free** non-text modality: it emits `structured` / `application/geo+json` signals (geometry inlined, coords promoted to the `geo` column) with no extraction model in the loop. The MapLibre renderer for these is still a badged placeholder; eager media extraction (Whisper / VLM / OCR) is a future seam.
- **Action-pack agency.** Analyst capabilities (tools / prompt fragments / rules / channels / governor / applicability) ship as modular, allow-listed bundles. A capability is usable only in the intersection `analyst.action_packs ∩ target.allowed_action_packs ∩ pack.applicability`, gated by a per-pack governor and a per-analyst daily token budget.
- **Eval loop (experimental).** Critic analysts grade outputs against rubrics; an optimizer analyst tunes prompt modules via a GEPA loop that runs as a **Dapr Workflow** on the daprd sidecar. The loop **generates candidates**; promoting a champion candidate to the live system prompt is **human-gated through the descriptor lifecycle and never auto-promotes by default**. This is a research surface — built and traceable, not a proven optimization capability (see [Proven core vs experimental](#proven-core-vs-experimental)).
- **On-demand consult engine.** A ReAct `consult_on_demand` analyst answers ad-hoc operator queries against the live substrate (`POST /api/v1/consult`).
- **First-person reflective journal.** A `journal_assessor` extension analyst — Legba's one reflective voice, pointed at the whole organism (its own self / state / flow) rather than a single target. It narrates a coherent point of view *over* the rest of the system in two tiers (a 12-hourly **entry** + a daily **consolidation** that distils prior entries into one forward-carried narrative). It is **off the fact/finding/nexus chain** — a journal row is a perspective *over* the provenance chain, never a member of it (it lands in its own `journal_entries` table with an always-empty `derived_from`, excluded from the lineage catalog). It writes only its own entries; every outward effect (a correction, a change, a self-revision) goes to a **human-gated propose-and-gate** queue, never a live table. *"Poetry without evidence is noise. Evidence without perspective is just a log file."*
- **Operator UI.** A Dockview single-page app (served by Caddy) surfacing findings, situations, predictions, the journal, the actor roster, lineage walks, the entity graph, budget ledgers, and inline descriptor editing — see [docs/UI.md](docs/UI.md). A unified **Live Feed** panel (`system.findings`) merges findings and signals from two NATS tails (`analyst.*.finding` + `legba.signals.>`) into one stream, with a Live on/off toggle, a Source filter (All / Findings / Signals), and near-duplicate clustering. A **System Status** panel (`system.status`) rolls per-source firing, per-analyst cadence (read from `analyst_traces`, not the NULL `actor_state.last_run_at`), queue backpressure, and infra reachability into one per-component / per-layer health page.

## AI models

Inference is hosted out-of-process via the `legba-models` service: a vLLM-served LLM (gpt-oss-120b), `BAAI/bge-m3` embeddings, NLLB translation, and spaCy / GLiREL NER. LLM providers (Anthropic / vLLM / OpenAI) are resolved through the stack registry, so a descriptor names a provider rather than hard-coding an endpoint. None of these models run inside the Legba containers. Full inventory and call patterns in [docs/AI_MODELS.md](docs/AI_MODELS.md).

## Documentation map

| Document | What it covers |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | Implementation design — core abstractions, data flows, decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Conceptual orientation — the four planes and why the system is shaped this way |
| [docs/ACQUISITION.md](docs/ACQUISITION.md) | The acquisition plane — `SourceActor`, baseline enrichment, fan-out / subscription |
| [docs/ANALYSIS.md](docs/ANALYSIS.md) | The analysis plane — targets, analysts, coalescing triggers, action-pack agency |
| [docs/SOURCES.md](docs/SOURCES.md) | The source catalog — the 3 / 46 / 49 scope model and the per-source table of what Legba ingests |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Source handler kinds and the data sources reachable through them |
| [docs/AI_MODELS.md](docs/AI_MODELS.md) | The hosted models, providers, and how the runtime reaches them |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operator runbook — bring-up, migrations, registration, troubleshooting |
| [docs/CODE_MAP.md](docs/CODE_MAP.md) | Code map — modules, function flows, dependencies |
| [docs/UI.md](docs/UI.md) | Operator UI guide — panels, auth chain, daily-driver workflow |
| [docs/DIRECTION.md](docs/DIRECTION.md) | Engineering direction — designed-not-built: RBAC/SSO, tenancy, STIX/TAXII, MCP, multimodal, scale-out, budget fallback, deep crawl |

## Key abstractions

The **descriptor registry** (content-hashed, signed audit log, DLQ, NATS events), the **stack registry** (substrate components) and **credential vault** (XSalsa20-Poly1305), **action-packs**, a polymorphic **`TargetScope`** (`GeoScope` / `EstateScope` / `EntityScope`), the **Starlark predicate DSL**, and the **runtime** itself (Dapr virtual actors `Source` / `Target` / `Analyst` plus a reconcile loop; the optimizer's GEPA loop as a Dapr Workflow).

## Future seams

Everything not built is a declared seam that fails loud rather than fabricating output — the authoritative list is [docs/SEAMS.md](docs/SEAMS.md). The significant ones: media extraction models (SeaweedFS + Whisper/VLM/OCR endpoints — the `process_media` job plane is real end-to-end, including derived signals re-entering fan-out with inherited geo/tags, but with no extraction endpoint configured the path refuses loudly rather than producing anything); the non-text UI renderers (the inline `structured` / `application/geo+json` modality renderer and the audio/video/image players are badged placeholders, SEAMS #13 — the `geojson` source already emits real structured signals, and the live geospatial view is a separate v4 **Leaflet** Dockview panel, not a modality renderer); and `stream` acquisition (poll and push are live). Feed-level **situation clustering** is **now built**: `finding_supersession` stamps a `situation_signature` on clustered findings and the `situation_clustering` deterministic handler (descriptor `descriptors/analyst_situation_clustering.yaml`) materializes them into durable `situations` rows on the deterministic cadence — the last dark write-path of the analysis plane is now live, not a seam. Reactive LLM trigger dispatch, source-side dedup tiers 1–2, the subscription-policy / action-pack-grant / backfill operator panels, the live-enforced per-pack governor caps, and analyst-side agency invocation (consult's tools run through the governed `substrate_read` pack; findings crossing the severity gate fire the `escalate_finding` pack end-to-end) are **live**.

## Release boundary

Legba ships as an honest single-operator platform: a feature is either **built**
(runs end-to-end today), a **guarded seam** (the surface exists but refuses
activation / raises loudly until its real edge is wired — never a silent stub), or
**designed, not built** (the design is written in [docs/DIRECTION.md](docs/DIRECTION.md);
no code claims it). The authoritative seam registry is [docs/SEAMS.md](docs/SEAMS.md);
the forward design is [docs/DIRECTION.md](docs/DIRECTION.md). This table is the
one-page truth-in-labeling summary.

| Capability | State | Notes |
|---|---|---|
| Source-first acquisition + predicate fan-out | **built** | 46-source catalog (49 live sources incl. seed/baseline); poll + push acquisition. `stream` mode is a guarded enum seam. |
| Baseline enrichment (language / geo / GLiREL NER + relation extraction) | **built** | Hosted `legba-models`; relation backend is **GLiREL** (`jackboyla/glirel-large-v0`), not REBEL. |
| Coalescing analysts → findings / predictions | **built** | Twelve built-in analyst kinds, plus the `journal_assessor` extension kind; reactive + cadence firing. |
| Temporal facts (`valid_from` / `valid_until` / `superseded_by`) | **built** | Migration 0032; open-only partial unique index. |
| Reified typed signed `nexuses` + structural-balance / graph-mining | **built** | Migration 0033; ≈15 agent + 17 seed nexuses live. |
| ACH competing hypotheses + calibration | **built** | **Per-cell consistency is now LLM-scored** (Heuer CC/C/N/I/II via the model plane, budget-gated, never litellm), with the deterministic lexical/polarity scorer as the **budget-exhausted fallback** (each row records `matrix_scorer = llm \| lexical`). Evidence is scoped to the resolved-entity set (`entity_profiles`), not a substring. **Calibration outcome-resolution is wired with an exogenous resolver built and preferred** — the Brier reads `resolved_outcome` (migration 0038), and the `subsequent_facts` / operator-label paths that stamp it against evidence produced *after* the hypothesis (not the hypothesis's own evidence balance) are coded and dispatched. **As of 2026-06, those exogenous paths have not yet fired live: all resolved outcomes to date are the `status_transition` (self-consistency) tier**, which the row flags as `self_consistency_only`. `confirmed / refuted` are defensible as status transitions. **Goal:** a real Brier against resolved real-world outcomes once the exogenous resolvers fire. **Residual caveats:** the subsequent-facts auto-resolver is a coarse directional heuristic (the operator-label path is higher-fidelity), and a budget-exhausted run falls back to the lexical scorer. No proven-forecast-accuracy claim. See [docs/ANALYSIS.md](docs/ANALYSIS.md) §7.4–§7.5 and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §5.3/§13. |
| Self-optimizing eval loop (critic + GEPA optimizer) | **built** | GEPA runs as a Dapr Workflow on an isolated worker and **generates candidate** prompt modules; promotion of a champion candidate to the live system prompt is **human-gated through the descriptor lifecycle — it never auto-promotes by default**. |
| On-demand consult (chat + deep) + MCP `legba_consult` tool | **built** | `POST /api/v1/consult`. |
| First-person reflective journal (off-chain voice) | **built** | The 11th `OutputKind` (`journal`), the `journal_assessor` extension analyst kind. Lands in a dedicated `journal_entries` table (migration 0048) — **off the fact/finding/nexus chain** (always-empty `derived_from`, excluded from the lineage catalog), so a lineage walk can never surface it. Entry (12h) + consolidation (daily) tiers; per-phase LLM split (the local gpt-oss / vLLM GATHER plane + the Anthropic plane for the voice). Outward effects (correction / change / self-revision) flow through the **human-gated `journal_proposals` queue** (`GET /api/v1/journal_proposals` + accept/reject); the `correction` / `self_revision` apply paths are tested end-to-end, the `change` apply path is import-verified but not yet exercised against a live registry. The Journal UI panel is `tsc`-green + wired, pending its first in-browser render. A critic / optimizer over the journal's own voice is designed-not-built (gated on a critic actuator). |
| STIX 2.1 bundle producer (NATS + file sinks) | **built** | Real + e2e-proven. |
| Curated seeding (`world_baseline` flavor-b) | **built** | `seed_batches` ledger; `world_baseline` + `wikidata_leaders` + `acled_conflict` + `sipri_arms_transfers` adapters live (UCDP / World Bank designed). |
| Time-series metrics (observability) + full-text search (BM25) | **declared seam** | The idle TimescaleDB metrics store + OpenSearch BM25 backing (zero callers) were **removed**; no metrics store, and `search_signals` falls back to Postgres FTS (SEAMS #21). `anomaly_detection` is unaffected — it reads `time_bucket()` from the primary Postgres pool. |
| SeaweedFS object store | **guarded seam** | Schema-slotted stack kind; no live integration module (deferred). |
| Eager media extraction (Whisper / VLM / OCR) + non-text UI renderers | **guarded seam** | Job loop is built end-to-end and refuses loudly with no endpoint configured. Model-deploy configs are prepped; the live endpoint is held as a stated seam (SEAMS #1/#13). |
| TAXII 2.1 upload + webhook output | **guarded seam** | STIX producer is live; TAXII upload raises `TaxiiServerNotConfiguredError` until an operator-confirmed server exists (SEAMS #10). |
| A2A skill router | **guarded seam** | Wired to mount, **not mounted on the production runtime** — the gated-off route fails loud (`/a2a/skills` → 503 `a2a_skill_surface_disabled`, SEAMS #15, xfail-tracked); operator-gated by `LEGBA_A2A_ENABLED`. |
| RBAC / SSO, multi-tenant isolation, MCP surface expansion | **designed, not built** | **Legba ships single-tenant.** No enterprise / multi-tenant / RBAC claim is made. The design (deny-by-default, scoped tokens, per-tenant RLS) lives in [docs/DIRECTION.md](docs/DIRECTION.md) §1–§2. |
| Horizontal scale-out (multi-node) | **designed, not built** | Single-node today; the hot ingest/analysis path is already replica-safe, but the runtime ships single-replica with a fail-loud guard. Design in [docs/DIRECTION.md](docs/DIRECTION.md) §6. |
| Deep-crawl discovery jobs | **designed, not built** | Source discovery runs via the registry route, not the job plane ([docs/DIRECTION.md](docs/DIRECTION.md) §8). |

## Status

Live and source-first — **single-operator / single-tenant**, single-node, run-it-yourself. The **proven core** runs end-to-end: real RSS (BBC / Deutsche Welle / Al Jazeera) flows through baseline enrichment, fan-out on `legba.signals.>`, and per-country coalescing into distinct findings with provenance and receipt chains — firing **reactively** on signal accumulation as well as on the cadence floor, with per-country runs executing concurrently. An entity knowledge graph is kept current by an ongoing deterministic analyst, and temporal facts + signed nexuses give the substrate its supersession-aware knowledge-fusion shape. Cold-start from empty volumes through to automated per-country findings is proven end-to-end from a single baseline schema migration.

The **experimental / research layer** (ACH competing hypotheses, calibration / outcome-resolution, the GEPA optimizer, advanced graph analytics) is built and traceable but **carries no validated skill metric**. Specifically: the ACH per-cell consistency matrix is LLM-scored (Heuer CC/C/N/I/II, budget-gated, with the deterministic lexical scorer as the budget-exhausted fallback), and the calibration Brier reads an exogenous `resolved_outcome` whose `subsequent_facts` / operator-label resolvers are built and preferred — but as of 2026-06 those exogenous resolvers have **not yet fired live**: every resolved outcome to date is the self-consistency (`status_transition`) tier, which the row flags as `self_consistency_only`. The goal is a real Brier against resolved real-world outcomes once they fire (see the release-boundary table and [docs/ANALYSIS.md](docs/ANALYSIS.md) §7.4–§7.5). What is not yet built is declared in [docs/SEAMS.md](docs/SEAMS.md) rather than implied — and laid out plainly in the release-boundary table above. **No enterprise, multi-tenant, RBAC, or forecast-accuracy / Brier-skill claim is made.**

## Contact

Want to talk shop? Reach out at legba@civislux.us.

## License

Copyright (C) 2026 Lewis George.

Legba is free software licensed under the **GNU Affero General Public License, version 3 or later** (`AGPL-3.0-or-later`) — see [LICENSE](LICENSE). Note the AGPL's network clause (§13): if you run a modified version to provide a service over a network, you must offer that service's users the complete corresponding source of your modified version.

A **commercial license** is available for uses the AGPL's copyleft does not fit — e.g. embedding Legba in a proprietary product, or operating a hosted service without releasing your modifications. Enquire via the [project repository](https://github.com/ldgeorge85/legba).
