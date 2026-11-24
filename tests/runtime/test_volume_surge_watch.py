# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-1 ``volume_surge`` — the detector's pure core and its live pass on fakes.

* freshness: a backlog item (published hours before it was fetched) does not
  count; a date with no time of day is unknown and counts;
* two feeds of one publisher are ONE outlet;
* the baseline floors its deviation (a quiet subject never gets a zero band);
* the shape of the 2026-11-13 ground stop (US: 74 fresh signals from 20
  outlets against an hourly mean of about 19) fires; the same count from one
  outlet, or an ordinary hour, does not;
* the live pass raises one ``volume_surge`` per firing subject (medium), closes
  a subject that stopped firing for the close window, and does its per-row
  work OFF the event loop (a worker thread).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from legba.runtime import volume_surge_watch as vs

TH = vs.SurgeThresholds()
NOW = datetime(2026, 11, 13, 22, 50, tzinfo=timezone.utc)


def _row(i: int, subject: str, source: str, *, minutes_ago: float = 10,
         published_lag_h: float | None = 0.2, title: str = "FAA ground stop") -> dict[str, Any]:
    fetched = NOW - timedelta(minutes=minutes_ago)
    pub = None if published_lag_h is None else (fetched - timedelta(hours=published_lag_h)).isoformat()
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "subject": subject,
            "source_id": source, "fetched_at": fetched, "published_at": pub,
            "title": f"{title} {i}"}


def test_freshness() -> None:
    f = NOW
    assert vs.is_fresh(f, (f - timedelta(hours=2)).isoformat(), 3)
    assert not vs.is_fresh(f, (f - timedelta(hours=16)).isoformat(), 3)
    assert vs.is_fresh(f, "20261113", 3)          # date only: unknown
    assert vs.is_fresh(f, "2026-11-13", 3)
    assert vs.is_fresh(f, None, 3)
    assert vs.is_fresh(f, "Fri, 13 Nov 2026 22:00:00 GMT", 3)  # unparseable: unknown


def test_baseline_floors_the_deviation() -> None:
    mean, sigma = vs.baseline_stats([0] * 335 + [1])
    assert mean < 0.01 and sigma == 1.0
    mean, sigma = vs.baseline_stats([19] * 336)
    assert mean == 19 and sigma == pytest.approx(19 ** 0.5)


def test_the_ground_stop_shape_fires_and_an_ordinary_hour_does_not() -> None:
    rows = [_row(i, "US", f"source.outlet{i % 20}.world") for i in range(74)]
    cands = vs.window_candidates(rows, TH)
    assert cands["US"]["count"] == 74 and len(cands["US"]["outlets"]) == 20
    (s,) = vs.detect(cands, {"US": (19.1, 7.8)}, TH)
    assert s.subject == "US" and s.z == pytest.approx((74 - 19.1) / 7.8)
    assert "74 signals from 20 independent outlets" in s.summary()
    # an ordinary hour for the US
    assert vs.detect(cands, {"US": (40.0, 9.0)}, TH) == []


def test_one_outlet_or_a_backlog_never_fires() -> None:
    one = [_row(i, "TW", "source.focustaiwan.news") for i in range(36)]
    assert vs.window_candidates(one, TH) == {}
    backlog = [_row(i, "RU", f"source.o{i % 12}", published_lag_h=16) for i in range(60)]
    assert vs.window_candidates(backlog, TH) == {}


def test_two_feeds_of_one_publisher_are_one_outlet(monkeypatch) -> None:  # noqa: ANN001
    from legba.data.analysts import source_independence as si

    monkeypatch.setitem(si._PUBLISHER_OF, "source.bbc.world", "bbc")
    monkeypatch.setitem(si._PUBLISHER_OF, "source.bbc.europe", "bbc")
    assert vs.outlet_key("source.bbc.world") == vs.outlet_key("source.bbc.europe") == "bbc"


# ---------------------------------------------------------------------------
# The live pass, on fakes
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, window: list[dict], baseline: list[dict]) -> None:
        self.window, self.baseline = window, baseline

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return self.window if "g = ANY" not in sql else self.baseline


class _Acq:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *a: Any) -> None:
        return None


class _Pg:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> _Acq:
        return _Acq(self.conn)


class _Wd:
    def __init__(self, conn: _Conn) -> None:
        self._pg = _Pg(conn)
        self._is_leader = None
        self._alert_sinks = object()


@pytest.mark.asyncio
async def test_live_pass_raises_closes_and_works_off_the_loop(monkeypatch) -> None:  # noqa: ANN001
    from legba.data.alerts import raise_alert as ra

    raised: list[dict] = []

    async def _fake_raise(pool: Any, **kw: Any) -> None:
        raised.append(kw)

    monkeypatch.setattr(ra, "raise_alert", _fake_raise)
    threads: list[str] = []
    real = vs.window_candidates

    def _spy(rows, th):  # noqa: ANN001
        threads.append(threading.current_thread().name)
        return real(rows, th)

    monkeypatch.setattr(vs, "window_candidates", _spy)
    vs._BASELINE_CACHE.clear()
    window = [_row(i, "US", f"source.outlet{i % 20}.world") for i in range(74)]
    # 14 days of ~19 fresh US signals an hour
    base_start = (NOW - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    baseline = [
        {"subject": "US", "fetched_at": base_start - timedelta(hours=h, minutes=m * 3),
         "published_at": None}
        for h in range(1, 14 * 24 + 1) for m in range(19)
    ]
    wd = _Wd(_Conn(window, baseline))
    surges = await vs.check_volume_surge_once(wd, now=NOW, force=True)
    assert [s.subject for s in surges] == ["US"]
    assert threads and threading.main_thread().name not in threads
    (r,) = raised
    assert r["kind"] == "volume_surge" and r["severity"] == "medium"
    assert r["subject_id"] == "US" and r["evidence_refs"][0].startswith("signal:")

    # an hour and a bit later, nothing fires: the episode is closed
    raised.clear()
    wd._pg.conn.window = []
    later = NOW + timedelta(minutes=70)
    assert await vs.check_volume_surge_once(wd, now=later, force=True) == []
    (c,) = raised
    assert c["close"] is True and c["subject_id"] == "US" and c["kind"] == "volume_surge"


@pytest.mark.asyncio
async def test_disabled_by_env_does_nothing(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv(vs.ENV_ENABLED, "0")
    assert await vs.check_volume_surge_once(_Wd(_Conn([], [])), force=True) == []
