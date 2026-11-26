# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""seven feeds lost items to the pull cursor; replayed here.

Each test replays one feed shape's failure against a fixture that keeps the
feed's structure, dates and ids but carries invented titles, summaries and
hosts (``tests/fixtures/pull_cursor/``), through the real handler and the real ``SourceCore.pull_once``:

  * the loss is reproduced on the historical mechanism (the strict ``since``
    filter at the cursor the old policy persisted, or the legacy policy run
    end to end), and
  * the new policy (``runtime/source_cursor.py``: never-future clamp, 72 h
    look-back with the judged-entry ledger, undated entries never advancing a
    dated feed's cursor; plus the per-feed fixes: ISW ``date_gmt``, Kremlin
    ``cursor_timestamp: latest``, localized Drupal month names) ingests the
    items and does not re-process what an earlier poll already judged.

Time is frozen per poll through ``source_cursor.utcnow`` (the clock the cursor
policy reads) and ``SourceContext.now_fn`` (json_api's window end). DB-free:
the substrate is the same in-memory double the cursor tests use; every
written signal is captured from the publish path.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import feedparser
import httpx
import pytest
import yaml

import legba.runtime.source_cursor as sc
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext
from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler
from legba.data.sources.rss import RSSConfig, RSSSourceHandler
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps, _RawConfig
from legba.runtime.source_factory import _unwrap_factory_dict

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "pull_cursor"
UTC = timezone.utc


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeConn:
    async def fetchrow(self, query, *args):
        return {"id": args[0]}

    async def fetchval(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):  # pragma: no cover - unused
        return None


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Feed:
    """A source under test: one SourceCore + real handler + mock transport."""

    def __init__(
        self,
        monkeypatch,
        *,
        kind: str,
        config: dict[str, Any],
        transport: Callable[[httpx.Request], httpx.Response],
        cursor: dict[str, Any] | None = None,
        clock: datetime,
    ) -> None:
        self.clock = _Clock(clock)
        monkeypatch.setattr(sc, "utcnow", self.clock)
        self.published: list[dict[str, Any]] = []
        self.store = InMemoryStateStore({"cursor": cursor} if cursor else None)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(transport), follow_redirects=True, timeout=5,
        )
        if kind == "rss":
            handler: Any = RSSSourceHandler(RSSConfig(**config), http_client=client)
        else:
            handler = JsonApiSourceHandler(JsonApiConfig(**config), http_client=client)
        source_id = f"source.test.f151_{uuid4().hex[:8]}"
        sd = SourceDescriptor(
            identity=SourceIdentity(
                id=source_id, name="f151", kind=kind, schema_uri="legba/source/3.0.0",
                version="f" * 16, owner="test:f151", created=datetime.now(tz=UTC),
                state=LifecycleState.ACTIVE,
            ),
            scope=SourceScope(owner_tenant="acme", languages=["en"]),
            acquisition="poll",
            config=dict(config),
            cadence=CadenceBlock(schedule=Cron(raw="*/5 * * * *")),
            pipeline=SourcePipeline(media="reference"),
        )

        async def _capture(subject, data):
            self.published.append(json.loads(data)["payload"])

        deps = StandardDeps(pg_pool=_FakePool(), nats_publish=_capture)
        self.core = SourceCore(f"source::{source_id}::t", SourceDeps(descriptor=sd, deps=deps))
        self.core.sd.handler = handler
        self.handler = handler
        core = self.core

        def _make_context():
            return SourceContext(
                target_id=source_id, target_version=core.descriptor.identity.version,
                source_id=source_id, config=_RawConfig(**config),
                state_store=self.store, now_fn=self.clock,
                scope_geo=[], scope_languages=["en"],
            )

        core._make_context = _make_context  # type: ignore[method-assign]

    async def poll(self, at: datetime) -> list[str]:
        """One poll at ``at``; returns the titles WRITTEN by it."""
        self.clock.now = at
        before = len(self.published)
        await self.core.pull_once()
        return [p.get("title") or "" for p in self.published[before:]]

    async def cursor(self) -> datetime:
        return _t((await self.store.get("cursor"))["last_pulled_at"])


