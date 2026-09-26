# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-4 — THE REFERENCE GAP DISPATCH: an unread world story becomes work.

``planning/ATTENTION_MEASUREMENT_DESIGN_2026-09-05.md`` §3.3 (row D-m). A-1
writes an out-of-plane reference per (target × bounded unit × day) from a model
that has never seen our corpus; A-2 diffs it against the desk's own slice on
three collection arms (url / entity / prose). An item that clears NONE of them
is a **reference gap**: the world had a development on this desk's bounded
question and our collection does not carry it — the one direction in which our
own internal detectors structurally cannot help, because they can only measure
what we already collected.

This module turns that item into TWO durable objects, both through EXISTING
organs, and neither of them a new table:

  1. **A collection requirement** — a ``collection_requirements`` row
     (migration 0113) with a THIRD ``origin``, ``reference_gap``, beside the
     monthly ``collection_gap`` and the standing ``source_request``. Written
     through ``collection_gap.write_requirements`` — that module's OWN writer,
     with its bulk ``natural_key`` check, its ``_attach_candidates``
     source-descriptor cross-reference, its ``ON CONFLICT DO NOTHING``
     backstop and its degrade guard. Not a fork of it: the whole point of the
     third origin is that the research program's dispatcher reads ``origin``
     and gets both triggers through one door.
  2. **A standing question** — an ``open_question`` ``hypotheses`` row through
     ``_research_dispatch.dispatch_open_questions``, stamping the SHARED
     four-key marker vocabulary (``marker`` / ``origin='harvest'`` /
     ``harvest_class='reference_gap'`` / ``source_id``) that
     ``grounding.harvest_class_of`` reads and the corpus_researcher's backlog
     ranks on. ``reference_gap`` sits at ``_HARVEST_CLASS_PRIORITY`` ordinal 1
     — after ``coverage_floor``, ahead of every harvested class.

WHY BOTH, AND NOT ONE
---------------------
They answer different questions and are consumed by different organs. The
requirement asks *"what SOURCE would have carried this, and does one already
exist in `source_descriptors`?"* — an operator review surface, and the answer
is a feed registration. The question asks *"what does the world say about this,
and does it bear on the desk?"* — the corpus_researcher's backlog, and the
answer is retrieved evidence. A gap needs both; either alone leaves half of it
unaddressed. This is the same item producing two objects, not one object
written twice, and the two are keyed independently (see IDEMPOTENCY).

MATERIALITY — THE BAR, AND WHY IT IS THE HANDLER'S OWN
-------------------------------------------------------
``desk_reference.MATERIALITY_VOCABULARY`` is ``("high", "medium", "low")``,
code-validated at parse time and never free text, with ``medium`` as the
default for an item the model left unlabelled. :data:`MATERIAL_CLASSES` is
``{"high", "medium"}`` — the vocabulary MINUS its bottom rung, and that is the
whole derivation. No number is invented here: the design ruled that the paging
bar is "the item's ``materiality`` cleared the reference writer's own bar", and
the writer's own vocabulary is what a bar can be stated in. ``low`` is the
model's explicit judgment that the development is minor; dispatching research
on it would spend the backlog's ranking budget on the class of item the
reference model itself declined to rank.

Two further bounds, both borrowed rather than minted:
:data:`MAX_REQUIREMENTS_PER_DESK_DIMENSION` caps how many gaps one desk×unit
can raise in one sweep (items arrive in the reference model's OWN salience
order — ``ordinal`` — so this TRIMS the tail and never reorders, the
``build_gap_requirement_rows`` contract verbatim), and
:data:`MAX_DISPATCH_PER_RUN` bounds the whole sweep's write into a backlog that
already holds thousands.

IDEMPOTENCY — TWO KEYS, DELIBERATELY DIFFERENT
------------------------------------------------
* **The requirement** keys on ``natural_key =
  reference_gap:<target>:<unit>:<item_fold>``, UNIQUE at the schema layer. A
  still-uncollected item re-detected tomorrow re-derives the SAME key and
  writes nothing — the design's own requirement, and the reason the key
  carries no date. It includes the UNIT because the requirement's answer is a
  FEED, and the source classes that would carry an escalation story are not
  the ones that would carry an energy-security story
  (``SOURCE_CLASSES_BY_DIMENSION``): the same polity surfacing under two desks
  is two collection needs.
