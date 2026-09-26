# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE FETCH LEG — the decisive page, read for real, through the real binding.

WHY THIS MODULE EXISTS. W-3 built the evidentiary contract
(:func:`legba.data.provenance.external_span_check.check_span`) as a PURE
function: it is handed a page and it gates a verdict. W-1 built the drain and
documented the checker as something that "fetches the URL (robots-gated)".
Neither lane was wrong about its own half; nobody owned the JOIN. The result
shipped on 2026-09-05 and every decisive verdict for the next day degraded to
``UNCHECKED/span_check_unavailable`` because the wrapper called the checker with
keywords it does not accept — and, underneath that, because *nothing anywhere
fetched the page*. The missing keyword was the visible symptom; the missing
fetch leg was the defect.

This module is the leg. It sits beside :func:`_external_audit_width.run_search`
— same binding, same unwrap idiom, same "return a reason, never raise" contract
— because a fetch and a search are the same kind of act: egress the drain
spends, through the pack the auditor was granted, with a named failure the
ledger can count.

F-7 — THE ROBOTS GATE, AND WHERE IT LIVES
-----------------------------------------
The 2026-09-05 width review named ``web_fetch``'s missing robots.txt check
(F-7) with the operator's answer already recorded: honour it. ``robots.py``
was built by the research lane as the shared answer, and its own module
docstring says so — *"A later lane wires the same helper into ``web_fetch``;
nothing here is specific to research."*

That wiring has NOT happened: :func:`agency.web_tools.web_fetch_tool` opens an
SSRF-guarded client and GETs the URL with no robots consultation anywhere in
its body. So this path gates itself, HERE, at the caller — deliberately, and
not as a stopgap:

* the gate must run BEFORE the tool call, because a robots-disallowed page must
  not be fetched at all; a check inside the tool would still have opened the
  connection or would have had to unwind one;
* the auditor's posture is stricter than a general planner's needs to be (it
  fails CLOSED on an unreachable robots.txt, per ``robots.py``), and pushing
  that into the shared tool would silently re-post every other ``web_fetch``
  caller;
* the refusal has to reach the LEDGER as a verdict class
  (``unchecked_reason='robots_disallowed'``), which a tool-level failure string
  cannot do without the caller interpreting it anyway.

A robots-disallowed URL is never fetched and its verdict degrades to UNCHECKED.
That is a real cost — a publisher who asks not to be read costs us a decisive
verdict — and it is the cost the posture was chosen with eyes open.

WHAT COMES BACK, AND WHAT G-3 NEEDS THAT IT DOES NOT
----------------------------------------------------
``web_fetch``'s ToolResult output is ``{url, status_code, content_type, body,
truncated}``. ``body`` is the raw decoded document, capped at 200,000 chars.
There are NO response headers in it and NO publication date — so G-3's
``published_at`` has to be recovered from the document itself.

Both halves are done with ``trafilatura``, which is already a base dependency
and already the house answer for exactly this pair (``sources/scrapers/
example_news.py`` reads ``extract`` + ``extract_metadata().date``;
``evidence_archiver._extract_text`` carries the lazy-import idiom this module
copies, because trafilatura's import is heavy and every other sub-handler in
this package would otherwise pay it at package-import time).

* **The text G-2 compares against** is the extracted main text, not the raw
  HTML. The shared fold (:mod:`data.provenance.text_fold`) folds unicode
  punctuation and whitespace; it does not strip tags or decode entities, so a
  span quoted from an article would fail to match raw markup for reasons that
  have nothing to do with whether the page says it. When extraction yields
  nothing (a non-HTML document, a page trafilatura cannot read) the raw body is
  used instead and :attr:`FetchedPage.extracted` records which — a fallback is
  a weaker check, not a different verdict, and the flag is what lets that be
  measured rather than assumed.
* **The date G-3 anchors on** is ``extract_metadata().date`` — trafilatura's
  own resolution over JSON-LD ``datePublished``, ``article:published_time``,
  ``og:*`` dates, ``<time datetime>`` and the URL slug. It is a DISCOVERY, never
  an invention: a page with no discoverable date yields ``None`` and G-3 does
  the demoting (see below). This module never guesses a date and never
  substitutes "today".

G-3 AND A MISSING DATE — SAY IT PLAINLY. ``check_span`` treats ``published_at
is None`` as ``DEMOTION_NO_PUBLISH_DATE`` whenever the read's evidence window is
measured: the verdict becomes NOT_FOUND. So a real Tier-1 page whose date this
module cannot recover LOSES a decisive verdict it may well have deserved. That
is W-3's ruling and this lane does not reverse it — a source that cannot be
placed in time cannot be shown to be a source the read could have seen. What
this module owes it is a serious extraction rather than a lazy one, which is why
the date comes from trafilatura's full metadata resolution and not from a
hand-rolled meta-tag regex.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..agency.robots import ALLOWED, DISALLOWED, NO_RULES, RobotsCache, robots_decision
from ..agency.web_tools import WEB_FETCH_USER_AGENT
from ._external_audit_sampling import (
    UNCHECKED_ROBOTS_DISALLOWED,
    UNCHECKED_SPAN_FETCH_FAILED,
)

