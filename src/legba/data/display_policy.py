# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The READER DISPLAY POLICY — one rule, every transport.

WHY. ``GET /api/v1/signals`` handed ``data.raw_body`` and
``data.archived_text`` — the publisher's full text, and our archived copy of it
— to any caller; ``analyst_outputs.data.citations[].source_text`` carries up to
3.2 KB of the same text on every grounded finding; the events WebSocket relays
whole signal rows; MCP and A2A pass the registry's rows straight through. Before
any stranger sees the platform, no READER transport may carry archived bytes or
publisher text beyond what a citation needs: the title, the cited span, the
link, the hash, the as-of.

THE RULE IS THIS MODULE. Two entry points, one policy:

  * :func:`display_projection` — given ONE signal or observation row and the
    principal's class, the row that principal may see. Operators keep the full
    row; a reader gets :data:`SIGNAL_PROJECTION_FIELDS` (or
    :data:`OBSERVATION_PROJECTION_FIELDS`) and nothing else.
  * :func:`display_payload` — the same rule applied to ANY JSON-shaped payload a
    transport is about to emit (a page of findings, an event frame, a consult
    tool result, an export document): signal / observation rows found anywhere
    inside it are replaced by their projection, the publisher-text fields
    (:data:`PUBLISHER_TEXT_FIELDS`) are withheld wherever they sit, and the
    text a publisher document legitimately carries for a citation is bounded to
    one span of :data:`SPAN_CHARS`.

Every reader transport calls one of the two; :data:`READER_TRANSPORTS` lists
them, and ``tests/edge/test_display_policy.py`` plants a sentinel in an archived
page and asserts it reaches none of them — enumerating the transports from that
one table, so a new transport fails the test until it is listed.

NEVER SILENT. A reader projection says it is one (``display_policy: reader``);
a mapping that lost fields names them (``display_withheld``); a NULL licence
reads ``license: unreviewed`` — never a guessed class, never an absent key.

OPERATOR IDENTITY. For an operator both functions return their input unchanged
(the same object — byte-identical downstream). The class itself is resolved
elsewhere (``registry.display_class`` for HTTP/WS/SSE; the consult front door's
``display_class`` stamp for the consult context; MCP and A2A are reader
surfaces by construction). This module is a stdlib-only LEAF so the slim
registry image, the MCP process and the analyst runtime can all import it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from .archive import sha256_from_object_ref

#: The two display classes. B2's roles map onto them (``operator`` and the
#: service principal → OPERATOR; ``read`` / ``reader-own-rows`` → READER).
OPERATOR = "operator"
READER = "reader"
PRINCIPAL_CLASSES = (OPERATOR, READER)

#: The most publisher text one cited span may carry, in characters.
SPAN_CHARS = 280

#: The most spans one projected row may carry.
MAX_SPANS = 3

#: What a reader sees where no licence has been recorded (NULL is the shipped
#: state of every source today — W11-F19). Never a guessed class.
LICENSE_UNREVIEWED = "unreviewed"

#: Every reader transport → the module that carries it. ONE list: the sentinel
#: test enumerates its probes from these keys and fails while a key has no
#: probe; a new transport is a new row here, and the test fails until it is.
READER_TRANSPORTS: dict[str, str] = {
    "rest": "legba/data/registry/display_class.py",
    "consult_context": "legba/data/analysts/consult_tool_rendering.py",
    "ws": "legba/data/registry/api.py",
    "sse": "legba/data/registry/consult_stream_api.py",
    "mcp": "legba/ui/mcp_server.py",
    "a2a": "legba/data/outputs/a2a_skill.py",
    "export": "legba/data/registry/export_api.py",
}

#: Fields that ARE publisher text or archived bytes. Withheld from a reader
#: wherever they sit in a payload, at any depth.
PUBLISHER_TEXT_FIELDS = frozenset({
    "archived_text",   # our archived copy of the page (evidence_archiver)
    "raw_body",        # the source's body as served (usually HTML)
    "source_text",     # a citation's grounding text (3.2 KB of the above)
    "best_body",       # the corpus index's duplicate of one of the above
    "distilled_body",  # a derived full body
    "raw_html",
    "html",
    "full_text",
    "article_text",
    "raw_b64",         # the events socket's hex dump of an undecodable frame
})

