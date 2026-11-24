# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The receipt of a Telegram message: what the client RECEIVED, serialised (B-1).

Telegram arrives over MTProto, not HTTP, so the egress tee never sees it and
every Telegram signal used to be written with no receipt (451 of the 1,559
unlinked signals of one live day). Its capture is the message object as the
client handed it to the handler, serialised deterministically:

* Telethon's own ``to_dict()`` when the object has one (the message's text,
  entities, dates, counters, reply / forward headers and the media
  DESCRIPTION Telethon already holds), else the attributes the handler reads;
* NO media bytes: media is never fetched for a receipt, and any ``bytes``
  value already inside the message object (a ``file_reference``, an inline
  ``stripped_thumb``) is replaced by ``{"bytes_sha256", "bytes_len"}`` — the
  receipt proves which value was there without keeping it;
* datetimes as ISO-8601, everything else as its JSON form or ``repr``.

The address is ``<canonical channel url>#message=<id>`` — the form the stream
connector (I-4, ``runtime/stream_ingest.receipt_address``) uses for one
received event — so the same message seen with different counters on a later
poll is a NEW version of the same address (nothing overwritten), and the same
bytes are the same version.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from .fetch_capture import capture_received

__all__ = ["MESSAGE_RECEIPT_FORMAT", "capture_telegram_message", "message_address", "serialise_message"]

MESSAGE_RECEIPT_FORMAT = "legba.telegram.message/1"
_MAX_DEPTH = 12
_FIELDS = ("id", "date", "edit_date", "message", "text", "views", "forwards", "post_author",
           "grouped_id", "pinned", "silent", "post", "noforwards")


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return repr(value)[:200]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"bytes_sha256": hashlib.sha256(raw).hexdigest(), "bytes_len": len(raw)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v, depth + 1) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _clean(to_dict(), depth + 1)
        except Exception:
            pass
    return repr(value)[:200]


def _message_dict(msg: Any) -> dict[str, Any]:
    to_dict = getattr(msg, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    out: dict[str, Any] = {}
    for name in _FIELDS:
        v = getattr(msg, name, None)
        if v is not None:
            out[name] = v
    media = getattr(msg, "media", None)
    if media is not None:
        out["media_type"] = type(media).__name__
    return out


def serialise_message(msg: Any, channel_info: dict[str, Any]) -> bytes:
    """The message as received: deterministic JSON bytes (sorted keys)."""
    doc = {
        "format": MESSAGE_RECEIPT_FORMAT,
        "channel": {k: channel_info.get(k) for k in ("username", "id", "title")},
        "message": _clean(_message_dict(msg)),
    }
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=repr).encode("utf-8")


def message_address(channel_info: dict[str, Any], msg_id: Any) -> tuple[str, str]:
    """``(url, address)`` of one message: the channel's canonical URL and
    ``<that url>#message=<id>``."""
    handle = channel_info.get("username") or channel_info.get("id") or "unknown"
    base = f"https://t.me/{quote(str(handle), safe='')}"
    return base, f"{base}#message={quote(str(msg_id), safe='')}"


def capture_telegram_message(msg: Any, channel_info: dict[str, Any]) -> bool:
    """Record the message as received in the active capture scope (the source
    actor's poll). Best-effort and silent: ``False`` when nothing was recorded."""
    try:
        body = serialise_message(msg, channel_info)
        url, address = message_address(channel_info, getattr(msg, "id", None))
    except Exception:
        return False
    return capture_received(url=url, body=body, content_type="application/json", address=address)
