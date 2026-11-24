# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F265 — the first poll's entry-age cap.

A feed source's first poll (no cursor) used to ingest every entry its feed
document carried, whatever its age (a status feed loaded entries from March, a
CERT alert feed reached back to 2015). Now:

  * a handler that advertises ``first_poll_max_entry_age_seconds`` has entries
    older than the cap skipped on a poll with no cursor — not written, not
    counted toward the entry cap — and the skip count lands on the poll row
    (``age_skipped``) and in the pull summary;
  * a poll WITH a cursor is never age-capped, and a poll that skipped nothing
    records exactly what it recorded before (``age_skipped`` stays out of the
    statement, NULL; no summary key);
  * undated entries are never skipped;
  * a handler advertising nothing / 0 / garbage is not capped (historical
    behaviour, every kind that does not opt in);
  * the rss kind advertises 30 days by default, the descriptor's
    ``first_poll_max_entry_age_days`` overrides it, and ``0`` means no cap;
  * the writer appends ``age_skipped`` as an integer parameter only when set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.sources._contract import Signal
from legba.data.sources.rss import (
    DEFAULT_FIRST_POLL_MAX_AGE_DAYS,
    RSSConfig,
    RSSSourceHandler,
)
from legba.runtime.source_first_poll_age import (
    DEFAULT_FIRST_POLL_MAX_AGE,
    FirstPollAgeCap,
    first_poll_age_cap_of,
)
from legba.runtime.source_poll_outcome import _insert_with, write_poll_outcome
from tests.runtime.test_source_poll_outcomes import _build, _StubHandler


def _dated(source_id: str, ts: datetime | None, title: str = "item") -> Signal:
    payload: dict = {"title": title, "external_id": uuid4().hex}
    if ts is not None:
        payload["_published_at_dt"] = ts
    return Signal(source_id=source_id, modality="text", payload=payload, content_hash=uuid4().hex)


class _CappedHandler(_StubHandler):
    first_poll_max_entry_age_seconds = 30 * 86400.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# pure pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, 0, -5, "abc", True, float("nan"), float("inf")])
def test_no_or_bad_advertisement_means_no_cap(raw) -> None:
    class H:
        first_poll_max_entry_age_seconds = raw

    assert first_poll_age_cap_of(H()) is None
    assert first_poll_age_cap_of(object()) is None


def test_cap_inactive_unless_first_poll() -> None:
    old = _dated("s", _now() - timedelta(days=400))
    cap = FirstPollAgeCap(_CappedHandler([]), first_poll=False)
    assert not cap.active and not cap.too_old(old) and cap.outcome_count() is None
    cap = FirstPollAgeCap(_CappedHandler([]), first_poll=True)
    assert cap.active and cap.too_old(old) and cap.outcome_count() == 1


def test_undated_and_recent_entries_are_kept() -> None:
    cap = FirstPollAgeCap(_CappedHandler([]), first_poll=True)
    assert not cap.too_old(_dated("s", None))
    assert not cap.too_old(_dated("s", _now() - timedelta(days=29)))
    assert cap.outcome_count() is None  # nothing skipped: the row and summary stay as before


def test_rss_default_override_and_no_cap() -> None:
    assert DEFAULT_FIRST_POLL_MAX_AGE == timedelta(days=DEFAULT_FIRST_POLL_MAX_AGE_DAYS)
    h = RSSSourceHandler(RSSConfig(url="http://x"))
    assert first_poll_age_cap_of(h) == DEFAULT_FIRST_POLL_MAX_AGE
    h = RSSSourceHandler(RSSConfig(url="http://x", first_poll_max_entry_age_days=7))
    assert first_poll_age_cap_of(h) == timedelta(days=7)
    h = RSSSourceHandler(RSSConfig(url="http://x", first_poll_max_entry_age_days=0))
    assert first_poll_age_cap_of(h) is None  # an archive wanted whole
    with pytest.raises(ValueError):
        RSSConfig(url="http://x", first_poll_max_entry_age_days=-1)


