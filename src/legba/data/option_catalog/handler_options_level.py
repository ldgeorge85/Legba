# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 option spec — the LEVEL-SHAPED indicator set of a bounded unit (X4 LEVEL,
W11-F36).

A unit's indicators are CHANGE-shaped by default: "a new sanction", "a pipeline
attack" — a signpost that fires once, when something happens. A unit whose
status is a STANDING CONDITION (energy security under a sanctions regime, a
military on a war footing) is never re-evidenced by a change-shaped signpost,
so its honest status is withheld from compositions as ``carried_unsupported``
(``legba.data.carried_state``) forever. A LEVEL indicator measures the
condition itself against a declared reference, every window.

``method.options.level_indicators`` names which of the unit's canonical
indicator ids are level-shaped and what each is measured against. Every other
id stays change-shaped — an absent option is byte-identical to today.

One entry per level indicator, ``<indicator-id>=<reference>``, where the
reference is

* ``bar:<text>`` — a declared bar, in words the prompt repeats verbatim
  ("a report inside this window that the measure is still in force"); or
* ``series:<series-id>[ <text>]`` — a series from the collections
  (``observations.series_id``) and, optionally, the bar on it.

The last composition's band is NOT a reference kind: the unit prompt does not
carry it, and a yardstick the model cannot see is not one. A level indicator
is re-evidenced only by a report in the run's own window, whatever the
reference (``legba.data.indicator_shape``).

Split into its own leaf for the module-size gate, the ``handler_options_events``
/ ``_inquiry`` / ``_programs`` idiom; stdlib + siblings only (this package is a
leaf the slim registry imports).
"""

from __future__ import annotations

import re

from .handler_options_base import OptionSpec

#: The descriptor knob, ``method.options.level_indicators``.
LEVEL_INDICATORS_OPTION = "level_indicators"

#: The two reference kinds.
REFERENCE_BAR = "bar"
REFERENCE_SERIES = "series"
REFERENCE_KINDS = (REFERENCE_BAR, REFERENCE_SERIES)

#: One entry. The id is the unit's canonical indicator slug; a bar is one line
#: of at most 240 characters; a series id is an ``observations.series_id``
#: (lower-case, dots and underscores, at most 96), optionally followed by the
#: bar on it (at most 140). Every reference therefore fits the 256-character
#: ``IndicatorEntry.reference`` it is stamped into (a test pins the bound).
LEVEL_ENTRY_PATTERN = re.compile(
    r"^(?P<id>[a-z0-9][a-z0-9-]{0,63})="
    r"(?:bar:(?P<bar>\S[^\r\n]{0,239})"
    r"|series:(?P<series>[a-z0-9][a-z0-9_.]{0,95})(?: (?P<series_bar>\S[^\r\n]{0,139}))?)"
    r"\Z"
)

LEVEL_INDICATOR_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        LEVEL_INDICATORS_OPTION,
        "str_list",
        "The unit's LEVEL-shaped indicators: one "
        "'<indicator-id>=bar:<text>' or '<indicator-id>=series:<series-id>"
        "[ <text>]' per indicator, naming what the level is measured against. "
        "A declared id is stamped shape 'level' with its reference, reads "
        "'observed_at_level' instead of 'triggered', and re-evidences a "
        "standing status for the carried-state rule only when it cites a "
        "report from the run's own window. Absent ⇒ every indicator is "
        "change-shaped (byte-identical).",
        pattern=LEVEL_ENTRY_PATTERN,
    ),
)

__all__ = [
    "LEVEL_ENTRY_PATTERN",
    "LEVEL_INDICATORS_OPTION",
    "LEVEL_INDICATOR_OPTIONS",
    "REFERENCE_BAR",
    "REFERENCE_KINDS",
    "REFERENCE_SERIES",
]
