# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A2A skill output kind — L-193.

This module replaces the spike's placeholder ``/a2a/skills/<id>`` JSON
endpoint with a real Ed25519-signed envelope protocol surface. Where the
spike returned recent findings as plain JSON, this kind:

  * Verifies an Ed25519-signed *request* envelope from the caller
    (e.g. Mnemosyne) before dispatching the skill.
  * Looks up the descriptor backing the requested ``skill_id`` and
    validates ``args`` against the descriptor's input JSON-Schema.
  * Returns an Ed25519-signed *response* envelope whose ``payload``
    carries the analyst's most-recent output for the requested scope.

The kind itself is a thin coordinator: it does not own the analyst's
runtime, the descriptor registry, or the Postgres pool. The runtime
process passes those in when wiring the route (see
:func:`register_a2a_skill_route`).

Envelope wire format (canonical, sortable, deterministic):

    {
      "envelope": {
        "envelope_version": "1",
        "skill_id": "<dotted.skill.name>",
        "nonce": "<uuid hex>",
        "issued_at": "<RFC3339 UTC>",
        "signer_did": "did:legba:registry:<host>",
        "payload": { ... skill args (request) or skill output (response) }
      },
      "signature": "<base64url of Ed25519 sig over canonical_json(envelope)>"
    }

The signature covers ``canonical_json(envelope)`` (the inner object) —
not the wrapper. ``signer_did`` is the DID-style identifier per
``data/registry/signing.py``; the registry resolves it to a verify-key
via :class:`TrustedKeyDirectory` (in-memory map populated by the
runtime, externalisable later).
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey
from pydantic import BaseModel, ConfigDict, Field

from ..provenance import canonical_json
from ..registry.signing import SigningIdentity

logger = logging.getLogger(__name__)


KIND_NAME = "a2a_skill"
ENVELOPE_VERSION = "1"
DEFAULT_RECENT_LIMIT = 5

# Wire / header constants.
HEADER_SIGNATURE = "X-Legba-A2A-Signature"
HEADER_SIGNER_DID = "X-Legba-A2A-Signer-DID"
HEADER_NONCE = "X-Legba-A2A-Nonce"
HEADER_ENVELOPE_VERSION = "X-Legba-A2A-Envelope-Version"


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------


@dataclass
class A2ASkillRegistration:
    """One descriptor's exposure as an A2A skill.

    Populated from an :class:`~legba.data.schemas.target.OutputBinding`
    whose ``kind == "a2a_skill"`` on an analyst descriptor. The runtime
    calls :meth:`A2ASkillRegistry.register_from_descriptor` at descriptor
    activate time and :meth:`A2ASkillRegistry.unregister_by_analyst` at
    retire.
    """

    skill_id: str
    analyst_id: str
    analyst_version: str
    input_schema: dict[str, Any]
    response_schema: dict[str, Any]
    auth_required: bool = True
    description: str = ""
    # Populated by the runtime when the registration is created; consumed by
    # the inbound dispatcher to find the analyst's latest output. The runtime
    # may overwrite to update.
    descriptor_id: str = ""


# ---------------------------------------------------------------------------
# Trusted-DID directory
# ---------------------------------------------------------------------------


@dataclass
class TrustedKeyDirectory:
    """In-memory directory of DID -> Ed25519 verify-key.

    The runtime populates this from operator-managed config (env var
    ``LEGBA_A2A_TRUSTED_KEYS`` — comma-separated ``did=hex`` pairs — at
    bring-up, plus runtime adds at descriptor-import time). The directory
    is intentionally tiny; cross-instance federation will replace it with
    a real DID resolver in Phase 10.
    """

    keys: dict[str, VerifyKey] = field(default_factory=dict)

    def add(self, did: str, verify_key: VerifyKey | bytes) -> None:
        if isinstance(verify_key, (bytes, bytearray)):
            verify_key = VerifyKey(bytes(verify_key))
        self.keys[did] = verify_key

    def get(self, did: str) -> VerifyKey | None:
        return self.keys.get(did)

    @classmethod
    def from_env(cls, var: str = "LEGBA_A2A_TRUSTED_KEYS") -> "TrustedKeyDirectory":
        d = cls()
        raw = os.getenv(var, "")
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            did, hex_key = entry.split("=", 1)
            try:
                d.add(did.strip(), bytes.fromhex(hex_key.strip()))
            except Exception as exc:  # pragma: no cover — malformed env
                logger.warning("a2a.trusted_keys.malformed did=%s err=%s", did, exc)
        return d


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_envelope(
    *,
    skill_id: str,
    payload: Mapping[str, Any],
    signer_did: str,
    nonce: str | None = None,
    issued_at: str | None = None,
) -> dict[str, Any]:
    """Construct the inner envelope dict (pre-signature)."""
    return {
        "envelope_version": ENVELOPE_VERSION,
        "skill_id": skill_id,
        "nonce": nonce or uuid4().hex,
        "issued_at": issued_at or _utcnow_iso(),
        "signer_did": signer_did,
        "payload": dict(payload),
    }


