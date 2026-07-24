# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 registrar-train smoke: signal_salience discovers as a kind + validates."""
from __future__ import annotations


def test_signal_salience_kind_is_discovered():
    from legba.data.analysts import discover_analyst_kinds
    reg = discover_analyst_kinds()
    assert "signal_salience" in reg, sorted(reg)
    h = reg["signal_salience"]
    assert callable(h.run_method)


def test_signal_salience_kind_name_registered_for_validation():
    # register_analyst_kind is called at import of legba.data.analysts.
    import legba.data.analysts  # noqa: F401  (side-effect: registers the kind)
    from legba.data.schemas.analyst import is_known_analyst_kind
    assert is_known_analyst_kind("signal_salience")
