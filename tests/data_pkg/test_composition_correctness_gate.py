# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G2 — THE COMPOSITION CORRECTNESS GATE (`LEGBA_COMPOSITION_CORRECTNESS_GATE`).

The spine's rule made executable: `country_composition` composes only over desk
units whose latest `unit_correctness` row clears the operator's bars. Units
that do not are QUOTED (the periphery tier this tree already has) — never
dropped, never silently composed over.

Two halves, in this order because the first is the one that matters:

  1. **FLAG OFF IS BYTE-IDENTICAL.** Shipped default. `READ_SLICE` issues the
     same queries in the same order with the same params; no row is marked; no
     key is stamped; `_run` renders the same prompt bytes and the same
     `finding.data` keys as a tree without this module. Modelled on
     `test_composition_tiered_evidence.py`'s flag-off proofs, which drive the
     REAL entry points (`synth.READ_SLICE`, `synth._run`) rather than a mock of
     the kind module.

  2. **FLAG ON DOES WHAT IT SAYS.** The SQL runs against the real migrated
     substrate (migration 0196 tables, real rows); the three outcomes
     (`composed` / `quoted` / `no_number`) are recorded per unit with the
     numbers and the bars that produced them; and a run the gate empties says
     the GATE emptied it rather than claiming an absence of reads.

The thresholds are ENV, not descriptor options — the operator sets them at
deploy, same discipline as `LEGBA_GRADER_DAILY_CEILING_USD`. Every test that
reads one sets it explicitly; the autouse pin in `conftest.py`
(`_PROGRAM_FLAG_DEFAULTS`) keeps the operator's live `.env` out of the rest.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import composition_correctness_gate as gate
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.composition_window import CORRECTNESS_QUARANTINE_KEY
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore

RUBRIC_SHA = "0a011222" + "c" * 56


# ---------------------------------------------------------------------------
# Doubles — mirroring test_composition_tiered_evidence.py's conventions.
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Fake asyncpg.Connection recording every fetch()'s SQL + params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return [dict(r) for r in self._rows]


class _GateConn(_CapturingConn):
    """Serves the basis gather AND the gate's own number lookup.

    The gate's query is the only one in the composition path reading
    `unit_correctness`, so dispatching on that table name is exact rather than
    a guess about statement order.
    """

    def __init__(
        self,
        *,
        basis: list[dict[str, Any]],
        numbers: list[dict[str, Any]],
    ) -> None:
        super().__init__(rows=basis)
        self._numbers = numbers

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "FROM unit_correctness" in query:
            return [dict(r) for r in self._numbers]
        return [dict(r) for r in self._rows]


class _CannedLLM:
    subprovider = "gate_test_double"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self, messages: list[Any], *, max_tokens: int | None = None,
        temperature: float | None = None, system: str | None = None,
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
        raise AssertionError("no LLM call on the empty-basis path")


def _user_prompt_of(llm: _CannedLLM) -> str:
    assert len(llm.calls) == 1
    for m in llm.calls[0]["messages"]:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            content = (
                m.get("content") if isinstance(m, dict)
                else getattr(m, "content", None)
            )
            return str(content)
    raise AssertionError("no user message captured")


def _row(
    *,
    analyst_id: str,
    uid: UUID | None = None,
    title: str = "sub-claim title",
    body: str = "sub-claim body",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": 0.7,
        "effective_confidence": 0.7,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"tags": tags or [], "evidence": []},
        "evidence": [],
        "target_id": "country_g20_in",
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": "2026-09-16T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


def _number(
    *,
    analyst_id: str,
    n_contains: int = 10,
    n_contradicts: int = 1,
    n_silent: int = 31,
    n_split: int = 2,
    n_unparseable: int = 1,
    n_single_family: int = 0,
    single_family_block: bool = False,
) -> dict[str, Any]:
    """A `unit_correctness` row as the gate's SELECT projects it."""
    n_claims = n_contains + n_contradicts + n_silent + n_split + n_unparseable
    decided = n_contains + n_contradicts
    return {
        "analyst_id": analyst_id,
        "as_of": datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc),
        "grain": "desk",
        "n_claims": n_claims,
        "n_contains": n_contains,
        "n_contradicts": n_contradicts,
        "n_single_family": n_single_family,
        "correctness_share": (n_contains / decided) if decided else None,
        "coverage_share": (decided / n_claims) if n_claims else None,
        "families": {"single_family": single_family_block},
    }


