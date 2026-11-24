# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One byte path (lane B-1): what a reader can say about a signal's bytes.

The promise: for any published sentence, the row it came from, the evidence
behind the row, and the exact bytes received. ``fetch_versions`` is the one
register of fetched bytes (migration 0245; the older ``evidence_archive``
objects are registered into it by hash — ``scripts/register_evidence_archive
.py``, migration 0266). This module is the single source of truth for:

1. :func:`evidence_states` / :func:`evidence_state` — a signal's evidence
   state as EXACTLY ONE of three labels, with the receipt / version ids:

   * ``received_bytes`` ("received bytes") — the signal links (role
     ``response`` / ``page`` / ``inbound``) to a version that is NOT an
     archiver copy and whose bytes are held (hot or cold). These are the
     bytes the poll received.
   * ``archived_later`` ("archived later") — no held received bytes, but the
     signal links to the evidence archiver's copy (role ``archived``, or any
     link to an ``archiver``-role version) whose bytes are held. Fetched at
     archive time, NOT at poll — a page may have changed in between.
   * ``no_bytes_kept`` ("no bytes kept") — neither. Any receipt whose bytes
     are gone (pruned, too large, never stored) is still named, as a hash.

   The Record sheet, the statement gate's ``no_receipt`` fallback and the
   drift pass all read this one function.

2. :func:`item_as_received` — the text a quote must be checked against for
   ONE link: for a feed / file / API response, the slice of the stored version
   the record's item is (its ``locator``: a feed item by guid / link, a GDELT
   export row by GLOBALEVENTID, or the whole capture for a Telegram message or
   a streamed event); for a page / archived copy, the page's extracted text.
   Read from the stored bytes, verified against their sha256. No re-fetching.

Pure helpers (:func:`slice_item`, :func:`xml_item_text`) carry the slicing so
it is testable without a database.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ARCHIVED_LATER",
    "EvidenceState",
    "ItemSlice",
    "ItemText",
    "LABELS",
    "LABEL_TEXT",
    "NO_BYTES_KEPT",
    "RECEIVED_BYTES",
    "evidence_state",
    "evidence_states",
    "item_as_received",
    "locator_from_signal",
    "signal_item_text",
    "slice_item",
    "xml_item_text",
]

RECEIVED_BYTES = "received_bytes"
ARCHIVED_LATER = "archived_later"
NO_BYTES_KEPT = "no_bytes_kept"
LABELS: tuple[str, ...] = (RECEIVED_BYTES, ARCHIVED_LATER, NO_BYTES_KEPT)
#: The words a screen shows (04_MERGED_RECOMMENDATION §5.4).
LABEL_TEXT: Mapping[str, str] = {
    RECEIVED_BYTES: "received bytes",
    ARCHIVED_LATER: "archived later",
    NO_BYTES_KEPT: "no bytes kept",
}

#: Link roles that are what a poll / push RECEIVED (vs ``archived``).
RECEIVED_ROLES = frozenset({"response", "page", "inbound"})
#: Preference among held received links: the article page, then the item's
#: response, then a pushed envelope.
_RECEIVED_ORDER = {"page": 0, "response": 1, "inbound": 2}
#: Item text above this many characters is cut (a pathological item).
MAX_ITEM_CHARS = 200_000


@dataclass(frozen=True)
class EvidenceState:
    signal_id: str
    label: str
    #: The version the label rests on (``None`` for ``no_bytes_kept``).
    version_id: int | None = None
    #: The link role of that version (``response`` / ``page`` / ``inbound`` / ``archived``).
    link_role: str | None = None
    #: Where the item sits inside ``version_id`` (``None`` = the whole version / a page).
    locator: Mapping[str, Any] | None = None
    #: Text a quote can be checked against is available for ``version_id``:
    #: stored extracted text, or an item locator into held bytes, or a held page.
    has_text: bool = False
    #: Every version the signal links to, by role (a receipt with no bytes included).
    links: Mapping[str, int] = field(default_factory=dict)
    #: Versions linked whose bytes are NOT held (hash only): pruned / too large / not stored.
    hash_only: tuple[int, ...] = ()

    @property
    def label_text(self) -> str:
        return LABEL_TEXT[self.label]

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id, "label": self.label, "label_text": self.label_text,
            "version_id": self.version_id, "link_role": self.link_role,
            "locator": dict(self.locator) if self.locator is not None else None,
            "has_text": self.has_text, "links": dict(self.links), "hash_only": list(self.hash_only),
        }


