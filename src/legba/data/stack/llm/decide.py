# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision-model client — ``POST {api_endpoint}/decide``.

A DECISION model answers typed questions about a free-text state instead of
generating text. The gateway serves one beside its chat
aliases, on its own route::

    request   {"model": "<decision model>",
               "state": "<free text>",
               "questions": {"<id>": {"type": "choice",
                                      "instructions": "<question>",
                                      "criteria": {"<option>": "<meaning>", …}}}}
    response  {"model": "<decision model>",
               "answers": {"<id>": {"type": "choice", "choice": "<option>",
                                    "confidence": 0.91,
                                    "probabilities": {"<option>": p, …}}},
               "usage": {"input_tokens": 317, "output_tokens": 0}}
    error     {"error": {"message": …, "type": …}} with HTTP 400/422/429/5xx

This handler is an :class:`LLMProviderHandler` so it is BUILT the way every
model client is — a registered ``llm_provider`` stack component, re-parsed
through :class:`LLMProviderConfig`, its Bearer key resolved out of the
CredentialVault by ``on_configure`` — and routed by
``analyst_deps_builder.infer_llm_subprovider`` (a component id containing
``.decide.`` → ``"decide"``). It has NO chat surface: ``chat_complete``
refuses loudly, because a decision component wired into a chat leg is a
configuration mistake, never a degraded answer.

