# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scheduled-events calendar, with fakes only.

The lane's defect: a consult asserted "nothing else scheduled in the
window" from MODEL MEMORY when ``search_signals`` failed, because the
platform had no always-on calendar to tell "nothing scheduled" apart from
"I could not look". These tests pin the three different facts — rows held
for the window, an EMPTY table, and an empty window over a non-empty table
— plus the tool plumbing (``_KNOWN_TOOLS`` / the substrate_read tuple /
the descriptor / the native grammar) and the seed-file round-trip.

No Postgres: the fake store is an in-memory conn driving the REAL store
functions (``legba.data.scheduled_events``) and the REAL production port
(``PostgresQdrantSubstrateQueryPort`` over a fake pool), so the window
semantics under test are the ones production runs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from legba.data import scheduled_events as sched
from legba.data.analysts import consult_prompt_context as pctx
from legba.data.analysts.agency.substrate_read import (
    SUBSTRATE_READ_TOOLS,
    list_scheduled_events_tool,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext
from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS, _dispatch_tool
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

pytestmark = [pytest.mark.asyncio]

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"
_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "seeds" / "scheduled_events.example.yaml"
)

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    title: str,
    starts_at: datetime,
    *,
    ends_at: datetime | None = None,
    scope: str = "global",
    subject_keys: list[str] | None = None,
    confidence: str = "confirmed",
    retired_at: datetime | None = None,
) -> dict:
    """One stored-row dict (the shape ``LIST_EVENTS_SQL`` returns)."""
    return {
        "id": uuid4(),
        "title": title,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "scope": scope,
        "subject_keys": subject_keys or [],
        "source_url": None,
        "confidence": confidence,
        "provenance": "operator",
        "created_at": NOW,
        "retired_at": retired_at,
    }


class _FakeConn:
    """A conn-shaped fake over an in-memory row list.

    ``fetch`` returns the stored rows (``list_events`` applies the retired
    exclusion, the window and the filters itself — the SQL carries no
    predicate); ``fetchrow`` plays INSERT ... ON CONFLICT (title,
    starts_at) DO NOTHING RETURNING id so the loader round-trip runs
    against the same code path as production.
    """

    def __init__(self, rows=()) -> None:
        self.rows = [dict(r) for r in rows]
        self.fetch_calls: list[str] = []

    async def fetch(self, sql, *params):
        self.fetch_calls.append(sql)
        return [dict(r) for r in self.rows]

    async def fetchrow(self, sql, *params):
        (
            _id, title, starts_at, ends_at, scope,
            subject_keys, source_url, confidence, provenance,
        ) = params
        for r in self.rows:
            if r["title"] == title and r["starts_at"] == starts_at:
                return None  # ON CONFLICT DO NOTHING
        row = {
            "id": _id,
            "title": title,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "scope": scope,
            "subject_keys": list(subject_keys),
            "source_url": source_url,
            "confidence": confidence,
            "provenance": provenance,
            "created_at": NOW,
            "retired_at": None,
        }
        self.rows.append(row)
        return {"id": _id}


