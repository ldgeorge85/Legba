-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0130_quarantine_subfloor_embeddings.sql
--
-- DATA REPAIR (roadmap B-4/B-5, 2026-08-02 engine review P2 §2.4).
--
-- WHAT THE REVIEW EXPECTED TO FIND, AND WHAT IS ACTUALLY THERE.
-- The roadmap called for purging the semantic dedup links that the measured
-- 0.95 threshold got wrong. There are none: `signal_aliases` holds 4,441
-- `ingest_url` and 6 `content_hash` rows, every one at score 1.0, and ZERO
-- `semantic_qdrant` rows. The tier never wrote a link in its history, because
-- it never issued a Qdrant query (it called `recommend()`, removed from the
-- client in 1.10). The "50.5% of links are wrong" figure was a SIMULATION of
-- what a repaired pass WOULD link, not a count of existing rows.
--
-- So there is nothing to purge — and the real hazard is upstream of the links:
-- the vectors those links would have been drawn from.
--
-- THE DEFECT. `signal_embedder._pick_body` took the FIRST non-empty body field
-- with no minimum length, so a 5-char "(END)" outranked a 446-char summary.
-- Identical input gives an identical vector, which gives cosine 1.0000 between
-- unrelated stories. Measured against this database on 2026-08-02:
--
--     vectored signals                                   59,994
--     first non-empty body field under 200 chars         36,733   (61.2%)
--       ...of which carry a usable title                 34,990
--       ...with no title at all                           1,743
--     under the absolute 80-char floor                    5,050
--
-- and, over sampled neighbour pairs at >=0.80, 60.8% are degenerate. Those
-- pairs score at the TOP of the range regardless of content, so raising the
-- threshold does not separate them: precision over ALL pairs at 0.97 is
-- 0.728, against 0.992 once degenerate pairs are excluded. The exclusion has
-- to be structural, which is what this migration provides.
--
-- WHY THE PREDICATE IS SOUND. `_clean_html` only ever SHRINKS a string (tags
-- become one space, entities unescape shorter, whitespace collapses), so
-- `raw_length < 200` GUARANTEES `cleaned_length < 200`. This is a one-sided
-- test: it never flags a row whose real embed input clears the floor, and it
-- misses some markup-heavy rows that do not. Erring toward under-repair is the
-- correct direction — a missed quarantine costs a little precision, an
-- over-eager one costs recall on good vectors.
--
-- QUARANTINE, NOT DELETE. No row is deleted, no Qdrant point is dropped (a SQL
-- migration cannot reach the vector store anyway; the stale point is harmless
-- once neither side of a link can reference it, and a re-embed overwrites it
-- in place because the point id IS the signal id). The marker moves from the
-- signal's own uuid to the sentinel `stale_subfloor`, which:
--
--   * removes the row as a dedup CANDIDATE — `cross_source_dedup` matches
--     `embedding_ref` against the uuid shape, which a sentinel fails;
--   * removes it as a dedup NEIGHBOUR — the handler's neighbour gate requires
--     the same uuid shape on the other side of every proposed link;
--   * keeps the cohort exactly queryable forever
--     (`WHERE embedding_ref = 'stale_subfloor'`), which NULL would not;
--   * is exactly REVERSIBLE — the previous value was always the row's own id:
--         UPDATE signals SET embedding_ref = id::text
--          WHERE embedding_ref = 'stale_subfloor';
--
-- RE-EMBED IS THE OPERATOR'S, DELIBERATELY. 36,733 rows is 36,733 hosted GPU
-- calls; at signal_embedder's default 200/tick over a 15-minute cadence that is
-- ~28 days, and it should be scheduled (or the per-tick budget raised) rather
-- than started as a side effect of a migration. When ready, one statement
-- releases the cohort into the sweep, which will re-embed it through the fixed
-- length floor + title composition:
--
--     UPDATE signals SET embedding_ref = NULL
--      WHERE embedding_ref = 'stale_subfloor';
--
-- Until then the vector plane is SMALLER and CORRECT rather than larger and
-- poisoned — which is the whole point, because a false dedup link sets
-- `canonical_signal_id` and every desk slice is canonical-only, so it makes a
-- real signal invisible to every analyst on the platform.

UPDATE signals
   SET embedding_ref = 'stale_subfloor',
       updated_at = NOW()
 WHERE embedding_ref ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
   AND length(
         COALESCE(
           NULLIF(btrim(payload->>'distilled_body'), ''),
           NULLIF(btrim(payload->>'raw_body'), ''),
           NULLIF(btrim(payload->>'summary'), ''),
           NULLIF(btrim(payload->>'description'), ''),
           NULLIF(btrim(payload->>'content_text'), ''),
           NULLIF(btrim(payload->>'text'), ''),
           ''
         )
       ) < 200;
