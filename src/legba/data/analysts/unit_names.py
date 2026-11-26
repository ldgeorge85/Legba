"""THE UNIT'S HUMAN NAME — the truthmaker a slug is not (Amendment 7f).

THE DEFECT, MEASURED. The spine row carries no human-readable name for any
unit: ``target_name == target_id`` on every block and every drop-ledger row of
every live world record (8 of 8 blocks and 50 of 50 drop rows on both
2026-09-08 spines). So the voice is handed ``country_watch_kp`` and asked to
write English about it, and on the 09-07 00:00Z replay it wrote **"Pakistan"**
three times across two arms — ``kp`` is North Korea, ``pk`` is Pakistan — and
the judge failed every one of those sentences, correctly: the country the voice
named appears nowhere in the evidence map, and neither does any name at all.

That is 3 of the 6 tension-relay sentences in a replay, and it was the
dominant cause of a 0-for-6 on the relay. It could not be repaired inside the
Assessment channel, whose fence is the payload: **the name was never on the
payload to render**.

WHAT THIS MODULE IS. The one place that turns ``(name, slug)`` into the string
every surface prints, plus the template statement that used to live in
``assembly_payload`` and now needs the same pairing. It is PURE — no connection,
no query, and no I/O beyond the profile's packaged label style, read once per
process (``legba.data.subject``) — so the Assessment channel's fence is unmoved
by importing it.

The D-6 AST guard walks the PROMPT MODULE'S OWN source and bans the names
``conn`` / ``asyncpg`` / ``fetch`` / ``fetchrow`` / ``execute`` / ``requests``
there, so an import only puts the imported NAMES in front of it — which means
the guard alone would not stop a module that reached the substrate on the prompt
module's behalf. Purity here is what actually holds the fence, and it is stated
as a property of this module rather than left to that test to catch.

THE RESOLUTION happens once, upstream, where a connection legally exists:
``composition_slice.resolve_unit_names`` reads ``target_descriptors.name`` — the
same column and the same ``is_head`` predicate the region rollup's member roster
already uses (``composition_slice._REGION_MEMBER_ROSTER_SQL``) — and stamps the
map onto the slice rows. ``meta_findings_synthesizer`` hands it to
``build_assembly(target_names=...)``, which was already the parameter for this
and which nobody had ever passed.

BOTH HALVES RIDE, ALWAYS. :func:`unit_label` prints ``Watch — Pakistan
(country_watch_pk)`` and not one or the other, because the two readers need
different halves and neither can be dropped:

  * THE VOICE needs the human name, or it invents one. That is the whole defect.
  * THE JUDGE and ``assessment_unsupported`` match on the SLUG — the roster
    fence, ``aperture_unit_identifiers``' ledger-grounding exemption, the
    ``[[ref:N]]`` evidence map. Drop the slug and every one of those goes blind.

And with both in front of the voice, a mistranslation stops being an ungradeable
guess and becomes a CHECKABLE CONTRADICTION: "Pakistan" written beside a record
that says ``Watch — Korea, Democratic People's Republic of (country_watch_kp)``
is a sentence the judge can fail against the map, which is the point.

BYTE-IDENTITY IS THE FALLBACK CONTRACT. With no name resolved — a legacy row, a
direct caller, a descriptor with a NULL name, the flag off — :func:`unit_label`
returns the slug ALONE, byte for byte what shipped. Every surface below degrades
to its shipped bytes rather than to a parenthesis with nothing in it.

THE ONE PRODUCER AND THE ONE PARSER (W10 SUBJECT-1). A subject's display name
is itself a label — ``Watch — Pakistan``, a TIER and a NAME joined by a
separator — and two readers used to take it apart on their own:
``assessment_unsupported`` with a regex and ``edge_qualification`` with a
``split_part`` in SQL. Both now come from here, and the separator, the glyphs a
parser also accepts and the order are PROFILE data (``subjects.label`` on
``profile.yaml``, read through :func:`legba.data.subject.label_style`):
:func:`subject_display` joins, :func:`parse_unit_label` and :func:`label_name`
take apart, :func:`label_name_sql` renders the same rule for a SQL column. On
the exemplar's style every output is byte-identical to the code it replaced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..profile import LabelStyle
from ..subject import label_style

__all__ = [
    "PAYLOAD_NAMES_KEY",
    "UnitLabel",
    "label_name",
    "label_name_sql",
    "name_of",
    "parse_unit_label",
    "payload_names",
    "published_names",
    "subject_display",
    "tension_statement",
    "unit_label",
]

#: Where the resolved map is published on ``assembly.v1``. ADDITIVE — no shipped
#: field changes shape — and read back by every fenced consumer (the Assessment
#: prompt may reach nothing but the payload, so the map has to BE on the
#: payload; a lookup helper would be a second substrate).
PAYLOAD_NAMES_KEY: str = "unit_names"


def name_of(names: Mapping[str, str] | None, slug: Any) -> str:
    """The unit's human name, or its slug when nothing resolved.

    Never empty for a slug that exists, and never a name for a slug that does
    not: this is the function that keeps ``target_name`` a NAME while leaving it
    safe to render bare, which every shipped call site does.
    """
    key = str(slug or "").strip()
    if not key:
        return ""
    resolved = str((names or {}).get(key) or "").strip()
    return resolved or key


def unit_label(name: Any, slug: Any, *, tier: Any = None) -> str:
    """``"Watch — Pakistan (country_watch_pk)"`` — the name, then the handle.

    NAME FIRST because the sentence the voice writes is English and the name is
    the part it will reuse; the handle in parentheses is what the judge and the
    deterministic marker match on. See the module docstring for why neither half
    may be dropped.

    Degrades in both directions rather than printing an empty bracket: slug
    alone when there is no name (or the "name" IS the slug, which is what every
    shipped row carries), name alone when there is no slug.

    ``tier`` (optional) joins a bare name to its tier first, through
    :func:`subject_display` — so the whole ``Tier — Name (slug)`` label has one
    producer. Unset, ``name`` is taken as the display name it already is.
    """
    n = str(name or "").strip()
    if tier is not None:
        n = subject_display(tier, n)
    s = str(slug or "").strip()
    if n and s and n != s:
        return f"{n} ({s})"
    return s or n


@dataclass(frozen=True)
class UnitLabel:
    """A label taken apart: ``tier`` and ``name`` of the display name (``tier``
    empty when the label has no separator) and the ``slug`` in parentheses
    (empty when it carries none)."""

    tier: str
    name: str
    slug: str


def _style(style: LabelStyle | None) -> LabelStyle:
    return label_style() if style is None else style


def _split_re(style: LabelStyle) -> re.Pattern[str]:
    glyphs = (style.separator, *style.separator_aliases)
    return re.compile(r"\s+(?:" + "|".join(re.escape(g) for g in glyphs) + r")\s+")


def subject_display(tier: Any, name: Any, *, style: LabelStyle | None = None) -> str:
    """``"Watch — Pakistan"`` — a tier and a name joined in the profile's order
    by its separator. Either half missing, the other alone."""
    st = _style(style)
    parts = {"tier": str(tier or "").strip(), "name": str(name or "").strip()}
    first, second = (parts[k] for k in st.order)
    if first and second:
        return f"{first} {st.separator} {second}"
    return first or second


_SLUG_TAIL_RE = re.compile(r"^(?P<label>.*\S)\s+\((?P<slug>[^()\s]+)\)$")


def parse_unit_label(label: Any, *, style: LabelStyle | None = None) -> UnitLabel:
    """The inverse of :func:`unit_label` (with a tier): ``"Watch — Pakistan
    (country_watch_pk)"`` -> ``UnitLabel("Watch", "Pakistan",
    "country_watch_pk")``. A label with no separator is all name."""
    st = _style(style)
    text = str(label or "").strip()
    slug = ""
    m = _SLUG_TAIL_RE.match(text)
    if m:
        text, slug = m.group("label"), m.group("slug")
    pieces = _split_re(st).split(text)
    if len(pieces) < 2:
        return UnitLabel(tier="", name=text, slug=slug)
    if st.order[0] == "tier":
        return UnitLabel(tier=pieces[0], name=pieces[-1], slug=slug)
    return UnitLabel(tier=pieces[-1], name=pieces[0], slug=slug)


def label_name(display: Any, *, style: LabelStyle | None = None) -> str:
    """The NAME half of a display name (``"G20 — Argentina"`` ->
    ``"Argentina"``); the text itself when it has no separator. The tier is a
    frame, not a place: a sentence naming only ``G20`` has named no unit."""
    st = _style(style)
    pieces = _split_re(st).split(str(display or ""))
    return pieces[-1] if st.order[0] == "tier" else pieces[0]


def label_name_sql(column: str, *, style: LabelStyle | None = None) -> str:
    """:func:`label_name` as a SQL expression over ``column`` — the primary
    separator only, the caller trimming (``btrim``) as the Python's split
    trims."""
    st = _style(style)
    sep = st.separator
    if st.order[0] == "tier":
        half = f"reverse(split_part(reverse({column}), '{sep}', 1))"
    else:
        half = f"split_part({column}, '{sep}', 1)"
    return (
        f"CASE WHEN {column} LIKE '%{sep}%'\n"
        f"                  THEN {half}\n"
        f"                  ELSE {column} END"
    )


def payload_names(payload: Mapping[str, Any] | None) -> dict[str, str]:
    """The published slug→name map off an assembly payload.

    Absent on every pre-Amendment-7f row, so this returns ``{}`` and every
    caller falls back to the slug — the byte-identity path.
    """
    raw = (payload or {}).get(PAYLOAD_NAMES_KEY) or {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v}


def published_names(
    names: Mapping[str, str] | None, mentioned: Iterable[Any]
) -> dict[str, str]:
    """The map to PUBLISH: the resolved names of the units this record mentions.

    Restricted to ``mentioned`` — the roster, the blocks, the drop ledger, the
    coverage entries and the tension refs — rather than the whole descriptor
    table, for the reason the drop ledger is capped: this rides in every
    citation's ``evidence_text`` eight times over, and a name for a unit the
    record never names is bytes the reader pays for and nobody reads.

    A name identical to its own slug is DROPPED, not stored: it is what an
    unresolved unit already renders as, so storing it would put a redundant
    entry on the row and make the "no names resolved" case indistinguishable
    from the resolved-to-itself one. Sorted, so two runs over one input publish
    one map and ARM 4(a) diffs values rather than orderings.
    """
    src = names or {}
    out: dict[str, str] = {}
    for slug in mentioned:
        key = str(slug or "").strip()
        if not key or key in out:
            continue
        resolved = str(src.get(key) or "").strip()
        if resolved and resolved != key:
            out[key] = resolved
    return {k: out[k] for k in sorted(out)}


def _side_label(side: Mapping[str, Any]) -> Any:
    """One tension side as the sentence should name it.

    ``target_name``/``target_id`` are the pair :func:`unit_label` exists for;
    the desk is the shipped last resort for a side that has neither, and it is
    kept so a target-less row still produces the sentence it always did.
    """
    return unit_label(side.get("target_name"), side.get("target_id")) or side.get(
        "desk"
    )


def _matched_indicator(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    """`` (indicator X)`` when both sides carry a shared indicator id, else empty.

    The sentence may name only what the detector matched. A cross-unit pair is
    admitted on the same desk plus a shared term; it says "same dimension" (the
    desk's bounded question) and adds the indicator ONLY when a shared indicator id
    is on both blocks, so no sentence claims a variable nobody matched.
    """
    def _ids(side: Mapping[str, Any]) -> set[str]:
        raw = side.get("indicator_ids")
        if isinstance(raw, str):
            raw = [raw]
        return {i.strip() for i in (raw or ()) if isinstance(i, str) and i.strip()}

    ids = sorted(_ids(a) & _ids(b))
    return f" (indicator {', '.join(ids)})" if ids else ""


def tension_statement(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    b_carried: bool = True,
    b_why: str | None = None,
) -> str:
    """The TEMPLATE statement — declares, never explains.

    Moved here from ``assembly_payload`` (Amendment 7f) because its two
    substantive nouns are unit names, and naming them correctly is now this
    module's job. The sentence templates are UNCHANGED, byte for byte; what
    changed is that ``an``/``bn`` resolve through :func:`unit_label`, so a
    record with names says ``Watch — Sudan (country_watch_sd)`` where it used to
    say ``country_watch_sd``, and a record without names says exactly what it
    said before.

    ``b_carried=True`` is the pre-W-1 text, byte for byte: two carried blocks
    are symmetric and the sentence may be symmetric with them.

    ``b_carried=False`` is the CROSS-TIER form. It still declares and still does
    not explain — what it adds is one FACT ABOUT THE RECORD (the B side was not
    carried, and the why-class that says so), which is the whole difference
    between a conflict the record published and a conflict its own drop ledger
    is the evidence for. The why-class rides the sentence rather than sitting
    only in the payload because the rendered body is what the voice reads.

    N-C (2026-11-11) — THE "DIFFERENT DESKS" SENTENCES ARE GONE. They were
    reachable only on a cross-unit pair, and on world record fd6584eb the one that
    printed ("leadership_transition on country_g20_tr, carried, and
    country_composition on ... Burkina Faso ... are different desks") contradicted
    its own gate, which had compared leadership_transition with
    leadership_transition. A tension is now one entity and one variable
    (``tension_pairing`` rule 0) and the caller names the desk the gate compared,
    so a different-desk pair is either one unit (the first form of each arm) or
    never declared; a direct caller handing two units and two desks gets a
    sentence that names no dimension at all.
    """
    an = _side_label(a)
    bn = _side_label(b)
    if b_carried:
        if an == bn and a.get("desk") == b.get("desk"):
            return (
                f"Two {a.get('desk')} reads on {an}{_matched_indicator(a, b)} "
                f"describe the same dimension in the same window and point in "
                f"opposite directions."
            )
        if an == bn:
            return (
                f"The {a.get('desk')} and {b.get('desk')} desks report on {an} in "
                f"the same window and point in opposite directions."
            )
        if a.get("desk") != b.get("desk"):
            # Two units AND two desks: no dimension was matched, so none is
            # named (unreachable from ``_tensions`` since rule 0).
            return (
                f"{a.get('desk')} on {an} and {b.get('desk')} on {bn} point in "
                f"opposite directions in the same window."
            )
        return (
            f"{a.get('desk')} on {an} and {b.get('desk')} on {bn} describe the same "
            f"dimension{_matched_indicator(a, b)} in the same window and point in "
            f"opposite directions."
        )
    why = f" ({b_why})" if b_why else ""
    if an == bn:
        return (
            f"The carried {a.get('desk')} read on {an} points the opposite way "
            f"from the {b.get('desk')} read on {an}, which this record did NOT "
            f"carry{why} — the conflict is with a read below the cut, not with "
            f"a read this record published."
        )
    if a.get("desk") != b.get("desk"):
        return (
            f"{a.get('desk')} on {an}, carried, and {b.get('desk')} on {bn}, which "
            f"this record did NOT carry{why}, point in opposite directions in the "
            f"same window — the conflict is with a read below the cut, not with a "
            f"read this record published."
        )
    return (
        f"{a.get('desk')} on {an}, carried, describes the same dimension"
        f"{_matched_indicator(a, b)} in the same window as {b.get('desk')} on "
        f"{bn}, which this record did NOT carry{why}, and the two point in "
        f"opposite directions — the conflict is with a read below the cut, not "
        f"with a read this record published."
    )
