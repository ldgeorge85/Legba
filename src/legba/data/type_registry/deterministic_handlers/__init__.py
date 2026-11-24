# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``deterministic_handler`` family: the LLM-free sub-handlers of the
``deterministic`` analyst kind, one declaration module each.

A declaration names its code (``implementation``), what one run produces
(``output``: ``finding`` = a row in ``analyst_outputs``; ``trace_only`` = the
real product is side-written and the run is audited in ``analyst_traces``
only) and three facts the rest of the tree used to keep in hand lists:

``requires_pool``
    The handler REFUSES LOUD without a live ``deps.pg_pool`` (it must never
    emit a zeroed clean result without reading the substrate). The generic
    empty-input test skips these and asserts they refuse.
``structural_claims``
    Its findings assert re-derivable quantities and get the deterministic
    re-derivation verify (``provenance.kinds.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS``).
    Only meaningful for ``output="finding"``.
``alerts``
    The alert kinds (``alert_kind`` family ids) the handler raises, if any.

What is DERIVED from this family (never listed by hand again):
``deterministic.SUB_HANDLERS`` and ``OUTPUT_KIND_BY_SUB_HANDLER``,
``provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` (= the ``finding``
emitters, by construction) and ``STRUCTURAL_CLAIMS_VERIFY_ANALYSTS``, the
console mirror in ``legba-ui-v3/src/lib/verdictModel.ts`` (generated), the
option catalogue's keys for handlers that declare ``input_schema`` inline,
and the handler table in ``docs/TYPES.md``.

By convention a deterministic descriptor's ``identity.id`` equals its
``options.sub_handler``, so a handler id doubles as an analyst id.
"""

from __future__ import annotations

from typing import Any

from .. import FamilySpec, TypeDecl, register_family

__all__ = [
    "FAMILY",
    "IMPLEMENTATION_PACKAGE",
    "structural_claims_ids",
    "structural_exempt_ids",
]

#: Where the handler code lives (the default an id's implementation follows).
IMPLEMENTATION_PACKAGE = "legba.data.analysts.deterministic_handlers"


def _finalize(decl: TypeDecl) -> TypeDecl:
    from dataclasses import replace

    if decl.retention is None:
        decl = replace(decl, retention="findings" if decl.output == "finding" else "traces")
    return decl


def _check(decl: TypeDecl) -> list[str]:
    problems: list[str] = []
    if decl.attr("structural_claims") and decl.output != "finding":
        problems.append("structural_claims needs output='finding' (a trace-only run has no row to verify)")
    alerts = decl.attr("alerts")
    if not isinstance(alerts, tuple) or not all(isinstance(a, str) for a in alerts):
        problems.append("alerts must be a tuple of alert kind ids")
    for flag in ("requires_pool", "structural_claims"):
        if not isinstance(decl.attr(flag), bool):
            problems.append(f"{flag} must be True or False")
    if decl.input_schema is not None and not isinstance(decl.input_schema, tuple):
        problems.append("input_schema must be a tuple of OptionSpec (or None: catalogue entry)")
    return problems


FAMILY = register_family(FamilySpec(
    name="deterministic_handler",
    doc="LLM-free sub-handlers of the deterministic analyst kind",
    attrs={
        "requires_pool": False,
        "structural_claims": False,
        "alerts": (),
    },
    outputs=("finding", "trace_only"),
    requires_implementation=True,
    check=_check,
    finalize=_finalize,
))


def _reg(registry: Any) -> Any:
    from .. import default_registry

    return registry or default_registry()


def structural_exempt_ids(registry: Any = None) -> frozenset[str]:
    """The verify-exempt structural analysts: the handlers whose one run
    writes a finding (``provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS``)."""
    return frozenset(
        d.id for d in _reg(registry).types("deterministic_handler") if d.output == "finding")


def structural_claims_ids(registry: Any = None) -> frozenset[str]:
    """The handlers whose structural claims get the re-derivation verify
    (``provenance.kinds.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS``)."""
    return frozenset(
        d.id for d in _reg(registry).types("deterministic_handler")
        if d.attr("structural_claims"))
