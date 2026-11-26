# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a fetched page STATES about itself — read from its markup, never guessed.

One lxml parse of the page (in the caller's worker thread) yields:

* ``title`` — ``og:title``, else the JSON-LD article ``headline``, else the
  first ``<h1>`` of the content region, else ``<title>``;
* ``published_at`` — ONLY a date the page declares: JSON-LD
  ``datePublished`` / ``dateCreated``, then a publication ``<meta>``
  (:data:`_PUBLISHED_META`), then a ``<time>`` marked as the publication
  (``itemprop="datePublished"`` / ``pubdate``), then the first ``<time
  datetime>`` inside ``<article>``. Nothing else: no URL-path dates, no
  "Last-Modified" header, no copyright year, no htmldate guess. Absent means
  absent; the caller records the fetch time beside it AS the fetch time;
* ``canonical`` — ``<link rel="canonical">``, resolved, kept ONLY when it is
  the same site as the fetched page (:func:`same_site`) — a cross-site
  canonical (a syndicated copy pointing at its origin, or a hostile page
  pointing at someone else's article) never renames the document;
* ``language`` — ``<html lang>`` / ``Content-Language`` meta / JSON-LD
  ``inLanguage`` / ``og:locale``, primary subtag;
* ``author``, ``site_name`` — JSON-LD, then ``<meta>``;
* ``paywall_declared`` — JSON-LD ``isAccessibleForFree: false``;
* ``password_field`` — an ``<input type="password">`` anywhere (login walls);
* ``references`` — the content region's outbound links, classified in the
  intake follow vocabulary (``document`` = a file link, ``anchor`` =
  anything else; ``article`` is the follow step's word for "the document's
  own page" and is never an outbound link), each with ``same_site``;
* ``link_density`` — anchor text / all text of the content region (the
  navigation gate reads it).

Pure (no I/O); lxml is imported lazily.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

__all__ = ["PageMeta", "parse_page_meta", "same_site", "DOCUMENT_SUFFIXES"]

#: File links the follow step classifies ``document`` (mirrors
#: ``runtime.intake_follow._DOC_SUFFIXES``).
DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".odt", ".ods", ".ppt", ".pptx")
#: At most this many references are kept per page (a section front can carry
#: thousands of links; the follow step takes ``max_per_document`` anyway).
MAX_REFERENCES = 200

#: Publication ``<meta>`` names/properties, in precedence order.
_PUBLISHED_META = (
    "article:published_time", "og:article:published_time", "datepublished",
    "publish-date", "publish_date", "pubdate", "parsely-pub-date",
    "sailthru.date", "dc.date.issued", "dcterms.issued", "dc.date", "date",
)
_AUTHOR_META = ("author", "article:author", "parsely-author", "sailthru.author", "dc.creator")
_NAV_TAGS = ("nav", "header", "footer", "aside", "form")
_ARTICLE_TYPES = {"article", "newsarticle", "reportagenewsarticle", "analysisnewsarticle",
                  "blogposting", "webpage", "report", "opinionnewsarticle", "liveblogposting"}
_WS = re.compile(r"\s+")
_SUBDOMAIN_PREFIXES = ("www.", "m.", "amp.", "mobile.")


@dataclass
class PageMeta:
    title: str | None = None
    title_basis: str | None = None
    html_title: str | None = None
    h1: str | None = None
    published_at: datetime | None = None
    published_basis: str = "absent"
    canonical: str | None = None
    canonical_declared: str | None = None
    canonical_basis: str = "fetched_url"
    language: str | None = None
    author: str | None = None
    site_name: str | None = None
    description: str | None = None
    paywall_declared: bool = False
    password_field: bool = False
    references: list[dict[str, Any]] = field(default_factory=list)
    link_density: float = 0.0
    content_chars: int = 0


def _norm_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for prefix in _SUBDOMAIN_PREFIXES:
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def same_site(a: str, b: str) -> bool:
    """Whether two URLs are the same site: equal hosts after a leading
    ``www.``/``m.``/``amp.``/``mobile.``, or one a subdomain of the other.
    Deliberately narrower than a public-suffix "registrable domain" (no
    suffix list ships in the tree): ``news.example.co.uk`` and
    ``example.co.uk`` match, two different ``*.co.uk`` sites never do."""
    ha, hb = _norm_host(a), _norm_host(b)
    if not ha or not hb:
        return False
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def _text(el: Any) -> str:
    return _WS.sub(" ", el.text_content() if el is not None else "").strip()


def _parse_dt(raw: Any) -> datetime | None:
    """An ISO-8601 date/datetime the page declared, as UTC. A bare date is
    kept at midnight UTC. Anything unparseable is NOT a date."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_ld_objects(doc: Any) -> Iterable[dict]:
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        raw = (script.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "@graph" in item:
                    stack.append(item["@graph"])
                yield item


def _ld_type(obj: dict) -> set[str]:
    t = obj.get("@type")
    types = t if isinstance(t, list) else [t]
    return {str(x).lower() for x in types if x}


def _name_of(v: Any) -> str | None:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        return _name_of(v.get("name"))
    if isinstance(v, list):
        names = [n for n in (_name_of(x) for x in v) if n]
        return ", ".join(names) or None
    return None


def _metas(doc: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in doc.xpath("//meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or m.get("http-equiv") or "").strip().lower()
        val = m.get("content")
        if key and val and key not in out:
            out[key] = val.strip()
    return out


def _content_region(doc: Any) -> Any:
    for xp in ("//article", "//main", '//*[@role="main"]'):
        found = doc.xpath(xp)
        if found:
            return max(found, key=lambda e: len(e.text_content() or ""))
    body = doc.xpath("//body")
    return body[0] if body else doc


def _region_stats(region: Any) -> tuple[int, int]:
    """``(all text chars, anchor text chars)`` of a region, nav chrome and
    scripts excluded."""
    counts = [0, 0]

    def _n(s: str | None) -> int:
        return len(_WS.sub(" ", s or "").strip())

    def _walk(el: Any, in_anchor: bool) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("script", "style", "noscript") or tag in _NAV_TAGS:
            return  # the subtree is chrome; its tail is counted by the parent
        in_anchor = in_anchor or tag == "a"
        own = _n(el.text)
        counts[0] += own
        if in_anchor:
            counts[1] += own
        for child in el:
            _walk(child, in_anchor)
            tail = _n(child.tail)
            counts[0] += tail
            if in_anchor:
                counts[1] += tail

    _walk(region, False)
    return counts[0], counts[1]


def _references(region: Any, base: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    own = base.split("#", 1)[0]
    for a in region.xpath(".//a[@href]"):
        if any(p.tag in _NAV_TAGS for p in a.iterancestors()):
            continue
        url = urljoin(base, a.get("href", "").strip()).split("#", 1)[0]
        if not url.startswith(("http://", "https://")) or url == own or url in seen:
            continue
        seen.add(url)
        kind = "document" if urlsplit(url).path.lower().endswith(DOCUMENT_SUFFIXES) else "anchor"
        out.append({"url": url, "kind": kind, "same_site": same_site(url, base),
                    "text": _text(a)[:200] or None})
        if len(out) >= MAX_REFERENCES:
            break
    return out


def parse_page_meta(html: str, url: str) -> PageMeta:
    """Everything :class:`PageMeta` holds, from ``html`` fetched at ``url``
    (the FINAL url after redirects). Never raises on bad markup — an empty
    :class:`PageMeta` is the answer for a document lxml cannot read."""
    from lxml import html as lxml_html  # noqa: PLC0415 — lazy (slim image)

    meta = PageMeta()
    try:
        doc = lxml_html.document_fromstring(html)
    except Exception:
        return meta
    metas = _metas(doc)
    articles = [o for o in _json_ld_objects(doc) if _ld_type(o) & _ARTICLE_TYPES]
    ld = next((o for o in articles if "webpage" not in _ld_type(o)), articles[0] if articles else {})

    t = doc.xpath("//title")
    meta.html_title = _text(t[0]) or None if t else None
    region = _content_region(doc)
    h1 = region.xpath(".//h1") or doc.xpath("//h1")
    meta.h1 = _text(h1[0]) or None if h1 else None
    for basis, cand in (("og:title", metas.get("og:title")), ("json_ld", _name_of(ld.get("headline"))),
                        ("h1", meta.h1), ("title", meta.html_title)):
        if cand:
            meta.title, meta.title_basis = cand.strip(), basis
            break

    for key in ("datePublished", "dateCreated"):
        dt = _parse_dt(ld.get(key))
        if dt is not None:
            meta.published_at, meta.published_basis = dt, f"json_ld:{key}"
            break
    if meta.published_at is None:
        for name in _PUBLISHED_META:
            dt = _parse_dt(metas.get(name))
            if dt is not None:
                meta.published_at, meta.published_basis = dt, f"meta:{name}"
                break
    if meta.published_at is None:
        for xp, basis in (('//time[@itemprop="datePublished"]', "time:itemprop"),
                          ("//time[@pubdate]", "time:pubdate"), ("//article//time[@datetime]", "time:article")):
            for el in doc.xpath(xp):
                dt = _parse_dt(el.get("datetime") or el.get("content"))
                if dt is not None:
                    meta.published_at, meta.published_basis = dt, basis
                    break
            if meta.published_at is not None:
                break

    meta.canonical = url
    links = doc.xpath('//link[@rel="canonical"]/@href')
    if links:
        declared = urljoin(url, links[0].strip())
        meta.canonical_declared = declared
        if declared.startswith(("http://", "https://")) and same_site(declared, url):
            meta.canonical, meta.canonical_basis = declared, "declared_same_site"
        else:
            meta.canonical_basis = "declared_cross_site_ignored"

    html_el = doc.xpath("/html")
    lang = (html_el[0].get("lang") if html_el else None) or metas.get("content-language") \
        or (ld.get("inLanguage") if isinstance(ld.get("inLanguage"), str) else None) or metas.get("og:locale")
    if lang:
        meta.language = re.split(r"[-_]", lang.strip().lower())[0][:8] or None

    meta.author = _name_of(ld.get("author")) or next(
        (metas[k] for k in _AUTHOR_META if metas.get(k) and not metas[k].startswith("http")), None)
    meta.site_name = metas.get("og:site_name") or _name_of(ld.get("publisher"))
    meta.description = metas.get("og:description") or metas.get("description") or (
        ld.get("description") if isinstance(ld.get("description"), str) else None)
    free = ld.get("isAccessibleForFree")
    meta.paywall_declared = free is False or (isinstance(free, str) and free.strip().lower() == "false")
    meta.password_field = bool(doc.xpath('//input[@type="password"]'))
    meta.references = _references(region, url)
    try:
        total, anchors = _region_stats(region)
    except RecursionError:  # pathologically nested markup: no density reading
        total, anchors = 0, 0
    meta.content_chars = total
    meta.link_density = round(anchors / total, 3) if total else 0.0
    return meta
