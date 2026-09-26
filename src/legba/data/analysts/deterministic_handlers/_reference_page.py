# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — one fetched page: its TEXT, its DATE, its ARCHIVE, its SPAN normaliser.

Ported from ``planning/PROGRAM2_2026-09-16/R1/build_core_reference.py``, which
measured every rule below on two live runs. Nothing here is new invention; what
changed is that the archive is the platform's content-addressed store rather
than a directory beside a planning file, and that the publish-date extractor is
now load-bearing rather than informational — it is the DATE GATE's only input.

WHY THE DATE EXTRACTOR IS THE MOST IMPORTANT FUNCTION IN THIS SUBSYSTEM. R1
run 2 shipped a FALSE development: "Israel Katz… has been named the new Defence
Minister", dated 2026-09-16, span verbatim, URL really fetched — and the page
carried no publish date in any metadata, so the model read the site's MASTHEAD
("Wednesday, Sep 16, 2026") as the article date. The article was from November
2024. A reference carrying that item marks a CORRECT live read as contradicted,
which is strictly worse than having no reference at all. So: a date the
extractor cannot find is NOT a date, the development is rejected, and the
rejection is counted. A masthead is never evidence of when an article was
published.

THE EXTRACTOR ITSELF now lives in :mod:`._reference_dates` — six ranked rungs,
a ``date_source`` per rung, and a disagreement record — and this module imports
it ONE WAY and re-exports :func:`extract_publish_date` / :func:`parse_iso_date`
so every caller and test is unchanged. Read that module's banner for the
precedence and for the four fences that keep a masthead out now that a visible
dateline is admissible at all.

WHY STDLIB HTML PARSING. ``bs4`` is not a dependency of the runtime image and
adding one for this would be a poor trade: the parse feeds a SUBSTRING CHECK,
so what matters is that the same text is produced at archive time and at verify
time, which one stdlib parser guarantees by being the only parser.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from ....data.archive import archive_root
from ._reference_dates import (
    PageDate,
    extract_publish_date,
    parse_iso_date,
    resolve_publish_date,
)

logger = logging.getLogger(__name__)

#: Reference archives live under their own subtree of the platform archive root
#: so nothing here can collide with, or be garbage-collected by, the evidence
#: CAS. Layout is the CAS layout (``<sha[:2]>/<sha>``) over the sha256 of the
#: EXTRACTED TEXT — the exact bytes a span is checked against.
ARCHIVE_SUBDIR = "reference"

#: The page text handed to the model per fetch. R1 measured the whole loop at
#: ~1–1.7M prompt tokens with this at 7,000; it is the single largest lever on
#: core-plane time and it is an option on the descriptor.
DEFAULT_PAGE_CHARS_TO_MODEL = 7000

#: Below this many characters a 200 is almost certainly a consent wall, a
#: paywall stub or a challenge interstitial rather than an article. R1 measured
#: the failure this catches: the S3 review found the evidence_archiver's own
#: challenge detector capped at 500 chars and missing 5–13 kB interstitials, so
#: a block read as "the web had nothing" — a FALSE ABSENCE.
THIN_PAGE_CHARS = 1000

_DROP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer",
              "form", "iframe", "aside", "template", "button", "select"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "blockquote", "figcaption", "td", "th",
               "header", "main", "ul", "ol", "dl", "dd", "dt", "pre", "hr"}


