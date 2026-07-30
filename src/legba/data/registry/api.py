# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI HTTP + WebSocket surface for the descriptor + stack + conversion
registries (L-113).

Mounts under `/api/v1/registry/`. The router is intentionally thin: every
endpoint delegates to the underlying registry class (`DescriptorRegistry`,
`StackRegistry`, `CredentialVault`, `DescriptorDeadLetter`, `AuditLogger`,
`VocabularyCache`, plus the conversion-webhook registry).

Design notes:

  * The router is constructed via `build_router(deps)` (where `deps` is a
    `RegistryAPIDeps` bundle) so it can be mounted onto any FastAPI app or
    composed with sibling routers — the `server.py` launcher wires the
    canonical deployment.

  * Auth: a single bearer-token gate via `LEGBA_REGISTRY_API_TOKEN`,
    FAIL-CLOSED (B-2). Token set → enforced with a constant-time compare.
    Token unset/empty → every guarded request (HTTP and WS) gets 503
    "service misconfigured", UNLESS `LEGBA_DEV_MODE=1` is explicitly set
    in the environment (development mode: any token, or no token,
    accepted). Real DID-bearer + OAuth2 = Phase 8/10 follow-up per the
    L-113 brief.

  * WebSocket multiplexing: each connection opens its own NATS subscription
    against the supplied filter (default = all registry subjects). A 30s
    server-side heartbeat keeps idle browsers from being closed by a
    middlebox; client close cleanly tears down the NATS subscription.

  * Credential vault: API never returns plaintext. Only `register`,
    `delete`, `exists` are exposed. Resolution stays server-side via
    `CredentialResolver` (Phase-2 use sites).

  * OpenAPI lives at `/api/v1/registry/openapi.json` + `/docs` —
    auto-generated from the pydantic request/response models below.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Header,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel, ConfigDict, Field

from ..nats import NatsStore
from ..schemas import (
    ActionPack,
    AnalystDescriptor,
    LifecycleState,
    SourceDescriptor,
    TargetDescriptor,
)
from .audit import AuditLogger
from .credentials import CredentialResolverProtocol, CredentialVault
from .descriptor import (
    DescriptorPredicate,
    DescriptorRegistry,
    DescriptorRow,
    Family,
)
from .dlq import DescriptorDeadLetter
from .errors import (
    AuditChainError,
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    RegistryError,
    UnknownVocabularyValue,
    VersionConflict,
)
from .signing import SigningIdentity, verify_audit_payload
from .stack import StackRegistry, StackValidationError, kind_from_schema_uri
from .vocabulary_cache import VocabularyCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversion-webhook surface. The typed `ConversionWebhookRegistry`
# (`legba.data.registry.conversion`) is the canonical backend and is proxied to
# when present. The import is guarded defensively so the registry API still
# mounts if that optional module is absent, in which case the helpers below fall
# back to raw SQL against the `conversion_webhooks` table. Either way the HTTP
# shape is identical so the UI / CLI doesn't have to branch.
# ---------------------------------------------------------------------------

try:
    from .conversion import (  # type: ignore[import-not-found]
        ConversionWebhookRegistry as _ConversionWebhookRegistry,
        WebhookNotFound as _WebhookNotFound,
        WebhookValidationError as _WebhookValidationError,
    )
    from ..schemas.versioning import ConversionWebhook as _ConversionWebhookSchema
    _HAS_L112 = True
except ImportError:  # pragma: no cover
    _ConversionWebhookRegistry = None  # type: ignore[assignment,misc]
    _WebhookNotFound = Exception  # type: ignore[assignment,misc]
    _WebhookValidationError = Exception  # type: ignore[assignment,misc]
    _ConversionWebhookSchema = None  # type: ignore[assignment,misc]
    _HAS_L112 = False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"
DEV_MODE_ENV = "LEGBA_DEV_MODE"

# The model-serving stack-component kinds a deployment MUST configure before
# the analysis layer can run: the LLM provider, the embedding service, and the
# hosted NLP service. The `/config/status` route reports first-run readiness
# against these; the Settings panel walks the operator through configuring
# them. Substrate components (postgres/nats/redis/…) are bootstrapped from the
# registry DSN at bring-up and are not part of the first-run wizard.
REQUIRED_MODEL_COMPONENT_KINDS: tuple[str, ...] = (
    "llm_provider",
    "embedding",
    "nlp_service",
)

# Returned on every guarded request when the gate is misconfigured (B-2).
MISCONFIGURED_AUTH_DETAIL = (
    f"registry API misconfigured: {API_TOKEN_ENV} is unset/empty and "
    f"{DEV_MODE_ENV}=1 is not set; refusing all guarded requests. "
    f"Set a bearer token, or set {DEV_MODE_ENV}=1 explicitly for local "
    "development."
)


def _current_token() -> str | None:
    raw = os.getenv(API_TOKEN_ENV, "").strip()
    return raw or None


def _dev_mode() -> bool:
    """Explicit development-mode opt-in (`LEGBA_DEV_MODE=1`).

    Only consulted when `LEGBA_REGISTRY_API_TOKEN` is unset/empty — a
    configured token is ALWAYS enforced, dev flag or not.
    """
    return os.getenv(DEV_MODE_ENV, "").strip() == "1"


def _token_matches(presented: str, configured: str) -> bool:
    """Constant-time bearer comparison (B-2)."""
    return hmac.compare_digest(
        presented.encode("utf-8"), configured.encode("utf-8"),
    )


def require_bearer(
    authorization: str | None = Header(default=None),
) -> str:
    """Bearer-token gate for the HTTP endpoints (B-2: fail-closed).

      * `LEGBA_REGISTRY_API_TOKEN` set → require `Authorization: Bearer
        <token>`; missing header → 401, mismatch → 403 (constant-time
        compare via `hmac.compare_digest`).
      * Unset/empty AND `LEGBA_DEV_MODE=1` → development mode: any
        bearer (and a missing header) accepted.
      * Unset/empty otherwise → 503 "service misconfigured" on EVERY
        guarded request. A deploy that forgot the token must not expose
        an open admin API.

    Returns the supplied principal token (or `"anonymous"` in dev mode) so
    handlers can stamp it as `actor` on audit-log rows.
    """
    configured = _current_token()
    if configured is None:
        if not _dev_mode():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=MISCONFIGURED_AUTH_DETAIL,
            )
        if not authorization:
            return "anonymous"
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization must be 'Bearer <token>'",
            )
        return authorization[7:].strip() or "anonymous"
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization: Bearer <token>",
        )
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be 'Bearer <token>'",
        )
    presented = authorization[7:].strip()
    if not _token_matches(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid bearer token",
        )
    return presented


#: End of the C3 deprecation window for the routes the source-quality ledger
#: merges (``/source_credibility`` reads + ``/sources/{id}/assurance``). They
#: KEEP SERVING their original wire shape until then — a 3xx would silently
#: hand callers a DIFFERENT response body, which is worse than a header they
#: can see. Removal is a later, deliberate commit, not a side effect of this
#: one (`docs/SEAMS.md`, the C3 entry).
DEPRECATION_SUNSET_HTTP_DATE = "Tue, 27 Oct 2026 00:00:00 GMT"


def sunset_headers(successor: str) -> Callable[[Response], None]:
    """A FastAPI dependency that marks a route deprecated, per RFC 8594/9745.

    Stamps ``Deprecation: true``, ``Sunset: <date>`` and a
    ``Link: <successor>; rel="successor-version"`` on the response. The route
    keeps serving its original body unchanged — this advertises the window, it
    does not shorten it. Attach per-route (``dependencies=[Depends(
    sunset_headers("/api/v1/v3/..."))]``) so a module's WRITE routes, which
    have no successor, are not swept up with its reads.
    """

    def _stamp(response: Response) -> None:
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = DEPRECATION_SUNSET_HTTP_DATE
        response.headers["Link"] = f'<{successor}>; rel="successor-version"'

    return _stamp


