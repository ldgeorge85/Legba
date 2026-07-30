# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-TIER — two-tier composition evidence (BASIS + PERIPHERY).

The operator's direction verbatim: "can we not include but properly weight or
separate it from others. Like even, conflicting points. Don't want to lose real
signal but want to distill it." Below-floor / unverified sub-claims stop being
either silently DROPPED (a hard floor) or silently BLENDED (the floor-0 gate):
they render as a capped, explicitly delimited PERIPHERY section requiring
hedged attribution, with conflicts against the verified BASIS surfaced as
"tensions worth watching".

Covered here:

  * flag + floor wiring — code default OFF byte-identical; ON ⇒ split floor
    (env floor when pinned, else the 0.50 scorecard lockstep) + a second
    periphery gather on the per-country, region, WORLD, and THEMATIC branches
    (the last two resolved the former SEAMS §44);
  * ``read_periphery_findings`` — the exact complement of the basis
    admissibility (LEFT join, below-floor-or-unverified predicate, coerce-tag
    drop, head-fold, NULL effective_confidence for unverified rows);
  * ``_select_periphery`` — worst-first (severity, recency) cap determinism;
  * prompt rendering — the delimited section, per-item status/score, ordinal
    continuation, hedge + conflict-surfacing instructions; EMPTY periphery ⇒
    byte-identical prompt;
  * ``_run`` end-to-end — periphery citations stamped ``tier='periphery'``,
    periphery ids appended to ``derived_from``, the ``data.evidence_tiers``
    envelope, and the honest empty-basis/periphery-present path (no LLM call);
  * verify treatment — a clause resting ONLY on periphery citations is
    SUPPORTED iff hedged/attributed; unhedged ⇒ the COUNTED
    ``unhedged_periphery_citation`` soft-fail; mixed basis+periphery clauses
    ride the basis leg; untiered citation lists are byte-identical.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic_handlers import scorecard_banding
from legba.data.provenance import verify


# ---------------------------------------------------------------------------
# helpers (mirroring test_meta_findings_composition.py conventions)
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Fake asyncpg.Connection that records every fetch() call's SQL+params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return [dict(r) for r in self._rows]


def _descriptor(others: list[tuple[str, str]]) -> SimpleNamespace:
    entries = [SimpleNamespace(id=i, time_window=w, data_types=[]) for i, w in others]
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=entries,
            targets=SimpleNamespace(predicate='has_tag("g20")'),
        )
    )


_UNITS = [("leadership_transition", "24h"), ("escalation", "24h")]


class _CannedLLM:
    """LLM double returning a caller-supplied payload; captures prompts."""

    subprovider = "tiered_test_double"

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
        raise AssertionError("LLM must not be called on the empty-basis path")


def _user_prompt_of(llm: _CannedLLM) -> str:
    """The rendered user prompt the double received (single user message)."""
    assert len(llm.calls) == 1
    messages = llm.calls[0]["messages"]
    # Message shape is provider-normalized by _reason_via_llm; find user content.
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "user":
            return str(content)
    raise AssertionError(f"no user message captured: {messages!r}")


def _row(
    *,
    analyst_id: str,
    uid: UUID | None = None,
    title: str = "sub-claim title",
    body: str = "sub-claim body",
    confidence: float = 0.7,
    effective_confidence: float | None = 0.7,
    faithfulness_score: float | None = 0.9,
    produced_at: str = "2026-06-30T00:00:00+00:00",
    tags: list[str] | None = None,
    periphery: bool = False,
    floor: float | None = None,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": title,
        "body": body,
        "confidence": confidence,
        "effective_confidence": effective_confidence,
        "faithfulness_score": faithfulness_score,
        "severity": None,
        "data": {"tags": tags or [], "evidence": []},
        "evidence": [],
        "target_id": "country_g20_in",
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": produced_at,
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }
    if periphery:
        r["_evidence_tier"] = synth.PERIPHERY_TIER
    if floor is not None:
        r["_evidence_floor"] = floor
    return r


def _comp_citation(
    ordinal: int,
    *,
    eff: float | None = None,
    derived: list[str] | None = None,
    source: str = "leadership_transition",
    tier: str | None = None,
) -> dict:
    c: dict[str, Any] = {
        "marker": f"[[ref:{ordinal}]]",
        "ordinal": ordinal,
        "ref_id": str(uuid4()),
        "ref_kind": "finding",
        "source": source,
        "title": "sub-claim",
        "evidence_text": "the unit found X",
        "derived_from": [str(x) for x in (derived or [])],
    }
    if eff is not None:
        c["effective_confidence"] = float(eff)
    if tier is not None:
        c["tier"] = tier
    return c


