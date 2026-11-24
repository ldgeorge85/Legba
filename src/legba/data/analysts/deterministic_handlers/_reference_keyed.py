# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""INST-1 — a KEYED reference, built from a machine-readable list. Pure.

The web reference builder (``reference_builder``) has a model find and quote an
account of a window, under fences, because a newsroom's record is prose. Some
subjects have a record that is not prose at all: a published, machine-readable
LIST indexed by a key — CISA's Known Exploited Vulnerabilities catalog (keyed
by CVE id), a designations list (keyed by an entity id). For such a subject a
reference needs no model and no fence: cut the list to the subject and the
window, and every item IS a development, its record IS its span.

This module is the cut, as code over a parsed document:

* :class:`FeedSpec` — the list's shape, from the descriptor's options: where
  the items live (``items_path``), which field is the KEY and which regex finds
  that key in prose (``key_field`` / ``key_pattern``), which field dates an
  item (``date_field``), which field says which SUBJECT an item belongs to
  (``subject_field``), and whether the list declares itself COMPLETE for its
  window (``list_complete`` — the one statement that lets an ABSENCE grade).
* :func:`parse_bindings` — ``target_id=value|value`` lines: which subject
  takes which items (KEV's ``vendorProject``, a regime's programme code).
* :func:`select_items` — the subject's items inside the window.
* :func:`build_keyed_reference` — the ``unit_references.ref_json``, carrying a
  ``keyed`` block the grader reads (:mod:`_correctness_keyed`) and the
  subject's code / name / kind in its header.

The decisive span of a keyed development is the RECORD itself, serialised
canonically (sorted keys, no whitespace drift): re-fetching the list and
re-serialising the item reproduces it byte for byte, which is what "span
verified" means for a record that has no page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from ... import subject as SUBJECT

#: The builder label prefix written to ``unit_references.builder``.
BUILDER_PREFIX = "deterministic"
#: This kind's population stamp (a change to the cut is a new one).
PIPELINE_VERSION = "2026-10-05/1"
#: The item id's lane letter: ``RD-<CODE>-K-<n>`` (the web builder's is C).
ITEM_LANE = "K"


@dataclass(frozen=True)
class FeedSpec:
    """One machine-readable list's shape (the descriptor's feed options)."""

    feed_id: str
    url: str
    items_path: str
    key_field: str
    key_pattern: str
    date_field: str
    subject_field: str
    summary_fields: tuple[str, ...] = ()
    outlet: str = ""
    complete: bool = False
    source_tier: int = 1

    def problems(self) -> list[str]:
        """What is missing or malformed — an unbound spec builds nothing."""
        out = [
            name for name in ("feed_id", "url", "key_field", "key_pattern",
                              "date_field", "subject_field")
            if not str(getattr(self, name) or "").strip()
        ]
        if self.key_pattern:
            try:
                re.compile(self.key_pattern)
            except re.error as exc:
                out.append(f"key_pattern does not compile ({exc})")
        return out


