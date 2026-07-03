-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0062_strip_publisher_origin_geo.sql
--
-- DQ Phase 3 / C2 in-place remediation (paired with the code fix in
-- src/legba/data/sources/baseline.py::_enrich_structured + run_baseline).
--
-- PROBLEM (C2): the per-source baseline stamped the source's ``scope_geo`` (the
-- PUBLISHER'S origin, e.g. anadolu=TR, tass=RU, cna=SG) into the indexed
-- ``signals.geo`` column BEFORE the in-body geocoder ran. The enrichment promote
-- step only APPENDS the resolved in-body ISO to ``geo`` (it never demotes the
-- origin hint), so a state wire's WORLD story ended up double-tagged: a Cuba-war
-- Anadolu story landed geo={TR,US}, a Damascus bombing on CNA landed {SG,CY}.
-- Every country desk that subscribes on ``geo && {its ISO}`` then read that
-- wire's ENTIRE world output as "its" country (Turkey's desk pool = 900 Anadolu
-- world stories vs ~14 real Turkey stories).
--
-- CODE FIX (already landed, this commit): baseline keeps the publisher origin
-- OUT of ``geo`` — it parks it in ``payload.publisher_origin`` and applies it to
-- ``geo`` ONLY as a post-enrichment FALLBACK when nothing in-body resolved. So
-- from here forward a world story keeps only its in-body geo; a genuinely-
-- domestic story still gets tagged with the home country.
--
-- THIS MIGRATION cleans the ~2 weeks of already-written history: for the 14
-- hint-bearing sources, it REMOVES the publisher-origin ISO(s) from
-- ``signals.geo`` wherever the story resolved a DIFFERENT in-body country
-- (payload.geo.country_iso2 exists and is not the origin). A genuinely-domestic
-- story (in-body country == origin, or no in-body country resolved) is left
-- untouched. It is an UPDATE-only cleanup — NO rows are deleted, and the safety
-- guard below guarantees no signal is ever left with an EMPTY geo.
--
-- MEASURED BLAST RADIUS (read-only SELECT against the live `legba` DB, all
-- history, 2026-07-03; 93,607 total signals):
--   rows touched (geo column UPDATEd) ...... 6,821
--   rows left with empty geo ............... 0   (safety guard EXISTS-checks
--                                                 a survivor before stripping)
--   per source:  anadolu 2026 | cna 1800 | tass 1257 | yonhap 939 |
--                nws 386 | presstv 213 | taskandpurpose 65 | tehrantimes 64 |
--                globaltimes 34 | irna 22 | ukrinform 15
--   (federalreserve / eia / cdc = 0 — their stories are genuinely US-domestic,
--    so nothing is stripped.)
--   Example transforms: {TR,US}->{US}  {TR,FR}->{FR}  {SG,CY}->{CY}.
--
-- The "14 hint-bearing sources" set is derived structurally, NOT hard-coded:
-- any head source_descriptor whose ``body.scope.geo`` is non-empty. So the
-- migration self-adjusts if the roster changes and is not coupled to a name list.
--
-- IDEMPOTENT / transactional / data-only:
--   The runner wraps this file in its own transaction and records it in
--   ``legba_data_migrations`` (no inline BEGIN/COMMIT or ledger insert here —
--   same as 0044/0051/0052/0053). Re-running is a no-op: after the first pass
--   the origin ISO is already gone from ``geo``, so ``s.geo && hint.isos`` no
--   longer matches those rows. On a fresh cold-start substrate there are no such
--   signals -> a clean no-op. This bulk UPDATE routes through the migration
--   runner (per house rule) so it is lineage-aware and does not trip the
--   raw-mass-mutation safety classifier.

UPDATE signals s
SET geo = ARRAY(
    -- keep every ISO that is NOT one of the source's publisher-origin hints
    SELECT g FROM unnest(s.geo) AS g
    WHERE g <> ALL (hint.isos)
)
FROM (
    -- per-source publisher-origin ISO set = head descriptor's scope.geo
    SELECT sd.descriptor_id AS source_id,
           ARRAY(SELECT jsonb_array_elements_text(sd.body->'scope'->'geo')) AS isos
    FROM source_descriptors sd
    WHERE sd.is_head = TRUE
      AND jsonb_array_length(COALESCE(sd.body->'scope'->'geo', '[]'::jsonb)) > 0
) AS hint
WHERE s.source_id = hint.source_id
  -- an in-body country resolved ...
  AND s.payload->'geo'->>'country_iso2' IS NOT NULL
  -- ... and it DIFFERS from the publisher origin (keeps domestic stories) ...
  AND NOT (s.payload->'geo'->>'country_iso2' = ANY (hint.isos))
  -- ... and the origin hint is actually present in geo (else nothing to strip) ...
  AND s.geo && hint.isos
  -- SAFETY: only strip when at least one non-origin ISO survives (never blank a
  -- signal's geo). Measured 0 rows fail this, but the guard makes it invariant.
  AND EXISTS (SELECT 1 FROM unnest(s.geo) AS g WHERE g <> ALL (hint.isos));
