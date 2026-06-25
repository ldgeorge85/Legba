# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-16 — Dapr-Workflow optimizer client (stable kind-facing interface).

This client satisfies the EXACT surface the optimizer kind calls — the
same contract the in-process fallback
(:class:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient`)
satisfies, so the kind (:mod:`legba.data.analysts.optimizer`) never
branches on the backend::

    handle = await client.start_optimizer_workflow(wf_input, workflow_id=...)
    result = await handle.result()     # -> OptimizerWorkflowResult

The handle the kind receives satisfies
:class:`legba.runtime.dapr_workflow.gepa.WorkflowHandleLike` (``.id``,
``.result_run_id``, ``async result()``) so
``legba.data.analysts.optimizer._dispatch_workflow`` reads
``handle.id`` / ``handle.result_run_id`` unchanged.

Async over a sync SDK
---------------------
``dapr.ext.workflow.DaprWorkflowClient`` is synchronous (blocking gRPC).
The kind's call path is ``async``.  We bridge with
:func:`asyncio.to_thread` so scheduling + the (potentially multi-hour)
wait-for-completion don't block the actor's event loop.

Connection
----------
The client talks to the daprd sidecar's gRPC endpoint.  Reads
``DAPR_GRPC_ENDPOINT`` / ``DAPR_RUNTIME_HOST`` + ``DAPR_GRPC_PORT`` (the
canonical Dapr SDK env vars) via the config, defaulting to
``127.0.0.1:50001`` — the dev-rig sidecar (``legba-runtime`` app, see
docker-compose ``dapr-sidecar``).  A separate long-running
``WorkflowRuntime`` worker (see :mod:`legba.runtime.dapr_workflow.worker`)
must be registered against the same sidecar to actually execute the
workflow + activities.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .gepa import (
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    WorkflowHandleLike,
)
from .workflow import optimizer_workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional dependency probe
# ---------------------------------------------------------------------------


try:  # pragma: no cover - live on the rig, shimmed in minimal envs
    from dapr.ext.workflow import (  # type: ignore[import-not-found]
        DaprWorkflowClient,
        WorkflowStatus,
    )

    _HAVE_DAPR_WF = True
except Exception:  # pragma: no cover
    DaprWorkflowClient = None  # type: ignore[assignment,misc]
    WorkflowStatus = None  # type: ignore[assignment,misc]
    _HAVE_DAPR_WF = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _default_grpc_host() -> str:
    # DAPR_RUNTIME_HOST is the SDK's canonical host var; default to
    # loopback (the sidecar shares the runtime container's netns, or on
    # the bare-host dev rig the sidecar publishes 50001 on 127.0.0.1).
    return os.environ.get("DAPR_RUNTIME_HOST") or "127.0.0.1"


def _default_grpc_port() -> str:
    return os.environ.get("DAPR_GRPC_PORT") or "50001"


@dataclass
class DaprWorkflowClientConfig:
    """Connection + timeout config for the Dapr-Workflow optimizer client.

    ``host`` / ``port`` point at the daprd sidecar gRPC channel.  The
    ``completion_timeout_seconds`` bounds the wait-for-completion call —
    4h is the "multi-hour deterministic workflow" envelope.
    """

    host: str = field(default_factory=_default_grpc_host)
    port: str = field(default_factory=_default_grpc_port)
    completion_timeout_seconds: int = 4 * 60 * 60


# ---------------------------------------------------------------------------
# Handle — satisfies WorkflowHandleLike
# ---------------------------------------------------------------------------


@dataclass
class DaprWorkflowHandle:
    """Handle returned by :meth:`DaprOptimizerWorkflowClient.start_optimizer_workflow`.

    Satisfies :class:`legba.runtime.dapr_workflow.gepa.WorkflowHandleLike`:
    ``id`` (the Dapr instance id), ``result_run_id`` (Dapr workflows have
    a single run per instance — we surface the instance id again, prefixed,
    so the candidate payload's ``temporal_run_id`` slot is non-empty and
    distinguishable from the in-process value), and an async
    ``result()`` that blocks on workflow completion off-thread.
    """

    id: str
    result_run_id: str
    _client: Any
    _config: DaprWorkflowClientConfig

    async def result(self) -> OptimizerWorkflowResult:
        state = await asyncio.to_thread(
            self._client.wait_for_workflow_completion,
            self.id,
            fetch_payloads=True,
            timeout_in_seconds=self._config.completion_timeout_seconds,
        )
        if state is None:
            raise TimeoutError(
                f"dapr workflow {self.id} did not complete within "
                f"{self._config.completion_timeout_seconds}s",
            )
        status_name = str(getattr(state, "runtime_status", "")).upper()
        if not status_name.endswith("COMPLETED"):
            # FAILED / TERMINATED / etc. — surface the engine's failure
            # detail so the actor's failure-classification path sees it.
            detail = getattr(state, "failure_details", None)
            raise RuntimeError(
                f"dapr workflow {self.id} ended status={status_name} "
                f"detail={detail!r}",
            )
        output = state.serialized_output
        return _rehydrate_result(output)


def _rehydrate_result(serialized_output: Any) -> OptimizerWorkflowResult:
    """Turn the engine's serialized workflow output into the result dataclass.

    ``serialized_output`` is the JSON string the workflow body returned
    (a dict).  durabletask hands it back as a JSON-encoded string; the
    SDK may pre-parse it to a dict depending on version — handle both.
    """
    import json

    data: dict[str, Any]
    if isinstance(serialized_output, str):
        data = json.loads(serialized_output) if serialized_output else {}
    elif isinstance(serialized_output, dict):
        data = serialized_output
    else:
        data = {}
    return OptimizerWorkflowResult(
        candidate_prompt_module_text=str(
            data.get("candidate_prompt_module_text", ""),
        ),
        training_set_size=int(data.get("training_set_size", 0)),
        eval_score=float(data.get("eval_score", 0.0)),
        eval_score_delta=float(data.get("eval_score_delta", 0.0)),
        gepa_generation=int(data.get("gepa_generation", 0)),
        parent_prompt_module_text=str(
            data.get("parent_prompt_module_text", ""),
        ),
        diagnostics=dict(data.get("diagnostics") or {}),
    )


# ---------------------------------------------------------------------------
# Client — stable start_optimizer_workflow surface
# ---------------------------------------------------------------------------


class DaprOptimizerWorkflowClient:
    """Dapr-Workflow client exposing the kind-stable optimizer surface.

    Construct cheaply (no connection opened until first schedule).  The
    underlying :class:`dapr.ext.workflow.DaprWorkflowClient` is created
    lazily + shared across calls.
    """

    def __init__(self, config: DaprWorkflowClientConfig | None = None) -> None:
        self._config = config or DaprWorkflowClientConfig()
        self._client: Any | None = None

    @classmethod
    def from_config(
        cls, config: DaprWorkflowClientConfig,
    ) -> "DaprOptimizerWorkflowClient":
        return cls(config=config)

    @property
    def config(self) -> DaprWorkflowClientConfig:
        return self._config

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not _HAVE_DAPR_WF or DaprWorkflowClient is None:  # pragma: no cover
            raise ModuleNotFoundError(
                "dapr.ext.workflow not installed; install "
                "dapr-ext-workflow or set LEGBA_OPTIMIZER_IN_PROCESS=1 "
                "to use the in-process fallback path",
            )
        self._client = DaprWorkflowClient(
            host=self._config.host, port=self._config.port,
        )
        return self._client

    async def start_optimizer_workflow(
        self,
        workflow_input: OptimizerWorkflowInput,
        *,
        workflow_id: str,
    ) -> WorkflowHandleLike:
        """Schedule :func:`optimizer_workflow` and return a handle.

        Same signature + return contract as
        :meth:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient.start_optimizer_workflow`.
        The caller awaits ``handle.result()`` for the
        :class:`OptimizerWorkflowResult`.
        """
        from dataclasses import asdict

        client = await asyncio.to_thread(self._connect)
        wf_input_dict = asdict(workflow_input)
        instance_id = await asyncio.to_thread(
            client.schedule_new_workflow,
            optimizer_workflow,
            input=wf_input_dict,
            instance_id=workflow_id,
        )
        logger.info(
            "dapr_workflow.scheduled instance_id=%s host=%s port=%s",
            instance_id, self._config.host, self._config.port,
        )
        return DaprWorkflowHandle(
            id=str(instance_id),
            result_run_id=f"dapr_wf::{instance_id}",
            _client=client,
            _config=self._config,
        )


# ---------------------------------------------------------------------------
# Factory — env-gated "verify or skip": return None rather than half-build
# ---------------------------------------------------------------------------


def build_dapr_workflow_client(
    *,
    config: DaprWorkflowClientConfig | None = None,
) -> DaprOptimizerWorkflowClient | None:
    """Construct the Dapr-Workflow optimizer client, or ``None`` to fall back.

    Resolution:

      * ``LEGBA_OPTIMIZER_IN_PROCESS=1`` → return ``None`` (caller keeps
        its in-process GEPA fallback).
      * ``dapr.ext.workflow`` not importable → return ``None``.
      * otherwise → a :class:`DaprOptimizerWorkflowClient` (cheap; no
        connection opened yet).

    Returning ``None`` rather than raising lets the optimizer kind's
    :class:`OptimizerDeps.__post_init__` keep its existing
    ``build_default_client()`` fallback chain untouched — this factory is
    purely additive.  The host (P-CUT) calls this and passes the result
    into ``build_analyst_run_method(..., temporal_client=<this>)``.
    """
    if os.environ.get("LEGBA_OPTIMIZER_IN_PROCESS") == "1":
        return None
    if not _HAVE_DAPR_WF:
        logger.info(
            "dapr_workflow.client.unavailable reason=dapr.ext.workflow not installed",
        )
        return None
    return DaprOptimizerWorkflowClient(config=config)


__all__ = [
    "DaprOptimizerWorkflowClient",
    "DaprWorkflowClientConfig",
    "DaprWorkflowHandle",
    "build_dapr_workflow_client",
]
