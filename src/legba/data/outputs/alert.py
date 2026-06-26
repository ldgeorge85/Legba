# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alert output kind — severity-aware fan-out (L-197).

The ``alert`` output kind takes an :class:`AlertPayload` produced by an
analyst and fans it out to operator-facing surfaces based on the payload's
severity. The intent — per the AlertPayload severity ladder in
:mod:`legba.data.provenance.models` — is:

  +----------+----------------------------------------------------------+
  | severity | default surfaces                                         |
  +==========+==========================================================+
  | info     | NATS (``legba.alerts.info``) — internal only             |
  +----------+----------------------------------------------------------+
  | low      | NATS + Pushover (optional, opt-in)                       |
  +----------+----------------------------------------------------------+
  | medium   | NATS + Pushover (default)                                |
  +----------+----------------------------------------------------------+
  | high     | NATS + Pushover + XMPP / Matrix (if extras installed)    |
  +----------+----------------------------------------------------------+
  | critical | All surfaces + immediate transient-retry on failure      |
  +----------+----------------------------------------------------------+

A descriptor opts surfaces in or out via::

    outputs:
      - kind: alert
        config:
          surfaces:
            - name: pushover
              mode: on
              min_severity: low
            - name: matrix
              mode: off
            - name: xmpp
              destination: ops@example.org

Surfaces with mode ``"default"`` (or omitted entirely) follow the
severity ladder above. Sinks that require an extra not installed
(``legba[xmpp]``, ``legba[matrix]``) record a ``skipped`` outcome with
``detail="extra-not-installed"`` and do not raise — keeping the kind
usable on minimal installs.

The retry behaviour at ``critical`` severity is intentionally local
(handler-scoped) so the substrate-write path remains untouched: the kind
write (``write_alert``) still happens once per AlertPayload; only the
*delivery side* retries. The runtime (L-103+) may layer outer durable
retries on top via Dapr actor reminders — that is L-110's territory, not
this kind's.

Per-attempt delivery audit (migration 0023)
-------------------------------------------
When the deps bundle carries a ``pg_pool`` and the ``OutputContext``
carries a ``alert_row_id`` (the persisted ``analyst_outputs.id`` for
this alert), the dispatcher writes one row per attempt to
``alert_sink_deliveries`` so the future P-2 panel and operator metrics
have structured backend state to read.

  * Successful first-attempt delivery → one row, ``status='delivered'``.
  * Permanent / transient-exhausted failure → one row, ``status='failed'``.
  * Critical-severity retry loop → ``status='retrying'`` per intermediate
    attempt + a single terminal ``delivered``/``failed`` row.
  * ``skipped`` sub-sinks (e.g. xmpp / matrix extras not installed) do
    not write a row — they did not attempt a delivery.

Existing callers that don't pass ``pg_pool`` (Wave A — every caller in
tree today) see pre-existing behaviour: the dispatcher logs delivery
outcomes and returns the :class:`SurfaceResult` list as before. The new
bootstrap path in :mod:`legba.runtime.analyst_deps` will thread the
registry's pool through ``OutputDeps.pg_pool`` and stamp the parent
``OutputContext.alert_row_id`` once the analyst-actor write path lands
the alert row.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID

from pydantic import ValidationError

from ..provenance.models import AlertPayload
from ._contract import (
    OutputContext,
    OutputDeps,
    OutputSurface,
    SurfaceResult,
)
from .alert_sinks.matrix import MATRIX_AVAILABLE, send_matrix_alert
from .alert_sinks.nats import send_nats_alert
from .alert_sinks.pushover import send_pushover_alert
from .alert_sinks.xmpp import XMPP_AVAILABLE, send_xmpp_alert


logger = logging.getLogger(__name__)


KIND_NAME = "alert"


# ---------------------------------------------------------------------------
# Default severity → surfaces matrix
# ---------------------------------------------------------------------------


Severity = Literal["info", "low", "medium", "high", "critical"]
SurfaceName = Literal["nats", "pushover", "xmpp", "matrix"]