# ---------------------------------------------------------------------------
# 1. Floor lockstep + flag/floor resolution
# ---------------------------------------------------------------------------


def test_tiered_basis_floor_lockstep_with_scorecard():
    """The split's default basis bar IS the system-wide verification floor —
    the scorecard's FAITH_FLOOR (the 0.50 decision), test-enforced lockstep."""
    assert synth.TIERED_BASIS_FLOOR_DEFAULT == scorecard_banding.FAITH_FLOOR


def test_split_floor_defaults_to_scorecard_floor(monkeypatch):
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    assert synth._resolve_split_floor(None) == synth.TIERED_BASIS_FLOOR_DEFAULT
    # The OFF-path resolver is untouched: env unset still means 0.0 there.
    assert synth._resolve_verify_floor(None) == synth.DEFAULT_VERIFY_FLOOR


def test_split_floor_env_pin_wins_and_clamps(monkeypatch):
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "0.3")
    assert synth._resolve_split_floor(None) == pytest.approx(0.3)
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "5.0")
    assert synth._resolve_split_floor(None) == 1.0
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "not-a-number")
    assert synth._resolve_split_floor(None) == synth.TIERED_BASIS_FLOOR_DEFAULT


def test_tiered_evidence_flag_default_off(monkeypatch):
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    assert synth._tiered_evidence_enabled() is False
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "0")
    assert synth._tiered_evidence_enabled() is False
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    assert synth._tiered_evidence_enabled() is True
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "true")
    assert synth._tiered_evidence_enabled() is True


# ---------------------------------------------------------------------------
# 2. READ_SLICE wiring — flag OFF byte-identical; flag ON = basis + periphery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_per_country_read_is_byte_identical(monkeypatch):
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in")
    # Exactly ONE gather (the legacy verify-floored basis read), floor 0.0.
    assert len(conn.calls) == 1
    query, params = conn.calls[0]
    assert "JOIN LATERAL" in query and "LEFT JOIN LATERAL" not in query
    assert "LEAST(f.confidence, v.faithfulness_score) >= $4" in query
    assert params[3] == synth.DEFAULT_VERIFY_FLOOR


@pytest.mark.asyncio
async def test_flag_on_per_country_read_splits_basis_and_periphery(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    basis_row = _row(analyst_id="leadership_transition")
    conn = _CapturingConn(rows=[basis_row])
    rows = await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_in"
    )
    # TWO gathers: the basis read at the SPLIT floor (0.50), then the periphery
    # complement at the SAME floor.
    assert len(conn.calls) == 2
    basis_q, basis_p = conn.calls[0]
    peri_q, peri_p = conn.calls[1]
    assert "LEAST(f.confidence, v.faithfulness_score) >= $4" in basis_q
    assert basis_p[3] == synth.TIERED_BASIS_FLOOR_DEFAULT
    assert "LEFT JOIN LATERAL" in peri_q
    assert "v.faithfulness_score IS NULL" in peri_q
    assert "LEAST(f.confidence, v.faithfulness_score) < $4" in peri_q
    # Same scope: analyst set, window, target; same floor value.
    assert peri_p[0] == basis_p[0]
    assert peri_p[1] == basis_p[1]
    assert peri_p[2] == basis_p[2] == "country_g20_in"
    assert peri_p[3] == basis_p[3]
    # Coerce-fallback garbage stays excluded outright; head-fold holds; the
    # per-country periphery keeps the meta exclusion (units are first-order).
    assert "?| array['unstructured','coerce_failed']" in peri_q
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in peri_q
    assert "IS DISTINCT FROM 'true'" in peri_q
    # Unverified rows must carry a NULL effective_confidence (explicit CASE —
    # SQL LEAST would otherwise launder raw confidence into a verified score).
    assert "CASE WHEN v.faithfulness_score IS NULL THEN NULL" in peri_q
    # Row marking: every returned row is tiered; basis rows carry the floor,
    # periphery rows carry tier + floor.
    basis_rows = [r for r in rows if r.get("_evidence_tier") != synth.PERIPHERY_TIER]
    peri_rows = [r for r in rows if r.get("_evidence_tier") == synth.PERIPHERY_TIER]
    assert basis_rows and peri_rows
    assert all(r["_evidence_floor"] == synth.TIERED_BASIS_FLOOR_DEFAULT for r in rows)


