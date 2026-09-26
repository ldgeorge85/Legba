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

SIX BLOCKS, all bounded, all absent-by-default (the sixth also GRANT-gated —
see :data:`GROUNDING_OPEN_EVENTS`):

  * PRIOR READ (:data:`GROUNDING_PRIOR_READ`) — THIS unit's own previous
    non-superseded, VERIFIED head for THIS target. Reuses the composition's
    :func:`~legba.data.analysts.meta_findings_synthesizer.read_prior_composition_head`
    verbatim (the reader is analyst-agnostic: "the same analyst_id's last verified
    head for the same target_id"), so the verify GATE, the lookback bound and the
    coerce-fallback drop are ONE implementation, not two that can drift.
  * WINDOW LEDGER (:data:`GROUNDING_WINDOW_LEDGER`) — FRAME-2, the CARRY. The
    prior read is ONE step back and the clause below obliges a SINGLE-STEP diff;
    the ledger is that memory made CUMULATIVE — every verified, severity-tagged
    head THIS unit wrote over the trailing fortnight, one dated line each. It
    exists because the round's largest attributed failure class (ATTRIBUTION
    H-FRAME Class 3, ~12 of 23 missed majors) was an event that happened in the
    window's first ten days, WAS in this desk's slice then, and had aged out of
    every 72h slice by T0 with nothing carrying it forward — while the unit
    printed "mass protest: not_observed" for a fortnight that contained exactly
    that. Scope is this unit's OWN heads, not the desk's: a unit answers ONE
    bounded question, and handing it its siblings' dimensions invites the scope
    creep its descriptor forbids (§2.2 — desk-wide at the unit layer is an R2
    decision, not a round-1 one). The selection, render, marker defuse and
    clause live in :mod:`legba.data.analysts.window_ledger` — ONE definition
    shared with the composition floor, which renders the DESK-scoped form of the
    same block.
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
  * OPEN EVENTS (:data:`GROUNDING_OPEN_EVENTS`) — V3/P2's missing half: the
    desk's live ``events`` rows, reached THROUGH its open ``situations`` (the
    same per-desk scope the register takes), each printed with its
    ``event:<uuid>`` TOKEN so the unit can cite the event's underlying REPORTS.
    P2 built the whole citation side — ``provenance.event_citations
    .expand_event_citation`` + the ``inline_target._expand_event_refs`` splice
    turn an ``event:<uuid>`` token in a finding body into fresh ``[K]`` markers
    over the event's member signals — and nothing in the tree ever RENDERED an
    event id into a prompt, so no model had ever emitted the token (59 receipts
    since ``LEGBA_EVENT_CITATIONS=1``, every one ``event_citations: 0``). This
    block is the OFFER. It is the only grounding block behind a descriptor
    GRANT (``method.options.offer_events``, default False): the plan's
    acceptance step is "ONE analyst granted event citations", and with the knob
    absent every unit's rendered prompt is byte-identical to the five-block
    render. The block cites like the register — ONE ordinal, no fabricated
    ``ref_id``, the real ``event_ids`` carried — and the TOKENS inside it are
    what the expansion resolves, never this block's own ordinal.

  * STANDING OPEN QUESTIONS (:data:`GROUNDING_QUESTIONS`) — the desk's open
    ``hypotheses`` rows (``status='open_question'``), newest-first. This closes a
    loop that was open end-to-end: every unit descriptor EMITS ``open_questions``
    (converted to first-class rows by ``inline_target.convert_open_questions``)
    and no unit ever read one back.

WHAT IS DELIBERATELY *NOT* HERE:

  * NO new analyst kind, NO new table, NO migration. Five SELECTs and a render.
  * NO event SUMMARY, anywhere. The OPEN EVENTS block prints an event's TITLE,
    span, corroboration counts and lifecycle state — never ``events.summary``,
    which DATA_MODEL_V3 §2.5 rules 1-2 declare is never evidence. The whole
    point of the token is that the event expands into its member REPORTS.
  * NO ``ref_kind='event'`` on the block's own citation, and ``'event'`` stays
    out of ``provenance.kinds.GROUNDING_REF_KINDS``. The block is
    ``ref_kind='open_events'`` (a grounding block, graded on its rendered
    text); an EXPANDED event citation is an ordinary per-signal entry. Two
    different things, two different kinds — conflating them is exactly how an
    event would end up graded against its own summary.
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
import re
from typing import Any, Mapping, Sequence
from uuid import UUID

from ..provenance.citation_markers import prior_read_ref
from ..provenance.origin import origin_class_clause
from .history_grounding import (
    GROUNDING_HISTORY,
    HISTORY_SERIES_CAP,
    HISTORY_SERIES_MAX_CAP,
    history_block_lines,
    observation_citations,
    ordinal_span,
    read_desk_history,
)
from .window_ledger import (
    LEDGER_UNIT_TOTAL_CAP,
    REGISTER_SELF_CORROBORATION_RULE,
    _render_evidence_age,
    ledger_block_lines,
    ledger_finding_ids,
    read_window_ledger,
    select_ledger_entries,
)

logger = logging.getLogger(__name__)