#: Default per-severity surface set. Keys are severities; values are the
#: list of surfaces that fire by default at that severity. NATS is always
#: present so internal subscribers (UI, audit, downstream analysts) get a
#: consistent stream regardless of operator-facing routing.
DEFAULT_SURFACES: dict[Severity, tuple[SurfaceName, ...]] = {
    "info":     ("nats",),
    "low":      ("nats",),
    "medium":   ("nats", "pushover"),
    "high":     ("nats", "pushover", "xmpp", "matrix"),
    "critical": ("nats", "pushover", "xmpp", "matrix"),
}

#: Severities at which transient delivery failures trigger an immediate
#: retry loop (bounded by :data:`CRITICAL_RETRY_MAX_ATTEMPTS`).
RETRY_SEVERITIES: frozenset[Severity] = frozenset({"critical"})

#: How many extra attempts to make for retryable surfaces at critical
#: severity. The first attempt + this many retries = total tries.
CRITICAL_RETRY_MAX_ATTEMPTS = 3

#: Backoff (seconds) for critical retries. Tests inject a faster schedule
#: via ``descriptor["_retry_backoff"]`` to keep the suite fast.
CRITICAL_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5, 4.0)

#: NATS subject pattern. ``{severity}`` is substituted at emit time.
NATS_SUBJECT_PATTERN = "legba.alerts.{severity}"

#: Target-id values that are NEVER a real target. A run that resolves to one of
#: these is the D32 no-target tail (a country_assessor whose target failed to
#: thread, stringified to None/"unknown"), and its alert is skipped at the emit
#: path — an empty "" is left ALONE because a legitimate META analyst
#: (world_assessor) emits target-less alerts by design. To additionally suppress
#: an empty-target alert, a descriptor sets ``config.require_target: true``.
_NO_TARGET_ALERT_SENTINELS: frozenset[str] = frozenset({"unknown", "none", "null"})

#: Severities at which Pushover fires when its surface override is "default"
#: but the descriptor explicitly opts in for ``low``. The default ladder
#: leaves ``low`` Pushover-off; an operator who wants low+Pushover writes
#: ``- {name: pushover, mode: on, min_severity: low}``.
_PUSHOVER_DEFAULT_ON: frozenset[Severity] = frozenset({"medium", "high", "critical"})


# ---------------------------------------------------------------------------
# Sink table
# ---------------------------------------------------------------------------


async def _sink_nats(
    payload: AlertPayload,
    ctx: OutputContext,
    deps: OutputDeps,
    destination: str | None,
    descriptor: Mapping[str, Any] | None,
) -> SurfaceResult:
    subject = destination or NATS_SUBJECT_PATTERN.format(severity=payload.severity)
    return await send_nats_alert(
        payload, ctx=ctx, deps=deps, subject=subject
    )


async def _sink_pushover(
    payload: AlertPayload,
    ctx: OutputContext,
    deps: OutputDeps,
    destination: str | None,
    descriptor: Mapping[str, Any] | None,
) -> SurfaceResult:
    return await send_pushover_alert(
        payload, ctx=ctx, deps=deps, user_override=destination,
        descriptor=descriptor,
    )


async def _sink_xmpp(
    payload: AlertPayload,
    ctx: OutputContext,
    deps: OutputDeps,
    destination: str | None,
    descriptor: Mapping[str, Any] | None,
) -> SurfaceResult:
    if not XMPP_AVAILABLE and deps.xmpp is None:
        return SurfaceResult(
            surface="xmpp",
            outcome="skipped",
            detail="extra-not-installed",
        )
    return await send_xmpp_alert(
        payload, ctx=ctx, deps=deps, jid_override=destination,
        descriptor=descriptor,
    )


async def _sink_matrix(
    payload: AlertPayload,
    ctx: OutputContext,
    deps: OutputDeps,
    destination: str | None,
    descriptor: Mapping[str, Any] | None,
) -> SurfaceResult:
    if not MATRIX_AVAILABLE and deps.matrix is None:
        return SurfaceResult(
            surface="matrix",
            outcome="skipped",
            detail="extra-not-installed",
        )
    return await send_matrix_alert(
        payload, ctx=ctx, deps=deps, room_override=destination,
        descriptor=descriptor,
    )


_SINK_TABLE = {
    "nats":     _sink_nats,
    "pushover": _sink_pushover,
    "xmpp":     _sink_xmpp,
    "matrix":   _sink_matrix,
}


# ---------------------------------------------------------------------------
# Descriptor parsing
# ---------------------------------------------------------------------------


