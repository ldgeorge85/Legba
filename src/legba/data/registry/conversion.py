# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conversion-webhook framework (L-112).

Per `design/legba_topology_redesign.md` §9.8 + `design/legba_descriptor_schemas.md`
§7. Bumping a schema's MAJOR version registers a webhook that converts
instances of the old schema to the new on demand. Conversion is
**forward-only** — never destructive, never lossy. Dropped fields are
archived to `descriptor_conversion_archives` rather than silently lost.

Two cooperating components:

* `ConversionWebhookRegistry` — CRUD over the `conversion_webhooks` table
  (migration 0008 + 0013). Registers, lists, retires webhooks and walks
  the registered graph to find the shortest conversion chain between any
  two schema URIs in the same family.

* `ConversionExecutor` — executes a found chain. Each step:
    1. Resolves the webhook's `impl` string (`module.path:function_name`)
       to a Python callable.
    2. Calls it with the current body dict.
    3. Computes a field-level diff between input and output; persists
       dropped fields to `descriptor_conversion_archives`.
    4. Logs a `conversion_executions` row.

The graph search uses BFS over the (from_uri → to_uri) edges of active
(non-retired) webhooks. Shortest-path bias keeps conversion latency
bounded when multiple paths exist (e.g., v1→v2→v3 vs a hypothetical
v1→v3 shortcut webhook — the latter wins).

Failures route to the existing `descriptor_dead_letter` stream (shared
with L-110) so the operator UI surfaces conversion failures alongside
validation failures. NATS topics per L-107 §6.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable
from uuid import UUID, uuid4

import asyncpg

from ..provenance import canonical_json
from ..schemas import ConversionWebhook
from .audit import AuditLogger
from .dlq import DescriptorDeadLetter
from .errors import RegistryError
from .signing import SigningIdentity, load_default_identity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NATS subjects (L-107 §6)
# ---------------------------------------------------------------------------

CONVERSION_TOPIC_PREFIX = "conversion_webhook"
CONVERSION_DLQ_PREFIX = "legba.dlq.descriptor.conversion_failed"


def conversion_subject(
    action: str,
    family: str,
    *,
    from_uri: str | None = None,
    to_uri: str | None = None,
    descriptor_id: str | None = None,
    webhook_id: str | None = None,
) -> str:
    """Compose the NATS subject for a conversion-webhook event.

    Naming follows the patterns in the L-112 brief §6:
      * `conversion_webhook.registered.<family>.<from_uri>-<to_uri>`
      * `conversion_webhook.retired.<family>.<webhook_id>`
      * `conversion_webhook.executed.<family>.<descriptor_id>`

    The `from_uri-to_uri` pair has its slashes replaced with `_` to keep
    NATS-friendly segment boundaries (NATS subjects use `.` as the
    delimiter; bare URIs would collide).
    """
    base = f"{CONVERSION_TOPIC_PREFIX}.{action}.{family}"
    if action == "registered":
        assert from_uri is not None and to_uri is not None
        return f"{base}.{_uri_token(from_uri)}-{_uri_token(to_uri)}"
    if action == "retired":
        assert webhook_id is not None
        return f"{base}.{webhook_id}"
    if action == "executed":
        assert descriptor_id is not None
        return f"{base}.{descriptor_id}"
    raise ValueError(f"unknown conversion action {action!r}")


def conversion_dlq_subject(family: str, descriptor_id: str | None) -> str:
    """`legba.dlq.descriptor.conversion_failed.<family>.<id>` (L-112 §6)."""
    return f"{CONVERSION_DLQ_PREFIX}.{family}.{descriptor_id or '__unknown__'}"


