# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-3 — strict kind discovery: a module named in a loader table MUST load.

Four packages walk a hand-maintained tuple of module names at boot and build a
kind registry from what imported successfully:

===================================================  ==========================
``legba.data.analysts._KIND_MODULE_NAMES``           analyst kinds
``legba.data.outputs._OUTPUT_KIND_MODULE_NAMES``     output kinds
``legba.data.discovery.registry._KIND_MODULE_NAMES`` discovery kinds
``legba.runtime.source_factory._SOURCE_MODULE_TABLE`` source kinds
===================================================  ==========================

Every one of them used to ``logger.warning(...); continue`` on a failed import
or a missing contract attribute. The consequence was not a crash — it was a
**quieter system**: the kind simply never appeared in the registry, so
descriptors naming it failed to bind, actors never fired, and the only trace
was one warning line at boot among thousands. A module rename could disable a
production analyst and every test still passed, because the tests build their
registries from the same forgiving walker.

These tuples are not a wishlist. They are a hand-written declaration that the
module exists and satisfies the kind contract. When that declaration is false,
the correct behaviour is the project's standing no-stubs rule: **fail loud**.

Aggregation matters as much as strictness. Raising on the first bad module
would make an operator fix a broken table one boot at a time; every helper
here collects all failures and raises once, so a single boot log names the
whole blast radius.

This module deliberately imports nothing from ``legba`` — all four loaders
depend on it, so any legba-level import here would create a cycle.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType


__all__ = [
    "DiscoveryFailure",
    "KindDiscoveryError",
    "import_declared_module",
    "raise_if_failed",
    "require_attrs",
]


@dataclass(frozen=True)
class DiscoveryFailure:
    """One declared module that did not satisfy its kind contract."""

    registry: str
    """Which loader table declared it, e.g. ``analysts``."""

    module: str
    """The dotted module path as resolved from the table entry."""

    reason: str
    """Machine-greppable cause: ``import_failed`` | ``missing_contract``."""

    detail: str
    """Human-readable specifics — exception text, or the absent attributes."""

    def as_line(self) -> str:
        return f"{self.registry}:{self.module} [{self.reason}] {self.detail}"


class KindDiscoveryError(RuntimeError):
    """A module declared in a kind-loader table failed to load or bind.

    Raised at boot, before any actor can bind a descriptor to a kind that
    silently is not there.
    """

    def __init__(self, failures: Sequence[DiscoveryFailure]) -> None:
        self.failures = list(failures)
        registries = sorted({f.registry for f in self.failures})
        head = (
            f"{len(self.failures)} declared kind module(s) failed to load "
            f"[{', '.join(registries)}] — a module named in a loader table is a "
            "contract, not a wishlist; fix the module or remove the entry"
        )
        super().__init__("\n".join([head] + [f"  - {f.as_line()}" for f in self.failures]))


def import_declared_module(
    registry: str,
    dotted_path: str,
    failures: list[DiscoveryFailure],
) -> ModuleType | None:
    """Import ``dotted_path``, recording (not raising) on failure.

    Returns the module, or ``None`` after appending a
    :class:`DiscoveryFailure`. Callers accumulate across the whole table and
    finish with :func:`raise_if_failed`.
    """
    try:
        return importlib.import_module(dotted_path)
    except Exception as exc:
        failures.append(DiscoveryFailure(
            registry=registry,
            module=dotted_path,
            reason="import_failed",
            detail=f"{type(exc).__name__}: {exc}",
        ))
        return None


def require_attrs(
    registry: str,
    dotted_path: str,
    module: ModuleType,
    required: Sequence[str],
    failures: list[DiscoveryFailure],
) -> bool:
    """Check the kind contract attributes are present and truthy-where-needed.

    ``required`` names attributes that must exist AND not be ``None``. Returns
    ``True`` when the module satisfies the contract; otherwise records a
    :class:`DiscoveryFailure` naming *every* missing attribute at once and
    returns ``False``.
    """
    missing = [
        name for name in required
        if getattr(module, name, None) in (None, "")
    ]
    if not missing:
        return True
    failures.append(DiscoveryFailure(
        registry=registry,
        module=dotted_path,
        reason="missing_contract",
        detail=f"absent or empty: {', '.join(missing)}",
    ))
    return False


def raise_if_failed(failures: Sequence[DiscoveryFailure]) -> None:
    """Raise :class:`KindDiscoveryError` when anything was recorded."""
    if failures:
        raise KindDiscoveryError(failures)
