# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane E2 — the record readers' ``known_at`` cut, and ``live_series`` (pure).

Fakes only (a recording pool; no database). Pinned here:

* the classification is COMPLETE for the record arm: every record tool is
  classified, every ``cut`` record reader takes ``known_at`` on the port AND
  the Protocol, and the router forwards the cut (never from the planner's args)
  — the inventory guard pins the arm's tool list to the table;
* with no ``known_at`` every statement a record reader renders is
  byte-identical to the base (sha256 of the statements rendered at 85dbe171);
* with one: the record-entry clause is in the statement with K bound, the
  readers' own clocks are clamped to K, and what reads today's state refuses;
* the record arm's binding forwards the run's as-of as the cut;
* ``live_series``: a separate reader, every row labelled live with its source,
  bounds declared in the schema and enforced by the reader.

The database-backed proof (a row recorded after K is invisible through the
production port and dispatcher) is ``test_record_reader_known_at_db.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import pathlib
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from legba.data import raw_reader_known_at as K
from legba.data.analysts.consult_substrate_port import SubstrateQueryPort
from legba.data.analysts.consult_tool_routing import _dispatch_tool
from legba.runtime import _observations_live_read as live
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort as Port

ROOT = pathlib.Path(__file__).resolve().parents[2]
KAT = datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
EID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _Rec:
    """A pool + connection that records every statement and answers with
    minimal shapes (a loaded collection, a profile, an event)."""

    def __init__(self) -> None:
        self.sql: list[tuple[str, tuple]] = []

    def acquire(self) -> "_Rec":
        return self

    async def __aenter__(self) -> "_Rec":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def fetch(self, sql: str, *a: Any) -> list:
        self.sql.append((sql, a))
        if "FROM collection_descriptors" in sql and "state = 'loaded'" in sql:
            return [{"descriptor_id": "c1", "collection_version": "v", "licence_class": "x",
                     "origin_class": "archive"}]
        return []

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.sql.append((sql, a))
        return 0

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.sql.append((sql, a))
        if "FROM entity_profiles" in sql:
            return {"id": EID, "canonical_name": "X", "entity_type": "person", "entity_class": None,
                    "version": 3, "completeness_score": 0.5, "last_event_link_at": None,
                    "last_verified_at": KAT + timedelta(days=3), "geo_country": None, "geo_region": None,
                    "produced_at": None, "analyst_id": "a", "target_id": None}
        if "FROM events e WHERE e.id" in sql:
            row = {k: None for k in (
                "event_signature analyst_id title summary category event_type severity lifecycle_state "
                "lifecycle_changed_at time_start time_end geo geo_lat geo_lon locations confidence signal_count "
                "distinct_source_count oversized source_method source_type target_id target_version "
                "analyst_version run_id origin_class collection_id valid_from valid_until superseded_by "
                "produced_at created_at updated_at").split()}
            return row | {"id": EID, "updated_at": KAT + timedelta(days=2), "lifecycle_state": "resolved",
                          "signal_count": 99, "superseded_by": uuid.uuid4()}
        return None


def _sha(stmts: list[str]) -> str:
    return hashlib.sha256("\n--\n".join(stmts).encode()).hexdigest()


def _record_arm_tools() -> tuple[str, ...]:
    sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))
    import arm_open

    return arm_open.RECORD_TOOLS


# ---------------------------------------------------------------------------
# 1. The classification is complete, and every cut reader takes the cut
# ---------------------------------------------------------------------------


def test_the_record_arm_is_exactly_the_classified_set():
    assert set(_record_arm_tools()) == set(K.RECORD_KNOWN_AT_CUTS)
    for name, c in K.RECORD_KNOWN_AT_CUTS.items():
        assert c.kind in (K.CUT, K.EXEMPT) and c.how.strip(), name
    assert K.RECORD_KNOWN_AT_CUTS["list_targets"].kind == K.EXEMPT


RECORD_CUT = sorted(t for t, c in {**K.RECORD_KNOWN_AT_CUTS, **K.LIVE_KNOWN_AT_CUTS}.items() if c.kind == K.CUT)


@pytest.mark.parametrize("tool", RECORD_CUT)
def test_every_cut_record_reader_takes_known_at_on_the_port_and_the_protocol(tool):
    assert "known_at" in inspect.signature(getattr(Port, tool)).parameters
    assert "known_at" in inspect.signature(getattr(SubstrateQueryPort, tool)).parameters


