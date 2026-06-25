# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-210 — Outbound A2A client for talking to Mnemosyne.

Where :mod:`legba.data.outputs.a2a_skill` is the *inbound* surface
(Mnemosyne calls Legba), this module is the *outbound* surface (Legba
calls Mnemosyne). The two share an envelope shape so a single canonical
form covers both directions.

Wire format (the "legba-native" A2A envelope)
---------------------------------------------

Mirrors :func:`legba.data.outputs.a2a_skill.build_envelope` exactly so
the same signing/verification helpers work on both sides::

    {
      "envelope": {
        "envelope_version": "1",
        "skill_id":         "<dotted.skill.name>",
        "nonce":            "<uuid hex>",
        "issued_at":        "<RFC3339 UTC>",
        "sender_did":       "did:legba:registry:<host>",
        "recipient_did":    "did:key:z6Mk...",
        "signer_did":       "did:legba:registry:<host>",   # alias for sender_did, kept
                                                            #   so the inbound verifier
                                                            #   (a2a_skill.py) accepts the
                                                            #   same wire form
        "payload":          { ... skill args (request) or skill output (response) }
      },
      "signature": "<base64url(Ed25519(canonical_json(envelope)))>"
    }

The signature covers ``canonical_json(envelope)`` (the inner object), not
the outer wrapper. Response envelopes have the directions reversed and
are signed by the recipient.

Trust-query shim (L-211 interop)
--------------------------------

The :meth:`MnemosyneA2AClient.trust_query` convenience wrapper takes a
target DID + scope and returns the L-210 contract shape
``{"score": float, "rationale": str, "hop_count": int, ...}``. It maps
Mnemosyne's underlying ``trust.query`` payload (``trust_score``,
``trust_distance``, ``recommendation``) onto that surface. The L-211
trust-query analyst tool keeps its own thinner ``{weight, hops}`` surface
for backwards compatibility — see
:mod:`legba.data.tools.mnemosyne_trust_query`.

Response signature verification
-------------------------------

When ``trusted_keys`` is set, the client looks up the response signer's
verify-key and rejects any envelope whose signature does not verify
(:class:`A2ASignatureError`). When ``trusted_keys`` is ``None`` (dev
mode) the client logs a warning and accepts the response unsigned.
This is the same posture as ``a2a_skill.py`` for unknown signers; it
keeps the client usable in single-instance dev setups where the
operator hasn't populated the trusted-DID directory.

Env-var driven config (:meth:`MnemosyneA2AClient.from_env`)
-----------------------------------------------------------

* ``MNEMOSYNE_A2A_URL``        — base URL, e.g. ``https://mnemosyne.example``.
* ``MNEMOSYNE_RECIPIENT_DID``  — the Mnemosyne instance DID to address
                                  envelopes to.
* ``MNEMOSYNE_A2A_TIMEOUT_S``  — float, optional (default 30.0).
* Identity resolved from :func:`legba.data.registry.signing.load_default_identity`.
* Trusted keys resolved from
  :meth:`legba.data.outputs.a2a_skill.TrustedKeyDirectory.from_env`
  (``LEGBA_A2A_TRUSTED_KEYS``).
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from ..data.outputs.a2a_skill import (
    ENVELOPE_VERSION,
    TrustedKeyDirectory,
)
from ..data.provenance import canonical_json
from ..data.registry.signing import SigningIdentity, load_default_identity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Skill id for the trust-query convenience wrapper. Matches Mnemosyne's
#: ``TRUST_QUERY.id`` (``mnemosyne/backend/app/services/a2a/skills.py``).
TRUST_QUERY_SKILL_ID = "trust.query"

#: Default per-request timeout. Trust queries are interactive so we fail
#: fast, but other skills (memory.search, etc.) may run longer — 30 s is
#: the dial-tone default; callers override via the constructor.
DEFAULT_TIMEOUT_S = 30.0

