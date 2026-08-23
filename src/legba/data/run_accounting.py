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

RUST-5 (2026-08-20) — ``prompt_rendered`` is now wired
-------------------------------------------------------

This module used to argue the opposite of what follows: a full prompt is up
to the 32k+-token input budget, and persisting one on EVERY call would be
multi-GB/week of row bloat. That argument was against persisting the
``llm_calls`` list's per-call prompts — it never had to be an argument
against persisting ONE prompt per trace. The decision on record is WIRE IT
(observability won), and the design below is the version of it that is
actually affordable:

  * :func:`record_prompt_rendered` OVERWRITES a single slot on the account
    (``last_prompt_rendered`` / ``last_prompt_sha256``) on every call —
    it never accumulates, so memory cost is bounded to ONE prompt's worth
    at a time regardless of how many calls (GATHER rounds, judge legs) a run
    makes, and NOTHING is written to ``llm_calls`` — that JSONB array stays
    exactly as bounded as it always was.
  * :func:`current_prompt_rendered` is read ONCE, at the same instant the
    actor flushes ``current_llm_calls()`` into the trace write (before the
    post-receipt verify/judge leg runs — see the S-4 section below), so in
    the ordinary run shape (GATHER rounds, then ONE synthesis call, then
    trace write) the captured prompt IS the synthesis call's: the last LLM
    call a run makes before its trace is written.
  * The returned text is capped at ``_MAX_PROMPT_RENDERED_CHARS`` with an
    explicit truncation marker; the returned sha256 is ALWAYS computed over
    the FULL, untruncated text, so a capped ``analyst_traces.prompt_rendered``
    is still byte-verifiable against a re-rendered prompt — the claim
    ``scripts/render_prompt_pack.py`` depends on.
  * ``prompt_sha256`` is stored in its own column, NOT folded into
    ``compute_receipt_hash``'s payload — supplementary provenance, not chain
    material, same posture as ``llm_calls``/``tool_calls``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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

#: Cap on the persisted ``analyst_traces.prompt_rendered`` text. Chosen to
#: echo the figure this module's own docstring used to cite against wiring
#: this at all (a full prompt runs up to the 32k-TOKEN input budget) while
#: actually being a small fraction of it in CHARS — enough to read what
#: shape of prompt a run sent without reintroducing the row-bloat argument
#: that blocked this. A truncated value ALWAYS carries an explicit marker
#: (see :func:`current_prompt_rendered`) — never a silent cut.
_MAX_PROMPT_RENDERED_CHARS = 32_000


@dataclass
class _RunAccount:
    """One run's collected calls. Never shared across runs."""

    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_dropped: int = 0
    tool_dropped: int = 0
    #: The MOST RECENT call's full rendered prompt + its sha256 — overwritten
    #: (never appended) on every :func:`record_prompt_rendered` call, so this
    #: never grows past one prompt's worth regardless of run length.
    last_prompt_rendered: str | None = None
    last_prompt_sha256: str | None = None


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
# RUST-5 — ``prompt_rendered`` (the LAST call's full rendered prompt + hash)
# ---------------------------------------------------------------------------


def _render_prompt_text(system: str | None, messages: Any) -> str:
    """Render ``(system, messages)`` — the ORIGINAL, pre-translation
    ``chat_complete`` args — into one readable text block.

    Provider-agnostic on purpose: this runs on the args the CALLER passed
    (before ``_translate_messages`` folds ``system`` into the wire messages
    for OpenAI-style providers, or leaves it separate for Anthropic-style
    ones), so the text is identical regardless of which provider handled the
    call. Handles multi-turn conversations (a GATHER round's accumulated
    tool-call history, the journal's narrate turn) the same as a single-shot
    call — every message in order, not just the last one.
    """
    parts: list[str] = []
    if system:
        parts.append(f"[SYSTEM]\n{system}")
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "?").upper()
        content = m.get("content")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, default=str, ensure_ascii=False)
            except Exception:  # pragma: no cover — defensive
                content = str(content)
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def record_prompt_rendered(system: str | None, messages: Any) -> None:
    """Capture one call's full rendered prompt into the bound account.

    OVERWRITES the account's single slot — this is not a log, it is "the
    most recent call's prompt," by construction. No-op when unbound (mirrors
    every other recorder in this module). Never raises.
    """
    try:
        acct = _account.get()
        if acct is None:
            return
        rendered = _render_prompt_text(system, messages)
        acct.last_prompt_rendered = rendered or None
        acct.last_prompt_sha256 = (
            hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            if rendered else None
        )
    except Exception:  # pragma: no cover — instrumentation must never fail a run
        logger.debug("run_accounting.record_prompt_rendered failed", exc_info=True)


