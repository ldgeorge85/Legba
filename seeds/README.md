# Seeds — the knowledge-base seeding layer

Legba ships the seed **machinery** but **no curated seed data**. The adapters,
the driver, and the CLI are all in-repo; the operator-curated YAML datasets that
were previously bundled here are **not part of the public project** and are no
longer tracked (per operator, 2026-07-09). A fresh instance therefore boots with
an **empty knowledge base** unless you provide your own seed files in this
directory.

To seed real starting knowledge you supply your own files here — start from the
format examples, which are the only data files this directory ships:
[`world_baseline.example.yaml`](world_baseline.example.yaml),
[`source_ratings.example.yaml`](source_ratings.example.yaml) and
[`exemplar_shelf.example.yaml`](exemplar_shelf.example.yaml).

## What a seed does

A seed adapter writes to the **knowledge layer** (`facts` / `nexuses` /
`entity_profiles`) — as opposed to a streaming `source` handler, which writes
raw `signals`. Seed rows are temporally honest: every fact/nexus carries a real
`valid_from` (and an optional `valid_until`), and re-importing is idempotent (the
driver upserts — no duplicate open triples on a re-run).

## The adapters (the machinery, all shipped)

Registered in `src/legba/data/seed/__init__.py` (`ADAPTERS`); list them live with
`python scripts/seed.py --list`:

| adapter | source_type | input | needs |
| --- | --- | --- | --- |
| `world_baseline` | seed | `seeds/world_baseline.yaml` (curated) | nothing (offline) |
| `sipri_arms_transfers` | seed | `seeds/sipri_arms_transfers.yaml` (curated) | nothing (offline) |
| `wikidata_leaders` | seed | Wikidata SPARQL | network |
| `acled_conflict` | backfill | ACLED API | network + creds |

Supporting code, also shipped:

- `src/legba/data/seed/_base.py` — the typed payloads (`SeedEntity`,
  `SeedFact`, `SeedNexus`), the `SeedSource` protocol, and `SeedContext`.
- `src/legba/data/seed/_driver.py` — resolves entities, writes, and records the
  `seed_batches` ledger row.
- `src/legba/data/seed/manual_batch.py` + `manual_schema.py` — the
  manual-ingest path (hand-authored batches of facts/nexuses). See
  [`../docs/MANUAL_INGEST_FORMAT.md`](../docs/MANUAL_INGEST_FORMAT.md) for that
  format.
- `scripts/seed.py` — the CLI (`--list`, `--source`, `--dry-run`).

The two offline curated adapters (`world_baseline`, `sipri_arms_transfers`)
**gracefully degrade**: if their YAML file is absent, `fetch()` logs a WARNING
and returns nothing (0 rows) instead of crashing — so a fresh public `--seed`
run is a clean no-op.

## The `world_baseline` YAML format

The full, accurate format (with inline field comments) is in
[`world_baseline.example.yaml`](world_baseline.example.yaml). In brief, three
optional top-level keys, mapped by
`src/legba/data/seed/adapters/world_baseline.py`:

- **`leaders:`** — each row (`leader`, `country`, `valid_from`; optional
  `office`, `valid_until`, `confidence`) becomes a `LeaderOf` fact
  (subject=leader) **plus** a country-subject office fact (subject=country,
  predicate=`office` defaulting to `head of state`, value=leader). The
  country-subject fact is the supersession-correct shape the grounding
  injection reads: a new office-holder closes the prior row; distinct offices
  (head of state vs head of government) coexist.
- **`alliances:`** — each bloc (`bloc`, optional `rel_type` default `MemberOf`,
  optional `polarity` default `+1`) with a `members:` list (each `country` +
  `valid_from`, optional `valid_until`/`confidence`) → one signed `MemberOf`
  nexus per member (subject=country, object=bloc, +1 supportive).
