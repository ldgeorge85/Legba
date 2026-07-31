-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0117_soft_close_prefix_relational_facts.sql
--
-- DATA REPAIR (2026-07-31 DQ sweep, finding R1). Every relation-backend fact
-- written before the pairing fix was produced by `_entities_to_triples()`
-- pairing a document-wide, non-text-ordered entity list BY INDEX — a 15-fact
-- spot-check passed 2. The fixed extractor binds the relation model's real
-- subject/object pairs and stamps a `source_ids` corroboration ledger on
-- every fact it writes, so `NOT (data ? 'source_ids')` is an exact marker
-- for "written by the broken pairer".
--
-- SOFT-CLOSE, NOT DELETE: `valid_until = now()` ends the fact's validity
-- while preserving the row, its lineage, and its receipts — reversible by
-- nulling `valid_until` on any subset later shown good. The stamp
-- `data.closed_by = 'mig_0117_prefix_pairing'` makes the cohort queryable.
-- The fixed extractor repopulates the family from live flow.

UPDATE facts
   SET valid_until = now(),
       data = data || '{"closed_by": "mig_0117_prefix_pairing"}'::jsonb
 WHERE source_type = 'ingestion'
   AND data->>'extractor' = 'fact_extractor'
   AND data->>'backend' = 'relation'
   AND NOT (data ? 'source_ids')
   AND valid_until IS NULL
   AND superseded_by IS NULL;
