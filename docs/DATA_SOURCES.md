<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Data sources

For someone adding sources to an instance or keeping them running: how sources
work, the kinds, adding a source or kind, credentials, licences, and what
happens while a source polls.

## How sources work

A **source** is one `SourceDescriptor` (`src/legba/data/schemas/source.py`).
The runtime turns each active descriptor into one `SourceActor`
(`runtime/source_actor.py`), which fetches for that source however many targets
read it. A **signal** is one observation (a feed entry, an event, a message). It
carries no target. The source writes, enriches and publishes it once.

**Fan-out** routes each signal to every target whose subscription matches it,
so a hundred targets sharing ten feeds still means ten polls. A target names its
sources in `TargetDescriptor.sources`, each entry either an explicit
`source_id` or a selector over source scope (tags, geo, languages, kinds,
tenant). A selector binds only sources in the target's own tenant or `shared`.
The source's `subscription_policy` decides who may subscribe:

| Policy | Who may subscribe |
|---|---|
| `open` | any target in the same tenant, or any target for a `shared` source; the only policy selectors wire automatically |
| `allowlist` | only the named targets or tenants |
| `grant` | targets holding an explicit, audit-logged grant |

Signals are published to the NATS subject
`legba.signals.<tenant>.<source_token>.<modality>.<event_class>` on the stream
`legba_signals`, with one durable consumer per target. After delivery, the
subscription's structured filter (`geo`, `languages`, `tags`,
`entity_classes`, `modalities`) runs as a SQL `WHERE`, then an optional
Starlark predicate. A predicate that errors or overruns its time budget drops
the signal. What happens to matched signals is in [ANALYSIS.md](ANALYSIS.md).

## What is registered

The repository carries every `descriptors/source_*.yaml` file plus the active
profile's source catalog (`profiles/<id>/rosters/source_catalog.yaml`). Every
file ships `state: draft`. A draft registers but gets no actor, so it polls
nothing.

`deploy/deploy.sh` runs `scripts/bringup_register_descriptors.py --profile <dir>`.
It registers every descriptor and catalog entry of the profile, skips the rows
in `descriptors/RETIRED.md`, and moves each one to the state the profile's
lifecycle overlay records ([descriptors/LIFECYCLE.md](../descriptors/LIFECYCLE.md));
a source with no overlay row stays `draft`. Re-running it is safe. It also seeds
per-host `source_credibility` rows without overwriting an admin's edits. After
activation the files still read `draft`; the registry holds the real state.

What is live on one instance is generated, by kind, into
[RELEASE_STATE.md](RELEASE_STATE.md). Rows named `src_autowire*` or `src_tmpl*`
are generated fan-out templates, not feeds.

For outlets with no feed, an optional **RSSHub** sidecar (its own compose
profile, loopback-bound) turns an outlet route into a feed the `rss` kind polls.
Add the sidecar to the runtime's egress allowlist first; the egress guard
blocks internal hosts.

## Source kinds

**Twenty-two** first-party kinds are registered today: eighteen declared in
`src/legba/data/sources/kinds/core.py` and four plug-in kinds (`page`,
`portwatch`, `series_api`, `stream_json`). `tests/runtime/test_data_sources_doc_kind_count.py`
checks this number against the registry. `page` is the kind behind on-request
fetching ([operations/on-request-fetching.md](operations/on-request-fetching.md));
`portwatch` is not in the table below.

A poll source sets its interval in `cadence.schedule`. The window column names
the handler's own setting for how far back one pull reaches. Credentials in
config are vault refs (a dotted credential id, never the secret itself),
resolved at pull time.

