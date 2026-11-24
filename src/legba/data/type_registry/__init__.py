# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ONE type registry: each type is one module ending in one ``declare(...)``.

WHY. The list of kinds used to live in several places that did not know each
other: a deterministic handler was a module, plus a dispatch-table entry, plus
an output-kind entry, plus a verify-exempt entry on the server and another in
the console, plus an option-catalogue entry, plus pinned test lists; an alert
kind was a row in ``alerts/kinds.py``, a row in ``alerts/topics.py`` and a row
in a doc table. Every lane that added one edited the same shared files, and
parallel lanes collided on them. (The measured cost, 2026-11-14: a
deterministic analyst plus one alert kind touched 18 files, 14 of them edits
to shared files.)

THE MECHANISM. The pattern ``data/sources/kinds/`` already proved, generalised:

* a FAMILY is a package of declaration modules (:data:`FAMILY_PACKAGES`);
* a TYPE is one module in that package that calls :func:`declare` once,
  usually as its last statement::

      from legba.data.type_registry import declare

      TYPE = declare(
          family="deterministic_handler",
          id="kev_digest",
          implementation="legba.data.analysts.deterministic_handlers.kev_digest:handle",
          output="finding",
          requires_pool=True,
          doc="New catalogue entries since the last digest, copied field by field.",
      )

* the registry walks every family package at first use, then the
  ``legba.types`` entry-point group (each value names a module that calls
  :func:`declare`), so another distribution can add types without editing
  this tree;
* anything that fails — a module that does not import, a module that
  declares nothing or twice, an unknown family or field, a missing required
  field, two declarations of one id — stops the process with EVERY failure
  named (:class:`legba.data.kind_discovery.KindDiscoveryError`). Nothing is
  ever skipped quietly: a skipped declaration is a quieter system, not a
  working one.

Every hand-maintained list of names in a migrated family is DERIVED from here
(the deterministic dispatch table, its output-kind map, the verify-exempt sets
and their console mirror, the option catalogue's keys, the alert kinds and
their topics, the doc tables), so adding a type is new files, not edits.

WHY THE DECLARATIONS DO NOT SIT INSIDE THE IMPLEMENTING PACKAGE. The
deterministic handlers import numpy / networkx / feedparser at module level,
and importing anything under ``legba.data.analysts`` imports every analyst
kind. The slim registry image has none of that and must still read the
declarations (the verify-exempt set, the alert kinds). So declaration modules
import nothing but this module (and, for handler options, the stdlib-only
``legba.data.option_catalog.handler_options_base``), and name their code by
dotted string; the code is imported only by the process that runs it.

WHAT IS NOT BUILT HERE (staged decisions): a ``record_kinds`` mirror table
that new kind-like columns would reference. It would attach at
:func:`all_types` — one row per declaration, written at registry start — and
is deliberately not built in this lane (storage decisions for record kinds
are staged; see ``docs/ADDING_A_TYPE.md``).

Stdlib only (plus the stdlib-only ``kind_discovery``): imported by the slim
registry image through ``provenance.kinds`` and ``alerts.kinds``.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import sys
import threading
from dataclasses import dataclass, field
from importlib import metadata
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..kind_discovery import DiscoveryFailure, KindDiscoveryError, raise_if_failed

__all__ = [
    "ENTRY_POINT_GROUP",
    "EVIDENCE_STANDINGS",
    "FAMILY_PACKAGES",
    "FamilySpec",
    "KindDiscoveryError",
    "RETENTION_CLASSES",
    "TypeDecl",
    "TypeRegistry",
    "all_types",
    "declare",
    "default_registry",
    "register_family",
    "types",
]

#: The families this tree declares, each with the package walked for its
#: declarations. A NEW FAMILY is a reviewed code change on purpose (it is a
#: new kind of thing the product has); a new TYPE in a family never edits this.
FAMILY_PACKAGES: Mapping[str, str] = MappingProxyType({
    "deterministic_handler": "legba.data.type_registry.deterministic_handlers",
    "alert_kind": "legba.data.type_registry.alert_kinds",
    "citation_target": "legba.data.type_registry.citation_targets",
    "principal_kind": "legba.data.type_registry.principal_kinds",
    "assertion_type": "legba.data.type_registry.assertion_types",
})

#: Third-party types: each entry point's value is a module path; importing it
#: must call :func:`declare` at least once (any family listed above).
ENTRY_POINT_GROUP = "legba.types"

#: Retention classes a type may name (the storage plan's classes; a retention
#: class says where a type's rows live and for how long — the policies
#: themselves are the ``retention_policies`` table's, not this module's).
RETENTION_CLASSES: tuple[str, ...] = (
    "findings",     # analyst_outputs rows (superseded rows kept; cold later)
    "traces",       # analyst_traces only (the run receipt; no product row)
    "alert_log",    # alert_log episodes
)