def _descriptor(others: list[tuple[str, str]]) -> SimpleNamespace:
    entries = [
        SimpleNamespace(id=i, time_window=w, data_types=[]) for i, w in others
    ]
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=entries,
            targets=SimpleNamespace(predicate='has_tag("g20")'),
        )
    )


_UNITS = [("leadership_transition", "24h"), ("escalation", "24h")]


def _canned_payload(body: str) -> dict[str, Any]:
    return {
        "title": "Composed read",
        "body": body,
        "confidence": 0.6,
        "evidence": [],
        "tags": [],
    }


# ---------------------------------------------------------------------------
# 1. FLAG OFF — the shipped default is byte-identical.
# ---------------------------------------------------------------------------


def test_flag_and_thresholds_have_the_documented_defaults(monkeypatch):
    for env in (
        gate.GATE_ENV, gate.MIN_CORRECTNESS_ENV, gate.MIN_COVERAGE_ENV,
        gate.ALLOW_SINGLE_FAMILY_ENV,
    ):
        monkeypatch.delenv(env, raising=False)
    assert gate.gate_enabled() is False
    assert gate.allow_single_family() is False
    assert gate.min_correctness() == pytest.approx(0.8)
    assert gate.min_coverage() == pytest.approx(0.2)
    # The env names the operator sets, pinned so a rename is a visible diff.
    assert gate.GATE_ENV == "LEGBA_COMPOSITION_CORRECTNESS_GATE"
    assert gate.MIN_CORRECTNESS_ENV == "LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS"
    assert gate.MIN_COVERAGE_ENV == "LEGBA_COMPOSITION_GATE_MIN_COVERAGE"
    assert (
        gate.ALLOW_SINGLE_FAMILY_ENV
        == "LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY"
    )


@pytest.mark.asyncio
async def test_flag_off_apply_gate_reads_and_marks_nothing(monkeypatch):
    """Off, the gate is INERT: no query, no marker, no stamp, no ledger."""
    monkeypatch.delenv(gate.GATE_ENV, raising=False)
    conn = _CapturingConn(rows=[])
    rows = [_row(analyst_id="escalation")]
    before = json.dumps(rows, default=str)

    assert await gate.apply_gate(conn, rows, target_id="country_g20_in") is None

    assert conn.calls == [], "flag-off must issue no query at all"
    assert json.dumps(rows, default=str) == before
    assert gate.GATE_LEDGER_KEY not in rows[0]
    assert synth._EVIDENCE_TIER_KEY not in rows[0]


@pytest.mark.asyncio
async def test_flag_off_per_country_read_slice_is_byte_identical(monkeypatch):
    """`READ_SLICE` issues exactly the pre-gate queries, in order.

    The same assertion `test_composition_tiered_evidence.py` makes for C-TIER:
    ONE evidence gather, then the two memory-section gathers. A fourth call
    here would mean the gate queried while switched off.
    """
    monkeypatch.delenv(gate.GATE_ENV, raising=False)
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    conn = _CapturingConn(rows=[])

    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )

    assert len(conn.calls) == 3
    assert "FROM unit_correctness" not in " ".join(c[0] for c in conn.calls)
    assert "FROM situations" in conn.calls[1][0]
    assert "CASE f.severity" in conn.calls[-1][0]
    assert all(gate.GATE_LEDGER_KEY not in r for r in rows)


