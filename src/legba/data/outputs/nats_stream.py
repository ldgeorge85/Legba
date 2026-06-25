# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.outputs.nats_stream — L-191 NATS stream output kind.

Pairs with the L-190 substrate output kind: an analyst that writes a finding
to substrate can ALSO `emit` to a NATS subject so downstream analysts
subscribed to that subject get a notification with the finding envelope.

Contract
--------
* `KIND_NAME = "nats_stream"`
* `async def emit(payload, *, subject, deps) -> None`

The descriptor-derived subject pattern is `analyst.<analyst_id>.<channel>`
per DESIGN.md §11 (Naming conventions, "Analyst NATS subject" row). The
caller resolves the subject via :func:`resolve_subject` from the analyst's
descriptor `outputs[*].config` block — accepting either an explicit
`nats_topic` override, or the (analyst_id, channel="findings") tuple.

Behaviour
---------
* Payload is required to be JSON-serializable. We validate that upfront and
  raise `OutputPayloadError` on failure (the analyst must fix the payload —
  this is a programmer error, not a transient broker issue).
* Subject is required to be a non-empty string with valid NATS subject
  grammar (no whitespace, no NUL). We validate up front.
* JetStream publish is wrapped in bounded retries — the L-190 substrate
  write must NOT be blocked by NATS hiccups, so we surface transient
  failures by routing to a dead-letter subject `legba.dlq.output.nats_stream.*`
  rather than re-raising. Programmer errors (non-serializable payload,
  malformed subject) raise immediately so they surface in tests.

Wiring
------
The output kind reads NATS access from `deps`. Two access patterns are
supported, in order:

1. `deps.nats_publish` — `Callable[[subject, bytes], Awaitable[None]]`.
   This is the `legba.runtime.deps.StandardDeps.nats_publish` slot. It's
   the recommended path because the runtime can swap it for an
   instrumented wrapper (tracing, budget, etc.).
2. `deps.nats_store` — a `legba.data.nats.NatsStore` instance with `.js`
   ready (`await store.connect()`). Used by tests and as a fallback when
   the runtime has not constructed a `StandardDeps`.

If neither is present we raise `OutputDepsError`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


KIND_NAME = "nats_stream"

# Default channel per DESIGN.md §11 ("analyst.<id>.findings" is the canonical
# `Analyst NATS subject` row example).
DEFAULT_CHANNEL = "findings"

# DLQ subject prefix per DESIGN.md §11 ("Dead-letter NATS topic" row:
# `legba.dlq.<namespace>.<id>`). The `<namespace>` for output-kind failures
# is `output.<kind>` so multiple output kinds can share the legba.dlq.* tree.
DLQ_SUBJECT_PREFIX = "legba.dlq.output.nats_stream"

# Bounded retry config. Transient NATS errors (connection drops, no_responders)
# are retried; programmer errors (ValueError / TypeError on payload) are not.
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_SECONDS = 0.1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutputPayloadError(ValueError):
    """Payload could not be JSON-encoded — caller must fix the payload."""


class OutputSubjectError(ValueError):
    """Subject violates NATS subject grammar (whitespace / empty / NUL)."""


class OutputDepsError(RuntimeError):
    """Deps bundle exposes neither `nats_publish` nor `nats_store`."""


# ---------------------------------------------------------------------------
# Subject grammar + resolution
# ---------------------------------------------------------------------------


def _validate_subject(subject: str) -> None:
    """Reject subjects that NATS will not route.

    NATS subjects are dot-separated tokens of printable characters; tokens
    may not contain whitespace or NUL. Wildcards (`*`, `>`) are valid in
    subscriptions but invalid in published subjects — we reject them at
    publish time so an analyst can't accidentally fan-out to siblings.
    """
    if not isinstance(subject, str) or not subject:
        raise OutputSubjectError("subject must be a non-empty string")
    if any(c.isspace() for c in subject):
        raise OutputSubjectError(f"subject contains whitespace: {subject!r}")
    if "\x00" in subject:
        raise OutputSubjectError("subject contains NUL byte")
    # Reject leading / trailing / double dots — would produce an empty token.
    if subject.startswith(".") or subject.endswith(".") or ".." in subject:
        raise OutputSubjectError(f"subject has empty token(s): {subject!r}")
    for tok in subject.split("."):
        if tok in ("*", ">"):
            raise OutputSubjectError(
                f"subject contains wildcard token {tok!r}: not allowed on publish"
            )
        # `*` and `>` are only wildcard *tokens*; embedded in a token they're
        # not technically wildcards per the NATS grammar, but nats-py rejects
        # them with confusing errors. Reject early.
        if "*" in tok or ">" in tok:
            raise OutputSubjectError(
                f"subject token {tok!r} contains '*' or '>'"
            )


