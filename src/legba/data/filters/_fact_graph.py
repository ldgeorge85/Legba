# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Predicate -> relation-label bucket for the ``fact_extractor`` stage.

This module used to carry the writer of the Apache AGE graph (``legba_graph``);
that graph held no row in production, was removed on 2026-11-15 (owner
decision, design note DATA_LAYERS_2026-11-14 section 6 item 6), and its writer
went with it. What stays is the one pure helper the extractor still uses to
bucket a free-text predicate into a small PascalCase relation label.

Unknown predicates fall back to ``CoOccursWith`` (the safe generic relation).
"""

from __future__ import annotations

# Predicate (lower-cased, trimmed) -> relation label. Reuses the NER predicate
# cue intent; everything unmapped -> CoOccursWith.
_PREDICATE_TO_EDGE: dict[str, str] = {
    "located in": "LocatedIn",
    "capital of": "LocatedIn",
    "headquarters location": "LocatedIn",
    "country": "LocatedIn",
    "country of citizenship": "LocatedIn",
    "country of origin": "LocatedIn",
    "member of": "MemberOf",
    "employer": "MemberOf",
    "part of": "PartOf",
    "subsidiary": "PartOf",
    "parent organization": "PartOf",
}

_GENERIC_EDGE = "CoOccursWith"


def edge_label_for_predicate(predicate: str) -> str:
    """Map a GLiREL relation predicate to a relation-label bucket (fallback)."""
    return _PREDICATE_TO_EDGE.get((predicate or "").lower().strip(), _GENERIC_EDGE)
