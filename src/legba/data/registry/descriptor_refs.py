# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-3 — descriptor string-reference extraction + resolution, one implementation.

A descriptor is data in Postgres; the things it names are code in this tree.
The join between them is a **string**: ``method.impl``,
``method.prompt_module``, ``method.sub_handler``, a source ``kind``, an
action-pack ``tools[].name``.  Nothing in Python's import graph links the two,
so a module rename or a dict-key rename cannot be caught by any importer,
linter, or type checker — only by resolving the string.

Historically each of those strings was resolved at a different layer, by
different code, and **every one of those resolvers swallowed its failure**:

* ``analysts/__init__.discover_analyst_kinds`` logged a warning and skipped —
  the kind simply vanished from the registry and no actor could bind it.
* ``analyst_deps_builder._resolve_prompt_module`` returned ``None`` on any
  exception, which the callers read as "no custom prompt declared" and
  silently substituted the kind default.  A renamed lens prompt module does
  not error; it swaps the analyst's persona.
* ``gepa._load_parent_prompt_text`` returned a ``<<missing prompt module>>``
  marker at ``logger.debug`` and then *optimized that marker as if it were a
  prompt*.

This module is the single place that knows the reference taxonomy, so the
three enforcement layers (boot reconcile, registry validation, runtime
dispatch) all agree on what "resolves" means and no fourth dialect appears.

Nothing here imports the runtime.  Resolution is pure: it imports the named
module, or consults an in-process registry of handler keys, and reports.  It
never mutates, never writes, and is safe to run against a live registry dump.

Design notes
------------

``ReferenceType`` distinguishes *shapes*, not *fields*, because the same field
carries two different shapes in the live registry today::

    prompt_module: legba.prompts.lens_diff:LENS_DIFF_SYSTEM   # MODULE_ATTR
    prompt_module: legba.prompts.meta_findings_synthesizer.v1 # MODULE

The first is a system-prompt constant read by
``analyst_deps_builder._resolve_prompt_module``; the second is a DSPy prompt
package read by ``gepa._import_prompt_module``.  ``_resolve_prompt_module``
returns ``None`` for the second **by design** (no colon ⇒ not a constant), and
that ``None`` is indistinguishable from "the module is gone" — which is
precisely the bug this module exists to make impossible.

``IMPLICIT_SUB_HANDLER`` deserves its own type.  ``dapr_actors`` falls back to
``descriptor.identity.id`` when ``method.sub_handler`` is null, so a
deterministic analyst can bind purely because its *descriptor id* happens to
equal a ``SUB_HANDLERS`` key.  That binding is real, load-bearing, and
invisible in the descriptor body — renaming the descriptor id silently
unbinds (or rebinds) the analyst.  An audit that only reads ``sub_handler``
would report such a descriptor as "no reference" and miss it entirely.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


__all__ = [
    "DescriptorReference",
    "DescriptorReferenceError",
    "ReferenceResolution",
    "ReferencePalettes",
    "ReferenceStatus",
    "ReferenceType",
    "audit_references",
    "extract_references",
    "format_failures",
    "load_palettes",
    "require_resolvable",
    "resolve_reference",
]


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


class ReferenceType(str, Enum):
    """The *shape* of a descriptor string reference, i.e. how it resolves."""

    #: ``package.module:ATTR`` — import the module, read the attribute.
    MODULE_ATTR = "module_attr"
    #: ``package.module`` — import the module, that is all.
    MODULE = "module"
    #: A key that must exist in ``deterministic.SUB_HANDLERS``.
    SUB_HANDLER = "sub_handler"
    #: A ``SUB_HANDLERS`` key supplied ONLY by the ``identity.id`` fallback.
    IMPLICIT_SUB_HANDLER = "implicit_sub_handler"
    #: A key that must exist in the discovered analyst-kind registry.
    ANALYST_KIND = "analyst_kind"
    #: A key that must exist in the discovered source-kind registry.
    SOURCE_KIND = "source_kind"
    #: An action-pack tool name that must exist in the agency tool palette.
    PACK_TOOL = "pack_tool"


class ReferenceStatus(str, Enum):
    """Outcome of resolving one reference."""

    #: Resolved to exactly what the descriptor author asked for.
    OK = "ok"
    #: The named module / key does not exist. The reference is broken.
    DEAD = "dead"
    #: The named module exists in-tree, but importing it needs a third-party
    #: dependency this PROCESS's environment doesn't carry (the registry image
    #: ships a lighter dependency set than the runtime — pycountry was the
    #: first live case). Not a dangling reference: the runtime boot gate does
    #: the real import with full deps and fails loud there. Refusing here
    #: would 422 every descriptor update whose impl chain touches a
    #: runtime-only package, from a container that never executes them.
    ENV_LIMITED = "env_limited"
    #: The name resolves, but to the wrong sort of thing (e.g. a
    #: ``prompt_module`` attribute that is not a string). The runtime would
    #: silently fall back rather than use it.
    MISMATCH = "mismatch"
    #: Resolves only via a fallback convention, not via a declared field.
    #: Load-bearing but invisible — a rename breaks it with no error.
    IMPLICIT = "implicit"


