# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-run LLM / tool call accounting for the ``analyst_traces`` receipt (R11).

``analyst_traces`` has declared ``llm_calls`` and ``tool_calls`` (both
``jsonb NOT NULL DEFAULT '[]'``) since the baseline migration, and NOTHING has
ever written them: every row all-time carries ``[]``. The consequence is that a
receipt cannot evidence whether the run called a model at all — provenance
survived only on the ``analyst_critiques`` side-rows. This module is the
collection mechanism that fills them.

Shape of the mechanism
----------------------

A :mod:`contextvars` slot holds ONE :class:`_RunAccount` per run. The actor's
run path binds a fresh account at the top of ``AnalystActor.run`` and resets it
in the same ``finally`` that unbinds the log-correlation context; the provider
plane (:meth:`legba.data.stack.llm.base.LLMProviderHandler.chat_complete`) and
the agency plane (:meth:`legba.data.analysts.agency.agency.Agency.run_pack_tool`)
append to whatever account is bound when they are called.

ContextVars are TASK-LOCAL and copied into every task spawned underneath, which
is exactly the isolation this needs:

  * concurrent analyst actors on the same event loop each see their own account
    — no cross-actor leakage, which a module-global list would guarantee;
  * an LLM call made from a sub-task the run spawned (a ``gather`` fan-out
    inside a GATHER loop) still lands in the RUN's account, because the child
    task inherited the context at creation;
  * the provider handlers themselves are CACHED AND SHARED across every actor
    (``dapr_host._llm_handler_cache``), so per-handler state was never an
    option in the first place.

Nothing bound → every recorder is a no-op. That keeps the registry process, the
filter plane, ad-hoc scripts and the whole test suite byte-for-byte unchanged.

Failure posture
---------------

This is INSTRUMENTATION. Every public entry point swallows its own exceptions:
a broken accounting call must never fail an analyst run, and must never convert
a successful LLM call into an error. The recorders are also bounded (see
``_MAX_CALLS``) — ``analyst_traces`` has retention, but a ReAct loop with a
runaway tool budget should not be able to write an unbounded JSONB blob.

What this module deliberately does NOT collect: ``prompt_rendered``. See the
per-call ``prompt_sha256`` / ``prompt_chars`` fields instead — they evidence
WHICH prompt was sent without persisting it (a full prompt is up to the
32k-token input budget; at the live trace rate that is multi-GB/week of row
bloat, and it would additionally change the canonical receipt-hash payload,
which is a provenance-semantics decision rather than instrumentation).
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Hard cap on entries retained per run, per list. Beyond it entries are
#: DROPPED and counted — the run still records, the row stays bounded, and the
#: overflow is visible rather than silent (see :func:`current_llm_calls`, which
#: appends a final ``{"truncated": N}`` marker).
_MAX_CALLS = 200

#: Cap on any single stringified field we persist (an error class name, a
#: block cause). Nothing large is meant to reach these — the cap is a backstop.
_MAX_FIELD_CHARS = 500


@dataclass
class _RunAccount:
    """One run's collected calls. Never shared across runs."""

    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_dropped: int = 0
    tool_dropped: int = 0


_account: ContextVar[_RunAccount | None] = ContextVar(
    "legba_run_account", default=None
)


# ---------------------------------------------------------------------------
# Bind / reset — mirrors legba.runtime.logging_setup.bind_run_log_context
# ---------------------------------------------------------------------------


def bind_run_accounting() -> Token:
    """Open a fresh per-run account and return the reset token.

    Call once at the top of a run; pass the token to :func:`reset_run_accounting`
    in a ``finally``. Binding is unconditional — a nested bind (a consult run
    invoked from inside another run) simply shadows the outer account for the
    inner scope, which is the correct per-run attribution.
    """
    return _account.set(_RunAccount())


def reset_run_accounting(token: Token) -> None:
    """Restore the account bound before :func:`bind_run_accounting`."""
    try:
        _account.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover — token from another ctx
        _account.set(None)


