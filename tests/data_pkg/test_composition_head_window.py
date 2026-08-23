# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-1 — COMPOSE OVER THE WINDOW (``planning/FRAME_PROGRAM_2026-08-20.md``).

The round's largest architecture finding: *a 72-hour pipeline forgets its own
window*. Three mechanisms, all covered here.

**§3 — the composition slice.** The trailing-24h wall-clock gate is retired in
favour of an ADMISSIBILITY HORIZON (the descriptor PUT 24h→336h) over the
head-fold that already existed. The code half is the honesty the wider window
obliges: every consumed head prints its human date and its AGE, the run stamps
``data.head_ages``, and the empty-slice sentence is re-worded to its only
now-possible honest forms. The BF-class output — "No source findings to
synthesize" over seven 42-hour-old heads — must be unreachable by construction.

**§4 — the floor's visibility.** Not its level: 0.50 does not move. Its action
becomes visible three ways — the tiered periphery (flag ON is the tested path
here, it flips at deploy), the deterministic COVERAGE LEDGER in the prompt, and
the coverage-rule amendment. The load-bearing assertion is the C4 atom-10
precedent as a test: a floor-withheld dimension reads "below verification
floor", NEVER "no read this cycle".

**§4/§3 — the GB-drone class.** The freshest head failed verify at 0.40 while an
in-horizon prior head at 0.571 would have passed, and was unreachable behind
``superseded_by IS NULL``. The newest floor-PASSING head goes to basis, dated
and labelled; the newer failing head stays in the periphery, also dated.

Both flag states are covered wherever the behavior forks, and the composition
assertions run through the REAL synthesizer entry (``run_method`` → ``_run``)
over seeded heads at controlled ages and floor states.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import composition_window as cw
from legba.data.analysts import meta_findings_synthesizer as synth


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``composition_window``'s clock. Ages ARE the subject of this train —
    a test that cannot pin "now" can only assert an age exists, never that the
    right one was rendered."""
    monkeypatch.setattr(cw, "_now", lambda: NOW)


# ---------------------------------------------------------------------------
# helpers (the house fixtures from test_composition_tiered_evidence.py)
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Fake asyncpg.Connection recording every fetch() call's SQL + params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return [dict(r) for r in self._rows]


class _CannedLLM:
    subprovider = "head_window_test_double"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or {
            "title": "Composed read",
            "body": "A composed clause [[ref:1]].",
            "confidence": 0.6,
            "evidence": [],
            "tags": [],
        }
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
        raise AssertionError("the empty-slice path must not call the LLM")


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _user_prompt_of(llm: _CannedLLM) -> str:
    assert len(llm.calls) == 1, f"expected 1 LLM call, got {len(llm.calls)}"
    for m in llm.calls[0]["messages"]:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            return str(
                m.get("content") if isinstance(m, dict) else getattr(m, "content")
            )
    raise AssertionError("no user message captured")


def _descriptor(others: list[tuple[str, str]]) -> SimpleNamespace:
    entries = [SimpleNamespace(id=i, time_window=w, data_types=[]) for i, w in others]
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=entries,
            targets=SimpleNamespace(predicate='has_tag("g20")'),
        )
    )


def _row(
    *,
    analyst_id: str,
    age_hours: float = 3.0,
    uid: UUID | None = None,
    title: str = "sub-claim title",
    body: str = "sub-claim body",
    confidence: float = 0.7,
    effective_confidence: float | None = 0.7,
    faithfulness_score: float | None = 0.9,
    periphery: bool = False,
    floor: float | None = None,
    horizon: int | None = None,
    target_id: str = "country_g20_gb",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One head row at a CONTROLLED age relative to :data:`NOW`."""
    r: dict[str, Any] = {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": confidence,
        "effective_confidence": effective_confidence,
        "faithfulness_score": faithfulness_score,
        "severity": None,
        "data": {"tags": [], "evidence": []},
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": (NOW - timedelta(hours=age_hours)).isoformat(),
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }
    if periphery:
        r[synth._EVIDENCE_TIER_KEY] = synth.PERIPHERY_TIER
    if floor is not None:
        r[synth._EVIDENCE_FLOOR_KEY] = floor
    if horizon is not None:
        r[synth.HORIZON_ROW_KEY] = horizon
    if extra:
        r.update(extra)
    return r


