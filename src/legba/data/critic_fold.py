# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LATEST-CRITIQUE-PER-FINDING fold, SET-BASED — one definition (H17).

Twenty-two reads in this tree ask the same question: *for each of these
findings, what did the critic last say about it?* Every one of them was written
as a correlated lateral::

    LEFT JOIN LATERAL (
        SELECT (cr.data->>'overall_score')::real AS faithfulness_score
          FROM analyst_outputs cr
         WHERE cr.kind = 'critique'
           AND cr.data->>'analyzed_output_id' = <outer>.id::text   -- correlated
           ...
         ORDER BY cr.produced_at DESC, cr.id DESC
         LIMIT 1
    ) v ON TRUE

That shape has one plan that is fine and one that is a disaster, and NOTHING in
the query picks between them — the planner does, per execution, from statistics
that drift:

* the good plan probes ``idx_analyst_outputs_critique_analyzed_output_id`` (the
  expression index on ``(data->>'analyzed_output_id') WHERE kind='critique'``)
  once per outer row;
* the bad plan serves the ``ORDER BY … LIMIT 1`` from
  ``idx_analyst_outputs_kind (kind, produced_at DESC)`` — walk critiques
  newest-first and stop at the first whose ``analyzed_output_id`` matches. For a
  finding that HAS a recent critique that stops early. For a finding that has
  none it walks EVERY critique row (~49,400 today, each a jsonb deref). The
  critic samples, so most findings have none.

