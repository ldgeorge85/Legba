# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``subscribe`` block of a source descriptor (lane I-4).

A SUBSCRIBED source is one Legba holds a connection open to, receiving
records as the upstream emits them. It is the fourth acquisition mode beside
``poll`` (a reminder wakes the actor, the handler pulls), ``push`` (an
upstream POSTs to the webhook front) and the intake's ``request``. It lands
on the same spine: the same handler kinds parse what arrives, the same sinks
write it, the same fetch receipts keep the bytes, and a window of the held
connection writes the same ``source_poll_outcomes`` row a poll writes.

The connection protocol is an OPTION per source: ``transport`` names it, and
this block carries that transport's settings plus the spine bounds every
transport shares (the receive queue cap, the rate bound, the idle timeout
that declares a stall, the reconnect backoff, the outcome window).

Transports DECLARED here but not built are refused at save with a message
naming the seam (``docs/SEAMS.md`` #78), never accepted and left to fail at
connect — the no-stubs rule. Built today: ``sse`` (Server-Sent Events over
HTTP, ``runtime/stream_sse.py``).

Stdlib + pydantic only: the registry validates descriptors in the slim image.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BUILT_TRANSPORTS",
    "DECLARED_TRANSPORTS",
    "FORBID_SUBSCRIBE_LICENSE_CLASSES",
    "SUBSCRIBE_SEAM",
    "SourceSubscribe",
    "SubscribeAuth",
    "validate_subscribe_descriptor",
]

#: Every transport name the vocabulary admits (so a descriptor naming one of
#: these gets the honest "declared, not built" refusal, and anything else the
#: plain "unknown transport" one).
DECLARED_TRANSPORTS: tuple[str, ...] = ("sse", "websocket", "nats")
#: The transports with a built connector module.
BUILT_TRANSPORTS: frozenset[str] = frozenset({"sse"})

SUBSCRIBE_SEAM = "SEAMS #78"

#: Licence classes that refuse a held connection outright — the same set the
#: page landing and the evidence archiver treat as "never fetch".
FORBID_SUBSCRIBE_LICENSE_CLASSES = frozenset({"anti_ai_walled", "tos_restrictive", "personal_use_only"})

_HEADER_NAME = r"^[A-Za-z0-9-]{1,64}$"
_FORBIDDEN_STATIC_HEADERS = frozenset({
    "authorization", "cookie", "proxy-authorization", "last-event-id", "host", "accept",
})


class SubscribeAuth(BaseModel):
    """A credential sent on connect, resolved from the vault at connect time
    (``secret_ref`` is a vault reference, never a raw secret). Declared and
    unresolvable is a refused connect, never an unauthenticated one."""

    model_config = ConfigDict(strict=True, extra="forbid")

    header: str = Field(default="Authorization", pattern=_HEADER_NAME)
    secret_ref: str = Field(min_length=3, max_length=256, pattern=r"^[a-z0-9_.:-]+$")
    #: ``{secret}`` is replaced by the resolved value (``"Bearer {secret}"``).
    value_template: str = Field(default="{secret}", min_length=8, max_length=128)

    @field_validator("value_template")
    @classmethod
    def _has_slot(cls, v: str) -> str:
        if v.count("{secret}") != 1:
            raise ValueError("subscribe.auth.value_template must contain '{secret}' exactly once")
        return v