@pytest.mark.asyncio
async def test_flag_off_run_payload_and_prompt_are_byte_identical(monkeypatch):
    """`_run` over an UNGATED slice: same prompt bytes, no new payload key.

    The gate's `_run` half is data-driven off the ledger READ_SLICE stamps, so
    a slice without it — flag-off, and every direct/legacy caller — takes the
    identical path. Proved by rendering the same inputs twice, once with the
    flag env present-but-off, and comparing the prompt and the payload keys.
    """
    uid = uuid4()
    options = {"target_id": "country_g20_in", "analyst_id": "country_composition"}
    payload = _canned_payload("Leadership is contested [[ref:1]].")

    monkeypatch.delenv(gate.GATE_ENV, raising=False)
    llm_a = _CannedLLM(payload)
    res_a = await synth._run(
        [_row(analyst_id="leadership_transition", uid=uid)], options,
        llm=llm_a, max_tokens=512, temperature=0.2, system_prompt="unused",
    )
    monkeypatch.setenv(gate.GATE_ENV, "0")
    llm_b = _CannedLLM(payload)
    res_b = await synth._run(
        [_row(analyst_id="leadership_transition", uid=uid)], options,
        llm=llm_b, max_tokens=512, temperature=0.2, system_prompt="unused",
    )

    assert _user_prompt_of(llm_a) == _user_prompt_of(llm_b)
    assert "correctness_gate" not in res_a.finding.data
    assert "correctness_gate" not in res_b.finding.data
    assert sorted(res_a.finding.data) == sorted(res_b.finding.data)
    assert res_a.derived_from == res_b.derived_from == [uid]


# ---------------------------------------------------------------------------
# 2. THE VERDICT — pure, so the rule is readable without a database.
# ---------------------------------------------------------------------------


def _verdict(number: dict[str, Any] | None, **kw: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "min_correctness_share": 0.8,
        "min_coverage_share": 0.2,
        "single_family_allowed": True,
    }
    opts.update(kw)
    return gate.verdict(number, **opts)


def test_verdict_composes_a_unit_that_clears_both_bars():
    v = _verdict(_number(analyst_id="internal_stability"))
    assert v["gate"] == gate.GATE_COMPOSED
    assert v["correctness_share"] == pytest.approx(10 / 11)
    assert v["coverage_share"] == pytest.approx(11 / 45)
    assert v["n_decided"] == 11
    assert v["n_claims"] == 45


def test_verdict_quotes_a_unit_below_the_correctness_bar():
    v = _verdict(_number(analyst_id="x", n_contains=2, n_contradicts=1))
    assert v["gate"] == gate.GATE_QUOTED
    assert "below the operator's bar" in v["reason"]
    assert gate.MIN_CORRECTNESS_ENV in v["reason"]


def test_verdict_quotes_a_perfect_score_over_too_little():
    """100% over one decided claim of seven is the failure coverage exists for.

    `economic_coercion` measured exactly this on 2026-09-16: one contains, six
    silent. A correctness-only gate would rank it above a desk that was right
    about two of three.
    """
    v = _verdict(_number(
        analyst_id="economic_coercion", n_contains=1, n_contradicts=0,
        n_silent=6, n_split=0, n_unparseable=0,
    ))
    assert v["correctness_share"] == pytest.approx(1.0)
    assert v["gate"] == gate.GATE_QUOTED
    assert gate.MIN_COVERAGE_ENV in v["reason"]


def test_verdict_quotes_an_unmeasured_unit_rather_than_scoring_it_zero():
    """`military_posture` on 2026-09-16: 7 claims, none decided.

    NULL is not zero and not a pass. The reason must say the reference bore on
    nothing — not that the desk scored badly.
    """
    v = _verdict(_number(
        analyst_id="military_posture", n_contains=0, n_contradicts=0,
        n_silent=7, n_split=0, n_unparseable=0,
    ))
    assert v["correctness_share"] is None
    assert v["gate"] == gate.GATE_QUOTED
    assert "unmeasured" in v["reason"]


def test_verdict_names_a_missing_row_no_number():
    v = _verdict(None)
    assert v["gate"] == gate.GATE_NO_NUMBER
    assert v["correctness_share"] is None
    assert v["n_claims"] is None
    assert "no unit_correctness row" in v["reason"]


