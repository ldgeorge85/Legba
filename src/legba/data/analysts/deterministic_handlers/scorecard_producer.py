# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scorecard_producer`` sub-handler — P4-T2 banded-scorecard producer.

The deterministic META analyst that MATERIALIZES the honest top of the glass
tower. Each tick it runs the T1 :func:`scorecard_banding.gather_and_band` over
EVERY active G20 country and side-writes ONE persisted ``kind='scorecard'`` row
per country (``target_id=<country>``, ``data.bands`` = the per-dimension banded
verdict). Pure SQL over ``deps.pg_pool``, LLM-free, $0 — a single global sweep.

Shape mirrors :mod:`calibration_tracking` / :mod:`unit_correctness_scorer` (the
deterministic META precedents) for the read + the summary FindingPayload, and
:mod:`situation_clustering` / :mod:`hypothesis_lifecycle` for the SIDE-WRITE: a
deterministic ``handle()`` returns exactly ONE :class:`AnalystMethodResult`
finding, so the N per-country rows MUST be side-written directly via
:func:`legba.data.provenance.writes.write_analyst_output` (the returned summary
is a per-run RECEIPT, marked ``TRACE_ONLY`` in ``deterministic.py`` so it never
also lands a redundant FINDING row — the receipt survives in ``analyst_traces``).

HONESTY DECISION (the whole point of P4)
----------------------------------------
A country with NO qualifying verified claim STILL emits a row — an
all-insufficient scorecard (every dimension ``band='insufficient-evidence'``,
``basis=[]``, ``reason`` set) tagged ``scorecard_all_insufficient``. We NEVER
omit and NEVER fabricate a band, so the read route always returns exactly one
honest card per active G20 country. A band NEVER exists without a real basis id
(``basis`` is only ever a real ``analyst_outputs.id`` returned by the T1 gather),
and the row's ``derived_from`` NAMES exactly the union of the real basis ids so a
P1 lineage walk resolves them with ZERO dangling refs.

P4-T5 EVAL FOLD
---------------
The P2 per-unit eval (``faithfulness`` + ``correctness_vs_reference``) is pulled
ONCE per sweep from the latest ``unit_correctness_scorer`` finding (it is
target-agnostic — the scorer aggregates over all targets) and folded into every
dimension's ``eval`` block, HONEST-NULL when the scorer never ran / the unit is
absent / the value is JSON-null. An unmeasured eval does NOT by itself demote a
band (it reads "unmeasured"); a per-claim faithfulness below the floor is what
demotes, via the T1 banding's dedicated ``low-faithfulness`` guard. As a
belt-and-suspenders DISPLAY flag we ALSO set ``eval.faithfulness_flagged=True``
whenever the CROSS-eval aggregate faithfulness is below the faithfulness floor —
distinct from the per-claim demote (see the FIELD-SOURCE NOTE below).

FIELD-SOURCE NOTE (a known reconcile-later seam): the T1 banding reads per-claim
faithfulness from the critique ``data->>'overall_score'``
(``scorecard_banding._GATHER_SQL``) while ``unit_correctness_scorer`` reads
faithfulness from the critique ``confidence`` column. Both filter
``title LIKE 'Faithfulness verify%'``. We do NOT cross-compare the two numbers as
identical: the per-claim ``critic_score`` drives the band + the demote; the eval
fold's aggregate faithfulness only drives the DISPLAY flag.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional
from uuid import UUID, uuid4

from ...provenance import AnalystContext, ScorecardPayload, write_analyst_output
from ...provenance.kinds import OutputKind
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import scorecard_banding

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "scorecard_producer"

# Enumerate the active ASSESSED countries by COVERAGE TAG (g20 OR watch) — every
# country the units fan out to: the G20 tier PLUS the high-consequence `watch`
# tier (Israel / Iran / Ukraine / Taiwan / North Korea, extensible). Tag-based
# (NOT an id LIKE) so registering a target with the g20/watch tag auto-cards it
# with zero code change, matching the analysts' has_tag("g20") or has_tag("watch")
# subscription. Only `retired` is excluded (draft + active both get a card).
_G20_TARGETS_SQL = """
    SELECT descriptor_id
      FROM target_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'scope' -> 'tags') ?| array['g20', 'watch']
     ORDER BY descriptor_id
"""

