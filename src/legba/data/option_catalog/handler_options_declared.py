# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Handler options DECLARED with the handler (R-1), spliced into the catalogue.

A deterministic handler declared in ``legba.data.type_registry`` may carry its
knobs inline (``input_schema=(OptionSpec(...), ...)``) instead of an entry in
the hand-kept catalogue; this module appends those entries to
``HANDLER_OPTIONS`` after the catalogue is built, so a NEW handler never edits
the catalogue file. The existing entries stay where they are (byte-identical,
pinned by ``tests/data_pkg/test_handler_options_catalog_split.py``).

A handler that has BOTH a catalogue entry and an inline schema is refused at
import: two homes for one handler's knobs is the drift this replaces.

Leaf rules hold: the type registry and the declaration modules import only the
stdlib and ``handler_options_base``.
"""

from __future__ import annotations

from typing import Any, MutableMapping

from ..type_registry import default_registry as _default_registry
from .handler_options_base import OptionSpec

__all__ = ["DeclaredOptionsCollision", "declared_handler_option_ids", "splice_declared"]


class DeclaredOptionsCollision(RuntimeError):
    """A handler's knobs are declared in two places."""


def declared_handler_option_ids(registry: Any = None) -> tuple[str, ...]:
    """The handler ids whose knobs come from their declaration."""
    reg = registry or _default_registry()
    return tuple(d.id for d in reg.types("deterministic_handler") if d.input_schema is not None)


def splice_declared(
    catalog: MutableMapping[str, tuple[OptionSpec, ...]], registry: Any = None,
) -> None:
    """Append every declared handler's inline options to ``catalog`` in place."""
    reg = registry or _default_registry()
    collisions: list[str] = []
    for decl in reg.types("deterministic_handler"):
        if decl.input_schema is None:
            continue
        if decl.id in catalog:
            collisions.append(decl.id)
            continue
        bad = [s for s in decl.input_schema if not isinstance(s, OptionSpec)]
        if bad:
            raise TypeError(
                f"deterministic_handler:{decl.id} input_schema holds non-OptionSpec "
                f"entries: {bad!r}")
        catalog[decl.id] = tuple(decl.input_schema)
    if collisions:
        raise DeclaredOptionsCollision(
            "handler options declared both in the catalogue and in the handler's "
            f"declaration: {sorted(collisions)} — keep one")
