# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Dapr orphan-reminder GC sweep.

The load-bearing invariant under test: the sweep unregisters reminders ONLY
for RETIRED actors and NEVER for a live (active/paused/error) actor — a buggy
sweep that kills a live reminder re-creates the silent-cadence stall it exists
to fix. These tests exercise the pure name-derivation + the sweep's call set
against in-memory fakes (no daprd, no Postgres).

A second invariant added by the GET-before-DELETE fix (2026-07 DQ sweep,
R12): ``build_sidecar_reminder_deleter`` must NOT count a candidate as
``removed`` off the DELETE response alone — daprd answers 2xx for an
already-absent reminder just as readily as a real one. The
``test_sidecar_deleter_*`` tests below drive that function against a fake
sidecar (``httpx.MockTransport``) to pin the GET-miss / GET-hit / failure
semantics.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from legba.runtime.lifecycle import ACTIVE, ERROR, PAUSED, RETIRED
from legba.runtime.reminder_gc import (
    ReminderGCResult,
    build_sidecar_reminder_deleter,
    orphan_reminder_names,
    sweep_orphan_reminders,
)
from legba.runtime.state import ActorStateRecord, SourceCursor


def _rec(
    actor_id: str,
    kind: str,
    *,
    lifecycle: str,
    descriptor_id: str | None = None,
    cursors: list[str] | None = None,
) -> ActorStateRecord:
    return ActorStateRecord(
        actor_id=actor_id,
        actor_kind=kind,
        descriptor_id=descriptor_id or actor_id.split("::")[1],
        descriptor_version="deadbeef00000000",
        lifecycle=lifecycle,
        source_cursors={c: SourceCursor(source_id=c) for c in (cursors or [])},
    )


class _FakeStore:
    """Minimal ActorStateStore stand-in: only list_by_lifecycle is used."""

    def __init__(self, records: list[ActorStateRecord]) -> None:
        self._records = records

    async def list_by_lifecycle(self, lifecycle: str) -> list[ActorStateRecord]:
        return [r for r in self._records if r.lifecycle == lifecycle]


class _RecordingDeleter:
    """Records every (actor_type, actor_id, name) delete call. Idempotent."""

    def __init__(self, *, present: set[tuple[str, str, str]] | None = None) -> None:
        # If `present` is given, only those calls report removed=True (a real
        # reminder existed); everything else is a no-op success (already gone).
        self.calls: list[tuple[str, str, str]] = []
        self._present = present

    async def __call__(self, actor_type: str, actor_id: str, name: str) -> bool:
        self.calls.append((actor_type, actor_id, name))
        if self._present is None:
            return True
        return (actor_type, actor_id, name) in self._present


# --------------------------------------------------------------------------
# orphan_reminder_names (pure)
# --------------------------------------------------------------------------


def test_source_reminder_name_is_poll_descriptor() -> None:
    rec = _rec("source::nyt_world::abcd", "source", lifecycle=RETIRED)
    assert orphan_reminder_names(rec) == ["poll_nyt_world"]


def test_source_includes_extra_cursor_sources_deduped() -> None:
    rec = _rec(
        "source::multi::abcd",
        "source",
        lifecycle=RETIRED,
        cursors=["multi", "feed_b", "feed_c"],
    )
    names = orphan_reminder_names(rec)
    # descriptor poll first, then the extra cursor sources, no dup of `multi`.
    assert names == ["poll_multi", "poll_feed_b", "poll_feed_c"]


def test_analyst_reminder_name_is_run_cadence() -> None:
    rec = _rec("analyst::country_critic::abcd", "analyst", lifecycle=RETIRED)
    assert orphan_reminder_names(rec) == ["run_cadence"]


def test_target_legacy_run_source_names_from_cursors() -> None:
    rec = _rec(
        "target::india::abcd", "target", lifecycle=RETIRED, cursors=["src_a", "src_b"]
    )
    assert orphan_reminder_names(rec) == ["run_source_src_a", "run_source_src_b"]


def test_target_with_no_cursors_yields_no_names() -> None:
    # Source-first targets register no reminders → nothing to GC.
    rec = _rec("target::india::abcd", "target", lifecycle=RETIRED)
    assert orphan_reminder_names(rec) == []


def test_unknown_kind_yields_no_names() -> None:
    rec = _rec("weird::x::abcd", "weird", lifecycle=RETIRED)
    assert orphan_reminder_names(rec) == []


