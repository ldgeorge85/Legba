"""SLM provider abstraction for the subconscious service.

Supports two backends:
- vLLM (OpenAI-compatible API) with guided_json for constrained decoding
- Anthropic (Haiku) with tool_use for structured output

Both return parsed dicts from the SLM's JSON output.
"""

from __future__ import annotations

import abc
import json
import logging
from typing import Any

import httpx

from .config import SubconsciousConfig

logger = logging.getLogger("legba.subconscious.provider")


class SLMError(Exception):
    """Raised when the SLM returns an error or unparseable response."""

    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class BaseSLMProvider(abc.ABC):
    """Abstract base class for SLM providers."""

    @abc.abstractmethod
    async def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
    ) -> dict:
        """Send a prompt to the SLM and return the parsed JSON response.

        Args:
            prompt: The user prompt text.
            system: Optional system prompt.
            json_schema: If provided, constrain output to this JSON schema.
                         For vLLM this uses guided_json; for Anthropic this
                         uses tool_use.

        Returns:
            Parsed dict from the SLM's JSON response.

        Raises:
            SLMError: On API errors or unparseable responses.
        """
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the HTTP client."""
        ...


class VLLMSLMProvider(BaseSLMProvider):
    """SLM provider using vLLM's OpenAI-compatible /v1/chat/completions.

    Uses the guided_json parameter for constrained decoding when a
    JSON schema is provided.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.1,
        timeout: int = 60,
        auth_user: str = "",
        auth_pass: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._auth_user = auth_user
        self._auth_pass = auth_pass
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self.base_url,
                "headers": {"Content-Type": "application/json"},
                "timeout": httpx.Timeout(self.timeout),
            }
            # Basic auth (Caddy-proxied endpoints) takes precedence over Bearer
            if self._auth_user and self._auth_pass:
                kwargs["auth"] = httpx.BasicAuth(self._auth_user, self._auth_pass)
            elif self.api_key:
                kwargs["headers"]["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
    ) -> dict:
        client = await self._get_client()

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }

        # Use guided_json for constrained decoding when schema is provided
        if json_schema is not None:
            payload["guided_json"] = json_schema

        logger.debug(
            "vLLM SLM request: model=%s, prompt_len=%d, guided_json=%s",
            self.model, len(prompt), json_schema is not None,
        )

        try:
            response = await client.post("/v1/chat/completions", json=payload)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise SLMError(f"vLLM connection error: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:1000]
            logger.error("vLLM SLM %d: %s", response.status_code, body)
            raise SLMError(
                f"vLLM API error {response.status_code}",
                status_code=response.status_code,
                body=body,
            )

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise SLMError("vLLM returned no choices")

        content = choices[0].get("message", {}).get("content", "") or ""

        logger.debug(
            "vLLM SLM response: finish_reason=%s, content_len=%d",
            choices[0].get("finish_reason", "unknown"), len(content),
        )

        # Parse JSON from content
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            # Try to extract JSON from markdown fences
            stripped = _extract_json(content)
            if stripped is not None:
                return stripped
            logger.warning("Failed to parse SLM JSON response: %s", content[:500])
            raise SLMError(f"Unparseable SLM response: {exc}") from exc

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class AnthropicSLMProvider(BaseSLMProvider):
    """SLM provider using Anthropic's /v1/messages with tool_use.

    Uses the tool_use pattern to get structured output: defines a single
    tool whose input_schema matches the desired JSON schema, and the
    model is forced to call it via tool_choice.
    """

    ANTHROPIC_API_URL = "https://api.anthropic.com"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-20241022",
        max_tokens: int = 2048,
        temperature: float = 0.1,
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
    ) -> dict:
        client = await self._get_client()

        messages = [{"role": "user", "content": prompt}]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        if system:
            payload["system"] = system

        # Use tool_use pattern for structured output
        if json_schema is not None:
            tool_name = "structured_output"
            payload["tools"] = [
                {
                    "name": tool_name,
                    "description": "Output the structured analysis result",
                    "input_schema": json_schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": tool_name}

        logger.debug(
            "Anthropic SLM request: model=%s, prompt_len=%d, tool_use=%s",
            self.model, len(prompt), json_schema is not None,
        )

        try:
            response = await client.post("/v1/messages", json=payload)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise SLMError(f"Anthropic connection error: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:1000]
            logger.error("Anthropic SLM %d: %s", response.status_code, body)
            raise SLMError(
                f"Anthropic API error {response.status_code}",
                status_code=response.status_code,
                body=body,
            )

        data = response.json()

        # If tool_use was requested, extract tool input from content blocks
        if json_schema is not None:
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    return block.get("input", {})
            # Fallback: try to parse text content as JSON
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        stripped = _extract_json(text)
                        if stripped is not None:
                            return stripped
            raise SLMError("Anthropic tool_use response contained no tool_use block")

        # No schema requested — parse text content as JSON
        content_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content_parts.append(block.get("text", ""))
        content = "\n".join(content_parts)

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            stripped = _extract_json(content)
            if stripped is not None:
                return stripped
            logger.warning("Failed to parse Anthropic SLM JSON: %s", content[:500])
            raise SLMError(f"Unparseable Anthropic response: {exc}") from exc

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


def _extract_json(text: str) -> dict | None:
    """Try to extract JSON from markdown code fences or raw text."""
    import re
    # Try ```json ... ``` pattern
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try to find first { ... } or [ ... ] block
    for start, end in [("{", "}"), ("[", "]")]:
        idx_start = text.find(start)
        idx_end = text.rfind(end)
        if idx_start != -1 and idx_end > idx_start:
            try:
                return json.loads(text[idx_start : idx_end + 1])
            except json.JSONDecodeError:
                pass
    return None


def create_provider(config: SubconsciousConfig) -> BaseSLMProvider:
    """Factory function to create an SLM provider from config.

    Args:
        config: SubconsciousConfig instance.

    Returns:
        An initialized BaseSLMProvider (VLLMSLMProvider or AnthropicSLMProvider).
    """
    if config.llm_provider == "anthropic":
        return AnthropicSLMProvider(
            api_key=config.llm_api_key,
            model=config.llm_model,
            max_tokens=config.max_tokens,
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
        )
    else:
        # Default to vLLM
        return VLLMSLMProvider(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            max_tokens=config.max_tokens,
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
            auth_user=config.llm_auth_user,
            auth_pass=config.llm_auth_pass,
        )
