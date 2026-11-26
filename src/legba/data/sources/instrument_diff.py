# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic differs for the ``instrument_snapshot`` source kind.

A published instrument — a sanctions list, an export-control supplement, a
regulation's text — is compared snapshot to snapshot. This module is the pure,
stdlib-only half of that comparison: no network, no archive, no clock. The
handler (:mod:`legba.data.sources.instrument_snapshot`) fetches and archives;
this module answers "what exactly changed between these two byte strings".

Four differs, chosen per binding (``InstrumentSnapshotConfig.differ``):

  * ``xml_records`` — a structured list in XML. Every element whose local name
    is ``record_tag`` is one entry, keyed on the text at ``key_path`` (a
    ``/``-separated local-name path below the entry). For the OFAC SDN list
    that is ``sdnEntry`` keyed on ``uid`` — the list's OWN identifier, never a
    position or a name. Namespaces are ignored (local names only), so the
    namespace URI moving between publications does not read as every entry
    changing. A path's LAST segment may name an ATTRIBUTE
    (``@logicalId``, ``nameAlias/@wholeName``) — for a list whose own
    identifier is an attribute (the EU FSF list keys ``sanctionEntity`` on
    ``@logicalId``). A path with no ``@`` segment reads element text exactly
    as before.
  * ``csv_records`` — a structured list in CSV, keyed on one column (a header
    name, or a zero-based index as a string for a headerless file).
  * ``text`` — a unified line diff of the decoded text (regulation text, an
    HTML or XML rendering read as lines).
  * ``bytes`` — no structural reading at all (a PDF): the two sha256 hashes
    and sizes ARE the receipt, and the diff says so rather than inventing one.

Entry identity is a CANONICAL form, not the raw bytes: an entry is reduced to
(local tag, stripped text, children in document order) before hashing, so a
re-indented publication or a reordered attribute is not a change, while a
program added to an entry, a new alias or a changed address is. A changed
entry names WHICH top-level fields moved (``fields_changed``).

The parser is :mod:`xml.etree.ElementTree` streamed with ``iterparse``; each
entry is cleared once canonicalised, so a 30 MB list does not hold its whole
tree. CPython 3.11's bundled expat (>= 2.4.1) refuses the entity-expansion
("billion laughs") class on its own; no DTD is ever fetched.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Literal

#: The differs a binding may name.
DifferKind = Literal["xml_records", "csv_records", "text", "bytes"]


