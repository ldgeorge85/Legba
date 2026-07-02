<p align="center">
  <img src="logo_small.png" alt="Legba" width="400">
</p>

<h1 align="center">Legba</h1>
<p align="center"><em>Cited, verified intelligence you can drill to source — over any feed you can reach, self-hostable.</em></p>
<p align="center">
  <a href="docs/GLOSSARY.md"><b>Glossary</b></a> ·
  <a href="docs/FAQ.md"><b>FAQ</b></a> ·
  <a href="docs/DIRECTION.md">Direction</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a>
</p>

**Legba turns a firehose of sources into cited, verified, drillable reports — because every claim drills to the source it rests on.** It is a *decompositional* intelligence system: it breaks a domain into a handful of narrow, bounded reasoning questions, answers each one from cited evidence, composes those answers bottom-up into per-target and world reads, and grades the whole chain. What sets it apart is the **discipline**, not the data — every claim is cited to a source signal, checked by a **mandatory second-pass faithfulness verifier**, and auditable hop by hop through a hash-chained receipt chain back to the original signal. It is not a search box or an LLM wrapper: most tools let an LLM *assert*; Legba makes every assertion **cited, verified, and auditable to source**. You declare what to watch and how to reason (no code per feed), and the runtime ingests, links, reasons, verifies, and writes back — an engine you **run yourself (AGPL)**, so you can *check* the intelligence, not just consume it.

