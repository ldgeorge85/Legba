# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ``dspy.BaseLM`` backed by Legba's own ``LLMProviderHandler``.

The GEPA optimizer (worker-only) needs an LM for its reflection + student
calls. Stock dspy uses litellm for that. **We never invoke litellm** — this
module routes every optimize-time LLM call through the project's existing
``LLMProviderHandler`` (the same provider + budget machinery the analysts
use), so litellm stays an inert transitive dependency. This is an operator
hard rule (see planning/OPTIMIZER_DSPY_GEPA_PLAN.md +
``feedback-never-litellm-dspy-production``); it is enforced structurally —
this module lives in the worker-only ``dapr_workflow`` package and is never
imported on the runtime inference hot path.

Concurrency note: dspy's ``teleprompter.compile`` is SYNCHRONOUS and runs
inside the activity's ``asyncio.run`` (the outer loop is blocked on the sync
compile). dspy then calls our LM synchronously. To reach our *async*
provider without a nested-loop error, every call is dispatched to a
dedicated background event loop (:class:`_AsyncLoopBridge`). The handler is
built + used entirely on that loop, so its lazily-created httpx client binds
there too.

``dspy`` is imported lazily (inside :func:`make_provider_lm`) so this module
is importable in environments without dspy (e.g. host unit tests that only
exercise the helpers).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "make_provider_lm",
    "configure_gepa_lm",
    "split_messages",
]


def _lm_call_timeout_s() -> float:
    """Per-call wall-clock bound (seconds) for a GEPA reflection/student call.

    The hang that left the optimizer leg dead ~4 days was a single GEPA
    reflection call to the provider blocking forever — there was no timeout
    anywhere in the bridge → ``forward()`` → ``compile()`` chain, so GEPA sat
    at ``0/30 rollouts`` and ``compile()`` never returned (no trace, looked
    dormant). Bounding every LM call means GEPA can never hang on one call: a
    timed-out call returns an empty completion, dspy scores it the
    ``failure_score``, and the rollout loop proceeds. Env-tunable;
    ``<= 0`` disables the bound (restores the old unbounded behaviour).
    """
    raw = os.environ.get("LEGBA_GEPA_LM_CALL_TIMEOUT_S", "120")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 120.0
    return v if v > 0 else 0.0


# ---------------------------------------------------------------------------
# Sync→async bridge
# ---------------------------------------------------------------------------


class _AsyncLoopBridge:
    """A daemon-thread event loop that synchronous code can submit coros to.

    dspy's compile is sync and blocks the activity's event loop; our provider
    is async. Submitting to this independent loop avoids "this event loop is
    already running" / cross-loop httpx-client errors. All provider work
    (build, configure, calls, teardown) happens on THIS loop.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="legba-gepa-lm",
        )
        self._thread.start()

    def run(self, coro: Awaitable[Any], *, timeout: float | None = None) -> Any:
        """Run ``coro`` on the bridge loop and block for its result.

        ``timeout`` (seconds) bounds the wait; ``None`` blocks forever (the
        original behaviour). On expiry the underlying coroutine is cancelled
        (best-effort, on the bridge loop) and ``TimeoutError`` is raised so the
        caller can degrade gracefully rather than hang the whole GEPA compile.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()  # request cancellation on the bridge loop (best-effort)
            raise TimeoutError(
                f"bridge coroutine exceeded {timeout}s"
            ) from exc

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        try:
            self._loop.close()
        except Exception:  # pragma: no cover - best-effort teardown
            pass


# ---------------------------------------------------------------------------
# OpenAI-chat-completion-shaped response (NO litellm / openai import)
# ---------------------------------------------------------------------------
# dspy.BaseLM._process_completion reads ``response.choices[i].message.content``
# and dspy.BaseLM._process_lm_response reads ``dict(response.usage)`` +
# ``response.model`` + ``response._hidden_params`` for its history entry
# (which the optimizer's usage-delta helper sums for G5 budget accounting).


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.reasoning_content = None
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"
        self.index = 0


