# legba.data — Legba substrate package (L-001)

The data foundation for the Legba descriptor/registry/runtime model.

**Phase 1 task L-001.** Consumes the L-090 / L-091 / L-101 / L-107 design
decisions; provides everything L-110 (descriptor registry CRUD), L-111
(stack registry CRUD), L-002 (analyst runtime), and the source/filter/output
handler kinds build on.

## Layout

```
src/legba/data/
  __init__.py
  config.py                 # env-driven config per store
  postgres.py               # asyncpg pool + AGE Cypher helper
  qdrant.py                 # only legba_signals survives
  redis.py                  # async redis with TTL on embed caches
  nats.py                   # nats + JetStream helper
  provenance.py             # universal provenance + lineage + receipts
  vocabulary.py             # trimmed AGE vocabulary (9 vertex + 14 edge)
  migrate.py                # SQL migration runner
  smoke.py                  # end-to-end sanity test
  migrations/
    0001_extensions_and_migration_ledger.sql
    0002_core_substrate.sql
    0003_facts_attribute_half.sql
    0004_age_setup.sql
    0005_runtime_tables.sql
    0006_descriptor_registry.sql
    0007_stack_registry.sql
    0008_conversion_webhooks.sql
    0009_dead_letter_and_audit.sql
    0010_seed_vocabulary.sql
  schemas/                  # vendored L-101 pydantic schemas
    __init__.py
    properties.py
    lifecycle.py
    versioning.py
    vocabulary.py
    target.py
    analyst.py
    stack.py
```

## What's in scope

**Schemas (per L-090):**
- 18 core substrate tables (signals, events, entity_profiles, …) with universal
  provenance columns (`target_id`, `target_version`, `analyst_id`,
  `analyst_version`, `produced_at`, `derived_from`, `schema_uri`, `run_id`).
- 5 zero-row tables retired (`notifications`, `operator_corrections`,
  `modifications`, `signals_staging`, `situation_signals`).
- Dead columns dropped (`signals.provenance`, `signals.confidence_components`,
  `events.parent_event_id`, `events.velocity_change`,
  `events.confidence_components`, `facts.confidence_components`,
  `facts.contradiction_of`, `facts.superseded_by`).
- 4 new runtime tables: `analyst_traces`, `analyst_critiques`,
  `budget_ledger`, `graph_metrics`.
- Descriptor registry: `target_descriptors`, `analyst_descriptors`,
  `wiring_descriptors`, `vocabulary_entries`.
- Stack registry: `stack_components` (with `kind` discriminator).
- Conversion webhooks: `conversion_webhooks`.
- Dead-letter + audit: `descriptor_dead_letter`, `output_dead_letter`,
  `descriptor_audit_log`, `audit_checkpoints`.

**AGE graph (per L-090 §4.5):**
- 9 retained vertex labels (entity_class taxonomy).
- 14 retained edge labels (relationship_type taxonomy with `Targets`
  promoted to canonical per DM-5).
- Runtime-extensible via `vocabulary_entries` (L-101 §8 VocabularyRegistry).
- Legacy `UPPER_SNAKE` → `PascalCase` normalization via `aliases`.
- `Nexus` collapses to a property on the underlying canonical edge.

**Storage (per L-091):**
- Qdrant — only `legba_signals` survives (1024-dim, BGE-M3); 3 dormant
  collections retired.
- Redis — `maxmemory-policy=allkeys-lru`, TTL on embed caches.

**Provenance (per L-107):**
- Native relational columns; not JSONB.
- GIN index on `derived_from UUID[]` per L-107 §1.
- Partial indexes on `target_id` / `analyst_id` (excludes back-tagged
  legacy rows tagged `pre-descriptor.legacy`).
- Iglu URI parser + builder (`iglu:legba/<entity>/jsonschema/<M-m-p>` for
  substrate rows, `legba/<family>/<M.m.p>` for descriptors).
- Receipt-hash helper (canonical-JSON + SHA-256 + `prev_receipt_hash` chain).

**Vendored schemas (per L-101):**
- 13 property factories with the `Property.*` aliased namespace.
- Lifecycle state machine with `_legal_transition` validator.
- Target / Analyst / Stack descriptor models.
- `content_hash(descriptor)` and `ConversionWebhook` model.
- `VocabularyEntry` + `VocabularyRegistry` for runtime extensibility.

## What's deferred to follow-ups

- **Registry CRUD + validation + NATS events** — L-110 / L-111. Schemas live
  here; the CRUD/events layer they enforce against does not.
- **Conversion-webhook walk + apply** — L-112. The table + the model are here.
- **Per-target Qdrant collection lifecycle hooks** — wired into runtime
  reconcile loop in L-114.
- **Starlark predicate compilation** — L-104.

## Bootstrap env vars

The new env vars use `LEGBA_DATA_*` prefixes so they coexist cleanly with the
legacy `POSTGRES_*` / `QDRANT_*` / etc. names. `from_env()` falls back to the
legacy names when the new ones aren't set, so existing deployments don't need
to change anything.

See `.env.example` in the repo root for the canonical list. The minimum
required to bootstrap the descriptor + stack registries (per topology §2.5):

```
LEGBA_DATA_REGISTRY_DSN=postgresql://legba:legba@postgres:5432/legba
LEGBA_DATA_MASTER_KEY=<base64-32-bytes>
LEGBA_DATA_DEFAULT_PG=pg.cluster_main
LEGBA_DATA_DEFAULT_REDIS=kv.redis.cluster_main
LEGBA_DATA_DEFAULT_NATS=bus.nats.cluster_main
LEGBA_DATA_DEFAULT_VECTOR=vector.qdrant.cluster_main
LEGBA_DATA_DEFAULT_EMBEDDING=embed.primary.openai_compat
```

## Running migrations

```
python -m legba.data.migrate                   # apply the primary chain
python -m legba.data.migrate --dry-run         # discover but don't apply
```

## Running the smoke test

```
python -m legba.data.smoke
python -m legba.data.smoke --json
```

Smoke exit code is 0 iff: every expected table exists with provenance
columns, no retired table is present, all 9 vertex + 14 edge AGE labels are
loaded, the sample target / stack / trace inserts round-trip, and the
provenance-tagged signal write read back via the analyst context.

## Tests

```
pytest tests/data_pkg/ -v
```

Tests assume the Legba data containers are up (`docker compose up -d redis
postgres qdrant nats`). The conftest
fixture brings them up if they're not, and tears nothing down — the
containers are kept available for ongoing development.

## Migration policy

CREATE-only per Lewis 2026-05-15 clean-restart decision (substrate clean
restart, no migration of legacy data). The runner records each applied
migration in `legba_data_migrations(name, sha256, …)` and skips applied
files on re-run. Manual schema edits should land as new migration files,
never amendments to applied ones.

## Non-goals

- L-205 retired the legacy cycle path (`src/legba/agent/`,
  `src/legba/subconscious/`, `src/legba/maintenance/`, `src/legba/ingestion/`,
  `src/legba/supervisor/`, `src/legba/airflow/`, `legba-ui/`). Git history
  preserves them.
- Migrating legacy data (Lewis's clean-restart decision).
