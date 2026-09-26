"""THE ASSEMBLY RENDER — `body` and `title`, both deterministic (D-1 §1.8, F-9).

The body is RENDERED FROM the payload, never authored beside it. It is not a
second source of truth: ``render_assembly_body(payload)`` is byte-stable, and
every world-claim sentence in it is a ``spans[].text`` verbatim.

THE CLOSED CONNECTIVE VOCABULARY. The tier's own voice in the body is restricted
to :data:`CONNECTIVES` — section headers, the generated attribution line, the
tension templates, the coverage and ledger lines, and the ordinal markers.
**No connective in the vocabulary may assert a state of the world**, and the
list is pinned by a test the way ``BANNED_PHRASE_MARKERS`` is today. That is the
whole demotion in one mechanism: the tier may say "these two reads disagree" and
may not say "tension is rising".

WHAT IS RETIRED HERE, AND WHAT IS DELIBERATELY KEPT.
``_tradecraft.COMPOSITION_BODY_SHAPE``'s "connected argument" clause,
``_shape_rule``, ``_hedge_rule``-as-a-voice-instruction and the legacy
``_SYSTEM_PROMPT``'s *"DO NOT re-state any individual finding verbatim"* — the
exact INVERSE of the new contract — do not apply on this path. They are kept in
full for the Assessment path (D-6), which still writes prose. That split is what
makes the retirement safe: nothing is deleted, one path stops reading it.

THE ONE PLACE THE BODY IS NOT BYTE-VERBATIM, stated rather than hidden. A span
cut from a LOWER COMPOSITION tier (a region or country read) can itself contain
``[[ref:N]]`` markers, which would collide with THIS tier's ordinal space and be
resolved as this read's own citations. The render therefore passes quoted text
through the house's existing ``_defuse_child_ref_markers`` (``[[ref:3]]`` →
``(child ref 3)``), which is a no-op on the overwhelming common case — a
first-order desk body never contains ``[[ref:``. The CANONICAL text is
``spans[].text`` in the payload, which is byte-identical to the origin body and
is what every quote check reads.

THE TITLE (F-9, ratified). ``analyst_outputs.title`` is NOT NULL and is the page
``<h1>``; VOICE §4.3 calls the hole "the title hole in D-1". It is closed
deterministically: a short prefix plus the lead span's own quoted fragment when
the lead is EARNED, a shape line naming the co-lead subjects when it is not, and
the bare masthead on a flat day. It carries no authored claim, so it needs no
title rubric — and the model no longer decides whether concentration was earned,
because the test decides and hands it the verdict as a fact.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .assembly_payload import (
    CONNECTIVE_VOCABULARY_VERSION,
    LEAD_CO_LEADS,
    LEAD_EARNED_SINGLE,
    TIER_COUNTRY,
    TIER_THEMATIC,
    TIER_WORLD,
)
from .assembly_spans import quoted_spans
from .contrary_tension import TENSION_KIND_CONTRARY, counter_refs
from .composition_window import MAX_TITLE_CHARS, _defuse_child_ref_markers
from .source_independence import SINGLE_SOURCE_MARKER, WIRE_FOLDED_LABEL
from .unit_names import payload_names, unit_label

__all__ = [
    "CONNECTIVES",
    "MAX_TITLE_FRAGMENT_CHARS",
    "assembly_title",
    "render_assembly_body",
    "quoted_text",
]

#: The CLOSED vocabulary. Every fixed string the render may emit, exactly as it
#: emits it. A test pins this list against the rendered output of a corpus, so a
#: new connective cannot arrive without a deliberate line in the diff — and the
#: reviewer's one question is the only one that matters: DOES IT ASSERT A STATE
#: OF THE WORLD? Every entry below answers no. They describe the READ (what was
#: carried, what was dropped, what was checked), never the world.
CONNECTIVES: tuple[str, ...] = (
    "**BLUF:**",
    "## The record",
    "## Tension",
    "## Coverage",
    "## Not carried",
    "## What this read did not see",
    "assembled from",
    "verified desk read",
    "verified desk reads",
    "shown and not carried",
    "reads carried below, in order",
    "lead this cycle, weighted and uncrowned",
    "No single read leads this cycle",
    "no conflicting pair was detected among the",
    "shown blocks",
    "pairs examined",
    "in basis",
    "below floor",
    "not verified",
    "no read inside the horizon",
    "rank",
    "cited mass",
    "verify",
    "severity",
    "sources",
    "not a candidate for this surface",
    # 7d — THE INDEPENDENCE MARKS. Both describe the READ's sourcing and
    # neither asserts a state of the world: "this block rests on one source"
    # and "this many outlets were the same dispatch". Spelled here as the
    # render emits them; the definitions live in ``source_independence`` and a
    # test pins the two equal, the way ``LEAD_CONNECTIVE_PHRASES`` is pinned.
    #
    # WHY ``CONNECTIVE_VOCABULARY_VERSION`` DOES NOT MOVE: the version is
    # stamped into every body, so bumping it would rewrite every read ever
    # rendered — including the ones this change is required to leave
    # byte-identical. Both entries are GUARDED (they render only on a block
    # the payload marked), the vocabulary's SHAPE is unchanged, and a read
    # with nothing to mark is byte-for-byte the read v1 always produced.
    SINGLE_SOURCE_MARKER,
    WIRE_FOLDED_LABEL,
    # 7a — THE COUNTER-EVIDENCE FOOT. Every entry describes a RETRIEVAL — what
    # was fetched, when, from where, under which hash — and the disclaimer says
    # in so many words that nothing here is adjudicated. None asserts a state of
    # the world; the world-claim, if there is one, is on the page we cite and is
    # carried as that page's own quoted words inside the tension bullet.
    #
    # ``CONNECTIVE_VOCABULARY_VERSION`` DOES NOT MOVE, for the reason the 7d
    # block above states: the version is stamped into every body, and bumping it
    # would rewrite every read ever rendered — including the ones this change is
    # required to leave byte-identical. The whole section is GUARDED on the
    # payload carrying a ``contrary_retrieval`` tension entry, and a payload
    # with none is byte-for-byte the read that ships today.
    "## Counter-evidence",
    "Pages this platform fetched because they state the opposite of a claim",
    "Retrieved on the free rung; not adjudicated",
    "no publication date stated",
    "counter-query",
    "sha256",
)

#: How much of the lead span the title may quote. 120 keeps the whole title
#: inside ``MAX_TITLE_CHARS`` (200) once the masthead prefix and the quote marks
#: are counted, and a fragment shorter than a clause reads as a stub.
MAX_TITLE_FRAGMENT_CHARS: int = 120

_TIER_MASTHEAD: dict[str, str] = {
    TIER_COUNTRY: "Country read",
    TIER_WORLD: "World read",
    TIER_THEMATIC: "Cross-desk read",
}


def quoted_text(span: Mapping[str, Any]) -> str:
    """A span's text as it appears in the BODY.

    Byte-identical to ``spans[].text`` except for the child-marker defuse
    documented in the module banner — which is a no-op on every first-order
    desk body, i.e. on essentially every span this tier carries.
    """
    return _defuse_child_ref_markers(str(span.get("text") or ""))


def _subjects(blocks: Sequence[Mapping[str, Any]], ordinals: Sequence[int]) -> list[str]:
    """The display subjects of the given ordinals — target names when the read
    spans several targets, desk names when it does not. Deterministic either
    way; the choice is a fact about the block set, not a preference."""
    by_ordinal = {int(b.get("ordinal") or 0): b for b in blocks}
    targets = {str(b.get("target_id") or "") for b in blocks}
    use_target = len(targets) > 1
    out: list[str] = []
    for o in ordinals:
        b = by_ordinal.get(int(o))
        if b is None:
            continue
        out.append(
            (
                unit_label(b.get("target_name"), b.get("target_id"))
                or str(b.get("desk") or "")
            )
            if use_target
            else str(b.get("desk") or "")
        )
    return out


def _lead_fragment(blocks: Sequence[Mapping[str, Any]], ordinal: int) -> str:
    """The lead span's own opening fragment, cut on a word boundary."""
    for b in blocks:
        if int(b.get("ordinal") or 0) != int(ordinal):
            continue
        spans = quoted_spans(b)
        if not spans:
            return ""
        text = " ".join(str(spans[0].get("text") or "").split())
        if len(text) <= MAX_TITLE_FRAGMENT_CHARS:
            return text
        cut = text[:MAX_TITLE_FRAGMENT_CHARS].rsplit(" ", 1)[0]
        return f"{cut}…"
    return ""


