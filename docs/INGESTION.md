# Ingestion Pipeline

The ingestion service is a standalone, continuously running process that fetches signals from configured sources, deduplicates them, stores them across three backing stores, and clusters them into derived events. It is fully deterministic — no LLM calls. It runs independently of the agent and can be deployed separately.

Entry point: `python -m legba.ingestion.service`

## Pipeline Flow

```
Sources (RSS, API, GeoJSON, CSV, Telegram)
    │
    ▼
┌─────────┐   fetch_source() / TelegramFetcher
│  Fetch   │   HTTP GET with auth, retry, UA fallback
└────┬────┘
     │  List[FetchedEntry]
     ▼
┌───────────┐  normalize_entry() / normalize_telegram_message()
│ Normalize  │  → timestamp parsing, category inference, spaCy NER
└────┬──────┘
     │  List[Signal]
     ▼
┌──────────────┐  ModelsClient (GPU, optional)
│ Model Enrich  │  → translate non-English titles, DeBERTa classify
└────┬─────────┘
     │  List[Signal] (enriched)
     ▼
┌────────┐  DedupEngine — 4-tier check
│ Dedup   │  GUID → URL → vector cosine → Jaccard title
└────┬───┘
     │  Survivors only
     ▼
┌───────┐  StorageLayer — triple-write
│ Store  │  Postgres + OpenSearch + Qdrant (embedding)
└────┬──┘
     │  Periodically (every ~10 min)
     ▼
┌──────────┐  SignalClusterer
│ Cluster   │  Signals → derived Events
└──────────┘
```

## Source Types

| Type | Parser | Content-Type | Notes |
|------|--------|-------------|-------|
| `rss` | feedparser | XML/Atom | Default. Falls back to JSON if feedparser returns 0 entries |
| `api` | JSON item extraction | `application/json` | Auto-discovers collection key from ~20 known keys (`articles`, `results`, `data`, `items`, etc.) |
| `geojson` | FeatureCollection parser | JSON | Extracts `properties` as entry fields, preserves `geometry` for geo resolution |
| `csv` | `csv.DictReader` | text/csv | Row-per-entry (e.g., NASA FIRMS fire data) |
| `static_json` | Same as `api` | JSON | For single-object APIs (e.g., exchange rates); wraps entire response as one entry |
| `telegram` | Telethon MTProto | N/A | Separate fetch path via `TelegramFetcher` (see below) |

### Authentication

Auth config lives in the source's `data` JSONB column under `auth_config`. Environment variable references (`$ENV_VAR`) are resolved at fetch time.

| Auth Type | Config Shape | Mechanism |
|-----------|-------------|-----------|
| `api_key` | `{"type": "api_key", "header": "X-Api-Key", "value": "$API_KEY_ENV"}` | Custom header |
| `query_param` | `{"type": "query_param", "key": "api_key", "value": "$API_KEY_ENV"}` | Appended to URL. Skipped if `query_template` already resolved env vars |
| `bearer` (static) | `{"type": "bearer", "token": "$TOKEN_ENV"}` | `Authorization: Bearer <token>` |
| `bearer` (OAuth2 client_credentials) | `{"type": "bearer", "token_url": "...", "client_id": "$CID", "client_secret": "$CS"}` | Token exchange, cached until expiry |
| `bearer` (OAuth2 ROPC) | `{"type": "bearer", "token_url": "...", "grant_type": "password", "username": "$U", "password": "$P"}` | Password grant (e.g., ACLED) |

### Query Templates

Sources can define a `query_template` URL with placeholders:

| Placeholder | Resolves To |
|-------------|-------------|
| `{since_iso}` | Last fetch time as ISO 8601 (or 7 days ago on first run) |
| `{timespan}` | Adaptive: `15min` to `30d` based on time since last fetch |
| `{date_today}` | `YYYY-MM-DD` |
| `{date_yesterday}` | `YYYY-MM-DD` |
| `$ENV_VAR` | URL-encoded environment variable value |

