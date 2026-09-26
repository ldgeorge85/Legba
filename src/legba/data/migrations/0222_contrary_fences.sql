-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0222_contrary_fences.sql
--
-- Program 7a — THE FOUR FENCES on `claim_contentions.stance='contradicts'`,
-- written against the contrary-evidence pass's FIRST LIVE RUN
-- (planning/NEXT_ARC_CAPTURE_2026-09-23.md §A.1, docs/ANALYSIS.md §10.6.1).
--
-- ─────────────────────────────────────────────────────────────────────────
-- WHY. Three rows, all false.
-- ─────────────────────────────────────────────────────────────────────────
-- Run `abfd43d6-77ce-41ef-945a-ce7f94d2b8eb`, 2026-09-25 22:26:54–22:37:44Z,
-- $0: 60 claims → 60 web_search + 142 web_fetch → 57 `none_found` (44 model-leg,
-- 13 polarity) and **3 `contradicts`, every one of them wrong**:
--
--   1. energy_security / country_g20_in — the claim was India's energy-security
--      pressure; the counter page was `en.wikipedia.org/wiki/Strait_of_Hormuz`,
--      metadata-dated **2018-12-10**, and the matched sentence was about Gulf
--      shipping generally. Subject overlap with the claim: 2 tokens.
--   2. escalation / country_g20_kr — the claim was a DMZ land-mine explosion in
--      this window; the counter page was
--      `en.wikipedia.org/wiki/Korean_Demilitarized_Zone`, **2007-05-17**, and
--      the matched sentence was the DEFINITION of the DMZ.
--   3. military_posture / country_g20_ru (the uncalibrated negation leg) — one
--      page was `en.wikipedia.org/wiki/Russia` (384 chars extracted), the other
--      a single FPRI article dated **2026-08-04** about mid-May exercises.
--
-- The run's own search health says the rest: SearXNG reported
-- `unresponsive_engines` five times and the free rung's hits were dominated by
-- wikipedia.org, merriam-webster.com and generic corporate pages. An
-- encyclopedia article is what a search engine returns when it has nothing.
--
-- The composition tension leg would otherwise have rendered rows 1 and 2 — the
-- polarity-derived ones — as counter-evidence in the next India and Korea
-- compositions. That is precisely the failure R2 shipped zero pairs rather than
-- risk: a false contradiction rendered into a composition manufactures a
-- disagreement for the fleet to write up.
--
-- ─────────────────────────────────────────────────────────────────────────
-- WHAT THESE FOUR COLUMNS ARE
-- ─────────────────────────────────────────────────────────────────────────
-- Each fence writes its own number onto the row, so the fence is MEASURABLE
-- rather than merely asserted — the `query_novel_tokens` precedent, which turned
-- "is the counter-query a paraphrase?" from an anxiety into a column somebody
-- can average.
--
--   `host_class`        F1 — what KIND of host the decisive page came from.
--                       `reference` (encyclopedia / dictionary / glossary) can
--                       never carry `contradicts`; a host this platform already
--                       ingests keeps the class it was REGISTERED under
--                       (`source_descriptors.body->scope->source_class`:
--                       reporting / analysis / official / state_media), and
--                       anything else is `unknown`, which PASSES. One
--                       vocabulary, not two.
--   `page_published_at` F2 — the date the DATE GATE actually parsed out of the
--                       page's own machine-readable metadata or a dated URL
--                       (the reference builder's gate, imported). A date this
--                       code could not find is not a date: NULL here on a
--                       stance-bearing row means the page was undated, and an
--                       undated page cannot contradict.
--   `subject_overlap`   F3 — how many of the CLAIM's subject tokens the matched
--                       sentence carried. Row 1 scored 2 against a floor of 2,
--                       which is why F1 and F2 exist: this fence alone would not
--                       have caught it, and a column that says so is better than
--                       a rule that pretends otherwise.
--   `independent_pages` F4 — how many INDEPENDENT outlets carried an admissible
--                       contradiction, counted by 7d's own
--                       `source_independence.independence_of` (two mastheads
--                       running one dispatch are one source). One page is
--                       `qualifies`; `contradicts` needs two.
--
-- ─────────────────────────────────────────────────────────────────────────
-- NULLABLE, AND NOT BACKFILLED. That is the design, not an omission.
-- ─────────────────────────────────────────────────────────────────────────
-- Every row written before this migration measured NONE of these four. NULL
-- means "not measured" and it is emphatically not zero: a backfill would have to
-- invent a host class from a URL nobody re-fetched, a publication date nobody
-- re-read, and an overlap against a sentence nobody re-derived — which is the
-- `reference_age_days` ruling in 0197, one table over.
--
-- The three false rows stay exactly as they are. They already carry an
-- `expires_at` in the past, so every reader treats them as inert, and the
-- pipeline stamp moved with the rules (`2026-09/7a.1` → `2026-09/7a.2`) so a
-- fenced row and an unfenced one can never be pooled into one population. They
-- are the lane's fixtures; deleting them would delete the evidence.
--
-- ─────────────────────────────────────────────────────────────────────────
-- THE CHECK IS `NOT VALID`, DELIBERATELY
-- ─────────────────────────────────────────────────────────────────────────
-- `ck_claim_contentions_contradicts_independent` says what F4 says: a row whose
-- stance is `contradicts` must name at least two independent pages. The NULL
-- leg is spelled out (`IS NOT NULL AND >= 2`) rather than left to SQL's
-- three-valued logic, which would PASS a NULL and let "not measured" be written
-- as a contradiction — the one reading of this column the whole migration
-- exists to refuse. It is added NOT VALID so the three grandfathered rows
-- (stance `contradicts`, `independent_pages` NULL) are left alone while EVERY
-- future insert and update is checked — Postgres enforces a NOT VALID check on
-- new rows, it only skips the back-scan. It is never VALIDATEd, because
-- validating it would require either deleting the evidence or inventing a
-- number for it.
--
-- This is the schema-level half of a fence that already runs in code
-- (`_contrary_fences.contradiction_admitted`) and is re-tested at the
-- composition reader (`contrary_tension.TENSION_SQL`). Three layers for one
-- rule is right here: the composition is the one surface where a false
-- contradiction becomes prose the fleet writes up.
--
-- The runner wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0204/0206/0207/
-- 0209/0220/0221).

