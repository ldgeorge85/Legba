-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0073_align_resolved_active_hypotheses.sql  (DQ Phase 6 / situations-hypotheses
--   finding "hypotheses/resolution circularity")
--
-- PROBLEM: the ONE exogenous resolver (competing_hypotheses'
--   _resolve_hypotheses_against_subsequent_facts) stamped resolved_outcome +
--   resolved_at + resolved_by='subsequent_facts' but LEFT `status='active'`. So
--   its 87 rows are in an inconsistent resolved-but-active state — they DOUBLE-COUNT
--   in both the active test pool AND the resolved pool. (Separately, the intensity-
--   drift self-consistency resolver produced the terminal confirmed/refuted rows;
--   those are correctly segregated by calibration_tracking and are NOT in scope.)
--
-- PAIRED CODE FIX (keeps it out): the subsequent_facts writer now sets
--   `status = CASE WHEN resolved_outcome=1 THEN 'confirmed' ELSE 'refuted' END` in
--   the SAME UPDATE that stamps resolved_outcome, so a newly-resolved row leaves
--   the active/working pool. Independently, hypothesis_lifecycle no longer walks a
--   hypothesis to a TERMINAL confirmed/refuted from intensity drift alone (it caps
--   at the working states supported/weakened), reserving the terminal states for
--   the exogenous subsequent_facts resolver / operator — closing the circularity.
--
-- THIS MIGRATION aligns the 87 already-resolved-but-active rows: sets
--   `status` from `resolved_outcome` (1 -> 'confirmed', 0 -> 'refuted') so they
--   stop double-counting. Scope = `resolved_by='subsequent_facts' AND status='active'
--   AND resolved_outcome IS NOT NULL`. Prior status ('active' for all 87) is
--   recorded as an audit marker appended to `diagnostic_evidence` for the reverse.
--
-- REVERSIBLE:
--   UPDATE hypotheses
--      SET status = 'active', updated_at = now()
--    WHERE resolved_by = 'subsequent_facts'
--      AND status IN ('confirmed','refuted')
--      AND diagnostic_evidence @> '[{"dq_p6_status_align": true}]'::jsonb;
--   (then optionally strip the marker from diagnostic_evidence).
-- IDEMPOTENT: the `status='active'` scope + the marker guard mean a re-run matches
--   nothing once applied; NO row is deleted, resolved_outcome is untouched. Routed
--   through the migration runner (ONE txn + ledger; NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 87 rows matched — 75 resolved_outcome=0
--   (-> 'refuted'), 12 resolved_outcome=1 (-> 'confirmed'); all resolved_by=
--   'subsequent_facts', all prior status='active'.

UPDATE hypotheses h
SET status = CASE WHEN h.resolved_outcome = 1 THEN 'confirmed' ELSE 'refuted' END,
    diagnostic_evidence = COALESCE(h.diagnostic_evidence, '[]'::jsonb)
        || jsonb_build_object(
               'dq_p6_status_align', true,
               'prior_status', h.status,
               'aligned_from_resolved_outcome', h.resolved_outcome
           ),
    updated_at = now()
WHERE h.resolved_by = 'subsequent_facts'
  AND h.status = 'active'
  AND h.resolved_outcome IS NOT NULL
  AND NOT (COALESCE(h.diagnostic_evidence, '[]'::jsonb) @> '[{"dq_p6_status_align": true}]'::jsonb);
