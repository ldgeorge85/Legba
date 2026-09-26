# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every "latest critique for this finding" read in the data package is
SET-BASED, and every one of them is the SAME definition (H17, 2026-09-24).

The sibling pin ``tests/runtime/test_critic_fold_set_based.py`` owns the
builder (:mod:`legba.data.critic_fold`) and the tree-wide ban on the correlated
probe. This file pins the SITES: each gather's rendered SQL carries the fold
CTE, joins it back on ``fid``, and carries no lateral of its own — so a rewrite
that quietly reintroduces the per-row probe in ONE handler is caught here even
though the other twenty still look right.

Why the shape matters and not just the numbers: the correlated form has a good
plan and a catastrophic one and nothing in the query chooses between them. On
2026-09-24 the same substrate served ``list_findings`` (no ``title`` pin) from
the bad plan — over 120 s — while ``alert_trigger_scan``'s contention scan,
which nested two laterals, measured 42.5 s and 25 million buffer hits. The
set-based rewrite of that scan returned the SAME 500 rows, byte for byte, in
2.1 s.
"""
from __future__ import annotations

import inspect

import pytest

from legba.data import critic_fold as cf
from legba.data.analysts import composition_window, situation_tracker, window_ledger
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic_handlers import (
    _contention_flip_scan,
    _event_candidates,
    _situation_escalation_scan,
    _watchlist_scan,
    alert_trigger_scan,
    evidence_archiver,
    scorecard_banding,
)
from legba.data.registry import (
    backlog_drains,
    export_api,
    goldset_api,
    since_api,
    substrate_reads_api,
    substrate_reads_folds,
)


#: (label, rendered SQL, the fold CTE's name, the alias it joins back on).
_SITES: list[tuple[str, str, str, str]] = [
    (
        "situation_tracker._NEW_EVIDENCE_SQL",
        situation_tracker._NEW_EVIDENCE_SQL, "v", "f.id::text",
    ),
    (
        "meta_findings_synthesizer._PRIOR_READ_SQL_TEMPLATE",
        synth._PRIOR_READ_SQL_TEMPLATE.format(target_clause="f.target_id = $4"),
        "v", "f.id::text",
    ),
    (
        "alert_trigger_scan._VERIFIED_FINDINGS_SQL",
        alert_trigger_scan._VERIFIED_FINDINGS_SQL, "v", "f.id::text",
    ),
    (
        "_contention_flip_scan._CONTENTIONS_SQL",
        _contention_flip_scan._CONTENTIONS_SQL, "cr", "cand.id::text",
    ),
    (
        "scorecard_banding._GATHER_SQL",
        scorecard_banding._GATHER_SQL, "v", "f.id::text",
    ),
    (
        "scorecard_banding._GATHER_SQL_AS_OF",
        scorecard_banding._GATHER_SQL_AS_OF, "v", "f.id::text",
    ),
    (
        "scorecard_banding._CONSUMED_SQL",
        scorecard_banding._CONSUMED_SQL, "v", "f.id::text",
    ),
    (
        "_situation_escalation_scan._ESCALATIONS_SQL",
        _situation_escalation_scan._ESCALATIONS_SQL, "v", "e.source_output_id::text",
    ),
    (
        "_watchlist_scan._VERIFIED_WINDOW_SQL",
        _watchlist_scan._VERIFIED_WINDOW_SQL, "v", "f.id::text",
    ),
    (
        "_event_candidates._FINDING_CANDIDATES_SQL",
        _event_candidates._FINDING_CANDIDATES_SQL, "v", "f.id::text",
    ),
    (
        "evidence_archiver._SELECT_CANDIDATES_SQL",
        evidence_archiver._SELECT_CANDIDATES_SQL, "v", "f.id::text",
    ),
    (
        "goldset_api._CANDIDATES_SQL",
        goldset_api._CANDIDATES_SQL, "v", "f.id::text",
    ),
    (
        "goldset_api._HYDRATE_SQL",
        goldset_api._HYDRATE_SQL, "v", "f.id::text",
    ),
    (
        "export_api._FINDING_SQL",
        export_api._FINDING_SQL, "c", "f.id::text",
    ),
    (
        "since_api._NEW_FINDINGS_SQL",
        since_api._NEW_FINDINGS_SQL, "v", "f.id::text",
    ),
]


@pytest.mark.parametrize(
    "label,sql,cte,join_on",
    _SITES,
    ids=[s[0] for s in _SITES],
)
def test_site_folds_the_critic_set_based(
    label: str, sql: str, cte: str, join_on: str,
) -> None:
    assert f"{cte} AS MATERIALIZED (" in sql, label
    assert "DISTINCT ON (" in sql, label
    assert f"{cte}.fid = {join_on}" in sql, label
    # No correlated per-row probe survives anywhere in the statement.
    assert "= ANY(ARRAY(" in sql or "IN (SELECT" in sql, label
    assert "\n           ORDER BY cr.produced_at DESC, cr.id DESC" not in sql, label


def test_window_ledger_gather_is_set_based():
    """The one gather whose SQL is assembled inside the function."""
    src = inspect.getsource(window_ledger.read_window_ledger)
    assert "critic_fold.latest_critique_cte(" in src
    assert "JOIN v ON v.fid = f.id::text" in src
    assert "LATERAL" not in src


def test_periphery_gather_is_set_based():
    src = inspect.getsource(composition_window.read_periphery_findings)
    assert "critic_fold.faithfulness_score_cte()" in src
    assert "LEFT JOIN v ON v.fid = f.id::text" in src
    assert "LATERAL" not in src


def test_composition_gather_is_set_based():
    """``read_other_analyst_findings`` — the composition's own basis gather.

    The floor and the head-fold DISTINCT ON stay OUTSIDE the bounded CTE
    deliberately: the head this gather wants is the newest row that PASSES,
    which is not in general the newest row, and folding first would change
    which row a composition rests on.
    """
    src = inspect.getsource(synth.read_other_analyst_findings)
    assert "critic_fold.latest_critique_cte(" in src
    assert "f JOIN v ON v.fid = f.id::text" in src
    assert "LATERAL" not in src
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in src


def test_substrate_reads_findings_route_folds_both_critiques_set_based():
    """``/findings`` reads TWO disjoint critique populations per projection."""
    faith = substrate_reads_folds.FAITHFULNESS_VERIFICATION_CTE
    struct = substrate_reads_folds.STRUCTURAL_VERIFICATION_CTE
    score = substrate_reads_folds.CRITIC_SCORE_CTE
    badge = substrate_reads_folds.STRUCTURAL_BADGE_CTE
    for sql in (faith, struct, score, badge):
        assert "AS MATERIALIZED (" in sql
        assert "DISTINCT ON (" in sql
        assert "= ANY(ARRAY(SELECT id::text FROM f))" in sql
        assert "LATERAL" not in sql
    assert "cr.title LIKE 'Faithfulness verify%'" in faith
    assert "cr.title LIKE 'Faithfulness verify%'" in score
    # The structural fold reads a DISJOINT population and carries no score
    # requirement — a structural critique need not stamp overall_score.
    assert "sr.title LIKE 'Structural verify%'" in struct
    assert "sr.title LIKE 'Structural verify%'" in badge
    assert "overall_score' IS NOT NULL" not in struct
    assert "overall_score' IS NOT NULL" not in badge

    src = inspect.getsource(substrate_reads_api.build_substrate_reads_router)
    assert "LEFT JOIN LATERAL" not in src
    assert src.count("_FAITHFULNESS_VERIFICATION_CTE") == 2
    assert src.count("_CRITIC_SCORE_CTE") == 1


def test_backlog_drain_reads_situation_members_once():
    drains = [
        d for d in backlog_drains.BACKLOG_DRAINS
        if "analyzed_output_id" in d.overdue_sql
    ]
    assert len(drains) == 1
    sql = drains[0].overdue_sql
    assert "c AS MATERIALIZED (" in sql
    assert "DISTINCT ON (" in sql
    assert "JOIN c ON c.fid = mem.id::text" in sql
    assert "LATERAL" not in sql


def test_every_site_pins_the_faithfulness_critique_by_title():
    """A generic critique that also stamps ``overall_score`` must never win the
    ``produced_at`` race and mask a verify demotion (S8-T2). The ONE read that
    deliberately folds ANY scored critique is the consult port's, which lives
    in the runtime package and is pinned there."""
    for label, sql, _cte, _join in _SITES:
        if "overall_score' IS NOT NULL" not in sql:
            continue
        assert cf.FAITHFULNESS_TITLE_LIKE in sql, label
