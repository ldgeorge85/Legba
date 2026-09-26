# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 — the declared option catalog for deterministic sub-handlers.

**The defect this closes.** Every deterministic handler was written to read
its thresholds out of the run ``options`` mapping
(``options.get("per_desk_cap", DEFAULT_PER_DESK_CAP)`` and ~60 siblings), but
no channel ever fed descriptor-sourced values into that mapping: the runtime
built ``options`` from scratch at fire time (``dapr_actors``) and
``MethodBlock`` was ``extra="forbid"`` with no ``options`` field. Every one of
those knobs was therefore unreachable dead config — the in-source default
always won, and several descriptors DOCUMENTED knobs they could not set.

**The mechanism.** ``method.options`` on the analyst descriptor (see
:class:`legba.data.schemas.analyst.MethodBlock`), merged into the run options
at fire time by the runtime. Descriptor-borne, so it inherits the registry's
versioning + content-hash + audit chain, and — because the runtime reads the
descriptor from its registry DB ROW, not the YAML file — it is live-editable
via ``PUT /api/v1/descriptors/analyst/{id}`` with no code edit, no schema
change and no image rebuild, exactly like the action-pack side's
``ToolSpec.config``.

**The contract, in four parts.**

1. *Defaults are byte-identical.* A descriptor with no ``method.options``
   block contributes NOTHING to the run options mapping — not a key, not a
   sentinel. Every handler's own ``options.get(key, DEFAULT)`` therefore
   resolves to the same in-source constant it always did. This module
   deliberately does **not** record each knob's default value: duplicating
   those constants here would create exactly the unsynchronized-copy problem
   the verify-floor already suffers (four drifting copies of 0.50). The
   handler's own default is the single source of truth; the catalog describes
   only the key's TYPE and admissible RANGE.

2. *Unknown keys degrade LOUDLY.* A key the running code does not declare is
   dropped, logged at WARNING, and noted on the run receipt
   (``analyst_traces.intermediate_steps``) — never silently swallowed, and
   never fatal. Fatal was rejected deliberately: registry rows outlive code,
   so a knob renamed in a later release would otherwise brick activation for
   every descriptor still carrying the old name — a fleet outage in exchange
   for a cosmetic problem.

3. *Values are validated.* A cap must be a positive int, a floor must sit in
   [0, 1], an enum must name a declared choice. A value that fails its
   invariant is dropped with the same loud-degrade treatment, so the handler
   default stands rather than a nonsense threshold taking effect.

4. *Runtime-owned keys are refused.* ``analyst_id`` / ``run_id`` /
   ``target_id`` / ``sub_handler`` and friends are provenance the runtime
   stamps; a descriptor that could overwrite them could forge lineage. They
   are rejected as ``reserved_key`` regardless of the per-handler catalog, as
   is any private ``_``-prefixed key (those are test hooks).

Merge semantics, validated invariants and loud degrade-to-default are lifted
straight from :mod:`legba.data.facts.decay`'s ``LEGBA_FACT_DECAY_CONFIG``
overlay — the one place in the tree that had already solved this problem
well. What the descriptor route adds over that JSON file is per-analyst
scope (two descriptors sharing a sub-handler can differ), versioning, and no
container recreate to change a value.

Adding a knob to a handler? Declare it here in the same commit. An
undeclared knob is not settable, which is the point: the catalog IS the
operator-facing contract, and ``tests/data_pkg/test_handler_options_x1.py``
holds it to the handlers' actual ``options.get`` call sites.

**This module is the machinery, not the data.** ``HANDLER_OPTIONS`` and
``ANALYST_KIND_OPTIONS`` — the two catalog dicts themselves, plus the
catalog-local spec constructors and the splice-in of the events/inquiry/
programs sibling catalogs — live in :mod:`legba.data.analysts.handler_options_catalog`
now, imported here and re-exported under the same names so every existing
importer keeps resolving. What stays in THIS file is ``OptionSpec``
validation, the reserved-key table, the resolve/degrade traversal and the
two ``known_*_option_names`` lookups — the part of X-1 that never grows one
entry per handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping
# ``Literal`` is re-exported (redundant self-alias, the standard idiom for an
# intentional re-export) purely so this module's public surface — everything
# `dir(handler_options)` shows minus dunders/underscores — is unchanged by
# the ``OptionKind``/``OptionSpec`` move below: pre-refactor, `Literal` was a
# module-level name here because `OptionKind = Literal[...]` was defined
# in-file. `OptionKind` itself is real, used API surface (every existing
# `OptionSpec.kind` value); `Literal` is not otherwise used in this file.
from typing import Literal as Literal

