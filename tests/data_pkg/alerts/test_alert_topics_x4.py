# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 ALERTS — two topics, level-only escalations never push.

Pure + fake-pool tests (no database). The real-driver half — the 9-move tick,
the ledger rows read back, the volume route, the ``alert_volume_high`` page —
lives in ``tests/data_pkg/test_alert_x4_db.py``.

What a user sees differently: the phone gets ``world`` (the subject) and
``engine`` (the platform) as two topics; an escalation whose read held the same
severity as the desk's previous read on that unit is no longer pushed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.alerts import AlertSinkDispatcher, NtfyAlertSink
from legba.data.alerts import topics as T
from legba.data.alerts.sinks import (
    ENV_SUPPRESS_LEVEL_ONLY,
    AlertSinkPayload,
    classify_escalation_transition,
    runtime_alert_payload,
)
from legba.data.analysts.agency.tools import ChannelEmitter
from legba.data.schemas.action_pack import Channel

SERVER_TOPIC_URL = "http://ntfy.example.test/legba-alerts"


class _Resp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = ""


class _Http:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url, *, content=None, headers=None, timeout=None, **kw):  # noqa: ANN001
        self.calls.append({"url": url, "content": content, "headers": dict(headers or {})})
        return _Resp(200)


async def _noop_sleep(_s: float) -> None:
    return None


def _ntfy(http: _Http, url: str = SERVER_TOPIC_URL) -> NtfyAlertSink:
    return NtfyAlertSink(url=url, http=http, sleep=_noop_sleep, min_severity="medium")


