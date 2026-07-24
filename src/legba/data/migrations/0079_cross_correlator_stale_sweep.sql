-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0079_cross_correlator_stale_sweep.sql  (live audit 2026-07-06, finding M17
--   "cross_correlator never supersedes -> ~32 stale live heads")
--
-- PROBLEM: the cross_analyst_correlator (kind cross_analyst_correlator) never
--   superseded — situation_signature NULL on every row, superseded_by NULL on
--   every row — so a meta-observation head accumulated PER CYCLE. 39 live heads
--   piled up (30/39 blind_spot meta-observations), including a now-FALSE 0.86
--   blind_spot a7fc52d7 "No dedicated Iran assessment despite multiple references"
--   (2026-07-01) that the repointed, live-layer-reading correlator no longer emits.
--
-- PAIRED CODE FIX (this commit): the kind now derives a stable
--   data['situation_signature'] = 'xcorr:<correlation_type>:<sorted target set>'
--   and the WRITE path folds prior same-signature heads + decays stale blind_spots
--   SYNCHRONOUSLY (fold_prior_correlation_heads, mirroring the FU6 composition
--   fold) — so GOING FORWARD the heads supersede cleanly. This migration folds the
--   BACKLOG that accumulated before that fix (the pre-code heads carry no signature,
--   so the live fold cannot key on them).
--
-- THIS MIGRATION (extends the 0074 world-head supersession pattern):
--   Close every cross_correlator live FINDING head OLDER than the blind_spot decay
--   TTL (72h = 3 days, the SAME cutoff the going-forward decay uses) as
--   superseded_by the CURRENT (newest) live cross_correlator head, stashing the
--   prior superseded_by in data->'_m0079_prior_superseded_by' for reversibility and
--   mirroring a finding_supersessions edge (reason='cross_correlator stale-head
--   sweep (M17)', produced_by='migration_0079'). RECENT heads (<=3 days) are HELD —
--   they are the correlator's current view and the going-forward fold will maintain
--   them once the paired code is live.
--
-- SCOPE is analyst_id='cross_correlator' ONLY — the blast radius is one analyst.
--   The current head is resolved LIVE at apply (drift-robust): the most-recent live
--   cross_correlator finding, which is <=3 days old (HELD) and so is never itself a
--   swept row (the `produced_at < NOW() - INTERVAL '3 days'` predicate + the
--   `o.id <> h.id` belt both exclude it).
--
-- REVERSIBLE (NO row deleted — only close/annotate):
--   UPDATE analyst_outputs o
--      SET superseded_by = NULLIF(o.data->>'_m0079_prior_superseded_by','')::uuid,
--          superseded_at = NULL,
--          data = o.data - '_m0079_prior_superseded_by'
--    WHERE o.data ? '_m0079_prior_superseded_by';
--   DELETE FROM finding_supersessions WHERE produced_by = 'migration_0079';
--
-- IDEMPOTENT: the UPDATE skips an already-folded row (superseded_by IS NULL + no
--   stash key), the edge INSERT is ON CONFLICT DO NOTHING, and a re-run finds the
--   remaining live heads either <=3 days (held) or already swept. On a fresh
--   substrate (no cross_correlator rows, or <2 live heads) every statement matches
--   0 rows — a clean no-op. Routed through the migration runner (ONE txn + ledger;
--   NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-06, migration head 0078):
--   39 live cross_correlator heads; 33 OLDER than 3 days (SWEPT) -> superseded_by
--   the current head 7fc5f5ee (blind_spot, 2026-07-06 00:45Z); 6 HELD (<=3 days):
--   7fc5f5ee, af9a1c8d (Asia-Pacific gap), 1b138a51 (divergent escalation risk),
--   3ec3acd9 (economic coercion gap), 8c6b545f (UK near-term gap), 1b45cb73
--   (European energy-security gap). 20-row SWEPT sample (newest-first): 0906f720,
--   190d412b, 4253bb45, 4dd8f98c, a7fc52d7 (the false-Iran head), e19e69d4,
--   a2677df5, 95d24b20, e2e41d5b, 3bc10070, 960a16cf, 5e1ae735, 4faa2a73, 1e1e77db,
--   75ba67fa, d9ce8ed5, 5da761d0, d269354e, 947b1286, ad140442.
--   HELD CASES: the 6 <=3-day heads above (still the correlator's current view;
--   the paired write-fold maintains them going forward).

-- Close every stale (>3-day) cross_correlator live head as superseded_by the
-- current (newest) live head (resolved LIVE), stashing the prior superseded_by.
WITH head AS (
    SELECT id
    FROM analyst_outputs
    WHERE analyst_id = 'cross_correlator'
      AND kind = 'finding'
      AND superseded_by IS NULL
    ORDER BY produced_at DESC, id DESC
    LIMIT 1
)
UPDATE analyst_outputs o
SET superseded_by = h.id,
    superseded_at = COALESCE(o.superseded_at, now()),
    data = jsonb_set(
        COALESCE(o.data, '{}'::jsonb),
        '{_m0079_prior_superseded_by}',
        to_jsonb(COALESCE(o.superseded_by::text, '')), true
    )
FROM head h
WHERE o.analyst_id = 'cross_correlator'
  AND o.kind = 'finding'
  AND o.superseded_by IS NULL
  AND o.id <> h.id
  AND o.produced_at < NOW() - INTERVAL '3 days'
  AND NOT (COALESCE(o.data, '{}'::jsonb) ? '_m0079_prior_superseded_by');

-- Mirror the live fold's finding_supersessions audit edge for each swept row.
-- Each swept row already carries its superseding head in o.superseded_by (set by
-- the UPDATE above), so the edge is derived directly from the row — no CTE needed.
INSERT INTO finding_supersessions
    (superseded_finding_id, superseding_finding_id,
     situation_signature, reason, score, produced_by)
SELECT o.id, o.superseded_by,
       'sit:xcorr:stale_sweep',
       'cross_correlator stale-head sweep (M17)',
       1.0,
       'migration_0079'
FROM analyst_outputs o
WHERE o.analyst_id = 'cross_correlator'
  AND o.kind = 'finding'
  AND (o.data ? '_m0079_prior_superseded_by')
  AND o.superseded_by IS NOT NULL
ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING;
