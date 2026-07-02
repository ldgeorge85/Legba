# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-T2 — per-COUNTRY composition READ_SLICE: target-scope + verify-floor.

The glass-tower COMPOSITION leg runs the EXISTING meta_findings_synthesizer kind
target-scoped (a descriptor with a ``subscription.targets`` block → the runtime
fans it out one worker per G20 country with ``target_filter`` set). This asserts
the two P3-T2 code changes to ``READ_SLICE`` / ``read_other_analyst_findings``:

  * a TARGET-SCOPED run (``target_filter`` set) scopes the slice to that country
    (``f.target_id = $N``) AND applies the verify-floor gate (an INNER JOIN to the
    paired ``Faithfulness verify%`` critique + a ``LEAST(f.confidence,
    faithfulness_score) >= floor`` filter + a coerce-fallback tag exclusion);
  * a GLOBAL meta run (``target_filter=None``) reproduces the LEGACY query
    byte-for-byte — no target filter, no verify JOIN — so the existing global
    synthesizer is unaffected (backward compatibility);
  * the floor is OPS-tunable via ``LEGBA_COMPOSITION_VERIFY_FLOOR``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth


class _CapturingConn:
    """Fake asyncpg.Connection that records the last fetch() call's params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return list(self._rows)


def _descriptor(others: list[tuple[str, str]]) -> SimpleNamespace:
    entries = [SimpleNamespace(id=i, time_window=w, data_types=[]) for i, w in others]
    # A per-country composition descriptor DOES carry a subscription.targets
    # block; the stub only needs other_analysts for READ_SLICE id resolution.
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=entries,
            targets=SimpleNamespace(predicate='has_tag("g20")'),
        )
    )


_UNITS = [
    ("leadership_transition", "24h"),
    ("energy_security", "24h"),
    ("escalation", "24h"),
    ("narrative_coordination", "24h"),
]


# ---------------------------------------------------------------------------
# Global-meta (target_filter=None) — legacy query, backward compatible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_run_is_unfiltered_legacy_query():
    """target_filter=None → NO target scope, NO verify JOIN (the global meta
    the existing analyst_meta_synthesizer.yaml runs is untouched)."""
    desc = _descriptor([("country_assessor", "24h"), ("world_assessor", "24h")])
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    query, params = conn.calls[0]
    assert "target_id =" not in query
    assert "Faithfulness verify" not in query
    assert "JOIN LATERAL" not in query
    # Only the two legacy params: analyst id list + window hours.
    assert params == (["country_assessor", "world_assessor"], 24)


# ---------------------------------------------------------------------------
# Per-country (target_filter set) — scope + verify-floor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_scoped_run_adds_target_and_verify_floor():
    desc = _descriptor(_UNITS)
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter="country_g20_in")
    query, params = conn.calls[0]

    # (a) TARGET SCOPE — the running country only.
    assert "f.target_id = $3" in query
    assert params[2] == "country_g20_in"

    # (b) VERIFY-FLOOR gate — INNER JOIN to the faithfulness critique + the
    # effective_confidence floor + coerce-fallback tag exclusion.
    assert "JOIN LATERAL" in query
    assert "Faithfulness verify%" in query
    assert "LEAST(f.confidence, v.faithfulness_score) >= $4" in query
    assert "?| array['unstructured','coerce_failed']" in query
    assert params[3] == synth.DEFAULT_VERIFY_FLOOR  # 0.0 default

    # The source-analyst set is the four units.
    assert params[0] == [u for u, _ in _UNITS]


@pytest.mark.asyncio
async def test_verify_floor_is_env_tunable(monkeypatch):
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "0.6")
    desc = _descriptor(_UNITS)
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter="country_g20_br")
    _, params = conn.calls[0]
    assert params[3] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_empty_units_short_circuits_even_when_target_scoped():
    """No other_analysts → ids=[] → refuse the query (no full-table scan)
    regardless of target scoping. Honesty: the empty slice is surfaced, not
    papered over with a whole-substrate read."""
    desc = _descriptor([])
    conn = _CapturingConn(rows=[{"id": "should-not-appear"}])
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter="country_g20_us")
    assert rows == []
    assert conn.calls == []


# ---------------------------------------------------------------------------
# read_other_analyst_findings direct — the None/None legacy path is byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_reader_legacy_path_unchanged():
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["a", "b"], time_window_hours=48
    )
    query, params = conn.calls[0]
    assert "JOIN LATERAL" not in query
    assert "target_id =" not in query
    assert params == (["a", "b"], 48)


@pytest.mark.asyncio
async def test_direct_reader_target_only_no_verify_join():
    """target_id without a floor scopes the slice but does NOT force a verify
    JOIN (the two filters are independent knobs)."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["a"], target_id="country_g20_de"
    )
    query, params = conn.calls[0]
    assert "f.target_id = $3" in query
    assert "JOIN LATERAL" not in query
    assert params[2] == "country_g20_de"