class _Pool:
    """Duck-typed pool: records INSERTs; ``fetchrow`` answers the two reads of
    the level-only rule from ``finding`` / ``prior`` (None = not found)."""

    def __init__(self, finding: dict | None = None, prior: dict | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.finding = finding
        self.prior = prior

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "produced_at < $3" in sql:
            return self.prior
        if "activation_count" in sql:
            return self.finding
        return None  # the enrichment read

    async def fetch(self, sql: str, *args: Any) -> Any:
        return []

    def rows(self, sink_kind: str) -> list[tuple[Any, ...]]:
        return [
            a for s, a in self.calls
            if "INSERT INTO alert_sink_deliveries" in s and a[2] == sink_kind
        ]


def _finding(severity: str = "high", *, activation: int | None = None,
             tags: list[str] | None = None) -> dict:
    return {
        "analyst_id": "military_posture",
        "target_id": "desk_x",
        "severity": severity,
        "produced_at": datetime.now(timezone.utc),
        "activation_count": None if activation is None else str(activation),
        "tags": json.dumps(tags or ["severity:" + severity, "severity_delta:steady"]),
    }


# ---------------------------------------------------------------------------
# 1) The topic table — every kind the code emits is declared
# ---------------------------------------------------------------------------


def test_every_trigger_class_and_watchdog_channel_names_its_topic() -> None:
    """A new trigger class or watchdog channel cannot silently land on the
    default topic: it must be declared in alerts.topics.TOPIC_BY_KIND."""
    from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
    from legba.runtime import liveness_watchdog as lw

    kinds = set(ats.TRIGGER_CLASSES) | set(lw._TRANSITION_CHANNELS) | {
        lw.ALERT_CHANNEL_GLOBAL, "escalations",
    }
    missing = sorted(k for k in kinds if k not in T.TOPIC_BY_KIND)
    assert not missing, f"alert kinds with no declared topic: {missing}"
    # The nightly suite keeps its own topic now that it raises through
    # the single alert path; only its kind may use it.
    assert set(T.TOPIC_BY_KIND.values()) == {T.TOPIC_WORLD, T.TOPIC_ENGINE, T.TOPIC_NIGHTLY}
    assert [k for k, t in T.TOPIC_BY_KIND.items() if t == T.TOPIC_NIGHTLY] == ["nightly_suite"]


@pytest.mark.parametrize(
    ("kind", "topic"),
    [
        ("escalations", "world"), ("band_crossing", "world"),
        ("verified_finding", "world"), ("situation_escalation", "world"),
        ("source_cadence_stall", "engine"), ("source_degraded", "engine"),
        ("search_degraded", "engine"), ("analyst_cadence_stall", "engine"),
        ("llm_route_dead", "engine"), ("production_deficit", "engine"),
        ("external_audit", "engine"), ("alert_volume_high", "engine"),
    ],
)
def test_topic_for_kind(kind: str, topic: str) -> None:
    assert T.topic_for(kind) == topic


def test_a_rollup_goes_to_world_when_any_member_does() -> None:
    assert T.topic_for_rollup(["production_deficit", "band_crossing"]) == "world"
    assert T.topic_for_rollup(["production_deficit", "coverage_floor"]) == "engine"


# ---------------------------------------------------------------------------
# 2) Topic URLs — the server the existing env names, two topics on it
# ---------------------------------------------------------------------------


def test_the_existing_topic_url_locates_the_server(monkeypatch) -> None:
    for env in (T.ENV_TOPIC_SPLIT, T.ENV_TOPIC_WORLD, T.ENV_TOPIC_ENGINE):
        monkeypatch.delenv(env, raising=False)
    assert T.server_root("http://ntfy/legba-alerts") == "http://ntfy"
    assert T.server_root("https://h.example/ntfy/legba-alerts/") == "https://h.example/ntfy"
    assert T.server_root("http://ntfy") == "http://ntfy"
    assert T.topic_url("http://ntfy/legba-alerts", "world") == "http://ntfy/world"
    assert T.topic_url("http://ntfy/legba-alerts", "engine") == "http://ntfy/engine"


def test_topic_names_are_tunable_and_validated(monkeypatch) -> None:
    monkeypatch.setenv(T.ENV_TOPIC_WORLD, "legba-world_1")
    monkeypatch.setenv(T.ENV_TOPIC_ENGINE, "bad/name")  # not an ntfy topic name
    assert T.topic_url("http://ntfy/x", "world") == "http://ntfy/legba-world_1"
    assert T.topic_url("http://ntfy/x", "engine") == "http://ntfy/engine"


def test_split_off_publishes_to_the_single_pre_split_topic(monkeypatch) -> None:
    monkeypatch.setenv(T.ENV_TOPIC_SPLIT, "0")
    assert T.topic_url("http://ntfy/legba-alerts", "engine") == "http://ntfy/legba-alerts"


# ---------------------------------------------------------------------------
# 3) The ntfy sink publishes to the payload's topic
# ---------------------------------------------------------------------------


async def test_a_source_stall_is_published_on_engine(monkeypatch) -> None:
    monkeypatch.delenv(T.ENV_TOPIC_SPLIT, raising=False)
    http = _Http()
    d = AlertSinkDispatcher(pg_pool=None, sinks=[_ntfy(http)], cooldown_seconds=0)
    payload = runtime_alert_payload(
        channel_name="source_cadence_stall", summary="Source poll stall: s1 silent 4h",
    )
    assert payload.topic == "engine" and payload.kind == "source_cadence_stall"
    [res] = await d.fan_out(payload)
    assert res.delivered
    assert [c["url"] for c in http.calls] == ["http://ntfy.example.test/engine"]


async def test_an_unlisted_runtime_channel_defaults_to_engine() -> None:
    p = runtime_alert_payload(channel_name="consult_uninstallable", summary="x")
    assert p.topic == "engine"


async def test_a_batched_push_titles_its_size_and_lists_its_members() -> None:
    http = _Http()
    sink = _ntfy(http)
    await sink.deliver(
        AlertSinkPayload(
            summary="9 band moves: 7 worse, 2 better", severity="high",
            channel_name="trigger_scan", kind="band_crossing", topic="world",
            verify_state="unverified — x", batch_count=9,
            detail="d1 escalation low→watch (worse)\nd2 escalation high→watch (better)",
        )
    )
    [call] = http.calls
    assert call["url"] == "http://ntfy.example.test/world"
    assert call["headers"]["X-Title"] == "Legba: high - 9 band moves"
    body = call["content"].decode("utf-8").split("\n")
    assert body[0] == "9 band moves: 7 worse, 2 better"
    assert "d1 escalation low→watch (worse)" in body


# ---------------------------------------------------------------------------
# 4) Per-topic cooldown — an engine push never swallows the world's
# ---------------------------------------------------------------------------


async def test_cooldown_is_per_topic() -> None:
    http = _Http()
    clock = [1000.0]
    d = AlertSinkDispatcher(
        pg_pool=None, sinks=[_ntfy(http)], cooldown_seconds=60,
        monotonic=lambda: clock[0],
    )
    eng = runtime_alert_payload(channel_name="source_degraded", summary="s1 degraded")
    eng2 = runtime_alert_payload(channel_name="source_degraded", summary="s2 degraded")
    world = runtime_alert_payload(
        channel_name="escalations", summary="w", topic="world"
    )
    assert [r.outcome for r in await d.fan_out(eng)] == ["delivered"]
    assert [r.outcome for r in await d.fan_out(world)] == ["delivered"]
    assert [r.outcome for r in await d.fan_out(eng2)] == ["skipped_cooldown"]
    clock[0] += 61
    # The world's next push carries no engine backlog; the engine's does.
    await d.fan_out(runtime_alert_payload(channel_name="escalations", summary="w2", topic="world"))
    await d.fan_out(runtime_alert_payload(channel_name="source_degraded", summary="s3"))
    world_body = http.calls[2]["content"].decode()
    engine_body = http.calls[3]["content"].decode()
    assert "more alert(s) during cooldown" not in world_body
    assert "+1 more alert(s) during cooldown" in engine_body and "s2 degraded" in engine_body


# ---------------------------------------------------------------------------
# 5) The level-only rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "prior", "found", "flip", "level_only", "text"),
    [
        ("high", "high", True, False, True, "high → high (level-only)"),
        ("High", "high ", True, False, True, "high → high (level-only)"),
        ("high", "elevated", True, False, False, "elevated → high"),
        ("elevated", "high", True, False, False, "high → elevated"),
        ("high", None, False, False, False, "new: — → high"),
        ("high", None, True, False, False,
         "unmeasured — the previous read carries no severity (→ high)"),
        (None, "high", True, False, False, "unmeasured — the read carries no severity"),
        ("high", "high", True, True, False, "indicator flip"),
    ],
)
def test_classify_escalation_transition(current, prior, found, flip, level_only, text):
    assert classify_escalation_transition(
        current=current, prior=prior, prior_found=found, indicator_flip=flip,
    ) == (level_only, text)


