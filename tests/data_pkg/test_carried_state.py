# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CARRIED STATE EXPIRES (``legba.data.carried_state``).

flow_semiconductors read "degraded, held by border seizures" for 45 days off
one seizure: every read since had all its indicators ``not_observed`` and
re-stated the status from its own prior read, and the composition's floor
fallback would promote a superseded read however old. The done line, as tests:

  * a fixture unit with an unevidenced status flips to ``carried_unsupported``
    once its last evidencing read is older than N windows (and not before);
  * the floor fallback refuses a read older than N windows, and stamps a
    promoted one ``fallback: true`` with its age;
  * the synthesizer (``run_method`` → ``_run``) on a fixture keeps a carried
    head out of the basis and names it in the coverage ledger, never banded;
  * the last-evidence and unit-heads reads, on the REAL driver.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data import carried_state as cs
from legba.data.analysts import composition_window as cw
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.composition_stale_reads import UNIT_CADENCE_ROW_KEY

NOW = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
CADENCE_H = 11.0  # disruption_status' cooldown_seconds 39600


def _indicators(*statuses: str) -> list[dict[str, Any]]:
    return [
        {"id": f"ind-{i}", "status": s, "citations": []}
        for i, s in enumerate(statuses)
    ]


def _unit_row(
    *,
    age_h: float = 1.0,
    severity: str | None = "moderate",
    indicators: list[dict[str, Any]] | None = None,
    analyst_id: str = "disruption_status",
    target_id: str = "flow_semiconductors",
    cadence_h: float | None = CADENCE_H,
) -> dict[str, Any]:
    data: dict[str, Any] = {"tags": [f"severity:{severity}"] if severity else []}
    if indicators is not None:
        data["data"] = {"indicators": indicators}
    row: dict[str, Any] = {
        "id": uuid4(),
        "kind": "finding",
        "title": "Taiwan semiconductor export flow remains degraded",
        "body": "degraded, held by border seizures",
        "confidence": 0.65,
        "effective_confidence": 0.65,
        "faithfulness_score": 0.9,
        "severity": severity,
        "data": data,
        "target_id": target_id,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": (NOW - timedelta(hours=age_h)).isoformat(),
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }
    if cadence_h is not None:
        row[UNIT_CADENCE_ROW_KEY] = cadence_h
    return row


def _evidence(age_h: float) -> dict[str, Any]:
    return {"id": "3c38f383-164f-42e1-afde-9f9d86993e3d", "produced_at": NOW - timedelta(hours=age_h)}


# ---------------------------------------------------------------------------
# 1. THE RULE — pure
# ---------------------------------------------------------------------------


def test_the_row_key_matches_the_stale_read_cadence_key() -> None:
    """The leaf may not import ``legba.data.analysts`` (slim image), so it spells
    the cadence key; this pins the two spellings together."""
    assert cs.UNIT_CADENCE_ROW_KEY == UNIT_CADENCE_ROW_KEY


def test_an_unevidenced_status_flips_to_carried_unsupported_after_n_windows() -> None:
    head = _unit_row(indicators=_indicators("not_observed", "not_observed"))
    # Inside N windows (3 × 11 h = 33 h): still a live status.
    assert cs.carry_verdict(head, last_evidence=_evidence(30.0), cadence_h=CADENCE_H, now=NOW, windows=3) is None
    # One window past: carried, unsupported — with the proof.
    v = cs.carry_verdict(head, last_evidence=_evidence(34.0), cadence_h=CADENCE_H, now=NOW, windows=3)
    assert v is not None
    assert v["kind"] == "carried_unsupported" == cs.CARRIED_UNSUPPORTED
    assert v["cause"] == cs.CAUSE_NOT_RE_EVIDENCED
    assert v["status"] == "moderate"
    assert v["last_evidence_id"] == "3c38f383-164f-42e1-afde-9f9d86993e3d"
    assert v["last_evidence_age_h"] == pytest.approx(34.0)
    assert v["limit_h"] == pytest.approx(33.0)
    assert "34 h old" in cs.verdict_sentence(v)


