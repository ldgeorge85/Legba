# Legba — Data Sources

*The source-handler catalog. What each kind reaches, how it's configured, how to add your own.*

In Legba's source-first model, a **source owns acquisition**. A `SourceActor`
turns a `SourceDescriptor` into a running actor that either polls on a cadence
or receives a push, produces **one canonical, target-agnostic `Signal`**,
enriches it once (baseline language / geo / entity NER), and publishes it once
to NATS JetStream. Signals carry no `target_id` — they are observations, and the
fan-out plane routes each one to many subscribing targets by predicate. This doc
catalogs the **source-handler kinds** that produce those signals.

For the descriptor schema see `src/legba/data/schemas/source.py`; for the
acquisition pipeline (actor → baseline → publish) see `ACQUISITION.md` and
`DESIGN.md`; for the predicate fan-out / subscription side see `DESIGN.md`.

---

## 1. The `Signal` a handler produces

Every handler — regardless of kind — yields the same `Signal` shape
(`src/legba/data/sources/_contract.py`). The handler fills only the
observation-side fields; the per-source baseline fills the rest:

- **Ownership / provenance** — `source_id`, `source_version`, `fetched_at`,
  `owner_tenant`. `produced_by_kind` is `"source"` for a raw row.
- **Modality-first** — `modality` (`text` / `image` / `audio` / `video` /
  `structured` / `binary`), `mime_type`, `media_ref` (a *reference*, never
  inlined bytes), `embedding_ref`.
- **Content** — open `payload` dict, `canonical_url`, `language_hint`,
  `raw_provenance`.
- **Indexed filter columns (set by baseline enrichment, not the handler)** —
  `language`, `geo`, `tags`, `entity_classes`, `source_credibility`. These are
  promoted to indexed columns on the `signals` table so subscription SQL +
  NATS subject pushdown can filter on them.
- **Dedup / lineage** — `content_hash`, `canonical_signal_id` (alias links,
  never destructive collapse), `derived_from`, `schema_uri`.

A handler MUST persist a **cursor** via `ctx.state_store` so a restart doesn't
re-pull the whole feed, and SHOULD treat `since` as a hint — downstream dedup
absorbs overlap windows.

---

## 2. Acquisition modes: poll vs push

`SourceDescriptor.acquisition` is `"poll"` or `"push"`
(`"stream"` is a documented future seam, not implemented). The two modes map to
two protocols in `src/legba/data/sources/_protocols.py`:

- **`PollSource`** — Legba *pulls* on a cadence. The `SourceActor` registers a
  Dapr **Reminder** from `descriptor.cadence.schedule`; on each fire it drains
  the handler's `pull(ctx, since)` async generator, runs the baseline, writes,
  and publishes. An active poll source must declare a `cadence.schedule`.
- **`PushSource`** — an upstream system *POSTs* events to Legba. The shared
  inbound-webhook router (`webhook_router.py`) receives the POST and hands the
  raw body + headers to the handler's `ingest(ctx, body, headers)`, which
  verifies + parses and yields the same `Signal` shape. Push sources never poll.

A third, **orthogonal** capability — `ProvisioningSource` — lets a poll *or* push
source register an outbound upstream watch at activation (`on_activate`) and
deregister at retirement (`on_retire`), reconciled idempotently via
`src/legba/data/sources/provision.py`.

Every concrete handler declares class-vars `kind`, `family = "source"`,
`schema_version`, and `config_schema` (a Pydantic model used for
descriptor-validation-time config parsing).

---

## 3. Implemented source kinds

The runtime discovers handlers by walking `legba.data.sources` and collecting
`kind → handler class` pairs (`src/legba/data/sources/__init__.py`). **Fourteen**
first-party kinds are registered today. Credential references in config are
**vault references** (a dotted credential id), never the raw secret — the
runtime resolves them at call time and the handler never caches them past a
single pull.

### 3.1 `rss` — RSS 2.0 / Atom 1.0 feeds

*Poll · free · `RSSSourceHandler` (`rss.py`)*

