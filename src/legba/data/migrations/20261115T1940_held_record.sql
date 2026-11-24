-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 20261115T1940_held_record.sql
--
-- THE HELD FAMILY (data-model lane 10, DM-C): the tables in which the record
-- holds what it has established, over time. Database name `assertion`; on
-- screen the checked kind is a "statement" (the owner's decision 2026-11-15).
-- This is the persistence plan's statement layer (P1 §2.1, lane S1) with four
-- changes: the type is a reference to the type registry (family
-- `assertion_type`), a `basis` column (derived / declared / reserved
-- extracted), real key columns on the support link (no kind+id pair except
-- one counted legacy key), and an entity id as the subject.
--
--   assertions                 identity: class, type, basis, subject, speaker,
--                              claim key. Insert-only.
--   assertion_versions         what it says, for which period, from which run.
--                              A revision is a new version, never an edit.
--   assertion_status_log       every status change (held, not_held, contested,
--                              superseded, withdrawn), its reason, the verdict
--                              and the principal that caused it. Append-only
--                              and HASH-CHAINED per assertion: the database
--                              sets seq, prev_hash, recorded_at and row_hash
--                              (assertion_status_row_hash below; the Python
--                              mirror is legba.data.held_record_chain).
--   assertion_status           the current status: a CACHE, maintained by a
--                              trigger on the log and rebuildable from it
--                              (scripts/held_record_verify.py proves rebuild
--                              equals cache). The one updatable table.
--   assertion_support          "this rests on that": a claimant (a version, or
--                              a desk shadow row for a restatement) and exactly
--                              one typed target (a signal, an observation, a
--                              finding sentence, a finding, another assertion
--                              version, a verdict), with the quote and the
--                              receipt version pinned. Insert-only.
--   assertion_shadow_receipts  one row per desk shadow record written: the
--                              counts, and the idempotency key.
--
-- WHAT FEEDS IT: only the existing desk-row shadow records, behind
-- LEGBA_HELD_RECORD_SHADOW (OFF by default), from one desk. Nothing a reader
-- sees changes: the only readers are GET /v3/record/assertions[/{id}] and the
-- verify script. Promotion of a statement to a fact, contradiction detection,
-- expiry and redaction are not built (docs/SEAMS.md #held-record).
--
-- WHY THE CLASS IS A CHECK LIST AND THE TYPE IS NOT: the class is the closed,
-- in-code list of behaviour classes (a new class changes what the product may
-- assert, so it is a reviewed change and a migration); the type is an open
-- registry declaration, checked by the writer before SQL and held to the
-- family by a container test.
--
-- WHY NO FOREIGN KEY TO signals / observations / analyst_outputs /
-- entity_profiles: those rows can be removed by the house's own paths
-- (retention with a tombstone, entity compaction, test teardown); an FK would
-- block them or cascade-delete held links. The real FKs are to the family's
-- own tables, to `fetch_versions` (never deleted), to `finding_sentences`
-- (insert-only) and to `principals` (never deleted).
--
-- INSERT-ONLY: UPDATE and DELETE are refused on every table but the cache,
-- except DELETE of a REPLAY batch named in `legba.held_record_rollback`
-- (`SET LOCAL legba.held_record_rollback = '<batch>'`). Rows fed from the
-- shadow (`origin = 'shadow'`) carry no batch and can never be deleted.
--
-- SAFETY: CREATE ... IF NOT EXISTS / CREATE OR REPLACE only; no existing table
-- is touched; no BEGIN/COMMIT (the runner wraps the file); forward-only.
-- Independent of 20261115T1829 (lane DM-G's graph drop) and sorts after it.

CREATE OR REPLACE FUNCTION public.held_record_insert_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND OLD.batch IS NOT NULL
       AND OLD.batch = current_setting('legba.held_record_rollback', true) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is insert-only: a held record is revised by a new version or a new status row, never edited; a replay batch is removed only with legba.held_record_rollback set to its batch id (%)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END $$;

-- ---------------------------------------------------------------------------
-- assertions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assertions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    class              text NOT NULL CHECK (class IN ('statement', 'attributed', 'assessment', 'absence')),
    -- A declared assertion_type id (type registry); the writer refuses others.
    type_id            text NOT NULL CHECK (type_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    -- derived: worked out by a run, through the gates. declared: entered by a
    -- person or a loader (lane 11). extracted: RESERVED for the machine-read
    -- triples, which stay in `facts` (lane DM-E); the writer refuses it.
    basis              text NOT NULL CHECK (basis IN ('derived', 'declared', 'extracted')),
    -- The subject. entity: an entity_profiles id (also in subject_key);
    -- target: a target descriptor id; place: an ISO country code; surface: a
    -- name copied from the quote that resolved to no unique entity.
    subject_kind       text NOT NULL CHECK (subject_kind IN ('entity', 'target', 'place', 'surface')),
    subject_entity_id  uuid,
    subject_key        text NOT NULL CHECK (length(subject_key) BETWEEN 1 AND 300),
    subject_text       text,                       -- the subject as the row named it, kept
    -- attributed only: who said it (the name as the row gave it, and its
    -- entity when that resolved).
    speaker_text       text,
    speaker_entity_id  uuid,
    -- sha256 over (type, subject key, the type's declared key fields,
    -- normalised): the matching key (legba.data.held_record.claim_key).
    claim_key          text NOT NULL CHECK (claim_key ~ '^[0-9a-f]{64}$'),
    collection         text,                       -- the desk collection (target) it came from
    -- RESERVED for promotion of a statement to a fact (not built; SEAMS
    -- #held-record): the statement a declared fact was promoted from. The
    -- support trigger refuses that fact as support for that statement.
    promoted_from      uuid REFERENCES public.assertions (id),
    origin             text NOT NULL DEFAULT 'shadow' CHECK (origin IN ('shadow', 'replay', 'live')),
    batch              text,
    written_by         uuid NOT NULL REFERENCES public.principals (id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assertions_entity_subject CHECK ((subject_kind = 'entity') = (subject_entity_id IS NOT NULL)),
    CONSTRAINT assertions_speaker_only_attributed CHECK (
        class = 'attributed' OR (speaker_text IS NULL AND speaker_entity_id IS NULL)),
    CONSTRAINT assertions_promoted_is_declared CHECK (promoted_from IS NULL OR basis = 'declared'),
    CONSTRAINT assertions_batch_iff_replay CHECK ((origin = 'replay') = (batch IS NOT NULL))
);

-- One assertion per claim key (per replay batch): a match is a new version or
-- a restatement link, never a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS assertions_claim_key_uq
    ON public.assertions (claim_key, (coalesce(batch, '')));
CREATE INDEX IF NOT EXISTS assertions_subject_idx
    ON public.assertions (subject_key, class);
CREATE INDEX IF NOT EXISTS assertions_subject_entity_idx
    ON public.assertions (subject_entity_id) WHERE subject_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertions_created_idx
    ON public.assertions (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS assertions_batch_idx
    ON public.assertions (batch) WHERE batch IS NOT NULL;

-- ---------------------------------------------------------------------------
-- assertion_versions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assertion_versions (
    id                    bigserial PRIMARY KEY,
    assertion_id          uuid NOT NULL REFERENCES public.assertions (id),
    version_no            integer NOT NULL CHECK (version_no >= 1),
    prev_version_id       bigint REFERENCES public.assertion_versions (id),
    -- first / later_window (an absence re-scoped to a later window) / ...: a
    -- closed list in code (legba.data.held_record.VERSION_REASONS).
    reason_code           text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    text                  text NOT NULL,
    text_sha256           text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    -- type-specific slots: a figure and its unit, a counterpart, an
    -- assessment's confidence word, an absence's topic and scope.
    slots                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- valid time, from the evidence (an absence: the collection window).
    valid_from            timestamptz,
    valid_from_precision  text,
    valid_to              timestamptz,
    -- provenance of the proposal: the desk shadow record and row it came
    -- from, and the run.
    shadow_id             bigint,
    shadow_row            text,
    run_id                text,
    analyst_id            text,
    contract_version      text,
    gate_version          text,
    -- who proposed it: the desk seat (agent:<id>@<version>) when the run's
    -- version is known; NULL otherwise, never guessed.
    proposed_by           uuid REFERENCES public.principals (id),
    origin                text NOT NULL DEFAULT 'shadow' CHECK (origin IN ('shadow', 'replay', 'live')),
    batch                 text,
    written_by            uuid NOT NULL REFERENCES public.principals (id),
    recorded_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assertion_versions_no_uq UNIQUE (assertion_id, version_no),
    CONSTRAINT assertion_versions_prev CHECK ((version_no = 1) = (prev_version_id IS NULL)),
    CONSTRAINT assertion_versions_period CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    CONSTRAINT assertion_versions_batch_iff_replay CHECK ((origin = 'replay') = (batch IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS assertion_versions_shadow_idx
    ON public.assertion_versions (shadow_id) WHERE shadow_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertion_versions_batch_idx
    ON public.assertion_versions (batch) WHERE batch IS NOT NULL;

-- ---------------------------------------------------------------------------
-- assertion_status_log (append-only, hash-chained per assertion)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assertion_status_log (
    id            bigserial PRIMARY KEY,
    assertion_id  uuid NOT NULL REFERENCES public.assertions (id),
    seq           integer NOT NULL CHECK (seq >= 1),
    status        text NOT NULL CHECK (status IN ('held', 'not_held', 'contested', 'superseded', 'withdrawn')),
    version_id    bigint NOT NULL REFERENCES public.assertion_versions (id),
    -- why (a closed list in code, legba.data.held_record.STATUS_REASONS), and
    -- the verifier's verdict when one decided it.
    reason_code   text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    reason        text,
    verdict       text,
    caused_by     uuid NOT NULL REFERENCES public.principals (id),
    source_ref    text,                            -- shadow:<id>:<row>, the input that caused it
    batch         text,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    prev_hash     text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    row_hash      text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT assertion_status_log_seq_uq UNIQUE (assertion_id, seq)
);

CREATE INDEX IF NOT EXISTS assertion_status_log_batch_idx
    ON public.assertion_status_log (batch) WHERE batch IS NOT NULL;

-- One hashed field: NULL is '~'; anything else is its length, ':', itself.
CREATE OR REPLACE FUNCTION public.held_record_hash_field(t text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN t IS NULL THEN '~' ELSE length(t)::text || ':' || t END
$$;

CREATE OR REPLACE FUNCTION public.assertion_status_row_hash(
    p_assertion uuid, p_seq integer, p_prev text, p_status text, p_version bigint,
    p_reason_code text, p_reason text, p_verdict text, p_caused_by uuid, p_source_ref text,
    p_at timestamptz)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT encode(sha256(convert_to(concat_ws('|',
        'assertion_status_log.v1',
        public.held_record_hash_field(p_assertion::text),
        public.held_record_hash_field(p_seq::text),
        public.held_record_hash_field(p_prev),
        public.held_record_hash_field(p_status),
        public.held_record_hash_field(p_version::text),
        public.held_record_hash_field(p_reason_code),
        public.held_record_hash_field(p_reason),
        public.held_record_hash_field(p_verdict),
        public.held_record_hash_field(p_caused_by::text),
        public.held_record_hash_field(p_source_ref),
        public.held_record_hash_field(to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
    ), 'UTF8')), 'hex')
$$;

-- BEFORE INSERT: the database, not the writer, sets the chain fields and the
-- recorded time. A writer that supplies seq / prev_hash / row_hash is refused;
-- recorded_at is overwritten with the server's time.
CREATE OR REPLACE FUNCTION public.assertion_status_log_chain()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_seq        integer;
    v_hash       text;
    v_assertion  uuid;
BEGIN
    IF NEW.seq IS NOT NULL OR NEW.prev_hash IS NOT NULL OR NEW.row_hash IS NOT NULL THEN
        RAISE EXCEPTION 'assertion_status_log: seq, prev_hash and row_hash are set by the database, never supplied'
            USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('assertion_status_log:' || NEW.assertion_id::text, 0));
    SELECT assertion_id INTO v_assertion FROM public.assertion_versions WHERE id = NEW.version_id;
    IF v_assertion IS DISTINCT FROM NEW.assertion_id THEN
        RAISE EXCEPTION 'assertion_status_log: version % is not a version of assertion %', NEW.version_id, NEW.assertion_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    SELECT l.seq, l.row_hash INTO v_seq, v_hash
      FROM public.assertion_status_log l
     WHERE l.assertion_id = NEW.assertion_id
     ORDER BY l.seq DESC LIMIT 1;
    NEW.seq := coalesce(v_seq, 0) + 1;
    NEW.prev_hash := coalesce(v_hash, repeat('0', 64));
    NEW.recorded_at := now();
    NEW.row_hash := public.assertion_status_row_hash(
        NEW.assertion_id, NEW.seq, NEW.prev_hash, NEW.status, NEW.version_id, NEW.reason_code,
        NEW.reason, NEW.verdict, NEW.caused_by, NEW.source_ref, NEW.recorded_at);
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- assertion_status (the cache)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assertion_status (
    assertion_id   uuid PRIMARY KEY REFERENCES public.assertions (id),
    status         text NOT NULL,
    version_id     bigint NOT NULL REFERENCES public.assertion_versions (id),
    last_seq       integer NOT NULL,
    last_row_hash  text NOT NULL,
    status_at      timestamptz NOT NULL,
    entries        integer NOT NULL,
    batch          text
);

CREATE INDEX IF NOT EXISTS assertion_status_status_idx
    ON public.assertion_status (status);

CREATE OR REPLACE FUNCTION public.assertion_status_cache()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.assertion_status AS c
        (assertion_id, status, version_id, last_seq, last_row_hash, status_at, entries, batch)
    VALUES (NEW.assertion_id, NEW.status, NEW.version_id, NEW.seq, NEW.row_hash, NEW.recorded_at, 1, NEW.batch)
    ON CONFLICT (assertion_id) DO UPDATE
       SET status = EXCLUDED.status, version_id = EXCLUDED.version_id, last_seq = EXCLUDED.last_seq,
           last_row_hash = EXCLUDED.last_row_hash, status_at = EXCLUDED.status_at,
           entries = c.entries + 1;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS assertion_status_log_chain ON public.assertion_status_log;
CREATE TRIGGER assertion_status_log_chain
    BEFORE INSERT ON public.assertion_status_log
    FOR EACH ROW EXECUTE FUNCTION public.assertion_status_log_chain();
DROP TRIGGER IF EXISTS assertion_status_log_cache ON public.assertion_status_log;
CREATE TRIGGER assertion_status_log_cache
    AFTER INSERT ON public.assertion_status_log
    FOR EACH ROW EXECUTE FUNCTION public.assertion_status_cache();

-- ---------------------------------------------------------------------------
-- assertion_support
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assertion_support (
    id                     bigserial PRIMARY KEY,
    -- The claimant: exactly one. A version rests on its targets; a desk
    -- shadow row (shadow:<id>:<row>) that restates a held record is a link,
    -- not a record; a finding sentence (reserved: no writer in this lane).
    claimant_version_id    bigint REFERENCES public.assertion_versions (id),
    claimant_shadow_row    text,
    claimant_sentence_id   bigint REFERENCES public.finding_sentences (id),
    -- evidence: counts toward holding it; premise: openly depends on it (an
    -- assessment on a statement; an entered fact shown as theirs); restates:
    -- the claimant says again what the target holds (never new evidence);
    -- background; contradicts (reserved: detection not built); verdict: the
    -- checker's verdict on the claimant.
    role                   text NOT NULL CHECK (role IN (
                               'evidence', 'premise', 'restates', 'background', 'contradicts', 'verdict')),
    target_kind            text NOT NULL CHECK (target_kind IN (
                               'signal', 'observation', 'sentence', 'finding', 'assertion_version', 'verdict')),
    signal_id              uuid,
    observation_id         uuid,
    sentence_id            bigint REFERENCES public.finding_sentences (id),
    output_id              uuid,                   -- a finding (the desk's prior read)
    target_version_id      bigint REFERENCES public.assertion_versions (id),
    -- A verdict has no table of its own yet: the legacy key names where it is
    -- held (shadow:<id>:<row>); counted, never joined.
    verdict_key            text,
    verdict                text,
    -- Copied at write from the target assertion (set by the trigger): the
    -- basis decides what it may be used for.
    target_basis           text,
    -- The bytes the material was read from (byte_path.classify's pick).
    fetch_version_id       bigint REFERENCES public.fetch_versions (id),
    fetch_link_role        text,
    bytes_label            text CHECK (bytes_label IS NULL OR bytes_label IN ('received_bytes', 'archived_later', 'no_bytes_kept')),
    bytes_sha256           text CHECK (bytes_sha256 IS NULL OR bytes_sha256 ~ '^[0-9a-f]{64}$'),
    text_sha256            text CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-f]{64}$'),
    quote                  text,
    quote_found_in         text,                   -- page_text / feed_item / both (the G1 check's finding)
    -- who is speaking inside the material, and how (lane DM-D fills these
    -- from the claim source class; NULL = not known at write).
    stance                 text,
    speaker                text,
    detail                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    origin                 text NOT NULL DEFAULT 'shadow' CHECK (origin IN ('shadow', 'replay', 'live')),
    batch                  text,
    written_by             uuid NOT NULL REFERENCES public.principals (id),
    recorded_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assertion_support_one_claimant CHECK (
        num_nonnulls(claimant_version_id, claimant_shadow_row, claimant_sentence_id) = 1),
    CONSTRAINT assertion_support_one_target CHECK (
        num_nonnulls(signal_id, observation_id, sentence_id, output_id, target_version_id, verdict_key) = 1),
    CONSTRAINT assertion_support_kind_matches CHECK (
            (target_kind = 'signal') = (signal_id IS NOT NULL)
        AND (target_kind = 'observation') = (observation_id IS NOT NULL)
        AND (target_kind = 'sentence') = (sentence_id IS NOT NULL)
        AND (target_kind = 'finding') = (output_id IS NOT NULL)
        AND (target_kind = 'assertion_version') = (target_version_id IS NOT NULL)
        AND (target_kind = 'verdict') = (verdict_key IS NOT NULL)),
    CONSTRAINT assertion_support_verdict_role CHECK ((role = 'verdict') = (target_kind = 'verdict')),
    CONSTRAINT assertion_support_verdict_value CHECK ((target_kind = 'verdict') = (verdict IS NOT NULL)),
    -- A restatement points at what it restates: a held record or the prior read.
    CONSTRAINT assertion_support_restates_target CHECK (
        role <> 'restates' OR target_kind IN ('assertion_version', 'finding')),
    -- A finding (a desk's earlier read) is never evidence.
    CONSTRAINT assertion_support_finding_not_evidence CHECK (
        target_kind <> 'finding' OR role IN ('restates', 'background')),
    -- G8: the record's own derived statements are never NEW evidence for
    -- another record; only a declared entry may be (lane DM-D adds the rule
    -- for when: a person's own rating alone never makes it a confirming source).
    CONSTRAINT assertion_support_derived_not_evidence CHECK (
        target_kind <> 'assertion_version' OR role <> 'evidence' OR target_basis = 'declared'),
    CONSTRAINT assertion_support_batch_iff_replay CHECK ((origin = 'replay') = (batch IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS assertion_support_claimant_idx
    ON public.assertion_support (claimant_version_id) WHERE claimant_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertion_support_shadow_row_idx
    ON public.assertion_support (claimant_shadow_row) WHERE claimant_shadow_row IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertion_support_target_version_idx
    ON public.assertion_support (target_version_id) WHERE target_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertion_support_signal_idx
    ON public.assertion_support (signal_id) WHERE signal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertion_support_fetch_version_idx
    ON public.assertion_support (fetch_version_id) WHERE fetch_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assertion_support_batch_idx
    ON public.assertion_support (batch) WHERE batch IS NOT NULL;

-- BEFORE INSERT: copy the target assertion's basis, refuse a record resting
-- on itself, and refuse a fact promoted FROM a statement as support for that
-- statement (the owner's rule; promotion itself is not built).
CREATE OR REPLACE FUNCTION public.assertion_support_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_target_assertion   uuid;
    v_target_basis       text;
    v_promoted_from      uuid;
    v_claimant_assertion uuid;
BEGIN
    NEW.recorded_at := now();
    IF NEW.target_version_id IS NULL THEN
        NEW.target_basis := NULL;
        RETURN NEW;
    END IF;
    SELECT a.id, a.basis, a.promoted_from INTO v_target_assertion, v_target_basis, v_promoted_from
      FROM public.assertion_versions v JOIN public.assertions a ON a.id = v.assertion_id
     WHERE v.id = NEW.target_version_id;
    NEW.target_basis := v_target_basis;
    IF NEW.claimant_version_id IS NOT NULL THEN
        SELECT assertion_id INTO v_claimant_assertion
          FROM public.assertion_versions WHERE id = NEW.claimant_version_id;
        IF v_claimant_assertion = v_target_assertion THEN
            RAISE EXCEPTION 'assertion_support: an assertion cannot rest on itself (%)', v_target_assertion
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_promoted_from IS NOT NULL AND v_promoted_from = v_claimant_assertion
           AND NEW.role IN ('evidence', 'premise') THEN
            RAISE EXCEPTION 'assertion_support: a fact promoted from statement % can never support it', v_claimant_assertion
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS assertion_support_guard ON public.assertion_support;
CREATE TRIGGER assertion_support_guard
    BEFORE INSERT ON public.assertion_support
    FOR EACH ROW EXECUTE FUNCTION public.assertion_support_guard();

-- ---------------------------------------------------------------------------
-- assertion_shadow_receipts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.assertion_shadow_receipts (
    id             bigserial PRIMARY KEY,
    shadow_id      bigint NOT NULL,                -- the desk shadow record (no FK: the ledger is measurement)
    run_id         text,
    analyst_id     text,
    target_id      text,
    occurred_at    timestamptz,                    -- the shadow record's, copied
    rule_version   text NOT NULL,                  -- the writer's rules (legba.data.held_record.RULE_VERSION)
    counts         jsonb NOT NULL DEFAULT '{}'::jsonb,
    origin         text NOT NULL DEFAULT 'shadow' CHECK (origin IN ('shadow', 'replay', 'live')),
    batch          text,
    written_by     uuid NOT NULL REFERENCES public.principals (id),
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assertion_shadow_receipts_batch_iff_replay CHECK ((origin = 'replay') = (batch IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS assertion_shadow_receipts_uq
    ON public.assertion_shadow_receipts (shadow_id, (coalesce(batch, '')));

-- ---------------------------------------------------------------------------
-- Insert-only triggers (the cache is the one updatable table)
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS assertions_insert_only ON public.assertions;
CREATE TRIGGER assertions_insert_only
    BEFORE UPDATE OR DELETE ON public.assertions
    FOR EACH ROW EXECUTE FUNCTION public.held_record_insert_only();
DROP TRIGGER IF EXISTS assertion_versions_insert_only ON public.assertion_versions;
CREATE TRIGGER assertion_versions_insert_only
    BEFORE UPDATE OR DELETE ON public.assertion_versions
    FOR EACH ROW EXECUTE FUNCTION public.held_record_insert_only();
DROP TRIGGER IF EXISTS assertion_status_log_insert_only ON public.assertion_status_log;
CREATE TRIGGER assertion_status_log_insert_only
    BEFORE UPDATE OR DELETE ON public.assertion_status_log
    FOR EACH ROW EXECUTE FUNCTION public.held_record_insert_only();
DROP TRIGGER IF EXISTS assertion_support_insert_only ON public.assertion_support;
CREATE TRIGGER assertion_support_insert_only
    BEFORE UPDATE OR DELETE ON public.assertion_support
    FOR EACH ROW EXECUTE FUNCTION public.held_record_insert_only();
DROP TRIGGER IF EXISTS assertion_shadow_receipts_insert_only ON public.assertion_shadow_receipts;
CREATE TRIGGER assertion_shadow_receipts_insert_only
    BEFORE UPDATE OR DELETE ON public.assertion_shadow_receipts
    FOR EACH ROW EXECUTE FUNCTION public.held_record_insert_only();

COMMENT ON TABLE public.assertions IS
    'DM-C: the held family''s identity rows (on screen: statements). Insert-only; type_id is an assertion_type declaration.';
COMMENT ON TABLE public.assertion_versions IS
    'DM-C: what an assertion says, for which period, from which run. A revision is a new version.';
COMMENT ON TABLE public.assertion_status_log IS
    'DM-C: every status change, append-only, hash-chained per assertion by the database (assertion_status_row_hash).';
COMMENT ON TABLE public.assertion_status IS
    'DM-C: current status per assertion; a cache maintained from the log by trigger, rebuildable (scripts/held_record_verify.py).';
COMMENT ON TABLE public.assertion_support IS
    'DM-C: what an assertion version rests on (one typed target per row, bytes pinned), and restatement links.';
COMMENT ON TABLE public.assertion_shadow_receipts IS
    'DM-C: one row per desk shadow record written into the held family (counts; the idempotency key).';
