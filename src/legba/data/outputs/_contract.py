# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output-kind handler contract — minimal Protocol surface (L-197).

The output family is the third leaf of the kind taxonomy alongside
``sources`` (L-102 §2) and ``filters`` (L-102 §3). Where sources ingest
raw signals and filters transform them in-flight, *outputs* fan analyst-
emitted payloads to operator-facing surfaces (NATS, Pushover, XMPP,
Matrix, etc.).

This module declares:

  * :class:`OutputContext` — what an output handler receives at ``emit``:
    descriptor identity + secrets resolver + bound logger.
  * :class:`OutputDeps` — typed substrate ports (NATS publisher, HTTP
    client) the handler depends on. Tests inject fakes; the runtime
    (L-103) wires real ones.
  * :class:`OutputSurface` — descriptor-side surface descriptor (``name``
    + ``opt_in`` flag) used to override per-severity default routing.
  * :class:`SurfaceResult` — what a sub-sink returns after attempting
    delivery. Carries the surface name, outcome, and a transient/permanent
    flag so the parent kind can decide whether to retry.
  * :class:`AlertEmitter` — runtime-checkable Protocol for the kind.

All shapes are Pydantic / Protocol — no ABC inheritance — so external
plugin packages can register output kinds without importing a Legba base.
"""

from __future__ import annotations

import logging
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# OutputContext
# ---------------------------------------------------------------------------


SecretResolverFn = Callable[[str], Awaitable[str]]


class OutputContext(BaseModel):
    """Per-emit context handed to an output handler.

    Carries the descriptor identity + the secret resolver + a bound logger.
    Mirrors :class:`legba.data.sources._contract.SourceContext` and
    :class:`legba.data.filters._contract.FilterContext` in spirit, minus
    state_store (output sinks are stateless; transient retries are bounded
    to the emit call).

    ``secrets_resolve`` is an async callable ``(vault_id) -> str``. It is
    optional so unit tests can run without the full credentials stack;
    sinks fall back to treating the descriptor-side literal as the secret
    in that case.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    analyst_id: str = ""
    analyst_version: str = ""
    target_id: str = ""
    target_version: str = ""
    run_id: str | None = None
    # Identity of the persisted analyst_outputs row this emit is
    # delivering. Set by the dispatcher when the alert kind is invoked
    # downstream of ``write_alert``; the value is the analyst_outputs.id
    # primary key. When non-None and ``deps.pg_pool`` is wired, the alert
    # dispatcher INSERTs an ``alert_sink_deliveries`` row per attempt so
    # the future P-2 panel has structured delivery state to read.
    alert_row_id: str | None = None
    secrets_resolve: SecretResolverFn | None = Field(default=None, exclude=True)
    logger: logging.Logger = Field(
        default_factory=lambda: logging.getLogger("legba.output"),
        exclude=True,
    )


# ---------------------------------------------------------------------------
# Substrate ports
# ---------------------------------------------------------------------------


@runtime_checkable
class NatsPublisher(Protocol):
    """Minimal async NATS publisher surface used by output sinks.

    The L-001 NatsStore satisfies this surface via its ``publish_json``
    method. Tests pass a recording fake that captures (subject, payload)
    tuples for assertion.

    NOTE: this Protocol is ``@runtime_checkable`` AND used as a pydantic field
    type (``OutputDeps.nats``), so pydantic validates every assigned publisher
    via ``isinstance`` against it. Keep it to the ONE method every publisher
    (and every test fake) implements — ``publish_json``. The alert sink also
    needs ``publish_core`` (interest-only publish for the streamless
    ``legba.alerts.*`` subject — see :meth:`legba.data.nats.NatsStore.publish_core`),
    but it is NOT declared here: adding it would make every ``publish_json``-only
    fake fail the isinstance validation and silently degrade to ``nats=None``.
    The alert sink calls ``publish_core`` duck-typed; the real store has it.
    """

    async def publish_json(self, subject: str, payload: bytes) -> None: ...


@runtime_checkable
class HttpClientLike(Protocol):
    """Minimal async HTTP client port for sinks that need HTTPS (Pushover,
    Matrix). Structural subset of ``httpx.AsyncClient``.

    Tests inject a stub that records calls + returns canned responses; the
    runtime passes a real ``httpx.AsyncClient`` with appropriate timeouts.
    """

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any: ...


@runtime_checkable
class XmppPublisher(Protocol):
    """Minimal XMPP publisher port.

    The real implementation (behind the ``legba[xmpp]`` extra) bridges
    slixmpp; the protocol is intentionally tiny so sinks can be tested
    without the real client library.
    """

    async def send_message(self, to: str, body: str) -> None: ...