#: May a row of this type be cited as evidence? ``never`` for an alert (it is a
#: statement about the engine or a pointer, not material); ``background`` for
#: a product that may be read but never counts as a confirming source;
#: ``evidence`` for material. ``None`` on a declaration = not classified yet
#: (readers keep their own rule; nothing reads the standing in this lane).
EVIDENCE_STANDINGS: tuple[str, ...] = ("evidence", "background", "never")

_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: Fields every declaration may carry (family-specific ones go in ``attrs``).
_COMMON = frozenset({
    "family", "id", "doc", "implementation", "input_schema", "output",
    "list_fields", "retention", "evidence", "position",
})


@dataclass(frozen=True)
class TypeDecl:
    """One declared type. Immutable; equality is field equality."""

    family: str
    id: str
    #: One line a person reads in the generated doc tables.
    doc: str
    #: ``"package.module:attr"`` — the code that implements the type, imported
    #: only by the process that runs it. ``None`` for a pure-data type.
    implementation: str | None = None
    #: The input/options schema: for a handler, its tuple of ``OptionSpec``
    #: (``None`` = its entry is in the hand-kept option catalogue).
    input_schema: Any = None
    #: The output kind the type produces (family-defined vocabulary).
    output: str | None = None
    #: The light list projection (column names) a list route returns by
    #: default. ``None`` = the type serves no list route of its own.
    list_fields: tuple[str, ...] | None = None
    retention: str | None = None
    evidence: str | None = None
    #: Sort key for families whose ORDER is observable (the alert kinds are
    #: the Watch surface's grouping order). Ties break by id, so two lanes that
    #: pick the same number never collide — they sort deterministically.
    position: float | None = None
    attrs: Mapping[str, Any] = field(default_factory=dict)
    #: The module that called :func:`declare` (filled in automatically).
    declared_in: str = ""

    def attr(self, name: str, default: Any = None) -> Any:
        return self.attrs.get(name, default)

    def sort_key(self) -> tuple[float, str]:
        return (self.position if self.position is not None else float("inf"), self.id)


@dataclass(frozen=True)
class FamilySpec:
    """What a family's declarations must carry, and how they are checked.

    ``attrs`` maps each allowed family-specific field to its default
    (``REQUIRED`` = must be declared). ``check`` returns the problems with one
    declaration (empty = fine); ``finalize`` may fill derived defaults.
    """

    name: str
    doc: str
    attrs: Mapping[str, Any]
    outputs: tuple[str, ...] | None = None
    requires_implementation: bool = False
    check: Callable[[TypeDecl], list[str]] | None = None
    finalize: Callable[[TypeDecl], TypeDecl] | None = None


#: Sentinel default: the field must be declared.
REQUIRED = object()

_FAMILY_SPECS: dict[str, FamilySpec] = {}
_PENDING: list[TypeDecl] = []
_LOCK = threading.RLock()


def register_family(spec: FamilySpec) -> FamilySpec:
    """Called by a family package's ``__init__``. Idempotent for one spec."""
    with _LOCK:
        prior = _FAMILY_SPECS.get(spec.name)
        if prior is not None and prior is not spec:
            raise KindDiscoveryError([DiscoveryFailure(
                registry="type_registry", module=spec.name, reason="family_collision",
                detail="two family specs registered under one name",
            )])
        _FAMILY_SPECS[spec.name] = spec
    return spec


def declare(
    *,
    family: str,
    id: str,  # noqa: A002 — the field is called id everywhere it is read
    doc: str,
    implementation: str | None = None,
    input_schema: Any = None,
    output: str | None = None,
    list_fields: Sequence[str] | None = None,
    retention: str | None = None,
    evidence: str | None = None,
    position: float | None = None,
    **attrs: Any,
) -> TypeDecl:
    """Declare one type. Validation happens when the registry is built, so a
    bad declaration is reported together with every other bad one."""
    caller = sys._getframe(1).f_globals.get("__name__", "?")  # noqa: SLF001
    decl = TypeDecl(
        family=family, id=id, doc=doc, implementation=implementation,
        input_schema=input_schema, output=output,
        list_fields=tuple(list_fields) if list_fields is not None else None,
        retention=retention, evidence=evidence, position=position,
        attrs=MappingProxyType(dict(attrs)), declared_in=caller,
    )
    with _LOCK:
        if decl not in _PENDING:
            _PENDING.append(decl)
    return decl


# ---------------------------------------------------------------------------
# Building a registry
# ---------------------------------------------------------------------------


