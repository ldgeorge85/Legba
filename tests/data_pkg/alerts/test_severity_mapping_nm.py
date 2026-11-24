# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-M (11-13) — the unit-severity → dispatcher-severity table, end to end.

Each unit word goes through the escalation edge (``payload_for_finding``) and
the dispatcher into an ntfy sink with the deployed floor (``medium``). The
table pins: the mapped severity, whether it pushed, the ntfy priority, and
that EVERY case wrote one ``alert_sink_deliveries`` row with a status (before
N-M, ``moderate`` / ``elevated`` folded to ``info`` and wrote no row at all).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest

from legba.data.alerts import AlertSinkDispatcher
from legba.data.alerts.ntfy_sink import NtfyAlertSink
from legba.data.alerts.sinks import (
    SEVERITY_ALIASES,
    UNKNOWN_SEVERITY_DEFAULT,
    map_alert_severity,
    normalise_severity,
    runtime_alert_payload,
)

_URL = "http://127.0.0.1:8093/legba-alerts"


class _Resp:
    status_code = 200
    text = ""


class _Http:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return _Resp()


class _Pool:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        if "INSERT INTO alert_sink_deliveries" in sql:
            self.rows.append(args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None  # transition unmeasured → not level-only

    async def fetch(self, sql: str, *args: Any) -> Any:
        return []


async def _noop_sleep(_s: float) -> None:
    return None


#: unit word → (dispatcher severity, pushed under floor=medium, ntfy priority)
TABLE = [
    ("info", "info", False, None),
    ("low", "low", False, None),
    ("moderate", "medium", True, "3"),
    ("medium", "medium", True, "3"),
    ("elevated", "high", True, "4"),
    ("high", "high", True, "4"),
    ("critical", "critical", True, "5"),
    ("ELEVATED ", "high", True, "4"),
    ("bogus", UNKNOWN_SEVERITY_DEFAULT, True, "3"),
    (None, UNKNOWN_SEVERITY_DEFAULT, True, "3"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("unit_word,mapped,pushed,priority", TABLE)
async def test_unit_severity_table(unit_word, mapped, pushed, priority) -> None:
    http = _Http()
    pool = _Pool()
    sink = NtfyAlertSink(url=_URL, min_severity="medium", http=http, sleep=_noop_sleep)
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[sink])
    payload = await d.payload_for_finding(
        channel_name="escalations",
        alert_row_id=str(uuid4()),
        target_id="ua",
        severity=unit_word,
        effective_confidence=0.8,
        title="t",
    )
    assert payload.severity == mapped
    results = await d.fan_out(payload)

    assert (len(http.calls) == 1) is pushed
    assert len(pool.rows) == 1, "every dispatch decision writes exactly one row"
    row = pool.rows[0]
    assert row[2] == "ntfy" and row[5] == mapped
    if pushed:
        assert results[0].outcome == "delivered" and row[8] == "delivered"
        assert http.calls[0]["headers"]["X-Priority"] == priority
    else:
        assert results[0].outcome == "skipped_below_severity"
        assert row[8] == "skipped_below_severity"
        assert "below" in (row[9] or "")


def test_aliases_cover_the_units_extra_words() -> None:
    assert SEVERITY_ALIASES == {"moderate": "medium", "elevated": "high"}
    assert normalise_severity("moderate") == "medium"
    assert normalise_severity("elevated") == "high"


def test_unknown_severity_is_logged_and_defined(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="legba.data.alerts.sinks"):
        assert map_alert_severity("severe", context="x") == "medium"
    assert "severity_unknown" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="legba.data.alerts.sinks"):
        assert map_alert_severity("elevated") == "high"
    assert "severity_unknown" not in caplog.text


def test_runtime_alert_unknown_defaults_high() -> None:
    assert runtime_alert_payload(
        channel_name="global_stall", summary="s", severity="??"
    ).severity == "high"


@pytest.mark.asyncio
async def test_duplicate_writes_a_row_too() -> None:
    http = _Http()
    pool = _Pool()
    sink = NtfyAlertSink(url=_URL, min_severity="medium", http=http, sleep=_noop_sleep)
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[sink])
    oid = str(uuid4())
    for _ in range(2):
        await d.fan_out(await d.payload_for_finding(
            channel_name="escalations", alert_row_id=oid, target_id="ua",
            severity="elevated", effective_confidence=0.8, title="t",
        ))
    assert [r[8] for r in pool.rows] == ["delivered", "skipped_duplicate"]
    assert "duplicate" in pool.rows[1][9]


def test_since_api_ranks_unit_words() -> None:
    """since_api's sort used to put elevated/moderate at ELSE 0 (with info)."""
    from legba.data.registry.since_api import _SEVERITY_RANK_SQL

    sql = _SEVERITY_RANK_SQL.format(col="s")
    rank = {}
    for word in ("critical", "high", "elevated", "medium", "moderate", "low"):
        rank[word] = int(sql.split(f"WHEN '{word}' THEN ")[1].split()[0])
    assert rank["critical"] > rank["high"] > rank["elevated"] > rank["medium"]
    assert rank["medium"] == rank["moderate"] > rank["low"] > 0