def assembly_title(payload: Mapping[str, Any]) -> str:
    """The row's ``title`` — deterministic, and carrying no authored claim.

    Three shapes, one per lead verdict (D-1 §1.5.3 / F-9):

      * ``earned_single`` — masthead + the lead span's own QUOTED fragment. The
        only claim in it is one the desk already made and the record already
        quotes.
      * ``co_leads`` — a shape line naming the co-lead subjects. It says the day
        had several threads; it does not rank them.
      * ``none`` — the masthead plus the two counts. This is the honest reading
        of a flat day, and it is a shape the current title contract can produce
        but rarely does: the world title crowns a single driver on 93.1% of
        reads today, on a key whose standard deviation is 0.024.
    """
    blocks = list(payload.get("blocks") or [])
    lead = payload.get("lead") or {}
    tier = str(payload.get("tier") or "")
    as_of = str(payload.get("as_of") or "")[:10]
    masthead = _TIER_MASTHEAD.get(tier, "Composed read")
    if tier == TIER_COUNTRY and blocks:
        masthead = str(
            blocks[0].get("target_name") or blocks[0].get("target_id") or masthead
        )
    prefix = f"{masthead}, {as_of}" if as_of else masthead

    kind = str(lead.get("kind") or "")
    ordinals = [int(o) for o in (lead.get("block_ordinals") or [])]
    if kind == LEAD_EARNED_SINGLE and ordinals:
        fragment = _lead_fragment(blocks, ordinals[0])
        if fragment:
            return f"{prefix} — “{fragment}”"[:MAX_TITLE_CHARS]
    if kind == LEAD_CO_LEADS and ordinals:
        subs = [s for s in _subjects(blocks, ordinals) if s]
        if subs:
            joined = ", ".join(subs)
            return f"{prefix} — {len(subs)} threads lead: {joined}"[
                :MAX_TITLE_CHARS
            ]
    dropped = int(((payload.get("drops") or {}).get("counts") or {}).get(
        "shown_not_carried", 0
    ) or 0)
    n = len(blocks)
    reads = "read" if n == 1 else "reads"
    return (
        f"{prefix} — {n} verified desk {reads}, {dropped} not carried"
    )[:MAX_TITLE_CHARS]


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _attribution(block: Mapping[str, Any]) -> str:
    """The GENERATED attribution line. Every field is a column on the origin
    head, so D-3's arm 3 is a regression test on this function rather than a
    check on a model — which is what kills the R3 §4.2 specimen (an assessment
    attributed to a "proliferation-watch desk" that does not exist) outright.
    """
    v = block.get("verify") or {}
    sal = block.get("salience") or {}
    corr = block.get("corroboration") or {}
    # 7d — THE SINGLE-SOURCE MARK, beside the ordinal. The payload decides (it
    # is the one that saw the cited signals); this line only renders what the
    # payload marked, so the body stays a projection of the payload rather
    # than a second opinion about it. Absent key ⇒ absent mark: a block whose
    # origin cites findings has an UNKNOWN source count, not a single one.
    ref = f"[[ref:{block.get('ordinal')}]]"
    if corr.get("single_source"):
        ref = f"{ref} {SINGLE_SOURCE_MARKER}"
    parts = [
        ref,
        str(block.get("desk") or ""),
        unit_label(block.get("target_name"), block.get("target_id")),
        str(block.get("produced_at") or "")[:19],
    ]
    tail = [
        f"severity {block.get('severity') or 'unstated'}",
        f"verify {_fmt(v.get('overall_score'))}",
        f"cited mass {_fmt(sal.get('cited_mass'))}",
    ]
    # Only when the block's origin cites WIRE items. A composition origin cites
    # findings, so its source count is not zero — it is UNKNOWN at this depth,
    # and "0 sources" beside a real read is a false statement rather than a
    # missing one. Say nothing instead.
    if str((block.get("salience") or {}).get("source") or "") == "cited_signals":
        tail.append(f"{int(corr.get('n_sources') or 0)} sources")
        # 7d — ``n_sources`` keeps counting OUTLETS, so the fold is stated
        # rather than silently subtracted from it. "3 sources · 2 wire-folded"
        # is the honest shape: three mastheads, one voice. A number that moved
        # with no line saying why is the class of defect this piece exists to
        # end, so the count never moves — the explanation arrives beside it.
        if corr.get("wire_folded"):
            tail.append(f"{int(corr['wire_folded'])} {WIRE_FOLDED_LABEL}")
    if str(block.get("tier") or "") == "periphery":
        tail.append("below floor")
    return " · ".join([p for p in parts if p] + tail)



