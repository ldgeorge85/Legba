-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0194_host_license_first_pass.sql
--
-- First-pass licence classification of source hosts, written to the
-- `source_credibility.license_class` column that migration 0192 added (NULL
-- on every row until now).
--
-- SCOPE
-- -----
-- Ten hosts whose licence is settled (public-domain doctrine or a published
-- open licence) are stamped with a cleared class. No host is stamped
-- forbidden: no host that would need it is registered as a source; the
-- template for that case is reproduced below, commented out. Every other
-- host stays NULL, which behaves as teaser depth.
--
-- VOCABULARY — enforced in code, asserted by this migration's test
-- ------------------------------------------------------------------
-- `license_class` carries no DB CHECK constraint (migration 0192's own
-- discipline — "the vocabulary grows with the ledger's review passes, and a
-- CHECK would turn every addition into a migration"). The four classes this
-- migration writes are members of the closed `Literal` at
-- `legba.data.schemas.source.LicenseClass`, and — the gate that actually
-- matters for behaviour — every one of them is in
-- `legba.data.research_evidence.CLEARED_LICENSE_CLASSES` (which the research
-- write path's `depth_for_license()` reads to grant `full_text` archive
-- depth). None is a member of
-- `legba.data.analysts.deterministic_handlers.evidence_archiver.
-- FORBID_RETENTION_CLASSES` (reused verbatim by
-- `research_evidence.forbidden_license_classes()`) — this migration writes
-- ZERO forbidding verdicts.
-- `tests/data_pkg/test_migration_0194_host_license_first_pass.py` asserts
-- both memberships directly against the code, not against this comment.
--
-- IDEMPOTENT + NEVER CLOBBERS A LATER MANUAL CLASS
-- ---------------------------------------------------
-- Every UPDATE below is guarded `WHERE source_host = '<host>' AND
-- license_class IS NULL`: a re-run of this file (the runner re-globs every
-- migration on every apply) matches 0 rows the second time, and an operator
-- who has ALREADY hand-classified one of these 10 hosts to something else
-- since this file was written keeps their own verdict — this migration will
-- never overwrite it. `scored_by` is stamped `'migration.0194'` (the 0080
-- convention) so the ledger shows these 10 rows came from this
-- migration, not from a later manual review.
--
-- CONSUMPTION — what changes, and when
-- ---------------------------------------
-- Nothing changes until the NEXT research fetch that resolves one of these
-- 10 hosts through `research_evidence.resolve_host_verdict()` →
-- `depth_for_license()`: `public_domain` / `open_gov_attribution` / `cc_nc` /
-- `cc_by` are all in `CLEARED_LICENSE_CLASSES`, so a hit against
-- cdc.gov / eia.gov / federalreserve.gov / nasa.gov / state.gov / usgs.gov /
-- weather.gov / gov.uk / who.int / globalvoices.org moves from `teaser`
-- (snippet-only, no bytes, never a numbered citation) to `full_text`
-- (extracted main text + content-addressed bytes + a numbered, citable row).
-- This migration is pure data — no research code path is touched, and no
-- currently-archived row is altered.
--
-- REVERSIBLE:
--   UPDATE source_credibility SET license_class = NULL, scored_by = NULL
--    WHERE scored_by = 'migration.0194';

-- ---------------------------------------------------------------------------
-- Sec 2.1 — public_domain (US federal works, 17 U.S.C. §105) — 7 hosts.
-- ---------------------------------------------------------------------------
UPDATE source_credibility
   SET license_class = 'public_domain',
       scored_by = 'migration.0194',
       last_updated = now()
 WHERE source_host IN (
   'cdc.gov', 'eia.gov', 'federalreserve.gov', 'nasa.gov',
   'state.gov', 'usgs.gov', 'weather.gov'
 )
   AND license_class IS NULL;

-- ---------------------------------------------------------------------------
-- Sec 2.2 — open_gov_attribution (UK Open Government Licence v3.0) — 1 host.
-- ---------------------------------------------------------------------------
UPDATE source_credibility
   SET license_class = 'open_gov_attribution',
       scored_by = 'migration.0194',
       last_updated = now()
 WHERE source_host = 'gov.uk'
   AND license_class IS NULL;

-- ---------------------------------------------------------------------------
-- Sec 2.3 — cc_nc (CC BY-NC-SA 3.0 IGO, live-verified 2026-07-10) — 1 host.
-- ---------------------------------------------------------------------------
UPDATE source_credibility
   SET license_class = 'cc_nc',
       scored_by = 'migration.0194',
       last_updated = now()
 WHERE source_host = 'who.int'
   AND license_class IS NULL;

-- ---------------------------------------------------------------------------
-- Sec 2.4 — cc_by (CC BY 3.0, live-verified 2026-07-10) — 1 host.
-- ---------------------------------------------------------------------------
UPDATE source_credibility
   SET license_class = 'cc_by',
       scored_by = 'migration.0194',
       last_updated = now()
 WHERE source_host = 'globalvoices.org'
   AND license_class IS NULL;

-- ---------------------------------------------------------------------------
-- Sec 3 — FORBIDDEN (anti_ai_walled). Zero of the 119 live hosts qualify —
-- the do-not-register list (TollBit-walled Penske Media four, ESPN, Reddit,
-- People/Dotdash Meredith) was never onboarded as a source. Template only,
-- inert until a future registration or research hit surfaces one of them:
--
-- UPDATE source_credibility
--    SET license_class = 'anti_ai_walled', scored_by = 'migration.0194',
--        last_updated = now()
--  WHERE source_host = '<host>'
--    AND license_class IS NULL;
-- ---------------------------------------------------------------------------

-- §4 (24 genuinely-ambiguous hosts) and §5 (85-host TEASER bookkeeping
-- stamp) are DELIBERATELY absent from this migration — see header.