Fetches any standards-conformant RSS/Atom feed via `httpx`, parses with
`feedparser`, and yields one signal per entry published after the cursor.
Honors conditional GET — stores `(ETag, Last-Modified)` in `state_store` and
sends `If-None-Match` / `If-Modified-Since`; HTTP 304 yields an empty pull.

**Key config (`RSSConfig`)** — `url` (required), `parser`
(`auto` | `rss` | `atom`), `user_agent`, `timeout_seconds`.

The broadest, most permissive surface: works against any feed, no auth. It is
the kind used for the 3-feed minimal cold-start set (BBC / Deutsche Welle /
Al Jazeera) and for 43 of the 46 catalog integrations (§3.15).

### 3.2 `gdelt_query` — GDELT 2.0 via BigQuery

*Poll · free tier (GCP-billed scan) · `GDELTBigQuerySourceHandler` (`gdelt.py`)*

Queries the public GDELT `events` / `gkg` tables in BigQuery. Pushes filters
(country, actor, event class, tone) and a date partition prune down into the
storage layer so scans stay small. 100+ languages, ~15-minute refresh,
CAMEO/FIPS event coding.

**Key config (`GDELTConfig`)** — `bq_credentials_secret` (vault ref to the GCP
service-account JSON), `bq_project_id`, `bq_location` (default `US`);
filters `cameo_country` (FIPS-10-4 two-letter), `actor_filter` (regex),
`event_root_codes` (CAMEO), `tone_filter` (min/max `AvgTone`); `lookback_minutes`
(default 15).

**Cost control is first-class.** Every pull does a BigQuery **dry-run** estimate
and refuses to execute if it exceeds `cost_cap_bytes_per_pull` (default 1 GiB;
hard ceiling 10 GiB). An optional `daily_cap_bytes` tracks cumulative bytes in
`state_store` and refuses pulls that would blow the rolling-day budget;
`max_rows_per_pull` caps the `LIMIT`. Over-cap raises `CostCapExceeded`.

### 3.3 `acled` — ACLED conflict / protest events

*Poll · free for non-commercial use · `ACLEDSourceHandler` (`acled.py`)*

Paginates the ACLED REST API at `api.acleddata.com/acled/read`. ACLED requires
both an API key **and** the registered email for rate-limit attribution.

**Key config (`ACLEDConfig`)** — `api_key_secret` (vault ref), `email`
(required); filters `country` (ISO-3166-1 alpha-3), `event_types` (validated
against the ACLED taxonomy), `region` (validated against ACLED regions);
`lookback_days` (default 7 — ACLED publishes weekly), `page_size` (≤ 5000).
Cursor tracks the highest `event_date` seen; same-day overlap is resolved by
downstream dedup on `external_id`.

ACLED's license is non-commercial (journalists / NGOs / academia); a commercial
license is required for business use.

### 3.4 `mediacloud` — Media Cloud open-news corpus

*Poll · free tier · `MediaCloudSourceHandler` (`mediacloud.py`)*

