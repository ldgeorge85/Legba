# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``stream_json`` — the first SUBSCRIBE-capable source kind.

A subscribed source's transport (``subscribe.transport``, e.g. ``sse``)
hands the spine one :class:`~.stream_event.StreamEvent` at a time; this kind
reads an event's payload as JSON — one item, or an array of items at
``items_path`` — and maps each item to a :class:`Signal` through the SAME
mapping the polled ``json_api`` kind uses (:func:`.json_api.map_json_item`).
So a record that reaches Legba over a held connection and the same record
fetched by a poll become the same signal: same payload, same content hash,
same dedupe key. Only ``raw_provenance`` says how it arrived.

The kind never polls (``pull`` is an empty generator, like a push kind) and
never opens a connection itself: the connection belongs to the stream
connector (``runtime/stream_connector.py``), which owns the transport, the
cursor, the bounds and the receipts. This module only PARSES, so it imports
under the slim registry image (pydantic + the json_api mapping).

First connect: ``first_connect_max_entry_age_days`` is this kind's first-poll
age cap (W11-F265 semantics, ``runtime/source_first_poll_age.py``): on a
connect with no stored resume point, an upstream that replays history has
every item older than the cap skipped and COUNTED (``age_skipped`` on the
window row), for the whole first connection, not only its first batch.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import AsyncIterator, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import Signal, SourceContext, SourceHealth
from .json_api import map_json_item, parse_path, resolve_path
from .json_api_mapping import JsonApiGeometry, parse_title_template
from .stream_event import MalformedStreamEvent, StreamEvent

__all__ = ["DEFAULT_FIRST_CONNECT_MAX_AGE_DAYS", "StreamJsonConfig", "StreamJsonSourceHandler"]

#: The default first-connect age cap (the rss kind's first-poll default).
DEFAULT_FIRST_CONNECT_MAX_AGE_DAYS = 30.0


class StreamJsonConfig(BaseModel):
    """The item mapping (the ``json_api`` field paths, same grammar) plus
    where items sit inside one event's JSON payload."""

    model_config = ConfigDict(extra="forbid")

    #: Path to an item ARRAY inside one event's payload; empty = the payload
    #: is ONE item (a JSON object).
    items_path: str = Field(default="", max_length=512)
    id_path: str = Field(default="id", max_length=512)
    title_path: str | None = Field(default=None, max_length=512)
    url_path: str | None = Field(default=None, max_length=512)
    timestamp_path: str | None = Field(default=None, max_length=512)
    body_path: str | None = Field(default=None, max_length=512)
    geo_path: str | None = Field(default=None, max_length=512)
    title_template: str | None = Field(default=None, min_length=1, max_length=512)
    body_template: str | None = Field(default=None, min_length=1, max_length=2048)
    geometry: JsonApiGeometry | None = None
    modality: Literal["text", "structured"] = "text"
    static_tags: list[str] = Field(default_factory=list, max_length=16)
    language: str | None = Field(default=None, max_length=8)
    #: Items per event above this are malformed (bounds one event's fan-out).
    max_items_per_event: int = Field(default=1000, ge=1, le=10_000)
    #: First-connect age cap in days (None = the 30-day default; 0 = no cap).
    first_connect_max_entry_age_days: float | None = Field(default=None, ge=0, le=36_500)

    @field_validator("items_path", "id_path", "title_path", "url_path",
                     "timestamp_path", "body_path", "geo_path")
    @classmethod
    def _validate_paths(cls, v: str | None) -> str | None:
        if v is not None:
            parse_path(v)
        return v

    @field_validator("title_template", "body_template")
    @classmethod
    def _validate_template(cls, v: str | None) -> str | None:
        if v is not None:
            parse_title_template(v)
        return v


class StreamJsonSourceHandler:
    """Parses one received event into signals; never polls, never connects."""

    kind: ClassVar[str] = "stream_json"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.stream_json/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = StreamJsonConfig
    handler_version: ClassVar[str] = "0.1.0"
    #: Marks a kind that can serve ``acquisition: subscribe`` (the
    #: connector refuses a descriptor whose kind cannot).
    subscribe_capable: ClassVar[bool] = True

    def __init__(self, config: StreamJsonConfig) -> None:
        self._config = config

    @property
    def first_poll_max_entry_age_seconds(self) -> float:
        """The first-connect age cap the connector reads (0 = no cap)."""
        days = self._config.first_connect_max_entry_age_days
        if days is None:
            return DEFAULT_FIRST_CONNECT_MAX_AGE_DAYS * 86400.0
        return float(days) * 86400.0

    def parse_event(self, ctx: SourceContext, event: StreamEvent, *, stream_url: str,
                    transport: str) -> list[Signal]:
        """One event → its signals. Raises :class:`MalformedStreamEvent` when
        the event cannot be read (oversize, not JSON, wrong shape)."""
        cfg = self._config
        if event.oversize:
            raise MalformedStreamEvent(f"event over the size bound ({len(event.raw)} bytes kept)")
        try:
            doc = json.loads(event.data)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise MalformedStreamEvent(f"event data is not JSON: {exc}") from exc
        if cfg.items_path:
            node = resolve_path(doc, parse_path(cfg.items_path))
            if not isinstance(node, list):
                raise MalformedStreamEvent(f"items_path {cfg.items_path!r} is not an array in this event")
            items = node
        else:
            items = [doc]
        if len(items) > cfg.max_items_per_event:
            raise MalformedStreamEvent(f"{len(items)} items in one event (max {cfg.max_items_per_event})")
        out: list[Signal] = []
        for item in items:
            if not isinstance(item, dict):
                raise MalformedStreamEvent(f"an item is a {type(item).__name__}, not a JSON object")
            out.append(map_json_item(cfg, item, ctx=ctx, source_url=stream_url, raw_provenance={
                "kind": self.kind,
                "schema_version": self.schema_version,
                "fetch_kind": "stream",
                "transport": transport,
                "stream_url": stream_url,
                "event_id": event.id,
                "event_type": event.event,
                "event_sha256": event.sha256,
                "received_at": event.received_at.isoformat(),
            }))
        return out

    async def pull(self, ctx: SourceContext, since: datetime | None = None) -> AsyncIterator[Signal]:
        """A subscribed kind is never polled — empty generator."""
        return
        yield  # pragma: no cover

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        # The connection's health is the connector's (the window rows); the
        # parser itself has nothing to probe.
        return SourceHealth(state="healthy", detail={"subscribe": True})