# The latest per-unit eval — target-agnostic (the scorer aggregates over ALL
# targets), so pulled ONCE per sweep. Mirrors labels_api /eval/scores.
_UNIT_EVAL_SQL = """
    SELECT data, produced_at
      FROM analyst_outputs
     WHERE analyst_id = 'unit_correctness_scorer'
       AND kind = 'finding'
       AND superseded_by IS NULL
     ORDER BY produced_at DESC, id DESC
     LIMIT 1
"""


# ---------------------------------------------------------------------------
# T5 eval fold (pure — testable without a DB)
# ---------------------------------------------------------------------------


def _num_or_none(v: Any) -> float | None:
    """Coerce a JSON value to a float, or None for a JSON-null / non-number.

    HONEST NULL: an unmeasured eval reads None (NEVER 0.0) — a real 0.0 only
    arises from a genuinely-measured zero elsewhere, never from a missing value.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def parse_unit_eval(raw_data: Any) -> dict[str, dict[str, Any]]:
    """Build ``eval_by_unit`` from a ``unit_correctness_scorer`` finding's data.

    ``raw_data`` is the finding's ``analyst_outputs.data`` JSONB (the nested
    scorer result lives under ``data['data']['units']``). Returns a map
    ``unit -> {faithfulness, judge_pipeline_version, correctness_operator,
    n_operator_scored, operator_sufficient, correctness_vs_reference,
    n_labeled}`` with HONEST NULLs. A missing / malformed blob yields an empty
    map (every unit then reads unmeasured), never a stub.

    M-1: ``correctness_operator`` is the OPERATOR gold-set axis, carried as its
    OWN keys beside — never merged with — the deterministic source-overlap
    ``correctness_vs_reference``. M-2: ``judge_pipeline_version`` names WHICH
    judge produced the faithfulness figure, so a card cannot display a number
    pooled across a judge swap without saying so.
    """
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw_data, Mapping):
        return {}
    nested = raw_data.get("data")
    units_map = nested.get("units") if isinstance(nested, Mapping) else None
    if not isinstance(units_map, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for unit, record in units_map.items():
        if not isinstance(record, Mapping):
            continue
        n_labeled = record.get("n_labeled")
        n_op = record.get("n_operator_scored")
        population = record.get("faithfulness_population")
        population = population if isinstance(population, Mapping) else {}
        out[str(unit)] = {
            "faithfulness": _num_or_none(record.get("faithfulness")),
            "judge_pipeline_version": population.get("judge_pipeline_version"),
            # PRIMARY correctness axis (operator gold set) — its own keys.
            "correctness_operator": _num_or_none(
                record.get("correctness_operator")
            ),
            "n_operator_scored": int(n_op) if isinstance(n_op, int) else 0,
            "operator_sufficient": bool(record.get("operator_sufficient")),
            # SECONDARY (diagnostic) correctness axis — source-id overlap.
            "correctness_vs_reference": _num_or_none(
                record.get("correctness_vs_reference")
            ),
            "n_labeled": int(n_labeled) if isinstance(n_labeled, int) else 0,
        }
    return out


def fold_unit_eval(
    verdict: Mapping[str, Any],
    eval_by_unit: Mapping[str, dict[str, Any]],
    *,
    faith_floor: float,
) -> None:
    """Attach a per-dimension ``eval`` block to a T1 verdict IN PLACE (T5-A).

    For every dimension: attach ``{faithfulness, judge_pipeline_version,
    correctness_operator, n_operator_scored, operator_sufficient,
    correctness_vs_reference, n_labeled, faithfulness_flagged}`` from
    ``eval_by_unit`` (honest-null when the unit is absent / unmeasured).
    ``faithfulness_flagged`` is the belt-and-suspenders DISPLAY flag — True
    whenever the CROSS-eval aggregate faithfulness is present AND below
    ``faith_floor`` (distinct from the per-claim demote, which the T1 banding
    already applied). An unmeasured (None) faithfulness is NEVER flagged
    (absence of proof is not proof of unfaithfulness).

    M-1: the operator correctness axis rides here as its OWN axis. It does NOT
    demote a band and is never averaged with faithfulness or with the
    source-overlap axis — a scorecard reader needs to see that a dimension can
    be highly faithful and only partially right at the same time, which is
    exactly what the 2026-07-28 gold-set round measured. ``operator_sufficient``
    travels with it so a tiny-n figure can never be rendered as a measured rate.
    """
    dimensions = verdict.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return
    for unit, dim in dimensions.items():
        if not isinstance(dim, dict):
            continue
        rec = eval_by_unit.get(unit) or {}
        faith = rec.get("faithfulness")
        dim["eval"] = {
            "faithfulness": faith,
            # M-2 — WHICH judge produced that number. A faithfulness figure with
            # no population named is a figure that can silently pool a swap.
            "judge_pipeline_version": rec.get("judge_pipeline_version"),
            # PRIMARY correctness axis — judge-independent, never pooled.
            "correctness_operator": rec.get("correctness_operator"),
            "n_operator_scored": int(rec.get("n_operator_scored") or 0),
            "operator_sufficient": bool(rec.get("operator_sufficient")),
            # SECONDARY (diagnostic) correctness axis.
            "correctness_vs_reference": rec.get("correctness_vs_reference"),
            "n_labeled": int(rec.get("n_labeled") or 0),
            "faithfulness_flagged": (
                faith is not None and float(faith) < faith_floor
            ),
        }


# ---------------------------------------------------------------------------
# Row assembly (pure — testable without a DB)
# ---------------------------------------------------------------------------


def basis_uuids_for_verdict(verdict: Mapping[str, Any]) -> list[UUID]:
    """The row's ``derived_from`` = the UNION of every REAL basis id across the
    four dimensions + the composition basis id, coerced to UUID.

    A dimension whose ``basis == []`` (insufficient) contributes NOTHING, and an
    absent composition contributes nothing — so the lineage array NAMES exactly
    the verified claims the bands rest on (ZERO dangling). An unparseable basis id
    is skipped defensively (the T1 gather only ever returns real
    ``analyst_outputs.id`` rows, so this is belt-and-suspenders)."""
    seen: set[UUID] = set()
    ordered: list[UUID] = []

    def _add(raw: Any) -> None:
        try:
            u = UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            return
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    dimensions = verdict.get("dimensions") or {}
    if isinstance(dimensions, Mapping):
        for dim in dimensions.values():
            for bid in (dim.get("basis") or []) if isinstance(dim, Mapping) else []:
                _add(bid)
    comp = verdict.get("composition") or {}
    if isinstance(comp, Mapping) and comp.get("present"):
        for bid in comp.get("basis") or []:
            _add(bid)
    return ordered


def _summarize_verdict(verdict: Mapping[str, Any]) -> tuple[int, int]:
    """Return ``(banded, insufficient)`` dimension counts for a verdict."""
    banded = 0
    insufficient = 0
    for dim in (verdict.get("dimensions") or {}).values():
        if not isinstance(dim, Mapping):
            continue
        if dim.get("band") == scorecard_banding.INSUFFICIENT:
            insufficient += 1
        else:
            banded += 1
    return banded, insufficient


def build_scorecard_payload(
    target_id: str, verdict: Mapping[str, Any]
) -> ScorecardPayload:
    """Assemble the persisted SCORECARD row payload for one country."""
    banded, insufficient = _summarize_verdict(verdict)
    comp_present = bool((verdict.get("composition") or {}).get("present"))
    title = (
        f"Scorecard {target_id} — {banded} banded / {insufficient} insufficient "
        f"({'comp present' if comp_present else 'no comp'})"
    )
    dim_lines = []
    for unit, dim in (verdict.get("dimensions") or {}).items():
        if not isinstance(dim, Mapping):
            continue
        ev = dim.get("eval") or {}
        n_op = int(ev.get("n_operator_scored") or 0)
        # The operator axis prints WITH its n, always — an `op=1.00` with no n
        # beside it is exactly the misreading the tiny-n rule exists to stop.
        op = (
            f" op={ev.get('correctness_operator')}(n={n_op}"
            f"{'' if ev.get('operator_sufficient') else ',indicative'})"
            if n_op else " op=unmeasured"
        )
        dim_lines.append(
            f"{unit}: {dim.get('band')} | {dim.get('reason')} | "
            f"eff={dim.get('effective_confidence')} | "
            f"faith={ev.get('faithfulness')} corr={ev.get('correctness_vs_reference')}"
            f"{op}"
            f"{' ⚑' if ev.get('faithfulness_flagged') else ''}"
        )
    body = "\n".join(dim_lines)
    tags = ["deterministic", "scorecard", f"target:{target_id}"]
    if banded == 0:
        # HONESTY tag: 0 dimensions banded — an all-insufficient card, emitted
        # (never omitted, never fabricated).
        tags.append("scorecard_all_insufficient")
    return ScorecardPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            # THE product — the T1 band_target verdict VERBATIM (already carries
            # target_id / generated_at / floors / dimensions / composition, now
            # T5-extended with the per-dimension eval block).
            "bands": dict(verdict),
        },
    )


# ---------------------------------------------------------------------------
# Live-substrate pull (best-effort)
# ---------------------------------------------------------------------------


async def _pull_eval_by_unit(pool: Any) -> dict[str, dict[str, Any]]:
    """Pull the latest per-unit eval ONCE (target-agnostic). Empty on any
    failure / absence — every unit then reads unmeasured, never a stub."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_UNIT_EVAL_SQL)
    except Exception as exc:  # noqa: BLE001 — never break the sweep
        logger.warning("scorecard_producer.eval_pull_failed err=%s", exc)
        return {}
    if row is None:
        return {}
    return parse_unit_eval(row["data"])