# --------------------------------------------------------------------------
# sweep_orphan_reminders (the safety-critical surface)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_only_touches_retired_actors() -> None:
    store = _FakeStore(
        [
            _rec("source::live::a", "source", lifecycle=ACTIVE),
            _rec("source::paused::b", "source", lifecycle=PAUSED),
            _rec("analyst::erroring::c", "analyst", lifecycle=ERROR),
            _rec("source::dead::d", "source", lifecycle=RETIRED),
            _rec("analyst::gone::e", "analyst", lifecycle=RETIRED),
        ]
    )
    deleter = _RecordingDeleter()

    result = await sweep_orphan_reminders(state_store=store, delete_reminder=deleter)

    # ONLY the two retired actors' reminders were deleted — never the
    # active/paused/error ones.
    assert deleter.calls == [
        ("SourceActor", "source::dead::d", "poll_dead"),
        ("AnalystActor", "analyst::gone::e", "run_cadence"),
    ]
    assert result.retired_scanned == 2
    assert result.removed == 2
    assert result.failed == 0


@pytest.mark.asyncio
async def test_sweep_never_deletes_a_live_reminder_even_if_listed() -> None:
    # Defence-in-depth: a misbehaving lister returns an ACTIVE row tagged as
    # retired-query result. The per-record guard must still refuse it.
    class _LeakyStore(_FakeStore):
        async def list_by_lifecycle(self, lifecycle: str):
            return [_rec("source::live::a", "source", lifecycle=ACTIVE)]

    deleter = _RecordingDeleter()
    result = await sweep_orphan_reminders(
        state_store=_LeakyStore([]), delete_reminder=deleter
    )
    assert deleter.calls == []
    assert result.removed == 0


@pytest.mark.asyncio
async def test_sweep_is_idempotent_no_action_when_already_clean() -> None:
    # Retired actor but the reminder is already gone → delete returns no-op
    # success (removed=False) → no alert, took_action False.
    store = _FakeStore([_rec("source::dead::d", "source", lifecycle=RETIRED)])
    deleter = _RecordingDeleter(present=set())  # nothing actually present

    fired: list[tuple[str, bytes]] = []

    async def _alert(subject: str, payload: bytes) -> None:
        fired.append((subject, payload))

    result = await sweep_orphan_reminders(
        state_store=store, delete_reminder=deleter, alert_publish=_alert
    )
    assert deleter.calls == [("SourceActor", "source::dead::d", "poll_dead")]
    assert result.removed == 0
    assert result.already_absent == 1
    assert result.took_action is False
    assert fired == []  # no alert when nothing was actually removed


@pytest.mark.asyncio
async def test_sweep_fires_alert_when_orphan_removed() -> None:
    store = _FakeStore([_rec("source::dead::d", "source", lifecycle=RETIRED)])
    present = {("SourceActor", "source::dead::d", "poll_dead")}
    deleter = _RecordingDeleter(present=present)

    fired: list[tuple[str, bytes]] = []

    async def _alert(subject: str, payload: bytes) -> None:
        fired.append((subject, payload))

    result = await sweep_orphan_reminders(
        state_store=store, delete_reminder=deleter, alert_publish=_alert
    )
    assert result.removed == 1
    assert result.took_action is True
    assert len(fired) == 1
    subject, payload = fired[0]
    assert subject == "legba.alerts.reminder_gc"
    body = json.loads(payload)
    assert body["kind"] == "reminder_gc"
    assert body["removed"] == 1
    assert body["reminders"] == [
        {"actor_id": "source::dead::d", "reminder": "poll_dead"}
    ]


@pytest.mark.asyncio
async def test_sweep_continues_past_a_per_actor_delete_failure() -> None:
    store = _FakeStore(
        [
            _rec("source::boom::a", "source", lifecycle=RETIRED),
            _rec("analyst::ok::b", "analyst", lifecycle=RETIRED),
        ]
    )

    async def _flaky(actor_type: str, actor_id: str, name: str) -> bool:
        if actor_id == "source::boom::a":
            raise RuntimeError("sidecar 500")
        return True

    result = await sweep_orphan_reminders(state_store=store, delete_reminder=_flaky)
    # The boom actor failed, but the ok actor's reminder was still removed.
    assert result.failed == 1
    assert result.removed == 1


@pytest.mark.asyncio
async def test_sweep_survives_store_outage() -> None:
    class _DeadStore:
        async def list_by_lifecycle(self, lifecycle: str):
            raise RuntimeError("pg down")

    deleter = _RecordingDeleter()
    result = await sweep_orphan_reminders(
        state_store=_DeadStore(), delete_reminder=deleter
    )
    assert isinstance(result, ReminderGCResult)
    assert result.retired_scanned == 0
    assert deleter.calls == []