# ---------------------------------------------------------------------------
# through SourceCore.pull_once
# ---------------------------------------------------------------------------


def _age_skipped(pool) -> list:
    """The age_skipped parameter of each captured poll-outcome INSERT (None
    when the column was not in the statement)."""
    out = []
    for query, args in pool.executes:
        if "source_poll_outcomes" not in query:
            continue
        out.append(args[11] if "age_skipped" in query else None)
    return out


@pytest.mark.asyncio
async def test_first_poll_skips_old_entries_and_counts_them() -> None:
    entries = [
        _dated("s", _now() - timedelta(days=1), "fresh"),
        _dated("s", _now() - timedelta(days=200), "march"),
        _dated("s", datetime(2015, 7, 24, tzinfo=timezone.utc), "2015"),
        _dated("s", None, "undated"),
    ]
    core, pool, _store, _sid = _build(_CappedHandler(entries))
    result = await core.pull_once()
    assert result["signals_written"] == 2  # fresh + undated
    assert result["first_poll_age_skipped"] == 2
    assert _age_skipped(pool) == [2]
    row = pool.outcome_writes[0]
    assert row["outcome"] == "success"


@pytest.mark.asyncio
async def test_all_old_first_poll_is_empty_with_count() -> None:
    entries = [_dated("s", _now() - timedelta(days=90 + i)) for i in range(5)]
    core, pool, _store, _sid = _build(_CappedHandler(entries))
    result = await core.pull_once()
    assert result["signals_written"] == 0
    assert pool.outcome_writes[0]["outcome"] == "empty"
    assert _age_skipped(pool) == [5]


@pytest.mark.asyncio
async def test_skipped_entries_take_no_entry_cap_slot() -> None:
    old = [_dated("s", _now() - timedelta(days=100 + i)) for i in range(150)]
    fresh = [_dated("s", _now() - timedelta(hours=i + 1)) for i in range(3)]
    core, pool, _store, _sid = _build(_CappedHandler(old + fresh))
    result = await core.pull_once()
    assert result["signals_written"] == 3
    assert pool.outcome_writes[0]["capped"] is False
    assert _age_skipped(pool) == [150]


@pytest.mark.asyncio
async def test_poll_with_a_cursor_is_not_age_capped() -> None:
    core, pool, store, _sid = _build(_CappedHandler([]))
    await core.pull_once()  # first poll seeds the cursor
    core.sd.handler = _CappedHandler([_dated("s", _now() - timedelta(days=300))])
    result = await core.pull_once()
    assert "first_poll_age_skipped" not in result
    assert _age_skipped(pool) == [None, None]
    assert (await store.get("cursor"))["last_pulled_at"]


@pytest.mark.asyncio
async def test_handler_without_cap_keeps_historical_first_poll() -> None:
    entries = [_dated("s", datetime(2015, 1, 1, tzinfo=timezone.utc))]
    core, pool, _store, _sid = _build(_StubHandler(entries))
    result = await core.pull_once()
    assert result["signals_written"] == 1
    assert "first_poll_age_skipped" not in result
    assert _age_skipped(pool) == [None]


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def test_insert_casts_each_optional_column() -> None:
    sql = _insert_with(["fetch_protocol", "age_skipped"])
    assert "keyword_filtered, fetch_protocol, age_skipped\n)" in sql
    assert "$12::jsonb, $13::integer)" in sql


class _Conn:
    def __init__(self) -> None:
        self.calls: list = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


@pytest.mark.asyncio
async def test_writer_appends_age_skipped_only_when_set() -> None:
    conn = _Conn()
    base = dict(source_id="s", source_version="v", owner_tenant="t", outcome="empty",
                health_state=None, capped=False, signals_written=0, error=None)
    await write_poll_outcome(conn, **base)
    await write_poll_outcome(conn, **base, age_skipped=0)
    await write_poll_outcome(conn, **base, age_skipped=7, fetch_protocol={"version": "1.1"})
    (s0, a0), (s1, a1), (s2, a2) = conn.calls
    assert "age_skipped" not in s0 and len(a0) == 11
    assert "age_skipped" in s1 and a1[11] == 0
    assert "fetch_protocol, age_skipped" in s2 and a2[11] == '{"version": "1.1"}' and a2[12] == 7


