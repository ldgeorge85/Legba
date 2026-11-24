-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0245_fetch_receipts.sql
--
-- N-L: FETCH RECEIPTS — the bytes a poll actually received, kept as a VERSION
-- SEQUENCE per canonical address (legba.runtime.fetch_receipts).
--
--   * `fetch_versions` — one row per (canonical address, content sha256) for
--     what WE fetched (receipt_kind 'fetch'), or per (source, body sha256) for
--     an envelope an upstream PUSHED (receipt_kind 'inbound', no address —
--     the version/drift model keys on an address and does not apply). The
--     address is `legba.data._url_canon.canonical_url(url)`, the platform's
--     existing document-identity canonicalisation. An identical re-fetch
--     bumps `last_seen_at` / `fetch_count` / `last_source_id` and stores
--     nothing new; a different body is a new row (a new version). Nothing is
--     overwritten. The bytes live in the evidence store (content-addressed,
--     zstd at rest — legba.data.archive_store), referenced by `object_ref`.
--     For a PAGE (an article fetched for its content, as opposed to a feed or
--     API response) the main text the platform already extracts (Trafilatura,
--     the evidence archiver's extractor) is stored with its own sha256, so a
--     later comparison can ignore page furniture.
--   * `fetch_version_records` — which records (signals, observations) were
--     derived from which version, by role (`response` = the feed/API body the
--     record was parsed from, `page` = the ingest-time copy of its article).
--     INSERT-ONLY (trigger): the link a record was built from never moves.
--   * `fetch_drift_observations` — a plain row each time a second fetch of a
--     page address yields different extracted text, or no longer answers
--     ("gone"). Raw material only: no classification, no flags, no alerts.
--   * `fetch_version_tombstones` — what the retention runner pruned (bytes
--     only; the version row, hash, metadata and extracted text stay).
--   * `source_poll_outcomes.sink_counts` — a non-document sink's per-poll
--     counts. NULL for every document source.
--
-- SAFETY (idempotent, additive, forward-only): CREATE ... IF NOT EXISTS / ADD
-- COLUMN IF NOT EXISTS only; the one existing table touched gains a NULLable
-- column. The runner wraps this file in its own transaction.

CREATE TABLE IF NOT EXISTS public.fetch_versions (
    id                bigserial PRIMARY KEY,
    -- 'fetch' = something WE fetched (a poll response, a page, the archiver's
    -- re-fetch): a version of a canonical address. 'inbound' = an envelope an
    -- upstream PUSHED to us (webhook): it has no address, so the version /
    -- drift model does not apply — it is deduplicated per source by body hash.
    receipt_kind      text NOT NULL DEFAULT 'fetch' CHECK (receipt_kind IN ('fetch', 'inbound')),
    address           text,
    url               text NOT NULL,              -- the exact URL first seen (inbound: the webhook path)
    sha256            text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    hash_basis        text NOT NULL CHECK (hash_basis IN ('entity', 'wire')),
    role              text NOT NULL CHECK (role IN ('response', 'page', 'archiver', 'inbound')),
    headers           jsonb,                      -- inbound: the request headers, secret-bearing values replaced by their sha256
    first_source_id   text,                       -- NULL for an archiver fetch
    last_source_id    text,
    status            integer NOT NULL,
    content_type      text,
    content_encoding  text,
    size_bytes        bigint NOT NULL CHECK (size_bytes >= 0),
    object_ref        text,                       -- cas:sha256/<hex> when the bytes are held
    store_status      text NOT NULL CHECK (store_status IN
                        ('stored', 'existed', 'too_large', 'store_unavailable', 'not_stored')),
    stored_size       bigint,                     -- bytes at rest when this row stored them
    text_sha256       text CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-f]{64}$'),
    text_chars        integer,
    extracted_text    text,
    first_seen_at     timestamptz NOT NULL,
    last_seen_at      timestamptz NOT NULL,
    fetch_count       integer NOT NULL DEFAULT 1 CHECK (fetch_count >= 1),
    bytes_pruned_at   timestamptz,
    CONSTRAINT fetch_versions_address_sha_unique UNIQUE (address, sha256),
    CONSTRAINT fetch_versions_kind_address CHECK (
        (receipt_kind = 'fetch' AND address IS NOT NULL AND role <> 'inbound')
        OR (receipt_kind = 'inbound' AND address IS NULL AND role = 'inbound'
            AND first_source_id IS NOT NULL)),
    CONSTRAINT fetch_versions_seen_ordered CHECK (last_seen_at >= first_seen_at)
);

