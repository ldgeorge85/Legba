"""HTTP client for the Legba models service (GPU inference).

Provides translation, classification, relation extraction, and summarization
via the legba-models FastAPI service. Graceful degradation — if the service
is unavailable, all methods return safe defaults and ingestion continues
with existing regex classification and raw titles.

Uses httpx.DigestAuth for Caddy's digest auth challenge.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class ModelsClient:
    """Async HTTP client for the legba-models inference service."""

    def __init__(self):
        base_url = os.getenv("MODELS_API_URL", "").rstrip("/")
        if not base_url:
            self._http = None
            self._available = False
            return

        auth_user = os.getenv("MODELS_API_USER", "")
        auth_pass = os.getenv("MODELS_API_PASS", "")

        auth = httpx.BasicAuth(auth_user, auth_pass) if auth_user else None

        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            timeout=httpx.Timeout(30.0),
        )
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def check_health(self) -> bool:
        """Check if the models service is healthy. Call periodically."""
        if not self._http:
            return False
        try:
            resp = await self._http.get("/health")
            data = resp.json()
            self._available = resp.status_code == 200 and data.get("status") == "ok"
            if self._available:
                gpu_mem = data.get("gpu_memory", {})
                logger.info("Models service healthy (GPU: %sMB allocated)", gpu_mem.get("allocated_mb", "?"))
        except Exception as e:
            self._available = False
            logger.debug("Models health check failed: %s", e)
        return self._available

    async def translate(self, text: str, source_lang: str, target_lang: str = "en") -> str | None:
        """Translate text. Returns translated text or None on failure."""
        if not self._available:
            return None
        try:
            resp = await self._http.post("/translate", json={
                "text": text[:2000],
                "source_lang": source_lang,
                "target_lang": target_lang,
            })
            if resp.status_code == 200:
                return resp.json()["translated"]
        except Exception as e:
            logger.debug("Translation failed: %s", e)
        return None

    async def classify(self, text: str) -> tuple[str, float]:
        """Classify text into a category. Returns (category, confidence)."""
        if not self._available:
            return "other", 0.0
        try:
            resp = await self._http.post("/classify", json={"text": text[:1000]})
            if resp.status_code == 200:
                data = resp.json()
                return data["category"], data.get("confidence", 0.0)
        except Exception as e:
            logger.debug("Classification failed: %s", e)
        return "other", 0.0

    async def extract_triples(self, text: str) -> list[dict]:
        """Extract relation triples from text. Returns list of {subject, predicate, object}."""
        if not self._available:
            return []
        try:
            resp = await self._http.post("/extract", json={"text": text[:2000]})
            if resp.status_code == 200:
                return resp.json().get("triples", [])
        except Exception as e:
            logger.debug("Extraction failed: %s", e)
        return []

    async def summarize(self, texts: list[str], max_length: int = 64) -> str | None:
        """Summarize multiple texts into one sentence. Returns summary or None."""
        if not self._available:
            return None
        try:
            resp = await self._http.post("/summarize", json={
                "texts": texts,
                "max_length": max_length,
            })
            if resp.status_code == 200:
                return resp.json().get("summary")
        except Exception as e:
            logger.debug("Summarization failed: %s", e)
        return None

    async def close(self):
        if self._http:
            await self._http.aclose()