- **`conflicts:`** — the current active-conflict layer. Preferred shape: `sides`
  groups co-belligerents into coalitions → a signed `-1 InActiveConflictWith`
  nexus for each ordered pair **across** sides, and a signed `+1 AlliedWith`
  nexus for each ordered pair **within** a multi-member side. A legacy flat
  `belligerents:` list (all-vs-all hostile, no alliances) is also accepted.
  `valid_from` is the conflict onset; set `valid_until` when it ends.

The `sipri_arms_transfers` adapter reads a `transfers:` list (each `supplier`,
`recipient`, `valid_from`; optional `rel_type`/`polarity`/`confidence`/`tiv_rank`)
→ one signed `+1 ArmsTransferTo` nexus per supplier→recipient relationship. See
its adapter docstring for the polarity convention.

## How to import

Provide your file(s) in this directory, then:

```bash
# List available adapters.
python scripts/seed.py --list

# Dry-run (parse + map, no DB writes).
python scripts/seed.py --source world_baseline --dry-run

# Import for real.
python scripts/seed.py --source world_baseline
python scripts/seed.py --source sipri_arms_transfers
```

Or as part of a full stack deploy:

```bash
deploy/deploy.sh --seed
```

`--seed` runs the curated-YAML adapters (`world_baseline`,
`sipri_arms_transfers`) before the runtime boots. Because the adapters degrade
gracefully, `--seed` is a **no-op** if you have not provided the seed files —
the instance simply boots with an empty knowledge base.

## Source ratings (assurance ledger catalog seed)

`seeds/source_ratings.yaml` (curated, gitignored) feeds the **source assurance
ledger** (`source_ratings`, migration 0094) rather than the knowledge layer:
per-source rubric grades with the Admiralty display vocabulary, upserted as
`method='catalog_seed'`, `visibility_class='public'` rows with supersession
history. It has its own loader (not a `SeedSource` adapter — it writes ratings,
not facts):

```bash
# Dry-run (parse + validate, no DB writes).
python scripts/seed_source_ratings.py --dry-run

# Import for real.
python scripts/seed_source_ratings.py
```

Format (with inline field comments):
[`source_ratings.example.yaml`](source_ratings.example.yaml) — the example
sources in it are FAKE. Same graceful degrade: a missing file is a warn +
no-op. Ratings are display/weighting metadata only — they never touch the
faithfulness score.

## Exemplar shelf (curated pattern-thread index)

`seeds/exemplar_shelf.yaml` (curated, gitignored) is the **exemplar shelf** —
the curated index of pattern-threads. A pattern-thread is a *warning problem*
(UK JDP 2-00): a named standing monitor with a watch condition that emits on
critical-indicator change rather than on cadence. The shelf says which warning
problems exist, at what stage, and how ids that have left it still resolve.

It writes **nothing** to the substrate — no facts, no nexuses, no ratings — so
it is not a `SeedSource` adapter and there is no `scripts/seed_*.py` for it. It
is read in-process:

```python
from legba.data.seed.exemplar_shelf import load_shelf

shelf = load_shelf()          # defaults to seeds/exemplar_shelf.yaml
shelf.offered_ids()           # the active shelf, and nothing else
shelf.resolve("some_old_id")  # merged / retired / active / None
```

Format (with inline field comments):
[`exemplar_shelf.example.yaml`](exemplar_shelf.example.yaml) — the example
patterns in it are FAKE. Same graceful degrade: a missing file is a warn plus
an empty shelf that offers nothing (it never invents patterns). A file that is
*present but malformed* raises `ExemplarShelfError` instead of half-loading.

**The one rule the loader exists to enforce:** a pattern removed from the
active shelf must stay **resolvable** and must **never be offered**. Curation
removes patterns two ways — *merged* (the id joins the survivor's `aliases` and
its `merged_from`, normally with a `variants` entry carrying the framing and
indicators it contributed) and *retired* (the id moves to `retired:` with the
deciding document's reason quoted). `slot` numbers are never renumbered, so a
cull leaves holes on purpose: an old reference to a cut slot cannot silently
become a different pattern.
