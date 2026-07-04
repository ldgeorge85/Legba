-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0071_dormant_steady_state_situations.sql  (DQ Phase 6 / situations-hypotheses
--   finding "situations/non-event pollution")
--
-- PROBLEM: the situations intensity_score is recency-weighted fold-count, so a
--   status-quo per-desk frame that absorbs every routine no-change finding
--   becomes the MOST intense open frame platform-wide — and the global grounding
--   block (journal top-8 by intensity) and the /situations surface were headed by
--   a NON-event (e.g. "United States - No observable WMD proliferation activity",
--   "Argentina - No observable standing military posture shift"). The legacy
--   non-event name filter anchored only on names STARTING with "no" (extinct in
--   the table), so 0 of the live mid-string status-quo frames were caught.
--
-- PAIRED CODE FIX (keeps it out): grounding.py `_NON_EVENT_SITUATION_RE` broadened
--   to the mid-string negation / status-quo shapes (no observable/discernible/…,
--   status quo, stability maintained, low <near-term|overall|…> risk) so these
--   frames are dropped from BOTH the per-country and the global grounding read;
--   situation_clustering stamps an authoritative `data.steady_state` flag at
--   materialization using the SAME shared predicate.
--
-- THIS MIGRATION retags the currently steady-state-NAMED, TARGET-BEARING (per-desk)
--   ACTIVE frames to status='dormant' so the /situations surface + the global
--   intensity ranking read clean IMMEDIATELY (the read-side regex already achieves
--   the grounding effect; this is the surface cleanup). Scope predicate = the SQL
--   mirror of the code regex, restricted to `target_id IS NOT NULL` so a legit
--   per-desk container is only DORMANT-ed (it re-opens to active the moment a real
--   event lands and its latest-member name is no longer a non-event). NULL-target
--   snapshot receipts are handled by 0072, not here.
--
-- NOTE (transience, honest): situation_clustering recomputes `status` from
--   last_event_at every run, so a dormant-ed frame with a fresh member re-opens on
--   the next clustering tick — this retag is an IMMEDIATE-SURFACE cleanup, the
--   durable fix is the code regex + steady_state tag. Prior status is stashed in
--   `data.dq_p6_prior_status` for the reverse.
--
-- REVERSIBLE:
--   UPDATE situations
--      SET status = data->>'dq_p6_prior_status',
--          data   = data - 'dq_p6_prior_status',
--          updated_at = now()
--    WHERE data ? 'dq_p6_prior_status' AND status = 'dormant';
--   (Or simply let the next situation_clustering run recompute status.)
-- IDEMPOTENT: the `NOT (data ? 'dq_p6_prior_status')` guard skips already-retagged
--   rows; NO row is deleted. Routed through the migration runner (ONE txn + ledger;
--   NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 14-17 target-bearing active steady-state
--   frames matched at authoring time (names churn run-to-run; the predicate, not a
--   frozen id list, is authoritative). Samples: 3c29a8cd "United States - No
--   observable WMD proliferation activity", 41d9a783 "Russia - No observable WMD
--   proliferation activity", d80ab64f "No coordinated narrative detected - organic
--   heat-wave and policy coverage", 328eb202 "Iran - No observable WMD activity;
--   trajectory holding". 0 NULL-target rows touched (those are 0072's scope).
--
-- DQ P6 r2: the regex above was TIGHTENED in lockstep with grounding.py — the
--   "No <qualifier>" branch is now anchored (segment boundary + trailing static
--   observation noun) so a real event mentioning "no significant …" mid-sentence is
--   not retagged, and 'clear'/'evident' were added so the live "No clear standing
--   military posture shift" frames (Japan/Saudi Arabia) are caught. Re-measured live
--   (rolled-back txn): the whole matched set is genuine steady-state (0 real events);
--   idempotent second pass = 0 rows; reverse restores the baseline.

UPDATE situations s
SET data = jsonb_set(
        COALESCE(s.data, '{}'::jsonb),
        '{dq_p6_prior_status}',
        to_jsonb(s.status),
        true
    ),
    status = 'dormant',
    updated_at = now()
WHERE s.status = 'active'
  AND s.target_id IS NOT NULL
  AND NOT (COALESCE(s.data, '{}'::jsonb) ? 'dq_p6_prior_status')
  -- POSIX (`~*`) mirror of grounding.py `_NON_EVENT_SITUATION_RE` (keep in lockstep):
  -- the "No <qualifier>" branch is anchored to a name-segment boundary (start or a
  -- desk separator –/—/-/:/'(') AND requires a trailing static observation noun in
  -- the SAME segment (stopped at '.'/';'), so a real event that merely mentions
  -- "no significant …" mid-sentence, or "No significant de-escalation; airstrikes
  -- intensify …" (post-qualifier word = the CHANGE noun "de-escalation"), is NOT
  -- retagged; 'clear'/'evident' added so "No clear standing military posture shift"
  -- (Japan/Saudi) is caught.
  AND s.name ~* '(^\s*no\y.*(in the latest batch|[- ]specific|alerts?))|((^|[–—:(-])\s*no\s+(dominant|observable|discernible|significant|coordinated|credible|material|notable|meaningful|apparent|clear|evident)\y[^.;]*?\y(activity|activities|shift|shifts|posture|pressure|signal|signals|narrative|narratives|detected|observed|vector|vectors|instability|instabilities|movement|movements|buildup|buildups|mobilization|maneuver|maneuvers|indication|indications|deployment|deployments|incident|incidents|unrest|anomaly|anomalies)\y)|(\ystatus\s+quo\y)|(\ystability\s+maintained\y)|(\ylow\s+(near[-\s]?term|multi[-\s]?domain|overall|leadership\s+transition)\y[^.]{0,24}\yrisk\y)';
