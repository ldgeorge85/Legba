# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.redis — async redis client.

Lightweight wrapper over `redis.asyncio.Redis`. Per L-091 §2.6 we also:
  * apply a TTL to embed-cache keys (`legba:*_embed:*`),
  * set `maxmemory-policy` to `allkeys-lru` at connect.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore[assignment]

from .config import RedisConfig

logger = logging.getLogger(__name__)


class RedisStore:
    def __init__(self, cfg: RedisConfig):
        if Redis is None:  # pragma: no cover
            raise RuntimeError("redis is not installed")
        self._cfg = cfg
        self._client: Redis | None = None

    @classmethod
    def from_env(cls) -> "RedisStore":
        return cls(RedisConfig.from_env())

    @property
    def client(self) -> "Redis":
        if self._client is None:
            raise RuntimeError("RedisStore not connected")
        return self._client

    @property
    def cfg(self) -> RedisConfig:
        return self._cfg

    async def connect(self, *, apply_policy: bool = True) -> None:
        if self._client is not None:
            return
        self._client = Redis(
            host=self._cfg.host,
            port=self._cfg.port,
            db=self._cfg.db,
            password=self._cfg.password,
            decode_responses=False,
        )
        if apply_policy:
            try:
                await self._client.config_set(
                    "maxmemory-policy", self._cfg.maxmemory_policy
                )
            except Exception as exc:
                logger.debug("redis CONFIG SET refused: %s", exc)

    async def close(self) -> None:
        if self._client is not None:
            # redis-py 5.0.1+ deprecated close() in favour of aclose() for
            # the async client; honour the rename. Older versions still
            # have close() as a fallback.
            closer = getattr(self._client, "aclose", None) or self._client.close
            await closer()
            self._client = None

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def cache_embed(self, key: str, value: bytes) -> None:
        """Set an embed-cache key with the configured TTL."""
        await self.client.setex(key, self._cfg.embed_cache_ttl_seconds, value)
