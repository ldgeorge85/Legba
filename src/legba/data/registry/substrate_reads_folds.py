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

from .. import critic_fold

__all__ = [
    "FACET_SCAN_CAP",
    "PAGE_IDS",
    "FAITHFULNESS_VERIFICATION_CTE",
    "STRUCTURAL_VERIFICATION_CTE",
    "CRITIC_SCORE_CTE",
    "STRUCTURAL_BADGE_CTE",
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
