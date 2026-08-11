# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modular alert-sink interface + dispatcher (P1-1, verification-gated alerting).

The internal alert edges already write durable ``alert_sink_deliveries``
rows — the agency :class:`~legba.data.analysts.agency.tools.ChannelEmitter`
(escalate / create_incident, migration 0061) and the liveness watchdog's
global-stall recorder (B0-12) — but until this module nothing pushed those
alerts OUTWARD. This module is the outward fan-out plane:

Anatomy (one converged payload)
-------------------------------
:class:`AlertSinkPayload` is THE alert shape every sink receives: summary,
severity, target/geo, source links (canonical URLs of the finding's cited
signals when resolvable), the verify state (the faithfulness score or an
explicit ``unverified — <reason>``), the effective (verify-folded)
confidence, event + detection timestamps, and a receipt link into the
lineage API (``GET /api/v1/lineage/{kind}/{id}``) so every outward page
carries its own provenance handle.

Sink protocol + registry
------------------------
:class:`AlertSink` is structural (Protocol) — a sink implements
``sink_kind`` / ``configured`` / ``target_summary()`` /
``accepts_severity()`` / ``deliver()``. Sinks register a zero-arg factory
via :func:`register_alert_sink` keyed by ``sink_kind`` (mirrors the
output-kind registry in :mod:`legba.data.outputs` and the agency
``ToolRegistry``). :func:`build_registered_sinks` instantiates every
factory — a broken factory is logged loudly and skipped, never allowed to
kill runtime bring-up.

Dispatcher (the shared machinery)
---------------------------------
:class:`AlertSinkDispatcher` owns the cross-cutting concerns so a sink
stays a dumb transport:

* **Durable ledger row per sink outcome.** Every fan-out lands one
  ``alert_sink_deliveries`` row per sink (``sink_kind`` = the sink's kind,
  ``sink_target`` REDACTED to host only): ``delivered`` / ``failed`` on a
  real attempt, ``skipped_unconfigured`` when the sink is declared-inactive
  (env not set — the gap stays VISIBLE, never a silent drop), and
  ``skipped_cooldown`` when the anti-noise gate suppressed a would-be POST.
  A below-``min_severity`` alert writes NO row (a configured filter working
  as designed is not a delivery event).
* **Per-alert-row idempotency.** ONE outward attempt series per
  ``alert_row_id`` — a finding escalated on two pack channels POSTs once.
  In-process (bounded LRU set); a restart may re-attempt, which is the
  right failure direction for an alerting path.
* **Global per-sink cooldown.** At most one outward POST per sink per
  ``cooldown_seconds`` (default 60s), in-process. A suppressed alert is
  still durably visible as its ``skipped_cooldown`` ledger row. Trigger-set
  shaping beyond this minimum is P1-3's territory.
* **Never raises.** The alert path (escalation emit, watchdog loop) must
  not die because an outward sink misbehaved — every failure degrades to a
  structured :class:`DeliveryResult` + a warning log + a ``failed`` row.

