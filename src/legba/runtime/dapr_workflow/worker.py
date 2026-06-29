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


# Default max gRPC message size for the durabletask channel (16 MiB) — well
# above daprd's 4 MB default so a workflow-input / activity-payload spike
# degrades gracefully instead of wedging the orchestrator with
# RESOURCE_EXHAUSTED. Defense-in-depth: the PRIMARY fix is pass-by-reference
# (the worker re-fetches the training set; see gepa.materialize_training_set).
# Env-overridable to mirror the daprd `-max-body-size` lever in compose.
_DEFAULT_GRPC_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _grpc_max_message_bytes() -> int:
    raw = os.environ.get("LEGBA_DAPR_WORKFLOW_GRPC_MAX_BYTES")
    if not raw:
        return _DEFAULT_GRPC_MAX_MESSAGE_BYTES
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_GRPC_MAX_MESSAGE_BYTES
    return n if n > 0 else _DEFAULT_GRPC_MAX_MESSAGE_BYTES


def _raise_worker_grpc_limit(runtime: Any) -> None:
    """Best-effort: raise the durabletask worker's gRPC channel size cap.

    GUARDRAIL (fix #1, secondary). ``dapr.ext.workflow.WorkflowRuntime`` does
    NOT expose the underlying durabletask ``TaskHubGrpcWorker``'s
    ``channel_options`` (the public wrapper only forwards host/port/logger/
    concurrency). The durabletask worker, however, reads ``self._channel_options``
    LAZILY when it (re)builds its gRPC channel at ``start()`` — so injecting the
    options on that object BEFORE ``runtime.start()`` is honoured.

    This reaches the wrapper's name-mangled ``__worker`` attribute
    (``_WorkflowRuntime__worker``). That's a private of a pinned dependency
    (daprio/dapr 1.17.9 ↔ dapr-ext-workflow), so it's guarded: any shape change
    (attr renamed / options field gone) logs + no-ops rather than crashing the
    worker — the daprd ``-max-body-size`` flag is the independent, supported
    lever, and pass-by-reference means the payload no longer needs the headroom.
    """
    max_bytes = _grpc_max_message_bytes()
    options = [
        ("grpc.max_receive_message_length", max_bytes),
        ("grpc.max_send_message_length", max_bytes),
    ]
    try:
        worker = getattr(runtime, "_WorkflowRuntime__worker", None)
        if worker is None or not hasattr(worker, "_channel_options"):
            logger.info(
                "dapr_workflow_worker.grpc_limit.skipped reason=no reachable "
                "channel_options on durabletask worker (relying on daprd "
                "-max-body-size)",
            )
            return
        existing = list(getattr(worker, "_channel_options", None) or [])
        keys = {k for k, _ in existing}
        existing.extend(o for o in options if o[0] not in keys)
        worker._channel_options = existing
        logger.info(
            "dapr_workflow_worker.grpc_limit.set max_bytes=%d", max_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — never block worker startup
        logger.warning(
            "dapr_workflow_worker.grpc_limit.failed err=%r "
            "(relying on daprd -max-body-size + pass-by-reference)", exc,
        )


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
    # Guardrail (fix #1): raise the durabletask channel's gRPC message cap
    # BEFORE start() builds the channel. Best-effort + guarded — the supported
    # daprd `-max-body-size` flag is the independent lever.
    _raise_worker_grpc_limit(runtime)
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
