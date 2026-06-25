-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0035_entity_profiles_composite_key.sql
--
-- Entity-resolution false-merge fix (entity-resolution Wave 1).
--
-- WHY:
--   Entity resolution deduped on lower(canonical_name) ALONE
--   (idx_entity_profiles_name, baseline). That false-merges DISTINCT entities
--   that share a name across classes -- proven live: "Georgia" (country +
--   US state) collapsed to ONE node geocoded to Azerbaijan; "Jordan"
--   (country) folded with location mentions; "Chad" undifferentiated. The
--   single key cannot tell a country from a location from a person.
--
-- WHAT:
--   Replace the single-column functional unique index with a COMPOSITE unique
--   index on (lower(canonical_name), entity_class). Same name + different
--   class now resolves to TWO rows (e.g. Georgia/country vs Georgia/location).
--
-- SAFETY (idempotent, ledger-friendly, NO data repair):
--   This RELAXES uniqueness -- every row that satisfied the old single-key
--   uniqueness still satisfies the composite one (a strict superset of allowed
--   states), so CREATE UNIQUE INDEX cannot fail on existing rows. No row is
--   mutated. Pre-existing false-merged rows are corrected by RE-SEED
--   (operator, per locked decision D4), NOT by this migration. The drop is
--   guarded with IF EXISTS and the create with IF NOT EXISTS so re-applying is
--   a no-op.

-- Drop the old single-key unique index (baseline idx_entity_profiles_name).
DROP INDEX IF EXISTS public.idx_entity_profiles_name;

-- Composite uniqueness: (lower(canonical_name), entity_class). entity_class is
-- NOT NULL with a 'entity' default (baseline), so the key is always
-- well-defined.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_profiles_name_class
    ON public.entity_profiles USING btree (lower(canonical_name), entity_class);
