-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0194_host_license_first_pass.sql
--
-- The operator's first-pass ratification of
-- `planning/HOST_LICENSE_CLASSIFICATION_PROPOSAL_2026-09-05.md` §2 (CLEARED)
-- and §3 (FORBIDDEN) against the `source_credibility.license_class` column
-- migration 0192 added (still NULL on all 119 rows, live, as of 2026-09-06 —
-- `select coalesce(license_class,'NULL'), count(*) from source_credibility
-- group by 1` returns exactly one row, `NULL | 119`).
--
-- SCOPE — exactly §2 and §3, nothing else
-- ----------------------------------------
-- The operator took the proposal's own recommended posture, verbatim: apply
-- §2 (10 hosts, settled public-domain doctrine or a live-verified licence)
-- and §3 (0 hosts — every do-not-register host in the LIC-1 ledger was never
-- onboarded as a source, so there is nothing live to retroactively forbid;
-- §3's own template is reproduced below, commented out, for the day a
-- forbidden host IS registered). §4's 24 genuinely-ambiguous hosts (the
-- personal-use bloc, the Al Jazeera anti-automated-analysis pair, the hard-
-- paywall bloc, and 9 unverified-terms singles) are a deliberate policy call
-- this migration does NOT make — they stay exactly as they are, NULL, i.e.
-- TEASER depth. §5's 85-host TEASER stamp table is likewise NOT applied here
-- — it is optional ledger-honesty bookkeeping the proposal itself marks
-- "recommended, not mandatory," and behaviour is IDENTICAL whether those rows
-- carry an explicit `permissive_feed_unreviewed`/`unknown` stamp or stay
-- NULL. This migration's only job is to move the 10 §2 hosts off NULL.
--
-- VOCABULARY — enforced in code, asserted by this train's own test
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
-- ZERO forbidding verdicts, matching §3's live count of 0.
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
-- convention) so the ledger shows these 10 rows were operator-ratified via
-- this ratification pass, not a live ad-hoc ledger review.
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
