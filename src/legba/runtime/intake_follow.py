# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FOLLOW step — references found in a fetched document become ingestion
requests, for a source that declares a follow policy.

Producer (a) of the intake. Behind ``LEGBA_INTAKE_FOLLOW`` (off by default)
AND the source's own ``follow`` block (absent on every live source), so it
does nothing until both are set. Called by the document sink after it
publishes written signals; best-effort — a failure here is logged and never
costs the signal.

For each written signal, references are extracted (in a worker thread — the
body can be a whole page) by kind:

* ``article`` — the document's own page, when the feed carried only a
  summary (a body under :data:`FULL_BODY_CHARS` characters);
* ``anchor`` — ``<a href>`` links in the HTML the payload carries (a page
  landed by the ``page`` kind carries them pre-classified as
  ``payload.references`` — its body is extracted text, not markup);
* ``document`` — anchors to a PDF / office / CSV file.

Each reference the policy allows (host, kind; at most ``max_per_document``)
is submitted with reason ``followed_reference`` under the policy's pipeline
and sink, as a child of the signal (and of the intake request that wrote the
signal, one level deeper, when there is one). Admission then applies every
rule again — depth, fan-out, duplicates, freshness, budgets — and records any
refusal.
"""

from __future__ import annotations

import asyncio
import logging
import os
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..data.intake_contract import host_of
from .intake_context import current_intake_run

logger = logging.getLogger(__name__)

__all__ = ["FOLLOW_ENV", "extract_references", "follow_enabled", "follow_written"]

FOLLOW_ENV = "LEGBA_INTAKE_FOLLOW"
#: A payload body at least this long already IS the article.
FULL_BODY_CHARS = 1500
_DOC_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".odt", ".ods", ".ppt", ".pptx")
_HTML_FIELDS = ("raw_body", "summary", "body", "description", "content", "html")
_TEXT_FIELDS = ("raw_body", "body", "text", "archived_text")
_MAX_HTML_CHARS = 500_000


def follow_enabled() -> bool:
    return os.environ.get(FOLLOW_ENV, "").strip().lower() in {"1", "on", "true", "yes"}


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v.strip())


def _doc_url(signal: Any) -> str | None:
    payload = getattr(signal, "payload", None) or {}
    for cand in (getattr(signal, "canonical_url", None), payload.get("link"),
                 payload.get("url"), payload.get("source_url")):
        if isinstance(cand, str) and cand.startswith(("http://", "https://")):
            return cand
    return None


def extract_references(signal: Any, kinds: list[str]) -> list[tuple[str, str]]:
    """``[(url, link_kind), ...]`` in document order, deduplicated, the
    document's own URL excluded from anchors. Pure (no I/O)."""
    payload = getattr(signal, "payload", None) or {}
    doc = _doc_url(signal)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if "article" in kinds and doc:
        body_len = max((len(payload.get(f) or "") for f in _TEXT_FIELDS
                        if isinstance(payload.get(f), str)), default=0)
        if body_len < FULL_BODY_CHARS:
            out.append((doc, "article"))
            seen.add(doc)
    refs = payload.get("references")
    if isinstance(refs, list) and ("anchor" in kinds or "document" in kinds):
        # I-2: a fetched page carries its body as extracted TEXT (no markup)
        # and its outbound links already classified (data/sources/page_meta).
        for ref in refs:
            url, kind = (ref or {}).get("url"), (ref or {}).get("kind")
            if isinstance(url, str) and kind in kinds and url not in seen and url != doc:
                out.append((url, kind))
                seen.add(url)
        return out
    if "anchor" in kinds or "document" in kinds:
        parser = _Anchors()
        for f in _HTML_FIELDS:
            val = payload.get(f)
            if isinstance(val, str) and "<a" in val.lower():
                try:
                    parser.feed(val[:_MAX_HTML_CHARS])
                except Exception:  # malformed markup: what was parsed stands
                    pass
        for href in parser.hrefs:
            url = urljoin(doc or "", href)
            if not url.startswith(("http://", "https://")):
                continue
            url = url.split("#", 1)[0]
            if url in seen or url == doc:
                continue
            path = urlsplit(url).path.lower()
            kind = "document" if path.endswith(_DOC_SUFFIXES) else "anchor"
            if kind in kinds:
                out.append((url, kind))
                seen.add(url)
    return out


async def follow_written(core: Any, signals: list[Any]) -> int:
    """Submit follow requests for ``signals`` written under ``core``'s
    source. Returns how many were submitted (admitted or not)."""
    policy = getattr(core.descriptor, "follow", None)
    if policy is None or not signals or not follow_enabled():
        return 0
    # C-1: links in a CLAIM source's rows are never followed (the schema
    # refuses a follow block on a claim source; this guards a stored body that
    # predates the rule). The intake refuses such a request too.
    scope = getattr(core.descriptor, "scope", None)
    if str(getattr(scope, "source_class", "") or "") == "claim":
        return 0
    from ..data.intake_store import SubmitRequest, submit_request  # noqa: PLC0415

    source_id = core.descriptor.identity.id
    run = current_intake_run()
    submitted = 0
    for sig in signals:
        try:
            refs = await asyncio.to_thread(extract_references, sig, list(policy.kinds))
            doc_host = host_of(_doc_url(sig))
            refs = [(u, k) for u, k in refs if policy.allows_host(doc_host, host_of(u))]
            for url, kind in refs[: policy.max_per_document]:
                await submit_request(core.deps.pg_pool, SubmitRequest(
                    target_kind="address", address=url, pipeline_source_id=policy.pipeline,
                    sink=policy.sink, reason="followed_reference",
                    requested_by=f"source::{source_id}", link_kind=kind,
                    follow_source_id=source_id, follow_document_host=doc_host,
                    parent_signal_id=sig.signal_id,
                    parent_request_id=run.request_id if run else None,
                    depth=(run.depth + 1) if run else 1,
                ))
                submitted += 1
        except Exception:
            logger.warning("intake.follow.failed source=%s signal=%s", source_id,
                           getattr(sig, "signal_id", None), exc_info=True)
    return submitted
