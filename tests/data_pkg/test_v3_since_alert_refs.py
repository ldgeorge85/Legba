# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure tests (no DB) for the alert pointer fields on ``/v3/since``.

An alert says what it points at — ``finding_id`` on a ``verified_finding``
page, ``situation_id`` + ``state_from``/``state_to`` on a
``situation_escalation`` page — so the reader's "Changed" list can fold the
alert into the item it alerts on, and drop an ``x -> x`` page as no change.
The fields are additive and None when the payload does not carry them.
"""
from __future__ import annotations

from datetime import datetime, timezone

import legba.data.registry.since_api as since_api

AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _row(**extra):
    base = {
        "id": "a-1",
        "severity": "high",
        "channel": "verified_finding",
        "summary": "Verified high-severity finding: something",
        "target_id": "desk_a",
        "produced_at": AT,
    }
    base.update(extra)
    return base


def test_alerts_sql_projects_the_pointer_columns_from_the_payload():
    sql = since_api._ALERTS_SQL
    for col in ("finding_id", "situation_id", "state_from", "state_to"):
        assert f"data->'data'->>'{col}'" in sql
        assert f"AS {col}" in sql


def test_alert_item_carries_the_finding_pointer():
    item = since_api.since_alert_item(_row(finding_id="f-9"))
    assert item.finding_id == "f-9"
    assert item.situation_id is None
    assert item.state_from is None and item.state_to is None


def test_alert_item_carries_the_situation_transition():
    item = since_api.since_alert_item(
        _row(channel="situation_escalation", situation_id="s-3", state_from="escalating", state_to="escalating")
    )
    assert item.situation_id == "s-3"
    assert (item.state_from, item.state_to) == ("escalating", "escalating")


def test_alert_item_from_an_older_row_shape_has_no_pointers():
    item = since_api.since_alert_item(_row())
    dumped = item.model_dump()
    assert dumped["finding_id"] is None
    assert dumped["situation_id"] is None
    assert item.channel == "verified_finding"


def test_empty_string_pointer_reads_none_not_empty():
    item = since_api.since_alert_item(_row(finding_id="", channel=None))
    assert item.finding_id is None
    assert item.channel == ""
