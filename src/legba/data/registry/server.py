# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI launcher for the registry HTTP + WebSocket API (L-113).

Wires every registry dependency (Postgres pool, NATS connection, descriptor
+ stack + credential vault, audit logger, vocabulary cache, dead-letter
writer, optional conversion-webhook registry) into a single FastAPI app and
mounts the L-113 router under `/api/v1/registry/`.

Run modes:

  * Programmatic: `app = create_app(...)`  → mount however you like.
  * CLI:          `python -m legba.data.registry.server`
                  honours `LEGBA_REGISTRY_API_PORT` (default 8090) and
                  `LEGBA_REGISTRY_API_HOST` (default 0.0.0.0).

The console script `legba-registry` (pyproject) targets `main()` below.

Lifespan responsibilities:
  * Connect Postgres + NATS pools on startup; close on shutdown.
  * Start the descriptor registry's vocabulary subscription.
  * Start the stack registry's background healthcheck loop.
  * Verify the credential vault master key is loadable (fail fast).
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from ..config import NatsConfig, PostgresConfig
from ..nats import NatsStore
from ..postgres import PostgresStore
from .api import (
    RegistryAPIDeps,
    build_router,
)
from .audit import AuditLogger
from .credentials import CredentialVault
from .descriptor import DescriptorRegistry
from .dlq import DescriptorDeadLetter
from .emitter import NATSEventEmitter
from .signing import load_default_identity
from .stack import StackRegistry
from .streams import ensure_runtime_event_streams
from .vocabulary_cache import VocabularyCache

logger = logging.getLogger(__name__)

