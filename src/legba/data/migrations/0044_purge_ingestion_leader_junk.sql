-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0044_purge_ingestion_leader_junk.sql
--
-- DQ-H3 — close ingestion-asserted leadership facts (NER junk) + normalize.
--
-- WHY:
--   The DQ sweep (2026-06-21) found the leader-fact surface polluted by
--   ingestion NER triples written as authoritative "X leader of Y" facts:
--   "Adolf Hitler leader of Germany", "Josef Stalin leader of Russian",
--   "Didier Deschamps leader of Algeria" (France football coach), "Antonio
--   Guterres leader of Haiti", "DA leader of seven years", and ~90 more — all
--   source_type='ingestion', confidence 0.75. These are reachable via the
--   agency read tools (query_facts) and muddy the authoritative current-leader
--   surface that the world_baseline / wikidata_leaders seeds own. The forward
--   gate lands in fact_extractor._is_junk_triple (DQ-H3: drop ingestion
--   leadership predicates); this migration closes the existing backlog.
--
-- WHAT (idempotent — only touches still-OPEN rows; every step is valid_until-
-- only so nothing collides with the open-triple unique index):
--   1. Close every OPEN ingestion leadership fact (valid_until=NOW()).
--   2. Close redundant OPEN CamelCase 'LeaderOf' rows that have an OPEN
--      canonical 'leader of' twin (same subject/value/valid_from) — the seed
--      writes both surfaces, so the 'leader of' twin is the keeper. (We do NOT
--      rename 'LeaderOf'→'leader of' in place: that would violate
--      idx_facts_temporal_triple_open against the existing open twin.)
--   3. Close OPEN leadership facts whose subject OR value is a bare Wikidata
--      QID (e.g. 'Q22686', 'Q5771800') — unresolved ids that render as junk in
--      an authoritative-context line.
--
-- NOT DONE HERE (operator-gated): the seed head-of-state SUPERSESSION INVERSION
--   (11 countries where the 2026-06-19 refresh left the OLDER leader OPEN and
--   superseded the NEWER one — e.g. US Biden open / Trump superseded, Germany
--   Scholz open / Merz superseded) is NOT repaired here. A blind valid_from
--   flip would mis-fix Russia (Putin head-of-state vs Mishustin head-of-
--   government conflated as one predicate), and the CURRENT correct values are
--   operator/current-world-state authoritative (post-cutoff). Tracked for the
--   operator with the per-country evidence in the DQ sweep report.

-- 1. Close open ingestion leadership facts (the NER junk).
UPDATE facts
   SET valid_until = NOW()
 WHERE source_type = 'ingestion'
   AND predicate IN ('leader of', 'head of state', 'head of government', 'LeaderOf')
   AND superseded_by IS NULL
   AND valid_until IS NULL;

-- 2. Close redundant open CamelCase 'LeaderOf' rows that have an open canonical
--    'leader of' twin (the seed emits both surfaces; keep the canonical one).
UPDATE facts f
   SET valid_until = NOW()
 WHERE f.predicate = 'LeaderOf'
   AND f.superseded_by IS NULL AND f.valid_until IS NULL
   AND EXISTS (
        SELECT 1 FROM facts g
         WHERE g.predicate = 'leader of'
           AND lower(g.subject) = lower(f.subject)
           AND lower(g.value)   = lower(f.value)
           AND COALESCE(g.valid_from, '1970-01-01'::timestamptz)
             = COALESCE(f.valid_from, '1970-01-01'::timestamptz)
           AND g.id <> f.id
           AND g.superseded_by IS NULL AND g.valid_until IS NULL
   );

-- 3. Close open leadership facts carrying a bare Wikidata QID endpoint.
UPDATE facts
   SET valid_until = NOW()
 WHERE predicate IN ('leader of', 'head of state', 'head of government')
   AND (subject ~ '^Q[0-9]+$' OR value ~ '^Q[0-9]+$')
   AND superseded_by IS NULL
   AND valid_until IS NULL;