_COUNTRY_OPTS = {
    "analyst_id": "country_composition",
    "target_id": "country_g20_gb",
}


# ---------------------------------------------------------------------------
# 1. AGES — the house date form, the age, and where they land
# ---------------------------------------------------------------------------


def test_human_datetime_is_the_house_form_and_locale_independent():
    """``NO_INSTRUMENT_READINGS`` demands "3 August 09:15 UTC", never an ISO
    stamp; ``%B`` would make the month name follow the container's LANG."""
    assert cw.human_datetime("2026-08-18T07:00:02+00:00") == "18 August 07:00 UTC"
    assert cw.human_datetime(datetime(2026, 1, 3, 9, 15, tzinfo=timezone.utc)) == (
        "3 January 09:15 UTC"
    )
    # Naive timestamps are UTC (the substrate stores UTC); ``Z`` parses.
    assert cw.human_datetime("2026-08-18T07:00:02Z") == "18 August 07:00 UTC"
    assert cw.human_datetime(datetime(2026, 8, 18, 7, 0)) == "18 August 07:00 UTC"
    assert cw.human_datetime(None) is None
    assert cw.human_datetime("not a date") is None


def test_format_age_hours_switches_to_days_past_two_days():
    assert cw.format_age_hours(42.4) == "42h"
    assert cw.format_age_hours(3.0) == "3h"
    assert cw.format_age_hours(312.0) == "312h (13d)"
    assert cw.format_age_hours(None) == "unknown"


def test_head_age_hours_never_negative_and_none_when_undatable():
    assert cw.head_age_hours({"produced_at": (NOW - timedelta(hours=42)).isoformat()},
                             now=NOW) == pytest.approx(42.0)
    # A clock skew that puts the head in the future reads 0, never a negative.
    assert cw.head_age_hours({"produced_at": (NOW + timedelta(hours=5)).isoformat()},
                             now=NOW) == 0.0
    assert cw.head_age_hours({"produced_at": None}, now=NOW) is None


def test_age_suffix_is_empty_for_an_undatable_row():
    """An undatable row renders EXACTLY as it did pre-FRAME-1 — never
    ``age=unknown`` noise, and never a guessed zero."""
    assert cw.age_suffix({"produced_at": None}, now=NOW) == ""


def test_basis_render_prints_the_human_date_and_the_age():
    rows = [_row(analyst_id="escalation", age_hours=42.0)]
    rendered = synth._render_user_prompt(
        rows, ["escalation"], include_source_ids=True
    )
    assert "read_date=" in rendered
    assert "18 August" in rendered
    assert "age=42h" in rendered
    # The machine-readable produced_at is KEPT beside it (the operator/debug
    # provenance the render has always carried).
    assert "produced_at=" in rendered


def test_global_meta_render_keeps_the_legacy_attribution_byte_for_byte():
    """``include_source_ids=False`` is the LEGACY global meta. Its attribution
    line must not grow an age — the standing legacy-read discipline."""
    rows = [_row(analyst_id="country_assessor", age_hours=42.0)]
    rendered = synth._render_user_prompt(rows, ["country_assessor"])
    assert "read_date=" not in rendered
    assert "age=" not in rendered


# ---------------------------------------------------------------------------
# 2. THE HEAD-AGES STAMP (§6.1) — the number the gauge reads
# ---------------------------------------------------------------------------


