-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0217_inquiry_kind_vocab.sql
--
-- PROGRAM 5 lane 2 (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §7.2) — the
-- `inquiry` analyst kind's registry vocabulary row.
--
-- WHAT THIS IS NOT. It is NOT the ledger: `inquiry_ledger` and its three named
-- constraints are migration 0215 (lane p5_state). It is NOT an `entry_kind`
-- widening either — `journal_entries.entry_kind` is a free TEXT column with no
-- CHECK constraint (the vocabulary is pinned in Python: JournalPayload's
-- Literal, journal_api._VALID_KINDS, export_api.JOURNAL_TIER_LABELS, all three
-- widened in the same commit as this file), so the two new values `inquiry` and
-- `crossroads` need no DDL at all.
--
-- WHAT IT IS. `inquiry` is an EXTENSION analyst kind — not a member of the
-- closed AnalystKind enum. The RUNTIME process registers it in-code
-- (legba.data.analysts.__init__), but the descriptor REGISTRY seeds its
-- kind-name validator from `vocabulary_entries` and REPLACES the extension set
-- on every refresh, so without this row the registry rejects an inquiry
-- descriptor PUT with "unknown analyst kind". journal_assessor /
-- entity_researcher / signal_salience each got their row by hand at deploy
-- time and situation_tracker (0184) was the first to declare it in the schema
-- instead; this follows 0184 for the same reason — the schema and the code land
-- together rather than leaving a step an operator can forget between them.
--
-- The CROSSROADS descriptor (lane p5_crossroads) rides this same kind, so it
-- needs no row of its own: the tier is the descriptor id, never a second kind.

INSERT INTO public.vocabulary_entries (family, value, notes)
VALUES (
    'analyst_kind',
    'inquiry',
    'Program 5 — the stateful journal voice: a standing investigation with a '
    'PUT-able brief and an inquiry_ledger carried across cycles (0216).'
)
ON CONFLICT (family, value) DO NOTHING;