from .handler_options_base import (
    OptionKind,
    OptionSpec,
)
from .handler_options_catalog import (
    ANALYST_KIND_OPTIONS,
    HANDLER_OPTIONS,
)

logger = logging.getLogger(__name__)


__all__ = [
    "ANALYST_KIND_OPTIONS",
    "HANDLER_OPTIONS",
    "OptionReject",
    "OptionResolution",
    "OptionSpec",
    "RESERVED_OPTION_KEYS",
    "known_kind_option_names",
    "known_option_names",
    "resolve_handler_options",
    "resolve_kind_options",
]


# ---------------------------------------------------------------------------
# Reserved keys — runtime-owned, never descriptor-settable
# ---------------------------------------------------------------------------

#: Keys the runtime itself stamps into the run ``options`` mapping at fire
#: time (``dapr_actors``): analyst/target provenance, the run id, the
#: sub-handler route, the per-run agency binding, GATHER wiring, the demoted
#: LLM ref, and the critic-context lookup. A descriptor MUST NOT be able to
#: set any of them — ``analyst_id`` alone would let a descriptor write its
#: side-effect rows under another analyst's name. Rejected as
#: ``reserved_key`` ahead of any per-handler catalog check.
RESERVED_OPTION_KEYS: frozenset[str] = frozenset({
    # identity / provenance
    "analyst_id",
    "analyst_version",
    "run_id",
    "target_id",
    "target_version",
    "owner_tenant",
    # dispatch + kind wiring
    "sub_handler",
    "gather_only",
    "composition",
    "thematic_dimension",
    "contention_groups",
    "source_analyst_ids",
    # agency / GATHER plumbing
    "agency_binding",
    "gather_tool_bindings",
    "gather_web_prompt_fragments",
    "gather_write_prompt_fragments",
    # LLM plane
    "llm_ref",
    "llm_demoted",
    # critic-kind context (resolved from the analyzed analyst's descriptor)
    "analyzed_output_id",
    "analyzed_analyst_id",
    "analyzed_model",
    "allow_self_correlated",
    "rubric",
})


# ---------------------------------------------------------------------------
# Spec shape
# ---------------------------------------------------------------------------
#
# ``OptionKind`` and ``OptionSpec`` live in ``handler_options_base`` (the leaf
# both this module and ``handler_options_programs`` import from) and are
# re-exported here under the same names — see the import block above and
# that module's own docstring for why. The one pattern guard that used to
# live beside this banner (``_IDENT_PATTERN``) moved to
# ``handler_options_catalog.py`` with the catalog entries that use it.


@dataclass(frozen=True)
class OptionReject:
    """One dropped option key + why. Rides the run receipt verbatim."""

    key: str
    cause: str  # unknown_key | reserved_key | private_key | invalid_value |
    #             unknown_handler
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "cause": self.cause, "detail": self.detail}


@dataclass(frozen=True)
class OptionResolution:
    """Outcome of validating one descriptor's ``method.options`` block."""

    accepted: dict[str, Any]
    rejected: tuple[OptionReject, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.rejected)


def known_option_names(sub_handler: str) -> tuple[str, ...]:
    """Declared option names for ``sub_handler`` (empty for an unknown one)."""
    return tuple(s.name for s in HANDLER_OPTIONS.get(sub_handler, ()))


