# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sink seam — where a handler's typed records land, by FAMILY.

A source handler yields typed records (:mod:`legba.data.sources.records`): a
:class:`~legba.data.sources._contract.Signal` is a document item, an
:class:`~legba.data.sources.records.ObservationRecord` is one point of a
series. The actor no longer knows where either goes. It asks this registry for
the sink of the record's family and hands the record over.

ADDING A FAMILY is a new module in this package that defines a sink class and
calls :func:`register_sink` at import — nothing in the actor changes.
:func:`sink_factories` imports every module of the package (fail loud, the K-3
rule: a module here that does not import is a boot failure, not a quieter
system), so a registration cannot be forgotten by a missing import line.

A sink is any class constructed as ``cls(core)`` (the owning
:class:`~legba.runtime.source_actor.SourceCore`, read through, never copied)
with a ``family`` string and a ``record_types`` tuple. Two shapes exist today:

* per-record (``document``): ``write(conn, ctx, record, dedup_stats=)``
  returns the written record or ``None``, and ``publish(records)`` fans out;
* batched (``observation``): ``accept(record)`` buffers and
  ``flush(conn) -> SinkResult`` writes at the end of the poll, because the
  change test compares a whole poll's keys against the latest vintages in one
  read.

Families that are declared but not built (register entries, dataset releases,
library works, external-coder events, media) are refused at descriptor save
(:data:`legba.data.sources.records.SEAM_FAMILIES`, SEAMS #77) — there is no
empty sink class for them to fall into.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "SinkResult",
    "SinkSet",
    "UnroutableRecord",
    "register_sink",
    "sink_factories",
]

_SINKS: dict[str, Callable[[Any], Any]] = {}
_DISCOVERED = False


class UnroutableRecord(TypeError):
    """A handler yielded a record no registered sink accepts, or a record of a
    family the source's descriptor did not declare."""


@dataclass
class SinkResult:
    """What one batched sink did with one poll's records."""

    written: int = 0
    unchanged: int = 0
    revised: int = 0
    rejected: list[str] = field(default_factory=list)
    written_ids: list[Any] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {
            "written": self.written, "unchanged": self.unchanged,
            "revised": self.revised, "rejected": len(self.rejected),
        }


def register_sink(family: str, factory: Callable[[Any], Any]) -> None:
    """Register the sink class for ``family``. A second registration of the
    same family by a DIFFERENT class is ambiguous and refused loud."""
    existing = _SINKS.get(family)
    if existing is not None and existing is not factory:
        raise ValueError(
            f"sink family {family!r} already registered by "
            f"{getattr(existing, '__name__', existing)!r}"
        )
    _SINKS[family] = factory


def sink_factories() -> dict[str, Callable[[Any], Any]]:
    """Every registered sink, after importing every module of this package."""
    global _DISCOVERED
    if not _DISCOVERED:
        for info in pkgutil.iter_modules(__path__):
            importlib.import_module(f"{__name__}.{info.name}")
        _DISCOVERED = True
    return dict(_SINKS)


class SinkSet:
    """The sinks one source routes to, built lazily per family."""

    def __init__(self, core: Any, family: str) -> None:
        self._core = core
        self.family = family
        self._built: dict[str, Any] = {}
        factories = sink_factories()
        if family not in factories:
            raise UnroutableRecord(
                f"source {core.descriptor.identity.id!r} declares sink.family="
                f"{family!r}, which has no registered sink (registered: "
                f"{sorted(factories)})"
            )

    def get(self, family: str) -> Any:
        sink = self._built.get(family)
        if sink is None:
            sink = sink_factories()[family](self._core)
            self._built[family] = sink
        return sink

    def for_record(self, record: Any) -> Any:
        """The sink for ``record`` — its type's family, which must be the
        family the descriptor declared (a source declares ONE family; a
        handler yielding another is a wiring fault, refused loud)."""
        from ...data.sources.records import record_family

        fam = record_family(record)
        if fam != self.family:
            raise UnroutableRecord(
                f"source {self._core.descriptor.identity.id!r} declares "
                f"sink.family={self.family!r} but its handler yielded a "
                f"{type(record).__name__} (family {fam!r})"
            )
        return self.get(fam)

    def batched(self) -> list[Any]:
        """The built sinks that buffer records until :meth:`flush`."""
        return [s for s in self._built.values() if hasattr(s, "flush")]