#: The fields of a signal row a reader sees (the reader projection, exactly).
SIGNAL_PROJECTION_FIELDS = (
    "id",
    "title",
    "source_id",
    "canonical_url",
    "fetched_at",
    "published_at",
    "content_hash",
    "archived",
    "archive_sha256",
    "spans",
    "license_class",
    "license",
    "display_policy",
)

#: The fields of an observation row a reader sees.
OBSERVATION_PROJECTION_FIELDS = (
    "id",
    "collection_id",
    "series_id",
    "subject_kind",
    "subject",
    "valid_from",
    "valid_to",
    "record_time",
    "value",
    "unit",
    "value_text",
    "canonical_url",
    "content_hash",
    "license_class",
    "license",
    "display_policy",
)

#: Keys whose presence marks a mapping as a PUBLISHER DOCUMENT (a citation
#: entry, a corpus hit, a read_document body): inside one, the prose-shaped
#: fields are publisher text and are bounded to one span.
_PUBLISHER_DOC_MARKERS = frozenset({
    "canonical_url", "content_hash", "signal_id", "body_field",
    "snippet_field", "source_url", "guid",
})

#: Prose-shaped fields that are publisher text inside a publisher document.
_DOC_TEXT_FIELDS = frozenset({
    "text", "summary", "description", "content", "body", "value_text",
})

#: Fields that are a cited span by definition — bounded everywhere.
_SPAN_FIELDS = frozenset({"snippet", "quote", "excerpt", "cited_span"})

#: Nested keys whose mapping value is a publisher document even when it does
#: not carry a marker itself (the corpus hit's ``source`` block).
_NESTED_DOC_KEYS = frozenset({"source", "document"})

