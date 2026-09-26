# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R1-d — THE FRAME-CONTENT GAUGE. It counts and it names. It never repairs.

The instrument ``planning/R1_FRAME_REPAIR_DESIGN_2026-09-06.md`` row R1-d asks
for, amended by ``planning/R1_FRAME_REPAIR_AMENDMENT_2026-09-06.md`` §3 and §4,
riding ``situation_clustering``'s own receipt in the ``_title_frame_gauge`` /
``composition_lineage_sweep`` precedent: **count and name, never repair, never
gate.** Nothing here writes a row, moves a threshold, changes a frame, touches
an alert or reaches the verify plane. If every function in this module returned
``None`` the fleet would behave identically.

WHY IT SHIPS BEFORE THE REPAIR, WHICH IS THE ONLY REASON IT IS INTERESTING.
R-1's flag (``LEGBA_REGISTER_CONTENTFUL_FRAMES``) does not exist yet and this
module must not wait for it: a gauge that starts measuring *after* the flip has
no baseline to be judged against, and the amendment's §4 is explicit that four
of these numbers are obligations precisely because the post-flip reading is
worthless without its pre-flip twin. So this runs FLAG-OFF, publishes the
degenerate values the flag-off world actually has (``anchored: 0``,
``distinct_anchors: 0``, ``evidence_mass_p50: None``), and is written so the
flag-on run needs no change to it whatsoever — every anchored-side field is
computed from the signature's ``#evt:`` slot, which is simply empty today.

THE FOUR AMENDED DEFINITIONS (§3, R1-d), each of which was re-cut for a reason:

``anchored``
    Frames whose key carries a non-``_domestic`` ``#evt:`` slot. Zero today —
    the mint is post-R4.
``domestic`` / ``retiring``
    ``#evt:_domestic`` (and, flag-off, un-suffixed) frames that RECEIVED
    MEMBERS this run are ``domestic``; one that received none is ``retiring``,
    counted separately. Without the split, D-l's retirement ladder reads as
    growth — a frame draining toward closure would be indistinguishable from a
    frame the desks are still feeding.
``contentless``
    The ``COVERAGE_FLOOR_82`` §1.1 method on ``situations.name`` and NOTHING
    else. Deliberately independent of the anchor: if the gauge scored content
    by asking whether a frame carries an anchor, then after the flip it would
    be measuring the repair WITH the repair, and would report success by
    construction. It stays a NAME test or it is not a measurement.
``evidence_mass_p50``
    Median distinct naming findings over ANCHORED frames — a FINDING count,
    not a signal count, because (amendment §1.2) the signal count is
    desk-uniform to within four signals across a desk's eight frames and
    therefore cannot order anything. Flag-off it is ``None``, never ``0``: no
    anchored frames is an empty denominator, and an empty denominator that
    reports a confident zero is the failure mode ``_title_frame_gauge`` §5.0
    retires entropy for. **Every ratio here is ``None`` on an empty
    denominator.**

THE PRE-FLIP TWIN (§4, obligation 3). ``evidence_mass_p50`` has no value to
publish flag-off, so the number that will BECOME it is published instead:
``naming_findings_distribution``, the p25/p50/p75 of "distinct findings naming
each nominated polity per (desk, dimension)" over the window (live 2026-09-06:
≈ 6 / 9 / 14). After the flip the p50 has a pre-flip twin computed by this same
code over this same window, which is the only way the comparison is honest.

It is published in BOTH readings, because they are different populations and
the difference is large: ``naming_findings_distribution`` over every nominated
cell, and ``naming_findings_at_mint_floor`` over the cells that would actually
anchor (see :func:`mint_floor_twin`). The second is the comparable one —
``evidence_mass_p50`` will be a median over ANCHORED frames — and the first is
published beside it so the restriction is visible rather than assumed. Pooled
ONLY as the twin: per amendment F-12 a desk's finding count is a cadence
artifact (9 dimensions on IL, 8 elsewhere), so it must never order desks
against each other, and ``evidence_mass_p50`` is reported per desk.

THE GUARD, CARRIED NOT RECOMPUTED (§4, obligation 1). ``breaches`` and
``breaches_naming_only`` are read off the last real ``coverage_floor`` scan's
receipt (``analyst_traces``) and reported side by side under ``guard``. Carried
rather than recomputed on purpose: the detector's numbers must come from the
detector, or the post-R4 delta is measured against a number produced by
different code. ``None`` when no recent scan is readable — the gauge never
substitutes a zero for "I could not look".

THE CENSUS (design §2.2), and its one honest limit. ``naming_census`` pairs each
(desk, polity) whose evidence clears the coverage-floor SIGNAL bar with
``frames_naming`` (open frames whose ``name`` is ``represented_by`` the polity)
and ``desks_engaging`` (latest head per analyst in the engagement window whose
``title``+``body`` prose is). The pair set is the SHIPPED detector's own phase-1
nomination — ``cluster_entities`` then ``candidate_clusters``, the same class
gate and the same home exclusion — with BOTH frame clauses off: clause 5
because a pair some frame already names is precisely the LICENSED half of the
2×2 and must be counted rather than suppressed, clause 6 for the same reason
R1-e now reports two numbers.

