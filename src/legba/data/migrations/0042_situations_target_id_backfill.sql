-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0042_situations_target_id_backfill.sql
--
-- Phase 5a follow-up (#66.2). Populate situations.target_id so situation
-- grounding can scope on a REAL target_id instead of the category==slug
-- coincidence.
--
-- WHY:
--   resolve_situations now scopes a country assessor's frames on
--   situations.target_id (grounding.py), and situation_clustering now derives
--   target_id from the topic category at write time (country_g20_xx → that
--   target). But the 20 pre-existing rows were written with a blank/NULL
--   target_id, so without this backfill a country assessor would see ZERO
--   situations until clustering re-mints each row. Backfill target_id = category
--   for the country-topic situations (their category IS the country target
--   slug), which is exactly what the new clustering writer would set.
--   Scoping on a populated target_id (not category) means a future THEMATIC
--   situation — distinct target_id — never leaks into a country's grounding.
--
-- SAFETY (idempotent, additive, no schema change):
--   Touches ONLY rows whose target_id is still empty AND whose category looks
--   like a country target slug; a re-apply is a no-op. No column added/dropped,
--   no row deleted. The clustering upsert also self-heals going forward via
--   target_id = COALESCE(situations.target_id, EXCLUDED.target_id).

UPDATE public.situations
   SET target_id = category
 WHERE (target_id IS NULL OR target_id = '')
   AND category LIKE 'country%';
