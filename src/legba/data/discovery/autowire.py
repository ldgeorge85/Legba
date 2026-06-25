# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Selector auto-wire (P-13 / PIVOT §4.4) — discovered source ⇒ matching targets.

When a source-discovery cycle registers a new source (or an operator registers
one by hand), targets whose ``source_selector`` SourceRefs match the new
source's scope should *automatically* wire to it — without an operator editing
every target. That is the whole point of selector-based SourceRefs: a target
declares "any open ``rss``/``news``/``geo:BR`` source", and a freshly-discovered
Brazilian news feed attaches itself.

This module is the trigger side of that contract. The *matching* logic already
exists in the W2 subscription engine
(:func:`legba.runtime.subscription.sourceref.resolve_source_refs`); this module
inverts it: instead of "given a target, which sources does it bind?", it asks
"given a NEW source, which targets' selectors now match it?" and records the
wire.

Gating (the non-negotiables, all enforced by the shared W2 matcher):

  * **subscription_policy** — only ``open`` sources auto-wire by selector;
    ``allowlist`` / ``grant`` sources require explicit opt-in and are never
    proposed. We skip the whole auto-wire when the new source isn't ``open``.
  * **tenancy** — a target auto-wires sources in its own tenant or ``shared``
    only (the W2 ``_scope_match`` tenancy gate).
  * **scope match** — tags ⊇, geo ∩, languages ∩, kinds ∋, + optional Starlark
    residual — all delegated to the W2 matcher so there is ONE definition of
    "a selector matches a source".

Recording the wire
-------------------

A "wire" is recorded as an entry in the matched target's ``sources`` list — a
selector SourceRef already covers the new source, so the binding is *implicit*:
the W2 ``resolve_source_refs`` will return the new source on the target's next
resolution. The explicit record this module writes is an
``auto_wired_sources`` provenance trailer on the target body (audit: which
sources a selector actually attracted, and when) — it does NOT mutate the
target's declared SourceRefs (those stay operator-owned). The runtime's
subscription layer resolves the live binding from the selector each cycle; this
trailer is the discovery-time observability record + the idempotency key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


async def _load_source_row(conn: asyncpg.Connection, source_id: str) -> dict | None:
    """Load the head source descriptor body for ``source_id``."""
    row = await conn.fetchrow(
        "SELECT descriptor_id, kind, state, body FROM source_descriptors "
        "WHERE descriptor_id = $1 AND is_head LIMIT 1",
        source_id,
    )
    if row is None:
        return None
    body = row["body"]
    if isinstance(body, (str, bytes, bytearray)):
        body = json.loads(body)
    return {
        "descriptor_id": row["descriptor_id"],
        "kind": row["kind"],
        "state": row["state"],
        "body": body or {},
    }


async def _load_targets_with_selectors(
    conn: asyncpg.Connection,
) -> list[dict]:
    """Load head, non-retired target descriptors that declare a selector SourceRef.

    The narrow is done in Python (the selector lives inside the JSONB
    ``sources`` array) over the head set — fine for the registry-scale target
    count. A future index on a ``has_source_selector`` generated column can
    push this into SQL.
    """
    rows = await conn.fetch(
        "SELECT descriptor_id, body FROM target_descriptors "
        "WHERE is_head AND state <> 'retired'"
    )
    out: list[dict] = []
    for r in rows:
        body = r["body"]
        if isinstance(body, (str, bytes, bytearray)):
            body = json.loads(body)
        sources = (body or {}).get("sources") or []
        if any(isinstance(s, dict) and s.get("source_selector") for s in sources):
            out.append({"descriptor_id": r["descriptor_id"], "body": body or {}})
    return out


def _target_tenant(body: dict) -> str:
    scope = (body or {}).get("scope") or {}
    return scope.get("owner_tenant", "default")


