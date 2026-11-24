# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The closed grammar of a round spec: read references and predicates.

The design's first named risk is the spec drifting into a programming
language. This module is where that line is held: every reference and every
predicate a spec may contain is one of the forms below, parsed by a fixed
regular expression into a typed value. Nothing is evaluated as an expression.

References (``reads[].from``, ``withheld``, ``map.over``)::

    input.<name>          a run input
    item.<name>           the current item of the enclosing map (``as: name``)
    <id>                  a seat's output, or a phase's RESULT
    <id>.sealed           every sealed output inside a phase
    <id>.others           the same, minus the reader's own
    <id>.tally            a panel's tally
    <seat>.<field>        one top-level field of a seat's output

Predicates (``loop.until``, ``branch.cases[].when``, ``escalate.when``)::

    [not] gate.<seat>.passed                  every gate of the seat's last attempt
    [not] gate.<seat>.<gate>.passed           one named gate
    tally.<phase>.outcome == stands|falls|tie|no_quorum|mixed
    count(<id>.<row_type>[ where <field> == <value>]) <op> <n>
    [not] unchanged(<phase>)
    budget.remaining.calls|wall_s <op> <n>
    phase.<id>.status == valid|invalid|partial

``<op>`` is one of ``== != < <= > >=``; ``<value>`` is ``true``, ``false``,
an integer, or a bare word / quoted string. Pure; no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

IDENT = r"[a-z][a-z0-9_]*"
ROW_TYPE = rf"{IDENT}/[1-9][0-9]*"
_IDENT_RE = re.compile(rf"^{IDENT}$")
_ROW_TYPE_RE = re.compile(rf"^{ROW_TYPE}$")

#: Suffixes of ``<id>.<suffix>`` that are not field reads.
PHASE_SUFFIXES = frozenset({"sealed", "others", "tally"})

RefKind = Literal["input", "item", "node", "sealed", "others", "tally", "field"]


class GrammarError(ValueError):
    """A reference or predicate outside the closed grammar."""


def is_ident(text: str) -> bool:
    return bool(_IDENT_RE.match(text or ""))


def is_row_type(text: str) -> bool:
    return bool(_ROW_TYPE_RE.match(text or ""))


@dataclass(frozen=True)
class Ref:
    """One parsed reference. ``target`` is the input / item name or the
    seat-or-phase id; ``field`` is set only for ``kind == "field"``."""

    raw: str
    kind: RefKind
    target: str
    field: str | None = None

    @property
    def node_id(self) -> str | None:
        """The seat or phase this reference points at (None for inputs/items)."""
        return None if self.kind in ("input", "item") else self.target


def parse_ref(raw: str) -> Ref:
    text = (raw or "").strip()
    parts = text.split(".")
    if not all(is_ident(p) for p in parts) or not 1 <= len(parts) <= 2:
        raise GrammarError(f"reference {raw!r} is not one of the closed forms")
    if len(parts) == 1:
        return Ref(text, "node", parts[0])
    head, tail = parts
    if head == "input":
        return Ref(text, "input", tail)
    if head == "item":
        return Ref(text, "item", tail)
    if tail in PHASE_SUFFIXES:
        return Ref(text, tail, head)  # type: ignore[arg-type]
    return Ref(text, "field", head, tail)


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

TALLY_OUTCOMES = ("stands", "falls", "tie", "no_quorum", "mixed")
PHASE_STATUSES = ("valid", "invalid", "partial")
OPS = ("==", "!=", "<=", ">=", "<", ">")

PredicateKind = Literal["gate", "tally", "count", "unchanged", "budget", "phase_status"]

_OP = r"(==|!=|<=|>=|<|>)"
_GATE_RE = re.compile(rf"^gate\.({IDENT})(?:\.({IDENT}))?\.passed$")
_TALLY_RE = re.compile(rf"^tally\.({IDENT})\.outcome\s*==\s*({'|'.join(TALLY_OUTCOMES)})$")
_COUNT_RE = re.compile(
    rf"^count\(\s*({IDENT})\.({ROW_TYPE})"
    rf"(?:\s+where\s+({IDENT})\s*==\s*(true|false|-?[0-9]+|{IDENT}|\"[^\"]*\"|'[^']*'))?\s*\)"
    rf"\s*{_OP}\s*([0-9]+)$"
)
_UNCHANGED_RE = re.compile(rf"^unchanged\(\s*({IDENT})\s*\)$")
_BUDGET_RE = re.compile(rf"^budget\.remaining\.(calls|wall_s)\s*{_OP}\s*([0-9]+)$")
_STATUS_RE = re.compile(rf"^phase\.({IDENT})\.status\s*==\s*({'|'.join(PHASE_STATUSES)})$")


@dataclass(frozen=True)
class Predicate:
    """One parsed predicate. Fields not used by ``kind`` are ``None``."""

    raw: str
    kind: PredicateKind
    negate: bool = False
    target: str | None = None  # seat / phase id
    gate: str | None = None
    outcome: str | None = None
    row_type: str | None = None
    field: str | None = None
    value: bool | int | str | None = None
    op: str | None = None
    n: int | None = None
    resource: str | None = None  # budget: calls | wall_s


def _literal(text: str) -> bool | int | str:
    if text == "true":
        return True
    if text == "false":
        return False
    if text[:1] in ("'", '"'):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        return text


def parse_predicate(raw: str) -> Predicate:
    text = " ".join((raw or "").split())
    negate = False
    if text.startswith("not "):
        negate, text = True, text[4:].strip()
    if m := _GATE_RE.match(text):
        return Predicate(raw, "gate", negate, target=m.group(1), gate=m.group(2))
    if m := _UNCHANGED_RE.match(text):
        return Predicate(raw, "unchanged", negate, target=m.group(1))
    if negate:
        raise GrammarError(f"predicate {raw!r}: 'not' applies only to gate and unchanged")
    if m := _TALLY_RE.match(text):
        return Predicate(raw, "tally", target=m.group(1), outcome=m.group(2))
    if m := _COUNT_RE.match(text):
        value = _literal(m.group(4)) if m.group(4) is not None else None
        return Predicate(raw, "count", target=m.group(1), row_type=m.group(2),
                         field=m.group(3), value=value, op=m.group(5), n=int(m.group(6)))
    if m := _BUDGET_RE.match(text):
        return Predicate(raw, "budget", resource=m.group(1), op=m.group(2), n=int(m.group(3)))
    if m := _STATUS_RE.match(text):
        return Predicate(raw, "phase_status", target=m.group(1), outcome=m.group(2))
    raise GrammarError(f"predicate {raw!r} is not one of the closed forms")


def compare(left: int | float, op: str, right: int | float) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise GrammarError(f"operator {op!r} is not in the closed list")
