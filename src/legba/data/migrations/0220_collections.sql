-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0220_collections.sql
--
-- Program 7g-1 — the COLLECTIONS core: the `collection` descriptor family's
-- table, the `observations` series store, `entity_aliases`, the
-- `collection_loads` ledger, and the end of the origin-class reader sweep
-- (planning/PROGRAM_7G_COLLECTIONS_DESIGN_2026-09-25.md §2, §3, §4, §5;
-- SEAMS #57 → resolved, SEAMS #62 → opened).
--
-- A COLLECTION IS NOT A SOURCE. A source is a live feed: polled on a
-- cadence, its silence a health signal, its arrival able to wake a reactive
-- trigger, everything it produces stamped `origin_class='live'`. A
-- collection is a bounded, versioned, curated HOLDING of the past: fetched
-- once by the operator, written DIRECTLY (no ingest pipeline, no NATS
-- publish), and fenced from every surface that reads "now". Nothing about a
-- collection is scheduled; it has no cadence, no cooldown and no health.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 1. `collection_descriptors` — the fifth descriptor family's table
-- ─────────────────────────────────────────────────────────────────────────
-- Shaped like `action_pack_descriptors` (0001) plus four columns a reader
-- needs without opening the body: `origin_shape`, `origin_class`,
-- `licence_class` and `collection_version`. The last is the hash of the
-- MANIFEST, not of the descriptor — re-approving a licence line must not
-- invalidate a load that already happened — and it is what
-- `collection_loads` joins on.
--
-- The `state` column carries the collection's OWN lifecycle
-- (draft → reviewed → loaded → superseded), not the shared
-- draft/configured/active/paused/retired one: a holding is never "paused"
-- and never "active". The CHECK is the closed vocabulary; the transition
-- rules live in `data/schemas/collection.py:COLLECTION_TRANSITIONS` and are
-- enforced by `registry/descriptor_families.py:state_machine_for`.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 2. `observations` — series rows, bitemporal, natively partitioned
-- ─────────────────────────────────────────────────────────────────────────
-- Documents go where documents already go (`signals`, with a history
-- `origin_class` and a `collection_id`). NUMBERS are a different shape and
-- get their own table.
--
-- BITEMPORAL FROM DAY ONE. `valid_from`/`valid_to` are the period a number
-- is ABOUT; `record_time` is when the provider published or revised it. A
-- 2016 GDP figure revised in 2023 is TWO rows, and an as-of reader picks by
-- `record_time <= D`. That is not a refinement to add later: a provider that
-- silently restates history (the World Bank restates the whole WDI series
-- on every release) makes a single-time table lie about what was knowable
-- when, and no amount of reading can recover it afterwards.
--
-- NATIVE RANGE PARTITIONING BY `valid_from`, one partition per year plus a
-- DEFAULT (§D.4 — no TimescaleDB). Ten years of annual series for four
-- desks is a small table; the partitioning is not for size, it is so an era
-- read ("what did we hold for 2016–2018") touches three partitions and a
-- decade-wide collection cannot be loaded into one undifferentiated heap.
-- 2016–2027 is the range the pilot needs plus headroom; anything outside it
-- lands in the DEFAULT partition rather than failing the insert, and a
-- later migration can split the default when a collection reaches past it.
--
-- POSTGRES REQUIRES THE PARTITION KEY IN EVERY UNIQUE INDEX, so the primary
-- key is `(id, valid_from)` rather than `(id)` alone, and the business
-- unique key carries `valid_from` too — which it would anyway, being the
-- period the number is about. The unique key is what makes the loader
-- IDEMPOTENT: loading the same collection version twice writes the same
-- rows, because the second write conflicts on
-- (collection_id, series_id, subject, valid_from, valid_to, record_time)
-- and does nothing.
--
-- NO ROW IS WRITTEN FOR A YEAR A PROVIDER DOES NOT HOLD. Absence is
-- absence: the EIA stopped publishing international crude trade in 2021 and
-- the manifest records the missing tail as a hole. A zero row would be a
-- number nobody published, and every average over the series would be
-- wrong. `value`/`value_text` therefore carry a CHECK that EXACTLY ONE of
-- them is present — a row with neither is not an observation.
--
-- `origin_class` is CHECKed to the three HISTORY classes only. A collection
-- can never write `live`, `web_retrieval` or `seed`: those are what the
-- firewall exists to keep it out of, and the constraint is what makes a
-- mis-stamped loader fail at the table rather than in a reader six weeks
-- later.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 3. `entity_aliases` — provider naming → our entity ids
-- ─────────────────────────────────────────────────────────────────────────
-- A provider says "Iran, Islamic Rep."; `entity_profiles` says something
-- else. This maps one to the other WITHOUT touching resolution: it is read
-- by the collection loader and by 7g-2's readers, never by the entity
-- resolver, so a provider's naming quirk can never re-shape the live graph.
-- Scoped by `collection_id` because the same surface form can mean
-- different things in different holdings. NOT a resurrection of the
-- `entity_alias` table 0086 created and 0119 dropped — that one was the
-- write-time canonicalization surface for live ingestion and had no reader;
-- this one is per-collection and has two.
--
-- No FOREIGN KEY to `entity_profiles`: entity rows are garbage-collected
-- (`deterministic_handlers/entity_gc.py`) and a collection's alias map must
-- not either block that or vanish with it. A dangling alias resolves to
-- nothing, which is the honest answer.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 4. `collection_loads` — one row per (collection, version)
-- ─────────────────────────────────────────────────────────────────────────
-- The resume ledger and the receipt. A load is an operator action run once
-- per collection VERSION; the row carries the resume key (the last
-- (series, subject) pair completed), the counts, the timestamps and the
-- status, so `--resume` picks up where an interrupted run stopped and so
-- the reviewer can read what a load actually did without re-deriving it
-- from `observations`.
--
-- ─────────────────────────────────────────────────────────────────────────
-- 5. THE ORIGIN-CLASS READER SWEEP (SEAMS #57 → resolved, #62 → opened)
-- ─────────────────────────────────────────────────────────────────────────
-- 0209 named the seam `<table>_origin_class_readers_not_swept` and said the
-- migration that lands the reader sweep drops it. This is that migration,
-- and it RENAMES rather than drops — because between 0209 and here the
-- constraint's job changed, and dropping it would have thrown away a guard
-- that is still load-bearing for a different reason.
--
-- WHAT THE SWEEP DID. Every reader on the eight surfaces the collection
-- firewall fences — cadence analysts, freshness, source health, calibration,
-- salience, alerts, reactive triggers, surge detection — now takes its gate
-- from `data/provenance/origin.py` (`origin_class_clause` /
-- `live_gate_sql`), and so does every open-row READ and SUPERSESSION site on
-- `facts` and `events`. The reactive trigger plane gets a ROW-LEVEL
-- predicate as well (`runtime/triggers/coalescer.py:is_live_origin`), so a
-- non-live row is dropped before it is counted. That inventory is pinned
-- exactly by `tests/data_pkg/test_origin_gate_inventory.py`, and finishing
-- it is what closes SEAMS #57: the READERS are swept.
--
-- WHAT IS STILL MISSING, AND WHY THE CHECK STAYS. Nothing can WRITE a
-- history row to these three tables. Series go to `observations`; documents
-- would go to `signals` through loader kinds that are not built; and the
-- history half of `facts` has an unsolved shape of its own — the partial
-- unique index `idx_facts_temporal_triple_open` keys on
-- (subject, predicate, value, valid_from) WHERE the row is open, with no
-- origin_class leg, so an archived 2016 fact and a live 2026 one asserting
-- the same triple would COLLIDE at the index rather than coexist. That is a
-- write-path design question, not a reader gap, and it belongs to the seam
-- that owns the history WRITE path.
--
-- So the three CHECKs are renamed to
-- `<table>_origin_class_history_writer_not_built` (SEAMS #62). The name is
-- the whole point: it is what a premature backfill sees in its error, and it
-- now names the seam it actually hit. The old name is gone from the tree, so
-- #57 closes without leaving its own guard rail standing under its name.
--
-- CREATE-only + idempotent: every CREATE is IF NOT EXISTS and the three
-- constraint operations are guarded, so a second apply against an
-- already-migrated database is a no-op. The runner wraps this file in its
-- own transaction and records it in `legba_data_migrations` (no inline
-- BEGIN/COMMIT — same as 0204/0206/0207/0209).

-- ── 1. the descriptor family's table ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.collection_descriptors (
    descriptor_id       text NOT NULL,
    version             text NOT NULL,
    schema_uri          text NOT NULL,
    is_head             boolean NOT NULL DEFAULT true,
    abstraction_level   text NOT NULL DEFAULT 'L0',
    state               text NOT NULL DEFAULT 'draft',
    owner               text NOT NULL,
    name                text NOT NULL,
    body                jsonb NOT NULL,
    inherits            text[] NOT NULL DEFAULT '{}'::text[],
    created_at          timestamptz NOT NULL DEFAULT now(),
    retire_after        timestamptz,
    origin_shape        text NOT NULL,
    origin_class        text NOT NULL,
    licence_class       text NOT NULL,
    collection_version  text NOT NULL,
    CONSTRAINT collection_descriptors_pkey
        PRIMARY KEY (descriptor_id, version),
    CONSTRAINT collection_descriptors_state_vocab
        CHECK (state IN ('draft','reviewed','loaded','superseded')),
    CONSTRAINT collection_descriptors_origin_shape_vocab
        CHECK (origin_shape IN ('source_with_history',
                                'source_without_history',
                                'archive_only')),
    CONSTRAINT collection_descriptors_origin_class_history_only
        CHECK (origin_class IN ('archive','backfill_native',
                                'backfill_reconstructed')),
    CONSTRAINT collection_descriptors_licence_class_vocab
        CHECK (licence_class IN ('public','licensed_commercial',
                                 'licensed_noncommercial','restricted',
                                 'internal'))
);

CREATE UNIQUE INDEX IF NOT EXISTS collection_descriptors_head_unique
    ON public.collection_descriptors USING btree (descriptor_id)
    WHERE is_head;
CREATE INDEX IF NOT EXISTS collection_descriptors_schema_idx
    ON public.collection_descriptors USING btree (schema_uri);
CREATE INDEX IF NOT EXISTS collection_descriptors_state_idx
    ON public.collection_descriptors USING btree (state);

COMMENT ON TABLE public.collection_descriptors IS
    'Program 7g: the `collection` descriptor family — bounded, versioned '
    'HOLDINGS of the past. File convention descriptors/collection_*.yaml, '
    'never source_. Lifecycle draft -> reviewed -> loaded -> superseded.';
COMMENT ON COLUMN public.collection_descriptors.collection_version IS
    'The hash of the MANIFEST (not of the descriptor): re-approving a '
    'licence line must not invalidate a load that already happened. The key '
    'collection_loads joins on.';

-- ── 2. observations ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.observations (
    id             uuid NOT NULL DEFAULT gen_random_uuid(),
    collection_id  text NOT NULL,
    series_id      text NOT NULL,
    subject_kind   text NOT NULL,
    subject        text NOT NULL,
    valid_from     date NOT NULL,
    valid_to       date NOT NULL,
    record_time    timestamptz NOT NULL,
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    value          numeric,
    unit           text NOT NULL,
    value_text     text,
    source_url     text NOT NULL,
    sha256         text NOT NULL,
    origin_class   text NOT NULL,
    provenance     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT observations_pkey PRIMARY KEY (id, valid_from),
    CONSTRAINT observations_subject_kind_vocab
        CHECK (subject_kind IN ('country','entity','target','global')),
    CONSTRAINT observations_origin_class_history_only
        CHECK (origin_class IN ('archive','backfill_native',
                                'backfill_reconstructed')),
    CONSTRAINT observations_period_ordered
        CHECK (valid_to >= valid_from),
    CONSTRAINT observations_exactly_one_value
        CHECK ((value IS NOT NULL) <> (value_text IS NOT NULL))
) PARTITION BY RANGE (valid_from);

-- The IDEMPOTENCY key. A second load of the same collection version writes
-- the same rows and conflicts here, doing nothing.
CREATE UNIQUE INDEX IF NOT EXISTS observations_identity_unique
    ON public.observations USING btree
    (collection_id, series_id, subject, valid_from, valid_to, record_time);

-- The two reads 7g-2 will make (§6), indexed now so they are proven before
-- the readers exist:
--   (a) a series window for one subject — collection + series + subject,
--       ordered by valid_from;
--   (b) a subject's latest record_time per valid period — collection +
--       subject across ALL series, DISTINCT ON (series, period) ordered by
--       record_time DESC. (a) rides the unique index above; (b) needs its
--       own leading (collection_id, subject).
CREATE INDEX IF NOT EXISTS observations_subject_period_idx
    ON public.observations USING btree
    (collection_id, subject, series_id, valid_from, valid_to,
     record_time DESC);

COMMENT ON TABLE public.observations IS
    'Program 7g: bitemporal series rows from a collection. valid_from/'
    'valid_to = the period the number is ABOUT; record_time = when the '
    'provider published or revised it. A 2016 figure revised in 2023 is two '
    'rows. No row exists for a year a provider does not hold — absence is '
    'absence, never a zero.';
COMMENT ON COLUMN public.observations.sha256 IS
    'Digest of the FILE the number came from (the fetched JSON body, or the '
    'provider bulk archive) — what makes a cited observation checkable.';
COMMENT ON COLUMN public.observations.provenance IS
    'Provider revision id, row offset, the manifest version that wrote the '
    'row, and the licence class it was loaded under.';

DO $$
DECLARE
    y int;
BEGIN
    FOR y IN 2016..2027 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS public.observations_%s '
            'PARTITION OF public.observations '
            'FOR VALUES FROM (%L) TO (%L)',
            y, format('%s-01-01', y), format('%s-01-01', y + 1));
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS public.observations_default
    PARTITION OF public.observations DEFAULT;

-- ── 3. entity_aliases ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.entity_aliases (
    alias          text NOT NULL,
    entity_id      uuid NOT NULL,
    collection_id  text NOT NULL,
    source         text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT entity_aliases_pkey PRIMARY KEY (collection_id, alias)
);

CREATE INDEX IF NOT EXISTS entity_aliases_entity_idx
    ON public.entity_aliases USING btree (entity_id);

COMMENT ON TABLE public.entity_aliases IS
    'Program 7g: provider naming -> entity_profiles, scoped per collection. '
    'Read by the collection loader and 7g-2 readers ONLY — never by entity '
    'resolution, so a provider naming quirk cannot re-shape the live graph. '
    'No FK to entity_profiles (those rows are GC-able).';

-- ── 4. collection_loads ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.collection_loads (
    id                  uuid NOT NULL DEFAULT gen_random_uuid(),
    collection_id       text NOT NULL,
    collection_version  text NOT NULL,
    descriptor_version  text,
    loader_kind         text NOT NULL,
    resume_key          text,
    status              text NOT NULL DEFAULT 'running',
    pairs_total         integer NOT NULL DEFAULT 0,
    pairs_done          integer NOT NULL DEFAULT 0,
    rows_written        bigint  NOT NULL DEFAULT 0,
    rows_skipped        bigint  NOT NULL DEFAULT 0,
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    error               text,
    provenance          jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT collection_loads_pkey PRIMARY KEY (id),
    CONSTRAINT collection_loads_version_unique
        UNIQUE (collection_id, collection_version),
    CONSTRAINT collection_loads_status_vocab
        CHECK (status IN ('running','completed','failed','interrupted')),
    CONSTRAINT collection_loads_counts_nonnegative
        CHECK (pairs_total >= 0 AND pairs_done >= 0
               AND rows_written >= 0 AND rows_skipped >= 0)
);

CREATE INDEX IF NOT EXISTS collection_loads_collection_idx
    ON public.collection_loads USING btree (collection_id, started_at DESC);

COMMENT ON TABLE public.collection_loads IS
    'Program 7g: one row per (collection, manifest version). The resume '
    'ledger and the receipt — resume_key is the last (series, subject) pair '
    'completed, so --resume picks up where an interrupted run stopped.';

-- ── 5. the origin-class reader sweep ───────────────────────────────────────

-- The readers are swept (SEAMS #57 resolved); no history WRITER exists for
-- these three tables (SEAMS #62 opened). Rename each CHECK so the error a
-- premature backfill sees names the seam it actually hit. Idempotent: the
-- rename runs only while the old name is present, and the constraint is
-- re-created only if neither name is.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['signals','facts','events'] LOOP
        IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid = format('public.%I', t)::regclass
               AND conname = format('%s_origin_class_readers_not_swept', t)
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.%I RENAME CONSTRAINT '
                '%I TO %I',
                t,
                format('%s_origin_class_readers_not_swept', t),
                format('%s_origin_class_history_writer_not_built', t));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid = format('public.%I', t)::regclass
               AND conname = format('%s_origin_class_history_writer_not_built', t)
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.%I ADD CONSTRAINT %I '
                'CHECK (origin_class IN (''live'',''web_retrieval'',''seed''))',
                t,
                format('%s_origin_class_history_writer_not_built', t));
        END IF;
    END LOOP;
END $$;

COMMENT ON COLUMN public.signals.origin_class IS
    'V3/P7: closed six-class provenance vocabulary (live·web_retrieval·seed·'
    'backfill_native·backfill_reconstructed·archive). The READERS are swept '
    '(SEAMS #57 resolved); signals_origin_class_history_writer_not_built '
    'refuses the three history classes because no history WRITER exists for '
    'this table yet (SEAMS #62). Collection SERIES land in `observations`.';
COMMENT ON COLUMN public.facts.origin_class IS
    'V3/P7: closed six-class provenance vocabulary; the live gate is '
    'origin_class_clause()/live_gate_sql() in data/provenance/origin.py, and '
    'every open-row read and supersession site now carries it (SEAMS #57 '
    'resolved). facts_origin_class_history_writer_not_built refuses a '
    'history-class row: the open-triple partial unique index carries no '
    'origin_class leg, so a history fact would COLLIDE with a live one '
    'rather than coexist (SEAMS #62).';
