# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-1 (MASTER_PLAN 2026-07-13) — compose-time head re-resolution (freshness).

A composition head freezes its lower-tier CITATIONS at its own tick. If a
sub-finding it cited later REVERSES (is superseded by a materially different
current head) AFTER that head composed, the reversal does not propagate up until
every intervening tier re-composes — the live Italy staleness race (escalation
``ed158597`` conf 0.90 "expulsions drive escalation risk" reversed to
``f0cd1c87`` conf 0.30 "no signs of near-term escalation" at 00:44, but the
country→region→world heads had already composed, so the world assessment cited
the superseded high-escalation reading).

The fix walks each input head's ``derived_from`` lineage at compose time and
flags any sub-finding superseded by a materially-different current head AFTER the
tier that cited it composed (a genuine post-hoc reversal, not routine re-run
churn the read gate already resolved). These tests lock the gate semantics:

  * the Italy mirror (post-hoc reversal, both risk directions) IS flagged;
  * a supersession that PRE-DATES the citing head is NOT flagged (the citer would
    have read the successor already — a different, resolved case);
  * an immaterial confidence delta is NOT flagged (routine churn);
  * multi-hop lineage (world → region → country → unit) reaches the unit reversal;
  * the prompt advisory dedupes per-target + caps; the trace ledger is full;
  * ``_attach_freshness`` is FAIL-SAFE (a conn error returns the slice unchanged);
  * ``_run`` PREPENDS the advisory to the user turn + stamps ``data.freshness``.

The BFS is exercised through a fake conn keyed by finding id (it only issues the
one ``_FRESHNESS_FETCH_SQL`` query), so the logic is tested without a live DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

import json

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.meta_findings_synthesizer import (
    DEFAULT_MAX_TOKENS,
    _attach_freshness,
    _detect_stale_inputs,
    _render_freshness_advisory_block,
    run_method,
)

_T0 = datetime(2026, 7, 12, 7, 0, tzinfo=timezone.utc)


def _at(hours: float) -> datetime:
    return _T0 + timedelta(hours=hours)


class _FreshnessConn:
    """Fake asyncpg conn for the freshness BFS.

    Holds a finding graph keyed by id; answers the single
    ``_FRESHNESS_FETCH_SQL`` query (``WHERE id = ANY($1::uuid[]) AND kind =
    'finding'``) by returning the rows whose id is in the requested list.
    """

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._by_id: dict[str, dict[str, Any]] = {str(f["id"]): f for f in findings}
        self.fetch_calls = 0

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        ids = params[0] if params else []
        wanted = {str(_coerce(i)) for i in ids}
        return [self._by_id[k] for k in self._by_id if k in wanted]


class _ExplodingConn:
    async def fetch(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("boom — freshness must swallow this")


def _coerce(x: Any) -> UUID | None:
    if isinstance(x, UUID):
        return x
    try:
        return UUID(str(x))
    except Exception:
        return None


def _finding(
    *,
    fid: UUID,
    analyst_id: str,
    target_id: str,
    confidence: float,
    produced_at: datetime,
    title: str,
    superseded_by: UUID | None = None,
    derived_from: list[UUID] | None = None,
) -> dict[str, Any]:
    return {
        "id": fid,
        "analyst_id": analyst_id,
        "target_id": target_id,
        "confidence": confidence,
        "produced_at": produced_at,
        "superseded_by": superseded_by,
        "derived_from": derived_from or [],
        "title": title,
    }


def _input_row(fid: UUID, produced_at: datetime, derived_from: list[UUID]) -> dict[str, Any]:
    """A composition's input head (what _run/_detect see as a top-level row)."""
    return {"id": fid, "produced_at": produced_at, "derived_from": list(derived_from)}


# ---------------------------------------------------------------------------
# core gate semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_italy_mirror_post_hoc_reversal_is_flagged():
    """Country head C (composed t=16.5h) cites unit A (t=0, conf 0.90); A is then
    superseded by A' (t=17.7h, conf 0.30) — AFTER C composed. Flagged (Δ0.60)."""
    a = uuid4()
    a2 = uuid4()
    c = uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), superseded_by=a2,
                 title="Italy – Diplomatic expulsions drive escalation risk"),
        _finding(fid=a2, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.30, produced_at=_at(17.7),
                 title="Italy shows no signs of near-term escalation"),
    ]
    conn = _FreshnessConn(findings)
    c_row = _input_row(c, _at(16.5), [a])  # country head cites A

    out = await _detect_stale_inputs(conn, [c_row])
    assert out is not None
    roots = out["stale_roots"]
    assert len(roots) == 1
    r = roots[0]
    assert r["unit"] == "escalation"
    assert r["target"] == "country_g20_it"
    assert r["old_confidence"] == 0.9
    assert r["new_confidence"] == 0.3
    assert r["delta_confidence"] == 0.6
    assert "no signs" in r["new_title"]
    assert out["advisory"] and out["advisory"][0]["target"] == "country_g20_it"