__all__ = [
    "GROUNDING_BASELINE",
    "GROUNDING_BLOCK_KINDS",
    "GROUNDING_HISTORY",
    "GROUNDING_OPEN_EVENTS",
    "GROUNDING_PRIOR_READ",
    "GROUNDING_QUESTIONS",
    "GROUNDING_RECEIPT_KEYS",
    "GROUNDING_SITUATIONS",
    "GROUNDING_WINDOW_LEDGER",
    "OPEN_EVENTS_CAP",
    "OPEN_EVENTS_CITE_RULE",
    "OPEN_EVENTS_MAX_CAP",
    "UNIT_GROUNDING_CLAUSE",
    "UNIT_GROUNDING_ROW_KEY",
    "block_ordinal_span",
    "citation_for_block",
    "gather_unit_grounding_rows",
    "grounding_citations",
    "grounding_receipts",
    "partition_grounding_rows",
    "read_desk_baselines",
    "read_desk_open_events",
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

GROUNDING_WINDOW_LEDGER: str = "window_ledger"
"""Marker value: the synthetic WINDOW LEDGER block (FRAME-2 — the carry).

Deliberately the SAME token as
:data:`legba.data.analysts.window_ledger.WINDOW_LEDGER_REF_KIND`, which is what
the composition floor stamps: one block, one name, one entry in
``provenance.kinds.GROUNDING_REF_KINDS``, so the verify path cannot end up
grading the two layers' copies of the same block by two different rules."""

GROUNDING_SITUATIONS: str = "situation_register"
"""Marker value: the synthetic OPEN-SITUATION REGISTER block."""

GROUNDING_OPEN_EVENTS: str = "open_events"
"""Marker value: the synthetic OPEN EVENTS block (V3/P2 — the OFFER).

The ONE grounding block behind a descriptor GRANT
(``method.options.offer_events``): every other block resolves for every unit
with a desk, this one renders only where the descriptor asked for it, because
the plan's acceptance step is "ONE analyst granted event citations" and a
fleet-wide offer would put an un-piloted token in nine units' prompts at once.

Deliberately NOT the token ``'event'``. ``'event'`` is the EXPANDED citation's
``ref_kind`` (``provenance.event_citations``) and is kept out of
``provenance.kinds.GROUNDING_REF_KINDS`` on purpose — an expanded entry carries
a ``signal_id`` and is graded on the REPORT's raw source text. This block is a
grounding block like the register: graded on its own rendered text, carrying
the real ``event_ids`` and no ``ref_id``. One name per thing."""

GROUNDING_BASELINE: str = "desk_baseline"
"""Marker value: the synthetic DESK BASELINE block."""

GROUNDING_QUESTIONS: str = "open_questions"
"""Marker value: the synthetic STANDING OPEN QUESTIONS block."""

GROUNDING_BLOCK_KINDS: tuple[str, ...] = (
    GROUNDING_PRIOR_READ,
    GROUNDING_WINDOW_LEDGER,
    GROUNDING_SITUATIONS,
    GROUNDING_OPEN_EVENTS,
    GROUNDING_BASELINE,
    GROUNDING_QUESTIONS,
    GROUNDING_HISTORY,
)
"""The block kinds IN RENDER ORDER — one step back, then the fortnight, then the
open picture, then the statistical prior, then the standing debt. The ordinal a
block receives is its position in THIS sequence among the blocks actually
present, so the ordinal space stays contiguous and gap-free whichever subset
resolved.

The WINDOW LEDGER sits SECOND, beside the prior read, because both are MEMORY
and a reader (human or model) meeting "what I said last cycle" immediately
followed by "what I established this fortnight" reads one continuous account of
before. The composition floor orders its own three blocks the same way.

OPEN EVENTS sits FOURTH, immediately after the register, because it is the
register's own contents one level down: the events it lists are reached THROUGH
this desk's open situations, so frames-then-occurrences is the order a reader
already holds in mind. Inserting it mid-sequence cannot renumber anything for
an UNGRANTED unit — the row only exists where ``offer_events`` is set, and
ordinals are positions among the blocks actually PRESENT.

HISTORICAL SERIES sits LAST, and it is the only block that is not about this
platform. The six before it are Legba's own memory — what this unit said, what
this desk is watching, what is normal here; the seventh is what the WORLD
measured, from a curated holding nothing schedules. A reader meets our account
of before and then the record against which it can be checked, which is the
order the two belong in. It is the second GRANTED block
(``method.options.offer_history``, default False) and, like OPEN EVENTS, its
absence renumbers nothing: with the knob unset the read does not fire, no row
exists, and the rendered prompt is byte-identical to the six-block render.

It is also the ONLY block that takes MORE THAN ONE ordinal — one per series
line. See :mod:`legba.data.analysts.history_grounding` for why (each line is an
independent number with its own period, and one ordinal over eleven of them is
how a 2016 figure gets graded as a current claim)."""

GROUNDING_PAYLOAD_KEY: str = "_grounding_payload"
"""Key on a SYNTHETIC grounding row carrying its rendered payload (the situation
/ baseline / question dicts). The PRIOR READ is a real ``analyst_outputs`` row
and carries no payload key — it renders off its own columns."""

GROUNDING_RECEIPT_KEYS: dict[str, str] = {
    GROUNDING_PRIOR_READ: "grounding_prior_ref",
    GROUNDING_WINDOW_LEDGER: "grounding_window_ledger_ref",
    GROUNDING_SITUATIONS: "grounding_situations_ref",
    GROUNDING_OPEN_EVENTS: "grounding_open_events_ref",
    GROUNDING_BASELINE: "grounding_baseline_ref",
    GROUNDING_QUESTIONS: "grounding_questions_ref",
    GROUNDING_HISTORY: "grounding_history_ref",
}
"""Receipt key per block kind. Reported on the run's ``orient`` step (and its own
``grounding_blocks`` step) so "did this unit get its memory this cycle" is
answerable from a trace without re-running the gather. 0/1 each — these are single
blocks by construction, and counting them is how a silently-absent memory becomes
visible instead of reading as a first run forever. FRAME-2's receipt
(``grounding_window_ledger_ref``) is the §2.2 requirement that a silently absent
CARRY is visible in traces rather than indistinguishable from a quiet desk."""


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

OPEN_EVENTS_CAP: int = 8
"""Max events in the OPEN EVENTS block. Eight rather than the register's six
because an event line is ONE line (the register's frames carry a trajectory
tail), and because the point of the block is a CITABLE menu: a desk offered
three tokens has effectively been offered the loudest three, which is the
selection bias the offer exists to avoid. Live worst case is well inside it —
33 desks carry an open event at all and the busiest (``country_g20_us``) holds
7 (measured 2026-09-24)."""

OPEN_EVENTS_MAX_CAP: int = 20
"""Ceiling on the descriptor-settable ``open_events_limit``. A unit prompt is
budgeted for an ORIENTING index, not a second evidence slice; twenty one-line
events is already past where a reader is reading and comfortably past what any
desk holds."""

EVENT_TITLE_CHARS: int = 120
"""Per-event title cap — an event title is a short occurrence LABEL, the same
posture :data:`SITUATION_NAME_CHARS` takes for a frame."""

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


# V3/P2 — the OPEN EVENTS read. The desk's events THROUGH its open situations,
# which is the only per-desk scope that exists today: ``events.target_id`` is
# NULL on 259 of the 317 live open events (measured 2026-09-24), so selecting
# by it would silently hand most desks an empty block while their frames carry
# dozens of occurrences. ``situations.target_id`` is the SAME scoping rule
# ``read_open_situations`` takes, for the same reason (a desk must not see
# another desk's frames), and ``idx_situations_target_id`` is what the plan
# walks into.
#
# THE SHAPE, and why it is two stages rather than one. Stage one picks the <=N
# events — a plain join, DISTINCT because one event can hang off several of a
# desk's frames, ordered by ``updated_at DESC`` with ``id`` breaking the tie so
# the block is stable between two runs that see the same rows. Stage two counts
# each of those <=N events' member links. That count is NOT
# ``events.signal_count``, and that is the load-bearing part of this reader:
# ``signal_count`` READ wrong on 244 of the 317 live open events (73 correct;
# worst 59,808 against 168 real ``signal_event_links`` rows — the lifecycle scan
# counted (member x actor) PAIRS; fixed at the source, rows corrected by
# migration 0218), ``distinct_source_count`` exact. Printing a report count 350x
# the truth into a prompt is a fabrication about evidence weight, so the number
# is DERIVED here. It is derived as a GROUPED AGGREGATE over the bounded <=N
# driving set — never as a correlated per-row probe, the shape this repo's
# review rules name as having failed four times — and it walks
# ``idx_sel_event``. Live cost with the second stage: 9.6 ms on the widest desk
# (``country_g20_ru``, 752 links walked); 5.6 ms without it.
#: P7/7g-1 — the origin-class leg on the OPEN EVENTS read (SEAMS #57 sweep).
#: `events` DOES carry origin_class (migration 0209); the live clustering lane
#: stamps 'live' on every row it mints, so this is a no-op on today's data and
#: a fence the day a history event exists.
_LIVE_EVENTS = origin_class_clause("e")

_OPEN_EVENTS_SQL = f"""
    WITH desk_events AS (
        SELECT DISTINCT
               e.id, e.title, e.time_start, e.time_end,
               e.distinct_source_count, e.lifecycle_state, e.updated_at
          FROM situation_event_links sel
          JOIN situations s ON s.id = sel.situation_id
          JOIN events e ON e.id = sel.event_id
         WHERE s.target_id = $1
           AND s.status IS DISTINCT FROM 'closed'
           AND e.lifecycle_state <> 'resolved'
           AND e.superseded_by IS NULL
           AND {_LIVE_EVENTS}
         ORDER BY e.updated_at DESC, e.id
         LIMIT $2
    )
    SELECT d.id, d.title, d.time_start, d.time_end,
           d.distinct_source_count, d.lifecycle_state, d.updated_at,
           EXTRACT(EPOCH FROM (NOW() - d.updated_at)) / 86400.0
               AS updated_age_days,
           count(l.signal_id) AS report_count
      FROM desk_events d
      LEFT JOIN signal_event_links l ON l.event_id = d.id
     GROUP BY d.id, d.title, d.time_start, d.time_end,
              d.distinct_source_count, d.lifecycle_state, d.updated_at
     ORDER BY d.updated_at DESC, d.id
"""


async def read_desk_open_events(
    conn,  # type: ignore[no-untyped-def]
    *,
    target_id: str,
    limit: int = OPEN_EVENTS_CAP,
) -> list[dict[str, Any]]:
    """The desk's LIVE events, reached through its OPEN situation frames.

    Returns compact, JSON-safe dicts, most recently updated first (``[]`` when
    the desk has no open frame carrying a live event — the honest absence: no
    block is rendered and the receipt reads 0). A row with no id or an empty
    title is SKIPPED rather than padded: the block may only offer tokens that
    resolve, and an unciteable line in a block whose whole purpose IS citation
    is worse than a shorter block.

    ``summary`` is deliberately not in the SELECT at all. It is never evidence
    (DATA_MODEL_V3 §2.5), and a column that must never be rendered is better
    kept out of the reader than trusted to a renderer not to print it.
    """
    if not target_id:
        return []
    rows = await conn.fetch(_OPEN_EVENTS_SQL, str(target_id), int(limit))
    out: list[dict[str, Any]] = []
    for raw in rows:
        r = dict(raw)
        eid = _coerce_uuid(r.get("id"))
        title = r.get("title")
        if eid is None or not isinstance(title, str) or not title.strip():
            continue
        out.append(
            {
                "event_id": str(eid),
                "title": title.strip()[:EVENT_TITLE_CHARS],
                "time_start": _iso_text(r.get("time_start")),
                "time_end": _iso_text(r.get("time_end")),
                "report_count": _as_int(r.get("report_count")),
                "source_count": _as_int(r.get("distinct_source_count")),
                "lifecycle_state": str(r.get("lifecycle_state") or "unknown"),
                "updated_at": _iso_text(r.get("updated_at")),
                "updated_age_days": _as_float(r.get("updated_age_days")),
            }
        )
    return out


async def gather_unit_grounding_rows(
    conn,  # type: ignore[no-untyped-def]
    *,
    analyst_id: str | None,
    target_filter: str | None,
    prior_lookback_hours: int = PRIOR_LOOKBACK_HOURS,
    offer_events: bool = False,
    open_events_limit: int = OPEN_EVENTS_CAP,
    offer_history: bool = False,
    history_series_limit: int = HISTORY_SERIES_CAP,
) -> list[dict[str, Any]]:
    """Gather the (at most seven) marked GROUNDING rows for one unit run.

    BEST-EFFORT by contract: this is ADDITIVE enrichment on top of an already
    complete slice, so ANY failure (a missing relation, a degraded read replica, a
    descriptor with no identity block) logs and yields fewer rows — never an
    exception, never a lost evidence slice. Each block is fetched in its OWN
    ``try`` so one failure cannot suppress its siblings.

    A unit with no ``target_filter`` gets NOTHING: every block is desk-scoped by
    construction, and an unscoped read would hand a desk another desk's frames —
    the contamination class the D4 fix exists to prevent. A missing ``analyst_id``
    suppresses the PRIOR READ **and the WINDOW LEDGER** — both are scoped to THIS
    unit's own heads, and an unattributable "what I said before" is exactly the
    uncited prior this design refuses; the three desk-scoped blocks still resolve.

    ``offer_events`` is the V3/P2 GRANT and defaults False, so every caller that
    does not pass it gathers exactly the five blocks it gathered before — the
    OPEN EVENTS read does not even fire. It is a parameter rather than an env
    flag because the grant is PER ANALYST (the plan: "ONE analyst granted event
    citations"), and per-analyst configuration on this platform is a descriptor
    option, not a process-wide switch.

    ``offer_history`` is 7g-2's GRANT and defaults False on the same terms, for
    the same reason: the HISTORICAL SERIES block reads a curated holding rather
    than this platform's own memory, and a fleet-wide offer would put ten-year-
    old numbers in nine units' prompts at once. With it unset the read does not
    fire and the rendered prompt is byte-identical to the six-block render.
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

        # FRAME-2 — the CARRY, at OWN-UNIT scope. ``per_unit_cap=None`` because
        # the plan's 3-lines-per-unit bound is a FAIRNESS rule for the desk-wide
        # form (stop one loud dimension crowding out six others) and there is
        # nobody here to crowd out; the one-line-per-(unit, calendar day) dedupe
        # already bounds a single unit's fortnight, and 3 lines is not a
        # fortnight. Same total cap either way.
        try:
            ledger = select_ledger_entries(
                await read_window_ledger(
                    conn,
                    target_id=str(target_filter),
                    analyst_ids=[str(analyst_id)],
                ),
                per_unit_cap=None,
                total_cap=LEDGER_UNIT_TOTAL_CAP,
            )
        except Exception as exc:  # pragma: no cover — best-effort enrichment
            logger.warning(
                "unit_grounding.window_ledger.failed analyst_id=%s target_id=%s "
                "err=%s — this run reads WITHOUT its fortnight",
                analyst_id, target_filter, exc,
            )
            ledger = []
        if ledger:
            out.append(_synthetic_row(GROUNDING_WINDOW_LEDGER, ledger))

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

    # V3/P2 — the OFFER, behind its per-analyst GRANT. Gathered immediately
    # after the register because it is the register's own frames one level
    # down, and skipped ENTIRELY (no query, no row, no cost) for every unit
    # whose descriptor did not ask: an ungranted run must not pay for a block
    # it will not render, and must render byte-identically to the five-block
    # prompt it rendered yesterday.
    if offer_events:
        try:
            events = await read_desk_open_events(
                conn,
                target_id=str(target_filter),
                limit=max(1, min(int(open_events_limit), OPEN_EVENTS_MAX_CAP)),
            )
        except Exception as exc:  # pragma: no cover — best-effort enrichment
            logger.warning(
                "unit_grounding.open_events.failed target_id=%s err=%s "
                "— this run reads WITHOUT its citable events",
                target_filter, exc,
            )
            events = []
        if events:
            out.append(_synthetic_row(GROUNDING_OPEN_EVENTS, events))

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

    # 7g-2 — HISTORICAL SERIES, behind its own per-analyst GRANT and skipped
    # ENTIRELY (no query, no row, no cost) for every unit whose descriptor did
    # not ask. The read reaches a table no other grounding block touches
    # (`observations`, a curated holding), and it is the only block whose rows
    # are not this platform's own memory — so it is offered per desk, never
    # fleet-wide, exactly as the OPEN EVENTS grant is.
    if offer_history:
        try:
            history = await read_desk_history(
                conn,
                target_id=str(target_filter),
                limit=max(1, min(int(history_series_limit), HISTORY_SERIES_MAX_CAP)),
            )
        except Exception as exc:  # pragma: no cover — best-effort enrichment
            logger.warning(
                "unit_grounding.history.failed target_id=%s err=%s "
                "— this run reads WITHOUT its historical series",
                target_filter, exc,
            )
            history = []
        if history:
            out.append(_synthetic_row(GROUNDING_HISTORY, history))

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

    Always reports EVERY key: a silently-absent memory is exactly what this
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

    The body's own ``[N]`` markers arrive already defused to ``[prior:N]``
    (see :func:`_defuse_prior_read_markers`) — the prior run's citations must
    never be inheritable as THIS run's — and when any survive the excerpt cap,
    ONE header line names what they are, so the model reads them as the prior
    run's numbering instead of a citation format to imitate.
    """
    title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
    produced_at = _iso_text(row.get("produced_at")) or "(unknown)"
    age = _as_float(row.get("age_hours"))
    age_part = f" age={age:.1f}h" if age is not None else ""
    analyst_id = str(row.get("analyst_id") or "(unknown)")
    body = _body_excerpt(row, PRIOR_BODY_CHARS)
    lines = [
        f"[{ordinal}] PRIOR READ — this unit's previous verified read of this "
        f"target: {title}",
        f"    analyst_id={analyst_id} produced_at={produced_at}{age_part}",
        # 2026-08-29 (task #62) — the LICENSED SPELLING. Citing this block as a
        # bare ``[N]`` is correct and stays correct; naming it costs the desk
        # nothing, because both marker parsers rewrite the long form back to
        # ``[N]`` before anything grades it (``citation_markers
        # ._PRIOR_READ_REF_RE`` at verify time, ``inline_target
        # ._normalize_citation_markers`` at write time). The point is that a
        # reader of the finished prose can see WHICH kind of thing was cited
        # without joining to the citation list.
        f"    cite as [{ordinal}] — or, to say so in the prose, "
        f"{prior_read_ref(ordinal)}",
    ]
    # The note renders ONLY when the body actually carries a defused marker, so
    # a marker-free prior read (the common case) stays byte-identical to the
    # pre-defuse render.
    if "[prior:" in body:
        lines.append(
            "    note: [prior:N] references inside the body are the PRIOR "
            "run's own signal numbering — they resolve to NOTHING in this "
            f"run and are NOT citable; cite this block as [{ordinal}]."
        )
    lines.append(f"    body: {body}")
    return lines


def _render_window_ledger(
    entries: Sequence[Mapping[str, Any]], ordinal: int
) -> list[str]:
    """The WINDOW LEDGER block — ONE ordinal for the whole fortnight.

    A thin adapter, on purpose: the render itself lives in
    :func:`legba.data.analysts.window_ledger.ledger_block_lines` so the unit and
    the composition show the SAME block under their two marker languages. All
    this supplies is the unit's ``[N]`` handle and the OWN-UNIT scope label —
    the label matters, because an own-unit ledger's silence means "this
    dimension recorded nothing", while a desk ledger's silence means "no
    dimension did", and a model told the wrong one mis-reads the absence.
    """
    return ledger_block_lines(entries, f"[{ordinal}]", scope="unit")


def _render_situations(situations: Sequence[Mapping[str, Any]], ordinal: int) -> list[str]:
    """The OPEN SITUATION REGISTER block — ONE ordinal for the whole register
    (it is a single orienting index, not N pieces of evidence).

    H1 — THIS IS THE BLOCK THE REGISTER LOOP RAN THROUGH. At the round's T0 the
    AR escalation desk cited ordinals ``[44][45]`` — past its own 43-signal slice
    count, i.e. these grounding blocks — and wrote "The open-situation register
    records a high intensity (59.1) and recent activity, indicating concrete
    operational impact rather than mere rhetoric" at confidence 0.90, about a
    strike that had ended three weeks earlier. So the desk-facing render carries
    the same two repairs the composition's does, from the SAME source of truth:
    :data:`~legba.data.analysts.window_ledger.REGISTER_SELF_CORROBORATION_RULE`
    at the head of the block, and :func:`
    ~legba.data.analysts.window_ledger._render_evidence_age` on every line. The
    thin-adapter posture of :func:`_render_window_ledger` for the same reason —
    one implementation, two marker languages, no drift.
    """
    lines = [
        f"[{ordinal}] OPEN SITUATION REGISTER — {len(situations)} open frame(s) "
        "on this desk, highest-intensity first (the platform's CLUSTERED "
        "situational picture, NOT operator-vetted ground truth):",
        f"    {REGISTER_SELF_CORROBORATION_RULE}",
    ]
    for s in situations:
        lines.append(
            f"    - {str(s.get('name') or '')[:SITUATION_NAME_CHARS]} :: "
            f"status={s.get('status')} intensity={_fmt(s.get('intensity_score'))} "
            f"events={_fmt_int(s.get('event_count'))} "
            f"last_event_at={s.get('last_event_at') or '(none)'} "
            f"{_render_evidence_age(s)} "
            f"open_for={_fmt_days(s.get('age_days'))}"
        )
    return lines


#: THE OFFER'S ONE INSTRUCTION, closing the OPEN EVENTS block.
#:
#: Everything V3/P2 built expands an ``event:<uuid>`` token a model has already
#: written. This sentence is the only place in the tree that tells a model the
#: token exists — and it has to do three things at once, because a half-stated
#: offer is worse than none: name the EXACT spelling (the expansion's
#: ``_EVENT_BODY_REF_RE`` accepts the bare token and the bracketed forms, so
#: "exactly as shown" is the instruction that cannot go wrong), name both
#: POSITIONS the splice reads (the body and the ``evidence`` list), and say
#: what the token BUYS — the event's member reports as ordinary ``[N]``
#: citations, which is the difference between citing an event and citing the
#: platform's prose about one.
#:
#: The last clause is the load-bearing one. ``events.summary`` is never
#: evidence (DATA_MODEL_V3 §2.5 rules 1-2) and this block never prints it; the
#: sentence says so anyway, because a desk that inferred it could cite the
#: event for what the summary WOULD have said is the rubber-stamp the whole
#: expansion design exists to prevent.
OPEN_EVENTS_CITE_RULE: str = (
    "To cite an event's underlying reports, write its token exactly as "
    "shown — event:<uuid> — inline in the body or as an evidence entry; the "
    "platform expands it into the event's member reports as ordinary [N] "
    "citations. The event's own summary is never evidence."
)


def _render_event_span(event: Mapping[str, Any]) -> str:
    """The event's VALIDITY span, printed from its own two columns.

    ``start..end`` when the occurrence is bounded, the bare start when it is
    still open-ended, and the honest ``(no span)`` when the cluster never
    carried one — never a fabricated "ongoing", which is precisely the
    continuity claim the grounding clause forbids a unit to invent.
    """
    start = event.get("time_start")
    end = event.get("time_end")
    if start and end:
        return f"{start}..{end}"
    if start:
        return str(start)
    if end:
        return f"..{end}"
    return "(no span)"


def _render_open_events(events: Sequence[Mapping[str, Any]], ordinal: int) -> list[str]:
    """The OPEN EVENTS block — ONE ordinal, N citable TOKENS.

    The block is a single orienting index like the register, so it takes ONE
    ``[N]`` handle for the whole list. What makes it different from every other
    grounding block is that its LINES carry their own ref: each prints the
    event's ``event:<uuid>`` token, and THAT is what
    ``inline_target._expand_event_refs`` resolves — into fresh ``[K]`` markers
    over the event's member signals, with the event's raw reports as the text
    the judge grades. Citing ``[N]`` cites this block (the menu); writing the
    token cites the event's REPORTS. Both are honest; they are different acts,
    and the closing rule states which is which.

    NOT PRINTED, deliberately: ``events.summary`` (never evidence, §2.5) and
    ``events.signal_count`` (it read wrong on 244 of 317 — see the reader's
    note; the ``reports=`` number here is derived from the real
    ``signal_event_links`` membership, which is that column's definition,
    so the derivation needs no trust in a rollup). ``sources=`` is
    ``distinct_source_count``, which IS exact live, and it is the number that
    answers "how many independent outlets" — the corroboration question a desk
    should be asking of a cluster.
    """
    lines = [
        f"[{ordinal}] OPEN EVENTS — {len(events)} event(s) linked to this "
        "desk's open situations, most recently updated first (clustered from "
        "reports; NOT operator-vetted):",
    ]
    for e in events:
        age = e.get("updated_age_days")
        age_part = (
            f" updated_age={float(age):.1f}d"
            if isinstance(age, (int, float)) and not isinstance(age, bool)
            else ""
        )
        lines.append(
            f"    - event:{e.get('event_id')} :: "
            f"{str(e.get('title') or '')[:EVENT_TITLE_CHARS]} | "
            f"{_render_event_span(e)} | "
            f"reports={_fmt_int(e.get('report_count'))} "
            f"sources={_fmt_int(e.get('source_count'))} | "
            f"state={e.get('lifecycle_state')} | "
            f"updated_at={e.get('updated_at') or '(unknown)'}{age_part}"
        )
    lines.append(f"    {OPEN_EVENTS_CITE_RULE}")
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
    GROUNDING_WINDOW_LEDGER: _render_window_ledger,
    GROUNDING_SITUATIONS: _render_situations,
    GROUNDING_OPEN_EVENTS: _render_open_events,
    GROUNDING_BASELINE: _render_baselines,
    GROUNDING_QUESTIONS: _render_questions,
    # 7g-2 — a thin adapter, the same posture ``_render_window_ledger`` takes:
    # the render lives beside the block in ``history_grounding`` so the block,
    # its `stale_tense` marker and its citation cannot drift apart. The
    # ``ordinal`` this one receives is the ordinal of its FIRST line.
    GROUNDING_HISTORY: history_block_lines,
}


