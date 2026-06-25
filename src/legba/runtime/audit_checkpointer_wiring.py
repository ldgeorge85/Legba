# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bootstrap wiring for the L-107 audit checkpointer.

Call from ``dapr_host.bring_up_production_runtime`` after substrate
connections + before reconcile loop start. Store the returned instance
in ``_RuntimeHandles`` so ``handles.stop()`` calls ``checkpointer.stop()``.

What this module does
---------------------
The :class:`legba.data.provenance.checkpointer.AuditCheckpointer` class is
the per-minute asyncio task that signs the current receipt-chain head per
analyst and writes a row to ``audit_checkpoints`` (Mnemosyne D5
alignment per legba_observability.md §7). Today nothing instantiates it
in production — the class exists but no bootstrap path starts it.

:func:`start_audit_checkpointer` constructs the checkpointer with sensible
defaults (60s interval, reusing the descriptor-audit-log Ed25519 identity
via :func:`legba.data.registry.signing.load_default_identity`) and starts
the background asyncio task. The task is spawned by
``AuditCheckpointer.start()`` which returns immediately, so this helper
also returns immediately; callers don't need to await anything beyond the
construction.

Signing identity choice
-----------------------
We deliberately reuse the registry's deployment-level Ed25519 identity
(``LEGBA_REGISTRY_SIGNING_KEY`` env or its keyfile sibling). Rationale:

  * The descriptor audit log already signs with this identity. Verifying a
    checkpoint against the same DID as the audit-log rows lets a single
    verifier rotate one trusted-key list — see
    ``data.registry.signing.verify_audit_payload`` and the regsitry's
    ``trusted_keys`` propagation in ``dapr_host.attach_a2a_skill_router``.
  * The runtime control plane runs in the same trust boundary as the
    registry process (both are deployment-controlled, not per-analyst).
  * The checkpointer's :class:`Ed25519Signer` is a Protocol-ish wrapper
    that needs ``did`` + ``sign(bytes) -> bytes``. The registry's
    :class:`SigningIdentity` exposes ``signing_key`` (a nacl SigningKey)
    + ``signer_did``. We bridge with a thin adapter that pulls the 32-byte
    seed out of the SigningKey via ``signing_key.encode()`` and constructs
    a checkpointer-shaped ``Ed25519Signer``.

If a deployment wants a separate "runtime checkpointer" identity (different
DID, different key material), pass an explicit ``Ed25519Signer`` to
``start_audit_checkpointer``; the default-identity bridge is only the
no-arg fallback.

Integration note for dapr_host.bring_up_production_runtime
-----------------------------------------------------------
Wire as::

    from .audit_checkpointer_wiring import start_audit_checkpointer
    ...
    audit_checkpointer = await start_audit_checkpointer(pg_store.pool)
    handles.audit_checkpointer = audit_checkpointer  # type: ignore[attr-defined]

and in ``_RuntimeHandles.stop()`` (reverse-shutdown order, before
``pg_store.close()`` so the pool is still usable for the final tick's
INSERT)::

    try:
        await self.audit_checkpointer.stop()
    except Exception as exc:
        logger.warning("audit_checkpointer.stop err=%s", exc)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncpg

from ..data.provenance.checkpointer import (
    AuditCheckpointer,
    CheckpointerConfig,
    Ed25519Signer,
)
from ..data.registry.signing import SigningIdentity, load_default_identity

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


__all__ = [
    "start_audit_checkpointer",
    "signer_from_registry_identity",
]


def signer_from_registry_identity(identity: SigningIdentity) -> Ed25519Signer:
    """Adapter — wrap a registry :class:`SigningIdentity` as a checkpointer
    :class:`Ed25519Signer`.

    The two types both wrap a nacl ``SigningKey`` but expose different
    Python surfaces:

      * :class:`SigningIdentity` — ``signing_key`` (nacl SigningKey) +
        ``signer_did`` (str). Used for descriptor-audit-log signing.
      * :class:`Ed25519Signer`  — ``__init__(seed: bytes, did: str)``,
        ``sign(bytes) -> bytes``. Used by the checkpointer loop.

    The nacl ``SigningKey.encode()`` method returns the 32-byte seed (the
    same bytes the SigningKey was originally constructed from), so we can
    losslessly rebuild a checkpointer-shaped signer from a registry
    identity. The resulting signer signs the same way the registry does
    (same key, same DID, same canonical-JSON form).
    """
    seed = bytes(identity.signing_key.encode())
    return Ed25519Signer(seed, did=identity.signer_did)


async def start_audit_checkpointer(
    pg_pool: asyncpg.Pool,
    *,
    signer: Ed25519Signer | None = None,
    interval_seconds: float = 60.0,
    max_traces_between: int = 100,
) -> AuditCheckpointer:
    """Construct and start the audit checkpointer.

    Parameters
    ----------
    pg_pool:
        Live asyncpg pool — the checkpointer reads ``analyst_traces`` per
        tick and INSERTs into ``audit_checkpoints``. Must be against the
        same database the runtime writes traces to.
    signer:
        Optional explicit :class:`Ed25519Signer`. When ``None`` we fall
        back to ``load_default_identity()`` (the descriptor-audit-log
        identity) bridged via :func:`signer_from_registry_identity`.
    interval_seconds:
        Floor on the per-tick wake cadence. Default 60s per L-107 §7 /
        legba_observability.md OBS-6 ("per-minute or per-100-runs,
        whichever fires first"). Tests pass smaller values.
    max_traces_between:
        Ceiling — sign + write when this many traces have accumulated
        since the last checkpoint regardless of the interval. Default 100
        per OBS-6.

    Returns
    -------
    The running :class:`AuditCheckpointer`. The asyncio task has already
    been created; ``await checkpointer.start()`` returned. The caller is
    responsible for ``await checkpointer.stop()`` during shutdown — store
    the returned instance on the runtime handles dataclass.
    """
    if signer is None:
        identity = load_default_identity()
        signer = signer_from_registry_identity(identity)
        logger.info(
            "audit_checkpointer.signer.default did=%s",
            signer.did,
        )
    else:
        logger.info(
            "audit_checkpointer.signer.explicit did=%s",
            signer.did,
        )

    config = CheckpointerConfig(
        interval_seconds=interval_seconds,
        max_traces_between=max_traces_between,
        # Leave NATS publishing off by default — production wires this
        # via a follow-up that threads the nats_store publish closure in.
        nats_subject_pattern=None,
    )
    checkpointer = AuditCheckpointer(pg_pool, signer, config)
    await checkpointer.start()
    logger.info(
        "audit_checkpointer.started interval=%.1fs max_traces_between=%d signer_did=%s",
        interval_seconds, max_traces_between, signer.did,
    )
    return checkpointer
