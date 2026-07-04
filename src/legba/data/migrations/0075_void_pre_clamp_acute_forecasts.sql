-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0075_void_pre_clamp_acute_forecasts.sql  (DQ Phase 7 / composition-layers +
--   acute-forecast pilot; OPERATOR-APPROVED void)
--
-- PROBLEM: the acute-forecast pilot's FIRST issued batch (issued 2026-06-24, one
--   weekly window [2026-06-29, 2026-07-06), method recent_rate_poisson) predates
--   the D9 epsilon-clamp fix — every one of its 19 rows asserts a DEGENERATE {0,1}
--   certainty (a saturated Poisson λ issued p=1.0, an empty λ issued p=0.0). A
--   degenerate forecast is a non-forecaster: a single surprise against a 1.0 call
--   is the worst possible Brier (1.0) / an infinite log-loss. When this window
--   closes (2026-07-06) the exogenous resolver would grade these rows and their
--   catastrophic Brier would poison the pilot's FIRST graded scoreboard, hiding
--   the fixed (clamped) issuer's real skill behind the known-broken batch.
--
-- PAIRED CODE FIX (already committed this phase, forecast_acute.py): (1) clamp_p
--   pulls every issued probability into [epsilon, 1-epsilon] so the producer can
--   never again assert a degenerate certainty; (2) the resolver + the scoreboard
--   pull now EXCLUDE any row whose resolved_by starts 'voided:' — so this void is
--   DURABLE: the resolver will not re-grade (and thus un-void) these rows when the
--   window closes, and the segregated pilot Brier never reads them.
--
-- THIS MIGRATION voids EXACTLY the 19 pre-clamp degenerate rows: stamps
--   resolved_by='voided:pre_clamp_degenerate' + resolved_at=now(), and LEAVES
--   resolved_outcome NULL (so the scoreboard, which grades resolved_outcome IS NOT
--   NULL, never scores them) and actual_value NULL. Scope pins ALL of the batch's
--   discriminators (issued date, method, the exact window, and the p IN (0,1)
--   degeneracy) so no clamped/future forecast is ever touched.
--
-- REVERSIBLE (NO row deleted — only annotate):
--   UPDATE acute_forecasts
--      SET resolved_by = NULL, resolved_at = NULL
--    WHERE resolved_by = 'voided:pre_clamp_degenerate'
--      AND resolved_outcome IS NULL;
--
-- IDEMPOTENT: the `resolved_by IS NULL` guard makes a re-run a no-op (voided rows
--   already carry the sentinel). NO row is deleted. Routed through the migration
--   runner (ONE txn + ledger; NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-04, migration head 0073): 19 rows match — all
--   issued 2026-06-24, method recent_rate_poisson, window [2026-06-29, 2026-07-06),
--   every p in {0,1}, all currently unresolved (resolved_by IS NULL,
--   resolved_outcome IS NULL).

UPDATE acute_forecasts
SET resolved_by = 'voided:pre_clamp_degenerate',
    resolved_at = now()
WHERE issued_at::date = DATE '2026-06-24'
  AND method = 'recent_rate_poisson'
  AND window_start = TIMESTAMPTZ '2026-06-29 00:00:00+00'
  AND window_end   = TIMESTAMPTZ '2026-07-06 00:00:00+00'
  AND p IN (0, 1)
  AND resolved_outcome IS NULL
  AND resolved_by IS NULL;
