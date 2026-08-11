-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- ==========================================================================
-- 0170 — the correctness axis, recorded in the schema (M-1).
-- ==========================================================================
-- WHY:
--   Legba has carried TWO correctness gold tables since 2026-07-24, and until
--   today the machinery read the wrong one.
--
--   `unit_reference_labels` (mig 0057) backs the deterministic source-id
--   overlap scorer. Live count: ONE row — for `p2_probe_unit`, a RETIRED
--   analyst, with zero `canonical_source_ids`. By the scorer's own null rule
--   (|G| == 0 -> None) that row is unscorable, so no live unit has ever had a
--   usable reference label and none was ever going to appear by itself. The
--   scorer reported `correctness=None` every day since 2026-07-02, and GEPA's
--   promotion gate counted the same empty table and could never change state.
--
--   `correctness_labels` (mig 0096) holds the weekly gold-set loop's OPERATOR
--   verdicts. Live count: EIGHT, across seven units, labelled 2026-07-28.
--   Weighted (correct 1.0 / partially_correct 0.5 / incorrect 0.0, with
--   `unresolvable` excluded from both numerator and denominator) they score
--   0.625 against a same-window faithfulness of 0.92 — the platform's only
--   judge-independent quality signal, and the only one that can say what
--   faithfulness structurally cannot: a finding can be scrupulously faithful to
--   citations that do not support the read it drew from them.
--
--   The 2026-08-02 engine review found that number computed, stored, and
--   surfaced nowhere a reader would meet it.
--
-- WHAT THIS MIGRATION DOES:
--   Nothing to the data. It writes the DECISION into the schema, because the
--   next person to meet these tables will meet them through `\d`, ask why one
--   of them is empty, and deserve an answer that is not a git archaeology
--   exercise. Table and column comments are the schema's own documentation and
--   they travel with the database.
--
--   The deterministic axis is DEMOTED, not retired: the arithmetic is correct,
--   it costs nothing, and it becomes live the instant somebody writes a
--   reference label for a live unit. It simply must not be the headline while
--   it is structurally incapable of producing one.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO:
--   It does not merge, copy, or reconcile the two tables. They measure
--   different things with different evidence — one is a human's read of the
--   prose, the other is set overlap on provenance ids — and mig 0096's header
--   already argued that folding them would "muddle two distinct measurements".
--   That argument still holds; the fix was never to merge them, it was to read
--   the one that is fed.
--
--   It does not add an index. `correctness_labels` is hand-labelled and will
--   stay small by construction (8 rows after two weeks of the loop);
--   `idx_correctness_labels_unit` from mig 0096 already covers the per-unit
--   aggregate, and the fleet read is a full scan of a table that will not
--   outgrow one page for a long time. An index here would be cargo cult.
--
-- SAFETY (idempotent, additive, forward-only): COMMENT statements only. No
-- table, column, index, or row is created, altered, or dropped. Re-apply and
-- cold-start are both no-ops. `COMMENT ON` on a missing relation would error,
-- so each is guarded by a to_regclass existence check — a fresh clone that has
-- not yet applied 0057/0096 skips silently rather than failing the runner. The
-- runner wraps this file in its own transaction (no inline BEGIN/COMMIT).

DO $$
BEGIN
    IF to_regclass('public.correctness_labels') IS NOT NULL THEN
        COMMENT ON TABLE public.correctness_labels IS
            'PRIMARY correctness axis (M-1, 2026-08-03). Per-finding OPERATOR '
            'semantic verdicts from the weekly gold-set loop — the platform''s '
            'only JUDGE-INDEPENDENT quality signal. Weighted correct=1.0 / '
            'partially_correct=0.5 / incorrect=0.0, with unresolvable excluded '
            'from BOTH numerator and denominator. One definition of that '
            'arithmetic exists: legba.data.correctness_axis, shared by '
            'unit_correctness_scorer, the /eval/scores overlay, the scorecard '
            'eval fold, GET /v3/eval/correctness and the GEPA promotion record. '
            'NEVER pooled into faithfulness, the Brier plane, or the band '
            'rates. Hand-labelled and small by construction: below the axis '
            'floors the mean is reported as INDICATIVE, never as a measured '
            'rate.';

        COMMENT ON COLUMN public.correctness_labels.label IS
            'Closed vocabulary. unresolvable = the operator looked and could '
            'not judge: excluded from both sides of the mean, reported in the '
            'verdict mix, and never scored as wrongness.';
    END IF;

    IF to_regclass('public.unit_reference_labels') IS NOT NULL THEN
        COMMENT ON TABLE public.unit_reference_labels IS
            'SECONDARY (diagnostic) correctness axis — DEMOTED 2026-08-03 '
            '(M-1), not retired. Backs unit_correctness_scorer''s deterministic '
            'source-id overlap recall (|C n G| / |G| over canonical_source_ids). '
            'It held ONE row from 2026-07-24 to 2026-08-03 — a retired analyst, '
            'zero source ids, unscorable by the scorer''s own |G|==0 rule — so '
            'the axis reported None every day of its life while being the '
            'headline. The arithmetic is correct and costs nothing, and the '
            'axis goes live the moment a reference label is written for a LIVE '
            'unit; until then the headline is the operator gold set in '
            'correctness_labels. The two are never pooled: different evidence '
            '(human read of prose vs set overlap on provenance ids), different '
            'n, either may be null while the other is not.';

        COMMENT ON COLUMN public.unit_reference_labels.canonical_source_ids IS
            'The gold answer''s load-bearing provenance rows. EMPTY means the '
            'row is UNSCORABLE (recall has no denominator) — the scorer skips '
            'it and reports None, never 0.0. A text-only label is not a '
            'measurement of wrongness.';
    END IF;
END $$;
