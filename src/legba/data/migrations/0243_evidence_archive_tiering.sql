-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0243_evidence_archive_tiering.sql
--
-- N-K: the evidence archive's COLD tier ledger (legba.data.archive_tier).
--
-- WHAT THIS IS NOT NEEDED FOR: compression. Archive objects may now rest
-- zstd-compressed; the codec is detected from the stored bytes' frame magic
-- (legba.data.archive_codec), `evidence_archive.sha256` / `size_bytes` keep
-- describing the ORIGINAL bytes, and no row changes. Compression and the
-- in-place compaction job (scripts/archive_compact.py) run with or without
-- this migration.
--
-- WHAT IT IS NEEDED FOR: retiring objects older than the hot window to a cold
-- backend (scripts/archive_tier.py refuses to run until both tables exist).
--
--   * `evidence_archive_bundles` — one row per uploaded, read-back-verified
--     monthly bundle (a tar of zstd members + an index; the key is the tar's
--     own sha256, so a bundle is content-addressed and immutable).
--   * `evidence_archive_cold` — one row per CONTENT ADDRESS that has a cold
--     copy: the bundle and the member's byte range. Keyed by sha256, not by
--     signal: one stored object serves every row citing the same bytes (and
--     the media leg), so the location cannot disagree with itself, and every
--     row reaches it through the sha256/object_ref it already carries.
--     `hot_removed_at` is stamped when the local copy is removed — after the
--     bundle verified, never before.
--
-- Nothing deletes from either table; a cold copy is evidence like a hot one.
--
-- SAFETY (idempotent, additive, forward-only): CREATE ... IF NOT EXISTS only;
-- no existing table is touched. The runner wraps this file in its own
-- transaction (no inline BEGIN/COMMIT). 0241 stays reserved (C-3, see 0242).

CREATE TABLE IF NOT EXISTS public.evidence_archive_bundles (
    bundle_key      text PRIMARY KEY,           -- bundles/<YYYY-MM>/<bundle_sha256>.tar
    backend         text NOT NULL,              -- the cold backend's name at upload (cold-s3 | cold-local)
    month           text NOT NULL CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
    bundle_sha256   text NOT NULL,
    member_count    integer NOT NULL CHECK (member_count >= 0),
    stored_bytes    bigint NOT NULL,            -- sum of member bytes at rest
    original_bytes  bigint NOT NULL,            -- sum of the ORIGINAL sizes
    verified_at     timestamptz NOT NULL,       -- read back + every member re-hashed
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.evidence_archive_cold (
    sha256          text PRIMARY KEY,           -- the ORIGINAL bytes' sha256 (the CAS address)
    bundle_key      text NOT NULL REFERENCES public.evidence_archive_bundles (bundle_key),
    member_offset   bigint NOT NULL CHECK (member_offset >= 0),
    member_length   bigint NOT NULL CHECK (member_length >= 0),
    stored_codec    text NOT NULL CHECK (stored_codec IN ('identity', 'zstd')),
    tiered_at       timestamptz NOT NULL DEFAULT now(),
    hot_removed_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_evidence_archive_cold_bundle
    ON public.evidence_archive_cold (bundle_key);
