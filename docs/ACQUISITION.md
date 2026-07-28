# Acquisition — how data enters Legba and reaches analysis

This document covers the source handler, the canonical signal, baseline
enrichment, fan-out and subscription, cross-source dedup, and discovery. For
what happens *after* a signal reaches a target — coalescing, analysts,
findings — see `ANALYSIS.md`. For the substrate stores see `ARCHITECTURE.md`;
for the hosted NLP/translation models see `AI_MODELS.md`. New here? Start with
the [README](../README.md) and the [Tour](TOUR.md).

The **acquisition plane** is the first of Legba's four planes. It owns
everything from "a source produces an observation" to "that observation is
matched against every interested target". Its governing principle is
**source-first**: a source ingests an observation *once*, enriches it *once*,
and publishes it *once*; the fan-out plane then routes that single canonical
observation to the *many* targets whose predicates select it. Signals are
observations — target-agnostic facts — not per-target interpretations.

**Contents:**
[1 The SourceActor](#1-the-sourceactor) ·
[2 The canonical Signal](#2-the-canonical-signal) ·
[3 Baseline enrichment](#3-baseline-enrichment-once-at-the-source) ·
[4 Fan-out and subscription](#4-fan-out-and-subscription) ·
[5 Cross-source dedup](#5-cross-source-dedup) ·
[6 Discovery](#6-discovery) ·
[7 End-to-end, in one line](#7-end-to-end-in-one-line)

---

## 1. The SourceActor

A source is a declarative `SourceDescriptor`
(`src/legba/data/schemas/source.py`). The Dapr virtual-actor runtime turns one
descriptor into one **`SourceActor`** (`src/legba/runtime/source_actor.py`).
The actor owns acquisition for that source regardless of how many targets
consume it: it pulls or receives *once*, runs the baseline *once*, writes one
canonical signal, and publishes it once.

The mechanism lives in a plain, directly-testable class — **`SourceCore`** —
and the thin `SourceActor` Dapr wrapper delegates to it, so the production
path and the tested path are the same code. The actor holds no cursor state in
Dapr; cursor and provisioning state live in a crash-safe `FilterStateStore`
(the Postgres `actor_filter_state` table), so a pull is idempotent across
sidecar restarts.

The descriptor's `acquisition` field selects one of two modes:

### 1.1 Poll (Dapr Reminder)

For `acquisition: "poll"` the actor, at activation, derives a **durable Dapr
Reminder** named `poll_<source_id>` from `cadence.schedule` (a cron). The
reminder survives sidecar restarts. Each fire calls `SourceCore.pull_once`,
which:

1. builds the source-kind handler (see §1.3),
2. loads the persisted cursor (`last_pulled_at`) and passes it as `since`,
3. iterates `handler.pull(ctx, since)`, running the baseline → write path per
   yielded signal,
4. advances the cursor (only on a clean pull),
5. publishes every written signal to the fan-out plane.

An active, non-discovery poll source *must* declare a `cadence.schedule`
(enforced by the descriptor validator). Constant-period crons map cleanly to a
Reminder; variable-period schedules are a future seam (Dapr Jobs).

### 1.1.1 Poll liveness — quiet vs. broken, graded honestly (2026-07)

Two additions let the watchdog and the operator tell a *quiet* feed apart from
a *broken* cursor, instead of one undifferentiated "silent":

- **`newest_entry_ts`** (migration 0092). On every parsed HTTP-200 poll the RSS
  handler records the newest entry timestamp it *saw* — **before** the
  since-filter, so a poisoned cursor still observes what the feed is serving.
  A future-skew clamp (+26h) rejects junk dates, and an HTTP-304 carries the
  prior observation forward so a 304 streak stays classifiable. The value lands
  on the `source_poll_outcomes` row. The liveness watchdog's empty-streak
  classifier then distinguishes **`honest_quiet`** (the feed itself has served
  nothing new — no alert), **`cursor_fault`** (the feed *is* serving entries
  newer than our last ingest but we store none — a distinct high-severity
  alert), and **`unknown`** (no observation — the legacy behavior, no
  regression). Per-source and per-analyst stall alerts fire on state
  **transitions only** (`entered` / `recovered`) — the durable
  `alert_sink_deliveries` ledger doubles as the state store, so a restart
  cannot re-fire a standing alert as a repeating level.
- **Freshness grades** (`registry/source_freshness.py`, surfaced on
  `GET /api/v1/v3/system/source-firing`). Each active source is graded
  `ok | stale | warn | empty | ungraded` against a **cadence-derived budget**:
  the descriptor's cron is walked (croniter) for its *maximum* fire-to-fire
  gap, × a 4× grace multiple, floored at 30 minutes (`warn` beyond 3× the
  budget). A source with no parseable cadence — or a non-active head — reads
  `ungraded`, never a fake `ok`; an active, budgeted source that has never
  produced reads `empty`.

### 1.2 Push (webhook)

For `acquisition: "push"` the actor registers no reminder. The source's
handler is bound to the shared inbound-webhook router; an inbound POST wakes
the handler, which emits each raw `Signal` through an `emit_signal` callback
the actor supplies (`SourceCore.make_emit_callback`). That callback runs the
*same* baseline → write → publish path as the poll branch — one webhook POST
is one short transaction. A push source is never polled; calling `run` on one
is a no-op.

### 1.3 Source handlers (the kind library)

Every source kind is a handler satisfying the structural-typing contract in
`src/legba/data/sources/_contract.py` (`SourceHandler`): it declares its `kind`
/ `schema_version` / `config_schema` and exposes `pull(ctx, since) ->
AsyncIterator[Signal]` plus `health_check(ctx) -> SourceHealth`. Handlers are
plain Protocol/Pydantic — no base class to inherit.

The reference, fully-wired handler is **RSS/Atom**
(`src/legba/data/sources/rss.py`):

- fetches via `httpx` honouring a stored `(ETag, Last-Modified)` cursor
  persisted in `ctx.state_store` (`If-None-Match` / `If-Modified-Since`);
  HTTP 304 yields an empty iterator;
- parses via `feedparser`, yielding one `Signal` per entry whose
  `published_at` is strictly after `since`;
- maps each entry into a target-agnostic `Signal` (title/link/summary/author/
  tags/body in `payload`, a SHA-256 `content_hash` over external-id+title+body,
  `canonical_url` from the entry link, `language_hint` from the feed);
- has clear failure semantics: transient network/5xx → one retry then empty;
  4xx (≠304) → unhealthy; parse failure → degraded; a single bad pull never
  loses cursor history.

Additional handlers ship in the same package (GDELT, MediaCloud, ACLED,
OpenSanctions, IntelMQ, Telegram, Discord, Firecrawl, Common Crawl, a generic
webhook, scrapers, a generic polled `json_api` for JSON/CSV HTTP APIs, and a
model-free `geojson` GIS handler). Each conforms to the same contract.

The **Telegram** poller is hardened with five bounded guards (2026-07, additive
to the earlier flood-control work): a 60s startup delay, a 15s per-channel
deadline, a 180s whole-cycle cap (per-channel budgets clamp to it), a
`FLOOD_WAIT` abort that persists the server-imposed deadline across polls
(honored on the next cycle rather than hammered), and a stale poll-lock
force-clear (single-flight lock, cleared past 300s) — so one slow or
rate-limited channel can never wedge the cycle or the actor.

The 3-feed RSS set (BBC, Deutsche Welle, Al Jazeera) is the **minimal
cold-start verification set** — the smallest loop that proves the path from
empty volumes. It is *not* the deployed scope. A fresh instance reaches
**current/full scope** by running `scripts/bringup_register_source_catalog.py`,
which registers the **46-entry** source catalog (43 `rss` + 3 `geojson`
handler integrations); the standalone state-media feeds (IRNA / PressTV /
Ukrinform) and the UCDP GED adapter (currently **paused pending an access
token**) are registered as separate descriptor files, and each source now
carries a `source_class` taxonomy tag (`reporting` / `analysis` / `official` /
`state_media`). A representative running deployment has **~53 distinct
sources actively producing signals** (the 46 catalog integrations plus the
state-media feeds and seed / world-baseline curated sources) over a substrate on
the order of tens of thousands of signals → thousands of findings / facts /
nexuses. See `DATA_SOURCES.md` for the full catalog (the
three-tier 3 / 46 / ~53 scope model, the per-source table, and the
handler-kind detail), and `SETUP.md` for the from-zero
cold-start-to-current-scope deploy commands.

**Two draft breadth lanes (2026-07) — registered, operator-activated.** A
breadth wave added **51 draft source descriptors** under `descriptors/`, all
`state: draft` (bulk registration creates no live actor; activation is
`draft → configured → active`, operator-paced):

- **Wave-A** — 41 verified no-auth feeds (38 `rss` + 3 `json_api`; 25
  country-scoped + 16 global), registered by
  `scripts/bringup_register_wave_a_sources.py` with credibility and
  state-affiliation seeds.
- **The RSSHub lane** — 10 descriptors (feeding under-covered watch desks)
  whose `rss` handler points at a **profile-gated local RSSHub service**
  (compose profile `sources-extra`, image `diygod/rsshub`, loopback `:1200`,
  no puppeteer; registered by `scripts/bringup_register_rsshub_sources.py`).
  Because the RSS fetch path carries an SSRF guard that blocks internal
  hosts, the guard takes an explicit allowlist —
  `LEGBA_EGRESS_ALLOW_HOSTS` (comma-separated, exact-name; code default
  empty, compose default `rsshub`) — so exactly the named internal host is
  reachable and nothing else.

---

## 2. The canonical Signal

`Signal` (`src/legba/data/sources/_contract.py`) is the one shape a source
produces. It is **target-agnostic and modality-first**: it carries no
`target_id` — interpretation is target-owned and lives only on derived analyst
outputs; observation is source-owned and shared. `payload` is an open dict;
the field set is `extra="forbid"` so new structured facts are declared on the
model rather than smuggled into the payload. The substrate write
(`write_canonical_signal`) inserts exactly one row into the `signals` table.

Key fields:

**Provenance / ownership**
- `source_id` — the **origin** `SourceDescriptor.id`.
- `source_version` — content-hash of the source descriptor.
- `produced_by_id` / `produced_by_kind` — what produced *this row*:
  `source` (a raw source row; `produced_by_id` null), or `job` / `analyst` /
  `deterministic` / `system` for derived rows.
- `derived_from` — list of upstream signal ids (lineage; empty for a raw row).
- `fetched_at` — ingest timestamp (the fan-out read order key).
- `owner_tenant` — from `SourceDescriptor.scope.owner_tenant`; indexed
  tenancy seam.

**Modality**
- `modality` — `text | image | audio | video | structured | binary`.
- `mime_type`, `media_ref` (object-store URI / external URL — a *reference*,
  never inlined bytes), `embedding_ref` (Qdrant point id for cross-modal
  retrieval), plus a media `retention_class` and `object_ref` for retained
  copies.

**Content + structured-filter columns** (populated once by the baseline,
indexed on `signals` for subscription push-down)
- `payload` (open dict), `canonical_url`, `language_hint`, `raw_provenance`.
- `language` (scalar), `geo` / `tags` / `entity_classes` (arrays),
  `source_credibility` (a per-signal score; per-target floor thresholds are
  evaluated at read time, not stored here).

**Dedup**
- `content_hash` — the dedup key.
- `canonical_signal_id` — set by the dedup analyst to alias a duplicate to its
  canonical row; **never** a destructive collapse (raw rows are preserved and
  linked).

- `schema_uri` — versions the contract (`iglu:legba/signal/jsonschema/3-0-0`).

**Intra-source exact-duplicate collapse at write (2026-07).** A live audit
found ~41% of stored rows were *intra-source* exact-hash duplicates — feeds
re-serving the same entry poll after poll (one earthquake bulletin stored
194×). `write_canonical_signal` (`runtime/source_actor.py`) now runs an atomic
pre-insert check keyed on `(source_id, content_hash, owner_tenant)` inside a
168h window (`LEGBA_INTRASOURCE_DEDUP_WINDOW_HOURS`): on a hit it **bumps the
existing row's `fetched_at` forward and skips the insert** — recency is
preserved, no second row lands. The bump targets the *most-recent* matching
row, so the "earliest `fetched_at` = canonical" rule the dedup analysts rely
on stays stable. It is strictly **intra-source** (cross-source linking remains
§5's alias machinery, never a skipped insert), provably lossless for an exact
hash, and gated by `LEGBA_INTRASOURCE_DEDUP` (**default ON**; empty
`content_hash` is never a dedup key). A skipped-because-duplicate poll is
counted so the liveness watchdog does not read a healthy re-serving feed as an
empty streak. No migration — it rides the existing indexes; the historical
duplicate pool is an operator cleanup previewed by the read-only
`scripts/report_intrasource_dupes.py`.

**Retention + the evidence archive (2026-07).** Two fields close the loop
from citation to preserved evidence. At ingest the source's declared
`SourceScope.license_class` is stamped into `payload.license_class`. Later —
*after* a finding that cites the signal clears the faithfulness verify floor —
the `evidence_archiver` deterministic analyst fetches the signal's original
bytes and stores them content-addressed; the signal's `object_ref` becomes
`cas:sha256/<hex>` and its `retention_class` is upgraded to `evidence_hold`.
The fetch path is deliberately narrow and guarded: **verified-cited-only**
selection (never bulk crawling), the SSRF egress guard, per-host politeness
(2s), a hard 20 MB cap, and the **LIC-2 license gate** — a forbidden
`license_class` (`anti_ai_walled` / `tos_restrictive` / `personal_use_only`)
is *skipped with a recorded counter*, an unknown class archives with the
class recorded. Outcomes land in the `evidence_archive` sidecar (mig 0104:
`archived` / `failed` / `skipped_license` / `skipped_size`), and extracted
full text is marked for re-indexing into the search corpus. See
`ARCHITECTURE.md` §8.6 and `SEAMS.md` #42 for the store and its declared
non-features.

---

## 3. Baseline enrichment (once, at the source)

The baseline runs **once per signal, at the source**, before the write — not
per consuming target. This is the "enrich once, read many" property the
source-first model buys. It is driven by the descriptor's
`pipeline` block (`SourcePipeline`) and implemented in
`src/legba/data/sources/baseline.py` (`run_baseline`).

Two tiers run inline:

1. **Structured-filter enrichment (always, cheap, deterministic).**
   `_enrich_structured` fills the typed indexed columns so a subscriber's
   predicate can match without re-deriving: `language` from the source/payload
   hint, `geo` from the source scope hints, `tags` lifted and normalised from
   the payload, and a backstop `content_hash` when the handler didn't set one.

2. **Media tier (`pipeline.media`).**
   - `reference` (default) — keep `media_ref` as a pointer; do not fetch bytes.
   - `eager` — for sources where the media content *is* the value, dispatch by
     modality to a registered `MediaExtractor` to transcribe/caption/OCR at
     ingest. The mechanism is complete and exercised end-to-end by a working
     in-process extractor; **production hosted Whisper/VLM/OCR extractors
     are a future seam** that register against the same `MediaExtractor`
     protocol. (Tier 3 — on-demand analyst-driven `process_media` — is the
     async job plane, not the baseline; see `ANALYSIS.md`.)

   The `MediaExtractor` registry is the ingest half of the modality →
   {extractor, renderer} registry (`DESIGN.md` §7.5); the UI renderer is the
   other half.

After the two tiers, an optional **enrichment filter chain** runs — the
`pipeline.enrichment` stage list. Each stage is a stream-resident filter
handler (`src/legba/data/filters/`, contract in `_contract.py`:
`StreamHandler.transform(signal, ctx) -> Signal | None`, returning `None` to
drop). The baseline NLP chain is:

- `language_detect` (`legba/filter.language_detect/1-0-0`) — promotes a
  detected `language`;
- `geocode` (`legba/filter.geocode/1-0-0`) — promotes resolved place codes
  into `geo`;
- `ner_multilingual` (`legba/filter.ner_multilingual/1-0-0`) — promotes
  entity classes into `entity_classes`.

These handlers call the hosted `legba-models` service (NLLB translation +
spaCy/GLiREL NER, BAAI/bge-m3 embeddings) — see `AI_MODELS.md`. The chain is
built and stack-configured by the runtime host and threaded into `SourceCore`
as the `enrichment_stage`; when no chain is wired, tier-1 structured
enrichment alone still produces a filterable signal. The net effect:
payload-and-hint data is promoted into indexed columns (`language`, `geo`,
`tags`, `entity_classes`) so the fan-out plane can push matching down to SQL
and NATS subjects.

**Geo honesty — two contamination fixes (2026-07).** A full-layer sweep found
two mechanisms mistagging signals with the *publisher's* geography instead of
the *story's*; both are now precision-improving gates, and both prefer
**untagged over mistagged** (a missing geo only under-includes; a wrong one
actively misroutes a signal to the wrong desk):

- **Publisher-origin fallback is content-corroborated** (`baseline.py`,
  `_origin_corroborated_by_content`). The source's origin country is parked in
  the payload and reaches `signal.geo` only when the *content* attests it —
  the country appears in the title/body text or the NER country entities.
  A Singapore outlet's world-news story no longer tags `SG`.
- **Dateline subject-guard** (`geocode.py`). An NER `location` entity is
  treated as the story's *subject* only if it appears in the **title**;
  body-only locations (datelines, "reported from…") are demoted below the
  in-body country sweep. A wire item datelined in one capital about another
  country no longer tags the dateline.

The gates apply forward; re-geocoding historical rows is a separate
operator-gated backfill.

---

## 4. Fan-out and subscription

A written signal is published once; the subscription plane routes it to every
interested target. The split is deliberate: **coarse** routing on NATS
subjects, **exact** matching downstream on SQL + Starlark. JetStream filters
subjects, not arbitrary JSON, so a subject only ever encodes coarse axes.

### 4.1 The coarse subject taxonomy

Acquisition publishes one message per signal
(`SourceCore._publish`, subject built by `signal_subject` in
`src/legba/data/nats.py`) to:

```
legba.signals.<tenant>.<source_token>.<modality>.<event_class>
```

- `tenant` — `scope.owner_tenant`;
- `source_token` — the source id with reserved chars (notably `.`) flattened
  to `_`, so the id is a single NATS token;
- `modality` — the signal modality;
- `event_class` — `raw` for a source row, `derived` for a job/analyst-produced
  row.

One shared interest stream, **`legba_signals`**, captures
`legba.signals.>`. Per-target consumers attach subject-filtered. Subjects are
never asked to express an arbitrary predicate.

### 4.2 SourceRefs: explicit vs selector

A `TargetDescriptor.sources` is a `list[SourceRef]`
(`src/legba/data/schemas/source.py`). Each ref is **exactly one** of:

- **explicit** — `source_id` names one source directly; resolution is a single
  head-row lookup;
- **selector** — `source_selector` is a coarse query over *source-descriptor
  scope* (`tags ⊇`, `geo ∩`, `languages ∩`, `kinds ∋`, `owner_tenant`, plus an
  optional Starlark residual over source metadata). It binds *any* source whose
  advertised scope matches.

A selector matches sources in the target's own tenant or `shared` only, and
**only `open` sources auto-wire by selector** — `allowlist`/`grant` sources
require explicit opt-in and are never proposed by a selector. Resolution lives
in `src/legba/runtime/subscription/sourceref.py` (`resolve_source_refs`),
producing a set of `ResolvedBinding`s, each carrying that ref's
`Subscription` (the signal-level slice).

A selector is distinct from a `Subscription`: the selector decides which
*sources* a target wires to; the `Subscription` decides which *signals* of a
bound source the target wants.

### 4.3 Structured filter + Starlark residual

Each `Subscription` (`src/legba/data/schemas/source.py`) is a structured
filter — `geo` / `languages` / `tags` / `entity_classes` / `modalities` — plus
an optional `predicate` (Starlark) and a `canonical_only` flag. Matching is
two-stage (`src/legba/runtime/subscription/filter.py`):

1. **Structured → SQL `WHERE`.** Array fields (`geo`/`tags`/`entity_classes`)
   push to GIN indexes as `&&` (any-of) overlap; scalar lists
   (`languages`/`modalities`) push to `= ANY(...)`; `source_id` and
   `owner_tenant` pin the coarse binding facts. `canonical_only` adds the
   dedup-aware delivery clause (deliver a row only if it is itself canonical).
   This is the batch read-slice and the narrowed set the residual runs over.

2. **Starlark residual.** The `predicate` — the long tail (`mentions()`,
   `severity_at_least()`, `geo_in()`, …) — is compiled once via the shared
   engine (`src/legba/data/predicates/`, `compile_predicate`) and evaluated in
   Python on the SQL-narrowed rows only. It is **never** expressed as SQL or as
   a NATS subject. The residual fails *closed*: a budget breach or runtime
   error drops the signal rather than over-delivering. The same matcher serves
   the real-time path (re-check one delivered NATS message via `matches`) and
   the batch path (`read_slice` / `read_target_slice`).

The Starlark predicate DSL is a single-expression, no-I/O, no-recursion
sandbox with a per-evaluation wall-clock budget; it appears at four descriptor
surfaces (target scope gate, source-ref filter, analyst→target bind, analyst
trigger) over one helper catalog.

### 4.4 Subscription policy

A source declares who may subscribe via
`SourceDescriptor.subscription_policy`, enforced at **registration** (the
control plane), not at delivery — the source stays "dumb" and just publishes
(`src/legba/runtime/subscription/policy.py`):

- **open** — any target in the same tenant (or a `shared` source) may attach;
- **allowlist** — only targets in `allowed_targets` / tenants in
  `allowed_tenants`;
- **grant** — requires an explicit grant recorded as a `wiring_descriptor`
  (audit-logged, keyed `(source_id, target_id)`; `write_grant` / `revoke_grant`).

The cross-tenant default-deny boundary lives here: a target may only subscribe
to a source in its own tenant or a `shared` source unless an allowlist/grant
widens it. An unknown policy fails closed. *(The subscription-policy locking and
action-pack grant operator UIs now ship as `legba-ui-v3` panels.)*

### 4.5 Per-target aggregated consumers

The `SubscriptionEngine`
(`src/legba/runtime/subscription/engine.py`) ties it together. On
`register_target` it resolves the refs → enforces policy → plans the coarse
subject filters (`subject_filters_for`, one filter per binding × modality axis,
tenant+source pinned, event-class left wildcard) → binds **one per-target
aggregated** durable PULL consumer (`target_<id>`) onto the union of those
filters (`NatsStore.ensure_durable_consumer`). One consumer per target, not one
per `(target, source)`. A refused binding either raises (strict) or is recorded
on `TargetSubscription.refused` and skipped (non-strict auto-wire). Per-target
consumer lag (`num_pending`) and stream growth are observable
(`consumer_lag` / `stream_growth`).

A late-joining target can register with **catch-up + seamless forward**
(`register_target_with_catch_up`): it captures the stream boundary before
resolving, replays the matching historical slice through a sink once, then
re-binds the live consumer just past the boundary so there is no gap or
duplicate. *(The operator-facing backfill-replay UI now ships as a `legba-ui-v3`
panel; the backend `POST /registry/targets/{id}/backfill` REST route is still a
seam.)*

### 4.6 From match to analysis

A matched signal does not run an analyst directly. The **trigger plane**
(`src/legba/runtime/triggers/`) consumes the `legba_signals` stream, re-checks
each delivered signal against a registration's *full* structured filter +
residual (`TriggerRegistration.matches_signal` → the same `matches` kernel the
subscription filter uses), and
marks the `(analyst, target)` pair **dirty** in a crash-safe accumulator. The
`Coalescer` then fires on whichever gate trips first — **cadence**,
**accumulation** threshold, or **severity** gate — all clamped by a
**cooldown**, with a CAS-guarded fire so two paths never double-dispatch.
Coalescing on a signal's *canonical* id gives alias-no-double-wake: two
deliveries of the same observation count once. A new upstream finding (an
analyst-produced `derived` signal) is just another matching signal on the same
stream — no special case. Both deterministic *and* LLM-bearing analysts dispatch
through this coalescing path: the `ActorTriggerRunner` fires either kind on the
accumulation/severity gates, with the "no per-signal LLM" rule enforced upstream
in the policy (LLM fires floored to a coalesced batch). The full
analyst/coalescing/finding model is documented in `ANALYSIS.md`.

---

## 5. Cross-source dedup

Two independent sources reporting the same event produce two raw signals.
Dedup is **alias/canonical and non-destructive**: a duplicate is *linked* to a
canonical row via `canonical_signal_id`; the raw rows are always preserved.
(A *same-source exact re-serve* never reaches this machinery at all — the
write path's intra-source collapse, §2, bumps the existing row and skips the
insert.)

- A progressive multi-tier dedup filter handler exists
  (`src/legba/data/filters/dedupe.py`): URL-exact → content-hash →
  semantic-vector → temporal, marking (not dropping) a hit
  (`payload.duplicate_of`, `payload.dedupe_tier`). This is the stream-resident
  filter kind. **Source-side ingest dedupe tiers 1–2** (canonical-URL then
  content-hash) ship in `data/filters/ingest_dedupe.py`, applied by the source
  actor's ingest path (`SourceCore`, built from the descriptor's
  `pipeline.ingestion_filters`) so a duplicate is linked to its canonical at
  ingest; tiers 3–4 (semantic/temporal) remain the periodic
  `cross_source_dedup` analyst's job.
- Delivery is dedup-aware via `Subscription.canonical_only` (default true): the
  SQL filter delivers a row only when it is itself canonical, so a subscriber
  sees the observation once.
- Coalescing keys on the canonical id, so even if two aliases reach a target,
  the analyst wakes once.

Because canonical linking is additive, the full raw pool stays intact for
audit and reprocessing; dedup only changes which rows *deliver* and how they
*coalesce*.

---

## 6. Discovery

Discovery **materialises sources and targets** rather than ingesting signals:
an L2/L3 *template* descriptor with a `discovery` block expands into N concrete
L1 instances. The package is `src/legba/data/discovery/`. Two flavours run
through the same machinery (`materializer.py`):

- **Target discovery** (`run_target_discovery_cycle`) — a discovery handler
  emits `CandidateTarget`s; the registry applies the template's deterministic
  relabel chain (Prometheus-style rewrite rules) to each candidate and
  materialises a target instance. (This is how the G20 country targets are
  produced from one template.)
- **Source discovery** (`run_source_discovery_cycle`) — a discovery handler
  emits `CandidateSource`s that become `SourceDescriptor`s.

**Validate-before-register** is the source-discovery default
(`source_validate.py`, `validate_candidate_source`): a candidate becomes a
registered source only after a real probe — (1) **liveness** (build its handler
and run `health_check`; `unhealthy` is rejected), and (2) a **trial pull/parse**
(consume up to a few signals; a handler that raises is rejected; a degraded
source must prove itself by producing ≥1 signal). The probe uses an in-memory
state store and discards pulled signals — a pure dry run against the live
upstream. This keeps the pool clean so selector auto-wire never attracts a dead
feed.

**Selector auto-wire** (`autowire.py`, `auto_wire_discovered_source`) closes
the loop: when a new `open` source registers, the existing source-ref matcher
(`resolve_source_refs`) is *inverted* to ask "which targets' selectors now
match this source?" An auto-wired source is recorded as an idempotent
provenance trailer on each matched target body; the target's declared
SourceRefs are not mutated (the runtime re-resolves the live binding from the
selector each cycle). The same scope/tenancy/policy gates as live binding apply
— only `open`, same-tenant-or-`shared`, scope-matching sources wire.

A disappearance policy (`disappearance.py`) classifies retained / new /
disappeared candidates each cycle and pauses a discovery whose
disappearance ratio exceeds its threshold, so a flaky upstream listing can't
silently retire a fleet of materialised instances. The Discovery Pipeline
operator panel ships in `legba-ui-v3` (it reads the frozen generic registry
routes — no bespoke discovery REST surface).

**Adjacent but distinct: seed adapters.** Seeds (`data/seed/adapters/`,
operator-run via `scripts/seed.py`) materialise *facts*, not sources — they
never touch the signal pipeline (see `ARCHITECTURE.md` §0). One acquisition-
relevant hardening (2026-07): the officeholder adapter (`wikidata_leaders`)
now resolves **exactly one current holder per (country, office)** — the
upstream query drops end-dated statements and the mapper keeps the latest
term-start per office, with head-of-state and head-of-government on separate
supersession keys — and stamps `data.as_of` on every emitted fact so upstream
data-lag is visible. A read-only diagnostic
(`scripts/diagnose_stale_leaders.py` — SELECT-only, writes nothing) previews
the re-seed delta first, because the live upstream can carry vandalism the
heuristic would import; **re-seeding is operator-gated, never automatic**.

---

## 7. End-to-end, in one line

Real RSS feeds → `SourceActor` pulls on a Dapr Reminder → one canonical,
target-agnostic `Signal` per entry, enriched once (language / geo / entity
classes promoted to indexed columns) → published once to
`legba.signals.>` → the shared `legba_signals` stream → per-target aggregated
consumers fan it out by coarse subject, narrowed exactly by SQL `WHERE` +
Starlark residual → matched signals coalesce per `(analyst, target)` → analysts
produce findings with full provenance. The same path, from empty volumes,
brings up cold end-to-end.