# ---------------------------------------------------------------------------
# P-5 — json_api's lookback-window drops join the same counted path
# ---------------------------------------------------------------------------
#
# A json_api pull with no cursor asks its API for the ``lookback_minutes``
# window and drops every returned item dated at or before the window start.
# That drop used to be silent. It is now counted: the handler reports it
# (``HANDLER_AGE_SKIPPED_ATTR``), the actor adds it to the F265 count, and it
# lands on ``age_skipped``, ``first_poll_age_skipped`` and ``pull.done``. What
# is ingested is unchanged — pinned against the FROZEN pre-move actor, which
# never reads the count.


def _json_api_case(now: datetime, ages_h: list[float], *, lookback_minutes: int = 1440):
    import json as _json

    import httpx

    from tests.runtime import _ingest_fakes as F

    body = _json.dumps({"value": [
        {"Id": f"it-{i}", "Title": f"Item {i}", "Summary": f"Summary {i}.",
         "PublicationDateAndTime": (now - timedelta(hours=h)).isoformat(),
         "Url": f"https://api.example.org/it/{i}"} for i, h in enumerate(ages_h)]}).encode()

    def _serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    cfg = {"url_template": "https://api.example.org/items", "items_path": "value", "id_path": "Id",
           "title_path": "Title", "timestamp_path": "PublicationDateAndTime", "body_path": "Summary",
           "url_path": "Url", "language": "en", "lookback_minutes": lookback_minutes}

    def _build_handler():
        from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler

        return JsonApiSourceHandler(JsonApiConfig(**cfg), http_client=F.client_for(_serve))

    return F.descriptor("source.test.p5_json_api", "json_api", cfg), _build_handler


async def _polls(mod, desc, build, n: int = 2):
    from legba.runtime.deps import StandardDeps
    from tests.runtime import _ingest_fakes as F

    pool, nats = F.RecordingPool(), F.NatsRecorder()
    deps = StandardDeps(pg_pool=pool, nats_publish=nats)
    actor_id = f"source::{desc.identity.id}::{desc.identity.version[:16]}"
    results = []
    for _ in range(n):
        core = mod.SourceCore(actor_id, mod.SourceDeps(descriptor=desc, deps=deps, handler=build()))
        results.append(await core.pull_once())
    return pool, nats, results


def _outcome_calls(pool) -> list:
    return [c for c in pool.calls if "source_poll_outcomes" in c[1]]


def test_handler_attr_name_is_the_actors() -> None:
    from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler
    from legba.runtime.source_first_poll_age import HANDLER_AGE_SKIPPED_ATTR

    h = JsonApiSourceHandler(JsonApiConfig(url_template="https://x.example/a"))
    assert getattr(h, HANDLER_AGE_SKIPPED_ATTR) == 0


@pytest.mark.parametrize("raw", [None, 0, -3, "7", True, 2.5, object()])
def test_absorb_ignores_garbage(raw) -> None:
    from legba.runtime.source_first_poll_age import HANDLER_AGE_SKIPPED_ATTR

    class H:
        pass

    h = H()
    setattr(h, HANDLER_AGE_SKIPPED_ATTR, raw)
    cap = FirstPollAgeCap(object(), first_poll=True)
    assert cap.absorb_handler_skips(h) == 0 and cap.outcome_count() is None
    assert cap.absorb_handler_skips(object()) == 0


def test_absorb_adds_to_the_actors_own_count() -> None:
    from legba.runtime.source_first_poll_age import HANDLER_AGE_SKIPPED_ATTR

    class H:
        pass

    h = H()
    setattr(h, HANDLER_AGE_SKIPPED_ATTR, 4)
    cap = FirstPollAgeCap(_CappedHandler([]), first_poll=True)
    assert cap.too_old(_dated("s", _now() - timedelta(days=400)))
    assert cap.absorb_handler_skips(h) == 4 and cap.outcome_count() == 5