Configuration
-------------
Sinks are env-configured (the webhook sink reads
``LEGBA_ALERT_WEBHOOK_URL`` — see :mod:`.webhook_sink`). The receipt link
base comes from ``LEGBA_PUBLIC_BASE_URL`` when set; the payload always
carries the relative lineage API path regardless.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity ladder (local copy — importing outputs.alert would drag the
# pushover/xmpp/matrix transports into this module's import graph)
# ---------------------------------------------------------------------------


Severity = Literal["info", "low", "medium", "high", "critical"]

SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def normalise_severity(raw: Any, *, default: str = "info") -> str:
    """Coerce an arbitrary severity value onto the ladder (defensive)."""
    value = str(raw or "").strip().lower()
    return value if value in SEVERITY_ORDER else default


# ---------------------------------------------------------------------------
# Receipt link (lineage deep-link)
# ---------------------------------------------------------------------------


#: Where the SPA / API resolves a row's provenance walk (lineage_api.py).
LINEAGE_API_PATH_TEMPLATE = "/api/v1/lineage/{row_kind}/{row_id}"

#: Optional absolute base for outward links (e.g. ``https://legba.example.org``).
#: Unset ⇒ payloads carry only the relative API path.
ENV_PUBLIC_BASE_URL = "LEGBA_PUBLIC_BASE_URL"


def receipt_link(
    row_id: Any,
    *,
    row_kind: str = "finding",
    base_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Build the (relative_path, absolute_url_or_None) receipt pair.

    ``row_kind`` must be one of the lineage API's supported kinds; the
    escalation edge always passes findings (``analyst_outputs`` rows).
    ``base_url`` defaults to env ``LEGBA_PUBLIC_BASE_URL``; when absent the
    absolute URL is None and consumers use the relative path against
    whatever host they already talk to.
    """
    if row_id is None or not str(row_id).strip():
        return None, None
    path = LINEAGE_API_PATH_TEMPLATE.format(row_kind=row_kind, row_id=row_id)
    base = (base_url if base_url is not None else os.environ.get(ENV_PUBLIC_BASE_URL, "")).strip()
    if not base:
        return path, None
    return path, base.rstrip("/") + path


def redact_url_to_host(url: str | None) -> str:
    """Reduce a sink URL to its host for ledger rows + logs.

    A webhook URL routinely embeds a secret (Slack/Discord/ntfy token in
    the path); the durable ledger and the logs must never carry it. Host
    only — no scheme, no path, no query.
    """
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Payload — the converged alert anatomy
# ---------------------------------------------------------------------------


class AlertSinkPayload(BaseModel):
    """The one alert shape every outward sink receives (and POSTs).

    Assembled by :meth:`AlertSinkDispatcher.payload_for_finding` (escalation
    edge, DB-enriched) or :func:`runtime_alert_payload` (deterministic
    runtime alerts — e.g. the global pipeline-stall watchdog).
    """

    model_config = ConfigDict(extra="forbid")

    schema_uri: str = "iglu:legba/alert_sink_payload/jsonschema/1-0-0"

    #: One-line human summary (the escalation/finding title).
    summary: str
    #: Longer body/detail (bounded upstream).
    detail: str = ""
    severity: str = "info"
    #: Originating internal channel ('escalations', 'liveness_stall', ...).
    channel_name: str = ""
    #: The finding's target (country code) when target-bound.
    target_id: str | None = None
    #: Geo hints aggregated from the cited signals (best-effort).
    geo: tuple[str, ...] = ()
    #: canonical_urls of the finding's cited signals, when resolvable.
    source_links: tuple[str, ...] = ()
    #: Verify-FOLDED effective confidence the escalate gate crossed.
    effective_confidence: float | None = None
    #: ``"faithfulness=<score>"`` when the verify pass produced a verdict,
    #: else an explicit ``"unverified — <reason>"``. Never empty: an outward
    #: alert always states its verification posture.
    verify_state: str
    #: When the underlying finding/event was produced (None when unknown).
    event_at: datetime | None = None
    #: When THIS alert fired (detection time).
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    #: The persisted ``analyst_outputs.id`` (str) — the receipt anchor.
    alert_row_id: str | None = None
    #: Relative lineage API path (``/api/v1/lineage/finding/<id>``).
    receipt_path: str | None = None
    #: Absolute receipt link when ``LEGBA_PUBLIC_BASE_URL`` is set.
    receipt_url: str | None = None
    #: Cooldown coalescing (anti-noise honesty): when the dispatcher's per-sink
    #: cooldown suppressed alerts since the previous outward POST, the NEXT
    #: allowed POST carries their count + a bounded preview so a burst is
    #: DISTILLED into the notification instead of silently thinned (the
    #: suppressed alerts' own ledger rows stay ``skipped_cooldown``).
    suppressed_in_cooldown: int = 0
    suppressed_preview: tuple[str, ...] = ()


def unverified_state(reason: str) -> str:
    """The canonical ``unverified`` verify-state string."""
    return f"unverified — {reason}"


#: The canonical ``unassessable`` verify-state string (Q-1). NOT ``unverified``:
#: the verify pass ran, it simply extracted nothing gradeable, and collapsing the
#: two would tell an operator no pass had happened when one had.
UNASSESSABLE_STATE = "unassessable — verify ran but extracted no gradeable claim"


def verify_state_from_score(
    faithfulness_score: Any,
    *,
    unverified_reason: str = "no faithfulness verdict recorded for this finding",
    score_state: Any = None,
    provisional: Any = None,
) -> str:
    """Fold an optional faithfulness score into the payload verify state.

    Q-1: three states reach an operator's notification, not two.

    * ``faithfulness=0.82`` — a real, adjudicated measurement;
    * ``faithfulness=0.82 (provisional — deterministic floor, no LLM judge)`` —
      a real measurement no grader confirmed;
    * ``unassessable — …`` — no measurement at all.

    ``score_state`` / ``provisional`` are OPTIONAL and default to ``None``, so
    every existing caller that passes only a score is byte-identical.
    """
    if str(score_state or "") == "unassessable":
        return UNASSESSABLE_STATE
    if isinstance(faithfulness_score, (int, float)) and not isinstance(
        faithfulness_score, bool
    ):
        state = f"faithfulness={float(faithfulness_score):.2f}"
        if provisional:
            state += " (provisional — deterministic floor, no LLM judge)"
        return state
    return unverified_state(unverified_reason)


def runtime_alert_payload(
    *,
    channel_name: str,
    summary: str,
    detail: str = "",
    severity: str = "high",
    target_id: str | None = None,
    unverified_reason: str = (
        "deterministic runtime alert (no LLM content; not subject to the "
        "faithfulness verify pass)"
    ),
) -> AlertSinkPayload:
    """Payload for a deterministic runtime alert (watchdog stall etc.).

    No finding row, no cited signals, no receipt anchor — the verify state
    is an explicit ``unverified — <reason>`` naming why (deterministic
    detector output, not LLM prose).
    """
    return AlertSinkPayload(
        summary=summary[:512],
        detail=detail[:4000],
        severity=normalise_severity(severity, default="high"),
        channel_name=channel_name,
        target_id=target_id,
        verify_state=unverified_state(unverified_reason),
    )


# ---------------------------------------------------------------------------
# Delivery result
# ---------------------------------------------------------------------------


Outcome = Literal[
    "delivered",
    "transient_error",
    "permanent_error",
    "skipped_unconfigured",
    "skipped_below_severity",
    "skipped_cooldown",
    "skipped_duplicate",
]


class DeliveryResult(BaseModel):
    """Structured outcome of one sink's handling of one alert.

    Mirrors :class:`legba.data.outputs.taxii_client.TaxiiPushResult` —
    classification, never an exception surface.
    """

    model_config = ConfigDict(extra="forbid")

    sink_kind: str
    outcome: Outcome
    http_status: int | None = None
    attempts: int = 0
    #: REDACTED destination (host only) — safe for ledger rows + logs.
    target: str = ""
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.outcome == "delivered"

    @property
    def attempted(self) -> bool:
        """True when the sink actually tried an outward delivery."""
        return self.outcome in ("delivered", "transient_error", "permanent_error")


# ---------------------------------------------------------------------------
# Sink protocol + registry
# ---------------------------------------------------------------------------


@runtime_checkable
class AlertSink(Protocol):
    """Structural surface every outward alert sink implements.

    ``deliver`` MUST NOT raise — it classifies every failure into a
    :class:`DeliveryResult` (the taxii/webhook outbound-HTTP convention).
    ``configured`` / ``accepts_severity`` are cheap pre-flight questions the
    dispatcher asks so IT can own the ledger + anti-noise policy without
    the sink duplicating them.
    """

    @property
    def sink_kind(self) -> str: ...

    @property
    def configured(self) -> bool: ...

    def target_summary(self) -> str:
        """Redacted destination (host only) for ledger rows + logs."""
        ...

    def accepts_severity(self, severity: str) -> bool: ...

    async def deliver(self, payload: AlertSinkPayload) -> DeliveryResult: ...


AlertSinkFactory = Callable[[], AlertSink]

_SINK_FACTORIES: dict[str, AlertSinkFactory] = {}


def register_alert_sink(
    sink_kind: str, factory: AlertSinkFactory, *, replace: bool = False
) -> None:
    """Register a sink factory under ``sink_kind``.

    Duplicate registration is a programmer error (raises) unless
    ``replace=True`` — tests replace the webhook factory with fakes.
    """
    if not replace and sink_kind in _SINK_FACTORIES:
        raise ValueError(
            f"alert sink kind {sink_kind!r} is already registered; "
            "pass replace=True to override (tests only)"
        )
    _SINK_FACTORIES[sink_kind] = factory


def registered_sink_kinds() -> tuple[str, ...]:
    return tuple(sorted(_SINK_FACTORIES))


def build_registered_sinks() -> list[AlertSink]:
    """Instantiate every registered factory.

    A factory that raises is logged LOUDLY and skipped — a broken sink
    module must not kill runtime bring-up; the missing sink is visible in
    the startup configuration log and the absent ledger rows.
    """
    sinks: list[AlertSink] = []
    for kind in sorted(_SINK_FACTORIES):
        try:
            sinks.append(_SINK_FACTORIES[kind]())
        except Exception as exc:  # pragma: no cover — defensive boot guard
            logger.error(
                "alert_sinks.factory_failed sink_kind=%s err=%s — sink "
                "DISABLED this process", kind, exc,
            )
    return sinks


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


#: Bound on the in-process idempotency set (alert_row_ids already fanned out).
_IDEMPOTENCY_MAX_ENTRIES = 4096

#: Default global per-sink cooldown between outward POSTs (seconds).
DEFAULT_COOLDOWN_SECONDS = 60.0

#: Cap on coalesced-preview lines carried onto the next allowed POST.
_SUPPRESSED_PREVIEW_MAX = 5


class AlertSinkDispatcher:
    """Fans one alert out across every registered sink, with ledger audit.

    Construction logs each sink's configuration state ONCE (the
    "webhook sink not configured" declared-inactive line), so an operator
    reading the boot log knows whether outward alerting is live.

    ``pg_pool`` is duck-typed (``execute`` + ``fetchrow`` + ``fetch`` — an
    asyncpg Pool or a test fake). ``None`` ⇒ no ledger rows (unit rigs),
    fan-out still runs.
    """

    def __init__(
        self,
        *,
        pg_pool: Any | None = None,
        sinks: list[AlertSink] | None = None,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pg_pool = pg_pool
        all_sinks: list[AlertSink] = (
            list(sinks) if sinks is not None else build_registered_sinks()
        )
        # Unconfigured-sink noise gate (operator feedback 07-28): when at least
        # ONE sink is configured, unconfigured siblings drop out of the fan-out
        # entirely — their per-alert ``skipped_unconfigured`` rows are pure
        # noise once alerts genuinely have somewhere to go. When NO sink is
        # configured the P1-1 visibility guarantee stands unchanged: every
        # registered-but-unconfigured sink keeps writing its skip row so the
        # "alerts have nowhere to go" gap can never be silent.
        configured = [s for s in all_sinks if s.configured]
        self._sinks: list[AlertSink] = configured if configured else all_sinks
        # Cooldown: ctor arg wins; else the env knob; else the 60s default.
        env_cd = os.getenv("LEGBA_ALERT_SINK_COOLDOWN_SECONDS", "").strip()
        if cooldown_seconds == DEFAULT_COOLDOWN_SECONDS and env_cd:
            try:
                cooldown_seconds = float(env_cd)
            except ValueError:
                logger.warning(
                    "alert_sinks.bad_cooldown_env value=%r — using default",
                    env_cd,
                )
        self._cooldown_s = float(cooldown_seconds)
        self._monotonic = monotonic
        #: sink_kind → monotonic time of the last outward POST attempt.
        self._last_post_at: dict[str, float] = {}
        #: sink_kind → (count, bounded previews) suppressed by cooldown since
        #: that sink's last ALLOWED post — coalesced onto the next one.
        self._suppressed: dict[str, tuple[int, list[str]]] = {}
        #: Bounded LRU set of alert_row_ids already given an attempt series.
        self._attempted: OrderedDict[str, None] = OrderedDict()
        self._log_configuration()

    # -- configuration visibility ---------------------------------------

    @property
    def sinks(self) -> tuple[AlertSink, ...]:
        return tuple(self._sinks)

    def _log_configuration(self) -> None:
        if not self._sinks:
            logger.warning(
                "alert_sinks.none_registered — no outward alert sinks exist "
                "this build; alerts stay internal (ledger + NATS only)"
            )
            return
        for sink in self._sinks:
            if sink.configured:
                logger.info(
                    "alert_sinks.configured sink_kind=%s target=%s",
                    sink.sink_kind, sink.target_summary(),
                )
            else:
                logger.warning(
                    "alert_sinks.not_configured sink_kind=%s — %s sink not "
                    "configured; outward delivery INACTIVE (deliveries will "
                    "record status=skipped_unconfigured so the gap stays "
                    "visible)", sink.sink_kind, sink.sink_kind,
                )

    # -- payload assembly (escalation edge, DB-enriched) ----------------

    async def payload_for_finding(
        self,
        *,
        channel_name: str,
        alert_row_id: Any,
        target_id: str | None,
        severity: Any,
        effective_confidence: Any,
        title: str,
        detail: str = "",
        faithfulness_score: Any = None,
    ) -> AlertSinkPayload:
        """Assemble the converged payload for a persisted finding.

        Best-effort DB enrichment (never raises): the finding row supplies
        ``event_at`` (produced_at) + ``derived_from``; the cited signals
        supply ``source_links`` (canonical_url) + ``geo``. Enrichment
        failures degrade to an un-enriched payload — an outward page with
        fewer links beats no page.
        """
        conf: float | None
        try:
            conf = float(effective_confidence) if effective_confidence is not None else None
        except (TypeError, ValueError):
            conf = None

        row_id_str = str(alert_row_id) if alert_row_id is not None else None
        event_at, source_links, geo = await self._enrich_from_finding(row_id_str)
        path, url = receipt_link(row_id_str, row_kind="finding")

        return AlertSinkPayload(
            summary=str(title or "")[:512],
            detail=str(detail or "")[:4000],
            severity=normalise_severity(severity, default="info"),
            channel_name=channel_name,
            target_id=target_id,
            geo=geo,
            source_links=source_links,
            effective_confidence=conf,
            verify_state=verify_state_from_score(faithfulness_score),
            event_at=event_at,
            alert_row_id=row_id_str,
            receipt_path=path,
            receipt_url=url,
        )

    async def _enrich_from_finding(
        self, alert_row_id: str | None
    ) -> tuple[datetime | None, tuple[str, ...], tuple[str, ...]]:
        """(event_at, source_links, geo) from the finding + its cited signals.

        Bounded (LIMIT 25 signals; ≤5 links; ≤10 geo) and best-effort — any
        DB error logs a warning and returns empties.
        """
        if self._pg_pool is None or not alert_row_id:
            return None, (), ()
        try:
            row_uuid = UUID(alert_row_id)
        except (ValueError, TypeError, AttributeError):
            return None, (), ()
        try:
            finding = await self._pg_pool.fetchrow(
                "SELECT produced_at, derived_from FROM analyst_outputs "
                "WHERE id = $1",
                row_uuid,
            )
            if finding is None:
                return None, (), ()
            event_at = finding["produced_at"]
            parents = list(finding["derived_from"] or [])
            if not parents:
                return event_at, (), ()
            signal_rows = await self._pg_pool.fetch(
                "SELECT canonical_url, geo FROM signals "
                "WHERE id = ANY($1::uuid[]) LIMIT 25",
                parents,
            )
        except Exception as exc:
            logger.warning(
                "alert_sinks.enrich_failed alert_row_id=%s err=%s "
                "(payload goes out un-enriched)", alert_row_id, exc,
            )
            return None, (), ()

        links: list[str] = []
        geo: list[str] = []
        for srow in signal_rows or []:
            url = srow["canonical_url"]
            if url and url not in links and len(links) < 5:
                links.append(str(url))
            for g in srow["geo"] or []:
                if g and g not in geo and len(geo) < 10:
                    geo.append(str(g))
        return event_at, tuple(links), tuple(geo)

    # -- fan-out ---------------------------------------------------------

    async def fan_out(self, payload: AlertSinkPayload) -> list[DeliveryResult]:
        """Deliver ``payload`` through every sink; one ledger row per outcome.

        NEVER raises — the alert path (escalation emit, watchdog loop) is
        load-bearing and an outward sink is best-effort by contract.
        """
        try:
            return await self._fan_out_inner(payload)
        except Exception as exc:  # pragma: no cover — belt over sink guards
            logger.error(
                "alert_sinks.fan_out_failed channel=%s err=%s",
                payload.channel_name, exc,
            )
            return []

    async def _fan_out_inner(self, payload: AlertSinkPayload) -> list[DeliveryResult]:
        if not self._sinks:
            return []

        # Per-alert-row idempotency: ONE attempt series per alert_row_id.
        # (A finding escalated on two pack channels reaches fan_out twice.)
        if payload.alert_row_id is not None:
            if payload.alert_row_id in self._attempted:
                logger.debug(
                    "alert_sinks.duplicate_suppressed alert_row_id=%s",
                    payload.alert_row_id,
                )
                return [
                    DeliveryResult(
                        sink_kind=sink.sink_kind,
                        outcome="skipped_duplicate",
                        target=sink.target_summary(),
                    )
                    for sink in self._sinks
                ]
            self._attempted[payload.alert_row_id] = None
            while len(self._attempted) > _IDEMPOTENCY_MAX_ENTRIES:
                self._attempted.popitem(last=False)

        results: list[DeliveryResult] = []
        for sink in self._sinks:
            result = await self._deliver_one(sink, payload)
            results.append(result)
            await self._record_ledger_row(sink, payload, result)
        return results

    async def _deliver_one(
        self, sink: AlertSink, payload: AlertSinkPayload
    ) -> DeliveryResult:
        kind = sink.sink_kind
        try:
            if not sink.configured:
                return DeliveryResult(
                    sink_kind=kind,
                    outcome="skipped_unconfigured",
                    detail=f"{kind} sink not configured",
                )
            if not sink.accepts_severity(payload.severity):
                return DeliveryResult(
                    sink_kind=kind,
                    outcome="skipped_below_severity",
                    target=sink.target_summary(),
                    detail=f"severity {payload.severity!r} below sink floor",
                )
            # Global per-sink cooldown between outward POSTs (anti-noise
            # minimum; in-process by design — documented module-level).
            now = self._monotonic()
            last = self._last_post_at.get(kind)
            if last is not None and (now - last) < self._cooldown_s:
                # Coalesce, don't just drop: remember this alert so the NEXT
                # allowed POST carries "+N suppressed" with a bounded preview
                # (a burst is distilled, never silently thinned).
                count, previews = self._suppressed.get(kind, (0, []))
                if len(previews) < _SUPPRESSED_PREVIEW_MAX:
                    previews.append(
                        f"{payload.severity} · "
                        f"{payload.target_id or payload.channel_name or '-'} · "
                        f"{(payload.summary or '')[:80]}"
                    )
                self._suppressed[kind] = (count + 1, previews)
                return DeliveryResult(
                    sink_kind=kind,
                    outcome="skipped_cooldown",
                    target=sink.target_summary(),
                    detail=(
                        f"cooldown: last POST {now - last:.1f}s ago "
                        f"(< {self._cooldown_s:.0f}s); coalesced onto next send"
                    ),
                )
            self._last_post_at[kind] = now
            # Attach any cooldown-suppressed backlog to this outgoing payload.
            suppressed = self._suppressed.pop(kind, None)
            if suppressed is not None and suppressed[0] > 0:
                payload = payload.model_copy(
                    update={
                        "suppressed_in_cooldown": suppressed[0],
                        "suppressed_preview": tuple(suppressed[1]),
                    }
                )
            result = await sink.deliver(payload)
            return result
        except Exception as exc:
            # A sink violating its never-raise contract still must not kill
            # the alert path — classify as failed and keep going.
            logger.error(
                "alert_sinks.sink_raised sink_kind=%s err=%s "
                "(sink contract violation — classified as permanent_error)",
                kind, exc,
            )
            return DeliveryResult(
                sink_kind=kind,
                outcome="permanent_error",
                attempts=1,
                target=sink.target_summary() if sink.configured else "",
                detail=f"{type(exc).__name__}: {exc}",
            )

    # -- ledger ----------------------------------------------------------

    #: DeliveryResult outcome → ledger status. ``None`` ⇒ no row.
    _OUTCOME_TO_STATUS: dict[str, str | None] = {
        "delivered": "delivered",
        "transient_error": "failed",
        "permanent_error": "failed",
        # Declared-inactive stays VISIBLE — the whole point of P1-1's
        # unconfigured contract.
        "skipped_unconfigured": "skipped_unconfigured",
        # A suppressed would-be POST is a delivery decision — audited.
        "skipped_cooldown": "skipped_cooldown",
        # A configured severity filter doing its job is not a delivery event.
        "skipped_below_severity": None,
        # The first series already audited this alert_row_id.
        "skipped_duplicate": None,
    }

    async def _record_ledger_row(
        self,
        sink: AlertSink,
        payload: AlertSinkPayload,
        result: DeliveryResult,
    ) -> None:
        """One ``alert_sink_deliveries`` row for this sink outcome.

        Mirrors the ChannelEmitter audit insert (migration 0061 columns).
        Best-effort: a ledger write failure is logged, never raised.
        """
        if self._pg_pool is None:
            return
        status = self._OUTCOME_TO_STATUS.get(result.outcome)
        if status is None:
            return

        row_uuid: UUID | None = None
        if payload.alert_row_id:
            try:
                row_uuid = UUID(payload.alert_row_id)
            except (ValueError, TypeError, AttributeError):
                row_uuid = None

        delivered_at = (
            datetime.now(tz=timezone.utc) if status == "delivered" else None
        )
        summary = {
            "summary": payload.summary[:200],
            "verify_state": payload.verify_state[:200],
            "receipt_path": payload.receipt_path,
            "source_links": len(payload.source_links),
            "outcome": result.outcome,
            "http_status": result.http_status,
        }
        sql = """
            INSERT INTO alert_sink_deliveries (
                alert_row_id, channel_name, sink_kind, sink_target,
                target_id, severity, effective_confidence, attempt_number,
                status, error_message, delivered_at, payload_summary
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
        """
        args = (
            row_uuid,
            payload.channel_name,
            sink.sink_kind,
            result.target or None,   # REDACTED host — never the full URL
            payload.target_id,
            payload.severity,
            payload.effective_confidence,
            max(result.attempts, 1),
            status,
            (result.detail or None) if status != "delivered" else None,
            delivered_at,
            json.dumps(summary, separators=(",", ":")),
        )
        try:
            await self._pg_pool.execute(sql, *args)
        except Exception as exc:  # pragma: no cover — fault-injection path
            logger.warning(
                "alert_sinks.ledger_write_failed sink_kind=%s status=%s err=%s",
                sink.sink_kind, status, exc,
            )


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "ENV_PUBLIC_BASE_URL",
    "LINEAGE_API_PATH_TEMPLATE",
    "SEVERITY_ORDER",
    "AlertSink",
    "AlertSinkDispatcher",
    "AlertSinkFactory",
    "AlertSinkPayload",
    "DeliveryResult",
    "Outcome",
    "Severity",
    "build_registered_sinks",
    "normalise_severity",
    "receipt_link",
    "redact_url_to_host",
    "register_alert_sink",
    "registered_sink_kinds",
    "runtime_alert_payload",
    "unverified_state",
    "verify_state_from_score",
]