**On honesty — what "verified" means here.** The verify pass measures **groundedness**, not truth: for each cited claim it asks *does this claim follow from the evidence it cites?* — not *is this claim true in the world?* Faithfulness is a score in `[0,1]`, folded into `effective_confidence = min(confidence, faithfulness_score)` at read time; a planted fabrication is flagged unsupported and demoted to a visible low-confidence tier (never silently deleted). Legba does not claim sensor-fusion or track-correlation rigor, and it makes **no forecast-accuracy claim** — it is situation-assessment over text and structured data, and it reports its own weak spots plainly (see [Status](#status)).

The engine is domain-agnostic; geopolitics (the G20) is the case we run, and that's what this README shows — but nothing in the engine is geopolitical: sources, targets, and analysts are all declared, so swap them and the same *source → enrich → fan-out → reason → verify → cite → compose* pipeline fits any domain you can point a feed at. Geopolitics is the exemplar, not the boundary.

**How it works (under the hood).** Legba ingests data from any source — pulled on a cadence, or pushed to a webhook — and fuses it into a provenance-tracked knowledge substrate: signals → facts / nexuses / situations (a temporal knowledge graph) → four bounded reasoning **units** → a per-country **composition** → a world **composition** → a banded **scorecard**, every output traceable to its sources via lineage and hash-chained receipt chains. It turns declarative descriptors — sources, targets, and analysts — into running actors over a shared substrate: a Dapr virtual-actor runtime reads each registered descriptor and stands up the corresponding actor, which acquires raw observations, routes them, reasons over them, verifies, and writes findings back. Derived rows carry full lineage plus a SHA-256 hash-chained receipt; the descriptor registry keeps a content-hashed, Ed25519-signed audit log of every change.

## The source-first model

**Ingest once, enrich once, match many.** Acquisition belongs to **sources**, not targets. A `SourceActor` polls on a Dapr reminder (or receives a webhook push), produces **one canonical, target-agnostic signal**, enriches it once (baseline: language detection, geo, and entity NER), and publishes it once to NATS JetStream (`legba.signals.>`). Signals carry **no `target_id`** — they are observations, not interpretations.

**Fan-out routes each signal to many subscribers by predicate.** A target is a passive subscriber: it declares a `SourceRef` (an explicit `source_id` or a `source_selector` predicate) and a `subscription_policy` (`open` / `allowlist` / `grant`). The fan-out plane delivers each published signal to every matching target — coarse NATS subjects (tenant / source / modality / event-class) narrowed by a SQL `WHERE` clause and a Starlark residual predicate. One BBC feed fans out to two dozen country desks without re-fetching anything.

**Analysts coalesce matched signals into findings.** An `AnalystActor` accumulates the signals routed to its target and fires on a coalescing trigger — an accumulation threshold plus a severity gate, clamped by a cooldown, with a cadence heartbeat as the coverage floor. Both deterministic *and* LLM-bearing analysts fire reactively: the trigger policy floors an LLM analyst to a coalesced accumulation batch (never per-signal), so it reacts to breaking accumulation as well as running on cadence. A target-bound analyst executes its matched targets **concurrently** via per-(analyst, target) worker actors (bounded fan-out), so a wide analyst over many countries doesn't serialize. Every output carries full `derived_from` provenance and a per-analyst SHA-256 hash-chained receipt chain — **chain-consistent (single-node)**.

### The four planes

1. **Acquisition** — `SourceActor` ownership of polling/push, baseline enrichment, and the fan-out / subscription engine. Signals are **modality-first** (`text` / `image` / `audio` / `video` / `structured` / `binary`); a dozen-plus source-handler kinds feed the same canonical `Signal` shape. A webhook front plus an **inbound NATS accept-and-enqueue path** (validate + auth → publish to `legba.inbound` → a durable drain writes the signal, with honest backpressure via a `discard=new` workqueue) are built; this push half is dormant plumbing until a live webhook source is wired.
2. **Analysis** — `TargetActor` subscribers, `AnalystActor` reasoning, coalescing triggers, action-pack agency with a per-pack governor and a per-analyst token budget, and the mandatory faithfulness-verify gate on cited findings.
3. **Async jobs** — a NATS work-queue (`LEGBA_JOBS`) with competing-consumer workers; `process_media` is the live job kind (source discovery runs via the registry route, not the job plane).
4. **Substrate** — Postgres + Apache AGE (relational + entity graph), Qdrant (vectors), Redis (hot state), NATS JetStream (event bus + durable streams). Time-series metrics (observability) and BM25 full-text search are **declared seams — not built**: there is no metrics store, and `search_signals` uses Postgres FTS (see [docs/SEAMS.md](docs/SEAMS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §5.5).

## The analysis spine — the product

The product is a bottom-up spine: narrow, cited, verified sub-claims composed into per-country and world reads and rolled into a banded scorecard. Nothing near the top is trusted unless everything under it was cited and passed the faithfulness gate. This is what "the discipline, not the data" means concretely.

1. **Four bounded reasoning units.** `leadership_transition`, `energy_security`, `escalation`, and `narrative_coordination` — each an `inline_target` LLM analyst scoped to every country desk via a `has_tag("g20") or has_tag("watch")` fan-out, each answering **one narrow question**. Each run: **assemble** a cited 72-hour signal slice plus an **authoritative current-context grounding preamble** of accumulated facts / nexuses / situations from the substrate → **cited synthesize** (a strict-JSON finding whose prose carries `[N]` citation markers mapped to signal ids) → **mandatory faithfulness verify** → the `effective_confidence` fold and drill-to-source provenance. Skill is a per-unit number, not a platform boast.
2. **Per-country composition.** `country_composition` (kind `meta_findings_synthesizer`) reads the four verified units for a country and writes a hedged, cited synthesis. Unverified sub-claims never enter it — the read slice is an INNER JOIN on the faithfulness critique, so a country whose units produced no verified sub-claim yields an explicit "no source findings to synthesize" read rather than an invented one.
3. **World composition.** `world_assessor` — repointed onto `meta_findings_synthesizer` — composes over the per-country reads into a cited, hedged world picture that drills country → units → source. It is a composition of verified reads, **not** the old verdict-from-nowhere monolith (that framing was retired).
4. **Banded scorecard.** `scorecard_producer` (a deterministic META analyst; the 12th OutputKind, `scorecard`) writes **one banded row per active country desk** (any active target tagged `g20`/`watch`) from high-precision rules over already-verified claims (severity tag × `effective_confidence`; demote-never-promote). Every band names the verified-claim id it rests on; a dimension with no qualifying verified claim reads **`insufficient-evidence`** with an explicit reason — never a fabricated band — and a per-claim faithfulness below a floor demotes to **`low-faithfulness`**.
5. **Skill scoreboard.** Per-unit eval — faithfulness plus correctness-vs-reference (honest-null where unmeasured) — alongside the exogenous calibration Brier and the acute-forecast BSS. A no-skill or insufficient-sample result is **published, not hidden**.

**The mandatory verify pass.** An LLM judge — **currently the same core reasoning model** (`llm.primary.openai_compat`, gpt-oss-120b) that writes the findings, **not** a cross-family judge — plus a deterministic citation-presence floor scores each cited finding for faithfulness in `[0,1]`. `effective_confidence = min(confidence, faithfulness_score)` is folded at read time and gates a visible low-confidence tier; the pass never hard-deletes. The judge is gated by `LEGBA_VERIFY_LLM_JUDGE` and soft-fails to the deterministic floor if the component is unresolved. Same-model judging is a deliberate, temporary choice — the earlier 8B cross-family judge (`llm.verify.slm_8b`, Llama-3.1-8B) proved too weak (harsh + mis-aimed), so the strong reasoning model runs the judging to prove the flow. **Known limitation:** a model verifying prose from its own family shares its blind spots, so this faithfulness signal is weaker than an independent cross-family judge; the deterministic floor and the signed provenance chain still backstop it, and a dedicated reasoning judge is planned.

**Provenance / drill-down.** `GET /api/v1/lineage/finding/{id}` walks the receipt chain; each node carries a SHA-256 `receipt_hash` and a re-computed `chain_consistent` boolean (badge: **"chain-consistent (single-node)"**), resolving hop by hop to the real source URL with **zero dangling links** — a lineage-integrity sweep prunes any dangling `derived_from`.

**Accumulation (it integrates over time, not just today).** The substrate is a temporal knowledge graph — facts and nexuses with `valid_from` / `valid_until` and decay, growing continuously. The units read it through the grounding preamble (e.g. *"US head of government: Trump since 2025-01-20; US in active conflict with Iran since 2026-02-28; NATO member since 1949"*), which both supplies multi-week context and **supersedes stale model priors**; on top of that each unit reads a 72-hour raw-signal window, and the scorecard bands over a 14-day verified-claim window.

### Measured experiments — honest about what doesn't work yet

The more ambitious legs return **only** as measured, honest experiments — kept and labeled as experiments, never dressed up as proven capability.

- **GEPA self-optimizer (`unit_optimizer`).** The prompt optimizer returns scoped to **one** measured unit (`leadership_transition`). Every candidate carries a real before/after **paired faithfulness delta** measured on the same faithfulness judge (currently the core model, not cross-family) that gates the live findings (a recent live cycle: parent `0.34` → candidate `0.29`, delta `-0.05`). It stays `promotion_gate=human_gated` and can **never** auto-promote on a degenerate, insufficient-sample, or non-positive delta. The old monolithic `country_optimizer` stays **cadence-frozen** (no reminder-flood regression).
- **Forecasting.** Returns only as a precise-question `acute_forecasts` Brier/BSS scoreboard — a question + window + probability + exogenous auto-resolution — surfaced solely on the calibration scoreboard route (`GET /api/v1/v3/eval/calibration`), **never** as a free-text claim or finding. A geography-dominated or degenerate probability vector **abstains** (zero rows). It currently reports **no proven skill** — honestly.

## Quick start

> **Clean-slate only — no migration path from pre-pivot Legba.** This is a complete
> refactor from the v1/v2 target-first design; the data model, substrate schema, and
> APIs are incompatible with pre-pivot instances. There is no upgrade or data-migration
> path: stand up a fresh empty substrate (Postgres+AGE, NATS JetStream, Qdrant, Redis)
> and apply migrations from `0001_baseline` forward. Do not point this build at a
> pre-pivot database.

**One command deploys it: [`deploy/deploy.sh`](deploy/deploy.sh).** It runs the entire phased, idempotent bring-up — preflight → schema (the single baseline `deploy/baseline/0001_baseline.sql`) → the ordered registrars (stack → action packs → sources + the ~46-source catalog → the country desks (19 G20 + a 5-country watch tier) → the analyst working set → deterministic analysts → budget) → optional seeds → runtime → boot-verify — in the correct order, so you don't have to sequence any of it by hand:

```bash
docker compose --profile runtime build      # one-time image build
deploy/deploy.sh                             # real stack (project legba), no seeds
deploy/deploy.sh --seed                      # + curated knowledge seeds
```

For a throwaway clean-slate validation stack on the same host, add `--project legba_val --no-caddy` (data-isolated; see [docs/SETUP.md](docs/SETUP.md)).

**Why a script — what it handles for you.** Bring-up is container-mode (the substrate is always-on; Dapr + the app images run under the `runtime` profile), and the ordering is load-bearing: migrate → register the stack → register the working set → *then* boot (or `--force-recreate`) the runtime. The runtime builds its NLP / embedding clients once at boot from the registered stack, so booting against an un-seeded registry pins `nlp_client=None` for the whole process lifetime (signals land with no geo/entities, and geo-scoped analysts never match). `deploy.sh` sequences all of that correctly — the manual steps below are the same sequence, kept for reference / partial re-runs. Read [docs/RUNBOOK.md](docs/RUNBOOK.md) **§0** first for the full operator notes.

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

# 5. Register the source-first working set: action packs + shared RSS sources +
#    the ~46-source catalog + the 19 G20 + 5 watch country desks + the analyst set.
docker exec legba-legba-registry-1 python scripts/bringup_register_action_packs.py
docker exec legba-legba-registry-1 python scripts/bringup_register_sources.py
docker exec legba-legba-registry-1 python scripts/bringup_register_source_catalog.py
docker exec legba-legba-registry-1 python scripts/bringup_register_g20_country_targets.py
docker exec legba-legba-registry-1 python scripts/bringup_register_watch_country_targets.py
docker exec legba-legba-registry-1 python scripts/bringup_register_analysts.py

# 6. NOW boot the runtime + Dapr + UI + Caddy, against the seeded registry, so the
#    runtime builds its nlp_client. --force-recreate is required if the runtime was
#    ever booted earlier in this lifetime (it pins nlp_client=None until recreated).
docker compose --profile runtime up -d --force-recreate
```

> If you already ran `docker compose --profile runtime up -d` before seeding, you
> must `--force-recreate legba-runtime-dapr` after seeding (per RUNBOOK §0) — a plain
> restart will not rebuild the boot-time `nlp_client`.

**What you get.** The `deploy.sh` registrars stand up three shared RSS sources (BBC World, Deutsche Welle, Al Jazeera) as an easily-verified cold-start bootstrap **plus** the full no-auth catalog (`scripts/bringup_register_source_catalog.py`, **~46 sources** — 43 news RSS + 3 geo-hazard GeoJSON — across the dozen-plus handler kinds), the nineteen G20 country targets **plus a five-country high-consequence watch tier (Israel, Iran, Ukraine, Taiwan, North Korea)**, and the analyst set: the **four bounded units**, `country_composition`, `world_assessor`, `scorecard_producer`, the deterministic maintenance/eval analysts, and consult. Sources begin polling and publishing enriched, target-agnostic signals; the fan-out plane routes them by geo predicate to the country desks; the four units coalesce them into cited, verified per-country sub-claims, which compose up to per-country and world reads and roll into one banded scorecard row per country. An ongoing `entity_resolution` deterministic analyst keeps an entity knowledge graph (`entity_profiles` / `signal_entity_links` / `proposed_edges`) current. Cold-start from empty volumes through to a verified per-country scorecard is proven end-to-end from a single baseline schema migration.

> The monolithic per-country `country_assessor` one-pager is **retired and stopped** — the four units plus composition supersede it, and nothing in the spine reads it. Its ~1.2k historical findings remain in the substrate, unread (not deleted, not a clean slate); see [Retirements & freezes](#retirements--freezes).

**Live scope (this deployment).** The running instance has gone far beyond the bootstrap: **~50 distinct sources** actively producing signals → **~85k signals** ingested → a temporal knowledge graph of **~4.6k facts** · **~4.9k nexuses** · **~940 hypotheses** · **27 situations**, with the four units + composition producing cited findings and one banded scorecard row per active country desk (the 19 G20 plus a 5-country watch tier). (Counts are a live snapshot and grow continuously.)

The operator UI is served by Caddy on `:443` (auto-HTTPS, basic-auth perimeter); the registry API is on `:8090` behind a bearer token. The runtime reaches the LLM, embedding, NER, and translation models over HTTP — none run in-container (see [docs/AI_MODELS.md](docs/AI_MODELS.md)).

```bash
# Confirm signals are landing + analysts are producing output:
docker exec legba-postgres-1 psql -U legba -d legba -c \
  "SELECT count(*) FROM signals; SELECT kind, count(*) FROM analyst_outputs GROUP BY kind;"
```

## Features

- **Descriptor-driven.** Sources, targets, analysts, and action packs are declarative descriptors. A content-hashed, Ed25519-signed registry (audit log) stores every instance, emits NATS events on change, and routes validation failures to a DLQ. Add / pause / retire at runtime — no deploys for content changes.
- **Bottom-up, cited analysis spine.** Four bounded reasoning units → per-country composition → world composition → a banded per-country scorecard, with a mandatory faithfulness-verify pass at every layer. A sub-claim must have been verified to compose (never-verified claims are excluded), and each composed clause's confidence is capped by its evidence's faithfulness (`effective_confidence = min(confidence, faithfulness)`) — so a weak sub-claim can compose but never lifts the read above its own score, and a dimension with no qualifying verified claim reads `insufficient-evidence`, never a fabricated band. The verify pass annotates-and-caps rather than hard-dropping, so the low-confidence tier stays visible, not silently removed.
- **Mandatory faithfulness verify.** The faithfulness verify judge (currently the same core gpt-oss-120b model, **not** cross-family — a deliberate, temporary choice; see AI models) plus a deterministic citation-presence floor grades every cited finding in `[0,1]`; `effective_confidence = min(confidence, faithfulness_score)` gates a visible low-confidence tier. It measures groundedness, not truth.
- **Shared substrate with universal provenance.** Every derived row carries `target_id`, `analyst_id`, version stamps, and a `derived_from` UUID array. Cross-target reasoning is a query over the substrate, walkable via `/api/v1/lineage`. Per-analyst SHA-256 hash-chained receipt chains make outputs auditable (chain-consistent, single-node).
- **Source-first fan-out.** One enriched signal is matched to many targets by predicate — coarse NATS subjects narrowed by SQL `WHERE` plus a Starlark residual. Targets subscribe; they do not pull.
- **Many source kinds, one signal shape.** A dozen-plus handlers — `rss`, `gdelt_query`, `acled`, `mediacloud`, `opensanctions`, `scraper`, `firecrawl`, `telegram_channel`, `discord_webhook`, `common_crawl_news`, `intelmq_collector_bridge`, `generic_webhook`, `json_api`, and `geojson` — all yield the same canonical `Signal`. Acquisition is poll or push (`stream` is a documented future seam); see [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
- **Multimodal seam.** Signals are modality-first; the modality → (ingest extractor, UI renderer) registry is scaffolded with `text` live and the rest keyed for drop-in. The `geojson` source is the first **model-free** non-text modality: it emits `structured` / `application/geo+json` signals (geometry inlined, coords promoted to the `geo` column) with no extraction model in the loop. Eager media extraction (Whisper / VLM / OCR) is a future seam.
- **Action-pack agency.** Analyst capabilities (tools / prompt fragments / rules / channels / governor / applicability) ship as modular, allow-listed bundles. A capability is usable only in the intersection `analyst.action_packs ∩ target.allowed_action_packs ∩ pack.applicability`, gated by a per-pack governor and a per-analyst daily token budget.
- **Measured self-optimizer (experimental).** The GEPA prompt optimizer returns as `unit_optimizer`, scoped to one bounded unit and gated on a **real paired faithfulness delta** measured by the same faithfulness judge (currently the core model); it **generates candidates** and **never auto-promotes** on a degenerate or non-positive delta — promotion to the live prompt is human-gated through the descriptor lifecycle. The old monolithic `country_optimizer` is cadence-frozen. This is a research surface, not a proven optimization capability.
- **On-demand consult engine.** A ReAct `consult_on_demand` analyst answers ad-hoc operator queries against the live substrate (`POST /api/v1/consult`), plus a deep-consult workflow bridge and an MCP `legba_consult` tool.
- **First-person reflective journal (introspective instrument).** A `journal_assessor` extension analyst — Legba's one reflective voice, pointed at the whole organism rather than a single target. It is **off the fact/finding/nexus chain**: a journal row lands in its own `journal_entries` table with an always-empty `derived_from`, excluded from the lineage catalog, so a lineage walk can never surface it. It **runs on cadence** — a 12-hour entry pass plus a daily consolidator — as a first-person introspective voice that cannot pollute product output. Routing its reflections back into the system through a human-gated proposal queue is a **future** item, not yet wired.
- **Operator UI.** A Dockview single-page app (served by Caddy) surfacing the scorecard, findings, compositions, situations, the journal, the actor roster, lineage walks, the entity graph, budget ledgers, and inline descriptor editing — see [docs/UI.md](docs/UI.md). A unified **Live Feed** panel merges findings and signals from two NATS tails into one stream (Live toggle, Source filter, near-duplicate clustering); a **System Status** panel rolls per-source firing, per-analyst cadence (from `analyst_traces`), queue backpressure, and infra reachability into one health page — with per-analyst run timing (count, avg/max wall-clock seconds, last run, non-success) exposed at `GET /api/v1/v3/eval/analyst_runtime`.

## AI models

Inference is hosted out-of-process; **none of these models run inside the Legba containers.** The core analyst plane is a self-hosted, vLLM-served **gpt-oss-120b** (`llm.primary.openai_compat`, `$0`) that drives the units and compositions; **consult and deep-consult only** use **Claude Opus 4.8** (billed, used sparingly); the mandatory faithfulness-verify judge currently runs on the **same core gpt-oss-120b model** (`llm.primary.openai_compat`, **not** cross-family — a deliberate, temporary choice while the too-weak 8B `legba-slm` / `llm.verify.slm_8b` judge is retired; a dedicated reasoning judge is planned). Baseline enrichment uses `BAAI/bge-m3` embeddings, NLLB translation, and spaCy / GLiREL NER. LLM providers (Anthropic / vLLM / OpenAI-compat) are resolved through the stack registry, so a descriptor names a provider rather than hard-coding an endpoint. **Hard rule:** no litellm / dspy in the runtime or the analyst inference path — dspy lives only in the opt-in GEPA worker image. Full inventory and call patterns in [docs/AI_MODELS.md](docs/AI_MODELS.md).

## Documentation map

| Document | What it covers |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | Implementation design — core abstractions, data flows, decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Conceptual orientation — the four planes and why the system is shaped this way |
| [docs/ACQUISITION.md](docs/ACQUISITION.md) | The acquisition plane — `SourceActor`, baseline enrichment, fan-out / subscription |
| [docs/ANALYSIS.md](docs/ANALYSIS.md) | The analysis plane — units, composition, verify, coalescing triggers, action-pack agency |
| [docs/SOURCES.md](docs/SOURCES.md) | The source catalog — the 3 / 46 / ~50 scope model and the per-source table of what Legba ingests |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Source handler kinds and the data sources reachable through them |
| [docs/AI_MODELS.md](docs/AI_MODELS.md) | The hosted models, providers, and how the runtime reaches them |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operator runbook — bring-up, migrations, registration, troubleshooting |
| [docs/CODE_MAP.md](docs/CODE_MAP.md) | Code map — modules, function flows, dependencies |
| [docs/UI.md](docs/UI.md) | Operator UI guide — panels, auth chain, daily-driver workflow |
| [docs/DIRECTION.md](docs/DIRECTION.md) | Engineering direction — designed-not-built: RBAC/SSO, tenancy, STIX/TAXII, MCP, multimodal, scale-out, budget fallback, deep crawl |

## Key abstractions

The **descriptor registry** (content-hashed, Ed25519-signed audit log, DLQ, NATS events), the **stack registry** (substrate components) and **credential vault** (XSalsa20-Poly1305), **action-packs**, a polymorphic **`TargetScope`** (`GeoScope` / `EstateScope` / `EntityScope`), the **Starlark predicate DSL**, the **faithfulness-verify** gate, and the **runtime** itself (Dapr virtual actors `Source` / `Target` / `Analyst` plus a reconcile loop; the optimizer's GEPA loop as a Dapr Workflow on an isolated worker).

## Retirements & freezes

Sequenced and documented in [docs/SEAMS.md](docs/SEAMS.md), so the trusted spine stays clean:

- **`country_assessor` (monolithic per-country one-pager) — RETIRED / STOPPED.** The four units + composition supersede it; nothing in the spine reads it (it had been feeding untrusted findings). Its ~1.2k historical findings remain in the DB, unread — not deleted, not a clean slate.
- **`country_predictor`, `india_energy_predictor` (forecast-as-claim) — RETIRED / STOPPED.** Forecasting returns only as the measured `acute_forecasts` Brier/BSS scoreboard, never a free-text claim; ~539 historical prediction rows remain in the DB.
- **`country_optimizer` (monolithic GEPA) — cadence-FROZEN** (descriptor still `state=active`). GEPA returns only as the scoped, faithfulness-measured `unit_optimizer` (no reminder-flood regression).
- **`journal_assessor` — RUNS (on cadence).** An off-chain introspective voice (12-hour entry + daily consolidator) that writes only `journal_entries`, never product output. Routing its reflections back via a human-gated proposal queue is a future item.
- **`world_assessor` — NOT retired.** It graduated into the world composition (repointed onto `meta_findings_synthesizer`).

## Future seams

Everything not built is a declared seam that fails loud rather than fabricating output — the authoritative list is [docs/SEAMS.md](docs/SEAMS.md). The significant ones: media extraction models (SeaweedFS + Whisper/VLM/OCR endpoints — the `process_media` job plane is real end-to-end, including derived signals re-entering fan-out with inherited geo/tags, but with no extraction endpoint configured the path refuses loudly rather than producing anything); the non-text UI renderers (the inline `structured` / `application/geo+json` modality renderer and the audio/video/image players are badged placeholders, SEAMS #13 — the `geojson` source already emits real structured signals, and the live geospatial view is a separate Leaflet Dockview panel, not a modality renderer); the inbound webhook **push** half (accept-and-enqueue + durable drain are built, but dormant until a live webhook source is wired); and `stream` acquisition (poll and push are live). Feed-level **situation clustering** is built (`finding_supersession` stamps a `situation_signature`, and the `situation_clustering` deterministic handler materializes durable `situations` rows on cadence). Reactive LLM trigger dispatch, source-side dedup tiers 1–2, and analyst-side agency invocation (consult's tools run through the governed `substrate_read` pack; findings crossing the severity gate fire the `escalate_finding` pack end-to-end) are **live**.

## Release boundary

Legba ships as an honest single-operator platform: a feature is either **built**
(runs end-to-end today), a **guarded seam** (the surface exists but refuses
activation / raises loudly until its real edge is wired — never a silent stub), or
**designed, not built** (the design is written in [docs/DIRECTION.md](docs/DIRECTION.md);
no code claims it). The authoritative seam registry is [docs/SEAMS.md](docs/SEAMS.md).
This table is the one-page truth-in-labeling summary.

| Capability | State | Notes |
|---|---|---|
| Source-first acquisition + predicate fan-out | **built** | ~46-source catalog (~50 live sources incl. seed/baseline); poll + push acquisition. `stream` mode is a guarded enum seam. |
| Baseline enrichment (language / geo / GLiREL NER + relation extraction) | **built** | Hosted out-of-process; relation backend is **GLiREL** (`jackboyla/glirel-large-v0`). |
| Four bounded reasoning units → cited findings | **built** | `leadership_transition` / `energy_security` / `escalation` / `narrative_coordination`, each fanned out across the 24 country desks (19 G20 + a 5-country watch tier) via `has_tag("g20") or has_tag("watch")`; reactive + cadence firing; `[N]`-cited strict-JSON findings. |
| Mandatory faithfulness verify pass | **built** | Faithfulness judge (currently the same core gpt-oss-120b model, **not** cross-family — temporary; known limitation) + deterministic citation-presence floor; `effective_confidence = min(confidence, faithfulness_score)`; gates a visible low-confidence tier, never hard-deletes. Measures groundedness, not truth. |
| Per-country + world composition | **built** | `country_composition` and `world_assessor` (both `meta_findings_synthesizer`); read slice INNER-JOINs the faithfulness critique — unverified sub-claims never enter; empty-slice yields an explicit no-read, not an invention. |
| Banded per-country scorecard | **built** | `scorecard_producer` (deterministic META; 12th OutputKind `scorecard`); one row per active country desk (any target tagged `g20`/`watch`); every band names its verified-claim basis; no-basis dimensions read `insufficient-evidence`; sub-floor faithfulness demotes to `low-faithfulness`. |
| Skill scoreboard (faithfulness + correctness-vs-reference) | **built, honest-null** | Per-unit eval + exogenous calibration Brier + acute-forecast BSS. Correctness-vs-reference gold set is **tiny (n=1, reported insufficient-sample)**; a no-skill result is published, not hidden. |
| Measured GEPA self-optimizer (`unit_optimizer`) | **built (experimental)** | Scoped to one unit; every candidate carries a real paired faithfulness delta on the same faithfulness judge (currently the core model); `human_gated`, **never auto-promotes** on a degenerate / insufficient / non-positive delta (live example: `0.34 → 0.29`). Monolithic `country_optimizer` is cadence-frozen. |
| Acute-forecast pilot (Brier / BSS) | **built, no proven skill** | `forecast_scoreboard` (deterministic META) issues one pre-registered weekly binary call per G20 country, resolved **exogenously** by upstream event time, scored by **BSS vs per-country climatology**. Segregated key (`brier_forecast_acute`), never pooled into the headline calibration Brier; abstains (zero rows) on a degenerate / geography-dominated vector; surfaced **only** on the calibration scoreboard, never as a claim. Earns the word "forecast" only when BSS > 0 on a non-degenerate sample — **not today**. |
| Temporal facts (`valid_from` / `valid_until` / `superseded_by`) | **built** | Open-only partial unique index. |
| Contested-claims arbiter ("alternate facts") | **built** | Detect-only, flag-gated (#101). A deterministic `Q·C·R·F` arbiter keeps disputed `(subject, predicate)` values coexisting open and surfaces a credibility-weighted winner in a sidecar — it **never mutates a fact** and abstains on a near-tie. Write-path coexistence and the optional LLM near-tie tie-break ship **OFF by default**; read API `GET /api/v1/contention`. A successful LLM tie-break *pick* is not yet observed live. See [docs/ANALYSIS.md](docs/ANALYSIS.md) §7.11. |
| Reified typed `nexuses` + structural-balance / graph-mining | **built** | Canonical polarity sign + intent + temporal bounds + supersession; ~4.9k nexuses live (~3.2k signed, polarity≠0). |
| ACH competing hypotheses + calibration | **built (no skill claim)** | Per-cell consistency is LLM-scored (Heuer CC/C/N/I/II, budget-gated) with a deterministic lexical scorer as the budget-exhausted fallback. Calibration Brier reads an exogenous `resolved_outcome`; the `subsequent_facts` / operator-label resolvers are built and preferred but **have not yet fired live** — every resolved outcome to date is the self-consistency (`status_transition`) tier, flagged `self_consistency_only`. No proven-forecast-accuracy claim. See [docs/ANALYSIS.md](docs/ANALYSIS.md) §7.4–§7.5. |
| First-person reflective journal (off-chain voice) | **built (runs on cadence)** | 11th OutputKind (`journal`); dedicated `journal_entries` table — off the fact/finding/nexus chain (always-empty `derived_from`, excluded from lineage). `journal_assessor` (12h entry) + `journal_consolidator` (daily) run as an introspective voice; writes never reach product output. Routing reflections back via the human-gated `journal_proposals` queue is a future item. |
| On-demand consult (chat + deep) + MCP `legba_consult` tool | **built** | `POST /api/v1/consult`; consult + deep-consult run on Claude Opus 4.8 (billed, used sparingly). |
| STIX 2.1 bundle producer (NATS + file sinks) | **built** | Real + e2e-proven. |
| Curated seeding (`world_baseline`) | **built** | `seed_batches` ledger; `world_baseline` + `wikidata_leaders` + `acled_conflict` + `sipri_arms_transfers` adapters live (UCDP / World Bank designed). |
| Time-series metrics (observability) + BM25 search | **declared seam** | No metrics store; `search_signals` falls back to Postgres FTS (SEAMS #21). `anomaly_detection` is unaffected — it reads `time_bucket()` from the primary Postgres pool. |
| SeaweedFS object store | **guarded seam** | Schema-slotted stack kind; no live integration module (deferred). |
| Eager media extraction (Whisper / VLM / OCR) + non-text UI renderers | **guarded seam** | Job loop is built end-to-end and refuses loudly with no endpoint configured (SEAMS #1/#13). |
| TAXII 2.1 upload + webhook output | **guarded seam** | STIX producer is live; TAXII upload raises `TaxiiServerNotConfiguredError` until an operator-confirmed server exists (SEAMS #10). |
| A2A skill router | **guarded seam** | Wired to mount, **not mounted on the production runtime** — the gated-off route fails loud (`/a2a/skills` → 503, SEAMS #15); operator-gated by `LEGBA_A2A_ENABLED`. |
| RBAC / SSO, multi-tenant isolation, MCP surface expansion | **designed, not built** | **Legba ships single-tenant.** No enterprise / multi-tenant / RBAC claim is made. Design in [docs/DIRECTION.md](docs/DIRECTION.md) §1–§2. |
| Horizontal scale-out (multi-node) | **designed, not built** | Single-node today; the hot ingest/analysis path is replica-safe, but the runtime ships single-replica with a fail-loud guard. Design in [docs/DIRECTION.md](docs/DIRECTION.md) §6. |
| Deep-crawl discovery jobs | **designed, not built** | Source discovery runs via the registry route, not the job plane ([docs/DIRECTION.md](docs/DIRECTION.md) §8). |

## Status

Live and source-first — **single-operator / single-tenant**, single-node, run-it-yourself. The **analysis spine** runs end-to-end: real RSS + catalog sources flow through baseline enrichment and fan-out on `legba.signals.>`; the four bounded units coalesce them into `[N]`-cited findings that pass the mandatory faithfulness verify; `country_composition` and `world_assessor` compose the **verified** sub-claims into hedged per-country and world reads; and `scorecard_producer` writes one banded row per active country desk (the 19 G20 plus a 5-country high-consequence watch tier). Cold-start from empty volumes through to a verified scorecard is proven end-to-end from a single baseline schema migration, firing reactively on signal accumulation as well as on the cadence floor.

**It is honest about where it is weak today.** The scorecard is a **mix**: some countries band from verified claims, while others read all-`insufficient-evidence` — for example the US currently reads all-insufficient because its unit faithfulness is genuinely low, and the scorecard refuses to fabricate a band over unverified sub-claims. The correctness-vs-reference gold set is **tiny (n=1, reported insufficient-sample)**. The acute-forecast pilot reports **no proven skill** and abstains on degenerate windows. The GEPA `unit_optimizer` measures a real faithfulness delta that is not yet positive (a recent cycle: `0.34 → 0.29`), so it promotes nothing. The ACH / calibration layer is built and traceable but carries **no validated skill metric** — every resolved outcome to date is the self-consistency tier, not an exogenous real-world resolution. All of this is surfaced, not hidden: what is not yet built is declared in [docs/SEAMS.md](docs/SEAMS.md) and laid out in the release-boundary table above. **No enterprise, multi-tenant, RBAC, or forecast-accuracy / Brier-skill claim is made.**

## Contact

Want to talk shop? Reach out at legba@civislux.us.

## License

Copyright (C) 2026 Lewis George.

Legba is free software licensed under the **GNU Affero General Public License, version 3 or later** (`AGPL-3.0-or-later`) — see [LICENSE](LICENSE). Note the AGPL's network clause (§13): if you run a modified version to provide a service over a network, you must offer that service's users the complete corresponding source of your modified version.

A **commercial license** is available for uses the AGPL's copyleft does not fit — e.g. embedding Legba in a proprietary product, or operating a hosted service without releasing your modifications. Enquire via the [project repository](https://github.com/ldgeorge85/legba).