def _counter_evidence_lines(payload: Mapping[str, Any]) -> list[str]:
    """The ``## Counter-evidence`` foot — 7a, and EMPTY unless one was retrieved.

    One entry per ``contrary_retrieval`` tension bullet, numbered in the order
    the bullets cite them, so ``[counter:N]`` in the Tension section resolves to
    a URL, a publication date, the retrieval time, the hash of the bytes this
    platform holds and the counter-query that found it.

    NOTHING HERE ADJUDICATES, and the note says so rather than leaving a reader
    to infer it. A retrieved page that states the opposite of a carried read is
    a fact about the retrieval; which side is right is a judgement this tier
    does not make.

    A MISSING PUBLICATION DATE RENDERS AS ABSENCE. The fetch leg DISCOVERS a
    date from the document and never invents one, so a page that states none
    gets "no publication date stated" — never today's date, and never a blank
    that reads as though nobody looked.
    """
    refs = counter_refs(payload)
    if not refs:
        return []
    lines = [
        "## Counter-evidence",
        "",
        "*Pages this platform fetched because they state the opposite of a "
        "claim above. Retrieved on the free rung; not adjudicated.*",
        "",
    ]
    for ref in refs:
        published = str(ref.get("published_at") or "")
        when = (
            f"published {published[:19]}" if published
            else "no publication date stated"
        )
        digest = str(ref.get("sha256") or "")
        lines.append(
            f"- [counter:{ref.get('ordinal')}] "
            f"{ref.get('counter_url') or '(no url)'} — {when} — retrieved "
            f"{str(ref.get('retrieved_at') or '')[:19]} — sha256 "
            f"{digest[:16] or 'not recorded'} — counter-query: "
            f"{ref.get('query') or 'not recorded'}"
        )
    lines.append("")
    return lines