#: Env-var names. Kept as constants so tests and runtime wiring can
#: reference one source of truth.
ENV_BASE_URL = "MNEMOSYNE_A2A_URL"
ENV_RECIPIENT_DID = "MNEMOSYNE_RECIPIENT_DID"
ENV_TIMEOUT_S = "MNEMOSYNE_A2A_TIMEOUT_S"


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class A2AError(RuntimeError):
    """Base class for outbound-A2A errors.

    The L-211 tool *does not* raise these — it traps them and returns an
    ``{"error": ...}`` shape so analyst handlers degrade gracefully.
    Direct callers (e.g. operator scripts, the future federation hook)
    may want to catch the specific subclass.
    """


class A2ATransportError(A2AError):
    """Network-layer failure — DNS, connect, TLS, timeout, 5xx.

    The remote either never answered or returned a server-side error
    that the client can't reason about. Retries are reasonable.
    """


class A2ASignatureError(A2AError):
    """The response envelope failed signature verification.

    Either the signer DID is unknown in :class:`TrustedKeyDirectory`, the
    signature does not verify against the advertised signer, or the
    envelope contradicts itself (e.g. ``signer_did`` ≠ ``sender_did``).
    Treat as a *security* failure: do NOT retry, escalate to operator.
    """


class A2ARemoteError(A2AError):
    """The remote answered with a 4xx error.

    Carries the parsed ``detail`` from the JSON body when present so
    callers can surface it. The ``status_code`` attribute lets callers
    distinguish auth (401), bad envelope (400/422), and skill-not-found
    (404) without re-parsing.
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"A2A remote {status_code}: {detail!r}")


# ---------------------------------------------------------------------------
# Envelope helpers (mirror a2a_skill.py)
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_request_envelope(
    *,
    skill_id: str,
    payload: Mapping[str, Any],
    sender_did: str,
    recipient_did: str,
    nonce: str | None = None,
    issued_at: str | None = None,
) -> dict[str, Any]:
    """Construct the inner envelope dict (pre-signature) for an outbound
    request. ``signer_did`` is set equal to ``sender_did`` so the inbound
    verifier (which keys off ``signer_did``) accepts the same wire form.
    """
    return {
        "envelope_version": ENVELOPE_VERSION,
        "skill_id": skill_id,
        "nonce": nonce or uuid4().hex,
        "issued_at": issued_at or _utcnow_iso(),
        "sender_did": sender_did,
        "recipient_did": recipient_did,
        "signer_did": sender_did,
        "payload": dict(payload),
    }


def _sign_envelope(envelope: Mapping[str, Any], signing_key: SigningKey) -> str:
    sig = signing_key.sign(canonical_json(envelope)).signature
    return _b64url(sig)


def _verify_envelope(
    envelope: Mapping[str, Any],
    signature_b64url: str,
    verify_key: VerifyKey,
) -> None:
    """Verify the envelope signature; raises :class:`A2ASignatureError`
    on failure (no return value — match the inbound verify_envelope's
    raise-on-failure convention but with our typed exception)."""
    try:
        sig = _b64url_decode(signature_b64url)
    except Exception as exc:
        raise A2ASignatureError(
            f"response signature not valid base64url: {exc}"
        ) from exc
    try:
        verify_key.verify(canonical_json(envelope), sig)
    except BadSignatureError as exc:
        raise A2ASignatureError(f"bad response envelope signature: {exc}") from exc


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MnemosyneA2AClient:
    """Outbound A2A client for Mnemosyne skill invocation.

    Responsibilities:

      * Build the Ed25519-signed request envelope (mirrors the inbound
        wire form from :mod:`legba.data.outputs.a2a_skill`).
      * POST to ``{base_url}/a2a/skills/{skill_id}``.
      * Verify the response envelope's signature when the response
        signer DID is in :class:`TrustedKeyDirectory`; warn-and-accept
        when ``trusted_keys`` is ``None``.
      * Surface typed exceptions (:class:`A2ATransportError`,
        :class:`A2ASignatureError`, :class:`A2ARemoteError`) so callers
        can branch on failure mode.

    The client is stateless beyond its identity / trusted-keys config;
    individual ``invoke`` calls are independent and safe to issue
    concurrently. ``httpx`` is imported lazily inside :meth:`invoke` to
    avoid the runtime sandbox cascade (any module that imports legba.runtime
    at import time pulls Temporal/Dapr deps).

    Parameters
    ----------
    base_url:
        Mnemosyne deployment root, e.g. ``https://mnemosyne.example.org``.
        Trailing slash optional; ``/a2a/skills/{skill_id}`` is appended at
        request time.
    sender_did:
        DID of THIS Legba instance — appears in the envelope as
        ``sender_did``/``signer_did``. Conventionally
        ``identity.signer_did``.
    identity:
        :class:`SigningIdentity` carrying the Ed25519 ``signing_key``.
        The task spec called this ``Ed25519Identity``; the actual class
        in this codebase is :class:`SigningIdentity` — same thing.
    recipient_did:
        DID of the Mnemosyne instance we're talking to. Embedded in the
        envelope and, when ``trusted_keys`` is populated, used to look
        up the verify key for response-signature checks.
    trusted_keys:
        Optional :class:`TrustedKeyDirectory`. When set, response signer
        DIDs MUST be present (else :class:`A2ASignatureError`). When
        ``None`` the client logs a warning and accepts unsigned/unknown
        responses — useful for dev / single-instance bring-up.
    timeout_seconds:
        Per-request HTTP timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        sender_did: str,
        identity: SigningIdentity,
        recipient_did: str,
        trusted_keys: TrustedKeyDirectory | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not sender_did:
            raise ValueError("sender_did is required")
        if not recipient_did:
            raise ValueError("recipient_did is required")
        if identity is None or identity.signing_key is None:
            raise ValueError("identity with signing_key is required")
        self._base_url = base_url.rstrip("/")
        self._sender_did = sender_did
        self._recipient_did = recipient_did
        self._identity = identity
        self._trusted_keys = trusted_keys
        self._timeout_s = float(timeout_seconds)

    # -- factory --------------------------------------------------------

    @classmethod
    def from_env(cls) -> "MnemosyneA2AClient":
        """Resolve config from environment variables.

        Required: ``MNEMOSYNE_A2A_URL``, ``MNEMOSYNE_RECIPIENT_DID``.
        Optional: ``MNEMOSYNE_A2A_TIMEOUT_S`` (float). Identity is
        loaded via :func:`load_default_identity`; trusted keys via
        :meth:`TrustedKeyDirectory.from_env`.

        Raises
        ------
        ValueError
            If a required env var is missing or unparsable.
        """
        base_url = os.getenv(ENV_BASE_URL, "").strip()
        if not base_url:
            raise ValueError(
                f"{ENV_BASE_URL} is required to construct MnemosyneA2AClient.from_env()"
            )
        recipient_did = os.getenv(ENV_RECIPIENT_DID, "").strip()
        if not recipient_did:
            raise ValueError(
                f"{ENV_RECIPIENT_DID} is required to construct MnemosyneA2AClient.from_env()"
            )
        timeout_s_raw = os.getenv(ENV_TIMEOUT_S, "").strip()
        if timeout_s_raw:
            try:
                timeout_s = float(timeout_s_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{ENV_TIMEOUT_S} must be a float, got {timeout_s_raw!r}"
                ) from exc
        else:
            timeout_s = DEFAULT_TIMEOUT_S

        identity = load_default_identity()
        trusted_keys = TrustedKeyDirectory.from_env()
        return cls(
            base_url=base_url,
            sender_did=identity.signer_did,
            identity=identity,
            recipient_did=recipient_did,
            trusted_keys=trusted_keys if trusted_keys.keys else None,
            timeout_seconds=timeout_s,
        )

    # -- accessors (for tests / observability) --------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def sender_did(self) -> str:
        return self._sender_did

    @property
    def recipient_did(self) -> str:
        return self._recipient_did

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_s

    # -- core ------------------------------------------------------------

    def build_envelope(
        self,
        *,
        skill_id: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build a signed ``{envelope, signature}`` wire dict.

        Exposed publicly so tests + the future federation hook can
        construct envelopes without going through :meth:`invoke`.
        """
        envelope = _build_request_envelope(
            skill_id=skill_id,
            payload=args,
            sender_did=self._sender_did,
            recipient_did=self._recipient_did,
        )
        signature = _sign_envelope(envelope, self._identity.signing_key)
        return {"envelope": envelope, "signature": signature}

    async def invoke(
        self,
        skill_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke a Mnemosyne A2A skill.

        Constructs the signed envelope, POSTs to
        ``{base_url}/a2a/skills/{skill_id}``, verifies the response
        signature (when ``trusted_keys`` is set), and returns the inner
        response ``payload`` dict.

        Parameters
        ----------
        skill_id:
            Dotted skill identifier (e.g. ``"trust.query"``).
        args:
            Skill-specific arguments. Embedded as the envelope's
            ``payload``; the remote validates against its own
            ``input_schema``.

        Returns
        -------
        dict
            The inner ``payload`` from the response envelope.

        Raises
        ------
        A2ATransportError
            DNS / connect / TLS / timeout failure, or 5xx response.
        A2ARemoteError
            4xx response (carries ``status_code`` + ``detail``).
        A2ASignatureError
            Response envelope signature missing/invalid (when
            ``trusted_keys`` is set), or signer DID mismatch.
        """
        # Imported lazily — see class docstring re: sandbox cascade.
        import httpx

        if not skill_id:
            raise ValueError("skill_id is required")
        if not isinstance(args, dict):
            raise ValueError(f"args must be a dict, got {type(args).__name__}")

        wire = self.build_envelope(skill_id=skill_id, args=args)
        url = f"{self._base_url}/a2a/skills/{skill_id}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(url, json=wire)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.warning(
                "mnemosyne_a2a transport error skill_id=%s url=%s err=%s",
                skill_id, url, exc,
            )
            raise A2ATransportError(
                f"transport error calling {url}: {exc}"
            ) from exc

        return self._handle_response(response, skill_id=skill_id, url=url)

    # -- response handling ----------------------------------------------

    def _handle_response(
        self,
        response: Any,  # httpx.Response — typed Any to keep httpx import lazy
        *,
        skill_id: str,
        url: str,
    ) -> dict[str, Any]:
        """Branch on HTTP status, verify signature, extract payload.

        Split out so tests can exercise the response-handling path
        without going through the network — and so the JSON-body parse
        / 4xx mapping is in one place rather than scattered across the
        invoke flow.
        """
        status = response.status_code

        if status >= 500:
            # Server-side failure — body may not be JSON, body() may not
            # even be set on transport errors. Surface the raw body when
            # we can; otherwise just the status.
            detail = self._safe_body_excerpt(response)
            logger.warning(
                "mnemosyne_a2a upstream %d skill_id=%s body=%s",
                status, skill_id, detail,
            )
            raise A2ATransportError(
                f"upstream {status} from {url}: {detail!r}"
            )

        if status >= 400:
            detail = self._safe_json_detail(response)
            logger.warning(
                "mnemosyne_a2a 4xx skill_id=%s status=%d detail=%s",
                skill_id, status, detail,
            )
            raise A2ARemoteError(status, detail)

        # 2xx — expect {envelope, signature}.
        try:
            body = response.json()
        except Exception as exc:
            raise A2ARemoteError(status, f"response not JSON: {exc}")

        if not isinstance(body, dict):
            raise A2ARemoteError(
                status, f"response body must be object, got {type(body).__name__}",
            )

        envelope = body.get("envelope")
        signature = body.get("signature")
        if not isinstance(envelope, dict):
            raise A2ARemoteError(
                status, "response missing 'envelope' object",
            )

        # Sanity check skill_id round-trip.
        if envelope.get("skill_id") and envelope["skill_id"] != skill_id:
            raise A2ASignatureError(
                f"response envelope.skill_id {envelope.get('skill_id')!r} "
                f"does not match requested {skill_id!r}"
            )

        signer_did = envelope.get("signer_did") or envelope.get("sender_did") or ""

        if self._trusted_keys is not None:
            if not signer_did:
                raise A2ASignatureError(
                    "response envelope has no signer_did but trusted_keys is set"
                )
            if not isinstance(signature, str):
                raise A2ASignatureError(
                    "response missing 'signature' string but trusted_keys is set"
                )
            verify_key = self._trusted_keys.get(signer_did)
            if verify_key is None:
                raise A2ASignatureError(
                    f"response signer_did {signer_did!r} not in trusted_keys directory"
                )
            # Strip signer_did to match what was signed? No — the inbound
            # verifier signs the full envelope including signer_did, so
            # we verify the same thing.
            _verify_envelope(envelope, signature, verify_key)
        else:
            # Dev mode — log so operators see this in audit.
            logger.warning(
                "mnemosyne_a2a accepting response without signature verification "
                "(trusted_keys=None, signer_did=%r)",
                signer_did,
            )

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            # Some skills may legitimately return non-dict payloads in
            # the future; surface what we got rather than guess.
            return {"_raw_payload": payload}
        return payload

    @staticmethod
    def _safe_json_detail(response: Any) -> Any:
        try:
            body = response.json()
        except Exception:
            return MnemosyneA2AClient._safe_body_excerpt(response)
        if isinstance(body, dict):
            # FastAPI's HTTPException puts the message under "detail".
            return body.get("detail", body)
        return body

    @staticmethod
    def _safe_body_excerpt(response: Any, limit: int = 256) -> str:
        try:
            text = response.text or ""
        except Exception:
            return "<unreadable body>"
        return text[:limit]

    # -- convenience wrappers -------------------------------------------

    async def trust_query(
        self,
        target_did: str,
        scope: str = "general",
    ) -> dict[str, Any]:
        """Convenience wrapper for the ``trust.query`` skill.

        Maps the L-210 contract surface
        (``{score, rationale, hop_count, ...}``) over Mnemosyne's
        ``TRUST_QUERY.output_schema`` (``trust_score``, ``trust_level``,
        ``trust_distance.{distance,trust_weight,reachable}``,
        ``recommendation``, …).

        The L-211 analyst tool (`mnemosyne_trust_query.py`) keeps its
        own ``{weight, hops}`` surface for analysts; this method serves
        operator scripts and future federation hooks that want the
        richer shape with rationale text.

        Parameters
        ----------
        target_did:
            The peer DID being queried (``did:key:z...``). Becomes the
            envelope payload's ``subject_did`` field.
        scope:
            Trust scope per Mnemosyne's enum — ``general``,
            ``data_access``, ``delegation``, ``group_membership``.
            Default ``"general"``.

        Returns
        -------
        dict
            ``{"score": float, "rationale": str, "hop_count": int,
            "trust_level": str | None, "recommendation": str | None,
            "subject_did": str, "raw": dict}``. The ``raw`` field
            carries the full Mnemosyne payload for callers that need
            the un-shimmed data (evidence summary, sybil flag, etc.).

        Raises
        ------
        Same as :meth:`invoke`: :class:`A2ATransportError`,
        :class:`A2ARemoteError`, :class:`A2ASignatureError`.
        """
        if not target_did:
            raise ValueError("target_did is required")
        args = {"subject_did": target_did, "scope": scope}
        payload = await self.invoke(TRUST_QUERY_SKILL_ID, args)
        return _map_trust_query_payload(payload, target_did=target_did)


