# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.runtime.subscription — fan-out + subscription engine (P-08).

The source-first pivot's delivery seam (PIVOT_PROPOSAL §4.4 / §4.4.1 / §6.1):

  * **SourceRef resolution** — explicit ``source_id`` AND ``source_selector``
    matching source SCOPE over ``source_descriptors`` (:mod:`.sourceref`).
  * **Subscription matching** — structured filter pushed to a SQL ``WHERE`` over
    the indexed ``signals`` table (GIN/btree from migration 0024) + a COARSE
    NATS subject filter (tenant/source/modality only) + a Starlark residual via
    the EXISTING engine in :mod:`legba.data.predicates` (:mod:`.filter`,
    :mod:`.subjects`).
  * **Per-target aggregated JetStream consumers** — ONE consumer per target,
    subject-filtered (``NatsStore.ensure_durable_consumer``).
  * **subscription_policy enforcement** — open / allowlist / grant (via a
    ``wiring_descriptor``) at subscription-registration time (:mod:`.policy`).
  * **Consumer-lag observability** — ``num_pending`` per target + stream growth.

Subjects are COARSE — arbitrary predicates are NEVER expressed as subjects
(PIVOT §6.1). Exact matching is SQL + Starlark.
"""

from __future__ import annotations

from .backfill import (
    BackfillCursor,
    BackfillResult,
    Backfiller,
    SignalSink,
    capture_cursor,
)
from .engine import SubscriptionEngine, TargetSubscription, target_consumer_name
from .filter import (
    SqlFilter,
    build_sql_filter,
    matches,
    residual_matches,
    residual_matches_async,
)
from .policy import (
    GRANT_SCHEMA_URI,
    SourcePolicy,
    SubscriptionPolicyError,
    enforce_subscription,
    grant_descriptor_id,
    load_source_policy,
    revoke_grant,
    write_grant,
)
from .sourceref import SHARED_TENANT, SourceRow, resolve_source_refs
from .subjects import ResolvedBinding, subject_filters_for

__all__ = [
    # engine
    "SubscriptionEngine",
    "TargetSubscription",
    "target_consumer_name",
    # backfill (P-12 catch-up + seamless forward handoff)
    "Backfiller",
    "BackfillCursor",
    "BackfillResult",
    "SignalSink",
    "capture_cursor",
    # sourceref
    "resolve_source_refs",
    "ResolvedBinding",
    "SourceRow",
    "SHARED_TENANT",
    # filter
    "build_sql_filter",
    "SqlFilter",
    "matches",
    "residual_matches",
    "residual_matches_async",
    # subjects
    "subject_filters_for",
    # policy
    "enforce_subscription",
    "SubscriptionPolicyError",
    "SourcePolicy",
    "load_source_policy",
    "write_grant",
    "revoke_grant",
    "grant_descriptor_id",
    "GRANT_SCHEMA_URI",
]
