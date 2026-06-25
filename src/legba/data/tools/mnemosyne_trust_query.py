# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-211 — Mnemosyne trust query tool.

A tool the runtime resolves for analyst kinds that whitelist
``"mnemosyne_trust_query"`` on their descriptor. Wraps an outbound
A2A ``tasks/send`` call to Mnemosyne's ``trust.query`` skill so analysts
can score peers without learning Mnemosyne's internal schema.

Per the 2026-05-20 task-tracker move (Phase 10 → Phase 6) the tool
existed solely to validate that the resolution mechanism is in place
before analyst kinds are built; the wire contract still tracks
Mnemosyne's A2A surface in
``mnemosyne/backend/app/services/a2a/skills.py:TRUST_QUERY``.

Wire-level contract
-------------------

* **Transport:** HTTPS POST to ``{base_url}/a2a``.
* **Body:** JSON-RPC 2.0 envelope::

      {
          "jsonrpc": "2.0",
          "id": "<uuid hex>",
          "method": "tasks/send",
          "params": {
              "skill_id": "trust.query",
              "data": {"subject_did": "<peer_did>", "scope": "<scope>"},
              "_meta": {                # signed-envelope per A2A server.py:887
                  "signature":    "<base64url Ed25519>",
                  "signer_did":   "did:key:z6Mk...",
                  "purpose":      "a2a.trust.query",
                  "nonce":        "<uuid hex>",
                  "timestamp":    <unix int>,
                  "content_hash": "<sha256 hex of canonical data>"
              }
          }
      }

* **Signed message:** ``f"{purpose}:{content_hash}:{nonce}:{timestamp}"``
  (matches ``A2AServer._verify_sender`` in Mnemosyne; 5-minute freshness
  window).
* **Response (happy path):** JSON-RPC ``result`` containing an A2A
  task dict whose first artifact part has ``type=application/json`` and
  a ``data`` field matching ``TRUST_QUERY.output_schema``.

Return shape (per MN-3 Q13 — 2026-05-12 decision)
-------------------------------------------------

The tool deliberately strips Mnemosyne's full payload down to::

    {"weight": float, "hops": int}

This is the "aggregated weight + hop count" surface mandated until the
trust-path signing chain primitive lands (see P-007); raw chain bytes
MUST NOT leak through this tool. The Mnemosyne response is mapped:

* ``weight`` ← ``trust_distance.trust_weight`` (with ``trust_score``
  as fallback when ``trust_distance`` is absent/empty).
* ``hops`` ← ``trust_distance.distance`` (with ``-1`` meaning "not
  reachable" per Mnemosyne's response convention).

When Mnemosyne returns the P-007 Q5 fallback signal (an explicit
``chain_unavailable: true`` flag), the tool returns
``{"error": "chain_unavailable"}`` exactly — analysts treat this as
"no usable trust evidence", which is distinct from a network error.

Errors
------

All non-happy paths surface as a return value, not an exception, so the
analyst's ``run`` phase can degrade gracefully (an analyst should never
crash because Mnemosyne is unreachable). The mapping:

* Mnemosyne signals ``chain_unavailable`` → ``{"error": "chain_unavailable"}``
* HTTP/transport error                    → ``{"error": "transport_error"}``
* JSON-RPC error                          → ``{"error": "rpc_error"}``
* Response signature mismatch             → ``{"error": "signature_mismatch"}``
* Task failed / no artifact               → ``{"error": "task_failed"}``
* Missing peer in registry                → ``{"weight": 0.0, "hops": -1}``
  (Mnemosyne returns this naturally — see ``_execute_trust_query`` which
  emits ``trust_level="unknown"``; we surface the zero-weight shape
  rather than a synthetic error).

The tool is constructed with an explicit ``MnemosyneTrustQueryDeps``
bundle; the runtime resolves the base URL, the Ed25519 signing key
(reusing the same instance key the A2A *server* uses — see
``legba.ui.agent_card.get_instance_signing_key``), and an
``httpx.AsyncClient``. Tests inject all three.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Identifier analysts use in their descriptor's ``tools.whitelist``. The
#: runtime registers this tool under ``deps.tools[TOOL_NAME]``.
TOOL_NAME = "mnemosyne_trust_query"

#: A2A skill we invoke on the Mnemosyne side. Mirrors
#: ``mnemosyne/backend/app/services/a2a/skills.py:TRUST_QUERY.id``.
SKILL_ID = "trust.query"