class _Text(HTMLParser):
    """HTML → readable text, keeping JSON-LD blocks aside for the date pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.ldjson: list[str] = []
        self._in_ld = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "script" and (attributes.get("type") or "").lower() == (
            "application/ld+json"
        ):
            self._in_ld = True
        if tag in _DROP_TAGS:
            self.skip += 1
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self._in_ld = False
        if tag in _DROP_TAGS and self.skip:
            self.skip -= 1
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self.ldjson.append(data)
            return
        if self.skip:
            return
        self.out.append(data)


def html_to_text(raw: str) -> tuple[str, list[str]]:
    """``(readable text, JSON-LD blocks)``. A malformed page yields what parsed."""
    parser = _Text()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 — a broken page still yields its prefix
        pass
    text = _html.unescape("".join(parser.out))
    text = text.replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), parser.ldjson


# ---------------------------------------------------------------------------
# The publish date — the DATE GATE's only input, re-exported from the LADDER
# ---------------------------------------------------------------------------
#
# ``_reference_dates`` owns the six rungs, the precedence and the disagreement
# record. Imported ONE WAY and re-exported here so that ``_reference_fences``,
# ``_reference_loop`` and ``_contrary_fences`` — every one of which imports
# ``parse_iso_date`` or ``extract_publish_date`` from this module — are
# untouched by the split.


# ---------------------------------------------------------------------------
# Span normalisation — whitespace + curly punctuation ONLY, never the wording
# ---------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00AD], None
)
_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "‒": "-", "―": "-", "−": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "´": "'", "ʼ": "'", "`": "'",
}


def norm_span(value: str) -> str:
    """The normaliser both the archive check and the verifier use.

    It folds whitespace, curly punctuation and zero-width characters and
    NOTHING else. It never lowercases, never strips punctuation, never edits a
    word. A span that passes after this is the page's own sentence; a span that
    fails is not, and the difference has to stay that sharp or "verified" means
    "approximately verified".
    """
    value = unicodedata.normalize("NFC", value)
    value = value.translate(_ZERO_WIDTH)
    for old, new in _PUNCT_MAP.items():
        value = value.replace(old, new)
    return re.sub(r"\s+", " ", value).strip()


def host_of(url: str) -> str:
    """The registrable-ish host: lowercase, ``www.`` stripped. ``""`` if unparseable."""
    try:
        return (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def url_key(url: str) -> str:
    """Loose URL identity, for ARCHIVE LOOKUP ONLY — never for verification."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    return host_of(url) + (parts.path or "/").rstrip("/").lower()


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


@dataclass
class ArchivedPage:
    """One page this run read, and everything a later reader needs to re-argue it."""

    url: str
    final_url: str
    host: str
    status: int
    chars: int
    publish_date: str | None = None
    date_source: str = "none"
    #: ``"<source>=<YYYY-MM-DD>"`` for every weaker rung that answered
    #: with a DIFFERENT date than ``date_source`` did. Empty on the
    #: overwhelming majority of pages; never a rejection, always a record.
    date_disagreement: tuple[str, ...] = ()
    archive_sha256: str = ""
    archive_path: str = ""
    fetch_ts: str = ""
    error: str = ""
    licence_class: str | None = None
    depth: str = ""
    depth_reason: str = ""
    stored_body: bool = False
    text: str = field(default="", repr=False)

    @property
    def usable(self) -> bool:
        """A page a development may be anchored to: served, and not a stub."""
        return self.status == 200 and self.chars >= THIN_PAGE_CHARS

    def manifest_line(self) -> str:
        return (
            f"- {self.final_url or self.url}  [{self.host}, published "
            f"{self.publish_date or 'UNDATED'}]"
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "host": self.host,
            "status": self.status,
            "chars": self.chars,
            "publish_date": self.publish_date,
            "date_source": self.date_source,
            "date_disagreement": list(self.date_disagreement),
            "archive_sha256": self.archive_sha256,
            "archive_path": self.archive_path,
            "fetch_ts": self.fetch_ts,
            "error": self.error,
            "licence_class": self.licence_class,
            "depth": self.depth,
            "depth_reason": self.depth_reason,
            "stored_body": self.stored_body,
        }