API_PORT_ENV = "LEGBA_REGISTRY_API_PORT"
API_HOST_ENV = "LEGBA_REGISTRY_API_HOST"
API_PREFIX = "/api/v1/registry"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    pg_config: PostgresConfig | None = None,
    nats_config: NatsConfig | None = None,
    enable_healthcheck_loop: bool = False,
    enable_vocabulary_subscription: bool = True,
) -> FastAPI:
    """Construct the FastAPI app with full registry wiring.

    Stores + registries are created here but not connected until the
    `lifespan` handler runs on app startup. This lets tests instantiate the
    app, hand it to a TestClient, and have everything come up via the
    standard FastAPI lifecycle.

    `enable_healthcheck_loop` controls whether the stack-side background
    health poll runs; default False so unit / integration tests don't spin
    a probe thread unless they ask for it.
    """
    pg_cfg = pg_config or PostgresConfig.from_env()
    nats_cfg = nats_config or NatsConfig.from_env()

    pg_store = PostgresStore(pg_cfg)
    nats_store = NatsStore(nats_cfg)

    signing_identity = load_default_identity()
    audit_logger = AuditLogger(identity=signing_identity)
    vocab_cache = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    # L-221: thread the registry's existing NATS emitter into the DLQ
    # writer so every descriptor-side dead-letter insert fires a per-row
    # ``legba.dlq.descriptor.{id}`` event for the UI live-tail panel.
    # The emitter is constructed before the DLQ so it can be passed in.
    emitter = NATSEventEmitter(nats_store)
    dlq = DescriptorDeadLetter(pg_store, emitter=emitter)
    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab_cache,
        signing_identity=signing_identity,
        audit_logger=audit_logger,
        dead_letter=dlq,
    )
    stack_registry = StackRegistry(
        pg_store,
        vault,
        audit=audit_logger,
        emitter=emitter,
        dlq=dlq,
    )

    # L-112 may or may not be present (in-flight in parallel per the L-113
    # brief). When the module is importable we wire it in; otherwise the API
    # layer falls back to a raw-SQL shim over `conversion_webhooks`.
    conversion_registry: object | None = None
    try:
        from .conversion import ConversionWebhookRegistry as _CWR

        conversion_registry = _CWR(
            pg_store,
            nats_store=nats_store,
            signing_identity=signing_identity,
            audit_logger=audit_logger,
        )
    except ImportError:  # pragma: no cover
        conversion_registry = None

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit_logger,
        vocabulary_cache=vocab_cache,
        nats_store=nats_store,
        conversion_registry=conversion_registry,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await pg_store.connect()
        await nats_store.connect()
        # Provision the three runtime event streams so descriptor / stack /
        # vocabulary publishes actually land (without a matching JetStream
        # stream `js.publish` returns "no stream matches" and the emitter
        # logs+drops the event). Idempotent — re-runs on warm restart are
        # cheap no-ops.
        try:
            await ensure_runtime_event_streams(nats_store)
        except Exception as exc:  # pragma: no cover — fail loud, do not block startup
            logger.warning(
                "registry.streams.ensure_failed err=%s "
                "(events will silently drop until streams are provisioned)",
                exc,
            )
        if enable_vocabulary_subscription:
            await descriptor_registry.start()
        else:
            await vocab_cache.refresh()
        health_task = None
        if enable_healthcheck_loop:
            health_task = stack_registry.start_health_loop()
        try:
            yield
        finally:
            if health_task is not None:
                try:
                    await stack_registry.stop_health_loop()
                except Exception as exc:  # pragma: no cover
                    logger.warning("stack health loop stop failed: %s", exc)
            try:
                await descriptor_registry.stop()
            except Exception as exc:  # pragma: no cover
                logger.warning("descriptor registry stop failed: %s", exc)
            try:
                await nats_store.close()
            except Exception:  # pragma: no cover
                pass
            try:
                await pg_store.close()
            except Exception:  # pragma: no cover
                pass

    app = FastAPI(
        title="Legba Registry API",
        description=(
            "L-113 HTTP + WebSocket surface for the descriptor, stack, "
            "credential vault, conversion-webhook, dead-letter, audit and "
            "vocabulary registries."
        ),
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.registry_deps = deps

    # Defense-in-depth rate limiting on the expensive actor-dispatch endpoints
    # (consult / deep_consult). Installs slowapi's 429 handler + middleware
    # against the module-singleton limiter the endpoint modules decorate
    # against; degrades to a loud-warn passthrough if slowapi is absent.
    from .rate_limit import install as install_rate_limit
    from .rate_limit import limiter as _rate_limiter
    install_rate_limit(app, _rate_limiter)

    app.include_router(build_router(deps), prefix=API_PREFIX)

    # v3 telemetry surface — runtime actor health + optimizer candidate queue.
    # Mounted under /api/v1/v3 so the UI's L-092 §3.5 S4/S6 panels resolve
    # against real substrate state without going through a separate runtime
    # HTTP service. See v3_api.py for the per-route shape contracts.
    from .v3_api import build_v3_router
    app.include_router(build_v3_router(deps), prefix="/api/v1/v3")

    # P1-6 — "since last visit" diff + band trajectory (console / wall tile).
    # Mounted under the SAME /api/v1/v3 prefix beside the telemetry router;
    # registry-slim (no runtime/deterministic import — mirrored constants with
    # drift guards instead). See since_api.py for the envelope contracts.
    from .since_api import build_since_router
    app.include_router(build_since_router(deps), prefix="/api/v1/v3")

    # P4-4 — validity-window timeline read surface (the `system.timeline`
    # panel): facts/situations/findings as RANGED items ([start, end|open) +
    # supersession-chain edges). Same /api/v1/v3 prefix beside the since router;
    # registry-slim (no runtime import). See timeline_api.py for the envelope.
    from .timeline_api import build_timeline_router
    app.include_router(build_timeline_router(deps), prefix="/api/v1/v3")

    # P3-1 — source assurance ledger read surface (A6 layers 1+2): current
    # per-rater ratings + dossier, visibility-filtered (private annex rows
    # only on explicit opt-in). Same /api/v1/v3 prefix; see
    # source_assurance_api.py for the visibility seam contract.
    from .source_assurance_api import build_source_assurance_router
    app.include_router(build_source_assurance_router(deps), prefix="/api/v1/v3")

    # P4-1/P4-2 — reified narratives + source-echo propagation graph (A11).
    # Read-only surface over the migration-0102 derived tables the
    # narrative_mapper analyst refreshes; the data surface for a future UI
    # narratives panel. Degrades to empty when 0102 is unapplied. Same
    # /api/v1/v3 prefix; see narratives_api.py for the honesty contract.
    from .narratives_api import build_narratives_router
    app.include_router(build_narratives_router(deps), prefix="/api/v1/v3")

    # Substrate-read endpoints (E-1) — cross-target intelligence feeds
    # the redesigned UI's daily-driver panels consume.
    from .substrate_reads_api import build_substrate_reads_router
    app.include_router(
        build_substrate_reads_router(deps), prefix="/api/v1",
    )

    # Lineage walk endpoint (E-2) — universal derived_from[] tracer.
    from .lineage_api import build_lineage_router
    app.include_router(build_lineage_router(deps), prefix="/api/v1")

    # Journal read surface (JOURNAL_ASSESSOR_PLAN §9 / Wave 3) — the open
    # consolidation + recent entries, with every cited ref resolved to its
    # (kind, title) for the panel's per-claim provenance chips. Read-only;
    # off-chain (reads journal_entries directly, never the lineage catalog).
    from .journal_api import build_journal_router
    app.include_router(build_journal_router(deps), prefix="/api/v1")

    # Journal propose-and-gate review surface (JOURNAL_ASSESSOR_PLAN §7.4 / Wave
    # 4) — the operator queue: list/filter journal_proposals + accept (apply via
    # the existing write/lifecycle path, idempotent) + reject (requires a reason).
    # The journal SUGGESTS into the queue; a human DISPOSES here.
    from .journal_proposals_api import build_journal_proposals_router
    app.include_router(build_journal_proposals_router(deps), prefix="/api/v1")

    # Labeled reference-set (gold) surface (P2-T4) — per-bounded-unit correctness
    # labels the Phase-2 scorer compares a unit's live read against. POST records
    # one (unit, target) gold answer grounded to canonical_source_ids; GET reads
    # them back filtered by unit/target. Backs unit_reference_labels (mig 0057).
    from .labels_api import build_labels_router
    app.include_router(build_labels_router(deps), prefix="/api/v1")

    # Correctness gold-set labeling loop (P2-5) — the weekly worksheet the
    # operator labels a handful of findings on (deterministic ISO-week sample,
    # pinned in goldset_week_samples) + the per-finding verdict upsert. Backs
    # correctness_labels (mig 0096); the per-unit operator aggregate overlays
    # onto /eval/scores (labels_api).
    from .goldset_api import build_goldset_router
    app.include_router(build_goldset_router(deps), prefix="/api/v1/v3")

    # Collection export (A10) — basket of findings + journal entries → one
    # markdown/JSON document, composed server-side at full fidelity.
    from .export_api import build_export_router
    app.include_router(build_export_router(deps), prefix="/api/v1/v3")

    # Watchlist v2 (P5-6) — operator-defined standing watches (entity / text /
    # geo), the server-side personal layer the alert_trigger_scan's
    # watchlist_hit class evaluates. First WRITE surface in the v3 family;
    # deletes are soft (active=false). See watchlist_api.py.
    from .watchlist_api import build_watchlist_router
    app.include_router(build_watchlist_router(deps), prefix="/api/v1/v3")

    # Entity knowledge-graph read API — entity_profiles + signal_entity_links
    # + proposed_edges for the Entities / Entity-Graph / Entity-Detail panels.
    from .entities_api import build_entities_router
    app.include_router(build_entities_router(deps), prefix="/api/v1")

    # Notable-structure overlay (#99) — ranked `interesting` shortlist from the
    # structural_balance + graph_mining graph_metrics rows (tense actors,
    # brokers, new-hostile edges, proxy chains), selection-aware by entity.
    from .graph_structure_api import build_graph_structure_router
    app.include_router(build_graph_structure_router(deps), prefix="/api/v1")

    # Runtime + analyst telemetry endpoints (E-3) — target/analyst
    # roster + per-actor source cursors + analyst run/output/critique
    # views. Paths are top-level under /api/v1, not /api/v1/v3.
    from .runtime_telemetry_api import build_runtime_telemetry_router
    app.include_router(
        build_runtime_telemetry_router(deps), prefix="/api/v1",
    )

    # Budget endpoints (E-4) — ledger + envelope + demotion-event views.
    from .budget_api import build_budget_router
    app.include_router(
        build_budget_router(deps), prefix="/api/v1/budget",
    )

    # Source-credibility CRUD (E-5) — operator-curated surface.
    from .source_credibility_api import build_source_credibility_router
    app.include_router(
        build_source_credibility_router(deps), prefix="/api/v1",
    )

    # On-demand consult invocation (Pass 3.5) — server-side proxy that
    # invokes the consult_default analyst actor via the Dapr
    # sidecar so the SPA consult panel can ask + answer in real time.
    from .consult_api import build_consult_router
    app.include_router(
        build_consult_router(deps), prefix="/api/v1",
    )

    from .consult_stream_api import build_consult_stream_router
    app.include_router(
        build_consult_stream_router(deps), prefix="/api/v1",
    )

    # Consult session-history read surface (0038 audit trail) — list prior
    # sessions + load one to re-seed the client transcript (continue a chat /
    # browse deep-consult task history). Write path is in consult_api /
    # deep_consult_api.
    from .consult_sessions_api import build_consult_sessions_router
    app.include_router(
        build_consult_sessions_router(deps), prefix="/api/v1",
    )

    # Deep-consult submit + status (anchor §5 PIECE 4) — the DETACHED variant:
    # POST schedules the staged Dapr Workflow and returns a task id; GET polls
    # the produced finding. Schedules via the deep_consult actor over the
    # runtime sidecar (same transport as consult_api).
    from .deep_consult_api import build_deep_consult_router
    app.include_router(
        build_deep_consult_router(deps), prefix="/api/v1",
    )

    # Prometheus /metrics exposition (resilience P2). App-level, no prefix
    # and no bearer gate — Prometheus scrapers don't carry the operator
    # token, matching the unauthenticated /healthz convention below.
    from .metrics_api import build_metrics_router
    app.include_router(build_metrics_router(deps))

    @app.get(f"{API_PREFIX}/docs", include_in_schema=False)
    async def custom_swagger_ui() -> "JSONResponse":
        return get_swagger_ui_html(
            openapi_url=f"{API_PREFIX}/openapi.json",
            title="Legba Registry API — Swagger",
        )

    @app.get(f"{API_PREFIX}/healthz", include_in_schema=False)
    async def healthz() -> "JSONResponse":
        """Readiness probe — pings Postgres + NATS (resilience-observability W-1b §4).

        Upgrades the old liveness stub (which just answered ``ok`` once the
        process bound a socket) into a *readiness* check: the container is only
        healthy when both substrate dependencies actually respond. A 200 means
        Postgres returned ``SELECT 1`` AND the NATS client reports connected; a
        503 with the failing component(s) means the registry is up but cannot
        serve — so the Docker HEALTHCHECK / Caddy upstream takes it out of
        rotation instead of routing into a half-dead process.
        """
        checks: dict[str, str] = {}
        ok = True

        # Postgres — cheapest possible round-trip on the live pool.
        try:
            value = await pg_store.pool.fetchval("SELECT 1")
            if value == 1:
                checks["postgres"] = "ok"
            else:
                checks["postgres"] = f"unexpected:{value!r}"
                ok = False
        except Exception as exc:  # pragma: no cover — exercised only on outage
            checks["postgres"] = f"error:{type(exc).__name__}"
            ok = False

        # NATS — the JetStream transport the emitter / fan-out depend on.
        try:
            nc = nats_store.nc
            if getattr(nc, "is_connected", False):
                checks["nats"] = "ok"
            else:
                checks["nats"] = "disconnected"
                ok = False
        except Exception as exc:  # pragma: no cover — only before connect()
            checks["nats"] = f"error:{type(exc).__name__}"
            ok = False

        body = {"status": "ok" if ok else "unavailable", "checks": checks}
        return JSONResponse(body, status_code=200 if ok else 503)

    return app


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _resolve_port() -> int:
    raw = os.getenv(API_PORT_ENV, "8090").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{API_PORT_ENV} must be an integer, got {raw!r}") from exc


def _resolve_host() -> str:
    return os.getenv(API_HOST_ENV, "0.0.0.0").strip() or "0.0.0.0"


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LEGBA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    host = _resolve_host()
    port = _resolve_port()
    app = create_app(
        enable_healthcheck_loop=bool(
            os.getenv("LEGBA_REGISTRY_HEALTH_LOOP", "").strip()
        ),
    )
    logger.info("legba-registry starting on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    main()
