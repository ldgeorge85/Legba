-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0141_backfill_frozen_apnews_fetched_at.sql
--
-- B-1 backfill — un-tell the six days of lies the 5 frozen AP feeds' rows are
-- still telling.
--
-- WHAT HAPPENED (MASTER_PLAN §32.7):
--   The 5 rsshub AP-topic feeds froze upstream on 2026-07-28 — same 39-item
--   snapshot on every poll, `pubDate` absent, HTTP 200 throughout. The S-4
--   intra-source collapse then bumped each surviving row's `fetched_at` on every
--   re-encounter, so 201 signals whose content was fetched ONCE, on 07-28,
--   reported `fetched_at = today` for six consecutive days. Because the substrate
--   slice windows AND orders on `fetched_at`, that snapshot topped every 72h
--   slice for the Korea / Taiwan / Niger / Haiti / DRC desks, and the journal
--   re-narrated the same "breaking" North Korea story every 12h leg.
--
--   The code path is fixed forward by the same commit-train as this migration
--   (a re-serve advances `last_seen_at`, never `fetched_at`). This migration
--   repairs the rows that fix cannot reach: the ones already carrying an
--   advanced timestamp.
--
-- WHY `created_at`:
--   The original `fetched_at` was overwritten in place and is not recoverable.
--   `created_at` is the row's INSERT time, which is within seconds of the fetch
--   that produced it (the write happens at the end of the same poll) — it is the
--   closest honest value available, and it errs in the safe direction: a hair
--   LATER than the true fetch, never earlier, so no row is made to look fresher
--   than it was. Measured spread on these rows before the repair:
--   `fetched_at - created_at` ranged from 3h to 5d18h, with ZERO rows under an
--   hour — every single one had been advanced, none marginally.
--
-- WHY `last_seen_at` GETS THE OLD VALUE:
--   The advanced `fetched_at` was not noise — it was a real observation, filed
--   under the wrong name. It records when the source last re-served this exact
--   content, which is precisely `last_seen_at`'s definition (migration 0140).
--   Moving it there rather than discarding it means the repair destroys no
--   information: after this migration each row says, truthfully, "fetched 07-28,
--   still being re-served on 08-03" — which is exactly the serve-stale picture
--   the desks should have been shown all along.
--
-- SCOPE — these 5 source ids and nothing else:
--   Named explicitly rather than pattern-matched. A `LIKE 'source.rsshub.%'`
--   would sweep in every other rsshub-routed feed, and a blanket
--   `fetched_at > created_at` repair across all sources would rewrite ~620MB of
--   table to correct rows nobody has evidence about. These 5 are the ones with a
--   diagnosed, dated, upstream-confirmed freeze; every other source's history is
--   left exactly as it stands. All 5 are `state='paused'` as of 08-03 (the B-0
--   mitigation), so nothing is re-bumping them while this runs.
--
-- IDEMPOTENT: the `fetched_at > created_at` predicate is self-extinguishing —
-- after one run there are no rows left to match. Re-applying is a no-op, not a
-- second rewind.

-- Preserve the re-serve observation under its correct name FIRST (the next
-- statement destroys the value this one reads). `GREATEST` guards the
-- already-correct case; COALESCE covers pre-0140 rows, which are all of them.
UPDATE public.signals
   SET last_seen_at = GREATEST(COALESCE(last_seen_at, fetched_at), fetched_at)
 WHERE source_id IN (
           'source.rsshub.apnews.north_korea',
           'source.rsshub.apnews.taiwan',
           'source.rsshub.apnews.niger',
           'source.rsshub.apnews.haiti',
           'source.rsshub.apnews.drcongo'
       )
   AND fetched_at > created_at;

UPDATE public.signals
   SET fetched_at = created_at,
       updated_at = NOW()
 WHERE source_id IN (
           'source.rsshub.apnews.north_korea',
           'source.rsshub.apnews.taiwan',
           'source.rsshub.apnews.niger',
           'source.rsshub.apnews.haiti',
           'source.rsshub.apnews.drcongo'
       )
   AND fetched_at > created_at;