async def _enumerate_targets(pool: Any) -> list[str]:
    """The active G20 country descriptor ids (empty on any failure)."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_G20_TARGETS_SQL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scorecard_producer.enumerate_failed err=%s", exc)
        return []
    return [str(r["descriptor_id"]) for r in rows]


async def _emit_scorecard(
    pool: Any,
    *,
    target_id: str,
    verdict: Mapping[str, Any],
    analyst_id: str,
    analyst_version: str | None,
    run_uuid: UUID,
) -> bool:
    """Side-write ONE country's scorecard row (superseding the prior head).

    Returns True on a successful write. Supersession keeps ``analyst_outputs``
    from filling (one live head scorecard per country) AND makes the read route's
    ``superseded_by IS NULL`` filter exact. Wrapped by the caller in try/except so
    one country's failure never aborts the sweep."""
    new_id = uuid4()
    basis = basis_uuids_for_verdict(verdict)
    payload = build_scorecard_payload(target_id, verdict)
    ctx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version or "0" * 16,
        run_id=run_uuid,
        target_id=target_id,
    )
    async with pool.acquire() as conn:
        # Stamp the prior head scorecard for this country as superseded by the
        # incoming id BEFORE the insert (retention; exact head-latest filter).
        await conn.execute(
            "UPDATE analyst_outputs SET superseded_by = $2 "
            "WHERE kind = 'scorecard' AND target_id = $1 "
            "AND superseded_by IS NULL",
            target_id,
            new_id,
        )
        row, dead = await write_analyst_output(
            conn,
            analyst_ctx=ctx,
            kind=OutputKind.SCORECARD,
            output_payload=payload,
            derived_from=basis,
            row_id=new_id,
        )
    if dead is not None:
        logger.warning(
            "scorecard_producer.write_rejected target=%s reason=%s",
            target_id, getattr(dead, "reason", "schema_fail"),
        )
        return False
    return row is not None