What it shares with the chat clients, deliberately:

  * the typed errors — :class:`TransientLLMFailure` for 429 / 5xx / network
    (carrying the provider's ``Retry-After``, parsed by the same
    :func:`_parse_retry_after` the chat loop uses), :class:`HardLLMFailure`
    for every other 4xx and for an answer that does not fit the question;
  * the per-component ``max_concurrent`` semaphore (the vLLM handler's idiom);
  * the R11 receipt: each call — success or failure — lands in the bound
    run account (``analyst_traces.llm_calls``) with ``component_id``,
    ``status``, ``http_status``, ``duration_ms`` and the input tokens, plus a
    ``leg`` tag when the caller names one.

No output limit is declared or sent: a decision call generates no tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, ClassVar, Mapping

import httpx

from ...run_accounting import COMPLETION_TEXT_FIELD, prompt_digest, record_llm_call
from .base import (
    _CALL_STATUS_BY_EXC,
    HardLLMFailure,
    LLMProviderHandler,
    LLMResponse,
    ModelPrice,
    TransientLLMFailure,
    _envelope_code,
    _parse_retry_after,
)

logger = logging.getLogger(__name__)

#: The question types the client will send. The gateway also knows ``score``,
#: which needs criteria too; nothing in the tree asks one yet, and a type the
#: client does not parse must not reach the wire.
QUESTION_TYPES: frozenset[str] = frozenset({"choice"})


def _error_detail(response: httpx.Response) -> str:
    """``error.message`` (and ``type``) from the gateway's error body, bounded."""
    try:
        data = response.json()
    except ValueError:
        return (response.text or "")[:300]
    err = data.get("error") if isinstance(data, Mapping) else None
    if isinstance(err, Mapping):
        kind = err.get("type")
        msg = str(err.get("message") or "")[:300]
        return f"{kind}: {msg}" if kind else msg
    return json.dumps(data, default=str)[:300]


def validate_questions(questions: Mapping[str, Any]) -> None:
    """Refuse a question set the endpoint would reject, before the wire.

    Every question needs a known ``type``, ``instructions`` and NON-EMPTY
    ``criteria`` (the endpoint answers 422 otherwise) — catching it here makes
    the mistake a caller bug with a legible message, not a burned call."""
    if not isinstance(questions, Mapping) or not questions:
        raise HardLLMFailure("decide: at least one question is required")
    for qid, q in questions.items():
        if not isinstance(q, Mapping):
            raise HardLLMFailure(f"decide: question {qid!r} is not a mapping")
        if q.get("type") not in QUESTION_TYPES:
            raise HardLLMFailure(
                f"decide: question {qid!r} type {q.get('type')!r} is not one of "
                f"{sorted(QUESTION_TYPES)}"
            )
        criteria = q.get("criteria")
        if not isinstance(criteria, Mapping) or not criteria:
            raise HardLLMFailure(f"decide: question {qid!r} needs non-empty criteria")
        if not str(q.get("instructions") or "").strip():
            raise HardLLMFailure(f"decide: question {qid!r} needs instructions")


def parse_answers(
    data: Any, questions: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """``{qid: {"type", "choice", "confidence", "probabilities"}}``.

    Every asked question must come back with a ``choice`` that is one of ITS
    OWN criteria keys; anything else is a :class:`HardLLMFailure` — an answer
    the caller cannot bind to the option it offered is not an answer.
    ``confidence`` is a float or ``None``; ``probabilities`` keeps only
    numeric entries."""
    answers = data.get("answers") if isinstance(data, Mapping) else None
    if not isinstance(answers, Mapping):
        raise HardLLMFailure("decide: response carries no 'answers' mapping")
    out: dict[str, dict[str, Any]] = {}
    for qid, q in questions.items():
        got = answers.get(qid)
        if not isinstance(got, Mapping):
            raise HardLLMFailure(f"decide: no answer for question {qid!r}")
        choice = got.get("choice")
        if not isinstance(choice, str) or choice not in q["criteria"]:
            raise HardLLMFailure(
                f"decide: question {qid!r} answered {choice!r}, not one of its criteria"
            )
        try:
            confidence: float | None = float(got.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        probs: dict[str, float] = {}
        raw_probs = got.get("probabilities")
        if isinstance(raw_probs, Mapping):
            for opt, p in raw_probs.items():
                try:
                    probs[str(opt)] = float(p)
                except (TypeError, ValueError):
                    continue
        out[qid] = {
            "type": str(got.get("type") or q["type"]),
            "choice": choice,
            "confidence": confidence,
            "probabilities": probs,
        }
    return out


class DecisionProviderHandler(LLMProviderHandler):
    """``llm_provider`` component whose model is a DECISION alias (``/decide``)."""

    subprovider: ClassVar[str] = "decide"
    schema_version: ClassVar[str] = "legba/stack.llm_provider/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    #: Our own box behind the gateway: no list price.
    PRICE_TABLE: ClassVar[Mapping[str, ModelPrice]] = {}
    #: Further attempts on a transient failure (429, any 5xx, a network error)
    #: — the chat loop's count. A caller with its own budget passes fewer.
    DEFAULT_MAX_RETRIES: ClassVar[int] = 3

    def __init__(self) -> None:
        super().__init__()
        # (loop id, limit) → semaphore: the vLLM handler's idiom (#21).
        self._concurrency_sems: dict[tuple[int, int], asyncio.Semaphore] = {}

    # ---- no chat surface --------------------------------------------------

    async def chat_complete(self, messages: Any, **kwargs: Any) -> LLMResponse:  # type: ignore[override]
        raise HardLLMFailure(
            f"{self._instance_id or 'decide'} is a DECISION component "
            "(POST /decide); it has no chat surface — call decide()"
        )

    async def _fetch_model_list(self) -> list[str]:
        """No discovery call at configure: a decision component is built once
        per process and its alias is the component's own ``model_name``."""
        return []

    def _decide_endpoint_path(self) -> str:
        # ``_get_client`` strips a trailing /v1 off the endpoint (the base's
        # defensive rule), so the /v1 goes back on here — the chat handlers'
        # convention, and the reason ``{api_endpoint}/decide`` and this path
        # name the same URL.
        return "/v1/decide"

    # ---- concurrency ------------------------------------------------------

    def _concurrency_limit(self) -> int | None:
        cfg = self._cfg
        raw = getattr(cfg, "max_concurrent", None) if cfg is not None else None
        if raw is None:
            return None
        try:
            limit = int(raw.raw)
        except (TypeError, ValueError):  # pragma: no cover — schema-checked
            return None
        return limit if limit > 0 else None

    def _semaphore(self) -> asyncio.Semaphore | None:
        limit = self._concurrency_limit()
        if limit is None:
            return None
        key = (id(asyncio.get_running_loop()), limit)
        sem = self._concurrency_sems.get(key)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._concurrency_sems[key] = sem
        return sem

    # ---- the call ---------------------------------------------------------

    async def decide(
        self,
        state: str,
        questions: Mapping[str, Any],
        *,
        model: str | None = None,
        leg: str | None = None,
        max_retries: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Ask ``questions`` about ``state``; return the parsed answers.

        Raises :class:`TransientLLMFailure` (429 / 5xx / network, after
        ``max_retries`` further attempts, carrying ``retry_after``) or
        :class:`HardLLMFailure` (any other 4xx, a malformed body, or an answer
        outside the question's criteria). ``leg`` is stamped on the receipt.
        """
        cfg = self._require_configured()
        validate_questions(questions)
        chosen_model = model or str(cfg.model_name.raw)
        payload = {
            "model": chosen_model,
            "state": str(state),
            "questions": {str(k): dict(v) for k, v in questions.items()},
        }
        retries = self.DEFAULT_MAX_RETRIES if max_retries is None else max(0, int(max_retries))
        started = time.monotonic()
        data: dict[str, Any] | None = None
        answers: dict[str, dict[str, Any]] | None = None
        exc_seen: BaseException | None = None
        try:
            sem = self._semaphore()
            if sem is None:
                data = await self._post_decide(payload, retries)
            else:
                async with sem:
                    data = await self._post_decide(payload, retries)
            answers = parse_answers(data, questions)
        except BaseException as exc:
            exc_seen = exc
            raise
        finally:
            self._account_decide(
                model=chosen_model,
                payload=payload,
                started_monotonic=started,
                data=data,
                answers=answers,
                exc=exc_seen,
                leg=leg,
            )
        return answers

    async def _post_decide(self, payload: Mapping[str, Any], max_retries: int) -> dict[str, Any]:
        client = await self._get_client()
        path = self._decide_endpoint_path()
        for attempt in range(max_retries + 1):
            last = attempt >= max_retries
            try:
                response = await client.post(path, json=dict(payload))
            except httpx.TransportError as exc:
                if not last:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise TransientLLMFailure(f"decide network error: {type(exc).__name__}") from exc
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            status = response.status_code
            if status == 429 or status >= 500:
                if not last:
                    await asyncio.sleep(retry_after if retry_after is not None else 2 ** attempt)
                    continue
                raise TransientLLMFailure(
                    f"decide {status}: {_error_detail(response)}",
                    status=status,
                    retry_after=retry_after,
                )
            if status >= 400:
                raise HardLLMFailure(
                    f"decide {status}: {_error_detail(response)}",
                    status=status,
                    body=self._safe_body(response)[:1000],
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise HardLLMFailure(
                    f"decide returned a non-JSON body (HTTP {status})", status=status
                ) from exc
            if not isinstance(data, dict):
                raise HardLLMFailure(f"decide returned a {type(data).__name__} body", status=status)
            err = data.get("error")
            if isinstance(err, Mapping) and "answers" not in data:
                # An error envelope on a 2xx (a router committing its headers
                # first): the status lives in the body, never on the line.
                code = _envelope_code(err)
                detail = str(err.get("message") or err)[:300]
                if code is None or code == 429 or code >= 500:
                    if not last:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise TransientLLMFailure(f"decide {status}/{code}: {detail}", status=code)
                raise HardLLMFailure(f"decide {status}/{code}: {detail}", status=code)
            return data
        raise TransientLLMFailure("decide failed after retries")  # pragma: no cover

    # ---- R11 receipt ------------------------------------------------------

    def _account_decide(
        self,
        *,
        model: str,
        payload: Mapping[str, Any],
        started_monotonic: float,
        data: Mapping[str, Any] | None,
        answers: Mapping[str, Any] | None,
        exc: BaseException | None,
        leg: str | None,
    ) -> None:
        """One ``llm_calls`` entry per decide call. Never raises.

        The prompt is digested (sha + chars), never stored; on success the
        receipt carries the answers' ``choice``/``confidence`` as its
        completion text, so "what did the model decide" is on the receipt the
        way a chat completion's text is."""
        try:
            sha, chars = prompt_digest(
                [{"role": "state", "content": payload.get("state")}],
                json.dumps(payload.get("questions"), sort_keys=True, default=str),
            )
            fields: dict[str, Any] = {
                "component_id": self._instance_id or None,
                "subprovider": self.subprovider,
                "model": model,
                "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
                "prompt_sha256": sha,
                "prompt_chars": chars,
                "leg": leg,
            }
            if exc is not None:
                fields["status"] = _CALL_STATUS_BY_EXC.get(type(exc).__name__, "error")
                fields["error"] = type(exc).__name__
                http_status = getattr(exc, "status", None)
                if isinstance(http_status, int):
                    fields["http_status"] = int(http_status)
            else:
                fields["status"] = "success"
                fields["http_status"] = 200
            usage = data.get("usage") if isinstance(data, Mapping) else None
            if isinstance(usage, Mapping):
                try:
                    inp = int(usage.get("input_tokens") or 0)
                    out = int(usage.get("output_tokens") or 0)
                except (TypeError, ValueError):
                    inp = out = 0
                fields.update(
                    prompt_tokens=inp,
                    completion_tokens=out,
                    total_tokens=inp + out,
                    cost_estimate_usd=0.0,
                )
            if answers:
                fields[COMPLETION_TEXT_FIELD] = json.dumps(
                    {k: {"choice": v.get("choice"), "confidence": v.get("confidence")}
                     for k, v in answers.items()},
                    sort_keys=True,
                )
            record_llm_call(**fields)
        except Exception:  # pragma: no cover — instrumentation must never bite
            logger.debug("llm.decide.account_call failed", exc_info=True)


__all__ = [
    "DecisionProviderHandler",
    "QUESTION_TYPES",
    "parse_answers",
    "validate_questions",
]