def resolve_subject(
    *,
    analyst_id: str | None = None,
    channel: str | None = None,
    override: str | None = None,
) -> str:
    """Resolve the publish subject from descriptor config.

    Precedence:
      1. Explicit `override` (descriptor `outputs[*].config.nats_topic`).
      2. `analyst.<analyst_id>.<channel>` derived from descriptor identity
         and `outputs[*].config.channel` (defaults to "findings").

    Either `override` OR `analyst_id` must be supplied. The resulting
    subject is validated before return.
    """
    if override:
        _validate_subject(override)
        return override
    if not analyst_id:
        raise OutputSubjectError(
            "resolve_subject requires either an override or an analyst_id"
        )
    ch = channel or DEFAULT_CHANNEL
    # The channel comes from descriptor config; it must obey token grammar
    # since it's a single subject token. Same constraints as analyst_id.
    for field_name, value in (("analyst_id", analyst_id), ("channel", ch)):
        if not value or not isinstance(value, str):
            raise OutputSubjectError(f"{field_name!r} must be non-empty string")
        if any(c in value for c in (" ", "\t", "\n", ".", "*", ">", "\x00")):
            raise OutputSubjectError(
                f"{field_name!r} contains disallowed character: {value!r}"
            )
    subject = f"analyst.{analyst_id}.{ch}"
    _validate_subject(subject)
    return subject


def dlq_subject(analyst_id: str | None) -> str:
    """Dead-letter subject for an analyst whose nats_stream emit failed."""
    suffix = analyst_id if analyst_id else "_anonymous"
    # Same token grammar as analyst_id — sanitize aggressively.
    for c in (" ", "\t", "\n", ".", "*", ">", "\x00"):
        suffix = suffix.replace(c, "_")
    if not suffix:
        suffix = "_anonymous"
    return f"{DLQ_SUBJECT_PREFIX}.{suffix}"


# ---------------------------------------------------------------------------
# Deps protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class _NatsStoreLike(Protocol):
    """Structural slice of `legba.data.nats.NatsStore` we depend on."""

    @property
    def js(self) -> Any: ...  # nats.js.JetStreamContext


@dataclass(frozen=True)
class _ResolvedPublisher:
    """Internal wrapper over the resolved publish callable + analyst id."""

    publish: Any  # Callable[[str, bytes], Awaitable[None]] or .js.publish
    analyst_id: str | None
    # When True, the publisher is a `.js.publish`-style callable that may
    # raise nats-py errors. When False, it's the runtime-injected
    # `deps.nats_publish` closure that already wraps tracing/retry.
    is_js: bool


def _resolve_publisher(deps: Any) -> _ResolvedPublisher:
    """Pick a publish callable from the deps bundle. Errors are explicit."""
    analyst_id = getattr(deps, "analyst_id", None)

    # Path 1: deps.nats_publish closure (StandardDeps).
    nats_publish = getattr(deps, "nats_publish", None)
    if nats_publish is not None:
        return _ResolvedPublisher(
            publish=nats_publish, analyst_id=analyst_id, is_js=False,
        )

    # Path 2: deps.nats_store — use the JetStream context's publish.
    nats_store = getattr(deps, "nats_store", None)
    if nats_store is not None:
        # We don't enforce the Protocol at runtime because some test deps
        # use dataclasses; we just check `.js` is present.
        try:
            js = nats_store.js
        except RuntimeError as exc:
            raise OutputDepsError(
                f"deps.nats_store is not connected: {exc}"
            ) from exc
        return _ResolvedPublisher(
            publish=js.publish, analyst_id=analyst_id, is_js=True,
        )

    raise OutputDepsError(
        "deps must expose either `nats_publish` (Callable) or `nats_store` "
        "(NatsStore-like with .js); got neither"
    )


