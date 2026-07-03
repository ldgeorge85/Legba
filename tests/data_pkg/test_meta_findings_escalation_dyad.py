# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2-T5 — FLASHPOINT DYAD: escalation_dyad (Iran ↔ Israel).

escalation_dyad reuses the S2-T4 thematic composition machinery but narrows the
fuse to EXACTLY two desks (country_watch_ir + country_watch_il) via the new
`thematic_desks` allow-list marker. It composes the two desks' `escalation`-UNIT
heads (NOT their country_composition heads) so the T7 correlation guard FIRES on a
shared cross-border IR-IL signal. It is STANDALONE — it feeds nothing upward.

This suite locks:
  * the descriptor + the `thematic_desks` marker (dyad allow-list) resolver;
  * the S2-T5 desk-restriction delta in _assemble_thematic_unit_slice — the read
    is target-id-scoped to the 2 desks and the coverage roster is restricted;
  * the T7 CORRELATION GUARD fires on a shared IR-IL signal (capped) and is inert
    on disjoint signals (not capped);
  * STANDALONE — escalation_dyad is in NO composition's other_analysts;
  * REGRESSION — escalation_composition carries NO thematic_desks (the delta is
    additive + guarded; the all-desks composition is byte-for-byte unchanged).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.analysts import meta_findings_synthesizer as synth

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS_DIR = _ROOT / "descriptors"

_DYAD_FILE = "analyst_escalation_dyad.yaml"
_IR = "country_watch_ir"
_IL = "country_watch_il"
_IN = "country_g20_in"


def _load(name: str) -> AnalystDescriptor:
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


def _bringup_module():
    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts",
        _ROOT / "scripts" / "bringup_register_analysts.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- fixtures (mirrors test_meta_findings_escalation_composition.py) ----------


class _ThematicConn:
    """Fake conn routing READ_SLICE's two queries by SQL content."""

    def __init__(self, *, roster: list[dict[str, Any]],
                 slice_rows: list[dict[str, Any]] | None = None) -> None:
        self._roster = roster
        self._slice_rows = slice_rows or []
        self.roster_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.slice_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        if "target_descriptors" in query:
            self.roster_calls.append((query, params))
            return list(self._roster)
        self.slice_calls.append((query, params))
        return list(self._slice_rows)


class _CannedLLM:
    subprovider = "escalation_dyad_test_double"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs) -> Any:
        self.calls.append({"messages": list(messages), "system": system})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _escalation_head_row(*, uid: UUID, target_id: str, title: str,
                         effective_confidence: float = 0.6,
                         derived_from: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": uid, "kind": "finding", "title": title,
        "body": f"{title} escalation read body",
        "confidence": 0.7, "effective_confidence": effective_confidence,
        "faithfulness_score": 0.9, "severity": None,
        "data": {"evidence": []}, "evidence": [],
        "target_id": target_id, "target_version": None,
        "analyst_id": "escalation", "analyst_version": "vtest",
        "produced_at": "2026-07-02T00:00:00+00:00",
        "derived_from": list(derived_from or []),
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0", "run_id": uuid4(),
    }


def _roster(*desks: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"descriptor_id": did, "name": name} for did, name in desks]


def _dyad_options(**extra: Any) -> dict[str, Any]:
    opts = {"analyst_id": "escalation_dyad", "thematic_dimension": "escalation",
            "run_id": uuid4()}
    opts.update(extra)
    return opts


# --- 1. descriptor + markers --------------------------------------------------


def test_descriptor_validates_target_less():
    desc = _load(_DYAD_FILE)
    assert desc.identity.id == "escalation_dyad"
    assert desc.identity.kind == "meta_findings_synthesizer"
    assert desc.identity.state == "active"
    # target-LESS ⇒ one global run.
    assert desc.subscription.targets is None


def test_thematic_markers_dimension_and_desk_allowlist():
    desc = _load(_DYAD_FILE)
    assert synth.thematic_dimension(desc) == "escalation"
    assert synth.thematic_desks(desc) == [_IR, _IL]


def test_in_bringup_set():
    mod = _bringup_module()
    assert _DYAD_FILE in mod.ANALYST_FILES


# --- 2. the S2-T5 desk-restriction delta (target-id scope + roster) -----------