@pytest.mark.asyncio
async def test_flag_on_env_floor_pin_drives_both_legs(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.setenv(synth.VERIFY_FLOOR_ENV, "0.3")
    conn = _CapturingConn(rows=[])
    await synth.READ_SLICE(
        conn, descriptor=_descriptor(_UNITS), target_filter="country_g20_br"
    )
    (_, basis_p), (_, peri_p) = conn.calls
    assert basis_p[3] == pytest.approx(0.3)
    assert peri_p[3] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_flag_on_region_read_splits_with_member_scope(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)

    class _RegionConn(_CapturingConn):
        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            self.calls.append((query, params))
            if "target_descriptors" in query:
                return [{"descriptor_id": "country_g20_sa"}, {"descriptor_id": "country_watch_ir"}]
            return []

    conn = _RegionConn()
    await synth.READ_SLICE(
        conn,
        descriptor=_descriptor([("country_composition", "24h")]),
        target_filter="region_mena",
    )
    # member-resolve + basis + periphery.
    assert len(conn.calls) == 3
    peri_q, peri_p = conn.calls[2]
    assert "LEFT JOIN LATERAL" in peri_q
    assert peri_p[2] == ["country_g20_sa", "country_watch_ir"]
    assert peri_p[3] == synth.TIERED_BASIS_FLOOR_DEFAULT
    # Region periphery reads country_composition heads, which ARE meta=True —
    # the meta-exclusion clause must be ABSENT (include_meta=True).
    assert "IS DISTINCT FROM 'true'" not in peri_q


@pytest.mark.asyncio
async def test_flag_off_region_read_is_byte_identical(monkeypatch):
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)

    class _RegionConn(_CapturingConn):
        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            self.calls.append((query, params))
            if "target_descriptors" in query:
                return [{"descriptor_id": "country_g20_sa"}]
            return []

    conn = _RegionConn()
    await synth.READ_SLICE(
        conn,
        descriptor=_descriptor([("country_composition", "24h")]),
        target_filter="region_mena",
    )
    # member-resolve + the ONE legacy basis read at floor 0.0 — no periphery.
    assert len(conn.calls) == 2
    _, basis_p = conn.calls[1]
    assert basis_p[3] == synth.DEFAULT_VERIFY_FLOOR


@pytest.mark.asyncio
async def test_periphery_reader_refuses_empty_analyst_set():
    conn = _CapturingConn(rows=[{"id": "should-not-appear"}])
    rows = await synth.read_periphery_findings(
        conn, analyst_ids=[], time_window_hours=24, floor=0.5
    )
    assert rows == []
    assert conn.calls == []


# ---------------------------------------------------------------------------
# 3. _select_periphery — worst-first cap determinism
# ---------------------------------------------------------------------------


def _peri_row(sev: str | None, at: str, uid: UUID | None = None) -> dict[str, Any]:
    tags = [f"severity:{sev}"] if sev else []
    return _row(
        analyst_id="escalation",
        uid=uid,
        tags=tags,
        produced_at=at,
        periphery=True,
        floor=0.5,
    )


def test_select_periphery_worst_first_and_capped():
    rows = [
        _peri_row("low", "2026-06-30T12:00:00+00:00"),
        _peri_row("critical", "2026-06-28T00:00:00+00:00"),
        _peri_row("high", "2026-06-29T00:00:00+00:00"),
        _peri_row(None, "2026-06-30T18:00:00+00:00"),  # unranked sorts last
        _peri_row("high", "2026-06-30T00:00:00+00:00"),  # newer high wins the tie
    ]
    sel = synth._select_periphery(rows, cap=3)
    assert [synth._row_severity_level(r) for r in sel] == ["critical", "high", "high"]
    # Within equal severity, newer first.
    assert sel[1]["produced_at"] == "2026-06-30T00:00:00+00:00"
    # Deterministic under input order permutation.
    assert synth._select_periphery(list(reversed(rows)), cap=3) == sel


def test_select_periphery_default_cap_is_periphery_cap():
    rows = [
        _peri_row("high", f"2026-06-{10 + i:02d}T00:00:00+00:00") for i in range(12)
    ]
    assert len(synth._select_periphery(rows)) == synth.PERIPHERY_CAP


# ---------------------------------------------------------------------------
# 4. Prompt rendering — the delimited PERIPHERY section
# ---------------------------------------------------------------------------


