# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auto-unpause re-probe — entity_gc op 7 (MASTER_PLAN 2026-07-10 ~L319).

The auto-pause latch (op 4, ``_pause_failing_sources``) writes a source's
``state`` -> 'paused' and stamps ``body->>auto_paused_*`` directly (an
out-of-band mutation that bypasses the descriptor's content-hash — an API PUT
would no-op or head-shift back to the pre-pause version). It has misfired
twice on healthy sources (ukrinform: 8 days lost to a 1-day upstream 404
blip; nasa.eonet: a transient 503) with no way back except a manual repair —
the latch never re-probed.

CONTENT-FRESHNESS gate (2026-07-23 night diagnostics rider): an HTTP-status
gate is provably wrong. Live counter-example: voa.africa answers HTTP 200
with valid, well-formed RSS 2.0 — but every ``<pubDate>`` item is frozen at
2025-03-14/15 while ``<lastBuildDate>`` ticks "today" (VOA's syndication
layer died while their CMS stayed alive). A status-only gate would have
resurrected it, and — because it has ZERO prior signals/cursor — its first
post-unpause poll would ingest all 20 sixteen-month-old items as if they were
fresh (the stale-backfill poisoning path: rss.py's since-filter only applies
when a cursor already exists). The re-probe therefore calls the handler's
REAL ``pull()`` (through a throwaway state store) and inspects each yielded
``Signal.payload["published_at"]`` — the item's OWN date, never a
feed/response-level field — requiring the newest item to be BOTH newer than
the auto-pause time AND within a bounded recency window before unpausing.

These pure, no-DB (mocked pool + a fake pull()-yielding handler in place of
``build_source_handler``) unit tests assert:

  * eligibility SQL only selects auto-paused (``auto_paused_at`` present)
    head rows old enough to clear the 24h floor — never an operator-paused
    or retired row (those never carry the marker keys);
  * ``published_at`` parsing (ISO strings, missing/malformed -> None);
  * the content-freshness probe walks the handler's real ``pull()`` and
    returns the MAX parsed item date, bounded by ``max_probe_signals``;
  * ``_probe_source_health``'s freshness verdict: fresh content unpauses;
    an HTTP-200-but-fossil-content source (the voa.africa shape) does NOT;
  * a fresh probe triggers the unpause UPDATE, written the SAME out-of-band
    way the pause writes (direct ``state`` column flip + a ``body`` jsonb key
    STRIP on the head row) — never an API PUT;
  * the unpause UPDATE's WHERE re-checks state/markers at write time (a
    concurrent operator pause/retire between the read and the write can
    never be clobbered);
  * the per-run cap bounds how many candidates get probed in a single tick;
  * ``handle()`` threads the new ``run_source_reprobe`` toggle + tallies
    ``sources_unpaused`` the same way the other legs tally their counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from legba.data.analysts.deterministic_handlers import entity_gc
from legba.data.sources._contract import Signal, SourceHealth


# ---------------------------------------------------------------------------
# Fake asyncpg pool/conn — capture SQL + args (mirrors test_entity_gc_source_pause).
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, fetch_rows: list[dict] | None = None, *, execute_result: str = "UPDATE 1"):
        self._fetch_rows = fetch_rows or []
        self._execute_result = execute_result
        self.fetched: list[str] = []
        self.fetch_args: list[tuple] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetched.append(sql)
        self.fetch_args.append(args)
        return list(self._fetch_rows)

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return self._execute_result


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


class _FakeDeps:
    def __init__(self, pool, *, secrets_resolve=None):
        self.pg_pool = pool
        self.secrets_resolve = secrets_resolve