def _uri_token(uri: str) -> str:
    """Sanitise a schema URI for use as a NATS subject segment.

    NATS segment delimiter is `.`, and `>` / `*` are wildcards. Replace `/`
    with `_` so `legba/target/2.0.0` becomes `legba_target_2.0.0` —
    losslessly invertible if a consumer needs it.
    """
    return uri.replace("/", "_")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConversionError(RegistryError):
    """Raised when a conversion attempt fails after path-finding.

    Carries the full execution context — path attempted, which step failed,
    nested error — so the DLQ writer (and the operator UI) can surface
    enough detail for a fix.
    """

    def __init__(
        self,
        message: str,
        *,
        family: str,
        from_uri: str,
        to_uri: str,
        descriptor_id: str | None = None,
        path: list[str] | None = None,
        failed_at_step: int | None = None,
        error_kind: str = "webhook_raise",
        original_body: dict[str, Any] | None = None,
        cause: Exception | None = None,
        execution_id: UUID | None = None,
    ):
        super().__init__(message)
        self.family = family
        self.from_uri = from_uri
        self.to_uri = to_uri
        self.descriptor_id = descriptor_id
        self.path = path or []
        self.failed_at_step = failed_at_step
        self.error_kind = error_kind
        self.original_body = original_body
        self.cause = cause
        self.execution_id = execution_id

    def to_context(self) -> dict[str, Any]:
        """Operator-friendly context blob for DLQ / logs."""
        return {
            "kind": "conversion",
            "family": self.family,
            "from_uri": self.from_uri,
            "to_uri": self.to_uri,
            "descriptor_id": self.descriptor_id,
            "path_attempted": self.path,
            "failed_at_step": self.failed_at_step,
            "error_kind": self.error_kind,
            "message": str(self),
            "cause": repr(self.cause) if self.cause else None,
            "execution_id": str(self.execution_id) if self.execution_id else None,
        }


class WebhookValidationError(RegistryError):
    """Raised when a `register_webhook()` call carries an invalid webhook
    shape (e.g., webhook impl unresolvable, version order inverted).

    Pydantic-level shape errors raise pydantic's own ValidationError before
    they hit this class — this covers the layer above.
    """


class WebhookNotFound(RegistryError):
    """Raised when a `retire_webhook()` references an unknown webhook id."""


# ---------------------------------------------------------------------------
# Helpers — URI parsing
# ---------------------------------------------------------------------------


def family_of_uri(uri: str) -> str:
    """Return the family portion of an Iglu-style schema URI.

    `legba/target/2.0.0` → `legba/target`
    `legba/stack/llm_provider/1.0.0` → `legba/stack/llm_provider`

    Mirrors the same-family validator in `ConversionWebhook._same_family`.
    """
    return uri.rsplit("/", 1)[0]


