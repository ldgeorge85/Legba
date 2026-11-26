# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a subscribed source's transport hands the spine.

A transport (``runtime/stream_transport.py``) turns bytes on a held
connection into :class:`StreamEvent` values; a subscribe-capable handler kind
(``stream_json``) turns one event into zero or more records through
``parse_event(ctx, event)``. The event keeps the exact bytes it arrived as
(``raw``): those are what the fetch receipt stores and hashes, so the proof
chain for a streamed record ends in the bytes Legba received, as a poll's
does.

Stdlib only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

__all__ = ["MalformedStreamEvent", "StreamEvent"]


class MalformedStreamEvent(ValueError):
    """The event arrived but cannot be read as a record (not JSON, the wrong
    shape, over the size bound). Counted on the window row; its bytes are
    still kept as a receipt — what was received is evidence either way."""


@dataclass
class StreamEvent:
    #: The transport's own event id (SSE ``id:``) — the resume point. None
    #: when the upstream sent none (such a stream cannot resume).
    id: str | None
    #: The event type (SSE ``event:``; ``message`` when absent).
    event: str
    #: The decoded payload text (SSE: the ``data:`` lines joined by ``\n``).
    data: str
    #: The event exactly as it arrived on the wire (the receipt's bytes).
    raw: bytes
    received_at: datetime
    #: Set by the transport when the event exceeded ``max_event_bytes`` (its
    #: ``data`` is then a prefix and the event is malformed by definition).
    oversize: bool = False
    _sha: str | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        if self._sha is None:
            self._sha = hashlib.sha256(self.raw).hexdigest()
        return self._sha