def _parse_surfaces(descriptor: Mapping[str, Any] | None) -> dict[str, OutputSurface]:
    """Parse descriptor['outputs']['alert']['surfaces'] into a name→Surface dict.

    Accepts either:
      * a list under ``surfaces`` of dicts each shaped like OutputSurface
      * a flat top-level mapping under ``surfaces``: {"pushover": {...}, ...}

    Unknown surface names are logged + ignored (forward-compat with new
    surfaces added in future kinds). Validation errors are raised loudly:
    a malformed descriptor is a deploy-time bug, not a runtime soft-fail.
    """
    if not descriptor:
        return {}

    block = descriptor
    # Accept three shapes:
    #   1. {"surfaces": [...]}                              (direct config)
    #   2. {"outputs": {"alert": {"surfaces": [...]}}}      (nested mapping)
    #   3. {"outputs": [{"kind": "alert", "config": {...}}]} (the runtime
    #      dispatcher's desc_map — outputs is a LIST of bindings)
    outputs = block.get("outputs") if isinstance(block, Mapping) else None
    if isinstance(outputs, list):
        # Pull the alert binding's config out of the list shape.
        block = _config_block(descriptor)
    elif isinstance(outputs, Mapping):
        block = outputs
    if isinstance(block, Mapping) and "alert" in block and isinstance(block["alert"], Mapping):
        block = block["alert"]

    surfaces_raw = block.get("surfaces") if isinstance(block, Mapping) else None
    if surfaces_raw is None:
        return {}

    out: dict[str, OutputSurface] = {}
    if isinstance(surfaces_raw, list):
        for entry in surfaces_raw:
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"alert.surfaces entry must be a mapping, got {type(entry).__name__}"
                )
            surface = OutputSurface.model_validate(entry)
            out[surface.name] = surface
    elif isinstance(surfaces_raw, Mapping):
        for name, entry in surfaces_raw.items():
            merged = {"name": name, **(entry if isinstance(entry, Mapping) else {})}
            surface = OutputSurface.model_validate(merged)
            out[surface.name] = surface
    else:
        raise ValueError(
            "alert.surfaces must be a list or mapping; got "
            f"{type(surfaces_raw).__name__}"
        )
    return out


