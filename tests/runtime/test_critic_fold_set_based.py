# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The critic-folded findings read is SET-BASED (2026-09-24).

The lateral ``ORDER BY produced_at DESC LIMIT 1`` per finding walked every
critique row for a finding with no critique; the first forced crossroads run
timed ``list_findings`` and ``get_assessments`` out at 60 s each and a live
EXPLAIN of the 24 h / 200-row read did not finish in 120 s. The set-based
shape (page of findings first, latest scored critique per id through the
expression index, then the join) ran the same read in 226 ms. These pins keep
the shape: the helper's SQL, and that BOTH port methods build through it
rather than carrying a lateral of their own again.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from legba.data import critic_fold as cf
from legba.data.registry import belief_api
from legba.runtime import substrate_query_port as sqp


def test_helper_sql_is_the_set_based_shape():
    sql = sqp.critic_folded_findings_sql("f.kind = 'finding' AND f.produced_at >= $1", 2)
    assert sql.startswith("WITH f AS MATERIALIZED (")
    assert "DISTINCT ON (cr.data->>'analyzed_output_id')" in sql
    assert "IN (SELECT id::text FROM f)" in sql
    assert "LIMIT $2" in sql
    assert "LEFT JOIN LATERAL" not in sql
    # The column list every caller's row loop reads, byte for byte.
    assert (
        "SELECT f.id, f.title, f.body, f.confidence, f.severity, "
        "       f.target_id, f.analyst_id, f.produced_at, "
        "       c.critic_score AS critic_score "
    ) in sql
    assert sql.rstrip().endswith("ORDER BY f.produced_at DESC, f.id DESC")


def test_helper_dates_the_fold_when_asked():
    plain = sqp.critic_folded_findings_sql("f.kind = 'finding'", 1)
    dated = sqp.critic_folded_findings_sql(
        "f.kind = 'finding'", 2, critic_as_of="AND cr.produced_at <= $1",
    )
    assert "cr.produced_at <= $1" not in plain
    assert "cr.produced_at <= $1" in dated
    # The dating lands inside the critique CTE, before its ORDER BY.
    assert dated.index("cr.produced_at <= $1") < dated.index(
        "ORDER BY cr.data->>'analyzed_output_id'"
    )


def test_both_port_methods_build_through_the_helper():
    for name in ("list_findings", "get_assessments"):
        src = inspect.getsource(getattr(sqp.PostgresQdrantSubstrateQueryPort, name))
        assert "critic_folded_findings_sql(" in src, name
        assert "LEFT JOIN LATERAL ( " not in src, name


# ---------------------------------------------------------------------------
# H17 — the shared builder, and the tree-wide ban on the correlated probe
# ---------------------------------------------------------------------------

#: The defect, spelled as a pattern: a critique subquery whose membership test
#: names ONE outer row's id column. Every set-based form spells membership as
#: ``= ANY(...)`` or ``IN (...)``, so none of them matches.
_CORRELATED_PROBE = re.compile(
    r"analyzed_output_id'\s*=\s*[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+::text"
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "legba"


def test_no_module_in_the_tree_carries_the_correlated_critique_probe():
    """The whole point of H17: there is no second copy to regress into.

    A new ``LEFT JOIN LATERAL (… analyzed_output_id = f.id::text … LIMIT 1)``
    anywhere under ``src/legba`` turns this red. The fix is
    :func:`legba.data.critic_fold.latest_critique_cte`, never an exemption.
    """
    offenders = [
        str(p.relative_to(_SRC_ROOT))
        for p in sorted(_SRC_ROOT.rglob("*.py"))
        if _CORRELATED_PROBE.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "correlated per-row critique probe(s) reintroduced: " + ", ".join(offenders)
    )


def test_helper_builds_the_set_based_cte():
    sql = cf.latest_critique_cte(
        "v", cf.FAITHFULNESS_SCORE_COLUMN, "SELECT id::text FROM f",
    )
    assert sql.startswith("v AS MATERIALIZED (")
    assert "DISTINCT ON (cr.data->>'analyzed_output_id')" in sql
    assert "cr.data->>'analyzed_output_id' AS fid" in sql
    assert "cr.kind = 'critique'" in sql
    assert "cr.data->>'overall_score' IS NOT NULL" in sql
    assert "cr.title LIKE 'Faithfulness verify%'" in sql
    assert "= ANY(ARRAY(SELECT id::text FROM f))" in sql
    # The lateral's tie-break, verbatim: same critique row wins.
    assert (
        "ORDER BY cr.data->>'analyzed_output_id', cr.produced_at DESC, cr.id DESC"
    ) in sql
    assert "LATERAL" not in sql
    assert "LIMIT 1" not in sql


def test_helper_membership_forms_and_knobs():
    array_form = cf.latest_critique_cte("v", "1 AS x", "SELECT id::text FROM f")
    in_form = cf.latest_critique_cte(
        "v", "1 AS x", "SELECT id::text FROM f", ids_as_array=False,
    )
    assert "= ANY(ARRAY(SELECT id::text FROM f))" in array_form
    assert "IN (SELECT id::text FROM f)" in in_form

    structural = cf.latest_critique_cte(
        "s", "1 AS x", "SELECT id::text FROM f",
        alias="sr", title_like=cf.STRUCTURAL_TITLE_LIKE, require_score=False,
    )
    assert "sr.title LIKE 'Structural verify%'" in structural
    assert "overall_score" not in structural
    assert "DISTINCT ON (sr.data->>'analyzed_output_id')" in structural

    untitled = cf.latest_critique_cte("v", "1 AS x", "SELECT id::text FROM f",
                                      title_like=None)
    assert "LIKE" not in untitled

    dated = cf.latest_critique_cte(
        "v", "1 AS x", "SELECT id::text FROM f",
        extra_where="cr.produced_at <= $4",
    )
    # The dating lands inside the fold, before its ORDER BY.
    assert dated.index("cr.produced_at <= $4") < dated.index("ORDER BY")


def test_faithfulness_helper_is_the_common_case_spelled_once():
    assert cf.faithfulness_score_cte() == cf.latest_critique_cte(
        "v", cf.FAITHFULNESS_SCORE_COLUMN, "SELECT id::text FROM f",
    )


def test_belief_read_is_one_definition_shared_by_the_port_and_the_route():
    sql = cf.dual_verdict_findings_sql("f.kind = 'finding'", 2)
    assert "WITH f AS MATERIALIZED (" in sql
    assert "), va AS MATERIALIZED (" in sql
    assert ", vl AS MATERIALIZED (" in sql
    assert "LEFT JOIN va ON va.fid = f.id::text" in sql
    assert "LEFT JOIN vl ON vl.fid = f.id::text" in sql
    # The as-of fold is dated, the latest fold is not.
    assert sql.count("cr.produced_at <= $1") == 1
    # Every column name the two row loops read, byte for byte.
    for col in (
        "verdict_score_as_of", "verdict_at_as_of",
        "verdict_score_latest", "verdict_at_latest",
    ):
        assert col in sql
    assert "LATERAL" not in sql

    port_src = inspect.getsource(sqp.PostgresQdrantSubstrateQueryPort.belief_as_of)
    route_src = inspect.getsource(belief_api.build_belief_router)
    for src in (port_src, route_src):
        assert "dual_verdict_findings_sql(" in src
        assert "LATERAL" not in src
