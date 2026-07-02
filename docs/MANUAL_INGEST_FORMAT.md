<!-- SPDX-FileCopyrightText: 2026 Lewis George -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Manual ingest — batch format

*The on-disk FORMAT for hand-supplying data to the knowledge layer: a directory
with a manifest and per-kind JSONL files. This describes the shape and the
validation contract. The loader that consumes a validated batch lives in
[`src/legba/data/seed/manual_batch.py`](../src/legba/data/seed/manual_batch.py)
(`run_manual_batch`, the skip/merge/force reconciliation) and is driven by the
[`scripts/manual_ingest.py`](../scripts/manual_ingest.py) CLI (`--batch DIR
[--mode skip|merge|force] [--dry-run]`), run in the registry container like
`migrate`/`seed`.*

Manual ingestion rides the existing seed plane (`src/legba/data/seed/`): a batch
is validated, then written through the same temporal write path
(`write_fact` / `write_nexus`) with supersession semantics and a `seed_batches`
ledger row. It is **not** a new subsystem — it is a defined file format plus a
generic loader over it.

The format and its validation live in
[`src/legba/data/seed/manual_schema.py`](../src/legba/data/seed/manual_schema.py)
(pydantic models). Validation reports **per-line** errors (file + 1-indexed line
+ reason) — one bad record in a large batch names its own line rather than
failing the whole batch opaquely.

## Directory layout

A batch is one directory:

```
mybatch/
  batch_manifest.yaml   # the manifest (identity + defaults) — required
  facts.jsonl           # one fact record per line
  entities.jsonl        # one entity record per line
  nexuses.jsonl         # one relationship record per line
  signals.jsonl         # one backfilled observation per line
  docs.jsonl            # one vector-corpus chunk's metadata per line
  docs/                 # (optional) source docs referenced by docs.jsonl
```

Only the lanes named in the manifest's `files:` block are processed; any subset
is valid, but a batch must declare **at least one** lane.