def _serve(doc: Callable[[], str]):
    def transport(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=doc(), request=req)
    return transport


def _blocks(raw: str) -> tuple[str, list[str], str]:
    found = list(re.finditer(r"<(item|entry)\b[^>]*>.*?</\1>", raw, re.S))
    return raw[: found[0].start()], [m.group(0) for m in found], raw[found[-1].end():]


def _doc(raw: str, keep: Callable[[int, Any], bool], edit=None) -> str:
    """The fixture as the feed served it at an earlier moment: only the
    entries ``keep(i, parsed_entry)`` selects, optionally edited."""
    head, blocks, tail = _blocks(raw)
    entries = feedparser.parse(raw).entries
    assert len(entries) == len(blocks)
    kept = []
    for i, (block, entry) in enumerate(zip(blocks, entries)):
        if keep(i, entry):
            kept.append(edit(i, block) if edit else block)
    return head + "\n".join(kept) + tail


def _entry_dt(entry: Any) -> datetime:
    from legba.data.sources.rss import _extract_published

    return _extract_published(entry)


async def _handler_titles(handler, since: datetime | None, clock: datetime) -> list[str]:
    ctx = SourceContext(
        target_id="t", target_version="v", source_id="t", config=_RawConfig(),
        state_store=InMemoryStateStore(), now_fn=lambda: clock,
    )
    return [s.payload.get("title") or "" async for s in handler.pull(ctx, since)]


def _rss_handler(raw: str, **config) -> RSSSourceHandler:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_serve(lambda: raw)), follow_redirects=True,
    )
    return RSSSourceHandler(RSSConfig(url="https://feed.invalid/x", **config), http_client=client)


def _descriptor_config(name: str) -> dict[str, Any]:
    body = yaml.safe_load((REPO / "descriptors" / name).read_text(encoding="utf-8"))
    return _unwrap_factory_dict(body["config"])


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. ISW — WordPress `date` is New York wall time, read as UTC
# ---------------------------------------------------------------------------

_ISW_POSTS = json.loads(_read("isw_wp_posts.json"))


def _wp_server(clock: _Clock):
    """WordPress REST semantics, verified live 11-05: ``after`` with a UTC
    offset compares against the post's GMT instant; newest first; only posts
    already published at the request time."""

    def transport(req: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(req.url)).query)
        after = _t(qs["after"][0]) if "after" in qs else None
        out = []
        for p in _ISW_POSTS:
            gmt = datetime.fromisoformat(p["date_gmt"]).replace(tzinfo=UTC)
            if gmt <= clock.now and (after is None or gmt > after):
                out.append(p)
        return httpx.Response(200, text=json.dumps(out[:20]), request=req)

    return transport


def _isw_polls():
    """The descriptor's cron ``30 2-23/6 * * *`` from 10-29 08:30Z to 11-05 08:30Z."""
    t = _t("2026-10-29T08:30:00+00:00")
    while t <= _t("2026-11-05T08:30:00+00:00"):
        yield t
        t += timedelta(hours=6)


async def _isw_week(monkeypatch, config: dict[str, Any]) -> set[str]:
    start = _t("2026-10-29T02:30:00+00:00")
    feed = _Feed(
        monkeypatch, kind="json_api", config=config, transport=lambda r: None,
        cursor={"last_pulled_at": start.isoformat()}, clock=start,
    )
    feed.handler._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_wp_server(feed.clock)), follow_redirects=True,
    )
    await feed.store.set("json_api_cursor", {"last_pulled_at": start.isoformat()})
    titles: set[str] = set()
    for at in _isw_polls():
        titles.update(await feed.poll(at))
    return titles