def _bearer_from_header(authorization: str | None) -> str | None:
    """Extract the raw token from an `Authorization: Bearer <token>` header.

    Returns the stripped token, or `None` when the header is absent or not a
    well-formed `Bearer` credential (the caller then falls back to `?token=`).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


def _authorize_ws_token(
    token: str | None,
    authorization: str | None = None,
) -> str:
    """WebSocket auth: the credential may arrive two ways —

      * `?token=` query-string — browsers can't set custom headers on the
        upgrade request, so the SPA passes the token here.
      * `Authorization: Bearer <token>` header — injected by the Caddy
        reverse proxy on the proxied WS upgrade for the canonical
        deployment (the operator's browser never sees it), so a header
        bearer also authenticates (ITEM 2.5).

    The query token is preferred when present; otherwise the header bearer
    is used. Returns the principal or raises HTTPException for the
    connection handler to convert to a WebSocket close frame.

    Mirrors `require_bearer` exactly (B-2): fail-closed 503 when the token
    is unconfigured and `LEGBA_DEV_MODE=1` is not set; constant-time
    comparison when configured."""
    presented = token or _bearer_from_header(authorization)
    configured = _current_token()
    if configured is None:
        if not _dev_mode():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=MISCONFIGURED_AUTH_DETAIL,
            )
        return presented or "anonymous"
    if not presented or not _token_matches(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid token",
        )
    return presented


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class RegistryAPIDeps:
    """Injected at app-construction time; all routes resolve via this."""

    descriptor_registry: DescriptorRegistry
    stack_registry: StackRegistry
    vault: CredentialVault
    dlq: DescriptorDeadLetter
    audit_logger: AuditLogger
    vocabulary_cache: VocabularyCache
    nats_store: Any | None = None  # for WebSocket multiplexing
    conversion_registry: Any | None = None  # ConversionWebhookRegistry | None


def _get_deps(request: Request) -> RegistryAPIDeps:
    deps = getattr(request.app.state, "registry_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="registry api not configured (missing RegistryAPIDeps)",
        )
    return deps


# ---------------------------------------------------------------------------
# Response / request pydantic shapes
# ---------------------------------------------------------------------------


class DescriptorRowOut(BaseModel):
    """JSON view of `DescriptorRow`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    descriptor_id: str
    version: str
    schema_uri: str
    is_head: bool
    state: str
    owner: str
    name: str
    family: str
    body: dict[str, Any]
    created_at: datetime
    abstraction_level: str | None = None
    inherits: list[str] = Field(default_factory=list)
    retire_after: datetime | None = None
    kind: str | None = None
    type_signature: dict[str, Any] | None = None
    # Non-fatal registration-time notes (X-1 dead-config warnings etc.) —
    # empty on the common path; populated when `register()`/`update()`
    # surfaced something the caller should see immediately.
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: DescriptorRow) -> "DescriptorRowOut":
        return cls(
            descriptor_id=row.descriptor_id,
            version=row.version,
            schema_uri=row.schema_uri,
            is_head=row.is_head,
            state=row.state,
            owner=row.owner,
            name=row.name,
            family=row.family.value,
            body=row.body,
            created_at=row.created_at,
            abstraction_level=row.abstraction_level,
            inherits=list(row.inherits or []),
            retire_after=row.retire_after,
            kind=row.kind,
            type_signature=row.type_signature,
            warnings=list(row.warnings or []),
        )


# ---------------------------------------------------------------------------
# P-05 — `source` + `action_pack` read views the UI consumes.
#
# These are CONVENIENCE read shapes over the same `source_descriptors` /
# `action_pack_descriptors` rows the generic `/descriptors/{family}` routes
# write. They project the descriptor body's source-/pack-specific surface to
# the top level so the L-204/P-15 UI (registry.sources panel + source.detail)
# doesn't have to dig into `body` for the fields it renders in every list row.
#
# FROZEN RESPONSE SHAPE (P-05 acceptance — "freeze the source-facing REST
# response shapes now so P-15 doesn't reverse-engineer"). Any change here MUST
# be mirrored in legba-ui-v3 types + this docstring bumped. The raw descriptor
# body is still available verbatim under `.body` and via the generic
# `/descriptors/source/{id}` route — these projections are additive sugar, not
# a replacement.
# ---------------------------------------------------------------------------


class SourceDescriptorOut(BaseModel):
    """UI list/detail row for a source descriptor.

    Mirrors `DescriptorRowOut` (identity columns) + the source-specific
    surface (`kind`, `acquisition`, scope, output/cadence/policy summary) the
    UI shows without opening the full body.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    descriptor_id: str
    version: str
    schema_uri: str
    is_head: bool
    state: str
    owner: str
    name: str
    abstraction_level: str | None = None
    inherits: list[str] = Field(default_factory=list)
    created_at: datetime
    retire_after: datetime | None = None
    # source-specific projection
    kind: str | None = None
    acquisition: str | None = None
    subscription_policy: str | None = None
    owner_tenant: str | None = None
    geo: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    has_discovery: bool = False
    has_provision: bool = False
    output_subject: str | None = None
    # P3-1 (assurance ledger) — the current PUBLIC Admiralty display grade
    # (reliability×credibility, e.g. "B2") from `source_ratings` (migration
    # 0094); null when the source is ungraded. Stamped post-`from_row` by the
    # list/detail routes via `load_assurance_grades` (additive: the field is
    # display-only and NEVER feeds the faithfulness score — A6 hard rule).
    assurance_grade: str | None = None
    # P3-3 (A6 layer 3) — the EARNED smoothed win-rate from `source_track_records`
    # (migration 0099): the MEASURED counterpart of the asserted grade above.
    # Beta-smoothed (prior-damped) rate over resolved contentions; null when the
    # source has no resolved-contest sample. Stamped post-`from_row` via
    # `load_earned_win_rates` (additive, display-only; feeds weighting/tie-break/
    # display ONLY — NEVER the faithfulness score, A6 hard rule).
    earned_win_rate: float | None = None
    # full descriptor body, verbatim (the detail view + editor reads this)
    body: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: DescriptorRow) -> "SourceDescriptorOut":
        body = row.body or {}
        scope = body.get("scope") or {}
        output = body.get("output") or {}
        source_id = body.get("identity", {}).get("id", row.descriptor_id)
        subject = output.get("subject_prefix") or f"source.{source_id}.signals"
        return cls(
            descriptor_id=row.descriptor_id,
            version=row.version,
            schema_uri=row.schema_uri,
            is_head=row.is_head,
            state=row.state,
            owner=row.owner,
            name=row.name,
            abstraction_level=row.abstraction_level,
            inherits=list(row.inherits or []),
            created_at=row.created_at,
            retire_after=row.retire_after,
            kind=row.kind,
            acquisition=body.get("acquisition"),
            subscription_policy=body.get("subscription_policy"),
            owner_tenant=scope.get("owner_tenant"),
            geo=list(scope.get("geo") or []),
            languages=list(scope.get("languages") or []),
            tags=list(scope.get("tags") or []),
            has_discovery=body.get("discovery") is not None,
            has_provision=bool((body.get("provision") or {}).get("enabled")),
            output_subject=subject,
            body=body,
        )


async def load_assurance_grades(
    pg: Any, source_ids: list[str],
) -> dict[str, str]:
    """Current PUBLIC Admiralty grade per source id (P3-1 assurance ledger).

    One query over ``source_ratings`` (migration 0094): per source, the most
    recent CURRENT (``superseded_by IS NULL``) ``visibility_class='public'``
    row carrying BOTH Admiralty halves wins; sources with no such row are
    absent from the map. Private-annex rows are NEVER consulted here — this
    feeds public projections (the ``/sources`` list + the assurance route's
    ``assurance_grade``).

    Degrades to ``{}`` (all-null grades) when the table does not exist yet —
    a registry rolled forward ahead of migration 0094 must not 500 the
    source list over an additive display column.
    """
    if not source_ids:
        return {}
    try:
        async with pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (source_id)
                       source_id,
                       admiralty_reliability || admiralty_credibility AS grade
                  FROM source_ratings
                 WHERE source_id = ANY($1::text[])
                   AND visibility_class = 'public'
                   AND superseded_by IS NULL
                   AND admiralty_reliability IS NOT NULL
                   AND admiralty_credibility IS NOT NULL
                 ORDER BY source_id, rated_at DESC
                """,
                list(source_ids),
            )
    except asyncpg.UndefinedTableError:
        logger.warning(
            "assurance grades unavailable: source_ratings table missing "
            "(migration 0094 not applied) — serving null grades",
        )
        return {}
    return {r["source_id"]: r["grade"] for r in rows}