class TypeRegistry:
    """An immutable, validated set of declarations, grouped by family."""

    def __init__(self, decls: Iterable[TypeDecl], specs: Mapping[str, FamilySpec]) -> None:
        by_family: dict[str, list[TypeDecl]] = {name: [] for name in specs}
        for decl in decls:
            by_family.setdefault(decl.family, []).append(decl)
        self._by_family = {
            fam: tuple(sorted(items, key=TypeDecl.sort_key))
            for fam, items in by_family.items()
        }
        self.specs = MappingProxyType(dict(specs))

    def types(self, family: str) -> tuple[TypeDecl, ...]:
        if family not in self._by_family:
            raise KeyError(f"unknown type family {family!r}: {sorted(self._by_family)}")
        return self._by_family[family]

    def get(self, family: str, type_id: str) -> TypeDecl | None:
        return next((d for d in self.types(family) if d.id == type_id), None)

    def ids(self, family: str) -> tuple[str, ...]:
        return tuple(d.id for d in self.types(family))

    def all(self) -> tuple[TypeDecl, ...]:
        return tuple(d for fam in sorted(self._by_family) for d in self._by_family[fam])

    @classmethod
    def build(
        cls,
        *,
        family_packages: Mapping[str, str] = FAMILY_PACKAGES,
        extra_modules: Sequence[str] = (),
        entry_point_group: str | None = ENTRY_POINT_GROUP,
    ) -> "TypeRegistry":
        """Walk the family packages, then ``extra_modules`` (tests, fixtures),
        then the entry-point group; validate everything; raise once with every
        failure. Pure with respect to module state: re-building returns the
        same registry."""
        failures: list[DiscoveryFailure] = []
        modules: list[tuple[str, str | None]] = []  # (module, family it must declare)

        for fam, package in family_packages.items():
            try:
                pkg = importlib.import_module(package)
            except Exception as exc:  # noqa: BLE001 — aggregated and raised below
                failures.append(DiscoveryFailure(
                    "type_registry", package, "import_failed", f"{type(exc).__name__}: {exc}"))
                continue
            for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
                if info.name.startswith("_"):
                    continue
                modules.append((f"{package}.{info.name}", fam))
        for mod in extra_modules:
            modules.append((mod, None))
        if entry_point_group:
            for value in _entry_point_modules(entry_point_group, failures):
                modules.append((value, None))

        seen_modules: set[str] = set()
        for mod, fam in modules:
            if mod in seen_modules:
                continue
            seen_modules.add(mod)
            try:
                importlib.import_module(mod)
            except Exception as exc:  # noqa: BLE001
                failures.append(DiscoveryFailure(
                    "type_registry", mod, "import_failed", f"{type(exc).__name__}: {exc}"))
                continue
            with _LOCK:
                mine = [d for d in _PENDING if d.declared_in == mod]
            if fam is not None:
                if len(mine) != 1:
                    failures.append(DiscoveryFailure(
                        "type_registry", mod, "missing_contract",
                        f"a module in the {fam} package must call declare() exactly once "
                        f"(it called it {len(mine)} times)"))
                for d in mine:
                    if d.family != fam:
                        failures.append(DiscoveryFailure(
                            "type_registry", mod, "missing_contract",
                            f"declared family {d.family!r} inside the {fam!r} package"))
            elif not mine:
                failures.append(DiscoveryFailure(
                    "type_registry", mod, "missing_contract",
                    "named as a type module but declares nothing"))

        with _LOCK:
            specs = dict(_FAMILY_SPECS)
            decls = [d for d in _PENDING if d.declared_in in seen_modules]

        final: list[TypeDecl] = []
        seen_ids: dict[tuple[str, str], str] = {}
        for decl in decls:
            spec = specs.get(decl.family)
            if spec is None or decl.family not in family_packages:
                failures.append(DiscoveryFailure(
                    "type_registry", decl.declared_in, "missing_contract",
                    f"unknown family {decl.family!r} (known: {sorted(family_packages)})"))
                continue
            decl, problems = _validate(decl, spec)
            key = (decl.family, decl.id)
            if key in seen_ids:
                problems.append(
                    f"{decl.family} id {decl.id!r} is also declared by {seen_ids[key]}")
            else:
                seen_ids[key] = decl.declared_in
            for problem in problems:
                failures.append(DiscoveryFailure(
                    "type_registry", decl.declared_in, "missing_contract",
                    f"{decl.family}:{decl.id}: {problem}"))
            if not problems:
                final.append(decl)

        raise_if_failed(failures)
        return cls(final, {k: v for k, v in specs.items() if k in family_packages})


def _entry_point_modules(group: str, failures: list[DiscoveryFailure]) -> list[str]:
    try:
        eps = metadata.entry_points(group=group)
    except Exception:  # pragma: no cover — metadata backend absent
        return []
    out = []
    for ep in eps:
        module = ep.value.partition(":")[0].strip()
        if not module:
            failures.append(DiscoveryFailure(
                "type_registry", f"{group}:{ep.name}", "missing_contract",
                "entry point names no module"))
            continue
        out.append(module)
    return sorted(set(out))