It stops at NOMINATION and does not re-run phase 2's exact per-signal recount,
which makes the pair set a deliberate SUPERSET of the detector's breaches — and
a superset is the conservative direction for M-2, since extra pairs can only
enlarge the denominator and lower the engagement rate. It stops there rather
than applying the full bar to the summed aggregate because only clauses 1 and 2
are upper bounds on summed numbers: applying the rest to them is not
conservative but simply wrong, and measured live 2026-09-06 it DROPS
IL/Palestine — the founding case — on a summed weighted mean magnitude of 0.498
against the exact recount's 0.508. A gauge that quietly loses the case it exists
to watch is worse than no gauge. **§2.3's circularity stands unamended: pre-flip
the census reads a loop (a frame is named because a desk wrote it), and only its
post-flip reading is a lever test.**

WHY IT IS SPACED. The pair set needs the coverage floor's own 14-day entity
aggregate, measured live at **9.4 s** — which is why the detector itself gates
that read at six hours. ``situation_clustering`` runs on a 20-minute clock, so
the gauge carries its own interval gate at the same default cadence and a
per-process cursor. The cursor is in-process and best-effort on purpose: it
writes nothing (a gauge that needs a watermark table has started to be
infrastructure), and a restart simply re-runs it once, which is the cheap
direction to be wrong in. Four readings a day is a baseline; eighteen an hour is
a load test.

ONE-WAY IMPORTS, DELIBERATE AND WORTH SAYING OUT LOUD. This module sits in
``legba.data`` beside :mod:`legba.data._polity_match`, whose matcher it reads
prose with, and imports three things UPWARD from
``legba.data.analysts.deterministic_handlers``: ``_title_frame_gauge``'s
published capitalisation stoplist, ``_coverage_floor_scan``'s bar, and
``finding_supersession``'s signature grammar. The alternative is three second
copies of three published vocabularies, and a second copy of a matcher is a
second set of false positives — the argument ``_polity_match``'s own banner
makes for existing at all. All three are leaf-ish and the direction is asserted
acyclic by ``test_frame_content_gauge.py``'s import test.

NO LLM, NO ANCHOR-SIDE SELF-DESCRIPTION (D-n, narrowed by amendment §2.4). This
module may not import an LLM handler and may not read ``data.key_entities`` or
its four producer-self-description siblings. A finding's ``title`` and ``body``
ARE allowed and are the substrate the naming counts read: under the QUOTATION
regime the desk sentence is the product, and authored prose about the world is
not a model's structured label for itself. Pinned by the AST guard in
``tests/data_pkg/test_frame_content_gauge.py``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ._polity_match import home_prose, represented_by
from .analysts.deterministic_handlers._coverage_floor_scan import (
    _ENTITY_AGG_SQL,
    _HOME_COUNTRIES_SQL,
    _SLICE_SIZE_SQL,
    _SURFACE_MIN_SIGNALS,
    CoverageFloorConfig,
    candidate_clusters,
    cluster_entities,
)
from .analysts.deterministic_handlers._title_frame_gauge import (
    entity_tokens,
    normalize,
)
from .analysts.deterministic_handlers.finding_supersession import (
    _SIGNATURE_EVENT_MARKER,
)

logger = logging.getLogger(__name__)

#: Descriptor-option prefix (the ``gauge_`` / ``coverage_floor_`` precedent).
OPTION_PREFIX: str = "frame_content_gauge_"

#: Env-var prefix. Env is the base default; a descriptor option, when set,
#: always wins — the order every other option in this tree uses.
ENV_PREFIX: str = "LEGBA_FRAME_CONTENT_GAUGE_"

#: The ``#evt:`` value migration 0193 will append to every existing frame. A
#: frame carrying it is DOMESTIC, not anchored — it is the residue, not a
#: story. Flag-off no frame carries any ``#evt:`` slot at all, which this
#: module reads as the same thing (see :func:`signature_anchor`).
DOMESTIC_ANCHOR: str = "_domestic"

#: How many frames a desk actually SEES — ``unit_grounding.SITUATION_CAP``,
#: mirrored rather than imported because a data-layer gauge must not drag the
#: grounding module into its import graph. C-6 pins it at 6 and the design is
#: explicit that anchor diversity re-selects WITHIN the cap and never widens
#: it, so a drift here would be a design change, not a tuning one.
RENDER_CAP: int = 6

#: Defensive per-run bounds. Hitting one is reported in the receipt, never
#: silent — the ``_coverage_floor_scan`` idiom.
_MAX_DESKS = 200
_MAX_FRAMES = 4_000
_MAX_ENTITY_ROWS = 40_000
_MAX_FINDING_ROWS = 40_000
_MAX_CENSUS_PAIRS = 400

#: Bounds the prose projection. Live max finding body is 14,598 B and the mean
#: 1,822 B (amendment §2.6); 6,000 keeps the read at ~13 MB over 14 days.
_MAX_BODY_CHARS = 6_000

#: Candidate polities kept per desk, worst-first by signal count. The live
#: nominated set is ~1-3 per desk; the bound stops a pathological substrate
#: turning the census into a quadratic prose match.
_MAX_CANDIDATES_PER_DESK = 6

