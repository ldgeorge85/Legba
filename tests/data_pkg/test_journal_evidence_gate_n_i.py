# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Short-id (8-hex prefix) citations resolve by a UNIQUE prefix match.

Crossroads 59fad301 (10-27) cited findings as ``[[ref:d25139d3-0000-0000-0000-
000000000000]]`` — the first 8 hex chars, zero-padded; 27 of its 28 refs are real.
Lens / inquiry / crossroads rows also write the bare ``[[ref:8f8bd8ef]]`` form.
"""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from legba.data import journal_evidence as je

T0 = datetime(2026, 10, 27, 12, 2, tzinfo=timezone.utc)


class _Conn:
    """``exact`` ids live in ``signals``; ``prefixed`` maps table -> [(id, ts)]
    served by the batched range query."""

    def __init__(self, exact=(), prefixed=None) -> None:
        self.exact = {str(x).lower() for x in exact}
        self.prefixed = prefixed or {}
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "JOIN LATERAL" in sql:
            table = sql.split(" FROM ", 2)[2].split()[0]
            out = []
            for lo, hi in zip(args[0], args[1]):
                for rid, ts in self.prefixed.get(table, ()):
                    if str(lo) <= rid <= str(hi):
                        out.append({"lo": lo, "id": rid, "ts": ts})
            return out
        if "FROM signals" in sql:
            return [{"id": str(k)} for k in args[0] if str(k).lower() in self.exact]
        return []


def _real(prefix: str) -> str:
    return prefix + "-1234-4abc-8def-0123456789ab"


def _pad(prefix: str) -> str:
    return prefix + je.PREFIX_REF_SUFFIX


def _resolve(conn, refs, before=T0):
    return asyncio.run(je.resolve_ref_existence(conn, refs, before=before))


# --- extraction ------------------------------------------------------------


def test_collect_refs_carries_padded_and_bare_short_ids_as_the_padded_form() -> None:
    body = (
        "four findings [[ref:d25139d3-0000-0000-0000-000000000000]] and "
        "[[ref:8f8bd8ef]], [[0be128b1]] and a list [a8b400c3, bd0f9532]."
    )
    assert je.collect_refs([], body) == [
        _pad("d25139d3"), _pad("8f8bd8ef"), _pad("0be128b1"),
        _pad("a8b400c3"), _pad("bd0f9532"),
    ]


def test_a_date_or_a_full_uuid_is_not_read_as_a_short_id() -> None:
    full = _real("abcdef12")
    body = f"on [20261027] the desk wrote [[ref:{full}]] and a run id {_real('cafe0001')}."
    assert je.collect_refs([], body) == [full]


def test_typographic_hyphens_inside_a_uuid_read_as_the_full_uuid() -> None:
    body = "air defence [[ref:a4878e1d‑8807‑4942‑ba3a‑792797b86ffc]]."
    assert je.collect_refs([], body) == ["a4878e1d-8807-4942-ba3a-792797b86ffc"]


# --- resolution ------------------------------------------------------------


def test_a_unique_prefix_match_created_before_resolves_and_is_counted() -> None:
    conn = _Conn(prefixed={"analyst_outputs": [(_real("d25139d3"), T0 - timedelta(hours=3))]})
    res = _resolve(conn, [_pad("d25139d3")])
    assert _pad("d25139d3") in res and res.by_prefix == {_pad("d25139d3")}
    v = je.evaluate_publish_gate(
        body="x" * 700, refs=[_pad("d25139d3")], resolved=res, entry_kind="crossroads",
    )
    assert not v.withhold and v.refs_resolved_by_prefix == 1
    assert v.as_dict()["refs_resolved_by_prefix"] == 1


def test_an_ambiguous_prefix_stays_unresolved_across_tables() -> None:
    conn = _Conn(prefixed={
        "analyst_outputs": [(_real("d25139d3"), T0 - timedelta(hours=3))],
        "proposed_edges": [("d25139d3-9999-4abc-8def-0123456789ab", T0 - timedelta(days=2))],
    })
    assert _pad("d25139d3") not in _resolve(conn, [_pad("d25139d3")])


def test_a_prefix_matching_only_a_later_row_stays_unresolved() -> None:
    conn = _Conn(prefixed={"analyst_outputs": [(_real("d25139d3"), T0 + timedelta(hours=1))]})
    assert _pad("d25139d3") not in _resolve(conn, [_pad("d25139d3")])
    # ...and the later row does not make an earlier unique match ambiguous.
    conn.prefixed["facts"] = [("d25139d3-0000-4abc-8def-0123456789ab", T0 - timedelta(days=1))]
    assert _pad("d25139d3") in _resolve(conn, [_pad("d25139d3")])


def test_no_match_stays_unresolved_and_an_invented_full_uuid_is_never_prefix_resolved() -> None:
    invented = "d25139d3-7777-4abc-8def-0123456789ab"
    conn = _Conn(prefixed={"analyst_outputs": [(_real("d25139d3"), T0 - timedelta(hours=3))]})
    res = _resolve(conn, [invented, _pad("feedface")])
    assert not res and not res.by_prefix
    # an invented full uuid never triggers a prefix probe at all
    conn2 = _Conn(prefixed=conn.prefixed)
    _resolve(conn2, [invented])
    assert not any("JOIN LATERAL" in s for s in conn2.sql)


def test_prefix_probe_is_one_batched_pk_range_query_per_table() -> None:
    conn = _Conn()
    _resolve(conn, [_pad("d25139d3"), _pad("b546fb88")])
    lateral = [s for s in conn.sql if "JOIN LATERAL" in s]
    assert len(lateral) == len(je.REF_TABLES)
    assert all("id >= p.lo AND id <= p.hi" in s and f"LIMIT {je.PREFIX_SCAN_CAP}" in s
               for s in lateral)
    assert any("issued_at" in s and "acute_forecasts" in s for s in lateral)


def test_the_59fad301_shape_passes_at_27_of_28() -> None:
    exact = [_real(f"{i:08x}") for i in range(0xa0000000, 0xa0000007)]
    prefixes = [f"{i:08x}" for i in range(0xb0000000, 0xb0000014)]
    conn = _Conn(exact=exact, prefixed={
        "analyst_outputs": [(_real(p), T0 - timedelta(hours=6)) for p in prefixes],
    })
    refs = exact + [_pad(p) for p in prefixes] + [_pad("c0ffee00")]
    v = je.evaluate_publish_gate(
        body="x" * 700, refs=refs, resolved=_resolve(conn, refs), entry_kind="crossroads",
    )
    assert not v.withhold and (v.refs_resolved, v.refs_total) == (27, 28)
    assert v.refs_resolved_by_prefix == 20
    assert "20 by unique short-id prefix" in je.verdict_detail(v)


def test_fabricated_short_ids_still_withhold() -> None:
    refs = [_pad(f"{i:08x}") for i in range(0xe0000000, 0xe0000007)]
    v = je.evaluate_publish_gate(
        body="x" * 700, refs=refs, resolved=_resolve(_Conn(), refs), entry_kind="lens",
    )
    assert v.withhold and v.reason == "refs_unresolved" and v.refs_resolved == 0


def test_as_of_recuts_per_citing_row() -> None:
    conn = _Conn(prefixed={"analyst_outputs": [(_real("d25139d3"), T0)]})
    res = _resolve(conn, [_pad("d25139d3")], before=None)
    assert _pad("d25139d3") in res
    assert _pad("d25139d3") not in res.as_of(T0)
    assert _pad("d25139d3") in res.as_of((T0 + timedelta(seconds=1)).isoformat())


def test_the_correction_script_judges_prefixes_as_of_each_row() -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "journal_correct_once.py"
    spec = importlib.util.spec_from_file_location("journal_correct_once_ni", path)
    jc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jc)
    conn = _Conn(prefixed={"analyst_outputs": [(_real("d25139d3"), T0)]})
    body = "x" * 700 + " [[ref:d25139d3]]"
    rows = [
        {"id": str(uuid4()), "entry_kind": "entry", "produced_at": T0 - timedelta(days=1),
         "body": body, "refs": []},
        {"id": str(uuid4()), "entry_kind": "entry", "produced_at": T0 + timedelta(days=1),
         "body": body, "refs": []},
    ]
    resolved = asyncio.run(je.resolve_ref_existence(conn, [_pad("d25139d3")]))
    plan = jc.plan_corrections(rows, resolved)
    assert [p["id"] for p in plan] == [rows[0]["id"]]
    assert plan[0]["refs_resolved_by_prefix"] == 0
