# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provisioning reconciliation for sources that subscribe upstream (P-06, §4.2.1).

Some sources must **register an outbound watch before they receive anything**:
a camera fleet (watch face X, here's our callback URL), a GitHub/Stripe webhook,
a partner event API. The source's ``on_activate`` fires the register call;
``on_retire`` deregisters. When the watch is *per-entity* the watch set is a
function of the source's **authorized subscriptions**, not static config — one
fleet source, dynamic watchlist (subscriber-driven, §4.2.1).

This is stateful reconciliation across two planes:

  * the **desired** set — the union of ``watch_param`` values from the source's
    active, authorized subscriptions (+ any static watch params on the
    descriptor), and
  * the **registered** set — what we've actually told upstream to watch,
    persisted in the source's crash-safe ``state_store``.

The spec the facial-rec example leans on:

  * **Idempotency.** Every upstream register call carries a stable
    ``idempotency_key`` (``descriptor.provision.idempotency_key_field`` +
    the watch param). Re-running ``reconcile`` is a no-op when desired ==
    registered — safe to call on every activation, after a crash, or on a
    subscription change.
  * **Partial-failure recovery.** Each add/remove is attempted independently;
    a failure on one watch param does NOT abort the others. Failures stay in
    the *pending* set persisted to state, so the next reconcile retries them.
    The registered-set in state only advances for params upstream confirmed.
  * **Rollback.** If activation must abort (policy/credential failure), the
    actor calls :func:`deprovision_all`, which removes every registered watch
    and clears state — leaving no orphan upstream subscriptions.

The upstream call itself is abstracted behind :class:`UpstreamClient` so the
mechanism is testable without a live partner API; one concrete
:class:`HttpUpstreamClient` (httpx) ships as the working example.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ._contract import SourceContext

logger = logging.getLogger(__name__)


_STATE_KEY = "provision_state"


# ---------------------------------------------------------------------------
# Upstream client seam
# ---------------------------------------------------------------------------


@runtime_checkable
class UpstreamClient(Protocol):
    """Fires the outbound register/deregister call to the upstream API.

    Both calls take the descriptor's ``register_call`` / ``deregister_call``
    template (a dict — method/url/body sketch), the resolved ``watch_param``
    being added/removed, the stable ``idempotency_key`` for the operation,
    and the resolved credential bytes. They return ``True`` on success.
    Implementations MUST be safe to retry under the same idempotency_key.
    """

    async def register(
        self,
        *,
        call: dict[str, Any],
        watch_param: str,
        idempotency_key: str,
        credential: bytes | None,
    ) -> bool: ...

    async def deregister(
        self,
        *,
        call: dict[str, Any],
        watch_param: str,
        idempotency_key: str,
        credential: bytes | None,
    ) -> bool: ...


class HttpUpstreamClient:
    """Working example: POST/DELETE the watch to an upstream HTTP API.

    Substitutes ``{watch_param}`` into the call's ``url`` / ``body`` and sends
    the idempotency key as the ``Idempotency-Key`` header (the de-facto
    standard most partner APIs honor). Construct with an ``httpx.AsyncClient``
    (the actor owns its lifecycle). Any non-2xx is a failure (left pending for
    retry); a network error likewise.
    """

    def __init__(self, http_client: Any) -> None:
        self._client = http_client

    def _render(self, call: dict[str, Any], watch_param: str) -> dict[str, Any]:
        url = str(call.get("url", "")).replace("{watch_param}", watch_param)
        body = call.get("body")
        if isinstance(body, dict):
            body = {
                k: (v.replace("{watch_param}", watch_param) if isinstance(v, str) else v)
                for k, v in body.items()
            }
        return {"url": url, "body": body, "method": call.get("method")}

    async def _send(
        self,
        *,
        default_method: str,
        call: dict[str, Any],
        watch_param: str,
        idempotency_key: str,
        credential: bytes | None,
    ) -> bool:
        rendered = self._render(call, watch_param)
        url = rendered["url"]
        if not url:
            logger.warning("provision.http.no_url watch_param=%s", watch_param)
            return False
        method = (rendered["method"] or default_method).upper()
        headers = {"Idempotency-Key": idempotency_key}
        if credential:
            headers["Authorization"] = f"Bearer {credential.decode('utf-8', 'replace')}"
        try:
            resp = await self._client.request(
                method, url, json=rendered["body"], headers=headers,
            )
        except Exception as exc:
            logger.warning(
                "provision.http.error method=%s url=%s watch_param=%s err=%s",
                method, url, watch_param, exc,
            )
            return False
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning(
                "provision.http.non2xx method=%s url=%s status=%s watch_param=%s",
                method, url, resp.status_code, watch_param,
            )
        return ok

    async def register(
        self, *, call, watch_param, idempotency_key, credential,
    ) -> bool:
        return await self._send(
            default_method="POST", call=call, watch_param=watch_param,
            idempotency_key=idempotency_key, credential=credential,
        )

    async def deregister(
        self, *, call, watch_param, idempotency_key, credential,
    ) -> bool:
        return await self._send(
            default_method="DELETE", call=call, watch_param=watch_param,
            idempotency_key=idempotency_key, credential=credential,
        )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    """Outcome of one :func:`reconcile_provision` pass."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)   # desired but upstream-failed
    pending_removals: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)  # confirmed registered after pass

    @property
    def converged(self) -> bool:
        return not self.pending and not self.pending_removals


def _idempotency_key(provision: Any, source_id: str, watch_param: str) -> str:
    field_name = getattr(provision, "idempotency_key_field", None) or "key"
    return f"{source_id}:{field_name}:{watch_param}"


async def _load_state(ctx: SourceContext) -> dict[str, list[str]]:
    raw = await ctx.state_store.get(_STATE_KEY)
    if not isinstance(raw, dict):
        return {"registered": [], "pending": [], "pending_removals": []}
    return {
        "registered": list(raw.get("registered") or []),
        "pending": list(raw.get("pending") or []),
        "pending_removals": list(raw.get("pending_removals") or []),
    }


async def _save_state(ctx: SourceContext, state: dict[str, list[str]]) -> None:
    await ctx.state_store.set(_STATE_KEY, state)


async def _resolve_credential(ctx: SourceContext, provision: Any) -> bytes | None:
    secret = getattr(provision, "credential_secret", None)
    if not secret:
        return None
    if ctx.secrets_resolve is None:
        # Bootstrap/test path — treat the SecretRef literal as the secret.
        return secret.encode("utf-8") if isinstance(secret, str) else None
    try:
        resolved = await ctx.secrets_resolve(secret)
    except Exception as exc:
        logger.warning("provision.credential.resolve_failed secret=%s err=%s", secret, exc)
        return None
    if isinstance(resolved, bytes):
        return resolved
    if isinstance(resolved, str):
        return resolved.encode("utf-8")
    return None


async def reconcile_provision(
    ctx: SourceContext,
    provision: Any,
    *,
    desired: set[str],
    client: UpstreamClient,
) -> ReconcileResult:
    """Reconcile the upstream watch set toward ``desired`` (idempotent).

    ``desired`` = the union of watch params from active authorized
    subscriptions (+ static descriptor watch params). ``provision`` is the
    descriptor's :class:`ProvisionBlock`. Safe to call repeatedly: only the
    diff against the persisted registered-set is sent upstream; confirmed
    operations advance the persisted set, failures stay pending for the next
    pass (partial-failure recovery).
    """
    result = ReconcileResult()
    if not getattr(provision, "enabled", False):
        return result

    state = await _load_state(ctx)
    registered: set[str] = set(state["registered"])
    # Retry params that previously failed to register/remove.
    pending = set(state["pending"])
    pending_removals = set(state["pending_removals"])

    credential = await _resolve_credential(ctx, provision)
    register_call = getattr(provision, "register_call", {}) or {}
    deregister_call = getattr(provision, "deregister_call", {}) or {}

    to_add = (desired - registered) | (pending & desired)
    # Anything registered (or pending-removal) that's no longer desired → remove.
    to_remove = (registered - desired) | pending_removals
    to_remove -= desired  # a param re-desired mid-cycle stays.

    for param in sorted(to_add):
        key = _idempotency_key(provision, ctx.source_id, param)
        ok = await client.register(
            call=register_call, watch_param=param,
            idempotency_key=key, credential=credential,
        )
        if ok:
            registered.add(param)
            pending.discard(param)
            result.added.append(param)
        else:
            pending.add(param)
            result.pending.append(param)

    for param in sorted(to_remove):
        key = _idempotency_key(provision, ctx.source_id, param)
        ok = await client.deregister(
            call=deregister_call, watch_param=param,
            idempotency_key=key, credential=credential,
        )
        if ok:
            registered.discard(param)
            pending_removals.discard(param)
            result.removed.append(param)
        else:
            pending_removals.add(param)
            result.pending_removals.append(param)

    state = {
        "registered": sorted(registered),
        "pending": sorted(pending),
        "pending_removals": sorted(pending_removals),
    }
    await _save_state(ctx, state)
    result.registered = sorted(registered)
    return result


async def deprovision_all(
    ctx: SourceContext,
    provision: Any,
    *,
    client: UpstreamClient,
) -> ReconcileResult:
    """Remove every registered watch (rollback / on_retire). Idempotent.

    Reconciles toward an empty desired set. Leftover params upstream couldn't
    remove stay in ``pending_removals`` for a later retry, so retiring never
    silently orphans an upstream subscription.
    """
    return await reconcile_provision(ctx, provision, desired=set(), client=client)


def desired_watch_set(
    provision: Any,
    *,
    subscriptions: list[dict[str, Any]],
    static_params: list[str] | None = None,
) -> set[str]:
    """Compute the desired watch set from active subscriptions (§4.2.1).

    Each authorized subscription contributes its ``watch_param_field`` value
    (e.g. ``face == person:X`` → ``"person:X"``). ``static_params`` are
    always-watched params declared on the descriptor. A subscription missing
    the field contributes nothing.
    """
    desired: set[str] = set(static_params or [])
    field_name = getattr(provision, "watch_param_field", None)
    if field_name:
        for sub in subscriptions:
            val = sub.get(field_name)
            if isinstance(val, str) and val:
                desired.add(val)
    return desired


__all__ = [
    "UpstreamClient",
    "HttpUpstreamClient",
    "ReconcileResult",
    "reconcile_provision",
    "deprovision_all",
    "desired_watch_set",
]
