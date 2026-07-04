-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0074_world_head_signature_repair.sql  (DQ Phase 7 / composition-layers finding
--   "world_assessor emit path")
--
-- PROBLEM: world composition rows were stamped with the FIRST INPUT ROW's
--   target_id (now region_africa) instead of NULL, and 5 stale per-target /
--   legacy-signature world heads never superseded. So a target-scoped read of
--   region_africa surfaces a WORLD composition, a target_id IS NULL query returns
--   nothing, and multiple concurrent world heads pollute head queries. The row is
--   internally incoherent: situation_signature='sit:composition:world_assessor:world'
--   while target_id='region_africa'.
--
-- PAIRED CODE FIX (already live, DQ P6): dapr_actors run() stamps target_id=NULL on
--   a global/meta verify-declaring composition (is_global_meta_composition) instead
--   of the inputs[0] fallback — so NEW world runs land target-less (verified live:
--   the 2026-07-04 12:00Z world head carries target_id IS NULL). This migration
--   folds the rows written BEFORE that fix + the residual mis-stamped ':world' rows.
--
-- THIS MIGRATION (extends the mig-0060 null-target composition fold pattern):
--   (a) NULL the target_id on every world_assessor finding whose signature is the
--       world theme (ends ':world') but still carries a target_id — stashing the
--       prior target_id in data->'_dq0074_prior_target_id' for reversibility.
--   (b) close every OTHER live world_assessor finding head (the 5 stale
--       per-target/legacy heads + any older ':world' head) as superseded_by the
--       CURRENT world head, and mirror a finding_supersessions edge
--       (reason='signature repair (DQ P7-F2)'). The current world head is resolved
--       LIVE as the most-recent target-less world_assessor finding (after (a)) —
--       drift-robust: it is the clean, post-P6-code head, never a mis-stamped
--       region/country row (those still carry a non-null target_id and are the
--       rows being closed).
--
-- SCOPE is analyst_id='world_assessor' ONLY — the blast radius is one analyst.
--   The ':world' suffix match (LIKE '%:world') selects the world theme signature
--   and NEVER a per-country/region signature (those end in the target slug).
--
-- REVERSIBLE (NO row deleted — only null/close/annotate):
--   -- reverse (b):
--   UPDATE analyst_outputs o
--      SET superseded_by = NULLIF(o.data->>'_dq0074_prior_superseded_by','')::uuid,
--          superseded_at = NULL,
--          data = o.data - '_dq0074_prior_superseded_by'
--    WHERE o.data ? '_dq0074_prior_superseded_by';
--   DELETE FROM finding_supersessions WHERE produced_by = 'migration_0074';
--   -- reverse (a):
--   UPDATE analyst_outputs o
--      SET target_id = NULLIF(o.data->>'_dq0074_prior_target_id','')::text,
--          data = o.data - '_dq0074_prior_target_id'
--    WHERE o.data ? '_dq0074_prior_target_id';
--
-- IDEMPOTENT: (a) skips a row already NULL'd (target_id IS NOT NULL + no stash
--   key); (b) skips an already-folded row (superseded_by IS NULL + no stash key)
--   and the edge INSERT is ON CONFLICT DO NOTHING. A re-run finds one live head
--   and matches nothing. On a fresh substrate (no world rows) every statement
--   matches 0 rows — a clean no-op. Routed through the migration runner (ONE txn +
--   ledger; NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-04, migration head 0073):
--   (a) 3 ':world'-signature rows carry target_id='region_africa'
--       (eaf83da3 head + f6505ff0, b8969357 in-chain) -> target_id NULL.
--   (b) >=6 stale world heads closed (count RESOLVED LIVE at apply — a world run
--       that lands between this measurement and apply adds one) -> superseded_by
--       the live head 3f290d75 (2026-07-04 12:00Z, target_id NULL): eaf83da3
--       (:world, region_africa), 47617d52 (country_g20_us), 3de5648e
--       (country_watch_ir), 723f1e33 (country_g20_in), 51ccaba9 (country_g20_tr),
--       d6638380 (legacy sig:americas). Result: ONE world head, target_id NULL.

-- (a) NULL the target_id on the ':world'-signature rows (stash prior target_id).
UPDATE analyst_outputs o
SET data = jsonb_set(
        COALESCE(o.data, '{}'::jsonb),
        '{_dq0074_prior_target_id}', to_jsonb(o.target_id), true
    ),
    target_id = NULL
WHERE o.analyst_id = 'world_assessor'
  AND o.kind = 'finding'
  AND o.situation_signature LIKE '%:world'
  AND o.target_id IS NOT NULL
  AND NOT (COALESCE(o.data, '{}'::jsonb) ? '_dq0074_prior_target_id');

-- (b) Close every OTHER live world head as superseded_by the current world head
--     (the latest target-less world_assessor finding, resolved AFTER (a)).
WITH head AS (
    SELECT id
    FROM analyst_outputs
    WHERE analyst_id = 'world_assessor'
      AND kind = 'finding'
      AND target_id IS NULL
    ORDER BY produced_at DESC, id DESC
    LIMIT 1
)
UPDATE analyst_outputs o
SET superseded_by = h.id,
    superseded_at = COALESCE(o.superseded_at, now()),
    data = jsonb_set(
        COALESCE(o.data, '{}'::jsonb),
        '{_dq0074_prior_superseded_by}',
        to_jsonb(COALESCE(o.superseded_by::text, '')), true
    )
FROM head h
WHERE o.analyst_id = 'world_assessor'
  AND o.kind = 'finding'
  AND o.superseded_by IS NULL
  AND o.id <> h.id
  AND NOT (COALESCE(o.data, '{}'::jsonb) ? '_dq0074_prior_superseded_by');

-- (b') Mirror the live path's finding_supersessions audit edge for each folded row.
WITH head AS (
    SELECT id
    FROM analyst_outputs
    WHERE analyst_id = 'world_assessor'
      AND kind = 'finding'
      AND target_id IS NULL
    ORDER BY produced_at DESC, id DESC
    LIMIT 1
)
INSERT INTO finding_supersessions
    (superseded_finding_id, superseding_finding_id,
     situation_signature, reason, score, produced_by)
SELECT o.id, h.id,
       'sit:composition:world_assessor:world',
       'signature repair (DQ P7-F2)',
       1.0,
       'migration_0074'
FROM analyst_outputs o, head h
WHERE o.analyst_id = 'world_assessor'
  AND o.kind = 'finding'
  AND o.superseded_by = h.id
  AND o.id <> h.id
ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING;