-- An inbound envelope is one receipt per (source, body hash): a redelivery
-- bumps last_seen_at / fetch_count and stores nothing new.
CREATE UNIQUE INDEX IF NOT EXISTS fetch_versions_inbound_unique
    ON public.fetch_versions (first_source_id, sha256)
    WHERE receipt_kind = 'inbound';
CREATE INDEX IF NOT EXISTS fetch_versions_sha256_idx
    ON public.fetch_versions (sha256);
CREATE INDEX IF NOT EXISTS fetch_versions_address_seen_idx
    ON public.fetch_versions (address, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS fetch_versions_prunable_idx
    ON public.fetch_versions (last_seen_at)
    WHERE object_ref IS NOT NULL AND bytes_pruned_at IS NULL;

COMMENT ON TABLE public.fetch_versions IS
    'N-L: one row per (canonical address, content sha256) a source poll (or '
    'the archiver''s fallback re-fetch) received. sha256 = the decoded entity '
    'body the parser consumed (hash_basis=entity) or, above the capture cap, '
    'the wire bytes (wire). Identical re-fetch = last_seen_at/fetch_count bump.';

CREATE TABLE IF NOT EXISTS public.fetch_version_records (
    record_kind   text NOT NULL CHECK (record_kind IN ('signal', 'observation')),
    record_id     uuid NOT NULL,
    role          text NOT NULL CHECK (role IN ('response', 'page', 'inbound')),
    version_id    bigint NOT NULL REFERENCES public.fetch_versions (id),
    sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    linked_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fetch_version_records_pkey PRIMARY KEY (record_kind, record_id, role)
);

CREATE INDEX IF NOT EXISTS fetch_version_records_version_idx
    ON public.fetch_version_records (version_id);

CREATE OR REPLACE FUNCTION public.fetch_version_records_insert_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'fetch_version_records is insert-only: the version a record was built from never moves (%)', TG_OP
        USING ERRCODE = 'restrict_violation';
END $$;

DROP TRIGGER IF EXISTS fetch_version_records_insert_only ON public.fetch_version_records;
CREATE TRIGGER fetch_version_records_insert_only
    BEFORE UPDATE OR DELETE ON public.fetch_version_records
    FOR EACH ROW EXECUTE FUNCTION public.fetch_version_records_insert_only();

CREATE TABLE IF NOT EXISTS public.fetch_drift_observations (
    id                   bigserial PRIMARY KEY,
    address              text NOT NULL,
    source_id            text,
    observed_by          text NOT NULL CHECK (observed_by IN ('ingest', 'archiver')),
    earlier_version_id   bigint NOT NULL REFERENCES public.fetch_versions (id),
    later_version_id     bigint REFERENCES public.fetch_versions (id),   -- NULL when gone
    earlier_fetched_at   timestamptz NOT NULL,
    later_fetched_at     timestamptz NOT NULL,
    earlier_sha256       text NOT NULL,
    later_sha256         text,
    earlier_text_sha256  text,
    later_text_sha256    text,
    earlier_size         bigint,
    later_size           bigint,
    later_status         integer,
    later_gone           boolean NOT NULL,
    observed_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fetch_drift_gone_has_no_version
        CHECK (later_gone = (later_version_id IS NULL))
);

CREATE INDEX IF NOT EXISTS fetch_drift_observations_address_idx
    ON public.fetch_drift_observations (address, observed_at DESC);

CREATE TABLE IF NOT EXISTS public.fetch_version_tombstones (
    id              bigserial PRIMARY KEY,
    version_id      bigint NOT NULL UNIQUE REFERENCES public.fetch_versions (id),
    sha256          text NOT NULL,
    stored_size     bigint,
    pruned_at       timestamptz NOT NULL DEFAULT now(),
    reason          text NOT NULL,
    policy_version  text NOT NULL
);

ALTER TABLE public.source_poll_outcomes
    ADD COLUMN IF NOT EXISTS sink_counts jsonb,
    ADD COLUMN IF NOT EXISTS fetch_protocol jsonb;

COMMENT ON COLUMN public.source_poll_outcomes.fetch_protocol IS
    'N-L fetch.http_version: written only when the poll was not the silent '
    'default (an HTTP/1.1 fetch or a fallback), e.g. {"http_version": "auto", '
    '"used": {"h2": 1, "http/1.1": 1}, "fallbacks": 1, "markers": ["header:cf-mitigated"]}.';

COMMENT ON COLUMN public.source_poll_outcomes.sink_counts IS
    'N-L: per-poll counts of a non-document sink, e.g. {"observation": '
    '{"written": 3, "unchanged": 193, "revised": 1, "rejected": 0}}. NULL for '
    'document sources (their counts are signals_written / reserve_unchanged).';
