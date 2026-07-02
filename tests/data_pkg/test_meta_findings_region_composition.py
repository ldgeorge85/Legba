# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2-T2 — per-REGION composition READ_SLICE + prompt selection.

The region composition runs the EXISTING meta_findings_synthesizer kind
target-scoped over a REGION FRAME (``target_filter='region_<slug>'``). Unlike the
per-COUNTRY branch (which scopes ``f.target_id=<country>`` and would match nothing
for a frame), a region run is a NEW 4th mode:

  * READ_SLICE detects the ``region_`` prefix, resolves the frame → its member
    country desks (targets whose ``scope.tags`` carry the SAME ``region_<slug>``
    tag), and reads THEIR country_composition heads as a SET
    (``f.target_id = ANY(...)``) — verify-floored, ``include_meta=True``
    (country_composition rows are ``meta=True``), one HEAD per member country;
  * a region with NO member desks reads an EMPTY set (an honest gap, NOT an
    unscoped whole-pool read);
  * the run selects the WORLD-shaped ``_WORLD_COMPOSITION_SYSTEM`` prompt (a region
    read is MULTI-country → cross-country hedge + disagreement), NOT the
    single-country ``_COMPOSITION_SYSTEM``;
  * the per-COUNTRY, WORLD, and legacy paths are UNCHANGED (a non-region
    ``target_filter`` never hits the members query and keeps the single-equality
    country scope).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Fake asyncpg.Connection that records each fetch() call's params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return list(self._rows)


class _RegionConn:
    """Fake conn routing the two READ_SLICE queries by SQL content:

      * the region-members query (``target_descriptors``) → member desk rows;
      * the slice query (``analyst_outputs``) → country_composition head rows.
    """

    def __init__(
        self,
        *,
        member_ids: list[str],
        slice_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._member_ids = member_ids
        self._slice_rows = slice_rows or []
        self.member_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.slice_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        if "target_descriptors" in query:
            self.member_calls.append((query, params))
            return [{"descriptor_id": m} for m in self._member_ids]
        self.slice_calls.append((query, params))
        return list(self._slice_rows)


def _region_descriptor() -> SimpleNamespace:
    """A region composition descriptor stub: subscription.other_analysts =
    [country_composition] (READ_SLICE id resolution), a targets block present."""
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(
                    id="country_composition", time_window="24h", data_types=[]
                )
            ],
            targets=SimpleNamespace(predicate='has_tag("region")'),
        )
    )


class _CannedLLM:
    """LLM double returning a fixed payload; captures the system prompt."""

    subprovider = "region_test_double"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": list(messages), "system": system})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("LLM must not be called on the empty-slice path")


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _country_read_row(*, uid: UUID, target_id: str, title: str) -> dict[str, Any]:
    """A verify-floored country_composition head row (as the region slice returns
    it — carries ``effective_confidence`` + ``faithfulness_score``)."""
    return {
        "id": uid,
        "kind": "finding",
        "title": title,
        "body": f"{title} read body",
        "confidence": 0.7,
        "effective_confidence": 0.6,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"evidence": [], "meta": True},
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": "country_composition",
        "analyst_version": "vtest",
        "produced_at": "2026-07-01T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


_MENA_MEMBERS = [
    "country_g20_sa",
    "country_g20_tr",
    "country_watch_il",
    "country_watch_ir",
]


# ---------------------------------------------------------------------------
# _is_region_target — the mode discriminator
# ---------------------------------------------------------------------------


def test_is_region_target_discriminates_frame_from_country_and_none():
    assert synth._is_region_target("region_mena") is True
    assert synth._is_region_target("region_europe") is True
    assert synth._is_region_target("country_g20_in") is False
    assert synth._is_region_target(None) is False
    assert synth._is_region_target("") is False


# ---------------------------------------------------------------------------
# _resolve_region_member_target_ids — members query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_region_members_queries_tag_excludes_frame_orders():
    conn = _RegionConn(member_ids=["country_g20_sa", "country_watch_ir"])
    out = await synth._resolve_region_member_target_ids(conn, "region_mena")
    assert out == ["country_g20_sa", "country_watch_ir"]
    # The tag-membership query fired with the region slug as the ONLY param.
    mq, mp = conn.member_calls[0]
    assert "target_descriptors" in mq
    assert "(body -> 'scope' -> 'tags') ? $1" in mq  # element-test on the slug tag
    assert "descriptor_id <> $1" in mq               # the frame excludes itself
    assert "is_head = TRUE" in mq and "state = 'active'" in mq
    assert mp == ("region_mena",)


