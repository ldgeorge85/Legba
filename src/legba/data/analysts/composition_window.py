# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-1 — the composition's ADMISSIBILITY WINDOW and its two-tier evidence.

``planning/FRAME_PROGRAM_2026-08-20.md`` §3/§4/§6, ratified 2026-08-21. One
architecture finding carried most of the CORRECTNESS-R1 mass — **"a 72-hour
pipeline forgets its own window"** — and one of its mechanisms lives here: the
composition subscribed to its source heads under a trailing **24h wall-clock
freshness cliff** while the units beneath it fire on an 11h cooldown that
HOLDS whenever a desk has no new matching signal. On 2026-08-20 the Burkina
Faso desk's seven units had last fired 42 hours earlier; the 11:36Z
composition's slice was mechanically empty and the product printed "No source
findings to synthesize" over seven two-day-old heads that carried the window's
major story.

The fix is not a bigger slice. It is a change of MEANING: the descriptor's
``time_window`` stops being a freshness cliff and becomes an **admissibility
horizon** (24h → 336h at deploy), the fold to one newest non-superseded head
per (unit, desk) — which already existed — becomes the whole selection rule,
and the composition is then obliged to say HOW OLD each head it consumed is.
An age that is printed can be narrated; an age that is filtered away can only
be forgotten.

Three things live here, and they are one idea seen from three sides:

**AGES.** :func:`head_age_hours` / :func:`human_datetime` / :func:`age_suffix`
render every consumed head's date as a human would write it plus its age in
hours, straight into the attribution line. The human date is load-bearing, not
decoration: ``_tradecraft.NO_INSTRUMENT_READINGS`` forbids the model printing a
raw ISO timestamp and the dated-claim rule forbids it COMPUTING a date, so a
prompt that shows only ``produced_at=2026-08-18T07:00:02+00:00`` is asking for
a date it also forbids. :func:`head_ages_stamp` records the same accounting on
the envelope (``data.head_ages``) so the §6 staleness gauge reads a number the
product itself published rather than re-deriving one.

**THE FLOOR'S ACTION, MADE VISIBLE.** The 0.50 verification floor does not
move. What changes is that its 25 dimension-slot drops stop being invisible:
:func:`build_coverage_ledger` computes, per declared unit, whether the desk's
newest head entered the BASIS, sat BELOW THE FLOOR (with its date and score),
or does not exist within the horizon at all — and
:func:`render_coverage_ledger_block` hands that accounting to the model as
DATA. This is the C4 audit's atom-10 precedent turned into contract: a
floor-withheld dimension is "below verification floor", **never** "no read this
cycle". Only a unit with no head in the horizon is a gap.

**THE NEWEST PASSING HEAD.** The GB-drone class (ATTRIBUTION, H-FLOOR): the
freshest head failed verify at eff 0.40 while an in-horizon PRIOR head at 0.571
would have passed. Because the basis gather also requires
``superseded_by IS NULL``, that prior head is unreachable — the newer failing
head superseded it — so the dimension vanished from BOTH tiers.
:func:`select_floor_fallback` picks the newest floor-PASSING head for exactly
those units, superseded or not, and stamps it :data:`FLOOR_FALLBACK_KEY` so the
render can say what it is: a dated passing read in the basis WITH the newer
failing read visible in the periphery. Showing both is strictly more honest
than showing neither, which is how the false-absence class was manufactured.

Also here, moved 2026-08-20 under the module-size gate
(``tests/test_module_size_gate.py`` — ``meta_findings_synthesizer.py`` was three
lines under its ceiling): the C-TIER **two-tier evidence** selection and
rendering. The seam is not arbitrary. The periphery IS "what the floor
withheld", which is the same question this module's coverage ledger answers
from the other direction, and both need the same row-reading primitives.
``meta_findings_synthesizer`` imports these names ONE WAY and re-exports them,
so every existing importer (and every test that reaches for
``synth._select_periphery``) is unchanged.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..provenance.models import severity_delta_from_tags

logger = logging.getLogger(__name__)


MAX_TITLE_CHARS: int = 200
"""Per-block title cap. Moved here with the periphery render (its only two
callers are that render and the basis render, which imports it back)."""


# ---------------------------------------------------------------------------
# C-TIER two-tier evidence — the vocabulary (moved 2026-08-20, size gate)
# ---------------------------------------------------------------------------

PERIPHERY_TIER: str = "periphery"
"""The tier token: the ``_evidence_tier`` row marker READ_SLICE stamps on
periphery rows AND the ``tier`` value the cite phase stamps on a citation that
resolves into the periphery section (the verify pass keys its hedge-required
rule on it)."""

PERIPHERY_CAP: int = 8
"""Max periphery items rendered per composition — worst-first (severity rank,
then recency), so the cap keeps the items most worth surfacing."""

PERIPHERY_BODY_CHARS: int = 400
"""Periphery body excerpt cap — tighter than the basis ``MAX_BODY_CHARS``
(periphery is hedged context, never the load-bearing narrative)."""

_EVIDENCE_TIER_KEY: str = "_evidence_tier"
_EVIDENCE_FLOOR_KEY: str = "_evidence_floor"

#: ``severity:<level>`` → worst-first rank for the periphery cap sort. Missing /
#: unknown level ranks -1 (sorts last — an unscored item never displaces a
#: scored one). Mirrors ``scorecard_banding.SEVERITY_TO_BAND``'s level set.
_SEVERITY_RANK: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "elevated": 2,
    "moderate": 1,
    "low": 0,
}