def _channel() -> Channel:
    return Channel(name="escalations", kind="alert", config={"subject": "channels.escalations"})


def _emit_payload(output_id: UUID) -> dict[str, Any]:
    return {
        "action": "escalate", "severity": "high", "title": "Posture holds high",
        "detail": "", "target_ref": f"analyst_outputs:{output_id}",
        "requested_by": "analyst::military_posture", "output_id": str(output_id),
        "target_id": "desk_x", "effective_confidence": 0.8, "faithfulness_score": 0.9,
    }


async def _ok_publish(subject: str, payload: bytes) -> None:
    return None


async def test_a_level_only_escalation_records_but_never_pushes(monkeypatch) -> None:
    monkeypatch.delenv(ENV_SUPPRESS_LEVEL_ONLY, raising=False)
    http = _Http()
    pool = _Pool(finding=_finding("high"), prior={"severity": "high"})
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_ntfy(http)], cooldown_seconds=0)
    emitter = ChannelEmitter(nats_publish=_ok_publish, pg_pool=pool, alert_sinks=d)
    oid = uuid4()

    await emitter.emit(_channel(), _emit_payload(oid))

    assert http.calls == []                       # no push
    [internal] = pool.rows("alert")               # the bus publish, unchanged
    assert internal[7] == "delivered"             # (the emitter's own column order)
    [ntfy_row] = pool.rows("ntfy")
    assert ntfy_row[0] == oid and ntfy_row[8] == "skipped_level_only"
    assert "high → high (level-only)" in ntfy_row[9]
    summary = json.loads(ntfy_row[11])
    assert summary["topic"] == "world" and summary["kind"] == "escalations"
    assert summary["transition"] == "high → high (level-only)"


