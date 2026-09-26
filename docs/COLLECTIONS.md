<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->

# Collections

*Bounded, versioned, curated holdings of the past — loaded once by the operator, never scheduled, and fenced from
every surface that reads "now".*

## What a collection is, and what it is not

A **source** is a live feed. It is polled on a cadence, its silence is a health signal, its arrival can wake a
reactive trigger, and everything it produces is stamped `origin_class='live'`. Legba's whole substrate is built on
that: a signal's `fetched_at` sits near its publication, and freshness, source health, calibration, salience and
the alert plane all read it as the present.

A **collection** is the opposite kind of thing. It is a *holding*: a manifest naming exactly which series or
documents, for which subjects, over which years, from which provider, under which licence — fetched once, written
directly to the substrate, and then left alone. Nothing about a collection is scheduled. A collection has no
cadence, no cooldown and no health.

That distinction is not cosmetic, and it is the reason collections exist as their own descriptor family rather
than as "a source with a date range". Ten years of history loaded into the live substrate would read as the
largest surge in the platform's life, as thirty publishers coming back from the dead, and as a calibration set
made of numbers nobody predicted. Every collection therefore carries a **firewall block** that names, explicitly,
the surfaces it is excluded from — and a manifest that omits any of them does not load.

```yaml
firewall:
  readers_opt_in: [consult, research, claim_watch, program6_baselines, replay]
  excluded_from:
    [cadence_analysts, freshness, source_health, calibration, salience, alerts,
     reactive_triggers, surge_detection]
```

