-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 20261115T1827_principals_origins_written_by.sql
--
-- WHO WRITES (data-model lane 9): principals, origins and origin groups, and
-- a `written_by` principal reference on the tables that hold judgements,
-- labels, decisions, descriptor changes and entered knowledge.
--
-- WHY
-- ---
-- Three different "who" are recorded, never merged into one "source"
-- (docs/TYPES.md "Principal kinds"; merged recommendation §1.3 #10):
--   * who WROTE the row -- a PRINCIPAL: a person (linked to `users`), a
--     service, an analyst seat (descriptor id + version), a deterministic
--     rule, or a credential several people hold (the shared admin token);
--   * where the CONTENT came from -- an ORIGIN (a source descriptor, a
--     knowledge set, or a host on the licence ledger), each in an ORIGIN
--     GROUP: two feeds of one publisher, or two channels of one claimant,
--     are one group -- the unit independence will count;
--   * who is SPEAKING inside it -- the claimant (unchanged here).
-- Until now a row said "operator" or nothing. Sign-in (0255) made people
-- real; this gives every kind of writer one row and one id.
--
-- WHAT THIS CREATES
-- -----------------
--   * principals -- one row per writer; `handle` = '<kind>:<name>', unique
--     and immutable. The KIND is a `principal_kind` declaration in the type
--     registry (src/legba/data/type_registry/principal_kinds/); the Python
--     side refuses an undeclared kind before any SQL runs, and the database
--     holds the handle's SHAPE only (no enumerated kind list here: a new
--     kind is a declaration, not a migration). Rows are created on first use
--     by `legba_principal(handle)`, never seeded.
--   * legba_principal(handle) -- upsert-and-return-id, called INSIDE each
--     write statement (`written_by = legba_principal($n)`), so a write site
--     makes no extra round trip. STRICT: a NULL handle writes NULL.
--   * origin_groups / origins / origin_assignment_log -- every source
--     descriptor, knowledge set and ledger host resolves to an origin, each
--     origin to one group, with the derivation RULE recorded per row; an
--     admin may move an origin to another group, and every assignment (the
--     derived one and each correction) is an append-only log row naming its
--     principal and reason (the log refuses UPDATE and DELETE).
--   * `written_by uuid REFERENCES principals(id)`, NULLABLE, on 24 tables
--     (class (b) of src/legba/data/write_attribution.py, which also declares
--     the other two classes for every table: (a) already carries a
--     server-stamped writer, (c) a machine table whose writer is implied by
--     the table and recorded once there). Rows written before this migration
--     stay NULL: their writer was never recorded, and is not guessed.
--
-- SAFETY
-- ------
-- Additive and idempotent: CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT
-- EXISTS, CREATE OR REPLACE FUNCTION; each ADD COLUMN is nullable with no
-- default (a catalogue-only change); the foreign keys validate against
-- all-NULL columns on small tables (the largest, source_descriptors, holds
-- about 600 rows live). Routed through the runner (no inline BEGIN/COMMIT).

CREATE TABLE IF NOT EXISTS public.principals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handle              text NOT NULL,
    kind                text NOT NULL,
    user_id             uuid REFERENCES public.users (id) ON DELETE SET NULL,
    descriptor_id       text,
    descriptor_version  text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT principals_handle_key UNIQUE (handle),
    CONSTRAINT principals_handle_shape
        CHECK (handle ~ '^[a-z][a-z0-9_]{0,63}:[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,254}$'),
    CONSTRAINT principals_kind_is_handle_prefix
        CHECK (kind = split_part(handle, ':', 1))
);

COMMENT ON TABLE public.principals IS
    'Who can write a row (lane 9). handle = <kind>:<name>, immutable; kind is '
    'a principal_kind declaration in the type registry. Created on first use '
    'by legba_principal(handle); never seeded or edited by hand.';

CREATE INDEX IF NOT EXISTS principals_user_id
    ON public.principals (user_id) WHERE user_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.legba_principal(p_handle text)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE STRICT
AS $fn$
DECLARE
    v_id      uuid;
    v_kind    text := split_part(p_handle, ':', 1);
    v_name    text := substr(p_handle, length(split_part(p_handle, ':', 1)) + 2);
    v_user    uuid;
    v_desc    text;
    v_ver     text;