def _validate(decl: TypeDecl, spec: FamilySpec) -> tuple[TypeDecl, list[str]]:
    problems: list[str] = []
    if not isinstance(decl.id, str) or not _ID.match(decl.id):
        problems.append("id must be lower_snake_case, 1-64 characters, starting with a letter")
    if not isinstance(decl.doc, str) or not decl.doc.strip():
        problems.append("doc is required: one line a person reads")
    elif "\n" in decl.doc.strip():
        problems.append("doc must be one line")
    unknown = sorted(set(decl.attrs) - set(spec.attrs))
    if unknown:
        problems.append(f"unknown fields for this family: {unknown} (allowed: {sorted(spec.attrs)})")
    attrs = dict(decl.attrs)
    for name, default in spec.attrs.items():
        if name not in attrs:
            if default is REQUIRED:
                problems.append(f"required field {name!r} is missing")
            else:
                attrs[name] = default
    if spec.requires_implementation and not decl.implementation:
        problems.append("implementation ('package.module:attr') is required")
    if decl.implementation is not None and (
        not isinstance(decl.implementation, str) or decl.implementation.count(":") != 1
    ):
        problems.append("implementation must be 'package.module:attr'")
    if spec.outputs is not None and decl.output not in spec.outputs:
        problems.append(f"output must be one of {list(spec.outputs)}, not {decl.output!r}")
    if decl.retention is not None and decl.retention not in RETENTION_CLASSES:
        problems.append(f"retention must be one of {list(RETENTION_CLASSES)}")
    if decl.evidence is not None and decl.evidence not in EVIDENCE_STANDINGS:
        problems.append(f"evidence must be one of {list(EVIDENCE_STANDINGS)}")
    decl = _replace(decl, attrs=MappingProxyType(attrs))
    if spec.finalize is not None and not problems:
        decl = spec.finalize(decl)
    if spec.check is not None and not problems:
        problems.extend(spec.check(decl))
    return decl, problems


def _replace(decl: TypeDecl, **changes: Any) -> TypeDecl:
    from dataclasses import replace

    return replace(decl, **changes)


# ---------------------------------------------------------------------------
# The process-wide registry
# ---------------------------------------------------------------------------

_DEFAULT: TypeRegistry | None = None


def default_registry() -> TypeRegistry:
    """The registry this process runs on, built once. Raises
    :class:`KindDiscoveryError` (every failure named) on the first call when
    anything is wrong — every importer of a derived table fails at start."""
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is None:
            _DEFAULT = TypeRegistry.build()
        return _DEFAULT


def types(family: str) -> tuple[TypeDecl, ...]:
    """The declared types of one family, in declared order (position, id)."""
    return default_registry().types(family)


def load_implementations(decls: Iterable[TypeDecl], *, registry: str) -> dict[str, Any]:
    """``{id: implementation object}`` for ``decls``, imported now.

    Every declaration whose module does not import, or lacks the attribute,
    or whose attribute is not callable, is collected, and the call raises ONCE
    naming all of them: the first import error itself (its type is what
    callers classify on) with every failure attached as a note, or
    :class:`KindDiscoveryError` when everything imported but a contract is
    broken. This is the step the heavy process (the runtime) takes.
    """
    failures: list[DiscoveryFailure] = []
    first_import_error: BaseException | None = None
    out: dict[str, Any] = {}
    for decl in decls:
        module_name, _, attr = str(decl.implementation or "").partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — aggregated
            first_import_error = first_import_error or exc
            failures.append(DiscoveryFailure(
                registry, module_name, "import_failed",
                f"{decl.family}:{decl.id}: {type(exc).__name__}: {exc}"))
            continue
        obj = getattr(module, attr, None)
        if obj is None or not callable(obj):
            failures.append(DiscoveryFailure(
                registry, module_name, "missing_contract",
                f"{decl.family}:{decl.id}: {attr!r} is absent or not callable"))
            continue
        out[decl.id] = obj
    if first_import_error is not None:
        # Re-raise the FIRST import failure as itself — exactly what a static
        # ``from . import (...)`` list raised before R-1 — because callers
        # classify it by type (a ``ModuleNotFoundError`` naming a third-party
        # package is an environment-limited process, e.g. the slim registry,
        # not a dead reference). Every other failure is attached as a note,
        # so the traceback still names the whole blast radius.
        first_import_error.add_note(str(KindDiscoveryError(failures)))
        raise first_import_error
    raise_if_failed(failures)
    return out


def all_types() -> tuple[TypeDecl, ...]:
    """Every declaration (where a ``record_kinds`` mirror would be written)."""
    return default_registry().all()
