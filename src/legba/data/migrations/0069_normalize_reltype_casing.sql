-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0069_normalize_reltype_casing.sql  (DQ Phase 5 / facts-nexuses finding
--   "nexuses / signal-to-noise" — rel_type vocabulary drift)
--
-- PROBLEM: the nexuses table carries CamelCase rel_type variants
--   ('MemberOf'/'HostileTo'/'LocatedIn'/...) alongside their lowercase-spaced
--   canon ('member of'/'hostile to'/'located in'), so any GROUP BY / signed-
--   graph read splits across the two surfaces and the lower(rel_type) open-
--   triple unique index / supersession never lines up. These are historical seed
--   rows written before the write-path convergence landed.
--
-- PAIRED CODE FIX (already in place, keeps new ones out): write_nexus routes
--   rel_type through legba.data.vocabulary._canonical_rel_type ->
--   normalize_predicate, which folds every CamelCase form to the lowercase-
--   spaced canon at write. This migration converges the historical residue.
--
-- THIS MIGRATION (deterministic, two steps):
--   (A) A CamelCase row whose canonical triple ALREADY has an OPEN canonical
--       row (e.g. seed 'Germany member of NATO' next to 'Germany MemberOf NATO')
--       is a duplicate — CLOSE it (valid_until=now(), superseded_by=<canon row>)
--       so renaming it cannot violate the open-triple unique index.
--   (B) Every remaining OPEN CamelCase row is RENAMED to its canon (the prior
--       surface stashed in data.dq_p5_reltype_from for reversibility). Verified
--       live: no two CamelCase rows fold to the same canonical triple, so the
--       rename never creates a duplicate.
--
-- REVERSIBLE: (A) reopen via valid_until/superseded_by; (B) restore rel_type
--   from data.dq_p5_reltype_from. IDEMPOTENT: after the run no OPEN CamelCase
--   variant remains, so both steps join nothing on re-run. Routed through the
--   migration runner (ONE transaction + ledger row; NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): 42 open CamelCase rows — (A) 23 closed
--   as duplicates of an existing open canonical triple, (B) 19 renamed.

-- (A) Close CamelCase rows that duplicate an existing OPEN canonical triple.
WITH canon(variant, canonical) AS (
    VALUES
      ('AffiliatedWith','affiliated with'),
      ('AlliedWith','allied with'),
      ('ConductedVia','conducted via'),
      ('CoOccursWith','co occurs with'),
      ('HostileTo','hostile to'),
      ('LeaderOf','leader of'),
      ('LocatedIn','located in'),
      ('MemberOf','member of'),
      ('OperatesIn','operates in'),
      ('Targets','targets')
),
collide AS (
    SELECT v.id AS loser_id, o.id AS canon_id
    FROM nexuses v
    JOIN canon m ON v.rel_type = m.variant
    JOIN nexuses o
      ON o.valid_until IS NULL AND o.superseded_by IS NULL AND o.id <> v.id
     AND lower(o.subject) = lower(v.subject)
     AND lower(COALESCE(o.intermediary, '')) = lower(COALESCE(v.intermediary, ''))
     AND lower(o.object) = lower(v.object)
     AND lower(o.rel_type) = m.canonical
    WHERE v.valid_until IS NULL AND v.superseded_by IS NULL
)
UPDATE nexuses n
SET valid_until   = now(),
    superseded_by = c.canon_id,
    updated_at    = now()
FROM collide c
WHERE n.id = c.loser_id
  AND n.valid_until IS NULL;

-- (B) Rename the remaining OPEN CamelCase variants to their lowercase canon.
WITH canon(variant, canonical) AS (
    VALUES
      ('AffiliatedWith','affiliated with'),
      ('AlliedWith','allied with'),
      ('ConductedVia','conducted via'),
      ('CoOccursWith','co occurs with'),
      ('HostileTo','hostile to'),
      ('LeaderOf','leader of'),
      ('LocatedIn','located in'),
      ('MemberOf','member of'),
      ('OperatesIn','operates in'),
      ('Targets','targets')
)
UPDATE nexuses n
SET rel_type   = m.canonical,
    data       = COALESCE(n.data, '{}'::jsonb)
                 || jsonb_build_object('dq_p5_reltype_from', n.rel_type),
    updated_at = now()
FROM canon m
WHERE n.rel_type = m.variant
  AND n.valid_until IS NULL
  AND n.superseded_by IS NULL;
