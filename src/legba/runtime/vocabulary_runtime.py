# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runtime's vocabulary cache for write-time tag binding (W7 TAG-1).

The runtime holds no database copy of the vocabulary: it reads the registry's
index route (``GET /api/v1/registry/vocabulary`` — VOCAB-1) and then each
listed family (``GET /api/v1/registry/vocabulary/{family}``), and keeps the
result as an immutable ``data.tags.VocabularyIndex`` for
``LEGBA_TAG_VOCABULARY_TTL_SECONDS`` (default 300 s).

Degrade, per family and never a crash:

  * a family the registry reports in ``families_unloaded`` — or whose own read
    fails or returns a non-list — is UNLOADED in the snapshot: every tag in it
    binds ``unclassified``;
  * the index read itself failing keeps the previous snapshot (a registry blip
    must not flip every bound tag to unclassified); with no previous snapshot
    the index is EMPTY (every family unloaded). A failed read is retried after
    ``min(ttl, 30 s)``, not a full TTL.

Producers never do I/O: `RuntimeVocabulary.index()` returns the snapshot and,
when it is stale, starts ONE background refresh. Only the very first read
after bring-up waits (bounded) for the priming read, so the rows written in
the first instant are not stamped against an empty index.

Wiring: ``install_tag_vocabulary(registry_client)`` — one call at runtime
bring-up (``dapr_host.bring_up_production_runtime``) — builds the cache over
the client's ``request_json``, installs it as the process's
``data.tags`` provider, starts the priming read, and returns the client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from ..data.tags import EMPTY_INDEX, VocabularyIndex, install_vocabulary_provider

logger = logging.getLogger(__name__)

TTL_ENV = "LEGBA_TAG_VOCABULARY_TTL_SECONDS"
DEFAULT_TTL_SECONDS = 300.0
#: Retry delay after a failed read (capped by the TTL).
RETRY_AFTER_FAILURE_SECONDS = 30.0
#: How long the first read after bring-up waits for the priming read.
FIRST_READ_WAIT_SECONDS = 10.0
VOCABULARY_PATH = "/api/v1/registry/vocabulary"

RequestJson = Callable[..., Awaitable[tuple[int, Any]]]


def ttl_seconds_from_env() -> float:
    raw = os.environ.get(TTL_ENV)
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        v = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", TTL_ENV, raw, DEFAULT_TTL_SECONDS)
        return DEFAULT_TTL_SECONDS
    return v if v > 0 else DEFAULT_TTL_SECONDS


class VocabularyReadError(RuntimeError):
    """The registry's vocabulary index could not be read."""


class RuntimeVocabulary:
    """TTL snapshot of the registry vocabulary (see module doc)."""

    def __init__(
        self,
        request_json: RequestJson,
        *,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._request = request_json
        self._ttl = float(ttl_seconds) if ttl_seconds is not None else ttl_seconds_from_env()
        self._clock = clock
        self._wall = wall_clock
        self._index: VocabularyIndex = EMPTY_INDEX
        self._loaded = False
        self._next_due: float | None = None  # None → due now
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def snapshot(self) -> VocabularyIndex:
        return self._index

    def _stale(self) -> bool:
        return self._next_due is None or self._clock() >= self._next_due

    async def refresh(self) -> VocabularyIndex:
        """Read the index route and every listed family; install the result."""
        try:
            status, body = await self._request("GET", VOCABULARY_PATH)
        except Exception as exc:
            raise VocabularyReadError(f"index read failed: {exc}") from exc
        if status != 200 or not isinstance(body, dict):
            raise VocabularyReadError(f"index read returned {status}")
        unloaded: set[str] = {
            u["family"] for u in body.get("families_unloaded") or ()
            if isinstance(u, dict) and isinstance(u.get("family"), str)
        }
        entries: list[dict[str, Any]] = []
        for fam_row in body.get("families") or ():
            fam = fam_row.get("family") if isinstance(fam_row, dict) else None
            if not isinstance(fam, str) or fam in unloaded:
                continue
            try:
                st, rows = await self._request(
                    "GET", f"{VOCABULARY_PATH}/{quote(fam, safe='')}",
                )
            except Exception as exc:
                logger.warning("vocabulary_runtime.family_read_failed family=%s err=%s", fam, exc)
                unloaded.add(fam)
                continue
            if st != 200 or not isinstance(rows, list):
                logger.warning("vocabulary_runtime.family_read_failed family=%s status=%s", fam, st)
                unloaded.add(fam)
                continue
            entries.extend(r for r in rows if isinstance(r, dict))
        idx = VocabularyIndex.from_entries(entries, unloaded=unloaded, loaded_at=self._wall())
        self._index = idx
        self._loaded = True
        self._next_due = self._clock() + self._ttl
        self.last_error = None
        if unloaded:
            logger.warning("vocabulary_runtime.families_unloaded %s", sorted(unloaded))
        return idx

    async def _refresh_guarded(self) -> None:
        try:
            idx = await self.refresh()
            logger.info(
                "vocabulary_runtime.refreshed families=%d unloaded=%d",
                len(idx.values), len(idx.unloaded),
            )
        except Exception as exc:
            self.last_error = str(exc)[:300]
            self._next_due = self._clock() + min(self._ttl, RETRY_AFTER_FAILURE_SECONDS)
            logger.warning(
                "vocabulary_runtime.refresh_failed err=%s — keeping the %s snapshot",
                exc, "previous" if self._loaded else "EMPTY",
            )

    def start_refresh(self) -> asyncio.Task | None:
        """Start one background refresh if none is in flight (needs a running
        loop; without one, nothing starts)."""
        if self._task is not None and not self._task.done():
            return self._task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._task = loop.create_task(self._refresh_guarded())
        return self._task

    async def index(self) -> VocabularyIndex:
        """The snapshot; starts a background refresh when stale. Before the
        first successful read, waits (bounded) for the in-flight read."""
        if self._stale():
            task = self.start_refresh()
            if not self._loaded and task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), FIRST_READ_WAIT_SECONDS)
                except Exception:
                    pass
        return self._index


_installed: RuntimeVocabulary | None = None


def install_tag_vocabulary(registry_client: Any, *, ttl_seconds: float | None = None) -> Any:
    """Install the runtime vocabulary cache over ``registry_client`` as the
    process's tag-binding provider, start the priming read, return the
    client (so bring-up wraps the construction line)."""
    global _installed
    rv = RuntimeVocabulary(registry_client.request_json, ttl_seconds=ttl_seconds)
    _installed = rv
    install_vocabulary_provider(rv.index)
    rv.start_refresh()
    logger.info("vocabulary_runtime.installed ttl_s=%.0f", rv.ttl_seconds)
    return registry_client


def installed_runtime_vocabulary() -> RuntimeVocabulary | None:
    return _installed


def uninstall_tag_vocabulary() -> None:
    global _installed
    _installed = None
    install_vocabulary_provider(None)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "RuntimeVocabulary",
    "TTL_ENV",
    "VOCABULARY_PATH",
    "VocabularyReadError",
    "install_tag_vocabulary",
    "installed_runtime_vocabulary",
    "ttl_seconds_from_env",
    "uninstall_tag_vocabulary",
]