@runtime_checkable
class MatrixPublisher(Protocol):
    """Minimal Matrix publisher port (behind ``legba[matrix]`` extra).

    A real implementation wraps ``matrix-nio``; the protocol is kept
    small so tests don't pull the SDK into the unit suite.
    """

    async def send_message(self, room_id: str, body: str) -> None: ...


class OutputDeps(BaseModel):
    """Dependency bundle passed to ``emit``.

    All ports default to ``None`` so a descriptor that only routes through
    NATS (e.g. ``info`` severity) doesn't need to wire transports it never
    uses. A sink that needs a missing port records a structured skip in
    its :class:`SurfaceResult` rather than raising.

    ``pg_pool`` is the registry's asyncpg pool. When set, the alert
    dispatcher writes one ``alert_sink_deliveries`` row per sub-sink
    attempt (delivered / failed / retrying) so the P-2 alerts panel has
    backend rows to render. When unset (the historical Wave A path),
    the dispatcher logs as today and skips the INSERT — strictly back-
    compatible with existing callers; new bootstrap paths thread the pool
    via the analyst-deps builder in :mod:`legba.runtime.analyst_deps`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    nats: NatsPublisher | None = Field(default=None, exclude=True)
    http: HttpClientLike | None = Field(default=None, exclude=True)
    xmpp: XmppPublisher | None = Field(default=None, exclude=True)
    matrix: MatrixPublisher | None = Field(default=None, exclude=True)
    # Typed as Any so this module doesn't pull asyncpg into the import
    # graph for installs that don't use the alert delivery audit table.
    # arbitrary_types_allowed + exclude=True keeps the pool out of any
    # model_dump / JSON serialisation.
    pg_pool: Any | None = Field(default=None, exclude=True)


# ---------------------------------------------------------------------------
# Descriptor-side override shape
# ---------------------------------------------------------------------------


class OutputSurface(BaseModel):
    """Descriptor-side opt-in/opt-out for a named surface.

    Used inside ``outputs.alert.surfaces: [...]`` to override the default
    severity routing. ``mode`` semantics:

      * ``"on"`` — force the surface on for *every* severity at or above
        :attr:`min_severity`. Useful for ops who want a Slack-like
        firehose into Pushover regardless of the default `info → NATS-only`
        behavior.
      * ``"off"`` — force the surface off for every severity. Useful for
        descriptors that, e.g., never want Matrix even at ``critical``.
      * ``"default"`` — use the kind's severity ladder (no override). This
        is the same as omitting the surface entry entirely; it's allowed
        so YAML doesn't have to be diff-noisy when toggling.
    """

    model_config = ConfigDict(extra="forbid")

    name: Literal["nats", "pushover", "xmpp", "matrix"]
    mode: Literal["on", "off", "default"] = "default"
    min_severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    # Per-surface optional destination overrides (e.g. specific XMPP JID,
    # Matrix room id, Pushover user key). Sinks honor these when present.
    destination: str | None = None


# ---------------------------------------------------------------------------
# SurfaceResult
# ---------------------------------------------------------------------------


SurfaceOutcome = Literal["delivered", "skipped", "transient_error", "permanent_error"]


class SurfaceResult(BaseModel):
    """What a sub-sink returns after attempting (or skipping) delivery.

    The parent ``emit`` aggregates these and decides whether to retry
    (transient errors at ``critical`` severity) or surface a permanent
    failure.
    """

    model_config = ConfigDict(extra="forbid")

    surface: str
    outcome: SurfaceOutcome
    detail: str = ""

    @property
    def is_transient(self) -> bool:
        return self.outcome == "transient_error"

    @property
    def delivered(self) -> bool:
        return self.outcome == "delivered"


# ---------------------------------------------------------------------------
# AlertEmitter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AlertEmitter(Protocol):
    """Structural-typing surface for the ``alert`` output kind. L-197.

    A concrete emitter exposes:

      * ``KIND_NAME``: ClassVar[str] — registered kind name.
      * ``emit(payload, *, descriptor, deps) -> None``.

    Sub-sinks are private to the module (``alert_sinks/*``); the only
    public contract is :func:`emit`.
    """

    KIND_NAME: str

    async def emit(
        self,
        payload: Any,
        *,
        descriptor: Mapping[str, Any] | None,
        deps: OutputDeps,
    ) -> None: ...


__all__ = [
    "AlertEmitter",
    "HttpClientLike",
    "MatrixPublisher",
    "NatsPublisher",
    "OutputContext",
    "OutputDeps",
    "OutputSurface",
    "SecretResolverFn",
    "SurfaceOutcome",
    "SurfaceResult",
    "XmppPublisher",
]
