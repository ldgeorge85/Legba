# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-1 webhook alert sink — bounded-retry POST, gating, redaction.

Same fake-HTTP pattern as ``tests/data_pkg/test_output_taxii_client.py``
(queued responses / raised exceptions, recorded calls) — the sink's HTTP
port is structural, so a recording fake exercises the production
classification + retry code end-to-end.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from legba.data.alerts import WebhookAlertSink
from legba.data.alerts.sinks import AlertSinkPayload
from legba.data.alerts.webhook_sink import (
    ENV_WEBHOOK_MIN_SEVERITY,
    ENV_WEBHOOK_URL,
)

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fakes (mirrors the taxii-client test fakes)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Http:
    """Returns a queued sequence of responses (or raises queued exceptions)."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
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


async def _noop_sleep(_seconds: float) -> None:
    return None


def _payload(severity: str = "high") -> AlertSinkPayload:
    return AlertSinkPayload(
        summary="Coup-risk spike",
        detail="Multiple corroborating signals.",
        severity=severity,
        channel_name="escalations",
        target_id="ua",
        source_links=("https://news.example.org/a",),
        effective_confidence=0.91,
        verify_state="faithfulness=0.83",
        alert_row_id="7e0b0f37-0000-0000-0000-000000000000",
        receipt_path="/api/v1/lineage/finding/7e0b0f37-0000-0000-0000-000000000000",
    )


def _sink(http: _Http, **kw: Any) -> WebhookAlertSink:
    kw.setdefault("url", "https://hooks.example.com/T123/secret-token")
    return WebhookAlertSink(http=http, sleep=_noop_sleep, **kw)


# ---------------------------------------------------------------------------
# Config gates
# ---------------------------------------------------------------------------


async def test_unconfigured_sink_skips_without_posting() -> None:
    http = _Http([])
    sink = WebhookAlertSink(url="", http=http, sleep=_noop_sleep)
    assert sink.configured is False
    result = await sink.deliver(_payload())
    assert result.outcome == "skipped_unconfigured"
    assert "not configured" in result.detail
    assert http.calls == []


async def test_below_min_severity_skips_without_posting() -> None:
    http = _Http([])
    sink = _sink(http)  # default floor: high
    result = await sink.deliver(_payload(severity="medium"))
    assert result.outcome == "skipped_below_severity"
    assert http.calls == []


async def test_min_severity_floor_is_configurable() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http, min_severity="low")
    result = await sink.deliver(_payload(severity="medium"))
    assert result.delivered


async def test_from_env_reads_url_and_floor(monkeypatch: Any) -> None:
    monkeypatch.setenv(ENV_WEBHOOK_URL, "https://hooks.example.com/x/y")
    monkeypatch.setenv(ENV_WEBHOOK_MIN_SEVERITY, "medium")
    sink = WebhookAlertSink.from_env()
    assert sink.configured
    assert sink.target_summary() == "hooks.example.com"
    assert sink.accepts_severity("medium") and not sink.accepts_severity("low")


async def test_from_env_unset_is_declared_inactive(monkeypatch: Any) -> None:
    monkeypatch.delenv(ENV_WEBHOOK_URL, raising=False)
    sink = WebhookAlertSink.from_env()
    assert sink.configured is False


# ---------------------------------------------------------------------------
# POST behaviour
# ---------------------------------------------------------------------------


async def test_2xx_delivers_payload_json_once() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.delivered and result.attempts == 1 and result.http_status == 200
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["headers"]["Content-Type"] == "application/json"
    body = json.loads(call["content"])
    # The converged anatomy is on the wire.
    assert body["summary"] == "Coup-risk spike"
    assert body["severity"] == "high"
    assert body["verify_state"] == "faithfulness=0.83"
    assert body["effective_confidence"] == pytest.approx(0.91)
    assert body["source_links"] == ["https://news.example.org/a"]
    assert body["receipt_path"].startswith("/api/v1/lineage/finding/")
    assert "detected_at" in body


async def test_result_target_is_host_only_never_full_url() -> None:
    http = _Http([_Resp(200)])
    sink = _sink(http)
    result = await sink.deliver(_payload())
    assert result.target == "hooks.example.com"
    assert "secret-token" not in result.target
    assert "secret-token" not in result.detail


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