async def test_a_transition_pushes_once_on_world(monkeypatch) -> None:
    monkeypatch.delenv(ENV_SUPPRESS_LEVEL_ONLY, raising=False)
    monkeypatch.delenv(T.ENV_TOPIC_SPLIT, raising=False)
    http = _Http()
    pool = _Pool(finding=_finding("high"), prior={"severity": "elevated"})
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_ntfy(http)], cooldown_seconds=0)
    emitter = ChannelEmitter(nats_publish=_ok_publish, pg_pool=pool, alert_sinks=d)

    await emitter.emit(_channel(), _emit_payload(uuid4()))

    assert [c["url"] for c in http.calls] == ["http://ntfy.example.test/world"]
    [ntfy_row] = pool.rows("ntfy")
    assert ntfy_row[8] == "delivered"
    summary = json.loads(ntfy_row[11])
    assert (summary["topic"], summary["transition"]) == ("world", "elevated → high")


async def test_an_indicator_flip_pushes_even_at_a_held_severity() -> None:
    http = _Http()
    pool = _Pool(finding=_finding("high", activation=2), prior={"severity": "high"})
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_ntfy(http)], cooldown_seconds=0)
    p = await d.payload_for_finding(
        channel_name="escalations", alert_row_id=str(uuid4()), target_id="desk_x",
        severity="high", effective_confidence=0.8, title="t",
    )
    assert (p.level_only, p.transition) == (False, "indicator flip")
    assert [r.outcome for r in await d.fan_out(p)] == ["delivered"]


async def test_an_unreadable_finding_fails_toward_paging() -> None:
    """No row to compare (fetchrow → None) is ``unmeasured`` and PUSHES."""
    http = _Http()
    d = AlertSinkDispatcher(pg_pool=_Pool(), sinks=[_ntfy(http)], cooldown_seconds=0)
    p = await d.payload_for_finding(
        channel_name="escalations", alert_row_id=str(uuid4()), target_id="desk_x",
        severity="high", effective_confidence=0.8, title="t",
    )
    assert p.level_only is False and p.transition.startswith("unmeasured")
    assert [r.outcome for r in await d.fan_out(p)] == ["delivered"]


async def test_the_level_only_switch_restores_the_push(monkeypatch) -> None:
    monkeypatch.setenv(ENV_SUPPRESS_LEVEL_ONLY, "0")
    http = _Http()
    pool = _Pool(finding=_finding("high"), prior={"severity": "high"})
    d = AlertSinkDispatcher(pg_pool=pool, sinks=[_ntfy(http)], cooldown_seconds=0)
    p = await d.payload_for_finding(
        channel_name="escalations", alert_row_id=str(uuid4()), target_id="desk_x",
        severity="high", effective_confidence=0.8, title="t",
    )
    assert p.level_only is True                    # still measured …
    assert [r.outcome for r in await d.fan_out(p)] == ["delivered"]  # … but pushed


async def test_split_off_the_sink_publishes_to_the_configured_url(monkeypatch) -> None:
    monkeypatch.setenv(T.ENV_TOPIC_SPLIT, "0")
    http = _Http()
    d = AlertSinkDispatcher(pg_pool=None, sinks=[_ntfy(http)], cooldown_seconds=0)
    await d.fan_out(runtime_alert_payload(channel_name="source_degraded", summary="s"))
    assert [c["url"] for c in http.calls] == [SERVER_TOPIC_URL]


# ---------------------------------------------------------------------------
# 6) The band batch (pure) and the budget's batch unit
# ---------------------------------------------------------------------------