@pytest.mark.asyncio
async def test_risk_up_reversal_is_flagged():
    """The gate is direction-agnostic: a LOW→HIGH escalation reversal is material
    too (an under-weighted risk is as wrong as an over-weighted one)."""
    a, a2, c = uuid4(), uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_ru",
                 confidence=0.30, produced_at=_at(0.0), superseded_by=a2,
                 title="Russia – proxy activity, low escalation"),
        _finding(fid=a2, analyst_id="escalation", target_id="country_g20_ru",
                 confidence=0.90, produced_at=_at(18.0),
                 title="Russia – drone attacks & naval drills drive escalation"),
    ]
    conn = _FreshnessConn(findings)
    out = await _detect_stale_inputs(conn, [_input_row(c, _at(16.0), [a])])
    assert out is not None
    assert out["stale_roots"][0]["delta_confidence"] == 0.6


@pytest.mark.asyncio
async def test_pre_compose_supersession_not_flagged():
    """A' post-dates A but PRE-dates the citing head C — C should already have
    read A' (the read gate returns current heads). Not a post-hoc reversal."""
    a, a2, c = uuid4(), uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), superseded_by=a2,
                 title="high"),
        _finding(fid=a2, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.30, produced_at=_at(5.0), title="low"),
    ]
    conn = _FreshnessConn(findings)
    # C composed at t=10h, AFTER the reversal at t=5h.
    out = await _detect_stale_inputs(conn, [_input_row(c, _at(10.0), [a])])
    assert out is None


@pytest.mark.asyncio
async def test_immaterial_delta_not_flagged():
    """|Δconf| below the materiality floor (0.25) is routine churn, not a
    reversal — a unit re-running with near-identical confidence."""
    a, a2, c = uuid4(), uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="energy_security", target_id="country_g20_in",
                 confidence=0.60, produced_at=_at(0.0), superseded_by=a2, title="moderate"),
        _finding(fid=a2, analyst_id="energy_security", target_id="country_g20_in",
                 confidence=0.70, produced_at=_at(18.0), title="moderate (updated)"),
    ]
    conn = _FreshnessConn(findings)
    out = await _detect_stale_inputs(conn, [_input_row(c, _at(16.0), [a])])
    assert out is None


@pytest.mark.asyncio
async def test_no_supersession_returns_none():
    a, c = uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), title="high (still current)"),
    ]
    conn = _FreshnessConn(findings)
    out = await _detect_stale_inputs(conn, [_input_row(c, _at(16.0), [a])])
    assert out is None


@pytest.mark.asyncio
async def test_multi_hop_world_region_country_unit():
    """The world tier's input is a REGION head; the reversal is at the UNIT
    finding two hops below (region → country → unit). The bounded walk reaches
    it and flags it against the COUNTRY head's compose time (its direct citer)."""
    unit, unit2 = uuid4(), uuid4()
    country = uuid4()
    region = uuid4()
    world_input = region
    findings = [
        # unit finding (escalation) reverses AFTER the country head composed
        _finding(fid=unit, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), superseded_by=unit2,
                 title="Italy – expulsions drive escalation risk"),
        _finding(fid=unit2, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.30, produced_at=_at(17.7), title="Italy – no near-term escalation"),
        # country head cites the unit; composed at 16.5h (before the reversal)
        _finding(fid=country, analyst_id="country_composition", target_id="country_g20_it",
                 confidence=0.85, produced_at=_at(16.5), derived_from=[unit],
                 title="Italy – composite"),
        # region head cites the country head; composed at 19.25h
        _finding(fid=region, analyst_id="region_composition", target_id="region_europe",
                 confidence=0.68, produced_at=_at(19.25), derived_from=[country],
                 title="Europe – regional composition"),
    ]
    conn = _FreshnessConn(findings)
    out = await _detect_stale_inputs(conn, [_input_row(world_input, _at(29.0), [region])])
    assert out is not None
    units = {r["unit"] for r in out["stale_roots"]}
    # The escalation unit reversal (deep in the lineage) is surfaced.
    assert "escalation" in units
    esc = next(r for r in out["stale_roots"] if r["unit"] == "escalation")
    assert esc["delta_confidence"] == 0.6