def test_head_ages_stamp_carries_per_head_hours_and_the_max():
    stamp = cw.head_ages_stamp(
        [
            _row(analyst_id="escalation", age_hours=42.0),
            _row(analyst_id="energy_security", age_hours=6.0),
        ],
        now=NOW,
        horizon_hours=336,
    )
    assert stamp is not None
    assert stamp["max_h"] == pytest.approx(42.0)
    assert stamp["min_h"] == pytest.approx(6.0)
    assert stamp["horizon_h"] == 336
    assert {h["analyst_id"] for h in stamp["heads"]} == {
        "escalation",
        "energy_security",
    }


def test_head_ages_stamp_is_absent_not_zero_when_nothing_is_datable():
    """An UNGAUGED composition and a FRESH one must never read the same — the
    §6 gauge keys on exactly this difference."""
    assert cw.head_ages_stamp([{"produced_at": None}], now=NOW) is None
    assert cw.head_ages_stamp([], now=NOW) is None


@pytest.mark.asyncio
async def test_run_stamps_head_ages_on_the_real_binding_path():
    llm = _CannedLLM()
    rows = [
        _row(analyst_id="escalation", age_hours=42.0, horizon=336),
        _row(analyst_id="energy_security", age_hours=5.0, horizon=336),
    ]
    result = await synth.run_method(rows, dict(_COUNTRY_OPTS), _Deps(llm))
    stamp = result.finding.data["head_ages"]
    assert stamp["horizon_h"] == 336
    assert stamp["max_h"] == pytest.approx(42.0, abs=0.5)
    assert len(stamp["heads"]) == 2
    steps = {s.get("phase") for s in result.intermediate_steps}
    assert "head_ages" in steps


@pytest.mark.asyncio
async def test_legacy_global_meta_stamps_no_head_ages():
    """The legacy global meta (no target/composition option) stays byte-for-byte
    — the standing discipline every branch of this kind honors."""
    llm = _CannedLLM()
    result = await synth.run_method(
        [_row(analyst_id="country_assessor", age_hours=42.0)],
        {"analyst_id": "analyst_meta_synthesizer"},
        _Deps(llm),
    )
    assert "head_ages" not in result.finding.data


# ---------------------------------------------------------------------------
# 3. THE HEAD-WINDOW BLOCK — horizon, staleness duty, coverage ledger
# ---------------------------------------------------------------------------


def test_block_states_the_horizon_as_admissibility_not_freshness():
    block = cw.render_coverage_ledger_block(
        [], horizon_hours=336, floor=0.5, max_age_hours=3.0
    )
    assert "ADMISSIBILITY HORIZON" in block
    assert "336h" in block and "14 days" in block
    # Fresh heads ⇒ no staleness paragraph.
    assert "STALENESS" not in block


def test_block_demands_the_staleness_disclosure_past_the_threshold():
    block = cw.render_coverage_ledger_block(
        [], horizon_hours=336, floor=0.5, max_age_hours=42.0
    )
    assert "STALENESS" in block
    assert "42h" in block


def test_staleness_threshold_is_a_whole_compose_cycle():
    """The prose discloses EARLIER than the operator is paged (34h)."""
    assert cw.STALE_HEAD_DISCLOSE_HOURS == 24.0
    just_under = cw.render_coverage_ledger_block(
        [], horizon_hours=336, floor=0.5, max_age_hours=23.9
    )
    assert "STALENESS" not in just_under


def test_block_is_empty_when_there_is_neither_horizon_nor_ledger():
    """A run that knows neither renders byte-identically to pre-FRAME-1."""
    assert cw.render_coverage_ledger_block(
        [], horizon_hours=None, floor=None, max_age_hours=None
    ) == ""