def test_the_live_flow_semiconductors_shape_is_convicted() -> None:
    """The 10-08 head: six indicators all not_observed, 'moderate', last
    triggered read 336 h old."""
    head = _unit_row(indicators=_indicators(*["not_observed"] * 6))
    v = cs.carry_verdict(head, last_evidence=_evidence(336.0), cadence_h=CADENCE_H, now=NOW)
    assert v is not None and v["windows"] == cs.CARRY_EXPIRY_WINDOWS_DEFAULT


def test_no_evidencing_read_in_the_lookback_is_said_in_words() -> None:
    head = _unit_row(indicators=_indicators("not_observed"))
    v = cs.carry_verdict(head, last_evidence=None, cadence_h=CADENCE_H, now=NOW)
    assert v is not None
    assert v["last_evidence_id"] is None and v["last_evidence_age_h"] is None
    assert "no read in the last 90 days" in cs.verdict_sentence(v)


@pytest.mark.parametrize(
    "row",
    [
        # The read evidences its own status: an indicator triggered.
        _unit_row(indicators=_indicators("not_observed", "triggered")),
        # The quiet level is not a carried claim.
        _unit_row(severity="low", indicators=_indicators("not_observed")),
        # No status at all.
        _unit_row(severity=None, indicators=_indicators("not_observed")),
        # No indicators declared: NOT MEASURED (never passed, never failed).
        _unit_row(indicators=None),
    ],
)
def test_what_the_rule_does_not_convict(row: dict[str, Any]) -> None:
    assert cs.carry_verdict(row, last_evidence=None, cadence_h=CADENCE_H, now=NOW) is None


def test_a_unit_with_no_cadence_is_not_measured() -> None:
    head = _unit_row(indicators=_indicators("not_observed"))
    assert cs.carry_verdict(head, last_evidence=None, cadence_h=None, now=NOW) is None


def test_the_window_count_is_a_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    head = _unit_row(indicators=_indicators("not_observed"))
    monkeypatch.setenv(cs.CARRY_EXPIRY_WINDOWS_ENV, "5")
    assert cs.carry_expiry_windows() == 5.0
    assert cs.carry_verdict(head, last_evidence=_evidence(50.0), cadence_h=CADENCE_H, now=NOW) is None
    assert cs.carry_verdict(head, last_evidence=_evidence(56.0), cadence_h=CADENCE_H, now=NOW) is not None
    for bad in ("", "abc", "0", "-2"):
        monkeypatch.setenv(cs.CARRY_EXPIRY_WINDOWS_ENV, bad)
        assert cs.carry_expiry_windows() == cs.CARRY_EXPIRY_WINDOWS_DEFAULT


def test_partition_lifts_only_the_stamped_heads() -> None:
    a, b = _unit_row(), _unit_row()
    b[cs.CARRIED_ROW_KEY] = {"kind": cs.CARRIED_UNSUPPORTED}
    kept, carried = cs.partition_carried([a, b])
    assert kept == [a] and carried == [b]
    kept, carried = cs.partition_carried([a])
    assert kept == [a] and carried == []


# ---------------------------------------------------------------------------
# 2. THE FLOOR FALLBACK — never promotes a read older than N windows
# ---------------------------------------------------------------------------


def _fallback_pair(candidate_age_h: float, *, cadence_h: float | None = CADENCE_H):
    newer_failing = _unit_row(age_h=2.0, cadence_h=cadence_h)
    newer_failing.update(effective_confidence=0.2, faithfulness_score=0.2)
    newer_failing[cw._EVIDENCE_TIER_KEY] = cw.PERIPHERY_TIER
    older_passing = _unit_row(age_h=candidate_age_h, cadence_h=cadence_h)
    return older_passing, newer_failing


def test_the_fallback_refuses_a_read_older_than_n_windows() -> None:
    old, newer = _fallback_pair(40.0)  # 40 h > 3 × 11 h
    out = cw.select_floor_fallback([old], [], [newer], now=NOW, windows=3)
    assert len(out) == 1
    assert cw.FLOOR_FALLBACK_KEY not in out[0], "an over-age read is never promoted"
    verdict = out[0][cs.CARRIED_ROW_KEY]
    assert verdict["cause"] == cs.CAUSE_FALLBACK_TOO_OLD
    assert verdict["head_age_h"] == pytest.approx(40.0)
    assert "not promoted" in cs.verdict_sentence(verdict)


