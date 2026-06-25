# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``subscription_policy`` enforcement (P-08 / PIVOT §4.4.1).

A source declares who may subscribe via ``SourceDescriptor.subscription_policy``:

  * **open**      — any target in the same tenant (or ``shared``) may attach.
  * **allowlist** — only targets in ``allowed_targets`` / tenants in
    ``allowed_tenants`` may attach.
  * **grant**     — a subscription requires an explicit GRANT recorded as a
    ``wiring_descriptor`` (the existing wiring family), audit-logged. The grant
    is keyed ``(source_id, target_id)``.

Enforcement happens at **subscription registration** (the control plane), NOT at
delivery — the source still just publishes to its coarse subject and stays
"dumb" (PIVOT §4.4.1). This module gates who may bind a consumer; it does not
touch the publish side.

The cross-tenant rule (PIVOT §8): a target may only subscribe to a source in its
own tenant or a ``shared`` source. ``allowlist``/``grant`` can widen this for a
named target/tenant, but the default-deny boundary is here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SHARED_TENANT = "shared"

# Wiring-descriptor schema for a subscription grant. The wiring family stores
# JSONB bodies; we identify grant rows by this schema_uri + a stable id scheme.
GRANT_SCHEMA_URI = "legba/wiring/subscription_grant/1.0.0"


def grant_descriptor_id(source_id: str, target_id: str) -> str:
    """Stable wiring_descriptor id for a (source, target) subscription grant."""
    # wiring_descriptors.descriptor_id is free-form TEXT; flatten dots so it
    # reads cleanly and is collision-free per pair.
    src = source_id.replace(".", "_")
    return f"subgrant.{src}.{target_id}"


class SubscriptionPolicyError(Exception):
    """A subscription was refused by the source's ``subscription_policy``."""

    def __init__(self, *, source_id: str, target_id: str, policy: str, reason: str):
        self.source_id = source_id
        self.target_id = target_id
        self.policy = policy
        self.reason = reason
        super().__init__(
            f"subscription refused: target {target_id!r} -> source {source_id!r} "
            f"(policy={policy}): {reason}"
        )


@dataclass(frozen=True)
class SourcePolicy:
    """The policy slice of a source descriptor needed for enforcement."""

    source_id: str
    owner_tenant: str
    subscription_policy: str
    allowed_targets: list[str]
    allowed_tenants: list[str]


async def load_source_policy(pg: Any, source_id: str) -> SourcePolicy | None:
    """Load a source's policy from its head ``source_descriptors`` row."""
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body FROM source_descriptors WHERE descriptor_id = $1 AND is_head",
            source_id,
        )
    if row is None:
        return None
    body = row["body"]
    if isinstance(body, (str, bytes, bytearray)):
        body = json.loads(body)
    scope = (body or {}).get("scope") or {}
    return SourcePolicy(
        source_id=source_id,
        owner_tenant=scope.get("owner_tenant", "default"),
        subscription_policy=(body or {}).get("subscription_policy", "open"),
        allowed_targets=list((body or {}).get("allowed_targets") or []),
        allowed_tenants=list((body or {}).get("allowed_tenants") or []),
    )


async def _grant_exists(pg: Any, source_id: str, target_id: str) -> bool:
    """True iff an active wiring_descriptor grant exists for (source, target)."""
    gid = grant_descriptor_id(source_id, target_id)
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM wiring_descriptors "
            "WHERE descriptor_id = $1 AND is_head LIMIT 1",
            gid,
        )
    if row is None:
        return False
    # A grant is honored unless explicitly retired/revoked.
    return row["state"] not in ("retired", "revoked")