@pytest.mark.asyncio
async def test_isw_week_replay_loses_posts_on_date_and_keeps_all_on_date_gmt(monkeypatch):
    """The measured loss ("3 of 20 ingested in 7 d"), then the fix.

    Old: ``timestamp_path: date`` (site-local ET read as UTC = every post 5 h
    early) with the strict window — a post survives only when published in
    the last hour of a 6 h window. New: the repo descriptor (``date_gmt``)."""
    expected = {
        p["title"]["rendered"].strip() for p in _ISW_POSTS
        if _t(p["date_gmt"] + "+00:00") > _t("2026-10-29T02:30:00+00:00")
    }
    assert len(expected) == 19

    new_cfg = _descriptor_config("source_isw.yaml")
    assert new_cfg["timestamp_path"] == "date_gmt"
    assert "date_gmt" in new_cfg["url_template"]

    old_cfg = dict(new_cfg, timestamp_path="date", cursor_lookback_hours=0)
    old = await _isw_week(monkeypatch, old_cfg)
    assert len(old) <= 5, sorted(old)            # the loss, reproduced
    assert expected - old                        # e.g. the Nov 4 update

    new = await _isw_week(monkeypatch, new_cfg)
    # Every post; the look-back also reconsiders the one post from 10-29
    # 01:33Z, before the replay's starting cursor (S-4 absorbs it live).
    assert new == expected | {"Example Campaign Assessment, October 28, 2026"}


@pytest.mark.asyncio
async def test_isw_single_window_reproduces_the_drop_and_stamps_utc(monkeypatch):
    """One poll window, the 11-04 20:30Z one: the regional update
    (published 18:33:33Z, ``date`` 13:33:33) is the only post in it."""
    clock = _Clock(_t("2026-11-04T20:30:00+00:00"))
    prior = {"last_pulled_at": "2026-11-04T14:30:00+00:00"}

    async def pull(cfg: dict[str, Any]) -> list[Signal]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(_wp_server(clock)))
        h = JsonApiSourceHandler(JsonApiConfig(**cfg), http_client=client)
        ctx = SourceContext(
            target_id="t", target_version="v", source_id="t", config=_RawConfig(),
            state_store=InMemoryStateStore({"json_api_cursor": dict(prior)}), now_fn=clock,
        )
        return [s async for s in h.pull(ctx, None)]

    new_cfg = _descriptor_config("source_isw.yaml")
    assert await pull(dict(new_cfg, timestamp_path="date")) == []      # dropped
    got = await pull(new_cfg)
    assert [s.payload["external_id"] for s in got] == ["161833"]
    assert got[0].payload["published_at"] == "2026-11-04T18:33:33+00:00"


# ---------------------------------------------------------------------------
# 2. State Dept — date-only pubDates, re-issued advisories keep their guid
# ---------------------------------------------------------------------------

_STATE = _read("stategov_travel_advisories.xml")
_ME = {
    "Iran", "Syria", "Yemen", "Bahrain", "Jordan", "Lebanon", "Oman", "Saudi Arabia",
    "Kuwait", "Iraq", "United Arab Emirates", "Qatar", "Israel",
}


def _is_me(entry_or_title) -> bool:
    title = entry_or_title if isinstance(entry_or_title, str) else entry_or_title.title
    return title.split(" - ")[0] in _ME


@pytest.mark.asyncio
async def test_stategov_reissued_middle_east_advisories(monkeypatch):
    """The 13 Middle-East advisories re-issued 10-23 carry ``Fri, 23 Oct 2026``
    (= 00:00Z) and surfaced in the feed during the US day — after the cursor
    had passed 00:00Z (the 05:41Z poll moved it there). Every one was dropped.
    """
    # The historical mechanism: the cursor the 10-23 05:41Z poll persisted,
    # then the strict since-filter on the document that carries them.
    old = await _handler_titles(_rss_handler(_STATE), _t("2026-10-23T05:41:01+00:00"), _t("2026-10-23T17:41:00+00:00"))
    assert not [t for t in old if _is_me(t)]
    # Same-day date-only loss even without poisoning: one 10-23 advisory
    # ingested moves the cursor to 10-23 00:00 and its siblings are `<=` it.
    old = await _handler_titles(_rss_handler(_STATE), _t("2026-10-23T00:00:00+00:00"), _t("2026-10-23T17:41:00+00:00"))
    assert not [t for t in old if _is_me(t)]

    feed_doc = {"doc": _doc(_STATE, lambda i, e: not _is_me(e))}
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://travel.state.gov/_res/rss/TAsTWs.xml"},
        transport=_serve(lambda: feed_doc["doc"]),
        cursor={"last_pulled_at": "2026-10-22T17:41:01+00:00"},
        clock=_t("2026-10-23T05:41:01+00:00"),
    )
    first = await feed.poll(_t("2026-10-23T05:41:01+00:00"))
    assert not [t for t in first if _is_me(t)]
    assert any(t.startswith("France") for t in first)   # inside the look-back

    feed_doc["doc"] = _STATE                             # the advisories surface
    second = await feed.poll(_t("2026-10-23T17:41:00+00:00"))
    assert sorted(t.split(" - ")[0] for t in second) == sorted(_ME)  # all 13, nothing else
    third = await feed.poll(_t("2026-10-23T23:41:00+00:00"))
    assert third == []                                   # judged once
    assert await feed.cursor() == _t("2026-10-23T00:00:00+00:00")


