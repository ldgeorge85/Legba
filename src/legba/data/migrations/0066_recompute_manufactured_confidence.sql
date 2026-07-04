-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0066_recompute_manufactured_confidence.sql  (DQ Phase 5 / facts-nexuses
--   finding "facts / confidence semantics")
--
-- PROBLEM: ingestion facts carry the documented heuristic FLOOR (0.5, the
--   relation extractor emits no real per-triple score), but the corroboration
--   noisy-OR compounds repeated floors toward near-certainty — syndicated junk
--   repeated across outlets reaches 0.99 ('Thousands located in South Africa'
--   0.99, 'Kyiv capital of Ukrainian' 0.99). Separately the exact-1.0 sentinel
--   (a "no real score" marker on the facts path) still leaks onto agent nexuses
--   (44 open agent nexuses at conf=1.0).
--
-- PAIRED CODE FIX (keeps it out):
--   * fact_extractor.py — the ingestion re-observation noisy-OR ceiling is
--     capped at 0.75 for floor-sourced observations (corroboration count is no
--     longer laundered into confidence).
--   * relationship_reifier.py — an exact-1.0 nexus confidence is treated as the
--     "no real score" sentinel and floored (mirrors the facts path).
--
-- THIS MIGRATION recomputes the already-written rows (a value RECOMPUTE, not a
--   close). It preserves the prior value so the change is REVERSIBLE:
--   (A) open ingestion facts with confidence>0.75 whose score is NOT a genuine
--       extractor_score -> confidence:=0.75, prior value stashed in
--       confidence_components.dq_p5_capped_from.
--   (B) open agent nexuses with confidence=1.0 -> confidence:=0.5, prior value
--       stashed in data.dq_p5_conf_floored_from.
--   Seed facts (0.92/0.95), manual facts, and genuinely-scored extractor facts
--   are NEVER touched (source/threshold guards).
--
-- REVERSIBLE: restore confidence from the stashed *_from key. IDEMPOTENT: after
--   the recompute the threshold no longer matches AND the stash-key guard blocks
--   a second write -> re-run is a no-op. Routed through the migration runner
--   (ONE transaction + ledger row; NO inline BEGIN/COMMIT).
--
-- MEASURED (live `legba`, 2026-07-03): (A) 38 facts (0.875–0.99 band; 0 in
--   scope carry a real extractor_score); (B) 44 agent nexuses.

-- (A) Cap noisy-OR-inflated heuristic-floor ingestion facts at 0.75.
UPDATE facts f
SET confidence = 0.75,
    confidence_components = COALESCE(f.confidence_components, '{}'::jsonb)
        || jsonb_build_object(
             'dq_p5_capped_from', f.confidence,
             'note', 'DQ P5: noisy-OR heuristic-floor ceiling cap (0.75)'
           ),
    updated_at = now()
WHERE f.valid_until IS NULL
  AND f.source_type = 'ingestion'
  AND f.confidence > 0.75
  AND COALESCE(f.data->'confidence_components'->>'source', 'heuristic_floor') <> 'extractor_score'
  AND (f.confidence_components->>'dq_p5_capped_from') IS NULL;

-- (B) Floor the exact-1.0 sentinel that leaked onto agent nexuses to 0.5.
UPDATE nexuses n
SET confidence = 0.5,
    data = COALESCE(n.data, '{}'::jsonb)
        || jsonb_build_object(
             'dq_p5_conf_floored_from', n.confidence,
             'note', 'DQ P5: exact-1.0 sentinel floor (0.5)'
           ),
    updated_at = now()
WHERE n.valid_until IS NULL
  AND n.source_type = 'agent'
  AND n.confidence = 1.0
  AND (n.data->>'dq_p5_conf_floored_from') IS NULL;