def current_prompt_rendered() -> tuple[str | None, str | None]:
    """``(prompt_rendered, prompt_sha256)`` for the run's most recent LLM call.

    ``(None, None)`` when nothing is bound or no call has been recorded yet —
    matching the historical ``NULL`` for a deterministic (no-LLM) run.

    ``prompt_rendered`` is capped at ``_MAX_PROMPT_RENDERED_CHARS`` with an
    explicit truncation marker naming the full length and the sha256 to
    verify against; ``prompt_sha256`` is ALWAYS computed over the FULL,
    untruncated text (see :func:`record_prompt_rendered`), so a capped row is
    still byte-verifiable against a re-rendered prompt.
    """
    acct = _account.get()
    if acct is None or acct.last_prompt_rendered is None:
        return None, None
    text = acct.last_prompt_rendered
    sha = acct.last_prompt_sha256
    if len(text) > _MAX_PROMPT_RENDERED_CHARS:
        omitted = len(text) - _MAX_PROMPT_RENDERED_CHARS
        text = (
            text[:_MAX_PROMPT_RENDERED_CHARS]
            + f"\n...[TRUNCATED: {omitted} of {len(text)} chars omitted; "
              f"full sha256={sha}]"
        )
    return text, sha


# ---------------------------------------------------------------------------
# S-4 — reading the calls made AFTER the receipt was flushed
# ---------------------------------------------------------------------------
#
# The analyst run writes its ``analyst_traces`` row (and therefore snapshots
# ``current_llm_calls()``) BEFORE the faithfulness verify pass runs — it has to,
# because V-B's absence-slice check reads ``analyst_traces.input_row_refs`` for
# THIS run over the same connection. The judge's ``chat_complete`` then lands in
# the still-bound account, after the snapshot, and was simply discarded at
# ``reset_run_accounting``.
#
# That is why the judge leg appeared in no receipt: not a missing chokepoint —
# ``LLMProviderHandler.chat_complete`` accounted the call correctly all along —
# but a flush that happened one step too early. These two functions let the
# caller take a WATERMARK at flush time and read exactly the tail recorded
# after it, which is by construction the post-receipt (verify/judge) leg.


def llm_call_watermark() -> int:
    """Count of LLM calls recorded so far under the bound account.

    Pair with :func:`llm_calls_since`. Returns 0 when nothing is bound, which
    makes the unbound case degrade to "no tail" rather than raising.
    """
    acct = _account.get()
    if acct is None:
        return 0
    return len(acct.llm_calls)


def llm_calls_since(watermark: int) -> list[dict[str, Any]]:
    """LLM calls recorded after ``watermark`` (from :func:`llm_call_watermark`).

    Returns copies, so a caller stamping its own fields (e.g. the ``leg`` tag)
    cannot mutate the account. Never includes the ``{"truncated": N}`` marker —
    that belongs to the whole-account flush, not to a tail slice; a tail taken
    after the ``_MAX_CALLS`` cap is simply empty, matching the drop that
    actually happened.
    """
    acct = _account.get()
    if acct is None:
        return []
    if watermark < 0:
        watermark = 0
    return [dict(entry) for entry in acct.llm_calls[watermark:]]


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
    ``units``, and ``args`` (see :func:`redact_tool_args`). Tool RESULTS are
    still not collected — a ``substrate_read`` result can be the whole slice,
    and unlike the arguments it is reconstructable by re-running the call.
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


