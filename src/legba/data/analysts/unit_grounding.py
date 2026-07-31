# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QW1-B — per-UNIT grounding blocks: the composition CONTINUITY idiom, one floor down.

WHAT THIS CLOSES. The P1 prompt gallery measured the bounded units reading one
byte-identical, undifferentiated slice-of-now: 8 of the 9 units re-triage the SAME
120-row pool every cadence tick with NOTHING in the prompt telling them what this
desk already knew, what frames are open on it, what "normal" looks like for it, or
what it asked last cycle and never got answered. A unit could not say "this
ESCALATES what we were watching" because it had no licensed way to know there WAS
anything before. Phase 1 gave the COMPOSITIONS a memory
(:mod:`legba.data.analysts.meta_findings_synthesizer` — grep ``CONTINUITY``); this
mirrors that idiom DOWNWARD to the units and widens it from two blocks to four.

THE SAME TWO HARD LESSONS BIND THIS DESIGN — verbatim from the composition note,
because they are properties of the platform, not of the layer:

  1. The world_context RAG ROLLBACK. An UNCITED prior leaking into cited analysis
     is this platform's NAMED failure mode. So grounding context enters ONLY as
     CITABLE blocks: each block gets its own ``[N]`` ordinal in the SAME flat
     resolution space as the numbered signals (and the GATHER-gathered corpus
     docs), carries its rendered text as ``evidence_text``, and the prompt clause
     requires the model to cite it exactly like any other evidence.
  2. TEMPORAL COLLAPSE. Every block carries its OWN dates INTO the rendered text
     (the prior read's ``produced_at`` + age, a situation's ``last_event_at`` +
     age, the baseline's ``computed_at`` + window, a question's ask date + age),
     and the clause anchors every temporal statement on those printed dates —
     never on run/fetch time.

FOUR BLOCKS, all bounded, all absent-by-default:

  * PRIOR READ (:data:`GROUNDING_PRIOR_READ`) — THIS unit's own previous
    non-superseded, VERIFIED head for THIS target. Reuses the composition's
    :func:`~legba.data.analysts.meta_findings_synthesizer.read_prior_composition_head`
    verbatim (the reader is analyst-agnostic: "the same analyst_id's last verified
    head for the same target_id"), so the verify GATE, the lookback bound and the
    coerce-fallback drop are ONE implementation, not two that can drift.
  * OPEN-SITUATION REGISTER (:data:`GROUNDING_SITUATIONS`) — the desk's currently
    OPEN ``situations`` frames, worst-first, one block. Reuses the composition's
    :func:`~legba.data.analysts.meta_findings_synthesizer.read_open_situations`
    with the per-desk ``target_id`` scope — the SAME scoping rule a per-country
    composition uses, for the same reason (a desk must not see another desk's
    frames).
  * DESK BASELINE (:data:`GROUNDING_BASELINE`) — the desk's ``desk_baselines``
    rows (mig 0103): what is NORMAL here, so "is this unusual" is a question the
    unit can answer against a number instead of a vibe. HONEST ABSENCE: rows with
    ``insufficient_history`` are NOT rendered — a band resting on thin history
    would read as authority it has not earned.
  * STANDING OPEN QUESTIONS (:data:`GROUNDING_QUESTIONS`) — the desk's open
    ``hypotheses`` rows (``status='open_question'``), newest-first. This closes a
    loop that was open end-to-end: every unit descriptor EMITS ``open_questions``
    (converted to first-class rows by ``inline_target.convert_open_questions``)
    and no unit ever read one back.

WHAT IS DELIBERATELY *NOT* HERE:

  * NO new analyst kind, NO new table, NO migration. Four SELECTs and a render.
  * NO fabricated anchor. The register / baseline / question blocks are not
    ``analyst_outputs`` rows and have NO single substrate id, so their citations
    carry the REAL underlying ids (``situation_ids`` / ``baseline_keys`` /
    ``question_ids``) and NO ``ref_id`` — minting one so a drill link resolves is
    exactly the dishonesty the citation contract refuses (mirrors
    ``SITUATION_REGISTER_REF_KIND``).
  * NO ``ref_kind='finding'`` on ANY unit block — that token is the composition
    discriminator (``verify._uses_subclaim_convention``), and stamping it on a
    unit citation would route the whole unit finding to the sub-claim verify
    floor. The prior read carries its real finding uuid as ``ref_id`` under its
    own ``ref_kind``.
  * NO confidence, NO lineage on a prior-read citation. The composition strips
    both (its rationale: a read must not bootstrap its confidence off its own
    last conclusion, and "what we said before" must never fold into a
    shared-lineage component with "what we see now"). A unit has no correlation
    guard at all, so this module never BUILDS them — and, one step further than
    the composition, keeps the confidence number out of the rendered block too: a
    unit's signal blocks print no confidence anywhere, so a number printed only
    on last cycle's own read is an anchor the unit cannot weigh.

BEST-EFFORT, DEGRADE-NEVER-BREAK: the gather is ADDITIVE enrichment on top of an
already-complete slice. Every block is fetched in its OWN ``try`` so one failure
never suppresses its siblings, and a total failure yields NO rows — a unit never
fails, and never loses its evidence slice, because its memory was unavailable.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)


