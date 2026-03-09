"""
OpenSearch Store

Async client wrapper for OpenSearch. Provides index management,
document CRUD, full-text search, and aggregations.

Degrades gracefully if OpenSearch is unavailable — returns empty
results, logs the failure, never crashes a cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from opensearchpy import OpenSearch, RequestError, NotFoundError
from opensearchpy._async.client import AsyncOpenSearch

from ...shared.config import OpenSearchConfig

logger = logging.getLogger(__name__)


class OpenSearchStore:
    """
    Async OpenSearch client for the Legba agent.

    Usage:
        store = OpenSearchStore(config)
        await store.connect()
        ...
        await store.close()
    """

    def __init__(self, config: OpenSearchConfig):
        self._config = config
        self._client: AsyncOpenSearch | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    async def connect(self) -> bool:
        """Connect to OpenSearch. Returns True if successful."""
        try:
            auth = None
            if self._config.username and self._config.password:
                auth = (self._config.username, self._config.password)

            self._client = AsyncOpenSearch(
                hosts=[{"host": self._config.host, "port": self._config.port}],
                http_auth=auth,
                scheme=self._config.scheme,
                timeout=30,
                max_retries=2,
                retry_on_timeout=True,
            )
            # Verify connection
            info = await self._client.info()
            version = info.get("version", {}).get("number", "unknown")
            logger.info("OpenSearch connected: %s:%d (v%s)",
                        self._config.host, self._config.port, version)

            # Clear any create-index blocks (set by security plugin disable script)
            try:
                await self._client.cluster.put_settings(body={
                    "persistent": {"cluster.blocks.create_index": None}
                })
            except Exception:
                pass  # Non-fatal — may not have permission

            self._available = True
            return True
        except Exception as e:
            logger.warning("OpenSearch unavailable (%s:%d): %s",
                           self._config.host, self._config.port, e)
            self._available = False
            return False

    async def close(self) -> None:
        """Close the OpenSearch connection."""
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            self._available = False

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def create_index(
        self,
        index: str,
        mappings: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an index with optional mappings and settings."""
        if not self.available:
            return {"error": "OpenSearch unavailable"}
        try:
            body: dict[str, Any] = {}
            if settings:
                body["settings"] = settings
            if mappings:
                body["mappings"] = mappings

            result = await self._client.indices.create(index=index, body=body or None)
            return {"acknowledged": result.get("acknowledged", False), "index": index}
        except RequestError as e:
            # Index already exists — treat as success
            if "resource_already_exists_exception" in str(e):
                return {"acknowledged": True, "index": index, "already_exists": True}
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    async def delete_index(self, index: str) -> dict[str, Any]:
        """Delete an index."""
        if not self.available:
            return {"error": "OpenSearch unavailable"}
        try:
            result = await self._client.indices.delete(index=index)
            return {"acknowledged": result.get("acknowledged", False), "index": index}
        except NotFoundError:
            return {"error": f"Index '{index}' not found"}
        except Exception as e:
            return {"error": str(e)}

    async def list_indices(self, pattern: str = "legba-*") -> list[dict[str, Any]]:
        """List indices matching a pattern."""
        if not self.available:
            return []
        try:
            result = await self._client.cat.indices(
                index=pattern, format="json", h="index,docs.count,store.size,health,status",
            )
            return [
                {
                    "index": r.get("index", ""),
                    "docs_count": int(r.get("docs.count", 0) or 0),
                    "store_size": r.get("store.size", "0b"),
                    "health": r.get("health", ""),
                    "status": r.get("status", ""),
                }
                for r in result
            ]
        except NotFoundError:
            return []
        except Exception as e:
            logger.error("Failed to list indices: %s", e)
            return []

    # ------------------------------------------------------------------
    # Document CRUD
    # ------------------------------------------------------------------

    async def index_document(
        self,
        index: str,
        document: dict[str, Any],
        doc_id: str | None = None,
    ) -> dict[str, Any]:
        """Index a single document."""
        if not self.available:
            return {"error": "OpenSearch unavailable"}
        try:
            result = await self._client.index(
                index=index,
                body=document,
                id=doc_id,
                refresh="wait_for",
            )
            return {
                "result": result.get("result", ""),
                "_id": result.get("_id", ""),
                "_index": result.get("_index", ""),
            }
        except Exception as e:
            return {"error": str(e)}

    async def bulk_index(
        self,
        index: str,
        documents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk index multiple documents."""
        if not self.available:
            return {"error": "OpenSearch unavailable"}
        if not documents:
            return {"indexed": 0, "errors": 0}
        try:
            actions = []
            for doc in documents:
                action = {"index": {"_index": index}}
                doc_id = doc.pop("_id", None)
                if doc_id:
                    action["index"]["_id"] = doc_id
                actions.append(json.dumps(action))
                actions.append(json.dumps(doc, default=str))

            body = "\n".join(actions) + "\n"
            result = await self._client.bulk(body=body, refresh="wait_for")

            errors = sum(1 for item in result.get("items", [])
                         if item.get("index", {}).get("error"))
            return {
                "indexed": len(documents) - errors,
                "errors": errors,
                "took_ms": result.get("took", 0),
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_document(
        self,
        index: str,
        doc_id: str,
    ) -> dict[str, Any] | None:
        """Get a document by ID."""
        if not self.available:
            return None
        try:
            result = await self._client.get(index=index, id=doc_id)
            if result.get("found"):
                doc = result.get("_source", {})
                doc["_id"] = result["_id"]
                return doc
            return None
        except NotFoundError:
            return None
        except Exception as e:
            logger.error("Failed to get document %s/%s: %s", index, doc_id, e)
            return None

    async def delete_document(
        self,
        index: str,
        doc_id: str,
    ) -> bool:
        """Delete a document by ID."""
        if not self.available:
            return False
        try:
            await self._client.delete(index=index, id=doc_id, refresh="wait_for")
            return True
        except NotFoundError:
            return False
        except Exception as e:
            logger.error("Failed to delete document %s/%s: %s", index, doc_id, e)
            return False

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        index: str,
        query: dict[str, Any],
        size: int = 10,
        sort: list[dict[str, Any]] | None = None,
        source: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a search query.

        Returns: {hits: [{_id, _score, ...fields}], total: int, took_ms: int}
        """
        if not self.available:
            return {"hits": [], "total": 0, "took_ms": 0}
        try:
            body: dict[str, Any] = {"query": query, "size": size}
            if sort:
                body["sort"] = sort
            if source:
                body["_source"] = source

            result = await self._client.search(index=index, body=body)

            hits = []
            for hit in result.get("hits", {}).get("hits", []):
                doc = hit.get("_source", {})
                doc["_id"] = hit["_id"]
                doc["_score"] = hit.get("_score")
                hits.append(doc)

            total = result.get("hits", {}).get("total", {})
            total_count = total.get("value", 0) if isinstance(total, dict) else total

            return {
                "hits": hits,
                "total": total_count,
                "took_ms": result.get("took", 0),
            }
        except NotFoundError:
            return {"hits": [], "total": 0, "took_ms": 0, "error": f"Index '{index}' not found"}
        except Exception as e:
            return {"hits": [], "total": 0, "took_ms": 0, "error": str(e)}

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    async def aggregate(
        self,
        index: str,
        aggs: dict[str, Any],
        query: dict[str, Any] | None = None,
        size: int = 0,
    ) -> dict[str, Any]:
        """
        Execute an aggregation query.

        Returns: {aggregations: {...}, took_ms: int}
        """
        if not self.available:
            return {"aggregations": {}, "took_ms": 0}
        try:
            body: dict[str, Any] = {"aggs": aggs, "size": size}
            if query:
                body["query"] = query

            result = await self._client.search(index=index, body=body)

            return {
                "aggregations": result.get("aggregations", {}),
                "took_ms": result.get("took", 0),
            }
        except NotFoundError:
            return {"aggregations": {}, "took_ms": 0, "error": f"Index '{index}' not found"}
        except Exception as e:
            return {"aggregations": {}, "took_ms": 0, "error": str(e)}
