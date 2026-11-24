# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VERIFIED-ONLY finding reads — the composition admission rule, as a reader filter.

The bake-off's arm C (planning/reviews/BAKEOFF_DESIGN.md §2, lane X3/G0) reads
the platform's findings THROUGH the checking pipeline and nothing else: only a
finding a composition would admit as basis evidence. The consult port's two
finding readers (``list_findings`` / ``belief_as_of``) LEFT JOIN the critique,
so a finding nobody verified comes back beside one that passed. This module is
the additive ``verified_only`` leg of those two readers, and nothing else.

THE RULE IS THE COMPOSITION'S, NOT A NEW ONE. ``meta_findings_synthesizer``'s
basis gather and prior-read fetch admit a sub-claim when:

  * the latest ``Faithfulness verify%`` critique for it exists and carries a
    score — an INNER join (a finding verify never ran on is not admitted);
  * ``LEAST(confidence, faithfulness) >= LEGBA_COMPOSITION_VERIFY_FLOOR``
    (default 0.50; the same env knob, clamped to ``[0, 1]``);
  * it is not a coerce-fallback row (tags ``unstructured`` / ``coerce_failed``).

The floor constants are MIRRORED here rather than imported: the meta
synthesizer is an analyst kind module and this leaf is read by the runtime port
and the registry-slim tests alike. Lockstep is asserted by
``tests/data_pkg/test_verified_findings.py``.

SET-BASED, like every other critique fold in the tree (``critic_fold``, H17):
the candidate findings are picked first, then ONE ``DISTINCT ON`` pass over the
critiques naming them, then the join. The difference from the plain reader is
WHERE the page limit sits: the plain reader pages findings first and folds the
page, which is right for a LEFT join and wrong for a filter — a page of 20 that
holds 3 verified rows would answer 3. Here the candidate set is bounded by the
caller's own predicates (open heads, a target, a window, a decision instant)
and the LIMIT applies AFTER the floor.

Leaf module: stdlib + ``critic_fold`` only, importable in the slim registry
image.
"""

from __future__ import annotations

import os

from . import critic_fold as _cf

__all__ = [
    "COERCE_EXCLUSION",
    "DEFAULT_VERIFY_FLOOR",
    "VERIFY_FLOOR_ENV",
    "verified_belief_sql",
    "verified_findings_sql",
    "verified_receipt_sql",
    "verify_floor",
]

#: MIRROR of ``meta_findings_synthesizer.VERIFY_FLOOR_ENV`` (lockstep-tested).
VERIFY_FLOOR_ENV = "LEGBA_COMPOSITION_VERIFY_FLOOR"

#: MIRROR of ``meta_findings_synthesizer.DEFAULT_VERIFY_FLOOR`` (lockstep-tested).
DEFAULT_VERIFY_FLOOR = 0.50

#: The coerce-fallback drop the composition gathers apply before the fold.
COERCE_EXCLUSION = (
    "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE"
)


def verify_floor() -> float:
    """The live admission floor — the composition's env knob, same clamp."""
    raw = os.getenv(VERIFY_FLOOR_ENV)
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_VERIFY_FLOOR


def verified_findings_sql(
    where: str, limit_param: int, floor_param: int, *, critic_as_of: str = "",
    offset_param: int | None = None,
) -> str:
    """``list_findings``' row shape, restricted to admitted findings.

    ``where`` is the caller's predicate over ``f`` (positional params); the
    column list is byte-identical to
    ``substrate_query_port.critic_folded_findings_sql`` so the caller's row loop
    is unchanged — ``critic_score`` here IS the faithfulness score the floor
    was applied to. ``critic_as_of`` (``"AND cr.produced_at <= $n"``) dates
    the verdict to a decision instant, as the plain reader does.
    ``offset_param`` pages the read (W11-F150); without it the SQL is
    unchanged.
    """
    fold = _cf.latest_critique_cte(
        "v",
        _cf.FAITHFULNESS_SCORE_COLUMN,
        "SELECT id::text FROM f",
        extra_where=critic_as_of.strip().removeprefix("AND").strip(),
    )
    return (
        "WITH f AS MATERIALIZED ( "
        "  SELECT f.id, f.title, f.body, f.confidence, f.severity, "
        "         f.target_id, f.analyst_id, f.produced_at "
        "  FROM analyst_outputs f "
        f" WHERE {where} AND {COERCE_EXCLUSION} "
        f"), {fold} "
        "SELECT f.id, f.title, f.body, f.confidence, f.severity, "
        "       f.target_id, f.analyst_id, f.produced_at, "
        "       v.faithfulness_score AS critic_score "
        "FROM f JOIN v ON v.fid = f.id::text "
        f"WHERE LEAST(f.confidence, v.faithfulness_score) >= ${floor_param} "
        "ORDER BY f.produced_at DESC, f.id DESC "
        f"LIMIT ${limit_param}"
        + (f" OFFSET ${offset_param}" if offset_param is not None else "")
    )