__all__ = [
    "GROUNDING_BASELINE",
    "GROUNDING_BLOCK_KINDS",
    "GROUNDING_PRIOR_READ",
    "GROUNDING_QUESTIONS",
    "GROUNDING_RECEIPT_KEYS",
    "GROUNDING_SITUATIONS",
    "UNIT_GROUNDING_CLAUSE",
    "UNIT_GROUNDING_ROW_KEY",
    "citation_for_block",
    "gather_unit_grounding_rows",
    "grounding_receipts",
    "partition_grounding_rows",
    "read_desk_baselines",
    "read_desk_open_questions",
    "render_grounding_section",
    "with_grounding_clause",
]


# ---------------------------------------------------------------------------
# Row markers + the block vocabulary
# ---------------------------------------------------------------------------

UNIT_GROUNDING_ROW_KEY: str = "_unit_grounding"
"""Row marker the SLICE READER stamps on a grounding row so the DB-less
``run_method`` can partition it out of the evidence slice on DATA, never on env.
Value is one of :data:`GROUNDING_BLOCK_KINDS`. An unmarked slice — every legacy
caller, every non-unit kind — is byte-for-byte the pre-grounding path."""

GROUNDING_PRIOR_READ: str = "prior_read"
"""Marker value: the row IS this unit+target's previous verified head."""

GROUNDING_SITUATIONS: str = "situation_register"
"""Marker value: the synthetic OPEN-SITUATION REGISTER block."""

GROUNDING_BASELINE: str = "desk_baseline"
"""Marker value: the synthetic DESK BASELINE block."""

GROUNDING_QUESTIONS: str = "open_questions"
"""Marker value: the synthetic STANDING OPEN QUESTIONS block."""

GROUNDING_BLOCK_KINDS: tuple[str, ...] = (
    GROUNDING_PRIOR_READ,
    GROUNDING_SITUATIONS,
    GROUNDING_BASELINE,
    GROUNDING_QUESTIONS,
)
"""The block kinds IN RENDER ORDER — memory, then the open picture, then the
statistical prior, then the standing debt. The ordinal a block receives is its
position in THIS sequence among the blocks actually present, so the ordinal space
stays contiguous and gap-free whichever subset resolved."""

GROUNDING_PAYLOAD_KEY: str = "_grounding_payload"
"""Key on a SYNTHETIC grounding row carrying its rendered payload (the situation
/ baseline / question dicts). The PRIOR READ is a real ``analyst_outputs`` row
and carries no payload key — it renders off its own columns."""

GROUNDING_RECEIPT_KEYS: dict[str, str] = {
    GROUNDING_PRIOR_READ: "grounding_prior_ref",
    GROUNDING_SITUATIONS: "grounding_situations_ref",
    GROUNDING_BASELINE: "grounding_baseline_ref",
    GROUNDING_QUESTIONS: "grounding_questions_ref",
}
"""Receipt key per block kind. Reported on the run's ``orient`` step (and its own
``grounding_blocks`` step) so "did this unit get its memory this cycle" is
answerable from a trace without re-running the gather. 0/1 each — these are single
blocks by construction, and counting them is how a silently-absent memory becomes
visible instead of reading as a first run forever."""


# ---------------------------------------------------------------------------
# Bounds — every one of them is what keeps a block an ORIENTING INDEX rather
# than a second evidence slice.
# ---------------------------------------------------------------------------

PRIOR_LOOKBACK_HOURS: int = 168
"""How far back the prior-read lookup reaches (7 days) — INDEPENDENT of the
unit's slice window, because "the previous read" is a per-head fact, not a
per-slice one: a unit on a 72h window whose last two cycles were skipped still has
a prior read worth diffing against. Bounded so a months-old head is never dressed
up as "the prior read"; the block always prints its produced_at + age so the model
(and the verify pass) can see exactly how stale the memory is."""

PRIOR_BODY_CHARS: int = 900
"""Body excerpt cap for the prior-read block — the same cap the composition
uses (``CONTINUITY_PRIOR_BODY_CHARS``): the diff IS the point, and a truncated
prior read produces a fabricated-looking "change"."""

SITUATION_CAP: int = 6
"""Max open frames in the register. Tighter than the composition's 8: a unit
answers ONE bounded question, so its register is an orientation, not a survey."""

