# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.registry — typed registries (L-110 descriptors, L-111 stack).

Versioned, content-addressed CRUD over the descriptor + stack registries per
`design/legba_topology_redesign.md` §2.4–§2.5. Layers validation,
content-hashing, NATS event emission, audit-log writes and dead-letter
routing on top of the L-001 tables.

Module map:
  * `errors`            — typed exceptions raised by either registry.
  * `events`            — NATS topic naming + payload shape (descriptor side).
  * `signing`           — Ed25519 signing helper for audit-log rows.
  * `audit`             — Ed25519-signed audit-log writer (shared by both
                           registries; appends to `descriptor_audit_log`).
  * `dlq`               — Dead-letter writer for both registries.
  * `credentials`       — Encrypted credential vault (XSalsa20-Poly1305 via
                           PyNaCl SecretBox) keyed by `Property.Secret` ids.
  * `stack_events`      — NATS subject naming for the stack-component side.
  * `stack`             — L-111 `StackRegistry` — typed-substrate-component CRUD.
  * `health`            — Stack-component healthcheck dispatch + background loop.
  * `vocabulary_cache`  — runtime-extensible vocabulary registry loaded from
                           `vocabulary_entries` (L-110, not yet landed).
  * `descriptor`        — `DescriptorRegistry` — descriptor-side CRUD (L-110).

L-112 (conversion webhook execution), L-113 (REST + WebSocket surface) and
L-114 (provenance write helpers) layer further work on top of this module.
"""

from __future__ import annotations

# Shared error types (L-110 + L-111).
from .errors import (
    AuditChainError,
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    UnknownVocabularyValue,
    VersionConflict,
)
from .events import (
    DEAD_LETTER_TOPIC_PREFIX,
    DESCRIPTOR_TOPIC_PREFIX,
    VOCABULARY_UPDATED_TOPIC,
    audit_payload,
    dead_letter_subject,
    descriptor_subject,
)

# Shared writers (audit + DLQ) live with the L-111 work but are imported by
# both registries.
from .audit import AuditLogger, AuditEntry
from .dlq import DescriptorDeadLetter, DLQEntry

# L-110 descriptor-side surface.
try:
    from .descriptor import (
        DescriptorPredicate,
        DescriptorRegistry,
        DescriptorRow,
        Family,
    )
    from .discovered_materializer import (
        MaterializeOutcome,
        ReconcileResult,
        materialize_discovered,
        merge_descriptor_bodies,
        reconcile_discovered_targets,
    )
    from .vocabulary_cache import VocabularyCache

    _L110_EXPORTS = [
        "DescriptorPredicate",
        "DescriptorRegistry",
        "DescriptorRow",
        "Family",
        "MaterializeOutcome",
        "ReconcileResult",
        "VocabularyCache",
        "materialize_discovered",
        "merge_descriptor_bodies",
        "reconcile_discovered_targets",
    ]
except ImportError:  # pragma: no cover
    _L110_EXPORTS = []

# L-112 conversion-webhook framework. Independent of L-111 stack pieces;
# uses the same shared writers (audit + DLQ) as L-110.
try:
    from .conversion import (
        CONVERSION_DLQ_PREFIX,
        CONVERSION_TOPIC_PREFIX,
        ConvertedBody,
        ConversionError,
        ConversionExecutor,
        ConversionWebhookRegistry,
        WebhookNotFound,
        WebhookRow,
        WebhookValidationError,
        conversion_dlq_subject,
        conversion_subject,
        family_of_uri,
        resolve_impl,
        version_of_uri,
    )

    _L112_EXPORTS = [
        "CONVERSION_DLQ_PREFIX",
        "CONVERSION_TOPIC_PREFIX",
        "ConvertedBody",
        "ConversionError",
        "ConversionExecutor",
        "ConversionWebhookRegistry",
        "WebhookNotFound",
        "WebhookRow",
        "WebhookValidationError",
        "conversion_dlq_subject",
        "conversion_subject",
        "family_of_uri",
        "resolve_impl",
        "version_of_uri",
    ]
except ImportError:  # pragma: no cover
    _L112_EXPORTS = []

# L-111 stack-side surface. Modules land in parallel — importing must not
# blow up if any sibling isn't present yet.
try:
    from .credentials import (
        CredentialResolverProtocol,
        CredentialVault,
        MissingSecretError,
        VaultLockedError,
    )
    from .emitter import NATSEventEmitter, NullEventEmitter, RegistryEventEmitter
    from .health import (
        HEALTH_CHECKERS,
        HealthState,
        StackComponentHealth,
        StackHealthChecker,
        StackHealthDispatcher,
        register_health_checker,
    )
    from .stack import (
        StackComponentRow,
        StackRegistry,
        StackRegistryError,
        StackValidationError,
    )
    from .stack_events import (
        STACK_DLQ_PREFIX,
        STACK_TOPIC_PREFIX,
        stack_dead_letter_subject,
        stack_event_payload,
        stack_health_subject,
        stack_subject,
    )

    _L111_EXPORTS = [
        "CredentialResolverProtocol",
        "CredentialVault",
        "MissingSecretError",
        "VaultLockedError",
        "NATSEventEmitter",
        "NullEventEmitter",
        "RegistryEventEmitter",
        "HEALTH_CHECKERS",
        "HealthState",
        "StackComponentHealth",
        "StackHealthChecker",
        "StackHealthDispatcher",
        "register_health_checker",
        "StackComponentRow",
        "StackRegistry",
        "StackRegistryError",
        "StackValidationError",
        "STACK_DLQ_PREFIX",
        "STACK_TOPIC_PREFIX",
        "stack_dead_letter_subject",
        "stack_event_payload",
        "stack_health_subject",
        "stack_subject",
    ]
except ImportError:  # pragma: no cover — L-111 still in flight
    _L111_EXPORTS = []


__all__ = [
    # Errors
    "AuditChainError",
    "DescriptorNotFound",
    "DescriptorValidationError",
    "IllegalLifecycleTransition",
    "UnknownVocabularyValue",
    "VersionConflict",
    # Descriptor event helpers (L-110)
    "DEAD_LETTER_TOPIC_PREFIX",
    "DESCRIPTOR_TOPIC_PREFIX",
    "VOCABULARY_UPDATED_TOPIC",
    "audit_payload",
    "dead_letter_subject",
    "descriptor_subject",
    # Shared writers
    "AuditEntry",
    "AuditLogger",
    "DLQEntry",
    "DescriptorDeadLetter",
] + _L110_EXPORTS + _L111_EXPORTS + _L112_EXPORTS