# ---------------------------------------------------------------------------
# P4 content-audit fix (2026-07-01) — composition reads fold to ONE HEAD per
# (unit, country): superseded prior-cycle findings excluded + DISTINCT ON dedup,
# so ``derived_from`` can't double-count a single unit across stale dupes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composition_path_folds_to_one_head_per_unit():
    """A per-country composition read (target_id set) MUST exclude superseded
    prior-cycle findings AND ``DISTINCT ON (analyst_id, target_id)`` so the audit's
    "both leadership-transition units" double-count (1 fresh + N superseded) can't
    recur."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["escalation"], target_id="country_g20_us"
    )
    query, _ = conn.calls[0]
    assert "f.superseded_by IS NULL" in query
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in query


@pytest.mark.asyncio
async def test_world_path_folds_and_excludes_superseded():
    """The world composition (include_meta=True) reads country_composition findings
    across countries: one HEAD per (analyst_id, target_id) → one per country,
    superseded excluded, meta-exclusion clause dropped so meta rows admit."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["country_composition"], include_meta=True
    )
    query, _ = conn.calls[0]
    assert "f.superseded_by IS NULL" in query
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in query
    assert "->> 'meta'" not in query


@pytest.mark.asyncio
async def test_legacy_path_has_no_superseded_or_distinct():
    """The legacy global-meta path (both filters off) stays byte-for-byte: no
    superseded filter, no DISTINCT ON dedup, no target scope."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["a", "b"], time_window_hours=24
    )
    query, params = conn.calls[0]
    assert "superseded_by" not in query
    assert "DISTINCT ON" not in query
    assert "target_id =" not in query
    assert params == (["a", "b"], 24)


# ---------------------------------------------------------------------------
# _resolve_verify_floor — clamp + default + bad-env fallback
# ---------------------------------------------------------------------------


def test_resolve_verify_floor_default(monkeypatch):
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    assert synth._resolve_verify_floor(None) == synth.DEFAULT_VERIFY_FLOOR


def test_resolve_verify_floor_clamps(monkeypatch):
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "5.0")
    assert synth._resolve_verify_floor(None) == 1.0
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "-2")
    assert synth._resolve_verify_floor(None) == 0.0


def test_resolve_verify_floor_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "not-a-number")
    assert synth._resolve_verify_floor(None) == synth.DEFAULT_VERIFY_FLOOR


# ---------------------------------------------------------------------------
# P3 composition PROMPT slice — options["target_id"]-gated, in-kind (the global
# meta stays byte-for-byte). Covers: prompt selection, the [[ref:N]] ordinal
# handle + finding_id surfacing in the render, [[ref:N]] ordinal extraction +
# out-of-range rejection, the end-to-end citation stamp, and the honest-empty path.
# ---------------------------------------------------------------------------


class _CannedLLM:
    """LLM double that returns a caller-supplied payload and captures the
    system prompt / whether it was called at all."""

    subprovider = "composition_test_double"

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
        content = json.dumps(self._payload)

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        class _Response:
            pass

        resp = _Response()
        resp.content = content
        resp.usage = _Usage()
        return resp


class _NeverCalledLLM:
    """LLM double that FAILS if invoked — used to prove the empty-slice path
    short-circuits before any LLM call."""

    subprovider = "never_called"

    async def chat_complete(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("LLM must not be called on the empty-slice path")


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _subclaim_row(
    *,
    analyst_id: str,
    uid: UUID,
    title: str = "sub-claim title",
    body: str = "sub-claim body",
    confidence: float = 0.7,
    effective_confidence: float | None = None,
    target_id: str = "country_g20_in",
) -> dict[str, Any]:
    """A row shaped like a verify-floored ``read_other_analyst_findings`` result
    (carries ``effective_confidence`` + ``faithfulness_score``)."""
    return {
        "id": uid,
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": confidence,
        "effective_confidence": (
            effective_confidence if effective_confidence is not None else confidence
        ),
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"evidence": []},
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": "2026-06-30T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


# --- render: finding_id surfacing -----------------------------------------


def test_render_surfaces_finding_id_and_effective_confidence_when_composing():
    uid = uuid4()
    row = _subclaim_row(
        analyst_id="leadership_transition",
        uid=uid,
        effective_confidence=0.42,
    )
    rendered = synth._render_user_prompt(
        [row], ["leadership_transition"], include_source_ids=True
    )
    # The block leads with the copyable ordinal handle (the model copies THIS).
    assert "[[ref:1]]" in rendered
    # The finding_id is still surfaced for operator/debug provenance.
    assert f"finding_id={uid}" in rendered
    assert "effective_confidence=0.42" in rendered


def test_render_global_path_is_unchanged_no_finding_id():
    """The global meta render (include_source_ids default False) must NOT leak a
    finding_id, keeps the legacy ``confidence=`` label AND the unit-style ``[1]``
    block head (never the ``[[ref:1]]`` composition handle)."""
    uid = uuid4()
    row = _subclaim_row(analyst_id="country_assessor", uid=uid, confidence=0.7)
    rendered = synth._render_user_prompt([row], ["country_assessor"])
    assert "finding_id=" not in rendered
    assert "effective_confidence=" not in rendered
    assert "confidence=0.7" in rendered
    assert "[[ref:" not in rendered
    assert "[1] " in rendered


# --- _extract_ref_markers: keep in-range ORDINALS, DROP out-of-range ---------


def test_extract_ref_markers_keeps_in_range_drops_out_of_range():
    # 3 sub-claims rendered → ordinals 1..3 are valid; 9 is fabricated.
    body = (
        "Leadership is contested [[ref:1]]. Energy supply is tight "
        "[[ref:2]]. A hallucinated claim [[ref:9]]."
    )
    resolved, dropped = synth._extract_ref_markers(body, 3)
    assert resolved == [1, 2]   # first-appearance order, only in-range ordinals
    assert dropped == 1         # the out-of-range handle, counted not emitted


def test_extract_ref_markers_dedups_ordinals():
    # The same ordinal cited three times collapses to one; zero is out-of-range.
    body = "[[ref:1]] ... [[ref:1]] ... [[ref:0]]"
    resolved, dropped = synth._extract_ref_markers(body, 2)
    assert resolved == [1]
    assert dropped == 1  # [[ref:0]] — 0 is below the 1-based range


def test_extract_ref_markers_empty_body():
    assert synth._extract_ref_markers("", 3) == ([], 0)


# --- end-to-end _run: composition selects the prompt + stamps citations ----


@pytest.mark.asyncio
async def test_composition_run_selects_prompt_and_stamps_only_resolved_citations():
    u1 = uuid4()
    u2 = uuid4()
    rows = [
        _subclaim_row(analyst_id="leadership_transition", uid=u1, title="Leadership"),
        _subclaim_row(analyst_id="energy_security", uid=u2, title="Energy"),
    ]
    # Ordinal 1 → the first rendered sub-claim (u1), 2 → the second (u2). [[ref:9]]
    # is out of range (only 2 sub-claims) → a fabricated handle, must be dropped.
    body = (
        f"BLUF: the units point in different directions. Leadership looks stable "
        f"[[ref:1]], yet the energy picture is tightening [[ref:2]]. "
        f"An unsupported aside [[ref:9]] should never be cited."
    )
    llm = _CannedLLM(
        {
            "title": "India composition",
            "body": body,
            "confidence": 0.55,
            "evidence": ["units disagree on direction"],
            "tags": ["composition"],
        }
    )
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "country_composition", "target_id": "country_g20_in", "run_id": uuid4()},
        _Deps(llm),
    )

    # Prompt selection: the composition system prompt (not the global one) was used.
    assert llm.calls, "composition run must call the LLM"
    assert llm.calls[-1]["system"] == synth._COMPOSITION_SYSTEM
    assert llm.calls[-1]["system"] != synth._SYSTEM_PROMPT

    # Lineage: derived_from is exactly this country's contributing sub-claim ids.
    assert set(result.derived_from) == {u1, u2}

    # Citations: ONLY the two resolved ordinals — the out-of-range one is dropped.
    cites = result.finding.data.get("citations")
    assert isinstance(cites, list)
    cited_ids = {c["ref_id"] for c in cites}
    assert cited_ids == {str(u1), str(u2)}
    # Kind-aware entry: ref_id is the FINDING drill target, ref_kind='finding',
    # ordinal is the resolution key, marker is the ordinal form. NO signal_id.
    allowed = {str(u) for u in result.derived_from}
    ord_to_id = {1: str(u1), 2: str(u2)}
    for c in cites:
        assert c["ref_id"] in allowed
        assert c["ref_kind"] == "finding"
        assert c["marker"] == f"[[ref:{c['ordinal']}]]"
        assert ord_to_id[c["ordinal"]] == c["ref_id"]
        assert "signal_id" not in c
    # source == the contributing unit analyst_id.
    by_id = {str(u1): "leadership_transition", str(u2): "energy_security"}
    for c in cites:
        assert c["source"] == by_id[c["ref_id"]]


@pytest.mark.asyncio
async def test_composition_citations_carry_verify_evidence_for_the_verifier():
    """P3-T3/T7: each stamped citation carries the sub-claim's ``evidence_text``,
    ``effective_confidence`` and ``derived_from`` (captured point-in-time) so the
    downstream composition VERIFY runs DB-free (checks each composed clause
    against the exact evidence the model saw)."""
    u1, u2 = uuid4(), uuid4()
    rows = [
        {
            **_subclaim_row(analyst_id="leadership_transition", uid=u1,
                            body="Leadership contested body", effective_confidence=0.42),
            "derived_from": ["sig-a", "sig-b"],
        },
        {
            **_subclaim_row(analyst_id="energy_security", uid=u2,
                            body="Energy tight body", effective_confidence=0.6),
            "derived_from": ["sig-c"],
        },
    ]
    body = f"Leadership contested [[ref:1]]. Energy tight [[ref:2]]."
    llm = _CannedLLM(
        {"title": "t", "body": body, "confidence": 0.55, "evidence": [], "tags": ["composition"]}
    )
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "country_composition", "target_id": "country_g20_in", "run_id": uuid4()},
        _Deps(llm),
    )
    cites = {c["ref_id"]: c for c in result.finding.data["citations"]}
    assert set(cites) == {str(u1), str(u2)}
    assert cites[str(u1)]["evidence_text"] == "Leadership contested body"
    assert cites[str(u1)]["effective_confidence"] == pytest.approx(0.42)
    assert cites[str(u1)]["derived_from"] == ["sig-a", "sig-b"]
    assert cites[str(u2)]["effective_confidence"] == pytest.approx(0.6)
    assert cites[str(u2)]["derived_from"] == ["sig-c"]


@pytest.mark.asyncio
async def test_composition_citation_omits_eff_when_row_missing_it():
    """HONESTY: a sub-claim row with no ``effective_confidence`` → the citation
    omits the key (never fabricates a score → the verifier won't falsely cap)."""
    u1 = uuid4()
    row = _subclaim_row(analyst_id="escalation", uid=u1, confidence=0.7)
    row.pop("effective_confidence", None)
    body = "Escalation risk rising [[ref:1]]."
    llm = _CannedLLM(
        {"title": "t", "body": body, "confidence": 0.5, "evidence": [], "tags": ["c"]}
    )
    result = await synth.run_method(
        list([row]),
        {"analyst_id": "country_composition", "target_id": "country_g20_in", "run_id": uuid4()},
        _Deps(llm),
    )
    cite = result.finding.data["citations"][0]
    assert "effective_confidence" not in cite
    assert cite["evidence_text"]  # body still captured
    assert "derived_from" in cite


@pytest.mark.asyncio
async def test_global_meta_run_unchanged_uses_default_prompt_no_citations():
    """No ``target_id`` in options ⇒ the GLOBAL meta: the module default system
    prompt, cite-by-analyst_id, and NO ``[[ref]]`` citation stamp."""
    rows = [
        _subclaim_row(analyst_id="country_assessor", uid=uuid4()),
        _subclaim_row(analyst_id="world_assessor", uid=uuid4()),
    ]
    llm = _CannedLLM(
        {"title": "t", "body": "global synthesis body", "confidence": 0.6,
         "evidence": [], "tags": ["synth"]}
    )
    result = await synth.run_method(
        list(rows), {"analyst_id": "meta_synthesizer", "run_id": uuid4()}, _Deps(llm)
    )
    assert llm.calls[-1]["system"] == synth._SYSTEM_PROMPT
    assert llm.calls[-1]["system"] != synth._COMPOSITION_SYSTEM
    assert "citations" not in result.finding.data


@pytest.mark.asyncio
async def test_composition_honest_empty_no_llm_no_citations():
    """A country with no verified sub-claims ⇒ empty slice ⇒ confidence 0.0
    honest-empty finding, no LLM call, no fabricated citations."""
    llm = _NeverCalledLLM()
    result = await synth.run_method(
        [],
        {"analyst_id": "country_composition", "target_id": "country_g20_zz", "run_id": uuid4()},
        _Deps(llm),
    )
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert result.derived_from == []
    assert "citations" not in result.finding.data


# ---------------------------------------------------------------------------
# P3-T5 — the GLOBAL/world composition (target_filter=None BUT the descriptor
# DECLARES method.llm.verify): READ_SLICE takes the global-composition branch
# (include_meta=True + verify-floor), the kind selects the WORLD prompt off the
# `composition` option, cites the COUNTRY reads [[ref:uuid]], and (T4) surfaces
# contested groups [[contested:id]].
# ---------------------------------------------------------------------------


def _world_descriptor(*, declares_verify: bool = True) -> SimpleNamespace:
    """A world_assessor-shaped descriptor: NO targets block, other_analysts =
    [region_composition] (S2-T3 repointed the world read over the region floor),
    method.llm carrying (optionally) a verify block."""
    llm: dict[str, Any] = {"primary": {"raw": "llm.primary.openai_compat"}}
    if declares_verify:
        llm["verify"] = {"raw": "llm.verify.slm_8b"}
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(id="region_composition", time_window="24h", data_types=[])
            ],
            targets=None,
        ),
        method=SimpleNamespace(llm=llm),
    )