class _Port:
    """Records the kwargs every reader was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        async def reader(**kw: Any) -> dict:
            self.calls.append((name, kw))
            return {"rows": [], "refs": [], "count": 0}
        return reader


_ARGS = {
    "list_findings": {"target_id": "t", "include_superseded": True},
    "belief_as_of": {"as_of": "2026-11-01"},
    "query_facts": {"subject": "x"},
    "inspect_entity": {"name": "x"},
    "query_events": {"target_id": "t"},
    "inspect_event": {"event_id": str(EID)},
    "series_history": {"series_id": "s", "subject": "US", "from": "2016", "to": "2025"},
    "series_compare": {"series_id": "s", "subjects": ["US"], "from": "2016", "to": "2025"},
    "live_series": {"series_id": "s", "subject": "US"},
}


@pytest.mark.parametrize("tool", RECORD_CUT)
def test_dispatch_forwards_known_at_to_record_readers_never_from_args(tool):
    p = _Port()
    later = (KAT + timedelta(days=9)).isoformat()
    out = asyncio.run(_dispatch_tool(p, name=tool, args={**_ARGS[tool], "known_at": later},
                                     scope_predicate=None, known_at=KAT))
    assert "error" not in out, out
    assert p.calls[0][0] == tool and p.calls[0][1]["known_at"] == KAT
    # without the cut, the reader is never handed the kwarg
    p = _Port()
    asyncio.run(_dispatch_tool(p, name=tool, args=_ARGS[tool], scope_predicate=None))
    assert "known_at" not in p.calls[0][1]


def test_every_known_tool_reaches_the_port_through_the_router():
    """No tool in the router's whitelist falls through to ``unreachable``."""
    from legba.data.analysts.consult_tool_routing import _KNOWN_TOOLS

    for name in sorted(_KNOWN_TOOLS):
        p = _Port()
        out = asyncio.run(_dispatch_tool(p, name=name, args=dict(_ARGS.get(name, {})), scope_predicate=None))
        assert out != {"error": "unreachable"}, name
        assert p.calls and p.calls[0][0] == name, name


# ---------------------------------------------------------------------------
# 2. Byte-identical without a cut
# ---------------------------------------------------------------------------

#: sha256 of each scenario's statements rendered by the base (85dbe171), no
#: known_at — the same recording pool, the same call.
_BASE = {
    "list_findings": ({}, "106be315a3b15c3283e4dc74254e802c5c4d7f8da119f316fe69039f29c745b9"),
    "list_findings_superseded": (
        {"include_superseded": True, "since_hours": 48, "analyst_id": "a"},
        "b2aebdf86fd55b5e9fd5f00602da1f88706671eb88f5c94d55c2e50206e92253"),
    "list_findings_believed": (
        {"believed_as_of": "2026-11-01T00:00:00Z"},
        "8ea4dd61e640e9d764e7f4869f2f56fef248bc866a80039544dff47d8747b922"),
    "list_findings_verified": (
        {"verified_only": True}, "3bd65e294a6f9be4ef162f295a3836a30b89055329746419768f487a14a87820"),
    "belief_as_of": (
        {"as_of": "2026-11-01T00:00:00Z", "target_id": "t"},
        "7fe09905e48c42483d75a02c3a19f987cb633e670382efdfdd98454c8316e79d"),
    "belief_as_of_verified": (
        {"as_of": "2026-11-01T00:00:00Z", "verified_only": True},
        "bf3bf3254e552f2a0686a54529214e8d84e4703458f4212b2291d57ec90e8be3"),
    "query_facts": ({"subject": "iran"}, "9814b589e476ee11532b0e374074dfdde5c38c86c21d3556066fc6b8028fcaae"),
    "query_facts_as_of": (
        {"subject": "iran", "as_of": "2026-11-01T00:00:00Z"},
        "edbcad7992a53bb3bd090c4f28f5de548e5508239c31e69b0c9e09398b279e48"),
    "inspect_entity": ({"name": "X"}, "97a5091a5d3f16bd1edc1085978051a2783763e76f94cce507ab0ba32efcf199"),
    "query_events": (
        {"target_id": "t", "lifecycle_state": "active"},
        "749060c9eee4d5335d57df09092a31a71bf8b409a53e3c1321c495c88bd95ada"),
    "query_events_as_of": (
        {"as_of": "2026-11-01T00:00:00Z", "since": "2026-10-01T00:00:00Z"},
        "bcc8c721a389a3b902cb54d953a08b46627524d4c0d1cddeba1591aeb5449b2f"),
    "inspect_event": ({"event_id": str(EID)}, "24b02204f1780355f24ef7cf04d603ece1b62584a5b3ed6751e9ef01ad0b6e45"),
    "series_history": (
        {"series_id": "s", "subject": "US", "since": "2016", "until": "2025"},
        "7ebb7b8ce2e803e5f038df109f266540390124d300fc842d9a37ec7b0320775c"),
    "series_compare": (
        {"series_id": "s", "subjects": ["US", "IL"], "since": "2016", "until": "2025", "as_of": "2024-01-01"},
        "b369845d098a79309f7075e343ae682f7e61c09145a186f6f34b1d5fa528043d"),
}


