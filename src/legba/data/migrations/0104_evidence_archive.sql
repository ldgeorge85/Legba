-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0104_evidence_archive.sql
--
-- P2-1 (program §A3, planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md): NATIVE
-- cited-evidence archival — the bookkeeping sidecar for the `evidence_archiver`
-- deterministic analyst. The archiver finds signals CITED by verified findings
-- (derived_from of findings with a passing faithfulness critique), fetches the
-- ORIGINAL bytes behind `canonical_url` (SSRF egress guard + the P2-2 license
-- gate applied), stores them CONTENT-ADDRESSED on the archive volume at
-- `{LEGBA_ARCHIVE_ROOT}/{sha256[:2]}/{sha256}`, and stamps the signal row —
-- so the receipt chain terminates in OUR verifiable copy, not a rotting URL.
--
-- WHY A SIDECAR (not more columns on `signals`) — a DIFFERENT rationale from
-- the 0098/0099/0103 recomputable-readout sidecars: this state is PRIMARY (the
-- archive record is evidence bookkeeping, not derivable), but it belongs to the
-- ARCHIVAL PLANE, not the ingest row: per-fetch status / attempt caps / the
-- license verdict / the media leg are lifecycle state of the archival job, and
-- `signals` (13 indexes, hot ingest path) should not grow six such columns for
-- the ~2% of rows that are cited evidence. The ONE schema-designated seam the
-- signal row does carry — `signals.object_ref` (existing since the pivot, empty
-- until now) — IS stamped on success, with the content address
-- `cas:sha256/<hex>`, so every read surface can derive `archived` +
-- `archive_sha256` from the EXISTING column with no join and no dependency on
-- this table. This table is the archival plane's own ledger.
--
-- IDENTITY + LIFECYCLE: one row per signal (`signal_id` PRIMARY KEY),
-- upserted by the archiver. Deliberately NO foreign key (the 0094/0099/0103
-- rationale, plus one of our own): archived objects are EVIDENCE — the archive
-- record and the bytes must outlive any future signals-row purge, so the
-- ledger must not cascade or block on signals lifecycle. (The archiver also
-- upgrades archived signals to `retention_class='evidence_hold'`, which the
-- signals_retention purge already exempts — belt and braces.)
--
-- `status` vocabulary (closed, enforced by CHECK):
--   archived         — bytes stored + hash recorded + signals.object_ref stamped.
--   failed           — fetch/store failed; retried until `attempts` reaches the
--                      archiver's cap (egress-blocked URLs are capped at once —
--                      a private-address target can never become fetchable).
--   skipped_license  — the P2-2 license gate refused retention for this
--                      source's license_class. Recorded (NEVER silent) with the
--                      class that triggered it, so a future policy flip can
--                      re-evaluate exactly these rows.
--   skipped_size     — the object exceeded the archiver's size cap. Recorded so
--                      the budget is never re-burned on it; a future cap raise
--                      can re-evaluate these rows.
--
-- RETENTION HONESTY: nothing deletes from this table and nothing deletes the
-- stored bytes — archived objects are evidence. The future retention /
-- `evidence_hold` interplay (expiry sweeps honoring media_ref_expires_at,
-- object-store GC, operator-gated erasure for a LIC-2 policy flip) is a
-- declared seam (docs/SEAMS.md), not built here.
--
-- SAFETY (idempotent, additive, forward-only): CREATE TABLE/INDEX IF NOT
-- EXISTS only; no existing table is touched; re-apply and cold-start are both
-- no-ops. NUMBERING: 0104 is this branch's assigned slot; the runner discovers
-- by sorted glob, so a gap is harmless (same note as 0099/0103). The runner
-- wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0091-0103).

CREATE TABLE IF NOT EXISTS public.evidence_archive (
    signal_id        uuid PRIMARY KEY,          -- 1:1 with signals(id); NO FK on purpose (evidence outlives the row)
    status           text NOT NULL CHECK (status IN
                        ('archived', 'failed', 'skipped_license', 'skipped_size')),
    -- The content address of OUR copy of the original bytes:
    --   object_ref = 'cas:sha256/<hex>'  →  {LEGBA_ARCHIVE_ROOT}/<hex[:2]>/<hex>
    -- (relative content address, so the archive root can move / become an
    -- object store without rewriting rows). Mirrored onto signals.object_ref.
    object_ref       text,
    sha256           text,                      -- hex sha256 of the ORIGINAL fetched bytes (the receipt anchor)
    size_bytes       bigint,
    content_type     text,                      -- upstream Content-Type at fetch time
    fetched_url      text,                      -- the URL actually fetched (canonical_url at archive time)
    -- Media leg (signals.media_ref bytes, fetched + stored the same way; NO
    -- processing here — the process_media plane backfills extraction later).
    media_object_ref text,
    media_sha256     text,
    media_size_bytes bigint,
    -- The license_class the P2-2 gate evaluated for this signal at archive/skip
    -- time (payload stamp → raw_provenance fallback; NULL = unknown/unset,
    -- which the default posture ARCHIVES — recorded so a policy flip can
    -- re-evaluate without guessing what the gate saw).
    license_class    text,
    text_extracted   boolean NOT NULL DEFAULT false,  -- Trafilatura main-text extraction succeeded (bytes are the archive either way)
    attempts         int NOT NULL DEFAULT 0,
    last_error       text,
    archived_at      timestamptz,               -- NULL until status='archived'
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Operator/eval visibility: "what did the gate skip, and why" is a status-first
-- read; the partial index keeps it off a full scan as the ledger grows (the
-- selection anti-join itself is a PK lookup and needs nothing extra).
CREATE INDEX IF NOT EXISTS evidence_archive_status_idx
    ON public.evidence_archive (status, updated_at DESC)
    WHERE status <> 'archived';