class InstrumentParseError(ValueError):
    """The snapshot could not be read the way its binding says it should be —
    malformed XML, a record tag that matches nothing, a key column that does
    not exist. The handler archives the bytes anyway (they are the evidence of
    what the publisher served) but never advances its last-good pointer and
    never emits a change signal off a parse it does not trust."""


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    """One keyed entry of a structured list, canonicalised."""

    key: str
    digest: str                                   # sha256 of the canonical form
    fields: dict[str, str] = field(default_factory=dict)   # top-level tag -> digest
    label: str = ""                               # a human handle (a name), never the key
    summary: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ParsedInstrument:
    """What one snapshot says, as the differ reads it."""

    differ: DifferKind
    entries: dict[str, Entry] = field(default_factory=dict)
    text_lines: list[str] | None = None
    published_date: str | None = None            # ISO date, the instrument's OWN
    declared_count: int | None = None             # the count the instrument states
    duplicate_keys: int = 0
    records_without_key: int = 0

    @property
    def entry_count(self) -> int | None:
        """Entries (structured) or lines (text); ``None`` for ``bytes`` — a
        PDF has no countable entries and a 0 here would be a lie."""
        if self.differ in ("xml_records", "csv_records"):
            return len(self.entries)
        if self.differ == "text":
            return len(self.text_lines or [])
        return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _local(tag: Any) -> str:
    """``{ns}name`` -> ``name`` (comments/PIs carry a non-str tag -> '')."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _canon(elem: ET.Element) -> list[Any]:
    """The canonical, namespace-free, whitespace-trimmed form of ``elem``.

    Attributes are sorted by local name; children keep document order (a list
    reordering its aliases IS a publication change, and is reported as one).
    """
    attrs = sorted((_local(k), v.strip()) for k, v in elem.attrib.items())
    return [
        _local(elem.tag),
        attrs,
        (elem.text or "").strip(),
        [_canon(c) for c in elem if _local(c.tag)],
    ]


def _digest(obj: Any) -> str:
    blob = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _split_path(path: str | None) -> list[str]:
    return [p for p in (path or "").strip("/").split("/") if p]


def attribute_path_error(path: str | None) -> str | None:
    """Why ``path`` is not a valid record path, or ``None``. An ``@name``
    segment (an attribute, matched on its local name) may only come LAST."""
    parts = _split_path(path)
    for i, part in enumerate(parts):
        if part.startswith("@") and (i != len(parts) - 1 or len(part) == 1):
            return f"path {path!r}: an '@attribute' segment must be the last, and named"
    return None


def _attr(elem: ET.Element, name: str) -> str:
    for k, v in elem.attrib.items():
        if _local(k) == name:
            return v.strip()
    return ""


def _find_texts(elem: ET.Element, path: str) -> list[str]:
    """Texts of every descendant at the local-name ``path`` below ``elem``;
    a last ``@name`` segment reads that attribute of the elements reached
    (I-5) instead of their text."""
    level = [elem]
    parts = _split_path(path)
    attr = parts.pop()[1:] if parts and parts[-1].startswith("@") else None
    for part in parts:
        level = [c for e in level for c in e if _local(c.tag) == part]
    if attr is not None:
        return [t for t in (_attr(e, attr) for e in level) if t]
    return [t for t in ((e.text or "").strip() for e in level) if t]


def parse_published_date(raw: str | None, fmt: str | None) -> str | None:
    """The instrument's own date as ISO ``YYYY-MM-DD``; ``None`` when absent
    or unparseable — never a guessed date."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if fmt:
            return datetime.strptime(raw, fmt).date().isoformat()
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int((raw or "").strip().replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_xml_records(
    body: bytes,
    *,
    record_tag: str,
    key_path: str,
    label_paths: Iterable[str] = (),
    summary_paths: Iterable[str] = (),
    published_date_path: str | None = None,
    published_date_format: str | None = None,
    declared_count_path: str | None = None,
) -> ParsedInstrument:
    """Stream ``body`` and key every ``record_tag`` element on ``key_path``.

    ``published_date_path`` / ``declared_count_path`` are local-name paths
    from the document ROOT's children (``publshInformation/Publish_Date`` on
    the SDN list). A record that has no key is counted, never invented a key;
    a key seen twice keeps the LAST occurrence and is counted.
    """
    label_paths = list(label_paths)
    summary_paths = list(summary_paths)
    want_date = _split_path(published_date_path)
    want_count = _split_path(declared_count_path)
    out = ParsedInstrument(differ="xml_records")
    stack: list[str] = []
    raw_date: str | None = None
    try:
        for event, elem in ET.iterparse(io.BytesIO(body), events=("start", "end")):
            name = _local(elem.tag)
            if event == "start":
                stack.append(name)
                continue
            # ``stack[0]`` is the root; paths are matched below it.
            below_root = stack[1:]
            if want_date and below_root == want_date and raw_date is None:
                raw_date = (elem.text or "").strip()
            if want_count and below_root == want_count and out.declared_count is None:
                out.declared_count = _int_or_none(elem.text)
            if name == record_tag:
                keys = _find_texts(elem, key_path)
                if not keys:
                    out.records_without_key += 1
                else:
                    key = keys[0]
                    if key in out.entries:
                        out.duplicate_keys += 1
                    fields: dict[str, list[Any]] = {}
                    for child in elem:
                        cname = _local(child.tag)
                        if cname:
                            fields.setdefault(cname, []).append(_canon(child))
                    if elem.attrib:
                        # I-5: the entry's OWN attributes are one field ("@"), so a
                        # changed reference number is named; an entry without
                        # attributes (OFAC's sdnEntry) is digested exactly as before.
                        fields["@"] = [sorted((_local(k), v.strip()) for k, v in elem.attrib.items())]
                    label_bits = [t for p in label_paths for t in _find_texts(elem, p)[:1]]
                    out.entries[key] = Entry(
                        key=key,
                        digest=_digest(_canon(elem)),
                        fields={k: _digest(v) for k, v in fields.items()},
                        label=", ".join(label_bits),
                        summary={p: _find_texts(elem, p) for p in summary_paths},
                    )
                elem.clear()
            stack.pop()
    except ET.ParseError as exc:
        raise InstrumentParseError(f"xml: {exc}") from exc
    if not out.entries:
        raise InstrumentParseError(
            f"xml: no <{record_tag}> entry carried a <{key_path}> key — the "
            "binding does not match what the publisher served"
        )
    out.published_date = parse_published_date(raw_date, published_date_format)
    return out


def parse_csv_records(
    body: bytes,
    *,
    key_column: str,
    has_header: bool = True,
    label_columns: Iterable[str] = (),
    summary_columns: Iterable[str] = (),
    encoding: str = "utf-8",
) -> ParsedInstrument:
    """Key every CSV row on ``key_column`` (a header name, or a zero-based
    index as a string when ``has_header`` is false)."""
    text = body.decode(encoding, errors="replace").lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise InstrumentParseError("csv: empty document")
    if has_header:
        header, data = [h.strip() for h in rows[0]], rows[1:]
    else:
        width = max(len(r) for r in rows)
        header, data = [str(i) for i in range(width)], rows
    if key_column not in header:
        raise InstrumentParseError(f"csv: key column {key_column!r} not in {header[:20]}")
    out = ParsedInstrument(differ="csv_records")
    label_columns = list(label_columns)
    summary_columns = list(summary_columns)
    for row in data:
        if not any(c.strip() for c in row):
            continue
        rec = {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)}
        key = rec.get(key_column, "")
        if not key:
            out.records_without_key += 1
            continue
        if key in out.entries:
            out.duplicate_keys += 1
        out.entries[key] = Entry(
            key=key,
            digest=_digest(rec),
            fields={h: _digest(v) for h, v in rec.items()},
            label=", ".join(rec[c] for c in label_columns if rec.get(c)),
            summary={c: [rec[c]] if rec.get(c) else [] for c in summary_columns},
        )
    if not out.entries:
        raise InstrumentParseError(f"csv: no row carried a {key_column!r} key")
    return out


