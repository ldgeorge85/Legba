# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""LEVEL-SHAPED INDICATORS — evidence for a standing status.

The carried-state rule (``legba.data.carried_state``) withholds a unit
status from compositions once none of the unit's own indicators has
re-evidenced it inside N cadence windows. A live census:
46 of 159 status-bearing unit heads carried, mostly ``energy_security`` at
"high / elevated" on G20 desks — a STANDING CONDITION (a sanctions regime in
force, a strait under attack, a grid emergency) that change-shaped indicators
("a new sanction", "a pipeline attack") never re-evidence. The rule is right;
the unit had nothing honest to say with.

A LEVEL indicator measures the condition itself against a declared reference
(``method.options.level_indicators``, ``option_catalog.handler_options_level``)
and reads ``observed_at_level`` when this window's own reporting places the
condition at the level. Three rules, one module:

1. **The declaration is the handler's, never the model's.** Before coercion
   (:func:`shape_raw_indicators`) a declared id is stamped ``shape='level'`` and
   its ``reference``, and a ``triggered`` on it reads ``observed_at_level`` (a
   level does not "fire"; it stands). A shape or reference the model wrote on
   an undeclared id is stripped, so an ``observed_at_level`` there fails the
   schema and drops like any malformed entry.
2. **Only a report re-evidences a level.** After citation resolution
   (:func:`demote_unsupported_levels`) an ``observed_at_level`` whose citations
   resolve to no collected signal — none at all, or only desk grounding (the
   unit's own PRIOR READ, the situation register, a HISTORICAL SERIES line) —
   is set back to ``not_observed``, citations cleared, and named in
   ``data.level_indicator_demotions``. The unit's memory of a condition is not
   evidence that it still holds; that is the exact carry W11-F14 closed.
3. **The carried-state rule counts it** (:func:`re_evidences`,
   :data:`RE_EVIDENCE_JSONPATH` — one predicate, Python and SQL): a read
   re-evidences its status when an indicator ``triggered``, or when a
   level-shaped one was ``observed_at_level`` with at least one citation.

A level observation is NOT an I&W activation: ``indicator_tracker`` keeps its
three-value scale (``triggered`` / ``not_observed`` / ``expired``) and skips
the new status, so a standing condition never reads as a stream of firings.

Stdlib only: ``carried_state`` imports this, and the registry's slim image
imports ``carried_state`` through the absence route.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .option_catalog.handler_options_level import (
    LEVEL_ENTRY_PATTERN,
    LEVEL_INDICATORS_OPTION,
    REFERENCE_BAR,
    REFERENCE_SERIES,
)

logger = logging.getLogger(__name__)

TRIGGERED = "triggered"
NOT_OBSERVED = "not_observed"
OBSERVED_AT_LEVEL = "observed_at_level"
SHAPE_LEVEL = "level"

#: Where a finding names the level observations rule 2 set back.
DEMOTIONS_DATA_KEY = "level_indicator_demotions"

#: The re-evidence predicate in SQL/JSON path, over ``analyst_outputs.data``
#: (the finding envelope: the payload's data sits at ``data -> 'data'``).
#: :func:`re_evidences` is its Python twin; a test runs both on the real driver.
RE_EVIDENCE_JSONPATH = (
    '$.data.indicators[*] ? (@.status == "triggered" || '
    '(@.status == "observed_at_level" && @.shape == "level" && @.citations.size() > 0))'
)


@dataclass(frozen=True)
class LevelSpec:
    """One declared level indicator: its id and the reference, as declared."""

    indicator_id: str
    kind: str
    reference: str


def level_specs(options: Mapping[str, Any] | None) -> dict[str, LevelSpec]:
    """The unit's declared level indicators, by id; ``{}`` when it declares none.

    The catalog has already validated every entry by the time ``run_method``
    sees the option; a malformed one here is skipped with a warning rather than
    trusted. A repeated id keeps its FIRST declaration.
    """
    raw = (options or {}).get(LEVEL_INDICATORS_OPTION)
    if not isinstance(raw, (list, tuple)):
        return {}
    out: dict[str, LevelSpec] = {}
    for item in raw:
        m = LEVEL_ENTRY_PATTERN.match(item) if isinstance(item, str) else None
        if m is None:
            logger.warning("indicator_shape.level_entry_malformed entry=%r", item)
            continue
        ind_id = m.group("id")
        if ind_id in out:
            logger.warning("indicator_shape.level_entry_repeated id=%s", ind_id)
            continue
        kind = REFERENCE_BAR if m.group("bar") is not None else REFERENCE_SERIES
        out[ind_id] = LevelSpec(ind_id, kind, item.split("=", 1)[1])
    return out


def shape_raw_indicators(raw: Any, specs: Mapping[str, LevelSpec]) -> Any:
    """Rule 1, on the model's raw ``indicators`` array, before coercion.

    A ``shape`` / ``reference`` the model wrote is stripped whether or not the
    unit declares anything — so a unit whose declaration is absent (or was
    rejected at resolve) can never persist a level observation off the
    prompt's example alone. Returns ``raw`` itself when it is not a list, or
    when nothing is declared and no entry carries either key (the pin's
    parse, byte for byte). Never mutates the input.
    """
    if not isinstance(raw, (list, tuple)):
        return raw
    if not specs and not any(
        isinstance(e, Mapping) and ("shape" in e or "reference" in e) for e in raw
    ):
        return raw
    out: list[Any] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            out.append(entry)
            continue
        shaped = {k: v for k, v in entry.items() if k not in ("shape", "reference")}
        spec = specs.get(str(entry.get("id") or ""))
        if spec is not None:
            shaped["shape"] = SHAPE_LEVEL
            shaped["reference"] = spec.reference
            if shaped.get("status") == TRIGGERED:
                shaped["status"] = OBSERVED_AT_LEVEL
        out.append(shaped)
    return out


def _signal_markers(citations: Any) -> set[int]:
    """The ``[N]`` ordinals that resolved to a collected SIGNAL (a citation
    entry carrying ``signal_id``) — never a desk-grounding block."""
    out: set[int] = set()
    for c in citations if isinstance(citations, (list, tuple)) else ():
        if not isinstance(c, Mapping) or not c.get("signal_id"):
            continue
        marker = str(c.get("marker") or "").strip()
        if marker.startswith("[") and marker.endswith("]") and marker[1:-1].isdigit():
            out.add(int(marker[1:-1]))
    return out


def demote_unsupported_levels(
    data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Rule 2: ``(data, demoted_ids)``. ``data`` is returned as given (same
    object) when nothing is demoted; otherwise a copy with the demoted entries
    set to ``not_observed`` and :data:`DEMOTIONS_DATA_KEY` naming them."""
    if not isinstance(data, Mapping):
        return (dict(data) if data is not None else None), []
    indicators = data.get("indicators")
    if not isinstance(indicators, list) or not any(
        isinstance(e, Mapping) and e.get("status") == OBSERVED_AT_LEVEL for e in indicators
    ):
        return dict(data), []
    signals = _signal_markers(data.get("citations"))
    demoted: list[str] = []
    shaped: list[Any] = []
    for entry in indicators:
        if (
            isinstance(entry, Mapping)
            and entry.get("status") == OBSERVED_AT_LEVEL
            and not any(isinstance(n, int) and n in signals for n in entry.get("citations") or ())
        ):
            demoted.append(str(entry.get("id")))
            shaped.append({**entry, "status": NOT_OBSERVED, "citations": []})
        else:
            shaped.append(entry)
    if not demoted:
        return dict(data), []
    out = dict(data)
    out["indicators"] = shaped
    out[DEMOTIONS_DATA_KEY] = demoted
    return out, demoted


def is_level_observation(entry: Any) -> bool:
    """A level-shaped indicator observed at its level, on at least one citation."""
    return (
        isinstance(entry, Mapping)
        and entry.get("status") == OBSERVED_AT_LEVEL
        and entry.get("shape") == SHAPE_LEVEL
        and isinstance(entry.get("citations"), (list, tuple))
        and len(entry["citations"]) > 0
    )


def re_evidences(entry: Any) -> bool:
    """Rule 3: does this one indicator re-evidence its read's standing status?"""
    if not isinstance(entry, Mapping):
        return False
    return str(entry.get("status") or "") == TRIGGERED or is_level_observation(entry)


def count_level_observations(indicators: Sequence[Any] | None) -> int:
    return sum(1 for e in indicators or () if is_level_observation(e))


__all__ = [
    "DEMOTIONS_DATA_KEY",
    "LevelSpec",
    "NOT_OBSERVED",
    "OBSERVED_AT_LEVEL",
    "RE_EVIDENCE_JSONPATH",
    "SHAPE_LEVEL",
    "TRIGGERED",
    "count_level_observations",
    "demote_unsupported_levels",
    "is_level_observation",
    "level_specs",
    "re_evidences",
    "shape_raw_indicators",
]