ALTER TABLE public.claim_contentions
    ADD COLUMN IF NOT EXISTS host_class        text,
    ADD COLUMN IF NOT EXISTS page_published_at date,
    ADD COLUMN IF NOT EXISTS subject_overlap   integer,
    ADD COLUMN IF NOT EXISTS independent_pages integer;

-- F1's vocabulary, closed, and it is the source-class taxonomy plus exactly two
-- words: `reference` (the fence's own class) and `unknown` (not a registered
-- source, which passes). A second vocabulary for "what kind of source is this"
-- is how two surfaces come to disagree about one page.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_claim_contentions_host_class_vocab'
           AND conrelid = 'public.claim_contentions'::regclass
    ) THEN
        ALTER TABLE public.claim_contentions
            ADD CONSTRAINT ck_claim_contentions_host_class_vocab
            CHECK (
                host_class IS NULL
                OR host_class IN ('reference', 'reporting', 'analysis',
                                  'official', 'state_media', 'unknown')
            ) NOT VALID;
    END IF;
END
$$;

-- Counts are counts. A negative overlap or a negative page count is not an
-- unexpected value, it is a bug, and it is unrepresentable rather than merely
-- surprising (the 0197 `reference_age_days` precedent).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_claim_contentions_fence_counts'
           AND conrelid = 'public.claim_contentions'::regclass
    ) THEN
        ALTER TABLE public.claim_contentions
            ADD CONSTRAINT ck_claim_contentions_fence_counts
            CHECK (
                (subject_overlap IS NULL OR subject_overlap >= 0)
                AND (independent_pages IS NULL OR independent_pages >= 0)
            ) NOT VALID;
    END IF;
END
$$;

-- F4, at the schema. See the header for why this one is NOT VALID and stays
-- that way.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_claim_contentions_contradicts_independent'
           AND conrelid = 'public.claim_contentions'::regclass
    ) THEN
        ALTER TABLE public.claim_contentions
            ADD CONSTRAINT ck_claim_contentions_contradicts_independent
            CHECK (
                stance <> 'contradicts'
                OR (independent_pages IS NOT NULL AND independent_pages >= 2)
            ) NOT VALID;
    END IF;
END
$$;

-- `GET /v3/contentions?scope=<target>`. The route's `scope` now accepts EITHER
-- the desk key (the analyst id, e.g. `energy_security`) or the TARGET
-- (`country_g20_in`) — every other reader surface on this platform means the
-- target by `scope`, and a route that quietly meant something else is a reader
-- looking at an empty list with no way to know why. `desk_key` already has
-- `claim_contentions_desk_idx`; this is its twin, so the OR can be a BitmapOr
-- of two index scans rather than a sequential scan of the table.
CREATE INDEX IF NOT EXISTS claim_contentions_target_idx
    ON public.claim_contentions USING btree (target_id, as_of DESC);

COMMENT ON COLUMN public.claim_contentions.host_class IS
    'F1 — the KIND of host the decisive counter page came from: reference '
    '(encyclopedia/dictionary/glossary, which can never carry contradicts), '
    'the source-class taxonomy''s own word for a registered source '
    '(reporting | analysis | official | state_media), or unknown, which '
    'passes. NULL = not measured (written before migration 0222).';
COMMENT ON COLUMN public.claim_contentions.page_published_at IS
    'F2 — the publication date the DATE GATE parsed from the page''s own '
    'machine-readable metadata or a dated URL (the reference builder''s gate). '
    'NULL on a stance-bearing row means the page was UNDATED, and an undated '
    'page cannot contradict; NULL on a pre-0222 row means not measured. Never '
    'read from prose, a masthead or a relative time string.';
COMMENT ON COLUMN public.claim_contentions.subject_overlap IS
    'F3 — how many of the claim''s subject tokens the MATCHED SENTENCE carried '
    '(R2''s subject_tokens_of on both sides). The other half of '
    'query_novel_tokens: one measures whether the query was about the claim, '
    'this measures whether the page was. NULL = not measured.';
COMMENT ON COLUMN public.claim_contentions.independent_pages IS
    'F4 — how many INDEPENDENT outlets carried an admissible contradiction, by '
    'source_independence.independence_of (two mastheads running one dispatch '
    'are one source). contradicts requires >= 2; one admissible page is '
    'qualifies. NULL = not measured, and the composition tension read excludes '
    'it for that reason.';