def test_a_young_fallback_is_promoted_and_marked_with_its_age() -> None:
    young, newer = _fallback_pair(20.0)
    out = cw.select_floor_fallback([young], [], [newer], now=NOW, windows=3)
    meta = out[0][cw.FLOOR_FALLBACK_KEY]
    assert meta["fallback"] is True
    assert meta["age_h"] == pytest.approx(20.0)
    assert cs.CARRIED_ROW_KEY not in out[0]


def test_a_fallback_with_no_known_cadence_is_promoted_as_before_with_its_age() -> None:
    old, newer = _fallback_pair(400.0, cadence_h=None)
    out = cw.select_floor_fallback([old], [], [newer], now=NOW)
    assert out[0][cw.FLOOR_FALLBACK_KEY]["age_h"] == pytest.approx(400.0)


def test_the_ledger_names_a_refused_fallback_and_never_calls_it_below_floor() -> None:
    old, newer = _fallback_pair(40.0)
    refused = cw.select_floor_fallback([old], [], [newer], now=NOW, windows=3)
    ledger = cw.build_coverage_ledger(["disruption_status"], [], [newer, *refused], now=NOW)
    assert ledger[0]["status"] == cw.COVERAGE_CARRIED == "carried_unsupported"
    assert ledger[0]["cause"] == cs.CAUSE_FALLBACK_TOO_OLD
    block = cw.render_coverage_ledger_block(ledger, horizon_hours=336, floor=0.5, max_age_hours=None)
    assert "disruption_status: CARRIED, UNSUPPORTED" in block
    assert "NEVER restate, band or score that status" in block


def test_a_ledger_with_no_carried_unit_renders_byte_identically() -> None:
    row = _unit_row()
    ledger = cw.build_coverage_ledger(["disruption_status"], [row], [], now=NOW)
    block = cw.render_coverage_ledger_block(ledger, horizon_hours=336, floor=0.5, max_age_hours=None)
    assert "CARRIED" not in block


# ---------------------------------------------------------------------------
# 3. THE SYNTHESIZER — run_method on a fixture
# ---------------------------------------------------------------------------


class _CannedLLM:
    subprovider = "carried_state_test_double"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": list(messages)})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        from types import SimpleNamespace

        resp = SimpleNamespace()
        resp.content = json.dumps({
            "title": "Composed read", "body": "A composed clause [[ref:1]].",
            "confidence": 0.6, "evidence": [], "tags": [],
        })
        resp.usage = _Usage()
        return resp


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _prompt(llm: _CannedLLM) -> str:
    for m in llm.calls[0]["messages"]:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            return str(m.get("content") if isinstance(m, dict) else m.content)
    raise AssertionError("no user message")


def _composition_rows() -> tuple[dict[str, Any], dict[str, Any]]:
    carried = _unit_row(analyst_id="energy_security", target_id="country_g20_jp",
                        severity="high", indicators=_indicators("not_observed"))
    carried["title"] = "Japan energy security stays HIGH-CARRIED-MARKER"
    carried[synth.HORIZON_ROW_KEY] = 336
    carried[cs.CARRIED_ROW_KEY] = cs.carry_verdict(
        carried, last_evidence=_evidence(313.0), cadence_h=CADENCE_H, now=NOW
    )
    live = _unit_row(analyst_id="escalation", target_id="country_g20_jp",
                     indicators=_indicators("triggered"))
    live["body"] = (
        "**BLUF:** Cross-border shelling rose this window, with two strikes on "
        "the northern line reported by separate outlets."
    )
    live[synth.HORIZON_ROW_KEY] = 336
    return carried, live


_JP_OPTS = {
    "analyst_id": "country_composition",
    "target_id": "country_g20_jp",
    "source_analyst_ids": ["energy_security", "escalation"],
}


