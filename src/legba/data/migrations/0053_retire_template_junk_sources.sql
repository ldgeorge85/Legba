-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0053_retire_template_junk_sources.sql
--
-- Delete the P-13 autowire/template/disc-example JUNK source descriptors that
-- clutter the source panel. These are bring-up artifacts — never-wired template,
-- autowire, locked-template, and discovery-EXAMPLE source descriptors left over
-- from the P-13 source-first bring-up. They carry 0 ingested signals (ever) and
-- are noise on the operator's source surface.
--
-- TARGET (descriptor_id LIKE any of the 6 P-13 artifact families):
--   src_autowire_p13_%  — autowire bring-up sources              (21 ids, retired)
--   src_locked_p13_%    — locked template bring-up sources        (21 ids, retired)
--   src_template_p13_%  — template bring-up sources               (21 ids, retired)
--   src_tmpl_aw_%       — autowire template artifacts             (21 ids, retired)
--   src_tmpl_ds_%       — discovery template artifacts            (21 ids, configured)
--   src_disc_%          — discovery liveexample/newsexample demos ( 2 ids, draft +
--                         versioned history)
-- = 107 distinct descriptor_ids across 168 table rows (the two src_disc_% ids
--   carry many versioned non-head draft rows; the other 105 are single-row).
--
-- FK SAFETY — DELETE is clean (verified live 2026-06-26):
--   * No DB-level FOREIGN KEY references `source_descriptors` at all.
--   * The only columns that could logically point at a source descriptor id are
--     `signals.source_id`, `signals.produced_by_id`, and
--     `source_poll_outcomes.source_id`. ALL THREE match 0 rows for every one of
--     the 6 patterns — these junk sources produced nothing and were polled into
--     nothing, so a row DELETE orphans no downstream signal or poll outcome.
--   Because there is nothing to orphan, we DELETE (a state=retired UPDATE would
--   leave all 168 noise rows on the panel — 21 are already retired yet still
--   listed — so RETIRE would not clear the clutter this migration exists to fix).
--
-- IDEMPOTENT / transactional / data-only:
--   The runner wraps this file in its own transaction and records it in
--   `legba_data_migrations` (no inline BEGIN/COMMIT or ledger insert here — same
--   as 0044/0051/0052). The DELETE is independently idempotent: after the first
--   run the matching rows are gone, so a re-run matches 0. On a fresh cold-start
--   substrate no P-13 artifacts exist -> a clean no-op. The unconstrained DELETE
--   removes head and non-head versions together; nothing collides with the
--   `source_descriptors_head_unique` partial index on a delete.

DELETE FROM source_descriptors
WHERE descriptor_id LIKE 'src_autowire_p13_%'
   OR descriptor_id LIKE 'src_locked_p13_%'
   OR descriptor_id LIKE 'src_template_p13_%'
   OR descriptor_id LIKE 'src_tmpl_aw_%'
   OR descriptor_id LIKE 'src_tmpl_ds_%'
   OR descriptor_id LIKE 'src_disc_%';
