# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H11 — what the voice LEFT OUT, as published numbers (2026-09-24).

The judge grades every prose span the Assessment wrote and the deterministic
pass marks six classes of overreach inline — but nothing measured COVERAGE: a
voice that never mentions a carried block, or demotes the record's lead in
prose without contradicting a span, scored the same as one that weighed
everything. The reflect step reported ``markers`` and ``cited_ordinals`` and
never compared them with the blocks the record carried.

This module computes that comparison, once, from the three things the channel
already has — the spine payload, the resolved markers and the body — and hands
back one dict the payload publishes as ``assessment.coverage`` and the receipt
step repeats. No gate, no score change: a published number, like the aperture
counters, so a read that cites 3 of 8 blocks SAYS so.

  * ``blocks_carried``          — blocks on the spine.
  * ``blocks_cited``            — distinct ordinals the body names.
  * ``blocks_uncited``          — the ordinals never named, in order.
  * ``uncited_labels``          — those blocks' targets (world tier: the
                                  countries) or desks, so the omission has a
                                  name and not just a number.
  * ``blocks_cited_mass_share`` — the cited blocks' share of the record's own
                                  cited mass (reviewer amendment (a): an
                                  omission of the heaviest block costs more
                                  than one of the lightest), ``None`` when the
                                  record carries no mass to share.
  * ``lead_named``              — reviewer amendment (g): whether the FIRST
                                  claim sentence (the BLUF) names every lead
                                  ordinal the record's lead test decided —
                                  both co-leads, the one crown — ``None`` when
                                  the record crowned nothing.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .assessment_unsupported import segment_sentences

__all__ = ["COVERAGE_VERSION", "coverage_of", "first_claim_sentence"]

COVERAGE_VERSION: str = "coverage.v1"

_REF_RE = re.compile(r"\[\[ref:(\d+)\]\]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_BOLD_ONLY_RE = re.compile(r"^\s*(?:[-*]\s+)?\*\*[^*]+\*\*\s*[:.]?\s*$")


def first_claim_sentence(body: str) -> str:
    """The first sentence that is a claim: not a heading, not a bold-only
    headline or label. The BLUF's own sentence in the house body shape."""
    for sentence in segment_sentences(str(body or "")):
        text = sentence.text
        if _HEADING_RE.match(text) or _BOLD_ONLY_RE.match(text):
            continue
        if not re.search(r"[A-Za-z]", text):
            continue
        return text
    return ""


def _label(block: Mapping[str, Any]) -> str:
    target = block.get("target_id")
    if target:
        return str(target)
    return str(block.get("desk") or block.get("analyst_id") or "")


def coverage_of(
    payload: Mapping[str, Any],
    markers: Sequence[Mapping[str, Any]],
    body: str,
) -> dict[str, Any]:
    """The coverage block. Pure; never raises on a thin payload."""
    blocks = [b for b in (payload.get("blocks") or []) if isinstance(b, Mapping)]
    by_ordinal: dict[int, Mapping[str, Any]] = {}
    for block in blocks:
        try:
            by_ordinal[int(block.get("ordinal"))] = block
        except (TypeError, ValueError):
            continue
    cited: set[int] = set()
    for marker in markers:
        try:
            cited.add(int(marker.get("ordinal")))
        except (TypeError, ValueError):
            continue
    carried = sorted(by_ordinal)
    uncited = [o for o in carried if o not in cited]

    def _mass(o: int) -> float:
        try:
            return float(by_ordinal[o].get("cited_mass") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    total_mass = sum(_mass(o) for o in carried)
    cited_mass = sum(_mass(o) for o in carried if o in cited)
    share = round(cited_mass / total_mass, 4) if total_mass > 0 else None

    lead = payload.get("lead") or {}
    lead_ordinals: list[int] = []
    for o in lead.get("block_ordinals") or []:
        try:
            lead_ordinals.append(int(o))
        except (TypeError, ValueError):
            continue
    lead_named: bool | None = None
    if lead_ordinals:
        first = first_claim_sentence(body)
        named = {int(m.group(1)) for m in _REF_RE.finditer(first)}
        lead_named = all(o in named for o in lead_ordinals)

    return {
        "version": COVERAGE_VERSION,
        "blocks_carried": len(carried),
        "blocks_cited": len([o for o in carried if o in cited]),
        "blocks_uncited": uncited,
        "uncited_labels": [_label(by_ordinal[o]) for o in uncited],
        "blocks_cited_mass_share": share,
        "lead_kind": str(lead.get("kind") or "none"),
        "lead_ordinals": lead_ordinals,
        "lead_named": lead_named,
    }