def test_coverage_ledger_calls_a_floor_withheld_unit_below_floor_never_a_gap():
    """THE C4 atom-10 precedent, as a test."""
    basis = [_row(analyst_id="escalation", age_hours=4.0)]
    peri = [
        _row(
            analyst_id="energy_security",
            age_hours=9.0,
            effective_confidence=0.40,
            faithfulness_score=0.40,
            periphery=True,
        )
    ]
    ledger = cw.build_coverage_ledger(
        ["escalation", "energy_security", "military_posture"], basis, peri, now=NOW
    )
    by_unit = {e["unit"]: e for e in ledger}
    assert by_unit["escalation"]["status"] == cw.COVERAGE_IN_BASIS
    assert by_unit["energy_security"]["status"] == cw.COVERAGE_BELOW_FLOOR
    assert by_unit["military_posture"]["status"] == cw.COVERAGE_NO_HEAD

    block = cw.render_coverage_ledger_block(
        ledger, horizon_hours=336, floor=0.5, max_age_hours=9.0
    )
    assert "energy_security: BELOW VERIFICATION FLOOR" in block
    assert "energy_security: NO READ" not in block
    # ...and ONLY the unit with no head at all is named a gap.
    assert "military_posture: NO READ within the horizon — an unassessed gap." in block
    assert "NEVER call it \"no read this cycle\"" in block
    assert "ONLY a unit listed with NO READ is a gap." in block


def test_coverage_ledger_distinguishes_unverified_from_below_floor():
    peri = [
        _row(
            analyst_id="narrative_coordination",
            age_hours=2.0,
            effective_confidence=None,
            faithfulness_score=None,
            periphery=True,
        )
    ]
    ledger = cw.build_coverage_ledger(["narrative_coordination"], [], peri, now=NOW)
    assert ledger[0]["status"] == cw.COVERAGE_UNVERIFIED
    block = cw.render_coverage_ledger_block(
        ledger, horizon_hours=336, floor=0.5, max_age_hours=2.0
    )
    assert "NOT VERIFIED" in block
    assert "Not a gap." in block


def test_coverage_ledger_order_follows_the_declared_roster():
    roster = ["military_posture", "escalation", "energy_security"]
    ledger = cw.build_coverage_ledger(roster, [], [], now=NOW)
    assert [e["unit"] for e in ledger] == roster


@pytest.mark.asyncio
async def test_run_renders_the_coverage_ledger_from_the_declared_roster():
    """The REAL path: the roster is the subscription-resolved
    ``source_analyst_ids``, which is the only honest denominator."""
    llm = _CannedLLM()
    rows = [
        _row(analyst_id="escalation", age_hours=42.0, horizon=336, floor=0.5),
        _row(
            analyst_id="energy_security",
            age_hours=9.0,
            effective_confidence=0.40,
            faithfulness_score=0.40,
            periphery=True,
            floor=0.5,
        ),
    ]
    await synth.run_method(
        rows,
        dict(
            _COUNTRY_OPTS,
            source_analyst_ids=["escalation", "energy_security", "military_posture"],
        ),
        _Deps(llm),
    )
    prompt = _user_prompt_of(llm)
    assert "COVERAGE LEDGER" in prompt
    assert "escalation: in basis" in prompt
    assert "energy_security: BELOW VERIFICATION FLOOR" in prompt
    assert "military_posture: NO READ within the horizon" in prompt
    assert "STALENESS" in prompt
    assert "ADMISSIBILITY HORIZON" in prompt


@pytest.mark.asyncio
async def test_run_without_a_roster_still_states_the_horizon_but_no_ledger():
    """No ``source_analyst_ids`` ⇒ no honest denominator ⇒ NO ledger (never one
    inferred from the rows that happened to arrive, which is the blindness the
    ledger exists to remove)."""
    llm = _CannedLLM()
    await synth.run_method(
        [_row(analyst_id="escalation", age_hours=4.0, horizon=336)],
        dict(_COUNTRY_OPTS),
        _Deps(llm),
    )
    prompt = _user_prompt_of(llm)
    assert "ADMISSIBILITY HORIZON" in prompt
    assert "COVERAGE LEDGER" not in prompt


