# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry's SHARED auth state in Redis — the sign-in lockout and the
consult RUN OWNERS (X4 GATE, W11-F76).

Two things about who-may-do-what were per process before this lane:

  * the sign-in failure counter (``users.LocalPasswordProvider``) reset on a
    restart and was not shared across replicas — ten guesses per process per
    window, not per email;
  * nothing at all recorded who started a consult RUN, so the SSE relay
    (``GET /consult/stream/{request_id}``) streamed any run's steps to any
    signed-in user holding its uuid4 — unguessable, but not access control.

Both now live in Redis, through the registry's existing client
(``legba.data.redis.RedisStore``, opened by :func:`open_auth_state` in the
app's lifespan). Each falls back to an in-process copy — loudly — when Redis
does not answer, and an app that never ran the lifespan (the tests' fixture
apps) uses the in-process copy alone.

RUN OWNERS. A run's owner of record is the FIRST principal that names its
``request_id``: the browser opens the step stream BEFORE it POSTs
(subscribe-before-publish, ``Consult.tsx``), so the stream's opener is the
run's owner, and ``POST /consult`` claims the same id for a client that did
not subscribe first. :func:`require_run_owner` claims (``SET NX``) and refuses
(403 ``not_row_owner``) a confined caller — a signed-in user — whose claim
lost to another principal. The operator, the fleet and dev mode are never
confined (they pass, and still claim, so a user cannot later claim an
operator's run). The owner is the AUDIT ACTOR (``user:<id>`` /
``operator:bearer:<sha12>``) — the value ``consult_sessions.principal`` holds
for the run's session.

REGISTRY-SLIM: the stdlib, fastapi's ``HTTPException`` and the
``session_auth`` / ``route_roles`` leaves; ``legba.data.redis`` is imported
inside :func:`open_auth_state` only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import HTTPException, status
from starlette.requests import HTTPConnection

from .. import users as _users
from . import route_roles as _rr
from . import session_auth as _sa

logger = logging.getLogger(__name__)

#: How long a run's owner of record is kept. Generous beside the run's own
#: lifetime (``consult_runs.invoke_timeout_seconds`` + the relay's grace): a
#: claim that outlives its run guards a stream nothing publishes on any more.
RUN_OWNER_TTL_S = 24 * 3600
RUN_OWNER_KEY_PREFIX = "legba:consult:run_owner:"
#: Bound on the in-process fallback map (oldest pruned first).
_LOCAL_MAX = 20_000

_client: Any | None = None
_store: Any | None = None


def install(client: Any | None) -> None:
    """Install (``None``: remove) the async Redis client the shared state uses."""
    global _client
    _client = client


def redis_client() -> Any | None:
    return _client


async def open_auth_state() -> None:
    """Lifespan start: open the registry's Redis client and install it here
    and as the sign-in lockout store. Never fails startup — an unreachable
    Redis degrades both to per-process, and every degraded call logs."""
    global _store
    try:
        from legba.data.redis import RedisStore

        store = RedisStore.from_env()
        # The registry never re-tunes the shared server's eviction policy.
        await store.connect(apply_policy=False)
    except Exception as exc:  # noqa: BLE001 — degrade, loudly
        logger.warning(
            "registry.auth_state.redis_unavailable err=%s — the sign-in lockout "
            "and consult run owners are PER PROCESS this boot", exc,
        )
        return
    _store = store
    install(store.client)
    _users.local_password_provider().use_failure_store(
        _users.RedisFailureStore(store.client),
    )


async def close_auth_state() -> None:
    """Lifespan end: uninstall and close what :func:`open_auth_state` opened."""
    global _store
    _users.local_password_provider().use_failure_store(None)
    install(None)
    store, _store = _store, None
    if store is not None:
        try:
            await store.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.debug("registry.auth_state.close_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Consult run owners
# ---------------------------------------------------------------------------


class _LocalOwners:
    """In-process ``request_id → (owner, expires_at)`` — the fallback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[str, tuple[str, float]] = {}

    def claim(self, request_id: str, owner: str) -> str:
        now = time.monotonic()
        with self._lock:
            held = self._owners.get(request_id)
            if held is not None and held[1] > now:
                return held[0]
            if len(self._owners) >= _LOCAL_MAX:
                for rid in [r for r, (_, exp) in self._owners.items() if exp <= now]:
                    del self._owners[rid]
                while len(self._owners) >= _LOCAL_MAX:
                    del self._owners[next(iter(self._owners))]
            self._owners[request_id] = (owner, now + RUN_OWNER_TTL_S)
            return owner

    def clear(self) -> None:
        with self._lock:
            self._owners.clear()


_local = _LocalOwners()


async def claim_run_owner(request_id: str, owner: str) -> str:
    """Make ``owner`` the owner of ``request_id`` unless one is on record;
    return the owner of record (shared through Redis when installed)."""
    client = _client
    if client is not None:
        key = f"{RUN_OWNER_KEY_PREFIX}{request_id}"
        try:
            for _ in range(2):  # a claim that expires between SET and GET retries once
                if await client.set(key, owner.encode("utf-8"), nx=True, ex=RUN_OWNER_TTL_S):
                    return owner
                held = await client.get(key)
                if held is not None:
                    return held.decode("utf-8") if isinstance(held, bytes) else str(held)
        except Exception as exc:  # noqa: BLE001 — degrade to in-process, loudly
            logger.warning(
                "registry.auth_state.run_owner_unavailable err=%s — run owners "
                "are PER PROCESS until Redis answers", exc,
            )
    return _local.claim(request_id, owner)


async def require_run_owner(
    conn: HTTPConnection, request_id: str, *, what: str = "consult run",
) -> None:
    """Claim ``request_id`` for the caller; refuse (403 ``not_row_owner``) a
    CONFINED caller (a signed-in user) when another principal owns it.

    Unconfined callers (the operator, the fleet, dev mode —
    ``session_auth.consult_owner_filter`` is ``None``) always pass and claim
    under their audit actor, so a user can never claim an operator's run.
    """
    confined = _sa.consult_owner_filter(conn)
    state = getattr(conn, "state", None)
    actor = confined or getattr(state, "principal_actor", None) or _rr.DEFAULT_SUBJECT
    owner = await claim_run_owner(str(request_id), str(actor))
    if confined is None or owner == confined:
        return
    logger.info(
        "registry.run_owner.refused what=%s request_id=%s caller=%s",
        what, request_id, confined,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "not_row_owner",
            "message": f"{what} {request_id} belongs to another user",
        },
    )


__all__ = [
    "RUN_OWNER_TTL_S",
    "claim_run_owner",
    "close_auth_state",
    "install",
    "open_auth_state",
    "redis_client",
    "require_run_owner",
]