## Normalization

`normalize_entry()` converts a `FetchedEntry` into a `Signal`:

1. **Timestamp extraction** — tries (in order): feedparser `struct_time` fields, `entry.published` string, 20+ raw_data date field names, epoch-ms detection, nested date objects. All results are sanity-checked (must be within past year / 1 day future).

2. **Category inference** — priority: source-level category (from `sources.category` column) > regex keyword matching > `OTHER`. Eight categories: `conflict`, `disaster`, `health`, `economic`, `political`, `technology`, `environment`, `social`. Each has a compiled regex pattern; the category with the most keyword hits wins.

3. **Entity extraction (spaCy NER)** — lazy-loads `en_core_web_sm`. Runs on `title + summary` (capped at 1000 chars) when no actors/locations were extracted from structured fields. Extracts `PERSON`/`ORG`/`NORP` as actors, `GPE`/`LOC`/`FAC` as locations, capped at 10 each.

4. **Source-specific normalizers** — dispatched by `source_name` via `get_source_normalizer()`. Return a `SourceOverrides` object that selectively replaces fields. Examples: GDELT (tone-based confidence, domain tags), USGS (magnitude/depth extraction), NWS (severity mapping), FIRMS (lat/lon from CSV row).

## Model Enrichment

Optional GPU-accelerated enrichment via the `legba-models` FastAPI service (`ModelsClient`). Health-checked every ~60s. If unavailable, ingestion continues with regex classification and raw titles.

| Capability | Endpoint | Behavior |
|-----------|----------|----------|
| Translation | `POST /translate` | Non-English signals: translates title to English, stores `original_lang:{code}` tag. Input capped at 2000 chars |
| Classification | `POST /classify` | DeBERTa zero-shot. Replaces regex-inferred category if confidence > 0.5 and result is not `other` |
| Relation extraction | `POST /extract` | Returns `{subject, predicate, object}` triples (not used during ingestion currently) |
| Summarization | `POST /summarize` | Multi-text summarization (available but not used during ingestion) |

All calls are best-effort — exceptions are caught and logged at debug level.

## Deduplication

`DedupEngine` runs a 4-tier check. Batch mode reduces 2N database queries to 2 using `ANY()` array matching. An additional intra-batch dedup pass catches duplicates within a single fetch.

| Tier | Method | Storage | Threshold |
|------|--------|---------|-----------|
| 1. GUID | Exact match on `signals.guid` | Postgres | Exact |
| 2. Source URL | Exact match on `signals.source_url` | Postgres | Exact |
| 3. Vector cosine | Embed title via vLLM, search Qdrant nearest neighbor | Qdrant (`legba_signals` collection) | >= 0.92 |
| 4. Jaccard title | Tokenize title, strip source suffixes/prefixes, compare word sets | In-memory cache (last N titles) | >= 0.4 (short titles, <=5 words) or >= 0.5 (normal) |

**Title normalization** (tier 4): before Jaccard comparison, titles are stripped of:
- Source suffixes: ` - Reuters`, ` | BBC News`, ` - AP`, etc. (~30 known outlets)
- Common prefixes: `Live Updates:`, `Breaking:`, `Watch:`, `Exclusive:`, etc.
- Stopwords, punctuation, and words <= 1 char

**Cache**: the last `dedup_cache_size` (default 500) signal titles are held in memory. The cache is refreshed from Postgres every ~10 minutes and updated on each store.

## Storage

`StorageLayer` performs a triple-write for each signal:

| Store | What | Purpose |
|-------|------|---------|
| **Postgres** | Full signal row (`signals` table) with JSONB `data` column | Primary persistence, relational queries, entity linking |
| **OpenSearch** | Signal document (title, summary, category, timestamp, actors, locations) | Full-text search, aggregations |
| **Qdrant** | Title embedding vector (1024-dim via `embedding-inno1` model) | Semantic dedup (tier 3), vector-based clustering |