* **The question** keys on ``source_id = <target>|<item_fold>``, probed by
  jsonb containment against OPEN questions only (``_EXISTS_SQL``). It omits
  the unit because a standing question is about the WORLD ("does Lebanon bear
  on this target?") and asking it once per bounded unit would put seven
  near-identical rows into one backlog. It omits the date because the probe is
  already scoped to ``status='open_question'``: the row stays deduped while it
  is open and a genuinely re-opened need mints a new question once the old one
  is closed. That is the rising edge the design asks for, expressed through
  the state the backlog already maintains rather than through a clock.

DEGRADE-NOT-BREAK
-----------------
Nothing here may fail the ``desk_reference`` sweep. Each leg is isolated, each
failure is COUNTED in ``data.reference_gap_dispatch.failures`` and logged, and
:func:`run_reference_gap_dispatch` never raises. The gauge is the durable
product of that analyst and an ungraded instrument's side-write may not cost
the operator its measurement — the ``_research_dispatch`` contract, verbatim.

THE FLAG
--------
``LEGBA_REFERENCE_GAP_DISPATCH_ENABLED``, default OFF, read through
``desk_reference.reference_enabled``'s own env idiom. Off, ``run_reference_diff``
is called with no ``gap_sink``, so not one candidate is built, not one extra
query runs, and the finding payload is byte-identical to its pre-A-4 self but
for ``data.reference_gap_dispatch = {"enabled": false}``. The second leg is
ADDITIONALLY gated on ``research_regime.research_evidence_enabled()`` inside
``dispatch_open_questions``: dispatching into a backlog whose researcher has no
web leg would be a question nothing can answer.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

from . import collection_gap
from ._research_dispatch import (
    REFERENCE_GAP_HARVEST_CLASS,
    THESIS_CHAR_CAP,
    dispatch_open_questions,
)

logger = logging.getLogger(__name__)

#: Kill switch. Default OFF — off, neither leg builds a candidate.
ENABLED_ENV = "LEGBA_REFERENCE_GAP_DISPATCH_ENABLED"

#: The THIRD ``collection_requirements.origin`` (migration 0193 widened the
#: CHECK). The research program's dispatcher branches on this value.
ORIGIN: str = "reference_gap"

#: ``evidence_kind`` for a requirement citing a ``unit_reference_labels`` row —
#: neither an ``analyst_outputs`` row nor a ``hypotheses`` row, so migration
#: 0193 widened that CHECK too.
EVIDENCE_KIND: str = "unit_reference_label"

#: The harvest class leg 2 stamps, owned by ``_research_dispatch`` beside the
#: coverage-floor class so ONE module holds the vocabulary both producers use.
HARVEST_CLASS: str = REFERENCE_GAP_HARVEST_CLASS

#: ``question_source`` — the PROVENANCE key naming which producer minted the
#: row, distinct from ``harvest_class`` (a RANKING key ``grounding`` owns).
QUESTION_SOURCE: str = "reference_gap"

#: ``hypotheses.analyst_id`` on a dispatched question. NOT a registered
#: analyst: no descriptor, no cadence, no LLM — this is a side-write by the
#: ``desk_reference`` sweep, and inventing a descriptor for it would claim an
#: organ that does not exist (the ``research_dispatch`` precedent).
DISPATCH_ANALYST_ID: str = "reference_gap_dispatch"

#: The materiality classes that clear the bar — ``desk_reference``'s own
#: vocabulary minus its bottom rung. See the module note; no threshold is
#: invented here.
MATERIAL_CLASSES: frozenset[str] = frozenset({"high", "medium"})

#: Per desk×dimension cap on gaps raised in ONE sweep. Candidates arrive in the
#: reference model's own salience order (``ordinal``), so this TRIMS the tail
#: and never reorders — ``build_gap_requirement_rows``'s contract verbatim.
MAX_REQUIREMENTS_PER_DESK_DIMENSION: int = 3

#: Whole-sweep cap on NEW rows of EITHER kind. A sweep that somehow found a
#: hundred gaps must not put a hundred rows into a review surface, or into a
#: backlog that already holds thousands, in one tick; the rest re-detect
#: tomorrow, when their keys are unchanged and still unwritten.
MAX_DISPATCH_PER_RUN: int = 25

#: Cap on how many candidate URLs ride in one rationale.
_MAX_URLS_IN_RATIONALE: int = 2

#: ``collection_requirements.topic`` / ``rationale`` are plain ``text``; these
#: mirror ``build_source_request_row``'s own trims so a long model sentence is
#: fitted rather than rejected.
_TOPIC_CHAR_CAP: int = 2048
_RATIONALE_CHAR_CAP: int = 2048


def dispatch_enabled(options: Mapping[str, Any] | None = None) -> bool:
    """``LEGBA_REFERENCE_GAP_DISPATCH_ENABLED`` — default OFF.

    Off, A-4 writes NOTHING: no requirement, no question, no candidate built,
    no extra query run. Deliberately the SAME shape as
    ``desk_reference.reference_enabled`` — an ``options`` parameter accepted
    and an env var read — so an operator can disarm the dispatch with an env
    change and a recreate without unregistering or re-PUTting the descriptor
    that carries the instrument.
    """
    raw = os.getenv(ENABLED_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def is_material(candidate: Mapping[str, Any]) -> bool:
    """Did this gap clear the reference writer's own materiality bar?

    An item whose ``materiality`` is absent or unrecognised reads as
    ``desk_reference``'s parse-time default (``medium``) and CLEARS — the same
    direction ``_clean_materiality`` already resolves in, so the bar cannot
    silently tighten because a model omitted a field.
    """
    raw = str(candidate.get("materiality") or "").strip().lower()
    from .desk_reference import MATERIALITY_VOCABULARY, _DEFAULT_MATERIALITY

    if raw not in MATERIALITY_VOCABULARY:
        raw = _DEFAULT_MATERIALITY
    return raw in MATERIAL_CLASSES


def item_fold(candidate: Mapping[str, Any]) -> str:
    """The gap's key fold — the FIRST of the item's anchorable folds.

    ``item_folds`` preserves the reference model's own entity order, so the
    first fold is the polity the item leads with. Deterministic across days
    (the item text is what it is) and therefore a stable key half; a set-based
    key would reorder with the model's phrasing and mint a duplicate row for
    the same gap tomorrow.
    """
    for raw in candidate.get("entity_folds") or ():
        fold = str(raw or "").strip()
        if fold:
            return fold
    return ""


def entity_name(candidate: Mapping[str, Any]) -> str:
    """The CANONICAL spelling behind :func:`item_fold`, for prose.

    ``_reference_diff.item_names`` is documented to run in lockstep with
    ``item_folds``, so index 0 of one is the same entity as index 0 of the
    other. The fold stays the KEY everywhere — the natural_key, the containment
    probe, the marker — and this is only what the written sentence says.
    Falls back to the fold when a candidate carries no names (an older payload,
    or an item whose surfaces did not canonicalize).
    """
    for raw in candidate.get("entity_names") or ():
        name = str(raw or "").strip()
        if name:
            return name
    return item_fold(candidate)


def natural_key_for(*, target_id: str, unit: str, fold: str) -> str:
    """``reference_gap:<target>:<unit>:<item_fold>`` — design §3.3, verbatim."""
    return f"{ORIGIN}:{target_id}:{unit}:{fold}"


def dispatch_source_id(*, target_id: str, fold: str) -> str:
    """``<target>|<item_fold>`` — the containment-probe key for leg 2.

    The ``_research_dispatch.dispatch_source_id`` idiom (pipe-joined, folded)
    without its date component and without the unit. See the module note on
    IDEMPOTENCY for why each omission is deliberate rather than an oversight.
    """
    return f"{target_id}|{fold}"


def select_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    per_pair_cap: int = MAX_REQUIREMENTS_PER_DESK_DIMENSION,
    total_cap: int = MAX_DISPATCH_PER_RUN,
) -> list[dict[str, Any]]:
    """Material, fold-bearing gaps, deduped and bounded. Pure.

    Ordering is INHERITED, never imposed: candidates arrive per reference in
    the reference model's own ``ordinal`` order, and the two caps only trim.
    Within one (target, unit) a repeated fold is dropped — two items naming the
    same polity are one collection need and would collide on ``natural_key``
    anyway; dropping it here makes the count in the receipt honest instead of
    reporting a write that the unique index silently refused.

    A candidate with no ``reference_id`` is dropped rather than written: it is
    the requirement's ``evidence_id``, the column is ``uuid NOT NULL``, and a
    requirement is never written without a citable evidence row — the
    ``build_gap_requirement_rows`` rule, applied to this origin.
    """
    per_pair: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(out) >= total_cap:
            break
        if not isinstance(candidate, Mapping) or not is_material(candidate):
            continue
        fold = item_fold(candidate)
        target_id = str(candidate.get("target_id") or "").strip()
        unit = str(candidate.get("unit") or "").strip()
        if not fold or not target_id or not unit:
            continue
        if not str(candidate.get("reference_id") or "").strip():
            continue
        key = natural_key_for(target_id=target_id, unit=unit, fold=fold)
        if key in seen:
            continue
        pair = (target_id, unit)
        if per_pair.get(pair, 0) >= per_pair_cap:
            continue
        per_pair[pair] = per_pair.get(pair, 0) + 1
        seen.add(key)
        out.append({**candidate, "fold": fold, "natural_key": key})
    return out


def build_requirement_row(
    candidate: Mapping[str, Any], *, priority_rank: int
) -> dict[str, Any]:
    """One selected gap -> a pre-shaped ``collection_requirements`` row.

    Pure — the caller hands it to ``collection_gap.write_requirements``, which
    fills ``candidate_sources`` / ``suggested_fetch_url`` / ``fillable`` /
    ``unfillable_reason`` through ``_attach_candidates`` exactly as it does for
    the other two origins. ``source_classes_wanted`` comes from
    ``SOURCE_CLASSES_BY_DIMENSION`` via that module's own ``_source_classes``,
    so a unit outside the doctrine map (``proliferation_watch``,
    ``disruption_status``) falls back to the documented default rather than
    dropping the row.

    ``suggested_fetch_url`` is PRE-SET to the item's own URL — the design's
    ruling, and the one place this origin differs from the other two. The
    reference model handed us the exact page the development is on; that is
    strictly better to sample with the guarded ``web_fetch`` than a
    non-active feed's registered root, and ``_attach_candidates`` only fills
    the field when it is empty.
    """
    unit = str(candidate.get("unit") or "")
    target_id = str(candidate.get("target_id") or "")
    headline = str(candidate.get("headline") or "").strip()
    sentence = str(candidate.get("sentence") or "").strip()
    fold = str(candidate.get("fold") or item_fold(candidate))
    urls = [str(u) for u in (candidate.get("urls") or []) if str(u).strip()]
    n_slice = int(candidate.get("n_slice_rows") or 0)
    arms = ", ".join(str(a) for a in (candidate.get("arms_failed") or ()))
    rationale = (
        f"{sentence or headline} — present in the world reference, absent from "
        f"the desk's own {n_slice}-row slice"
    )
    if arms:
        rationale += f"; no collection arm matched ({arms})"
    if urls:
        rationale += f"; reference url(s): {' '.join(urls[:_MAX_URLS_IN_RATIONALE])}"
    return {
        "natural_key": candidate.get("natural_key")
        or natural_key_for(target_id=target_id, unit=unit, fold=fold),
        "origin": ORIGIN,
        "desk": target_id,
        "dimension": unit,
        "topic": (
            f"{unit} coverage for {target_id}: "
            f"{headline or entity_name(candidate)}"
        )[:_TOPIC_CHAR_CAP],
        "rationale": rationale[:_RATIONALE_CHAR_CAP],
        "evidence_kind": EVIDENCE_KIND,
        "evidence_id": str(candidate.get("reference_id") or ""),
        "source_classes_wanted": collection_gap.source_classes_for(unit),
        "suggested_fetch_url": urls[0] if urls else None,
        "priority_rank": priority_rank,
    }


def build_thesis(candidate: Mapping[str, Any]) -> str:
    """The standing question a reference gap becomes.

    Target-scoped and stated as a question about the WORLD, the
    ``_research_dispatch.build_thesis`` shape: what the reference says, what
    our slice does not carry, and the explicit note that our own corpus is the
    collection that missed it — so the researcher knows from the row itself
    that re-mining the substrate is the one instrument guaranteed not to close
    it. Every fact in the sentence is one the diff already measured; nothing is
    recomputed here and nothing is invented.
    """
    target_id = str(candidate.get("target_id") or "")
    unit = str(candidate.get("unit") or "")
    headline = str(candidate.get("headline") or "").strip()
    sentence = str(candidate.get("sentence") or "").strip()
    name = entity_name(candidate)
    n_slice = int(candidate.get("n_slice_rows") or 0)
    urls = [str(u) for u in (candidate.get("urls") or []) if str(u).strip()]
    named = headline or name
    slice_bit = (
        f"the desk's own {n_slice}-row slice for that day carries no row "
        f"matching it on url, entity or prose"
        if n_slice
        else "the desk's own slice for that day carries nothing at all"
    )
    head = (
        f"REFERENCE GAP — an out-of-plane reference for {target_id}'s "
        f"{unit} question named \"{named}\""
    )
    if sentence:
        head += f" ({sentence})"
    head += (
        f", and {slice_bit}. What is the world reporting about {name} that "
        f"bears on {target_id}, and does it bear on this desk's "
        f"{unit} question? Our corpus is the collection that missed it, so "
        f"answering this is expected to require reaching OUTSIDE it."
    )
    if urls:
        head += f" The reference cited: {urls[0]}"
    return head[:THESIS_CHAR_CAP]


def build_dispatch_entry(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """One selected gap -> a ``dispatch_open_questions`` entry. Pure.

    ``geo`` is the target polity's ISO codes and is load-bearing: §1.3 makes it
    the ONLY reachability key, and the 2026-09-06 researcher ranking fix
    depends on it being present on the winning row — a dispatched question
    without it dispatches research that reaches no desk.

    ``exemplar_signal_ids`` is EMPTY and cannot be otherwise: the item is
    uncollected, so by construction no signal in our corpus matched it. There
    is no lineage to claim, and claiming one would be a false provenance walk.
    The citing reference row rides on the marker's ``extra`` instead.
    """
    target_id = str(candidate.get("target_id") or "")
    fold = str(candidate.get("fold") or item_fold(candidate))
    return {
        "target_id": target_id,
        "geo": [str(g) for g in (candidate.get("geo") or ()) if str(g).strip()],
        "entity_fold": fold,
        "gap_name": str(candidate.get("headline") or entity_name(candidate)),
        "source_id": dispatch_source_id(target_id=target_id, fold=fold),
        "thesis": build_thesis(candidate),
        "exemplar_signal_ids": [],
        "marker_extra": {
            "question_source": QUESTION_SOURCE,
            "unit": str(candidate.get("unit") or ""),
            "evidence_kind": EVIDENCE_KIND,
            "evidence_id": str(candidate.get("reference_id") or ""),
            "materiality": str(candidate.get("materiality") or ""),
            "natural_key": str(candidate.get("natural_key") or ""),
        },
    }


def empty_payload(*, enabled: bool = False) -> dict[str, Any]:
    """The receipt block when the flag is off. ``{"enabled": false}`` and
    nothing else — the design's byte-identity contract states the payload
    carries exactly this, so the shape is defined ONCE, here, rather than
    re-spelled at the call site."""
    return {"enabled": enabled}


async def run_reference_gap_dispatch(
    pool: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    run_id: Any = None,
) -> dict[str, Any]:
    """Both legs for one ``desk_reference`` sweep. NEVER raises.

    Returns the ``data.reference_gap_dispatch`` receipt block:
    ``{enabled, candidates, requirements_written, requirements_existing,
    questions_opened, questions_existing, failures}``. A failure in either leg
    is counted in ``failures`` and the OTHER leg still runs — the requirement
    is an operator review surface and the question is a researcher backlog row,
    and losing one is no reason to lose the other.
    """
    payload: dict[str, Any] = {
        "enabled": True,
        "candidates": len(candidates),
        # How many cleared the materiality bar and the two caps. Reported
        # beside `candidates` because "38 gaps, 3 dispatched" and "3 gaps, 3
        # dispatched" are different days and the written count alone cannot
        # tell them apart.
        "selected": 0,
        "requirements_written": 0,
        "requirements_existing": 0,
        "questions_opened": 0,
        "questions_existing": 0,
        "failures": [],
    }
    if not candidates:
        return payload

    try:
        selected = select_candidates(candidates)
    except Exception as exc:  # noqa: BLE001 — a bad candidate never sinks the sweep
        logger.warning("reference_gap_dispatch.select_failed err=%s", exc)
        payload["failures"].append(f"select: {exc}")
        return payload
    payload["selected"] = len(selected)
    if not selected:
        return payload

    # ---- leg 1: the collection requirement ---------------------------------
    try:
        rows = [
            build_requirement_row(candidate, priority_rank=rank)
            for rank, candidate in enumerate(selected)
        ]
        stats: dict[str, int] = {}
        payload["requirements_written"] = await collection_gap.write_requirements(
            pool, rows, stats=stats
        )
        payload["requirements_existing"] = int(stats.get("existing") or 0)
        if stats.get("failed"):
            payload["failures"].append(
                f"requirements: {stats['failed']} row(s) did not write"
            )
    except Exception as exc:  # noqa: BLE001 — degrade: leg 2 still runs
        logger.warning("reference_gap_dispatch.requirements_failed err=%s", exc)
        payload["failures"].append(f"requirements: {exc}")

    # ---- leg 2: the standing question ---------------------------------------
    try:
        entries = [build_dispatch_entry(candidate) for candidate in selected]
        stats = {}
        async with pool.acquire() as conn:
            payload["questions_opened"] = await dispatch_open_questions(
                conn,
                entries,
                alert_output_id=None,
                run_id=run_id,
                harvest_class=HARVEST_CLASS,
                question_source=QUESTION_SOURCE,
                analyst_id=DISPATCH_ANALYST_ID,
                max_dispatch=MAX_DISPATCH_PER_RUN,
                stats=stats,
            )
        payload["questions_existing"] = int(stats.get("existing") or 0)
        if stats.get("failed"):
            payload["failures"].append(
                f"questions: {stats['failed']} entr(y/ies) did not write"
            )
    except Exception as exc:  # noqa: BLE001 — degrade: the gauge is already safe
        logger.warning("reference_gap_dispatch.questions_failed err=%s", exc)
        payload["failures"].append(f"questions: {exc}")

    logger.info(
        "reference_gap_dispatch.ran candidates=%d selected=%d requirements=%d "
        "questions=%d failures=%d",
        payload["candidates"], len(selected), payload["requirements_written"],
        payload["questions_opened"], len(payload["failures"]),
    )
    return payload


__all__ = [
    "DISPATCH_ANALYST_ID",
    "ENABLED_ENV",
    "EVIDENCE_KIND",
    "HARVEST_CLASS",
    "MATERIAL_CLASSES",
    "MAX_DISPATCH_PER_RUN",
    "MAX_REQUIREMENTS_PER_DESK_DIMENSION",
    "ORIGIN",
    "QUESTION_SOURCE",
    "build_dispatch_entry",
    "build_requirement_row",
    "build_thesis",
    "dispatch_enabled",
    "dispatch_source_id",
    "empty_payload",
    "entity_name",
    "is_material",
    "item_fold",
    "natural_key_for",
    "run_reference_gap_dispatch",
    "select_candidates",
]
