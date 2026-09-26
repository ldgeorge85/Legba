# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HISTORICAL SERIES grounding block, and the `observation` citation (7g-2 §6).

THE SEVENTH BLOCK, and the first one that is not about this platform. The six
blocks :mod:`legba.data.analysts.unit_grounding` renders are all Legba's own
memory — what this unit said, what this desk is watching, what is normal here.
This one is the WORLD's record: the curated historical series a COLLECTION
loaded once (7g-1's `observations`), rendered so a desk can see what the past
actually measured instead of recalling it.

WHY IT LIVES IN ITS OWN MODULE. ``window_ledger.py`` set the precedent: a block
whose render, rule and citation shape are a cohesive unit lives beside the
block, and ``unit_grounding`` keeps a thin adapter. That keeps one definition
of the block and keeps ``unit_grounding`` under the module-size gate's entry
threshold — the gate is honoured by splitting, never by raising a number.

THE ONE THING THIS BLOCK DOES DIFFERENTLY: **one ordinal per LINE.** Every
other grounding block takes ONE ``[N]`` for the whole block, because it is a
single orienting index and a clause resting on it rests on the whole thing. A
historical series block is not that. It is N independent NUMBERS, each with its
own period, its own record time and its own provider file; a single ordinal
over eleven of them would mean a clause citing ``[12]`` is graded against a
block in which *some* number supports it, which is precisely how a 2016 figure
gets graded as a current claim. So each line takes its own ordinal, each
ordinal resolves to ONE ``observations`` row (``ref_kind='observation'``,
``ref_id`` = that row's real uuid), and the judge grades a cited claim against
that row's own rendered text — period included.

THE `stale_tense` MARKER. Every line ends with, exactly:

    (historical: valid YYYY..YYYY, recorded YYYY-MM)

— :func:`stale_tense_marker`, and the spelling is load-bearing. It is rendered
into the prompt line the model reads, captured verbatim into the citation's
``evidence_text`` (which is what ``verify._grounding_ordinals`` hands the
judge), and printed into the export's endnote. Three surfaces, one string, one
function, so a read that re-asserts a 2016 figure as current is graded against
a row that says 2016 in the same words on all three.

WHAT IS DELIBERATELY NOT HERE. No trend, no delta, no "up from". The block
prints the LATEST period held per series and nothing else: computing a change
would be an analysis this module is not licensed to make, and a desk that wants
one has ``series_history`` as a pack tool. No rounding either — the value is
the provider's own number at the provider's own precision, because a citation
must match the row it points at.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from ...runtime import _observations_read as _obs

logger = logging.getLogger(__name__)

__all__ = [
    "GROUNDING_HISTORY",
    "HISTORY_CITE_RULE",
    "HISTORY_EVIDENCE_CHARS",
    "HISTORY_LOOKBACK_YEARS",
    "HISTORY_SERIES_CAP",
    "HISTORY_SERIES_MAX_CAP",
    "history_block_lines",
    "observation_citation",
    "observation_citations",
    "ordinal_span",
    "observation_evidence_text",
    "read_desk_history",
    "stale_tense_marker",
]


#: Marker value for the HISTORICAL SERIES row — the seventh grounding block.
GROUNDING_HISTORY: str = "history_series"

#: Max series LINES in the block. Eleven series ship in the first collection;
#: twelve is that plus one, because the block is an index of what the platform
#: HOLDS for this desk and a desk that holds twelve series has twelve numbers,
#: not a survey. A holding with more is truncated here rather than allowed to
#: crowd out the evidence slice, and the line count is printed so the model can
#: see that it is reading a bounded view.
HISTORY_SERIES_CAP: int = 12

#: Ceiling on the descriptor-settable ``history_series_limit``. A unit prompt
#: is budgeted for an ORIENTING index; forty one-line numbers is already well
#: past where a reader is reading.
HISTORY_SERIES_MAX_CAP: int = 40

#: How far back in VALID time the block's read reaches, in years, when the
#: caller names no floor. It bounds the scan (and prunes partitions on a
#: holding that reaches further back than the pilot's decade); it is NOT a
#: statement about what is interesting, and nothing rendered depends on it.
HISTORY_LOOKBACK_YEARS: int = 25

#: Cap on ONE line's captured ``evidence_text``. A line is one number with its
#: provenance — an order of magnitude shorter than a six-frame register — and
#: this is sized to hold the whole line plus its block context, never to cut
#: it. LOAD-BEARING: ``verify._marker_to_evidence`` applies no cap of its own
#: to a grounding entry, so a cut here would be a cut in what the judge grades,
#: and the `stale_tense` marker lives at the END of the line.
HISTORY_EVIDENCE_CHARS: int = 900


#: THE BLOCK'S ONE INSTRUCTION. Three obligations, because a half-stated one is
#: worse than none: (1) what these numbers ARE, (2) that a historical figure is
#: never a current claim, and (3) that citing one obliges you to print its
#: period — the obligation the `stale_tense` marker exists to make checkable.
#:
#: The last clause is the load-bearing one. The judge grades a cited claim
#: against the row's own rendered text, and that text states the valid period;
#: a claim that says "GDP growth is 2.8%" while its citation says "valid
#: 2025..2025" is not faithful to what it cited, and this sentence is what
#: makes that a rule the model was told rather than a trap it walked into.
HISTORY_CITE_RULE: str = (
    "These are HISTORICAL observations from a curated holding — numbers a "
    "provider published about a PAST period, not live reporting and not a "
    "measurement of now. A historical figure is never, on its own, a claim "
    "about the present: do not write one as a current value, and do not infer "
    "a trend the block does not show. When you use one, cite its [N] and "
    "state its valid period in the sentence."
)


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


async def read_desk_history(  # type: ignore[no-untyped-def]
    conn,
    *,
    target_id: str,
    limit: int = HISTORY_SERIES_CAP,
    valid_from: date | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """The latest period HELD for each series this DESK has, one row per series.

    Two bounded statements, not one join: the desk is resolved to its
    ``(collection, subject)`` pairs off the LOADED holdings' own ``subjects[]``
    blocks (the holding is what declares which desk it was loaded for), then
    the series are read for that subject. Resolving first is what keeps the
    series read on the leading columns of ``observations_subject_period_idx``
    instead of handing the planner a join to guess at.

    ``[]`` for a desk no loaded holding names — which is not an empty history,
    it is the absence of a holding, and no block renders for it. ``[]`` also
    when nothing is loaded at all. Both are honest absences and neither is an
    error.
    """
    if not target_id:
        return []
    pairs = await _obs.desk_subjects(conn, desk=str(target_id))
    if not pairs:
        return []
    floor = valid_from or date(
        datetime.now(timezone.utc).year - HISTORY_LOOKBACK_YEARS, 1, 1
    )
    out: list[dict[str, Any]] = []
    # One read per (collection, subject) pair. Today that is exactly one; the
    # loop exists because two holdings may each name the same desk, and
    # unioning them in SQL would lose which holding a number came from —
    # which is the first thing a licence question asks.
    for pair in pairs:
        rows = await _obs.latest_per_series(
            conn,
            collection_ids=[pair["collection_id"]],
            subject=pair["subject"],
            valid_from=floor,
            as_of=as_of,
            limit=max(1, min(int(limit), HISTORY_SERIES_MAX_CAP)),
        )
        for row in rows:
            enriched = dict(row)
            enriched["subject_name"] = pair.get("subject_name")
            enriched.setdefault("licence_class", pair.get("licence_class"))
            enriched["collection_version"] = pair.get("collection_version")
            out.append(enriched)
    out.sort(key=lambda r: (str(r.get("series_id") or ""), str(r.get("subject") or "")))
    return out[: max(1, min(int(limit), HISTORY_SERIES_MAX_CAP))]


# ---------------------------------------------------------------------------
# The marker + the render
# ---------------------------------------------------------------------------


def stale_tense_marker(entry: Mapping[str, Any]) -> str:
    """The `stale_tense` marker for one observation, EXACTLY as it renders.

    ``(historical: valid YYYY..YYYY, recorded YYYY-MM)``

    One function, three surfaces: the prompt line, the captured
    ``evidence_text`` the judge grades against, and the export's endnote. A row
    missing either time renders ``????`` in that slot rather than a guessed
    year — a marker that quietly invents a period is worse than no marker,
    because the whole point of it is that the period is checkable.
    """
    valid_from = _year(entry.get("valid_from"))
    valid_to = _year(entry.get("valid_to"))
    recorded = _year_month(entry.get("record_time"))
    return f"(historical: valid {valid_from}..{valid_to}, recorded {recorded})"


def _observation_line(entry: Mapping[str, Any]) -> str:
    """One series line WITHOUT its ordinal — the shared body of the render and
    the captured evidence, so the two cannot say different things."""
    label = (
        _text(entry.get("indicator_name"))
        or _text(entry.get("series_id"))
        or "(unnamed series)"
    )
    subject = _text(entry.get("subject")) or "?"
    subject_name = _text(entry.get("subject_name"))
    who = f"{subject} ({subject_name})" if subject_name else subject
    provider = _text(entry.get("provider")) or "(provider not recorded)"
    value = _value_text(entry)
    unit = _text(entry.get("unit")) or "(unit not recorded)"
    return (
        f"{label} — {provider} · {_text(entry.get('series_id')) or '?'} · "
        f"{who} = {value} {unit} {stale_tense_marker(entry)}"
    )


def history_block_lines(
    entries: Sequence[Mapping[str, Any]], start_ordinal: int
) -> list[str]:
    """The HISTORICAL SERIES block — a header, N numbered lines, the rule.

    Line ``i`` takes ordinal ``start_ordinal + i``, so the block occupies a
    contiguous run of the same flat ``[N]`` space the signals use. That run is
    what :func:`ordinal_span` tells the section renderer to reserve.
    """
    rows = [e for e in entries if isinstance(e, Mapping)]
    if not rows:
        return []
    holding = _text(rows[0].get("collection_id")) or "(unnamed holding)"
    licence = _text(rows[0].get("licence_class")) or "unrecorded"
    lines = [
        f"HISTORICAL SERIES — {len(rows)} curated historical series held for "
        f"this desk (holding {holding}, licence {licence}). Each line is its "
        "OWN [N] and resolves to ONE stored observation with its provider "
        "file; these are numbers about PAST periods, not live reporting:",
    ]
    for offset, entry in enumerate(rows):
        lines.append(f"    [{start_ordinal + offset}] {_observation_line(entry)}")
    lines.append(f"    {HISTORY_CITE_RULE}")
    return lines


def ordinal_span(entries: Sequence[Mapping[str, Any]]) -> int:
    """How many ordinals this block consumes — one per rendered line."""
    return sum(1 for e in entries if isinstance(e, Mapping))


def observation_evidence_text(entry: Mapping[str, Any], ordinal: int) -> str:
    """What the judge is handed for ONE cited observation ordinal.

    The line the model read, plus the two fields that make the number
    CHECKABLE rather than merely stated: the URL it was fetched from and the
    sha256 of the file it was read out of. Deliberately NOT the whole block —
    the ordinal points at one row, and grading a claim against its eleven
    neighbours is exactly what the per-line ordinal exists to prevent.
    """
    parts = [
        f"[{ordinal}] HISTORICAL OBSERVATION — {_observation_line(entry)}",
    ]
    url = _text(entry.get("source_url"))
    if url:
        parts.append(f"    source: {url}")
    sha = _text(entry.get("sha256"))
    if sha:
        parts.append(f"    file sha256: {sha}")
    parts.append(f"    {HISTORY_CITE_RULE}")
    return "\n".join(parts)[:HISTORY_EVIDENCE_CHARS]


# ---------------------------------------------------------------------------
# The citation
# ---------------------------------------------------------------------------

#: Value of ``resolves_against`` on an observation citation — the same
#: in-row target the other grounding kinds name, spelled as data.
_RESOLVES_IN_ROW = "data.citations"

#: The structural mark every DESK GROUNDING citation carries.
_MARKER_CLASS_GROUNDING = "desk_grounding"


def observation_citation(
    entry: Mapping[str, Any], ordinal: int
) -> dict[str, Any] | None:
    """ONE resolved citation for a cited observation, or ``None``.

    UNLIKE the five synthetic blocks this one carries ``ref_id`` — an
    ``observations`` row has a real, single uuid, so pointing at it is the
    honest drill target rather than a fabricated anchor. It carries NO
    ``signal_id``: an observation is not a signals row, has no article behind
    it and no outlet, which is exactly why ``is_grounding_citation`` admits it
    and why the judge grades it on its own captured text.

    ``None`` when the row has no id — a number we cannot point at is never
    rendered as a citable one.
    """
    observation_id = _text(entry.get("observation_id"))
    if not observation_id:
        return None
    evidence = observation_evidence_text(entry, ordinal)
    if not evidence.strip():
        return None
    citation: dict[str, Any] = {
        "marker": f"[{ordinal}]",
        "ordinal": ordinal,
        "ref_kind": "observation",
        "ref_id": observation_id,
        "grounding": GROUNDING_HISTORY,
        "title": _observation_line(entry),
        "evidence_text": evidence,
        "marker_class": _MARKER_CLASS_GROUNDING,
        "resolves_against": _RESOLVES_IN_ROW,
        # The row's OWN fields, carried so every downstream reader — the
        # export's endnote, the Inspector's chip — renders the same number
        # with the same period without re-reading the table. A cited
        # observation that had to be re-resolved to be legible would be
        # illegible in an exported document, which is where most of them are
        # read.
        "source": _text(entry.get("source_url")),
        "observation": {
            "collection_id": _text(entry.get("collection_id")),
            "series_id": _text(entry.get("series_id")),
            "provider": _text(entry.get("provider")),
            "indicator_name": _text(entry.get("indicator_name")),
            "subject": _text(entry.get("subject")),
            "subject_kind": _text(entry.get("subject_kind")),
            "value": _value_text(entry),
            "unit": _text(entry.get("unit")),
            "valid_from": _text(entry.get("valid_from")),
            "valid_to": _text(entry.get("valid_to")),
            "record_time": _text(entry.get("record_time")),
            "source_url": _text(entry.get("source_url")),
            "sha256": _text(entry.get("sha256")),
            "origin_class": _text(entry.get("origin_class")),
            "licence_class": _text(entry.get("licence_class")),
            "stale_tense": stale_tense_marker(entry),
        },
    }
    return citation


def observation_citations(
    entries: Sequence[Mapping[str, Any]], start_ordinal: int
) -> list[tuple[int, dict[str, Any]]]:
    """``[(ordinal, citation)]`` for a whole block — the per-line front door."""
    out: list[tuple[int, dict[str, Any]]] = []
    for offset, entry in enumerate(
        [e for e in entries if isinstance(e, Mapping)]
    ):
        ordinal = start_ordinal + offset
        citation = observation_citation(entry, ordinal)
        if citation is not None:
            out.append((ordinal, citation))
    return out


# ---------------------------------------------------------------------------
# Formatting — absence renders as absence, a number renders as the provider
# wrote it
# ---------------------------------------------------------------------------


def _value_text(entry: Mapping[str, Any]) -> str:
    """The number, at the PROVIDER's precision, or the text value, or ``—``.

    Never rounded: a citation must match the row it points at, and a rounded
    figure in the prompt beside an unrounded one in the citation is a drift a
    judge would score as unfaithful. Never zero-filled: the table CHECKs that
    exactly one of ``value`` / ``value_text`` is present, so a row with
    neither cannot exist — but a projection that lost both renders the absence
    mark rather than a 0.
    """
    display = _text(entry.get("value_display"))
    if display:
        return display
    text = _text(entry.get("value_text"))
    if text:
        return text
    raw = entry.get("value")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return repr(raw)
    return "—"


def _year(value: Any) -> str:
    text = _text(value)
    if text and len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return "????"


def _year_month(value: Any) -> str:
    text = _text(value)
    if text and len(text) >= 7 and text[:4].isdigit() and text[5:7].isdigit():
        return text[:7]
    return "????-??"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
