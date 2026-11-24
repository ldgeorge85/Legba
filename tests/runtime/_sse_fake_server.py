# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local Server-Sent Events server for the subscribe-connector tests (I-4).

A real TCP server on 127.0.0.1 (``asyncio.start_server``), speaking just
enough HTTP/1.1 for an event stream: it reads the request, records its
headers (``Last-Event-ID`` above all), answers ``200 text/event-stream`` with
``Connection: close`` and writes event blocks. Nothing leaves the host.

Each connection follows the next :class:`Plan` in ``plans`` (the last plan
repeats). A plan says which events to send and how the connection ends:

* ``end="close"`` — close after the events (the upstream ends the stream);
* ``end="hang"``  — keep the socket open and send NOTHING (a stall);
* ``end="beat"``  — send a keep-alive comment every ``beat_s`` until the
  client goes away (an honestly quiet stream);
* ``status`` — answer a non-200 status instead (a refused connect).

``log`` is the server's event history (id -> block). With ``resume=True`` a
connection carrying ``Last-Event-ID: k`` is sent the logged events after
``k`` before the plan's own (the upstream replays, as SSE servers do).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


def block(event_id: str | None, data: str, *, event: str | None = None) -> bytes:
    lines = []
    if event:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def item(i: int, *, published: str) -> str:
    return json.dumps({"Id": f"ev-{i}", "Title": f"Port strike update {i}",
                       "Summary": f"Dockworkers extended the stoppage, update {i}.",
                       "Published": published, "Url": f"https://news.example.org/strike/{i}"})


@dataclass
class Plan:
    events: list[tuple[str | None, str]] = field(default_factory=list)   # (id, data)
    end: str = "close"
    status: int = 200
    beat_s: float = 0.05
    gap_s: float = 0.0          # pause between events
    content_type: str = "text/event-stream"


class FakeSseServer:
    def __init__(self, plans: list[Plan], *, resume: bool = True) -> None:
        self.plans = plans
        self.resume = resume
        self.requests: list[dict[str, str]] = []
        self.log: list[tuple[str, bytes]] = []
        self._server: asyncio.base_events.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/stream"

    async def __aenter__(self) -> "FakeSseServer":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for w in self._writers:
            w.close()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        headers["_request_line"] = lines[0]
        self.requests.append(headers)
        plan = self.plans[min(len(self.requests) - 1, len(self.plans) - 1)]
        try:
            if plan.status != 200:
                writer.write(f"HTTP/1.1 {plan.status} Nope\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
                await writer.drain()
                return
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {plan.content_type}\r\n"
                          "Cache-Control: no-cache\r\nConnection: close\r\n\r\n").encode())
            await writer.drain()
            last = headers.get("last-event-id")
            if self.resume and last is not None:
                ids = [i for i, _ in self.log]
                if last in ids:
                    for _id, blk in self.log[ids.index(last) + 1:]:
                        writer.write(blk)
                    await writer.drain()
            for event_id, data in plan.events:
                blk = block(event_id, data)
                if event_id is not None and all(event_id != i for i, _ in self.log):
                    self.log.append((event_id, blk))
                writer.write(blk)
                await writer.drain()
                if plan.gap_s:
                    await asyncio.sleep(plan.gap_s)
            if plan.end == "hang":
                await asyncio.sleep(3600)
            elif plan.end == "beat":
                while True:
                    writer.write(b": keep-alive\n\n")
                    await writer.drain()
                    await asyncio.sleep(plan.beat_s)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