def test_render_periphery_block_golden():
    below = _row(
        analyst_id="escalation",
        title="Unverified troop movement report",
        body="Convoy sightings near the border.",
        effective_confidence=0.31,
        faithfulness_score=0.31,
        tags=["severity:high"],
        periphery=True,
        floor=0.5,
    )
    unverified = _row(
        analyst_id="narrative_coordination",
        title="Ungraded narrative spike",
        effective_confidence=None,
        faithfulness_score=None,
        periphery=True,
        floor=0.5,
    )
    block = synth._render_periphery_block([below, unverified], start_ordinal=8, floor=0.5)
    # The explicit delimiter + honesty framing.
    assert "WEAKLY-SUPPORTED / UNVERIFIED SIGNALS" in block
    assert "below the verification floor 0.50" in block
    assert "MUST NOT be cited as established fact" in block
    # The hedge requirement + the operator's conflict-surfacing ask.
    assert "MUST be attributed and hedged" in block
    assert "weakly-supported reporting suggests" in block
    assert "Tensions worth watching" in block
    assert "never drop it and never blend it in" in block
    # Ordinals CONTINUE the basis numbering; each item carries its own score.
    assert "[[ref:8]] Unverified troop movement report" in block
    assert "[[ref:9]] Ungraded narrative spike" in block
    assert "status=below_floor effective_confidence=0.31 severity=high" in block
    assert "status=unverified" in block
    # Empty set ⇒ no section at all (the byte-identity precondition).
    assert synth._render_periphery_block([], start_ordinal=8, floor=0.5) == ""


# ---------------------------------------------------------------------------
# 5. _run end-to-end — prompt section, citations, lineage, envelope
# ---------------------------------------------------------------------------


def _canned_payload(body: str) -> dict[str, Any]:
    return {
        "title": "Composed read",
        "body": body,
        "confidence": 0.6,
        "evidence": [],
        "tags": [],
    }


@pytest.mark.asyncio
async def test_run_renders_periphery_and_stamps_citation_tier():
    basis_uid, peri_uid = uuid4(), uuid4()
    basis = _row(analyst_id="leadership_transition", uid=basis_uid, floor=0.5)
    peri = _row(
        analyst_id="escalation",
        uid=peri_uid,
        title="Weak convoy report",
        effective_confidence=0.31,
        faithfulness_score=0.31,
        tags=["severity:high"],
        periphery=True,
        floor=0.5,
    )
    llm = _CannedLLM(
        _canned_payload(
            "Leadership is contested [[ref:1]]. Weakly-supported reporting "
            "suggests a convoy moved [[ref:2]]."
        )
    )
    result = await synth._run(
        [basis, peri],
        {"target_id": "country_g20_in", "analyst_id": "country_composition"},
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )
    prompt = _user_prompt_of(llm)
    # The basis renders as today; the periphery section follows it, delimited,
    # with the ordinal continuing the basis numbering.
    assert "First-order findings to synthesize: 1." in prompt
    assert "WEAKLY-SUPPORTED / UNVERIFIED SIGNALS" in prompt
    assert prompt.index("[[ref:1]]") < prompt.index("WEAKLY-SUPPORTED")
    assert "[[ref:2]] Weak convoy report" in prompt

    finding = result.finding
    cites = finding.data["citations"]
    by_ord = {c["ordinal"]: c for c in cites}
    assert set(by_ord) == {1, 2}
    # The periphery citation carries its tier (the verify hedge-rule key) and
    # its honest below-floor score; the basis citation is byte-identical.
    assert by_ord[2]["tier"] == synth.PERIPHERY_TIER
    assert by_ord[2]["effective_confidence"] == pytest.approx(0.31)
    assert "tier" not in by_ord[1]
    # Lineage: basis ids first, then the kept periphery ids (ordinal order).
    assert result.derived_from == [basis_uid, peri_uid]
    # Envelope honesty: built on 1 verified + 1 weak signal at floor 0.5.
    tiers = finding.data["evidence_tiers"]
    assert tiers == {
        "basis_count": 1,
        "periphery_count": 1,
        "periphery_ids": [str(peri_uid)],
        "floor": 0.5,
    }


@pytest.mark.asyncio
async def test_run_empty_periphery_prompt_is_byte_identical():
    """Tiered mode with ZERO periphery renders the SAME prompt bytes as the
    untiered run — the section only exists when periphery does. The envelope
    still records the honest zero-periphery stamp."""
    uid = uuid4()

    def _basis(floor: float | None) -> dict[str, Any]:
        return _row(analyst_id="leadership_transition", uid=uid, floor=floor)

    options = {"target_id": "country_g20_in", "analyst_id": "country_composition"}
    payload = _canned_payload("Leadership is contested [[ref:1]].")

    llm_plain = _CannedLLM(payload)
    res_plain = await synth._run(
        [_basis(None)], options, llm=llm_plain, max_tokens=512,
        temperature=0.2, system_prompt="unused",
    )
    llm_tiered = _CannedLLM(payload)
    res_tiered = await synth._run(
        [_basis(0.5)], options, llm=llm_tiered, max_tokens=512,
        temperature=0.2, system_prompt="unused",
    )
    assert _user_prompt_of(llm_plain) == _user_prompt_of(llm_tiered)
    assert "evidence_tiers" not in res_plain.finding.data
    assert res_tiered.finding.data["evidence_tiers"] == {
        "basis_count": 1,
        "periphery_count": 0,
        "periphery_ids": [],
        "floor": 0.5,
    }
    assert res_plain.derived_from == res_tiered.derived_from == [uid]


