# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — THE PACKET: one byte-identical grading item set, and the leak scan.

Ported from ``planning/PROGRAM1_2026-09-16/build_step2_packets.py``, shape for
shape. What every family sees is the SAME object: the note, the rubric verbatim,
the output contract READ OUT of the rubric, and one item per claim carrying an
assertion and the reduced reference. The only variable left is the family, which
is the entire point of building it once.

THE REFERENCE REDUCTION IS A WHITELIST. Exactly five fields per development
survive — ``item_id``, ``summary``, ``decisive_span``, ``outlet``,
``publish_date`` — plus the band table. The reference's ``header`` is round
apparatus; ``source_url`` / ``source_tier`` / ``significance`` / ``hindsight`` /
``gaps`` / ``dimension`` are the reference builder's own metadata, and ANNEX C
v4 rule 5 explicitly retires tier and hindsight from the grader's job. A
whitelist rather than a blacklist so a field nobody anticipated cannot ride in
on a reference written by another lane (R2 builds these; G1 only reads them).

THE LEAK SCAN is ``r4_common.leak_scan``, carried. It is NOT a substring search:
three tiers, each with its own argument.

  * KEYS — matched only in JSON KEY POSITION (``"regime":``). Four of the
    forbidden words (``assembly``, ``regime``, ``faithfulness``, ``coverage``)
    are ordinary English a country read legitimately uses; a bare-substring test
    on ``regime`` fired on all ten live packets in R4, every hit a political
    noun. A gate that cannot pass a clean packet is an alarm nobody reads.
  * IDENTIFIERS — snake_case / dotted machine names that are never English.
    Matched anywhere, because there is no innocent occurrence.
  * PATTERNS — multi-word render telemetry (``· verify 0.36``), anywhere.

A hit REFUSES the packet. The grader then records ``leak_scan_failed`` and
grades nothing, which is the honest outcome: an external family must never be
handed the platform's own telemetry about the read it is grading.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from ._correctness_rubric import (
    NOTE_TO_GRADER,
    OUTPUT_CONTRACT,
    RUBRIC_TEXT,
)

#: THE FIVE FIELDS, and nothing else.
REF_DEV_FIELDS: tuple[str, ...] = (
    "item_id", "summary", "decisive_span", "outlet", "publish_date",
)
#: What the reduction drops, named so the receipt can record the promise it keeps.
REF_DEV_DROPPED: tuple[str, ...] = (
    "source_url", "source_tier", "significance", "hindsight", "header", "gaps",
    "dimension", "archive", "fetch_ts", "corroboration", "notes",
)

ROUND = "LEGBA_CORRECTNESS_GRADER"


class PacketError(ValueError):
    """A packet this module refuses to ship. Loud, never a quiet substitution."""


# ---------------------------------------------------------------------------
# The leak scan (r4_common.leak_scan, carried)
# ---------------------------------------------------------------------------

FORBIDDEN_KEYS: tuple[str, ...] = (
    "assembly", "regime", "faithfulness", "coverage",
    "drops", "lead", "verify", "tensions", "blocks", "spans",
    "connectives", "tension_checked", "unsupported", "stamp",
)

FORBIDDEN_IDENTIFIERS: tuple[str, ...] = (
    "cited_mass", "overall_score", "shown_not_carried", "quote_fidelity",
    "assembly.v1", "earned_single", "co_leads", "n_candidates",
    "not_selected", "below_floor", "invisible_heads", "body_sha256",
    "scope_tokens", "effective_confidence", "checkable_claims",
    "supported_claims", "judge_status", "score_state", "fidelity_to_spine",
    "judge_pipeline_version", "no_head_in_horizon", "in_basis",
    "cap_trimmed", "shown_not_selected", "correlated_duplicate",
    "construction_failed", "cited_salience", "cited_magnitudes",
    "LEGBA_COMPOSITION_ASSEMBLY", "world_assessment", "world_assessor",
    "country_composition", "country_assessment",
    "escalation_composition", "region_rollup",
    "EXTERNAL_AUDIT_PIPELINE_VERSION", "external_audit", "standing_auditor",
)

FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("judge stamp literal", r"\b20\d{2}-\d{2}-\d{2}/\d+\b"),
    ("auditor critique title", r"\bExternal audit\b"),
    ("faithfulness score", r"\bfaithfulness\s+score\b"),
    ("quote fidelity", r"\bquote\s+fidelity\b"),
    ("render verify telemetry", r"·\s*verify\s+(?:n/a|\d)"),
    ("render cited-mass telemetry", r"·\s*cited mass\s+(?:n/a|\d)"),
    ("verify score inline", r"\bverify\s+score\b"),
)

_KEY_TMPL = r'"{k}"\s*:'