#: The child-``[[ref:N]]`` marker language, for the DEFUSE rewrite below. A
#: deliberate second spelling of ``meta_findings_synthesizer._REF_MARKER_RE``:
#: that one parses the model's OWN OUTPUT markers, this one rewrites markers
#: found in INPUT text before it is shown as a parent tier's evidence. Same
#: language, two questions — and the two are held in lockstep by a test
#: (``test_composition_head_window.py``), the same idiom
#: ``TIERED_BASIS_FLOOR_DEFAULT`` uses to mirror ``scorecard_banding.FAITH_FLOOR``.
_CHILD_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")


def _defuse_child_ref_markers(text: str) -> str:
    """Rewrite a lower tier's embedded ``[[ref:N]]`` markers to a visually
    distinct, non-resolvable form before ``text`` is rendered as a PARENT
    tier's evidence.

    ``[[ref:N]]`` becomes ``(child ref N)`` — unambiguous both from this
    tier's OWN ``[[ref:N]]`` ordinal space and from a first-order unit's
    bracketed ``[N]`` signal index, while still preserving the information
    that the child cited something there. Pure / idempotent; text with no
    embedded marker (the overwhelming common case: first-order unit bodies
    never contain ``[[ref:N]]``) is returned unchanged with no allocation
    beyond the input.
    """
    if not text or "[[ref:" not in text:
        return text
    return _CHILD_REF_MARKER_RE.sub(lambda m: f"(child ref {m.group(1)})", text)


def _row_tags(row: Mapping[str, Any]) -> tuple[str, ...]:
    """A finding row's stamped tag list, or ``()``.

    A finding's ``data`` column is the FindingPayload envelope, so its tags land
    at ``data -> 'tags'`` (see the meta-filter note on
    ``read_other_analyst_findings``). Tolerates a JSON-encoded ``data`` string
    (asyncpg without a JSONB codec).

    Extracted from :func:`_row_severity_level` when FRAME-3 gave the row a
    SECOND tag to read: the envelope unwrap is the fiddly half of both readers,
    and two hand-maintained copies of it would drift on the first driver change.
    """
    env = row.get("data")
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (ValueError, TypeError):
            return ()
    if not isinstance(env, Mapping):
        return ()
    tags = env.get("tags")
    if not isinstance(tags, (list, tuple)):
        return ()
    return tuple(t for t in tags if isinstance(t, str))


def _row_severity_level(row: Mapping[str, Any]) -> str | None:
    """The ``severity:<level>`` level from a finding row's stamped tags.

    FRAME-3 changed what this level MEANS without changing how it is read: it is
    now the dimension's STANDING state on the source desk rather than the
    severity of that desk's slice delta. The movement it used to conflate is
    :func:`_row_severity_delta`.

    The LAST valid tag wins (the analyst contract emits exactly one);
    absent/unknown → ``None``.
    """
    level: str | None = None
    for tag in _row_tags(row):
        if not tag.startswith("severity:"):
            continue
        candidate = tag.split(":", 1)[1].strip().lower()
        if candidate in _SEVERITY_RANK:
            level = candidate
    return level


def _row_severity_delta(row: Mapping[str, Any]) -> str | None:
    """FRAME-3 — the ``severity_delta:<rose|fell|steady|new>`` call, or ``None``.

    ``None`` is the common and CORRECT answer for any head a desk wrote before
    the FRAME-3 prompt flip reached it, which is why every render of this value
    is suffix-style and omitted when absent: an unflipped desk's block stays
    byte-identical, and no consumer may substitute ``steady`` for silence (the
    :func:`~legba.data.provenance.models.severity_delta_from_tags` contract).

    The vocabulary itself is NOT re-listed here — it is imported — so a level
    added to the tag contract reaches this reader instead of being silently
    dropped by a second copy of the list.
    """
    return severity_delta_from_tags(_row_tags(row))


def _row_severity_rank(row: Mapping[str, Any]) -> int:
    """Worst-first sort rank for :func:`_select_periphery` (missing → -1).

    Ranks the STANDING level only. FRAME-3 deliberately keeps the delta out of
    this key: the periphery cap should keep the worst CONDITIONS, and sorting a
    quiet desk's ``rose`` above a running war's ``steady`` is the novelty-over-
    consequence ordering the same train forbids the model to use.
    """
    level = _row_severity_level(row)
    return _SEVERITY_RANK.get(level, -1) if level is not None else -1


def _row_body_excerpt(row: Mapping[str, Any], cap: int) -> str:
    """The row's body excerpt — column first, then ``data.body`` (the same
    fallback chain the basis render uses), with the SAME
    child-``[[ref:N]]``-marker defuse (periphery rows for a region/world/
    thematic composition are the SAME lower-tier composition heads as the
    basis rows, just below the floor — they carry the identical pollution
    risk)."""
    body = row.get("body")
    if not isinstance(body, str):
        data = row.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                data = None
        inner = data.get("body") if isinstance(data, Mapping) else None
        body = inner if isinstance(inner, str) else ""
    return _defuse_child_ref_markers(body)[:cap]


