# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE HOUSE SAMPLING TEMPERATURE — one named constant, one rule (2026-11-13).

Standing operator rule: temperature 1.0 on every model call the platform
makes. The served models' vendors document 1.0 (one model card for all uses,
one vendor forces it in thinking mode), and the gateway fixes it for ``gemma``.
Every call site that SENDS a temperature imports :data:`HOUSE_TEMPERATURE`
from here; a numeric literal in ``src/legba`` outside this module is a defect
(pinned by ``tests/test_house_temperature_guard.py``).

This is a leaf module with no imports beyond the standard library, so the
registry's slim image and the runtime import it alike.
"""
from __future__ import annotations

from typing import Any

HOUSE_TEMPERATURE: float = 1.0

#: The sampling regime recorded beside judge / critic / grader outputs written
#: under the house rule; it rides the judge pipeline version stamp.
SAMPLING_REGIME: str = "temperature-1.0"


def validate_house_temperature(value: Any, *, where: str = "temperature") -> float:
    """Return ``value`` as a float when it is the house temperature, else raise.

    Raises :class:`ValueError` naming ``where``; used at descriptor save so a
    ``method.llm.temperature`` other than 1.0 is refused with a clear reason
    rather than silently sent.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{where}: {value!r} is not a number; the house temperature is {HOUSE_TEMPERATURE}"
        ) from None
    if v != HOUSE_TEMPERATURE:
        raise ValueError(
            f"{where}: {v} is not allowed; the platform's rule is temperature "
            f"{HOUSE_TEMPERATURE} on every model call (omit the field to take it)"
        )
    return v


__all__ = ["HOUSE_TEMPERATURE", "SAMPLING_REGIME", "validate_house_temperature"]
