-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0121_retire_gdelt_doc_api.sql
--
-- SOURCE LIFECYCLE (2026-08-02 engine review, P1 §5). `source.gdelt.doc_api`
-- is still `state='active'` and still polling, even though `gdelt_files` was
-- purpose-built to REPLACE it — the 15-min file-dump adapter exists precisely
-- because doc_api is IP-throttled. Nothing in the docs disposes of the old
-- descriptor: this is an un-retired legacy row, not a deliberate dual-source
-- decision, and it costs a per-IP rate-limit budget that the successor needs.
--
-- Verified read-only before writing (7-day window):
--   source.gdelt.doc_api  326 polls — 273 error (84.3%, all HTTP 429 GDELT
--                         rate-limiting) / 51 success / 2 empty; 229 signals.
--   source.gdelt.files    521 polls — 481 success (92.3%) / 40 error (the
--                         inherent "dump not published yet" 404); 8,974
--                         signals, and it is `state='active'`, `is_head`.
-- The successor carries ~39x the volume at an inverted success rate. Retiring
-- doc_api removes redundant load and stops it polluting the source-health
-- error ledger with failures that signify nothing.
--
-- RETIRE, NOT DELETE — the opposite call from 0053, and for the opposite
-- reason. Those P-13 template rows had 0 signals and 0 poll outcomes, so a
-- DELETE orphaned nothing. doc_api has real ingested history (signals +
-- source_poll_outcomes + a source-quality track record) that must keep its
-- provenance and stay joinable. `retired` is the lifecycle state the runtime
-- already honours: boot wiring takes `state == 'active'` only, and the
-- analyst-side source reads all carry `COALESCE(state,'active') <> 'retired'`,
-- so the descriptor stops being polled and stops appearing as a live source
-- while every row it ever produced stays exactly where it is.
--
-- Idempotent: the second run matches 0 rows (state is already 'retired').
-- On a fresh substrate with no such descriptor it is a clean no-op.

UPDATE source_descriptors
   SET state = 'retired'
 WHERE descriptor_id = 'source.gdelt.doc_api'
   AND is_head
   AND state <> 'retired';