async def auto_wire_discovered_source(
    conn: asyncpg.Connection,
    *,
    source_id: str,
    nats_publish: Any = None,
) -> list[str]:
    """Wire a freshly-registered source to every target whose selector matches.

    Returns the list of target ids that now wire to ``source_id``. The match is
    computed by the W2 subscription engine
    (:func:`legba.runtime.subscription.sourceref.resolve_source_refs`) so the
    scope-match + tenancy + policy gating is identical to the live binding path.

    For each matched target we write an ``auto_wired_sources`` provenance entry
    onto the target body (idempotent — re-running the same cycle is a no-op if
    the source is already recorded). The declared SourceRefs are NOT mutated.
    """
    src = await _load_source_row(conn, source_id)
    if src is None:
        logger.warning("autowire.source_not_found source_id=%s", source_id)
        return []

    # Gate 1: only `open` sources auto-wire by selector (PIVOT §4.4.1). Skip the
    # whole scan otherwise — an allowlist/grant source never gets proposed.
    policy = (src["body"] or {}).get("subscription_policy", "open")
    if policy != "open":
        logger.info(
            "autowire.skip_non_open source_id=%s policy=%s", source_id, policy
        )
        return []
    if src["state"] == "retired":
        return []

    # Import the W2 matcher lazily (runtime package; avoids a data→runtime
    # import at module load).
    from ...runtime.subscription.sourceref import resolve_source_refs
    from ..schemas.source import SourceRef

    targets = await _load_targets_with_selectors(conn)
    wired: list[str] = []

    for tgt in targets:
        target_id = tgt["descriptor_id"]
        body = tgt["body"]
        target_tenant = _target_tenant(body)
        # Build the SourceRef list from the target body's `sources`.
        try:
            source_refs = [
                SourceRef.model_validate(s) for s in (body.get("sources") or [])
            ]
        except Exception as exc:
            logger.warning(
                "autowire.bad_source_refs target=%s err=%s", target_id, exc
            )
            continue
        # Keep only the selector refs (explicit-id refs are operator-pinned and
        # not part of auto-wire).
        selector_refs = [r for r in source_refs if r.source_selector is not None]
        if not selector_refs:
            continue

        # Ask the W2 engine which sources these selectors bind. If the new
        # source is among them, it matched.
        bindings = await resolve_source_refs(
            _SingleSourcePool(conn),
            target_id=target_id,
            target_tenant=target_tenant,
            source_refs=selector_refs,
        )
        if not any(b.source_id == source_id and b.via_selector for b in bindings):
            continue

        # Record the wire (idempotent provenance trailer).
        wrote = await _record_autowire(conn, target_id=target_id, source_id=source_id)
        if wrote:
            wired.append(target_id)
            logger.info(
                "autowire.wired target=%s source=%s", target_id, source_id
            )
            if nats_publish is not None:
                try:
                    await nats_publish(
                        f"legba.discovery.autowire.{target_id}",
                        json.dumps(
                            {"target_id": target_id, "source_id": source_id}
                        ).encode("utf-8"),
                    )
                except Exception as exc:  # pragma: no cover
                    logger.warning("autowire.nats_failed err=%s", exc)

    return wired


async def _record_autowire(
    conn: asyncpg.Connection, *, target_id: str, source_id: str
) -> bool:
    """Append ``source_id`` to the target body's ``auto_wired_sources`` trailer.

    Returns True iff the source was newly recorded (False = already present, a
    no-op for idempotent re-runs). The trailer lives in a JSONB sidecar key so
    it never collides with the strict TargetDescriptor schema (which is read
    back via ``body`` minus the trailer when the descriptor is rehydrated).
    """
    row = await conn.fetchrow(
        "SELECT body FROM target_descriptors "
        "WHERE descriptor_id = $1 AND is_head LIMIT 1",
        target_id,
    )
    if row is None:
        return False
    body = row["body"]
    if isinstance(body, (str, bytes, bytearray)):
        body = json.loads(body)
    body = dict(body or {})
    trailer = dict(body.get("_auto_wired_sources") or {})
    if source_id in trailer:
        return False
    trailer[source_id] = datetime.now(tz=timezone.utc).isoformat()
    body["_auto_wired_sources"] = trailer
    await conn.execute(
        "UPDATE target_descriptors SET body = $2::jsonb "
        "WHERE descriptor_id = $1 AND is_head",
        target_id,
        json.dumps(body, default=str),
    )
    return True


class _SingleSourcePool:
    """Adapt a single asyncpg connection to the ``pg.acquire()`` shape the W2
    ``resolve_source_refs`` expects.

    ``resolve_source_refs`` calls ``async with pg.acquire() as conn``; we wrap
    the live connection so the auto-wire runs inside the materialiser's
    transaction (it must see the just-inserted source row).
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> "_AcquireCtx":
        return _AcquireCtx(self._conn)


class _AcquireCtx:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


__all__ = [
    "auto_wire_discovered_source",
]