def parse_text(body: bytes, *, encoding: str = "utf-8") -> ParsedInstrument:
    """Decoded text as lines, trailing whitespace trimmed per line (a
    re-wrapped line ending is not a change; a changed word is)."""
    text = body.decode(encoding, errors="replace").lstrip("﻿")
    return ParsedInstrument(
        differ="text", text_lines=[ln.rstrip() for ln in text.splitlines()],
    )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _entry_view(e: Entry) -> dict[str, Any]:
    view: dict[str, Any] = {"key": e.key}
    if e.label:
        view["label"] = e.label
    if e.summary:
        view["summary"] = e.summary
    return view


def diff_records(before: ParsedInstrument, after: ParsedInstrument) -> dict[str, Any]:
    """The exact added / removed / changed entry sets, keyed on the list's own
    identifier, sorted by key so the same pair always yields the same diff."""
    bk, ak = set(before.entries), set(after.entries)

    def _order(keys: Iterable[str]) -> list[str]:
        return sorted(keys, key=lambda k: (len(k), k))   # "36" before "100"

    added = [_entry_view(after.entries[k]) for k in _order(ak - bk)]
    removed = [_entry_view(before.entries[k]) for k in _order(bk - ak)]
    changed: list[dict[str, Any]] = []
    for k in _order(ak & bk):
        b, a = before.entries[k], after.entries[k]
        if a.digest == b.digest:
            continue
        moved = sorted(
            f for f in set(a.fields) | set(b.fields) if a.fields.get(f) != b.fields.get(f)
        )
        row = _entry_view(a)
        row["fields_changed"] = moved
        if a.summary != b.summary:
            row["summary_before"] = b.summary
        if a.label != b.label:
            row["label_before"] = b.label
        changed.append(row)
    return {
        "kind": "records",
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def diff_text(
    before: ParsedInstrument, after: ParsedInstrument, *, context: int = 2,
) -> dict[str, Any]:
    """A unified line diff plus its +/- line counts."""
    lines = list(difflib.unified_diff(
        before.text_lines or [], after.text_lines or [],
        fromfile="previous", tofile="current", lineterm="", n=context,
    ))
    plus = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return {
        "kind": "text",
        "counts": {"lines_added": plus, "lines_removed": minus},
        "unified_diff": "\n".join(lines),
    }


def diff_instruments(before: ParsedInstrument, after: ParsedInstrument) -> dict[str, Any]:
    """Dispatch on the differ; ``bytes`` says plainly that there is no
    structural diff — the hashes are the receipt."""
    if before.differ != after.differ:
        raise InstrumentParseError(
            f"differ changed between snapshots ({before.differ} -> {after.differ})"
        )
    if after.differ in ("xml_records", "csv_records"):
        return diff_records(before, after)
    if after.differ == "text":
        return diff_text(before, after)
    return {
        "kind": "bytes",
        "counts": {},
        "note": "binary instrument — no structural diff; the two sha256 hashes are the receipt",
    }


def cap_diff(diff: dict[str, Any], *, max_entries: int, max_text_chars: int) -> dict[str, Any]:
    """A copy of ``diff`` bounded for a signal payload. ``counts`` always stay
    whole; a list or text that was cut says so (``truncated``) — the full diff
    is archived beside the snapshots, never lost."""
    out = dict(diff)
    truncated = False
    for name in ("added", "removed", "changed"):
        items = out.get(name)
        if isinstance(items, list) and len(items) > max_entries:
            out[name] = items[:max_entries]
            truncated = True
    text = out.get("unified_diff")
    if isinstance(text, str) and len(text) > max_text_chars:
        out["unified_diff"] = text[:max_text_chars]
        truncated = True
    out["truncated"] = truncated
    return out


__all__ = [
    "DifferKind",
    "Entry",
    "attribute_path_error",
    "InstrumentParseError",
    "ParsedInstrument",
    "cap_diff",
    "diff_instruments",
    "diff_records",
    "diff_text",
    "parse_csv_records",
    "parse_published_date",
    "parse_text",
    "parse_xml_records",
]
