"""
Anthropic Messages API Provider

Connects to Anthropic's /v1/messages endpoint for Claude models.

Key differences from vLLM/OpenAI:
- System prompt is a top-level field, not in messages array
- max_tokens is required
- Response uses content blocks (list), not a single string
- Token fields: input_tokens/output_tokens (not prompt_tokens/completion_tokens)
- stop_reason (not finish_reason), values: end_turn, max_tokens, stop_sequence
- Auth: x-api-key header (not Authorization: Bearer)
- No Harmony tokens in output
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .format import safe_response_body as _safe_response_body
from .provider import LLMResponse, LLMApiError

log = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# Map Anthropic stop_reason to normalized values
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


class AnthropicProvider:
    """
    Provider for Anthropic's Messages API (Claude models).

    Uses /v1/messages with proper system role separation.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        timeout: int = 300,
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat_complete(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        max_retries: int = 3,
        system: str | None = None,
    ) -> LLMResponse:
        """
        Generate a completion via Anthropic /v1/messages.

        Args:
            messages: User/assistant message dicts (no system role).
            max_tokens: Max output tokens (required by Anthropic, defaults to self.max_tokens).
            temperature: Sampling temperature (0.0-1.0).
            system: System prompt text (passed as top-level field).
        """
        client = await self._get_client()

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if stop:
            payload["stop_sequences"] = stop

        retryable_codes = {429, 500, 529}
        last_error: Exception | None = None
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if system:
            total_chars += len(system)

        for attempt in range(max_retries + 1):
            try:
                response = await client.post("/v1/messages", json=payload)
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    log.warning("Anthropic connection error (attempt %d/%d), retrying in %ds: %s",
                                attempt + 1, max_retries + 1, wait, exc)
                    await asyncio.sleep(wait)
                    continue
                raise

            if response.status_code in retryable_codes and attempt < max_retries:
                body = _safe_response_body(response)
                # Respect retry-after header on 429
                retry_after = response.headers.get("retry-after")
                wait = int(retry_after) if retry_after else 2 ** attempt
                log.warning("Anthropic %d (attempt %d/%d), retrying in %ds: %s",
                            response.status_code, attempt + 1, max_retries + 1, wait, body)
                await asyncio.sleep(wait)
                continue

            if response.status_code >= 400:
                body = _safe_response_body(response)
                log.error("Anthropic API %d: total_chars=%d, body=%s",
                          response.status_code, total_chars, body)
                raise LLMApiError(response.status_code, body, len(messages), total_chars)

            data = response.json()

            # Extract text from content blocks
            content_parts = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content_parts.append(block.get("text", ""))
            content = "\n".join(content_parts) if content_parts else ""

            # Map usage fields to normalized names
            raw_usage = data.get("usage", {})
            usage = {
                "prompt_tokens": raw_usage.get("input_tokens", 0),
                "completion_tokens": raw_usage.get("output_tokens", 0),
                "total_tokens": (
                    raw_usage.get("input_tokens", 0)
                    + raw_usage.get("output_tokens", 0)
                ),
            }

            # Normalize stop_reason
            stop_reason = data.get("stop_reason", "unknown")
            finish_reason = _STOP_REASON_MAP.get(stop_reason, stop_reason)

            return LLMResponse(
                content=content,
                finish_reason=finish_reason,
                usage=usage,
                raw_response=data,
            )

        raise last_error or RuntimeError("Anthropic call failed after retries")
