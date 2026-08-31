# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-2 — THE CARRY: the WINDOW LEDGER, and the memory section it joins.

``planning/FRAME_PROGRAM_2026-08-20.md`` §2, ratified 2026-08-21. FRAME-1 made
the COMPOSITION stop forgetting the fortnight; this train makes the READS stop
forgetting it. The defect is one sentence long — **a 72-hour pipeline forgets
its own window** — and the ATTRIBUTION's H-FRAME table puts the largest single
class of missed majors on it: ≈12 of 23, *outside-72h-and-continuity-dropped*.
The desks printed "mass protest: not_observed" (AR), "State of emergency –
not_observed" (GB), "no new or tightened sanctions" (UA) for a fortnight that
contained exactly those things, because the memory they were handed was ONE
900-char prior read and a single-step diff obligation.

**THE LEDGER.** A per-scope, bounded, citable grounding block of the trailing
fortnight's OWN verified, severity-tagged heads, derived at PROMPT-BUILD time
from rows that already exist: no new table, no new writer, no new analyst kind
— four SELECT-and-a-render idiom, exactly like
``unit_grounding.gather_unit_grounding_rows``. It is the continuity chain made
CUMULATIVE: today's chain carries one prior body, the ledger carries ~20 dated
one-line assertions spanning 14 days. The decisive check (§1.5) is that the
missed majors were ALREADY in the desks' own verified heads — AR's "mass
protests over property bill raise internal instability" (severity elevated,
7 August) is an ``analyst_outputs`` row the internal_stability unit wrote and
its own later runs could not see.

Four properties, each load-bearing:

**SUPERSEDED ROWS ARE INCLUDED, DELIBERATELY.** Supersession is a FRESHNESS
relation, not a retraction, and the ledger's question is "what did this desk
call significant WHEN IT WAS FRESH". Under a head-fold the fortnight's record
consists almost entirely of superseded rows — the AR protest head is one — so
the fold that is right for the basis is exactly wrong here.

**EVERY LINE CARRIES ITS OWN DATE, IN THE HOUSE HUMAN FORM.**
``_tradecraft.NO_INSTRUMENT_READINGS`` forbids the model printing a raw ISO
timestamp and ``_DATED_CLAIM_RULE`` forbids it COMPUTING a date, so the ONLY
way a carried event can be dated in the prose is for the block to SHOW the date
in the form the prose must use ("7 August 2026"). Printing it is then a copy,
which the faithfulness contract always permits. This is what stops the new risk
the carry creates — a fortnight-old claim asserted as fresh.

**FOREIGN MARKERS ARE DEFUSED.** The plan expected titles to be marker-free
one-liners; the live table disagrees (36 finding titles in 30 days carry a
``[N]``-shaped marker). A title rendered verbatim into a prompt whose live
``[1]``…``[N]`` space points at DIFFERENT blocks makes inheriting a stale
ordinal the cheapest compliant move — the V-2 renderer defect, one field over.
:func:`defuse_ledger_markers` kills BOTH marker languages (the unit ``[N]`` and
the composition ``[[ref:N]]``) into the bracket-free ``(ledger ref N)`` form,
which re-arms under no parser in the tree.

**THE CLAUSE IS ONE DEFINITION.** :func:`window_ledger_rule` spells the SAME
three obligations for both layers — a ledger-grounded event is stated WITH ITS
DATE and never as new; standing-state claims are licensed by ledger lines; and
an absence claim the ledger CONTRADICTS is forbidden outright. That last rule is
the direct kill for the "not_observed"-against-the-window class.

WHAT THE LEDGER CANNOT DO, STATED PLAINLY: it carries only what a desk ACCEPTED
while the event was inside its 72h residence. A desk that never wrote a head
(ATTRIBUTION Class 2, the MOU) or was never collected (M23 / ISSP / SONABEL) is
not recoverable by ANY carry — those belong to VOICE and to collection.

---