def sign_envelope(envelope: Mapping[str, Any], signing_key: SigningKey) -> str:
    """Sign an envelope dict, returning the base64url-encoded signature."""
    body = canonical_json(envelope)
    sig = signing_key.sign(body).signature
    return _b64url(sig)


def verify_envelope(
    envelope: Mapping[str, Any],
    signature_b64url: str,
    verify_key: VerifyKey,
) -> bool:
    """Verify the signature over canonical-JSON of ``envelope``.

    Raises :class:`ValueError` on signature mismatch so callers can map
    to an HTTP 401. Returns ``True`` on success.
    """
    try:
        sig = _b64url_decode(signature_b64url)
    except Exception as exc:
        raise ValueError(f"signature not valid base64url: {exc}") from exc
    try:
        verify_key.verify(canonical_json(envelope), sig)
    except BadSignatureError as exc:
        raise ValueError(f"bad envelope signature: {exc}") from exc
    return True


def envelope_response(
    *,
    skill_id: str,
    payload: Mapping[str, Any],
    identity: SigningIdentity,
) -> dict[str, Any]:
    """Build a wire-shaped ``{envelope, signature}`` dict signed by
    ``identity``. Returned object is JSON-ready and matches the response
    body shape produced by :func:`register_a2a_skill_route`.
    """
    env = build_envelope(
        skill_id=skill_id,
        payload=payload,
        signer_did=identity.signer_did,
    )
    sig = sign_envelope(env, identity.signing_key)
    return {"envelope": env, "signature": sig}


# ---------------------------------------------------------------------------
# JSON-schema validation (intentionally tiny — pyproject has no jsonschema)
# ---------------------------------------------------------------------------


def _validate_args(args: Any, schema: Mapping[str, Any]) -> None:
    """Minimal JSON-Schema validator covering ``type``/``required``/
    ``properties.type``.

    The full draft-07 surface is overkill for the registration shape we
    accept here — the descriptor's ``input_schema`` is operator-authored
    against a strict allowlist. If a descriptor needs richer validation
    the runtime can swap in a real validator (e.g. ``jsonschema``) without
    changing this kind's API.
    """
    if schema is None:
        return
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(args, dict):
            raise ValueError(f"expected object, got {type(args).__name__}")
        required = schema.get("required", [])
        for k in required:
            if k not in args:
                raise ValueError(f"missing required field: {k!r}")
        props = schema.get("properties", {}) or {}
        for k, v in args.items():
            sub = props.get(k)
            if not sub:
                continue
            sub_type = sub.get("type")
            if sub_type == "string" and not isinstance(v, str):
                raise ValueError(f"field {k!r}: expected string")
            elif sub_type == "integer" and not isinstance(v, int):
                raise ValueError(f"field {k!r}: expected integer")
            elif sub_type == "number" and not isinstance(v, (int, float)):
                raise ValueError(f"field {k!r}: expected number")
            elif sub_type == "boolean" and not isinstance(v, bool):
                raise ValueError(f"field {k!r}: expected boolean")
            elif sub_type == "array" and not isinstance(v, list):
                raise ValueError(f"field {k!r}: expected array")
            elif sub_type == "object" and not isinstance(v, dict):
                raise ValueError(f"field {k!r}: expected object")
    elif expected == "array":
        if not isinstance(args, list):
            raise ValueError(f"expected array, got {type(args).__name__}")
    # Unrecognised top-level types are accepted to keep the validator
    # additive — the descriptor authors own the schema shape.


# ---------------------------------------------------------------------------
# Latest-output fetcher port
# ---------------------------------------------------------------------------


