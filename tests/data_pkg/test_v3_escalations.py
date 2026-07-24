# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the human-visible escalation-delivery route on the v3 API.

Covers the read route added to :mod:`legba.data.registry.v3_api` for audit
finding C3 / decision D1:

  * ``GET /api/v1/v3/system/escalations`` -> ``EscalationDeliveriesResponse``

``build_v3_router`` only touches ``deps`` lazily inside the async handler, so
the route registers against a trivial stub and its path is introspected without
a live substrate (the ``test_v3_system_status`` precedent). The LOAD-BEARING
contract — the shaping of raw ``alert_sink_deliveries`` rows + the 24h
canary-aligned non-delivery summary — is exercised directly on the pure reducer
``_build_escalations_response`` with real inputs, so "real rows in → correct
shape/order/summary out; empty → honest empty" is asserted without a DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from legba.data.registry.v3_api import (
    EscalationDeliveriesResponse,
    EscalationDeliveryRow,
    _build_escalations_response,
    _escalation_delivery_row,
    build_v3_router,
)


def test_escalations_route_registered() -> None:
    """The route registers under the /api/v1/v3 mount prefix the panel polls."""
    router = build_v3_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/system/escalations" in paths


def test_empty_inputs_are_honest_empty_not_fabricated() -> None:
    """No rows → a zeroed summary + no rows: a first-class 'nothing escalated'
    state, never fabricated activity."""
    resp = _build_escalations_response([], [], window_hours=24)
    assert isinstance(resp, EscalationDeliveriesResponse)
    assert resp.rows == []
    s = resp.summary
    assert s.window_hours == 24
    assert (s.total, s.delivered, s.failed, s.logged_only, s.retrying, s.other) == (
        0, 0, 0, 0, 0, 0,
    )
    assert s.non_delivery == 0
    assert s.by_sink_status == []


def test_row_shaping_maps_every_column_and_parses_jsonb_summary() -> None:
    """A raw audit row maps 1:1 into the model, UUIDs stringify, and a JSONB
    payload_summary handed back as a raw string is parsed to a dict."""
    now = datetime.now(timezone.utc)
    oid = uuid4()
    rid = uuid4()
    row = {
        "id": rid,
        "alert_row_id": oid,
        "channel_name": "escalations",
        "sink_kind": "alert",
        "sink_target": "channels.escalations",
        "target_id": "us",
        "severity": "high",
        "effective_confidence": 0.91,
        "status": "delivered",
        "error_message": None,
        "attempt_number": 1,
        "attempted_at": now,
        "delivered_at": now,
        # asyncpg may hand JSONB back as a raw string — must be parsed.
        "payload_summary": '{"action":"escalate","delivered":true}',
    }
    shaped = _escalation_delivery_row(row)
    assert isinstance(shaped, EscalationDeliveryRow)
    assert shaped.id == str(rid)
    assert shaped.alert_row_id == str(oid)
    assert shaped.channel_name == "escalations"
    assert shaped.sink_kind == "alert"
    assert shaped.sink_target == "channels.escalations"
    assert shaped.target_id == "us"
    assert shaped.severity == "high"
    assert shaped.effective_confidence == 0.91
    assert shaped.status == "delivered"
    assert shaped.delivered_at == now
    assert shaped.payload_summary == {"action": "escalate", "delivered": True}


def test_legacy_row_with_null_honesty_columns_shapes_cleanly() -> None:
    """A legacy alert-output-kind row (NULL channel/target/severity/conf, dict
    payload_summary) shapes without inventing values."""
    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(),
        "alert_row_id": None,
        "channel_name": None,
        "sink_kind": "webhook",
        "sink_target": None,
        "target_id": None,
        "severity": None,
        "effective_confidence": None,
        "status": "logged_only",
        "error_message": None,
        "attempt_number": 1,
        "attempted_at": now,
        "delivered_at": None,
        "payload_summary": {},
    }
    shaped = _escalation_delivery_row(row)
    assert shaped.alert_row_id is None
    assert shaped.channel_name is None
    assert shaped.effective_confidence is None
    assert shaped.delivered_at is None
    assert shaped.payload_summary == {}


def test_summary_counts_and_non_delivery_breakdown_match_the_canary() -> None:
    """The 24h summary tallies each status and the non_delivery count (failed +
    logged_only) EXACTLY as the W1-T3 canary, with a worst-first breakdown that
    carries the sample error."""
    # GROUP BY status, sink_kind tallies over the window.
    summary_rows = [
        {"status": "delivered", "sink_kind": "alert", "n": 40, "sample_err": None},
        {"status": "failed", "sink_kind": "pushover", "n": 552,
         "sample_err": "pushover 552: monthly limit"},
        {"status": "logged_only", "sink_kind": "webhook", "n": 3, "sample_err": None},
        {"status": "retrying", "sink_kind": "alert", "n": 2, "sample_err": None},
    ]
    resp = _build_escalations_response([], summary_rows, window_hours=24)
    s = resp.summary
    assert s.total == 40 + 552 + 3 + 2
    assert s.delivered == 40
    assert s.failed == 552
    assert s.logged_only == 3
    assert s.retrying == 2
    assert s.other == 0
    # The canary signal: failed + logged_only (retrying is in-flight, excluded).
    assert s.non_delivery == 555
    # Breakdown worst-first: hard 'failed' (552) before 'logged_only' (3).
    assert [(b.status, b.sink_kind, b.n) for b in s.by_sink_status] == [
        ("failed", "pushover", 552),
        ("logged_only", "webhook", 3),
    ]
    assert s.by_sink_status[0].sample_error == "pushover 552: monthly limit"


def test_rows_preserve_sql_newest_first_order() -> None:
    """The reducer preserves the input row order (SQL orders newest-first by
    attempted_at); a later attempt must lead."""
    t2 = datetime.now(timezone.utc)
    t1 = t2 - timedelta(hours=2)

    def _row(status: str, ts: datetime) -> dict:
        return {
            "id": uuid4(), "alert_row_id": None, "channel_name": "escalations",
            "sink_kind": "alert", "sink_target": "channels.escalations",
            "target_id": "us", "severity": "high", "effective_confidence": None,
            "status": status, "error_message": None, "attempt_number": 1,
            "attempted_at": ts, "delivered_at": ts if status == "delivered" else None,
            "payload_summary": {},
        }

    # As SQL would return them: newest (t2) first.
    delivery_rows = [_row("failed", t2), _row("delivered", t1)]
    resp = _build_escalations_response(delivery_rows, [], window_hours=24)
    assert [r.status for r in resp.rows] == ["failed", "delivered"]
    assert resp.rows[0].attempted_at == t2
