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
`LEGBA_REGISTRY_WORKERS` > 1 launches that many uvicorn workers through
`app_factory` (W11-F85; what is shared and what stays per worker is in
`workers.py`). Unset is one worker, launched exactly as before.

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
from ..taxon_families import install_vocabulary
from .api import (
    RegistryAPIDeps,
    build_router,
)
from .audit import AuditLogger
from .auth_state import close_auth_state, open_auth_state
from .credentials import CredentialVault
from .descriptor import DescriptorRegistry
from .dlq import DescriptorDeadLetter
from .emitter import NATSEventEmitter
from .signing import load_default_identity
from .stack import StackRegistry
from .streams import ensure_runtime_event_streams
from .vocabulary_cache import VocabularyCache
from . import workers as _workers

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
    # W11-F85: at LEGBA_REGISTRY_WORKERS > 1 each worker takes its share of
    # the pool, so N workers hold what one did (an explicit config is kept).
    pg_cfg = pg_config or _workers.worker_pg_config(PostgresConfig.from_env())
    nats_cfg = nats_config or NatsConfig.from_env()

    pg_store = PostgresStore(pg_cfg)
    nats_store = NatsStore(nats_cfg)

    signing_identity = load_default_identity()
    audit_logger = AuditLogger(identity=signing_identity)
    vocab_cache = VocabularyCache(pg_store)
    # W7 TAXON-1: the four taxon families (salience_class, collection_gap_kind,
    # band, source_layer) read through this cache in-process; a family it does
    # not hold reads its code literal (legba.data.taxon_families).
    install_vocabulary(vocab_cache)

    # L-221: the registry's shared NATS emitter, threaded into every registry
    # component that publishes lifecycle events — the DLQ writer (per-row
    # ``legba.dlq.descriptor.{id}``), the stack registry (``stack.component.>``),
    # and the credential vault (``vault.secret.rotated.<id>`` — the rotation
    # eviction hook: a rotated secret must not keep serving from a runtime
    # process's cached LLM handler until a container recreate). Constructed
    # before all three so it can be passed into each.
    emitter = NATSEventEmitter(nats_store)
    vault = CredentialVault(pg_store, emitter=emitter)
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

    async def _ensure_streams() -> None:
        # Provision the runtime event streams (descriptor / stack / vocabulary
        # / dlq / vault) so publishes actually land (without a matching
        # JetStream stream `js.publish` returns "no stream matches" and the
        # emitter logs+drops the event). Idempotent — re-runs on warm restart
        # are cheap no-ops.
        try:
            await ensure_runtime_event_streams(nats_store)
        except Exception as exc:  # pragma: no cover — fail loud, do not block startup
            logger.warning(
                "registry.streams.ensure_failed err=%s "
                "(events will silently drop until streams are provisioned)",
                exc,
            )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await pg_store.connect()
        await nats_store.connect()
        # W11-F85 — ONCE-ONLY work (stream provisioning, the health loop) runs
        # on the worker holding the advisory lock; at one worker the lease is
        # a no-op holder and this is the order it always was.
        once = _workers.OnceOnlyLease(pg_store)
        if await once.acquire():
            await _ensure_streams()
        if enable_vocabulary_subscription:
            await descriptor_registry.start()
        else:
            await vocab_cache.refresh()
        health_task = None
        if enable_healthcheck_loop and once.is_holder:
            health_task = stack_registry.start_health_loop()

        async def _promoted() -> None:  # a standby that won the lock later
            await _ensure_streams()
            if enable_healthcheck_loop:
                stack_registry.start_health_loop()

        async def _demoted() -> None:
            if enable_healthcheck_loop:
                await stack_registry.stop_health_loop()

        once.watch(on_promote=_promoted, on_demote=_demoted)
        # X4 GATE — the shared auth state (the sign-in lockout, consult run
        # owners) in Redis; degrades to per-process, loudly, never blocks boot.
        await open_auth_state()
        try:
            yield
        finally:
            await close_auth_state()
            if health_task is not None or (enable_healthcheck_loop and once.enabled):
                try:
                    await stack_registry.stop_health_loop()
                except Exception as exc:  # pragma: no cover
                    logger.warning("stack health loop stop failed: %s", exc)
            await once.stop()
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

    # X2-B2 — the route-role gate: ONE app-level dependency that gates every
    # mounted route through route_roles.ROUTE_ROLES (operator /
    # reader-own-rows / read). Installed HERE, before any include_router, so
    # every route inherits it. X4 GATE: it reads the stamps the two auth
    # middlewares below wrote and REFUSES an unstamped caller on every
    # non-public route; the bearer gate in a route still authenticates.
    from fastapi import Depends

    from .route_roles import enforce_route_role

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
        dependencies=[Depends(enforce_route_role)],
    )
    app.state.registry_deps = deps

    # Defense-in-depth rate limiting on the expensive actor-dispatch endpoints
    # (consult / deep_consult). Installs slowapi's 429 handler + middleware
    # against the module-singleton limiter the endpoint modules decorate
    # against; degrades to a loud-warn passthrough if slowapi is absent.
    from .rate_limit import install as install_rate_limit
    from .rate_limit import limiter as _rate_limiter
    install_rate_limit(app, _rate_limiter)

    # X2-B4 — the reader DISPLAY POLICY on every JSON response this app serves
    # (``display_class.DisplayPolicyMiddleware``): a reader-class caller gets
    # the projection, never archived bytes or publisher text; an operator's
    # responses pass through unbuffered and byte-identical.
    from .display_class import DisplayPolicyMiddleware
    app.add_middleware(DisplayPolicyMiddleware)

    # R-1 — ETag + `Cache-Control: private, max-age=5` on the console landing's
    # read routes (read_latency.DEFAULT_CACHEABLE_PATHS), a 304 on a matching
    # If-None-Match. Added AFTER the display policy so it is OUTSIDE it: the
    # tag is over the bytes this caller was actually served.
    from .read_latency import ConditionalGetMiddleware
    app.add_middleware(ConditionalGetMiddleware)

    # X3-B3 — the SESSION COOKIE: a signed-in user's cookie (no bearer
    # credential) is resolved here and stamped on the request state
    # (legba_role / legba_subject / principal_actor = user:<id>) BEFORE any
    # dependency — the route-role gate above and the display class read it.
    from .session_auth import SessionCookieMiddleware
    app.add_middleware(SessionCookieMiddleware)

    # X4 GATE (W11-F75) — the BEARER stamp, added LAST so it is the OUTERMOST
    # layer and runs first: it resolves the Authorization header (the SSE
    # relay's ?token=, the events socket's subprotocol) through the _deps
    # resolvers and stamps principal / principal_actor / legba_role =
    # operator — or records the 401/403/503 the gate then answers — BEFORE
    # the session middleware (which skips a request that presented a bearer)
    # and before the route-role gate, which REFUSES whoever neither stamped.
    # Request order: bearer → session → display policy → rate limit → gate →
    # route dependencies.
    from .bearer_auth import BearerPrincipalMiddleware
    app.add_middleware(BearerPrincipalMiddleware)

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

    # S-1 — the signed-in person's own seen cursor (GET/PUT /me/seen); 404
    # with the sign-in switch off. Registry-slim. See me_seen_api.py.
    from .me_seen_api import build_me_seen_router
    app.include_router(build_me_seen_router(deps), prefix="/api/v1/v3")

    # S-2 — acknowledge / snooze an alert episode (contributor+), and the
    # episode's attention state + history (reader). Registry-slim. See
    # alert_ack_api.py.
    from .alert_ack_api import build_alert_ack_router
    app.include_router(build_alert_ack_router(deps), prefix="/api/v1/v3")

    # P4-4 — validity-window timeline read surface (the `system.timeline`
    # panel): facts/situations/findings as RANGED items ([start, end|open) +
    # supersession-chain edges). Same /api/v1/v3 prefix beside the since router;
    # registry-slim (no runtime import). See timeline_api.py for the envelope.
    from .timeline_api import build_timeline_router
    app.include_router(build_timeline_router(deps), prefix="/api/v1/v3")

    # c1 — the correctness GRADER's roster: every graded desk's correctness AND
    # coverage over a trailing window, plus the roster totals (mean-of-desks and
    # claim-pooled, each labelled). Its own leaf module on the /api/v1/v3 prefix
    # rather than a section of v3_api: that file is one line under its
    # module-size ceiling, and this axis is the machine grader's, never to be
    # pooled with the operator gold-set axis /eval/correctness serves from
    # there. See grader_roster_api.py for the four honesty rules.
    from .grader_roster_api import build_grader_roster_router
    app.include_router(build_grader_roster_router(deps), prefix="/api/v1/v3")

    # P3-1 — source assurance ledger read surface (A6 layers 1+2): current
    # per-rater ratings + dossier, visibility-filtered (private annex rows
    # only on explicit opt-in). Same /api/v1/v3 prefix; see
    # source_assurance_api.py for the visibility seam contract.
    from .source_assurance_api import build_source_assurance_router
    app.include_router(build_source_assurance_router(deps), prefix="/api/v1/v3")

    # C3 — the merged source-quality ledger (migration 0115): asserted
    # (Admiralty grade + host credibility) / earned (contested track record) /
    # computed (A7 freshness) as three TYPED sections that are never blended.
    # Supersedes the assurance route above and the /source_credibility reads,
    # which keep serving under a Deprecation/Sunset window. Mounted AFTER
    # source_assurance_api so the deprecated path stays registered; the two
    # have no path collision (.../assurance vs .../quality).
    from .source_quality_api import build_source_quality_router
    app.include_router(build_source_quality_router(deps), prefix="/api/v1/v3")

    # S-1 — the expected-vs-actual PRODUCTION gauge. Joins the /system/*
    # freshness family (staleness-debt / analyst-cadence / source-firing) and
    # answers what none of those can: not "is it running" but "did it produce
    # what its own descriptor and history promised". Same judgment function
    # the alert plane's production_deficit trigger class pages on — the
    # source_freshness precedent, one implementation and two readers, so a
    # threshold cannot mean one thing here and another on the phone. Its own
    # module rather than another block inside the 2.2k-line v3_api.
    from .production_gauge_api import build_production_gauge_router
    app.include_router(build_production_gauge_router(deps), prefix="/api/v1/v3")

    # GLASS-3 — the judge's verdict mix by SERVING PROVIDER. `served_by` has been
    # on every LLM receipt since 2026-08-16 and read by nothing, while a provider
    # change was measured to flip 13.6% of verdicts: an unannounced upstream input
    # to the faithfulness numbers the product is graded on. Read-only aggregate
    # over `analyst_traces.llm_calls` + critique rows — no migration, no new
    # writer. Its own module rather than another block inside the 2.4k-line
    # v3_api (which is 11 lines under its size ceiling).
    from .judge_stats_api import build_judge_stats_router
    app.include_router(build_judge_stats_router(deps), prefix="/api/v1/v3")

    # D5 — the STANDING EXTERNAL AUDIT. The judge-stats route above measures how
    # well we grade OURSELVES; this one is the only surface reporting a check
    # against the world outside the substrate. Two things live here that live
    # nowhere else: the auditor's own heartbeat (a run can end
    # `status='success'` having audited nothing, so `analyst_traces` cannot say
    # whether the auditor is actually working — the judge-outage shape), and the
    # CONTRADICTED verdicts, named individually because they are rare by
    # construction and each one is an operator event. Read-only over
    # `analyst_outputs` critiques + the 0091 watermark row; no migration.
    from .external_audit_api import build_external_audit_router
    app.include_router(build_external_audit_router(deps), prefix="/api/v1/v3")

    # V3/P4a — the five pre-registered graph-engine trigger gauges
    # (JUDGE_SYNTHESIS §4.2) for the 2026-11-03 sitting: each of E1-E5 reports
    # its threshold + reading + distance, or an explicit "unreadable" with the
    # reason — a gauge that cannot be computed must say so, never return 0.
    from .graph_triggers_api import build_graph_triggers_router
    app.include_router(build_graph_triggers_router(deps), prefix="/api/v1/v3")

    # V3/P4b — the cross-layer graph projection read. Plane-filtered bounded
    # ego/walk over graph_arcs with as_of, publishing the build stamp on every
    # response and refusing distinctly (projection_disabled / _empty /
    # _stale) — never found=False — while LEGBA_GRAPH_PROJECTION is off or the
    # projection is not there.
    from .graph_arcs_api import build_graph_arcs_router
    app.include_router(build_graph_arcs_router(deps), prefix="/api/v1/v3")

    # W9 VIEW-1 — the per-profile projection, one plane per profile, read off
    # the profile_projector's whole-rebuild table; refuses by name
    # (projection_disabled / projection_empty / projection_stale, plane_unknown
    # / section_undeclared), never found=False. Registry-slim (the leaf only).
    from .projection_api import build_projection_router
    app.include_router(build_projection_router(deps), prefix=API_PREFIX)

    # V3/P3 — the decision-time belief register ("what did Legba believe on
    # date D"). Bearer-gated, registry-slim, reads analyst_outputs directly.
    from .belief_api import build_belief_router
    app.include_router(build_belief_router(deps), prefix="/api/v1/v3")

    # H13 — the receipt-chain external-timestamp ledger: one row per
    # (day, calendar) the receipt_anchor handler attested via OpenTimestamps.
    from .receipt_anchors_api import build_receipt_anchors_router
    app.include_router(build_receipt_anchors_router(deps), prefix="/api/v1/v3")

    # RA-1 — the reader's narrow reads of two admin lists: the roster
    # (subjects / analysts / sources) and the Watch's alert log.
    from .reader_reads_api import build_reader_reads_router
    app.include_router(build_reader_reads_router(deps), prefix="/api/v1/v3")

    # ST0-C — the exogenous forecast ledger (migration 0242): every question,
    # open first by due date, with every issuer's number and the resolution.
    from .forecast_ledger_api import build_forecast_ledger_router
    app.include_router(build_forecast_ledger_router(deps), prefix="/api/v1/v3")
    # ST1 — the forecast scorecard (Brier per arm against the resolutions).
    from .forecast_scorecard_api import build_forecast_scorecard_router
    app.include_router(build_forecast_scorecard_router(deps), prefix="/api/v1/v3")
    # U-11 — one question's resolution record (proposals with their evidence,
    # and every act on them), for the console's resolution queue. Read-only.
    from .forecast_records_api import build_forecast_records_router
    app.include_router(build_forecast_records_router(deps), prefix="/api/v1/v3")
    # E-3 — the published evaluation record (migration 0260): every version
    # of every run the owner published, withdrawn ones marked. Read-only.
    from .eval_runs_api import build_eval_runs_router
    app.include_router(build_eval_runs_router(deps), prefix="/api/v1/v3")
    # F-1 — deciding a forecast's outcome from the console (confirm / reject /
    # judgement / correct, the same store calls as scripts/resolve_forecasts.py)
    # and the queue a person works; and a seal's inclusion proof in a daily
    # anchor (migration 0262).
    from .forecast_resolution_api import build_forecast_resolution_router
    app.include_router(build_forecast_resolution_router(deps), prefix="/api/v1/v3")
    from .seal_proof_api import build_seal_proof_router
    app.include_router(build_seal_proof_router(deps), prefix="/api/v1/v3")
    # U-4 — the map's two missing reads: a live series at its place (latest,
    # 30-day baseline, revised flag, last 30 values) and incidents filed at sea.
    from .map_layers_api import build_map_layers_router
    app.include_router(build_map_layers_router(deps), prefix="/api/v1/v3")

    # X1-A2 — last backup set + last passing restore drill (host status files).
    from .backup_status_api import build_backup_status_router
    app.include_router(build_backup_status_router(deps), prefix="/api/v1/v3")

    # N-J — the Flow canvas's read-only routes: the semantic graph derived from
    # the descriptor heads, one node's inspector, a stored version, one run.
    from .flow_api import build_flow_router
    app.include_router(build_flow_router(deps), prefix="/api/v1/v3")

    # 7a — the contrary-evidence pass's records. A SIBLING of
    # `/api/v1/contention` (which serves fact_contention), never an extension of
    # it: different keys, different vocabulary, no winner and never one.
    from .contentions_api import build_contentions_router
    app.include_router(build_contentions_router(deps), prefix="/api/v1/v3")

    # Program 6 L2 (7b-v) — the DIVERGENCE MAP read surface. The only reader of
    # the layer_divergence receipt, which is where a QUIET run's series lives:
    # a run that fires nothing is suppressed to trace-only, so a surface reading
    # analyst_outputs alone would be blank on exactly the days the instrument
    # worked. Reads the newest trace for the receipt and the recent findings for
    # the fires. Registry-slim (only the stdlib-only layers._vocab leaf).
    from .layers_api import build_layers_router
    app.include_router(build_layers_router(deps), prefix="/api/v1/v3")
    # 7b/k5 — TYPED ABSENCE as one named thing: the audit's scoped absence, the
    # reads' insufficient evidence, Program 6's declared-absent layers, silent
    # units and silent sources answered for one desk, each with its proof
    # (`GET /v3/absence?scope=`). Registry-slim, defensive per kind.
    from .absence_api import build_absence_router
    app.include_router(build_absence_router(deps), prefix="/api/v1/v3")
    # X1/B5 — search health: the degraded share per provider and per upstream
    # engine over 7 d, by the same rule the absence kinds above are typed with.
    from .search_health_api import build_search_health_router
    app.include_router(build_search_health_router(deps), prefix="/api/v1/v3")
    # I-1 — the ingestion intake's queue: depth by state and reason, refusals
    # by reason, per-host rates, oldest queued age. Registry-slim (the
    # stdlib-only intake_visibility leaf).
    from .intake_api import build_intake_router
    app.include_router(build_intake_router(deps), prefix="/api/v1/v3")
    # W11-F271 — signals and findings landed in a window (the World KPI
    # strip's "24h" cards): two count(*) statements. Registry-slim.
    from .window_counts_api import build_window_counts_router
    app.include_router(build_window_counts_router(deps), prefix="/api/v1/v3")
    # D-1 — source drift: open flags with their dependants and changed
    # spans, counts by class and source, one address's version history, the
    # sources that edit after publication most. Registry-slim (the
    # stdlib-only evidence_drift_reads leaf).
    from .evidence_drift_api import build_evidence_drift_router
    app.include_router(build_evidence_drift_router(deps), prefix="/api/v1/v3")
    # U-10 — the Record sheet's receipts, a version's stored text (bounded;
    # withheld from a reader and under a refusing licence) and a run's
    # integrity line. Registry-slim (the stdlib-only record_sheet_reads leaf).
    from .record_sheet_api import build_record_sheet_router
    app.include_router(build_record_sheet_router(deps), prefix="/api/v1/v3")

    # 7g-2 — the ERA COVERAGE MAP: what a curated collection's manifest
    # DECLARES the provider holds for a desk, what `observations` actually
    # carries, and the holes between them (`GET /v3/collections/coverage?
    # scope=`). Shares ONE reader with the `history_gap` typed absence above,
    # so the map and the absence cannot disagree about the same silence.
    # Registry-slim.
    from .collections_api import build_collections_router
    app.include_router(build_collections_router(deps), prefix="/api/v1/v3")

    # V3/P6 — the event read surface: the paged list (filters mirroring
    # query_events + a route-only situation_id for the tracked-events read),
    # the one-event dossier, and the lifecycle ledger. Same live/as_of +
    # origin-class gate as the tool path.
    from .events_api import build_events_router
    app.include_router(build_events_router(deps), prefix="/api/v1/v3")

    # Continuity P2 — the situation TRAJECTORY ledger read. Additive route, no
    # panel (the UI is Phase 3); it is what the situation_escalation alert links
    # to, and what makes "how did this frame get here" answerable without
    # reconstructing it from prose. Its own module rather than another block
    # inside the 2.2k-line v3_api.
    from .situation_trajectory_api import build_situation_trajectory_router
    app.include_router(build_situation_trajectory_router(deps), prefix="/api/v1/v3")

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

    # Collection requirements (R-2) — the operator review surface over
    # collection_gap's durable "we have a hole" proposals (mig 0113): list /
    # get / disposition-only PATCH (status + reviewed_by + note). Never a
    # write path to source_descriptors — registering/activating a source
    # stays a separate operator action. See collection_requirements_api.py.
    from .collection_requirements_api import build_collection_requirements_router
    app.include_router(
        build_collection_requirements_router(deps), prefix="/api/v1/v3"
    )

    # Retention-policy config (C2 follow-on) — list / get / update over the
    # one-janitor `retention_policies` table (mig 0109): ttl_days /
    # keep_classes / batch_size / enabled / description only. No POST/DELETE
    # — every row is paired 1:1 with a Python retention adapter that reads it
    # by policy_name. See retention_policies_api.py.
    from .retention_policies_api import build_retention_policies_router
    app.include_router(
        build_retention_policies_router(deps), prefix="/api/v1/v3"
    )

    # Origins and origin groups (data-model lane 9): list, the assignment
    # log, an admin's correction and the derived assignment.
    from .origins_api import build_origins_router
    app.include_router(build_origins_router(deps), prefix="/api/v1/registry")

    # Entity knowledge-graph read API — entity_profiles + signal_entity_links
    # + proposed_edges for the Entities / Entity-Graph / Entity-Detail panels.
    from .entities_api import build_entities_router
    app.include_router(build_entities_router(deps), prefix="/api/v1")

    # Notable-structure overlay (#99) — ranked `interesting` shortlist from the
    # structural_balance + graph_mining graph_metrics rows (tense actors,
    # brokers, new-hostile edges, proxy chains), selection-aware by entity.
    from .graph_structure_api import build_graph_structure_router
    app.include_router(build_graph_structure_router(deps), prefix="/api/v1")

    # K-G4 — the graph VIEWER surface: anchored 1-hop ego over `entity_edges`
    # (/graph/ego) plus per-edge evidence (/graph/edge/{id}). Mounted after the
    # structure router; the paths do not collide (`/graph/structure`,
    # `/graph/path` vs `/graph/ego`, `/graph/edge/{id}`). This is the relational
    # walk the AGE probe's measurements pointed at — see graph_walk_api.py.
    from .graph_walk_api import build_graph_walk_router
    app.include_router(build_graph_walk_router(deps), prefix="/api/v1")

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

    # X3-B3 — local users: invite / accept / sign-in / sign-out / me + the
    # operator's user admin (`/api/v1/auth/*`, migration 0236).
    from .users_api import build_users_router
    app.include_router(build_users_router(deps), prefix="/api/v1")

    # Deep-consult submit + status (anchor §5 PIECE 4) — the DETACHED variant:
    # POST schedules the staged Dapr Workflow and returns a task id; GET polls
    # the produced finding. Schedules via the deep_consult actor over the
    # runtime sidecar (same transport as consult_api).
    from .deep_consult_api import build_deep_consult_router
    app.include_router(
        build_deep_consult_router(deps), prefix="/api/v1",
    )

    # Read telemetry (D2e) — the ONE plane that receipts a READ rather than a
    # write. Ingest (POST /read-events) is append-only into migration 0189 and
    # deliberately joins nothing; the rollup (GET /read-events/rollup) is the
    # oracle wager's scoreboard. See read_events_api.py and
    # planning/CAMPAIGN_2026-08-29/PREMISE_REASON_TO_EXIST.md §5 Option 1.
    from .read_events_api import build_read_events_router
    app.include_router(
        build_read_events_router(deps), prefix="/api/v1",
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


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LEGBA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _env_app() -> FastAPI:
    return create_app(
        enable_healthcheck_loop=bool(
            os.getenv("LEGBA_REGISTRY_HEALTH_LOOP", "").strip()
        ),
    )


def app_factory() -> FastAPI:
    """The app one WORKER builds at ``LEGBA_REGISTRY_WORKERS > 1`` (uvicorn
    ``factory=True``): a spawned worker inherits no logging config, so it sets
    the same one ``main`` does."""
    _configure_logging()
    return _env_app()


#: The import string uvicorn's workers load (``factory=True``).
APP_FACTORY = "legba.data.registry.server:app_factory"


def main() -> None:
    _configure_logging()
    host = _resolve_host()
    port = _resolve_port()
    workers = _workers.worker_count()
    if workers == 1:
        app = _env_app()
        logger.info("legba-registry starting on %s:%d", host, port)
        uvicorn.run(app, host=host, port=port, log_config=None)
        return
    # W11-F85: N workers need an import string, not an app object; each
    # worker builds its own app (pools, NATS, Redis) through app_factory.
    try:
        _workers.assert_multi_worker_safe(workers)
    except _workers.MultiWorkerUnsafe as exc:
        raise SystemExit(str(exc)) from exc
    _workers.log_posture(workers)
    logger.info("legba-registry starting on %s:%d workers=%d", host, port, workers)
    uvicorn.run(
        APP_FACTORY, factory=True, host=host, port=port, workers=workers,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
