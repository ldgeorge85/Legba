# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The meta_findings_synthesizer entry-point surface — deps Protocol + Runner.

V3/P2 extraction: ``MetaFindingsDeps`` and
``MetaFindingsSynthesizerRunner`` were the entry-point block of
:mod:`.meta_findings_synthesizer`, lifted verbatim when the P2 event-citation
wiring (the optional ``pg`` param on both) pushed that module past its
module-size ceiling. The synthesizer imports both names back ONE WAY and
re-exports them, so ``synth.MetaFindingsDeps`` /
``synth.MetaFindingsSynthesizerRunner`` resolve unchanged for every
importer and test. The Runner's ``__call__`` imports ``_run`` DEFERRED —
the leaf cannot import the synthesizer at module level without closing a
cycle, and a deferred import inside the call is the house idiom.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike


# ---------------------------------------------------------------------------
# Deps surface — LLM port only (no substrate side-deps; the runtime
# materializes inputs before calling run_method, same as the other kinds).
# ---------------------------------------------------------------------------


@runtime_checkable
class MetaFindingsDeps(Protocol):
    """Minimum dep surface ``run_method`` needs.

    The runtime constructs this from ``StandardDeps`` (typically a small
    adapter that surfaces ``deps.extras['llm']``). A plain object with an
    ``llm`` attribute conforming to
    :class:`legba.runtime.analyst_method.LLMHandlerLike` satisfies it;
    tests use a stub.
    """

    llm: LLMHandlerLike

    # V3/P2 — OPTIONAL substrate pool/conn for the [[event:<uuid>]] citation
    # expansion (LEGBA_EVENT_CITATIONS). Accessed via ``getattr(deps, "pg",
    # None)`` so llm-only carriers (every pre-P2 test stub) conform unchanged.
    pg: Any | None = None


# ---------------------------------------------------------------------------
# Runner — wires the synth LLM call together
# ---------------------------------------------------------------------------


class MetaFindingsSynthesizerRunner:
    """Callable conforming to the runtime's ``AnalystRunFn`` shape.

    Constructed once per analyst actor; the runtime injects a configured
    LLM handler. Each call makes one chat_complete invocation and returns
    one second-order :class:`FindingPayload`.

    Signature parity with ``InlineTargetRunner`` / ``CrossTargetRawRunner``
    is intentional — the actor layer in :mod:`legba.runtime.dapr_actors`
    treats them interchangeably.
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system_prompt: str | None = None,
        # V3/P2 — optional substrate pool/conn for the event-citation
        # expansion (LEGBA_EVENT_CITATIONS).
        pg: Any = None,
    ) -> None:
        # Deferred — the constants live on the synthesizer, which imports
        # THIS leaf back; a module-level import would close the cycle.
        from .meta_findings_synthesizer import (
            DEFAULT_MAX_TOKENS,
            DEFAULT_TEMPERATURE,
            _SYSTEM_PROMPT,
        )
        self._llm = llm
        self._max_tokens = (
            DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        )
        self._temperature = (
            DEFAULT_TEMPERATURE if temperature is None else temperature
        )
        self._system_prompt = system_prompt or _SYSTEM_PROMPT
        self._pg = pg

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        # Deferred — same cycle note as ``__init__``.
        from .meta_findings_synthesizer import _run
        return await _run(
            inputs,
            options,
            llm=self._llm,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            system_prompt=self._system_prompt,
            pg=self._pg,
        )


