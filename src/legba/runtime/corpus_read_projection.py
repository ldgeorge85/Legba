# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citable, bounded projections for the OpenSearch corpus readers.

The consult run of 2026-09-16 is what this module exists to make impossible.
``search_corpus`` returned ``count 5-8`` with ``refs: 0`` on EVERY call and
``read_document`` returned ``count 0, refs 0`` on all three of its calls —
including on the one document (``3f69f001…``) the whole provenance analysis
rested on. Neither tool was erroring. Both were returning the corpus hit
verbatim and leaving out the one field that makes a hit USABLE:

  * ``refs`` — :func:`legba.data.analysts.consult_on_demand._refs_from_tool_result`
    lifts a tool's citable substrate UUIDs out of ``result["refs"]`` and
    nothing else. A reader that omits the key contributes NOTHING to
    ``cited_substrate_refs``, so every hit it found is uncitable: the model
    could read the Vance interview and still not cite it.
  * a BOUND — the raw ``_source`` carries ``raw_body`` (the article's full
    HTML), ``archived_text`` AND ``best_body`` (a duplicate of one of them).
    A single live corpus doc measures 5-55 KB. The conversation hands a tool
    result to the model through ``_bounded_tool_json(result, 8000)``, so a
    54 KB ``read_document`` collapsed into ``{"truncated": true,
    "raw_prefix": "<7.9 KB of mid-JSON HTML>"}`` and an 8-hit
    ``search_corpus`` (45 KB) silently dropped 5 of its 8 rows.

So the projection is the fix, and the INVARIANT is the point: a corpus hit
either carries a citable ref or it is dropped with a counted reason
(:func:`project_corpus_hits`). ``count`` can never again exceed ``len(refs)``
for this reader, and an empty-refs-with-hits result cannot be constructed.

Everything here is pure — no I/O, no pool, no client — so the invariant is
property-testable over recorded OpenSearch responses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

#: Per-hit body budget for ``search_corpus``. Sized so a full default page of
#: hits plus their facets lands inside the 8 KB conversation bound — the
#: planner sees EVERY hit it was told it got, instead of the three that
#: survived the raw-``_source`` rows in the live run.
SNIPPET_CHARS = 280

#: Body budget for ``read_document``. This reader exists to hand over a whole
#: article, so it gets the lion's share of the 8 KB message; the remainder
#: covers the facets and the envelope. A longer body is cut HERE, with the
#: cut declared in-band (``body_truncated`` + ``body_chars_total``), rather
#: than left to the outer ``_bounded_tool_json`` chop that turns the whole
#: result into an unparseable ``raw_prefix`` string.
DOCUMENT_BODY_CHARS = 6000

#: Body-bearing corpus fields in preference order. ``archived_text`` is the
#: evidence-archiver's clean full article text (P2-1) and wins outright;
#: ``text`` is the chat-platform message body (R6); ``raw_body`` is usually
#: the source's raw HTML, so it ranks below both but above a summary.
_BODY_FIELDS = (
    "archived_text",
    "text",
    "distilled_body",
    "raw_body",
    "best_body",
    "summary",
)

#: Facets worth carrying on a projected row. Deliberately NOT the whole
#: ``_source``: every body-shaped field is folded into one ``snippet`` /
#: ``body`` by :func:`_best_body`, and re-emitting them would re-create the
#: size blow-up this module exists to remove.
_FACET_FIELDS = (
    "title",
    "source_id",
    "canonical_url",
    "language",
    "modality",
    "published_at",
    "fetched_at",
    "geo",
    "tags",
    "license_class",
    "retrieval_origin",
    "source_credibility",
)

#: The RAW body fields the GATHER citation builder grounds a [N] marker on
#: (``inline_target._citation_entry``'s faithfulness trust boundary, and
#: ``verify._marker_to_evidence`` behind it). They stay on the projection under
#: their ORIGINAL names — a corpus hit that lost them would silently demote
#: every gathered citation to title-only — but bounded to
#: :data:`CITATION_SOURCE_CHARS`, which is the cap the citation entry applies
#: anyway. Carried under ``row["source"]`` / the read_document ``document``,
#: where ``gather_surface._gathered_signals_from_result`` already looks.
CITATION_SOURCE_FIELDS = (
    "archived_text",
    "raw_body",
    "text",
    "distilled_body",
    "summary",
)

#: Mirrors ``inline_target._SOURCE_TEXT_CHARS`` — the judge never grounds on
#: more than this, so carrying more is pure weight.
CITATION_SOURCE_CHARS = 3200

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


def strip_markup(text: str) -> str:
    """Flatten source HTML to readable prose.

    The corpus stores ``raw_body`` exactly as the source served it, which for
    an RSS-derived signal is a wrapped ``<article>`` element. Handing that to
    a model spends the body budget on tag soup, so tags go, the handful of
    entities a news body actually carries are unescaped, and runs of
    whitespace collapse. Not a sanitizer — nothing here is rendered; this is
    a token-economy measure on text the model will only read.
    """
    if not text:
        return ""
    out = _TAG_RE.sub(" ", text)
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&rsquo;", "’"),
        ("&ldquo;", "“"),
        ("&rdquo;", "”"),
    ):
        out = out.replace(entity, char)
    out = _WS_RE.sub(" ", out)
    out = _BLANK_RE.sub("\n\n", out)
    return out.strip()


