# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`DescriptorRegistry` — versioned CRUD over target + analyst descriptors.

Per `design/legba_topology_redesign.md` §2.4 + L-101 §6 (lifecycle) + L-101
§7 (versioning) + L-101 §8 (vocabulary) + L-107 §6 (DLQ) + L-107 §8 (audit).

Lifecycle of a `register` call:

    1. Pydantic parse (already done by the time we hit `register` — the
       caller passes a TargetDescriptor / AnalystDescriptor instance).
    2. Vocabulary validation: TargetScope.entity_classes,
       TargetScope.relationship_types — via injected VocabularyCache.
    3. Conversion-webhook existence check for the declared schema_uri
       (the *execution* of conversion lands with L-112; we just check
       a webhook is registered for the source schema version if it doesn't
       match the latest registered for that family).
    4. content_hash() over the descriptor body (identity.version excluded).
    5. INSERT into target_descriptors / analyst_descriptors with is_head=true.
    6. Audit-log row (Ed25519-signed).
    7. NATS publish on `descriptor.registered.<family>.<id>`.

On any validation failure (steps 2 or 3) the raw payload is written to
`descriptor_dead_letter`, a NATS event fires on
`legba.dlq.descriptor.<family>.<id>`, and DescriptorValidationError is
raised so the caller knows the mutation didn't land.

`update` adds a new (descriptor_id, version) row and shifts is_head; the
prior version is preserved. `retire` moves the head row's state to
`retired`. `promote` / `rollback` shift the head pointer between existing
content-hash versions without minting a new one.

Transaction model: every mutation runs inside a single asyncpg transaction
that wraps (a) the head-row update, (b) the dead-letter / new-version
insert and (c) the audit-log row. NATS publish happens after commit — if
NATS is down the row state is still correct, just observability lag.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ValidationError

from ..outputs.ui_panel import (
    LayoutSlotConflict,
    UIPanelDescriptorError,
    UIPanelRegistry,
    register_from_descriptor as _register_ui_panels_from_descriptor,
)
from ..provenance import canonical_json
from ..schemas import (
    ALLOWED_TRANSITIONS,
    ActionPack,
    AnalystDescriptor,
    LifecycleState,
    SourceDescriptor,
    TargetDescriptor,
    content_hash,
)
from ..schemas.analyst import ANALYST_KIND_REGISTRY
from .audit import AuditLogger
from .dlq import DescriptorDeadLetter
from .errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    UnknownVocabularyValue,
    VersionConflict,
)
from .events import (
    dead_letter_event_payload,
    dead_letter_subject,
    descriptor_event_payload,
    descriptor_subject,
)
from .signing import SigningIdentity, load_default_identity
from .vocabulary_cache import VocabularyCache

if False:  # TYPE_CHECKING — keep runtime import-cycle free
    from .conversion import ConversionExecutor, ConversionWebhookRegistry  # noqa: F401

logger = logging.getLogger(__name__)


class Family(str, Enum):
    TARGET = "target"
    ANALYST = "analyst"
    SOURCE = "source"
    ACTION_PACK = "action_pack"

    @property
    def table(self) -> str:
        return {
            Family.TARGET: "target_descriptors",
            Family.ANALYST: "analyst_descriptors",
            Family.SOURCE: "source_descriptors",
            Family.ACTION_PACK: "action_pack_descriptors",
        }[self]

    @property
    def model(self) -> type:
        return {
            Family.TARGET: TargetDescriptor,
            Family.ANALYST: AnalystDescriptor,
            Family.SOURCE: SourceDescriptor,
            Family.ACTION_PACK: ActionPack,
        }[self]


DescriptorT = TargetDescriptor | AnalystDescriptor | SourceDescriptor | ActionPack


# ---------------------------------------------------------------------------
# Result row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DescriptorRow:
    descriptor_id: str
    version: str
    schema_uri: str
    is_head: bool
    state: str
    owner: str
    name: str
    body: dict[str, Any]
    created_at: datetime
    family: Family
    # Family-specific bits.
    abstraction_level: str | None = None
    inherits: list[str] = field(default_factory=list)
    retire_after: datetime | None = None
    kind: str | None = None
    type_signature: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Predicate (for list)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DescriptorPredicate:
    """Composable filter for `list`. Each field translates to a SQL clause.

    Compose by populating multiple fields; AND semantics.
    """

    family: Family | None = None
    descriptor_id: str | None = None
    state: str | None = None
    schema_uri: str | None = None
    owner: str | None = None
    kind: str | None = None                # analyst only
    abstraction_level: str | None = None   # target only
    head_only: bool = True
    limit: int | None = None


# ---------------------------------------------------------------------------
# DescriptorRegistry
# ---------------------------------------------------------------------------


