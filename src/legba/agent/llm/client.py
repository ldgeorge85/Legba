"""
Legba LLM Client

High-level client that combines the message formatter, LLM provider,
and tool call parser. Every LLM call is logged through the cycle logger.

Supports multiple providers (vLLM/GPT-OSS, Anthropic/Claude) via
the LLM_PROVIDER config. Provider-specific formatting is handled
transparently — the rest of the system sees the same interface.

Single-turn pattern: every LLM call sends exactly [system, user] messages.
No multi-turn conversation growth. Each step rebuilds the user message with
accumulated tool results.

Context management: sliding window keeps last N tool interactions in full,
condenses older steps to one-line summaries to prevent unbounded growth.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from typing import Any, Callable, Awaitable

import httpx

from .format import Message, to_chat_messages, to_anthropic_messages, format_tool_result
from .provider import VLLMProvider, LLMResponse, LLMApiError
from .tool_parser import parse_tool_calls, has_tool_call
from ..log import CycleLogger
from ..prompt import templates
from ...shared.config import LLMConfig


log = logging.getLogger(__name__)

# Maximum concurrent tool calls per step.
MAX_CONCURRENT_TOOLS = 4

# Number of recent tool steps to keep in full detail.
# Older steps are condensed to a one-line summary.
SLIDING_WINDOW_SIZE = 8

# Max characters to keep from a tool result in condensed form.
CONDENSED_RESULT_MAX_CHARS = 2000

# Max characters for a tool result in the full history.
MAX_TOOL_RESULT_CHARS = 30000


class WorkingMemory:
    """
    In-cycle scratchpad that tracks observations, tool results, and notes.

    Fed to re-grounding prompts, the REFLECT phase, and the forced-final prompt.
    Does NOT persist across cycles (that's episodic memory's job).
    """

    def __init__(self):
        self._entries: list[dict[str, str]] = []

    def add_tool_result(self, step: int, tool_name: str, args_summary: str, result_summary: str) -> None:
        self._entries.append({
            "step": str(step),
            "type": "tool",
            "tool": tool_name,
            "args": args_summary[:500],
            "result": result_summary[:800],
        })

    def add_note(self, note: str) -> None:
        self._entries.append({
            "type": "note",
            "content": note[:1000],
        })

    def summary(self) -> str:
        if not self._entries:
            return "(no observations yet)"
        lines = []
        for e in self._entries:
            if e["type"] == "tool":
                lines.append(f"  Step {e['step']}: {e['tool']}({e['args']}) -> {e['result']}")
            elif e["type"] == "note":
                lines.append(f"  Note: {e['content']}")
        return "\n".join(lines)

    def full_text(self) -> str:
        """Full text for reflection phase."""
        if not self._entries:
            return "(no observations recorded this cycle)"
        lines = []
        for e in self._entries:
            if e["type"] == "tool":
                lines.append(f"[Step {e['step']}] Called {e['tool']}({e['args']})\n  Result: {e['result']}")
            elif e["type"] == "note":
                lines.append(f"[Note] {e['content']}")
        return "\n\n".join(lines)


class LLMClient:
    """
    LLM client with logging proxy and tool call loop.

    Supports vLLM (GPT-OSS) and Anthropic (Claude) providers.
    Provider selection is based on config.provider ("vllm" or "anthropic").
    """

    def __init__(
        self,
        config: LLMConfig,
        logger: CycleLogger,
        provider: VLLMProvider | None = None,
    ):
        self.config = config
        self.logger = logger
        self.provider_type = config.provider  # "vllm" or "anthropic"

        if provider is not None:
            self.provider = provider
        elif config.provider == "anthropic":
            from .anthropic_provider import AnthropicProvider
            self.provider = AnthropicProvider(
                api_key=config.api_key,
                model=config.model,
                timeout=config.timeout,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
        else:
            self.provider = VLLMProvider(
                api_base=config.api_base,
                api_key=config.api_key,
                model=config.model,
                timeout=config.timeout,
                temperature=config.temperature,
                top_p=config.top_p,
            )

        self._embedding_client: httpx.AsyncClient | None = None
        self.working_memory = WorkingMemory()
        self.router = None  # Optional PromptRouter for hybrid LLM routing

    async def close(self) -> None:
        await self.provider.close()
        # Close escalation provider if router has one
        if self.router and self.router.escalation_provider:
            try:
                await self.router.escalation_provider.close()
            except Exception:
                pass
        if self._embedding_client:
            await self._embedding_client.aclose()
            self._embedding_client = None

    async def complete(
        self,
        messages: list[Message],
        purpose: str = "reason",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """
        Single completion call with logging.

        Formats messages per provider and sends to LLM.
        If a PromptRouter is attached, the router selects the provider
        based on the purpose (prompt name).
        """
        start = time.monotonic()

        # Determine which provider to use for this call
        active_provider = self.provider
        active_provider_type = self.provider_type
        if self.router:
            routed = self.router.route(purpose)
            if routed is not self.provider:
                active_provider = routed
                # Detect provider type from class name
                active_provider_type = (
                    "anthropic" if type(routed).__name__ == "AnthropicProvider"
                    else "vllm"
                )

        # Log full messages for debugging (no truncation)
        log.info("LLM call [%s]: system=%d chars, user=%d chars, msgs=%d, provider=%s",
                 purpose,
                 len(messages[0].content) if messages else 0,
                 sum(len(m.content) for m in messages[1:]),
                 len(messages),
                 active_provider_type)

        try:
            if active_provider_type == "anthropic":
                system_text, chat_msgs = to_anthropic_messages(messages)
                response = await active_provider.chat_complete(
                    messages=chat_msgs,
                    max_tokens=max_tokens or self.config.max_tokens,
                    temperature=temperature,
                    system=system_text,
                )
            else:
                chat_msgs = to_chat_messages(messages)
                response = await active_provider.chat_complete(
                    messages=chat_msgs,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            latency = (time.monotonic() - start) * 1000

            self.logger.log_llm_call(
                purpose=purpose,
                prompt={
                    "system": messages[0].content,
                    "user": "\n\n".join(m.content for m in messages[1:]),
                },
                response=response.content,
                finish_reason=response.finish_reason,
                usage=response.usage,
                latency_ms=latency,
                provider=active_provider_type,
            )

            return response

        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            self.logger.log_llm_call(
                purpose=purpose,
                prompt={
                    "system": messages[0].content,
                    "user": "\n\n".join(m.content for m in messages[1:]),
                },
                response="",
                finish_reason="error",
                usage={},
                latency_ms=latency,
                error=str(e),
            )
            raise

    async def reason_with_tools(
        self,
        messages: list[Message],
        tool_executor: Callable[[str, dict], Awaitable[Any]],
        purpose: str = "reason",
        max_steps: int = 20,
        stop_check: Callable[[], bool] | None = None,
    ) -> tuple[str, list[Message]]:
        """
        Run the REASON->ACT loop with single-turn pattern.

        Each step sends exactly [system, user] to the LLM. No multi-turn
        conversation growth. Tool results are accumulated into the user
        message with sliding window condensation.

        Args:
            messages: Initial [system, user] messages from the assembler
            tool_executor: async function(tool_name, arguments) -> result
            purpose: Logging label for this reasoning chain
            max_steps: Maximum tool call iterations
            stop_check: Optional callable returning True for graceful shutdown

        Returns:
            (final_response_text, history_messages) where history_messages
            contains synthetic Messages for action counting by the caller.
        """
        # Extract system message (stays constant across all steps — includes
        # tool defs + calling instructions in the instructions-first pattern).
        system_msg = messages[0]

        # Combine all non-system messages into initial user content.
        # User message is data-only: CONTEXT DATA / data / END CONTEXT / task.
        initial_user_content = "\n\n".join(m.content for m in messages[1:])

        # Split on the context end separator so subsequent steps can inject
        # tool history and working memory between the data and the act instruction.
        end_marker = templates.CONTEXT_END_SEPARATOR
        marker_idx = initial_user_content.find(end_marker)
        if marker_idx >= 0:
            # base_context = everything through --- END CONTEXT ---
            base_context = initial_user_content[:marker_idx + len(end_marker)]
        else:
            base_context = initial_user_content

        # Track tool history for sliding window
        # Each entry: {"step": int, "tool": str, "args": str, "result": str, "full_result": str}
        tool_history: list[dict[str, str]] = []

        # Synthetic message list for the caller (action counting)
        history_messages: list[Message] = list(messages)

        step = 0
        consecutive_empty = 0  # track consecutive no-tool-call responses
        consecutive_api_errors = 0  # track consecutive LLM API failures
        override_prompt: str | None = None  # set when a retry needs a custom prompt

        MAX_FORMAT_RETRIES = 2   # re-prompt attempts on unparseable responses
        MAX_API_RETRIES = 2      # retry attempts on transient LLM API errors

        while step < max_steps:
            if stop_check and stop_check():
                self.working_memory.add_note(
                    "Graceful shutdown: supervisor requested wrap-up. "
                    "Exiting tool loop to proceed to REFLECT and PERSIST."
                )
                break

            step += 1

            # Build messages for this step
            if override_prompt is not None:
                # Retry with a custom final prompt (format correction or reground)
                user_content = self._build_step_message(
                    base_context=base_context,
                    tool_history=tool_history,
                    final_prompt=override_prompt,
                )
                step_msgs = [system_msg, Message(role="user", content=user_content)]
                override_prompt = None
            elif step == 1:
                # First step: use initial messages as-is (full context)
                step_msgs = [system_msg, Message(role="user", content=initial_user_content)]
            else:
                # Subsequent steps: lean user message with tool history
                user_content = self._build_step_message(
                    base_context=base_context,
                    tool_history=tool_history,
                )
                step_msgs = [system_msg, Message(role="user", content=user_content)]

            # Get completion — retry transient API errors before giving up
            try:
                response = await self.complete(
                    step_msgs,
                    purpose=f"{purpose}_step_{step}",
                )
                consecutive_api_errors = 0  # reset on success
            except LLMApiError as e:
                consecutive_api_errors += 1
                if consecutive_api_errors <= MAX_API_RETRIES:
                    log.warning(
                        "LLM API error at step %d (attempt %d/%d): %s. Retrying.",
                        step, consecutive_api_errors, MAX_API_RETRIES, e,
                    )
                    self.working_memory.add_note(
                        f"LLM API error at step {step} (attempt {consecutive_api_errors}): {e}. Retrying."
                    )
                    step -= 1  # don't count the failed attempt against step budget
                    await asyncio.sleep(min(2 ** consecutive_api_errors, 10))
                    continue
                else:
                    self.working_memory.add_note(
                        f"LLM API error at step {step} after {MAX_API_RETRIES} retries: {e}. "
                        "Exiting tool loop to proceed to REFLECT and PERSIST."
                    )
                    log.warning("LLM API error at step %d, retries exhausted, breaking tool loop: %s", step, e)
                    break

            raw = response.content

            # Parse tool calls
            tool_calls = parse_tool_calls(raw)

            if not tool_calls:
                consecutive_empty += 1

                if has_tool_call(raw):
                    # Response looks like it intended a tool call but JSON
                    # didn't parse — re-prompt with format correction
                    log.warning(
                        "Step %d: unparseable tool call detected (attempt %d/%d). "
                        "Raw (first 500 chars): %s",
                        step, consecutive_empty, MAX_FORMAT_RETRIES, raw[:500],
                    )
                    if consecutive_empty <= MAX_FORMAT_RETRIES:
                        override_prompt = templates.FORMAT_RETRY_PROMPT
                        continue  # costs one step for the retry
                    else:
                        log.warning("Step %d: format retries exhausted after %d attempts, exiting tool loop",
                                    step, MAX_FORMAT_RETRIES)
                        break
                else:
                    # No tool-like content at all — nudge with reground
                    log.info(
                        "Step %d: no tool call in response (attempt %d, first 300 chars): %s",
                        step, consecutive_empty, raw[:300],
                    )
                    if consecutive_empty <= MAX_FORMAT_RETRIES:
                        override_prompt = (
                            "You did not produce a tool call. Continue executing your plan. "
                            'Output one JSON object: {"actions": [...]}. '
                            "If you are finished, call cycle_complete."
                        )
                        continue  # costs one step for the nudge
                    else:
                        # Multiple consecutive empty responses — model genuinely done
                        log.info("Step %d: model produced no tool calls %d times, exiting loop",
                                 step, consecutive_empty)
                        history_messages.append(Message(role="assistant", content=raw))
                        return raw, history_messages
            else:
                consecutive_empty = 0  # reset on successful parse

            # Check for cycle_complete signal
            if any(tc.tool_name == "cycle_complete" for tc in tool_calls):
                cc = next(tc for tc in tool_calls if tc.tool_name == "cycle_complete")
                reason = cc.arguments.get("reason", "plan completed")
                self.working_memory.add_note(f"Cycle complete: {reason}")
                break

            # Cap concurrent calls
            tool_calls = tool_calls[:MAX_CONCURRENT_TOOLS]

            # Deduplicate identical tool calls
            _seen_keys: set[str] = set()
            _deduped: list = []
            for tc in tool_calls:
                key = f"{tc.tool_name}:{_json.dumps(tc.arguments, sort_keys=True, default=str)}"
                if key not in _seen_keys:
                    _seen_keys.add(key)
                    _deduped.append(tc)
                else:
                    self.working_memory.add_note(
                        f"Skipped duplicate tool call: {tc.tool_name}"
                    )
            tool_calls = _deduped

            # Record the assistant response in history
            history_messages.append(Message(role="assistant", content=raw))

            # Execute all tool calls concurrently
            async def _exec(tc):
                try:
                    return await tool_executor(tc.tool_name, tc.arguments)
                except Exception as e:
                    return f"Error executing {tc.tool_name}: {e}"

            results = await asyncio.gather(*[_exec(tc) for tc in tool_calls])

            # Process results
            for tc, result in zip(tool_calls, results):
                result_str = str(result) if not isinstance(result, str) else result

                # Truncate oversized tool results
                if len(result_str) > MAX_TOOL_RESULT_CHARS:
                    truncated_note = (
                        f"\n\n[TRUNCATED — result was {len(result_str):,} chars, "
                        f"showing first {MAX_TOOL_RESULT_CHARS:,}]"
                    )
                    result_str = result_str[:MAX_TOOL_RESULT_CHARS] + truncated_note

                # Add to tool history for next step's user message
                args_summary = _summarize_args(tc.arguments)
                tool_history.append({
                    "step": str(step),
                    "tool": tc.tool_name,
                    "args": args_summary,
                    "result": result_str[:CONDENSED_RESULT_MAX_CHARS],
                    "full_result": result_str,
                })

                # Add synthetic tool result message for caller's action counting
                history_messages.append(
                    Message(role="user", content=format_tool_result(tc.tool_name, result_str))
                )

                # Update working memory
                self.working_memory.add_tool_result(
                    step, tc.tool_name, args_summary,
                    result_str[:CONDENSED_RESULT_MAX_CHARS],
                )

        # Exhausted step budget or cycle_complete — force a final response
        user_content = self._build_step_message(
            base_context=base_context,
            tool_history=tool_history,
            final_prompt=templates.BUDGET_EXHAUSTED_PROMPT,
        )
        final_msgs = [system_msg, Message(role="user", content=user_content)]

        response = await self.complete(
            final_msgs,
            purpose=f"{purpose}_final_forced",
        )

        return response.content, history_messages

    def _build_step_message(
        self,
        base_context: str,
        tool_history: list[dict[str, str]],
        final_prompt: str = "",
    ) -> str:
        """
        Build the user message for a tool loop step.

        Data-only pattern: base context (goals, memories, etc.) + tool history +
        working memory + short act instruction. Tool defs and calling format are
        in the system message (instructions-first pattern).
        """
        parts: list[str] = []

        # Base context (data sections through --- END CONTEXT ---)
        parts.append(base_context)

        # Tool history with sliding window
        if tool_history:
            parts.append(self._format_tool_history(tool_history))

        # Working memory
        wm = self.working_memory.summary()
        if wm != "(no observations yet)":
            parts.append(f"## Working Memory\n{wm}")

        # Action instruction (short — format rules are in system)
        if final_prompt:
            parts.append(final_prompt)
        else:
            parts.append('Continue executing your plan. Output one JSON object: {"actions": [...]}')

        return "\n\n".join(parts)

    def _format_tool_history(self, tool_history: list[dict[str, str]]) -> str:
        """Format tool history with sliding window condensation."""
        n = len(tool_history)
        if n == 0:
            return "## Progress So Far\n(no actions taken yet)"

        window_start = max(0, n - SLIDING_WINDOW_SIZE)
        lines = ["## Progress So Far"]

        # Condensed older steps
        if window_start > 0:
            lines.append("### Earlier steps (condensed)")
            for entry in tool_history[:window_start]:
                lines.append(
                    f"- Step {entry['step']}: {entry['tool']}({entry['args']}) "
                    f"-> {entry['result'][:500]}"
                )

        # Recent steps in full detail
        if window_start < n:
            lines.append("### Recent steps")
            for entry in tool_history[window_start:]:
                lines.append(
                    f"**Step {entry['step']}: {entry['tool']}({entry['args']})**\n"
                    f"Result:\n{entry['full_result']}"
                )

        return "\n\n".join(lines)

    async def generate_embedding(self, text: str) -> list[float]:
        """Generate an embedding vector using the embedding model.

        Uses a separate HTTP client for embeddings — Anthropic has no
        embedding API, so this always hits the vLLM/OpenAI endpoint.
        """
        if self._embedding_client is None:
            base = (self.config.embedding_api_base or self.config.api_base).rstrip("/")
            key = self.config.embedding_api_key or self.config.api_key
            self._embedding_client = httpx.AsyncClient(
                base_url=base,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30),
            )

        response = await self._embedding_client.post(
            "/embeddings",
            json={
                "model": self.config.embedding_model,
                "input": text,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["data"][0]["embedding"]


def _summarize_args(args: dict) -> str:
    """Create a brief summary of tool arguments."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)
