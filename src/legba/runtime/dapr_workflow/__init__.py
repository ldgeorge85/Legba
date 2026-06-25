# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-16 Dapr-Workflow runtime package — optimizer workflow + worker.

The optimizer analyst kind (GEPA/DSPy self-improvement loop) is the one
Legba analyst whose work is fundamentally multi-hour — a deterministic
outer loop driving non-deterministic LLM activities — and therefore the
one kind that needs a durable-workflow substrate rather than a
single-shot ≤180 s Dapr Actor.

Why Dapr Workflow (and the Temporal-removal story)
--------------------------------------------------
Legba already runs a daprd sidecar (actors, placement, scheduler, state
store) for every other runtime concern.  Hosting the optimizer as a
*Dapr Workflow* — the durabletask-backed orchestration engine that ships
inside the same daprd — collapses the runtime to a single control plane:
no second cluster (Temporal frontend/history/matching), no second
Postgres database pair (``temporal_persistence`` / ``temporal_visibility``),
no second worker image.  P-CUT completed the removal: ``temporalio`` left
the dependency set with L-205/P-16, and the retired
``legba.runtime.temporal`` package was deleted outright — its live pieces
(the workflow I/O dataclasses + the GEPA loop) moved into
:mod:`legba.runtime.dapr_workflow.gepa`.

Boundary (the kind-stable client contract)
------------------------------------------
The optimizer kind in :mod:`legba.data.analysts.optimizer` constructs an
:class:`legba.data.analysts.optimizer.OptimizerDeps` carrying a
``temporal_client`` (historical name, kept stable — it is just "the
workflow client" slot).  That client must satisfy the
:class:`legba.runtime.dapr_workflow.gepa.WorkflowHandleLike`-returning
contract::

    async def start_optimizer_workflow(
        self, workflow_input: OptimizerWorkflowInput, *, workflow_id: str,
    ) -> WorkflowHandleLike

:class:`legba.runtime.dapr_workflow.client.DaprOptimizerWorkflowClient`
satisfies that contract, as does the in-process fallback
(:class:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient`) — so
the optimizer kind never branches on the backend.

Modules
-------
  * :mod:`.gepa`       — the GEPA algorithm itself
                          (``_run_gepa_loop`` / ``run_optimizer_in_process``)
                          + the substrate-agnostic workflow I/O dataclasses
                          + the in-process fallback client.
  * :mod:`.workflow`   — :func:`optimizer_workflow` deterministic
                          generator body (validate → compile) + activity
                          functions, registered with a
                          ``WorkflowRuntime``.
  * :mod:`.client`     — :class:`DaprOptimizerWorkflowClient` (schedule +
                          wait) + :func:`build_dapr_workflow_client`
                          env-gated factory.  Exposes the same
                          ``start_optimizer_workflow`` surface the kind
                          calls.
  * :mod:`.worker`     — ``legba-dapr-workflow-worker`` entrypoint — a
                          long-running ``WorkflowRuntime`` that registers
                          the workflow + activities against the daprd
                          sidecar's gRPC channel.  P-CUT wires this into
                          the host (or runs it as a sidecar container);
                          this package only exposes the clean entrypoint.

Graceful degradation
---------------------
When ``dapr.ext.workflow`` isn't importable (minimal test envs), the
client + workflow modules import cleanly with the symbols set to ``None``
and :func:`build_dapr_workflow_client` returns ``None`` so the optimizer
kind's :class:`OptimizerDeps.__post_init__` keeps its existing
``build_default_client()`` fallback (in-process GEPA loop).
"""

from __future__ import annotations

try:  # pragma: no cover - probes the optional dep
    import dapr.ext.workflow as _dapr_wf_mod  # noqa: F401

    DAPR_WORKFLOW_AVAILABLE = True
except Exception:  # pragma: no cover
    DAPR_WORKFLOW_AVAILABLE = False


# Re-export the stable workflow I/O shapes.  They are plain
# JSON-serializable dataclasses — substrate-agnostic.
from .gepa import (  # noqa: E402
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
)
from .client import (  # noqa: E402
    DaprOptimizerWorkflowClient,
    DaprWorkflowClientConfig,
    build_dapr_workflow_client,
)

# Deep-consult workflow (anchor §5 PIECE 4) — the altitude-3 on-demand deep
# analysis job. Same actor→workflow substrate as the optimizer, pointed at
# analysis (plan → acquire → analyze → synthesize).
from .deep_consult import (  # noqa: E402
    DeepConsultWorkflowInput,
    DeepConsultWorkflowResult,
)
from .deep_consult_client import (  # noqa: E402
    DaprDeepConsultWorkflowClient,
    build_deep_consult_workflow_client,
)

__all__ = [
    "DAPR_WORKFLOW_AVAILABLE",
    "DaprDeepConsultWorkflowClient",
    "DaprOptimizerWorkflowClient",
    "DaprWorkflowClientConfig",
    "DeepConsultWorkflowInput",
    "DeepConsultWorkflowResult",
    "OptimizerWorkflowInput",
    "OptimizerWorkflowResult",
    "build_dapr_workflow_client",
    "build_deep_consult_workflow_client",
]
