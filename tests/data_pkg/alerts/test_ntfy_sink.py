# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E-2 ntfy alert sink — native publish shape, gating, retry, redaction.

Mirrors ``test_webhook_sink.py`` (recording fake HTTP exercising the
production classification + retry code) plus the dispatcher ledger seam
from ``test_dispatcher_fanout.py``, plus ONE real-wire test against a
local ``http.server`` (the ``test_source_actor_acquisition.py`` pattern)
so the native ntfy headers + plaintext body are asserted as they actually
leave a real ``httpx`` client.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import uuid4

import httpx
import pytest

from legba.data.alerts import AlertSinkDispatcher, NtfyAlertSink
from legba.data.alerts.sinks import AlertSinkPayload, registered_sink_kinds
from legba.data.alerts.ntfy_sink import (
    ENV_NTFY_MIN_SEVERITY,
    ENV_NTFY_TOKEN,
    ENV_NTFY_URL,
    ntfy_message,
)

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fakes (mirrors the webhook sink test fakes)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Http:
    """Returns a queued sequence of responses (or raises queued exceptions)."""

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls: list[dict[str, Any]] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append(
            {"url": url, "content": content, "headers": dict(headers or {}),
             "timeout": timeout}
        )
        out = self._outcomes.pop(0) if self._outcomes else _Resp(200)
        if isinstance(out, BaseException):
            raise out
        return out