# ---------------------------------------------------------------------------
# READ_SLICE region branch — resolve members + read their heads as a SET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_region_read_slice_resolves_members_and_reads_head_set():
    """A region ``target_filter`` resolves member desks, then reads their
    country_composition heads as a target-id SET with the composition gates."""
    desc = _region_descriptor()
    conn = _RegionConn(member_ids=_MENA_MEMBERS)
    await synth.READ_SLICE(conn, descriptor=desc, target_filter="region_mena")

    # (1) the members query ran for THIS region slug.
    assert conn.member_calls, "region run must resolve member desks"
    assert conn.member_calls[0][1] == ("region_mena",)

    # (2) the slice query is a SET scope, NOT the single-country equality.
    assert conn.slice_calls, "region run must read the member heads"
    sq, sp = conn.slice_calls[0]
    assert "f.target_id = ANY($3::TEXT[])" in sq
    assert "f.target_id = $3" not in sq          # not the single-country form
    assert sp[2] == _MENA_MEMBERS

    # (3) verify-floor gate + head-fold + meta-inclusive (country reads are meta).
    assert "JOIN LATERAL" in sq
    assert "Faithfulness verify%" in sq
    assert "f.superseded_by IS NULL" in sq
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in sq
    assert "'meta'" not in sq                     # include_meta → exclusion dropped

    # (4) analyst set = country_composition; floor = the default (last param).
    assert sp[0] == ["country_composition"]
    assert sp[-1] == synth.DEFAULT_VERIFY_FLOOR


@pytest.mark.asyncio
async def test_region_read_slice_fuses_at_least_three_member_heads():
    """Accept: a region_mena run FUSES >=3 country reads (SA/TR/IL/IR)."""
    desc = _region_descriptor()
    slice_rows = [
        _country_read_row(uid=uuid4(), target_id=t, title=t) for t in _MENA_MEMBERS
    ]
    conn = _RegionConn(member_ids=_MENA_MEMBERS, slice_rows=slice_rows)
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter="region_mena")
    assert len(rows) >= 3
    assert {r["target_id"] for r in rows} == set(_MENA_MEMBERS)


@pytest.mark.asyncio
async def test_region_with_no_members_reads_empty_set_not_unscoped():
    """A region with NO member desks reads an EMPTY set (0 rows) — the honest gap
    — NEVER an unscoped whole-pool read."""
    desc = _region_descriptor()
    conn = _RegionConn(member_ids=[])
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter="region_mena")
    assert rows == []
    # The slice query STILL ran, still SET-scoped, with the empty member list.
    sq, sp = conn.slice_calls[0]
    assert "f.target_id = ANY($3::TEXT[])" in sq
    assert sp[2] == []


# ---------------------------------------------------------------------------
# Per-COUNTRY / WORLD paths UNCHANGED — a non-region target_filter never hits the
# members query and keeps the single-equality country scope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_country_target_filter_is_unchanged_no_members_query():
    """A per-COUNTRY ``target_filter`` does NOT resolve members and keeps the
    single-country ``f.target_id = $3`` equality (byte-for-byte)."""
    desc = _region_descriptor()
    conn = _RegionConn(member_ids=["must-not-be-queried"])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter="country_g20_in")
    assert conn.member_calls == []               # no region member resolution
    sq, sp = conn.slice_calls[0]
    assert "f.target_id = $3" in sq
    assert "f.target_id = ANY(" not in sq        # not the region SET form
    assert sp[2] == "country_g20_in"


@pytest.mark.asyncio
async def test_world_target_none_is_unchanged_no_members_query():
    """A GLOBAL run (``target_filter=None``) never resolves members."""
    desc = _region_descriptor()
    conn = _RegionConn(member_ids=["must-not-be-queried"])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    assert conn.member_calls == []


# ---------------------------------------------------------------------------
# read_other_analyst_findings — the target_ids SET filter (direct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_reader_target_ids_set_filter_with_gates():
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn,
        analyst_ids=["country_composition"],
        target_ids=["country_g20_sa", "country_g20_tr"],
        verify_floor=0.0,
        include_meta=True,
    )
    q, p = conn.calls[0]
    assert "f.target_id = ANY($3::TEXT[])" in q
    assert p[2] == ["country_g20_sa", "country_g20_tr"]
    assert "f.superseded_by IS NULL" in q
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in q
    assert "'meta'" not in q                      # include_meta True


@pytest.mark.asyncio
async def test_direct_reader_empty_target_ids_still_scoped():
    """An empty ``target_ids`` keeps the ANY filter (0 rows), NOT an unscoped read
    — guarded on ``is not None`` rather than truthiness."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["country_composition"], target_ids=[]
    )
    q, p = conn.calls[0]
    assert "f.target_id = ANY($3::TEXT[])" in q
    assert p[2] == []
    # dedupe still engaged (target_ids is not None).
    assert "f.superseded_by IS NULL" in q
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in q


@pytest.mark.asyncio
async def test_direct_reader_target_id_wins_over_target_ids():
    """When both are passed the single ``target_id`` wins (the branches are an
    if/elif) — a defensive contract, not a real call shape."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["a"], target_id="country_g20_de", target_ids=["x", "y"]
    )
    q, p = conn.calls[0]
    assert "f.target_id = $3" in q
    assert "f.target_id = ANY(" not in q         # the SET form is NOT used
    assert p[2] == "country_g20_de"


