# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dapr-Workflow deep-consult client — DETACHED submit + status poll.

Sibling of :class:`legba.runtime.dapr_workflow.client.DaprOptimizerWorkflowClient`.
It REUSES :class:`DaprWorkflowClientConfig` + the lazy-connect / ``asyncio.to_thread``
bridge pattern from ``client.py``; the only genuinely-new behaviour is the
**schedule-vs-await split**:

  * :meth:`DaprDeepConsultWorkflowClient.start_deep_consult_workflow` schedules
    :func:`deep_consult_workflow` and returns the instance id IMMEDIATELY —
    it does NOT call ``wait_for_workflow_completion``.  Deep consult is
    fire-and-forget (minutes → hours); the submit path must never block the
    180s sidecar-HTTP envelope the consult invoke path is pinned by.
  * :meth:`DaprDeepConsultWorkflowClient.get_status` reads
    ``DaprWorkflowClient.get_workflow_state`` (confirmed against the installed
    ``dapr.ext.workflow`` — the SDK exposes ``get_workflow_state`` returning a
    ``WorkflowState`` whose ``runtime_status`` + ``serialized_output`` are the
    SAME shape ``DaprWorkflowHandle.result()`` reads, ``client.py:139-149``).

The optimizer client awaits inside the actor run because GEPA is a blocking
analyst run; deep consult is read later, so we never await here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from typing import Any

from .client import DaprWorkflowClientConfig
from .deep_consult import DeepConsultWorkflowInput, DeepConsultWorkflowResult
from .deep_consult_workflow import deep_consult_workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional dependency probe
# ---------------------------------------------------------------------------


try:  # pragma: no cover - live on the rig, shimmed in minimal envs
    from dapr.ext.workflow import DaprWorkflowClient  # type: ignore[import-not-found]

    _HAVE_DAPR_WF = True
except Exception:  # pragma: no cover
    DaprWorkflowClient = None  # type: ignore[assignment,misc]
    _HAVE_DAPR_WF = False


def _rehydrate_result(serialized_output: Any) -> DeepConsultWorkflowResult:
    """Turn the engine's serialized workflow output into the result dataclass.

    Mirrors ``client._rehydrate_result`` — durabletask hands the body's return
    dict back as a JSON string (or a pre-parsed dict on some SDK versions).
    """
    import json

    data: dict[str, Any]
    if isinstance(serialized_output, str):
        data = json.loads(serialized_output) if serialized_output else {}
    elif isinstance(serialized_output, dict):
        data = serialized_output
    else:
        data = {}
    return DeepConsultWorkflowResult(
        finding_id=str(data.get("finding_id", "")),
        answer=str(data.get("answer", "")),
        cited_substrate_refs=[str(r) for r in (data.get("cited_substrate_refs") or [])],
        fact_ids=[str(f) for f in (data.get("fact_ids") or [])],
        hypothesis_ids=[str(h) for h in (data.get("hypothesis_ids") or [])],
        uncertainty=float(data.get("uncertainty", 1.0)),
        run_id=str(data.get("run_id", "")),
        stage_diagnostics=dict(data.get("stage_diagnostics") or {}),
    )


class DaprDeepConsultWorkflowClient:
    """Detached deep-consult workflow client.

    Construct cheaply (no connection opened until first schedule). The
    underlying :class:`dapr.ext.workflow.DaprWorkflowClient` is created lazily +
    shared across calls (same as the optimizer client).
    """

    def __init__(self, config: DaprWorkflowClientConfig | None = None) -> None:
        self._config = config or DaprWorkflowClientConfig()
        self._client: Any | None = None

    @property
    def config(self) -> DaprWorkflowClientConfig:
        return self._config

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not _HAVE_DAPR_WF or DaprWorkflowClient is None:  # pragma: no cover
            raise ModuleNotFoundError(
                "dapr.ext.workflow not installed; deep consult requires the "
                "Dapr Workflow engine (no in-process fallback for the detached "
                "submit — the run is durable by design)",
            )
        self._client = DaprWorkflowClient(
            host=self._config.host, port=self._config.port,
        )
        return self._client

    async def start_deep_consult_workflow(
        self,
        wf_input: DeepConsultWorkflowInput,
        *,
        workflow_id: str,
    ) -> str:
        """Schedule :func:`deep_consult_workflow` and return the instance id.

        DETACHED — does NOT await ``result()``. Returns the task id the caller
        polls via :meth:`get_status`.
        """
        client = await asyncio.to_thread(self._connect)
        instance_id = await asyncio.to_thread(
            client.schedule_new_workflow,
            deep_consult_workflow,
            input=asdict(wf_input),
            instance_id=workflow_id,
        )
        logger.info(
            "deep_consult.scheduled instance_id=%s host=%s port=%s",
            instance_id, self._config.host, self._config.port,
        )
        return str(instance_id)

    async def get_status(self, task_id: str) -> dict[str, Any]:
        """Read the workflow engine state for ``task_id``.

        Returns ``{"status": running|completed|failed|pending|unknown, ...}``;
        on COMPLETED the produced :class:`DeepConsultWorkflowResult` is
        rehydrated and folded in (finding_id, answer, uncertainty, cited refs).
        """
        client = await asyncio.to_thread(self._connect)
        state = await asyncio.to_thread(
            client.get_workflow_state, task_id, fetch_payloads=True,
        )
        if state is None:
            return {"status": "unknown", "task_id": task_id, "detail": "no_state"}

        status_name = str(getattr(state, "runtime_status", "")).upper()
        if status_name.endswith("COMPLETED"):
            result = _rehydrate_result(getattr(state, "serialized_output", None))
            return {
                "status": "completed",
                "task_id": task_id,
                "finding_id": result.finding_id,
                "answer": result.answer,
                "uncertainty": result.uncertainty,
                "cited_refs": result.cited_substrate_refs,
                "fact_ids": result.fact_ids,
                "hypothesis_ids": result.hypothesis_ids,
                "run_id": result.run_id,
            }
        if status_name.endswith("FAILED") or status_name.endswith("TERMINATED"):
            detail = getattr(state, "failure_details", None)
            return {
                "status": "failed",
                "task_id": task_id,
                "detail": str(detail) if detail is not None else status_name,
            }
        if status_name.endswith("PENDING"):
            return {"status": "pending", "task_id": task_id}
        if status_name.endswith("RUNNING"):
            return {"status": "running", "task_id": task_id}
        return {"status": "running", "task_id": task_id, "engine_status": status_name}


def build_deep_consult_workflow_client(
    *,
    config: DaprWorkflowClientConfig | None = None,
) -> DaprDeepConsultWorkflowClient | None:
    """Construct the deep-consult client, or ``None`` to signal unavailable.

    Resolution (mirrors ``build_dapr_workflow_client``):

      * ``dapr.ext.workflow`` not importable → return ``None`` (the registry
        route then surfaces a clean 503 rather than half-building).
      * otherwise → a :class:`DaprDeepConsultWorkflowClient` (cheap; no
        connection opened yet).
    """
    if not _HAVE_DAPR_WF:
        logger.info(
            "deep_consult.client.unavailable reason=dapr.ext.workflow not installed",
        )
        return None
    return DaprDeepConsultWorkflowClient(config=config)


__all__ = [
    "DaprDeepConsultWorkflowClient",
    "build_deep_consult_workflow_client",
]
