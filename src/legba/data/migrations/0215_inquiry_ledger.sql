-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0215_inquiry_ledger.sql
--
-- Program 5 lane 1 — the inquiry ledger
-- (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §3).
--
-- An inquiry is STATEFUL (§1): it carries a brief, hypotheses each with a
-- resolution test, questions it has dispatched into the platform's own
-- machinery (open_questions / the contested-claims arbiter / a desk's next
-- cadence), and observations it expects a desk to later carry forward. This
-- ONE table is its continuity — the `inquiry_state` action pack
-- (src/legba/data/analysts/agency/inquiry_state.py) is its ONLY read/write
-- surface, every tool scoped to the calling descriptor
-- (inquiry_state.py:scope_fence).
--
-- kind vocabulary: hypothesis | question | observation | expectation.
-- status vocabulary: open | confirmed | refuted | answered | expired | withdrawn.
--
-- THE SEALED-LEDGER DISCIPLINE (H13, mirroring migration 0212's
-- `acute_forecasts.resolution_test`): a `hypothesis` row with no
-- `resolution_test` is refused at INSERT — `inquiry_ledger_hypothesis_needs_test`.
-- A test decided AFTER the fact is a test that can be softened to fit the
-- outcome; freezing it at write is the whole point.
--
-- `cited_refs` is written ONLY by `ledger_close` (§4: "an observation whose
-- cited_refs a later finding cites" — the citation IS the close-time warrant,
-- never a decoration at open); `ledger_write` never takes it. `closed_by_entry`
-- is a PLAIN uuid, NOT a self-FK (mirrors `journal_entries.superseded_by`,
-- migration 0048) — lane 2's journal-write path stamps it once a journal
-- entry narrates the close; this migration only carries the column.
--
-- `inquiry_ledger_closed_at_matches_status`: the row's own state machine —
-- `status = 'open'` iff `closed_at IS NULL`. A row cannot claim a terminal
-- status with no close timestamp, or claim 'open' while carrying one.
--
-- WHAT (idempotent: CREATE TABLE / INDEX ... IF NOT EXISTS only; additive,
-- no data migration — this is a net-new table).

CREATE TABLE IF NOT EXISTS public.inquiry_ledger (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- the inquiry (analyst) descriptor that owns this row — the scope fence's
    -- one predicate, on every read/write/close.
    descriptor_id   text        NOT NULL,
    kind            text        NOT NULL,
    text            text        NOT NULL,
    status          text        NOT NULL DEFAULT 'open',
    -- frozen at mint for a hypothesis (H13); NULL for every other kind.
    resolution_test text,
    resolves_by     timestamptz,
    -- open_question id | contention id | desk analyst_id (§3) — free-form,
    -- deliberately no FK: the three referent tables share no common key.
    dispatched_to   text,
    -- the ONLY writer of this column is ledger_close (see module docstring).
    cited_refs      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    -- the journal_entries row (lane 2) that narrated this row's close, if any.
    -- A plain uuid, not a self-FK — mirrors journal_entries.superseded_by.
    closed_by_entry uuid,

    CONSTRAINT inquiry_ledger_kind_vocab CHECK (
        kind IN ('hypothesis', 'question', 'observation', 'expectation')
    ),
    CONSTRAINT inquiry_ledger_status_vocab CHECK (
        status IN ('open', 'confirmed', 'refuted', 'answered', 'expired', 'withdrawn')
    ),
    -- THE SEALED-LEDGER DISCIPLINE — see module banner above.
    CONSTRAINT inquiry_ledger_hypothesis_needs_test CHECK (
        kind <> 'hypothesis' OR resolution_test IS NOT NULL
    ),
    -- the row's own open/closed state machine, held consistent by the DB
    -- (not merely by the pack's own UPDATE shape).
    CONSTRAINT inquiry_ledger_closed_at_matches_status CHECK (
        (status = 'open') = (closed_at IS NULL)
    ),
    CONSTRAINT inquiry_ledger_text_nonempty CHECK (btrim(text) <> ''),
    CONSTRAINT inquiry_ledger_descriptor_id_nonempty CHECK (btrim(descriptor_id) <> '')
);

-- ledger_read / ledger_close both filter on (descriptor_id[, status]) — the
-- scope fence's own access pattern.
CREATE INDEX IF NOT EXISTS idx_inquiry_ledger_descriptor_status
    ON public.inquiry_ledger (descriptor_id, status);

COMMENT ON TABLE public.inquiry_ledger IS
    'Program 5 — one inquiry''s continuity: hypotheses (each with a frozen '
    'resolution_test), dispatched questions, observations, and expectations. '
    'One row-set per descriptor_id; the inquiry_state action pack is its only '
    'read/write surface, scoped per-tool to the calling descriptor.';