After Postgres write, two best-effort operations run:
- **Embedding**: title is embedded via the vLLM OpenAI-compatible `/embeddings` endpoint and upserted to Qdrant
- **Entity auto-linking**: actors/locations are matched against the `entities` table for graph connectivity

Redis is used for metrics counters and heartbeat tracking (not signal data).

## Clustering

`SignalClusterer` runs every ~10 minutes (tick offset). It groups unclustered signals into derived events.

### Process

1. **Fetch**: recent unclustered signals (no entry in `signal_event_links`) within the time window, excluding `other` category.

2. **Feature extraction**: for each signal, extract entity set (actors + locations, lowercased), title words, timestamp, category, confidence.

3. **Similarity scoring**: pairwise, with two modes depending on vector availability:

   **Vector mode** (if >50% of signals have Qdrant vectors):

   | Component | Weight |
   |-----------|--------|
   | Cosine similarity (embedding vectors) | 0.6 |
   | Temporal proximity (linear decay over 48h) | 0.2 |
   | Category match (same = 1.0, different = 0.0) | 0.2 |

   **Keyword fallback** (if vectors unavailable):

   | Component | Weight |
   |-----------|--------|
   | Entity Jaccard (actors + locations overlap) | 0.3 |
   | Title word Jaccard | 0.3 |
   | Temporal proximity (linear decay over 48h) | 0.2 |
   | Category match | 0.2 |

4. **Clustering**: single-linkage with union-find. Threshold: 0.5. Cluster size capped at 30 to prevent mega-buckets from high-frequency entities.

5. **Event creation**:
   - **Multi-signal clusters (2+)**: create a new event or merge into an existing one if entity overlap >= 0.3 with a recent event in the same category.
   - **Singletons**: auto-promoted to 1:1 events only if from a structured source (NWS, USGS, GDACS, ACLED, etc.) and not `environment` category.

6. **Reinforcement**: when signals merge into an existing event, confidence is boosted (capped at 0.8). Threshold crossings at 3, 5, 10, and 20 signals trigger reinforcement notifications.

7. **Situation linking**: new and reinforced events are auto-linked to active situations via entity substring matching.

8. **Proxy candidate flagging**: when an event involves 3+ actors in conflict or political categories, the clusterer flags it as a proxy relationship candidate to a Redis queue (`legba:proxy_candidates`). The conscious agent reads these candidates during ORIENT and can investigate them during SURVEY/ANALYSIS cycles.

## Telegram Integration

Telegram channels are configured as sources with `source_type = "telegram"` and URL format `telegram://@channelhandle`.