#: THE TWIN'S TWO SHAPING CONSTANTS, and they gate NOTHING.
#:
#: ``evidence_mass_p50`` will be a median over ANCHORED frames, so its pre-flip
#: twin must be taken over the cells that would actually anchor — otherwise the
#: comparison is a different population wearing the same name, which is the
#: failure amendment §4's obligation 3 exists to prevent. These mirror the
#: amendment §2.2 recommendation for R1-a's bar (``min_anchor_findings = 5``,
#: ``max_anchor_ubiquity = 0.75``) so the twin is taken where the mint will be.
#:
#: They are NOT the bar and they are NOT knobs: nothing here mints, refuses or
#: suppresses anything, and the unrestricted distribution is published beside
#: the restricted one so the choice is visible rather than baked in. When R1-a
#: ships its own calibrated values these follow them; until then a drift
#: between the two is a reporting inaccuracy, never a behaviour change.
_TWIN_MIN_FINDINGS = 5
_TWIN_MAX_UBIQUITY = 0.75

#: How far back to look for the coverage floor's last REAL scan (its own
#: interval gate declines most ticks, and a declined tick's zeros are not the
#: guard's state). Bounded so the read stays on the analyst/started_at index.
_GUARD_LOOKBACK_HOURS = 48

#: The flag R-1's mint will hide behind. Named here only so the receipt can
#: say, in the row itself, which world these numbers were taken in.
REGISTER_FLAG_ENV: str = "LEGBA_REGISTER_CONTENTFUL_FRAMES"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameContentConfig:
    """The gauge's three knobs. Every one is declared in ``handler_options``."""

    #: The census / naming window. Matches the coverage floor's own, so the two
    #: instruments cannot disagree about which fortnight they are describing.
    window_days: int = 14
    #: ``desks_engaging``'s window — the design §2.2 "latest head per analyst
    #: in a 7-day window".
    engagement_days: int = 7
    #: Minimum spacing between the heavy 14-day entity aggregates. 0 runs the
    #: gauge every tick (what the tests do); the default matches the detector's
    #: own ``min_scan_interval_hours``.
    min_interval_hours: float = 6.0

    @classmethod
    def from_options(
        cls, options: Mapping[str, Any] | None
    ) -> "FrameContentConfig":
        kwargs: dict[str, Any] = {}
        opts = dict(options or {})
        for f in fields(cls):
            value = f.default
            env_raw = os.environ.get(ENV_PREFIX + f.name.upper())
            if env_raw is not None:
                value = _coerce(f.name, env_raw, value)
            opt_key = OPTION_PREFIX + f.name
            if opt_key in opts:
                value = _coerce(f.name, opts[opt_key], value)
            kwargs[f.name] = value
        return cls(**kwargs)


def config_from_options(
    options: Mapping[str, Any] | None,
) -> FrameContentConfig:
    """The PREFIX-FAMILY reader, in the ``_coverage_floor_scan`` idiom.

    A module-level function rather than only the classmethod because
    ``tests/data_pkg/test_handler_options_x1.py`` proves a prefixed knob
    REACHABLE by invoking its family's reader with a probe value — the
    stronger form of "no dead config", since no literal
    ``options.get("frame_content_gauge_window_days")`` exists to grep for.
    """
    return FrameContentConfig.from_options(options)


def _coerce(name: str, raw: Any, fallback: Any) -> Any:
    """Coerce to the field's type, keeping the predecessor on a bad value.

    A mistyped knob must degrade the gauge's resolution, never take the
    clusterer offline — the ``CoverageFloorConfig._coerce`` contract.
    """
    try:
        return type(fallback)(raw)
    except (TypeError, ValueError):
        logger.warning(
            "frame_content_gauge.bad_option name=%s raw=%r — keeping %r",
            name, raw, fallback,
        )
        return fallback


# ---------------------------------------------------------------------------
# THE NAME TEST — COVERAGE_FLOOR_82 §1.1, and nothing to do with the anchor
# ---------------------------------------------------------------------------


def leading_token(name: str) -> str:
    """The name's first word — capitalised by grammar, not by reference.

    ``COVERAGE_FLOOR_82`` §1.1/§1.3 excludes it explicitly, and the exclusion
    is load-bearing rather than cosmetic: it is the difference between
    reproducing that census (136 of 232 live at the time of writing, 112 of the
    192 RENDERED frames — the published figure exactly) and reporting 112 of
    232, which is a different and more flattering claim.
    """
    parts = normalize(name or "").strip().split()
    return parts[0] if parts else ""


def distinguishing_tokens(name: str, *, home_blob: str) -> list[str]:
    """The capitalised tokens of ``name`` that are neither boilerplate, the
    sentence's leading word, nor the desk's own country.

    The three exclusions are the census's own three: ``entity_tokens`` carries
    the published generic/dimension/verb stoplist (so "Military Posture" and
    "Escalating" are not subjects), the leading word goes for the reason above,
    and the home country is resolved through the SHIPPED matcher — never a fold
    containment, which gets Niger/Nigeria and Sudan/South Sudan wrong.
    """
    lead = leading_token(name)
    # BOTH DIRECTIONS, and the second one is not optional. ``represented_by``
    # takes a CANONICAL country name and prose; asking it whether the home
    # blob represents the token catches "Israel" and "Israel's", and asking it
    # whether the home country represents the token catches "Israeli" — a
    # demonym is not a canonical name, so the first direction alone scores the
    # desk's own adjective as a distinguishing proper noun and reports
    # "Escalating rhetoric heightens Israeli escalation risk" as content.
    home_names = [p.strip() for p in (home_blob or "").split("||") if p.strip()]
    out: list[str] = []
    for token in entity_tokens(name or ""):
        if token == lead:
            continue
        bare = token.rstrip("'s").rstrip("'")
        if home_blob and represented_by(bare, home_blob) is not None:
            continue
        if any(represented_by(h, bare) is not None for h in home_names):
            continue
        out.append(token)
    return out