# ---------------------------------------------------------------------------
# Payload encoding
# ---------------------------------------------------------------------------


def _encode_payload(payload: Any) -> bytes:
    """JSON-encode the payload or raise OutputPayloadError.

    The contract says `payload: dict` — but we accept any Mapping for
    flexibility (pydantic .model_dump() returns dict; Mapping subclasses
    work too). Bytes/str pass through unchanged so callers that already
    encoded for a different transport don't pay twice.
    """
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if not isinstance(payload, Mapping):
        raise OutputPayloadError(
            f"payload must be a Mapping/dict (got {type(payload).__name__})"
        )
    try:
        return json.dumps(dict(payload), default=_json_default).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OutputPayloadError(
            f"payload is not JSON-serializable: {exc}"
        ) from exc


def _json_default(o: Any) -> Any:
    """Match the convention used by provenance.writes._json_default for UUID
    and datetime — but defined locally so we don't import a read-only sibling."""
    from datetime import date, datetime
    from uuid import UUID
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Publish helpers with retry + DLQ fallback
# ---------------------------------------------------------------------------


# Errors we treat as transient and retry. We intentionally use a small
# duck-typed set — nats-py raises different exception classes depending on
# version, and we don't want to import nats.errors at module top because
# the data package guards against `nats` being unavailable.
_TRANSIENT_ERROR_NAMES = frozenset(
    {
        "NoRespondersError",
        "TimeoutError",
        "ConnectionClosedError",
        "OutboundBufferLimitError",
        "FlushTimeoutError",
        "BadServerError",
        "StaleConnectionError",
    }
)