@pytest.mark.asyncio
async def test_declares_verify_helper():
    assert synth._declares_verify(_world_descriptor(declares_verify=True)) is True
    assert synth._declares_verify(_world_descriptor(declares_verify=False)) is False


@pytest.mark.asyncio
async def test_world_composition_read_slice_includes_meta_and_verify_floor():
    """target_filter=None + declares verify → the S2-T3 world branch. It first
    reads the region ROSTER (``target_descriptors``); with no roster present it
    falls back to a plain region-head read: NO target scope, INCLUDE meta (the
    exclusion clause is DROPPED — region_composition findings are meta=True; else
    the slice is silently zeroed), and the verify JOIN over ``region_composition``."""
    desc = _world_descriptor(declares_verify=True)
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    # The world branch reads the region-frame roster (empty here → plain read).
    assert any("target_descriptors" in q for q, _ in conn.calls)
    # The region-head slice read (last call) — no target scope, meta-inclusive,
    # verify-floored, over the region composition.
    query, params = conn.calls[-1]
    assert "target_descriptors" not in query
    assert "f.target_id =" not in query          # no target scope on a global run
    assert "'meta'" not in query                 # include_meta=True → exclusion dropped
    assert "JOIN LATERAL" in query               # verify-floor gate present
    assert "Faithfulness verify%" in query
    assert params[0] == ["region_composition"]   # the region layer, not country
    assert params[-1] == synth.DEFAULT_VERIFY_FLOOR  # floor (last positional)


