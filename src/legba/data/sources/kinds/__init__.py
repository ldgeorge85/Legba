# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source-kind registry, populated by REGISTRATION instead of a closed tuple.

Before N-L the kinds were a hard-coded tuple in ``runtime/source_factory.py``;
adding a fetch method meant editing it. Now a kind is declared by a module:

* every module in THIS package is imported by :func:`declared_source_kinds`
  and calls :func:`declare_source_kind` at import. ``core.py`` declares the
  eighteen first-party kinds, in the order the old tuple listed them;
  ``portwatch.py`` is the first plug-in. A new kind = its handler module plus a
  one-line declaration module here — no edit to the factory or the actor;
* a third-party distribution may declare kinds through the
  ``legba.source_kinds`` entry-point group, each value ``module:ClassName``.

A declaration is ``(module, class)``: a bare module name is resolved under
``legba.data.sources``; a dotted one is used as given. Resolution, the kind
contract checks and the fail-loud aggregation are unchanged —
:func:`legba.runtime.source_factory.discover_source_kinds` walks the result
exactly as it walked the tuple (K-3: a declared module that does not import,
or lacks its class, or collides on ``kind``, stops boot).

The declaration modules import NO handler code (strings only), so loading
the registry costs nothing and a broken handler still fails at discovery,
where its failure is aggregated and named.
"""

from __future__ import annotations

import importlib
import pkgutil
from importlib import metadata

__all__ = ["ENTRY_POINT_GROUP", "declare_source_kind", "declared_source_kinds"]

ENTRY_POINT_GROUP = "legba.source_kinds"

_DECLARED: list[tuple[str, str]] = []


def declare_source_kind(module: str, class_name: str) -> None:
    """Declare that ``module`` holds the handler class ``class_name``.
    Idempotent for an identical declaration."""
    entry = (module, class_name)
    if entry not in _DECLARED:
        _DECLARED.append(entry)


def _entry_point_declarations() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover — metadata backend absent
        return out
    for ep in eps:
        module, _, attr = ep.value.partition(":")
        # A malformed value is passed through as-is so discovery names it.
        out.append((module.strip(), attr.strip() or ep.name))
    return out


def declared_source_kinds() -> tuple[tuple[str, str], ...]:
    """Every declared ``(module, class)``: this package's modules (``core``
    first, then the rest alphabetically), then entry points."""
    names = sorted(i.name for i in pkgutil.iter_modules(__path__))
    for name in (["core"] if "core" in names else []) + [n for n in names if n != "core"]:
        importlib.import_module(f"{__name__}.{name}")
    table = list(_DECLARED)
    for entry in _entry_point_declarations():
        if entry not in table:
            table.append(entry)
    return tuple(table)