def test_verdict_single_family_is_quoted_unless_the_operator_allows_it():
    """At the shipped $0 grader ceiling every number is single-family."""
    n = _number(analyst_id="escalation", single_family_block=True)
    strict = _verdict(n, single_family_allowed=False)
    assert strict["gate"] == gate.GATE_QUOTED
    assert strict["single_family"] is True
    assert gate.ALLOW_SINGLE_FAMILY_ENV in strict["reason"]
    assert _verdict(n, single_family_allowed=True)["gate"] == gate.GATE_COMPOSED

    # The other route to single-family: every claim carried one label.
    every = _number(analyst_id="escalation", n_single_family=45)
    assert _verdict(every, single_family_allowed=False)["gate"] == gate.GATE_QUOTED


def test_thresholds_come_from_env_and_fail_toward_the_default(monkeypatch):
    monkeypatch.setenv(gate.MIN_CORRECTNESS_ENV, "0.95")
    monkeypatch.setenv(gate.MIN_COVERAGE_ENV, "0.5")
    assert gate.min_correctness() == pytest.approx(0.95)
    assert gate.min_coverage() == pytest.approx(0.5)

    # A typo must not remove the bar (the grader-ceiling discipline).
    monkeypatch.setenv(gate.MIN_CORRECTNESS_ENV, "eighty percent")
    assert gate.min_correctness() == pytest.approx(gate.DEFAULT_MIN_CORRECTNESS)
    # Out of range clamps into [0, 1] rather than disabling the gate.
    monkeypatch.setenv(gate.MIN_COVERAGE_ENV, "-3")
    assert gate.min_coverage() == pytest.approx(0.0)
    monkeypatch.setenv(gate.MIN_COVERAGE_ENV, "7")
    assert gate.min_coverage() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 3. FLAG ON — through READ_SLICE, the real entry point.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_on_quotes_the_failing_unit_and_composes_the_other(
    monkeypatch,
):
    monkeypatch.setenv(gate.GATE_ENV, "1")
    monkeypatch.setenv(gate.ALLOW_SINGLE_FAMILY_ENV, "1")
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)

    good = _row(analyst_id="leadership_transition")
    bad = _row(analyst_id="escalation")
    conn = _GateConn(
        basis=[good, bad],
        numbers=[
            _number(analyst_id="leadership_transition"),
            _number(analyst_id="escalation", n_contains=1, n_contradicts=4),
        ],
    )

    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )

    assert any("FROM unit_correctness" in c[0] for c in conn.calls)
    by_unit = {r["analyst_id"]: r for r in rows if r.get("analyst_id")}
    assert synth._EVIDENCE_TIER_KEY not in by_unit["leadership_transition"]
    assert by_unit["escalation"][synth._EVIDENCE_TIER_KEY] == synth.PERIPHERY_TIER
    # The correctness gate must NOT borrow C-TIER's floor key — a faithfulness
    # floor and a correctness bar are different measurements.
    assert synth._EVIDENCE_FLOOR_KEY not in by_unit["escalation"]

    ledger = gate.gate_ledger_of(rows)
    assert ledger is not None
    assert {e["unit"]: e["gate"] for e in ledger} == {
        "leadership_transition": gate.GATE_COMPOSED,
        "escalation": gate.GATE_QUOTED,
    }


@pytest.mark.asyncio
async def test_gate_on_with_no_numbers_quotes_everything_as_no_number(
    monkeypatch,
):
    monkeypatch.setenv(gate.GATE_ENV, "1")
    conn = _GateConn(basis=[_row(analyst_id="escalation")], numbers=[])

    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )

    ledger = gate.gate_ledger_of(rows)
    assert [e["gate"] for e in ledger] == [gate.GATE_NO_NUMBER]
    marked = [r for r in rows if r.get("analyst_id") == "escalation"]
    assert marked[0][synth._EVIDENCE_TIER_KEY] == synth.PERIPHERY_TIER