# ---------------------------------------------------------------------------
# trust.query payload mapping (kept module-level so tests can target it)
# ---------------------------------------------------------------------------


def _map_trust_query_payload(
    payload: Mapping[str, Any],
    *,
    target_did: str,
) -> dict[str, Any]:
    """Project Mnemosyne's TRUST_QUERY output onto L-210's contract.

    Mapping rules:

      * ``score``         ← ``trust_score`` (top-level), or
                            ``trust_distance.trust_weight`` when the
                            top-level value is missing/None.
      * ``hop_count``     ← ``trust_distance.distance`` (``-1`` when
                            ``trust_distance`` is absent or
                            unreachable per Mnemosyne's convention).
      * ``trust_level``   ← passed through as-is when present.
      * ``recommendation``← passed through as-is when present.
      * ``rationale``     ← synthesised from ``trust_level`` +
                            ``recommendation`` + endorsement/evidence
                            hints. Mnemosyne doesn't emit a free-form
                            rationale today; this gives callers a
                            human-readable summary derived from the
                            structured fields. Stable enough that
                            tests can pin specific phrasings; expand
                            once Mnemosyne adds a real rationale
                            field (P-007 follow-up).
      * ``subject_did``   ← echoed from Mnemosyne's payload or
                            target_did, whichever is present.
      * ``raw``           ← the full Mnemosyne payload (untouched).
    """
    raw = dict(payload) if isinstance(payload, Mapping) else {}

    trust_distance = raw.get("trust_distance") or {}
    if not isinstance(trust_distance, dict):
        trust_distance = {}

    # Score — prefer top-level trust_score, fall back to trust_weight.
    score: float = 0.0
    raw_score = raw.get("trust_score")
    if raw_score is None:
        raw_score = trust_distance.get("trust_weight")
    try:
        if raw_score is not None:
            score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0

    # Hop count — distance from the dijkstra path, or -1.
    hop_count = -1
    raw_distance = trust_distance.get("distance")
    try:
        if raw_distance is not None:
            hop_count = int(raw_distance)
    except (TypeError, ValueError):
        hop_count = -1

    trust_level = raw.get("trust_level")
    if not isinstance(trust_level, str):
        trust_level = None
    recommendation = raw.get("recommendation")
    if not isinstance(recommendation, str):
        recommendation = None

    subject_did = raw.get("subject_did") if isinstance(raw.get("subject_did"), str) else target_did

    return {
        "score": score,
        "rationale": _synthesise_rationale(
            trust_level=trust_level,
            recommendation=recommendation,
            hop_count=hop_count,
            score=score,
            endorsements=raw.get("endorsement_count"),
            sybil_verified=raw.get("sybil_verified"),
        ),
        "hop_count": hop_count,
        "trust_level": trust_level,
        "recommendation": recommendation,
        "subject_did": subject_did,
        "raw": raw,
    }


