-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0224_evidence_archive_expired_uncited.sql
--
-- A terminal status for "the stored bytes were removed by the retention sweep".
--
-- The `archive_retention` sweep (LEGBA_ARCHIVE_RETENTION, default OFF) removes the
-- stored object of an archive row whose signal is cited by no analyst output, fact
-- or event and is older than the retention window, and KEEPS the row: the url,
-- the sha256 receipt anchor and the licence class stay, so what was held and why
-- it went is still answerable. `archived` would now be false (the bytes are gone)
-- and `failed` would be wrong (it was retryable and the candidate query
-- re-attempts it), so the row needs a status of its own.
--
-- The replacement CHECK is a strict superset of migration 0200's, so no existing
-- row can violate it. Two partial indexes carry the sweep's "does another live row
-- still use this content address" probe (objects are content addressed and
-- shared). Additive and forward-only; re-apply is a no-op.

ALTER TABLE public.evidence_archive
    DROP CONSTRAINT IF EXISTS evidence_archive_status_check;

ALTER TABLE public.evidence_archive
    ADD CONSTRAINT evidence_archive_status_check CHECK (status IN (
        'archived',
        'failed',
        'skipped_license',
        'skipped_size',
        'skipped_license_unreviewed',
        'blocked_challenge',
        -- The retention sweep removed the stored object(s); the row is kept.
        'expired_uncited'
    ));

COMMENT ON CONSTRAINT evidence_archive_status_check ON public.evidence_archive IS
    'Closed status vocabulary. ''blocked_challenge'' (mig 0200) = an interstitial '
    'was archived instead of the article. ''expired_uncited'' (mig 0224) = the '
    'archive_retention sweep removed the stored bytes of an uncited row past the '
    'retention window; object_ref and sha256 are kept as the receipt.';

CREATE INDEX IF NOT EXISTS evidence_archive_object_ref_idx
    ON public.evidence_archive (object_ref)
    WHERE object_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS evidence_archive_media_object_ref_idx
    ON public.evidence_archive (media_object_ref)
    WHERE media_object_ref IS NOT NULL;
