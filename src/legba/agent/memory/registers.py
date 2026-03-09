"""
Redis Registers

Fast key-value store for cycle state, counters, flags, and scratch variables.
Loaded every cycle, cheap to access.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis


class RegisterStore:
    """
    Redis-backed register store.

    Provides typed get/set for common register patterns:
    scalars, counters, flags, and JSON objects.
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0, password: str | None = None):
        self._redis: redis.Redis | None = None
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._prefix = "legba:"
        # In-memory fallback if Redis is unavailable
        self._fallback: dict[str, Any] = {}
        self._using_fallback = False

    async def connect(self) -> None:
        try:
            self._redis = redis.Redis(
                host=self._host,
                port=self._port,
                db=self._db,
                password=self._password,
                decode_responses=True,
            )
            await self._redis.ping()
            self._using_fallback = False
        except Exception:
            self._redis = None
            self._using_fallback = True

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    # --- Scalar operations ---

    async def get(self, name: str, default: str | None = None) -> str | None:
        if self._using_fallback:
            return self._fallback.get(name, default)
        try:
            val = await self._redis.get(self._key(name))
            return val if val is not None else default
        except Exception:
            return self._fallback.get(name, default)

    async def set(self, name: str, value: str) -> None:
        if self._using_fallback:
            self._fallback[name] = value
            return
        try:
            await self._redis.set(self._key(name), value)
        except Exception:
            self._fallback[name] = value

    # --- Counter operations ---

    async def incr(self, name: str) -> int:
        if self._using_fallback:
            val = int(self._fallback.get(name, 0)) + 1
            self._fallback[name] = str(val)
            return val
        try:
            return await self._redis.incr(self._key(name))
        except Exception:
            val = int(self._fallback.get(name, 0)) + 1
            self._fallback[name] = str(val)
            return val

    async def get_int(self, name: str, default: int = 0) -> int:
        val = await self.get(name)
        if val is None:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    # --- Flag operations ---

    async def set_flag(self, name: str, value: bool = True) -> None:
        await self.set(name, "1" if value else "0")

    async def get_flag(self, name: str) -> bool:
        val = await self.get(name)
        return val == "1"

    # --- JSON operations ---

    async def set_json(self, name: str, value: Any) -> None:
        await self.set(name, json.dumps(value, default=str))

    async def get_json(self, name: str, default: Any = None) -> Any:
        val = await self.get(name)
        if val is None:
            return default
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return default

    # --- Bulk operations ---

    async def get_all_registers(self) -> dict[str, str]:
        """Get all registers (for debugging/logging)."""
        if self._using_fallback:
            return dict(self._fallback)
        try:
            keys = []
            async for key in self._redis.scan_iter(match=f"{self._prefix}*"):
                keys.append(key)
            if not keys:
                return {}
            values = await self._redis.mget(keys)
            return {
                k.removeprefix(self._prefix): v
                for k, v in zip(keys, values)
                if v is not None
            }
        except Exception:
            return dict(self._fallback)