@pytest.mark.asyncio
async def test_chained_reversal_resolves_to_terminal_head():
    """A re-reversal within the window (A → A' → A'') must be judged against A''
    (the CURRENT reading), not the intermediate A' — so the Δconf + reversal title
    reflect where the claim actually landed (review MED finding)."""
    a, a2, a3, c = uuid4(), uuid4(), uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), superseded_by=a2, title="high"),
        _finding(fid=a2, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.55, produced_at=_at(17.0), superseded_by=a3, title="mid"),
        _finding(fid=a3, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.20, produced_at=_at(19.0), title="low (current)"),
    ]
    conn = _FreshnessConn(findings)
    out = await _detect_stale_inputs(conn, [_input_row(c, _at(16.0), [a])])
    assert out is not None
    r = out["stale_roots"][0]
    assert r["new_confidence"] == 0.2  # terminal A'', not the 0.55 intermediate
    assert r["delta_confidence"] == 0.7  # |0.90 - 0.20|
    assert "current" in r["new_title"]


@pytest.mark.asyncio
async def test_reverting_chain_not_flagged():
    """A → A' (big swing) → A'' back near A: the CURRENT state matches what the
    citer relied on, so there is no material staleness (terminal-resolution makes
    this correct where immediate-successor would have false-flagged A → A')."""
    a, a2, a3, c = uuid4(), uuid4(), uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), superseded_by=a2, title="high"),
        _finding(fid=a2, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.30, produced_at=_at(17.0), superseded_by=a3, title="low"),
        _finding(fid=a3, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.88, produced_at=_at(19.0), title="high again (current)"),
    ]
    conn = _FreshnessConn(findings)
    out = await _detect_stale_inputs(conn, [_input_row(c, _at(16.0), [a])])
    assert out is None  # |0.90 - 0.88| = 0.02 < 0.25


@pytest.mark.asyncio
async def test_advisory_dedupes_per_target_and_caps():
    """The trace ledger keeps one row per (unit,target); the prompt advisory
    keeps one per TARGET (sharpest) and is capped, ranked by Δconf desc."""
    rows_findings: list[dict[str, Any]] = []
    input_children: list[UUID] = []
    # 8 distinct targets each with a material reversal of increasing delta.
    for i in range(8):
        a, a2 = uuid4(), uuid4()
        delta_conf = 0.30 + 0.05 * i
        rows_findings.append(
            _finding(fid=a, analyst_id="escalation", target_id=f"country_{i}",
                     confidence=0.90, produced_at=_at(0.0), superseded_by=a2, title=f"old {i}")
        )
        rows_findings.append(
            _finding(fid=a2, analyst_id="escalation", target_id=f"country_{i}",
                     confidence=round(0.90 - delta_conf, 3), produced_at=_at(18.0), title=f"new {i}")
        )
        input_children.append(a)
    conn = _FreshnessConn(rows_findings)
    out = await _detect_stale_inputs(conn, [_input_row(uuid4(), _at(16.0), input_children)])
    assert out is not None
    assert len(out["stale_roots"]) == 8  # full ledger keeps all distinct targets
    assert len(out["advisory"]) == synth.FRESHNESS_MAX_ADVISORY  # capped
    # Ranked sharpest-first.
    deltas = [a["delta_confidence"] for a in out["advisory"]]
    assert deltas == sorted(deltas, reverse=True)
    assert deltas[0] == out["stale_roots"][0]["delta_confidence"]


# ---------------------------------------------------------------------------
# render + attach + fail-safe
# ---------------------------------------------------------------------------


def test_render_block_empty_and_nonempty():
    assert _render_freshness_advisory_block([]) == ""
    block = _render_freshness_advisory_block(
        [
            {
                "unit": "escalation",
                "target": "country_g20_it",
                "old_title": "expulsions drive escalation",
                "old_confidence": 0.9,
                "new_title": "no near-term escalation",
                "new_confidence": 0.3,
                "superseded_at": "2026-07-13T00:44:39+00:00",
            }
        ]
    )
    assert "FRESHNESS ADVISORY" in block
    assert "SUPERSEDED" in block
    assert "country_g20_it" in block
    assert "escalation" in block


@pytest.mark.asyncio
async def test_attach_freshness_failsafe_on_conn_error():
    """A freshness-pass error must NEVER break a compose — the slice is returned
    unchanged and no ``_freshness`` key is attached."""
    rows = [_input_row(uuid4(), _at(16.0), [uuid4()])]
    out = await _attach_freshness(_ExplodingConn(), rows)
    assert out is rows
    assert "_freshness" not in out[0]