#: Where a mapping says what the reader policy took out of it.
WITHHELD_KEY = "display_withheld"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: Guard against pathological nesting (a payload is JSON from our own code;
#: nothing legitimate is this deep).
_MAX_DEPTH = 64


def principal_class(value: Any) -> str:
    """Normalise a class value. Anything that is not exactly OPERATOR is READER
    — an unknown class fails CLOSED."""
    return OPERATOR if value == OPERATOR else READER


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _iso(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clip_span(text: str, span_chars: int = SPAN_CHARS) -> str:
    """``text`` bounded to one span (word boundary when one is near)."""
    if len(text) <= span_chars:
        return text
    cut = text[:span_chars]
    space = cut.rfind(" ")
    if space > span_chars // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _plain(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def cited_spans(
    text: str | None,
    markers: Sequence[str],
    *,
    span_chars: int = SPAN_CHARS,
    max_spans: int = MAX_SPANS,
) -> list[str]:
    """The window of at most ``span_chars`` around each marker found in
    ``text`` (case-insensitive), at most ``max_spans`` of them.

    A marker is the cited passage a citation names (a quote, a claim's quoted
    span). No marker, or a marker the text does not contain, yields no span —
    the reader gets nothing rather than a lead paragraph nobody cited.
    """
    if not text or not markers:
        return []
    body = _plain(text)
    low = body.lower()
    spans: list[str] = []
    for marker in markers:
        needle = _plain(str(marker or "")).lower()
        if not needle:
            continue
        at = low.find(needle)
        if at < 0:
            continue
        if len(needle) >= span_chars:
            start = at
        else:
            start = max(0, at - (span_chars - len(needle)) // 2)
        end = min(len(body), start + span_chars)
        start = max(0, end - span_chars)
        spans.append(body[start:end])
        if len(spans) >= max_spans:
            break
    return spans


def _license(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    """The recorded licence class, read where it exists: the ingest stamp
    (``payload.license_class``), a row column, then the manual-batch
    provenance licence. ``None`` when nothing was recorded — never guessed."""
    for candidate in (payload.get("license_class"), row.get("license_class")):
        text = _str_or_none(candidate)
        if text:
            return text
    prov = _as_mapping(row.get("raw_provenance")) or {}
    inner = prov.get("provenance")
    for candidate in (
        inner.get("license") if isinstance(inner, Mapping) else None,
        prov.get("license"),
    ):
        text = _str_or_none(candidate)
        if text:
            return text
    return None


def _body_text(payload: Mapping[str, Any]) -> str | None:
    for key in ("archived_text", "text", "raw_body", "distilled_body", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# ---------------------------------------------------------------------------
# The projections
# ---------------------------------------------------------------------------


def _is_observation_row(row: Mapping[str, Any]) -> bool:
    return "series_id" in row and "value" in row and (
        "sha256" in row or "source_url" in row or "collection_id" in row
    )


def _signal_projection(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    markers: Sequence[str],
) -> dict[str, Any]:
    title = payload.get("title") or payload.get("headline") or row.get("title")
    object_ref = row.get("object_ref")
    archive_sha = row.get("archive_sha256") or sha256_from_object_ref(
        object_ref if isinstance(object_ref, str) else None
    )
    archived = row.get("archived")
    if not isinstance(archived, bool):
        archived = object_ref is not None or archive_sha is not None
    license_class = _license(row, payload)
    return {
        "id": _str_or_none(row.get("id") or row.get("signal_id")),
        "title": _str_or_none(title),
        "source_id": _str_or_none(
            row.get("source_id") or row.get("descriptor_source_id")
            or payload.get("source_id")
        ),
        "canonical_url": _str_or_none(
            row.get("canonical_url") or row.get("source_url")
            or payload.get("link") or payload.get("url")
        ),
        "fetched_at": _iso(row.get("fetched_at") or row.get("produced_at")),
        "published_at": _iso(payload.get("published_at")),
        "content_hash": _str_or_none(row.get("content_hash") or row.get("guid")),
        "archived": archived,
        "archive_sha256": archive_sha,
        "spans": cited_spans(_body_text(payload), markers),
        "license_class": license_class,
        "license": license_class or LICENSE_UNREVIEWED,
        "display_policy": READER,
    }


def _observation_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    license_class = _license(row, _as_mapping(row.get("provenance")) or {})
    value_text = row.get("value_text")
    return {
        "id": _str_or_none(row.get("id")),
        "collection_id": row.get("collection_id"),
        "series_id": row.get("series_id"),
        "subject_kind": row.get("subject_kind"),
        "subject": row.get("subject"),
        "valid_from": _iso(row.get("valid_from")),
        "valid_to": _iso(row.get("valid_to")),
        "record_time": _iso(row.get("record_time")),
        "value": row.get("value"),
        "unit": row.get("unit"),
        "value_text": clip_span(value_text) if isinstance(value_text, str) else value_text,
        "canonical_url": _str_or_none(row.get("source_url") or row.get("canonical_url")),
        "content_hash": _str_or_none(row.get("sha256") or row.get("content_hash")),
        "license_class": license_class,
        "license": license_class or LICENSE_UNREVIEWED,
        "display_policy": READER,
    }


def display_projection(
    row: Mapping[str, Any],
    principal: str,
    *,
    markers: Sequence[str] = (),
) -> Mapping[str, Any]:
    """THE function: one signal or observation row → what ``principal`` sees.

    ``principal`` is a display class (:data:`OPERATOR` / :data:`READER`; any
    other value is READER). An operator gets the row itself, unchanged. A
    reader gets :data:`SIGNAL_PROJECTION_FIELDS` — title, source, link, as-of,
    content hash, the archive hash (never the bytes), the cited span(s) around
    ``markers`` (≤ :data:`SPAN_CHARS` each) and the licence — or, for an
    observation row, :data:`OBSERVATION_PROJECTION_FIELDS`.

    Accepts every shape a signal travels in: a ``signals`` row (``payload``), a
    ``Signal`` model dump (``signal_id``), the REST ``SignalRow`` (``data``),
    a lineage node (``body``).
    """
    if principal_class(principal) == OPERATOR:
        return row
    if _is_observation_row(row):
        return _observation_projection(row)
    payload = (
        _as_mapping(row.get("payload"))
        or _as_mapping(row.get("data"))
        or _as_mapping(row.get("body"))
        or {}
    )
    return _signal_projection(row, payload, markers)


def _shape_row(m: Mapping[str, Any]) -> Any:
    """The reader replacement for a mapping that IS a signal / observation row
    in one of its known shapes, or ``None`` when it is not one."""
    if _is_observation_row(m):
        return _observation_projection(m)
    # A signals row / Signal model dump: the payload plus the row's columns.
    if _as_mapping(m.get("payload")) is not None and (
        "content_hash" in m or "canonical_url" in m or "fetched_at" in m
    ):
        return display_projection(m, READER)
    # The REST ``SignalRow``: keep the typed envelope (title, ids, geo/tags
    # columns, the archive flag + hash), project ``data``.
    if "descriptor_source_id" in m and _as_mapping(m.get("data")) is not None:
        out = {
            k: _scrub(v, depth=1) for k, v in m.items()
            if k != "data" and k not in PUBLISHER_TEXT_FIELDS
        }
        projected = dict(display_projection(m, READER))
        geo = (_as_mapping(m.get("data")) or {}).get("geo")
        if isinstance(geo, Mapping):
            # Our geocoder's coordinates (not publisher text) — the Map reads
            # ``data.geo.{lat,lon}``.
            coords = {
                k: geo[k] for k in ("lat", "lon")
                if isinstance(geo.get(k), (int, float))
            }
            if coords:
                projected["geo"] = coords
        out["data"] = projected
        return out
    # A lineage node rooted at a signal: ``body`` is the signal payload.
    if m.get("row_kind") == "signal" and _as_mapping(m.get("body")) is not None:
        out = {k: _scrub(v, depth=1) for k, v in m.items() if k != "body"}
        out["body"] = display_projection(m, READER)
        return out
    return None


def _scrub_json_text(text: str, *, depth: int) -> str | None:
    """``text`` re-encoded after the policy, when it is a JSON object/array;
    the original string when the policy changes nothing; ``None`` when it is
    not JSON at all (the caller treats it as prose)."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, (Mapping, list)):
        return None
    scrubbed = _scrub(parsed, depth=depth)
    return text if scrubbed == parsed else json.dumps(scrubbed, default=str)


def _scrub(obj: Any, *, doc: bool = False, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(obj, Mapping):
        shaped = _shape_row(obj)
        if shaped is not None:
            return shaped
        in_doc = doc or any(k in obj for k in _PUBLISHER_DOC_MARKERS)
        analyst_body = str(obj.get("body_field") or "").startswith("analyst_outputs")
        out: dict[str, Any] = {}
        withheld: list[str] = []
        for raw_key, value in obj.items():
            key = str(raw_key)
            if key in PUBLISHER_TEXT_FIELDS:
                withheld.append(key)
                continue
            if isinstance(value, str) and value[:1] in ("{", "["):
                # JSON carried as text (a jsonb read without a codec, a frame
                # inside a frame): the policy applies to what it encodes.
                inner = _scrub_json_text(value, depth=depth + 1)
                if inner is not None:
                    out[key] = inner
                    continue
            if isinstance(value, str):
                bounded = key in _SPAN_FIELDS or (
                    in_doc and key in _DOC_TEXT_FIELDS
                    and not (key == "body" and analyst_body)
                )
                out[key] = clip_span(value) if bounded else value
                continue
            out[key] = _scrub(
                value,
                doc=key in _NESTED_DOC_KEYS and isinstance(value, Mapping),
                depth=depth + 1,
            )
        if withheld:
            prior = obj.get(WITHHELD_KEY)
            merged = set(prior) if isinstance(prior, list) else set()
            out[WITHHELD_KEY] = sorted(merged | set(withheld))
        return out
    if isinstance(obj, (list, tuple)):
        return [_scrub(item, depth=depth + 1) for item in obj]
    return obj


def display_payload(payload: Any, principal: str) -> Any:
    """The policy applied to any JSON-shaped payload a transport emits.

    Operator → ``payload`` itself (the same object). Reader → a copy in which
    every signal / observation row is replaced by :func:`display_projection`,
    every :data:`PUBLISHER_TEXT_FIELDS` key is withheld (and named under
    :data:`WITHHELD_KEY`), and the prose of every publisher document (and every
    span-shaped field) is bounded to :data:`SPAN_CHARS`. Idempotent.
    """
    if principal_class(principal) == OPERATOR:
        return payload
    if isinstance(payload, str) and payload.lstrip()[:1] in ("{", "["):
        inner = _scrub_json_text(payload.lstrip(), depth=0)
        return payload if inner is None else inner
    return _scrub(payload)


__all__ = [
    "LICENSE_UNREVIEWED",
    "MAX_SPANS",
    "OBSERVATION_PROJECTION_FIELDS",
    "OPERATOR",
    "PRINCIPAL_CLASSES",
    "PUBLISHER_TEXT_FIELDS",
    "READER",
    "READER_TRANSPORTS",
    "SIGNAL_PROJECTION_FIELDS",
    "SPAN_CHARS",
    "WITHHELD_KEY",
    "cited_spans",
    "clip_span",
    "display_payload",
    "display_projection",
    "principal_class",
]
