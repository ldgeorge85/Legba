# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discord webhook source kind (L-137).

Implements :class:`DiscordWebhookSourceHandler` — an *inbound* source that
receives Discord events via a registered HTTP webhook rather than polling.

Discord delivers events two ways (per developer docs):

  1. **Interactions (slash commands, components):** POST to the application's
     Interactions Endpoint URL, signed Ed25519. Requires PING/PONG handshake
     during registration.
  2. **Event webhooks (message_create, etc.):** POST signed the same way
     when the app is subscribed to those event types.

Both shapes are accepted here and emitted as :class:`Signal`s into the
shared substrate via an ``emit_signal`` callback. The pull path is a no-op
generator per L-102 push-source convention — the signal-write happens
inside :meth:`handle_webhook`, not :meth:`pull`.

Signature scheme (Discord docs, "Validating Security Request Headers"):

  * Sign string = ``X-Signature-Timestamp + raw_body``.
  * Verify with the application public key from ``X-Signature-Ed25519``.
  * On verification failure → respond 401. Discord retries with backoff;
    persistent failures pause the endpoint.

The handler conforms to the L-102 :class:`SourceHandler` Protocol via
:mod:`._contract` so the runtime can host it identically to polling sources.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Literal
from uuid import uuid4

from fastapi import HTTPException, Request, Response, status
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import Signal, SourceContext, SourceHealth
from .webhook_router import InboundWebhookRouter, default_router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discord protocol constants — per
# https://discord.com/developers/docs/interactions/receiving-and-responding
# ---------------------------------------------------------------------------

INTERACTION_TYPE_PING = 1
INTERACTION_TYPE_APPLICATION_COMMAND = 2
INTERACTION_TYPE_MESSAGE_COMPONENT = 3
INTERACTION_TYPE_AUTOCOMPLETE = 4
INTERACTION_TYPE_MODAL_SUBMIT = 5

# Event-webhook event type strings Legba surfaces; see "Webhook Events"
# in Discord docs. Open list — handler stamps whatever Discord sends.
EVENT_TYPE_INTERACTION_PING = "interaction.ping"
EVENT_TYPE_APPLICATION_COMMAND = "interaction.application_command"
EVENT_TYPE_MESSAGE_COMPONENT = "interaction.message_component"
EVENT_TYPE_AUTOCOMPLETE = "interaction.autocomplete"
EVENT_TYPE_MODAL_SUBMIT = "interaction.modal_submit"
EVENT_TYPE_MESSAGE_CREATE = "message_create"
EVENT_TYPE_UNKNOWN = "unknown"

_INTERACTION_TYPE_NAMES = {
    INTERACTION_TYPE_PING: EVENT_TYPE_INTERACTION_PING,
    INTERACTION_TYPE_APPLICATION_COMMAND: EVENT_TYPE_APPLICATION_COMMAND,
    INTERACTION_TYPE_MESSAGE_COMPONENT: EVENT_TYPE_MESSAGE_COMPONENT,
    INTERACTION_TYPE_AUTOCOMPLETE: EVENT_TYPE_AUTOCOMPLETE,
    INTERACTION_TYPE_MODAL_SUBMIT: EVENT_TYPE_MODAL_SUBMIT,
}


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class DiscordWebhookConfig(BaseModel):
    """L-137 source-config schema.

    Field semantics:
      * ``application_id`` — Discord application / bot id; stamped on every
        emitted Signal for provenance.
      * ``public_key_secret`` — vault secret reference (a dotted id resolved
        via :class:`CredentialResolverProtocol`). The plaintext is the
        application's Ed25519 public key as hex (64 chars, 32 bytes).
      * ``allowed_event_types`` — optional whitelist. If set, payloads whose
        derived event_type isn't in this list are accepted (signature must
        still verify) but **not** emitted as Signals. ``None`` = pass-through.
    """

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1, description="Discord application id")
    public_key_secret: str = Field(
        min_length=1,
        description="vault credential id resolving to hex Ed25519 public key",
    )
    allowed_event_types: list[str] | None = Field(
        default=None,
        description=(
            "If set, only Signals whose event_type is in this list are emitted. "
            "Verification still runs on filtered events."
        ),
    )

    @field_validator("allowed_event_types")
    @classmethod
    def _normalize_event_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        # Defensive: empty list means "drop everything" (legitimate), but a
        # list with empty strings is operator error.
        cleaned = [e.strip() for e in v if e and e.strip()]
        if len(cleaned) != len(v):
            raise ValueError(
                "allowed_event_types contains empty / whitespace-only strings"
            )
        return cleaned