def _select_periphery(
    rows: Sequence[Mapping[str, Any]], *, cap: int = PERIPHERY_CAP
) -> list[Mapping[str, Any]]:
    """Deterministic worst-first periphery selection: severity rank DESC, then
    recency DESC, then row id (pure tiebreak) — so the cap keeps the items most
    worth surfacing and the same input set always yields the same list. Pure;
    unit-tested for cap + order determinism."""

    def _key(row: Mapping[str, Any]) -> tuple[int, str, str]:
        v = row.get("produced_at")
        if v is None:
            rec = ""
        elif isinstance(v, str):
            rec = v
        else:
            iso = getattr(v, "isoformat", None)
            rec = iso() if callable(iso) else str(v)
        return (_row_severity_rank(row), rec, str(row.get("id") or ""))

    ordered = sorted(rows, key=_key, reverse=True)
    return list(ordered[:cap])


def _periphery_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The kept periphery rows' finding ids (envelope honesty; malformed ids
    are skipped, mirroring ``_orient``)."""
    out: list[str] = []
    for row in rows:
        uid = _coerce_uuid_text(row.get("id"))
        if uid is not None:
            out.append(uid)
    return out


def _render_periphery_block(
    rows: Sequence[Mapping[str, Any]],
    *,
    start_ordinal: int,
    floor: float | None,
) -> str:
    """Render the PERIPHERY tier as an explicit delimited prompt section.

    Ordinals CONTINUE the basis numbering (``start_ordinal = len(basis)+1``) so
    ``[[ref:N]]`` stays one flat resolution space — the cite phase maps ordinal
    ``N`` to the Nth rendered block across BOTH sections, and the verify pass
    tells the tiers apart by the ``tier`` stamp on the resolved citation, never
    by re-parsing the prompt. Each item carries its honest status
    (``below_floor`` with its score, or ``unverified``) so the model sees WHY
    the item is quarantined. Returns ``""`` for an empty set — the empty-
    periphery prompt is byte-identical to the untiered render.

    FRAME-1: each item additionally carries its human read date + age, for the
    same reason the basis blocks do — a dated weak read the model can name
    ("a newer, unverified read of 19 August") is the other half of the
    newest-passing-head routing in :func:`select_floor_fallback`.
    """
    if not rows:
        return ""
    floor_txt = (
        f"{float(floor):.2f}" if isinstance(floor, (int, float)) else "(unset)"
    )
    header = (
        "=== WEAKLY-SUPPORTED / UNVERIFIED SIGNALS "
        f"(below the verification floor {floor_txt}) ===\n"
        f"The {len(rows)} item(s) below did NOT clear the verification floor: "
        "each either scored below it on its faithfulness verify "
        "(status=below_floor) or never passed one (status=unverified). They are "
        "NOT established facts and MUST NOT be cited as established fact. Rules "
        "for this section:\n"
        "  - These items may inform HEDGED context only. Any claim resting "
        "solely on an item below MUST be attributed and hedged (e.g. "
        '"weakly-supported reporting suggests ..." / "an unverified read '
        'indicates ..."). The verify pass flags unhedged use.\n'
        "  - Where an item below CONFLICTS with a verified finding above, "
        "SURFACE the tension explicitly — a brief, hedged 'Tensions worth "
        "watching' note naming both sides — never drop it and never blend it "
        "in silently.\n"
        "  - Do NOT let these items set the BLUF or the severity; the verified "
        "findings above are the load-bearing evidence.\n"
        "  - Cite these by their [[ref:N]] handle exactly like the findings "
        "above.\n\n"
    )
    body_lines: list[str] = []
    for i, row in enumerate(rows, start=start_ordinal):
        title = str(row.get("title") or "(untitled)")[:MAX_TITLE_CHARS]
        analyst_id = str(row.get("analyst_id") or "(unknown)")
        produced_at = row.get("produced_at")
        status = "unverified" if row.get("faithfulness_score") is None else "below_floor"
        eff = row.get("effective_confidence")
        try:
            score_part = f" effective_confidence={float(eff):.2f}" if eff is not None else ""
        except (TypeError, ValueError):
            score_part = ""
        # FRAME-3: the STANDING level and, beside it, the source's own movement
        # call. Both are suffix-style and omitted when unstamped, so a block
        # from a desk that has not been flipped renders byte-identically.
        level = _row_severity_level(row)
        sev_part = f" severity={level}" if level else ""
        delta = _row_severity_delta(row)
        sev_part += f" severity_delta={delta}" if delta else ""
        body = _row_body_excerpt(row, PERIPHERY_BODY_CHARS)
        body_lines.append(
            f"[[ref:{i}]] {title}\n"
            f"      analyst_id={analyst_id} status={status}{score_part}{sev_part}"
            f" produced_at={produced_at}{age_suffix(row)}\n"
            f"      body: {body}"
        )
    return header + "\n".join(body_lines)


# ---------------------------------------------------------------------------
# FRAME-1 — AGES: what a consumed head's date and age are, and how they render
# ---------------------------------------------------------------------------

#: Month names spelled out here rather than via ``%B``: ``strftime`` month names
#: are LOCALE-dependent, and a prompt whose dates silently change language with
#: the container's LANG is a fabrication risk (the model copies what it is
#: shown). Deterministic by construction.
_MONTHS: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: Above this age a consumed head PREDATES the composition's own previous run
#: (the country composition's fallback cadence is twice daily, ``30 11,23 * * *``)
#: — i.e. the desk has been silent for a whole compose cycle, and the prose must
#: say so instead of reading as if it were written today. Deliberately well
#: under the §6 gauge's 34h alarm: the PROSE should disclose staleness earlier
#: than the OPERATOR is paged for it.
STALE_HEAD_DISCLOSE_HOURS: float = 24.0

#: Bound on the per-head ages recorded in the ``data.head_ages`` envelope stamp.
#: Sized above ``MAX_WORLD_INPUT_FINDINGS`` (64) so the cap never bites on a real
#: slice; it exists so a pathological input set cannot balloon the finding row.
HEAD_AGES_CAP: int = 64

#: Row key: the admissibility horizon (hours) READ_SLICE resolved for this run,
#: denormalized onto every returned row so the DB-less ``_run`` can render and
#: stamp it without a second descriptor read. Mirrors the ``_region_coverage`` /
#: ``_freshness`` denormalization idiom exactly.
HORIZON_ROW_KEY: str = "_admissibility_horizon_h"

#: Row key: stamped on a basis row admitted by :func:`select_floor_fallback` —
#: the newest floor-PASSING head for a unit whose LIVE head failed the floor.
#: Carries the newer failing head's date/score so the render can name the
#: relation instead of silently presenting an older read as the current one.
FLOOR_FALLBACK_KEY: str = "_floor_fallback"


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_uuid_text(value: Any) -> str | None:
    """A row id as text, or ``None`` for a missing/blank one. Deliberately
    permissive (any non-empty stringable id passes): the callers use it for
    envelope honesty and dedupe keys, never as a drill target."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now() -> datetime:
    """The clock every age in this module is measured against.

    A named indirection, not ceremony: ages are the whole subject here, and a
    test that cannot pin "now" can only assert that an age exists, never that
    the right one was printed. Every public entry also takes an explicit
    ``now=`` for callers that already hold one.
    """
    return datetime.now(tz=timezone.utc)


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a row's ``produced_at`` to an aware UTC datetime.

    Accepts a datetime (naive treated as UTC — the substrate stores UTC) or an
    ISO-8601 string (asyncpg without a codec, and every test fixture). Anything
    else yields ``None``, which every caller renders as "unknown" rather than
    guessing an age.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def human_datetime(value: Any) -> str | None:
    """``"18 August 07:00 UTC"`` — the house date form, or ``None``.

    ``_tradecraft.NO_INSTRUMENT_READINGS`` tells every composition to render
    dates "as a human would ... NEVER a raw ISO or microsecond timestamp", and
    the dated-claim rule tells it never to COMPUTE a date. Both are satisfied
    only if the prompt SHOWS the human form — then printing it is a copy, which
    is the one thing the faithfulness contract always permits.
    """
    dt = _as_datetime(value)
    if dt is None:
        return None
    utc = dt.astimezone(timezone.utc)
    return f"{utc.day} {_MONTHS[utc.month - 1]} {utc:%H:%M} UTC"