| Component | Details |
|-----------|---------|
| Client | Telethon (MTProto), lazy-imported |
| Auth | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` env vars |
| Session | Persisted at `TELEGRAM_SESSION_PATH` (default `/shared/telegram.session`). Initial auth via `telegram_auth.py` |
| Fetch | `iter_messages()` since last fetch (or last 1h), text-only (media/service messages skipped), up to `batch_size` |
| Normalization | Separate path: `normalize_telegram_message()`. Title extracted from first sentence. GUID = `tg:{channel}:{message_id}`. Confidence scaled by view count |
| Pipeline | After normalization, feeds into the same dedup + store pipeline as RSS/API signals |

Enabled by `TELEGRAM_ENABLED=true`. If Telethon is not installed or session is unauthorized, gracefully disabled.

## Scheduling

The service runs a tick loop with configurable interval (default 30s):

1. **Tick**: query `sources` table for active sources where `next_fetch_at <= NOW()` or `last_successful_fetch_at IS NULL` (never fetched).
2. **Priority**: never-fetched sources first, then most overdue.
3. **Concurrency**: up to `max_workers` (default 4) sources fetched in parallel via `asyncio.Semaphore`.
4. **Advance**: `next_fetch_at` is updated immediately on selection (prevents re-selection during processing).
5. **Failure handling**: consecutive failures tracked per source. After `auto_pause_threshold` (default 10) consecutive failures, source status is set to `error`.
6. **Periodic tasks** (offset from each other):
   - Dedup cache refresh: every ~10 min
   - Batch entity linking: every ~30 min
   - Signal clustering: every ~10 min

Per-source fetch interval is stored in `sources.fetch_interval_minutes` (default 60).

## User-Agent Handling

```
Default:  Legba-SA/1.0 (autonomous research agent; +https://github.com/ldgeorge85/legba)
Browser:  Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...
```

Resolution order:
1. Per-source override: `data.user_agent` field in the source's JSONB config (passed as `user_agent` kwarg to `fetch_source()`)
2. Default bot UA (honest identification)
3. On 403/405: automatic retry with browser UA string

## Configuration

All settings are read from environment variables with sensible defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `INGESTION_CHECK_INTERVAL` | `30` | Seconds between scheduler ticks |
| `INGESTION_MAX_WORKERS` | `4` | Max concurrent source fetches |
| `INGESTION_HTTP_TIMEOUT` | `30` | Per-source HTTP timeout (seconds) |
| `INGESTION_DEDUP_CACHE_SIZE` | `500` | Recent titles in memory for Jaccard dedup |
| `INGESTION_BATCH_SIZE` | `50` | Max entries per source fetch |
| `INGESTION_HEALTH_PORT` | `8600` | Health/metrics HTTP endpoint port |
| `INGESTION_AUTO_PAUSE_THRESHOLD` | `10` | Consecutive failures before auto-pause |
| `INGESTION_LOG_LEVEL` | `INFO` | Log level |
| `EMBEDDING_API_BASE` | — | vLLM embedding endpoint base URL |
| `EMBEDDING_API_KEY` | — | API key for embedding endpoint |
| `MEMORY_EMBEDDING_MODEL` | `embedding-inno1` | Embedding model name |
| `MEMORY_VECTOR_DIMENSIONS` | `1024` | Embedding vector dimensions |
| `MODELS_API_URL` | — | legba-models GPU service URL |
| `MODELS_API_USER` / `MODELS_API_PASS` | — | legba-models auth |
| `TELEGRAM_ENABLED` | `false` | Enable Telegram ingestion |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | Telegram API credentials |
| `TELEGRAM_SESSION_PATH` | `/shared/telegram.session` | Telethon session file path |
| `QDRANT_HOST` / `QDRANT_PORT` | `localhost` / `6333` | Qdrant vector store |

Standard Postgres, Redis, OpenSearch, and NATS connection vars are shared with the agent (see `shared/config.py`).

### HTTP Retry

Transient HTTP errors (429, 502, 503) are retried up to 3 times with backoff schedule `[5s, 15s, 45s]`. `Retry-After` header is honored (capped at 60s).

## Key Files

| File | Role |
|------|------|
| `ingestion/service.py` | Main loop, orchestrator, tick scheduling |
| `ingestion/fetcher.py` | HTTP fetch, RSS/JSON/GeoJSON/CSV parsing |
| `ingestion/normalizer.py` | FetchedEntry to Signal conversion, NER, category inference |
| `ingestion/source_normalizers.py` | Source-specific field overrides (GDELT, USGS, NWS, etc.) |
| `ingestion/dedup.py` | 4-tier deduplication engine |
| `ingestion/storage.py` | Triple-write to Postgres + OpenSearch + Qdrant |
| `ingestion/cluster.py` | Signal-to-event clustering |
| `ingestion/models_client.py` | GPU models service client (translation, classification) |
| `ingestion/telegram.py` | Telethon-based Telegram channel fetcher |
| `ingestion/telegram_normalizer.py` | Telegram message to Signal conversion |
| `ingestion/scheduler.py` | Source scheduling queries |
| `ingestion/config.py` | Environment-based configuration |
