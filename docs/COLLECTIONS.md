<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Collections

For an admin who loads historical data into Legba, and for anyone reading answers that cite it.

A **collection** is a bounded, versioned holding of past data: a manifest naming which series or documents, for which
subjects, over which years, from which provider, under which licence. An admin loads it once. It is never scheduled,
and it is kept away from every part of the system that treats incoming data as current.

## Collections and sources

A **source** is a live feed. It is polled on a cadence, its silence is a health signal, its arrivals can wake reactive
triggers, and its rows are stamped `origin_class='live'`. Freshness, source health, calibration, salience and alerts
all treat live rows as the present.

A collection has no cadence, no cooldown and no health. It is fetched once and written directly to the database.
Loaded into the live data, ten years of history would look like a huge surge of new reporting and like publishers
returning after years of silence. So every collection carries a **firewall** block naming the surfaces it is excluded
from, and a manifest that omits any of them does not load:

```yaml
firewall:
  readers_opt_in: [consult, research, claim_watch, program6_baselines, replay]
  excluded_from:
    [cadence_analysts, freshness, source_health, calibration, salience, alerts,
     reactive_triggers, surge_detection]
```

`readers_opt_in` takes values from `OPT_IN_READERS` in `src/legba/data/schemas/collection.py`. The origin-class
vocabulary behind the firewall is in `src/legba/data/provenance/origin.py`.

