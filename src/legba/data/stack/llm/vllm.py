# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""vLLM / the core model LLM provider handler (L-120).

vLLM serves an OpenAI-compatible API; the self-hosted core model
endpoint at `https://llm.example.internal/v1` is one such deployment
behind the OpenAI Chat Completions wire. This handler reuses 90% of the
OpenAI handler's translation layer and overrides the bits that differ:

  * Self-hosted ⇒ zero-cost; `PRICE_TABLE` is empty by default so cost
    rolls up to $0.00 in the budget UI. Operators can override per
    deployment if they want to apportion compute cost.
  * Token field shape matches OpenAI (prompt_tokens / completion_tokens).
  * Models of the core model's family served by vLLM emit Harmony control tokens in some
    output paths; the existing legba parser handles that in the agent
    layer. The handler here does NOT strip Harmony markers — that's a
    decision the caller (analyst handler) makes based on whether it's
    treating the response as data or as already-parsed text. (We leave
    the `legba.agent.llm.format.strip_harmony_response` path available
    for callers that want it.)
  * Some vLLM builds return multi-message choice lists (reasoning + final);
    we coalesce by joining text in choice order, mirroring the existing
    `legba.agent.llm.provider.VLLMProvider` behavior.
  * Temperature defaults to 1.0 for core-model workloads per ernie's proven
    pattern (`legba.agent.llm.provider`). Callers can override.

Streaming: EVERY generation goes over the wire with
`stream: true` + `stream_options.include_usage`, and the SSE
`chat.completion.chunk` events are folded back into the same JSON shape the
non-streaming endpoint returns (`_accumulate_sse`), so `_parse_response` and
every caller are unchanged. Consult 6cd91190 (2026-10-29) is why: the core
plane's forced final sat on ONE non-streaming request for 19 minutes until the
connection dropped, with zero text delivered. A live stream resets the read
timeout with every delta, publishes its text to an installed
``stream_observer`` sink, and a stream that dies after output raises
:class:`StreamInterruptedFailure` CARRYING that output. A caller can opt out
per call with ``stream=False`` (the historical wire call, exactly).