def _is_transient(exc: BaseException) -> bool:
    """Heuristic transient-error classifier."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    name = type(exc).__name__
    if name in _TRANSIENT_ERROR_NAMES:
        return True
    # nats-py 2.x bundles errors under `nats.errors.*` and raises
    # `Error` subclasses with `.description`. Fall back to module check
    # so we don't break on version drift.
    mod = type(exc).__module__ or ""
    if mod.startswith("nats."):
        return True
    return False


async def _await_if_awaitable(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


async def _publish_once(publisher: _ResolvedPublisher, subject: str, body: bytes) -> None:
    """One publish attempt. `publish` may be sync-returning-coro (StandardDeps
    nats_publish stores the coro) or directly awaitable (`.js.publish`)."""
    result = publisher.publish(subject, body)
    await _await_if_awaitable(result)


async def _publish_with_retry(
    publisher: _ResolvedPublisher,
    subject: str,
    body: bytes,
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> None:
    """Bounded retry loop. Re-raises the last transient error if exhausted;
    re-raises programmer errors immediately."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await _publish_once(publisher, subject, body)
            return
        except (OutputPayloadError, OutputSubjectError, OutputDepsError):
            raise
        except BaseException as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            logger.warning(
                "nats_stream.publish.transient_failure subject=%s attempt=%d/%d err=%s",
                subject, attempt, max_attempts, exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(backoff_seconds * attempt)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# emit — the L-191 output-kind entry point
# ---------------------------------------------------------------------------


def _resolve_subject_from_descriptor(descriptor: Any, ctx: Any) -> str:
    """Derive the publish subject for a ``nats_stream`` binding from the
    descriptor ``outputs[*].config`` block + the run context — the standard
    output-emit contract (mirrors how ``stix_bundle`` / ``alert`` read their
    own config). This lets the generic output dispatcher call
    ``emit(payload, descriptor=, ctx=, deps=)`` without pre-resolving the
    subject (the dispatcher passes ``descriptor``/``ctx``, never ``subject``).
    """
    cfg: dict[str, Any] = {}
    for b in (descriptor or {}).get("outputs", []) or []:
        if isinstance(b, dict) and b.get("kind") == "nats_stream":
            cfg = dict(b.get("config") or {})
            break
    return resolve_subject(
        analyst_id=getattr(ctx, "analyst_id", None) or None,
        channel=cfg.get("channel"),
        override=cfg.get("nats_topic"),
    )


async def emit(
    payload: Any,
    *,
    subject: str | None = None,
    deps: Any,
    descriptor: Any = None,
    ctx: Any = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
    dlq: bool = True,
) -> None:
    """Publish `payload` to `subject` via JetStream.

    Parameters
    ----------
    payload:
        JSON-serializable Mapping (typically `dict`). Bytes / str pass
        through. UUID and datetime fields are coerced via `_json_default`.
    subject:
        Fully-qualified NATS subject (validated). Optional: when omitted
        (the generic output dispatcher path) it is resolved from
        ``descriptor``/``ctx`` via :func:`resolve_subject`. Direct callers /
        tests pass it explicitly.
    descriptor / ctx:
        The standard output-emit contract inputs (the dispatcher passes a
        descriptor-shaped mapping + an OutputContext). Used to resolve the
        subject when ``subject`` is not given directly.
    deps:
        Object exposing either `nats_publish` (callable) or `nats_store`
        (NatsStore-like). Typically `legba.runtime.deps.StandardDeps`.
    max_attempts:
        Retry budget for transient NATS errors.
    backoff_seconds:
        Base backoff between attempts (multiplied by attempt index).
    dlq:
        If True (default), transient publish failures after retries are
        routed to `legba.dlq.output.nats_stream.<analyst_id>` instead of
        raising. The substrate write must not be blocked by NATS hiccups.
        If False, the last transient error is re-raised (useful for
        tests).
    """
    # 0) Contract alignment: the generic output dispatcher
    #    (dapr_actors._emit_output_bindings) calls every emit with
    #    descriptor/ctx/deps and never pre-resolves the subject. Resolve it
    #    here from the descriptor config when not supplied directly — without
    #    this, every nats_stream binding raised "emit() missing 1 required
    #    keyword-only argument: 'subject'" and never published.
    if subject is None:
        subject = _resolve_subject_from_descriptor(descriptor, ctx)
    # 1) Validate up-front. Programmer errors surface immediately.
    _validate_subject(subject)
    body = _encode_payload(payload)
    publisher = _resolve_publisher(deps)

    # 2) Attempt with bounded retry.
    try:
        await _publish_with_retry(
            publisher,
            subject,
            body,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        return
    except (OutputPayloadError, OutputSubjectError, OutputDepsError):
        # Programmer errors — never DLQ.
        raise
    except BaseException as exc:
        # Only transient errors are eligible for DLQ routing. Non-transient
        # errors (ValueError, TypeError, etc.) from a programmer bug must
        # surface — DLQ would silently swallow real defects.
        if not _is_transient(exc):
            raise
        if not dlq:
            raise
        # 3) Best-effort DLQ. We do NOT retry the DLQ publish — if NATS is
        #    truly down both attempts will fail, and we'd rather log loudly
        #    than spin.
        target = dlq_subject(publisher.analyst_id)
        logger.error(
            "nats_stream.publish.dlq_route subject=%s dlq=%s err=%s",
            subject, target, exc,
        )
        try:
            dlq_envelope = json.dumps(
                {
                    "original_subject": subject,
                    "analyst_id": publisher.analyst_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    # Echo the (already-encoded) body so a human can inspect.
                    # We base64-tolerate utf-8 by carrying the JSON string.
                    "payload_utf8": body.decode("utf-8", errors="replace"),
                },
                default=_json_default,
            ).encode("utf-8")
            await _publish_once(publisher, target, dlq_envelope)
        except Exception as dlq_exc:  # pragma: no cover
            logger.exception(
                "nats_stream.publish.dlq_failed subject=%s err=%s",
                target, dlq_exc,
            )


__all__ = [
    "KIND_NAME",
    "DEFAULT_CHANNEL",
    "DLQ_SUBJECT_PREFIX",
    "OutputPayloadError",
    "OutputSubjectError",
    "OutputDepsError",
    "resolve_subject",
    "dlq_subject",
    "emit",
]
