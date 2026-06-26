-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0052_remediation_data_cleanup.sql
--
-- One-time data cleanup from the 2026-06-26 platform-health remediation. Both
-- legs are pure backlog drains whose FORWARD gates are already live (W2/W4):
--
--   * D29 — prune the legacy bookkeeping FINDING backlog. cross_source_dedup +
--     entity_resolution emitted a kind='finding' receipt per run as housekeeping;
--     the force_trace_only fix (pre-2026-06-19) stopped that (0 emitted since),
--     but ~17k pre-fix rows remain and dominate the operator feed (~84% noise).
--     They are leaf bookkeeping rows (nothing derives_from them — verified).
--   * D31 — close the 2 wrong US<->Israel "in active conflict" SEED nexuses. The
--     corrected coalition seed adapter (sides-aware: US+Israel allied vs Iran) no
--     longer creates them; this closes the existing open pair so grounding stops
--     treating Israel as antagonistic to the US.
--
-- IDEMPOTENT: after the first run the bookkeeping findings are gone (re-run deletes
-- 0) and the wrong nexuses are closed (valid_until set; re-run matches 0). On a
-- fresh cold-start substrate both sets are empty -> a clean no-op.
--
-- NOTE: 0052_entity_cross_class_merge.sql (D8 option-B, the 623-fragment drain) is
-- held OPERATOR-OPT-IN in planning/ and is intentionally NOT in this sequence.

-- D29: prune the legacy bookkeeping finding backlog (pre-fix housekeeping rows).
DELETE FROM analyst_outputs
WHERE kind = 'finding'
  AND analyst_id IN ('cross_source_dedup', 'entity_resolution')
  AND produced_at < TIMESTAMPTZ '2026-06-19 00:00:00+00';

-- D31: close the OPEN US<->Israel "active conflict" SEED nexuses (both directions).
UPDATE nexuses
SET valid_until = now()
WHERE valid_until IS NULL
  AND superseded_by IS NULL
  AND source_type = 'seed'
  AND rel_type ~* 'conflict'
  AND (
    (lower(subject) = 'united states' AND lower(object) = 'israel')
    OR (lower(subject) = 'israel' AND lower(object) = 'united states')
  );
