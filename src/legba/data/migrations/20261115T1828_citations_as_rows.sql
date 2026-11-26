-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 20261115T1828_citations_as_rows.sql
--
-- CITATIONS AS ROWS. A finding's citations live
-- inside its document (`analyst_outputs.data->'data'->'citations'`) and its
-- prose markers ([N]); "what does this sentence rest on, and which stored
-- bytes was it read from" is a walk through jsonb. These five insert-only
-- tables make it a join. They are written BESIDE today's fields, in the same
-- transaction as a NEW finding (`legba.data.citation_rows_store`), and read by
-- nothing but the Record sheet's support route and the report script.
--
--   finding_sentences          one row per sentence of a finding, in order.
--   citation_support           one row per "this sentence rests on that
--                              record": a typed key column per target kind
--                              (no kind+id blob), the receipt version the
--                              material was read from, and the copies made at
--                              write so a later relabel never rewrites history.
--   citation_unresolved        a marker or citation entry that names nothing,
--                              with the reason (logged, not refused: the
--                              refusal is LEGBA_CITATION_REFUSE_UNRESOLVED,
--                              off, SEAMS #citation-refusal).
--   run_handed                 what the run was HANDED (its [N] index), typed
--                              by kind, so "cited something it was never
--                              shown" is a query, not a reading of a trace
--                              that is pruned after 45 days.
--   finding_citation_receipts  one row per finding: the counts of the write
--                              (sentences, supports, unresolved, handed). The
--                              row's presence is also what makes the dual
--                              write and the backfill idempotent.
--
-- WHY NO FOREIGN KEY TO analyst_outputs / signals / observations / …: those
-- rows can be removed by the house's own paths (signal retention with a
-- tombstone, test teardown, a backfill batch rollback), and an FK would either
-- block those paths or cascade-delete evidence links. The real FKs are to
-- `fetch_versions` (never deleted; only its bytes are pruned, with a
-- tombstone) and, on `written_by`, to `principals` (never deleted). Dangling
-- targets are counted by `scripts/citation_rows_report.py`.
--
-- WHY `target_kind` IS NOT A CHECK LIST: the kinds are declared in the type
-- registry (family `citation_target`, one module per kind, each naming its
-- key column); the writer refuses an undeclared kind and a test holds every
-- declared column to this table. A new kind that reuses a column is a new
-- declaration and no migration.
--
-- INSERT-ONLY: a trigger refuses every UPDATE, and every DELETE except of a
-- BACKFILL batch named in the session setting `legba.citation_rows_rollback`
-- (`SET LOCAL legba.citation_rows_rollback = '<batch>'`), which is how a
-- backfill batch is removed. Live rows (`origin = 'live'`) can never be
-- deleted by that route.
--
-- `written_by` (nullable, every table) is the principal that wrote the row
-- (migration `20261115T1827_principals_origins_written_by`, which
-- sorts first): the finding's seat (`agent:<id>@<version>`) or deterministic
-- handler (`rule:<id>`) on a live write, `rule:citation_rows_backfill` on a
-- backfill row, filled with `legba_principal($handle)` inside the INSERT.
-- NULL only when the finding names no version (a hand-built context): never
-- a guessed writer. Class (b) in `src/legba/data/write_attribution.py`.
--
-- SAFETY: CREATE ... IF NOT EXISTS only; no existing table is touched; no
-- BEGIN/COMMIT (the runner wraps the file); forward-only.

CREATE OR REPLACE FUNCTION public.citation_rows_insert_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND OLD.backfill_batch IS NOT NULL
       AND OLD.backfill_batch = current_setting('legba.citation_rows_rollback', true) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is insert-only: a citation row is never edited; a backfill batch is removed only with legba.citation_rows_rollback set to its batch id (%)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END $$;

