-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0140_signal_last_seen_at.sql
--
-- B-1 (freshness honesty) — separate "when did we FETCH this content" from
-- "when did the source last RE-SERVE it".
--
-- WHY (the 2026-08-03 incident, MASTER_PLAN §32.7):
--   The S-4 intra-source collapse (migration-less, `source_actor.py`) bumps an
--   existing row's `fetched_at` every time the source re-lists byte-identical
--   content. That was designed as "recency stays fresh — we still see this".
--   It is a LIE with load-bearing consequences: when the 5 rsshub AP-topic
--   feeds froze upstream on 07-28 and re-served the same 39-item snapshot every
--   poll for six days, 201 signals kept reporting `fetched_at = today`. The
--   substrate slice WINDOWS and ORDERS on `fetched_at`
--   (`actor_substrate_slice.py`), so a frozen 07-28 snapshot topped every 72h
--   slice for every geo-matched desk (Korea/Taiwan/Niger/Haiti/DRC) and the
--   journal re-narrated the same "breaking" story every 12h leg.
--
--   `fetched_at` must mean what every reader already assumes it means: the time
--   we fetched THIS content. Identical content re-served is not a new fetch.
--
-- WHY A NEW COLUMN (and not just dropping the bump):
--   The bump was doing a second, legitimate job: it kept a continuously
--   re-served item inside its own dedup lookback window
--   (`LEGBA_INTRASOURCE_DEDUP_WINDOW_HOURS`, default 168h). Drop the bump with
--   the window still keyed on `fetched_at` and an item re-served for more than
--   7 days falls out of its own window — a duplicate row inserts with a genuinely
--   fresh `fetched_at`, which re-creates the exact freshness lie on a weekly
--   period AND regresses the 41%-duplicate-rows collapse S-4 was built for.
--   `last_seen_at` carries the "we still see this" fact honestly, and the
--   window keys off it.
--
--   `updated_at` was considered and rejected as the window key: it fires on
--   every row mutation (embedder, summarizer, salience, alias stamping,
--   re-enrichment), so it answers "was this row touched", not "did the source
--   re-serve it" — the wrong question for a serve-stale readout.
--
-- WHY NULLABLE (no backfill of the 107k existing rows):
--   A NULL `last_seen_at` means "never re-served since insert", which is exactly
--   true for every pre-existing row we have no re-serve record for. Readers use
--   `COALESCE(last_seen_at, fetched_at)`, so the column needs no mass UPDATE
--   (which would rewrite the whole 619MB table for zero information gained).
--   New rows get `now()` from the default; the insert path stamps the signal's
--   own `fetched_at` explicitly so the two agree on the first write.
--
--   No index: the only reader is the S-4 collapse probe, whose candidate set is
--   already pinned to a tiny (source_id, content_hash) equality — `last_seen_at`
--   is a filter on a handful of rows, never a scan driver.
--
-- SECOND CHANGE — `source_poll_outcomes.reserve_unchanged`:
--   With the bump gone, a poll of a frozen feed writes 0 signals and mutates
--   nothing. The count of unchanged re-serves is the evidence that distinguishes
--   "the source is alive and re-serving stale content" from "the source returned
--   nothing at all" — the discriminator the acquisition watchdog never had (the
--   incident's poll ledger said success/healthy, signals_written=0,
--   newest_entry_ts NULL). Recording it per poll gives the serve-stale gauge a
--   real denominator. Additive, defaulted, no rewrite.

ALTER TABLE public.signals
    ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;

-- Set the default AFTER the add so the (fast, catalog-only) ADD COLUMN does not
-- have to materialize a value for 107k existing rows. Existing rows stay NULL
-- (= "no re-serve recorded"); every new row gets a real value.
ALTER TABLE public.signals
    ALTER COLUMN last_seen_at SET DEFAULT now();

COMMENT ON COLUMN public.signals.last_seen_at IS
    'When the source last RE-SERVED this exact content (S-4 intra-source '
    'exact-hash collapse). NULL = never re-served since insert. Distinct from '
    'fetched_at, which is pinned to the fetch that first delivered this content '
    'and must never be advanced by a re-serve (B-1, 2026-08-03).';

ALTER TABLE public.source_poll_outcomes
    ADD COLUMN IF NOT EXISTS reserve_unchanged integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.source_poll_outcomes.reserve_unchanged IS
    'How many entries this poll re-served byte-identically (exact content_hash '
    'match on an existing row): the source is alive but its content is not '
    'moving. A poll with signals_written=0 AND reserve_unchanged>0 is the '
    'serve-stale signature (B-1, 2026-08-03).';