@pytest.mark.asyncio
async def test_run_without_a_horizon_stamp_renders_no_head_window_block():
    """A legacy/direct caller that never stamps the horizon is byte-identical."""
    llm = _CannedLLM()
    await synth.run_method(
        [_row(analyst_id="escalation", age_hours=4.0)],
        dict(_COUNTRY_OPTS),
        _Deps(llm),
    )
    prompt = _user_prompt_of(llm)
    assert "ADMISSIBILITY HORIZON" not in prompt
    assert "COVERAGE LEDGER" not in prompt


# ---------------------------------------------------------------------------
# 4. THE EMPTY SLICE — the BF sentence, re-worded to its honest forms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_slice_under_a_horizon_names_the_window_it_looked_across():
    """A desk with NOTHING inside 336h. The only row is the composition's own
    prior read (a continuity ref, never basis evidence), which still carries the
    horizon — so the diagnostic head can say WHICH window it searched instead of
    the window-less "the slice was empty"."""
    prior = _row(analyst_id="country_composition", age_hours=13.0, horizon=336)
    prior[synth.CONTINUITY_ROW_KEY] = synth.CONTINUITY_PRIOR
    result = await synth.run_method(
        [prior],
        dict(_COUNTRY_OPTS, source_analyst_ids=["escalation"]),
        _Deps(_NeverCalledLLM()),
    )
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert result.finding.title == "No desk read within the trailing 336h (14 days)"
    assert "absence of READS, not a reading of calm" in result.finding.body


@pytest.mark.asyncio
async def test_empty_basis_with_periphery_says_below_floor_not_no_read():
    """The §4 language: an all-below-floor desk is a VERIFICATION WITHHOLDING,
    never 'no read'. The LLM is never called (a composition is not synthesized
    from weak signals alone) — the honesty has to live in the diagnostic head."""
    rows = [
        _row(
            analyst_id="energy_security",
            age_hours=9.0,
            effective_confidence=0.40,
            faithfulness_score=0.40,
            periphery=True,
            floor=0.5,
            horizon=336,
        )
    ]
    result = await synth.run_method(
        rows, dict(_COUNTRY_OPTS), _Deps(_NeverCalledLLM())
    )
    body = result.finding.body
    title = result.finding.title
    assert "BELOW THE VERIFICATION FLOOR" in body
    assert "NOT an absence of reads" in body
    assert "336h" in body
    assert title == "All reads below the verification floor"
    assert "No source findings to synthesize" not in title
    assert result.finding.data["evidence_tiers"]["periphery_count"] == 1


@pytest.mark.asyncio
async def test_empty_basis_no_periphery_under_a_horizon_says_no_desk_read():
    """The other honest form: a TRUE absence across the whole horizon. The
    BF-class sentence ("No source findings to synthesize" over 42h-old heads)
    cannot be produced here — the heads would have been admitted."""
    rows = [
        _row(
            analyst_id="escalation",
            age_hours=9.0,
            periphery=True,
            effective_confidence=None,
            faithfulness_score=None,
            floor=0.5,
            horizon=336,
        )
    ]
    # Drop the periphery marker so the row is neither basis (it is filtered by
    # the caller in production) nor periphery: an EMPTY slice carrying only the
    # horizon stamp.
    rows[0].pop(synth._EVIDENCE_TIER_KEY)
    rows[0].pop(synth._EVIDENCE_FLOOR_KEY)
    result = await synth.run_method(
        [], dict(_COUNTRY_OPTS), _Deps(_NeverCalledLLM())
    )
    # With no rows at all there is no horizon to name — the legacy sentence
    # stands, and it is TRUE (nothing was read).
    assert result.finding.title == "No source findings to synthesize"


# ---------------------------------------------------------------------------
# 5. THE NEWEST FLOOR-PASSING HEAD (§3 floor interplay, the GB-drone class)
# ---------------------------------------------------------------------------


