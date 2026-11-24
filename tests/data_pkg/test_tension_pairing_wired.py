# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W2 through ``_tensions``: the desk/indicator gate reaches the declared tensions."""
from __future__ import annotations

from tests.data_pkg.test_composition_assembly_d2 import _assembly, _row

UP = "**BLUF:** Oil exports through the corridor rose sharply.\n"
DOWN = "**BLUF:** Oil exports through the corridor fell sharply.\n"


def test_different_desks_on_different_units_declare_no_tension() -> None:
    up = _row("a", analyst_id="narrative_coordination", target_id="country_g20_us", body=UP)
    down = _row("b", analyst_id="energy_security", target_id="country_g20_cn", body=DOWN)
    _, payload = _assembly([up, down])
    assert payload["tension_checked"]["pairs_found"] == 0
    assert payload["tensions"] == []


def test_n_c_same_desk_on_different_units_declares_no_tension() -> None:
    """N-C rule 0: one entity AND one variable — a shared desk across two units
    is two variables (was: declared as "the same dimension")."""
    up = _row("a", analyst_id="energy_security", target_id="country_g20_us", body=UP)
    down = _row("b", analyst_id="energy_security", target_id="country_g20_cn", body=DOWN)
    _, payload = _assembly([up, down])
    assert payload["tension_checked"]["pairs_found"] == 0
    assert payload["tensions"] == []


def test_n_c_different_desks_on_one_unit_declare_no_tension() -> None:
    """N-C follow-up: one variable at every tier (was: declared)."""
    up = _row("a", analyst_id="narrative_coordination", target_id="country_g20_us", body=UP)
    down = _row("b", analyst_id="energy_security", target_id="country_g20_us", body=DOWN)
    _, payload = _assembly([up, down])
    assert payload["tension_checked"]["pairs_found"] == 0


def test_one_desk_on_one_unit_declares_it() -> None:
    up = _row("a", analyst_id="energy_security", target_id="country_g20_us", body=UP)
    down = _row("b", analyst_id="energy_security", target_id="country_g20_us", body=DOWN)
    _, payload = _assembly([up, down])
    assert payload["tension_checked"]["pairs_found"] == 1