#: Purpose string included in the signed envelope. Surfaced to Mnemosyne
#: in ``_meta.purpose`` so audit logs distinguish trust-query calls from
#: other A2A traffic.
SIGN_PURPOSE = "a2a.trust.query"

#: Default HTTP timeout (seconds). Trust queries are interactive — an
#: analyst is waiting on them — so we fail fast rather than block.
DEFAULT_TIMEOUT_S = 10.0

#: Allowed scope values per Mnemosyne's TRUST_QUERY schema. Kept here so
#: callers can validate before the network hop; the Mnemosyne server is
#: the canonical authority and may reject additional values.
ALLOWED_SCOPES = frozenset(
    {"general", "data_access", "delegation", "group_membership"}
)


# ---------------------------------------------------------------------------
# Errors + deps
# ---------------------------------------------------------------------------


class MnemosyneTrustQueryError(ValueError):
    """Raised for *programming* errors (bad args, misconfigured deps).

    Network / RPC / signature failures are NOT raised — they surface as
    ``{"error": ...}`` return values per the module docstring contract.
    """


@dataclass
class MnemosyneTrustQueryDeps:
    """Runtime-resolved dependencies for the trust-query tool.

    The runtime constructs this from the analyst descriptor's
    ``Property.Secret`` / ``Property.StackRef`` references at tool
    registration time. None of the fields are looked up here; the tool
    is intentionally a thin async function so the optimizer's replay
    step (L-176) sees deterministic inputs.

    Parameters
    ----------
    base_url:
        Mnemosyne deployment root, e.g. ``"https://mnemosyne.example"``.
        Trailing slash optional; ``/a2a`` is appended at request time.
    signing_key:
        The Legba instance's Ed25519 signing key. Reuses the same key
        the A2A *server* uses (``legba.ui.agent_card.get_instance_signing_key``)
        so a single DID identifies this instance to Mnemosyne in both
        directions.
    signer_did:
        ``did:key:z...`` derived from ``signing_key``'s public bytes.
        Passed in (rather than recomputed) so tests can mismatch them
        deliberately and the runtime doesn't pay the base58btc encode
        cost on every call.
    http_client:
        Shared ``httpx.AsyncClient`` so the runtime can pool connections
        across calls. Tests inject a transport-mocked client.
    expected_responder_did:
        Optional DID of the Mnemosyne instance. When set, the response
        artifact's ``metadata.signer_did`` MUST match this value and the
        artifact signature MUST verify — otherwise the tool returns
        ``{"error": "signature_mismatch"}``. When ``None``, signatures
        are still verified if present but the responder identity is not
        pinned.
    timeout_s:
        Per-request HTTP timeout in seconds. Defaults to
        :data:`DEFAULT_TIMEOUT_S`.
    """

    base_url: str
    signing_key: SigningKey
    signer_did: str
    http_client: httpx.AsyncClient
    expected_responder_did: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Canonical JSON + envelope signing
# ---------------------------------------------------------------------------


def _canonical_json_bytes(data: Any) -> bytes:
    """Canonical-JSON encoding matching Mnemosyne's ``crypto_hash_json``.

    The Mnemosyne side hashes the artifact ``data`` payload with sorted
    keys + tight separators (see
    ``mnemosyne/backend/app/services/sovereignty/crypto.py`` /
    ``crypto_hash_json``). We mirror the same form to keep request and
    response hashes consistent across both endpoints.
    """

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def _build_signed_envelope(
    data: dict[str, Any],
    *,
    signing_key: SigningKey,
    signer_did: str,
    purpose: str = SIGN_PURPOSE,
) -> dict[str, Any]:
    """Build the ``_meta`` block Mnemosyne's ``_verify_sender`` accepts.

    Matches ``A2AServer._verify_sender`` exactly: message is
    ``f"{purpose}:{content_hash}:{nonce}:{timestamp}"``. Mnemosyne
    enforces a 5-minute freshness window on ``timestamp``.
    """

    content_hash = _sha256_hex(_canonical_json_bytes(data))
    nonce = uuid4().hex
    timestamp = int(time.time())
    message = f"{purpose}:{content_hash}:{nonce}:{timestamp}".encode("utf-8")
    signature = signing_key.sign(message).signature
    return {
        "signature": _b64url_nopad(signature),
        "signer_did": signer_did,
        "purpose": purpose,
        "nonce": nonce,
        "timestamp": timestamp,
        "content_hash": content_hash,
    }