# --------------------------------------------------------------------------
# alert_publish degradation (best-effort side-channel — no alerts stream)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_stream_publish_error_degrades_to_debug_not_warning(caplog) -> None:
    # The GC succeeded (it removed an orphan), but the alert publish hits a
    # subject with no capturing JetStream stream → ``no response from stream``.
    # That must NOT log at WARNING (it was spamming 143×/12h); it degrades to a
    # single DEBUG and the sweep result is unaffected.
    import logging as _logging

    import legba.runtime.reminder_gc as gc_mod

    # Reset the module-level once-guard so this test is order-independent.
    gc_mod._no_stream_logged = False

    store = _FakeStore([_rec("source::dead::d", "source", lifecycle=RETIRED)])
    deleter = _RecordingDeleter(present={("SourceActor", "source::dead::d", "poll_dead")})

    async def _no_stream_alert(subject: str, payload: bytes) -> None:
        raise RuntimeError("nats: no response from stream")

    with caplog.at_level(_logging.DEBUG, logger="legba.runtime.reminder_gc"):
        result = await sweep_orphan_reminders(
            state_store=store, delete_reminder=deleter, alert_publish=_no_stream_alert
        )

    # GC unaffected — the orphan was still removed.
    assert result.removed == 1
    assert result.took_action is True
    # No WARNING-level alert-publish-failed record.
    warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    assert not any("alert_publish_failed" in r.getMessage() for r in warnings), (
        f"unexpected WARNING noise: {[r.getMessage() for r in warnings]}"
    )
    # Exactly one DEBUG no-stream record.
    debug_no_stream = [
        r for r in caplog.records if "alert_publish_no_stream" in r.getMessage()
    ]
    assert len(debug_no_stream) == 1
    assert debug_no_stream[0].levelno == _logging.DEBUG


@pytest.mark.asyncio
async def test_no_stream_debug_logged_only_once_across_sweeps(caplog) -> None:
    # The once-guard means a steady-state stack (alert fails every sweep)
    # emits the DEBUG note a single time, not once per sweep.
    import logging as _logging

    import legba.runtime.reminder_gc as gc_mod

    gc_mod._no_stream_logged = False

    store = _FakeStore([_rec("source::dead::d", "source", lifecycle=RETIRED)])
    deleter = _RecordingDeleter(present={("SourceActor", "source::dead::d", "poll_dead")})

    async def _no_stream_alert(subject: str, payload: bytes) -> None:
        raise RuntimeError("nats: no response from stream")

    with caplog.at_level(_logging.DEBUG, logger="legba.runtime.reminder_gc"):
        for _ in range(3):
            await sweep_orphan_reminders(
                state_store=store,
                delete_reminder=deleter,
                alert_publish=_no_stream_alert,
            )

    debug_no_stream = [
        r for r in caplog.records if "alert_publish_no_stream" in r.getMessage()
    ]
    assert len(debug_no_stream) == 1


@pytest.mark.asyncio
async def test_genuine_publish_error_still_warns(caplog) -> None:
    # A non-no-stream publish failure (e.g. broker drain) must NOT be silenced —
    # it stays at WARNING so a real regression is visible.
    import logging as _logging

    import legba.runtime.reminder_gc as gc_mod

    gc_mod._no_stream_logged = False

    store = _FakeStore([_rec("source::dead::d", "source", lifecycle=RETIRED)])
    deleter = _RecordingDeleter(present={("SourceActor", "source::dead::d", "poll_dead")})

    async def _broken_alert(subject: str, payload: bytes) -> None:
        raise RuntimeError("connection reset by peer")

    with caplog.at_level(_logging.DEBUG, logger="legba.runtime.reminder_gc"):
        result = await sweep_orphan_reminders(
            state_store=store, delete_reminder=deleter, alert_publish=_broken_alert
        )

    assert result.removed == 1
    assert any(
        r.levelno >= _logging.WARNING and "alert_publish_failed" in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------------------------------
# build_sidecar_reminder_deleter — the GET-before-DELETE fix (R12)
#
# The production deleter must never count a candidate as `removed` off the
# DELETE response alone: daprd answers 2xx for a reminder that was never
# registered just as readily as for one it actually deleted. Only a
# confirmed GET-hit followed by a successful DELETE may return True.
# --------------------------------------------------------------------------


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[tuple[str, str]]:
    """Route the module's local ``import httpx`` AsyncClient through a
    :class:`httpx.MockTransport`. Returns the list of (method, path) seen,
    in call order, so tests can assert GET-before-DELETE ordering (and that
    a GET-miss never triggers a DELETE at all).
    """
    calls: list[tuple[str, str]] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    real_async_client = httpx.AsyncClient

    def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched)
    return calls


