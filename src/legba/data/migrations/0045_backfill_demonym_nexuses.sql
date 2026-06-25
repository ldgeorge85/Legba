-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0045_backfill_demonym_nexuses.sql
--
-- DQ-H4 — close demonym / junk nexus endpoints polluting the graph.
--
-- WHY:
--   The DQ sweep (2026-06-21) found ~13% of agent nexuses touch a national
--   demonym ("Iranian", "Israeli", "Russian", …) or a junk token ("TV") as a
--   FIRST-CLASS node — distinct from the country it denotes ("Iran co occurs
--   with Iranian", same referent). This double-counts nodes and inflates graph
--   centrality (Iran degree=59). The FORWARD fixes land this same change:
--     * _entity_canon.canonicalize_entity now collapses demonyms → country and
--       drops junk tokens, so new entities/edges never carry them;
--     * proposed_edge_governance._promote_candidates now rejects any edge whose
--       endpoint is a demonym/junk token, so they never graduate to a nexus.
--   This migration closes the EXISTING open demonym/junk-endpoint nexuses.
--
-- WHAT (idempotent — only touches still-OPEN rows; valid_until-only, no rename,
-- so nothing collides with any open-triple constraint). The endpoint list
-- mirrors _entity_canon._DEMONYM_MAP + _JUNK_ENTITIES; declared once via a CTE.

WITH dem(d) AS (
    SELECT unnest(ARRAY[
        'american','british','french','german','italian','spanish','russian',
        'ukrainian','chinese','japanese','indian','pakistani','iranian','iraqi',
        'israeli','palestinian','syrian','lebanese','yemeni','saudi','egyptian',
        'turkish','qatari','afghan','polish','canadian','mexican','brazilian',
        'argentine','argentinian','australian','indonesian','nigerian',
        'venezuelan','sudanese','tv','radio','online'
    ]::text[])
)
UPDATE nexuses
   SET valid_until = NOW()
 WHERE superseded_by IS NULL
   AND valid_until IS NULL
   AND (
        lower(btrim(subject)) IN (SELECT d FROM dem)
     OR lower(btrim(object))  IN (SELECT d FROM dem)
   );