_STATE_SQL = """
SELECT r.record_id::text AS signal_id, r.role, r.version_id, r.locator,
       v.role AS version_role, v.object_ref, v.bytes_pruned_at,
       (v.extracted_text IS NOT NULL) AS has_text
  FROM fetch_version_records r
  JOIN fetch_versions v ON v.id = r.version_id
 WHERE r.record_kind = 'signal' AND r.record_id = ANY($1::uuid[])
"""

#: Before migration 0266 there is no ``locator`` column (the reader may roll first).
_STATE_SQL_PRE_0266 = _STATE_SQL.replace("r.locator,", "NULL::jsonb AS locator,")
_LOCATOR_PRESENT_SQL = (
    "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
    " AND table_name = 'fetch_version_records' AND column_name = 'locator')"
)
_LOCATOR_SEEN = False


async def _has_locator(conn: Any) -> bool:
    """Is migration 0266's column there? Asked until the answer is yes (a probe
    never fails a caller's transaction the way a missing column would)."""
    global _LOCATOR_SEEN
    if not _LOCATOR_SEEN:
        _LOCATOR_SEEN = bool(await conn.fetchval(_LOCATOR_PRESENT_SQL))
    return _LOCATOR_SEEN


def _loc(raw: Any) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) else None


def classify(signal_id: str, rows: Sequence[Mapping[str, Any]]) -> EvidenceState:
    """The label for one signal from its link rows (pure; :data:`_STATE_SQL` shape)."""
    links = {str(r["role"]): int(r["version_id"]) for r in rows}
    held = [r for r in rows if r["object_ref"] is not None and r["bytes_pruned_at"] is None]
    held_ids = {int(r["version_id"]) for r in held}
    hash_only = tuple(sorted({int(r["version_id"]) for r in rows} - held_ids))
    received = sorted(
        (r for r in held if r["role"] in RECEIVED_ROLES and r["version_role"] != "archiver"),
        key=lambda r: (_RECEIVED_ORDER.get(str(r["role"]), 9), int(r["version_id"])),
    )
    archived = sorted(
        (r for r in held if r["role"] == "archived" or r["version_role"] == "archiver"),
        key=lambda r: (str(r["role"]) != "archived", int(r["version_id"])),
    )
    for label, pick in ((RECEIVED_BYTES, received), (ARCHIVED_LATER, archived)):
        if pick:
            r = pick[0]
            loc = _loc(r["locator"]) if r["role"] == "response" else None
            has_text = bool(r["has_text"]) or loc is not None or r["version_role"] in ("page", "archiver")
            return EvidenceState(signal_id, label, int(r["version_id"]), str(r["role"]), loc,
                                 has_text, links, hash_only)
    return EvidenceState(signal_id, NO_BYTES_KEPT, None, None, None, False, links, hash_only)


async def evidence_states(conn: Any, signal_ids: Iterable[Any]) -> dict[str, EvidenceState]:
    """The evidence state of each signal, keyed by its id as text. One query."""
    ids = [str(s) for s in signal_ids]
    if not ids:
        return {}
    by: dict[str, list[Mapping[str, Any]]] = {i: [] for i in ids}
    sql = _STATE_SQL if await _has_locator(conn) else _STATE_SQL_PRE_0266
    for r in await conn.fetch(sql, ids):
        by.setdefault(str(r["signal_id"]), []).append(r)
    return {sid: classify(sid, rows) for sid, rows in by.items()}


async def evidence_state(conn: Any, signal_id: Any) -> EvidenceState:
    return (await evidence_states(conn, [signal_id]))[str(signal_id)]


# ---------------------------------------------------------------------------
# The item as received
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemSlice:
    """One item cut from a stored body. ``exact`` = ``raw`` is a byte range of
    the stored body (False when it had to be re-serialised, e.g. a JSON object)."""

    raw: bytes
    text: str
    basis: str
    exact: bool = True


@dataclass(frozen=True)
class ItemText:
    version_id: int
    version_sha256: str
    basis: str
    text: str
    text_sha256: str
    item_sha256: str | None = None
    item_bytes: int | None = None
    exact: bool = True
    locator: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id, "version_sha256": self.version_sha256,
            "basis": self.basis, "text": self.text, "text_sha256": self.text_sha256,
            "item_sha256": self.item_sha256, "item_bytes": self.item_bytes, "exact": self.exact,
            "locator": dict(self.locator) if self.locator is not None else None,
        }