SITUATION_NAME_CHARS: int = 120
"""Per-frame name cap — a situation name is a short frame LABEL."""

BASELINE_METRIC_CAP: int = 4
"""Max (desk, metric) baseline rows rendered. Two metrics ship today
(``signal_volume_24h`` / ``high_sev_findings_24h``); the headroom is for the next
metric, not for a survey."""

QUESTION_CAP: int = 5
"""Max standing questions rendered, newest-first."""

QUESTION_TEXT_CHARS: int = 300
"""Per-question text cap. A question is one sentence; anything longer is a
mis-emitted finding body and must not be allowed to dominate the block."""

EVIDENCE_TEXT_CHARS: int = 2400
"""Cap on a block's captured ``evidence_text``. Sized to hold the WHOLE rendered
block at its own bounds rather than reusing the 600-char single-signal cap.
LOAD-BEARING: ``verify._marker_to_evidence`` applies no cap of its own to a
grounding entry, so the capture made HERE is what the judge grades against — a
600-char cut would silently hide a block's tail and false-demote a faithful claim
about a frame the model was actually shown. Mirrors
``SITUATION_REGISTER_EVIDENCE_CHARS`` on the composition side."""

MAX_TITLE_CHARS: int = 200
"""Prior-read title cap (matches the unit slice renderer's title cap)."""


# ---------------------------------------------------------------------------
# Readers — two reused from the composition floor, two new
# ---------------------------------------------------------------------------


# The desk-baseline read. ``insufficient_history`` is the honesty flag mig 0103
# ships: a band computed over thin history is a number without a claim behind it,
# so it is EXCLUDED here rather than rendered with a caveat the model may drop.
# No row => no block, which is the honest absence.
_BASELINE_SQL = """
    SELECT desk_id, metric, baseline_days, n_sigma, expected, center_median,
           band_low, band_high, current, deviation, deviation_sigma,
           sample_days, active_days, computed_at
      FROM desk_baselines
     WHERE desk_id = $1
       AND insufficient_history IS NOT TRUE
     ORDER BY metric
     LIMIT $2
"""


async def read_desk_baselines(
    conn,  # type: ignore[no-untyped-def]
    *,
    desk_id: str,
    limit: int = BASELINE_METRIC_CAP,
) -> list[dict[str, Any]]:
    """The desk's statistical baselines — ONLY those with sufficient history.

    Returns compact, JSON-safe dicts (``[]`` when the desk has no sufficient-history
    baseline, which is the honest absence: no block is rendered and no receipt is
    stamped). A row missing its metric name is SKIPPED rather than padded — the
    block may only state bands that actually exist.
    """
    if not desk_id:
        return []
    rows = await conn.fetch(_BASELINE_SQL, str(desk_id), int(limit))
    out: list[dict[str, Any]] = []
    for raw in rows:
        r = dict(raw)
        metric = r.get("metric")
        if not isinstance(metric, str) or not metric.strip():
            continue
        out.append(
            {
                "desk_id": str(r.get("desk_id") or desk_id),
                "metric": metric.strip(),
                "expected": _as_float(r.get("expected")),
                "center_median": _as_float(r.get("center_median")),
                "band_low": _as_float(r.get("band_low")),
                "band_high": _as_float(r.get("band_high")),
                "current": _as_float(r.get("current")),
                "deviation": str(r.get("deviation") or "unknown"),
                "deviation_sigma": _as_float(r.get("deviation_sigma")),
                "n_sigma": _as_float(r.get("n_sigma")),
                "baseline_days": _as_int(r.get("baseline_days")),
                "sample_days": _as_int(r.get("sample_days")),
                "active_days": _as_int(r.get("active_days")),
                "computed_at": _iso_text(r.get("computed_at")),
            }
        )
    return out


# The standing-question read. Scoped to the DESK (``target_id``) — the same
# per-desk scope the register takes, and the reason this is NOT the R-1
# ``open_questions`` grounding source: that one ranks the GLOBAL backlog by
# forward reach for a backlog-working analyst and renders into the (non-citable)
# preamble. This is "what has THIS desk been asking", as a CITABLE block.
#
# ``status='open_question'`` is the open marker (nothing flips it today — an
# unanswered question stays standing), so the block is deliberately unbounded in
# AGE and prints each question's own ask-date + age instead: staleness is a fact
# the model must weigh, not one this reader may hide by filtering.
_OPEN_QUESTIONS_SQL = """
    SELECT id, thesis, produced_at, analyst_id,
           EXTRACT(EPOCH FROM (NOW() - produced_at)) / 86400.0 AS age_days
      FROM hypotheses
     WHERE status = 'open_question'
       AND target_id = $1
     ORDER BY produced_at DESC, id
     LIMIT $2
"""


