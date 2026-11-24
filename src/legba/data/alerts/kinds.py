# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE closed list of alert kinds (L-1, single alert path).

Every alert the platform raises names one of these kinds, and
:func:`legba.data.alerts.raise_alert.raise_alert` refuses any other with
:class:`UnknownAlertKind` before it writes anything. The list was derived from
every producer in the tree (the 2026-11-13 alerting review's P1-P16 and H1-H6,
plus the kinds added since); a guard test checks that every kind has a topic
and that no producer names a kind outside this table.

R-1: the table is no longer written here. Each kind is ONE declaration module
in ``legba.data.type_registry.alert_kinds`` (its subject kind, lifecycle,
quiet window, group, topic, paging and summary); this module and
``topics.TOPIC_BY_KIND`` are derived from them, so a new kind is a new file and
not an edit to this one.

Each kind declares:

* ``subject_kind`` — what the alert is ABOUT: ``target`` (a desk / country),
  ``source``, ``analyst``, ``route`` (an LLM route or consult mode),
  ``question`` (a forecast question), ``host``, ``deploy`` or ``platform``
  (the pipeline as a whole).
* ``lifecycle`` — ``condition``: the producer opens an episode when the
  condition starts and closes it when it clears (a watchdog transition, a disk
  band); ``event``: a point fact (an escalation, a verified finding, a failed
  run). An event episode is closed by the alert log itself once nothing has
  re-raised it for ``quiet_close_hours``; a condition episode is closed by its
  producer (or, as a backstop, after ``quiet_close_hours`` with no raise —
  ``None`` means never).
* ``summary`` — one line a person reads on the Watch surface.

The page rule (:mod:`.page_rule`) decides which kinds page; this table only
says what exists.

Stdlib only: the registry's alert-log route imports it in the slim image.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from ..type_registry import default_registry as _default_registry
from ..type_registry.alert_kinds import SUBJECT_KINDS as _FAMILY_SUBJECT_KINDS
from . import topics as _topics

SubjectKind = Literal[
    "target", "source", "analyst", "route", "question", "host", "deploy", "platform",
]
SUBJECT_KINDS: tuple[str, ...] = _FAMILY_SUBJECT_KINDS
Lifecycle = Literal["condition", "event"]


class UnknownAlertKind(ValueError):
    """A raise named a kind outside the closed list (refused before any write)."""


@dataclass(frozen=True)
class AlertKind:
    name: str
    subject_kind: SubjectKind
    lifecycle: Lifecycle
    #: Hours with no raise after which the alert log closes an open episode.
    quiet_close_hours: Optional[float]
    group: str
    summary: str

    @property
    def topic(self) -> str:
        return _topics.TOPIC_BY_KIND[self.name]


def _k(name: str, subject: SubjectKind, lifecycle: Lifecycle, quiet: Optional[float],
       group: str, summary: str) -> AlertKind:
    return AlertKind(name, subject, lifecycle, quiet, group, summary)


#: The closed list, DERIVED (R-1) from the ``alert_kind`` declarations in
#: ``legba.data.type_registry.alert_kinds`` — one module per kind. Order is the
#: Watch surface's grouping order (each declaration's ``position``).
def build_kinds(registry: Any = None) -> dict[str, AlertKind]:
    """``{name: AlertKind}`` from the declarations, in declared order."""
    reg = registry or _default_registry()
    return {
        d.id: _k(
            d.id, d.attr("subject_kind"), d.attr("lifecycle"),
            d.attr("quiet_close_hours"), d.attr("group"), d.doc,
        )
        for d in reg.types("alert_kind")
    }


KINDS: Mapping[str, AlertKind] = build_kinds()

KIND_NAMES: tuple[str, ...] = tuple(KINDS)


def require_kind(name: str) -> AlertKind:
    """The kind named ``name``, or :class:`UnknownAlertKind` with the list."""
    kind = KINDS.get(str(name or "").strip())
    if kind is None:
        raise UnknownAlertKind(
            f"alert kind {name!r} is not in the closed list "
            f"(legba.data.alerts.kinds.KINDS): {', '.join(KIND_NAMES)}. "
            "Declare it (one module in legba/data/type_registry/alert_kinds/, "
            "topic included) before raising it."
        )
    return kind


__all__ = [
    "KINDS",
    "KIND_NAMES",
    "AlertKind",
    "Lifecycle",
    "SUBJECT_KINDS",
    "SubjectKind",
    "UnknownAlertKind",
    "build_kinds",
    "require_kind",
]