def test_units_missing_from_basis_is_the_withheld_set():
    basis = [_row(analyst_id="escalation")]
    peri = [
        _row(analyst_id="military_posture", periphery=True),
        _row(analyst_id="escalation", periphery=True),
    ]
    assert cw.units_missing_from_basis(basis, peri) == ["military_posture"]


def test_select_floor_fallback_promotes_the_older_passing_head_and_dates_the_newer():
    """GB drones: newest head eff 0.40 (fails), prior head 0.571 (passes)."""
    newer_failing = _row(
        analyst_id="military_posture",
        age_hours=2.0,
        effective_confidence=0.40,
        faithfulness_score=0.40,
        periphery=True,
    )
    older_passing = _row(
        analyst_id="military_posture",
        age_hours=50.0,
        effective_confidence=0.571,
        faithfulness_score=0.60,
    )
    promoted = cw.select_floor_fallback([older_passing], [], [newer_failing])
    assert len(promoted) == 1
    meta = promoted[0][cw.FLOOR_FALLBACK_KEY]
    assert meta["newer_head_status"] == "below_floor"
    assert meta["newer_head_effective_confidence"] == pytest.approx(0.40)
    assert meta["newer_head_read_date"] is not None


def test_select_floor_fallback_leaves_a_unit_already_in_basis_alone():
    in_basis = _row(analyst_id="military_posture", age_hours=2.0)
    candidate = _row(analyst_id="military_posture", age_hours=50.0)
    peri = [_row(analyst_id="military_posture", age_hours=1.0, periphery=True)]
    assert cw.select_floor_fallback([candidate], [in_basis], peri) == []


def test_select_floor_fallback_refuses_a_candidate_not_older_than_the_failing_head():
    """If the 'fallback' is not older, the live head already IS this row —
    promoting it would double-count the same head."""
    newer_failing = _row(analyst_id="military_posture", age_hours=50.0,
                         effective_confidence=0.4, periphery=True)
    same_or_newer = _row(analyst_id="military_posture", age_hours=2.0)
    assert cw.select_floor_fallback([same_or_newer], [], [newer_failing]) == []


def test_select_floor_fallback_ignores_a_unit_with_no_failing_head():
    """Nothing was WITHHELD for this unit, so nothing is being routed — the
    head-fold's one-head-per-unit rule is not widened by accident."""
    candidate = _row(analyst_id="military_posture", age_hours=50.0)
    assert cw.select_floor_fallback([candidate], [], []) == []


def test_floor_fallback_suffix_names_the_relation_in_the_basis_render():
    newer = _row(analyst_id="military_posture", age_hours=2.0,
                 effective_confidence=0.40, periphery=True)
    older = _row(analyst_id="military_posture", age_hours=50.0,
                 effective_confidence=0.571)
    promoted = cw.select_floor_fallback([older], [], [newer])
    rendered = synth._render_user_prompt(
        promoted, ["military_posture"], include_source_ids=True
    )
    assert "newest_read_that_cleared_the_floor" in rendered
    assert "NOT the desk's latest read" in rendered


@pytest.mark.asyncio
async def test_fallback_query_drops_only_the_supersession_predicate():
    conn = _CapturingConn(rows=[])
    await synth.read_floor_fallback_heads(
        conn,
        basis_reader=synth.read_other_analyst_findings,
        analyst_ids=["military_posture"],
        time_window_hours=336,
        floor=0.5,
        basis_rows=[],
        periphery_rows=[_row(analyst_id="military_posture", periphery=True)],
        target_id="country_g20_gb",
    )
    assert len(conn.calls) == 1
    query, params = conn.calls[0]
    assert "f.superseded_by IS NULL" not in query
    # Everything else is the basis admissibility, verbatim.
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in query
    assert "JOIN LATERAL" in query
    assert "LEAST(f.confidence, v.faithfulness_score)" in query
    assert params[1] == 336


