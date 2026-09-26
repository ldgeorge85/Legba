-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0192_source_credibility_license_class.sql
--
-- R-A (the research program's write path, RESEARCH_PROGRAM_SPEC §1.5): the
-- HOST LICENCE LEDGER — one additive column, and no new table.
--
-- WHY IT IS NEEDED AT ALL
-- -----------------------
-- Stamping `retrieval_origin` on a fetched page makes archiving IMPOSSIBLE by
-- default, and that is a feature, not a bug: `evidence_archiver`'s R-3b gate
-- (`WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES = False`) fails CLOSED for a
-- web-origin row whose `license_class` is unset or `unknown`, because the
-- fail-OPEN default it inverts was calibrated against ~48 operator-reviewed
-- sources and the open web is neither bounded nor reviewed. Live, NOT ONE of
-- the 123 registered sources carries a `license_class`, `evidence_archive.
-- license_class` is NULL across all 70,836 rows, and `SearchResult.
-- license_class` is hard-`None` BY DESIGN ("populating this from a guess would
-- defeat the retention gate that reads it").
--
-- So a research signal's archive depth has to be resolved PER HOST, from an
-- operator-owned ledger, or it is resolved by a guess — which is precisely the
-- laundering the licensing ledger's firewall exists to stop.
--
-- WHY THIS TABLE
-- --------------
-- `source_credibility` already IS a per-host, operator-owned ledger: 119 rows
-- keyed on `source_host`, carrying `score` / `score_rationale` / `tier` /
-- `scored_by` / `last_updated`, seeded `system.seed` and already served by
-- `/api/v1/source_credibility`. A licence verdict is the same KIND of judgment
-- about the same KEY, made by the same operator. Adding a second host table
-- would give one host two homes and two review surfaces.
--
-- NO CHECK CONSTRAINT, mirroring migration 0112's discipline: the licence
-- vocabulary grows with the ledger's review passes, and a CHECK would turn
-- every addition into a migration. The vocabulary is enforced in code
-- (`research_evidence.CLEARED_LICENSE_CLASSES` for the affirmative set,
-- `evidence_archiver.FORBID_RETENTION_CLASSES` for the refusing set), and an
-- UNRECOGNISED value resolves to TEASER depth — the fail-safe direction.
--
-- NULL IS THE HONEST DEFAULT AND IT STAYS THAT WAY. This migration seeds ZERO
-- verdicts. Every existing row keeps `license_class IS NULL`, which resolves
-- to teaser depth: title/url/snippet landed as evidence, no bytes archived.
-- Classifying a host is a LEGAL READING, and R-A will not have a model make
-- one — it is a named OPERATOR item, and until it happens the full-text path
-- is empty by construction rather than by accident.

ALTER TABLE source_credibility
    ADD COLUMN IF NOT EXISTS license_class text;

COMMENT ON COLUMN source_credibility.license_class IS
    'Operator-reviewed publisher licence for this host (the LIC-1 vocabulary). '
    'NULL = never reviewed, which is the shipped state for every seeded row and '
    'which resolves to TEASER fetch depth for outbound research: the hit is '
    'landed as a signal with the search snippet, and NO bytes are archived. An '
    'affirmative class (public_domain / open_gov_attribution / cc_by / cc_by_sa '
    '/ cc_nc / open_data_sharealike / api_terms) clears full-text depth + a CAS '
    'archive; a refusing class (anti_ai_walled / tos_restrictive / '
    'personal_use_only) means the page is NEVER FETCHED and no row is written '
    'at all. Set by an operator, never by a model or by a search provider.';