@pytest.mark.asyncio
async def test_dyad_slice_restricts_to_the_two_desks():
    """With desk_ids set, the thematic slice reads target-id-scoped to the 2 desks
    (the ANY() clause appears) AND the coverage roster is restricted to them — a
    third g20 desk in the roster is NOT covered."""
    desc = _load(_DYAD_FILE)
    heads = [
        _escalation_head_row(uid=uuid4(), target_id=_IR, title="Iran"),
        _escalation_head_row(uid=uuid4(), target_id=_IL, title="Israel"),
    ]
    conn = _ThematicConn(
        roster=_roster((_IR, "Iran"), (_IL, "Israel"), (_IN, "India")),
        slice_rows=heads,
    )
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)

    # (1) the slice read is target-id-scoped to the allow-list (ANY() clause).
    assert conn.slice_calls, "dyad run must read the escalation heads"
    sq, _ = conn.slice_calls[0]
    assert "f.target_id = ANY(" in sq
    assert "f.target_id = $" not in sq          # a SET, not a single scope

    # (2) coverage is restricted to the 2 dyad desks — India (not in the allow-list)
    #     is NOT covered even though it's in the roster.
    assert rows
    cov = rows[0]["_thematic_coverage"]
    assert {c["desk_id"] for c in cov} == {_IR, _IL}


# --- 3. the T7 correlation guard (the whole point of a dyad) ------------------


@pytest.mark.asyncio
async def test_guard_fires_on_shared_ir_il_signal():
    """A single cross-border IR-IL incident (one shared signal in both escalation
    heads' derived_from) → the guard FIRES, collapses to one component, caps the
    fused confidence (never double-counts the shared wire)."""
    shared_signal = str(uuid4())
    iran, israel = uuid4(), uuid4()
    rows = [
        _escalation_head_row(uid=iran, target_id=_IR, title="Iran",
                             effective_confidence=0.6,
                             derived_from=[shared_signal, str(uuid4())]),
        _escalation_head_row(uid=israel, target_id=_IL, title="Israel",
                             effective_confidence=0.6,
                             derived_from=[shared_signal]),
    ]
    body = ("BLUF: one cross-border incident drives the dyad. Iran escalates "
            "[[ref:1]] and Israel responds [[ref:2]].")
    llm = _CannedLLM({"title": "IR-IL dyad", "body": body, "confidence": 0.9,
                      "evidence": [], "tags": ["escalation"]})
    result = await synth.run_method(list(rows), _dyad_options(), _Deps(llm))

    guard = result.finding.data["correlation_guard"]
    assert guard["shared_lineage_detected"] is True
    assert guard["cited_heads"] == 2
    assert guard["independent_components"] == 1
    grp = guard["correlated_groups"][0]
    assert set(grp["ordinals"]) == {1, 2}
    assert set(grp["desks"]) == {_IR, _IL}
    assert shared_signal in grp["shared_signals"]
    assert guard["dedup_confidence_ceiling"] == pytest.approx(0.6)
    assert guard["confidence_capped"] is True
    assert result.finding.confidence == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_guard_inert_on_disjoint_signals():
    """Contrast: IR + IL on DISJOINT signals are independent — no cap."""
    iran, israel = uuid4(), uuid4()
    rows = [
        _escalation_head_row(uid=iran, target_id=_IR, title="Iran",
                             effective_confidence=0.6, derived_from=[str(uuid4())]),
        _escalation_head_row(uid=israel, target_id=_IL, title="Israel",
                             effective_confidence=0.5, derived_from=[str(uuid4())]),
    ]
    body = "Iran escalates [[ref:1]]; Israel is calm [[ref:2]]."
    llm = _CannedLLM({"title": "t", "body": body, "confidence": 0.55,
                      "evidence": [], "tags": ["e"]})
    result = await synth.run_method(list(rows), _dyad_options(), _Deps(llm))

    guard = result.finding.data["correlation_guard"]
    assert guard["shared_lineage_detected"] is False
    assert guard["independent_components"] == 2
    assert guard["confidence_capped"] is False
    assert result.finding.confidence == pytest.approx(0.55)


# --- 4. STANDALONE + regression ----------------------------------------------


def test_standalone_feeds_no_composition():
    """The dyad feeds nothing upward — it is in NO composition's other_analysts."""
    for parent in ("analyst_country_composition.yaml",
                   "analyst_region_composition.yaml",
                   "analyst_world_assessor.yaml"):
        desc = _load(parent)
        other = getattr(desc.subscription, "other_analysts", None) or []
        ids = [a.id for a in other]
        assert "escalation_dyad" not in ids, f"{parent} must not read the dyad"


def test_regression_escalation_composition_spans_all_desks():
    """The delta is additive + guarded: escalation_composition carries NO
    thematic_desks marker, so it still spans EVERY desk (byte-for-byte)."""
    comp = _load("analyst_escalation_composition.yaml")
    assert synth.thematic_dimension(comp) == "escalation"
    assert synth.thematic_desks(comp) is None