def _paused_row(
    descriptor_id: str = "source.rss.dead",
    *,
    kind: str = "rss",
    paused_hours_ago: float = 30.0,
    reason: str = "Exceeded 20 consecutive failed polls (24 error outcomes)",
    config: dict | None = None,
) -> dict[str, Any]:
    paused_at = datetime.now(timezone.utc) - timedelta(hours=paused_hours_ago)
    return {
        "descriptor_id": descriptor_id,
        "kind": kind,
        "body": {
            "identity": {"id": descriptor_id, "kind": kind, "version": "abc123"},
            "config": config or {"url": {"raw": "https://example.invalid/rss.xml", "factory_kind": "text"}},
            "auto_paused_at": paused_at.isoformat(),
            "auto_paused_reason": reason,
        },
        "auto_paused_at": paused_at,
    }


# ---------------------------------------------------------------------------
# _fetch_reprobe_candidates — eligibility SQL shape
# ---------------------------------------------------------------------------


async def test_fetch_candidates_sql_targets_auto_paused_heads_only():
    conn = _FakeConn(fetch_rows=[])
    await entity_gc._fetch_reprobe_candidates(
        _FakePool(conn), min_age=entity_gc._REPROBE_MIN_PAUSED_AGE, limit=10,
    )
    assert len(conn.fetched) == 1
    sql = conn.fetched[0]
    assert "source_descriptors" in sql
    assert "is_head" in sql
    assert "state = 'paused'" in sql
    # Both auto-pause markers required — an operator-paused row (which never
    # carries these keys) can never be selected.
    assert "auto_paused_at" in sql
    assert "auto_paused_reason" in sql
    assert "ORDER BY" in sql
    assert "LIMIT" in sql
    # Age floor + cap are BOUND params, not inlined literals.
    args = conn.fetch_args[0]
    assert args[0] == entity_gc._REPROBE_MIN_PAUSED_AGE
    assert args[1] == 10


async def test_fetch_candidates_returns_rows_as_dicts():
    row = _paused_row()
    conn = _FakeConn(fetch_rows=[row])
    out = await entity_gc._fetch_reprobe_candidates(
        _FakePool(conn), min_age=entity_gc._REPROBE_MIN_PAUSED_AGE, limit=10,
    )
    assert out == [row]


# ---------------------------------------------------------------------------
# _parse_signal_published_at — published_at extraction (item date, never
# feed/response-level fields)
# ---------------------------------------------------------------------------


def _sig(published_at: str | None, **payload_extra: Any) -> Signal:
    payload = {"published_at": published_at, **payload_extra}
    return Signal(source_id="source.test", payload=payload)


def test_parse_published_at_valid_iso_z_suffix():
    dt = entity_gc._parse_signal_published_at(_sig("2026-07-20T12:00:00Z"))
    assert dt == datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_published_at_valid_iso_offset():
    dt = entity_gc._parse_signal_published_at(_sig("2026-07-20T12:00:00+02:00"))
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=2)


def test_parse_published_at_naive_gets_utc():
    dt = entity_gc._parse_signal_published_at(_sig("2026-07-20T12:00:00"))
    assert dt == datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_published_at_none_value():
    assert entity_gc._parse_signal_published_at(_sig(None)) is None


def test_parse_published_at_malformed_string():
    assert entity_gc._parse_signal_published_at(_sig("not-a-date")) is None


def test_parse_published_at_missing_key():
    sig = Signal(source_id="source.test", payload={})
    assert entity_gc._parse_signal_published_at(sig) is None


# ---------------------------------------------------------------------------
# _probe_content_freshness — walks the handler's REAL pull(), never a
# shallow health_check(). Fake handlers below mimic exactly what
# TelegramChannelSourceHandler / RSSSourceHandler / etc. do: an async-gen
# pull(ctx, since) yielding real Signal objects.
# ---------------------------------------------------------------------------