_TAG_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_WS_RE = re.compile(r"\s+")
_ITEM_RE = re.compile(rb"<(item|entry)\b[^>]*>.*?</\1\s*>", re.S | re.I)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def xml_item_text(raw: bytes) -> str:
    """The readable text of one feed item's XML: CDATA unwrapped, tags
    stripped, entities unescaped — twice, because a description routinely
    carries escaped HTML."""
    s = _CDATA_RE.sub(lambda m: " " + m.group(1) + " ", _decode(raw))
    for _ in range(2):
        s = html.unescape(_TAG_RE.sub(" ", s))
    return _WS_RE.sub(" ", s).strip()[:MAX_ITEM_CHARS]


def _needles(value: str) -> list[bytes]:
    out = [value, html.escape(value, quote=False), html.escape(value, quote=True)]
    return [n.encode("utf-8") for n in dict.fromkeys(out) if n]


def _slice_xml(body: bytes, locator: Mapping[str, Any]) -> ItemSlice | None:
    keys = [str(locator[k]) for k in ("item_id", "item_link") if locator.get(k)]
    if not keys:
        return None
    items = [m for m in _ITEM_RE.finditer(body)]
    for key in keys:   # the item id first, then the link
        needles = _needles(key)
        for m in items:
            chunk = m.group(0)
            if any(n in chunk for n in needles):
                return ItemSlice(chunk, xml_item_text(chunk), "feed_item")
    return None


def _strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _find_json_item(node: Any, keys: set[str], depth: int = 0) -> Any:
    """The DEEPEST object one of whose own scalar values equals a key."""
    if depth > 32:
        return None
    best = None
    if isinstance(node, Mapping):
        for v in node.values():
            hit = _find_json_item(v, keys, depth + 1)
            if hit is not None:
                return hit
        if any(isinstance(v, (str, int)) and str(v) in keys for v in node.values()):
            best = node
    elif isinstance(node, list):
        for v in node:
            hit = _find_json_item(v, keys, depth + 1)
            if hit is not None:
                return hit
    return best


def _slice_json(body: bytes, locator: Mapping[str, Any]) -> ItemSlice | None:
    keys = {str(locator[k]) for k in ("item_id", "item_link") if locator.get(k)}
    if not keys:
        return None
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    found = _find_json_item(doc, keys)
    if found is None:
        return None
    raw = json.dumps(found, sort_keys=True, ensure_ascii=False).encode("utf-8")
    text = _WS_RE.sub(" ", " ".join(s for s in _strings(found) if s)).strip()
    return ItemSlice(raw, text[:MAX_ITEM_CHARS], "json_object", exact=False)


def _slice_zip_csv(body: bytes, locator: Mapping[str, Any]) -> ItemSlice | None:
    key = str(locator.get("key") or "")
    if not key:
        return None
    col = int(locator.get("key_index") or 0)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
            if not names:
                return None
            data = zf.read(names[0])
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return None
    for line in data.splitlines():
        cells = line.split(b"\t")
        if len(cells) > col and cells[col].decode("utf-8", "replace").strip() == key:
            return ItemSlice(line, _decode(line), "zip_csv_row")
    return None


def _whole(body: bytes, locator: Mapping[str, Any]) -> ItemSlice:
    text = None
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if isinstance(doc, Mapping) and isinstance(doc.get("message"), Mapping):
        msg = doc["message"]   # a Telegram message receipt (message_receipt.py)
        val = msg.get("message") if msg.get("message") is not None else msg.get("text")
        text = val if isinstance(val, str) else None
    elif doc is not None:
        text = _WS_RE.sub(" ", " ".join(s for s in _strings(doc) if s)).strip()
    if text is None:
        text = _decode(body)
    return ItemSlice(body, text[:MAX_ITEM_CHARS], "whole_capture")


def _looks_xml(body: bytes, content_type: str | None) -> bool:
    ct = (content_type or "").lower()
    if "xml" in ct or "rss" in ct or "atom" in ct:
        return True
    return body.lstrip()[:1] == b"<"


def slice_item(body: bytes, content_type: str | None, locator: Mapping[str, Any] | None) -> ItemSlice | None:
    """The record's item inside a stored response body, by its locator. ``None``
    when the locator does not name an item this body holds (never a guess)."""
    if not locator or not body:
        return None
    if locator.get("whole"):
        return _whole(body, locator)
    if locator.get("format") == "zip_csv":
        return _slice_zip_csv(body, locator)
    if _looks_xml(body, content_type):
        return _slice_xml(body, locator)
    return _slice_json(body, locator)


