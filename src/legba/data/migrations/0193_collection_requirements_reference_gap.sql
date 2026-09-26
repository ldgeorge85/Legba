-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0193_collection_requirements_reference_gap.sql
--
-- A-4 (ATTENTION_MEASUREMENT_DESIGN_2026-09-05 §3.3, row D-m): the reference
-- diff's UNCOLLECTED item set side-writes `collection_requirements` as a THIRD
-- origin, `reference_gap`, beside the existing `collection_gap` (the internal,
-- monthly, starved-scorecard-cell signal) and `source_request` (the standing
-- assessor backlog).
--
-- WHY A MIGRATION AT ALL — the design says "zero new tables", and this adds
-- none. But migration 0113 wrote the vocabulary as two CHECK constraints:
--
--     origin        IN ('collection_gap', 'source_request')
--     evidence_kind IN ('analyst_output', 'hypothesis')
--
-- and a `reference_gap` row cites a `unit_reference_labels` row (migration
-- 0057's table, given its machine columns by 0191) which is NEITHER an
-- `analyst_outputs` row nor a `hypotheses` row. So both vocabularies have to
-- grow by exactly one value, or the writer fails closed at insert time with a
-- constraint violation that no amount of degrade-not-break handling can turn
-- into a written row.
--
-- Both constraints are KEPT rather than dropped. The alternative — dropping
-- them and enforcing the vocabulary in code, the 0112/0192 discipline — is
-- wrong here: `origin` is what the research program's dispatcher branches on
-- ("its only dependency on the research spec is that its dispatcher read
-- `origin` and not assume one value"), and `evidence_kind` is half of the
-- provenance-walk index `idx_collection_requirements_evidence`. A typo'd
-- origin would be an invisible row nothing dispatches; the CHECK is what makes
-- it a loud failure instead.
--
-- SAFETY (idempotent, additive, forward-only): each constraint is dropped and
-- re-added with a SUPERSET of its old vocabulary, so no existing row can be
-- invalidated (the 51 live rows are all `origin='collection_gap'`,
-- `evidence_kind='analyst_output'`). `IF EXISTS` on the drop and a
-- `DROP`-then-`ADD` pair make re-apply and cold-start both no-ops. The runner
-- wraps this file in its own transaction and records it in
-- `legba_data_migrations` (no inline BEGIN/COMMIT — same as 0113/0191/0192),
-- so a failure anywhere leaves the table exactly as it was.

ALTER TABLE public.collection_requirements
    DROP CONSTRAINT IF EXISTS collection_requirements_origin_check;

ALTER TABLE public.collection_requirements
    ADD CONSTRAINT collection_requirements_origin_check
    CHECK (origin IN ('collection_gap', 'source_request', 'reference_gap'));

ALTER TABLE public.collection_requirements
    DROP CONSTRAINT IF EXISTS collection_requirements_evidence_kind_check;

ALTER TABLE public.collection_requirements
    ADD CONSTRAINT collection_requirements_evidence_kind_check
    CHECK (evidence_kind IN ('analyst_output', 'hypothesis',
                             'unit_reference_label'));