# ---------------------------------------------------------------------------
# Signature verification — exposed for unit tests
# ---------------------------------------------------------------------------


class DiscordSignatureError(Exception):
    """Raised when a request fails Ed25519 signature verification."""


def _decode_public_key(material: bytes | str) -> VerifyKey:
    """Construct a :class:`VerifyKey` from hex (str/bytes) or raw bytes.

    Discord publishes the public key as 64 hex chars in the developer
    dashboard; the vault may store either the hex string or the raw 32 bytes
    depending on how the operator registered it.
    """
    if isinstance(material, VerifyKey):
        return material
    if isinstance(material, bytes):
        # Maybe raw 32 bytes; maybe hex-encoded bytes.
        if len(material) == 32:
            return VerifyKey(material)
        try:
            return VerifyKey(bytes.fromhex(material.decode("ascii").strip()))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DiscordSignatureError(
                f"public key bytes are neither 32 raw nor hex-encoded: {exc}"
            ) from exc
    if isinstance(material, str):
        try:
            return VerifyKey(bytes.fromhex(material.strip()))
        except ValueError as exc:
            raise DiscordSignatureError(
                f"public key string is not 64-char hex: {exc}"
            ) from exc
    raise DiscordSignatureError(
        f"unsupported public key material type: {type(material).__name__}"
    )


