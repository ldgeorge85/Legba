# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-V — the event-loop lag monitor names whatever holds the loop."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from legba.runtime import loop_lag_monitor as llm


def _hog_the_loop_synchronously(seconds: float) -> None:
    time.sleep(seconds)   # stands in for a CPU-bound pure-Python walk


@pytest.mark.asyncio
async def test_a_blocked_loop_is_reported_with_the_task_and_frame(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=llm.__name__)
    monitor = await llm.LoopLagMonitor(
        threshold_seconds=0.2, tick_seconds=0.05
    ).start()
    try:
        await asyncio.sleep(0.15)

        async def reconciler_pass() -> None:
            _hog_the_loop_synchronously(0.7)

        await asyncio.create_task(reconciler_pass(), name="analyst::event_reconciler")
        await asyncio.sleep(0.3)   # let the watchdog see the recovery
    finally:
        await monitor.stop()
    assert len(monitor.stalls) == 1, monitor.stalls
    stall = monitor.stalls[0]
    assert stall["task"] == "analyst::event_reconciler"
    assert "reconciler_pass" in stall["coro"]
    assert "_hog_the_loop_synchronously" in stall["where"]
    assert 0.5 <= stall["blocked_seconds"] <= 1.5
    assert monitor.max_lag_seconds >= 0.5
    text = caplog.text
    assert "loop_lag.blocked" in text and "analyst::event_reconciler" in text
    assert "loop_lag.recovered" in text


@pytest.mark.asyncio
async def test_an_idle_or_awaiting_loop_reports_nothing() -> None:
    monitor = await llm.LoopLagMonitor(
        threshold_seconds=0.2, tick_seconds=0.05
    ).start()
    try:
        for _ in range(10):
            await asyncio.sleep(0.03)
            _hog_the_loop_synchronously(0.02)   # short, normal slices
    finally:
        await monitor.stop()
        await monitor.stop()   # idempotent
    assert monitor.stalls == []
    assert monitor.max_lag_seconds < 0.2


def test_threshold_from_env(monkeypatch) -> None:
    monkeypatch.delenv(llm.LOOP_LAG_WARN_ENV, raising=False)
    assert llm.warn_seconds_from_env() == llm.DEFAULT_WARN_SECONDS == 2.0
    monkeypatch.setenv(llm.LOOP_LAG_WARN_ENV, "5")
    assert llm.warn_seconds_from_env() == 5.0
    monkeypatch.setenv(llm.LOOP_LAG_WARN_ENV, "nope")
    assert llm.warn_seconds_from_env() == llm.DEFAULT_WARN_SECONDS


@pytest.mark.asyncio
async def test_disabled_at_zero(monkeypatch) -> None:
    monkeypatch.setenv(llm.LOOP_LAG_WARN_ENV, "0")
    assert await llm.start_loop_lag_monitor_from_env() is None


@pytest.mark.asyncio
async def test_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv(llm.LOOP_LAG_WARN_ENV, raising=False)
    monitor = await llm.start_loop_lag_monitor_from_env()
    try:
        assert monitor is not None and monitor.threshold == 2.0
        assert monitor.tick == llm.DEFAULT_TICK_SECONDS
    finally:
        await monitor.stop()


def test_the_production_lifespan_starts_and_stops_it() -> None:
    src = (Path(llm.__file__).parent / "dapr_host.py").read_text()
    lifespan = src.split("async def production_lifespan", 1)[1].split("app = FastAPI(", 1)[0]
    assert "start_loop_lag_monitor_from_env()" in lifespan
    assert "await lag_monitor.stop()" in lifespan


def _fs(path: str, line: int, name: str):
    import traceback

    return traceback.FrameSummary(path, line, name, lookup_line=False)


def test_library_only_frames_get_the_application_caller_appended() -> None:
    """N-W: live reports named ``croniter.py`` six deep and nothing else."""
    root = llm._PKG_ROOT
    stack = [
        _fs("/usr/lib/python3.11/asyncio/events.py", 80, "_run"),
        _fs(f"{root}/data/analysts/deterministic.py", 890, "run_method"),
        _fs(f"{root}/data/registry/production_gauge.py", 753, "judge"),
        _fs(f"{root}/data/registry/source_freshness.py", 95, "cadence"),
    ] + [_fs("/install/site-packages/croniter.py", 700 + i, f"c{i}") for i in range(6)]
    where = llm.describe_frames(stack)
    head, app = where.split(" | app: ")
    assert head.startswith("croniter.py:705:c5 <- ") and "legba" not in head
    assert app == (
        "data/registry/source_freshness.py:95:cadence <- "
        "data/registry/production_gauge.py:753:judge <- "
        "data/analysts/deterministic.py:890:run_method"
    )


def test_an_application_frame_in_the_window_needs_no_suffix() -> None:
    root = llm._PKG_ROOT
    stack = [_fs(f"{root}/data/x.py", 1, "outer")] + [
        _fs(f"{root}/data/_url_canon.py", 435, "normalized_levenshtein")
    ]
    assert llm.describe_frames(stack) == (
        "data/_url_canon.py:435:normalized_levenshtein <- data/x.py:1:outer"
    )