@pytest.mark.asyncio
async def test_global_meta_without_verify_still_excludes_meta_and_no_join():
    """target_filter=None + NO verify block (the old analyst_meta_synthesizer) →
    legacy path, BYTE-FOR-BYTE: meta-exclusion clause PRESENT, no verify JOIN, no
    include_meta, and NO region roster read (the world branch is verify-gated)."""
    desc = _world_descriptor(declares_verify=False)
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    # The legacy path issues exactly ONE query — no region roster read.
    assert not any("target_descriptors" in q for q, _ in conn.calls)
    query, params = conn.calls[0]
    assert "'meta'" in query  # exclusion clause kept
    assert "JOIN LATERAL" not in query
    assert params == (["region_composition"], 24)


# --- read_open_contention + render -----------------------------------------


class _ContentionConn:
    """Fake conn routing the two contention SELECTs by SQL content."""

    def __init__(self, group_rows: list[dict[str, Any]], value_rows: list[dict[str, Any]]):
        self._group_rows = group_rows
        self._value_rows = value_rows
        self.calls: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if "fact_contention_values" in sql:
            return list(self._value_rows)
        return list(self._group_rows)


@pytest.mark.asyncio
async def test_read_open_contention_assembles_two_sided_groups_only():
    g1, g2 = uuid4(), uuid4()
    groups = [
        {"id": g1, "subject_key": "khamenei", "predicate_key": "status", "status": "surfaced"},
        {"id": g2, "subject_key": "lonely", "predicate_key": "x", "status": "contested"},
    ]
    values = [
        {"contention_id": g1, "value_key": "assassinated", "arbiter_score": 0.9,
         "surfaced_winner": True, "distinct_source_count": 4},
        {"contention_id": g1, "value_key": "alive", "arbiter_score": 0.3,
         "surfaced_winner": False, "distinct_source_count": 1},
        # g2 has only ONE non-junk cluster → not a two-sided dispute → dropped.
        {"contention_id": g2, "value_key": "solo", "arbiter_score": 0.5,
         "surfaced_winner": True, "distinct_source_count": 2},
    ]
    conn = _ContentionConn(groups, values)
    out = await synth.read_open_contention(conn)
    assert len(out) == 1
    grp = out[0]
    assert grp["contention_id"] == str(g1)
    assert grp["subject_key"] == "khamenei"
    assert {v["value_key"] for v in grp["values"]} == {"assassinated", "alive"}


