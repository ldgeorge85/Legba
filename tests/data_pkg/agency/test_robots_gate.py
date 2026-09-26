# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``agency/robots.py`` — the agentic fetch path's robots.txt gate.

No DB, no pack, no provider: the module is deliberately standalone so the
second caller (``web_fetch``, a later lane) inherits a tested helper rather
than re-deciding the posture.

The behaviour that matters, and each case is a test:

  * a matching ``Disallow`` REFUSES;
  * a 404 (no rules published) PERMITS — RFC 9309 §2.3.1.3, and reading it as a
    refusal would make most of the open web unfetchable for no stated reason;
  * a 5xx or a network failure REFUSES — RFC 9309 §2.3.1.4, and this is where
    the module deliberately diverges from ``scraper.py``'s fail-open;
  * one robots.txt fetch per ORIGIN per TTL, however many URLs are checked;
  * every outcome is COUNTED, because a refusal that leaves no trace reads
    downstream as "the web had nothing".
"""

from __future__ import annotations

import httpx
import pytest

from legba.data.analysts.agency import robots

pytestmark = [pytest.mark.asyncio]


_RULES = """
User-agent: *
Disallow: /private/
Disallow: /paywall
"""

#: A UA-SPECIFIC group. RFC 9309 §2.2.1: the most specific matching group wins
#: outright, so a rules file that names us replaces the ``*`` group entirely —
#: which is exactly why the two cases get two fixtures.
_UA_RULES = """
User-agent: *
Disallow: /

