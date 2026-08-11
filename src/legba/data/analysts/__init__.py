# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
# kind modules auto-discovered by host
"""legba.data.analysts — first-party analyst-kind handler implementations.

L-170 .. L-178. Each module here registers one analyst kind against the L-102
analyst-kind contract (see ``plans/design/legba_kind_contracts.md`` §5).

Kind modules expose three things at minimum:

  * ``KIND_NAME`` — globally unique within the analyst-kind namespace.
  * ``run_method(inputs, options, deps) -> AnalystMethodResult`` — the async
    entry point the runtime calls per analyst-actor run.
  * Optional ``build_prompt_module()`` — returns the kind's DSPy module
    instance (per L-105 §2). Modules that don't use DSPy (e.g. ``deterministic``)
    omit this.

Module-level constants the host uses for dispatch:

  * ``OUTPUT_KIND`` — :class:`legba.data.provenance.kinds.OutputKind` enum
    value, picked by the host's analyst-output write path so the runtime
    writes the correct row family (FINDING / PREDICTION / SITUATION …).
    Defaults to ``FINDING`` when the module omits the constant.
  * ``READ_SLICE`` — optional ``async (conn, *, descriptor, target_filter, **kw)
    -> list[dict]`` callable.  When ``None`` the host falls back to its
    default signals-only reader.  Kinds whose subscription window crosses
    multiple targets (``cross_target_raw``, ``meta_findings_synthesizer``,
    ``cross_analyst_correlator``) provide their own reader.

The runtime host walks this package at startup via
:func:`discover_analyst_kinds` and registers each kind by ``KIND_NAME``.
Adding a kind is a single new module here plus a schema-side descriptor
registration.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Kind-handler bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KindHandler:
    """Discovered analyst-kind metadata.

    Attributes
    ----------
    kind_name:
        The kind's ``KIND_NAME`` string (the registry / descriptor key).
    run_method:
        The async entry point ``(inputs, options, deps) -> AnalystMethodResult``.
    output_kind:
        :class:`OutputKind` the runtime writes for this kind's output.
    read_slice:
        Optional per-kind substrate-slice reader, or ``None`` to fall
        back to the host's default signals reader.
    build_prompt_module:
        Optional ``() -> dspy.Module`` constructor (callers must guard with
        ``importlib.util.find_spec('dspy')`` if dspy is optional in their
        environment).
    module:
        The Python module object itself, for callers that need richer
        introspection (e.g. ``SUB_HANDLERS`` on the deterministic kind).
    """

    kind_name: str
    run_method: Callable[..., Any]
    output_kind: Any  # OutputKind — typed loosely to avoid circular import
    read_slice: Callable[..., Any] | None
    build_prompt_module: Callable[[], Any] | None
    module: Any


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


_KIND_MODULE_NAMES: tuple[str, ...] = (
    "inline_target",
    "cross_target_raw",
    "meta_findings_synthesizer",
    "cross_analyst_correlator",
    "relationship_reifier",
    "competing_hypotheses",
    "consult_on_demand",
    "deep_consult",
    "predictor",
    "deterministic",
    "critic",
    "optimizer",
    # The 11th OutputKind's producer — Legba's first-person reflective voice
    # (plan §4.8 leg 1). OUTPUT_KIND = OutputKind.JOURNAL, off-chain.
    "journal_assessor",
    # E4 — the entity de-fragmentation analyst (global META, LLM-adjudicated
    # merges). OUTPUT_KIND = TRACE_ONLY; real product = entity_profiles merges.
    "entity_researcher",
    # S-1 — the per-signal salience scorer (global META sweep, $0-plane LLM).
    # OUTPUT_KIND = TRACE_ONLY; real product = signals.salience writes.
    "signal_salience",
    # Continuity P2 — the situation TRAJECTORY ledger's one writer.
    # OUTPUT_KIND = OutputKind.SITUATION_UPDATE (a first-class GRADED claim, not
    # a receipt); side product = the append-only `situation_events` rows.
    "situation_tracker",
)


# ``journal_assessor`` is the first EXTENSION analyst kind (NOT a member of the
# closed ``AnalystKind`` enum). The descriptor REGISTRY seeds the kind-name
# validator (``ANALYST_KIND_REGISTRY``) from ``vocabulary_entries`` at start(),
# but the RUNTIME process parses descriptors locally (the deps-resolver's
# ``AnalystDescriptor.model_validate``) and never syncs vocab — so the kind must
# be registered in-code. This package is imported at runtime boot (before any
# reconcile/activation), so registering the name here makes ``model_validate``
# accept it everywhere the analyst package loads. Idempotent; built-ins no-op.
from ..schemas.analyst import register_analyst_kind as _register_analyst_kind

_register_analyst_kind("journal_assessor")
# E4 entity_researcher is likewise an EXTENSION kind (not in the closed
# AnalystKind enum) — register its identity.kind so model_validate accepts it.
_register_analyst_kind("entity_researcher")
# S-1 signal_salience is an EXTENSION kind (not in the closed AnalystKind enum).
_register_analyst_kind("signal_salience")
# Continuity P2 situation_tracker is an EXTENSION kind. Registry-side, migration
# 0184 seeds the matching `vocabulary_entries` row (the registry REPLACES its
# extension set from that table on every refresh, so in-code registration alone
# would not survive there).
_register_analyst_kind("situation_tracker")


def discover_analyst_kinds() -> dict[str, KindHandler]:
    """Walk the package, import every kind module, return a registry.

    Each kind module that exposes a ``KIND_NAME`` + ``run_method`` is
    included in the returned mapping.

    K-3: this walker used to log a warning and ``continue`` past a module
    that failed to import or lacked the contract. That is the exact shape
    that lets a rename disable a production analyst silently — the kind just
    stops appearing in the registry, ``analyst_deps_builder`` raises
    "unknown analyst kind" per run, and nothing at boot said why. Every
    module named in :data:`_KIND_MODULE_NAMES` is a declaration that it
    exists and satisfies the contract, so a false declaration now raises
    :class:`~legba.data.kind_discovery.KindDiscoveryError` — after collecting
    ALL failures, so one boot names the whole blast radius.

    Returns a dict keyed by ``KIND_NAME`` mapping to a :class:`KindHandler`
    bundle.

    Raises
    ------
    KindDiscoveryError
        Any declared module failed to import or is missing ``KIND_NAME`` /
        ``run_method``.
    """
    # Lazy import here so the package init is cheap even when callers
    # don't need the OutputKind enum.
    from ..provenance.kinds import OutputKind  # noqa: F401 — re-exported
    from ..kind_discovery import (
        DiscoveryFailure, import_declared_module, raise_if_failed, require_attrs,
    )

    registry: dict[str, KindHandler] = {}
    failures: list[DiscoveryFailure] = []
    for mod_name in _KIND_MODULE_NAMES:
        dotted = f"{__name__}.{mod_name}"
        module = import_declared_module("analysts", dotted, failures)
        if module is None:
            continue
        if not require_attrs(
            "analysts", dotted, module, ("KIND_NAME", "run_method"), failures,
        ):
            continue

        kind_name = getattr(module, "KIND_NAME")
        run_method = getattr(module, "run_method")

        registry[str(kind_name)] = KindHandler(
            kind_name=str(kind_name),
            run_method=run_method,
            output_kind=getattr(module, "OUTPUT_KIND", OutputKind.FINDING),
            read_slice=getattr(module, "READ_SLICE", None),
            build_prompt_module=getattr(module, "build_prompt_module", None),
            module=module,
        )

    raise_if_failed(failures)
    return registry


# ---------------------------------------------------------------------------
# Package-level re-exports
# ---------------------------------------------------------------------------
#
# K-3: these two blocks used to be ``try: ... except Exception: pass``, framed
# as tolerating "a partial wave". The waves landed years ago; what the bare
# ``except`` tolerates today is a typo. A failure here does not surface as an
# error — the name simply stops being importable from the package, and every
# call site that does ``from legba.data.analysts import X`` gets an ImportError
# far from the cause, or worse, a caller using ``getattr`` gets ``None``.
# Both blocks are now plain imports: they fail at import time, at the line that
# is wrong, with the real traceback.


__all__: list[str] = ["KindHandler", "discover_analyst_kinds"]

# inline_target (L-170) — re-export for the spike's existing call sites.
from .inline_target import (                                    # noqa: E402,F401
    AnalystMethodResult,
    InlineTargetRunner,
    KIND_NAME,
    LLMHandlerLike,
    build_prompt_module,
    run_method,
)

__all__.extend([
    "AnalystMethodResult",
    "InlineTargetRunner",
    "KIND_NAME",
    "LLMHandlerLike",
    "build_prompt_module",
    "run_method",
])

# Sibling kind modules — imported eagerly so the package namespace carries
# them (call sites do ``analysts.deterministic``). Any import error here is a
# real defect in that module and must not be swallowed.
_SIBLING_KIND_MODULES: tuple[str, ...] = (
    "cross_target_raw",
    "cross_analyst_correlator",
    "competing_hypotheses",
    "consult_on_demand",
    "deep_consult",
    "deterministic",
    "meta_findings_synthesizer",
    "predictor",
    "critic",
    "optimizer",
    "journal_assessor",
)

for _mod in _SIBLING_KIND_MODULES:
    importlib.import_module(f"{__name__}.{_mod}")
    __all__.append(_mod)