def parse_bindings(raw: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """``["target_id=value|value", …]`` -> ``{target_id: (value, …)}``.
    A line without ``=`` or without a value is dropped (and counted by the
    caller, never guessed)."""
    out: dict[str, tuple[str, ...]] = {}
    for line in raw or ():
        target, sep, values = str(line).partition("=")
        target = target.strip()
        vals = tuple(v.strip() for v in values.split("|") if v.strip())
        if sep and target and vals:
            out[target] = vals
    return out


def _walk(doc: Any, path: str) -> Any:
    node = doc
    for part in [p for p in str(path or "").split(".") if p]:
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def parse_feed(body: str | bytes, spec: FeedSpec) -> list[dict[str, Any]]:
    """The list's items (JSON; ``items_path`` dotted, empty = the root).
    Raises ``ValueError`` for a document that is not the declared shape."""
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    doc = json.loads(text)
    items = _walk(doc, spec.items_path) if spec.items_path else doc
    if not isinstance(items, list):
        raise ValueError(
            f"{spec.feed_id}: items_path {spec.items_path!r} is not a list"
        )
    return [dict(i) for i in items if isinstance(i, Mapping)]


def item_date(item: Mapping[str, Any], field: str) -> date | None:
    """The item's own date (``YYYY-MM-DD`` or an ISO instant); ``None`` when it
    carries none — an undated item is out of every window."""
    raw = str(item.get(field) or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _subject_values(item: Mapping[str, Any], field: str) -> set[str]:
    raw = item.get(field)
    vals = raw if isinstance(raw, (list, tuple)) else [raw]
    return {str(v).strip().casefold() for v in vals if str(v or "").strip()}


def select_items(
    items: Sequence[Mapping[str, Any]],
    spec: FeedSpec,
    values: Sequence[str],
    *,
    window_start: date,
    window_end: date,
) -> list[dict[str, Any]]:
    """The subject's items (``subject_field`` equal to one of ``values``,
    case-folded) dated inside ``[window_start, window_end]``, oldest first,
    then by key — a total order, so a rebuild is byte-identical."""
    wanted = {v.strip().casefold() for v in values if v.strip()}
    out = []
    for item in items:
        if not (_subject_values(item, spec.subject_field) & wanted):
            continue
        when = item_date(item, spec.date_field)
        if when is None or not (window_start <= when <= window_end):
            continue
        out.append(dict(item))
    return sorted(out, key=lambda i: (
        item_date(i, spec.date_field), str(i.get(spec.key_field) or "")
    ))


def record_span(item: Mapping[str, Any]) -> str:
    """The record, canonically serialised — the development's span."""
    return json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def build_keyed_reference(
    *,
    target_id: str,
    identity: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    spec: FeedSpec,
    dimensions: Sequence[str],
    t0: datetime,
    window_start: datetime,
    feed_sha256: str,
) -> dict[str, Any]:
    """The ``unit_references.ref_json`` for one subject over one window."""
    code = str(identity.get("code") or target_id)
    prefix = f"RD-{SUBJECT.item_code(code)}-{ITEM_LANE}"
    developments = []
    for n, item in enumerate(items, 1):
        key = str(item.get(spec.key_field) or "").strip()
        when = item_date(item, spec.date_field)
        summary = " — ".join(
            str(item.get(f)).strip() for f in spec.summary_fields
            if str(item.get(f) or "").strip()
        ) or key
        developments.append({
            "item_id": f"{prefix}-{n}",
            "key": key,
            "summary": summary[:600],
            "decisive_span": record_span(item),
            "outlet": spec.outlet or spec.feed_id,
            "publish_date": when.isoformat() if when else None,
            "source_url": spec.url,
            "source_tier": int(spec.source_tier),
            "significance": "notable",
            "hindsight": False,
            "dimension": list(dimensions),
            "span_verified": True,
            "span_source": "record",
        })
    return {
        "header": {
            "target_id": target_id,
            "country": str(identity.get("name") or target_id),
            "country_code": code,
            "subject": {
                "id": target_id, "code": code,
                "name": str(identity.get("name") or target_id),
                "kind": str(identity.get("kind") or ""),
            },
            "t0": t0.isoformat(),
            "window": f"{window_start.isoformat()} -> {t0.isoformat()}",
            "builder": f"{BUILDER_PREFIX}:{spec.feed_id}",
            "pipeline_version": PIPELINE_VERSION,
            "model": None,
            "served_by": "no model — a machine-readable list, cut by code",
            "feed": {"id": spec.feed_id, "url": spec.url,
                     "sha256": feed_sha256, "outlet": spec.outlet},
            "span_verified_rate": 1.0 if developments else None,
            "cost": {"paid_api_usd": 0.0, "plane": "none"},
        },
        "keyed": {
            "matcher": "key_match/1",
            "key_field": spec.key_field,
            "key_pattern": spec.key_pattern,
            "complete": bool(spec.complete),
            "subject_field": spec.subject_field,
        },
        "ref_bands": {d: "insufficient-basis" for d in dimensions},
        "ref_developments": developments,
        "gaps": [] if developments else [
            f"no item of {spec.outlet or spec.feed_id} is bound to this subject "
            "inside the window"
        ],
    }


__all__ = [
    "BUILDER_PREFIX",
    "FeedSpec",
    "ITEM_LANE",
    "PIPELINE_VERSION",
    "build_keyed_reference",
    "item_date",
    "parse_bindings",
    "parse_feed",
    "record_span",
    "select_items",
]