def _best_body(source: Mapping[str, Any]) -> tuple[str, str | None]:
    """The best body text on a corpus doc + the field it came from.

    Returns ``("", None)`` for a doc with no prose at all (a structured-payload
    signal — a GDELT CAMEO dump, say — whose body fields were deliberately
    omitted at index time by ``opensearch._as_text_field``). An honest empty,
    never a fabricated one.
    """
    for field in _BODY_FIELDS:
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            flattened = strip_markup(value)
            if flattened:
                return flattened, field
    return "", None


def _facets(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: source[k]
        for k in _FACET_FIELDS
        if source.get(k) is not None and source.get(k) != "" and source.get(k) != []
    }


def citation_source(source: Mapping[str, Any]) -> dict[str, Any]:
    """The citation-grade slice of a corpus doc: facets + the RAW body fields.

    Kept under the original field names and bounded to
    :data:`CITATION_SOURCE_CHARS`. This is what the GATHER path grounds a
    numbered [N] on; the model reads ``snippet`` / ``body`` instead, and
    ``consult_on_demand._bounded_tool_json`` drops this block from the
    CONVERSATION when the message would not otherwise fit — the result stays
    complete for in-process consumers, the context stays affordable.
    """
    out = _facets(source)
    for field in CITATION_SOURCE_FIELDS:
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            out[field] = value[:CITATION_SOURCE_CHARS]
    return out


def _clip(body: str, budget: int) -> tuple[str, bool]:
    """Cut ``body`` to ``budget`` chars on a word boundary; flag whether cut."""
    if len(body) <= budget:
        return body, False
    cut = body[:budget]
    space = cut.rfind(" ")
    if space > budget // 2:
        cut = cut[:space]
    return cut.rstrip() + "…", True


def citable_ref(raw: Any) -> str | None:
    """``raw`` as a canonical substrate-UUID string, or None.

    The corpus doc ``_id`` IS the ``signals.id`` (``opensearch.signal_to_doc``
    pins ``_id`` to it so a re-index overwrites in place), so a well-formed
    hit id is always a citable substrate ref. Anything that does not parse as
    a UUID is not a substrate row and must not reach ``cited_refs``.
    """
    if raw is None:
        return None
    try:
        return str(UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None


def project_corpus_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    snippet_chars: int = SNIPPET_CHARS,
) -> dict[str, Any]:
    """Project raw OpenSearch hits into citable, bounded ``search_corpus`` rows.

    THE INVARIANT (defect 2 of the 2026-09-16 review): every returned row
    carries an ``id`` that is a valid substrate UUID and appears in ``refs``,
    so ``len(refs) == count`` always and an empty-refs-with-hits result cannot
    be built. A hit that cannot yield a ref is DROPPED into ``dropped`` with a
    counted ``reason`` — visible, never silent.

    ``hits`` is the ``OpenSearchStore.search`` shape: ``{"id", "score",
    "source"}`` per hit.
    """
    rows: list[dict[str, Any]] = []
    refs: list[str] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for hit in hits:
        if not isinstance(hit, Mapping):
            dropped.append({"reason": "malformed_hit"})
            continue
        ref = citable_ref(hit.get("id"))
        if ref is None:
            # A corpus doc whose _id is not a signal UUID cannot be cited, and
            # a row the model cannot cite is a row it should not weigh.
            dropped.append({"reason": "no_citable_id", "id": hit.get("id")})
            continue
        if ref in seen:
            dropped.append({"reason": "duplicate_id", "id": ref})
            continue
        source = hit.get("source")
        source = source if isinstance(source, Mapping) else {}
        body, body_field = _best_body(source)
        snippet, clipped = _clip(body, snippet_chars)
        row: dict[str, Any] = {"id": ref, "score": hit.get("score")}
        row.update(_facets(source))
        row["snippet"] = snippet
        row["snippet_field"] = body_field
        if clipped:
            # The planner is told the row is a teaser and told the tool that
            # opens it — the read_document call it never made in the live run.
            row["snippet_truncated"] = True
            row["body_chars_total"] = len(body)
            row["read_full_with"] = "read_document"
        # The nested citation block, under the key the GATHER numbering path
        # already reads (``gather_surface._gathered_signals_from_result``).
        row["source"] = citation_source(source)
        seen.add(ref)
        refs.append(ref)
        rows.append(row)

    out: dict[str, Any] = {"rows": rows, "refs": refs, "count": len(rows)}
    if dropped:
        out["dropped"] = dropped
        out["dropped_count"] = len(dropped)
    return out