async def load_earned_win_rates(
    pg: Any, source_ids: list[str],
) -> dict[str, float]:
    """Current EARNED smoothed win-rate per source id (P3-3 assurance layer 3).

    One query over ``source_track_records`` (migration 0099): the Beta-smoothed
    (prior-damped) win-rate for each source that HAS a resolved-contest sample
    (``contested_total > 0``); sources with no sample are absent from the map
    (their raw rate is undefined and the smoothed 0.5 would read as a real
    measurement it is not). Feeds the additive ``/sources`` ``earned_win_rate``
    projection — display-only, NEVER the faithfulness score (A6 hard rule).

    Degrades to ``{}`` when the table does not exist yet — a registry rolled
    forward ahead of migration 0099 must not 500 the source list over an
    additive display column.
    """
    if not source_ids:
        return {}
    try:
        async with pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source_id, win_rate_smoothed
                  FROM source_track_records
                 WHERE source_id = ANY($1::text[])
                   AND contested_total > 0
                """,
                list(source_ids),
            )
    except asyncpg.UndefinedTableError:
        logger.warning(
            "earned win-rates unavailable: source_track_records table missing "
            "(migration 0099 not applied) — serving null rates",
        )
        return {}
    return {r["source_id"]: float(r["win_rate_smoothed"]) for r in rows}


class ActionPackOut(BaseModel):
    """UI list/detail row for an action-pack descriptor."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    descriptor_id: str
    version: str
    schema_uri: str
    is_head: bool
    state: str
    owner: str
    name: str
    abstraction_level: str | None = None
    inherits: list[str] = Field(default_factory=list)
    created_at: datetime
    retire_after: datetime | None = None
    # pack-specific projection
    tool_names: list[str] = Field(default_factory=list)
    channel_names: list[str] = Field(default_factory=list)
    applies_to_tags: list[str] = Field(default_factory=list)
    has_governor: bool = False
    body: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: DescriptorRow) -> "ActionPackOut":
        body = row.body or {}
        governor = body.get("governor") or {}
        return cls(
            descriptor_id=row.descriptor_id,
            version=row.version,
            schema_uri=row.schema_uri,
            is_head=row.is_head,
            state=row.state,
            owner=row.owner,
            name=row.name,
            abstraction_level=row.abstraction_level,
            inherits=list(row.inherits or []),
            created_at=row.created_at,
            retire_after=row.retire_after,
            tool_names=[t.get("name") for t in (body.get("tools") or []) if t.get("name")],
            channel_names=[c.get("name") for c in (body.get("channels") or []) if c.get("name")],
            applies_to_tags=list(body.get("applies_to_tags") or []),
            has_governor=any(v is not None for v in governor.values()),
            body=body,
        )


class StackComponentOut(BaseModel):
    component_id: str
    version: str
    schema_uri: str
    kind: str
    is_head: bool
    state: str
    owner: str
    name: str
    body: dict[str, Any]
    created_at: datetime


class StackHealthOut(BaseModel):
    component_id: str
    kind: str
    state: str
    checked_at: datetime
    detail: str = ""
    last_success_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RequiredComponentStatus(BaseModel):
    """First-run readiness for one required model-serving component kind.

    Secrets-safe: this carries only identity + lifecycle metadata
    (component_id / name / state), NEVER any credential value. The vault is
    never read here — `register`/`update` route secrets through the vault as
    references, and this status view only checks for the row's existence and
    lifecycle state.
    """

    kind: str
    configured: bool
    active: bool
    component_id: str | None = None
    name: str | None = None
    state: str | None = None


class ConfigStatusOut(BaseModel):
    """Aggregate first-run config readiness.

    `first_run` is True when ANY required model-serving component
    (llm_provider / embedding / nlp_service) has no non-retired head row yet —
    the UI then shows its "required components unconfigured → configure here"
    onboarding state instead of the normal panels.
    """

    first_run: bool
    all_configured: bool
    all_active: bool
    required: list[RequiredComponentStatus]


class PromoteBody(BaseModel):
    candidate_version: str


class RollbackBody(BaseModel):
    target_version: str
    reason: str | None = None


class RetireBody(BaseModel):
    reason: str | None = None


class TransitionBody(BaseModel):
    """Drive an FSM lifecycle transition on a descriptor head.

    `to_state` is one of the `LifecycleState` values; the transition is
    validated against `ALLOWED_TRANSITIONS` by the registry's existing
    `update()` path (which carries state into a new content-hash version).
    Retiring still has its own dedicated `/retire` route; this covers the
    draft→configured→active→paused moves the source registry needs.
    """

    to_state: str
    reason: str | None = None


class SecretRegisterBody(BaseModel):
    secret_id: str
    plaintext: str = Field(min_length=1, description="UTF-8 plaintext; NEVER logged")
    notes: str | None = None


class SecretExistsOut(BaseModel):
    secret_id: str
    exists: bool


class ConversionWebhookIn(BaseModel):
    from_uri: str
    to_uri: str
    impl: str
    direction: Literal["forward", "bidirectional"] = "forward"
    notes: str | None = None


class ConversionWebhookOut(BaseModel):
    id: str
    from_uri: str
    to_uri: str
    impl: str
    direction: str
    notes: str | None
    created_at: datetime


class DLQEntryOut(BaseModel):
    id: str
    attempted_at: datetime
    actor: str
    namespace: str
    declared_schema_uri: str | None = None
    validation_error: dict[str, Any] = Field(default_factory=dict)
    resolution: str | None = None
    attempted_payload: dict[str, Any] | None = None


class DLQResubmitBody(BaseModel):
    patch: dict[str, Any] | None = Field(
        default=None,
        description="Optional shallow-merge patch over the attempted_payload "
                    "before re-submission.",
    )


class AuditEntryOut(BaseModel):
    id: str
    occurred_at: datetime
    actor_id: str
    actor_role: str
    namespace: str
    descriptor_id: str
    action: str
    from_version: str | None
    to_version: str | None
    change_summary: dict[str, Any]
    signer_did: str
    signature_verified: bool | None = Field(
        default=None,
        description="True/False if the entry was re-verified inline against "
                    "the registry signing identity; null if no verifier was "
                    "configured (audit reader-only mode).",
    )


class VocabularyEntryIn(BaseModel):
    value: str
    notes: str | None = None
    aliases: list[str] = Field(default_factory=list)
    parent: str | None = None


class VocabularyEntryOut(BaseModel):
    id: str
    family: str
    value: str
    schema_uri: str
    introduced: datetime
    deprecated: datetime | None
    notes: str | None
    aliases: list[str]
    parent: str | None


class VocabularyEntryUpdate(BaseModel):
    notes: str | None = None
    aliases: list[str] | None = None
    parent: str | None = None


# ---------------------------------------------------------------------------
# L-204 — UI panel registry surface (`outputs.ui_panel`).
#
# L-192 owns the SQL + Python registry (`legba.data.outputs.ui_panel`); this
# block exposes a thin read-only REST view for the L-204 frontend to fetch
# panel registrations at boot and after `registry.bindings.activated`
# events. The WebSocket subscription that gates "something changed →
# refetch" reuses the existing `/events` endpoint with subject filter
# `registry.bindings.>`.
# ---------------------------------------------------------------------------


