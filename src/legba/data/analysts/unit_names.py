"""THE UNIT'S HUMAN NAME — the truthmaker a slug is not (Amendment 7f).

THE DEFECT, MEASURED. The spine row carries no human-readable name for any
unit: ``target_name == target_id`` on every block and every drop-ledger row of
every live world record (8 of 8 blocks and 50 of 50 drop rows on both
2026-09-08 spines). So the voice is handed ``country_watch_kp`` and asked to
write English about it, and on the 09-07 00:00Z replay it wrote **"Pakistan"**
three times across two arms — ``kp`` is North Korea, ``pk`` is Pakistan — and
the judge failed every one of those sentences, correctly: the country the voice
named appears nowhere in the evidence map, and neither does any name at all.

That is 3 of the 6 tension-relay sentences in the D-6 replay
(``planning/ASSESSMENT_TENSIONS_IN_MAP_REPORT.md`` §5.1), and it was the
dominant cause of a 0-for-6 on the relay. It could not be repaired inside the
Assessment channel, whose fence is the payload: **the name was never on the
payload to render**.

WHAT THIS MODULE IS. The one place that turns ``(name, slug)`` into the string
every surface prints, plus the template statement that used to live in
``assembly_payload`` and now needs the same pairing. It is PURE — no connection,
no query, no I/O — so the Assessment channel's fence is unmoved by importing it.

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
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "PAYLOAD_NAMES_KEY",
    "name_of",
    "payload_names",
    "published_names",
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


def unit_label(name: Any, slug: Any) -> str:
    """``"Watch — Pakistan (country_watch_pk)"`` — the name, then the handle.

    NAME FIRST because the sentence the voice writes is English and the name is
    the part it will reuse; the handle in parentheses is what the judge and the
    deterministic marker match on. See the module docstring for why neither half
    may be dropped.

    Degrades in both directions rather than printing an empty bracket: slug
    alone when there is no name (or the "name" IS the slug, which is what every
    shipped row carries), name alone when there is no slug.
    """
    n = str(name or "").strip()
    s = str(slug or "").strip()
    if n and s and n != s:
        return f"{n} ({s})"
    return s or n


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
    """
    an = _side_label(a)
    bn = _side_label(b)
    if b_carried:
        if an == bn:
            return (
                f"The {a.get('desk')} and {b.get('desk')} desks report on {an} in "
                f"the same window and point in opposite directions."
            )
        return (
            f"{a.get('desk')} on {an} and {b.get('desk')} on {bn} describe the same "
            f"dimension in the same window and point in opposite directions."
        )
    why = f" ({b_why})" if b_why else ""
    if an == bn:
        return (
            f"The carried {a.get('desk')} read on {an} points the opposite way "
            f"from the {b.get('desk')} read on {an}, which this record did NOT "
            f"carry{why} — the conflict is with a read below the cut, not with "
            f"a read this record published."
        )
    return (
        f"{a.get('desk')} on {an}, carried, describes the same dimension in the "
        f"same window as {b.get('desk')} on {bn}, which this record did NOT "
        f"carry{why}, and the two point in opposite directions — the conflict "
        f"is with a read below the cut, not with a read this record published."
    )