@pytest.mark.asyncio
async def test_sidecar_deleter_get_404_not_counted_as_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GET-miss (404): confirmed absent. Must return False and must NEVER
    # issue the DELETE — there is nothing to delete.
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(404)

    calls = _patch_async_client(monkeypatch, _handler)
    delete = build_sidecar_reminder_deleter(sidecar_url="http://fake-sidecar:3500")

    removed = await delete("SourceActor", "source::dead::d", "poll_dead")

    assert removed is False
    assert calls == [
        ("GET", "/v1.0/actors/SourceActor/source::dead::d/reminders/poll_dead")
    ]


@pytest.mark.asyncio
async def test_sidecar_deleter_get_hit_then_delete_counts_as_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GET-hit (200 + real body): the reminder genuinely exists. Only now may
    # the DELETE fire, and only then does the candidate count as removed.
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"period": "1h", "dueTime": "0s"})
        assert request.method == "DELETE"
        return httpx.Response(204)

    calls = _patch_async_client(monkeypatch, _handler)
    delete = build_sidecar_reminder_deleter(sidecar_url="http://fake-sidecar:3500")

    removed = await delete("SourceActor", "source::live::a", "poll_live")

    assert removed is True
    # GET before DELETE, same path, in that order.
    path = "/v1.0/actors/SourceActor/source::live::a/reminders/poll_live"
    assert calls == [("GET", path), ("DELETE", path)]


@pytest.mark.asyncio
async def test_sidecar_deleter_get_200_empty_body_not_counted_as_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defensive case: some daprd builds answer 200 + empty/null body instead
    # of 404 for a missing reminder. Must be treated the same as a 404.
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, content=b"{}")

    calls = _patch_async_client(monkeypatch, _handler)
    delete = build_sidecar_reminder_deleter(sidecar_url="http://fake-sidecar:3500")

    removed = await delete("AnalystActor", "analyst::gone::e", "run_cadence")

    assert removed is False
    # No DELETE was issued against an already-absent reminder.
    assert all(method != "DELETE" for method, _ in calls)


@pytest.mark.asyncio
async def test_sidecar_deleter_get_unexpected_status_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Neither 200 nor 404 — an unexpected sidecar status must NOT be
    # silently treated as absent; it must raise so the sweep counts it
    # under `failed` (retried next sweep) rather than masking it.
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_async_client(monkeypatch, _handler)
    delete = build_sidecar_reminder_deleter(sidecar_url="http://fake-sidecar:3500")

    with pytest.raises(RuntimeError):
        await delete("SourceActor", "source::flaky::z", "poll_flaky")


@pytest.mark.asyncio
async def test_sidecar_deleter_delete_failure_after_hit_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GET confirmed the reminder exists, but the DELETE itself fails — this
    # is a genuine failure, not an already-absent no-op, and must not be
    # reported as removed=False (which the sweep would silently treat as
    # "nothing to do").
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"period": "1h"})
        return httpx.Response(500)

    _patch_async_client(monkeypatch, _handler)
    delete = build_sidecar_reminder_deleter(sidecar_url="http://fake-sidecar:3500")

    with pytest.raises(RuntimeError):
        await delete("SourceActor", "source::live::a", "poll_live")


@pytest.mark.asyncio
async def test_sweep_with_sidecar_deleter_alerts_only_on_genuine_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end through the real production deleter: a sweep with one
    # genuine orphan and one already-absent phantom candidate must report
    # removed=1 / already_absent=1, and the alert payload must name only
    # the genuine removal — the exact regression (phantom deletions firing
    # the alert every sweep) this fix closes.
    def _handler(request: httpx.Request) -> httpx.Response:
        if "source::dead::d" in str(request.url):
            if request.method == "GET":
                return httpx.Response(200, json={"period": "24h"})
            return httpx.Response(204)
        # Every other candidate (the phantom) is genuinely absent.
        assert request.method == "GET"
        return httpx.Response(404)

    _patch_async_client(monkeypatch, _handler)
    delete = build_sidecar_reminder_deleter(sidecar_url="http://fake-sidecar:3500")

    store = _FakeStore(
        [
            _rec("source::dead::d", "source", lifecycle=RETIRED),
            _rec("analyst::phantom::old", "analyst", lifecycle=RETIRED),
        ]
    )
    fired: list[tuple[str, bytes]] = []

    async def _alert(subject: str, payload: bytes) -> None:
        fired.append((subject, payload))

    result = await sweep_orphan_reminders(
        state_store=store, delete_reminder=delete, alert_publish=_alert
    )

    assert result.removed == 1
    assert result.already_absent == 1
    assert result.took_action is True
    assert len(fired) == 1
    body = json.loads(fired[0][1])
    assert body["removed"] == 1
    assert body["reminders"] == [{"actor_id": "source::dead::d", "reminder": "poll_dead"}]
