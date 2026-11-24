-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0230_events_event_type_unclassified.sql
--
-- `events.event_type` ADMITS `unclassified` (W9 TAX-4).
--
-- WHY
-- ---
-- Every case-bearing column now resolves to a registered value or to
-- `unclassified` (`legba.data.case_columns`). `events.event_type` is bound to
-- the `event_type` platform vocabulary family (seeded from
-- descriptors/vocabulary_event_type.yaml), and the column carries its own
-- CHECK (migration 0202: incident / development / shift / threshold). A value
-- the family does not hold — the family narrowed by an operator, or a producer
-- emitting a word the family never registered — must land as `unclassified`,
-- never be guessed into `incident` and never fail the event write. The 0202
-- CHECK refuses `unclassified`, so this replaces it with a strict superset.
--
-- The family can still only NARROW the column: a value registered in the
-- family but absent here binds `unclassified` at write time
-- (`case_columns.event_type_values` intersects the family with this set).
--
-- SAFETY
-- ------
-- The replacement CHECK is a strict superset of 0202's, so no existing row can
-- violate it (live 2026-10-04: incident 1,303 / development 521, 1,824 rows —
-- the validating scan is over a table of that size). Additive and
-- forward-only; re-apply is a no-op (DROP IF EXISTS + ADD), routed through the
-- runner (no inline BEGIN/COMMIT). No backfill: nothing live is outside the
-- family; `scripts/retrofit_case_columns.py --apply` is the operator's act.

ALTER TABLE public.events
    DROP CONSTRAINT IF EXISTS events_event_type_check;

ALTER TABLE public.events
    ADD CONSTRAINT events_event_type_check CHECK (event_type IN (
        'incident',
        'development',
        'shift',
        'threshold',
        -- W9 TAX-4: a value outside the registered `event_type` family.
        'unclassified'
    ));

COMMENT ON CONSTRAINT events_event_type_check ON public.events IS
    'The event_type vocabulary family (descriptors/vocabulary_event_type.yaml) '
    'plus ''unclassified'' (mig 0230): a value outside the registered family '
    'binds unclassified at write time (legba.data.case_columns). The family can '
    'narrow this set, never widen it.';
