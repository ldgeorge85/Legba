-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0115_source_quality_ledger.sql
--
-- C3 (one source-quality ledger) — the coherence-wave fold of FOUR separately
-- grown source-quality organs into ONE typed read surface:
--
--   1. `source_credibility`   (ASSERTED, baseline 0001) — an operator/seed
--      score per HOST (0..1) + tier + state-affiliation flag.
--   2. `source_ratings`       (ASSERTED, 0094)          — the Admiralty rubric
--      grade (reliability A-F x credibility 1-6) per SOURCE DESCRIPTOR, and
--      `source_dossiers` (0094) — the cited descriptive dossier.
--   3. `source_track_records` (EARNED,   0099)          — the MEASURED record
--      of how the source's contested claims actually fared.
--   4. freshness              (COMPUTED)                — signal-production
--      recency against the source's OWN declared cadence.
--
-- MERGED, NOT BLENDED. Every column is prefixed with the KIND of knowledge it
-- carries — `asserted_*` (somebody said so), `earned_*` (our substrate
-- measured it), `computed_*` (derived from observed production). That split is
-- the DESIGNED honesty property of the A6 program (an asserted A1 grade and an
-- earned 0.31 win-rate must never average into one number), so it is enforced
-- by the schema's own naming rather than by convention in a reader. There is
-- deliberately NO composite "quality score" column anywhere: the surface hands
-- a reader four legs and refuses to collapse them.
--
-- NUMBERING: the C3 plan reserved 0110/0111, but 0112 (retrieval_origin),
-- 0113 (collection_requirements) and 0114 (source_poll_outcome_success) landed
-- first, so this is 0115. The runner discovers by sorted glob, so the 0110/0111
-- gap is harmless (same note as 0098/0099). Only ONE slot was needed — the
-- second reserved slot is unused.
--
-- WHY A VIEW, NOT A TABLE. Every leg is already durably stored and separately
-- owned (ratings by the seed loader, host scores by the credibility CRUD,
-- track records by the daily `source_track_record` analyst, signals by the
-- source actors). A materialized copy would be a FIFTH organ needing its own
-- refresh job — exactly the disease this wave treats. The view owns no state:
-- drop it, recreate it, and the content is identical. It is also the reason
-- this migration is safely re-appliable and instantly revertible.
--
-- IDENTITY (`source_id`) is the source DESCRIPTOR id, matching 0094 / 0099 /
-- `signals.source_id`. The row SPINE is the UNION of every id any leg knows —
-- head descriptors, current ratings, current dossiers, track records — because
-- a rating may legitimately precede registration (catalog seeds) and a track
-- record may outlive a retired descriptor. `registered` states which case a
-- row is, so a reader is never guessing.
--
-- THE HOST JOIN (the one non-obvious leg). `source_credibility` is keyed by
-- HOST, everything else by descriptor id. The bridge is the descriptor's own
-- declared endpoint (`body.config.url.raw`): extract its host, then probe the
-- credibility table with the EXACT host first and progressively-trimmed
-- parent domains after it, first hit wins. That is deliberately the SAME rule
-- `legba.data.filters.source_credibility.extract_lookup_hosts` applies at the
-- signal WRITE path (`www.csis.org` -> `csis.org`), so the ledger's asserted
-- host score agrees with the score actually stamped onto that source's
-- signals instead of quietly disagreeing with it. IP literals are NOT trimmed
-- (trimming an address is meaningless and would produce false hits) — the same
-- guard the Python helper carries. Drift between the two implementations is
-- test-enforced (tests/data_pkg/test_source_quality_ledger.py::
-- test_view_host_resolution_matches_extract_lookup_hosts).
--
-- WHAT THE VIEW DOES NOT CARRY: the freshness GRADE itself (`ok`/`stale`/
-- `warn`/`empty`/`ungraded`). Its budget derives from a cron expression via
-- croniter, which is Python, not SQL — so the view publishes the grade's
-- INPUTS (`cadence_raw`, `computed_last_signal_at`, `computed_age_seconds`)
-- and the shared `legba.data.registry.source_freshness` module grades them at
-- read time, exactly as the existing System Status route does. One grading
-- implementation, two readers — never a second, drifting SQL copy.
--
-- COST: the signals leg is ONE grouped scan (~90ms on the live corpus),
-- deliberately the same shape the `/v3/system/source-firing` route already
-- runs, rather than a per-source LATERAL (measured 15x slower at 109 sources).
--
-- CONSUMPTION RULE (A6, unchanged and load-bearing): nothing here feeds the
-- faithfulness score. Trust is not groundedness. The arbiter's earned
-- tie-break seam (`LEGBA_CONTENTION_EARNED_WEIGHT`, default OFF) still
-- recomputes its weight LIVE with the acyclicity guard and NEVER reads this
-- view or the stored aggregate behind it — the byte-identity of that read
-- across this fold is test-enforced.
--
-- SAFETY (idempotent, additive, forward-only): CREATE OR REPLACE VIEW only; no
-- existing table or view is touched, no data is written or moved. Re-apply and
-- cold-start are both no-ops. The runner wraps this file in its own
-- transaction and records it in `legba_data_migrations` (no inline
-- BEGIN/COMMIT — same as 0091-0114).