def _tool(key: str) -> str:
    return next(t for t in sorted(K.RECORD_KNOWN_AT_CUTS, key=len, reverse=True)
                if key == t or key.startswith(t + "_"))


@pytest.mark.parametrize("key", sorted(_BASE))
def test_record_reader_sql_is_byte_identical_without_known_at(key):
    kw, sha = _BASE[key]
    r = _Rec()
    asyncio.run(getattr(Port(pg_pool=r, qdrant_client=None), _tool(key))(**kw))
    assert _sha([s for s, _ in r.sql]) == sha


# ---------------------------------------------------------------------------
# 3. With a cut: the clause, the clamp, the refusals
# ---------------------------------------------------------------------------


def _run(tool: str, **kw: Any) -> tuple[dict, _Rec]:
    r = _Rec()
    out = asyncio.run(getattr(Port(pg_pool=r, qdrant_client=None), tool)(known_at=KAT, **kw))
    return out, r


def test_list_findings_cut_reads_the_heads_live_at_known_at():
    out, r = _run("list_findings", believed_as_of="2026-12-01T00:00:00Z", since_hours=24)
    sql, params = r.sql[0]
    assert "f.produced_at <= $1 AND (f.superseded_at IS NULL OR f.superseded_at > $1)" in sql
    assert "f.created_at < $2" in sql and "cr.produced_at <= $1" in sql
    assert params[0] == KAT and params[1] == KAT          # believed_as_of after K -> K
    assert params[2] == KAT - timedelta(hours=24)          # since_hours counts back from K
    assert out["known_at_cut"]["known_at"] == KAT.isoformat()
    # include_superseded: every finding recorded before K, critic dated to K
    out, r = _run("list_findings", include_superseded=True)
    sql, params = r.sql[0]
    assert "superseded" not in sql.split("WHERE", 1)[1].split("ORDER")[0].replace("f.superseded_by", "")
    assert "f.created_at < $1" in sql and "cr.produced_at <= $1" in sql and params[0] == KAT


def test_list_findings_survey_refuses_under_known_at():
    out, r = _run("list_findings", mode="survey")
    assert out["rows"] == [] and "survey mode" in out["error"] and r.sql == []


def test_belief_as_of_clamps_and_refuses_the_latest_fold():
    out, r = _run("belief_as_of", as_of="2027-01-01T00:00:00Z")
    sql, params = r.sql[0]
    assert params[0] == KAT and params[1] == KAT and "f.created_at < $2" in sql
    assert out["as_of"] == KAT.isoformat()
    out, r = _run("belief_as_of", as_of="2026-10-01T00:00:00Z", fold_verdicts="latest")
    assert "fold_verdicts='latest'" in out["error"] and r.sql == []


def test_query_facts_cut_is_the_validity_read_at_known_at():
    out, r = _run("query_facts", subject="iran")
    sql, params = r.sql[0]
    assert "valid_until IS NULL OR valid_until > $1" in sql and "created_at < $2" in sql
    assert "superseded_by IS NULL" not in sql
    assert params[:2] == (KAT, KAT) and out["as_of"] == KAT.isoformat()