class SourceSubscribe(BaseModel):
    """How a subscribed source's connection is held."""

    model_config = ConfigDict(strict=True, extra="forbid")

    transport: Literal["sse", "websocket", "nats"]
    #: The stream endpoint. For ``sse`` an http(s) URL; it is fetched through
    #: the same SSRF egress guard as every poll.
    url: str = Field(min_length=8, max_length=2048)
    #: Static, non-secret request headers (a credential goes in ``auth``).
    headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    auth: SubscribeAuth | None = None
    #: ``sse``: the ``event:`` types to keep (empty = every type; an event
    #: with no type is ``message``). Other types are counted, not parsed.
    event_types: list[str] = Field(default_factory=list, max_length=32)
    #: ``last_event_id``: reconnect with the last processed event id (the
    #: ``Last-Event-ID`` header for SSE). ``none``: never resume — every
    #: reconnect is a DECLARED gap.
    resume: Literal["last_event_id", "none"] = "last_event_id"
    #: No bytes at all (events or the upstream's keep-alive comments) for
    #: this long = a STALL: the connection is dropped and re-opened.
    idle_timeout_seconds: float = Field(default=90.0, ge=5.0, le=3600.0)
    #: Reconnect backoff: full jitter over min(max, initial * 2^attempt).
    backoff_initial_seconds: float = Field(default=1.0, ge=0.05, le=60.0)
    backoff_max_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    #: The bounded receive queue. Full = the event is dropped and the gap
    #: DECLARED (``source_stream_gaps``) and counted — never silent.
    queue_max: int = Field(default=1000, ge=1, le=100_000)
    #: Per-source rate bound on what reaches the sinks (events per second).
    #: Faster arrival waits in the queue; a queue that fills overflows.
    max_events_per_second: float = Field(default=50.0, gt=0.0, le=10_000.0)
    #: Events written per batch (one receipt flush, one cursor persist).
    batch_max: int = Field(default=100, ge=1, le=1000)
    #: One ``source_poll_outcomes`` row per window — the poll-outcome
    #: equivalent the health and firing views read.
    outcome_window_seconds: int = Field(default=300, ge=30, le=3600)
    #: An event larger than this is MALFORMED (counted; its prefix is not
    #: parsed). Bounds memory per event.
    max_event_bytes: int = Field(default=1_000_000, ge=1024, le=16_000_000)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https", "nats", "ws", "wss") or not parts.netloc:
            raise ValueError(f"subscribe.url must be an absolute URL, got {v!r}")
        if parts.username or parts.password:
            raise ValueError("subscribe.url may not carry credentials (use subscribe.auth)")
        return v

    @field_validator("headers")
    @classmethod
    def _static_headers(cls, v: dict[str, str]) -> dict[str, str]:
        import re  # noqa: PLC0415

        for name, value in v.items():
            if not re.match(_HEADER_NAME, name):
                raise ValueError(f"subscribe.headers: bad header name {name!r}")
            if name.lower() in _FORBIDDEN_STATIC_HEADERS:
                raise ValueError(
                    f"subscribe.headers may not set {name!r} (credentials go in subscribe.auth; "
                    "the connector owns Accept and Last-Event-ID)"
                )
            if len(value) > 512 or "\n" in value or "\r" in value:
                raise ValueError(f"subscribe.headers[{name!r}] is not a single-line value <= 512 chars")
        return v

    @model_validator(mode="after")
    def _transport_rules(self) -> "SourceSubscribe":
        if self.transport not in BUILT_TRANSPORTS:
            raise ValueError(
                f"subscribe.transport={self.transport!r} is declared but not built "
                f"({SUBSCRIBE_SEAM}: built transports: {sorted(BUILT_TRANSPORTS)}); a second "
                "transport is one connector module behind runtime/stream_transport.py"
            )
        if self.transport == "sse" and urlsplit(self.url).scheme not in ("http", "https"):
            raise ValueError("subscribe.transport='sse' needs an http(s) url")
        if self.backoff_max_seconds < self.backoff_initial_seconds:
            raise ValueError("subscribe.backoff_max_seconds must be >= backoff_initial_seconds")
        if self.idle_timeout_seconds >= self.outcome_window_seconds * 4:
            raise ValueError(
                "subscribe.idle_timeout_seconds must be under 4 outcome windows, or a stalled "
                "connection could look connected to the health views"
            )
        return self


def validate_subscribe_descriptor(
    *, acquisition: str, subscribe: SourceSubscribe | None, license_class: str | None,
) -> None:
    """The save-time rules for the ``subscribe`` acquisition (raises
    ``ValueError``; the descriptor validator turns it into a 422)."""
    if acquisition != "subscribe":
        if subscribe is not None:
            raise ValueError(
                f"a 'subscribe' block needs acquisition='subscribe' (this source is {acquisition!r})"
            )
        return
    if subscribe is None:
        raise ValueError(
            "acquisition='subscribe' needs a 'subscribe' block naming the transport and the "
            "stream url (built transports: " + ", ".join(sorted(BUILT_TRANSPORTS)) + ")"
        )
    # A held connection is a standing commitment to a host: its terms are
    # declared like any source's, and a reviewed-restrictive class refuses it.
    if license_class is None:
        raise ValueError(
            "a subscribed source must declare scope.license_class (its terms note); "
            "'unknown' is the honest reviewed-but-undetermined value"
        )
    if license_class in FORBID_SUBSCRIBE_LICENSE_CLASSES:
        raise ValueError(
            f"scope.license_class={license_class!r} refuses a held connection "
            f"(never-fetch classes: {sorted(FORBID_SUBSCRIBE_LICENSE_CLASSES)})"
        )