CREATE OR REPLACE VIEW public.source_quality AS
WITH heads AS (
    SELECT descriptor_id                          AS source_id,
           state,
           kind,
           body->'cadence'->'schedule'->>'raw'    AS cadence_raw,
           body->'config'->'url'->>'raw'          AS endpoint_url
      FROM public.source_descriptors
     WHERE is_head
),
ids AS (
    SELECT source_id FROM heads
    UNION
    SELECT source_id FROM public.source_ratings        WHERE superseded_by IS NULL
    UNION
    SELECT source_id FROM public.source_dossiers       WHERE superseded_by IS NULL
    UNION
    SELECT source_id FROM public.source_track_records
),
spine AS (
    SELECT i.source_id,
           (h.source_id IS NOT NULL)               AS registered,
           h.state                                 AS declared_state,
           h.kind                                  AS declared_kind,
           h.cadence_raw,
           h.endpoint_url,
           -- scheme://[user@]host[:port]/... ; bracketed IPv6 literals keep
           -- their inner address (the brackets are stripped, not matched).
           lower(substring(h.endpoint_url
                 FROM '^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/?#]*@)?\[?([^/:?#\[\]]+)'))
                                                   AS endpoint_host
      FROM ids i
      LEFT JOIN heads h ON h.source_id = i.source_id
),
sig AS (
    -- Production truth, keyed on created_at (when the row landed in the
    -- substrate) — the same column and shape /v3/system/source-firing uses.
    SELECT g.source_id,
           max(g.created_at)                       AS last_signal_at,
           count(*) FILTER (
               WHERE g.created_at > now() - interval '24 hours'
           )                                       AS signals_24h,
           count(*) FILTER (
               WHERE g.created_at > now() - interval '7 days'
           )                                       AS signals_7d
      FROM public.signals g
     GROUP BY g.source_id
)
SELECT
    -- ── identity / backbone ────────────────────────────────────────────────
    s.source_id,
    s.registered,
    s.declared_state,
    s.declared_kind,
    s.cadence_raw,
    s.endpoint_url,
    s.endpoint_host,

    -- ── ASSERTED, layer 2: the Admiralty rubric grade (0094) ───────────────
    -- The CURRENT, PUBLIC, fully-graded rating; most recent wins. Identical
    -- selection rule to `registry.api.load_assurance_grades` so the ledger and
    -- the /sources projection can never disagree. Private-annex rows are NEVER
    -- consulted for these columns (the visibility default-deny posture); they
    -- are only COUNTED, below.
    adm.admiralty_reliability                      AS asserted_admiralty_reliability,
    adm.admiralty_credibility                      AS asserted_admiralty_credibility,
    adm.grade                                      AS asserted_admiralty_grade,
    adm.rater                                      AS asserted_admiralty_rater,
    adm.method                                     AS asserted_admiralty_method,
    adm.rated_at                                   AS asserted_admiralty_rated_at,
    rc.public_count                                AS asserted_public_rating_count,
    rc.private_count                               AS asserted_private_rating_count,

    -- ── ASSERTED, layer 1: the cited dossier (0094) ────────────────────────
    -- Presence + provenance only; the markdown body stays behind the detail
    -- route (a list surface has no business carrying compiled prose).
    (dos.compiled_at IS NOT NULL)                  AS asserted_has_dossier,
    dos.compiled_at                                AS asserted_dossier_compiled_at,
    dos.compiled_by                                AS asserted_dossier_compiled_by,

    -- ── ASSERTED, host score: the credibility table (baseline 0001) ────────
    cred.source_host                               AS asserted_host_matched,
    cred.score                                     AS asserted_host_score,
    cred.tier                                      AS asserted_host_tier,
    cred.state_affiliation                         AS asserted_host_state_affiliation,
    cred.score_rationale                           AS asserted_host_rationale,
    cred.scored_by                                 AS asserted_host_scored_by,
    cred.last_updated                              AS asserted_host_scored_at,

    -- ── EARNED: the measured track record (0099) ───────────────────────────
    tr.wins                                        AS earned_wins,
    tr.losses                                      AS earned_losses,
    tr.contested_total                             AS earned_contested_total,
    tr.win_rate_raw                                AS earned_win_rate_raw,
    tr.win_rate_smoothed                           AS earned_win_rate_smoothed,
    tr.win_rate_lower                              AS earned_win_rate_lower,
    tr.low_sample                                  AS earned_low_sample,
    tr.corroborated                                AS earned_corroborated,
    tr.corroboration_total                         AS earned_corroboration_total,
    tr.corroboration_rate                          AS earned_corroboration_rate,
    tr.lag_hours                                   AS earned_lag_hours,
    tr.sample_as_of                                AS earned_sample_as_of,
    tr.computed_at                                 AS earned_computed_at,

    -- ── COMPUTED: observed production (the freshness grade's inputs) ───────
    sig.last_signal_at                             AS computed_last_signal_at,
    EXTRACT(EPOCH FROM (now() - sig.last_signal_at))::bigint
                                                   AS computed_age_seconds,
    COALESCE(sig.signals_24h, 0)                   AS computed_signals_24h,
    COALESCE(sig.signals_7d, 0)                    AS computed_signals_7d

  FROM spine s

  LEFT JOIN LATERAL (
        SELECT r.admiralty_reliability,
               r.admiralty_credibility,
               r.admiralty_reliability || r.admiralty_credibility AS grade,
               r.rater,
               r.method,
               r.rated_at
          FROM public.source_ratings r
         WHERE r.source_id = s.source_id
           AND r.superseded_by IS NULL
           AND r.visibility_class = 'public'
           AND r.admiralty_reliability IS NOT NULL
           AND r.admiralty_credibility IS NOT NULL
         ORDER BY r.rated_at DESC
         LIMIT 1
  ) adm ON TRUE

  LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE r.visibility_class = 'public')::int
                   AS public_count,
               count(*) FILTER (WHERE r.visibility_class = 'private')::int
                   AS private_count
          FROM public.source_ratings r
         WHERE r.source_id = s.source_id
           AND r.superseded_by IS NULL
  ) rc ON TRUE

  LEFT JOIN LATERAL (
        SELECT d.compiled_at, d.compiled_by
          FROM public.source_dossiers d
         WHERE d.source_id = s.source_id
           AND d.superseded_by IS NULL
         LIMIT 1
  ) dos ON TRUE

  LEFT JOIN LATERAL (
        -- Exact host first, then progressively-trimmed parent domains, first
        -- hit wins — `extract_lookup_hosts`' rule, in SQL. IP literals (v4
        -- dotted-quad or anything carrying ':') are probed unsplit.
        SELECT c.source_host, c.score, c.tier, c.state_affiliation,
               c.score_rationale, c.scored_by, c.last_updated
          FROM unnest(
                 CASE
                   WHEN s.endpoint_host IS NULL THEN NULL::text[]
                   WHEN s.endpoint_host ~ '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
                        OR position(':' IN s.endpoint_host) > 0
                     THEN ARRAY[s.endpoint_host]
                   ELSE (
                     SELECT array_agg(
                              array_to_string(
                                  (string_to_array(s.endpoint_host, '.'))[k:],
                                  '.')
                              ORDER BY k)
                       FROM generate_subscripts(
                                string_to_array(s.endpoint_host, '.'), 1) AS k
                   )
                 END
               ) WITH ORDINALITY AS cand(host, ord)
          JOIN public.source_credibility c ON c.source_host = cand.host
         ORDER BY cand.ord
         LIMIT 1
  ) cred ON TRUE

  LEFT JOIN public.source_track_records tr ON tr.source_id = s.source_id
  LEFT JOIN sig                             ON sig.source_id = s.source_id;

COMMENT ON VIEW public.source_quality IS
    'C3 source-quality ledger (migration 0115): one typed read surface over the '
    'asserted (source_credibility host scores + source_ratings Admiralty grades '
    '+ source_dossiers), earned (source_track_records) and computed (observed '
    'signal production) legs, keyed by source descriptor id. Columns are '
    'prefixed by KIND of knowledge on purpose — asserted/earned/computed are '
    'never blended, and no composite score exists. The freshness GRADE is '
    'derived at read by legba.data.registry.source_freshness (croniter budgets '
    'are Python, not SQL); this view carries its inputs. Nothing here feeds the '
    'faithfulness score (A6 hard rule), and the arbiter earned tie-break does '
    'not read it.';
