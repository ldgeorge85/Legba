# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P0-T2 — the actor-side verify seam: verify_inline_target_finding persists a
faithfulness critique whose data carries the gate keys.

DETERMINISTIC: the LLM judge is OFF (env unset) so no live call. We capture the
critique INSERT via a fake conn and assert the row's JSONB ``data`` carries
``overall_score`` (the gate JOIN key) + ``analyzed_output_id`` + the verification
detail. We also assert the SCOPE GUARD: a non-inline_target kind is a no-op.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from legba.data.provenance.models import FindingPayload
from legba.runtime.actor_critic import verify_inline_target_finding


class _FakeConn:
    """Records every conn.execute(sql, *args) — the critique INSERT lands here."""

    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, *a, **k):  # pragma: no cover — not hit on happy path
        return None


def _deps(kind: str, *, verify_judge=None, analyst_id="country_assessor"):
    """Minimal _AnalystDeps-shaped stub: the helper only reads identity + judge."""
    descriptor = SimpleNamespace(
        identity=SimpleNamespace(kind=kind, id=analyst_id, version="1.2.3"),
        method=SimpleNamespace(llm={"primary": {"raw": "llm.primary.openai_compat"}}),
    )
    return SimpleNamespace(descriptor=descriptor, verify_judge=verify_judge)


def _insert_data_dict(conn: _FakeConn) -> dict:
    """Pull the JSONB ``data`` arg out of the recorded analyst_outputs INSERT."""
    for sql, args in conn.executes:
        if "INSERT INTO analyst_outputs" in sql:
            # _insert_analyst_output positional args: (..., data is arg[6] as json str)
            for a in args:
                if isinstance(a, str) and a.lstrip().startswith("{"):
                    try:
                        parsed = json.loads(a)
                    except json.JSONDecodeError:
                        continue
                    if "overall_score" in parsed:
                        return parsed
    raise AssertionError("no critique INSERT with overall_score recorded")


async def test_verify_seam_persists_critique_with_gate_keys():
    """A cited inline_target finding with one PLANTED uncited claim → a critique
    row whose data carries overall_score < 1, analyzed_output_id = the finding id,
    and the verification detail naming the unsupported span."""
    conn = _FakeConn()
    fid = uuid4()
    sid = str(uuid4())
    finding = FindingPayload(
        title="Brazil energy",
        body=(
            "## Key developments\n"
            "- Itaipu hydro upgrade completed [1].\n"
            "- A coup attempt was reported overnight.\n"  # PLANTED: no citation
        ),
        confidence=0.85,
        data={"citations": [{"marker": "[1]", "signal_id": sid}]},
    )
    deps = _deps("inline_target")

    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=fid, finding_payload=finding, run_id=uuid4(),
    )

    # The report came back (judge OFF → deterministic floor).
    assert result is not None
    assert result["checkable_claims"] == 2
    assert result["supported_claims"] == 1
    assert result["judge_status"] == "deterministic"

    data = _insert_data_dict(conn)
    # Gate JOIN keys live at the TOP of the critique's JSONB data.
    assert data["overall_score"] == pytest.approx(0.5)
    assert data["analyzed_output_id"] == str(fid)
    assert data["kind_marker"] == "critique"
    # The verification detail (data->'data'->'verification') names the span.
    verification = data["data"]["verification"]
    assert verification["faithfulness_score"] == pytest.approx(0.5)
    spans = verification["unsupported_spans"]
    assert any(s["reason"] == "no_citation" for s in spans)


async def test_verify_seam_scope_guard_non_inline_target_noop():
    """SCOPE: a non-inline_target kind is a no-op — no critique written, returns
    None (P0 does NOT widen verify to all analysts)."""
    conn = _FakeConn()
    finding = FindingPayload(title="t", body="x [1].", confidence=0.9, data={})
    deps = _deps("critic")  # not inline_target

    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=uuid4(), finding_payload=finding, run_id=uuid4(),
    )
    assert result is None
    assert conn.executes == []  # nothing persisted


async def test_verify_seam_all_resolved_high_score():
    """A fully-cited finding → faithfulness 1.0, overall_score 1.0 → the gate
    leaves effective_confidence == confidence (no demotion)."""
    conn = _FakeConn()
    fid = uuid4()
    sid = str(uuid4())
    finding = FindingPayload(
        title="t",
        body="## Key developments\n- The plant reopened this week [1].\n",
        confidence=0.8,
        data={"citations": [{"marker": "[1]", "signal_id": sid}]},
    )
    deps = _deps("inline_target")
    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=fid, finding_payload=finding, run_id=uuid4(),
    )
    assert result["faithfulness_score"] == 1.0
    data = _insert_data_dict(conn)
    assert data["overall_score"] == 1.0


# ---------------------------------------------------------------------------
# P3-T3 / P3-T7 — the COMPOSITION seam: the SAME helper verifies a target-scoped
# meta_findings_synthesizer finding via its [[ref:N]] sub-claim bridge and
# caps confidence via the T7 evidence ceiling.
# ---------------------------------------------------------------------------


def _comp_citation(ordinal, *, ref_id=None, eff=None, derived=None, source="leadership_transition"):
    c = {
        "marker": f"[[ref:{ordinal}]]",
        "ordinal": ordinal,
        "ref_id": ref_id or str(uuid4()),
        "ref_kind": "finding",
        "source": source,
        "title": "sub-claim",
        "evidence_text": "the unit found the leadership is contested",
        "derived_from": [str(x) for x in (derived or [])],
    }
    if eff is not None:
        c["effective_confidence"] = float(eff)
    return c