def locator_from_signal(
    payload: Mapping[str, Any] | None, raw_provenance: Mapping[str, Any] | None,
    canonical_url: str | None,
) -> dict[str, Any] | None:
    """Where a signal's item sits inside the response it was parsed from,
    from the signal row alone — the ONE rule both the link writer (for a
    handler without its own ``receipt_locator``) and the reader (for a link
    written before locators existed) use. ``None`` when nothing names it."""
    payload = payload if isinstance(payload, Mapping) else {}
    prov = raw_provenance if isinstance(raw_provenance, Mapping) else {}
    kind = prov.get("kind") or prov.get("fetch_kind") or prov.get("source_kind")
    if kind == "gdelt_files" and prov.get("global_event_id"):
        return {"format": "zip_csv", "key_column": "GLOBALEVENTID", "key_index": 0,
                "key": str(prov["global_event_id"])}
    if kind == "telegram_channel":
        return {"whole": True}
    out: dict[str, Any] = {}
    item_id = payload.get("external_id") or prov.get("external_id")
    if isinstance(item_id, (str, int)) and str(item_id):
        out["item_id"] = str(item_id)
    link = payload.get("link") or canonical_url
    if isinstance(link, str) and link:
        out["item_link"] = link
    return out or None


_SIGNAL_ROW_SQL = "SELECT payload, raw_provenance, canonical_url FROM signals WHERE id = $1::uuid"


def _json_obj(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


_VERSION_META_SQL = """
SELECT id, sha256, role, content_type, extracted_text, object_ref, bytes_pruned_at
  FROM fetch_versions WHERE id = $1
"""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


async def item_as_received(
    conn: Any, version_id: int, locator: Mapping[str, Any] | None = None, *,
    root: Path | None = None,
) -> ItemText | None:
    """The text a quote must be found in, for one link: the item as the
    response carried it (``locator`` into the stored, re-verified bytes), or —
    for a page / archived copy (no locator) — the page's extracted text.
    ``None`` when nothing can be read (no bytes kept, no item at the locator,
    no extractable text). Read-only; never re-fetches."""
    from ..runtime.fetch_receipts import extract_page_text, read_version_body  # noqa: PLC0415

    meta = await conn.fetchrow(_VERSION_META_SQL, int(version_id))
    if meta is None:
        return None
    vsha = str(meta["sha256"])
    if locator is None:
        if meta["role"] not in ("page", "archiver"):
            return None   # a whole feed is not one item's text
        text = meta["extracted_text"]
        basis = "page_text"
        if text is None:
            body = await read_version_body(conn, int(version_id), root=root)
            if body is None:
                return None
            text = extract_page_text(body, meta["content_type"])
            basis = "page_text_derived"
        if not text:
            return None
        return ItemText(int(version_id), vsha, basis, text, _sha(text.encode("utf-8")))
    body = await read_version_body(conn, int(version_id), root=root)
    if body is None:
        return None
    cut = slice_item(body, meta["content_type"], locator)
    if cut is None or not cut.text:
        return None
    return ItemText(
        int(version_id), vsha, cut.basis, cut.text, _sha(cut.text.encode("utf-8")),
        _sha(cut.raw), len(cut.raw), cut.exact, dict(locator),
    )


async def signal_item_text(
    conn: Any, signal_id: Any, *, root: Path | None = None,
) -> tuple[EvidenceState, ItemText | None]:
    """A signal's evidence state and the text its quotes are checked against
    (the item as received, or the archived page's text) — what the statement
    gate's evidence resolver and the Record sheet call. A ``no_bytes_kept``
    signal returns ``(state, None)``: the caller falls back to the signal's
    stored title/summary and says so (``no_receipt``)."""
    state = await evidence_state(conn, signal_id)
    if state.version_id is None:
        return state, None
    locator = state.locator
    if locator is None and state.link_role in ("response", "inbound"):
        # A link written before locators existed: the signal row names its item.
        row = await conn.fetchrow(_SIGNAL_ROW_SQL, str(signal_id))
        if row is not None:
            locator = locator_from_signal(_json_obj(row["payload"]), _json_obj(row["raw_provenance"]),
                                          row["canonical_url"])
        if locator is None:
            return state, None
    return state, await item_as_received(conn, state.version_id, locator, root=root)