# ---------------------------------------------------------------------------
# 3. ICG — French month names; an undated entry moved the cursor to NOW
# ---------------------------------------------------------------------------

_ICG = _read("crisisgroup_latest.xml")
_ICG_FR = "Exampleland\u00a0: contenir les effets d'une crise voisine"


def _undated_fr(i, block: str) -> str:
    # The entry the 11-02 20:21Z stale document carried with a date the
    # parser could not read (stands in for the undated ar/fr entries).
    return re.sub(r"<pubDate>.*?</pubDate>", "", block)


@pytest.mark.asyncio
async def test_icg_localized_month_is_dated():
    from legba.data.sources.rss import _extract_published

    entry = next(e for e in feedparser.parse(_ICG).entries if e.title == _ICG_FR)
    assert entry.published == "Jeudi, octobre 29, 2026 - 11:01"
    assert _extract_published(entry) == _t("2026-10-29T11:01:00+00:00")


@pytest.mark.asyncio
async def test_icg_undated_entry_no_longer_advances_the_cursor(monkeypatch):
    """11-02 20:21Z: the CDN served a stale document whose only new-looking
    entry was undated; the old policy advanced the cursor to the poll time.
    11-03 08:21Z: the fresh document's nine English items (10-27 .. 10-30) were
    all `<=` that cursor and dropped; only the undated item landed."""
    old = await _handler_titles(_rss_handler(_ICG), _t("2026-11-02T20:21:03+00:00"), _t("2026-11-03T08:21:27+00:00"))
    assert old == []                     # every dated item lost at that cursor

    docs = {"doc": _doc(_ICG, lambda i, e: e.title == _ICG_FR, edit=_undated_fr)}
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://www.crisisgroup.org/rss"},
        transport=_serve(lambda: docs["doc"]),
        cursor={"last_pulled_at": "2026-10-23T18:13:00+00:00"},
        clock=_t("2026-11-02T20:21:03+00:00"),
    )
    stale = await feed.poll(_t("2026-11-02T20:21:03+00:00"))
    assert stale == [_ICG_FR]                                       # still ingested
    assert await feed.cursor() == _t("2026-10-23T18:13:00+00:00")   # but moved nothing

    docs["doc"] = _ICG
    fresh = await feed.poll(_t("2026-11-03T08:21:27+00:00"))
    english = [e.title for e in feedparser.parse(_ICG).entries if e.title != _ICG_FR]
    assert len(english) == 9
    assert set(english) <= set(fresh)
    assert await feed.cursor() == _t("2026-10-30T17:15:00+00:00")


# ---------------------------------------------------------------------------
# 4. Kremlin — `published` is the event time; items are posted later
# ---------------------------------------------------------------------------

_KREMLIN = _read("kremlin_events.atom")
_LATE = {
    # title prefix -> (event time, posted/updated time), both UTC
    "Presenting state awards": ("2026-11-04T13:40:00", "2026-11-05T06:56:14"),
    "Meeting of the port safety working group": ("2026-11-02T14:00:00", "2026-11-05T13:26:06"),
}