Series rows land in `observations` (bitemporal: `valid_from`/`valid_to` is the period a number is *about*,
`record_time` is when the provider published or revised it — a 2016 GDP figure revised in 2023 is two rows).
Documents would land in `signals` with a history `origin_class` and a `collection_id`, and that half is **not
built** ([SEAMS #62](SEAMS.md)). The origin-class vocabulary that carries the firewall is
`src/legba/data/provenance/origin.py`.

File convention: **`descriptors/collection_<id>.yaml`** — never `source_`. The prefix is load-bearing: the source
machinery globs `descriptors/source_*.yaml`, and a mis-prefixed collection would be picked up as a live feed.

Lifecycle: `draft` → `reviewed` → `loaded` → `superseded`. A collection version is the hash of its manifest.

## The tables (migration 0220)

| table | what it holds |
|---|---|
| `collection_descriptors` | the `collection` descriptor family's rows — the fifth family beside target / analyst / source / action_pack, with `origin_shape`, `origin_class`, `licence_class` and `collection_version` lifted out of the body so a reader never has to open it |
| `observations` | series rows: bitemporal, natively RANGE-partitioned by `valid_from` (one partition per year 2016–2027 plus a DEFAULT; **no TimescaleDB**) |
| `entity_aliases` | provider naming → `entity_profiles`, scoped per collection. Read by the loader and by the collection readers, **never by entity resolution** — a provider's naming quirk cannot re-shape the live graph |
| `collection_loads` | one row per (collection, manifest version): the resume key, the counts, the timestamps, the status |

Three properties of `observations` are worth stating as rules rather than as columns:

* **A revision is a second row, never an overwrite.** `record_time` is part of the identity key
  `(collection_id, series_id, subject, valid_from, valid_to, record_time)`, so a provider that restates history
  (the World Bank restates the whole WDI series on every release) cannot silently replace what was knowable
  before. An as-of reader picks by `record_time <= D`.
* **That same key is what makes the loader idempotent** — not any bookkeeping inside the loader. Loading the same
  collection version twice writes the same rows, conflicts, and does nothing.
* **No row exists for a year a provider does not hold.** Absence is absence. A `CHECK` requires exactly one of
  `value` / `value_text`, so a valueless row is unstorable, and the EIA's frozen crude-trade tail is simply
  missing rather than zero.

`origin_class` on `observations` is `CHECK`ed to the three history classes: a collection can never write a
live-class row.

## Loading a collection

```bash
python3 scripts/load_collection.py descriptors/collection_series_pilot_2016_2026.yaml --dry-run
python3 scripts/load_collection.py collection.series_pilot_2016_2026
python3 scripts/load_collection.py collection.series_pilot_2016_2026 --resume
```

An operator action, run once per collection **version**. It takes a path or an `identity.id`, validates the
manifest through `legba/collection/1.0.0` (the fail-closed licence class, the eight-surface firewall, the
history-only origin class), and reads the fetch plan through
`scripts/verify_collection_manifest.py` — **one parser, two readers**: a loader that parsed a World Bank
envelope its own way could disagree with the verifier about what a provider holds, and the disagreement would
surface only as a wrong number inside a citation.

It writes rows **directly**. No NATS publish, no ingest pipeline, no enrichment enqueue, no analyst run — that
directness is the firewall's first line, and it is enforced structurally: `test_collection_loader.py` reads the
loader's import graph and fails if it ever reaches `legba.runtime`, `legba.data.filters`, `legba.data.sources`,
`legba.data.analysts`, `legba.data.outputs`, `legba.data.alerts` or any NATS client, and reads its SQL verbs
and fails if it ever writes a table other than `observations` and `collection_loads`.

Every row carries `collection_id`, the history `origin_class`, the `source_url` it came from, the **`sha256` of
the file** the number was read out of (the fetched JSON body, or the provider's bulk archive) and a
`provenance` block with the provider revision, the row offset, the manifest version and the licence class.

**`--resume` and what it is careful about.** `collection_loads.resume_key` is the last `series_id:subject` pair
whose rows are *committed*, not the last one *read*. Rows are batched, so a pair can be fetched into a batch the
crash took down; recording that pair as done would lose exactly those rows. Resume therefore re-fetches any pair
whose rows are not on disk — which is free, because the identity key makes the second write a no-op. Resume
skips work; the unique key is what guarantees correctness.

**What it refuses, loudly.**

* A `documents_*` loader kind — SEAMS #62. It raises before a single fetch.
* A cadence it cannot build a PERIOD from. The shared provider parsers key their maps by YEAR, so `annual` is
  what a row can be built from today; a sub-annual cadence raises rather than stamping a whole-year validity on
  a monthly number.
* A pair whose provider published no revision stamp. `record_time` is the *provider's* instant; stamping `now()`
  in its place would turn a 2016 number into something recorded today. The pair is **skipped with its reason
  recorded in the ledger**, never written with a guessed time.

`--dry-run` fetches and reports what it *would* write and touches the database not at all — not even the ledger
row. Passing no connection outside `--dry-run` is refused rather than silently downgraded.

## The firewall, proven

The firewall is not a claim in a YAML block; it is four layers, and each has a test that would fail if the
layer went away.

1. **The descriptor refuses.** `firewall.excluded_from` must name **exactly** the eight surfaces — not "at
   least". A manifest missing one does not validate, and the registry returns 422 (one test per surface).
2. **The loader cannot reach the event plane.** No NATS, no ingest, no enrichment — asserted on the import
   graph and on the SQL verbs, not on intent.
3. **A load moves nothing.** `test_collection_loader.py` snapshots every table the eight surfaces write —
   `trigger_state`, `source_poll_outcomes`, `source_track_records`, `source_dossiers`, `source_ratings`,
   `source_credibility`, `band_calibration_claims`, `band_calibration_scan_state`, `grader_calibrations`,
   `desk_baselines`, `alert_sink_deliveries`, `alert_trigger_watermarks`, `analyst_outputs`, `analyst_traces`,
   `signals`, `facts`, `events`, `nexuses`, `situations` — plants a live sentinel signal so "unchanged" means
   "still exactly what I put there" rather than "still empty", loads the pilot's recorded pairs, and asserts
   every count and the sentinel's own `salience` and `updated_at` are untouched.
4. **The readers are gated.** SEAMS #57's sweep: every reader on the eight surfaces renders the origin-class
   leg from `data/provenance/origin.py`, and the reactive trigger plane gates the delivered ROW
   (`coalescer.is_live_origin`) because a NATS message has no WHERE clause.
   `tests/data_pkg/test_origin_firewall_surfaces.py` plants a synthetic 40-row history burst beside a 3-row
   live population in one geo and measures what each surface sees: the desk baseline's sigma, the alert edge
   detector's current window, the geo convergence scan and the salience batch all read **3**. The same file
   runs the counterfactual — the identical query with the leg stripped out reads **43**, a 14× step, which is
   exactly what an edge detector fires on.


## The first collection

`descriptors/collection_series_pilot_2016_2026.yaml` — **eleven annual series, four desks, ten years (2016–2025),
44 (series, subject) pairs**, both providers free, keyless and publicly licensed.

| | |
|---|---|
| Subjects | `country_g20_us` (US) · `country_watch_il` (IL) · `country_watch_ir` (IR) · `country_watch_ua` (UA) |
| Window | 2016-01-01 … 2025-12-31, annual |
| `origin_shape` / `origin_class` | `archive_only` / `archive` |
| `licence_class` | `public` — World Bank CC BY 4.0, EIA U.S. public domain |
| Loader | `series_api`, priority `low` |
| State | `draft` — nothing is loaded. The schema, `observations` and the loader have shipped, so the operator's `draft` → `reviewed` approval (the licence line) is now the only thing between it and a load |

**World Bank** (World Development Indicators, `api.worldbank.org/v2`, keyless): GDP growth, consumer-price
inflation, military expenditure % of GDP, external debt stocks, energy use per capita, FDI net inflows % of GDP.

**EIA** (International Energy Data, dataset `INTL`, keyless via the bulk route): crude oil production, refinery
processing gain, refined-products consumption, crude oil exports, crude oil imports.

### On the EIA API key

The EIA **v2 REST API** (`api.eia.gov/v2/...`) requires a free registered `api_key`. The **bulk download route
does not**: `https://api.eia.gov/bulk/INTL.zip` serves the complete International Energy Data set — 105,055 series
records as JSON lines, `accessLevel: public` — over plain HTTP with no credential. This collection reads the bulk
route, so **no EIA key is needed and none was registered.** A future collection that needs the v2 route (monthly
partial-year data lands there first) would open one item: *a free EIA API key, stored in the vault as
`eia.api_key`.*

### What the providers do not hold

The manifest records real holes rather than filling them. Two are worth knowing before reading anything built on
this collection:

* **World Bank external debt stocks (`DT.DOD.DECT.CD`) has no value for the US or Israel in any year.** The Debtor
  Reporting System covers low- and middle-income borrowers only. Iran and Ukraine are reported through 2024.
  This is *absence*, not zero, and the manifest says so with a null and a reason.
* **EIA crude oil imports/exports were frozen by the provider in 2021** (`last_updated: 2021-07-09`). The series
  stop at 2020 for US/IL and at 2018 for IR/UA — five and seven of the ten years are simply not published.

`INTL.54-1` (*refined petroleum products production*) stops at 2014 for all four subjects and is therefore **not**
in the collection at all. Refinery *throughput* is not published on any keyless route covering the four subjects
across the window; `INTL.56-1` (*refinery processing gain*) stands in for it and the manifest says so in as many
words, so a reader is never handed processing gain labelled as throughput.

## Readers — how a holding is read, and cited

A collection that nothing reads is a table. These are the five surfaces that read one, and
the rule they share: **a historical number is never, on its own, a claim about the present.**

### The series tools

`series_history(series_id, subject, from, to, as_of=None)` and
`series_compare(series_id, subjects, from, to, as_of=None)` — read-only pack tools on the
`substrate_read` pack, available to **consult** and to the **GATHER (research)** loop, the two
readers the firewall's `readers_opt_in` names first. They read only holdings whose descriptor
head is in state **`loaded`**: the operator's approval is the gate, and it is enforced in the
reader (`runtime/_observations_read.py`) rather than trusted to a caller.

`from`/`to` are **required** and bound the VALID time — the period the numbers are *about*. A
missing or unparseable bound **refuses** rather than widening to all-time, because a widened
window is the one failure a reader cannot see in the answer. `as_of` is a different question:
it bounds the **record** time, so each period comes back as the latest revision the provider
had published *on or before that instant*. An `as_of` before anything was recorded returns
nothing, which is the honest answer and not an error.

Every returned row carries its valid period, its record time, the value with its unit, the
`source_url`, the **`sha256` of the file the number was read out of**, and a
`ref` — `observation:<uuid>` — the citation builder resolves.

### The `observation` ref kind

A cited observation is a citation like any other, registered in
`provenance.kinds.GROUNDING_REF_KINDS` so the verify floor grades a clause resting on one
instead of scoring it as an unresolved citation. It carries **no `signal_id`** (an
observations row is not a signals row, has no article behind it and no outlet) and **does**
carry `ref_id` — an observations row has a real uuid, so pointing at it is the honest drill
target rather than a fabricated anchor.

It is graded on its own captured `evidence_text`, which is the deterministic rendering of the
row itself. That text is not a *summary of* evidence, it **is** the evidence — which is
exactly the property `event` lacks, and exactly why `event` stays out of that set while this
one belongs in it.

### The historical grounding block, and the `stale_tense` marker

`GROUNDING_HISTORY` (`analysts/history_grounding.py`) is the seventh desk-grounding block and
the first that is not about this platform: the six before it are Legba's own memory, this one
is what the world measured. It is **opt-in per analyst** (`method.options.offer_history`,
default off, bounded by `history_series_limit`) on exactly the terms of the OPEN EVENTS grant
— with the knob unset the read does not fire, no row exists, and the rendered prompt is
byte-identical to the six-block render.

It is the only block that takes **more than one ordinal**: one per series LINE. Every other
block is a single orienting index, so a clause resting on it rests on the whole thing. A
historical series block is N independent numbers, each with its own period and its own
provider file; a single ordinal over eleven of them would mean a clause citing `[12]` is
graded against a block in which *some* number supports it, which is precisely how a 2016
figure gets graded as a current claim.

Every line ends with the **`stale_tense` marker**, rendered exactly:

```
(historical: valid YYYY..YYYY, recorded YYYY-MM)
```

One function (`history_grounding.stale_tense_marker`), three surfaces: the prompt line the
model reads, the captured `evidence_text` the judge grades against, and the exported endnote.
A row missing either time renders `????` in that slot rather than a guessed year — a marker
that quietly invents a period is worse than no marker, because the whole point of it is that
the period is checkable.

### The era coverage map

`GET /api/v1/v3/collections/coverage?scope=<desk>` — per series, the valid-time spans the
manifest **declares** the provider holds against the spans `observations` actually **holds**,
and the holes between them. The load ledger says how many rows a load wrote; that is a
different question from which *years* are on record, and a load that finished with 355 of 365
rows is indistinguishable from a complete one in the ledger alone.

Four statuses, and keeping them apart is the whole point:

| status | means |
|---|---|
| `held_complete` | the declared span is on record |
| `held_with_holes` | the manifest declares years the table does not hold |
| `declared_not_loaded` | the manifest declares the pair and the table holds none of it |
| `provider_holds_nothing` | the **provider** publishes nothing for this subject — an absence at the source, carried with the manifest's own reason, never our gap |
| `declared_extent_undetermined` | the coverage block's `values` count does not match its own `first..last` span, so which years are provider nulls is not recorded and a hole cannot be told from one |

### The `history_gap` typed absence

The same measurement, typed: `history_gap` is the eighth
[typed absence](GLOSSARY.md#analysis--methods) kind on `GET /v3/absence?scope=`, composed off
the same reader (`registry/collections_coverage.py`) so the map and the absence cannot tell a
reader two different stories about the same silence.

Its proof is a **load receipt** (`collection_loads`, `ref_kind: collection_load`) rather than a
read: a gap is the absence of a row, so the honest thing to point at is the run that should
have written it. Its `as_of` is that load's `finished_at` and never the moment somebody asked;
a collection has no cadence by construction, so the shelf life is a stated 30-day **review**
interval rather than a schedule, and past it the gap reads *last known, not re-checked*.

A desk no loaded holding names is **not** a gap. It goes to `not_measured` naming the desks the
holdings do cover — a gap in a holding that does not exist is not an absence this platform can
type.

### Consult's provenance census

The consult answer carries `provenance_census`: the cited refs counted by their **own**
`origin_class` column (`live` / `web_retrieval` / `history` / `seed`, the three history classes
folded into one bucket), plus the number of sentences carrying no citation at all. The Consult
panel prints one line beside the answer header — *"cited: 6 live · 2 history · 0 web · model
knowledge: 3 sentences"* — and computes nothing of its own: a share the client derived could
disagree with the answer it is printed beside.

Refs are classified from the rows, never from which tool returned them — a guess dressed as
provenance is worse than no number. A class that could not be measured is `null`, not `0`:
"cites no live reporting" is a claim, and absence is not.

## Running the verifier

```bash
python3 scripts/verify_collection_manifest.py descriptors/collection_series_pilot_2016_2026.yaml
python3 scripts/verify_collection_manifest.py <manifest> --strict
python3 scripts/verify_collection_manifest.py <manifest> --cache-dir /var/tmp/legba-collections
```

It reads the manifest, goes to the providers, and prints the coverage it **measures** beside the coverage the
file **declares** — one row per (series, subject), with the unit and the first/last year carrying a value:

```
series                                 subj unit                         first  last vals null     declared  status
wb.gdp_growth_annual_pct               IR   pct_per_year                  2016  2025   10    0       10v/0n  ok
wb.external_debt_stocks_usd            US   current_usd                      —     —    0   10       0v/10n  ok (declared not held)
eia.crude_oil_exports_tbpd             IR   thousand_barrels_per_day      2016  2018    3    7        3v/7n  ok
```

Absence prints as `—`, never as a zero. Exit codes: **0** every series resolved for at least one subject; **1** a
series resolved *nowhere* (no subject, no year, no value); **2** `--strict` and either measured coverage drifted
from the declared block or a series was skipped for a missing key. A keyed series with no key in the environment
is **skipped with its reason printed** — a skip never counts as coverage, so a manifest that quietly needs
credentials cannot read as a verified one.

The verifier fetches, it does not load: no database driver is imported, no row is written, no descriptor is
registered. Loading is `scripts/load_collection.py` and is a different program with a different blast radius.

It reads two fetch shapes, declared per series in `fetch.mode`: `json_api` (one GET per series and subject) and
`bulk_jsonl` (one download of a provider archive, scanned once for every pair that names it — twenty EIA series
would otherwise be twenty downloads of the same 24 MB file).

Re-run it whenever a provider release lands. A `DRIFT` line means the provider moved and the manifest's coverage
block is now a claim about the past, which is exactly what a hand-written coverage block cannot notice on its own.

`tests/data_pkg/test_collection_manifest_pilot.py` pins the manifest as data and drives the verifier's parsing
half against fixtures — real captured World Bank responses and EIA bulk records. **No test in it opens a socket.**

## What the operator approves

Per collection, before it moves `draft` → `reviewed`:

1. **The licence line.** The `licence:` block's text, URL and attribution for each provider — what may be kept,
   what must be credited, and whether any noncommercial bar applies. A manifest without a recorded licence class
   does not load; this is the fail-closed axis.
2. **The manifest itself** — which series, which subjects, which window. An agent drafts it; the operator's
   approval is what makes it loadable.
3. **The load.** Collections are never scheduled. `scripts/load_collection.py <collection id>` is an operator
   action, run once per collection version, idempotent and resumable.

Then, after the load: zero trigger fires, zero freshness movement, and the era coverage map showing the spans
actually held — the proofs a load owes.