class _FakePullHandler:
    """Stand-in for a constructed source handler whose pull() is a canned
    async generator of Signals — mirrors every first-party handler's
    ``pull(ctx, since) -> AsyncIterator[Signal]`` contract exactly."""

    def __init__(self, signals: list[Signal], *, raise_on_pull: Exception | None = None):
        self._signals = signals
        self._raise = raise_on_pull
        self.pull_calls: list[tuple[Any, Any]] = []

    async def pull(self, ctx: Any, since: Any = None) -> AsyncIterator[Signal]:
        self.pull_calls.append((ctx, since))
        if self._raise is not None:
            raise self._raise
        for sig in self._signals:
            yield sig

    async def health_check(self, ctx: Any) -> SourceHealth:
        # Deliberately returns healthy even when content is stale — this is
        # EXACTLY the voa.africa trap: HTTP/connectivity says fine, content
        # says fossil. The freshness probe must not lean on this at all.
        return SourceHealth(state="healthy")


async def test_content_freshness_probe_calls_pull_with_since_none():
    """since=None so a fossil feed's fossil items are actually VISIBLE to
    inspect, rather than silently filtered by the handler's own since-based
    recency filter (which only a handler WITH a cursor would even apply)."""
    handler = _FakePullHandler([_sig("2026-07-20T00:00:00Z")])
    from legba.data.sources._contract import InMemoryStateStore, SourceContext
    ctx = SourceContext(
        target_id="x", target_version="v1", source_id="source.test",
        config=entity_gc._RawConfig(), state_store=InMemoryStateStore(),
    )
    await entity_gc._probe_content_freshness(handler=handler, ctx=ctx, auto_paused_at=None)
    assert len(handler.pull_calls) == 1
    assert handler.pull_calls[0][1] is None  # since


async def test_content_freshness_probe_returns_max_item_date():
    handler = _FakePullHandler([
        _sig("2026-07-10T00:00:00Z"),
        _sig("2026-07-22T00:00:00Z"),  # newest
        _sig("2026-06-01T00:00:00Z"),
    ])
    from legba.data.sources._contract import InMemoryStateStore, SourceContext
    ctx = SourceContext(
        target_id="x", target_version="v1", source_id="source.test",
        config=entity_gc._RawConfig(), state_store=InMemoryStateStore(),
    )
    newest = await entity_gc._probe_content_freshness(handler=handler, ctx=ctx, auto_paused_at=None)
    assert newest == datetime(2026, 7, 22, tzinfo=timezone.utc)


async def test_content_freshness_probe_no_signals_returns_none():
    handler = _FakePullHandler([])
    from legba.data.sources._contract import InMemoryStateStore, SourceContext
    ctx = SourceContext(
        target_id="x", target_version="v1", source_id="source.test",
        config=entity_gc._RawConfig(), state_store=InMemoryStateStore(),
    )
    newest = await entity_gc._probe_content_freshness(handler=handler, ctx=ctx, auto_paused_at=None)
    assert newest is None


async def test_content_freshness_probe_signals_with_no_parseable_date_returns_none():
    handler = _FakePullHandler([_sig(None), _sig("garbage")])
    from legba.data.sources._contract import InMemoryStateStore, SourceContext
    ctx = SourceContext(
        target_id="x", target_version="v1", source_id="source.test",
        config=entity_gc._RawConfig(), state_store=InMemoryStateStore(),
    )
    newest = await entity_gc._probe_content_freshness(handler=handler, ctx=ctx, auto_paused_at=None)
    assert newest is None


async def test_content_freshness_probe_bounded_by_max_probe_signals():
    """A bounded read — never drains an unbounded/paginated source."""
    many = [_sig(f"2020-01-{(i % 28) + 1:02d}T00:00:00Z") for i in range(500)]
    handler = _FakePullHandler(many)
    from legba.data.sources._contract import InMemoryStateStore, SourceContext
    ctx = SourceContext(
        target_id="x", target_version="v1", source_id="source.test",
        config=entity_gc._RawConfig(), state_store=InMemoryStateStore(),
    )
    # If it drained the whole 500-item generator, the test would still pass
    # correctness-wise but this asserts the cap is actually enforced.
    consumed = 0
    orig_pull = handler.pull

    async def _counting_pull(ctx, since=None):
        nonlocal consumed
        async for sig in orig_pull(ctx, since):
            consumed += 1
            yield sig

    handler.pull = _counting_pull
    await entity_gc._probe_content_freshness(
        handler=handler, ctx=ctx, auto_paused_at=None, max_probe_signals=10,
    )
    assert consumed == 10


