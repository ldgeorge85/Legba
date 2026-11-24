# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult TOOL SELF-REGISTRATION HOOK (design C-2a §7).

C-1b hard-wired one extra tool family into the consult loop — the nine
``workstation_drive`` actions an armed turn may present. The write plane is the
second family, and every later write class would have been a third edit to the
same shared files. This leaf generalises what C-1b wired so a tool family
declares itself ONCE and the loop, the round router and the bindings read the
declaration:

* :class:`ConsultToolProvider` — one pack's tools: its grammar (``specs``,
  merged into ``tool_rounds.TOOL_SCHEMAS`` at registration — a name collision
  raises), its agency handlers (informational; the handlers register by
  literal name on the agency ToolRegistry so the F-7 AST guard can see them),
  whether its tools WRITE (``writes`` → ``tool_rounds.is_write_tool``), when it
  is offered (``offered(flags)`` over the turn's :class:`RunFlags`), the prompt
  fragment an offered turn appends, the run-scoped gate it opens, the refusal
  a call on a turn that did not offer it gets, and the words an offered turn
  with no binding raises (never a silent plain answer).
* :func:`register_consult_tools` — the one entry point.

The loop's rule is the C-1a ``DRIVE_TOOLS`` rule generalised: a round's
built-in reads run concurrently, then every provider tool runs SERIALLY in the
order the model emitted it, through the provider's gate.

A leaf: it imports only ``tool_rounds`` (for the schema registry). Providers
register from their own modules (``consult_case`` for the drive pack,
``consult_write_gate`` for the write pack); :func:`load_providers` imports
them so a reader of :func:`providers` sees every family.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..stack.llm.tool_rounds import ToolSpec, register_tool_schema

__all__ = [
    "ConsultToolProvider",
    "RunFlags",
    "binding_for",
    "RunGate",
    "load_providers",
    "offered_providers",
    "provider_for_tool",
    "provider_tool_names",
    "providers",
    "register_consult_tools",
]


@dataclass(frozen=True)
class RunFlags:
    """What the operator armed on THIS turn (each threaded per request)."""

    present_case: bool = False
    allow_writes: bool = False

    @classmethod
    def from_request(cls, first: Mapping[str, Any]) -> "RunFlags":
        return cls(
            present_case=bool(first.get("present_case")),
            allow_writes=bool(first.get("allow_writes")),
        )


class RunGate(Protocol):
    """A provider's run-scoped gate (the case gate, the write gate)."""

    async def run(
        self, tool: str, args: Mapping[str, Any], round_no: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def harvest(self, round_no: int, calls: list[Mapping[str, Any]], results: list[Any]) -> None: ...


#: ``(binding, *, record, first, spend) -> RunGate``
OpenGate = Callable[..., Any]
#: ``(binding | None, tool) -> (result, meta)`` — a call on a turn that did not offer it.
Refuse = Callable[[Any, str], Awaitable[tuple[dict[str, Any], dict[str, Any]]]]


@dataclass(frozen=True)
class ConsultToolProvider:
    """One consult tool family (one action pack)."""

    pack_id: str
    specs: tuple[ToolSpec, ...]
    handlers: Mapping[str, Any]
    writes: bool
    offered: Callable[[RunFlags], bool]
    #: ``str`` or ``(binding) -> str`` (the drive block reads the live pack).
    prompt_fragment: str | Callable[[Any], str]
    open_gate: OpenGate
    refuse: Refuse
    unavailable: str
    #: The legacy ``ConsultOnDemandDeps`` attribute that carries this pack's
    #: binding when ``deps.provider_bindings`` does not (C-1b's ``drive_binding``).
    deps_attr: str = ""
    tool_names: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_names", tuple(s.name for s in self.specs))

    def fragment(self, binding: Any) -> str:
        frag = self.prompt_fragment
        return frag(binding) if callable(frag) else str(frag)


_PROVIDERS: dict[str, ConsultToolProvider] = {}


def register_consult_tools(provider: ConsultToolProvider) -> ConsultToolProvider:
    """Register one family. Idempotent for the same object; a different
    provider under a taken pack id, or a tool name another provider owns, raises."""
    existing = _PROVIDERS.get(provider.pack_id)
    if existing is not None:
        if existing is provider or existing == provider:
            return existing
        raise ValueError(f"consult tool provider {provider.pack_id!r} is already registered")
    for other in _PROVIDERS.values():
        clash = set(other.tool_names) & set(provider.tool_names)
        if clash:
            raise ValueError(
                f"tool name(s) {sorted(clash)} already belong to provider {other.pack_id!r}"
            )
    if set(provider.handlers) != set(provider.tool_names):
        raise ValueError(f"provider {provider.pack_id!r}: handlers do not match its specs")
    for spec in provider.specs:
        register_tool_schema(spec, writes=provider.writes)
    _PROVIDERS[provider.pack_id] = provider
    return provider


def load_providers() -> None:
    """Import every provider module (each registers at import)."""
    from . import consult_case  # noqa: F401 — workstation_drive
    from . import consult_write_gate  # noqa: F401 — consult_write


def binding_for(deps: Any, pack_id: str) -> Any | None:
    """The per-analyst binding of ``pack_id`` on a deps bundle, or None."""
    bound = getattr(deps, "provider_bindings", None) or {}
    if bound.get(pack_id) is not None:
        return bound[pack_id]
    p = _PROVIDERS.get(pack_id)
    return getattr(deps, p.deps_attr, None) if p is not None and p.deps_attr else None


def providers() -> tuple[ConsultToolProvider, ...]:
    load_providers()
    return tuple(_PROVIDERS.values())


def provider_for_tool(name: str) -> ConsultToolProvider | None:
    for p in providers():
        if name in p.tool_names:
            return p
    return None


def provider_tool_names() -> frozenset[str]:
    return frozenset(n for p in providers() for n in p.tool_names)


def offered_providers(flags: RunFlags) -> list[ConsultToolProvider]:
    return [p for p in providers() if p.offered(flags)]
