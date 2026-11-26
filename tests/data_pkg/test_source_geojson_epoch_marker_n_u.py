# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A geojson update marker in EPOCH form parses.

USGS ``properties.updated`` is epoch MILLISECONDS (live 11-13: 1794282147855),
so ``_parse_marker_datetime`` returned None, ``newest_entry_ts`` was always
NULL for ``source.usgs.earthquakes_m45``, and its empty streak read "unknown"
(paged at 5 polls) instead of honest-quiet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from legba.data.sources.geojson import _newest_entry_ts, _parse_marker_datetime


@pytest.mark.parametrize(("raw", "expected"), [
    ("1794282147855", datetime(2026, 11, 10, 3, 42, 27, 855000, tzinfo=timezone.utc)),
    ("1794282147", datetime(2026, 11, 10, 3, 42, 27, tzinfo=timezone.utc)),
    ("1794282147855.0", datetime(2026, 11, 10, 3, 42, 27, 855000, tzinfo=timezone.utc)),
    ("2026-09-02T22:16:00Z", datetime(2026, 9, 2, 22, 16, tzinfo=timezone.utc)),
])
def test_epoch_and_iso_markers_parse(raw, expected):
    assert _parse_marker_datetime(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "17942821478551234", "abc"])
def test_numbers_outside_the_epoch_shapes_are_junk(raw):
    assert _parse_marker_datetime(raw) is None


def test_usgs_shaped_features_yield_newest_entry_ts():
    now = datetime.now(tz=timezone.utc)
    older = int((now - timedelta(days=2)).timestamp() * 1000)
    newer = int((now - timedelta(hours=3)).timestamp() * 1000)
    features = [
        {"type": "Feature", "properties": {"mag": 6.4, "time": older, "updated": older}},
        {"type": "Feature", "properties": {"mag": 5.1, "time": newer, "updated": newer}},
    ]
    got = _newest_entry_ts(features)
    assert got is not None and abs((got - (now - timedelta(hours=3))).total_seconds()) < 1


def test_a_future_epoch_marker_is_still_excluded():
    future = int((datetime.now(tz=timezone.utc) + timedelta(days=5)).timestamp() * 1000)
    assert _newest_entry_ts([{"properties": {"updated": future}}]) is None
