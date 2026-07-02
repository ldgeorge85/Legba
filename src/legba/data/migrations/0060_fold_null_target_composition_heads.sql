-- 0060: fold the NULL-target composition heads that 0058 missed.
--
-- 0058 folded historical meta-composition heads per (analyst_id, target_id), but
-- SQL groups NULL target_ids as DISTINCT (NULL <> NULL), so the world_assessor's
-- world composition (target_id NULL) never collapsed — 20 concurrent live heads
-- survived. The S8-T3 write path stamps NEW heads with a COALESCE(target_id,
-- 'world') signature, so this only closes the historical gap.
--
-- Append-only + idempotent: keeps the newest live head per
-- (analyst_id, COALESCE(target_id,'world')) and stamps superseded_by on the rest.
-- A re-run finds a single live head per group -> UPDATE matches nothing.
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY analyst_id, COALESCE(target_id, 'world')
            ORDER BY produced_at DESC, created_at DESC, id DESC
        ) AS rn,
        first_value(id) OVER (
            PARTITION BY analyst_id, COALESCE(target_id, 'world')
            ORDER BY produced_at DESC, created_at DESC, id DESC
        ) AS newest_id
    FROM public.analyst_outputs
    WHERE analyst_id IN ('world_assessor', 'country_composition')
      AND kind = 'finding'
      AND superseded_by IS NULL
)
UPDATE public.analyst_outputs o
SET superseded_by = r.newest_id,
    superseded_at = COALESCE(o.superseded_at, now())
FROM ranked r
WHERE o.id = r.id
  AND r.rn > 1;
