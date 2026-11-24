-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0252_evidence_drift.sql
--
-- D-1: SOURCE DRIFT — classify a page's change, flag what relied on it, and
-- record "re-derive this with the changed text" for a later lane
-- (legba.data.evidence_drift_classify / evidence_drift_store / _walk).
--
--   * `evidence_drift_classifications` — ONE row per `fetch_drift_observations`
--     row: the class from a closed list (identical_text, cosmetic, appended,
--     minor_edit, title_changed, substantive_edit, retracted_or_replaced,
--     gone; `unclassifiable` when neither version kept text), the rule that
--     fired, the classifier version and the evidence (a compact diff), plus
--     the bounded dependant walk's counts. INSERT-ONLY (trigger).
--   * `evidence_drift_flags` — one row per DEPENDANT of a superseded version
--     when the class is substantive_edit / title_changed /
--     retracted_or_replaced / gone: what relied on it (a signal or
--     observation built from the version, a finding / journal entry /
--     consult turn / forecast evidence citing that record, an output derived
--     from such a finding), how deep, through what, the cited span and
--     whether it still appears in the new text. Never the dependant itself:
--     nothing here rewrites or deletes what it flags. Lifecycle open ->
--     reviewed -> resolved (unchanged_in_substance | dependant_corrected |
--     dependant_withdrawn) with a principal string and a note at each step;
--     identity columns never change and a flag is never deleted (trigger).
--     Idempotent: one flag per (drift row, dependant).
--   * `evidence_rederive_requests` — "this dependant should be re-derived
--     with the changed text": a row a later lane (the statement layer) acts
--     on. Nothing in this lane re-runs anything.
--   * an index on `ingestion_requests` for the drift re-check batch's
--     per-address back-off.
--
-- SAFETY (idempotent, additive, forward-only): three NEW tables, their
-- indexes and triggers, one new index on an existing table. CREATE ... IF
-- NOT EXISTS only. The runner wraps this file in its own transaction.

