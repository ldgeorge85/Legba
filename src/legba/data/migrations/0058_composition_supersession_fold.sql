-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0058_composition_supersession_fold.sql
--
-- S8-T3 — fold the historical meta_findings_synthesizer COMPOSITION heads.
--
-- Composition findings (the per-COUNTRY country_composition reads and the WORLD
-- read) carry no entity/topic content, so finding_supersession.derive_signature
-- returned None and they never clustered — every cadence cycle left ANOTHER live
-- head. Live symptom (PREPUSH_REVIEW_2026-07-01): ~79/80 live heads, with ~8
-- concurrent United-States composition heads reachable by the read/findings API
-- (hidden from the FUSION read only by its DISTINCT ON (analyst_id, target_id)
-- belt). The forward fix stamps data['situation_signature'] on new composition
-- outputs; this migration folds the rows written BEFORE that fix.
--
-- For each (analyst_id, target_id) composition cluster it keeps the NEWEST head
-- (produced_at DESC, id DESC — matching finding_supersession._pick_latest) and
-- links every older head to it: superseded_by / superseded_at pointer + the same
-- situation_signature the live handler now computes
-- ('sit:composition:<analyst_id>:<target_id|world>' — derive_signature wraps the
-- stamped raw signature in the explicit 'sit:' prefix). The cluster key encodes
-- target_id (per-country target = the country; the world head's target_id is NULL
-- -> the 'world' literal) so one analyst's per-country compositions never collapse
-- into a single head. It also mirrors the live path's finding_supersessions link
-- row so the audit trail matches a live supersession.
--
-- APPEND-ONLY: never DELETEs a finding row — both the superseded and the
-- superseding rows are preserved (the read API's superseded_by IS NULL filter
-- surfaces the one canonical head).
--
-- Composition rows are identified as kind='finding' + meta=true (every
-- meta_findings_synthesizer output) + a present data.citations array (only the
-- CITE-running compositions carry it — this excludes the legacy un-cited global
-- meta and first-order unit findings). Honest-empty composition heads (no
-- citations key) are left alone; the forward code stamps them going forward.
--
-- IDEMPOTENT: re-run is a no-op. The supersede UPDATE is guarded by
-- superseded_by IS NULL (already-folded rows skip); the head signature UPDATE by
-- IS DISTINCT FROM (already-stamped rows skip); the link INSERT by ON CONFLICT
-- DO NOTHING. On a fresh cold-start substrate (no composition rows) every
-- statement matches 0 rows -> a clean no-op.

-- (1) Stamp the situation_signature on EVERY member of every composition cluster
--     (heads included) so the live supersession/situation-clustering reads key on
--     the same signature the forward code computes.
WITH ranked AS (
    SELECT id,
           'sit:composition:'
             || COALESCE(analyst_id, 'unknown') || ':'
             || COALESCE(target_id, 'world') AS sig
    FROM analyst_outputs
    WHERE kind = 'finding'
      AND (data -> 'data' ->> 'meta') = 'true'
      AND (data -> 'data' ? 'citations')
)
UPDATE analyst_outputs ao
SET situation_signature = r.sig
FROM ranked r
WHERE ao.id = r.id
  AND ao.situation_signature IS DISTINCT FROM r.sig;

-- (2) Supersede all-but-the-newest head in each (analyst_id, target_id) cluster:
--     point the older rows at the newest (head_id) and stamp superseded_at. The
--     superseded_by IS NULL guard makes a re-run a no-op.
WITH ranked AS (
    SELECT id,
           FIRST_VALUE(id) OVER w AS head_id,
           ROW_NUMBER()    OVER w AS rn
    FROM analyst_outputs
    WHERE kind = 'finding'
      AND (data -> 'data' ->> 'meta') = 'true'
      AND (data -> 'data' ? 'citations')
    WINDOW w AS (
        PARTITION BY analyst_id, target_id
        ORDER BY produced_at DESC, id DESC
    )
)
UPDATE analyst_outputs ao
SET superseded_by = r.head_id,
    superseded_at = COALESCE(ao.superseded_at, now())
FROM ranked r
WHERE ao.id = r.id
  AND r.rn > 1
  AND ao.superseded_by IS NULL;

-- (3) Mirror the live path's finding_supersessions link row (older -> newest)
--     for the audit trail. reason='situation_id' matches what the live handler
--     records for a 'sit:'-prefixed signature. Idempotent via the pkey conflict.
WITH ranked AS (
    SELECT id,
           analyst_id,
           target_id,
           FIRST_VALUE(id) OVER w AS head_id,
           ROW_NUMBER()    OVER w AS rn
    FROM analyst_outputs
    WHERE kind = 'finding'
      AND (data -> 'data' ->> 'meta') = 'true'
      AND (data -> 'data' ? 'citations')
    WINDOW w AS (
        PARTITION BY analyst_id, target_id
        ORDER BY produced_at DESC, id DESC
    )
)
INSERT INTO finding_supersessions
    (superseded_finding_id, superseding_finding_id,
     situation_signature, reason, score, produced_by)
SELECT r.id,
       r.head_id,
       'sit:composition:'
         || COALESCE(r.analyst_id, 'unknown') || ':'
         || COALESCE(r.target_id, 'world'),
       'situation_id',
       1.0,
       'migration_0058'
FROM ranked r
WHERE r.rn > 1
ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING;
