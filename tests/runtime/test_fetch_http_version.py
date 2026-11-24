# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-L ``fetch.http_version`` — per-source protocol posture with a guarded
HTTP/1.1 fallback on a CDN/WAF protocol block (``_http_version.py``)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from legba.data.sources import _egress
from legba.data.sources._http_version import ProtocolPosture
from legba.runtime import source_actor as sa
from legba.runtime.deps import StandardDeps

from tests.runtime import _ingest_fakes as F

@pytest.fixture(autouse=True)
def _private_archive(tmp_path, monkeypatch):
    """Receipts store bytes: keep them in this test's own directory."""
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))


_CHALLENGE = httpx.Response(403, headers={"cf-mitigated": "challenge", "content-type": "text/html"},
                            content=b"<html><title>Just a moment...</title></html>")
_ORIGIN_403 = httpx.Response(403, headers={"content-type": "text/html"}, content=b"<html>Forbidden</html>")


class _Sender:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)


#: Every transport below is a test double: no byte leaves the process.
_HOST = "gov.example.org"


def _req(host: str = _HOST) -> httpx.Request:
    return httpx.Request("GET", f"https://{host}/feed")


@pytest.mark.asyncio
async def test_auto_falls_back_once_on_a_challenge_and_remembers() -> None:
    p = ProtocolPosture("auto")
    h2, h1 = _Sender(_CHALLENGE), _Sender(httpx.Response(200, content=b"<rss/>"))
    r = await p.send(_req(), h2=h2, h1=h1)
    assert r.status_code == 200 and (h2.calls, h1.calls) == (1, 1)
    assert p.fallbacks == 1 and p.used == {"http/1.1": 1} and _HOST in p.memory
    assert p.outcome()["markers"] == ["header:cf-mitigated"] and p.eventful()
    # Later polls go straight to HTTP/1.1 for that host.
    await p.send(_req(), h2=h2, h1=h1)
    assert (h2.calls, h1.calls) == (1, 2)


@pytest.mark.asyncio
async def test_a_body_tell_is_a_block_too_and_an_h2_error_falls_back() -> None:
    body_tell = httpx.Response(503, headers={"content-type": "text/html"},
                               content=b"<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>")
    p = ProtocolPosture("auto")
    await p.send(_req(), h2=_Sender(body_tell), h1=_Sender(httpx.Response(200)))
    p2 = ProtocolPosture("auto")
    await p2.send(_req(), h2=_Sender(httpx.RemoteProtocolError("stream reset")), h1=_Sender(httpx.Response(200)))
    assert p.fallbacks == 1 and p2.fallbacks == 1


@pytest.mark.asyncio
async def test_an_origin_refusal_does_not_fall_back() -> None:
    for resp in (_ORIGIN_403, httpx.Response(401), httpx.Response(404), httpx.Response(429)):
        p = ProtocolPosture("auto")
        h1 = _Sender(httpx.Response(200))
        r = await p.send(_req(), h2=_Sender(resp), h1=h1)
        assert r.status_code == resp.status_code and h1.calls == 0 and p.fallbacks == 0
        assert not p.eventful()


@pytest.mark.asyncio
async def test_explicit_modes() -> None:
    p = ProtocolPosture("http1.1")
    h2, h1 = _Sender(httpx.Response(200)), _Sender(httpx.Response(200))
    await p.send(_req(), h2=h2, h1=h1)
    assert (h2.calls, h1.calls) == (0, 1)
    p = ProtocolPosture("h2")
    h1 = _Sender(httpx.Response(200))
    r = await p.send(_req(), h2=_Sender(_CHALLENGE), h1=h1)
    assert r.status_code == 403 and h1.calls == 0 and p.fallbacks == 0


class _PostTransport(httpx.AsyncBaseTransport):
    """The guarded transport's post-guard dispatch (``_egress.dispatch_request``)
    over two test-double legs — the real posture code, no socket."""

    def __init__(self, h2: _Sender, h1: _Sender) -> None:
        self.h2, self.h1 = h2, h1
        self._h1 = type("H1", (), {"handle_async_request": staticmethod(h1)})()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await _egress.dispatch_request(request, primary=self.h2, h1=lambda: self._h1, h2_offered=True)


@pytest.mark.asyncio
async def test_the_dispatch_never_offers_h2_under_http11() -> None:
    from legba.data.sources._http_version import posture_scope

    h2, h1 = _Sender(httpx.Response(200)), _Sender(httpx.Response(200, content=b"ok"))
    t = _PostTransport(h2, h1)
    with posture_scope(ProtocolPosture("http1.1")):
        await t.handle_async_request(_req())
    assert (h2.calls, h1.calls) == (0, 1)
    # Outside a poll there is no posture: exactly the old path.
    await t.handle_async_request(_req())
    assert (h2.calls, h1.calls) == (1, 1)


def test_the_field_is_validated_at_save_and_hash_neutral() -> None:
    desc, _ = F.rss_case(datetime.now(tz=timezone.utc))
    body = desc.model_dump(mode="json")
    assert "fetch" not in body
    with pytest.raises(ValidationError):
        type(desc).model_validate({**body, "fetch": {"http_version": "h3"}}, strict=False)
    d = type(desc).model_validate({**body, "fetch": {"http_version": "http1.1"}}, strict=False)
    assert d.fetch.http_version == "http1.1" and d.model_dump(mode="json")["fetch"] == {"http_version": "http1.1"}


@pytest.mark.asyncio
async def test_a_poll_records_the_fallback_and_carries_the_memory() -> None:
    """End to end through the actor: an RSS source behind a challenge-on-h2 CDN."""
    now = datetime.now(tz=timezone.utc)
    xml = F._rss_xml(now)
    h2 = _Sender(httpx.Response(403, headers={"cf-mitigated": "challenge"}, content=b"blocked"))
    h1 = _Sender(httpx.Response(200, content=xml, headers={"content-type": "application/rss+xml"}))

    from legba.data.sources.rss import RSSConfig, RSSSourceHandler

    desc, _ = F.rss_case(now)
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    for _poll in range(2):
        handler = RSSSourceHandler(RSSConfig(url=f"https://{_HOST}/feed", first_poll_max_entry_age_days=0),  # 2025-dated
                                   http_client=httpx.AsyncClient(transport=_PostTransport(h2, h1)))
        core = sa.SourceCore("source::x::" + "b" * 16, sa.SourceDeps(
            descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats), handler=handler))
        await core.pull_once()
    assert (h2.calls, h1.calls) == (1, 2)
    outcomes = [c for c in pool.calls if "source_poll_outcomes" in c[1]]
    first = json.loads(outcomes[0][2][-1])
    assert first["fallbacks"] == 1 and first["used"] == {"http/1.1": 1}
    second = json.loads(outcomes[1][2][-1])
    assert second["fallbacks"] == 0 and second["used"] == {"http/1.1": 1}   # remembered: straight to 1.1
    cursors = [json.loads(c[2][3]) for c in pool.calls
               if c[1].startswith("INSERT INTO public.actor_filter_state") and c[2][2] == "cursor"]
    assert _HOST in cursors[-1]["fetch_protocol_memory"]
    assert len(nats.messages) == 3