@pytest.mark.asyncio
async def test_run_method_keeps_a_carried_head_out_of_the_basis_and_names_it(monkeypatch) -> None:
    """The generative regime: the carried head is never rendered (so never
    banded) and the prompt's coverage ledger names it as the typed absence."""
    from legba.data.analysts import assembly_payload as ap

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "0")
    carried, live = _composition_rows()
    llm = _CannedLLM()
    result = await synth.run_method([carried, live], dict(_JP_OPTS), _Deps(llm))
    prompt = _prompt(llm)
    assert "HIGH-CARRIED-MARKER" not in prompt
    assert "energy_security: CARRIED, UNSUPPORTED" in prompt
    assert "313 h old" in prompt
    assert "NEVER restate, band or score that status" in prompt
    # The record is not derived from the carried head.
    assert carried["id"] not in result.derived_from and live["id"] in result.derived_from


@pytest.mark.asyncio
async def test_the_assembly_record_persists_the_carried_row_and_its_arm_accepts_it(monkeypatch) -> None:
    """The assembly regime (live): no block for the carried head, a persisted
    ``carried_unsupported`` coverage row with its proof, and the coverage arm
    reads the status as part of the closed set."""
    from legba.data.analysts import assembly_payload as ap
    from legba.data.provenance import assembly_arms as arms

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    carried, live = _composition_rows()
    result = await synth.run_method([carried, live], dict(_JP_OPTS), _Deps(_CannedLLM()))
    assembly = result.finding.data["assembly"]
    assert "HIGH-CARRIED-MARKER" not in result.finding.body
    coverage = {c["unit"]: c for c in assembly["coverage"]}
    assert coverage["energy_security"]["status"] == "carried_unsupported"
    assert coverage["energy_security"]["carried_status"] == "high"
    assert coverage["energy_security"]["last_evidence_age_h"] == pytest.approx(313.0)
    assert coverage["escalation"]["status"] == "in_basis"
    arm = arms._coverage_completeness(assembly)
    assert not [f for f in arm.findings if f.reason == arms.COVERAGE_STATUS_INVALID]


@pytest.mark.asyncio
async def test_a_basis_emptied_by_the_carry_rule_never_says_no_desk_read(monkeypatch) -> None:
    """The reads exist: an all-carried desk says the EVIDENCE is absent, names
    the heads, and never prints "No desk read"."""
    from legba.data.analysts import assembly_payload as ap

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "0")
    carried, _ = _composition_rows()
    result = await synth.run_method([carried], dict(_JP_OPTS), _Deps(_CannedLLM()))
    assert result.finding.title == "Every desk read is carried, unsupported"
    assert "No desk read" not in result.finding.body
    assert "absence of EVIDENCE" in result.finding.body
    [verdict] = result.finding.data["carried_unsupported"]
    assert verdict["unit"] == "energy_security" and verdict["status"] == "high"


# ---------------------------------------------------------------------------
# 4. READ_SLICE side — the stamp, with a fake connection
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return list(self.rows)


@pytest.mark.asyncio
async def test_stamp_issues_no_query_when_no_head_is_a_candidate() -> None:
    conn = _Conn([])
    rows = [_unit_row(indicators=_indicators("triggered")), _unit_row(indicators=None)]
    assert await cs.stamp_carried_state(conn, rows, now=NOW) == 0
    assert conn.calls == []


@pytest.mark.asyncio
async def test_stamp_convicts_a_basis_head_and_skips_periphery_and_continuity() -> None:
    head = _unit_row(indicators=_indicators("not_observed"))
    peri = _unit_row(indicators=_indicators("not_observed"), analyst_id="escalation")
    peri["_evidence_tier"] = "periphery"
    cont = _unit_row(indicators=_indicators("not_observed"), analyst_id="economic_coercion")
    cont["_continuity"] = "prior"
    conn = _Conn([{
        "analyst_id": "disruption_status", "target_id": "flow_semiconductors",
        "id": "3c38f383-164f-42e1-afde-9f9d86993e3d",
        "produced_at": NOW - timedelta(hours=336),
    }])
    assert await cs.stamp_carried_state(conn, [head, peri, cont], now=NOW) == 1
    assert len(conn.calls) == 1 and conn.calls[0][0] == cs.LAST_EVIDENCE_SQL
    assert head[cs.CARRIED_ROW_KEY]["last_evidence_age_h"] == pytest.approx(336.0)
    assert cs.CARRIED_ROW_KEY not in peri and cs.CARRIED_ROW_KEY not in cont