def block_ordinal_span(row: Mapping[str, Any]) -> int:
    """How many ``[N]`` ordinals ONE grounding row consumes.

    One for every block but HISTORICAL SERIES, which takes one per series LINE
    (see :mod:`legba.data.analysts.history_grounding`). Spelled as a function
    rather than assumed to be 1 so the section renderer reserves the right run
    and the ordinal space stays contiguous and collision-free.
    """
    if row.get(UNIT_GROUNDING_ROW_KEY) != GROUNDING_HISTORY:
        return 1
    payload = row.get(GROUNDING_PAYLOAD_KEY)
    return ordinal_span(payload) if isinstance(payload, (list, tuple)) else 0


def _render_block(row: Mapping[str, Any], ordinal: int) -> list[str]:
    """Render ONE grounding block at ``ordinal`` (``[]`` for an unknown kind).

    For HISTORICAL SERIES, ``ordinal`` is the ordinal of its FIRST line.
    """
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
        # 7g-2: advance by what the block actually CLAIMED, not by one. Every
        # block but HISTORICAL SERIES claims exactly one, so this is the
        # pre-7g-2 arithmetic for all of them.
        ordinal += max(1, block_ordinal_span(row))
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

#: Value of a grounding citation's ``marker_class`` — the discriminator that
#: says this ordinal indexes a DESK GROUNDING block, not a slice row. The
#: signal path carries no ``marker_class`` at all, so absence keeps every
#: pre-existing citation byte-identical and the key is read as "grounding when
#: present" rather than as a field every entry must now supply.
_MARKER_CLASS_GROUNDING: str = "desk_grounding"