def _flush(entries: list[dict[str, Any]], dropped: int) -> list[dict[str, Any]]:
    out = list(entries)
    if dropped:
        out.append({"truncated": dropped})
    return out


def current_llm_calls() -> list[dict[str, Any]]:
    """The LLM calls recorded under the currently-bound account.

    Returns ``[]`` when nothing is bound or nothing was called — matching the
    ``analyst_traces.llm_calls`` column default, so a deterministic (no-LLM)
    run records an empty list rather than NULL.
    """
    acct = _account.get()
    if acct is None:
        return []
    return _flush(acct.llm_calls, acct.llm_dropped)


def current_tool_calls() -> list[dict[str, Any]]:
    """The agency tool calls recorded under the currently-bound account."""
    acct = _account.get()
    if acct is None:
        return []
    return _flush(acct.tool_calls, acct.tool_dropped)


# ---------------------------------------------------------------------------
# Recorders — called from the provider / agency planes. NEVER raise.
# ---------------------------------------------------------------------------


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS]
    return value


def record_llm_call(**fields: Any) -> None:
    """Append one LLM-call record to the bound account (no-op when unbound).

    Canonical fields (all optional — the provider plane fills what it has):
    ``component_id`` (the stack ref the call was routed to, e.g.
    ``llm.primary.openai_compat``), ``subprovider``, ``model``, ``status``,
    ``duration_ms``, ``prompt_tokens``, ``completion_tokens``,
    ``reasoning_tokens``, ``total_tokens``, ``cost_estimate_usd``,
    ``finish_reason``, ``prompt_sha256``, ``prompt_chars``, ``error``, and —
    only when non-zero, so an uncached provider's receipt is unchanged —
    ``cache_read_tokens`` / ``cache_write_tokens``.
    """
    try:
        acct = _account.get()
        if acct is None:
            return
        if len(acct.llm_calls) >= _MAX_CALLS:
            acct.llm_dropped += 1
            return
        entry = {k: _clip(v) for k, v in fields.items() if v is not None}
        entry.setdefault("at", datetime.now(tz=timezone.utc).isoformat())
        acct.llm_calls.append(entry)
    except Exception:  # pragma: no cover — instrumentation must never fail a run
        logger.debug("run_accounting.record_llm_call failed", exc_info=True)


def record_tool_call(**fields: Any) -> None:
    """Append one agency tool-call record to the bound account.

    Canonical fields: ``source`` (``"agency"``), ``pack``, ``name``,
    ``admitted``, ``status``, ``block_cause``, ``duration_ms``, ``cost_usd``,
    ``units``. Tool ARGUMENTS and RESULTS are deliberately not collected — they
    are unbounded and already ledgered in ``action_pack_invocations``.
    """
    try:
        acct = _account.get()
        if acct is None:
            return
        if len(acct.tool_calls) >= _MAX_CALLS:
            acct.tool_dropped += 1
            return
        entry = {k: _clip(v) for k, v in fields.items() if v is not None}
        entry.setdefault("at", datetime.now(tz=timezone.utc).isoformat())
        acct.tool_calls.append(entry)
    except Exception:  # pragma: no cover — instrumentation must never fail a run
        logger.debug("run_accounting.record_tool_call failed", exc_info=True)


def prompt_digest(messages: Any, system: Any = None) -> tuple[str | None, int]:
    """``(sha256_hex, char_count)`` over the rendered prompt actually sent.

    The BOUNDED stand-in for ``prompt_rendered``: two scalars that let an
    auditor prove which prompt produced a finding (re-render it, hash it,
    compare) without persisting the prompt itself. Returns ``(None, 0)`` if the
    prompt can't be serialized — never raises.
    """
    try:
        blob = json.dumps(
            {"system": system, "messages": messages},
            sort_keys=True, default=str, ensure_ascii=False,
        )
    except Exception:  # pragma: no cover — defensive
        return None, 0
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), len(blob)


__all__ = [
    "bind_run_accounting",
    "reset_run_accounting",
    "current_llm_calls",
    "current_tool_calls",
    "record_llm_call",
    "record_tool_call",
    "prompt_digest",
]