class DescriptorRegistry:
    """CRUD surface for target + analyst descriptors.

    Construct with a connected `PostgresStore`, an optional connected
    `NatsStore`, an optional `VocabularyCache` (auto-built if missing) and
    an optional `SigningIdentity` (loaded from env if missing).

    Typical wiring:

        registry = DescriptorRegistry(pg_store, nats_store=nats_store)
        await registry.start()    # builds the vocab cache, subscribes
        ...
        await registry.stop()
    """

    def __init__(
        self,
        pg_store: Any,
        *,
        nats_store: Any = None,
        vocabulary_cache: VocabularyCache | None = None,
        signing_identity: SigningIdentity | None = None,
        audit_logger: AuditLogger | None = None,
        dead_letter: DescriptorDeadLetter | None = None,
        conversion_executor: "ConversionExecutor | None" = None,
        webhook_registry: "ConversionWebhookRegistry | None" = None,
        current_schema_uris: dict[str, str] | None = None,
    ):
        self._pg = pg_store
        self._nats = nats_store
        self._vocab = vocabulary_cache or VocabularyCache(pg_store)
        self._identity = signing_identity or load_default_identity()
        self._audit = audit_logger or AuditLogger(identity=self._identity)
        self._dlq = dead_letter or DescriptorDeadLetter(pg_store)
        # L-112 wiring. Either both or neither are typically passed; if
        # the caller passes only `conversion_executor`, we reach back into
        # it for the webhook registry. If neither is supplied the registry
        # still works — conversion calls become no-ops and the obsolete
        # check falls back to the L-110 logging-only behaviour.
        self._executor = conversion_executor
        if webhook_registry is None and conversion_executor is not None:
            webhook_registry = conversion_executor._webhooks  # noqa: SLF001
        self._webhooks = webhook_registry
        # Map family.value → preferred / "current" schema_uri. When set,
        # registrations with a different schema_uri in that family attempt
        # conversion before pydantic validation. If unset, the registry
        # falls back to the L-110 logging-only behaviour (no auto-convert).
        self._current_schema_uris = dict(current_schema_uris or {})
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the vocabulary cache + NATS subscription."""
        await self._vocab.refresh()
        self._sync_analyst_kind_registry()
        if self._nats is not None:
            try:
                await self._vocab.start_subscription(self._nats)
            except Exception as exc:
                logger.warning("vocabulary auto-subscription failed: %s", exc)
            # Mirror analyst-kind extensions from the vocab cache after each
            # NATS-driven refresh. L-241: the schema validator consults
            # ANALYST_KIND_REGISTRY at construction time; without this sync
            # an extension kind added via the API would land in the cache
            # but the typed schema would still reject it until process
            # restart.
            try:
                await self._vocab.start_subscription_hook(
                    self._sync_analyst_kind_registry
                )
            except AttributeError:
                # Older VocabularyCache without hook support — fall back to
                # a one-shot sync. The cache still refreshes on NATS events;
                # the kind registry catches up at the next explicit
                # `registry.start()` or `sync_analyst_kinds()` call.
                pass
        self._started = True

    async def stop(self) -> None:
        if self._nats is not None:
            await self._vocab.stop_subscription()
        self._started = False

    def _sync_analyst_kind_registry(self) -> None:
        """Mirror `analyst_kind` family from the vocab cache into the
        process-wide `ANALYST_KIND_REGISTRY`.

        Called after every cache refresh; idempotent. Built-in kinds are
        always known regardless of DB contents.
        """
        extension_values = self._vocab.values("analyst_kind")
        ANALYST_KIND_REGISTRY.replace_extensions(set(extension_values))

    async def sync_analyst_kinds(self) -> None:
        """Public hook: re-pull the analyst_kind family and mirror it into
        the schema-level registry.

        Callers that mutate `vocabulary_entries` outside the registry's own
        NATS-driven refresh path (e.g., a direct SQL insert in an admin
        script) can call this to update the schema validator without a
        process restart.
        """
        await self._vocab.refresh()
        self._sync_analyst_kind_registry()

    @property
    def vocabulary(self) -> VocabularyCache:
        return self._vocab

    @property
    def signing_identity(self) -> SigningIdentity:
        return self._identity

    @property
    def pg(self) -> Any:
        """The shared `PostgresStore` (L-244).

        Public accessor for sibling services (the `api.py` HTTP surface, the
        Phase 5 runtime, audit/DLQ inspection endpoints) that need to issue
        raw SQL against the same connection pool the registry owns. Promoted
        from the prior private `_pg` attribute per the 2026-05-20 review.

        Use `async with registry.pg.acquire() as conn: …` for one-shot reads
        and `async with registry.pg.transaction() as conn: …` when atomicity
        across multiple statements is required.
        """
        return self._pg

    # ------------------------------------------------------------------
    # Public CRUD — register
    # ------------------------------------------------------------------

    async def register(
        self,
        descriptor: DescriptorT,
        actor: str,
        *,
        actor_role: str = "operator",
    ) -> DescriptorRow:
        """Insert a brand-new descriptor head row.

        Re-registering the same descriptor_id (regardless of body) is
        rejected with `VersionConflict`; use `update` for subsequent
        versions.
        """
        family = _family_of(descriptor)
        descriptor_id = descriptor.identity.id

        # Validate (raises DescriptorValidationError on failure, after
        # routing to DLQ).
        await self._validate_or_dlq(descriptor, family, actor)

        # Compute hash, stamp it onto the model.
        hash_hex = content_hash(descriptor)
        descriptor = _stamp_version(descriptor, hash_hex)
        body = descriptor.model_dump(mode="json", by_alias=True)

        async with self._pg.transaction() as conn:
            # Refuse if a head already exists.
            existing = await conn.fetchrow(
                f"SELECT version FROM {family.table} "
                f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
                descriptor_id,
            )
            if existing is not None:
                raise VersionConflict(
                    f"{family.value} descriptor {descriptor_id!r} already "
                    f"registered (head version={existing['version']}); use update()"
                )
            await self._insert_row(conn, family, descriptor, hash_hex, body)
            await self._sync_ui_panels(
                conn,
                family=family,
                descriptor=descriptor,
                version=hash_hex,
            )
            await self._write_audit(
                conn,
                action="register",
                family=family,
                descriptor_id=descriptor_id,
                actor=actor,
                actor_role=actor_role,
                from_version=None,
                to_version=hash_hex,
                change_summary={"event": "initial_register"},
            )

        await self._publish_descriptor_event(
            action="registered",
            family=family,
            descriptor_id=descriptor_id,
            actor=actor,
            version=hash_hex,
            schema_uri=descriptor.identity.schema_uri,
        )

        return await self._fetch_row(family, descriptor_id, hash_hex)

    # ------------------------------------------------------------------
    # register_raw — accepts a dict body, runs conversion before pydantic
    # ------------------------------------------------------------------

    async def register_raw(
        self,
        body: dict[str, Any],
        actor: str,
        *,
        family: Family,
        actor_role: str = "operator",
    ) -> DescriptorRow:
        """Register a descriptor from a raw dict body (L-112 entry point).

        Unlike `register()`, this entry accepts a body that may carry an
        obsolete `schema_uri`. The flow:

          1. Read the `schema_uri` from `body["identity"]["schema_uri"]`.
          2. If it doesn't match the configured current schema_uri for
             the family, walk + apply the conversion chain.
          3. Parse the (possibly converted) body into the target pydantic
             model; delegate to `register()`.

        On conversion failure the body is routed to `descriptor_dead_letter`
        (by `ConversionExecutor`) and `ConversionError` propagates. On
        pydantic-validation failure of the converted body the usual
        `DescriptorValidationError` propagates (routed via `register()`).

        If no `current_schema_uris` mapping for the family is configured,
        no conversion is attempted; the body parses against the typed
        model as-is.
        """
        cls = family.model
        identity = body.get("identity") or {}
        if not isinstance(identity, dict):
            raise DescriptorValidationError(
                "body.identity must be an object",
                attempted_payload=body,
                declared_schema_uri=None,
                validation_error={"reason": "no_identity"},
            )
        source_uri = identity.get("schema_uri")
        if not isinstance(source_uri, str):
            raise DescriptorValidationError(
                "body.identity.schema_uri must be a string",
                attempted_payload=body,
                declared_schema_uri=None,
                validation_error={"reason": "no_schema_uri"},
            )

        upgraded_body = body
        target_uri = self._current_schema_uris.get(family.value)
        if (
            target_uri is not None
            and target_uri != source_uri
            and self._executor is not None
        ):
            fam_token = _uri_family_for(family, target_uri)
            descriptor_id = identity.get("id")
            converted = await self._executor.convert(
                body,
                family=fam_token,
                from_uri=source_uri,
                to_uri=target_uri,
                descriptor_id=descriptor_id if isinstance(descriptor_id, str) else None,
                actor=actor,
            )
            upgraded_body = converted.body

        # Parse the (possibly converted) body into the pydantic model.
        try:
            descriptor = cls.model_validate(upgraded_body, strict=False)
        except ValidationError as exc:
            payload = upgraded_body
            dl_id = await self._write_dead_letter(
                actor=actor,
                family=family,
                payload=payload,
                declared_schema_uri=upgraded_body.get("identity", {}).get(
                    "schema_uri"
                ),
                validation_error={
                    "kind": "pydantic_post_conversion",
                    "errors": exc.errors(),
                    "source_uri": source_uri,
                    "target_uri": target_uri,
                },
            )
            raise DescriptorValidationError(
                "pydantic validation failed after conversion",
                attempted_payload=payload,
                declared_schema_uri=upgraded_body.get("identity", {}).get(
                    "schema_uri"
                ),
                validation_error=exc.errors(),
                dead_letter_id=str(dl_id) if dl_id else None,
            ) from exc

        return await self.register(descriptor, actor=actor, actor_role=actor_role)

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    async def update(
        self,
        descriptor_id: str,
        new_descriptor: DescriptorT,
        actor: str,
        *,
        actor_role: str = "operator",
    ) -> DescriptorRow:
        """Replace the current head with a new content-hash version.

        The old row is preserved (rolled back to is_head=false). If the
        recomputed content-hash already exists as the current head, the
        update is a no-op (returns the existing head).
        """
        if new_descriptor.identity.id != descriptor_id:
            raise DescriptorValidationError(
                f"identity.id mismatch: update target was {descriptor_id!r} "
                f"but descriptor.identity.id = {new_descriptor.identity.id!r}",
                attempted_payload=new_descriptor.model_dump(mode="json", by_alias=True),
                declared_schema_uri=new_descriptor.identity.schema_uri,
                validation_error={"reason": "id_mismatch"},
            )
        family = _family_of(new_descriptor)

        # Validate before we touch any rows.
        await self._validate_or_dlq(new_descriptor, family, actor)

        # Look up prior head BEFORE hashing so we can carry the live state
        # into the new descriptor — state is part of content_hash input
        # (only identity.version is excluded per L-101 §7), so any state
        # stamp has to happen before we compute the hash.
        async with self._pg.acquire() as conn:
            head = await conn.fetchrow(
                f"SELECT version, body, state FROM {family.table} "
                f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
                descriptor_id,
            )
        if head is None:
            raise DescriptorNotFound(family.value, descriptor_id)
        from_version = head["version"]
        from_state = head["state"]

        # Carry the live state onto the new descriptor unless the caller
        # explicitly set a different one (and that other one is legal under
        # the state machine).
        if new_descriptor.identity.state.value != from_state:
            cur = LifecycleState(from_state)
            new_state_val = new_descriptor.identity.state
            if new_state_val not in ALLOWED_TRANSITIONS[cur] and new_state_val != cur:
                raise IllegalLifecycleTransition(cur.value, new_state_val.value)
        else:
            new_descriptor = _stamp_state(new_descriptor, from_state)

        new_hash = content_hash(new_descriptor)
        new_descriptor = _stamp_version(new_descriptor, new_hash)
        body = new_descriptor.model_dump(mode="json", by_alias=True)

        async with self._pg.transaction() as conn:
            # Re-read head inside the transaction to catch a concurrent
            # write between the acquire above and now. We check both
            # version and state — a retire would shift state without
            # changing version, but the body we hashed would be stale.
            head_in_tx = await conn.fetchrow(
                f"SELECT version, state FROM {family.table} "
                f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
                descriptor_id,
            )
            if head_in_tx is None:
                raise DescriptorNotFound(family.value, descriptor_id)
            if head_in_tx["version"] != from_version:
                # Another writer raced us; the caller can retry.
                raise VersionConflict(
                    f"{family.value} descriptor {descriptor_id!r}: "
                    f"head shifted from {from_version} to {head_in_tx['version']} "
                    f"during update; retry"
                )
            if head_in_tx["state"] != from_state:
                raise VersionConflict(
                    f"{family.value} descriptor {descriptor_id!r}: "
                    f"state shifted from {from_state} to {head_in_tx['state']} "
                    f"during update; retry"
                )

            if from_version == new_hash:
                logger.info(
                    "update no-op: %s/%s already at version %s",
                    family.value, descriptor_id, new_hash,
                )
                return await self._fetch_row(family, descriptor_id, new_hash)

            # Demote the prior head.
            await conn.execute(
                f"UPDATE {family.table} SET is_head = false "
                f"WHERE descriptor_id = $1 AND version = $2",
                descriptor_id,
                from_version,
            )
            await self._insert_row(conn, family, new_descriptor, new_hash, body)
            # L-192: refresh the UI panel registrations. With
            # retire_prior_versions=True (the default), this also retires
            # any active panel rows owned by the prior content-hash.
            await self._sync_ui_panels(
                conn,
                family=family,
                descriptor=new_descriptor,
                version=new_hash,
            )

            change_summary = _diff_summary(
                _json_loads_maybe(head["body"]), body
            )
            await self._write_audit(
                conn,
                action="update",
                family=family,
                descriptor_id=descriptor_id,
                actor=actor,
                actor_role=actor_role,
                from_version=from_version,
                to_version=new_hash,
                change_summary=change_summary,
            )

        await self._publish_descriptor_event(
            action="updated",
            family=family,
            descriptor_id=descriptor_id,
            actor=actor,
            from_version=from_version,
            to_version=new_hash,
            schema_uri=new_descriptor.identity.schema_uri,
        )
        return await self._fetch_row(family, descriptor_id, new_hash)

    # ------------------------------------------------------------------
    # retire
    # ------------------------------------------------------------------

    async def retire(
        self,
        descriptor_id: str,
        actor: str,
        *,
        family: Family,
        actor_role: str = "operator",
        reason: str | None = None,
    ) -> DescriptorRow:
        """Mark the head row as `retired`. The descriptor history stays
        intact; rolling back from `retired` is not legal (terminal state)."""
        async with self._pg.transaction() as conn:
            head = await conn.fetchrow(
                f"SELECT version, state FROM {family.table} "
                f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
                descriptor_id,
            )
            if head is None:
                raise DescriptorNotFound(family.value, descriptor_id)
            cur = LifecycleState(head["state"])
            new = LifecycleState.RETIRED
            if new not in ALLOWED_TRANSITIONS[cur]:
                raise IllegalLifecycleTransition(cur.value, new.value)
            await conn.execute(
                f"UPDATE {family.table} SET state = $1 "
                f"WHERE descriptor_id = $2 AND version = $3",
                new.value,
                descriptor_id,
                head["version"],
            )
            # L-192: soft-delete every panel row owned by this descriptor
            # (any version) so the L-204 frontend stops surfacing them.
            try:
                await UIPanelRegistry(conn).retire_for_descriptor(descriptor_id)
            except Exception as exc:
                logger.warning(
                    "ui_panel retire failed for %s/%s: %s",
                    family.value, descriptor_id, exc,
                )
            await self._write_audit(
                conn,
                action="retire",
                family=family,
                descriptor_id=descriptor_id,
                actor=actor,
                actor_role=actor_role,
                from_version=head["version"],
                to_version=None,
                change_summary={
                    "from_state": cur.value,
                    "to_state": new.value,
                    "reason": reason,
                },
            )

        await self._publish_descriptor_event(
            action="retired",
            family=family,
            descriptor_id=descriptor_id,
            actor=actor,
            version=head["version"],
            extra={"reason": reason} if reason else None,
        )
        return await self._fetch_row(family, descriptor_id, head["version"])

    # ------------------------------------------------------------------
    # promote — shift active head pointer to a candidate
    # ------------------------------------------------------------------

    async def promote(
        self,
        descriptor_id: str,
        candidate_version: str,
        actor: str,
        *,
        family: Family,
        actor_role: str = "operator",
    ) -> DescriptorRow:
        """Make `candidate_version` the new head (per L-176 optimizer flow).

        The previous head is demoted (`is_head=false`); the candidate is
        promoted (`is_head=true`). State of the previous head is *not*
        inherited — promotion implies the candidate has its own state.
        """
        return await self._set_head(
            descriptor_id,
            candidate_version,
            actor,
            family=family,
            actor_role=actor_role,
            action="promote",
        )

    # ------------------------------------------------------------------
    # rollback — shift active head back to a prior content hash
    # ------------------------------------------------------------------

    async def rollback(
        self,
        descriptor_id: str,
        target_version: str,
        actor: str,
        *,
        family: Family,
        actor_role: str = "operator",
        reason: str | None = None,
    ) -> DescriptorRow:
        """Restore a prior version as head.

        Differs from `promote` only in audit-log action name and that
        `target_version` must already exist as a *prior* (is_head=false)
        version.
        """
        return await self._set_head(
            descriptor_id,
            target_version,
            actor,
            family=family,
            actor_role=actor_role,
            action="rollback",
            extra_summary={"reason": reason} if reason else None,
        )

    async def _set_head(
        self,
        descriptor_id: str,
        target_version: str,
        actor: str,
        *,
        family: Family,
        actor_role: str,
        action: Literal["promote", "rollback"],
        extra_summary: dict[str, Any] | None = None,
    ) -> DescriptorRow:
        async with self._pg.transaction() as conn:
            cur_head = await conn.fetchrow(
                f"SELECT version FROM {family.table} "
                f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
                descriptor_id,
            )
            if cur_head is None:
                raise DescriptorNotFound(family.value, descriptor_id)
            target_row = await conn.fetchrow(
                f"SELECT version FROM {family.table} "
                f"WHERE descriptor_id = $1 AND version = $2",
                descriptor_id,
                target_version,
            )
            if target_row is None:
                raise VersionConflict(
                    f"{family.value} descriptor {descriptor_id!r}: "
                    f"target version {target_version} does not exist"
                )
            if cur_head["version"] == target_version:
                raise VersionConflict(
                    f"{family.value} descriptor {descriptor_id!r}: "
                    f"version {target_version} is already the head"
                )

            await conn.execute(
                f"UPDATE {family.table} SET is_head = false "
                f"WHERE descriptor_id = $1 AND version = $2",
                descriptor_id,
                cur_head["version"],
            )
            await conn.execute(
                f"UPDATE {family.table} SET is_head = true "
                f"WHERE descriptor_id = $1 AND version = $2",
                descriptor_id,
                target_version,
            )
            summary: dict[str, Any] = {
                "from_version": cur_head["version"],
                "to_version": target_version,
            }
            if extra_summary:
                summary.update(extra_summary)
            await self._write_audit(
                conn,
                action=action,
                family=family,
                descriptor_id=descriptor_id,
                actor=actor,
                actor_role=actor_role,
                from_version=cur_head["version"],
                to_version=target_version,
                change_summary=summary,
            )

        event_action = "promoted" if action == "promote" else "rolled_back"
        await self._publish_descriptor_event(
            action=event_action,
            family=family,
            descriptor_id=descriptor_id,
            actor=actor,
            from_version=cur_head["version"],
            to_version=target_version,
        )
        return await self._fetch_row(family, descriptor_id, target_version)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(
        self,
        descriptor_id: str,
        *,
        family: Family,
        version: str | None = None,
    ) -> DescriptorRow:
        """Return active head or a specific version. Raises if not found."""
        if version is None:
            return await self._fetch_head(family, descriptor_id)
        return await self._fetch_row(family, descriptor_id, version)

    async def get_typed(
        self,
        descriptor_id: str,
        *,
        family: Family,
        version: str | None = None,
        auto_upgrade: bool = True,
    ) -> DescriptorT:
        """Return the descriptor body re-parsed into its pydantic model.

        Useful for callers that want a typed instance back. Vocabulary
        validation is skipped at re-parse time (snapshot was already
        registry-validated at write time).

        L-112: if `auto_upgrade=True` (default) and the stored row's
        `schema_uri` is obsolete relative to `_current_schema_uris[family]`,
        the body is upgraded in-memory via `ConversionExecutor.convert()`
        before pydantic validation. The stored row is *not* mutated —
        upgrade-on-read is transparent to the caller; persisting the
        upgraded body requires an explicit `update()` (or a re-register).

        If conversion fails the call raises `ConversionError`; passing
        `auto_upgrade=False` skips the conversion attempt and returns the
        body parsed as-is (which may itself raise pydantic validation
        errors if the schema has drifted in a breaking way).
        """
        row = await self.get(descriptor_id, family=family, version=version)
        body = row.body
        # Strip JSONB sidecar trailers — underscore-prefixed top-level keys
        # (e.g. the discovery autowire's `_auto_wired_sources`) are operational
        # metadata persisted alongside the descriptor, never part of the strict
        # schema. They must be removed before rehydrating the typed model, or
        # the `extra="forbid"` models reject them. The writers read the raw
        # body, not the typed model, so this is transparent to them.
        if isinstance(body, dict) and any(k.startswith("_") for k in body):
            body = {k: v for k, v in body.items() if not k.startswith("_")}
        if auto_upgrade and self._executor is not None:
            preferred = self._current_schema_uris.get(family.value)
            if preferred and row.schema_uri != preferred:
                converted = await self._executor.convert(
                    body,
                    family=_uri_family_for(family, preferred),
                    from_uri=row.schema_uri,
                    to_uri=preferred,
                    descriptor_id=descriptor_id,
                    actor="system:get_typed",
                )
                body = converted.body
        cls = family.model
        # strict=False per L-111 pattern: stored JSONB has string enum values
        # and ISO-format datetimes that need coercion back to typed instances.
        return cls.model_validate(body, strict=False)

    async def list(self, predicate: DescriptorPredicate) -> list[DescriptorRow]:
        """List rows matching `predicate`. Predicate composes with AND."""
        if predicate.family is None:
            # Combine both families.
            target_rows = await self._list_table(Family.TARGET, predicate)
            analyst_rows = await self._list_table(Family.ANALYST, predicate)
            return [*target_rows, *analyst_rows]
        return await self._list_table(predicate.family, predicate)

    async def query_history(
        self,
        descriptor_id: str,
        *,
        family: Family,
    ) -> list[DescriptorRow]:
        """Full version history, newest first by `created_at`."""
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {family.table} "
                f"WHERE descriptor_id = $1 ORDER BY created_at DESC",
                descriptor_id,
            )
        return [_row_to_descriptor_row(family, r) for r in rows]

    # ------------------------------------------------------------------
    # Audit-log query helpers
    # ------------------------------------------------------------------

    async def audit_log_for(
        self,
        descriptor_id: str,
        *,
        family: Family | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._pg.acquire() as conn:
            if family is None:
                rows = await conn.fetch(
                    """
                    SELECT id, occurred_at, actor_id, actor_role, namespace,
                           descriptor_id, action, from_version, to_version,
                           change_summary, signed_payload, signer_did
                    FROM descriptor_audit_log
                    WHERE descriptor_id = $1
                    ORDER BY occurred_at DESC
                    LIMIT $2
                    """,
                    descriptor_id, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, occurred_at, actor_id, actor_role, namespace,
                           descriptor_id, action, from_version, to_version,
                           change_summary, signed_payload, signer_did
                    FROM descriptor_audit_log
                    WHERE descriptor_id = $1 AND namespace = $2
                    ORDER BY occurred_at DESC
                    LIMIT $3
                    """,
                    descriptor_id, family.value, limit,
                )
        return [dict(r) for r in rows]

    async def dead_letter_for(
        self,
        family: Family | None = None,
        *,
        limit: int = 100,
        include_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        async with self._pg.acquire() as conn:
            params: list[Any] = []
            sql = (
                "SELECT id, attempted_at, actor, namespace, attempted_payload, "
                "declared_schema_uri, validation_error, resolution, resolution_at "
                "FROM descriptor_dead_letter WHERE TRUE"
            )
            if family is not None:
                params.append(family.value)
                sql += f" AND namespace = ${len(params)}"
            if not include_resolved:
                sql += " AND resolution IS NULL"
            params.append(limit)
            sql += f" ORDER BY attempted_at DESC LIMIT ${len(params)}"
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    # ==================================================================
    # Internal helpers
    # ==================================================================

    async def _validate_or_dlq(
        self,
        descriptor: DescriptorT,
        family: Family,
        actor: str,
    ) -> None:
        """Run vocabulary validation against the live cache.

        On failure: write to descriptor_dead_letter (separate connection so
        the failure doesn't poison the caller's transaction), publish DLQ
        NATS event, raise DescriptorValidationError.
        """
        descriptor_id = descriptor.identity.id
        schema_uri = descriptor.identity.schema_uri
        try:
            # Vocabulary checks per family. Only target descriptors carry
            # scope-level vocab references; analyst descriptors reference
            # vocabulary through fields the runtime spec (L-103) owns.
            if isinstance(descriptor, TargetDescriptor):
                ec_validator = self._vocab.make_validator("entity_class")
                rt_validator = self._vocab.make_validator("relationship_type")
                ec_validator(descriptor.scope.entity_classes)
                rt_validator(descriptor.scope.relationship_types)
        except UnknownVocabularyValue as exc:
            payload = descriptor.model_dump(mode="json", by_alias=True)
            dl_id = await self._write_dead_letter(
                actor=actor,
                family=family,
                payload=payload,
                declared_schema_uri=schema_uri,
                validation_error={
                    "kind": "vocabulary",
                    "family": exc.family,
                    "unknown_values": exc.unknown,
                    "message": str(exc),
                },
            )
            await self._publish_dlq_event(
                family=family,
                descriptor_id=descriptor_id,
                actor=actor,
                declared_schema_uri=schema_uri,
                error_kind="vocabulary",
                error_summary=str(exc),
                dead_letter_id=str(dl_id) if dl_id else None,
            )
            raise DescriptorValidationError(
                str(exc),
                attempted_payload=payload,
                declared_schema_uri=schema_uri,
                validation_error={
                    "kind": "vocabulary",
                    "family": exc.family,
                    "unknown_values": exc.unknown,
                },
                dead_letter_id=str(dl_id) if dl_id else None,
            ) from exc
        except ValidationError as exc:
            payload = descriptor.model_dump(mode="json", by_alias=True)
            dl_id = await self._write_dead_letter(
                actor=actor,
                family=family,
                payload=payload,
                declared_schema_uri=schema_uri,
                validation_error={"kind": "pydantic", "errors": exc.errors()},
            )
            await self._publish_dlq_event(
                family=family,
                descriptor_id=descriptor_id,
                actor=actor,
                declared_schema_uri=schema_uri,
                error_kind="pydantic",
                error_summary=str(exc),
                dead_letter_id=str(dl_id) if dl_id else None,
            )
            raise DescriptorValidationError(
                "pydantic validation failed",
                attempted_payload=payload,
                declared_schema_uri=schema_uri,
                validation_error=exc.errors(),
                dead_letter_id=str(dl_id) if dl_id else None,
            ) from exc

        # Schema-version conversion webhook lookup: if a webhook is
        # registered, this is a known upgrade path — flag it but don't
        # execute (execution lands with L-112). If schema_uri is the latest
        # known, no webhook is required. We only require a webhook when
        # the schema_uri appears as a `from_uri` AND there's at least one
        # row in this family with a different schema_uri (i.e., the
        # registry has moved past it). Conservative — never block
        # registration on missing webhook for a freshly-seen schema.
        await self._check_webhook_if_obsolete(family, schema_uri)

    async def _check_webhook_if_obsolete(
        self,
        family: Family,
        schema_uri: str,
    ) -> None:
        """L-112: if the registry has a configured current schema_uri for
        this family and the incoming descriptor's schema_uri is different,
        verify an actual conversion path exists.

        If no current schema_uri is configured, fall back to the L-110
        log-only behaviour: surface coexistence with un-covered URIs as
        an INFO line but don't block registration. The `register_raw`
        entry point is the place that *runs* the conversion; this hook
        only validates that the path is *available* for descriptors that
        arrived via the typed `register()` path (which never converts —
        the caller is asserting the typed instance is already on the
        current schema).
        """
        async with self._pg.acquire() as conn:
            distinct = await conn.fetch(
                f"SELECT DISTINCT schema_uri FROM {family.table} WHERE is_head",
            )
        observed = {r["schema_uri"] for r in distinct}
        observed.discard(schema_uri)
        if not observed:
            return

        current = self._current_schema_uris.get(family.value)
        if current is not None and self._webhooks is not None and current != schema_uri:
            fam_token = _uri_family_for(family, current)
            path = await self._webhooks.find_path(fam_token, schema_uri, current)
            if path is None:
                logger.warning(
                    "no conversion webhook path from %s to %s in family=%s; "
                    "register_raw() with this body would route to DLQ",
                    schema_uri, current, family.value,
                )
            return

        async with self._pg.acquire() as conn:
            hook = await conn.fetchrow(
                "SELECT 1 FROM conversion_webhooks "
                "WHERE (from_uri = $1 OR to_uri = $1) AND retired_at IS NULL "
                "LIMIT 1",
                schema_uri,
            )
        if hook is None:
            logger.info(
                "registry-info: schema_uri=%s coexists with %s in family=%s "
                "without a registered conversion_webhook",
                schema_uri, sorted(observed), family.value,
            )

    async def _write_dead_letter(
        self,
        *,
        actor: str,
        family: Family,
        payload: dict[str, Any],
        declared_schema_uri: str | None,
        validation_error: dict[str, Any],
    ) -> UUID | None:
        try:
            entry = await self._dlq.record(
                actor=actor,
                namespace=family.value,
                attempted_payload=payload,
                validation_error=validation_error,
                declared_schema_uri=declared_schema_uri,
            )
            return entry.id
        except Exception as exc:
            logger.error("failed to write dead-letter row: %s", exc)
            return None

    async def _sync_ui_panels(
        self,
        conn: asyncpg.Connection,
        *,
        family: Family,
        descriptor: DescriptorT,
        version: str,
    ) -> None:
        """L-192 hook: materialize ``outputs.ui_panel`` entries into the
        ``ui_panel_registrations`` table.

        Called from inside ``register()`` and ``update()`` transactions so
        the panel rows commit atomically with the descriptor itself. Any
        prior-version panel rows owned by the same ``descriptor_id`` are
        retired (soft-delete) by
        :func:`legba.data.outputs.ui_panel.register_from_descriptor`.

        Descriptors that declare zero ``ui_panel`` outputs run through this
        helper as a no-op (the parser yields an empty list).

        Failure modes
        -------------
        * :class:`UIPanelDescriptorError` — descriptor has a malformed
          ``ui_panel`` entry. Re-raised so the surrounding transaction
          aborts and the descriptor row is rolled back. The validation
          surface already caught most shape errors; reaching this branch
          implies a schema-validator gap.
        * :class:`LayoutSlotConflict` — another active descriptor already
          owns ``(mode, layout_slot)``. Re-raised so the operator sees the
          conflict at register-time, not at L-204 boot.
        """
        outputs = list(getattr(descriptor, "outputs", []) or [])
        if not outputs:
            return
        # The PanelRegistration parser needs plain dict-shaped entries.
        # OutputBinding is a pydantic model — dump it back to a mapping.
        entries: list[dict[str, Any]] = []
        for entry in outputs:
            if hasattr(entry, "model_dump"):
                entries.append(entry.model_dump(mode="json"))
            elif isinstance(entry, dict):
                entries.append(entry)
        await _register_ui_panels_from_descriptor(
            conn,
            descriptor_id=descriptor.identity.id,
            descriptor_version=version,
            descriptor_family=family.value,
            outputs=entries,
        )

    async def _insert_row(
        self,
        conn: asyncpg.Connection,
        family: Family,
        descriptor: DescriptorT,
        version: str,
        body: dict[str, Any],
    ) -> None:
        if family is Family.TARGET:
            assert isinstance(descriptor, TargetDescriptor)
            await conn.execute(
                """
                INSERT INTO target_descriptors
                    (descriptor_id, version, schema_uri, is_head,
                     abstraction_level, state, owner, name, body,
                     inherits, created_at, retire_after)
                VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
                """,
                descriptor.identity.id,
                version,
                descriptor.identity.schema_uri,
                descriptor.identity.abstraction_level.value,
                descriptor.identity.state.value,
                descriptor.identity.owner,
                descriptor.identity.name,
                json.dumps(body, default=_jsonify),
                list(descriptor.identity.inherits),
                datetime.now(tz=timezone.utc),
                descriptor.identity.retire_after,
            )
        elif family is Family.ANALYST:
            assert isinstance(descriptor, AnalystDescriptor)
            await conn.execute(
                """
                INSERT INTO analyst_descriptors
                    (descriptor_id, version, schema_uri, is_head,
                     kind, state, owner, name, body,
                     type_signature, inherits, created_at)
                VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb,
                        $9::jsonb, $10, $11)
                """,
                descriptor.identity.id,
                version,
                descriptor.identity.schema_uri,
                descriptor.identity.kind,
                descriptor.identity.state.value,
                descriptor.identity.owner,
                descriptor.identity.name,
                json.dumps(body, default=_jsonify),
                json.dumps(
                    descriptor.identity.type_signature.model_dump(mode="json"),
                    default=_jsonify,
                ),
                list(descriptor.identity.inherits),
                datetime.now(tz=timezone.utc),
            )
        elif family is Family.SOURCE:
            assert isinstance(descriptor, SourceDescriptor)
            await conn.execute(
                """
                INSERT INTO source_descriptors
                    (descriptor_id, version, schema_uri, is_head,
                     abstraction_level, kind, state, owner, name, body,
                     inherits, created_at, retire_after)
                VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8, $9::jsonb,
                        $10, $11, $12)
                """,
                descriptor.identity.id,
                version,
                descriptor.identity.schema_uri,
                descriptor.identity.abstraction_level.value,
                descriptor.identity.kind,
                descriptor.identity.state.value,
                descriptor.identity.owner,
                descriptor.identity.name,
                json.dumps(body, default=_jsonify),
                list(descriptor.identity.inherits),
                datetime.now(tz=timezone.utc),
                descriptor.identity.retire_after,
            )
        else:  # Family.ACTION_PACK
            assert isinstance(descriptor, ActionPack)
            await conn.execute(
                """
                INSERT INTO action_pack_descriptors
                    (descriptor_id, version, schema_uri, is_head,
                     abstraction_level, state, owner, name, body,
                     inherits, created_at, retire_after)
                VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb,
                        $9, $10, $11)
                """,
                descriptor.identity.id,
                version,
                descriptor.identity.schema_uri,
                descriptor.identity.abstraction_level.value,
                descriptor.identity.state.value,
                descriptor.identity.owner,
                descriptor.identity.name,
                json.dumps(body, default=_jsonify),
                list(descriptor.identity.inherits),
                datetime.now(tz=timezone.utc),
                descriptor.identity.retire_after,
            )

    async def _write_audit(
        self,
        conn: asyncpg.Connection,
        *,
        action: str,
        family: Family,
        descriptor_id: str,
        actor: str,
        actor_role: str,
        from_version: str | None,
        to_version: str | None,
        change_summary: dict[str, Any] | None,
    ) -> None:
        # Delegated to the shared AuditLogger so signing identity, payload
        # shape and signature scheme are consistent across L-110 + L-111.
        await self._audit.record(
            conn,
            actor_id=actor,
            actor_role=actor_role,
            namespace=family.value,
            descriptor_id=descriptor_id,
            action=action,
            from_version=from_version,
            to_version=to_version,
            change_summary=change_summary,
        )

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_row(
        self,
        family: Family,
        descriptor_id: str,
        version: str,
    ) -> DescriptorRow:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {family.table} "
                f"WHERE descriptor_id = $1 AND version = $2",
                descriptor_id, version,
            )
        if row is None:
            raise DescriptorNotFound(family.value, descriptor_id, version)
        return _row_to_descriptor_row(family, row)

    async def _fetch_head(
        self,
        family: Family,
        descriptor_id: str,
    ) -> DescriptorRow:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {family.table} "
                f"WHERE descriptor_id = $1 AND is_head LIMIT 1",
                descriptor_id,
            )
        if row is None:
            raise DescriptorNotFound(family.value, descriptor_id)
        return _row_to_descriptor_row(family, row)

    async def _list_table(
        self,
        family: Family,
        predicate: DescriptorPredicate,
    ) -> list[DescriptorRow]:
        clauses: list[str] = []
        params: list[Any] = []

        def _add(clause: str, value: Any) -> None:
            params.append(value)
            clauses.append(clause.replace("?", f"${len(params)}"))

        if predicate.descriptor_id is not None:
            _add("descriptor_id = ?", predicate.descriptor_id)
        if predicate.state is not None:
            _add("state = ?", predicate.state)
        if predicate.schema_uri is not None:
            _add("schema_uri = ?", predicate.schema_uri)
        if predicate.owner is not None:
            _add("owner = ?", predicate.owner)
        if family is Family.ANALYST and predicate.kind is not None:
            _add("kind = ?", predicate.kind)
        if family is Family.TARGET and predicate.abstraction_level is not None:
            _add("abstraction_level = ?", predicate.abstraction_level)
        if predicate.head_only:
            clauses.append("is_head")

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM {family.table}{where} ORDER BY created_at DESC"
        if predicate.limit is not None:
            params.append(int(predicate.limit))
            sql += f" LIMIT ${len(params)}"

        async with self._pg.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_descriptor_row(family, r) for r in rows]

    # ------------------------------------------------------------------
    # NATS publish
    # ------------------------------------------------------------------

    async def _publish_descriptor_event(
        self,
        *,
        action: str,
        family: Family,
        descriptor_id: str,
        actor: str,
        version: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        schema_uri: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._nats is None:
            return
        subject = descriptor_subject(action, family.value, descriptor_id)  # type: ignore[arg-type]
        payload = descriptor_event_payload(
            action=action,
            family=family.value,
            descriptor_id=descriptor_id,
            actor=actor,
            version=version,
            from_version=from_version,
            to_version=to_version,
            schema_uri=schema_uri,
            extra=extra,
        )
        try:
            # Core NATS publish — descriptor events are observability
            # signals, not durable work items. JetStream subscribers can
            # still pin a stream that covers `descriptor.>` if they want
            # durability, but the registry doesn't require it.
            await self._nats.nc.publish(subject, canonical_json(payload))
        except Exception as exc:
            logger.warning(
                "NATS publish failed on %s: %s (descriptor state OK)", subject, exc
            )

    async def _publish_dlq_event(
        self,
        *,
        family: Family,
        descriptor_id: str | None,
        actor: str,
        declared_schema_uri: str | None,
        error_kind: str,
        error_summary: str,
        dead_letter_id: str | None,
    ) -> None:
        if self._nats is None:
            return
        subject = dead_letter_subject(family.value, descriptor_id)
        payload = dead_letter_event_payload(
            family=family.value,
            descriptor_id=descriptor_id,
            actor=actor,
            declared_schema_uri=declared_schema_uri,
            error_kind=error_kind,
            error_summary=error_summary,
            dead_letter_id=dead_letter_id,
        )
        try:
            await self._nats.nc.publish(subject, canonical_json(payload))
        except Exception as exc:
            logger.warning("NATS DLQ publish failed on %s: %s", subject, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _family_of(descriptor: DescriptorT) -> Family:
    if isinstance(descriptor, TargetDescriptor):
        return Family.TARGET
    if isinstance(descriptor, AnalystDescriptor):
        return Family.ANALYST
    if isinstance(descriptor, SourceDescriptor):
        return Family.SOURCE
    if isinstance(descriptor, ActionPack):
        return Family.ACTION_PACK
    raise TypeError(f"unsupported descriptor type: {type(descriptor).__name__}")


def _stamp_version(descriptor: DescriptorT, version: str) -> DescriptorT:
    """Return a copy of `descriptor` with `identity.version = version`."""
    new_identity = descriptor.identity.model_copy(update={"version": version})
    return descriptor.model_copy(update={"identity": new_identity})


def _stamp_state(descriptor: DescriptorT, state: str) -> DescriptorT:
    new_identity = descriptor.identity.model_copy(
        update={"state": LifecycleState(state)}
    )
    return descriptor.model_copy(update={"identity": new_identity})


def _jsonify(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialize {value!r}")


def _json_loads_maybe(value: Any) -> dict[str, Any]:
    """asyncpg returns JSONB as a string in some setups, as a dict in others."""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value  # type: ignore[return-value]
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return {}


def _diff_summary(
    old: dict[str, Any], new: dict[str, Any]
) -> dict[str, Any]:
    """Lightweight field-level diff for the audit-log `change_summary`.

    Not a full JSON-patch yet (L-113 / UI side-by-side renderer will likely
    expect that shape, this stays compact for storage). Reports:
      * top-level keys added / removed
      * top-level keys changed (with old / new canonical-JSON-equal check)
    """
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed: list[str] = []
    for k in sorted(set(old) & set(new)):
        if canonical_json(old.get(k)) != canonical_json(new.get(k)):
            changed.append(k)
    return {"added": added, "removed": removed, "changed": changed}


def _uri_family_for(family: Family, schema_uri: str) -> str:
    """Compute the URI-family token (e.g., `legba/target`) for a schema URI
    in a given descriptor family.

    The schema URI carries the family in its prefix (`legba/target/2.0.0` →
    `legba/target`). For target/analyst descriptors the URI family is
    always `legba/<family.value>`; we still derive from the URI in case a
    future stack-style sub-family lands.
    """
    if "/" in schema_uri:
        return schema_uri.rsplit("/", 1)[0]
    return f"legba/{family.value}"


def _row_to_descriptor_row(family: Family, row: asyncpg.Record) -> DescriptorRow:
    body = _json_loads_maybe(row["body"])
    base = {
        "descriptor_id": row["descriptor_id"],
        "version": row["version"],
        "schema_uri": row["schema_uri"],
        "is_head": row["is_head"],
        "state": row["state"],
        "owner": row["owner"],
        "name": row["name"],
        "body": body,
        "created_at": row["created_at"],
        "family": family,
        "inherits": list(row["inherits"] or []),
    }
    if family is Family.TARGET:
        return DescriptorRow(
            **base,
            abstraction_level=row["abstraction_level"],
            retire_after=row["retire_after"],
        )
    if family is Family.SOURCE:
        return DescriptorRow(
            **base,
            abstraction_level=row["abstraction_level"],
            kind=row["kind"],
            retire_after=row["retire_after"],
        )
    if family is Family.ACTION_PACK:
        return DescriptorRow(
            **base,
            abstraction_level=row["abstraction_level"],
            retire_after=row["retire_after"],
        )
    # ANALYST
    return DescriptorRow(
        **base,
        kind=row["kind"],
        type_signature=_json_loads_maybe(row["type_signature"]),
    )