def test_inspect_entity_cut_rebuilds_the_profile_at_known_at():
    out, r = _run("inspect_entity", name="X")
    stmts = [s for s, _ in r.sql]
    assert "created_at < $2" in stmts[0]
    assert "created_at < $3" in stmts[1]
    assert "sel.created_at < $3 AND s.created_at < $3" in stmts[2]
    assert "created_at < $3" in stmts[3] and "valid_until > $3" in stmts[3]
    assert all(a[-1] == KAT for _, a in r.sql)
    p = out["profile"]
    assert p["version"] is None and p["completeness_score"] is None
    assert p["last_verified_at"] is None                    # stamped after K: masked
    assert out["known_at_cut"]["masked_after_known_at"] >= 2


def test_query_events_cut_filters_lifecycle_at_known_at():
    out, r = _run("query_events", target_id="t", lifecycle_state="active", entity="x",
                  as_of="2027-01-01T00:00:00Z")
    sql, params = r.sql[0]
    assert "e.created_at < $2" in sql and params[0] == KAT and params[1] == KAT
    assert "(SELECT l.state_to FROM event_lifecycle_events l WHERE l.event_id = e.id" in sql
    assert "e.lifecycle_state = $" not in sql
    assert "eel.created_at < $2" in sql
    assert out["known_at_cut"]["known_at"] == KAT.isoformat()


def test_inspect_event_cut_every_section_and_rebuilds_the_event():
    out, r = _run("inspect_event", event_id=str(EID))
    stmts = [s for s, _ in r.sql]
    assert "e.created_at < $2" in stmts[0]
    for s in stmts[1:6]:
        assert "< $3" in s, s
    assert "superseded_by IS NULL" not in stmts[3]
    ev = out["event"]
    # rebuilt from the (empty) ledger and links at K; undated successor dropped
    assert ev["lifecycle_state"] is None and ev["signal_count"] == 0 and ev["superseded_by"] is None
    assert ev["updated_at"] is None
    assert out["known_at_cut"]["masked_after_known_at"] >= 1


def test_inspect_event_not_in_the_record_at_known_at():
    class _NoEvent(_Rec):
        async def fetchrow(self, sql, *a):
            self.sql.append((sql, a))
            return None

    r = _NoEvent()
    out = asyncio.run(Port(pg_pool=r, qdrant_client=None).inspect_event(event_id=str(EID), known_at=KAT))
    assert out["found"] is False and "known_at" in out["error"] and len(r.sql) == 1


def test_series_cut_on_load_time_and_record_time_clamped():
    out, r = _run("series_history", series_id="s", subject="US", since="2016", until="2025",
                  as_of="2030-01-01")
    sql, params = r.sql[-1]
    assert "AND o.loaded_at < $8\n     ORDER BY" in sql
    assert params[5] == KAT and params[7] == KAT
    assert out["as_of"] == KAT.isoformat() and out["known_at_cut"]["known_at"] == KAT.isoformat()


# ---------------------------------------------------------------------------
# 4. The record arm's binding cuts at the run's as-of
# ---------------------------------------------------------------------------


def test_the_record_binding_forwards_the_runs_as_of():
    sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))
    import arm_open

    p = _Port()
    b = arm_open.RecordBinding(p, KAT)
    out = asyncio.run(b.run_tool("list_findings", {"target_id": "t", "known_at": "2030-01-01"}))
    assert out.admitted and p.calls[0][1]["known_at"] == KAT
    out = asyncio.run(b.run_tool("search_corpus", {"query": "x"}))
    assert out.admitted is False


# ---------------------------------------------------------------------------
# 5. live_series — labelled, bounded, cut
# ---------------------------------------------------------------------------


class _LiveRec(_Rec):
    def __init__(self, rows: list[dict] | None = None) -> None:
        super().__init__()
        self.rows = rows or []

    async def fetch(self, sql, *a):
        self.sql.append((sql, a))
        return self.rows


def _obs(valid: date, record: datetime, value: str) -> dict:
    return {"id": uuid.uuid4(), "collection_id": "live:source.x", "source_id": "source.x",
            "series_id": "s", "subject": "US", "subject_kind": "country", "period": "day",
            "valid_from": valid, "valid_to": valid, "record_time": record, "record_time_basis": "provider",
            "loaded_at": record + timedelta(minutes=1), "value": Decimal(value), "value_text": None,
            "unit": "percent", "source_url": "https://x.invalid", "fetch_sha256": "a" * 64,
            "methodology_version": "m1", "origin_class": "live"}


