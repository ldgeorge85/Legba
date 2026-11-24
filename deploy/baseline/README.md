# Baseline schema

`0001_baseline.sql` creates the schema of the first 23 migrations (`0001` … `0053`) in one file
and pre-seeds the migration ledger with those 23 rows. `deploy/deploy.sh` applies it to an empty
database, then `python -m legba.data.migrate` applies every later migration (the tree has 185
migration files in `src/legba/data/migrations/`). `migrate --dry-run` against a freshly applied
baseline lists only the later ones.

## What it contains

1. **Public-schema DDL**: tables, types, sequences, indexes, constraints and functions, from a
   schema-only dump of a database built from those migrations. The extensions are created with
   `IF NOT EXISTS`: `plpgsql`, `pgcrypto`, `pg_trgm`, `uuid-ossp`, `age`.
2. **An Apache AGE graph** (`legba_graph`) with 10 vertex and 15 edge labels, built with AGE's own
   functions (`create_graph`, `create_vlabel`, `create_elabel`). A schema-only dump of AGE is not
   usable, because it omits the catalog rows that register the graph. The dumped `ag_catalog` DDL
   is stripped and `CREATE EXTENSION IF NOT EXISTS age;` has no `WITH SCHEMA` clause, so the file
   applies both on images that pre-install AGE and on ones that do not. Nothing uses the graph: the
   later migration `20261115T1829_drop_empty_age_graph.sql` drops it. The Postgres image still
   carries the extension.
3. **The ledger pre-seed**: one `INSERT INTO public.legba_data_migrations (name, sha256) … ON
   CONFLICT (name) DO NOTHING` with the 23 names and the digests the runner computes for them.

Three tables (`actor_state`, `actor_filter_state`, `legba_jobs`) are created by the runtime at boot
and are not in this file.

## Applying it by hand

```bash
createdb -U legba <db>
psql -U legba -d <db> -v ON_ERROR_STOP=1 -f deploy/baseline/0001_baseline.sql
python -m legba.data.migrate          # with LEGBA_DATA_PG_* pointing at <db>
```

## Checking it

After applying the file to an empty database, `psql` exits 0 and the ledger insert reports 23
rows. Before the ledger is seeded, `migrate --dry-run` lists those 23 as pending and computes the
same digests as the pre-seed, which shows the ledger matches the files and not only their names.
