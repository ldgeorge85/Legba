"""
Audit Log Indexer

Indexes cycle logs into a dedicated OpenSearch instance that the agent
cannot access. Uses httpx directly (not opensearch-py) to keep supervisor
dependencies light.

The audit OpenSearch instance is separate from the agent's data OpenSearch.
The agent never receives the audit instance hostname — isolation by env var
omission. Primary log record stays on disk (JSONL archive); OpenSearch is
a searchable copy with ISM retention policies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from ..shared.config import OpenSearchConfig

logger = logging.getLogger(__name__)

# Index template for audit log indices
_INDEX_TEMPLATE = {
    "index_patterns": ["legba-audit-*"],
    "priority": 100,
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {
            "properties": {
                "timestamp": {"type": "date"},
                "cycle": {"type": "integer"},
                "event": {"type": "keyword"},
                # LLM call fields
                "purpose": {"type": "keyword"},
                "prompt": {"type": "object", "enabled": False},
                "response": {"type": "object", "enabled": False},
                "finish_reason": {"type": "keyword"},
                "usage": {"type": "object"},
                "latency_ms": {"type": "float"},
                "tool_calls": {"type": "object", "enabled": False},
                # Tool call fields
                "tool_name": {"type": "keyword"},
                "arguments": {"type": "object", "enabled": False},
                "result": {"type": "text"},
                "duration_ms": {"type": "float"},
                # Phase fields
                "phase": {"type": "keyword"},
                # Error fields
                "error": {"type": "text"},
                # Memory fields
                "operation": {"type": "keyword"},
                "store": {"type": "keyword"},
                # Self-modification fields
                "action": {"type": "keyword"},
                "file_path": {"type": "keyword"},
            }
        },
    },
}

# ISM policy: delete audit indices after 90 days
_ISM_POLICY = {
    "policy": {
        "description": "Legba audit log retention — 90 day hot, then delete",
        "default_state": "hot",
        "states": [
            {
                "name": "hot",
                "actions": [],
                "transitions": [
                    {
                        "state_name": "delete",
                        "conditions": {"min_index_age": "90d"},
                    }
                ],
            },
            {
                "name": "delete",
                "actions": [{"delete": {}}],
            },
        ],
        "ism_template": [
            {"index_patterns": ["legba-audit-*"], "priority": 100}
        ],
    }
}


class AuditIndexer:
    """
    Indexes cycle logs into a dedicated audit OpenSearch instance.

    Graceful degradation: if the audit instance is unavailable, logs are
    still archived to disk. Indexing is best-effort.
    """

    def __init__(self, config: OpenSearchConfig):
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    async def connect(self) -> bool:
        """Connect to audit OpenSearch and set up index template + ISM policy."""
        try:
            self._client = httpx.AsyncClient(
                base_url=self._config.url,
                timeout=30.0,
            )

            resp = await self._client.get("/_cluster/health")
            if resp.status_code != 200:
                logger.warning(
                    "Audit OpenSearch unhealthy at %s: %d",
                    self._config.url, resp.status_code,
                )
                return False

            logger.info("Audit OpenSearch connected: %s", self._config.url)

            await self._ensure_index_template()
            await self._ensure_ism_policy()

            self._available = True
            return True

        except Exception as e:
            logger.warning("Audit OpenSearch unavailable at %s: %s", self._config.url, e)
            self._available = False
            return False

    async def index_cycle_logs(
        self, cycle_number: int, log_entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Bulk index log entries for a cycle into the monthly audit index.

        Returns summary: {indexed: int, errors: int, index: str}
        """
        if not self.available or not log_entries:
            return {"indexed": 0, "errors": 0, "index": ""}

        # Monthly index: legba-audit-YYYY.MM
        now = datetime.now(timezone.utc)
        index = f"legba-audit-{now:%Y.%m}"

        # Build bulk request body (newline-delimited JSON)
        lines: list[str] = []
        for entry in log_entries:
            action = json.dumps({"index": {"_index": index}})
            doc = json.dumps(entry, default=str)
            lines.append(action)
            lines.append(doc)
        body = "\n".join(lines) + "\n"

        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                "/_bulk",
                content=body,
                headers={"Content-Type": "application/x-ndjson"},
            )

            if resp.status_code not in (200, 201):
                logger.warning(
                    "Audit bulk index failed for cycle %d: %d %s",
                    cycle_number, resp.status_code, resp.text[:200],
                )
                return {"indexed": 0, "errors": 1, "index": index}

            result = resp.json()
            errors = sum(
                1 for item in result.get("items", [])
                if item.get("index", {}).get("error")
            )
            indexed = len(result.get("items", [])) - errors

            if errors:
                logger.warning(
                    "Audit indexed cycle %d: %d docs, %d errors",
                    cycle_number, indexed, errors,
                )
            else:
                logger.info(
                    "Audit indexed cycle %d: %d docs → %s",
                    cycle_number, indexed, index,
                )

            return {"indexed": indexed, "errors": errors, "index": index}

        except Exception as e:
            logger.warning("Audit index failed for cycle %d: %s", cycle_number, e)
            return {"indexed": 0, "errors": 1, "index": index}

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._available = False

    async def _ensure_index_template(self) -> None:
        """Create or update the index template for legba-audit-* indices."""
        try:
            resp = await self._client.put(  # type: ignore[union-attr]
                "/_index_template/legba-audit",
                json=_INDEX_TEMPLATE,
            )
            if resp.status_code in (200, 201):
                logger.info("Audit index template created/updated")
            else:
                logger.warning("Audit index template setup failed: %d", resp.status_code)
        except Exception as e:
            logger.warning("Audit index template setup error: %s", e)

    async def _ensure_ism_policy(self) -> None:
        """Create or update the ISM policy for 90-day retention."""
        try:
            resp = await self._client.put(  # type: ignore[union-attr]
                "/_plugins/_ism/policies/legba-audit-retention",
                json=_ISM_POLICY,
            )
            if resp.status_code in (200, 201):
                logger.info("Audit ISM policy created/updated")
            else:
                # ISM plugin might not be available — non-fatal
                logger.info(
                    "Audit ISM policy setup returned %d (may not be available)",
                    resp.status_code,
                )
        except Exception as e:
            logger.info("Audit ISM policy setup skipped: %s", e)