def is_contentless(name: str, *, home_blob: str) -> bool:
    """True when the frame's NAME carries no distinguishing proper noun.

    "Argentina's military posture unchanged as no new developments observed" is
    grammatically a frame and informationally empty. This is the whole test,
    and it looks at ``situations.name`` ALONE — never at the key, never at an
    anchor, never at the frame's evidence. See the module banner for why that
    independence is the point rather than an oversight.
    """
    return not distinguishing_tokens(name, home_blob=home_blob)


# ---------------------------------------------------------------------------
# The signature's reserved anchor slot
# ---------------------------------------------------------------------------


def signature_anchor(signature: Any) -> str | None:
    """The non-``_domestic`` anchor a signature carries, or ``None``.

    ``None`` covers both flag-off shapes — a key with no ``#evt:`` slot at all
    (today, every frame) and a key explicitly re-suffixed ``#evt:_domestic`` by
    migration 0193 — because they mean the same thing: this frame is the
    dimension's own residue and no evidence-established anchor is on it.
    """
    text = str(signature or "")
    if _SIGNATURE_EVENT_MARKER not in text:
        return None
    anchor = text.rsplit(_SIGNATURE_EVENT_MARKER, 1)[1].strip()
    if not anchor or anchor == DOMESTIC_ANCHOR:
        return None
    return anchor


# ---------------------------------------------------------------------------
# Small honest statistics — every one of them ``None`` on nothing
# ---------------------------------------------------------------------------