logger = logging.getLogger(__name__)

#: The drain's own ceiling on one page fetch. Deliberately above the pack's
#: ``web_fetch`` default (15s) so the TOOL's timeout is the one that normally
#: fires and reports itself; this is the backstop for a binding that hangs
#: somewhere the tool's own timeout does not cover.
#:
#: 35, not 25, since ``web_fetch`` retries a TIMED-OUT fetch once (see
#: ``web_tools._FETCH_TIMEOUT_RETRIES``): a host that hangs twice now costs up
#: to 2 x 15s inside the tool, and a 25s backstop would fire first — turning
#: the tool's honest, named ``fetch_timed_out`` into this leg's generic
#: ``span_fetch_failed`` and re-hiding exactly the signal the retry exists to
#: expose. The retry spends no extra TOOL call, so the queue's
#: ``EGRESS_CALLS_PER_CLAIM`` budget is untouched — see the note in
#: :func:`fetch_decisive_page` on "no retry beyond the tool's own".
FETCH_TIMEOUT_SECONDS = 35.0

#: The robots.txt fetch's own budget. Separate from the page fetch because it is
#: a different host path and a slow robots.txt must not eat the page's timeout.
ROBOTS_TIMEOUT_SECONDS = 5.0

#: How much of an extracted page is kept for the span comparison. The tool
#: already caps the raw body at 200,000 chars; extraction shrinks that, and this
#: is the belt-and-braces bound on what a single grade can carry into memory and
#: into the grading archive.
MAX_PAGE_CHARS = 200_000

#: The robots decisions that permit the fetch. Mirrors ``robots._PERMITS_FETCH``
#: rather than calling :func:`robots_allows`, because this plane needs the two
#: refusals told APART (see :func:`fetch_decisive_page`) and the boolean helper
#: collapses four outcomes into one ``False``.
_PERMITS_FETCH = frozenset({ALLOWED, NO_RULES})


@dataclass(frozen=True)
class FetchedPage:
    """One decisive page, as the gates will see it.

    ``text`` is what G-2 compares and what the grading archive content-addresses
    — the same string for both, so a stored object is exactly the evidence the
    verdict was decided on and a replay of the gate is byte-exact rather than
    approximate.
    """

    url: str
    text: str
    published_at: str | None = None
    #: False when trafilatura extracted nothing and ``text`` is the raw body.
    extracted: bool = True
    status_code: int | None = None
    truncated: bool = False


def page_text_and_date(body: object, url: str) -> tuple[str, str | None, bool]:
    """Readable text, stated publication date, and whether extraction worked.

    Best-effort on all three and never raises: a document that cannot be parsed
    returns ``(raw, None, False)``, which G-2 can still fail honestly against
    and G-3 will demote for want of a date. Raising here would turn a bad page
    into a lost claim.

    ``include_tables=True`` deliberately — a decisive span is often a figure in
    a table (a policy rate, a casualty count), and ``example_news``'s
    ``include_tables=False`` is a NEWS-BODY setting, not an evidence one.
    """
    raw = body if isinstance(body, str) else ""
    if not raw:
        return "", None, False
    text = ""
    published: str | None = None
    try:
        import trafilatura  # noqa: PLC0415 — deliberate lazy import (heavy)

        extracted = trafilatura.extract(
            raw, include_comments=False, include_tables=True, favor_recall=True,
        )
        if extracted and extracted.strip():
            text = extracted.strip()
        meta = trafilatura.extract_metadata(raw, default_url=url)
        if meta is not None:
            raw_date = getattr(meta, "date", None)
            published = str(raw_date) if raw_date else None
    except Exception as exc:  # extraction is best-effort by contract
        logger.debug("external_audit.page_extract_failed url=%s err=%s", url, exc)
    return (text or raw)[:MAX_PAGE_CHARS], published, bool(text)