@pytest.mark.asyncio
async def test_read_open_contention_empty_short_circuits():
    conn = _ContentionConn([], [])
    assert await synth.read_open_contention(conn) == []
    # Only the group query ran (no value query when there are no groups).
    assert len(conn.calls) == 1


def test_render_contested_block_names_both_sides_and_marker():
    groups = [{
        "contention_id": "11111111-1111-1111-1111-111111111111",
        "subject_key": "khamenei", "predicate_key": "status",
        "values": [
            {"value_key": "assassinated", "surfaced_winner": True, "arbiter_score": 0.9,
             "distinct_source_count": 4},
            {"value_key": "alive", "surfaced_winner": False, "arbiter_score": 0.3,
             "distinct_source_count": 1},
        ],
    }]
    block = synth._render_contested_block(groups)
    assert "[[contested:11111111-1111-1111-1111-111111111111]]" in block
    assert "assassinated" in block and "alive" in block
    assert "winner" in block  # the surfaced side is flagged


def test_render_contested_block_empty_is_blank():
    assert synth._render_contested_block([]) == ""


# --- _extract_contested_markers: keep resolved, DROP fabricated ------------


def test_extract_contested_markers_keeps_allowed_drops_fabricated():
    g1, fake = uuid4(), uuid4()
    allowed = {str(g1)}
    body = f"Khamenei's status is disputed [[contested:{g1}]]; also [[contested:{fake}]]."
    resolved, dropped = synth._extract_contested_markers(body, allowed)
    assert resolved == [str(g1)]
    assert dropped == 1