def leak_scan(text: str) -> list[dict[str, Any]]:
    """Every forbidden hit in ``text``, with its offset and a context window.

    Returns ``[]`` on a clean packet — the ONLY passing result. Case-insensitive
    throughout: a packet that leaks ``Assembly`` leaks exactly as much as one
    that leaks ``assembly``.
    """
    hits: list[dict[str, Any]] = []

    def add(kind: str, token: str, match: re.Match[str]) -> None:
        hits.append({
            "kind": kind,
            "token": token,
            "offset": match.start(),
            "context": text[max(0, match.start() - 40):match.end() + 40],
        })

    for key in FORBIDDEN_KEYS:
        for match in re.finditer(
            _KEY_TMPL.format(k=re.escape(key)), text, re.IGNORECASE
        ):
            add("payload-key", key, match)
    for ident in FORBIDDEN_IDENTIFIERS:
        for match in re.finditer(re.escape(ident), text, re.IGNORECASE):
            add("identifier", ident, match)
    for name, pattern in FORBIDDEN_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            add("pattern", name, match)
    return hits


def leak_summary(hits: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{"<kind>:<token>": count}`` — what a receipt can carry without the text.

    The CONTEXT is deliberately dropped here: it is the leaking text itself, and
    a receipt that quoted it would re-leak into the row that recorded the leak.
    """
    out: dict[str, int] = {}
    for hit in hits:
        key = f"{hit['kind']}:{hit['token']}"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# The reference
# ---------------------------------------------------------------------------


def reduce_reference(
    reference: Mapping[str, Any], country: str, lane: str = "A"
) -> dict[str, Any]:
    """``{ref_developments: [five fields …], ref_bands: …}`` and nothing else.

    ``item_id`` is the reference's own development id where it carries one;
    where it does not, it is derived by R3's convention (``RD-<CC>-<lane>-<n>``)
    — the same fallback the ratified v3/v4 packets used, so an id in a packet
    here means what it meant there.
    """
    developments: list[dict[str, Any]] = []
    for index, dev in enumerate(reference.get("ref_developments") or []):
        row = {field: dev.get(field) for field in REF_DEV_FIELDS}
        if not row.get("item_id"):
            row["item_id"] = f"RD-{country}-{lane}-{index + 1}"
        developments.append(row)
    return {
        "ref_developments": developments,
        "ref_bands": reference.get("ref_bands"),
    }


# ---------------------------------------------------------------------------
# The packet
# ---------------------------------------------------------------------------


def build_packet(
    claims: Sequence[Mapping[str, Any]],
    reference: Mapping[str, Any],
    *,
    packet_kind: str,
    rubric_text: str = RUBRIC_TEXT,
    note: str = NOTE_TO_GRADER,
    output_contract: str = OUTPUT_CONTRACT,
) -> dict[str, Any]:
    """One item per claim, ORDERED BY ID, every item carrying the same reference.

    One country, one window, one reference. Ordering by id (rather than by the
    read's own order) means position discloses nothing about which desk a claim
    came from beyond the grain its prefix already declares.
    """
    items: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = str(claim.get("id") or "")
        if not (claim_id.startswith("DR-") or claim_id.startswith("CR-")):
            raise PacketError(
                f"claim id {claim_id!r} is in neither registered namespace "
                "(DR- for a desk atom, CR- for a composition atom). Refusing to "
                "ship an atom whose grain the scorer cannot read off its id."
            )
        text = (claim.get("text") or "").strip()
        if not text:
            raise PacketError(
                f"claim {claim_id} has no grader-facing text — refusing to ship "
                "an empty assertion"
            )
        items.append({
            "p1_id": claim_id, "assertion": text, "reference": reference,
        })
    items.sort(key=lambda item: item["p1_id"])
    if len({item["p1_id"] for item in items}) != len(items):
        raise PacketError(
            "duplicate claim id in the packet — refusing to ship two atoms a "
            "grader cannot tell apart"
        )
    return {
        "round": ROUND,
        "packet": packet_kind,
        "note_to_grader": note,
        "rubric": rubric_text,
        "output_contract": output_contract,
        "items": items,
    }


def packet_text(packet: Mapping[str, Any]) -> str:
    """The packet's canonical bytes — what the sha256 is taken over."""
    return json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=False)


def packet_sha256(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(packet_text(packet).encode("utf-8")).hexdigest()


def scan_packet(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Leak-scan the packet MINUS the rubric field.

    The rubric is the one shared frozen document every family sees and it is not
    part of the leak surface. A ``.replace()`` would miss it — the embedded copy
    is JSON-escaped — so the field is DROPPED before scanning, exactly as the
    ratified builder does it.
    """
    without_rubric = {k: v for k, v in packet.items() if k != "rubric"}
    return leak_scan(json.dumps(without_rubric, ensure_ascii=False))


__all__ = [
    "FORBIDDEN_IDENTIFIERS",
    "FORBIDDEN_KEYS",
    "FORBIDDEN_PATTERNS",
    "PacketError",
    "REF_DEV_DROPPED",
    "REF_DEV_FIELDS",
    "ROUND",
    "build_packet",
    "leak_scan",
    "leak_summary",
    "packet_sha256",
    "packet_text",
    "reduce_reference",
    "scan_packet",
]