LatestOutputFetcher = Callable[..., Awaitable[list[dict[str, Any]]]]
"""Callable signature ``async (analyst_ids: list[str], limit: int,
target_filter: str | None) -> list[dict]``.

The runtime injects this; tests pass a deterministic fake. Returning
``list[dict]`` keeps the kind decoupled from row models.
"""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class A2ASkillRegistry(BaseModel):
    """Process-wide registry mapping ``skill_id -> A2ASkillRegistration``.

    Thread-affinity: not safe for concurrent mutation; mutation happens
    on the runtime's reconcile thread. Lookup is safe under read-only
    concurrency.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    skills: dict[str, A2ASkillRegistration] = Field(default_factory=dict, exclude=True)

    # Reverse index: analyst_id -> {skill_id, ...} so retire is O(1).
    _by_analyst: dict[str, set[str]] = {}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._by_analyst = {}

    # -- mutation -------------------------------------------------------

    def register(self, reg: A2ASkillRegistration) -> None:
        """Add or replace a registration. Idempotent on ``skill_id``."""
        existing = self.skills.get(reg.skill_id)
        if existing is not None and existing.analyst_id != reg.analyst_id:
            raise ValueError(
                f"skill_id {reg.skill_id!r} already registered by analyst "
                f"{existing.analyst_id!r}, cannot reassign to {reg.analyst_id!r}"
            )
        self.skills[reg.skill_id] = reg
        self._by_analyst.setdefault(reg.analyst_id, set()).add(reg.skill_id)
        logger.info(
            "a2a.skill.registered skill_id=%s analyst_id=%s version=%s",
            reg.skill_id, reg.analyst_id, reg.analyst_version,
        )

    def unregister(self, skill_id: str) -> None:
        reg = self.skills.pop(skill_id, None)
        if reg is None:
            return
        s = self._by_analyst.get(reg.analyst_id)
        if s is not None:
            s.discard(skill_id)
            if not s:
                del self._by_analyst[reg.analyst_id]
        logger.info("a2a.skill.unregistered skill_id=%s", skill_id)

    def unregister_by_analyst(self, analyst_id: str) -> int:
        """Remove every skill registered by ``analyst_id``. Returns the
        count removed."""
        skill_ids = list(self._by_analyst.get(analyst_id, ()))
        for sid in skill_ids:
            self.unregister(sid)
        return len(skill_ids)

    def has_analyst_version(self, analyst_id: str, version: str) -> bool:
        """True iff ``analyst_id`` has skills registered AND they all carry
        ``version``.

        Lets the reconcile executor re-register a2a skills on ENSURE_ACTIVE /
        resume (so a restart — which re-asserts active analysts via
        ENSURE_ACTIVE, not CREATE — re-populates the in-memory registry) while
        skipping the redundant descriptor re-fetch + replace on every steady
        resync. Returns False when nothing is registered (restart) or the stored
        version differs (descriptor edit), both of which must re-register.
        """
        sids = self._by_analyst.get(analyst_id)
        if not sids:
            return False
        return all(
            self.skills.get(sid) is not None
            and self.skills[sid].analyst_version == version
            for sid in sids
        )

    def register_from_descriptor(
        self,
        *,
        analyst_id: str,
        analyst_version: str,
        descriptor_id: str,
        outputs: list[Mapping[str, Any]],
        type_signature: Mapping[str, Any] | None = None,
    ) -> list[A2ASkillRegistration]:
        """Scan ``outputs`` for ``kind == 'a2a_skill'`` bindings and
        register one :class:`A2ASkillRegistration` per match.

        ``outputs`` is the analyst descriptor's ``outputs`` list — list of
        ``OutputBinding``-shaped dicts. ``type_signature`` is the analyst's
        ``identity.type_signature`` dict (``{input_type, output_type, ...}``)
        and is used to populate ``input_schema`` / ``response_schema`` when
        the descriptor's a2a_skill config omits them — the kind's auto-
        registration path falls back to ``{"type": "object"}`` for the
        input and the analyst's emitted-finding shape for the response.
        """
        out: list[A2ASkillRegistration] = []
        for binding in outputs or []:
            if binding.get("kind") != KIND_NAME:
                continue
            cfg = binding.get("config") or {}
            skill_id = cfg.get("skill_id") or binding.get("skill_id")
            if not skill_id:
                logger.warning(
                    "a2a.skill.skip analyst_id=%s reason=no_skill_id binding=%s",
                    analyst_id, binding,
                )
                continue

            input_schema = (
                cfg.get("input_schema")
                or binding.get("input_schema")
                or _default_input_schema(type_signature)
            )
            response_schema = (
                cfg.get("response_schema")
                or cfg.get("output_schema")
                or binding.get("response_schema")
                or _default_response_schema(type_signature)
            )

            reg = A2ASkillRegistration(
                skill_id=skill_id,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                descriptor_id=descriptor_id,
                input_schema=dict(input_schema),
                response_schema=dict(response_schema),
                auth_required=bool(cfg.get("auth_required", True)),
                description=str(cfg.get("description", "") or ""),
            )
            self.register(reg)
            out.append(reg)
        return out

    # -- lookup ---------------------------------------------------------

    def get(self, skill_id: str) -> A2ASkillRegistration | None:
        return self.skills.get(skill_id)

    def list_skills(self) -> list[A2ASkillRegistration]:
        return list(self.skills.values())


def _default_input_schema(type_signature: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fallback input schema — accepts an object with a ``target_id``
    scope filter and an optional ``limit``.

    Used when the descriptor's a2a_skill config omits ``input_schema``.
    """
    return {
        "type": "object",
        "properties": {
            "target_id": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": [],
        "description": (
            f"Auto-derived input schema for analyst input_type="
            f"{(type_signature or {}).get('input_type', 'unknown')!r}"
        ),
    }


def _default_response_schema(type_signature: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fallback response schema — the analyst's recent findings list."""
    return {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string"},
            "findings": {"type": "array"},
        },
        "required": ["skill_id", "findings"],
        "description": (
            f"Auto-derived response schema for analyst output_type="
            f"{(type_signature or {}).get('output_type', 'unknown')!r}"
        ),
    }


# ---------------------------------------------------------------------------
# FastAPI route registration
# ---------------------------------------------------------------------------


def register_a2a_skill_route(
    app: Any,
    *,
    registry: A2ASkillRegistry,
    identity: SigningIdentity,
    fetch_latest_outputs: LatestOutputFetcher,
    trusted_keys: TrustedKeyDirectory | None = None,
    prefix: str = "/a2a/skills",
) -> APIRouter:
    """Mount the A2A skill protocol surface onto ``app``.

    Returns the constructed :class:`APIRouter` so callers can introspect /
    further-mount the routes. The router exposes:

      * ``GET  {prefix}``                — list registered skills.
      * ``GET  {prefix}/{skill_id}``     — skill metadata + recent output
                                            (legacy spike compatibility; the
                                            response is signed-envelope,
                                            not raw JSON).
      * ``POST {prefix}/{skill_id}``     — A2A invocation. Body:
                                            ``{envelope, signature}``;
                                            response: signed envelope.

    The runtime passes ``identity`` (the registry-process signing key) and
    ``fetch_latest_outputs`` (a callable that resolves the analyst's most
    recent findings from Postgres). ``trusted_keys`` is the caller-DID
    directory; when ``None`` we fall back to env-derived defaults.
    """
    trusted = trusted_keys or TrustedKeyDirectory.from_env()
    router = APIRouter()

    @router.get(prefix)
    async def list_skills() -> dict[str, Any]:
        return {
            "skills": [
                {
                    "skill_id": r.skill_id,
                    "analyst_id": r.analyst_id,
                    "analyst_version": r.analyst_version,
                    "auth_required": r.auth_required,
                    "input_schema": r.input_schema,
                    "response_schema": r.response_schema,
                    "description": r.description,
                }
                for r in registry.list_skills()
            ],
            "signer_did": identity.signer_did,
        }

    @router.get(prefix + "/{skill_id}")
    async def get_skill(skill_id: str, limit: int = DEFAULT_RECENT_LIMIT) -> Any:
        """Compatibility GET — returns a signed envelope containing the
        latest outputs for the skill's backing analyst.

        Unlike the spike's placeholder this *is* signed (response only —
        GET requests are intentionally not envelope-required so a browser
        or curl can sanity-check). Auth-required skills 401 here too if
        the request omits a verified DID header.
        """
        reg = registry.get(skill_id)
        if reg is None:
            raise HTTPException(
                status_code=404,
                detail=f"no analyst exposes A2A skill {skill_id!r}",
            )
        rows = await fetch_latest_outputs(
            analyst_ids=[reg.analyst_id], limit=limit, target_filter=None,
        )
        payload = {"skill_id": skill_id, "findings": rows, "limit": limit}
        body = envelope_response(skill_id=skill_id, payload=payload, identity=identity)
        return JSONResponse(
            content=body,
            headers={
                HEADER_SIGNATURE: body["signature"],
                HEADER_SIGNER_DID: identity.signer_did,
                HEADER_ENVELOPE_VERSION: ENVELOPE_VERSION,
                HEADER_NONCE: body["envelope"]["nonce"],
            },
        )

    @router.post(prefix + "/{skill_id}")
    async def invoke_skill(skill_id: str, request: Request) -> Any:
        """Invoke a registered A2A skill via signed envelope.

        Request body shape (canonical wire form):
            {"envelope": {...}, "signature": "<b64url Ed25519 sig>"}

        On success the response is the analyst's most recent output for
        the requested scope, wrapped in a *response* signed envelope.
        """
        reg = registry.get(skill_id)
        if reg is None:
            raise HTTPException(
                status_code=404,
                detail=f"no analyst exposes A2A skill {skill_id!r}",
            )

        try:
            body_bytes = await request.body()
            import json as _json
            wire = _json.loads(body_bytes)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}")

        envelope = wire.get("envelope")
        signature = wire.get("signature")
        if not isinstance(envelope, dict) or not isinstance(signature, str):
            raise HTTPException(
                status_code=400,
                detail="request body must be {envelope: object, signature: string}",
            )

        # Sanity: skill_id in envelope matches URL path.
        if envelope.get("skill_id") != skill_id:
            raise HTTPException(
                status_code=400,
                detail="envelope.skill_id does not match URL path",
            )
        if envelope.get("envelope_version") != ENVELOPE_VERSION:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported envelope_version "
                    f"{envelope.get('envelope_version')!r}; expected "
                    f"{ENVELOPE_VERSION!r}"
                ),
            )

        signer_did = envelope.get("signer_did") or ""
        verify_key = trusted.get(signer_did)
        if reg.auth_required:
            if not signer_did or verify_key is None:
                raise HTTPException(
                    status_code=401,
                    detail=f"unknown or untrusted signer_did {signer_did!r}",
                )
            try:
                verify_envelope(envelope, signature, verify_key)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc))
        elif verify_key is not None:
            # Opportunistic verify when key is known but auth not required.
            try:
                verify_envelope(envelope, signature, verify_key)
            except ValueError as exc:
                logger.info(
                    "a2a.envelope.unverified_optional skill_id=%s err=%s",
                    skill_id, exc,
                )

        # Schema-validate args.
        args = envelope.get("payload") or {}
        try:
            _validate_args(args, reg.input_schema)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"args failed input_schema validation: {exc}",
            )

        # Fetch the analyst's most-recent outputs for the requested scope.
        target_filter = args.get("target_id") if isinstance(args, dict) else None
        limit = int(args.get("limit", DEFAULT_RECENT_LIMIT)) if isinstance(args, dict) else DEFAULT_RECENT_LIMIT
        rows = await fetch_latest_outputs(
            analyst_ids=[reg.analyst_id],
            limit=limit,
            target_filter=target_filter,
        )
        payload = {
            "skill_id": skill_id,
            "analyst_id": reg.analyst_id,
            "analyst_version": reg.analyst_version,
            "findings": rows,
            "in_reply_to_nonce": envelope.get("nonce"),
        }
        response_body = envelope_response(
            skill_id=skill_id, payload=payload, identity=identity,
        )
        return JSONResponse(
            content=response_body,
            headers={
                HEADER_SIGNATURE: response_body["signature"],
                HEADER_SIGNER_DID: identity.signer_did,
                HEADER_ENVELOPE_VERSION: ENVELOPE_VERSION,
                HEADER_NONCE: response_body["envelope"]["nonce"],
            },
        )

    app.include_router(router)
    return router


__all__ = [
    "A2ASkillRegistration",
    "A2ASkillRegistry",
    "DEFAULT_RECENT_LIMIT",
    "ENVELOPE_VERSION",
    "HEADER_ENVELOPE_VERSION",
    "HEADER_NONCE",
    "HEADER_SIGNATURE",
    "HEADER_SIGNER_DID",
    "KIND_NAME",
    "LatestOutputFetcher",
    "TrustedKeyDirectory",
    "build_envelope",
    "envelope_response",
    "register_a2a_skill_route",
    "sign_envelope",
    "verify_envelope",
]
