# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``robots.txt`` for the AGENTIC fetch path — fetch, cache, obey, COUNT.

Legba's crawler has honoured robots.txt since it was written
(``data/sources/scraper.py:_robots_allow``). Its agentic web tools never have:
``web_fetch`` opens an SSRF-guarded client and GETs whatever URL it is handed,
and the 2026-09-05 width review named that gap (F-7) with the operator's
default answer already recorded — **honour it**. This module is that answer,
built ONCE so the second and third caller cannot each re-decide it.

R-A owns the module and uses it in ``web_evidence`` before every page fetch. A
later lane wires the same helper into ``web_fetch``; nothing here is specific
to research, and the module is importable and testable standalone with no
pack, no pool and no provider.

THE POSTURE, and where it deliberately differs from the crawler's
-----------------------------------------------------------------
RFC 9309 §2.3.1 distinguishes three fetch outcomes for ``/robots.txt``, and
they do NOT all mean the same thing:

* **2xx** — parse it and obey it (§2.3.1.1). A ``Disallow`` that matches is a
  refusal, full stop.
* **4xx** ("unavailable", §2.3.1.3) — there are no rules; the crawler MAY
  access anything. A 404 is the common shape for a host that simply has no
  robots.txt, and reading it as a refusal would make most of the open web
  unfetchable for no stated reason.
* **5xx / a network failure** ("unreachable", §2.3.1.4) — the RFC says a
  crawler SHOULD assume COMPLETE DISALLOW. ``scraper.py`` fails OPEN here
  instead, and that was defensible for a BFS crawl over a small operator-chosen
  seed set. It is not defensible for an agentic fetcher pointed at an unbounded
  domain set chosen by a search engine's relevance model: the host we cannot
  ask is exactly the host we should not assume has said yes. **So this module
  fails CLOSED on unreachable**, and the divergence is recorded here rather
  than discovered later by whoever diffs the two.

Everything is COUNTED (:class:`RobotsCounters`), because a refusal that leaves
no trace reads downstream as "the web had nothing" — the same false-absence
failure the search plane's degradation reads exist to prevent. A caller folds
the counters into its tool output so a run can say *how many* candidate pages
it did not read, and why.

The robots.txt fetch itself egresses through the SAME
:func:`~legba.data.sources._egress.guarded_async_client` as everything else, so
a hostile redirect on the robots URL is refused before connect exactly like any
other fetch.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from ...sources._egress import EgressBlockedError, guarded_async_client

logger = logging.getLogger(__name__)

#: The UA robots rules are evaluated against, and the UA we send. It names the
#: project and carries a contact URL so an operator whose site we read can find
#: out who we are — the minimum courtesy that makes honouring robots mean
#: anything.
ROBOTS_USER_AGENT = "legba-research/1.0 (+https://github.com/ldgeorge85/legba)"

#: Per-host verdict TTL. Long enough that a multi-hit research round costs ONE
#: robots fetch per host; short enough that an operator who publishes a new
#: rule is obeyed within the hour.
ROBOTS_CACHE_TTL_SECONDS = 3600.0

#: robots.txt fetch timeout. Deliberately shorter than a page fetch: a host too
#: slow to serve a ~1KB text file is a host we fail closed on, not one we wait
#: on while a GATHER round's clock runs.
ROBOTS_TIMEOUT_SECONDS = 5.0

#: Cap on the robots.txt body we parse. The largest real-world robots.txt files
#: run to a few hundred KB; anything past this is not a rules file.
ROBOTS_MAX_BYTES = 512_000


#: The verdict vocabulary. Plain strings: they ride into a tool output and a
#: log line unchanged, and a caller compares them by value.
ALLOWED = "allowed"
DISALLOWED = "disallowed"
NO_RULES = "no_rules"            # 4xx — there are no rules to obey
UNREACHABLE = "unreachable"      # 5xx / network failure — fail CLOSED
BAD_URL = "bad_url"              # not http(s), or no host

#: The decisions that permit a fetch. ``UNREACHABLE`` is NOT among them.
_PERMITS_FETCH = frozenset({ALLOWED, NO_RULES})


@dataclass
class RobotsCounters:
    """What the run should be able to say about robots afterwards."""

    checked: int = 0
    allowed: int = 0
    disallowed: int = 0
    no_rules: int = 0
    unreachable: int = 0
    bad_url: int = 0
    fetches: int = 0          # actual robots.txt HTTP requests issued
    cache_hits: int = 0

    def record(self, decision: str) -> None:
        self.checked += 1
        attr = {
            ALLOWED: "allowed",
            DISALLOWED: "disallowed",
            NO_RULES: "no_rules",
            UNREACHABLE: "unreachable",
            BAD_URL: "bad_url",
        }.get(decision)
        if attr:
            setattr(self, attr, getattr(self, attr) + 1)

    def to_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "allowed": self.allowed,
            "disallowed": self.disallowed,
            "no_rules": self.no_rules,
            "unreachable": self.unreachable,
            "bad_url": self.bad_url,
            "fetches": self.fetches,
            "cache_hits": self.cache_hits,
        }


@dataclass
class _Entry:
    parser: RobotFileParser | None
    decision_when_no_parser: str
    expires_at: float