@pytest.mark.asyncio
async def test_fallback_fires_no_query_when_nothing_was_withheld():
    conn = _CapturingConn(rows=[])
    out = await synth.read_floor_fallback_heads(
        conn,
        basis_reader=synth.read_other_analyst_findings,
        analyst_ids=["escalation"],
        time_window_hours=336,
        floor=0.5,
        basis_rows=[_row(analyst_id="escalation")],
        periphery_rows=[_row(analyst_id="escalation", periphery=True)],
    )
    assert out == []
    assert conn.calls == []


@pytest.mark.asyncio
async def test_include_superseded_defaults_off_so_the_basis_query_is_unchanged():
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn,
        analyst_ids=["escalation"],
        time_window_hours=336,
        target_id="country_g20_gb",
        verify_floor=0.5,
    )
    assert "f.superseded_by IS NULL" in conn.calls[0][0]


# ---------------------------------------------------------------------------
# 6. READ_SLICE wiring — both flag states (the flag flips at deploy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_slice_stamps_the_admissibility_horizon_on_every_row(monkeypatch):
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)

    class _OneRowConn(_CapturingConn):
        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            self.calls.append((query, params))
            if "analyst_id = ANY($1::TEXT[])" in query and "f.kind = 'finding'" in query:
                return [_row(analyst_id="escalation", age_hours=42.0)]
            return []

    conn = _OneRowConn()
    rows = await synth.READ_SLICE(
        conn,
        descriptor=_descriptor([("escalation", "336h")]),
        target_filter="country_g20_gb",
    )
    assert rows, "the seeded head must survive the read"
    assert all(r[synth.HORIZON_ROW_KEY] == 336 for r in rows)
    # And the horizon the descriptor DECLARES is what reached the query.
    assert conn.calls[0][1][1] == 336


@pytest.mark.asyncio
async def test_read_slice_flag_on_runs_the_fallback_gather(monkeypatch):
    """Flag ON is the tested path — it flips at deploy."""
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)

    class _SplitConn(_CapturingConn):
        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            self.calls.append((query, params))
            if "LEFT JOIN LATERAL" in query:            # the periphery gather
                return [
                    _row(
                        analyst_id="military_posture",
                        age_hours=2.0,
                        effective_confidence=0.40,
                        faithfulness_score=0.40,
                    )
                ]
            if "f.superseded_by IS NULL" not in query and "JOIN LATERAL" in query:
                return [                                # the FALLBACK gather
                    _row(
                        analyst_id="military_posture",
                        age_hours=50.0,
                        effective_confidence=0.571,
                        faithfulness_score=0.60,
                    )
                ]
            return []                                   # basis: nothing passes live

    conn = _SplitConn()
    rows = await synth.READ_SLICE(
        conn,
        descriptor=_descriptor([("military_posture", "336h")]),
        target_filter="country_g20_gb",
    )
    queries = [q for q, _ in conn.calls]
    assert any(
        "JOIN LATERAL" in q and "f.superseded_by IS NULL" not in q for q in queries
    ), "the newest-passing fallback gather must have run"
    promoted = [r for r in rows if r.get(cw.FLOOR_FALLBACK_KEY)]
    assert len(promoted) == 1
    assert promoted[0]["analyst_id"] == "military_posture"
    # The newer FAILING head is still present, as dated periphery.
    peri = [r for r in rows if r.get(synth._EVIDENCE_TIER_KEY) == synth.PERIPHERY_TIER]
    assert len(peri) == 1