CREATE TABLE IF NOT EXISTS public.evidence_drift_classifications (
    id                   bigserial PRIMARY KEY,
    drift_id             bigint NOT NULL UNIQUE REFERENCES public.fetch_drift_observations (id),
    address              text NOT NULL,
    source_id            text,
    host                 text,
    earlier_version_id   bigint NOT NULL REFERENCES public.fetch_versions (id),
    later_version_id     bigint REFERENCES public.fetch_versions (id),
    earlier_fetched_at   timestamptz NOT NULL,
    later_fetched_at     timestamptz NOT NULL,
    drift_class          text NOT NULL CHECK (drift_class IN (
                           'identical_text', 'cosmetic', 'appended', 'minor_edit',
                           'title_changed', 'substantive_edit', 'retracted_or_replaced',
                           'gone', 'unclassifiable')),
    rule                 text NOT NULL,
    classifier_version   text NOT NULL,
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb,
    dependants_found     integer NOT NULL DEFAULT 0 CHECK (dependants_found >= 0),
    flags_written        integer NOT NULL DEFAULT 0 CHECK (flags_written >= 0),
    inputs_only          integer NOT NULL DEFAULT 0 CHECK (inputs_only >= 0),
    walk_depth           smallint NOT NULL DEFAULT 0 CHECK (walk_depth BETWEEN 0 AND 5),
    walk_truncated       boolean NOT NULL DEFAULT false,
    classified_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS evidence_drift_classifications_later_idx
    ON public.evidence_drift_classifications (later_fetched_at DESC);
CREATE INDEX IF NOT EXISTS evidence_drift_classifications_address_idx
    ON public.evidence_drift_classifications (address, later_fetched_at DESC);
CREATE INDEX IF NOT EXISTS evidence_drift_classifications_source_idx
    ON public.evidence_drift_classifications (source_id, classified_at DESC);
CREATE INDEX IF NOT EXISTS evidence_drift_classifications_class_idx
    ON public.evidence_drift_classifications (drift_class, classified_at DESC);

CREATE OR REPLACE FUNCTION public.evidence_drift_classifications_insert_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evidence_drift_classifications is insert-only: a classification is a record of what was decided (%)', TG_OP
        USING ERRCODE = 'restrict_violation';
END $$;

DROP TRIGGER IF EXISTS evidence_drift_classifications_insert_only ON public.evidence_drift_classifications;
CREATE TRIGGER evidence_drift_classifications_insert_only
    BEFORE UPDATE OR DELETE ON public.evidence_drift_classifications
    FOR EACH ROW EXECUTE FUNCTION public.evidence_drift_classifications_insert_only();

COMMENT ON TABLE public.evidence_drift_classifications IS
    'D-1: the class of one fetch_drift_observations row (rule-based, '
    'legba.data.evidence_drift_classify), its evidence and the bounded '
    'dependant walk counts. Insert-only.';

CREATE TABLE IF NOT EXISTS public.evidence_drift_flags (
    id                   bigserial PRIMARY KEY,
    classification_id    bigint NOT NULL REFERENCES public.evidence_drift_classifications (id),
    drift_id             bigint NOT NULL REFERENCES public.fetch_drift_observations (id),
    address              text NOT NULL,
    source_id            text,
    earlier_version_id   bigint NOT NULL REFERENCES public.fetch_versions (id),
    later_version_id     bigint REFERENCES public.fetch_versions (id),
    drift_class          text NOT NULL CHECK (drift_class IN (
                           'substantive_edit', 'title_changed', 'retracted_or_replaced', 'gone')),
    dependant_kind       text NOT NULL CHECK (dependant_kind IN (
                           'signal', 'observation', 'finding', 'journal_entry',
                           'journal_proposal', 'consult_turn', 'inquiry',
                           'claim_contention', 'forecast_resolution',
                           'forecast_resolution_proposal', 'scorecard_entry',
                           'derived_output')),
    dependant_id         text NOT NULL CHECK (length(dependant_id) BETWEEN 1 AND 256),
    depth                smallint NOT NULL CHECK (depth BETWEEN 0 AND 5),
    via_kind             text,                 -- what it relied on: a record kind, or 'finding'
    via_id               text,
    record_version_id    bigint REFERENCES public.fetch_versions (id),
    cited_span           text CHECK (cited_span IS NULL OR length(cited_span) <= 4000),
    span_check           jsonb,
    span_in_earlier      boolean,
    span_in_later        boolean,
    state                text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'reviewed', 'resolved')),
    reviewed_at          timestamptz,
    reviewed_by          text CHECK (reviewed_by IS NULL OR length(reviewed_by) BETWEEN 1 AND 256),
    review_note          text CHECK (review_note IS NULL OR length(review_note) <= 2000),
    resolved_at          timestamptz,
    resolved_by          text CHECK (resolved_by IS NULL OR length(resolved_by) BETWEEN 1 AND 256),
    resolution           text CHECK (resolution IS NULL OR resolution IN (
                           'unchanged_in_substance', 'dependant_corrected', 'dependant_withdrawn')),
    resolution_note      text CHECK (resolution_note IS NULL OR length(resolution_note) <= 2000),
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evidence_drift_flags_once UNIQUE (drift_id, dependant_kind, dependant_id),
    CONSTRAINT evidence_drift_flags_resolved_shape CHECK (
        (state = 'resolved') = (resolution IS NOT NULL AND resolved_at IS NOT NULL AND resolved_by IS NOT NULL)),
    CONSTRAINT evidence_drift_flags_reviewed_shape CHECK (
        state = 'open' OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS evidence_drift_flags_open_idx
    ON public.evidence_drift_flags (created_at DESC) WHERE state <> 'resolved';
CREATE INDEX IF NOT EXISTS evidence_drift_flags_dependant_idx
    ON public.evidence_drift_flags (dependant_kind, dependant_id);
CREATE INDEX IF NOT EXISTS evidence_drift_flags_address_idx
    ON public.evidence_drift_flags (address, created_at DESC);
CREATE INDEX IF NOT EXISTS evidence_drift_flags_classification_idx
    ON public.evidence_drift_flags (classification_id);

-- A flag is marked, never rewritten: the identity columns never change, the
-- state only moves forward (open -> reviewed -> resolved, or open ->
-- resolved), and nothing is deleted.
CREATE OR REPLACE FUNCTION public.evidence_drift_flags_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'evidence_drift_flags rows are never deleted (flag %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.classification_id, NEW.drift_id, NEW.address, NEW.earlier_version_id,
        NEW.drift_class, NEW.dependant_kind, NEW.dependant_id, NEW.depth, NEW.created_at)
       IS DISTINCT FROM
       (OLD.classification_id, OLD.drift_id, OLD.address, OLD.earlier_version_id,
        OLD.drift_class, OLD.dependant_kind, OLD.dependant_id, OLD.depth, OLD.created_at)
       OR NEW.later_version_id IS DISTINCT FROM OLD.later_version_id
       OR NEW.cited_span IS DISTINCT FROM OLD.cited_span
       OR NEW.span_check IS DISTINCT FROM OLD.span_check
       OR NEW.span_in_earlier IS DISTINCT FROM OLD.span_in_earlier
       OR NEW.span_in_later IS DISTINCT FROM OLD.span_in_later THEN
        RAISE EXCEPTION 'evidence_drift_flags identity columns never change (flag %)', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'resolved'
       OR (OLD.state = 'reviewed' AND NEW.state = 'open')
       OR (OLD.state = NEW.state AND NEW.state <> 'open') THEN
        RAISE EXCEPTION 'evidence_drift_flags state moves forward only (% -> %, flag %)',
            OLD.state, NEW.state, OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS evidence_drift_flags_guard ON public.evidence_drift_flags;