# ---------------------------------------------------------------------------
# _probe_source_health — the full freshness-gated verdict, incl. the
# voa.africa counter-example the coordinator's rider mandates explicitly.
# ---------------------------------------------------------------------------


def _patch_build_source_handler(monkeypatch, handler, *, capture: dict | None = None):
    def _fake_build(kind, config, *, secrets_resolve=None, registry=None):
        if capture is not None:
            capture["kind"] = kind
            capture["config"] = config
            capture["secrets_resolve"] = secrets_resolve
        return handler

    monkeypatch.setattr(entity_gc, "build_source_handler", _fake_build)


async def test_probe_fresh_content_within_window_and_newer_than_pause_is_fresh(monkeypatch):
    row = _paused_row(descriptor_id="source.rss.back", kind="rss", paused_hours_ago=48)
    handler = _FakePullHandler([_sig((datetime.now(timezone.utc) - timedelta(days=1)).isoformat())])
    capture: dict = {}
    _patch_build_source_handler(monkeypatch, handler, capture=capture)

    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is True
    assert result["newest_item_at"] is not None
    assert capture["kind"] == "rss"
    # config passed to the factory is the RAW (still property-factory-wrapped)
    # body.config dict, unmodified — build_source_handler does its own unwrap.
    assert capture["config"] == row["body"]["config"]


async def test_probe_voa_africa_shape_http_200_valid_feed_all_items_over_a_year_stale_no_unpause(monkeypatch):
    """THE coordinator-mandated regression case, verbatim shape: an HTTP 200
    + a well-formed feed whose every item is frozen >1 year in the past,
    with a feed-level field (lastBuildDate-equivalent) that WOULD claim
    "today" if it were consulted — but the probe never reads that field, only
    each item's own published_at. Auto-paused 30h ago (safely past the 24h
    floor); every item predates the auto-pause AND sits outside the 14-day
    freshness window. Must NOT be marked fresh."""
    paused_at = datetime.now(timezone.utc) - timedelta(hours=30)
    row = _paused_row(
        descriptor_id="source.voa.africa", kind="rss", paused_hours_ago=30,
    )
    # 20 items, all >1 year stale (mirrors voa.africa's 2025-03-09..03-14
    # pubDates against a "today" lastBuildDate that the probe never reads).
    stale_date = datetime(2025, 3, 14, tzinfo=timezone.utc)
    signals = [
        _sig(
            (stale_date - timedelta(hours=i)).isoformat(),
            # A feed/response-level field the probe MUST ignore even if a
            # handler happened to smuggle it onto the payload — freshness is
            # decided ONLY by published_at (the item's own date).
            _feed_last_build_date=datetime.now(timezone.utc).isoformat(),
        )
        for i in range(20)
    ]
    handler = _FakePullHandler(signals)
    handler.health_check = None  # not consulted by the freshness path at all
    _patch_build_source_handler(monkeypatch, handler)

    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is False
    assert result["reason"] == "stale_content"
    # The newest item date IS surfaced (for the operator log line) even
    # though the verdict is "stay paused" — per the coordinator rider.
    assert result["newest_item_at"] == stale_date
    assert result["newest_item_at"] < paused_at


async def test_probe_no_signals_at_all_is_not_fresh(monkeypatch):
    handler = _FakePullHandler([])
    _patch_build_source_handler(monkeypatch, handler)
    row = _paused_row()
    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is False
    assert result["reason"] == "no_parseable_item_dates"
    assert result["newest_item_at"] is None


