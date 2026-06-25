# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""StackRegistry — L-111.

Typed-substrate-component CRUD over the `stack_components` table created
by migration 0007. Each component family (LLMProvider, VectorStore, …)
maps to a pydantic model in `legba.data.schemas.stack`, discriminated by
`schema_uri` (e.g. `legba/stack/postgres/1.0.0`).

Operations:
    register(component, actor) -> StackComponentRow
    update(component_id, new_component, actor) -> StackComponentRow
    retire(component_id, actor) -> StackComponentRow
    get(component_id, version=None) -> StackComponentRow
    get_by_kind(kind, name=None) -> list[StackComponentRow]
    list(predicate=None) -> list[StackComponentRow]
    healthcheck(component_id) -> StackComponentHealth

Each mutation:
  1. Validates the body against the kind-specific pydantic model.
  2. Verifies every `Property.Secret` reference exists in the credential
     vault (`CredentialVault.verify_exists`).
  3. Verifies the body NEVER contains a plaintext credential — only
     references. Validator rejects any field whose serialized form has a
     `factory_kind: 'secret'` but no `raw` (or whose value carries a
     plaintext-looking string of length > 50 in a known credential slot).
  4. Computes the content-hash version.
  5. Writes the row in a single transaction with the audit-log entry.
  6. Emits a NATS event.
  7. On validation failure: writes a `descriptor_dead_letter` row + emits
     `legba.dlq.stack.*` and raises `StackValidationError`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

import asyncpg
from pydantic import BaseModel, ValidationError

