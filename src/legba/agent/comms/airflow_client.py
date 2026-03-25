"""
Airflow REST API Client

Async client for Apache Airflow. Provides DAG management, triggering,
status queries, and DAG file deployment.

Degrades gracefully if Airflow is unavailable — returns empty results,
logs the failure, never crashes a cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ...shared.config import AirflowConfig

logger = logging.getLogger(__name__)


class AirflowClient:
    """
    Async Airflow REST API client for the Legba agent.

    Usage:
        client = AirflowClient(config)
        await client.connect()
        ...
        await client.close()
    """

    def __init__(self, config: AirflowConfig):
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    async def connect(self) -> bool:
        """Connect to Airflow. Returns True if the API is reachable."""
        try:
            self._client = httpx.AsyncClient(
                base_url=self._config.url,
                auth=(self._config.username, self._config.password),
                timeout=30.0,
                headers={"Content-Type": "application/json"},
            )
            # Verify connection
            resp = await self._client.get("/api/v1/health")
            if resp.status_code == 200:
                self._available = True
                logger.info("Airflow connected: %s", self._config.url)
                return True
            else:
                logger.warning("Airflow health check failed (HTTP %d): %s",
                               resp.status_code, self._config.url)
                self._available = False
                return False
        except Exception as e:
            logger.warning("Airflow unavailable (%s): %s", self._config.url, e)
            self._available = False
            return False

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
            self._available = False

    # ------------------------------------------------------------------
    # DAG file deployment
    # ------------------------------------------------------------------

    def deploy_dag(self, dag_id: str, dag_code: str) -> dict[str, Any]:
        """
        Write a DAG Python file to the shared dags volume.

        This is a synchronous filesystem write — the DAG file lands in
        the shared volume, and Airflow's scheduler picks it up.
        """
        try:
            dags_dir = Path(self._config.dags_path)
            dags_dir.mkdir(parents=True, exist_ok=True)
            dag_file = dags_dir / f"{dag_id}.py"
            dag_file.write_text(dag_code)
            return {"deployed": True, "dag_id": dag_id, "path": str(dag_file)}
        except Exception as e:
            return {"deployed": False, "error": str(e)}

    def remove_dag_file(self, dag_id: str) -> dict[str, Any]:
        """Remove a DAG file from the dags volume."""
        try:
            dag_file = Path(self._config.dags_path) / f"{dag_id}.py"
            if dag_file.exists():
                dag_file.unlink()
                return {"removed": True, "dag_id": dag_id}
            return {"removed": False, "error": f"DAG file not found: {dag_id}.py"}
        except Exception as e:
            return {"removed": False, "error": str(e)}

    # ------------------------------------------------------------------
    # DAG queries
    # ------------------------------------------------------------------

    async def list_dags(self, limit: int = 50) -> list[dict[str, Any]]:
        """List all DAGs known to Airflow."""
        if not self.available:
            return []
        try:
            resp = await self._client.get(
                "/api/v1/dags",
                params={"limit": limit},
            )
            if resp.status_code != 200:
                logger.error("Failed to list DAGs (HTTP %d)", resp.status_code)
                return []
            data = resp.json()
            return [
                {
                    "dag_id": d["dag_id"],
                    "is_paused": d.get("is_paused", True),
                    "is_active": d.get("is_active", False),
                    "schedule_interval": d.get("schedule_interval"),
                    "description": d.get("description", ""),
                    "tags": [t.get("name", "") for t in d.get("tags", [])],
                }
                for d in data.get("dags", [])
            ]
        except Exception as e:
            logger.error("Failed to list DAGs: %s", e)
            return []

    async def get_dag(self, dag_id: str) -> dict[str, Any] | None:
        """Get details for a single DAG."""
        if not self.available:
            return None
        try:
            resp = await self._client.get(f"/api/v1/dags/{dag_id}")
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                return None
            d = resp.json()
            return {
                "dag_id": d["dag_id"],
                "is_paused": d.get("is_paused", True),
                "is_active": d.get("is_active", False),
                "schedule_interval": d.get("schedule_interval"),
                "description": d.get("description", ""),
                "file_token": d.get("file_token", ""),
                "tags": [t.get("name", "") for t in d.get("tags", [])],
            }
        except Exception as e:
            logger.error("Failed to get DAG %s: %s", dag_id, e)
            return None

    # ------------------------------------------------------------------
    # DAG control
    # ------------------------------------------------------------------

    async def trigger_dag(
        self,
        dag_id: str,
        conf: dict[str, Any] | None = None,
        logical_date: str | None = None,
    ) -> dict[str, Any]:
        """
        Trigger a DAG run.

        Args:
            dag_id: The DAG to trigger
            conf: Optional configuration dict passed to the DAG run
            logical_date: Optional execution date (ISO format)
        """
        if not self.available:
            return {"error": "Airflow unavailable"}
        try:
            body: dict[str, Any] = {}
            if conf:
                body["conf"] = conf
            if logical_date:
                body["logical_date"] = logical_date

            resp = await self._client.post(
                f"/api/v1/dags/{dag_id}/dagRuns",
                json=body,
            )
            if resp.status_code in (200, 201):
                run = resp.json()
                return {
                    "dag_id": run.get("dag_id"),
                    "dag_run_id": run.get("dag_run_id"),
                    "state": run.get("state"),
                    "logical_date": run.get("logical_date"),
                }
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    async def pause_dag(self, dag_id: str, paused: bool = True) -> dict[str, Any]:
        """Pause or unpause a DAG."""
        if not self.available:
            return {"error": "Airflow unavailable"}
        try:
            resp = await self._client.patch(
                f"/api/v1/dags/{dag_id}",
                json={"is_paused": paused},
            )
            if resp.status_code == 200:
                d = resp.json()
                return {"dag_id": d["dag_id"], "is_paused": d.get("is_paused")}
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # DAG run queries
    # ------------------------------------------------------------------

    async def list_dag_runs(
        self,
        dag_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List recent runs for a DAG."""
        if not self.available:
            return []
        try:
            resp = await self._client.get(
                f"/api/v1/dags/{dag_id}/dagRuns",
                params={"limit": limit, "order_by": "-start_date"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [
                {
                    "dag_run_id": r.get("dag_run_id"),
                    "state": r.get("state"),
                    "start_date": r.get("start_date"),
                    "end_date": r.get("end_date"),
                    "logical_date": r.get("logical_date"),
                }
                for r in data.get("dag_runs", [])
            ]
        except Exception as e:
            logger.error("Failed to list runs for %s: %s", dag_id, e)
            return []

    async def get_dag_run(
        self,
        dag_id: str,
        dag_run_id: str,
    ) -> dict[str, Any] | None:
        """Get details for a specific DAG run."""
        if not self.available:
            return None
        try:
            resp = await self._client.get(
                f"/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}",
            )
            if resp.status_code != 200:
                return None
            r = resp.json()
            return {
                "dag_run_id": r.get("dag_run_id"),
                "dag_id": r.get("dag_id"),
                "state": r.get("state"),
                "start_date": r.get("start_date"),
                "end_date": r.get("end_date"),
                "logical_date": r.get("logical_date"),
                "conf": r.get("conf", {}),
            }
        except Exception as e:
            logger.error("Failed to get run %s/%s: %s", dag_id, dag_run_id, e)
            return None

    # ------------------------------------------------------------------
    # Task instance queries
    # ------------------------------------------------------------------

    async def list_task_instances(
        self,
        dag_id: str,
        dag_run_id: str,
    ) -> list[dict[str, Any]]:
        """List task instances for a DAG run."""
        if not self.available:
            return []
        try:
            resp = await self._client.get(
                f"/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances",
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [
                {
                    "task_id": t.get("task_id"),
                    "state": t.get("state"),
                    "start_date": t.get("start_date"),
                    "end_date": t.get("end_date"),
                    "duration": t.get("duration"),
                    "try_number": t.get("try_number"),
                }
                for t in data.get("task_instances", [])
            ]
        except Exception as e:
            logger.error("Failed to list tasks for %s/%s: %s", dag_id, dag_run_id, e)
            return []