_SEVERITY_ORDER: dict[Severity, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_ORDER_TO_SEVERITY: dict[int, Severity] = {v: k for k, v in _SEVERITY_ORDER.items()}

#: Default gate when a descriptor's ``outputs.alert.config`` omits one.
#: Only HIGH+ findings should page operators by default — info/low/medium
#: findings stay in the substrate. An operator widens the gate per-analyst.
_DEFAULT_MIN_SEVERITY: Severity = "high"


def _config_block(descriptor: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Pull the ``outputs.alert.config`` (or ``alert.config`` / ``config``)
    block out of whatever descriptor shape the caller passed.

    The runtime dispatcher (``dapr_actors._emit_output_bindings``) hands each
    binding a descriptor shaped ``{"outputs": [{"kind": "alert", "config":
    {...}}]}``. Tests pass narrower shapes. We tolerate all three and return
    an empty mapping when there is no alert binding.
    """
    if not isinstance(descriptor, Mapping):
        return {}
    outputs = descriptor.get("outputs")
    if isinstance(outputs, list):
        for entry in outputs:
            if isinstance(entry, Mapping) and entry.get("kind") == "alert":
                cfg = entry.get("config")
                return cfg if isinstance(cfg, Mapping) else {}
        return {}
    if isinstance(outputs, Mapping):
        alert_block = outputs.get("alert")
        if isinstance(alert_block, Mapping):
            cfg = alert_block.get("config", alert_block)
            return cfg if isinstance(cfg, Mapping) else {}
    cfg = descriptor.get("config")
    return cfg if isinstance(cfg, Mapping) else {}


def _confidence_to_severity(confidence: float) -> Severity:
    """Map a finding's [0,1] confidence to a coarse severity bucket.

    Used only when a non-alert payload (a FindingPayload from
    country_assessor / country_critic) carries no explicit
    ``data['severity']``. Conservative ladder — a finding needs strong
    confidence to reach ``high``/``critical`` and page an operator.
    """
    if confidence >= 0.9:
        return "critical"
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.5:
        return "medium"
    if confidence >= 0.25:
        return "low"
    return "info"


def _coerce_alert_payload(
    payload: Any,
    *,
    descriptor: Mapping[str, Any] | None,
) -> AlertPayload | None:
    """Coerce an arbitrary analyst payload into an :class:`AlertPayload`,
    gated by the descriptor's ``min_severity`` / ``min_confidence`` config.

    Returns ``None`` (no-op — caller emits nothing) when the payload's
    derived severity/confidence is below the gate, so binding ``alert`` to a
    high-volume analyst only pages on the findings that clear the bar.

    Resolution order for severity, mirroring ``_maybe_escalate_finding`` in
    the runtime (which reads ``payload.severity`` then ``payload.data[
    'severity']``):

      1. An already-typed :class:`AlertPayload` passes through (its own
         ``severity`` wins) — still subject to the gate.
      2. ``payload.data['severity']`` if the analyst stamped one.
      3. Else the confidence→severity ladder.
    """
    cfg = _config_block(descriptor)
    raw_min = str(cfg.get("min_severity", _DEFAULT_MIN_SEVERITY)).lower()
    min_severity: Severity = raw_min if raw_min in _SEVERITY_ORDER else _DEFAULT_MIN_SEVERITY  # type: ignore[assignment]
    min_confidence = cfg.get("min_confidence")

    # Already an AlertPayload — honour its own severity, still gate it.
    if isinstance(payload, AlertPayload):
        if _SEVERITY_ORDER[payload.severity] < _SEVERITY_ORDER[min_severity]:
            return None
        return payload

    title = getattr(payload, "title", None)
    if not title:
        return None  # nothing alert-worthy without a title
    body = getattr(payload, "body", "") or ""
    try:
        confidence = float(getattr(payload, "confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    data = getattr(payload, "data", None)
    data = data if isinstance(data, dict) else {}

    raw_sev = str(data.get("severity") or "").lower()
    if raw_sev in _SEVERITY_ORDER:
        severity: Severity = raw_sev  # type: ignore[assignment]
    else:
        severity = _confidence_to_severity(confidence)

    # Gate: below the configured severity floor → no alert.
    if _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[min_severity]:
        return None
    if min_confidence is not None:
        try:
            if confidence < float(min_confidence):
                return None
        except (TypeError, ValueError):
            pass

    tags: Sequence[Any] = getattr(payload, "tags", None) or []
    evidence: Sequence[Any] = getattr(payload, "evidence", None) or []
    return AlertPayload(
        title=str(title)[:2048],
        body=str(body)[:65536],
        confidence=max(0.0, min(1.0, confidence)),
        severity=severity,
        tags=[str(t)[:64] for t in tags][:50],
        evidence=[str(e) for e in evidence][:50],
        routing_hint=str(cfg.get("routing_hint", ""))[:256],
    )


def _resolve_surfaces(
    payload: AlertPayload,
    overrides: Mapping[str, OutputSurface],
) -> list[tuple[str, str | None]]:
    """Resolve the (surface_name, destination) list to actually emit on,
    honouring descriptor overrides on top of the default severity ladder.

    Algorithm:

      1. Start with ``DEFAULT_SURFACES[payload.severity]``.
      2. For each override:
         * ``mode="off"`` removes the surface unconditionally.
         * ``mode="on"`` adds the surface if the payload severity is at
           or above the override's ``min_severity``.
         * ``mode="default"`` is a no-op (placeholder for diff-stable yaml).
      3. Each surface gets its descriptor-specified destination (or None
         meaning "use sink default").

    The result is deduplicated while preserving first-seen order so
    log/audit output is stable.
    """
    severity = payload.severity
    sev_rank = _SEVERITY_ORDER[severity]

    selected: list[tuple[str, str | None]] = [
        (name, overrides[name].destination if name in overrides else None)
        for name in DEFAULT_SURFACES[severity]
    ]

    # Apply explicit on/off overrides.
    for name, surface in overrides.items():
        if surface.mode == "off":
            selected = [pair for pair in selected if pair[0] != name]
        elif surface.mode == "on":
            if sev_rank >= _SEVERITY_ORDER[surface.min_severity]:
                if not any(pair[0] == name for pair in selected):
                    selected.append((name, surface.destination))

    # Drop unknown surfaces (defensive — _parse_surfaces already validated).
    selected = [pair for pair in selected if pair[0] in _SINK_TABLE]

    return selected


# ---------------------------------------------------------------------------
# alert_sink_deliveries audit row writer (migration 0023)
# ---------------------------------------------------------------------------


def _result_to_status(result: SurfaceResult, more_attempts_queued: bool) -> str | None:
    """Map a :class:`SurfaceResult` outcome to a delivery-row status.

    * ``delivered``          → ``"delivered"``.
    * ``permanent_error``    → ``"failed"`` (no retry will be attempted).
    * ``transient_error``    → ``"retrying"`` if the dispatcher will try
                                again (critical-severity loop), else
                                ``"failed"``.
    * ``skipped``            → ``None`` — no delivery was attempted (e.g.
                                an extra is not installed); the task brief
                                says only attempted deliveries land rows.
    """
    outcome = result.outcome
    if outcome == "delivered":
        return "delivered"
    if outcome == "permanent_error":
        return "failed"
    if outcome == "transient_error":
        return "retrying" if more_attempts_queued else "failed"
    return None  # skipped


def _coerce_uuid(raw: Any) -> UUID | None:
    """Best-effort UUID coercion — None on failure so the audit row write
    degrades gracefully rather than killing the emit path."""
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _summarise_payload(
    payload: AlertPayload,
    surface_name: str,
    status: str,
    error_message: str | None,
) -> dict[str, Any]:
    """Compact JSONB summary stored alongside the delivery row.

    Kept narrow: severity + title + status are enough for the P-2 panel
    + operator metrics. Full payload bytes live on the parent
    ``analyst_outputs`` row already (joined via ``alert_row_id``).
    """
    return {
        "severity": payload.severity,
        "title": payload.title[:200],
        "tags": list(payload.tags),
        "routing_hint": payload.routing_hint,
        "surface": surface_name,
        "status": status,
        "error_detail": (error_message or "")[:500],
    }


async def _record_delivery(
    *,
    pg_pool: Any,
    ctx: OutputContext,
    payload: AlertPayload,
    surface_name: str,
    destination: str | None,
    attempt_number: int,
    status: str,
    error_message: str | None,
) -> None:
    """Write one ``alert_sink_deliveries`` row.

    Best-effort: any failure is logged but does NOT propagate — the audit
    row is observability, not correctness. The emit path must not die
    because a writer connection blipped.
    """
    alert_uuid = _coerce_uuid(ctx.alert_row_id)
    if alert_uuid is None:
        # No persisted parent row to FK against — skip silently.
        return

    delivered_at = datetime.now(tz=timezone.utc) if status == "delivered" else None
    summary = _summarise_payload(payload, surface_name, status, error_message)

    sql = """
        INSERT INTO alert_sink_deliveries (
            alert_row_id, descriptor_id, descriptor_version,
            sink_kind, sink_target, attempt_number, status,
            error_message, delivered_at, payload_summary
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
    """
    args = (
        alert_uuid,
        ctx.analyst_id or "",
        ctx.analyst_version or "",
        surface_name,
        destination,
        attempt_number,
        status,
        error_message,
        delivered_at,
        json.dumps(summary, separators=(",", ":")),
    )
    try:
        # asyncpg.Pool exposes ``execute``; tests + runtime both wire one.
        # If a raw connection is passed (e.g. a fixture), ``execute`` works
        # the same way — we duck-type instead of importing asyncpg here so
        # this module stays library-agnostic at type level.
        await pg_pool.execute(sql, *args)
    except Exception as exc:  # pragma: no cover - exercised via fault-injection
        ctx.logger.warning(
            "alert.delivery.audit_write_failed surface=%s status=%s err=%s",
            surface_name, status, exc,
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def emit(
    payload: AlertPayload | Mapping[str, Any] | Any,
    *,
    descriptor: Mapping[str, Any] | None = None,
    deps: OutputDeps,
    ctx: OutputContext | None = None,
    output_id: Any = None,
    derived_from: Sequence[Any] | None = None,
) -> list[SurfaceResult]:
    """Emit an :class:`AlertPayload` to all routed surfaces.

    Severity-based default routing plus descriptor opt-in/opt-out as
    documented at module level. Returns the per-surface
    :class:`SurfaceResult` list so callers can audit / re-route.

    **Runtime output-binding path (L-197 wiring).** The analyst-run dispatcher
    (``dapr_actors._emit_output_bindings``) calls every output kind's ``emit``
    with the LIVE analyst payload and the kwargs ``output_id`` /
    ``derived_from`` (the same shape ``stix_bundle.emit`` accepts). For the
    ``alert`` kind that live payload is usually a :class:`FindingPayload`
    (country_assessor / country_critic), not an :class:`AlertPayload`. We
    coerce it to an AlertPayload **gated by the descriptor's
    ``config.min_severity`` / ``config.min_confidence``** so binding ``alert``
    to a high-volume analyst only pages on findings that clear the bar.
    Sub-threshold findings short-circuit to ``[]`` (no surfaces fired).

    ``output_id`` (the persisted ``analyst_outputs.id``) is threaded onto
    ``ctx.alert_row_id`` when not already set, so the per-attempt
    ``alert_sink_deliveries`` audit rows FK against the parent finding when a
    ``deps.pg_pool`` is wired.

    Critical-severity alerts retry transient failures up to
    :data:`CRITICAL_RETRY_MAX_ATTEMPTS` times with the
    :data:`CRITICAL_RETRY_BACKOFF_SECONDS` schedule. Non-critical alerts
    record the transient outcome and move on — outer runtime layers
    (L-110 DLQ + Dapr reminders) are the durable retry surface.

    A mapping payload that cannot be coerced into :class:`AlertPayload`
    raises ``ValidationError`` — this is a contract bug, not a runtime
    soft-fail (the substrate already rejected it via ``write_alert``'s DLQ
    path).
    """
    if isinstance(payload, AlertPayload):
        coerced: AlertPayload | None = payload
    elif isinstance(payload, Mapping):
        # Back-compat: an explicit alert mapping (Wave A callers) validates
        # directly — no severity gate, the caller already decided to alert.
        coerced = AlertPayload.model_validate(payload)
    else:
        # Runtime output-binding path: a FindingPayload (or sibling) the
        # analyst produced. Coerce + severity-gate. None ⇒ below the gate.
        coerced = _coerce_alert_payload(payload, descriptor=descriptor)
        if coerced is None:
            return []
    payload = coerced

    out_ctx = ctx or OutputContext()
    if out_ctx.alert_row_id is None and output_id is not None:
        out_ctx = out_ctx.model_copy(update={"alert_row_id": str(output_id)})

    # D32 no-target emit gate. A run whose target failed to thread (the D4
    # contamination tail) emitted a NULL-target alert. Skip the sentinel
    # ("unknown"/"none"/"null") values outright — they are never a real target —
    # and skip an empty target when the descriptor opts in via
    # ``config.require_target``. A legitimately target-less META alert
    # (world_assessor, empty target, no ``require_target``) still fires.
    _tgt = str(out_ctx.target_id or "").strip()
    _cfg = _config_block(descriptor)
    _require_target = bool(_cfg.get("require_target")) if isinstance(_cfg, Mapping) else False
    if _tgt.casefold() in _NO_TARGET_ALERT_SENTINELS or (_require_target and not _tgt):
        out_ctx.logger.warning(
            "alert.emit.no_target_skip target=%r require_target=%s — refusing to "
            "emit a NULL/unknown-target alert (D32)",
            out_ctx.target_id, _require_target,
        )
        return []

    # Audit-row write is opt-in: needs both a pg_pool and a parent
    # analyst_outputs row id. When either is absent we keep the historical
    # Wave A behaviour (log-only, no INSERT). Computed BEFORE _parse_surfaces so
    # a malformed descriptor (e.g. the YAML-1.1 `mode: on`→bool trap that left
    # alert_sink_deliveries empty) is AUDITED as a delivery error rather than
    # swallowed — a silent parse crash that never writes a row is exactly how
    # "0 deliveries, 0 attempts" hid the alert pipeline being dead (K-1).
    pg_pool = getattr(deps, "pg_pool", None)
    audit_enabled = pg_pool is not None and out_ctx.alert_row_id is not None

    try:
        overrides = _parse_surfaces(descriptor)
    except (ValidationError, ValueError) as err:
        out_ctx.logger.error(
            "alert.emit: descriptor surface overrides invalid: %s", err
        )
        if audit_enabled:
            # Fail LOUD + audited: leave an operator-visible error row instead
            # of an empty table. "_descriptor" sentinel (sink_kind is NOT NULL);
            # _record_delivery is itself best-effort so this can't break the run.
            await _record_delivery(
                pg_pool=pg_pool,
                ctx=out_ctx,
                payload=payload,
                surface_name="_descriptor",
                destination=None,
                attempt_number=1,
                status="error",
                error_message=str(err)[:2000],
            )
        raise

    targets = _resolve_surfaces(payload, overrides)

    backoff = (
        tuple(descriptor.get("_retry_backoff"))
        if descriptor and isinstance(descriptor.get("_retry_backoff"), (list, tuple))
        else CRITICAL_RETRY_BACKOFF_SECONDS
    )

    results: list[SurfaceResult] = []
    do_retry = payload.severity in RETRY_SEVERITIES

    for surface_name, destination in targets:
        sink = _SINK_TABLE[surface_name]
        result = await sink(payload, out_ctx, deps, destination, descriptor)
        attempt_number = 1

        if do_retry and result.is_transient:
            # Bounded retry loop. Sleep gates are explicit so tests can
            # shrink them via the ``_retry_backoff`` descriptor knob.
            attempts_remaining = CRITICAL_RETRY_MAX_ATTEMPTS
            attempt_idx = 0
            while attempts_remaining > 0 and result.is_transient:
                # Audit the just-finished transient attempt as 'retrying' —
                # we know another attempt is queued because the while guard
                # holds.
                if audit_enabled:
                    await _record_delivery(
                        pg_pool=pg_pool,
                        ctx=out_ctx,
                        payload=payload,
                        surface_name=surface_name,
                        destination=destination,
                        attempt_number=attempt_number,
                        status="retrying",
                        error_message=result.detail or None,
                    )
                delay = backoff[min(attempt_idx, len(backoff) - 1)] if backoff else 0.0
                if delay > 0:
                    await asyncio.sleep(delay)
                result = await sink(payload, out_ctx, deps, destination, descriptor)
                attempts_remaining -= 1
                attempt_idx += 1
                attempt_number += 1
            out_ctx.logger.info(
                "alert.emit: critical retry surface=%s final_outcome=%s",
                surface_name,
                result.outcome,
            )

        # Record the terminal-state row for this surface (delivered /
        # failed). Skipped sinks (extras-not-installed) write no row —
        # the brief says only attempted deliveries are audited.
        if audit_enabled:
            final_status = _result_to_status(result, more_attempts_queued=False)
            if final_status is not None:
                await _record_delivery(
                    pg_pool=pg_pool,
                    ctx=out_ctx,
                    payload=payload,
                    surface_name=surface_name,
                    destination=destination,
                    attempt_number=attempt_number,
                    status=final_status,
                    error_message=result.detail or None,
                )

        results.append(result)

    out_ctx.logger.info(
        "alert.emit severity=%s surfaces=%s outcomes=%s",
        payload.severity,
        [r.surface for r in results],
        [r.outcome for r in results],
    )
    return results


# ---------------------------------------------------------------------------
# Helpers exposed for testing
# ---------------------------------------------------------------------------


def _payload_envelope(payload: AlertPayload, ctx: OutputContext) -> dict[str, Any]:
    """Serialisable envelope shared across HTTPS sinks.

    Kept out of the sink modules to avoid duplicating the analyst/run/
    target identity wiring; sinks call this to build their request body
    on top of the payload itself.
    """
    return {
        "kind": KIND_NAME,
        "severity": payload.severity,
        "title": payload.title,
        "body": payload.body,
        "confidence": payload.confidence,
        "tags": list(payload.tags),
        "evidence": list(payload.evidence),
        "routing_hint": payload.routing_hint,
        "analyst_id": ctx.analyst_id,
        "analyst_version": ctx.analyst_version,
        "target_id": ctx.target_id,
        "target_version": ctx.target_version,
        "run_id": ctx.run_id,
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }


__all__ = [
    "CRITICAL_RETRY_BACKOFF_SECONDS",
    "CRITICAL_RETRY_MAX_ATTEMPTS",
    "DEFAULT_SURFACES",
    "KIND_NAME",
    "NATS_SUBJECT_PATTERN",
    "RETRY_SEVERITIES",
    "Severity",
    "SurfaceName",
    "emit",
]
