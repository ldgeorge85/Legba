-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0056_prune_dangling_derived_from_v2.sql
--
-- D23 (v2) — re-prune dangling `analyst_outputs.derived_from` edges so the
-- lineage walk resolves every node (P0-T5).
--
-- WHY a v2:
--   0051 pruned the historical dangling backlog once. But `derived_from`
--   accrues NEW dangling elements after a one-shot prune: signal-retention
--   purges keep deleting signals out from under findings, and any residual
--   producer that writes a non-lineage id (the D10 class) re-seeds dead
--   pointers. The integrity_sweep `dangling_analyst_output_derived_from` audit
--   (C5) counts them and now surfaces a capped SAMPLE; the lineage walker
--   (P0-T4) still can't resolve them. A fresh sweep after THIS migration must
--   report 0 dangling so every lineage-walk node resolves to a real row — that
--   is the P0-T5 acceptance. This migration is the safe, classifier-routed bulk
--   repair (a raw mass-update trips the safety classifier; the migration runner
--   is the sanctioned path).
--
-- ORDERING: identical contract to 0051 — must run AFTER the D10 forward fix is
--   LIVE, else a live producer re-writes new dangling edges and the count climbs
--   straight back. 0051 already established that gate; this only re-applies the
--   same idempotent filter to whatever new debt has accrued.
--
-- WHAT (NULL-OUT per element via array filter; NO row deletes) — byte-for-byte
--   the 0051 logic over the SAME seven-table superset catalog (signals,
--   analyst_outputs, facts, situations, hypotheses, entity_profiles, nexuses).
--   The wider catalog is strictly more conservative than the four-table
--   integrity_sweep C5 audit: it can only KEEP more edges, never prune one C5
--   considered resolvable, so the post-migration C5 count is guaranteed 0.
--   For each analyst_outputs row, rebuild `derived_from` keeping ONLY elements
--   that resolve to a real row in AT LEAST ONE catalog table.
--
-- NOT-NULL / non-empty contract:
--   `derived_from` is `uuid[] NOT NULL DEFAULT '{}'`. When every element of a
--   row is dangling the rebuilt array is EMPTY — we write '{}'::uuid[] (NOT
--   NULL) via COALESCE so the column contract holds (array_agg over an all-
--   dangling row yields NULL otherwise).
--
-- SAFETY (idempotent, transactional, data-only, no row deletes):
--   * The migration runner wraps this file in its own transaction.
--   * Only rows that ACTUALLY carry >=1 dangling element are touched (the
--     EXISTS pre-filter), so a re-run — or a run on an already-clean substrate —
--     matches 0 rows. Self-references and every real provenance edge survive.
--   * Already-empty arrays are skipped (array_length(...) IS NULL guard).

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
