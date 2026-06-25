# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.predicates — Starlark predicate DSL (L-104, L-242).

Predicates appear at four surfaces in descriptors:

1. ``target.scope.predicate``                — per-signal ingestion gate.
2. ``target.sources[*].filter``              — per-source pre-filter (field TBD).
3. ``analyst.subscription.targets.predicate`` — bind-time target match.
4. ``analyst.cadence.trigger``               — NATS event trigger gate.

All four share one language (Starlark via the `starlark-pyo3` Rust binding) and
one helper-function catalog (`helpers.HELPER_CATALOG`) with per-surface context
binding.

Public API
~~~~~~~~~~

``compile_predicate(source, surface, *, catalog_version=None) -> CompiledPredicate``
    Parse + resolve a predicate source; LRU-cached by `(source_hash, surface,
    catalog_version)`. Use at descriptor-registration time.

``CompiledPredicate.evaluate(ctx) -> bool``
    Build the runtime sandbox, bind helpers + ctx-derived globals, run the
    expression, return its truthy value. Raises ``PredicateBudgetExceeded`` on
    wall-clock breach; raises ``PredicateRuntimeError`` for helper exceptions.

``PredicateCompilationError``, ``PredicateBudgetExceeded``,
``PredicateRuntimeError`` — typed exceptions for the schema validators and
hot-path callers respectively.

Library + sandbox constraints per spec §3:
  - No I/O, no imports, no `while` (Starlark native).
  - No recursion: AST analysis at compile time rejects self-referential defs.
  - Predicate must be a single expression (no top-level `def`/statements).
  - Wall-clock budget: 5 ms per evaluation (SIGALRM-based; main thread only).
  - LRU cache: 10k compiled predicates default cap (~160 MB worst case).

Surface enum, helper catalog, and version are exposed for downstream
validation / registry-side surface checks.
"""

from __future__ import annotations

from .compiler import (
    CACHE_MAXSIZE,
    HELPER_CATALOG_VERSION,
    CompiledPredicate,
    PredicateSurface,
    compile_predicate,
    predicate_cache_clear,
    predicate_cache_info,
)
from .errors import (
    PredicateBudgetExceeded,
    PredicateCompilationError,
    PredicateError,
    PredicateRuntimeError,
)
from .helpers import (
    HELPER_CATALOG,
    HELPER_NAMES,
    SURFACE_CTX_CONTRACTS,
    TARGET_SCOPE_APPLICABILITY_CTX,
    helper_requirements,
    helper_unsatisfied,
    surface_helpers,
)

__all__ = [
    # Public compile + evaluate API
    "compile_predicate",
    "CompiledPredicate",
    "PredicateSurface",
    # Errors
    "PredicateError",
    "PredicateCompilationError",
    "PredicateBudgetExceeded",
    "PredicateRuntimeError",
    # Helper catalog (for introspection / surface validators)
    "HELPER_CATALOG",
    "HELPER_NAMES",
    "HELPER_CATALOG_VERSION",
    "SURFACE_CTX_CONTRACTS",
    "TARGET_SCOPE_APPLICABILITY_CTX",
    "helper_requirements",
    "helper_unsatisfied",
    "surface_helpers",
    # Cache controls (mostly for tests + ops)
    "CACHE_MAXSIZE",
    "predicate_cache_clear",
    "predicate_cache_info",
]
