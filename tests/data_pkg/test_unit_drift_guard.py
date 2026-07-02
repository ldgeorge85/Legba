# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-T7 — the bringup unit drift guard fails loud on missing eval coverage.

A bounded reasoning UNIT (the T1/T2 unit-factory pattern) is JUST an
inline_target DESCRIPTOR carrying its OWN inline ``method.system_prompt`` +
``eval.rubric`` + ``method.llm.verify``. The bringup script's drift guard
(:func:`_assert_unit_eval_coverage`) must FAIL LOUD at register time — the
earliest point — if any such unit lacks:

  (a) ``eval.rubric``       — the critic hard-fails without it; AND
  (b) ``method.llm.verify`` — the faithfulness judge ref (else the unit
      silently falls back to the deterministic floor).

These tests construct unit descriptor dicts (rubric-less, verify-less, and
complete) and assert the guard raises on the gaps + passes the complete one.
They also assert the country_assessor MONOLITH FEEDER (inline_target +
prompt_module, NO inline system_prompt) is exempt — it is not a bounded unit.

The guard is loaded from the bringup script via importlib (the exact module the
operator runs at bringup), so the test can't drift from the live guard.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_BRINGUP = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "bringup_register_analysts.py"
)


def _load_bringup():
    """Import the bringup module by path (no main() run — it's __main__-guarded)."""
    spec = importlib.util.spec_from_file_location("_bringup_register_analysts", _BRINGUP)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_bringup()


# ---------------------------------------------------------------------------
# Descriptor-body builders (the RAW dict shape the guard inspects).
# ---------------------------------------------------------------------------


def _unit_body(*, with_rubric: bool = True, with_verify: bool = True) -> dict:
    """A bounded UNIT body: inline_target + inline system_prompt (+/- coverage)."""
    llm: dict = {
        "primary": {
            "factory_kind": "stack_ref",
            "raw": "llm.primary.openai_compat",
            "expected_family": "llm_provider",
        },
    }
    if with_verify:
        llm["verify"] = {
            "factory_kind": "stack_ref",
            "raw": "llm.verify.slm_8b",
            "expected_family": "llm_provider",
        }
    body: dict = {
        "identity": {"id": "test_unit", "kind": "inline_target"},
        "method": {
            "kind": "llm_planner",
            "system_prompt": "You are a bounded test unit. Cite every claim with [N].",
            "llm": llm,
        },
    }
    if with_rubric:
        body["eval"] = {"rubric": '{"dimensions": [], "scale": "0.0-1.0"}'}
    return body


def _build_named_unit(unit_id: str, **kw) -> dict:
    """A unit body with a specific identity.id (the builders default to one id)."""
    body = _unit_body(**kw)
    body["identity"]["id"] = unit_id
    return body


def _feeder_body() -> dict:
    """A country_assessor-style FEEDER: inline_target but prompt_module-driven,
    NO inline system_prompt, and deliberately NO rubric/verify — to prove the
    guard exempts it (it is not a bounded unit)."""
    return {
        "identity": {"id": "country_assessor", "kind": "inline_target"},
        "method": {
            "kind": "llm_planner",
            "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            "llm": {
                "primary": {
                    "factory_kind": "stack_ref",
                    "raw": "llm.primary.openai_compat",
                    "expected_family": "llm_provider",
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Unit-signature detection.
# ---------------------------------------------------------------------------


def test_unit_signature_detection():
    """inline_target + inline system_prompt => unit; prompt_module feeder => not."""
    assert _MOD._is_bounded_unit(_unit_body()) is True
    # The feeder is inline_target but prompt_module-driven (no inline prompt).
    assert _MOD._is_bounded_unit(_feeder_body()) is False
    # A non-inline_target descriptor is never a unit.
    deterministic = {
        "identity": {"id": "fact_decay", "kind": "deterministic"},
        "method": {"kind": "deterministic", "impl": "legba...:run"},
    }
    assert _MOD._is_bounded_unit(deterministic) is False


# ---------------------------------------------------------------------------
# The guard: raises on a gap, passes a complete unit.
# ---------------------------------------------------------------------------


def test_complete_unit_passes():
    """A unit carrying BOTH eval.rubric and method.llm.verify registers clean."""
    # No raise == pass.
    _MOD._assert_unit_eval_coverage([("analyst_test_unit.yaml", _unit_body())])


def test_rubric_less_unit_raises():
    body = _unit_body(with_rubric=False, with_verify=True)
    with pytest.raises(_MOD.UnitDriftError) as exc:
        _MOD._assert_unit_eval_coverage([("analyst_test_unit.yaml", body)])
    assert "eval.rubric" in str(exc.value)
    assert "test_unit" in str(exc.value)


def test_verify_less_unit_raises():
    body = _unit_body(with_rubric=True, with_verify=False)
    with pytest.raises(_MOD.UnitDriftError) as exc:
        _MOD._assert_unit_eval_coverage([("analyst_test_unit.yaml", body)])
    assert "method.llm.verify" in str(exc.value)


def test_unit_missing_both_lists_both():
    body = _unit_body(with_rubric=False, with_verify=False)
    with pytest.raises(_MOD.UnitDriftError) as exc:
        _MOD._assert_unit_eval_coverage([("analyst_test_unit.yaml", body)])
    msg = str(exc.value)
    assert "eval.rubric" in msg and "method.llm.verify" in msg


def test_feeder_is_exempt():
    """country_assessor (prompt_module feeder, no rubric/verify) must NOT trip
    the guard — it is not a bounded unit."""
    # No raise even though the feeder body carries neither rubric nor verify.
    _MOD._assert_unit_eval_coverage([("analyst_country_assessor.yaml", _feeder_body())])


def test_mixed_set_reports_only_the_offending_unit():
    """A complete unit + a feeder pass; an incomplete unit in the same set raises
    and names ONLY the offender."""
    good = ("analyst_good_unit.yaml", _unit_body())
    feeder = ("analyst_country_assessor.yaml", _feeder_body())
    bad_body = _unit_body(with_rubric=False)
    bad_body["identity"]["id"] = "broken_unit"
    bad = ("analyst_broken_unit.yaml", bad_body)
    with pytest.raises(_MOD.UnitDriftError) as exc:
        _MOD._assert_unit_eval_coverage([good, feeder, bad])
    msg = str(exc.value)
    assert "broken_unit" in msg
    assert "test_unit" not in msg  # the good unit isn't flagged


# ---------------------------------------------------------------------------
# The enumerator surfaces per-unit coverage (and skips non-units).
# ---------------------------------------------------------------------------


def test_coverage_rows_enumerate_units_only():
    rows = _MOD._unit_coverage_rows(
        [
            ("analyst_complete.yaml", _build_named_unit("complete_unit")),
            ("analyst_country_assessor.yaml", _feeder_body()),
            ("analyst_vless.yaml", _build_named_unit("vless_unit", with_verify=False)),
            ("analyst_rless.yaml", _build_named_unit("rless_unit", with_rubric=False)),
        ]
    )
    # The feeder is skipped; the three units are enumerated with their status.
    by_id = {unit_id: (r, v) for unit_id, _f, r, v in rows}
    assert "country_assessor" not in by_id
    assert by_id == {
        "complete_unit": (True, True),
        "vless_unit": (True, False),
        "rless_unit": (False, True),
    }


def test_t5_scorer_wired_into_bringup():
    """The P2-T5 unit-correctness scorer descriptor must be in ANALYST_FILES so
    bringup registers it (the T5 agent does not touch bringup)."""
    assert "analyst_unit_correctness_scorer.yaml" in _MOD.ANALYST_FILES