# ---------------------------------------------------------------------------
# Tool arguments — bounded + secret-redacted
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. ``tool_calls`` recorded that a tool ran, through which pack,
# and how it ended — but not WHAT IT WAS ASKED. That makes the tool leg of a
# receipt unfalsifiable: two `search_corpus` calls with completely different
# queries were indistinguishable in the audit record, so a finding could not be
# traced back to the evidence the analyst actually reached for.
#
# The old docstring justified the omission by saying arguments "already live in
# ``action_pack_invocations``". They do not. That table's columns are
# ``id, pack_id, pack_version, tool_name, budget_account, requested_by,
# tenant_id, cost_usd, units, outcome, job_id, occurred_at`` — verified against
# the live schema. There is no args column and never was, so the arguments were
# not recorded ANYWHERE. The receipt is the right home for them: it is
# per-run, hash-chained, and already retention-bounded at 45 days.
#
# The two hazards are real and are what the rest of this section is:
#
#   * UNBOUNDED — a ``substrate_read`` filter can carry a large id list, and
#     ``analyst_traces`` already grows +7.2k rows/day at 604 MB.
#   * SECRETS — a tool arg can carry a credential (the vault holds 20, incl.
#     ``api_key`` / ``map_key`` / ``primary_key`` / ``api_hash`` / ``session``
#     shapes), and this row is durable, hash-chained, and leaves the box via
#     ``substrate_export``. A leak here is permanent and portable.

#: Name tokens that mark a value as a credential. Matched against the key split
#: on ``_ - . /`` and camelCase, so ``api_key`` / ``apiKey`` / ``auth.token``
#: all hit while ``keyword`` and ``monkey`` do not.
_SECRET_NAME_TOKENS: frozenset[str] = frozenset({
    "key", "keys", "apikey", "token", "tokens", "secret", "secrets",
    "password", "passwd", "pwd", "pass", "credential", "credentials",
    "cred", "creds", "auth", "authorization", "session", "sessions",
    "cookie", "cookies", "bearer", "private", "signature", "salt",
})

#: Compound names that are credentials but whose tokens are individually
#: innocent. Kept separate on purpose: putting ``hash`` in the token set above
#: would also redact ``content_hash`` / ``signal_hash``, which are load-bearing
#: forensics and not secrets. Substring match, lowercased.
_SECRET_NAME_SUBSTRINGS: tuple[str, ...] = ("api_hash", "apihash")

#: Value shapes that are secrets whatever the key is called — the second net,
#: for a credential passed under an innocuous name. Deliberately narrow: broad
#: entropy heuristics would redact ids, hashes and geohashes.
_SECRET_VALUE_PREFIXES: tuple[str, ...] = ("sk-", "sk_", "bearer ", "eyj", "-----begin")

_REDACTED = "[redacted]"

#: Bounds. Sized so a typical call records in full and a pathological one still
#: fits comfortably inside a jsonb column beside the rest of the receipt.
_MAX_ARG_KEYS = 25
_MAX_ARG_DEPTH = 3
_MAX_ARG_ITEMS = 10
_MAX_ARG_VALUE_CHARS = 200
_MAX_ARGS_SERIALIZED_CHARS = 2_000


#: Word splitter for arg names. The acronym alternative comes FIRST so
#: ``API_KEY`` tokenises to {api, key} rather than to six single letters — an
#: all-caps env-style name is exactly the shape a credential arrives in, and a
#: naive "split before every capital" rule silently fails to match it.
_NAME_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+")


def _name_tokens(name: str) -> set[str]:
    """Split an arg name into comparable tokens (snake, kebab, dotted, camel).

    ``api_key`` / ``apiKey`` / ``APIKey`` / ``API_KEY`` / ``auth.token`` all
    yield the token that matters; ``keyword`` and ``monkey`` yield one token
    each and therefore do not match.
    """
    return {tok.lower() for tok in _NAME_TOKEN_RE.findall(str(name))}


