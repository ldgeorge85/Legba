<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Layers — the per-country source-layer table

The layered source fan-out ([Direction](DIRECTION.md)): today a desk reads one pool of sources. The
next widening ingests, per country, the same stack of source LAYERS — the press split domestic and
foreign, official and state media, public data, a channel-level social digest, and a model-free
physical/hazard layer — and makes the CHANGE in that country's own layer-to-layer divergence the
finding, never the raw gap (every country has one). A layer that is structurally absent is declared as
such per desk, so a missing layer never reads as agreement. This page is L0: the foundation the later
programs (the divergence measure, the finding, the removal-as-signal sweep) read from.

## The three pieces

**The layer table** (`source_layers`, migration 0214) is PLATFORM x COUNTRY -> LAYER, curated and
versioned, with the reason each entry is in — curation IS the layer's bias, so an entry with no stated
reason hides exactly the judgement the table exists to make visible.

**The aperture declaration** (`desk_apertures`, migration 0214) is TARGET x LAYER ->
`{present, absent, unmeasured}`, per desk. `absent` REQUIRES a non-blank reason (enforced twice — the
pydantic model and a table CHECK, `desk_apertures_absent_needs_reason`); `unmeasured` is the honest
default nothing infers away. A loaded map always declares all six layers
(`registry/layer_map_schema.py`'s `LayerMapDescriptor` refuses a map missing one), so a layer with no
row for a target is *undeclared*, never the same thing as `unmeasured`.

**The retag generator** (`scripts/gen_layer_map_draft.py`) derives a first DRAFT layer table for one
country from what the existing `descriptors/source_*.yaml` already declare — `scope.geo`,
`scope.source_class`, `identity.kind`, and the wire/agency set in `descriptors/wire_map.yaml`'s
`same_publisher['ap']`. Every drafted entry carries `reason: "derived: <rule>"`; every drafted aperture
is `declared: unmeasured`. **The code never infers `present` or `absent` — the operator curates.**

## The vocabulary

`src/legba/data/layers/_vocab.py:LAYER_VOCAB` (the migration's two CHECK constraints mirror it exactly):

| Layer | What it means |
|---|---|
| `official` | the target country's OWN government/primary-source publisher, or a state-controlled outlet read as that state's voice |
| `domestic_press` | independent/commercial press headquartered in and covering this country |
| `foreign_press` | international press, wire/agency dispatches, press not scoped to this country |
| `social_digest` | channel-level social monitoring — counts and summaries only, never identities |
| `public_data` | structured feeds: IGO/government open data, sanctions lists, conflict-event datasets, analytical output with no single-country scope |
| `physical` | model-free structured feeds (GeoJSON-style hazard/sensor data) — no editorial voice at all |

`APERTURE_DECLARED_VOCAB`: `present` / `absent` / `unmeasured`.

## Why a layer is not an origin class and not a source class

`origin_class` ([`data/provenance/origin.py`](../src/legba/data/provenance/origin.py), migration 0209)
says WHERE a row came from — live vs. seed vs. imported history. `SourceScope.source_class`
([`data/schemas/source.py`](../src/legba/data/schemas/source.py)) is the editorial tier a source
declares ONCE, platform-wide — reporting / analysis / official / state_media. A LAYER is neither: it is
the COUNTRY-SCOPED READING of a source. The same `state_media` outlet is `official` for the country it
speaks for and, read from another desk, is a foreign state's messaging rather than that desk's own
official layer at all — "the same channel is independent commentary in one country and the state's
voice in another" (Direction). The three axes stay orthogonal on purpose.

## The pilot: Israel

`descriptors/layer_map_il.yaml` is the committed worked example. It started as the retag generator's
own output for `--country IL`, unedited, `identity.state: draft`, `map_version: layer_map_il.draft`,
every reason `"derived: ..."` and every aperture `unmeasured` — the shape below. It has since been
curated (`curate_il`): `identity.state: reviewed`, `map_version: layer_map_il.v1`,
every entry reasoned either `"derived: ... — confirmed"` or `"curated: ..."`, and all six apertures
declared for real (five `present`, one `absent` — IL has no dedicated official-class source; see that
aperture's own reason in the file). The DRAFT shape every generated map still takes, before curation:

```yaml
identity: {id, name, schema_uri, kind: layer_map, owner, created, state}
country: IL           # ISO-3166-1 alpha-2
map_version: layer_map_il.draft
entries:               # [source_id, layer, reason] — reason: "derived: ..." until curated
  - {source_id: source.jpost.frontpage, layer: domestic_press, reason: "derived: ..."}
apertures:              # every layer in LAYER_VOCAB, exactly once
  - {layer: official, declared: unmeasured, reason: ""}
```

`descriptors/layer_map_<cc>.yaml` is a distinct descriptor KIND from a `SourceDescriptor` —
`descriptors/source_*.yaml` stays reserved for sources
(`tests/data_pkg/test_source_class_taxonomy.py`).

## Loading a curated map

Nothing loads automatically. Once an operator has turned a draft into a curated map (real entries,
real apertures, a real `map_version`):

```
PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \
    python3 scripts/load_layer_map.py descriptors/layer_map_il.yaml --target country_watch_il
```

`--target` is the desk this country's aperture half writes to
(`country_watch_<cc>` / `country_g20_<cc>`, `runtime/target_resolution.py`'s convention) — supplied by
the operator, never guessed: a country's layer table is a fact about its information environment, but
which desk reads it is a separate fact.

The load is idempotent and re-versioning: reloading an unchanged file is a no-op; editing content
without bumping `map_version` is refused loudly (`LayerMapVersionConflict`); bumping `map_version`
closes the prior open rows (`valid_until`) and opens new ones — nothing is ever overwritten in place,
the same discipline as `facts` / `nexuses` / `entity_edges`.

## The divergence measure (L2)

`deterministic_handlers/layer_divergence.py` is the unit that turns a loaded map into numbers and
findings — the one reader of these two tables. Per target country, over a 28-day window:

1. **counts** signals per layer per UTC day, wire copies folded to one *within* a (layer, day)
   bucket (`_layer_fold.py`; the same dispatch under `domestic_press` **and** `foreign_press` is the
   narrative-control pair's subject, not a duplicate, so the fold is never global);
2. **baselines** each pair of interest — `official`/`social_digest` (the regime–public gap),
   `domestic_press`/`foreign_press` (narrative control), `official`/`public_data` (the credibility
   gap) — as `log2((a+0.5)/(b+0.5))` against its own rolling 14-day median and MAD;
3. **fires** only when `|z| ≥ 2` on **two consecutive days with the same sign**, named `widening` or
   `narrowing`, severity from the z, confidence from the counts, with the day's top folded signals
   per layer as real citations. A layer under 5 items/day is `thin` and the finding says so.

The aperture binds it: an `absent` layer contributes **no count** and the finding states the
operator's reason; `unmeasured` and `undeclared` are excluded and named; a pair with either side
excluded is skipped and the receipt says which side and why. When the map carries sources for a
layer the aperture calls `absent`, the suppressed count is reported under
`counts_suppressed_by_aperture` — the map and the declaration disagreeing is itself worth reading.

The series and the receipt ride `analyst_outputs.data` (no new table this round), and a run that
fires nothing is suppressed to trace-only, its receipt still landing in
`analyst_traces.output_payload`. `descriptors/analyst_layer_divergence.yaml` is the daily cadence
descriptor, `state: draft`, aimed at RU, IL and Argentina as the no-reference control.

**The classification error is not yet bounded.** A source filed under the wrong layer is a
*directional* error in every number above — it moves the gap one way rather than blurring it — and
no sampled audit has been run. That is [SEAMS #60](SEAMS.md), and `CLASSIFICATION_AUDIT_NOTE` is
stamped on every finding and receipt the unit writes so no reader can mistake an un-audited map for
an audited one.

## Reading it: the divergence map

`GET /api/v1/v3/layers/divergence`
([`registry/layers_api.py`](../src/legba/data/registry/layers_api.py)) is the one read surface over
this unit, and the `system.layer_divergence` panel ("Layer Divergence", Analysis —
[UI.md](UI.md#the-panel-catalog)) is its reader.

It serves the NEWEST receipt, desk by desk, plus each desk's newest FIRED divergence:
`{as_of, receipt_run_id, run_started_at, method_version, payload_schema, classification_audit,
window_days, baseline_days, z_threshold, mad_floor, consecutive_days, thin_min_per_day, layer_vocab,
pairs_declared[], desks: [{target_id, country, map_version, aperture, counts_suppressed_by_aperture,
layers{<layer>: {declared, reason, sources_mapped, thin_days, daily[]}}, pairs[…verbatim…],
fired | null}], desks_unresolved[], warnings[]}`.

Two things about where it reads from. The receipt comes from **`analyst_traces.output_payload`**, not
from `analyst_outputs` — a run that fires nothing is suppressed to trace-only, so a surface reading
the findings table alone would be blank on exactly the days the instrument worked. The fires come
from the summary findings, and because this is a target-agnostic META analyst the rows carry no
`target_id` of their own: the desk lives inside `data.divergences[]`, and the route's `DISTINCT ON`
walks the unnested array (bounded by a `fired_days` window and a row cap).

The route computes **no statistic**. Every pair row — the series, the baselines, the
`no_fire_reason`, the excluded layers and the operator's own reason text — is passed through
verbatim; its whole arithmetic is turning the receipt's `daily` map into a day-ordered list. A desk
with no receipt is absent from `desks` and `receipt_run_id` is `null`, which the panel renders as
"no run yet" — a different fact from a run that measured nothing. `CLASSIFICATION_AUDIT_NOTE` rides
every response and the panel always shows it.

## What this lane does not build

No registry lifecycle governs the `layer_map` descriptor kind — it is validated and loaded directly,
in the `descriptors/wire_map.yaml` tradition of a plain, operator-curated map. Nothing WRITES these
tables over HTTP: the read surface above is read-only, and `scripts/load_layer_map.py` remains the
only writer. The removal-as-signal sweep is a later program still.
