# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ceiling on the INPUT a consult call carries — the final synthesis above all.

What the round compaction does not bound
========================================

``tool_round_compaction`` bounds each ROUND's bodies and digests older rounds,
and both are operator-tunable by env. Neither bounds the input a call actually
sends: with compaction pinned off (``LEGBA_CONSULT_KEEP_FULL_ROUNDS=1000`` on
the live box since the 2026-09-16 revert) every round's full bodies ride every
later call, and the call that writes the answer is by construction the one
carrying the most. One recorded session sent 136,833 prompt
tokens on its ninth and answering call; its trace's ``usage.input_tokens`` of
621,505 is the RUN's running sum over nine calls, which is the figure the
2026-09-28 review quoted as "the final input".

The ceiling
===========

One number, ``context_ceiling_tokens`` on the consult kind's descriptor
(:data:`DEFAULT_CONTEXT_CEILING_TOKENS` when absent, ``0`` for none), measured
by the house estimator (``_llm_budget.estimate_tokens``, chars/4) over the
system prompt plus the JSON of the message array. It is applied to EVERY call
the loop sends — on the native route any round's reply may be the answer, so
"the final synthesis input" is every round's input there — and to the forced
final and the recovery replay alike.

When the descriptor sets no ceiling (absent, or ``0``) the loop DERIVES one
(:func:`derive_ceiling`): ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` when set, else the
active route's context window less room for its largest maximum output less
:data:`DERIVED_MARGIN_TOKENS`, else the input budget's default. Consult 6cd91190 ran 19 rounds with no
ceiling into a provider refusal at 229,377 prompt tokens; a route whose window
the loop can outrun is never left unbounded. The stamp's ``ceiling_source``
says which number applied.

When the input is over, tool RESULTS are withheld, and only results:

* **oldest first** — position in the array, which is the order they arrived;
* **whole, never partial** — the body is replaced by a one-line
  placeholder; a cut body would be a different result, not a smaller one;
* **never a cited one** — a result whose body names any ref in the planner's
  cited set stays. The set is every ref the planner itself wrote in an
  assistant turn (its tool arguments, its prose, any ``cited_refs`` header
  line) plus, on the recovery path, the persisted turn's ``cited_refs``;
* **never the last round** — the results the next call reasons over.

The question, the system prompt, the seeded transcript and every assistant
turn are never touched; neither is any id, role or block order, so the
provider's call/result correlation survives the withholding. If what remains
is still over the ceiling the call goes as it is and the stamp says so — the
ceiling withholds evidence, it never refuses to answer.

Under the ceiling the input array is returned AS GIVEN (the same object), so
the call is byte-identical to a build without this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ._llm_budget import (
    CHARS_PER_TOKEN,
    DERIVED_MARGIN_TOKENS as _DERIVED_MARGIN_TOKENS,
    LLM_INPUT_TOKEN_BUDGET_ENV,
    SOURCE_INPUT_BUDGET_DEFAULT as _SOURCE_INPUT_BUDGET_DEFAULT,
    SOURCE_INPUT_BUDGET_ENV as _SOURCE_INPUT_BUDGET_ENV,
    SOURCE_MODEL_WINDOW as _SOURCE_MODEL_WINDOW,
    derived_input_budget,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

#: The descriptor knob (``ANALYST_KIND_OPTIONS["consult_on_demand"]``).
OPTION_NAME = "context_ceiling_tokens"

#: Code default: OFF. The operator reverted every capacity cut on 2026-09-16
#: (round compaction, spend ceilings), and that session's answering call read
#: 136,833 tokens, so no default that respects that revert would have fired
#: on it. The ceiling is opt-in per descriptor (``context_ceiling_tokens``);
#: 160,000 is the value to set on the Anthropic planes (200k window).
DEFAULT_CONTEXT_CEILING_TOKENS = 0

#: The data-bag / frame key the stamp rides.
STAMP_KEY = "context_compaction"

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def resolve_ceiling(options: Mapping[str, Any] | None) -> int | None:
    """The run's ceiling in estimated tokens, or ``None`` for no ceiling.

    The descriptor value arrives catalog-validated; a per-run payload value
    does not, so a malformed one falls back to the default (logged) rather
    than lifting the ceiling. ``0`` is the operator's explicit "no ceiling".
    """
    raw = (options or {}).get(OPTION_NAME)
    if raw is None:
        return DEFAULT_CONTEXT_CEILING_TOKENS or None
    try:
        if isinstance(raw, bool):
            raise ValueError("bool")
        value = int(raw)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        logger.warning(
            "consult.context_ceiling.ignored raw=%r — using the default %d",
            raw, DEFAULT_CONTEXT_CEILING_TOKENS,
        )
        return DEFAULT_CONTEXT_CEILING_TOKENS or None
    return value or None


#: ``ceiling_source`` values on the stamp and the ``render_prompt`` frame. The
#: derived ones are ``_llm_budget``'s, so the unit and consult paths name a
#: source the same way.
DERIVED_MARGIN_TOKENS = _DERIVED_MARGIN_TOKENS
SOURCE_DESCRIPTOR = "descriptor"
SOURCE_MODEL_WINDOW = _SOURCE_MODEL_WINDOW
SOURCE_INPUT_BUDGET_ENV = _SOURCE_INPUT_BUDGET_ENV
SOURCE_INPUT_BUDGET_DEFAULT = _SOURCE_INPUT_BUDGET_DEFAULT

_SOURCES_LOGGED: set[str] = set()


def _log_source_once(source: str, ceiling: int, detail: str) -> None:
    if source in _SOURCES_LOGGED:
        return
    _SOURCES_LOGGED.add(source)
    logger.warning(
        "consult.context_ceiling.derived source=%s ceiling=%d %s — the "
        "descriptor sets no %s; this ceiling keeps the loop's input inside "
        "what the route can take (logged once per source per process)",
        source, ceiling, detail, OPTION_NAME,
    )


def derive_ceiling(llm: Any) -> tuple[int, str]:
    """``(ceiling, source)`` for a run whose descriptor sets no ceiling.

    Consult 6cd91190: with no ceiling the core-plane transcript grew to
    229,377 tokens and the provider refused the call. A route whose window the
    loop can outrun never runs without a ceiling; the number is the shared
    input budget (``_llm_budget.derived_input_budget``):

    * ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` when set (an explicit override);
    * else the route's context window less room for its largest maximum
      output (at most half the window; a thinking alias's reasoning counts
      against its output) less :data:`DERIVED_MARGIN_TOKENS`;
    * else the budget's code default.

    The descriptor's ``method.llm.max_tokens`` plays no part. Which source
    applied is logged once per source and returned for the stamp.
    """
    ceiling, source = derived_input_budget(llm)
    _log_source_once(source, ceiling, f"({LLM_INPUT_TOKEN_BUDGET_ENV}="
                     f"{'set' if (os.getenv(LLM_INPUT_TOKEN_BUDGET_ENV) or '').strip() else 'unset'})")
    return ceiling, source


def _tokens(chars: int) -> int:
    """``estimate_tokens`` over a length already measured (same ceiling)."""
    return (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _dumped_len(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False))


def estimate_input_tokens(system: str, messages: Sequence[Mapping[str, Any]]) -> int:
    """The house chars/4 estimate of one call's input (system + messages)."""
    return estimate_tokens(system or "") + estimate_tokens(
        json.dumps(list(messages), default=str, ensure_ascii=False)
    )