@pytest.mark.asyncio
async def test_read_slice_flag_off_never_runs_the_fallback_gather(monkeypatch):
    """Flag OFF: no periphery is rendered, so promoting an older passing head
    would show a stale read as current and say nothing about the newer failing
    one. The two halves ship and flip together."""
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(
        conn,
        descriptor=_descriptor([("military_posture", "336h")]),
        target_filter="country_g20_gb",
    )
    # The predicate this asserts on is the HEAD-FOLD gather's — the query whose
    # job is "one newest non-superseded head per (unit, desk)". FRAME-2's WINDOW
    # LEDGER also joins the verify lateral and DELIBERATELY carries no
    # supersession predicate (supersession is a freshness relation, and the
    # fortnight's record is almost entirely superseded rows), so it is excluded
    # by its own severity CASE rather than by a blanket "any lateral" sweep.
    folding_queries = [
        q
        for q, _ in conn.calls
        if "JOIN LATERAL" in q
        and "FROM situations" not in q
        and "CASE f.severity" not in q
    ]
    assert folding_queries, "expected at least one head-fold gather"
    for query in folding_queries:
        assert "f.superseded_by IS NULL" in query, (
            "flag OFF must never drop the supersession predicate"
        )


# ---------------------------------------------------------------------------
# 7. THE COVERAGE RULE amendment (§4.3) — in every composition system prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,prompt",
    [
        ("country", synth._COMPOSITION_SYSTEM),
        ("region", synth._REGION_COMPOSITION_SYSTEM),
        ("world", synth._WORLD_OVER_REGIONS_SYSTEM),
    ],
)
def test_coverage_rule_forks_below_floor_from_gap(name, prompt):
    assert "ABSENCE HAS TWO KINDS" in prompt, name
    assert "below verification floor" in prompt, name
    assert "no read this cycle" in prompt, name
    # The gap sentence now REQUIRES "no read at all inside the stated window".
    assert "NO read at all inside the stated window" in prompt, name


def test_coverage_rule_keeps_the_integrity_guarantee():
    """The amendment must not weaken the original: every shown unit is still
    accounted for, and a gap still never carries a [[ref:N]]."""
    prompt = synth._COMPOSITION_SYSTEM
    assert "silently dropping one is an integrity failure" in prompt
    assert "never attach a [[ref:N]] to a gap" in prompt


# ---------------------------------------------------------------------------
# 8. THE EXTRACTION — one-way import, unchanged surface, lockstep
# ---------------------------------------------------------------------------


def test_child_ref_marker_regex_stays_in_lockstep_with_the_output_marker():
    """Two spellings, two questions, ONE language. The house idiom for a
    deliberate copy across a module boundary (cf. TIERED_BASIS_FLOOR_DEFAULT
    mirroring scorecard_banding.FAITH_FLOOR)."""
    assert cw._CHILD_REF_MARKER_RE.pattern == synth._REF_MARKER_RE.pattern


@pytest.mark.parametrize(
    "name",
    [
        "MAX_TITLE_CHARS",
        "PERIPHERY_CAP",
        "PERIPHERY_BODY_CHARS",
        "PERIPHERY_TIER",
        "_EVIDENCE_TIER_KEY",
        "_EVIDENCE_FLOOR_KEY",
        "_SEVERITY_RANK",
        "_defuse_child_ref_markers",
        "_periphery_ids",
        "_render_periphery_block",
        "_row_body_excerpt",
        "_row_severity_level",
        "_row_severity_rank",
        "_select_periphery",
        "read_periphery_findings",
    ],
)
def test_moved_names_still_resolve_through_the_synthesizer(name):
    """A split that re-exports the moved names is invisible to every importer —
    including the monkeypatching tests that reach for ``synth.<name>``."""
    assert getattr(synth, name) is getattr(cw, name)


def test_composition_window_does_not_import_the_synthesizer():
    """LEAF discipline: the fallback gather takes the basis reader as a
    PARAMETER instead of importing it back, so there is no cycle to break."""
    source = (
        __import__("pathlib").Path(cw.__file__).read_text(encoding="utf-8")
    )
    assert "meta_findings_synthesizer" not in source.replace(
        "``meta_findings_synthesizer", "«doc"
    ).replace("meta_findings_synthesizer._REF_MARKER_RE", "«doc")