# --- end-to-end world composition _run -------------------------------------


def _country_read_row(*, uid: UUID, target_id: str, title: str) -> dict[str, Any]:
    return _subclaim_row(
        analyst_id="country_composition", uid=uid, title=title,
        body=f"{title} read body", target_id=target_id, effective_confidence=0.6,
    )


@pytest.mark.asyncio
async def test_world_composition_run_selects_world_prompt_cites_and_marks_contested():
    """options composition=True (no target_id) → the WORLD prompt, [[ref:uuid]]
    citations of the COUNTRY reads, and a resolved [[contested:id]] marker; a
    fabricated contested id is DROPPED."""
    br, inn = uuid4(), uuid4()
    gid = uuid4()
    fake_gid = uuid4()
    rows = [
        _country_read_row(uid=br, target_id="country_g20_br", title="Brazil"),
        _country_read_row(uid=inn, target_id="country_g20_in", title="India"),
    ]
    body = (
        f"BLUF: the country reads diverge. Brazil looks stable [[ref:1]], while "
        f"India is tightening [[ref:2]]. Khamenei's status is disputed "
        f"[[contested:{gid}]]. A made-up dispute [[contested:{fake_gid}]] must drop."
    )
    llm = _CannedLLM(
        {"title": "World read", "body": body, "confidence": 0.5,
         "evidence": ["countries diverge"], "tags": ["world"]}
    )
    contention_groups = [{
        "contention_id": str(gid), "subject_key": "khamenei",
        "predicate_key": "status",
        "values": [{"value_key": "assassinated", "surfaced_winner": True},
                   {"value_key": "alive", "surfaced_winner": False}],
    }]
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "world_assessor", "composition": True,
         "contention_groups": contention_groups, "run_id": uuid4()},
        _Deps(llm),
    )

    # Prompt: the WORLD-over-REGIONS composition prompt (S2-T3) — not the region/
    # per-country prompt, not the global legacy synth.
    assert llm.calls[-1]["system"] == synth._WORLD_OVER_REGIONS_SYSTEM
    assert llm.calls[-1]["system"] != synth._COMPOSITION_SYSTEM
    assert llm.calls[-1]["system"] != synth._SYSTEM_PROMPT
    # The CONTESTED FACTS block was fed into the user prompt.
    assert "CONTESTED FACTS" in llm.calls[-1]["messages"][0]["content"]

    # Citations: both country reads, cross-country divergence surface.
    cited = {c["ref_id"] for c in result.finding.data["citations"]}
    assert cited == {str(br), str(inn)}
    assert all(c["ref_kind"] == "finding" for c in result.finding.data["citations"])

    # Contested: the real group resolved, the fabricated one dropped.
    contested = result.finding.data["contested"]
    assert [c["contention_id"] for c in contested] == [str(gid)]
    assert contested[0]["subject_key"] == "khamenei"
    assert str(fake_gid) not in {c["contention_id"] for c in contested}


@pytest.mark.asyncio
async def test_world_composition_honest_empty_no_llm_no_citations_no_contested():
    """No country reads ⇒ empty slice ⇒ confidence 0.0 honest-empty, no LLM,
    no citations key (so the verify pass no-ops), no contested."""
    llm = _NeverCalledLLM()
    result = await synth.run_method(
        [],
        {"analyst_id": "world_assessor", "composition": True,
         "contention_groups": [], "run_id": uuid4()},
        _Deps(llm),
    )
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert "citations" not in result.finding.data
    assert "contested" not in result.finding.data