def _synthesise_rationale(
    *,
    trust_level: str | None,
    recommendation: str | None,
    hop_count: int,
    score: float,
    endorsements: Any,
    sybil_verified: Any,
) -> str:
    """Build a human-readable rationale from the structured fields.

    Output shape is deliberately deterministic + testable: leading
    trust-level phrase, then optional distance, recommendation, and
    sybil/endorsement hints separated by '; '. When everything is
    unknown we return a single ``"trust evidence unavailable"`` so
    callers can still display *something* meaningful.
    """
    parts: list[str] = []
    if trust_level:
        parts.append(f"trust_level={trust_level}")
    if hop_count >= 0:
        parts.append(f"reachable in {hop_count} hops")
    elif trust_level == "unknown" or trust_level is None:
        # Don't double-report; the "unknown" trust_level already implies
        # unreachability. Only add the bare "unreachable" hint when we
        # have no trust_level signal.
        if trust_level is None:
            parts.append("not reachable in trust graph")
    if recommendation:
        parts.append(f"recommendation={recommendation}")
    try:
        endorsement_n = int(endorsements) if endorsements is not None else None
    except (TypeError, ValueError):
        endorsement_n = None
    if endorsement_n is not None and endorsement_n > 0:
        parts.append(f"{endorsement_n} endorsements")
    if sybil_verified is True:
        parts.append("sybil-verified")
    elif sybil_verified is False:
        parts.append("not sybil-verified")

    if not parts:
        return "trust evidence unavailable"
    parts.append(f"score={score:.3f}")
    return "; ".join(parts)


__all__ = [
    "A2AError",
    "A2ARemoteError",
    "A2ASignatureError",
    "A2ATransportError",
    "DEFAULT_TIMEOUT_S",
    "ENV_BASE_URL",
    "ENV_RECIPIENT_DID",
    "ENV_TIMEOUT_S",
    "MnemosyneA2AClient",
    "TRUST_QUERY_SKILL_ID",
]