def human_date(value: Any) -> str | None:
    """``"7 August 2026"`` — the house date form WITHOUT a time, or ``None``.

    The date-only sibling of :func:`human_datetime`, added for FRAME-2's window
    ledger: a ledger line records WHICH DAY a desk established something over a
    fortnight, and printing 07:00 UTC beside a two-week-old assertion invites a
    precision the line does not carry. The year is included because a 14-day
    window can straddle a New Year and "7 January" would then be ambiguous by
    exactly the amount that matters.

    Same locale-independence discipline as :func:`human_datetime` (see
    :data:`_MONTHS`): a prompt whose dates change language with the container's
    ``LANG`` is a fabrication risk, because the model copies what it is shown.
    """
    dt = _as_datetime(value)
    if dt is None:
        return None
    utc = dt.astimezone(timezone.utc)
    return f"{utc.day} {_MONTHS[utc.month - 1]} {utc.year}"


def head_age_hours(row: Mapping[str, Any], *, now: datetime | None = None) -> float | None:
    """Hours between a head's ``produced_at`` and ``now`` (never negative).

    ``None`` when the row carries no parsable timestamp — an unknown age is
    stated as unknown, never as zero (a zero would read as "composed just now",
    which is exactly the lie this train exists to stop).
    """
    produced = _as_datetime(row.get("produced_at"))
    if produced is None:
        return None
    anchor = now or _now()
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0.0, (anchor - produced).total_seconds() / 3600.0)


def format_age_hours(hours: float | None) -> str:
    """``"42h"``; ``"312h (13d)"`` past two days (a 336h horizon reaches 14)."""
    if hours is None:
        return "unknown"
    if hours >= 48.0:
        return f"{hours:.0f}h ({hours / 24.0:.0f}d)"
    return f"{hours:.0f}h"


def age_suffix(row: Mapping[str, Any], *, now: datetime | None = None) -> str:
    """The ``read_date=... age=...`` tail appended to a rendered head's
    attribution line, or ``""`` when the row has no usable timestamp.

    Empty-on-unknown keeps a timestampless row's render byte-identical to the
    pre-FRAME-1 form rather than printing ``age=unknown`` noise.
    """
    hours = head_age_hours(row, now=now)
    human = human_datetime(row.get("produced_at"))
    if hours is None or human is None:
        return ""
    return f' read_date="{human}" age={format_age_hours(hours)}'