def _is_tool_result_message(message: Mapping[str, Any]) -> bool:
    if message.get("role") == "tool":
        return True
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and any(
            isinstance(b, Mapping) and b.get("type") == "tool_result" for b in content
        )
    )


def _result_slots(
    messages: Sequence[Mapping[str, Any]],
) -> list[tuple[int, int | None, str]]:
    """``(message index, block index | None, body)`` per tool result, in order.

    OpenAI-compatible / text route: one ``role=tool`` message per result.
    Anthropic: one ``tool_result`` block per result inside a ``role=user``
    message. Non-string bodies are left alone (never produced by this loop).
    """
    slots: list[tuple[int, int | None, str]] = []
    for i, message in enumerate(messages):
        if not _is_tool_result_message(message):
            continue
        content = message.get("content")
        if isinstance(content, str):
            slots.append((i, None, content))
        elif isinstance(content, list):
            for j, block in enumerate(content):
                if (
                    isinstance(block, Mapping)
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("content"), str)
                ):
                    slots.append((i, j, block["content"]))
    return slots


def _last_round(messages: Sequence[Mapping[str, Any]]) -> set[int]:
    """Indices of the newest contiguous run of tool-result messages.

    The same definition of "a round" the round compaction uses: walking from
    the end, the first contiguous run of tool-result messages.
    """
    run: set[int] = set()
    for i in range(len(messages) - 1, -1, -1):
        if _is_tool_result_message(messages[i]):
            run.add(i)
        elif run:
            break
    return run


