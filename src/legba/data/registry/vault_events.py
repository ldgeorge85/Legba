# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS subject naming + payload helpers for credential-vault rotation.

Subjects follow the same convention as :mod:`legba.data.registry.stack_events`
(L-111), one token family per event domain:

    vault.secret.<action>.<secret_id>

Today there is exactly one action (``rotated`` — covers both the first
``store_secret`` for a `secret_id` and every subsequent rotation; a handler
cache has nothing cached for a brand-new secret, so the eviction sweep is a
harmless no-op on first-store). The ``Literal`` is kept open-ended the same
way ``StackAction`` is, so a future action (e.g. ``deleted``) is a type-level
addition, not a subject-grammar change.

Why this is a SEPARATE event family from ``stack.component.>`` rather than a
new ``StackAction``: a vault secret is not a stack component — it has no
``kind`` token (``stack_subject`` bakes ``kind`` into the subject), and a
rotation cannot be mapped to the ``component_id``(s) that reference it without
a registry-side reverse lookup that does not exist (a component's body would
need to be walked for ``Property.Secret`` references matching the rotated
``secret_id`` — plausible future work, not today's fix). So the consumer side
(:class:`legba.runtime.nats_informer.NatsVaultRotationInformer`) intentionally
does NOT target a specific cache entry — it calls
:func:`legba.runtime.llm_handler_cache.evict_all_llm_handlers` on ANY message,
the same "ops escape hatch" sweep the module already ships. Coarser than the
per-component stack eviction, but rotations are rare, operator-initiated
events (not a hot path), so a process-wide handler-cache drop is a fully
acceptable cost for guaranteed correctness — see the module docstring on
:mod:`legba.runtime.llm_handler_cache` for why eviction never leaves a stale
secret served past the next acquire.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

VAULT_TOPIC_PREFIX = "vault.secret"

VaultAction = Literal["rotated"]


def vault_subject(action: VaultAction, secret_id: str) -> str:
    """Per-action NATS subject for a vault secret.

    ``secret_id`` is itself dotted (``llm.primary.openai_compat.api_key``);
    unlike ``stack_subject`` there is no trailing ``kind`` token to rejoin
    around, so no special parsing is needed on the consumer side — it never
    inspects the id, only the fact that a message arrived.
    """
    return f"{VAULT_TOPIC_PREFIX}.{action}.{secret_id}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def vault_event_payload(
    *,
    action: VaultAction,
    secret_id: str,
    actor: str,
    version: int,
) -> dict[str, Any]:
    """Payload published on ``vault.secret.<action>.*`` NATS subjects.

    Deliberately carries NO plaintext and no ciphertext — only the metadata
    already safe to log (mirrors ``CredentialVault.store_secret``'s own
    INFO-level log line).
    """
    return {
        "secret_id": secret_id,
        "action": action,
        "actor": actor,
        "version": version,
        "timestamp": _now_iso(),
    }


__all__ = [
    "VAULT_TOPIC_PREFIX",
    "VaultAction",
    "vault_event_payload",
    "vault_subject",
]