class _ChatResponse:
    def __init__(self, content: str, usage: dict[str, int], model: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = usage  # plain dict → dict(usage) round-trips for history
        self.model = model
        self._hidden_params: dict[str, Any] = {}


def split_messages(
    prompt: str | None, messages: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, str]], str | None]:
    """Translate dspy's (prompt|messages) call shape into our chat_complete
    shape: a non-system message list + a hoisted ``system`` string."""
    if messages is None:
        messages = [{"role": "user", "content": prompt or ""}]
    system_parts = [
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    ]
    rest = [
        {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
        for m in messages
        if m.get("role") != "system"
    ]
    if not rest:
        rest = [{"role": "user", "content": ""}]
    system = "\n\n".join(p for p in system_parts if p) or None
    return rest, system


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def make_provider_lm(
    handler: Any,
    bridge: _AsyncLoopBridge,
    *,
    model: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> Any:
    """Build a ``dspy.BaseLM`` that calls ``handler.chat_complete`` (async)
    via ``bridge``. Imports dspy lazily (worker-only dep)."""
    import dspy

    class LegbaProviderLM(dspy.BaseLM):
        def __init__(self) -> None:
            super().__init__(
                model=model,
                model_type="chat",
                temperature=temperature,
                max_tokens=max_tokens,
                cache=False,
            )
            self._handler = handler
            self._bridge = bridge

        def forward(self, prompt=None, messages=None, **kwargs):  # type: ignore[override]
            msgs, system = split_messages(prompt, messages)
            mt = int(kwargs.get("max_tokens") or self.kwargs.get("max_tokens") or max_tokens)
            temp = float(
                kwargs.get("temperature", self.kwargs.get("temperature", temperature))
            )
            call_timeout = _lm_call_timeout_s()
            try:
                resp = self._bridge.run(
                    self._handler.chat_complete(
                        msgs, system=system, max_tokens=mt, temperature=temp,
                    ),
                    timeout=call_timeout or None,
                )
            except TimeoutError:
                # The provider stalled on this call. Return an empty completion
                # (zero usage) so GEPA scores it the failure_score and the
                # rollout loop continues instead of hanging the whole compile —
                # the exact failure that left the optimizer leg dead ~4 days.
                logger.warning(
                    "optimizer.gepa.lm_call_timeout model=%s timeout=%ss "
                    "— returning empty completion so GEPA records a failure "
                    "and continues", model, call_timeout,
                )
                return _ChatResponse(
                    content="",
                    usage={"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0},
                    model=model,
                )
            content = getattr(resp, "content", "") or ""
            usage = getattr(resp, "usage", None)
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            rt = int(getattr(usage, "reasoning_tokens", 0) or 0)
            # Fold reasoning tokens into completion for the ledger; keep an
            # explicit total so the optimizer's usage-delta reads it directly.
            usage_dict = {
                "prompt_tokens": pt,
                "completion_tokens": ct + rt,
                "total_tokens": pt + ct + rt,
            }
            return _ChatResponse(content=content, usage=usage_dict, model=model)

    return LegbaProviderLM()


# ---------------------------------------------------------------------------
# Resolution: analyst_id → configured dspy LM (+ cleanup)
# ---------------------------------------------------------------------------


def configure_gepa_lm(
    analyst_id: str,
    *,
    reflection_component_id: str | None = None,
) -> tuple[Any, Callable[[], None]] | None:
    """Resolve the analyzed analyst's LLM into a configured dspy LM.

    Returns ``(lm, cleanup)`` — caller does ``dspy.settings.configure(lm=lm)``,
    runs GEPA, then ``cleanup()``. Returns ``None`` on ANY failure (missing
    config, registry unreachable, dspy absent) so the optimizer degrades to
    its naive fallback rather than crashing the workflow.

    All substrate work happens on the bridge loop so the handler's httpx
    client (and the pg pool) bind to the loop dspy's sync calls reach.
    """
    bridge = _AsyncLoopBridge()
    try:
        state = bridge.run(_build_handler(analyst_id, reflection_component_id))
    except Exception as exc:  # noqa: BLE001 — defensive: fall back to naive
        logger.warning(
            "optimizer.gepa.lm_resolve_failed analyst=%s err=%r "
            "(falling back to naive candidate search)", analyst_id, exc,
        )
        bridge.close()
        return None

    handler = state["handler"]
    lm = make_provider_lm(
        handler,
        bridge,
        model=state["model"],
        max_tokens=int(state["max_tokens"]),
        temperature=float(state["temperature"]),
    )
    logger.info(
        "optimizer.gepa.lm_configured analyst=%s component=%s model=%s",
        analyst_id, state["component_id"], state["model"],
    )

    def _cleanup() -> None:
        # Bound the teardown: closing the handler's httpx client + pg pool runs
        # on the bridge loop, and this runs in the GEPA activity's `finally` —
        # so if a close() blocks it hangs the WHOLE activity AFTER compile
        # already finished (the orchestration then stalls waiting for an
        # activity result that never comes, exactly the DQ-C4 round-trip hang
        # that compile-completion first exposed). A timeout guarantees the
        # activity returns; bridge.close() then reclaims the loop thread.
        try:
            bridge.run(_teardown_handler(state), timeout=30.0)
        except TimeoutError:
            logger.warning(
                "optimizer.gepa.lm_teardown_timeout analyst=%s "
                "— closing bridge anyway", analyst_id,
            )
        except Exception:  # pragma: no cover - best-effort
            pass
        bridge.close()

    return lm, _cleanup


def _scalar_option(value: Any, *, default: float) -> float:
    """Read a descriptor scalar option that may be bare or factory-wrapped."""
    if isinstance(value, dict):
        value = value.get("raw", default)
    if isinstance(value, bool):  # guard: bool is an int subclass
        return default
    if isinstance(value, (int, float)):
        return value
    return default


async def _build_handler(
    analyst_id: str, reflection_component_id: str | None,
) -> dict[str, Any]:
    """Build the LLM handler + read its model params. Runs on the bridge loop."""
    from ...data.config import PostgresConfig
    from ...data.postgres import PostgresStore
    from ...data.registry.credentials import CredentialVault
    from ..analyst_deps_builder import build_llm_handler_from_stack_component
    from ..registry_client import RegistryHTTPClient

    pg = PostgresStore(PostgresConfig.from_env())
    await pg.connect()
    vault = CredentialVault(pg)

    async def _secrets_resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    registry = RegistryHTTPClient()
    typed = await registry.get_descriptor_typed(analyst_id, family="analyst")
    if not typed:
        raise RuntimeError(f"analyst descriptor {analyst_id!r} not found")

    # Read the LLM component id + options straight from the typed (JSON-dumped)
    # descriptor dict. We deliberately do NOT AnalystDescriptor.model_validate(...)
    # it — the typed dump serializes enums to strings (identity.state='active')
    # which the model's strict is_instance_of[LifecycleState] field rejects.
    # The method.llm.primary StackRef is a {raw, factory_kind, expected_family}
    # dict; max_tokens/temperature are bare scalars (or factory-wrapped).
    llm_block = ((typed.get("method") or {}).get("llm")) or {}
    primary = llm_block.get("primary")
    if isinstance(primary, dict):
        descriptor_component_id = primary.get("raw")
    elif isinstance(primary, str):
        descriptor_component_id = primary
    else:
        descriptor_component_id = None
    component_id = reflection_component_id or descriptor_component_id
    if not component_id:
        raise RuntimeError(
            f"analyst {analyst_id!r} has no resolvable LLM component "
            "(method.llm.primary unset)"
        )
    max_tokens = _scalar_option(llm_block.get("max_tokens"), default=1024)
    temperature = _scalar_option(llm_block.get("temperature"), default=0.2)

    handler = await build_llm_handler_from_stack_component(
        component_id,
        registry_client=registry,
        secrets_resolve=_secrets_resolve,
    )
    return {
        "handler": handler,
        "model": str(component_id),
        "component_id": str(component_id),
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "pg": pg,
        "registry": registry,
    }


async def _teardown_handler(state: dict[str, Any]) -> None:
    """Close the handler's httpx client + pg pool. Runs on the bridge loop."""
    handler = state.get("handler")
    if handler is not None and hasattr(handler, "on_deactivate"):
        try:
            await handler.on_deactivate(None)  # closes the lazily-opened client
        except Exception:  # pragma: no cover
            pass
    registry = state.get("registry")
    if registry is not None and hasattr(registry, "aclose"):
        try:
            await registry.aclose()
        except Exception:  # pragma: no cover
            pass
    pg = state.get("pg")
    if pg is not None and hasattr(pg, "close"):
        try:
            await pg.close()
        except Exception:  # pragma: no cover
            pass