Healthcheck: TCP reachability + vault verification (same lean default as
the base — burning self-hosted compute on every 60s poll still wastes GPU
cycles).
"""

from __future__ import annotations
from legba.shared.sampling import HOUSE_TEMPERATURE

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncIterator, ClassVar, Mapping

import httpx

from .base import (
    LLMProviderHandler,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    ModelPrice,
    StreamInterruptedFailure,
    TransientLLMFailure,
    estimate_cost,
)
from .call_gate import gate_for_component
from .stream_observer import publish_text_delta, publish_tool_use_start

logger = logging.getLogger(__name__)

#: The SSE terminator (``data: [DONE]``), yielded by ``_iter_chat_sse``.
_DONE: Any = object()

#: Top-level keys of a ``chat.completion.chunk`` carried onto the accumulated
#: body, first value wins. ``provider`` is the router's serving provider — the
#: receipt's ``served_by`` and the width grader's ``grader_served_by`` read it.
_ENVELOPE_KEYS: tuple[str, ...] = (
    "id", "created", "model", "provider", "system_fingerprint",
)

#: ``/v1/models`` keys that name a model's context window, in probe order: the
#: model gateway's ``context_tokens``, vLLM's ``max_model_len``, OpenRouter's
#: ``context_length``.
_WINDOW_KEYS: tuple[str, ...] = (
    "context_tokens", "max_model_len", "context_length", "context_window",
)

#: Delta keys a reasoning model streams its thinking under (never the answer).
_REASONING_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning")


#: The house token estimate's divisor (``analysts/_llm_budget.CHARS_PER_TOKEN``).
_CHARS_PER_TOKEN = 4

#: ``LEGBA_LLM_SEND_MAX_TOKENS`` values that stop this handler sending any
#: ``max_tokens``. Unset (or any other value) sends the declared maximum.
SEND_MAX_TOKENS_ENV = "LEGBA_LLM_SEND_MAX_TOKENS"
_SEND_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def _send_switched_off() -> bool:
    """Is the deployment-wide off switch set?"""
    return os.getenv(SEND_MAX_TOKENS_ENV, "").strip().lower() in _SEND_OFF_VALUES


def _window_of(item: Mapping[str, Any]) -> int | None:
    for key in _WINDOW_KEYS:
        raw = item.get(key)
        if isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


async def _iter_chat_sse(response: httpx.Response) -> AsyncIterator[Any]:
    """Yield each ``data:`` payload of an OpenAI-style SSE body as a dict.

    ``data: [DONE]`` yields :data:`_DONE`. Comment lines (``: OPENROUTER
    PROCESSING`` keep-alives) and ``event:`` lines are skipped; a payload that
    does not parse is skipped rather than fatal — the accumulator's
    missing-terminator check catches a stream that carried nothing usable.
    """
    data_lines: list[str] = []

    def _flush() -> Any:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines.clear()
        if raw.strip() == "[DONE]":
            return _DONE
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    async for line in response.aiter_lines():
        if line == "":
            event = _flush()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    event = _flush()
    if event is not None:
        yield event


class VLLMProviderHandler(LLMProviderHandler):
    """Concrete handler for vLLM / the core model / any OpenAI-compatible endpoint."""

    subprovider: ClassVar[str] = "vllm"
    schema_version: ClassVar[str] = "legba/stack.llm_provider/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    default_port: ClassVar[int] = 443

    # Self-hosted is zero-list-price by default. This class-level table stays
    # EMPTY on purpose: this handler also serves every hosted PAYG
    # `.openai_compat` lane (Cerebras, OpenRouter), and one code table cannot
    # price two endpoints serving the same model name differently. Pricing is
    # therefore PER-COMPONENT — `price_input_per_m` / `price_output_per_m` on
    # the component config (see `_component_price`); a component with neither
    # set costs $0.00 exactly as before (#22, 2026-08-15).
    PRICE_TABLE: ClassVar[Mapping[str, ModelPrice]] = {}

    # Default temperature for core-model-class workloads; callers override.
    _DEFAULT_TEMPERATURE: ClassVar[float] = HOUSE_TEMPERATURE

    #: The base ``_call_chat`` hands an SSE body to
    #: ``_accumulate_sse`` (every pre-stream branch is the base's own).
    ACCUMULATES_SSE: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        # Per-event-loop concurrency semaphores (see `_call_chat`). asyncio
        # primitives bind to the loop they are first awaited on, and one
        # cached handler instance can be driven from more than one loop across
        # its life, so the semaphore is keyed by (loop id, limit) — a config
        # change to `max_concurrent` simply starts gating on a fresh
        # semaphore. Bounded by loops x distinct limits, i.e. a handful.
        self._concurrency_sems: dict[tuple[int, int], asyncio.Semaphore] = {}
        # model id -> context window (tokens), from the ``/v1/models`` listing
        # read at configure time; empty when the server does not report one.
        self._context_windows: dict[str, int] = {}
        self._usage_absent_logged = False
        #: Calls whose usage block had no ``completion_tokens_details.
        #: reasoning_tokens`` breakout (recorded as 0, not estimated).
        self.reasoning_usage_absent = 0
        self._reasoning_absent_logged = False

    # ---- Per-component pricing (#22) --------------------------------------

    def _component_price(self) -> ModelPrice | None:
        """The component config's own USD-per-1M price, or ``None``.

        ``None`` (neither field set) is the self-hosted posture and falls
        back to ``PRICE_TABLE`` — empty, so $0.00 — byte-identical to the
        historical behavior. One field set alone prices the other side at
        0.0 rather than refusing: a free-input promo lane is a real thing,
        and a half-priced receipt beats a silently unpriced one.
        """
        cfg = self._cfg
        if cfg is None:
            return None
        inp = getattr(cfg, "price_input_per_m", None)
        out = getattr(cfg, "price_output_per_m", None)
        if inp is None and out is None:
            return None
        try:
            return ModelPrice(
                input_per_m=float(inp.raw) if inp is not None else 0.0,
                output_per_m=float(out.raw) if out is not None else 0.0,
            )
        except (TypeError, ValueError):  # pragma: no cover — schema-checked
            logger.warning(
                "vllm._component_price unparsable price on %s — costing 0.0",
                self._instance_id,
            )
            return None

    # ---- Client concurrency (#21) -----------------------------------------

    def _concurrency_limit(self) -> int | None:
        """``max_concurrent`` from the component config; ``None`` = unlimited.

        T-1: on the core-plane component the process-wide gate
        (``call_gate``, held by ``base.chat_complete``) is THE bound, so the
        per-instance semaphore steps aside there — two bounds would make the
        smaller one the real one and the gate's fairness moot.
        """
        if gate_for_component(self._instance_id) is not None:
            return None
        cfg = self._cfg
        raw = getattr(cfg, "max_concurrent", None) if cfg is not None else None
        if raw is None:
            return None
        try:
            limit = int(raw.raw)
        except (TypeError, ValueError):  # pragma: no cover — schema-checked
            return None
        return limit if limit > 0 else None

    async def _call_chat(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Gate the wire call behind the component's ``max_concurrent``.

        The override wraps ONLY the network hop — payload assembly, response
        parsing and the R11 receipt accounting all happen outside the gate,
        so a held slot is always a request actually in flight against the
        endpoint. No configured limit = the base call, untouched.
        """
        limit = self._concurrency_limit()
        if limit is None:
            return await super()._call_chat(payload)
        key = (id(asyncio.get_running_loop()), limit)
        sem = self._concurrency_sems.get(key)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._concurrency_sems[key] = sem
        async with sem:
            return await super()._call_chat(payload)

    # ---- Endpoint --------------------------------------------------------

    def _chat_endpoint_path(self) -> str:
        # base.py's `_get_client` strips trailing `/v1` from the operator's
        # `api_endpoint` defensively (so `https://host` and `https://host/v1`
        # both work). The handler must therefore prepend `/v1/...` here —
        # otherwise the final URL becomes `https://host/chat/completions`
        # which the vLLM server doesn't serve (it lives at `/v1/chat/completions`
        # per OpenAI-compat surface). Sibling handlers
        # `openai._chat_endpoint_path` returns `/v1/chat/completions` and
        # `anthropic` returns `/v1/messages` — vLLM was the only one
        # missing the prefix until 2026-05-29.
        return "/v1/chat/completions"

    # ---- Tool translation (OpenAI passthrough) ---------------------------

    def _translate_tools(self, tools: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """vLLM ships OpenAI-compatible tool-call semantics (>=0.6.x). Same
        passthrough as the OpenAI handler."""
        result: list[Mapping[str, Any]] = []
        for spec in tools:
            if "function" in spec:
                result.append(dict(spec))
            elif "name" in spec:
                result.append({"type": "function", "function": dict(spec)})
            else:
                result.append(dict(spec))
        return result

    # ---- Payload assembly ------------------------------------------------

    def _build_chat_payload(
        self,
        *,
        messages: list[Mapping[str, Any]],
        system: str | None,
        tools: list[Mapping[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float | None,
        reasoning_effort: str | None,  # vLLM doesn't honor this directly
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Stream unless the caller explicitly asks not to
        # (``stream=False``) — the explicit flag is the ONLY way back to the
        # historical non-streaming wire call.
        stream = kwargs.pop("stream", True) is not False
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": stream,
            "temperature": (
                temperature if temperature is not None else self._DEFAULT_TEMPERATURE
            ),
        }
        if stream:
            # The final chunk then carries ``usage`` (choices: []); without it a
            # streamed call would report zero tokens to the spend meter.
            payload["stream_options"] = dict(
                kwargs.pop("stream_options", None) or {"include_usage": True}
            )
        else:
            kwargs.pop("stream_options", None)
        # `max_tokens` is the alias's own declared maximum output, so an
        # endpoint whose default is lower (a hosted gateway's per-alias
        # default) cannot cut a long answer or a long reasoning trace short.
        # Never the caller's `max_tokens`. An alias with no declared maximum
        # gets no field and the endpoint's own default applies, as before.
        # Less than the maximum only when the counted input leaves less room in
        # the window (an API validity constraint; the receipt records it).
        # LEGBA_LLM_SEND_MAX_TOKENS=0 (or false/no/off) is the deployment-wide
        # off switch; `send_max_tokens=False` is the per-call one.
        send_limit = kwargs.pop("send_max_tokens", None) is not False and not _send_switched_off()
        if tools:
            payload["tools"] = list(tools)
        stop = kwargs.pop("stop", None)
        if stop:
            payload["stop"] = list(stop)
        top_p = kwargs.pop("top_p", None)
        if top_p is not None:
            payload["top_p"] = top_p
        # vLLM-specific extras (reasoning effort isn't a wire arg; some
        # the core model's templates expect a "reasoning: high" instruction in the
        # message content. The caller injects that, not the handler.)
        for k, v in kwargs.items():
            payload.setdefault(k, v)
        # Router provider routing (2026-09-25): the component's
        # ``provider_ignore`` knob becomes OpenRouter's ``provider.ignore``
        # list on every request. Merged UNDER any caller-supplied
        # ``provider`` object rather than replacing it; unset or empty knob
        # ⇒ no ``provider`` key at all, so every other component's body is
        # byte-identical to before the knob existed.
        ignore = self._provider_ignore()
        if ignore:
            provider = dict(payload.get("provider") or {})
            merged = list(provider.get("ignore") or [])
            merged.extend(slug for slug in ignore if slug not in merged)
            provider["ignore"] = merged
            payload["provider"] = provider
        if send_limit:
            # Last, so the counted input includes the tool schemas.
            limit = self._max_tokens_for_window(model, payload)
            if limit:
                payload["max_tokens"] = limit
        return payload

    # ---- Output limit: the alias's maximum, inside the window ------------

    def _counted_input_tokens(self, payload: Mapping[str, Any]) -> int:
        """The request's input tokens, counted before sending (the house
        chars/4 estimate over messages and tool schemas, as the Anthropic
        handler counts)."""
        body = json.dumps([payload.get("messages"), payload.get("tools")], default=str)
        return (len(body) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN

    def _max_tokens_for_window(self, model: str, payload: Mapping[str, Any]) -> int | None:
        """The alias's declared maximum, or the window's room when the counted
        input leaves less; ``None`` when no maximum is declared.

        room <= 0 (the input alone fills the window): the maximum goes and the
        endpoint's own refusal says why, rather than a guess."""
        ceiling = self.model_max_output_tokens(model)
        if not ceiling:
            return None
        window = self.context_window_tokens()
        if not window:
            return ceiling
        room = window - self._counted_input_tokens(payload)
        return room if 0 < room < ceiling else ceiling

    def _wire_receipt_fields(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Sparse receipt fields: present only when the window limited max_tokens."""
        sent = payload.get("max_tokens")
        ceiling = self.model_max_output_tokens(str(payload.get("model") or "")) if sent else None
        if isinstance(sent, int) and isinstance(ceiling, int) and sent < ceiling:
            return {"max_tokens_sent": sent, "max_tokens_model_max": ceiling,
                    "max_tokens_limited_by": "context_window"}
        return None

    def _provider_ignore(self) -> list[str]:
        """The component's ``provider_ignore`` slugs, comma/whitespace-split,
        in order, de-duplicated; ``[]`` when unset, empty, or unparsable."""
        cfg = self._cfg
        knob = getattr(cfg, "provider_ignore", None) if cfg is not None else None
        raw = getattr(knob, "raw", None) if knob is not None else None
        if not isinstance(raw, str):
            return []
        out: list[str] = []
        for part in raw.replace(",", " ").split():
            slug = part.strip()
            if slug and slug not in out:
                out.append(slug)
        return out

    # ---- Streaming --------------------------------------------

    async def _accumulate_sse(self, response: httpx.Response) -> dict[str, Any]:
        """Fold ``chat.completion.chunk`` events into one ``chat.completion`` body.

        Per choice: ``delta.content`` text is joined (and published to an
        installed ``stream_observer`` sink as it arrives), ``delta.tool_calls``
        fragments are assembled BY INDEX (id / name from the first fragment that
        carries them, ``arguments`` concatenated), reasoning deltas are kept on
        the message under the key they arrived with, and the last non-null
        ``finish_reason`` wins. ``usage`` comes from the final chunk the server
        sends under ``stream_options.include_usage``; if it never arrives the
        body carries none and the call reports ZERO tokens (logged once per
        handler) — never an estimate.

        Outcomes:

          * terminated (``[DONE]``, or the primary choice's ``finish_reason``)
            → the body, the same shape the non-streaming endpoint returns;
          * an in-stream ``error`` event BEFORE any output → the error envelope
            with no ``choices``, which the base's router branch retries or
            raises exactly as it does the JSON form;
          * any other failure BEFORE output → :class:`TransientLLMFailure`
            (nothing was generated; not retried here);
          * any failure AFTER output (text or a tool call) →
            :class:`StreamInterruptedFailure` carrying the partial text, whether
            a tool call had begun, and the partial body. Never retried: the
            prefix is already generated.
        """
        envelope: dict[str, Any] = {}
        choices: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        error_obj: dict[str, Any] | None = None
        saw_done = False
        stream_error: str | None = None

        try:
            async for event in _iter_chat_sse(response):
                if event is _DONE:
                    saw_done = True
                    break
                for key in _ENVELOPE_KEYS:
                    if key in event and key not in envelope:
                        envelope[key] = event[key]
                if isinstance(event.get("error"), Mapping):
                    error_obj = dict(event["error"])
                    stream_error = (
                        "provider error event: "
                        + json.dumps(error_obj, default=str)[:300]
                    )
                    break
                raw_usage = event.get("usage")
                if isinstance(raw_usage, Mapping) and raw_usage:
                    usage = dict(raw_usage)
                for ch in event.get("choices") or []:
                    if isinstance(ch, Mapping):
                        self._fold_chunk_choice(choices, ch)
        except httpx.HTTPError as exc:
            stream_error = f"{type(exc).__name__}: {exc}"

        body_choices = [
            self._assembled_choice(idx, choices[idx]) for idx in sorted(choices)
        ]
        data: dict[str, Any] = {
            **envelope, "object": "chat.completion", "choices": body_choices,
        }
        if usage is not None:
            data["usage"] = usage

        primary = choices[min(choices)] if choices else None
        finished = saw_done or (
            primary is not None and primary["finish_reason"] is not None
        )
        if stream_error is not None and error_obj is None and finished:
            # The generation completed; only the trailer (usage / [DONE]) was
            # lost. The answer is whole — deliver it as one.
            logger.warning(
                "vllm.stream.trailer_lost component=%s err=%s — the generation "
                "had finished; delivered whole",
                self._instance_id or "<unconfigured>", stream_error,
            )
            stream_error = None
        if stream_error is None and not finished:
            stream_error = "stream closed before [DONE]"

        partial_text = "\n".join(
            text for text in (
                "".join(choices[idx]["content"]) for idx in sorted(choices)
            ) if text
        )
        tool_use_started = any(st["tool_calls"] for st in choices.values())

        if stream_error is not None:
            if not partial_text and not tool_use_started:
                if error_obj is not None:
                    # Nothing generated: the router's error envelope, which the
                    # base retries or raises exactly as the JSON form.
                    return {**envelope, "error": error_obj}
                raise TransientLLMFailure(
                    f"{self.subprovider} stream failed before any content "
                    f"arrived: {stream_error}",
                )
            if body_choices:
                body_choices[0]["finish_reason"] = "error"
            data["legba_stream_error"] = stream_error
            logger.warning(
                "vllm.stream.MID_STREAM_FAILURE component=%s model=%s chars=%d "
                "tool_use_started=%s err=%s — raising StreamInterruptedFailure "
                "WITH the partial generation. NOT retried: the streamed prefix "
                "is already generated.",
                self._instance_id or "<unconfigured>",
                data.get("model") or "<unknown>",
                len(partial_text), tool_use_started, stream_error,
            )
            raise StreamInterruptedFailure(
                f"{self.subprovider} stream interrupted after "
                f"{len(partial_text)} chars: {stream_error}",
                partial_text=partial_text,
                tool_use_started=tool_use_started,
                partial_body=data,
            )

        if usage is None and not self._usage_absent_logged:
            self._usage_absent_logged = True
            logger.warning(
                "vllm.stream.usage_absent component=%s — the server sent no "
                "usage chunk despite stream_options.include_usage; this "
                "handler's streamed calls report 0 tokens (not estimated)",
                self._instance_id or "<unconfigured>",
            )
        return data

    @staticmethod
    def _fold_chunk_choice(
        choices: dict[int, dict[str, Any]], ch: Mapping[str, Any],
    ) -> None:
        """Fold one chunk choice's ``delta`` into its accumulator slot."""
        try:
            idx = int(ch.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        st = choices.setdefault(idx, {
            "role": None, "content": [], "reasoning": {}, "tool_calls": {},
            "finish_reason": None,
        })
        delta = ch.get("delta")
        if not isinstance(delta, Mapping):
            delta = {}
        if delta.get("role"):
            st["role"] = delta["role"]
        text = delta.get("content")
        if isinstance(text, str) and text:
            st["content"].append(text)
            # Lets a caller that is cut (or whose call fails) mid-generation
            # still hold the prefix — see ``stream_observer``.
            publish_text_delta(text)
        # Thinking (the ``deep`` alias) streams here. It is NOT answer text:
        # never published to the stream observer (a live Consult shows only
        # ``content``), never in ``LLMResponse.content`` (so never counted as
        # answer length), and counted only through the usage block's
        # ``reasoning_tokens``.
        for rkey in _REASONING_KEYS:
            rtext = delta.get(rkey)
            if isinstance(rtext, str) and rtext:
                st["reasoning"].setdefault(rkey, []).append(rtext)
        calls: dict[int, dict[str, Any]] = st["tool_calls"]
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, Mapping):
                continue
            # Lets a caller cut mid-round tell a tool round's preamble from an
            # answer (``stream_observer``).
            publish_tool_use_start()
            tidx = tc.get("index")
            if not isinstance(tidx, int) or isinstance(tidx, bool):
                # No index: a new id opens a new call; otherwise the fragment
                # continues the latest one.
                last = max(calls) if calls else None
                if last is None or (
                    tc.get("id") and calls[last]["id"]
                    and tc.get("id") != calls[last]["id"]
                ):
                    tidx = len(calls)
                else:
                    tidx = last
            call = calls.setdefault(tidx, {
                "id": None, "type": "function", "name": "", "args": [],
            })
            if tc.get("id") and not call["id"]:
                call["id"] = tc["id"]
            if tc.get("type"):
                call["type"] = tc["type"]
            fn = tc.get("function")
            if not isinstance(fn, Mapping):
                fn = {}
            if fn.get("name") and not call["name"]:
                call["name"] = fn["name"]
            args = fn.get("arguments")
            if isinstance(args, str):
                call["args"].append(args)
            elif isinstance(args, Mapping):
                call["args"].append(json.dumps(args))
        if ch.get("finish_reason") is not None:
            st["finish_reason"] = ch["finish_reason"]

    @staticmethod
    def _assembled_choice(idx: int, st: Mapping[str, Any]) -> dict[str, Any]:
        """One accumulator slot as a non-streaming ``choices[]`` entry."""
        message: dict[str, Any] = {
            "role": st["role"] or "assistant",
            "content": "".join(st["content"]) if st["content"] else None,
        }
        for rkey, parts in st["reasoning"].items():
            message[rkey] = "".join(parts)
        if st["tool_calls"]:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": call["type"],
                    "function": {
                        "name": call["name"],
                        "arguments": "".join(call["args"]),
                    },
                }
                for _, call in sorted(st["tool_calls"].items())
            ]
        return {"index": idx, "message": message, "finish_reason": st["finish_reason"]}

    # ---- Response parsing ------------------------------------------------

    def _parse_response(self, data: Mapping[str, Any], *, model: str) -> LLMResponse:
        choices = data.get("choices", []) or []
        if not choices:
            return LLMResponse(
                content="", finish_reason="error",
                raw_response=dict(data),
                usage=LLMUsage(model=model),
            )

        # vLLM reasoning-mode often emits 2+ choices: reasoning then final.
        # Coalesce by joining the text content of every choice in order.
        content_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        primary = choices[0]
        for ch in choices:
            message = ch.get("message", {}) or {}
            c = message.get("content")
            if isinstance(c, str) and c:
                content_parts.append(c)
            # Gather tool_calls only from the primary choice (vLLM places
            # them on choices[0] when present; reasoning choices don't carry
            # tool_calls fields).
            if ch is primary:
                for tc in message.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args_raw = fn.get("arguments", "")
                    try:
                        arguments = (
                            json.loads(args_raw) if isinstance(args_raw, str)
                            else dict(args_raw)
                        )
                    except (json.JSONDecodeError, TypeError, ValueError):
                        arguments = {"_raw": args_raw}
                    if not isinstance(arguments, dict):
                        arguments = {"_value": arguments}
                    tool_calls.append(
                        LLMToolCall(
                            id=str(tc.get("id") or uuid.uuid4().hex),
                            name=str(fn.get("name") or ""),
                            arguments=arguments,
                        )
                    )

        content = "\n".join(p for p in content_parts if p)
        finish_reason = self._normalize_finish_reason(primary.get("finish_reason"))

        raw_usage = data.get("usage", {}) or {}
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0))
        completion_tokens = int(raw_usage.get("completion_tokens", 0))
        usage = LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=self._reasoning_tokens_of(raw_usage),
            total_tokens=int(raw_usage.get("total_tokens",
                                            prompt_tokens + completion_tokens)),
            model=model,
        )
        # Per-component price wins when configured (the hosted PAYG lanes);
        # otherwise the subprovider table, which for vllm is empty = $0.00
        # (self-hosted, the historical posture).
        price = self._component_price()
        usage.cost_estimate_usd = estimate_cost(
            model, usage, {model: price} if price is not None else self.PRICE_TABLE
        )

        return LLMResponse(
            content=content,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=dict(data),
        )

    def _reasoning_tokens_of(self, raw_usage: Mapping[str, Any]) -> int:
        """``usage.completion_tokens_details.reasoning_tokens``, or 0 when absent.

        The OpenAI-compatible shape, and the one the gateway's own docs state
        (read 2026-11-13): reasoning tokens are INCLUDED in ``completion_tokens`` and
        broken out under ``completion_tokens_details``. So this is a subset of
        ``completion_tokens``, never added to it — ``total_tokens`` stays the
        server's number, and the receipt records it beside input and output
        exactly as ``openai.py`` does. The 11-13 probes read 13 reasoning of
        17 completion on ``deep`` and 0 on ``fast``.

        Recording only: nothing here bounds reasoning (no token caps on the
        core plane). An absent or malformed field records 0 — never an
        estimate — and is counted on ``reasoning_usage_absent`` (a usage block
        with no breakout), logged once per handler, so a 0 from a server that
        does not break reasoning out is told apart from a measured 0.
        """
        details = raw_usage.get("completion_tokens_details") if raw_usage else None
        value = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                pass
        if raw_usage:
            self.reasoning_usage_absent += 1
            if not self._reasoning_absent_logged:
                self._reasoning_absent_logged = True
                logger.info(
                    "vllm.usage.reasoning_absent component=%s — the usage block "
                    "carries no completion_tokens_details.reasoning_tokens; "
                    "this handler records reasoning_tokens=0 for such calls "
                    "(counted on reasoning_usage_absent, not estimated)",
                    self._instance_id or "<unconfigured>",
                )
        return 0

    @staticmethod
    def _normalize_finish_reason(raw: Any) -> str:
        if raw is None:
            return "unknown"
        s = str(raw)
        if s == "function_call":
            return "tool_calls"
        return s

    async def _fetch_model_list(self) -> list[str]:
        """``/v1/models`` (then ``/models``): the catalog AND each model's window.

        The configured api_endpoint may or may not include ``/v1`` — both
        shapes are tried, first non-empty listing wins (the historical order).
        The same listing's context window per model (the gateway's
        ``context_tokens``, vLLM's ``max_model_len``, OpenRouter's
        ``context_length``) is kept for :meth:`context_window_tokens`.
        """
        client = await self._get_client()
        for path in ("/v1/models", "/models"):
            try:
                resp = await client.get(path)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except ValueError:
                continue
            raw_items = data.get("data") if isinstance(data, Mapping) else None
            items = [
                item for item in raw_items or []
                if isinstance(item, dict) and "id" in item
            ]
            if not items:
                continue
            windows: dict[str, int] = {}
            for item in items:
                window = _window_of(item)
                if window is not None:
                    windows[str(item["id"])] = window
            self._context_windows = windows
            return [str(item["id"]) for item in items]
        return []

    def context_window_tokens(self) -> int | None:
        """The declared ``context_window_tokens``; else the configured model's
        window from the ``/v1/models`` listing; else ``None``."""
        cfg = self._cfg
        if cfg is None:
            return None
        declared = super().context_window_tokens()
        if declared is not None:
            return declared
        return self._context_windows.get(str(cfg.model_name.raw))


__all__ = ["VLLMProviderHandler"]