async def test_probe_fresh_within_window_but_not_newer_than_pause_is_not_fresh(monkeypatch):
    """The source was paused AFTER its most recent content — its newest
    item, though within the 14-day recency window, does not postdate the
    pause, so nothing PROVES the source moved since it went dark."""
    now = datetime.now(timezone.utc)
    paused_at = now - timedelta(hours=1)  # paused VERY recently relative to content
    row = _paused_row(
        descriptor_id="source.rss.x", kind="rss", paused_hours_ago=1 / 60,  # ~1 minute — irrelevant, overridden below
    )
    # Override body's auto_paused_at directly to control the exact instant.
    row["body"]["auto_paused_at"] = paused_at.isoformat()
    item_date = now - timedelta(days=3)  # within the 14-day window...
    assert item_date < paused_at  # ...but predates the pause
    handler = _FakePullHandler([_sig(item_date.isoformat())])
    _patch_build_source_handler(monkeypatch, handler)

    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is False
    assert result["reason"] == "not_newer_than_auto_pause"


async def test_probe_missing_auto_paused_at_only_requires_window(monkeypatch):
    """Defensive: if auto_paused_at is somehow absent/unparseable on the row
    (should not happen given the eligibility SQL, but the probe must not
    crash), freshness falls back to JUST the recency window."""
    row = _paused_row()
    row["body"]["auto_paused_at"] = "not-a-timestamp"
    fresh_date = datetime.now(timezone.utc) - timedelta(days=1)
    handler = _FakePullHandler([_sig(fresh_date.isoformat())])
    _patch_build_source_handler(monkeypatch, handler)

    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is True


async def test_probe_pull_exception_degrades_to_not_fresh(monkeypatch):
    handler = _FakePullHandler([], raise_on_pull=RuntimeError("network unreachable"))
    _patch_build_source_handler(monkeypatch, handler)
    row = _paused_row()
    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is False
    assert result["newest_item_at"] is None
    assert "probe_error" in result["reason"]


async def test_probe_build_handler_exception_degrades_to_not_fresh(monkeypatch):
    def _boom(kind, config, *, secrets_resolve=None, registry=None):
        raise ValueError("unknown source kind")

    monkeypatch.setattr(entity_gc, "build_source_handler", _boom)
    row = _paused_row()
    result = await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None),
    )
    assert result["fresh"] is False