@pytest.mark.asyncio
async def test_gate_never_promotes_a_row_c_tier_already_quoted(monkeypatch):
    """A below-floor row stays quoted even with a passing correctness number.

    The faithfulness floor and the correctness gate are AND-ed, never OR-ed:
    a read the verify pass withheld must not walk back into the basis because
    an independent reference happened to agree with it.
    """
    monkeypatch.setenv(gate.GATE_ENV, "1")
    monkeypatch.setenv(gate.ALLOW_SINGLE_FAMILY_ENV, "1")
    row = _row(analyst_id="escalation")
    row[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    conn = _GateConn(basis=[], numbers=[_number(analyst_id="escalation")])

    ledger = await gate.apply_gate(conn, [row], target_id="country_g20_in")

    assert ledger == []
    assert row[synth._EVIDENCE_TIER_KEY] == synth.PERIPHERY_TIER
    # It was never even asked about — the gate only queries its candidates.
    assert conn.calls == []


# ---------------------------------------------------------------------------
# 4. FLAG ON — through `_run`, where the payload is written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_records_the_gate_per_unit_with_its_numbers(monkeypatch):
    monkeypatch.setenv(gate.GATE_ENV, "1")
    monkeypatch.setenv(gate.MIN_CORRECTNESS_ENV, "0.8")
    monkeypatch.setenv(gate.MIN_COVERAGE_ENV, "0.2")
    monkeypatch.setenv(gate.ALLOW_SINGLE_FAMILY_ENV, "1")

    good_uid, bad_uid = uuid4(), uuid4()
    good = _row(analyst_id="leadership_transition", uid=good_uid)
    bad = _row(
        analyst_id="escalation", uid=bad_uid, title="A weakly-supported read",
        tags=["severity:high"],
    )
    bad[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    ledger = [
        {
            "unit": "leadership_transition", "gate": gate.GATE_COMPOSED,
            "reason": "clears", "correctness_share": 0.909,
            "coverage_share": 0.244, "n_claims": 45, "n_decided": 11,
            "single_family": False, "as_of": "2026-09-16T19:30:00+00:00",
        },
        {
            "unit": "escalation", "gate": gate.GATE_QUOTED,
            "reason": "correctness 0.200 is below the operator's bar",
            "correctness_share": 0.2, "coverage_share": 0.5, "n_claims": 10,
            "n_decided": 5, "single_family": False,
            "as_of": "2026-09-16T19:30:00+00:00",
        },
    ]
    gate.stamp_gate_ledger([good, bad], ledger)

    llm = _CannedLLM(
        _canned_payload("Leadership is contested [[ref:1]].")
    )
    res = await synth._run(
        [good, bad],
        {"target_id": "country_g20_in", "analyst_id": "country_composition"},
        llm=llm, max_tokens=512, temperature=0.2, system_prompt="unused",
    )

    block = res.finding.data["correctness_gate"]
    assert block["composed"] == ["leadership_transition"]
    assert block["quoted"] == ["escalation"]
    assert block["no_number"] == []
    assert block["basis_count"] == 1
    # The bars in force travel WITH the verdicts.
    assert block["min_correctness"] == pytest.approx(0.8)
    assert block["min_coverage"] == pytest.approx(0.2)
    assert block["allow_single_family"] is True
    quoted = next(u for u in block["units"] if u["unit"] == "escalation")
    assert quoted["correctness_share"] == pytest.approx(0.2)
    assert quoted["n_decided"] == 5
    assert "below the operator's bar" in quoted["reason"]
    # The quoted desk is still VISIBLE — quoted, not disappeared.
    assert "A weakly-supported read" in _user_prompt_of(llm)


@pytest.mark.asyncio
async def test_run_says_the_gate_emptied_the_basis(monkeypatch):
    """Every desk quoted ⇒ the honest sentence names the GATE.

    "No desk read exists" over eight desks that all read fine and all missed a
    correctness bar is the BF-desk lie this tree already paid for once.
    """
    monkeypatch.setenv(gate.GATE_ENV, "1")
    row = _row(analyst_id="escalation", title="A read nobody graded")
    row[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    gate.stamp_gate_ledger([row], [
        {
            "unit": "escalation", "gate": gate.GATE_NO_NUMBER,
            "reason": "no unit_correctness row for this unit",
            "correctness_share": None, "coverage_share": None,
            "n_claims": None, "n_decided": None, "single_family": None,
            "as_of": None,
        },
    ])

    res = await synth._run(
        [row],
        {"target_id": "country_g20_in", "analyst_id": "country_composition"},
        llm=_NeverCalledLLM(), max_tokens=512, temperature=0.2,
        system_prompt="unused",
    )

    assert res.finding.title == "All reads withheld by the correctness gate"
    assert "CORRECTNESS GATE" in res.finding.body
    assert "not an absence of reads" in res.finding.body
    assert res.finding.data["correctness_gate"]["no_number"] == ["escalation"]
    assert res.finding.data["correctness_gate"]["basis_count"] == 0


def test_gate_empty_sentence_is_silent_when_something_composed():
    composed = [{"unit": "a", "gate": gate.GATE_COMPOSED}]
    assert gate.gate_empty_sentence(composed) is None
    assert gate.gate_empty_sentence([]) is None
    quoted = [{"unit": "a", "gate": gate.GATE_QUOTED}]
    assert gate.gate_empty_sentence(quoted) is not None


def test_gate_ledger_of_distinguishes_absent_from_empty():
    """`None` = ungated slice; `[]` = a gated run over zero units."""
    assert gate.gate_ledger_of([{"analyst_id": "x"}]) is None
    rows = [{"analyst_id": "x"}]
    gate.stamp_gate_ledger(rows, [])
    assert gate.gate_ledger_of(rows) == []


# ---------------------------------------------------------------------------
# 5. THE SQL, against the real migrated substrate (migration 0196).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_unit_numbers_reads_the_latest_row_per_unit(
    migrated_pg: PostgresConfig, monkeypatch,
):
    """The gate's SELECT, run for real: newest row per unit, scoped to target.

    Pinned against a live schema because the gate and the read surface
    (`unit_correctness_api`) MUST select the same row — a badge that explains
    a decision the gate did not make is worse than no badge.
    """
    monkeypatch.setenv(gate.GATE_ENV, "1")
    monkeypatch.setenv(gate.ALLOW_SINGLE_FAMILY_ENV, "1")
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    try:
        target_id = f"gate-{uuid4().hex[:10]}"
        other_id = f"gate-{uuid4().hex[:10]}"
        now = datetime.now(timezone.utc)
        ref_id = uuid4()
        async with pg_store.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO unit_references (
                    id, target_id, window_start, window_end, built_at,
                    builder, ref_json, thin_dimensions, sha256
                ) VALUES ($1,$2,$3,$4,$5,'test','{}'::jsonb,'{}',$6)
                """,
                ref_id, target_id, now - timedelta(days=14), now, now,
                uuid4().hex + uuid4().hex,
            )

            async def _put(
                analyst_id: str, tgt: str, as_of: datetime, contains: int
            ) -> None:
                await conn.execute(
                    """
                    INSERT INTO unit_correctness (
                        id, analyst_id, target_id, head_id, as_of,
                        reference_id, rubric_sha, grain, n_claims, n_contains,
                        n_contradicts, n_silent, n_split, n_unparseable,
                        n_single_family, correctness_share, coverage_share,
                        families, cost_usd
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,'desk',10,$8,$9,$10,0,0,0,
                        $11,$12,'{}'::jsonb,0
                    )
                    """,
                    uuid4(), analyst_id, tgt, uuid4(), as_of, ref_id,
                    RUBRIC_SHA, contains, 10 - contains, 0,
                    contains / 10.0, 1.0,
                )

            # Two gradings of the same desk: the older one was worse.
            await _put("escalation", target_id, now - timedelta(days=2), 3)
            await _put("escalation", target_id, now, 9)
            # A different target's number must never leak in.
            await _put("escalation", other_id, now, 1)
            await _put("leadership_transition", target_id, now, 5)

            numbers = await gate.read_unit_numbers(
                conn, target_id=target_id,
                analyst_ids=["escalation", "leadership_transition", "absent"],
            )

        assert set(numbers) == {"escalation", "leadership_transition"}
        assert numbers["escalation"]["correctness_share"] == pytest.approx(0.9)

        v_esc = gate.verdict(
            numbers["escalation"], min_correctness_share=0.8,
            min_coverage_share=0.2, single_family_allowed=True,
        )
        v_lead = gate.verdict(
            numbers.get("leadership_transition"), min_correctness_share=0.8,
            min_coverage_share=0.2, single_family_allowed=True,
        )
        v_absent = gate.verdict(
            numbers.get("absent"), min_correctness_share=0.8,
            min_coverage_share=0.2, single_family_allowed=True,
        )
        assert v_esc["gate"] == gate.GATE_COMPOSED
        assert v_lead["gate"] == gate.GATE_QUOTED
        assert v_absent["gate"] == gate.GATE_NO_NUMBER
    finally:
        await pg_store.close()


# ---------------------------------------------------------------------------
# 6. The PERIPHERY RENDER must not lie about WHY a gated row is quarantined.
# ---------------------------------------------------------------------------


def test_periphery_render_is_byte_identical_without_a_gated_row():
    """No gated row ⇒ the pre-G2 header and status lines, exactly."""
    row = _row(analyst_id="escalation", title="A weak read")
    row["effective_confidence"] = 0.31
    row["faithfulness_score"] = 0.31
    block = synth._render_periphery_block([row], start_ordinal=2, floor=0.5)
    assert "(below the verification floor 0.50) ===" in block
    assert "did NOT clear the verification floor" in block
    assert "status=below_floor effective_confidence=0.31" in block
    assert "CORRECTNESS GATE" not in block


def test_periphery_render_names_the_correctness_gate_for_a_gated_row():
    """A read that CLEARED the faithfulness floor and was withheld by the
    correctness gate must not be described as below that floor.

    The pre-G2 header says every quarantined item "did NOT clear the
    verification floor" and stamps `status=below_floor` on anything carrying a
    faithfulness score. For a correctness-gated row both are FALSE — it may
    have scored 0.90 — and a false sentence in the prompt is precisely the
    class of error this whole track exists to remove.
    """
    gated = _row(analyst_id="internal_stability", title="A read graded wrong")
    gated[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    gated[CORRECTNESS_QUARANTINE_KEY] = {
        "unit": "internal_stability", "gate": gate.GATE_QUOTED,
        "reason": "correctness 0.667 is below the operator's bar 0.800",
        "correctness_share": 0.667, "coverage_share": 0.5,
    }
    weak = _row(analyst_id="escalation", title="A weak read")
    weak["effective_confidence"] = 0.31
    weak["faithfulness_score"] = 0.31

    block = synth._render_periphery_block(
        [gated, weak], start_ordinal=2, floor=0.5
    )

    assert "CORRECTNESS GATE" in block
    assert "1 did not clear the verification floor 0.50" in block
    assert "1 DID clear it and were withheld" in block
    # The gated row reports the measurement that quarantined it, both shares.
    assert "status=quoted correctness=0.67 coverage=0.50" in block
    assert "status=below_floor effective_confidence=0.31" in block
    # And the old blanket sentence is gone — it would have been false here.
    assert "did NOT clear the verification floor" not in block


def test_periphery_render_says_ungraded_for_a_no_number_row():
    row = _row(analyst_id="proliferation_watch", title="A read nobody graded")
    row[CORRECTNESS_QUARANTINE_KEY] = {
        "unit": "proliferation_watch", "gate": gate.GATE_NO_NUMBER,
        "reason": "no unit_correctness row for this unit",
        "correctness_share": None, "coverage_share": None,
    }
    block = synth._render_periphery_block([row], start_ordinal=2, floor=0.5)
    assert "status=no_number correctness=ungraded" in block
    # Never a fabricated 0.00 for a unit that was simply never measured.
    assert "correctness=0.00" not in block
