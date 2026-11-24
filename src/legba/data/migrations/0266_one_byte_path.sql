-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0266_one_byte_path.sql
--
-- ONE BYTE PATH (lane B-1). `fetch_versions` becomes the one register of
-- fetched bytes: every new signal links to what its poll received, and the
-- older `evidence_archive` objects are registered into it BY HASH (no byte is
-- copied, moved or deleted — `scripts/register_evidence_archive.py`).
--
-- WHAT THIS ADDS (additive, nullable, no data change)
-- ---------------------------------------------------
-- * `fetch_version_records.locator` (jsonb) — WHERE in the linked version the
--   record's item sits, when the version is a whole feed / file / API body:
--   e.g. {"item_id": "<guid>", "item_link": "<url>"} for a feed item,
--   {"format": "zip_csv", "key_column": "GLOBALEVENTID", "key": "123"} for a
--   GDELT export row, {"whole": true} when the capture IS the item (a
--   Telegram message as received). NULL = no locator recorded (every link
--   written before this lane). Read by `legba.data.byte_path.item_as_received`.
-- * `fetch_version_records.role` admits `archived`: the record's bytes were
--   ARCHIVED LATER (the evidence archiver's copy, fetched at archive time),
--   never "received at poll". The CHECK is replaced by a named one with the
--   same list plus `archived`, NOT VALID (every existing row satisfies the
--   older, narrower list).
-- * `fetch_version_records.register_batch` (text) — the register run that
--   wrote the link (NULL for every link a poll writes). Rollback of a run =
--   these rows (the table is insert-only by trigger: a rollback is an
--   operator act that lifts the trigger for that batch, by name).
-- * `fetch_versions.registered` (jsonb) — set only on a version the register
--   script inserted, or whose missing `object_ref` it filled from the
--   archive's existing object: {"from": "evidence_archive", "batch": "...",
--   "action": "inserted" | "object_ref_filled", "at": "..."}. A registered
--   row's `status` 200 is INFERRED from the archive row's status `archived`
--   (the archive kept no HTTP status), and `first_seen_at` is the ARCHIVE
--   time, not a poll time.
--
-- The three labels a reader can say about a signal's bytes
-- (`legba.data.byte_path.evidence_state`): "received bytes" (a response /
-- page / inbound link to a version whose bytes are held), "archived later"
-- (an `archived` link, or an `archiver`-role version), "no bytes kept".
--
-- SAFETY: idempotent (ADD COLUMN IF NOT EXISTS, drop-if-exists then add,
-- CREATE INDEX IF NOT EXISTS), no BEGIN/COMMIT (the runner wraps the file),
-- forward-only, no rewrite of existing rows.

ALTER TABLE public.fetch_version_records
    ADD COLUMN IF NOT EXISTS locator jsonb,
    ADD COLUMN IF NOT EXISTS register_batch text;

ALTER TABLE public.fetch_version_records
    DROP CONSTRAINT IF EXISTS fetch_version_records_role_check;

ALTER TABLE public.fetch_version_records
    DROP CONSTRAINT IF EXISTS ck_fetch_version_records_role;

ALTER TABLE public.fetch_version_records
    ADD CONSTRAINT ck_fetch_version_records_role
    CHECK (role IN ('response', 'page', 'inbound', 'archived')) NOT VALID;

CREATE INDEX IF NOT EXISTS fetch_version_records_register_batch_idx
    ON public.fetch_version_records (register_batch)
    WHERE register_batch IS NOT NULL;

ALTER TABLE public.fetch_versions
    ADD COLUMN IF NOT EXISTS registered jsonb;

COMMENT ON COLUMN public.fetch_version_records.locator IS
    'B-1: where the record''s item sits inside the linked version (feed item '
    'id/link, a zip-CSV key, or {"whole": true}). NULL = not recorded.';

COMMENT ON COLUMN public.fetch_version_records.register_batch IS
    'B-1: the register run (scripts/register_evidence_archive.py) that wrote '
    'this link; NULL for links a poll wrote.';

COMMENT ON COLUMN public.fetch_versions.registered IS
    'B-1: set when the register script inserted this version from an '
    'evidence_archive object (or filled its object_ref from one): bytes '
    'ARCHIVED LATER, fetched at the archive time, never received at poll.';