@pytest.mark.asyncio
async def test_run_empty_basis_with_periphery_short_circuits_honestly():
    """Zero verified basis + weak signals present ⇒ NO synthesis (a composition
    is never built from weak signals alone) but the signal is RECORDED, never
    lost: the empty-slice head names the periphery ids + floor."""
    peri_uid = uuid4()
    peri = _row(
        analyst_id="escalation",
        uid=peri_uid,
        effective_confidence=0.2,
        faithfulness_score=0.2,
        periphery=True,
        floor=0.5,
    )
    result = await synth._run(
        [peri],
        {"target_id": "country_g20_in", "analyst_id": "country_composition"},
        llm=_NeverCalledLLM(),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    finding = result.finding
    assert "empty_slice" in finding.tags
    assert finding.confidence == 0.0
    assert finding.data["evidence_tiers"] == {
        "basis_count": 0,
        "periphery_count": 1,
        "periphery_ids": [str(peri_uid)],
        "floor": 0.5,
    }
    assert "below-floor/unverified signal(s) were present" in finding.body
    assert "never synthesized from weak signals alone" in finding.body


# ---------------------------------------------------------------------------
# 6. Verify treatment — hedge-required periphery citations
# ---------------------------------------------------------------------------


def test_unhedged_periphery_only_claim_is_counted_defect():
    body = "Militia convoys moved toward the border crossing [[ref:2]]."
    citations = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"], source="escalation", tier="periphery"),
    ]
    rep = verify._deterministic_floor_subclaim(body, citations)
    assert rep.checkable_claims == 1
    assert rep.supported_claims == 0
    assert rep.faithfulness_score == 0.0
    assert [s.reason for s in rep.unsupported_spans] == ["unhedged_periphery_citation"]
    assert rep.unsupported_spans[0].markers == [2]
    failed = [v for v in rep.claim_verdicts if v.verdict != "supported"]
    assert len(failed) == 1 and failed[0].reason == "unhedged_periphery_citation"


def test_hedged_periphery_claim_is_supported():
    body = (
        "Weakly-supported reporting suggests militia convoys moved toward "
        "the border crossing [[ref:2]]."
    )
    citations = [
        _comp_citation(2, eff=0.3, derived=["s2"], source="escalation", tier="periphery"),
    ]
    rep = verify._deterministic_floor_subclaim(body, citations, 0.6)
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == 1.0
    reasons = [s.reason for s in rep.unsupported_spans]
    assert "unhedged_periphery_citation" not in reasons
    # The numeric hedge-laundering comparison is SKIPPED for a periphery-only
    # clause — the hedged text IS the remedy (0.6 > 0.3 must NOT flag).
    assert "hedge_laundering" not in reasons
    # But the evidence ceiling still caps a periphery-only composition at its
    # weak evidence (0.3) — hedged prose never inflates the number.
    assert rep.confidence_ceiling == pytest.approx(0.3)


def test_mixed_basis_and_periphery_claim_rides_the_basis_leg():
    body = "Leadership is contested and convoys reportedly moved [[ref:1]][[ref:2]]."
    citations = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"], source="escalation", tier="periphery"),
    ]
    rep = verify._deterministic_floor_subclaim(body, citations)
    assert rep.supported_claims == 1
    assert not [
        s for s in rep.unsupported_spans if s.reason == "unhedged_periphery_citation"
    ]


def test_untiered_citations_are_byte_identical():
    """The SAME unhedged body over the SAME citations WITHOUT tier stamps (every
    pre-C-TIER composition) stays supported — the rule is inert."""
    body = "Militia convoys moved toward the border crossing [[ref:2]]."
    citations = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"], source="escalation"),
    ]
    rep = verify._deterministic_floor_subclaim(body, citations)
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == 1.0
    assert not [
        s for s in rep.unsupported_spans if s.reason == "unhedged_periphery_citation"
    ]


def test_periphery_reason_maps_soft_fail():
    assert (
        verify.fail_class_for_reason("unhedged_periphery_citation")
        == verify.FAIL_CLASS_SOFT
    )


# ---------------------------------------------------------------------------
# 6b. Tier-aware LLM-judge rubric (former SEAMS §45)
# ---------------------------------------------------------------------------


def test_judge_periphery_rubric_empty_for_untiered_citations():
    """Untiered citation lists (every pre-C-TIER composition, every unit) yield
    the EMPTY string — the judge prompt stays byte-identical."""
    untiered = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"]),
    ]
    assert verify._judge_periphery_rubric(untiered) == ""
    assert verify._judge_periphery_rubric([]) == ""
    assert verify._judge_periphery_rubric(None) == ""
    # Unit-convention citations (no tier key) are inert too.
    assert verify._judge_periphery_rubric(
        [{"marker": "[2]", "signal_id": "sig-2"}]
    ) == ""