class _FakePool:
    """A pool-shaped fake whose ``acquire()`` yields one conn."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


def _port(conn: _FakeConn) -> PostgresQdrantSubstrateQueryPort:
    """The REAL production port over the fake store."""
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=_FakePool(conn), qdrant_client=None,
        signals_collection="legba_test_sched__signals",
    )


def _three_event_store(anchor: datetime = NOW) -> _FakeConn:
    """The done test's store: one in-window, one outside, one retired.

    ``anchor`` is the clock the window is read against: the frozen ``NOW``
    for the store tests, the WALL clock for the tests that go through the real
    port (it defaults its window to ``datetime.now()`` → +14 d, so events
    pinned to the frozen ``NOW`` fall out of it once the calendar passes them —
    "Summit opens" did at 2026-11-08 12:00Z)."""
    return _FakeConn([
        _event("Summit opens", anchor + timedelta(days=3)),
        _event("Far-future deadline", anchor + timedelta(days=60)),
        _event("Cancelled vote", anchor + timedelta(days=2), retired_at=anchor),
    ])


# ---------------------------------------------------------------------------
# The store: window semantics, coverage, filters
# ---------------------------------------------------------------------------


async def test_three_events_one_in_window_returns_one_and_coverage_says_so():
    """The done test: one inside the 14-day window, one outside, one
    retired → one row and coverage.n == 1."""
    out = await sched.list_events(
        _three_event_store(),
        window_start=NOW,
        window_end=NOW + timedelta(days=14),
    )
    assert [r["title"] for r in out["rows"]] == ["Summit opens"]
    assert out["coverage"]["n"] == 1
    # The retired row is not an event; the far-future one is held, just not
    # in this window — total_rows keeps the two facts apart.
    assert out["coverage"]["total_rows"] == 2
    assert out["coverage"]["window"] == [
        NOW.isoformat(), (NOW + timedelta(days=14)).isoformat(),
    ]


async def test_a_ranged_event_overlapping_the_window_counts():
    """An event that began before the window and ends inside it is IN the
    window — COP-length events would otherwise vanish from every consult."""
    conn = _FakeConn([
        _event("Long summit", NOW - timedelta(days=5), ends_at=NOW + timedelta(days=2)),
        _event("Done yesterday", NOW - timedelta(days=3), ends_at=NOW - timedelta(days=1)),
    ])
    out = await sched.list_events(
        conn, window_start=NOW, window_end=NOW + timedelta(days=14),
    )
    assert [r["title"] for r in out["rows"]] == ["Long summit"]


async def test_scope_is_an_exact_match_and_subject_keys_match_on_any_shared_key():
    conn = _FakeConn([
        _event("Israel vote", NOW + timedelta(days=1), scope="il"),
        _event("COP session", NOW + timedelta(days=1), scope="global",
               subject_keys=["climate"]),
    ])
    out = await sched.list_events(
        conn, window_start=NOW, window_end=NOW + timedelta(days=14), scope="il",
    )
    assert [r["title"] for r in out["rows"]] == ["Israel vote"]
    out = await sched.list_events(
        conn, window_start=NOW, window_end=NOW + timedelta(days=14),
        subject_keys=["climate"],
    )
    assert [r["title"] for r in out["rows"]] == ["COP session"]


async def test_the_real_port_defaults_the_window_to_now_plus_fourteen_days():
    """The production port parses no args into now → +14 d, clamps the
    limit and returns rows + coverage — the shape the tool result folds
    into the loop."""
    conn = _three_event_store(datetime.now(timezone.utc))
    out = await _port(conn).list_scheduled_events()
    assert out["count"] == 1
    assert out["rows"][0]["title"] == "Summit opens"
    assert out["coverage"]["n"] == 1
    window = out["coverage"]["window"]
    assert window[0] < window[1]
    span = datetime.fromisoformat(window[1]) - datetime.fromisoformat(window[0])
    assert span == timedelta(days=14)


async def test_the_port_refuses_a_malformed_bound_and_an_inverted_window():
    port = _port(_three_event_store())
    bad = await port.list_scheduled_events(window_start="last tuesday")
    assert "error" in bad and bad["rows"] == []
    inverted = await port.list_scheduled_events(
        window_start="2026-11-20", window_end="2026-11-01",
    )
    assert "error" in inverted and inverted["rows"] == []


# ---------------------------------------------------------------------------
# The tool surface: pack tuple, descriptor, grammar, dispatch
# ---------------------------------------------------------------------------


def test_the_tool_is_on_every_surface_it_must_agree_across():
    """_KNOWN_TOOLS, the pack tuple, the descriptor YAML and the native
    grammar — a miss in any one is the zero-invocation defect."""
    assert "list_scheduled_events" in _KNOWN_TOOLS
    assert "list_scheduled_events" in SUBSTRATE_READ_TOOLS
    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text()
    )
    assert "list_scheduled_events" in {t["name"] for t in body["tools"]}
    from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS

    props = set(TOOL_SCHEMAS["list_scheduled_events"].json_schema["properties"])
    assert props == {
        "window_start", "window_end", "scope", "subject_keys", "limit",
    }


async def test_the_handler_coerces_args_onto_the_port_signature():
    """subject_keys accepts a bare string OR a list; limit coerces int;
    the window bounds pass through as the planner wrote them."""

    class _Recorder:
        def __init__(self) -> None:
            self.kwargs = None

        async def list_scheduled_events(self, **kwargs):
            self.kwargs = kwargs
            return {"rows": [], "refs": [], "count": 0, "coverage": {}}

    port = _Recorder()
    call = ToolCall(
        pack_id="substrate_read",
        tool_name="list_scheduled_events",
        args={"window_start": "2026-11-05", "window_end": "2026-11-19",
              "scope": "il", "subject_keys": "elections", "limit": "10"},
    )
    result = await list_scheduled_events_tool(call, None, ToolContext(substrate=port))
    assert result.status == "completed"
    assert port.kwargs["scope"] == "il"
    assert port.kwargs["subject_keys"] == ["elections"]
    assert port.kwargs["limit"] == 10
    assert port.kwargs["window_start"] == "2026-11-05"


async def test_the_tool_with_no_port_fails_visibly():
    """No substrate port → a failed ToolResult naming the gap, never a
    silent empty the planner could read as 'nothing scheduled'."""
    call = ToolCall(
        pack_id="substrate_read", tool_name="list_scheduled_events", args={},
    )
    result = await list_scheduled_events_tool(call, None, ToolContext(substrate=None))
    assert result.status == "failed"
    assert "list_scheduled_events" in (result.error or "")


async def test_dispatch_through_the_consult_loop_returns_rows_and_coverage():
    """The ungoverned dispatch path reaches the same port method."""
    out = await _dispatch_tool(
        _port(_three_event_store(datetime.now(timezone.utc))),
        name="list_scheduled_events",
        args={},
        scope_predicate=None,
    )
    assert out["count"] == 1
    assert out["coverage"]["n"] == 1


# ---------------------------------------------------------------------------
# The consult prompt: the dated list, the honest absences, NOT MEASURED
# ---------------------------------------------------------------------------


class _Substrate:
    """A substrate stub carrying the calendar reader (and nothing else the
    run context reads — no holdings reader, no binding)."""

    def __init__(self, conn: _FakeConn, *, raises: bool = False) -> None:
        self._conn = conn
        self._raises = raises

    async def list_scheduled_events(self, **kwargs):
        if self._raises:
            raise RuntimeError("calendar read failed")
        window_start = datetime.fromisoformat(kwargs["window_start"])
        window_end = datetime.fromisoformat(kwargs["window_end"])
        return await sched.list_events(
            self._conn,
            window_start=window_start,
            window_end=window_end,
            limit=kwargs.get("limit", 50),
        )


async def test_the_prompt_renders_the_dated_line_and_the_never_assert_sentence():
    conn = _FakeConn([
        _event("Knesset vote", datetime.now(timezone.utc) + timedelta(days=5)),
    ])
    block = await pctx.render_run_context(
        substrate=_Substrate(conn), agency_binding=None,
    )
    assert "Scheduled events" in block
    assert "Knesset vote" in block
    assert "never assert that nothing is scheduled" in block
    assert pctx.RUN_CONTEXT_HEADER in block


async def test_the_events_block_is_bounded_in_characters_not_just_rows():
    """Forty-one events with 2 KB titles: the block stays under the char
    cap, every title is clipped, and the overflow is said, not hidden."""
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    conn = _FakeConn([
        _event(f"Event {i:02d} " + "x" * 2000, soon + timedelta(minutes=i))
        for i in range(pctx.MAX_SCHEDULED_EVENTS_RENDERED + 1)
    ])
    block = await pctx._events_block(_Substrate(conn))
    assert len(block) <= pctx.MAX_SCHEDULED_EVENTS_BLOCK_CHARS
    listed = [ln for ln in block.splitlines() if ln.startswith("  - ")]
    assert listed and all(
        len(ln) < pctx.MAX_SCHEDULED_EVENT_TITLE_CHARS + 80 for ln in listed
    )
    assert "x" * (pctx.MAX_SCHEDULED_EVENT_TITLE_CHARS + 1) not in block
    assert "held for the window not listed here" in block
    assert block.rstrip().endswith("never assert that nothing is scheduled.")


async def test_an_empty_table_says_the_table_is_empty():
    block = await pctx.render_run_context(
        substrate=_Substrate(_FakeConn()), agency_binding=None,
    )
    assert "EMPTY" in block
    assert "never assert that nothing is scheduled" in block


async def test_an_empty_window_over_a_non_empty_table_says_that_instead():
    conn = _FakeConn([
        _event("Far-future deadline",
               datetime.now(timezone.utc) + timedelta(days=60)),
    ])
    block = await pctx.render_run_context(
        substrate=_Substrate(conn), agency_binding=None,
    )
    assert "none held for this window" in block
    assert "EMPTY" not in block
    assert "never assert that nothing is scheduled" in block


async def test_a_failed_calendar_read_says_NOT_MEASURED():
    """The read failing is not evidence that nothing is scheduled."""
    block = await pctx.render_run_context(
        substrate=_Substrate(_FakeConn(), raises=True), agency_binding=None,
    )
    assert "NOT MEASURED" in block
    assert "EMPTY" not in block


async def test_no_reader_leaves_the_prompt_untouched():
    """A hand-built embedder without the calendar reader gets the prompt it
    always had — the block is byte-identical."""
    block = await pctx.render_run_context(substrate=object(), agency_binding=None)
    assert "Scheduled events" not in block
    assert block.strip().startswith(pctx.RUN_CONTEXT_HEADER + " Today is ")


# ---------------------------------------------------------------------------
# The loader: the example file round-trips
# ---------------------------------------------------------------------------


async def test_the_example_file_parses_into_two_valid_specs():
    specs, errors = sched.parse_events_yaml(_EXAMPLE)
    assert errors == []
    assert len(specs) == 2
    assert specs[0]["title"] == "Klarvian parliamentary elections"
    assert specs[0]["confidence"] == "confirmed"
    assert specs[1]["ends_at"] is not None


async def test_the_loader_round_trips_the_example_file_and_is_idempotent():
    """Parse → insert → read back through the real store functions; a
    second pass inserts nothing (the (title, starts_at) key)."""
    specs, errors = sched.parse_events_yaml(_EXAMPLE)
    assert not errors
    conn = _FakeConn()
    for spec in specs:
        assert await sched.insert_event(conn, provenance="seed", **spec) is not None
    # Re-run: every row is a conflict.
    for spec in specs:
        assert await sched.insert_event(conn, provenance='seed', **spec) is None
    out = await sched.list_events(
        conn,
        window_start=datetime(2027, 1, 1, tzinfo=timezone.utc),
        window_end=datetime(2028, 1, 1, tzinfo=timezone.utc),
    )
    assert out["coverage"]["total_rows"] == 2
    assert {r["title"] for r in out["rows"]} == {s["title"] for s in specs}
    assert all(r["provenance"] == "seed" for r in out["rows"])


async def test_a_bad_row_is_reported_and_skipped_not_aborted(tmp_path):
    path = tmp_path / "events.yaml"
    path.write_text(
        "events:\n"
        "  - title: ok\n    starts_at: '2027-01-01'\n"
        "  - title: no start\n"
        "  - title: bad confidence\n    starts_at: '2027-01-02'\n"
        "    confidence: maybe\n",
        encoding="utf-8",
    )
    specs, errors = sched.parse_events_yaml(path)
    assert [s["title"] for s in specs] == ["ok"]
    assert len(errors) == 2