@pytest.mark.asyncio
async def test_json_api_lookback_drops_are_counted_and_ingest_is_unchanged(monkeypatch, tmp_path) -> None:
    from legba.runtime import source_actor as new_sa
    from tests.runtime import _ingest_fakes as F

    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "0")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    now = datetime.now(tz=timezone.utc)
    # 24 h lookback: two items inside it, three older (one by a whole year).
    desc, build = _json_api_case(now, [1, 5, 30, 72, 24 * 365])
    o_pool, o_nats, o_res = await _polls(F.load_frozen_actor(), desc, build)
    n_pool, n_nats, n_res = await _polls(new_sa, desc, build)
    n_pool.strip_url_hash_column()

    # Ingest unchanged: every statement but the poll row, every message.
    rest = lambda p: F.normalise([c for c in p.calls if "source_poll_outcomes" not in c[1]], now=now)  # noqa: E731
    assert rest(n_pool) == rest(o_pool)
    assert F.normalise(n_nats.messages, now=now) == F.normalise(o_nats.messages, now=now)
    assert [r["signals_written"] for r in n_res] == [r["signals_written"] for r in o_res]
    assert n_res[0]["signals_written"] == 2

    # Counted on the FIRST poll only: the second poll's window starts at the
    # handler cursor, and what it drops there was already seen.
    assert n_res[0]["first_poll_age_skipped"] == 3
    assert "first_poll_age_skipped" not in n_res[1]
    first, second = _outcome_calls(n_pool)
    o_first, o_second = _outcome_calls(o_pool)
    assert "age_skipped" in first[1] and first[2][11] == 3
    assert "age_skipped" not in second[1]
    # Every other column of the row is what the old actor wrote, and the
    # summaries are the old ones plus the one key.
    assert F.normalise(list(first[2][:11]), now=now) == F.normalise(list(o_first[2][:11]), now=now)
    assert F.normalise(second, now=now) == F.normalise(o_second, now=now)
    trimmed = {k: v for k, v in n_res[0].items() if k != "first_poll_age_skipped"}
    assert F.normalise([trimmed, n_res[1]], now=now) == F.normalise(o_res, now=now)


@pytest.mark.asyncio
async def test_json_api_with_nothing_old_records_as_before(monkeypatch, tmp_path) -> None:
    from legba.runtime import source_actor as new_sa
    from tests.runtime import _ingest_fakes as F

    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "0")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    now = datetime.now(tz=timezone.utc)
    desc, build = _json_api_case(now, [1, 2, 3])
    o_pool, o_nats, o_res = await _polls(F.load_frozen_actor(), desc, build)
    n_pool, n_nats, n_res = await _polls(new_sa, desc, build)
    n_pool.strip_url_hash_column()
    full = lambda p, n, r: F.normalise({"sql": p.calls, "nats": n.messages, "results": r}, now=now)  # noqa: E731
    assert full(n_pool, n_nats, n_res) == full(o_pool, o_nats, o_res)


@pytest.mark.asyncio
async def test_json_api_pull_done_line_carries_the_count(monkeypatch, tmp_path, caplog) -> None:
    import logging

    from legba.runtime import source_actor as new_sa

    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "0")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    now = datetime.now(tz=timezone.utc)
    desc, build = _json_api_case(now, [48, 96])  # everything older than the window
    with caplog.at_level(logging.INFO, logger=new_sa.logger.name):
        _pool, _nats, res = await _polls(new_sa, desc, build, n=1)
    assert res[0]["signals_written"] == 0 and res[0]["first_poll_age_skipped"] == 2
    done = [r.getMessage() for r in caplog.records if "source_actor.pull.done" in r.getMessage()]
    assert done and "first_poll_age_skipped=2" in done[-1]