class ReferenceArchive:
    """Content-addressed page archive, plus the in-run index a verifier walks.

    Two lookups, and the distinction between them is load-bearing:

      * ``by_url`` is EXACT — the URL the model wrote must be a URL this run
        fetched. That is the fetched-URL manifest fence, enforced at verify time
        as well as prompted at commit time.
      * ``by_key`` is LOOSE (host + path, no scheme/query/trailing slash) and is
        used ONLY to locate the archived text after the exact lookup has already
        decided the development is admissible. A loose match never admits a URL;
        it only finds bytes for one already admitted.

    ``root=None`` keeps everything in memory — which is what the tests use, and
    what a deployment with no writable archive root degrades to rather than
    losing the run.
    """

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self.by_url: dict[str, ArchivedPage] = {}
        self.by_key: dict[str, ArchivedPage] = {}
        self._text: dict[str, str] = {}

    @classmethod
    def open_default(cls, *, enabled: bool = True) -> "ReferenceArchive":
        """The configured archive root, or memory-only if it is unwritable."""
        if not enabled:
            return cls(None)
        try:
            root = archive_root() / ARCHIVE_SUBDIR
            root.mkdir(parents=True, exist_ok=True)
            return cls(root)
        except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
            logger.warning(
                "reference_builder.archive_unwritable root=%s err=%s — the run "
                "keeps its pages in memory; spans are still verified, but they "
                "cannot be re-verified after the process exits",
                os.environ.get("LEGBA_ARCHIVE_ROOT"), exc,
            )
            return cls(None)

    def put(self, page: ArchivedPage, text: str) -> ArchivedPage:
        """Archive ``text`` under sha256(text) and index the page by its URLs."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        page.archive_sha256 = digest
        page.archive_path = f"{ARCHIVE_SUBDIR}/{digest[:2]}/{digest}"
        if self.root is not None and text:
            try:
                target = self.root / digest[:2] / digest
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    tmp = target.with_suffix(".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(target)
                page.stored_body = True
            except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
                logger.warning(
                    "reference_builder.archive_write_failed sha=%s err=%s",
                    digest[:16], exc,
                )
        self._text[digest] = text
        page.text = text
        for candidate in (page.url, page.final_url):
            if candidate:
                self.by_url.setdefault(candidate, page)
                self.by_key.setdefault(url_key(candidate), page)
        return page

    def index_unstored(self, page: ArchivedPage, text: str) -> ArchivedPage:
        """Index a page whose body the LICENCE RULE forbids storing.

        The text is held for THIS process only — long enough for the span check
        to run against it — and never written to the archive. The page keeps no
        ``archive_sha256``, so a later reader cannot be misled into thinking the
        bytes are re-servable; the development that results is flagged
        ``span_source: snippet`` by the fences, which is the ledger's own rule
        for a teaser-depth host.
        """
        page.archive_sha256 = ""
        page.archive_path = ""
        page.stored_body = False
        page.text = text
        for candidate in (page.url, page.final_url):
            if candidate:
                self.by_url.setdefault(candidate, page)
                self.by_key.setdefault(url_key(candidate), page)
        return page

    def text_for(self, page: ArchivedPage) -> str:
        """The archived text: from memory, else from the store, else ``""``."""
        if not page.archive_sha256:
            return page.text
        cached = self._text.get(page.archive_sha256)
        if cached is not None:
            return cached
        if self.root is not None and page.archive_sha256:
            path = self.root / page.archive_sha256[:2] / page.archive_sha256
            try:
                loaded = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — a missing object is not a crash
                return ""
            self._text[page.archive_sha256] = loaded
            return loaded
        return ""

    def lookup_exact(self, url: str) -> ArchivedPage | None:
        return self.by_url.get((url or "").strip())

    def lookup_loose(self, url: str) -> ArchivedPage | None:
        return self.by_key.get(url_key(url))

    def fetched_pages(self) -> list[ArchivedPage]:
        """Every USABLE page, oldest publish date first — the manifest's order."""
        pages = {id(p): p for p in self.by_url.values()}.values()
        usable = [p for p in pages if p.usable]
        usable.sort(key=lambda p: (p.publish_date or "", p.final_url or p.url))
        return usable


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def coerce_page_text(body: str) -> tuple[str, list[str]]:
    """``body`` → ``(text, ldjson)``, tolerating a non-HTML payload.

    A JSON or plain-text response is passed through as its own text rather than
    run through the tag stripper, so a span quoted from an official JSON feed
    still verifies.
    """
    stripped = body.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except ValueError:
            pass
        else:
            return body.strip(), []
    if "<" not in body:
        return body.strip(), []
    return html_to_text(body)


