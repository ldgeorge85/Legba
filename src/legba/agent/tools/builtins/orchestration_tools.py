"""
Orchestration Agent Tools (Airflow)

Define, deploy, trigger, monitor, and manage DAG-based workflows.
Wired to the live AirflowClient by cycle.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...comms.airflow_client import AirflowClient
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

WORKFLOW_DEFINE_DEF = ToolDefinition(
    name="workflow_define",
    description="Deploy a DAG workflow to Airflow. Write a Python DAG definition and "
                "deploy it to the shared dags volume. Airflow's scheduler auto-detects "
                "new DAG files. The DAG code must be a valid Airflow DAG definition.",
    parameters=[
        ToolParameter(name="dag_id", type="string",
                      description="Unique DAG identifier (e.g. daily_cve_ingest)"),
        ToolParameter(name="dag_code", type="string",
                      description="Complete Python DAG definition (valid Airflow DAG file)"),
    ],
)

WORKFLOW_TRIGGER_DEF = ToolDefinition(
    name="workflow_trigger",
    description="Trigger a DAG run in Airflow. Optionally pass configuration "
                "parameters. Returns the run ID and state.",
    parameters=[
        ToolParameter(name="dag_id", type="string",
                      description="DAG to trigger"),
        ToolParameter(name="conf", type="string",
                      description="Optional JSON config dict passed to the DAG run",
                      required=False),
    ],
)

WORKFLOW_STATUS_DEF = ToolDefinition(
    name="workflow_status",
    description="Get the status of a DAG or a specific DAG run. Shows run state, "
                "task instances, start/end times.",
    parameters=[
        ToolParameter(name="dag_id", type="string",
                      description="DAG to query"),
        ToolParameter(name="dag_run_id", type="string",
                      description="Specific run ID to query (omit for latest runs)",
                      required=False),
        ToolParameter(name="include_tasks", type="boolean",
                      description="Include task instance details (default false)",
                      required=False),
    ],
)

WORKFLOW_LIST_DEF = ToolDefinition(
    name="workflow_list",
    description="List all DAGs known to Airflow with their pause state, schedule, "
                "and active status.",
    parameters=[
        ToolParameter(name="limit", type="number",
                      description="Max DAGs to return (default 50)",
                      required=False),
    ],
)

WORKFLOW_PAUSE_DEF = ToolDefinition(
    name="workflow_pause",
    description="Pause or unpause a DAG in Airflow. Paused DAGs do not run on schedule "
                "but can still be triggered manually.",
    parameters=[
        ToolParameter(name="dag_id", type="string",
                      description="DAG to pause/unpause"),
        ToolParameter(name="paused", type="boolean",
                      description="True to pause, false to unpause"),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    registry: ToolRegistry,
    *,
    airflow: AirflowClient,
) -> None:
    """Register orchestration tools with the Airflow client."""

    # -- workflow_define ------------------------------------------------
    async def workflow_define_handler(args: dict) -> str:
        dag_id = args.get("dag_id", "")
        dag_code = args.get("dag_code", "")
        if not dag_id or not dag_code:
            return json.dumps({"error": "dag_id and dag_code are required"})

        result = airflow.deploy_dag(dag_id, dag_code)
        if result.get("deployed"):
            # Unpause the DAG so it becomes active
            if airflow.available:
                await airflow.pause_dag(dag_id, paused=False)
        return json.dumps(result)

    registry.register(WORKFLOW_DEFINE_DEF, workflow_define_handler)

    # -- workflow_trigger -----------------------------------------------
    async def workflow_trigger_handler(args: dict) -> str:
        dag_id = args.get("dag_id", "")
        if not dag_id:
            return json.dumps({"error": "dag_id is required"})

        conf = None
        conf_str = args.get("conf", "")
        if conf_str:
            try:
                conf = json.loads(conf_str)
            except json.JSONDecodeError:
                return json.dumps({"error": "conf must be valid JSON"})

        result = await airflow.trigger_dag(dag_id, conf=conf)
        return json.dumps(result)

    registry.register(WORKFLOW_TRIGGER_DEF, workflow_trigger_handler)

    # -- workflow_status ------------------------------------------------
    async def workflow_status_handler(args: dict) -> str:
        dag_id = args.get("dag_id", "")
        if not dag_id:
            return json.dumps({"error": "dag_id is required"})

        dag_run_id = args.get("dag_run_id", "")
        include_tasks = str(args.get("include_tasks", "false")).lower() == "true"

        if dag_run_id:
            # Get specific run
            run = await airflow.get_dag_run(dag_id, dag_run_id)
            if not run:
                return json.dumps({"error": f"Run {dag_run_id} not found for DAG {dag_id}"})
            if include_tasks:
                run["tasks"] = await airflow.list_task_instances(dag_id, dag_run_id)
            return json.dumps(run)
        else:
            # Get DAG info + recent runs
            dag_info = await airflow.get_dag(dag_id)
            runs = await airflow.list_dag_runs(dag_id, limit=5)
            result = {
                "dag": dag_info or {"error": f"DAG {dag_id} not found"},
                "recent_runs": runs,
            }
            if include_tasks and runs:
                latest_run_id = runs[0].get("dag_run_id")
                if latest_run_id:
                    result["latest_tasks"] = await airflow.list_task_instances(
                        dag_id, latest_run_id
                    )
            return json.dumps(result)

    registry.register(WORKFLOW_STATUS_DEF, workflow_status_handler)

    # -- workflow_list --------------------------------------------------
    async def workflow_list_handler(args: dict) -> str:
        limit = int(args.get("limit", 50))
        dags = await airflow.list_dags(limit=limit)
        return json.dumps({"dags": dags, "count": len(dags)})

    registry.register(WORKFLOW_LIST_DEF, workflow_list_handler)

    # -- workflow_pause -------------------------------------------------
    async def workflow_pause_handler(args: dict) -> str:
        dag_id = args.get("dag_id", "")
        if not dag_id:
            return json.dumps({"error": "dag_id is required"})

        paused = str(args.get("paused", "true")).lower() == "true"
        result = await airflow.pause_dag(dag_id, paused=paused)
        return json.dumps(result)

    registry.register(WORKFLOW_PAUSE_DEF, workflow_pause_handler)