#: Statuses that mean "this reference does not do what the descriptor says".
FAILING_STATUSES: frozenset[ReferenceStatus] = frozenset(
    {ReferenceStatus.DEAD, ReferenceStatus.MISMATCH},
)


@dataclass(frozen=True)
class DescriptorReference:
    """One string in one descriptor that names something in the code tree."""

    family: str
    """``analyst`` | ``source`` | ``target`` | ``wiring`` | ``action_pack``."""

    descriptor_id: str
    version: str
    state: str
    field_path: str
    """Dotted path into the descriptor body, e.g. ``method.prompt_module``."""

    raw: str
    """The literal string as stored."""

    ref_type: ReferenceType

    def locator(self) -> str:
        """Stable one-line identity used in every error message and report."""
        return f"{self.family}:{self.descriptor_id}@{self.version}[{self.state}].{self.field_path}"


@dataclass(frozen=True)
class ReferenceResolution:
    """The verdict on one :class:`DescriptorReference`."""

    reference: DescriptorReference
    status: ReferenceStatus
    detail: str = ""
    resolved_to: str = ""

    @property
    def failing(self) -> bool:
        return self.status in FAILING_STATUSES

    def as_line(self) -> str:
        bits = [f"{self.reference.locator()} -> {self.reference.raw!r}",
                f"[{self.status.value}]"]
        if self.resolved_to:
            bits.append(f"resolved={self.resolved_to}")
        if self.detail:
            bits.append(self.detail)
        return " ".join(bits)


class DescriptorReferenceError(RuntimeError):
    """A descriptor names something that does not resolve.

    Carries every failing resolution, not just the first, so one boot or one
    validation call reports the whole blast radius rather than making the
    operator fix them one deploy at a time.
    """

    def __init__(self, failures: Sequence[ReferenceResolution], *, context: str = "") -> None:
        self.failures = list(failures)
        self.context = context
        super().__init__(format_failures(self.failures, context=context))


def format_failures(
    failures: Sequence[ReferenceResolution],
    *,
    context: str = "",
) -> str:
    """Render failing resolutions as a single multi-line, greppable message."""
    head = f"{len(failures)} descriptor reference(s) do not resolve"
    if context:
        head = f"{head} ({context})"
    lines = [head] + [f"  - {r.as_line()}" for r in failures]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _method_block(body: Mapping[str, Any]) -> Mapping[str, Any]:
    method = body.get("method")
    return method if isinstance(method, Mapping) else {}


def _classify_dotted(raw: str) -> ReferenceType:
    """A dotted path with a ``:`` names an attribute; without, a module."""
    return ReferenceType.MODULE_ATTR if ":" in raw else ReferenceType.MODULE


def extract_references(
    *,
    family: str,
    descriptor_id: str,
    version: str,
    state: str,
    body: Mapping[str, Any],
    kind: str | None = None,
) -> list[DescriptorReference]:
    """Pull every code-naming string out of one descriptor body.

    ``kind`` is the table's denormalised kind column; it is passed separately
    because ``source_descriptors`` and ``analyst_descriptors`` both carry it
    outside the body in the live schema.

    Returns references in a stable order so reports diff cleanly.
    """
    refs: list[DescriptorReference] = []

    def add(field_path: str, raw: Any, ref_type: ReferenceType) -> None:
        if isinstance(raw, str) and raw.strip():
            refs.append(DescriptorReference(
                family=family,
                descriptor_id=descriptor_id,
                version=version,
                state=state,
                field_path=field_path,
                raw=raw.strip(),
                ref_type=ref_type,
            ))

    if family == "analyst":
        method = _method_block(body)
        impl = method.get("impl")
        if isinstance(impl, str) and impl.strip():
            add("method.impl", impl, _classify_dotted(impl))

        prompt_module = method.get("prompt_module")
        if isinstance(prompt_module, str) and prompt_module.strip():
            add("method.prompt_module", prompt_module, _classify_dotted(prompt_module))

        declared_kind = kind or method.get("kind")
        if isinstance(declared_kind, str) and declared_kind.strip():
            add("method.kind", declared_kind, ReferenceType.ANALYST_KIND)

        sub_handler = method.get("sub_handler")
        if isinstance(sub_handler, str) and sub_handler.strip():
            add("method.sub_handler", sub_handler, ReferenceType.SUB_HANDLER)
        elif declared_kind == "deterministic":
            # The dapr_actors ``identity.id`` fallback IS the binding here.
            # Recording it as IMPLICIT is the whole point: an audit that only
            # reads the field would call this descriptor reference-free while
            # a rename of its id silently unbinds a live analyst.
            add("identity.id (sub_handler fallback)", descriptor_id,
                ReferenceType.IMPLICIT_SUB_HANDLER)

    elif family == "source":
        declared_kind = kind or body.get("kind") or (
            body.get("identity", {}).get("kind")
            if isinstance(body.get("identity"), Mapping) else None
        )
        add("kind", declared_kind, ReferenceType.SOURCE_KIND)

    elif family == "action_pack":
        tools = body.get("tools")
        if isinstance(tools, Iterable) and not isinstance(tools, (str, bytes)):
            for idx, tool in enumerate(tools):
                if not isinstance(tool, Mapping):
                    continue
                impl = tool.get("impl")
                if isinstance(impl, str) and impl.strip():
                    add(f"tools[{idx}].impl", impl, _classify_dotted(impl))
                else:
                    # ``impl`` is null for every live pack — the tool binds by
                    # NAME against the agency palette, so the name is the
                    # reference.
                    add(f"tools[{idx}].name", tool.get("name"),
                        ReferenceType.PACK_TOOL)

    return refs


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass
class ReferencePalettes:
    """Lazily-built in-process key registries the string references target."""

    sub_handlers: frozenset[str] = field(default_factory=frozenset)
    analyst_kinds: frozenset[str] = field(default_factory=frozenset)
    source_kinds: frozenset[str] = field(default_factory=frozenset)
    pack_tools: frozenset[str] = field(default_factory=frozenset)