CREATE TRIGGER evidence_drift_flags_guard
    BEFORE UPDATE OR DELETE ON public.evidence_drift_flags
    FOR EACH ROW EXECUTE FUNCTION public.evidence_drift_flags_guard();

COMMENT ON TABLE public.evidence_drift_flags IS
    'D-1: one flag per dependant of a page version superseded by a '
    'substantive edit, title change, retraction/replacement or disappearance. '
    'Marks, never rewrites: open -> reviewed -> resolved.';

CREATE TABLE IF NOT EXISTS public.evidence_rederive_requests (
    id                   uuid PRIMARY KEY,
    flag_id              bigint REFERENCES public.evidence_drift_flags (id),
    dependant_kind       text NOT NULL,
    dependant_id         text NOT NULL CHECK (length(dependant_id) BETWEEN 1 AND 256),
    address              text NOT NULL,
    earlier_version_id   bigint NOT NULL REFERENCES public.fetch_versions (id),
    later_version_id     bigint REFERENCES public.fetch_versions (id),
    requested_by         text NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 256),
    note                 text CHECK (note IS NULL OR length(note) <= 2000),
    state                text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'taken', 'done', 'declined')),
    taken_by             text,
    outcome              jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    finished_at          timestamptz
);

-- One pending request per (dependant, later version): a second ask while one
-- is pending returns the first.
CREATE UNIQUE INDEX IF NOT EXISTS evidence_rederive_requests_pending_key
    ON public.evidence_rederive_requests (dependant_kind, dependant_id, coalesce(later_version_id, -1))
    WHERE state IN ('pending', 'taken');
CREATE INDEX IF NOT EXISTS evidence_rederive_requests_state_idx
    ON public.evidence_rederive_requests (state, created_at);

COMMENT ON TABLE public.evidence_rederive_requests IS
    'D-1: "re-derive this dependant with the changed text" — recorded for the '
    'statement layer to act on; nothing in D-1 re-runs an analyst.';

-- The drift re-check batch's back-off reads the done re-checks of one address.
CREATE INDEX IF NOT EXISTS ingestion_requests_drift_address_idx
    ON public.ingestion_requests (address_canonical, finished_at DESC)
    WHERE reason = 'drift_recheck';
