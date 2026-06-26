# Legba baseline schema

`0001_baseline.sql` is a single, round-trip-proven baseline that reproduces the
canonical Legba schema — the same shape you get by applying the full
23-migration reference history (`0001` … `0053`) to a fresh database — in one
file, including a **working Apache AGE graph** and a **pre-seeded migration
ledger**.

It exists to collapse the 23 reference migrations into one fast, deterministic
provisioning step for a clean database. After applying it, the migration runner
(`legba.data.migrate`) sees the 23 historical migrations as already applied and
only runs **future** (`0054`+) migrations.

## What it contains (in order)

1. **Public-schema DDL** — all relational tables, types/enums, sequences,
   indexes, constraints, and functions, taken verbatim from a
   `pg_dump --schema-only` of the canonical reference DB. The `CREATE EXTENSION`
   lines are kept (`plpgsql`, `pgcrypto`, `pg_trgm`, `uuid-ossp`, `age`).
   `plpgsql` — a Postgres default that `pg_dump` omits — is added back
   explicitly.

2. **A correct Apache AGE graph block.** The `pg_dump --schema-only` AGE output
   is unusable: it recreates the per-label tables with
   `DEFAULT ag_catalog._label_id('legba_graph', 'X')` but does **not** dump the
   `ag_catalog.ag_graph` / `ag_catalog.ag_label` catalog **rows** that register
   the graph and its labels — so those tables are orphaned and the `_label_id`
   defaults fail. We discard that entire block and instead build the graph with
   AGE's **own** functions:

   ```sql
   CREATE EXTENSION IF NOT EXISTS age;
   LOAD 'age';
   SET search_path = ag_catalog, "$user", public;
   SELECT create_graph('legba_graph');
   SELECT create_vlabel('legba_graph', '<label>');   -- 10 vertex labels
   SELECT create_elabel('legba_graph', '<label>');   -- 15 edge labels
   ```

   The 10 vertex labels (`Concept`, `Corporation`, `Country`, `Entity`, `Event`,
   `Location`, `Organization`, `Output`, `Person`, `Software`) and 15 edge labels
   (`AffiliatedWith`, `AlliedWith`, `CoOccursWith`, `ConductedVia`, `DerivedFrom`,
   `HostileTo`, `InvolvedIn`, `LeaderOf`, `LocatedIn`, `MemberOf`, `OperatesIn`,
   `PartOf`, `PartyTo`, `SuppliesWeaponsTo`, `Targets`) are exactly those
   registered in the canonical reference DB. With the two internal AGE labels
   (`_ag_label_vertex`, `_ag_label_edge`) this is 27 `ag_label` rows total.

3. **A migration-ledger pre-seed.** A single
   `INSERT INTO public.legba_data_migrations (name, sha256) VALUES …
   ON CONFLICT (name) DO NOTHING;` records the 23 canonical migrations. The
   runner keys idempotency on `name` (the table's primary key); the `sha256`
   values are copied from the reference DB so they match what the runner would
   compute for each file. The result: `python -m legba.data.migrate --dry-run`
   reports **zero** pending against a DB provisioned from this baseline.

## How to apply

```bash
createdb -U legba <db>                      # or: psql -U legba -d postgres -c "CREATE DATABASE <db>;"
psql -U legba -d <db> -v ON_ERROR_STOP=1 -f deploy/baseline/0001_baseline.sql
```

## Verification results

Built from the canonical reference DB and proven against a fresh
`legba_baseline_verify` DB; every gate was re-run from a clean drop/recreate to
prove the file is idempotent:

| Gate | Check | Result |
|------|-------|--------|
| **A** | Apply with `psql -v ON_ERROR_STOP=1` | **PASS** — exit 0, zero errors; ledger `INSERT 0 23` |
| **B** | Diff vs reference DB | **PASS** — all identical |
| | (i) public tables | 49 = 49, identical |
| | (ii) columns (`table.col:type:len:nullable:default`) | 610 = 610, identical |
| | (iii) extensions | identical (`age`, `pg_trgm`, `pgcrypto`, `plpgsql`, `uuid-ossp`) |
| | (iv) AGE labels (`kind,name`) | 27 = 27, identical |
| **C** | AGE cypher smoke (create + match + edge) | **PASS** — `CREATE (n:TestNode)` returns a vertex, `MATCH` reads `"x"` back, `(:Country)-[:AlliedWith]->(:Country)` edge created |
| **D** | `migrate --dry-run` reports pending | **PASS** — `primary: []` (0 pending) |

Diffs used `LC_ALL=C sort` to avoid false ordering differences. A negative
control (dry-run against an un-seeded DB) correctly listed all 23 migrations as
"would apply", and the digests it computed matched the pre-seeded `sha256`
values — confirming the ledger is consistent, not merely name-matched.

## Known caveats

- **Lazy AGE labels.** This baseline registers the labels present in the
  canonical reference. AGE also creates labels lazily on first use, so the
  runtime may add a handful of extra edge/vertex labels (and the cypher smoke
  test above creates a `TestNode` vertex label) the first time a new
  relationship type is written. That is expected AGE behavior and does not
  affect the relational schema or the labels this baseline guarantees.
- **Runtime-owned tables.** Three tables — `actor_state`, `actor_filter_state`,
  and `legba_jobs` — are created by the runtime at boot, not by this baseline,
  and so are intentionally absent from the schema-only reference and from this
  file.