def _ratio(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile over an ALREADY-SORTED sequence."""
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return float(sorted_values[low]) * (1 - frac) + float(sorted_values[high]) * frac


def distribution(values: Iterable[float]) -> dict[str, Any]:
    """``{n, min, p25, p50, p75, max}`` — every statistic ``None`` when n=0.

    The pre-flip twin of ``evidence_mass_p50`` (amendment §4, obligation 3),
    and the shape ``evidence_mass_p50`` itself is read out of.
    """
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return {"n": 0, "min": None, "p25": None, "p50": None,
                "p75": None, "max": None}
    return {
        "n": len(ordered),
        "min": round(ordered[0], 4),
        "p25": round(_quantile(ordered, 0.25), 4),
        "p50": round(_quantile(ordered, 0.50), 4),
        "p75": round(_quantile(ordered, 0.75), 4),
        "max": round(ordered[-1], 4),
    }


# ---------------------------------------------------------------------------
# THE GAUGE — pure over rows, so it is testable without a substrate
# ---------------------------------------------------------------------------


def frame_content_rows(
    frames: Sequence[Mapping[str, Any]],
    *,
    home_blobs: Mapping[str, str],
    touched_signatures: Iterable[str],
    evidence_mass: Mapping[tuple[str, str, str], int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-desk ``{open, anchored, domestic, retiring, contentless,
    distinct_anchors, rendered_contentless, evidence_mass_p50}``.

    ``frames`` rows carry ``target_id``, ``name``, ``situation_signature``,
    ``intensity_score`` and ``dimension``. ``touched_signatures`` is the set of
    signatures that RECEIVED MEMBERS in the run this gauge rides — the
    ``domestic`` / ``retiring`` split, and the reason the gauge lives inside
    the clusterer rather than beside it: nothing else in the tower knows which
    frames were fed this tick. ``evidence_mass`` maps
    ``(target, dimension, anchor)`` to the distinct findings naming that anchor;
    it is consulted only for ANCHORED frames, so flag-off it is never read.
    """
    touched = {str(s) for s in touched_signatures}
    masses = evidence_mass or {}
    by_desk: dict[str, list[Mapping[str, Any]]] = {}
    for row in frames:
        by_desk.setdefault(str(row.get("target_id") or ""), []).append(row)
    by_desk.pop("", None)

    out: dict[str, dict[str, Any]] = {}
    for desk, rows in sorted(by_desk.items()):
        blob = home_blobs.get(desk, "")
        anchors: set[str] = set()
        anchored = domestic = retiring = contentless = 0
        desk_masses: list[float] = []
        for row in rows:
            signature = str(row.get("situation_signature") or "")
            anchor = signature_anchor(signature)
            if is_contentless(str(row.get("name") or ""), home_blob=blob):
                contentless += 1
            if anchor is not None:
                anchored += 1
                anchors.add(anchor)
                mass = masses.get(
                    (desk, str(row.get("dimension") or ""), anchor)
                )
                if mass is not None:
                    desk_masses.append(float(mass))
            elif signature in touched:
                domestic += 1
            else:
                # D-l's ladder: a stored open frame that received no members
                # this run is on its way out. Counted apart so a register
                # DRAINING is never read as a register GROWING.
                retiring += 1
        rendered = sorted(
            rows,
            key=lambda r: (-float(r.get("intensity_score") or 0.0),
                           str(r.get("situation_signature") or "")),
        )[:RENDER_CAP]
        rendered_contentless = sum(
            1 for r in rendered
            if is_contentless(str(r.get("name") or ""), home_blob=blob)
        )
        out[desk] = {
            "open": len(rows),
            "anchored": anchored,
            "domestic": domestic,
            "retiring": retiring,
            "contentless": contentless,
            "contentless_rate": _ratio(contentless, len(rows)),
            "distinct_anchors": len(anchors),
            "rendered": len(rendered),
            "rendered_contentless": rendered_contentless,
            "rendered_contentless_rate": _ratio(rendered_contentless,
                                                len(rendered)),
            "evidence_mass_p50": distribution(desk_masses)["p50"],
        }
    return out


def naming_census_rows(
    pairs: Sequence[tuple[str, str]],
    *,
    frames: Sequence[Mapping[str, Any]],
    heads: Mapping[str, Sequence[Mapping[str, Any]]],
    naming_findings: Mapping[tuple[str, str], int] | None = None,
) -> dict[str, Any]:
    """The design §2.2 pair table plus its ``licensed × engaged`` 2×2.

    ``pairs`` is ``(target_id, polity)``; ``frames`` the open frames;
    ``heads`` the desk's latest head per analyst in the engagement window, each
    carrying ``title`` and ``body``. A pair is LICENSED when at least one open
    frame NAMES the polity and ENGAGED when at least one head's prose does —
    M-3's stop rule lives in the cell where both are true, so both halves are
    reported per pair and pooled into the 2×2 rather than only summarised.
    """
    names_by_desk: dict[str, list[str]] = {}
    for row in frames:
        names_by_desk.setdefault(
            str(row.get("target_id") or ""), []
        ).append(str(row.get("name") or ""))

    cells = {
        "licensed_engaged": 0,
        "licensed_unengaged": 0,
        "unlicensed_engaged": 0,
        "unlicensed_unengaged": 0,
    }
    unlicensed_engaging = 0
    unlicensed_slots = 0
    rows: list[dict[str, Any]] = []
    for desk, polity in pairs:
        desk_frames = names_by_desk.get(desk, [])
        frames_naming = sum(
            1 for n in desk_frames if represented_by(polity, n) is not None
        )
        desk_heads = list(heads.get(desk, ()))
        desks_engaging = sum(
            1 for h in desk_heads
            if represented_by(
                polity,
                f"{h.get('title') or ''} || {h.get('body') or ''}",
            ) is not None
        )
        licensed = frames_naming >= 1
        engaged = desks_engaging >= 1
        key = (
            f"{'licensed' if licensed else 'unlicensed'}_"
            f"{'engaged' if engaged else 'unengaged'}"
        )
        cells[key] += 1
        if not licensed:
            unlicensed_engaging += desks_engaging
            unlicensed_slots += len(desk_heads)
        rows.append({
            "target_id": desk,
            "polity": polity,
            "frames_naming": frames_naming,
            "open_frames": len(desk_frames),
            "desks_engaging": desks_engaging,
            "desk_heads": len(desk_heads),
            "naming_findings": (naming_findings or {}).get((desk, polity)),
        })
    rows.sort(key=lambda r: (r["target_id"], r["polity"]))
    return {
        "pairs": rows,
        "licensed_engaged_2x2": cells,
        # M-2's pooled number: ``desks_engaging`` over the pairs no frame
        # names, against those desks' own head slots (today 2 of 52 = 0.038).
        "unlicensed_pooled_engagement": {
            "engaging": unlicensed_engaging,
            "desk_slots": unlicensed_slots,
            "rate": _ratio(unlicensed_engaging, unlicensed_slots),
        },
    }


def churn_rows(desk_gauge: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """C-1 … C-5, computed off the per-desk gauge that just ran.

    C-4 (anchor survival) and C-5 (name churn) are ``None`` flag-off and say
    why in the row: C-4's denominator is anchored frames minted seven days ago,
    of which there are none until the flip, and C-5 is unmeasurable until
    ``data.name_version`` exists — ``situations.name`` is re-derived every 20
    minutes today with no history at all, which is the design's own note.
    """
    opens = [int(v["open"]) for v in desk_gauge.values()]
    anchors = [int(v["distinct_anchors"]) for v in desk_gauge.values()]
    return {
        "c1_open_frames_per_desk_mean": (
            None if not opens else round(sum(opens) / len(opens), 4)
        ),
        "c2_open_frames_per_desk_max": max(opens) if opens else None,
        "c3_distinct_anchors_per_desk_max": max(anchors) if anchors else None,
        "c4_anchor_survival_7d": None,
        "c4_reason": "no anchored frames exist flag-off; denominator empty",
        "c5_name_changes_per_frame_week": None,
        "c5_reason": (
            "situations.name is re-derived every run with no history; the "
            "data.name_version counter this needs lands with the mint"
        ),
        "c6_render_cap": RENDER_CAP,
        "desks": len(opens),
    }


# ---------------------------------------------------------------------------
# SQL — every read bounded, every read a SELECT
# ---------------------------------------------------------------------------

#: Open frames with the two fields the coverage floor's own frame read does not
#: project: the signature (the anchor slot lives in it) and the dimension.
_OPEN_FRAMES_SQL = """
    SELECT target_id,
           name,
           situation_signature,
           intensity_score,
           data ->> 'dimension' AS dimension
      FROM situations
     WHERE superseded_by IS NULL
       AND (valid_until IS NULL OR valid_until > now())
       AND status <> 'closed'
       AND target_id = ANY($1::text[])
     ORDER BY target_id, intensity_score DESC NULLS LAST, situation_signature
     LIMIT $2
"""

#: The desks' own authored prose for the window. ONE read serves both the
#: engagement half of the census (the latest head per analyst inside the
#: shorter engagement window) and the naming distribution (every finding in the
#: full window, per dimension) — the same rows, asked two questions, rather
#: than two passes over the same table.
_FINDINGS_SQL = """
    SELECT target_id,
           analyst_id,
           produced_at,
           title,
           left(body, $3) AS body
      FROM analyst_outputs
     WHERE kind = 'finding'
       AND target_id = ANY($1::text[])
       AND produced_at > now() - make_interval(days => $2)
     ORDER BY target_id, analyst_id, produced_at DESC
     LIMIT $4
"""

#: The last coverage-floor scan that actually SCANNED. Its own interval gate
#: declines most ticks and a declined tick reports zeros; adopting those would
#: report the guard as clean whenever we happened to look between scans.
_GUARD_SQL = """
    SELECT run_started_at,
           output_payload -> 'data' -> 'counts_by_class' -> 'coverage_floor'
             AS counts
      FROM analyst_traces
     WHERE analyst_id = 'alert_trigger_scan'
       AND run_started_at > now() - make_interval(hours => $1)
       AND COALESCE(
             (output_payload -> 'data' -> 'counts_by_class'
                            -> 'coverage_floor' ->> 'skipped_interval')::int,
             1
           ) = 0
     ORDER BY run_started_at DESC
     LIMIT 1
"""


# ---------------------------------------------------------------------------
# The interval cursor — per process, best-effort, writes nothing
# ---------------------------------------------------------------------------

_LAST_RUN_AT: dict[str, datetime] = {}


def due(key: str, *, now: datetime, interval_hours: float) -> bool:
    """Whether the heavy pass may run, and stamp the cursor when it may.

    Deliberately in-process (see the module banner): a gauge that needs a
    durable cursor has started to be infrastructure, and the cost of being
    wrong is one extra 9-second read after a restart.
    """
    if interval_hours <= 0:
        return True
    last = _LAST_RUN_AT.get(key)
    if last is not None and now - last < timedelta(hours=interval_hours):
        return False
    _LAST_RUN_AT[key] = now
    return True


def reset_cursor() -> None:
    """Forget every interval cursor. For tests and for nothing else."""
    _LAST_RUN_AT.clear()


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


async def _read_guard(conn: Any) -> dict[str, Any]:
    """``breaches`` / ``breaches_naming_only`` off the detector's own receipt.

    ``None`` — never 0 — when no recent real scan is readable. "The guard is
    quiet" and "I could not read the guard" are different facts and the row
    says which.
    """
    out: dict[str, Any] = {
        "source": "alert_trigger_scan.counts_by_class.coverage_floor",
        "as_of": None,
        "breaches": None,
        "breaches_naming_only": None,
    }
    row = await conn.fetchrow(_GUARD_SQL, float(_GUARD_LOOKBACK_HOURS))
    if row is None:
        return out
    counts = row["counts"]
    if isinstance(counts, str):
        try:
            counts = json.loads(counts)
        except ValueError:
            counts = None
    if not isinstance(counts, Mapping):
        return out
    started = row["run_started_at"]
    out["as_of"] = started.isoformat() if started is not None else None
    for key in ("breaches", "breaches_naming_only"):
        value = counts.get(key)
        if isinstance(value, int):
            out[key] = value
    return out


def _latest_heads(
    findings: Sequence[Mapping[str, Any]], *, engagement_days: int, now: datetime
) -> dict[str, list[Mapping[str, Any]]]:
    """The latest head per (desk, analyst) inside the engagement window."""
    cutoff = now - timedelta(days=engagement_days)
    seen: set[tuple[str, str]] = set()
    out: dict[str, list[Mapping[str, Any]]] = {}
    for row in findings:  # already ordered target, analyst, produced_at DESC
        produced = row.get("produced_at")
        if produced is None or produced < cutoff:
            continue
        key = (str(row.get("target_id") or ""), str(row.get("analyst_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(key[0], []).append(row)
    return out


def mint_floor_twin(
    by_dimension: Mapping[tuple[str, str, str], int],
    *,
    by_desk: Mapping[tuple[str, str], int],
    desk_findings: Mapping[str, int],
) -> dict[str, Any]:
    """The distribution restricted to the cells that would actually ANCHOR.

    ``evidence_mass_p50`` is a median over anchored frames; a pre-flip twin
    taken over EVERY nominated pair is a wider, softer population and would
    make the post-flip p50 look like a change when it is a change of
    denominator. So the twin drops the two families R1-a's bar drops: cells
    below the recurrence floor, and polities so ubiquitous on their own desk
    that they are wire boilerplate rather than a second story (live: the
    United States is named in 94-97% of every desk's finding bodies, which is
    the whole reason D-t exists).

    Reported BESIDE the unrestricted distribution, never instead of it.
    """
    kept: list[int] = []
    for (desk, _dimension, polity), count in by_dimension.items():
        if count < _TWIN_MIN_FINDINGS:
            continue
        total = desk_findings.get(desk, 0)
        named = by_desk.get((desk, polity), 0)
        if total and named / total >= _TWIN_MAX_UBIQUITY:
            continue
        kept.append(count)
    out = distribution(kept)
    out["min_findings"] = _TWIN_MIN_FINDINGS
    out["max_ubiquity"] = _TWIN_MAX_UBIQUITY
    return out


def _naming_counts(
    findings: Sequence[Mapping[str, Any]],
    *,
    candidates_by_desk: Mapping[str, Sequence[str]],
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], int]]:
    """Distinct findings naming each candidate polity.

    Returns ``(by (desk, dimension, polity), by (desk, polity))``. The prose is
    the finding's own ``title`` + ``body`` — the analyst's authored judgment,
    which amendment §1.4 measures as the only projection that discriminates at
    frame grain (IL's frames name Palestine in 12/12/7/5/1/0/0/0 bodies where
    the signal pool is flat to within four signals across all eight).
    """
    by_dimension: dict[tuple[str, str, str], int] = {}
    by_desk: dict[tuple[str, str], int] = {}
    for row in findings:
        desk = str(row.get("target_id") or "")
        candidates = candidates_by_desk.get(desk)
        if not candidates:
            continue
        prose = f"{row.get('title') or ''} || {row.get('body') or ''}"
        dimension = str(row.get("analyst_id") or "")
        for polity in candidates:
            if represented_by(polity, prose) is None:
                continue
            by_dimension[(desk, dimension, polity)] = (
                by_dimension.get((desk, dimension, polity), 0) + 1
            )
            by_desk[(desk, polity)] = by_desk.get((desk, polity), 0) + 1
    return by_dimension, by_desk


async def collect(
    pool: Any,
    *,
    options: Mapping[str, Any] | None = None,
    touched_signatures: Iterable[str] = (),
    cursor_key: str = "situation_clustering",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Run the gauge, or return ``None`` when it is not this tick's turn.

    The return value is the three receipt keys and nothing else, so the caller
    can splice it into its own ``data`` without touching a field it already
    publishes. Every read here is a SELECT; nothing is written anywhere.
    """
    from .analysts.deterministic_handlers.alert_trigger_scan import (
        _DESKS_SQL,
        _parse_jsonish,
    )

    cfg = FrameContentConfig.from_options(options)
    now = now or datetime.now(timezone.utc)
    if not due(cursor_key, now=now, interval_hours=cfg.min_interval_hours):
        return None

    floor_cfg = CoverageFloorConfig.from_options(options)
    bounds = {"desks": 0, "frames": 0, "entity_rows": 0, "findings": 0,
              "census_pairs": 0, "candidates_truncated_desks": 0}
    async with pool.acquire() as conn:
        desks = await conn.fetch(_DESKS_SQL, _MAX_DESKS)
        desk_ids = [str(r["descriptor_id"]) for r in desks]
        if not desk_ids:
            return None
        frames = [
            dict(r)
            for r in await conn.fetch(_OPEN_FRAMES_SQL, desk_ids, _MAX_FRAMES)
        ]
        entity_rows = await conn.fetch(
            _ENTITY_AGG_SQL,
            desk_ids,
            int(cfg.window_days),
            float(floor_cfg.min_entity_confidence),
            float(floor_cfg.high_magnitude),
            int(_SURFACE_MIN_SIGNALS),
            int(_MAX_ENTITY_ROWS),
        )
        slice_rows = await conn.fetch(
            _SLICE_SIZE_SQL, desk_ids, int(cfg.window_days)
        )
        geo_by_desk = {
            str(r["descriptor_id"]): [
                str(g) for g in (_parse_jsonish(r["geo"]) or [])
                if isinstance(g, str)
            ]
            for r in desks
        }
        home_rows = await conn.fetch(
            _HOME_COUNTRIES_SQL,
            sorted({g for codes in geo_by_desk.values() for g in codes}),
        )
        findings = [
            dict(r)
            for r in await conn.fetch(
                _FINDINGS_SQL,
                desk_ids,
                int(cfg.window_days),
                int(_MAX_BODY_CHARS),
                int(_MAX_FINDING_ROWS),
            )
        ]
        guard = await _read_guard(conn)

    bounds["desks"] = int(len(desks) >= _MAX_DESKS)
    bounds["frames"] = int(len(frames) >= _MAX_FRAMES)
    bounds["entity_rows"] = int(len(entity_rows) >= _MAX_ENTITY_ROWS)
    bounds["findings"] = int(len(findings) >= _MAX_FINDING_ROWS)

    iso_to_name = {str(r["iso2"]): str(r["name"]) for r in home_rows}
    home_blobs = {
        desk: home_prose(
            codes, [iso_to_name[c] for c in codes if c in iso_to_name]
        )
        for desk, codes in geo_by_desk.items()
    }
    slice_size = {
        str(r["target_id"]): int(r["n_entity_signals"] or 0) for r in slice_rows
    }
    by_target_entities: dict[str, list[Mapping[str, Any]]] = {}
    for row in entity_rows:
        by_target_entities.setdefault(str(row["target_id"]), []).append(row)

    # THE CENSUS PAIR SET — the detector's own phase-1 nomination, with BOTH
    # frame clauses disabled. ``frame_names=()`` turns clause 5 off (a pair a
    # frame already names is exactly the LICENSED half of the 2×2 and must be
    # in the census, not suppressed from it) and ``frame_finding_titles``
    # defaults to () so clause 6 is off too.
    #
    # Why nomination rather than the full bar: only clauses 1 and 2 (signal
    # count, high-magnitude count) are UPPER bounds on the summed aggregate,
    # which is the whole reason the detector defers the rest to a phase-2
    # exact recount. Applying the full bar to summed numbers is not
    # conservative, it is WRONG in both directions — measured live 2026-09-06,
    # it drops IL/Palestine, the founding case, because the summed weighted
    # mean magnitude reads 0.498 where the exact recount reads 0.508. A gauge
    # that quietly loses the case it exists to watch is worse than no gauge,
    # so the pair set is the honest superset and the census says so.
    pairs: list[tuple[str, str]] = []
    candidates_by_desk: dict[str, list[str]] = {}
    truncated_desks = 0
    for desk in desk_ids:
        blob = home_blobs.get(desk, "")
        if not blob:
            # No gazetteer for this desk's geo => the home exclusion cannot be
            # applied, and without it the desk's own country would nominate
            # itself. Skip the desk rather than publish a wrong pair.
            continue
        clusters = cluster_entities(
            by_target_entities.get(desk, ()), home_blob=blob
        )
        nominated = candidate_clusters(
            clusters, frame_names=(), config=floor_cfg
        )
        kept = nominated[:_MAX_CANDIDATES_PER_DESK]
        truncated_desks += int(len(nominated) > len(kept))
        if kept:
            candidates_by_desk[desk] = [c.name for c in kept]
            pairs.extend((desk, c.name) for c in kept)
    bounds["candidates_truncated_desks"] = truncated_desks
    if len(pairs) > _MAX_CENSUS_PAIRS:
        bounds["census_pairs"] = 1
        pairs = sorted(pairs)[:_MAX_CENSUS_PAIRS]
        kept_desks = {d for d, _p in pairs}
        candidates_by_desk = {
            d: [p for dd, p in pairs if dd == d] for d in kept_desks
        }

    mass_by_dimension, mass_by_desk = _naming_counts(
        findings, candidates_by_desk=candidates_by_desk
    )
    desk_finding_counts: dict[str, int] = {}
    for row in findings:
        desk = str(row.get("target_id") or "")
        desk_finding_counts[desk] = desk_finding_counts.get(desk, 0) + 1
    heads = _latest_heads(
        findings, engagement_days=cfg.engagement_days, now=now
    )
    desk_gauge = frame_content_rows(
        frames,
        home_blobs=home_blobs,
        touched_signatures=touched_signatures,
        evidence_mass=mass_by_dimension,
    )
    census = naming_census_rows(
        pairs, frames=frames, heads=heads, naming_findings=mass_by_desk
    )
    fleet = {
        key: sum(int(v[key]) for v in desk_gauge.values())
        for key in ("open", "anchored", "domestic", "retiring", "contentless",
                    "rendered", "rendered_contentless")
    }
    fleet["desks"] = len(desk_gauge)
    fleet["distinct_anchors"] = sum(
        int(v["distinct_anchors"]) for v in desk_gauge.values()
    )
    fleet["contentless_rate"] = _ratio(fleet["contentless"], fleet["open"])
    fleet["rendered_contentless_rate"] = _ratio(
        fleet["rendered_contentless"], fleet["rendered"]
    )
    return {
        "frame_content_gauge": {
            "as_of": now.isoformat(),
            "window_days": cfg.window_days,
            "engagement_days": cfg.engagement_days,
            "register_flag": {
                "env": REGISTER_FLAG_ENV,
                "present": REGISTER_FLAG_ENV in os.environ,
            },
            "desks": desk_gauge,
            "fleet": fleet,
            # The pre-flip twin of ``evidence_mass_p50`` (§4, obligation 3),
            # in both readings: every nominated cell, and the cells that would
            # actually anchor. Pooled ONLY as the twin — never as a cross-desk
            # ordering (F-12: a desk's finding count is a cadence artifact).
            "naming_findings_distribution": distribution(
                mass_by_dimension.values()
            ),
            "naming_findings_at_mint_floor": mint_floor_twin(
                mass_by_dimension,
                by_desk=mass_by_desk,
                desk_findings=desk_finding_counts,
            ),
            "guard": guard,
            "bounds_hit": bounds,
        },
        "naming_census": census,
        "churn": churn_rows(desk_gauge),
    }

__all__ = [
    "DOMESTIC_ANCHOR",
    "ENV_PREFIX",
    "FrameContentConfig",
    "OPTION_PREFIX",
    "REGISTER_FLAG_ENV",
    "RENDER_CAP",
    "churn_rows",
    "config_from_options",
    "collect",
    "distinguishing_tokens",
    "distribution",
    "due",
    "frame_content_rows",
    "is_contentless",
    "leading_token",
    "mint_floor_twin",
    "naming_census_rows",
    "reset_cursor",
    "signature_anchor",
]
