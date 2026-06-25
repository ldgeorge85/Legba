# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Standard dependency bundle threaded through actor runs.

Mirrors L-103's RuntimeContext sketch. The runtime constructs one of these
per actor activation and passes it into source/filter/analyst handlers.

This is the concrete pydantic shape referenced by AnalystDescriptor's
`type_signature.deps_type` default (`legba.runtime.deps.StandardDeps`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

import asyncpg


@runtime_checkable
class BudgetReporter(Protocol):
    """Per L-102 §7 budget reporter slice."""

    async def record(
        self,
        *,
        kind: str,
        amount: int,
        dimension: str | None = None,
    ) -> None: ...

    async def check_envelope(self) -> str: ...


class _NoopBudget:
    """Dev-mode budget reporter — accepts everything, records nothing."""

    async def record(self, *, kind: str, amount: int, dimension: str | None = None) -> None:
        return None

    async def check_envelope(self) -> str:
        return "ok"


@dataclass
class StandardDeps:
    """The dependency bundle passed into every actor run.

    Attributes
    ----------
    pg_pool:
        Asyncpg pool against the legba primary Postgres cluster.
    nats_publish:
        Async callable ``(subject, payload_bytes) -> None`` for emitting
        substrate-write events on NATS. ``None`` means no event emission.
    secrets_resolve:
        Async callable ``(secret_id) -> bytes`` to resolve a vault-stored
        credential to its plaintext bytes. The runtime injects a closure
        bound to the CredentialVault.
    logger:
        Bound structured logger.
    budget:
        Optional budget reporter (per-analyst envelope check + token record).
    extras:
        Free-form bag for kind-specific injection (e.g., a precomputed
        LLM handler for the inline_target analyst).
    """

    pg_pool: asyncpg.Pool
    nats_publish: Callable[..., Any] | None = None
    secrets_resolve: Callable[..., Any] | None = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("legba.runtime")
    )
    budget: BudgetReporter = field(default_factory=_NoopBudget)
    extras: Mapping[str, Any] = field(default_factory=dict)