| Kind | Mode | What it reaches | Window settings | Notes |
|---|---|---|---|---|
| `rss` | poll | any RSS 2.0 or Atom 1.0 feed | `cursor_lookback_hours`, `first_poll_max_entry_age_days` | Conditional GET, so a 304 is an empty pull. Defaults: 72 h lookback, 30-day first-poll cap. No auth. |
| `json_api` | poll | a read-only JSON or CSV API returning an item array | `lookback_minutes`, `cursor_lookback_hours`, `max_items_per_pull` | Field paths in config. Timestamps without a zone are read as UTC. Secret goes in a header or param. |
| `geojson` | poll | an RFC 7946 GeoJSON URL | `max_features` | Geometry kept; coordinates fill the indexed geo column. No model involved. |
| `email_imap` | poll | one folder of an IMAP mailbox you control | `since_days`, `max_messages_per_poll` | Read-only, TLS only. One signal per message from `allow_from`. Refuses to run without a password. |
| `gdelt_files` | poll | GDELT 2.0 events as 15-minute CSV dumps | `lookback_minutes`, `max_files_per_pull`, `max_rows_per_file`, `max_signals_per_pull` | No key, no rate limit. Filters hard before emitting. |
| `gdelt_query` | poll | GDELT 2.0 events and GKG through BigQuery | `lookback_minutes`, `max_rows_per_pull` | Dry-runs each query and refuses one over the per-pull byte cap. GCP service account. |
| `acled` | poll | ACLED conflict and protest events | `lookback_days`, `page_size` | API key and registered email. Non-commercial licence. |
| `ucdp` | poll | the UCDP Georeferenced Event Dataset | `lookback_days`, `max_pages`, `page_size` | Free access token; without one the pull is skipped. CC BY 4.0. |
| `mediacloud` | poll | the Media Cloud news corpus | `lookback_days`, `page_size` | API key. Backs off on rate limits. |
| `opensanctions` | poll | sanctions, PEP and criminal lists | `api_page_size`, `max_bulk_rows` | Modes `bulk_csv` (no auth), `api` (key), `self_hosted`. |
| `instrument_snapshot` | poll | one published instrument as a whole document (XML, CSV, HTML, PDF) | — | Archives each version and emits one signal with the diff when it changes. See below. |
| `series_api` | poll | an endpoint returning a numeric time series | `lag_days`, `revision_days`, `backfill_days`, `backfill_from` | Writes observations, not signals. See below. |
| `scraper` | poll | arbitrary sites through a site-specific implementation | `max_depth` | Obeys robots.txt. Optional proxy pool. |
| `firecrawl` | poll | arbitrary URLs, returned as markdown | `crawl_limit`, `max_depth` | Paid, credit-metered; each pull records its credit use. |
| `telegram_channel` | poll | public Telegram channels | `lookback_hours`, `per_channel_message_limit`, `max_entries_per_poll` | One running descriptor per session. Media metadata only. |
| `common_crawl_news` | poll | the Common Crawl CC-NEWS archive | `lookback_days`, `max_records_per_run`, `max_warc_files_per_run`, `max_body_bytes` | Anonymous S3 read. Mainly backfill. |
| `intelmq_collector_bridge` | poll | IntelMQ collector bots | `max_events_per_pull` | Subprocess mode needs the optional extra; Redis-queue mode does not. |
| `discord_webhook` | push | Discord interactions and messages | — | Ed25519 signature checked on every request. |
| `generic_webhook` | push | any inbound JSON POST | — | Optional shared-secret header. |
| `stream_json` | subscribe | JSON records over a held connection | `first_connect_max_entry_age_days` | Same item mapping as `json_api`. See [Subscribed sources](#subscribed-sources). |

**`instrument_snapshot`** archives each version of a whole document (a
sanctions list, an export-control supplement) and compares it with the last. A
change emits one signal with both hashes, the diff and entry counts; for XML and
CSV the diff is keyed on the list's own identifier (`key_path`). An unchanged
document writes a "checked, unchanged" receipt. It needs a writable archive.
Draft examples are in `profiles/sanctions_export_control/descriptors/`.

**`series_api`** writes one dated value per series, subject and period, only
when it changes. Row location, date format, value, subject and unit are all
declared in config from fixed vocabularies. Each poll re-reads
`lag_days + revision_days` to catch revisions.

## Adding a source

Adding a source needs no code. A descriptor is one YAML document:

```yaml
identity:                # id, name, kind, schema_uri, version, state, owner, created
  id: source.example.world
  kind: rss
  state: draft           # registers without a live actor; an admin activates it
scope:                   # what target selectors match against
  owner_tenant: shared
  geo: []                # omit for a global feed
  languages: [en]
  tags: [news, world]
  classifications: []    # optional: [<family>:<value>] of registered families
  source_class: reporting
  license_class: permissive_feed_unreviewed
acquisition: poll        # or push, or subscribe
cadence:                 # poll only
  schedule: { factory_kind: cron, raw: "*/15 * * * *" }
  cooldown_seconds: 60
  jitter_seconds: 30
config:                  # validated against the kind's own config_schema
  url: { factory_kind: text, raw: "https://example.org/feed.xml" }
pipeline:                # enrichment filters and media policy
  media: reference
```

`subscription_policy` defaults to `open`. `scope.classifications` values must
come from vocabulary families already registered; every signal the source
writes carries them as tags (see [DATA_MODEL.md](DATA_MODEL.md)).

To add the source:

1. Register it as `draft` with `POST /api/v1/registry/descriptors/source`.
2. Check the URL and field paths against the live upstream. A wrong path makes
   the handler emit nothing and report unhealthy.
3. Move it `draft → configured → active` with
   `POST /api/v1/registry/descriptors/source/{id}/transition` (the same route
   pauses and resumes; `/retire` retires).

The Source Registry panel in the console does the same ([UI.md](UI.md)).
Targets whose selectors match the new source pick it up.

## Adding a source kind

1. Write a handler class in a new module under `src/legba/data/sources/` that
   satisfies the `SourceHandler` protocol in `_contract.py`. Declare `kind`,
   `family = "source"`, `schema_version` and `config_schema`. There is no base
   class; matching the protocol is enough.
2. Implement the mode. A poll kind implements `pull(ctx, since)` and yields
   signals. A push kind implements `ingest(ctx, body, headers)` and sets
   `push_source = True`. A kind that can serve `acquisition: subscribe`
   implements `parse_event(ctx, event, stream_url=, transport=)`. All implement
   `health_check(ctx)`. A kind that must register a watch upstream also
   implements `on_activate` and `on_retire`.
3. Make `config_schema` a pydantic model with `extra="forbid"`. Keep credentials
   as vault refs resolved at pull time. Validate filters in the schema so a bad
   descriptor fails at registration.
4. Yield plain signals: modality, payload, canonical URL, language hint and a
   media reference. Leave language, geo, tags, entity classes and credibility to
   enrichment. Treat `since` as a hint; dedup absorbs overlap.
5. Declare the kind: add a module under `src/legba/data/sources/kinds/` that
   calls `declare_source_kind("<module>", "<ClassName>")` (see `kinds/page.py`).
   A separate package can declare kinds through the `legba.source_kinds` entry
   point group instead. A declared module that fails to import stops startup,
   so import optional packages inside the handler.
6. Update the kind count sentence above; its test fails until you do.

## Credentials

The shipped catalog uses keyless kinds. Each kind below needs a secret in the
vault, ships as its own `draft` descriptor, and polls only after an admin adds
the secret and activates it. The descriptor headers say how.

| Kind | Needs | Notes |
|---|---|---|
| Telegram | API id, API hash and a session, as vault refs | The session is generated outside Legba. One running descriptor per session. |
| Firecrawl | API key | Paid; each pull records its credit use. |
| MediaCloud | API key | Free tier, rate-limited. |
| Discord | the application's Ed25519 public key | Inbound webhook. |
| GDELT via BigQuery | a GCP service account | Billed per scan; bounded by the per-pull byte cap and an optional daily budget. |
| OpenSanctions | nothing for bulk; a key for `api`; a base URL for `self_hosted` | |
| IntelMQ | an IntelMQ deployment, optionally its Redis queue | |
| ACLED | API key and the registered email | Non-commercial licence. |
| UCDP | a free access token | |
| Residential proxies | a `proxy_pool` stack component | Only for `scraper`. |

A second Telegram client on the same session kills the session. Reviewed
channels that are not yet on can wait in a second descriptor left `configured`;
to switch one on, move its handle and `config.classes` entry into the running
descriptor. The poller bounds each cycle with a startup delay, a flood-wait
abort, a single-flight lock, a cycle cap and a per-channel deadline.

## Source class

`scope.source_class` is the source's editorial type. Analysts use it to weigh a
claim by what kind of source made it. The default is `reporting`.

| `source_class` | Meaning |
|---|---|
| `reporting` | News and wire reporting; the default when unsure |
| `analysis` | Research groups, data projects, think tanks, OSINT groups, coded-event datasets |
| `official` | Government, IGO and primary-source publishers |
| `state_media` | State-controlled outlets, read as a government's position |
| `claim` | A party's own claim, or a tracker of such claims. See [Claim sources](#claim-sources) |

The `narrative_coordination` unit reads `state_media` as framing; state media is
ingested so a desk can see what each side says. The hourly salience sweep turns
the class into a fixed `authority` rank: `official` 4, `reporting` 3,
`analysis` 2, `state_media` 1, and 0 for a source with no class. The
`collection_gap` analyst uses the same classes.

A `telegram_channel` descriptor may set a class per channel in
`config.classes`; keys must be configured channels.

Publicly funded broadcasters with conventional editorial practice stay
`reporting`, with a state-affiliation flag on their credibility row.

### Claim sources

A claim source carries a party's own claim, such as a group announcing an
attack or a tracker listing what such groups posted. It sets
`scope.source_class: claim` and a `scope.claim` block
(`src/legba/data/schemas/source_claim.py`) with `claimant` (the name rows are
attributed to), optional `claimant_path` (a payload field naming the claimant
per row) and `claimant_aliases`, and `verb` (`claimed`, `posted` or `listed`).
A model never chooses the claimant.

A claim source may not declare a `fact_extractor` stage, a `follow` policy,
`pipeline.media: eager`, a media object store, or any sink other than
`document`. A single Telegram channel can be marked `claim` in
`config.classes`, with its own block in `config.claims.<channel>`.

Each row is stamped `payload.claim`. `src/legba/data/claim_hygiene.py` reduces
its URLs to hosts and replaces emails, phone, ID and card numbers and
credentials with placeholders. Names of private people are not redacted; do
not switch on a source whose posts name them.

What a claim row can support is set in `src/legba/data/claim_support.py`:

| Path | Rule |
|---|---|
| Verify floor | A cited claim supports a sentence only if the sentence attributes it to the claimant ("X posted ..."). Otherwise it fails as `claim_as_fact`, soft unless `LEGBA_VERIFY_CLAIM_AS_FACT=hard`. |
| Judge evidence | A claim citation is prefixed `CLAIM ONLY:`. |
| Fact writes | A fact resting only on claims is refused unless its predicate is about the claim. |
| Independence and event counts | A claim never counts as an independent source or toward the two-source bar. |

Links in a claim row are never followed or archived. A claim source cannot
become `active` without `scope.claim.sign_off: {by, at, note}`; the lifecycle
route answers 409 `claim_sign_off_required`. Two example descriptors ship as `draft`:
`source.ransomware_live.claims` and `source.ransomlook.claims`. How to switch
one on is in [operations/claim-sources.md](operations/claim-sources.md); what is
not built yet is in [SEAMS.md](SEAMS.md) #82 and #91.

## Licences and robots

`scope.license_class` takes a value from the `LicenseClass` list in
`src/legba/data/schemas/source.py` (`cc_by`, `cc_nc`, `api_terms`,
`tos_restrictive` and others). Unset means never reviewed; `unknown` means
reviewed and undetermined. Every signal carries the value in its payload.

Hosts reached without a descriptor (a search hit, a followed link) use the
**licence ledger**: the `license_class` column on the admin-curated
`source_credibility` table. It ships empty. Until an admin sets a host's class,
pages from that host land as a snippet and no bytes are archived. An affirmative
class allows full text and archiving; a refusing class means the page is never
fetched. An unrecognised value gets the shallowest depth.

The `scraper` kind fetches each host's `/robots.txt` once, caches it and skips
disallowed URLs. `respect_robots` defaults to `True`, and a failed robots fetch
marks the handler degraded. Feed kinds fetch only the published feed URL.

A feed whose terms allow only non-commercial reuse is `cc_nc`. That limits
reuse and does not restrict reading.

## Access class

`scope.access_class` labels who inside an instance may read what the source
produces. Every signal copies it into its own `access_class` column.

| `access_class` | Meaning | Given to |
|---|---|---|
| `public` | anyone with read access | free, open feeds: news, government, IGO, state media, UCDP |
| `licensed_commercial` | a paid commercial feed | none shipped |
| `licensed_noncommercial` | a feed licensed for non-commercial use | ACLED, OpenSanctions bulk and API |
| `restricted` | sensitive or never classified; the default | generic adapters with no concrete source, such as `firecrawl` or `page_pipeline_default` |
| `internal` | the instance's own staff | none shipped |

`tests/data_pkg/test_access_class_taxonomy.py` pins each committed file's class
and fails when a new `source_*.yaml` has none. The `/signals` and `/findings`
routes accept `access_class_in=<csv>` as an opt-in filter. Nothing else enforces
the label yet; sign-in roles do not read it ([SEAMS.md](SEAMS.md) #59).

## Acquisition behaviour

### Polling

At activation the actor creates a durable Dapr reminder, `poll_<source_id>`,
from `cadence.schedule`. Each fire calls `SourceCore.pull_once`, which passes
the stored cursor to the handler as `since`, then enriches, writes and
publishes each signal. The cursor lives in Postgres (`actor_filter_state`), so
a restart does not skip a window. After an error or an empty poll the cursor
stays put; after entries are read it moves to the last entry's timestamp, so a
backlog past the per-poll cap is read next time. A cursor dated in the future
is discarded. Each poll is bounded in time and entries by the descriptor's
config, else the handler's `poll_budget_seconds` / `max_entries_per_poll`,
else a default.

### Push and on-request fetches

A push source's handler is bound to `/api/v1/webhooks/<source_id>`, and each
POST runs the same enrich, write and publish path as a poll.

An analyst tool, a follow policy, the evidence archiver or the drift check can
ask for a single fetch. Each ask is a row in `ingestion_requests`, naming the
source whose pipeline handles it. `runtime/intake_drain.py` runs it through the
same code a poll uses; a stored copy answers it without a fetch. See
[operations/on-request-fetching.md](operations/on-request-fetching.md).

### Poll outcomes and liveness

Each poll writes one `source_poll_outcomes` row: `success` (wrote a signal or
collapsed a duplicate), `error`, or `empty` (fetched cleanly, nothing new). The
row also records `newest_entry_ts`, the newest entry the feed served. When a
source keeps returning `empty`, the liveness watchdog compares that timestamp
with the last stored signal:

- `honest_quiet`: the feed has published nothing new;
- `cursor_fault`: the feed is serving newer entries than Legba has stored;
- `unknown`: no observation exists.

Stall alerts fire only when this state changes. `registry/source_freshness.py`
also grades each source `ok`, `stale`, `warn`, `empty` (active, never produced)
or `ungraded` (no schedule, or not active) against its cron's longest gap.

### Enrichment

Enrichment runs once per signal, before the write (`data/sources/baseline.py`):

1. Structured: language, normalised tags and a `content_hash`, with no external
   call.
2. Media: `pipeline.media: reference` (the default) keeps a pointer and fetches
   no bytes. `eager` needs a registered extractor; only a text passthrough
   ships, and an eager signal with none is refused.
3. Filters: an ordered chain from `data/filters/`, usually
   `language_detect`, `ner_multilingual`, then `geocode`, with `fact_extractor`
   before `geocode` where bodies carry facts. A filter may drop a signal. These
   stages call the `legba-models` service. Non-Latin text is translated before
   entity extraction.

Two rules guard geo tags. The publisher's country is written to `geo` only when
the content mentions that country. A location from entity extraction counts as
the subject only when it is in the title, so a dateline does not tag the article.
Both apply to new rows; re-geocoding old rows is a separate backfill.

### Dedup

Within one source, a row with the same `content_hash` inside a lookback window
is not inserted again; the existing row's `last_seen_at` advances and the
re-serve is counted. Across sources nothing is deleted. `ingest_dedupe` links a
duplicate to the earliest row by canonical URL or content hash through
`canonical_signal_id` and a `signal_aliases` row. Periodic analysts
(`cross_source_dedup`, `cross_source_coalesce`) find near-duplicates using
embeddings; with no embedding service, `cross_source_coalesce` writes nothing
and says so. A subscription with `canonical_only` receives each event once.

### Fetch receipts and the evidence archive

Every fetched body, pushed envelope and streamed event is stored compressed
and content-addressed as a version per address (`fetch_versions`), and each
record links to the version it came from. When a later fetch differs or the
page is gone, a drift observation is written. Old objects can move to
S3-compatible cold storage ([operations/archive-tiering.md](operations/archive-tiering.md)).

The `evidence_archiver` analyst fetches the full page behind signals cited by
a verified finding, stores it as `cas:sha256/<hex>` and sets the signal's
`retention_class` to `evidence_hold`. It applies the egress guard, per-host
politeness, a size cap, a per-run budget and the licence gate. A bot challenge
page is recorded as `blocked_by_challenge`. Browser TLS fingerprinting
(`LEGBA_FETCH_IMPERSONATE`) is off by default; `_fetch_policy` can enable it per
host as a retry after a 401, 403 or 429.

### Discovery

A template descriptor with a `discovery` block expands into concrete targets or
sources. A discovered source is registered only after its `health_check`
passes and a trial pull produces at least one signal. A new `open` source is offered to every target whose selector matches it. If too many
previously discovered items disappear in one cycle, the discovery pauses.

### Collection requirements

The `collection_gap` analyst writes `collection_requirements` rows for what a
desk needed and lacked, from scorecard cells banded `insufficient-evidence` and
from analysts' source requests. Each row suggests registered sources (any
state) matching by class and geo, or says why none fits. An admin reviews the
rows; marking one `registered` activates nothing.

## Subscribed sources

A subscribed source (`acquisition: subscribe`) holds a connection open to an
upstream that sends records as they happen. It uses the same handlers, sinks,
fetch receipts and `source_poll_outcomes` rows as a poll, one row per window.

```yaml
acquisition: subscribe
subscribe:
  transport: sse                   # the only built transport
  url: https://stream.example.org/v1/stream
  auth: {header: Authorization, secret_ref: source.example.stream_token, value_template: "Bearer {secret}"}
  resume: last_event_id            # or none: every reconnect is a declared gap
  idle_timeout_seconds: 90         # no byte for this long = a stall
  queue_max: 1000                  # the bounded receive queue
  outcome_window_seconds: 300      # one outcome row per window
```

A supervisor in the runtime (`runtime/stream_supervisor.py`), off unless
`LEGBA_SUBSCRIBE_SUPERVISOR` is set, runs one connector per active subscribed
source, held on one replica by a Postgres advisory lease. Server-Sent Events
resumes with `Last-Event-ID` from the last fully written event. When a stream
cannot resume or the queue overflows, a `source_stream_gaps` row records it
(`no_resume`, `overflow` or `shutdown`) with a count of dropped events.

Each window row's `stream` column records `connected`, `quiet`,
`reconnecting`, `stalled`, `refused` or `stopped`, and the watchdog raises
`source_stream_down` after three bad windows. On a connect with no resume
point, entries older than `first_connect_max_entry_age_days` (default 30) are
skipped. A subscribed source must declare `scope.license_class`.

## Source layers

A source's scope says what it covers. A **layer** says how one country's desk
should read it. A target can read its sources as a stack of layers and treat a
change in the gap between two layers as a finding. A layer the desk does not
have is declared absent, so a missing layer is never read as agreement.

| Layer | Meaning |
|---|---|
| `official` | the country's own government or primary-source publisher, or a state outlet read as that state's voice |
| `domestic_press` | independent or commercial press based in and covering the country |
| `foreign_press` | international press and wire copy |
| `social_digest` | channel-level social monitoring; counts and summaries, never identities |
| `public_data` | structured open data, sanctions lists, event datasets |
| `physical` | model-free sensor and hazard feeds |

The list is `LAYER_VOCAB` in `src/legba/data/layers/_vocab.py`. A layer is
separate from `source_class` (set once per source) and from `origin_class` (live,
seed or imported). The same state outlet is `official` for its own country and
is not the `official` layer of any other.

Two tables hold the map (migration 0214):

- `source_layers`: source, country, layer and the reason for the placement.
- `desk_apertures`: per target and layer, `present`, `absent` (reason required)
  or `unmeasured`. A loaded map declares all six layers, so a layer with no row
  is undeclared, which is different from `unmeasured`.

`scripts/gen_layer_map_draft.py --country <cc>` writes a draft map from the
existing source descriptors. Every entry gets `reason: "derived: <rule>"` and
every aperture `unmeasured`; a person sets `present` or `absent`.
`descriptors/layer_map_il.yaml` is a curated example of the file format. A layer
map is its own descriptor kind with no registry lifecycle.

### Loading a curated map

Nothing loads a map automatically:

```
PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \
    python3 scripts/load_layer_map.py descriptors/layer_map_<cc>.yaml --target <target_id>
```

`--target` names the desk the apertures belong to. Reloading an unchanged file
does nothing. Changing content without bumping `map_version` is refused
(`LayerMapVersionConflict`). Bumping it closes the old rows and opens new ones.
The script is the only writer.

### The divergence measure

`deterministic_handlers/layer_divergence.py` reads the two tables. For each
country, over 28 days, it:

1. counts signals per layer per day, folding wire copies within a layer and day;
2. computes `log2((a+0.5)/(b+0.5))` for `official`/`social_digest`,
   `domestic_press`/`foreign_press` and `official`/`public_data` against a
   rolling 14-day median and MAD;
3. fires when `|z| ≥ 2` on two consecutive days with the same sign, as
   `widening` or `narrowing`, citing the top signals per layer.

An `absent` layer contributes no count and the finding gives its reason.
`unmeasured` and undeclared layers are excluded and named; a pair missing
either side is skipped. A run that fires nothing writes only its trace.
`descriptors/analyst_layer_divergence.yaml` runs daily and ships `draft`. Layer
assignments have not been audited, so every finding carries
`CLASSIFICATION_AUDIT_NOTE` ([SEAMS.md](SEAMS.md) #60).

`GET /api/v1/v3/layers/divergence` (`registry/layers_api.py`) returns each
desk's newest receipt and fired divergence (`?target_id=<desk>` for one desk).
The Layer Divergence panel in the console shows it.