def test_the_schema_declares_the_readers_bounds():
    from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS, WRITE_TOOLS

    spec = TOOL_SCHEMAS["live_series"]
    assert spec.json_schema["properties"]["limit"]["maximum"] == live.MAX_LIVE_ROWS
    assert f"at most {live.MAX_WINDOW_DAYS} days" in spec.description
    assert f"last {live.DEFAULT_WINDOW_DAYS} days" in spec.description
    assert f"At most {live.MAX_LIVE_ROWS} rows" in spec.description
    assert "live_series" not in WRITE_TOOLS


def test_window_read_labels_every_row_live_with_revisions():
    d = date(2026, 11, 1)
    rows = [_obs(d, datetime(2026, 11, 2, tzinfo=timezone.utc), "4.5"),
            _obs(d, datetime(2026, 11, 3, tzinfo=timezone.utc), "4.25"),
            _obs(date(2026, 11, 2), datetime(2026, 11, 3, tzinfo=timezone.utc), "4.25")]
    r = _LiveRec(rows)
    out = asyncio.run(live.read_live_series(r, series_id="s", subject="US", since="2026-10-01", until="2026-11-10"))
    assert out["mode"] == "window" and out["count"] == 3 and out["revised_periods"] == 1
    assert all(x["origin_class"] == "live" and x["label"] == "LIVE" and x["source_id"] == "source.x" for x in out["rows"])
    assert [x["is_latest_revision"] for x in out["rows"]] == [False, True, True]
    assert all(x["record_time"] and x["loaded_at"] for x in out["rows"])
    assert out["refs"] == [x["observation_id"] for x in out["rows"]]
    sql, params = r.sql[0]
    assert "o.origin_class = 'live'" in sql and params[4] == live.DEFAULT_ROWS + 1


def test_window_and_row_caps_are_enforced():
    r = _LiveRec()
    out = asyncio.run(live.read_live_series(r, series_id="s", subject="US", since="2024-01-01", until="2026-01-01"))
    assert "at most 366" in out["error"] and r.sql == []
    out = asyncio.run(live.read_live_series(r, series_id="s", subject="US", limit=10_000))
    assert r.sql[-1][1][4] == live.MAX_LIVE_ROWS + 1
    d = date(2026, 11, 1)
    many = [_obs(d, datetime(2026, 11, 2, tzinfo=timezone.utc), "1")] * 4
    out = asyncio.run(live.read_live_series(_LiveRec(many), series_id="s", subject="US", limit=3))
    assert out["count"] == 3 and out["truncated"] is True


def test_catalogue_mode_and_the_cut():
    r = _LiveRec()
    out = asyncio.run(live.read_live_series(r))
    assert out["mode"] == "catalogue" and out["series"] == []
    sql, params = r.sql[0]
    assert "GROUP BY" in sql and "o.loaded_at" not in sql.split("FROM")[1] and params == (live.MAX_CATALOGUE_LINES,)
    r = _LiveRec()
    out = asyncio.run(live.read_live_series(r, source_id="source.x", known_at=KAT))
    sql, params = r.sql[0]
    assert "o.loaded_at < $2" in sql and "o.record_time <= $2" in sql and params == ("source.x", KAT, live.MAX_CATALOGUE_LINES)
    assert out["known_at_cut"]["known_at"] == KAT.isoformat()
    r = _LiveRec()
    asyncio.run(live.read_live_series(r, series_id="s", subject="US", source_id="source.x", known_at=KAT))
    sql, params = r.sql[0]
    assert "o.source_id = $6" in sql and "o.loaded_at < $7" in sql and params[3] == KAT.date()


def test_the_history_reads_never_select_a_live_row():
    """The 0246 firewall stands: the history SQL keys on loaded collection ids,
    and a live row's collection id ('live:<source>') is never one."""
    from legba.runtime import _observations_read as hist

    for sql in (hist._SERIES_HISTORY_SQL, hist._SERIES_COMPARE_SQL, hist._LATEST_PER_SERIES_SQL):
        assert "o.collection_id = ANY($1::text[])" in sql
    assert "state = 'loaded'" in hist._LOADED_COLLECTIONS_SQL