def load_palettes() -> ReferencePalettes:
    """Import the live registries. Import errors here are real failures.

    Deliberately NOT cached: the audit runs once per process and a stale cache
    across a reload would be its own silent-wrong-answer bug.
    """
    from ..analysts import discover_analyst_kinds
    from ..analysts.agency.tools import default_tool_registry
    from ..analysts.deterministic import SUB_HANDLERS
    from ...runtime.source_factory import discover_source_kinds

    # ``default_tool_registry`` is the SAME builder the agency dispatch uses,
    # so "resolves here" and "resolves at ``agency._run_pack_tool``" cannot
    # drift. Auditing against ``consult_on_demand._KNOWN_TOOLS`` instead would
    # report every write/web/media tool as dead — that palette governs the
    # consult surface only.
    return ReferencePalettes(
        sub_handlers=frozenset(SUB_HANDLERS),
        analyst_kinds=frozenset(discover_analyst_kinds()),
        source_kinds=frozenset(discover_source_kinds()),
        pack_tools=frozenset(default_tool_registry().names),
    )


def _missing_dependency_name(exc: BaseException, target_module: str) -> str | None:
    """The absent THIRD-PARTY module name, or None when the target itself is missing.

    ``ModuleNotFoundError.name`` is the first unimportable component. When that
    component is the referenced module or one of its own parents, the reference
    is genuinely dangling (DEAD). When it is anything else — a dependency the
    referenced module imports — the reference resolves in-tree and only this
    process's environment is short a package (ENV_LIMITED).
    """
    if not isinstance(exc, ModuleNotFoundError) or not exc.name:
        return None
    if target_module == exc.name or target_module.startswith(exc.name + "."):
        return None
    return exc.name


def _resolve_dotted_module(path: str) -> tuple[ReferenceStatus, str, str]:
    try:
        module = importlib.import_module(path)
    except Exception as exc:
        if dep := _missing_dependency_name(exc, path):
            return (ReferenceStatus.ENV_LIMITED,
                    f"module exists in-tree; dependency {dep!r} absent in this "
                    "environment — the runtime boot gate owns the real import",
                    path)
        return ReferenceStatus.DEAD, f"import failed: {type(exc).__name__}: {exc}", ""
    return ReferenceStatus.OK, "", getattr(module, "__name__", path)


#: Sentinel distinguishing "the attribute is absent" from "its value is None".
_ABSENT = object()


def _resolve_module_attr(path: str) -> tuple[ReferenceStatus, str, str, Any]:
    """Returns ``(status, detail, resolved_to, value)``.

    The resolved *value* comes back so callers can type-check it without a
    second import — re-importing to inspect the attribute would read module
    state at a different moment than the one that produced the verdict.
    """
    mod_name, _, attr = path.partition(":")
    try:
        module = importlib.import_module(mod_name)
    except Exception as exc:
        if dep := _missing_dependency_name(exc, mod_name):
            return (ReferenceStatus.ENV_LIMITED,
                    f"module exists in-tree; dependency {dep!r} absent in this "
                    "environment — the runtime boot gate owns the real import "
                    "and the attribute check",
                    f"{mod_name}:{attr}", _ABSENT)
        return (ReferenceStatus.DEAD,
                f"import failed: {type(exc).__name__}: {exc}", "", _ABSENT)
    if not hasattr(module, attr):
        return (ReferenceStatus.DEAD,
                f"module {mod_name!r} has no attribute {attr!r}", "", _ABSENT)
    value = getattr(module, attr)
    return (ReferenceStatus.OK, "",
            f"{mod_name}:{attr}={type(value).__name__}", value)


