-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0214_source_layers.sql
--
-- Program 6 L0 — the LAYER TABLE and the APERTURE DECLARATION
-- (docs/DIRECTION.md, "The layered source fan-out"). The next widening ingests,
-- per country, the same stack of source LAYERS (the press split domestic and
-- foreign, official and state media, public data, a channel-level social
-- digest) and makes the CHANGE in that country's own layer-to-layer divergence
-- the finding — never the raw gap, which every country has. A layer that is
-- structurally absent has to be DECLARED as such, per desk, so a missing layer
-- never reads as agreement.
--
-- TWO TABLES, deliberately separate:
--   source_layers  — PLATFORM x COUNTRY -> LAYER, curated + versioned, with the
--                    reason each entry is in (curation is the layer's bias).
--   desk_apertures — TARGET x LAYER -> {present, absent, unmeasured}, per desk.
-- Neither writes the other: a source can be curated into a country's layer
-- table before or after that desk's own aperture is declared, and the join
-- between them (which sources back a desk's `present` layers) is a query, not
-- a foreign key — `source_layers.country` and `desk_apertures.target_id` speak
-- different vocabularies (ISO-3166-1 alpha-2 vs. `country_watch_<cc>` /
-- `country_g20_<cc>` target ids) and the mapping between them is
-- `runtime/target_resolution.py`'s, not this migration's to encode.
--
-- RE-VERSIONING, THE ENTITY_EDGES SHAPE (migration 0143). Neither table's
-- business key (source_id+country for source_layers; target_id+layer for
-- desk_apertures) is the PRIMARY KEY — a synthetic uuid is, with a
-- `valid_from`/`valid_until` window and a PARTIAL unique index over the OPEN
-- predicate (`valid_until IS NULL`). A `map_version` reload for an unchanged
-- assignment is therefore a legitimate re-INSERT candidate that the loader's
-- own idempotence check (not a DB constraint) turns into a no-op; a changed
-- assignment closes the old open row (`valid_until = now()`) and opens a new
-- one carrying the new `map_version`. Nothing is ever UPDATEd in place and
-- nothing is ever deleted — a map's history stays fully re-arguable, the same
-- discipline as `facts`/`nexuses`/`entity_edges`.
--
-- VOCABULARY. `layer` is the six-value closed set in
-- `src/legba/data/layers/_vocab.py:LAYER_VOCAB` (official / domestic_press /
-- foreign_press / social_digest / public_data / physical), the two CHECKs
-- pinned equal to it by `tests/data_pkg/test_source_layers_ddl.py` — a LAYER is
-- orthogonal to `origin_class` (0209 — where a row came from) and to
-- `SourceScope.source_class` (schemas/source.py — the platform-wide editorial
-- tier a source declares once): a layer is the COUNTRY-SCOPED reading of a
-- source, which is why the vocabulary and the mapping rule live beside this
-- table rather than folded into either of those. `declared` is the
-- three-value closed set `APERTURE_DECLARED_VOCAB` (present / absent /
-- unmeasured); `desk_apertures_declared_vocab` is this file's marker
-- constraint. `desk_apertures_absent_needs_reason` is the constraint that
-- makes a reasonless absence unrepresentable at the table, not just at the
-- pydantic layer (`registry/layer_map_schema.py` enforces the identical rule
-- before a row is ever built, so the same illegal shape is refused twice, by
-- two different mechanisms, on purpose).
--
-- WHAT WRITES THESE TABLES. `scripts/load_layer_map.py` (via
-- `legba.data.layers.loader`) ONLY, in this lane — no analyst, no HTTP route,
-- no registry-lifecycle registration. `scripts/gen_layer_map_draft.py`
-- produces a candidate YAML for an operator to curate; the draft is NEVER
-- loaded automatically.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only, no data-touching statements (both tables land empty). The
-- runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0143/0209).

-- ---------------------------------------------------------------------------
-- 1. source_layers — PLATFORM x COUNTRY -> LAYER
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.source_layers (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- IDENTITY. `source_id` is NOT a foreign key into `source_descriptors`:
    -- the retag generator and a curated map both name sources by their
    -- descriptor id string, and a layer map may be curated (or drafted) for a
    -- source that has not been registered yet, or that was registered and
    -- later retired — the layer assignment is a statement about the map, and
    -- it must stay readable either way. `country` is the TARGET country this
    -- assignment is FOR (ISO-3166-1 alpha-2, uppercase) — the same outlet can
    -- carry a different layer/reason in a different country's map.
    source_id    text NOT NULL,
    country      text NOT NULL
                 CONSTRAINT source_layers_country_iso2
                 CHECK (country ~ '^[A-Z]{2}$'),

    layer        text NOT NULL
                 CONSTRAINT source_layers_layer_vocab
                 CHECK (layer IN ('official', 'domestic_press', 'foreign_press',
                                  'social_digest', 'public_data', 'physical')),

    -- WHY THIS ENTRY IS IN. Never optional — curation IS the layer's bias
    -- (docs/DIRECTION.md), so an entry with no stated reason hides exactly
    -- the judgement the table exists to make visible. Mirrored by
    -- `registry/layer_map_schema.py:LayerMapEntry`'s identical, non-bypassable
    -- pydantic check.
    reason       text NOT NULL
                 CONSTRAINT source_layers_reason_nonblank
                 CHECK (length(btrim(reason)) > 0),

    map_version  text NOT NULL,

    -- TEMPORAL. Same open-row contract as `entity_edges` (0143): open when
    -- `valid_until IS NULL`. A reload of an unchanged assignment is the
    -- loader's no-op (checked in Python, not here); a changed assignment
    -- closes the old open row and opens a new one under the new map_version.
    valid_from   timestamptz NOT NULL DEFAULT now(),
    valid_until  timestamptz,

    created_at   timestamptz NOT NULL DEFAULT now()
);

-- One open layer assignment per (source, country) — a source reads as exactly
-- one layer in any one country's map at a time. Closed history is
-- unconstrained, so the assignment may change across map versions.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_layers_open
    ON public.source_layers (source_id, country)
    WHERE valid_until IS NULL;

-- "this country's layer table" — the read the composition/scorecard tower's
-- later consumer runs.
CREATE INDEX IF NOT EXISTS idx_source_layers_country_open
    ON public.source_layers (country, layer)
    WHERE valid_until IS NULL;

-- "every country a source has been mapped into" — the retag generator's own
-- sanity read, and any future cross-country audit.
CREATE INDEX IF NOT EXISTS idx_source_layers_source
    ON public.source_layers (source_id);

COMMENT ON TABLE public.source_layers IS
    'Program 6 L0: PLATFORM x COUNTRY -> LAYER, curated and versioned. Written '
    'only by scripts/load_layer_map.py. See migration file header for the '
    'open-row/re-versioning contract (mirrors entity_edges, 0143).';
COMMENT ON COLUMN public.source_layers.layer IS
    'official | domestic_press | foreign_press | social_digest | public_data | '
    'physical — src/legba/data/layers/_vocab.py:LAYER_VOCAB. Orthogonal to '
    'origin_class (0209) and to SourceScope.source_class (schemas/source.py).';

-- ---------------------------------------------------------------------------
-- 2. desk_apertures — TARGET x LAYER -> {present, absent, unmeasured}
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.desk_apertures (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- `target_id` is NOT a foreign key into descriptor storage, for the same
    -- reason `source_layers.source_id` isn't: an aperture may be declared for
    -- a target ahead of (or after) that target's own registration/retirement,
    -- and the declaration must stay readable regardless.
    target_id    text NOT NULL,

    layer        text NOT NULL
                 CONSTRAINT desk_apertures_layer_vocab
                 CHECK (layer IN ('official', 'domestic_press', 'foreign_press',
                                  'social_digest', 'public_data', 'physical')),

    -- THE MARKER CONSTRAINT (lane p6_l0). The closed three-state vocabulary —
    -- src/legba/data/layers/_vocab.py:APERTURE_DECLARED_VOCAB — that keeps a
    -- missing layer from silently reading as agreement.
    declared     text NOT NULL
                 CONSTRAINT desk_apertures_declared_vocab
                 CHECK (declared IN ('present', 'absent', 'unmeasured')),

    -- `absent` MUST be reasoned; `present`/`unmeasured` may carry an empty
    -- string (their meaning does not depend on a stated reason the way a
    -- claimed absence does). Mirrored by
    -- `registry/layer_map_schema.py:LayerMapAperture`'s identical pydantic
    -- model-validator — the same illegal shape refused twice, on purpose.
    reason       text NOT NULL DEFAULT ''
                 CONSTRAINT desk_apertures_absent_needs_reason
                 CHECK (declared <> 'absent' OR length(btrim(reason)) > 0),

    map_version  text NOT NULL,

    valid_from   timestamptz NOT NULL DEFAULT now(),
    valid_until  timestamptz,

    created_at   timestamptz NOT NULL DEFAULT now()
);

-- One open declaration per (target, layer).
CREATE UNIQUE INDEX IF NOT EXISTS uq_desk_apertures_open
    ON public.desk_apertures (target_id, layer)
    WHERE valid_until IS NULL;

-- "this desk's whole aperture" — the read a composition/scorecard consumer
-- runs to render the six-layer row for one target.
CREATE INDEX IF NOT EXISTS idx_desk_apertures_target_open
    ON public.desk_apertures (target_id)
    WHERE valid_until IS NULL;

COMMENT ON TABLE public.desk_apertures IS
    'Program 6 L0: TARGET x LAYER -> {present, absent, unmeasured}, curated and '
    'versioned. Written only by scripts/load_layer_map.py. A layer with no row '
    'for a target is UNDECLARED, not the same as unmeasured — the loader''s '
    'contract is that a loaded map always declares all six layers '
    '(registry/layer_map_schema.py enforces full coverage before load).';
COMMENT ON COLUMN public.desk_apertures.declared IS
    'present | absent | unmeasured — src/legba/data/layers/_vocab.py:'
    'APERTURE_DECLARED_VOCAB. absent REQUIRES a non-blank reason '
    '(desk_apertures_absent_needs_reason) so a missing layer never silently '
    'reads as agreement.';
