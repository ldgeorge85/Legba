# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``alert_kind`` family: the closed list of alert kinds, one declaration
module each (L-1, the single alert path).

Every alert the platform raises names one of these kinds;
:func:`legba.data.alerts.raise_alert.raise_alert` refuses any other before it
writes anything. A declaration carries:

``subject_kind``
    What the alert is ABOUT (:data:`SUBJECT_KINDS`).
``lifecycle``
    ``condition`` (the producer opens and closes the episode) or ``event`` (a
    point fact the alert log closes after ``quiet_close_hours`` with no raise).
``quiet_close_hours``
    Hours with no raise after which the log closes an open episode; ``None``
    = never (a condition kind its producer closes).
``group``
    The Watch surface group (:data:`GROUP_LABELS`).
``topic``
    The outward push topic (:data:`TOPIC_NAMES`); was ``alerts/topics.py``'s
    hand table.
``paging``
    ``page_rule`` = the shipped ``alerts/page_rule.yaml`` has an entry for it;
    ``logged_only`` = it is logged and shown and the shipped rule pages nothing
    for it (paging it is a new rule version, the owner's call). A test holds
    the shipped rule and these declarations to each other.
``position``
    Its place in the Watch surface's order (ties break by id).
``doc``
    The one-line summary a person reads on the Watch surface.

DERIVED from this family: ``alerts.kinds.KINDS`` / ``KIND_NAMES``,
``alerts.topics.TOPIC_BY_KIND``, and the kind table in ``docs/ALERTING.md``.
"""

from __future__ import annotations

from .. import REQUIRED, FamilySpec, TypeDecl, register_family

__all__ = [
    "FAMILY",
    "GROUP_LABELS",
    "LIFECYCLES",
    "PAGING",
    "SUBJECT_KINDS",
    "TOPIC_NAMES",
]

SUBJECT_KINDS: tuple[str, ...] = (
    "target", "source", "analyst", "route", "question", "host", "deploy", "platform",
)
LIFECYCLES: tuple[str, ...] = ("condition", "event")
#: The outward topics (``alerts/topics.py`` names and routes them).
TOPIC_NAMES: tuple[str, ...] = ("world", "engine", "nightly")
PAGING: tuple[str, ...] = ("page_rule", "logged_only")

#: Group id → the label the docs table prints. A group is a reviewed addition
#: (it changes the Watch surface's sections); a kind in an existing group is not.
GROUP_LABELS: dict[str, str] = {
    "findings": "findings",
    "baseline": "baseline",
    "source_health": "source health",
    "analyst_failure": "analyst failure",
    "consult_failure": "consult",
    "pipeline": "pipeline",
    "forecast": "forecasts",
    "expectations": "expectations",
    "host": "host",
    "deploy": "deploy",
}


def _check(decl: TypeDecl) -> list[str]:
    problems: list[str] = []
    if decl.attr("subject_kind") not in SUBJECT_KINDS:
        problems.append(f"subject_kind must be one of {list(SUBJECT_KINDS)}")
    lifecycle = decl.attr("lifecycle")
    if lifecycle not in LIFECYCLES:
        problems.append(f"lifecycle must be one of {list(LIFECYCLES)}")
    quiet = decl.attr("quiet_close_hours")
    if quiet is not None and (isinstance(quiet, bool) or not isinstance(quiet, (int, float)) or quiet <= 0):
        problems.append("quiet_close_hours must be a positive number or None")
    if lifecycle == "event" and quiet is None:
        problems.append("an event kind must close itself: quiet_close_hours is required")
    if decl.attr("group") not in GROUP_LABELS:
        problems.append(f"group must be one of {sorted(GROUP_LABELS)} (a new group is a reviewed change)")
    if decl.attr("topic") not in TOPIC_NAMES:
        problems.append(f"topic must be one of {list(TOPIC_NAMES)}")
    if decl.attr("paging") not in PAGING:
        problems.append(f"paging must be one of {list(PAGING)}")
    if decl.position is None:
        problems.append("position is required (the Watch surface's order)")
    return problems


def _finalize(decl: TypeDecl) -> TypeDecl:
    from dataclasses import replace

    changes = {}
    if decl.retention is None:
        changes["retention"] = "alert_log"
    if decl.evidence is None:
        changes["evidence"] = "never"
    return replace(decl, **changes) if changes else decl


FAMILY = register_family(FamilySpec(
    name="alert_kind",
    doc="the closed list of alert kinds (the single alert path)",
    attrs={
        "subject_kind": REQUIRED,
        "lifecycle": REQUIRED,
        "quiet_close_hours": REQUIRED,
        "group": REQUIRED,
        "topic": REQUIRED,
        "paging": REQUIRED,
        #: Docs only: how the subject column reads when the subject kind alone
        #: would mislead (a kind whose raises carry their own subject kinds).
        "subject_note": None,
    },
    outputs=(None,),
    check=_check,
    finalize=_finalize,
))