def resolve_reference(
    ref: DescriptorReference,
    *,
    palettes: ReferencePalettes | None = None,
) -> ReferenceResolution:
    """Resolve one reference against the real code tree. Never raises."""
    pal = palettes if palettes is not None else load_palettes()

    if ref.ref_type is ReferenceType.MODULE:
        status, detail, resolved = _resolve_dotted_module(ref.raw)
        return ReferenceResolution(ref, status, detail, resolved)

    if ref.ref_type is ReferenceType.MODULE_ATTR:
        status, detail, resolved, value = _resolve_module_attr(ref.raw)
        if (
            status is ReferenceStatus.OK
            and ref.field_path == "method.prompt_module"
            and not isinstance(value, str)
        ):
            # ``_resolve_prompt_module`` only honours a *string* constant; any
            # other type is dropped and the kind default wins silently. So a
            # reference can import perfectly and still not do what it says.
            return ReferenceResolution(
                ref, ReferenceStatus.MISMATCH,
                f"attribute is {type(value).__name__}, not str — the runtime "
                "drops it and uses the kind default prompt",
                resolved,
            )
        return ReferenceResolution(ref, status, detail, resolved)

    if ref.ref_type is ReferenceType.SUB_HANDLER:
        if ref.raw in pal.sub_handlers:
            return ReferenceResolution(ref, ReferenceStatus.OK, "", ref.raw)
        return ReferenceResolution(
            ref, ReferenceStatus.DEAD,
            f"not a SUB_HANDLERS key ({len(pal.sub_handlers)} registered)",
        )

    if ref.ref_type is ReferenceType.IMPLICIT_SUB_HANDLER:
        if ref.raw in pal.sub_handlers:
            return ReferenceResolution(
                ref, ReferenceStatus.IMPLICIT,
                "binds only via the dapr_actors identity.id fallback; "
                "method.sub_handler is unset, so renaming the descriptor id "
                "unbinds this analyst with no error",
                ref.raw,
            )
        return ReferenceResolution(
            ref, ReferenceStatus.DEAD,
            "method.sub_handler is unset AND the descriptor id is not a "
            "SUB_HANDLERS key — every run raises DeterministicDispatchError",
        )

    if ref.ref_type is ReferenceType.ANALYST_KIND:
        if ref.raw in pal.analyst_kinds:
            return ReferenceResolution(ref, ReferenceStatus.OK, "", ref.raw)
        return ReferenceResolution(
            ref, ReferenceStatus.DEAD,
            f"no analyst kind module registers KIND_NAME={ref.raw!r}",
        )

    if ref.ref_type is ReferenceType.SOURCE_KIND:
        if ref.raw in pal.source_kinds:
            return ReferenceResolution(ref, ReferenceStatus.OK, "", ref.raw)
        return ReferenceResolution(
            ref, ReferenceStatus.DEAD,
            f"no source handler class declares kind={ref.raw!r}",
        )

    if ref.ref_type is ReferenceType.PACK_TOOL:
        if ref.raw in pal.pack_tools:
            return ReferenceResolution(ref, ReferenceStatus.OK, "", ref.raw)
        return ReferenceResolution(
            ref, ReferenceStatus.DEAD,
            "not in the agency tool palette — the tool is advertised to the "
            "model in the prompt but every call is rejected as unknown_tool",
        )

    raise AssertionError(f"unhandled reference type {ref.ref_type!r}")  # pragma: no cover


def audit_references(
    refs: Iterable[DescriptorReference],
    *,
    palettes: ReferencePalettes | None = None,
) -> list[ReferenceResolution]:
    """Resolve many references, sharing one palette build. Never raises."""
    pal = palettes if palettes is not None else load_palettes()
    return [resolve_reference(ref, palettes=pal) for ref in refs]


def require_resolvable(
    refs: Iterable[DescriptorReference],
    *,
    context: str = "",
    palettes: ReferencePalettes | None = None,
) -> list[ReferenceResolution]:
    """Resolve, and raise :class:`DescriptorReferenceError` on any failure.

    ``IMPLICIT`` is not a failure — it is a live, working binding — but it is
    returned so callers can log it. Making it fatal would break every
    deterministic analyst that binds by the ``identity.id`` convention today.
    """
    resolutions = audit_references(refs, palettes=palettes)
    failures = [r for r in resolutions if r.failing]
    if failures:
        raise DescriptorReferenceError(failures, context=context)
    return resolutions