def project_corpus_document(
    doc_id: str,
    source: Mapping[str, Any],
    *,
    origin: str = "corpus",
    body_chars: int = DOCUMENT_BODY_CHARS,
) -> dict[str, Any]:
    """Project one corpus ``_source`` into a citable, bounded document result.

    Carries ``refs`` (defect 1: the document the model reads must be the
    document the model can cite) and ``count`` — ``1`` for a real document,
    ``0`` for a body-less one — so the consult trace stops rendering a
    successful read as ``count 0, refs 0``.
    """
    body, body_field = _best_body(source)
    clipped_body, truncated = _clip(body, body_chars)
    ref = citable_ref(doc_id)
    document: dict[str, Any] = {"id": str(doc_id)}
    document.update(_facets(source))
    document["body"] = clipped_body
    document["body_field"] = body_field
    document["body_chars"] = len(clipped_body)
    if truncated:
        document["body_truncated"] = True
        document["body_chars_total"] = len(body)
    # The RAW fields under their original names, so a GATHER assessor that
    # numbers this document keeps grounding its [N] on the real article rather
    # than on our flattened reading copy.
    for field in CITATION_SOURCE_FIELDS:
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            document[field] = value[:CITATION_SOURCE_CHARS]
    return {
        "status": "found",
        "doc_id": str(doc_id),
        "origin": origin,
        "document": document,
        "refs": [ref] if ref else [],
        # A doc with no prose is a real miss for a READER even though the row
        # exists — count says so rather than implying a body arrived.
        "count": 1 if body else 0,
    }


#: Body-bearing ``signals.payload`` keys, preference order — the Postgres-side
#: twin of :data:`_BODY_FIELDS` (the corpus doc projection reads the same
#: payload, so the two orders must agree or an un-indexed row would read
#: differently from an indexed one).
_PAYLOAD_BODY_FIELDS = (
    "archived_text",
    "text",
    "distilled_body",
    "raw_body",
    "summary",
)

#: ``signals`` columns carried onto a Postgres-fallback document.
_SIGNAL_COLUMNS = (
    "source_id",
    "canonical_url",
    "language",
    "modality",
    "geo",
    "tags",
)


def _payload(raw: Any) -> dict[str, Any]:
    """A ``signals.payload`` as a dict (asyncpg jsonb may arrive as str)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def project_signal_row(
    row: Mapping[str, Any],
    *,
    origin: str = "signals",
    body_chars: int = DOCUMENT_BODY_CHARS,
) -> dict[str, Any]:
    """Project a ``signals`` row into the ``read_document`` result shape.

    The Postgres side of the namespace fallback: a signal that exists but is
    not in the corpus index (not yet indexed, or indexed without prose) is
    still a readable, citable document.
    """
    payload = _payload(row.get("payload"))
    source: dict[str, Any] = {
        k: row.get(k) for k in _SIGNAL_COLUMNS if row.get(k) is not None
    }
    source["title"] = payload.get("title")
    fetched = row.get("fetched_at")
    source["fetched_at"] = fetched.isoformat() if hasattr(fetched, "isoformat") else fetched
    source["published_at"] = payload.get("published_at")
    for field in _PAYLOAD_BODY_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            source[field] = value
    return project_corpus_document(
        str(row.get("id")), source, origin=origin, body_chars=body_chars,
    )


def project_finding_row(
    row: Mapping[str, Any], *, body_chars: int = DOCUMENT_BODY_CHARS,
) -> dict[str, Any]:
    """Project an ``analyst_outputs`` row into the ``read_document`` shape.

    A planner holding a ref from ``list_findings`` will eventually call
    ``read_document`` on it. That id is not in the corpus index, and the old
    reader answered ``not_found`` — for a row that is very much there.
    ``origin`` marks it as the platform's OWN synthesis so the model weighs it
    as analysis rather than as a source document.
    """
    body, truncated = _clip(strip_markup(str(row.get("body") or "")), body_chars)
    produced = row.get("produced_at")
    document: dict[str, Any] = {
        "id": str(row.get("id")),
        "title": row.get("title"),
        "kind": row.get("kind"),
        "target_id": row.get("target_id"),
        "analyst_id": row.get("analyst_id"),
        "confidence": (
            float(row["confidence"]) if row.get("confidence") is not None else None
        ),
        "severity": row.get("severity"),
        "produced_at": (
            produced.isoformat() if hasattr(produced, "isoformat") else produced
        ),
        "body": body,
        "body_field": "analyst_outputs.body",
        "body_chars": len(body),
    }
    if truncated:
        document["body_truncated"] = True
    return {
        "status": "found",
        "doc_id": str(row.get("id")),
        "origin": "analyst_output",
        "document": document,
        "refs": [str(row.get("id"))],
        "count": 1 if body else 0,
    }


__all__ = [
    "CITATION_SOURCE_CHARS",
    "CITATION_SOURCE_FIELDS",
    "DOCUMENT_BODY_CHARS",
    "SNIPPET_CHARS",
    "citable_ref",
    "citation_source",
    "project_corpus_document",
    "project_corpus_hits",
    "project_finding_row",
    "project_signal_row",
    "strip_markup",
]