async def test_probe_threads_secrets_resolve_for_credentialed_kinds(monkeypatch):
    handler = _FakePullHandler([_sig((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())])
    capture: dict = {}
    _patch_build_source_handler(monkeypatch, handler, capture=capture)

    async def _resolve(vault_id: str) -> str:
        return "secret-value"

    row = _paused_row(descriptor_id="source.telegram.org_channels", kind="telegram_channel")
    await entity_gc._probe_source_health(
        descriptor_id=row["descriptor_id"], kind=row["kind"], body=row["body"],
        deps=_FakeDeps(pool=None, secrets_resolve=_resolve),
    )
    assert capture["secrets_resolve"] is _resolve


# ---------------------------------------------------------------------------
# _reprobe_paused_sources — the full leg: candidates -> probe -> mirrored write
# ---------------------------------------------------------------------------


async def test_reprobe_unpauses_on_fresh_content_with_mirrored_write(monkeypatch):
    row = _paused_row(descriptor_id="source.rss.back", kind="rss", paused_hours_ago=48)
    conn = _FakeConn(fetch_rows=[row])
    handler = _FakePullHandler([_sig((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())])
    _patch_build_source_handler(monkeypatch, handler)

    unpaused = await entity_gc._reprobe_paused_sources(_FakePool(conn), _FakeDeps(pool=None))
    assert unpaused == 1
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    # Mirrors the pause write EXACTLY in reverse: same head-row targeting,
    # a direct state flip + a body key STRIP (never an API PUT / re-register).
    assert "UPDATE source_descriptors" in sql
    assert "state = 'active'" in sql
    assert "is_head" in sql
    assert "auto_paused_at" in sql
    assert "auto_paused_reason" in sql
    assert "-" in sql  # jsonb key-delete operator
    assert "DELETE" not in sql.upper()
    assert args == ("source.rss.back",)


async def test_reprobe_leaves_voa_africa_shaped_fossil_untouched(monkeypatch):
    """End-to-end: the voa.africa counter-example must never reach the
    UPDATE at all — the whole point of the freshness gate."""
    row = _paused_row(descriptor_id="source.voa.africa", kind="rss", paused_hours_ago=30)
    conn = _FakeConn(fetch_rows=[row])
    stale_date = datetime(2025, 3, 14, tzinfo=timezone.utc)
    handler = _FakePullHandler(
        [_sig((stale_date - timedelta(hours=i)).isoformat()) for i in range(20)]
    )
    _patch_build_source_handler(monkeypatch, handler)

    unpaused = await entity_gc._reprobe_paused_sources(_FakePool(conn), _FakeDeps(pool=None))
    assert unpaused == 0
    assert conn.executed == []


async def test_reprobe_concurrent_guard_write_returns_zero_rows(monkeypatch):
    """A concurrent operator pause/retire between the read and the write
    means the UPDATE's WHERE (state='paused' AND body ? auto_paused_at)
    matches nothing at write time — 'UPDATE 0' — and must NOT be counted
    as a successful unpause."""
    row = _paused_row(paused_hours_ago=48)
    conn = _FakeConn(fetch_rows=[row], execute_result="UPDATE 0")
    handler = _FakePullHandler([_sig((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())])
    _patch_build_source_handler(monkeypatch, handler)

    unpaused = await entity_gc._reprobe_paused_sources(_FakePool(conn), _FakeDeps(pool=None))
    assert unpaused == 0
    assert len(conn.executed) == 1  # the write was still attempted


async def test_reprobe_multiple_candidates_mixed_outcomes(monkeypatch):
    healthy_row = _paused_row(descriptor_id="source.rss.healthy", kind="rss", paused_hours_ago=48)
    fossil_row = _paused_row(descriptor_id="source.rss.fossil", kind="rss", paused_hours_ago=48)
    conn = _FakeConn(fetch_rows=[healthy_row, fossil_row])

    calls = {"n": 0}

    def _fake_build(kind, config, *, secrets_resolve=None, registry=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakePullHandler([_sig((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())])
        return _FakePullHandler(
            [_sig("2025-03-14T00:00:00Z")]  # stale, like fossil_row
        )

    monkeypatch.setattr(entity_gc, "build_source_handler", _fake_build)

    unpaused = await entity_gc._reprobe_paused_sources(_FakePool(conn), _FakeDeps(pool=None))
    assert unpaused == 1
    assert len(conn.executed) == 1
    assert conn.executed[0][1] == ("source.rss.healthy",)


async def test_reprobe_no_candidates_is_noop(monkeypatch):
    conn = _FakeConn(fetch_rows=[])

    def _fail_if_called(*a, **k):
        raise AssertionError("build_source_handler must not be called with no candidates")

    monkeypatch.setattr(entity_gc, "build_source_handler", _fail_if_called)

    unpaused = await entity_gc._reprobe_paused_sources(_FakePool(conn), _FakeDeps(pool=None))
    assert unpaused == 0
    assert conn.executed == []


# ---------------------------------------------------------------------------
# Per-run cap — the fetch's LIMIT is _REPROBE_MAX_PER_RUN
# ---------------------------------------------------------------------------


async def test_reprobe_run_cap_is_positive_and_bounded():
    # A future spike in simultaneously-auto-paused sources must not turn one
    # GC tick into an unbounded serial fan-out of live HTTP probes.
    assert entity_gc._REPROBE_MAX_PER_RUN > 0
    assert entity_gc._REPROBE_MAX_PER_RUN <= 50


async def test_reprobe_passes_cap_as_fetch_limit(monkeypatch):
    conn = _FakeConn(fetch_rows=[])

    async def _noop_probe(**kwargs):
        return {"fresh": False, "newest_item_at": None, "reason": "n/a"}

    monkeypatch.setattr(entity_gc, "_probe_source_health", _noop_probe)
    await entity_gc._reprobe_paused_sources(_FakePool(conn), _FakeDeps(pool=None))
    assert conn.fetch_args[0][1] == entity_gc._REPROBE_MAX_PER_RUN


# ---------------------------------------------------------------------------
# Constants — the eligibility floor + the freshness window, spec-anchored
# ---------------------------------------------------------------------------


def test_reprobe_min_age_matches_master_plan_spec():
    # MASTER_PLAN 2026-07-10 ~L319: "hourly HEAD after 24h auto-paused".
    assert entity_gc._REPROBE_MIN_PAUSED_AGE == timedelta(hours=24)


def test_reprobe_freshness_window_matches_coordinator_rider():
    # 2026-07-23 night diagnostics rider: "within a bounded window (e.g. <=14 days)".
    assert entity_gc._REPROBE_FRESHNESS_WINDOW == timedelta(days=14)


# ---------------------------------------------------------------------------
# handle() — the reprobe leg is the ONLY one enabled (others off -> pure)
# ---------------------------------------------------------------------------


async def test_handle_reprobe_leg_counts_and_tags(monkeypatch):
    row = _paused_row(descriptor_id="source.rss.recovered", kind="rss", paused_hours_ago=48)
    conn = _FakeConn(fetch_rows=[row])
    handler = _FakePullHandler([_sig((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())])
    _patch_build_source_handler(monkeypatch, handler)
    deps = _FakeDeps(_FakePool(conn))

    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": False,
            "run_orphan_proposed_edges": False,
            "run_compaction": False,
            "run_source_reprobe": True,
        },
        deps,
    )
    data = result.finding.data
    assert data["sources_unpaused"] == 1
    assert "gc_actions_taken" in result.finding.tags
    assert "sources_unpaused=1" in result.finding.body
    # other legs untouched
    assert data["sources_paused"] == 0
    assert data["dormant_entities"] == 0
    assert data["compacted_edges"] == 0


async def test_handle_reprobe_leg_default_on(monkeypatch):
    """run_source_reprobe defaults True — an omitted option still runs it,
    matching every other leg's default-on convention."""
    conn = _FakeConn(fetch_rows=[])
    deps = _FakeDeps(_FakePool(conn))
    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": False,
            "run_orphan_proposed_edges": False,
            "run_compaction": False,
            # run_source_reprobe omitted deliberately
        },
        deps,
    )
    # No candidates -> 0, but the fetch DID run (proves the leg fired).
    assert result.finding.data["sources_unpaused"] == 0
    assert len(conn.fetched) == 1


async def test_handle_reprobe_can_be_disabled(monkeypatch):
    conn = _FakeConn(fetch_rows=[_paused_row()])

    def _fail_if_called(*a, **k):
        raise AssertionError("reprobe leg must not run when disabled")

    monkeypatch.setattr(entity_gc, "build_source_handler", _fail_if_called)
    deps = _FakeDeps(_FakePool(conn))

    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": False,
            "run_orphan_proposed_edges": False,
            "run_compaction": False,
            "run_source_reprobe": False,
        },
        deps,
    )
    assert result.finding.data["sources_unpaused"] == 0
    assert conn.fetched == []  # the leg never even ran its SELECT


async def test_handle_reprobe_failure_is_swallowed(monkeypatch):
    class _BoomConn(_FakeConn):
        async def fetch(self, sql, *args):
            raise RuntimeError("boom")

    deps = _FakeDeps(_FakePool(_BoomConn()))
    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": False,
            "run_orphan_proposed_edges": False,
            "run_compaction": False,
            "run_source_reprobe": True,
        },
        deps,
    )
    # Degrades to zero, does not abort the run.
    assert result.finding.data["sources_unpaused"] == 0