Both plans were observed live on 2026-09-24 against the same substrate:
``list_findings`` (24 h / 200 rows) did not finish in 120 s under the bad plan
and ran in 226 ms once it was rewritten set-based (that rewrite is
:func:`legba.runtime.substrate_query_port.critic_folded_findings_sql`, the first
instance of this module's shape). ``alert_trigger_scan``'s contention scan was
worse still — 42.5 s — because its lateral nested a SECOND one inside it.

The set-based shape removes the choice. Pick the outer row set FIRST, then make
exactly ONE pass over the critiques that name a member of it, ``DISTINCT ON`` the
analyzed id so the newest per finding wins, then join. That pass can only be
served by the expression index, because the id list is the only thing it filters
on. Linear in |outer| + |critiques naming the outer|, with no per-row decision
left for the planner to get wrong.

``ORDER BY analyzed_output_id, produced_at DESC, id DESC`` under the
``DISTINCT ON`` is the lateral's ``ORDER BY produced_at DESC, id DESC LIMIT 1``
verbatim, so the SAME critique row wins the same tie and no caller's numbers
move.

Membership — ``= ANY(ARRAY(<ids>))`` vs ``IN (<ids>)``
------------------------------------------------------
Both are correct; they plan differently, and the difference was worth 5 s on the
contention scan. ``IN (subquery)`` is a semi-join, so the planner sizes it from
its ESTIMATE of the outer CTE. When the outer CTE carries a ``LIMIT`` that
estimate is exact and the two forms plan identically. When it does not — the
contention scan's candidate CTE estimated 135,224 rows against 492 actual — the
planner picks a hash semi-join and reads every critique row to build it (7.3 s).
``= ANY(ARRAY(<ids>))`` evaluates the ids to an array first, which leaves the
expression index as the only sensible access path (2.1 s). So the array form is
the default here, and ``ids_as_array=False`` is for the one caller whose plan was
measured and pinned under ``IN``.
"""

from __future__ import annotations

__all__ = [
    "FAITHFULNESS_TITLE_LIKE",
    "STRUCTURAL_TITLE_LIKE",
    "FOLD_KEY",
    "FAITHFULNESS_SCORE_COLUMN",
    "latest_critique_cte",
    "faithfulness_score_cte",
    "dual_verdict_findings_sql",
]

#: The faithfulness-verify critique, pinned by title. Every fold that feeds an
#: ``effective_confidence`` uses it: without the pin a later GENERIC critique
#: that also stamps ``overall_score`` wins the ``produced_at`` race and masks the
#: verify verdict (S8-T2).
FAITHFULNESS_TITLE_LIKE = "Faithfulness verify%"

#: The structural-verify critique — a DISJOINT population (a structural finding
#: never carries a faithfulness critique), read by ``/findings`` beside the one
#: above.
STRUCTURAL_TITLE_LIKE = "Structural verify%"

#: The column every fold CTE keys on, so the join back is always
#: ``ON <cte>.fid = <outer>.id::text``.
FOLD_KEY = "fid"

#: The projection thirteen of these reads want and nothing more: the verify
#: pass's ``overall_score`` under the name every row loop and every
#: ``LEAST(confidence, …)`` fold in the tree already spells.
FAITHFULNESS_SCORE_COLUMN = "(cr.data->>'overall_score')::real AS faithfulness_score"


def latest_critique_cte(
    name: str,
    columns: str,
    ids_sql: str,
    *,
    alias: str = "cr",
    title_like: str | None = FAITHFULNESS_TITLE_LIKE,
    require_score: bool = True,
    extra_where: str = "",
    ids_as_array: bool = True,
) -> str:
    """One ``<name> AS MATERIALIZED (…)`` CTE: the latest critique per finding.

    :param name: the CTE's name, e.g. ``"v"``.
    :param columns: the projected critique expressions, written against
        ``alias`` and already carrying their ``AS`` names — e.g.
        ``"(cr.data->>'overall_score')::real AS faithfulness_score"``. The
        keying column (:data:`FOLD_KEY`) is projected for you.
    :param ids_sql: a SELECT yielding the outer set's ids AS TEXT, e.g.
        ``"SELECT id::text FROM f"``. It must be bounded by the caller — a page
        LIMIT, a target/window predicate, or an explicit id array.
    :param alias: the critique table's alias inside the CTE.
    :param title_like: the ``title LIKE`` pin, or ``None`` for "any critique"
        (what the consult port's fold does — see its own docstring).
    :param require_score: keep the ``overall_score IS NOT NULL`` predicate. The
        structural read drops it: it projects the verification block, not a
        score, and a structural critique need not carry one.
    :param extra_where: one more predicate, no leading ``AND`` — e.g.
        ``"cr.produced_at <= $4"`` to date the fold to a decision instant.
    :param ids_as_array: ``= ANY(ARRAY(<ids_sql>))`` (default) vs
        ``IN (<ids_sql>)``. See the module docstring: the array form is the one
        that survives a bad row estimate on the outer CTE.
    """
    key = f"{alias}.data->>'analyzed_output_id'"
    preds = [f"{alias}.kind = 'critique'"]
    if require_score:
        preds.append(f"{alias}.data->>'overall_score' IS NOT NULL")
    if title_like:
        preds.append(f"{alias}.title LIKE '{title_like}'")
    preds.append(
        f"{key} = ANY(ARRAY({ids_sql}))" if ids_as_array else f"{key} IN ({ids_sql})"
    )
    if extra_where:
        preds.append(extra_where)
    where = "\n               AND ".join(preds)
    return (
        f"{name} AS MATERIALIZED (\n"
        f"        SELECT DISTINCT ON ({key})\n"
        f"               {key} AS {FOLD_KEY},\n"
        f"               {columns}\n"
        f"          FROM analyst_outputs {alias}\n"
        f"         WHERE {where}\n"
        f"         ORDER BY {key}, {alias}.produced_at DESC, {alias}.id DESC\n"
        f"    )"
    )


def faithfulness_score_cte(
    name: str = "v", ids_sql: str = "SELECT id::text FROM f", **kwargs: object,
) -> str:
    """:func:`latest_critique_cte` with the one projection most callers want.

    The default names match the shape those callers already share — the fold
    CTE is ``v``, the bounded outer CTE is ``f`` — so a read whose outer set is
    a plain findings CTE spells its whole verify leg as
    ``critic_fold.faithfulness_score_cte()``.
    """
    return latest_critique_cte(
        name, FAITHFULNESS_SCORE_COLUMN, ids_sql, **kwargs,  # type: ignore[arg-type]
    )


def dual_verdict_findings_sql(
    where: str, limit_param: int, *, as_of_param: int = 1,
) -> str:
    """The DECISION-TIME belief read: each finding beside BOTH of its verdicts.

    ``/v3/belief`` (``legba.data.registry.belief_api``) and the consult port's
    ``belief_as_of`` ask the identical question and were carrying identical
    copies of it — two findings-page laterals each, four in total. One
    definition now, set-based:

    * ``va`` — the verdict Legba HELD on the as-of instant (the latest
      faithfulness critique produced by then). A finding whose verdict had not
      landed yet comes back NULL, which is what the callers count as
      ``verdict_pending_at_as_of``;
    * ``vl`` — today's verdict, for "what we believe NOW about what we said
      THEN".

    ``where`` is the caller's decision predicate over ``f`` and ``limit_param``
    its page-size parameter, both positional so the two callers keep their own
    parameter lists. ``as_of_param`` is the positional parameter holding the
    instant; both callers bind it first, so it defaults to ``$1``.

    The projected column names are byte-identical to the lateral form the two
    routes shipped before, so neither row loop changes.
    """
    ids = "SELECT id::text FROM f"
    columns = (
        "(cr.data->>'overall_score')::real AS verdict_score,\n"
        "               cr.produced_at AS verdict_at"
    )
    held = latest_critique_cte(
        "va", columns, ids, extra_where=f"cr.produced_at <= ${as_of_param}",
    )
    latest = latest_critique_cte("vl", columns, ids)
    return f"""
        WITH f AS MATERIALIZED (
            SELECT f.id, f.title, f.body, f.confidence, f.severity,
                   f.target_id, f.analyst_id, f.produced_at
              FROM analyst_outputs f
             WHERE {where}
             ORDER BY f.produced_at DESC, f.id DESC
             LIMIT ${limit_param}
        ), {held}, {latest}
        SELECT f.id, f.title, f.body, f.confidence, f.severity,
               f.target_id, f.analyst_id, f.produced_at,
               va.verdict_score AS verdict_score_as_of,
               va.verdict_at   AS verdict_at_as_of,
               vl.verdict_score AS verdict_score_latest,
               vl.verdict_at   AS verdict_at_latest
          FROM f
          LEFT JOIN va ON va.{FOLD_KEY} = f.id::text
          LEFT JOIN vl ON vl.{FOLD_KEY} = f.id::text
         ORDER BY f.produced_at DESC, f.id DESC
    """
