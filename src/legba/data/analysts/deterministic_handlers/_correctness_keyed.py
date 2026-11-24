# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""INST-1 — THE KEYED REFERENCE, graded EXACTLY: present or absent, by key.

A reference built by ``reference_deterministic`` is not an account of a window
assembled by a model; it is a MACHINE-READABLE LIST (a catalog of exploited
vulnerabilities, a designations list) cut to one subject and one window, every
item carrying the KEY the list is indexed by (a CVE id, an entity id). Against
such a reference the question "does the reference bear on this claim, and does
it bear it out" has an exact answer that needs no model: does the claim name a
key of the list's kind, and is that key on the list?

The reference says it is keyed in its own JSON (``ref_json["keyed"]``, written
by :mod:`_reference_keyed`): the key pattern, whether the list declares itself
COMPLETE for its window, and the matcher. This module grades one claim at a
time under a closed rule set, and the rules ARE the instrument:

  =====================  ==================  ==========================  =========
  the claim names        the claim negates   the key is ON the list      label
  =====================  ==================  ==========================  =========
  no key of this kind    —                   —                           silent
  a key                  no                  yes                         contains
  a key                  yes                 yes                         contradicts
  a key                  no                  no, list COMPLETE           contradicts
  a key                  yes                 no, list COMPLETE           contains
  a key                  either              no, list NOT complete       silent
  =====================  ==================  ==========================  =========

"Negates" is a CLOSED token set (:data:`NEGATION_RE`) read over the whole
claim — deliberately crude and deliberately written down: a claim the closed
set misreads is a claim this matcher mislabels, and that is visible in the
ledger (every row carries the key, the rule and the matched item) rather than
hidden inside a model. An absence from a list that does NOT declare itself
complete is never evidence of anything — KEV lists exploited vulnerabilities,
it does not certify the rest unexploited — so it reads ``silent``.

ONE FAMILY, NAMED. The rows are written under family ``D0`` with model
``deterministic:key_match/1`` and cost 0. They are honestly single-family (one
instrument stood behind each label) and say so; what they are not is a model's
reading, and the ``families`` block names the matcher so no reader can take it
for one. The labels are the rubric's own (``contains`` / ``contradicts`` /
``silent``), so a keyed share and a model-graded share mean the same thing —
and they never pool by accident, because ``families.keyed`` marks the row.

PURE: no connection, no clock, no model.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

#: The key ``ref_json`` carries the keyed block under.
KEYED_KEY = "keyed"
#: The matcher's identity, versioned: a change to the rule table is a new one.
MATCHER = "key_match/1"
#: The family the keyed rows are written under, and its "model".
FAMILY = "D0"
MODEL = f"deterministic:{MATCHER}"

#: The closed negation set (see the banner).
NEGATION_RE = re.compile(
    r"\b(?:no|not|never|none|neither|nor|without|denied|denies|deny|"
    r"unconfirmed|absent)\b|n't\b",
    re.IGNORECASE,
)

LABEL_CONTAINS = "contains"
LABEL_CONTRADICTS = "contradicts"
LABEL_SILENT = "silent"