Pulls stories from the Berkman-Klein Media Cloud corpus (billion-story scale,
worldwide) over its v4 HTTP `search/story-list` API via `httpx` (the handler
deliberately does **not** take the synchronous upstream `mediacloud` client, to
keep the actor's event loop non-blocking).

**Key config (`MediaCloudConfig`)** — `api_key_secret` (vault ref), `query`
(Elasticsearch-style query-string DSL), `collections` (numeric collection ids),
`language` (ISO-639-1), `lookback_days` (default 1, floor on the cursor window),
`page_size` (≤ 1000); `fetch_missing_text` pulls article body when the response
omits it. 429s are retried with backoff (`rate_limit_*` knobs); exhaustion
raises `MediaCloudRateLimited`.

### 3.5 `opensanctions` — sanctions / PEPs / criminal lists

*Poll · free (bulk) or paid (API) · `OpenSanctionsSourceHandler` (`opensanctions.py`)*

Reaches the OpenSanctions consolidated entity datasets (sanctions targets,
politically-exposed persons, criminal lists) over three operator-chosen modes.
Entities follow the FollowTheMoney schema; the `followthemoney` package is an
optional dep with a pass-through fallback when absent.

**Key config (`OpenSanctionsConfig`)** — `mode`:

- `bulk_csv` (default, **no auth**) — full dataset from
  `data.opensanctions.org/datasets/latest/<dataset>/targets.simple.csv`.
- `api` — low-volume drill-down against `api.opensanctions.org`; requires
  `api_key_secret`.
- `self_hosted` — license-compliant high volume; requires `base_url`.

Plus `dataset` (e.g. `all` / `sanctions` / `peps` / `us_ofac_sdn`),
`schema_filter` (FollowTheMoney schemas to keep), `api_page_size`,
`max_bulk_rows` (cap per bulk pull for incremental backfill). The OpenSanctions
`topics` controlled vocabulary (`sanction`, `role.pep`, `crime`, …) is preserved
on the signal payload for downstream filters.

### 3.6 `scraper` — generic pluggable web scraper

*Poll · free (proxy costs optional) · `ScraperSourceHandler` (`scraper.py`)*

A generic crawler that owns rate-limiting, robots.txt, BFS depth, and proxying,
delegating site-specific URL discovery + extraction to a drop-in **scraper impl**
referenced by dotted path. Depends only on `httpx` + `trafilatura` +
`feedparser`. An example impl ships at
`legba.data.sources.scrapers.example_news:ExampleNewsScraper`.

**Key config (`ScraperConfig`)** — `impl` (dotted path `pkg.mod:Class`),
`seed_urls`, `max_depth` (0–10), `rate_limit` (e.g. `"10/min"`, enforced by a
sliding-window token bucket), `respect_robots` (per-host robots cache,
fail-open per RFC 9309), `request_timeout_seconds`, `user_agent`; optional
`proxy_pool` (a `StackRef` to a residential-proxy pool component) and
`proxy_country`.

### 3.7 `firecrawl` — AI-friendly URL extraction

*Poll · paid (credit-based) · `FirecrawlSourceHandler` (`firecrawl.py`)*

Wraps the Firecrawl REST API (`api.firecrawl.dev`) over `httpx` with
`Authorization: Bearer`. Distinct from `scraper`: the cleaned, LLM-consumable
**markdown is the product**. Mode-dispatched (`scrape` single-page sync,
`crawl` async job polled to completion, `map` URL discovery).

**Key config (`FirecrawlConfig`)** — `api_key_secret` (vault ref), `seed_urls`,
`mode` (`scrape` | `crawl` | `map`), `extract_format` (`markdown` | `html` |
`links` | `screenshot`), `max_depth`, `include_paths` / `exclude_paths`,
`crawl_limit`, `crawl_max_polls` / `crawl_poll_interval_seconds`. Emits a
`CreditUsageRecord` per pull for the budget ledger. 401/403 → `FirecrawlAuthError`
(hard); 429 → `FirecrawlRateLimited` (retryable).

### 3.8 `telegram_channel` — Telegram channels

*Poll · free (account-bound) · `TelegramChannelSourceHandler` (`telegram.py`)*

Reads public Telegram channels via Telethon (a user-session, MTProto client).
`on_activate` connects the client; `iter_messages` is paginated per channel.
Media metadata only (type / mime / size) is attached when `include_media` is
set — **bytes are never downloaded** by this handler; downstream analysts fan
out for OCR / transcription. `FloodWaitError` is honored inline up to a cap.

**Key config (`TelegramChannelSourceConfig`)** — `api_id_secret`,
`api_hash_secret`, `session_secret` (all vault refs; the base64 Telethon session
is generated out-of-band), `channels` (handles or numeric ids),
`lookback_hours` (default 24), `include_media`, `per_channel_message_limit`
(≤ 1000), retry / backoff / `flood_wait_cap_seconds`.

### 3.9 `discord_webhook` — Discord (inbound)

*Push · free · `DiscordWebhookSourceHandler` (`discord.py`)*

A **push** source: Discord POSTs interaction / message events to a registered
webhook; the shared router verifies the request and hands it to `ingest`.
Every request's **Ed25519 signature is verified** (PyNaCl) against the
application's public key before any event is emitted; a bad signature raises and
the router returns 4xx. Interaction `PING`s are answered without producing a
signal.

**Key config (`DiscordWebhookConfig`)** — `application_id` (stamped on every
signal for provenance), `public_key_secret` (vault ref → hex Ed25519 public
key), optional `allowed_event_types` whitelist (verification still runs on
filtered events; non-listed events simply aren't emitted).

### 3.10 `common_crawl_news` — Common Crawl CC-NEWS

*Poll · free (anonymous S3 read) · `CommonCrawlNewsSourceHandler` (`common_crawl.py`)*

Streams WARC records from the public Common Crawl CC-NEWS bucket
(`s3://commoncrawl/crawl-data/CC-NEWS`, `us-east-1`) via `aiobotocore` —
anonymous read, no auth. Primarily a historical-backfill / bulk-coverage source
(~daily partitions over a large news-site set).

**Key config (`CommonCrawlNewsConfig`)** — `s3_bucket` / `s3_region` /
`s3_endpoint_url` (override for a MinIO/LocalStack mirror) / `prefix`,
`lookback_days`, `language_filter` (ISO-639-3, matched against the WARC
content-language header), `host_filter` (regex against hostname, e.g. country
scoping), and hard caps `max_records_per_run` / `max_warc_files_per_run` /
`max_body_bytes` (each WARC is ~1 GB; the runtime re-invokes `pull` to continue
from the cursor).

### 3.11 `intelmq_collector_bridge` — IntelMQ collector bots

*Poll · free · `IntelMQCollectorBridge` (`intelmq.py`)*

Bridges IntelMQ's CERT-grade catalog of 200+ collector bots into Legba so we
reuse them rather than re-implementing feed collectors. Hooks the IntelMQ
Collector → Parser boundary: collector bots emit IDF (IntelMQ Data Format)
events, which the bridge translates to `Signal`s (full IDF preserved under
`payload['idf']`; geo / ASN / actor hints lifted to top-level slots).

**Key config (`IntelMQBridgeConfig`)** — `mode`:

- `subprocess` — runs `python -m <bot_module>` one shot per pull and reads
  JSON events from stdout. Requires the `legba[intelmq]` optional extra.
- `redis_pipe` — drains an IntelMQ Redis destination queue (`LPOP`/`RPOP`).
  Uses the base Redis client; does **not** require the extra.

Plus `bot_module`, opaque `bot_config` (validated by IntelMQ, not Legba), the
`intelmq_redis_*` connection block (`redis_pipe` mode), `subprocess_timeout_s`
(`subprocess` mode), and `max_events_per_pull`. IntelMQ is a heavy optional
dependency, imported lazily; enabling the kind without the extra raises
`IntelMQNotInstalled`.

### 3.12 `generic_webhook` — reference inbound webhook

*Push · free · `GenericWebhookSourceHandler` (`generic_webhook.py`)*

The minimal reference **push** kind, with no external dependency. An upstream
POSTs an arbitrary JSON body; the handler emits one signal. Optional minimal
auth via a constant-time `shared_secret` check against the `X-Webhook-Token`
header.

**Key config (`GenericWebhookConfig`)** — `shared_secret` (optional),
`modality` (stamped on the signal, default `structured`), `id_field` /
`url_field` (payload keys for external id + canonical URL), optional
`media_ref_field` (a payload key whose value becomes the signal's `media_ref`).

### 3.13 `json_api` — generic polled JSON/CSV HTTP API

*Poll · free (API-dependent) · `JsonApiSourceHandler` (`json_api.py`)*

The generic **poll** counterpart to `generic_webhook`: reaches any read-only
HTTP API that returns a JSON document (or CSV table) containing an array of
items — ReliefWeb, GDELT DOC 2.0, CKAN portals, status feeds — without a
bespoke handler per provider. The poll window is cursor-driven
(`state_store["json_api_cursor"].last_pulled_at`); `url_template` placeholders
`{date_today}` / `{date_yesterday}` / `{window_start_iso}` / `{window_end_iso}`
are substituted (URL-quoted) from that window. Items are located via a small
dot/bracket JSONPath-lite resolver (no new deps; `a.b`, `a[0]`, `a['k']`) and
mapped to signals through configurable field paths. Items timestamped at or
before the window start are skipped; unstamped items flow through and
downstream content-hash dedupe absorbs overlap.

**Key config (`JsonApiConfig`)** — `url_template` (required; GET only),
`response_format` (`json` | `csv` via stdlib `csv`), `items_path`, field
mappings `id_path` / `title_path` / `url_path` / `timestamp_path` /
`body_path` / `geo_path`, `modality` (`text` | `structured`), optional `auth`
(`mode` header|query, `name`, `secret_ref` vault ref, `value_template` e.g.
`"Bearer {secret}"`), `lookback_minutes` (first-run window),
`max_items_per_pull` (default 100 — the source plane's per-poll bound),
`static_tags`, `language`.

**Fail-loud auth.** When `auth` declares a `secret_ref` and no secrets
resolver is wired, `on_configure` / `on_activate` / `pull` raise
`JsonApiAuthNotConfigured` and `health_check` reports `unhealthy` — a keyed
descriptor never polls unauthenticated. The resolved secret is applied as a
separate request header/param, so the rendered URL stamped into
payload/provenance never contains it. Example descriptors:
`descriptors/source_reliefweb_api.yaml` (keyless, ReliefWeb `appname`
convention) and `descriptors/source_gdelt_doc_api.yaml` (keyless GDELT DOC
2.0 — the no-billing GDELT alternative).

---

### 3.14 `geojson` — GeoJSON / GIS documents (model-free, structured)

Polls a configurable GeoJSON (RFC 7946) document URL and emits
`modality="structured"` Signals (`mime_type="application/geo+json"`) with
geometry-derived `geo`. No ML model is in the loop — this is the model-free
structured/GIS path (the bundled USGS-earthquakes descriptor is the example).
The live geo view is the separate v4 Leaflet Dockview panel
(`MapPanel` → `LeafletWorldMap`), which renders geometry from the substrate; the
inline `application/geo+json` modality renderer remains a badged placeholder
(SEAMS #13) and is not the path used today.

A curated catalog of **46 real-world feeds** (43 `rss` + 3 `geojson`: USGS
`significant_week`, NWS `severity=Severe,Extreme`, NASA EONET `days=3` GeoJSON,
EMSC retired, plus assorted curated RSS — GDACS, ICG, WHO, IAEA, CDC, HRW,
Amnesty, CIVICUS, …) is registered by the `CATALOG` tuple in
`scripts/bringup_register_source_catalog.py` — the operator-facing reference for
what concrete `geojson` / `rss` descriptors exist out of the box. This is the
**full/current-scope** bring-up path (see §3.15).

### 3.15 Deploying to current scope (cold-start vs full catalog)

The 3-feed RSS set (BBC / Deutsche Welle / Al Jazeera) is the **minimal
cold-start verification set** — the smallest end-to-end loop that proves the
acquisition → fan-out → analysis path from empty volumes. It is *not* the
deployed scope and not a proven-live limit.

To stand a fresh instance up to **current/full scope**, run the catalog
bring-up:

```
python scripts/bringup_register_source_catalog.py        # register all 46
python scripts/bringup_register_source_catalog.py --verify  # dry-run probe feeds first
```

This registers the full **46-entry** `CATALOG` (43 `rss` + 3 `geojson`
handler integrations), seeds the `source_credibility` table, and (with
`--verify`) probes each feed for liveness before registering. The `geojson` /
`json_api` / additional keyed kinds (GDELT, ACLED, MediaCloud, …) are then
authored as individual `SourceDescriptor`s on top of this base.

**Validated live scope** (a representative running deployment):

| Metric | Count |
|---|---|
| Distinct sources actively producing signals | **49** |
| Signals ingested | **54,197** |
| Findings produced | **19,629** |
| Facts | **3,019** |
| Nexuses | **3,822** |
| Situations | **25** |
| Hypotheses | **398** |

The 49 live sources = the 46 catalog handler integrations plus the seed /
world-baseline curated sources. The relation/extract backend in the NER chain
is **GLiREL** (`jackboyla/glirel-large-v0`) emitting real per-relation
confidence scores — see §3.16 below and `AI_MODELS.md`.

### 3.16 NER / relation extraction backend

The baseline NER + relation enrichment (`ner_multilingual`,
`fact_extractor`) calls the hosted NLP stack, whose relation-extraction model
is **GLiREL** (`jackboyla/glirel-large-v0`). GLiREL emits **real per-relation
confidence scores** (live facts span 0.75 / 0.80 / 0.92 / 0.95, with only a
small tail at exactly 1.0), not a synthetic constant. See `AI_MODELS.md` for
the full model inventory.

> Note: some in-repo `src/*.py` code comments still name an older "REBEL"
> backend; those comments are stale and a tracked code-cleanup follow-up
> (reconciling the conf-1.0 sentinel against GLiREL's real scores). The
> deployed backend is GLiREL.

---

## 4. Cost-tier summary

| Kind | Mode | Cost tier | Auth | Reaches |
|---|---|---|---|---|
| `rss` | poll | free | none | any RSS/Atom feed |
| `geojson` | poll | free | none | any RFC-7946 GeoJSON document URL |
| `gdelt_query` | poll | free tier (GCP scan-billed; capped) | GCP service account | GDELT 2.0, 100+ langs, ~15-min |
| `acled` | poll | free (non-commercial) | API key + email | ACLED conflict/protest events |
| `mediacloud` | poll | free tier | API key | Media Cloud open-news corpus |
| `opensanctions` | poll | free (bulk) / paid (API) | none (bulk) / API key | sanctions / PEPs / criminal lists |
| `scraper` | poll | free (+ optional proxy cost) | none / proxy pool | arbitrary sites via drop-in impl |
| `firecrawl` | poll | paid (credit-based) | API key | arbitrary URLs → clean markdown |
| `telegram_channel` | poll | free (account-bound) | Telegram API id/hash/session | public Telegram channels |
| `discord_webhook` | push | free | Ed25519 public key | Discord interactions / messages |
| `common_crawl_news` | poll | free (anon S3) | none | Common Crawl CC-NEWS WARCs |
| `intelmq_collector_bridge` | poll | free | per-bot | IntelMQ's 200+ collector bots |
| `generic_webhook` | push | free | optional shared secret | any inbound JSON POST |
| `json_api` | poll | free (API-dependent) | none / vault header or query key | any JSON/CSV HTTP API with an item array |

---

## 5. Source credibility scoring

Source credibility is a **per-signal float in `[0.0, 1.0]`** annotated by the
`source_credibility` filter (`src/legba/data/filters/source_credibility.py`),
not by the source handler itself. It runs in the baseline enrichment chain and
is informational — it **flags but never drops** a signal.

How it works:

1. Extract a source host from the signal — preferring `canonical_url`, falling
   back to `payload["source_url"]` / `url` / `link`.
2. Normalize the host: lowercase, strip `www.`, drop user-info + port, decode
   punycode (IDN).
3. Look it up in the registry-backed `source_credibility` table (created in
   `0001_baseline.sql`; the migration head is **0046**). On a miss, retry
   against progressively-trimmed parent domains
   (`news.bbc.co.uk` → `bbc.co.uk` → `co.uk`); first hit wins. A small
   per-instance TTL'd LRU cache (default 3600 s) avoids re-querying the same
   host.
4. Stamp `source_credibility` + `source_credibility_rationale` on the signal;
   set `below_credibility_threshold = True` when the score is strictly below the
   configured `min_score` (default 0.3).

**Config (`SourceCredibilityConfig`)** — `min_score`, optional `default_score`
(applied to unknown hosts; otherwise they stay null), `cache_ttl_seconds`,
`cache_max_entries`. The table is operator-curated through the registry API
(`/api/v1/registry/source_credibility`,
`src/legba/data/registry/source_credibility_api.py`); the filter is read-only.

Per-target credibility **floors** are evaluated at subscription / read time (a
predicate over the stored score), not on the signal row — so the same scored
signal can be admitted by a lenient target and filtered out by a strict one.

---

## 6. Adding a new source kind

A source kind is a drop-in. There is no central registration list to edit beyond
one table entry; the runtime discovers handlers structurally.

1. **Write a handler class** in a new module under
   `src/legba/data/sources/`. Implement the `SourceHandler` protocol from
   `_contract.py` — declare the class-vars:

   ```python
   kind: ClassVar[str] = "my_kind"
   family: ClassVar[str] = "source"
   schema_version: ClassVar[str] = "legba/source.my_kind/1-0-0"
   config_schema: ClassVar[type[BaseModel]] = MyConfig
   ```

   No base class to inherit — the contract is a `runtime_checkable` Protocol;
   structural conformance is enough.

2. **Pick a mode.** For a poll source, implement
   `pull(ctx, since) -> AsyncIterator[Signal]` (satisfies `PollSource`); for a
   push source, implement `ingest(ctx, body, headers) -> AsyncIterator[Signal]`
   and set `push_source = True` (satisfies `PushSource`). Both also implement
   `async health_check(ctx) -> SourceHealth`. If the source needs an outbound
   upstream registration, also implement `on_activate` / `on_retire`
   (`ProvisioningSource`).

3. **Define a Pydantic `config_schema`** with `extra="forbid"`. Store
   credentials as vault references (a dotted credential id), never raw secrets —
   resolve them at pull time via `ctx.secrets_resolve` (or a constructor-injected
   resolver). Validate filters at config time so bad descriptors fail fast.

4. **Yield `Signal`s**, not enriched rows. Set the observation-side fields
   (`modality`, `payload`, `canonical_url`, `language_hint`, `media_ref` as a
   reference). Leave `language` / `geo` / `tags` / `entity_classes` /
   `source_credibility` to the baseline. Persist a cursor in `ctx.state_store`.

5. **Register the module** by adding one `(module_name, class_name)` tuple to
   `_SOURCE_MODULE_TABLE` in `src/legba/runtime/source_factory.py`. The factory
   defensively imports each module, so a missing optional dependency logs +
   skips that one kind without poisoning the registry, and inspects your
   `__init__` signature to thread in only the dependencies you declare (config,
   and `secrets_resolve` under any of the `secret_resolver` /
   `credential_resolver` / `secrets_resolve` parameter names).

Once registered, an operator can author a `SourceDescriptor` of that `kind`, the
`SourceActor` schedules it, the baseline enriches its signals, and the fan-out
plane routes them to subscribing targets — no other code changes.

---

## 7. Future seams (not yet working)

- **Eager media extraction** — `descriptor.pipeline.media = "eager"` wires a
  modality → `MediaExtractor` registry (`baseline.py`); production handlers
  (Whisper / VLM / OCR against the hosted endpoints, SeaweedFS object store) are
  thin today. Handlers attach `media_ref` references; bytes are not fetched.
- **`stream` acquisition** — `SourceDescriptor.acquisition` documents a third
  `"stream"` mode; only `poll` / `push` are implemented.
- **Source-side dedup tiers** — `content_hash` / `canonical_signal_id` carry the
  alias-link contract. Source-side ingest dedupe tiers 1–2 (canonical-URL then
  content-hash) now run at ingest (`data/filters/ingest_dedupe.py`); the periodic
  `cross_source_dedup` analyst still owns tiers 3–4 (semantic / temporal).
