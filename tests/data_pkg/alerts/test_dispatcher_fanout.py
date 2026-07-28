# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-1 dispatcher fan-out — ledger rows, idempotency, cooldown, wiring.

The dispatcher is the shared machinery every sink rides: one durable
``alert_sink_deliveries`` row per sink outcome (delivered / failed /
skipped_unconfigured / skipped_cooldown), host-only target redaction,
per-alert-row idempotency, and the global per-sink cooldown. Fake
duck-typed pool + fake HTTP (the established seams — mirrors
``test_channel_delivery_audit.py`` / ``test_output_taxii_client.py``).

Also covers the two LIVE wiring edges end-to-end at unit level:
the agency :class:`ChannelEmitter` (escalation emit → internal audit row +
webhook fan-out) and the liveness watchdog's global-stall alert.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.alerts import AlertSinkDispatcher, WebhookAlertSink
from legba.data.alerts.sinks import runtime_alert_payload
from legba.data.analysts.agency.tools import ChannelEmitter
from legba.data.schemas.action_pack import Channel

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Http:
    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls: list[dict[str, Any]] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append({"url": url, "content": content})
        out = self._outcomes.pop(0) if self._outcomes else _Resp(200)
        if isinstance(out, BaseException):
            raise out
        return out


class _RecordingPool:
    """Duck-typed pool — records execute() INSERTs; enrichment reads no-op."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def fetch(self, sql: str, *args: Any) -> Any:
        return []

    def webhook_rows(self) -> list[tuple[Any, ...]]:
        return [
            args for sql, args in self.calls
            if "INSERT INTO alert_sink_deliveries" in sql and args[2] == "webhook"
        ]


async def _noop_sleep(_seconds: float) -> None:
    return None


SECRET_URL = "https://hooks.example.com/T123/secret-token"


def _sink(http: _Http, *, url: str = SECRET_URL, **kw: Any) -> WebhookAlertSink:
    return WebhookAlertSink(url=url, http=http, sleep=_noop_sleep, **kw)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


async def _finding_payload(
    dispatcher: AlertSinkDispatcher, *, alert_row_id: Any, severity: str = "high"
):
    return await dispatcher.payload_for_finding(
        channel_name="escalations",
        alert_row_id=alert_row_id,
        target_id="ua",
        severity=severity,
        effective_confidence=0.91,
        title="Coup-risk spike",
        detail="Multiple corroborating signals.",
        faithfulness_score=0.83,
    )


# ---------------------------------------------------------------------------
# Ledger rows
# ---------------------------------------------------------------------------


async def test_unconfigured_sink_writes_visible_skipped_row() -> None:
    """DECLARED-INACTIVE is never a silent drop: the ledger records
    status='skipped_unconfigured' for every alert that had nowhere to go."""
    http = _Http()
    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http, url="")])
    oid = uuid4()
    results = await d.fan_out(await _finding_payload(d, alert_row_id=str(oid)))

    assert [r.outcome for r in results] == ["skipped_unconfigured"]
    assert http.calls == []
    rows = pool.webhook_rows()
    assert len(rows) == 1
    args = rows[0]
    assert args[0] == oid                      # alert_row_id
    assert args[1] == "escalations"            # channel_name
    assert args[2] == "webhook"                # sink_kind
    assert args[3] is None                     # sink_target — nothing configured
    assert args[8] == "skipped_unconfigured"   # status
    assert "not configured" in args[9]         # error_message names the gap


async def test_delivered_row_carries_redacted_target_and_anatomy() -> None:
    http = _Http([_Resp(200)])
    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    oid = uuid4()
    results = await d.fan_out(await _finding_payload(d, alert_row_id=str(oid)))

    assert results[0].delivered
    rows = pool.webhook_rows()
    assert len(rows) == 1
    args = rows[0]
    assert args[0] == oid
    assert args[3] == "hooks.example.com"      # host ONLY — secret redacted
    assert args[4] == "ua"                     # target_id
    assert args[5] == "high"                   # severity
    assert args[6] == pytest.approx(0.91)      # effective_confidence
    assert args[8] == "delivered"
    assert args[9] is None                     # no error
    assert args[10] is not None                # delivered_at stamped
    summary = json.loads(args[11])
    assert summary["verify_state"] == "faithfulness=0.83"
    assert summary["receipt_path"] == f"/api/v1/lineage/finding/{oid}"
    # The secret never reaches the ledger in ANY column.
    assert all("secret-token" not in str(a) for a in args)


async def test_failed_post_writes_failed_row_with_error() -> None:
    http = _Http([_Resp(500), _Resp(500), _Resp(500)])
    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    results = await d.fan_out(
        await _finding_payload(d, alert_row_id=str(uuid4()))
    )
    assert results[0].outcome == "transient_error"
    args = pool.webhook_rows()[0]
    assert args[7] == 3                        # attempt_number = attempts made
    assert args[8] == "failed"
    assert "http 500" in args[9]
    assert args[10] is None                    # no delivered_at


async def test_below_severity_writes_no_row() -> None:
    """A configured min-severity filter working as designed is not a
    delivery event — no ledger noise."""
    http = _Http()
    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])  # floor: high
    results = await d.fan_out(
        await _finding_payload(d, alert_row_id=str(uuid4()), severity="medium")
    )
    assert results[0].outcome == "skipped_below_severity"
    assert pool.webhook_rows() == [] and http.calls == []


# ---------------------------------------------------------------------------
# Anti-noise: idempotency + cooldown
# ---------------------------------------------------------------------------


async def test_one_attempt_series_per_alert_row_id() -> None:
    """A finding escalated on two channels reaches fan_out twice — ONE POST,
    ONE ledger row; the duplicate is suppressed without a row."""
    http = _Http([_Resp(200)])
    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    oid = str(uuid4())

    first = await d.fan_out(await _finding_payload(d, alert_row_id=oid))
    second = await d.fan_out(await _finding_payload(d, alert_row_id=oid))

    assert first[0].delivered
    assert second[0].outcome == "skipped_duplicate"
    assert len(http.calls) == 1
    assert len(pool.webhook_rows()) == 1


async def test_global_cooldown_suppresses_and_audits() -> None:
    clock = _Clock()
    http = _Http([_Resp(200), _Resp(200)])
    pool = _RecordingPool()
    d = AlertSinkDispatcher(
        pg_pool=pool, sinks=[_sink(http)], cooldown_seconds=60.0,
        monotonic=clock,
    )

    a = await d.fan_out(await _finding_payload(d, alert_row_id=str(uuid4())))
    clock.now += 10.0  # inside the window
    b = await d.fan_out(await _finding_payload(d, alert_row_id=str(uuid4())))
    clock.now += 120.0  # window elapsed
    c = await d.fan_out(await _finding_payload(d, alert_row_id=str(uuid4())))

    assert a[0].delivered
    assert b[0].outcome == "skipped_cooldown"
    assert c[0].delivered
    assert len(http.calls) == 2
    statuses = [args[8] for args in pool.webhook_rows()]
    assert statuses == ["delivered", "skipped_cooldown", "delivered"]


async def test_runtime_alert_without_row_id_is_not_deduplicated() -> None:
    """Watchdog-class alerts carry no alert_row_id — the cooldown (not the
    idempotency set) is their anti-noise gate."""
    clock = _Clock()
    http = _Http([_Resp(200), _Resp(200)])
    pool = _RecordingPool()
    d = AlertSinkDispatcher(
        pg_pool=pool, sinks=[_sink(http)], monotonic=clock,
    )
    p = runtime_alert_payload(
        channel_name="liveness_stall", summary="stall", severity="high",
    )
    first = await d.fan_out(p)
    clock.now += 3600.0
    second = await d.fan_out(p)
    assert first[0].delivered and second[0].delivered
    assert len(http.calls) == 2


async def test_misbehaving_sink_never_kills_fan_out() -> None:
    class _RaisingSink:
        sink_kind = "webhook"
        configured = True

        def target_summary(self) -> str:
            return "hooks.example.com"

        def accepts_severity(self, severity: str) -> bool:
            return True

        async def deliver(self, payload: Any) -> Any:
            raise RuntimeError("sink contract violation")

    pool = _RecordingPool()
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_RaisingSink()])
    results = await d.fan_out(
        await _finding_payload(d, alert_row_id=str(uuid4()))
    )
    assert results[0].outcome == "permanent_error"
    assert pool.webhook_rows()[0][8] == "failed"


# ---------------------------------------------------------------------------
# Wiring edge 1 — ChannelEmitter (escalation emit)
# ---------------------------------------------------------------------------


def _escalation_channel() -> Channel:
    return Channel(
        name="escalations", kind="alert",
        config={"subject": "channels.escalations"},
    )


def _escalation_payload(output_id: UUID) -> dict[str, Any]:
    return {
        "action": "escalate",
        "severity": "high",
        "title": "Coup-risk spike in country X",
        "detail": "Multiple corroborating signals.",
        "target_ref": f"analyst_outputs:{output_id}",
        "requested_by": "escalate_finding",
        "output_id": str(output_id),
        "target_id": "ua",
        "effective_confidence": 0.91,
        "faithfulness_score": 0.83,
    }


async def test_channel_emitter_fans_out_internal_row_plus_webhook() -> None:
    async def _ok_publish(subject: str, payload: bytes) -> None:
        return None

    http = _Http([_Resp(200)])
    pool = _RecordingPool()
    dispatcher = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    emitter = ChannelEmitter(
        nats_publish=_ok_publish, pg_pool=pool, alert_sinks=dispatcher,
    )
    oid = uuid4()

    record = await emitter.emit(_escalation_channel(), _escalation_payload(oid))

    assert record["delivered"] is True
    # TWO ledger rows: the internal channel audit + the outward webhook row.
    inserts = [args for sql, args in pool.calls if "INSERT INTO" in sql]
    assert len(inserts) == 2
    kinds = sorted(args[2] for args in inserts)
    assert kinds == ["alert", "webhook"]
    # The webhook POST body carries the verify state + receipt link.
    body = json.loads(http.calls[0]["content"])
    assert body["verify_state"] == "faithfulness=0.83"
    assert body["receipt_path"] == f"/api/v1/lineage/finding/{oid}"
    assert body["channel_name"] == "escalations"


async def test_channel_emitter_without_dispatcher_is_unchanged() -> None:
    async def _ok_publish(subject: str, payload: bytes) -> None:
        return None

    pool = _RecordingPool()
    emitter = ChannelEmitter(nats_publish=_ok_publish, pg_pool=pool)
    record = await emitter.emit(
        _escalation_channel(), _escalation_payload(uuid4())
    )
    assert record["delivered"] is True
    assert len(pool.calls) == 1  # only the pre-existing internal audit row


async def test_channel_emitter_fan_out_survives_failed_internal_publish() -> None:
    """The outward page must not depend on the internal bus being healthy."""

    async def _raising_publish(subject: str, payload: bytes) -> None:
        raise RuntimeError("no stream responds")

    http = _Http([_Resp(200)])
    pool = _RecordingPool()
    dispatcher = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    emitter = ChannelEmitter(
        nats_publish=_raising_publish, pg_pool=pool, alert_sinks=dispatcher,
    )
    record = await emitter.emit(
        _escalation_channel(), _escalation_payload(uuid4())
    )
    assert record["delivered"] is False
    assert len(http.calls) == 1  # webhook still fired


# ---------------------------------------------------------------------------
# Wiring edge 2 — liveness watchdog global-stall alert
# ---------------------------------------------------------------------------


async def test_watchdog_stall_alert_fans_out_through_webhook() -> None:
    from legba.runtime.liveness_watchdog import LivenessWatchdog

    class _FakeNats:
        def __init__(self) -> None:
            self.published: list[tuple[str, bytes]] = []

        async def publish_core(self, subject: str, body: bytes) -> None:
            self.published.append((subject, body))

    http = _Http([_Resp(200)])
    pool = _RecordingPool()
    dispatcher = AlertSinkDispatcher(pg_pool=pool, sinks=[_sink(http)])
    nats = _FakeNats()
    watchdog = LivenessWatchdog(nats, alert_sinks=dispatcher)

    await watchdog._emit_stall_alert(idle_s=1200.0)

    # Internal NATS alert still published (pre-existing behaviour) ...
    assert len(nats.published) == 1
    # ... AND the outward webhook fired with the runtime-alert anatomy.
    assert len(http.calls) == 1
    body = json.loads(http.calls[0]["content"])
    assert body["channel_name"] == "liveness_stall"
    assert body["severity"] == "high"
    assert body["verify_state"].startswith("unverified — ")
    rows = pool.webhook_rows()
    assert len(rows) == 1 and rows[0][8] == "delivered"
    assert rows[0][1] == "liveness_stall"