def _is_secret_name(name: str) -> bool:
    lowered = str(name).lower()
    if any(frag in lowered for frag in _SECRET_NAME_SUBSTRINGS):
        return True
    return bool(_name_tokens(str(name)) & _SECRET_NAME_TOKENS)


def _is_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    probe = value.strip().lower()
    return probe.startswith(_SECRET_VALUE_PREFIXES)


def _redact_value(value: Any, depth: int) -> Any:
    if _is_secret_value(value):
        return _REDACTED
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        if len(value) > _MAX_ARG_VALUE_CHARS:
            return value[:_MAX_ARG_VALUE_CHARS] + f"…(+{len(value) - _MAX_ARG_VALUE_CHARS})"
        return value
    if depth >= _MAX_ARG_DEPTH:
        return f"[depth>{_MAX_ARG_DEPTH}]"
    if isinstance(value, dict):
        return _redact_mapping(value, depth + 1)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        kept = [_redact_value(v, depth + 1) for v in items[:_MAX_ARG_ITEMS]]
        if len(items) > _MAX_ARG_ITEMS:
            kept.append(f"…(+{len(items) - _MAX_ARG_ITEMS} more)")
        return kept
    return _redact_value(str(value), depth)


def _redact_mapping(mapping: dict[Any, Any], depth: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, (raw_key, raw_val) in enumerate(mapping.items()):
        key = str(raw_key)
        if i >= _MAX_ARG_KEYS:
            out["_truncated_keys"] = len(mapping) - _MAX_ARG_KEYS
            break
        out[key] = _REDACTED if _is_secret_name(key) else _redact_value(raw_val, depth)
    return out


def redact_tool_args(args: Any) -> dict[str, Any]:
    """A receipt-safe rendering of one pack tool's arguments.

    Redaction is by KEY NAME first (``api_key``, ``authToken``, ``session`` …)
    and by VALUE SHAPE second (``sk-``, ``Bearer ``, a JWT, a PEM header), so a
    credential passed under an innocuous name is still caught. It deliberately
    OVER-redacts: any key whose tokens include ``key`` goes, which costs the
    occasional ``sort_key`` in a diagnostic. That trade is not close — the
    downside of over-redaction is a lost debugging hint, and the downside of
    under-redaction is a live credential written into a hash-chained row that
    is retained for 45 days and leaves the box via ``substrate_export``.

    Bounded on every axis (keys, depth, list length, string length, and a final
    serialized-size backstop) so a large filter argument cannot bloat the row.
    Never raises: an un-renderable argument degrades to a marker, because this
    is instrumentation and must not fail a tool call that otherwise succeeded.
    """
    try:
        if args is None:
            return {}
        if not isinstance(args, dict):
            return {"_args": _redact_value(args, 0)}
        redacted = _redact_mapping(args, 0)
        # Backstop: many small keys can still add up. Collapse to the key NAMES,
        # which keep the call identifiable, and say how big it was.
        try:
            size = len(json.dumps(redacted, default=str, ensure_ascii=False))
        except Exception:  # pragma: no cover — defensive
            size = 0
        if size > _MAX_ARGS_SERIALIZED_CHARS:
            return {
                "_oversize_chars": size,
                "_keys": sorted(str(k) for k in list(args)[:_MAX_ARG_KEYS]),
            }
        return redacted
    except Exception:  # pragma: no cover — instrumentation must never fail a run
        logger.debug("run_accounting.redact_tool_args failed", exc_info=True)
        return {"_unrenderable": True}


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
    "current_prompt_rendered",
    "current_tool_calls",
    "llm_call_watermark",
    "llm_calls_since",
    "record_llm_call",
    "record_prompt_rendered",
    "record_tool_call",
    "redact_tool_args",
    "prompt_digest",
]