BEGIN
    SELECT id INTO v_id FROM public.principals WHERE handle = p_handle;
    IF v_id IS NOT NULL THEN
        RETURN v_id;
    END IF;
    IF v_kind = 'user' AND v_name ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        SELECT u.id INTO v_user FROM public.users u WHERE u.id = v_name::uuid;
    ELSIF v_kind = 'agent' AND position('@' IN v_name) > 0 THEN
        v_desc := regexp_replace(v_name, '@[^@]*$', '');
        v_ver  := regexp_replace(v_name, '^.*@', '');
    END IF;
    INSERT INTO public.principals (handle, kind, user_id, descriptor_id, descriptor_version)
    VALUES (p_handle, v_kind, v_user, v_desc, v_ver)
    ON CONFLICT (handle) DO NOTHING
    RETURNING id INTO v_id;
    IF v_id IS NULL THEN
        -- A concurrent first use won the insert; read the row it committed.
        SELECT id INTO v_id FROM public.principals WHERE handle = p_handle;
    END IF;
    RETURN v_id;
END
$fn$;

COMMENT ON FUNCTION public.legba_principal(text) IS
    'The principal id for a handle, created on first use (lane 9). Called '
    'inside write statements: written_by = legba_principal($n).';

-- ---------------------------------------------------------------------------
-- Origins and origin groups
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.origin_groups (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key         text NOT NULL,
    label       text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    written_by  uuid REFERENCES public.principals (id),
    CONSTRAINT origin_groups_key_key UNIQUE (key),
    CONSTRAINT origin_groups_key_shape CHECK (key ~ '^[a-z][a-z_]*:.+$')
);

COMMENT ON TABLE public.origin_groups IS
    'One independent origin for counting (lane 9): host:<registrable domain>, '
    'claimant:<name>, telegram:<channel>, set:<knowledge set>, descriptor:<id>, '
    'or an admin-named group. Two feeds of one publisher share a group.';

CREATE TABLE IF NOT EXISTS public.origins (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key              text NOT NULL,
    origin_kind      text NOT NULL,
    ref              text NOT NULL,
    label            text NOT NULL DEFAULT '',
    origin_group_id  uuid NOT NULL REFERENCES public.origin_groups (id),
    rule             text NOT NULL,
    rule_detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    corrected        boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    written_by       uuid REFERENCES public.principals (id),
    CONSTRAINT origins_key_key UNIQUE (key),
    CONSTRAINT origins_key_is_kind_and_ref CHECK (key = origin_kind || ':' || ref)
);

COMMENT ON TABLE public.origins IS
    'Where content comes from (lane 9): source:<descriptor id>, '
    'knowledge_set:<set>, ledger_host:<host>. rule = how its group was derived '
    '(legba.data.origins) or admin_correction; corrected rows are never '
    're-derived.';

CREATE INDEX IF NOT EXISTS origins_group ON public.origins (origin_group_id);

CREATE TABLE IF NOT EXISTS public.origin_assignment_log (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    origin_id      uuid NOT NULL REFERENCES public.origins (id),
    from_group_id  uuid REFERENCES public.origin_groups (id),
    to_group_id    uuid NOT NULL REFERENCES public.origin_groups (id),
    rule           text NOT NULL,
    reason         text NOT NULL,
    written_by     uuid NOT NULL REFERENCES public.principals (id),
    at             timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.origin_assignment_log IS
    'Every origin-to-group assignment, derived or corrected (lane 9). '
    'Append-only: UPDATE and DELETE are refused.';

CREATE INDEX IF NOT EXISTS origin_assignment_log_origin
    ON public.origin_assignment_log (origin_id, at);

CREATE OR REPLACE FUNCTION public.origin_assignment_log_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION 'origin_assignment_log is append-only (% refused)', TG_OP;
END
$fn$;

DROP TRIGGER IF EXISTS origin_assignment_log_append_only ON public.origin_assignment_log;
CREATE TRIGGER origin_assignment_log_append_only
    BEFORE UPDATE OR DELETE ON public.origin_assignment_log
    FOR EACH ROW EXECUTE FUNCTION public.origin_assignment_log_append_only();

-- ---------------------------------------------------------------------------
-- written_by on class (b) (src/legba/data/write_attribution.py, CLASS_B)
-- ---------------------------------------------------------------------------

ALTER TABLE public.watchlist               ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.collection_requirements ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.correctness_labels      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.unit_reference_labels   ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.source_credibility      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.journal_proposals       ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.entity_judgement        ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.consult_writes          ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.vocabulary_entries      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.retention_policies      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.stack_components        ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.stack_credentials       ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.analyst_descriptors     ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.source_descriptors      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.target_descriptors      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.action_pack_descriptors ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.collection_descriptors  ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.wiring_descriptors      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.read_events             ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.scheduled_events        ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.forecast_questions      ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.forecast_numbers        ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.forecast_resolutions    ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
ALTER TABLE public.inquiry_ledger          ADD COLUMN IF NOT EXISTS written_by uuid REFERENCES public.principals (id);