async def enforce_subscription(
    pg: Any,
    *,
    source: SourcePolicy,
    target_id: str,
    target_tenant: str,
) -> None:
    """Authorize one target→source subscription; raise on refusal.

    Decision table (PIVOT §4.4.1):
      * cross-tenant to a non-``shared`` source → refused unless an
        allowlist/grant explicitly widens it.
      * ``open``      → allow same-tenant / shared.
      * ``allowlist`` → allow only if target ∈ allowed_targets OR
        target_tenant ∈ allowed_tenants.
      * ``grant``     → allow only if a wiring_descriptor grant exists.
    """
    policy = source.subscription_policy
    same_tenant = (
        target_tenant == source.owner_tenant or source.owner_tenant == SHARED_TENANT
    )

    if policy == "open":
        if not same_tenant:
            raise SubscriptionPolicyError(
                source_id=source.source_id, target_id=target_id, policy=policy,
                reason=(
                    f"cross-tenant: target tenant {target_tenant!r} != source "
                    f"tenant {source.owner_tenant!r} and source is not 'shared'"
                ),
            )
        return

    if policy == "allowlist":
        if target_id in source.allowed_targets:
            return
        if target_tenant in source.allowed_tenants:
            return
        raise SubscriptionPolicyError(
            source_id=source.source_id, target_id=target_id, policy=policy,
            reason=(
                f"target {target_id!r} (tenant {target_tenant!r}) not in "
                f"allowed_targets/allowed_tenants"
            ),
        )

    if policy == "grant":
        if await _grant_exists(pg, source.source_id, target_id):
            return
        raise SubscriptionPolicyError(
            source_id=source.source_id, target_id=target_id, policy=policy,
            reason=(
                f"no active subscription grant (wiring_descriptor "
                f"{grant_descriptor_id(source.source_id, target_id)!r})"
            ),
        )

    # Unknown policy → fail closed.
    raise SubscriptionPolicyError(
        source_id=source.source_id, target_id=target_id, policy=policy,
        reason=f"unknown subscription_policy {policy!r}",
    )


async def write_grant(
    pg: Any,
    *,
    source_id: str,
    target_id: str,
    owner: str,
    reason: str = "",
) -> str:
    """Record a ``grant`` subscription grant as a wiring_descriptor head row.

    Idempotent on (source, target): re-granting an existing active grant is a
    no-op. Returns the wiring descriptor id. This is the control-plane action
    an operator (or the discovery auto-wire path) takes to authorize a
    ``grant`` source for a specific target.
    """
    gid = grant_descriptor_id(source_id, target_id)
    body = {
        "kind": "subscription_grant",
        "source_id": source_id,
        "target_id": target_id,
        "reason": reason,
    }
    async with pg.transaction() as conn:
        existing = await conn.fetchrow(
            "SELECT version, state FROM wiring_descriptors "
            "WHERE descriptor_id = $1 AND is_head LIMIT 1",
            gid,
        )
        if existing is not None and existing["state"] not in ("retired", "revoked"):
            return gid
        # version is a content-style token; keep it deterministic + simple.
        version = f"grant-{abs(hash((source_id, target_id))) & 0xFFFFFFFFFFFF:012x}"
        if existing is not None:
            await conn.execute(
                "UPDATE wiring_descriptors SET is_head = false "
                "WHERE descriptor_id = $1 AND is_head",
                gid,
            )
        await conn.execute(
            "INSERT INTO wiring_descriptors "
            "(descriptor_id, version, schema_uri, is_head, state, owner, name, body) "
            "VALUES ($1, $2, $3, true, 'active', $4, $5, $6::jsonb) "
            "ON CONFLICT (descriptor_id, version) DO UPDATE SET is_head = true, "
            "state = 'active', body = EXCLUDED.body",
            gid, version, GRANT_SCHEMA_URI, owner,
            f"grant {target_id} -> {source_id}", json.dumps(body),
        )
    return gid


async def revoke_grant(pg: Any, *, source_id: str, target_id: str) -> bool:
    """Revoke an active grant. Returns True if a grant was revoked."""
    gid = grant_descriptor_id(source_id, target_id)
    async with pg.transaction() as conn:
        row = await conn.fetchrow(
            "SELECT version FROM wiring_descriptors "
            "WHERE descriptor_id = $1 AND is_head LIMIT 1",
            gid,
        )
        if row is None:
            return False
        await conn.execute(
            "UPDATE wiring_descriptors SET state = 'revoked' "
            "WHERE descriptor_id = $1 AND version = $2",
            gid, row["version"],
        )
    return True


__all__ = [
    "GRANT_SCHEMA_URI",
    "SHARED_TENANT",
    "SourcePolicy",
    "SubscriptionPolicyError",
    "enforce_subscription",
    "grant_descriptor_id",
    "load_source_policy",
    "revoke_grant",
    "write_grant",
]