def planner_cited_refs(
    messages: Sequence[Mapping[str, Any]], extra: Iterable[Any] = (),
) -> set[str]:
    """Every ref the planner named in its own turns, plus ``extra``. Lowercased."""
    refs = {str(r).strip().lower() for r in extra if r}
    for message in messages:
        if message.get("role") == "assistant":
            refs.update(m.lower() for m in _UUID_RE.findall(json.dumps(
                message, default=str, ensure_ascii=False,
            )))
    return refs


def _withheld_body(ceiling: int, refs_withheld: int) -> str:
    return json.dumps({
        "compacted": True,
        "dropped": True,
        "refs_withheld": refs_withheld,
        "summary": (
            f"withheld from this call by the {ceiling:,}-token context ceiling "
            "(oldest uncited results go first); the full payload is on the "
            "run trace"
        ),
    })


def apply_context_ceiling(
    messages: Sequence[Mapping[str, Any]],
    *,
    system: str,
    ceiling: int | None,
    cited_refs: Iterable[Any] = (),
    source: str | None = None,
) -> tuple[Sequence[Mapping[str, Any]], dict[str, Any] | None]:
    """``(messages to send, stamp | None)`` — see the module docstring.

    The stamp is returned only when the input was over the ceiling; under it
    the input comes back as the SAME object with ``None``. ``source``
    (``descriptor`` / ``model_window`` / ``input_token_budget_*``)
    rides the stamp as ``ceiling_source`` when given.
    """
    if not ceiling:
        return messages, None
    system_tokens = estimate_tokens(system or "")
    chars = len(json.dumps(list(messages), default=str, ensure_ascii=False))
    before = system_tokens + _tokens(chars)
    if before <= ceiling:
        return messages, None

    protected = planner_cited_refs(messages, cited_refs)
    exempt = _last_round(messages)
    replaced: dict[tuple[int, int | None], str] = {}
    for i, j, body in _result_slots(messages):
        if system_tokens + _tokens(chars) <= ceiling:
            break
        if i in exempt:
            continue
        body_refs = {m.lower() for m in _UUID_RE.findall(body)}
        if body_refs & protected:
            continue
        placeholder = _withheld_body(ceiling, len(body_refs))
        if len(placeholder) >= len(body):
            continue  # a digest already this small — withholding saves nothing
        chars -= _dumped_len(body) - _dumped_len(placeholder)
        replaced[(i, j)] = placeholder

    out: list[Mapping[str, Any]] = list(messages)
    for (i, j), placeholder in replaced.items():
        message = out[i]
        if j is None:
            out[i] = {**message, "content": placeholder}
        else:
            blocks = list(message["content"])
            blocks[j] = {**blocks[j], "content": placeholder}
            out[i] = {**message, "content": blocks}
    after = estimate_input_tokens(system, out)
    stamp = {
        "input_tokens_before": before,
        "input_tokens_after": after,
        "results_dropped": len(replaced),
        "ceiling": ceiling,
    }
    if source:
        stamp["ceiling_source"] = source
    logger.info("consult.context_ceiling.applied %s", stamp)
    return out, stamp


def stamp_fields(stamp: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{STAMP_KEY: stamp}`` when the ceiling bit, else ``{}`` — for a frame or bag."""
    return {STAMP_KEY: dict(stamp)} if stamp else {}


__all__ = [
    "DEFAULT_CONTEXT_CEILING_TOKENS",
    "DERIVED_MARGIN_TOKENS",
    "OPTION_NAME",
    "STAMP_KEY",
    "apply_context_ceiling",
    "derive_ceiling",
    "estimate_input_tokens",
    "planner_cited_refs",
    "resolve_ceiling",
    "stamp_fields",
]
