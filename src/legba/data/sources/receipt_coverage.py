# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How each source kind's signals get a receipt (lane B-1) — declared, tested.

Every registered kind (``legba.runtime.source_factory.discover_source_kinds``)
must appear here, keyed by its ``handler.kind``; ``tests/runtime/
test_receipt_every_kind.py`` fails for a kind that is missing, so a new kind
cannot ship without saying how its bytes are kept. The modes:

* ``http_tee`` — the handler fetches through the guarded egress client, whose
  transport tees every response into the poll's capture scope; the actor links
  each record to the response it was parsed from (``runtime/fetch_receipts``).
* ``message_capture`` — a non-HTTP transport records what it received itself
  (``fetch_capture.capture_received``): Telegram's message as the client got it.
* ``inbound_envelope`` — a PUSH kind: the envelope as the inbound drain
  received it is the receipt.
* ``stream_event`` — a SUBSCRIBED kind: each received event is a capture
  (``runtime/stream_ingest``).
* ``not_captured`` — no receipt yet. Pinned (the guard test lists the set and
  it may only shrink); each carries its reason. None of these kinds has an
  active source on the live instance (measured 2026-11-14).
"""

from __future__ import annotations

from typing import Mapping

__all__ = ["MODES", "NOT_CAPTURED_REASONS", "RECEIPT_COVERAGE"]

MODES = ("http_tee", "message_capture", "inbound_envelope", "stream_event", "not_captured")

RECEIPT_COVERAGE: Mapping[str, str] = {
    "rss": "http_tee",
    "geojson": "http_tee",
    "json_api": "http_tee",
    "acled": "http_tee",
    "ucdp": "http_tee",
    "mediacloud": "http_tee",
    "opensanctions": "http_tee",
    "gdelt_files": "http_tee",
    "scraper": "http_tee",
    "firecrawl": "http_tee",
    "instrument_snapshot": "http_tee",
    "page": "http_tee",
    "portwatch": "http_tee",
    "series_api": "http_tee",
    "telegram_channel": "message_capture",
    "generic_webhook": "inbound_envelope",
    "discord_webhook": "inbound_envelope",
    "stream_json": "stream_event",
    "email_imap": "not_captured",
    "gdelt_query": "not_captured",
    "common_crawl_news": "not_captured",
    "intelmq_collector_bridge": "not_captured",
}

NOT_CAPTURED_REASONS: Mapping[str, str] = {
    "email_imap": "IMAP (imaplib), not HTTP: the RFC822 message as fetched is the "
                  "capture to add through fetch_capture.capture_received",
    "gdelt_query": "BigQuery client, not the egress client: the result row as "
                   "returned is the capture to add",
    "common_crawl_news": "S3 (aiobotocore) WARC reads: the WARC record as read is "
                         "the capture to add",
    "intelmq_collector_bridge": "a collector subprocess's stdout or IntelMQ's Redis "
                                "queue, not HTTP: the IDF event line as read is the "
                                "capture to add",
}
