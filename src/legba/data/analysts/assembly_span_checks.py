# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DESK-TIER span checks: the quoted BLUF against the signals IT cites (lane N-D).

The assembly quotes a desk head's lead sentence verbatim, and the arms prove
the quotation is a faithful slice of the head. Quote fidelity means QUOTED
faithfully — it says nothing about whether the head's own sentence agrees with
its sources. Two deterministic checks close the gap at construction, over the
head's OWN ``data.data.citations`` (the text the desk was shown):

  * ``number_unsupported`` — a number in the span that no signal its markers
    cite states with the same qualifying words. RU 470c27a4: "across eight
    Ukrainian regions [11][51][17][120]" while CBC [51] says "10 Ukrainian
    regions" (its "eight" is RUSSIAN regions) — caught; "162 drones" — passes.
  * ``actor_mismatch`` — a citation that names none of the span's actors, or
    reaches it only through the desk's own country while naming countries the
    span never mentions. US 4e230ea2's US-Iran BLUF citing a Zelensky /
    Russia-Ukraine item [18] — caught.

Both ANNOTATE: the block carries ``span_checks`` (it rides the world carry
unchanged, ``assembly_carry.carry_block`` deep-copies the block) and the page
renders it beside the quotation. Nothing is dropped and no rank moves.

Only SIGNAL-backed citations count as evidence ("every number must appear in a
signal the span cites"): a ``prior_read`` grounding marker is the desk's own
earlier sentence, and a number resting only on it is exactly what this check
exists to surface.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..provenance.span_evidence_checks import (
    CHECKS_VERSION,
    actor_mismatched_citations,
    lexicon_from_gazetteer,
    unsupported_numbers,
)
from .source_independence import head_citations

__all__ = ["NUMBER_UNSUPPORTED", "ACTOR_MISMATCH", "span_checks"]

NUMBER_UNSUPPORTED = "number_unsupported"
ACTOR_MISMATCH = "actor_mismatch"

_ORDINAL_RE = re.compile(r"[\[【](\d{1,3})[\]】]")


def _ordinal(citation: Mapping[str, Any]) -> str | None:
    m = _ORDINAL_RE.search(str(citation.get("marker") or ""))
    if m:
        return str(int(m.group(1)))
    o = citation.get("ordinal")
    return str(int(o)) if isinstance(o, int) else None


def _cited_text(citation: Mapping[str, Any]) -> str | None:
    """The signal text the desk was shown, or ``None`` for a non-signal ref."""
    if not citation.get("signal_id"):
        return None
    parts: list[str] = []
    for key in ("title", "snippet", "source_text"):
        v = citation.get(key)
        if isinstance(v, str) and v.strip() and v not in parts:
            parts.append(v)
    return "\n".join(parts) or None


def _self_key(target_id: Any) -> str | None:
    from ..provenance.absence_slice import _country_desk_slug

    return _country_desk_slug(str(target_id) if target_id else None)


def span_checks(span: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``blocks[].span_checks`` entry for one quoted span of ``row``.

    ``None`` when the head carries no citation bridge at all (nothing to check
    against — stated by absence, never as a pass). Otherwise a dict that is
    present even when clean, so "checked, nothing found" and "not checked"
    read differently on the page.
    """
    citations = head_citations(row)
    if not citations:
        return None
    by_ordinal: dict[str, Mapping[str, Any]] = {}
    for c in citations:
        o = _ordinal(c)
        if o is not None and o not in by_ordinal:
            by_ordinal[o] = c
    text = str(span.get("text") or "")

    def resolve(ref: str) -> str | None:
        c = by_ordinal.get(ref)
        return _cited_text(c) if c is not None else None

    def counts(refs: list[str]) -> list[float]:
        srcs = {str(by_ordinal[r].get("source_id") or "") for r in refs if r in by_ordinal}
        return [float(len(refs)), float(len(srcs - {""}))]

    numbers = unsupported_numbers(text, resolve, computed_counts=counts)
    for n in numbers:
        n["markers"] = [f"[{r}]" for r in n.pop("refs")]

    actors: list[dict[str, Any]] = []
    lex_applicable = True
    try:
        from ..gazetteer import gazetteer

        lex = lexicon_from_gazetteer(gazetteer())
    except Exception:  # noqa: BLE001 — a profile without a gazetteer
        lex = None
    if lex is None:
        lex_applicable = False
    else:
        markers = [m.strip("[]") for m in span.get("markers") or []]
        pairs = [
            (f"[{o}]", t) for o in markers
            if (t := resolve(o)) is not None
        ]
        self_key = _self_key(row.get("target_id"))
        actors = actor_mismatched_citations(
            text, pairs, lex,
            self_key=self_key if self_key in lex.names else None,
        )

    flags = [
        name for name, hit in ((NUMBER_UNSUPPORTED, numbers), (ACTOR_MISMATCH, actors))
        if hit
    ]
    return {
        "version": CHECKS_VERSION,
        "flags": flags,
        NUMBER_UNSUPPORTED: numbers,
        ACTOR_MISMATCH: actors,
        "actor_check_applicable": lex_applicable,
    }