# ---------------------------------------------------------------------------
# Artifact-side signature verification
# ---------------------------------------------------------------------------


def _did_key_to_public_bytes(did: str) -> bytes:
    """Decode the Ed25519 public key from a ``did:key:z...`` identifier.

    Defers to the existing implementation in ``legba.ui.agent_card`` so
    we have one canonical decoder per the shared crypto spec. Imported
    lazily because the ``legba.ui`` package pulls FastAPI which we don't
    want as a hard import for tool callers.
    """

    from ...ui.agent_card import did_to_public_key

    return did_to_public_key(did)


def _verify_artifact_signature(
    *,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None,
    expected_did: str | None,
) -> bool:
    """Verify the artifact metadata signature Mnemosyne attaches.

    Mnemosyne's ``A2AServer._sign_artifact`` produces:

      * ``content_hash``: ``"sha256:" + hex(canonical_json(data))``
      * ``signature``:    base64url Ed25519 over ``content_hash.encode()``
      * ``signer_did``:   Mnemosyne instance DID

    Verification rules:

      * If ``expected_did`` is set, ``signer_did`` MUST equal it.
      * If a signature is present, it MUST verify.
      * The recomputed content hash MUST equal the advertised one.
      * If no signature is present and ``expected_did`` is None, the
        artifact is accepted unsigned (matches Mnemosyne's behaviour
        when no signing key is configured).
    """

    metadata = metadata or {}

    advertised_hash = metadata.get("content_hash", "")
    if advertised_hash.startswith("sha256:"):
        advertised_hash = advertised_hash[len("sha256:") :]

    recomputed = _sha256_hex(_canonical_json_bytes(payload))
    if advertised_hash and advertised_hash != recomputed:
        logger.warning(
            "mnemosyne_trust_query artifact content_hash mismatch "
            "(advertised=%s recomputed=%s)",
            advertised_hash,
            recomputed,
        )
        return False

    sig_b64 = metadata.get("signature")
    signer_did = metadata.get("signer_did")

    if expected_did and signer_did != expected_did:
        logger.warning(
            "mnemosyne_trust_query artifact signer_did mismatch "
            "(expected=%s got=%s)",
            expected_did,
            signer_did,
        )
        return False

    if sig_b64 is None:
        # Unsigned artifact — acceptable only when we have no pin.
        return expected_did is None

    if not signer_did:
        return False

    try:
        public_key = _did_key_to_public_bytes(signer_did)
        verify_key = VerifyKey(public_key)
        verify_key.verify(recomputed.encode("utf-8"), _b64url_decode(sig_b64))
        return True
    except (BadSignatureError, ValueError) as exc:
        logger.warning(
            "mnemosyne_trust_query artifact signature verification failed: %s",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Argument validation + payload extraction
# ---------------------------------------------------------------------------


def _validate_args(args: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(args, dict):
        raise MnemosyneTrustQueryError(
            f"args must be a dict, got {type(args).__name__}"
        )
    peer_did = args.get("peer_did")
    if not isinstance(peer_did, str) or not peer_did.startswith("did:key:z"):
        raise MnemosyneTrustQueryError(
            "args['peer_did'] must be a did:key:z... string"
        )
    scope = args.get("scope", "general")
    if not isinstance(scope, str):
        raise MnemosyneTrustQueryError("args['scope'] must be a string")
    if scope not in ALLOWED_SCOPES:
        # Don't block — Mnemosyne is canonical, but warn so misuses surface
        # in logs rather than silently passing through.
        logger.warning(
            "mnemosyne_trust_query: scope=%r not in known set %s",
            scope,
            sorted(ALLOWED_SCOPES),
        )
    return peer_did, scope


def _extract_first_json_part(task: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pull the first ``application/json`` part out of the task envelope.

    Returns ``(payload, metadata)``. Either may be ``None`` when the
    response is malformed.
    """

    artifacts = task.get("artifacts") or []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        parts = artifact.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "application/json":
                data = part.get("data")
                if isinstance(data, dict):
                    return data, artifact.get("metadata")
    return None, None


def _map_to_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce Mnemosyne's full TRUST_QUERY payload to the tool surface.

    Per MN-3 Q13: aggregated weight + hop count only; no raw chain
    bytes. Per P-007 Q5: explicit ``chain_unavailable`` flag short-
    circuits to the error shape.
    """

    if payload.get("chain_unavailable") is True:
        return {"error": "chain_unavailable"}

    distance = payload.get("trust_distance") or {}
    if isinstance(distance, dict) and "distance" in distance:
        hops = distance.get("distance", -1)
        weight = distance.get("trust_weight")
    else:
        hops = -1
        weight = None

    if weight is None:
        # Fall back to top-level trust_score when trust_distance is empty
        # (Mnemosyne returns this for unknown DIDs).
        weight = payload.get("trust_score", 0.0)

    try:
        weight_f = float(weight)
    except (TypeError, ValueError):
        weight_f = 0.0
    try:
        hops_i = int(hops)
    except (TypeError, ValueError):
        hops_i = -1

    return {"weight": weight_f, "hops": hops_i}


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


async def call(
    args: dict[str, Any], deps: MnemosyneTrustQueryDeps
) -> dict[str, Any]:
    """Invoke Mnemosyne's ``trust.query`` skill.

    Parameters
    ----------
    args:
        ``{"peer_did": str, "scope": str}``. ``scope`` defaults to
        ``"general"`` if omitted.
    deps:
        Runtime-resolved :class:`MnemosyneTrustQueryDeps`.

    Returns
    -------
    dict
        Either ``{"weight": float, "hops": int}`` on success or
        ``{"error": <code>}`` on a recoverable failure (see module
        docstring for the error code mapping).

    Raises
    ------
    MnemosyneTrustQueryError
        For *programming* errors — bad ``args`` shape, missing deps
        fields. Network / RPC / signature failures DO NOT raise; they
        return the ``{"error": ...}`` shape so analyst handlers can
        degrade gracefully.
    """

    if not isinstance(deps, MnemosyneTrustQueryDeps):
        raise MnemosyneTrustQueryError(
            f"deps must be MnemosyneTrustQueryDeps, got {type(deps).__name__}"
        )
    if not deps.base_url:
        raise MnemosyneTrustQueryError("deps.base_url is required")
    if deps.signing_key is None:
        raise MnemosyneTrustQueryError("deps.signing_key is required")
    if not deps.signer_did:
        raise MnemosyneTrustQueryError("deps.signer_did is required")

    peer_did, scope = _validate_args(args)

    data_payload: dict[str, Any] = {"subject_did": peer_did, "scope": scope}
    envelope = _build_signed_envelope(
        data_payload,
        signing_key=deps.signing_key,
        signer_did=deps.signer_did,
    )

    request_body = {
        "jsonrpc": "2.0",
        "id": uuid4().hex,
        "method": "tasks/send",
        "params": {
            "skill_id": SKILL_ID,
            "data": data_payload,
            "_meta": envelope,
        },
    }

    url = deps.base_url.rstrip("/") + "/a2a"
    try:
        response = await deps.http_client.post(
            url,
            json=request_body,
            timeout=deps.timeout_s,
        )
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        logger.warning("mnemosyne_trust_query transport error: %s", exc)
        return {"error": "transport_error"}

    if response.status_code >= 500:
        logger.warning(
            "mnemosyne_trust_query upstream HTTP %d", response.status_code
        )
        return {"error": "transport_error"}

    try:
        rpc_body = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("mnemosyne_trust_query response not JSON: %s", exc)
        return {"error": "rpc_error"}

    if not isinstance(rpc_body, dict):
        return {"error": "rpc_error"}

    if "error" in rpc_body:
        logger.warning(
            "mnemosyne_trust_query JSON-RPC error: %s",
            rpc_body.get("error"),
        )
        return {"error": "rpc_error"}

    task = rpc_body.get("result")
    if not isinstance(task, dict):
        return {"error": "rpc_error"}

    state = (task.get("state") or "").lower()
    if state not in {"completed", "working"}:
        logger.warning(
            "mnemosyne_trust_query task in non-success state: %s", state
        )
        return {"error": "task_failed"}

    payload, metadata = _extract_first_json_part(task)
    if payload is None:
        return {"error": "task_failed"}

    if not _verify_artifact_signature(
        payload=payload,
        metadata=metadata,
        expected_did=deps.expected_responder_did,
    ):
        return {"error": "signature_mismatch"}

    return _map_to_tool_result(payload)