async def test_verify_seam_composition_hedge_launder_caps_overall_score():
    """A COMPOSITION finding (kind meta_findings_synthesizer) asserting confidence
    0.9 over a cited sub-claim with effective_confidence 0.5 → a critique whose
    overall_score is capped at 0.5 (the T7 ceiling) with analyzed_output_id = the
    composition finding id. The hedge_laundering span is surfaced."""
    conn = _FakeConn()
    fid = uuid4()
    finding = FindingPayload(
        title="India composition",
        body="The country is on the brink of collapse [[ref:1]].\n",
        confidence=0.9,
        data={"citations": [_comp_citation(1, eff=0.5, derived=["sig-a"])]},
    )
    deps = _deps("meta_findings_synthesizer", analyst_id="country_composition")
    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=fid, finding_payload=finding, run_id=uuid4(),
    )
    assert result is not None
    data = _insert_data_dict(conn)
    assert data["analyzed_output_id"] == str(fid)
    # overall_score capped by the evidence ceiling.
    assert data["overall_score"] == pytest.approx(0.5)
    verification = data["data"]["verification"]
    assert verification["confidence_ceiling"] == pytest.approx(0.5)
    assert any(s["reason"] == "hedge_laundering" for s in verification["unsupported_spans"])


async def test_verify_seam_composition_double_count_ceiling_max_not_sum():
    """Two cited sub-claims sharing a derived_from signal → double_counted flag and
    the ceiling is the component MAX (0.5), not the naive sum."""
    conn = _FakeConn()
    fid = uuid4()
    finding = FindingPayload(
        title="composition",
        body=(
            "Leadership is contested [[ref:1]].\n"
            "The transition is unstable [[ref:2]].\n"
        ),
        confidence=0.55,
        data={"citations": [
            _comp_citation(1, eff=0.5, derived=["shared-sig"]),
            _comp_citation(2, eff=0.5, derived=["shared-sig"]),
        ]},
    )
    deps = _deps("meta_findings_synthesizer", analyst_id="country_composition")
    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=fid, finding_payload=finding, run_id=uuid4(),
    )
    assert result is not None
    verification = _insert_data_dict(conn)["data"]["verification"]
    assert verification["confidence_ceiling"] == pytest.approx(0.5)
    assert any(s["reason"] == "double_counted" for s in verification["unsupported_spans"])


async def test_verify_seam_composition_no_citations_key_is_noop():
    """SCOPE: a meta_findings_synthesizer finding with NO citations key (the
    honest-EMPTY composition + the global meta) → no-op, nothing persisted."""
    conn = _FakeConn()
    finding = FindingPayload(
        title="No source findings to synthesize",
        body="The other-analyst output slice for this run was empty.",
        confidence=0.0,
        data={"meta": True},  # NO 'citations' key
    )
    deps = _deps("meta_findings_synthesizer", analyst_id="country_composition")
    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=uuid4(), finding_payload=finding, run_id=uuid4(),
    )
    assert result is None
    assert conn.executes == []


# ---------------------------------------------------------------------------
# P3-T5 — the GLOBAL/world composition: the SAME helper verifies a TARGET-LESS
# meta_findings_synthesizer finding whose [[ref:N]] bridge cites the COUNTRY
# reads, and the dispatch discriminator (dapr_actors._descriptor_declares_verify)
# fires it while the old global meta_synthesizer (no verify block) stays excluded.
# ---------------------------------------------------------------------------


async def test_verify_seam_world_composition_target_less_still_verifies():
    """A world composition finding (kind meta_findings_synthesizer, analyst_id
    world_assessor) with a [[ref:N]] country-read citation and NO target
    scoping → the SAME helper writes a critique (the scope guard keys on the
    citations bridge, not target_id). Proves the target-LESS crown is verified."""
    conn = _FakeConn()
    fid = uuid4()
    finding = FindingPayload(
        title="World read",
        body="The country reads point different directions [[ref:1]].\n",
        confidence=0.5,
        data={"citations": [_comp_citation(1, eff=0.6, derived=["sig-a"],
                                           source="country_composition")]},
    )
    deps = _deps("meta_findings_synthesizer", analyst_id="world_assessor")
    result = await verify_inline_target_finding(
        conn, deps=deps, finding_id=fid, finding_payload=finding, run_id=uuid4(),
    )
    assert result is not None
    data = _insert_data_dict(conn)
    assert data["analyzed_output_id"] == str(fid)
    # A fully-cited single clause floors 1.0; the 0.6 sub-claim ceiling caps it.
    assert data["overall_score"] == pytest.approx(0.6)


def test_descriptor_declares_verify_discriminator():
    """The dispatch discriminator: a descriptor DECLARING method.llm.verify →
    True (both compositions); one without it (the old global meta_synthesizer) →
    False. This is what the actor keys the world-composition verify fire on
    (replacing the bool(target_id) gate that excluded the target-less run)."""
    from legba.runtime.dapr_actors import _descriptor_declares_verify

    with_verify = SimpleNamespace(
        method=SimpleNamespace(
            llm={
                "primary": {"raw": "llm.primary.openai_compat"},
                "verify": {"raw": "llm.verify.slm_8b"},
            }
        )
    )
    without_verify = SimpleNamespace(
        method=SimpleNamespace(llm={"primary": {"raw": "llm.primary.openai_compat"}})
    )
    assert _descriptor_declares_verify(with_verify) is True
    assert _descriptor_declares_verify(without_verify) is False