@dataclass
class RobotsCache:
    """Per-origin robots.txt cache with a TTL. One instance per process is fine
    (it holds only parsed rules), and a test injects its own.

    Keyed by ORIGIN (scheme + host + port), not by bare host: ``https://x`` and
    ``http://x`` are different origins to the RFC and can serve different rules.
    """

    ttl_seconds: float = ROBOTS_CACHE_TTL_SECONDS
    counters: RobotsCounters = field(default_factory=RobotsCounters)
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _clock: object = None

    def _now(self) -> float:
        clock = self._clock
        return float(clock()) if callable(clock) else time.monotonic()

    def get(self, origin: str) -> _Entry | None:
        entry = self._entries.get(origin)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            self._entries.pop(origin, None)
            return None
        return entry

    def put(self, origin: str, entry: _Entry) -> None:
        self._entries[origin] = entry

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        return n


def _origin_of(url: str) -> tuple[str, str] | None:
    """``url`` → ``(origin, robots_url)``, or ``None`` when it is not fetchable."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    return origin, robots_url


async def _load_robots(
    robots_url: str, *, timeout: float, user_agent: str,
) -> tuple[RobotFileParser | None, str]:
    """Fetch + parse ``robots_url``. Returns ``(parser, fallback_decision)``.

    ``parser`` is ``None`` when there is nothing to obey, and the second
    element then says WHICH nothing: :data:`NO_RULES` (a 4xx — permitted) or
    :data:`UNREACHABLE` (a 5xx / network failure / egress refusal — refused).
    """
    try:
        async with guarded_async_client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        ) as client:
            response = await client.get(robots_url)
    except EgressBlockedError as exc:
        # The robots URL itself resolved somewhere non-public. Refuse the host
        # outright — the page fetch would be refused by the same guard anyway,
        # and saying so here keeps the reason legible.
        logger.warning("robots.egress_blocked url=%s err=%s", robots_url, exc)
        return None, UNREACHABLE
    except httpx.HTTPError as exc:
        logger.info("robots.unreachable url=%s err=%s", robots_url, exc)
        return None, UNREACHABLE

    status = response.status_code
    if 400 <= status < 500:
        # RFC 9309 §2.3.1.3 — "unavailable": no rules exist, access permitted.
        return None, NO_RULES
    if status >= 500 or status < 200:
        # RFC 9309 §2.3.1.4 — "unreachable": assume complete disallow.
        return None, UNREACHABLE

    body = response.text
    if len(body.encode("utf-8", "ignore")) > ROBOTS_MAX_BYTES:
        body = body[: ROBOTS_MAX_BYTES // 2]
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    return parser, ALLOWED


async def robots_decision(
    url: str,
    *,
    cache: RobotsCache | None = None,
    user_agent: str = ROBOTS_USER_AGENT,
    timeout: float = ROBOTS_TIMEOUT_SECONDS,
) -> str:
    """The robots verdict for ONE url. Never raises.

    One robots.txt fetch per origin per TTL; every subsequent URL on that
    origin is answered from the cache. The verdict is one of :data:`ALLOWED` /
    :data:`DISALLOWED` / :data:`NO_RULES` / :data:`UNREACHABLE` / :data:`BAD_URL`.
    """
    cache = cache if cache is not None else RobotsCache()
    resolved = _origin_of(url)
    if resolved is None:
        cache.counters.record(BAD_URL)
        return BAD_URL
    origin, robots_url = resolved

    entry = cache.get(origin)
    if entry is not None:
        cache.counters.cache_hits += 1
    else:
        parser, fallback = await _load_robots(
            robots_url, timeout=timeout, user_agent=user_agent,
        )
        cache.counters.fetches += 1
        entry = _Entry(
            parser=parser,
            decision_when_no_parser=fallback,
            expires_at=cache._now() + cache.ttl_seconds,
        )
        cache.put(origin, entry)

    if entry.parser is None:
        cache.counters.record(entry.decision_when_no_parser)
        return entry.decision_when_no_parser
    try:
        permitted = entry.parser.can_fetch(user_agent, url)
    except Exception:  # pragma: no cover — a malformed rules file must not raise
        logger.debug("robots.can_fetch_failed url=%s", url, exc_info=True)
        permitted = False
    decision = ALLOWED if permitted else DISALLOWED
    cache.counters.record(decision)
    return decision


async def robots_allows(
    url: str,
    *,
    cache: RobotsCache | None = None,
    user_agent: str = ROBOTS_USER_AGENT,
    timeout: float = ROBOTS_TIMEOUT_SECONDS,
) -> bool:
    """``True`` when this URL may be fetched. The one-line caller surface.

    Fails CLOSED: an unreachable robots.txt, a malformed rules file and a
    non-http(s) URL all return ``False``. See the module docstring for why this
    diverges from ``scraper.py``'s fail-open.
    """
    decision = await robots_decision(
        url, cache=cache, user_agent=user_agent, timeout=timeout,
    )
    return decision in _PERMITS_FETCH


__all__ = [
    "ALLOWED",
    "BAD_URL",
    "DISALLOWED",
    "NO_RULES",
    "ROBOTS_CACHE_TTL_SECONDS",
    "ROBOTS_MAX_BYTES",
    "ROBOTS_TIMEOUT_SECONDS",
    "ROBOTS_USER_AGENT",
    "UNREACHABLE",
    "RobotsCache",
    "RobotsCounters",
    "robots_allows",
    "robots_decision",
]