Each `*.jsonl` file is [JSON Lines](https://jsonlines.org/): one JSON object per
line. Blank lines are ignored (and do not shift line numbering in error
reports).

## The manifest — `batch_manifest.yaml`

```yaml
schema_version: "1"                # must match the build's supported version
batch_id: "iiss-milbal-2026"       # stable id for this batch (ledger key)
operator: "your-handle"            # who assembled it
created_at: 2026-07-02T00:00:00Z   # ISO-8601
description: "IISS Military Balance officeholders + orders of battle."

default_provenance: manual         # curated | manual   (default: manual)
mode: skip                         # skip | merge | force  (default: skip)
default_confidence: 0.85           # batch-wide fallback (0.0–1.0), optional

# Provenance / licensing defaults every record inherits unless it overrides.
license: "CC-BY-4.0"
source_url: "https://example.org/report"
provenance_notes: "extracted from the 2026 print edition"

files:                             # per-kind file references (≥1 required)
  facts: facts.jsonl
  entities: entities.jsonl
  nexuses: nexuses.jsonl
  signals: signals.jsonl
  docs: docs.jsonl
```

Unknown top-level keys are rejected (fail-loud against typos). A manifest whose
`schema_version` does not match the build, or that declares no lane, is refused
before any record is read.

### Provenance tier — `default_provenance`

Load-bearing. Two values:

| tier | meaning | grounding-eligible |
|---|---|---|
| `curated` | authoritative, vetted; may feed the analysis grounding preamble | **yes** |
| `manual`  | stored, but **not** injected as a trusted prior | no |

The default is the safe `manual`: a batch must ask, explicitly, to be treated as
grounding-eligible. This mirrors the platform's Tier-1 provenance gate, which
only injects `seed`/`curated` context into the authoritative preamble — loading
data as `manual` records it without making it a trusted prior.

### Modes — `mode`

How the loader reconciles a record against existing rows by its natural key. No
mode ever hard-deletes; history is preserved via the temporal
`valid_until` / `superseded_by` close.

| mode | behaviour |
|---|---|
| `skip`  | insert-if-absent by natural key; a re-run is a no-op (default) |
| `merge` | fill empty fields; on a value change, write the new row and supersede the old |
| `force` | supersede every matching row unconditionally; the batch is the authority |

### Confidence policy — no silent `1.0`

For the **asserting lanes** (facts, nexuses) confidence must be supplied
honestly, either:

- per record (`"confidence": 0.9` on the line), **or**
- via the manifest's `default_confidence`.

If a fact/nexus record carries no confidence **and** the manifest sets no
`default_confidence`, the record is **refused** with a per-line error — the
loader never fabricates a `1.0`. Entities, signals, and docs are not assertions
and carry no confidence (signals carry `source_credibility` instead).

## Per-kind record schemas + natural keys

Field names mirror the internal typed payloads, so a validated record maps
one-to-one onto a write. The **natural key** is what idempotency and merge
targeting key on.

### `facts.jsonl`

```json
{"subject": "Iran", "predicate": "head of state", "value": "Mojtaba Khamenei", "valid_from": "2026-03-08", "confidence": 0.95}
```

| field | required | notes |
|---|---|---|
| `subject`, `predicate`, `value` | yes | the `(subject, predicate, value)` triple |
| `valid_from` | yes | ISO date/datetime — facts are temporally honest |
| `valid_until` | no | close time; omit for an open-ended fact |
| `confidence` | see policy | 0.0–1.0; per-record or batch default |
| `geo_lat`, `geo_lon` | no | optional point geo |
| `data` | no | free-form extras bag |

**Natural key:** `(subject, predicate, valid_from)`.

### `entities.jsonl`

```json
{"canonical_name": "Iran", "entity_class": "country", "geo_country": "IR"}
```

| field | required | notes |
|---|---|---|
| `canonical_name` | yes | resolved through the shared entity-canon normalizer at load |
| `entity_class` | no | `country` / `person` / `organization` / … (default `entity`) |
| `geo_lat`, `geo_lon`, `geo_country` | no | optional geo |
| `data` | no | extras bag |

**Natural key:** `canonical_name`. Emit an entity record only to enrich an
entity with a class/geo the facts alone would not carry — the loader
auto-resolves every fact/nexus endpoint regardless.

### `nexuses.jsonl`

```json
{"subject": "Iran", "object": "United States", "rel_type": "in active conflict with", "polarity": -1, "valid_from": "2026-02-28", "intent": "conflict", "confidence": 0.9}
```

| field | required | notes |
|---|---|---|
| `subject`, `object`, `rel_type` | yes | the reified relationship |
| `polarity` | yes | structural-balance sign: `+1` / `0` / `-1` |
| `valid_from` | yes | ISO date/datetime |
| `valid_until` | no | close time |
| `intermediary` | no | for an indirect A→[X]→B relationship |
| `label`, `intent`, `channel` | no | `channel` default `direct` |
| `confidence` | see policy | per-record or batch default |
| `data` | no | extras bag |

**Natural key:** `(subject, rel_type, object, valid_from)`.

### `signals.jsonl`

Backfilled events / articles / reports. These enter through the normal signal
contract (enrichment, fan-out, dedupe all apply). `published_at` is the real
event time (backdated); the loader stamps the load time and marks the row as a
backfill so time-windowed analysts can exclude it while the fact/accumulation
paths still benefit.

```json
{"external_id": "rep-001", "title": "…", "body": "…", "published_at": "2026-02-28T18:30:00Z", "geo": ["IR"], "entities": ["Iran"], "tags": ["conflict"], "source_credibility": 0.8}
```

| field | required | notes |
|---|---|---|
| `published_at` | yes | the real event time |
| `external_id` | no | stable source id (the natural key when present) |
| `title`, `body`, `canonical_url` | no | content |
| `modality`, `language` | no | `modality` default `text` |
| `geo`, `entities`, `tags` | no | inline enrichment lets the loader skip NER |
| `source_credibility` | no | 0.0–1.0 source property (not `confidence`) |
| `data` | no | extras bag |

**Natural key:** `external_id` when supplied; otherwise the pipeline's existing
content-hash dedupe keys the row.

### `docs.jsonl`

Vector-corpus chunk metadata (destined for the retrieval store). The chunk text
is inline (`text`) or referenced (`text_ref`, a path under the batch's `docs/`
dir).

```json
{"corpus": "world_context", "doc_id": "iran-brief", "chunk_seq": 0, "title": "Iran country brief", "section": "overview", "text": "…", "countries": ["Iran"], "topics": ["background"], "lang": "en", "license": "CC0-1.0", "effective_date": "2026-01-01"}
```

| field | required | notes |
|---|---|---|
| `corpus`, `doc_id` | yes | corpus name + document id |
| `chunk_seq` | no | 0-based chunk index (default 0) |
| `text` **or** `text_ref` | no | inline chunk text, or a path under `docs/` |
| `title`, `section` | no | heading context |
| `countries`, `topics`, `lang` | no | retrieval filters |
| `license`, `source_url`, `effective_date` | no | provenance |
| `data` | no | extras bag |

**Natural key:** `(corpus, doc_id, chunk_seq)`.

## Validation

`legba.data.seed.validate_batch(batch_dir, *, strict=False)` reads the manifest,
then every declared lane line-by-line, and returns a `ValidatedBatch` carrying
the typed records grouped by lane plus **every** per-line error (validation does
not stop at the first bad record). Each error names its file, 1-indexed line,
and a compact reason, e.g.:

```
facts.jsonl:5 [facts] confidence absent and no batch default_confidence (refusing a silent 1.0)
nexuses.jsonl:1 [nexuses] polarity: Input should be less than or equal to 1
```

`strict=True` raises `BatchValidationError` (which carries the same per-line
error list) instead of returning a result with errors. A malformed manifest is
fail-loud and raises before any lane is walked.

## Lane 4 — the vector-corpus loader (`docs.jsonl` → Qdrant)

The structured lanes (facts/entities/nexuses/signals) write to Postgres. The
`docs` lane is different: it is **chunked, embedded, and upserted into Qdrant**
by `legba.data.rag.load_vector_batch` (CLI: `scripts/manual_ingest_vectors.py`),
riding the same `seed_batches` ledger for idempotency.

**Collections.** Only two RAG corpora are provisioned; a `docs` record's
`corpus` must be one of them (an unknown corpus is refused, never silently
created):

| `corpus` | Qdrant collection | contents |
|---|---|---|
| `world_context` | `world_context` | country/topic priors — briefs, doctrine summaries keyed to places/actors |
| `tradecraft`    | `tradecraft`    | how-to-analyze corpus — analytic standards, SAT handbooks |

Both are 1024-dim cosine (bge-m3), same as `legba_signals`.

**Chunking.** Each record's text (inline `text` or the file at `text_ref`) is
run through a heading-aware chunker (`legba.data.rag.chunk_text`, ~400-800
tokens, small overlap). A record that already fits the token band becomes one
chunk (`chunk_part = 0`); a longer one splits into `chunk_part = 0..N`. The
stored **natural key** is therefore `(corpus, doc_id, chunk_seq, chunk_part)` —
the design's `(corpus, doc_id, chunk_seq)` plus the sub-index. Point ids are a
deterministic `uuid5` of that key, so a re-upsert overwrites in place.

**Modes.** `skip` (and `merge`) are no-ops on an identical re-run (deduped on
the ledger's manifest `content_hash`, so no re-embed). `force` = **delete the
doc's existing chunks and re-embed** (see below).

### ⚠ DELETE-EXCEPTION — `--mode=force` on the vector lane

The platform rule is *no hard deletes* — structured substrate (Lanes 1-3) is
only ever superseded (`valid_until` / `superseded_by`), never `DELETE`d. **Lane 4
is the one documented exception.** `force` deletes every stored chunk of each
`(corpus, doc_id)` in the batch, then re-embeds from source. This is safe here,
and only here, because **vector rows are DERIVED, re-embeddable artifacts** — a
chunk can always be rebuilt from its source document, so deleting it loses no
authored knowledge (unlike a fact/nexus, whose supersession history is the
record). Use `force` to re-chunk a document after its source text, chunker
params, or embedding model change. The delete is scoped by a payload filter on
`corpus` + `doc_id`, so it never touches another document's or another corpus's
chunks.