def version_of_uri(uri: str) -> tuple[int, int, int]:
    """Return (major, minor, patch) parsed from the trailing semver."""
    semver = uri.rsplit("/", 1)[1]
    parts = semver.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a semver-trailing URI: {uri!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


# ---------------------------------------------------------------------------
# Webhook impl resolution
# ---------------------------------------------------------------------------


WebhookCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


def resolve_impl(impl_path: str) -> WebhookCallable:
    """Resolve a `module.path:function_name` impl string to a callable.

    Format matches L-101 §7 `ConversionWebhook.impl` field. Callable is
    expected to take a dict (descriptor body) and return a dict (the
    upgraded body). Sync or async callables both supported by the executor.
    """
    if ":" not in impl_path:
        raise WebhookValidationError(
            f"impl {impl_path!r} missing ':' — expected 'module.path:function_name'"
        )
    module_name, _, func_name = impl_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise WebhookValidationError(
            f"impl module {module_name!r} not importable: {exc}"
        ) from exc
    try:
        func = getattr(module, func_name)
    except AttributeError as exc:
        raise WebhookValidationError(
            f"impl {impl_path!r}: module {module_name!r} has no attribute {func_name!r}"
        ) from exc
    if not callable(func):
        raise WebhookValidationError(f"impl {impl_path!r} resolved to non-callable {func!r}")
    return func


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookRow:
    """One row from the `conversion_webhooks` table."""

    id: UUID
    from_uri: str
    to_uri: str
    impl: str
    direction: str
    notes: str | None
    created_at: datetime
    retired_at: datetime | None
    retired_by: str | None
    retired_reason: str | None

    @property
    def is_active(self) -> bool:
        return self.retired_at is None

    @property
    def family(self) -> str:
        return family_of_uri(self.from_uri)


@dataclass(frozen=True)
class ConvertedBody:
    """Output of a successful `ConversionExecutor.convert()` call."""

    body: dict[str, Any]
    family: str
    from_uri: str
    to_uri: str
    path_webhook_ids: list[UUID]
    path_uri_chain: list[str]
    archived_legacy_fields: list[dict[str, Any]]   # one entry per step, with dropped fields
    execution_id: UUID


# ---------------------------------------------------------------------------
# ConversionWebhookRegistry
# ---------------------------------------------------------------------------


class ConversionWebhookRegistry:
    """CRUD + graph search over the `conversion_webhooks` table.

    Construct with a connected `PostgresStore` and an optional `NatsStore`
    + signing identity. The audit-log writer is shared with the other
    registries (L-110 / L-111) so signing is consistent.
    """

    def __init__(
        self,
        pg_store: Any,
        *,
        nats_store: Any = None,
        signing_identity: SigningIdentity | None = None,
        audit_logger: AuditLogger | None = None,
    ):
        self._pg = pg_store
        self._nats = nats_store
        self._identity = signing_identity or load_default_identity()
        self._audit = audit_logger or AuditLogger(identity=self._identity)

    @property
    def signing_identity(self) -> SigningIdentity:
        return self._identity

    # ------------------------------------------------------------------
    # register
    # ------------------------------------------------------------------

    async def register_webhook(
        self,
        webhook: ConversionWebhook,
        actor: str,
        *,
        actor_role: str = "operator",
        notes: str | None = None,
    ) -> WebhookRow:
        """Persist a webhook + emit registration event.

        Validates:
          * Same-family constraint (pydantic enforces; we re-check for
            defence in depth so an operator hand-crafting SQL via the
            registry layer can't bypass).
          * Forward-only version order: `from_uri` semver must be strictly
            less than `to_uri` semver.
          * `impl` resolves to an importable callable (eager-fails the
            registration; otherwise the operator only discovers a typo
            when a real descriptor needs converting).
        """
        from_fam = family_of_uri(webhook.from_uri)
        to_fam = family_of_uri(webhook.to_uri)
        if from_fam != to_fam:
            raise WebhookValidationError(
                f"cross-family webhook rejected: {from_fam} → {to_fam}"
            )

        from_v = version_of_uri(webhook.from_uri)
        to_v = version_of_uri(webhook.to_uri)
        if from_v >= to_v:
            raise WebhookValidationError(
                f"forward-only conversion: from_uri ({webhook.from_uri}) "
                f"must precede to_uri ({webhook.to_uri}); per L-101 §7"
            )

        # Eager-resolve the impl so an unimportable path fails registration
        # instead of every conversion attempt downstream.
        resolve_impl(webhook.impl)

        webhook_id = uuid4()
        async with self._pg.transaction() as conn:
            # Reject duplicates against active (non-retired) webhooks for
            # the same (from_uri, to_uri). The UNIQUE constraint from
            # migration 0008 covers ALL rows; we route to a clearer error.
            existing = await conn.fetchrow(
                """
                SELECT id, retired_at FROM conversion_webhooks
                 WHERE from_uri = $1 AND to_uri = $2
                """,
                webhook.from_uri, webhook.to_uri,
            )
            if existing is not None and existing["retired_at"] is None:
                raise WebhookValidationError(
                    f"active conversion webhook already exists for "
                    f"{webhook.from_uri} → {webhook.to_uri} "
                    f"(id={existing['id']}); retire it first"
                )
            if existing is not None:
                # A retired row exists — mint a new id but bump the
                # retired_at fields out so the UNIQUE (from,to) still
                # tolerates the new row. We delete the old row to keep the
                # constraint clean; the audit log preserves the retire+re-
                # register sequence.
                await conn.execute(
                    "DELETE FROM conversion_webhooks WHERE id = $1",
                    existing["id"],
                )

            await conn.execute(
                """
                INSERT INTO conversion_webhooks
                    (id, from_uri, to_uri, impl, direction, notes)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                webhook_id,
                webhook.from_uri,
                webhook.to_uri,
                webhook.impl,
                webhook.direction,
                notes,
            )

            await self._audit.record(
                conn,
                actor_id=actor,
                actor_role=actor_role,
                namespace=from_fam,
                descriptor_id=str(webhook_id),  # webhook itself is the audit subject
                action="register_webhook",
                from_version=webhook.from_uri,
                to_version=webhook.to_uri,
                change_summary={
                    "impl": webhook.impl,
                    "direction": webhook.direction,
                    "notes": notes,
                },
            )

        await self._publish_event(
            "registered",
            family=from_fam,
            from_uri=webhook.from_uri,
            to_uri=webhook.to_uri,
            payload={
                "webhook_id": str(webhook_id),
                "actor": actor,
                "impl": webhook.impl,
                "direction": webhook.direction,
                "from_uri": webhook.from_uri,
                "to_uri": webhook.to_uri,
            },
        )

        return await self._fetch_one(webhook_id)

    # ------------------------------------------------------------------
    # retire
    # ------------------------------------------------------------------

    async def retire_webhook(
        self,
        webhook_id: UUID | str,
        actor: str,
        *,
        actor_role: str = "operator",
        reason: str | None = None,
    ) -> WebhookRow:
        """Soft-retire a webhook. The row is preserved (immutable artifact).

        New `find_path` searches skip retired webhooks. Conversions already
        in flight that hold a resolved callable complete unaffected — the
        impl reference doesn't go away.
        """
        wid = UUID(str(webhook_id))
        async with self._pg.transaction() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversion_webhooks WHERE id = $1",
                wid,
            )
            if row is None:
                raise WebhookNotFound(f"conversion webhook {wid} not found")
            if row["retired_at"] is not None:
                # Idempotent: already retired.
                return _row_to_webhook(row)

            now = datetime.now(tz=timezone.utc)
            await conn.execute(
                """
                UPDATE conversion_webhooks
                   SET retired_at = $2, retired_by = $3, retired_reason = $4
                 WHERE id = $1
                """,
                wid, now, actor, reason,
            )
            await self._audit.record(
                conn,
                actor_id=actor,
                actor_role=actor_role,
                namespace=family_of_uri(row["from_uri"]),
                descriptor_id=str(wid),
                action="retire_webhook",
                from_version=row["from_uri"],
                to_version=row["to_uri"],
                change_summary={"reason": reason, "retired_at": now.isoformat()},
            )

        await self._publish_event(
            "retired",
            family=family_of_uri(row["from_uri"]),
            webhook_id=str(wid),
            payload={
                "webhook_id": str(wid),
                "actor": actor,
                "reason": reason,
                "from_uri": row["from_uri"],
                "to_uri": row["to_uri"],
            },
        )
        return await self._fetch_one(wid)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_webhook(self, webhook_id: UUID | str) -> WebhookRow:
        wid = UUID(str(webhook_id))
        return await self._fetch_one(wid)

    async def list_webhooks(
        self,
        family: str | None = None,
        *,
        from_uri: str | None = None,
        to_uri: str | None = None,
        include_retired: bool = False,
    ) -> list[WebhookRow]:
        """Return webhooks matching the predicate. AND semantics.

        `family` filters by either endpoint's family (most webhooks have
        identical families on both ends per the same-family constraint).
        """
        clauses: list[str] = []
        params: list[Any] = []

        def _add(clause: str, value: Any) -> None:
            params.append(value)
            clauses.append(clause.replace("?", f"${len(params)}"))

        if from_uri is not None:
            _add("from_uri = ?", from_uri)
        if to_uri is not None:
            _add("to_uri = ?", to_uri)
        if family is not None:
            params.append(family + "/%")
            clauses.append(f"(from_uri LIKE ${len(params)} OR to_uri LIKE ${len(params)})")
        if not include_retired:
            clauses.append("retired_at IS NULL")

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM conversion_webhooks{where} ORDER BY created_at DESC"
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_webhook(r) for r in rows]

    # ------------------------------------------------------------------
    # Path finding (BFS, shortest first)
    # ------------------------------------------------------------------

    async def find_path(
        self,
        family: str,
        from_uri: str,
        to_uri: str,
    ) -> list[UUID] | None:
        """Return ordered list of webhook ids that takes `from_uri` to
        `to_uri`. None if no path. Same-family constraint enforced.

        BFS over the active-webhook graph. Ties broken by lower webhook
        `created_at` (oldest registered wins) so the path is deterministic
        across calls. Shortest-path bias keeps latency bounded.
        """
        if family_of_uri(from_uri) != family or family_of_uri(to_uri) != family:
            # Out-of-family request — no path. We don't raise: callers
            # treat None as a generic "no path", and routing it via the
            # path-finder lets the DLQ layer record a sensible error.
            return None
        if from_uri == to_uri:
            return []  # already at target, empty conversion path

        # Build adjacency from active webhooks in this family. Limit to
        # the family to keep the graph small (a target/analyst migration
        # never crosses families).
        rows = await self.list_webhooks(family=family, include_retired=False)
        # Sort by created_at for tie-break determinism.
        rows.sort(key=lambda r: r.created_at)
        adj: dict[str, list[tuple[str, UUID]]] = {}
        for r in rows:
            adj.setdefault(r.from_uri, []).append((r.to_uri, r.id))

        # BFS with parent-tracking.
        visited: dict[str, tuple[str, UUID] | None] = {from_uri: None}
        queue: list[str] = [from_uri]
        idx = 0
        while idx < len(queue):
            node = queue[idx]
            idx += 1
            if node == to_uri:
                break
            for (nxt, wid) in adj.get(node, []):
                if nxt in visited:
                    continue
                visited[nxt] = (node, wid)
                queue.append(nxt)

        if to_uri not in visited:
            return None

        # Reconstruct.
        path: list[UUID] = []
        node = to_uri
        while visited[node] is not None:
            parent, wid = visited[node]  # type: ignore[misc]
            path.append(wid)
            node = parent
        path.reverse()
        return path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_one(self, webhook_id: UUID) -> WebhookRow:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversion_webhooks WHERE id = $1",
                webhook_id,
            )
        if row is None:
            raise WebhookNotFound(f"conversion webhook {webhook_id} not found")
        return _row_to_webhook(row)

    async def _publish_event(
        self,
        action: str,
        *,
        family: str,
        from_uri: str | None = None,
        to_uri: str | None = None,
        webhook_id: str | None = None,
        descriptor_id: str | None = None,
        payload: dict[str, Any],
    ) -> None:
        if self._nats is None:
            return
        subject = conversion_subject(
            action,
            family,
            from_uri=from_uri,
            to_uri=to_uri,
            webhook_id=webhook_id,
            descriptor_id=descriptor_id,
        )
        full = {
            **payload,
            "family": family,
            "action": action,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            await self._nats.nc.publish(subject, canonical_json(full))
        except Exception as exc:
            logger.warning("NATS publish failed on %s: %s", subject, exc)


# ---------------------------------------------------------------------------
# ConversionExecutor
# ---------------------------------------------------------------------------


class ConversionExecutor:
    """Walks a found path and applies each webhook in order.

    Construct with the `ConversionWebhookRegistry` (for path lookup) plus
    the PG store (for archive + execution-log writes), the optional NATS
    store and DLQ writer.

    Single entry point: `convert(body, family, from_uri, to_uri)`. Returns
    a `ConvertedBody`; raises `ConversionError` on failure (after writing
    to the DLQ + publishing the failure NATS topic).
    """

    def __init__(
        self,
        webhook_registry: ConversionWebhookRegistry,
        pg_store: Any,
        *,
        nats_store: Any = None,
        dead_letter: DescriptorDeadLetter | None = None,
    ):
        self._webhooks = webhook_registry
        self._pg = pg_store
        self._nats = nats_store
        self._dlq = dead_letter or DescriptorDeadLetter(pg_store)

    async def convert(
        self,
        body: dict[str, Any],
        *,
        family: str,
        from_uri: str,
        to_uri: str,
        descriptor_id: str | None = None,
        actor: str = "system",
    ) -> ConvertedBody:
        """Apply the shortest registered conversion chain.

        Raises `ConversionError` on:
          * no path (`error_kind='no_path'`)
          * impl resolve failure mid-path (`error_kind='impl_resolve'`)
          * webhook callable raising (`error_kind='webhook_raise'`)
          * webhook returning non-dict (`error_kind='post_validate'`)

        On any failure, the original body + execution context is written
        to `descriptor_dead_letter` (namespace=family) before re-raising.
        """
        execution_id = uuid4()

        # Same-family / no-op shortcut handled by find_path; we re-check
        # so a misuse here surfaces as a typed error rather than a silent
        # noop.
        if family_of_uri(from_uri) != family:
            raise ConversionError(
                f"from_uri {from_uri!r} not in family {family!r}",
                family=family, from_uri=from_uri, to_uri=to_uri,
                descriptor_id=descriptor_id,
                error_kind="no_path",
                original_body=body,
                execution_id=execution_id,
            )
        if family_of_uri(to_uri) != family:
            raise ConversionError(
                f"to_uri {to_uri!r} not in family {family!r}",
                family=family, from_uri=from_uri, to_uri=to_uri,
                descriptor_id=descriptor_id,
                error_kind="no_path",
                original_body=body,
                execution_id=execution_id,
            )

        if from_uri == to_uri:
            # No-op conversion. Don't log an execution row — the executor
            # is the wrong layer to record "we didn't need to do anything".
            return ConvertedBody(
                body=dict(body),
                family=family,
                from_uri=from_uri,
                to_uri=to_uri,
                path_webhook_ids=[],
                path_uri_chain=[from_uri],
                archived_legacy_fields=[],
                execution_id=execution_id,
            )

        path = await self._webhooks.find_path(family, from_uri, to_uri)
        if path is None:
            err = ConversionError(
                f"no conversion webhook from {from_uri!r} to {to_uri!r} "
                f"(family={family})",
                family=family, from_uri=from_uri, to_uri=to_uri,
                descriptor_id=descriptor_id,
                error_kind="no_path",
                original_body=body,
                execution_id=execution_id,
            )
            await self._record_failure(err, body, [], [from_uri], actor)
            raise err

        # Materialise the path with full webhook rows so we have URIs +
        # impls without an extra round-trip per step.
        webhooks_in_path: list[WebhookRow] = []
        uri_chain: list[str] = [from_uri]
        current_uri = from_uri
        for wid in path:
            w = await self._webhooks.get_webhook(wid)
            # Defensive — find_path only returns active edges, but a row
            # could be retired between lookup and execution.
            if not w.is_active:
                err = ConversionError(
                    f"webhook {wid} retired between path-find and execution",
                    family=family, from_uri=from_uri, to_uri=to_uri,
                    descriptor_id=descriptor_id,
                    path=uri_chain + [w.to_uri],
                    failed_at_step=len(webhooks_in_path),
                    error_kind="no_path",
                    original_body=body,
                    execution_id=execution_id,
                )
                await self._record_failure(err, body, [str(p) for p in path], uri_chain, actor)
                raise err
            if w.from_uri != current_uri:
                err = ConversionError(
                    f"path-finder returned discontinuous chain at step "
                    f"{len(webhooks_in_path)}: expected from_uri={current_uri!r} "
                    f"got webhook from_uri={w.from_uri!r}",
                    family=family, from_uri=from_uri, to_uri=to_uri,
                    descriptor_id=descriptor_id,
                    path=uri_chain + [w.to_uri],
                    failed_at_step=len(webhooks_in_path),
                    error_kind="post_validate",
                    original_body=body,
                    execution_id=execution_id,
                )
                await self._record_failure(err, body, [str(p) for p in path], uri_chain, actor)
                raise err
            webhooks_in_path.append(w)
            uri_chain.append(w.to_uri)
            current_uri = w.to_uri

        # Apply each step. Track archived fields per step.
        current_body = dict(body)
        archives: list[dict[str, Any]] = []
        for step_idx, w in enumerate(webhooks_in_path):
            try:
                impl = resolve_impl(w.impl)
            except WebhookValidationError as exc:
                err = ConversionError(
                    f"step {step_idx} ({w.from_uri} → {w.to_uri}): impl "
                    f"{w.impl!r} failed to resolve: {exc}",
                    family=family, from_uri=from_uri, to_uri=to_uri,
                    descriptor_id=descriptor_id,
                    path=uri_chain,
                    failed_at_step=step_idx,
                    error_kind="impl_resolve",
                    original_body=body,
                    cause=exc,
                    execution_id=execution_id,
                )
                await self._record_failure(err, body, [str(w.id) for w in webhooks_in_path], uri_chain, actor)
                raise err

            try:
                result = impl(current_body)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
            except Exception as exc:  # webhook user code
                err = ConversionError(
                    f"step {step_idx} ({w.from_uri} → {w.to_uri}): webhook "
                    f"{w.impl!r} raised: {exc!r}",
                    family=family, from_uri=from_uri, to_uri=to_uri,
                    descriptor_id=descriptor_id,
                    path=uri_chain,
                    failed_at_step=step_idx,
                    error_kind="webhook_raise",
                    original_body=body,
                    cause=exc,
                    execution_id=execution_id,
                )
                await self._record_failure(err, body, [str(w.id) for w in webhooks_in_path], uri_chain, actor)
                raise err

            if not isinstance(result, dict):
                err = ConversionError(
                    f"step {step_idx} ({w.from_uri} → {w.to_uri}): webhook "
                    f"{w.impl!r} returned {type(result).__name__}, expected dict",
                    family=family, from_uri=from_uri, to_uri=to_uri,
                    descriptor_id=descriptor_id,
                    path=uri_chain,
                    failed_at_step=step_idx,
                    error_kind="post_validate",
                    original_body=body,
                    execution_id=execution_id,
                )
                await self._record_failure(err, body, [str(w.id) for w in webhooks_in_path], uri_chain, actor)
                raise err

            # Forward-only: identify fields dropped during this step.
            # Strict definition — top-level keys present in input but
            # absent in output. Deeper structural drops are the webhook's
            # responsibility to surface via the `_archive_fields_inline`
            # convention (see contract docs); we don't second-guess the
            # nested shape because it depends on the descriptor family.
            dropped = _dropped_fields(current_body, result)
            if dropped:
                await self._write_archive(
                    namespace=family.rsplit("/", 1)[-1] if "/" in family else family,
                    descriptor_id=descriptor_id,
                    from_uri=w.from_uri,
                    to_uri=w.to_uri,
                    webhook_id=w.id,
                    legacy_fields=dropped,
                )
                archives.append({
                    "step": step_idx,
                    "webhook_id": str(w.id),
                    "dropped": dropped,
                })

            current_body = result

        # Record successful execution.
        await self._log_execution(
            namespace=_namespace_token(family),
            descriptor_id=descriptor_id,
            from_uri=from_uri,
            to_uri=to_uri,
            path_webhook_ids=[w.id for w in webhooks_in_path],
            path_uri_chain=uri_chain,
            success=True,
        )
        # Audit log entry (delegated through the registry's audit logger so
        # the signature scheme is consistent with the rest of L-110/111).
        audit_logger = self._webhooks._audit  # noqa: SLF001 — internal reuse
        async with self._pg.transaction() as conn:
            await audit_logger.record(
                conn,
                actor_id=actor,
                actor_role="system" if actor == "system" else "operator",
                namespace=_namespace_token(family),
                descriptor_id=descriptor_id or str(execution_id),
                action="convert",
                from_version=from_uri,
                to_version=to_uri,
                change_summary={
                    "execution_id": str(execution_id),
                    "path_uri_chain": uri_chain,
                    "path_webhook_ids": [str(w.id) for w in webhooks_in_path],
                    "archives": archives,
                },
            )

        await self._publish_executed(
            family=family,
            descriptor_id=descriptor_id,
            payload={
                "execution_id": str(execution_id),
                "descriptor_id": descriptor_id,
                "from_uri": from_uri,
                "to_uri": to_uri,
                "path_uri_chain": uri_chain,
                "path_webhook_ids": [str(w.id) for w in webhooks_in_path],
                "actor": actor,
            },
        )

        return ConvertedBody(
            body=current_body,
            family=family,
            from_uri=from_uri,
            to_uri=to_uri,
            path_webhook_ids=[w.id for w in webhooks_in_path],
            path_uri_chain=uri_chain,
            archived_legacy_fields=archives,
            execution_id=execution_id,
        )

    # ------------------------------------------------------------------
    # Internal — failure routing
    # ------------------------------------------------------------------

    async def _record_failure(
        self,
        err: ConversionError,
        original_body: dict[str, Any],
        path_webhook_ids: list[str],
        uri_chain: list[str],
        actor: str,
    ) -> None:
        """Write the DLQ row, the conversion_executions row, publish NATS."""
        execution_id = err.execution_id or uuid4()
        ns = _namespace_token(err.family)

        # 1. conversion_executions log
        try:
            await self._log_execution(
                namespace=ns,
                descriptor_id=err.descriptor_id,
                from_uri=err.from_uri,
                to_uri=err.to_uri,
                path_webhook_ids=[
                    UUID(w) for w in path_webhook_ids
                ],
                path_uri_chain=uri_chain,
                success=False,
                failed_at_step=err.failed_at_step,
                error_kind=err.error_kind,
                error_message=str(err),
                execution_id=execution_id,
            )
        except Exception as exc:
            logger.error("conversion_executions log write failed: %s", exc)

        # 2. descriptor_dead_letter
        try:
            await self._dlq.record(
                actor=actor,
                namespace=ns,
                attempted_payload=original_body,
                validation_error=err.to_context(),
                declared_schema_uri=err.from_uri,
            )
        except Exception as exc:
            logger.error("DLQ write failed for conversion failure: %s", exc)

        # 3. NATS event
        if self._nats is not None:
            subject = conversion_dlq_subject(err.family, err.descriptor_id)
            payload = {
                **err.to_context(),
                "actor": actor,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
            try:
                await self._nats.nc.publish(subject, canonical_json(payload))
            except Exception as exc:
                logger.warning("NATS publish failed on %s: %s", subject, exc)

    async def _publish_executed(
        self,
        *,
        family: str,
        descriptor_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        if self._nats is None:
            return
        if descriptor_id is None:
            # No descriptor id → can't route a per-descriptor event. The
            # conversion_executions row + DLQ still cover the trace.
            return
        subject = conversion_subject(
            "executed",
            family,
            descriptor_id=descriptor_id,
        )
        full = {
            **payload,
            "family": family,
            "action": "executed",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            await self._nats.nc.publish(subject, canonical_json(full))
        except Exception as exc:
            logger.warning("NATS publish failed on %s: %s", subject, exc)

    async def _log_execution(
        self,
        *,
        namespace: str,
        descriptor_id: str | None,
        from_uri: str,
        to_uri: str,
        path_webhook_ids: list[UUID],
        path_uri_chain: list[str],
        success: bool,
        failed_at_step: int | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
        execution_id: UUID | None = None,
    ) -> UUID:
        eid = execution_id or uuid4()
        async with self._pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO conversion_executions
                    (id, namespace, descriptor_id, from_uri, to_uri,
                     path_webhook_ids, path_uri_chain,
                     success, failed_at_step, error_kind, error_message)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                eid, namespace, descriptor_id, from_uri, to_uri,
                path_webhook_ids, path_uri_chain,
                success, failed_at_step, error_kind, error_message,
            )
        return eid

    async def _write_archive(
        self,
        *,
        namespace: str,
        descriptor_id: str | None,
        from_uri: str,
        to_uri: str,
        webhook_id: UUID,
        legacy_fields: dict[str, Any],
    ) -> UUID:
        archive_id = uuid4()
        async with self._pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO descriptor_conversion_archives
                    (id, namespace, descriptor_id, from_uri, to_uri,
                     webhook_id, legacy_fields)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                archive_id,
                namespace,
                descriptor_id or "__unknown__",
                from_uri,
                to_uri,
                webhook_id,
                canonical_json(legacy_fields).decode("utf-8"),
            )
        return archive_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_webhook(row: asyncpg.Record) -> WebhookRow:
    return WebhookRow(
        id=row["id"],
        from_uri=row["from_uri"],
        to_uri=row["to_uri"],
        impl=row["impl"],
        direction=row["direction"],
        notes=row["notes"],
        created_at=row["created_at"],
        retired_at=row["retired_at"],
        retired_by=row["retired_by"],
        retired_reason=row["retired_reason"],
    )


def _dropped_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return {key: old_value} for every top-level key in `old` missing
    from `new`. Deep-diff is deliberately out of scope — see the docstring
    on `ConversionExecutor.convert` for the rationale.
    """
    out: dict[str, Any] = {}
    for k, v in old.items():
        if k not in new:
            out[k] = v
    return out


def _namespace_token(family: str) -> str:
    """Compress a family token to the namespace label used by DLQ /
    descriptor_audit_log (`target` / `analyst` / `stack`).

    The family for L-101 §1 is the URI prefix (`legba/target`,
    `legba/stack/llm_provider`); the namespace column on the side tables
    uses the trailing segment. For `legba/stack/llm_provider` we collapse
    to `stack`, preserving the existing audit-table convention.
    """
    parts = family.split("/")
    if len(parts) >= 2 and parts[0] == "legba":
        return parts[1]
    return family
