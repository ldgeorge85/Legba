-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0249_ingestion_requests.sql
--
-- I-1: THE INGESTION INTAKE — one queue of ingestion requests
-- (legba.data.intake_store submits and admits, legba.runtime.intake_drain
-- claims and runs them through the same acquisition code and sinks a poll
-- uses).
--
--   * `ingestion_requests` — one row per request: WHAT to fetch (an address;
--     a source plus a window start to re-poll; or "re-check this stored
--     version" of a fetched page), UNDER WHICH pipeline (a source descriptor
--     id whose scope, enrichment chain and sink the result lands under) and
--     sink (`document` / `observation` = the pipeline's own family, or
--     `receipt` = keep the fetched version only, write no records), WHY (a
--     closed reason list), WHO asked (`requested_by`, a principal string;
--     `on_behalf_of` is the person a request flowed through an agent for,
--     NULL until the identity design supplies real principals), the parent
--     request or signal it came from, depth, priority, not-before, expiry and
--     budget class. States: queued -> claimed -> done | failed (retry policy:
--     back to queued with a back-off until max_attempts) ; queued -> expired ;
--     refused at submit (with its reason, counted). A request satisfied from
--     a stored copy at submit is `done` with `satisfied_from` set and no
--     fetch.
--   * `ingestion_request_records` — which records (signals, observations) a
--     request's run wrote. INSERT-ONLY by convention (the drain writes it
--     once, at completion).
--
-- SAFETY (idempotent, additive, forward-only): two NEW tables and their
-- indexes, CREATE ... IF NOT EXISTS only. No existing table is touched. The
-- runner wraps this file in its own transaction.

