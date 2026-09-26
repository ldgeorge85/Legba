# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The leaf of the X-1 option-catalog family: ``OptionSpec`` plus the terse
constructor helpers both :mod:`legba.data.analysts.handler_options` and
:mod:`legba.data.analysts.handler_options_programs` need at import time.

**Why this module exists.** Those two siblings used to import from EACH
OTHER: ``handler_options_programs`` read ``OptionSpec`` / ``_pos_int`` /
``_window_days`` / ``_window_hours`` back from ``handler_options``, which in
turn imported ``handler_options_programs``'s three catalog blocks lower in
its own file — a partial circular import that only resolved because of
import ORDER (``handler_options`` had to be the first of the two touched).
Importing the sibling FIRST —
``python -c "import legba.data.analysts.handler_options_programs"`` in a
fresh interpreter — raised ``ImportError: cannot import name
'DESK_REFERENCE_OPTIONS' from partially initialized module``. Order-dependent
correctness is not correctness; this module makes the cycle structurally
impossible instead of merely "safe in practice": it imports from NEITHER
sibling, so whichever of the two a caller reaches first, this module (their
shared, genuine dependency) is already fully initialized by the time either
needs it.

``handler_options.py`` imports every name below and re-exports it under the
same name, so ``handler_options.OptionSpec`` / ``handler_options._pos_int`` /
etc. keep resolving for every existing importer and test — this module is an
internal split of that catalog's own machinery, not a new piece of public
surface. ``handler_options_programs.py`` imports from here directly rather
than from ``handler_options``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

OptionKind = Literal["int", "float", "bool", "str", "str_list"]


@dataclass(frozen=True)
class OptionSpec:
    """One declared, operator-settable knob on one deterministic handler.

    Carries the key's TYPE and admissible RANGE — never its default. The
    handler's own ``options.get(name, DEFAULT)`` remains the sole source of
    truth for the default, so an absent option is byte-identical to today
    by construction and no constant is copied twice.
    """

    name: str
    kind: OptionKind
    doc: str
    minimum: float | None = None
    maximum: float | None = None
    #: Inclusive-lower by default; set False for a strict ``> minimum`` bound
    #: (e.g. a timeout that must be positive, not merely non-negative).
    minimum_inclusive: bool = True
    choices: tuple[str, ...] | None = None
    #: Compiled guard for ``str`` kinds that reach an identifier position.
    pattern: re.Pattern[str] | None = None

    def validate(self, value: Any) -> tuple[bool, Any, str]:
        """Return ``(ok, coerced_value, cause)``. ``cause`` is "" when ok."""
        if self.kind == "bool":
            if not isinstance(value, bool):
                return False, None, "expected bool"
            return True, value, ""

        if self.kind == "int":
            # bool is an int subclass in Python — refuse it explicitly so a
            # `true` in YAML can never become a cap of 1.
            if isinstance(value, bool) or not isinstance(value, int):
                return False, None, "expected int"
            return self._check_range(float(value), value)

        if self.kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, None, "expected number"
            return self._check_range(float(value), float(value))

        if self.kind == "str":
            if not isinstance(value, str) or not value.strip():
                return False, None, "expected non-empty string"
            if self.choices is not None and value not in self.choices:
                return False, None, f"not one of {list(self.choices)}"
            if self.pattern is not None and not self.pattern.match(value):
                return False, None, "does not match the allowed pattern"
            return True, value, ""

        if self.kind == "str_list":
            if not isinstance(value, (list, tuple)):
                return False, None, "expected a list of strings"
            out: list[str] = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    return False, None, "expected a list of non-empty strings"
                if self.choices is not None and item not in self.choices:
                    return False, None, f"'{item}' not one of {list(self.choices)}"
                # A ``pattern`` binds EVERY member, exactly as it binds the whole
                # value on a scalar ``str`` — otherwise a list-shaped knob would
                # be the one place a declared shape guard silently does nothing.
                if self.pattern is not None and not self.pattern.match(item):
                    return False, None, f"'{item}' does not match the allowed pattern"
                out.append(item)
            return True, out, ""

        return False, None, f"unsupported option kind {self.kind!r}"

    def _check_range(self, as_float: float, coerced: Any) -> tuple[bool, Any, str]:
        if self.minimum is not None:
            if self.minimum_inclusive:
                if as_float < self.minimum:
                    return False, None, f"must be >= {self.minimum}"
            elif as_float <= self.minimum:
                return False, None, f"must be > {self.minimum}"
        if self.maximum is not None and as_float > self.maximum:
            return False, None, f"must be <= {self.maximum}"
        return True, coerced, ""


# ---------------------------------------------------------------------------
# Shared spec constructors used by BOTH siblings (the catalog-local ones —
# ``_nonneg_int`` / ``_unit_float`` / ``_nonneg_float`` / ``_pos_float`` /
# ``_flag`` / ``_edge_families`` — stay in ``handler_options.py``, since only
# its own catalog calls them; moving them here would widen this leaf past
# what the cycle fix actually requires)
# ---------------------------------------------------------------------------


def _pos_int(name: str, doc: str, *, maximum: float | None = None) -> OptionSpec:
    """A cap/limit: an int >= 1."""
    return OptionSpec(name, "int", doc, minimum=1, maximum=maximum)


#: A year of hours — the ceiling on every "window/lookback in hours" knob, so
#: a fat-fingered value cannot turn a bounded scan into a full-table walk.
_MAX_WINDOW_HOURS = 8760
#: Ten years of days, same rationale.
_MAX_WINDOW_DAYS = 3650


def _window_hours(name: str, doc: str) -> OptionSpec:
    return OptionSpec(name, "int", doc, minimum=1, maximum=_MAX_WINDOW_HOURS)


def _window_days(name: str, doc: str) -> OptionSpec:
    return OptionSpec(name, "int", doc, minimum=1, maximum=_MAX_WINDOW_DAYS)
