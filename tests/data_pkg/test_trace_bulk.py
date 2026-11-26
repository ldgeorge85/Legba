# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compressed trace bulk without a database: the bundle format, the read
helper over fake connections, the job's settings, the retention handler's
switch, and the registry routes importing under the slim image."""

from __future__ import annotations

import json
import lzma
from uuid import uuid4

import pytest

from legba.data import trace_bulk, trace_bulk_compact
from legba.data.trace_bulk import (
    TraceBulkError,
    decode_bundle,
    encode_bundle,
    hydrate_traces,
    present_sql,
)
from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe


def test_import_path_is_this_tree() -> None:
    from pathlib import Path

    import legba

    repo_root = Path(__file__).resolve().parents[2]
    assert Path(legba.__file__).resolve().is_relative_to(repo_root / "src")


_MEMBERS = [
    ("r1", {"prompt_rendered": "[SYSTEM] Ünïcödé — 中文 ✓\n" * 50,
            "input_payload": '{"a": 1, "prompt_tail": {"text": "tail"}}',
            "output_payload": '{"data": {"x": [1, 2.50, null, "é"]}}'}),
    ("r2", {"input_payload": "{}", "output_payload": "{}"}),
    ("r3", {"prompt_rendered": ""}),
    ("r4", {}),
]


def test_round_trip_is_byte_identical() -> None:
    body, raw_len = encode_bundle(_MEMBERS)
    assert raw_len > len(body)
    decoded = decode_bundle(body)
    assert decoded == {rid: cols for rid, cols in _MEMBERS}
    for rid, cols in _MEMBERS:
        for c, text in cols.items():
            assert decoded[rid][c].encode() == text.encode()


def test_similar_runs_compress_far_below_their_size() -> None:
    prompt = "You are an analyst. " * 400
    members = [(f"r{i}", {"prompt_rendered": prompt + f" run {i}",
                          "output_payload": json.dumps({"i": i})}) for i in range(32)]
    body, raw_len = encode_bundle(members)
    assert len(body) * 50 < raw_len


def test_encode_refuses_bad_members() -> None:
    with pytest.raises(TypeError):
        encode_bundle([("r", {"output_payload": {"not": "text"}})])
    with pytest.raises(ValueError):
        encode_bundle([("r", {"llm_calls": "[]"})])
    with pytest.raises(ValueError):
        encode_bundle([("r", {}), ("r", {})])


def test_corrupt_bodies_raise_never_return_wrong_text() -> None:
    body, _ = encode_bundle(_MEMBERS)
    flipped = bytearray(body)
    flipped[len(flipped) // 2] ^= 0xFF
    with pytest.raises(TraceBulkError):
        decode_bundle(bytes(flipped))
    with pytest.raises(TraceBulkError):
        decode_bundle(lzma.compress(b"not a bundle"))


def test_present_sql() -> None:
    assert present_sql("prompt_rendered", "t") == (
        "(t.prompt_rendered IS NOT NULL OR 'prompt_rendered' = ANY(t.bulk_columns))"
    )
    with pytest.raises(ValueError):
        present_sql("llm_calls")


class _FakeConn:
    """Bundles by id; ``json_codec`` mimics a pool that decodes jsonb."""

    def __init__(self, bundles: dict, *, json_codec: bool) -> None:
        self.bundles = bundles
        self.json_codec = json_codec
        self.fetches = 0

    async def fetch(self, sql, ids):
        self.fetches += 1
        return [{"bundle_id": i, "codec": trace_bulk.CODEC, "body": self.bundles[i]}
                for i in ids if i in self.bundles]

    async def fetchval(self, sql):
        assert "jsonb" in sql
        return [] if self.json_codec else "[]"


def _rows(bid):
    return [
        {"run_id": "r1", "prompt_rendered": None, "gold": None, "output_payload_bytes": None,
         "bulk_bundle_id": bid, "bulk_columns": ["prompt_rendered", "input_payload", "output_payload"]},
        {"run_id": "hot", "prompt_rendered": "hot text", "gold": {"k": 1}, "output_payload_bytes": 8,
         "bulk_bundle_id": None, "bulk_columns": None},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("json_codec", [False, True])
async def test_hydrate_restores_compacted_rows_like_the_connection_would(json_codec) -> None:
    bid = uuid4()
    body, _ = encode_bundle(_MEMBERS)
    conn = _FakeConn({bid: body}, json_codec=json_codec)
    out = await hydrate_traces(conn, _rows(bid), aliases={"output_payload": "gold"})
    cold, hot = out
    assert cold["prompt_rendered"] == _MEMBERS[0][1]["prompt_rendered"]
    text = _MEMBERS[0][1]["output_payload"]
    assert cold["gold"] == (json.loads(text) if json_codec else text)
    assert cold["output_payload_bytes"] == len(text.encode())
    assert "input_payload" not in cold  # not selected, not added
    assert hot == {"run_id": "hot", "prompt_rendered": "hot text", "gold": {"k": 1},
                   "output_payload_bytes": 8}
    assert all("bulk_bundle_id" not in r and "bulk_columns" not in r for r in out)
    assert conn.fetches == 1


@pytest.mark.asyncio
async def test_hydrate_touches_nothing_when_no_row_is_compacted() -> None:
    conn = _FakeConn({}, json_codec=False)
    out = await hydrate_traces(conn, [_rows(None)[1]])
    assert conn.fetches == 0 and out[0]["prompt_rendered"] == "hot text"


@pytest.mark.asyncio
async def test_hydrate_refuses_rows_without_pointers_and_missing_bundles() -> None:
    conn = _FakeConn({}, json_codec=False)
    with pytest.raises(TraceBulkError, match="selected without"):
        await hydrate_traces(conn, [{"run_id": "x", "prompt_rendered": None}])
    with pytest.raises(TraceBulkError, match="missing"):
        await hydrate_traces(conn, _rows(uuid4())[:1])
    bid = uuid4()
    body, _ = encode_bundle([("other", {})])
    with pytest.raises(TraceBulkError, match="not in bundle"):
        await hydrate_traces(_FakeConn({bid: body}, json_codec=False), _rows(bid)[:1])
    body, _ = encode_bundle([("r1", {"prompt_rendered": "x"})])
    with pytest.raises(TraceBulkError, match="moved"):
        await hydrate_traces(_FakeConn({bid: body}, json_codec=False), _rows(bid)[:1])


def test_settings_default_off_and_floor(monkeypatch) -> None:
    for k in ("LEGBA_TRACE_COMPRESS", "LEGBA_TRACE_COMPRESS_AFTER_DAYS",
              "LEGBA_TRACE_COMPRESS_BUNDLE_SIZE", "LEGBA_TRACE_COMPRESS_MAX_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    s = trace_bulk_compact.settings_from_env()
    assert (s.enabled, s.age_days, s.bundle_size, s.max_seconds) == (False, 30, 32, 300)
    monkeypatch.setenv("LEGBA_TRACE_COMPRESS", "1")
    monkeypatch.setenv("LEGBA_TRACE_COMPRESS_AFTER_DAYS", "3")
    monkeypatch.setenv("LEGBA_TRACE_COMPRESS_BUNDLE_SIZE", "100000")
    s = trace_bulk_compact.settings_from_env()
    assert s.enabled and s.age_days == trace_bulk_compact.MIN_AGE_DAYS
    assert s.bundle_size == trace_bulk_compact.MAX_BUNDLE_SIZE


def test_recent_window_sql_readers_sit_inside_the_compaction_floor() -> None:
    """The frame guard filters output_payload in SQL over its lookback; that is
    correct only while compaction never reaches that far."""
    from legba.data import _frame_content

    assert _frame_content._GUARD_LOOKBACK_HOURS / 24 < trace_bulk_compact.MIN_AGE_DAYS


@pytest.mark.asyncio
async def test_compact_switched_off_touches_nothing() -> None:
    class _Boom:
        def acquire(self):  # pragma: no cover - must not be called
            raise AssertionError("pool touched")

    stats = await trace_bulk_compact.compact(_Boom(), trace_bulk_compact.CompactSettings())
    assert stats.bundles == 0 and stats.traces == 0


@pytest.mark.asyncio
async def test_retention_finding_is_unchanged_with_the_switch_off(monkeypatch) -> None:
    from legba.data.analysts.deterministic_handlers import analyst_traces_retention

    monkeypatch.delenv("LEGBA_TRACE_COMPRESS", raising=False)
    res = await analyst_traces_retention.handle([], {"ttl_days": 0}, None)
    assert not any(k.startswith("compress_") for k in res.finding.data)
    monkeypatch.setenv("LEGBA_TRACE_COMPRESS", "1")
    res = await analyst_traces_retention.handle([], {"ttl_days": 0}, None)  # no pool
    assert not any(k.startswith("compress_") for k in res.finding.data)


def test_trace_readers_import_in_the_slim_registry_image() -> None:
    """Every registry route that reads trace bulk, and the read helper, import
    with the slim image's absent modules poisoned (zstandard included: the
    registry image does not carry it) and pull in no analyst/runtime module."""
    _assert_ok(_probe(
        """
        sys.modules["zstandard"] = None
        import legba.data.registry.server as server
        import legba.data.registry.lineage_api as lineage
        import legba.data.registry.layers_api as layers
        import legba.data.registry.flow_api as flow
        import legba.data.record_sheet_reads as sheet
        import legba.data.trace_bulk as tb

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the trace readers: %r" % leaked
        assert "bulk_bundle_id" in lineage._TRACE_COLUMNS
        assert "bulk_columns" in flow.RUN_SQL and "bulk_columns" in layers._NEWEST_RECEIPT_SQL
        body, _ = tb.encode_bundle([("r", {"prompt_rendered": "p", "output_payload": "{}"})])
        assert tb.decode_bundle(body) == {"r": {"prompt_rendered": "p", "output_payload": "{}"}}
        print("OK")
        """
    ))