def verify_discord_signature(
    *,
    public_key: bytes | str | VerifyKey,
    signature_hex: str,
    timestamp: str,
    body: bytes,
) -> None:
    """Verify a Discord-style Ed25519 signature.

    Raises :class:`DiscordSignatureError` on any failure mode (malformed
    signature, malformed timestamp, mismatched signature). Returns ``None``
    on success.

    Per Discord docs:
        signed = timestamp.encode() + body
        verify(signed, bytes.fromhex(signature_hex), public_key)
    """
    vk = _decode_public_key(public_key)

    if not signature_hex or not isinstance(signature_hex, str):
        raise DiscordSignatureError("missing or empty X-Signature-Ed25519 header")
    if not timestamp or not isinstance(timestamp, str):
        raise DiscordSignatureError("missing or empty X-Signature-Timestamp header")

    try:
        signature = bytes.fromhex(signature_hex.strip())
    except ValueError as exc:
        raise DiscordSignatureError(
            f"X-Signature-Ed25519 is not valid hex: {exc}"
        ) from exc

    if len(signature) != 64:
        raise DiscordSignatureError(
            f"X-Signature-Ed25519 must decode to 64 bytes, got {len(signature)}"
        )

    signed = timestamp.encode("utf-8") + body

    try:
        vk.verify(signed, signature)
    except BadSignatureError as exc:
        raise DiscordSignatureError(f"signature verification failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(value: Any) -> datetime:
    """Best-effort parse of a Discord timestamp into UTC ``datetime``.

    Discord uses ISO 8601 with millis (e.g. ``2026-05-15T10:00:00.123000+00:00``);
    some fields are unix seconds. Falls back to ``now`` on parse failure (the
    runtime stamps ``fetched_at`` independently — ``published_at`` going wrong
    shouldn't lose the signal).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError):
            return datetime.now(tz=timezone.utc)
    if isinstance(value, str):
        # Discord uses Z-suffix or explicit offset.
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return datetime.now(tz=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(tz=timezone.utc)


def _content_hash(payload: dict[str, Any]) -> str:
    """Stable hash of the parsed payload for dedupe tier 2."""
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ParsedDiscordEvent(BaseModel):
    """Normalized view extracted from a raw Discord webhook payload.

    Kept separate from :class:`Signal` so parsing logic can be unit-tested
    without instantiating a full SourceContext. The handler turns one of
    these into a Signal at emit time.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str
    event_type: str
    text: str
    channel: str | None = None
    author: str | None = None
    guild_id: str | None = None
    published_at: datetime
    interaction_type: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


def parse_discord_payload(payload: dict[str, Any]) -> ParsedDiscordEvent:
    """Coerce a raw Discord JSON payload into a :class:`ParsedDiscordEvent`.

    Handles three flavors:
      1. Interaction (top-level ``type`` is an integer + ``id`` present).
      2. Event-webhook with ``type=="MESSAGE_CREATE"`` shape (Discord's
         ``EVENT`` webhook type, payload nested under ``data``).
      3. Anything else → ``event_type=EVENT_TYPE_UNKNOWN`` and best-effort
         extraction. Signal still emits — the runtime's dead-letter handling
         applies downstream if the event_type filter drops it.
    """
    # --- 1. Interaction shape ---------------------------------------------
    if isinstance(payload.get("type"), int):
        interaction_type = int(payload["type"])
        event_type = _INTERACTION_TYPE_NAMES.get(interaction_type, EVENT_TYPE_UNKNOWN)
        external_id = str(payload.get("id") or uuid4())
        channel = (
            str(payload["channel_id"])
            if payload.get("channel_id") is not None
            else None
        )
        guild = (
            str(payload["guild_id"])
            if payload.get("guild_id") is not None
            else None
        )
        # Author is under member.user.id (guild context) or user.id (DM).
        author = None
        member = payload.get("member") or {}
        user = (member.get("user") if isinstance(member, dict) else None) or payload.get("user")
        if isinstance(user, dict):
            author = str(user.get("id") or "") or None

        # Text: depends on interaction kind.
        text = ""
        data = payload.get("data") or {}
        if isinstance(data, dict):
            # APPLICATION_COMMAND → command name (+ options stringified).
            if interaction_type == INTERACTION_TYPE_APPLICATION_COMMAND:
                name = str(data.get("name") or "")
                opts = data.get("options") or []
                option_repr = ""
                if isinstance(opts, list) and opts:
                    flat = []
                    for opt in opts:
                        if isinstance(opt, dict):
                            flat.append(
                                f"{opt.get('name', '')}={opt.get('value', '')}"
                            )
                    if flat:
                        option_repr = " " + " ".join(flat)
                text = (f"/{name}{option_repr}").strip()
            elif interaction_type == INTERACTION_TYPE_MESSAGE_COMPONENT:
                text = str(data.get("custom_id") or "")
            elif interaction_type == INTERACTION_TYPE_MODAL_SUBMIT:
                text = str(data.get("custom_id") or "")
            elif interaction_type == INTERACTION_TYPE_PING:
                text = ""

        published = _parse_iso_timestamp(
            payload.get("timestamp")
            or payload.get("created_at")
            or datetime.now(tz=timezone.utc)
        )

        return ParsedDiscordEvent(
            external_id=external_id,
            event_type=event_type,
            text=text,
            channel=channel,
            author=author,
            guild_id=guild,
            published_at=published,
            interaction_type=interaction_type,
            raw=payload,
        )

    # --- 2. Event-webhook shape (top-level "type": "MESSAGE_CREATE" etc.) -
    event_string = payload.get("type") or payload.get("event_type")
    if isinstance(event_string, str) and event_string:
        # Discord uses upper-case event names; normalize for matching.
        event_type = event_string.lower()
        # The actual message payload nests under "data" for webhook events.
        data = payload.get("data") or payload
        message_id = (
            data.get("id")
            or data.get("message_id")
            or payload.get("id")
            or uuid4()
        )
        external_id = str(message_id)
        channel = (
            str(data.get("channel_id"))
            if isinstance(data, dict) and data.get("channel_id") is not None
            else None
        )
        guild = (
            str(data.get("guild_id"))
            if isinstance(data, dict) and data.get("guild_id") is not None
            else None
        )
        author = None
        if isinstance(data, dict):
            author_obj = data.get("author") or {}
            if isinstance(author_obj, dict):
                author = str(author_obj.get("id") or "") or None
        text = ""
        if isinstance(data, dict):
            text = str(data.get("content") or "")
        published = _parse_iso_timestamp(
            (data.get("timestamp") if isinstance(data, dict) else None)
            or payload.get("timestamp")
            or datetime.now(tz=timezone.utc)
        )
        return ParsedDiscordEvent(
            external_id=external_id,
            event_type=event_type,
            text=text,
            channel=channel,
            author=author,
            guild_id=guild,
            published_at=published,
            interaction_type=None,
            raw=payload,
        )

    # --- 3. Unknown shape — best effort -----------------------------------
    return ParsedDiscordEvent(
        external_id=str(payload.get("id") or uuid4()),
        event_type=EVENT_TYPE_UNKNOWN,
        text=str(payload.get("content") or ""),
        channel=None,
        author=None,
        guild_id=None,
        published_at=datetime.now(tz=timezone.utc),
        interaction_type=None,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


EmitSignal = Callable[[Signal], Awaitable[None]]
"""Callback shape the runtime supplies so push handlers can publish Signals.

The runtime wires this to the target's NATS stream + provenance write
path. For tests the fixture provides an in-memory list-appender.
"""


class DiscordWebhookSourceHandler:
    """L-137 inbound (push) source kind.

    See module docstring for the architectural shape. This class is wire-
    compatible with the L-102 :class:`SourceHandler` Protocol — ``pull``
    returns an empty async iterator (no-op generator), ``health_check``
    reports webhook reachability + key presence.

    Registration responsibilities (called by the runtime / tests):

      * :meth:`on_configure` — resolve the Ed25519 public key from the vault.
      * :meth:`on_activate` — register with the inbound-webhook router and
        record the URL on ``self.webhook_path``.
      * :meth:`on_pause` / :meth:`on_resume` — flip ``self._paused`` so
        inbound requests respond 503.
      * :meth:`on_retire` — unregister from the router.

    Construction takes the optional dependencies (router, secret resolver,
    emit_signal callback) so tests can wire stand-ins. In runtime use these
    come from :class:`SourceContext` / :class:`RuntimeContext`; the
    convention is "construct, then call lifecycle hooks".
    """

    kind: ClassVar[str] = "discord_webhook"
    family: ClassVar[Literal["source"]] = "source"
    schema_version: ClassVar[str] = "legba/source.discord_webhook/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = DiscordWebhookConfig
    push_source: ClassVar[bool] = True

    def __init__(
        self,
        config: DiscordWebhookConfig,
        *,
        secret_resolver: Callable[[str], Awaitable[bytes]] | None = None,
        webhook_router: InboundWebhookRouter | None = None,
        emit_signal: EmitSignal | None = None,
    ) -> None:
        self.config = config
        self._secret_resolver = secret_resolver
        self._router = webhook_router
        self._emit_signal = emit_signal
        self._public_key: VerifyKey | None = None
        self._paused: bool = False
        self._signals_24h: int = 0
        self._last_inbound_at: datetime | None = None
        self._last_error: str | None = None
        self._signal_ctx: SourceContext | None = None
        self.webhook_path: str | None = None

    # ------------------------------------------------------------------
    # Inbound dispatch — the protocol the webhook_router calls
    # ------------------------------------------------------------------

    @property
    def source_id(self) -> str:
        """``source_id`` for the router. Filled when activated against a context."""
        if self._signal_ctx is None:
            raise RuntimeError(
                "DiscordWebhookSourceHandler not bound to a SourceContext yet"
            )
        return self._signal_ctx.source_id

    async def handle_webhook(self, request: Request) -> Response:
        """Verify the request, parse the payload, emit a Signal."""
        if self._paused:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="discord webhook source is paused",
            )
        if self._public_key is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="discord public key not yet resolved",
            )

        signature = request.headers.get("X-Signature-Ed25519") or ""
        timestamp = request.headers.get("X-Signature-Timestamp") or ""
        body = await request.body()

        try:
            verify_discord_signature(
                public_key=self._public_key,
                signature_hex=signature,
                timestamp=timestamp,
                body=body,
            )
        except DiscordSignatureError as exc:
            self._last_error = f"signature: {exc}"
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid request signature",
            ) from exc

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._last_error = f"json: {exc}"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="payload is not valid UTF-8 JSON",
            ) from exc

        # PING handshake — must respond {"type": 1}. No Signal emitted.
        if (
            isinstance(payload.get("type"), int)
            and payload["type"] == INTERACTION_TYPE_PING
        ):
            self._last_inbound_at = datetime.now(tz=timezone.utc)
            return Response(
                content=json.dumps({"type": 1}).encode("utf-8"),
                media_type="application/json",
            )

        event = parse_discord_payload(payload)

        # allowed_event_types filtering.
        allowed = self.config.allowed_event_types
        if allowed is not None and event.event_type not in allowed:
            self._last_inbound_at = datetime.now(tz=timezone.utc)
            logger.debug(
                "discord_webhook drop event_type=%s allowed=%s",
                event.event_type,
                allowed,
            )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # Emit.
        if self._signal_ctx is None or self._emit_signal is None:
            self._last_error = "handler not configured for emit"
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="handler not bound to a runtime emit callback",
            )

        signal = self._build_signal(event)
        await self._emit_signal(signal)
        self._signals_24h += 1
        self._last_inbound_at = datetime.now(tz=timezone.utc)
        self._last_error = None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def _build_signal(self, event: ParsedDiscordEvent) -> Signal:
        ctx = self._signal_ctx
        assert ctx is not None
        payload: dict[str, Any] = {
            "external_id": event.external_id,
            "published_at": event.published_at.isoformat(),
            "text": event.text,
            "event_type": event.event_type,
            "channel": event.channel,
            "author": event.author,
            "guild_id": event.guild_id,
            "interaction_type": event.interaction_type,
            "application_id": self.config.application_id,
            "raw": event.raw,
        }
        return Signal(
            source_id=ctx.source_id,
            payload=payload,
            content_hash=_content_hash(payload),
            canonical_url=None,
            language_hint=None,
            raw_provenance={
                "application_id": self.config.application_id,
                "discord_event_type": event.event_type,
            },
        )

    # ------------------------------------------------------------------
    # L-102 lifecycle + source-handler surface
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: SourceContext) -> None:
        """Resolve the public key from the vault. Idempotent."""
        self._signal_ctx = ctx
        if self._secret_resolver is None:
            raise RuntimeError(
                "DiscordWebhookSourceHandler requires a secret_resolver for "
                "vault credential resolution"
            )
        raw = await self._secret_resolver(self.config.public_key_secret)
        try:
            self._public_key = _decode_public_key(raw)
        except DiscordSignatureError as exc:
            raise RuntimeError(
                f"public_key_secret {self.config.public_key_secret!r} did not "
                f"resolve to a valid Ed25519 public key: {exc}"
            ) from exc
        ctx.logger.info(
            "discord_webhook configured source_id=%s application_id=%s",
            ctx.source_id,
            self.config.application_id,
        )

    async def on_activate(self, ctx: SourceContext) -> None:
        """Register against the inbound-webhook router."""
        if self._public_key is None:
            await self.on_configure(ctx)
        router = self._router or default_router()
        self._router = router
        self.webhook_path = router.register_handler(self)
        self._paused = False
        ctx.logger.info(
            "discord_webhook activated source_id=%s path=%s",
            ctx.source_id,
            self.webhook_path,
        )

    async def on_pause(self, ctx: SourceContext) -> None:
        self._paused = True

    async def on_resume(self, ctx: SourceContext) -> None:
        self._paused = False

    async def on_retire(self, ctx: SourceContext) -> None:
        if self._router is not None:
            self._router.unregister_handler(ctx.source_id)
        self.webhook_path = None
        self._public_key = None

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """No-op generator — Discord is push-only. Per L-102 push convention
        the signal-write happens in :meth:`handle_webhook`, not here. The
        runtime's polling loop still ticks this; it just yields nothing.
        """
        if False:  # pragma: no cover - generator-typing trick
            yield  # type: ignore[unreachable]
        return

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Health = webhook registered + public key present.

        Optional self-check: signature-verify a known-good test envelope to
        confirm the resolved key still matches what Discord expects. The
        runtime is welcome to schedule this as a synthetic round-trip too.
        """
        registered = (
            self._router is not None
            and self._router.is_registered(ctx.source_id)
        )
        key_present = self._public_key is not None

        if not key_present:
            state = "unhealthy"
        elif not registered:
            state = "degraded"
        else:
            state = "healthy"

        return SourceHealth(
            state=state,
            last_success_at=self._last_inbound_at,
            last_error=self._last_error,
            rows_pulled_24h=self._signals_24h,
            last_cursor=None,
            rate_limit_remaining=None,
            detail={
                "application_id": self.config.application_id,
                "webhook_path": self.webhook_path,
                "registered": registered,
                "public_key_present": key_present,
                "paused": self._paused,
                "allowed_event_types": self.config.allowed_event_types,
            },
        )

    # ------------------------------------------------------------------
    # Test seam — let tests verify the key resolved without poking internals
    # ------------------------------------------------------------------

    def _verify_test_envelope(
        self,
        *,
        signature_hex: str,
        timestamp: str,
        body: bytes,
    ) -> bool:
        """Run a signature-verify dry-run against the resolved key.

        Returns True iff the supplied envelope verifies. Useful as an
        operational self-check ("did the operator paste the right key?").
        Returns False on any failure mode rather than raising — this is for
        health surfaces, not the request path.
        """
        if self._public_key is None:
            return False
        try:
            verify_discord_signature(
                public_key=self._public_key,
                signature_hex=signature_hex,
                timestamp=timestamp,
                body=body,
            )
        except DiscordSignatureError:
            return False
        return True


__all__ = [
    "DiscordSignatureError",
    "DiscordWebhookConfig",
    "DiscordWebhookSourceHandler",
    "EmitSignal",
    "ParsedDiscordEvent",
    "parse_discord_payload",
    "verify_discord_signature",
    "EVENT_TYPE_APPLICATION_COMMAND",
    "EVENT_TYPE_MESSAGE_COMPONENT",
    "EVENT_TYPE_MODAL_SUBMIT",
    "EVENT_TYPE_AUTOCOMPLETE",
    "EVENT_TYPE_INTERACTION_PING",
    "EVENT_TYPE_MESSAGE_CREATE",
    "EVENT_TYPE_UNKNOWN",
    "INTERACTION_TYPE_PING",
    "INTERACTION_TYPE_APPLICATION_COMMAND",
    "INTERACTION_TYPE_MESSAGE_COMPONENT",
    "INTERACTION_TYPE_AUTOCOMPLETE",
    "INTERACTION_TYPE_MODAL_SUBMIT",
]