-- ---------------------------------------------------------------------------
-- finding_sentences
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.finding_sentences (
    id              bigserial PRIMARY KEY,
    finding_id      uuid NOT NULL,                 -- analyst_outputs.id (kind finding)
    ordinal         integer NOT NULL CHECK (ordinal >= 0),   -- position in the body, 0-based
    char_start      integer NOT NULL CHECK (char_start >= 0),
    char_end        integer NOT NULL CHECK (char_end >= char_start),
    text            text NOT NULL,                 -- as written, markers normalised to ASCII [N]
    text_sha256     text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    -- What the sentence is, where known at write: 'heading' today; a later
    -- typing step adds statement / assessment / absence. NULL = not known.
    kind            text CHECK (kind IS NULL OR kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    marker_count    integer NOT NULL DEFAULT 0 CHECK (marker_count >= 0),  -- distinct markers in it
    run_id          uuid,
    analyst_id      text,
    origin          text NOT NULL DEFAULT 'live' CHECK (origin IN ('live', 'backfill')),
    backfill_batch  text,
    written_by      uuid REFERENCES public.principals (id),
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT finding_sentences_finding_ordinal UNIQUE (finding_id, ordinal),
    CONSTRAINT finding_sentences_batch_iff_backfill CHECK ((origin = 'backfill') = (backfill_batch IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- citation_support
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.citation_support (
    id                bigserial PRIMARY KEY,
    finding_id        uuid NOT NULL,
    -- The sentence the marker sits in. NULL for a citation no sentence
    -- carries (the unmarked-basis fallback: the run read these and cited none).
    sentence_id       bigint REFERENCES public.finding_sentences (id),
    sentence_ordinal  integer,
    -- The marker as the prose carries it, canonical: '[3]' (a desk) or
    -- '[[ref:3]]' (a composition). NULL when the entry is bound to no marker.
    marker            text,
    -- cites = the sentence rests on it; restates = it is the desk's own earlier
    -- read (a prior read or a window-ledger member: never new evidence);
    -- basis = the run's fallback "I read these" list, bound to no sentence.
    role              text NOT NULL CHECK (role IN ('cites', 'restates', 'basis')),
    -- A declared citation_target id (type registry); the writer refuses others.
    target_kind       text NOT NULL CHECK (target_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    -- The grounding block the target was a member of (window_ledger,
    -- situation_register, open_events, open_questions, desk_baseline), or
    -- 'event' for a signal an event token expanded to. NULL = cited directly.
    via_block         text,
    -- Exactly one typed key per row.
    signal_id         uuid,
    observation_id    uuid,
    output_id         uuid,                        -- an analyst_outputs row (finding, prior read)
    situation_id      uuid,
    event_id          uuid,
    hypothesis_id     uuid,                        -- an open question (hypotheses row)
    inquiry_id        uuid,                        -- an inquiry_ledger row
    legacy_key        text,                        -- a kind with no uuid (desk_baseline 'desk:metric'); counted, never joined
    -- The bytes the material was read from: the receipt version the signal or
    -- observation links to (byte_path.classify's pick), pinned at write.
    fetch_version_id  bigint REFERENCES public.fetch_versions (id),
    fetch_link_role   text,                        -- response / page / inbound / archived
    bytes_label       text CHECK (bytes_label IS NULL OR bytes_label IN ('received_bytes', 'archived_later', 'no_bytes_kept')),
    bytes_sha256      text CHECK (bytes_sha256 IS NULL OR bytes_sha256 ~ '^[0-9a-f]{64}$'),
    text_sha256       text CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-f]{64}$'),
    quote             text,                        -- the exact quote, where the citation carried one
    -- What the model was shown for this citation, hashed (evidence_text for a
    -- block, source_text / snippet for a signal): a later check can prove the
    -- text it grades is the text that was handed.
    shown_field       text,
    shown_sha256      text CHECK (shown_sha256 IS NULL OR shown_sha256 ~ '^[0-9a-f]{64}$'),
    -- Copied at write from the citation (the claim source class, C-1): stance
    -- 'claim' with the claimant as speaker, 'report' for any other signal.
    stance            text,
    speaker           text,
    source_id         text,
    run_id            uuid,
    analyst_id        text,
    origin            text NOT NULL DEFAULT 'live' CHECK (origin IN ('live', 'backfill')),
    backfill_batch    text,
    written_by        uuid REFERENCES public.principals (id),
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT citation_support_one_target CHECK (num_nonnulls(
        signal_id, observation_id, output_id, situation_id, event_id,
        hypothesis_id, inquiry_id, legacy_key) = 1),
    CONSTRAINT citation_support_batch_iff_backfill CHECK ((origin = 'backfill') = (backfill_batch IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- citation_unresolved
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.citation_unresolved (
    id                bigserial PRIMARY KEY,
    finding_id        uuid NOT NULL,
    sentence_id       bigint REFERENCES public.finding_sentences (id),
    sentence_ordinal  integer,
    marker            text,
    -- marker_not_in_citations: a prose [N] with no persisted citation entry
    --   (out of range, or the handed row had no id);
    -- entry_without_target: an entry that names no record at all;
    -- unknown_ref_kind: an entry whose kind is not a declared citation target;
    -- malformed_id: an id that is not a uuid;
    -- target_missing: a well-formed id with no row in its table at write.
    reason            text NOT NULL CHECK (reason IN (
                          'marker_not_in_citations', 'entry_without_target',
                          'unknown_ref_kind', 'malformed_id', 'target_missing')),
    ref_kind          text,
    ref_value         text,
    run_id            uuid,
    analyst_id        text,
    origin            text NOT NULL DEFAULT 'live' CHECK (origin IN ('live', 'backfill')),
    backfill_batch    text,
    written_by        uuid REFERENCES public.principals (id),
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT citation_unresolved_batch_iff_backfill CHECK ((origin = 'backfill') = (backfill_batch IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- run_handed
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.run_handed (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL,
    finding_id      uuid,                          -- the finding the run wrote
    target_kind     text NOT NULL CHECK (target_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    via_block       text,                          -- as on citation_support
    -- One of: the uuids handed (every uuid-keyed kind) or the text keys
    -- (a kind with no uuid). `ordinals` is parallel: the [N] each was shown as.
    ids             uuid[],
    keys            text[],
    ordinals        integer[] NOT NULL,
    origin          text NOT NULL DEFAULT 'live' CHECK (origin IN ('live', 'backfill')),
    backfill_batch  text,
    written_by      uuid REFERENCES public.principals (id),
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_handed_one_list CHECK (num_nonnulls(ids, keys) = 1),
    CONSTRAINT run_handed_parallel CHECK (
        cardinality(ordinals) = cardinality(coalesce(ids::text[], keys))),
    CONSTRAINT run_handed_batch_iff_backfill CHECK ((origin = 'backfill') = (backfill_batch IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- finding_citation_receipts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.finding_citation_receipts (
    finding_id       uuid PRIMARY KEY,
    run_id           uuid,
    analyst_id       text,
    target_id        text,
    produced_at      timestamptz NOT NULL,         -- the finding's, copied: a day's report reads only this table
    sentences        integer NOT NULL,
    cited_sentences  integer NOT NULL,
    markers          integer NOT NULL,             -- distinct (sentence, marker) pairs of the finding's own convention
    -- Markers of the OTHER convention (a unit [N] carried into a composition,
    -- whose own markers are [[ref:N]]): not this finding's to resolve.
    foreign_markers  integer NOT NULL DEFAULT 0,
    supports         integer NOT NULL,
    unresolved       integer NOT NULL,
    handed_recorded  boolean NOT NULL,             -- false = the writer was not handed the run's index
    handed           integer NOT NULL DEFAULT 0,
    origin           text NOT NULL DEFAULT 'live' CHECK (origin IN ('live', 'backfill')),
    backfill_batch   text,
    written_by       uuid REFERENCES public.principals (id),
    recorded_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT finding_citation_receipts_batch_iff_backfill CHECK ((origin = 'backfill') = (backfill_batch IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- Indexes: the reads (sentence → bytes; what rests on this
-- item / these bytes) and the report's day window. Partial on the sparse
-- typed columns; the rarely-cited kinds (situation, event, question, inquiry)
-- carry none until a reader needs one.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS citation_support_finding_idx
    ON public.citation_support (finding_id);
CREATE INDEX IF NOT EXISTS citation_support_sentence_idx
    ON public.citation_support (sentence_id) WHERE sentence_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS citation_support_signal_idx
    ON public.citation_support (signal_id) WHERE signal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS citation_support_output_idx
    ON public.citation_support (output_id) WHERE output_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS citation_support_observation_idx
    ON public.citation_support (observation_id) WHERE observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS citation_support_fetch_version_idx
    ON public.citation_support (fetch_version_id) WHERE fetch_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS citation_support_batch_idx
    ON public.citation_support (backfill_batch) WHERE backfill_batch IS NOT NULL;
CREATE INDEX IF NOT EXISTS finding_sentences_batch_idx
    ON public.finding_sentences (backfill_batch) WHERE backfill_batch IS NOT NULL;
CREATE INDEX IF NOT EXISTS citation_unresolved_finding_idx
    ON public.citation_unresolved (finding_id);
CREATE INDEX IF NOT EXISTS run_handed_run_idx
    ON public.run_handed (run_id);
CREATE INDEX IF NOT EXISTS finding_citation_receipts_produced_idx
    ON public.finding_citation_receipts (produced_at);
CREATE INDEX IF NOT EXISTS finding_citation_receipts_batch_idx
    ON public.finding_citation_receipts (backfill_batch) WHERE backfill_batch IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Insert-only triggers
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS finding_sentences_insert_only ON public.finding_sentences;
CREATE TRIGGER finding_sentences_insert_only
    BEFORE UPDATE OR DELETE ON public.finding_sentences
    FOR EACH ROW EXECUTE FUNCTION public.citation_rows_insert_only();
DROP TRIGGER IF EXISTS citation_support_insert_only ON public.citation_support;
CREATE TRIGGER citation_support_insert_only
    BEFORE UPDATE OR DELETE ON public.citation_support
    FOR EACH ROW EXECUTE FUNCTION public.citation_rows_insert_only();
DROP TRIGGER IF EXISTS citation_unresolved_insert_only ON public.citation_unresolved;
CREATE TRIGGER citation_unresolved_insert_only
    BEFORE UPDATE OR DELETE ON public.citation_unresolved
    FOR EACH ROW EXECUTE FUNCTION public.citation_rows_insert_only();
DROP TRIGGER IF EXISTS run_handed_insert_only ON public.run_handed;
CREATE TRIGGER run_handed_insert_only
    BEFORE UPDATE OR DELETE ON public.run_handed
    FOR EACH ROW EXECUTE FUNCTION public.citation_rows_insert_only();
DROP TRIGGER IF EXISTS finding_citation_receipts_insert_only ON public.finding_citation_receipts;
CREATE TRIGGER finding_citation_receipts_insert_only
    BEFORE UPDATE OR DELETE ON public.finding_citation_receipts
    FOR EACH ROW EXECUTE FUNCTION public.citation_rows_insert_only();

COMMENT ON TABLE public.finding_sentences IS
    'DM-A: one row per sentence of a finding (new findings, dual-written in the finding''s transaction; backfill rows carry a batch).';
COMMENT ON TABLE public.citation_support IS
    'DM-A: one row per citation: sentence -> the record it rests on (one typed key column) -> the receipt version it was read from.';
COMMENT ON TABLE public.citation_unresolved IS
    'DM-A: a marker or citation entry that names nothing, with the reason. Logged, not refused (LEGBA_CITATION_REFUSE_UNRESOLVED, off).';
COMMENT ON TABLE public.run_handed IS
    'DM-A: what a run was handed (its [N] index), typed by kind; "cited but never shown" is a join on run_id.';
COMMENT ON TABLE public.finding_citation_receipts IS
    'DM-A: per finding, the counts of its citation-row write; presence = written (the dual write and backfill are idempotent on it).';