def head_ages_stamp(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    horizon_hours: int | None = None,
) -> dict[str, Any] | None:
    """The ``data.head_ages`` envelope stamp (FRAME_PROGRAM §6.1).

    Per-head hours + the max, derived from rows already in hand at render time
    (zero extra queries). ``None`` when NO row carries a parsable timestamp —
    an absent stamp and a stamp reading 0.0 are different facts, and the §6
    gauge keys on the difference (no stamp ⇒ that composition is simply not
    gauged, never gauged as fresh).
    """
    heads: list[dict[str, Any]] = []
    ages: list[float] = []
    for row in rows[:HEAD_AGES_CAP]:
        hours = head_age_hours(row, now=now)
        if hours is None:
            continue
        ages.append(hours)
        heads.append(
            {
                "analyst_id": str(row.get("analyst_id") or ""),
                "target_id": (
                    str(row["target_id"]) if row.get("target_id") is not None else None
                ),
                "age_h": round(hours, 2),
            }
        )
    if not ages:
        return None
    stamp: dict[str, Any] = {
        "max_h": round(max(ages), 2),
        "min_h": round(min(ages), 2),
        "heads": heads,
    }
    if horizon_hours is not None:
        stamp["horizon_h"] = int(horizon_hours)
    return stamp


def max_head_age_hours(
    rows: Sequence[Mapping[str, Any]], *, now: datetime | None = None
) -> float | None:
    """The oldest consumed head's age, or ``None`` when nothing is datable."""
    ages = [
        h
        for h in (head_age_hours(r, now=now) for r in rows)
        if h is not None
    ]
    return max(ages) if ages else None


# ---------------------------------------------------------------------------
# FRAME-1 — the NEWEST FLOOR-PASSING head (the GB-drone class)
# ---------------------------------------------------------------------------


def head_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """The ``(analyst_id, target_id)`` identity the head-fold folds on."""
    return (
        str(row.get("analyst_id") or ""),
        str(row.get("target_id") or ""),
    )