async def fetch_and_archive(
    url: str,
    archive: "ReferenceArchive",
    *,
    timeout: float = 30.0,
    user_agent: str | None = None,
) -> ArchivedPage:
    """Fetch ONE url through the platform's SSRF-guarded transport and archive it.

    The scheduled builder never calls this — it goes through the ``web_access``
    ACTION PACK, which adds the invocation governor and the cost ledger on top
    of this same guard. This is the door for OPERATOR-RUN tooling that has no
    ``ToolContext`` to build a pack binding from: the top-up merge, which must
    re-verify a paid lane's spans against the pages they cite, and the
    proving harness.

    The guard is NOT optional on either door. ``guarded_async_client`` validates
    every connection including redirect hops, so a URL that resolves to a
    private address is refused rather than fetched — which matters more here
    than in the loop, because the URLs reaching this function were written by a
    lane the platform did not run.

    Never raises: a refusal, a timeout or a transport error returns an
    ``ArchivedPage`` with ``status=0`` and the reason in ``error``, and the
    fences drop the development that cited it and count the drop.
    """
    from ...sources._egress import EgressBlockedError, guarded_async_client

    headers = {
        "User-Agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    page = ArchivedPage(
        url=url, final_url=url, host=host_of(url), status=0, chars=0,
        fetch_ts=now_iso(),
    )
    try:
        async with guarded_async_client(
            follow_redirects=True, timeout=timeout, headers=headers
        ) as client:
            response = await client.get(url)
        raw = response.text
        page.status = int(response.status_code)
        page.final_url = str(response.url)
        page.host = host_of(page.final_url)
    except EgressBlockedError as exc:
        page.error = f"egress_blocked: {exc!s}"
        return page
    except Exception as exc:  # noqa: BLE001 — a dead host is a dropped item
        page.error = f"{type(exc).__name__}: {exc!s}"
        return page

    text, ldjson = coerce_page_text(raw)
    # The WHOLE ladder: this door holds the response, so it can offer the URL,
    # the extracted text (the dateline rung) and the headers (``Last-Modified``,
    # official-class hosts only) that the pack-mediated loop cannot.
    dated = resolve_publish_date(
        raw, ldjson, url=page.final_url or url, text=text,
        headers=dict(response.headers),
    )
    page.publish_date = dated.value
    page.date_source = dated.source
    page.date_disagreement = dated.disagreements
    page.chars = len(text)
    archive.put(page, text)
    return page


def page_summary(page: ArchivedPage, meta: Mapping[str, Any] | None = None) -> str:
    parts = [f"{page.host} status={page.status} chars={page.chars}"]
    if page.publish_date:
        parts.append(f"published={page.publish_date} ({page.date_source})")
    else:
        parts.append("UNDATED")
    if meta:
        parts.extend(f"{k}={v}" for k, v in meta.items())
    return " ".join(parts)


__all__ = [
    "ARCHIVE_SUBDIR",
    "DEFAULT_PAGE_CHARS_TO_MODEL",
    "THIN_PAGE_CHARS",
    "ArchivedPage",
    "PageDate",
    "ReferenceArchive",
    "coerce_page_text",
    "extract_publish_date",
    "fetch_and_archive",
    "host_of",
    "html_to_text",
    "norm_span",
    "now_iso",
    "page_summary",
    "parse_iso_date",
    "resolve_publish_date",
    "url_key",
]