# ---------------------------------------------------------------------------
# Summary receipt assembly
# ---------------------------------------------------------------------------


def _build_summary(
    *,
    countries: int,
    written: int,
    banded_dims: int,
    insufficient_dims: int,
    all_insufficient_countries: int,
    warnings: list[str],
) -> FindingPayload:
    head = (
        f"Scorecard sweep: {written}/{countries} countries carded, "
        f"{banded_dims} banded dims, {insufficient_dims} insufficient"
    )
    body = (
        f"countries={countries}\n"
        f"written={written}\n"
        f"banded_dims={banded_dims}\n"
        f"insufficient_dims={insufficient_dims}\n"
        f"all_insufficient_countries={all_insufficient_countries}\n"
        f"warnings={warnings}\n"
    )
    return FindingPayload(
        title=head[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", "scorecard_producer"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "countries": countries,
            "written": written,
            "banded_dims": banded_dims,
            "insufficient_dims": insufficient_dims,
            "all_insufficient_countries": all_insufficient_countries,
            "warnings": warnings,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — the global G20 scorecard sweep.

    A META single global sweep: the cadence actor hands this handler the generic
    signals slice as ``inputs`` (ignored — everything is pulled per-country from
    the substrate). ``deps`` (or its pool) being None degrades to the HONEST empty
    result (a TRACE_ONLY summary "no pool", write nothing), NOT a stub."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = options.get("analyst_version")
    # META no-target run: the actor injects run_id, but default defensively to a
    # fresh uuid4 so an absent run_id never aborts a side-write (the row's own
    # lineage still NAMES the verified basis via derived_from).
    raw_run_id = options.get("run_id")
    try:
        run_uuid = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        run_uuid = uuid4()

    faith_floor = float(
        options.get("faith_floor", scorecard_banding.FAITH_FLOOR)
    )
    conf_floor = float(options.get("conf_floor", scorecard_banding.CONF_FLOOR))
    conf_confident = float(
        options.get("conf_confident", scorecard_banding.CONF_CONFIDENT)
    )
    lookback_hours = int(
        options.get("lookback_hours", scorecard_banding.DEFAULT_LOOKBACK_HOURS)
    )

    warnings: list[str] = []
    if pool is None:
        warnings.append("scorecard_producer.no_pool")
        finding = _build_summary(
            countries=0,
            written=0,
            banded_dims=0,
            insufficient_dims=0,
            all_insufficient_countries=0,
            warnings=warnings,
        )
        return AnalystMethodResult(
            finding=finding,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    # Pull the per-unit eval ONCE (target-agnostic) before the loop.
    eval_by_unit = await _pull_eval_by_unit(pool)
    targets = await _enumerate_targets(pool)

    written = 0
    banded_dims = 0
    insufficient_dims = 0
    all_insufficient_countries = 0

    for target_id in targets:
        try:
            verdict = await scorecard_banding.gather_and_band(
                pool,
                target_id,
                conf_floor=conf_floor,
                conf_confident=conf_confident,
                faith_floor=faith_floor,
                lookback_hours=lookback_hours,
            )
            # T5-A — fold the per-unit eval into every dimension (honest-null).
            fold_unit_eval(verdict, eval_by_unit, faith_floor=faith_floor)

            banded, insufficient = _summarize_verdict(verdict)
            banded_dims += banded
            insufficient_dims += insufficient
            if banded == 0:
                all_insufficient_countries += 1

            ok = await _emit_scorecard(
                pool,
                target_id=target_id,
                verdict=verdict,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                run_uuid=run_uuid,
            )
            if ok:
                written += 1
        except Exception as exc:  # noqa: BLE001 — one country never aborts the sweep
            logger.warning(
                "scorecard_producer.country_failed target=%s err=%s",
                target_id, exc,
            )
            warnings.append(f"scorecard_producer.country_failed target={target_id}")

    finding = _build_summary(
        countries=len(targets),
        written=written,
        banded_dims=banded_dims,
        insufficient_dims=insufficient_dims,
        all_insufficient_countries=all_insufficient_countries,
        warnings=warnings,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "parse_unit_eval",
    "fold_unit_eval",
    "basis_uuids_for_verdict",
    "build_scorecard_payload",
]
