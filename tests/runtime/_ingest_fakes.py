# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared doubles for the N-L ingestion tests: a RECORDING pool that answers
the source poll path's SQL like a tiny in-memory Postgres, a NATS recorder, a
normaliser that makes two runs comparable, and live-shaped handler fixtures
(RSS, GeoJSON, json_api, GDELT files, Telegram)."""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import httpx

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    FilterStage,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources.fetch_capture import ReceiptTeeTransport

REPO = Path(__file__).resolve().parents[2]
FROZEN_ACTOR = REPO / "tests" / "fixtures" / "ingestion" / "source_actor_9a59a0da.py"


def load_frozen_actor() -> Any:
    spec = importlib.util.spec_from_file_location("legba_frozen_source_actor_9a59a0da", FROZEN_ACTOR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Recording pool
# ---------------------------------------------------------------------------


class _Row(dict):
    pass


class RecordingConn:
    def __init__(self, db: "RecordingPool") -> None:
        self.db = db

    def transaction(self) -> Any:
        class _Tx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    def _log(self, method: str, query: str, args: tuple) -> None:
        self.db.calls.append((method, " ".join(query.split()), args))

    async def execute(self, query: str, *args: Any) -> str:
        self._log("execute", query, args)
        q = " ".join(query.split())
        if q.startswith("INSERT INTO public.actor_filter_state"):
            self.db.state[(args[0], args[1], args[2])] = args[3]
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self._log("fetchrow", query, args)
        q = " ".join(query.split())
        if q.startswith("SELECT value FROM public.actor_filter_state"):
            v = self.db.state.get((args[0], args[1], args[2]))
            return None if v is None else _Row(value=v)
        if q.startswith("INSERT INTO signals"):
            if any(r["id"] == args[0] for r in self.db.signals):
                return None  # ON CONFLICT (id) DO NOTHING
            self.db.signals.append({"id": args[0], "source_id": args[1], "content_hash": args[23]})
            return _Row(id=args[0])
        if q.startswith("INSERT INTO fetch_versions"):
            key = (args[0], args[2])
            if key in self.db.versions:
                return _Row(id=self.db.versions[key], inserted=False, first_seen_at=args[16])
            self.db.versions[key] = len(self.db.versions) + 1
            return _Row(id=self.db.versions[key], inserted=True, first_seen_at=args[16])
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._log("fetchval", query, args)
        q = " ".join(query.split())
        if q.startswith("UPDATE signals SET last_seen_at"):
            for row in reversed(self.db.signals):
                if row["source_id"] == args[0] and row["content_hash"] == args[1]:
                    return row["id"]
            return None
        if q.startswith("INSERT INTO fetch_versions"):   # the inbound receipt upsert
            key = ("inbound", args[2], args[1])
            self.db.versions.setdefault(key, len(self.db.versions) + 1)
            return self.db.versions[key]
        return None

    async def fetch(self, query: str, *args: Any) -> list:
        self._log("fetch", query, args)
        return []


class RecordingPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.state: dict[tuple, Any] = {}
        self.signals: list[dict] = []
        self.versions: dict[tuple, int] = {}

    def acquire(self) -> Any:
        conn = RecordingConn(self)

        class _Acq:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()

    def calls_on(self, *, exclude_prefixes: tuple[str, ...] = ("fetch_",)) -> list:
        out = []
        for method, q, args in self.calls:
            tables = re.findall(r"(?:INTO|FROM|UPDATE|JOIN)\s+(?:public\.)?([a-z_]+)", q)
            if any(t.startswith(exclude_prefixes) for t in tables):
                continue
            out.append((method, q, args))
        return out

    def strip_url_hash_column(self) -> int:
        """N-S (migration 0247) added ONE column to the document sink's
        INSERT: ``canonical_url_hash``, bound as the last argument. Rewrite
        every such recorded INSERT back to its pre-0247 text and arguments,
        asserting on the way that the dropped value is exactly
        ``stored_canonical_url_hash(<the canonical_url argument>)`` — so a
        before/after comparison proves the column is the only change and that
        it is a pure function of a value already written. Returns the number
        of statements rewritten."""
        from legba.data._url_canon import stored_canonical_url_hash

        rewritten = 0
        for i, (method, q, args) in enumerate(self.calls):
            if not (q.startswith("INSERT INTO signals") and "canonical_url_hash" in q):
                continue
            assert args[-1] == stored_canonical_url_hash(args[15]), (args[15], args[-1])
            q = q.replace(", data, canonical_url_hash )", ", data )").replace(
                ", $29::jsonb, $30 )", ", $29::jsonb )")
            assert "canonical_url_hash" not in q and "$30" not in q, q
            self.calls[i] = (method, q, args[:-1])
            rewritten += 1
        return rewritten

    def fetch_table_calls(self) -> list:
        return [c for c in self.calls if re.search(r"\bfetch_(versions|version_records)\b", c[1])]


class NatsRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []

    async def __call__(self, subject: str, data: bytes) -> None:
        self.messages.append((subject, data))


# ---------------------------------------------------------------------------
# Normaliser
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EPOCH_RE = re.compile(r"\b1\d{9}\.\d+\b")
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def normalise(obj: Any, *, now: datetime) -> Any:
    """Make two runs comparable: every UUID becomes ``<uuid-N>`` by order of
    first appearance, every timestamp within a day of ``now`` becomes
    ``<now>`` (fetch times and the actor's own clock), everything else is kept
    verbatim."""
    ids: dict[str, str] = {}

    def _uuid(s: str) -> str:
        return ids.setdefault(s, f"<uuid-{len(ids)}>")

    def _ts(m: re.Match) -> str:
        raw = m.group(0)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return "<now>" if abs((dt - now).total_seconds()) < 86400 else raw

    def _walk(x: Any) -> Any:
        if isinstance(x, UUID):
            return _uuid(str(x))
        if isinstance(x, datetime):
            return _ts(_ISO_RE.match(x.isoformat()))
        if isinstance(x, (bytes, bytearray)):
            return _walk(bytes(x).decode("utf-8", "replace"))
        if isinstance(x, float) and abs(x - now.timestamp()) < 86400:
            return "<now>"
        if isinstance(x, str):
            x = _EPOCH_RE.sub(
                lambda m: "<now>" if abs(float(m.group(0)) - now.timestamp()) < 86400 else m.group(0), x)
            return _ISO_RE.sub(_ts, _UUID_RE.sub(lambda m: _uuid(m.group(0)), x))
        if isinstance(x, (list, tuple)):
            return [_walk(i) for i in x]
        if isinstance(x, dict):
            return {k: _walk(v) for k, v in x.items()}
        return x

    return _walk(obj)


# ---------------------------------------------------------------------------
# Descriptors + handler fixtures
# ---------------------------------------------------------------------------


def descriptor(source_id: str, kind: str, config: dict[str, Any], *, dedupe: bool = True) -> SourceDescriptor:
    filters = [FilterStage(kind="dedupe_tier_1"), FilterStage(kind="dedupe_tier_2")] if dedupe else []
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name=f"{kind} fixture", kind=kind,
            schema_uri="legba/source/3.0.0", version="b" * 16,
            owner="test:n_l", created=datetime(2026, 11, 1, tzinfo=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="shared", languages=["en"], access_class="public",
                          license_class="permissive_feed_unreviewed"),
        acquisition="poll",
        config=config,
        cadence=CadenceBlock(schedule=Cron(raw="*/15 * * * *")),
        pipeline=SourcePipeline(ingestion_filters=filters, media="reference"),
    )


def client_for(handler_fn: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """A test client whose transport is the receipt tee around a MockTransport
    (production gets the same tee from SsrfGuardedTransport)."""
    return httpx.AsyncClient(transport=ReceiptTeeTransport(httpx.MockTransport(handler_fn)),
                             follow_redirects=True)


def _rss_xml(now: datetime) -> bytes:
    items = "".join(
        f"<item><title>Item {i}: port strike widens</title>"
        f"<link>https://news.example.org/a/{i}</link>"
        f"<guid>https://news.example.org/a/{i}</guid>"
        f"<description>Body text {i} about dockworkers in Rotterdam.</description>"
        f"<pubDate>{(now - timedelta(minutes=10 * i)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i in range(1, 4)
    )
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title>'
            f"<link>https://news.example.org</link><description>x</description>{items}</channel></rss>").encode()


def rss_case(now: datetime) -> tuple[SourceDescriptor, Callable[[], Any]]:
    body = gzip.compress(_rss_xml(now))

    def _serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={
            "content-type": "application/rss+xml; charset=utf-8", "content-encoding": "gzip",
            "etag": '"v1"'})

    def _build() -> Any:
        from legba.data.sources.rss import RSSConfig, RSSSourceHandler

        return RSSSourceHandler(RSSConfig(url="https://news.example.org/feed"), http_client=client_for(_serve))

    return descriptor("source.test.nl_rss", "rss", {"url": "https://news.example.org/feed"}), _build


def geojson_case(now: datetime) -> tuple[SourceDescriptor, Callable[[], Any]]:
    feats = [{"type": "Feature", "id": f"ev{i}",
              "properties": {"title": f"M 5.{i} - near Chupaca, Peru", "time": int((now - timedelta(hours=i)).timestamp() * 1000),
                             "url": f"https://quake.example.org/ev{i}", "mag": 5 + i / 10},
              "geometry": {"type": "Point", "coordinates": [-75.2, -12.1, 10.0]}} for i in range(1, 3)]
    body = json.dumps({"type": "FeatureCollection", "features": feats}).encode()

    def _serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/geo+json"})

    cfg = {"url": "https://quake.example.org/feed.geojson", "feature_id_key": "id"}

    def _build() -> Any:
        from legba.data.sources.geojson import GeoJSONConfig, GeoJSONSourceHandler

        return GeoJSONSourceHandler(GeoJSONConfig(**cfg), http_client=client_for(_serve))

    return descriptor("source.test.nl_geojson", "geojson", cfg), _build


def json_api_case(now: datetime) -> tuple[SourceDescriptor, Callable[[], Any]]:
    body = json.dumps({"value": [
        {"Id": f"don-{i}", "Title": f"Disease outbreak {i}", "Summary": f"Summary {i} of an outbreak.",
         "PublicationDateAndTime": (now - timedelta(hours=i)).isoformat(),
         "Url": f"https://who.example.org/don/{i}"} for i in range(1, 4)]}).encode()

    def _serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    cfg = {"url_template": "https://who.example.org/api/don", "items_path": "value", "id_path": "Id",
           "title_path": "Title", "timestamp_path": "PublicationDateAndTime", "body_path": "Summary",
           "url_path": "Url", "language": "en"}

    def _build() -> Any:
        from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler

        return JsonApiSourceHandler(JsonApiConfig(**cfg), http_client=client_for(_serve))

    return descriptor("source.test.nl_json_api", "json_api", cfg), _build


def gdelt_case(now: datetime) -> tuple[SourceDescriptor, Callable[[], Any]]:
    from legba.data.sources.gdelt_files import EVENTS_EXPORT_COLUMNS, GDELTFilesSourceHandler

    slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    stamp = slot.strftime("%Y%m%d%H%M%S")
    export_url = f"http://data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"
    rows = []
    for i, (root, ment) in enumerate((("19", "25"), ("18", "30"), ("04", "40")), start=1):
        base = {c: "" for c in EVENTS_EXPORT_COLUMNS}
        base.update({
            "GLOBALEVENTID": f"20000{i}", "SQLDATE": slot.strftime("%Y%m%d"), "MonthYear": slot.strftime("%Y%m"),
            "Year": slot.strftime("%Y"), "Actor1Name": "UNITED STATES", "Actor1CountryCode": "US",
            "Actor2Name": "IRAN", "Actor2CountryCode": "IR", "IsRootEvent": "1",
            "EventCode": f"{root}0", "EventBaseCode": f"{root}0", "EventRootCode": root, "QuadClass": "4",
            "GoldsteinScale": "-6.0", "NumMentions": ment, "NumSources": "5", "NumArticles": "10",
            "AvgTone": "-4.2", "ActionGeo_Type": "1", "ActionGeo_FullName": "Strait of Hormuz",
            "ActionGeo_CountryCode": "IR", "ActionGeo_Lat": "26.5", "ActionGeo_Long": "56.25",
            "DATEADDED": stamp, "SOURCEURL": f"https://example.com/gdelt/{i}",
        })
        rows.append("\t".join(base[c] for c in EVENTS_EXPORT_COLUMNS))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{stamp}.export.CSV", "\n".join(rows) + "\n")
    zbody = buf.getvalue()
    lastupdate = (f"1 {'a' * 32} {export_url}\n2 {'b' * 32} http://data.gdeltproject.org/gdeltv2/{stamp}.mentions.CSV.zip\n"
                  f"3 {'c' * 32} http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip\n")

    def _serve(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("lastupdate.txt"):
            return httpx.Response(200, text=lastupdate)
        if url == export_url:
            return httpx.Response(200, content=zbody, headers={"content-type": "application/zip"})
        return httpx.Response(404)

    def _build() -> Any:
        h = GDELTFilesSourceHandler()
        h._http_client_factory = lambda: client_for(_serve)
        return h

    return descriptor("source.test.nl_gdelt", "gdelt_files", {}, dedupe=False), _build


def gdelt_redirect_case(now: datetime) -> tuple[SourceDescriptor, Callable[[], Any]]:
    """B-1: :func:`gdelt_case` as the live host serves it — every ``http://``
    URL lastupdate.txt names is answered with a 301 to ``https://``."""
    desc, build = gdelt_case(now)

    def _build() -> Any:
        h = build()
        served = h._http_client_factory()._transport._inner.handler   # the MockTransport's

        def _serve(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith("http://"):
                return httpx.Response(301, headers={"location": "https://" + url[len("http://"):]})
            return served(httpx.Request(request.method, "http://" + url[len("https://"):]))

        h._http_client_factory = lambda: client_for(_serve)
        return h

    return desc, _build


def telegram_case(now: datetime) -> tuple[SourceDescriptor, Callable[[], Any]]:
    from tests.data_pkg.test_source_telegram import (
        _StubChannelState,
        _StubEntity,
        _StubMessage,
        _StubTelegramClient,
        _configure_handler,
        _make_config,
    )
    from legba.data.sources.telegram import TelegramChannelSourceHandler

    msgs = [_StubMessage(id=10 + i, text=f"message {i} from the front", date=now - timedelta(minutes=10 - i),
                         views=100 * i, forwards=i) for i in range(3)]
    cfg = _make_config(channels=["@warnews"], lookback_hours=48)

    async def _build_async() -> Any:
        client = _StubTelegramClient({"warnews": _StubChannelState(
            entity=_StubEntity(username="warnews", id=1001, title="War News"), messages=list(msgs))})
        handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
        await _configure_handler(handler, cfg)
        return handler

    return descriptor("source.test.nl_telegram", "telegram_channel", {}, dedupe=False), _build_async
