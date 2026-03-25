"""
Legba LLM Provider

Connects to vLLM (or any OpenAI-compatible endpoint).

Uses /v1/chat/completions matching ernie's proven pattern:
- Single user message (system+user combined by format.py)
- reasoning: high in content (set by templates/assembler)
- Temperature 1.0
- No max_tokens (server manages budget)
- No Harmony token wrapping (chat template handles tokenization)
- No assistant primer or stop tokens
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .format import safe_response_body as _safe_response_body, strip_harmony_response

log = logging.getLogger(__name__)


class LLMApiError(Exception):
    """Raised when the LLM API returns a non-retryable error."""

    def __init__(self, status_code: int, body: str, msg_count: int, total_chars: int):
        self.status_code = status_code
        self.body = body
        self.msg_count = msg_count
        self.total_chars = total_chars
        super().__init__(
            f"LLM API {status_code}: messages={msg_count}, chars={total_chars}, body={body[:500]}"
        )


@dataclass
class LLMResponse:
    """Response from an LLM completion call."""

    content: str
    finish_reason: str  # "stop", "length", etc.
    usage: dict[str, int]  # prompt_tokens, completion_tokens, total_tokens
    raw_response: dict[str, Any] | None = None


class VLLMProvider:
    """
    Provider for vLLM with OpenAI-compatible API.

    Matches ernie's proven pattern:
    - /v1/chat/completions
    - Single user message (no system role)
    - reasoning: high in content
    - Temperature 1.0, no max_tokens
    - No Harmony wrapping, no assistant primer, no stop tokens
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        timeout: int = 180,
        temperature: float = 1.0,
        top_p: float = 0.9,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.api_base,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
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
    ) -> LLMResponse:
        """
        Generate a completion via /v1/chat/completions.

        Messages are sent as-is (single user message from format.py).
        Temperature is always 1.0 for GPT-OSS.
        No max_tokens — server manages the budget.

        Retries on transient errors with exponential backoff.
        """
        client = await self._get_client()

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 1.0,  # GPT-OSS requires 1.0 for all work
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        # Log outbound payload (sans message content) for debugging
        log.info("vLLM request: model=%s, temperature=%s, max_tokens=%s, msg_count=%d, total_chars=%d",
                 payload.get("model"), payload.get("temperature"),
                 payload.get("max_tokens", "(not set)"),
                 len(messages),
                 sum(len(m.get("content", "")) for m in messages))

        retryable_codes = {429, 500, 502, 503}
        last_error: Exception | None = None
        total_chars = sum(len(m.get("content", "")) for m in messages)

        for attempt in range(max_retries + 1):
            try:
                response = await client.post("/chat/completions", json=payload)
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    log.warning("LLM connection error (attempt %d/%d), retrying in %ds: %s",
                                attempt + 1, max_retries + 1, wait, exc)
                    await asyncio.sleep(wait)
                    continue
                raise

            if response.status_code in retryable_codes and attempt < max_retries:
                body = _safe_response_body(response)
                wait = 2 ** attempt
                log.warning("LLM %d (attempt %d/%d), retrying in %ds: %s",
                            response.status_code, attempt + 1, max_retries + 1, wait, body)
                await asyncio.sleep(wait)
                continue

            if response.status_code >= 400:
                body = _safe_response_body(response)
                log.error("LLM API %d: total_chars=%d, body=%s",
                          response.status_code, total_chars, body)
                raise LLMApiError(response.status_code, body, len(messages), total_chars)

            data = response.json()
            choices = data.get("choices", [])

            # Handle vLLM reasoning mode multi-message output.
            # Reasoning mode produces N output messages (reasoning + final).
            # Normally 2, but occasionally >2 which previously caused errors.
            if len(choices) < 1:
                raise LLMApiError(
                    response.status_code,
                    f"Expected at least 1 output choice, but got {len(choices)}",
                    len(messages), total_chars,
                )
            if len(choices) > 1:
                log.warning("Got %d output choices instead of 1, concatenating extras", len(choices))
                combined_content = "\n".join(
                    c.get("message", {}).get("content", "") or "" for c in choices[1:]
                )
                # Merge extra choices content into the first choice
                first_content = choices[0].get("message", {}).get("content", "") or ""
                choices = [
                    {**choices[0], "message": {**choices[0].get("message", {}), "content": first_content + "\n" + combined_content}}
                ]

            choice = choices[0]
            raw_content = choice.get("message", {}).get("content", "") or ""

            # Strip any Harmony markers the model may emit in its output
            content = strip_harmony_response(raw_content)

            if raw_content and not content:
                log.warning("strip_harmony_response removed all content (%d chars)", len(raw_content))

            return LLMResponse(
                content=content,
                finish_reason=choice.get("finish_reason", "unknown"),
                usage=data.get("usage", {}),
                raw_response=data,
            )

        raise last_error or RuntimeError("LLM call failed after retries")