Also here, moved 2026-08-20 under the module-size gate: the composition's
**CONTINUITY render** — the prior-read block, the open-situation register, and
the assembly that numbers them into one flat ``[[ref:N]]`` space. FRAME-1's own
ceiling note named this seam ("the CONTINUITY render … is FRAME-2's own
surface") and the seam is real rather than convenient: those two blocks plus the
ledger ARE the composition's memory section — one header, one contiguous ordinal
walk, one clause — and the ledger is now the third of the three.
``meta_findings_synthesizer`` imports these names ONE WAY and re-exports them,
so every existing importer (and every test reaching for
``synth._render_continuity_block``) is unchanged.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Sequence

from ..situations.trajectory import DELTA_UNCHANGED_CHECKPOINT
from .composition_window import (
    MAX_TITLE_CHARS,
    _SEVERITY_RANK,
    _as_datetime,
    _defuse_child_ref_markers,
    _row_body_excerpt,
    human_date,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# THE WINDOW LEDGER — bounds
# ---------------------------------------------------------------------------
#
# Every bound here is what keeps the ledger an INDEX of the fortnight rather
# than a second evidence slice. They are sized like the situation register's
# caps, and for the same reason: the block's worst-case footprint — and
# therefore its captured ``evidence_text``, which is what the judge grades
# against — must be a KNOWN size.

LEDGER_WINDOW_HOURS: int = 336
"""The fortnight, in hours. Deliberately the SAME number FRAME-1 gave the
composition's admissibility horizon: the reader's frame is a 14-day country
read (ATTRIBUTION, "both frames scored explicitly"), and a carry shorter than
the horizon the layer above admits would leave a gap between the two memories
that neither could name."""

LEDGER_MIN_SEVERITY: str = "moderate"
"""The severity floor for admission. A desk tags exactly one
``severity:<level>``; ``low`` is the level a unit assigns when it looked and
found nothing worth carrying, so admitting it would fill the fortnight's record
with the very "no change observed" chatter the carry exists to see past.

FRAME-3 (2026-08-21) SHARPENS THIS RATHER THAN BREAKING IT. That tag is now the
dimension's STANDING STATE rather than its slice delta, so the bar reads "what
this desk considered a serious CONDITION during the fortnight" instead of "what
it considered a big MOVE" — which is closer to what a record of a window is for,
and is why the floor does not move. The volume risk it introduces (a standing
war now clears the bar on every run, not only on the runs that moved it) is
already bounded by the one-line-per-(unit, calendar-day) dedupe in
:func:`select_ledger_entries`: a fortnight of a continuing war costs at most
fourteen lines and buys the reader the duration claim the carry exists to
license. The SELECTION is deliberately NOT re-keyed on ``severity_delta``: the
ledger's question is what was significant, not what was newest, and ranking
movement over stakes is the novelty-over-consequence sort the same train
forbids one floor up."""

LEDGER_PER_UNIT_CAP: int = 3
"""Max lines ONE unit may contribute to a DESK-scoped ledger. The plan's number.
It is a fairness bound, not a volume bound: without it the loudest dimension on
a desk (escalation, usually) crowds the other six out of a 20-line block.

DELIBERATELY NOT APPLIED IN OWN-UNIT SCOPE — see
:func:`select_ledger_entries`. A unit reading its OWN fortnight has nobody to
crowd out, and 3 lines is not a fortnight."""

LEDGER_TOTAL_CAP: int = 20
"""Max lines in a DESK-scoped rendered block. The plan's ~20."""

LEDGER_UNIT_TOTAL_CAP: int = 10
"""Max lines in an OWN-UNIT-scoped block, and NOT an arbitrary trim.

The unit layer's captured ``evidence_text`` is bounded twice — by
``unit_grounding.EVIDENCE_TEXT_CHARS`` and again by
``verify._EVIDENCE_GROUNDING_CHARS`` — at 2400 chars, and THAT capture is what
the judge grades a ledger-backed clause against. A block that renders longer
than its capture is a block whose tail the model can read and the judge cannot,
which false-demotes a faithful claim about a line the model was actually shown.
So the line bound is DERIVED from the capture bound rather than chosen: ten
lines at :data:`LEDGER_TITLE_CHARS` plus the header fits inside 2400 with room
(``tests/data_pkg/test_window_ledger.py`` asserts it, so a future edit to the
header or the line format trips a test instead of silently truncating a judge's
evidence). The desk-scoped form has its own, wider capture and keeps the
plan's 20.

What ten lines COSTS at unit scope: with one line per (unit, calendar day) and
severity-first selection, ten lines is ten distinct days of this dimension's
own fortnight, severest first — on the live AR internal_stability desk the whole
14-day record is eight days, so the cap does not bite at all."""

LEDGER_TITLE_CHARS: int = 120
"""Per-line title cap. A unit head's title IS a one-line assertive claim (the
descriptors demand it, and the live median is well under this), so it bounds the
pathological case rather than the normal one — and it is one of the two numbers
:data:`LEDGER_UNIT_TOTAL_CAP` is derived from."""

LEDGER_EVIDENCE_CHARS: int = 4800
"""Cap on a DESK-scoped block's captured ``evidence_text``. Sized to hold the
WHOLE rendered block at its own bounds (:data:`LEDGER_TOTAL_CAP` lines x ~190
chars + the header). LOAD-BEARING for the same reason
``SITUATION_REGISTER_EVIDENCE_CHARS`` is: the composition's
``verify._ordinal_evidence_map`` applies NO cap of its own, so the capture made
HERE is what the judge grades against."""

LEDGER_FETCH_LIMIT: int = 240
"""Row bound on the gather. The busiest live desk yields ~111 admissible rows
over 336h (read-only census, 2026-08-20), so this never bites in practice; it
exists so a pathological desk cannot turn a prompt build into a scan. The SQL
orders by SEVERITY first, so if it ever DID bite it would drop the least severe
row rather than the oldest — the oldest is the whole point."""

LEDGER_VERIFY_FLOOR: float = 0.50
"""The admissibility floor on ``LEAST(confidence, faithfulness)``. The house
number (``meta_findings_synthesizer.DEFAULT_VERIFY_FLOOR`` /
``scorecard_banding.FAITH_FLOOR``): the ledger records what the desk's own
verify pass ACCEPTED, so it must use the same bar every other consumer does."""

WINDOW_LEDGER_REF_KIND: str = "window_ledger"
"""``ref_kind`` stamped on a citation resolving into the ledger block, on BOTH
layers.

DELIBERATELY NOT ``'finding'``: the ledger is a SYNTHETIC multi-row block with
no single substrate id, and ``'finding'`` is the composition sub-claim
discriminator (``verify._uses_subclaim_convention``) — stamping it would route
a whole unit finding to the sub-claim verify floor. The citation carries the
REAL member uuids on ``ledger_finding_ids`` and NO ``ref_id``; minting one so a
drill link resolves would be a fabricated anchor (the same rule
``SITUATION_REGISTER_REF_KIND`` states one floor up). Registered in
``provenance.kinds.GROUNDING_REF_KINDS`` so the verify path grades a
ledger-backed clause against the block's own captured text."""


#: The admissible severity levels, worst-first — every level at or above
#: :data:`LEDGER_MIN_SEVERITY` in the shared ``_SEVERITY_RANK`` vocabulary.
#: DERIVED, never re-listed: a new level added to that ladder joins the ledger
#: automatically instead of silently failing to.
LEDGER_SEVERITIES: tuple[str, ...] = tuple(
    level
    for level, rank in sorted(
        _SEVERITY_RANK.items(), key=lambda kv: kv[1], reverse=True
    )
    if rank >= _SEVERITY_RANK[LEDGER_MIN_SEVERITY]
)


# ---------------------------------------------------------------------------
# THE MARKER DEFUSE — both languages, into a form that re-arms under neither
# ---------------------------------------------------------------------------

#: A unit head's own ``[N]`` signal markers, as they appear INSIDE a title that
#: is about to be rendered in a DIFFERENT run's ordinal space. Same language as
#: ``unit_grounding._PRIOR_MARKER_RE``, different question: that one rewrites a
#: prior read's BODY for the unit layer only; this one rewrites ledger TITLES
#: for both layers, so it cannot borrow the ``[prior:N]`` form (which is a unit
#: -layer vocabulary and would read as a prior-read reference on a composition).
_LEDGER_MARKER_RE = re.compile(r"\[(\d+)\]")


def defuse_ledger_markers(text: str) -> str:
    """Neutralize any citation marker embedded in a ledger line's text.

    Two languages, one pass. ``[[ref:N]]`` (a composition head's marker) goes
    first through the composition's own rewrite, then any remaining ``[N]`` (a
    unit head's marker) becomes ``(ledger ref N)``.

    The output form is BRACKET-FREE on purpose. ``[prior:N]`` was safe enough
    for a unit-only render because it matches neither the unit ``[N]`` parse nor
    the composition ``[[ref:N]]`` parse — but this text is rendered into BOTH
    layers and into the write-time marker normalizers, and the only shape that
    provably re-arms under none of them is one with no brackets at all (the
    ``(child ref N)`` precedent). The information that the head cited something
    there is still preserved.

    Pure / idempotent; marker-free text — the overwhelming common case — is
    returned unchanged with no allocation beyond the input.
    """
    if not text or "[" not in text:
        return text
    return _LEDGER_MARKER_RE.sub(
        lambda m: f"(ledger ref {m.group(1)})", _defuse_child_ref_markers(text)
    )


# ---------------------------------------------------------------------------
# THE GATHER — one query, the house admissibility, superseded rows INCLUDED
# ---------------------------------------------------------------------------

#: SQL severity rank, generated from the SAME ladder the Python selection sorts
#: on so the two can never drift. Ordering in SQL rather than Python is what
#: makes :data:`LEDGER_FETCH_LIMIT` honest: a truncation drops the LEAST severe
#: row, never the oldest.
_SEVERITY_CASE_SQL: str = "CASE f.severity" + "".join(
    f" WHEN '{level}' THEN {_SEVERITY_RANK[level]}" for level in LEDGER_SEVERITIES
) + " ELSE -1 END"


async def read_window_ledger(
    conn,  # type: ignore[no-untyped-def]
    *,
    target_id: str,
    analyst_ids: Sequence[str] | None = None,
    window_hours: int = LEDGER_WINDOW_HOURS,
    verify_floor: float = LEDGER_VERIFY_FLOOR,
    limit: int = LEDGER_FETCH_LIMIT,
) -> list[dict[str, Any]]:
    """The trailing-fortnight record of what this scope ACCEPTED as significant.

    ``target_id`` is the desk. ``analyst_ids`` narrows to OWN-UNIT scope (the
    unit layer passes its own id); ``None`` is DESK scope (the country
    composition), which is the only place cross-dimension synthesis belongs — a
    unit answers one bounded question and handing it its siblings' dimensions
    invites the scope creep its descriptor forbids (§2.2).

    The admissibility is the house pattern verbatim — ``kind='finding'``, the
    INNER lateral to the latest ``Faithfulness verify%`` critique (so an
    UNVERIFIED head is inadmissible, not merely low), the ``LEAST(confidence,
    faithfulness) >= floor`` fold, the coerce-fallback tag drop, and the meta
    exclusion (the ledger records UNIT heads; a composition's own prior head is
    the PRIOR READ block's job) — with ONE deliberate omission: **no
    ``superseded_by IS NULL``**. See the module note.

    Returns compact, JSON-safe entries, newest-severest first. A row with no
    resolvable id or an empty title is SKIPPED rather than padded: the ledger
    may only state assertions that actually exist. An empty result is the honest
    absence — no block, no receipt, no ordinal consumed.
    """
    if not target_id:
        return []
    if analyst_ids is not None and not analyst_ids:
        # An explicitly EMPTY scope reads nothing rather than silently widening
        # to the desk (the ``read_other_analyst_findings`` empty-set contract).
        return []

    params: list[Any] = [
        str(target_id),
        int(window_hours),
        float(verify_floor),
        list(LEDGER_SEVERITIES),
    ]
    where = [
        "f.kind = 'finding'",
        "f.target_id = $1",
        "f.produced_at > NOW() - make_interval(hours => $2)",
        "LEAST(f.confidence, v.faithfulness_score) >= $3",
        "f.severity = ANY($4::TEXT[])",
        "(f.data -> 'data' ->> 'meta') IS DISTINCT FROM 'true'",
        "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE",
    ]
    if analyst_ids is not None:
        params.append([str(a) for a in analyst_ids])
        where.append(f"f.analyst_id = ANY(${len(params)}::TEXT[])")

    sql = f"""
    SELECT f.id, f.analyst_id, f.title, f.severity, f.produced_at,
           LEAST(f.confidence, v.faithfulness_score) AS effective_confidence
      FROM analyst_outputs f
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE {' AND '.join(where)}
     ORDER BY {_SEVERITY_CASE_SQL} DESC, f.produced_at DESC, f.id DESC
     LIMIT {int(limit)}
    """
    rows = await conn.fetch(sql, *params)
    out: list[dict[str, Any]] = []
    for raw in rows:
        entry = ledger_entry(dict(raw))
        if entry is not None:
            out.append(entry)
    return out


def ledger_entry(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """One raw head row as a compact ledger entry, or ``None`` to skip it.

    ``None`` for a row with no id, no usable title, no parsable timestamp, or no
    ADMISSIBLE severity. The first three because the ledger's whole value is
    that every line is dated and attributable, and a line missing either is not
    worth the ordinal it would consume. The fourth because the block's header
    STATES that every line was tagged at or above :data:`LEDGER_MIN_SEVERITY` —
    rendering an ungraded line beneath that sentence would make the block lie
    about itself. The gather's own ``f.severity = ANY(...)`` predicate means this
    leg never fires in production; it is the belt to that suspenders, and the
    guarantee for any other caller that builds entries by hand.
    """
    fid = row.get("id")
    title = row.get("title")
    when = _as_datetime(row.get("produced_at"))
    if fid is None or not isinstance(title, str) or not title.strip() or when is None:
        return None
    level = str(row.get("severity") or "").strip().lower()
    if level not in LEDGER_SEVERITIES:
        return None
    return {
        "finding_id": str(fid),
        "analyst_id": str(row.get("analyst_id") or "(unknown)"),
        "severity": level,
        "severity_rank": _SEVERITY_RANK[level],
        "title": defuse_ledger_markers(title.strip())[:LEDGER_TITLE_CHARS],
        "read_date": human_date(when),
        "day": when.date().isoformat(),
        "produced_at": when.isoformat(),
    }


# ---------------------------------------------------------------------------
# THE SELECTION — pure, so the bounds are testable without a database
# ---------------------------------------------------------------------------


def select_ledger_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    per_unit_cap: int | None = LEDGER_PER_UNIT_CAP,
    total_cap: int = LEDGER_TOTAL_CAP,
) -> list[Mapping[str, Any]]:
    """Bound the fortnight to a block, then put it back in time order.

    Three steps, in this order and for these reasons:

    1. **DEDUPE to one line per (unit, calendar day).** A unit fires twice a day
       and re-asserts the same standing state each time; without this the AR
       protest story alone would spend eight lines saying it eight ways. The
       SEVEREST line for a unit-day wins (input order is already
       severity-then-recency).
    2. **CAP** — per unit, then overall, keeping the severest. ``per_unit_cap``
       ``None`` disables the per-unit bound: in OWN-UNIT scope there is exactly
       one unit and nobody for it to crowd out, so the plan's 3-line fairness
       bound would leave a unit reading a three-line "fortnight". The
       unit-day dedupe already bounds that scope at one line per day.
    3. **RE-ORDER CHRONOLOGICALLY, OLDEST FIRST.** The house convention for the
       register / questions / trajectory blocks is newest-first, and this block
       deliberately breaks it: those are INDEXES of a current state, this one is
       a RECORD OF A WINDOW, and a record of a window is read forward. The
       header says so in as many words, so the order can never be mistaken for
       a ranking.

    PURE: a new list of the SAME entry objects, never copies and never mutations
    — the caller stores the result as a row payload and nothing downstream
    writes to it.
    """
    ranked = sorted(
        entries,
        key=lambda e: (
            _rank_of(e),
            str(e.get("produced_at") or ""),
            str(e.get("finding_id") or ""),
        ),
        reverse=True,
    )
    seen_days: set[tuple[str, str]] = set()
    per_unit: dict[str, int] = {}
    kept: list[Mapping[str, Any]] = []
    for entry in ranked:
        unit = str(entry.get("analyst_id") or "")
        day_key = (unit, str(entry.get("day") or ""))
        if day_key in seen_days:
            continue
        if per_unit_cap is not None and per_unit.get(unit, 0) >= per_unit_cap:
            continue
        seen_days.add(day_key)
        per_unit[unit] = per_unit.get(unit, 0) + 1
        kept.append(entry)
        if len(kept) >= total_cap:
            break
    return sorted(
        kept,
        key=lambda e: (str(e.get("produced_at") or ""), str(e.get("finding_id") or "")),
    )


def _rank_of(entry: Mapping[str, Any]) -> int:
    raw = entry.get("severity_rank")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return _SEVERITY_RANK.get(str(entry.get("severity") or "").lower(), -1)


# ---------------------------------------------------------------------------
# THE RENDER — one block, one ordinal, both layers
# ---------------------------------------------------------------------------


def ledger_block_lines(
    entries: Sequence[Mapping[str, Any]], handle: str, *, scope: str = "desk"
) -> list[str]:
    """The rendered ledger block, as lines, under the caller's ``handle``.

    ``handle`` is the ALREADY-FORMATTED citation handle for the calling layer —
    ``"[7]"`` for a unit, ``"[[ref:7]]"`` for a composition. Passing the
    formatted string rather than an ordinal is what lets ONE render serve two
    marker languages without this module knowing which layer it is in.

    ``scope`` names what the block covers, because the two scopes make DIFFERENT
    claims: an own-unit ledger is "everything THIS dimension called significant",
    a desk ledger is "everything ANY dimension on this desk did", and a model
    told the wrong one would either over- or under-read the block's silence.

    Returns ``[]`` for an empty selection — an empty ledger is never rendered as
    an empty block (see the module's absent-by-default posture).
    """
    if not entries:
        return []
    what = (
        "THIS UNIT's own"
        if scope == "unit"
        else "THIS DESK's own (every dimension's)"
    )
    lines = [
        f"{handle} WINDOW LEDGER — {what} verified reads of the trailing "
        f"{LEDGER_WINDOW_HOURS // 24} days that were tagged {LEDGER_MIN_SEVERITY} "
        f"severity or above ({len(entries)} line(s), OLDEST FIRST — this is a "
        "record of the window in time order, NOT a ranking). This block is the "
        "licensed record of what was already established in this window; it is "
        "NOT this cycle's evidence and nothing in it is new.",
    ]
    for e in entries:
        lines.append(
            f"    - {e.get('read_date') or '(undated)'} :: {e.get('analyst_id')} "
            f"[{e.get('severity')}] {e.get('title')}"
        )
    return lines


def ledger_evidence_text(
    entries: Sequence[Mapping[str, Any]], handle: str, *, scope: str = "desk"
) -> str:
    """Exactly the bytes the model was shown, bounded by
    :data:`LEDGER_EVIDENCE_CHARS` — this is what the judge grades against."""
    return "\n".join(ledger_block_lines(entries, handle, scope=scope))[
        :LEDGER_EVIDENCE_CHARS
    ]


def ledger_finding_ids(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """The REAL member finding uuids behind the block — the honest handle for a
    synthetic block with no substrate id of its own."""
    return [str(e["finding_id"]) for e in entries if e.get("finding_id")]


# ---------------------------------------------------------------------------
# THE CLAUSE — one definition, two marker languages
# ---------------------------------------------------------------------------


def window_ledger_rule(handle_form: str) -> str:
    """The ledger's prompt obligation, worded for one layer's handle syntax.

    A function rather than two constants for the reason ``_tradecraft.as_of_rule``
    is one: the obligation is identical at both layers and only the citation
    handle differs, and two hand-maintained copies of a contract whose entire
    purpose is that every read states it identically would drift on the first
    edit.

    The three rules, and what each kills:

      * **DATE IT, AND NEVER AS NEW.** The carry's own risk is a fortnight-old
        claim narrated as this cycle's news. Every line prints its date; the
        prose must copy it.
      * **STANDING STATE IS LICENSED HERE.** The slice shows a 72h delta, so
        "the strike is in its third week" had no licensed source and was
        correctly refused by the continuity clause. Now it has one.
      * **AN ABSENCE THE LEDGER CONTRADICTS IS FORBIDDEN.** The direct kill for
        the ``not_observed``-against-the-window class: 'no mass protest this
        window' cannot be written while the block carries a mass-protest line.
        The honest alternatives are named so the rule is followable — cite the
        line, or scope the absence to the slice IN WORDS.
    """
    return (
        "WINDOW LEDGER (what this desk already ESTABLISHED this fortnight). A "
        f"WINDOW LEDGER block may be shown with its own {handle_form} handle: "
        "dated one-line records of the verified, severity-tagged reads this "
        "scope itself produced over the trailing 14 days, oldest first. It is "
        "the ONLY licensed source for claims at WINDOW scale — how long "
        "something has been running, whether a thing recurred, what the "
        "standing state of this dimension is — none of which your 72-hour slice "
        "can show. When it is shown you MUST: (1) treat every ledger line as "
        "ALREADY ESTABLISHED, never as news — when you carry one into your "
        "read, state it WITH THE DATE PRINTED ON THAT LINE and cite the block "
        f"({handle_form}), e.g. 'mass protests recorded on 7 August "
        f"{handle_form} have since subsided'; NEVER write a ledger line as "
        "though it happened in this slice, and NEVER print a date that is not "
        "on a line you were shown; (2) you MAY assert a standing state or a "
        "duration ONLY where the ledger lines support it, and you MUST cite "
        "them when you do; (3) NEVER write that something was absent, not "
        "observed, or did not occur in this WINDOW when a ledger line records "
        "it — if your current slice simply does not show it, say THAT in words "
        "('no new development since the read of 7 August "
        f"{handle_form}' / 'nothing on this in the last 72 hours'), which is a "
        "different and honest statement. An absence contradicted by your own "
        "ledger is the single worst error you can make here. If NO WINDOW "
        "LEDGER block is shown, make no claim about the fortnight at all."
    )


# ===========================================================================
# THE COMPOSITION'S CONTINUITY SECTION
# (moved from ``meta_findings_synthesizer`` 2026-08-20 under the module-size
# gate — the seam FRAME-1's ceiling note named. Behavior unchanged except for
# the third block the ledger adds.)
# ===========================================================================
#
# CONTINUITY (Phase 1, 2026-07-31) — TEMPORAL CONTINUITY VIA CITABLE REFS.
#
# The units are deliberately STATELESS slice-of-now analyzers, and so was every
# composition above them: each cycle re-derived the picture from scratch, so a
# new event could never read as "this ESCALATES the situation we were already
# watching". Phase 1 gives the compositions (country / region / thematic) and the
# WORLD read a memory — WITHOUT a new kind, a new schema, or a trajectory ledger
# (that is Phase 2).
#
# TWO HARD LESSONS BIND THE DESIGN:
#
#   1. The world_context RAG ROLLBACK. An UNCITED prior leaking into cited
#      analysis is this platform's NAMED failure mode. So continuity context
#      enters ONLY as CITABLE REFS: each block gets its own ``[[ref:N]]`` ordinal
#      in the SAME flat resolution space as the basis + periphery sub-claims, and
#      the prompt requires the model to cite it exactly like any other evidence.
#   2. TEMPORAL COLLAPSE. The prior read carries its OWN ``produced_at``, each
#      situation its OWN ``last_event_at`` / age, and each ledger line its OWN
#      date, all rendered INTO the block; the clause anchors every temporal
#      statement on those dates and forbids anchoring on run/fetch time.
#
# THREE BLOCKS, all bounded, all absent-by-default: PRIOR READ (one step back),
# WINDOW LEDGER (FRAME-2 — the fortnight), OPEN-SITUATION REGISTER (what is
# open). NOT wired into the LEGACY global meta, which keeps the standing
# "legacy read byte-for-byte" discipline every branch honors.

CONTINUITY_ROW_KEY: str = "_continuity"
"""Row marker READ_SLICE stamps on a continuity row so the DB-less ``_run`` can
partition it out of the BASIS/PERIPHERY tiers on DATA, never on env. Value is
:data:`CONTINUITY_PRIOR`, :data:`CONTINUITY_SITUATIONS` or
:data:`CONTINUITY_WINDOW_LEDGER`."""

CONTINUITY_PRIOR: str = "prior_read"
"""Marker value: the row IS this target's previous verified composition head."""

CONTINUITY_SITUATIONS: str = "situations_register"
"""Marker value: the row is the synthetic open-situation REGISTER block."""

CONTINUITY_WINDOW_LEDGER: str = "window_ledger"
"""Marker value: the row is the synthetic WINDOW LEDGER block (FRAME-2)."""

CONTINUITY_SITUATIONS_ROW_KEY: str = "_situations"
"""Key on the synthetic register row carrying its bounded situation dicts."""

CONTINUITY_LEDGER_ROW_KEY: str = "_window_ledger"
"""Key on the synthetic ledger row carrying its bounded ledger entries."""

CONTINUITY_PRIOR_RECEIPT: str = "continuity_prior_ref"
"""Receipt key: 1 when a prior-read ref entered the slice, else 0."""

CONTINUITY_SITUATIONS_RECEIPT: str = "continuity_situations_ref"
"""Receipt key: 1 when the situation-register ref entered the slice, else 0."""

CONTINUITY_LEDGER_RECEIPT: str = "continuity_window_ledger_ref"
"""Receipt key: 1 when the WINDOW LEDGER ref entered the slice, else 0. A
silently-absent carry is exactly what this receipt exists to make visible — the
§2.2 requirement, and the difference between "this desk had no fortnight" and
"the gather failed and nobody noticed"."""

CONTINUITY_PRIOR_LOOKBACK_HOURS: int = 168
"""How far back the prior-read lookup reaches (7 days) — INDEPENDENT of the
slice window, because "the previous read" is a per-head fact, not a per-slice
one: a composition on a 24h window whose last cycle was skipped still has a
prior read worth diffing against. Bounded so a months-old head is never dressed
up as "the prior read"; the block always shows its produced_at + age so the model
(and the verify pass) can see exactly how stale the memory is."""

CONTINUITY_PRIOR_BODY_CHARS: int = 900
"""Body excerpt cap for the prior-read block. Wider than
``PERIPHERY_BODY_CHARS`` (the diff is the whole point — a truncated prior read
produces a fabricated-looking "change") but well under the basis
``MAX_BODY_CHARS`` * the slice size, so the block cannot dominate the turn."""

SITUATION_REGISTER_CAP: int = 8
"""Max open situations rendered in the register — worst-first (intensity, then
recency). A register is a compact ORIENTING index, not a second slice."""

SITUATION_REGISTER_NAME_CHARS: int = 120
"""Per-frame name cap in the register. Tighter than :data:`MAX_TITLE_CHARS` — a
situation name is a short frame LABEL, and the cap is what bounds the register's
worst-case footprint (and therefore its captured evidence text) to a known
size."""

SITUATION_REGISTER_TRAJECTORY_DEPTH: int = 3
"""How many SIGNIFICANT dated deltas per frame the register carries.

FRAME-2 (§2.3.1) changed what these three ARE, not how many. They used to be the
newest three ledger rows of ANY kind, and the tracker writes multiple
``unchanged_checkpoint`` rows a day — so at the round's T0 the three rendered AR
lines were three same-day "unchanged_checkpoint: no coercive economic measures
observed", while the frame's three real dated August escalations sat below the
render cut. The record of movement existed; the render selected the noise. They
are now the newest three SIGNIFICANT deltas, with the latest checkpoint kept as
one honest tail line (``situations.trajectory.read_trajectories``)."""

SITUATION_REGISTER_WHY_CHARS: int = 180
"""Per-delta ``why`` cap in the register. A ledger ``why`` is already one
sentence; this bounds the pathological case so the register's worst-case
footprint (and therefore its captured evidence text) stays a known size."""

SITUATION_REGISTER_EVIDENCE_CHARS: int = 5000
"""Cap on the register's captured ``evidence_text``. Sized to hold the WHOLE
rendered register at its own bounds. Load-bearing: ``verify._ordinal_evidence_map``
applies NO cap of its own, so the synth-side capture IS what the judge grades
against — a 600-char cut would silently hide the tail of the register and
false-demote a faithful claim about a frame the model was actually shown."""

SITUATION_REGISTER_REF_KIND: str = "situation_register"
"""``ref_kind`` stamped on a citation that resolves into the register block.

DELIBERATELY NOT ``'finding'``: the register is not an ``analyst_outputs`` row,
and it has NO single substrate id, so the citation carries NO ``ref_id`` — it
carries ``situation_ids`` (the REAL ``situations`` uuids behind the block)
instead. Fabricating a ``ref_id`` (e.g. the top situation's) to make a drill
link resolve would be exactly the dishonesty this platform refuses."""

CONTINUITY_CITATION_KEY: str = "continuity"
"""Citation field naming WHICH continuity ref a citation resolved into
(:data:`CONTINUITY_PRIOR` / :data:`CONTINUITY_SITUATIONS` /
:data:`CONTINUITY_WINDOW_LEDGER`). Additive — a basis or periphery citation
never carries it."""


def _iso_text(value: Any) -> str | None:
    """A JSON-safe timestamp string: ISO-8601 for a datetime, the string itself
    when the driver already handed one back, ``None`` otherwise. Wider than a
    datetime-only coercion because a register entry that silently lost its
    ``last_event_at`` to a str/datetime mismatch would render as ``(none)`` — an
    invented absence, which is the one thing the register must never report."""
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


def _continuity_selection(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Split the marked continuity rows into ``(prior_row, register_row)``.

    FIRST-wins per kind (READ_SLICE emits at most one of each; a duplicate would
    be a bug, and taking the first keeps the ordinal space deterministic rather
    than silently renumbering). Either may be ``None`` — both refs are
    independently optional. The WINDOW LEDGER row is selected by
    :func:`_ledger_selection` rather than widening this tuple: every caller of
    this function (and every test reaching for it) unpacks exactly two values,
    and a third block is not worth breaking that shape over.
    """
    prior: Mapping[str, Any] | None = None
    register: Mapping[str, Any] | None = None
    for row in rows:
        kind = row.get(CONTINUITY_ROW_KEY)
        if kind == CONTINUITY_PRIOR and prior is None:
            prior = row
        elif kind == CONTINUITY_SITUATIONS and register is None:
            register = row
    return prior, register


def _ledger_selection(
    rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """The marked WINDOW LEDGER row, or ``None``. FIRST-wins, same contract as
    :func:`_continuity_selection`."""
    for row in rows:
        if row.get(CONTINUITY_ROW_KEY) == CONTINUITY_WINDOW_LEDGER:
            return row
    return None


def _register_situations(row: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The situation dicts carried on a register row (``[]`` when absent)."""
    if row is None:
        return []
    raw = row.get(CONTINUITY_SITUATIONS_ROW_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    return [s for s in raw if isinstance(s, Mapping)]


def _ledger_entries(row: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The ledger entries carried on a ledger row (``[]`` when absent)."""
    if row is None:
        return []
    raw = row.get(CONTINUITY_LEDGER_ROW_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    return [e for e in raw if isinstance(e, Mapping)]


def _render_prior_read_lines(row: Mapping[str, Any], ordinal: int) -> list[str]:
    """The PRIOR READ sub-block — one citable ordinal, dated by its OWN clock."""
    title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
    produced_at = _iso_text(row.get("produced_at")) or "(unknown)"
    age = _as_float(row.get("age_hours"))
    age_part = f" age={age:.1f}h" if age is not None else ""
    eff = _as_float(row.get("effective_confidence"))
    eff_part = f" effective_confidence={eff:.2f}" if eff is not None else ""
    analyst_id = str(row.get("analyst_id") or "(unknown)")
    body = _row_body_excerpt(row, CONTINUITY_PRIOR_BODY_CHARS)
    return [
        f"[[ref:{ordinal}]] PRIOR READ (this target's previous verified read): {title}",
        f"      analyst_id={analyst_id} produced_at={produced_at}{age_part}{eff_part}",
        f"      body: {body}",
    ]


#: H1 — how old a frame's last CORROBORATING evidence may be before the register
#: labels it STALE. The desk's own 72-hour slice: the admissibility horizon
#: inside which a read is licensed to treat something as current. It matches
#: ``situation_clustering._CORROBORATION_ACTIVE_MAX_DAYS`` deliberately — the
#: same horizon that demotes ``status`` to ``dormant`` is the one that labels the
#: line, so a reader never sees a frame the writer called dormant printed without
#: a reason beside it.
REGISTER_STALE_AFTER_DAYS: float = 3.0

#: The label a stale frame carries, and the phrase the anti-self-corroboration
#: rule names. Rendered in caps because it must survive being skimmed.
REGISTER_STALE_LABEL: str = "STALE-NO-NEW-EVIDENCE"

#: What the register says when the trajectory ledger has never moved a frame.
#: NOT "0d", NOT ``last_event_at`` — see the H1 note in
#: :func:`_render_evidence_age`. "We have never corroborated this" and "we
#: corroborated it just now" must never render alike.
REGISTER_NO_CORROBORATION: str = "NEVER-CORROBORATED"

#: THE ANTI-SELF-CORROBORATION RULE (H1, CORRECTNESS-R2 §1 recommendation 1).
#:
#: The register is ``[N]``-citable, which is what makes the loop possible: the
#: standing "no claim may rest on orientation alone" clause does not reach a
#: block that carries an ordinal. So the rule is stated where the block is
#: rendered, in the block itself, immediately above the frames it governs.
#:
#: At the round's T0 the AR escalation desk wrote "The open-situation register
#: records a high intensity (59.1) and recent activity, indicating concrete
#: operational impact rather than mere rhetoric" at confidence 0.90, and the
#: composition's BLUF said the register "confirms the strike remains the active
#: driver". Both sentences are the product citing its own bookkeeping as
#: evidence about the world. This paragraph is what forbids them.
REGISTER_SELF_CORROBORATION_RULE: str = (
    "HOW TO READ THIS BLOCK. Every number on a frame line is the PRODUCT'S OWN "
    "BOOKKEEPING, not a report from the world. `intensity` and `events` count "
    "how much THIS SYSTEM has written about the frame — a desk that looks and "
    "sees nothing still writes, so a high count can mean nothing has happened. "
    "`last_event_at` is the last time any desk WROTE about the frame; "
    "`last_corroborated_at` is the last time NEW EVIDENCE actually moved it, "
    "and it is the only date here that says anything about the world. "
    "THEREFORE: this register may orient you, and you may cite it for what the "
    f"SYSTEM currently holds, but it may NEVER be your evidence that an event "
    f"is ongoing, current, confirmed, or still the active driver. Do not write "
    f"that the register 'confirms' or 'corroborates' anything about the world. "
    f"A frame marked {REGISTER_STALE_LABEL} has had NO new corroborating "
    "evidence inside the current read's horizon, and one marked "
    f"{REGISTER_NO_CORROBORATION} has NEVER had any: treat both as standing "
    "questions, not live events, and say so in those words if you mention them "
    "at all. Any claim that an event is still happening must rest on a dated "
    "item from the evidence slice or the window ledger."
)


def is_stale_frame(situation: Mapping[str, Any]) -> bool:
    """True when a frame's last KNOWN CONTACT WITH THE WORLD is past the horizon.

    ``evidence_age_days`` is the age of the frame's evidence anchor: its last
    significant trajectory delta, or — for a frame the ledger has never moved —
    the frame's own OPENING. So a never-corroborated frame is stale exactly when
    it is OLD, which is the case the 2026-08-27 DQ sweep found fleet-wide (24 of
    50 non-closed frames, 22 with no ledger rows at all, rendering ``active`` at
    intensity up to 60.9; the worst 73 days old with zero events ever).
    """
    age = _as_float(situation.get("evidence_age_days"))
    return age is not None and age > REGISTER_STALE_AFTER_DAYS


def _render_evidence_age(situation: Mapping[str, Any]) -> str:
    """The EVIDENCE-age fragment every register line carries.

    ``last_corroborated_at=<iso> evidence_age=<N>d`` plus the
    :data:`REGISTER_STALE_LABEL` past the horizon;
    :data:`REGISTER_NO_CORROBORATION` in place of the date when the trajectory
    ledger has never moved the frame — the age is still printed, and so is the
    label, because a frame nothing has ever corroborated is not a fresher one.

    This fragment is the load-bearing half of the H1 render repair. The register
    already printed a date — ``last_event_at`` — and a desk read it as a world
    date, writing "the latest event timestamp on 20 August 2026" into prose about
    a strike that had ended on 5 August. The column was telling the truth; it was
    answering a different question. Printing the evidence clock BESIDE the
    bookkeeping clock, on every line, is what makes the two impossible to
    confuse — and printing NEVER as a word rather than as a missing field is what
    stops the fleet's 24 never-corroborated frames reading as merely quiet ones.
    """
    when = situation.get("last_corroborated_at") or REGISTER_NO_CORROBORATION
    age = _as_float(situation.get("evidence_age_days"))
    age_part = f" evidence_age={age:.1f}d" if age is not None else ""
    stale_part = f" {REGISTER_STALE_LABEL}" if is_stale_frame(situation) else ""
    return f"last_corroborated_at={when}{age_part}{stale_part}"


def _render_situation_register_lines(
    situations: Sequence[Mapping[str, Any]], ordinal: int
) -> list[str]:
    """The OPEN SITUATION REGISTER sub-block — ONE citable ordinal for the whole
    register (it is a single orienting index, not N pieces of evidence).

    H1: every frame line now carries its EVIDENCE age beside its bookkeeping age
    (:func:`_render_evidence_age`), and the block leads with
    :data:`REGISTER_SELF_CORROBORATION_RULE` — the rule that forbids resting a
    world claim on the register's own numbers.

    REGISTER-1c: a trailing ``unchanged_checkpoint`` renders its DATE and its
    delta name and nothing else. H1 damped the loop's NUMBER and left its
    SENTENCE — see the comment on the checkpoint branch below.
    """
    lines = [
        f"[[ref:{ordinal}]] OPEN SITUATION REGISTER "
        f"({len(situations)} open frame(s) in scope, highest-intensity first):",
        f"      {REGISTER_SELF_CORROBORATION_RULE}",
    ]
    for s in situations:
        intensity = s.get("intensity_score")
        intensity_txt = (
            f"{float(intensity):.2f}" if isinstance(intensity, (int, float)) else "n/a"
        )
        events = s.get("event_count")
        events_txt = str(events) if isinstance(events, int) else "n/a"
        age = s.get("age_days")
        age_txt = f"{float(age):.1f}d" if isinstance(age, (int, float)) else "n/a"
        state = s.get("trajectory_state")
        lines.append(
            f"      - {s.get('name')} :: status={s.get('status')} "
            f"intensity={intensity_txt} events={events_txt} "
            f"last_event_at={s.get('last_event_at') or '(none)'} "
            f"{_render_evidence_age(s)} open_for={age_txt}"
            + (f" trajectory={state}" if state else "")
        )
        # CONTINUITY P2 — the frame's own DATED deltas. FRAME-2 (§2.3.1): these
        # are now the SIGNIFICANT deltas first (escalates / de_escalates /
        # broadens), with at most one trailing checkpoint line, so the record of
        # MOVEMENT is what the model reads instead of the day's checkpoint
        # chatter that used to fill all three slots.
        for delta in s.get("trajectory") or ():
            if not isinstance(delta, Mapping):
                continue
            # FRAME-2: the human date rides BESIDE the machine one, for exactly
            # the reason FRAME-1 put it beside a head's ``produced_at``. This
            # clause tells the model to date a trajectory claim from the line it
            # was shown while ``_DATED_CLAIM_RULE`` forbids it COMPUTING a date
            # — a line printing only ``2026-08-20T13:31:42+00:00`` asks for a
            # date the same prompt also forbids. Showing the form the prose must
            # use makes printing it a COPY, which is always permitted.
            when = delta.get("occurred_at")
            human = human_date(when)
            when_txt = f"{human} ({when})" if human else (when or "(undated)")
            kind = delta.get("delta")
            # REGISTER-1c (2026-08-29) — A CHECKPOINT RENDERS ITS DATE, NEVER ITS
            # PROSE.
            #
            # H1 exempted ``unchanged_checkpoint`` from the evidence requirement
            # on the ground that "a checkpoint asserts nothing about the world
            # (it is the one delta the ledger writes without evidence), so
            # showing an old one cannot mislead the way an old escalation
            # could". That is true of the delta TYPE and false of the ROW: the
            # row also carries ``why`` — free LLM prose written under a prompt
            # whose own instruction is "situations mostly continue" — and this
            # render printed it verbatim into nine desk prompts.
            #
            # MEASURED at the 2026-08-29 premise review: of 1,095
            # ``unchanged_checkpoint`` rows, 375 (34%) carry currency language
            # (remains / continues / ongoing / persists / still) and 53 carry
            # CORROBORATION language (confirms / corroborates / verifies) — in
            # the one row class the design exempted because it "asserts
            # nothing". Of the checkpoints actually RENDERED (one per frame,
            # UNWINDOWED on purpose, mean age 3.7d, max 17.3d), HALF asserted
            # currency or confirmation. The live example is the whole defect in
            # one line: the AR frame's rendered checkpoint, dated 23 August,
            # read "No material change was observed; the maritime pilot strike
            # continues with…" — 24 days after the strike ended, in a prompt
            # that also rendered ``trajectory=escalating``.
            #
            # So the fix is the one the exemption's own logic implies. A
            # checkpoint's FACT is "we looked on <date> and nothing had changed";
            # that fact is fully carried by the date and the delta name, which
            # is why the checkpoint line was kept in the first place. Its ``why``
            # adds no fact and is the only channel by which the loop's SENTENCE
            # reached the desks. A SIGNIFICANT delta keeps its ``why``: it cannot
            # be written without cited evidence (``trajectory.TrajectoryEvent``
            # rejects it, and so does migration 0184's
            # ``situation_events_delta_requires_evidence`` CHECK) and it is
            # windowed to the read's own
            # fortnight, so its prose is a dated, evidence-backed statement of
            # what moved — exactly what the block exists to carry.
            if kind == DELTA_UNCHANGED_CHECKPOINT:
                lines.append(f"          * {when_txt} {kind}")
                continue
            lines.append(f"          * {when_txt} {kind}: {delta.get('why')}")
    return lines


def _render_continuity_block(
    prior: Mapping[str, Any] | None,
    situations: Sequence[Mapping[str, Any]],
    *,
    start_ordinal: int,
    ledger: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render the CONTINUITY section — up to three citable blocks, or ``""``.

    Ordinals CONTINUE the basis+periphery numbering (``start_ordinal =
    len(basis) + len(periphery) + 1``) so ``[[ref:N]]`` stays ONE flat resolution
    space: the cite phase maps ordinal ``N`` to the Nth rendered block across all
    sections, and no consumer has to re-parse the prompt to tell them apart (the
    resolved citation's ``continuity`` stamp does that).

    The order is the order of the memory itself — one step back (PRIOR READ),
    then the fortnight (WINDOW LEDGER), then what is open (REGISTER) — and the
    cite phase walks the same sequence, so ordinal N always means "the Nth
    rendered block" with no drift.

    Returns ``""`` when there is no memory at all — so a FIRST run's prompt is
    byte-identical to the pre-continuity render.
    """
    if prior is None and not situations and not ledger:
        return ""
    header = [
        "=== CONTINUITY (what this desk already knew) ===",
        "The block(s) below are the ONLY licensed source of 'before'. Cite them "
        "by their [[ref:N]] handle exactly like any other evidence, state what "
        "CHANGED against them, and take every date from the block itself — never "
        "from the time you are running. If nothing material changed, say that "
        "plainly. Never assert a trend, escalation, or 'ongoing' framing that "
        "these blocks do not support. Where a frame carries dated trajectory "
        "lines, those ARE the record of how it moved — use them for direction "
        "and their dates for timing; where a frame carries none, the system has "
        "no trajectory for it, which is not the same as it being steady.",
        "",
    ]
    body_lines: list[str] = []
    ordinal = start_ordinal
    if prior is not None:
        body_lines.extend(_render_prior_read_lines(prior, ordinal))
        ordinal += 1
    if ledger:
        if body_lines:
            body_lines.append("")
        body_lines.extend(ledger_block_lines(ledger, f"[[ref:{ordinal}]]"))
        ordinal += 1
    if situations:
        if body_lines:
            body_lines.append("")
        body_lines.extend(_render_situation_register_lines(situations, ordinal))
    return "\n".join(header + body_lines)


def window_ledger_citation(
    entries: Sequence[Mapping[str, Any]], ordinal: int
) -> dict[str, Any]:
    """The composition-side citation for a cited WINDOW LEDGER block.

    Same honest shape as the register's: NO ``ref_id`` (a synthetic multi-row
    block has no single drill target), the REAL member uuids on
    ``ledger_finding_ids``, and ``evidence_text`` carrying the rendered block so
    the verify pass grades a ledger-backed clause against exactly what the model
    was shown — with no change to the verify path.
    """
    return {
        "marker": f"[[ref:{ordinal}]]",
        "ordinal": ordinal,
        "ref_kind": WINDOW_LEDGER_REF_KIND,
        CONTINUITY_CITATION_KEY: CONTINUITY_WINDOW_LEDGER,
        "title": f"Window ledger ({len(entries)} dated record(s), trailing 14 days)",
        "ledger_finding_ids": ledger_finding_ids(entries),
        "evidence_text": ledger_evidence_text(entries, f"[[ref:{ordinal}]]"),
    }


__all__ = [
    "CONTINUITY_CITATION_KEY",
    "CONTINUITY_LEDGER_RECEIPT",
    "CONTINUITY_LEDGER_ROW_KEY",
    "CONTINUITY_PRIOR",
    "CONTINUITY_PRIOR_BODY_CHARS",
    "CONTINUITY_PRIOR_LOOKBACK_HOURS",
    "CONTINUITY_PRIOR_RECEIPT",
    "CONTINUITY_ROW_KEY",
    "CONTINUITY_SITUATIONS",
    "CONTINUITY_SITUATIONS_RECEIPT",
    "CONTINUITY_SITUATIONS_ROW_KEY",
    "CONTINUITY_WINDOW_LEDGER",
    "LEDGER_EVIDENCE_CHARS",
    "LEDGER_FETCH_LIMIT",
    "LEDGER_MIN_SEVERITY",
    "LEDGER_PER_UNIT_CAP",
    "LEDGER_SEVERITIES",
    "LEDGER_TITLE_CHARS",
    "LEDGER_TOTAL_CAP",
    "LEDGER_UNIT_TOTAL_CAP",
    "LEDGER_VERIFY_FLOOR",
    "LEDGER_WINDOW_HOURS",
    "REGISTER_NO_CORROBORATION",
    "REGISTER_SELF_CORROBORATION_RULE",
    "REGISTER_STALE_AFTER_DAYS",
    "REGISTER_STALE_LABEL",
    "SITUATION_REGISTER_CAP",
    "SITUATION_REGISTER_EVIDENCE_CHARS",
    "SITUATION_REGISTER_NAME_CHARS",
    "SITUATION_REGISTER_REF_KIND",
    "SITUATION_REGISTER_TRAJECTORY_DEPTH",
    "SITUATION_REGISTER_WHY_CHARS",
    "WINDOW_LEDGER_REF_KIND",
    "_as_float",
    "_continuity_selection",
    "_iso_text",
    "_ledger_entries",
    "_ledger_selection",
    "_register_situations",
    "_render_continuity_block",
    "_render_evidence_age",
    "_render_prior_read_lines",
    "_render_situation_register_lines",
    "defuse_ledger_markers",
    "is_stale_frame",
    "ledger_block_lines",
    "ledger_entry",
    "ledger_evidence_text",
    "ledger_finding_ids",
    "read_window_ledger",
    "select_ledger_entries",
    "window_ledger_citation",
    "window_ledger_rule",
]
