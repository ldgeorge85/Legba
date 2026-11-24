-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- SW-10 (wave W1) — ONE-OFF: close the inquiry hypotheses that can never resolve.
--
-- WHY. Until SW-10 the inquiry ledger minted a hypothesis with no `resolves_by`
-- horizon (the LEDGER prompt called it optional; `ledger_write` accepted NULL).
-- Every hypothesis the two inquiries (`crossroads`, `inquiry_pilot`) opened from
-- 2026-09-24 carries `resolves_by` NULL, none is dispatched, none was ever
-- closed — so the weekly `inquiry_yield` can never score one, and the inquiries'
-- PLAN prompt carries an ever-growing pile of un-settleable rows. SW-10 makes
-- such a row un-mintable (plan_ledger_writes + ledger_write both refuse it as
-- `hypothesis_refused_no_resolves_by`); this script settles the rows that were
-- minted before that rule rolled.
--
-- WHAT. Every OPEN `hypothesis` row with `resolves_by IS NULL` is closed as
-- `withdrawn`, the status vocabulary's nearest to "void, no horizon" (migration
-- 0215's `inquiry_ledger_status_vocab` has no `void_no_horizon`, and this lane
-- adds no migration). NOT `expired`: the yield counts an in-window `expired`
-- close as a scored hypothesis, and these were never scoreable — 49 fake
-- expiries would be the instrument lying about the week. `withdrawn` is not a
-- yield counter. The close is stamped exactly the way `ledger_close` stamps one
-- (inquiry_state.py): `closed_at = now()`, `updated_at = now()`, and the reason
-- appended to the row's own `text` as "\n\n[closed -> withdrawn] <reason>" —
-- the ledger has no separate reason column. `cited_refs` is left as opened.
--
-- WHAT IT DOES NOT TOUCH. Questions and observations: the horizon rule binds
-- hypotheses only, both have a real close path (`answered`; an observation
-- closed with cited_refs is what the yield's "anticipated" counter reads), and
-- voiding them would only re-accumulate. They are counted below for the record.
--
-- IDEMPOTENT. The predicate is `status = 'open' AND kind = 'hypothesis' AND
-- resolves_by IS NULL`; a second run closes zero rows. Run it AFTER the SW-10
-- roll — once the rule is live no new row can match, so the predicate needs no
-- date cutoff. One transaction; the before/after counts print either way.
--
-- RUN IT (a live write — the orchestrator's, not a lane's):
--   docker exec -i legba-postgres-1 psql -U legba -d legba -v ON_ERROR_STOP=1 \
--     < scripts/void_inquiry_hypotheses_no_horizon.sql
--
-- Live counts when written (2026-10-03 ~13Z, read-only): 49 open hypotheses with
-- resolves_by NULL (crossroads 15, inquiry_pilot 34), plus 29 open questions and
-- 19 open observations that this script leaves alone.

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL statement_timeout = '120s';

-- BEFORE: the open ledger by descriptor / kind / horizon.
SELECT 'before' AS phase, descriptor_id, kind,
       (resolves_by IS NULL) AS no_horizon, count(*) AS open_rows
  FROM inquiry_ledger
 WHERE status = 'open'
 GROUP BY descriptor_id, kind, (resolves_by IS NULL)
 ORDER BY descriptor_id, kind, no_horizon;

WITH voided AS (
    UPDATE inquiry_ledger
       SET status = 'withdrawn',
           text = text || E'\n\n[closed -> withdrawn] void_no_horizon: minted '
                       || 'with no resolves_by horizon before the horizon rule '
                       || '(SW-10, 2026-10) — it could never be scored, so it is '
                       || 'withdrawn rather than carried open forever.',
           closed_at = now(),
           updated_at = now()
     WHERE status = 'open'
       AND kind = 'hypothesis'
       AND resolves_by IS NULL
    RETURNING descriptor_id
)
SELECT 'voided' AS phase, descriptor_id, count(*) AS rows_closed
  FROM voided
 GROUP BY descriptor_id
 ORDER BY descriptor_id;

-- AFTER: must show no open hypothesis with no_horizon = t.
SELECT 'after' AS phase, descriptor_id, kind,
       (resolves_by IS NULL) AS no_horizon, count(*) AS open_rows
  FROM inquiry_ledger
 WHERE status = 'open'
 GROUP BY descriptor_id, kind, (resolves_by IS NULL)
 ORDER BY descriptor_id, kind, no_horizon;

COMMIT;