@pytest.mark.asyncio
async def test_attach_freshness_attaches_same_dict_to_all_rows():
    a, a2 = uuid4(), uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), superseded_by=a2, title="high"),
        _finding(fid=a2, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.30, produced_at=_at(18.0), title="low"),
    ]
    conn = _FreshnessConn(findings)
    rows = [
        _input_row(uuid4(), _at(16.0), [a]),
        _input_row(uuid4(), _at(16.0), [a]),
    ]
    out = await _attach_freshness(conn, rows)
    assert all("_freshness" in r for r in out)
    assert out[0]["_freshness"] is out[1]["_freshness"]  # one shared dict
    assert out[0]["_freshness"]["stale_roots"]


@pytest.mark.asyncio
async def test_attach_freshness_noop_when_clean():
    """No material reversal → no ``_freshness`` key (fresh compose stays clean)."""
    a = uuid4()
    findings = [
        _finding(fid=a, analyst_id="escalation", target_id="country_g20_it",
                 confidence=0.90, produced_at=_at(0.0), title="high (current)"),
    ]
    conn = _FreshnessConn(findings)
    rows = [_input_row(uuid4(), _at(16.0), [a])]
    out = await _attach_freshness(conn, rows)
    assert "_freshness" not in out[0]


# ---------------------------------------------------------------------------
# _run integration — advisory prepend + trace stamp
# ---------------------------------------------------------------------------


class _CaptureLLM:
    subprovider = "test_double"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        self.calls.append({"messages": list(messages), "system": system})

        class _U:
            prompt_tokens = 10
            completion_tokens = 10
            reasoning_tokens = 0

        class _R:
            content = json.dumps(
                {"title": "world synth", "body": "b", "confidence": 0.6,
                 "evidence": [], "tags": ["synth"]}
            )
            usage = _U()

        return _R()


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _prompt_text(call: Mapping[str, Any]) -> str:
    parts = [call.get("system") or ""]
    for m in call["messages"]:
        c = m.get("content") if isinstance(m, Mapping) else None
        if isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_run_prepends_advisory_and_stamps_trace():
    """An input slice carrying a freshness advisory → ``_run`` PREPENDS the
    directive block to the user turn and stamps ``data.freshness`` in the trace."""
    fresh = {
        "inputs_as_of": [{"id": "x", "as_of": "2026-07-13T02:15:00+00:00"}],
        "stale_roots": [
            {
                "unit": "escalation",
                "target": "country_g20_it",
                "old_id": "old",
                "old_title": "Italy – expulsions drive escalation risk",
                "old_confidence": 0.9,
                "new_id": "new",
                "new_title": "Italy – no near-term escalation",
                "new_confidence": 0.3,
                "delta_confidence": 0.6,
                "superseded_at": "2026-07-13T00:44:39+00:00",
            }
        ],
        "advisory": [
            {
                "unit": "escalation",
                "target": "country_g20_it",
                "old_title": "Italy – expulsions drive escalation risk",
                "old_confidence": 0.9,
                "new_title": "Italy – no near-term escalation",
                "new_confidence": 0.3,
                "superseded_at": "2026-07-13T00:44:39+00:00",
            }
        ],
    }
    rows = [
        {
            "id": uuid4(),
            "analyst_id": "region_composition",
            "title": "Europe – regional composition",
            "body": "europe body",
            "confidence": 0.68,
            "produced_at": _at(19.25),
            "_freshness": fresh,
        }
    ]
    llm = _CaptureLLM()
    result = await run_method(list(rows), {"analyst_id": "world_assessor"}, _Deps(llm))

    assert len(llm.calls) == 1
    text = _prompt_text(llm.calls[0])
    assert "FRESHNESS ADVISORY" in text
    assert "country_g20_it" in text
    assert "no near-term escalation" in text
    # The advisory leads the user turn (before the findings render).
    assert text.index("FRESHNESS ADVISORY") < text.index("Europe – regional composition")

    # Trace ledger stamped.
    stamped = result.finding.data.get("freshness")
    assert isinstance(stamped, dict)
    assert stamped["stale_roots"][0]["target"] == "country_g20_it"
    assert stamped["advised"] == 1


@pytest.mark.asyncio
async def test_run_no_advisory_is_byte_clean():
    """A slice with no ``_freshness`` → no advisory, no ``data.freshness`` key."""
    rows = [
        {
            "id": uuid4(),
            "analyst_id": "region_composition",
            "title": "Europe – regional composition",
            "body": "b",
            "confidence": 0.68,
            "produced_at": _at(19.25),
        }
    ]
    llm = _CaptureLLM()
    result = await run_method(list(rows), {"analyst_id": "world_assessor"}, _Deps(llm))
    text = _prompt_text(llm.calls[0])
    assert "FRESHNESS ADVISORY" not in text
    assert "freshness" not in result.finding.data