def _late(titles) -> set[str]:
    return {p for p in _LATE for t in titles if " ".join(t.split()).startswith(p)}


@pytest.mark.asyncio
async def test_kremlin_items_posted_after_their_event_time(monkeypatch):
    cursor = _t("2026-11-04T16:15:00+00:00")         # the live cursor, 11-05
    at = _t("2026-11-05T20:51:00+00:00")
    old = await _handler_titles(_rss_handler(_KREMLIN, parser="atom"), cursor, at)
    assert _late(old) == set()                       # the loss, reproduced

    cfg = _descriptor_config("source_kremlin.yaml")
    assert cfg["cursor_timestamp"] == "latest"
    # The knob alone: posted-after-the-cursor is new, whatever the event date.
    knob = await _handler_titles(_rss_handler(_KREMLIN, parser="atom", cursor_timestamp="latest"), cursor, at)
    assert _late(knob) == set(_LATE)

    feed = _Feed(
        monkeypatch, kind="rss", config=cfg, transport=_serve(lambda: _KREMLIN),
        cursor={"last_pulled_at": cursor.isoformat()}, clock=at,
    )
    written = await feed.poll(at)
    assert _late(written) == set(_LATE)
    # The cursor follows the posting time (newest <updated> 17:28:55Z).
    assert await feed.cursor() == _t("2026-11-05T17:28:55+00:00")
    assert await feed.poll(at + timedelta(hours=6)) == []


# ---------------------------------------------------------------------------
# 5-7. Future-dated entries poisoned the cursor (Maritime Executive, Taipei
# Times, HRW; Daily Maverick / JPost at +0.8 h are the same class)
# ---------------------------------------------------------------------------

_MARITIME = _read("maritime_executive.xml")
_MARITIME_POISON = "Port Authorities Bust Shipment of Contraband Hidden in Cooking Oil"


def _maritime_at_0809(i, block: str) -> str:
    # The 08:09Z poll persisted this entry as published 2026-11-06T04:11:18Z
    # (signals row, 11-05); the feed has since corrected it to 11-05 03:11Z.
    if _MARITIME_POISON in block:
        return block.replace("2026-11-04T22:11:18-05:00", "2026-11-05T23:11:18-05:00")
    return block


@pytest.mark.asyncio
async def test_maritime_executive_future_entry_poisons_cursor(monkeypatch):
    t1, t2 = _t("2026-11-05T08:09:00+00:00"), _t("2026-11-05T20:09:00+00:00")
    doc1 = _doc(_MARITIME, lambda i, e: _entry_dt(e) <= t1, edit=_maritime_at_0809)
    doc2 = _doc(_MARITIME, lambda i, e: _entry_dt(e) <= t2)
    after_t1 = [e.title for e in feedparser.parse(doc2).entries if _entry_dt(e) > t1]
    assert len(after_t1) == 5

    # Old: the poison date (+20 h, under the 26 h clamp) became the cursor.
    old = await _handler_titles(_rss_handler(doc2), _t("2026-11-06T04:11:18+00:00"), t2)
    assert old == []

    docs = {"doc": doc1}
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://www.maritime-executive.com/articles.rss"},
        transport=_serve(lambda: docs["doc"]),
        cursor={"last_pulled_at": "2026-11-05T04:09:00+00:00"}, clock=t1,
    )
    await feed.poll(t1)
    assert await feed.cursor() == t1                 # clamped to now, not 11-06
    docs["doc"] = doc2
    written = await feed.poll(t2)
    assert set(after_t1) <= set(written)


_TAIPEI = _read("taipei_times.rdf")


