-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0229_profile_projection.sql
--
-- THE PER-PROFILE PROJECTION.
--
-- WHY
-- ---
-- A profile's case-shaped vocabulary — its subject groups, its unit roster
-- with each unit's cadence, its tier labels, the Morning Read's bands, the
-- absence route's scope resolution, its crosswalk rows — was a set of code
-- constants every reader carried (the registry, the workstation, the gap
-- strip). This table is that vocabulary as a MATERIALIZED READ MODEL, one
-- plane per profile, built from the profile's declared view
-- (`profiles/<id>/projection/view.yaml`) and the live substrate by the
-- `profile_projector` deterministic analyst, and served at
-- GET /api/v1/registry/projection/<plane>[/<section>].
--
-- SHAPE
-- -----
-- One row per (plane, section, key): `value` is the jsonb the view resolves
-- the key to; `ord` is its declared order inside the section (tiers, units and
-- bands are ORDERED vocabularies). The section vocabulary is closed in code
-- (`legba.data.profile_projection.SECTIONS`) and the CHECK below admits the
-- slug shape plus the one reserved `_meta`.
--
-- `plane` IS THE LOAD-BEARING COLUMN. Each profile's projection is a separate
-- plane: two profiles on one substrate never read each other's rows, and the
-- builder's whole rebuild is `DELETE ... WHERE plane = $1` + one INSERT inside
-- ONE transaction — a rebuild of one plane never touches another, and a
-- reader sees the old plane until the commit lands, the new one after.
--
-- THE `_meta` ROW. (plane, '_meta', 'build') is the plane's build receipt:
-- `projected_at` is the published staleness scalar (a reader past
-- LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS refuses `projection_stale`), and
-- `value` carries row_count / section_counts / unmeasured / build_seconds /
-- the view digest. No receipt = `projection_empty`. Every row of one build
-- carries the same `projected_at` (the transaction's now()).
--
-- NO FOREIGN KEYS, DELIBERATELY (the 0207 graph_arcs rule): a projection is
-- disposable — rebuilt by SELECT from tables that do carry integrity, so it
-- can be stale, never wrong, and no retention cascade can reach into it.
--
-- SAFETY
-- ------
-- Additive and idempotent (IF NOT EXISTS), routed through the runner (no
-- inline BEGIN/COMMIT). Ships EMPTY: nothing is built until the projector's
-- descriptor is activated and LEGBA_PROFILE_PROJECTION is on; until then every
-- reader refuses by name. The primary key is the only index — every read is
-- `plane = $1 [AND section = $2]`, a PK prefix scan over a table of a few
-- hundred rows per plane.

CREATE TABLE IF NOT EXISTS public.profile_projection (
    plane        text        NOT NULL CHECK (plane ~ '^[a-z][a-z0-9_]*$'),
    section      text        NOT NULL CHECK (section ~ '^([a-z][a-z0-9_]*|_meta)$'),
    key          text        NOT NULL CHECK (key <> ''),
    ord          integer     NOT NULL DEFAULT 0,
    value        jsonb       NOT NULL,
    projected_at timestamptz NOT NULL,
    PRIMARY KEY (plane, section, key)
);

COMMENT ON TABLE public.profile_projection IS
    'W9 VIEW-1: the per-profile projection, one plane per profile, whole-rebuilt '
    'per plane by the profile_projector analyst from profiles/<id>/projection/'
    'view.yaml + the live substrate. (plane, ''_meta'', ''build'') is the build '
    'receipt: projected_at is the staleness scalar every reader publishes. '
    'Disposable: no foreign keys. Read at /api/v1/registry/projection/<plane>.';

COMMENT ON COLUMN public.profile_projection.plane IS
    'The profile''s plane id (profile.yaml projection.plane). A rebuild touches '
    'exactly one plane.';
COMMENT ON COLUMN public.profile_projection.ord IS
    'The key''s declared order inside its section (tiers, units, bands are ordered).';
