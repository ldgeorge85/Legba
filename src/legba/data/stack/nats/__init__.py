# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.nats — Phase-2 NATS cluster stack handler (L-124).

Per L-102 §1 (kind handler contracts), each stack-component family is backed
by a handler class that conforms to the `KindHandler` protocol. This
sub-package owns the NATS cluster family.

Single concrete kind for now:

  * `nats.jetstream` — `NATSClusterHandler` (`kind = "nats"`).

Wraps the L-001 `legba.data.nats.NatsStore` connection wrapper. Adds:

  * Operations surface (`ensure_stream`, `ensure_consumer`, `publish`,
    `subscribe`, `pull_subscribe`, `kv_get`, `kv_put`, `kv_delete`) typed
    against the L-101 §5 `NATSClusterConfig` schema, so the runtime never
    needs to import nats-py types directly.
  * Per-target / per-analyst stream naming convention
    (`legba.target.<target_id>.signals`, `legba.analyst.<analyst_id>.findings`)
    exposed via `target_stream_name()` / `analyst_stream_name()` helpers.
  * Lifecycle hooks (`on_configure`, `on_activate`, `on_pause`, `on_retire`)
    per L-102 §1. `on_pause` gracefully drains in-flight subscriptions before
    returning; `on_configure` verifies the JetStream account is reachable.
  * Healthcheck consistent with L-102 `HandlerHealth` (state + last_success
    + detail + extras) layered on top of `streams_info()` + `account_info()`.
  * Subscription bookkeeping: every push or pull subscription created via
    this handler is tracked so `on_pause` can drain them cleanly.

This handler does not own actor scheduling, budget reporting, or trace
emission — those land in Phase 5 (L-160 / L-163 / L-107). The handler
exposes the operations surface those Phase-5 components compose over.
"""

from __future__ import annotations

from .jetstream import (
    ConfigureContext,
    HandlerHealth,
    NATSClusterHandler,
    NATSClusterHandlerConfig,
    RuntimeContext,
    SubscriptionHandle,
    analyst_stream_name,
    analyst_subject_prefix,
    target_stream_name,
    target_subject_prefix,
)

__all__ = [
    "ConfigureContext",
    "HandlerHealth",
    "NATSClusterHandler",
    "NATSClusterHandlerConfig",
    "RuntimeContext",
    "SubscriptionHandle",
    "analyst_stream_name",
    "analyst_subject_prefix",
    "target_stream_name",
    "target_subject_prefix",
]