@pytest.mark.asyncio
async def test_a_failed_evidence_read_stamps_nothing() -> None:
    class _Boom:
        async def fetch(self, *a: Any) -> Any:
            raise RuntimeError("down")

    head = _unit_row(indicators=_indicators("not_observed"))
    assert await cs.stamp_carried_state(_Boom(), [head], now=NOW) == 0
    assert cs.CARRIED_ROW_KEY not in head


# ---------------------------------------------------------------------------
# 5. The reads on the REAL driver
# ---------------------------------------------------------------------------

_UNIT = "e1_carry_fixture_unit"
_TARGET = "flow_e1_carry_fixture"
_OWNER = "test_e1_carried_state"


async def _seed_finding(conn, *, age_h: float, statuses: list[str], severity: str, now: datetime) -> str:
    rid = uuid4()
    await conn.execute(
        "INSERT INTO public.analyst_outputs "
        "  (id, kind, title, body, confidence, severity, data, target_id, analyst_id, "
        "   produced_at, schema_uri) "
        "VALUES ($1, 'finding', 'e1 fixture', 'e1', 0.6, $2, $3::jsonb, $4, $5, $6, "
        "        'iglu:legba/finding/jsonschema/1-0-0')",
        rid,
        severity,
        json.dumps({
            "tags": [f"severity:{severity}"],
            "data": {"indicators": [{"id": f"i{i}", "status": s} for i, s in enumerate(statuses)]},
        }),
        _TARGET,
        _UNIT,
        now - timedelta(hours=age_h),
    )
    return str(rid)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_reads_convict_the_fixture_unit_on_the_real_driver(migrated_pg) -> None:
    from legba.data.postgres import PostgresStore

    store = PostgresStore(migrated_pg)
    await store.connect()
    now = datetime.now(timezone.utc)
    try:
        async with store.acquire() as conn:
            await conn.execute("DELETE FROM public.analyst_outputs WHERE target_id = $1", _TARGET)
            await conn.execute(
                "DELETE FROM public.analyst_descriptors WHERE owner = $1", _OWNER
            )
            await conn.execute(
                "INSERT INTO public.analyst_descriptors "
                "  (descriptor_id, version, schema_uri, is_head, kind, state, owner, name, body) "
                "VALUES ($1, 'e1v1', 'legba/analyst/2.0.0', TRUE, 'inline_target', "
                "        'active', $2, $1, $3::jsonb)",
                _UNIT,
                _OWNER,
                json.dumps({"cadence": {"cooldown_seconds": 39600}}),
            )
            evidencing = await _seed_finding(conn, age_h=336, statuses=["triggered"], severity="moderate", now=now)
            for age in (60, 30, 2):
                await _seed_finding(conn, age_h=age, statuses=["not_observed", "not_observed"], severity="moderate", now=now)

            verdicts = await cs.unit_carried_verdicts(conn, target=_TARGET, units=[_UNIT], now=now)
            assert len(verdicts) == 1
            v = verdicts[0]
            assert v["unit"] == _UNIT and v["status"] == "moderate"
            assert v["last_evidence_id"] == evidencing
            assert v["last_evidence_age_h"] == pytest.approx(336.0, abs=0.1)
            assert v["cadence_h"] == pytest.approx(11.0)

            # A fresh triggered read clears it — the status is re-evidenced.
            await _seed_finding(conn, age_h=1, statuses=["triggered"], severity="moderate", now=now)
            assert await cs.unit_carried_verdicts(conn, target=_TARGET, units=[_UNIT], now=now) == []
    finally:
        async with store.acquire() as conn:
            await conn.execute("DELETE FROM public.analyst_outputs WHERE target_id = $1", _TARGET)
            await conn.execute("DELETE FROM public.analyst_descriptors WHERE owner = $1", _OWNER)
        await store.close()