@pytest.mark.asyncio
async def test_direct_reader_legacy_path_unchanged_no_target_ids():
    """No ``target_id`` / ``target_ids`` / ``include_meta`` → the legacy global-meta
    query is byte-for-byte (no SET filter, no dedupe)."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["a", "b"], time_window_hours=24
    )
    q, p = conn.calls[0]
    assert "f.target_id = ANY(" not in q         # no SET filter
    assert "target_id =" not in q
    assert "superseded_by" not in q
    assert "DISTINCT ON" not in q
    assert p == (["a", "b"], 24)


# ---------------------------------------------------------------------------
# End-to-end _run — a region target selects the WORLD prompt, cites the country
# reads, and stamps derived_from = exactly the fused country_composition heads.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_region_run_selects_world_prompt_and_cites_country_reads():
    sa, ir = uuid4(), uuid4()
    rows = [
        _country_read_row(uid=sa, target_id="country_g20_sa", title="Saudi Arabia"),
        _country_read_row(uid=ir, target_id="country_watch_ir", title="Iran"),
    ]
    # Ordinal 1 → the first rendered country read (sa), 2 → the second (ir).
    # [[ref:9]] is out of range (only 2 reads) → fabricated → dropped.
    body = (
        "BLUF: the region's country reads diverge. Saudi Arabia looks stable "
        "[[ref:1]], while Iran is escalating [[ref:2]]. An unsupported aside "
        "[[ref:9]] must never be cited."
    )
    llm = _CannedLLM(
        {
            "title": "MENA region composition",
            "body": body,
            "confidence": 0.5,
            "evidence": ["country reads diverge"],
            "tags": ["region"],
        }
    )
    result = await synth.run_method(
        list(rows),
        {
            "analyst_id": "region_composition",
            "target_id": "region_mena",
            "run_id": uuid4(),
        },
        _Deps(llm),
    )

    # Prompt: the WORLD-shaped composition prompt (multi-country), NOT the
    # single-country one, NOT the legacy global synth.
    assert llm.calls, "region run must call the LLM"
    assert llm.calls[-1]["system"] == synth._WORLD_COMPOSITION_SYSTEM
    assert llm.calls[-1]["system"] != synth._COMPOSITION_SYSTEM
    assert llm.calls[-1]["system"] != synth._SYSTEM_PROMPT

    # Lineage: derived_from is EXACTLY the fused country_composition head ids.
    assert set(result.derived_from) == {sa, ir}

    # Citations: only the two resolved ordinals — the out-of-range one dropped.
    cites = result.finding.data.get("citations")
    assert isinstance(cites, list)
    assert {c["ref_id"] for c in cites} == {str(sa), str(ir)}
    assert all(c["ref_kind"] == "finding" for c in cites)
    by_id = {str(sa): "country_composition", str(ir): "country_composition"}
    for c in cites:
        assert c["source"] == by_id[c["ref_id"]]

    # A region run is NOT the world contested path → no contested block.
    assert "contested" not in result.finding.data


@pytest.mark.asyncio
async def test_region_run_does_not_trim_below_the_member_roster():
    """A region read fuses one head PER member country, so it uses the WORLD cap
    (not the per-country 15) — a member country must never be silently dropped."""
    rows = [
        _country_read_row(uid=uuid4(), target_id=f"country_watch_{i}", title=f"c{i}")
        for i in range(20)
    ]
    body = "Region read [[ref:1]]."
    llm = _CannedLLM(
        {"title": "t", "body": body, "confidence": 0.4, "evidence": [], "tags": ["r"]}
    )
    result = await synth.run_method(
        list(rows),
        {
            "analyst_id": "region_composition",
            "target_id": "region_europe",
            "run_id": uuid4(),
        },
        _Deps(llm),
    )
    # All 20 member heads survive the cap (> the per-country MAX_INPUT_FINDINGS=15).
    assert len(result.derived_from) == 20


@pytest.mark.asyncio
async def test_region_honest_empty_is_a_named_gap_no_llm():
    """A region with no member country reads ⇒ empty slice ⇒ confidence 0.0
    honest-empty finding (the gap is NAMED — the actor stamps target_id=region_
    on the row), no LLM call, no fabricated citations."""
    llm = _NeverCalledLLM()
    result = await synth.run_method(
        [],
        {
            "analyst_id": "region_composition",
            "target_id": "region_mena",
            "run_id": uuid4(),
        },
        _Deps(llm),
    )
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert result.derived_from == []
    assert "citations" not in result.finding.data