#: Value of ``resolves_against`` — names the SET the ordinal is a position in.
#: Spelled as data, not inferred from ``ref_kind``, because inferring it is
#: exactly the step the 08-27 sweep skipped.
_RESOLVES_AGAINST_GROUNDING: str = "data.citations"

_BLOCK_TITLES = {
    GROUNDING_PRIOR_READ: "Prior read (this unit's previous verified read)",
    GROUNDING_WINDOW_LEDGER: "Window ledger (this unit's trailing 14-day record)",
    GROUNDING_SITUATIONS: "Open-situation register",
    GROUNDING_OPEN_EVENTS: "Open events on this desk's open frames",
    GROUNDING_BASELINE: "Desk baseline",
    GROUNDING_QUESTIONS: "Standing open questions",
    GROUNDING_HISTORY: "Historical series held for this desk",
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
    * WINDOW LEDGER / REGISTER / OPEN EVENTS / BASELINE / QUESTIONS — synthetic
      blocks with no single substrate id, so they carry the REAL underlying ids
      (``ledger_finding_ids`` / ``situation_ids`` / ``event_ids`` /
      ``baseline_keys`` / ``question_ids``) and NO ``ref_id``. Minting one so a drill link resolves
      would be a fabricated anchor. The ledger's members ARE real
      ``analyst_outputs`` rows, which makes the temptation sharper and the rule
      no different: the block is N of them, so pointing at any ONE would be a
      lie about what the clause rests on.

    Every shape carries ``evidence_text`` (the rendered block) so the verify pass
    grades a block-backed clause against exactly what the model was shown, and
    ``grounding`` naming WHICH block it resolved into.
    """
    kind = row.get(UNIT_GROUNDING_ROW_KEY)
    if not isinstance(kind, str) or kind not in GROUNDING_RECEIPT_KEYS:
        return None
    if kind == GROUNDING_HISTORY:
        # 7g-2 — HISTORICAL SERIES is not a single-ordinal block and has no
        # single citation: each of its LINES is its own ordinal and its own
        # ``observation`` ref. Returning one citation here would have to pick a
        # line (a lie about what the ordinal points at) or invent a block-level
        # ref_kind that is not in ``GROUNDING_REF_KINDS`` (which the verify
        # path would then score as unresolved). :func:`grounding_citations` is
        # the front door for every block, and the only one for this one.
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
        # 2026-08-29 (task #62) — the row says what its own ordinal MEANS.
        # ``ref_kind`` already carried the answer, but only to a reader who
        # knows ``provenance.kinds.GROUNDING_REF_KINDS``; the 08-27 DQ sweep
        # did not, resolved every marker against ``analyst_traces
        # .input_row_refs`` — a ``uuid[]`` of consumed SUBSTRATE rows that by
        # construction cannot hold a grounding block — and published a 53.6%
        # citation RED that sweep v2 then falsified (0 of 6,556 markers
        # genuinely unresolved).
        #
        # TWO LIVE CONSUMERS DO GET IT WRONG, and these keys do NOT yet fix
        # them, because both discriminate on ``signal_id`` / ``ref_id`` rather
        # than on kind: ``export_api._stored_citations`` strips every grounding
        # entry from an exported document, and the v3 UI's ``citationsModel``
        # handles only ``situation_register`` — rendering the three id-less
        # blocks as "Unresolved citation" chips and mis-typing ``prior_read`` as
        # a signal. These keys are what a repair of either would key on instead.
        "marker_class": _MARKER_CLASS_GROUNDING,
        "resolves_against": _RESOLVES_AGAINST_GROUNDING,
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
    if kind == GROUNDING_WINDOW_LEDGER:
        citation["ledger_finding_ids"] = ledger_finding_ids(entries)
    elif kind == GROUNDING_SITUATIONS:
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
    elif kind == GROUNDING_OPEN_EVENTS:
        # The REAL event uuids, exactly as the register carries its situation
        # ids — and NO ``ref_id``, for the same reason: the block is N events,
        # so pointing at any one would be a lie about what the clause rests on.
        # These are also NOT the expansion's lineage: a finding that writes a
        # token gets the event id into ``derived_from`` through
        # ``_expand_event_refs``. This list is what the BLOCK showed.
        citation["event_ids"] = [
            str(e["event_id"]) for e in entries if e.get("event_id")
        ]
    elif kind == GROUNDING_QUESTIONS:
        citation["question_ids"] = [
            str(e["question_id"]) for e in entries if e.get("question_id")
        ]
    return citation

def grounding_citations(
    row: Mapping[str, Any], start_ordinal: int
) -> list[tuple[int, dict[str, Any]]]:
    """``[(ordinal, citation)]`` for ONE stamped grounding row — the front door.

    Exists because 7g-2's HISTORICAL SERIES block claims one ordinal per LINE
    and therefore resolves to N citations, while every other block claims one
    and resolves to one. Callers iterate the pairs instead of assuming the
    one-block-one-citation shape, and :func:`citation_for_block` is untouched
    (it is still the whole answer for the six single-ordinal blocks, and every
    existing caller and test of it reads the same).

    An empty list is the honest answer for a block we cannot cite — a prior
    read with no resolvable id, or a history row whose observations lost their
    ids — and never a fabricated anchor.
    """
    if row.get(UNIT_GROUNDING_ROW_KEY) == GROUNDING_HISTORY:
        payload = row.get(GROUNDING_PAYLOAD_KEY)
        entries = payload if isinstance(payload, (list, tuple)) else []
        return observation_citations(entries, start_ordinal)
    citation = citation_for_block(row, start_ordinal)
    return [(start_ordinal, citation)] if citation is not None else []


# ---------------------------------------------------------------------------
# The prompt contract — extracted to ``unit_grounding_clause`` (7g-2)
#
# The three clause constants, their fingerprint and the idempotent append moved
# to a sibling VERBATIM when the HISTORICAL SERIES block brought this module to
# the module-size gate's 1,500-line entry threshold. The gate is honoured by
# splitting, never by pinning a new ceiling, and the contract was the clean cut:
# it depends on nothing in this module. Imported back ONE WAY and re-exported,
# so every caller and test is unchanged.
# ---------------------------------------------------------------------------

from .unit_grounding_clause import (  # noqa: E402
    UNIT_GROUNDING_CLAUSE,
    with_grounding_clause,
)




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


# PRIOR-MARKER DEFUSE (V-2 renderer defect, live capture 2026-08-05) — the
# prior read's body is the PREVIOUS cycle's completed prose, written to cite
# ITS OWN ``[N]`` ordinals over ITS OWN evidence slice. Rendered verbatim
# inside THIS run's prompt, those markers land in a prompt whose live
# ``[1]``…``[N]`` space points at DIFFERENT signals (the capture: a ``[15]``
# PRIOR READ block carrying "…is reported in the current 72-hour slice
# [15][1][2][3][4]…[14]" — fourteen stale ordinals plus a recursive
# self-cite). The section header then orders the model to cite the block
# "exactly like a numbered signal", which makes INHERITING a stale marker the
# cheapest compliant move — and a claim that inherits one is genuinely
# mis-cited, so the judge fails it CORRECTLY: the renderer, not the judge, is
# the defect. Mirrors the composition's ``_defuse_child_ref_markers`` (the
# same pollution class, one marker syntax down): the rewrite happens at RENDER
# time only — the stored finding is never touched — and ``[prior:N]`` resolves
# NOWHERE (not the unit ``[N]`` parse, not the composition ``[[ref:N]]``
# parse, not the write-time variant normalizers), while still preserving the
# information that the prior read cited something there.
_PRIOR_MARKER_RE = re.compile(r"\[(\d+)\]")


def _defuse_prior_read_markers(text: str) -> str:
    """Rewrite an embedded prior-read body's ``[N]`` markers to the visually
    close but non-resolvable ``[prior:N]`` form.

    Pure / idempotent (``[prior:N]`` does not match the ``[N]`` pattern);
    marker-free text — the common case — is returned unchanged with no
    allocation beyond the input.
    """
    if not text or "[" not in text:
        return text
    return _PRIOR_MARKER_RE.sub(lambda m: f"[prior:{m.group(1)}]", text)


def _body_excerpt(row: Mapping[str, Any], cap: int) -> str:
    """The row's body excerpt — column first, then ``data.body`` (the same
    fallback chain the composition's ``_row_body_excerpt`` walks), with the
    SAME defuse-before-cap order: capping first could cut a live marker in
    half at the boundary, and the truncation must never re-arm what the
    defuse exists to kill."""
    body = row.get("body")
    if not isinstance(body, str):
        data = row.get("data")
        inner = data.get("body") if isinstance(data, Mapping) else None
        body = inner if isinstance(inner, str) else ""
    return _defuse_prior_read_markers(body)[:cap]