def keyed_spec(reference: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The reference's keyed block, or ``None`` when it is not a keyed
    reference (or its pattern does not compile — refused, never guessed)."""
    if not isinstance(reference, Mapping):
        return None
    block = reference.get(KEYED_KEY)
    if not isinstance(block, Mapping):
        return None
    pattern = str(block.get("key_pattern") or "")
    if not pattern:
        return None
    try:
        re.compile(pattern)
    except re.error:
        return None
    return dict(block)


def is_keyed(reference: Mapping[str, Any] | None) -> bool:
    """Is this a keyed (deterministic) reference?"""
    return keyed_spec(reference) is not None


def normalise_key(key: Any) -> str:
    """A key as it is compared: stripped, upper-cased."""
    return str(key or "").strip().upper()


def reference_index(reference: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{key: development}`` over the reference's keyed developments."""
    out: dict[str, dict[str, Any]] = {}
    for dev in reference.get("ref_developments") or []:
        if not isinstance(dev, Mapping):
            continue
        key = normalise_key(dev.get("key"))
        if key and key not in out:
            out[key] = dict(dev)
    return out


def claim_keys(text: str, pattern: str) -> list[str]:
    """The keys a claim names, in order of appearance, once each."""
    found = re.findall(pattern, str(text or ""), flags=re.IGNORECASE)
    keys = [normalise_key(k if isinstance(k, str) else k[0]) for k in found]
    return list(dict.fromkeys(k for k in keys if k))


def label_claim(
    text: str, spec: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """One claim's row, in the shape a model family's row has."""
    keys = claim_keys(text, str(spec.get("key_pattern") or ""))
    complete = bool(spec.get("complete"))
    row: dict[str, Any] = {
        "model": MODEL, "cost_usd": 0.0, "retried": False,
        "span_unverified": False, "span_failure": None,
        "core_claim": str(text or "")[:300], "decisive_span": "",
        "keys": keys,
    }
    if not keys:
        row.update(verdict=LABEL_SILENT, rule="no_key",
                   reason="the claim names no key of this reference's kind")
        return row
    negated = bool(NEGATION_RE.search(str(text or "")))
    on = [k for k in keys if k in index]
    if on:
        item = index[on[0]]
        row.update(
            verdict=LABEL_CONTRADICTS if negated else LABEL_CONTAINS,
            rule="on_list_negated" if negated else "on_list",
            decisive_span=str(item.get("decisive_span") or ""),
            matched_item=str(item.get("item_id") or ""),
            reason=f"{on[0]} is on {item.get('outlet') or 'the list'} in the window",
        )
        return row
    if complete:
        row.update(
            verdict=LABEL_CONTAINS if negated else LABEL_CONTRADICTS,
            rule="off_complete_list_negated" if negated else "off_complete_list",
            reason=(f"{keys[0]} is not on a list declared complete for the window"),
        )
        return row
    row.update(
        verdict=LABEL_SILENT, rule="off_incomplete_list",
        reason=(f"{keys[0]} is not on the list, and the list does not declare "
                "itself complete — absence is not evidence"),
    )
    return row


def grade_claims(
    claims: Sequence[Mapping[str, Any]],
    reference: Mapping[str, Any],
    *,
    ceiling_usd: float,
    spent_usd: float,
    pipeline_version: str,
    rubric: str,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    """``({"D0": {claim_id: row}}, ledger)`` — the same shapes the model path
    returns, so adjudication and persistence are the grader's own, unchanged."""
    spec = keyed_spec(reference) or {}
    index = reference_index(reference)
    rows = {
        str(c["id"]): label_claim(str(c.get("text") or ""), spec, index)
        for c in claims
    }
    ledger: dict[str, Any] = {
        "ceiling_usd": ceiling_usd,
        "spent_before_run_usd": round(float(spent_usd), 6),
        "run_cost_usd": 0.0,
        "capped_out": False,
        "capped_before": None,
        "calls": {FAMILY: len(rows)},
        "families_used": [FAMILY] if rows else [],
        "skipped_families": {
            "F0": "keyed reference — graded exactly by key, no model called",
            "F2": "keyed reference — graded exactly by key, no model called",
            "F3": "keyed reference — graded exactly by key, no model called",
        },
        "keyed": {
            "matcher": MATCHER,
            "key_pattern": spec.get("key_pattern"),
            "complete": bool(spec.get("complete")),
            "n_keys": len(index),
        },
        "pipeline_version": pipeline_version,
        "rubric": rubric,
    }
    return ({FAMILY: rows} if rows else {}), ledger


def family_block(
    by_family: Mapping[str, Mapping[str, Mapping[str, Any]]],
    claim_ids: Sequence[str],
    ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    """The ``families`` jsonb for one unit graded by key, and its cost (0)."""
    wanted = set(claim_ids)
    mine = [r for cid, r in (by_family.get(FAMILY) or {}).items() if cid in wanted]
    labels: dict[str, int] = {}
    for row in mine:
        label = str(row.get("verdict") or "")
        labels[label] = labels.get(label, 0) + 1
    block: dict[str, Any] = {
        "ceiling_usd": ledger.get("ceiling_usd"),
        "capped_out": False,
        "skipped": dict(ledger.get("skipped_families") or {}),
        "pipeline_version": ledger.get("pipeline_version"),
        "rubric": ledger.get("rubric"),
        "keyed": dict(ledger.get("keyed") or {}),
        FAMILY: {
            "model": MODEL,
            "model_configured": MODEL,
            "component": None,
            "n_calls": len(mine),
            "labels": dict(sorted(labels.items())),
            "span_unverified": 0,
            "cost_usd": 0.0,
        },
        "single_family": True,
    }
    return block, 0.0


__all__ = [
    "FAMILY",
    "KEYED_KEY",
    "MATCHER",
    "MODEL",
    "NEGATION_RE",
    "claim_keys",
    "family_block",
    "grade_claims",
    "is_keyed",
    "keyed_spec",
    "label_claim",
    "normalise_key",
    "reference_index",
]
