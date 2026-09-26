# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M22 — the opportunistic-RAG auto-rollback guard (legba.runtime.rag_rollback).

Infra-free unit tests for the REAL code guard that replaces the pre-M22 comments-
only rollback:

  * ``evaluate_rollback`` — the pure pre-registered rule: fires on (a) faithfulness
    drop, (b) low-faith-rate ratio, or (c) token-cost rise; passes otherwise; marks
    an under-filled window provisional.
  * ``world_context_disabled_units`` / ``is_world_context_enabled`` — the kill-switch
    sourced from the env pin AND the persisted state file.
  * ``record_rollback`` — the actuator that persists a unit into the state file.
  * The GENERALIZED kill-switch (``disabled_units_for`` / ``is_source_enabled`` /
    ``record_rollback_for``) and the ``exemplar`` corpus's own named wrappers
    (``exemplar_disabled_units`` / ``is_exemplar_enabled``) — the new RAG corpus
    gets the SAME rollback treatment as ``world_context``, and the two sources'
    state stays isolated in one shared state file.
"""

from __future__ import annotations

import json

import pytest

from legba.runtime.rag_rollback import (
    DEFAULT_TOKEN_RISE_FRAC,
    RollbackWindow,
    disabled_units_for,
    evaluate_rollback,
    exemplar_disabled_units,
    is_exemplar_enabled,
    is_source_enabled,
    is_world_context_enabled,
    record_rollback,
    record_rollback_for,
    world_context_disabled_units,
)

_ENV_DISABLED = "LEGBA_WORLD_CONTEXT_DISABLED_UNITS"
_ENV_STATE = "LEGBA_RAG_ROLLBACK_STATE"
_ENV_EXEMPLAR_DISABLED = "LEGBA_EXEMPLAR_DISABLED_UNITS"


# ---------------------------------------------------------------------------
# evaluate_rollback — the rule
# ---------------------------------------------------------------------------


def _win(n=15, faith=None, low_rate=None, low_count=0, tokens=None):
    return RollbackWindow(
        n=n, mean_faith=faith, low_faith_rate=low_rate,
        low_faith_count=low_count, tokens_mean=tokens,
    )


def test_no_trigger_when_stable():
    d = evaluate_rollback(_win(faith=0.71), _win(faith=0.70), window=15)
    assert d.triggered is False
    assert d.reasons == []
    assert d.provisional is False


def test_trigger_a_faithfulness_drop():
    d = evaluate_rollback(_win(faith=0.71), _win(faith=0.62), window=15)
    assert d.triggered is True
    assert any("faithfulness dropped" in r for r in d.reasons)
    assert d.faith_delta == pytest.approx(0.09)


def test_faith_drop_below_trigger_does_not_fire():
    # 0.07 drop < 0.08 trigger.
    d = evaluate_rollback(_win(faith=0.70), _win(faith=0.63), window=15)
    assert d.triggered is False


def test_trigger_b_low_faith_rate_doubles():
    d = evaluate_rollback(
        _win(faith=0.70, low_rate=0.10, low_count=1),
        _win(faith=0.70, low_rate=0.30, low_count=4),
        window=15,
    )
    assert d.triggered is True
    assert any("low-faith rate" in r for r in d.reasons)


def test_trigger_b_zero_baseline_needs_two_low_rows():
    # Clean baseline (rate 0): one post-flip low-faith row does NOT fire...
    one = evaluate_rollback(
        _win(faith=0.70, low_rate=0.0, low_count=0),
        _win(faith=0.70, low_rate=0.06, low_count=1),
        window=15,
    )
    assert one.triggered is False
    # ...two DOES.
    two = evaluate_rollback(
        _win(faith=0.70, low_rate=0.0, low_count=0),
        _win(faith=0.70, low_rate=0.13, low_count=2),
        window=15,
    )
    assert two.triggered is True


def test_trigger_c_token_cost_rise():
    # +50% avg tokens/run at the default 0.35 trigger, faithfulness flat.
    before = _win(faith=0.70, tokens=10_000)
    after = _win(faith=0.70, tokens=15_000)
    d = evaluate_rollback(before, after, window=15)
    assert d.triggered is True
    assert any("tokens/run" in r for r in d.reasons)
    assert d.token_rise_frac == pytest.approx(0.5)


def test_token_trigger_default_is_035():
    # FIX B: 0.35 (not 0.50) so the trigger CATCHES the motivating +42% case.
    assert DEFAULT_TOKEN_RISE_FRAC == 0.35


def test_token_rise_catches_motivating_42pct_case():
    # The leadership_transition rollback rode a +42% token rise — the whole reason
    # (c) exists. A 0.50 default would MISS it; 0.35 catches it.
    before = _win(faith=0.70, tokens=10_000)
    after = _win(faith=0.70, tokens=14_200)  # +42%
    d = evaluate_rollback(before, after, window=15)
    assert d.triggered is True
    assert d.token_rise_frac == pytest.approx(0.42)


def test_token_rise_below_trigger_does_not_fire():
    d = evaluate_rollback(
        _win(faith=0.70, tokens=10_000), _win(faith=0.70, tokens=13_000), window=15,
    )
    assert d.triggered is False  # +30% < 35%


def test_provisional_when_window_underfilled():
    d = evaluate_rollback(_win(n=6, faith=0.71), _win(n=4, faith=0.62), window=15)
    assert d.triggered is True
    assert d.provisional is True


def test_missing_inputs_never_raise_and_dont_fire():
    d = evaluate_rollback(_win(faith=None), _win(faith=None), window=15)
    assert d.triggered is False


# ---------------------------------------------------------------------------
# kill-switch — env pin + persisted state
# ---------------------------------------------------------------------------


def test_kill_switch_env_pin(monkeypatch):
    monkeypatch.setenv(_ENV_DISABLED, "internal_stability, Leadership_Transition")
    monkeypatch.delenv(_ENV_STATE, raising=False)
    disabled = world_context_disabled_units()
    assert "internal_stability" in disabled
    # casefolded.
    assert "leadership_transition" in disabled
    assert is_world_context_enabled("internal_stability") is False
    assert is_world_context_enabled("proliferation_watch") is True


def test_kill_switch_empty_env_enables_all(monkeypatch):
    monkeypatch.delenv(_ENV_DISABLED, raising=False)
    monkeypatch.delenv(_ENV_STATE, raising=False)
    assert world_context_disabled_units() == frozenset()
    assert is_world_context_enabled("internal_stability") is True


def test_record_rollback_persists_and_disables(monkeypatch, tmp_path):
    state = tmp_path / "rag_rollback.json"
    monkeypatch.setenv(_ENV_STATE, str(state))
    monkeypatch.delenv(_ENV_DISABLED, raising=False)

    # before: enabled.
    assert is_world_context_enabled("internal_stability") is True
    path = record_rollback("internal_stability", reasons=["faithfulness dropped -0.09"])
    assert path == str(state)
    # after: the runtime kill-switch now reverts it.
    assert is_world_context_enabled("internal_stability") is False
    assert "internal_stability" in world_context_disabled_units()

    # the state file carries the disabled unit + an audit log entry.
    data = json.loads(state.read_text())
    assert data["disabled_units"] == ["internal_stability"]
    assert data["rollback_log"][0]["analyst_id"] == "internal_stability"
    assert "faithfulness dropped -0.09" in data["rollback_log"][0]["reasons"]


def test_record_rollback_idempotent(monkeypatch, tmp_path):
    state = tmp_path / "s.json"
    monkeypatch.setenv(_ENV_STATE, str(state))
    record_rollback("unit_a")
    record_rollback("unit_a")
    data = json.loads(state.read_text())
    assert data["disabled_units"] == ["unit_a"]  # not duplicated
    assert len(data["rollback_log"]) == 2  # every action is logged


def test_record_rollback_no_state_path_returns_none(monkeypatch):
    monkeypatch.delenv(_ENV_STATE, raising=False)
    assert record_rollback("unit_a") is None


def test_env_and_state_union(monkeypatch, tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"disabled_units": ["from_state"]}))
    monkeypatch.setenv(_ENV_STATE, str(state))
    monkeypatch.setenv(_ENV_DISABLED, "from_env")
    disabled = world_context_disabled_units()
    assert {"from_env", "from_state"} <= disabled


# ---------------------------------------------------------------------------
# generalized kill-switch — the `exemplar` corpus gets the SAME treatment
# ---------------------------------------------------------------------------


def test_exemplar_env_pin(monkeypatch):
    monkeypatch.setenv(_ENV_EXEMPLAR_DISABLED, "port_capacity_crisis_unit")
    monkeypatch.delenv(_ENV_STATE, raising=False)
    disabled = exemplar_disabled_units()
    assert "port_capacity_crisis_unit" in disabled
    assert is_exemplar_enabled("port_capacity_crisis_unit") is False
    assert is_exemplar_enabled("some_other_unit") is True


def test_exemplar_rollback_persists_and_disables(monkeypatch, tmp_path):
    state = tmp_path / "rag_rollback.json"
    monkeypatch.setenv(_ENV_STATE, str(state))
    monkeypatch.delenv(_ENV_EXEMPLAR_DISABLED, raising=False)

    assert is_exemplar_enabled("chokepoint_unit") is True
    path = record_rollback_for(
        "exemplar", "chokepoint_unit", reasons=["faithfulness dropped -0.12"]
    )
    assert path == str(state)
    assert is_exemplar_enabled("chokepoint_unit") is False
    assert "chokepoint_unit" in exemplar_disabled_units()

    data = json.loads(state.read_text())
    # Own state key ("exemplar_disabled_units"), NOT the legacy bare
    # "disabled_units" key world_context uses — the two sources never collide.
    assert data["exemplar_disabled_units"] == ["chokepoint_unit"]
    assert "disabled_units" not in data
    entry = data["rollback_log"][0]
    assert entry["source"] == "exemplar"
    assert entry["analyst_id"] == "chokepoint_unit"
    assert "faithfulness dropped -0.12" in entry["reasons"]


def test_rollback_sources_are_isolated(monkeypatch, tmp_path):
    """Rolling back exemplar must NOT touch world_context, and vice versa —
    the whole point of parameterizing by ``source``."""
    state = tmp_path / "s.json"
    monkeypatch.setenv(_ENV_STATE, str(state))
    monkeypatch.delenv(_ENV_DISABLED, raising=False)
    monkeypatch.delenv(_ENV_EXEMPLAR_DISABLED, raising=False)

    record_rollback_for("exemplar", "shared_unit_name")

    assert is_exemplar_enabled("shared_unit_name") is False
    assert is_world_context_enabled("shared_unit_name") is True

    record_rollback("shared_unit_name")  # the legacy world_context actuator

    assert is_exemplar_enabled("shared_unit_name") is False
    assert is_world_context_enabled("shared_unit_name") is False

    # Both sources' units + a SHARED audit log, tagged by source.
    data = json.loads(state.read_text())
    assert data["exemplar_disabled_units"] == ["shared_unit_name"]
    assert data["disabled_units"] == ["shared_unit_name"]
    assert [e["source"] for e in data["rollback_log"]] == ["exemplar", "world_context"]


def test_generic_source_api_matches_named_wrappers(monkeypatch, tmp_path):
    """``disabled_units_for`` / ``is_source_enabled`` / ``record_rollback_for``
    are what the named per-corpus wrappers are built on — same result either
    way, for any source string (not just the two named ones)."""
    state = tmp_path / "s.json"
    monkeypatch.setenv(_ENV_STATE, str(state))

    record_rollback_for("tradecraft", "some_unit", reasons=["manual pin"])
    assert is_source_enabled("tradecraft", "some_unit") is False
    assert "some_unit" in disabled_units_for("tradecraft")
    # A brand-new source name works with zero prior wiring (no bespoke guard
    # required per corpus — that is the whole point of the generalization).
    assert is_source_enabled("some_future_corpus", "some_unit") is True