def verified_belief_sql(
    where: str, limit_param: int, floor_param: int, *,
    fold_verdicts: str, as_of_param: int = 1,
) -> str:
    """``belief_as_of``' dual-verdict shape, restricted to admitted findings.

    The verdict the floor is applied to is the one the caller FOLDS by:
    ``as_of`` → the verdict held on the instant (a finding still ungraded then
    was not admissible then, so it is excluded); ``latest`` → today's verdict.
    Column names are byte-identical to ``critic_fold.dual_verdict_findings_sql``.
    """
    if fold_verdicts not in ("as_of", "latest"):
        raise ValueError(f"fold_verdicts must be 'as_of' or 'latest', got {fold_verdicts!r}")
    ids = "SELECT id::text FROM f"
    columns = (
        "(cr.data->>'overall_score')::real AS verdict_score,\n"
        "               cr.produced_at AS verdict_at"
    )
    held = _cf.latest_critique_cte(
        "va", columns, ids, extra_where=f"cr.produced_at <= ${as_of_param}",
    )
    latest = _cf.latest_critique_cte("vl", columns, ids)
    gate = "va" if fold_verdicts == "as_of" else "vl"
    return f"""
        WITH f AS MATERIALIZED (
            SELECT f.id, f.title, f.body, f.confidence, f.severity,
                   f.target_id, f.analyst_id, f.produced_at
              FROM analyst_outputs f
             WHERE {where} AND {COERCE_EXCLUSION}
        ), {held}, {latest}
        SELECT f.id, f.title, f.body, f.confidence, f.severity,
               f.target_id, f.analyst_id, f.produced_at,
               va.verdict_score AS verdict_score_as_of,
               va.verdict_at   AS verdict_at_as_of,
               vl.verdict_score AS verdict_score_latest,
               vl.verdict_at   AS verdict_at_latest
          FROM f
          LEFT JOIN va ON va.{_cf.FOLD_KEY} = f.id::text
          LEFT JOIN vl ON vl.{_cf.FOLD_KEY} = f.id::text
         WHERE {gate}.verdict_score IS NOT NULL
           AND LEAST(f.confidence, {gate}.verdict_score) >= ${floor_param}
         ORDER BY f.produced_at DESC, f.id DESC
         LIMIT ${limit_param}
    """


def verified_receipt_sql() -> str:
    """``$1`` (a signal uuid) is a RECEIPT of at least one admitted finding.

    The receipt-scoped ``read_document`` (arm C): a reader fed only verified
    findings may open the sources those findings cite and nothing else, so it
    cannot search its way to new material. "Cites" is ``derived_from`` — the
    signal ids a finding was written over (GIN-indexed). ``$2`` is the floor.
    Any admitted finding counts, live or superseded: ``belief_as_of`` returns
    past heads, and their receipts are as legitimate as today's.
    """
    fold = _cf.faithfulness_score_cte()
    return (
        "WITH f AS MATERIALIZED ( "
        "  SELECT f.id, f.confidence FROM analyst_outputs f "
        "   WHERE f.kind = 'finding' "
        "     AND f.derived_from @> ARRAY[$1::uuid] "
        f"    AND {COERCE_EXCLUSION} "
        f"), {fold} "
        "SELECT EXISTS ( "
        "  SELECT 1 FROM f JOIN v ON v.fid = f.id::text "
        "   WHERE LEAST(f.confidence, v.faithfulness_score) >= $2 "
        ") AS admitted"
    )