from ..postgres import PostgresStore
from ..schemas.lifecycle import ALLOWED_TRANSITIONS, LifecycleState
from ..schemas.properties import FactoryValue, Secret
from ..schemas.stack import (
    EmbeddingService,
    LLMProvider,
    NATSCluster,
    NLPService,
    PostgresCluster,
    ProxyPool,
    RedisCluster,
    StackComponentBase,
    VectorStore,
)
from ..schemas.versioning import canonical_json_bytes
from .audit import AuditLogger
from .credentials import CredentialResolverProtocol
from .dlq import DescriptorDeadLetter
from .emitter import NullEventEmitter, RegistryEventEmitter
from .errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    RegistryError,
    VersionConflict,
)
from .health import StackComponentHealth, StackHealthDispatcher
from .stack_events import (
    stack_dead_letter_subject,
    stack_event_payload,
    stack_subject,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind catalog
# ---------------------------------------------------------------------------

# Maps the `kind` family string (extracted from `schema_uri`) to its pydantic
# model. Discrimination per schema_uri: 'legba/stack/<family>/<semver>'.
KIND_MODELS: dict[str, type[StackComponentBase]] = {
    "llm_provider": LLMProvider,
    "vector_store": VectorStore,
    "embedding":    EmbeddingService,
    "nlp_service":  NLPService,
    "nats":         NATSCluster,
    "postgres":     PostgresCluster,
    "redis":        RedisCluster,
    "proxy_pool":   ProxyPool,
}

# Reverse: pydantic class -> kind family string.
MODEL_KINDS: dict[type[StackComponentBase], str] = {
    cls: kind for kind, cls in KIND_MODELS.items()
}

# Health-checker kind names mirror KIND_MODELS keys.
HEALTH_CHECKER_KIND: dict[str, str] = {
    "llm_provider": "llm_provider",
    "vector_store": "vector_store",
    "embedding":    "embedding",
    "nlp_service":  "nlp_service",
    "nats":         "nats",
    "postgres":     "postgres",
    "redis":        "redis",
    "proxy_pool":   "proxy_pool",
}


_SCHEMA_URI_RE = re.compile(r"^legba/stack/([a-z_]+)/(\d+\.\d+\.\d+)$")


def kind_from_schema_uri(schema_uri: str) -> str:
    """Extract the kind family from a stack-component schema_uri."""
    m = _SCHEMA_URI_RE.match(schema_uri)
    if not m:
        raise ValueError(f"invalid stack schema_uri: {schema_uri!r}")
    return m.group(1)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StackRegistryError(RegistryError):
    """Base for stack-registry-only failures."""


class StackValidationError(DescriptorValidationError):
    """Stack-side validation failure. Reuses descriptor validation envelope."""


# ---------------------------------------------------------------------------
# Row dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StackComponentRow:
    """Read-only snapshot of a `stack_components` row."""

    component_id: str
    version: str
    schema_uri: str
    kind: str
    is_head: bool
    state: LifecycleState
    owner: str
    name: str
    body: dict[str, Any]
    created_at: datetime

    @property
    def natural_key(self) -> str:
        """`kind::component_id` — the human lookup key."""
        return f"{self.kind}::{self.component_id}"

    def to_typed(self) -> StackComponentBase:
        """Re-hydrate the typed pydantic model from `body`.

        The DB stores the JSON dump (state='draft' as a string), so we
        re-validate in non-strict mode so the enum + nested factories coerce
        from their dict representations.
        """
        model = KIND_MODELS.get(self.kind)
        if model is None:
            raise ValueError(f"no model registered for kind={self.kind!r}")
        return model.model_validate(self.body, strict=False)


# ---------------------------------------------------------------------------
# Content-hashing
# ---------------------------------------------------------------------------


def _compute_version(component: StackComponentBase) -> str:
    """SHA-256 over canonical-JSON of the component, excluding the `version`
    field itself. Mirrors `legba.data.schemas.versioning.content_hash` but
    works on `StackComponentBase` which has the `version` at the top level
    (not nested under `.identity`)."""
    payload = component.model_dump(mode="json", exclude={"version"}, by_alias=True)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Plaintext-leak guard
# ---------------------------------------------------------------------------

# Field names that the validator treats as credential slots: any value here
# that doesn't carry a `factory_kind: 'secret'` shape is rejected.
_CREDENTIAL_FIELD_NAMES: tuple[str, ...] = (
    "api_key", "api_user", "api_pass", "auth", "password",
    "credentials", "secret", "token", "bearer",
)


def _walk(obj: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, path + (str(k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, path + (str(i),))
    else:
        yield path, obj


def _enforce_secret_indirection(typed: StackComponentBase) -> None:
    """Inspect the typed config object: for any field named in
    `_CREDENTIAL_FIELD_NAMES`, the value MUST be a `Secret` instance (or
    None for optional fields). A bare string, dict, or non-Secret
    `FactoryValue` subclass is rejected.

    Works on the typed model rather than the dumped dict so we don't have
    to recover `factory_kind` (which is a `ClassVar`, not a serialized
    field) from the dump.
    """
    config = getattr(typed, "config", None)
    if config is None:
        return
    for field_name in config.__class__.model_fields:
        if field_name not in _CREDENTIAL_FIELD_NAMES:
            continue
        value = getattr(config, field_name)
        if value is None:
            continue  # Optional Secret slot left blank.
        if isinstance(value, Secret):
            if not value.raw:
                raise ValueError(
                    f"credential field {field_name!r} carries an empty Secret ref"
                )
            continue
        # Anything else in a credential slot is a leak.
        raise ValueError(
            f"credential field {field_name!r} must be a Property.Secret "
            f"reference, got {type(value).__name__}={value!r}"
        )


def _collect_secret_refs(typed: StackComponentBase) -> list[str]:
    """Return all `Property.Secret` ids referenced by the typed component."""
    refs: list[str] = []
    config = getattr(typed, "config", None)
    if config is None:
        return refs
    for field_name in config.__class__.model_fields:
        value = getattr(config, field_name)
        if isinstance(value, Secret) and value.raw:
            refs.append(value.raw)
    return refs


# ---------------------------------------------------------------------------
# StackRegistry
# ---------------------------------------------------------------------------


class StackRegistry:
    """Async CRUD + healthcheck over `stack_components`.

    Construction:
        store       — primary `PostgresStore` (must be connected).
        vault       — credential resolver (`CredentialVault` or another impl
                       satisfying `CredentialResolverProtocol`).
        audit       — `AuditLogger` instance (shared with descriptor side).
        emitter     — NATS publisher (`NATSEventEmitter`) or `NullEventEmitter`
                       for tests.
        dlq         — `DescriptorDeadLetter` writer.
        health      — `StackHealthDispatcher` for `healthcheck()` calls.

    Thread-safety: methods are async-safe within a single event loop. The
    underlying asyncpg pool handles concurrency between calls.
    """

    NAMESPACE = "stack"

    def __init__(
        self,
        store: PostgresStore,
        vault: CredentialResolverProtocol,
        *,
        audit: AuditLogger | None = None,
        emitter: RegistryEventEmitter | None = None,
        dlq: DescriptorDeadLetter | None = None,
        health: StackHealthDispatcher | None = None,
    ):
        self._store = store
        self._vault = vault
        self._audit = audit or AuditLogger()
        self._emitter = emitter or NullEventEmitter()
        self._dlq = dlq or DescriptorDeadLetter(store)
        self._health = health or StackHealthDispatcher(emitter=self._emitter)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def store(self) -> PostgresStore:
        return self._store

    @property
    def vault(self) -> CredentialResolverProtocol:
        return self._vault

    @property
    def health_dispatcher(self) -> StackHealthDispatcher:
        return self._health

    @property
    def emitter(self) -> RegistryEventEmitter:
        return self._emitter

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def _validate(
        self,
        component: StackComponentBase | Mapping[str, Any],
        *,
        actor: str,
    ) -> StackComponentBase:
        """Run schema validation + credential indirection check. Either
        returns the typed model (with the content-hash version set) or
        raises `StackValidationError` after writing a DLQ row.

        Accepts either an already-typed model (re-validated for safety) or
        a raw dict.
        """
        # Step 1: discover the schema_uri (handles both typed and raw input).
        if isinstance(component, StackComponentBase):
            schema_uri = component.schema_uri
            # `dump` is only used for DLQ payload + ref collection when the
            # typed model already exists. Use python-mode so the LifecycleState
            # round-trips via the original enum.
            dump = component.model_dump(mode="json", by_alias=True)
        else:
            dump = dict(component)
            schema_uri = dump.get("schema_uri", "")
        try:
            kind = kind_from_schema_uri(schema_uri)
        except ValueError as exc:
            await self._record_dlq(
                actor=actor,
                attempted_payload=dump,
                declared_schema_uri=schema_uri or None,
                error_kind="bad_schema_uri",
                error_summary=str(exc),
            )
            raise StackValidationError(
                f"unparseable schema_uri: {exc}",
                attempted_payload=dump,
                declared_schema_uri=schema_uri or None,
                validation_error={"reason": str(exc)},
            ) from exc

        model = KIND_MODELS.get(kind)
        if model is None:
            err = f"unknown stack kind {kind!r}"
            await self._record_dlq(
                actor=actor,
                attempted_payload=dump,
                declared_schema_uri=schema_uri,
                error_kind="unknown_kind",
                error_summary=err,
            )
            raise StackValidationError(
                err, attempted_payload=dump,
                declared_schema_uri=schema_uri,
                validation_error={"reason": err},
            )

        # Step 2: validate against the kind-specific pydantic model. If the
        # caller already passed a typed instance, accept it as-is (pydantic
        # already validated at construction time, and re-validating its dump
        # against the strict schema would reject the enum/factory shapes the
        # dumper emits). Raw-dict input always goes through model_validate.
        try:
            if isinstance(component, StackComponentBase):
                if not isinstance(component, model):
                    raise StackRegistryError(
                        f"typed component {type(component).__name__} does not "
                        f"match kind {kind!r} ({model.__name__})"
                    )
                typed = component
            else:
                # Raw-dict input: validate non-strict so enums + factory
                # dicts coerce from their JSON representations.
                typed = model.model_validate(dump, strict=False)
        except ValidationError as exc:
            await self._record_dlq(
                actor=actor,
                attempted_payload=dump,
                declared_schema_uri=schema_uri,
                error_kind="pydantic_validation",
                error_summary=str(exc)[:512],
            )
            raise StackValidationError(
                "pydantic validation failed",
                attempted_payload=dump,
                declared_schema_uri=schema_uri,
                validation_error=json.loads(exc.json()),
            ) from exc

        # Step 3: enforce that no plaintext credentials slipped in.
        typed_dump = typed.model_dump(mode="json", by_alias=True)
        try:
            _enforce_secret_indirection(typed)
        except ValueError as exc:
            await self._record_dlq(
                actor=actor,
                attempted_payload=typed_dump,
                declared_schema_uri=schema_uri,
                error_kind="plaintext_credential",
                error_summary=str(exc),
            )
            raise StackValidationError(
                str(exc),
                attempted_payload=typed_dump,
                declared_schema_uri=schema_uri,
                validation_error={"reason": str(exc)},
            ) from exc

        # Step 4: verify every Property.Secret reference exists in the vault.
        secret_refs = _collect_secret_refs(typed)
        missing: list[str] = []
        for sid in secret_refs:
            try:
                if not await self._vault.verify_exists(sid):
                    missing.append(sid)
            except Exception as exc:
                logger.warning("vault verify failed for %s: %s", sid, exc)
                missing.append(sid)
        if missing:
            err = f"vault missing required secrets: {sorted(set(missing))}"
            await self._record_dlq(
                actor=actor,
                attempted_payload=typed_dump,
                declared_schema_uri=schema_uri,
                error_kind="missing_secret",
                error_summary=err,
            )
            raise StackValidationError(
                err,
                attempted_payload=typed_dump,
                declared_schema_uri=schema_uri,
                validation_error={"missing_secrets": sorted(set(missing))},
            )

        # Step 5: set the content-hash version on the typed model.
        computed = _compute_version(typed)
        # Pydantic frozen — use model_copy to swap the version field.
        if typed.version != computed:
            typed = typed.model_copy(update={"version": computed})
        return typed

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def register(
        self,
        component: StackComponentBase | Mapping[str, Any],
        actor: str,
    ) -> StackComponentRow:
        """Insert a brand-new stack component. component.id must be unique
        across the registry."""
        typed = await self._validate(component, actor=actor)
        kind = kind_from_schema_uri(typed.schema_uri)
        body = typed.model_dump(mode="json", by_alias=True)

        async with self._store.transaction() as conn:
            # Make sure the natural key isn't already taken.
            existing = await conn.fetchrow(
                "SELECT version FROM stack_components "
                "WHERE component_id = $1 AND is_head",
                typed.id,
            )
            if existing is not None:
                raise VersionConflict(
                    f"component {typed.id!r} already registered "
                    f"(head version={existing['version']}); use update() instead"
                )
            row = await conn.fetchrow(
                """
                INSERT INTO stack_components
                    (component_id, version, schema_uri, kind, is_head,
                     state, owner, name, body)
                VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7, $8::jsonb)
                RETURNING component_id, version, schema_uri, kind, is_head,
                          state, owner, name, body, created_at
                """,
                typed.id, typed.version, typed.schema_uri, kind,
                typed.state.value, typed.owner, typed.name,
                json.dumps(body),
            )
            await self._audit.record(
                conn,
                actor_id=actor,
                namespace=self.NAMESPACE,
                descriptor_id=typed.id,
                action="register",
                from_version=None,
                to_version=typed.version,
                change_summary={"kind": kind, "schema_uri": typed.schema_uri},
            )

        result = self._row_to_dataclass(row)
        await self._emit("registered", result, actor)
        return result

    async def update(
        self,
        component_id: str,
        new_component: StackComponentBase | Mapping[str, Any],
        actor: str,
    ) -> StackComponentRow:
        """Replace the head version. The old version row stays in the
        table (immutable history) but flips `is_head=false`."""
        current = await self.get(component_id)
        typed = await self._validate(new_component, actor=actor)
        if typed.id != component_id:
            raise StackRegistryError(
                f"component id mismatch: argument {component_id!r} vs body {typed.id!r}"
            )
        if typed.version == current.version:
            raise VersionConflict(
                f"new content matches head version {current.version!r}; nothing to update"
            )
        kind = kind_from_schema_uri(typed.schema_uri)
        body = typed.model_dump(mode="json", by_alias=True)

        async with self._store.transaction() as conn:
            # Flip the previous head.
            await conn.execute(
                "UPDATE stack_components SET is_head = FALSE "
                "WHERE component_id = $1 AND is_head",
                component_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO stack_components
                    (component_id, version, schema_uri, kind, is_head,
                     state, owner, name, body)
                VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7, $8::jsonb)
                RETURNING component_id, version, schema_uri, kind, is_head,
                          state, owner, name, body, created_at
                """,
                typed.id, typed.version, typed.schema_uri, kind,
                typed.state.value, typed.owner, typed.name,
                json.dumps(body),
            )
            await self._audit.record(
                conn,
                actor_id=actor,
                namespace=self.NAMESPACE,
                descriptor_id=typed.id,
                action="update",
                from_version=current.version,
                to_version=typed.version,
                change_summary={"kind": kind, "schema_uri": typed.schema_uri},
            )

        result = self._row_to_dataclass(row)
        await self._emit("updated", result, actor, from_version=current.version)
        return result

    async def retire(self, component_id: str, actor: str) -> StackComponentRow:
        """Transition the head row to RETIRED.

        Lifecycle: any non-retired state -> RETIRED.
        """
        current = await self.get(component_id)
        if current.state == LifecycleState.RETIRED:
            raise IllegalLifecycleTransition(current.state.value, LifecycleState.RETIRED.value)
        # ALLOWED_TRANSITIONS encodes "RETIRED reachable from anything except RETIRED itself".
        if LifecycleState.RETIRED not in ALLOWED_TRANSITIONS[current.state]:
            raise IllegalLifecycleTransition(
                current.state.value, LifecycleState.RETIRED.value
            )

        async with self._store.transaction() as conn:
            row = await conn.fetchrow(
                """
                UPDATE stack_components
                SET state = $2
                WHERE component_id = $1 AND is_head
                RETURNING component_id, version, schema_uri, kind, is_head,
                          state, owner, name, body, created_at
                """,
                component_id, LifecycleState.RETIRED.value,
            )
            await self._audit.record(
                conn,
                actor_id=actor,
                namespace=self.NAMESPACE,
                descriptor_id=component_id,
                action="retire",
                from_version=current.version,
                to_version=current.version,
                change_summary={
                    "from_state": current.state.value,
                    "to_state": LifecycleState.RETIRED.value,
                },
            )

        result = self._row_to_dataclass(row)
        await self._emit("retired", result, actor)
        return result

    async def get(
        self, component_id: str, version: str | None = None,
    ) -> StackComponentRow:
        """Fetch by `component_id`. `version=None` returns the head."""
        async with self._store.acquire() as conn:
            if version is None:
                row = await conn.fetchrow(
                    "SELECT component_id, version, schema_uri, kind, is_head, "
                    "state, owner, name, body, created_at "
                    "FROM stack_components "
                    "WHERE component_id = $1 AND is_head",
                    component_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT component_id, version, schema_uri, kind, is_head, "
                    "state, owner, name, body, created_at "
                    "FROM stack_components "
                    "WHERE component_id = $1 AND version = $2",
                    component_id, version,
                )
        if row is None:
            raise DescriptorNotFound(self.NAMESPACE, component_id, version)
        return self._row_to_dataclass(row)

    async def get_by_kind(
        self, kind: str, name: str | None = None,
    ) -> list[StackComponentRow]:
        """Lookup by `kind::name` natural key shape.

        `name=None` returns every head row for the kind.
        """
        async with self._store.acquire() as conn:
            if name is None:
                rows = await conn.fetch(
                    "SELECT component_id, version, schema_uri, kind, is_head, "
                    "state, owner, name, body, created_at "
                    "FROM stack_components "
                    "WHERE kind = $1 AND is_head "
                    "ORDER BY created_at",
                    kind,
                )
            else:
                rows = await conn.fetch(
                    "SELECT component_id, version, schema_uri, kind, is_head, "
                    "state, owner, name, body, created_at "
                    "FROM stack_components "
                    "WHERE kind = $1 AND name = $2 AND is_head "
                    "ORDER BY created_at",
                    kind, name,
                )
        return [self._row_to_dataclass(r) for r in rows]

    async def list(
        self,
        predicate: Callable[[StackComponentRow], bool] | None = None,
        *,
        kind: str | None = None,
        state: LifecycleState | None = None,
        include_history: bool = False,
    ) -> list[StackComponentRow]:
        """List components matching filters. Predicate runs after SQL filter
        for arbitrary checks.

        `include_history=False` (default) returns only `is_head=true` rows.
        """
        where: list[str] = []
        params: list[Any] = []
        if not include_history:
            where.append("is_head")
        if kind is not None:
            params.append(kind)
            where.append(f"kind = ${len(params)}")
        if state is not None:
            params.append(state.value)
            where.append(f"state = ${len(params)}")
        sql = (
            "SELECT component_id, version, schema_uri, kind, is_head, "
            "state, owner, name, body, created_at "
            "FROM stack_components"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at"
        async with self._store.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        result = [self._row_to_dataclass(r) for r in rows]
        if predicate is not None:
            result = [r for r in result if predicate(r)]
        return result

    # ------------------------------------------------------------------
    # Healthcheck
    # ------------------------------------------------------------------

    async def healthcheck(self, component_id: str) -> StackComponentHealth:
        """Probe the head version of `component_id` via the dispatcher."""
        row = await self.get(component_id)
        typed = row.to_typed()
        return await self._health.check_component(
            component_id=row.component_id,
            kind=row.kind,
            typed_config=typed.config,
            resolver=self._vault,
        )

    async def healthcheck_all(
        self,
        kind: str | None = None,
    ) -> dict[str, StackComponentHealth]:
        """Probe every head component (optionally filtered by kind)."""
        rows = await self.list(kind=kind)
        out: dict[str, StackComponentHealth] = {}
        for r in rows:
            try:
                out[r.component_id] = await self.healthcheck(r.component_id)
            except Exception as exc:
                logger.exception(
                    "healthcheck failed for component %s", r.component_id
                )
        return out

    async def _health_provider(
        self,
    ) -> list[tuple[str, str, Any, CredentialResolverProtocol]]:
        """Provider callback for the dispatcher's background loop."""
        rows = await self.list()
        out: list[tuple[str, str, Any, CredentialResolverProtocol]] = []
        for r in rows:
            if r.state == LifecycleState.RETIRED:
                continue
            typed = r.to_typed()
            out.append((r.component_id, r.kind, typed.config, self._vault))
        return out

    def start_health_loop(self):
        """Start the background poll loop. Returns the asyncio.Task."""
        return self._health.start_loop(self._health_provider)

    async def stop_health_loop(self) -> None:
        await self._health.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_dataclass(self, row: asyncpg.Record | Mapping) -> StackComponentRow:
        body = row["body"]
        if isinstance(body, str):
            body = json.loads(body)
        return StackComponentRow(
            component_id=row["component_id"],
            version=row["version"],
            schema_uri=row["schema_uri"],
            kind=row["kind"],
            is_head=row["is_head"],
            state=LifecycleState(row["state"]),
            owner=row["owner"],
            name=row["name"],
            body=body,
            created_at=row["created_at"],
        )

    async def _emit(
        self,
        action: str,
        row: StackComponentRow,
        actor: str,
        *,
        from_version: str | None = None,
    ) -> None:
        payload = stack_event_payload(
            action=action,  # type: ignore[arg-type]
            kind=row.kind,
            component_id=row.component_id,
            actor=actor,
            version=row.version,
            from_version=from_version,
            schema_uri=row.schema_uri,
        )
        await self._emitter.publish(
            stack_subject(action, row.kind, row.component_id),  # type: ignore[arg-type]
            payload,
        )

    async def _record_dlq(
        self,
        *,
        actor: str,
        attempted_payload: dict[str, Any],
        declared_schema_uri: str | None,
        error_kind: str,
        error_summary: str,
    ) -> None:
        try:
            entry = await self._dlq.record(
                actor=actor,
                namespace=self.NAMESPACE,
                attempted_payload=attempted_payload,
                validation_error={
                    "kind": error_kind,
                    "summary": error_summary,
                },
                declared_schema_uri=declared_schema_uri,
            )
        except Exception:
            logger.exception("failed to write DLQ row for stack registration")
            return
        # Best-effort NATS publish on DLQ.
        try:
            kind_guess = "unknown"
            if declared_schema_uri:
                try:
                    kind_guess = kind_from_schema_uri(declared_schema_uri)
                except ValueError:
                    pass
            await self._emitter.publish(
                stack_dead_letter_subject(
                    kind_guess, attempted_payload.get("id"),
                ),
                {
                    "dead_letter_id": str(entry.id),
                    "namespace": self.NAMESPACE,
                    "error_kind": error_kind,
                    "error_summary": error_summary,
                    "declared_schema_uri": declared_schema_uri,
                    "actor": actor,
                },
            )
        except Exception:
            logger.exception("failed to publish DLQ event")