async def read_desk_open_questions(
    conn,  # type: ignore[no-untyped-def]
    *,
    target_id: str,
    limit: int = QUESTION_CAP,
) -> list[dict[str, Any]]:
    """The desk's STANDING (unanswered) open questions, newest-first.

    Returns compact, JSON-safe dicts. A row with no id or an empty thesis is
    SKIPPED (never padded with a placeholder): the block may only name questions
    that actually exist.
    """
    if not target_id:
        return []
    rows = await conn.fetch(_OPEN_QUESTIONS_SQL, str(target_id), int(limit))
    out: list[dict[str, Any]] = []
    for raw in rows:
        r = dict(raw)
        qid = _coerce_uuid(r.get("id"))
        thesis = r.get("thesis")
        if qid is None or not isinstance(thesis, str) or not thesis.strip():
            continue
        out.append(
            {
                "question_id": str(qid),
                "question": thesis.strip()[:QUESTION_TEXT_CHARS],
                "asked_at": _iso_text(r.get("produced_at")),
                "age_days": _as_float(r.get("age_days")),
                "analyst_id": (
                    str(r["analyst_id"]) if r.get("analyst_id") is not None else None
                ),
            }
        )
    return out


async def gather_unit_grounding_rows(
    conn,  # type: ignore[no-untyped-def]
    *,
    analyst_id: str | None,
    target_filter: str | None,
    prior_lookback_hours: int = PRIOR_LOOKBACK_HOURS,
) -> list[dict[str, Any]]:
    """Gather the (at most four) marked GROUNDING rows for one unit run.

    BEST-EFFORT by contract: this is ADDITIVE enrichment on top of an already
    complete slice, so ANY failure (a missing relation, a degraded read replica, a
    descriptor with no identity block) logs and yields fewer rows — never an
    exception, never a lost evidence slice. Each block is fetched in its OWN
    ``try`` so one failure cannot suppress its siblings.

    A unit with no ``target_filter`` gets NOTHING: every block is desk-scoped by
    construction, and an unscoped read would hand a desk another desk's frames —
    the contamination class the D4 fix exists to prevent. A missing ``analyst_id``
    suppresses only the PRIOR READ (an unattributable "previous read" is exactly
    the uncited prior this design refuses); the three desk-scoped blocks still
    resolve.
    """
    if not target_filter:
        return []
    # LAZY by NECESSITY, not by taste. ``meta_findings_synthesizer`` imports
    # ``runtime.analyst_method``, which imports ``inline_target`` at ITS top —
    # and ``inline_target`` imports THIS module at its own top. A module-level
    # import here would close that cycle and break the runtime's very first
    # analyst import. Deferring it to the one function that needs a DB connection
    # costs nothing (the module is cached after the first call) and keeps the
    # readers a single implementation shared with the composition floor rather
    # than a second copy of the same SQL that can drift.
    from .meta_findings_synthesizer import (
        DEFAULT_VERIFY_FLOOR,
        read_open_situations,
        read_prior_composition_head,
    )

    out: list[dict[str, Any]] = []

    if analyst_id:
        try:
            prior = await read_prior_composition_head(
                conn,
                analyst_id=str(analyst_id),
                target_id=str(target_filter),
                verify_floor=DEFAULT_VERIFY_FLOOR,
                lookback_hours=prior_lookback_hours,
            )
        except Exception as exc:  # pragma: no cover — best-effort enrichment
            logger.warning(
                "unit_grounding.prior_read.failed analyst_id=%s target_id=%s err=%s",
                analyst_id, target_filter, exc,
            )
            prior = None
        if prior is not None:
            # ``read_prior_composition_head`` stamps the COMPOSITION marker key;
            # re-stamp under the unit key so the unit partition (which must never
            # look at composition markers) is the one that owns this row.
            row = dict(prior)
            row[UNIT_GROUNDING_ROW_KEY] = GROUNDING_PRIOR_READ
            out.append(row)

    try:
        situations = await read_open_situations(
            conn, target_id=str(target_filter), limit=SITUATION_CAP,
        )
    except Exception as exc:  # pragma: no cover — best-effort enrichment
        logger.warning(
            "unit_grounding.situations.failed target_id=%s err=%s", target_filter, exc,
        )
        situations = []
    if situations:
        out.append(_synthetic_row(GROUNDING_SITUATIONS, situations))

    try:
        baselines = await read_desk_baselines(conn, desk_id=str(target_filter))
    except Exception as exc:  # pragma: no cover — best-effort enrichment
        logger.warning(
            "unit_grounding.baseline.failed target_id=%s err=%s", target_filter, exc,
        )
        baselines = []
    if baselines:
        out.append(_synthetic_row(GROUNDING_BASELINE, baselines))

    try:
        questions = await read_desk_open_questions(conn, target_id=str(target_filter))
    except Exception as exc:  # pragma: no cover — best-effort enrichment
        logger.warning(
            "unit_grounding.questions.failed target_id=%s err=%s", target_filter, exc,
        )
        questions = []
    if questions:
        out.append(_synthetic_row(GROUNDING_QUESTIONS, questions))

    return out