@pytest.mark.asyncio
async def test_taipei_times_next_day_edition_dated_ahead(monkeypatch):
    """Every item of the 11-06 edition carries 2026-11-06T08:00+08:00 (=
    00:00Z) and is posted from ~17:00Z the evening before: the first batch set
    the cursor to 00:00Z and the rest of the edition was `<=` it."""
    t1, t2 = _t("2026-11-05T17:18:00+00:00"), _t("2026-11-05T20:18:00+00:00")
    titles = [e.title for e in feedparser.parse(_TAIPEI).entries]
    first_batch = titles[:3]       # the three rows written at 17:18Z (signals, 11-05)
    doc1 = _doc(_TAIPEI, lambda i, e: i < 3)

    old = await _handler_titles(_rss_handler(_TAIPEI), _t("2026-11-06T00:00:00+00:00"), t2)
    assert old == []

    docs = {"doc": doc1}
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://www.taipeitimes.com/xml/index.rss"},
        transport=_serve(lambda: docs["doc"]),
        cursor={"last_pulled_at": "2026-11-05T00:00:00+00:00"}, clock=t1,
    )
    assert sorted(await feed.poll(t1)) == sorted(first_batch)
    assert await feed.cursor() == t1
    docs["doc"] = _TAIPEI
    assert sorted(await feed.poll(t2)) == sorted(titles[3:])   # the rest, once


_HRW = _read("hrw_news.xml")
_HRW_EMBARGO = "Eastmere: Union Leader Gets Nearly 10 More Years in Prison"
_HRW_LOST = "Halden Expands Human Rights Sanctions Regime"


@pytest.mark.asyncio
async def test_hrw_embargoed_item_dated_ahead(monkeypatch):
    """04:52Z: HRW served the embargoed item dated 16:30Z (+11.6 h); the
    cursor sat at 16:30Z and the 13:00Z "Halden Expands ..." item was dropped."""
    t1, t2 = _t("2026-11-05T04:52:01+00:00"), _t("2026-11-05T16:52:00+00:00")
    doc1 = _doc(_HRW, lambda i, e: _entry_dt(e) <= t1 or e.title == _HRW_EMBARGO)
    doc2 = _doc(_HRW, lambda i, e: _entry_dt(e) <= t2)

    old = await _handler_titles(_rss_handler(doc2), _t("2026-11-05T16:30:00+00:00"), t2)
    assert _HRW_LOST not in old

    docs = {"doc": doc1}
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://www.hrw.org/rss/news"},
        transport=_serve(lambda: docs["doc"]),
        cursor={"last_pulled_at": "2026-11-04T22:30:00+00:00"}, clock=t1,
    )
    assert _HRW_EMBARGO in await feed.poll(t1)
    assert await feed.cursor() == t1
    docs["doc"] = doc2
    assert await feed.poll(t2) == [_HRW_LOST]        # and the embargoed one is not redone


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class _Stub:
    """Yields fixed signals; optionally advertises a look-back; may raise."""

    def __init__(self, signals, *, lookback: float | None = None, boom: bool = False):
        self._signals = signals
        self._boom = boom
        if lookback is not None:
            self.cursor_lookback_seconds = lookback
        self.seen_since = "unset"

    async def pull(self, ctx, since=None):
        self.seen_since = since
        for s in self._signals:
            yield s
        if self._boom:
            raise RuntimeError("upstream broke mid-pull")


def _sig(ts: datetime | None, title: str = "x") -> Signal:
    payload: dict[str, Any] = {"title": title, "external_id": title}
    if ts is not None:
        payload["_published_at_dt"] = ts
    return Signal(source_id="s", modality="text", payload=payload, content_hash=uuid4().hex)


_NOW = _t("2026-11-05T12:00:00+00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize("lookback", [None, 72 * 3600.0])
@pytest.mark.parametrize("boom", [False, True])
@pytest.mark.parametrize("stored", [None, "2027-11-05T12:00:00+00:00", "2026-11-05T13:00:00+00:00", "2026-11-05T11:00:00+00:00"])
@pytest.mark.parametrize("entries", [
    [],
    [_NOW + timedelta(days=365)],                   # the year-typo class
    [_NOW + timedelta(hours=20), _NOW - timedelta(hours=1)],
    [_NOW + timedelta(minutes=2)],                  # inside the tolerance
    [None],                                         # undated
])
async def test_cursor_is_never_set_later_than_now_plus_tolerance(monkeypatch, lookback, boom, stored, entries):
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://x.invalid"}, transport=_serve(lambda: ""),
        cursor={"last_pulled_at": stored} if stored else None, clock=_NOW,
    )
    feed.core.sd.handler = _Stub([_sig(ts, f"e{i}") for i, ts in enumerate(entries)], lookback=lookback, boom=boom)
    await feed.poll(_NOW)
    assert await feed.cursor() <= _NOW + sc.CURSOR_FUTURE_TOLERANCE
    assert await feed.cursor() <= _NOW               # healthy cursors never pass now
    # A poisoned stored cursor never reaches the handler as-is.
    seen = feed.core.sd.handler.seen_since
    assert seen is None or seen <= _NOW