def _band(desk: str, frm: str, to: str, direction: str, severity: str):
    from legba.data.analysts.deterministic_handlers.alert_trigger_scan import (
        TRIGGER_BAND, AlertCandidate,
    )

    return AlertCandidate(
        trigger_class=TRIGGER_BAND, severity=severity,
        title=f"Band {direction}: {desk} escalation {frm} → {to}", body="",
        target_id=desk,
        data={"trigger_class": TRIGGER_BAND, "desk": desk, "dimension": "escalation",
              "from_band": frm, "to_band": to, "direction": direction},
    )


def _nine():
    return (
        [_band(f"d{i}", "watch", "high", "deterioration", "high") for i in range(7)]
        + [_band(f"e{i}", "high", "watch", "improvement", "medium") for i in range(2)]
    )


def test_batch_payload_counts_lists_worst_first_and_caps() -> None:
    from legba.data.analysts.deterministic_handlers import _band_push_batch as bpb
    from legba.data.analysts.deterministic_handlers.alert_trigger_scan import _sink_payload

    cands = list(reversed(_nine()))  # improvements first: order must not matter
    members = [(uuid4(), c) for c in cands]
    p = bpb.batch_payload(members, sink_payload=_sink_payload, max_lines=4)
    assert p.summary == "9 band moves: 7 worse, 2 better"
    lines = p.detail.split("\n")
    assert lines[:4] == [f"d{i} escalation watch→high (worse)" for i in range(4)]
    assert lines[4].startswith("… and 5 more (per-tick cap 4")
    assert (p.severity, p.target_id, p.kind, p.topic) == ("high", None, "band_crossing", "world")
    assert p.batch_count == 9 and len(p.batch_alert_ids) == 9
    # The receipt anchors on the worst move.
    worst_id = next(r for r, c in members if c.target_id == "d0")
    assert p.alert_row_id == str(worst_id)


def test_one_band_move_pushes_exactly_what_it_always_did() -> None:
    from legba.data.analysts.deterministic_handlers import _band_push_batch as bpb
    from legba.data.analysts.deterministic_handlers.alert_trigger_scan import _sink_payload

    rid, cand = uuid4(), _band("d0", "watch", "high", "deterioration", "high")
    single = bpb.batch_payload([(rid, cand)], sink_payload=_sink_payload)
    direct = _sink_payload(rid, cand)
    assert single.model_dump(exclude={"detected_at"}) == direct.model_dump(
        exclude={"detected_at"})


def test_group_pushes_folds_band_moves_and_keeps_order() -> None:
    from legba.data.analysts.deterministic_handlers import _band_push_batch as bpb
    from legba.data.analysts.deterministic_handlers.alert_trigger_scan import (
        AlertCandidate, _sink_payload,
    )

    other = AlertCandidate(trigger_class="production_deficit", severity="medium",
                           title="deficit", body="", target_id=None)
    items = [(uuid4(), c) for c in _nine()[:3]] + [(uuid4(), other)]
    pushes, n = bpb.group_pushes(items, sink_payload=_sink_payload)
    assert n == 3 and len(pushes) == 2
    assert pushes[0].batch_count == 3 and pushes[0].topic == "world"
    assert pushes[1].kind == "production_deficit" and pushes[1].topic == "engine"


def test_the_budget_treats_a_push_batch_as_one_unit() -> None:
    from legba.data.analysts.deterministic_handlers import _band_push_batch as bpb
    from legba.data.analysts.deterministic_handlers import _daily_page_budget as dpb

    cands = _nine()
    bpb.mark_band_batch(cands, run_uuid="r1")
    deferred = dpb.apply_daily_page_budget(
        cands, already_paged_today=0, budget=1, per_kind_cap=1,
    )
    assert deferred == 0
    assert [c.data["budget_deferred"] for c in cands] == [False] * 9
    # The batch's whole fate follows its best-ranked member: no slot left.
    cands2 = _nine()
    bpb.mark_band_batch(cands2, run_uuid="r2")
    deferred2 = dpb.apply_daily_page_budget(
        cands2, already_paged_today=1, budget=1, per_kind_cap=1,
    )
    assert deferred2 == 9
    assert {c.data["budget_deferred"] for c in cands2} == {True}
