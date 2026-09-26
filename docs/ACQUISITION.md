<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Acquisition — how data enters Legba and reaches analysis

The acquisition plane owns everything from "a source produces an observation" to "that observation is
matched against every interested target". Its governing principle is **source-first**: a source
ingests an observation once, enriches it once, and publishes it once; the fan-out plane then routes
that single canonical observation to the many targets whose predicates select it. Signals are
observations — target-agnostic — not per-target interpretations.

For what happens after a matched signal reaches a desk, see `ANALYSIS.md`. For the substrate stores
see `ARCHITECTURE.md` and `DATA_MODEL.md`; for the hosted NLP models see `AI_MODELS.md`; for the
source catalogue itself see `DATA_SOURCES.md`; for every threshold and budget named below see
`TUNABLES.md`.

**Contents:** [1 The SourceActor](#1-the-sourceactor) · [2 The canonical signal](#2-the-canonical-signal) ·
[3 Baseline enrichment](#3-baseline-enrichment) · [4 Fan-out and subscription](#4-fan-out-and-subscription) ·
[5 Dedup](#5-dedup) · [6 The evidence archive](#6-the-evidence-archive) · [7 Discovery](#7-discovery) ·
[8 Collection requirements](#8-collection-requirements) · [9 End to end](#9-end-to-end)

---

## 1. The SourceActor

A source is a declarative `SourceDescriptor` (`data/schemas/source.py`). The Dapr virtual-actor
runtime turns one descriptor into one `SourceActor` (`runtime/source_actor.py`), which owns
acquisition for that source regardless of how many targets consume it.

The mechanism lives in a plain, directly testable class, `SourceCore`; the thin `SourceActor` Dapr
wrapper delegates to it, so the production path and the tested path are the same code. Cursor and
provisioning state live in a crash-safe `FilterStateStore` (the Postgres `actor_filter_state` table)
rather than in Dapr actor state, so a pull is idempotent across sidecar restarts.

The descriptor's `acquisition` field selects one of two modes.

### 1.1 Poll

**Reads** the persisted cursor (`last_pulled_at`) and the source's own upstream. **Writes** one
canonical signal per surviving entry plus one `source_poll_outcomes` row per poll. **Cadence** is the
descriptor's `cadence.schedule` cron, which the actor turns into a durable Dapr reminder named
`poll_<source_id>` at activation; the reminder survives sidecar restarts. An active, non-discovery
poll source must declare a schedule, enforced by the descriptor validator. Constant-period crons map
cleanly onto a reminder; variable-period schedules are a declared seam.

Each fire calls `SourceCore.pull_once`, which builds the source-kind handler, loads the cursor and
passes it as `since`, iterates `handler.pull(ctx, since)` running the baseline and write path per
yielded signal, publishes each written signal as it is written, and advances the cursor.

A stored cursor dated in the future is discarded as poisoned and the window re-scanned, because dedup
absorbs the overlap and a poisoned cursor otherwise marches forward forever.

**Poll bounds** resolve descriptor config first, then the handler's own advertisement
(`poll_budget_seconds` / `max_entries_per_poll`), then a generic default, each clamped to a ceiling —
so a handler that knows its own shape (Telegram's channel walk) is not truncated by a bound written
for feeds. Per-entry enrichment is separately time-boxed.

**Cursor advance** runs in a `finally`, so it always happens, and has three branches, each closing a
distinct silent-stall class:

- a hard error keeps the prior `since` and retries the window;
- one or more entries consumed advances to the last consumed entry's logical timestamp — not to now,
  which would skip the backlog past the entry cap. A future-dated feed timestamp is clamped to now
  for cursor purposes only; the persisted signal keeps its own value;
- zero entries leaves the cursor unchanged, because advancing to now on a dropped window makes
  `since` march irreversibly forward into a permanent silent stall.

Bulk sources additionally carry a high-water resume offset so a capped walk resumes rather than
restarts.

### 1.2 Push

For `acquisition: "push"` the actor registers no reminder. The handler is bound to the shared inbound
webhook router; a POST to `/api/v1/webhooks/<source_id>`, optionally gated by a shared-secret header,
wakes the handler, which emits each raw signal through an `emit_signal` callback the actor supplies.
That callback runs the same baseline → write → publish path as the poll branch, so one POST is one
short transaction. A push source is never polled.

### 1.3 Source handlers

Every source kind is a handler satisfying the structural-typing contract in `data/sources/_contract.py`:
it declares its `kind` / `schema_version` / `config_schema` and exposes `pull(ctx, since)` yielding
signals plus `health_check(ctx)`. Handlers are plain Protocol and pydantic types — there is no base
class to inherit, so a handler can be tested without a runtime.

`rss` is the reference poll handler: it fetches over `httpx` honouring a stored `(ETag,
Last-Modified)` cursor, parses with `feedparser`, yields one signal per entry published after `since`,
maps each entry into a target-agnostic signal with a SHA-256 `content_hash`, and has explicit failure
semantics — transient network or 5xx retries once then yields empty, a 4xx other than 304 is
unhealthy, a parse failure is degraded, and a single bad pull never loses cursor history.
`generic_webhook` is the reference push kind.

Handlers ship for feeds and APIs (`rss`, `json_api`, `geojson`), bulk and event archives (`gdelt_files`,
`gdelt_query`, `acled`, `ucdp`, `common_crawl_news`), curated data (`mediacloud`, `opensanctions`),
messaging (`telegram_channel`, `discord_webhook`), crawling (`firecrawl`, `scraper`), and the
`intelmq_collector_bridge`. `DATA_SOURCES.md` carries the per-source catalogue; `RELEASE_STATE.md`
carries the live registered count, which is generated rather than hand-typed.

The Telegram poller carries five bounded guards, each logging when it trips, because one slow or
rate-limited channel must never wedge the cycle or the actor: a startup delay (a fresh MTProto client
racing a lingering old container trips an auth-key duplication that permanently kills the session), a
flood-wait abort that persists the server-imposed deadline across polls rather than hammering it, a
single-flight poll lock with a stale-lock force-clear, a whole-cycle cap, and a per-channel deadline
clamped to the cycle deadline. Polls rotate through the channel list with a write-ahead resume
pointer, and a per-channel override lets an individual channel carry its own honest `source_class`
without a second Telegram session.

### 1.4 Poll liveness — quiet against broken

Two mechanisms let the watchdog and the operator tell a quiet feed from a broken cursor rather than
one undifferentiated silence.

`source_poll_outcomes` takes exactly one row per poll, with productivity deciding the outcome: a poll
that wrote a signal or collapsed an intra-source duplicate is `success`, an escaped exception or an
unhealthy handler is `error`, and a clean fetch with nothing new is `empty`. Handler health is trusted
only when the pull ran to a natural conclusion, since a capped pull can leave a stale record. *Why the
`success` outcome exists:* the table was failure-only on the premise that a productive poll is
self-evidencing through its signals rows — which holds for a reader inspecting one poll and fails for
every reader that walks a run, because an absence cannot break a run, so a repaired source kept
presenting its historical error rows as the leading run and the auto-pause sweep re-paused it
mid-ingest.

Each row also carries `newest_entry_ts`: the newest entry timestamp the handler *saw*, recorded before
the since-filter so a poisoned cursor still observes what the feed is serving, with a future-skew
clamp against junk dates and a 304 carrying the prior observation forward. The liveness watchdog's
empty-streak classifier reads it and returns exactly three states — `honest_quiet` (the feed served
nothing new), `cursor_fault` (the feed is serving entries newer than our last ingest and we store
none), and `unknown` (no observation at all). Per-source and per-analyst stall alerts fire on state
transitions only; the durable `alert_sink_deliveries` ledger doubles as the state store, so a restart
cannot re-fire a standing alert as a repeating level.

Separately, `registry/source_freshness.py` grades each source `ok | stale | warn | empty | ungraded`
against a cadence-derived budget: the descriptor's cron is walked for its *maximum* fire-to-fire gap
(so a clustered cron is not read by its naive step), multiplied by a grace factor and floored at a
minimum. A source with no parseable cadence, or a non-active head, reads `ungraded` rather than a fake
`ok`; an active, budgeted source that has never produced reads `empty`. One grading implementation
serves both readers.

---

## 2. The canonical signal

`Signal` (`data/sources/_contract.py`) is the one shape a source produces. It is target-agnostic and
modality-first: it carries no `target_id`, because interpretation is target-owned and lives only on
derived analyst outputs while observation is source-owned and shared. The field set is `extra="forbid"`,
so a new structured fact is declared on the model rather than smuggled into the payload. The substrate
write inserts exactly one row into `signals`.

| Group | Fields | Note |
|---|---|---|
| Provenance | `source_id`, `source_version`, `produced_by_id`, `produced_by_kind`, `derived_from`, `fetched_at`, `last_seen_at`, `owner_tenant` | `produced_by_kind` is `source` for a raw row, `job` / `analyst` / `deterministic` / `system` for a derived one; `owner_tenant` is the indexed tenancy seam |
| Modality | `modality`, `mime_type`, `media_ref`, `embedding_ref` | `media_ref` is a reference, never inlined bytes |
| Retention | `retention_class`, `media_ref_expires_at`, `object_ref` | §6 |
| Content | `payload`, `canonical_url`, `language_hint`, `raw_provenance` | `payload` is the one open dict |
| Filter columns | `language`, `geo`, `tags`, `entity_classes`, `source_credibility` | populated once by the baseline, indexed for subscription push-down |
| Dedup | `content_hash`, `canonical_signal_id` | §5 |
| Contract | `schema_uri` | versions the shape |

**Intra-source exact-duplicate collapse.** Before inserting, the write path looks for an existing row
with the same `(source_id, content_hash, owner_tenant)` inside a lookback window. On a hit it advances
that row's `last_seen_at` in one atomic statement, counts the re-serve, and skips the insert —
returning nothing, so nothing is fanned out. It advances `last_seen_at` and never `fetched_at`,
because bumping `fetched_at` made frozen feeds report themselves as fresh. It is strictly
intra-source: a cross-source same-hash duplicate is kept and alias-linked instead (§5), and an empty
`content_hash` is never a dedup key. *Why it exists:* feeds re-serve the same entry poll after poll,
and a collapse that is provably lossless for an exact hash is the cheapest place to stop it. The
skipped poll is counted so the liveness watchdog does not read a healthy re-serving feed as an empty
streak.

---

## 3. Baseline enrichment

The baseline runs once per signal, at the source, before the write — not per consuming target. This is
the "enrich once, read many" property the source-first model buys. It is driven by the descriptor's
`pipeline` block and implemented in `data/sources/baseline.py`.

**Tier 1 — structured enrichment.** Always, cheap, no external call. Fills the typed indexed columns a
subscriber's predicate matches on without re-deriving: `language` from the source or payload hint,
`tags` lifted and normalised from the payload, and a backstop `content_hash` keyed the same way the
dedup tiers key theirs. The source's origin country is parked in the payload rather than written
straight to `geo` — see the geo gates below.

**Tier 2 — media.** `pipeline.media` is `reference` (default: keep `media_ref` as a pointer, fetch no
bytes) or `eager` (dispatch by modality to a registered `MediaExtractor` and transcribe, caption or
OCR at ingest, for sources where the media content *is* the value). Eager is a declared seam: only a
text passthrough extractor ships, and an eager media signal with no registered extractor raises a
typed error with no row written, rather than fabricating one. Production extractors register against
the same protocol. The on-demand `process_media` tier is the async job plane, not the baseline.

**Tier 3 — the enrichment filter chain.** Optional and descriptor-ordered. Each stage is a
stream-resident filter handler (`data/filters/`) satisfying `transform(signal, ctx) -> Signal | None`,
where `None` drops the signal. Each declares an `output_contract` the registry checks at
pipeline-registration time, before activation. The chain most sources declare is
`language_detect → ner_multilingual → geocode` — language first so NER uses the detected language — with
`fact_extractor` inserted before `geocode` on the sources whose bodies carry extractable relations.
These stages call the hosted `legba-models` service; `ner_multilingual` hard-requires it, while
`fact_extractor` uses it only as an optional fallback. Entity extraction is translate-then-NER for
non-Latin scripts, which otherwise yield essentially no spans.

Two stages write somewhere other than the signal row: `fact_extractor` appends to `facts` with
`source_type='ingestion'`, and `ingest_dedupe` appends a `signal_aliases` row and sets
`canonical_signal_id`. Everything else mutates the one row in place.

**Geo honesty.** Two gates prefer untagged over mistagged, because a missing geo only under-includes a
signal while a wrong one actively misroutes it to the wrong desk. The publisher's origin country
reaches the indexed `geo` column only when the *content* corroborates it — the country appears in the
title or body text, or in the NER country entities — so a Singapore outlet's world-news story does not
tag `SG`. And an NER location is treated as the story's subject only when it appears in the title;
body-only locations (datelines, "reported from…") are demoted below the in-body country sweep, so a
wire item datelined in one capital about another country does not tag the dateline. Both apply
forward; re-geocoding historical rows is a separate operator-gated backfill.

---

## 4. Fan-out and subscription

A written signal is published once; the subscription plane routes it to every interested target. The
split is deliberate: **coarse** routing on NATS subjects, **exact** matching downstream on SQL plus
Starlark. JetStream filters subjects, not arbitrary JSON, so a subject only ever encodes coarse axes.

### 4.1 The coarse subject taxonomy

Acquisition publishes one message per signal to

```
legba.signals.<tenant>.<source_token>.<modality>.<event_class>
```

where `source_token` is the source id with reserved characters (notably `.`) flattened to `_` so the
id is a single NATS token, and `event_class` is `raw` for a source row or `derived` for a
job- or analyst-produced one. One shared interest stream, `legba_signals`, captures `legba.signals.>`;
per-target consumers attach subject-filtered. Subjects are never asked to express an arbitrary
predicate.

### 4.2 Source refs: explicit against selector

A `TargetDescriptor.sources` entry is exactly one of an **explicit** ref naming one `source_id`
(resolved by a single head-row lookup), or a **selector** — a coarse query over source-descriptor
*scope* (`tags ⊇`, `geo ∩`, `languages ∩`, `kinds ∋`, `owner_tenant`, plus an optional Starlark
residual over source metadata) that binds any source whose advertised scope matches. Resolution
produces a set of `ResolvedBinding`s, each carrying that ref's `Subscription`.

A selector matches sources in the target's own tenant or `shared` only, and only `open` sources
auto-wire by selector: `allowlist` and `grant` sources require explicit opt-in and are never proposed.
A selector decides which *sources* a target wires to; the subscription decides which *signals* of a
bound source it wants.

### 4.3 Structured filter plus Starlark residual

Each `Subscription` is a structured filter — `geo` / `languages` / `tags` / `entity_classes` /
`modalities` — plus an optional Starlark `predicate` and a `canonical_only` flag. Matching is two-stage:

1. **Structured to SQL `WHERE`.** Array fields push to GIN indexes as `&&` overlap; scalar lists push
   to `= ANY(...)`; `source_id` and `owner_tenant` pin the coarse binding facts; `canonical_only` adds
   the dedup-aware delivery clause. This is both the batch read-slice and the narrowed set the
   residual runs over.
2. **Starlark residual.** The long tail (`mentions()`, `severity_at_least()`, `geo_in()`, …) is
   compiled once through the shared predicate engine and evaluated in Python on the SQL-narrowed rows
   only. It is never expressed as SQL or as a subject. It fails *closed*: a budget breach or runtime
   error drops the signal rather than over-delivering.

The same matcher serves the real-time path (re-check one delivered message) and the batch path. The
predicate DSL is a single-expression, no-I/O, no-recursion sandbox under a per-evaluation wall-clock
budget, and it appears at four descriptor surfaces — target scope gate, source-ref filter,
analyst-to-target bind, analyst trigger — over one helper catalogue.

### 4.4 Subscription policy

A source declares who may subscribe through `SourceDescriptor.subscription_policy`, enforced at
**registration** in the control plane rather than at delivery, so the source stays dumb and just
publishes:

- **open** — any target in the same tenant, or any target at all for a `shared` source;
- **allowlist** — only the named targets or tenants;
- **grant** — an explicit grant recorded as a wiring descriptor, audit-logged and keyed
  `(source_id, target_id)`.

The cross-tenant default-deny boundary lives here: a target may subscribe only to a source in its own
tenant or a `shared` source unless an allowlist or grant widens it. An unknown policy fails closed.

### 4.5 Per-target aggregated consumers

`SubscriptionEngine.register_target` resolves the refs, enforces policy, plans the coarse subject
filters (one per binding × modality axis, tenant and source pinned, event class left wildcard), and
binds **one per-target aggregated** durable pull consumer onto the union of those filters — one
consumer per target, not one per `(target, source)`. A refused binding either raises, in strict mode,
or is recorded on the target's `refused` list and skipped. Per-target consumer lag and stream growth
are observable.

A late-joining target registers with catch-up and seamless forward: the engine captures the stream
boundary before resolving, replays the matching historical slice through a sink once, then binds the
live consumer just past the boundary, so there is neither a gap nor a duplicate.

### 4.6 From match to analysis

A matched signal does not run an analyst directly. The trigger plane consumes the `legba_signals`
stream, re-checks each delivered signal against the registration's *full* structured filter and
residual using the same match kernel, and marks the `(analyst, target)` pair dirty in a crash-safe
accumulator. The coalescer then fires on whichever gate trips first, clamped by a cooldown and guarded
by a compare-and-set claim so two paths never double-dispatch. A new upstream finding — an
analyst-produced `derived` signal — is just another matching signal on the same stream, with no
special case. `ANALYSIS.md` documents the gates.

---

## 5. Dedup

Two independent sources reporting the same event produce two raw signals. Dedup across sources is
alias-and-canonical and **non-destructive**: a duplicate is linked to a canonical row through
`canonical_signal_id`, and the raw rows are always preserved, so source-level evidence stays
audit-grade. Three layers do the work:

- **At ingest, in the write connection.** `ingest_dedupe` applies tiers 1 and 2 — canonical-URL hash,
  then content hash — after the insert, writing a `signal_aliases` row and setting the alias row's
  `canonical_signal_id`, chaining through to the earliest canonical rather than to another alias. A
  failure here leaves the row raw for the periodic analyst rather than failing the write.
- **The intra-source collapse** (§2), the one path that suppresses a row, and only for byte-identical
  content from the same source.
- **Periodically.** `cross_source_dedup` sweeps the shared raw pool for content-hash duplicates and,
  when a Qdrant client and embeddings are present, semantic near-duplicates; `cross_source_coalesce`
  closes the "same event, different source, different wording, no shared hash" gap over one shared
  embedding collection. The coalescer has no non-vector fallback, since exact-hash matching is the
  other analyst's job, so with its embedding service absent it refuses loudly and writes zero aliases
  rather than fabricating links.

The canonical is chosen deterministically — earliest `fetched_at`, tie-broken by smallest id — and
points its own `canonical_signal_id` at itself. Delivery is dedup-aware through
`Subscription.canonical_only`, so a subscriber sees the observation once, and coalescing keys on the
canonical id, so even if two aliases reach a desk the analyst wakes once. The identity rules the
tiers share live in one URL-canonicalisation module, because the ingest engine, the baseline backstop
and the filter used to disagree about what the same URL is.

Because canonical linking is additive, the full raw pool stays intact for audit and reprocessing;
dedup only changes which rows *deliver* and how they *coalesce*.

---

## 6. The evidence archive

At ingest the signal carries the source's declared `license_class` in its payload and a
`retention_class` that defaults to reference-only — bytes are not fetched at ingest. Archival is a
separate deterministic analyst, `evidence_archiver`, sweeping on its own cadence.

**It reads** only cited evidence: signals named in the `derived_from` of a verified, non-superseded
finding whose folded confidence clears the verify floor, with no archived copy yet and a non-empty
canonical URL. **It writes** the original bytes content-addressed on a filesystem volume, recorded on
the signal as `object_ref = cas:sha256/<hex>`, upgrades the signal's `retention_class` to
`evidence_hold` (exempting it from the retention sweep), sets the corpus dirty marker in the same
update so the extracted full text replaces the thin teaser in the search corpus, and upserts the
`evidence_archive` sidecar. **It is measured** by that sidecar's outcome vocabulary.

The fetch path is deliberately narrow and guarded, because a citation-preservation job is the one
place a platform is tempted into bulk crawling: verified-cited-only selection, the SSRF egress guard,
per-host politeness, a hard size cap, a bounded per-run fetch budget, and a licence gate under which a
forbidden `license_class` is skipped with a recorded counter while an unknown class archives with the
class recorded.

**A block is not an empty web.** A modern bot interstitial is a large page, so a challenge that is not
detected lands as a thin or failed extraction and reads downstream as *the web had nothing* — a false
absence, which is the worse defect. The challenge detector classifies at three tiers — extracted text
with no length cap, the raw body bounded only for cost (for the tells that never survive extraction),
and the HTTP status for the case where the body was already discarded — and the verdict travels under
one name, `blocked_by_challenge`. The archiver counts it, tags the run's finding, and records the
stored interstitial with its tell, because the bytes *are* the evidence of the block; the outbound
research tool counts it and marks the hit blocked rather than failed. An edge refusal with no body
stays retryable, since that is exactly the row a future fetch option exists to unblock. A payment
status is deliberately excluded: a paywall is a licence decision, not a challenge. The licence gate is
untouched by any of this.

**Browser-fingerprint impersonation** is available behind a flag and ships off. Unset — every shipped
deployment — the page-fetch client is exactly the guarded client it has always been, structurally: the
off path is a single statement returning the guarded client with the caller's arguments untouched, and
does not import the impersonation module at all. Set, it presents a browser TLS fingerprint while
sending the same identifying user agent, never a browser one, because the robots decision is computed
for our token and must describe the agent in the publisher's log; the SSRF guard is re-expressed on
that path as a pre-request host assertion plus a hand-walked redirect loop, and robots.txt itself is
still fetched with the plain guarded client. It stays off because it was measured and did not work:
the blocks in question are IP-reputation or genuine challenges, not TLS fingerprinting. Publishers
that close themselves in robots.txt are never fetched by any option here.

---

## 7. Discovery

Discovery **materialises sources and targets** rather than ingesting signals: a template descriptor
carrying a `discovery` block expands into concrete instances. Both flavours run through the same
machinery.

**Target discovery** emits candidate targets; the registry applies the template's deterministic relabel
chain to each candidate and materialises a target instance. This is how the country desks are produced
from one template. **Source discovery** emits candidate sources that become source descriptors.

The relabel chain is a closed set of rewrite actions applied by the registry, never by the handler, so
a discovery kind cannot invent a label shape. Each candidate carries a stable natural key, its raw
label set, and its evidence.

**Validate-before-register** is the source-discovery default: a candidate becomes a registered source
only after a real probe — build its handler and run `health_check` (unhealthy is rejected), then a
trial pull and parse (a handler that raises is rejected; a degraded source must prove itself by
producing at least one signal). The probe uses an in-memory state store and discards what it pulls, so
it is a pure dry run against the live upstream. *Why:* this keeps the pool clean, so selector
auto-wire never attracts a dead feed.

**Selector auto-wire** closes the loop by inverting the source-ref matcher: when a new `open` source
registers, it asks which targets' selectors now match it. An auto-wired source is recorded as an
idempotent provenance trailer on each matched target body; the target's declared source refs are never
mutated, because the runtime re-resolves the live binding from the selector each cycle. The same
scope, tenancy and policy gates as live binding apply.

A **disappearance policy** classifies retained, new and disappeared candidates each cycle and pauses a
discovery whose disappearance ratio exceeds its threshold, so a flaky upstream listing cannot silently
retire a fleet of materialised instances.

**Seed adapters are adjacent but distinct.** Seeds materialise *facts*, not sources, and never touch
the signal pipeline. One acquisition-relevant property: the officeholder adapter resolves exactly one
current holder per `(country, office)` — the upstream query drops end-dated statements, the mapper
keeps the latest term-start per office, and head of state and head of government sit on separate
supersession keys — and stamps an as-of date on every emitted fact so upstream data lag is visible. A
read-only diagnostic previews the re-seed delta first, because the live upstream can carry vandalism
the heuristic would import; re-seeding is operator-gated, never automatic.

---

## 8. Collection requirements

Discovery answers *what else could we acquire?* This answers the harder question in the other
direction — *what did we need and not have?* — and makes the answer durable rather than a sentence
inside a monthly finding that scrolls away.

`collection_gap` is a deterministic, no-LLM analyst. **It reads** the persisted scorecard rows, finding
the desk × dimension cells banded `insufficient-evidence`, and a second backlog of `hypotheses` rows
with `status='source_request'` — the rows the operator-gated `request_source` tool lands when an
analyst hits a coverage wall. **It writes** both into `collection_requirements`: the desk and
dimension, the topic, the rationale, the evidence it came from by id, which source classes would
plausibly feed it, and up to a few candidate sources. **It is measured** by its own run counters and,
like every deterministic producer, by a trace receipt. **Its cadence** is monthly, with a daily
sibling dispatching reference gaps through the same writer rather than forking it.

The candidates are deterministic, not proposed by a model: a plain SQL match against the registered
source descriptors on declared source class and geo overlap, across *any* lifecycle state, so that
reuse comes before create — a paused or draft match is a reactivation candidate whose registered URL
becomes the suggested fetch URL, and an already-active match is an honest quality-gap flag. Where
nothing matches, the requirement is stamped not fillable with a reason, which is a more useful
operator artifact than a fabricated suggestion; a paired constraint makes an unfillable requirement
without a reason a database error.

Idempotency is a unique natural key with an insert that does nothing on conflict, so a cell that stays
starved across sweeps stays **one** requirement rather than a new one each month. Priority is
inherited from the gap ranking — desks with more starved dimensions first, then persistence.

**A proposal is never an activation**, and that is enforced structurally rather than by convention.
The analyst reads the source registry and has no write path to it. The route is disposition-only: list,
get, and a patch that may set only the status, reviewer and note. There is no create and no delete,
the content columns are write-once, and nothing on the route touches the source registry. The
disposition vocabulary is closed and validated at both the route and the database; `registered` records
that the operator *separately* added a source through the normal registration path, and setting it
performs no activation. Nothing consumes a requirement: exactly one writer and one dispositioner
reference the table, and no job, analyst or ingest path watches for a disposition and acts on it. The
requirement is a note to the operator, and the operator is the loop.

---

## 9. End to end

Real feeds → the `SourceActor` pulls on a Dapr reminder → one canonical, target-agnostic signal per
entry, enriched once so language, geo and entity classes land in indexed columns → published once to
`legba.signals.>` → the shared `legba_signals` stream → per-target aggregated consumers fan it out by
coarse subject, narrowed exactly by a SQL `WHERE` plus a Starlark residual → matched signals coalesce
per `(analyst, target)` → analysts produce findings with full provenance. The same path brings an
instance up cold from empty volumes.