def units_missing_from_basis(
    basis_rows: Sequence[Mapping[str, Any]],
    periphery_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Source analyst ids that have a PERIPHERY head but NO basis head.

    These are exactly the dimensions the floor withheld this cycle — the
    candidates for a newest-floor-PASSING fallback. Returned sorted so the
    follow-up query's parameters (and therefore its plan and its trace) are
    deterministic.
    """
    in_basis = {head_key(r)[0] for r in basis_rows if r.get("analyst_id")}
    missing = {
        str(r["analyst_id"])
        for r in periphery_rows
        if r.get("analyst_id") and str(r["analyst_id"]) not in in_basis
    }
    return sorted(missing)


def select_floor_fallback(
    candidates: Sequence[Mapping[str, Any]],
    basis_rows: Sequence[Mapping[str, Any]],
    periphery_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Pick the newest floor-PASSING head for each unit the floor withheld.

    ``candidates`` is the same admissibility query as the basis gather with the
    ``superseded_by IS NULL`` predicate DROPPED — so it already yields the
    newest PASSING head per ``(analyst_id, target_id)``, superseded or not. All
    this does is keep the ones for keys the basis does not already cover, and
    stamp each with the NEWER FAILING head it is standing in for.

    The stamp is the honesty: without it the render would show a five-day-old
    read as though it were this cycle's, which is a different lie from the one
    we are fixing. With it, the basis says "newest read that cleared the floor,
    18 August" while the periphery shows the newer failing read of 20 August,
    dated and scored — the two halves the ATTRIBUTION table asked for.

    Pure (no DB, no clock): the caller supplies the three row sets.
    """
    covered = {head_key(r) for r in basis_rows}
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in periphery_rows:
        by_key.setdefault(head_key(row), row)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in candidates:
        key = head_key(row)
        if key in covered or key in seen:
            continue
        newer = by_key.get(key)
        if newer is None:
            # No newer FAILING head for this unit: nothing was withheld, so
            # this candidate is not a fallback at all — admitting it would
            # silently widen the basis beyond the head-fold's one-head rule.
            continue
        newer_age = _as_datetime(newer.get("produced_at"))
        this_age = _as_datetime(row.get("produced_at"))
        if newer_age is not None and this_age is not None and this_age >= newer_age:
            # The "fallback" is not older than the failing head — the live head
            # must already be this row. Nothing to route.
            continue
        seen.add(key)
        promoted = dict(row)
        promoted[FLOOR_FALLBACK_KEY] = {
            "newer_head_id": _coerce_uuid_text(newer.get("id")),
            "newer_head_produced_at": (
                newer_age.isoformat() if newer_age is not None else None
            ),
            "newer_head_read_date": human_datetime(newer.get("produced_at")),
            "newer_head_effective_confidence": _as_float(
                newer.get("effective_confidence")
            ),
            "newer_head_status": (
                "unverified"
                if newer.get("faithfulness_score") is None
                else "below_floor"
            ),
        }
        out.append(promoted)
    return out


def floor_fallback_suffix(row: Mapping[str, Any]) -> str:
    """The basis attribution tail naming a fallback row for what it is."""
    meta = row.get(FLOOR_FALLBACK_KEY)
    if not isinstance(meta, Mapping):
        return ""
    when = meta.get("newer_head_read_date") or "(undated)"
    status = meta.get("newer_head_status") or "below_floor"
    eff = _as_float(meta.get("newer_head_effective_confidence"))
    eff_part = f" at effective_confidence {eff:.2f}" if eff is not None else ""
    return (
        " status=newest_read_that_cleared_the_floor"
        f" (a NEWER read of {when} is {status}{eff_part} and appears in the"
        " weakly-supported section below — this block is NOT the desk's"
        " latest read)"
    )


# ---------------------------------------------------------------------------
# FRAME-1 — the COVERAGE LEDGER (§4.2/§4.3: the floor's action, as data)
# ---------------------------------------------------------------------------

COVERAGE_IN_BASIS: str = "in_basis"
COVERAGE_BELOW_FLOOR: str = "below_floor"
COVERAGE_UNVERIFIED: str = "unverified"
COVERAGE_NO_HEAD: str = "no_head_in_horizon"


def build_coverage_ledger(
    roster: Sequence[str],
    basis_rows: Sequence[Mapping[str, Any]],
    periphery_rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Per declared unit: in basis / below floor / unverified / no head at all.

    ``roster`` is the subscription-resolved source-analyst set (the runtime's
    ``options['source_analyst_ids']``) — the ONLY honest denominator. Deriving
    the roster from the rows that arrived would make a missing unit invisible,
    which is the exact defect: the model inferred "gap" for a dimension it was
    simply never shown, and inferred it for floor-withheld dimensions too.

    Order follows ``roster`` so the rendered accounting is stable across runs.
    """
    basis_by_unit: dict[str, Mapping[str, Any]] = {}
    for row in basis_rows:
        aid = str(row.get("analyst_id") or "")
        if aid and aid not in basis_by_unit:
            basis_by_unit[aid] = row
    peri_by_unit: dict[str, Mapping[str, Any]] = {}
    for row in periphery_rows:
        aid = str(row.get("analyst_id") or "")
        if aid and aid not in peri_by_unit:
            peri_by_unit[aid] = row

    ledger: list[dict[str, Any]] = []
    for unit in roster:
        unit_id = str(unit)
        row = basis_by_unit.get(unit_id)
        status = COVERAGE_IN_BASIS
        if row is None:
            row = peri_by_unit.get(unit_id)
            if row is None:
                ledger.append({"unit": unit_id, "status": COVERAGE_NO_HEAD})
                continue
            status = (
                COVERAGE_UNVERIFIED
                if row.get("faithfulness_score") is None
                else COVERAGE_BELOW_FLOOR
            )
        hours = head_age_hours(row, now=now)
        entry: dict[str, Any] = {
            "unit": unit_id,
            "status": status,
            "read_date": human_datetime(row.get("produced_at")),
            "age_h": None if hours is None else round(hours, 2),
        }
        eff = _as_float(row.get("effective_confidence"))
        if eff is not None:
            entry["effective_confidence"] = round(eff, 3)
        if row.get(FLOOR_FALLBACK_KEY):
            entry["floor_fallback"] = True
        ledger.append(entry)
    return ledger


def render_coverage_ledger_block(
    ledger: Sequence[Mapping[str, Any]],
    *,
    horizon_hours: int | None,
    floor: float | None,
    max_age_hours: float | None,
) -> str:
    """The HEAD WINDOW block: the horizon, the staleness duty, the accounting.

    Rendered as a DIRECTIVE (no ``[[ref:N]]`` ordinal, like the region/desk
    coverage blocks) — it is the composition's own bookkeeping about what it was
    shown, not a new piece of evidence, and minting an ordinal for it would put
    a fabricated anchor in the citation space.

    Returns ``""`` for an empty ledger with nothing to disclose, so a run that
    knows neither its roster nor its horizon renders byte-identically to the
    pre-FRAME-1 prompt.
    """
    lines: list[str] = []
    if horizon_hours is not None:
        days = horizon_hours / 24.0
        lines.append(
            f"HEAD WINDOW — this read admits each unit's NEWEST head produced "
            f"within the trailing {int(horizon_hours)}h ({days:.0f} days). The "
            "window is an ADMISSIBILITY HORIZON, not a claim of freshness: a "
            "shown block may be days old, and each one carries its own "
            "read_date and age."
        )
    if max_age_hours is not None and max_age_hours >= STALE_HEAD_DISCLOSE_HOURS:
        lines.append(
            "STALENESS — the oldest read you were shown is "
            f"{format_age_hours(max_age_hours)} old. Say so plainly in the read "
            '(e.g. "the desk\'s newest read on this is two days old") rather '
            "than writing as if every block were composed today; anchor every "
            "temporal statement on the blocks' own read_dates, never on the "
            "time you are running."
        )
    if ledger:
        floor_txt = (
            f"{float(floor):.2f}" if isinstance(floor, (int, float)) else "the floor"
        )
        lines.append(
            "COVERAGE LEDGER (deterministic — this IS the accounting for your "
            "'## Coverage' line; use these words, do not re-derive them):"
        )
        for entry in ledger:
            lines.append(_coverage_ledger_line(entry, floor_txt=floor_txt))
        lines.append(
            "A unit listed BELOW VERIFICATION FLOOR has a read — it did not "
            "clear verification. Name it as \"below verification floor\" with "
            "its date; NEVER call it \"no read this cycle\", an unassessed gap, "
            "or unobserved. ONLY a unit listed with NO READ is a gap. Never "
            "band, score or characterise the state of a unit in either of those "
            "two classes."
        )
    if not lines:
        return ""
    return "\n".join(["", *lines])


def _coverage_ledger_line(entry: Mapping[str, Any], *, floor_txt: str) -> str:
    unit = str(entry.get("unit") or "(unknown unit)")
    status = str(entry.get("status") or "")
    when = entry.get("read_date") or "(undated)"
    age = format_age_hours(_as_float(entry.get("age_h")))
    if status == COVERAGE_NO_HEAD:
        return f"- {unit}: NO READ within the horizon — an unassessed gap."
    if status == COVERAGE_BELOW_FLOOR:
        eff = _as_float(entry.get("effective_confidence"))
        eff_part = f", scored {eff:.2f} against the {floor_txt} floor" if eff is not None else ""
        return (
            f"- {unit}: BELOW VERIFICATION FLOOR — newest read {when} "
            f"({age} old){eff_part}. Not a gap."
        )
    if status == COVERAGE_UNVERIFIED:
        return (
            f"- {unit}: NOT VERIFIED — newest read {when} ({age} old) never "
            "passed a faithfulness verify. Not a gap."
        )
    fallback = " (the newest read that CLEARED the floor)" if entry.get("floor_fallback") else ""
    return f"- {unit}: in basis — read of {when}, {age} old{fallback}."


# ---------------------------------------------------------------------------
# The C-TIER PERIPHERY GATHER + the FRAME-1 fallback gather (moved 2026-08-20)
# ---------------------------------------------------------------------------
# The DB side of the two tiers, here with the selection and render it feeds so
# the whole "what the floor withheld" question lives in one module. Both are
# read-only and take the connection they were handed.


async def read_periphery_findings(
    conn,  # type: ignore[no-untyped-def]
    *,
    analyst_ids: Sequence[str],
    time_window_hours: int,
    floor: float,
    limit: int = 32,
    target_id: str | None = None,
    target_ids: Sequence[str] | None = None,
    include_meta: bool = False,
) -> list[dict[str, Any]]:
    """C-TIER — gather the PERIPHERY tier: what the basis bar EXCLUDED.

    The exact COMPLEMENT of the :func:`read_other_analyst_findings`
    ``verify_floor`` admissibility over the same scope (same analyst set,
    window, target scope, meta filter, head-fold dedupe, coerce-tag drop),
    inverted on the verify leg:

      * LEFT (not INNER) join to the latest ``Faithfulness verify%`` critique —
        an UNVERIFIED head is periphery (claim-bearing but ungraded), not
        invisible;
      * admitted iff ``v.faithfulness_score IS NULL`` (unverified) OR
        ``LEAST(confidence, faithfulness) < floor`` (verify-scored below the
        bar) — i.e. exactly the rows the basis gather refuses;
      * coerce-fallback (``unstructured``/``coerce_failed``) rows stay excluded
        OUTRIGHT — a garbage body is not claim-bearing signal, it is noise;
      * ``effective_confidence`` is NULL for an unverified row (an explicit
        CASE — SQL ``LEAST`` ignores NULLs, which would otherwise launder a raw
        confidence into a verified-looking score), so an ungraded head can
        never raise a ceiling or masquerade as verified.

    Every returned row is stamped ``_evidence_tier='periphery'`` +
    ``_evidence_floor=<floor>`` so the DB-less ``_run`` partitions on data, not
    env. The DB fetch is head-folded + capped at ``limit``; the worst-first
    PERIPHERY_CAP selection happens in the pure :func:`_select_periphery` so
    the ordering rule is unit-testable without a database.
    """
    if not analyst_ids:
        return []

    params: list[Any] = [list(analyst_ids), int(time_window_hours)]
    where: list[str] = [
        "f.kind = 'finding'",
        "f.analyst_id = ANY($1::TEXT[])",
        "f.produced_at > NOW() - make_interval(hours => $2)",
        "f.superseded_by IS NULL",
    ]
    if not include_meta:
        where.append("(f.data -> 'data' ->> 'meta') IS DISTINCT FROM 'true'")
    if target_id is not None:
        params.append(str(target_id))
        where.append(f"f.target_id = ${len(params)}")
    elif target_ids is not None:
        params.append([str(t) for t in target_ids])
        where.append(f"f.target_id = ANY(${len(params)}::TEXT[])")
    params.append(float(floor))
    where.append(
        "(v.faithfulness_score IS NULL"
        f" OR LEAST(f.confidence, v.faithfulness_score) < ${len(params)})"
    )
    where.append(
        "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE"
    )

    sql = f"""
    SELECT * FROM (
        SELECT DISTINCT ON (f.analyst_id, f.target_id)
               f.id, f.kind, f.title, f.body, f.confidence, f.severity, f.data,
               f.target_id, f.target_version, f.analyst_id, f.analyst_version,
               f.produced_at, f.derived_from, f.schema_uri, f.run_id,
               CASE WHEN v.faithfulness_score IS NULL THEN NULL
                    ELSE LEAST(f.confidence, v.faithfulness_score)
               END AS effective_confidence,
               v.faithfulness_score AS faithfulness_score
        FROM analyst_outputs f
        LEFT JOIN LATERAL (
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
        ORDER BY f.analyst_id, f.target_id, f.produced_at DESC, f.id DESC
    ) dedup
    ORDER BY dedup.produced_at DESC
    LIMIT {int(limit)}
    """
    rows = await conn.fetch(sql, *params)
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row[_EVIDENCE_TIER_KEY] = PERIPHERY_TIER
        row[_EVIDENCE_FLOOR_KEY] = float(floor)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# FRAME-1 — the newest floor-PASSING head (§3 floor-interplay, GB-drone class)
# ---------------------------------------------------------------------------


async def read_floor_fallback_heads(
    conn,  # type: ignore[no-untyped-def]
    *,
    basis_reader: Any,
    analyst_ids: Sequence[str],
    time_window_hours: int,
    floor: float,
    basis_rows: Sequence[Mapping[str, Any]],
    periphery_rows: Sequence[Mapping[str, Any]],
    target_id: str | None = None,
    include_meta: bool = False,
) -> list[dict[str, Any]]:
    """Route the newest floor-PASSING head into BASIS for a withheld unit.

    The defect, from the round's ATTRIBUTION H-FLOOR table: the GB drone
    dimension's freshest head failed verify at eff 0.40 while an in-horizon
    PRIOR head at 0.571 would have passed. It was unreachable because the basis
    gather requires ``superseded_by IS NULL`` and the failing head had
    superseded it — so the dimension appeared in NEITHER tier and the coverage
    rule then called it an unassessed gap. The alternative (keep newest-only and
    let the dimension drop) was rejected in §3: it manufactures exactly the
    false-absence class, and showing BOTH — a dated passing read in the basis, a
    dated newer failing read in the periphery — is strictly more honest than
    showing neither.

    Cost discipline: the follow-up query fires ONLY for the analyst ids that
    have a periphery head and no basis head (usually none — on a healthy desk
    every unit clears the floor), and reuses the basis admissibility verbatim
    with ``include_superseded=True``. Best-effort like every other enrichment on
    this path: any failure logs and yields no rows, never a broken slice.

    ``basis_reader`` is ``meta_findings_synthesizer.read_other_analyst_findings``,
    passed in rather than imported: this module is a LEAF (the synthesizer imports
    it, never the reverse), and naming the basis gather as this function's own
    parameter states what the fallback IS — the same admissibility query with one
    predicate dropped — instead of hiding it behind a call-time import.
    """
    wanted = units_missing_from_basis(basis_rows, periphery_rows)
    if not wanted:
        return []
    try:
        candidates = await basis_reader(
            conn,
            analyst_ids=[a for a in analyst_ids if a in set(wanted)] or wanted,
            time_window_hours=time_window_hours,
            limit=len(wanted) * 4,
            target_id=target_id,
            verify_floor=floor,
            include_meta=include_meta,
            include_superseded=True,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never break a compose
        logger.warning(
            "meta.composition.floor_fallback_failed units=%s err=%s", wanted, exc
        )
        return []
    promoted = select_floor_fallback(candidates, basis_rows, periphery_rows)
    if promoted:
        logger.info(
            "meta.composition.floor_fallback units=%s — the newest head that "
            "cleared the floor is in BASIS; the newer failing head stays in "
            "periphery, dated",
            [r.get("analyst_id") for r in promoted],
        )
    return promoted


def _stamp_horizon(
    rows: list[dict[str, Any]], time_window_hours: int
) -> list[dict[str, Any]]:
    """Denormalize the ADMISSIBILITY HORIZON onto every returned row.

    The ``_region_coverage`` / ``_freshness`` idiom: the DB-less ``_run`` has no
    descriptor, so a fact resolved at read time reaches it by riding the rows.
    ``_run`` reads it from the first row that carries it, so a legacy/direct
    caller that never stamps renders byte-identically.
    """
    for row in rows:
        row[HORIZON_ROW_KEY] = int(time_window_hours)
    return rows


__all__ = [
    "COVERAGE_BELOW_FLOOR",
    "COVERAGE_IN_BASIS",
    "COVERAGE_NO_HEAD",
    "COVERAGE_UNVERIFIED",
    "FLOOR_FALLBACK_KEY",
    "HEAD_AGES_CAP",
    "HORIZON_ROW_KEY",
    "MAX_TITLE_CHARS",
    "PERIPHERY_BODY_CHARS",
    "PERIPHERY_CAP",
    "PERIPHERY_TIER",
    "STALE_HEAD_DISCLOSE_HOURS",
    "_CHILD_REF_MARKER_RE",
    "_EVIDENCE_FLOOR_KEY",
    "_EVIDENCE_TIER_KEY",
    "_MONTHS",
    "_SEVERITY_RANK",
    "_as_datetime",
    "_defuse_child_ref_markers",
    "_stamp_horizon",
    "_periphery_ids",
    "_render_periphery_block",
    "_row_body_excerpt",
    "_row_severity_delta",
    "_row_severity_level",
    "_row_severity_rank",
    "_row_tags",
    "_select_periphery",
    "age_suffix",
    "build_coverage_ledger",
    "floor_fallback_suffix",
    "format_age_hours",
    "head_age_hours",
    "head_ages_stamp",
    "head_key",
    "human_date",
    "human_datetime",
    "max_head_age_hours",
    "read_floor_fallback_heads",
    "read_periphery_findings",
    "render_coverage_ledger_block",
    "select_floor_fallback",
    "units_missing_from_basis",
]
