# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic pages in realistic shapes for the ``page`` kind's tests (lane
I-2). Written for the tests — no real article is downloaded or copied."""

from __future__ import annotations

import json
from typing import Any

import httpx

PARA = ("dockworkers in Rotterdam and Antwerp extended the stoppage on Thursday as talks over "
        "automation and pay broke down, the union said, with container traffic backed up along the Scheldt.")


def article(
    *, title: str = "Port strike widens to Antwerp", paras: int = 12, lang: str = "en-GB",
    canonical: str | None = None, published: str | None = "2026-11-13T08:00:00Z",
    published_where: str = "json_ld", author: str | None = "A. Writer",
    site: str | None = "Example News", description: str | None = "Dockworkers extend the stoppage.",
    paywall_declared: bool = False, extra_body: str = "", nav_links: int = 6,
    related_links: int = 3,
) -> bytes:
    ld: dict[str, Any] = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": title}
    meta = ""
    if published and published_where == "json_ld":
        ld["datePublished"] = published
    elif published and published_where == "meta":
        meta += f"<meta property='article:published_time' content='{published}'>"
    if author:
        ld["author"] = {"@type": "Person", "name": author}
    if paywall_declared:
        ld["isAccessibleForFree"] = False
    head = (f"<title>{title} | {site or 'News'}</title>"
            + (f"<meta property='og:site_name' content='{site}'>" if site else "")
            + (f"<meta name='description' content='{description}'>" if description else "")
            + (f"<link rel='canonical' href='{canonical}'>" if canonical else "")
            + meta + f"<script type='application/ld+json'>{json.dumps(ld)}</script>")
    nav = "<nav>" + "".join(f"<a href='/section{i}'>Section {i}</a>" for i in range(nav_links)) + "</nav>"
    time_tag = (f"<time datetime='{published}'>13 November</time>"
                if published and published_where == "time" else "")
    body = "".join(f"<p>Paragraph {i}: {PARA}</p>" for i in range(paras))
    related = "<p>Related: " + " ".join(
        f"<a href='https://news.example.org/2026/11/1{i}/story-{i}'>story {i}</a>" for i in range(related_links)
    ) + " and <a href='https://other.example.com/files/report.pdf'>the full report</a>.</p>"
    return (f"<!doctype html><html lang='{lang}'><head>{head}</head><body>{nav}<article>"
            f"<h1>{title}</h1>{time_tag}{body}{related}{extra_body}</article>"
            "<footer><a href='/about'>About</a> Copyright</footer></body></html>").encode()


def section_front(n: int = 60) -> bytes:
    items = "".join(f"<li><a href='https://news.example.org/2026/11/13/item-{i}'>A headline about "
                    f"event number {i} in the region today</a></li>" for i in range(n))
    return (f"<!doctype html><html lang='en'><head><title>World news</title></head><body>"
            f"<main><h1>World</h1><ul>{items}</ul></main></body></html>").encode()


def soft_404() -> bytes:
    return ("<!doctype html><html lang='en'><head><title>Page not found | Example News</title></head>"
            "<body><main><h1>Sorry, we can't find that page</h1><p>The page you requested could not be "
            "found. It may have been moved or deleted. Try the search box or return to the home page."
            "</p></main></body></html>").encode()


def login_page() -> bytes:
    return ("<!doctype html><html lang='en'><head><title>Sign in</title></head><body><main>"
            "<h1>Sign in to continue</h1><form><input type='email' name='e'>"
            "<input type='password' name='p'></form><p>Members can read every story after signing "
            "in with their account. Forgot your password? Reset it here.</p></main></body></html>").encode()


def consent_page() -> bytes:
    return ("<!doctype html><html lang='en'><head><title>Before you continue</title></head><body><main>"
            "<h1>We value your privacy</h1><p>We and our partners use cookies and similar technologies "
            "to store and access information on your device, with your consent or on the basis of "
            "legitimate interest. You can accept all, reject all, or manage preferences.</p>"
            "<button>Accept all</button><button>Reject all</button></main></body></html>").encode()


def paywall_page() -> bytes:
    return ("<!doctype html><html lang='en'><head><title>Markets slide | Paper</title>"
            "<script type='application/ld+json'>{\"@type\":\"NewsArticle\",\"headline\":\"Markets slide\","
            "\"isAccessibleForFree\":false}</script></head><body><article><h1>Markets slide</h1>"
            "<p>Stocks fell sharply on Thursday as investors weighed the outlook for rates and the "
            "economy, with the main index heading for its worst week since the spring sell-off.</p>"
            "<p>Subscribe to continue reading. Already a subscriber? Sign in.</p></article></body></html>"
            ).encode()


def challenge_page() -> bytes:
    return ("<!doctype html><html><head><title>Just a moment...</title></head><body>"
            "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script>"
            "<p>Checking if the site connection is secure. Enable JavaScript and cookies to continue.</p>"
            "</body></html>").encode()


def thin_page() -> bytes:
    return ("<!doctype html><html lang='en'><head><title>Brief</title></head><body><article><h1>Brief</h1>"
            "<p>Officials confirmed the closure on Thursday.</p></article></body></html>").encode()


FEDERAL_REGISTER_WALL_TEXT = (
    "Due to aggressive automated scraping of FederalRegister.gov and eCFR.gov, programmatic access "
    "to these sites is limited to access to our extensive developer APIs. Please visit "
    "FederalRegister.gov API documentation or eCFR.gov API documentation to learn more about how to "
    "access the API. Your request has been flagged as potentially automated. If you are human user "
    "receiving this message, please complete the CAPTCHA (bot test) below and click \"Request "
    "Access\". You may occassionally be asked to complete the CAPTCHA again, this is normal and part "
    "of our security measures. An official website of the United States government. If you "
    "experiencing issues with the CAPTCHA or want to request a wider IP range, you can use the "
    "\"Site Help\" button found in the lower, right of this page to make a request."
)


def federal_register_wall() -> bytes:
    """The page www.federalregister.gov redirected to —
    the wall's own sentences, about 800 chars of text, a CAPTCHA."""
    paras = "".join(f"<p>{s.strip()}.</p>" for s in FEDERAL_REGISTER_WALL_TEXT.split(". ") if s.strip())
    return ("<!doctype html><html lang='en'><head><title>Request Access</title></head><body>"
            f"<main><h1>Request Access</h1>{paras}<form><div class='g-recaptcha'></div>"
            "<button>Request Access</button></form></main></body></html>").encode()


class Web:
    """A tiny web: url -> (status, body, content type). robots.txt answers
    404 (no rules) unless set."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, bytes, str]] = {}
        self.robots: dict[str, tuple[int, str]] = {}
        self.redirects: dict[str, str] = {}
        self.hits: list[str] = []

    def put(self, url: str, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8") -> str:
        self.pages[url] = (status, body, ctype)
        return url

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits.append(url)
        if request.url.path == "/robots.txt":
            status, text = self.robots.get(request.url.host, (404, ""))
            return httpx.Response(status, content=text.encode())
        if url in self.redirects:
            return httpx.Response(301, headers={"location": self.redirects[url]})
        status, body, ctype = self.pages.get(url, (404, b"<html><body>no</body></html>", "text/html"))
        return httpx.Response(status, content=body, headers={"content-type": ctype})


def client(web: Web, **kw: Any) -> httpx.AsyncClient:
    """An httpx client on the in-memory web, with the receipt tee — the
    shape the guarded client has in production."""
    from legba.data.sources.fetch_capture import ReceiptTeeTransport

    return httpx.AsyncClient(transport=ReceiptTeeTransport(httpx.MockTransport(web)),
                             follow_redirects=True, max_redirects=kw.get("max_redirects", 5))