User-agent: legba-research
Disallow: /nope
"""


def _transport(handler):
    """A stub transport in place of the SSRF-guarded one.

    The guard itself is proven by the agency e2e suite (a URL resolving to a
    private address is refused before connect); here we need deterministic
    robots.txt RESPONSES, which a real transport cannot give.
    """
    return httpx.MockTransport(handler)


@pytest.fixture
def served(monkeypatch):
    """Serve a scripted robots.txt and count the requests that reached it."""
    calls: list[str] = []

    def _install(responder):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return responder(request)

        def fake_client(**kwargs):
            kwargs["transport"] = _transport(handler)
            return httpx.AsyncClient(**kwargs)

        monkeypatch.setattr(robots, "guarded_async_client", fake_client)
        return calls

    return _install


async def test_disallowed_path_is_refused(served):
    served(lambda r: httpx.Response(200, text=_RULES))
    cache = robots.RobotsCache()
    assert await robots.robots_allows("https://x.test/private/doc", cache=cache) is False
    assert await robots.robots_allows("https://x.test/news/doc", cache=cache) is True
    assert cache.counters.disallowed == 1
    assert cache.counters.allowed == 1


async def test_404_means_no_rules_and_permits(served):
    served(lambda r: httpx.Response(404, text="nope"))
    cache = robots.RobotsCache()
    assert await robots.robots_decision("https://x.test/a", cache=cache) == (
        robots.NO_RULES
    )
    assert await robots.robots_allows("https://x.test/a", cache=cache) is True
    assert cache.counters.no_rules == 2


async def test_5xx_fails_CLOSED(served):
    """RFC 9309 §2.3.1.4 — 'unreachable' means assume complete disallow. The
    host we cannot ask is exactly the host we must not assume said yes."""
    served(lambda r: httpx.Response(503, text=""))
    cache = robots.RobotsCache()
    assert await robots.robots_decision("https://x.test/a", cache=cache) == (
        robots.UNREACHABLE
    )
    assert await robots.robots_allows("https://x.test/a", cache=cache) is False
    assert cache.counters.unreachable == 2


async def test_network_failure_fails_CLOSED(served):
    def boom(request):
        raise httpx.ConnectTimeout("no route", request=request)

    served(boom)
    cache = robots.RobotsCache()
    assert await robots.robots_allows("https://x.test/a", cache=cache) is False
    assert cache.counters.unreachable == 1


async def test_egress_block_on_the_robots_url_fails_closed(monkeypatch):
    from legba.data.sources._egress import EgressBlockedError

    def blocked(**kwargs):
        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                raise EgressBlockedError("egress blocked: 169.254.169.254")

        return _C()

    monkeypatch.setattr(robots, "guarded_async_client", blocked)
    cache = robots.RobotsCache()
    assert await robots.robots_allows("https://x.test/a", cache=cache) is False
    assert cache.counters.unreachable == 1


async def test_non_http_url_is_refused_without_a_fetch(served):
    calls = served(lambda r: httpx.Response(200, text=_RULES))
    cache = robots.RobotsCache()
    assert await robots.robots_decision("file:///etc/passwd", cache=cache) == (
        robots.BAD_URL
    )
    assert await robots.robots_allows("file:///etc/passwd", cache=cache) is False
    assert calls == []
    assert cache.counters.bad_url == 2


async def test_one_fetch_per_origin_then_cached(served):
    calls = served(lambda r: httpx.Response(200, text=_RULES))
    cache = robots.RobotsCache()
    for path in ("/a", "/b", "/private/c", "/d"):
        await robots.robots_allows(f"https://x.test{path}", cache=cache)
    assert len(calls) == 1, "one robots.txt fetch per origin per TTL"
    assert cache.counters.fetches == 1
    assert cache.counters.cache_hits == 3
    # A DIFFERENT origin is a different rules file — scheme included.
    await robots.robots_allows("https://y.test/a", cache=cache)
    await robots.robots_allows("http://x.test/a", cache=cache)
    assert len(calls) == 3


async def test_ttl_expiry_refetches(served):
    calls = served(lambda r: httpx.Response(200, text=_RULES))
    clock = {"t": 1000.0}
    cache = robots.RobotsCache(ttl_seconds=60.0)
    cache._clock = lambda: clock["t"]
    await robots.robots_allows("https://x.test/a", cache=cache)
    clock["t"] += 30
    await robots.robots_allows("https://x.test/a", cache=cache)
    assert len(calls) == 1
    clock["t"] += 61
    await robots.robots_allows("https://x.test/a", cache=cache)
    assert len(calls) == 2


async def test_our_user_agent_is_sent_and_matched(served):
    """A UA-specific rule for us must bind — the whole point of sending a UA
    that names the project."""
    served(lambda r: httpx.Response(200, text=_UA_RULES))
    cache = robots.RobotsCache()
    # Our UA's own group binds: /nope is refused, /news is not.
    assert await robots.robots_allows(
        "https://x.test/nope", cache=cache, user_agent="legba-research",
    ) is False
    assert await robots.robots_allows(
        "https://x.test/news", cache=cache, user_agent="legba-research",
    ) is True
    # An agent the file does not name falls to the `*` group, which here
    # disallows everything.
    assert await robots.robots_allows(
        "https://x.test/news", cache=robots.RobotsCache(),
        user_agent="some-other-agent",
    ) is False
    # The shipped UA resolves to that same named group (RobotFileParser keys on
    # the product token before the slash), so honouring a site's legba rule
    # does not depend on the version string.
    assert robots.ROBOTS_USER_AGENT.split("/")[0] == "legba-research"


async def test_counters_serialise_for_a_tool_output(served):
    served(lambda r: httpx.Response(200, text=_RULES))
    cache = robots.RobotsCache()
    await robots.robots_allows("https://x.test/a", cache=cache)
    await robots.robots_allows("https://x.test/private/b", cache=cache)
    d = cache.counters.to_dict()
    assert d["checked"] == 2 and d["allowed"] == 1 and d["disallowed"] == 1
    assert set(d) == {
        "checked", "allowed", "disallowed", "no_rules", "unreachable",
        "bad_url", "fetches", "cache_hits",
    }


async def test_unreachable_is_not_in_the_permitting_set():
    """The divergence from ``scraper.py``, pinned so nobody 'fixes' it back."""
    assert robots.ALLOWED in robots._PERMITS_FETCH
    assert robots.NO_RULES in robots._PERMITS_FETCH
    assert robots.UNREACHABLE not in robots._PERMITS_FETCH
    assert robots.DISALLOWED not in robots._PERMITS_FETCH
    assert robots.BAD_URL not in robots._PERMITS_FETCH