CREATE TABLE IF NOT EXISTS public.ingestion_requests (
    id                 uuid PRIMARY KEY,
    target_kind        text NOT NULL CHECK (target_kind IN ('address', 'source_window', 'stored_version')),
    address            text,                 -- as given (stored_version: the version's URL)
    address_canonical  text,                 -- legba.data._url_canon.canonical_url(address)
    host               text,                 -- politeness key (lowercased host); NULL = none known
    source_id          text,                 -- source_window: the source to re-poll
    window_start       timestamptz,          -- source_window: the cursor the re-poll starts from
    version_id         bigint REFERENCES public.fetch_versions (id),   -- stored_version
    pipeline_source_id text NOT NULL,        -- the source descriptor the run executes under
    sink               text NOT NULL CHECK (sink IN ('document', 'observation', 'receipt')),
    reason             text NOT NULL CHECK (reason IN (
                         'scheduled', 'followed_reference', 'agent_request',
                         'drift_recheck', 'backfill', 'operator_request', 'user_request')),
    budget_class       text NOT NULL CHECK (budget_class IN (
                         'routine', 'follow', 'agent', 'drift', 'backfill', 'operator')),
    requested_by       text NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 256),
    on_behalf_of       text CHECK (on_behalf_of IS NULL OR length(on_behalf_of) BETWEEN 1 AND 256),
    note               text CHECK (note IS NULL OR length(note) <= 2000),
    link_kind          text,                 -- followed_reference: article | anchor | document
    parent_request_id  uuid REFERENCES public.ingestion_requests (id),
    parent_signal_id   uuid,
    depth              integer NOT NULL DEFAULT 0 CHECK (depth >= 0),
    priority           smallint NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    not_before         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz,
    dedupe_key         text NOT NULL,
    state              text NOT NULL DEFAULT 'queued' CHECK (state IN (
                         'queued', 'claimed', 'done', 'refused', 'failed', 'expired')),
    refusal_reason     text,
    satisfied_from     text CHECK (satisfied_from IS NULL OR satisfied_from IN ('stored_version', 'stored_signal')),
    attempts           integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts       integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    claim_token        uuid,
    claimed_by         text,
    claimed_at         timestamptz,
    lease_expires_at   timestamptz,
    last_error         text,
    result             jsonb,
    result_version_id  bigint REFERENCES public.fetch_versions (id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    -- A refused request keeps what it was asked for in `result` (a refused
    -- stored_version names a version that does not exist, so it cannot
    -- reference it); every admitted request carries its target.
    CONSTRAINT ingestion_requests_target_shape CHECK (
        state = 'refused'
        OR (target_kind = 'address' AND address IS NOT NULL)
        OR (target_kind = 'source_window' AND source_id IS NOT NULL)
        OR (target_kind = 'stored_version' AND version_id IS NOT NULL AND address IS NOT NULL)),
    CONSTRAINT ingestion_requests_refused_has_reason CHECK (
        (state = 'refused') = (refusal_reason IS NOT NULL)),
    CONSTRAINT ingestion_requests_claimed_has_lease CHECK (
        state <> 'claimed' OR (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);

-- The drain's claim order (priority, then not-before, then age) over the
-- queued set only.
CREATE INDEX IF NOT EXISTS ingestion_requests_claim_idx
    ON public.ingestion_requests (priority DESC, not_before, created_at)
    WHERE state = 'queued';
-- One pending request per (target, pipeline, sink): a second submit while
-- the first is queued or claimed is refused `duplicate_pending`.
CREATE UNIQUE INDEX IF NOT EXISTS ingestion_requests_pending_key
    ON public.ingestion_requests (dedupe_key)
    WHERE state IN ('queued', 'claimed');
-- Lease reclaim (a claimed request whose worker died).
CREATE INDEX IF NOT EXISTS ingestion_requests_lease_idx
    ON public.ingestion_requests (lease_expires_at)
    WHERE state = 'claimed';
-- Queued-request expiry.
CREATE INDEX IF NOT EXISTS ingestion_requests_expiry_idx
    ON public.ingestion_requests (expires_at)
    WHERE state = 'queued' AND expires_at IS NOT NULL;
-- Per-host politeness at claim (concurrency + minimum interval) and the
-- per-host daily admission cap.
CREATE INDEX IF NOT EXISTS ingestion_requests_host_claimed_idx
    ON public.ingestion_requests (host, claimed_at)
    WHERE host IS NOT NULL AND claimed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ingestion_requests_host_created_idx
    ON public.ingestion_requests (host, created_at)
    WHERE host IS NOT NULL;
-- Daily budgets: per requester, per budget class; the windowed counts.
CREATE INDEX IF NOT EXISTS ingestion_requests_requester_idx
    ON public.ingestion_requests (requested_by, created_at);
CREATE INDEX IF NOT EXISTS ingestion_requests_class_idx
    ON public.ingestion_requests (budget_class, created_at);
CREATE INDEX IF NOT EXISTS ingestion_requests_created_idx
    ON public.ingestion_requests (created_at);
-- Per-parent fan-out caps and the parent links.
CREATE INDEX IF NOT EXISTS ingestion_requests_parent_request_idx
    ON public.ingestion_requests (parent_request_id)
    WHERE parent_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ingestion_requests_parent_signal_idx
    ON public.ingestion_requests (parent_signal_id)
    WHERE parent_signal_id IS NOT NULL;

COMMENT ON TABLE public.ingestion_requests IS
    'I-1: the ingestion intake. One row per request to fetch an address, re-poll '
    'a source window, or re-check a stored page version, under a named pipeline '
    '(source descriptor) and sink. Admission refusals are rows (state refused, '
    'refusal_reason); the drain claims with SKIP LOCKED under a lease.';

CREATE TABLE IF NOT EXISTS public.ingestion_request_records (
    request_id   uuid NOT NULL REFERENCES public.ingestion_requests (id),
    record_kind  text NOT NULL CHECK (record_kind IN ('signal', 'observation')),
    record_id    uuid NOT NULL,
    linked_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ingestion_request_records_pkey PRIMARY KEY (request_id, record_kind, record_id)
);

CREATE INDEX IF NOT EXISTS ingestion_request_records_record_idx
    ON public.ingestion_request_records (record_id);