class UIPanelRegistrationOut(BaseModel):
    """JSON view of `legba.data.outputs.ui_panel.PanelRegistration`.

    Field set mirrors the dataclass column-for-column so the frontend
    `PanelRegistration` interface (legba-ui-v3/src/types.ts) round-trips
    cleanly. Updates here MUST be mirrored in the TS interface.
    """

    id: str
    panel_id: str
    descriptor_id: str
    descriptor_version: str
    descriptor_family: str
    analyst_id: str | None
    title: str
    mode: str
    layout_slot: str
    data_query: dict[str, Any] = Field(default_factory=dict)
    binding: dict[str, Any] = Field(default_factory=dict)
    retired: bool
    created_at: datetime
    retired_at: datetime | None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the v1 registry router bound to a dep bundle.

    Mount on an app via:

        app = FastAPI()
        app.state.registry_deps = deps
        app.include_router(build_router(deps), prefix="/api/v1/registry")
    """
    router = APIRouter(tags=["registry"])

    # ------------------------------------------------------------------
    # Descriptor registry
    # ------------------------------------------------------------------

    def _parse_family(family: str) -> Family:
        try:
            return Family(family)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown descriptor family {family!r}; "
                       f"expected target|analyst|source|action_pack",
            ) from exc

    def _parse_descriptor(family: Family, body: dict[str, Any]):
        """Pydantic-parse the inbound descriptor JSON. Errors get re-raised
        as 422 so the validation rationale reaches the caller untruncated.

        Uses `strict=False` so JSON-native types (string enums, ISO-format
        datetimes) coerce into the typed model — the schemas declare
        `strict=True` for in-process construction safety; over HTTP the
        wire format is always JSON.

        Family-aware per the source-first pivot (P-05): the descriptor
        family selects the pydantic class. `Family.model` is the single
        source of truth (it's the same map the registry insert/fetch path
        uses), so adding a family is a one-line change there, not here.
        """
        cls = family.model
        try:
            return cls.model_validate(body, strict=False)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "pydantic_validation",
                    "message": str(exc),
                },
            ) from exc

    @router.post(
        "/descriptors/{family}",
        response_model=DescriptorRowOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_descriptor(
        family: str,
        body: dict[str, Any],
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        fam = _parse_family(family)
        descriptor = _parse_descriptor(fam, body)
        try:
            row = await deps_.descriptor_registry.register(descriptor, actor=actor)
        except DescriptorValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "validation",
                    "message": str(exc),
                    "dead_letter_id": exc.dead_letter_id,
                    "validation_error": exc.validation_error,
                },
            ) from exc
        except VersionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc),
            ) from exc
        return DescriptorRowOut.from_row(row)

    @router.get("/descriptors/{family}/{descriptor_id}", response_model=DescriptorRowOut)
    async def get_descriptor(
        family: str,
        descriptor_id: str,
        version: str | None = Query(default=None),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        fam = _parse_family(family)
        try:
            row = await deps_.descriptor_registry.get(
                descriptor_id, family=fam, version=version,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return DescriptorRowOut.from_row(row)

    @router.get("/descriptors/{family}/{descriptor_id}/typed")
    async def get_descriptor_typed(
        family: str,
        descriptor_id: str,
        version: str | None = Query(default=None),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        fam = _parse_family(family)
        try:
            typed = await deps_.descriptor_registry.get_typed(
                descriptor_id, family=fam, version=version,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Return the typed pydantic dump verbatim — this is the "what the
        # registry parsed it as" view, useful for round-trip tooling.
        return typed.model_dump(mode="json", by_alias=True)

    @router.get(
        "/descriptors/{family}/{descriptor_id}/history",
        response_model=list[DescriptorRowOut],
    )
    async def get_descriptor_history(
        family: str,
        descriptor_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[DescriptorRowOut]:
        fam = _parse_family(family)
        rows = await deps_.descriptor_registry.query_history(
            descriptor_id, family=fam,
        )
        return [DescriptorRowOut.from_row(r) for r in rows]

    @router.put(
        "/descriptors/{family}/{descriptor_id}",
        response_model=DescriptorRowOut,
    )
    async def update_descriptor(
        family: str,
        descriptor_id: str,
        body: dict[str, Any],
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        fam = _parse_family(family)
        descriptor = _parse_descriptor(fam, body)
        try:
            row = await deps_.descriptor_registry.update(
                descriptor_id, descriptor, actor=actor,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DescriptorValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "validation",
                    "message": str(exc),
                    "dead_letter_id": exc.dead_letter_id,
                    "validation_error": exc.validation_error,
                },
            ) from exc
        except (VersionConflict, IllegalLifecycleTransition) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return DescriptorRowOut.from_row(row)

    @router.post(
        "/descriptors/{family}/{descriptor_id}/retire",
        response_model=DescriptorRowOut,
    )
    async def retire_descriptor(
        family: str,
        descriptor_id: str,
        body: RetireBody | None = None,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        fam = _parse_family(family)
        reason = body.reason if body else None
        try:
            row = await deps_.descriptor_registry.retire(
                descriptor_id, actor=actor, family=fam, reason=reason,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except IllegalLifecycleTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return DescriptorRowOut.from_row(row)

    @router.post(
        "/descriptors/{family}/{descriptor_id}/transition",
        response_model=DescriptorRowOut,
    )
    async def transition_descriptor(
        family: str,
        descriptor_id: str,
        body: TransitionBody,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        """Drive a lifecycle FSM transition (draft→configured→active→paused…)
        through the existing registry `update()` path.

        We don't reach into the table directly: we fetch the typed head,
        re-stamp `identity.state`, and call `registry.update()`, which (a)
        re-runs vocabulary/pydantic validation, (b) enforces
        `ALLOWED_TRANSITIONS`, (c) mints an audited content-hash version, and
        (d) publishes the descriptor event — i.e. the *same* lifecycle path
        every other mutation uses. `retired` is rejected here so the audit
        `action` stays accurate — use `/retire`.
        """
        fam = _parse_family(family)
        try:
            to_state = LifecycleState(body.to_state)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"unknown lifecycle state {body.to_state!r}; "
                       f"expected {[s.value for s in LifecycleState]}",
            ) from exc
        if to_state is LifecycleState.RETIRED:
            raise HTTPException(
                status_code=400,
                detail="use POST /retire to move a descriptor to 'retired'",
            )
        try:
            typed = await deps_.descriptor_registry.get_typed(
                descriptor_id, family=fam,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        new_identity = typed.identity.model_copy(update={"state": to_state})
        restamped = typed.model_copy(update={"identity": new_identity})
        try:
            row = await deps_.descriptor_registry.update(
                descriptor_id, restamped, actor=actor,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DescriptorValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "validation",
                    "message": str(exc),
                    "dead_letter_id": exc.dead_letter_id,
                    "validation_error": exc.validation_error,
                },
            ) from exc
        except (VersionConflict, IllegalLifecycleTransition) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return DescriptorRowOut.from_row(row)

    @router.post(
        "/descriptors/{family}/{descriptor_id}/promote",
        response_model=DescriptorRowOut,
    )
    async def promote_descriptor(
        family: str,
        descriptor_id: str,
        body: PromoteBody,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        fam = _parse_family(family)
        try:
            row = await deps_.descriptor_registry.promote(
                descriptor_id, body.candidate_version, actor=actor, family=fam,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return DescriptorRowOut.from_row(row)

    @router.post(
        "/descriptors/{family}/{descriptor_id}/rollback",
        response_model=DescriptorRowOut,
    )
    async def rollback_descriptor(
        family: str,
        descriptor_id: str,
        body: RollbackBody,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DescriptorRowOut:
        fam = _parse_family(family)
        try:
            row = await deps_.descriptor_registry.rollback(
                descriptor_id,
                body.target_version,
                actor=actor,
                family=fam,
                reason=body.reason,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return DescriptorRowOut.from_row(row)

    @router.get("/descriptors", response_model=list[DescriptorRowOut])
    async def list_descriptors(
        family: str | None = Query(default=None),
        state: str | None = Query(default=None),
        kind: str | None = Query(default=None),
        schema_uri: str | None = Query(default=None),
        owner: str | None = Query(default=None),
        abstraction_level: str | None = Query(default=None),
        descriptor_id: str | None = Query(default=None),
        head_only: bool = Query(default=True),
        limit: int | None = Query(default=None, ge=1, le=1000),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[DescriptorRowOut]:
        fam = _parse_family(family) if family else None
        pred = DescriptorPredicate(
            family=fam,
            descriptor_id=descriptor_id,
            state=state,
            schema_uri=schema_uri,
            owner=owner,
            kind=kind,
            abstraction_level=abstraction_level,
            head_only=head_only,
            limit=limit,
        )
        rows = await deps_.descriptor_registry.list(pred)
        return [DescriptorRowOut.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # P-05 — `source` read view (registry.sources) the UI needs.
    #
    # The generic `/descriptors/source/*` routes already cover the full
    # register→get→update→retire→list→typed→history lifecycle for the source
    # family (family is a path param; the registry insert/fetch path is
    # family-aware). These two routes are the UI's read sugar: a projected
    # list + by-id detail over `source_descriptors` with the source-specific
    # surface lifted to the top level (so the registry.sources panel renders a
    # row without opening `body`). Response shape frozen per P-05 acceptance.
    # ------------------------------------------------------------------

    @router.get("/sources", response_model=list[SourceDescriptorOut])
    async def list_sources(
        state: str | None = Query(default=None),
        kind: str | None = Query(default=None),
        owner: str | None = Query(default=None),
        schema_uri: str | None = Query(default=None),
        descriptor_id: str | None = Query(default=None),
        head_only: bool = Query(default=True),
        limit: int | None = Query(default=None, ge=1, le=1000),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[SourceDescriptorOut]:
        pred = DescriptorPredicate(
            family=Family.SOURCE,
            descriptor_id=descriptor_id,
            state=state,
            schema_uri=schema_uri,
            owner=owner,
            kind=kind,
            head_only=head_only,
            limit=limit,
        )
        rows = await deps_.descriptor_registry.list(pred)
        out = [SourceDescriptorOut.from_row(r) for r in rows]
        # P3-1 — stamp the current public Admiralty grade (additive column;
        # one batched query, null for ungraded sources). P3-3 — the EARNED
        # smoothed win-rate beside it (additive, one batched query, null when
        # the source has no resolved-contest sample).
        ids = [o.descriptor_id for o in out]
        grades = await load_assurance_grades(deps_.descriptor_registry.pg, ids)
        earned = await load_earned_win_rates(deps_.descriptor_registry.pg, ids)
        for o in out:
            o.assurance_grade = grades.get(o.descriptor_id)
            o.earned_win_rate = earned.get(o.descriptor_id)
        return out

    @router.get("/sources/{descriptor_id}", response_model=SourceDescriptorOut)
    async def get_source(
        descriptor_id: str,
        version: str | None = Query(default=None),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SourceDescriptorOut:
        try:
            row = await deps_.descriptor_registry.get(
                descriptor_id, family=Family.SOURCE, version=version,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        out = SourceDescriptorOut.from_row(row)
        grades = await load_assurance_grades(
            deps_.descriptor_registry.pg, [out.descriptor_id],
        )
        earned = await load_earned_win_rates(
            deps_.descriptor_registry.pg, [out.descriptor_id],
        )
        out.assurance_grade = grades.get(out.descriptor_id)
        out.earned_win_rate = earned.get(out.descriptor_id)
        return out

    # ------------------------------------------------------------------
    # P-05 — `action_pack` read view the UI needs (same mirror).
    # ------------------------------------------------------------------

    @router.get("/action_packs", response_model=list[ActionPackOut])
    async def list_action_packs(
        state: str | None = Query(default=None),
        owner: str | None = Query(default=None),
        schema_uri: str | None = Query(default=None),
        descriptor_id: str | None = Query(default=None),
        head_only: bool = Query(default=True),
        limit: int | None = Query(default=None, ge=1, le=1000),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[ActionPackOut]:
        pred = DescriptorPredicate(
            family=Family.ACTION_PACK,
            descriptor_id=descriptor_id,
            state=state,
            schema_uri=schema_uri,
            owner=owner,
            head_only=head_only,
            limit=limit,
        )
        rows = await deps_.descriptor_registry.list(pred)
        return [ActionPackOut.from_row(r) for r in rows]

    @router.get("/action_packs/{descriptor_id}", response_model=ActionPackOut)
    async def get_action_pack(
        descriptor_id: str,
        version: str | None = Query(default=None),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> ActionPackOut:
        try:
            row = await deps_.descriptor_registry.get(
                descriptor_id, family=Family.ACTION_PACK, version=version,
            )
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ActionPackOut.from_row(row)

    # ------------------------------------------------------------------
    # Stack registry
    # ------------------------------------------------------------------

    def _stack_row_out(row) -> StackComponentOut:
        return StackComponentOut(
            component_id=row.component_id,
            version=row.version,
            schema_uri=row.schema_uri,
            kind=row.kind,
            is_head=row.is_head,
            state=row.state.value if hasattr(row.state, "value") else str(row.state),
            owner=row.owner,
            name=row.name,
            body=row.body,
            created_at=row.created_at,
        )

    @router.post(
        "/stack",
        response_model=StackComponentOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_stack(
        body: dict[str, Any],
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> StackComponentOut:
        try:
            row = await deps_.stack_registry.register(body, actor)
        except StackValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "validation",
                    "message": str(exc),
                    "validation_error": exc.validation_error,
                },
            ) from exc
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _stack_row_out(row)

    @router.get("/stack/{component_id}", response_model=StackComponentOut)
    async def get_stack(
        component_id: str,
        version: str | None = Query(default=None),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> StackComponentOut:
        try:
            row = await deps_.stack_registry.get(component_id, version=version)
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _stack_row_out(row)

    @router.get(
        "/stack/by-kind/{kind}/{name}", response_model=list[StackComponentOut]
    )
    async def get_stack_by_kind(
        kind: str,
        name: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[StackComponentOut]:
        rows = await deps_.stack_registry.get_by_kind(kind, name=name)
        return [_stack_row_out(r) for r in rows]

    @router.get("/stack", response_model=list[StackComponentOut])
    async def list_stack(
        kind: str | None = Query(default=None),
        state: str | None = Query(default=None),
        include_history: bool = Query(default=False),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[StackComponentOut]:
        state_enum: LifecycleState | None = None
        if state is not None:
            try:
                state_enum = LifecycleState(state)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        rows = await deps_.stack_registry.list(
            kind=kind, state=state_enum, include_history=include_history,
        )
        return [_stack_row_out(r) for r in rows]

    @router.put("/stack/{component_id}", response_model=StackComponentOut)
    async def update_stack(
        component_id: str,
        body: dict[str, Any],
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> StackComponentOut:
        try:
            row = await deps_.stack_registry.update(component_id, body, actor)
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StackValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "validation", "message": str(exc)},
            ) from exc
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _stack_row_out(row)

    @router.post(
        "/stack/{component_id}/retire", response_model=StackComponentOut,
    )
    async def retire_stack(
        component_id: str,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> StackComponentOut:
        try:
            row = await deps_.stack_registry.retire(component_id, actor)
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except IllegalLifecycleTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _stack_row_out(row)

    @router.post(
        "/stack/{component_id}/healthcheck", response_model=StackHealthOut,
    )
    async def healthcheck_stack(
        component_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> StackHealthOut:
        try:
            health = await deps_.stack_registry.healthcheck(component_id)
        except DescriptorNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return StackHealthOut(
            component_id=health.component_id,
            kind=health.kind,
            state=health.state.value,
            checked_at=health.checked_at,
            detail=health.detail,
            last_success_at=health.last_success_at,
            extra=dict(health.extra),
        )

    # ------------------------------------------------------------------
    # First-run config readiness (config-honesty stream).
    #
    # The runtime source of truth is the `stack_components` registry, not
    # `.env` (which only SEEDS these rows once at bring-up). This route lets
    # the UI ask "are the required model-serving components configured yet?"
    # so it can show a first-run "configure here" state when they're absent
    # — instead of rendering empty panels against an unconfigured stack.
    #
    # Secrets-safe: only identity + lifecycle state are returned; the vault is
    # never touched and no credential value is exposed.
    # ------------------------------------------------------------------

    @router.get("/config/status", response_model=ConfigStatusOut)
    async def config_status(
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> ConfigStatusOut:
        required: list[RequiredComponentStatus] = []
        for kind in REQUIRED_MODEL_COMPONENT_KINDS:
            rows = await deps_.stack_registry.list(kind=kind)
            # Ignore retired heads — a retired component is not "configured".
            live = [r for r in rows if r.state != LifecycleState.RETIRED]
            chosen = None
            # Prefer an ACTIVE head; otherwise the first non-retired one.
            for r in live:
                if r.state == LifecycleState.ACTIVE:
                    chosen = r
                    break
            if chosen is None and live:
                chosen = live[0]
            required.append(
                RequiredComponentStatus(
                    kind=kind,
                    configured=chosen is not None,
                    active=chosen is not None
                    and chosen.state == LifecycleState.ACTIVE,
                    component_id=chosen.component_id if chosen else None,
                    name=chosen.name if chosen else None,
                    state=chosen.state.value if chosen else None,
                )
            )
        all_configured = all(c.configured for c in required)
        all_active = all(c.active for c in required)
        return ConfigStatusOut(
            first_run=not all_configured,
            all_configured=all_configured,
            all_active=all_active,
            required=required,
        )

    # ------------------------------------------------------------------
    # Credential vault — register / delete / exists only. NEVER read.
    # ------------------------------------------------------------------

    @router.post(
        "/vault/secrets",
        status_code=status.HTTP_201_CREATED,
    )
    async def register_secret(
        body: SecretRegisterBody,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        try:
            version = await deps_.vault.store_secret(
                body.secret_id,
                body.plaintext,
                actor=actor,
                notes=body.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # NEVER echo the plaintext back; return only metadata.
        return {"secret_id": body.secret_id, "version": version}

    @router.delete("/vault/secrets/{secret_id}")
    async def delete_secret(
        secret_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        removed = await deps_.vault.delete_secret(secret_id)
        return {"secret_id": secret_id, "removed_rows": removed}

    @router.get(
        "/vault/secrets/{secret_id}/exists", response_model=SecretExistsOut,
    )
    async def secret_exists(
        secret_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SecretExistsOut:
        exists = await deps_.vault.verify_exists(secret_id)
        return SecretExistsOut(secret_id=secret_id, exists=exists)

    # ------------------------------------------------------------------
    # Conversion webhooks — proxied to the typed `ConversionWebhookRegistry`,
    # with a raw-table fallback when that optional module is absent.
    # ------------------------------------------------------------------

    @router.post(
        "/conversions",
        response_model=ConversionWebhookOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_conversion(
        body: ConversionWebhookIn,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> ConversionWebhookOut:
        return await _conv_register(deps_, body, actor=actor)

    @router.get(
        "/conversions", response_model=list[ConversionWebhookOut],
    )
    async def find_conversions(
        family: str | None = Query(default=None),
        from_uri: str | None = Query(default=None),
        to_uri: str | None = Query(default=None),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[ConversionWebhookOut]:
        return await _conv_find(deps_, family=family, from_uri=from_uri, to_uri=to_uri)

    @router.delete("/conversions/{webhook_id}")
    async def retire_conversion(
        webhook_id: str,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        return await _conv_delete(deps_, webhook_id, actor=actor)

    # ------------------------------------------------------------------
    # Dead-letter inspection (per L-107 §6)
    # ------------------------------------------------------------------

    @router.get("/dead_letter", response_model=list[DLQEntryOut])
    async def list_dead_letter(
        namespace: str | None = Query(default=None),
        family: str | None = Query(default=None),
        since: datetime | None = Query(default=None),
        include_resolved: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=1000),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[DLQEntryOut]:
        # `namespace` and `family` are aliases — `descriptor.dead_letter_for`
        # takes the descriptor `Family` enum which maps onto namespace
        # strings. `stack` is its own namespace string.
        ns = namespace or family
        async with deps_.descriptor_registry.pg.acquire() as conn:
            params: list[Any] = []
            sql = (
                "SELECT id, attempted_at, actor, namespace, "
                "declared_schema_uri, validation_error, resolution, "
                "attempted_payload "
                "FROM descriptor_dead_letter WHERE TRUE"
            )
            if ns is not None:
                params.append(ns)
                sql += f" AND namespace = ${len(params)}"
            if since is not None:
                params.append(since)
                sql += f" AND attempted_at >= ${len(params)}"
            if not include_resolved:
                sql += " AND resolution IS NULL"
            params.append(limit)
            sql += f" ORDER BY attempted_at DESC LIMIT ${len(params)}"
            rows = await conn.fetch(sql, *params)
        return [_dlq_row_out(r) for r in rows]

    @router.get("/dead_letter/{entry_id}", response_model=DLQEntryOut)
    async def get_dead_letter(
        entry_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> DLQEntryOut:
        try:
            uid = UUID(entry_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with deps_.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, attempted_at, actor, namespace, "
                "declared_schema_uri, validation_error, resolution, "
                "attempted_payload "
                "FROM descriptor_dead_letter WHERE id = $1",
                uid,
            )
        if row is None:
            raise HTTPException(status_code=404, detail=f"DLQ entry {entry_id} not found")
        return _dlq_row_out(row)

    @router.post("/dead_letter/{entry_id}/resubmit")
    async def resubmit_dead_letter(
        entry_id: str,
        body: DLQResubmitBody | None = None,
        actor: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        try:
            uid = UUID(entry_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async with deps_.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, namespace, attempted_payload, resolution "
                "FROM descriptor_dead_letter WHERE id = $1",
                uid,
            )
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"DLQ entry {entry_id} not found",
            )
        if row["resolution"] is not None:
            raise HTTPException(
                status_code=409,
                detail=f"DLQ entry already resolved ({row['resolution']})",
            )

        payload = row["attempted_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        elif payload is None:
            payload = {}
        else:
            payload = dict(payload)

        # Apply the patch (shallow merge — caller usually fixes one or two
        # top-level fields).
        if body and body.patch:
            payload = _shallow_merge(payload, body.patch)

        namespace = row["namespace"]
        # All descriptor families (target/analyst/source/action_pack) share
        # the typed register() path; `stack` is the one non-descriptor
        # namespace and gets its own branch below.
        descriptor_families = {f.value for f in Family}
        if namespace in descriptor_families:
            fam = Family(namespace)
            descriptor = _parse_descriptor(fam, payload)
            try:
                result = await deps_.descriptor_registry.register(
                    descriptor, actor=actor,
                )
            except DescriptorValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "still_invalid",
                        "message": str(exc),
                        "dead_letter_id": exc.dead_letter_id,
                    },
                ) from exc
            except VersionConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            # Mark the original DLQ row resolved with a pointer to the new id.
            await deps_.dlq.resolve(uid, "resubmitted_ok")
            return {
                "resubmitted": True,
                "descriptor_id": result.descriptor_id,
                "version": result.version,
            }
        if namespace == "stack":
            try:
                result = await deps_.stack_registry.register(payload, actor)
            except StackValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "still_invalid", "message": str(exc)},
                ) from exc
            except VersionConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            await deps_.dlq.resolve(uid, "resubmitted_ok")
            return {
                "resubmitted": True,
                "component_id": result.component_id,
                "version": result.version,
            }
        raise HTTPException(
            status_code=400,
            detail=f"unsupported DLQ namespace {namespace!r}",
        )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    @router.get("/audit", response_model=list[AuditEntryOut])
    async def list_audit(
        descriptor_id: str | None = Query(default=None),
        family: str | None = Query(default=None),
        since: datetime | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[AuditEntryOut]:
        async with deps_.descriptor_registry.pg.acquire() as conn:
            params: list[Any] = []
            sql = (
                "SELECT id, occurred_at, actor_id, actor_role, namespace, "
                "descriptor_id, action, from_version, to_version, "
                "change_summary, signed_payload, signer_did "
                "FROM descriptor_audit_log WHERE TRUE"
            )
            if descriptor_id is not None:
                params.append(descriptor_id)
                sql += f" AND descriptor_id = ${len(params)}"
            if family is not None:
                params.append(family)
                sql += f" AND namespace = ${len(params)}"
            if since is not None:
                params.append(since)
                sql += f" AND occurred_at >= ${len(params)}"
            params.append(limit)
            sql += f" ORDER BY occurred_at DESC LIMIT ${len(params)}"
            rows = await conn.fetch(sql, *params)
        return [_audit_row_out(r, verifier=deps_.audit_logger) for r in rows]

    @router.get("/audit/{entry_id}", response_model=AuditEntryOut)
    async def get_audit(
        entry_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> AuditEntryOut:
        try:
            uid = UUID(entry_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with deps_.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, occurred_at, actor_id, actor_role, namespace, "
                "descriptor_id, action, from_version, to_version, "
                "change_summary, signed_payload, signer_did "
                "FROM descriptor_audit_log WHERE id = $1",
                uid,
            )
        if row is None:
            raise HTTPException(status_code=404, detail=f"audit entry {entry_id} not found")
        return _audit_row_out(row, verifier=deps_.audit_logger)

    # ------------------------------------------------------------------
    # Vocabulary
    # ------------------------------------------------------------------

    @router.get("/vocabulary/{family}", response_model=list[VocabularyEntryOut])
    async def list_vocabulary(
        family: str,
        include_deprecated: bool = Query(default=False),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[VocabularyEntryOut]:
        async with deps_.descriptor_registry.pg.acquire() as conn:
            sql = (
                "SELECT id, family, value, schema_uri, introduced, deprecated, "
                "notes, aliases, parent FROM vocabulary_entries "
                "WHERE family = $1"
            )
            if not include_deprecated:
                sql += " AND deprecated IS NULL"
            sql += " ORDER BY value"
            rows = await conn.fetch(sql, family)
        return [_vocab_row_out(r) for r in rows]

    @router.post(
        "/vocabulary/{family}",
        response_model=VocabularyEntryOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_vocabulary(
        family: str,
        body: VocabularyEntryIn,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> VocabularyEntryOut:
        # Shape validation: piggy-back on the pydantic VocabularyEntry validator.
        from ..schemas.vocabulary import VocabularyEntry

        try:
            VocabularyEntry(
                family=family,  # type: ignore[arg-type]
                value=body.value,
                schema_uri="legba/vocabulary/1.0.0",
                introduced=datetime.now(tz=timezone.utc),
                aliases=body.aliases,
                parent=body.parent,
                notes=body.notes,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async with deps_.descriptor_registry.pg.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO vocabulary_entries
                        (family, value, schema_uri, notes, aliases, parent)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, family, value, schema_uri, introduced,
                              deprecated, notes, aliases, parent
                    """,
                    family,
                    body.value,
                    "iglu:legba/vocabulary/jsonschema/1-0-0",
                    body.notes,
                    body.aliases,
                    body.parent,
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Publish vocabulary.updated.<family> so subscribed caches refresh.
        if deps_.nats_store is not None:
            try:
                await deps_.nats_store.nc.publish(
                    f"vocabulary.updated.{family}", b"{}",
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("vocabulary nats publish failed: %s", exc)
        # Refresh the in-process cache too.
        try:
            await deps_.vocabulary_cache.refresh()
        except Exception as exc:  # pragma: no cover
            logger.warning("vocabulary cache refresh failed: %s", exc)
        return _vocab_row_out(row)

    @router.put(
        "/vocabulary/{family}/{entry_id}", response_model=VocabularyEntryOut,
    )
    async def update_vocabulary(
        family: str,
        entry_id: str,
        body: VocabularyEntryUpdate,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> VocabularyEntryOut:
        try:
            uid = UUID(entry_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with deps_.descriptor_registry.pg.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, family, value, schema_uri, introduced, deprecated, "
                "notes, aliases, parent FROM vocabulary_entries "
                "WHERE id = $1 AND family = $2",
                uid, family,
            )
            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"vocabulary entry {entry_id} (family={family}) not found",
                )
            notes = body.notes if body.notes is not None else existing["notes"]
            aliases = (
                body.aliases if body.aliases is not None
                else list(existing["aliases"] or [])
            )
            parent = body.parent if body.parent is not None else existing["parent"]
            row = await conn.fetchrow(
                """
                UPDATE vocabulary_entries
                SET notes = $2, aliases = $3, parent = $4
                WHERE id = $1
                RETURNING id, family, value, schema_uri, introduced,
                          deprecated, notes, aliases, parent
                """,
                uid, notes, aliases, parent,
            )
        if deps_.nats_store is not None:
            try:
                await deps_.nats_store.nc.publish(
                    f"vocabulary.updated.{family}", b"{}",
                )
            except Exception:  # pragma: no cover
                pass
        try:
            await deps_.vocabulary_cache.refresh()
        except Exception:  # pragma: no cover
            pass
        return _vocab_row_out(row)

    @router.delete("/vocabulary/{family}/{entry_id}")
    async def retire_vocabulary(
        family: str,
        entry_id: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        try:
            uid = UUID(entry_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with deps_.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE vocabulary_entries "
                "SET deprecated = NOW() "
                "WHERE id = $1 AND family = $2 AND deprecated IS NULL "
                "RETURNING id",
                uid, family,
            )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"vocabulary entry {entry_id} (family={family}) not found or already deprecated",
            )
        if deps_.nats_store is not None:
            try:
                await deps_.nats_store.nc.publish(
                    f"vocabulary.updated.{family}", b"{}",
                )
            except Exception:  # pragma: no cover
                pass
        try:
            await deps_.vocabulary_cache.refresh()
        except Exception:  # pragma: no cover
            pass
        return {"entry_id": entry_id, "family": family, "deprecated": True}

    # ------------------------------------------------------------------
    # L-204 — UI panel registry read endpoints
    # ------------------------------------------------------------------

    @router.get(
        "/ui_panels", response_model=list[UIPanelRegistrationOut],
    )
    async def list_ui_panels(
        mode: str = Query(
            ...,
            description="Deployment mode — 'personal' | 'above_ai' | 'cis'. "
                        "Aliases ('cis_fellowship', 'above-ai') are normalized "
                        "by the registry.",
        ),
        include_retired: bool = Query(
            default=False,
            description="Include soft-deleted rows so layout-restore can "
                        "render UnboundPanelPlaceholders for retired bindings.",
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[UIPanelRegistrationOut]:
        """L-204 daily-driver UI fetches this at boot to populate the
        reactive panel registry. The WebSocket `/events` endpoint with
        filter `registry.bindings.>` provides the live-update channel.
        """
        # Import lazily so the kind module isn't loaded at API-import time
        # in environments where the table isn't migrated yet.
        from ..outputs.ui_panel import UIPanelRegistry, UIPanelDescriptorError

        async with deps_.descriptor_registry.pg.acquire() as conn:
            registry = UIPanelRegistry(conn)
            try:
                rows = await registry.list_by_mode(
                    mode, include_retired=include_retired,
                )
            except UIPanelDescriptorError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [_ui_panel_row_out(r) for r in rows]

    @router.get(
        "/ui_panels/by_slot/{layout_slot}",
        response_model=list[UIPanelRegistrationOut],
    )
    async def get_ui_panels_by_slot(
        layout_slot: str,
        include_retired: bool = Query(default=False),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[UIPanelRegistrationOut]:
        """Preset-resolution path. The L-204 layout-preset expansion
        calls this when deep-linking a `(mode, layout_slot)` URL.
        """
        from ..outputs.ui_panel import UIPanelRegistry

        async with deps_.descriptor_registry.pg.acquire() as conn:
            registry = UIPanelRegistry(conn)
            rows = await registry.list_by_layout_slot(
                layout_slot, include_retired=include_retired,
            )
        return [_ui_panel_row_out(r) for r in rows]

    # ------------------------------------------------------------------
    # WebSocket — multiplexed registry events
    # ------------------------------------------------------------------

    @router.websocket("/events")
    async def events_ws(
        websocket: WebSocket,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        filter: str = Query(
            default=">",
            description="NATS subject pattern. Default = all subjects (`>`). "
                        "Examples: `descriptor.registered.target.*`, "
                        "`stack.component.>`, `legba.dlq.>`.",
        ),
    ) -> None:
        try:
            principal = _authorize_ws_token(token, authorization)
        except HTTPException as exc:
            # 503 (gate misconfigured, B-2) maps to 1011 internal-error;
            # auth rejections map to 1008 policy-violation. Close-frame
            # reasons are capped at 123 bytes by RFC 6455 — truncate.
            close_code = (
                status.WS_1011_INTERNAL_ERROR
                if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
                else status.WS_1008_POLICY_VIOLATION
            )
            await websocket.close(
                code=close_code, reason=str(exc.detail)[:120],
            )
            return
        deps_ = getattr(websocket.app.state, "registry_deps", None)
        if deps_ is None or deps_.nats_store is None:
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="NATS store not configured",
            )
            return
        await websocket.accept()
        logger.info(
            "ws connected principal=%s filter=%s", principal, filter,
        )

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

        async def _cb(msg: Any) -> None:
            try:
                queue.put_nowait((msg.subject, msg.data))
            except asyncio.QueueFull:  # pragma: no cover
                pass

        # The NATS connection in `deps_.nats_store` is bound to the loop
        # that constructed it. Browsers + production deployments share one
        # loop end-to-end so reusing the connection is fine. The Starlette
        # TestClient runs each WS handler on its own thread-local loop,
        # so we open a per-connection NATS client here using the same
        # config when the cached one is bound to a different loop.
        nats_for_ws = deps_.nats_store
        local_nats_owner = False
        try:
            existing_loop = getattr(deps_.nats_store.nc, "_loop", None)
            if existing_loop is not None and existing_loop is not loop:
                fresh = NatsStore(deps_.nats_store.cfg)
                await fresh.connect()
                nats_for_ws = fresh
                local_nats_owner = True
        except Exception as exc:  # pragma: no cover
            logger.debug("ws-local nats fallback skipped: %s", exc)

        sub = await nats_for_ws.nc.subscribe(filter, cb=_cb)
        heartbeat_task: asyncio.Task | None = None
        try:
            async def _heartbeat() -> None:
                while True:
                    await asyncio.sleep(30)
                    try:
                        await websocket.send_json(
                            {"type": "heartbeat", "ts": _now_iso()},
                        )
                    except Exception:
                        return

            heartbeat_task = loop.create_task(_heartbeat())

            # Send a hello frame so the client knows we're live.
            await websocket.send_json(
                {
                    "type": "subscribed",
                    "filter": filter,
                    "ts": _now_iso(),
                }
            )

            while True:
                # Race the inbound NATS queue against client close. We use a
                # small timeout on the queue so we can poll the websocket
                # state for `client_state == DISCONNECTED` (value=2 per
                # starlette.websockets.WebSocketState).
                try:
                    subject, data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    cstate = getattr(websocket, "client_state", None)
                    if cstate is not None and cstate.value == 2:
                        break
                    continue
                try:
                    payload = json.loads(data.decode())
                except Exception:
                    payload = {"raw_b64": data.hex()}
                try:
                    await websocket.send_json(
                        {
                            "type": "event",
                            "subject": subject,
                            "payload": payload,
                            "ts": _now_iso(),
                        }
                    )
                except WebSocketDisconnect:
                    break
                except Exception as exc:  # pragma: no cover
                    logger.warning("ws send failed: %s", exc)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except Exception:
                    pass
            try:
                await sub.unsubscribe()
            except Exception as exc:  # pragma: no cover
                logger.debug("ws nats unsub failed: %s", exc)
            if local_nats_owner:
                try:
                    await nats_for_ws.close()
                except Exception:  # pragma: no cover
                    pass
            logger.info("ws closed principal=%s", principal)

    # ---- Governor decisions (operator telemetry) -----------------------
    @router.get("/governor_events")
    async def list_governor_events(  # noqa: ANN202 — bare list response
        pack_id: str | None = Query(default=None),
        decision: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
        principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[dict[str, Any]]:
        """Historical governor decisions — the initial load for the
        GovernorEvents operator panel (live updates arrive over the
        ``governor.events.>`` WS). Newest first; optional pack_id / decision
        filters. Returns a bare array, matching the UI's expected shape.
        """
        where: list[str] = []
        args: list[Any] = []
        if pack_id:
            args.append(pack_id)
            where.append(f"pack_id = ${len(args)}")
        if decision:
            args.append(decision)
            where.append(f"decision = ${len(args)}")
        args.append(limit)
        sql = (
            "SELECT pack_id, decision, cause, tool_name, budget_account, "
            "requested_by, tenant_id, cap_dimension, cap_limit, "
            "observed_value, detail, occurred_at FROM governor_events "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + f"ORDER BY occurred_at DESC LIMIT ${len(args)}"
        )
        async with deps_.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        def _num(v: Any) -> float | None:
            return float(v) if v is not None else None

        return [
            {
                "pack_id": r["pack_id"],
                "decision": r["decision"],
                "cause": r["cause"],
                "tool_name": r["tool_name"],
                "budget_account": r["budget_account"],
                "requested_by": r["requested_by"],
                "tenant_id": r["tenant_id"],
                "cap_dimension": r["cap_dimension"],
                "cap_limit": _num(r["cap_limit"]),
                "observed_value": _num(r["observed_value"]),
                "detail": r["detail"],
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            }
            for r in rows
        ]

    # ---- Backfill catch-up (P-12) — honest 501, not a silent 404 -------
    @router.post("/targets/{target_id}/backfill", status_code=501)
    async def trigger_backfill(
        target_id: str,
        principal: str = Depends(require_bearer),
    ) -> None:
        """P-12 catch-up replay (``Backfiller.backfill``) is a RUNTIME
        operation — it needs the live ``SubscriptionEngine`` + a forward sink,
        which the registry service does not host. The backend exists
        (``runtime/subscription/backfill.py``) but a REST trigger is not wired
        through the registry API in this build. Return 501 (honest: the
        capability exists but isn't exposed here) rather than 404 (which would
        imply it doesn't exist). Tracked: wire it on the runtime API.
        """
        raise HTTPException(
            status_code=501,
            detail=(
                "Backfill catch-up runs on the runtime actor host and is not "
                "exposed via the registry REST API in this build. The backend "
                "(Backfiller) exists; a runtime-side trigger is a tracked "
                "follow-up."
            ),
        )

    return router


# ---------------------------------------------------------------------------
# Conversion helpers — proxy to the typed `ConversionWebhookRegistry` when it
# is wired, otherwise raw SQL against the `conversion_webhooks` table.
# ---------------------------------------------------------------------------


async def _conv_register(
    deps_: RegistryAPIDeps, body: ConversionWebhookIn,
    actor: str = "anonymous",
) -> ConversionWebhookOut:
    if deps_.conversion_registry is not None and _ConversionWebhookSchema is not None:
        try:
            webhook = _ConversionWebhookSchema(
                from_uri=body.from_uri,
                to_uri=body.to_uri,
                impl=body.impl,
                direction=body.direction,
            )
            row = await deps_.conversion_registry.register_webhook(
                webhook, actor=actor, notes=body.notes,
            )
            return _conv_row_out(row)
        except _WebhookValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Fallback (typed registry not wired): write directly to the
    # conversion_webhooks table. The typed `ConversionWebhookRegistry` branch
    # above is the canonical path and adds validation + event-publish +
    # audit-log; this raw-SQL path is the degradation when it is absent.
    async with deps_.descriptor_registry.pg.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO conversion_webhooks
                    (from_uri, to_uri, impl, direction, notes)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, from_uri, to_uri, impl, direction, notes, created_at
                """,
                body.from_uri, body.to_uri, body.impl, body.direction, body.notes,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _conv_row_out(row)


async def _conv_find(
    deps_: RegistryAPIDeps,
    *,
    family: str | None,
    from_uri: str | None,
    to_uri: str | None,
) -> list[ConversionWebhookOut]:
    if deps_.conversion_registry is not None:
        try:
            # `list_webhooks` takes the same filters; the brief asks for
            # the rows themselves (the UI does the path render). For an
            # explicit path lookup, callers pass both from_uri AND to_uri
            # AND family; we still return the list view so they get all
            # candidate rows.
            rows = await deps_.conversion_registry.list_webhooks(
                family=family, from_uri=from_uri, to_uri=to_uri,
            )
            return [_conv_row_out(r) for r in rows]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with deps_.descriptor_registry.pg.acquire() as conn:
        params: list[Any] = []
        sql = (
            "SELECT id, from_uri, to_uri, impl, direction, notes, created_at "
            "FROM conversion_webhooks WHERE TRUE"
        )
        if from_uri is not None:
            params.append(from_uri)
            sql += f" AND from_uri = ${len(params)}"
        if to_uri is not None:
            params.append(to_uri)
            sql += f" AND to_uri = ${len(params)}"
        if family is not None:
            # family filter = "<family>/" segment of the from_uri.
            params.append(f"legba/{family}/%")
            sql += f" AND from_uri LIKE ${len(params)}"
        sql += " ORDER BY created_at DESC"
        rows = await conn.fetch(sql, *params)
    return [_conv_row_out(r) for r in rows]


async def _conv_delete(
    deps_: RegistryAPIDeps, webhook_id: str, actor: str = "anonymous",
) -> dict[str, Any]:
    if deps_.conversion_registry is not None:
        try:
            await deps_.conversion_registry.retire_webhook(webhook_id, actor=actor)
            return {"webhook_id": webhook_id, "retired": True}
        except _WebhookNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        uid = UUID(webhook_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with deps_.descriptor_registry.pg.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM conversion_webhooks WHERE id = $1", uid,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"webhook {webhook_id} not found")
    return {"webhook_id": webhook_id, "retired": True}


def _conv_row_out(row) -> ConversionWebhookOut:
    """Coerce either a typed `WebhookRow` dataclass or a raw asyncpg
    `Record` into the JSON output schema."""
    # asyncpg.Record supports __getitem__; WebhookRow has named attrs.
    if hasattr(row, "from_uri") and not hasattr(row, "keys"):
        return ConversionWebhookOut(
            id=str(row.id),
            from_uri=row.from_uri,
            to_uri=row.to_uri,
            impl=row.impl,
            direction=row.direction,
            notes=getattr(row, "notes", None),
            created_at=row.created_at,
        )
    return ConversionWebhookOut(
        id=str(row["id"]),
        from_uri=row["from_uri"],
        to_uri=row["to_uri"],
        impl=row["impl"],
        direction=row["direction"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Row → out shapers
# ---------------------------------------------------------------------------


def _dlq_row_out(row) -> DLQEntryOut:
    ve = row["validation_error"]
    if isinstance(ve, str):
        try:
            ve = json.loads(ve)
        except Exception:
            ve = {"raw": ve}
    payload = row["attempted_payload"] if "attempted_payload" in row.keys() else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {"raw": payload}
    return DLQEntryOut(
        id=str(row["id"]),
        attempted_at=row["attempted_at"],
        actor=row["actor"],
        namespace=row["namespace"],
        declared_schema_uri=row["declared_schema_uri"],
        validation_error=ve or {},
        resolution=row["resolution"],
        attempted_payload=payload if isinstance(payload, dict) else None,
    )


def _audit_row_out(row, *, verifier: AuditLogger | None) -> AuditEntryOut:
    summary = row["change_summary"]
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except Exception:
            summary = {}
    signed = row["signed_payload"]
    verified: bool | None = None
    if verifier is not None and signed is not None:
        try:
            from .events import audit_payload as _ap
            payload = _ap(
                action=row["action"],
                family=row["namespace"],
                descriptor_id=row["descriptor_id"],
                actor_id=row["actor_id"],
                actor_role=row["actor_role"],
                from_version=row["from_version"],
                to_version=row["to_version"],
                change_summary=summary,
                occurred_at=row["occurred_at"]
                    .astimezone(timezone.utc).isoformat(),
            )
            try:
                verified = verify_audit_payload(
                    verifier.identity.verify_key, payload, bytes(signed),
                )
            except AuditChainError:
                verified = False
        except Exception:  # pragma: no cover
            verified = None
    return AuditEntryOut(
        id=str(row["id"]),
        occurred_at=row["occurred_at"],
        actor_id=row["actor_id"],
        actor_role=row["actor_role"],
        namespace=row["namespace"],
        descriptor_id=row["descriptor_id"],
        action=row["action"],
        from_version=row["from_version"],
        to_version=row["to_version"],
        change_summary=summary or {},
        signer_did=row["signer_did"],
        signature_verified=verified,
    )


def _ui_panel_row_out(reg) -> "UIPanelRegistrationOut":
    """Coerce a `PanelRegistration` dataclass into the JSON output schema."""
    return UIPanelRegistrationOut(
        id=str(reg.id),
        panel_id=reg.panel_id,
        descriptor_id=reg.descriptor_id,
        descriptor_version=reg.descriptor_version,
        descriptor_family=reg.descriptor_family,
        analyst_id=reg.analyst_id,
        title=reg.title,
        mode=reg.mode,
        layout_slot=reg.layout_slot,
        data_query=dict(reg.data_query or {}),
        binding=dict(reg.binding or {}),
        retired=reg.retired,
        created_at=reg.created_at,
        retired_at=reg.retired_at,
    )


def _vocab_row_out(row) -> VocabularyEntryOut:
    return VocabularyEntryOut(
        id=str(row["id"]),
        family=row["family"],
        value=row["value"],
        schema_uri=row["schema_uri"],
        introduced=row["introduced"],
        deprecated=row["deprecated"],
        notes=row["notes"],
        aliases=list(row["aliases"] or []),
        parent=row["parent"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _shallow_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict = base with patch shallowly merged in.

    Nested dicts get a one-level recursive merge; non-dict values are
    replaced wholesale. Sufficient for DLQ-resubmit fixup; full JSON-patch
    semantics live with the UI side-by-side editor (L-204).
    """
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _shallow_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Mode-gate notice at import time so operators see the auth posture (B-2:
# the unset-token default is FAIL-CLOSED, not dev mode).
# ---------------------------------------------------------------------------


if _current_token() is None:
    if _dev_mode():
        logger.warning(
            "%s is not set and %s=1; registry API runs in DEVELOPMENT MODE "
            "(any bearer token, or none, will be accepted). "
            "Never set %s=1 in production.",
            API_TOKEN_ENV, DEV_MODE_ENV, DEV_MODE_ENV,
        )
    else:
        logger.error(
            "%s is not set and %s=1 is not set; registry API is FAIL-CLOSED "
            "— every guarded request will get HTTP 503 until a token is "
            "configured (or %s=1 is set explicitly for local development).",
            API_TOKEN_ENV, DEV_MODE_ENV, DEV_MODE_ENV,
        )

if not _HAS_L112:
    logger.info(
        "typed conversion-webhook registry module not importable; falling back "
        "to the raw-SQL conversion_webhooks path at "
        "/api/v1/registry/conversions"
    )
