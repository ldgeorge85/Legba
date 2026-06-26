-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0051_prune_dangling_derived_from.sql
--
-- D23 — NULL-OUT dangling `analyst_outputs.derived_from` edges (forward-only).
--
-- WHY:
--   ~24% of `analyst_outputs.derived_from` array elements point at NO row in any
--   lineage table — historical debt from (a) pre-release signal-retention purges
--   that deleted the referenced signals out from under the finding, and (b) the
--   D10 `country_optimizer` bug that wrote `analyst_traces.run_id` (a trace id,
--   not a lineage row) into `derived_from`. The integrity_sweep
--   `dangling_analyst_output_derived_from` audit (C5) counts them; the lineage
--   walker can't resolve them, so they are dead pointers, not provenance. This
--   migration removes ONLY the unresolvable elements, leaving every real
--   provenance edge in place.
--
-- ORDERING: must run AFTER the D10 code fix (country_optimizer derived_from at
--   source) is LIVE — otherwise the active producer immediately re-writes new
--   dangling run_id edges and the count climbs straight back. The roadmap gates
--   this migration behind that forward fix.
--
-- WHAT (NULL-OUT per element via array filter; NO row deletes):
--   For each analyst_outputs row, rebuild `derived_from` keeping ONLY the
--   elements that resolve to a real row in ANY of the seven lineage tables —
--   signals, analyst_outputs, facts, situations, hypotheses, entity_profiles,
--   nexuses. (This is a SUPERSET of the four-table integrity_sweep C5 catalog;
--   the wider catalog is strictly more conservative — it can only KEEP more
--   edges, never prune a live one — so it never removes an edge C5 considered
--   resolvable.) An element survives iff it exists in at least one catalog.
--
-- NOT-NULL / non-empty contract:
--   `derived_from` is `uuid[] NOT NULL DEFAULT '{}'`. When every element of a
--   row is dangling, the rebuilt array is EMPTY — we write '{}'::uuid[] (NOT
--   NULL), preserving the NOT-NULL column contract. array_agg over an all-
--   dangling row would yield NULL; COALESCE(..., '{}') guards that case.
--
-- SAFETY (idempotent, transactional, data-only, no row deletes):
--   * Only rows that ACTUALLY carry >=1 dangling element are touched (the
--     EXISTS pre-filter), so re-running matches 0 rows once clean.
--   * A row whose derived_from is already all-resolvable is skipped (its
--     filtered array would equal the original — excluded by the EXISTS guard).
--   * Self-reference: an element pointing at another analyst_outputs row (incl.
--     the row's own id) is RESOLVABLE and kept; only truly orphaned ids drop.

UPDATE analyst_outputs ao
   SET derived_from = COALESCE(
           (
               SELECT array_agg(df.ref ORDER BY ord)
               FROM unnest(ao.derived_from) WITH ORDINALITY AS df(ref, ord)
               WHERE EXISTS (SELECT 1 FROM signals s          WHERE s.id  = df.ref)
                  OR EXISTS (SELECT 1 FROM analyst_outputs ao2 WHERE ao2.id = df.ref)
                  OR EXISTS (SELECT 1 FROM facts f            WHERE f.id  = df.ref)
                  OR EXISTS (SELECT 1 FROM situations si      WHERE si.id = df.ref)
                  OR EXISTS (SELECT 1 FROM hypotheses h       WHERE h.id  = df.ref)
                  OR EXISTS (SELECT 1 FROM entity_profiles ep WHERE ep.id = df.ref)
                  OR EXISTS (SELECT 1 FROM nexuses nx         WHERE nx.id = df.ref)
           ),
           '{}'::uuid[]                              -- preserve NOT-NULL on all-dangling rows
       )
 WHERE array_length(ao.derived_from, 1) IS NOT NULL  -- skip already-empty arrays
   AND EXISTS (                                       -- only rows with >=1 dangling element
        SELECT 1
        FROM unnest(ao.derived_from) AS d(ref)
        WHERE NOT EXISTS (SELECT 1 FROM signals s          WHERE s.id  = d.ref)
          AND NOT EXISTS (SELECT 1 FROM analyst_outputs ao2 WHERE ao2.id = d.ref)
          AND NOT EXISTS (SELECT 1 FROM facts f            WHERE f.id  = d.ref)
          AND NOT EXISTS (SELECT 1 FROM situations si      WHERE si.id = d.ref)
          AND NOT EXISTS (SELECT 1 FROM hypotheses h       WHERE h.id  = d.ref)
          AND NOT EXISTS (SELECT 1 FROM entity_profiles ep WHERE ep.id = d.ref)
          AND NOT EXISTS (SELECT 1 FROM nexuses nx         WHERE nx.id = d.ref)
   );
