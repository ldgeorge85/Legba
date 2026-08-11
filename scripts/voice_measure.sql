-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- Phase-V VOICE — the diagnostic's frequency counts, as ONE re-runnable query.
--
-- VOICE_DIAGNOSTIC_2026-08-04 rests on a table of measured percentages over a
-- trailing 36h window. Those are the numbers the prompt deltas are supposed to
-- move, and the only honest way to know whether they moved is to re-run the
-- SAME counts over the SAME window shape a few days after deploy. Everything
-- the prompts changed lives in model OUTPUT, which no unit test can pin — this
-- file is that test.
--
-- RUN IT (read-only; SELECTs only, no temp tables, no writes):
--   docker exec -i legba-postgres-1 psql -U legba -d legba < scripts/voice_measure.sql
--
-- WINDOW: trailing 36h, matching the diagnostic. To widen it, edit BOTH
-- `interval '36 hours'` literals and say which window a reported number came
-- from — these are percentages of an occupancy that moves with cadence.
--
-- TWO NORMALIZATIONS, both load-bearing, both discovered by running this query
-- against the live corpus before trusting it:
--
--   * UNICODE DASHES. The core-plane model writes U+2011 NON-BREAKING HYPHEN,
--     not ASCII '-': the live prose is "most plausible near‑term trajectory".
--     An ASCII-hyphen regex scores that phrase at 0/1412 — a clean sweep that
--     is entirely an artifact. Every phrase metric below runs against `nbody`,
--     which folds U+2010..U+2015 and U+2212 to '-'.
--   * MARKDOWN BOLD. The same sentence renders as "the dominant **escalation
--     vector**", so `dominant\s+[a-z-]+\s+vector` misses it. `nbody` strips
--     asterisks. STRUCTURE metrics (## headers, the *As of* line) deliberately
--     use the RAW body, because there the markup IS the thing measured.
--
-- BASELINE: the `baseline_pre_deploy` column is THIS query's own reading taken
-- 2026-08-04, before the Phase-V deploy — not the figures in the diagnostic
-- report. The report's numbers came from a differently-shaped ad-hoc query over
-- a different 36h window and are NOT comparable row-for-row; comparing this
-- query to itself is. Direction of the win is in the metric name: rows tagged
-- (UP) should rise, (DOWN) should fall.

WITH units AS (
    SELECT id, title, body, confidence, analyst_id,
           regexp_replace(
               translate(body, U&'\2010\2011\2012\2013\2014\2015\2212', '-------'),
               '\*', '', 'g') AS nbody
      FROM analyst_outputs
     WHERE produced_at > now() - interval '36 hours'
       AND analyst_id IN (
             'escalation', 'energy_security', 'economic_coercion',
             'internal_stability', 'military_posture', 'proliferation_watch',
             'leadership_transition', 'narrative_coordination')
),
-- The BLUF line, isolated from the normalized body (units emit `**BLUF:**`,
-- `**BLUF**` and bare `BLUF:`; all three are caught). A finding with no BLUF
-- line yields NULL rather than silently scoring the whole body.
u AS (
    SELECT *,
           substring(nbody from '(?in)^[^\n]*BLUF[^\n]*') AS bluf
      FROM units
),
-- One calendar-date grammar, shared by every date metric: "2 August",
-- "Aug 2", "13 December 2026", "2026-08-03".
ud AS (
    SELECT *,
           nbody ~* '(\y\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\y|\y(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\y|\d{4}-\d{2}-\d{2})'
               AS body_dated,
           bluf ~* '(\y\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\y|\y(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\y|\d{4}-\d{2}-\d{2})'
               AS bluf_dated
      FROM u
),
comps AS (
    SELECT id, title, body, analyst_id,
           regexp_replace(
               translate(body, U&'\2010\2011\2012\2013\2014\2015\2212', '-------'),
               '\*', '', 'g') AS nbody
      FROM analyst_outputs
     WHERE produced_at > now() - interval '36 hours'
       AND analyst_id IN (
             'country_composition', 'region_composition',
             'world_assessor', 'escalation_composition')
),
unit_metrics(ord, metric, n, of, baseline) AS (
    SELECT 1, 'UNIT: body carries any calendar date (UP)',
           count(*) FILTER (WHERE body_dated), count(*), '6.5%' FROM ud
    UNION ALL SELECT 2, 'UNIT: BLUF carries a calendar date (UP)',
           count(*) FILTER (WHERE bluf_dated), count(*), '0.1%' FROM ud
    UNION ALL SELECT 3, 'UNIT: body opens with an *As of ...* line (UP, new)',
           count(*) FILTER (WHERE body ~* '(?n)^\s*\*as of\y'), count(*), '0.0%' FROM ud
    UNION ALL SELECT 4, 'UNIT: BLUF uses undated deixis (DOWN)',
           count(*) FILTER (WHERE bluf ~* '\y(currently|now|remains|continues)\y'),
           count(*), '18.6%' FROM ud
    UNION ALL SELECT 5, 'UNIT: echoes "most plausible near-term trajectory" (DOWN)',
           count(*) FILTER (WHERE nbody ~* 'most plausible near-term trajectory'),
           count(*), '22.7%' FROM ud
    UNION ALL SELECT 6, 'UNIT: echoes "the dominant ... vector" (DOWN)',
           count(*) FILTER (WHERE nbody ~* 'dominant\s+[a-z-]+\s+vector'),
           count(*), '27.8%' FROM ud
    UNION ALL SELECT 7, 'UNIT: contains "steady tension" (DOWN)',
           count(*) FILTER (WHERE nbody ~* 'steady tension'), count(*), '6.4%' FROM ud
    UNION ALL SELECT 8, 'UNIT: BLUF asserts an absence (context)',
           count(*) FILTER (WHERE bluf ~* '\y(no|not|none|nothing|neither|absence|absent)\y'),
           count(*), '25.9%' FROM ud
    UNION ALL SELECT 9, 'UNIT: absence BLUF is collection-scoped (UP; of absence BLUFs)',
           count(*) FILTER (
               WHERE bluf ~* '\y(no|not|none|nothing|neither|absence|absent)\y'
                 AND bluf ~* '(collect|this desk|reported in|in this slice|observed in|this window|these sources|the collection)'),
           count(*) FILTER (WHERE bluf ~* '\y(no|not|none|nothing|neither|absence|absent)\y'),
           '11.0%' FROM ud
    UNION ALL SELECT 10, 'UNIT: names a collection gap / thin slice (UP)',
           count(*) FILTER (
               WHERE nbody ~* '(collection gap|coverage gap|does not collect|do not collect|not in this collection|this desk collects)'
                  OR nbody ~* '\y(thin|sparse|limited) (slice|collection|coverage|reporting)\y'),
           count(*), '0.0%' FROM ud
    UNION ALL SELECT 11, 'UNIT: has a "what would change this read" section (UP)',
           count(*) FILTER (WHERE nbody ~* 'what would change this read'), count(*), '0.0%' FROM ud
    UNION ALL SELECT 12, 'UNIT: emits the new body shape (## What changed) (UP, new)',
           count(*) FILTER (WHERE body ~* '(?n)^##\s+What changed\y'), count(*), '0.0%' FROM ud
    UNION ALL SELECT 13, 'UNIT: header glued to prose (no newline before ##) (DOWN)',
           count(*) FILTER (WHERE body ~ '\S##\s'), count(*), '8.4%' FROM ud
    UNION ALL SELECT 14, 'UNIT: title over 90 chars (DOWN)',
           count(*) FILTER (WHERE length(title) > 90), count(*), '4.0%' FROM ud
    UNION ALL SELECT 15, 'UNIT: title contains "BLUF" (DOWN)',
           count(*) FILTER (WHERE title ~* 'BLUF'), count(*), '0.6%' FROM ud
    UNION ALL SELECT 16, 'UNIT: raw ISO/microsecond timestamp in prose (DOWN)',
           count(*) FILTER (WHERE body ~ '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d'),
           count(*), '0.6%' FROM ud
    UNION ALL SELECT 17, 'UNIT: indicator schema keys pasted into prose (DOWN)',
           count(*) FILTER (WHERE nbody ~* '(Status:\s*(not_observed|triggered|expired)|Horizon:\s*\d{4}-|First seen:\s*\d{4}-)'),
           count(*), '1.0%' FROM ud
),
comp_metrics(ord, metric, n, of, baseline) AS (
    SELECT 20, 'COMP: microsecond ISO timestamp in prose (DOWN)',
           count(*) FILTER (WHERE body ~ '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d'),
           count(*), '4.2%' FROM comps
    UNION ALL SELECT 21, 'COMP: leaks intensity NN.NN or an event count (DOWN)',
           count(*) FILTER (WHERE nbody ~* '(intensity\s+[0-9]+\.[0-9]+|[0-9]+\s+events\y)'),
           count(*), '13.9%' FROM comps
    UNION ALL SELECT 22, 'COMP: prints a raw confidence number in prose (DOWN)',
           count(*) FILTER (WHERE nbody ~* '(effective[ _]confidence\s*[:=(]?\s*0\.[0-9]|confidence\s*\(?0\.[0-9])'),
           count(*), '2.1%' FROM comps
    UNION ALL SELECT 23, 'COMP: OBSERVATION/JUDGMENT roll-call skeleton (DOWN)',
           count(*) FILTER (WHERE nbody ~* '(?n)^\s*(OBSERVATIONS?|JUDGMENT)\s*[:(]'),
           count(*), '38.2%' FROM comps
    UNION ALL SELECT 24, 'COMP: emits "## The picture" (UP, new)',
           count(*) FILTER (WHERE body ~* '(?n)^##\s+The picture\y'), count(*), '0.0%' FROM comps
    UNION ALL SELECT 25, 'COMP: emits "## Tension" (UP, new)',
           count(*) FILTER (WHERE body ~* '(?n)^##\s+Tension\y'), count(*), '0.0%' FROM comps
    UNION ALL SELECT 26, 'COMP: emits "## Coverage" (UP, new)',
           count(*) FILTER (WHERE body ~* '(?n)^##\s+Coverage\y'), count(*), '0.0%' FROM comps
    UNION ALL SELECT 27, 'COMP: opens with an *As of ...* line (UP, new)',
           count(*) FILTER (WHERE body ~* '(?n)^\s*\*as of\y'), count(*), '0.0%' FROM comps
    UNION ALL SELECT 28, 'COMP: malformed (refN) marker (DOWN)',
           count(*) FILTER (WHERE nbody ~ '\(ref[0-9]+\)'), count(*), '2.5%' FROM comps
)
SELECT metric,
       n,
       of,
       CASE WHEN of > 0
            THEN to_char(100.0 * n / of, 'FM990.0') || '%'
            ELSE 'n/a' END AS pct,
       baseline AS baseline_pre_deploy
  FROM (SELECT * FROM unit_metrics UNION ALL SELECT * FROM comp_metrics) m
 ORDER BY ord;