def test_judge_periphery_rubric_names_periphery_ordinals():
    citations = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"], tier="periphery"),
        _comp_citation(3, eff=0.2, derived=["s3"], tier="periphery"),
    ]
    rubric = verify._judge_periphery_rubric(citations)
    assert "EVIDENCE TIERS" in rubric
    assert "[2, 3]" in rubric
    assert "PERIPHERY tier" in rubric
    assert "hedged AND attributed" in rubric
    assert "ESTABLISHED FACT" in rubric
    # Non-empty rubric ends with the block separator so the evidence map
    # renders on its own line.
    assert rubric.endswith("\n\n")


class _JudgePromptCapture:
    """Judge LLM double: captures every prompt, answers all-supported."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        prompt = str(messages[0]["content"])
        self.prompts.append(prompt)
        # One verdict per numbered claim (count the numbered CLAIMS lines).
        n = sum(
            1
            for line in prompt.splitlines()
            if line[:1].isdigit() and ". " in line[:6]
        )
        return SimpleNamespace(content=json.dumps({"verdicts": ["supported"] * n}))


@pytest.mark.asyncio
async def test_run_judge_prompt_carries_tier_rubric_only_when_tiered():
    body = (
        "Leadership is contested [[ref:1]]. Weakly-supported reporting "
        "suggests a convoy moved [[ref:2]]."
    )
    tiered = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"], tier="periphery"),
    ]
    untiered = [
        _comp_citation(1, eff=0.8, derived=["s1"]),
        _comp_citation(2, eff=0.3, derived=["s2"]),
    ]
    judge_tiered = _JudgePromptCapture()
    await verify._run_judge(judge_tiered, body=body, citations=tiered)
    assert judge_tiered.prompts, "judge was never called"
    assert all("EVIDENCE TIERS" in p for p in judge_tiered.prompts if "N -> sub-claim" in p)
    assert any("EVIDENCE TIERS" in p for p in judge_tiered.prompts)
    assert any("[2]" in p and "PERIPHERY tier" in p for p in judge_tiered.prompts)

    judge_untiered = _JudgePromptCapture()
    await verify._run_judge(judge_untiered, body=body, citations=untiered)
    assert judge_untiered.prompts
    assert all("EVIDENCE TIERS" not in p for p in judge_untiered.prompts)


# ---------------------------------------------------------------------------
# 7. READ_SLICE wiring — WORLD + THEMATIC branches (former SEAMS §44)
# ---------------------------------------------------------------------------


class _DispatchConn:
    """Fake conn that dispatches on query shape: roster / periphery / basis."""

    def __init__(
        self,
        basis_rows: list[dict[str, Any]] | None = None,
        roster_rows: list[dict[str, Any]] | None = None,
        periphery_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._basis = basis_rows or []
        self._roster = roster_rows or []
        self._periphery = periphery_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "target_descriptors" in query:
            return [dict(r) for r in self._roster]
        if "LEFT JOIN LATERAL" in query:
            return [dict(r) for r in self._periphery]
        return [dict(r) for r in self._basis]


def _thematic_descriptor(desks: list[str] | None = None) -> SimpleNamespace:
    substrate: dict[str, Any] = {synth.THEMATIC_DIMENSION_KEY: "escalation"}
    if desks is not None:
        substrate[synth.THEMATIC_DESKS_KEY] = desks
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(id="escalation", time_window="24h", data_types=[])
            ],
            substrate=substrate,
        ),
        method=SimpleNamespace(
            llm={"verify": {"factory_kind": "stack_ref", "raw": "llm.x"}}
        ),
    )


def _world_descriptor() -> SimpleNamespace:
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(
                    id="region_composition", time_window="24h", data_types=[]
                )
            ],
            substrate={},
        ),
        method=SimpleNamespace(
            llm={"verify": {"factory_kind": "stack_ref", "raw": "llm.x"}}
        ),
    )


@pytest.mark.asyncio
async def test_flag_on_thematic_read_splits_basis_and_periphery(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    basis = _row(analyst_id="escalation")
    basis["derived_from"] = []
    peri = _row(analyst_id="escalation", periphery=True, floor=0.5)
    conn = _DispatchConn(
        basis_rows=[basis],
        roster_rows=[{"descriptor_id": "country_g20_in", "name": "India"}],
        periphery_rows=[peri],
    )
    rows = await synth.READ_SLICE(
        conn, descriptor=_thematic_descriptor(), target_filter=None
    )
    # THREE fetches: the basis unit read at the SPLIT floor, the desk roster,
    # then the periphery complement at the SAME floor.
    basis_calls = [
        (q, p) for q, p in conn.calls
        if "JOIN LATERAL" in q and "LEFT JOIN LATERAL" not in q
    ]
    peri_calls = [(q, p) for q, p in conn.calls if "LEFT JOIN LATERAL" in q]
    assert len(basis_calls) == 1 and len(peri_calls) == 1
    basis_q, basis_p = basis_calls[0]
    peri_q, peri_p = peri_calls[0]
    # Thematic basis: no target scope (all desks) → floor is $3; head-folded;
    # meta stays EXCLUDED (the unit is first-order).
    assert basis_p[2] == synth.TIERED_BASIS_FLOOR_DEFAULT
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in basis_q
    assert "IS DISTINCT FROM 'true'" in basis_q
    # Periphery complement: same analyst set + window + floor, meta excluded.
    assert peri_p[0] == basis_p[0]
    assert peri_p[1] == basis_p[1]
    assert peri_p[2] == basis_p[2]
    assert "v.faithfulness_score IS NULL" in peri_q
    assert "IS DISTINCT FROM 'true'" in peri_q
    # Row marking: basis rows carry the floor; periphery rows carry tier+floor.
    basis_rows = [r for r in rows if r.get("_evidence_tier") != synth.PERIPHERY_TIER]
    peri_rows = [r for r in rows if r.get("_evidence_tier") == synth.PERIPHERY_TIER]
    assert basis_rows and peri_rows
    assert all(
        r["_evidence_floor"] == synth.TIERED_BASIS_FLOOR_DEFAULT for r in rows
    )


@pytest.mark.asyncio
async def test_flag_on_thematic_dyad_scopes_periphery_to_desk_allowlist(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    conn = _DispatchConn()
    await synth.READ_SLICE(
        conn,
        descriptor=_thematic_descriptor(
            desks=["country_watch_ir", "country_watch_il"]
        ),
        target_filter=None,
    )
    peri_calls = [(q, p) for q, p in conn.calls if "LEFT JOIN LATERAL" in q]
    assert len(peri_calls) == 1
    peri_q, peri_p = peri_calls[0]
    # Dyad allow-list scopes the periphery too: target-id SET at $3, floor $4.
    assert "f.target_id = ANY($3::TEXT[])" in peri_q
    assert peri_p[2] == ["country_watch_ir", "country_watch_il"]
    assert peri_p[3] == synth.TIERED_BASIS_FLOOR_DEFAULT


@pytest.mark.asyncio
async def test_flag_off_thematic_read_is_byte_identical(monkeypatch):
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    basis = _row(analyst_id="escalation")
    basis["derived_from"] = []
    conn = _DispatchConn(
        basis_rows=[basis],
        roster_rows=[{"descriptor_id": "country_g20_in", "name": "India"}],
    )
    rows = await synth.READ_SLICE(
        conn, descriptor=_thematic_descriptor(), target_filter=None
    )
    # Exactly the legacy pair: basis read (floor 0.0) + desk roster; NO periphery.
    assert not [q for q, _ in conn.calls if "LEFT JOIN LATERAL" in q]
    basis_q, basis_p = conn.calls[0]
    assert basis_p[2] == synth.DEFAULT_VERIFY_FLOOR
    assert all("_evidence_floor" not in r for r in rows)


@pytest.mark.asyncio
async def test_flag_on_world_read_splits_basis_and_periphery(monkeypatch):
    monkeypatch.setenv(synth.TIERED_EVIDENCE_ENV, "1")
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    basis = _row(analyst_id="region_composition")
    basis["target_id"] = "region_mena"
    basis["derived_from"] = []
    peri = _row(analyst_id="region_composition", periphery=True, floor=0.5)
    peri["target_id"] = "region_apac"
    conn = _DispatchConn(basis_rows=[basis], periphery_rows=[peri])
    rows = await synth.READ_SLICE(
        conn, descriptor=_world_descriptor(), target_filter=None
    )
    basis_calls = [
        (q, p) for q, p in conn.calls
        if "JOIN LATERAL" in q and "LEFT JOIN LATERAL" not in q
    ]
    peri_calls = [(q, p) for q, p in conn.calls if "LEFT JOIN LATERAL" in q]
    assert len(basis_calls) == 1 and len(peri_calls) == 1
    basis_q, basis_p = basis_calls[0]
    peri_q, peri_p = peri_calls[0]
    # World basis: SPLIT floor at $3, meta-INCLUSIVE (region heads are meta=True).
    assert basis_p[2] == synth.TIERED_BASIS_FLOOR_DEFAULT
    assert "IS DISTINCT FROM 'true'" not in basis_q
    # Periphery complement: same roster + window + floor, meta-inclusive too.
    assert peri_p[0] == basis_p[0]
    assert peri_p[1] == basis_p[1]
    assert peri_p[2] == basis_p[2]
    assert "IS DISTINCT FROM 'true'" not in peri_q
    assert "v.faithfulness_score IS NULL" in peri_q
    basis_rows = [r for r in rows if r.get("_evidence_tier") != synth.PERIPHERY_TIER]
    peri_rows = [r for r in rows if r.get("_evidence_tier") == synth.PERIPHERY_TIER]
    assert basis_rows and peri_rows
    assert all(
        r["_evidence_floor"] == synth.TIERED_BASIS_FLOOR_DEFAULT for r in rows
    )


@pytest.mark.asyncio
async def test_flag_off_world_read_is_byte_identical(monkeypatch):
    monkeypatch.delenv(synth.TIERED_EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(synth.VERIFY_FLOOR_ENV, raising=False)
    conn = _DispatchConn()
    await synth.READ_SLICE(
        conn, descriptor=_world_descriptor(), target_filter=None
    )
    # Legacy pair only: region roster + the ONE floor-0.0 basis read.
    assert not [q for q, _ in conn.calls if "LEFT JOIN LATERAL" in q]
    basis_calls = [
        (q, p) for q, p in conn.calls
        if "JOIN LATERAL" in q and "LEFT JOIN LATERAL" not in q
    ]
    assert len(basis_calls) == 1
    _, basis_p = basis_calls[0]
    assert basis_p[2] == synth.DEFAULT_VERIFY_FLOOR


@pytest.mark.asyncio
async def test_run_thematic_composition_gets_full_periphery_treatment():
    """The data-driven ``_run`` gives a THEMATIC slice with marked periphery the
    identical rendering/citation/envelope treatment the country path gets."""
    basis_uid, peri_uid = uuid4(), uuid4()
    basis = _row(analyst_id="escalation", uid=basis_uid, floor=0.5)
    peri = _row(
        analyst_id="escalation",
        uid=peri_uid,
        title="Ungraded escalation read",
        effective_confidence=None,
        faithfulness_score=None,
        periphery=True,
        floor=0.5,
    )
    llm = _CannedLLM(
        _canned_payload(
            "Escalation is contained [[ref:1]]. Weakly-supported reporting "
            "suggests a new front [[ref:2]]."
        )
    )
    result = await synth._run(
        [basis, peri],
        {"thematic_dimension": "escalation", "analyst_id": "escalation_composition"},
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )
    prompt = _user_prompt_of(llm)
    assert "WEAKLY-SUPPORTED / UNVERIFIED SIGNALS" in prompt
    assert "[[ref:2]] Ungraded escalation read" in prompt
    cites = {c["ordinal"]: c for c in result.finding.data["citations"]}
    assert cites[2]["tier"] == synth.PERIPHERY_TIER
    assert "tier" not in cites[1]
    assert result.finding.data["evidence_tiers"] == {
        "basis_count": 1,
        "periphery_count": 1,
        "periphery_ids": [str(peri_uid)],
        "floor": 0.5,
    }
    assert result.derived_from == [basis_uid, peri_uid]


@pytest.mark.asyncio
async def test_run_world_composition_gets_full_periphery_treatment():
    basis_uid, peri_uid = uuid4(), uuid4()
    basis = _row(analyst_id="region_composition", uid=basis_uid, floor=0.5)
    basis["target_id"] = "region_mena"
    peri = _row(
        analyst_id="region_composition",
        uid=peri_uid,
        title="Below-floor region read",
        effective_confidence=0.3,
        faithfulness_score=0.3,
        periphery=True,
        floor=0.5,
    )
    peri["target_id"] = "region_apac"
    llm = _CannedLLM(
        _canned_payload(
            "MENA holds steady [[ref:1]]. Weakly-supported reporting suggests "
            "APAC tension [[ref:2]]."
        )
    )
    result = await synth._run(
        [basis, peri],
        {"composition": True, "analyst_id": "world_assessor"},
        llm=llm,
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused-global",
    )
    prompt = _user_prompt_of(llm)
    assert "WEAKLY-SUPPORTED / UNVERIFIED SIGNALS" in prompt
    cites = {c["ordinal"]: c for c in result.finding.data["citations"]}
    assert cites[2]["tier"] == synth.PERIPHERY_TIER
    tiers = result.finding.data["evidence_tiers"]
    assert tiers["basis_count"] == 1 and tiers["periphery_count"] == 1
    assert tiers["floor"] == 0.5
    assert result.derived_from == [basis_uid, peri_uid]