class _RecordingPool:
    """Duck-typed pool — records execute() INSERTs (the ledger seam)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def fetch(self, sql: str, *args: Any) -> Any:
        return []

    def ntfy_rows(self) -> list[tuple[Any, ...]]:
        return [
            args for sql, args in self.calls
            if "INSERT INTO alert_sink_deliveries" in sql and args[2] == "ntfy"
        ]


async def _noop_sleep(_seconds: float) -> None:
    return None


#: The topic path IS the secret — it must never survive redaction.
SECRET_TOPIC_URL = "https://ntfy.example.com/legba-secret-topic"
RECEIPT_URL = (
    "https://legba.example.org/api/v1/lineage/finding/"
    "7e0b0f37-0000-0000-0000-000000000000"
)


def _payload(
    severity: str = "high",
    *,
    channel: str = "escalations",
    target_id: str | None = "ua",
    confidence: float | None = 0.91,
    receipt_url: str | None = RECEIPT_URL,
    alert_row_id: str | None = "7e0b0f37-0000-0000-0000-000000000000",
) -> AlertSinkPayload:
    return AlertSinkPayload(
        summary="Coup-risk spike",
        detail="Multiple corroborating signals.",
        severity=severity,
        channel_name=channel,
        target_id=target_id,
        source_links=("https://news.example.org/a",),
        effective_confidence=confidence,
        verify_state="faithfulness=0.83",
        alert_row_id=alert_row_id,
        receipt_path="/api/v1/lineage/finding/7e0b0f37-0000-0000-0000-000000000000",
        receipt_url=receipt_url,
    )


def _sink(http: _Http, **kw: Any) -> NtfyAlertSink:
    kw.setdefault("url", SECRET_TOPIC_URL)
    return NtfyAlertSink(http=http, sleep=_noop_sleep, **kw)


# ---------------------------------------------------------------------------
# Registration + config gates
# ---------------------------------------------------------------------------


async def test_ntfy_is_registered_by_package_import() -> None:
    assert "ntfy" in registered_sink_kinds()


async def test_unconfigured_sink_skips_without_posting() -> None:
    http = _Http([])
    sink = NtfyAlertSink(url="", http=http, sleep=_noop_sleep)
    assert sink.configured is False
    result = await sink.deliver(_payload())
    assert result.outcome == "skipped_unconfigured"
    assert "not configured" in result.detail
    assert http.calls == []


async def test_unconfigured_dispatcher_writes_visible_skipped_row() -> None:
    """DECLARED-INACTIVE is never a silent drop: the dispatcher records
    status='skipped_unconfigured' for the ntfy sink, like the webhook."""
    http = _Http()
    pool = _RecordingPool()
    d = AlertSinkDispatcher(
        pg_pool=pool, sinks=[NtfyAlertSink(url="", http=http, sleep=_noop_sleep)]
    )
    oid = uuid4()
    results = await d.fan_out(_payload(alert_row_id=str(oid)))

    assert [r.outcome for r in results] == ["skipped_unconfigured"]
    assert http.calls == []
    rows = pool.ntfy_rows()
    assert len(rows) == 1
    args = rows[0]
    assert args[0] == oid                      # alert_row_id
    assert args[2] == "ntfy"                   # sink_kind
    assert args[3] is None                     # sink_target — nothing configured
    assert args[8] == "skipped_unconfigured"   # status
    assert "not configured" in args[9]         # error_message names the gap


async def test_below_min_severity_skips_without_posting() -> None:
    http = _Http([])
    sink = _sink(http)  # default floor: high (mirrors webhook)
    result = await sink.deliver(_payload(severity="medium"))
    assert result.outcome == "skipped_below_severity"
    assert http.calls == []


async def test_min_severity_floor_is_configurable() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http, min_severity="low")
    result = await sink.deliver(_payload(severity="medium"))
    assert result.delivered


async def test_from_env_reads_url_token_and_floor(monkeypatch: Any) -> None:
    monkeypatch.setenv(ENV_NTFY_URL, "http://127.0.0.1:8093/legba-alerts")
    monkeypatch.setenv(ENV_NTFY_TOKEN, "tk_secret")
    monkeypatch.setenv(ENV_NTFY_MIN_SEVERITY, "medium")
    sink = NtfyAlertSink.from_env()
    assert sink.configured
    assert sink.target_summary() == "127.0.0.1"
    assert sink.accepts_severity("medium") and not sink.accepts_severity("low")


async def test_from_env_unset_is_declared_inactive(monkeypatch: Any) -> None:
    monkeypatch.delenv(ENV_NTFY_URL, raising=False)
    sink = NtfyAlertSink.from_env()
    assert sink.configured is False


# ---------------------------------------------------------------------------
# Native publish shape — plaintext body + ntfy headers
# ---------------------------------------------------------------------------


async def test_2xx_publishes_native_ntfy_message() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.delivered and result.attempts == 1 and result.http_status == 200
    assert len(http.calls) == 1
    call = http.calls[0]

    # Body: readable plaintext, NOT a JSON blob.
    body = call["content"].decode("utf-8")
    assert not body.lstrip().startswith("{")
    lines = body.split("\n")
    assert lines[0] == "Coup-risk spike"
    assert lines[1] == "high · ua · confidence 0.91 · faithfulness=0.83"
    assert lines[2] == RECEIPT_URL             # receipt link on its own line

    # ntfy headers.
    h = call["headers"]
    assert h["Content-Type"].startswith("text/plain")
    assert h["X-Title"] == "Legba: high - ua"
    assert h["X-Priority"] == "4"
    assert h["X-Tags"] == "warning,escalations"
    assert h["X-Click"] == RECEIPT_URL
    assert "Authorization" not in h            # no token configured


@pytest.mark.parametrize(
    ("severity", "priority"),
    [("critical", "5"), ("high", "4"), ("medium", "3"), ("low", "2"), ("info", "2")],
)
async def test_priority_maps_from_severity(severity: str, priority: str) -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http, min_severity="info")
    result = await sink.deliver(_payload(severity=severity))
    assert result.delivered
    assert http.calls[0]["headers"]["X-Priority"] == priority


async def test_tags_are_severity_shortcode_plus_channel() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    await sink.deliver(_payload(severity="critical", channel="band_crossing"))
    assert http.calls[0]["headers"]["X-Tags"] == "rotating_light,band_crossing"


async def test_channel_tag_is_sanitised() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    await sink.deliver(_payload(channel="Weird, Channel!"))
    assert http.calls[0]["headers"]["X-Tags"] == "warning,weird_channel"


async def test_no_receipt_url_no_click_header_relative_path_in_body() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    await sink.deliver(_payload(receipt_url=None))
    call = http.calls[0]
    assert "X-Click" not in call["headers"]    # relative path is no tap target
    lines = call["content"].decode("utf-8").split("\n")
    assert lines[-1].startswith("/api/v1/lineage/finding/")


async def test_bearer_token_header_when_configured() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http, token="tk_secret")
    await sink.deliver(_payload())
    assert http.calls[0]["headers"]["Authorization"] == "Bearer tk_secret"


async def test_absent_pieces_are_omitted_from_meta_line() -> None:
    body = ntfy_message(
        _payload(target_id=None, confidence=None, receipt_url=None)
    )
    lines = body.split("\n")
    assert lines[1] == "high · faithfulness=0.83"


async def test_headers_stay_ascii_on_unicode_payload() -> None:
    """Non-latin-1 header values would fail inside the HTTP client — the
    title strips them; the UTF-8 body keeps the full content."""
    http = _Http([_Resp(200)])
    sink = _sink(http)
    await sink.deliver(_payload(target_id="воронеж"))
    title = http.calls[0]["headers"]["X-Title"]
    assert all(32 <= ord(c) < 127 for c in title)
    assert title.startswith("Legba: high")


# ---------------------------------------------------------------------------
# Redaction — the topic path is the secret
# ---------------------------------------------------------------------------


async def test_result_target_is_host_only_never_topic_path() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.target == "ntfy.example.com"
    assert "legba-secret-topic" not in result.target
    assert "legba-secret-topic" not in result.detail


async def test_ledger_row_redacts_topic_path() -> None:
    http = _Http([_Resp(200)])
    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    results = await d.fan_out(_payload(alert_row_id=str(uuid4())))

    assert results[0].delivered
    args = pool.ntfy_rows()[0]
    assert args[3] == "ntfy.example.com"       # host ONLY
    assert args[8] == "delivered"
    # The topic secret never reaches the ledger in ANY column.
    assert all("legba-secret-topic" not in str(a) for a in args)


# ---------------------------------------------------------------------------
# Retry behaviour (the webhook pattern, verbatim)
# ---------------------------------------------------------------------------


async def test_4xx_is_permanent_no_retry() -> None:
    http = _Http([_Resp(403, text="forbidden")])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.outcome == "permanent_error"
    assert result.http_status == 403 and result.attempts == 1
    assert len(http.calls) == 1


async def test_5xx_retries_then_succeeds() -> None:
    http = _Http([_Resp(503), _Resp(200)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.delivered and result.attempts == 2
    assert len(http.calls) == 2


async def test_transient_exhaustion_degrades_never_raises() -> None:
    http = _Http([_Resp(503), _Resp(502), _Resp(500)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.outcome == "transient_error"
    # 2 retries → 3 attempts total (the P1-1 bounded-retry contract).
    assert result.attempts == 3 and len(http.calls) == 3
    assert "http 500" in result.detail


async def test_network_error_is_transient_and_retried() -> None:
    http = _Http([httpx.ConnectError("boom"), _Resp(200)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.delivered and result.attempts == 2


async def test_unexpected_exception_is_classified_not_raised() -> None:
    http = _Http([ValueError("programmer error in transport")])
    sink = _sink(http)
    result = await sink.deliver(_payload())  # must NOT raise
    assert result.outcome == "permanent_error"
    assert "ValueError" in result.detail


# ---------------------------------------------------------------------------
# Real wire — local HTTP server, real httpx client
# ---------------------------------------------------------------------------


class _NtfyHandler(BaseHTTPRequestHandler):
    received: list[dict[str, Any]] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).received.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": self.rfile.read(length),
            }
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):  # silence
        return


@pytest.fixture()
def ntfy_topic_url():
    _NtfyHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _NtfyHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/legba-alerts"
    server.shutdown()


async def test_real_wire_native_publish(ntfy_topic_url: str) -> None:
    """End-to-end through a REAL httpx client: the ntfy headers and the
    plaintext body arrive on the wire exactly as mapped."""
    sink = NtfyAlertSink(url=ntfy_topic_url, token="tk_wire")
    try:
        result = await sink.deliver(_payload())
        assert result.delivered and result.http_status == 200
        assert result.target == "127.0.0.1"

        req = _NtfyHandler.received[0]
        assert req["path"] == "/legba-alerts"
        assert req["headers"]["x-title"] == "Legba: high - ua"
        assert req["headers"]["x-priority"] == "4"
        assert req["headers"]["x-tags"] == "warning,escalations"
        assert req["headers"]["x-click"] == RECEIPT_URL
        assert req["headers"]["authorization"] == "Bearer tk_wire"
        body = req["body"].decode("utf-8")
        assert body.split("\n")[0] == "Coup-risk spike"
        assert RECEIPT_URL in body
    finally:
        if sink._http is not None:
            await sink._http.aclose()