def _synthetic_row(kind: str, payload: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One SYNTHETIC grounding row: a marker + its payload, nothing else.

    Deliberately carries NO ``id`` — the ORIENT partition lifts it out before
    ``derived_from`` is built, so a unit is never DERIVED from its own memory
    (it is ANNOTATED by it), and a stray consumer that walks the slice sees a row
    that cannot masquerade as a signal.
    """
    return {UNIT_GROUNDING_ROW_KEY: kind, GROUNDING_PAYLOAD_KEY: list(payload)}


# ---------------------------------------------------------------------------
# Partition + receipts
# ---------------------------------------------------------------------------


def partition_grounding_rows(
    inputs: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split a raw slice into ``(signal_rows, grounding_rows)``.

    Grounding rows are lifted off FIRST so they can never be mistaken for
    evidence: they must not consume the INPUT-token budget, must not enter
    ``derived_from``, and must not be re-ranked by the per-unit focus. Ordered by
    :data:`GROUNDING_BLOCK_KINDS` and de-duplicated FIRST-wins per kind (the
    reader emits at most one of each; a duplicate would be a bug, and taking the
    first keeps the ordinal space deterministic rather than silently renumbering).

    An input list with NO marked row returns ``(list(inputs), [])`` — the
    byte-identical pre-grounding path.
    """
    grounding: dict[str, Mapping[str, Any]] = {}
    signals: list[Mapping[str, Any]] = []
    for row in inputs:
        kind = row.get(UNIT_GROUNDING_ROW_KEY) if isinstance(row, Mapping) else None
        if isinstance(kind, str) and kind in GROUNDING_RECEIPT_KEYS:
            grounding.setdefault(kind, row)
            continue
        signals.append(row)
    ordered = [grounding[k] for k in GROUNDING_BLOCK_KINDS if k in grounding]
    return signals, ordered


def grounding_receipts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{receipt_key: 0|1}`` for EVERY block kind — present AND absent.

    Always reports all four keys: a silently-absent memory is exactly what this
    receipt exists to make visible, and a key that only appears when the block
    resolved cannot distinguish "no prior read" from "the receipt changed shape".
    """
    present = {
        row.get(UNIT_GROUNDING_ROW_KEY)
        for row in rows
        if isinstance(row, Mapping)
    }
    return {
        receipt: (1 if kind in present else 0)
        for kind, receipt in GROUNDING_RECEIPT_KEYS.items()
    }


# ---------------------------------------------------------------------------
# Render — one section, one ``[N]`` ordinal per block
# ---------------------------------------------------------------------------

_SECTION_HEADER = (
    "=== DESK GROUNDING (what this desk already knew) ===\n"
    "The block(s) below are the ONLY licensed source of 'before'. Each carries "
    "its own [N] handle: cite them exactly like a numbered signal, state what "
    "CHANGED against them, and take every date from the block itself — never "
    "from the time you are running. If nothing material changed, say that "
    "plainly. Never assert a trend, escalation, or 'ongoing' framing these "
    "blocks do not support."
)


def _render_prior_read(row: Mapping[str, Any], ordinal: int) -> list[str]:
    """The PRIOR READ block — dated by its OWN clock, confidence-free.

    No confidence number is printed (see the module note): a unit's signal blocks
    carry none, so a number shown only on last cycle's own read is an anchor the
    unit cannot weigh and would be tempted to inherit.
    """
    title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
    produced_at = _iso_text(row.get("produced_at")) or "(unknown)"
    age = _as_float(row.get("age_hours"))
    age_part = f" age={age:.1f}h" if age is not None else ""
    analyst_id = str(row.get("analyst_id") or "(unknown)")
    return [
        f"[{ordinal}] PRIOR READ — this unit's previous verified read of this "
        f"target: {title}",
        f"    analyst_id={analyst_id} produced_at={produced_at}{age_part}",
        f"    body: {_body_excerpt(row, PRIOR_BODY_CHARS)}",
    ]


def _render_situations(situations: Sequence[Mapping[str, Any]], ordinal: int) -> list[str]:
    """The OPEN SITUATION REGISTER block — ONE ordinal for the whole register
    (it is a single orienting index, not N pieces of evidence)."""
    lines = [
        f"[{ordinal}] OPEN SITUATION REGISTER — {len(situations)} open frame(s) "
        "on this desk, highest-intensity first (the platform's CLUSTERED "
        "situational picture, NOT operator-vetted ground truth):",
    ]
    for s in situations:
        lines.append(
            f"    - {str(s.get('name') or '')[:SITUATION_NAME_CHARS]} :: "
            f"status={s.get('status')} intensity={_fmt(s.get('intensity_score'))} "
            f"events={_fmt_int(s.get('event_count'))} "
            f"last_event_at={s.get('last_event_at') or '(none)'} "
            f"open_for={_fmt_days(s.get('age_days'))}"
        )
    return lines


def _render_baselines(baselines: Sequence[Mapping[str, Any]], ordinal: int) -> list[str]:
    """The DESK BASELINE block — what is NORMAL here, as a falsifiable number.

    Every rendered row cleared ``insufficient_history`` at read time, so the block
    never has to caveat itself. It states the band, the observed current value and
    the computed direction; it is NOT a forecast and says so.
    """
    lines = [
        f"[{ordinal}] DESK BASELINE — what is NORMAL for this desk "
        f"({len(baselines)} metric(s); trailing statistical prior, "
        "analysis-derived, NOT a forecast):",
    ]
    for b in baselines:
        sigma = b.get("deviation_sigma")
        sigma_part = (
            f" ({float(sigma):+.1f}σ)" if isinstance(sigma, (int, float)) else ""
        )
        lines.append(
            f"    - {b.get('metric')} :: expected={_fmt(b.get('expected'))} "
            f"(median {_fmt(b.get('center_median'))}) normal band "
            f"{_fmt(b.get('band_low'))}-{_fmt(b.get('band_high'))} "
            f"at ±{_fmt(b.get('n_sigma'))}σ over {_fmt_int(b.get('baseline_days'))}d "
            f"({_fmt_int(b.get('sample_days'))} sampled, "
            f"{_fmt_int(b.get('active_days'))} active) — "
            f"current={_fmt(b.get('current'))} => {str(b.get('deviation')).upper()}"
            f"{sigma_part} [computed_at={b.get('computed_at') or '(unknown)'}]"
        )
    return lines


def _render_questions(questions: Sequence[Mapping[str, Any]], ordinal: int) -> list[str]:
    """The STANDING OPEN QUESTIONS block — this desk's own unanswered asks."""
    lines = [
        f"[{ordinal}] STANDING OPEN QUESTIONS — {len(questions)} question(s) this "
        "desk raised and NOBODY has answered yet, newest first:",
    ]
    for q in questions:
        asked_by = q.get("analyst_id")
        by_part = f" by={asked_by}" if asked_by else ""
        lines.append(
            f"    - {q.get('question')} [asked_at={q.get('asked_at') or '(unknown)'}"
            f" open_for={_fmt_days(q.get('age_days'))}{by_part}]"
        )
    return lines


_RENDERERS = {
    GROUNDING_PRIOR_READ: _render_prior_read,
    GROUNDING_SITUATIONS: _render_situations,
    GROUNDING_BASELINE: _render_baselines,
    GROUNDING_QUESTIONS: _render_questions,
}


def _render_block(row: Mapping[str, Any], ordinal: int) -> list[str]:
    """Render ONE grounding block at ``ordinal`` (``[]`` for an unknown kind)."""
    kind = row.get(UNIT_GROUNDING_ROW_KEY)
    renderer = _RENDERERS.get(str(kind))
    if renderer is None:
        return []
    if kind == GROUNDING_PRIOR_READ:
        return renderer(row, ordinal)  # type: ignore[arg-type]
    payload = row.get(GROUNDING_PAYLOAD_KEY)
    entries = [e for e in payload if isinstance(e, Mapping)] if isinstance(payload, (list, tuple)) else []
    if not entries:
        return []
    return renderer(entries, ordinal)  # type: ignore[arg-type]


def render_grounding_section(
    rows: Sequence[Mapping[str, Any]],
    *,
    start_ordinal: int,
) -> tuple[str, list[tuple[int, Mapping[str, Any]]]]:
    """Render the grounding section + the ``(ordinal, row)`` pairs it stamped.

    Ordinals CONTINUE the numbering the signal slice (and any GATHER-gathered
    corpus docs) already used, so ``[N]`` stays ONE flat resolution space: the
    REFLECT phase maps ordinal N to the Nth rendered block across all sources, and
    no consumer has to re-parse the prompt to tell them apart (the resolved
    citation's ``grounding`` stamp does that).

    Returns ``("", [])`` when there is nothing to render — so a first run's prompt
    is byte-identical to the pre-grounding render.
    """
    body: list[str] = []
    stamped: list[tuple[int, Mapping[str, Any]]] = []
    ordinal = start_ordinal
    for row in rows:
        lines = _render_block(row, ordinal)
        if not lines:
            continue
        if body:
            body.append("")
        body.extend(lines)
        stamped.append((ordinal, row))
        ordinal += 1
    if not stamped:
        return "", []
    return "\n".join([_SECTION_HEADER, ""] + body), stamped


def block_evidence_text(row: Mapping[str, Any], ordinal: int) -> str:
    """The captured evidence text for ONE block — exactly what was rendered.

    This is what ``verify._marker_to_evidence`` hands the judge, so it must be the
    same bytes the model read (bounded by :data:`EVIDENCE_TEXT_CHARS`).
    """
    return "\n".join(_render_block(row, ordinal))[:EVIDENCE_TEXT_CHARS]


# ---------------------------------------------------------------------------
# Citations — honest ref shapes, no fabricated anchors
# ---------------------------------------------------------------------------

_BLOCK_TITLES = {
    GROUNDING_PRIOR_READ: "Prior read (this unit's previous verified read)",
    GROUNDING_SITUATIONS: "Open-situation register",
    GROUNDING_BASELINE: "Desk baseline",
    GROUNDING_QUESTIONS: "Standing open questions",
}


def citation_for_block(row: Mapping[str, Any], ordinal: int) -> dict[str, Any] | None:
    """ONE resolved citation for a cited grounding block, or ``None``.

    Ref shapes, and why each is what it is:

    * PRIOR READ — a real ``analyst_outputs`` row, so it carries ``ref_id`` (the
      correct drill target: the previous read) under ``ref_kind='prior_read'``.
      NOT ``ref_kind='finding'``: that token is the COMPOSITION discriminator
      (``verify._uses_subclaim_convention``) and would route the whole unit
      finding to the sub-claim floor. It carries NO ``effective_confidence`` and
      NO ``derived_from`` — the prior read is MEMORY, not corroboration.
    * REGISTER / BASELINE / QUESTIONS — synthetic blocks with no single substrate
      id, so they carry the REAL underlying ids (``situation_ids`` /
      ``baseline_keys`` / ``question_ids``) and NO ``ref_id``. Minting one so a
      drill link resolves would be a fabricated anchor.

    Every shape carries ``evidence_text`` (the rendered block) so the verify pass
    grades a block-backed clause against exactly what the model was shown, and
    ``grounding`` naming WHICH block it resolved into.
    """
    kind = row.get(UNIT_GROUNDING_ROW_KEY)
    if not isinstance(kind, str) or kind not in GROUNDING_RECEIPT_KEYS:
        return None
    evidence = block_evidence_text(row, ordinal)
    if not evidence:
        return None
    citation: dict[str, Any] = {
        "marker": f"[{ordinal}]",
        "ordinal": ordinal,
        "ref_kind": kind,
        "grounding": kind,
        "title": _BLOCK_TITLES.get(kind, kind),
        "evidence_text": evidence,
    }
    if kind == GROUNDING_PRIOR_READ:
        ref_id = _coerce_uuid(row.get("id"))
        if ref_id is None:
            # Never claim a prior read we cannot point at.
            return None
        citation["ref_id"] = str(ref_id)
        citation["title"] = str(row.get("title") or citation["title"])[:MAX_TITLE_CHARS]
        citation["produced_at"] = _iso_text(row.get("produced_at"))
        return citation
    entries = row.get(GROUNDING_PAYLOAD_KEY)
    entries = [e for e in entries if isinstance(e, Mapping)] if isinstance(entries, (list, tuple)) else []
    if kind == GROUNDING_SITUATIONS:
        citation["situation_ids"] = [
            str(e["situation_id"]) for e in entries if e.get("situation_id")
        ]
    elif kind == GROUNDING_BASELINE:
        # ``desk_baselines`` is keyed (desk_id, metric) with no uuid, so the
        # honest handle is that composite key — not a synthesized id.
        citation["baseline_keys"] = [
            f"{e.get('desk_id')}:{e.get('metric')}"
            for e in entries
            if e.get("metric")
        ]
    elif kind == GROUNDING_QUESTIONS:
        citation["question_ids"] = [
            str(e["question_id"]) for e in entries if e.get("question_id")
        ]
    return citation


# ---------------------------------------------------------------------------
# The prompt contract — ONE clause, appended to every unit system prompt
# ---------------------------------------------------------------------------
#
# Generated as a single constant rather than pasted into nine descriptors: a copy
# per unit would drift the moment one is edited, and the whole point of a
# grounding contract is that every unit states it identically. It encodes exactly
# the four obligations the composition clause encodes, in the order a reader needs
# them — SAY WHAT CHANGED, ANCHOR ON THE BLOCK'S OWN DATES, NO-CHANGE IS AN
# ANSWER, and NEVER assert continuity that is not grounded in a shown block —
# plus the two obligations the extra unit blocks create: argue against the
# baseline number instead of a vibe, and treat a standing question as standing
# unless this run's own evidence answers it.

UNIT_GROUNDING_CLAUSE: str = (
    "DESK GROUNDING (what this desk already knew). AFTER the numbered signals you "
    "may be shown a DESK GROUNDING section carrying up to four blocks, each with "
    "its own [N] handle in the SAME numbering as the signals: a PRIOR READ (this "
    "unit's own previous verified read of this target, with its produced_at and "
    "age), an OPEN SITUATION REGISTER (the desk's open frames with their status, "
    "intensity, event count and last_event_at), a DESK BASELINE (the trailing "
    "normal band for this desk with the current observed value), and STANDING "
    "OPEN QUESTIONS (questions this desk raised that nobody has answered). When a "
    "block is shown you MUST: (1) state EXPLICITLY what CHANGED versus the cited "
    "PRIOR READ — name the change and cite the block by its [N] handle exactly "
    "like a signal; (2) anchor EVERY temporal statement on the dates printed IN "
    "those blocks (the prior read's produced_at, a situation's last_event_at, the "
    "baseline's computed_at, a question's asked_at) — NEVER on 'today', 'now', "
    "'as of this run', or the time you are running; (3) if nothing material "
    "changed, SAY SO plainly and briefly (e.g. 'no material change since the "
    "prior read of <its produced_at> [N]') rather than re-deriving the same "
    "picture in different words; (4) describe a situation ONLY as the register "
    "states it — its own name, status, intensity and event count — and never "
    "upgrade, downgrade, or re-date it beyond what the register shows; (5) when "
    "you call this window unusual (or normal), say so AGAINST the DESK BASELINE "
    "band and cite it — do not assert 'elevated' or 'a spike' when the baseline "
    "block shows the current value inside its normal band, and never restate the "
    "baseline as a forecast; (6) treat every STANDING OPEN QUESTION as still "
    "open unless THIS run's cited evidence answers it — if it does, say which "
    "question and cite the signal that answers it; if it does not, do not "
    "re-ask the same question as if it were new. NEVER assert continuity of ANY "
    "kind — an escalation, a de-escalation, a trend, an 'ongoing'/'longstanding' "
    "framing, or that something has 'been building' — unless it is grounded in a "
    "cited DESK GROUNDING block. If NO DESK GROUNDING section is shown this is a "
    "FIRST read of this target: make NO claim about what came before and use no "
    "'ongoing' / 'continuing' / 'still' framing."
)

_CLAUSE_FINGERPRINT = "DESK GROUNDING (what this desk already knew)."


def with_grounding_clause(system_prompt: str) -> str:
    """Append the grounding clause to a unit system prompt, exactly once.

    Idempotent by fingerprint so a re-resolution (or a GEPA-promoted candidate
    that already carries the clause) can never double it — the same posture
    ``_tradecraft.with_preamble_if_absent`` takes for the house preamble.

    The clause is appended in CODE rather than pasted into nine descriptors: one
    definition, nine units, no drift. It is appended UNCONDITIONALLY — the
    "no blocks shown ⇒ this is a first read, claim nothing about before" leg is
    exactly the obligation a unit with no memory needs, and making the clause
    conditional on the blocks resolving would leave that leg unstated on the one
    run where it bites hardest.
    """
    if not system_prompt:
        return system_prompt
    if _CLAUSE_FINGERPRINT in system_prompt:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{UNIT_GROUNDING_CLAUSE}\n"


# ---------------------------------------------------------------------------
# Small coercions — each returns None rather than a fabricated zero/date
# ---------------------------------------------------------------------------


def _coerce_uuid(raw: Any) -> UUID | None:
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _iso_text(value: Any) -> str | None:
    """ISO-8601 for a datetime, the string itself when the driver handed one
    back, ``None`` otherwise — never a fabricated absence."""
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_float(value: Any) -> float | None:
    """Float coercion that returns ``None`` rather than a fabricated 0.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """Int coercion that returns ``None`` rather than a fabricated 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    """A number for the prompt, or the honest ``n/a`` — never a fabricated 0."""
    return f"{float(value):.1f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "n/a"


def _fmt_int(value: Any) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "n/a"


def _fmt_days(value: Any) -> str:
    return (
        f"{float(value):.1f}d"
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else "n/a"
    )


def _body_excerpt(row: Mapping[str, Any], cap: int) -> str:
    """The row's body excerpt — column first, then ``data.body`` (the same
    fallback chain the composition's ``_row_body_excerpt`` walks)."""
    body = row.get("body")
    if not isinstance(body, str):
        data = row.get("data")
        inner = data.get("body") if isinstance(data, Mapping) else None
        body = inner if isinstance(inner, str) else ""
    return body[:cap]