- Series rows land in `observations`. Document rows would land in `signals` with a history `origin_class` and a
  `collection_id`; that half is not built ([SEAMS #62](SEAMS.md)).
- Files are named `descriptors/collection_<id>.yaml`, never `source_<id>.yaml`. The source machinery globs
  `source_*.yaml` and would treat a misnamed collection as a live feed.
- Lifecycle: `draft` → `reviewed` → `loaded` → `superseded`. A collection version is the hash of its manifest.

## Tables (migration 0220)

| table | holds |
|---|---|
| `collection_descriptors` | collection descriptors, with `origin_shape`, `origin_class`, `licence_class` and `collection_version` as columns |
| `observations` | series rows, bitemporal, range-partitioned by `valid_from` (one partition per year 2016–2027 plus a default) |
| `entity_aliases` | provider names mapped to `entity_profiles`, per collection; read by the loader and collection readers, never by entity resolution |
| `collection_loads` | one row per collection and manifest version: resume key, counts, timestamps, status |

In `observations`, `valid_from`/`valid_to` is the period a number is about and `record_time` is when the provider
published or revised it. A figure revised years later is two rows.

- A revision is a second row. `record_time` is part of the identity key
  `(collection_id, series_id, subject, valid_from, valid_to, record_time)`, so a provider that restates history cannot
  replace what was known before. An as-of reader picks rows with `record_time <= D`.
- The same key makes loading idempotent: loading a version twice conflicts on every row and writes nothing new.
- There is no row for a year the provider does not publish. A `CHECK` requires exactly one of `value` or
  `value_text`.

Since migration 0246 the table also holds live series rows written by a source's observation sink. A `CHECK` keeps
the two apart: a history row (`archive` or `backfill_*`) has no `source_id`; a live row must have its `source_id`, a
`collection_id` of `live:<source_id>`, and the hash of the response it was parsed from. A collection cannot write a
live row, and collection readers read only rows whose collection head is `loaded`.

## Loading a collection

```bash
python3 scripts/load_collection.py descriptors/collection_series_pilot_2016_2026.yaml --dry-run
python3 scripts/load_collection.py collection.series_pilot_2016_2026
python3 scripts/load_collection.py collection.series_pilot_2016_2026 --resume
```

Run it once per collection version. It takes a path or an `identity.id`, validates the manifest against
`legba/collection/1.0.0` (licence class, the eight-surface firewall, a history-only origin class), and reads the
fetch plan with the same parser as `scripts/verify_collection_manifest.py`, so the loader and the verifier agree on
what a provider holds.

The loader writes rows directly: no NATS publish, no ingest pipeline, no enrichment, no analyst run.
`test_collection_loader.py` checks its import graph (it must not reach `legba.runtime`, `legba.data.filters`,
`legba.data.sources`, `legba.data.analysts`, `legba.data.outputs`, `legba.data.alerts` or a NATS client) and its SQL
(it may write only `observations` and `collection_loads`).

Every row carries `collection_id`, the history `origin_class`, the `source_url`, the `sha256` of the file the number
was read from, and a `provenance` block with the provider revision, row offset, manifest version and licence class.

`--resume` restarts after the last `series_id:subject` pair whose rows were committed. Any pair not fully on disk is
fetched again; the identity key makes the repeat writes no-ops.

The loader refuses:

- a `documents_*` loader kind (SEAMS #62), before any fetch;
- a cadence it cannot build a period from (only `annual` is supported today);
- a pair whose provider published no revision stamp. The pair is skipped and the reason recorded in
  `collection_loads`; the loader never stamps the load time as `record_time`.

`--dry-run` fetches and reports what it would write and does not touch the database. Without `--dry-run`, a missing
database connection is an error.

## How the firewall is tested

1. **The descriptor.** `firewall.excluded_from` must name exactly the eight surfaces. A manifest missing one fails
   validation and the registry returns 422 (one test per surface).
2. **The loader.** Its import graph and SQL are checked as above.
3. **A load changes nothing else.** `test_collection_loader.py` snapshots every table the eight surfaces write, plants
   a live sentinel signal, loads the pilot's recorded pairs, and asserts every count and the sentinel's `salience` and
   `updated_at` are unchanged.
4. **The readers.** Every reader on the eight surfaces filters on origin class using `data/provenance/origin.py`, and
   the reactive trigger plane checks each delivered row (`coalescer.is_live_origin`).
   `tests/data_pkg/test_origin_firewall_surfaces.py` plants a 40-row history burst beside 3 live rows in one geo and
   checks that the desk baseline, the alert edge detector, the geo convergence scan and the salience batch all see 3.
   The same query without the filter sees 43.

## The pilot collection

`descriptors/collection_series_pilot_2016_2026.yaml` ships as an example: eleven annual series for four subjects
over 2016–2025, from two free, keyless, publicly licensed providers.

| | |
|---|---|
| Subjects | `country_g20_us`, `country_watch_il`, `country_watch_ir`, `country_watch_ua` |
| Window | 2016-01-01 to 2025-12-31, annual |
| `origin_shape` / `origin_class` | `archive_only` / `archive` |
| `licence_class` | `public` (World Bank CC BY 4.0, EIA U.S. public domain) |
| Loader | `series_api`, priority `low` |
| State | `draft`; it loads only after an admin moves it to `reviewed` |

**World Bank** (World Development Indicators, `api.worldbank.org/v2`): GDP growth, consumer-price inflation, military
expenditure % of GDP, external debt stocks, energy use per capita, FDI net inflows % of GDP.

**EIA** (International Energy Data, dataset `INTL`, bulk route): crude oil production, refinery processing gain,
refined-products consumption, crude oil exports, crude oil imports.

The EIA v2 REST API (`api.eia.gov/v2/...`) needs a free `api_key`; the bulk download
`https://api.eia.gov/bulk/INTL.zip` does not, and this collection uses the bulk route. A collection that needs the v2
route would store its key in the vault as `eia.api_key`.

The manifest records gaps as declared nulls with a reason:

- World Bank external debt stocks (`DT.DOD.DECT.CD`) has no value for high-income countries; the Debtor Reporting
  System covers low- and middle-income borrowers only.
- EIA froze its crude oil import and export series in 2021, so several subjects have no values for the later years.
- `INTL.54-1` (refined products production) ends in 2014 and is left out. `INTL.56-1` (refinery processing gain) is
  included and labelled as processing gain, not throughput.

## Reading a collection

A historical number is never, by itself, a claim about the present. Five surfaces read collections.

### Series tools

`series_history(series_id, subject, from, to, as_of=None)` and
`series_compare(series_id, subjects, from, to, as_of=None)` are read-only tools on the `substrate_read` pack,
available to consult and to the research loop. They read only holdings whose descriptor head is `loaded`; the check is
in the reader (`runtime/_observations_read.py`).

`from` and `to` are required and bound the valid time. A missing or unparseable bound is refused. `as_of` bounds the
record time: each period comes back as the latest revision the provider had published on or before that instant. An
`as_of` before anything was recorded returns nothing.

Each row carries its valid period, record time, value and unit, `source_url`, the `sha256` of the source file, and a
`ref` (`observation:<uuid>`) that the citation builder resolves.

### The `observation` ref kind

A cited observation is registered in `provenance.kinds.GROUNDING_REF_KINDS`, so the verify floor grades a clause that
rests on one. It has no `signal_id` (there is no article or outlet behind it) and carries `ref_id`, the row's uuid. It
is graded on its captured `evidence_text`, which is a fixed rendering of the row itself.

### Historical grounding and the `stale_tense` marker

`GROUNDING_HISTORY` (`analysts/history_grounding.py`) is a desk-grounding block with what the world measured, beside
the blocks built from Legba's own records. It is opt-in per analyst (`method.options.offer_history`, default off,
limited by `history_series_limit`). With the option unset the read does not run and the prompt is unchanged.

Each series line gets its own citation number, so a clause citing one line is graded against that line alone. Every
line ends with the marker:

```
(historical: valid YYYY..YYYY, recorded YYYY-MM)
```

One function (`history_grounding.stale_tense_marker`) renders it in the prompt, in the captured `evidence_text` the
judge grades against, and in the exported endnote. A row missing either time shows `????` in that slot.

### Coverage map

`GET /api/v1/v3/collections/coverage?scope=<desk>` compares, per series, the valid-time spans the manifest declares
with the spans `observations` holds, and lists the holes. The load ledger says how many rows a load wrote; this map
says which years are actually on record.

| status | meaning |
|---|---|
| `held_complete` | the declared span is on record |
| `held_with_holes` | the manifest declares years the table does not hold |
| `declared_not_loaded` | the manifest declares the pair and the table holds none of it |
| `provider_holds_nothing` | the provider publishes nothing for this subject, with the manifest's reason |
| `declared_extent_undetermined` | the coverage block's `values` count does not match its `first..last` span |

### The `history_gap` typed absence

`history_gap` is a [typed absence](GLOSSARY.md) kind on `GET /v3/absence?scope=`, computed by the same reader as the
coverage map (`registry/collections_coverage.py`). Its proof is the load receipt (`collection_loads`,
`ref_kind: collection_load`), and its `as_of` is that load's `finished_at`. Because collections have no cadence, a gap
has a 30-day review interval; after that it reads *last known, not re-checked*. A desk that no loaded holding covers
gets `not_measured`, naming the desks that are covered.

### Consult's provenance census

A consult answer carries `provenance_census`: cited refs counted by their own `origin_class` (`live`,
`web_retrieval`, `history`, `seed`; the three history classes count as one), plus the number of sentences with no
citation. The Consult panel prints it beside the answer header, for example (illustrative)
*"cited: 6 live · 2 history · 0 web · model knowledge: 3 sentences"*. The panel computes nothing itself. Refs are
classified from their rows, and a class that could not be measured is `null`, not `0`.

## Verifying a manifest

```bash
python3 scripts/verify_collection_manifest.py descriptors/collection_series_pilot_2016_2026.yaml
python3 scripts/verify_collection_manifest.py <manifest> --strict
python3 scripts/verify_collection_manifest.py <manifest> --cache-dir /var/tmp/legba-collections
```

The verifier fetches from the providers and prints, per series and subject, the coverage it measures beside the
coverage the manifest declares, with the unit and the first and last year that has a value. Absence prints as `—`.
It imports no database driver and writes nothing.

| exit code | meaning |
|---|---|
| 0 | every series resolved for at least one subject |
| 1 | a series resolved for no subject at all |
| 2 | with `--strict`: measured coverage differs from the declared block, or a series was skipped for a missing key |

A keyed series with no key in the environment is skipped with its reason and never counts as coverage. Each series
declares a `fetch.mode`: `json_api` (one GET per series and subject) or `bulk_jsonl` (one download of a provider
archive, scanned once for every pair that names it). Re-run the verifier when a provider publishes a new release; a
`DRIFT` line means the manifest's coverage block is out of date.

`tests/data_pkg/test_collection_manifest_pilot.py` checks the pilot manifest and the verifier's parsing against
captured World Bank responses and EIA bulk records, without network access.

## Approving and loading

Before a collection moves from `draft` to `reviewed`, an admin checks:

1. **The licence.** The `licence:` block's text, URL and attribution for each provider: what may be kept, what must be
   credited, and whether a noncommercial restriction applies. A manifest without a licence class does not load.
2. **The manifest.** Which series, which subjects, which window.
3. **The load.** Run `scripts/load_collection.py <collection id>` once per version. It is idempotent and resumable.

After the load, check that no trigger fired, freshness did not move, and the coverage map shows the expected spans.
