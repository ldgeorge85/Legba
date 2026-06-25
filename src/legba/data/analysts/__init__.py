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
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


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
)


def discover_analyst_kinds() -> dict[str, KindHandler]:
    """Walk the package, import every kind module, return a registry.

    Each kind module that exposes a ``KIND_NAME`` + ``run_method`` is
    included in the returned mapping.  Modules that fail to import (e.g.
    a Phase 6 kind landing mid-wave) are logged and skipped — same
    defensive-import shape the package init has had since L-170.

    Returns a dict keyed by ``KIND_NAME`` mapping to a :class:`KindHandler`
    bundle.
    """
    # Lazy import here so the package init is cheap even when callers
    # don't need the OutputKind enum.
    from ..provenance.kinds import OutputKind  # noqa: F401 — re-exported

    registry: dict[str, KindHandler] = {}
    for mod_name in _KIND_MODULE_NAMES:
        try:
            module = importlib.import_module(f"{__name__}.{mod_name}")
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "analysts.discover.import_failed module=%s err=%s",
                mod_name, exc,
            )
            continue

        kind_name = getattr(module, "KIND_NAME", None)
        run_method = getattr(module, "run_method", None)
        if not kind_name or run_method is None:
            logger.warning(
                "analysts.discover.skip module=%s reason=missing_contract "
                "(KIND_NAME=%r run_method=%r)",
                mod_name, kind_name, run_method,
            )
            continue

        registry[str(kind_name)] = KindHandler(
            kind_name=str(kind_name),
            run_method=run_method,
            output_kind=getattr(module, "OUTPUT_KIND", OutputKind.FINDING),
            read_slice=getattr(module, "READ_SLICE", None),
            build_prompt_module=getattr(module, "build_prompt_module", None),
            module=module,
        )

    return registry


# ---------------------------------------------------------------------------
# Defensive-import re-exports (preserve the spike's import shape)
# ---------------------------------------------------------------------------


__all__: list[str] = ["KindHandler", "discover_analyst_kinds"]

# inline_target (L-170) — re-export for the spike's existing call sites.
try:                                                            # pragma: no cover
    from .inline_target import (                                # noqa: F401
        AnalystMethodResult,
        InlineTargetRunner,
        KIND_NAME,
        LLMHandlerLike,
        build_prompt_module,
        run_method,
    )
except Exception:                                               # pragma: no cover
    pass
else:                                                           # pragma: no cover
    __all__.extend([
        "AnalystMethodResult",
        "InlineTargetRunner",
        "KIND_NAME",
        "LLMHandlerLike",
        "build_prompt_module",
        "run_method",
    ])

# Sibling kind modules — defensive imports so a partial wave doesn't break
# the package import for already-merged kinds.
for _mod in (
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
):
    try:                                                        # pragma: no cover
        importlib.import_module(f"{__name__}.{_mod}")
    except Exception:                                           # pragma: no cover
        pass
    else:                                                       # pragma: no cover
        __all__.append(_mod)
