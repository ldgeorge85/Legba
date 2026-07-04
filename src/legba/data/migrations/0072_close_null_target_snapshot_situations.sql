-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0072_close_null_target_snapshot_situations.sql  (DQ Phase 6 /
--   situations-hypotheses findings "snapshot-as-situation pollution" +
--   "duplication")
--
-- PROBLEM: assessment REPORTS materialize as situations. country_composition /
--   region_composition / escalation_composition / world_assessor findings got a
--   situation_signature from finding_supersession (which stamped ALL kind='finding'
--   rows with no analyst-kind gate) and situation_clustering then minted a
--   "situation" per report stream, NAMED after the report title — including exact-
--   name-duplicate "Composite Assessment" rows, dated "World situational assessment
--   - <date>" rows, and one row whose name was a raw JSON-envelope fragment
--   ('"title": "World situational assessment - 2026-06-30",' — the #125 parse-
--   fallback class). These NULL-target rows never ground per-country (grounding
--   scopes on target_id equality) yet compete in the GLOBAL intensity ranking and
--   pollute the /situations product surface. Every covered theater also carried 2+
--   frames (the June unit mega-frame + the July composite snapshot).
--
-- PAIRED CODE FIX (keeps it out): finding_supersession + situation_clustering now
--   EXCLUDE the composition/meta analysts (country_composition, region_composition,
--   escalation_composition, world_assessor) from signature stamping AND from
--   clustering materialization (_COMPOSITION_ANALYST_IDS gate), so no NEW report
--   receipt mints a situation; situation_clustering ALSO rejects a dated-snapshot /
--   leaked-JSON title at naming time. Because the composition signatures no longer
--   re-enter the member pool, these closed rows are NOT re-touched (the close is
--   durable, not overwritten next tick).
--
-- THIS MIGRATION closes (status='closed'; valid_until=now(); NO delete) the
--   NULL-target ACTIVE report-receipt rows. Scope = `target_id IS NULL AND
--   status='active'` — the EXACT, reviewer-verifiable discriminator: a legitimate
--   per-desk unit frame ALWAYS carries a country target_id (situation_clustering's
--   _target_for_category populates it from a country-slug category), so every
--   per-desk frame is target_id IS NOT NULL and is NEVER in scope here. Verified
--   live: 0 of the in-scope rows have a country_* target_id.
--
-- REVERSIBLE:
--   UPDATE situations
--      SET status = data->>'dq_p6_prior_status',
--          valid_until = NULLIF(data->>'dq_p6_prior_valid_until','')::timestamptz,
--          data = data - 'dq_p6_prior_status' - 'dq_p6_prior_valid_until',
--          updated_at = now()
--    WHERE data ? 'dq_p6_prior_status' AND status = 'closed' AND target_id IS NULL;
-- IDEMPOTENT: the `NOT (data ? 'dq_p6_prior_status')` guard skips already-closed
--   rows; NO row is deleted. Routed through the migration runner (ONE txn + ledger;
--   NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 35 NULL-target active rows matched; 0 carry
--   a country_* target_id (no per-desk frame closed). Samples: 99ed6b1e-class
--   "United Kingdom - Composite Assessment", 4599f120 "China - Overall Strategic
--   Composition Assessment", 265df140 "Canada - Overall Stability Assessment",
--   0ec6e022 "Global Composition Indicates Broad Low Risk …", f0e07333 "Iran -
--   Maritime/Diplomatic Tension" (escalation_composition receipt), 89e6a2b4 "Iran -
--   Low near-term leadership transition risk" (severity:low receipt).

UPDATE situations s
SET data = jsonb_set(
        jsonb_set(
            COALESCE(s.data, '{}'::jsonb),
            '{dq_p6_prior_status}', to_jsonb(s.status), true
        ),
        '{dq_p6_prior_valid_until}',
        to_jsonb(COALESCE(s.valid_until::text, '')),
        true
    ),
    status = 'closed',
    valid_until = now(),
    updated_at = now()
WHERE s.status = 'active'
  AND s.target_id IS NULL
  AND NOT (COALESCE(s.data, '{}'::jsonb) ? 'dq_p6_prior_status');