async def fetch_decisive_page(
    binding: Any,
    url: str,
    *,
    robots_cache: RobotsCache | None = None,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> tuple[FetchedPage | None, str]:
    """Fetch ONE decisive page through the real pack binding. Never raises.

    Returns ``(page, unchecked_reason)``. A non-empty reason means no page, and
    it is one of exactly two closed-vocabulary values so a week of rows
    stratifies (the tool's own words survive in the log line, not in the
    column):

    * :data:`UNCHECKED_ROBOTS_DISALLOWED` — ``robots.txt`` said no. NO fetch was
      attempted. Never worth a retry: it is the publisher's standing answer.
    * :data:`UNCHECKED_SPAN_FETCH_FAILED` — everything else that produced no
      page: a timeout, an SSRF-guard refusal, a gate block, a non-2xx, an empty
      body, and — deliberately — an UNREACHABLE or BAD_URL robots verdict.

    THAT LAST SPLIT IS THE POINT. ``robots_allows`` collapses DISALLOWED,
    UNREACHABLE and BAD_URL into one ``False``, and those are different honesty
    claims. Only DISALLOWED is a publisher refusing us; an unreachable
    robots.txt is OUR conservatism (``robots.py`` fails closed, by its own
    ruling) and a later tick may well reach it. Counting the second as
    ``robots_disallowed`` would inflate the one number an operator would read to
    decide a ToS posture, so this leg calls :func:`robots_decision` and keeps
    ``robots_disallowed`` meaning precisely one thing.

    The robots question is asked about the user-agent ``web_fetch`` ACTUALLY
    SENDS (:data:`WEB_FETCH_USER_AGENT`), not ``robots.py``'s research default —
    a rule is written per agent, and obeying one written for a name we do not
    send is a courtesy to nobody.

    ONE fetch per decisive proposal and no retry beyond the tool's own. The
    queue's budget already reserves it — ``EGRESS_CALLS_PER_CLAIM = 3`` is "the
    primary query, ONE reformulation on NOT_FOUND, and ONE span-verification
    ``web_fetch``" — so this leg spends a call the governor was already sized
    for, and a retry here would silently exceed the clamp that derives
    ``max_claims_per_tick``.
    """
    target = str(url or "").strip()
    if not target:
        return None, UNCHECKED_SPAN_FETCH_FAILED

    # F-7. BEFORE the tool call, always: a disallowed page is not fetched.
    try:
        decision = await robots_decision(
            target,
            cache=robots_cache,
            user_agent=WEB_FETCH_USER_AGENT,
            timeout=ROBOTS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # robots_decision is total; belt and braces
        logger.warning("external_audit.robots_raised url=%s err=%s", target, exc)
        decision = DISALLOWED
    if decision not in _PERMITS_FETCH:
        logger.info(
            "external_audit.span_fetch_robots_refused url=%s decision=%s",
            target, decision,
        )
        return None, (
            UNCHECKED_ROBOTS_DISALLOWED if decision == DISALLOWED
            else UNCHECKED_SPAN_FETCH_FAILED
        )

    try:
        outcome = await asyncio.wait_for(
            binding.run_tool("web_fetch", {"url": target}), timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("external_audit.span_fetch_timeout url=%s", target)
        return None, UNCHECKED_SPAN_FETCH_FAILED
    except Exception as exc:
        logger.warning(
            "external_audit.span_fetch_failed url=%s err=%s", target, exc,
        )
        return None, UNCHECKED_SPAN_FETCH_FAILED

    if not getattr(outcome, "admitted", False):
        cause = getattr(outcome, "block_cause", None) or "blocked"
        logger.warning(
            "external_audit.span_fetch_blocked url=%s cause=%s", target, cause,
        )
        return None, UNCHECKED_SPAN_FETCH_FAILED
    result = getattr(outcome, "tool_result", None)
    if result is None or getattr(result, "status", "") != "completed":
        # ``fetch_outcome`` tells a TIMED-OUT host apart from a refusing one.
        # Both stay ``span_fetch_failed`` in the width vocabulary — that set is
        # a published contract and a timeout is not a new KIND of unchecked —
        # but the log now says which, so an operator reading a tick of
        # span_fetch_failed can see whether the hosts hung or refused.
        why = str(
            (getattr(result, "output", None) or {}).get("fetch_outcome") or ""
        ) if result is not None else ""
        logger.warning(
            "external_audit.span_fetch_incomplete url=%s outcome=%s err=%s",
            target, why or "unknown",
            getattr(result, "error", "") or "no tool result",
        )
        return None, UNCHECKED_SPAN_FETCH_FAILED

    output = dict(getattr(result, "output", None) or {})

    # A NON-2xx IS NOT A PAGE. ``web_fetch_tool`` never calls
    # ``raise_for_status``, so a 404 or a 503 comes back status="completed"
    # carrying the ERROR PAGE's HTML. Folding that into G-2 would let a
    # publisher's "not found" template decide a claim — and, worse, a 404 body
    # that happens to echo the query would resolve a span against a page that
    # does not exist. The status code is checked here because it is the only
    # place that can see it.
    status_code = output.get("status_code")
    if isinstance(status_code, int) and not (200 <= status_code < 300):
        logger.info(
            "external_audit.span_fetch_status url=%s status=%s",
            target, status_code,
        )
        return None, UNCHECKED_SPAN_FETCH_FAILED

    text, published, extracted = page_text_and_date(output.get("body"), target)
    if not text:
        logger.warning("external_audit.span_fetch_empty url=%s", target)
        return None, UNCHECKED_SPAN_FETCH_FAILED

    return FetchedPage(
        url=str(output.get("url") or target),
        text=text,
        published_at=published,
        extracted=extracted,
        status_code=status_code if isinstance(status_code, int) else None,
        truncated=bool(output.get("truncated")),
    ), ""


__all__ = [
    "FETCH_TIMEOUT_SECONDS",
    "MAX_PAGE_CHARS",
    "ROBOTS_TIMEOUT_SECONDS",
    "FetchedPage",
    "fetch_decisive_page",
    "page_text_and_date",
]
