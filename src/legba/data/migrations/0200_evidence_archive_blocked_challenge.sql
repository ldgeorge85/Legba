-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0200_evidence_archive_blocked_challenge.sql
--
-- A distinct terminal status for "a publisher's edge refused us", so a block
-- stops reading as an empty web.
--
-- THE DEFECT (planning FETCH_REVIEW §2, 2026-09-16)
-- ------------------------------------------------
-- `evidence_archiver`'s wall detector already carried bot-challenge phrasing
-- (`are you a robot`, `checking your browser before accessing`, …) but gated
-- EVERY pattern behind `_WALL_MAX_CHARS = 500`. That cap was tuned on the
-- ≤500-char no-JS fallback pages a live audit actually found — Le Monde at
-- 286 chars, irna at 70. It does not reach a modern challenge page: the review
-- measured AP's Cloudflare interstitial at 5 507 B and Times of Israel's at
-- 12 604 B. Both sailed past the cap, so a challenge landed as a thin or
-- failed extraction and read downstream as *the web had nothing*.
--
-- That is a FALSE ABSENCE, and the review is explicit that it is a bigger
-- defect than the 403 itself: it is the same failure the search plane's
-- degradation reads exist to prevent. A desk must be able to tell "this
-- publisher blocked us" from "nothing was published".
--
-- WHAT THIS ADDS
-- --------------
-- One status value. `archived` means we hold the page. `failed` means we could
-- not fetch it. Neither of those is true of a stored Cloudflare interstitial:
-- the bytes ARE archived (they are the evidence of the block) but they are not
-- the article, and nothing about them will improve on a retry with the same
-- client. `blocked_challenge` is that third outcome, recorded with the CAS
-- object_ref and sha256 like any other stored body, and with the detected tell
-- in `last_error` (`blocked_by_challenge: <tell>`).
--
-- WHAT THIS DELIBERATELY DOES NOT COVER
-- ------------------------------------
-- An edge that answers 401/403/429 with no body keeps `status = 'failed'`.
-- That refusal may well clear if an operator turns `LEGBA_FETCH_IMPERSONATE`
-- on (the same lane's option (a)), and `failed` is the only status the
-- candidate query re-attempts — a terminal status would freeze those rows out
-- of the re-attempt the flag exists to enable. They are still visible: the
-- `blocked_by_challenge` counter on the run's finding counts them, and
-- `last_error` names the status.
--
-- The replacement CHECK is a strict superset of migration 0112's, so no
-- existing row can violate it.

ALTER TABLE public.evidence_archive
    DROP CONSTRAINT IF EXISTS evidence_archive_status_check;

ALTER TABLE public.evidence_archive
    ADD CONSTRAINT evidence_archive_status_check CHECK (status IN (
        'archived',
        'failed',
        'skipped_license',
        'skipped_size',
        -- R-3b: web-retrieval origin + unreviewed licence ⇒ bytes NOT archived.
        'skipped_license_unreviewed',
        -- (a′): an anti-bot challenge/interstitial stood in front of the page.
        -- Bytes ARE stored; they are just not the article.
        'blocked_challenge'
    ));

COMMENT ON CONSTRAINT evidence_archive_status_check ON public.evidence_archive IS
    'Closed status vocabulary. ''blocked_challenge'' (mig 0196) = a Cloudflare/'
    'DataDome-style interstitial was archived instead of the article; the tell '
    'is in last_error as ''blocked_by_challenge: <tell>''. An edge 401/403/429 '
    'with no body stays ''failed'' so it remains retryable.';
