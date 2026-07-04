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
--   NULL-target ACTIVE report-receipt rows. Scope discriminator = PROVENANCE, not a
--   bare `target_id IS NULL` (which over-caught two REAL, NULL-target UNIT frames —
--   f0e07333 "Iran – Maritime/Diplomatic Tension" from the `escalation` unit, and
--   89e6a2b4 "Iran – Low near-term leadership transition risk" from the
--   `leadership_transition` unit; a unit desk frame can legitimately carry a NULL
--   target when its category is not a country slug, e.g. 'severity:low'/'escalation').
--   A situation's member findings are the `analyst_outputs` rows sharing its
--   `situation_signature`. We close a NULL-target active frame ONLY IF:
--     (a) at least one member was produced by a COMPOSITION/meta analyst
--         (country_composition / region_composition / escalation_composition /
--         world_assessor) — i.e. it is a genuine report receipt; AND
--     (b) NO member was produced by any OTHER (unit) analyst, and none has a NULL
--         producer — so ANY frame with a unit-analyst member is SPARED.
--   This is the review-preferred provenance rule: a report-receipt situation's
--   signature is minted only from composition producers, whereas a unit frame's
--   signature is minted from its unit analyst — the two never mix in this table.
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
-- MEASURED (live `legba`, 2026-07-03, migration head 0070): 33 NULL-target active
--   composition receipts matched by the provenance predicate (down from the 35 the
--   bare `target_id IS NULL` caught); the two REAL unit frames f0e07333 (escalation
--   unit) and 89e6a2b4 (leadership_transition unit) are SPARED — each has 0
--   composition members and >0 unit members. All 33 in-scope rows carry a
--   `sit:composition:<producer>:<scope>` signature whose members are 100%
--   composition/meta producers. Samples: 99ed6b1e "United Kingdom – Composite
--   Assessment" (country_composition), 4599f120 "China – Overall Strategic
--   Composition Assessment", 265df140 "Canada – Overall Stability Assessment",
--   0ec6e022 "Global Composition Indicates Broad Low Risk …" (region_composition),
--   9b98bfad "Global Near-Term Risk Landscape …" (world_assessor).

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
  AND NOT (COALESCE(s.data, '{}'::jsonb) ? 'dq_p6_prior_status')
  AND s.situation_signature IS NOT NULL
  -- (a) a genuine report receipt: >=1 member from a composition/meta analyst
  AND EXISTS (
        SELECT 1 FROM analyst_outputs ao
         WHERE ao.situation_signature = s.situation_signature
           AND ao.analyst_id IN ('country_composition', 'region_composition',
                                  'escalation_composition', 'world_assessor')
      )
  -- (b) SPARE any frame with a unit-analyst (or unknown-producer) member
  AND NOT EXISTS (
        SELECT 1 FROM analyst_outputs ao
         WHERE ao.situation_signature = s.situation_signature
           AND (ao.analyst_id IS NULL
                OR ao.analyst_id NOT IN ('country_composition', 'region_composition',
                                         'escalation_composition', 'world_assessor'))
      );