def known_kind_option_names(kind: str) -> tuple[str, ...]:
    """Declared option names for analyst ``kind`` (empty for an unknown one)."""
    return tuple(s.name for s in ANALYST_KIND_OPTIONS.get(kind, ()))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_handler_options(
    sub_handler: str | None,
    raw: Mapping[str, Any] | None,
    *,
    log_context: str = "",
) -> OptionResolution:
    """Validate a descriptor's ``method.options`` against the live catalog.

    Returns the accepted subset plus a rejection list. NEVER raises and never
    partially applies a bad value: a key either lands intact or is dropped
    whole. An empty/absent ``raw`` yields an empty resolution, which is what
    makes an options-less descriptor byte-identical to today.

    ``log_context`` (typically ``analyst_id@version``) is folded into the
    warning lines so an operator can find the offending descriptor.
    """
    return _resolve_against(
        HANDLER_OPTIONS.get(sub_handler or ""),
        raw,
        label=sub_handler,
        label_noun="sub_handler",
        unknown_cause="unknown_handler",
        log_context=log_context,
    )


def resolve_kind_options(
    kind: str | None,
    raw: Mapping[str, Any] | None,
    *,
    log_context: str = "",
) -> OptionResolution:
    """Validate a descriptor's ``method.options`` against the KIND catalog.

    The :data:`ANALYST_KIND_OPTIONS` twin of :func:`resolve_handler_options`, for
    knobs an analyst KIND's own ``run_method`` reads rather than a deterministic
    sub-handler. Identical contract in every respect — same reserved-key refusal,
    same loud degrade, same never-raises, same "an absent block is byte-identical
    to today" — differing only in which catalog the names are checked against.
    """
    return _resolve_against(
        ANALYST_KIND_OPTIONS.get(kind or ""),
        raw,
        label=kind,
        label_noun="kind",
        unknown_cause="unknown_kind",
        log_context=log_context,
    )


def _resolve_against(
    specs: tuple[OptionSpec, ...] | None,
    raw: Mapping[str, Any] | None,
    *,
    label: str | None,
    label_noun: str,
    unknown_cause: str,
    log_context: str,
) -> OptionResolution:
    """The ONE validation traversal both catalogs share.

    Extracted rather than copied: a per-catalog copy of the reserved-key refusal,
    the private-key refusal, the value validation and the loud-degrade logging is
    exactly the unsynchronized-copy problem this module's own docstring warns
    about.
    """
    if not raw:
        return OptionResolution(accepted={}, rejected=())

    accepted: dict[str, Any] = {}
    rejected: list[OptionReject] = []

    if specs is None:
        # A route this build does not register. Every key is unusable, but the
        # run still proceeds on pure defaults — the dispatcher itself will fail
        # loudly if the route is genuinely dead.
        for key in raw:
            rejected.append(
                OptionReject(
                    key=str(key),
                    cause=unknown_cause,
                    detail=(
                        f"{label_noun} {label!r} declares no option "
                        "catalog in this build"
                    ),
                )
            )
        _log(rejected, label, log_context)
        return OptionResolution(accepted={}, rejected=tuple(rejected))

    by_name = {s.name: s for s in specs}
    for key, value in raw.items():
        name = str(key)
        if name.startswith("_"):
            rejected.append(
                OptionReject(
                    name,
                    "private_key",
                    "keys starting with '_' are private runtime/test hooks",
                )
            )
            continue
        if name in RESERVED_OPTION_KEYS:
            rejected.append(
                OptionReject(
                    name,
                    "reserved_key",
                    "stamped by the runtime (identity/provenance/dispatch); a "
                    "descriptor may not override it",
                )
            )
            continue
        spec = by_name.get(name)
        if spec is None:
            rejected.append(
                OptionReject(
                    name,
                    "unknown_key",
                    (
                        f"{label} declares no such option; known: "
                        f"{sorted(by_name)}"
                    ),
                )
            )
            continue
        ok, coerced, cause = spec.validate(value)
        if not ok:
            rejected.append(
                OptionReject(name, "invalid_value", f"{cause} (got {value!r})")
            )
            continue
        accepted[name] = coerced

    _log(rejected, label, log_context)
    return OptionResolution(accepted=accepted, rejected=tuple(rejected))


def _log(
    rejected: list[OptionReject], route: str | None, log_context: str
) -> None:
    """One WARNING per dropped key — loud degrade, never silent, never fatal."""
    for rej in rejected:
        logger.warning(
            "handler_options.rejected route=%s descriptor=%s key=%s "
            "cause=%s detail=%s — the declared default stands",
            route,
            log_context or "?",
            rej.key,
            rej.cause,
            rej.detail,
        )