@pytest.mark.asyncio
async def test_lookback_reyields_are_not_counted_toward_the_entry_cap(monkeypatch):
    """A judged entry the look-back re-yields costs no cap slot: 150 judged
    entries ahead of 3 new ones must not cap the poll before the new ones."""
    import legba.runtime.source_actor as sa

    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 10)
    old = [_sig(_NOW - timedelta(hours=10, minutes=i), f"old{i}") for i in range(150)]
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://x.invalid"}, transport=_serve(lambda: ""),
        cursor={"last_pulled_at": (_NOW - timedelta(hours=9)).isoformat()}, clock=_NOW,
    )
    stub = _Stub(old, lookback=72 * 3600.0)
    feed.core.sd.handler = stub
    for _ in range(20):                       # drain the backlog 10 at a time
        await feed.poll(_NOW)
    new = [_sig(_NOW - timedelta(minutes=i), f"new{i}") for i in range(3)]
    stub._signals = old + new
    assert sorted(await feed.poll(_NOW)) == ["new0", "new1", "new2"]
    cur = await feed.store.get("cursor")
    assert cur["lookback_skipped_last_poll"] == 150


@pytest.mark.asyncio
async def test_transient_enrichment_skip_is_not_recorded_as_judged(monkeypatch):
    """W11-F103 interplay: an entry skipped for an enrichment TIMEOUT is not
    put in the ledger, so the look-back re-yields it and the next poll retries."""
    import asyncio

    import legba.runtime.source_actor as sa

    calls = {"n": 0}
    real = sa.run_baseline

    async def flaky(raw, ctx, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError()
        return await real(raw, ctx, **kw)

    monkeypatch.setattr(sa, "run_baseline", flaky)
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://x.invalid"}, transport=_serve(lambda: ""),
        cursor={"last_pulled_at": (_NOW - timedelta(hours=3)).isoformat()}, clock=_NOW,
    )
    feed.core.sd.handler = _Stub([_sig(_NOW - timedelta(hours=1), "a")], lookback=72 * 3600.0)
    assert await feed.poll(_NOW) == []
    assert await feed.poll(_NOW) == ["a"]
    assert await feed.poll(_NOW) == []


def test_ledger_is_pruned_to_the_window():
    led = sc.LookbackLedger({}, timedelta(hours=72))
    led.mark(_sig(_NOW - timedelta(hours=100), "gone"))
    led.mark(_sig(_NOW - timedelta(hours=1), "kept"))
    led.mark(_sig(_NOW + timedelta(hours=11), "embargoed"))
    led.mark(_sig(None, "undated"))                             # not recorded
    fields = led.cursor_fields(_NOW)
    assert len(fields[sc.SEEN_FIELD]) == 2
    again = sc.LookbackLedger(fields, timedelta(hours=72))
    assert again.judged(_sig(_NOW - timedelta(hours=1), "kept"))
    assert again.judged(_sig(_NOW + timedelta(hours=11), "embargoed"))
    assert not again.judged(_sig(_NOW, "kept"))                 # re-dated = new
    assert not again.judged(_sig(None, "undated"))
    assert not again.judged(_sig(_NOW - timedelta(hours=100), "gone"))


