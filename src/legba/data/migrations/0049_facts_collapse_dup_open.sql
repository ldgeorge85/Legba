-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0049_facts_collapse_dup_open.sql
--
-- D17 — collapse duplicate OPEN rows per standing triple (close-not-delete).
--
-- WHY:
--   Full-triple fact supersession leaked open duplicates: each analyst cycle
--   re-asserts a standing triple with a fresh `valid_from`, so the open-triple
--   unique index (lower(subject),lower(predicate),lower(value),COALESCE(valid_
--   from,...)) does NOT collide — a new `valid_from` is a new key — and the
--   substrate accumulated >1 OPEN row (valid_until IS NULL AND superseded_by IS
--   NULL) for the SAME (lower-subject, lower-predicate, lower-value) standing
--   fact. Every confidence-ordered / latest-value consumer then sees N copies
--   of one fact. The FORWARD fix lands in writes.py via the `collapse_open_
--   triple` helper called from both `_insert_fact` and `_insert_ingestion_fact`
--   (one open row per standing triple); this migration drains the EXISTING
--   backlog of multi-open groups produced before that gate went live.
--
-- WHAT (close-not-delete; per (lower(subject),lower(predicate),lower(value))
-- group with >1 open row, KEEP the earliest-`valid_from` row and CLOSE the
-- rest):
--   * Keeper = the group's MIN(valid_from) row (NULLs ordered last so a real
--     timestamp always wins a NULL); ties on valid_from broken by MIN(id) so
--     the keeper is deterministic and a re-run picks the same survivor.
--   * Fold-up onto the keeper FIRST (before closing the losers, while they are
--     still open and selectable): confidence = MAX(confidence) over the group,
--     derived_from = the unioned distinct UUIDs across the group, valid_from =
--     LEAST(valid_from) over the group (i.e. the keeper's own min, a no-op for
--     the keeper but makes the intent explicit and the step order-independent).
--   * Then CLOSE every non-keeper open row in the group: valid_until = NOW(),
--     superseded_by = <keeper id>. No row is deleted.
--
-- SAFETY (idempotent, transactional, data-only):
--   * Re-run is a no-op: once a group has exactly ONE open row the >1 filter
--     matches nothing, so neither the fold nor the close touches anything.
--   * Closing the losers BEFORE the index can be violated is safe because the
--     keeper's (subject,predicate,value,valid_from) key is unchanged — we never
--     mutate the keeper's triple/valid_from in a way that collides with a still-
--     open sibling (the siblings are closed in the same statement set, and the
--     keeper already owns the surviving open key).
--   * Single timestamp: NOW() is evaluated per-statement; the close statement
--     stamps every loser in the group with one transaction clock value.

-- The set of open triples that have MORE THAN ONE open row, with the chosen
-- keeper id and the folded aggregates. Materialized once and reused by both the
-- keeper-fold UPDATE and the loser-close UPDATE so they see the SAME keeper.
WITH open_facts AS (
    SELECT
        f.id,
        lower(f.subject)   AS s,
        lower(f.predicate) AS p,
        lower(f.value)     AS v,
        f.valid_from,
        f.confidence,
        f.derived_from
    FROM facts f
    WHERE f.valid_until IS NULL
      AND f.superseded_by IS NULL
),
groups AS (
    SELECT s, p, v
    FROM open_facts
    GROUP BY s, p, v
    HAVING count(*) > 1
),
ranked AS (
    SELECT
        of.id,
        of.s, of.p, of.v,
        -- earliest valid_from is the keeper; NULL valid_from sorts LAST so a
        -- real timestamp always beats an unknown one. Deterministic tie-break
        -- on id keeps the survivor stable across re-runs.
        row_number() OVER (
            PARTITION BY of.s, of.p, of.v
            ORDER BY of.valid_from ASC NULLS LAST, of.id ASC
        ) AS rn
    FROM open_facts of
    JOIN groups g USING (s, p, v)
),
keepers AS (
    SELECT id AS keeper_id, s, p, v
    FROM ranked
    WHERE rn = 1
),
agg AS (
    -- Folded aggregates over the WHOLE group (keeper + losers), computed while
    -- every member is still open.
    SELECT
        of.s, of.p, of.v,
        max(of.confidence) AS max_conf,
        min(of.valid_from) AS min_valid_from,
        (
            SELECT array_agg(DISTINCT elem)
            FROM open_facts of2
            CROSS JOIN LATERAL unnest(of2.derived_from) AS u(elem)
            WHERE of2.s = of.s AND of2.p = of.p AND of2.v = of.v
        ) AS union_derived_from
    FROM open_facts of
    JOIN groups g USING (s, p, v)
    GROUP BY of.s, of.p, of.v
)
-- Step 1: fold the group aggregates onto the keeper row.
UPDATE facts f
   SET confidence   = a.max_conf,
       valid_from   = a.min_valid_from,
       derived_from = COALESCE(a.union_derived_from, '{}'::uuid[]),
       updated_at   = NOW()
  FROM keepers k
  JOIN agg a ON a.s = k.s AND a.p = k.p AND a.v = k.v
 WHERE f.id = k.keeper_id;

-- Step 2: close every non-keeper open row in a multi-open group, pointing
-- superseded_by at the keeper. Re-derives the same groups/keepers as Step 1 so
-- the migration is one self-contained pass; the keeper is excluded by id.
WITH open_facts AS (
    SELECT
        f.id,
        lower(f.subject)   AS s,
        lower(f.predicate) AS p,
        lower(f.value)     AS v,
        f.valid_from
    FROM facts f
    WHERE f.valid_until IS NULL
      AND f.superseded_by IS NULL
),
groups AS (
    SELECT s, p, v
    FROM open_facts
    GROUP BY s, p, v
    HAVING count(*) > 1
),
ranked AS (
    SELECT
        of.id,
        of.s, of.p, of.v,
        row_number() OVER (
            PARTITION BY of.s, of.p, of.v
            ORDER BY of.valid_from ASC NULLS LAST, of.id ASC
        ) AS rn
    FROM open_facts of
    JOIN groups g USING (s, p, v)
),
keepers AS (
    SELECT id AS keeper_id, s, p, v
    FROM ranked
    WHERE rn = 1
)
UPDATE facts f
   SET valid_until   = NOW(),
       superseded_by = k.keeper_id,
       updated_at    = NOW()
  FROM ranked r
  JOIN keepers k ON k.s = r.s AND k.p = r.p AND k.v = r.v
 WHERE f.id = r.id
   AND r.rn > 1                       -- losers only
   AND f.valid_until IS NULL          -- still open (idempotent guard)
   AND f.superseded_by IS NULL;
