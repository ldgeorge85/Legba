# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-16 — ``legba-dapr-workflow-worker`` entrypoint.

Runs a long-lived :class:`dapr.ext.workflow.WorkflowRuntime` that registers the
optimizer workflow + its activities against the daprd sidecar's gRPC
channel and then services workflow/activity work items the engine
dispatches.

Two ways P-CUT can use this
---------------------------
1. **As a standalone process / sidecar container** — ``python -m
   legba.runtime.dapr_workflow.worker`` (or the ``legba-dapr-workflow-worker``
   console script).  Connects to the sidecar gRPC named by
   ``DAPR_RUNTIME_HOST`` / ``DAPR_GRPC_PORT``.  This is the direct
   replacement for the retired ``legba-temporal-worker`` container (L-205).

2. **Embedded in the dapr-host process** — P-CUT can call
   :func:`build_workflow_runtime` to get a configured (but not started)
   ``WorkflowRuntime``, then ``runtime.start()`` it inside the host's
   lifespan and ``runtime.shutdown()`` on stop.  This collapses the
   worker into the host process so there's no second container at all.
   :func:`build_workflow_runtime` is the clean entrypoint the task brief
   asks this package to expose to P-CUT.

Both paths register the SAME workflow + activity functions, so a workflow
scheduled by :class:`legba.runtime.dapr_workflow.client.DaprOptimizerWorkflowClient`
is picked up regardless of which deployment shape is in use.

Why a dedicated runtime and not piggy-back on the actor host's channel:
the ``WorkflowRuntime`` is itself a durabletask gRPC worker — it opens its
own stream to the sidecar's workflow engine.  It is independent of the
Dapr Actor registration the FastAPI ``DaprActor(app)`` mounts; both can
coexist against one sidecar (the actor endpoints are HTTP-app-channel,
the workflow worker is an outbound gRPC stream).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LEGBA_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_workflow_runtime(
    *, host: str | None = None, port: str | None = None,
) -> Any:
    """Construct + register a :class:`WorkflowRuntime` (NOT started).

    This is the clean entrypoint P-CUT calls to embed the optimizer
    workflow worker inside the dapr-host process.  Caller is responsible
    for ``runtime.start()`` (after the sidecar gRPC is reachable) and
    ``runtime.shutdown()`` on host stop.

    Raises :class:`ModuleNotFoundError` if ``dapr.ext.workflow`` isn't
    installed — embedding the worker requires the dep (the in-process
    optimizer fallback is the kind's concern, not the worker's).
    """
    try:
        from dapr.ext.workflow import WorkflowRuntime  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise ModuleNotFoundError(
            "dapr.ext.workflow not installed — install dapr-ext-workflow "
            "to run the optimizer Dapr-Workflow worker",
        ) from exc

    from .deep_consult_workflow import (
        acquire_activity,
        analyze_activity,
        deep_consult_workflow,
        plan_activity,
        synthesize_activity,
    )
    from .workflow import (
        WORKFLOW_NAME,
        compile_candidate_activity,
        optimizer_workflow,
        validate_training_set_activity,
    )

    resolved_host = host or os.environ.get("DAPR_RUNTIME_HOST") or "127.0.0.1"
    resolved_port = port or os.environ.get("DAPR_GRPC_PORT") or "50001"

    runtime = WorkflowRuntime(host=resolved_host, port=resolved_port)
    # Register under the workflow function's OWN name (no name= override). The
    # client schedules via the function object (client.schedule_new_workflow(
    # optimizer_workflow, ...)), so durabletask resolves the orchestrator by
    # the function's __name__ ('optimizer_workflow'). An explicit name=override
    # only matched when the SAME process held the registration (the embedded
    # path); an EXTERNAL worker registering under a different name caused the
    # client to schedule a name the engine had no orchestrator for
    # (OrchestratorNotRegisteredError). Keeping both sides on the function name
    # makes embedded + external identical.
    runtime.register_workflow(optimizer_workflow)
    runtime.register_activity(validate_training_set_activity)
    runtime.register_activity(compile_candidate_activity)
    # Deep-consult workflow (anchor §5 PIECE 4) — the SAME register-by-function-
    # name rule; both workflows ride this one runtime so the SAME worker /
    # console script services both (no second container needed for v1).
    runtime.register_workflow(deep_consult_workflow)
    runtime.register_activity(plan_activity)
    runtime.register_activity(acquire_activity)
    runtime.register_activity(analyze_activity)
    runtime.register_activity(synthesize_activity)
    logger.info(
        "dapr_workflow_worker.registered workflows=%s,%s host=%s port=%s",
        optimizer_workflow.__name__, deep_consult_workflow.__name__,
        resolved_host, resolved_port,
    )
    return runtime


def run_worker() -> None:
    """Start the runtime + block until SIGINT/SIGTERM.

    Standalone-process path.  Exits non-zero if ``dapr.ext.workflow``
    isn't installed.
    """
    try:
        runtime = build_workflow_runtime()
    except ModuleNotFoundError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    runtime.start()
    logger.info("dapr_workflow_worker.started")

    stop = threading.Event()

    def _handle(*_: Any) -> None:
        logger.info("dapr_workflow_worker.shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        stop.wait()
    finally:
        runtime.shutdown()
        logger.info("dapr_workflow_worker.stopped")


def main() -> None:
    """Console-script entry point (``legba-dapr-workflow-worker``)."""
    _configure_logging()
    try:
        run_worker()
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("dapr_workflow_worker.shutdown reason=keyboard_interrupt")
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