def test_handlers_advertise_the_lookback():
    assert sc.lookback_of(RSSSourceHandler(RSSConfig(url="https://x.invalid"))) == sc.DEFAULT_LOOKBACK
    assert sc.lookback_of(RSSSourceHandler(RSSConfig(url="https://x.invalid", cursor_lookback_hours=0))) == timedelta(0)
    assert sc.lookback_of(JsonApiSourceHandler(JsonApiConfig(url_template="https://x.invalid"))) == sc.DEFAULT_LOOKBACK
    assert sc.lookback_of(object()) == timedelta(0)            # telegram, gdelt, ...
    assert sc.lookback_of(type("H", (), {"cursor_lookback_seconds": 10**9})()) == sc.LOOKBACK_CEILING


# ---------------------------------------------------------------------------
# Capped newest-first drain (2026-11-06: ISW 11 of the last 20 posts missing)
# ---------------------------------------------------------------------------


class _WindowedStub:
    """A dated feed with WordPress semantics: every entry newer than the
    ``since`` it is handed, newest first (or oldest first)."""

    cursor_lookback_seconds = 72 * 3600.0

    def __init__(self, entries: list[tuple[datetime, str]], *, newest_first: bool = True):
        self.entries = entries
        self.newest_first = newest_first

    async def pull(self, ctx, since=None):
        rows = [e for e in self.entries if since is None or e[0] > since]
        rows.sort(key=lambda e: e[0], reverse=self.newest_first)
        for ts, title in rows:
            yield _sig(ts, title)


@pytest.mark.asyncio
@pytest.mark.parametrize("newest_first", [True, False])
async def test_capped_drain_never_skips_items_across_polls(monkeypatch, newest_first):
    """20 items spread over 9.5 days (well past the 72 h look-back), a budget
    for 2 per poll. Every item is written exactly once across successive
    polls, in either order. The old advance (cursor to the newest processed
    item on every capped poll) wrote 6 of the 20."""
    import legba.runtime.source_actor as sa

    monkeypatch.setattr(sa, "_MAX_ENTRIES_PER_POLL", 2)
    entries = [(_NOW - timedelta(hours=12 * i), f"post{i:02d}") for i in range(20)]
    start = _NOW - timedelta(days=10)
    feed = _Feed(
        monkeypatch, kind="rss", config={"url": "https://x.invalid"}, transport=_serve(lambda: ""),
        cursor={"last_pulled_at": start.isoformat()}, clock=_NOW,
    )
    feed.core.sd.handler = _WindowedStub(entries, newest_first=newest_first)
    written: list[str] = []
    for _ in range(12):
        written += await feed.poll(_NOW)
        # The cursor never sits past an item no poll has written yet.
        unwritten = [ts for ts, title in entries if title not in written]
        if unwritten:
            assert await feed.cursor() < min(unwritten)
    assert sorted(written) == sorted(t for _, t in entries)
    assert len(written) == len(set(written)) == 20
    assert await feed.poll(_NOW) == []
    # The next new item is written by a complete poll, which moves the cursor
    # to it.
    fresh = _NOW + timedelta(hours=1)
    feed.core.sd.handler.entries.append((fresh, "post-new"))
    assert await feed.poll(fresh) == ["post-new"]
    assert await feed.cursor() == fresh


def test_walk_order_tracks_ascent_and_ignores_undated():
    w = sc.WalkOrder()
    w.see(_NOW - timedelta(hours=2))
    w.see(None)
    w.see(_NOW - timedelta(hours=1))
    assert w.ascending
    w.see(_NOW - timedelta(hours=3))
    assert not w.ascending


def test_advance_holds_a_dated_capped_descending_walk_only():
    class _NoHold:
        def cursor_ts(self, last, since):
            return last

    since = _NOW - timedelta(days=5)
    last = _NOW - timedelta(hours=1)
    kw = dict(errored=False, since=since, last_processed_ts=last, hold=_NoHold(), now=_NOW)
    assert sc.advance(dated=True, capped=True, walk_ascending=False, **kw) == since
    assert sc.advance(dated=True, capped=True, walk_ascending=True, **kw) == last
    assert sc.advance(dated=True, capped=False, walk_ascending=False, **kw) == last
    # No ledger, no hold: the historical anti-trap advance stands.
    assert sc.advance(dated=False, capped=True, walk_ascending=False, **kw) == last
