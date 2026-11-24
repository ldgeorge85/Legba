# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The four critique folds ``GET /findings`` runs, declared once (H17).

Extracted from ``substrate_reads_api`` 2026-09-24 under the module-size gate.
Pure declarations — SQL fragments and one bound — so the route module keeps the
routes and this one keeps the shape they read the critic through.
``substrate_reads_api`` imports these names ONE WAY; nothing else does.

Each of the route's three projections (``fields=summary``, ``fields=judgment``,
and the full row) used to carry its own PAIR of correlated laterals: the latest
``Faithfulness verify%`` critique and the latest ``Structural verify%`` one, per
finding, ``ORDER BY produced_at DESC LIMIT 1``. Six laterals, one shape — and
that shape is the one the planner may serve from ``(kind, produced_at DESC)``,
walking every critique row for a finding that has none (see
:mod:`legba.data.critic_fold` for the measurement). Each fold is now ONE
``DISTINCT ON`` pass over the critiques naming the page, through
``idx_analyst_outputs_critique_analyzed_output_id``.

The STRUCTURAL folds drop ``overall_score IS NOT NULL`` (as their laterals did):
they project the verification block, not a score, and a structural critique need
not carry one. The two critique populations are DISJOINT — a structural finding
never has a faithfulness critique — which is why both are read and coalesced
rather than one masking the other.
"""

from __future__ import annotations

import json
from typing import Any

from .. import critic_fold

__all__ = [
    "FACET_SCAN_CAP",
    "PAGE_IDS",
    "FAITHFULNESS_VERIFICATION_CTE",
    "STRUCTURAL_VERIFICATION_CTE",
    "CRITIC_SCORE_CTE",
    "STRUCTURAL_BADGE_CTE",
    "FULL_ROW_JSON_TEXT_COLUMNS",
    "full_row_sql",
    "parse_full_row",
]

#: How many findings ONE ``/findings`` call scans when the verification facet
#: (``verified`` / ``judge_status``) is on. That facet reads the folds, so the
#: page can only be cut AFTER the join, so the page CTE has to be bounded by
#: something other than the page size. Measured live 2026-09-24 over 47,690
#: findings / 49,429 critiques: unbounded 4,798 ms, at this cap 186 ms (the
#: lateral this replaced: 905 ms). A capped scan never hides rows — a short page
#: returns the cap-th row as its ``next_cursor`` and the client walks on.
FACET_SCAN_CAP = 2_000

#: The page CTE every fold below folds over.
PAGE_IDS = "SELECT id::text FROM f"

#: ``fields=summary`` / ``fields=judgment`` — the verification BLOCK only.
FAITHFULNESS_VERIFICATION_CTE = critic_fold.latest_critique_cte(
    "c", "(cr.data->'data'->'verification') AS verification", PAGE_IDS,
)
STRUCTURAL_VERIFICATION_CTE = critic_fold.latest_critique_cte(
    "s",
    "(sr.data->'data'->'verification') AS structural_verification",
    PAGE_IDS,
    alias="sr",
    title_like=critic_fold.STRUCTURAL_TITLE_LIKE,
    require_score=False,
)

#: The full row — the gate INPUT (``overall_score``) beside the block, and the
#: structural badge the UI flips on.
CRITIC_SCORE_CTE = critic_fold.latest_critique_cte(
    "c",
    "(cr.data->>'overall_score')::real AS critic_score,"
    " (cr.data->'data'->'verification') AS verification",
    PAGE_IDS,
)
STRUCTURAL_BADGE_CTE = critic_fold.latest_critique_cte(
    "s",
    "(sr.data->'data'->'verification'->>'structural_verified')"
    " AS structural_verified,"
    " (sr.data->>'overall_score')::real AS structural_score,"
    " (sr.data->'data'->'verification') AS structural_verification",
    PAGE_IDS,
    alias="sr",
    title_like=critic_fold.STRUCTURAL_TITLE_LIKE,
    require_score=False,
)


# ---------------------------------------------------------------------------
# The full-row read (moved here from the route under the module-size gate, P-5)
# ---------------------------------------------------------------------------
#
# The faithfulness fold (``c``) picks the LATEST faithfulness-verify critique
# per finding and surfaces BOTH the gate input (overall_score) AND the P0-T3
# detail block (cr.data->'data'->'verification', written by the verify pass via
# CritiquePayload.data). The block is NULL for a finding with no faithfulness
# critique, so the API surfaces no fabricated block and effective_confidence
# stays == confidence.
#
# S8-T2 — PIN to the faithfulness critique (``title LIKE 'Faithfulness
# verify%'``), not just any newest ``overall_score`` critique: a later GENERIC
# critique (e.g. a country_critic that also stamps ``overall_score``) would
# otherwise win the ``produced_at DESC`` race and un-demote a finding the
# verify pass floored. Mirrors the composition GATHER (meta_findings_
# synthesizer) and the gepa parent SQL.
# C2b (P4-6) — the SECOND fold (``s``) picks the latest STRUCTURAL verify
# critique (``title LIKE 'Structural verify%'``; a structural finding never
# has a faithfulness critique): the ``structural_verified`` marker (the
# ``structural-verified`` badge), the structural detail (shown when no
# faithfulness block exists) and the structural score (folded into
# effective_confidence ONLY when the gate flag is on — see _hydrate_finding).
#
# P-5 — the three jsonb columns come back as their TEXT (``::text`` is
# jsonb_out, the very text the pool's json codec would parse) and are parsed
# off the event loop with the hydration and the page bytes
# (read_latency.page_json_response_off_loop). A full-weight page is tens of MB
# (200 composition findings = 22.8 MB live); parsing, dumping and encoding it
# on the loop stalled EVERY concurrent request by ~1.3 s. The casts sit
# OUTSIDE the ordered (and, with a facet, limited) page ``p``, so only the rows
# returned are rendered: a cast below the facet's sort ran on every scanned row
# (1772 for a 100-row page: +0.35 s).

#: The full row's jsonb columns, selected as ``::text``.
FULL_ROW_JSON_TEXT_COLUMNS = frozenset({"data", "verification", "structural_verification"})


def full_row_sql(where_sql: str, page_limit: str, after_fold: str) -> str:
    """The full-row ``/findings`` statement for the route's own WHERE, page
    bound and post-fold filter/order/limit (see the notes above)."""
    return f"""
            WITH f AS MATERIALIZED (
                SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
                       f.data, f.target_id, f.target_version, f.analyst_id,
                       f.analyst_version, f.produced_at, f.derived_from,
                       f.schema_uri, f.run_id, f.created_at
                  FROM analyst_outputs f
                 WHERE {where_sql}
                 ORDER BY f.produced_at DESC, f.id DESC
                 {page_limit}
            ), {CRITIC_SCORE_CTE}, {STRUCTURAL_BADGE_CTE}
            SELECT p.id, p.kind, p.title, p.body, p.confidence, p.severity,
                   p.data::text AS data, p.target_id, p.target_version,
                   p.analyst_id, p.analyst_version, p.produced_at,
                   p.derived_from, p.schema_uri, p.run_id, p.created_at,
                   p.critic_score,
                   p.verification::text AS verification,
                   p.structural_verified, p.structural_score,
                   p.structural_verification::text AS structural_verification
              FROM (
                SELECT f.id, f.kind, f.title, f.body, f.confidence, f.severity,
                       f.data, f.target_id, f.target_version, f.analyst_id,
                       f.analyst_version, f.produced_at, f.derived_from,
                       f.schema_uri, f.run_id, f.created_at,
                       c.critic_score AS critic_score,
                       c.verification AS verification,
                       s.structural_verified AS structural_verified,
                       s.structural_score AS structural_score,
                       s.structural_verification AS structural_verification
                  FROM f
                  LEFT JOIN c ON c.fid = f.id::text
                  LEFT JOIN s ON s.fid = f.id::text
                 {after_fold}
              ) p
             ORDER BY p.produced_at DESC, p.id DESC
        """


def parse_full_row(row: Any) -> dict[str, Any]:
    """A full-row record as a dict, its jsonb-as-text columns parsed — the same
    ``json.loads`` of the same text the pool's json codec runs, so the values
    are the ones a jsonb-typed select would have handed back."""
    return {
        k: (json.loads(v) if k in FULL_ROW_JSON_TEXT_COLUMNS and isinstance(v, str) else v)
        for k, v in row.items()
    }