def render_assembly_body(payload: Mapping[str, Any]) -> str:
    """The row's ``body``, rendered from ``payload``. Byte-stable.

    Section order is fixed. The ordinal markers are ``[[ref:N]]`` so the
    existing CITE resolution — unchanged — mints exactly one citation per block,
    which is what makes ``|blocks| == |citations| == |distinct markers|`` true by
    construction rather than by inspection.
    """
    blocks = list(payload.get("blocks") or [])
    lead = payload.get("lead") or {}
    drops = payload.get("drops") or {}
    counts = drops.get("counts") or {}
    lines: list[str] = []

    n = len(blocks)
    reads = "verified desk read" if n == 1 else "verified desk reads"
    lines.append(
        f"*As of {str(payload.get('as_of') or '')[:19]}; assembled from {n} "
        f"{reads}; {int(counts.get('shown_not_carried') or 0)} shown and not "
        f"carried.*"
    )
    lines.append("")

    kind = str(lead.get("kind") or "")
    ordinals = [int(o) for o in (lead.get("block_ordinals") or [])]
    if kind == LEAD_EARNED_SINGLE and ordinals:
        by_ordinal = {int(b.get("ordinal") or 0): b for b in blocks}
        b = by_ordinal.get(ordinals[0])
        span = (quoted_spans(b) or [{}])[0] if b else {}
        lines.append(f"**BLUF:** {quoted_text(span)} [[ref:{ordinals[0]}]]")
    elif kind == LEAD_CO_LEADS and ordinals:
        refs = ", ".join(f"[[ref:{o}]]" for o in ordinals)
        lines.append(
            f"**BLUF:** {len(ordinals)} reads lead this cycle, weighted and "
            f"uncrowned: {refs}."
        )
    else:
        lines.append(
            f"**BLUF:** No single read leads this cycle; {n} reads carried "
            f"below, in order."
        )
    lines.extend(["", "## The record", ""])

    for b in blocks:
        subject = unit_label(b.get("target_name"), b.get("target_id"))
        lines.append(
            f"### {b.get('ordinal')} — {b.get('question')}"
            + (f" ({subject})" if subject else "")
        )
        # P3-A: QUOTED spans only. A country block additionally carries its
        # origin head in FULL (role ``context_body``) for the fenced reader; the
        # RECORD quotes the lead sentence and nothing else, and that is the whole
        # demotion. ``quoted_spans`` is the identity on every block with no
        # context span, so this render is byte-for-byte what it has always been —
        # pinned by ``test_country_assessment_p3::test_the_record_body_is_byte_
        # identical_with_context_spans_present``.
        for span in quoted_spans(b):
            for para in quoted_text(span).split("\n"):
                lines.append(f"> {para}" if para.strip() else ">")
        lines.append("")
        lines.append(_attribution(b))
        lines.append("")

    lines.append("## Tension")
    lines.append("")
    checked = payload.get("tension_checked") or {}
    tensions = list(payload.get("tensions") or [])
    if tensions:
        # 7a — a RETRIEVED other side takes a plain ``[counter:N]`` ordinal,
        # never a ``[[ref:N]]``. The assembly mints exactly one ``[[ref:N]]``
        # per carried block, which is what makes
        # ``|blocks| == |citations| == |distinct markers|`` true BY
        # CONSTRUCTION; a marker with no block behind it would break that
        # invariant to say something a plain ordinal says just as well.
        counter_n = 0
        for t in tensions:
            a = (t.get("a") or {}).get("ordinal")
            b_ord = (t.get("b") or {}).get("ordinal") if t.get("b") else None
            if t.get("kind") == TENSION_KIND_CONTRARY:
                counter_n += 1
                ref_b = f"[counter:{counter_n}]"
            else:
                ref_b = (
                    f"[[ref:{b_ord}]]" if b_ord
                    else "a read that was not carried"
                )
            lines.append(f"- [[ref:{a}]] / {ref_b} — {t.get('statement')}")
    else:
        lines.append(
            f"- no conflicting pair was detected among the "
            f"{int(checked.get('blocks') or 0)} shown blocks "
            f"({int(checked.get('pairs_examined') or 0)} pairs examined; "
            f"{checked.get('scope_note')})."
        )
    lines.append("")

    coverage = list(payload.get("coverage") or [])
    if coverage:
        # Amendment 7f — the ledger keys units by SLUG (it is a machine
        # join), so the name comes off the payload's published map. Absent
        # map, absent name: the line is the slug it always was.
        names = payload_names(payload)
        lines.extend(["## Coverage", ""])
        for c in coverage:
            status = {
                "in_basis": "in basis",
                "below_floor": "below floor",
                "unverified": "not verified",
                "no_head_in_horizon": "no read inside the horizon",
            }.get(str(c.get("status") or ""), str(c.get("status") or ""))
            age = c.get("age_h")
            suffix = f", {_fmt(age, 1)}h old" if age is not None else ""
            unit = str(c.get("unit") or "")
            label = unit_label(names.get(unit), unit)
            lines.append(f"- {label}: {status}{suffix}")
        lines.append("")

    not_carried = list(drops.get("shown_not_carried") or [])
    lines.extend(["## Not carried", ""])
    if not_carried:
        for d in not_carried:
            lines.append(
                f"- rank {d.get('rank')}: {d.get('desk')} / "
                f"{unit_label(d.get('target_name'), d.get('target_id'))} — "
                f"cited mass {_fmt(d.get('cited_mass'))} — {d.get('why')}"
            )
    else:
        lines.append("- every read shown to this assembly was carried.")
    lines.append("")

    invisible = int(counts.get("invisible_heads") or 0)
    if counts.get("invisible_heads") and invisible:
        lines.extend([
            "## What this read did not see",
            "",
            f"- {invisible} desk reads were not a candidate for this surface "
            f"(below its target grain); they are one click away on the country "
            f"boards.",
            "",
        ])

    lines.extend(_counter_evidence_lines(payload))

    lines.append(f"<!-- connectives: {CONNECTIVE_VOCABULARY_VERSION} -->")
    return "\n".join(lines).rstrip() + "\n"
